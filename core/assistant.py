"""Assistant collaborators: pomodoro, notifications, reminders, watcher ticks.

Small state machines with explicit dependencies — no Assistant import, no
Qt, no app state. handsoff.Assistant constructs them and keeps thin
delegating methods so the H.* monkeypatch contract and tests keep working.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
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

log = logging.getLogger("handsoff")


def dbus_strings(line: str) -> list[str]:
    """Extract ordinary quoted D-Bus string values from monitor output."""
    return re.findall(r'(?<!\\)"((?:\\.|[^"\\])*)"', line)


def notification_muted(app: str, summary: str, body: str, *,
                       mute_apps, app_name: str) -> bool:
    """New mute contract: user list matches app (+summary) with word-ish
    semantics (app substring, summary whole word, never body); self-mute
    when app==app_name or app_name appears in summary/body."""
    try:
        a = (app or "").lower()
        s = (summary or "").lower()
        b = (body or "").lower()
        me = (app_name or "").lower()
        # SELF first: our own popups echo the app name in summary/body.
        if a.strip() == me:
            return True
        if me and (me in s or me in b):
            return True
        for raw in mute_apps or []:
            m = str(raw or "").strip().lower()
            if not m:
                continue
            if m in a:
                return True
            try:
                if re.search(r"\b" + re.escape(m) + r"\b", s):
                    return True
            except re.error:
                if m in s:
                    return True
        return False
    except Exception:
        return False


class NotificationReader:
    """Session D-Bus notification monitor with mute list and per-app cooldown.

    Notification text is never replayed from history; only future
    notifications are spoken. `announce(text)` speaks, `muted(app, summary,
    body)` filters, `popen_factory(*args, **kwargs)` spawns dbus-monitor,
    `persist(key, value)` records setting changes, `is_closed()` reports
    assistant shutdown.
    """

    APP_COOLDOWN = 60.0  # one spoken digest per app per minute, max

    def __init__(self, *, spawn, is_closed, announce, muted, popen_factory,
                 persist) -> None:
        self._spawn = spawn
        self._is_closed = is_closed
        self._announce = announce
        self._muted = muted
        self._popen_factory = popen_factory
        self._persist = persist
        self._proc = None
        self._stop: threading.Event | None = None
        self._thread = None
        self._cooldown_lock = threading.Lock()
        self._app_last: dict[str, float] = {}

    def set_enabled(self, enabled: bool):
        """Start/stop the monitor; muted apps are filtered before TTS."""
        if enabled:
            if self._is_closed():
                return "ERROR: assistant is shut down"
            if self._thread is not None and self._thread.is_alive():
                return "notification reader is already on"
            try:
                self._proc = self._popen_factory(
                    ["dbus-monitor", "--session",
                     "interface='org.freedesktop.Notifications',member='Notify'"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1, start_new_session=True)
            except FileNotFoundError:
                self._proc = None
                self._persist("notification_reader", False)
                return "ERROR: dbus-monitor is not installed"
            except OSError as e:
                self._proc = None
                self._persist("notification_reader", False)
                return f"ERROR: notification monitor failed: {e}"
            stop = threading.Event()
            self._stop = stop
            self._thread = self._spawn(
                self.run, args=(stop,), name="notification-reader")
            log.info("desktop notification reader enabled")
            return "notification reader enabled"
        stop = self._stop
        proc = self._proc
        self._stop = None
        self._proc = None
        if stop is not None:
            stop.set()
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        log.info("desktop notification reader disabled")
        return "notification reader disabled"

    def shutdown(self) -> None:
        """Stop the monitor; safe on a never-started reader."""
        self.set_enabled(False)

    def loop(self, proc, stop: threading.Event) -> None:
        values: list[str] | None = None  # None: between messages, ignore trailers
        try:
            for line in proc.stdout or ():
                if stop.is_set():
                    return
                if "member=Notify" in line and (
                        line.startswith("signal ") or line.startswith("method call ")):
                    values = []
                    continue
                if values is None:
                    continue
                if not values and not line.lstrip().startswith("string"):
                    continue
                values.extend(dbus_strings(line))
                # Notify's signature is (app, replaces-id, icon, summary,
                # body, actions, hints, expire-time). dbus-monitor prints the
                # uint32/arrays separately, so the four strings we need are
                # app, icon, summary, body — consume once per message and
                # ignore the actions/hints trailers (sender-pid, urgency…),
                # which must never fire their own announcements.
                if len(values) >= 4:
                    app, _icon, summary, body = values[:4]
                    values = None  # consumed: one utterance per message
                    try:
                        if self._muted(app, summary, body):
                            log.info("notification muted from %s", app)
                            continue
                    except Exception:
                        pass
                    now = time.monotonic()
                    with self._cooldown_lock:
                        last = self._app_last.get(app.lower())
                        if last is not None and now - last < self.APP_COOLDOWN:
                            log.info("notification cooldown suppresses %s", app)
                            continue
                        self._app_last[app.lower()] = now
                    text = f"Notification from {app}: {summary}"
                    if body.strip():
                        text += f". {body.strip()}"
                    try:
                        self._announce(text[:500])
                    except Exception:
                        log.exception("notification announcement failed")
            # stdout exhaustion (dbus-monitor died/restarted): log it so a
            # silent reader is visible; the reconnect wrapper respawns with
            # bounded backoff. Direct loop() callers simply return here.
            if not stop.is_set():
                log.warning("notification reader stdout exhausted — monitor exited")
        except (OSError, ValueError):
            if not stop.is_set():
                log.exception("notification reader stopped unexpectedly")
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except (AttributeError, OSError):
                    pass

    def run(self, stop: threading.Event) -> None:
        """Production wrapper: run loop(), respawning dbus-monitor with
        bounded backoff when its stdout is exhausted. At most 5 respawns,
        1s→30s exponential backoff; gives up quietly when disabled."""
        backoff = 1.0
        attempts = 0
        while not stop.is_set() and attempts < 5:
            proc = self._proc
            if proc is None or (hasattr(proc, "poll") and proc.poll() is not None):
                # previous monitor died — respawn it under backoff
                if stop.wait(backoff):
                    return
                try:
                    proc = self._popen_factory(
                        ["dbus-monitor", "--session",
                         "interface='org.freedesktop.Notifications',member='Notify'"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, bufsize=1, start_new_session=True)
                    self._proc = proc
                    log.warning("notification reader respawned (attempt %d)", attempts + 1)
                except Exception:
                    log.exception("notification reader respawn failed")
                    backoff = min(backoff * 2.0, 30.0)
                    attempts += 1
                    continue
                backoff = min(backoff * 2.0, 30.0)
                attempts += 1
            try:
                self.loop(proc, stop)
            except Exception:
                log.exception("notification reader pass failed")
            if stop.is_set():
                return
            # loop() returned via exhaustion: loop to respawn.


def split_due_reminders(items: list[dict], now: float
                        ) -> tuple[list[dict], list[dict]]:
    """Split reminders into (fired, kept).

    Repeats advance in whole repeat-steps, so a sleep or restart never loses
    one and a long sleep never machine-guns a backlog of missed occurrences.
    Pure arithmetic on purpose: the store, the worker tick and the tests all
    share this one implementation.
    """
    fired, kept = [], []
    for r in items:
        if r["due"] > now:
            kept.append(r)
            continue
        fired.append(r)
        step = float(r.get("repeat_hours") or 0) * 3600
        if step > 0:
            ahead = r["due"] + max(1, int((now - r["due"]) // step) + 1) * step
            kept.append({**r, "due": ahead})
    return fired, kept


class ReminderStore:
    """The persisted reminder queue: reminders.json behind one writer.

    Everything the queue needs from the application is injected — the
    in-process lock, the cross-process sidecar flock factory, the atomic
    write + backup helpers, a logger and a clock — so the parsing, the
    serialized transactions and the startup catch-up can be tested without
    an Assistant, Qt or a real config directory.

    Locking contract (unchanged from the inlined version this replaces): the
    sidecar flock is non-reentrant, so a caller must never nest two of those
    guards on the same thread; `lock` serializes threads instead.
    """

    def __init__(self, path, *, lock, file_lock, backup, write,
                 logger=None, clock=time.time) -> None:
        # `path` and `lock` stay public: the host application rebinds its
        # REMINDERS_FILE/REMINDERS_LOCK globals (tests redirect them) and
        # refreshes both here before each use, so the store must never be
        # constructed once with a captured path.
        self.path = path
        self.lock = lock
        self._file_lock = file_lock
        self._backup = backup
        self._write = write
        self._log = logger or logging.getLogger("handsoff")
        self._clock = clock

    def load(self) -> list[dict]:
        """Read reminders.json, dropping entries that are malformed."""
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, list):
                return []
            out = []
            for r in data:
                if not isinstance(r, dict) or not isinstance(r.get("name"), str) \
                        or not isinstance(r.get("due"), (int, float)):
                    continue
                try:
                    r["repeat_hours"] = float(r.get("repeat_hours") or 0)
                except (TypeError, ValueError):
                    r["repeat_hours"] = 0.0
                out.append(r)
            return out
        except (OSError, ValueError):
            return []

    def save(self, items: list[dict]) -> None:
        """Caller MUST hold `lock` + the sidecar flock: two concurrent
        writers would corrupt each other's read-modify-write."""
        self._backup(self.path)
        self._write(self.path, json.dumps(items, indent=1))

    def update(self, mutate) -> list[dict]:
        """One serialized read-modify-write transaction: load, mutate, save.
        Every mutation of the queue goes through here."""
        with self.lock, self._file_lock(self.path.parent, "reminders.json.lock"):
            items = self.load()
            items = mutate(items) or items
            self.save(items)
            return items

    def take_missed(self) -> list[dict]:
        """Pop reminders that came due while we were off (startup call)."""
        with self.lock, self._file_lock(self.path.parent, "reminders.json.lock"):
            items = self.load()
            if not items:
                return []
            fired, kept = split_due_reminders(items, self._clock())
            if fired:
                try:
                    self.save(kept)
                except OSError:
                    self._log.exception("could not prune fired reminders")
        return fired

    def drain_due(self) -> list[dict]:
        """Worker tick: fire everything due now and persist the survivors.

        Returns the fired entries (empty when nothing is due). The whole
        read-modify-write happens under one guard pair here, which is why
        callers must not wrap this in a flock of their own.
        """
        with self.lock, self._file_lock(self.path.parent, "reminders.json.lock"):
            items = self.load()
            fired, kept = [], items
            if items:
                fired, kept = split_due_reminders(items, self._clock())
            if fired and kept != items:
                self.save(kept)
        return fired
