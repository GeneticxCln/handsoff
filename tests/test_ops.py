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
                     "settings_schema.py", "hardware.py"):
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
        source = (HERE / "install.sh").read_text(encoding="utf-8")
        body = source[source.index("_configured_model() {"):]
        body = body[:body.index("\n}\n") + 3]
        conf = tmp_path / ".config" / "handsoff"
        conf.mkdir(parents=True)
        script = (f'CONF_DIR={conf!s}\nPYBIN="{sys.executable}"\n{body}\n'
                  'printf "%s" "$(_configured_model)"\n')
        (conf / "settings.json").write_text(json.dumps({"model": "gemma4:12b"}),
                                            encoding="utf-8")
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout == "gemma4:12b", out.stdout

        (conf / "settings.json").unlink()
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True)
        assert out.returncode == 0 and out.stdout == "", (
            f"a missing settings file must leave the default in force: {out}")
        assert "${HANDSOFF_MODEL:-$(_configured_model)}" in source, (
            "the configured model must be the primary source and the named "
            "default only the fallback")

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
        assert hashed_top == sorted(expected_top | {"handsoff-restart"})
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
