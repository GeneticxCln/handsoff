"""Fault-injection pass: break the boundaries the bubble depends on, on purpose.

Every other suite proves what the bubble does when things work. This one breaks
one external seam at a time — Ollama refusing or going quiet, the mic handing
back nothing, dbus-monitor dying, a disk write failing, the control socket
vanishing — and asserts the bubble degrades **loudly**: something a person can
find without a debugger (a WARNING/ERROR in the journal, a spoken line naming
the cause, a reported failure, a toggle that stops claiming to be on), plus the
silence of the alternative — no fabricated success, no swallowed error, no value
that is reported as saved when it is not.

Injection happens at the boundary (the HTTP opener, the recorder's return value,
the popen factory, the write path), never by replacing the code under test: the
point is to exercise the real error path, not a stand-in for it.

Five of these tests fail against the tree they were written on — a refused
Ollama escaping as URLError in the streaming path (reported to the user as
"an empty answer"), a key press that produced nothing being dropped in silence,
a failed settings write leaving memory claiming a value the disk never got, the
notification toggle reporting "on" after an unsaved change, and a control socket
that vanished under a live bubble never being noticed. Every fix is in the same
commit, and every guard was verified to fail on the unfixed code (see
GAP_ANALYSIS.md, "fault-injection pass").
"""
from __future__ import annotations

import errno
import json
import logging
import queue
import socket
import sys
import threading
import time
import types
import urllib.error
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import wait_for

BASE = "http://127.0.0.1:11434"


# ------------------------------------------------------------------- helpers

def loud(caplog, needle: str = "") -> list[str]:
    """WARNING+ records from the app's own logger — what lands in journalctl."""
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and needle in r.getMessage()]


def assert_loud(caplog, needle: str) -> None:
    hits = loud(caplog, needle)
    assert hits, (
        "this failure is silent: nothing at WARNING+ mentions %r. Records "
        "seen: %s" % (needle, [(r.levelname, r.getMessage())
                               for r in caplog.records]))


def refused() -> urllib.error.URLError:
    return urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class _QuietResponse:
    """A server that accepted the connection and then never says anything."""

    def __init__(self, entered: threading.Event, release: threading.Event):
        self._entered, self._release = entered, release

    def __enter__(self):
        self._entered.set()
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        self._release.wait(5)      # a hung Ollama: bytes never arrive
        raise StopIteration


# ------------------------------------------------------------- Ollama refuses

class TestOllamaRefuses:
    """The brain being down is the common failure — it must be named, once."""

    def _stream(self, H, urlopen, cancel=None):
        q: queue.Queue = queue.Queue()
        cancel = cancel or threading.Event()
        result = H._brain.ollama_chat_stream(
            [{"role": "user", "content": "hi"}], q, cancel, None,
            base=BASE, model="m", num_ctx=4096, guard=lambda: None,
            logger=logging.getLogger("handsoff.fault"), urlopen=urlopen)
        return result, q

    def test_streamed_refusal_is_an_actionable_error(self, H, caplog):
        """A refused connection must raise RuntimeError, not escape as URLError.

        `_brain_turn` diagnoses a down brain from RuntimeError alone (the
        non-streaming path converts URLError for exactly that reason). Escaping
        as URLError meant a dead Ollama was reported to the user as *"my brain
        gave me an empty answer"* and the real cause surfaced only as an
        unhandled exception in the streamer thread.
        """
        with caplog.at_level(logging.INFO, logger="handsoff"):
            with pytest.raises(RuntimeError) as ei:
                self._stream(H, lambda req, timeout=None: (_ for _ in ()).throw(refused()))
        message = str(ei.value)
        assert BASE in message, message
        assert "systemctl start ollama" in message, \
            "the fix-it hint the non-streaming path gives is missing"
        assert_loud(caplog, "cannot reach")

    def test_a_failed_stream_always_ends_the_queue(self, H):
        """Exactly one terminator, even on failure: the speaker waits on this
        queue, so a missing terminator hangs the turn instead of reporting it."""
        with pytest.raises(RuntimeError):
            self._stream(H, lambda req, timeout=None: (_ for _ in ()).throw(refused()))
        q: queue.Queue = queue.Queue()
        with pytest.raises(RuntimeError):
            H._brain.ollama_chat_stream(
                [{"role": "user", "content": "hi"}], q, threading.Event(), None,
                base=BASE, model="m", num_ctx=4096, guard=lambda: None,
                logger=logging.getLogger("handsoff.fault"),
                urlopen=lambda req, timeout=None: (_ for _ in ()).throw(refused()))
        assert list(q.queue) == [None]

    def test_refusal_reaches_the_user_as_offline_not_empty(self, H, caplog, monkeypatch):
        """End to end, through the streaming path the bubble actually uses."""
        spoken: list[str] = []

        class Belt:
            _last_images: list = []
            _last_confirmation_offer = False

            def _set_user_turn(self, gen):
                pass

        asst = H.Assistant.__new__(H.Assistant)
        asst._tools = Belt()
        asst._gen = 1
        asst._history = []
        asst._turn_spoke = False
        asst._conversation_for = lambda text: [{"role": "system"},
                                               {"role": "user", "content": text}]
        asst._save_history = lambda: None
        asst._speak = lambda text, gen, cancel, sentence_q=None: (
            spoken.append(text) if text else None)
        monkeypatch.setitem(H.SETTINGS, "streaming_tts", True)

        # Inject at the opener the brain is handed, so the wrapper, the
        # sentence queue and _brain_turn are all the real ones.
        real_deps = H._brain_deps

        def refusing_deps():
            deps = real_deps()
            deps["urlopen"] = lambda req, timeout=None: (
                _ for _ in ()).throw(refused())
            return deps

        monkeypatch.setattr(H, "_brain_deps", refusing_deps)
        with caplog.at_level(logging.INFO, logger="handsoff"):
            H.Assistant._brain_turn(asst, "what time is it", 1, threading.Event())

        joined = " ".join(t for t in spoken if t)
        assert "offline" in joined, joined
        assert BASE in joined, f"the user is not told which server is down: {joined}"
        assert "empty answer" not in joined, (
            "a refused connection is still being reported as an empty reply")
        assert asst._history == [], "a failed turn must not be written to memory"

    def test_non_streaming_refusal_says_the_same_thing(self, H):
        """Both paths must diagnose alike — this pins the wording the streaming
        path was fixed to match."""
        with pytest.raises(RuntimeError) as ei:
            H._brain.ollama_chat(
                [{"role": "user", "content": "hi"}], None,
                base=BASE, model="m", num_ctx=4096, guard=lambda: None,
                logger=logging.getLogger("handsoff.fault"),
                urlopen=lambda req, timeout=None: (_ for _ in ()).throw(refused()))
        assert f"cannot reach Ollama at {BASE}" in str(ei.value)

    def test_a_silent_server_still_yields_to_a_barge_in(self, H):
        """Ollama accepting the connection and then never answering must not
        hold the turn: the read loop has to notice `cancel` between bytes."""
        entered, release = threading.Event(), threading.Event()
        cancel = threading.Event()
        q: queue.Queue = queue.Queue()
        box: dict = {}

        def run():
            box["r"] = H._brain.ollama_chat_stream(
                [{"role": "user", "content": "hi"}], q, cancel, None,
                base=BASE, model="m", num_ctx=4096, guard=lambda: None,
                logger=logging.getLogger("handsoff.fault"),
                urlopen=lambda req, timeout=None: _QuietResponse(entered, release))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert entered.wait(3), "the fake server was never asked"
        cancel.set()          # the user interrupts
        release.set()         # ...and the server finally answers
        t.join(5)
        assert not t.is_alive(), "a barge-in left the stream worker running"
        assert list(q.queue) == [None]
        assert box["r"]["content"] == "", \
            "audio queued before the barge-in is still spoken over the user"


# ------------------------------------------------------- the mic returns nothing

class TestMicReturnsNothing:
    """A press that produces no audio is a MIC fault, not a quiet user."""

    def _bare(self, H, handsfree: bool = False):
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = handsfree
        a._gen = 0
        a._is_closed = lambda: False
        states: list = []
        a._set = lambda gen, state: states.append(state)
        queued: list = []
        a._enqueue_pipeline_turn = lambda item: (queued.append(item), True)[-1]
        a.interrupt = lambda: None
        # The stop-probe transcribes the capture on a daemon thread. Nothing
        # in this class is about transcription, and leaving the real seam here
        # made the good-press case load whisper from the host's model dir on a
        # thread that outlived the test — a multi-GB load whose result was
        # published into a global the NEXT test then read.
        a._maybe_instant_stop = lambda audio, gen: None
        return a, states, queued

    def test_a_dead_press_is_loud_and_says_why(self, H, caplog):
        """Silence is reported with the numbers needed to act on it."""
        a, states, queued = self._bare(H)
        quiet = np.zeros(int(H.SAMPLE_RATE * 2.0), dtype=np.int16)
        with caplog.at_level(logging.INFO, logger="handsoff"):
            H.Assistant.submit_audio(a, quiet)
        assert_loud(caplog, "push-to-talk capture rejected")
        line = loud(caplog, "push-to-talk capture rejected")[0]
        for needle in ("frames=", "peak=", "threshold="):
            assert needle in line, f"the journal line is not actionable: {line}"
        assert states == [H.IDLE]
        assert queued == [], "a dead press must not reach the brain"

    def test_no_audio_at_all_is_loud(self, H, caplog):
        """The recorder handing back None (wedged stop / open failure) used to
        be a bare return — indistinguishable from having nothing to say."""
        a, states, queued = self._bare(H)
        with caplog.at_level(logging.INFO, logger="handsoff"):
            H.Assistant.submit_audio(a, None)
        assert_loud(caplog, "no audio from the recorder")
        assert states == [H.IDLE] and queued == []

    def test_hands_free_noise_is_not_an_alarm(self, H, caplog):
        """The distinction that keeps the loud case meaningful: in hands-free,
        a quiet fragment is ordinary and must not cry wolf."""
        a, _states, _queued = self._bare(H, handsfree=True)
        quiet = np.zeros(int(H.SAMPLE_RATE * 2.0), dtype=np.int16)
        with caplog.at_level(logging.INFO, logger="handsoff"):
            H.Assistant.submit_audio(a, quiet)
        assert loud(caplog, "push-to-talk") == [], \
            "hands-free room noise reported as a push-to-talk mic fault"
        assert [r.getMessage() for r in caplog.records
                if "discarding too-short/quiet" in r.getMessage()]

    def test_a_good_press_still_reaches_the_brain(self, H):
        """The guard must not have been bought by refusing real audio."""
        a, _states, queued = self._bare(H)
        good = np.zeros(int(H.SAMPLE_RATE * 2.0), dtype=np.int16)
        good[: int(H.SAMPLE_RATE * 0.5)] = 1200
        H.Assistant.submit_audio(a, good)
        assert len(queued) == 1


# ------------------------------------------------------ dbus-monitor keeps dying

class TestNotificationMonitorDies:
    def test_giving_up_is_loud_and_takes_the_toggle_with_it(self, H, caplog):
        """dbus-monitor dying is not the failure — claiming to be on after it
        is. The reader must stop itself AND say so."""
        from core.assistant import NotificationReader

        class DeadProc:
            stdout: tuple = ()

            def poll(self):
                return 0                 # already exited every time
            def terminate(self):
                pass

        class InstantStop:
            """Exhaust the respawn budget in milliseconds instead of minutes."""
            def is_set(self):
                return False
            def set(self):
                pass
            def wait(self, timeout):
                return False

        saved: list = []
        reader = NotificationReader(
            spawn=lambda *a, **k: None, is_closed=lambda: False,
            announce=lambda *a, **k: None, muted=lambda *a, **k: False,
            popen_factory=lambda *a, **k: DeadProc(),
            persist=lambda k, v: saved.append((k, v)))
        with caplog.at_level(logging.INFO, logger="handsoff"):
            reader.run(InstantStop())
        assert_loud(caplog, "gave up")
        assert saved and saved[-1] == ("notification_reader", False), \
            "the toggle still says 'on' with nothing listening"
        assert reader._proc is None, "the dead monitor is still held"

    def test_a_missing_binary_is_reported_not_assumed(self):
        """The other death: dbus-monitor isn't installed at all."""
        from core.assistant import NotificationReader

        saved: list = []
        reader = NotificationReader(
            spawn=lambda *a, **k: None, is_closed=lambda: False,
            announce=lambda *a, **k: None, muted=lambda *a, **k: False,
            popen_factory=lambda *a, **k: (_ for _ in ()).throw(
                FileNotFoundError("no dbus-monitor")),
            persist=lambda k, v: saved.append((k, v)))
        assert reader.set_enabled(True).startswith("ERROR:")
        assert saved == [("notification_reader", False)]


# ------------------------------------------------------- the disk write fails

class TestDiskWriteFails:
    def _unwritable(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(H, "_core_settings", H._core_settings)
        monkeypatch.setattr(
            H._core_settings, "_read_settings_for_write",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")))

    def test_set_setting_keeps_memory_equal_to_disk(self, H, caplog, monkeypatch, tmp_path):
        """The single settings entry point must not leave the runtime on a value
        the next start cannot read back."""
        self._unwritable(H, monkeypatch, tmp_path)
        monkeypatch.setitem(H.SETTINGS, "mic_threshold", H.SETTINGS["mic_threshold"])
        before = H.SETTINGS["mic_threshold"]
        with caplog.at_level(logging.INFO, logger="handsoff"):
            assert H.set_setting("mic_threshold", 999) is False
        assert H.SETTINGS["mic_threshold"] == before, \
            "memory took a value that never reached the disk"
        assert_loud(caplog, "NOT persisted")

    def test_notification_reader_reports_an_unsaved_toggle(self, H, monkeypatch):
        """The tool result is what the model repeats to the user: saying "on"
        when the save failed is the lie that matters."""
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"], "notifications": True}
        tb._on_notification = lambda enabled: None
        monkeypatch.setattr(H, "set_setting", lambda k, v: False)
        out = tb.notification_reader("start")
        assert "WARNING" in out and "restart" in out, out
        assert "not saved" in out.lower(), out

    def test_an_unsaved_mute_list_is_reported(self, H, monkeypatch):
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"], "notifications": True}
        tb._on_notification = lambda enabled: None
        monkeypatch.setattr(H, "set_setting", lambda k, v: False)
        out = tb.notification_reader("mute", "noisy,noisier")
        assert "NOT saved" in out, out

    def test_handsfree_warns_that_it_will_revert(self, H, caplog, monkeypatch):
        """It really did start the listener, so the session is fine — the user
        must still be told the choice is not on disk."""
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = False
        a._gen = 0
        a._set = lambda gen, state: None
        a._listener = SimpleNamespace(start=lambda: None, stop=lambda: None)
        a._mic_selfheal_rearm = lambda: None
        monkeypatch.setattr(H, "set_setting", lambda k, v: False)
        monkeypatch.setattr(H, "notify", lambda *a, **k: None)
        with caplog.at_level(logging.INFO, logger="handsoff"):
            H.Assistant.set_handsfree(a, True)
        assert_loud(caplog, "revert on restart")
        assert a._handsfree is True, "the session itself must still work"


# -------------------------------------------------- the control socket vanishes

class TestControlSocketDisappears:
    @pytest.fixture()
    def live_server(self, H, tmp_path, monkeypatch):
        """A real ControlServer on a private path, with a stub assistant."""
        sock_path = tmp_path / "control.sock"
        monkeypatch.setattr(H, "CONTROL_SOCK", sock_path)
        asst = SimpleNamespace(state="idle", _handsfree=False,
                               sigCommand=SimpleNamespace(emit=lambda a: None))
        srv = H.ControlServer(asst)
        srv.start()
        assert wait_for(lambda: sock_path.exists()), "server never bound"
        try:
            yield H, srv, sock_path
        finally:
            srv.stop()

    @staticmethod
    def _ask(sock_path: Path, action: str) -> str:
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

    def test_a_vanished_path_is_noticed_and_rebound(self, live_server, caplog):
        """`rm` the socket under a live bubble and it must notice.

        The listener keeps accepting on an inode no client can reach: `--ptt`
        says "not running", doctor says "not created yet", and the live process
        said nothing at all — the bubble was unreachable in total silence.
        """
        H, _srv, sock_path = live_server
        assert self._ask(sock_path, "status").startswith("state=")
        sock_path.unlink()
        with caplog.at_level(logging.INFO, logger="handsoff"):
            assert wait_for(lambda: sock_path.exists(), timeout=8), \
                "the path was never re-created"
            assert_loud(caplog, "removed while running")
            # and it is a WORKING socket again, not just a file
            assert wait_for(
                lambda: self._ask(sock_path, "status").startswith("state="),
                timeout=5)

    def test_a_foreign_socket_at_our_path_is_reported_not_stolen(
            self, live_server, caplog):
        """Reclaiming the path could delete another live bubble's socket, so the
        only honest response is to say so and leave it alone."""
        H, _srv, sock_path = live_server
        sock_path.unlink()
        intruder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        intruder.bind(str(sock_path))
        intruder.listen(1)
        try:
            with caplog.at_level(logging.INFO, logger="handsoff"):
                assert wait_for(lambda: loud(caplog, "another inode"), timeout=8), \
                    "a foreign socket took the path and nothing said so"
            assert intruder.fileno() != -1, "the foreign socket was closed/stolen"
            assert sock_path.stat().st_ino  # still the intruder's path
        finally:
            intruder.close()

    def test_a_moved_path_is_never_repaired_by_an_older_server(self, live_server):
        """A server may only ever repair the path it BOUND.

        CONTROL_SOCK is a module global, and the suite (or an embedder) moves
        it. The first draft of the periodic repair read that global, so a
        server left running from an earlier test materialised a fresh 0600
        socket at whatever path a LATER test had pointed it at — measured, not
        theorised — which showed up as an intermittent failure in
        tests/test_hardening.py::TestSecureFile (the socket it had just made
        0777 was replaced under it). Ownership is now the path string captured
        at bind; anything else is somebody else's socket.
        """
        H, srv, sock_path = live_server
        elsewhere = sock_path.parent / "elsewhere.sock"
        monkeypatch_global = H.CONTROL_SOCK
        H.CONTROL_SOCK = elsewhere
        try:
            time.sleep(1.5)          # past one idle second of the accept loop
            assert not elsewhere.exists(), \
                "a server created a socket on a path it never bound"
            assert sock_path.exists(), "it abandoned its own path instead"
        finally:
            H.CONTROL_SOCK = monkeypatch_global

    def test_a_stopping_server_never_resurrects_the_socket(self, live_server):
        """The rebind must lose every race against shutdown.

        The first draft of this check did not, and the accept loop re-created
        the socket after `stop()` had removed it — leaving behind exactly the
        stale inode this class exists to avoid, which the next test's server
        then had to reason about. `stop()` sets the event before it cleans up,
        so the invariant is: once stopping, the path is never re-bound.
        """
        H, srv, sock_path = live_server
        sock_path.unlink()
        srv._stop.set()                      # as stop() does, before cleanup
        assert srv._rebind(srv._server) is None, \
            "a stopping server re-bound the control socket"
        assert not sock_path.exists(), "shutdown resurrected the socket file"

    def test_the_client_says_which_socket_is_missing(self, H, tmp_path, monkeypatch, capsys):
        """The CLI half: exit 1 is not enough, the user needs to know what is
        missing and where it was looked for."""
        monkeypatch.setattr(H, "CONTROL_SOCK", tmp_path / "gone.sock")
        assert H.ptt_client(["status"]) == 1
        err = capsys.readouterr().err
        assert "not running" in err or "no control socket" in err
        assert "gone.sock" in err, f"the message does not name the socket: {err!r}"


# ------------------------------------------- the compositor / ydotoold vanishes

class TestDesktopServicesVanish:
    """niri and ydotoold are external daemons that can die mid-turn.

    Two very different faults hide behind "nothing happened": the compositor
    being gone (nothing can be verified, so injection must fail closed and the
    report must say so) and the injector being gone (the keys/clicks simply
    never landed). Neither may be reported as the other, and neither may be
    reported as success.
    """

    def test_a_dead_ydotool_socket_is_re_resolved_not_remembered(
            self, H, tmp_path, monkeypatch):
        """A cached socket path must be revalidated, not returned forever.

        `_YDOTOOL_SOCK_CACHE` cached the first connectable candidate for the
        life of the process, so once ydotoold died there every later call was
        handed the dead path — typing and clicking stayed broken with an error
        each time even after the daemon came back on the OTHER candidate
        (runtime dir vs the CLI's compiled-in /tmp default). The docstring
        promised a later daemon would be picked up; the cache made that false.
        """
        runtime = tmp_path / "run"
        runtime.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.setattr(H.ToolBelt, "YDOTOOL_SOCKET", str(tmp_path / "legacy"))
        monkeypatch.setattr(H.ToolBelt, "_YDOTOOL_SOCK_CACHE", [])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        first = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        first.bind(str(runtime / ".ydotool_socket"))
        assert belt._ydotool_socket() == str(runtime / ".ydotool_socket")
        # the daemon dies...
        first.close()
        # ...and comes back as the OTHER candidate
        second = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        second.bind(str(tmp_path / "legacy"))
        try:
            assert belt._ydotool_socket() == str(tmp_path / "legacy"), \
                "a cached-but-dead socket path is still being handed out"
            assert belt._YDOTOOL_SOCK_CACHE == [str(tmp_path / "legacy")]
        finally:
            second.close()

    def test_keys_and_clicks_never_report_a_dead_injector_as_success(
            self, H, monkeypatch):
        """Every ydotool-backed action must carry the failure back."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(belt.__class__, "_ydotool",
                            lambda self, *a: "ERROR: ydotool failed: cannot connect")
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Docs"})
        belt._perm = {**H.DEFAULT_SETTINGS["permissions"], "operator": True}
        out, err = belt.execute("press_keys", {"combo": "ctrl+s"})
        assert err and "cannot connect" in out, out
        out, err = belt.execute("scroll", {"direction": "down"})
        assert err and "cannot connect" in out, out
        out, err = belt.execute("click_at", {"x": 10, "y": 10})
        assert err and "mouse move failed" in out, out
        assert "clicked" not in out

    def test_typing_that_dies_mid_way_says_how_much_landed(self, H, monkeypatch):
        """A half-typed message is not "typed": the split must be reported.

        The chunked path retried once and, if that failed too, used to be able
        to claim success on the strength of the FIRST chunk landing. The count
        is the only thing that tells the user how much of their text arrived.
        """
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Docs"})
        calls = []

        def dying(self, *args):
            calls.append(args)
            return "ok" if len(calls) == 1 else "ERROR: ydotool failed: cannot connect"

        monkeypatch.setattr(belt.__class__, "_ydotool", dying)
        text = "x" * 1200
        out, err = belt.execute("type_text", {"text": text})
        assert err, "a half-typed message was reported as success"
        assert "512/1200" in out, out
        assert "typed 1200" not in out

    def test_the_compositor_dying_is_not_blamed_on_the_app(
            self, H, monkeypatch):
        """"No new window appeared" is a lie when the window list is gone.

        open_app snapshot the window list, spawns, then waits. If niri dies in
        between, every poll fails; the wait loop swallowed that and reported
        the timeout as the launched app failing to draw a window — pointing the
        user at the wrong thing entirely.
        """
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.shutil, "which", lambda p: "/usr/bin/foot")
        monkeypatch.setattr(H.ToolBelt, "_WIN_POLL_S", 0.01)
        monkeypatch.setattr(H.ToolBelt, "OPEN_APP_WAIT_S", 0.2)
        state = {"snapshot_taken": False}

        def niri_dies(self):
            if not state["snapshot_taken"]:
                state["snapshot_taken"] = True
                return []
            raise RuntimeError("niri IPC unavailable (connection refused)")

        monkeypatch.setattr(belt.__class__, "_niri_windows", niri_dies)
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: None)
        out, err = belt.execute("open_app", {"app": "foot"})
        assert "launched foot" in out
        assert "no new window appeared" not in out, (
            "a dead compositor is still being reported as a failed app launch")
        assert any(w in out for w in ("compositor", "IPC is down", "unreachable")), out

    def test_unverifiable_focus_refuses_instead_of_typing_blind(
            self, H, monkeypatch):
        """Fail closed: no window list means no keyboard injection at all."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(
            belt.__class__, "_niri_windows",
            lambda self: (_ for _ in ()).throw(RuntimeError("niri is gone")))
        calls = []
        monkeypatch.setattr(belt.__class__, "_ydotool",
                            lambda self, *a: calls.append(a) or "ok")
        out, err = belt.execute("type_text", {"text": "rm -rf /"})
        assert err and out.startswith("REFUSED"), out
        assert calls == [], "keys were injected with unverifiable focus"


# ------------------------------------------------ the speech models go missing

class TestSpeechModelsUnavailable:
    """Whisper and piper are big files that live outside the package.

    Missing, unreadable or damaged, they are the difference between a bubble
    that answers and one that is simply deaf — and the failure arrives on the
    hottest path (every utterance, every reply) where a raw exception from a
    library thread is invisible to the person waiting.
    """

    @pytest.fixture()
    def hushed(self, H, tmp_path, monkeypatch):
        """Nothing cached, no GPU probe, and no reference clip configured."""
        monkeypatch.setattr(H._audio, "WHISPER_MODEL_DIR", tmp_path / "whisper-model")
        monkeypatch.setattr(H._audio, "_whisper_cpu_fallback", True)
        monkeypatch.setattr(H._audio, "_whisper_model", None)
        monkeypatch.setattr(H._audio, "_tts_model", None)
        monkeypatch.setattr(H._audio, "TTS_REFERENCE", "")
        monkeypatch.setattr(H._audio, "TTS_DEVICE", "cpu")
        monkeypatch.setattr(H._audio, "_tts_device", "")
        monkeypatch.setattr(H._audio, "_nvidia_free_vram_mb", lambda: None)
        monkeypatch.setattr(H, "_whisper_model", None)
        monkeypatch.setattr(H, "_tts_model", None)
        return H

    def _fake(self, monkeypatch, name: str, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    def test_a_missing_whisper_model_names_the_path_and_the_fix(
            self, hushed, monkeypatch):
        """The bare library error names neither the directory nor the remedy."""
        def explode(*a, **kw):
            raise ValueError("Unable to open file 'model.bin'")

        self._fake(monkeypatch, "faster_whisper", WhisperModel=explode)
        with pytest.raises(RuntimeError) as ei:
            hushed.get_whisper()
        message = str(ei.value)
        assert "whisper" in message and "model.bin" in message, message
        assert str(hushed._audio.WHISPER_MODEL_DIR) in message, message
        assert "install.sh" in message, "no remedy is offered"

    def test_a_failed_whisper_load_is_not_cached(
            self, hushed, monkeypatch):
        """A failure must not poison the model: install.sh then retry works."""
        def explode(*a, **kw):
            raise ValueError("model directory is gone")

        self._fake(monkeypatch, "faster_whisper", WhisperModel=explode)
        with pytest.raises(RuntimeError):
            hushed.get_whisper()
        sentinel = object()
        self._fake(monkeypatch, "faster_whisper",
                   WhisperModel=lambda *a, **kw: sentinel)
        assert hushed.get_whisper() is sentinel, \
            "the failed load was cached — the bubble stays deaf after a fix"

    def _fake_engine(self, monkeypatch, from_pretrained):
        """A stand-in chatterbox package whose loader is `from_pretrained`.

        Both modules are injected: `from chatterbox.tts_turbo import ...`
        imports the parent package first, so leaving the real one on the path
        would import torch (absent on CI) instead of exercising this code.
        """
        pkg = self._fake(monkeypatch, "chatterbox")
        pkg.tts_turbo = self._fake(
            monkeypatch, "chatterbox.tts_turbo",
            ChatterboxTurboTTS=types.SimpleNamespace(
                from_pretrained=from_pretrained))
        return pkg

    def _fake_unimportable_engine(self, monkeypatch):
        """An EMPTY chatterbox package, so the submodule import fails."""
        self._fake(monkeypatch, "chatterbox")

    def test_a_missing_engine_names_the_engine_and_the_remedy(
            self, hushed, monkeypatch):
        """A 3.8 GB GPU stack that install.sh owns is a real state to be in,
        and the bare library error names neither the engine nor the fix."""
        def explode(*a, **kw):
            raise OSError("no such file: t3_turbo_v1.safetensors")

        self._fake_engine(monkeypatch, explode)
        with pytest.raises(RuntimeError) as ei:
            hushed.get_tts()
        message = str(ei.value)
        assert "chatterbox-turbo" in message, message
        assert "t3_turbo_v1.safetensors" in message, message   # the real cause
        assert "install.sh" in message, "no remedy is offered"

    def test_an_unimportable_engine_says_to_run_the_installer(
            self, hushed, monkeypatch):
        """"Not installed" must not be reported as "the weights are damaged"
        — one sends the user to install.sh, the other to a 3.8 GB re-download."""
        self._fake_unimportable_engine(monkeypatch)
        with pytest.raises(RuntimeError) as ei:
            hushed.get_tts()
        message = str(ei.value)
        assert "chatterbox is not installed" in message, message
        assert "install.sh" in message, message

    def test_get_tts_actually_applies_the_float32_shim(self, hushed,
                                                      monkeypatch):
        """Defining the shim is not enough — the LOADER has to apply it.

        Without this, deleting one line in get_tts leaves every unit test of
        `_patch_float32_norm` passing while every reference clip in the real
        bubble raises "expected scalar type Float but found Double".
        """
        class _Engine:
            def norm_loudness(self, wav, sr, *a, **kw):
                return np.float64(0.5) * np.asarray(wav, dtype=np.float32)

        self._fake_engine(monkeypatch, lambda *a, **kw: _Engine())
        model = hushed.get_tts()
        out = model.norm_loudness(np.ones(4, dtype=np.float32), 24000)
        assert out.dtype == np.float32, (
            "get_tts loaded the model without the float32 shim: every reference "
            "clip would fail at the mel matmul")

    def test_a_failed_engine_load_is_not_cached(self, hushed, monkeypatch):
        """A failure must not poison the loader: install.sh then retry works."""
        self._fake_engine(monkeypatch, lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("t3_turbo_v1.safetensors is truncated")))
        with pytest.raises(RuntimeError):
            hushed.get_tts()
        sentinel = object()
        self._fake_engine(monkeypatch, lambda *a, **kw: sentinel)
        assert hushed.get_tts() is sentinel, (
            "the failed load was cached — the bubble stays mute after a fix")

    def test_a_too_short_reference_clip_is_refused_before_the_weights_load(
            self, hushed, tmp_path, monkeypatch):
        """`prepare_conditionals` asserts > 5 s, and that assert fires on EVERY
        turn — so a 2 s clip is not a degraded voice, it is a mute bubble. It
        has to be caught before the multi-gigabyte load, and it must blame the
        clip: the old shape of this failure pointed at the weights.
        """
        clip = tmp_path / "optimus_clip.wav"
        with wave.open(str(clip), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 24000)             # exactly 1 s
        monkeypatch.setattr(hushed._audio, "TTS_REFERENCE", str(clip))
        touched = []
        self._fake_engine(monkeypatch, lambda *a, **kw: touched.append(True))
        with pytest.raises(RuntimeError) as ei:
            hushed.get_tts()
        assert touched == [], "a bad clip must be caught BEFORE the 3.8 GB load"
        message = str(ei.value)
        assert "optimus_clip.wav" in message, message
        assert "Settings" in message, "the remedy must be actionable"

    def test_a_missing_reference_clip_is_named_too(self, hushed, tmp_path,
                                                  monkeypatch):
        monkeypatch.setattr(hushed._audio, "TTS_REFERENCE",
                            str(tmp_path / "gone.wav"))
        self._fake_engine(monkeypatch, lambda *a, **kw: None)
        with pytest.raises(RuntimeError) as ei:
            hushed.get_tts()
        assert "gone.wav" in str(ei.value), str(ei.value)


# -------------------------------------- a turn or a reply that never happened

class TestTurnAndSpeechFailures:  # noqa: D101 - see module docstring
    """The two places a failure was swallowed with nothing but a log line."""

    def _assistant(self, H, monkeypatch, tmp_path):
        """A bare Assistant, wired only with what speech and state need."""
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 7
        a._is_closed = lambda: False
        a._models_ready = threading.Event()
        a._models_ready.set()
        a._recently_spoken = []
        a._last_spoken = ""
        a._turn_spoke = False
        a._handsfree = True
        a._followup_until = 0.0
        a._states = []
        a._set = lambda gen, state: a._states.append(state)
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 30.0)
        return a

    def test_a_crashed_turn_is_reported_with_its_cause(
            self, H, monkeypatch, tmp_path, caplog):
        """The pipeline worker used to log "crash" and tell nobody.

        The commonest cause is the speech model being unavailable, so every
        utterance raised: the user spoke and got nothing back, forever, with
        the bubble still looking like it was listening.
        """
        a = self._assistant(H, monkeypatch, tmp_path)
        told = []
        monkeypatch.setattr(H, "notify", lambda text: told.append(text))
        spoken = []
        monkeypatch.setattr(H.Assistant, "_speak",
                            lambda self, text, gen, cancel, sentence_q=None:
                                spoken.append(text))
        cancel = threading.Event()
        with caplog.at_level(logging.INFO, logger="handsoff"):
            H.Assistant._report_turn_failure(
                a, RuntimeError("cannot load the whisper 'base' model"), 7, cancel)
        assert a._states == [H.IDLE], "the bubble was left stuck mid-turn"
        assert told and "whisper" in told[0], told
        assert spoken and "whisper" in spoken[0], (
            "the user waiting on the reply was told nothing: %r" % (spoken,))
        assert_loud(caplog, "turn failed")

    def test_a_repeating_turn_failure_alarms_once(
            self, H, monkeypatch, tmp_path):
        """A broken model fails on EVERY turn; one alarm, many log lines."""
        a = self._assistant(H, monkeypatch, tmp_path)
        told = []
        monkeypatch.setattr(H, "notify", lambda text: told.append(text))
        monkeypatch.setattr(H.Assistant, "_speak",
                            lambda self, text, gen, cancel, sentence_q=None: None)
        cancel = threading.Event()
        for _ in range(4):
            H.Assistant._report_turn_failure(a, RuntimeError("no model"), 7, cancel)
        assert len(told) == 1, told
        assert a._states == [H.IDLE] * 4, "state must recover after every failure"
        # a DIFFERENT fault is a different problem and gets its own alarm
        H.Assistant._report_turn_failure(a, OSError("disk gone"), 7, cancel)
        assert len(told) == 2, told

    def test_a_stale_turn_failure_does_not_speak_over_the_newer_turn(
            self, H, monkeypatch, tmp_path):
        """By the time a crash is reported the user may have moved on."""
        a = self._assistant(H, monkeypatch, tmp_path)
        monkeypatch.setattr(H, "notify", lambda text: None)
        spoken = []
        monkeypatch.setattr(H.Assistant, "_speak",
                            lambda self, text, gen, cancel, sentence_q=None:
                                spoken.append(text))
        a._gen = 99                       # a newer utterance has taken over
        H.Assistant._report_turn_failure(
            a, RuntimeError("no model"), 7, threading.Event())
        assert spoken == [], "an old failure talked over the current turn"

    def test_the_worker_routes_a_pipeline_crash_to_the_report(
            self, H, monkeypatch, tmp_path):
        """Pins the wiring itself: the worker must not swallow it again."""
        a = self._assistant(H, monkeypatch, tmp_path)
        seen = []
        monkeypatch.setattr(H.Assistant, "_lifecycle_ensure", lambda self: None)
        monkeypatch.setattr(
            H.Assistant, "_pipeline",
            lambda self, audio, gen, cancel: seen.append("ran") or
            (_ for _ in ()).throw(RuntimeError("transcribe failed")))
        monkeypatch.setattr(H.Assistant, "_report_turn_failure",
                            lambda self, exc, gen, cancel: seen.append(str(exc)))
        a._pipeline_q = queue.Queue()
        a._pipeline_q.put((np.zeros(8, dtype=np.int16), 7, threading.Event()))
        a._pipeline_q.put((None, 0, None))          # sentinel: stop the worker
        H.Assistant._pipeline_worker(a)
        assert seen == ["ran", "transcribe failed"], seen

    def test_a_reply_that_could_not_be_spoken_is_not_reported_as_spoken(
            self, H, monkeypatch, tmp_path):
        """Silent piper used to be recorded as a completed, spoken reply.

        `_last_spoken`/`_turn_spoke`/`_recently_spoken` were written BEFORE
        synthesis, so a missing voice left the bubble believing it had
        answered: the follow-up window opened on nothing and the user's next
        utterance was echo-filtered against a line never spoken.
        """
        a = self._assistant(H, monkeypatch, tmp_path)
        told = []
        monkeypatch.setattr(H, "notify", lambda text: told.append(text))
        monkeypatch.setattr(H, "tts_to_wav",
                            lambda *a_, **kw: (_ for _ in ()).throw(
                                FileNotFoundError("no piper voice")))
        H.Assistant._speak(a, "Hello there.", 7, threading.Event())
        assert a._turn_spoke is False, "an unspoken reply was recorded as spoken"
        assert a._last_spoken == "", a._last_spoken
        assert a._recently_spoken == [], (
            "a line that was never spoken would echo-filter the user: %r"
            % (a._recently_spoken,))
        assert a._followup_until == 0.0, "the follow-up window opened on silence"
        assert told and "speak" in told[0].lower(), told

    def test_a_spoken_reply_still_arms_the_echo_filter_and_followup(
            self, H, monkeypatch, tmp_path):
        """The guard must not have been bought by breaking normal speech."""
        a = self._assistant(H, monkeypatch, tmp_path)
        played = []
        monkeypatch.setattr(H, "tts_to_wav", lambda text, path: None)
        monkeypatch.setattr(H, "play_wav", lambda path, cancel: played.append(path))
        H.Assistant._speak(a, "Hello there.", 7, threading.Event())
        assert played, "nothing was played"
        assert a._turn_spoke is True and a._last_spoken == "Hello there."
        assert a._recently_spoken == ["Hello there."], a._recently_spoken
        assert a._followup_until > 0.0, "the follow-up window never armed"

    def test_a_streaming_failure_keeps_the_lines_that_did_play(
            self, H, monkeypatch, tmp_path):
        """One bad sentence must not erase the ones the user heard."""
        a = self._assistant(H, monkeypatch, tmp_path)
        monkeypatch.setattr(H, "notify", lambda text: None)
        played = []
        monkeypatch.setattr(H, "play_wav", lambda path, cancel: played.append(path))
        state = {"n": 0}

        def flaky(text, path):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("voice file vanished")

        monkeypatch.setattr(H, "tts_to_wav", flaky)
        q: queue.Queue = queue.Queue()
        for line in ("First line.", "Second line.", "Third line."):
            q.put(line)
        q.put(None)
        H.Assistant._speak(a, "", 7, threading.Event(), sentence_q=q)
        assert played, "the lines that did play were dropped"
        assert a._last_spoken == "First line. Third line.", a._last_spoken
        assert "Second line." not in a._recently_spoken


# ------------------------------------------------------------- the disk is full

class _NoSpaceFile:
    """A file object that can never accept a byte.

    Everything else is delegated, including `fileno()` — the settings lock
    takes an flock on a file it never writes, and a wrapper that hid the
    descriptor would break the lock instead of the write.
    """

    def __init__(self, fh):
        self._fh = fh

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def write(self, text):
        raise OSError(errno.ENOSPC, "No space left on device")


class _FullDiskOS:
    """`os`, except file objects it hands out cannot be written.

    Injected as `core.settings.os` — that module's OWN reference — so the
    fault lands exactly at the boundary the writer uses to put bytes on the
    disk and nothing else in the process (pytest's own file handling, the
    config directory, other suites) is touched. The real descriptor is still
    closed by the wrapper, so the injection leaks nothing.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def fdopen(self, fd, *args, **kwargs):
        return _NoSpaceFile(self._real.fdopen(fd, *args, **kwargs))


class TestDiskIsFull:
    """ENOSPC is the failure that must never damage what is already on disk."""

    @pytest.fixture()
    def full(self, H, monkeypatch):
        monkeypatch.setattr(H._core_settings, "os",
                            _FullDiskOS(H._core_settings.os))
        return H

    def test_a_failed_write_never_damages_the_previous_file(
            self, H, full, tmp_path):
        """The atomic write is the safety mechanism — prove it, don't assume it."""
        target = tmp_path / "settings.json"
        target.write_text('{"keep": true}')
        before = target.read_bytes()
        with pytest.raises(OSError) as ei:
            H._atomic_private_write(target, '{"keep": false}')
        assert ei.value.errno == errno.ENOSPC
        assert target.read_bytes() == before, "the previous file was damaged"
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"temp files left behind: {leftovers}"

    def test_a_full_disk_does_not_damage_the_backup_either(
            self, H, full, tmp_path):
        """The .bak is the file you restore from, so it must survive too.

        The backup used to be a straight `shutil.copy2` over the old `.bak`:
        when the disk filled mid-copy the BACKUP was left truncated — damaged
        by the very failure backups exist for — while the live file (written
        atomically) was fine.
        """
        target = tmp_path / "memory.json"
        target.write_text('{"new": 1}')
        backup = Path(str(target) + ".bak")
        backup.write_text('{"previous": 1}')
        before = backup.read_bytes()
        H._backup_runtime_json(target)
        assert backup.read_bytes() == before, \
            "a full disk left the backup truncated"

    def test_set_setting_reports_unsaved_instead_of_raising(
            self, H, full, tmp_path, monkeypatch, caplog):
        """`is False` is how every caller learns to warn the user.

        set_setting's contract is a bool (False = nothing reached the disk).
        The write error escaped it, so a full disk made callers that do
        `if set_setting(...) is False` crash with a traceback instead of
        saying the change was not saved.
        """
        monkeypatch.setattr(H, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        before = H.SETTINGS["mic_threshold"]
        with caplog.at_level(logging.INFO, logger="handsoff"):
            assert H.set_setting("mic_threshold", 4242) is False
        assert H.SETTINGS["mic_threshold"] == before, \
            "memory took a value that never reached the disk"
        assert_loud(caplog, "persist_setting")

    def test_a_reminder_that_cannot_be_saved_says_so(
            self, H, full, tmp_path, monkeypatch):
        """The model repeats the tool result, so it has to name the failure."""
        monkeypatch.setattr(H, "REMINDERS_FILE", tmp_path / "reminders.json")
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt._perm = {**H.DEFAULT_SETTINGS["permissions"], "reminders": True}
        out, err = belt.execute("set_reminder",
                                {"wake_name": "tea", "when_due": "in 5 minutes"})
        assert err and out.startswith("ERROR"), out
        assert "could not save" in out, out
        assert not (tmp_path / "reminders.json").exists(), \
            "a reminder half-reached the disk"

    def test_the_reminder_worker_alarms_once_when_the_store_fails(
            self, H, monkeypatch, tmp_path):
        """Silently never firing is the worst outcome for a reminder."""
        a = H.Assistant.__new__(H.Assistant)
        a._is_closed = lambda: False
        told = []
        monkeypatch.setattr(H, "notify", lambda text: told.append(text))

        class _Stop:
            """Let the real worker loop run N passes, without any real waiting."""

            def __init__(self, passes):
                self.passes, self.n = passes, 0

            def wait(self, timeout=None):
                return self.n >= self.passes

            def set(self):
                self.n += 1

        a._shutdown_event = _Stop(2)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] >= 2:
                a._shutdown_event.set()
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(H, "_reminder_store", lambda: SimpleNamespace(
            drain_due=boom))
        H.Assistant._reminder_worker(a)
        assert calls["n"] >= 2, "the worker never reached the failing store"
        assert len(told) == 1, f"expected one alarm, got {told}"
        assert "reminders" in told[0], told


# ------------------------------------------------------ the clock jumps back

class _NoLock:
    """Stand-in for the cross-process sidecar lock in a single-process test."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestClockJumpsBackwards:
    """NTP correction, a DST fix, a VM resume: the wall clock can go BACK.

    Session timing is monotonic on purpose and immune. Wall-clock stamps are
    not — and a backwards step must not resurrect "already handled" state or
    expire anything early, silently.
    """

    def test_a_rollback_does_not_extend_a_headlines_life(
            self, H, tmp_path, monkeypatch):
        """The seen-store stops expiring when the clock steps back.

        Entries are kept while they are newer than `now - TTL`. Read through a
        clock that has stepped BACK, `now - TTL` moves back with it, so entries
        that are genuinely past the TTL suddenly qualify again — the store
        silently never lets them go, and the bubble stops announcing those
        headlines for the duration of the rollback. The stamps themselves were
        written before the step, so they are the honest reference when they are
        ahead of the clock.

        No clock is patched: the stamps are written relative to a point two
        hours ahead of the current wall clock, which is exactly the state a
        backwards step leaves behind. The stale entry sits just past the TTL of
        the true reference and just inside the window of the stepped-back one.
        """
        monkeypatch.setattr(H, "WORLD_EVENTS_FILE", tmp_path / "seen.json")
        ttl = H.WORLD_EVENTS_TTL_S
        ref = time.time() + 7200     # the clock now reads 2h BEHIND these stamps
        fresh = H._world_norm_key("Announced an hour ago")
        stale = H._world_norm_key("Announced well past the TTL")
        H.WORLD_EVENTS_FILE.write_text(json.dumps(
            {fresh: ref - 3600, stale: ref - ttl - 5400}))
        assert H._world_seen(fresh), "a headline still in the window was dropped"
        assert not H._world_seen(stale), \
            "a backwards clock step kept a stale headline alive forever"

    def test_a_genuinely_stale_headline_still_expires(
            self, H, tmp_path, monkeypatch):
        """The rollback guard must not make the store immortal."""
        monkeypatch.setattr(H, "WORLD_EVENTS_FILE", tmp_path / "seen.json")
        key = H._world_norm_key("Old news")
        H.WORLD_EVENTS_FILE.write_text(
            json.dumps({key: time.time() - H.WORLD_EVENTS_TTL_S - 3600}))
        assert not H._world_seen(key)

    def test_a_fired_reminder_does_not_fire_twice_after_a_rollback(
            self, H, tmp_path):
        """Firing is recorded on disk, so a backwards step cannot re-arm it."""
        from core.assistant import ReminderStore

        now = 1_000_000.0
        clock = {"t": now}
        rfile = tmp_path / "reminders.json"
        rfile.write_text(json.dumps(
            [{"name": "tea", "due": now - 1.0, "repeat_hours": 24.0}]))
        store = ReminderStore(
            rfile, lock=threading.Lock(),
            file_lock=lambda *a, **k: _NoLock(),
            backup=lambda p: None,
            write=lambda p, text: Path(p).write_text(text),
            clock=lambda: clock["t"])
        fired = store.drain_due()
        assert [r["name"] for r in fired] == ["tea"]
        assert json.loads(rfile.read_text())[0]["due"] > now
        clock["t"] = now - 3600        # the clock jumps an hour BACKWARDS
        assert store.drain_due() == [], \
            "a backwards clock step re-fired an already-delivered reminder"

    def test_session_timing_ignores_the_wall_clock(
            self, H, monkeypatch):
        """The windows that keep a turn honest run on monotonic time.

        The transcript-reuse window, the snooze/kill offers and the follow-up
        window are all measured with `_tick_now()`. If any of them used the
        wall clock, a backwards step would resurrect a stale transcript or
        expire a live offer — silently.
        """
        real = H.time.time
        with monkeypatch.context() as m:
            m.setattr(H.time, "time", lambda: real() - 7200)
            first = H._tick_now()
            second = H._tick_now()
        assert second >= first > 0, "session timing followed the wall clock"
