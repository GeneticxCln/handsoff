"""Edge-case tests for the runtime-hardening paths merged from the parallel
session: _prepare_runtime / _private_dir / _secure_file / _secure_runtime_files
and _remove_stale_control_socket. Every test here covers a case the original
happy-path suite did not."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import HERE as ROOT, run_driver

from core import settings as _core_settings

HERE = ROOT   # the repo root


# NOTE: this module used to load its OWN second copy of the monolith (as
# handsoff_core_hardening), which defeated conftest's config isolation — that
# import read the developer's real ~/.config/handsoff/settings.json and
# resolved CONFIG_DIR/STATE_DIR into their real home. It now shares the
# session-scoped instance from conftest, so there is exactly one module object
# and it always points at a throw-away config.


def test_the_suite_cannot_touch_the_real_config_or_state(H):
    """Guard the isolation itself, not just the code it protects.

    If this ever regresses, every later run silently reads and writes the
    developer's real settings, history and control socket — and starts grading
    itself against their personal configuration again.
    """
    real_home = Path.home()
    for name in ("CONFIG_DIR", "STATE_DIR", "HISTORY_FILE", "MEMORY_FILE",
                 "LOG_FILE", "CONTROL_SOCK"):
        path = getattr(H, name)
        assert real_home not in path.parents, (name, path)
        assert real_home != path, (name, path)
    assert H.SETTINGS_FILE != real_home / ".config" / "handsoff" / "settings.json"


@pytest.fixture()
def sandbox(H, monkeypatch, tmp_path):
    """Redirect every runtime path into a private tmp sandbox."""
    cfg = tmp_path / "cfg"
    state = tmp_path / "state"
    monkeypatch.setattr(H, "CONFIG_DIR", cfg)
    monkeypatch.setattr(H, "STATE_DIR", state)
    monkeypatch.setattr(H, "WHISPER_MODEL_DIR", cfg / "whisper-model")
    monkeypatch.setattr(H._audio, "TTS_REFERENCE", "")
    monkeypatch.setattr(H, "SETTINGS_FILE", cfg / "settings.json")
    monkeypatch.setattr(H, "HISTORY_FILE", cfg / "history.json")
    monkeypatch.setattr(H, "MEMORY_FILE", cfg / "memory.json")
    monkeypatch.setattr(H, "CRASH_LOG", state / "crash.log")
    monkeypatch.setattr(H, "PENDING_FILE", state / "pending-restart.json")
    monkeypatch.setattr(H, "LOCK_FILE", state / "handsoff.lock")
    monkeypatch.setattr(H, "LOG_FILE", state / "handsoff.log")
    monkeypatch.setattr(H, "CONTROL_SOCK", state / "control.sock")
    monkeypatch.setattr(H, "CONTROL_TOKEN", state / "control.token")
    monkeypatch.setattr(H, "MIC_EVENTS_FILE", state / "mic-health.json")
    monkeypatch.setattr(H, "REMINDERS_FILE", state / "reminders.json")
    return tmp_path


def _mk_socket(path: Path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    return s


class TestPrivateDir:
    def test_creates_dir_owner_only(self, H, sandbox):
        d = sandbox / "cfg" / "sub" / "deep"
        assert H._private_dir(d) is True
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_tightens_existing_group_world_readable(self, H, sandbox):
        d = sandbox / "cfg"
        d.mkdir()
        d.chmod(0o755)
        assert H._private_dir(d) is True
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_refuses_symlink(self, H, sandbox):
        target = sandbox / "elsewhere"
        target.mkdir()
        link = sandbox / "cfg"
        link.symlink_to(target)
        assert H._private_dir(link) is False

    def test_refuses_regular_file_at_dir_path(self, H, sandbox):
        f = sandbox / "cfg"
        f.write_text("not a dir")
        assert H._private_dir(f) is False

    def test_refuses_foreign_owned_dir(self, H, monkeypatch, sandbox):
        d = sandbox / "cfg"
        d.mkdir()
        monkeypatch.setattr(H.os, "getuid", lambda: d.stat().st_uid + 1)
        assert H._private_dir(d) is False


class TestSecureFile:
    def test_missing_file_is_success(self, H, sandbox):
        assert _core_settings.secure_file(sandbox / "state" / "nope.json") is True

    def test_regular_file_gets_0600(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "f.json"
        f.write_text("{}")
        f.chmod(0o644)
        assert _core_settings.secure_file(f) is True
        assert (f.stat().st_mode & 0o777) == 0o600

    def test_refuses_symlink_including_broken(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        target = sandbox / "real.txt"
        target.write_text("data")
        link = state / "link.json"
        link.symlink_to(target)
        assert _core_settings.secure_file(link) is False
        broken = state / "broken.json"
        broken.symlink_to(sandbox / "ghost")     # broken symlink
        assert _core_settings.secure_file(broken) is False

    def test_refuses_directory(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        d = state / "dir.json"
        d.mkdir()
        assert _core_settings.secure_file(d) is False

    def test_refuses_foreign_owned(self, H, monkeypatch, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "f.json"
        f.write_text("{}")
        monkeypatch.setattr(H.os, "getuid", lambda: f.stat().st_uid + 1)
        assert _core_settings.secure_file(f) is False

    def test_stale_permissive_socket_self_heals(self, H, sandbox):
        """E10 (the wedge): a stale socket with 0777 (umask 000 bind) must be
        tightened, not make _prepare_runtime refuse startup forever."""
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        old = os.umask(0o000)
        try:
            s = _mk_socket(sp)                    # mode 0777 under umask 0
        finally:
            os.umask(old)
        assert (sp.stat().st_mode & 0o777) == 0o777
        # self-healed, not refused
        assert _core_settings.secure_file(sp) is True
        assert (sp.stat().st_mode & 0o777) == 0o600
        s.close()

    def test_already_private_socket_untouched(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        sp.chmod(0o600)
        assert _core_settings.secure_file(sp) is True
        s.close()


class TestControlCapabilityToken:
    """The token is what separates "reports state" from "changes it".

    `_peer_uid` plus a 0700 state directory keep OTHER users off the socket;
    neither can exclude a same-UID process (a compromised child of ours, a
    sandboxed app running as the user), and every verb used to ride the same
    trust. These pin the FILE half: where it lives, how it is written, that it
    rotates, and that reading it or forging it is not the same as holding it.
    """

    def test_the_token_is_owner_only_inside_the_state_directory(self, H,
                                                                sandbox):
        assert H._prepare_runtime() is True
        token = H._rotate_control_token()
        assert token and len(token) == H._CONTROL_TOKEN_BYTES * 2
        assert H.CONTROL_TOKEN.parent == H.STATE_DIR
        assert (H.CONTROL_TOKEN.stat().st_mode & 0o777) == 0o600
        assert H._read_control_token() == token

    def test_a_rotation_replaces_the_previous_token(self, H, sandbox):
        """A token left in the file by an earlier run must be worth nothing,
        or the file's whole history would stay valid."""
        H._prepare_runtime()
        first = H._rotate_control_token()
        second = H._rotate_control_token()
        assert first != second
        assert H._read_control_token() == second
        assert H._read_control_token() != first

    def test_a_missing_file_reads_as_no_token(self, H, sandbox):
        H._prepare_runtime()
        assert not H.CONTROL_TOKEN.exists()
        assert H._read_control_token() is None

    def test_an_unwritable_state_dir_reports_no_token_rather_than_raising(
            self, H, sandbox, monkeypatch):
        """The server refuses only the state-changing verbs in this case; a
        token it cannot write must not stop the bubble from serving at all."""
        H._prepare_runtime()
        monkeypatch.setattr(H, "CONTROL_TOKEN",
                            sandbox / "no-such-dir" / "control.token")
        monkeypatch.setattr(H._core_settings, "atomic_private_write",
                            _raise_oserror)
        assert H._rotate_control_token() is None


def _raise_oserror(*a, **k):
    raise OSError("read-only state directory")


class TestPrepareRuntime:
    def test_fresh_sandbox_passes_and_private(self, H, sandbox):
        assert H._prepare_runtime() is True
        for d in (sandbox / "cfg", sandbox / "state"):
            assert (d.stat().st_mode & 0o777) == 0o700

    def test_refuses_when_a_dir_is_symlink(self, H, sandbox):
        (sandbox / "elsewhere").mkdir()
        cfg = sandbox / "cfg"
        cfg.symlink_to(sandbox / "elsewhere")
        assert H._prepare_runtime() is False

    def test_all_files_hardened_even_when_one_is_bad(self, H, sandbox):
        H._prepare_runtime()
        good = sandbox / "state" / "mic-health.json"
        good.write_text("[]")
        good.chmod(0o644)
        bad = sandbox / "state" / "handsoff.lock"
        bad.mkdir()                               # a dir where a file belongs
        assert H._secure_runtime_files() is False
        assert (good.stat().st_mode & 0o777) == 0o600   # still tightened


class TestRemoveStaleControlSocket:
    def test_missing_socket_is_a_noop(self, H, sandbox):
        H._remove_stale_control_socket()          # must not raise

    def test_removes_own_stale_socket(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        s.close()
        H._remove_stale_control_socket()
        assert not sp.exists()

    def test_refuses_symlink(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        real = sandbox / "real.sock"
        real.write_text("x")
        link = state / "control.sock"
        link.symlink_to(real)
        with pytest.raises(OSError, match="symlink"):
            H._remove_stale_control_socket()
        assert real.exists()                      # target untouched

    def test_refuses_regular_file(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "control.sock"
        f.write_text("this is not a socket")
        with pytest.raises(OSError, match="not a socket"):
            H._remove_stale_control_socket()
        assert f.exists()                         # contents preserved

    def test_refuses_foreign_owned_socket(self, H, monkeypatch, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        s.close()
        monkeypatch.setattr(H.os, "getuid", lambda: sp.stat().st_uid + 1)
        with pytest.raises(OSError, match="uid"):
            H._remove_stale_control_socket()
        assert sp.exists()


class TestRuntimePrepareStartupIntegration:
    """The fail-closed paths a user actually sees."""

    def test_doctor_reports_socket_problem(self, H, sandbox, monkeypatch):
        """Before E9, a refusing socket path failed silently at startup."""
        state = sandbox / "state"
        state.mkdir()
        link = state / "control.sock"
        link.symlink_to(sandbox / "evil")
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no niri in tests"))))
        text = H.run_doctor()
        assert "REFUSES STARTUP" in text and "symlink" in text

    def test_doctor_reports_missing_socket_calmly(self, H, sandbox, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no niri in tests"))))
        assert "not created yet" in H.run_doctor()

    def test_serve_refuses_when_prepare_fails(self, H, sandbox, monkeypatch):
        """ControlServer._serve must bail out cleanly, not bind an insecure
        socket, when _prepare_runtime refuses the runtime."""
        (sandbox / "cfg").symlink_to(sandbox / "elsewhere")
        # Built through the real constructor: the accept slot now lives in the
        # shared registry, so a __new__-built server has no slot to start into
        # and would fail for the wrong reason. _serve uses no assistant.
        srv = H.ControlServer(None)
        srv._serve()                              # must not raise
        assert srv._server is None
        assert not (sandbox / "state" / "control.sock").exists()

    def test_runtime_backup_is_always_owner_only(self, H, sandbox):
        """A .bak holds the same secrets as its source, so it must be 0600.

        `shutil.copy2` preserves the SOURCE's mode. A runtime file that was
        still 0644 when it was backed up therefore left a world-readable copy
        of the same transcript — measurable on this machine, where several
        history.json.bak-* sidecars are 0644.
        """
        cfg = sandbox / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        src = cfg / "history.json"
        src.write_text("[]", encoding="utf-8")
        os.chmod(src, 0o644)                      # a loose legacy file
        H._backup_runtime_json(src)
        bak = Path(str(src) + ".bak")
        assert bak.exists()
        assert (bak.stat().st_mode & 0o777) == 0o600, oct(
            bak.stat().st_mode & 0o777)

    def test_secure_runtime_files_sweeps_loose_backups(self, H, sandbox):
        """Existing sidecars are re-hardened on startup, not just new ones.

        Backups written before the source was tightened stay loose forever
        otherwise; the files this was found on were real 0644 transcripts.
        """
        cfg = sandbox / "cfg"
        state = sandbox / "state"
        cfg.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        loose = [cfg / "history.json.bak-modelswitch",
                 cfg / "settings.json.bak-threshold",
                 state / "reminders.json.bak"]
        for p in loose:
            p.write_text("[]", encoding="utf-8")
            os.chmod(p, 0o644)
        assert H._secure_runtime_files() is True
        for p in loose:
            assert (p.stat().st_mode & 0o777) == 0o600, p

    def test_secure_runtime_files_leaves_no_symlinked_backup_alone(
            self, H, sandbox):
        """The sweep must not follow a symlink out of the config dir."""
        cfg = sandbox / "cfg"
        cfg.mkdir(parents=True, exist_ok=True)
        outside = sandbox / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        os.chmod(outside, 0o644)
        (cfg / "history.json.bak-evil").symlink_to(outside)
        H._secure_runtime_files()
        assert (outside.stat().st_mode & 0o777) == 0o644, "followed the symlink"
        assert outside.read_text(encoding="utf-8") == "keep"

    def test_lock_file_created_private(self, H, sandbox):
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fh = None
        for _attempt in range(H.LOCK_RETRIES + 1):
            try:
                fh = open(H.LOCK_FILE, "w")
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                continue
            os.chmod(H.LOCK_FILE, 0o600)
            fh.write("123")
            fh.flush()
            break
        assert (H.LOCK_FILE.stat().st_mode & 0o777) == 0o600
        fh.close()


class TestMissingAudioFallback:
    """`handsoff.py` guards `from core import audio` with an inline fallback so a
    pre-Phase-4a bundle (no core/audio.py) still boots. Nothing pinned it: this
    imports the real module in a child process with core.audio made
    unimportable, under a throwaway HOME so no real config or log is touched.
    """

    DRIVER = textwrap.dedent(
        """
        import builtins, importlib.util, json, os, sys
        real_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            # exactly the import the compatibility branch wraps; nothing else in
            # core/ imports core.audio, so this is the real partial-install case
            if name == "core" and fromlist and "audio" in fromlist:
                raise ImportError("simulated partial install: no core/audio.py")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = _blocked
        spec = importlib.util.spec_from_file_location(
            "handsoff_no_audio", os.path.join(sys.argv[1], "handsoff.py"))
        mod = importlib.util.module_from_spec(spec)
        # Name it BEFORE executing it: the app refuses to run unregistered,
        # because an unnamed load cannot be told apart from a second copy. A
        # local name is right here — this child wants ONE app, not the
        # deployed identity — and the canonical name is claimed by the app
        # itself as it loads.
        sys.modules["handsoff_no_audio"] = mod
        spec.loader.exec_module(mod)
        audio = mod._audio
        report = {
            "class": type(audio).__name__,
            "device_choice": list(audio._whisper_device_choice()),
            "cuda_error": bool(audio._is_cuda_error("cuda")),
            "free_vram": audio._nvidia_free_vram_mb(),
            "whisper_model": audio._whisper_model,
            "tts_model": audio._tts_model,
            "tts_reference": audio.TTS_REFERENCE,
            "tts_engine": audio.TTS_ENGINE,
            "tts_device": audio._tts_device,
            "configure_returns_none": audio.configure() is None,
            "portaudio_busy": audio.portaudio_busy(),
            "aliases": {
                # The app reaches these through its `core.audio` handle rather
                # than re-exporting them, so read them where it does.
                "resample": callable(audio._resample_to_16k),
                "mic_lock": hasattr(audio.MIC_OPERATION_LOCK, "acquire"),
            },
        }
        for probe in ("transcribe", "get_whisper", "play_wav", "get_tts",
                      "reference_problem"):
            try:
                getattr(audio, probe)()
            except ImportError:
                report[probe] = "ImportError"
            else:
                report[probe] = "no-error"
        try:
            audio.Recorder()
        except ImportError:
            report["Recorder"] = "ImportError"
        else:
            report["Recorder"] = "no-error"
        # the listener's recovery path calls this from inside an except block, so
        # it must be a usable no-op rather than an AttributeError
        with audio.portaudio_in_use():
            pass
        report["portaudio_ctx"] = True
        print(json.dumps(report))
        """
    )

    def test_partial_install_imports_and_fails_loudly(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        # sandbox_env: the driver loads the monolith, and a redirected HOME
        # hides user site-packages (sounddevice + PySide6 live there) — this
        # test is about the missing core/audio.py, not about missing deps.
        proc = run_driver(
            ["-c", self.DRIVER, str(HERE)], home=home,
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["class"] == "_MissingAudio"
        # the defensive stubs other code reads must keep their documented values
        assert report["device_choice"] == ["cpu", "int8"]
        assert report["cuda_error"] is False
        assert report["free_vram"] is None
        assert report["whisper_model"] is None
        assert report["tts_model"] is None
        assert report["tts_reference"] == ""
        assert report["tts_engine"] == "chatterbox-turbo"
        # mic_health() reports the loading device: without the stub a partial
        # install raises AttributeError out of the `health` command instead of
        # answering, which is the whole reason every path exists there.
        assert report["tts_device"] == ""
        # configure() is called at import time: a partial install must get here
        assert report["configure_returns_none"] is True
        # nothing is open in a partial install, so the teardown guard is inert
        assert report["portaudio_busy"] is False
        assert report["portaudio_ctx"] is True
        assert report["aliases"] == {"resample": True, "mic_lock": True}
        # and the audio entry points must fail loudly, never return junk
        for probe in ("transcribe", "get_whisper", "play_wav", "Recorder"):
            assert report[probe] == "ImportError", probe


class TestMissingBrainFallback:
    """The other compatibility branch, and the one nothing could see.

    `handsoff.py` keeps a whole `_LegacyBrain` for a bundle whose
    `core/brain.py` is absent (a pre-extraction install). Its class body read
    `_brain._read_http_error` — and `_brain` is exactly the name that branch
    does NOT have, because having it is what skips the branch — so the fallback
    raised NameError while being BUILT: the app could not start at all on the
    bundle the class exists for, and no test could notice, since the class only
    exists on a bundle the suite never loads.

    So this boots the real module with core/brain.py unbuildable, the way
    `TestMissingAudioFallback` does for core/audio.py, and drives the class
    rather than merely touching it.
    """

    DRIVER = textwrap.dedent(
        """
        import importlib.util, io, json, logging, os, queue, sys, urllib.error
        _real_spec = importlib.util.spec_from_file_location

        def _unbuildable(name, *args, **kwargs):
            # core.load_module builds every candidate's spec this way, so None
            # for "brain" is what a bundle without the file looks like from the
            # loader's side: every candidate is skipped and it raises
            # ImportError, exactly as it does when the file is not there.
            if name == "brain":
                return None
            return _real_spec(name, *args, **kwargs)

        importlib.util.spec_from_file_location = _unbuildable
        spec = importlib.util.spec_from_file_location(
            "handsoff_no_brain", os.path.join(sys.argv[1], "handsoff.py"))
        mod = importlib.util.module_from_spec(spec)
        # Named before executing: the app refuses to run unregistered, because
        # an unnamed load cannot be told apart from a second copy.
        sys.modules["handsoff_no_brain"] = mod
        spec.loader.exec_module(mod)
        from core import brain as real_brain

        def http_error(body, code=400, reason="Bad Request"):
            return urllib.error.HTTPError("http://127.0.0.1:11434/api/chat",
                                          code, reason, None, io.BytesIO(body))

        class _Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return io.BytesIO(self.payload)

            def __exit__(self, *exc):
                return False

        calls, state = [], {}

        def urlopen(request, timeout=None):
            calls.append(json.loads(request.data.decode()))
            if len(calls) == 1:
                raise http_error(json.dumps(
                    {"error": "this model does not support tools"}).encode())
            return _Response(json.dumps({"message": {"content": "ok"}}).encode())

        legacy = mod._brain
        report = {
            "name": getattr(legacy, "__name__", type(legacy).__name__),
            "turn_stream": legacy.TurnStream(1, None, None).__class__.__name__,
            # the MODULE-level implementations, not copies of them: the whole
            # reason the filters moved out here is that a fallback drifting from
            # the real one is how a legitimate "<3" reply stopped being spoken
            "shared_filters": (
                legacy.strip_thinking is mod._fallback_strip_thinking
                and legacy.is_leaked_markup is mod._fallback_is_leaked_markup),
            # identity, not behaviour: a second COPY of the reader inside the
            # class is exactly what drifted before, and it would pass every
            # behavioural check here while being the thing that was wrong.
            "shared_reader": legacy._read_http_error is mod._fallback_read_http_error,
            "reader_json": legacy._read_http_error(
                http_error(json.dumps({"error": "no such model"}).encode())),
            "reader_not_json": legacy._read_http_error(
                http_error(b"<html>oops</html>", code=500,
                           reason="Server Error")),
            "reader_matches_core": all(
                legacy._read_http_error(http_error(body))
                == real_brain._read_http_error(http_error(body))
                for body in (json.dumps({"error": "x"}).encode(), b"",
                             b"not json at all")),
        }
        report["chat"] = legacy.ollama_chat(
            [{"role": "user", "content": "hi"}], [{"type": "function"}],
            base="http://127.0.0.1:11434", model="m", num_ctx=8,
            guard=lambda: None, logger=logging.getLogger("driver"),
            state=state, urlopen=urlopen)
        report["call_tools"] = ["tools" in call for call in calls]
        report["state"] = state

        # ---- the STREAMING arm, which is the DEFAULT turn path -------------
        # A whole second copy of `core.brain.ollama_chat_stream`, and a third
        # instance of the defect class this branch has already been fixed for
        # twice (the shared filters, the shared error reader). Measured
        # 2026-09-27 by driving it: a 404 naming the model reached the user
        # with no `ollama pull` in it while its own non-streaming sibling had
        # the command, and a refused connection escaped as URLError so the
        # turn said "URLError: <urlopen error [Errno 111] ...>" instead of the
        # server and `systemctl start ollama`.
        # `chr(10)` rather than a backslash escape: this driver is a string
        # inside a string, so a newline written the obvious way arrives in
        # the child as two characters. And no docstrings below either --
        # a triple quote in here closes the string holding the driver.
        EOL = chr(10)

        def ndjson(*pieces):
            return [(json.dumps({"message": {"content": p}}) + EOL).encode()
                    for p in pieces]

        def leaked(token):
            # one NDJSON line: a control token, then a newline of its own
            return (json.dumps({"message": {"content": token + EOL}})
                    + EOL).encode("utf-8")

        def answered(text):
            return (json.dumps({"message": {"content": text}}) + EOL).encode()

        class StreamResp:
            def __init__(self, lines):
                self.lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return iter(self.lines)

            def read(self):
                return b"".join(self.lines)

        BASE = "http://127.0.0.1:11434"
        log = logging.getLogger("driver")

        def speaking(lines):
            q = queue.Queue()
            legacy.ollama_chat_stream(
                [{"role": "user", "content": "hi"}], q, None, None,
                base=BASE, model="gpt-oss:20b", num_ctx=8,
                guard=lambda: None, logger=log,
                urlopen=lambda req, timeout=None: StreamResp(list(lines)))
            return list(q.queue)

        report["stream_said"] = speaking(ndjson("One. ", "Two more"))

        # a stream that dies right after a finished sentence (which the splitter
        # is still holding for its lookahead) still says it, in this copy as in
        # core; half a sentence stays unsaid
        class DyingResp(StreamResp):
            def __iter__(self):
                yield from self.lines
                raise OSError("connection reset")

        def dying(*pieces):
            q = queue.Queue()
            try:
                legacy.ollama_chat_stream(
                    [{"role": "user", "content": "hi"}], q, None, None,
                    base=BASE, model="gpt-oss:20b", num_ctx=8,
                    guard=lambda: None, logger=log,
                    urlopen=lambda req, timeout=None: DyingResp(ndjson(*pieces)))
            except OSError:
                pass
            return list(q.queue)

        report["stream_dies_after_a_sentence"] = dying("Berlin is in Germany", ".")
        report["stream_dies_mid_sentence"] = dying("Berlin is in Germany. It has", " three")

        # where a reply is cut into sentences must not depend on where the
        # network cut the stream: the same reply, split at EVERY position, in
        # this copy and in core, gives the sentences the reply gives whole
        def split_at(brain, text, i):
            q = queue.Queue()
            brain.ollama_chat_stream(
                [{"role": "user", "content": "hi"}], q, None, None,
                base=BASE, model="gpt-oss:20b", num_ctx=8,
                guard=lambda: None, logger=log,
                urlopen=lambda req, timeout=None: StreamResp(
                    ndjson(text[:i], text[i:])))
            return list(q.queue)

        REPLIES = ("It is 18.5 degrees and 3.2 inches of rain.",
                   "Version 3.12.1 shipped. Visit example.com now.",
                   "Really?! Yes... maybe. Okay!")
        report["split_whole"] = [split_at(legacy, r, len(r)) for r in REPLIES]
        report["split_independent"] = all(
            split_at(legacy, r, i) == split_at(legacy, r, len(r))
            for r in REPLIES for i in range(1, len(r)))
        report["split_matches_core"] = all(
            split_at(legacy, r, i) == split_at(real_brain, r, i)
            for r in REPLIES for i in range(1, len(r)))
        # the leaked-token rule, both directions: a token glued to a
        # sentence costs only its own line, a bare one costs the line
        report["stream_leaked"] = speaking(
            [leaked("<|im_start|>assistant"), answered("Here.")])
        report["stream_bare_token"] = speaking(
            [leaked("<|im_start|>"), answered("Here.")])
        # and the two failure sentences, against the REAL functions
        def failing(kind):
            def urlopen(req, timeout=None):
                if kind == "refused":
                    raise urllib.error.URLError(
                        ConnectionRefusedError(111, "Connection refused"))
                raise http_error(json.dumps(
                    {"error": 'model "gpt-oss:20b" not found'}).encode(),
                    code=404, reason="Not Found")
            q = queue.Queue()
            try:
                legacy.ollama_chat_stream(
                    [{"role": "user", "content": "hi"}], q, None, None,
                    base=BASE, model="gpt-oss:20b", num_ctx=8,
                    guard=lambda: None, logger=log, urlopen=urlopen)
            except Exception as exc:
                return f"{type(exc).__name__}: {exc}"
            return "NO RAISE"

        report["stream_refused"] = failing("refused")
        report["stream_missing_model"] = failing("404")

        # the readiness probe: a whole second copy of `core.brain`'s, and until
        # 2026-09-27 nothing in the suite held it. Found by
        # `tests/test_rule_copies.py`'s branch census, which asks every copy in
        # a compatibility branch to name the test that holds it — and this one
        # had none. It is a copy because it must be: the module it would
        # delegate to is the one that failed to load.
        def probe(urlopen):
            return legacy.ollama_available(base=BASE, guard=lambda: None,
                                          urlopen=urlopen)

        def tags(payload):
            def urlopen(url, timeout=None):
                if isinstance(payload, BaseException):
                    raise payload
                return StreamResp([payload])
            return urlopen

        report["probe_up"] = probe(tags(json.dumps({"models": []}).encode()))
        report["probe_html"] = probe(tags(b"<html>a captive portal</html>"))
        report["probe_down"] = probe(tags(
            urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
        core_probe = real_brain.ollama_available
        report["core_probe"] = [
            core_probe(base=BASE, guard=lambda: None, urlopen=urlopen)
            for urlopen in (tags(json.dumps({"models": []}).encode()),
                            tags(b"<html>a captive portal</html>"),
                            tags(urllib.error.URLError(
                                ConnectionRefusedError(111, "refused"))))]

        # the SAME two failures through the real `core.brain`, so the report
        # carries both sentences and the test compares them
        def core_arm(kind):
            def urlopen(req, timeout=None):
                if kind == "refused":
                    raise urllib.error.URLError(
                        ConnectionRefusedError(111, "Connection refused"))
                raise http_error(json.dumps(
                    {"error": 'model "gpt-oss:20b" not found'}).encode(),
                    code=404, reason="Not Found")
            call = (real_brain.ollama_chat_stream
                    if kind == "refused" else real_brain.ollama_chat)
            args = ([{"role": "user", "content": "hi"}], queue.Queue())
            try:
                if kind == "refused":
                    call(*args, None, None, base=BASE, model="gpt-oss:20b",
                         num_ctx=8, guard=lambda: None, logger=log,
                         urlopen=urlopen)
                else:
                    call(args[0], None, base=BASE, model="gpt-oss:20b",
                         num_ctx=8, guard=lambda: None, logger=log,
                         urlopen=urlopen)
            except Exception as exc:
                return f"{type(exc).__name__}: {exc}"
            return "NO RAISE"

        report["core_refused"] = core_arm("refused")
        report["core_missing_model"] = core_arm("404")

        # ---- the system-first fold, in BOTH arms ------------------------
        # A fourth copy of a rule, and the one nothing held: measured
        # 2026-09-27 by driving this class with a conversation carrying a
        # per-turn note as a system message, the shape the rule exists for.
        # Both arms put it on the wire AFTER the first
        # (['system', 'user', 'system', 'assistant', 'user']), where Ollama
        # answers the whole request with HTTP 500 -- so a turn was lost on
        # exactly the bundles this class is here to keep working, and silently
        # worked everywhere `core/brain.py` loads.
        TAIL = [{"role": "system", "content": "you are a voice assistant"},
                {"role": "user", "content": "what is the weather"},
                {"role": "system", "content": "the user is in Ghent"},
                {"role": "assistant", "content": "it is raining"},
                {"role": "user", "content": "and tomorrow?"}]
        # a tail system message with NO CONTENT: still a system message in a
        # position the renderer refuses, and core's copy drops it.
        EMPTY = [{"role": "system", "content": "you are a voice assistant"},
                 {"role": "user", "content": "what is the weather"},
                 {"role": "system", "content": ""}]
        # TWO leading system messages, so `head[1:]` is non-empty. Without
        # this shape the first `parts = [...]` line is dead: measured as a
        # surviving mutation, because a corpus with one leading system message
        # leaves that list empty however it is written.
        TWO_HEADS = [{"role": "system", "content": "you are a voice assistant"},
                     {"role": "system", "content": "answer in one sentence"},
                     {"role": "user", "content": "what is the weather"},
                     {"role": "system", "content": "the user is in Ghent"}]
        # A LEADING system message with no content AND a tail one with no
        # content, which is the only input the empty-tail drop actually
        # decides. Both halves are needed and both were arrived at by the
        # corpus being wrong first: a lone empty leading system is answered by
        # the early return (there is no tail system, so nothing is out of
        # order) and a non-empty leading system is answered identically by the
        # drop and by the merge, which is why two earlier corpora reported the
        # drop as an equivalent mutant. Here the merge path would hand back
        # [{"role": "system", "content": ""}] — legal for Ollama, and a system
        # prompt with nothing in it. This is the shape the original core
        # defect had: an empty re-injected note.
        EMPTY_HEAD = [{"role": "system", "content": ""},
                      {"role": "user", "content": "what is the weather"},
                      {"role": "system", "content": ""}]
        SHAPES = (TAIL, EMPTY, TWO_HEADS, EMPTY_HEAD)

        captured = []

        def wire(messages, stream):
            sent2 = []

            def rec(req, timeout=None):
                captured.append(1)
                sent2.append(json.loads(req.data.decode()))
                return StreamResp([(json.dumps(
                    {"message": {"content": "ok"}}) + EOL).encode()])
            if stream:
                legacy.ollama_chat_stream(
                    messages, queue.Queue(), None, None, base=BASE, model="m",
                    num_ctx=8, guard=lambda: None, logger=log, urlopen=rec)
            else:
                legacy.ollama_chat(
                    messages, None, base=BASE, model="m", num_ctx=8,
                    guard=lambda: None, logger=log, urlopen=rec)
            return sent2[0]

        report["fold_wire"] = [wire(m, st) for m in SHAPES
                               for st in (False, True)]
        # How many requests were ACTUALLY opened. This is the only evidence
        # here that a fold function cannot forge: the payload's `stream` and
        # `model` can be written by hand, and the folded list can be computed
        # without sending anything, so a driver that never touched the wire
        # satisfied every other assertion — measured twice, as two different
        # forgeries of the same hole. A request was opened or it was not.
        report["wire_calls"] = len(captured)
        # the SAME lists through core's own fold, so the report carries both
        # and the test compares rather than re-deriving the expectation
        report["fold_core"] = [real_brain._messages_system_first(m, log)
                               for m in SHAPES]
        # The copy called DIRECTLY, as a third source. Without it the two above
        # can be the same function and the equality below is a tautology:
        # measured as a surviving mutation, where recording core's answer as
        # what went on the wire left every assertion passing. Tying the wire to
        # the copy that produced it, and the copy to core, is what makes the
        # three distinct.
        report["fold_legacy"] = [legacy._messages_system_first(m, log)
                                 for m in SHAPES]
        print(json.dumps(report))
        """
    )

    def test_the_bundle_boots_and_its_reader_behaves_like_core(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        proc = run_driver(
            ["-c", self.DRIVER, str(HERE)], home=home,
            capture_output=True, text=True, timeout=180,
        )
        # a NameError while BUILDING the class is what this catches: the app
        # never reaches its first turn, so the exit code is the assertion that
        # matters most here.
        assert proc.returncode == 0, proc.stderr[-3000:]
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["name"] == "_LegacyBrain"
        assert report["turn_stream"] == "_LegacyTurnStream"
        assert report["shared_filters"] is True
        assert report["shared_reader"] is True
        # the reader: the JSON body's `error`, and the reason when it is not JSON
        assert report["reader_json"] == "no such model"
        assert report["reader_not_json"] == "Server Error"
        assert report["reader_matches_core"] is True
        # and the class's own chat path really works through it — a 400 naming
        # tools retries without them, which is the behaviour the branch exists
        # to preserve, not just the function it calls
        assert report["chat"] == {"content": "ok"}
        assert report["call_tools"] == [True, False]
        assert report["state"] == {"tools_supported": False}

    def test_its_arms_fold_a_tail_system_message_exactly_as_core_does(
            self, tmp_path):
        """The copy and the original, on what the SERVER receives.

        Ollama refuses the whole request with HTTP 500 when a system message
        follows the first, and a caller that appends a per-turn note as one
        loses every turn. `core.brain._messages_system_first` is the one place
        every request passes through to enforce that, and until now the
        fallback had none: measured 2026-09-27, BOTH arms of this class put
        the note on the wire in third position, so a turn died on exactly the
        bundles this class exists to keep working and worked everywhere else.

        Both arms, both shapes, compared against core's own fold on the same
        inputs — because a copy that has quietly stopped matching is the whole
        defect class, and reading the two and seeing them agree is not evidence.
        """
        home = tmp_path / "home"
        home.mkdir()
        proc = run_driver(
            ["-c", self.DRIVER, str(HERE)], home=home,
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        wire, core = report["fold_wire"], report["fold_core"]
        copied = report["fold_legacy"]
        assert len(wire) == 8 and len(core) == 4 and len(copied) == 4, (
            f"the driver drove {len(wire)} wire cases, {len(core)} core and "
            f"{len(copied)} copy ones; the two arms and the four shapes are "
            "the claim")

        def misordered(messages):
            # Ollama's rule, stated rather than trusted: at most the FIRST
            # message may be a system one.
            return [m["role"] for m in messages][1:].count("system") > 0

        # The records are PAYLOADS, not message lists, and that is the check
        # that a fold function cannot forge: `stream` says which arm ran, so
        # these four flags prove both arms were driven and that what the test
        # reads came off a request. Measured as a surviving mutation without
        # it — a driver that recorded core's answer in place of the wire
        # satisfied every other assertion here, because when the two copies
        # agree all three sources agree and the equality is a tautology.
        assert [p["stream"] for p in wire] == [False, True] * 4, (
            f"the wire records do not alternate between the two arms: "
            f"{[p.get('stream') for p in wire]}")
        assert all(p.get("model") == "m" for p in wire), wire
        assert report["wire_calls"] == 8, (
            f"the fallback opened {report['wire_calls']} request(s) where 8 "
            "were driven, so the wire records were not read off a request")
        sent_lists = [p["messages"] for p in wire]

        assert not any(map(misordered, sent_lists)), (
            f"a fallback arm put a system message after the first: {sent_lists}")
        for index, sent in enumerate(sent_lists):
            shape = index // 2
            # THREE sources, not two. The wire is what the server was sent and
            # `copied` is the fallback's own fold called directly; either one
            # on its own leaves the equality below satisfiable by a driver that
            # computed both sides from the same function, which is a real hole
            # and was measured as one.
            assert sent == copied[shape], (
                f"arm {index % 2} sent a different list from the copy that "
                f"produced it.\n  wire: {sent}\n  copy: {copied[shape]}")
            assert copied[shape] == core[shape], (
                f"the fallback's fold and core's disagree.\n"
                f"  fallback: {copied[shape]}\n  core:     {core[shape]}")

        # And the shapes, so the equality above is not two empty lists.
        # Expected against core's OWN answer rather than a list written here,
        # because a second hand-written expectation is a second thing to drift.
        assert [m["role"] for m in core[0]] == ["system", "user", "assistant",
                                                "user"], core[0]
        assert "the user is in Ghent" in core[0][-1]["content"], (
            "the note was dropped rather than folded into the user turn: "
            f"{core[0]}")
        # two leading system messages MERGE into one, in order
        assert len(core[2]) == 2 and core[2][0]["role"] == "system", core[2]
        assert "you are a voice assistant" in core[2][0]["content"], core[2]
        assert "answer in one sentence" in core[2][0]["content"], core[2]
        assert "the user is in Ghent" in core[2][-1]["content"], core[2]
        # and a LEADING empty system message is dropped rather than kept
        assert core[3] == [{"role": "user", "content": "what is the weather"}], (
            f"an empty leading system message was kept: {core[3]}")

    def test_its_streaming_arm_says_the_same_things_core_does(self, tmp_path):
        """The copy and the original, on the sentences the user HEARS.

        A second copy of a rule cannot be left to drift: this branch was fixed
        twice already for exactly that, and both fixes were things the
        non-streaming sibling in the same class had and the streaming one did
        not. Driven here rather than compared by reading, because the whole
        point is that reading the two and seeing them agree is not evidence.
        """
        home = tmp_path / "home"
        home.mkdir()
        proc = run_driver(
            ["-c", self.DRIVER, str(HERE)], home=home,
            capture_output=True, text=True, timeout=180,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        # the queue: sentences, then exactly one terminator
        assert report["stream_said"] == ["One.", "Two more", None], \
            report["stream_said"]
        # the splitter's boundaries do not depend on where the stream was cut:
        # a decimal, a version, a domain and "?!" / "..." stay whole in this
        # copy exactly as in core, at every cut position
        assert report["split_whole"] == [
            ["It is 18.5 degrees and 3.2 inches of rain.", None],
            ["Version 3.12.1 shipped.", "Visit example.com now.", None],
            ["Really?!", "Yes...", "maybe.", "Okay!", None]], report["split_whole"]
        assert report["stream_dies_after_a_sentence"] == [
            "Berlin is in Germany.", None], report["stream_dies_after_a_sentence"]
        assert report["stream_dies_mid_sentence"] == [
            "Berlin is in Germany.", None], report["stream_dies_mid_sentence"]
        assert report["split_independent"] is True
        assert report["split_matches_core"] is True
        # the leak rule, in the direction that lost a sentence until 2026-09-27
        assert report["stream_leaked"] == ["Here.", None], report["stream_leaked"]
        assert report["stream_bare_token"] == ["Here.", None], \
            report["stream_bare_token"]
        # the two failure sentences, identical in both copies
        assert report["stream_refused"] == report["core_refused"], (
            report["stream_refused"], report["core_refused"])
        assert "systemctl start ollama" in report["stream_refused"], \
            report["stream_refused"]
        assert report["stream_missing_model"] == report["core_missing_model"], (
            report["stream_missing_model"], report["core_missing_model"])
        assert "run: ollama pull gpt-oss:20b" in report["stream_missing_model"], \
            report["stream_missing_model"]
        # and the readiness probe, whose second copy had no holder at all
        assert report["probe_up"] is True, report["probe_up"]
        assert report["probe_html"] is False, (
            "a 200 with an HTML body is a captive portal or a wrong port, not "
            "a brain: reading it as up gets the user a turn that fails on "
            "every utterance")
        assert report["probe_down"] is False, report["probe_down"]
        assert [report["probe_up"], report["probe_html"],
                report["probe_down"]] == report["core_probe"], (
            "the fallback's copy of the probe and core's answer differently "
            f"on the same three servers: {report['core_probe']}")


class TestSweepStaleScratch:
    """Scratch a killed run left behind in STATE_DIR is reclaimed at startup.

    The leak is real: every spoken reply synthesizes through a
    `TemporaryDirectory(dir=STATE_DIR)` (default prefix `tmp`) and several
    state writers go through a loose `*.tmp` sibling — a SIGKILL or a power
    cut between create and cleanup strands both, forever, because the process
    that owed the cleanup no longer exists. The sweep runs inside
    `_prepare_runtime()` with a grace window, because a sibling instance can
    still be mid-synthesis while this one starts.
    """

    NOW = 1_800_000_000.0

    @staticmethod
    def _age(path: Path, now: float, age_s: float = 10_000.0) -> None:
        """Stamp `path`'s OWN mtime `age_s` before `now` (default ~2.8 h,
        past the grace). Everything is stamped relative to the test's clock:
        real mtimes would read as arbitrarily old against a fictional now."""
        past = now - age_s
        os.utime(path, (past, past), follow_symlinks=False)

    def test_a_leftover_tmp_directory_from_a_killed_run_is_reclaimed(
            self, H, sandbox, monkeypatch):
        """The exact shape `_speak` leaves when the process dies mid-turn:
        a `TemporaryDirectory(dir=STATE_DIR)` that never got its cleanup."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        leaked = H.STATE_DIR / "tmpab12cd34"       # tempfile's default prefix
        leaked.mkdir()
        (leaked / "tts.wav").write_bytes(b"RIFF....")
        self._age(leaked, self.NOW)
        assert H._sweep_stale_scratch(self.NOW) == 1
        assert not leaked.exists()

    def test_a_loose_tmp_file_from_an_interrupted_write_is_reclaimed(
            self, H, sandbox, monkeypatch):
        """The second shape: state writers stage through an in-directory
        `*.tmp` file (`atomic_private_write`'s mkstemp, the laya corpus
        writer); a kill mid-write strands it beside the real file."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        leaked = H.STATE_DIR / ".reminders.json.a1b2c3.tmp"
        leaked.write_text("{\"partial\"", encoding="utf-8")
        self._age(leaked, self.NOW)
        assert H._sweep_stale_scratch(self.NOW) == 1
        assert not leaked.exists()

    def test_younger_than_the_grace_window_is_left_alone(
            self, H, sandbox, monkeypatch):
        """A sibling instance can be mid-synthesis while this one starts:
        fresh scratch belongs to the living and is never swept."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fresh_dir = H.STATE_DIR / "tmpfresh01"
        fresh_dir.mkdir()
        (fresh_dir / "tts.wav").write_bytes(b"RIFF")
        fresh_file = H.STATE_DIR / ".world-events-seen.x9y8z7.tmp"
        fresh_file.write_text("{}", encoding="utf-8")
        self._age(fresh_dir, self.NOW, age_s=10.0)    # 10 s old: inside grace
        self._age(fresh_file, self.NOW, age_s=10.0)
        assert H._sweep_stale_scratch(self.NOW) == 0
        assert fresh_dir.exists() and fresh_file.exists()

    def test_reclaimed_scratch_is_archived_not_destroyed(self, H, sandbox,
                                                        monkeypatch):
        """Reclaim is REVERSIBLE: the entry MOVES into a dated folder inside
        the state dir, owner-only, bytes intact — the shapes are matched by
        NAME, so the day that match is ever wrong the content is recoverable
        instead of gone."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        leaked = H.STATE_DIR / "tmpab12cd34"
        leaked.mkdir()
        (leaked / "tts.wav").write_bytes(b"RIFF....")
        loose = H.STATE_DIR / ".reminders.json.a1b2.tmp"
        loose.write_text("{\"partial\"", encoding="utf-8")
        self._age(leaked, self.NOW)
        self._age(loose, self.NOW)
        assert H._sweep_stale_scratch(self.NOW) == 2
        assert not leaked.exists() and not loose.exists()   # out of the way
        day = H.datetime.datetime.fromtimestamp(self.NOW).strftime("%Y-%m-%d")
        folder = H.STATE_DIR / H.SCRATCH_QUARANTINE_NAME / day
        assert (folder / "tmpab12cd34" / "tts.wav").read_bytes() == b"RIFF...."
        assert (folder / ".reminders.json.a1b2.tmp").read_text() == "{\"partial\""
        assert (folder.stat().st_mode & 0o777) == 0o700

    def test_the_archive_expires_on_its_own_ttl(self, H, sandbox, monkeypatch):
        """The archive's clock is its own: a dated folder survives its TTL and
        is gone after it, and entries in there that are not dated folders of
        ours (a stray file, someone else's folder) are never deleted."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        root = H.STATE_DIR / H.SCRATCH_QUARANTINE_NAME
        root.mkdir(parents=True)
        today = H.datetime.datetime.fromtimestamp(self.NOW).date()

        def dated(days_ago):
            folder = root / (today
                             - H.datetime.timedelta(days=days_ago)).isoformat()
            folder.mkdir()
            (folder / "tts.wav").write_bytes(b"RIFF")
            return folder

        keep_today = dated(0)
        keep_boundary = dated(H.SCRATCH_QUARANTINE_TTL_DAYS)
        gone = dated(H.SCRATCH_QUARANTINE_TTL_DAYS + 1)
        foreign_dir = root / "notes"                    # not a date
        foreign_dir.mkdir()
        foreign_file = root / "README"
        foreign_file.write_text("mine", encoding="utf-8")
        assert H._sweep_stale_scratch(self.NOW) == 0     # nothing to reclaim
        assert keep_today.is_dir() and keep_boundary.is_dir()
        assert not gone.exists()
        assert foreign_dir.is_dir() and foreign_file.exists()

    def test_a_name_collision_in_the_archive_keeps_both_copies(
            self, H, sandbox, monkeypatch):
        """Two runs can reclaim the same scratch name on the same day; the
        earlier copy must not be replaced by the later one — keeping both is
        the whole point of an archive."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        day = H.datetime.datetime.fromtimestamp(self.NOW).strftime("%Y-%m-%d")
        folder = H.STATE_DIR / H.SCRATCH_QUARANTINE_NAME / day
        (folder / "tmpab12cd34").mkdir(parents=True)
        (folder / "tmpab12cd34" / "tts.wav").write_bytes(b"FIRST")
        leaked = H.STATE_DIR / "tmpab12cd34"
        leaked.mkdir()
        (leaked / "tts.wav").write_bytes(b"SECOND")
        self._age(leaked, self.NOW)
        assert H._sweep_stale_scratch(self.NOW) == 1
        assert (folder / "tmpab12cd34" / "tts.wav").read_bytes() == b"FIRST"
        assert (folder / "tmpab12cd34~1" / "tts.wav").read_bytes() == b"SECOND"

    def test_a_symlinked_archive_is_refused_and_nothing_is_destroyed(
            self, H, sandbox, monkeypatch):
        """The runtime hardening rule, applied to the archive: a planted
        `scratch-quarantine` symlink must not become a way to carry reclaimed
        state out of the bubble — and when the archive cannot be made private
        the stale entry stays where it is rather than being deleted."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        outside = sandbox / "outside"
        outside.mkdir()
        (H.STATE_DIR / H.SCRATCH_QUARANTINE_NAME).symlink_to(outside)
        leaked = H.STATE_DIR / "tmpab12cd34"
        leaked.mkdir()
        self._age(leaked, self.NOW)
        assert H._sweep_stale_scratch(self.NOW) == 0
        assert leaked.is_dir()                           # left where it was
        assert list(outside.iterdir()) == []             # nothing carried out

    def test_a_clean_start_creates_no_archive(self, H, sandbox, monkeypatch):
        """No stale scratch, no new directory: a healthy state dir is left
        exactly as it was (the archive is made on first use, not at import)."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        assert H._sweep_stale_scratch(self.NOW) == 0
        assert not (H.STATE_DIR / H.SCRATCH_QUARANTINE_NAME).exists()

    def test_prepare_runtime_runs_the_sweep(self, H, sandbox, monkeypatch):
        """Reclamation is part of startup hardening, not an extra step a
        caller can forget — proven through `_prepare_runtime` itself, the
        path both `main()` and the control-socket server take."""
        monkeypatch.setattr(H.time, "time", lambda: self.NOW)
        leaked = H.STATE_DIR / "tmpdeadbeef"
        leaked.mkdir(parents=True)
        (leaked / "tts.wav").write_bytes(b"RIFF")
        self._age(leaked, H.time.time())   # genuinely old on the REAL clock
        assert H._prepare_runtime() is True
        assert not leaked.exists()

    def test_ordinary_state_entries_are_never_swept(
            self, H, sandbox, monkeypatch):
        """The sweep names exactly two shapes; everything a live runtime owns
        survives it — old files of other shapes, backups, and a directory
        whose name merely ENDS with .tmp (the suffix is the FILE shape, the
        prefix the directory shape, never crossed)."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        keepers = [H.STATE_DIR / "decisions.jsonl",
                   H.STATE_DIR / "handsoff.lock",
                   H.STATE_DIR / "laya-turns.jsonl",
                   H.STATE_DIR / "reminders.json.bak",   # backup, not scratch
                   H.STATE_DIR / "not-scratch.tmp",      # a DIRECTORY
                   H.STATE_DIR / "tmp-notes.txt"]        # a FILE with the prefix
        for k in keepers[:-2]:
            k.write_text("{}", encoding="utf-8")
        keepers[-2].mkdir()
        keepers[-1].write_text("notes", encoding="utf-8")
        for k in keepers:
            self._age(k, self.NOW)
        assert H._sweep_stale_scratch(self.NOW) == 0
        for k in keepers:
            assert k.exists(), k

    def test_a_symlink_is_never_followed_out_of_the_state_dir(
            self, H, sandbox, monkeypatch):
        """Same contract as the backup sweep: a stale-looking scratch name
        that is a symlink is skipped, and the target outside the state dir
        keeps its bytes."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        outside = sandbox / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        link = H.STATE_DIR / "tmpattack.tmp"
        link.symlink_to(outside)
        self._age(link, self.NOW)   # the LINK's own mtime, not the target's
        assert H._sweep_stale_scratch(self.NOW) == 0
        assert link.is_symlink()
        assert outside.read_text(encoding="utf-8") == "keep"

    def test_an_unreadable_state_dir_is_a_warning_not_a_crash(
            self, H, sandbox, monkeypatch, caplog):
        """The bubble must start with a dirty state dir, not refuse to:
        enumeration failing costs a warning and zero removals. The
        warn-once flags are process-globals, so they are pinned EMPTY here —
        a neighbour that already tripped the warning must not rewrite this
        test's answer (the same order-sensitivity the spotter-state pins
        cover elsewhere)."""
        monkeypatch.setattr(H, "_SWEEP_WARNED", set())
        monkeypatch.setattr(H, "_SWEPT_DIRS", set())
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "no-such-state")
        with caplog.at_level("WARNING", logger="handsoff"):
            assert H._sweep_stale_scratch() == 0
        assert any("could not enumerate" in r.message for r in caplog.records)

    def test_the_sweep_is_wired_into_prepare_runtime(self, H):
        """A wiring pin: without this, a refactor of the startup sequence can
        drop the call and every behavioural guard above still passes against
        a function nobody calls."""
        import inspect
        source = inspect.getsource(H._prepare_runtime)
        assert "_sweep_stale_scratch()" in source

    def test_a_second_sweep_cannot_erase_the_first_ones_reclaim(
            self, H, sandbox, monkeypatch):
        """One start, one sweep — and one recorded result.

        `_prepare_runtime()` runs twice in a service process (`main()`, then
        `ControlServer._serve()`); the second pass is a no-op, but it used to
        re-stamp `_LAST_SWEEP` with `removed=0` and erase what the start had
        just reclaimed, so the doctor read "last start swept nothing" seconds
        after a start that had swept (found live on this desk 2026-09-25).
        """
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": None, "removed": 0,
                                                "unreadable": False})
        monkeypatch.setattr(H, "_SWEPT_DIRS", set())
        monkeypatch.setattr(H, "_SWEEP_WARNED", set())
        monkeypatch.setattr(H.time, "time", lambda: self.NOW)
        leaked = H.STATE_DIR / "tmpdeadbeef"
        leaked.mkdir(parents=True)
        (leaked / "tts.wav").write_bytes(b"RIFF")
        self._age(leaked, self.NOW)
        assert H._prepare_runtime() is True             # the start's sweep
        assert H._LAST_SWEEP["removed"] == 1
        # the control server's second pass: the record must survive it
        assert H._sweep_stale_scratch() == 0
        assert H._LAST_SWEEP["removed"] == 1
        assert not leaked.exists()
        assert "last start swept 1 entry" in H._state_hygiene_line()

    def test_the_summary_is_logged_once_the_journal_exists(
            self, H, sandbox, monkeypatch, caplog):
        """The reclaim has to be VISIBLE in production.

        The sweep runs before `setup_logging()`, and an INFO record on a
        logger with no handlers is dropped (`lastResort` is WARNING-only), so
        logging the summary where the work happens loses it. `main()` says the
        line right after logging is up, reading the same `_LAST_SWEEP` the
        doctor reads — one dict, so the journal and the doctor cannot
        disagree. A clean start says nothing.
        """
        import inspect
        order = inspect.getsource(H.main)
        assert (order.index("_log_swept_scratch()")
                > order.index("setup_logging()")), "emitted before the journal"
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": H.time.time(),
                                                "removed": 2,
                                                "unreadable": False})
        with caplog.at_level("INFO", logger="handsoff"):
            H._log_swept_scratch()
        assert any("swept 2 stale scratch entries" in r.message
                   and "recoverable for" in r.message
                   for r in caplog.records), caplog.text
        caplog.clear()
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": H.time.time(),
                                                "removed": 0,
                                                "unreadable": False})
        with caplog.at_level("INFO", logger="handsoff"):
            H._log_swept_scratch()
        assert not [r for r in caplog.records if "swept" in r.message]


class TestStateHygieneReading:
    """`_state_hygiene` / `_state_hygiene_line`: the doctor's state-dir
    hygiene reading. The collector and the sweep share ONE shape predicate
    (`_scratch_shaped`), so a diagnostic can never disagree with the sweep
    about what a leak is — and the line is the host's own rendering of the
    same dict the JSON surface ships, so words and numbers cannot drift."""

    def test_the_collector_and_the_sweep_share_one_shape_predicate(
            self, H, sandbox):
        """A tmp* dir and a *.tmp file count as scratch; a tmp-prefixed FILE,
        a .tmp-suffixed DIRECTORY and ordinary state entries do not — the
        same two shapes the sweep removes, minus the age gate."""
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        (state / "tmpab12cd34").mkdir()
        (state / ".reminders.json.a1b2.tmp").write_text("x")
        (state / "tmp-notes.txt").write_text("x")       # tmp-prefixed FILE
        (state / "not-scratch.tmp").mkdir()             # .tmp-suffixed DIR
        (state / "decisions.jsonl").write_text("{}")
        info = H._state_hygiene()
        assert info["readable"] is True
        assert info["scratch_left"] == 2
        assert info["entries"] == 5
        assert info["size_bytes"] > 0

    def test_a_young_scratch_entry_still_shows_in_the_reading(
            self, H, sandbox):
        """The reading has NO age gate, on purpose: a diagnostic reports what
        IS (a live sibling's in-flight scratch shows up), while the sweep
        with its grace window removes only what is old. That difference is
        the point of the line — "present, swept next start"."""
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        (state / "tmpfresh01").mkdir()
        info = H._state_hygiene()
        assert info["scratch_left"] == 1
        line = H._state_hygiene_line()
        assert "in grace or from a live sibling" in line

    def test_the_line_reports_the_last_sweep_honestly(self, H, sandbox,
                                                      monkeypatch):
        """No sweep yet reads as `not run this process` (never a false
        "nothing was leaking"); a run that removed entries reads what and
        when; a start that found nothing reads `nothing`."""
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": None, "removed": 0,
                                                "unreadable": False})
        assert "sweep not run this process" in H._state_hygiene_line()
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": H.time.time() - 30.0,
                                                "removed": 10,
                                                "unreadable": False})
        line = H._state_hygiene_line()
        assert "last start swept 10 entries" in line
        assert "30s ago" in line
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": H.time.time() - 2.0,
                                                "removed": 0,
                                                "unreadable": False})
        assert "last start swept nothing (just now)" in H._state_hygiene_line()

    def test_the_sweep_records_its_result_for_the_doctor(
            self, H, sandbox, monkeypatch):
        """The wiring: a real sweep run stamps `_LAST_SWEEP` — removals, an
        unreadable dir, and a clean run all leave the honest stamp. The dict
        is pinned fresh first (process-global, order rule)."""
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": None, "removed": 0,
                                                "unreadable": False})
        monkeypatch.setattr(H, "_SWEEP_WARNED", set())
        monkeypatch.setattr(H, "_SWEPT_DIRS", set())
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        stale = state / "tmpdeadbeef"
        stale.mkdir()
        TestSweepStaleScratch._age(stale, H.time.time())
        assert H._sweep_stale_scratch() == 1
        assert H._LAST_SWEEP["removed"] == 1
        assert H._LAST_SWEEP["at"] is not None
        assert H._LAST_SWEEP["unreadable"] is False
        # and the unreadable case stamps too, rather than leaving the last
        # good reading standing in for a failed one
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "no-such-state")
        H._sweep_stale_scratch()
        assert H._LAST_SWEEP["unreadable"] is True

    def test_an_unreadable_state_dir_reads_as_unreadable(self, H, sandbox):
        """The diagnostic must not fail on the one dir it exists to watch: a
        state dir that cannot be read reports `readable: False`, and the
        line says so instead of showing stale numbers."""
        H.STATE_DIR = sandbox / "no-such-state"
        assert H._state_hygiene() == {"readable": False}
        assert "could not be read" in H._state_hygiene_line()

    def test_the_size_reader_never_follows_a_symlink_out(self, H, sandbox):
        """The runtime hardening rule, in miniature: nothing in the state dir
        may lead out of it — the size reader counts a symlink as an entry
        and does not walk it."""
        state = H.STATE_DIR = sandbox / "state"
        outside = sandbox / "outside"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"x" * 4096)
        state.mkdir()
        (state / "tmpthing").symlink_to(outside)
        size_b, count = H._dir_size(state)
        assert size_b == 0                       # the target was never read
        assert count == 1                        # ...but the link is an entry


class TestStateHygieneTrend:
    """The trend log: one reading per start, and growth over a week.

    A single size says nothing about whether the state dir is GROWING, which is
    the question an operator actually has. Every start appends its reading to a
    capped jsonl (`state-hygiene.jsonl`) and the doctor compares the newest
    reading with one at least `STATE_HYGIENE_TREND_DAYS` old — never with a
    convenient shorter one — so the number on the line is a week of evidence
    rather than an hour of it wearing a weekly label.
    """

    NOW = 1_800_000_000.0

    def _history(self, H, rows) -> Path:
        """Write synthetic readings `(days_ago, size_bytes, entries)`; returns
        the log path. The timestamps are the test's own clock, so the window
        arithmetic is decided by the fixture and not by when the suite runs."""
        lines = [json.dumps({"at": self.NOW - days * 86400.0,
                             "date": "2026-01-01", "scratch_left": 0,
                             "size_bytes": size, "entries": entries,
                             "swept": 0}, sort_keys=True)
                 for days, size, entries in rows]
        log = H._state_hygiene_log()
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return log

    def test_a_start_records_one_row_of_the_reading(self, H, sandbox,
                                                   monkeypatch):
        """The row IS the reading the line shows: same collector, one dict,
        so a trend cannot be computed from numbers the doctor never saw."""
        monkeypatch.setattr(H, "_HYGIENE_LOGGED", set())
        # `_LAST_SWEEP` is a process global every sweep test leaves a mark on;
        # unpinned, this row's `swept` is whichever neighbour swept last, which
        # the test-order shuffle exposed (seed 424242, 2026-09-25)
        monkeypatch.setattr(H, "_LAST_SWEEP", {"at": None, "removed": 0,
                                                "unreadable": False})
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        (state / "handsoff.lock").write_text("x", encoding="utf-8")
        before = H._state_hygiene()          # taken BEFORE the row exists
        assert H._record_state_hygiene() is True
        rows = H._read_state_hygiene_log()
        assert len(rows) == 1
        row = rows[0]
        assert row["entries"] == before["entries"]
        assert row["size_bytes"] == before["size_bytes"]
        assert row["scratch_left"] == before["scratch_left"] == 0
        assert row["swept"] == 0
        assert row["date"] == H.datetime.date.fromtimestamp(
            row["at"]).isoformat()
        assert (H._state_hygiene_log().stat().st_mode & 0o777) == 0o600

    def test_one_row_per_start_not_per_call(self, H, sandbox, monkeypatch):
        """`_prepare_runtime()` runs twice per service process, so the append
        is latched exactly as the sweep is: two rows a second apart are not a
        trend, they are one sample recorded twice."""
        monkeypatch.setattr(H, "_HYGIENE_LOGGED", set())
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        assert H._record_state_hygiene() is True
        assert H._record_state_hygiene() is False        # the second pass
        assert len(H._read_state_hygiene_log()) == 1

    def test_the_trend_compares_against_a_reading_a_week_old(
            self, H, sandbox, monkeypatch):
        """Three readings, one inside the window: the delta must come from the
        OLDER one, because a week is the claim the line makes."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True)
        self._history(H, [(12, 1_000_000_000, 10),
                          (6, 2_000_000_000, 15),
                          (0, 2_000_000_000, 30)])
        trend = H._hygiene_trend()
        assert trend["rows"] == 3
        assert trend["span_days"] == 12.0
        assert trend["since_days"] == 12.0      # NOT the 6-day-old reading
        assert trend["size_delta"] == 1_000_000_000
        assert trend["entries_delta"] == 20
        line = H._state_hygiene_line()
        assert "7-day trend +953.7 MB, +20 entries" in line

    def test_a_short_history_reports_its_span_and_no_deltas(
            self, H, sandbox, monkeypatch):
        """A week-over-week number computed from three days of samples would
        be a lie shaped like a trend, so none is offered."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True)
        self._history(H, [(3, 1_000_000_000, 10), (0, 2_000_000_000, 30)])
        trend = H._hygiene_trend()
        assert trend["rows"] == 2
        assert "size_delta" not in trend and "entries_delta" not in trend
        assert "trend needs a week (3.0 days recorded)" in H._state_hygiene_line()

    def test_no_history_at_all_says_so(self, H, sandbox, monkeypatch):
        """A missing clause would be indistinguishable from a trend that
        stopped being computed, so an empty log is a positive finding."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True)
        assert H._hygiene_trend() == {"rows": 0}
        assert "trend: no readings yet" in H._state_hygiene_line()

    def test_a_torn_or_foreign_line_is_dropped_not_fatal(
            self, H, sandbox, monkeypatch):
        """A power cut leaves a torn last line, and the log is a diagnostic:
        the history must still render (what it can read) instead of throwing
        away the growth it exists to show."""
        monkeypatch.setattr(H, "STATE_DIR", sandbox / "state")
        H.STATE_DIR.mkdir(parents=True)
        H._state_hygiene_log().write_text(
            "not json at all\n"
            "{}\n"                                  # no `at`
            "{\"at\": \"soon\"}\n"                  # not a number
            f"{json.dumps({'at': self.NOW, 'size_bytes': 5, 'entries': 1})}\n"
            '{"at": 17, "size_b',                  # torn mid-line
            encoding="utf-8")
        rows = H._read_state_hygiene_log()
        assert len(rows) == 1 and rows[0]["at"] == self.NOW
        assert "trend needs a week (0.0 days recorded)" in H._state_hygiene_line()

    def test_the_log_is_capped_and_drops_the_oldest(self, H, sandbox,
                                                   monkeypatch):
        """Unbounded diagnostics are the leak this line reports: the log is
        capped, and the cap drops the OLDEST rows, not the newest."""
        monkeypatch.setattr(H, "_HYGIENE_LOGGED", set())
        monkeypatch.setattr(H.time, "time", lambda: self.NOW)
        state = H.STATE_DIR = sandbox / "state"
        state.mkdir(parents=True)
        count = H.STATE_HYGIENE_MAX + 10
        H._state_hygiene_log().write_text(
            "".join(json.dumps({"at": self.NOW - i, "size_bytes": i,
                                "entries": i}) + "\n" for i in range(count)),
            encoding="utf-8")
        assert H._record_state_hygiene() is True
        rows = H._read_state_hygiene_log()
        assert len(rows) == H.STATE_HYGIENE_MAX
        assert rows[-1]["at"] == self.NOW            # the new reading is last
        assert rows[0]["at"] == self.NOW - 11        # the oldest were dropped

    def test_an_unreadable_state_dir_records_nothing(self, H, sandbox,
                                                    monkeypatch):
        """Nothing to read, nothing to record — and no file conjured up in a
        directory the reading just said it could not see."""
        monkeypatch.setattr(H, "_HYGIENE_LOGGED", set())
        H.STATE_DIR = sandbox / "no-such-state"
        assert H._record_state_hygiene() is False
        assert not H._state_hygiene_log().exists()

    def test_prepare_runtime_records_the_post_reclaim_reading(
            self, H, sandbox, monkeypatch):
        """The wiring, and the ORDER: the row is taken after the sweep, so a
        start that reclaimed scratch says so in its own row."""
        monkeypatch.setattr(H, "_HYGIENE_LOGGED", set())
        leaked = H.STATE_DIR / "tmpdeadbeef"
        leaked.mkdir(parents=True)
        (leaked / "tts.wav").write_bytes(b"RIFF")
        TestSweepStaleScratch._age(leaked, H.time.time())
        assert H._prepare_runtime() is True
        rows = H._read_state_hygiene_log()
        assert len(rows) == 1
        assert rows[0]["swept"] == 1                 # the sweep ran first
        assert rows[0]["scratch_left"] == 0
        assert not leaked.exists()


class TestNoUnboundedBlockingCall:
    """A voice assistant is a set of long-lived threads, and one call that can
    wait forever holds its thread — and whatever the thread was doing for the
    user — for the life of the process. Every `subprocess.run`, `urlopen` and
    `create_connection` in the shipped sources therefore states its timeout.

    Found by sweeping the tree for the shape, which turned up exactly one: the
    self-test's clipboard restore ran `wl-copy` with its output captured and no
    timeout, and wl-copy forks a server that keeps the inherited pipes open —
    `copy_text` beside it already detached them and bounded the call.
    """

    SOURCES = ("handsoff.py", "hardware.py", "settings_schema.py",
               "handsoff-settings.py")

    @staticmethod
    def _calls_without_a_timeout(path: Path) -> list:
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (ast.unparse(fn) if isinstance(fn, (ast.Attribute, ast.Name))
                    else "")
            keywords = {k.arg for k in node.keywords}
            if None in keywords:            # **kwargs: cannot be judged here
                continue
            if name in ("subprocess.run", "subprocess.check_output",
                        "subprocess.check_call", "subprocess.call"):
                bare = "timeout" not in keywords
            elif name.endswith("urlopen"):
                bare = "timeout" not in keywords and len(node.args) < 3
            elif name == "socket.create_connection":
                bare = "timeout" not in keywords and len(node.args) < 2
            else:
                continue
            if bare:
                found.append(f"{path.name}:{node.lineno}: {ast.unparse(node)[:90]}")
        return found

    def test_no_shipped_call_can_wait_forever(self):
        paths = [ROOT / name for name in self.SOURCES]
        paths += sorted((ROOT / "core").glob("*.py"))
        offenders = [line for p in paths if p.is_file()
                     for line in self._calls_without_a_timeout(p)]
        assert not offenders, (
            "these calls have no timeout, so a wedged far end holds the calling "
            "thread for ever:\n  " + "\n  ".join(offenders))

    def test_the_guard_does_catch_the_shape_it_guards(self, tmp_path):
        """A sweep that finds nothing proves nothing until it is shown to find
        something."""
        sample = tmp_path / "sample.py"
        sample.write_text(
            "import subprocess, socket, urllib.request\n"
            "subprocess.run(['a'])\n"
            "subprocess.run(['a'], timeout=3)\n"
            "urllib.request.urlopen('http://x')\n"
            "urllib.request.urlopen('http://x', timeout=3)\n"
            "socket.create_connection(('h', 1))\n"
            "socket.create_connection(('h', 1), 5)\n", encoding="utf-8")
        found = self._calls_without_a_timeout(sample)
        assert [line.split(":")[1] for line in found] == ["2", "4", "6"], found
