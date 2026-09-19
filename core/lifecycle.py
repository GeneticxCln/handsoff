"""Minimal per-turn lifecycle primitives for the coordinator path.

This module is intentionally tiny: it extracts only the pure coordinator
primitives that were inlined in handsoff.py. No Qt, no Assistant, no globals,
no brain streaming state (that lives in core.brain.TurnStream).

It is also the home of the turn generation counter. The counter's storage
(`GenerationCounter`), the lock that makes a claim atomic, and the single
increment that advances it (`next_turn`) all live here, so with `claim()` there
is exactly one place that can hand out a generation — which matters because the
staleness checks (`gen != self._gen`) and the gen-keyed transcript cache trust
that number, and a duplicate lets one utterance be answered with another's
text.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

__all__ = ["TurnState", "GenerationCounter", "new_counter", "next_turn"]


@dataclass(slots=True)
class TurnState:
    """Per-turn state for the coordinator/orchestration path.

    Fields:
        generation: Monotonically increasing turn number.
        cancel: Event to signal cancellation of this turn's work.
        done: Event set when the turn's work completes.
        result: Arbitrary result payload from the turn's work.
    """
    generation: int
    cancel: threading.Event
    done: threading.Event
    result: Any = None


_COUNTER_LOCK = threading.Lock()


def new_counter(start: int = 0) -> dict:
    """Create the mutable container `next_turn` advances.

    The shape is this module's contract, not the caller's: `next_turn` advances
    the `"gen"` key, so a caller that hand-rolled `{"generation": n}` would be
    handed generation 0 for every turn. Hold one container per turn stream.

    The container is a mutable object rather than an int attribute because
    `counter["gen"] += 1` is read-add-store at the call site, which is the race
    the increment lock exists to remove.
    """
    return {"gen": int(start)}


class GenerationCounter:
    """One turn stream's generation counter.

    `claim()` is the only way to advance it, and it returns the whole
    `TurnState` (generation plus the fresh cancel/done events) so the increment
    and the events it belongs to are built inside one critical section.
    `value` is what the staleness checks read, and is settable for callers that
    rebase the stream (tests plant a generation; nothing in the bubble does).

    Plain class, not a dataclass: the storage is a detail, and `claim()` is the
    interface.
    """

    __slots__ = ("_box",)

    def __init__(self, start: int = 0) -> None:
        self._box = new_counter(start)

    @property
    def value(self) -> int:
        return int(self._box.get("gen", 0))

    @value.setter
    def value(self, new: int) -> None:
        # Under the same lock as claim(), because this writes the one field the
        # staleness checks trust while `claim()` is a read-modify-write of it.
        # Unlocked, a planted value could land between claim()'s read and its
        # store and two turns would be handed the same generation — the exact
        # collision the lock exists to prevent. Tests are the only caller today
        # (nothing in the bubble rebases a stream), which is why this was a
        # convention rather than an invariant; now it is the latter.
        with _COUNTER_LOCK:
            self._box["gen"] = int(new)

    def claim(self) -> TurnState:
        """Advance this counter and return a fresh TurnState for the turn."""
        return next_turn(self._box)


def next_turn(counter: list[int] | dict) -> TurnState:
    """Advance the mutable counter and return a fresh TurnState.

    Args:
        counter: A mutable container holding the generation counter.
                 Either a single-element list `[n]` or a dict `{"gen": n}`.
                 `new_counter()` makes the dict shape.

    Returns:
        A new TurnState with incremented generation and fresh cancel/done events.

    The increment and the read are one critical section. `counter[0] += 1` is
    load-add-store and the generation is what the staleness checks and the
    gen-keyed transcript cache trust, so two concurrent callers must never be
    handed the same value — one utterance would then be answered with another's
    text. The lock is module-level because the counter container belongs to the
    caller and one call may be made per container from different threads.
    """
    with _COUNTER_LOCK:
        if isinstance(counter, list):
            counter[0] += 1
            gen = counter[0]
        else:
            counter["gen"] = counter.get("gen", 0) + 1
            gen = counter["gen"]
    return TurnState(
        generation=gen,
        cancel=threading.Event(),
        done=threading.Event(),
        result=None,
    )
