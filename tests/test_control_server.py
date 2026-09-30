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
        self.sigCommand = types.SimpleNamespace(emit=self.commands.append)

    def level_snapshot(self) -> dict:
        return {"raw": 0.0}

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
