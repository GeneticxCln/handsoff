"""Tests for `ci/desk_retest.py` — ONE command for §10's automated half.

The driver is what a person runs to close ACCEPTANCE §10's mechanical items: it
plants probes, restarts the unit, reads the journal and the doctor's line, and
prints the block that gets pasted under the items. A driver nobody pins is a
driver that can lie politely, and this one already did — it read the state
line's clauses from the SECOND on, so the scratch count silently disappeared
and three checks accused a truthful line. Its own `--simulate` rehearsal caught
that; the reader is pinned here.

Three things are guarded: the interpreters that decide what the journal and the
line MEAN (`sweep_entries`, `parse_state_line`, `wake_line_ok`), the evidence
block a person pastes (its marks, its reasons, and the OWED line naming what is
still a human's), and the CLI behaviour (`--only`, `--dry-run`, litter) — and,
added when B2's first by-hand attempt found the reply already gone, what the
run itself deletes. The desk-only checks (B1's real SIGKILL, A5's arming warning) cannot run here, so
their WIRING is pinned by shape instead — the same split the driver itself
makes when it reports them as `not run here`. The real back end's own
constraints are pinned the same way, because the first live run of the driver
found them the hard way: systemd counts a start per restart and the unit's
`StartLimitBurst=5`/`StartLimitIntervalSec=120` refused the sixth, leaving the
bubble `failed` for ~3 minutes while two checks graded a CLIENT-side `doctor`
report as the bubble's own.
"""
from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from conftest import _load as _load_module, sandbox_env

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def D():
    """`ci/desk_retest.py`, through the suite's one (sandboxed) loader."""
    return _load_module("desk_retest", HERE / "ci" / "desk_retest.py")


class TestTheJournalReader:
    """`sweep_entries`: the `swept N stale scratch entries … into DEST` line.

    The phrasing is the HOST's (`_log_swept_scratch`). A reader that guessed at
    it could agree with itself while the journal said nothing, so the guard feeds
    it the real sentence and the malformed neighbours it must refuse.
    """

    def test_a_sweep_line_yields_its_count_and_archive(self, D):
        log = (
            "2026-09-25 06:11:02,000 INFO handsoff: starting up\n"
            "2026-09-25 06:11:02,401 INFO handsoff: swept 2 stale scratch "
            "entries from /home/u/.local/state/handsoff into "
            "/home/u/.local/state/handsoff/scratch-quarantine/2026-09-25 "
            "(recoverable for 7 days)\n")
        assert D.sweep_entries(log) == [
            (2, "/home/u/.local/state/handsoff/scratch-quarantine/2026-09-25")]

    def test_the_singular_phrasing_is_the_same_fact(self, D):
        line = ("handsoff: swept 1 stale scratch entry from /s into "
                "/s/scratch-quarantine/2026-09-24 (recoverable for 7 days)")
        assert D.sweep_entries(line) == [(1, "/s/scratch-quarantine/2026-09-24")]

    def test_every_sweep_is_kept_in_the_order_the_journal_holds_them(self, D):
        log = ("handsoff: swept 3 stale scratch entries from /s into /s/a "
               "(recoverable for 7 days)\n"
               "handsoff: swept 1 stale scratch entry from /s into /s/b "
               "(recoverable for 7 days)\n")
        assert D.sweep_entries(log) == [(3, "/s/a"), (1, "/s/b")]

    def test_lines_that_only_talk_about_sweeping_are_not_entries(self, D):
        """A `swept`-shaped line with no count must be SKIPPED, not crashed on
        and not counted as zero — counting it as zero would let C2 pass on a
        journal that never recorded the reclaim."""
        assert D.sweep_entries("nothing was swept this time\n") == []
        assert D.sweep_entries("handsoff: swept  stale scratch\n") == []
        assert D.sweep_entries("") == []


class TestTheStateLineReader:
    """`parse_state_line`: the doctor's `state:` sentence as facts.

    Whatever the reader cannot understand stays None, so a check fails loudly
    instead of comparing nothing with nothing. The clause ORDER is load-bearing
    twice over — the scratch count rides directly behind `state:` (dropped by a
    reader that started at the second clause), and a mature trend clause ends in
    `entries` (swallowed by the size clause when the trend is read last).
    """

    def test_the_scratch_count_is_the_first_clause_not_the_second(self, D):
        line = ("state: 3 scratch-shaped entries present (in grace or from a "
                "live sibling) — the next start sweeps them; last start swept "
                "nothing (just now); 727 B in 6 entries; trend needs a week "
                "(0.0 days recorded)")
        parsed = D.parse_state_line(line)
        assert parsed["scratch_left"] == 3
        assert parsed["swept"] == 0
        assert parsed["size"] == "727 B"
        assert parsed["entries"] == 6
        assert "needs a week" in parsed["trend"]

    def test_a_clean_line_and_a_counted_line_both_read(self, D):
        clean = D.parse_state_line(
            "state: no leaked scratch; last start swept 2 entries just now; "
            "1.4 KB in 7 entries; trend: no readings yet")
        assert clean["scratch_left"] == 0
        assert clean["swept"] == 2
        assert clean["entries"] == 7
        counted = D.parse_state_line(
            "state: 1 scratch-shaped entry present (in grace or from a live "
            "sibling) — the next start sweeps them; sweep not run this "
            "process; 3.2 GB in 39 entries; trend needs a week (0.0 days "
            "recorded)")
        assert counted["scratch_left"] == 1
        assert counted["swept"] is None       # a process that never swept

    def test_a_mature_trend_clause_is_not_swallowed_by_the_size_clause(self, D):
        """`7-day trend +412.0 MB, +3 entries` ENDS in `entries`, so reading the
        trend after the size clause left a truthful line with no trend at all —
        C4 would have cried wolf the first week this desk had a history."""
        line = ("state: no leaked scratch; last start swept nothing (just now); "
                "1.4 KB in 7 entries; 7-day trend +412.0 MB, +3 entries")
        parsed = D.parse_state_line(line)
        assert parsed["trend"] == "7-day trend +412.0 MB, +3 entries"
        assert parsed["trend_delta"] == "+412.0 MB, +3 entries"
        assert parsed["size"] == "1.4 KB"     # the size clause still reads
        assert parsed["entries"] == 7

    def test_an_unreadable_line_reads_as_nothing_not_as_zero(self, D):
        for line in ("", "health: all good",
                     "state: /x could not be read for a hygiene reading"):
            parsed = D.parse_state_line(line)
            assert parsed["scratch_left"] is None, line
            assert parsed["swept"] is None, line
            assert parsed["entries"] is None, line


class TestTheWakeVerdict:
    """`wake_line_ok`: the `wake:` line must name the channel that opens.

    The whole point of that line is that it never claims coverage for a name no
    model covers, so the verdict is a whitelist of the channels the host can
    honestly report — not "has the word spotter in it".
    """

    def test_every_documented_channel_passes(self, D):
        for shape in D.WAKE_LINE_SHAPES:
            ok, why = D.wake_line_ok(f"wake: {shape} (detail)")
            assert ok, (shape, why)

    def test_a_line_that_claims_coverage_it_does_not_have_fails(self, D):
        ok, why = D.wake_line_ok("wake: the custom name is always covered")
        assert not ok
        assert "names none of the known channels" in why

    def test_a_line_that_is_not_a_wake_line_at_all_fails(self, D):
        ok, why = D.wake_line_ok("state: no leaked scratch")
        assert not ok
        assert "no `wake:` line" in why


class TestTheEvidenceBlock:
    """`render`: the block that gets pasted under §10's items.

    Its job is to be legible to a person who did not run it: the mark per item,
    the reason a red one is red, the reason a skipped one was skipped, the count
    — and the OWED line, which is the one part that must never quietly lose an
    item, because an item dropped from it is an item nobody does.
    """

    def _block(self, D, tmp_path):
        desk = D.SimDesk(tmp_path / "root", HERE, "python")
        results = [
            D.Result("B2", "the archived bytes are the bytes").passed(
                "planted sha256 ab12 → scratch-quarantine/2026-09-25/probe"),
            D.Result("C1", "the `state:` line agrees with the disk").failed(
                "the line says 3, the disk has 0", "line: state: 3 entries"),
            D.Result("A5", "the `wake:` line").skipped("needs systemd"),
        ]
        return D.render(results, desk, "2026-09-25 07:00:00")

    def test_the_block_marks_each_verdict_and_says_why(self, D, tmp_path):
        block = self._block(D, tmp_path)
        assert "### §10 automated-half retest — 2026-09-25 07:00:00 " \
               "(simulate back end)" in block
        assert "- ✅ **B2**" in block
        assert "planted sha256 ab12" in block
        assert "- ❌ **C1**" in block
        assert "FAILED: the line says 3, the disk has 0" in block
        assert "- ⏭ **A5**" in block
        assert "not run here: needs systemd" in block
        assert "automated: 1/3 passed, FAILED C1 (not run here: A5)" in block

    def test_the_owed_line_names_exactly_the_human_items(self, D, tmp_path):
        block = self._block(D, tmp_path)
        assert ("OWED at the desk (real voice — no substitute): "
                "A1 A2 A3 A4") in block
        assert D.OWED_HUMAN == ("A1", "A2", "A3", "A4")


class TestTheCli:
    """`main`: the one command's surface — narrowing, rehearsal, exit status.

    `--only` is how a person runs one item without restarting the unit nine
    times, and `--dry-run` is how they see what a run would touch before it
    touches it. Both are cheap to get subtly wrong (a `--only` that silently
    ignores its argument still prints a block), so both are pinned.
    """

    def test_only_narrows_the_run_to_the_named_items(self, D, capsys):
        assert D.main(["--simulate", "--dry-run", "--only", "B2, c4"]) == 0
        out = capsys.readouterr().out
        assert "would run (simulate): B2 C4" in out
        assert "owed to a human afterwards: A1 A2 A3 A4" in out

    def test_an_id_no_check_answers_to_is_a_usage_error(self, D, capsys):
        assert D.main(["--simulate", "--dry-run", "--only", "Z9"]) == 2
        assert "no checks match --only 'Z9'" in capsys.readouterr().err

    def test_dry_run_keeps_no_litter(self, D, capsys):
        """A rehearsal that leaves its own throwaway root behind is the same
        leak the suite sweeps its sandbox homes for."""
        temp = Path(tempfile.gettempdir())
        before = set(temp.glob("desk-retest-*"))
        assert D.main(["--simulate", "--dry-run"]) == 0
        assert set(temp.glob("desk-retest-*")) == before

    def test_simulate_marks_the_desk_only_checks_and_leaves_nothing_behind(
            self, D, capsys, monkeypatch):
        """The whole command, end to end, on the throwaway back end.

        This is §10's automated half as a person runs it, minus systemd: probes
        planted, starts performed, the block printed. What is pinned is the
        contract around it — every automated item holds, A5 and B1 say why they
        cannot run here, the OWED line is intact, and the run leaves no litter:
        neither its throwaway root nor a probe inside it.
        """
        for key, value in sandbox_env().items():
            monkeypatch.setenv(key, value)
        temp = Path(tempfile.gettempdir())
        before = set(temp.glob("desk-retest-*"))
        assert D.main(["--simulate"]) == 0
        out = capsys.readouterr().out
        assert "### §10 automated-half retest" in out
        assert "(simulate back end)" in out
        for item in ("B2", "B3", "B4", "B5", "C1", "C2", "C3", "C4", "C5"):
            assert f"✅ **{item}**" in out, (item, out)
        assert "⏭ **A5**" in out and "⏭ **B1**" in out
        assert "automated: 9/11 passed (not run here: A5 B1)" in out
        assert "OWED at the desk (real voice — no substitute): " \
               "A1 A2 A3 A4" in out
        assert set(temp.glob("desk-retest-*")) == before


class TestTheDeskOnlyWiring:
    """The checks that only exist on the desk, pinned by shape.

    A5's arming warning and B1's real SIGKILL cannot run in the suite — the
    driver reports them as `not run here` for exactly that reason. What CAN be
    pinned is the wiring that makes them meaningful when a person does run them,
    because a refactor can drop it and every guard above still passes.
    """

    def test_the_children_call_what_main_calls_in_the_same_order(self, D, H):
        """The simulate back end is a hand-copy of `main()`'s startup.

        Its child must call `_prepare_runtime()`, then `setup_logging()`, then
        `_log_swept_scratch()` — the order that makes the sweep summary reach
        the journal at all. A copy that drifts lets the rehearsal pass while
        production logs nothing, which is the failure that cost a live desk
        session on 2026-09-25.
        """
        child = D.START_PASS
        main = inspect.getsource(H.main)
        for first, second in (("_prepare_runtime()", "setup_logging()"),
                              ("setup_logging()", "_log_swept_scratch()")):
            assert child.index(first) < child.index(second), (first, second)
            assert main.index(first) < main.index(second), (first, second)

    def test_b1_speaks_before_it_kills_so_a_synthesis_is_really_in_flight(
            self, D):
        """B1's scenario is a bubble killed MID-synthesis: the scratch dir is
        created before the TTS call. Kill a silent bubble and nothing is
        stranded, so the check has to start a spoken reply and let it get there
        before the SIGKILL — otherwise its own timing reads as a regression."""
        body = inspect.getsource(D.check_b1)
        assert "d.speak(" in body and "d.kill9()" in body
        spoken = body.index("d.speak(")
        killed = body.index("d.kill9()")
        assert spoken < killed
        assert "time.sleep(" in body[spoken:killed]

    def test_b1_waits_for_the_wav_so_the_reply_is_playable(self, D):
        """A kill during the voice's LOAD strands an EMPTY dir: the scratch
        dir exists from the first moment, but `tts.wav` only appears once
        synthesis starts writing. Both live runs of 2026-09-25 killed too
        early and archived nothing playable — B2's `aplay` had nothing to
        play, though both runs PASSED. So the kill waits for wav bytes, and
        the check grades the wav itself, exactly as §10's B1 asks ("with its
        `tts.wav` intact")."""
        body = inspect.getsource(D.check_b1)
        waited = body.index("tts.wav")
        killed = body.index("d.kill9()")
        assert waited < killed, "the kill must wait for the wav to have bytes"
        assert body.index("tts.wav", killed) > killed, (
            "the stranded dir's wav is graded AFTER the kill, per §10's B1 "
            "criterion — an empty dir is a fail, not a pass")


class TestWhatTheRunCleansUp:
    """`cleanup()` deletes what the run PLANTED, and nothing else.

    Found live 2026-09-25: the run's archive sweep deleted B1's REAL stranded
    TTS dir at exit — the reply the bubble actually synthesized, which is the
    exact artifact §10's B2 exists to `cp`/`file`/`aplay` by hand. The earlier
    session's `tmptq9yudu_` was already gone when the session owing the
    `aplay` began, and `tmpklue3jp7` vanished between B1's PASS and the `cp`.
    Probes carry the `tmpdeskcheck` prefix; real scratch is named `tmp` plus
    eight random characters (11 chars, `mkdtemp`) and can never carry that
    prefix, so it is the whole boundary. The archived reply's way out is the
    archive's own 7-day TTL, not the retest.
    """

    def test_a_real_stranded_dir_survives_cleanup_its_probes_do_not(
            self, D, tmp_path):
        desk = D.SimDesk(tmp_path / "root", HERE, "python")
        reply = desk.plant_dir("tmpklue3jp7")       # B1's real scratch
        probe = desk.plant_dir(f"{D.PROBE_PREFIX}-fresh")
        desk.cleanup()
        assert reply.is_dir(), (
            "the run deleted B1's real stranded dir — the reply B2's human "
            "half exists to play back")
        assert not probe.exists(), "the run left its own probe behind"

    def test_an_archived_probe_is_removed_a_real_archived_reply_is_not(
            self, D, tmp_path):
        desk = D.SimDesk(tmp_path / "root", HERE, "python")
        day = desk.dated_archive()
        day.mkdir(parents=True, exist_ok=True)
        (day / "tmpklue3jp7").mkdir()               # the real archived reply
        (day / f"{D.PROBE_PREFIX}-recover").mkdir()  # a probe, archived
        desk.created.append(desk.state / "tmpklue3jp7")
        desk.created.append(desk.state / f"{D.PROBE_PREFIX}-recover")
        desk.cleanup()
        assert (day / "tmpklue3jp7").is_dir(), (
            "the archive sweep deleted the real archived reply — the one "
            "artefact a person is owed")
        assert not (day / f"{D.PROBE_PREFIX}-recover").exists()

    def test_both_cleanup_loops_are_bounded_by_the_probe_prefix(self, D):
        """By shape, because the delete-only-probes contract is the guard: a
        revert of either loop must fail here, not on someone's desk."""
        source = inspect.getsource(D.Desk.cleanup)
        assert source.count("startswith(PROBE_PREFIX)") == 2, (
            "the state sweep and the archive sweep are BOTH bounded by "
            "PROBE_PREFIX — one unbounded loop deletes real scratch")


class _FakeClock:
    """A clock the start pacer can be measured against — it is never slept on."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestTheRealBackendsStartPolicy:
    """The unit's own start policy: read, respected, and recoverable.

    Systemd counts a start per restart, and this unit sets
    `StartLimitBurst=5` / `StartLimitIntervalSec=120`. The first live run of
    this driver spent more than that in a couple of minutes, so the sixth start
    was REFUSED (`start-limit-hit`), the unit went `failed` and the bubble
    stayed off the air for ~3 minutes — while two checks went on grading a
    CLIENT-side doctor report as if it were the bubble's (found live
    2026-09-25). Both halves are pinned here, because either one on its own is
    a retest that breaks the desk it came to measure.
    """

    def test_the_pacer_reads_the_units_own_limits(self, D):
        class _Done:
            def __init__(self, out):
                self.stdout = out

        span = D.SystemdDesk._span_s
        # `systemctl show` prints durations as SPANS, not bare numbers — the
        # first parser read `2min` with `isdigit()`, silently fell back to the
        # default, and only matched this unit by luck
        assert span("2min") == 120.0
        assert span("90") == 90.0
        assert span("1min 30s") == 90.0
        assert span("nonsense") is None
        desk = D.SystemdDesk(Path("/tmp/nowhere"), HERE, "python")
        desk._systemctl = lambda *a: _Done("StartLimitBurst=2\n"
                                          "StartLimitIntervalSec=30\n")
        assert desk._start_limits() == (2, 30.0)
        desk._systemctl = lambda *a: _Done("StartLimitBurst=nonsense\n")
        assert desk._start_limits() == (5, 120.0), (
            "a systemd that answers nothing must not silently disable the "
            "pacing")
        desk._systemctl = lambda *a: _Done(
            "StartLimitBurst=6\nStartLimitIntervalSec=2min\n")
        assert desk._start_limits() == (6, 120.0), "a real span must be parsed"

    def test_the_driver_never_outruns_the_start_limit_found_live(
            self, D, monkeypatch):
        """The retest must never be the straw: burst-1 is its reserve."""
        clock = _FakeClock()
        monkeypatch.setattr(D, "time", clock)
        desk = D.SystemdDesk(Path("/tmp/nowhere"), HERE, "python")
        monkeypatch.setattr(desk, "_start_limits", lambda: (5, 120.0))
        journal: list[float] = []
        monkeypatch.setattr(desk, "_journal_start_stamps",
                            lambda interval: list(journal))
        for _ in range(4):
            desk._wait_for_budget()
            journal.append(clock.time())
            desk._starts.append(clock.time())
        assert clock.slept == [], "four starts leave one in reserve; none wait"
        desk._wait_for_budget()          # the reserve: 4 in the window is enough
        assert clock.slept and clock.slept[0] >= 120.0, (
            "the retest itself must never spend the last start of the burst")
        journal.append(clock.time())
        desk._starts.append(clock.time())     # the reserve is spent
        desk._wait_for_budget()
        assert clock.slept and clock.slept[0] >= 120.0
        assert any("start limit" in note for note in desk.notes)
        held = len(clock.slept)
        journal.append(clock.time())
        desk._wait_for_budget()
        assert len(clock.slept) == held, (
            "the window frees rather than blocking that start forever")

    def test_the_pacer_counts_the_journals_starts_not_its_own(
            self, D, monkeypatch):
        """The in-process list cannot know about starts this run did not cause
        — the previous run's tail, a `Restart=always` respawn — and that gap is
        exactly how a paced retest still tripped the limit (found live
        2026-09-25, the run a dead session interrupted)."""
        clock = _FakeClock(now=10_000.0)
        monkeypatch.setattr(D, "time", clock)
        desk = D.SystemdDesk(Path("/tmp/nowhere"), HERE, "python")
        monkeypatch.setattr(desk, "_start_limits", lambda: (5, 120.0))
        desk._starts = [clock.time() - 400.0]      # this run knows one OLD start
        journal = [clock.time() - n for n in (110.0, 80.0, 50.0, 20.0, 5.0)]
        monkeypatch.setattr(desk, "_journal_start_stamps",
                            lambda interval: journal)
        desk._wait_for_budget()
        assert clock.slept and clock.slept[0] > 10.0, (
            "five journal starts in the window must hold the next one, even "
            "though this run caused none of them")
        monkeypatch.setattr(desk, "_journal_start_stamps", lambda interval: [])
        desk._starts = [clock.time() - 10.0] * 5
        clock.slept.clear()
        desk._wait_for_budget()
        assert clock.slept, "without a journal the process's own list gates"

    def test_readiness_is_the_control_socket_not_the_doctor(self, D):
        """`doctor` cannot answer "is the bubble up?": a dead bubble answers it
        from a local report, which is how a slow start read as a failed sweep."""
        start = inspect.getsource(D.SystemdDesk.start_pass)
        assert start.index("_wait_for_budget()") \
            < start.index('_systemctl("restart"')
        assert "service_answers()" in inspect.getsource(D.SystemdDesk._await_ready)
        assert "READY_VERB" in inspect.getsource(D.SystemdDesk.service_answers)
        assert D.SystemdDesk.READY_VERB == "status"

    def test_a_start_that_does_not_come_back_uses_the_documented_recovery(
            self, D):
        """`specs/50-ops.md` §6: a unit that hit the limit stays `failed` until
        `reset-failed` clears the state AND the limiter's counters."""
        start = inspect.getsource(D.SystemdDesk.start_pass)
        assert "reset-failed" in start
        assert 'self._systemctl("start"' in start
        assert start.index("reset-failed") \
            < start.index('self._systemctl("start"')

    def test_the_wait_ends_early_when_the_limiter_has_refused(
            self, D, monkeypatch):
        """Once systemd says `start-limit-hit`, waiting cannot clear it — only
        `reset-failed` can — so `_await_ready` hands back instead of burning
        its whole start timeout on a state nothing but recovery can change."""
        clock = _FakeClock()
        monkeypatch.setattr(D, "time", clock)
        desk = D.SystemdDesk(Path("/tmp/nowhere"), HERE, "python")
        monkeypatch.setattr(desk, "is_active", lambda: False)
        monkeypatch.setattr(desk, "service_answers", lambda: False)
        hits = iter([False, True])
        monkeypatch.setattr(desk, "_start_limit_hit", lambda: next(hits))
        assert desk._await_ready() is False
        assert sum(clock.slept) < 10.0, (
            "a refused start is recovered, not waited out")
        monkeypatch.setattr(desk, "_start_limit_hit", lambda: False)
        clock.slept.clear()
        assert desk._await_ready() is False          # the deadline still works
        assert sum(clock.slept) >= D.SystemdDesk.START_TIMEOUT_S - 2.0

    def test_a_retest_that_finds_the_bubble_down_revives_it_first(
            self, D, monkeypatch):
        """The run a dead session interrupted left the unit `failed` with no
        socket; grading a doctor over that is the one lie this driver exists
        never to tell, so it revives first — held to the budget like any other
        start — and says so."""
        revive = inspect.getsource(D.SystemdDesk.ensure_up)
        assert revive.index("is_active()") < revive.index("reset-failed")
        assert "_wait_for_budget()" in revive
        main = inspect.getsource(D.main)
        assert "desk.ensure_up()" in main
        assert main.index("desk.ensure_up()") < main.index("results = []")

    def test_a_client_side_doctor_report_is_not_graded_as_the_bubble(
            self, D, monkeypatch):
        local = ("(bubble not running — local report)\n\n"
                 "state: no leaked scratch; sweep not run this process; "
                 "1 B in 1 entry\n"
                 "wake: transcript gate ('cypher') — audio spotter untried\n")
        assert D.doctor_is_local(local)
        assert not D.doctor_is_local("deployment: in-sync\n"
                                    "state: no leaked scratch\n")
        desk = D.SystemdDesk(Path("/tmp/nowhere"), HERE, "python")
        monkeypatch.setattr(desk, "_doctor", lambda: local)
        assert desk.state_line() == ""
        assert desk.wake_line() == ""
        monkeypatch.setattr(desk, "_doctor", lambda: local.split("\n\n", 1)[1])
        assert desk.state_line().startswith("state: no leaked scratch")
        assert desk.wake_line().startswith("wake:")

    def test_a5_refuses_to_grade_a_client_line_when_the_bubble_is_down(
            self, D, tmp_path, monkeypatch):
        desk = D.SimDesk(tmp_path / "root", HERE, "python")
        monkeypatch.setattr(D.SimDesk, "mode", "systemd")
        monkeypatch.setattr(desk, "service_answers", lambda: False)
        result = D.check_a5(desk)
        assert result.ok is False
        assert "not answering" in result.why
