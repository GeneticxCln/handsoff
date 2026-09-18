"""Lifecycle tests: launch, control socket, streaming, restart, repo integrity gates."""
from __future__ import annotations

import base64
import importlib.util
import inspect
from collections import deque
import threading
import io
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import (HERE as ROOT, _load, core_module, run_driver,
                      sandbox_env, wait_for)

# Resolved on first use rather than at collection (conftest.core_module).
_core_tools = core_module("tools")

HERE = ROOT   # the repo root (conftest resolves it from conftest.py's parent)

# Bytes planted in a fake deployment's core/ to prove a rollback restores the
# whole saved module set (see TestInstallerRehearsal / the rollback tests).
SENTINEL_CORE_BYTES = "# OLD core module bytes\n"


class TestCoreLifecycle:
    """core.lifecycle imports independently and provides minimal turn primitives."""

    def test_core_lifecycle_imports_independently(self):
        """core.lifecycle must not import handsoff.py, Qt, or Assistant."""
        import core.lifecycle as lifecycle

        # Verify the module loads without pulling in heavy deps
        assert hasattr(lifecycle, "TurnState")
        assert hasattr(lifecycle, "next_turn")

    def test_turnstate_is_dataclass_with_four_fields(self):
        """TurnState is a small dataclass with generation, cancel, done, result."""
        import core.lifecycle as lifecycle
        from dataclasses import fields

        fs = {f.name for f in fields(lifecycle.TurnState)}
        assert fs == {"generation", "cancel", "done", "result"}

        # Can construct with all fields
        cancel = threading.Event()
        done = threading.Event()
        ts = lifecycle.TurnState(generation=42, cancel=cancel, done=done, result="ok")
        assert ts.generation == 42
        assert ts.cancel is cancel
        assert ts.done is done
        assert ts.result == "ok"

    def test_next_turn_advances_counter_and_returns_fresh_turnstate(self):
        """next_turn increments the mutable counter and returns a fresh TurnState."""
        import core.lifecycle as lifecycle

        counter = [0]
        ts1 = lifecycle.next_turn(counter)
        assert ts1.generation == 1
        assert counter[0] == 1
        assert isinstance(ts1.cancel, threading.Event)
        assert isinstance(ts1.done, threading.Event)
        assert not ts1.cancel.is_set()
        assert not ts1.done.is_set()
        assert ts1.result is None

        ts2 = lifecycle.next_turn(counter)
        assert ts2.generation == 2
        assert counter[0] == 2
        # Fresh events each call
        assert ts2.cancel is not ts1.cancel
        assert ts2.done is not ts1.done

    def test_next_turn_works_with_dict_counter(self):
        """next_turn also accepts a dict as the mutable counter container."""
        import core.lifecycle as lifecycle

        counter = {"gen": 0}
        ts1 = lifecycle.next_turn(counter)
        assert ts1.generation == 1
        assert counter["gen"] == 1

        ts2 = lifecycle.next_turn(counter)
        assert ts2.generation == 2
        assert counter["gen"] == 2

    def test_new_counter_makes_the_container_next_turn_advances(self):
        """The container shape is the module's contract, not the caller's.

        `next_turn` advances the `"gen"` key, so a hand-rolled
        `{"generation": n}` would be handed generation 0 for every turn.
        """
        import core.lifecycle as lifecycle

        box = lifecycle.new_counter()
        assert box == {"gen": 0}
        assert lifecycle.next_turn(box).generation == 1
        assert box["gen"] == 1
        assert lifecycle.new_counter(41)["gen"] == 41
        assert lifecycle.new_counter() is not box, "counters must not be shared"

    def test_generation_counter_claims_and_exposes_its_value(self):
        """One counter per turn stream: claim advances it, value reads it."""
        import core.lifecycle as lifecycle

        counter = lifecycle.GenerationCounter(7)
        assert counter.value == 7

        first = counter.claim()
        assert first.generation == 8 and counter.value == 8
        assert isinstance(first.cancel, threading.Event)
        assert not first.cancel.is_set() and not first.done.is_set()

        second = counter.claim()
        assert second.generation == 9
        assert second.cancel is not first.cancel, "fresh events per claim"
        assert second.done is not first.done

        counter.value = 30
        assert counter.claim().generation == 31

    def test_generation_counters_are_not_shared_between_instances(self):
        """Two streams must not advance one number."""
        import core.lifecycle as lifecycle

        a = lifecycle.GenerationCounter()
        b = lifecycle.GenerationCounter()
        a.claim()
        assert a.value == 1
        assert b.value == 0, "a second counter has to start where it was made"

    def test_concurrent_claims_on_one_counter_are_all_distinct(self):
        """The claim is what the bubble calls from five kinds of thread."""
        import core.lifecycle as lifecycle

        counter = lifecycle.GenerationCounter()
        seen: list = []
        guard = threading.Lock()
        barrier = threading.Barrier(8)

        def _claim() -> None:
            barrier.wait(timeout=5)
            gen = counter.claim().generation
            with guard:
                seen.append(gen)

        threads = [threading.Thread(target=_claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(seen) == 8, "a claimant never returned"
        assert sorted(seen) == list(range(1, 9)), (
            f"generations were handed out twice or skipped: {sorted(seen)}")


class _YieldingList(list):
    """A list whose item access releases the GIL.

    `counter[0] += 1` is load-add-store, and what makes it racy is the chance
    of a thread switch between the two. This container makes that chance a
    certainty, so a missing lock fails every run instead of once in a while —
    measured earlier, the plain one-liner produced 0 duplicates in 64 000
    claims under the GIL, which is why the race needed a shape that yields.
    """

    def __getitem__(self, i):
        value = list.__getitem__(self, i)
        time.sleep(0.002)
        return value

    def __setitem__(self, i, value):
        time.sleep(0.002)
        list.__setitem__(self, i, value)


class _YieldingDict(dict):
    """The dict-counter shape, with the same deliberate gap."""

    def get(self, key, default=None):
        value = dict.get(self, key, default)
        time.sleep(0.002)
        return value

    def __setitem__(self, key, value):
        time.sleep(0.002)
        dict.__setitem__(self, key, value)


class TestTurnGenerationIsAtomic:
    """Two turns must never claim the SAME generation.

    The generation is what the staleness checks (`gen != self._gen`) and the
    gen-keyed transcript cache trust, so a duplicate lets one utterance's text
    be answered in another turn. `next_turn` is the exported form of that
    counter and is reachable from any embedder, so it takes the lock itself
    rather than relying on its callers.
    """

    @staticmethod
    def _claim(lifecycle, counter, results, barrier):
        barrier.wait(timeout=5)
        results.append(lifecycle.next_turn(counter).generation)

    def test_concurrent_claims_are_all_distinct_list_counter(self):
        import core.lifecycle as lifecycle

        counter = _YieldingList([0])
        results: list = []
        barrier = threading.Barrier(4)
        threads = [threading.Thread(target=self._claim,
                                    args=(lifecycle, counter, results, barrier))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 4, "a claimant never returned"
        assert len(set(results)) == 4, (
            f"two turns claimed the same generation: {sorted(results)}")
        assert counter[0] == 4

    def test_concurrent_claims_are_all_distinct_dict_counter(self):
        import core.lifecycle as lifecycle

        counter = _YieldingDict({"gen": 0})
        results: list = []
        barrier = threading.Barrier(4)
        threads = [threading.Thread(target=self._claim,
                                    args=(lifecycle, counter, results, barrier))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(set(results)) == 4, (
            f"two turns claimed the same generation: {sorted(results)}")
        assert counter["gen"] == 4


class TestSourceIntegrity:
    @pytest.mark.parametrize("name", ["handsoff.py", "handsoff-settings.py"])
    def test_compiles(self, name):
        src = (HERE / name).read_text(encoding="utf-8")
        compile(src, name, "exec")

    @pytest.mark.parametrize("name", ["handsoff.py", "handsoff-settings.py"])
    def test_marker_present(self, name, H):
        second_line = (HERE / name).read_text(encoding="utf-8").splitlines()[1]
        assert second_line == H.SELF_MARKER

    def test_ptt_actions_documented_in_usage(self, H):
        for word in H.PTT_ACTIONS:
            assert word in H.USAGE

    def test_shutdown_is_bounded_and_rejects_new_work(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._shutdown_event = threading.Event()
        a._closed = False
        a._workers = set()
        a._cancel = threading.Event()
        # shutdown() must CLOSE the listener, not merely stop it: stop() is the
        # hands-free toggle, and the mic-health reporter (spawned in __init__) is
        # a second thread that only close() ends.
        listener_calls = []
        a._listener = types.SimpleNamespace(
            close=lambda: listener_calls.append("close"))
        a._tools = None
        a._notifications = types.SimpleNamespace(
            set_enabled=lambda enabled: "notification reader disabled")
        a._recorder = None
        a._pipeline_q = __import__("queue").Queue(maxsize=1)
        a._state = H.IDLE
        a._gen = 0
        spoken = []
        a._speak = lambda *args, **kwargs: spoken.append(args[0])
        started = threading.Event()
        release = threading.Event()
        worker = a._start_worker(
            lambda: (started.set(), release.wait(5)), name="stuck-test-worker")
        assert started.wait(1)
        began = time.monotonic()
        a.shutdown()
        assert time.monotonic() - began < 1.0
        release.set()
        worker.join(1)
        assert a._closed is True
        assert a._shutdown_event.is_set()
        assert listener_calls == ["close"], listener_calls
        a._announce_now("late")
        assert spoken == []
        queued = a._pipeline_q.qsize()
        a.submit_audio(np.zeros(16000, dtype=np.int16))
        assert a._pipeline_q.qsize() == queued

    def test_shutdown_takes_the_mic_reporter_with_it(self, H):
        # The bubble's own shutdown used to stop the CAPTURE and leave the hourly
        # reporter running — a daemon thread nothing else could end, which only
        # the process exit hid. The stub above pins that shutdown CALLS the right
        # seam; this one pins that the seam does what it claims on a real
        # listener, because a stub can agree with a method that does nothing.
        a = H.Assistant()
        reporter = a._listener._health_thread
        try:
            assert reporter.is_alive(), "the reporter must start with the bubble"
            a.shutdown()
            reporter.join(timeout=2.0)
            assert not reporter.is_alive(), (
                "shutdown() left the mic-health reporter running: every bubble "
                "built and dropped then leaks one thread that polls forever")
        finally:
            a._listener.close()


# ------------------------------------------------------------------ control socket


class TestControlSocket:
    @pytest.fixture()
    def server(self, H, tmp_path):
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        delivered: list[str] = []
        # keep the module's real socket path out of the picture: point the
        # module-level constant at a fresh per-test path
        sock_path = tmp_path / "control.sock"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(H, "CONTROL_SOCK", sock_path)
        asst = H.Assistant()
        asst.sigCommand.connect(delivered.append)
        srv = H.ControlServer(asst)
        srv.start()
        # wait until the server actually answers (the socket file may exist
        # before the thread is listening, and stale files from old runs linger)
        deadline = time.time() + 5
        srv_ready, last_err = False, None
        while time.time() < deadline:
            try:
                if self._roundtrip(sock_path, "status").startswith("state="):
                    srv_ready = True
                    break
            except OSError as e:
                last_err = e
            time.sleep(0.05)
        assert srv_ready, f"control server never answered ({last_err})"
        try:
            yield H, delivered, app
        finally:
            # Stop the accept loop BEFORE the socket path is restored: the
            # loop is a named worker with a stop path, so leaving it running
            # is a leak the suite's worker guard now fails on.
            srv.stop()
            # A command this test caused but never drained would run during
            # whichever test next calls processEvents() — and `handsfree-status`
            # really speaks, so the worker it starts outlives a test that has
            # already finished and the leak guard blames that innocent
            # neighbour (measured). Draining here is what makes the GUILTY test
            # the one that fails: anything that arrives now was left queued.
            before = len(delivered)
            app.processEvents()
            assert delivered[before:] == [], (
                "this test left control command(s) queued: "
                + ", ".join(delivered[before:])
                + " — a command runs on the Qt event loop, so process events "
                  "and assert its delivery inside the test that sent it")
            monkey.undo()
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def test_overlapping_diagnostics_cannot_start_two_workers(self, H, tmp_path,
                                                              monkeypatch):
        """One slot for slow diagnostics, and the losers are refused by name.

        A timed-out health/doctor worker cannot be cancelled — it is blocked in
        an Ollama or nvidia-smi call — so "is the previous worker alive?" then
        "start one" is how repeated requests against a wedged backend pile up
        threads that never return. The worker is parked until every caller has
        been through, so the assertions are about the invariant rather than
        about winning a race.

        The thread census counts the workers THIS test started, not every
        `diag-worker` in the process: a wedged worker another test is still
        winding down is not this cap's pile-up, and counting it made the
        result depend on which test ran first (the shuffled ordering probe
        caught it).
        """
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "diag.sock")
        asst = H.Assistant()
        # The refusal speaks through the real announcement path, which would
        # start a TTS worker in a test; the channel is what is stubbed, not the
        # decision to speak.
        said: list = []
        asst._announce_now = said.append
        srv = H.ControlServer(asst)
        before = H._cap_refusal_summary()["by_registry"].get("diagnostic") or 0
        release = threading.Event()
        ran: list = []

        def slow_diagnostic():
            ran.append(1)
            release.wait(10)
            return "snapshot"

        barrier = threading.Barrier(8, timeout=10)
        results: list = []

        def call():
            barrier.wait(timeout=10)
            try:
                results.append(srv._diagnostic_call(slow_diagnostic, 5.0))
            except Exception as e:  # noqa: BLE001
                results.append(f"{type(e).__name__}: {e}")

        started_before = {t.ident for t in threading.enumerate()
                          if t.name == "diag-worker"}
        threads = [threading.Thread(target=call) for _ in range(8)]
        for t in threads:
            t.start()
        deadline = time.time() + 5
        while len(results) < 7 and time.time() < deadline:
            time.sleep(0.01)
        workers = [t for t in threading.enumerate()
                   if t.name == "diag-worker" and t.ident not in started_before]
        assert len(workers) == 1, f"{len(workers)} diagnostic workers piled up"
        assert len(ran) == 1, f"the backend call ran {len(ran)} times"
        release.set()
        for t in threads:
            t.join(10)
        assert results.count("snapshot") == 1, results
        refused = [r for r in results if "still running" in r]
        assert len(refused) == 7, results
        # ...and a refused diagnostic is durable evidence, not just a reply.
        after = H._cap_refusal_summary()["by_registry"].get("diagnostic") or 0
        assert after - before == 7, (before, after)
        # ...and it is SAID, once, rather than seven times.
        assert len(said) == 1, said
        assert "diagnostic-worker" in said[0], said

    @staticmethod
    def _roundtrip(sock_path: Path, action: str) -> str:
        from core import app_module
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(sock_path))
        # Sent the way a REAL client sends it, capability token and all: the
        # helper stands in for `--ptt`, and a state-changing verb sent bare is
        # refused now. Going around that would leave every one of these tests
        # exercising a protocol no client uses.
        argv = action.split(" ")
        s.sendall(app_module()._control_payload(argv[0], argv))
        s.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            part = s.recv(1024)
            if not part:
                break
            reply += part
        s.close()
        return reply.decode()

    @pytest.fixture()
    def held_serve(self, H, monkeypatch):
        """A `_serve` that announces itself and then blocks.

        Holding the spawn open is what makes the concurrency assertion about
        the INVARIANT instead of about winning a race: on the fixed code the
        loser cannot even reach the spawn, because the winner's reservation is
        already counted against the cap.
        """
        gate = threading.Event()
        release = threading.Event()
        started: list[int] = []

        def serve(_self):
            started.append(1)
            gate.set()
            release.wait(10)

        monkeypatch.setattr(H.ControlServer, "_serve", serve)
        try:
            yield started, gate, release
        finally:
            release.set()

    def test_overlapping_starts_cannot_start_two_accept_loops(
            self, H, tmp_path, monkeypatch, held_serve):
        """One acceptor, decided at admission. The last hand-rolled cap.

        "Is one already running?" then "start one" were two reads with a thread
        spawn between them, so two overlapping start() calls both saw nothing
        running and both bound a server: the loser's bind replaced the winner's
        socket, leaving a loop accepting on an inode no client could reach —
        `--ptt` says "not running" while the bubble believes it is reachable.
        """
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "control.sock")
        srv = H.ControlServer(H.Assistant())
        started, gate, release = held_serve
        before = {t.ident for t in threading.enumerate() if t.name == "control"}
        barrier = threading.Barrier(8, timeout=10)

        def call() -> None:
            barrier.wait(timeout=10)
            srv.start()

        callers = [threading.Thread(target=call) for _ in range(8)]
        try:
            for t in callers:
                t.start()
            for t in callers:
                t.join(10)
            assert gate.wait(5), "no accept loop ever started"
            assert started == [1], f"{len(started)} accept loops were started"
            live = [t for t in threading.enumerate()
                    if t.name == "control" and t.ident not in before]
            assert len(live) == 1, f"{len(live)} control threads are running"
            # ...and the slot is the bookkeeper, exactly as for every other cap
            assert len(srv._runs) == 1
            assert srv._runs.get(H.ControlServer.ACCEPT_SLOT) is not None
        finally:
            release.set()          # let the held spawn return...
            srv.stop()             # ...then stop it through the real path

    def test_a_dead_accept_loop_frees_its_slot(self, H, tmp_path, monkeypatch):
        """A loop that gave up must not wedge the server forever.

        `_serve` returns without binding when the runtime refuses or the bind
        fails. The slot then holds a corpse; the reclaim predicate — evaluated
        where the slot is handed out — is what lets the next start() take it,
        instead of "already running" being read off a thread that is gone.
        """
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "control.sock")
        calls: list[int] = []

        def one_shot(_self):
            calls.append(1)          # returns at once: the loop dies

        monkeypatch.setattr(H.ControlServer, "_serve", one_shot)
        srv = H.ControlServer(H.Assistant())
        srv.start()
        first = srv._runs.get(H.ControlServer.ACCEPT_SLOT)
        assert first is not None and wait_for(lambda: not first.is_alive()), \
            "the stubbed accept loop never finished"
        srv.start()
        assert wait_for(lambda: len(calls) == 2), \
            "a dead accept loop kept the accept slot"

    def test_stop_frees_the_slot_so_a_restart_rebinds(self, H, tmp_path,
                                                     monkeypatch):
        """stop() hands the slot back, and only once the loop is really gone.

        Releasing it before the join is how a restart gets a second acceptor
        beside the old one, which is the orphan this class exists to avoid.
        """
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        sock_path = tmp_path / "restart.sock"
        monkeypatch.setattr(H, "CONTROL_SOCK", sock_path)
        srv = H.ControlServer(H.Assistant())

        def answers() -> bool:
            try:
                return self._roundtrip(sock_path, "status").startswith("state=")
            except OSError:
                return False

        try:
            srv.start()
            assert wait_for(answers, timeout=5), "the server never answered"
            srv.stop()
            assert srv._runs.get(H.ControlServer.ACCEPT_SLOT) is None, \
                "a stopped accept loop kept the slot"
            assert not sock_path.exists(), "stop() left its socket behind"
            srv.start()
            assert wait_for(answers, timeout=5), "the restart never answered"
        finally:
            srv.stop()

    def test_stop_keeps_the_slot_while_the_loop_is_still_alive(
            self, H, tmp_path, monkeypatch):
        """Only a loop that is actually gone frees the accept slot.

        Freeing it before the join is how a restart gets a second acceptor
        beside the old one — the orphan this class exists to avoid. A
        thread-like stand-in pins the rule without spending a 2s join budget
        inside the suite.
        """
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "control.sock")
        started: list[int] = []
        monkeypatch.setattr(H.ControlServer, "_serve",
                            lambda _self: started.append(1))
        srv = H.ControlServer(H.Assistant())
        alive = {"yes": True}
        fake = types.SimpleNamespace(is_alive=lambda: alive["yes"],
                                     join=lambda timeout=None: None)
        srv._runs.reserve(H.ControlServer.ACCEPT_SLOT).commit(fake)
        srv.stop()
        assert srv._runs.get(H.ControlServer.ACCEPT_SLOT) is fake, \
            "stop() freed a slot whose accept loop was still alive"
        # ...and the moment it really dies the next start() reclaims it
        alive["yes"] = False
        srv.start()
        assert wait_for(lambda: started == [1]), \
            "the dead accept loop's slot was never reclaimed"
        assert srv._runs.get(H.ControlServer.ACCEPT_SLOT) is not fake

    def test_status_roundtrip(self, server):
        H, _delivered, _app = server
        assert H.ptt_client(["status"]) == 0

    def test_status_reply_content(self, server):
        H, _delivered, _app = server
        text = self._roundtrip(H.CONTROL_SOCK, "status")
        assert text.startswith("state=idle")
        assert "handsfree=" in text and "model=" in text

    def test_action_delivery_via_event_loop(self, server):
        H, delivered, app = server
        assert H.ptt_client(["interrupt"]) == 0
        deadline = time.time() + 3
        while "interrupt" not in delivered and time.time() < deadline:
            app.processEvents()
        assert "interrupt" in delivered

    def test_unknown_command_replies_error(self, server):
        H, _delivered, _app = server
        assert self._roundtrip(H.CONTROL_SOCK, "bogus").startswith(
            "error: unknown command 'bogus'")

    def test_control_socket_refuses_a_foreign_uid(self, server, monkeypatch):
        """Defence in depth: a peer that is not us is refused and logged.

        This is NOT the boundary that protects the socket — STATE_DIR is 0700
        and refuses to start otherwise, which is what keeps other users out.
        A same-uid process has our uid, so no credential check can exclude it.
        What this pins is that the explicit check exists and that a refused
        peer never reaches the action dispatcher.
        """
        H, delivered, _app = server
        monkeypatch.setattr(H, "_peer_uid", lambda conn: os.getuid() + 1)
        assert self._roundtrip(H.CONTROL_SOCK, "interrupt") == "error: not permitted\n"
        assert "interrupt" not in delivered

    def test_control_socket_allows_our_own_uid(self, server):
        """The same-uid path (every real caller) must be untouched."""
        H, _delivered, _app = server
        assert self._roundtrip(H.CONTROL_SOCK, "status").startswith("state=")

    @staticmethod
    def _bare_roundtrip(sock_path: Path, action: str) -> str:
        """A request with NO capability token: an old client, or a process
        that can reach the socket but has not read the token file."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall(action.encode())
        s.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            part = s.recv(1024)
            if not part:
                break
            reply += part
        s.close()
        return reply.decode()

    def test_a_state_changing_verb_without_the_token_is_refused(
            self, server, monkeypatch):
        """`say` in the user's voice, dropping the conversation and swapping
        the previewed look all ride this socket, and a same-UID process passes
        the uid check — so `_peer_uid` and the 0700 directory are not what
        stands between a compromised child and those verbs. The token is.

        The assertion is on what the assistant was ASKED to do, not only on
        the reply: a refusal that dispatched anyway would be the whole bug.
        """
        H, _delivered, _app = server
        said: list = []
        monkeypatch.setattr(H.Assistant, "say_preview",
                            lambda self, text: said.append(text) or "ok")
        text = self._bare_roundtrip(H.CONTROL_SOCK, "say hello there")
        assert text.startswith("error:") and "control token" in text, text
        assert said == [], "a tokenless request still reached the assistant"
        # ...and WITH the token the very same verb does reach it, so what was
        # refused is the credential rather than the command being broken
        assert self._roundtrip(H.CONTROL_SOCK,
                               "say hello there").startswith("ok")
        assert said == ["hello there"], said

    def test_the_refusal_names_the_token_file(self, server, monkeypatch):
        """A refusal an honest client cannot act on is a dead end: the reply
        has to say WHICH file to read, and which verbs do not need it."""
        H, _delivered, _app = server
        text = self._bare_roundtrip(H.CONTROL_SOCK, "say hello")
        assert text.startswith("error:"), text
        assert "control token" in text
        assert str(H.CONTROL_TOKEN) in text
        for verb in sorted(H.PTT_READ_ONLY):
            assert verb in text, (verb, text)

    def test_read_only_verbs_stay_open_and_state_changing_ones_do_not(
            self, server, monkeypatch):
        """The split itself: every verb in PTT_ACTIONS is on exactly one side,
        the read-only side answers without a token, and the other side refuses
        without one and accepts with it.

        A command ACCEPTED here still runs on the Qt event loop afterwards, so
        this test delivers what it caused instead of leaving it in the queue:
        `handsfree-status` really speaks, and a command executed during whatever
        test next calls processEvents() leaves a TTS worker for a test that has
        already finished — measured, with the leak guard blaming that test.
        The speech channel is stubbed for the same reason, and the drain is not
        decoration: it also pins that each verb REACHED the bubble rather than
        merely avoiding the refusal string.
        """
        H, delivered, app = server
        assert H.PTT_READ_ONLY <= H.PTT_ACTIONS
        # `handsfree-status` is the one read-only verb the server does NOT
        # answer itself (status/level/health/doctor are replied to directly), so
        # it is the one that goes to the Qt thread — and it speaks. Stub the
        # channel, then wait for the dispatch HERE: accepting a command only
        # queues it, and one left queued runs during whichever test next calls
        # processEvents() — measured, as a TTS worker blamed on an unrelated
        # test.
        monkeypatch.setattr(H.Assistant, "_announce_now",
                            lambda self, text: None)
        for verb in sorted(H.PTT_READ_ONLY):
            if verb in ("doctor", "health"):
                continue          # slow diagnostics, exercised elsewhere
            reply = self._bare_roundtrip(H.CONTROL_SOCK, verb)
            assert "control token" not in reply, (verb, reply)
        # ...and a state-changing verb is refused bare, accepted with it
        bare = self._bare_roundtrip(H.CONTROL_SOCK, "preview-clear")
        assert bare.startswith("error:") and "control token" in bare
        accepted = self._roundtrip(H.CONTROL_SOCK, "preview-clear")
        assert "control token" not in accepted, accepted
        deadline = time.time() + 3
        while "handsfree-status" not in delivered and time.time() < deadline:
            app.processEvents()
        assert "handsfree-status" in delivered, (
            f"delivered {delivered} — the accepted command stayed queued, so it "
            f"would run (and speak) inside a later test")

    def test_a_forged_token_file_does_not_authorize(self, server, tmp_path,
                                                    monkeypatch):
        """The server compares against the token IT generated, not against
        whatever the file says now — so overwriting the file is not a way in.
        """
        H, _delivered, _app = server
        monkeypatch.setattr(H, "CONTROL_TOKEN", tmp_path / "forged.token")
        H.CONTROL_TOKEN.write_text("0" * 64 + "\n")
        text = self._roundtrip(H.CONTROL_SOCK, "preview-clear")
        assert text.startswith("error:") and "control token" in text

    def test_the_cli_payload_carries_the_token_only_where_it_is_needed(
            self, H):
        """The `--ptt` half. Read-only verbs are sent bare (one of them is
        polled twenty times a second), everything else leads with the token.
        """
        H._prepare_runtime()
        token = H._rotate_control_token()
        assert token
        for verb in sorted(H.PTT_READ_ONLY):
            assert H._control_payload(verb, [verb]) == verb.encode(), verb
        for verb in sorted(H.PTT_ACTIONS - H.PTT_READ_ONLY):
            data = H._control_payload(verb, [verb])
            assert data.startswith(
                f"{H._CONTROL_TOKEN_PREFIX}{token}\n".encode()), verb
            assert data.endswith(verb.encode()), verb

    def test_a_previewed_pack_is_drawn_by_the_running_bubble(self, server,
                                                             tmp_path):
        """The Appearance panel's live preview, over the real socket.

        The panel cannot draw on the desktop, so the preview is a COMMAND — and
        what has to hold is that the bubble is what ends up drawing it, that a
        folder which is not a pack is refused here in the very words an install
        would use (the same reading, so a preview cannot be more forgiving than
        the install it stands in for), and that the panel can always take it
        back off. Nothing is installed and the settings are not touched: a
        preview is not an edit.
        """
        from PySide6.QtGui import QColor, QImage
        H, _delivered, _app = server
        bubble = H._core_bubble
        source = tmp_path / "cand"
        source.mkdir()
        img = QImage(32, 32, QImage.Format_ARGB32)
        img.fill(QColor(20, 90, 180, 255))
        assert img.save(str(source / "idle.png")), "a real PNG is required"
        (source / "pack.json").write_text(
            json.dumps({"name": "Candidate", "states": {"idle": "idle.png"},
                        "any": "idle.png"}), encoding="utf-8")
        before = bubble.design_picture("idle")
        try:
            reply = self._roundtrip(H.CONTROL_SOCK, f"preview-pack {source}")
            assert "previewing Candidate" in reply, reply
            assert "nothing installed" in reply, reply
            assert Path(bubble.design_picture("idle")) == source / "idle.png"
            assert bubble.design_in_effect() == "image", (
                "a preview has to be drawn by the design that draws pictures")
            assert bubble.installed_packs() == [], "a preview installs nothing"

            # A folder that is not a pack is refused HERE, in the sentence an
            # install would use — and it ends the preview that was up, because
            # the caller asked for this instead.
            refused = self._roundtrip(H.CONTROL_SOCK,
                                      f"preview-pack {tmp_path}").strip()
            assert refused == bubble.install_pack(tmp_path)[1], (
                f"preview and install must refuse in the SAME words: {refused}")
            assert bubble.design_picture("idle") == before

            assert "previewing Candidate" in self._roundtrip(
                H.CONTROL_SOCK, f"preview-pack {source}")
            assert self._roundtrip(H.CONTROL_SOCK, "preview-clear").strip() == \
                "stopped previewing Candidate"
            assert bubble.design_picture("idle") == before
            assert self._roundtrip(H.CONTROL_SOCK, "preview-clear").strip() == \
                "no pack was being previewed"
        finally:
            # The preview is process-global state in the bubble module, and the
            # module is shared with the rest of the suite: leaving one up would
            # be drawing it from another test's assertions.
            bubble.clear_pack_preview()

    def test_generation_bump_is_atomic_under_contention(self, H):
        """Two threads must never claim the same turn generation.

        `self._gen += 1` then `gen = self._gen` was not atomic. Duplicate
        generations defeat the `gen != self._gen` staleness checks and the
        gen-keyed transcript cache, so a second utterance can be answered with
        the first utterance's text. Every increment now goes through
        _bump_gen under one lock.
        """
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        asst = H.Assistant()
        seen: list[int] = []
        guard = threading.Lock()
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)          # maximise interleaving
        try:
            def _worker() -> None:
                for _ in range(150):
                    gen, _cancel = asst._bump_gen()
                    with guard:
                        seen.append(gen)
            threads = [threading.Thread(target=_worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(old_interval)
        assert len(seen) == 8 * 150
        assert len(set(seen)) == len(seen), "two turns claimed the same generation"

    def test_bump_gen_holds_the_counter_lock_across_the_claim(self, H,
                                                             monkeypatch):
        """Deterministic proof that the claim happens under the lock.

        The counter lives in core.lifecycle, so the lock that makes a claim
        atomic is the MODULE's and the probe points at it: a claim that ran
        outside it would hand out a duplicate generation no matter which
        thread called which entry point.

        The reproducible failure was the WIDE shape: `self._gen += 1`, then
        `self._cancel = threading.Event()`, then `gen = self._gen`. Building
        the event between the increment and the read is a real switch point,
        and 8 threads claimed 714 duplicate generations per 32 000 in ~2% of
        turns (128 000 claims: 16 930 duplicates). The narrow one-liner alone
        did NOT reproduce under the GIL — which is exactly why a stress test
        is too weak to pin this and why the lock is asserted directly.
        """
        import core.lifecycle as lifecycle
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        asst = H.Assistant()
        claimed: dict = {}
        reached = threading.Event()  # set when the claimer is inside __enter__
        real_lock = lifecycle._COUNTER_LOCK

        class Probe:
            """Delegates to the real lock, announcing the attempt first.

            Without this the test could only sleep and hope the competing
            thread had reached the claim; signalling from __enter__ makes
            "nothing claimed yet" mean *blocked*, which is the property under
            test — not *not scheduled yet*.
            """

            def __enter__(self):
                reached.set()
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

            def acquire(self, *a, **k):
                return real_lock.acquire(*a, **k)

            def release(self):
                return real_lock.release()

        monkeypatch.setattr(lifecycle, "_COUNTER_LOCK", Probe())

        def _claim() -> None:
            claimed["gen"] = asst._bump_gen()[0]

        before = asst._gen
        real_lock.acquire()
        try:
            t = threading.Thread(target=_claim)
            t.start()
            assert reached.wait(2.0), "the claimer never reached the lock"
            assert "gen" not in claimed, "the claim ignored the counter lock"
        finally:
            real_lock.release()
        t.join(2.0)
        assert claimed.get("gen") == before + 1

    def test_bump_gen_claims_through_the_module(self, H, monkeypatch):
        """The claim has to go through core.lifecycle, not around it.

        A parallel increment left inside the host would keep every
        number-only test green while creating a second writer the module's
        lock does not cover — which is the whole defect. The spy is on the
        module's own method, so only a real delegation satisfies it.
        """
        import core.lifecycle as lifecycle
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        asst = H.Assistant()
        claims: list = []
        real = lifecycle.GenerationCounter.claim

        def spy(self):
            state = real(self)
            claims.append(state.generation)
            return state

        monkeypatch.setattr(lifecycle.GenerationCounter, "claim", spy)
        before = asst._gen
        gen, cancel = asst._bump_gen()

        assert claims == [before + 1], (
            "_bump_gen did not advance the module's counter")
        assert gen == before + 1 and asst._gen == gen
        assert isinstance(cancel, threading.Event)
        assert not cancel.is_set(), "a claimed cancel event must start clear"

    def test_gen_is_a_view_of_the_counter_not_a_copy(self, H):
        """`_gen` has to read the counter, and write back to it.

        The twenty-odd readers (`gen != self._gen`, the gen-keyed transcript
        cache) and the tests that plant a generation (`a._gen = 5`) both go
        through the property. A stored attribute beside the counter would let
        them disagree, which is how a stale turn reads a fresh number.
        """
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        asst = H.Assistant()

        asst._gen = 5
        assert asst._gen == 5 and asst._gen_counter_get().value == 5

        state = asst._gen_counter_get().claim()
        assert state.generation == 6
        assert asst._gen == 6, "a claim has to be visible through _gen"

    def test_a___new___instance_gets_exactly_one_counter(self, H):
        """Instances built with __new__ must not end up with two counters.

        `__new__` skips `__init__` (tests do this), so the counter is created
        lazily — and two threads each creating their own would be two counters
        handing out the same generation, the defect the atomic claim exists to
        prevent. Double-checked creation under `_gen_lock` is the guard.
        """
        asst = H.Assistant.__new__(H.Assistant)
        assert asst._gen == 0, "a fresh instance starts at generation 0"

        seen: list = []
        guard = threading.Lock()
        barrier = threading.Barrier(8)

        def _ask() -> None:
            barrier.wait(timeout=5)
            counter = asst._gen_counter_get()
            with guard:
                seen.append(counter)

        threads = [threading.Thread(target=_ask) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(seen) == 8, "a counter claimant never returned"
        assert len({id(c) for c in seen}) == 1, "two counters for one instance"
        asst._gen = 3
        assert seen[0].value == 3, "_gen must write the one counter in place"

    def test_generation_is_only_bumped_inside_the_module(self):
        """Source guard: the host must not advance the counter by hand.

        `_gen` is a property and `_bump_gen` delegates, so there is no legal
        `self._gen += 1` left in handsoff.py — and a new call site that adds
        one bypasses the module's lock exactly like the pre-fix code did.
        """
        host = (HERE / "handsoff.py").read_text(encoding="utf-8")
        raw = [i + 1 for i, line in enumerate(host.splitlines())
               if line.strip() in ("self._gen += 1",
                                   "self._gen_counter.value += 1",
                                   'self._gen_counter._box["gen"] += 1')]
        assert not raw, (
            f"raw generation increments at handsoff.py:{raw} bypass "
            f"core.lifecycle's lock")
        assert ".claim()" in host, (
            "_bump_gen has to claim through the module's counter")

        core = (HERE / "core" / "lifecycle.py").read_text(encoding="utf-8")
        assert 'counter["gen"] = counter.get("gen", 0) + 1' in core, (
            "core/lifecycle.next_turn is the home of the only increment")

    def test_clear_history_roundtrip_empties_memory_and_disk(self, H, tmp_path, monkeypatch):
        """A model switch in Settings must clear the RUNNING bubble.

        Truncating HISTORY_FILE from another process is not enough: the bubble
        holds the transcript in `_history` and rewrites the whole file on its
        next save, so the old conversation comes straight back. The socket
        action is what makes a model switch actually take effect.
        """
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        hist = tmp_path / "history.json"
        monkeypatch.setattr(H, "HISTORY_FILE", hist)
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "control.sock")
        hist.write_text(json.dumps([{"role": "user", "content": "stale"}]))
        asst = H.Assistant()
        assert asst._history                     # loaded the stale transcript
        asst._history = [{"role": "user", "content": "stale"},
                         {"role": "assistant", "content": "reply"}]
        srv = H.ControlServer(asst)
        srv.start()
        try:
            deadline = time.time() + 5
            reply = ""
            while time.time() < deadline:
                try:
                    reply = self._roundtrip(H.CONTROL_SOCK, "clear-history")
                    break
                except OSError:
                    time.sleep(0.05)
            assert reply.startswith("ok: cleared 2"), reply
        finally:
            srv.stop()
        assert asst._history == []
        assert json.loads(hist.read_text()) == []
        # and a stale in-memory copy cannot come back on the next save
        asst._save_history()
        assert json.loads(hist.read_text()) == []

    def test_clear_history_is_a_documented_ptt_action(self, H):
        assert "clear-history" in H.PTT_ACTIONS
        assert "clear-history" in H.USAGE

    def test_level_command_reports_the_shared_voice_level(self, server):
        # What the Settings → Voice meter polls. It has to be the signal the
        # DESIGNS paint from and it has to say WHERE it came from: a bare
        # number cannot tell "the mic never fed the level" from "the feed
        # arrived but the bubble is not showing it", which is the whole point
        # of having a meter at all.
        H, _delivered, _app = server
        doc = json.loads(self._roundtrip(H.CONTROL_SOCK, "level"))
        assert set(doc) == {"raw", "ui", "source", "age_s", "state", "handsfree"}
        assert doc["raw"] == 0.0 and doc["ui"] == 0.0
        assert doc["source"] == "none", "nothing has fed a level yet"
        assert doc["age_s"] is None
        assert doc["state"] == "idle"


def _bare_assistant(H):
    """An Assistant with no threads, no audio and no Qt event loop.

    Only the voice-level bookkeeping is under test, so this builds the object
    the same way the rest of the suite does (H.Assistant.__new__) and seeds
    just what _emit_level / level_snapshot touch. `seen` collects what the
    signal actually carried, because recording a level without emitting it
    would silently break every design's voice reaction.
    """
    a = H.Assistant.__new__(H.Assistant)
    a._lifecycle_ensure()
    a._state = H.IDLE
    a._level_last = 0.0
    a._level_source = "none"
    a._level_at = 0.0
    seen: list = []
    a.sigLevel = type("S", (), {"emit": staticmethod(seen.append)})()
    a.seen = seen
    return a


class TestSharedVoiceLevel:
    """The one level signal the bubble's designs and the Settings meter share."""

    def test_emit_records_the_value_and_its_source(self, H):
        a = _bare_assistant(H)
        a._emit_level(0.5, "mic")
        assert a.level_snapshot()["raw"] == 0.5
        assert a.level_snapshot()["source"] == "mic"
        a._emit_level(0.9, "tts")
        snap = a.level_snapshot()
        assert snap["raw"] == 0.9 and snap["source"] == "tts", (
            "the bubble's own voice must be distinguishable from the mic")
        assert a.seen == [0.5, 0.9], (
            "the level must still reach sigLevel, or no design reacts to it")

    def test_emit_clamps_and_survives_garbage(self, H):
        a = _bare_assistant(H)
        for bad in (-3.0, 7.0, 0.25):
            a._emit_level(bad, "mic")
            snap = a.level_snapshot()
            assert 0.0 <= snap["raw"] <= 1.0, f"{bad!r} produced {snap['raw']}"
        assert snap["raw"] == 0.25
        a._emit_level(7.0, "mic")
        assert a.level_snapshot()["raw"] == 1.0, (
            "a too-loud value clamps, it does not vanish")
        emitted = len(a.seen)
        for bad in ("loud", None, object()):
            before = a.level_snapshot()["raw"]
            a._emit_level(bad, "mic")
            assert a.level_snapshot()["raw"] == before, (
                f"{bad!r} must be ignored, not zeroed")
        assert len(a.seen) == emitted, "junk must not reach the signal either"

    def test_age_only_moves_on_a_nonzero_level(self, H):
        # silence must NOT look like a fresh feed: a wedged producer emitting
        # exact zeros is the failure this exposes in the Voice meter.
        a = _bare_assistant(H)
        assert a.level_snapshot()["age_s"] is None
        a._emit_level(0.4, "mic")
        first = a.level_snapshot()["age_s"]
        assert first is not None and first < 1.0
        time.sleep(0.05)
        a._emit_level(0.0, "mic")
        assert a.level_snapshot()["age_s"] > first, (
            "a zero level must not reset the age")

    def test_ui_level_falls_back_when_there_is_no_bubble_widget(self, H):
        # headless (or before the widget exists) the smoothed painter value is
        # unknowable, so the meter must show the raw signal rather than zero
        a = _bare_assistant(H)
        a._emit_level(0.33, "ptt")
        snap = a.level_snapshot()
        assert snap["ui"] == 0.33 and snap["source"] == "ptt"

    def test_the_playback_hook_is_a_tagged_publisher(self, H):
        # core.audio calls whatever Assistant.__init__ registered, and that
        # callable is now the tagged publisher — which is what lets the meter
        # say "the bubble's own voice" instead of showing a bare number while
        # the mic is deliberately blanked.
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        mod = getattr(H, "_audio", None)
        if not hasattr(mod, "_level_hook_lock"):
            pytest.skip("core.audio is a stub in this environment")
        a = H.Assistant()
        try:
            with mod._level_hook_lock:
                hook = mod._level_hook
            assert hook is not None, "playback must feed the visual level"
            hook(0.37)
            snap = a.level_snapshot()
            assert snap["raw"] == 0.37 and snap["source"] == "tts", snap
        finally:
            with mod._level_hook_lock:
                mod._level_hook = None
            a.shutdown()

    def test_ui_level_is_the_value_the_painters_read(self, H):
        # the meter exists to show what the DESIGNS use, so _level_ui wins over
        # the raw signal whenever the widget is there to have one
        a = _bare_assistant(H)
        a._emit_level(0.80, "mic")
        a._bubble_widget = type("W", (), {"_level_ui": 0.25})()
        snap = a.level_snapshot()
        assert snap["raw"] == 0.8 and snap["ui"] == 0.25, snap

    def test_snapshot_survives_a_deleted_widget(self, H):
        # RuntimeError is what PySide raises once the C++ object is gone; the
        # health/level readouts run during shutdown and must not explode
        a = _bare_assistant(H)

        class _Gone:
            @property
            def _level_ui(self):
                raise RuntimeError("wrapped C/C++ object has been deleted")

        a._emit_level(0.6, "mic")
        a._bubble_widget = _Gone()
        snap = a.level_snapshot()
        assert snap["raw"] == 0.6 and snap["ui"] == 0.6

    def test_client_rejects_unknown_action(self, H):
        assert H.ptt_client(["nonsense"]) == 2

    def test_client_without_server(self, H, tmp_path, monkeypatch):
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "missing.sock")
        assert H.ptt_client(["status"]) == 1


class TestBubbleMenuHoldGuard:
    def test_right_click_menu_stops_and_guards_hold_timer(self, H, monkeypatch):
        """Opening the modal menu must not let a queued hold start PTT."""
        class Hold:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Assistant:
            _handsfree = False

            def __init__(self):
                self.begin_calls = 0

            def begin_listening(self):
                self.begin_calls += 1

        assistant = Assistant()
        widget = H._core_bubble.BubbleWidget.__new__(H._core_bubble.BubbleWidget)
        widget._assistant = assistant
        widget._hold = Hold()
        widget._pressing = True
        widget._dragging = False
        widget._listening = False

        class Menu:
            def __init__(self, owner):
                self.owner = owner

            def addAction(self, _text):
                return object()

            def addSeparator(self):
                pass

            def exec(self, _pos):
                assert self.owner._menu_open is True
                self.owner._hold_fired()  # simulate the queued timeout
                return None

        monkeypatch.setattr(H._core_bubble, "QMenu", Menu)

        class Event:
            def button(self):
                return H.Qt.RightButton

            def globalPosition(self):
                return types.SimpleNamespace(toPoint=lambda: None)

        widget.mousePressEvent(Event())
        assert widget._hold.stopped is True
        assert assistant.begin_calls == 0


class TestLiveSettingsReload:
    def test_reload_applies_size_colors_design_without_restart(self, H, tmp_path, monkeypatch):
        """reload-settings picks up settings.json: dict, geometry, colours,
        design and widget size — no restart, no new Assistant.

        H is session-scoped: snapshot every touched global and restore it,
        or later tests inherit our values (order-dependent failures)."""
        snap_settings = dict(H.SETTINGS)
        snap_geom = (H._core_bubble.WINDOW_PX, H._core_bubble.BUBBLE_R0, H._core_bubble.GLOW_PAD, H._core_bubble.GEOM_K)
        snap_colors = dict(H._core_bubble.STATE_COLORS)
        cfg = dict(H.DEFAULT_SETTINGS)
        cfg.update(snap_settings)  # keep model/whisper/voice: no cache drops
        cfg.update({
            "bubble_size": 160, "bubble_design": "halo",
            "colors": {"idle": "#111111", "listening": "#222222",
                       "thinking": "#333333", "speaking": "#444444"},
            "handsfree": False,
        })
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(cfg))
        monkeypatch.setattr(H, "SETTINGS_FILE", settings_file)
        a = H.Assistant.__new__(H.Assistant)
        a._shutdown_event = threading.Event()
        a._closed = False
        a._gen = 0
        a._handsfree = False
        a._listener = types.SimpleNamespace(
            start=lambda: (_ for _ in ()).throw(AssertionError("must not start")),
            stop=lambda: (_ for _ in ()).throw(AssertionError("must not stop")),
        )
        a._mic_selfheal_rearm = lambda: (_ for _ in ()).throw(
            AssertionError("must not rearm when handsfree unchanged"))
        a._set = lambda *args, **kwargs: None
        sizes: dict = {}

        class Widget:
            def setFixedSize(self, w, h):
                sizes["size"] = (w, h)

            def update(self):
                sizes["updated"] = True

        a._bubble_widget = Widget()
        try:
            a._on_command("reload-settings")
            assert H.SETTINGS["bubble_size"] == 160
            assert H.SETTINGS["bubble_design"] == "halo"
            assert H._core_bubble.WINDOW_PX == 160
            assert H._core_bubble.BUBBLE_R0 == pytest.approx(160 * 44.0 / 128.0)
            assert H._core_bubble.GEOM_K == pytest.approx(160 / 128.0)
            assert H._core_bubble.STATE_COLORS["idle"].name() == "#111111"
            assert H._core_bubble.STATE_COLORS["speaking"].name() == "#444444"
            assert sizes["size"] == (160, 160) and sizes.get("updated") is True
        finally:
            H.SETTINGS.clear()
            H.SETTINGS.update(snap_settings)
            (H._core_bubble.WINDOW_PX, H._core_bubble.BUBBLE_R0, H._core_bubble.GLOW_PAD, H._core_bubble.GEOM_K) = snap_geom
            H._core_bubble.STATE_COLORS.clear()
            H._core_bubble.STATE_COLORS.update(snap_colors)


# ------------------------------------------------------------------ offscreen launch


class TestOffscreenLaunch:
    def test_bubble_starts_and_opens_control_socket(self, H):
        """Full launch under QT_QPA_PLATFORM=offscreen in a sandboxed HOME."""
        with tempfile.TemporaryDirectory(prefix="handsoff-test-") as tmp:
            home = Path(tmp)
            state = home / "state"
            # sandbox_env, not a hand-built dict: the throw-away HOME/XDG pair
            # and the real user-site PYTHONPATH (a redirected HOME hides the
            # PySide6 installed there) belong to one constructor. The launch's
            # own switches go on top of it.
            env = sandbox_env(home)
            env.update({
                "XDG_STATE_HOME": str(state),
                "QT_QPA_PLATFORM": "offscreen",
                # an empty theme stops Qt from loading the GTK theme, which
                # needs a real display and kills the process headless
                "QT_QPA_PLATFORMTHEME": "",
                "NO_AT_BRIDGE": "1",
                "QT_ACCESSIBILITY": "0",
                "OLLAMA_HOST": "http://127.0.0.1:9",  # unreachable: loader logs, ok
                "HF_HUB_OFFLINE": "1",                # no model download in tests
            })
            for var in ("NIRI_CONFIG", "DISPLAY", "WAYLAND_DISPLAY"):
                env.pop(var, None)
            proc = subprocess.Popen(
                [sys.executable, str(HERE / "handsoff.py")],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                sock = state / "handsoff" / "control.sock"
                deadline = time.time() + 15
                while not sock.exists() and time.time() < deadline:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                assert sock.exists(), (
                    f"bubble exited early (rc={proc.poll()}):\n"
                    + proc.stderr.read().decode(errors="replace")[-2000:]
                )
                # the --ptt client from the test process must reach the bubble
                # (run_driver: same HOME as the bubble it talks to, and the
                # user-site PYTHONPATH the throw-away HOME would otherwise
                # hide)
                out = run_driver(
                    [str(HERE / "handsoff.py"), "--ptt", "status"],
                    home=home, env_extra=env,
                    capture_output=True, text=True, timeout=10,
                )
                assert out.returncode == 0, out.stderr
                assert out.stdout.startswith("state=idle")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


# ------------------------------------------------------------------ settings app


class TestStreamingChat:
    """ollama_chat_stream against a fake local ollama NDJSON server."""

    def test_core_brain_public_names_and_turn_isolation(self):
        from core import brain

        assert callable(brain.ollama_chat)
        assert callable(brain.ollama_chat_stream)
        assert callable(brain.strip_thinking)
        old = brain.TurnStream(1, threading.Event(), queue.Queue())
        new = brain.TurnStream(2, threading.Event(), queue.Queue())
        old.result = {"content": "old"}
        new.result = {"content": "new"}
        assert old.result != new.result
        assert old.generation == 1 and new.generation == 2

    @pytest.fixture  # function-scoped: class-scope-on-instance-method is deprecated (removed in pytest 10)
    def fake_ollama(self):
        """A live fake Ollama on an ephemeral port.

        The server is polled until it actually answers instead of sleeping a
        fixed 0.8 s and hoping: on a loaded machine the readiness bet fails and
        the test reports a broken brain rather than a slow start.
        """
        import subprocess as sp, socket, time
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # sandbox_env: every interpreter child gets the throw-away HOME and the
        # user-site PYTHONPATH, whether or not it loads the app today — a
        # child that grows an app import must not silently read the real one.
        proc = sp.Popen([sys.executable, str(HERE / "tests" / "fake_ollama.py"), str(port)],
                        env=sandbox_env(), stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                # a TCP connect, not an HTTP GET: the fake only implements
                # POST /api/chat, and readiness is exactly "is the listener up"
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        try:
            assert ready, ("fake ollama never came up"
                           f" (rc={proc.poll()})")
            yield base
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_stream_sentences_and_tool_calls(self, H, fake_ollama):
        import queue as qmod
        old_base = H.OLLAMA_BASE
        H.OLLAMA_BASE = fake_ollama
        try:
            q = qmod.Queue()
            msgs = [{"role": "user", "content": "hi"}]
            res = H.ollama_chat_stream(msgs, q, None, H.TOOLS)
            assert res["tool_calls"], "tool call must be collected from the stream"
            assert res["tool_calls"][0]["function"]["name"] == "copy_text"
            s1 = q.get(timeout=2)
            assert s1 == "Copied it."   # content sentence
            assert q.get(timeout=2) is None  # then terminator
            # follow-up with a tool result streams plain sentences
            msgs += [{"role": "assistant", "content": "", "tool_calls": res["tool_calls"]},
                     {"role": "tool", "tool_name": "copy_text", "content": "ok"}]
            q2 = qmod.Queue()
            res2 = H.ollama_chat_stream(msgs, q2, None, H.TOOLS)
            sentences = []
            while True:
                item = q2.get(timeout=2)
                if item is None:
                    break
                sentences.append(item)
            assert sentences == ["One.", "Two.", "Three."], sentences
            assert res2["content"] == "One. Two. Three."
        finally:
            H.OLLAMA_BASE = old_base

    def test_a_turn_with_facts_and_a_hardware_note_is_accepted(self, H,
                                                              fake_ollama,
                                                              monkeypatch):
        """The real conversation builder through the real wire path, against a
        server that enforces Ollama's rule (the fake answers HTTP 500, "system
        message must be at the beginning", to any list with a system message
        after the first — measured live: qwen3.8:27b 500s, gemma4:latest does
        not).

        The host put the remembered facts and the hardware note in their own
        system messages, so every turn carrying either died with "Sorry, my
        brain is offline" on a model that enforces the rule. The turn must now
        reach the model AND the injected blocks must stay out of history —
        they are re-injected every turn, so a persisted copy accumulates.
        """
        old_base = H.OLLAMA_BASE
        H.OLLAMA_BASE = fake_ollama
        try:
            asst = H.Assistant.__new__(H.Assistant)
            asst._tools = types.SimpleNamespace()
            asst._gen = 1
            asst._history = []
            asst._memory = [{"k": "name", "v": "Quinton"}]
            asst._hardware_note = "mic looks silent"
            asst._turn_spoke = False
            asst._maybe_briefing_prefix = lambda text: ""
            asst._save_history = lambda: None
            asst._set = lambda *args, **kwargs: None
            said = []

            def fake_speak(text, gen, cancel, sentence_q=None):
                if sentence_q is None:
                    if text:
                        said.append(text)
                    return
                while True:
                    item = sentence_q.get(timeout=2)
                    if item is None:
                        return
                    said.append(item)

            asst._speak = fake_speak
            monkeypatch.setitem(H.SETTINGS, "streaming_tts", True)
            monkeypatch.setitem(H._BRAIN_STATE, "tools_supported", False)

            H.Assistant._brain_turn(asst, "what is my name", 1,
                                    threading.Event())

            assert said == ["One.", "Two.", "Three."], said
            assert asst._hardware_note == ""      # consumed once, not persisted
            users = [m for m in asst._history if m.get("role") == "user"]
            assert users and users[-1]["content"] == "what is my name", users
            assert "Facts you remember" not in users[-1]["content"]
            assert not any("hardware note" in str(m.get("content", ""))
                           for m in asst._history)
        finally:
            H.OLLAMA_BASE = old_base

    def test_old_stream_cannot_overwrite_new_turn_result(self, H, monkeypatch):
        """A late canceled stream owns its result and cannot replace turn B's."""
        asst = H.Assistant.__new__(H.Assistant)
        asst._tools = types.SimpleNamespace()
        asst._gen = 1
        asst._history = []
        asst._turn_spoke = False
        asst._conversation_for = lambda text: [{"role": "system"},
                                                {"role": "user", "content": text}]
        asst._save_history = lambda: None
        asst._set = lambda *_args: None
        old_started = threading.Event()
        release_old = threading.Event()
        old_cancel = threading.Event()
        new_started = threading.Event()
        new_speaking = threading.Event()
        release_new = threading.Event()
        calls = []

        def fake_stream(_conversation, _q, _cancel, _tools):
            calls.append(len(calls) + 1)
            if calls[-1] == 1:
                old_started.set()
                release_old.wait(2)
                return {"content": "old", "tool_calls": []}
            new_started.set()
            return {"content": "new", "tool_calls": []}

        def fake_speak(_text, gen, _cancel, sentence_q=None):
            if gen == 2:
                new_speaking.set()
                release_new.wait(2)

        monkeypatch.setitem(H.SETTINGS, "streaming_tts", True)
        monkeypatch.setattr(H, "ollama_chat_stream", fake_stream)
        asst._speak = fake_speak
        old = threading.Thread(target=H.Assistant._brain_turn,
                               args=(asst, "old", 1, old_cancel))
        old.start()
        assert old_started.wait(1)
        asst._gen = 2
        new_cancel = threading.Event()
        new = threading.Thread(target=H.Assistant._brain_turn,
                               args=(asst, "new", 2, new_cancel))
        new.start()
        assert new_started.wait(1)
        assert new_speaking.wait(1)
        old_cancel.set()
        release_old.set()
        time.sleep(0.05)
        old_cancel.set()
        release_new.set()
        old.join(2)
        new.join(2)
        assert not old.is_alive() and not new.is_alive()
        assert asst._history[-1]["content"] == "new"


class TestConfirmationLoop:
    def test_confirmation_offer_stops_same_model_tool_loop(self, H, monkeypatch):
        """A confirmation offer must not let a later tool call in the same
        model response confirm and execute it."""
        class Belt:
            _last_images = []

            def __init__(self):
                self.calls = []
                self._last_confirmation_offer = False

            def execute(self, name, args):
                self.calls.append(name)
                self._last_confirmation_offer = name == "wait"
                if name == "wait":
                    return _core_tools.ToolResult("CONFIRM REQUIRED: pending",
                                                  "confirm")
                return _core_tools.ToolResult("unexpected", "error")

        asst = H.Assistant.__new__(H.Assistant)
        asst._tools = Belt()
        asst._gen = 1
        asst._history = []
        asst._turn_spoke = False
        asst._conversation_for = lambda text: [{"role": "system"},
                                                {"role": "user", "content": text}]
        asst._save_history = lambda: None
        asst._speak = lambda *args, **kwargs: None
        monkeypatch.setitem(H.SETTINGS, "streaming_tts", False)
        monkeypatch.setattr(H, "ollama_chat", lambda conversation, tools: {
            "content": "",
            "tool_calls": [
                {"function": {"name": "wait", "arguments": {"seconds": 1}}},
                {"function": {"name": "confirm_action", "arguments": {"answer": "yes"}}},
            ],
        })

        H.Assistant._brain_turn(asst, "do it", 1, threading.Event())

        assert asst._tools.calls == ["wait"]


# -------------------------------------------------------------------- audit fixes


class TestRestartScriptSystemdAware:
    """The restart script must defer to systemd when the unit exists
    (a nohup spawn would race the unit's Restart=on-failure)."""

    def test_restart_script_defers_to_systemd(self):
        # Test the repo copy (the source of truth the installer ships), not
        # the installed one: a clean checkout has nothing installed, and
        # checking only the installed copy let the repo version rot.
        script = HERE / "handsoff-restart"
        assert script.exists()
        text = script.read_text()
        assert "systemctl --user is-active" in text
        assert "systemctl --user restart handsoff.service" in text
        assert "exit 0" in text      # systemd path must not fall through to nohup


class TestToolSchemaFromCode:
    """@tool decorator: Python functions ARE the Ollama tool schema."""

    def test_every_tool_has_valid_schema(self, H):
        assert len(H.TOOLS) >= 18
        for t in H.TOOLS:
            fn = t["function"]
            assert fn["name"] and fn["description"].strip()
            params = fn["parameters"]["properties"]
            assert all(v.get("type") in ("string", "integer", "number", "boolean")
                       for v in params.values())
            assert set(fn["parameters"]["required"]) <= set(params)

    def test_decorator_extracts_params_from_signature_and_docstring(self, H):
        @_core_tools.tool(description="Test tool.")
        def sample(self, city: str, days: int = 3) -> str:
            """Do a thing.

            city: which city to use
            """
        params = _core_tools._param_schema(sample)
        assert params == {
            "city": {"type": "string", "description": "which city to use"},
            "days": {"type": "integer"},
        }

    def test_string_annotations_coerce(self, H, monkeypatch, tmp_path):
        """from __future__ annotations arrive as strings; bool/int must still coerce."""
        monkeypatch.chdir(tmp_path)
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder", {"name": "tea",
                                                 "when": "600",
                                                 "repeat": "24"})
        assert not err and "repeating every 24 hours" in out, out
        row = H.json.loads(rf.read_text())[0]
        assert row["repeat_hours"] == 24.0 and row["name"] == "tea"

    def test_alias_resolution_via_decorator(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder",
                                {"what": "tea", "when": "30", "every": "0"})
        assert not err and "reminder 'tea' set" in out, out
        assert len(H.json.loads(rf.read_text())) == 1

    def test_unknown_tool_still_errors(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("definitely_not_a_tool", {})
        assert err and "unknown tool" in out


class TestRestartResilience:
    """Why 'the bubble won't start again after a restart' happened, pinned forever."""

    def test_startlimit_in_unit_section(self):
        """StartLimitIntervalSec/Burst MUST be in [Unit] — in [Service] systemd
        silently ignores them and the crash-loop guard disappears."""
        section = None
        seen = {}
        for line in (HERE / "install.sh").read_text().splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1]
            elif s.startswith("StartLimitIntervalSec=") or s.startswith("StartLimitBurst="):
                seen[s.split("=")[0]] = section
        assert seen.get("StartLimitIntervalSec") == "Unit"
        assert seen.get("StartLimitBurst") == "Unit"

    def test_restart_always_recovers_clean_exits(self):
        """Restart=always: SIGTERM / Quit / app.quit() must resurrect, not just crashes."""
        text = (HERE / "install.sh").read_text()
        assert "\nRestart=always" in text

    def test_pacman_python_targets_probed_individually(self):
        """CachyOS has no python-pyside6/python-sounddevice in its repos; pacman
        aborts the WHOLE transaction on an unknown target, which killed the
        entire install. Each python target must be probed and skipped, with
        requirements.txt as the fallback provider."""
        text = (HERE / "install.sh").read_text()
        assert 'pacman -Si "$p"' in text, "python targets must be probed per-package"
        assert "$ARCH_PKGS" in text, "transaction must use the probed package list"
        assert "-Syu --needed --noconfirm $ARCH_PKGS" in text

    def test_installer_restarts_active_service(self):
        """An already-running bubble keeps executing the OLD code after a new
        install until restarted — the manifest would say in-sync while the
        live process serves stale logic. The installer must restart it."""
        text = (HERE / "install.sh").read_text()
        assert "systemctl --user is-active --quiet handsoff.service" in text
        assert "systemctl --user restart handsoff.service" in text

    def test_lock_retry_budget_covers_restart_window(self, H):
        """The lock retry loop must outlast the restart script's kill+wait window."""
        assert H.LOCK_RETRIES * H.LOCK_RETRY_WAIT >= 8.0

    def test_installer_enables_correct_ydotoold_unit(self):
        """Arch's user unit is ydotool.service (it starts ydotoold); requiring
        ydotoold.service only made the installer print a WARN while typing
        tools stayed offline despite a perfectly startable unit."""
        text = (HERE / "install.sh").read_text()
        assert "for u in ydotool.service ydotoold.service" in text
        assert "ydotoold running via" in text

    def test_installer_ships_every_core_module(self):
        """core/ ships as a SET, not a hand-maintained list.

        core/theme.py was added and silently never installed: staging, the
        switch list, the rollback list and the deployment manifest each
        enumerated core modules by name, so the settings GUI lost wallpaper
        matching while doctor still reported `in-sync` — the manifest can only
        hash files it was told about. This pins the design that makes a new
        module deployable without editing install.sh at all.
        """
        text = (HERE / "install.sh").read_text()
        # Staging, the switch list and rollback all glob the tree.
        assert '"$HERE"/core/*.py' in text, "core/ must be staged by glob"
        assert '"$STAGE_DIR"/core/*.py' in text, (
            "the switch list must be built from what was staged")
        assert '"$PREV"/core/*.py' in text, (
            "rollback must restore the whole saved core/ set")
        assert "CORE_REQUIRED=" in text, (
            "the hand-maintained list may only survive as an explicit floor")
        # ...and the manifest hashes the staged set instead of enumerating it.
        # The stanza lives inside a `<<MANIFEST_EOF ... MANIFEST_EOF` heredoc,
        # which is what _deployment_snapshot() in handsoff.py diffs against.
        open_tag = "<<MANIFEST_EOF"
        close_tag = "MANIFEST_EOF"
        start = text.find(open_tag)
        end = text.find(close_tag, start + len(open_tag))
        assert start != -1 and end != -1, "MANIFEST_EOF heredoc not found"
        manifest_body = text[start + len(open_tag):end]
        assert "$manifest_core_files" in manifest_body, (
            "the manifest must hash every staged core module")
        assert '"core/doctor.py"' not in manifest_body, (
            "a literal core/ entry is exactly how a new module escapes drift "
            "detection — the manifest must be generated")
        # The generator must hash both copies per module, or doctor cannot
        # see a stale deployment.
        generator = text[text.index("manifest_core_files="):start]
        assert "source_sha256" in generator and "installed_sha256" in generator

    def test_installer_defines_the_shipped_set_once(self):
        """The shipped top-level set must be discovered, not re-listed.

        It used to be written out in seven separate steps (rollback,
        uninstall, staging, the compile gate, the prev save, the switch and
        the manifest), so a module added beside handsoff.py had seven
        independent ways to be forgotten — the same defect that shipped
        core/theme.py nowhere while doctor reported in-sync.
        """
        text = (HERE / "install.sh").read_text()
        assert "TOP_REQUIRED=" in text, "the required floor must be explicit"
        assert 'for src in "$HERE"/*.py; do' in text, "staging must glob"
        assert 'STAGED_PY=("$STAGE_DIR"/*.py)' in text, "compile must glob"
        assert 'for f in "$STAGE_DIR"/*.py "$STAGE_DIR/handsoff-restart"' in text, \
            "the prev save must take the stage's own list"
        assert 'for f in "$PREV"/*.py' in text, "rollback must glob"
        assert "manifest_top_files=" in text, "the manifest must be generated"
        # The old hand-written lists must be gone, not merely joined.
        assert "for mod in settings_schema hardware" not in text
        assert 'SWITCH_FILES_644="settings_schema.py hardware.py"' not in text
        assert '"$BIN_DIR/settings_schema.py"' not in text, (
            "uninstall must not hard-list the deployed files")
        assert '"$BIN_DIR"/*.py' not in text, (
            "uninstall must never sweep the shared ~/.local/bin")

    def test_installer_ships_only_files_the_project_owns(self):
        """Top-level membership is `declared + what git tracks`, not `*.py`.

        A bare glob delivered whatever happened to sit beside handsoff.py: a
        scratch `test.py` from a TTS experiment was copied into ~/.local/bin
        (the user's PATH) and hashed into the deployment manifest, so the next
        edit to that scratch file made `--ptt doctor` report the WHOLE
        installation as "installed-drift" — a false alarm about the bubble,
        raised by a file the bubble never uses. A stray name can also collide
        with a real binary in $BIN_DIR and overwrite it.

        Discovery must stay automatic (the earlier audit's point: no list to
        maintain), so this pins the rule and its single definition rather than
        any enumeration: the helper is called from staging, from the manifest
        generator and from the rehearsal check, and the glob survives only as
        the no-git fallback.
        """
        text = (HERE / "install.sh").read_text()
        assert "ship_file()" in text, "membership needs one named definition"
        assert "ship_top() { ship_file" in text and "ship_core() { ship_file" in text, (
            "core/ and the top level must share that one rule, not duplicate it")
        assert 'git -C "$HERE" ls-files' in text, (
            "tracked files are how a new module ships without editing this")
        assert 'TRACKED_PY=""' in text, "the no-git fallback must be explicit"
        assert text.count("ship_top ") + text.count("ship_core ") >= 6, (
            "staging, the manifest and the rehearsal check must agree, for both")
        assert "CORE_REQUIRED=" in text and "DECLARED_PY=" in text, (
            "the declared floor and the declared set must both be explicit")
        # the manifest generator is inside the MANIFEST heredoc's vicinity and
        # must filter with the same helper, or doctor tracks what never shipped
        gen = text[text.index("manifest_top_files="):text.index("<<MANIFEST_EOF")]
        assert 'ship_top "$rel" || continue' in gen, (
            "the manifest must use the same membership rule as staging")

    # The files the rule has an opinion about, and what they are for: three
    # declared entry points, two scratch files nobody should ship, and one module
    # that is NOT declared but can still be TRACKED — the case that proves git is
    # how a new module ships without editing the declared set.
    SHIP_PROBES = ("handsoff.py", "handsoff-settings.py", "core/tools.py",
                   "test.py", "scratch_probe.py", "core/zz_future.py")
    SHIP_DECLARED = ("handsoff.py", "handsoff-settings.py", "core/tools.py")

    def test_ship_file_ships_the_declared_set_and_the_tracked_list(self, tmp_path):
        """Both branches of the installer's membership rule, in its own repo.

        The git rule fixed `test.py` LEAVING a checkout — but the fallback used
        when there is no git tree returned 0 for every path, so a tarball built
        from the working directory (not from `git archive`, which carries no
        untracked file) still shipped the scratch and hashed it into the
        manifest. The incident came back through the other door, so: with no git
        there is no ownership signal at all, and the fallback is the DECLARED set
        rather than `whatever the glob finds`.

        This runs the REAL `ship_file` out of install.sh (extracted, not
        re-implemented) against all three states of `TRACKED_PY`, so it pins the
        behaviour and not the text. The repository it runs in is its OWN, built
        here: the property belongs to the rule, not to the developer's checkout,
        and a tree with no `.git` at all (which is how the suite runs under the
        pre-commit hook's staged copy) has no tracked list to consult — the
        earlier version of this test died there on `set -e`, which made the whole
        suite unrunnable in a file-only copy and hid this half of the rule.
        """
        text = (HERE / "install.sh").read_text()

        def var(name):
            head = f'{name}="'
            start = text.index(head) + len(head)
            return text[start:text.index('"', start)]

        fn = text[text.index("ship_file() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        assert "DECLARED_PY" in fn and "TRACKED_PY" in fn, fn

        prelude = (
            'set -eu\n'
            f'TOP_REQUIRED="{var("TOP_REQUIRED")}"\n'
            f'CORE_REQUIRED="{var("CORE_REQUIRED")}"\n'
            'DECLARED_PY="$TOP_REQUIRED handsoff-settings.py"\n'
            'for m in $CORE_REQUIRED; do\n'
            '    DECLARED_PY="$DECLARED_PY core/$m.py"\n'
            'done\n')
        # Every probe reported by name, so an assertion can be about WHICH file
        # took which branch instead of how many colons came back.
        body = (
            'for rel in ' + " ".join(self.SHIP_PROBES) + '; do\n'
            '    if ship_file "$rel"; then verdict=ship; else verdict=keep; fi\n'
            '    printf \'%s=%s\\n\' "$rel" "$verdict"\n'
            'done\n')
        root = tmp_path / "ship"
        (root / "core").mkdir(parents=True)
        for rel in self.SHIP_PROBES:
            (root / rel).write_text("# probe\n", encoding="utf-8")
        env = sandbox_env()
        assert subprocess.run(["git", "-C", str(root), "init", "-q"],
                              capture_output=True, text=True,
                              env=env).returncode == 0
        assert subprocess.run(["git", "-C", str(root), "add",
                               *self.SHIP_DECLARED], capture_output=True,
                              text=True, env=env).returncode == 0

        def verdicts(tracked: str) -> dict:
            # sandbox_env, not a bare launch: this child is bash, but the rule
            # the suite enforces is that NO child inherits the developer's
            # HOME/XDG, whatever it runs — and the guard in test_sandbox.py
            # cannot tell a shell probe from an app load from the argv alone
            # (these strings name `handsoff.py`, one of its markers).
            proc = subprocess.run(
                ["bash", "-c", prelude + fn + f'\nTRACKED_PY="{tracked}"\n'
                 + body],
                cwd=root, capture_output=True, text=True, env=env)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            return dict(line.split("=", 1) for line in proc.stdout.splitlines())

        # 1. No git: the declared set is the whole answer.
        nothing = verdicts("")
        assert set(nothing) == set(self.SHIP_PROBES), nothing
        assert [k for k, v in nothing.items() if v == "ship"] == \
            list(self.SHIP_DECLARED), (
                "a no-git tree must ship ONLY the declared set: " + str(nothing))

        # 2. A tracked list that holds the declared files only: the scratch stays
        #    home, whatever the glob would have found.
        listed = verdicts('$(git -C . ls-files -- "*.py")')
        assert [k for k, v in listed.items() if v == "ship"] == \
            list(self.SHIP_DECLARED), (
                "the untracked scratch must not ship even where git exists: "
                + str(listed))

        # 3. And the point of consulting git at all: a module that is tracked but
        #    NOT declared ships, which is how a new file joins the deployment
        #    without anyone editing install.sh.
        assert subprocess.run(["git", "-C", str(root), "add", "core/zz_future.py"],
                              capture_output=True, text=True,
                              env=env).returncode == 0
        grown = verdicts('$(git -C . ls-files -- "*.py")')
        assert grown["core/zz_future.py"] == "ship", (
            "a tracked module outside the declared set is exactly what the "
            "tracked half of the rule is for: " + str(grown))
        assert grown["test.py"] == "keep" and grown["scratch_probe.py"] == "keep", (
            "and tracking the new module does not drag the scratch in: "
            + str(grown))

    def test_no_untracked_scratch_sits_in_a_shipped_directory(self):
        """Excluding a scratch file is not the same as it not being there.

        Both answers to "what should the installer do with it?" have burned us:
        shipping it put it in the user's PATH and hashed it into the deployment
        manifest, so the next edit to the scratch made `--ptt doctor` report the
        WHOLE installation as drift — a false alarm raised by a file the bubble
        never uses — and then the no-git fallback shipped it anyway, after the
        tracked-only rule was already in place.

        The root and `core/` are the directories the installer decides about by
        glob, so a file there is a file it must have an opinion on. Keep them
        clean instead of merely ignored: anything in either one that git does
        not track fails here, when it appears, rather than at the next install.
        Commit it if it is a module; otherwise it belongs outside the tree.
        """
        try:
            listed = subprocess.run(
                ["git", "-C", str(HERE), "ls-files", "-z"],
                capture_output=True, text=True, env=sandbox_env())
        except FileNotFoundError:
            pytest.skip("no git — tracking cannot be consulted")
        if listed.returncode != 0:
            pytest.skip("not a git work tree — nothing to compare against")
        tracked = {p for p in listed.stdout.split("\0") if p}

        # what the installer stages by glob, plus the one file it names
        candidates = sorted(p.relative_to(HERE).as_posix()
                            for p in list(HERE.glob("*.py"))
                            + list((HERE / "core").glob("*.py")))
        candidates.append("handsoff-restart")
        offenders = [c for c in candidates
                     if c != "handsoff-restart" and c not in tracked]
        assert not offenders, (
            "untracked scratch in a directory the installer ships by glob — it "
            "would land in $BIN_DIR and be hashed into the deployment "
            f"manifest: {offenders}")

        # Media scratch is not shippable, but it is still 7 MB of clutter that
        # nothing in the tree reads — and a stray `*.wav` beside handsoff.py is
        # how the voice-clip experiment left its copies behind in the first
        # place. The live reference lives in ~/.config/handsoff/voice-clips/.
        stray_media = sorted(p.name for p in HERE.glob("*.wav"))
        assert not stray_media, (
            "stray media beside handsoff.py — move it out of the tree (the "
            f"configured voice reference is under the config dir): {stray_media}")

    def test_lock_failure_logs_instead_of_silent_exit(self, H, monkeypatch):
        """If the lock can't be acquired, say so in the log (no more silent vanish)."""
        import builtins
        msgs = []
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if str(path).endswith("handsoff.lock"):
                raise OSError("simulated contention")
            return real_open(path, *a, **k)

        class FakeLog:
            def error(self, *a):
                msgs.append(a)

            def warning(self, *a):
                pass

            def info(self, *a):
                pass

        monkeypatch.setattr(H, "open", fake_open, raising=False)
        monkeypatch.setattr(H, "log", FakeLog())
        monkeypatch.setattr(H, "LOCK_RETRIES", 2)
        monkeypatch.setattr(H, "LOCK_RETRY_WAIT", 0.01)
        assert H.acquire_lock() is None
        assert any("holds the lock" in " ".join(map(str, m)) for m in msgs)

    def test_whisper_loads_offline(self, H):
        """Startup must never block on the HuggingFace network (11.5s stalls)."""
        src = (HERE / "handsoff.py").read_text()
        assert "local_files_only=True" in src

    def test_menu_quit_stops_unit_first(self, H):
        """Quit under Restart=always must stop the systemd unit, not get resurrected.

        The quit path belongs to the bubble's context menu, which now lives in
        core/bubble.py — reading handsoff.py here would assert against a file
        that no longer contains the code being ordered.
        """
        src = (HERE / "core" / "bubble.py").read_text()
        quit_idx = src.index("if chosen == act_quit:")
        stop_idx = src.index('"systemctl", "--user", "stop"', quit_idx)
        app_quit_idx = src.index("QApplication.quit()", quit_idx)
        assert stop_idx < app_quit_idx  # unit stop happens BEFORE the app exits


class TestPrecommitHook:
    """The versioned pre-commit gate: self-edits that break the suite
    must not be committable. core.hooksPath pins the hook to clones."""

    def test_precommit_hook_is_versioned(self):
        hook = HERE / "githooks" / "pre-commit"
        assert hook.exists(), "githooks/pre-commit went missing"
        text = hook.read_text()
        # the hook must actually gate the things that rot
        assert "py_compile" in text
        assert "bash -n" in text
        assert "pytest" in text
        assert "--no-verify" in text   # documented escape hatch

    def test_hook_blocked_commit_is_reproducible(self):
        """Replay the refusal: run the hook's compile leg against a broken
        file the way git would (staged, cwd = repo root)."""
        import subprocess as sp
        broken = HERE / "zz_hook_probe_broken.py"
        broken.write_text("def broken(:\n    pass\n", encoding="utf-8")
        try:
            r = run_driver(["-m", "py_compile", str(broken)],
                           capture_output=True)
            assert r.returncode != 0, "py_compile must fail on broken syntax"
        finally:
            broken.unlink(missing_ok=True)


class TestStagedRelease:
    """install.sh stages + gates a release before touching ~/.local/bin,
    keeps the previous set for rollback, and auto-restores on switch failure.
    """

    REHEARSAL_HOME_NAME = "rehearsal-home"

    def _fake_home(self, tmp_path, bin_py="# deployed handsoff\n"):
        """Rehearsal mode re-roots HOME at HANDSOFF_REHEARSAL_ROOT, so the
        pre-existing deployment must live inside that same tree."""
        home = tmp_path / self.REHEARSAL_HOME_NAME
        (home / ".local" / "state").mkdir(parents=True)
        conf = home / ".config" / "handsoff"
        conf.mkdir(parents=True)
        if bin_py is not None:
            bin_dir = home / ".local" / "bin"
            (bin_dir / "core").mkdir(parents=True)
            for f in ("handsoff.py", "settings_schema.py", "hardware.py",
                      "handsoff-restart"):
                (bin_dir / f).write_text(bin_py)
            (bin_dir / "core" / "__init__.py").write_text(bin_py)
            # A module the old hand-maintained lists would have dropped on
            # rollback: sentinel bytes prove the restore is glob-driven.
            (bin_dir / "core" / "theme.py").write_text(SENTINEL_CORE_BYTES)
        return home, conf

    def _run_rehearsal(self, tmp_path, home):
        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "HANDSOFF_REHEARSAL_ROOT": str(home),
            "HANDSOFF_SKIP_SYSTEM_PKGS": "1",
            "HANDSOFF_NO_OLLAMA_SERVICE": "1",
        })
        return subprocess.run(
            ["bash", str(HERE / "install.sh"), "--rehearsal"],
            env=env, capture_output=True, text=True, timeout=300,
        )

    def test_rehearsal_never_delivers_a_scratch_file(self, tmp_path):
        """End to end: an untracked *.py in the checkout stays out of ~/.local/bin.

        The behavioural half of the membership rule above — a rehearsal in a
        throw-away HOME, with a scratch module written beside handsoff.py the
        way a user's experiment really sits there (removed again either way).
        """
        probe = subprocess.run(["git", "-C", str(HERE), "rev-parse",
                                "--is-inside-work-tree"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            pytest.skip("not a git work tree: the installer falls back to the glob")
        scratch = HERE / "test_zz_scratch_experiment.py"
        assert not scratch.exists(), f"refusing to clobber an existing {scratch}"
        home, conf = self._fake_home(tmp_path)
        scratch.write_text("print('a user experiment, not part of the app')\n")
        try:
            r = self._run_rehearsal(tmp_path, home)
            assert r.returncode == 0, r.stderr[-3000:]
            assert f"NOT shipping {scratch.name}" in r.stdout, r.stdout[-2000:]
            assert not (home / ".local" / "bin" / scratch.name).exists(), \
                "a scratch file was delivered into the user's PATH"
            manifest = json.loads((conf / "deployment.json").read_text())
            assert scratch.name not in manifest["files"], \
                "doctor would track a file the bubble never uses"
            # ...while the project's own modules still shipped
            assert "handsoff.py" in manifest["files"]
            assert "core/theme.py" in manifest["files"]
        finally:
            scratch.unlink(missing_ok=True)

    def test_rehearsal_switches_and_saves_previous_release(self, tmp_path):
        """A rehearsal install must gate through the stage and keep the old
        deployed set at releases/prev for --rollback."""
        home, conf = self._fake_home(tmp_path, bin_py="# OLD deployed bytes\n")
        r = self._run_rehearsal(tmp_path, home)
        assert r.returncode == 0, r.stderr[-3000:]
        assert "previous release kept" in r.stdout
        prev = conf / "releases" / "prev"
        assert (prev / "handsoff.py").read_text() == "# OLD deployed bytes\n"
        assert (prev / "core" / "__init__.py").exists()
        # live bin now serves the CHECKOUT bytes, not the old ones
        assert (home / ".local" / "bin" / "handsoff.py").read_text() != "# OLD deployed bytes\n"
        # the stage is cleaned up
        assert not list((conf / "releases").glob("staged.*"))

    def test_rollback_restores_previous_bytes(self, tmp_path):
        """install.sh --rollback must put the saved previous release back."""
        home, conf = self._fake_home(tmp_path, bin_py="# OLD deployed bytes\n")
        r = self._run_rehearsal(tmp_path, home)
        assert r.returncode == 0, r.stderr[-3000:]
        bin_handsoff = home / ".local" / "bin" / "handsoff.py"
        assert bin_handsoff.read_text() != "# OLD deployed bytes\n"
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["XDG_STATE_HOME"] = str(home / ".local" / "state")
        rb = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--rollback"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert rb.returncode == 0, rb.stderr
        assert bin_handsoff.read_text() == "# OLD deployed bytes\n"
        # ...including core modules the previous release had: rollback restores
        # the whole saved set, not just the modules someone remembered to list.
        assert (home / ".local" / "bin" / "core" / "theme.py").read_text() \
            == SENTINEL_CORE_BYTES

    def test_rollback_without_previous_release_fails_cleanly(self, tmp_path):
        home, conf = self._fake_home(tmp_path, bin_py=None)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["XDG_STATE_HOME"] = str(home / ".local" / "state")
        rb = subprocess.run(
            ["bash", str(HERE / "install.sh"), "--rollback"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert rb.returncode != 0
        assert "no previous release" in rb.stderr

    def test_switch_gates_on_staged_compile(self):
        """A staged set that cannot byte-compile must never reach the live
        bin — the gate must run before any install into BIN_DIR."""
        text = (HERE / "install.sh").read_text()
        # The staged set is globbed from the stage itself, so whatever shipped
        # is what was compiled — a listed set could name a file it never staged.
        assert 'STAGED_PY=("$STAGE_DIR"/*.py)' in text
        assert 'STAGED_PY+=("$STAGE_DIR"/core/*.py)' in text
        assert 'py_compile "${STAGED_PY[@]}"' in text
        compile_line = text.index('py_compile "${STAGED_PY[@]}"')
        first_install = text.index('install -m 755 "$STAGE_DIR/$f" "$BIN_DIR/$f"')
        assert compile_line < first_install, "compile gate must precede the switch"

    def test_switch_failure_restores_previous_release(self):
        """switch_fail must reinstall the saved prev set, never leave the bin
        half-old half-new."""
        text = (HERE / "install.sh").read_text()
        assert "switch_fail()" in text
        assert 'previous release restored' in text
        # switch_fail must reference the prev dir and reinstall from it
        sf = text[text.index("switch_fail()"):text.index("switch_fail()") + 2000]
        assert "$PREV_DIR/$f" in sf
        assert "install -m" in sf
