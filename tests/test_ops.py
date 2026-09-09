"""Ops and trust tests: deployment reporting, doctor, bounded jobs, and the
installed-copy smoke test (the bubble must work from ~/.local/bin, not only
from the checkout)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from conftest import HERE as ROOT, _user_site

HERE = ROOT   # the repo root


class TestDeploymentReporting:
    """P0: the running product must be able to say which code it is."""

    def test_snapshot_shape(self, H, monkeypatch):
        snap = H._deployment_snapshot()
        assert snap["status"] in (
            "in-sync", "installed-drift", "source-unknown",
            "installed-missing", "running-missing")
        assert snap["running_path"]
        assert set(snap) >= {"running_sha256", "installed_path",
                             "installed_sha256", "manifest"}
        json.dumps(snap)

    def test_repo_source_prefers_checkout_over_installed(self, H, monkeypatch, tmp_path):
        """The known false-in-sync bug: an installed copy must not compare
        against itself."""
        monkeypatch.setenv("HANDSOFF_SOURCE_PATH", str(tmp_path / "missing.py"))
        fake_home = tmp_path / "home"
        (fake_home / ".local" / "bin").mkdir(parents=True)
        src = fake_home / ".local" / "bin" / "handsoff.py"
        src.write_text("# installed copy\n")
        monkeypatch.setattr(H, "HOME", fake_home)
        # running from a checkout beside a .git entry
        (tmp_path / ".git").mkdir()
        checkout = tmp_path / "handsoff.py"
        checkout.write_text("# checkout copy\n")
        monkeypatch.setattr(H, "SELF_PATH", checkout)
        snap = H._deployment_snapshot()
        assert snap["repo_path"] == str(checkout)
        assert snap["installed_path"] == str(src)
        assert snap["running_sha256"] != snap["installed_sha256"]

    def test_health_includes_deployment(self, H, monkeypatch):
        """`--ptt health` must answer 'is the running code the tested code?'"""
        a = H.Assistant.__new__(H.Assistant)
        a._state = "idle"
        a._handsfree = False
        a._followup_until = 0.0
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._frames_seen = 0
        ln._last_nonzero = 0.0
        ln._capture_rate = None
        ln._health_utt = 0
        ln._health_opens_ok = 0
        ln._health_opens_failed = 0
        ln._health_open_device = ""
        ln._health_last_open = ""
        ln._health_failing_since = None
        ln._health_stalled_since = None
        ln._lock = threading.RLock()
        a._listener = ln
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        snap = a.mic_health()
        assert "deployment" in snap
        assert snap["deployment"]["status"] in (
            "in-sync", "installed-drift", "source-unknown",
            "installed-missing", "running-missing")


class TestDoctor:
    """The doctor report: one pass over deployment + dependencies."""

    def test_report_mentions_deployment_and_deps(self, H):
        text = H.run_doctor()
        assert "deployment:" in text
        assert "brain:" in text            # ollama line
        assert "niri IPC:" in text
        assert "systemd unit:" in text
        assert "restart script:" in text
        assert "ydotool:" in text

    def test_doctor_json_shape(self, H):
        d = H.doctor_json()
        assert "deployment" in d and "restart_script" in d
        assert set(d["systemd_unit"]) == {"present", "auto_restart"}

    def test_tool_registered(self, H):
        names = {t["function"]["name"] for t in H.TOOLS}
        assert "handsoff_doctor" in names

    def test_installed_drift_is_called_out(self, H, monkeypatch, tmp_path):
        """A stale installed copy must be flagged, not just reported."""
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        running = tmp_path / "checkout" / "handsoff.py"
        running.parent.mkdir(parents=True)
        running.write_text("# checkout bytes\n")
        installed = bin_dir / "handsoff.py"
        installed.write_text("# OLD deployed bytes\n")   # drifted!
        monkeypatch.setattr(H, "SELF_PATH", running)
        monkeypatch.setattr(H, "HOME", tmp_path)
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://127.0.0.1:9")
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no niri in tests"))))
        text = H.run_doctor()
        assert "STALE" in text or "installed-drift" in text


class TestBoundedJobs:
    """start_command / job_status: bounded background jobs with completion
    announcements and a refusal policy identical to run_command."""

    def _belt(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"]}
        tb._tool_times = deque()
        tb._policy = H.DecisionPolicy(H.SETTINGS)
        tb._pending_confirm = None
        tb._confirm_running = None
        tb._jobs = {}
        tb._job_seq = 0
        tb._job_lock = threading.Lock()
        announced: list[str] = []
        tb._on_announce = announced.append
        return tb, announced

    def test_job_runs_and_completes(self, H, monkeypatch):
        tb, announced = self._belt(H, monkeypatch)
        out, err = tb.execute("start_command", {"command": "echo hello-jobs"})
        assert not err, out
        assert "started job-" in out
        # poll until done (echo exits fast)
        deadline = time.time() + 10
        while time.time() < deadline:
            out, err = tb.execute("job_status", {})
            assert not err, out
            if "exit code" in out:
                break
            time.sleep(0.05)
        assert "exit code 0" in out
        assert "hello-jobs" in out
        assert announced and "exit code 0" in announced[0]

    def test_job_id_refusals_match_run_command(self, H, monkeypatch):
        """Bounded jobs must not widen the whitelist."""
        tb, _ = self._belt(H, monkeypatch)
        out, err = tb.execute("start_command", {"command": "rm -rf /tmp/x"})
        assert err and "rm" in out          # same refusal as run_command

    def test_job_status_unknown_id(self, H, monkeypatch):
        tb, _ = self._belt(H, monkeypatch)
        out, err = tb.execute("job_status", {"job_id": "job-999"})
        assert err and "no job" in out

    def test_job_status_empty(self, H, monkeypatch):
        tb, _ = self._belt(H, monkeypatch)
        out, err = tb.execute("job_status", {})
        assert not err and "no background jobs" in out

    def test_job_limit_is_bounded(self, H, monkeypatch):
        tb, _ = self._belt(H, monkeypatch)
        tb._jobs = {f"job-{i}": None for i in range(H.BoundedJob.MAX_JOBS)}
        out, err = tb.execute("start_command", {"command": "echo x"})
        assert err and "job limit reached" in out

    def test_bounded_job_poll_timeout(self, H):
        proc = subprocess.Popen(["sleep", "60"])
        job = H.BoundedJob("j", "sleep 60", proc)
        job.started = time.monotonic() - (H.BoundedJob.MAX_LIFETIME_S + 5)
        state, done = job.poll()
        try:
            assert state == "timeout-killed" and done
            assert proc.poll() is not None    # was actually killed
        finally:
            proc.kill()
            proc.wait()


class TestInstalledCopySmoke:
    """The bubble must run from ~/.local/bin (the deployed copy), not only
    from the checkout: install to a fake home, launch, poke the socket."""

    def test_deployed_bubble_starts_and_answers(self, tmp_path):
        home = tmp_path / "home"
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("handsoff.py", "handsoff-settings.py", "handsoff-restart"):
            src = HERE / name
            if src.exists():
                (bin_dir / name).write_bytes(src.read_bytes())
        (bin_dir / "handsoff-restart").chmod(0o755)
        state = home / "state"
        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "XDG_STATE_HOME": str(state),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QPA_PLATFORMTHEME": "",
            "NO_AT_BRIDGE": "1",
            "QT_ACCESSIBILITY": "0",
            "OLLAMA_HOST": "http://127.0.0.1:9",
            "HF_HUB_OFFLINE": "1",
            "PYTHONPATH": ".".join(p for p in (_user_site(), env.get("PYTHONPATH", "")) if p),
        })
        for var in ("NIRI_CONFIG", "DISPLAY", "WAYLAND_DISPLAY"):
            env.pop(var, None)
        proc = subprocess.Popen(
            [sys.executable, str(bin_dir / "handsoff.py")],
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
                f"installed copy exited early (rc={proc.poll()}):\n"
                + proc.stderr.read().decode(errors="replace")[-2000:])
            out = subprocess.run(
                [sys.executable, str(bin_dir / "handsoff.py"),
                 "--ptt", "status"],
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
