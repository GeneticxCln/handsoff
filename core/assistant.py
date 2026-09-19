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

# Absolute, not relative: `handsoff.py` loads this module through the shared
# origin-checked loader, which exec's a bare spec (`__package__ == ""`), so a
# relative import cannot resolve. `core.tools` already imports its siblings this
# way, and a plain `import core.assistant` resolves to the same module object.
from core.registry import BoundedRegistry


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
        # One lock for the whole start/stop/transition handshake. Two turns can
        # overlap (a spoken turn and a hands-free one), so "is a worker alive?"
        # then "start one" is a check-then-act that can start TWO loops — and
        # two loops announce every phase boundary twice. It also serialises the
        # phase flip against shutdown(), so a stop cannot land between the wait
        # timing out and the announce that follows it. Re-entrant on purpose:
        # the phase flip announces while holding it, and an announce is a
        # callback into the rest of the app.
        self._lock = threading.RLock()

    def command(self, action: str, work: float, break_minutes: float) -> str:
        """Own the bounded pomodoro worker and announce work/break transitions."""
        if action == "status":
            with self._lock:
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
        with self._lock:
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
            with self._lock:
                state = self._state
            if not state:
                return
            if stop.wait(max(0.05, state["until"] - time.monotonic())):
                return
            phase = "break" if phase == "work" else "work"
            minutes = state["break"] if phase == "break" else state["work"]
            with self._lock:
                # Re-check under the lock: the wait above may have expired at
                # the same moment another thread stopped us, and announcing a
                # transition after "pomodoro stopped" is a ghost the user
                # cannot explain.
                if stop.is_set() or self._state is None:
                    return
                self._state = {**self._state, "phase": phase,
                               "until": time.monotonic() + minutes * 60}
                self._announce(
                    f"Pomodoro: {('break' if phase == 'break' else 'back to work')} "
                    f"for {minutes:.0f} minutes.")

    def shutdown(self) -> None:
        """Signal stop and clear phase state, then join the worker.

        The join is what makes the stop real: without it `command('stop')`
        could return while the loop was already past its wait and about to
        announce the next phase, so the bubble said "pomodoro stopped" and
        then spoke a transition anyway.
        """
        with self._lock:
            stop, self._stop = self._stop, None
            thread, self._thread = self._thread, None
            self._state = None
        if stop is not None:
            stop.set()
        _join_worker(thread, "pomodoro worker")

log = logging.getLogger("handsoff")


def _join_worker(thread, what: str, timeout: float = 2.0) -> None:
    """Join a worker thread on stop, tolerating thread-like stand-ins.

    `spawn` is injected (the app passes a real Thread; tests pass doubles), so
    the only contract relied on here is `is_alive()` plus a join if one
    exists. Never joins the calling thread, which would deadlock a worker
    stopping itself. A worker still alive after the budget is the app's
    problem, not a reason to hang the caller that asked for a stop.
    """
    if thread is None or thread is threading.current_thread():
        return
    try:
        if not thread.is_alive():
            return
    except Exception:
        return
    join = getattr(thread, "join", None)
    if join is None:
        return
    try:
        join(timeout=timeout)
    except Exception:
        log.exception("%s join failed", what)
        return
    if thread.is_alive():
        log.warning("%s did not stop within %.0fs", what, timeout)


def dbus_strings(line: str) -> list[str]:
    """Extract ordinary quoted D-Bus string values from monitor output."""
    return re.findall(r'(?<!\\)"((?:\\.|[^"\\])*)"', line)


def _mute_word(needle: str, haystack: str) -> bool:
    """Whole-word containment; a needle full of regex characters is literal."""
    if not needle or not haystack:
        return False
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack) is not None


def notification_muted(app: str, summary: str, body: str, *,
                       mute_apps, app_name: str) -> bool:
    """New mute contract: user list matches app (+summary) with word-ish
    semantics (app substring, summary whole word, never body); self-mute
    when app==app_name or app_name appears as a WORD in summary/body."""
    try:
        a = (app or "").lower()
        s = (summary or "").lower()
        b = (body or "").lower()
        me = (app_name or "").lower()
        # SELF first: our own popups echo the app name in summary/body. Word
        # boundaries, because plain substring containment swallowed unrelated
        # notifications that merely happened to spell the name inside a longer
        # word -- "assistant manager update" was muted by an "assistant" app.
        if a.strip() == me:
            return True
        if _mute_word(me, s) or _mute_word(me, b):
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
        # MUTE, do not announce. The old `return False` made a mute list that
        # could not be evaluated indistinguishable from no mute list at all,
        # so a failure on this path read the notification aloud — the one
        # direction the list exists to prevent. A bug in the check is a reason
        # to say less, not more.
        log.exception("cannot evaluate the notification mute list")
        return True


class _MonitorRun:
    """One monitor + worker pair, registered while it owns the reader slot.

    The slot's occupant is a record rather than three attributes on the reader
    because the reader must be able to hand a DEAD occupant back to the
    registry and have it disposed of, without the registry ever touching a
    process or a thread itself.
    """

    __slots__ = ("thread", "proc", "stop")

    def __init__(self, thread, proc, stop) -> None:
        self.thread = thread
        self.proc = proc
        self.stop = stop

    def alive(self) -> bool:
        thread = self.thread
        return thread is not None and thread.is_alive()

    def dispose(self) -> None:
        """Signal stop, kill the monitor, join the worker — best effort.

        Used for both a live run (disable) and a reclaimed corpse (the worker
        already gave up, so this is usually just closing its pipe).
        """
        if self.stop is not None:
            self.stop.set()
        proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except (AttributeError, OSError):
                pass
        _join_worker(self.thread, "notification reader")


class NotificationReader:
    """Session D-Bus notification monitor with mute list and per-app cooldown.

    Notification text is never replayed from history; only future
    notifications are spoken. `announce(text)` speaks, `muted(app, summary,
    body)` filters, `popen_factory(*args, **kwargs)` spawns dbus-monitor,
    `persist(key, value)` records setting changes, `is_closed()` reports
    assistant shutdown.

    The reader owns exactly ONE slot, and the slot is handed out by
    ``BoundedRegistry``: "is a worker already alive?" and "may I start one?"
    used to be two separate reads with a process spawn between them, so two
    overlapping enables both saw a free slot and both started a reader — two
    dbus-monitors, two loops, one holding the other's stop event, every
    notification spoken twice. A worker that gave up is a corpse in the slot:
    the ``reclaim`` predicate takes it back under the same lock that grants the
    slot, which is what keeps "restart a dead reader" atomic too.
    """

    APP_COOLDOWN = 60.0  # one spoken digest per app per minute, max
    SLOT = "dbus-monitor"  # the one name the reader slot is registered under

    def __init__(self, *, spawn, is_closed, announce, muted, popen_factory,
                 persist) -> None:
        self._spawn = spawn
        self._is_closed = is_closed
        self._announce = announce
        self._muted = muted
        self._popen_factory = popen_factory
        self._persist = persist
        self._runs = BoundedRegistry("notification-reader", 1)
        self._cooldown_lock = threading.Lock()
        self._app_last: dict[str, float] = {}

    # -- the live run, as views onto the registered slot -----------------------
    # Kept as attributes' names because the assistant, the doctor and the tests
    # all ask "is a reader running, and on which monitor?".
    @property
    def _thread(self):
        run = self._runs.get(self.SLOT)
        return run.thread if run is not None else None

    @property
    def _proc(self):
        run = self._runs.get(self.SLOT)
        return run.proc if run is not None else None

    @property
    def _stop(self):
        run = self._runs.get(self.SLOT)
        return run.stop if run is not None else None

    def set_enabled(self, enabled: bool):
        """Start/stop the monitor; muted apps are filtered before TTS."""
        if enabled:
            if self._is_closed():
                return "ERROR: assistant is shut down"
            slot = self._runs.reserve(
                self.SLOT, reclaim=lambda run: not run.alive())
            if slot is None:
                # Not a cap refusal worth shouting about: "already on" is the
                # documented answer to a repeated toggle, unlike a job the user
                # asked for being turned away. The registry still counts it.
                log.info("notification reader already on")
                return "notification reader is already on"
            corpse = slot.reclaimed
            try:
                with slot:
                    try:
                        proc = self._popen_factory(
                            ["dbus-monitor", "--session",
                             "interface='org.freedesktop.Notifications',"
                             "member='Notify'"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1, start_new_session=True)
                    except FileNotFoundError:
                        self._persist("notification_reader", False)
                        return "ERROR: dbus-monitor is not installed"
                    except OSError as e:
                        self._persist("notification_reader", False)
                        return f"ERROR: notification monitor failed: {e}"
                    stop = threading.Event()
                    run = _MonitorRun(None, proc, stop)
                    try:
                        run.thread = self._spawn(
                            self.run, args=(stop, run),
                            name="notification-reader")
                    except Exception:
                        # A monitor nothing is reading must not be left
                        # running: its pipe would fill and it would outlive the
                        # reader that owns it.
                        run.proc = None
                        try:
                            proc.terminate()
                        except (AttributeError, OSError):
                            pass
                        log.exception("notification reader worker failed to start")
                        return "ERROR: notification reader failed to start"
                    slot.commit(run)
                log.info("desktop notification reader enabled")
                return "notification reader enabled"
            finally:
                if corpse is not None:
                    corpse.dispose()
        run = self._runs.get(self.SLOT)
        if run is not None:
            run.dispose()
            # Only now is the slot free. Releasing it before the worker is gone
            # is how a restart gets a second reader beside the old one — the
            # orphan the join exists to prevent — so a worker that outlived its
            # join budget keeps the slot, and the reclaim predicate frees it the
            # moment it actually dies.
            if not run.alive():
                self._runs.release(self.SLOT)
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

    def run(self, stop: threading.Event, run=None) -> None:
        """Production wrapper: run loop(), respawning dbus-monitor with
        bounded backoff when its stdout is exhausted. At most 5 respawns,
        1s→30s exponential backoff; gives up quietly when disabled.

        `run` is the registered slot record when this worker was started by
        set_enabled(); a direct call (tests, embedding) has none, and then there
        is no registered monitor to keep in step.
        """
        if run is None:
            run = self._runs.get(self.SLOT)
        backoff = 1.0
        attempts = 0
        while not stop.is_set() and attempts < 5:
            proc = run.proc if run is not None else None
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
                    if run is not None:
                        run.proc = proc
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
        # Out of respawns while still enabled: dbus-monitor is not coming
        # back, so the reader must stop claiming to be on. Leaving
        # notification_reader=True here was the worst state — the toggle and
        # the settings file said "on", nothing was listening, and no
        # notification would ever be spoken again.
        if not stop.is_set():
            log.error("notification reader gave up after %d monitor respawns — "
                      "turning it off", attempts)
            if run is not None:
                # The corpse stays registered: the slot is still occupied by
                # this (still-running) worker, and the reclaim predicate frees
                # it the instant it exits.
                run.proc = None
            try:
                self._persist("notification_reader", False)
            except Exception:
                log.exception("notification reader could not persist its stop")


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
        Every mutation of the queue goes through here.

        `mutate` returns the new list, or None to mean "I edited in place".
        Only None preserves the loaded list: an empty list is a real result
        (cancelling the last reminder), and `mutate(items) or items` used to
        resurrect it, silently no-opping cancel-all.
        """
        with self.lock, self._file_lock(self.path.parent, "reminders.json.lock"):
            items = self.load()
            result = mutate(items)
            if result is not None:
                items = result
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
                    # They are STILL ON DISK. Reporting them as taken would
                    # announce the same reminders again on the next boot — and
                    # every boot after that, since the prune never lands. Hand
                    # back nothing and let the next tick fire them normally.
                    return []
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
