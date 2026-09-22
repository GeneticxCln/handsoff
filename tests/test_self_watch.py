"""The agent watching itself.

`core/selfwatch.py` owns *deciding* (liveness + wedge detection, cooldowns);
the Assistant owns *saying* (journal, notify, announce) and owns the probes.
Pinned here: dead-thread detection with the learning baseline, wedge
detection through a frozen probe, cooldown so a recurring alarm is heard
without becoming spam, the never-raises contract of both sampler and
snapshot, and the announcement sentences.
"""
from __future__ import annotations

import contextlib
import sys
import threading
import types

from conftest import HERE as ROOT

sys.path.insert(0, str(ROOT))          # core/ is importable from the checkout

from core import selfwatch as sw


@contextlib.contextmanager
def _held(name: str):
    """A genuinely live watched thread for the duration of a block."""
    ev = threading.Event()
    t = threading.Thread(target=ev.wait, name=name, daemon=True)
    t.start()
    try:
        yield t
    finally:
        ev.set()
        t.join()


class FakeClock:
    """Deterministic monotonic clock the tests drive by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _watcher(sync_fns=None, clock: FakeClock | None = None) -> sw.SelfWatch:
    return sw.SelfWatch(sync_fns=sync_fns, clock=clock or FakeClock())


# -- liveness ---------------------------------------------------------------


class TestThreadLiveness:
    def test_a_watched_thread_present_then_gone_is_reported_dead(self):
        """Dead requires a prior sighting: the sampler may start after its
        components, so the first tick LEARNS the baseline instead of alarming
        on it. Driven with a REAL named thread: it is genuinely alive during
        tick 1 (baseline), genuinely gone by tick 2.
        """
        clock = FakeClock()
        w = _watcher(clock=clock)
        release = threading.Event()
        live = threading.Thread(target=release.wait, name="reminders",
                                daemon=True)
        live.start()
        try:
            w.tick(_Host())                 # tick 1: learns 'reminders'
            assert w.snapshot_health("idle", 0)["watched"]["reminders"][
                "finding"] is None
        finally:
            release.set()
            live.join()
        clock.advance(1)
        w.tick(_Host())                     # tick 2: gone
        snap = w.snapshot_health("idle", 0)
        assert snap["watched"]["reminders"]["alive"] is False
        assert snap["watched"]["reminders"]["finding"] == "dead"

    def test_a_thread_never_seen_gets_no_baseline_finding(self):
        """A component the sampler has never seen is not dead — it is
        unknown, and alarming on startup ordering would be a false alarm
        the user learns to ignore."""
        clock = FakeClock()
        w = _watcher(clock=clock)
        w.tick(_Host())                     # no 'pomodoro' thread exists
        clock.advance(1)
        w.tick(_Host())
        snap = w.snapshot_health("idle", 0)
        assert snap["watched"]["pomodoro"]["finding"] is None
        assert snap["watched"]["pomodoro"]["alive"] is False

    def test_one_shot_threads_are_never_watched(self):
        """One-shot workers (selfheal-tts, ptt-stop, brain-call…) match no
        prefix: the inventory must not flap when they come and go."""
        clock = FakeClock()
        w = _watcher(clock=clock)
        w.tick(_Host())
        with _held("reminders"):
            pass
        clock.advance(1)
        snap = w.tick(_Host())
        assert snap.get("error") is None
        # every watched entry still says alive or baseline-learned, none dead
        assert all(v["finding"] is None for v in snap["watched"].values())


class _Host:
    """A minimal probe host: no probe methods at all."""

    def __init__(self) -> None:
        self.state = "idle"


# -- wedging -----------------------------------------------------------------


class TestWedging:
    """Wedge probes sample only LIVE components — a dead thread's probe is
    unreadable, so each of these holds a real 'reminders' thread open."""

    def test_a_frozen_probe_is_reported_wedged_after_the_threshold(self):
        clock = FakeClock()
        w = _watcher(sync_fns={"reminders": "probe"}, clock=clock)
        host = _ProbedHost()
        with _held("reminders"):
            w.tick(host)                   # baseline
            for _ in range(3):
                clock.advance(sw.WEDGED_AFTER_S / 2)
                host.beat += 1             # healthy: probe keeps changing
                w.tick(host)
            snap = w.snapshot_health("idle", 0)
            assert snap["watched"]["reminders"]["finding"] is None
            w.tick(host)                   # last change now
            clock.advance(sw.WEDGED_AFTER_S + 1)
            w.tick(host)                   # frozen ever since
            snap = w.snapshot_health("idle", 0)
        assert snap["watched"]["reminders"]["finding"] == "wedged"

    def test_a_moving_probe_never_reports_wedged(self):
        clock = FakeClock()
        w = _watcher(sync_fns={"reminders": "probe"}, clock=clock)
        host = _ProbedHost()
        with _held("reminders"):
            for _ in range(10):
                clock.advance(60)
                host.beat += 1
                w.tick(host)
            snap = w.snapshot_health("idle", 0)
        assert snap["watched"]["reminders"]["finding"] is None

    def test_a_frozen_then_moving_probe_clears(self):
        clock = FakeClock()
        w = _watcher(sync_fns={"reminders": "probe"}, clock=clock)
        host = _ProbedHost()
        with _held("reminders"):
            w.tick(host)
            w.tick(host)                   # freeze from here
            clock.advance(sw.WEDGED_AFTER_S + 1)
            w.tick(host)                   # wedged now
            assert w.snapshot_health("idle", 0)["watched"]["reminders"][
                "finding"] == "wedged"
            host.beat += 1                 # recovery
            clock.advance(1)
            w.tick(host)
            snap = w.snapshot_health("idle", 0)
        assert snap["watched"]["reminders"]["finding"] is None

    def test_an_unprobeable_component_is_liveness_watched_only(self):
        """No probe method -> no wedge finding is possible for it; only a
        vanished thread can raise a finding."""
        clock = FakeClock()
        w = _watcher(sync_fns={"reminders": "no_such_method"}, clock=clock)
        host = _Host()
        for _ in range(5):
            clock.advance(sw.WEDGED_AFTER_S * 2)
            w.tick(host)
        snap = w.snapshot_health("idle", 0)
        assert snap["watched"]["reminders"]["finding"] is None

    def test_a_raising_probe_is_treated_as_no_probe(self):
        clock = FakeClock()
        w = _watcher(sync_fns={"reminders": "probe"}, clock=clock)
        host = _RaisingHost()
        for _ in range(5):
            clock.advance(sw.WEDGED_AFTER_S * 2)
            w.tick(host)
        assert w.snapshot_health("idle", 0)["watched"]["reminders"][
            "finding"] is None


class _ProbedHost:
    def __init__(self) -> None:
        self.beat = 0

    def probe(self):
        return self.beat


class _RaisingHost:
    def probe(self):
        raise RuntimeError("boom")


# -- cooldowns and announcements ---------------------------------------------


class TestAnnouncements:
    def test_one_announcement_per_finding_until_cooldown_expires(self):
        clock = FakeClock()
        w = _watcher(clock=clock)
        release = threading.Event()
        live = threading.Thread(target=release.wait, name="reminders",
                                daemon=True)
        live.start()
        w.tick(_Host())                     # baseline
        release.set()
        live.join()
        clock.advance(1)
        w.tick(_Host())                     # dead now
        first = w.pending_announcements()
        assert [f["prefix"] for f in first] == ["reminders"]
        assert first[0]["kind"] == "dead"
        # within the cooldown: nothing new
        clock.advance(10)
        assert w.pending_announcements() == []
        # after the cooldown: the same standing finding re-announces
        clock.advance(sw.ANNOUNCE_COOLDOWN_S)
        again = w.pending_announcements()
        assert [f["prefix"] for f in again] == ["reminders"]

    def test_a_frozen_then_recovered_streak_announces_once_each_way(self):
        clock = FakeClock()
        w = _watcher(sync_fns={"reminders": "probe"}, clock=clock)
        host = _ProbedHost()
        with _held("reminders"):
            w.tick(host)
            w.tick(host)
            clock.advance(sw.WEDGED_AFTER_S + 1)
            w.tick(host)
            assert len(w.pending_announcements()) == 1
            host.beat += 1
            clock.advance(1)
            w.tick(host)                   # recovery
        clock.advance(sw.ANNOUNCE_COOLDOWN_S)
        assert w.pending_announcements() == [], \
            "a recovered component has nothing left to say"

    def test_the_dead_sentence_and_the_wedged_sentence_differ(self):
        dead = sw.announcement_text({"prefix": "reminders", "kind": "dead"})
        wedged = sw.announcement_text({"prefix": "reminders", "kind": "wedged"})
        assert "not running" in dead
        assert "stuck" in wedged
        assert dead != wedged
        # every sentence is speakable: no jargon, no prefixes left in
        for text in (dead, wedged):
            assert "self-watch" not in text
            assert text.strip() == text


# -- the never-raises contract -----------------------------------------------


class TestNeverRaises:
    def test_tick_survives_a_host_that_raises_everywhere(self):
        """The designed answer to a raising probe: `_probe` absorbs it and
        the component degrades to liveness-only. The tick must not raise,
        and the inventory must survive intact."""
        w = _watcher(sync_fns={"reminders": "probe"}, clock=FakeClock())
        snap = w.tick(_ExplodingHost())    # the contract: no raise
        assert snap.get("error") is None
        assert snap["ticks"] == 1
        assert "reminders" in snap["watched"]

    def test_snapshot_survives_a_broken_state(self):
        clock = FakeClock()
        w = _watcher(clock=clock)
        w.tick(_Host())
        w._lock = None                     # sabotage the internals
        out = w.snapshot_health("idle", 0)
        assert "error" in out

    def test_pending_announcements_survives_sabotage(self):
        clock = FakeClock()
        w = _watcher(clock=clock)
        w._clock = None
        assert w.pending_announcements() == []

    def test_the_inventory_tolerates_a_nameless_thread(self):
        clock = FakeClock()
        w = _watcher(clock=clock)
        with _held(""):
            pass
        assert w.tick(_Host()).get("error") is None


class _ExplodingHost:
    def __getattr__(self, name):
        raise RuntimeError("everything is broken")


# -- the app's half ----------------------------------------------------------


class TestTheAppSide:
    def test_health_carries_the_self_watch_section(self, H, monkeypatch):
        """`--ptt health` shows what the sampler sees — and builds on a host
        constructed with __new__ that never started the watcher."""
        a = H.Assistant.__new__(H.Assistant)
        a._state = "idle"
        a._handsfree = False
        a._followup_until = 0.0
        a._listener = types.SimpleNamespace(mic_snapshot=dict)
        # no _selfwatch attribute at all: the section reports that honestly
        snap = H.Assistant.mic_health(a)
        assert snap["self_watch"] == {"error": "self-watch not started"}
        # ...and a started sampler reports through (no error key when healthy)
        a._selfwatch = _watcher()
        snap = H.Assistant.mic_health(a)
        assert snap["self_watch"].get("error") is None
        assert "reminders" in snap["self_watch"]["watched"]
        assert snap["self_watch"]["state"] == "idle"

    def test_shutdown_event_is_the_only_stop_signal(self, H, monkeypatch):
        """The watcher loop exits when the app's own shutdown Event is set —
        no separate flag to forget, no thread outliving shutdown."""
        a = H.Assistant.__new__(H.Assistant)
        a._shutdown_event = threading.Event()
        a._selfwatch = _watcher()
        ticks: list[int] = []

        def one_tick():
            ticks.append(1)
            a._shutdown_event.set()        # the first pass ends the loop

        monkeypatch.setattr(a, "_selfwatch_tick", one_tick)
        a._selfwatch_loop()
        assert ticks == [1]
