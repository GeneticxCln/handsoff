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
import threading
from pathlib import Path

import pytest

from conftest import HERE as ROOT, _user_site

HERE = ROOT   # the repo root


@pytest.fixture(scope="module")
def H():
    spec = importlib.util.spec_from_file_location(
        "handsoff_core_hardening", HERE / "handsoff.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sandbox(H, monkeypatch, tmp_path):
    """Redirect every runtime path into a private tmp sandbox."""
    cfg = tmp_path / "cfg"
    state = tmp_path / "state"
    monkeypatch.setattr(H, "CONFIG_DIR", cfg)
    monkeypatch.setattr(H, "STATE_DIR", state)
    monkeypatch.setattr(H, "WHISPER_MODEL_DIR", cfg / "whisper-model")
    monkeypatch.setattr(H, "PIPER_VOICE_DIR", cfg / "piper-voice")
    monkeypatch.setattr(H, "SETTINGS_FILE", cfg / "settings.json")
    monkeypatch.setattr(H, "HISTORY_FILE", cfg / "history.json")
    monkeypatch.setattr(H, "MEMORY_FILE", cfg / "memory.json")
    monkeypatch.setattr(H, "CRASH_LOG", state / "crash.log")
    monkeypatch.setattr(H, "PENDING_FILE", state / "pending-restart.json")
    monkeypatch.setattr(H, "LOCK_FILE", state / "handsoff.lock")
    monkeypatch.setattr(H, "LOG_FILE", state / "handsoff.log")
    monkeypatch.setattr(H, "CONTROL_SOCK", state / "control.sock")
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
        assert H._secure_file(sandbox / "state" / "nope.json") is True

    def test_regular_file_gets_0600(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "f.json"
        f.write_text("{}")
        f.chmod(0o644)
        assert H._secure_file(f) is True
        assert (f.stat().st_mode & 0o777) == 0o600

    def test_refuses_symlink_including_broken(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        target = sandbox / "real.txt"
        target.write_text("data")
        link = state / "link.json"
        link.symlink_to(target)
        assert H._secure_file(link) is False
        broken = state / "broken.json"
        broken.symlink_to(sandbox / "ghost")     # broken symlink
        assert H._secure_file(broken) is False

    def test_refuses_directory(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        d = state / "dir.json"
        d.mkdir()
        assert H._secure_file(d) is False

    def test_refuses_foreign_owned(self, H, monkeypatch, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "f.json"
        f.write_text("{}")
        monkeypatch.setattr(H.os, "getuid", lambda: f.stat().st_uid + 1)
        assert H._secure_file(f) is False

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
        assert H._secure_file(sp) is True         # self-healed, not refused
        assert (sp.stat().st_mode & 0o777) == 0o600
        s.close()

    def test_already_private_socket_untouched(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        sp.chmod(0o600)
        assert H._secure_file(sp) is True
        s.close()


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
        srv = H.ControlServer.__new__(H.ControlServer)
        srv._assistant = None
        srv._stop = threading.Event()
        srv._server = None
        srv._thread = None
        srv._serve()                              # must not raise
        assert srv._server is None
        assert not (sandbox / "state" / "control.sock").exists()

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
        spec.loader.exec_module(mod)
        audio = mod._audio
        report = {
            "class": type(audio).__name__,
            "device_choice": list(audio._whisper_device_choice()),
            "cuda_error": bool(audio._is_cuda_error("cuda")),
            "free_vram": audio._nvidia_free_vram_mb(),
            "whisper_model": audio._whisper_model,
            "piper_voice": audio._piper_voice,
            "configure_returns_none": audio.configure() is None,
            "portaudio_busy": audio.portaudio_busy(),
            "aliases": {
                "resample": callable(mod._resample_to_16k),
                "mic_lock": hasattr(mod._MIC_OPERATION_LOCK, "acquire"),
            },
        }
        for probe in ("transcribe", "get_whisper", "play_wav"):
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
        env = dict(os.environ)
        # a redirected HOME hides user site-packages (sounddevice + PySide6 live
        # there), so keep the real one on PYTHONPATH: this test is about the
        # missing core/audio.py, not about missing dependencies
        env.update({
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(HERE), _user_site(), env.get("PYTHONPATH", "")) if p),
        })
        proc = subprocess.run(
            [sys.executable, "-c", self.DRIVER, str(HERE)],
            env=env, capture_output=True, text=True, timeout=180, cwd=str(HERE),
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        assert report["class"] == "_MissingAudio"
        # the defensive stubs other code reads must keep their documented values
        assert report["device_choice"] == ["cpu", "int8"]
        assert report["cuda_error"] is False
        assert report["free_vram"] is None
        assert report["whisper_model"] is None
        assert report["piper_voice"] is None
        # configure() is called at import time: a partial install must get here
        assert report["configure_returns_none"] is True
        # nothing is open in a partial install, so the teardown guard is inert
        assert report["portaudio_busy"] is False
        assert report["portaudio_ctx"] is True
        assert report["aliases"] == {"resample": True, "mic_lock": True}
        # and the audio entry points must fail loudly, never return junk
        for probe in ("transcribe", "get_whisper", "play_wav", "Recorder"):
            assert report[probe] == "ImportError", probe
