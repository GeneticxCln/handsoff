"""core/control_server.py: the control socket, without the app.

The class used to be 500 lines inside the app module, reaching the app's globals
by bare name. It now takes them from a `dependencies` object, so the part of the
program that faces OTHER PROCESSES (capability token, peer-uid check, request
bounds, re-bind) can be exercised on its own — which is what these tests do:
nothing here loads `handsoff.py`, Qt or a microphone.

What the extraction must not have changed is pinned by tests/test_lifecycle.py
(`TestControlSocket`, the real-bubble launches) and the files that drive
`H.ControlServer`; this file pins what is NEW about the shape:

* it runs from a stand-in host (the module has no import-time tie to the app);
* every request reads the host's names LIVE (`H.CONTROL_SOCK`, `H.log`, the
  verb tables are reassigned by tests and embedders);
* two servers read their OWN dependencies (no process-wide "current host");
* the CLI-only reply names the HOST's file — `Path(__file__)` in the moved code
  would have named `control_server.py`.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import socket
import stat
import threading
import time
import types
from pathlib import Path

import pytest

from conftest import HERE as ROOT

from core import control_server, unix_address


class _Log:
    """The host's logger, recording what the server says and at which level."""

    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def _add(self, level, msg, *args, **_kwargs):
        self.lines.append((level, msg % args if args else msg))

    def info(self, msg, *args, **kw):
        self._add("info", msg, *args)

    def warning(self, msg, *args, **kw):
        self._add("warning", msg, *args)

    def error(self, msg, *args, **kw):
        self._add("error", msg, *args)

    def exception(self, msg, *args, **kw):
        self._add("exception", msg, *args)

    def has(self, level: str, needle: str) -> bool:
        return any(lv == level and needle in text for lv, text in self.lines)


def _host(tmp_path: Path, name: str = "a", *, model: str = "test-model",
          **overrides):
    """A stand-in for the app module: exactly the names the server reads."""
    sock = tmp_path / f"{name}.sock"

    def remove_stale() -> None:
        try:
            sock.unlink()
        except FileNotFoundError:
            pass

    host = types.SimpleNamespace(
        __file__=str(tmp_path / "handsoff.py"),
        log=logging.getLogger(f"test-control-server-{name}"),
        CONTROL_SOCK=sock,
        CONTROL_TOKEN=tmp_path / f"{name}.token",
        PTT_ACTIONS={"status", "toggle", "selftest", "level"},
        PTT_READ_ONLY=frozenset({"status", "level"}),
        PTT_CLI_ONLY=frozenset({"selftest"}),
        _CONTROL_READ_BUDGET=5.0,
        _CONTROL_REQUEST_MAX=65536,
        _CONTROL_TOKEN_PREFIX="token=",
        _peer_uid=lambda conn: os.getuid(),
        _remove_stale_control_socket=remove_stale,
        _prepare_runtime=lambda: True,
        _rotate_control_token=lambda: f"tok-{name}",
        _record_cap_refusal=lambda report: None,
        run_doctor=lambda: "doctor ok",
        OLLAMA_MODEL=model,
        SETTINGS_APP=tmp_path / "absent-settings.py",
    )
    vars(host).update(overrides)
    return host


class _Assistant:
    def __init__(self):
        self.state = "idle"
        self._handsfree = False
        self.commands: list[str] = []
        self.said: list[str] = []
        self.sigCommand = types.SimpleNamespace(emit=self.commands.append)

    def level_snapshot(self) -> dict:
        return {"raw": 0.0}

    def mic_health(self) -> dict:
        return {"mic": "ok", "note": "café"}

    def clear_history(self) -> int:
        return 3

    def say_preview(self, text: str) -> str:
        self.said.append(text)
        return f"ok: said {len(text)} chars"

    def set_pack_preview(self, folder: str) -> str:
        return f"ok: previewing {folder}"

    def clear_pack_preview(self) -> str:
        return "ok: preview cleared"

    def announce_cap_refusal(self, report, detail) -> None:
        pass


def _eventually(condition, timeout: float = 5.0) -> bool:
    """Poll `condition` — the server runs on its own thread, so a fact about its
    startup is true "soon", never "at the line after start()"."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


def _accepting(server, host) -> bool:
    """True once the server is LISTENING, not merely once its path exists: the file
    appears at bind(), before listen(), before the listener is recorded, and a
    client that connects in that window is refused."""
    if server._server is None or not host.CONTROL_SOCK.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with unix_address(host.CONTROL_SOCK) as address:
            probe.connect(address)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _ask(host, request: str, timeout: float = 5.0) -> str:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        with unix_address(host.CONTROL_SOCK) as address:
            client.connect(address)
        client.sendall(request.encode())
        client.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            part = client.recv(4096)
            if not part:
                break
            reply += part
        return reply.decode()
    finally:
        client.close()


@pytest.fixture
def running(tmp_path):
    servers = []

    def make(name: str = "a", **kwargs):
        host = _host(tmp_path, name, **kwargs)
        assistant = _Assistant()
        server = control_server.ControlServer(assistant, host)
        server.start()
        servers.append(server)
        assert _eventually(lambda: _accepting(server, host)), (
            "the control socket never started accepting")
        return server, host, assistant

    yield make
    for server in servers:
        server.stop()


class TestTheServerRunsWithoutTheApp:
    def test_a_read_only_verb_is_answered_from_the_hosts_names(self, running):
        server, host, _assistant = running()
        assert _ask(host, "status") == "state=idle handsfree=off model=test-model\n"

    def test_a_verb_that_changes_state_needs_the_token(self, running):
        server, host, assistant = running()
        refused = _ask(host, "toggle")
        assert refused.startswith("error: 'toggle' changes state and needs the control token")
        assert str(host.CONTROL_TOKEN) in refused
        assert assistant.commands == []
        accepted = _ask(host, "token=tok-a\ntoggle")
        assert accepted == "ok: toggle\n" and assistant.commands == ["toggle"]

    def test_an_unknown_verb_lists_the_commands_that_do_exist(self, running):
        server, host, _assistant = running()
        reply = _ask(host, "frobnicate")
        assert reply.startswith("error: unknown command 'frobnicate'"), reply
        assert "level status toggle" in reply and "selftest" not in reply, reply

    def test_stop_removes_the_socket_it_bound(self, running):
        server, host, _assistant = running()
        server.stop()
        assert not host.CONTROL_SOCK.exists()

    def test_the_module_imports_neither_the_app_nor_qt(self):
        tree = ast.parse((ROOT / "core" / "control_server.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"handsoff", "PySide6", "sounddevice", "numpy"}, imported


class TestWhatTheExtractionCouldHaveBroken:
    def test_the_cli_only_reply_names_the_hosts_file_not_this_module(self, running):
        """`Path(__file__).name` moved with the code would say `control_server.py`
        — a command that does not exist. The host's own file is what the user
        must run."""
        server, host, _assistant = running()
        reply = _ask(host, "token=tok-a\nselftest")
        assert "python3 handsoff.py --ptt selftest" in reply, reply
        assert "control_server" not in reply, reply

    def test_names_are_read_live_on_every_request(self, running):
        server, host, _assistant = running()
        assert "model=test-model" in _ask(host, "status")
        host.OLLAMA_MODEL = "swapped-model"          # `reload_derived_settings`
        assert "model=swapped-model" in _ask(host, "status")
        host.PTT_READ_ONLY = frozenset({"status"})   # `level` now changes state
        assert _ask(host, "level").startswith("error: 'level' changes state")

    def test_the_logger_is_the_hosts_and_is_read_live(self, running):
        server, host, _assistant = running()
        seen: list[str] = []
        host.log = types.SimpleNamespace(
            info=lambda *a, **k: None, error=lambda *a, **k: None,
            exception=lambda *a, **k: None,
            warning=lambda msg, *args: seen.append(msg % args))
        _ask(host, "toggle")                          # refused: no token
        assert any("refusing 'toggle'" in line for line in seen), seen

    def test_two_servers_read_their_own_dependencies(self, running):
        first, host_a, _ = running("a", model="model-a")
        second, host_b, _ = running("b", model="model-b")
        assert "model=model-a" in _ask(host_a, "status")
        assert "model=model-b" in _ask(host_b, "status")
        assert host_a.CONTROL_SOCK != host_b.CONTROL_SOCK
        assert _ask(host_b, "token=tok-a\ntoggle").startswith("error:"), (
            "one server accepted the OTHER server's token")

    def test_the_app_subclass_hands_over_the_apps_own_live_names(self, H):
        server = H.ControlServer(object())
        assert isinstance(server, control_server.ControlServer)
        assert server._d is H._tool_dependencies
        assert server._d.CONTROL_SOCK == H.CONTROL_SOCK
        other = types.SimpleNamespace()
        assert H.ControlServer(object(), other)._d is other

    def test_a_request_that_fails_costs_that_request_not_the_server(self, running):
        server, host, assistant = running()
        assistant.level_snapshot = lambda: 1 / 0
        reply = _ask(host, "level")
        assert reply.startswith("error: level failed (ZeroDivisionError)"), reply
        assert "state=idle" in _ask(host, "status")         # still serving

    def test_the_accept_loop_is_a_singleton_slot(self, running):
        server, host, _assistant = running()
        first = server._thread
        assert first is not None and first.is_alive()
        server.start()                                      # documented no-op
        assert server._thread is first


class TestWhatTheMutationGateFoundUnpinned:
    """The gate mutated the moved code and 14 of the first 40 mutants passed every
    test in the project: the socket's mode, the `listen`/idle-poll constants, what
    `stop()` does, the stale-path removal before a bind, the no-token path, the
    unavailable path, the repeated start, and the cap-refusal recorders. Each is a
    behaviour another process (or the next start) depends on, so each is pinned."""

    def test_the_socket_file_is_private(self, running):
        server, host, _assistant = running()
        assert stat.S_IMODE(host.CONTROL_SOCK.stat().st_mode) == 0o600

    def test_the_backlog_and_the_idle_poll_are_the_documented_ones(
            self, tmp_path, monkeypatch):
        calls: list[tuple[str, object]] = []
        real = socket.socket

        class Recording(real):
            def listen(self, backlog=None):
                calls.append(("listen", backlog))
                return super().listen(backlog)

            def settimeout(self, value):
                calls.append(("settimeout", value))
                return super().settimeout(value)

        monkeypatch.setattr(control_server.socket, "socket", Recording)
        host = _host(tmp_path)
        server = control_server.ControlServer(_Assistant(), host)
        server.start()
        assert _eventually(lambda: server._server is not None)
        try:
            assert ("listen", 4) in calls, calls
            # one lstat per idle SECOND is what notices a removed path
            assert ("settimeout", 1.0) in calls, calls
        finally:
            server.stop()

    def test_stop_ends_the_loop_quietly_and_closes_the_listener(self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        listener = server._server
        assert listener is not None and listener.fileno() >= 0
        thread = server._thread
        server.stop()
        assert listener.fileno() == -1, "stop() left the listening socket open"
        assert not thread.is_alive()
        assert not log.has("exception", "accept failed"), (
            "stop() did not mark the shutdown BEFORE closing the listener, so the "
            f"accept loop reported its own shutdown as a failure: {log.lines}")

    def test_stop_removes_the_path_and_survives_a_removal_that_raises(self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        seen: list[str] = []

        def failing_remove():
            seen.append("called")
            raise OSError("cannot remove")

        host._remove_stale_control_socket = failing_remove
        server.stop()                                   # must not raise
        assert seen, "stop() never asked the host to remove the path"
        assert log.has("exception", "could not remove control socket during shutdown")

    def test_a_stale_socket_is_removed_before_the_bind(self, tmp_path):
        host = _host(tmp_path)
        relic = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with unix_address(host.CONTROL_SOCK) as address:
            relic.bind(address)
        relic.close()                                  # a SIGKILL relic: file, no owner
        assert host.CONTROL_SOCK.exists()
        server = control_server.ControlServer(_Assistant(), host)
        server.start()
        try:
            deadline = time.time() + 5
            reply = ""
            while time.time() < deadline:
                try:
                    reply = _ask(host, "status")
                    break
                except OSError:
                    time.sleep(0.05)
            assert reply.startswith("state=idle"), (
                "the bind ran over a stale path it had not removed")
        finally:
            server.stop()

    def test_no_token_means_read_only_verbs_only_and_the_journal_says_so(self, running):
        log = _Log()
        server, host, assistant = running(log=log, _rotate_control_token=lambda: None)
        assert log.has("error", "no capability token could be written")
        assert log.has("error", "(level status)")           # names what still works
        assert _ask(host, "status").startswith("state=idle")
        assert _ask(host, "token=anything\ntoggle").startswith("error: 'toggle' changes state")
        assert assistant.commands == []

    def test_a_normal_start_reports_no_error(self, running):
        log = _Log()
        running(log=log)
        assert _eventually(lambda: log.has("info", "control socket at")), log.lines
        assert not [line for line in log.lines if line[0] in ("error", "exception")], (
            log.lines)

    def test_an_unavailable_socket_is_logged_and_the_server_ends(self, tmp_path):
        log = _Log()
        host = _host(tmp_path, log=log, _prepare_runtime=lambda: False)
        server = control_server.ControlServer(_Assistant(), host)
        server.start()
        deadline = time.time() + 5
        while server._thread is not None and server._thread.is_alive() \
                and time.time() < deadline:
            time.sleep(0.02)
        assert log.has("error", "control socket unavailable: runtime/config "
                                "directories or files are not private"), log.lines
        assert not host.CONTROL_SOCK.exists()

    def test_a_repeated_start_says_it_is_already_accepting(self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        server.start()
        assert log.has("info", "control socket already accepting")

    def test_a_second_diagnostic_is_refused_by_name_and_recorded_and_announced(
            self, running):
        recorded: list[dict] = []
        announced: list[tuple] = []
        server, host, assistant = running(
            log=_Log(), _record_cap_refusal=recorded.append)
        assistant.announce_cap_refusal = lambda report, detail: announced.append(
            (report, detail))
        release = threading.Event()
        try:
            with pytest.raises(TimeoutError, match="timed out"):
                server._diagnostic_call(lambda: release.wait(10), 0.05)
            with pytest.raises(TimeoutError, match="previous diagnostic is still running"):
                server._diagnostic_call(lambda: 1, 1.0)
        finally:
            release.set()
        assert recorded and recorded[0]["detail"] == "previous diagnostic still running"
        assert announced and announced[0][1] == "previous diagnostic still running"
        assert host.log.has("warning", "cap refusal")

    def test_a_failing_recorder_or_announcer_does_not_hide_the_refusal(self, running):
        log = _Log()

        def broken_recorder(report):
            raise RuntimeError("disk full")

        server, host, assistant = running(log=log, _record_cap_refusal=broken_recorder)

        def broken_announcer(report, detail):
            raise RuntimeError("no speaker")

        assistant.announce_cap_refusal = broken_announcer
        release = threading.Event()
        try:
            with pytest.raises(TimeoutError):
                server._diagnostic_call(lambda: release.wait(10), 0.05)
            with pytest.raises(TimeoutError, match="previous diagnostic is still running"):
                server._diagnostic_call(lambda: 1, 1.0)
        finally:
            release.set()
        assert log.has("exception", "cap-refusal recorder failed"), log.lines
        assert log.has("exception", "cap-refusal announcement failed"), log.lines


# Every verb the real socket carries. The default stand-in host knows four; the
# tests below need the rest, so they pass this as overrides.
_ALL_VERBS = dict(
    PTT_ACTIONS={"status", "toggle", "selftest", "level", "health", "doctor",
                 "clear-history", "say", "preview-pack", "preview-clear", "settings"},
    PTT_READ_ONLY=frozenset({"status", "level", "health", "doctor"}),
    PTT_CLI_ONLY=frozenset({"selftest"}),
)


class _Closer:
    """A listener stand-in that only counts how often it is closed."""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class TestEveryVerbTheSocketCarries:
    """Mutation gate, second pass: the verb arms had no test that ran them from
    this module. Each arm answers another process, so each reply is pinned."""

    def test_health_answers_the_snapshot_as_json_and_names_its_timeout(self, running):
        server, host, _assistant = running(**_ALL_VERBS)
        timeouts: list[float] = []
        real = server._diagnostic_call
        server._diagnostic_call = lambda fn, t: (timeouts.append(t), real(fn, t))[1]
        reply = _ask(host, "health")
        assert json.loads(reply) == {"mic": "ok", "note": "café"}
        assert "café" in reply, "non-ASCII must not be escaped"
        assert timeouts == [4.0]

    def test_a_failing_health_snapshot_is_named_in_the_reply_and_the_journal(self, running):
        log = _Log()
        server, host, assistant = running(log=log, **_ALL_VERBS)

        def boom():
            raise RuntimeError("backend wedged")

        assistant.mic_health = boom
        assert _ask(host, "health") == "error: health snapshot failed: backend wedged\n"
        assert log.has("exception", "health snapshot failed")

    def test_doctor_answers_the_hosts_report_and_names_its_timeout(self, running):
        server, host, _assistant = running(**_ALL_VERBS)
        timeouts: list[float] = []
        real = server._diagnostic_call
        server._diagnostic_call = lambda fn, t: (timeouts.append(t), real(fn, t))[1]
        assert _ask(host, "doctor") == "doctor ok\n"
        assert timeouts == [4.5]

    def test_a_failing_doctor_is_named_in_the_reply_and_the_journal(self, running):
        log = _Log()

        def boom():
            raise RuntimeError("no nvidia-smi")

        server, host, _assistant = running(log=log, run_doctor=boom, **_ALL_VERBS)
        assert _ask(host, "doctor") == "error: doctor report failed: no nvidia-smi\n"
        assert log.has("exception", "doctor report failed")

    def test_level_and_clear_history_answer_from_the_assistant(self, running):
        server, host, _assistant = running(**_ALL_VERBS)
        assert json.loads(_ask(host, "level")) == {"raw": 0.0}
        assert _ask(host, "token=tok-a\nclear-history") == "ok: cleared 3 history message(s)\n"

    def test_say_keeps_the_text_exactly_as_typed(self, running):
        server, host, assistant = running(**_ALL_VERBS)
        reply = _ask(host, "token=tok-a\nSAY Hello World")
        assert reply == "ok: said 11 chars\n"
        assert assistant.said == ["Hello World"], "the verb is lowercased, the TEXT is not"

    def test_the_pack_preview_verbs_pass_the_folder_through(self, running):
        server, host, _assistant = running(**_ALL_VERBS)
        assert _ask(host, "token=tok-a\npreview-pack /some/folder") == (
            "ok: previewing /some/folder\n")
        assert _ask(host, "token=tok-a\npreview-clear") == "ok: preview cleared\n"

    def test_settings_launches_the_settings_app_detached_when_it_exists(
            self, running, monkeypatch, tmp_path):
        app = tmp_path / "settings-app.py"
        app.write_text("")
        launched: list[tuple] = []
        monkeypatch.setattr(control_server.subprocess, "Popen",
                            lambda argv, **kw: launched.append((argv, kw)))
        server, host, _assistant = running(SETTINGS_APP=app, **_ALL_VERBS)
        assert _ask(host, "token=tok-a\nsettings") == "ok: settings window launched\n"
        argv, kwargs = launched[0]
        assert argv[1:] == [str(app)]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] is control_server.subprocess.DEVNULL
        assert kwargs["stderr"] is control_server.subprocess.DEVNULL

    def test_settings_says_where_it_looked_when_the_app_is_missing(self, running, tmp_path):
        missing = tmp_path / "no-such-settings.py"
        server, host, _assistant = running(SETTINGS_APP=missing, **_ALL_VERBS)
        assert _ask(host, "token=tok-a\nsettings") == (
            f"ERROR: settings app missing at {missing}\n")

    def test_a_failing_verb_is_journalled_by_name_and_the_reply_says_which(self, running):
        log = _Log()
        server, host, assistant = running(log=log, **_ALL_VERBS)
        assistant.level_snapshot = lambda: 1 / 0
        assert _ask(host, "level") == "error: level failed (ZeroDivisionError) — see the journal\n"
        assert log.has("exception", "control socket: 'level' failed")
        assert _ask(host, "status").startswith("state=idle")     # still serving


class TestTheDiagnosticWorker:
    def test_a_slow_diagnostic_is_waited_for_within_its_timeout(self, running):
        """Without the join, a diagnostic that has not finished the instant the
        worker starts reads as timed out; a FAST one hides that, which is how the
        mutation gate found it unpinned."""
        server, host, _assistant = running()
        result = server._diagnostic_call(lambda: (time.sleep(0.3), "slow ok")[1], 3.0)
        assert result == "slow ok"

    def test_a_diagnostic_that_outlives_its_timeout_is_reported_by_name(self, running):
        server, host, _assistant = running()
        release = threading.Event()
        try:
            with pytest.raises(TimeoutError, match=r"timed out after 0\.1s"):
                server._diagnostic_call(lambda: release.wait(10), 0.1)
        finally:
            release.set()

    def test_a_diagnostic_that_raises_re_raises_the_original_error(self, running):
        server, host, _assistant = running()

        def boom():
            raise KeyError("the original")

        with pytest.raises(KeyError, match="the original"):
            server._diagnostic_call(boom, 3.0)


class TestStartAndRestart:
    def test_a_stopped_server_can_be_started_again(self, running):
        server, host, _assistant = running()
        server.stop()
        assert not host.CONTROL_SOCK.exists()
        server.start()                       # the stop flag must not outlive the stop
        assert _eventually(lambda: _accepting(server, host))
        assert _ask(host, "status").startswith("state=idle")

    def test_a_refused_start_does_not_re_arm_a_server_that_is_shutting_down(self, running):
        server, host, _assistant = running()
        server._stop.set()                   # a shutdown has begun; the loop is still alive
        server.start()                       # refused: the slot is occupied
        assert server._stop.is_set(), "a refused start cleared the shutdown flag"


class TestTheAcceptSlotOnStop:
    def test_stop_frees_the_slot_once_the_loop_is_gone(self, running):
        server, host, _assistant = running()
        server.stop()
        assert server._thread is None, "the slot still names a dead accept loop"

    def test_a_loop_that_outlives_the_join_budget_keeps_the_slot(self, tmp_path):
        """A restart must not get a second acceptor beside one that is still
        running: the slot is freed only once its occupant is really dead."""
        host = _host(tmp_path)
        server = control_server.ControlServer(_Assistant(), host)
        release = threading.Event()
        stuck = threading.Thread(target=release.wait, args=(30,), daemon=True)
        stuck.start()
        slot = server._runs.reserve(server.ACCEPT_SLOT, reclaim=lambda t: not t.is_alive())
        with slot:
            slot.commit(stuck)
        try:
            server.stop()                       # waits out its 2 s join budget
            assert stuck.is_alive()
            assert server._thread is stuck, "the slot was freed under a live loop"
        finally:
            release.set()


class TestHowARequestIsRead:
    def test_the_request_is_cut_at_the_size_ceiling(self, running):
        server, host, assistant = running(_CONTROL_REQUEST_MAX=64, **_ALL_VERBS)
        request = "token=tok-a\nsay " + "x" * 200
        try:
            _ask(host, request)
        except OSError:
            # past the ceiling the server stops reading and closes with the
            # client's bytes unread, which the client sees as a reset (the
            # class's own comment on why a request is read before it is judged)
            pass
        assert _eventually(lambda: assistant.said == ["x" * 48]), assistant.said
        # 64 bytes in all, 16 of them the `token=…\nsay ` header

    def test_a_request_that_dribbles_past_the_read_budget_is_cut_and_logged(self, running):
        log = _Log()
        server, host, _assistant = running(log=log, _CONTROL_READ_BUDGET=0.3, **_ALL_VERBS)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        try:
            with unix_address(host.CONTROL_SOCK) as address:
                client.connect(address)
            for byte in b"sta":                     # 0.75 s, a budget of 0.3 s
                client.sendall(bytes([byte]))
                time.sleep(0.25)
            reply = client.recv(4096).decode()
        finally:
            client.close()
        assert reply.startswith("error: unknown command 'sta'"), reply
        assert log.has("warning", "did not finish within 0.3s"), log.lines

    def test_a_connection_gets_a_five_second_read_timeout(self, tmp_path, monkeypatch):
        timeouts: list[float] = []
        real = socket.socket

        class Recording(real):
            def settimeout(self, value):
                timeouts.append(value)
                return super().settimeout(value)

        host = _host(tmp_path)
        server = control_server.ControlServer(_Assistant(), host)
        monkeypatch.setattr(control_server.socket, "socket", Recording)
        server.start()
        try:
            assert _eventually(lambda: _accepting(server, host))
            _ask(host, "status", timeout=4.5)          # the client's own timeout differs
            assert _eventually(lambda: 5.0 in timeouts), timeouts
        finally:
            server.stop()

    def test_a_peer_that_is_not_us_is_refused_and_logged(self, running):
        log = _Log()
        server, host, assistant = running(log=log, _peer_uid=lambda conn: os.getuid() + 1)
        assert _ask(host, "status") == "error: not permitted\n"
        assert log.has("warning", f"refusing peer uid {os.getuid() + 1}"), log.lines
        assert assistant.commands == []

    def test_a_platform_that_cannot_report_the_peer_is_not_refused(self, running):
        server, host, _assistant = running(_peer_uid=lambda conn: None)
        assert _ask(host, "status").startswith("state=idle")


class TestTheAcceptLoopWatchesItsPath:
    """One lstat per idle second: the path can be removed or replaced under a live
    bubble, and the loop has to notice, repair only what it bound, and never
    unlink an inode that is not its own."""

    def test_the_loop_ends_on_the_next_idle_second_when_asked_to_stop(self, running):
        server, host, _assistant = running()
        thread = server._thread
        server._stop.set()                # without closing the listener
        assert _eventually(lambda: not thread.is_alive(), timeout=4.0)
        assert not host.CONTROL_SOCK.exists(), "the loop's own cleanup did not remove the path"

    def test_an_idle_loop_keeps_serving(self, running):
        server, host, _assistant = running()
        time.sleep(2.3)                   # two idle timeouts
        assert _ask(host, "status").startswith("state=idle")

    def test_a_removed_path_is_rebound_and_served_again(self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        old = server._server
        host.CONTROL_SOCK.unlink()

        def served():
            try:
                return _ask(host, "status", timeout=1.0).startswith("state=idle")
            except OSError:
                return False

        assert _eventually(served, timeout=6.0), "the removed path was never re-bound"
        assert log.has("warning", "was removed while running — re-bound"), log.lines
        assert stat.S_IMODE(host.CONTROL_SOCK.stat().st_mode) == 0o600
        assert server._server is not old and old.fileno() == -1
        assert server._orphan_reported is False

    def test_a_path_that_names_someone_elses_socket_is_reported_once_and_left_alone(
            self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        host.CONTROL_SOCK.unlink()
        foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with unix_address(host.CONTROL_SOCK) as address:
            foreign.bind(address)
        foreign.listen(1)
        try:
            ident = (host.CONTROL_SOCK.lstat().st_dev, host.CONTROL_SOCK.lstat().st_ino)
            assert _eventually(lambda: log.has("error", "now belongs to another inode"),
                               timeout=5.0), log.lines
            time.sleep(2.3)               # two more idle seconds: the latch holds
            said = [t for lv, t in log.lines if "now belongs to another inode" in t]
            assert len(said) == 1, said
            assert (host.CONTROL_SOCK.lstat().st_dev,
                    host.CONTROL_SOCK.lstat().st_ino) == ident, "the foreign inode was replaced"
        finally:
            foreign.close()

    def test_a_path_this_server_never_bound_is_not_repaired(self, running, tmp_path):
        server, host, _assistant = running()
        other = tmp_path / "somebody-elses.sock"
        host.CONTROL_SOCK = other          # an embedder or a test reassigns the global
        time.sleep(2.3)
        assert not other.exists(), "the server created a socket at a path it never bound"

    def test_an_accept_that_fails_outside_a_shutdown_is_journalled(self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        thread = server._thread
        server._server.close()             # the listener dies under the loop
        assert _eventually(lambda: not thread.is_alive(), timeout=4.0)
        assert log.has("exception", "control socket accept failed"), log.lines

    def test_a_socket_that_cannot_be_bound_is_closed_again(self, tmp_path, monkeypatch):
        closed: list[int] = []
        real = socket.socket

        class Recording(real):
            def close(self):
                closed.append(1)
                return super().close()

        monkeypatch.setattr(control_server.socket, "socket", Recording)
        host = _host(tmp_path, log=_Log(), CONTROL_SOCK=tmp_path / "no-such-dir" / "c.sock")
        server = control_server.ControlServer(_Assistant(), host)
        server.start()
        thread = server._thread
        assert _eventually(lambda: not thread.is_alive(), timeout=4.0)
        assert closed, "the half-made listener was leaked"
        assert host.log.has("error", "control socket unavailable"), host.log.lines


class TestWhatTheLoopLeavesBehind:
    def test_after_the_loop_ends_nothing_is_left_open_or_registered(self, running):
        log = _Log()
        server, host, _assistant = running(log=log)
        listener, thread = server._server, server._thread
        removed: list[int] = []

        def remove_and_fail():
            removed.append(1)
            raise OSError("cannot remove")

        host._remove_stale_control_socket = remove_and_fail
        server._stop.set()
        assert _eventually(lambda: not thread.is_alive(), timeout=4.0)
        assert server._server is None
        assert listener.fileno() == -1
        assert removed, "the loop's own cleanup never asked the host to remove the path"
        assert log.has("exception", "could not remove control socket"), log.lines

    def test_stop_closes_the_listener_it_holds(self, running):
        server, host, _assistant = running()
        real = server._server
        closer = _Closer()
        server._server = closer
        server.stop()
        assert closer.closed == 1, "stop() did not close the listener"
        assert real.fileno() == -1        # the loop's own cleanup closed the real one


class TestThePathStateAndTheRebind:
    def test_the_identity_of_a_path_is_its_device_and_inode(self, tmp_path):
        target = tmp_path / "f"
        target.write_text("x")
        info = target.lstat()
        assert control_server.ControlServer._ident_of(target) == (info.st_dev, info.st_ino)
        assert control_server.ControlServer._ident_of(tmp_path / "absent") is None

    def test_the_state_of_the_path_is_ours_gone_or_foreign(self, running, tmp_path):
        server, host, _assistant = running()
        assert server._socket_path_state() == "ours"
        server._sock_ident = (0, 0)                        # what we bound is not what is there
        assert server._socket_path_state() == "foreign"
        server._sock_ident = None                          # nothing recorded: cannot say
        assert server._socket_path_state() == "ours"
        host.CONTROL_SOCK = tmp_path / "gone.sock"
        assert server._socket_path_state() == "gone"

    def test_a_path_that_cannot_be_inspected_is_treated_as_ours(self, running):
        server, host, _assistant = running()

        class Unreadable:
            def lstat(self):
                raise PermissionError("no")

        host.CONTROL_SOCK = Unreadable()
        assert server._socket_path_state() == "ours"

    def _idle_server(self, tmp_path, **overrides):
        host = _host(tmp_path, log=_Log(), **overrides)
        server = control_server.ControlServer(_Assistant(), host)
        server._bound_path = str(host.CONTROL_SOCK)
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        return server, host, old

    def test_a_rebind_makes_a_private_listening_socket_and_retires_the_old_one(
            self, tmp_path):
        server, host, old = self._idle_server(tmp_path)
        fresh = server._rebind(old)
        assert fresh is not None and server._server is fresh
        assert old.fileno() == -1
        assert stat.S_IMODE(host.CONTROL_SOCK.stat().st_mode) == 0o600
        assert fresh.gettimeout() == 1.0
        assert server._sock_ident == (host.CONTROL_SOCK.lstat().st_dev,
                                      host.CONTROL_SOCK.lstat().st_ino)
        assert host.log.has("warning", "re-bound at")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with unix_address(host.CONTROL_SOCK) as address:
                client.connect(address)          # it LISTENS
        finally:
            client.close()
            fresh.close()

    def test_a_rebind_clears_a_stale_relic_first(self, tmp_path):
        server, host, old = self._idle_server(tmp_path)
        relic = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with unix_address(host.CONTROL_SOCK) as address:
            relic.bind(address)
        relic.close()
        fresh = server._rebind(old)
        assert fresh is not None, "the stale relic blocked the re-bind"
        fresh.close()

    def test_a_rebind_that_fails_says_so_once_and_keeps_the_old_listener(self, tmp_path):
        def refuse():
            raise OSError("foreign owner")

        server, host, old = self._idle_server(tmp_path, _remove_stale_control_socket=refuse)
        assert server._rebind(old) is None
        assert server._rebind(old) is None
        said = [t for lv, t in host.log.lines if "could not be re-bound" in t]
        assert len(said) == 1 and "foreign owner" in said[0], host.log.lines
        assert server._orphan_reported is True and old.fileno() >= 0
        old.close()

    def test_a_rebind_is_refused_during_shutdown_and_for_a_path_never_bound(self, tmp_path):
        server, host, old = self._idle_server(tmp_path)
        server._stop.set()
        assert server._rebind(old) is None and not host.CONTROL_SOCK.exists()
        server._stop.clear()
        server._bound_path = None
        assert server._rebind(old) is None and not host.CONTROL_SOCK.exists()
        server._bound_path = str(tmp_path / "some-other.sock")
        assert server._rebind(old) is None and not host.CONTROL_SOCK.exists()
        old.close()
