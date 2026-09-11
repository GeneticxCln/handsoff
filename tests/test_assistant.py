"""Assistant collaborator tests: pomodoro, notifications and the reminder
store in isolation (no Assistant, no Qt, no real config directory)."""
from __future__ import annotations

import json
import threading
import time

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
