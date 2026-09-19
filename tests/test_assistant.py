"""Assistant collaborator tests: pomodoro, notifications and the reminder
store in isolation (no Assistant, no Qt, no real config directory)."""
from __future__ import annotations

import json
import threading
import time
import types

from core.assistant import PomodoroController, ReminderStore, split_due_reminders


def _controller(spoken: list):
    started = []

    def spawn(target, args=(), name="t"):
        th = threading.Thread(target=target, args=args, name=name, daemon=True)
        started.append(th)
        th.start()
        return th

    return PomodoroController(announce=spoken.append, spawn=spawn,
                              is_closed=lambda: False), started

def test_pomodoro_start_status_stop():
    spoken: list = []
    pomo, _ = _controller(spoken)
    assert pomo.command("status", 25, 5) == "pomodoro is off"
    assert pomo.command("start", 25, 5).startswith("pomodoro started")
    assert "work phase" in pomo.command("status", 25, 5)
    assert "minutes remaining" in pomo.command("status", 25, 5)
    assert pomo.command("start", 25, 5) == "pomodoro is already running"
    assert pomo.command("stop", 0, 0) == "pomodoro stopped"
    assert pomo.command("status", 0, 0) == "pomodoro is off"
    assert spoken and spoken[0].startswith("Pomodoro started")


def test_pomodoro_loop_announces_transition():
    spoken: list = []
    pomo, started = _controller(spoken)
    pomo.command("start", 0.001, 0.001)  # ~60 ms phases
    deadline = time.monotonic() + 5
    while len(spoken) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    pomo.command("stop", 0, 0)
    for th in started:
        th.join(2)
    assert len(spoken) >= 2 and "break" in spoken[1]


def test_pomodoro_shutdown_is_idempotent():
    spoken: list = []
    pomo, _ = _controller(spoken)
    pomo.shutdown()  # never started: no-op, no raise
    pomo.command("start", 25, 5)
    pomo.shutdown()
    pomo.shutdown()


class _FakeProc:
    """A dbus-monitor stand-in: live until told to die, with empty stdout."""

    stdout: tuple = ()

    def __init__(self, *a, **k):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def _reader(spoken: list, **kw):
    from core.assistant import NotificationReader
    args = dict(spawn=lambda *a, **k: None, is_closed=lambda: False,
                announce=spoken.append,
                muted=lambda a, s, b: False,
                popen_factory=None, persist=lambda k, v: None)
    args.update(kw)
    return NotificationReader(**args)


def test_reader_start_stop_lifecycle():
    spoken: list = []
    procs: list = []

    class FakeProc:
        def __init__(self, *a, **k):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    import types as _types
    reader = _reader(
        spoken,
        spawn=lambda *a, **k: _types.SimpleNamespace(is_alive=lambda: True),
        popen_factory=lambda *a, **k: procs.append(FakeProc(*a, **k)) or procs[-1])
    assert reader.set_enabled(True) == "notification reader enabled"
    assert reader.set_enabled(True) == "notification reader is already on"
    assert reader.set_enabled(False) == "notification reader disabled"
    assert procs and procs[0].terminated is True
    reader.shutdown()  # idempotent, never started-twice state


def test_reader_missing_binary_disables_and_persists():
    spoken: list = []
    saved: list = []

    def nobin(*a, **k):
        raise FileNotFoundError("no dbus-monitor")

    reader = _reader(spoken, popen_factory=nobin, persist=lambda k, v: saved.append((k, v)))
    assert reader.set_enabled(True) == "ERROR: dbus-monitor is not installed"
    assert saved == [("notification_reader", False)]


def test_reader_refuses_when_closed():
    reader = _reader([], is_closed=lambda: True,
                     popen_factory=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    assert reader.set_enabled(True) == "ERROR: assistant is shut down"


# --------------------------------------------------------------- reminders
def _store(tmp_path, **over):
    """A ReminderStore wired to tmp files with no real locking or backup."""
    saved = []
    ctx = None

    import contextlib

    @contextlib.contextmanager
    def file_lock(_dir=None, _name=None):
        yield

    store = ReminderStore(
        over.pop("path", tmp_path / "reminders.json"),
        lock=threading.RLock(),
        file_lock=file_lock,
        backup=lambda _p: None,
        write=lambda p, text: (saved.append(text), p.write_text(text)),
        clock=over.pop("clock", lambda: 1000.0),
        **over,
    )
    return store, saved, ctx


def test_split_due_reminders_keeps_future_ones():
    items = [{"name": "later", "due": 2000.0, "repeat_hours": 0}]
    fired, kept = split_due_reminders(items, 1000.0)
    assert fired == []
    assert kept == items


def test_split_due_reminders_advances_repeats_past_missed_steps():
    # a daily reminder whose due time was 3 days ago must advance to the next
    # future occurrence, never fire a backlog and never sit in the past
    day = 86400.0
    items = [{"name": "daily", "due": 1000.0, "repeat_hours": 24}]
    fired, kept = split_due_reminders(items, 1000.0 + 3.5 * day)
    assert [r["name"] for r in fired] == ["daily"]
    assert len(kept) == 1
    assert kept[0]["due"] > 1000.0 + 3.5 * day
    assert (kept[0]["due"] - 1000.0) % day == 0


def test_split_due_reminders_fires_one_shot_once():
    items = [{"name": "once", "due": 999.0, "repeat_hours": 0}]
    fired, kept = split_due_reminders(items, 1000.0)
    assert [r["name"] for r in fired] == ["once"]
    assert kept == []


def test_load_drops_malformed_entries(tmp_path):
    store, _saved, _ = _store(tmp_path)
    store.path.write_text(json.dumps([
        {"name": "ok", "due": 1.0},
        {"name": "no-due"},
        {"due": 2.0},
        "junk",
    ]))
    loaded = store.load()
    assert [r["name"] for r in loaded] == ["ok"]
    assert loaded[0]["repeat_hours"] == 0.0


def test_load_tolerates_missing_or_bad_json(tmp_path):
    store, _saved, _ = _store(tmp_path)
    assert store.load() == []
    store.path.write_text("not json")
    assert store.load() == []
    store.path.write_text(json.dumps({"not": "a list"}))
    assert store.load() == []


def test_update_is_one_read_modify_write(tmp_path):
    store, saved, _ = _store(tmp_path)
    store.update(lambda items: items + [{"name": "a", "due": 5.0}])
    store.update(lambda items: items + [{"name": "b", "due": 6.0}])
    assert [r["name"] for r in store.load()] == ["a", "b"]
    assert len(saved) == 2          # one write per transaction


def test_update_ignores_a_falsy_mutate_result(tmp_path):
    store, _saved, _ = _store(tmp_path)
    store.update(lambda items: items + [{"name": "keep", "due": 5.0}])
    store.update(lambda items: None)          # None keeps the list as-is
    assert [r["name"] for r in store.load()] == ["keep"]


def test_take_missed_pops_due_and_keeps_the_rest(tmp_path):
    store, _saved, _ = _store(tmp_path, clock=lambda: 1000.0)
    store.update(lambda items: [
        {"name": "past", "due": 900.0, "repeat_hours": 0},
        {"name": "future", "due": 2000.0, "repeat_hours": 0},
    ])
    assert [r["name"] for r in store.take_missed()] == ["past"]
    assert [r["name"] for r in store.load()] == ["future"]


def test_take_missed_noop_on_empty_queue(tmp_path):
    store, saved, _ = _store(tmp_path)
    assert store.take_missed() == []
    assert saved == []              # an empty queue is never rewritten


def test_drain_due_fires_and_prunes(tmp_path):
    store, _saved, _ = _store(tmp_path, clock=lambda: 1000.0)
    store.update(lambda items: [
        {"name": "now", "due": 1000.0, "repeat_hours": 0},
        {"name": "later", "due": 3000.0, "repeat_hours": 0},
    ])
    assert [r["name"] for r in store.drain_due()] == ["now"]
    assert [r["name"] for r in store.load()] == ["later"]
    assert store.drain_due() == []  # nothing due: no rewrite, no re-fire


def test_store_follows_a_rebound_path(tmp_path):
    """The seam the old inlined code had: rebinding the store's path (tests,
    diagnostics) must redirect every later call."""
    store, _saved, _ = _store(tmp_path / "first")
    moved = tmp_path / "second.json"
    store.path = moved
    store.save([{"name": "moved", "due": 1.0}])
    assert not (tmp_path / "first").exists()
    assert [r["name"] for r in store.load()] == ["moved"]


# ------------------------------------------- concurrency & lifecycle (batch 3)
def test_pomodoro_double_start_is_serialised():
    """Two overlapping turns must not start two workers.

    `is_alive()` then `start()` with no lock is a check-then-act, and a spoken
    turn can overlap a hands-free one. Two loops announce every phase boundary
    twice, which sounds exactly like the timer malfunctioning.
    """
    spoken: list = []
    import threading as _t
    barrier = _t.Barrier(8)
    results: list = []
    guard = _t.Lock()

    pomo, started = _controller(spoken)

    def _start():
        barrier.wait()
        r = pomo.command("start", 25, 5)
        with guard:
            results.append(r)

    threads = [_t.Thread(target=_start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)
    pomo.shutdown()
    assert len(started) == 1, started
    assert sum(1 for r in results if r.startswith("pomodoro started")) == 1, results
    assert sum(1 for s in spoken if "Pomodoro started" in s) == 1, spoken


def test_pomodoro_stop_cannot_leave_a_ghost_transition():
    """A stop landing exactly as a phase expires must not announce the phase.

    The loop's `wait()` timing out and the user's stop are independent events.
    Announcing the transition without re-checking under the lock shutdown()
    takes means the bubble says "pomodoro stopped" and then speaks the next
    phase anyway.
    """
    spoken: list = []
    pomo, _ = _controller(spoken)
    pomo._state = {"phase": "work", "until": time.monotonic() - 1,
                   "work": 0.001, "break": 30.0}

    class RacyStop:
        """The timeout expires at the same instant the user asks to stop."""

        def __init__(self):
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            self._set = True

        def wait(self, timeout):
            pomo.shutdown()          # the user stops here…
            return False             # …and the wait still reports "timed out"

    racy = RacyStop()
    pomo._stop = racy
    pomo._loop(racy)
    assert spoken == [], spoken


def test_pomodoro_shutdown_joins_the_worker():
    """`stopped` must mean the worker is OUT, not on its way out.

    The worker is given a slow unwind after it notices the stop, so this
    asserts the join itself rather than the thread's speed: without it,
    command("stop") returns while the loop is still unwinding.
    """
    spoken: list = []
    pomo, started = _controller(spoken)
    unwound = threading.Event()

    def slow_loop(stop):
        stop.wait(5.0)          # notice the stop…
        time.sleep(0.25)        # …then take a moment to unwind
        unwound.set()

    pomo._loop = slow_loop
    assert pomo.command("start", 25, 5).startswith("pomodoro started")
    deadline = time.monotonic() + 2
    while not started[0].is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    pomo.shutdown()
    assert unwound.is_set(), "shutdown returned before the worker finished"
    assert pomo._thread is None


def test_reader_disable_joins_so_a_restart_cannot_orphan_it():
    """`off` must mean the worker is gone before the next `on`.

    Without the join, disable returned while the old loop was still parked on
    the dead monitor's stdout; enabling again immediately started a second loop
    beside it — two readers, one holding the previous stop event, both
    speaking notifications.
    """
    spoken: list = []
    started: list = []

    def spawn(target, args=(), name="t"):
        th = threading.Thread(target=target, args=args, name=name, daemon=True)
        started.append(th)
        th.start()
        return th

    class SlowProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    reader = _reader(spoken, spawn=spawn, popen_factory=lambda *a, **k: SlowProc())

    def slow_loop(proc, stop):
        stop.wait(5.0)          # notice the stop…
        time.sleep(0.25)        # …then take a moment to unwind

    reader.loop = slow_loop
    assert reader.set_enabled(True) == "notification reader enabled"
    deadline = time.monotonic() + 2
    while not started[0].is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert reader.set_enabled(False) == "notification reader disabled"
    assert not started[0].is_alive(), "disable returned before the worker stopped"
    assert reader._thread is None
    assert reader.set_enabled(True) == "notification reader enabled"
    assert len(started) == 2, started        # exactly one new worker, no orphan
    reader.set_enabled(False)


def test_two_overlapping_enables_start_exactly_one_reader():
    """The reader owns ONE slot, and the registry hands it out.

    "Is a worker already alive?" and "start one" were two separate reads with a
    process spawn between them, so two overlapping enables both saw a free slot
    and both spawned — two dbus-monitors, two loops, one of them holding the
    other's stop event, every notification spoken twice. The spawn is held open
    here so both callers are genuinely inside the window at once.
    """
    spoken: list = []
    procs: list = []
    started: list = []
    entered = threading.Barrier(2, timeout=5)

    def spawn(target, args=(), name="t"):
        # The old shape assigned its thread attribute only AFTER this returned,
        # so a second caller arriving here still saw a free slot.
        time.sleep(0.2)
        started.append(args)
        return threading.current_thread()

    reader = _reader(
        spoken, spawn=spawn,
        popen_factory=lambda *a, **k: procs.append(_FakeProc()) or procs[-1])
    results: list = []
    guard = threading.Lock()

    def enable():
        entered.wait(timeout=5)
        out = reader.set_enabled(True)
        with guard:
            results.append(out)

    threads = [threading.Thread(target=enable) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(procs) == 1, f"{len(procs)} readers were started"
    assert len(started) == 1, f"{len(started)} worker threads were spawned"
    assert sorted(results) == ["notification reader enabled",
                               "notification reader is already on"], results


def test_a_reader_that_will_not_die_keeps_its_slot():
    """`off` must not free the slot while the worker is still running.

    The slot is the only thing standing between a restart and two readers, so
    a worker that outlived its join budget keeps it, and the reclaim predicate
    frees it the moment it actually dies.
    """
    import types
    reader = _reader([],
                     spawn=lambda *a, **k: types.SimpleNamespace(
                         is_alive=lambda: True),
                     popen_factory=lambda *a, **k: _FakeProc())
    assert reader.set_enabled(True) == "notification reader enabled"
    assert reader.set_enabled(False) == "notification reader disabled"
    assert reader.set_enabled(True) == "notification reader is already on", \
        "the slot was freed while its worker was still alive"


def test_a_reader_whose_worker_is_gone_is_restarted_not_refused():
    """A corpse in the slot must not make "on" a lie: the reclaim predicate
    takes the dead occupant back under the same lock that grants the slot."""
    procs: list = []
    reader = _reader([])
    reader._popen_factory = lambda *a, **k: procs.append(_FakeProc()) or procs[-1]
    reader.set_enabled(True)
    assert reader._thread is None          # the default spawn starts nothing
    assert reader.set_enabled(True) == "notification reader enabled", \
        "a dead worker stranded its slot"
    assert len(procs) == 2, "the dead monitor's slot was not reused"
    assert procs[0].terminated is True, "the dead monitor was never disposed"


def test_reader_gives_up_loudly_instead_of_claiming_to_be_on():
    """Respawns exhausted ⇒ the setting must stop saying "on".

    dbus-monitor dying repeatedly used to leave notification_reader=True with
    no monitor and no reader: the toggle said on, nothing was listening, and no
    notification would ever be spoken again.
    """
    spoken: list = []
    saved: list = []

    class DeadProc:
        stdout: tuple = ()

        def poll(self):
            return 0                 # already exited

        def terminate(self):
            pass

    class InstantStop:
        """No real waiting: exhaust the five respawns in milliseconds."""
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, timeout):
            return False

    reader = _reader(spoken, popen_factory=lambda *a, **k: DeadProc(),
                     persist=lambda k, v: saved.append((k, v)))
    reader.run(InstantStop())
    assert saved and saved[-1] == ("notification_reader", False), saved
    assert reader._proc is None



def test_a_pass_that_raises_spends_the_budget_instead_of_spinning():
    """A loop() that RAISES is not exhaustion, and must not hot-spin.

    The monitor can still poll as ALIVE while its pass raises (a wedged
    dbus-monitor whose stdout read died), so the respawn branch is skipped on
    the next iteration and the call used to be re-entered immediately: a
    full-CPU spin that logged one traceback per pass and never gave up, because
    `attempts` was only incremented on the spawn path. Spinning is invisible
    from outside — the reader still reports itself enabled — so it is pinned
    here rather than left to the next load average.
    """

    class AliveProc:
        stdout: tuple = ()

        def poll(self):
            return None                  # looks healthy to the respawn check

        def terminate(self):
            pass

    class BudgetedStop:
        """Bounded, so a hot-spinning reader fails rather than hangs the
        suite, and it records the waits it was asked for."""

        def __init__(self, passes: int):
            self.passes = passes
            self.calls = 0
            self.waits = 0
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            self._set = True

        def wait(self, timeout):
            self.waits += 1
            return False

    saved: list = []
    stop = BudgetedStop(passes=12)
    reader = _reader([], popen_factory=lambda *a, **k: AliveProc(),
                     persist=lambda k, v: saved.append((k, v)))

    def bad_loop(proc, stop_event):
        stop.calls += 1
        if stop.calls > stop.passes:
            stop.set()                   # never let the fixed loop spin here
        raise RuntimeError("stdout vanished")

    reader.loop = bad_loop
    reader.run(stop, run=types.SimpleNamespace(proc=AliveProc()))

    assert stop.calls <= 5, (
        f"the raising pass ran {stop.calls} times: it did not spend the "
        "respawn budget, which is a hot spin rather than a retry")
    assert stop.waits >= 5, (
        "no backoff between passes — a permanent error burned a core")
    assert saved and saved[-1] == ("notification_reader", False), (
        "giving up must still take the toggle with it")
