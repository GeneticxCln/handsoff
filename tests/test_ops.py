"""Ops and trust tests: deployment reporting, doctor, bounded jobs, and the
installed-copy smoke test (the bubble must work from ~/.local/bin, not only
from the checkout)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
import time
from collections import deque
from pathlib import Path

import pytest

from conftest import HERE as ROOT, _user_site

HERE = ROOT   # the repo root


class TestTypingSelftestWiring:
    """--ptt selftest: the hardware typing checks as one local command.

    The live run needs a desktop; here we pin the deterministic parts —
    the report format, the verdict aggregation, the SKIP accounting and
    the CLI wiring — so the command cannot rot silently."""

    def test_selftest_in_ptt_actions_and_usage(self, H):
        assert "selftest" in H.PTT_ACTIONS
        assert "selftest" in H.USAGE

    def test_report_pass_fail_skip(self, H):
        results = [
            {"name": "a", "status": "PASS", "detail": "d1"},
            {"name": "b", "status": "FAIL", "detail": "d2"},
        ]
        text = H._selftest_report(results)
        assert "[PASS] a — d1" in text and "[FAIL] b — d2" in text
        assert "verdict: FAIL (1 of 2 checks failed)" in text
        text = H._selftest_report(results[:1])
        assert "verdict: PASS (1/1 checks)" in text
        text = H._selftest_report([
            {"name": "a", "status": "PASS", "detail": "d"},
            {"name": "c", "status": "SKIP", "detail": "gone"},
        ])
        assert "verdict: PASS (1 passed, 1 skipped)" in text

    def test_selftest_dead_daemon_fails_with_skip_reason(self, H, monkeypatch):
        """No ydotoold: the daemon check FAILs and every later stage is
        skipped with the reason — nothing launched, nothing typed."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.ToolBelt, "_ydotool_socket",
                            classmethod(lambda cls: "/none/ydotool"))
        monkeypatch.setattr(H.ToolBelt, "_socket_connectable",
                            staticmethod(lambda p: False))
        text = H.run_typing_selftest(belt=belt)
        assert "[FAIL] ydotool daemon" in text
        assert "skipped" in text            # later stages say why they ran not
        assert "verdict: FAIL" in text

    def test_selftest_terminal_refusal_and_cleanup(self, H, monkeypatch):
        """With a fake window world: the scratch terminal is recognised,
        both refusal results must PASS, and the process is terminated in the
        finally block."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        terminated = {"flag": False}
        foot_win = {"id": 7, "app_id": "foot", "title": "foot",
                    "is_focused": True}

        class FakeMsg:
            returncode = 0
            stdout = json.dumps([foot_win])

        def fake_popen(*a, **k):
            return types.SimpleNamespace(
                poll=lambda: None,
                terminate=lambda: terminated.update(flag=True))

        monkeypatch.setattr(H.ToolBelt, "_niri_msg", staticmethod(lambda *a, **k: FakeMsg()))
        monkeypatch.setattr(H.shutil, "which",
                            lambda n: "/usr/bin/foot" if n == "foot" else None)
        monkeypatch.setattr(H.ToolBelt, "_terminal_marker",
                            classmethod(lambda cls, w: "foot"))
        monkeypatch.setattr(H.ToolBelt, "_typing_guard",
                            lambda self: foot_win)
        outs = iter([("REFUSED: terminal (foot)", True),
                     ("REFUSED: terminal (foot)", True)])
        monkeypatch.setattr(H.ToolBelt, "execute",
                            lambda self, name, args: next(outs))
        monkeypatch.setattr(H.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(stdout=""))
        monkeypatch.setattr(H.subprocess, "Popen", fake_popen)
        text = H.run_typing_selftest(belt=belt, timeout=5)
        assert "[PASS] terminal refusal" in text
        assert "type_text" in text and "SKIP" in text   # no editor available
        assert terminated["flag"], "scratch terminal must be terminated"

    def test_ptt_selftest_runs_locally_without_bubble(self, H, monkeypatch, capsys):
        """The CLI action works when the bubble is dead (local execution,
        same contract as `settings`)."""
        monkeypatch.setattr(H, "run_typing_selftest",
                            lambda *a, **k: "verdict: PASS (0/0 checks)")
        rc = H.ptt_client(["selftest"])
        assert rc == 0
        assert "verdict" in capsys.readouterr().out


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

    def test_manifest_preserves_optional_whisper_provenance(self, H, monkeypatch,
                                                            tmp_path):
        manifest = tmp_path / "deployment.json"
        manifest.write_text(json.dumps({
            "files": {}, "whisper_model": "tiny", "whisper_revision": "abc123",
            "whisper_sha256": "digest", "python": "/venv/bin/python",
        }))
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", manifest)
        snap = H._deployment_snapshot()
        assert snap["manifest"]["whisper_revision"] == "abc123"
        assert snap["manifest"]["whisper_sha256"] == "digest"
        assert snap["manifest"]["python"] == "/venv/bin/python"

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

    def test_partial_deployment_source_is_drift(self, H, monkeypatch, tmp_path):
        """An installed sibling without a checkout source is not in-sync."""
        checkout = tmp_path / "checkout"
        installed = tmp_path / "home" / ".local" / "bin"
        checkout.mkdir()
        installed.mkdir(parents=True)
        (checkout / ".git").mkdir()
        (checkout / "handsoff.py").write_text("# checkout\n")
        monkeypatch.setattr(H, "SELF_PATH", checkout / "handsoff.py")
        monkeypatch.setattr(H, "HOME", tmp_path / "home")
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", tmp_path / "deployment.json")
        for rel in H._DEPLOY_FILES:
            dst = installed / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("# installed\n")
        # The main source matches; this sibling exists only in the deployment.
        (installed / "handsoff.py").write_text("# checkout\n")
        snap = H._deployment_snapshot()
        assert snap["status"] == "installed-drift"
        assert snap["files"]["hardware.py"]["source_sha256"] is None
        assert snap["files"]["hardware.py"]["match"] is None

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

    def test_report_mentions_whisper_and_python_provenance(self, H, monkeypatch,
                                                            tmp_path):
        manifest = tmp_path / "deployment.json"
        manifest.write_text(json.dumps({
            "whisper_revision": "abc123", "whisper_sha256": "digest",
            "python": "/venv/bin/python",
        }))
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", manifest)
        text = H.run_doctor()
        assert "whisper: revision abc123; sha256 digest" in text
        assert "python: /venv/bin/python (" in text

    def test_report_tolerates_old_manifest(self, H, monkeypatch, tmp_path):
        manifest = tmp_path / "deployment.json"
        manifest.write_text(json.dumps({"files": {}}))
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", manifest)
        text = H.run_doctor()
        assert "whisper: revision unknown; sha256 unknown" in text
        assert "python: " in text

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
        # the sibling modules handsoff.py imports beside itself (schema =
        # single-source defaults, hardware = lazy-imported watch, core/ = the
        # settings package from split step (a))
        for name in ("handsoff.py", "handsoff-settings.py", "handsoff-restart",
                     "settings_schema.py", "hardware.py"):
            src = HERE / name
            if src.exists():
                (bin_dir / name).write_bytes(src.read_bytes())
        core_dir = bin_dir / "core"
        core_dir.mkdir(exist_ok=True)
        for name in ("__init__.py", "settings.py"):
            (core_dir / name).write_bytes((HERE / "core" / name).read_bytes())
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
