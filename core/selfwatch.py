"""The agent watching itself.

Every other loop in this app monitors a *consumer*: the mic reporter watches
the capture stream, the reminder worker watches the clock, the settings
watcher watches the file. Nobody watched the watchers — the long-lived threads
that make the agent an agent — and a thread that dies or wedges today fails
silently, because the health payload is pull-only: something has to ask. This
module is the push half: a sampler, owned by the Assistant, that reads the
process's own functional state and turns raw facts into a small number of
findings:

* a long-lived thread that was seen once and is gone (``dead``)
* a long-lived thread whose progress probe stopped changing (``wedged``)

Findings reach the user through three channels — a WARNING journal line, a
desktop notification, and a spoken announcement — each with its own cooldown,
because a *repeated* alarm is its own kind of failure. The sampler owns
*deciding*; the host owns *saying* (via :meth:`SelfWatch.pending_announcements`
and :func:`announcement_text`).

Dependency-free leaf: stdlib only, no handsoff globals. Everything it needs —
what to watch, what to ask, what to say — arrives as arguments, which is what
lets tests drive it against fake threads and fake clocks.
"""
from __future__ import annotations

import threading
import time

__all__ = [
    "ANNOUNCE_COOLDOWN_S",
    "LONG_LIVED_PREFIXES",
    "WEDGED_AFTER_S",
    "SelfWatch",
    "announcement_text",
]

# Threads the agent cannot function without, matched by exact name or name
# prefix. One-shot workers ("selfheal-tts", "announce", "ptt-stop",
# "brain-call", "crash-report", "dns-resolve") deliberately match none of
# these: they are born, run and die inside a single turn, and counting them
# would make the inventory flap round to round.
LONG_LIVED_PREFIXES = (
    "mic-health",            # the mic reporter (spawned once per listener)
    "pipeline",              # the audio -> transcription worker
    "loader",                # the model loader
    "reminders",             # the reminder worker
    "settings-watch",        # the settings file watcher
    "control",               # the control socket server
    "notification-reader",   # the dbus notification reader
    "pomodoro",              # the pomodoro timer
)

# Wedge threshold: a watched component whose probe value has not changed for
# this long is reported wedged. Generous on purpose — a false "wedged" that
# speaks is worse than a late one.
WEDGED_AFTER_S = 120.0
# Spoken/notify cooldown per finding: a standing failure re-speaks at this
# cadence (so a recurring alarm is heard without becoming spam), and a
# recovered finding says nothing more.
ANNOUNCE_COOLDOWN_S = 300.0


def _now() -> float:
    return time.monotonic()


class SelfWatch:
    """One self-watch sampler. No public method raises.

    Lifecycle: the Assistant constructs one and calls :meth:`tick` from its
    own long-lived loop (spawned once per process, next to the mic reporter).
    ``sync_fns`` maps a watched prefix to the name of an assistant method
    whose *return value* is that component's progress signal: a value that
    changes whenever the component is doing its job (a counter, a clock, a
    generation number). A component with no probe is still watched for
    liveness — only wedging needs a probe.
    """

    def __init__(self, *, prefixes=LONG_LIVED_PREFIXES, sync_fns=None,
                 wedged_after: float = WEDGED_AFTER_S,
                 announce_cooldown: float = ANNOUNCE_COOLDOWN_S,
                 clock=None) -> None:
        self._prefixes = tuple(prefixes)
        self._sync_fns = dict(sync_fns or {})
        self._wedged_after = float(wedged_after)
        self._announce_cooldown = float(announce_cooldown)
        self._clock = clock or _now
        self._lock = threading.Lock()
        # prefix -> first-seen monotonic. The first ticks LEARN the baseline
        # instead of alarming on it: the sampler may start after some
        # components, and a thread absent at t0 is not yet "dead".
        self._seen: dict[str, float] = {}
        self._alive_now: set[str] = set()   # prefixes live as of the last tick
        # prefix -> (repr of last probe value, when it last changed)
        self._probe_last: dict[str, tuple[str, float]] = {}
        # prefix -> {"kind": "dead"|"wedged"|None, "since": float|None,
        #            "announced_at": float, "cleared_at": float|None}
        self._findings: dict[str, dict] = {}
        self.last_tick_error: str | None = None
        self.ticks = 0

    # -- sampling ------------------------------------------------------------

    def _inventory(self) -> list[tuple[str, str]]:
        """(prefix, full-name) for every live long-lived thread."""
        out: list[tuple[str, str]] = []
        for t in threading.enumerate():
            name = t.name or ""
            for p in self._prefixes:
                if name == p or name.startswith((p + "-", p + "_")):
                    out.append((p, name))
                    break
        return out

    def _probe(self, assistant, prefix: str):
        """One component's progress value, or None when unprobeable."""
        attr = self._sync_fns.get(prefix)
        if not attr:
            return None
        method = getattr(assistant, attr, None)
        if method is None:
            return None
        try:
            return method()
        except Exception:  # noqa: BLE001 - a probe must not kill the sampler
            return None

    # -- findings (all mutations under self._lock) ----------------------------

    def _open_finding(self, prefix: str, kind: str, now: float) -> None:
        with self._lock:
            f = self._findings.get(prefix)
            if f is None or f.get("kind") != kind:
                self._findings[prefix] = {"kind": kind, "since": now,
                                          "announced_at": 0.0,
                                          "cleared_at": None}
            else:
                f["since"] = f.get("since") or now

    def _close_finding(self, prefix: str, now: float) -> None:
        with self._lock:
            f = self._findings.get(prefix)
            if f is not None and f.get("kind") is not None:
                f["kind"] = None
                f["since"] = None
                f["cleared_at"] = now

    def _update_liveness(self, alive: set[str], now: float) -> None:
        """Dead = previously seen, now absent. First sight only baselines."""
        with self._lock:
            self._alive_now = set(alive)
            known = set(self._seen)
        for prefix in known:
            if prefix in alive:
                with self._lock:
                    self._seen[prefix] = now
                f_kind = None
                with self._lock:
                    f_kind = (self._findings.get(prefix) or {}).get("kind")
                if f_kind == "dead":
                    self._close_finding(prefix, now)
            else:
                self._open_finding(prefix, "dead", now)
        for prefix in alive - known:
            with self._lock:
                self._seen[prefix] = now

    def _update_wedged(self, assistant, alive: set[str], now: float) -> None:
        """Wedged = probe value unchanged for wedged_after seconds."""
        for prefix in sorted(alive):
            val = self._probe(assistant, prefix)
            if val is None:
                continue
            key = repr(val)
            with self._lock:
                last = self._probe_last.get(prefix)
                if last is None or last[0] != key:
                    self._probe_last[prefix] = (key, now)
                    stale_since = now
                else:
                    stale_since = last[1]
            if now - stale_since > self._wedged_after:
                self._open_finding(prefix, "wedged", now)
            else:
                f_kind = None
                with self._lock:
                    f_kind = (self._findings.get(prefix) or {}).get("kind")
                if f_kind == "wedged":
                    self._close_finding(prefix, now)
        # probes of components that are gone stop being tracked
        with self._lock:
            for prefix in list(self._probe_last):
                if prefix not in alive:
                    self._probe_last.pop(prefix, None)

    # -- public ----------------------------------------------------------------

    def tick(self, assistant) -> dict:
        """Sample once. Returns the JSON-ready snapshot for ``--ptt health``.

        Never raises: a sampler that could take down its host loop would be
        the exact failure it exists to catch.
        """
        try:
            return self._tick(assistant)
        except Exception as e:  # noqa: BLE001
            self.last_tick_error = f"{type(e).__name__}: {e}"
            return {"error": self.last_tick_error, "ticks": self.ticks}

    def _tick(self, assistant) -> dict:
        now = self._clock()
        inventory = self._inventory()
        alive = {p for p, _name in inventory}
        self._update_liveness(alive, now)
        self._update_wedged(assistant, alive, now)
        self.ticks += 1
        with self._lock:
            findings_now = {p: (f.get("kind") if f else None)
                            for p, f in self._findings.items()}
        watched = {}
        for p in self._prefixes:
            watched[p] = {
                "alive": p in alive,
                "finding": findings_now.get(p),
            }
        return {
            "watched": watched,
            "ticks": self.ticks,
        }

    def snapshot_health(self, state: str | None,
                        state_age_s: float | None) -> dict:
        """The ``self_watch`` section of ``--ptt health``. Never raises."""
        try:
            with self._lock:
                findings_now = {p: (f.get("kind") if f else None)
                                for p, f in self._findings.items()}
            watched = {}
            for p in self._prefixes:
                watched[p] = {
                    "alive": p in self._alive_now,
                    "finding": findings_now.get(p),
                }
            return {
                "watched": watched,
                "state": state,
                "state_age_s": state_age_s,
                "ticks": self.ticks,
                "last_error": self.last_tick_error,
            }
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    # -- announcements -----------------------------------------------------------

    def pending_announcements(self) -> list[dict]:
        """Findings due to be spoken/notified, marked announced on return.

        Cooldown per finding; the caller's only job is to say the sentence.
        Never raises.
        """
        out: list[dict] = []
        try:
            now = self._clock()
            with self._lock:
                for prefix, f in sorted(self._findings.items()):
                    kind = f.get("kind")
                    if kind is None:
                        continue
                    if f.get("announced_at") and \
                            now - f["announced_at"] < self._announce_cooldown:
                        continue
                    f["announced_at"] = now
                    out.append({"prefix": prefix, "kind": kind,
                                "since_s": (round(now - f["since"], 1)
                                            if f.get("since") else None)})
        except Exception:  # noqa: BLE001
            return out
        return out


def announcement_text(finding: dict) -> str:
    """The sentence a finding speaks. Pure function, test-pinned."""
    prefix = str(finding.get("prefix") or "component").replace("-", " ")
    kind = str(finding.get("kind") or "")
    if kind == "dead":
        return (f"Heads up: my {prefix} thread is not running anymore — some "
                f"things may not respond until I am restarted.")
    if kind == "wedged":
        return (f"Heads up: my {prefix} has not responded for a while and "
                f"may be stuck.")
    return f"Heads up: my {prefix} is degraded."
