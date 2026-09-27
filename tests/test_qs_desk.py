"""The Quantum Space desk client: the contract, the wire, and the four states.

The fake bridge is a REAL HTTP server on an ephemeral port, not a recorded
double, because half of what this client has to get right is the WIRE itself —
a POST to "/", a bearer header, no Origin, and the HTTP status checked before
the body is worth parsing. A mock would let all of that pass by construction.

Nothing here needs Quantum Space installed, a port open, or a discovery file in
the developer's own `~/.config`: every test builds its own profile directory
under `tmp_path` and points `XDG_CONFIG_HOME` at it, which is also what proves
the module reads the environment rather than anything about this machine.

The four failure states a user actually hears are pinned here by SENTENCE, not
by state name, because those sentences are the feature: the desk's own words for
a refusal, and four distinct answers that do not collapse into one "couldn't
reach Quantum Space".
"""
from __future__ import annotations

import ast
import http.server
import json
import os
import subprocess
import sys
import threading

import pytest
from conftest import HERE as ROOT
from conftest import sandbox_env

from core import qs_desk as _qs

TOKEN = "9f" * 32                      # shaped like the desk's 64 hex chars
OTHER_TOKEN = "1a" * 32

# The desk's two `not-granted` sentences, in its own words: ONE wire reason for
# two situations the user experiences differently, which is why the message is
# what has to survive the trip.
DESK_NOT_ALLOWED = ("handsoff is not allowed to read this desk yet. Open Quant "
                    "Space → Settings → Control and allow it.")
DESK_CONTROL_OFF = ("Quantum Space's desk control is switched off. Open Quant "
                    "Space → Settings → Control to turn it on.")

#: The desk's own session rows, in the shapes a LIVE desk answered on
#: 2026-09-21 (a real Claude tile in a real run of the app): `kind` is the
#: tile's own kind (`claude`, not a generic "agent"), `agent` is lowercase and
#: is JSON null for a plain shell, ids are the window-prefixed panel ids the app
#: mints (`w1_p_1`), and every row carries `readable`. The ids here stay short
#: on purpose — they are opaque to this client, and one test below hands back a
#: real `w1_p_1` to prove that.
SESSIONS = [
    {"id": "s1", "name": "claude", "kind": "claude", "agent": "claude",
     "cwd": "/home/u/api", "startedAt": 1789146441007, "readable": True},
    # A plain shell tile: no agent, and no name of its own — which is what makes
    # two of them read as "two shells" rather than as two one-off labels.
    {"id": "s2", "name": "", "kind": "shell", "agent": None,
     "cwd": "/home/u", "startedAt": 1789146441007, "readable": True},
]

#: What a real read's tail looks like after the desk's own ANSI stripping: a TUI
#: that positions with escapes comes back as prose with the spaces eaten, boxes
#: and repeated repaints and all. Relay it, do not tidy it.
MANGLED_TAIL = ("Quicksafetycheck:Isthis\n"
                "a projectyoucreatedor\n"
                "❯No,exit\n"
                "────────────────────────")


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, reason, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32000, "message": message,
                      "data": {"reason": reason}}}


class FakeDesk:
    """The desk's own behaviour, scripted. The socket around it is real.

    Every reason string and every sentence here is the contract's own wording —
    a fake that invented friendlier refusals would test the wrong thing, because
    what the tools must NOT do is rewrite the desk's sentence.
    """

    def __init__(self):
        self.granted = True
        self.refusal = DESK_NOT_ALLOWED
        self.sessions = [dict(s) for s in SESSIONS]
        self.text = "running the tests\n12 passed"
        self.truncated = False
        self.version = "0.5.1"
        self.folder = "/home/u/api"
        self.windows = 2
        self.http_status = None        # force a bare HTTP status, no JSON-RPC
        self.raw_body = None           # force a non-JSON body with status 200
        self.raw_status = None
        self.seen = []                 # [(method, params)] in order

    def status_result(self):
        """`desk.status` as a real desk answers it.

        `control` is the desk's OWN object — ``{enabled, clients}`` — not a
        bare bool; a bool was this fake's simplification, and it hid the fact
        that the real shape made `describe_status` fall silent.
        """
        return {"app": "quant-space", "protocol": 1,
                "version": self.version, "folder": self.folder,
                "sessions": len(self.sessions),
                "control": {"enabled": True, "clients": ["handsoff"]},
                "windows": self.windows}

    def answer(self, body):
        rid = (body or {}).get("id")
        method = (body or {}).get("method") or ""
        params = (body or {}).get("params") or {}
        self.seen.append((method, params))
        if self.http_status is not None:
            # Empty, like the real bridge: 401/403/405/400 carry no body at all.
            return self.http_status, None
        if not self.granted:
            return 200, _error(rid, "not-granted", self.refusal)
        if method == "ping":
            return 200, _result(rid, {"app": "quant-space", "protocol": 1,
                                      "version": self.version, "at": 1})
        if method == "hello":
            # A real `hello` answers with the whole desk status under `desk`,
            # not with a word for "live".
            return 200, _result(rid, {"granted": True, "client": params.get("client"),
                                      "desk": self.status_result(),
                                      "methods": ["ping", "hello", "desk.status",
                                                  "desk.sessions", "session.read"]})
        if method == "desk.status":
            return 200, _result(rid, self.status_result())
        if method == "desk.sessions":
            return 200, _result(rid, {"sessions": self.sessions})
        if method == "session.read":
            sid = params.get("id")
            match = [s for s in self.sessions if s.get("id") == sid]
            if not match:
                return 200, _error(rid, "session-not-found",
                                   "No session on this desk has that id.")
            if not self.text:
                return 200, _error(rid, "no-output",
                                   "That session has no readable output in "
                                   "Quant Space.")
            rows = self.text.splitlines()
            asked = params.get("lines")
            asked = max(1, min(2000, int(asked))) if isinstance(asked, int) else 200
            # A real read answers with `lines` — how many it actually returned
            # — alongside the text and the flag.
            return 200, _result(rid, {"id": sid, "name": match[0].get("name"),
                                      "kind": match[0].get("kind"),
                                      "agent": match[0].get("agent"),
                                      "text": self.text,
                                      "lines": min(asked, len(rows)),
                                      "truncated": (self.truncated
                                                    or len(rows) > asked)})
        return 200, _error(rid, "unknown-method",
                           f"Quant Space does not offer \u201c{method}\u201d.")


def _send(handler, status, payload):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Bridge:
    """A real HTTP server on 127.0.0.1:0 that records what it was sent."""

    def __init__(self, desk=None):
        self.desk = desk or FakeDesk()
        self.calls = []
        self.lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):     # keep the suite's output clean
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = None
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = None
                with outer.lock:
                    outer.calls.append(
                        {"verb": self.command, "path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": body, "raw": raw})
                if outer.desk.raw_body is not None:
                    _send(self, outer.desk.raw_status or 200, outer.desk.raw_body)
                    return
                status, doc = outer.desk.answer(body)
                payload = b"" if doc is None else json.dumps(doc).encode("utf-8")
                _send(self, status, payload)

            def do_GET(self):
                with outer.lock:
                    outer.calls.append({"verb": self.command, "path": self.path,
                                        "headers": {}, "body": None, "raw": b""})
                # The real bridge answers a non-POST with 405 and NOTHING else,
                # so the client may not read a sentence out of the body.
                _send(self, 405, b"")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def sent(self, index=-1):
        with self.lock:
            return dict(self.calls[index])

    @property
    def count(self):
        with self.lock:
            return len(self.calls)


@pytest.fixture
def bridge():
    running = Bridge()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A throw-away `$XDG_CONFIG_HOME`, so nothing reads the developer's."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _control(root, *, port, token=TOKEN, pid=None, protocol=1, mode=0o600,
             name="Quantum Space", body=None):
    """Write a discovery file the way the app does: private, 0600, atomic name."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "control.json"
    if body is None:
        body = json.dumps({"app": "quant-space", "protocol": protocol,
                           "version": "0.5.1",
                           "pid": os.getpid() if pid is None else pid,
                           "port": port, "token": token,
                           "folder": "/home/u/api",
                           "startedAt": 1789146441007})
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _a_dead_pid():
    """A pid that really is gone: a reaped child's, checked with kill(0).

    A REAL one rather than a monkeypatched `os.kill`, because the property under
    test is "a file whose app has died" - the child is started through
    `sandbox_env()` like every other interpreter the suite spawns (a child with
    the developer's HOME is the leak that rule exists to stop), and reaped so
    the kernel is holding nothing for it.
    """
    for _ in range(8):
        proc = subprocess.Popen([sys.executable, "-c", "pass"],
                                env=sandbox_env())
        proc.wait()
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            return proc.pid
    raise AssertionError("no reaped child's pid stayed free — cannot test stale")


def case_gate_names():
    """{tool name: effective gate} for the four `quant_space_*` tools.

    Read off the class the host publishes, so a tool that quietly fell back to
    its own name as its gate (`gates=None` means exactly that) shows up here as
    four dropdowns the user has to reason about instead of one.
    """
    from core import tools as _core_tools
    out = {}
    for name in dir(_core_tools.ToolBelt):
        fn = getattr(_core_tools.ToolBelt, name)
        if getattr(fn, "_is_tool", False) and fn._tool_name.startswith("quant_space_"):
            out[fn._tool_name] = fn._tool_gates
    return out


class TestTheDiscoveryFileIsNotOursToTrust:
    """The file is a CLAIM by whatever wrote it. Three ways it gets refused."""

    def test_no_file_anywhere_is_not_running(self, profile):
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "not-running"
        assert caught.value.message == "Quantum Space isn't running."

    def test_a_stale_pid_is_not_running_not_connection_refused(
            self, profile, bridge):
        """The app can die without cleaning up. That is a different sentence."""
        _control(profile, port=bridge.port, pid=_a_dead_pid())
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "not-running", caught.value.state
        assert "isn't running" in caught.value.message
        assert "refused" not in caught.value.message.lower()
        assert bridge.count == 0, "a stale file must not become a connection"

    def test_a_world_readable_file_is_refused_before_anything_is_sent(
            self, profile, bridge):
        _control(profile, port=bridge.port, mode=0o644)
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "untrusted"
        assert "0600" in caught.value.message and "0644" in caught.value.message
        assert bridge.count == 0, "an untrusted file must never be dialled"

    def test_a_wrong_protocol_is_a_mismatch_not_a_best_effort_attempt(
            self, profile, bridge):
        _control(profile, port=bridge.port, protocol=2)
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "protocol"
        assert "2" in caught.value.message and "1" in caught.value.message
        assert "mismatch" in caught.value.message
        assert bridge.count == 0

    def test_a_file_that_is_not_json_is_refused(self, profile, bridge):
        _control(profile, port=bridge.port, body="{not json at all")
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "untrusted"
        assert "JSON" in caught.value.message
        assert bridge.count == 0

    def test_a_file_that_is_not_text_is_refused_and_never_dialled(
            self, profile, bridge):
        """Not text is not a broken JSON file; it is a different file.

        `read_text` refuses it with UnicodeDecodeError — a ValueError, not an
        OSError — so before the `except UnicodeDecodeError` arm in `_load`
        this walked straight past all ten states and out of the client: none
        of the four tools' six `except DeskError` handlers saw it, and what
        the user got was the tool loop's own guard saying the ASSISTANT's
        check had failed before the tool ran (measured 2026-09-27, a 0600
        file whose ninth byte is 0xff).
        """
        path = _control(profile, port=bridge.port)
        path.write_bytes(b'{"app": "\xff\xfe not text", "protocol": 1, '
                         b'"port": 48213, "token": "9f9f"}')
        os.chmod(path, 0o600)
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "untrusted", caught.value.state
        assert "not UTF-8 text" in caught.value.message, caught.value.message
        assert "JSON" not in caught.value.message, (
            "a file that is not text must not be reported as bad JSON — it is "
            "not a JSON file at all")
        assert bridge.count == 0, "an untrusted file must never be dialled"

    def test_a_live_foreign_pid_means_another_app_owns_that_file(
            self, profile, bridge, monkeypatch):
        """`kill(0)` succeeding is ours; PermissionError is someone else's pid."""
        _control(profile, port=bridge.port, pid=4242)

        def foreign(pid, sig):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(_qs.os, "kill", foreign)
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "untrusted"
        assert "not ours" in caught.value.message
        assert bridge.count == 0, "never connect to a process that did not write it"

    def test_the_dev_profile_is_tried_too(self, profile, bridge):
        """`npm run dev` is a different profile, and often the only one.

        The app's own dev directory is `<productName>-dev`, so with the product
        named "Quant Space" that is `Quant Space-dev` — the FIRST live run of
        this client asked a granted, listening desk whether it was running and
        was told no, because the frozen handoff had spelled it "Quantum".
        """
        _control(profile, port=bridge.port, name="Quant Space-dev")
        status = _qs.connect().status()
        assert status["folder"] == "/home/u/api"

    def test_the_handoffs_own_spelling_still_finds_a_desk(self, profile, bridge):
        """Both spellings are probed, so a build named either way is found."""
        _control(profile, port=bridge.port, name="Quantum Space-dev")
        assert _qs.connect().status()["version"] == "0.5.1"

    def test_a_stale_installed_profile_gives_way_to_a_live_dev_one(
            self, profile, bridge):
        """Installed first in the search order, but not at the cost of the truth."""
        _control(profile, port=bridge.port, pid=_a_dead_pid())
        _control(profile, port=bridge.port, name="Quantum Space-dev")
        assert _qs.connect().status()["version"] == "0.5.1"
        assert bridge.count == 1

    def test_the_profile_paths_name_the_app_before_the_handoffs_spelling(self, profile):
        """The app's own product name first, and both spellings are probed.

        MEASURED, not copied: `app.getPath('userData')` is the packaged
        `productName`, and that name is "Quant Space" — the built `app.asar`
        carries it 272 times and "Quantum Space" zero times, while the handoff
        that froze this contract spelled the directory with the extra syllable.
        Order matters only if both exist; that both are tried is the fix.
        """
        paths = _qs.discovery_paths()
        assert [p.parent.name for p in paths] == [
            "Quant Space", "Quant Space-dev", "Quantum Space", "Quantum Space-dev"]
        assert all(p.name == "control.json" for p in paths)
        assert all(p.parent.parent == profile for p in paths)

    def test_the_token_reaches_no_repr_and_no_message(self, profile, bridge):
        """The credential must not travel: not into a log, not into a traceback."""
        path = _control(profile, port=bridge.port)
        endpoint = _qs._load(path)
        assert TOKEN not in repr(endpoint)
        assert TOKEN not in repr(_qs.connect())
        assert "hidden" in repr(endpoint)
        # ...and not out of a failure either, on either side of the wire.
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect([path.parent.parent / "nowhere" / "control.json"]).ping()
        assert TOKEN not in str(caught.value)
        bridge.desk.http_status = 401
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().ping()
        assert TOKEN not in str(caught.value)


class TestWhatADeskRefusesToBeBuiltFrom:
    """A wrong state is worse than a crash: it reads exactly like the truth.

    Measured against the live app on this machine: `Desk("handsoff")` — the
    CLIENT name passed where a path was wanted — became one relative path,
    matched no file, and answered "Quantum Space isn't running." about a desk
    that was running, with a 0600 file on disk and a live pid in it. Every
    other way of getting the argument wrong has to fail the same way that
    mistake should have: loudly, and naming the shape that was wanted.
    """

    def test_a_client_name_where_a_path_belongs_is_a_type_error_not_a_state(
            self, profile, bridge):
        _control(profile, port=bridge.port)
        with pytest.raises(ValueError) as caught:
            _qs.Desk("handsoff")
        assert "handsoff" in str(caught.value)
        assert "client=" in str(caught.value), "the message must name the fix"
        # ...and the desk that IS running is still reachable by the right call.
        assert _qs.connect().status().get("app") == "quant-space"
        assert bridge.count == 1, "only the correct construction dialled"

    def test_a_relative_path_is_refused_rather_than_searched(self, profile):
        with pytest.raises(ValueError) as caught:
            _qs.Desk(["config/control.json"])
        assert "absolute" in str(caught.value)

    def test_one_non_path_in_a_list_is_refused_by_name_too(self, profile,
                                                           bridge):
        """The same mistake one spelling in is still the same mistake.

        `Desk(7)` is refused by name, and `Desk([path, 7])` answered with
        Python's own `argument should be a str or an os.PathLike object…`
        (measured 2026-09-27) — naming neither what was wanted nor the
        mistake, and reading like a bug in this module rather than in the
        call. A list is not a special case.
        """
        path = _control(profile, port=bridge.port)
        with pytest.raises(TypeError) as caught:
            _qs.Desk([str(path), 7])
        said = str(caught.value)
        assert "7" in said and "int" in said, said
        assert "discovery-file path" in said, said
        assert _qs.connect().status().get("app") == "quant-space", (
            "the desk that IS running is still reachable by the right call")

    def test_an_empty_path_list_is_refused_not_answered(self):
        """Nothing to try would make every call say 'not running' for ever."""
        with pytest.raises(ValueError) as caught:
            _qs.Desk([])
        assert "empty" in str(caught.value)

    def test_no_paths_at_all_searches_the_profiles(self, profile, bridge):
        """The default is the profile directories, and it is the SAME list."""
        _control(profile, port=bridge.port, name="Quant Space-dev")
        desk = _qs.Desk(client="handsoff")
        assert desk.paths == _qs.discovery_paths()
        assert desk.status().get("app") == "quant-space"

    def test_a_single_absolute_path_is_a_path_list(self, profile, bridge):
        """One file is a legitimate thing to name — as a PATH, not a name."""
        path = _control(profile, port=bridge.port)
        desk = _qs.Desk(path)
        assert desk.paths == [path]
        assert desk.status().get("app") == "quant-space"


class TestTheWire:
    """What the desk's contract says on the socket, checked on the socket."""

    def test_the_request_is_a_post_to_the_root_with_a_bearer_and_no_origin(
            self, profile, bridge):
        _control(profile, port=bridge.port)
        _qs.connect().status()
        sent = bridge.sent()
        assert sent["verb"] == "POST"
        assert sent["path"] == "/"
        assert sent["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert sent["headers"]["content-type"] == "application/json"
        assert "origin" not in sent["headers"], (
            "a request carrying an Origin header is refused outright, by design")

    def test_the_client_name_goes_on_every_call_that_takes_one(
            self, profile, bridge):
        _control(profile, port=bridge.port)
        desk = _qs.connect()
        desk.ping()
        desk.hello()
        desk.status()
        desk.sessions()
        desk.read("s1")
        seen = bridge.desk.seen
        assert seen[0][1] == {}, "ping carries no client — it is liveness only"
        for _method, params in seen[1:]:
            assert params["client"] == "handsoff", params

    def test_lines_are_clamped_to_the_desks_own_range(self, profile, bridge):
        _control(profile, port=bridge.port)
        desk = _qs.connect()
        desk.read("s1", 99999)
        desk.read("s1", 0)
        assert bridge.desk.seen[0][1]["lines"] == 2000
        assert "lines" not in bridge.desk.seen[1][1], (
            "0 means the desk's own useful tail, so the key is not sent")

    def test_the_token_is_read_per_request_and_never_carried_forward(
            self, profile, bridge):
        """The headline property: a cached token is the stale credential."""
        path = _control(profile, port=bridge.port)
        desk = _qs.connect()
        desk.status()
        assert bridge.sent(0)["headers"]["authorization"] == f"Bearer {TOKEN}"
        _control(profile, port=bridge.port, token=OTHER_TOKEN)
        desk.status()
        assert bridge.sent(1)["headers"]["authorization"] == f"Bearer {OTHER_TOKEN}"
        assert path.exists()

    def test_removing_the_file_between_calls_stops_the_next_one(
            self, profile, bridge):
        """Nothing about the desk is cached on the client either."""
        path = _control(profile, port=bridge.port)
        desk = _qs.connect()
        desk.ping()
        path.unlink()
        with pytest.raises(_qs.DeskError) as caught:
            desk.ping()
        assert caught.value.state == "not-running"
        assert bridge.count == 1

    def test_a_live_file_with_nothing_listening_says_not_answering(
            self, profile):
        quiet = Bridge()
        port = quiet.port
        quiet.stop()
        _control(profile, port=port)
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "unreachable"
        assert str(port) in caught.value.message


class TestTheDesksOwnRefusals:
    """Every answer the desk writes for a human is relayed, not paraphrased."""

    def test_not_granted_speaks_the_desks_own_sentence(self, profile, bridge):
        _control(profile, port=bridge.port)
        bridge.desk.granted = False
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "not-granted"
        assert caught.value.message == bridge.desk.refusal
        assert caught.value.reason == "not-granted"

    def test_control_off_and_not_yet_allowed_stay_two_sentences(
            self, profile, bridge):
        """One wire reason covers both; the desk's message is what separates them."""
        _control(profile, port=bridge.port)
        bridge.desk.granted = False
        bridge.desk.refusal = DESK_CONTROL_OFF
        with pytest.raises(_qs.DeskError) as off:
            _qs.connect().status()
        bridge.desk.refusal = DESK_NOT_ALLOWED
        with pytest.raises(_qs.DeskError) as allowed:
            _qs.connect().status()
        assert off.value.state == allowed.value.state == "not-granted"
        assert off.value.message == DESK_CONTROL_OFF
        assert allowed.value.message == DESK_NOT_ALLOWED
        assert off.value.message != allowed.value.message

    def test_a_not_granted_hello_that_does_not_say_granted_is_a_refusal(
            self, profile, bridge):
        """`granted: true` is the answer; anything else is not a grant."""
        _control(profile, port=bridge.port)
        bridge.desk.granted = True
        original = bridge.desk.answer
        bridge.desk.answer = lambda body: (
            200, _result(body.get("id"), {"granted": False}))
        try:
            with pytest.raises(_qs.DeskError) as caught:
                _qs.connect().hello()
        finally:
            bridge.desk.answer = original
        assert caught.value.state == "not-granted"

    def test_session_not_found_is_its_own_state(self, profile, bridge):
        _control(profile, port=bridge.port)
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().read("ghost")
        assert caught.value.state == "session-gone"
        assert caught.value.reason == "session-not-found"

    def test_no_output_is_its_own_state(self, profile, bridge):
        _control(profile, port=bridge.port)
        bridge.desk.text = ""
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().read("s1")
        assert caught.value.state == "no-output"
        # The desk's own sentence, in the desk's own words (its live wording on
        # 2026-09-21) — this client relays it rather than paraphrasing.
        assert caught.value.message == ("That session has no readable output "
                                        "in Quant Space.")

    def test_an_unknown_method_names_a_version_mismatch(self, profile, bridge):
        _control(profile, port=bridge.port)
        original = bridge.desk.answer
        bridge.desk.answer = lambda body: (
            200, _error(body.get("id"), "unknown-method", "no such method"))
        try:
            with pytest.raises(_qs.DeskError) as caught:
                _qs.connect().call("desk.future")
        finally:
            bridge.desk.answer = original
        assert caught.value.state == "error"
        assert "version mismatch" in caught.value.message

    def test_a_401_is_the_token_and_says_so(self, profile, bridge):
        _control(profile, port=bridge.port)
        bridge.desk.http_status = 401
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "auth"
        assert "token" in caught.value.message

    @pytest.mark.parametrize("status,needle", [
        (403, "Origin"),
        (405, "POST"),
        (400, "JSON"),
        (413, "cap"),
    ])
    def test_the_http_status_is_checked_before_the_body(
            self, profile, bridge, status, needle):
        _control(profile, port=bridge.port)
        bridge.desk.http_status = status
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "refused"
        assert str(status) in caught.value.message and needle in caught.value.message

    def test_a_notification_answer_is_not_a_silent_success(self, profile, bridge):
        _control(profile, port=bridge.port)
        bridge.desk.http_status = 202
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "refused"
        assert "notification" in caught.value.message

    def test_a_200_that_is_not_json_is_refused(self, profile, bridge):
        _control(profile, port=bridge.port)
        bridge.desk.raw_body = b"<html>hello</html>"
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "error"

    def test_an_error_without_a_known_reason_still_carries_the_desks_words(
            self, profile, bridge):
        _control(profile, port=bridge.port)
        bridge.desk.answer = lambda body: (
            200, _error(body.get("id"), "something-new", "the desk is restarting"))
        with pytest.raises(_qs.DeskError) as caught:
            _qs.connect().status()
        assert caught.value.state == "error"
        assert caught.value.message == "the desk is restarting"

    def test_the_state_vocabulary_is_closed(self):
        """An unknown state fails CLOSED rather than reading as a success."""
        assert _qs.DeskError("nonsense", "x").state == "error"
        assert set(_qs.STATES) >= {"not-running", "not-granted", "session-gone",
                                   "untrusted", "protocol", "unreachable",
                                   "auth", "refused", "no-output", "error"}


class TestShapingAnAnswerForAVoice:
    """Pure shaping: what the model reads, and what a person hears."""

    def test_repeated_kinds_fold_into_a_count(self):
        sessions = [dict(s) for s in SESSIONS] + [
            {"id": "s3", "kind": "shell", "cwd": "/home/u", "readable": True}]
        said = _qs.describe_sessions(sessions)
        assert said == ("three sessions: claude in the api folder and two shells "
                        "in the u folder."), said

    def test_an_empty_desk_says_so(self):
        assert _qs.describe_sessions([]) == "Quantum Space has no sessions open."

    def test_a_desk_with_many_sessions_is_summarised(self):
        said = _qs.describe_sessions(
            [{"id": f"s{i}", "kind": "shell", "cwd": "/u"} for i in range(9)])
        assert said.startswith("nine sessions:") and "three more" in said

    def test_the_index_names_the_ids_the_model_must_hand_back(self):
        index = _qs.session_index(SESSIONS)
        assert "s1: claude in api" in index and "s2: shell in u" in index
        assert _qs.session_index([]) == ""

    def test_resolve_session_takes_an_id_a_name_or_half_a_name(self):
        assert _qs.resolve_session(SESSIONS, "s1")["id"] == "s1"
        assert _qs.resolve_session(SESSIONS, "claude")["id"] == "s1"
        assert _qs.resolve_session(SESSIONS, "CLAUD")["id"] == "s1"

    def test_an_ambiguous_half_name_is_refused_rather_than_guessed(self):
        two = [{"id": "a", "name": "api-1"}, {"id": "b", "name": "api-2"}]
        with pytest.raises(_qs.DeskError) as caught:
            _qs.resolve_session(two, "api")
        assert "more than one" in caught.value.message
        assert "api-1" in caught.value.message and "api-2" in caught.value.message

    def test_two_tiles_with_the_same_name_are_still_refused(self):
        """The confident version of the same refusal, and it had no test.

        Found by measurement rather than by reading: the coverage gate
        reported `core/qs_desk.py:657` uncovered (2026-09-27), which is the
        EXACT-name branch — the half-match above is the only one the walk's
        corpus reaches, because both `raise _ambiguous(...)` sites are hops
        and a corpus keyed by class name has one entry for them. So a user
        who said exactly what the desk calls a tile, and was still wrong
        because two tiles answer to it, was the one shape of this refusal
        nothing exercised.

        What is pinned is the refusal, not its wording: reading the wrong
        agent's screen is worse than asking, and that is true whichever
        branch the ambiguity came from. The CHOICES this sentence lists are
        the same word twice when the names collide, so the "say which one"
        it ends with is not yet actionable — that is reported, not pinned
        here, because the fix is a wording decision about what a person
        should hear and not a test's to make.
        """
        twin = [{"id": "w1_p_1", "name": "claude", "cwd": "/home/u/api"},
                {"id": "w1_p_2", "name": "claude", "cwd": "/home/u/web"}]
        with pytest.raises(_qs.DeskError) as caught:
            _qs.resolve_session(twin, "claude")
        assert "more than one" in caught.value.message, caught.value.message
        assert "claude" in caught.value.message
        # the id is the tie-breaker the caller has, and it must not resolve
        # silently: a name that is exact is still refused, not guessed
        assert _qs.resolve_session(twin, "w1_p_2")["id"] == "w1_p_2"

    def test_a_name_that_is_gone_lists_what_is_open(self):
        with pytest.raises(_qs.DeskError) as caught:
            _qs.resolve_session(SESSIONS, "ghost")
        assert caught.value.state == "session-gone"
        assert "claude" in caught.value.message
        assert "shell" in caught.value.message

    def test_a_long_read_keeps_the_tail_and_says_it_trimmed(self):
        said = _qs.describe_read({"id": "s1", "name": "claude",
                                  "text": "x" * 20 + "NEWEST"}, max_chars=10)
        assert "xxxxNEWEST" in said, "the TAIL is what is on screen now"
        assert "trimmed to the last 10 characters" in said

    def test_the_desks_own_truncation_flag_is_stated(self):
        said = _qs.describe_read({"id": "s1", "name": "claude", "text": "hi",
                                  "truncated": True})
        assert "truncated" in said

    def test_an_empty_read_is_an_answer_with_a_sentence(self):
        said = _qs.describe_read({"id": "s1", "name": "claude", "text": ""})
        assert said == "claude has nothing readable on screen right now."

    def test_the_status_line_names_the_app_the_folder_and_the_sessions(self):
        said = _qs.describe_status(
            {"version": "0.5.1", "folder": "/home/u/api", "control": True,
             "windows": 2}, SESSIONS[:1])
        assert said.startswith("Quantum Space is running (v0.5.1) with the api "
                               "folder open.")
        assert "one session: claude in the api folder." in said
        assert "Control is on and 2 windows." in said

    def test_the_desks_own_control_object_is_spoken_with_the_grant(self):
        """The shape a LIVE desk answers with, not the bool a fake invented.

        Caught on 2026-09-21 against a real Quant Space: `control` comes back as
        ``{enabled, clients}``, and a client that only understood a bare bool
        said nothing about the very consent the whole feature rests on.
        """
        said = _qs.describe_status(
            {"version": "0.5.1", "folder": "/home/u/api",
             "control": {"enabled": True, "clients": ["handsoff"]},
             "windows": 1}, SESSIONS[:1])
        assert "Control is on for handsoff" in said
        assert "1 window." in said

    def test_a_control_object_with_nothing_to_say_says_nothing(self):
        """Off is said; an unknown shape is not guessed at."""
        off = _qs.describe_status({"control": {"enabled": False, "clients": []}}, [])
        assert "Control is off." in off
        unknown = _qs.describe_status(
            {"version": "0.5.1", "control": {"enabled": "yes"}, "windows": [1, 2]},
            [])
        assert "control" not in unknown.lower()
        assert "window" not in unknown.lower()

    def test_a_name_that_is_a_whole_command_line_is_cut_before_it_is_spoken(self):
        """Measured live: a `run` tile's name IS its 171-character command line.

        The desk names a run tile with the command it was started with, and this
        client reads names out loud — so the bound belongs here, and the cut is
        shown rather than hidden.
        """
        command = ('ollama run gemma4:12b "Write a long technical report in '
                   'markdown about terminal emulators: at least 120 lines, with '
                   'headings, a table and a code block. Do not stop early."')
        one = [{"id": "w1_p_2", "name": command, "kind": "run",
                "cwd": "/tmp/qs-desk-demo", "readable": True}]
        said = _qs.describe_sessions(one)
        assert command not in said
        assert "…" in said, said
        assert len(said) < 140, said
        # The index the model reads is bounded the same way, and the ID it has to
        # hand back is untouched.
        index = _qs.session_index(one)
        assert command not in index and "w1_p_2" in index

    def test_a_screen_with_no_line_breaks_is_called_out_as_one_line(self):
        """Measured live: a TUI screen reads as ONE line, and the desk says so not.

        2 173 characters, 362 box glyphs, zero newlines, and the desk reporting
        `lines: 1, truncated: false` about a whole screen. A read that says
        nothing would present a screen as a one-line session.
        """
        screen = ("78Claude Codev2.1.278 ▐▛███▛█nvidia/nemotron-3-▝▜██████▀u… "
                  + "─" * 300 + " ❯ /model   ⎿  Keptmodelasth-orchestra")
        said = _qs.describe_read({"id": "w1_p_1", "name": "claude",
                                  "text": screen, "lines": 1,
                                  "truncated": False})
        assert "ONE line" in said
        assert screen in said, "the screen itself is still relayed"

    def test_a_real_stream_of_lines_is_not_called_out(self):
        """The note is about a screen, not about reads in general."""
        stream = "\n".join(f"line {i}" for i in range(50))
        said = _qs.describe_read({"id": "w1_p_2", "name": "ollama",
                                  "text": stream, "lines": 50,
                                  "truncated": False})
        assert "ONE line" not in said

    def test_a_mangled_tail_is_relayed_exactly_as_the_desk_stripped_it(self):
        """The desk's stripping is not always prose — and it is not ours to tidy.

        A real Claude TUI positions with escapes, so the desk's own stripping
        hands back words with the spaces eaten, box rules and a repeated repaint
        (measured 2026-09-21). Rewriting that here would be this client inventing
        the agent's words; the read is what the desk says it is.
        """
        said = _qs.describe_read(
            {"id": "w1_p_1", "name": "claude", "kind": "claude",
             "agent": "claude", "text": MANGLED_TAIL, "lines": 4,
             "truncated": True})
        assert MANGLED_TAIL in said
        assert "truncated" in said


class Case:
    """A belt wired to a throw-away profile, a fake desk and a recording voice.

    The belt is built exactly as the host builds it — with the live permissions
    dict — so the gate tests exercise the real path rather than a fixture that
    remembers to set `_perm` by hand.
    """

    def __init__(self, H, monkeypatch, tmp_path, bridge):
        self.H, self.bridge, self.tmp = H, bridge, tmp_path
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        monkeypatch.setattr(H, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(H, "DECISIONS_FILE",
                            tmp_path / "state" / "decisions.jsonl")
        self.said = []                      # what the user is told without a
        monkeypatch.setattr(H, "notify", self.said.append)   # transcript
        self.control = _control(tmp_path, port=bridge.port)
        self.belt = H.ToolBelt(on_restart_pending=lambda: None,
                               permissions=H.SETTINGS["permissions"])

    def run(self, tool, **args):
        return self.belt.execute(tool, args)

    def decisions(self):
        path = self.H.DECISIONS_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def case(H, monkeypatch, tmp_path, bridge):
    return Case(H, monkeypatch, tmp_path, bridge)


class TestTheTools:
    """The family, driven through `execute` — gates, arguments and all."""

    def test_status_answers_what_is_open(self, case):
        text, err = case.run("quant_space_status")
        assert not err, text
        assert text.startswith("Quantum Space is running (v0.5.1)")
        assert "claude in the api folder" in text
        assert [m for m, _p in case.bridge.desk.seen][:2] == ["hello", "desk.status"]

    def test_an_unreadable_control_file_reaches_the_user_as_its_own_sentence(
            self, case):
        """The sentence has to arrive, not the loop's excuse for it.

        A discovery file that is not text used to leave the client as a
        `UnicodeDecodeError`, which no `except DeskError` in these tools
        catches — so the user heard the tool loop report that the
        ASSISTANT's own check had failed before the tool ran, which is a
        different and wrong story about a desk that was sitting right there.
        """
        case.control.write_bytes(b'{"app": "\xff\xfe", "protocol": 1}')
        os.chmod(case.control, 0o600)
        text, err = case.run("quant_space_status")
        assert err, text
        assert text.startswith("ERROR:"), text
        assert "not UTF-8 text" in text, text
        assert "could not be evaluated" not in text, (
            "the assistant's check did not fail — the desk's file was not "
            f"text, and that is what the user has to be told: {text}")

    def test_sessions_gives_the_model_the_ids_it_has_to_hand_back(self, case):
        text, err = case.run("quant_space_sessions")
        assert not err, text
        assert text.startswith("two sessions:")
        assert "s1: claude in api" in text

    def test_read_resolves_a_name_against_the_desks_own_list(self, case):
        text, err = case.run("quant_space_read", session="claude")
        assert not err, text
        assert text.startswith("claude in the api folder — the tail of that "
                               "session:")
        assert "12 passed" in text
        read = next(p for m, p in case.bridge.desk.seen if m == "session.read")
        assert read["id"] == "s1"

    def test_an_id_in_the_apps_own_shape_is_handed_straight_back(self, case):
        """A real desk's ids are window-prefixed panel ids (`w1_p_1`).

        This client treats an id as opaque — it is matched against the desk's
        own list and sent back verbatim — so the shape must not matter.
        """
        case.bridge.desk.sessions = [
            {"id": "w1_p_1", "name": "claude", "kind": "claude",
             "agent": "claude", "cwd": "/home/u/api", "startedAt": 1,
             "readable": True}]
        text, err = case.run("quant_space_read", session="w1_p_1")
        assert not err, text
        read = next(p for m, p in case.bridge.desk.seen if m == "session.read")
        assert read["id"] == "w1_p_1"

    def test_read_refuses_when_the_name_is_gone_and_says_what_is_open(self, case):
        text, err = case.run("quant_space_read", session="ghost")
        assert err
        assert text == ("ERROR: That session isn't on the desk any more. Open "
                        "now: claude, shell.")

    def test_a_session_with_nothing_on_screen_is_an_answer_not_a_failure(self, case):
        case.bridge.desk.text = ""
        text, err = case.run("quant_space_read", session="claude")
        assert not err, text
        assert text == ("claude in the api folder has nothing readable on "
                        "screen right now.")

    def test_the_four_failure_states_are_four_sentences(self, case):
        """The requirement in one test: they must not collapse into one line.

        not running / the desk refusing on its own rules / a session that has
        gone are three problems with three different fixes, and a user who
        hears one sentence for all three cannot tell which one they have.
        """
        case.control.unlink()
        not_running, _ = case.run("quant_space_status")

        case.control = _control(case.tmp, port=case.bridge.port)
        case.bridge.desk.granted = False
        refused, _ = case.run("quant_space_read", session="claude")

        case.bridge.desk.granted = True
        gone, _ = case.run("quant_space_read", session="ghost")

        assert not_running == "ERROR: Quantum Space isn't running."
        assert refused == f"REFUSED: {DESK_NOT_ALLOWED}"
        assert gone == ("ERROR: That session isn't on the desk any more. Open "
                        "now: claude, shell.")
        assert len({not_running, refused, gone}) == 3

    def test_control_off_and_not_yet_allowed_are_two_sentences(self, case):
        """The desk's own two `not-granted` situations, kept apart."""
        case.bridge.desk.granted = False
        case.bridge.desk.refusal = DESK_CONTROL_OFF
        off, _ = case.run("quant_space_check")
        case.bridge.desk.refusal = DESK_NOT_ALLOWED
        allowed, _ = case.run("quant_space_check")
        assert DESK_CONTROL_OFF in off and DESK_NOT_ALLOWED in allowed
        assert off != allowed

    def test_a_refusal_is_announced_and_kept_not_only_in_the_reply(self, case):
        case.bridge.desk.granted = False
        text, err = case.run("quant_space_status")
        assert err and text.startswith("REFUSED: ")
        assert case.said == [f"Quantum Space: {case.bridge.desk.refusal}"], case.said
        entry = [d for d in case.decisions() if d["tool"] == "quant_space_status"][-1]
        assert entry["decision"] == "REFUSED"
        assert entry["result"] == "desk: not-granted"

    def test_a_looping_model_folds_into_the_hosts_coalescing(self, case):
        """The belt does not rate-limit the announcement; the host folds it.

        Deliberately NOT a second cooldown here: the host's notify already folds
        identical repeats inside its window (tests/test_notify_coalesce.py), so
        the belt's job is to say the SAME sentence every time and let it.
        """
        case.bridge.desk.granted = False
        case.run("quant_space_sessions")
        case.run("quant_space_sessions")
        assert case.said == [f"Quantum Space: {DESK_NOT_ALLOWED}"] * 2

    def test_the_family_rides_one_gate(self):
        assert case_gate_names() == {"quant_space_status": "quant_space",
                                     "quant_space_sessions": "quant_space",
                                     "quant_space_read": "quant_space",
                                     "quant_space_check": "quant_space"}

    def test_switching_the_gate_off_refuses_every_one_of_them(self, case):
        case.H.SETTINGS["permissions"] = {
            **case.H.SETTINGS["permissions"], "quant_space": False}
        case.belt.set_permissions(case.H.SETTINGS["permissions"])
        for tool, args in (("quant_space_status", {}),
                           ("quant_space_sessions", {}),
                           ("quant_space_read", {"session": "claude"}),
                           ("quant_space_check", {})):
            text, err = case.run(tool, **args)
            assert err, (tool, text)
            assert text == ("REFUSED: the 'quant_space' tool is disabled in "
                            "handsoff settings"), (tool, text)
        assert case.bridge.count == 0, "a disabled gate must not reach the desk"

    def test_a_command_policy_deny_still_wins_over_the_gate(self, case):
        case.H.SETTINGS["command_policy"] = {"quant_space_read": "DENY"}
        text, err = case.run("quant_space_read", session="claude")
        assert err and "DENIED by the user's command policy" in text
        assert case.bridge.count == 0

    def test_the_check_tool_names_the_state_it_found(self, case):
        text, err = case.run("quant_space_check")
        assert not err, text
        assert text.startswith("Quantum Space desk: fine — Quantum Space is running")

        case.bridge.desk.granted = False
        text, err = case.run("quant_space_check")
        assert text.startswith("REFUSED: not granted — the desk says: ")
        assert DESK_NOT_ALLOWED in text

        case.bridge.desk.granted = True
        case.control.unlink()
        text, err = case.run("quant_space_check")
        assert text == "ERROR: not running — Quantum Space isn't running."

    def test_the_check_tool_can_report_a_named_session_that_has_gone(self, case):
        text, _err = case.run("quant_space_check", session="ghost")
        assert "that session is gone" in text
        assert "Open now: claude, shell." in text

    def test_nothing_the_tool_says_or_keeps_carries_the_token(self, case):
        case.run("quant_space_read", session="claude")
        case.bridge.desk.granted = False
        case.run("quant_space_read", session="claude")
        blob = json.dumps({"said": case.said, "decisions": case.decisions()})
        assert TOKEN not in blob, "the credential must not reach a log or a popup"

    def test_a_broken_decision_log_never_breaks_the_tool_call(
            self, case, monkeypatch):
        """Reading the desk must not depend on the audit trail being writable."""
        monkeypatch.setattr(case.H, "DECISIONS_FILE",
                            case.tmp / "nowhere" / "nested" / "decisions.jsonl")
        text, err = case.run("quant_space_status")
        assert not err, text
        assert text.startswith("Quantum Space is running")


class TestTheDeclaration:
    """The module has to reach a deployment, and the specs have to say so."""

    def test_the_installer_ships_it(self):
        """CORE_REQUIRED is the floor a tarball install stages; a missing entry
        is a feature that compiles here and is absent on the deployed machine."""
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines()
                    if ln.startswith("CORE_REQUIRED="))
        required = line.split('"', 2)[1].split()
        assert "qs_desk" in required, required
        assert (ROOT / "core" / "qs_desk.py").is_file()

    def test_the_module_is_a_stdlib_leaf(self):
        """It must not reach into the host: that is what makes it testable here."""
        tree = ast.parse((ROOT / "core" / "qs_desk.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= (set(sys.stdlib_module_names) | {"__future__"}), imported
