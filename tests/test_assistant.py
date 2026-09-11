"""Assistant collaborator tests: pomodoro controller in isolation."""
from __future__ import annotations

import threading
import time

from core.assistant import PomodoroController


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
