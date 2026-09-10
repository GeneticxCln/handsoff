"""Minimal per-turn lifecycle primitives for the coordinator path.

This module is intentionally tiny: it extracts only the pure coordinator
primitives that were inlined in handsoff.py. No Qt, no Assistant, no globals,
no brain streaming state (that lives in core.brain.TurnStream).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


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


def next_turn(counter: list[int] | dict) -> TurnState:
    """Advance the mutable counter and return a fresh TurnState.

    Args:
        counter: A mutable container holding the generation counter.
                 Either a single-element list `[n]` or a dict `{"gen": n}`.

    Returns:
        A new TurnState with incremented generation and fresh cancel/done events.
    """
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