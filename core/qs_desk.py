"""Read the Quantum Space desk over the local control channel it publishes.

Quantum Space — the desktop workbench that runs AI coding agents in terminal
tiles — writes a discovery file beside its own settings (a port, a capability
token, and the pid that owns the file) and serves five READ-ONLY JSON-RPC
methods on loopback. This module is the client half of that contract: it hands a
question about the user's desk to the app that owns the desk, and brings back
the sentence that app wrote.

Four rules shape everything here, and each one is a way a client gets this
wrong:

* **The discovery file is not ours to trust.** A mode that is not ``0600``, a
  protocol number we do not speak, or a live pid that is not ours means DO NOT
  CONNECT — the file is a claim by whatever wrote it, not evidence about the
  app.
* **The token is read PER REQUEST, never cached.** Quantum Space rotates it
  every time it starts, so a copy held from connect time is exactly the stale
  credential the rotation exists to defeat — the same reason handsoff's own
  ``_read_control_token()`` reads its token per request rather than at startup.
* **A client that cannot read the credential still sends the request.** The
  refusal comes from the server that owns the rule, in one place, with its own
  wording; nothing here decides on the user's behalf that "control is off".
* **The desk's sentence is the answer.** ``message`` is already written for a
  person ("handsoff is not allowed to read this desk yet. Open Quant Space →
  Settings → Control and allow it."), so it is SURFACED, never paraphrased.

Deliberately NOT ``core/web.py``: that module's whole job is the opposite of
this one. ``_public_target`` refuses loopback and private addresses — 127.0.0.1
comes back as "is on this machine or a private network" — and ``read_page``
walks a public redirect chain. Loopback-only is a property here, not a hazard,
so the transport is stdlib ``http.client``, which also ignores the proxy
environment variables ``urllib`` honours: that would be one more way a request
meant for this machine could leave it.

A dependency-free leaf (stdlib only, nothing from handsoff's globals): where to
look for the discovery file and who we claim to be arrive as arguments, which is
what lets the suite drive every path against a fake bridge on an ephemeral port
with no Quantum Space installed.
"""
from __future__ import annotations

import http.client
import json
import os
import stat as _stat
from itertools import count
from pathlib import Path

#: The desk protocol this client speaks. A different number is a DIFFERENT
#: contract, not a best-effort attempt, so it is refused by name.
PROTOCOL = 1

#: Who we say we are. The desk matches this against ``[A-Za-z0-9][A-Za-z0-9._-]{0,39}``
#: and keeps its own allow-list, so the name is a claim to be granted, not a
#: permission.
CLIENT = "handsoff"

#: Where Quantum Space writes ``control.json``, in the order we try them. The
#: installed build and a ``npm run dev`` run are different PROFILES — separate
#: app directories, separate tokens — and normally only one exists.
#:
#: MEASURED against the app itself, not copied from the handoff: these are
#: ``app.getPath('userData')`` names, which come from the packaged
#: ``productName``. That product name is **"Quant Space"** — the built
#: ``app.asar`` carries that string 272 times and "Quantum Space" zero times,
#: and a dev run appends ``-dev`` to the SAME basename (``review-profile.js``:
#: ``normalPath + (packaged ? '' : '-dev')``). The first live run of this client
#: said "isn't running" at a granted, listening desk because the frozen handoff
#: spelled the directory "Quantum Space": one letter of prose, and the file was
#: invisible. Both spellings are probed, the app's own first, so a build named
#: either way is found; a name that exists but is not ours to trust still stops
#: the search (see ``_load``), so this is not a guess about which desk answered.
APP_DIRS = ("Quant Space", "Quant Space-dev",
            "Quantum Space", "Quantum Space-dev")

#: A few seconds: this is a loopback call to a local app, and the desk's own
#: request limit is 30 s.
TIMEOUT_S = 5.0

#: The desk clamps ``lines`` to 1..2000; clamped here too, so an argument from a
#: small model cannot ask for a million lines and be quietly given 2000.
MAX_LINES = 2000

#: What one read may put in the conversation. A coding agent's scrollback is not
#: a document, and the model is going to have to talk about it.
MAX_TEXT_CHARS = 8000

#: How much of a session's own name may be spoken. The desk names a `run` tile
#: with the WHOLE command line it was started with — measured live, a 171-char
#: sentence — and a name is something this app says out loud ("two sessions:
#: …"). Bounded here rather than trusted, because the length comes from another
#: app's naming convention and not from anything this client chose.
MAX_NAME_CHARS = 40

#: Every state a desk call can end in. `not-granted` is ONE wire reason that
#: covers two situations the user experiences differently — control switched off
#: in Quantum Space, and handsoff not yet on its allow-list — and the desk's own
#: message is what tells them apart, which is why the message is carried whole.
STATES = (
    "not-running",     # no discovery file, or the pid in it is gone (stale)
    "untrusted",       # a file that exists and is not ours to trust
    "protocol",        # a desk that speaks a different protocol
    "unreachable",     # a live file, nothing answering on the port
    "auth",            # the desk refused the token (401)
    "refused",         # the desk refused the request's shape (403/405/413/400/202)
    "not-granted",     # the desk's own refusal: control off, or not allowed yet
    "session-gone",    # session-not-found
    "no-output",       # the session is open and has nothing to read
    "error",           # anything else the desk said, or a malformed reply
)

_NOT_RUNNING = "Quantum Space isn't running."
_NOT_GRANTED_FALLBACK = (
    "handsoff is not allowed to read this desk yet — open Quantum Space → "
    "Settings → Control and allow it.")
_SESSION_GONE = "That session isn't on the desk any more."


class DeskError(Exception):
    """A desk call that produced no result, with the reason kept WHOLE.

    ``state`` is handsoff's own word (one of :data:`STATES`); ``reason`` is the
    desk's wire reason when there was one; ``message`` is the sentence to say
    out loud — the desk's own for ``not-granted``, handsoff's otherwise; and
    ``detail`` is the desk's raw message where it is not already ``message``, so
    the diagnostic can show the desk's own words without paraphrasing them away.
    """

    def __init__(self, state: str, message: str, reason: str = "",
                 detail: str = "") -> None:
        super().__init__(message)
        self.state = state if state in STATES else "error"
        self.message = str(message)
        self.reason = str(reason or "")
        self.detail = str(detail or "")

    def __str__(self) -> str:
        return self.message


def discovery_paths(config_home=None) -> list:
    """Every place Quantum Space may have written its discovery file.

    ``$XDG_CONFIG_HOME`` decides the root (``~/.config`` when it is unset — the
    contract has no environment override for the file itself), and every
    profile name in :data:`APP_DIRS` is tried because only one of them will
    exist. Returns them in the order they are tried; the first existing file
    decides, and a file that exists and is not ours to trust refuses outright.
    """
    root = config_home or os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return [Path(root) / name / "control.json" for name in APP_DIRS]


class _Endpoint:
    """One resolved discovery file: where to dial, and with what.

    The token lives here as an attribute and nowhere else — not in ``__repr__``,
    not in any message — because this object can end up inside a traceback, and
    a traceback ends up in the journal.
    """

    __slots__ = ("app", "folder", "path", "pid", "port", "token", "version")

    def __init__(self, path, port, token, pid, app, version, folder) -> None:
        self.path = path
        self.port = port
        self.token = token
        self.pid = pid
        self.app = app
        self.version = version
        self.folder = folder

    def __repr__(self) -> str:      # a token must never reach a log line
        return (f"_Endpoint(port={self.port}, pid={self.pid!r}, "
                f"app={self.app!r}, version={self.version!r}, "
                f"token=<{len(self.token)} chars hidden>)")


def _load(path) -> _Endpoint | None:
    """The endpoint a discovery file names, or None when there is no file.

    Every way the file can fail to be ours is a REFUSAL that stops the search
    rather than a reason to go looking somewhere else: a wrong mode, a protocol
    we do not speak and a file that is not JSON at all are each a definite
    answer, and the other profile's file is a different desk, not a fallback.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise DeskError(
            "untrusted",
            f"Quantum Space's control file {path} could not be read "
            f"({type(e).__name__}) — refusing to use it.") from None
    if not _stat.S_ISREG(info.st_mode):
        raise DeskError(
            "untrusted",
            f"Quantum Space's control file {path} is not a regular file — "
            f"refusing to use it.")
    mode = _stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise DeskError(
            "untrusted",
            f"Quantum Space's control file {path} is mode {mode:04o}, not 0600 "
            f"— refusing to use a file that holds a control token.")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise DeskError(
            "untrusted",
            f"Quantum Space's control file {path} could not be read "
            f"({type(e).__name__}) — refusing to use it.") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise DeskError(
            "untrusted",
            f"Quantum Space's control file {path} is not valid JSON — "
            f"refusing to use it.") from None
    if not isinstance(data, dict):
        raise DeskError("untrusted",
                        f"Quantum Space's control file {path} is not a JSON "
                        f"object — refusing to use it.")
    protocol = data.get("protocol")
    if protocol != PROTOCOL:
        raise DeskError(
            "protocol",
            f"Quantum Space's control file {path} names desk protocol "
            f"{protocol!r}; handsoff speaks {PROTOCOL}. That is a version "
            f"mismatch, not a connection problem.")
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise DeskError("untrusted",
                        f"Quantum Space's control file {path} names no usable "
                        f"port — refusing to use it.")
    token = data.get("token")
    if not isinstance(token, str) or not token.strip():
        raise DeskError("untrusted",
                        f"Quantum Space's control file {path} carries no control "
                        f"token — refusing to use it.")
    return _Endpoint(path, port, token.strip(), data.get("pid"),
                     str(data.get("app") or ""), str(data.get("version") or ""),
                     str(data.get("folder") or ""))


def _pid_state(pid) -> str:
    """'live' | 'gone' | 'foreign' | 'unknown' for the pid a file names.

    One ``kill(0)`` answers both questions the file raises: a ProcessLookupError
    means the app died without cleaning up (a STALE file — say so, rather than
    reporting "connection refused" about a port nobody claimed), and a
    PermissionError means the process exists and is not ours, which is a
    different app owning that file. No /proc parsing: the signal check is the
    stronger statement and does not depend on a layout.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "foreign"
    except OSError:
        return "unknown"
    return "live"


def _headers(token: str) -> dict:
    """The headers the desk accepts, and notably the one it refuses.

    No ``Origin``: a request carrying one is refused outright by design, so a
    web page cannot reach the desk. ``http.client`` sends none by default, and
    this is the place that says so on purpose.
    """
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"}


def _status_sentence(status: int) -> str:
    """What a non-200 status means, in the user's terms, one sentence each.

    HTTP comes first in the contract and is checked first here: a 403 is not an
    error inside the JSON body, it is the desk refusing the shape of the request
    before it ever parsed one.
    """
    if status == 403:
        return ("Quantum Space refused the request's shape (403) — it refuses "
                "any request carrying an Origin header and requires a 127.0.0.1 "
                "Host, so something on this machine rewrote it. Nothing was read.")
    if status == 405:
        return ("Quantum Space only accepts POST (405) — handsoff sent the wrong "
                "verb.")
    if status == 400:
        return "Quantum Space could not parse the request as JSON (400)."
    if status == 413:
        return "The request was over Quantum Space's body cap (413)."
    return f"Quantum Space refused the request with HTTP {status}."


def _rpc_error(method: str, error) -> DeskError:
    """The DeskError for one JSON-RPC ``error`` object.

    ``data.reason`` decides the state, and ``message`` is already a sentence
    written for a human — so for a refusal by the desk's own rules it IS the
    message we speak, and it is never rewritten into our own words.
    """
    if not isinstance(error, dict):
        return DeskError("error", f"Quantum Space answered {method!r} with a "
                                  f"malformed error.")
    message = str(error.get("message") or "").strip()
    data = error.get("data")
    reason = ""
    if isinstance(data, dict):
        reason = str(data.get("reason") or "").strip()
    if reason == "not-granted":
        # Control switched off, or handsoff not allowed yet: one wire reason,
        # and the desk's sentence is the one that tells them apart.
        return DeskError("not-granted", message or _NOT_GRANTED_FALLBACK,
                         reason=reason, detail=message)
    if reason == "session-not-found":
        return DeskError("session-gone", _SESSION_GONE, reason=reason,
                         detail=message)
    if reason == "no-output":
        return DeskError("no-output",
                         message or "That session has nothing readable yet.",
                         reason=reason, detail=message)
    if reason in ("unknown-method", "invalid-request"):
        return DeskError("error",
                         f"Quantum Space does not accept {method!r} as handsoff "
                         f"sends it ({reason}) — a version mismatch, not a "
                         f"permission problem.", reason=reason, detail=message)
    return DeskError("error",
                     message or f"Quantum Space refused {method!r} "
                                f"(code {error.get('code')!r}).",
                     reason=reason, detail=message)


class Desk:
    """A handle on the desk: it resolves the discovery file PER CALL.

    Deliberately not "connect once, then talk": the port and the token belong to
    the process that wrote the file, and that process can restart between two
    calls, so each request re-reads the file, re-checks the pid and re-reads the
    TOKEN — the credential is never carried forward from an earlier moment.
    """

    def __init__(self, paths=None, client: str = CLIENT,
                 timeout: float = TIMEOUT_S) -> None:
        # `paths` may be omitted (the profile directories are then searched),
        # and every entry must be an absolute path: see `_as_paths`, which
        # exists because a client name passed here used to read as "the desk is
        # not running" instead of as the mistake it was.
        self.paths = _as_paths(paths)
        self.client = str(client or CLIENT)
        self.timeout = float(timeout)
        self._ids = count(1)

    def __repr__(self) -> str:
        return f"Desk({[str(p) for p in self.paths]!r}, client={self.client!r})"

    def resolve(self) -> _Endpoint:
        """The live endpoint right now, or a DeskError naming the state."""
        for path in self.paths:
            endpoint = _load(path)
            if endpoint is None:
                continue
            state = _pid_state(endpoint.pid)
            if state == "live":
                return endpoint
            if state == "foreign":
                raise DeskError(
                    "untrusted",
                    f"Quantum Space's control file {path} names pid "
                    f"{endpoint.pid}, which is running and is not ours — "
                    f"refusing to connect to a process that did not write it.")
        # Neither profile has a file, or the one that has it names a pid that is
        # gone: from where the user sits those are the same answer, which is why
        # they are one state and one sentence.
        raise DeskError("not-running", _NOT_RUNNING)

    def call(self, method: str, params=None) -> dict:
        """One JSON-RPC request; the result dict, or a DeskError."""
        endpoint = self.resolve()
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": str(method),
                   "params": dict(params or {})}
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError):
            raise DeskError("error",
                            f"handsoff could not encode its own {method!r} "
                            f"request.") from None
        conn = http.client.HTTPConnection("127.0.0.1", endpoint.port,
                                          timeout=self.timeout)
        try:
            conn.request("POST", "/", body=body, headers=_headers(endpoint.token))
            response = conn.getresponse()
            status, raw = response.status, response.read()
        except (OSError, http.client.HTTPException) as e:
            raise DeskError(
                "unreachable",
                f"Quantum Space's control file is there, but nothing answered "
                f"on 127.0.0.1:{endpoint.port} ({type(e).__name__}) — it may be "
                f"starting up or shutting down.") from None
        finally:
            conn.close()
        if status == 202:
            raise DeskError("refused",
                            "Quantum Space took the request as a notification "
                            "and answered nothing (202) — it expected an id.")
        if status != 200:
            raise DeskError("auth" if status == 401 else "refused",
                            ("Quantum Space refused the control token (401) — "
                             "handsoff read it a moment too late. Try again."
                             if status == 401 else _status_sentence(status)))
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise DeskError("error",
                            f"Quantum Space's answer to {method!r} was not "
                            f"JSON.") from None
        if not isinstance(doc, dict):
            raise DeskError("error",
                            f"Quantum Space's answer to {method!r} was not a "
                            f"JSON object.")
        if doc.get("error"):
            raise _rpc_error(str(method), doc["error"])
        result = doc.get("result")
        if not isinstance(result, dict):
            raise DeskError("error",
                            f"Quantum Space answered {method!r} without a "
                            f"result.")
        return result

    # -- the five methods ----------------------------------------------------
    def ping(self) -> dict:
        """Liveness only: no client name, no desk state."""
        return self.call("ping")

    def hello(self) -> dict:
        """Offer the desk our name; it answers whether it will hear us."""
        result = self.call("hello", {"client": self.client})
        if result.get("granted") is not True:
            raise DeskError("not-granted", _NOT_GRANTED_FALLBACK)
        return result

    def status(self) -> dict:
        return self.call("desk.status", {"client": self.client})

    def sessions(self) -> list:
        """The open sessions, as a list (a malformed answer reads as empty)."""
        result = self.call("desk.sessions", {"client": self.client})
        sessions = result.get("sessions")
        if not isinstance(sessions, list):
            return []
        return [s for s in sessions if isinstance(s, dict)]

    def read(self, session_id, lines=None) -> dict:
        """The tail of one session, by the id ``sessions()`` reported."""
        wanted = str(session_id or "").strip()
        if not wanted:
            raise DeskError("error", "No session was named to read.")
        params = {"client": self.client, "id": wanted}
        if lines:
            try:
                params["lines"] = max(1, min(int(lines), MAX_LINES))
            except (TypeError, ValueError):
                raise DeskError("error",
                                f"{lines!r} is not a number of lines.") from None
        return self.call("session.read", params)


def _as_paths(paths) -> list:
    """`paths` as a list of absolute discovery-file paths, or a loud refusal.

    Measured mistake: `Desk("handsoff")` — the CLIENT name passed where a path
    was wanted — became one relative path, matched no file, and answered
    "Quantum Space isn't running." about a desk that WAS running, with a file on
    disk and a live pid. A wrong state is worse than a crash here: it is
    indistinguishable from the truth. So a path that cannot be one (relative,
    empty, or not a path at all) is refused as the programming error it is.
    """
    if paths is None:
        return discovery_paths()
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    try:
        items = list(paths)
    except TypeError:
        raise TypeError(
            f"Desk() wants discovery-file paths, not {type(paths).__name__} — "
            f"pass paths (or nothing, for the profile directories) and the "
            f"client name as client=.") from None
    if not items:
        raise ValueError(
            "Desk() was given an empty path list: there would be nothing to "
            "try, and every call would answer that the desk is not running. "
            "Pass no paths to search the profile directories.")
    out = []
    for item in items:
        path = Path(item)
        if not path.is_absolute():
            raise ValueError(
                f"Desk() wants absolute discovery-file paths; got {str(item)!r}, "
                f"which cannot be one. To name the client, use "
                f"Desk(client={str(item)!r}) — a client name is not a path.")
        out.append(path)
    return out


def connect(paths=None, *, client: str = CLIENT, timeout: float = TIMEOUT_S,
            config_home=None) -> Desk:
    """A Desk for `paths`, defaulting to the two profile directories."""
    return Desk(paths if paths is not None else discovery_paths(config_home),
                client=client, timeout=timeout)


def session_index(sessions) -> str:
    """One line naming every session, for a model that has to hand an id back.

    `describe_sessions` is what a person hears and deliberately does not read
    ids aloud; this is the same list for the model, one entry per session, so
    reading a tile never depends on guessing which id is which.
    """
    rows = []
    for session in sessions:
        who = _session_who(session)
        where = _folder(str(session.get("cwd") or ""))
        row = f"{session.get('id')}: {who}"
        if where:
            row += f" in {where}"
        if session.get("readable") is False:
            row += " (not readable)"
        rows.append(row)
    return "sessions — " + "; ".join(rows) if rows else ""


def session_names(sessions) -> list:
    """What the desk calls each session, for an answer that lists them."""
    return [_session_who(s) for s in sessions]


def _session_who(session) -> str:
    """The shortest true name for one session: agent, then name, then kind.

    Bounded to :data:`MAX_NAME_CHARS`, because the desk's `name` for a tile is
    sometimes a whole command line (a `run` tile's name IS its command, measured
    at 171 characters live) and this string gets read aloud.
    """
    for key in ("agent", "name", "kind", "id"):
        value = str(session.get(key) or "").strip()
        if value:
            return _bounded_name(value)
    return "session"


def _bounded_name(text: str, limit: int = MAX_NAME_CHARS) -> str:
    """A name cut to something sayable, with the cut shown rather than hidden."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _folder(path: str) -> str:
    """The folder part of a cwd, because a path is not a thing to say aloud."""
    text = str(path or "").strip().rstrip("/")
    return Path(text).name if text else ""


def resolve_session(sessions, needle) -> dict:
    """The one session `needle` names, or a DeskError.

    Resolution goes through the desk's own list rather than being guessed at:
    an exact id first (that is what an id is for), then an exact name, then a
    name the user only half-remembered — and an AMBIGUOUS half-match is refused
    with the choices named rather than settled by picking the first, because
    reading the wrong agent's screen and describing it would be worse than
    asking.
    """
    wanted = str(needle or "").strip()
    if not wanted:
        raise DeskError("error", "No session was named to read.")
    low = wanted.lower()
    for session in sessions:
        if str(session.get("id") or "") == wanted:
            return session
    exact = [s for s in sessions if str(s.get("name") or "").lower() == low]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise _ambiguous(wanted, exact)
    partial = [s for s in sessions
               if low in str(s.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise _ambiguous(wanted, partial)
    raise DeskError("session-gone",
                    f"{_SESSION_GONE} Open now: "
                    f"{', '.join(session_names(sessions)) or 'nothing'}.",
                    reason="session-not-found")


def _ambiguous(needle: str, matches) -> DeskError:
    return DeskError("error",
                     f"'{needle}' matches more than one session "
                     f"({', '.join(session_names(matches))}) — say which one.")


# -- shaping an answer for a voice --------------------------------------------
def _number_word(n: int) -> str:
    words = ("zero", "one", "two", "three", "four", "five", "six", "seven",
             "eight", "nine", "ten", "eleven", "twelve")
    return words[n] if 0 <= n < len(words) else str(n)


def _join(parts) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _plural(noun: str) -> str:
    return noun if noun.endswith("s") else noun + "s"


def _run_label(run) -> str:
    """One run of sessions that share a `who`, as a phrase a person would say.

    A single session is named with its folder ("Claude in the api folder").
    Several of the same kind fold into a count ("two shells") — the folder is
    kept only when every one of them is in the SAME folder, because "two shells
    in the u folder" is true and "two shells in the u folder" for two different
    folders would not be. Unreadable sessions are called out, since "there are
    three" and "there are three you can read" are different answers.
    """
    who = _session_who(run[0])
    folders = {_folder(str(s.get("cwd") or "")) for s in run}
    where = folders.pop() if len(folders) == 1 else ""
    blind = sum(1 for s in run if s.get("readable") is False)
    if len(run) == 1:
        label = f"{who} in the {where} folder" if where else who
    else:
        label = f"{_number_word(len(run))} {_plural(who)}"
        if where:
            label += f" in the {where} folder"
    if blind == len(run):
        label += " (not readable)"
    elif blind:
        label += f" ({_number_word(blind)} not readable)"
    return label


def describe_sessions(sessions, limit: int = 6) -> str:
    """A spoken summary: 'three sessions: Claude in the api folder, two shells.'

    Sessions are grouped in RUNS of the same kind, in the order the desk gave
    them, so the sentence keeps reading in desk order while repeated kinds still
    fold into a count — which is how a person says it. A desk with more than
    `limit` sessions is summarised rather than read out in full: twelve tiles is
    a list nobody wants to hear.
    """
    if not sessions:
        return "Quantum Space has no sessions open."
    shown = sessions[:limit]
    runs, parts = [], []
    for session in shown:
        if runs and _session_who(runs[-1][0]) == _session_who(session):
            runs[-1].append(session)
        else:
            runs.append([session])
    for run in runs:
        parts.append(_run_label(run))
    total = len(sessions)
    tail = ""
    if total > limit:
        tail = f", and {_number_word(total - limit)} more"
    noun = "session" if total == 1 else "sessions"
    return f"{_number_word(total)} {noun}: {_join(parts)}{tail}."


def describe_status(status, sessions) -> str:
    """One spoken paragraph about the desk that answered.

    Only fields whose shape this client actually KNOWS are spoken — `windows`
    when it is an int, and `control` in either shape the desk really sends: a
    bare bool, or its own object ``{"enabled": true, "clients": ["handsoff"]}``.
    Anything else is left out, because a client that guesses at another app's
    nested shapes invents things the user then hears as fact.

    The object shape is not a guess: it is what a live desk answers (measured
    2026-09-21 — see ``APP_DIRS`` for what the same run found), and the boolean
    alone was a fake-desk simplification that made this function look like it
    handled `control` when against the real thing it fell silent.
    """
    folder = _folder(str(status.get("folder") or ""))
    version = str(status.get("version") or "").strip()
    lead = "Quantum Space is running"
    if version:
        lead += f" (v{version})"
    if folder:
        lead += f" with the {folder} folder open"
    extra = []
    control = status.get("control")
    enabled, grants = None, []
    if isinstance(control, bool):
        enabled = control
    elif isinstance(control, dict) and isinstance(control.get("enabled"), bool):
        enabled = bool(control["enabled"])
        clients = control.get("clients")
        if isinstance(clients, list):
            # Names, so "control is on for handsoff" is about a consent a
            # person gave rather than a light being green.
            grants = [str(c) for c in clients
                      if isinstance(c, str) and c.strip()]
    if enabled is not None:
        line = f"control is {'on' if enabled else 'off'}"
        if grants:
            line += f" for {_join(grants)}"
        extra.append(line)
    windows = status.get("windows")
    if isinstance(windows, int) and not isinstance(windows, bool):
        extra.append(f"{windows} window{'' if windows == 1 else 's'}")
    text = lead + ". " + describe_sessions(sessions)
    if extra:
        text += " " + _join(extra).capitalize() + "."
    return text


def describe_read(result, max_chars: int = MAX_TEXT_CHARS) -> str:
    """One read as text for the model: whose screen, and the tail of it.

    The TAIL is what is kept when the answer is longer than one reply should
    carry — "what is it doing" is a question about now — and both our own cut
    and the desk's own ``truncated`` flag are stated, so a short answer is never
    mistaken for a short session.
    """
    who = _session_who(result)
    where = _folder(str(result.get("cwd") or ""))
    label = f"{who} in the {where} folder" if where else who
    text = str(result.get("text") or "").rstrip()
    if not text:
        return f"{label} has nothing readable on screen right now."
    notes = []
    if "\n" not in text and len(text) > 200:
        # A full-screen TUI positions with cursor moves, so the desk's stripping
        # leaves a SCREEN with no line breaks in it — measured live (2 173 chars,
        # 362 box glyphs, zero newlines, and the desk reporting `lines: 1`
        # `truncated: false` while what it handed over was a whole screen). Said
        # plainly, because otherwise a screen reads as a one-line session.
        notes.append("[the desk returned this as ONE line: a full-screen terminal "
                     "UI has no line breaks in it, so the desk's own line count "
                     "describes nothing here]")
    if len(text) > max_chars:
        text = text[-max_chars:]
        notes.append(f"[older output trimmed to the last {max_chars} characters]")
    if result.get("truncated"):
        notes.append("[Quantum Space says this tail is truncated]")
    body = f"{label} — the tail of that session:\n{text}"
    return body + ("\n" + "\n".join(notes) if notes else "")


def describe_state(error: DeskError) -> str:
    """Which state the link is in, with the desk's own words where there are any.

    Four of these are the sentences a user will actually hear when they ask
    about the desk, and they are distinct on purpose: not running, the desk
    refusing on its own rules, and a session that has vanished are three
    different problems with three different fixes.
    """
    state = error.state
    if state == "not-running":
        return f"not running — {error.message}"
    if state == "not-granted":
        return f"not granted — the desk says: {error.message}"
    if state == "session-gone":
        return f"that session is gone — {error.message}"
    if state == "untrusted":
        return f"refusing its control file — {error.message}"
    if state == "protocol":
        return f"protocol mismatch — {error.message}"
    if state == "unreachable":
        return f"not answering — {error.message}"
    if state == "auth":
        return f"token refused — {error.message}"
    if state == "no-output":
        return f"nothing to read — {error.message}"
    return f"{state} — {error.message}"
