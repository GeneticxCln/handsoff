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
