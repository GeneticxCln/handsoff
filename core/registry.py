"""Admission control for the bubble's shared registries.

The audits kept re-finding one shape, in a new place each time:

    check the cap while holding the lock  ->  release the lock  ->  do the
    slow thing that creates the resource (fork/exec, spawn a thread, resolve
    a path)  ->  insert WITHOUT re-checking.

Two overlapping callers then both see room and both insert, so a "bounded"
resource is unbounded — measured at seven jobs against a cap of four. The same
family appears twice more:

  * as an OFFER arm — ``offer.clear()`` then ``offer.update(...)`` is two
    steps, and a reader landing between them sees an EMPTY offer for one that
    exists, or pairs one call's payload with another's deadline;
  * as an OFFER read — the deadline is checked at each call site, so a
    consumer that forgets it acts on a window that has already closed.

``BoundedRegistry`` is now the only place a capacity is enforced, and ``Offer``
the only place an offer is armed, read and consumed. A caller describes WHAT
it wants to create; the helper decides WHETHER it may be admitted. The lock
never leaves the helper's hands, so there is no caller-visible lock to release
at the wrong moment — which was the defect.

The third shape is the SINGLETON whose occupant can die: a notification
monitor, a diagnostic worker. There "is the worker still alive?" and "may I
start one?" were two separate reads in the caller, under two separate locks
(none), so two callers both saw a dead slot and both started a worker. That
question is a `reclaim` predicate, evaluated where the slot is handed out.
"""
from __future__ import annotations

import threading
import time

__all__ = ["BoundedRegistry", "Offer"]


class _Reservation:
    """One counted slot in a :class:`BoundedRegistry`, held until settled.

    Usable as a context manager: leaving the block without ``commit()``
    cancels, so a prepare step that raises (a failed ``Popen``, a bad pattern)
    cannot leak the slot it was holding.

    ``reclaimed`` is the occupant a ``reclaim`` predicate judged dead, handed
    back so the caller disposes of it OUTSIDE the lock (closing a pipe, joining
    a thread). It is deliberately not disposed here: a lock the rest of the
    program can block on is exactly what this class exists to keep out of
    teardown paths.
    """

    __slots__ = ("_registry", "key", "replace", "reclaimed", "_settled",
                 "_released", "_result")

    def __init__(self, registry: "BoundedRegistry", key, replace: bool) -> None:
        self._registry = registry
        self.key = key
        self.replace = replace
        self.reclaimed = None
        self._settled = False
        self._result = None
        # Set when this reservation's slot has been given back (by commit OR
        # cancel). It guards the double-release: a second commit() on one
        # reservation used to decrement `_held` again, inventing capacity the
        # cap thinks it still has.
        self._released = False

    def commit(self, build):
        """Register the resource and return ``(key, displaced)``.

        ``build`` is either the entry itself or a callable receiving the key.
        A callable runs under the registry's lock, so minting a key (a job
        sequence number) and inserting are one step and two callers can never
        be handed the same key. ``displaced`` is whatever this commit replaced
        — the caller disposes of it *outside* the lock.
        """
        if self._settled:
            # Idempotent, deliberately. Committing twice used to register a
            # SECOND resource for ONE reservation — which the cap counted once,
            # so the extra entry was never released and never counted, and the
            # cap silently admitted an extra. Returning the first result keeps
            # a teardown that settles twice from inventing anything.
            return self._result
        result = self._registry._commit(self, build)
        self._settled = True
        self._result = result
        return result

    def cancel(self) -> None:
        """Give the slot back without registering anything."""
        if not self._settled:
            self._settled = True
            self._registry._cancel(self)

    def __enter__(self) -> "_Reservation":
        return self

    def __exit__(self, *_exc) -> bool:
        self.cancel()
        return False


class BoundedRegistry:
    """A name->entry map with a hard capacity, enforced at admission.

    The capacity is checked while the lock is held, a reservation is counted
    against it IMMEDIATELY, and the slow preparation runs while holding that
    reservation — so a second caller cannot be told there is room the first is
    about to take. ``reserve()`` is the only way in and ``commit()`` the only
    way an entry appears.
    """

    def __init__(self, name: str, cap: int, lock=None, clock=time.time) -> None:
        if cap < 0:
            raise ValueError("cap must be >= 0")
        self.name = name
        self._cap = int(cap)
        # An injected lock lets two registries share one critical section
        # (the file and process watchers are stopped together, so a watcher
        # must not be able to start between the two clears).
        self._lock = lock if lock is not None else threading.Lock()
        # Wall clock, unlike Offer's monotonic one: a refusal is a journal
        # entry that has to be comparable across processes and restarts.
        self._clock = clock
        self._items: dict = {}
        self._held = 0          # reservations in flight, counted against the cap
        self._auto = 0          # auto keys minted for this registry
        self._refusals = 0      # admissions this registry has turned away
        self._last_refusal: dict | None = None

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def refusals(self) -> int:
        """How many admissions this registry has refused since it was built.

        A refusal is the one in-the-wild signal that a cap is being hit. It is
        counted HERE rather than by each caller because this is the only place
        that knows the decision was made — which is the same reason admission
        lives here, and why a future registry cannot forget to report one.
        """
        with self._lock:
            return self._refusals

    def refusal_report(self) -> dict | None:
        """The most recent refusal as a copy, or None.

        Keys: registry, cap, held, reserved, occupants, count, at. Deliberately
        a plain dict the caller can log or persist without reaching back into
        the registry, and deliberately taken under the lock — the alternative
        was a caller reading len()/keys() after the fact and reporting a
        different instant than the one that was refused.
        """
        with self._lock:
            return dict(self._last_refusal) if self._last_refusal else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._items

    def __getitem__(self, key):
        with self._lock:
            return self._items[key]

    def __iter__(self):
        return iter(self.snapshot())

    def get(self, key, default=None):
        with self._lock:
            return self._items.get(key, default)

    def snapshot(self) -> dict:
        """Shallow copy of the registered entries (safe to iterate)."""
        with self._lock:
            return dict(self._items)

    def keys(self) -> list:
        return sorted(self.snapshot())

    def values(self) -> list:
        return list(self.snapshot().values())

    def items(self) -> list:
        return list(self.snapshot().items())

    def release(self, key):
        """Remove and return one entry (``None`` when it was not registered)."""
        with self._lock:
            return self._items.pop(key, None)

    def clear(self) -> list:
        """Remove every entry and return them, for the caller to dispose."""
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
            return items

    def room(self, key=None, replace: bool = False) -> bool:
        """Whether one more entry could be admitted *right now*.

        Advisory only: a caller that acts on the answer is back in the
        check-then-act race this class exists to remove. It is here for
        reporting and tests — admission goes through :meth:`reserve`.
        """
        with self._lock:
            return self._has_room(key, replace)

    def _has_room(self, key, replace: bool) -> bool:
        if replace and key is not None and key in self._items:
            return True        # re-registering a name never costs a second slot
        return len(self._items) + self._held < self._cap

    def reserve(self, key=None, replace: bool = False, reclaim=None):
        """Reserve one slot, or ``None`` when there is no room.

        ``replace=True`` treats an existing entry with that key as room, so a
        re-registration of the same name is allowed at the cap and simply
        displaces the old entry.

        ``reclaim`` is a predicate for a slot whose occupant may be DEAD — a
        worker that crashed, a monitor that gave up. It is evaluated under the
        same lock that hands the slot out, so "is the occupant alive?" and "may
        I have its slot?" are one step: the false answer to each is the same
        refusal, and there is no window between them for a second caller. A
        reclaimed occupant is returned on the reservation (``.reclaimed``) for
        the caller to dispose of after the lock is released.

        A refusal is recorded before it is returned — count, and the shape of
        the moment (cap, how many were held, which names held them) — so the
        caller can report it instead of the refusal being visible only in
        whatever the caller chose to tell the user.
        """
        with self._lock:
            corpse = None
            if reclaim is not None and key is not None and key in self._items:
                entry = self._items[key]
                # Judged BEFORE it is dropped: a predicate that raises must
                # leave the registry exactly as it found it, and popping a
                # corpse can only free the slot it was holding (entries +
                # reservations never exceed the cap), so the room check below
                # cannot fail because of this.
                if reclaim(entry):
                    del self._items[key]
                    corpse = entry
            if not self._has_room(key, replace):
                self._refusals += 1
                self._last_refusal = {
                    "registry": self.name,
                    "cap": self._cap,
                    "held": len(self._items),
                    "reserved": self._held,
                    "occupants": sorted(self._items),
                    "count": self._refusals,
                    "at": self._clock(),
                }
                return None
            self._held += 1
        reservation = _Reservation(self, key, replace)
        reservation.reclaimed = corpse
        return reservation

    def refusal_line(self, detail: str) -> str:
        """One journal-ready sentence describing the last refusal.

        The wording lives here, once, because it is rendered from three places
        (the tool belt, the control server, the doctor) that must not drift
        into three accounts of the same event.
        """
        report = self.refusal_report() or {}
        return (f"{self.name} at {report.get('held', 0)}/"
                f"{report.get('cap', self._cap)} held — {detail} "
                f"(nothing was created)")

    def _commit(self, reservation: _Reservation, build):
        with self._lock:
            key = reservation.key
            if key is None:
                self._auto += 1
                key = f"{self.name}-{self._auto}"
            displaced = self._items.get(key)
            self._items[key] = build(key) if callable(build) else build
            self._release(reservation)
            return key, displaced

    def _release(self, reservation: "_Reservation") -> None:
        """Give back one reservation's slot — at most once, ever."""
        if not reservation._released:
            reservation._released = True
            self._held = max(0, self._held - 1)

    def _cancel(self, reservation: "_Reservation | None" = None) -> None:
        with self._lock:
            if reservation is None:
                self._held = max(0, self._held - 1)
            else:
                self._release(reservation)


class Offer:
    """One expiring offer, armed/read/consumed as a single step.

    ``arm()`` replaces whatever was armed in ONE assignment under the lock. The
    ``clear()`` + ``update()`` pair it replaces left a window in which a reader
    saw an EMPTY offer for one that exists, and it could pair one caller's
    payload with another's deadline. Reads go through :meth:`state` and
    :meth:`consume`, which apply the deadline themselves: a consumer cannot
    forget it, and cannot act on a window that has closed.

    The ``bool``/``len``/``[]``/``in``/``keys``/``get`` surface is read-only
    observability (logging, tests). It deliberately has no mutators, so nothing
    outside this class can arm, clear or expire an offer without going through
    the deadline-aware methods.
    """

    def __init__(self, name: str, clock=time.monotonic, lock=None) -> None:
        self.name = name
        self._clock = clock
        self._lock = lock if lock is not None else threading.Lock()
        self._fields: dict = {}

    def __repr__(self) -> str:
        fields = self.snapshot_state()
        return f"Offer({self.name!r}, {'empty' if not fields else fields})"

    def arm(self, window: float, **fields) -> None:
        """Replace the current offer with ``fields``, live for ``window`` s.

        A negative window arms an already-expired offer, which is how a caller
        invalidates one it no longer wants honoured. The clock is read INSIDE
        the lock, so the deadline is measured when the offer actually becomes
        visible rather than when the caller started queueing for it.
        """
        with self._lock:
            self._fields = {**fields, "until": self._clock() + float(window)}

    def arm_unless(self, predicate, window: float, **fields) -> bool:
        """Arm only when no LIVE offer satisfies ``predicate``.

        The read-the-offer-then-maybe-write-it sequence is one step, so a
        repeated request cannot extend a window a caller deliberately left
        alone (a model looping on the same tool call must not hold its own
        confirmation open forever), and two callers cannot both decide to arm.
        Returns True when it armed.
        """
        with self._lock:
            live, _expired = self._read_locked()
            if live is not None and predicate(live):
                return False
            self._fields = {**fields, "until": self._clock() + float(window)}
            return True

    def clear(self) -> None:
        with self._lock:
            self._fields = {}

    def expire(self) -> None:
        """Drag the live offer's deadline into the past."""
        with self._lock:
            if self._fields:
                self._fields = {**self._fields, "until": self._clock() - 1.0}

    def state(self):
        """``(offer, expired)`` for the armed offer.

        * ``(None, False)`` — nothing is armed;
        * ``(None, True)`` — something was armed and its window has closed; the
          stale offer is cleared here, so a caller that only checks for ``None``
          still cannot act on it;
        * ``(dict, False)`` — the live offer, NOT consumed (a copy).
        """
        with self._lock:
            return self._read_locked()

    def consume(self):
        """The live offer, consumed — ``None`` when absent or already stale.

        This is the claim: whichever caller gets the dict owns the offer, and
        the one that gets ``None`` must refuse. Two confirmations therefore
        cannot both run the same action.
        """
        with self._lock:
            live, _expired = self._read_locked()
            self._fields = {}
            return live

    def _read_locked(self):
        """Caller holds the lock: ``(live_copy_or_None, expired)``."""
        if not self._fields:
            return (None, False)
        if self._clock() >= self._fields["until"]:
            self._fields = {}
            return (None, True)
        return (dict(self._fields), False)

    # -- state restoration (tests hand each other the same module state) ------
    def snapshot_state(self):
        """Raw armed fields, without deadline filtering."""
        with self._lock:
            return dict(self._fields) or None

    def restore_state(self, fields) -> None:
        with self._lock:
            self._fields = dict(fields or {})

    # -- read-only views ------------------------------------------------------
    def __bool__(self) -> bool:
        return self.state()[0] is not None

    def __len__(self) -> int:
        return 1 if self else 0

    def __contains__(self, key) -> bool:
        live = self.state()[0]
        return bool(live) and key in live

    def __getitem__(self, key):
        live = self.state()[0]
        if not live:
            raise KeyError(key)
        return live[key]

    def get(self, key, default=None):
        live = self.state()[0]
        return default if not live else live.get(key, default)

    def keys(self) -> list:
        live = self.state()[0]
        return sorted(live) if live else []
