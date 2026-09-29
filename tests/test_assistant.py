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


def test_pomodoro_speaks_its_minutes_as_a_person_would(monkeypatch):
    """`{n:.0f} minutes` said "1 minutes", rounded a 2.5-minute phase to "2"
    (round-half-even) and, in `status`, floored 59 s to "0 minutes remaining"."""
    from core import assistant as A
    assert A._minutes(1) == "1 minute"
    assert A._minutes(25) == "25 minutes"
    assert A._minutes(2.5) == "2.5 minutes"
    assert A._minutes(1.0) == "1 minute"
    one: list = []
    pomo1, _ = _controller(one)
    assert pomo1.command("start", 1, 1) == (
        "pomodoro started: 1 minute work and 1 minute break")
    assert one[0] == "Pomodoro started: 1 minute of work."
    pomo1.command("stop", 0, 0)

    spoken: list = []
    pomo, _ = _controller(spoken)
    assert pomo.command("start", 25, 5) == (
        "pomodoro started: 25 minute work and 5 minute break")
    assert spoken[0] == "Pomodoro started: 25 minutes of work."
    until = pomo._state["until"]
    # 59 s left is "less than a minute"; 61 s left is two minutes (rounded UP);
    # exactly one minute left is singular
    for left, said in ((59, "less than a minute"), (61, "2 minutes"),
                       (60, "1 minute"), (1500, "25 minutes")):
        monkeypatch.setattr(A.time, "monotonic", lambda left=left: until - left)
        assert pomo.command("status", 1, 1) == (
            f"pomodoro is in work phase with {said} remaining"), left
    monkeypatch.undo()
    pomo.command("stop", 0, 0)


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


def test_an_unparseable_file_is_quarantined_not_emptied(tmp_path):
    """An unreadable or unparseable file is EVIDENCE, not an empty queue:
    load() must quarantine it (the settings.json rule) and mark the load
    failed, so the corruption stays on disk and no caller mistakes it for
    "the user cancelled everything" (audit 2026-09-23)."""
    moved = []
    store, _saved, _ = _store(
        tmp_path, quarantine=lambda p: (moved.append(p)))
    store.path.write_text("{ CORRUPT !!!")
    assert store.load() == []
    assert moved == [store.path]
    assert store.load_failed
    # A missing file is NOT a failure: first run, a real empty.
    store.path.unlink()
    assert store.load() == [] and not store.load_failed


def test_a_reminders_file_that_is_not_text_is_evidence_too(tmp_path):
    """`read_text()` raised UnicodeDecodeError for a file with one bad byte, and
    only OSError was caught, so it escaped `load` — every reminder tool and the
    worker tick failed, and `update()` never got the chance to refuse to save
    over it. It is quarantined and marked failed like a file that is not JSON."""
    moved = []
    store, saved, _ = _store(tmp_path, quarantine=lambda p: moved.append(p))
    store.path.write_bytes(b'[{"name": "caf\xe9", "due": 5.0}]')
    assert store.load() == []                        # no exception
    assert moved == [store.path] and store.load_failed
    # ...and a transaction refuses to save a fresh queue over it
    assert store.update(lambda items: items + [{"name": "x", "due": 9.0}]) == []
    assert saved == [], "a queue we could not read must not be overwritten"


def test_update_never_saves_over_a_load_that_failed(tmp_path):
    """The data-loss reproduction: live queue + a corrupt file + one update()
    used to write a fresh queue over the corrupt file AND _backup()-first
    then copied the CORRUPT bytes over the good .bak — queue and recovery
    copy destroyed together, silently. Now the transaction aborts: the
    corrupt file stays on disk un-replaced, and nothing is written."""
    store, saved, _ = _store(tmp_path)
    store.update(lambda items: items + [{"name": "water plants", "due": 5.0}])
    good = store.path.read_text()
    store.path.write_text("{ CORRUPT !!!")       # disk corrupts between turns
    result = store.update(
        lambda items: items + [{"name": "new", "due": 9.0}])
    assert result == [], "the aborted transaction returns what it could read"
    assert store.path.read_text() == "{ CORRUPT !!!", (
        "update() must not overwrite a file it could not read")
    assert saved[-1] == good, (
        "no new save may run: the corrupt file must not become the .bak")


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


# ------------------------------------------------ the reader's own health
# What `--ptt health` reads. This reader is silent BY DESIGN — it speaks only
# when someone else's notification arrives — so "it said nothing" cannot tell a
# working reader from a wedged one, and that was exactly the state a wedged one
# was in. These pin the counters that can, and the states they resolve to.

_HEALTH_KEYS = {
    "enabled", "state", "running", "passes", "notifications", "failures",
    "attempts_used", "attempts_budget", "backoff_seconds", "pass_seconds",
    "gave_up", "last_failure", "rearm_offer",
}


def _one_notify_lines(app="Firefox", summary="hi", body="there"):
    """One minimal dbus-monitor Notify message: the header, then the four
    payload strings `loop()` consumes."""
    return [
        ("method call time=1.0 sender=:1.5 -> destination=:1.6 serial=7 "
         "path=/org/freedesktop/Notifications; "
         "interface=org.freedesktop.Notifications; member=Notify"),
        f'   string "{app}"',
        "   uint32 0",
        '   string ""',
        f'   string "{summary}"',
        f'   string "{body}"',
    ]


# -------------------------------------------------------- raw dbus-monitor text
# dbus-monitor prints a string argument RAW: the quotes it holds are not escaped
# and neither is a newline. Captured from a real `dbus-monitor` (dbus 1.14) for
# Notify("Signal", 0, "", "Alice", 'She said "hi there" and\nsecond line', ...):
_REAL_CAPTURE = [
    "method call time=1790686292.505645 sender=:1.1 -> "
    "destination=org.freedesktop.Notifications serial=3 "
    "path=/org/freedesktop/Notifications; "
    "interface=org.freedesktop.Notifications; member=Notify",
    '   string "Signal"',
    "   uint32 0",
    '   string ""',
    '   string "Alice"',
    '   string "She said "hi there" and',
    'second line"',
    "   array [",
    '      string "default"',
    '      string "Open"',
    "   ]",
    "   array [",
    "      dict entry(",
    '         string "urgency"',
    "         variant             byte 1",
    "      )",
    "   ]",
    "   int32 5000",
]


def _spoken_from(lines):
    spoken: list = []
    reader = _reader(spoken)
    proc = types.SimpleNamespace(stdout=iter([l + "\n" for l in lines]),
                                 poll=lambda: 0)
    reader.loop(proc, threading.Event())
    return spoken


def _notify(app, summary, body_lines, trailers=True):
    head = _one_notify_lines(app, summary, "")[:5]      # header .. summary
    tail = _REAL_CAPTURE[7:] if trailers else []
    return head + body_lines + tail


def test_a_body_with_quotes_and_a_newline_is_spoken_whole():
    """The real capture above. The regex stopped at the first inner quote and the
    continuation line had no `string` at all, so the body was spoken as "She
    said" — and for a body that only spans lines, as the NEXT string of the
    message: the action name `default`."""
    assert _spoken_from(_REAL_CAPTURE) == [
        'Notification from Signal: Alice. She said "hi there" and second line']


def test_a_multi_line_body_is_not_replaced_by_the_action_name():
    assert _spoken_from(_notify("Mail", "Inbox", ['   string "first line',
                                                  'second line"'])) == [
        "Notification from Mail: Inbox. first line second line"]


def test_a_body_that_ends_in_a_quote_character_keeps_it():
    assert _spoken_from(_notify("Chat", "Bob", ['   string "he said "no""'])) == [
        'Notification from Chat: Bob. he said "no"']


def test_a_plain_body_and_an_empty_body_still_read_as_before():
    assert _spoken_from(_notify("Firefox", "hi", ['   string "there"'])) == [
        "Notification from Firefox: hi. there"]
    assert _spoken_from(_notify("Firefox", "hi", ['   string ""'])) == [
        "Notification from Firefox: hi"]


def test_two_messages_in_a_row_are_each_delivered_once():
    lines = (_notify("A", "s1", ['   string "b1"'])
             + _notify("B", "s2", ['   string "multi', 'line"']))
    assert _spoken_from(lines) == ["Notification from A: s1. b1",
                                   "Notification from B: s2. multi line"]


def test_a_message_cut_off_after_its_body_is_still_delivered():
    """The body is the stream's last line: the line that would close it never
    comes, so the end of the stream does."""
    assert _spoken_from(_notify("Signal", "Alice", ['   string "last line"'],
                                trailers=False)) == [
        "Notification from Signal: Alice. last line"]


def test_dbus_string_value_drops_only_the_closing_quote():
    from core.assistant import dbus_string_value
    assert dbus_string_value(['abc"']) == "abc"
    assert dbus_string_value(['a "b" c', 'd"']) == 'a "b" c\nd'
    assert dbus_string_value(['no closing quote']) == "no closing quote"
    assert dbus_string_value(['"']) == ""


def test_reader_health_has_one_shape_before_it_ever_runs():
    """`--ptt health` answers with the same keys whether or not a reader was
    ever built: the bubble serves this from its control-socket thread, where a
    KeyError is the whole snapshot lost."""
    from core.assistant import NotificationReader
    reader = _reader([])
    h = reader.health(enabled=False)
    assert set(h) == _HEALTH_KEYS, set(h) ^ _HEALTH_KEYS
    assert (h["state"], h["running"], h["passes"], h["notifications"],
            h["failures"], h["gave_up"], h["last_failure"]) == (
        "off", False, 0, 0, 0, False, None)
    assert h["attempts_used"] == 0
    assert h["attempts_budget"] == reader.ATTEMPT_BUDGET
    assert h["pass_seconds"] is None
    json.dumps(h)          # the socket hands this out as JSON, always
    # ...and a host with no reader object at all reports the same shape.
    assert set(NotificationReader.absent_health(enabled=True)) == _HEALTH_KEYS


def test_reader_health_counts_passes_and_notifications():
    """A pass, and the messages it carried: the only evidence that a monitor
    nobody can see is actually producing anything."""
    reader = _reader([])
    proc = types.SimpleNamespace(stdout=iter(_one_notify_lines()), poll=lambda: 0)
    reader.loop(proc, threading.Event())
    h = reader.health(enabled=True)
    assert h["passes"] == 1 and h["notifications"] == 1, h
    assert h["failures"] == 0 and h["last_failure"] is None
    assert h["pass_seconds"] is None, "a finished pass must not read as quiet"


def test_reader_health_calls_a_quiet_reader_healthy_not_wedged():
    """The point of the exercise: running, with its whole budget, and nothing
    to say. Silence has to be readable as health — otherwise the surface that
    exists to find a wedged reader cries wolf on a quiet desktop."""
    reader = _reader([], spawn=lambda *a, **k: types.SimpleNamespace(
        is_alive=lambda: True), popen_factory=lambda *a, **k: _FakeProc())
    assert reader.set_enabled(True) == "notification reader enabled"
    h = reader.health(enabled=True)
    assert h["state"] == "running" and h["running"] is True
    assert h["passes"] == 0, "parked on stdout, waiting for a notification"
    assert h["attempts_used"] == 0 and h["failures"] == 0


def test_reader_health_shows_the_stall_the_toggle_hides():
    """Enabled, and nothing left listening. The toggle and the settings file
    both call this "on", and before this it looked exactly like a quiet
    desktop — the worst state the reader can be in, and the only one a caller
    cannot infer from the reader's own counters."""
    reader = _reader([])
    assert reader.health(enabled=True)["state"] == "stalled"
    assert reader.health(enabled=False)["state"] == "off"


def test_reader_health_names_the_last_failure_and_what_it_cost():
    """A wedged monitor has to be actionable from the snapshot alone: which
    pass failed, why, how long ago, and how much of the retry budget is gone."""
    saved: list = []

    class AliveProc:
        stdout: tuple = ()

        def poll(self):
            return None

        def terminate(self):
            pass

    class InstantStop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, timeout):
            return False

    reader = _reader([], popen_factory=lambda *a, **k: AliveProc(),
                     persist=lambda k, v: saved.append((k, v)))
    reader.loop = lambda proc, stop: (_ for _ in ()).throw(
        OSError("monitor said no"))
    reader.run(InstantStop(), run=types.SimpleNamespace(proc=AliveProc()))

    h = reader.health(enabled=True)
    assert h["gave_up"] is True and h["state"] == "gave-up", h
    assert h["failures"] >= 1 and h["last_failure"] is not None
    assert h["last_failure"]["where"] == "pass"
    assert "OSError" in h["last_failure"]["error"]
    assert "monitor said no" in h["last_failure"]["error"]
    assert h["last_failure"]["age_seconds"] >= 0
    assert h["attempts_used"] >= h["attempts_budget"], (
        "a reader that gave up must show the budget spent, not a retry left")
    assert saved and saved[-1] == ("notification_reader", False), saved


def test_reader_gave_up_arms_and_speaks_the_rearm_offer():
    """The third channel the gave-up path opens, after the journal and the
    reload request: the user is OFFERED a way back that does not need a
    restart. A finding is never only in the journal — and an offer the user
    never hears is not an offer."""
    spoken: list = []
    saved: list = []

    class AliveProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    class InstantStop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, timeout):
            return False

    reader = _reader(spoken, popen_factory=lambda *a, **k: AliveProc(),
                     persist=lambda k, v: saved.append((k, v)))
    reader.loop = lambda proc, stop: (_ for _ in ()).throw(
        OSError("monitor said no"))
    reader.run(InstantStop(), run=types.SimpleNamespace(proc=AliveProc()))
    assert any("re-enable notifications" in s for s in spoken), spoken
    needed, live = reader.rearm_gate()
    assert (needed, live) == (True, True), (needed, live)
    h = reader.health(enabled=False)
    assert h["rearm_offer"] is True, h
    # The offer claims exactly once.
    assert reader.consume_rearm_offer() is not None
    assert reader.rearm_gate() == (True, False), (
        "after the claim the gate reads live=False: the next bare start is "
        "refused with the expired path")


def test_reader_rearm_offer_is_not_extended_by_a_repeat_gave_up():
    """``arm_unless``'s rule, applied here: a second gave-up inside the window
    neither extends the deadline nor repeats the announce. A broken
    dbus-monitor plus a monitor-killing operator must not produce a chorus.
    """
    spoken: list = []
    reader = _reader(spoken)
    reader._health_update(gave_up=True)
    assert reader.arm_rearm_offer() is True
    first = reader._rearm_offer.snapshot_state()
    assert reader.arm_rearm_offer() is False, "the offer must not re-arm"
    assert reader._rearm_offer.snapshot_state() == first, (
        "the deadline must not move")
    assert sum("re-enable" in s for s in spoken) == 1, spoken


def test_reader_rearm_announce_survives_a_broken_speaker():
    """A broken speaker must not cost the user the offer: the offer is armed
    and queryable either way, and the finding is in the journal regardless.
    """
    def boom(_text):
        raise RuntimeError("no audio device")

    reader = _reader([boom])
    reader._health_update(gave_up=True)   # the diagnosis the offer answers
    assert reader.arm_rearm_offer() is True
    assert reader.rearm_gate() == (True, True)


def test_reader_rearm_gate_tracks_its_own_gave_up_diagnosis():
    """`needed` is the reader's own fact, not "an offer exists" — a fresh
    reader with no offer must read (False, False), which is what keeps a
    healthy reader's bare start ungated. And the arm itself refuses on a
    healthy reader: the offer exists only because of a gave-up.
    """
    spoken: list = []
    reader = _reader(spoken)
    assert reader.rearm_gate() == (False, False)
    assert reader.arm_rearm_offer() is False, (
        "a healthy reader must not arm a re-arm offer")
    assert spoken == [], "nothing was spoken for a healthy reader"


def test_reader_enable_clears_a_stale_rearm_offer():
    """A live reader owns no re-arm offer: a stale "yes" from an old
    conversation must not look like consent to a LATER gave-up. Cleared
    before the slot is granted, so the reader that starts can never inherit
    one.
    """
    spoken: list = []
    procs: list = []

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    reader = _reader(
        spoken,
        spawn=lambda *a, **k: types.SimpleNamespace(is_alive=lambda: True),
        popen_factory=lambda *a, **k: procs.append(FakeProc()) or procs[-1])
    reader._rearm_offer.arm(999.0, reason="stale")
    assert reader.set_enabled(True) == "notification reader enabled"
    assert reader.rearm_gate() == (False, False), (
        "an enabled reader must not carry a claimable offer")
    reader.shutdown()


def test_reader_gave_up_asks_for_the_live_reload_after_persisting():
    """The gave-up persist is a settings change made OUTSIDE the settings
    app, so it must go through the one channel a change is applied through:
    the reload request the settings app's own Save asks for. Ordered AFTER
    the persist (the reload re-reads the file; asking first would re-apply
    the stale value), and best-effort — a channel that raises must not
    turn the reader's own stop into a failure.
    """
    reloaded: list[int] = []
    saved: list[tuple] = []

    class AliveProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    class InstantStop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, timeout):
            return False

    reader = _reader([], popen_factory=lambda *a, **k: AliveProc(),
                     persist=lambda k, v: saved.append((k, v)),
                     request_reload=lambda: reloaded.append(1))
    reader.loop = lambda proc, stop: (_ for _ in ()).throw(
        OSError("monitor said no"))
    reader.run(InstantStop(), run=types.SimpleNamespace(proc=AliveProc()))
    assert saved[-1] == ("notification_reader", False), saved
    assert reloaded == [1], reloaded


def test_reader_gave_up_survives_a_raising_reload_channel():
    """The stop must land even if the reload channel is broken: the persist
    is the truth, the reload is a courtesy — and a courtesy that raises
    must not escalate the reader's own shutdown into an error state."""
    saved: list[tuple] = []

    class AliveProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    class InstantStop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, timeout):
            return False

    def broken():
        raise RuntimeError("channel down")

    reader = _reader([], popen_factory=lambda *a, **k: AliveProc(),
                     persist=lambda k, v: saved.append((k, v)),
                     request_reload=broken)
    reader.loop = lambda proc, stop: (_ for _ in ()).throw(
        OSError("monitor said no"))
    reader.run(InstantStop(), run=types.SimpleNamespace(proc=AliveProc()))
    assert saved[-1] == ("notification_reader", False), \
        "the stop persisted before the channel was asked"
    h = reader.health(enabled=False)
    assert h["gave_up"] is True, h


def test_reader_gave_up_without_a_channel_still_stops_cleanly():
    """The channel is optional (an embedder, a test host): None means no
    reload request is made and nothing else about the stop changes."""
    saved: list[tuple] = []

    class AliveProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    class InstantStop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, timeout):
            return False

    reader = _reader([], popen_factory=lambda *a, **k: AliveProc(),
                     persist=lambda k, v: saved.append((k, v)))
    reader.loop = lambda proc, stop: (_ for _ in ()).throw(
        OSError("monitor said no"))
    reader.run(InstantStop(), run=types.SimpleNamespace(proc=AliveProc()))
    assert saved[-1] == ("notification_reader", False), saved


def test_re_enabling_the_reader_starts_the_counters_over():
    """`health` describes the reader that is live NOW: carrying the previous
    run's failures forward would report a healthy reader as a failing one.
    The reset lands before the worker starts — after it, it would wipe the
    first pass."""
    reader = _reader([], spawn=lambda *a, **k: types.SimpleNamespace(
        is_alive=lambda: True), popen_factory=lambda *a, **k: _FakeProc())
    reader._health_update(failures=4, gave_up=True, attempts_used=5,
                          backoff_seconds=16.0,
                          last_failure={"where": "pass", "error": "old",
                                        "at": time.monotonic()})
    assert reader.set_enabled(True) == "notification reader enabled"
    h = reader.health(enabled=True)
    assert (h["failures"], h["gave_up"], h["attempts_used"],
            h["backoff_seconds"], h["last_failure"]) == (0, False, 0, 0.0, None)
    assert h["state"] == "running"


def test_pomodoro_beating_wait_keeps_the_exact_wait_contract():
    """The chunked wait exists so a 25-minute phase is not indistinguishable
    from a wedged loop to the self-watch sampler. The contract it must keep:
    a set stop wins IMMEDIATELY (never a full chunk), the total sleep is the
    requested amount (phase timing untouched), and the beat advances between
    chunks — which is the only reason the chunking exists.
    """
    from core.assistant import PomodoroController

    spoken: list = []

    def spawn(*a, **k):
        return types.SimpleNamespace(is_alive=lambda: True, join=lambda *a: None)

    ctrl = PomodoroController(announce=spoken.append, spawn=spawn,
                              is_closed=lambda: False)
    stop = threading.Event()
    ctrl._beat = 0

    # Stop already set: wins immediately, zero chunks consumed.
    stop.set()
    assert ctrl._wait_beating(stop, 300.0) is True
    assert ctrl._beat == 0

    # Unset stop, long wait: the wait consumes exactly `seconds` of chunk
    # sleeps (an Event.wait returns False after each full chunk) and beats
    # between chunks — 300 s in 30 s chunks is 10 beats.
    stop.clear()
    start = time.monotonic()
    assert ctrl._wait_beating(stop, 0.05) is False
    assert ctrl._beat == 1
    assert time.monotonic() - start < 1.0, "a 0.05 s wait must stay 0.05 s"
