"""Lifecycle tests: launch, control socket, streaming, restart, repo integrity gates."""
from __future__ import annotations

import base64
import importlib.util
import inspect
from collections import deque
import threading
import io
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import HERE as ROOT, _load, _user_site

HERE = ROOT   # the repo root (conftest resolves it from conftest.py's parent)


class TestCoreLifecycle:
    """core.lifecycle imports independently and provides minimal turn primitives."""

    def test_core_lifecycle_imports_independently(self):
        """core.lifecycle must not import handsoff.py, Qt, or Assistant."""
        import core.lifecycle as lifecycle

        # Verify the module loads without pulling in heavy deps
        assert hasattr(lifecycle, "TurnState")
        assert hasattr(lifecycle, "next_turn")

    def test_turnstate_is_dataclass_with_four_fields(self):
        """TurnState is a small dataclass with generation, cancel, done, result."""
        import core.lifecycle as lifecycle
        from dataclasses import fields

        fs = {f.name for f in fields(lifecycle.TurnState)}
        assert fs == {"generation", "cancel", "done", "result"}

        # Can construct with all fields
        cancel = threading.Event()
        done = threading.Event()
        ts = lifecycle.TurnState(generation=42, cancel=cancel, done=done, result="ok")
        assert ts.generation == 42
        assert ts.cancel is cancel
        assert ts.done is done
        assert ts.result == "ok"

    def test_next_turn_advances_counter_and_returns_fresh_turnstate(self):
        """next_turn increments the mutable counter and returns a fresh TurnState."""
        import core.lifecycle as lifecycle

        counter = [0]
        ts1 = lifecycle.next_turn(counter)
        assert ts1.generation == 1
        assert counter[0] == 1
        assert isinstance(ts1.cancel, threading.Event)
        assert isinstance(ts1.done, threading.Event)
        assert not ts1.cancel.is_set()
        assert not ts1.done.is_set()
        assert ts1.result is None

        ts2 = lifecycle.next_turn(counter)
        assert ts2.generation == 2
        assert counter[0] == 2
        # Fresh events each call
        assert ts2.cancel is not ts1.cancel
        assert ts2.done is not ts1.done

    def test_next_turn_works_with_dict_counter(self):
        """next_turn also accepts a dict as the mutable counter container."""
        import core.lifecycle as lifecycle

        counter = {"gen": 0}
        ts1 = lifecycle.next_turn(counter)
        assert ts1.generation == 1
        assert counter["gen"] == 1

        ts2 = lifecycle.next_turn(counter)
        assert ts2.generation == 2
        assert counter["gen"] == 2


class TestSourceIntegrity:
    @pytest.mark.parametrize("name", ["handsoff.py", "handsoff-settings.py"])
    def test_compiles(self, name):
        src = (HERE / name).read_text(encoding="utf-8")
        compile(src, name, "exec")

    @pytest.mark.parametrize("name", ["handsoff.py", "handsoff-settings.py"])
    def test_marker_present(self, name, H):
        second_line = (HERE / name).read_text(encoding="utf-8").splitlines()[1]
        assert second_line == H.SELF_MARKER

    def test_ptt_actions_documented_in_usage(self, H):
        for word in H.PTT_ACTIONS:
            assert word in H.USAGE

    def test_shutdown_is_bounded_and_rejects_new_work(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._shutdown_event = threading.Event()
        a._closed = False
        a._workers = set()
        a._cancel = threading.Event()
        a._listener = types.SimpleNamespace(stop=lambda: None)
        a._tools = None
        a._notification_thread = None
        a._notification_stop = None
        a._notification_proc = None
        a._pomodoro_stop = None
        a._pomodoro_state = None
        a._recorder = None
        a._pipeline_q = __import__("queue").Queue(maxsize=1)
        a._state = H.IDLE
        a._gen = 0
        spoken = []
        a._speak = lambda *args, **kwargs: spoken.append(args[0])
        started = threading.Event()
        release = threading.Event()
        worker = a._start_worker(
            lambda: (started.set(), release.wait(5)), name="stuck-test-worker")
        assert started.wait(1)
        began = time.monotonic()
        a.shutdown()
        assert time.monotonic() - began < 1.0
        release.set()
        worker.join(1)
        assert a._closed is True
        assert a._shutdown_event.is_set()
        a._announce_now("late")
        assert spoken == []
        queued = a._pipeline_q.qsize()
        a.submit_audio(np.zeros(16000, dtype=np.int16))
        assert a._pipeline_q.qsize() == queued


# ------------------------------------------------------------------ control socket


class TestControlSocket:
    @pytest.fixture()
    def server(self, H, tmp_path):
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        delivered: list[str] = []
        # keep the module's real socket path out of the picture: point the
        # module-level constant at a fresh per-test path
        sock_path = tmp_path / "control.sock"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(H, "CONTROL_SOCK", sock_path)
        asst = H.Assistant()
        asst.sigCommand.connect(delivered.append)
        srv = H.ControlServer(asst)
        srv.start()
        # wait until the server actually answers (the socket file may exist
        # before the thread is listening, and stale files from old runs linger)
        deadline = time.time() + 5
        srv_ready, last_err = False, None
        while time.time() < deadline:
            try:
                if self._roundtrip(sock_path, "status").startswith("state="):
                    srv_ready = True
                    break
            except OSError as e:
                last_err = e
            time.sleep(0.05)
        assert srv_ready, f"control server never answered ({last_err})"
        try:
            yield H, delivered, app
        finally:
            monkey.undo()
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _roundtrip(sock_path: Path, action: str) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall(action.encode())
        s.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            part = s.recv(1024)
            if not part:
                break
            reply += part
        s.close()
        return reply.decode()

    def test_status_roundtrip(self, server):
        H, _delivered, _app = server
        assert H.ptt_client(["status"]) == 0

    def test_status_reply_content(self, server):
        H, _delivered, _app = server
        text = self._roundtrip(H.CONTROL_SOCK, "status")
        assert text.startswith("state=idle")
        assert "handsfree=" in text and "model=" in text

    def test_action_delivery_via_event_loop(self, server):
        H, delivered, app = server
        assert H.ptt_client(["interrupt"]) == 0
        deadline = time.time() + 3
        while "interrupt" not in delivered and time.time() < deadline:
            app.processEvents()
        assert "interrupt" in delivered

    def test_unknown_command_replies_error(self, server):
        H, _delivered, _app = server
        assert self._roundtrip(H.CONTROL_SOCK, "bogus").startswith(
            "error: unknown command 'bogus'")

    def test_client_rejects_unknown_action(self, H):
        assert H.ptt_client(["nonsense"]) == 2

    def test_client_without_server(self, H, tmp_path, monkeypatch):
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "missing.sock")
        assert H.ptt_client(["status"]) == 1


class TestBubbleMenuHoldGuard:
    def test_right_click_menu_stops_and_guards_hold_timer(self, H, monkeypatch):
        """Opening the modal menu must not let a queued hold start PTT."""
        class Hold:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Assistant:
            _handsfree = False

            def __init__(self):
                self.begin_calls = 0

            def begin_listening(self):
                self.begin_calls += 1

        assistant = Assistant()
        widget = H.BubbleWidget.__new__(H.BubbleWidget)
        widget._assistant = assistant
        widget._hold = Hold()
        widget._pressing = True
        widget._dragging = False
        widget._listening = False

        class Menu:
            def __init__(self, owner):
                self.owner = owner

            def addAction(self, _text):
                return object()

            def addSeparator(self):
                pass

            def exec(self, _pos):
                assert self.owner._menu_open is True
                self.owner._hold_fired()  # simulate the queued timeout
                return None

        monkeypatch.setattr(H, "QMenu", Menu)

        class Event:
            def button(self):
                return H.Qt.RightButton

            def globalPosition(self):
                return types.SimpleNamespace(toPoint=lambda: None)

        widget.mousePressEvent(Event())
        assert widget._hold.stopped is True
        assert assistant.begin_calls == 0


# ------------------------------------------------------------------ offscreen launch


class TestOffscreenLaunch:
    def test_bubble_starts_and_opens_control_socket(self, H):
        """Full launch under QT_QPA_PLATFORM=offscreen in a sandboxed HOME."""
        with tempfile.TemporaryDirectory(prefix="handsoff-test-") as tmp:
            home = Path(tmp)
            state = home / "state"
            env = dict(os.environ)
            env.update({
                "HOME": str(home),
                "XDG_STATE_HOME": str(state),
                "QT_QPA_PLATFORM": "offscreen",
                # an empty theme stops Qt from loading the GTK theme, which
                # needs a real display and kills the process headless
                "QT_QPA_PLATFORMTHEME": "",
                "NO_AT_BRIDGE": "1",
                "QT_ACCESSIBILITY": "0",
                "OLLAMA_HOST": "http://127.0.0.1:9",  # unreachable: loader logs, ok
                "HF_HUB_OFFLINE": "1",                # no model download in tests
                # sandboxed HOME hides the user site-packages that hold PySide6
                "PYTHONPATH": ".".join(p for p in (_user_site(), env.get("PYTHONPATH", "")) if p),
            })
            for var in ("NIRI_CONFIG", "DISPLAY", "WAYLAND_DISPLAY"):
                env.pop(var, None)
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "handsoff.py")],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                sock = state / "handsoff" / "control.sock"
                deadline = time.time() + 15
                while not sock.exists() and time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                assert sock.exists(), (
                    f"bubble exited early (rc={proc.poll()}):\n"
                    + proc.stderr.read().decode(errors="replace")[-2000:]
                )
                # the --ptt client from the test process must reach the bubble
                out = subprocess.run(
                    [sys.executable, str(HERE / "handsoff.py"), "--ptt", "status"],
                    env=env, capture_output=True, text=True, timeout=10,
                )
                assert out.returncode == 0, out.stderr
                assert out.stdout.startswith("state=idle")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


# ------------------------------------------------------------------ settings app


class TestStreamingChat:
    """ollama_chat_stream against a fake local ollama NDJSON server."""

    def test_core_brain_public_names_and_turn_isolation(self):
        from core import brain

        assert callable(brain.ollama_chat)
        assert callable(brain.ollama_chat_stream)
        assert callable(brain.strip_thinking)
        old = brain.TurnStream(1, threading.Event(), queue.Queue())
        new = brain.TurnStream(2, threading.Event(), queue.Queue())
        old.result = {"content": "old"}
        new.result = {"content": "new"}
        assert old.result != new.result
        assert old.generation == 1 and new.generation == 2

    @pytest.fixture  # function-scoped: class-scope-on-instance-method is deprecated (removed in pytest 10)
    def fake_ollama(self):
        import subprocess as sp, socket, time
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        proc = sp.Popen([sys.executable, str(HERE / "tests" / "fake_ollama.py"), str(port)],
                        stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        time.sleep(0.8)
        yield f"http://127.0.0.1:{port}"
        proc.terminate()

    def test_stream_sentences_and_tool_calls(self, H, fake_ollama):
        import queue as qmod
        monkey_patch_target = fake_ollama
        old_base = H.OLLAMA_BASE
        H.OLLAMA_BASE = fake_ollama
        try:
            q = qmod.Queue()
            msgs = [{"role": "user", "content": "hi"}]
            res = H.ollama_chat_stream(msgs, q, None, H.TOOLS)
            assert res["tool_calls"], "tool call must be collected from the stream"
            assert res["tool_calls"][0]["function"]["name"] == "copy_text"
            s1 = q.get(timeout=2)
            assert s1 == "Copied it."   # content sentence
            assert q.get(timeout=2) is None  # then terminator
            # follow-up with a tool result streams plain sentences
            msgs += [{"role": "assistant", "content": "", "tool_calls": res["tool_calls"]},
                     {"role": "tool", "tool_name": "copy_text", "content": "ok"}]
            q2 = qmod.Queue()
            res2 = H.ollama_chat_stream(msgs, q2, None, H.TOOLS)
            sentences = []
            while True:
                item = q2.get(timeout=2)
                if item is None:
                    break
                sentences.append(item)
            assert sentences == ["One.", "Two.", "Three."], sentences
            assert res2["content"] == "One. Two. Three."
        finally:
            H.OLLAMA_BASE = old_base

    def test_old_stream_cannot_overwrite_new_turn_result(self, H, monkeypatch):
        """A late canceled stream owns its result and cannot replace turn B's."""
        asst = H.Assistant.__new__(H.Assistant)
        asst._tools = types.SimpleNamespace()
        asst._gen = 1
        asst._history = []
        asst._turn_spoke = False
        asst._conversation_for = lambda text: [{"role": "system"},
                                                {"role": "user", "content": text}]
        asst._save_history = lambda: None
        asst._set = lambda *_args: None
        old_started = threading.Event()
        release_old = threading.Event()
        old_cancel = threading.Event()
        new_started = threading.Event()
        new_speaking = threading.Event()
        release_new = threading.Event()
        calls = []

        def fake_stream(_conversation, _q, _cancel, _tools):
            calls.append(len(calls) + 1)
            if calls[-1] == 1:
                old_started.set()
                release_old.wait(2)
                return {"content": "old", "tool_calls": []}
            new_started.set()
            return {"content": "new", "tool_calls": []}

        def fake_speak(_text, gen, _cancel, sentence_q=None):
            if gen == 2:
                new_speaking.set()
                release_new.wait(2)

        monkeypatch.setitem(H.SETTINGS, "streaming_tts", True)
        monkeypatch.setattr(H, "ollama_chat_stream", fake_stream)
        asst._speak = fake_speak
        old = threading.Thread(target=H.Assistant._brain_turn,
                               args=(asst, "old", 1, old_cancel))
        old.start()
        assert old_started.wait(1)
        asst._gen = 2
        new_cancel = threading.Event()
        new = threading.Thread(target=H.Assistant._brain_turn,
                               args=(asst, "new", 2, new_cancel))
        new.start()
        assert new_started.wait(1)
        assert new_speaking.wait(1)
        old_cancel.set()
        release_old.set()
        time.sleep(0.05)
        old_cancel.set()
        release_new.set()
        old.join(2)
        new.join(2)
        assert not old.is_alive() and not new.is_alive()
        assert asst._history[-1]["content"] == "new"


class TestConfirmationLoop:
    def test_confirmation_offer_stops_same_model_tool_loop(self, H, monkeypatch):
        """A confirmation offer must not let a later tool call in the same
        model response confirm and execute it."""
        class Belt:
            _last_images = []

            def __init__(self):
                self.calls = []
                self._last_confirmation_offer = False

            def execute(self, name, args):
                self.calls.append(name)
                self._last_confirmation_offer = name == "wait"
                if name == "wait":
                    return "CONFIRM REQUIRED: pending", True
                return "unexpected", True

        asst = H.Assistant.__new__(H.Assistant)
        asst._tools = Belt()
        asst._gen = 1
        asst._history = []
        asst._turn_spoke = False
        asst._conversation_for = lambda text: [{"role": "system"},
                                                {"role": "user", "content": text}]
        asst._save_history = lambda: None
        asst._speak = lambda *args, **kwargs: None
        monkeypatch.setitem(H.SETTINGS, "streaming_tts", False)
        monkeypatch.setattr(H, "ollama_chat", lambda conversation, tools: {
            "content": "",
            "tool_calls": [
                {"function": {"name": "wait", "arguments": {"seconds": 1}}},
                {"function": {"name": "confirm_action", "arguments": {"answer": "yes"}}},
            ],
        })

        H.Assistant._brain_turn(asst, "do it", 1, threading.Event())

        assert asst._tools.calls == ["wait"]


# -------------------------------------------------------------------- audit fixes


class TestRestartScriptSystemdAware:
    """The restart script must defer to systemd when the unit exists
    (a nohup spawn would race the unit's Restart=on-failure)."""

    def test_restart_script_defers_to_systemd(self):
        # Test the repo copy (the source of truth the installer ships), not
        # the installed one: a clean checkout has nothing installed, and
        # checking only the installed copy let the repo version rot.
        script = HERE / "handsoff-restart"
        assert script.exists()
        text = script.read_text()
        assert "systemctl --user is-active" in text
        assert "systemctl --user restart handsoff.service" in text
        assert "exit 0" in text      # systemd path must not fall through to nohup


class TestToolSchemaFromCode:
    """@tool decorator: Python functions ARE the Ollama tool schema."""

    def test_every_tool_has_valid_schema(self, H):
        assert len(H.TOOLS) >= 18
        for t in H.TOOLS:
            fn = t["function"]
            assert fn["name"] and fn["description"].strip()
            params = fn["parameters"]["properties"]
            assert all(v.get("type") in ("string", "integer", "number", "boolean")
                       for v in params.values())
            assert set(fn["parameters"]["required"]) <= set(params)

    def test_decorator_extracts_params_from_signature_and_docstring(self, H):
        @H.tool(description="Test tool.")
        def sample(self, city: str, days: int = 3) -> str:
            """Do a thing.

            city: which city to use
            """
        params = H._param_schema(sample)
        assert params == {
            "city": {"type": "string", "description": "which city to use"},
            "days": {"type": "integer"},
        }

    def test_string_annotations_coerce(self, H, monkeypatch, tmp_path):
        """from __future__ annotations arrive as strings; bool/int must still coerce."""
        monkeypatch.chdir(tmp_path)
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder", {"name": "tea",
                                                 "when": "600",
                                                 "repeat": "24"})
        assert not err and "repeating every 24 hours" in out, out
        row = H.json.loads(rf.read_text())[0]
        assert row["repeat_hours"] == 24.0 and row["name"] == "tea"

    def test_alias_resolution_via_decorator(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder",
                                {"what": "tea", "when": "30", "every": "0"})
        assert not err and "reminder 'tea' set" in out, out
        assert len(H.json.loads(rf.read_text())) == 1

    def test_unknown_tool_still_errors(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("definitely_not_a_tool", {})
        assert err and "unknown tool" in out


class TestRestartResilience:
    """Why 'the bubble won't start again after a restart' happened, pinned forever."""

    def test_startlimit_in_unit_section(self):
        """StartLimitIntervalSec/Burst MUST be in [Unit] — in [Service] systemd
        silently ignores them and the crash-loop guard disappears."""
        section = None
        seen = {}
        for line in (HERE / "install.sh").read_text().splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1]
            elif s.startswith("StartLimitIntervalSec=") or s.startswith("StartLimitBurst="):
                seen[s.split("=")[0]] = section
        assert seen.get("StartLimitIntervalSec") == "Unit"
        assert seen.get("StartLimitBurst") == "Unit"

    def test_restart_always_recovers_clean_exits(self):
        """Restart=always: SIGTERM / Quit / app.quit() must resurrect, not just crashes."""
        text = (HERE / "install.sh").read_text()
        assert "\nRestart=always" in text

    def test_pacman_python_targets_probed_individually(self):
        """CachyOS has no python-pyside6/python-sounddevice in its repos; pacman
        aborts the WHOLE transaction on an unknown target, which killed the
        entire install. Each python target must be probed and skipped, with
        requirements.txt as the fallback provider."""
        text = (HERE / "install.sh").read_text()
        assert 'pacman -Si "$p"' in text, "python targets must be probed per-package"
        assert "$ARCH_PKGS" in text, "transaction must use the probed package list"
        assert "-Syu --needed --noconfirm $ARCH_PKGS" in text

    def test_installer_restarts_active_service(self):
        """An already-running bubble keeps executing the OLD code after a new
        install until restarted — the manifest would say in-sync while the
        live process serves stale logic. The installer must restart it."""
        text = (HERE / "install.sh").read_text()
        assert "systemctl --user is-active --quiet handsoff.service" in text
        assert "systemctl --user restart handsoff.service" in text

    def test_lock_retry_budget_covers_restart_window(self, H):
        """The lock retry loop must outlast the restart script's kill+wait window."""
        assert H.LOCK_RETRIES * H.LOCK_RETRY_WAIT >= 8.0

    def test_installer_enables_correct_ydotoold_unit(self):
        """Arch's user unit is ydotool.service (it starts ydotoold); requiring
        ydotoold.service only made the installer print a WARN while typing
        tools stayed offline despite a perfectly startable unit."""
        text = (HERE / "install.sh").read_text()
        assert "for u in ydotool.service ydotoold.service" in text
        assert "ydotoold running via" in text

    def test_installer_ships_core_doctor(self):
        """core/doctor.py is the new home of the doctor diagnostic; the
        installer must copy it into the deployed set and the deployment
        manifest must hash it (so drift detection still works after the
        monolith cut)."""
        text = (HERE / "install.sh").read_text()
        assert "core/doctor.py" in text, (
            "install.sh must add core/doctor.py to the deployed file set")
        # Manifest must hash it too — the deployment manifest stanza lives
        # inside a `<<MANIFEST_EOF ... MANIFEST_EOF` heredoc and is what
        # _deployment_snapshot() in handsoff.py diffs against. Slice out
        # that body and look for the line.
        open_tag = "<<MANIFEST_EOF"
        close_tag = "MANIFEST_EOF"
        start = text.find(open_tag)
        end = text.find(close_tag, start + len(open_tag))
        assert start != -1 and end != -1, "MANIFEST_EOF heredoc not found"
        manifest_body = text[start + len(open_tag):end]
        assert '"core/doctor.py"' in manifest_body, (
            "core/doctor.py must appear in the deployment manifest hash check")

    def test_lock_failure_logs_instead_of_silent_exit(self, H, monkeypatch):
        """If the lock can't be acquired, say so in the log (no more silent vanish)."""
        import builtins
        msgs = []
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if str(path).endswith("handsoff.lock"):
                raise OSError("simulated contention")
            return real_open(path, *a, **k)

        class FakeLog:
            def error(self, *a):
                msgs.append(a)

            def warning(self, *a):
                pass

            def info(self, *a):
                pass

        monkeypatch.setattr(H, "open", fake_open, raising=False)
        monkeypatch.setattr(H, "log", FakeLog())
        monkeypatch.setattr(H, "LOCK_RETRIES", 2)
        monkeypatch.setattr(H, "LOCK_RETRY_WAIT", 0.01)
        assert H.acquire_lock() is None
        assert any("holds the lock" in " ".join(map(str, m)) for m in msgs)

    def test_whisper_loads_offline(self, H):
        """Startup must never block on the HuggingFace network (11.5s stalls)."""
        src = (HERE / "handsoff.py").read_text()
        assert "local_files_only=True" in src

    def test_menu_quit_stops_unit_first(self, H):
        """Quit under Restart=always must stop the systemd unit, not get resurrected."""
        src = (HERE / "handsoff.py").read_text()
        quit_idx = src.index("if chosen == act_quit:")
        stop_idx = src.index('"systemctl", "--user", "stop"', quit_idx)
        app_quit_idx = src.index("QApplication.quit()", quit_idx)
        assert stop_idx < app_quit_idx  # unit stop happens BEFORE the app exits


class TestPrecommitHook:
    """The versioned pre-commit gate: self-edits that break the suite
    must not be committable. core.hooksPath pins the hook to clones."""

    def test_precommit_hook_is_versioned(self):
        hook = HERE / "githooks" / "pre-commit"
        assert hook.exists(), "githooks/pre-commit went missing"
        text = hook.read_text()
        # the hook must actually gate the things that rot
        assert "py_compile" in text
        assert "bash -n" in text
        assert "pytest" in text
        assert "--no-verify" in text   # documented escape hatch

    def test_hook_blocked_commit_is_reproducible(self):
        """Replay the refusal: run the hook's compile leg against a broken
        file the way git would (staged, cwd = repo root)."""
        import subprocess as sp
        broken = HERE / "zz_hook_probe_broken.py"
        broken.write_text("def broken(:\n    pass\n", encoding="utf-8")
        try:
            r = sp.run([sys.executable, "-m", "py_compile", str(broken)],
                       capture_output=True)
            assert r.returncode != 0, "py_compile must fail on broken syntax"
        finally:
            broken.unlink(missing_ok=True)
