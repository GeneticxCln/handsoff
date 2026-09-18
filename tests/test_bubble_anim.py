"""The bubble's animation tick, driven by hand instead of by the clock.

Why this file exists. `_on_tick` was reached only by the 16 ms QTimer a
constructed widget starts in `__init__`, so whether the suite covered its body
depended on whether some test's event loop happened to spin for 16 ms. Three gate
runs of the same tree read 2468 / 2483 / 2468 missing statements, and diffing two
full `--cov-report=term-missing` tables put every one of those lines here — the
whole tick body. A timer is the wrong instrument for a BEHAVIOUR test anyway:
`dt` came from `QElapsedTimer`, so the smoothing constants were never checked
against a known step, and a test that waits for frames asserts whatever the
machine's scheduler produced.

So the tick is driven the way the widget drives it — one `_on_tick()` per frame —
with a clock the test advances by hand. No QApplication is needed: the tick reads
instance state, does arithmetic and asks for one repaint, so a `__new__`-style
instance whose only Qt call is overridden is the whole harness (the same shape
tests/test_design_packs.py uses for the design painters).

The closed forms below (`_attack_step`, `_release_step`) are this file's
SPECIFICATION of the smoothing, not a transcription of the method: "frame-rate
independent exponential chase, 24/s attacking and 7/s releasing". A test that
re-derived them from the implementation would agree with every change to it,
including the wrong ones.
"""
from __future__ import annotations

import math

import pytest

from conftest import core_module

# The window size the geometry is derived from. 144 is the shipped default, and
# the assertions below read BUBBLE_R0/GEOM_K from the module rather than
# restating them, so a change of default size does not rewrite this file.
SIZE = 144


@pytest.fixture()
def bubble():
    """`core.bubble` with real geometry, restored afterwards.

    Geometry starts at ZERO at import (`BUBBLE_R0 = 0.0   # configure()`), and
    `configure()` is its only writer — a tick test on the import-time values
    would compare zeros with zeros and pass while asserting nothing.
    """
    mod = core_module("bubble")
    names = ("WINDOW_PX", "BUBBLE_R0", "GLOW_PAD", "GEOM_K", "APERTURE_R",
             "BUBBLE_ACCENT", "ANIM_ENERGY")
    snapshot = {name: getattr(mod, name) for name in names}
    mod.configure({"bubble_size": SIZE})
    try:
        yield mod
    finally:
        for name, value in snapshot.items():
            setattr(mod, name, value)


class Clock:
    """The one method `_on_tick` calls on its `QElapsedTimer`, in milliseconds."""

    def __init__(self, ms: float = 0.0) -> None:
        self.ms = float(ms)

    def elapsed(self) -> float:
        return self.ms

    def advance(self, ms: float) -> None:
        self.ms += float(ms)


def harness(bubble, clock=None, *, state="idle", level=0.0, energy=None):
    """A BubbleWidget with no window, no timer and a frame counter.

    `BubbleWidget.__init__` cannot run here: it is all Qt (window flags, a fixed
    size, a started QTimer) and needs a QApplication. The tick needs none of
    that, so the harness is a subclass whose `__init__` never calls Qt and whose
    `update` — the tick's ONLY Qt call — counts frames instead of asking for a
    paint.
    """
    clock = Clock() if clock is None else clock
    idle = bubble.STATE_COLORS[bubble.IDLE]
    colour = [idle.redF(), idle.greenF(), idle.blueF()]

    class _Harness(bubble.BubbleWidget):
        def __init__(self):
            self._clock = clock
            self._last_tick = clock.elapsed() / 1000.0
            self._state = state
            self._level_target = level
            self._level_ui = level
            self._color_ui = list(colour)
            self._energy_ui = bubble._fx_energy(state) if energy is None else energy
            self._radius_ui = None
            self._radius_vel = 0.0
            self.frames = 0

        def update(self):                       # no Qt: count the frame
            self.frames += 1

    return _Harness()


def _attack_step(level: float, target: float, dt: float) -> float:
    """One frame of the exponential chase: fast when the voice rises."""
    return level + (target - level) * (1.0 - math.exp(-dt * 24.0))


def _release_step(level: float, target: float, dt: float) -> float:
    """...gentle when it falls, so a quiet syllable does not flicker."""
    return level + (target - level) * (1.0 - math.exp(-dt * 7.0))


def _crossfade(value: float, target: float, dt: float) -> float:
    """One frame of the 5/s state-colour crossfade.

    `_on_tick` applies this per channel. A mutation sweep is what put the exact
    rate here: the first draft of these tests pinned only the SHAPE (moves, does
    not overshoot, converges), and doubling the rate passed every one of them —
    the crossfade got twice as fast and nothing said so.
    """
    return value + (target - value) * (1.0 - math.exp(-dt * 5.0))


def _energy_step(energy: float, target: float, dt: float) -> float:
    """One frame of the 4/s glow-energy chase."""
    return energy + (target - energy) * (1.0 - math.exp(-dt * 4.0))


# -- the step itself ---------------------------------------------------------

def test_a_frame_step_is_the_clock_in_seconds(bubble):
    clock = Clock()
    w = harness(bubble, clock=clock)
    w._level_target = 1.0

    clock.advance(20)                       # 20 ms, the QTimer's own ballpark
    w._on_tick()

    assert w._last_tick == pytest.approx(0.02), "elapsed() is milliseconds"
    assert w._level_ui == pytest.approx(_attack_step(0.0, 1.0, 0.02))


def test_a_stalled_frame_never_teleports_the_smoothing(bubble):
    clock = Clock()
    w = harness(bubble, clock=clock)
    w._level_target = 1.0

    clock.advance(5000)                     # the app was busy for five seconds
    w._on_tick()
    assert w._level_ui == pytest.approx(_attack_step(0.0, 1.0, 0.05)), (
        "a five-second gap must be ONE 50 ms frame, not the whole gap")

    clock.advance(20)                       # and the gap is CONSUMED: the next
    w._on_tick()                            # frame steps from the new now
    assert w._level_ui == pytest.approx(_attack_step(
        _attack_step(0.0, 1.0, 0.05), 1.0, 0.02)), (
        "a stale `_last_tick` would charge the whole gap again on every frame")


def test_two_ticks_in_the_same_instant_are_not_dead_frames(bubble):
    clock = Clock()
    w = harness(bubble, clock=clock)
    w._level_target = 1.0

    w._on_tick()
    first = w._level_ui
    w._on_tick()                            # no clock advance at all
    assert first == pytest.approx(_attack_step(0.0, 1.0, 0.001))
    assert w._level_ui > first, "a zero-length frame must still advance the chase"
    assert w._level_ui == pytest.approx(_attack_step(first, 1.0, 0.001))


# -- what the step is applied to ---------------------------------------------

def test_the_level_attacks_faster_than_it_releases(bubble):
    clock = Clock()
    rising = harness(bubble, clock=clock, level=0.0)
    falling = harness(bubble, clock=clock, level=1.0)
    rising._level_target = 1.0
    falling._level_target = 0.0

    clock.advance(20)
    rising._on_tick()
    falling._on_tick()

    assert rising._level_ui == pytest.approx(_attack_step(0.0, 1.0, 0.02))
    assert falling._level_ui == pytest.approx(_release_step(1.0, 0.0, 0.02))
    rise = rising._level_ui
    fall = 1.0 - falling._level_ui
    assert rise > 2 * fall, (rise, fall)


def test_the_colour_crossfades_and_never_passes_the_target(bubble):
    clock = Clock()
    w = harness(bubble, clock=clock)
    target = bubble.STATE_COLORS[bubble.SPEAKING]
    want = (target.redF(), target.greenF(), target.blueF())
    start = list(w._color_ui)
    w._state = bubble.SPEAKING

    seen = [list(start)]
    for _ in range(80):
        clock.advance(16)
        w._on_tick()
        seen.append(list(w._color_ui))

    for i, (a, b) in enumerate(zip(start, want)):
        if abs(a - b) < 1e-9:
            continue
        lo, hi = sorted((a, b))
        for frame in seen:
            assert lo - 1e-9 <= frame[i] <= hi + 1e-9, (
                f"channel {i} overshot its target: {frame[i]} outside {(lo, hi)}")
        assert seen[1][i] != pytest.approx(a), "the first frame must MOVE"
        assert abs(seen[1][i] - a) < 0.25 * abs(b - a), (
            "a quarter of the way in one frame is a pop, not a crossfade")
        expected = a
        for number, frame in enumerate(seen[1:], start=1):
            expected = _crossfade(expected, b, 0.016)
            assert frame[i] == pytest.approx(expected, abs=1e-12), (
                f"channel {i} at frame {number} is off the 5/s crossfade")
        assert seen[-1][i] == pytest.approx(b, abs=0.01), (i, seen[-1][i], b)


def test_an_unknown_state_name_falls_back_to_the_idle_colour(bubble):
    clock = Clock()
    w = harness(bubble, clock=clock)
    idle = bubble.STATE_COLORS[bubble.IDLE]
    w._state = "no-such-state"

    for _ in range(80):
        clock.advance(16)
        w._on_tick()                        # must not raise

    assert w._color_ui == pytest.approx(
        [idle.redF(), idle.greenF(), idle.blueF()], abs=0.01)


def test_the_energy_chases_the_state_without_passing_it(bubble):
    clock = Clock()
    w = harness(bubble, clock=clock, energy=0.0)
    w._state = bubble.THINKING
    want = bubble._fx_energy(bubble.THINKING)
    assert want > 0.0

    first = None
    expected = 0.0
    for number in range(1, 81):
        clock.advance(16)
        w._on_tick()
        expected = _energy_step(expected, want, 0.016)
        if first is None:
            first = w._energy_ui
        assert w._energy_ui <= want + 1e-9, "energy must not overshoot its target"
        assert w._energy_ui == pytest.approx(expected, abs=1e-12), (
            f"the energy chase left the 4/s curve at frame {number}")
    assert first < 0.25 * want, "the first frame jumped a quarter of the way"
    assert w._energy_ui == pytest.approx(want, abs=0.01)


def test_the_first_frame_seeds_the_radius_instead_of_springing_from_none(bubble):
    w = harness(bubble, level=0.6, state=bubble.LISTENING)
    assert w._radius_ui is None

    w._on_tick()

    assert w._radius_ui == pytest.approx(w._radius_target(0.0)), (
        "the first frame must start AT the target; springing from None is not a "
        "starting condition, it is a crash waiting for the branch to be removed")
    assert w._radius_vel == 0.0, "a first-frame kick is a visible pop"


def test_the_radius_spring_settles_and_its_overshoot_stays_a_fraction_of_a_pixel(bubble):
    for dt_ms in (16.0, 1.0):
        clock = Clock()
        w = harness(bubble, clock=clock, state=bubble.LISTENING, level=0.6)
        # Start it where an IDLE bubble sits and let the spring chase the wider
        # listening radius with the voice held still, so the target it settles
        # on is FIXED and "overshoot" is measured against one number.
        w._radius_ui = bubble.BUBBLE_R0
        w._radius_vel = 0.0
        start = w._radius_ui
        want = w._radius_target(0.0)
        travel = want - start
        assert travel > 1.0, "anti-vacuity: the listening bubble must grow"

        deviations = []
        for frame in range(1, 2000):
            clock.advance(dt_ms)
            w._on_tick()
            deviations.append(w._radius_ui - want)

        # A spring overshoots and rings; what makes it usable is that the ring
        # DECAYS and then stops. So the turns are measured, not banned — an
        # earlier draft of this test asserted "no overshoot" from the comment
        # above `_on_tick` and that claim is simply not what the arithmetic
        # does: it misses by ~2.4% of the travel at 16 ms frames and ~3.9% at
        # 1 ms. What IS pinned is that each turn is at least five times smaller
        # than the one before (measured: ~25x) and that the ring reaches the
        # floor instead of orbiting a radius nobody can see it settle on.
        extrema = []
        for before, here, after in zip(deviations, deviations[1:], deviations[2:]):
            if (here > before and here >= after) or (here < before and here <= after):
                miss = abs(here)
                if miss > 1e-6 * travel:
                    extrema.append(miss)
        assert extrema, "no turning point at all: this is not a spring"
        assert extrema[0] <= 0.05 * travel, (
            f"dt={dt_ms}ms first overshoot {extrema[0] / travel:.1%} of the travel")
        for earlier, later in zip(extrema, extrema[1:]):
            assert later <= earlier / 5, (
                f"dt={dt_ms}ms the ring barely decays: "
                f"{[round(e / travel, 5) for e in extrema[:4]]} of the travel")
        assert abs(deviations[-1]) <= 0.0001 * travel, (
            f"dt={dt_ms}ms never stopped: {abs(deviations[-1]) / travel:.3%} off "
            f"a {travel:.2f}px travel after {len(deviations)} frames")
        assert max(abs(d) for d in deviations[:20]) <= 1.01 * travel, (
            f"dt={dt_ms}ms undershot the start of the travel")
        if dt_ms == 16.0:
            settled = next(i for i, d in enumerate(deviations)
                           if abs(d) < 0.01 * travel)
            assert settled < 20, (
                f"{settled + 1} frames is a third of a second to get within 1%")


def test_the_radius_spring_is_what_grows_and_shrinks_with_the_voice(bubble):
    """The spring chases `_radius_target`, which is where the voice enters."""
    quiet = harness(bubble, state=bubble.LISTENING, level=0.0)
    loud = harness(bubble, state=bubble.LISTENING, level=1.0)
    assert loud._radius_target(0.0) > quiet._radius_target(0.0)
    # ...and only while LISTENING: the other states carry their own motion.
    still = harness(bubble, state=bubble.THINKING, level=1.0)
    assert still._radius_target(0.0) == pytest.approx(bubble.BUBBLE_R0), (
        "thinking is a steady radius: the dots animate, the bubble does not")


def test_every_frame_asks_for_exactly_one_repaint(bubble):
    """The tick's last line is the one that makes any of it visible."""
    clock = Clock()
    w = harness(bubble, clock=clock)
    for _ in range(3):
        clock.advance(16)
        w._on_tick()
    assert w.frames == 3

    idle_again = harness(bubble, clock=Clock(), state=bubble.IDLE, level=0.0)
    idle_again._on_tick()                   # nothing has changed at all
    assert idle_again.frames == 1, "a no-op frame must not leave a stale frame"
