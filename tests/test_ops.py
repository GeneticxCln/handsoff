"""Ops and trust tests: deployment reporting, doctor, bounded jobs, and the
installed-copy smoke test (the bubble must work from ~/.local/bin, not only
from the checkout)."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import threading
import types
import time
from collections import deque
from pathlib import Path

import pytest

from conftest import HERE as ROOT, core_module, run_driver, sandbox_env

from core import registry as _core_registry

# Resolved on first use rather than at collection (conftest.core_module).
_core_tools = core_module("tools")

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
        # The daemon probe is the one piece of the selftest that reads the real
        # machine, and with no live socket every later stage is skipped: the
        # refusal results this test is about never run. Reproduced by pointing
        # XDG_RUNTIME_DIR at a directory with no socket. Stage 1 is still
        # exercised — by its own test above — so it is stubbed here.
        monkeypatch.setattr(H.ToolBelt, "_ydotool_socket",
                            staticmethod(lambda: "/tmp/fake-ydotool.sock"))
        monkeypatch.setattr(H.ToolBelt, "_socket_connectable",
                            staticmethod(lambda p: True))
        monkeypatch.setattr(H.ToolBelt, "_terminal_marker",
                            classmethod(lambda cls, w: "foot"))
        monkeypatch.setattr(H.ToolBelt, "_typing_guard",
                            lambda self: foot_win)
        outs = iter([_core_tools.ToolResult("REFUSED: terminal (foot)",
                                           "refused"),
                     _core_tools.ToolResult("REFUSED: terminal (foot)",
                                           "refused")])
        monkeypatch.setattr(H.ToolBelt, "execute",
                            lambda self, name, args: next(outs))
        monkeypatch.setattr(H.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(stdout=""))
        monkeypatch.setattr(H.subprocess, "Popen", fake_popen)
        text = H.run_typing_selftest(belt=belt, timeout=5)
        assert "[PASS] terminal refusal" in text
        assert "type_text" in text and "SKIP" in text   # no editor available
        assert terminated["flag"], "scratch terminal must be terminated"

    def test_selftest_failure_names_the_reason(self, H, monkeypatch):
        """The FAIL row has to carry the REFUSAL, not the success text.

        It read `str(out) if not err else str(out)` — both arms the same
        expression — so a failed type_text landing check reported the text a
        SUCCESS would have produced, and the refusal that caused it was dropped.
        This is the one row a person reads when typing is broken (verified in
        source, 2026-09-19). `err` is a bool, so the reason lives in the tool's
        own text plus the KIND the result carries.
        """
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        editor_win = {"id": 11, "app_id": "org.gnome.TextEditor",
                      "title": "scratch", "is_focused": True}
        foot_win = {"id": 7, "app_id": "foot", "title": "foot",
                    "is_focused": True}
        reason = "ERROR: the focused window is not the scratch editor"

        class FakeMsg:
            returncode = 0
            stdout = json.dumps([foot_win, editor_win])

        def fake_execute(self, name, args):
            if name == "type_text" and str(args.get("text", "")).startswith(
                    "handsoff selftest"):
                return _core_tools.ToolResult(reason, "error")
            return _core_tools.ToolResult("REFUSED: terminal (foot)", "refused")

        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: FakeMsg()))
        monkeypatch.setattr(H.shutil, "which",
                            lambda n: f"/usr/bin/{n}"
                            if n in ("foot", "gnome-text-editor") else None)
        monkeypatch.setattr(H.ToolBelt, "_ydotool_socket",
                            staticmethod(lambda: "/tmp/fake-ydotool.sock"))
        monkeypatch.setattr(H.ToolBelt, "_socket_connectable",
                            staticmethod(lambda p: True))
        monkeypatch.setattr(H.ToolBelt, "_terminal_marker",
                            classmethod(lambda cls, w: "foot"))
        monkeypatch.setattr(H.ToolBelt, "_typing_guard",
                            lambda self: foot_win)
        monkeypatch.setattr(H.ToolBelt, "execute", fake_execute)
        monkeypatch.setattr(H.subprocess, "run",
                            lambda *a, **k: types.SimpleNamespace(stdout=""))
        monkeypatch.setattr(H.subprocess, "Popen",
                            lambda *a, **k: types.SimpleNamespace(
                                poll=lambda: None, terminate=lambda: None))
        text = H.run_typing_selftest(belt=belt, timeout=5)
        row = next(l for l in text.splitlines() if "[FAIL] type_text" in l)
        assert row, text
        assert reason in row, (
            f"the FAIL row must name what refused the typing, got: {row}")
        assert "error" in row, "the result's KIND is part of the diagnosis"

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

    def test_manifest_drives_the_compared_set(self, H, monkeypatch, tmp_path):
        """The compared files come from the installer's manifest, not a second
        hardcoded list.

        core/theme.py was added to the checkout, was never installed, and
        doctor still reported `in-sync` — because the per-file comparison ran
        over _DEPLOY_FILES, which had never heard of it. Driven by the
        manifest, the same deployment is correctly reported as drift.
        """
        checkout = tmp_path / "checkout"
        installed = tmp_path / "home" / ".local" / "bin"
        checkout.mkdir()
        installed.mkdir(parents=True)
        (checkout / ".git").mkdir()
        (checkout / "handsoff.py").write_text("# checkout\n")
        for rel in H._DEPLOY_FILES:
            src = checkout / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("# checkout\n")
            dst = installed / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("# checkout\n")
        # The module exists in the checkout but never reached the deployment.
        (checkout / "core" / "theme.py").write_text("# checkout theme\n")
        manifest = tmp_path / "deployment.json"
        manifest.write_text(json.dumps({"files": {"core/theme.py": {}}}))
        monkeypatch.setattr(H, "SELF_PATH", checkout / "handsoff.py")
        monkeypatch.setattr(H, "HOME", tmp_path / "home")
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", manifest)
        snap = H._deployment_snapshot()
        assert "core/theme.py" in snap["files"], (
            "the manifest's files must be compared, not just the floor list")
        assert snap["files"]["core/theme.py"]["match"] is False
        assert snap["status"] == "installed-drift"

    def test_a_manifestless_install_compares_every_module(self, H, monkeypatch, tmp_path):
        """_DEPLOY_FILES is the top-level floor; the checkout's core set is the
        ceiling a hand-rolled install must be compared against.

        The floor lists eight entries, three of them core modules, while
        install.sh declares thirteen — so an install with no manifest compared
        eight files and reported `in-sync` while half the modules differed.
        Driven from the checkout, every module the tree ships is compared.
        """
        checkout = tmp_path / "checkout"
        installed = tmp_path / "home" / ".local" / "bin"
        checkout.mkdir()
        (checkout / "core").mkdir()
        installed.mkdir(parents=True)
        (checkout / ".git").mkdir()
        (checkout / "handsoff.py").write_text("# checkout\n")
        for rel in H._DEPLOY_FILES:
            src = checkout / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("# checkout\n")
            dst = installed / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("# checkout\n")
        for name in ("audio", "brain", "tools", "bubble", "web", "theme"):
            (checkout / "core" / f"{name}.py").write_text("# checkout\n")
            (installed / "core" / f"{name}.py").write_text("# checkout\n")
        # One module the checkout ships never reached the deployment.
        (checkout / "core" / "voice.py").write_text("# checkout\n")
        monkeypatch.setattr(H, "SELF_PATH", checkout / "handsoff.py")
        monkeypatch.setattr(H, "HOME", tmp_path / "home")
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", tmp_path / "deployment.json")
        snap = H._deployment_snapshot()
        assert snap["manifest"] == {}, "no manifest is the case under test"
        assert "core/voice.py" in snap["files"], (
            "a module only the checkout has must still be compared")
        assert snap["files"]["core/voice.py"]["match"] is False
        assert snap["status"] == "installed-drift"

    def test_the_compared_set_covers_what_the_installer_declares(self, H, monkeypatch, tmp_path):
        """The real install.sh, the real tree: every declared module is compared.

        A manifest names the deployed files, so the declared set is what covers
        the install WITHOUT one. This is that claim, read from the installer's
        own declaration rather than from a second list kept here.
        """
        text = (HERE / "install.sh").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines()
                    if ln.startswith("CORE_REQUIRED="))
        declared = line.split('"')[1].split()
        assert declared, "install.sh's CORE_REQUIRED must be readable"
        monkeypatch.setattr(H, "SELF_PATH", HERE / "handsoff.py")
        monkeypatch.setattr(H, "HOME", tmp_path / "home")
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", tmp_path / "deployment.json")
        snap = H._deployment_snapshot()
        assert snap["manifest"] == {}
        compared = set(snap["files"])
        missing = sorted(f"core/{name}.py" for name in declared
                         if f"core/{name}.py" not in compared)
        assert missing == [], (
            f"a manifest-less install would not compare {missing} — the floor "
            f"covers a manifest-less install, so it must hold every module "
            f"install.sh declares")

    # ---- the two watchers that were invisible in health -------------------

    def test_world_watch_block_ages_and_counts_ticks(self, H, monkeypatch):
        """The wedge detector is the AGE of the last tick: a dead or blocked
        loop stops ticking, which silence alone can never show. Off-by-default
        renders as never-ticked (None), not as a fake stale age."""
        monkeypatch.setattr(H, "_WORLD_WATCH",
                            {"last_tick_ts": 0.0, "last_tick_age_s": None,
                             "ticks": 0, "degraded_ticks": 0,
                             "last_degraded_ts": 0.0, "last_error": ""})
        fresh = H._world_watch_health()
        assert fresh["last_tick_age_s"] is None and fresh["ticks"] == 0
        H._world_watch_record_tick(False)
        H._world_watch_record_tick(True, "ddg timed out")
        block = H._world_watch_health()
        assert block["ticks"] == 2 and block["degraded_ticks"] == 1
        assert block["last_tick_age_s"] is not None
        assert block["last_tick_age_s"] < 5
        assert "ddg timed out" in block["last_error"]

    def test_world_tick_off_renders_never_ticked(self, H, monkeypatch):
        """A poller that is opted out is not wedged — it is off. The tick
        returns without recording, so health reads None, not a stale age."""
        monkeypatch.setattr(H, "_WORLD_WATCH",
                            {"last_tick_ts": 0.0, "last_tick_age_s": None,
                             "ticks": 0, "degraded_ticks": 0,
                             "last_degraded_ts": 0.0, "last_error": ""})
        monkeypatch.setattr(H, "_setting_flag",
                            lambda key, default, repair=False:
                            key != "world_warnings")
        a = H.Assistant.__new__(H.Assistant)
        a._world_last_announce = 0.0
        a._world_tick()
        assert H._world_watch_health()["ticks"] == 0

    def test_world_tick_degraded_records_and_still_announces(self, H,
                                                             monkeypatch):
        """A degraded fetch (network down) is recorded AND the urgent path
        still works: the tracker must not change the tick's behavior."""
        monkeypatch.setattr(H, "_WORLD_WATCH",
                            {"last_tick_ts": 0.0, "last_tick_age_s": None,
                             "ticks": 0, "degraded_ticks": 0,
                             "last_degraded_ts": 0.0, "last_error": ""})
        monkeypatch.setattr(H, "_setting_flag",
                            lambda key, default, repair=False:
                            key == "world_warnings")
        monkeypatch.setattr(H, "SETTINGS",
                            {"world_cooldown_min": 60.0})
        monkeypatch.setattr(H, "_announce_ok", lambda last, cooldown: True)
        monkeypatch.setattr(H, "_world_events",
                            lambda kind, limit: ([], True))
        a = H.Assistant.__new__(H.Assistant)
        a._world_last_announce = 0.0
        a._world_tick()
        block = H._world_watch_health()
        assert block["ticks"] == 1 and block["degraded_ticks"] == 1

    def test_qs_stats_block_shape_and_reset(self, H):
        """The desk client's block: per-tool attempts and last_state, capped
        store, and the reload handshake clears it so a fixed desk does not
        keep reading as broken."""
        import core.tools as _ct
        _ct.qs_stats_reset()
        assert _ct.qs_stats_snapshot() == {}
        _ct.qs_stats_record("quant_space_status", "ok")
        _ct.qs_stats_record("quant_space_status", "ok")
        _ct.qs_stats_record("quant_space_read", "not-granted")
        snap = _ct.qs_stats_snapshot()
        assert snap["quant_space_status"]["attempts"] == 2
        assert snap["quant_space_status"]["last_state"] == "ok"
        assert snap["quant_space_read"]["last_state"] == "not-granted"
        # Cap: a 25th tool drops the oldest entry, never raises.
        for i in range(_ct._QS_STATS_MAX + 2):
            _ct.qs_stats_record(f"quant_space_x{i}", "not-running")
        assert len(_ct.qs_stats_snapshot()) <= _ct._QS_STATS_MAX
        _ct.qs_stats_reset()

    def test_health_carries_both_new_blocks(self, H, monkeypatch):
        """The snapshot contract: `--ptt health` carries world_watch and
        quant_space beside the reader's block — one mirror-signal shape for
        every watcher that is quiet when it works."""
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
        assert "world_watch" in snap and "quant_space" in snap
        assert snap["world_watch"]["last_tick_age_s"] is None or isinstance(
            snap["world_watch"]["last_tick_age_s"], float)
        assert "calls" in snap["quant_space"]

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


class TestCapRefusalReporting:
    """A cap turning work away must be visible AFTER the fact.

    A refusal used to exist only in the string handed back to the model: a run
    that hit its own job cap left no trace in the journal, in the state, or in
    the doctor. The shape that let the cap be OVERSHOT was found by reading the
    source, never by anything the running bubble said — which is the gap these
    tests close. They also pin that the good news is reported: "none" is a
    finding, not a missing line.
    """

    def _belt_at_cap(self, H, monkeypatch, tmp_path):
        """A belt whose job registry is already full, with the refusal record
        redirected at tmp_path so a live state file cannot decide the test."""
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "cap-refusals.json")
        tb, _announced = TestBoundedJobs()._belt(H, monkeypatch)
        for _ in range(_core_tools.BoundedJob.MAX_JOBS):
            slot = tb._jobs.reserve()
            assert slot is not None
            slot.commit(lambda key: None)
        return tb

    def test_a_refusal_reaches_the_journal_not_just_the_reply(
            self, H, monkeypatch, tmp_path, caplog):
        tb = self._belt_at_cap(H, monkeypatch, tmp_path)
        with caplog.at_level("WARNING"):
            out, err = tb.execute("start_command", {"command": "pytest -q"})
        assert err and "job limit reached" in out
        assert "cap refusal: job at 4/4 held" in caplog.text
        assert "start_command 'pytest -q'" in caplog.text
        assert "nothing was created" in caplog.text

    def test_the_refusal_is_persisted_with_the_shape_of_the_moment(
            self, H, monkeypatch, tmp_path):
        """So the diagnosis survives the process that refused."""
        tb = self._belt_at_cap(H, monkeypatch, tmp_path)
        tb.execute("start_command", {"command": "pytest -q"})
        doc = json.loads(H.CAP_EVENTS_FILE.read_text())
        assert doc["count"] == 1 and doc["by_registry"] == {"job": 1}
        event = doc["events"][-1]
        assert event["registry"] == "job"
        assert event["cap"] == _core_tools.BoundedJob.MAX_JOBS
        assert event["held"] == _core_tools.BoundedJob.MAX_JOBS
        assert event["occupants"] == sorted(tb._jobs.keys())
        assert event["detail"].endswith("'pytest -q'")
        assert isinstance(event["at"], (int, float)) and event["at"] > 0

    def test_a_clean_record_reports_nothing_for_the_job_cap(
            self, H, monkeypatch, tmp_path):
        """Full is not the same as refused: an at-cap belt that never had a
        request turned away must not report a refusal."""
        tb = self._belt_at_cap(H, monkeypatch, tmp_path)
        assert tb._jobs.refusals == 0
        assert tb._cap_refusal_note("job") == ""
        assert H._cap_refusal_summary()["count"] == 0
        assert not H.CAP_EVENTS_FILE.exists(), "a non-refusal wrote a record"
        # and the clean status line carries no refusal note
        clean, _ = TestBoundedJobs()._belt(H, monkeypatch)
        out, err = clean.execute("job_status", {})
        assert not err and out == "no background jobs", out

    def test_job_status_reports_the_refusal(self, H, monkeypatch, tmp_path):
        """The tool the user would actually ask is where the answer has to be.

        'no background jobs' is exactly what a run that hit the cap sees, so
        the case that matters most is the one with nothing listed.
        """
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "cap-refusals.json")
        tb, _ = TestBoundedJobs()._belt(H, monkeypatch)
        H._record_cap_refusal({"registry": "job", "cap": 4, "held": 4,
                               "occupants": ["job-1", "job-2", "job-3", "job-4"],
                               "detail": "start_command 'pytest -q'"})
        out, err = tb.execute("job_status", {})
        assert not err
        assert out.startswith("no background jobs")
        assert "background-job cap has refused 1 request" in out
        assert "pytest -q" in out
        assert "A refused call starts nothing." in out

    def test_watcher_refusals_are_recorded_too(self, H, monkeypatch, tmp_path):
        """The counting lives in the registry, so the watcher caps cannot be
        the silent ones left behind."""
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "cap-refusals.json")
        tb = H.ToolBelt(on_restart_pending=lambda: None)
        for i in range(4):
            slot = tb._file_watchers.reserve(f"/tmp/watched-{i}", replace=True)
            assert slot is not None
            slot.commit((threading.Event(), None))
        target = tmp_path / "watched.txt"
        target.write_text("hi\n")
        out = tb.watch_file(str(target), "hi")
        assert out.startswith("ERROR") and "maximum of four" in out
        doc = json.loads(H.CAP_EVENTS_FILE.read_text())
        assert doc["by_registry"] == {"watch-file": 1}
        assert "file-watcher cap has refused 1 request" in \
            H._cap_refusal_note("watch-file")

    def test_the_record_is_bounded_but_the_totals_survive(
            self, H, monkeypatch, tmp_path):
        """A refusal storm must not grow the state file without limit, while
        the cumulative count keeps telling the truth."""
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "cap-refusals.json")
        n = H.CAP_EVENTS_MAX + 12
        for i in range(n):
            H._record_cap_refusal({"registry": "job", "cap": 4, "held": 4,
                                   "detail": f"start_command 'cmd-{i}'"})
        doc = json.loads(H.CAP_EVENTS_FILE.read_text())
        assert len(doc["events"]) == H.CAP_EVENTS_MAX
        assert doc["count"] == n and doc["by_registry"] == {"job": n}
        assert doc["events"][-1]["detail"].endswith(f"'cmd-{n - 1}'")
        assert f"refused {n} request" in H._cap_refusal_note("job")

    def test_an_unreadable_record_is_not_an_error(self, H, monkeypatch, tmp_path):
        """Diagnostics must never be able to fail the path they describe."""
        bad = tmp_path / "cap-refusals.json"
        bad.write_text("{not json")
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", bad)
        assert H._cap_refusal_summary() == {
            "count": 0, "by_registry": {}, "last": None}
        assert H._cap_refusal_note() == ""
        H._record_cap_refusal({"registry": "job", "cap": 4, "held": 4})
        assert H._cap_refusal_summary()["count"] == 1   # recovers and rewrites


class TestCapRefusalIsSpoken:
    """A refused request must be SAID, not only written down.

    The journal and the durable record are both things the user has to go and
    read — while the refusal itself happened because something was ASKED for and
    did not happen. So the bubble says it out loud, on the channel job
    completions already use, once per cap and then at most once a cooldown: the
    refusal path is retried by nature (a model re-calling the same tool, a
    health keybind being hammered) and an audio loop is worse than the
    invisibility this replaces.
    """

    def _assistant(self, H, monkeypatch):
        """A minimal Assistant: no Qt, the speech channel stubbed."""
        said: list = []
        a = H.Assistant.__new__(H.Assistant)
        monkeypatch.setattr(a, "_is_closed", lambda: False)
        a._announce_now = said.append
        return a, said

    def test_a_refused_request_is_spoken_out_loud(self, H, monkeypatch):
        a, said = self._assistant(H, monkeypatch)
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4,
                                "count": 1}, "start_command 'pytest -q'")
        assert len(said) == 1, said
        assert said[0].startswith("I couldn't do that")
        assert "background-job" in said[0] and "4 of 4" in said[0]
        assert "Nothing was started" in said[0], said

    def test_the_same_cap_is_not_repeated_inside_the_cooldown(self, H, monkeypatch):
        """Six refusals in a row must not be six sentences."""
        a, said = self._assistant(H, monkeypatch)
        for i in range(1, 7):
            a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4,
                                    "count": i}, "start_command 'x'")
        assert len(said) == 1, said
        # The stamp is what was SAID (count 1), not the latest attempt — which
        # is what makes the next thing heard the delta of the whole storm rather
        # than a repeat of the first sentence.
        assert a._cap_spoken["job"][1] == 1, a._cap_spoken
        monkeypatch.setattr(H, "CAP_ANNOUNCE_COOLDOWN", 0.0)
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4,
                                "count": 7})
        assert "6 more requests turned away" in said[1], said

    def test_a_storm_is_summarised_once_the_cooldown_passes(self, H, monkeypatch):
        monkeypatch.setattr(H, "CAP_ANNOUNCE_COOLDOWN", 0.0)
        a, said = self._assistant(H, monkeypatch)
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4, "count": 1})
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4, "count": 4})
        assert len(said) == 2, said
        assert "still can't" in said[1] and "3 more requests" in said[1]
        # ...and one more request is one request, not "1 requests"
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4, "count": 5})
        assert "1 more request turned away" in said[2], said[2]

    def test_each_cap_speaks_for_itself(self, H, monkeypatch):
        """A full job cap must not silence a wedged diagnostic."""
        a, said = self._assistant(H, monkeypatch)
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4, "count": 1})
        a.announce_cap_refusal({"registry": "watch-file", "cap": 4,
                                "held": 4, "count": 1})
        a.announce_cap_refusal({"registry": "diagnostic", "cap": 1,
                                "held": 1, "count": 1})
        assert len(said) == 3, said
        assert "file-watcher" in said[1] and "diagnostic-worker" in said[2]

    def test_a_closed_bubble_does_not_speak(self, H, monkeypatch):
        a, said = self._assistant(H, monkeypatch)
        monkeypatch.setattr(a, "_is_closed", lambda: True)
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4, "count": 1})
        assert said == []

    def test_junk_reports_never_raise_and_never_speak_nonsense(self, H, monkeypatch):
        a, said = self._assistant(H, monkeypatch)
        for junk in (None, {}, {"registry": ""}, {"cap": "x", "held": None}, "nope"):
            a.announce_cap_refusal(junk)
        assert said == [], "an unidentified refusal was spoken"
        # an unknown registry still says something legible rather than nothing
        a.announce_cap_refusal({"registry": "mystery", "cap": 2, "held": 2,
                                "count": 1})
        assert len(said) == 1 and "mystery" in said[0], said

    def test_a_broken_speaker_is_survivable(self, H, monkeypatch):
        """Announcing must never be a reason a refusal takes another path."""
        a, _said = self._assistant(H, monkeypatch)

        def boom(_text):
            raise RuntimeError("no speaker")

        a._announce_now = boom
        a.announce_cap_refusal({"registry": "job", "cap": 4, "held": 4, "count": 1})

    def test_the_spoken_map_is_bounded(self, H, monkeypatch):
        a, _said = self._assistant(H, monkeypatch)
        for i in range(H.CAP_ANNOUNCE_MAX + 5):
            a.announce_cap_refusal({"registry": f"reg-{i}", "cap": 1,
                                    "held": 1, "count": 1})
        assert len(a._cap_spoken) == H.CAP_ANNOUNCE_MAX
        assert "reg-0" not in a._cap_spoken, "the newest caps were dropped"


class TestTheHostIsOfferedEveryRefusal:
    """The belt only knows a cap turned work away; the host owns the wording."""

    def _belt_at_job_cap(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "cap.json")
        tb, _ = TestBoundedJobs()._belt(H, monkeypatch)
        for _ in range(_core_tools.BoundedJob.MAX_JOBS):
            slot = tb._jobs.reserve()
            assert slot is not None
            slot.commit(lambda key: None)
        return tb

    def test_the_belt_offers_the_refusal_to_the_host(self, H, monkeypatch, tmp_path):
        tb = self._belt_at_job_cap(H, monkeypatch, tmp_path)
        told: list = []
        tb._on_cap_refusal = lambda report, detail: told.append((report, detail))
        out, err = tb.execute("start_command", {"command": "pytest -q"})
        assert err and "job limit reached" in out
        assert len(told) == 1, told
        report, detail = told[0]
        assert report["registry"] == "job"
        assert report["held"] == _core_tools.BoundedJob.MAX_JOBS
        assert "pytest -q" in detail

    def test_a_belt_without_a_host_still_refuses(self, H, monkeypatch, tmp_path):
        """Tests and embedding build bare belts: no hook, same refusal."""
        tb = self._belt_at_job_cap(H, monkeypatch, tmp_path)
        out, err = tb.execute("start_command", {"command": "pytest -q"})
        assert err and "job limit reached" in out

    def test_the_bubble_connects_the_refusal_channel_to_its_belt(self, H, monkeypatch):
        """The seam is only worth anything if the real bubble wires it.

        Every other test here builds its own belt, so a belt handed the hook by
        the APP is what makes a refusal speak in production — and its absence
        would leave the whole suite green.
        """
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        a = H.Assistant()
        assert a._tools._on_cap_refusal == a.announce_cap_refusal, \
            "the bubble's own belt is not connected to the announcement"

    def test_a_broken_host_cannot_change_the_refusal(self, H, monkeypatch, tmp_path):
        tb = self._belt_at_job_cap(H, monkeypatch, tmp_path)

        def boom(_report, _detail):
            raise RuntimeError("host broke")

        tb._on_cap_refusal = boom
        out, err = tb.execute("start_command", {"command": "pytest -q"})
        assert err and "job limit reached" in out


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

    # ---- stop-attribution ledger in doctor + health -----------------------

    def test_doctor_reports_a_named_stopper(self, H, monkeypatch, tmp_path):
        """The whole point: "who stopped the unit?" is answerable from doctor,
        without reading the raw ledger file."""
        ledger = tmp_path / "stop-attribution.jsonl"
        ledger.write_text(json.dumps({
            "ts": "2026-09-22T18:41:43+02:00", "unit": "handsoff.service",
            "callers": [{"pid": 7, "exe": "systemctl",
                         "cmd": "systemctl --user stop handsoff.service",
                         "chain": [{"pid": 6, "exe": "bash",
                                    "cmd": "bash -c systemctl ..."}]}],
            "note": ""}) + "\n", encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        text = H.run_doctor()
        assert "stop attribution: last stop 2026-09-22T18:41:43 by systemctl (bash)" in text, text
        out = H.doctor_json()["stop_attribution"]
        assert out["present"] and out["total"] == 1
        assert out["last"]["callers"][0]["exe"] == "systemctl"
        assert out["last"]["callers"][0]["chain"][0]["exe"] == "bash"

    def test_doctor_reports_an_unattributed_stop(self, H, monkeypatch, tmp_path):
        """callers:[] is a finding, not a failure: it is what a session
        shutdown or a direct D-Bus call looks like — doctor must say so."""
        ledger = tmp_path / "stop-attribution.jsonl"
        ledger.write_text(json.dumps({
            "ts": "2026-09-22T19:00:00+02:00", "unit": "handsoff.service",
            "callers": [], "note": "no invoking process visible"}) + "\n",
            encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        text = H.run_doctor()
        assert "stop attribution: last stop 2026-09-22T19:00:00 had NO visible caller" in text
        assert H.doctor_json()["stop_attribution"]["last"]["unattributed"] is True

    def test_doctor_says_when_the_ledger_is_absent(self, H, monkeypatch, tmp_path):
        """No ledger is a positive finding with a reason ("probe not shipped or
        no stop since"), never a missing line — the cap-refusal rule."""
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", tmp_path / "none.jsonl")
        text = H.run_doctor()
        assert "stop attribution: no ledger" in text
        assert H.doctor_json()["stop_attribution"] == {
            "present": False, "total": 0, "last": None}

    def test_health_snapshot_carries_the_attribution_tail(self, H, monkeypatch,
                                                          tmp_path):
        """--ptt health carries the same tail: exe, cmdline head and the chain,
        capped, so a wedged stop shows without reading the file."""
        ledger = tmp_path / "stop-attribution.jsonl"
        rows = [
            {"ts": f"2026-09-22T19:0{i}:00+02:00", "unit": "handsoff.service",
             "callers": [{"pid": i, "exe": "systemctl",
                          "cmd": "x" * 400, "chain": []}], "note": ""}
            for i in range(5)
        ]
        ledger.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        health = H._stop_attribution_health()
        assert health["present"] and health["total"] == 5
        assert len(health["tail"]) == 3          # capped at the last three
        assert health["last"]["ts"].startswith("2026-09-22T19:04")
        assert len(health["last"]["callers"][0]["cmd"]) == 120   # cmdline head

    def test_a_torn_ledger_line_is_skipped_not_fatal(self, H, monkeypatch,
                                                     tmp_path):
        """Diagnostics must not break on a half-written line: the probe appends
        under systemd, so a torn tail is possible; skip it, count the rest."""
        ledger = tmp_path / "stop-attribution.jsonl"
        good = json.dumps({"ts": "t1", "unit": "handsoff.service",
                           "callers": [], "note": ""})
        ledger.write_text(good + "\n{" + "\n", encoding="utf-8")  # + torn line
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        health = H._stop_attribution_health()
        assert health["total"] == 1 and health["last"]["ts"] == "t1"
        assert "stop attribution: no ledger" not in H.run_doctor()

    # ---- ghost-stop pattern: invisible stops FOLLOWING attributed ones

    @staticmethod
    def _ghost_ledger(tmp_path, rows):
        ledger = tmp_path / "stop-attribution.jsonl"
        ledger.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return ledger

    @staticmethod
    def _attr_row(ts):
        return {"ts": ts, "unit": "handsoff.service",
                "callers": [{"pid": 1, "exe": "systemctl", "cmd": "x",
                             "chain": []}], "note": ""}

    @staticmethod
    def _ghost_row(ts):
        return {"ts": ts, "unit": "handsoff.service",
                "callers": [], "note": "no invoking process visible"}

    def test_ghost_pair_within_the_window_is_flagged(self, H, monkeypatch,
                                                     tmp_path):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       self._ghost_row("2026-09-23T09:20:00+02:00")]))
        text = H.run_doctor()
        assert "stop attribution: PATTERN — 1 invisible stop(s) followed an " \
               "attributed one within 60 min (last 2026-09-23T09:20:00) — " \
               "the invisible-killer shape, not a session shutdown" in text
        g = H.doctor_json()["stop_attribution"]["ghost_pattern"]
        assert g == {"seen": True, "pairs": 1, "window_min": 60,
                     "last_ghost_ts": "2026-09-23T09:20:00+02:00"}

    def test_a_lone_ghost_is_not_a_pattern(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._ghost_row("2026-09-23T09:00:00+02:00")]))
        text = H.run_doctor()
        assert "PATTERN" not in text
        assert "had NO visible caller" in text      # rendered, not flagged
        # the cap_refusals idiom: when the ledger exists the pattern is
        # ALWAYS reported, so `seen: false` is a finding, not an omission
        g = H.doctor_json()["stop_attribution"]["ghost_pattern"]
        assert g["seen"] is False and g["pairs"] == 0

    def test_ghost_after_the_window_is_not_a_pattern(self, H, monkeypatch,
                                                     tmp_path):
        """61 minutes after the attributed stop, it's a different event."""
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       self._ghost_row("2026-09-23T10:01:00+02:00")]))
        assert "PATTERN" not in H.run_doctor()

    def test_ghost_before_the_attributed_stop_is_not_a_pattern(self, H,
                                                               monkeypatch,
                                                               tmp_path):
        """Only a stop that FOLLOWS an attributed one counts — an old ghost
        must not accuse a later attributed stop (negative gap)."""
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._ghost_row("2026-09-23T09:00:00+02:00"),
                       self._attr_row("2026-09-23T10:00:00+02:00")]))
        assert "PATTERN" not in H.run_doctor()

    def test_pattern_flags_even_when_the_last_stop_was_attributed(self, H,
                                                                  monkeypatch,
                                                                  tmp_path):
        """A killer that alternates attributed and invisible stops must not
        slip out between two clean-looking lines."""
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       self._ghost_row("2026-09-23T09:30:00+02:00"),
                       self._attr_row("2026-09-23T09:50:00+02:00")]))
        text = H.run_doctor()
        assert "PATTERN — 1 invisible stop(s)" in text
        assert "by systemctl" in text               # the last-stop line too

    def test_two_pairs_count_two_and_name_the_latest(self, H, monkeypatch,
                                                     tmp_path):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       self._ghost_row("2026-09-23T09:10:00+02:00"),
                       self._attr_row("2026-09-23T09:30:00+02:00"),
                       self._ghost_row("2026-09-23T09:40:00+02:00")]))
        assert "PATTERN — 2 invisible stop(s)" in H.run_doctor()
        g = H.doctor_json()["stop_attribution"]["ghost_pattern"]
        assert g["pairs"] == 2
        assert g["last_ghost_ts"] == "2026-09-23T09:40:00+02:00"

    def test_unparseable_ts_never_accuses(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       {"ts": "garbage", "unit": "handsoff.service",
                        "callers": [], "note": ""}]))
        assert "PATTERN" not in H.run_doctor()

    def test_ghost_pattern_absent_when_no_ledger(self, H, monkeypatch,
                                                 tmp_path):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", tmp_path / "none")
        health = H._stop_attribution_health()
        assert health == {"present": False, "total": 0, "last": None}
        assert "PATTERN" not in H.run_doctor()

    # ---- shutdown exemption: the session's own sweep is never the pattern

    def test_shutdown_annotated_ghost_is_exempt_from_the_pattern(self, H,
                                                                 monkeypatch,
                                                                 tmp_path):
        """The real 21:34:06 shape: deploy restart 21:32, poweroff 21:34.
        The ghost carries shutdown:1 (the probe saw exit.target), so the pair
        must NOT read as the killer pattern."""
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       {"ts": "2026-09-23T09:03:00+02:00",
                        "unit": "handsoff.service", "callers": [],
                        "shutdown": 1, "note": "session-shutdown sweep"}]))
        text = H.run_doctor()
        assert "PATTERN" not in text
        assert "session-shutdown sweep (exit.target) — expected, not an " \
               "anomaly" in text
        g = H.doctor_json()["stop_attribution"]["ghost_pattern"]
        assert g["seen"] is False and g["pairs"] == 0

    def test_shutdown_flag_defaults_false_for_pre_annotation_rows(self, H,
                                                                  monkeypatch,
                                                                  tmp_path):
        """Rows the old probe wrote (no shutdown key) must behave exactly as
        before — the exemption can never resurrect an old ghost as benign
        without evidence."""
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       self._ghost_row("2026-09-23T09:03:00+02:00")]))
        assert "PATTERN" in H.run_doctor()        # the d34ebb5 behavior holds
        h = H._stop_attribution_health()
        assert h["last"]["shutdown"] is False     # shape carried either way

    # ---- the OPEN list: stops the tripwire shipped too late to see

    def test_open_unexplained_stops_are_listed(self, H, monkeypatch, tmp_path):
        """The standing item renders while entries remain: an open mystery
        with no home is how it quietly disappears from doctor. Pinned against
        injected data, not yesterday's — the 18:xx list RESOLVED by autopsy
        (four restart jobs from the injection battle's own step boundaries;
        the verdicts live in the comment beside the now-empty tuple), and a
        future incident re-opens the item by adding one line of data."""
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", (
            {"ts": "2026-09-23T07:00:00+02:00", "detail": "stop, boot 0"},
            {"ts": "2026-09-23T07:05:00+02:00", "detail": "stop, boot 0"},
        ))
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", tmp_path / "none")
        text = H.run_doctor()
        assert "stop attribution: 2 stop(s) remain UNEXPLAINED (newest " \
               "2026-09-23T07:05) — predates the tripwire; listed until " \
               "explained or superseded" in text
        u = H.doctor_json()["unexplained_stops"]
        assert len(u["open"]) == 2 and u["superseded_by"] == ""

    def test_a_recurrence_caught_red_handed_supersedes(self, H, monkeypatch,
                                                       tmp_path):
        """Supersession is strict: the recurrence must RETURN (an
        unattributed ghost after the incidents) AND be caught (an attributed
        catch after that ghost) — then the list is answered by evidence."""
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00"),
                       self._ghost_row("2026-09-23T09:10:00+02:00"),
                       self._attr_row("2026-09-23T09:30:00+02:00")]))
        text = H.run_doctor()
        assert "UNEXPLAINED" not in text
        assert "no unexplained stops — superseded by the attributed catch " \
               "at 2026-09-23T09:30:00" in text
        u = H.doctor_json()["unexplained_stops"]
        assert u["open"] == [] and u["superseded_by"] == "2026-09-23T09:30:00+02:00"

    def test_an_attributed_catch_alone_does_not_supersede(self, H, monkeypatch,
                                                          tmp_path):
        """The naive rule would close the item on day one — the installer's
        own restart is an attributed catch. Only a caught RECURRENCE counts."""
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [self._attr_row("2026-09-23T09:00:00+02:00")]))
        text = H.run_doctor()
        assert "UNEXPLAINED" not in text          # list is empty (data edit)
        assert "superseded by" not in text        # but nothing superseded it
        assert H.doctor_json()["unexplained_stops"]["superseded_by"] == ""

    def test_health_carries_the_same_unexplained_block_as_doctor_json(self, H,
                                                                      monkeypatch,
                                                                      tmp_path):
        """--ptt health and doctor_json read the same reader, so the open-items
        block cannot disagree between the two surfaces — the design note's
        open edge, closed."""
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", (
            {"ts": "2026-09-23T07:00:00+02:00", "detail": "stop, boot 0"},
        ))
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", tmp_path / "none")
        health_json = H.doctor_json()["unexplained_stops"]
        # The snapshot builder and doctor deps call the SAME reader, so the
        # blocks must be equal — that equality IS the parity contract.
        snap_block = H._unexplained_stops_health()
        assert health_json == snap_block == {
            "open": [{"ts": "2026-09-23T07:00:00+02:00",
                      "detail": "stop, boot 0"}],
            "superseded_by": "",
        }

    # ---- the boot-time audit, visible in doctor ---------------------------

    def test_boot_audit_verdict_renders_in_doctor(self, H, monkeypatch):
        """The wiring contract: a verdict stored at boot surfaces in the
        doctor text AND the doctor_json block — no raw-file reading."""
        stored = {"ts": "2026-09-23T13:30:00+02:00", "verdict": "PASS",
                  "summary": "verdict: PASS (6 passed)"}
        monkeypatch.setattr(H, "_boot_stop_audit_health", lambda: dict(stored))
        text = H.run_doctor()
        assert "stop attribution: boot audit PASS at 2026-09-23T13:30" in text
        assert "verdict: PASS (6 passed)" in text
        block = H.doctor_json()["boot_stop_audit"]
        assert block == {"verdict": "PASS", "ts": "2026-09-23T13:30:00+02:00",
                         "summary": "verdict: PASS (6 passed)"}

    def test_boot_audit_fail_is_visible_in_doctor(self, H, monkeypatch):
        """A FAIL at boot is the finding the whole feature exists for — it
        must render with its verdict named, not be softened into prose."""
        stored = {"ts": "2026-09-23T13:30:00+02:00", "verdict": "FAIL",
                  "summary": "verdict: FAIL (1 of 6 checks failed)"}
        monkeypatch.setattr(H, "_boot_stop_audit_health", lambda: dict(stored))
        text = H.run_doctor()
        assert "stop attribution: boot audit FAIL at 2026-09-23T13:30" in text

    def test_no_boot_audit_renders_silence_not_a_claim(self, H, monkeypatch):
        """A host that never adjudicated renders silence — doctor must not
        manufacture a "passed" for an audit that did not happen."""
        monkeypatch.setattr(H, "_boot_stop_audit_health", dict)
        text = H.run_doctor()
        assert "boot audit" not in text
        assert "boot_stop_audit" not in H.doctor_json()

    def test_boot_audit_runner_survives_a_raising_audit(self, H, monkeypatch):
        """The startup runner is best effort: an audit that raises is logged
        and swallowed — the boot must never be delayed behind diagnostics."""
        # The store is process-global and an earlier test's Assistant startup
        # may have populated it with a real verdict; this test owns its state.
        monkeypatch.setattr(H, "_BOOT_AUDIT", {})
        monkeypatch.setattr(H, "_stop_audit_report",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        H._run_boot_stop_audit()          # must not raise
        assert H._boot_stop_audit_health() == {}
        # And the happy path stores a verdict the doctor can read.
        report = "handsoff stop-audit\n  verdict: PASS (6 passed)"
        monkeypatch.setattr(H, "_stop_audit_report", lambda: (report, 0))
        H._run_boot_stop_audit()
        stored = H._boot_stop_audit_health()
        assert stored["verdict"] == "PASS" and "6 passed" in stored["summary"]
        assert stored["ts"]

    # ---- --ptt stop-audit: the post-boot check as one built-in verb

    def _audit(self, H, monkeypatch, tmp_path, rows, *, no_unexplained=()):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, rows))
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", no_unexplained)
        return H._stop_audit_report()

    def test_stop_audit_passes_on_a_healthy_ledger(self, H, monkeypatch,
                                                   tmp_path):
        text, rc = self._audit(H, monkeypatch, tmp_path, [
            self._attr_row("2026-09-23T09:00:00+02:00"),
            self._attr_row("2026-09-23T10:00:00+02:00"),
        ])
        assert rc == 0 and "verdict: PASS" in text
        assert "[FAIL]" not in text and "[WARN]" not in text
        assert "pattern reader" in text and "parity reader" in text

    def test_stop_audit_flags_a_torn_line_as_warn_not_fail(self, H,
                                                           monkeypatch,
                                                           tmp_path):
        ledger = tmp_path / "stop-attribution.jsonl"
        good = json.dumps(self._attr_row("2026-09-23T09:00:00+02:00"))
        ledger.write_text(good + "\n{" + "\n", encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        text, rc = H._stop_audit_report()
        assert rc == 0 and "[WARN] lines" in text and "torn" in text

    def test_stop_audit_reports_the_exemption_delta_on_sweep_ghosts(self, H,
                                                                    monkeypatch,
                                                                    tmp_path):
        """The regression detector: with sweep ghosts present, the audit
        recomputes the pattern with the exemption OFF and reports the delta —
        if the exemption ever breaks (flags lost, rule regressed), pairs
        appear here that doctor would wrongly render."""
        rows = [self._attr_row("2026-09-23T09:00:00+02:00"),
                {"ts": "2026-09-23T09:03:00+02:00",
                 "unit": "handsoff.service", "callers": [],
                 "shutdown": 1, "note": "session-shutdown sweep"}]
        text, rc = self._audit(H, monkeypatch, tmp_path, rows)
        assert rc == 0 and "verdict: PASS" in text
        assert "sweep ghost(s) in window" in text
        assert "pairs with exemption 0 vs without 1" in text

    def test_stop_audit_fails_when_the_pattern_reader_disagrees(self, H,
                                                                monkeypatch,
                                                                tmp_path):
        """A reader whose reported block diverges from a fresh compute is a
        machinery regression, not an environment artifact — FAIL, exit 1."""
        rows = [self._attr_row("2026-09-23T09:00:00+02:00"),
                self._ghost_row("2026-09-23T09:03:00+02:00")]
        ledger = tmp_path / "stop-attribution.jsonl"
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                          encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        # The audit re-parses the ledger into its own row objects, so the
        # only honest seam for the lie is the reported block itself.
        real_health = H._stop_attribution_health

        def lying_health():
            block = dict(real_health())
            block["ghost_pattern"] = {"seen": False, "pairs": 0,
                                      "window_min": 60, "last_ghost_ts": ""}
            return block

        monkeypatch.setattr(H, "_stop_attribution_health", lying_health)
        text, rc = H._stop_audit_report()
        assert rc == 1 and "[FAIL] pattern reader" in text

    def test_stop_audit_on_a_missing_ledger_warns_and_exits_zero(self, H,
                                                                 monkeypatch,
                                                                 tmp_path):
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", tmp_path / "none")
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        text, rc = H._stop_audit_report()
        assert rc == 0 and "no ledger" in text and "[WARN] ledger" in text

    def test_stop_audits_verdict_line_is_not_double_labeled(self, H,
                                                            monkeypatch,
                                                            tmp_path):
        text, _rc = self._audit(H, monkeypatch, tmp_path, [
            self._attr_row("2026-09-23T09:00:00+02:00")])
        assert "verdict: PASS" in text
        assert not any(ln.strip().startswith("[PASS] PASS")
                       for ln in text.splitlines())

    # ---- fault-injection guards: the three injected failure modes ----------

    def test_a_reader_that_lost_its_shutdown_skip_fails_the_audit(self, H,
                                                                  monkeypatch,
                                                                  tmp_path):
        """Fault 3a, now a guard: a reader whose shutdown skip is gone counts
        an annotated sweep as a pattern pair — the false positive this whole
        arc exists to prevent. The independent oracle (expected pairs
        recomputed from the raw rows) must catch it, since the stripped-flags
        delta cannot: the breakage survives re-parsing and lies identically
        both ways."""
        rows = [self._attr_row("2026-09-23T09:00:00+02:00"),
                {"ts": "2026-09-23T09:03:00+02:00",
                 "unit": "handsoff.service", "callers": [],
                 "shutdown": 1, "note": "session-shutdown sweep"}]
        ledger = tmp_path / "stop-attribution.jsonl"
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                          encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())

        def broken_reader(records):
            stripped = [{**r, "shutdown": False} for r in records]
            return H._expected_ghost_pairs(stripped) and {
                "seen": True, "pairs": H._expected_ghost_pairs(stripped),
                "window_min": 60, "last_ghost_ts": ""}

        monkeypatch.setattr(H, "_ghost_stop_pattern", broken_reader)
        text, rc = H._stop_audit_report()
        # The broken reader is self-consistent (its own stripped-flags delta
        # matches), so `pattern reader` passes and the ORACLE catches it.
        assert rc == 1 and "[FAIL] exemption" in text
        assert "independent count 0" in text

    def test_a_reader_that_overcounts_fails_the_exemption_check(self, H,
                                                                monkeypatch,
                                                                tmp_path):
        """Fault 3b, now a guard: the old check bounded pairs from one side
        only (`gained >= 0`), so a reader that lies identically with and
        without flags passed its own delta. Equality with the independent
        count is the contract now — both in the sweep-ghost branch and in
        the no-sweep-ghost branch."""
        rows = [self._attr_row("2026-09-23T09:00:00+02:00"),
                self._ghost_row("2026-09-23T09:03:00+02:00")]
        ledger = tmp_path / "stop-attribution.jsonl"
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                          encoding="utf-8")
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        real = H._ghost_stop_pattern

        def gainy_reader(records):
            out = real(records)
            return {**out, "pairs": out["pairs"] + 1, "seen": True}

        monkeypatch.setattr(H, "_ghost_stop_pattern", gainy_reader)
        text, rc = H._stop_audit_report()
        assert rc == 1 and "[FAIL] exemption" in text
        assert "the raw rows support 1" in text

    def test_the_oracle_agrees_with_the_reader_on_the_documented_rule(self, H,
                                                                      monkeypatch,
                                                                      tmp_path):
        """The oracle is deliberate duplication — this guard is the price of
        that choice: the two implementations must agree on the documented
        rule across the shapes that matter (anchor then ghost, sweep ghost
        exempt, negative gap never counts, unparseable ts never accuses)."""
        shapes = [
            [("attr", "09:00"), ("ghost", "09:03")],        # 1 pair
            [("attr", "09:00"), ("sweep", "09:03")],        # exempt: 0
            [("attr", "09:00"), ("ghost", "08:00")],        # negative gap: 0
            [("ghost", "09:03")],                            # no anchor: 0
            [("attr", "09:00"), ("ghost", "11:03")],        # outside window: 0
        ]

        def row(kind, hm):
            ts = f"2026-09-23T{hm}:00+02:00"
            if kind == "attr":
                return self._attr_row(ts)
            r = self._ghost_row(ts)
            if kind == "sweep":
                r["shutdown"] = 1
                r["note"] = "sweep"
            return r

        for shape in shapes:
            rows = [row(k, hm) for k, hm in shape]
            ledger = tmp_path / "stop-attribution.jsonl"
            ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                              encoding="utf-8")
            monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", ledger)
            monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
            fresh = H._ghost_stop_pattern(rows)
            assert fresh["pairs"] == H._expected_ghost_pairs(rows), shape
            # The audit's VERDICT is a different rule (the unattributed
            # finding below) and has its own guards; this test is about the
            # oracle and the reader agreeing on the pairing rule.

    def test_stop_audit_counts_open_unattributed_stops_as_a_finding(self, H,
                                                                    monkeypatch,
                                                                    tmp_path):
        """The audit verified the MACHINERY (reader, oracle, writer) but a
        ghost riding a passing ledger was silent — machinery checks all pass
        while the finding goes unsaid. The unattributed check closes that:
        a non-shutdown ghost after the anchor with NO attributed catch after
        it FAILs the audit, so a recurring ghost pattern after a boot is
        impossible to miss in the verdict doctor renders."""
        text, rc = self._audit(H, monkeypatch, tmp_path, [
            self._attr_row("2026-09-23T09:00:00+02:00"),
            self._ghost_row("2026-09-23T09:03:00+02:00"),
        ])
        assert rc == 1 and "[FAIL] unattributed" in text
        assert "1 unattributed stop(s) with no attributed catch" in text
        assert "invisible-killer shape is OPEN" in text
        assert "verdict: FAIL" in text

    def test_stop_audit_warns_when_a_catch_supersedes_the_ghost(self, H,
                                                                monkeypatch,
                                                                tmp_path):
        """A ghost the tripwire later caught red-handed is ANSWERED — the
        house rule keeps it visible (a refusal is never only in the reply)
        as a WARN with both timestamps, without failing a boot whose
        machinery worked."""
        text, rc = self._audit(H, monkeypatch, tmp_path, [
            self._attr_row("2026-09-23T09:00:00+02:00"),
            self._ghost_row("2026-09-23T09:03:00+02:00"),
            self._attr_row("2026-09-23T09:05:00+02:00"),
        ])
        assert rc == 0 and "[WARN] unattributed" in text
        assert "1 unattributed stop(s) in window" in text
        assert "superseded by the attributed catch at 2026-09-23T09:05" in text
        assert "verdict: PASS" in text

    def test_stop_audit_exempts_sweeps_and_history_from_the_finding(self, H,
                                                                    monkeypatch,
                                                                    tmp_path):
        """Shutdown-annotated sweeps are invisible by design and rows at or
        below the anchor ARE the anchor's history — neither is new evidence,
        so a ledger of sweeps plus pre-anchor ghosts reads clean."""
        sweep = self._ghost_row("2026-09-23T09:03:00+02:00")
        sweep["shutdown"] = 1
        sweep["note"] = "session-shutdown sweep"
        history = self._ghost_row("2026-09-22T12:00:00+02:00")  # before anchor
        text, rc = self._audit(H, monkeypatch, tmp_path, [
            history,
            self._attr_row("2026-09-23T09:00:00+02:00"),
            sweep,
            self._attr_row("2026-09-23T10:00:00+02:00"),
        ])
        assert rc == 0 and "verdict: PASS" in text
        assert "no unattributed stop after the anchor" in text
        assert "[FAIL]" not in text

    def test_a_shutdown_ghost_does_not_start_supersession(self, H,
                                                          monkeypatch,
                                                          tmp_path):
        """An annotated sweep ghost is explained by its flag — it is not the
        mystery returning, so it must not arm the supersession sequence."""
        monkeypatch.setattr(H, "_OPEN_UNEXPLAINED_STOPS", ())
        monkeypatch.setattr(H, "STOP_ATTRIBUTION_FILE", self._ghost_ledger(
            tmp_path, [{"ts": "2026-09-23T09:10:00+02:00",
                        "unit": "handsoff.service", "callers": [],
                        "shutdown": 1, "note": "sweep"},
                       self._attr_row("2026-09-23T09:30:00+02:00")]))
        assert H.doctor_json()["unexplained_stops"]["superseded_by"] == ""

    def test_doctor_json_shape(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "none.json")
        d = H.doctor_json()
        assert "deployment" in d and "restart_script" in d
        assert set(d["systemd_unit"]) == {"present", "auto_restart"}
        assert d["cap_refusals"] == {"count": 0, "by_registry": {}, "last": None}

    def test_a_clean_bubble_says_so_about_cap_refusals(self, H, monkeypatch,
                                                       tmp_path):
        """'none' is a positive finding: a missing line would be
        indistinguishable from a doctor that stopped reporting them."""
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "none.json")
        assert "cap refusals: none recorded" in H.run_doctor()

    def test_the_doctor_reports_a_recorded_refusal(self, H, monkeypatch,
                                                   tmp_path):
        monkeypatch.setattr(H, "CAP_EVENTS_FILE", tmp_path / "cap-refusals.json")
        H._record_cap_refusal({"registry": "job", "cap": 4, "held": 4,
                               "occupants": ["job-1", "job-2", "job-3", "job-4"],
                               "detail": "start_command 'pytest -q'"})
        text = H.run_doctor()
        assert "cap refusals: 1 recorded (1x background-job)" in text
        assert "pytest -q" in text
        refusal = H.doctor_json()["cap_refusals"]
        assert refusal["count"] == 1 and refusal["by_registry"] == {"job": 1}
        assert refusal["last"]["detail"].endswith("'pytest -q'")

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


class TestDoctorModuleExtraction:
    """core/doctor.py is the new home of run_doctor/doctor_json. The host
    (handsoff.py) only re-exports them; the module must be importable and
    usable on its own with an explicit deps object — never importing
    handsoff.py itself."""

    def test_core_doctor_imports_without_handsoff(self, monkeypatch):
        """core.doctor must compile + import cleanly even when handsoff is
        not on sys.path or has been removed. The shim must point at a
        module whose only public names are run_doctor + doctor_json."""
        import importlib
        import sys as _sys

        # Drop any cached core.doctor + handsoff shim so the import is honest.
        for mod in list(_sys.modules):
            if mod == "core.doctor" or mod.startswith("handsoff"):
                _sys.modules.pop(mod, None)
        # Strip the repo root from sys.path briefly so a stale handsoff.py
        # cannot satisfy `import handsoff` (the path-handsoff is loaded from
        # for tests still works — this just confirms the extraction does not
        # require it to be importable by name during core.doctor import).
        saved_path = list(_sys.path)
        try:
            from core import doctor
        finally:
            _sys.path[:] = saved_path
        assert callable(doctor.run_doctor)
        assert callable(doctor.doctor_json)

    def test_core_doctor_does_not_import_handsoff(self):
        """Loading the app must NOT be a transitive side effect of
        `from core import doctor`. host-side deps arrive via DI, not globals.

        The app's entry is the CANONICAL name (`core.APP_MODULE_NAME`), not the
        bare `handsoff` this used to check: the bare alias is retired, so
        asserting on it would pass without looking at anything.
        """
        import sys as _sys
        from core import APP_MODULE_NAME
        for mod in list(_sys.modules):
            if mod == "core.doctor" or mod == APP_MODULE_NAME:
                _sys.modules.pop(mod, None)
        from core import doctor  # noqa: F401
        assert APP_MODULE_NAME not in _sys.modules, (
            "core.doctor must not pull in the app; use a DoctorDeps object "
            "for all host-side state")

    def test_legacy_fallback_byte_stable(self, monkeypatch, tmp_path, H):
        """With the hardware module absent and ollama/niri/ydotool probes
        monkeypatched into the legacy fallback path, run_doctor() output
        must be byte-stable against the strings the old monolithic code
        produced. This is the contract `--ptt doctor` and the
        handsoff_doctor tool depend on."""
        # Force the legacy path: pretend hardware.snapshot is absent.
        monkeypatch.setattr(H, "_hardware", None, raising=False)
        # Pin the ollama endpoint/model so the test is independent of host
        # settings.json and env.
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://127.0.0.1:9")
        monkeypatch.setattr(H, "OLLAMA_MODEL", "tinyllama")
        # Legacy ollama path: ollama_available() returns False.
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        # niri probe raises -> "UNAVAILABLE" branch with the bare exception
        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no niri in tests"))))
        # ydotool: pretend it's not installed so the line is exactly the
        # legacy "NOT INSTALLED" string.
        monkeypatch.setattr(H.shutil, "which", lambda n: None)
        # sounddevice: present but reports zero input devices.
        class _FakeSD:
            @staticmethod
            def query_devices():
                return []
        monkeypatch.setattr(H, "sd", _FakeSD())
        # Point restart/systemd/control-sock/crash at empty tmp paths so
        # the "MISSING" / "not created yet" lines are deterministic.
        monkeypatch.setattr(H, "RESTART_SCRIPT", tmp_path / "no-restart")
        monkeypatch.setattr(H, "SYSTEMD_UNIT_FILE", tmp_path / "no-unit.service")
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "no-sock")
        monkeypatch.setattr(H, "CRASH_LOG", tmp_path / "no-crash.log")
        # Make the deployment snapshot independent of the host checkout.
        fake_manifest = tmp_path / "deployment.json"
        fake_manifest.write_text(json.dumps({
            "whisper_revision": "legacy", "whisper_sha256": "legacy",
            "python": "/usr/bin/python3",
        }))
        monkeypatch.setattr(H, "DEPLOYMENT_FILE", fake_manifest)

        text = H.run_doctor()

        # Per-line legacy contract — every line the original code emits on
        # the fallback path. If any of these changes, the legacy doctor
        # output has drifted and every existing test diff will fail.
        assert "brain: OLLAMA UNREACHABLE at http://127.0.0.1:9 — " \
               "`systemctl status ollama`, then `ollama pull tinyllama`" in text
        # The tts line names the engine and its voice (built-in vs a reference
        # clip), because "voice NOT loaded yet" cannot tell a healthy built-in
        # voice from a clip the engine refuses — the question doctor is run to
        # answer when speech is wrong.
        assert (f"tts: {H.TTS_ENGINE} (built-in voice) — model NOT loaded yet; "
                "stt: whisper NOT loaded yet") in text
        assert "mic: NO input devices visible — check the mic is plugged in" in text
        assert "niri IPC: UNAVAILABLE (no niri in tests) — desktop actions will fail" in text
        assert "ydotool: NOT INSTALLED (typing tools will fail)" in text
        assert f"restart script: MISSING at {tmp_path / 'no-restart'} — run install.sh" in text
        assert "systemd unit: not installed (autostart falls back to niri spawn)" in text
        assert "crash log: none (no native crashes recorded)" in text
        assert "control socket: not created yet (bubble not running?)" in text


class TestBoundedJobs:
    """start_command / job_status: bounded background jobs with completion
    announcements and a refusal policy identical to run_command."""

    def _belt(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"]}
        tb._tool_times = deque()
        tb._policy = _core_tools.DecisionPolicy(H.SETTINGS)
        tb._pending_confirm = _core_registry.Offer("confirm")
        tb._confirm_running = None
        tb._jobs = _core_registry.BoundedRegistry(
            "job", _core_tools.BoundedJob.MAX_JOBS)
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

    def test_completion_is_announced_exactly_once_under_concurrent_polls(
            self, H, monkeypatch):
        """A finished job must be announced once, however many polls race.

        `if not job._announced: job._announced = True` is a check-then-set with
        no lock. A spoken turn and a hands-free turn can overlap, so both polls
        could see False and announce the same finished job twice. The window is
        only a couple of bytecodes, so the claim is asserted through the lock
        rather than by hoping a stress test lands in it.
        """
        tb, announced = self._belt(H, monkeypatch)
        out, err = tb.execute("start_command", {"command": "echo once-only"})
        assert not err, out
        jid = next(iter(tb._jobs))
        job = tb._jobs[jid]
        deadline = time.time() + 10
        while time.time() < deadline and not job.poll()[1]:
            time.sleep(0.05)
        job._announce_lock.acquire()
        try:
            t = threading.Thread(
                target=lambda: tb.execute("job_status", {"job_id": jid}))
            t.start()
            time.sleep(0.2)
            assert announced == [], "announced without holding the claim lock"
        finally:
            job._announce_lock.release()
        t.join(3.0)
        assert len(announced) == 1, announced
        assert job.claim_announcement() is False, "claimed twice"

    def test_announcement_claim_is_exclusive(self, H, monkeypatch):
        """Many concurrent claims: exactly one winner."""
        tb, _ = self._belt(H, monkeypatch)
        tb.execute("start_command", {"command": "echo claim"})
        job = tb._jobs[next(iter(tb._jobs))]
        winners: list[int] = []
        guard = threading.Lock()
        barrier = threading.Barrier(8)

        def _claim():
            barrier.wait()
            if job.claim_announcement():
                with guard:
                    winners.append(1)

        threads = [threading.Thread(target=_claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3.0)
        assert sum(winners) == 1, winners

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
        for _ in range(_core_tools.BoundedJob.MAX_JOBS):
            slot = tb._jobs.reserve()
            assert slot is not None
            slot.commit(lambda key: None)
        out, err = tb.execute("start_command", {"command": "echo x"})
        assert err and "job limit reached" in out

    def test_cap_holds_when_calls_overlap(self, H, monkeypatch):
        """Eight overlapping calls against a cap of four: four run, four are
        refused — and the four refusals happen BEFORE the fork.

        The cap used to be checked before the (slow) validate+Popen window with
        the lock released across it, so every caller saw room, every caller
        spawned, and every caller inserted: seven jobs against a cap of four,
        measured with eight threads parked in that window by a barrier. Now the
        slot is reserved before the spawn, so the barrier only admits the four
        callers that actually hold a slot — the other four are refused without
        ever reaching validate or Popen, which `spawned` proves directly.
        """
        tb, _ = self._belt(H, monkeypatch)
        restarts: list = []
        tb._on_restart_pending = lambda: restarts.append(1)
        spawned: list = []
        real_popen = H.subprocess.Popen

        def counting_popen(*a, **kw):
            spawned.append(a)
            return real_popen(*a, **kw)

        monkeypatch.setattr(H.subprocess, "Popen", counting_popen)
        gate = threading.Barrier(_core_tools.BoundedJob.MAX_JOBS)

        def gated(_command):
            gate.wait(timeout=10)
            return (["echo", "x"], "echo", None, True)

        monkeypatch.setattr(tb, "_validate_command", gated)
        results: list[str] = []
        threads = [threading.Thread(
            target=lambda: results.append(tb.start_command("echo x")))
            for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert len(tb._jobs) == _core_tools.BoundedJob.MAX_JOBS, results
            started = [r for r in results if r.startswith("started")]
            assert len(started) == _core_tools.BoundedJob.MAX_JOBS, results
            refused = [r for r in results if "job limit reached" in r]
            assert len(refused) == 8 - _core_tools.BoundedJob.MAX_JOBS, results
            # A job refused at the cap never ran, so it must not leave the
            # bubble believing a restart is in flight.
            assert len(restarts) == _core_tools.BoundedJob.MAX_JOBS, restarts
            assert len(spawned) == _core_tools.BoundedJob.MAX_JOBS, (
                f"spawned {len(spawned)} processes for a cap of "
                f"{_core_tools.BoundedJob.MAX_JOBS}: a refused caller must not fork")
        finally:
            for job in tb._jobs.values():
                job.proc.kill()
                job.proc.wait()

    def test_a_refused_spawn_is_abandoned_before_registration(self, H, monkeypatch):
        """Raising inside the prepare step must give the slot back.

        A reservation that leaked on failure would shrink the cap silently: the
        registry would keep counting a job that does not exist, and after a few
        failed launches the bubble would refuse work while `job_status` lists
        nothing to reap. The context-manager form of the reservation is what
        makes that impossible — leaving the block without committing cancels.
        """
        tb, _ = self._belt(H, monkeypatch)
        monkeypatch.setattr(tb, "_validate_command",
                            lambda _c: (["echo", "x"], "echo", None, False))
        boom = {"n": 0}
        real_popen = H.subprocess.Popen

        def flaky_popen(*a, **kw):
            boom["n"] += 1
            if boom["n"] <= 2:
                raise OSError("no exec for you")
            return real_popen(*a, **kw)

        monkeypatch.setattr(H.subprocess, "Popen", flaky_popen)
        assert "launch failed" in tb.start_command("echo x")
        assert "launch failed" in tb.start_command("echo x")
        assert not tb._jobs, "a failed launch must not hold a slot"
        assert tb._jobs.room(), "the cap shrank after two failed launches"
        out, err = tb.execute("start_command", {"command": "echo x"})
        assert not err, out
        try:
            assert len(tb._jobs) == 1, tb._jobs.keys()
        finally:
            for job in tb._jobs.values():
                job.proc.kill()
                job.proc.wait()

    def test_bounded_job_poll_timeout(self, H):
        proc = subprocess.Popen(["sleep", "60"])
        job = _core_tools.BoundedJob("j", "sleep 60", proc)
        job.started = time.monotonic() - (_core_tools.BoundedJob.MAX_LIFETIME_S + 5)
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
                     "handsoff-stop-probe", "settings_schema.py", "hardware.py"):
            src = HERE / name
            if src.exists():
                (bin_dir / name).write_bytes(src.read_bytes())
        core_dir = bin_dir / "core"
        core_dir.mkdir(exist_ok=True)
        for src in sorted((HERE / "core").glob("*.py")):
            (core_dir / src.name).write_bytes(src.read_bytes())
        (bin_dir / "handsoff-restart").chmod(0o755)
        state = home / "state"
        # sandbox_env: one place knows that a launch needs a throw-away
        # HOME/XDG pair AND the real user-site PYTHONPATH, because redirecting
        # HOME hides the PySide6 installed there.
        env = sandbox_env(home)
        env.update({
            "XDG_STATE_HOME": str(state),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QPA_PLATFORMTHEME": "",
            "NO_AT_BRIDGE": "1",
            "QT_ACCESSIBILITY": "0",
            "OLLAMA_HOST": "http://127.0.0.1:9",
            "HF_HUB_OFFLINE": "1",
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
            # run_driver: the client half of the same sandbox (same HOME as
            # the bubble it is talking to)
            out = run_driver(
                [str(bin_dir / "handsoff.py"), "--ptt", "status"],
                home=home, env_extra=env,
                capture_output=True, text=True, timeout=10,
            )
            assert out.returncode == 0, out.stderr
            assert out.stdout.startswith("state=idle")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class TestInstallerPurgeBackup:
    """Purge must prove its backup is usable before removing user data."""

    @staticmethod
    def _run_purge(tmp_path, tar_script=None):
        home = tmp_path / "home"
        conf = home / ".config" / "handsoff"
        state = home / ".local" / "state" / "handsoff"
        conf.mkdir(parents=True)
        state.mkdir(parents=True)
        (conf / "settings.json").write_text("config")
        (state / "history.json").write_text("state")

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "systemctl").chmod(0o755)
        if tar_script is not None:
            tar = fake_bin / "tar"
            tar.write_text(tar_script)
            tar.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "PATH": str(fake_bin) + os.pathsep + env["PATH"],
        })
        return subprocess.run(
            ["bash", str(HERE / "install.sh"), "--uninstall", "--purge"],
            env=env, capture_output=True, text=True,
        ), home, conf, state

    def test_purge_refuses_to_delete_after_tar_failure(self, tmp_path):
        result, _, conf, state = self._run_purge(
            tmp_path, "#!/bin/sh\nexit 1\n")
        assert result.returncode != 0
        assert conf.exists() and state.exists()
        assert "backup" in result.stderr.lower()

    def test_purge_verifies_secure_unique_backup(self, tmp_path):
        result, home, conf, state = self._run_purge(tmp_path)
        assert result.returncode == 0, result.stderr
        assert not conf.exists() and not state.exists()
        archives = sorted(home.glob("handsoff-backup-*.tar.gz"))
        assert len(archives) == 1
        assert archives[0].stat().st_mode & 0o777 == 0o600
        listing = subprocess.run(
            ["tar", "tzf", str(archives[0])], capture_output=True,
            text=True, check=True,
        ).stdout
        assert ".config/handsoff/settings.json" in listing
        assert ".local/state/handsoff/history.json" in listing

        # A second purge must not overwrite the first day's backup.
        conf.mkdir(parents=True)
        state.mkdir(parents=True)
        (conf / "settings.json").write_text("new config")
        (state / "history.json").write_text("new state")
        result = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--uninstall", "--purge"],
            env={**os.environ, "HOME": str(home),
                 "XDG_STATE_HOME": str(home / ".local" / "state"),
                 "PATH": str(home.parent / "bin") + os.pathsep + os.environ["PATH"]},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert len(list(home.glob("handsoff-backup-*.tar.gz"))) == 2

    def test_uninstall_keeps_a_foreign_core_directory(self, tmp_path):
        """`~/.local/bin` is shared; `core/` is not ours to `rm -rf`.

        The uninstaller removed exactly what the manifest named and then wiped
        the whole directory anyway. Any unrelated package installed under a
        name as plausible as `core` was deleted with it — and the directory is
        never solely ours: the manifest can be missing (an older install), in
        which case nothing named its contents at all.
        """
        home = tmp_path / "home"
        bin_dir = home / ".local" / "bin"
        core = bin_dir / "core"
        core.mkdir(parents=True)
        (core / "__init__.py").write_text("# ours\n")
        (core / "audio.py").write_text("# ours\n")
        (core / "someone_elses_module.py").write_text("# NOT ours\n")
        conf = home / ".config" / "handsoff"
        conf.mkdir(parents=True)
        (conf / "deployment.json").write_text(json.dumps({
            "files": {"handsoff.py": {}, "core/__init__.py": {},
                      "core/audio.py": {}}}))
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "systemctl").chmod(0o755)

        result = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--uninstall"],
            env={**os.environ, "HOME": str(home),
                 "XDG_STATE_HOME": str(home / ".local" / "state"),
                 "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert not (core / "audio.py").exists(), "our manifest-named file must go"
        assert (core / "someone_elses_module.py").exists(), (
            "a file this install never deployed was deleted with the directory")
        assert "kept" in result.stdout and "did not deploy" in result.stdout, (
            f"keeping it must be said, not silent: {result.stdout!r}")

    def test_uninstall_drops_core_only_when_it_is_ours_to_drop(self, tmp_path):
        """With no manifest the fallback removes the floor, then the directory
        only when nothing foreign is left in it."""
        home = tmp_path / "home"
        core = home / ".local" / "bin" / "core"
        core.mkdir(parents=True)
        (core / "audio.py").write_text("# ours\n")
        (core / "theme.py").write_text("# ours\n")
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "systemctl").chmod(0o755)

        result = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--uninstall"],
            env={**os.environ, "HOME": str(home),
                 "XDG_STATE_HOME": str(home / ".local" / "state"),
                 "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert not (core / "audio.py").exists(), "the imported floor must be removed"
        assert not core.exists(), (
            "an empty core/ directory is ours to remove — otherwise uninstall "
            "leaves litter behind")

    def test_purge_refuses_invalid_tar_listing(self, tmp_path):
        result, home, conf, state = self._run_purge(
            tmp_path,
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  czf) printf bad > \"$2\"; exit 0 ;;\n"
            "  tzf) exit 1 ;;\n"
            "esac\n"
            "exit 2\n",
        )
        assert result.returncode != 0
        assert conf.exists() and state.exists()
        assert not list(home.glob("handsoff-backup-*.tar.gz"))

    def test_purge_backs_up_the_xdg_redirected_state_it_deletes(self, tmp_path):
        """--purge deletes the XDG-RESOLVED $STATE_DIR (line 45), so the
        backup archive must name the same tree — the audit found the tar
        hardcoding .local/state/handsoff, which under an XDG-redirected
        HOME archives a wrong-or-absent directory while rm -rf destroys
        the real one. Both spellings must agree, and the archive must
        actually contain the redirected state's files."""
        home = tmp_path / "home"
        xdg_state = home / "xdg-state"            # NOT .local/state
        conf = home / ".config" / "handsoff"
        state = xdg_state / "handsoff"
        conf.mkdir(parents=True)
        state.mkdir(parents=True)
        (conf / "settings.json").write_text("config")
        (state / "history.json").write_text("redirected state")

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "systemctl").chmod(0o755)
        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "XDG_STATE_HOME": str(xdg_state),
            "PATH": str(fake_bin) + os.pathsep + env["PATH"],
        })
        result = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--uninstall", "--purge"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not conf.exists() and not state.exists(), (
            "the redirected state itself must be purged")
        archives = sorted(home.glob("handsoff-backup-*.tar.gz"))
        assert len(archives) == 1, result.stderr
        listing = subprocess.run(
            ["tar", "tzf", str(archives[0])], capture_output=True,
            text=True, check=True,
        ).stdout
        # The archive holds the state purge ACTUALLY deleted — the redirected
        # tree — not the hardcoded default path it never touched.
        assert "xdg-state/handsoff/history.json" in listing, listing
        assert ".local/state/handsoff" not in listing, listing
        assert ".config/handsoff/settings.json" in listing


# ------------------------------- what install.sh provisions, and from where
# install.sh decides five things the app has already decided: the whisper size
# step 5 DOWNLOADS (the bubble loads `settings["whisper_size"]`), the model step
# 8 pulls and judges, the server step 8 probes and fills, the speech repo step 6
# primes, and the app-id the niri rule it writes has to match. Each of those was
# a literal, and the literals drifted — `qwen3:8b` judged while the bubble ran
# `gemma4:12b`, and piper → chatterbox renamed the repo a second copy of the
# name would have kept priming. These helpers RUN the shipped resolvers against
# fixture settings files instead of reading them out of the script, so a guard
# cannot be satisfied by a comment that says the right thing.
_INSTALLER_RESOLVERS = ("_read_setting", "_read_bool_setting", "_app_constant",
                        "_resolve_whisper_size", "_resolve_ollama_endpoint")


def _installer_source() -> str:
    return (HERE / "install.sh").read_text(encoding="utf-8")


def _installer_function(name: str) -> str:
    """The shell function exactly as shipped, sliced by its own closing brace."""
    lines = _installer_source().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines)
                 if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].startswith("}"))
    return "".join(lines[start:end + 1])


def _installer_assignment(name: str) -> str:
    """The literal value of a top-level `NAME="..."` assignment."""
    found = re.search(rf'^{re.escape(name)}="([^"]*)"', _installer_source(), re.M)
    assert found, f"install.sh does not assign {name}"
    return found.group(1)


def _run_resolvers(body: str, home, settings=None, env=None, here=None) -> str:
    """Run `body` with the shipped resolvers sourced and CONF_DIR in a scratch home.

    `set -eu` on purpose: every resolver here is written to answer with a
    default instead of failing, so a non-zero exit or an unset variable is a bug
    in them rather than in the caller. HANDSOFF_ALLOW_REMOTE_OLLAMA is cleared
    unless the test sets it, so the machine running the suite cannot decide what
    these cases assert.
    """
    conf = Path(home) / ".config" / "handsoff"
    conf.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (conf / "settings.json").write_text(json.dumps(settings),
                                            encoding="utf-8")
    lines = ["set -eu",
             f'CONF_DIR="{conf}"',
             f'PYBIN="{sys.executable}"',
             f'HERE="{here or HERE}"']
    for name in ("OLLAMA_DEFAULT", "DEFAULT_MODEL", "DEFAULT_WHISPER_SIZE",
                 "DEFAULT_TTS_REPO", "DEFAULT_APP_ID"):
        lines.append(f'{name}="{_installer_assignment(name)}"')
    lines += [_installer_function(name) for name in _INSTALLER_RESOLVERS]
    lines.append(body)
    child = dict(os.environ)
    child.pop("HANDSOFF_ALLOW_REMOTE_OLLAMA", None)
    child.update(env or {})
    out = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True,
                         text=True, env=child, cwd=str(home))
    assert out.returncode == 0, out.stderr
    return out.stdout


class TestInstallerRehearsal:
    """The installer can exercise deployment without touching the host."""

    def _run(self, tmp_path, extra_env=None):
        rehearsal = tmp_path / "rehearsal"
        sentinel = tmp_path / "real-home"
        sentinel.mkdir()
        env = dict(os.environ)
        env.update({
            "HOME": str(sentinel),
            "XDG_STATE_HOME": str(sentinel / ".local" / "state"),
            "HANDSOFF_REHEARSAL_ROOT": str(rehearsal),
            "HANDSOFF_SKIP_SYSTEM_PKGS": "1",
            "HANDSOFF_NO_OLLAMA_SERVICE": "1",
            "HANDSOFF_PYTHON": sys.executable,
        })
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--rehearsal"],
            env=env, capture_output=True, text=True,
        )
        return result, rehearsal, sentinel

    def test_rehearsal_generates_and_validates_deployment(self, tmp_path):
        result, root, sentinel = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "rehearsal complete" in result.stdout
        assert not any(sentinel.iterdir())

        manifest_path = root / ".config" / "handsoff" / "deployment.json"
        manifest = json.loads(manifest_path.read_text())
        assert set(manifest["files"]) >= {
            "handsoff.py", "settings_schema.py", "hardware.py",
            "core/__init__.py", "core/settings.py", "handsoff-restart",
        }
        assert (root / ".config" / "systemd" / "user" / "handsoff.service").exists()
        snippet = root / ".config" / "handsoff" / "niri-window-rule.kdl"
        assert "window-rule" in snippet.read_text()

    def test_the_model_the_installer_judges_is_the_one_the_app_uses(
            self, tmp_path):
        """Step 8 must judge the CONFIGURED model, not the deployment default.

        The app reads `SETTINGS["model"]` out of settings.json and its unit sets
        no HANDSOFF_MODEL, so on a machine that has been running, the hardcoded
        default names a model NOTHING loads — the installer warned about
        `qwen3:8b` (and would have pulled its several GB on a host without it)
        while the bubble ran `gemma4:12b`. Rehearsal cannot show this, because
        step 8 skips the ollama checks there, so the shipped resolver is
        extracted and RUN against a settings file instead of being read.
        """
        body = 'printf "%s" "$(_read_setting model)"'
        assert _run_resolvers(body, tmp_path,
                              {"model": "gemma4:12b"}) == "gemma4:12b"

        # No settings file, a corrupt one, and a key that is not a string all
        # leave the shipped default in force, and that default is the app's.
        for home, settings in ((tmp_path / "bare", None),
                               (tmp_path / "corrupt", "{ not json"),
                               (tmp_path / "num", {"model": None})):
            if isinstance(settings, str):
                conf = home / ".config" / "handsoff"
                conf.mkdir(parents=True)
                (conf / "settings.json").write_text(settings, encoding="utf-8")
                assert _run_resolvers(body, home) == ""
            else:
                assert _run_resolvers(body, home, settings) == ""

        source = _installer_source()
        assert "${HANDSOFF_MODEL:-$(_read_setting model)}" in source, (
            "the configured model must be the primary source and the named "
            "default only the fallback")

    def test_rehearsal_downloads_the_size_the_app_loads_and_records_it(
            self, tmp_path):
        """The end-to-end half: step 5 says 'small' and the manifest records it.

        Rehearsal skips the ollama checks but not the whisper step, so a
        settings file seeded before the run is the whole experiment: with the
        old literal this line said 'tiny' for a bubble configured to load
        'small', and the manifest then claimed a model the app never asked for.
        """
        conf = tmp_path / "rehearsal" / ".config" / "handsoff"
        conf.mkdir(parents=True)
        (conf / "settings.json").write_text(
            json.dumps({"whisper_size": "small",
                        "ollama_host": "http://box.lan:11434",
                        "allow_remote_ollama": True}), encoding="utf-8")
        result, root, sentinel = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "Downloading whisper 'small' model" in result.stdout, result.stdout
        assert "[8/8] Checking ollama (http://box.lan:11434)" in result.stdout, (
            "step 8 must name the server the app is configured with")
        assert not any(sentinel.iterdir())
        manifest = json.loads(
            (root / ".config" / "handsoff" / "deployment.json").read_text())
        assert manifest["whisper_model"] == "small", (
            "the manifest must record the model that was actually downloaded")
        snippet = (root / ".config" / "handsoff" / "niri-window-rule.kdl").read_text()
        assert "@APP_ID@" not in snippet, (
            "the app-id placeholder must be substituted, not shipped raw")

    def _expected_shipped(self):
        """The set the installer must deliver, derived the way IT derives it.

        Membership is `declared entry points + what git tracks`, not a bare
        glob: a glob delivered whatever sat in the checkout, so the user's
        scratch `test.py` was copied into ~/.local/bin and hashed into the
        deployment manifest — editing it then made `--ptt doctor` call the whole
        installation stale, and a stray name could overwrite a real binary in
        $BIN_DIR. Derived here independently (git, not install.sh) so a new
        module is still covered without editing this test; outside a git work
        tree the installer globs, which is what a tarball install has.
        """
        declared_top = {"handsoff.py", "handsoff-settings.py", "hardware.py",
                        "settings_schema.py"}
        # Mirrors install.sh's CORE_REQUIRED floor: a module listed here is one
        # the project DECLARES it ships, which is also what keeps this test
        # meaningful in a tree where a new file is not committed yet.
        declared_core = {"__init__.py", "registry.py", "settings.py", "audio.py",
                         "brain.py", "tools.py", "doctor.py", "lifecycle.py",
                         "calendar.py", "assistant.py", "bubble.py", "web.py"}
        # A machine without git raises FileNotFoundError here rather than
        # returning non-zero, so the fallback below never applied and the test
        # errored instead of degrading — the mirror-image of the installer's
        # own rule, which globs when git is missing.
        try:
            tracked = subprocess.run(
                ["git", "-C", str(HERE), "ls-files", "--", "*.py"],
                capture_output=True, text=True)
        except OSError:
            tracked = None
        if tracked is None or tracked.returncode != 0:
            return (set(p.name for p in HERE.glob("*.py")),
                    set(p.name for p in (HERE / "core").glob("*.py")))
        rel = [line for line in tracked.stdout.split() if line.endswith(".py")]
        top = {p for p in rel if "/" not in p} | declared_top
        core = {p.split("/", 1)[1] for p in rel if p.startswith("core/")} | declared_core
        return top, core

    def test_staging_directory_cannot_collide_between_two_installs(self):
        """`staged.$$` is predictable, so two installs shared one stage.

        Nothing stops a second install starting while the first runs — the
        bubble's own self-edit restart can race a manual `./install.sh`, and
        both would stage, gate and switch the SAME directory. The staging path
        is therefore created by `mktemp -d`; the releases directory it lives in
        has to exist first, or mktemp fails on a fresh install.
        """
        src = (HERE / "install.sh").read_text(encoding="utf-8")
        assert 'STAGE_DIR="$(mktemp -d' in src, (
            "the staging directory must be unpredictable")
        assert "staged.$$" not in src
        assert '"$CONF_DIR/releases"' in src.split("mktemp -d")[0], (
            "the staging parent must be created before mktemp -d runs")

    def test_rehearsal_deploys_every_module_the_project_owns(self, tmp_path):
        """Regression: core/theme.py was added and never installed.

        Staging, the switch list, the rollback list and the manifest each
        enumerated modules by name, so the settings GUI silently lost wallpaper
        matching — and doctor still said `in-sync`, because the manifest only
        hashed the files it was told about. Driven from the checkout, so any
        module added later is covered without editing this test.
        """
        result, root, _sentinel = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        bin_dir = root / ".local" / "bin"
        manifest = json.loads(
            (root / ".config" / "handsoff" / "deployment.json").read_text())

        expected_top, expected_core = self._expected_shipped()
        assert expected_core, "no core modules expected — test is vacuous"
        deployed_core = sorted(p.name for p in (bin_dir / "core").glob("*.py"))
        assert deployed_core == sorted(expected_core), (
            "every project core module must reach the deployed set")

        # Top level too: the same hand-list lived in seven places here, so a
        # new module beside handsoff.py had seven ways to be forgotten.
        deployed_top = sorted(p.name for p in bin_dir.glob("*.py"))
        assert deployed_top == sorted(expected_top), (
            "every project top-level module must reach the deployed set")
        assert (bin_dir / "handsoff-restart").exists()

        # Files the project does NOT own stay out of the user's PATH entirely: a
        # scratch experiment in the checkout is not the installer's to deliver.
        owned = expected_top | {f"core/{n}" for n in expected_core}
        present = {p.name for p in HERE.glob("*.py")}
        present |= {f"core/{p.name}" for p in (HERE / "core").glob("*.py")}
        for rel in sorted(present - owned):
            assert not (bin_dir / rel).exists(), (
                f"a file the project does not own was delivered: {rel}")
            assert rel not in manifest["files"], (
                f"doctor would track a file the bubble never uses: {rel}")

        # ...and the manifest must hash all of them, or doctor cannot see that
        # one is missing or stale (the core/theme.py bug).
        hashed = sorted(k for k in manifest["files"] if k.startswith("core/"))
        assert hashed == [f"core/{name}" for name in sorted(expected_core)]
        hashed_top = sorted(k for k in manifest["files"] if "/" not in k)
        assert hashed_top == sorted(
            expected_top | {"handsoff-restart", "handsoff-stop-probe"})
        for rel in manifest["files"]:
            entry = manifest["files"][rel]
            assert entry["source_sha256"] == entry["installed_sha256"], rel

    def _uninstall(self, tmp_path, home):
        """Run --uninstall with a stubbed systemctl.

        The real uninstaller calls `systemctl --user`; stubbing it on PATH
        keeps this test from ever reaching the developer's live user bus.
        """
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "systemctl"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "PATH": str(stub_dir) + os.pathsep + os.environ["PATH"],
        })
        return subprocess.run(
            ["bash", str(HERE / "install.sh"), "--uninstall"],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def test_uninstall_removes_what_it_deployed_and_nothing_else(self, tmp_path):
        """--uninstall must remove the deployed set without sweeping the
        shared ~/.local/bin, where unrelated user scripts live.

        The removal list is read back from the deployment manifest (generated
        from the shipped set), so a module added later is removed too, and a
        hand-edited manifest cannot point the uninstaller outside the tree.
        """
        result, root, _sentinel = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        bin_dir = root / ".local" / "bin"
        assert (bin_dir / "handsoff.py").exists()
        stranger = bin_dir / "user_own_script.py"
        stranger.write_text("# not ours\n")

        r = self._uninstall(tmp_path, root)
        assert r.returncode == 0, r.stderr
        assert stranger.exists(), (
            "uninstall must never delete files it did not install")
        assert not (bin_dir / "handsoff.py").exists()
        assert not (bin_dir / "handsoff-restart").exists()
        assert not (bin_dir / "core").exists()
        assert not (bin_dir / "settings_schema.py").exists()

    def test_manifest_write_failure_preserves_previous_file(self, tmp_path):
        root = tmp_path / "rehearsal"
        manifest = root / ".config" / "handsoff" / "deployment.json"
        manifest.parent.mkdir(parents=True)
        original = '{"old": true}\n'
        manifest.write_text(original)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        mv = fake_bin / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            "case \"$3\" in\n"
            "  *deployment.json) exit 1 ;;\n"
            "esac\n"
            "exec /usr/bin/mv \"$@\"\n"
        )
        mv.chmod(0o755)
        result, _root, _sentinel = self._run(
            tmp_path,
            {"HANDSOFF_REHEARSAL_ROOT": str(root),
             "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        )
        assert result.returncode != 0
        assert manifest.read_text() == original

    def test_unit_write_failure_preserves_previous_file(self, tmp_path):
        root = tmp_path / "rehearsal"
        unit = root / ".config" / "systemd" / "user" / "handsoff.service"
        unit.parent.mkdir(parents=True)
        original = "old unit\n"
        unit.write_text(original)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        mv = fake_bin / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            "case \"$3\" in\n"
            "  *handsoff.service) exit 1 ;;\n"
            "esac\n"
            "exec /usr/bin/mv \"$@\"\n"
        )
        mv.chmod(0o755)
        result, _root, _sentinel = self._run(
            tmp_path,
            {"HANDSOFF_REHEARSAL_ROOT": str(root),
             "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        )
        assert result.returncode != 0
        assert unit.read_text() == original


class TestBoundedJobBuffer:
    """The drain buffer keeps the NEWEST output and leaves nothing behind when
    a job dies in a way it cannot reap."""

    @staticmethod
    def _job(H, *args):
        """A real child for the drain buffer.

        The argv is built HERE, in the function that takes its environment from
        the sandbox — which is what keeps the child from inheriting the
        developer's HOME (the suite's own guard enforces that on every
        `[sys.executable, …]` in the tests).
        """
        argv = [sys.executable, *args]
        proc = subprocess.Popen(argv, env=sandbox_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        return _core_tools.BoundedJob("job-buffer", " ".join(argv), proc)

    def test_output_tail_keeps_the_newest_bytes(self, H):
        """The buffer must retain the END of the output, not the beginning.

        Keeping the FIRST MAX_OUTPUT bytes meant "here is the recent output"
        handed back the startup banner and dropped whatever came after it —
        which, for a job long enough to outrun the cap, is the part that
        matters.
        """
        script = ("import sys\n"
                  "sys.stdout.write('HEAD-MARKER\\n')\n"
                  "sys.stdout.write('x' * 1500000)\n"
                  "sys.stdout.write('\\nTAIL-MARKER\\n')\n")
        job = self._job(H, "-c", script)
        deadline = time.time() + 30
        while time.time() < deadline and not job.poll()[1]:
            time.sleep(0.05)
        assert job.poll()[1] is True, "job did not finish"
        job._join_drain(timeout=10.0)
        tail = job.output_tail(4096)
        assert "TAIL-MARKER" in tail, tail[:200]
        assert "HEAD-MARKER" not in tail, tail[:200]
        # still bounded: at most two caps plus one chunk in flight
        assert job._out_len <= _core_tools.BoundedJob.MAX_OUTPUT * 2 + 65536, \
            job._out_len

    def test_unreapable_kill_is_not_reported_as_done(self, H):
        """A SIGKILL that has not reaped must keep the job open.

        Reporting 'timeout-killed' while returncode is still None drops the
        only reaper the job has, leaving a zombie until handsoff exits.
        """
        class StuckProc:
            pid = 4242
            returncode = None
            stdout = None

            def kill(self):
                pass

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("stuck", timeout)

        job = _core_tools.BoundedJob("job-stuck", "sleep 99999", StuckProc())
        job.started -= _core_tools.BoundedJob.MAX_LIFETIME_S + 1
        assert job.poll() == ("running", False)

        class ReapProc(StuckProc):
            def __init__(self):
                self.returncode = None

            def wait(self, timeout=None):
                self.returncode = -9
                return -9

        reaped = _core_tools.BoundedJob("job-killed", "sleep 99999", ReapProc())
        reaped.started -= _core_tools.BoundedJob.MAX_LIFETIME_S + 1
        assert reaped.poll() == ("timeout-killed", True)

    def test_a_blocked_drainer_is_released(self, H):
        """read() returns only when EVERY writer closes the pipe.

        The job starts its own session, so a grandchild that inherited stdout
        keeps the drainer blocked long after the job is gone; closing our end
        ends the thread instead of leaking it for the rest of the session.
        """
        class BlockedPipe:
            def __init__(self):
                self.closed = threading.Event()
                self.release = threading.Event()

            def read(self, n):
                self.release.wait(30)
                return ""                  # EOF once released

            def close(self):
                self.closed.set()
                self.release.set()

        class BlockedProc:
            pid = 7
            returncode = None

            def __init__(self):
                self.stdout = BlockedPipe()

            def kill(self):
                pass

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("blocked", timeout)

        proc = BlockedProc()
        job = _core_tools.BoundedJob("job-blocked", "leaky", proc)
        assert job._drain_thread is not None
        time.sleep(0.2)
        assert job._drain_thread.is_alive(), "drainer should be blocked in read()"
        job._unstick_drain()
        assert proc.stdout.closed.is_set(), "the pipe was never closed"
        assert not job._drain_thread.is_alive(), "drain thread leaked"
        assert job._drain_done.is_set()

    def test_reap_path_releases_the_drainer(self, H, monkeypatch):
        """...and job_status is what calls it."""
        tb, _announced = TestBoundedJobs()._belt(H, monkeypatch)
        calls: list = []
        monkeypatch.setattr(_core_tools.BoundedJob, "_unstick_drain",
                            lambda self: calls.append(self.id))
        out, err = tb.execute("start_command", {"command": "echo wiring"})
        assert not err, out
        jid = next(iter(tb._jobs))
        deadline = time.time() + 10
        while jid in tb._jobs and time.time() < deadline:
            tb.execute("job_status", {"job_id": jid})
            time.sleep(0.05)
        assert jid not in tb._jobs, "job was never reaped"
        assert calls == [jid], calls


class TestInstallerProvisionsWhatTheAppDecided:
    """install.sh must not decide again what settings.json already decided.

    One shape, five values: the whisper size step 5 downloads, the model step 8
    pulls and judges, the server step 8 probes and fills, the speech repo step 6
    primes, and the app-id the niri rule matches. Each was a literal here and
    the app's own value elsewhere, and the literals had already drifted. The
    fallbacks stay — a bare machine has no settings.json to read — so one test
    below holds every one of them against the app's own default.
    """

    def test_the_whisper_size_it_downloads_is_the_size_the_app_loads(
            self, tmp_path):
        body = 'printf "%s" "$(_resolve_whisper_size)"'
        assert _run_resolvers(body, tmp_path,
                              {"whisper_size": "small"}) == "small"
        # The knob still wins, for a machine being provisioned for a size its
        # settings file does not carry yet.
        assert _run_resolvers(body, tmp_path, {"whisper_size": "small"},
                              env={"HANDSOFF_WHISPER": "base"}) == "base"
        # A size the app would refuse is refused HERE, with the list named,
        # instead of reaching faster-whisper as a repo that does not exist.
        out = _run_resolvers(body, tmp_path / "junk", {"whisper_size": "huge"})
        assert out.splitlines()[-1] == _installer_assignment("DEFAULT_WHISPER_SIZE")
        assert "not a size this app accepts" in out, out
        assert "WHISPER_SIZES" in out, out
        # No settings at all: the default, which is the app's own default.
        bare = _run_resolvers(body, tmp_path / "bare")
        assert bare == _installer_assignment("DEFAULT_WHISPER_SIZE"), bare
        # ...and the download step and the manifest both use that resolved
        # value: a manifest naming a model nobody downloaded is the drift the
        # doctor's deployment check then reports forever.
        source = _installer_source()
        assert "WHISPER_SIZE=\"$(_resolve_whisper_size)\"" in source
        assert "Downloading whisper '$WHISPER_SIZE' model" in source
        assert '"whisper_model": "$WHISPER_SIZE"' in source

    def test_step_8_talks_to_the_server_the_app_is_configured_with(self, tmp_path):
        body = ('_resolve_ollama_endpoint' + chr(10)
                + 'printf "%s|%s" "$OLLAMA_HOST_URL" "$OLLAMA_REMOTE"')
        default = _installer_assignment("OLLAMA_DEFAULT")
        # A loopback server on a non-default port is used as written.
        assert _run_resolvers(body, tmp_path,
                              {"ollama_host": "http://127.0.0.1:11500"}) \
            == "http://127.0.0.1:11500|0"
        # A bare host:port gets the same scheme the app adds.
        assert _run_resolvers(body, tmp_path / "bare",
                              {"ollama_host": "box.lan:11434",
                               "allow_remote_ollama": True}) == "http://box.lan:11434|1"
        # A remote brain needs the app's opt-in, and the SETTINGS side of it is
        # strict exactly as the app reads it (`is True`): a hand-edited string
        # is not an opt-in, so provisioning that server would describe a bubble
        # that refuses every request.
        out = _run_resolvers(body, tmp_path / "string",
                             {"ollama_host": "http://box.lan:11434",
                              "allow_remote_ollama": "true"})
        assert out.splitlines()[-1] == f"{default}|0", out
        assert "allow_remote_ollama is off" in out, out
        # ...and the environment token the send guard accepts is the fallback.
        assert _run_resolvers(body, tmp_path / "env",
                              {"ollama_host": "http://box.lan:11434"},
                              env={"HANDSOFF_ALLOW_REMOTE_OLLAMA": "Yes"}) \
            == "http://box.lan:11434|1"
        # The failing case a user without either opt-in is in: the bubble
        # refuses the remote host, so this script checks loopback instead.
        assert _run_resolvers(body, tmp_path / "off",
                              {"ollama_host": "http://box.lan:11434"}) \
            .splitlines()[-1] == f"{default}|0"
        # Wiring: step 8 uses the resolved endpoint, points the CLI at the same
        # server (or `pull` fills one and `show` judges another), and reaches
        # for the LOCAL service only when the endpoint IS local.
        source = _installer_source()
        assert 'curl -s --max-time 2 "$OLLAMA_HOST_URL/api/tags"' in source
        assert 'OLLAMA_HOST="${OLLAMA_HOST_URL#*://}"' in source
        assert 'export OLLAMA_HOST' in source
        step8 = source[source.index("_resolve_ollama_endpoint" + chr(10)
                                    + 'echo "==> [8/8]'):]
        assert step8.index('"$OLLAMA_REMOTE" = "1"') \
            < step8.index("HANDSOFF_NO_OLLAMA_SERVICE") \
            < step8.index("sudo systemctl enable --now ollama"), (
            "the remote branch must come before the branch that starts the "
            "local service")

    def test_the_speech_repo_it_primes_is_the_one_the_app_reads(self, tmp_path):
        from conftest import core_module
        audio = core_module("audio")
        body = 'printf "%s" "$(_app_constant core/audio.py TTS_REPO_ID)"'
        assert _run_resolvers(body, tmp_path) == audio.TTS_REPO_ID
        # The proof that this READS the app rather than carrying a second copy:
        # rename the constant in a copy of the module and the answer follows.
        root = tmp_path / "app"
        (root / "core").mkdir(parents=True)
        original = (HERE / "core" / "audio.py").read_text(encoding="utf-8")
        declared = f'TTS_REPO_ID = "{audio.TTS_REPO_ID}"'
        assert declared in original, "the fixture does not contain the constant"
        (root / "core" / "audio.py").write_text(
            original.replace(declared, 'TTS_REPO_ID = "someone-else/renamed"'),
            encoding="utf-8")
        assert _run_resolvers(body, tmp_path / "mut", here=root) \
            == "someone-else/renamed"
        assert "$(_app_constant core/audio.py TTS_REPO_ID)" in _installer_source()

    def test_the_window_rule_names_the_app_id_the_bubble_sets(self, tmp_path, H):
        body = 'printf "%s" "$(_app_constant handsoff.py APP_NAME)"'
        assert _run_resolvers(body, tmp_path) == H.APP_NAME
        source = _installer_source()
        assert 'match app-id=r#"^@APP_ID@$"#' in source, (
            "the rule is written from the app-id the bubble sets")
        assert "s/@APP_ID@/$APP_ID/" in source
        assert 'match app-id=r#"^handsoff$"#' not in source, (
            "the rule must not carry its own copy of the app-id")

    def test_every_fallback_is_the_apps_own_default(self, H):
        """A fallback that drifts is a machine provisioned for nobody's config."""
        from settings_schema import DEFAULT_SETTINGS, WHISPER_SIZES
        from conftest import core_module
        assert _installer_assignment("DEFAULT_MODEL") == DEFAULT_SETTINGS["model"]
        assert _installer_assignment("DEFAULT_WHISPER_SIZE") \
            == DEFAULT_SETTINGS["whisper_size"]
        assert _installer_assignment("DEFAULT_WHISPER_SIZE") in WHISPER_SIZES
        assert _installer_assignment("OLLAMA_DEFAULT") \
            == DEFAULT_SETTINGS["ollama_host"]
        assert _installer_assignment("DEFAULT_TTS_REPO") \
            == core_module("audio").TTS_REPO_ID
        assert _installer_assignment("DEFAULT_APP_ID") == H.APP_NAME


class TestShippedSetExistsInCheckout:
    """Every file the installer stages BY NAME must exist in the checkout.

    install.sh stages top-level *.py and core/*.py by GLOB and enumerates by
    name only what it must: TOP_REQUIRED, CORE_REQUIRED, TOP_EXECUTABLE, the
    switch pairs — plus handsoff.py's _DEPLOY_FILES floor for a manifest-less
    install. A name missing from the checkout fails a REAL install loudly,
    but a rehearsal fails only for the names the stage itself validates
    (TOP_REQUIRED, CORE_REQUIRED, handsoff-restart, handsoff-stop-probe): a
    missing handsoff-settings.py or settings_schema.py is silently skipped
    by the glob, deploys a half-app, and nothing turns red until someone
    runs the real thing. This guard is the static half — the checkout must
    be able to stage its own declared set, without running the installer.
    (The deletion that motivated it: a lost handsoff-restart sat in the
    working tree for days, because every rehearsal test it broke looked
    unrelated to whoever had deleted it.)
    """

    def _required_names(self) -> set[str]:
        """The union of every by-name shipped set, parsed from their sources.

        SWITCH_FILES_644 may legitimately be empty (the switch fills it from
        the staged set at run time); TOP_REQUIRED and TOP_EXECUTABLE may not.
        """
        names: set[str] = set()
        for var in ("TOP_REQUIRED", "TOP_EXECUTABLE", "SWITCH_FILES_755",
                    "SWITCH_FILES_644"):
            values = _installer_assignment(var)
            if var in ("TOP_REQUIRED", "TOP_EXECUTABLE"):
                assert values, f"install.sh declares {var} as an empty list"
            names.update(values.split())
        core = _installer_assignment("CORE_REQUIRED")
        assert core, "install.sh declares CORE_REQUIRED as an empty list"
        names.update(f"core/{module}.py" for module in core.split())
        # handsoff.py's own floor for a manifest-less install, read from the
        # AST so the literal and this guard cannot drift apart either.
        tree = ast.parse((ROOT / "handsoff.py").read_text(encoding="utf-8"))
        (floor,) = [n for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == "_DEPLOY_FILES"
                            for t in n.targets)]
        names.update(ast.literal_eval(floor.value))
        return names

    def test_every_staged_by_name_file_exists_in_the_checkout(self):
        missing = sorted(name for name in self._required_names()
                         if not (ROOT / name).is_file())
        assert not missing, (
            "files the installer stages by name are missing from the checkout: "
            + ", ".join(missing)
            + " — the glob stages only what exists, so a rehearsal would ship a"
            " half-app (or fail, for the names the stage validates). Restore"
            " them (git restore <name>) or drop the stale name from the list.")

    def test_the_guard_names_a_missing_file(self, tmp_path, monkeypatch):
        """The mutation that proves the guard bites — against a SYNTHETIC

        checkout, not this one. Deleting a tracked checkout file is exactly
        what the suite's checkout-write guard forbids (rightly: this class
        exists because a real deletion hid in the tree), so the failure is
        exercised on a copy of the two SOURCES with ROOT pointed at it: the
        guard must name what is missing, not merely count it. The real
        checkout's true-negative is the test above; `git restore
        handsoff-restart` remains how a real deletion is undone.
        """
        fake = tmp_path
        (fake / "core").mkdir()
        (fake / "install.sh").write_text(_installer_source(), encoding="utf-8")
        (fake / "handsoff.py").write_text(
            (ROOT / "handsoff.py").read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "ROOT", fake)
        with pytest.raises(AssertionError) as reason:
            TestShippedSetExistsInCheckout(
            ).test_every_staged_by_name_file_exists_in_the_checkout()
        for named in ("handsoff-restart", "handsoff-settings.py",
                      "core/tools.py"):
            assert named in str(reason.value), (named, reason.value)
        # ...and handsoff-restart specifically IS a floor the stage validates,
        # so the installer's own loud path exists for it too:
        assert "could not stage handsoff-restart" in _installer_source()
