"""core.registry: the one place a cap is enforced and an offer is armed.

Both primitives exist to delete a defect family that kept reappearing in a new
registry — "check while holding the lock, release it across the slow part, then
insert without re-checking" — so the tests here are about the INVARIANTS that
make the family impossible, not about one caller:

  * a reservation counts against the cap, so two callers can never both be told
    there is room;
  * a prepare that fails gives its slot back (no silent shrinkage of the cap);
  * arming an offer is a single step, so no reader can see it half-armed;
  * the deadline is applied by the helper, so no consumer can forget it;
  * consume() is the claim, so an action cannot run twice.
"""
import pathlib
import threading
import time

import pytest

from conftest import core_module

from core import registry as _core_registry

# Resolved on first use rather than at collection (conftest.core_module).
_core_tools = core_module("tools")

class TestBoundedRegistry:
    def test_a_held_reservation_counts_against_the_cap(self, H):
        """The reservation IS the admission: taken before the slow prepare.

        This is the whole difference from the shape it replaces. A caller that
        has reserved but not committed owns the slot, so a second caller is
        refused rather than told there is room it is about to lose — which is
        how eight overlapping `start_command` calls came to start seven
        processes against a cap of four.
        """
        reg = _core_registry.BoundedRegistry("t", 1)
        slot = reg.reserve()
        assert slot is not None
        assert reg.reserve() is None, "the reserved slot was handed out twice"
        assert reg.reserve() is None, "still reserved"
        assert len(reg) == 0, "an uncommitted reservation is not an entry"
        assert not reg.room()
        key, displaced = slot.commit("first")
        assert displaced is None
        assert reg.snapshot() == {key: "first"}
        assert reg.reserve() is None, "cap 1 with one live entry"
        reg.release(key)
        assert reg.reserve() is not None

    def test_a_failed_prepare_gives_the_slot_back(self, H):
        """`with slot:` cancels on the way out.

        A leaked reservation shrinks the cap silently: the registry keeps
        counting a resource that does not exist, and after a few failed
        launches it refuses work while `job_status` lists nothing to reap.
        """
        reg = _core_registry.BoundedRegistry("t", 1)
        try:
            with reg.reserve() as slot:
                assert slot is not None
                raise OSError("prepare failed")
        except OSError:
            pass
        assert reg.room(), "the cap shrank on the failure path"
        slot = reg.reserve()
        assert slot is not None, "the slot leaked on the failure path"
        slot.cancel()

    def test_commit_mints_unique_keys_inside_the_lock(self, H):
        """Key allocation and insertion are one critical section.

        The job registry used to bump a sequence number under a lock it had to
        remember to take. Minting the key inside `commit` means two entries can
        never be handed the same id, even from eight racing callers.
        """
        reg = _core_registry.BoundedRegistry("job", 8)
        barrier = threading.Barrier(8)
        keys: list = []
        guard = threading.Lock()

        def worker():
            barrier.wait(timeout=10)
            slot = reg.reserve()
            assert slot is not None
            key, _displaced = slot.commit(lambda k: k.upper())
            with guard:
                keys.append(key)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(set(keys)) == 8, keys
        assert sorted(reg.keys()) == sorted(keys)
        assert sorted(reg.values()) == sorted(k.upper() for k in keys)

    def test_replacing_a_name_never_costs_a_second_slot(self, H):
        """Re-registering an existing name is room even at the cap.

        Re-watching the same path must not be refused because the registry is
        full of the other three, and the displaced entry is handed BACK so the
        caller can stop it outside the helper's lock.
        """
        reg = _core_registry.BoundedRegistry("w", 1)
        key, _ = reg.reserve().commit("old")
        slot = reg.reserve(key, replace=True)
        assert slot is not None, "re-registering a name must need no room"
        same, displaced = slot.commit("new")
        assert same == key and displaced == "old"
        assert len(reg) == 1 and reg[key] == "new"
        assert reg.reserve("other") is None, "a NEW name at the cap is refused"

    def test_the_cap_is_the_only_refusal_and_it_counts_reservations(self, H):
        """Eight racing reservations against a cap of four: exactly four win."""
        reg = _core_registry.BoundedRegistry("t", 4)
        barrier = threading.Barrier(8)
        won: list = []
        lost: list = []
        guard = threading.Lock()

        def worker():
            barrier.wait(timeout=10)
            slot = reg.reserve()
            with guard:
                (won if slot is not None else lost).append(slot)
            if slot is not None:
                slot.commit("held")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(won) == 4, (len(won), len(lost))
        assert len(lost) == 4
        assert len(reg) == 4

    def test_a_refusal_is_counted_and_its_shape_remembered(self, H):
        """The registry is the only place that KNOWS a cap was hit, so it is
        the only place a refusal can be counted — which is why a future cap
        cannot grow silently. `at` and the occupant list are captured under the
        same lock as the decision, so the report describes the instant that was
        refused rather than a later read of len()/keys().
        """
        reg = _core_registry.BoundedRegistry("job", 1)
        assert reg.refusals == 0 and reg.refusal_report() is None
        key, _ = reg.reserve().commit("a")
        assert reg.reserve() is None
        report = reg.refusal_report()
        assert reg.refusals == 1
        assert report["registry"] == "job" and report["cap"] == 1
        assert report["held"] == 1 and report["occupants"] == [key]
        assert report["reserved"] == 0 and report["count"] == 1
        assert isinstance(report["at"], float) and report["at"] > 0
        assert reg.reserve() is None
        assert reg.refusals == 2 and reg.refusal_report()["count"] == 2

    def test_a_reservation_that_was_not_refused_is_not_counted(self, H):
        """Only a refusal is a refusal: releasing and re-reserving is normal
        traffic and must not make the report cry wolf."""
        reg = _core_registry.BoundedRegistry("t", 1)
        key, _ = reg.reserve().commit("a")
        reg.release(key)
        again = reg.reserve()
        assert again is not None
        again.cancel()
        assert reg.refusals == 0 and reg.refusal_report() is None
        # ...and the bounded-window case counts too: a replace at the cap that
        # IS allowed is not a refusal either.
        reg2 = _core_registry.BoundedRegistry("w", 1)
        key2, _ = reg2.reserve().commit("x")
        assert reg2.reserve(key2, replace=True) is not None
        assert reg2.refusals == 0

    def test_the_refusal_line_is_one_sentence_for_every_caller(self, H):
        """The journal, the tool reply and the doctor must not describe one
        refusal in three different ways, so the sentence is rendered by the
        registry that made the decision.
        """
        reg = _core_registry.BoundedRegistry("job", 4)
        for i in range(4):
            reg.reserve().commit(f"j{i}")
        assert reg.reserve() is None
        line = reg.refusal_line("start_command 'echo x'")
        assert line == ("job at 4/4 held — start_command 'echo x' "
                        "(nothing was created)"), line

    def test_the_refusal_report_hands_out_a_copy(self, H):
        reg = _core_registry.BoundedRegistry("job", 1)
        reg.reserve().commit("a")
        reg.reserve()
        report = reg.refusal_report()
        report["cap"] = 99
        assert reg.refusal_report()["cap"] == 1, "the record was writable"

    def test_clear_returns_the_entries_it_dropped(self, H):
        reg = _core_registry.BoundedRegistry("t", 2)
        reg.reserve().commit("a")
        reg.reserve().commit("b")
        assert sorted(reg.clear()) == ["a", "b"]
        assert len(reg) == 0 and reg.room()

    def test_the_read_surface_is_the_mapping_it_claims(self, H):
        """The calls that actually exist: job_status iterates and reaps by
        membership, and watch_file joins the keys into its listing."""
        reg = _core_registry.BoundedRegistry("job", 2)
        k1, _ = reg.reserve().commit("a")
        k2, _ = reg.reserve().commit("b")
        assert len(reg) == 2
        assert k1 in reg and reg[k1] == "a"
        assert sorted(iter(reg)) == sorted((k1, k2))
        assert sorted(reg.values()) == ["a", "b"]
        assert sorted(reg.items()) == sorted(((k1, "a"), (k2, "b")))
        assert reg.get("nope") is None and reg.get("nope", "d") == "d"
        assert reg.release("nope") is None
        assert reg.snapshot() == {k1: "a", k2: "b"}

    def test_cap_and_name_are_reported(self, H):
        reg = _core_registry.BoundedRegistry("watch-file", 4)
        assert reg.cap == 4 and reg.name == "watch-file"
        with pytest.raises(ValueError):
            _core_registry.BoundedRegistry("bad", -1)

    def test_a_dead_occupant_is_reclaimed_where_the_slot_is_handed_out(self, H):
        """The singleton whose occupant can die: a monitor that gave up, a
        diagnostic worker that crashed. "Is it still alive?" and "may I take
        its slot?" were two reads in the caller, so two callers both saw a dead
        slot and both started a worker. Here the second question is asked under
        the same lock as the first, and the corpse comes back for disposal.
        """
        reg = _core_registry.BoundedRegistry("notification-reader", 1)
        reg.reserve("m").commit("dead monitor")
        assert reg.reserve("m", reclaim=lambda e: False) is None, \
            "a live occupant's slot was handed out"
        assert reg.snapshot() == {"m": "dead monitor"}, "the entry was lost"
        slot = reg.reserve("m", reclaim=lambda e: e == "dead monitor")
        assert slot is not None, "a dead occupant stranded the slot"
        assert slot.reclaimed == "dead monitor", slot.reclaimed
        key, _displaced = slot.commit("fresh monitor")
        assert key == "m" and reg.snapshot() == {"m": "fresh monitor"}

    def test_a_reclaimed_occupant_is_not_an_entry_any_more(self, H):
        """The corpse leaves the registry when it is judged dead, so the slot
        it held is genuinely free — a caller that abandons the reservation in
        between must not leave the registry counting a resource that is gone.
        """
        reg = _core_registry.BoundedRegistry("t", 1)
        reg.reserve("k").commit("goneskies")
        slot = reg.reserve("k", reclaim=lambda e: True)
        assert len(reg) == 0 and not reg.room()  # held by the reservation now
        slot.cancel()
        assert reg.room() and len(reg) == 0
        assert reg.reserve("k").commit("next")[0] == "k"

    def test_a_predicate_that_raises_leaves_the_entry_alone(self, H):
        """A reclaim predicate is caller code. If it fails, the reserve fails —
        but the occupant must still be registered, or a bug in a predicate
        would silently delete the monitor it was asked about.
        """
        reg = _core_registry.BoundedRegistry("t", 1)
        reg.reserve("k").commit("precious")

        def boom(_entry):
            raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            reg.reserve("k", reclaim=boom)
        assert reg.snapshot() == {"k": "precious"}
        assert reg.refusals == 0, "a failed reserve is not a refusal"

    def test_committing_twice_does_not_invent_capacity(self, H):
        """One reservation releases ONE slot, however many times it settles.

        `commit()` gave the slot back unconditionally, so calling it twice on
        one reservation decremented `_held` twice — the registry then believed
        it had a free slot it had already handed out, and the next caller was
        admitted past the cap. The same trap was open to `cancel()` after a
        commit, so both go through one idempotent release now.
        """
        reg = _core_registry.BoundedRegistry("t", 1)
        slot = reg.reserve()
        assert reg.reserve() is None, "the only slot was not counted"
        key, displaced = slot.commit("first")
        assert reg.room() is False, "a committed entry must still occupy the cap"
        # The misuse: settle the same reservation again. It must register
        # nothing new (one reservation, one resource) and must not release a
        # second slot.
        again_key, again_displaced = slot.commit("second")
        slot.cancel()
        assert (again_key, again_displaced) == (key, displaced)
        assert reg.snapshot() == {key: "first"}, reg.snapshot()
        assert reg.room() is False, "capacity was invented by a double release"
        assert reg.reserve() is None, "a second slot appeared from nowhere"
        reg.release(key)
        assert reg.room() is True

    def test_two_registries_can_share_one_lock(self, H):
        """The watcher registries share a lock on purpose: their teardown is
        one step, so a watcher cannot start in the gap between the two clears.
        """
        lock = threading.RLock()
        a = _core_registry.BoundedRegistry("a", 1, lock=lock)
        b = _core_registry.BoundedRegistry("b", 1, lock=lock)
        assert a._lock is b._lock
        with lock:
            assert sorted(a.clear() + b.clear()) == []


class TestOffer:
    def test_a_reader_never_sees_a_half_armed_offer(self, H, monkeypatch):
        """Arming is ONE assignment; `clear()` + `update()` was two steps.

        A reader landing between them saw an EMPTY offer for one that exists
        (and could pair one caller's payload with another's deadline). The arm
        is held open here — the clock is read while the lock is held — so the
        interleaving is certain rather than a race the test hopes to hit.
        """
        off = _core_registry.Offer("x")
        real = time.monotonic
        inside = threading.Event()

        def slow_clock():
            inside.set()
            time.sleep(0.3)
            return real()

        monkeypatch.setattr(off, "_clock", slow_clock)
        seen: list = []

        def arm():
            off.arm(60, tool="wait")

        armer = threading.Thread(target=arm)
        armer.start()
        assert inside.wait(5), "the arm never reached its clock"
        reader = threading.Thread(target=lambda: seen.append(off.state()[0]))
        reader.start()
        armer.join(5)
        reader.join(5)
        assert seen and seen[0] is not None, (
            "the reader saw an empty offer while an arm was in flight")
        assert seen[0].get("tool") == "wait", seen

    def test_state_applies_the_deadline_and_clears_a_closed_window(self, H):
        off = _core_registry.Offer("x")
        assert off.state() == (None, False)
        assert not off
        off.arm(-1, name="old")            # armed, window already gone
        assert off.state() == (None, True)
        assert not off, "a closed window must be cleared by the read"
        assert off.state() == (None, False), "the expired flag must not stick"

    def test_consume_is_the_claim(self, H):
        """Two racing confirmations: exactly one gets the offer."""
        off = _core_registry.Offer("x")
        off.arm(60, tool="wait")
        assert off.consume()["tool"] == "wait"
        assert off.consume() is None, "a consumed offer was claimable twice"
        assert not off

    def test_consume_refuses_a_closed_window(self, H):
        off = _core_registry.Offer("x")
        off.arm(-1, tool="wait")
        assert off.consume() is None
        assert not off

    def test_arm_unless_never_extends_a_live_window(self, H):
        """A model looping on the same call must not hold its own confirmation
        open forever, and two callers must not both decide to arm."""
        off = _core_registry.Offer("x")
        # The predicate asks "is the LIVE offer the same request I am making?",
        # which is how the CONFIRM path spells it (same tool, same turn).
        already_offered = lambda tool: (lambda live: live.get("tool") == tool)
        assert off.arm_unless(already_offered("wait"), 60, tool="wait") is True
        before = off.get("until")
        assert off.arm_unless(already_offered("wait"), 600, tool="wait") is False
        assert off.get("until") == before, "a repeat extended the window"
        assert off.arm_unless(already_offered("other"), 600, tool="other") is True
        assert off["tool"] == "other"

    def test_arm_unless_does_not_match_a_closed_window(self, H):
        """An expired offer must be re-armed, not silently kept."""
        off = _core_registry.Offer("x")
        off.arm(-1, tool="wait")
        assert off.arm_unless(lambda live: live.get("tool") == "wait",
                              60, tool="wait") is True
        assert off["tool"] == "wait" and off

    def test_the_read_surface_cannot_mutate(self, H):
        """No mutators outside the deadline-aware methods.

        The defect was a bare dict handed to every caller, which then armed it
        by hand. There is deliberately no way to do that any more.
        """
        off = _core_registry.Offer("x")
        off.arm(60, tool="wait")
        assert off["tool"] == "wait"
        assert "tool" in off and "tool" in off.keys()
        assert off.get("missing", "d") == "d"
        assert not hasattr(off, "update")
        with pytest.raises(TypeError):
            off["tool"] = "rm"           # type: ignore[index]
        assert off["tool"] == "wait"
        # ...and the reads hand out COPIES: a consumer that mutates what it was
        # given must not be able to rewrite the armed offer behind the helper.
        snapshot, _expired = off.state()
        snapshot["tool"] = "rm"
        assert off["tool"] == "wait", "state() handed out the live dict"

    def test_the_offer_read_surface_and_repr(self, H):
        off = _core_registry.Offer("kill")
        assert len(off) == 0 and "empty" in repr(off)
        off.arm(60, pid=7)
        assert len(off) == 1 and "pid" in repr(off)
        assert off.get("pid") == 7 and off["pid"] == 7
        assert "pid" in off and "until" in off.keys()
        with pytest.raises(KeyError):
            off["missing"]
        off.expire()
        assert not off and off.get("pid") is None and off.keys() == []
        with pytest.raises(KeyError):
            off["pid"]                  # a closed window is not readable

    def test_state_snapshot_and_restore(self, H):
        """The conftest isolation hook: an armed offer must not leak."""
        off = _core_registry.Offer("x")
        assert off.snapshot_state() is None
        off.arm(60, tool="wait")
        snap = off.snapshot_state()
        off.clear()
        assert not off
        off.restore_state(snap)
        assert off["tool"] == "wait"


class TestEveryCapAndOfferLivesInTheHelper:
    """The point of the refactor, pinned: one answer to "where is the cap
    enforced?" and "where is an offer armed?"."""

    def test_the_belt_owns_only_helper_registries(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        assert isinstance(belt._jobs, _core_registry.BoundedRegistry)
        assert isinstance(belt._file_watchers, _core_registry.BoundedRegistry)
        assert isinstance(belt._process_watchers, _core_registry.BoundedRegistry)
        assert isinstance(belt._pending_confirm, _core_registry.Offer)
        assert belt._jobs.cap == _core_tools.BoundedJob.MAX_JOBS
        assert belt._file_watchers.cap == 4 and belt._process_watchers.cap == 4
        # both watcher registries share one critical section (atomic teardown)
        assert belt._file_watchers._lock is belt._process_watchers._lock

    def test_the_module_offers_are_helper_instances(self, H):
        assert isinstance(H._kill_offer, _core_registry.Offer)
        assert isinstance(H._snooze_offer, _core_registry.Offer)
        assert not hasattr(H, "_KILL_LOCK"), "the kill offer still has a raw lock"
        assert not hasattr(H, "_SNOOZE_LOCK"), "the snooze offer still has one"
        assert not hasattr(H, "_kill_offer_dict")

    def test_no_hand_rolled_registry_lock_remains(self, H):
        """Source-level pin: a caller-visible lock released at the wrong moment
        was the defect, so the names that carried one must be gone from every
        module that used to hold a cap or an offer."""
        root = pathlib.Path(H.__file__).resolve().parent
        for src in (root / "handsoff.py", root / "core" / "tools.py"):
            text = src.read_text(encoding="utf-8")
            for name in ("_KILL_LOCK", "_SNOOZE_LOCK", "_job_lock", "_job_seq"):
                assert name not in text, f"{src.name} still owns {name}"
