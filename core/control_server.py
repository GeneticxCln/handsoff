"""The control socket: a unix-socket remote control for the running bubble.

`niri` keybinds, `handsoff.py --ptt <verb>` and the settings app all drive the
bubble through this one listener. It was a 500-line class inside the app module;
it lives here so that the part of the program that faces OTHER PROCESSES — the
capability token, the peer-uid check, the request-size and read-time bounds, the
re-bind of a path that was removed under a live bubble — can be read, reviewed
and tested as one file.

Nothing here is Qt and nothing imports the app. Everything the server needs from
the host (the socket and token paths, the verb tables, the doctor, the model
name, the logger) is read LIVE from the `dependencies` object the host passes to
the constructor — `handsoff.ControlServer` is a thin subclass that hands it the
app's own globals — so a test that reassigns `H.CONTROL_SOCK`, or an embedder
that runs a second app in the same process, sees exactly what it did when this
class was part of the app module. It is per INSTANCE on purpose: a process-wide
"current host" is the shape that made whichever app loaded last own every core
module (see tests/conftest.py, `_di_host_is_restored`).
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# Absolute, not relative: the shared origin-checked loader exec's a bare spec
# (`__package__ == ""`), so a relative import cannot resolve (see core/assistant).
from core import unix_address
from core.registry import BoundedRegistry


class ControlServer:
    """Unix-socket remote control so niri keybinds can drive the bubble.

    The server owns its socket and stop event. This matters on a clean Qt
    shutdown: a daemon thread that outlives the widget can otherwise keep a
    stale socket inode around until the process is killed, confusing the next
    startup and making a failed launch look like a live bubble.
    """

    # The accept loop is a SINGLETON slot in the shared registry, not a plain
    # attribute. "Is one already running?" then "start one" was the last
    # hand-rolled check-then-act in the project: two overlapping start() calls
    # both saw no live thread and both spawned an accept loop, so one path had
    # two servers — the loser's bind replacing the winner's socket, leaving a
    # loop accepting on an inode no client could reach. The registry hands the
    # slot out under one lock, with a reclaim predicate for a loop that DIED,
    # so "the old one is gone" and "may I have its slot?" are one decision.
    ACCEPT_SLOT = "accept"

    def __init__(self, assistant, dependencies) -> None:
        self._assistant = assistant
        self._d = dependencies
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._runs = BoundedRegistry("control", 1)
        # The capability token this process will require for every verb that
        # changes state. Decided in `_serve`, before the socket exists, so a
        # client can never reach a listener whose token is still undecided.
        self._token: str | None = None
        # Latch for the orphaned-path reports so a path that cannot be
        # reclaimed cannot fill the journal with one line per idle second.
        self._orphan_reported = False
        # (st_dev, st_ino) of the path we bound, captured from lstat right
        # after bind. NOTE: this cannot be derived from the socket fd —
        # os.fstat() on an AF_UNIX fd returns the *socket object's* inode,
        # never the filesystem entry's — so it is recorded, not computed.
        self._sock_ident: tuple[int, int] | None = None
        # The path string this server bound. CONTROL_SOCK is a module global
        # that an embedder or a test can reassign; repair work must only ever
        # touch OUR path, or a server left running from earlier would create a
        # socket at whatever path the global now names (measured: it did).
        self._bound_path: str | None = None
        # The slow diagnostic workers (health/doctor). A timed-out worker
        # cannot be cancelled — it is blocked in an Ollama or nvidia-smi call —
        # so without a cap the accept loop would start a fresh one per request
        # and pile them up against a wedged backend. One slot, handed out by
        # the registry: "is the previous worker alive?" and "may I start one?"
        # used to be two reads with a thread spawn between them.
        self._diag = BoundedRegistry("diagnostic", 1)

    @property
    def _log(self):
        """The host's logger, read live: a test that swaps `H.log` for a capture
        sees this server's lines exactly as it did when the class lived in the
        app module."""
        return self._d.log

    def _diagnostic_call(self, fn, timeout_s: float):
        """Run slow diagnostics off the accept thread: the accept loop must stay
        responsive (1s accept timeout) even when Ollama/nvidia-smi wedge.

        At most one such worker exists at a time. A timed-out worker keeps
        running (it is blocked in the backend call; nothing here can cancel
        it), so spawning another per request is how repeated `health`/`doctor`
        against a wedged Ollama piles up threads that never return. A second
        request is refused fast and by name instead of adding to the pile —
        through the same registry that owns every other cap, so the refusal is
        counted, logged in the shared wording, and durable.
        """
        slot = self._diag.reserve("diagnostic",
                                  reclaim=lambda t: not t.is_alive())
        if slot is None:
            detail = "previous diagnostic still running"
            self._log.warning("cap refusal: %s", self._diag.refusal_line(detail))
            try:
                self._d._record_cap_refusal({**(self._diag.refusal_report() or {}),
                                     "detail": detail})
            except Exception:
                self._log.exception("cap-refusal recorder failed")
            # ...and say it: a refusal the user asked for by pressing the
            # health keybind must not be discoverable only in the journal.
            try:
                self._assistant.announce_cap_refusal(
                    self._diag.refusal_report() or {}, detail)
            except Exception:
                self._log.exception("cap-refusal announcement failed")
            raise TimeoutError(
                "a previous diagnostic is still running "
                "(the backend is not answering)")
        box: dict = {}

        def _run() -> None:
            try:
                box["out"] = fn()
            except Exception as e:  # noqa: BLE001
                box["err"] = e

        # Start INSIDE the reservation and commit after, so a worker that
        # cannot be started gives the slot back instead of occupying it with a
        # thread that does not exist.
        with slot:
            worker = threading.Thread(target=_run, daemon=True,
                                      name="diag-worker")
            worker.start()
            slot.commit(worker)
        worker.join(timeout_s)
        if worker.is_alive():
            raise TimeoutError(f"timed out after {timeout_s:.1f}s")
        if "err" in box:
            raise box["err"]
        return box.get("out")

    @property
    def _thread(self):
        """The accept thread, or ``None``: a view onto the registered slot.

        Kept as a property because the slot's occupant IS the thread — the
        reclaim predicate asks it whether it is still alive — and because
        stop() must never be able to join a thread the registry does not know
        about.
        """
        return self._runs.get(self.ACCEPT_SLOT)

    def start(self) -> None:
        """Start the accept loop, idempotently, through the registry slot.

        A repeated start is the documented no-op it always was: the occupant
        is alive, the reservation is refused, and nothing is spawned. That is
        deliberately NOT reported as a cap refusal worth shouting about —
        nothing the user asked for was turned away — but the registry counts
        it all the same.
        """
        slot = self._runs.reserve(
            self.ACCEPT_SLOT, reclaim=lambda t: not t.is_alive())
        if slot is None:
            self._log.info("control socket already accepting")
            return
        # A dead accept loop the predicate just reclaimed needs no disposal:
        # _serve's finally already closed its socket and removed the path. The
        # stop event is cleared only on the path that actually starts a loop,
        # so a refused start cannot re-arm a server that is shutting down.
        self._stop.clear()
        with slot:
            thread = threading.Thread(target=self._serve, name="control",
                                      daemon=True)
            thread.start()
            slot.commit(thread)

    def stop(self) -> None:
        """Stop the accept loop and remove only our owner-owned socket."""
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        # Free the slot only once the loop is actually gone. A thread that
        # outlived its join budget keeps the slot, so a restart cannot get a
        # second acceptor beside the old one; the reclaim predicate frees it
        # the moment it really dies.
        if thread is not None and not thread.is_alive():
            self._runs.release(self.ACCEPT_SLOT)
        try:
            self._d._remove_stale_control_socket()
        except OSError:
            self._log.exception("could not remove control socket during shutdown")

    def _serve(self) -> None:
        server = None
        try:
            if not self._d._prepare_runtime():
                raise OSError("runtime/config directories or files are not private")
            # Rotate the token BEFORE the socket exists: a client must never
            # find a listener whose capability has not been decided, and a
            # token left by a previous run is replaced rather than reused.
            self._token = self._d._rotate_control_token()
            if self._token is None:
                self._log.error(
                    "control socket: no capability token could be written — "
                    "only the read-only commands (%s) will be accepted",
                    " ".join(sorted(self._d.PTT_READ_ONLY)))
            self._d._remove_stale_control_socket()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with unix_address(self._d.CONTROL_SOCK) as address:
                server.bind(address)
            os.chmod(self._d.CONTROL_SOCK, 0o600)
            server.listen(4)
            server.settimeout(1.0)
            self._bound_path = str(self._d.CONTROL_SOCK)
            self._sock_ident = self._ident_of(self._d.CONTROL_SOCK)
            self._server = server
        except OSError as e:
            self._log.error("control socket unavailable: %s", e)
            if server is not None:
                server.close()
            return
        self._log.info("control socket at %s", self._d.CONTROL_SOCK)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    # The path can be removed underneath a live bubble (a
                    # cleanup script, `rm ~/.local/state/handsoff/*`, an
                    # unmount). The listener keeps accepting on an inode no
                    # client can reach: `--ptt` says "not running", doctor says
                    # "not created yet", and nothing anywhere says otherwise.
                    # One lstat per idle second is the cheapest way to notice
                    # and re-bind instead of degrading in silence.
                    if self._stop.is_set():
                        # Shutdown already removed the path. Re-binding here
                        # would re-create the socket AFTER stop() cleaned up,
                        # leaving behind exactly the stale inode this class
                        # exists to avoid.
                        break
                    if str(self._d.CONTROL_SOCK) != self._bound_path:
                        # CONTROL_SOCK names a path we did not bind: someone
                        # else's socket, someone else's business. Repairing it
                        # would create a control socket on a path this bubble
                        # never owned — and in the test suite a server left
                        # running would then materialise a 0600 socket under a
                        # later test's temporary path.
                        continue
                    state = self._socket_path_state()
                    if state == "gone":
                        rebound = self._rebind(server)
                        if rebound is not None:
                            server = rebound
                    elif state == "foreign" and not self._orphan_reported:
                        # Someone else's socket took our path. Deleting it
                        # could be another live bubble's socket, so say so and
                        # leave it: a loud lie beats a silent theft.
                        self._orphan_reported = True
                        self._log.error(
                            "control socket path %s now belongs to another inode — "
                            "'--ptt' reaches whoever owns it, not this bubble "
                            "(a second handsoff, or a stale path from another run)",
                            self._d.CONTROL_SOCK)
                    continue
                except OSError:
                    if not self._stop.is_set():
                        self._log.exception("control socket accept failed")
                    return
                action = ""            # for the failure reply, if we get that far
                try:
                    conn.settimeout(5.0)
                    # Read the request BEFORE the credential check: closing a
                    # socket that still holds the peer's unread bytes sends
                    # RST, so a refused caller would see a connection reset
                    # instead of the reason. Reading first keeps the refusal
                    # legible (the check still gates every dispatch).
                    # Read the WHOLE request, not the first 1024 bytes. Both
                    # clients half-close after sending, so EOF is the end of
                    # the request; a client that does not (an older build) is
                    # bounded by the 5 s timeout rather than by a truncated
                    # command.
                    chunks: list[bytes] = []
                    total = 0
                    deadline = time.monotonic() + self._d._CONTROL_READ_BUDGET
                    while total < self._d._CONTROL_REQUEST_MAX:
                        if time.monotonic() >= deadline:
                            self._log.warning(
                                "control socket: request from uid %s did not "
                                "finish within %.1fs — reading what arrived",
                                self._d._peer_uid(conn), self._d._CONTROL_READ_BUDGET)
                            break
                        try:
                            # Ask for what is LEFT of the ceiling, not a fixed
                            # chunk: the loop checks BEFORE reading, so a fixed
                            # 64 KiB asked for one more chunk than the bound
                            # allowed and the request could reach the ceiling
                            # plus a full recv. Measured under a loaded full-suite
                            # run, where the kernel delivers a 100 KB request in
                            # pieces: the dispatched argument was 101996 bytes for
                            # a 65536-byte bound, and the test that holds the bound
                            # was right about it.
                            part = conn.recv(min(65536,
                                                 self._d._CONTROL_REQUEST_MAX - total))
                        except (TimeoutError, OSError):
                            break
                        if not part:
                            break               # EOF: the request is complete
                        chunks.append(part)
                        total += len(part)
                    raw = b"".join(chunks).decode("utf-8", "replace").strip()
                    # A client that holds the token sends it as an explicit
                    # first line (`token=<hex>`), a client that does not sends
                    # the bare command. Explicit rather than positional so that
                    # `say` text containing a newline can never be read as a
                    # credential — and so an old client keeps working for every
                    # read-only verb.
                    token_line, sep, rest = raw.partition("\n")
                    supplied = None
                    if sep and token_line.startswith(self._d._CONTROL_TOKEN_PREFIX):
                        supplied = token_line[len(self._d._CONTROL_TOKEN_PREFIX):].strip()
                        raw = rest.strip()
                    # Split the optional argument off BEFORE lowercasing: `say`
                    # carries the text to synthesize, so lowercasing the whole
                    # payload would make the bubble read a different sentence
                    # than the one the user typed.
                    action, _, action_arg = raw.partition(" ")
                    action = action.lower()
                    peer = self._d._peer_uid(conn)
                    if peer is not None and peer != os.getuid():
                        self._log.warning(
                            "control socket: refusing peer uid %s (ours is %s)",
                            peer, os.getuid())
                        conn.sendall(b"error: not permitted\n")
                        continue
                    if action in self._d.PTT_ACTIONS and action not in self._d.PTT_READ_ONLY:
                        # Constant-time compare: the token is not secret from
                        # anyone who can read the state directory, but a
                        # comparison that leaks its prefix by timing is still
                        # free to avoid.
                        if (not self._token or not supplied
                                or not secrets.compare_digest(supplied,
                                                              self._token)):
                            self._log.warning(
                                "control socket: refusing %r from uid %s — "
                                "this command changes state and no valid "
                                "capability token was presented", action, peer)
                            conn.sendall(
                                ("error: '{0}' changes state and needs the "
                                 "control token; read it from {1}. Read-only "
                                 "commands do not need it: {2}.\n").format(
                                     action, self._d.CONTROL_TOKEN,
                                     ", ".join(sorted(self._d.PTT_READ_ONLY))
                                 ).encode("utf-8"))
                            continue

                    def _with_timeout(fn, timeout_s: float):
                        """Slow diagnostics, off the accept thread (see
                        ControlServer._diagnostic_call)."""
                        return self._diagnostic_call(fn, timeout_s)

                    if action in self._d.PTT_ACTIONS:
                        if action == "status":
                            reply = (f"state={self._assistant.state} "
                                     f"handsfree={'on' if self._assistant._handsfree else 'off'} "
                                     f"model={self._d.OLLAMA_MODEL}")
                        elif action == "health":
                            try:
                                snap = _with_timeout(
                                    self._assistant.mic_health, 4.0)
                                reply = json.dumps(snap, ensure_ascii=False)
                            except Exception as e:  # noqa: BLE001
                                self._log.exception("health snapshot failed")
                                # Name the cause in the reply too: the commonest
                                # one is "a previous diagnostic is still running",
                                # and "see log" sends the user digging for a
                                # sentence we already have.
                                reply = f"error: health snapshot failed: {e}"
                        elif action == "level":
                            # deliberately NOT wrapped in _with_timeout: it reads
                            # three scalars and the Voice meter polls it ~20x/s,
                            # so spawning a worker thread per poll would be pure
                            # overhead. `doctor`/`health` are the slow ones.
                            reply = json.dumps(self._assistant.level_snapshot(),
                                               ensure_ascii=False)
                        elif action == "clear-history":
                            # Settings asks for this when the model changes: the
                            # bubble OWNS the history in memory and rewrites the
                            # whole file on its next save, so truncating the file
                            # from another process would be silently resurrected.
                            reply = (f"ok: cleared {self._assistant.clear_history()} "
                                     f"history message(s)")
                        elif action == "doctor":
                            try:
                                reply = _with_timeout(self._d.run_doctor, 4.5)
                            except Exception as e:  # noqa: BLE001
                                self._log.exception("doctor report failed")
                                reply = f"error: doctor report failed: {e}"
                        elif action == "say":
                            reply = self._assistant.say_preview(action_arg)
                        elif action == "preview-pack":
                            # The folder is a path the CALLER chose, so the
                            # bubble validates it itself (the same reading an
                            # install does) instead of drawing whatever it is
                            # pointed at.
                            reply = self._assistant.set_pack_preview(action_arg)
                        elif action == "preview-clear":
                            reply = self._assistant.clear_pack_preview()
                        elif action in self._d.PTT_CLI_ONLY:
                            # Refused BY NAME, with the path that works. The
                            # generic arm below would answer "ok" and do
                            # nothing, because _on_command has no arm for
                            # these — they run locally, over scratch windows
                            # and the ledger, so they work even when the
                            # bubble is dead and a socket is not there to ask.
                            reply = (f"error: '{action}' is a command-line "
                                     f"command, not a socket one — it runs "
                                     f"locally so it works when the bubble is "
                                     f"down. Use: python3 {Path(self._d.__file__).name} "
                                     f"--ptt {action}")
                        elif action == "settings":
                            if self._d.SETTINGS_APP.exists():
                                subprocess.Popen(
                                    [sys.executable, str(self._d.SETTINGS_APP)], start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                )
                                reply = "ok: settings window launched"
                            else:
                                reply = f"ERROR: settings app missing at {self._d.SETTINGS_APP}"
                        else:
                            # The last arm forwards to the assistant and acks
                            # OPTIMISTICALLY, because a Qt signal cannot report
                            # back what the receiver did with it. That is only
                            # honest for a verb the receiver actually handles,
                            # so the two it does not are refused above rather
                            # than falling through to a bare "ok" for work that
                            # never happened.
                            self._assistant.sigCommand.emit(action)
                            reply = f"ok: {action}"
                    else:
                        reply = (f"error: unknown command '{action}'. "
                                 f"commands: {' '.join(sorted(self._d.PTT_ACTIONS - self._d.PTT_CLI_ONLY))}")
                    conn.sendall((reply + "\n").encode("utf-8"))
                except OSError:
                    pass
                except Exception as exc:  # noqa: BLE001 -- see below
                    # ONE request must cost that request and not the server.
                    # Only OSError was caught here, so any other exception from
                    # a verb (a bug, a value nobody expected) left this `while`
                    # and ended the accept thread: the bubble carried on with no
                    # control socket — `--ptt` says "not running", the keybinds
                    # do nothing, settings cannot reach it — which is the zombie
                    # this project's health machinery exists to prevent. Named
                    # in the journal with its traceback, and the client is told
                    # instead of being left reading a closed socket.
                    self._log.exception("control socket: %r failed", action)
                    try:
                        conn.sendall(
                            (f"error: {action or 'request'} failed "
                             f"({type(exc).__name__}) — see the journal\n"
                             ).encode("utf-8"))
                    except OSError:
                        pass
                finally:
                    conn.close()
        finally:
            if self._server is server:
                self._server = None
            try:
                server.close()
            except OSError:
                pass
            try:
                self._d._remove_stale_control_socket()
            except OSError:
                self._log.exception("could not remove control socket")


    @staticmethod
    def _ident_of(path: Path) -> "tuple[int, int] | None":
        try:
            info = path.lstat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def _socket_path_state(self) -> str:
        """'ours' | 'gone' | 'foreign' for the path we bound.

        'ours' also covers an unreadable path: a transient lstat failure
        (permission on a parent directory, an idle-mounted home) must not make
        the bubble replace a socket it cannot even inspect.
        """
        try:
            info = self._d.CONTROL_SOCK.lstat()
        except FileNotFoundError:
            return "gone"
        except OSError:
            return "ours"
        if self._sock_ident is None:
            return "ours"
        return "ours" if (info.st_dev, info.st_ino) == self._sock_ident else "foreign"

    def _rebind(self, server: socket.socket) -> "socket.socket | None":
        """Re-create the control socket after its path disappeared.

        Returns the new listening socket, or None when the path cannot be
        reclaimed safely (someone else owns it) — the caller then keeps the old
        one and retries after the next idle second, so a foreign inode is never
        unlinked on the strength of a guess.
        """
        if self._stop.is_set():
            return None      # shutdown owns the path from here; do not resurrect it
        if not self._bound_path or str(self._d.CONTROL_SOCK) != self._bound_path:
            return None      # only ever repair the path this server bound
        try:
            # Same guard startup uses: refuses a symlink, a foreign owner, or a
            # non-socket at that path rather than unlinking it.
            self._d._remove_stale_control_socket()
            fresh = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with unix_address(self._d.CONTROL_SOCK) as address:
                fresh.bind(address)
            os.chmod(self._d.CONTROL_SOCK, 0o600)
            fresh.listen(4)
            fresh.settimeout(1.0)
        except OSError as e:
            if not self._orphan_reported:
                self._orphan_reported = True
                self._log.error(
                    "control socket path is gone and could not be re-bound (%s) — "
                    "'--ptt' cannot reach this bubble until it restarts", e)
            return None
        self._log.warning(
            "control socket path was removed while running — re-bound at %s "
            "(clients that failed in the meantime should retry)", self._d.CONTROL_SOCK)
        self._sock_ident = self._ident_of(self._d.CONTROL_SOCK)
        self._orphan_reported = False
        try:
            server.close()
        except OSError:
            pass
        self._server = fresh
        return fresh
