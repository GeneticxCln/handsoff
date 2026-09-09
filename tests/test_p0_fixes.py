"""P0 regression pins: single-exec start_command, pipe-drain jobs, spawn
bypass, corrupt-config quarantine, no-truncate lock, atomic writes."""
from __future__ import annotations

import fcntl
import io
import json
import time
from pathlib import Path

import pytest

from conftest import HERE as ROOT

HERE = ROOT   # the repo root


def _belt(H, monkeypatch):
    """Construct via __init__ (never __new__): pins the real init path."""
    monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
    return H.ToolBelt(
        on_restart_pending=lambda: None,
        permissions={**H.DEFAULT_SETTINGS["permissions"]},
        on_announce=lambda text: None,
    )


class TestStartCommandSingleExec:
    """start_command must validate without executing: exactly one spawn."""

    def test_start_command_runs_once(self, H, monkeypatch):
        tb = _belt(H, monkeypatch)
        launches: list = []

        def fail_run(*a, **k):
            raise AssertionError("run_command executed during validation!")

        class _FakeOut(io.StringIO):
            pass

        class _FakeProc:
            def __init__(self):
                self.stdout = _FakeOut("")
                self.returncode = None

            def poll(self):
                return None

        def fake_popen(*a, **k):
            launches.append(a)
            return _FakeProc()

        monkeypatch.setattr(H.subprocess, "run", fail_run)
        monkeypatch.setattr(H.subprocess, "Popen", fake_popen)
        out, err = tb.execute("start_command", {"command": "echo hello-once"})
        assert not err, out
        assert "started job-" in out
        assert len(launches) == 1, launches


class TestPipeFillDeadlock:
    """A job printing >64k (pipe buffer) must still complete via the drain."""

    def test_verbose_job_completes(self, H, monkeypatch):
        tb = _belt(H, monkeypatch)
        # no real `cat` / tmp file: `echo` with a >64k arg floods the pipe
        # through the same Popen + drain-thread path.
        payload = "x" * 100_000
        out, err = tb.execute(
            "start_command",
            {"command": f"echo PIPEFILL-START {payload} PIPEFILL-END"})
        assert not err, out
        deadline = time.time() + 15
        while time.time() < deadline:
            out, err = tb.execute("job_status", {})
            assert not err, out
            if "exit code" in out:
                break
            time.sleep(0.05)
        assert "exit code 0" in out, out
        assert "PIPEFILL-END" in out, out[-500:]


class TestSpawnTerminalBypass:
    """spawn must not smuggle interpreters/terminals in extra args."""

    @pytest.mark.parametrize(
        "which_result", ["/usr/bin/tool", None],
        ids=["binary-present", "binary-missing"])
    def test_terminal_with_args_and_interpreter_arg_denied(
            self, H, monkeypatch, which_result):
        executed: list = []
        # refusal happens before the which() install check, so it must hold
        # whether or not the binary is present (missing would be ERROR, not
        # REFUSED, if the ordering ever regressed).
        monkeypatch.setattr(H.shutil, "which", lambda p: which_result)
        monkeypatch.setattr(
            H.subprocess, "run",
            lambda argv, **kw: executed.append(argv) or type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        for cmd in ("niri msg action spawn -- alacritty bash",
                    "niri msg action spawn -- firefox -- python3 x"):
            out, err = belt.execute("run_command", {"command": cmd})
            assert err and "REFUSED" in out, (cmd, out)
        assert executed == [], "spawn bypass reached subprocess!"


class TestCorruptSettingsFailClosed:
    """Garbage settings.json quarantines to *.bad-* and yields defaults."""

    def test_garbage_quarantined_and_defaults_used(self, H, monkeypatch, tmp_path):
        cfg = tmp_path / "settings.json"
        cfg.write_text("{not valid json!!!", encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", cfg)
        for var in ("OLLAMA_HOST", "HANDSOFF_MODEL", "HANDSOFF_NUM_CTX",
                    "HANDSOFF_WHISPER", "HANDSOFF_VOICE"):
            monkeypatch.delenv(var, raising=False)
        s = H._load_settings()
        assert s["model"] == H.DEFAULT_SETTINGS["model"]
        assert s["command_policy"] == {}
        assert s["permissions"]["operator"] is False  # fail-closed
        bad = list(tmp_path.glob("settings.json.bad-*"))
        assert bad, "corrupt settings were not quarantined"


class TestLockNoTruncateOnFail:
    """A failed lock attempt must not wipe the pid file (no O_TRUNC first)."""

    def test_contended_lock_preserves_file(self, H, monkeypatch, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        lock = state / "handsoff.lock"
        lock.write_text("ORIGINAL-CONTENT", encoding="utf-8")
        monkeypatch.setattr(H, "STATE_DIR", state)
        monkeypatch.setattr(H, "LOCK_FILE", lock)
        monkeypatch.setattr(H, "LOCK_RETRIES", 0)
        monkeypatch.setattr(H, "LOCK_RETRY_WAIT", 0.01)
        holder = open(lock, "a+")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            assert H.acquire_lock() is None
            assert lock.read_text(encoding="utf-8") == "ORIGINAL-CONTENT"
        finally:
            try:
                fcntl.flock(holder, fcntl.LOCK_UN)
            except OSError:
                pass
            holder.close()


class TestAtomicPrivateWrite:
    """Atomic writes are 0600, exact, and leave no predictable .tmp behind."""

    def test_mode_content_and_no_predictable_tmp(self, H, tmp_path):
        target = tmp_path / "s.json"
        H._atomic_private_write(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        assert (target.stat().st_mode & 0o777) == 0o600
        assert not (tmp_path / "s.json.tmp").exists()
        assert list(tmp_path.glob("*.tmp")) == []
