"""Assistant collaborators: pomodoro, notifications, reminders, watcher ticks.

Small state machines with explicit dependencies — no Assistant import, no
Qt, no module globals. handsoff.Assistant constructs them and keeps thin
delegating methods so the H.* monkeypatch contract and tests keep working.
"""
from __future__ import annotations

import threading
import time


class PomodoroController:
    """Bounded pomodoro worker: work/break phases with spoken transitions.

    `announce(text)` speaks, `spawn(target, args, name)` starts a tracked
    worker thread, `is_closed()` reports assistant shutdown.
    """

    def __init__(self, announce, spawn, is_closed) -> None:
        self._announce = announce
        self._spawn = spawn
        self._is_closed = is_closed
        self._stop: threading.Event | None = None
        self._thread = None
        self._state: dict | None = None

    def command(self, action: str, work: float, break_minutes: float) -> str:
        """Own the bounded pomodoro worker and announce work/break transitions."""
        if action == "status":
            state = self._state
            if not state:
                return "pomodoro is off"
            remaining = max(0, int(state["until"] - time.monotonic()))
            return (f"pomodoro is in {state['phase']} phase with "
                    f"{remaining // 60} minutes remaining")
        if action == "stop":
            self.shutdown()
            return "pomodoro stopped"
        if self._is_closed():
            return "ERROR: assistant is shut down"
        if self._thread is not None and self._thread.is_alive():
            return "pomodoro is already running"
        stop = threading.Event()
        self._stop = stop
        self._state = {"phase": "work", "until": time.monotonic() + work * 60,
                       "work": work, "break": break_minutes}
        self._thread = self._spawn(self._loop, args=(stop,), name="pomodoro")
        self._announce(f"Pomodoro started: {work:.0f} minutes of work.")
        return f"pomodoro started: {work:.0f} minute work and {break_minutes:.0f} minute break"

    def _loop(self, stop: threading.Event) -> None:
        phase = "work"
        while not stop.is_set():
            state = self._state
            if not state:
                return
            if stop.wait(max(0.05, state["until"] - time.monotonic())):
                return
            phase = "break" if phase == "work" else "work"
            minutes = state["break"] if phase == "break" else state["work"]
            self._state = {**state, "phase": phase,
                           "until": time.monotonic() + minutes * 60}
            self._announce(
                f"Pomodoro: {('break' if phase == 'break' else 'back to work')} "
                f"for {minutes:.0f} minutes.")

    def shutdown(self) -> None:
        """Signal stop and clear phase state (the thread exits on its own)."""
        stop, self._stop = self._stop, None
        self._state = None
        if stop is not None:
            stop.set()
