#!/usr/bin/env python
# handsoff-self-marker: this line must be preserved across self-edits
"""
handsoff — a self-modifying voice assistant bubble for Arch Linux + niri (Wayland).

A small frameless translucent bubble floats on your desktop. Hold it with the
left mouse button and speak; release to send. Speech is transcribed locally
(faster-whisper), answered by a local Ollama model, and spoken back with Piper.

States:  idle (blue, breathing) · listening (red, grows with your voice RMS) ·
         thinking (orange, wobbling) · speaking (green, pulsing).
Right-click the bubble for a menu (restart / quit). Drag it to move it.

Layout of this file: config → system prompt → Ollama client → audio (STT/TTS) →
tools → Assistant state machine → Bubble UI → main(). Run:

    python ~/.local/bin/handsoff.py

Settings app:
    python ~/.local/bin/handsoff-settings.py   (also: right-click the bubble → Settings…)
    Writes ~/.config/handsoff/settings.json: microphone, Ollama host/model,
    whisper size, TTS voice reference + speech rate/volume, tool permissions, bubble
    size and colours, and the niri autostart entry.
    Precedence: built-in defaults <- environment <- settings.json.

Environment overrides (apply only where settings.json has no value):
    HANDSOFF_MODEL, OLLAMA_HOST, HANDSOFF_NUM_CTX, HANDSOFF_WHISPER, HANDSOFF_VOICE,
    HANDSOFF_KEEP_ALIVE

Files:
    ~/.config/handsoff/settings.json    settings written by the settings app
    ~/.config/handsoff/whisper-model/   faster-whisper model cache
    ~/.config/handsoff/chatterbox/      speech weights + an optional reference clip
    ~/.config/handsoff/history.json     conversation memory (survives restarts)
    ~/.local/state/handsoff/reminders.json   pending reminders (survive restarts)
    ~/.local/bin/handsoff-restart       kills + relaunches this program
    ~/.local/bin/handsoff-settings.py   the settings GUI
    ~/.local/state/handsoff/            logs, lock, pending-restart note
    ~/.local/state/handsoff/decisions.jsonl  one line per tool-policy decision
"""
from __future__ import annotations

import base64
import faulthandler
import fcntl
import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import signal
import queue
import random
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import inspect
from collections import deque
import datetime
import html as _html_mod
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path


def _missing(module: str, pip_name: str, pacman: str | None = None) -> None:
    hint = f"pacman -S {pacman}" if pacman else ""
    sys.stderr.write(
        f"handsoff: missing dependency '{module}'. Install it with "
        f"`{hint}` or `pip install --user --break-system-packages {pip_name}`, "
        "or run the installer: install.sh\n"
    )
    sys.exit(1)


try:
    import numpy as np
except ImportError:
    _missing("numpy", "numpy", "python-numpy")

try:
    import sounddevice as sd
except ImportError:
    _missing("sounddevice", "sounddevice", "python-sounddevice")

try:
    from PySide6.QtCore import (
        QElapsedTimer,
        QObject,
        QPointF,
        QRect,
        Qt,
        QTimer,
        Signal,
    )
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QConicalGradient,
        QGuiApplication,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPainterPathStroker,
        QPen,
        QPolygonF,
        QRadialGradient,
        QRegion,
    )
    from PySide6.QtWidgets import QApplication, QMenu, QWidget
except ImportError:
    _missing("PySide6", "pyside6", "python-pyside6")

# --------------------------------------------------------------------------- config

VERSION = "1.0.0"
APP_NAME = "handsoff"
SHUTDOWN_JOIN_TIMEOUT = 0.25
HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "handsoff"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / APP_NAME
WHISPER_MODEL_DIR = CONFIG_DIR / "whisper-model"
TTS_ENGINE = "chatterbox-turbo"
SELF_PATH = Path(__file__).resolve()  # the AI edits this file to modify itself
RESTART_SCRIPT = HOME / ".local/bin" / "handsoff-restart"
SETTINGS_APP = HOME / ".local/bin" / "handsoff-settings.py"
if not SETTINGS_APP.exists():
    _sibling = Path(__file__).resolve().parent / "handsoff-settings.py"
    if _sibling.exists():
        SETTINGS_APP = _sibling
HISTORY_FILE = CONFIG_DIR / "history.json"
MEMORY_FILE = CONFIG_DIR / "memory.json"   # durable facts about the user
CRASH_LOG = STATE_DIR / "crash.log"
PENDING_FILE = STATE_DIR / "pending-restart.json"
LOCK_FILE = STATE_DIR / "handsoff.lock"
LOG_FILE = STATE_DIR / "handsoff.log"
CONTROL_SOCK = STATE_DIR / "control.sock"
MIC_EVENTS_FILE = STATE_DIR / "mic-health.json"   # mic transitions + last briefing
MIC_EVENTS_MAX = 200                              # hard cap on recorded transitions
_MIC_EVENTS_LOCK = threading.Lock()   # both writers are read-modify-write
# Cap refusals: the bubble turning real work away because a bounded registry
# was full. Kept on disk as well as in the journal, so `--ptt doctor` can still
# report it once the process that refused it is gone — the point is that a
# refusal is never only in the model's reply.
CAP_EVENTS_FILE = STATE_DIR / "cap-refusals.json"
CAP_EVENTS_MAX = 50                   # bounded: a refusal storm must not grow state
_CAP_EVENTS_LOCK = threading.Lock()
# A refusal is also SPOKEN, because the journal and the durable record are both
# things the user has to go and read. Rate-limited for the same reason the
# reader has a per-app cooldown: the refusal path is retried by nature (a model
# re-calling the same tool, a keybind being hammered) and an audio loop is worse
# than the invisibility it replaces.
CAP_ANNOUNCE_COOLDOWN = 60.0
CAP_ANNOUNCE_MAX = 8                  # bounded: a registry name cannot grow it
_CAP_EVENT_FIELDS = ("registry", "cap", "held", "reserved", "occupants",
                     "at", "detail")
_CAP_LABELS = {"job": "background-job", "watch-file": "file-watcher",
               "watch-process": "process-watcher",
               "diagnostic": "diagnostic-worker"}
SELF_MARKER = "# handsoff-self-marker: this line must be preserved across self-edits"

# Support-module loading: ONE shared order (beside-this-file -> ~/.local/bin
# -> origin-checked plain import) and ONE origin rule, owned by
# core.load_module. This bootstrap only loads the `core` package itself
# (same rule, same order); settings_schema/hardware then come through the
# shared loader — never a bare import a foreign sys.path entry could
# satisfy, and never a half-initialized sys.modules entry left behind.
def _support_origin_ok(mod: object) -> bool:
    """Same origin rule as core._origin_ok for the pre-core bootstrap: only
    a module living beside this file (repo root / deployed dir, incl. its
    core/ subdir) or in ~/.local/bin counts as ours."""
    try:
        parent = Path(getattr(mod, "__file__", "") or "").resolve().parent
    except OSError:
        return False
    try:
        here = Path(__file__).resolve().parent
    except OSError:
        here = Path(__file__).parent
    roots = [here, here / "core", HOME / ".local" / "bin"]
    resolved = set()
    for root in roots:
        try:
            resolved.add(root.resolve())
        except OSError:
            resolved.add(root)
    return parent in resolved


def _load_core_package():
    """Import our `core` package (same-origin) or spec-load it beside this
    file / from ~/.local/bin. ONE shared order everywhere: beside-this-file
    first, then the installed copy, then a plain import — all under ONE
    origin rule (this file's dir incl. its core/ subdir, or ~/.local/bin),
    so a foreign module planted in sys.modules or on sys.path can never
    satisfy us. The `core` entry in sys.modules is only filled when absent
    or same-origin, and NEVER swapped under live core.* submodules (that
    would orphan them); a failed exec restores whatever was there (no
    half-initialized squat). A real package spec is required so the
    relative `from . import load_module` inside core.settings resolves."""
    prev = sys.modules.get("core")
    if prev is not None and _support_origin_ok(prev):
        try:
            import core.settings as _cs
            return _cs
        except ImportError:
            pass
    import importlib.util as _ilu
    here = Path(__file__).resolve().parent
    last_err: Exception | None = None
    cands = [here / "core" / "__init__.py",
             HOME / ".local" / "bin" / "core" / "__init__.py"]
    seen: set[str] = set()
    for init in cands:
        try:
            key = str(init.resolve())
        except OSError:
            key = str(init)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not init.is_file():
                continue
        except OSError:
            continue
        if prev is not None and not _support_origin_ok(prev):
            live = [k for k in sys.modules
                    if k == "core" or k.startswith("core.")]
            if live:
                raise ImportError(
                    "handsoff: refusing to swap a foreign 'core' module "
                    f"({getattr(prev, '__file__', '?')}) under live "
                    f"submodules {live}")
        spec = _ilu.spec_from_file_location(
            "core", init, submodule_search_locations=[str(init.parent)])
        if spec is None or spec.loader is None:
            continue
        pkg = _ilu.module_from_spec(spec)
        sys.modules["core"] = pkg
        try:
            spec.loader.exec_module(pkg)
        except Exception as e:
            last_err = e
            if prev is None:
                sys.modules.pop("core", None)
            else:
                sys.modules["core"] = prev
            continue
        import core.settings as _cs2
        return _cs2
    # last resort: a plain import, accepted only when same-origin
    try:
        import core as _cand
        if _support_origin_ok(_cand):
            import core.settings as _cs3
            return _cs3
    except ImportError as e:
        last_err = e
    if last_err is not None:
        raise last_err
    raise ImportError("handsoff: cannot load the core package beside this file")


_core_settings = _load_core_package()
from core import load_module as _load_module
from core import registry as _core_registry
from core import APP_MODULE_NAME as _canonical_app_name
from core import claim_app_instance as _claim_app_instance


# -------------------------------------------------------------------- identity
# ONE canonical name, claimed by the app ITSELF, and the name is NOT this file's
# business to invent: `core.APP_MODULE_NAME` owns it so the settings app, the
# test harness and any embedder compare against the same string.
#
# Every loader used to give the module a name of its own — "handsoff_core" and a
# bare "handsoff" alias in the tests, "handsoff_core" in the settings app,
# "handsoff_core_gui" in the offscreen GUI driver, "handsoff_no_audio" in the
# hardening driver — so "is the app already loaded in this process?" had no
# answer anyone could ask, and each of those paths could exec a SECOND app: its
# own CONFIG_DIR/STATE_DIR/SETTINGS, its own model mirrors, and a module body
# that calls `core.audio.configure(...)` below, repointing the SHARED core.audio
# at the copy that ran last.
#
# The claim happens HERE: as soon as `core` is importable, and BEFORE anything
# this body can do to shared state (the earliest such call is
# `_audio.configure(...)` far below). A second copy is therefore refused before
# it can do damage rather than after it.
def _claim_app_name() -> str:
    """Register this running module under the one canonical name, or refuse.

    Two refusals, for the two ways two apps could otherwise run silently:

    * **Unnamed.** `module_from_spec(...)` + `exec_module(...)` without a
      `sys.modules` entry executes the app into a namespace nothing can see,
      which is indistinguishable from a duplicate — so the loader must name it
      first. `core.load_app_module` is that implementation, and it is the one
      every loader in this tree uses.
    * **Second copy.** The canonical name already holds a DIFFERENT live module.
      That is a second app; it is refused here, by name, instead of being
      discovered later as a repointed `core.audio` or a diverged SETTINGS.
    """
    me = sys.modules.get(__name__)
    if me is None or getattr(me, "__dict__", None) is not globals():
        raise ImportError(
            "handsoff: executing without a sys.modules registration "
            f"({__name__!r}) — register the module under its spec name BEFORE "
            "executing it (core.load_app_module does), because an unnamed load "
            "cannot be told apart from a second copy of the app")
    # Two records of one fact: the out-of-band instance in `core` (which
    # survives a test popping the registration) and the canonical name in
    # `sys.modules` (which is what every loader looks up). Either one being a
    # different module is a second app, and this is where it is refused.
    _claim_app_instance(me)
    holder = sys.modules.get(_canonical_app_name)
    if holder is not None and holder is not me:
        raise ImportError(
            "handsoff: refusing to run a SECOND copy of the app in this "
            f"process — {_canonical_app_name} already holds "
            f"{getattr(holder, '__file__', '?')}; reuse it (core.app_module()) "
            "instead of loading another")
    sys.modules[_canonical_app_name] = me
    return _canonical_app_name


#: The name this module is registered under (same string as
#: `core.APP_MODULE_NAME`, published here for callers that reach the app
#: through `H.*`).
APP_MODULE_NAME = _claim_app_name()

# Admission control for every bounded registry and every expiring offer lives in
# core.registry; the app reaches it through the handle above (`_core_registry`),
# not by re-exporting its classes into this namespace.

# Phase 4c: core.tools owns the extracted runtime; this host supplies the
# existing globals and callbacks so historical monkeypatch seams stay live.

# Text filtering for a bundle whose core/ is not importable. Defined at MODULE
# level, not inside the legacy class, so it is a single implementation the tests
# can compare against core.brain's — a fallback that drifts from the real one is
# exactly how a legitimate "<3" reply ended up being dropped from speech.
_LEAKED_MARKUP_RE = re.compile(
    r"^\s*(?:</?(?:think|tool_calls?|im_start|im_end)\b|<\|)", re.IGNORECASE)


def _fallback_is_leaked_markup(sentence: str) -> bool:
    return bool(_LEAKED_MARKUP_RE.match(sentence or ""))


def _fallback_strip_thinking(text: str) -> str:
    value = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    # an UNCLOSED block must go too, or the reasoning is spoken aloud
    value = re.sub(r"<think>.*\Z", "", value, flags=re.DOTALL)
    return re.sub(r"^\s*\[TOOL_CALLS\][^\n]*(?:\n|$)", "", value,
                  flags=re.MULTILINE).strip()


try:
    _brain = _load_module("brain")
except ImportError:
    # Compatibility with already-installed bundles made before core/brain.py.
    class _LegacyTurnStream:
        def __init__(self, generation, cancel, sentence_q):
            self.generation, self.cancel, self.sentence_q = generation, cancel, sentence_q
            self.result = None
            self.done = threading.Event()

    class _LegacyBrain:
        TurnStream = _LegacyTurnStream
        strip_thinking = staticmethod(_fallback_strip_thinking)
        is_leaked_markup = staticmethod(_fallback_is_leaked_markup)

        @staticmethod
        def _read_http_error(error):
            try:
                return str(json.loads(error.read().decode("utf-8")).get("error", ""))
            except Exception:
                return str(error.reason)

        _error = _brain._read_http_error

        @staticmethod
        def ollama_available(*, base, guard, urlopen):
            try:
                guard()
                with urlopen(base + "/api/tags", timeout=3) as response:
                    json.loads(response.read().decode("utf-8"))
                return True
            except Exception:
                return False

        @classmethod
        def ollama_chat(cls, messages, tools=None, *, base, model, num_ctx,
                        guard, logger, state=None, urlopen=urllib.request.urlopen,
                        keep_alive=None):
            guard()
            payload = {"model": model, "messages": messages, "stream": False,
                       "think": False, "keep_alive": keep_alive or os.environ.get(
                           "HANDSOFF_KEEP_ALIVE", "1h"),
                       "options": {"temperature": 0.3, "num_ctx": num_ctx}}
            if tools:
                payload["tools"] = tools
            req = urllib.request.Request(base + "/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=300) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return body.get("message") or {}
            except urllib.error.HTTPError as error:
                detail = cls._read_http_error(error)
                if error.code == 400 and tools and "tool" in detail.lower():
                    logger.warning("model %s does not support tools; continuing without", model)
                    return cls.ollama_chat(messages, None, base=base, model=model,
                                           num_ctx=num_ctx, guard=guard, logger=logger,
                                           state=state, urlopen=urlopen,
                                           keep_alive=keep_alive)
                if error.code == 404 and "model" in detail.lower():
                    detail += f" — run: ollama pull {model}"
                raise RuntimeError(f"Ollama error {error.code}: {detail}") from None
            except urllib.error.URLError as error:
                raise RuntimeError(f"cannot reach Ollama at {base} ({error.reason}). "
                                   "Start it with: systemctl start ollama") from None

        @classmethod
        def ollama_chat_stream(cls, messages, q, cancel=None, tools=None, *, base,
                               model, num_ctx, guard, logger, state=None,
                               urlopen=urllib.request.urlopen, keep_alive=None):
            guard()
            payload = {"model": model, "messages": messages, "stream": True,
                       "think": False, "keep_alive": keep_alive or os.environ.get(
                           "HANDSOFF_KEEP_ALIVE", "1h"),
                       "options": {"temperature": 0.3, "num_ctx": num_ctx}}
            if tools:
                payload["tools"] = tools
            req = urllib.request.Request(base + "/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            buf, full, calls, fallback = "", "", [], False
            try:
                with urlopen(req, timeout=300) as response:
                    for raw in response:
                        if cancel is not None and cancel.is_set():
                            break
                        try:
                            chunk = json.loads(raw.strip().decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            continue
                        msg = chunk.get("message") or {}
                        calls.extend(msg.get("tool_calls") or [])
                        piece = msg.get("content") or ""
                        buf += piece
                        full += piece
                        while True:
                            match = re.search(r"[.!?…](\s|$)", buf)
                            if not match:
                                break
                            sentence, buf = buf[:match.end()], buf[match.end():]
                            sentence = cls.strip_thinking(sentence)
                            if sentence and not cls.is_leaked_markup(sentence):
                                q.put(sentence)
                # no tail flush after a barge-in: it would be spoken over the user
                if not (cancel is not None and cancel.is_set()):
                    tail = cls.strip_thinking(buf)
                    if tail and not cls.is_leaked_markup(tail):
                        q.put(tail)
            except urllib.error.HTTPError as error:
                detail = cls._read_http_error(error)
                if error.code == 400 and tools and "tool" in detail.lower():
                    logger.warning("model %s does not support tools; continuing without", model)
                    fallback = True
                    return cls.ollama_chat_stream(messages, q, cancel, None,
                        base=base, model=model, num_ctx=num_ctx, guard=guard,
                        logger=logger, state=state, urlopen=urlopen,
                        keep_alive=keep_alive)
                raise RuntimeError(f"Ollama error {error.code}: {detail}") from None
            finally:
                if not fallback:
                    q.put(None)
            return {"tool_calls": calls, "content": cls.strip_thinking(full)}

    _brain = _LegacyBrain
_ss_mod = sys.modules.get("core.settings_schema")  # core/__init__ loads it
if _ss_mod is None:
    _ss_mod = _load_module("settings_schema")
DEFAULT_SETTINGS = _ss_mod.DEFAULT_SETTINGS  # noqa: F811  (single source)
SETTINGS_VERSION = _ss_mod.SETTINGS_VERSION  # noqa: F811  (single source)
if sys.modules.get("settings_schema") is None:
    sys.modules["settings_schema"] = _ss_mod  # alias; never clobber foreign

# Step (a) of the monolith cut plan: the settings machinery lives in
# core/settings.py (loaded above, beside-file in the deployed ~/.local/bin
# layout). The app keeps thin late-bound WRAPPERS for what it needs to bind to
# its own paths — never a re-export of core's names. core.settings never reaches
# back into handsoff globals: every path is a parameter.


def _load_settings() -> dict:
    """Defaults <- environment <- settings.json (implemented in core.settings;
    the paths stay handsoff globals so tests can redirect them)."""
    s = _core_settings._load_settings(SETTINGS_FILE)
    _SETTINGS_OBJ.settings_file = SETTINGS_FILE
    _SETTINGS_OBJ.config_dir = CONFIG_DIR
    _SETTINGS_OBJ._data = s
    _SETTINGS_OBJ._loaded = True
    return s

SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEPLOYMENT_FILE = CONFIG_DIR / "deployment.json"
SYSTEMD_UNIT_FILE = HOME / ".config/systemd/user/handsoff.service"
# the explicit settings object (step a): same dict, one owner — wrappers and
# future split steps route reads/writes through it
_SETTINGS_OBJ = _core_settings.settings_object(SETTINGS_FILE, CONFIG_DIR)


def _sha256_file(path: Path) -> str | None:
    """Return a file hash for deployment diagnostics without raising."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _repo_source_path() -> Path | None:
    """Find the checkout that should match an installed handsoff copy.

    The installed service normally runs from ``~/.local/bin`` while
    development happens in ``~/Projects/handsoff``.  The old implementation
    returned ``SELF_PATH`` first, which made an installed copy compare against
    itself and report a false "in sync" result.  Prefer an explicit source,
    a real checkout, or a common checkout location; only use the running file
    itself when it is actually inside a checkout.
    """
    try:
        self_path = SELF_PATH.resolve()
    except OSError:
        self_path = SELF_PATH

    def as_file(value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        return candidate / "handsoff.py" if candidate.is_dir() else candidate

    candidates: list[Path] = []
    explicit = os.environ.get("HANDSOFF_SOURCE_PATH")
    if explicit:
        candidates.append(as_file(explicit))
    # A checkout has a .git entry beside the source.  This also supports a
    # clone in a non-standard directory when handsoff is run from that clone.
    if (self_path.parent / ".git").exists():
        candidates.append(self_path)
    candidates.extend((
        HOME / "Projects/handsoff/handsoff.py",
        HOME / "projects/handsoff/handsoff.py",
        Path.cwd() / "handsoff.py",
        self_path,
    ))
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = as_file(candidate).resolve()
        except OSError:
            continue
        # An installed ~/.local/bin copy is not a source checkout.  Do not
        # let it satisfy the comparison merely because it exists.
        if (resolved == self_path and resolved.parent.name == "bin"
                and resolved.parent.parent.name == ".local"):
            continue
        if str(resolved) in seen or not resolved.is_file():
            continue
        seen.add(str(resolved))
        return resolved
    return None


_DEPLOY_FILES = (
    "handsoff.py",
    "handsoff-settings.py",
    "settings_schema.py",
    "hardware.py",
    "core/__init__.py",
    "core/settings.py",
    "core/doctor.py",
    "handsoff-restart",
)


def _appearance_look() -> str:
    """The Appearance look the current settings spell out, or "" for Custom.

    Derived from the settings, never stored beside them (see the catalogue in
    settings_schema): a saved name plus independently editable values is how a
    GUI ends up claiming "Neon" while the bubble renders something else. This
    is what `--ptt health` and the doctor report, so "which look am I running"
    has an answer that cannot disagree with the bubble in front of you.
    """
    try:
        return _core_settings.look_matching(SETTINGS)
    except Exception:
        return ""


def _appearance_note() -> str:
    """One doctor line: the look, the design and the window size.

    The `image` design draws the user's own file, and the only thing that can
    go wrong there IS the file: a path that no longer exists, a folder, a
    format this Qt cannot decode, or a picture that is entirely transparent.
    The bubble draws its dashed empty slot for every one of those, which is
    deliberately indistinguishable from "no picture chosen yet" — so the
    REASON has to be readable here. The sentence comes from the bubble module's
    own `design_image_problem`, the same one the settings picker shows, so
    doctor and the desktop cannot disagree about one file.
    """
    look = _core_settings.look_label(_appearance_look()) or "Custom"
    design = str(SETTINGS.get("bubble_design", "orb"))
    try:
        size = int(SETTINGS.get("bubble_size", _core_bubble.WINDOW_PX))
    except (TypeError, ValueError):
        size = _core_bubble.WINDOW_PX
    note = f"look {look} ({design}, {size} px)"
    pack = str(SETTINGS.get("design_pack") or "").strip()
    if pack:
        # Named rather than implied: a pack is the AUTHORITY over the single
        # picture, so "which pictures are on screen" has to be answerable here
        # without opening the settings app.
        note = f"{note} — pack {pack}"
    try:
        # Pack first, then the per-state pictures, then the single file —
        # `art_problem` owns that precedence, so doctor cannot describe a
        # different source than the renderer draws from.
        problem = _core_bubble.art_problem()
        own = _core_bubble.state_pictures(SETTINGS)
        usable = _core_bubble.usable_state_pictures(SETTINGS)
    except Exception:
        problem, own, usable = "", {}, {}
    if own and not pack:
        # Named as a COUNT rather than by listing four paths: the line is a
        # summary, and "which states have their own picture" is what is
        # actionable (the settings panel shows each one in full). The count is
        # of pictures that will RENDER, not of settings that are set — the
        # `image:` clause below names the broken one, and 4/4 beside that name
        # would be the line contradicting itself.
        note = (f"{note} — picture per state: {len(usable)}/"
                f"{len(_core_bubble.PACK_STATES)}")
    return f"{note} — image: {problem}" if problem else note


def _web_lines() -> list:
    """Two doctor lines: which search backends and reader are actually usable.

    The content is the bubble module's own record of what it has OBSERVED —
    never a probe that assumes a backend is healthy because it is configured,
    which is the false-positive shape the ydotool probe infamously has. A
    backend nothing has asked says `untried`; the SearXNG entry is the one real
    probe (a localhost connect, no external traffic), because that backend's
    availability is a local service rather than a remote site.
    """
    try:
        return _web.doctor_lines()
    except Exception:
        log.exception("web doctor lines failed")
        return []


def _deployment_snapshot() -> dict:
    """Describe the code actually running and whether it matches the checkout.

    This is deliberately based on hashes, not mtimes: a stale installed copy
    can have a newer timestamp after a failed deployment.  in-sync requires
    EVERY deployed file (the install.sh manifest set: bubble, settings app,
    support modules, restart script) to match its checkout source —
    comparing handsoff.py alone hides a stale sibling.  No settings values
    or calendar secrets are included in this diagnostic payload.
    """
    try:
        current = SELF_PATH.resolve()
    except OSError:
        current = SELF_PATH
    repo = _repo_source_path()
    try:
        installed = (HOME / ".local/bin/handsoff.py").resolve()
    except OSError:
        installed = HOME / ".local/bin/handsoff.py"
    current_hash = _sha256_file(current)
    repo_hash = _sha256_file(repo) if repo else None
    installed_hash = _sha256_file(installed)
    manifest: dict = {}
    try:
        raw = json.loads(DEPLOYMENT_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            manifest = raw
    except (OSError, ValueError):
        pass
    same_current_repo = bool(current_hash and repo_hash and current_hash == repo_hash)
    same_installed_repo = bool(installed_hash and repo_hash and installed_hash == repo_hash)
    repo_dir = repo.parent if repo else None
    bin_dir = installed.parent
    # What gets compared comes from the installer's manifest, which is written
    # by glob over the files actually deployed — so a newly added module is
    # covered the moment it ships. _DEPLOY_FILES is the floor for a
    # manifest-less install (or a hand-rolled checkout). Maintaining a second
    # hand-written list here is exactly how core/theme.py reached the
    # deployment and then went missing from it while doctor still said
    # `in-sync`.
    manifest_files = manifest.get("files")
    tracked = sorted(set(_DEPLOY_FILES) | (
        set(manifest_files) if isinstance(manifest_files, dict) else set()))
    files: dict = {}
    all_match = True
    partial_source = False  # installed file exists but its source is missing
    for rel in tracked:
        src = repo_dir / rel if repo_dir is not None else None
        src_hash = _sha256_file(src) if src is not None else None
        dst_hash = _sha256_file(bin_dir / rel)
        match = (bool(dst_hash and dst_hash == src_hash)
                 if src_hash else None)  # None: no source to compare
        files[rel] = {"source_sha256": src_hash,
                      "installed_sha256": dst_hash, "match": match}
        if match is False:
            all_match = False
        elif match is None and dst_hash:
            partial_source = True
    if not current_hash:
        status = "running-missing"
    elif not repo_hash:
        status = "source-unknown"
    elif not installed_hash:
        status = "installed-missing"
    elif all_match and not partial_source:
        status = "in-sync"
    else:
        status = "installed-drift"
    return {
        "status": status,
        "running_path": str(current),
        "running_sha256": current_hash,
        "repo_path": str(repo) if repo else None,
        "repo_sha256": repo_hash,
        "installed_path": str(installed),
        "installed_sha256": installed_hash,
        "running_is_installed": current == installed,
        "running_matches_repo": same_current_repo,
        "installed_matches_repo": same_installed_repo,
        "files": files,
        "manifest": manifest,
    }


# --------------------------------------------------------------- doctor report
# Phase 4d: doctor diagnostics moved to core/doctor.py. This file re-exports
# the public names and wires every host-side dependency through the
# core/doctor.Deps seam so the existing monkeypatch contract (H.OLLAMA_BASE,
# H.ToolBelt._niri_msg, etc.) keeps working untouched.

try:
    _hardware = _load_module("hardware")  # shared loader: origin-checked
except ImportError:  # installed copy without the sibling module (yet)
    _hardware = None

try:
    _core_doctor = _load_module("doctor")
except ImportError:
    # Compatibility with already-installed bundles made before core/doctor.py:
    # fall back to the original in-process implementation so the bubble keeps
    # running while the user has not yet redeployed.
    from core import doctor as _core_doctor  # type: ignore[no-redef]


_DOCTOR_TTL: dict = {}  # shared TTL cache for prompt-context snapshots


def _doctor_ctx() -> dict:
    """handsoff globals → hardware ctx (never the reverse: no SETTINGS import)."""
    return {
        "ollama_base": OLLAMA_BASE, "ollama_model": OLLAMA_MODEL,
        "whisper_size": WHISPER_SIZE, "tts_engine": TTS_ENGINE,
        "tts_reference": TTS_REFERENCE,
        "tts_weights_dir": str(_audio.tts_weights_dir()),
        "mic_device": str(SETTINGS.get("mic_device") or ""),
        "whisper_model_dir": str(WHISPER_MODEL_DIR),
        "control_sock": str(CONTROL_SOCK), "state_dir": str(STATE_DIR),
        "systemd_unit_file": str(SYSTEMD_UNIT_FILE),
    }


def _doctor_probers() -> dict:
    """Existing seams as late-bound lambdas, so monkeypatch keeps working."""
    return {
        "niri_windows": lambda: ToolBelt._niri_msg("msg", "--json", "windows"),
        "ydotool_which": lambda: shutil.which("ydotool"),
        "ydotool_socket": lambda: ToolBelt._ydotool_socket(),
        "socket_connectable": lambda sock: ToolBelt._socket_connectable(sock),
    }


def _doctor_snapshot(ttl_cache: dict | None) -> dict | None:
    """hardware.snapshot() or None (module absent / unexpected failure)."""
    if _hardware is None:
        return None
    try:
        return _hardware.snapshot(_doctor_ctx(), _doctor_probers(), ttl_cache)
    except Exception:
        log.exception("hardware snapshot failed")
        return None


def _hardware_prompt_context() -> str:
    """5-line system-prompt context: niri/wayland, mic, model, voices, mounts."""
    if _hardware is None:
        return ""
    snap = _doctor_snapshot(_DOCTOR_TTL)
    if not snap:
        return ""
    try:
        return _hardware.prompt_context(snap)
    except Exception:
        return ""


def _build_doctor_deps() -> _core_doctor.DoctorDeps:
    """Late-bound deps so monkeypatch on H.OLLAMA_BASE / H.ToolBelt._niri_msg
    / H.ollama_available / etc. keeps affecting the doctor output. The deps
    object is rebuilt on every call (cheap; no I/O) so live host globals
    always win."""
    return _core_doctor.DoctorDeps(
        ollama_base=OLLAMA_BASE,
        ollama_model=OLLAMA_MODEL,
        ollama_available=ollama_available,
        tts_model=_tts_model,
        tts_engine=TTS_ENGINE,
        tts_reference=TTS_REFERENCE,
        whisper_model=_whisper_model,
        deployment_snapshot=_deployment_snapshot,
        hardware_snapshot=_doctor_snapshot,
        hardware_prompt_context=_hardware_prompt_context,
        doctor_ttl=_DOCTOR_TTL,
        niri_msg=ToolBelt._niri_msg,
        ydotool_socket=ToolBelt._ydotool_socket,
        socket_connectable=ToolBelt._socket_connectable,
        sys_version_info=sys.version_info,
        restart_script=RESTART_SCRIPT,
        systemd_unit_file=SYSTEMD_UNIT_FILE,
        control_sock=CONTROL_SOCK,
        crash_log=CRASH_LOG,
        appearance_look=_appearance_note,
        web_lines=_web_lines,
        cap_refusal_note=_cap_refusal_note,
        cap_refusals=_cap_refusal_summary,
        remote_ollama_allowed=_ollama_remote_opted_in,
        remote_ollama_optin_source=_remote_ollama_optin_source,
        shutil=shutil,
        sounddevice=sd,
        log=log,
    )


def run_doctor() -> str:
    """One human-readable diagnostic pass over everything the bubble needs.

    Read-only, side-effect-free (the Ollama probe is a 2 s GET). Served via
    `--ptt doctor` and the `handsoff_doctor` tool; the AI reads it to fix
    itself instead of guessing.
    """
    token = _core_doctor.set_dependencies(_build_doctor_deps())
    try:
        return _core_doctor.run_doctor()
    finally:
        _core_doctor.reset_dependencies(token)


def doctor_json() -> dict:
    """Machine-readable doctor output for the control socket."""
    token = _core_doctor.set_dependencies(_build_doctor_deps())
    try:
        return _core_doctor.doctor_json()
    finally:
        _core_doctor.reset_dependencies(token)


# ------------------------------------------------------------ typing selftest

def _selftest_check(results: list, name: str, status: str, detail: str) -> None:
    results.append({"name": name, "status": status, "detail": detail})


def _selftest_report(results: list) -> str:
    lines = ["handsoff typing selftest"]
    for r in results:
        lines.append(f"  [{r['status']}] {r['name']} — {r['detail']}")
    fails = sum(1 for r in results if r["status"] == "FAIL")
    skips = sum(1 for r in results if r["status"] == "SKIP")
    if fails:
        verdict = f"FAIL ({fails} of {len(results)} checks failed)"
    elif skips:
        verdict = f"PASS ({len(results) - skips} passed, {skips} skipped)"
    else:
        verdict = f"PASS ({len(results)}/{len(results)} checks)"
    lines.append(f"verdict: {verdict}")
    return "\n".join(lines)


def run_typing_selftest(timeout: float = 45.0, belt: "ToolBelt | None" = None) -> str:
    """The hardware typing checks of ACCEPTANCE.md section 5, one pass.

    Launches its OWN scratch windows (a terminal and a text editor) and types
    only into them, so it is safe on a live desktop; windows are closed and
    the clipboard restored afterwards. Stages: ydotoold socket, focus
    verification, terminal-refusal (fail-closed), type_text landing, and the
    ctrl+a/ctrl+c clipboard round-trip. `belt` is injectable for tests.
    """
    belt = belt or ToolBelt(on_restart_pending=lambda: None)
    deadline = time.monotonic() + timeout
    results: list = []
    procs: list[subprocess.Popen] = []
    scratch: list[dict] = []
    token = f"handsoff selftest {time.strftime('%H%M%S')}"
    clip_before = None

    def windows() -> list[dict]:
        r = ToolBelt._niri_msg("msg", "--json", "windows")
        if r.returncode != 0:
            raise RuntimeError("niri IPC unavailable — cannot verify focus")
        return json.loads(r.stdout or "[]")

    def wait_for(app_id: str) -> dict:
        while time.monotonic() < deadline:
            for w in windows():
                if w.get("app_id") == app_id:
                    return w
            time.sleep(0.4)
        raise RuntimeError(f"{app_id} window did not appear within {timeout:.0f}s")

    def focus(win: dict) -> None:
        r = ToolBelt._niri_msg("msg", "action", "focus-window",
                               "--id", str(win["id"]))
        if r.returncode != 0:
            raise RuntimeError("focus-window action failed")
        time.sleep(0.6)
        cur = next((w for w in windows() if w.get("id") == win["id"]), None)
        if not (cur and cur.get("is_focused")):
            raise RuntimeError("scratch window did not take focus")

    # 1. the ydotool daemon the CLI must reach
    try:
        sock = ToolBelt._ydotool_socket()
        if ToolBelt._socket_connectable(sock):
            _selftest_check(results, "ydotool daemon", "PASS", f"socket {sock}")
        else:
            _selftest_check(results, "ydotool daemon", "FAIL",
                            f"no live socket at {sock} — start the user daemon: "
                            "systemctl --user enable --now ydotool.service")
    except Exception as e:
        _selftest_check(results, "ydotool daemon", "FAIL", str(e))

    try:
        if results[0]["status"] != "PASS":
            raise RuntimeError("skipped: the ydotoold daemon is unreachable")

        # 2. focus verification plumbing (niri live)
        fw = belt._focused_window_info()
        _selftest_check(results, "focus verification", "PASS",
                        f"focused: {belt._win_label(fw)}" if fw else
                        "no focused window (typing would fail closed)")

        # 3. terminal refusal — never touches a pre-existing terminal
        term = next(((b, a) for b, a in (("foot", "foot"), ("kitty", "kitty"),
                    ("alacritty", "alacritty"), ("xterm", "xterm"))
                    if shutil.which(b)), None)
        if term is None:
            _selftest_check(results, "terminal refusal", "SKIP",
                            "no terminal emulator installed")
        else:
            bin_, app_id = term
            procs.append(subprocess.Popen(
                [bin_], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True))
            tw = wait_for(app_id)
            focus(tw)
            marker = belt._terminal_marker(belt._typing_guard())
            if not marker:
                raise RuntimeError(f"{app_id} was not recognised as a terminal")
            out, err = belt.execute("type_text", {"text": "selftest refused"})
            out2, err2 = belt.execute("press_keys", {"combo": "enter"})
            refused = (err and str(out).startswith("REFUSED")
                       and err2 and str(out2).startswith("REFUSED"))
            _selftest_check(results, "terminal refusal",
                            "PASS" if refused else "FAIL",
                            f"{marker}: type_text and press_keys refused"
                            if refused else f"NOT refused: {out} / {out2}")

        # 4. type_text lands in a scratch editor (focus-guarded by the tool)
        if shutil.which("gnome-text-editor"):
            procs.append(subprocess.Popen(
                ["gnome-text-editor"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True))
            ed = wait_for("org.gnome.TextEditor")
            scratch.append(ed)
            focus(ed)
            out, err = belt.execute("type_text", {"text": token})
            ok = not err and "typed" in str(out) and "WARNING" not in str(out)
            _selftest_check(results, "type_text", "PASS" if ok else "FAIL",
                            str(out) if not err else str(out))

            # 5. ctrl+a/ctrl+c round-trip proves what landed, byte-for-byte
            try:
                clip_before = subprocess.run(
                    ["wl-paste", "--no-newline"], capture_output=True,
                    text=True).stdout or ""
            except Exception:
                clip_before = None
            o1, e1 = belt.execute("press_keys", {"combo": "ctrl+a"})
            o2, e2 = belt.execute("press_keys", {"combo": "ctrl+c"})
            time.sleep(0.8)
            clip = subprocess.run(["wl-paste", "--no-newline"],
                                  capture_output=True, text=True).stdout
            match = not e1 and not e2 and clip == token
            _selftest_check(results, "clipboard round-trip",
                            "PASS" if match else "FAIL",
                            f"wl-paste matches the typed token ({len(token)} chars)"
                            if match else f"clipboard mismatch: {clip[:60]!r}")
        else:
            _selftest_check(results, "type_text", "SKIP",
                            "gnome-text-editor not installed")
            _selftest_check(results, "clipboard round-trip", "SKIP",
                            "no scratch editor available")
    except Exception as e:
        _selftest_check(results, "selftest run", "FAIL", str(e))
    finally:
        for w in scratch:
            try:
                ToolBelt._niri_msg("msg", "action", "close-window",
                                   "--id", str(w["id"]))
            except Exception:
                pass
        for p in procs:
            if p.poll() is None:
                p.terminate()
        if clip_before is not None:
            try:
                subprocess.run(["wl-copy"], input=clip_before,
                               capture_output=True, text=True)
            except Exception:
                pass
    return _selftest_report(results)


# ------------------------------------------------------------ decision log


DECISIONS_FILE = STATE_DIR / "decisions.jsonl"
_DECISIONS_LOCK = threading.Lock()
_DECISIONS_MAX = 500






# ------------------------------------------------------------ bounded jobs




def _private_dir(path: Path) -> bool:
    """Create a state/config directory and make it owner-only.

    Refuse symlinked directories: configuration and the control socket must
    never be redirected to an attacker-controlled location.
    """
    try:
        if path.is_symlink():
            return False
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        info = path.stat()
        return (stat.S_ISDIR(info.st_mode)
                and (info.st_mode & 0o077) == 0
                and info.st_uid == os.getuid())
    except OSError:
        return False


def _secure_runtime_files() -> bool:
    """Harden files that can contain secrets, transcripts, or control state.

    Deliberately no short-circuit: every file gets hardened even when an
    earlier one is bad, and the AND of the results is returned."""
    paths = [SETTINGS_FILE, HISTORY_FILE, MEMORY_FILE, CRASH_LOG,
             PENDING_FILE, LOCK_FILE, LOG_FILE, CONTROL_SOCK, MIC_EVENTS_FILE,
             CAP_EVENTS_FILE, REMINDERS_FILE]
    # Backups hold the SAME secrets as the files they copy (a transcript, the
    # settings), and `shutil.copy2` inherits the SOURCE's mode at copy time —
    # so any sidecar written while the source was still loose stays loose
    # forever. Hardening the live file is not enough: sweep the sidecars too,
    # or history.json.bak-modelswitch keeps a 0644 conversation transcript.
    for directory in (CONFIG_DIR, STATE_DIR):
        try:
            paths.extend(p for p in directory.glob("*.bak*")
                         if not p.is_symlink())
        except OSError:
            log.warning("could not enumerate backups in %s", directory)
    # materialize first: all(generator) short-circuits, which would leave
    # every file after a bad one unhardened
    return all([_core_settings._secure_file(path) for path in paths])


def _prepare_runtime() -> bool:
    """Create the runtime roots privately and harden existing state files."""
    for directory in (CONFIG_DIR, STATE_DIR, WHISPER_MODEL_DIR):
        if not _private_dir(directory):
            return False
    return _secure_runtime_files()


def _migrate_settings(data: dict, _slog: "logging.Logger | None" = None) -> dict:
    """Migrate an older settings.json layout (thin wrapper: real hook in core)."""
    return _core_settings._migrate_settings(data, _slog)


def _backup_runtime_json(path: Path) -> None:
    """One-generation .bak beside a runtime JSON file (thin wrapper: real
    implementation in core.settings)."""
    _core_settings._backup_runtime_json(path)


def _write_settings_dict(data: dict, *, stamp_version: bool = True) -> None:
    """Serialize a full settings dict to settings.json: version-stamped,
    backed up one generation, atomic (thin wrapper: real writer in core)."""
    _core_settings._write_settings_dict(data, SETTINGS_FILE, CONFIG_DIR,
                                        stamp_version=stamp_version)


def _persist_setting(key: str, value) -> bool:
    """Persist one runtime setting (implemented in core.settings; this wrapper
    is the tests' patch seam and adds the derived-global refresh).

    Returns whether it reached the disk. The runtime globals and the derived
    settings are refreshed ONLY on success: updating them after a swallowed
    write error left the bubble running on (and re-stamping) a value that the
    next start would not read back.
    """
    if not _core_settings._persist_setting(key, value, SETTINGS_FILE, CONFIG_DIR):
        log.warning("setting %r was NOT persisted — keeping the old value", key)
        return False
    SETTINGS[key] = value
    SETTINGS["version"] = SETTINGS_VERSION
    reload_derived_settings()
    return True


def set_setting(key: str, value) -> bool:
    """Single entry point for every SETTINGS mutation.

    Takes the in-process lock AND the cross-process file lock (via
    _persist_setting's read-merge-write), refreshes the in-memory copy and
    derived globals — no caller may use a raw ``SETTINGS[k] = v`` write and
    bypass the lock. Returns whether the value reached the disk.

    Memory is updated by the wrapper, so only on success. Writing SETTINGS
    first (the old behaviour) meant a failed write left the bubble running on
    — and re-stamping — a value the next start could not read back, the exact
    divergence `_persist_setting` was fixed to prevent for its other callers.
    A caller that must tell the user about an unsaved change checks this.
    """
    return bool(_persist_setting(key, value))


SETTINGS = _load_settings()

_ollama_host = str(SETTINGS["ollama_host"])
if not _ollama_host.startswith(("http://", "https://")):
    _ollama_host = "http://" + _ollama_host
OLLAMA_BASE = _ollama_host.rstrip("/")
OLLAMA_MODEL = str(SETTINGS["model"])
OLLAMA_NUM_CTX = int(SETTINGS["num_ctx"])
# token-based history budget: never send more than this many estimated tokens
# (chars/4 is a conservative estimate). Override with history_tokens in
# settings. Default: num_ctx MINUS the fixed per-turn cost (system prompt +
# tool schemas, estimated at import time) MINUS a reserve for the reply —
# a 3/4-of-ctx budget on top of the fixed cost could overflow the window.
HISTORY_CHARS_PER_TOKEN = 4
_FIXED_PROMPT_TOKENS = 0


def _fixed_prompt_tokens() -> int:
    """Estimated tokens of the fixed per-turn cost: system prompt + tools.
    Computed lazily (build_tools needs the ToolBelt class to exist)."""
    global _FIXED_PROMPT_TOKENS
    if not _FIXED_PROMPT_TOKENS:
        _FIXED_PROMPT_TOKENS = (len(SYSTEM_PROMPT)
                                + len(json.dumps(_core_tools.build_tools()))) \
            // HISTORY_CHARS_PER_TOKEN
    return _FIXED_PROMPT_TOKENS


def _history_budget() -> int:
    """History token budget: explicit setting wins; otherwise num_ctx minus
    the fixed prompt cost minus a 1024-token reply reserve (floor 1024)."""
    explicit = int(SETTINGS.get("history_tokens") or 0)
    if explicit:
        return explicit
    return max(1024, OLLAMA_NUM_CTX - _fixed_prompt_tokens() - 1024)
WHISPER_SIZE = SETTINGS["whisper_size"]
WHISPER_DEVICE = SETTINGS.get("whisper_device", "auto")
# Speech engine: chatterbox-turbo, with an OPTIONAL >=5 s reference clip whose
# voice it clones. "" means the model's own built-in voice.
TTS_REFERENCE = str(SETTINGS.get("tts_reference") or "")


def reload_derived_settings() -> None:
    """Refresh derived globals after a model/ctx/host change.

    Frozen OLLAMA_* / token-budget / tool-support state otherwise survives a
    settings save until restart. Resets the prompt-token cache and re-arms
    tool-support probing when the model changes.
    """
    global OLLAMA_BASE, OLLAMA_MODEL, OLLAMA_NUM_CTX
    global WHISPER_SIZE, WHISPER_DEVICE, TTS_REFERENCE
    global _FIXED_PROMPT_TOKENS, _TOOLS_SUPPORTED
    host = str(SETTINGS.get("ollama_host", OLLAMA_BASE))
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    OLLAMA_BASE = host.rstrip("/")
    new_model = str(SETTINGS.get("model", OLLAMA_MODEL))
    try:
        new_ctx = int(SETTINGS.get("num_ctx", OLLAMA_NUM_CTX))
    except (TypeError, ValueError):
        new_ctx = OLLAMA_NUM_CTX
    if new_model != OLLAMA_MODEL:
        _TOOLS_SUPPORTED = True
        _FIXED_PROMPT_TOKENS = 0
    if new_ctx != OLLAMA_NUM_CTX:
        _FIXED_PROMPT_TOKENS = 0
    OLLAMA_MODEL = new_model
    OLLAMA_NUM_CTX = new_ctx
    WHISPER_SIZE = SETTINGS.get("whisper_size", WHISPER_SIZE)
    WHISPER_DEVICE = SETTINGS.get("whisper_device", "auto")
    TTS_REFERENCE = str(SETTINGS.get("tts_reference") or "")
    # The appearance — window size, the geometry derived from it, the palette
    # and the two animation knobs — is owned by core.bubble, and this is the
    # reload path's single call into it. Guarded the same way `_audio` is just
    # below: this function is defined before the handle exists, and a partial
    # install may never get one.
    _bubble = globals().get("_core_bubble")
    if _bubble is not None:
        _bubble.configure(SETTINGS)
    if "_audio" in globals():
        _audio.configure(
            sample_rate=SAMPLE_RATE,
            whisper_size=WHISPER_SIZE,
            whisper_device=WHISPER_DEVICE,
            whisper_model_dir=WHISPER_MODEL_DIR,
            tts_reference=TTS_REFERENCE,
            settings=SETTINGS,
            logger=log,
        )

def _wake_name() -> str:
    """The assistant's wake name, lowercased (default: 'assistant')."""
    return str(SETTINGS.get("assistant_name", "assistant")).strip().lower() or "assistant"


_WAKE_FILLER = {"hey", "ok", "okay", "hi", "yo"}


def _norm_words(text: str) -> list[str]:
    return [w.strip(".,!?;:") for w in (text or "").split()]


def _is_wake_utt(text: str) -> bool:
    """True when the whole utterance is just the wake name ('hey assistant')."""
    words = [w for w in _norm_words(text.lower()) if w]
    name_words = _wake_name().split()
    if words == name_words:
        return True
    return (len(words) == len(name_words) + 1 and words[0] in _WAKE_FILLER
            and words[1:] == name_words)


def _wake_skeleton(word: str) -> str:
    """Pronunciation-ish skeleton: consonants only, c/k/q/x/z→s, h/w dropped,
    liquids/nasals (r/l/m) unified to n. Whistle-down of whisper mishearings
    like 'cypher'→'Siphon' (both → 'spn'): wake matching must err toward
    LISTENING, not toward ignoring its own user."""
    w = word.lower().translate(str.maketrans("", "", "hw"))
    w = w.translate(str.maketrans("ckqxz", "sssss"))
    w = "".join(ch for ch in w if ch not in "aeiouy")
    return w.replace("r", "n").replace("m", "n").replace("l", "n")


def _match_wake(text: str) -> str | None:
    """If `text` starts with the wake name, return the rest (possibly '').
    Accepts 'name ...', 'hey name ...', 'name, ...'. None if no wake word.
    Word-token based, name tried before the filler skip (so a custom name
    that itself starts with 'hey' still works). Falls back to a fuzzy
    pronunciation-skeleton match for misheard names (Siphon~cypher)."""
    words = _norm_words(text)
    if not words:
        return None
    lw = [w.lower() for w in words]
    name_words = _wake_name().split()
    fuzzy_ok = all(len(nw) >= 4 for nw in name_words)   # never fuzzy on tiny names
    for skip in (0, 1):
        if skip and (len(lw) <= skip or lw[skip - 1] not in _WAKE_FILLER):
            continue
        seg = lw[skip:skip + len(name_words)]
        if seg == name_words:
            return " ".join(words[skip + len(name_words):])
        if (fuzzy_ok and len(seg) == len(name_words)
                and [_wake_skeleton(w) for w in seg]
                == [_wake_skeleton(nw) for nw in name_words]):
            return " ".join(words[skip + len(name_words):])
    return None


def _tick_now() -> float:
    return time.monotonic()



SAMPLE_RATE = 16_000
MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 40

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"


LOCK_RETRIES = 40                     # lock wait on restart: 40 x 0.25s = 10s
LOCK_RETRY_WAIT = 0.25

log = logging.getLogger("handsoff")

# Audio primitives live independently of this application module.  These
# adapters preserve the historical monkeypatch/module-global seams while
# passing settings, paths, and logging explicitly into core.audio.
try:
    from core import audio as _audio
except ImportError:  # compatibility with pre-Phase-4a deployed bundles
    class _MissingAudio:
        _TRANSCRIBE_LOCK = threading.Lock()
        _whisper_lock = threading.Lock()
        _CUDA_ERR_RE = re.compile(r"cuda|cublas|cudnn", re.IGNORECASE)
        _WHISPER_VRAM_MB = {}
        _tts_lock = threading.Lock()
        _tts_model = None
        # mic_health() reports the loading device and the configured voice, so
        # these must exist here too: a partial install answers `health` (and the
        # settings app's whole Voice tab) or it raises AttributeError instead of
        # reporting that the engine is missing.
        _tts_device = ""
        TTS_ENGINE = "chatterbox-turbo"
        TTS_REFERENCE = ""
        MIN_REFERENCE_S = 5.0
        _MIC_OPERATION_LOCK = threading.RLock()
        _MIC_OPERATION_STATE_LOCK = threading.Lock()
        _MIC_OPERATION_OWNER = None
        _whisper_model = None
        _whisper_cpu_fallback = False
        _nvidia_free_vram_mb = staticmethod(lambda: None)
        tts_weights_dir = staticmethod(lambda: Path("/nonexistent"))
        tts_weights_cached = staticmethod(lambda: False)
        _whisper_device_choice = staticmethod(lambda *_args: ("cpu", "int8"))
        _is_cuda_error = staticmethod(lambda *_args: False)

        @staticmethod
        def _missing(*_args, **_kwargs):
            raise ImportError("handsoff: core/audio.py is missing from this deployment")

        @staticmethod
        def configure(*_args, **_kwargs):
            return None

        # The PortAudio teardown guard must exist here too: the hands-free
        # listener's recovery path calls _audio.portaudio_busy() while handling
        # a mic failure, so a partial install must report "not busy" (nothing
        # is open) rather than raise AttributeError out of the except block.
        # (Defined inside each method: a class-body lambda cannot see _NoBusy,
        # for the same reason a nested class cannot see _missing above.)
        @staticmethod
        def portaudio_in_use():
            class _Inert:
                def __enter__(self):
                    return self

                def __exit__(self, *_exc):
                    return False

            return _Inert()

        @staticmethod
        def portaudio_busy():
            return False

        _resample_to_16k = _open_input = _stop_recorder_bounded = _missing
        get_whisper = get_tts = transcribe = tts_to_wav = play_wav = _missing
        reference_problem = reference_clip_seconds = _missing

        class Recorder:
            def __init__(self, *_args, **_kwargs):
                # a nested class does NOT inherit the outer one's attributes, so
                # self._missing would be an AttributeError: name it explicitly so
                # a partial install fails with the honest ImportError instead
                _MissingAudio._missing()

    _audio = _MissingAudio()

_audio.configure(
    sample_rate=SAMPLE_RATE,
    whisper_size=WHISPER_SIZE,
    whisper_device=WHISPER_DEVICE,
    whisper_model_dir=WHISPER_MODEL_DIR,
    tts_reference=TTS_REFERENCE,
    settings=SETTINGS,
    logger=log,
)
# core.audio uses local_files_only=True so model startup never blocks on a download.

# The bubble — its mask geometry, state palette and 13 designs — lives in its
# own module for the same reason the audio primitives do: what can be rendered
# and measured on its own belongs on its own. It is application-free and takes
# the host by injection, so THIS is the one place that binds it to the app's
# settings, paths and notifier; there are no shim re-exports above.
try:
    from core import bubble as _core_bubble
except ImportError:  # compatibility with pre-Phase-4a deployed bundles
    class _MissingBubble:
        """A partial install still imports and reports; it just cannot paint.

        `configure()` must succeed (it is called at import time, like
        `core.audio.configure`), and every path that would actually draw has to
        fail loudly instead of returning a bubble nobody sees.
        """
        WINDOW_PX = 144
        BUBBLE_R0 = 0.0
        GLOW_PAD = 0.0
        GEOM_K = 0.0
        APERTURE_R = 0.0
        BUBBLE_ACCENT = 0.5
        ANIM_ENERGY = 1.0
        STATE_COLORS: dict = {}
        SETTINGS: dict = {}
        APP_NAME = "handsoff"
        SETTINGS_APP = None
        RESTART_SCRIPT = None
        PACK_DIR_NAME = "design-packs"
        PACKS_DIR = None

        @staticmethod
        def configure(*_args, **_kwargs):
            return None

        @staticmethod
        def _missing(*_args, **_kwargs):
            raise ImportError("handsoff: core/bubble.py is missing from this "
                              "deployment")

        design_region = _missing

        class BubbleWidget:
            def __init__(self, *_args, **_kwargs):
                _MissingBubble._missing()

    _core_bubble = _MissingBubble()

_core_bubble.SETTINGS = SETTINGS
_core_bubble.APP_NAME = APP_NAME
_core_bubble.SETTINGS_APP = SETTINGS_APP
_core_bubble.RESTART_SCRIPT = RESTART_SCRIPT
# Where installed design packs live. The host owns every path this module needs;
# the module's own fallback follows the same XDG rule, so the settings app —
# which loads the module on its own — resolves the SAME tree without being told.
_core_bubble.PACKS_DIR = CONFIG_DIR / _core_bubble.PACK_DIR_NAME
# The palette and the geometry have to exist before anything can paint, so the
# appearance is derived from the loaded settings right here — the same import
# time derivation the module-level constants used to do.
_core_bubble.configure(SETTINGS)

# PortAudio is process-global.  In particular, stream.stop() must not overlap
# another InputStream construction or a close from a different thread. The locks
# live in core.audio (single owner) and are reached through `_audio` there.
_MIC_LAST_OPEN_DEVICE = threading.local()


def _available_input_devices() -> list[dict]:
    """Return input-capable devices without allowing a failed query to escape."""
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    if isinstance(devices, dict):
        devices = [devices]
    result = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        try:
            if int(device.get("max_input_channels", 0) or 0) > 0:
                result.append(device)
        except (TypeError, ValueError):
            continue
    return result


def _device_is_available(device, devices: list[dict]) -> bool:
    want = str(device).strip().casefold()
    for info in devices:
        name = str(info.get("name", "")).strip().casefold()
        if want == name or want in name:
            return True
    return False


def _open_input_unlocked(device, rate: int, blocksize: int, cb) -> tuple:
    """Open input, handling stale configured names without querying them."""
    configured = device is not None and str(device).strip()
    devices = _available_input_devices() if configured else []
    candidates = [device]
    if configured and not _device_is_available(device, devices):
        log.warning("configured microphone %r is unavailable; falling back to "
                    "system default", device)
        candidates = [None]
        candidates.extend(str(info.get("name", "")) for info in devices
                          if str(info.get("name", "")))

    last_error = None
    for candidate in candidates:
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=blocksize, callback=cb,
                                    device=candidate)
            _MIC_LAST_OPEN_DEVICE.value = candidate
            return stream, rate
        except Exception as exc:
            last_error = exc
            try:
                info = (sd.query_devices(candidate, kind="input")
                        if candidate is not None else
                        sd.query_devices(kind="input"))
                native = int(float(info.get("default_samplerate") or rate))
            except Exception:
                native = rate
            if native != rate:
                try:
                    stream = sd.InputStream(samplerate=native, channels=1,
                                            dtype="int16", blocksize=blocksize,
                                            callback=cb, device=candidate)
                    _MIC_LAST_OPEN_DEVICE.value = candidate
                    return stream, native
                except Exception as exc2:
                    last_error = exc2
    if last_error is not None:
        raise last_error
    raise ValueError("no input device available")


def _open_input(device, rate: int, blocksize: int, cb) -> tuple:
    """Serialize InputStream construction with native stream teardown."""
    if not _audio._MIC_OPERATION_LOCK.acquire(timeout=0.6):
        raise RuntimeError("microphone operation still in flight")
    try:
        return _open_input_unlocked(device, rate, blocksize, cb)
    finally:
        _audio._MIC_OPERATION_LOCK.release()


def _stop_stream_owned(stream) -> None:
    """Run stream teardown under the same owner as InputStream construction."""
    if stream is None:
        return
    _audio._MIC_OPERATION_LOCK.acquire()
    try:
        stream.stop()
        stream.close()
    finally:
        _audio._MIC_OPERATION_LOCK.release()


def _start_stream_owned(stream) -> None:
    _audio._MIC_OPERATION_LOCK.acquire()
    try:
        stream.start()
    finally:
        _audio._MIC_OPERATION_LOCK.release()


class Recorder(_audio.Recorder):
    """Recorder using the application-wide PortAudio ownership guard."""

    def start(self) -> None:
        self._frames = []
        self._samples = 0
        self._stream, self._native_rate = _open_input(
            self._device, SAMPLE_RATE, 1024, self._cb)
        selected = getattr(_MIC_LAST_OPEN_DEVICE, "value", self._device)
        if selected is not None:
            self._device = selected
        _start_stream_owned(self._stream)

    def stop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                _stop_stream_owned(stream)
            except Exception:
                log.exception("failed to close input stream")
        if not self._frames:
            return None
        audio = np.concatenate(self._frames).reshape(-1)
        return _audio._resample_to_16k(audio, getattr(self, "_native_rate", SAMPLE_RATE))


def _stop_recorder_bounded(rec, timeout: float = 3.0):
    """Stop without aborting a live native call from a second thread.

    Thin seam over core.audio: the owner protocol (mic lock, _handsoff_stop_*
    recorder attributes) is implemented once in core.audio so the abort-based
    duplicate cannot diverge again.
    """
    return _audio._stop_recorder_bounded(rec, timeout=timeout)
_whisper_model = None
_whisper_cpu_fallback = False
_tts_model = None


# The loaded models live in TWO places for historical reasons: core.audio owns
# them (it loads, uses and drops them) and this module mirrors them, because
# `_speak`, health, doctor and the test suite still ask `H._tts_model`/H._whisper_model
# "is a model loaded?". A mirror must never undo a drop.
#
# It could, and did: the push (`_audio._tts_model = _tts_model`) is read-then-assign,
# so a settings reload that dropped the cache could land between the two steps and
# be overwritten by the stale model the caller had just read. That model then came
# back and STAYED — the load saw a non-None cache, returned the old model, and the
# adopt published it to this module again — so a voice/device change silently did
# nothing until the next reload. One lock, held only for the assignment and never
# across a load, makes push-then-drop and drop-then-push the only two orders, and
# both end consistent.
_model_cache_lock = threading.Lock()


def _push_model(attr: str) -> None:
    """Hand this module's copy of a model to core.audio, atomically.

    Takes the attribute NAME, not its value, and reads it inside the lock. A
    value read at the call site is read *before* the lock is taken, so a drop
    that lands in between is overwritten by the stale model just the same — the
    lock would serialise the write while protecting nothing.
    """
    with _model_cache_lock:
        setattr(_audio, attr, globals()[attr])


def _adopt_model(attr: str, loaded):
    """Publish core.audio's model here — unless it was dropped meanwhile.

    The identity check is the whole point: if the cache was dropped while this
    call was loading, `_audio` no longer holds `loaded` and publishing it back
    into the mirror would set up exactly the resurrection the lock prevents.
    The caller still gets the model it asked for either way.
    """
    with _model_cache_lock:
        if getattr(_audio, attr, None) is loaded:
            globals()[attr] = loaded
    return loaded


def get_whisper():
    _push_model("_whisper_model")
    return _adopt_model("_whisper_model", _audio.get_whisper())


def get_tts():
    # Keep old H.get_tts monkeypatches effective without coupling core.audio
    # back to this module (same seam shape as get_whisper above).
    _push_model("_tts_model")
    return _adopt_model("_tts_model", _audio.get_tts())


def warm_tts() -> int:
    """Load-then-synthesize once at startup (seam for tests: see core.audio).

    Takes the loaded model through this module's `get_tts` so the historical
    monkeypatch seam keeps working, and passes it EXPLICITLY: core.audio must
    not fall back to loading a model of its own here, or a test that stubs
    get_tts would quietly pull 3.8 GB into the test process.
    """
    return _audio.warm_tts(model=get_tts())


def transcribe(audio_int16: np.ndarray) -> str:
    # Keep old H.get_whisper monkeypatches effective without coupling core.audio
    # back to this module. The model itself is published through `get_whisper`
    # (core.audio calls it via model_getter), so only the cpu-fallback flag is
    # mirrored here — read inside the lock for the same reason as the models.
    _push_model("_whisper_model")
    with _model_cache_lock:
        _audio._whisper_cpu_fallback = globals()["_whisper_cpu_fallback"]
    result = _audio.transcribe(audio_int16, model_getter=get_whisper)
    with _model_cache_lock:
        globals()["_whisper_cpu_fallback"] = _audio._whisper_cpu_fallback
    return result


def tts_to_wav(text: str, wav_path: Path) -> None:
    return _audio.tts_to_wav(text, wav_path, voice_getter=get_tts)


def _log_metadata(value, kind: str = "text") -> str:
    """Keep logs useful for diagnosis without retaining user-provided data."""
    if kind == "command":
        try:
            argv = shlex.split(str(value))
            return f"command={Path(argv[0]).name if argv else '(empty)'} args={max(0, len(argv) - 1)}"
        except ValueError:
            return "command=(unparseable)"
    if kind == "path":
        try:
            return f"path={Path(str(value)).name or '(root)'}"
        except (TypeError, ValueError):
            return "path=(invalid)"
    if isinstance(value, dict):
        return "keys=" + ",".join(sorted(str(k) for k in value))
    text = str(value or "")
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                parts = ["keys=" + ",".join(sorted(str(k) for k in data))]
                if "command" in data:
                    parts.append(_log_metadata(data["command"], "command"))
                if "path" in data:
                    parts.append(_log_metadata(data["path"], "path"))
                if "content" in data:
                    parts.append(_log_metadata(data["content"], "content"))
                return " ".join(parts)
        except (TypeError, ValueError):
            pass
    return f"{kind}=<redacted chars={len(text)}>"


def _redact_log_text(message: str) -> str:
    """Remove content-bearing fields while retaining event metadata."""
    text = str(message)
    patterns = (
        (r"(heard \(gen=\d+\):|follow-up accepted \(no wake word\):|"
         r"ignored \(no wake word\):|dictation typed \d+ chars \()[^)]*", "\\1<redacted>"),
        (r"(echo-check: heard )[^ ]+( -> (?:True|False))", "\\1<redacted>\\2"),
        (r"(memory: ).*", "\\1<redacted facts>"),
        (r"(run_command: |start_command: ).*", "\\1<redacted command>"),
        (r"(edit_file(?: content)?: ).*", "\\1<redacted file content>"),
        (r"(watcher alert: |reminder fired: |announcing timer: |"
         r"snooze window open for ).*", "\\1<redacted>"),
        (r"(operator: clicked ).*", "\\1<redacted interaction>"),
        (r"(previous crash detected \([^)]*\): ).*", "\\1<redacted crash details>"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


class _PrivacyLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = _redact_log_text(record.getMessage())
        if record.exc_info:
            message += f" ({record.exc_info[0].__name__})"
        record.msg = message
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def _ollama_remote() -> bool:
    """Whether the configured Ollama endpoint is outside the local machine."""
    try:
        host = urllib.parse.urlparse(OLLAMA_BASE).hostname
        if not host:
            return True
        if host.lower() == "localhost":
            return False
        return not ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return True


_REMOTE_OLLAMA_ENV = "HANDSOFF_ALLOW_REMOTE_OLLAMA"
_REMOTE_OLLAMA_ENV_TOKENS = frozenset({"1", "true", "yes", "on"})


def _remote_ollama_optin_source() -> str:
    """Which channel opted into a non-loopback brain: '' | 'settings' | 'env'.

    The single source of truth for the remote-brain opt-in, used by the send
    guard, the settings file's contract and `--ptt doctor` alike. The settings
    value is compared with `is True` rather than truthiness, matching
    core.settings.coerce_settings' fail-closed contract, so a caller that
    seeds SETTINGS without coercion cannot fail open on `"yes"` or `1`.
    """
    settings = globals().get("SETTINGS") or {}
    if settings.get("allow_remote_ollama") is True:
        return "settings"
    if os.environ.get(_REMOTE_OLLAMA_ENV, "").strip().lower() in _REMOTE_OLLAMA_ENV_TOKENS:
        return "env"
    return ""


def _ollama_remote_opted_in() -> bool:
    return bool(_remote_ollama_optin_source())


_REMOTE_OLLAMA_WARNED = False


def _guard_ollama_endpoint() -> None:
    """Require an explicit environment opt-in before sending data remotely."""
    global _REMOTE_OLLAMA_WARNED
    if not _ollama_remote():
        return
    if not _REMOTE_OLLAMA_WARNED:
        log.warning(
            "Ollama endpoint %s is remote; privacy is not guaranteed. "
            "Set HANDSOFF_ALLOW_REMOTE_OLLAMA=1 to explicitly allow it",
            _log_metadata(OLLAMA_BASE, "endpoint"),
        )
        _REMOTE_OLLAMA_WARNED = True
    if not _ollama_remote_opted_in():
        raise RuntimeError(
            "Ollama is configured on a non-loopback host; refusing to send "
            "private conversation data. Set HANDSOFF_ALLOW_REMOTE_OLLAMA=1 "
            "to explicitly allow remote Ollama."
        )


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler whose active and backup files remain owner-only."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)

    def doRollover(self) -> None:
        super().doRollover()
        for path in (self.baseFilename, *(f"{self.baseFilename}.{i}" for i in range(1, self.backupCount + 1))):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        # 1 MB per file, 2 rotations: the state dir can't grow forever
        handlers.append(_PrivateRotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=2, delay=True))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    privacy = _PrivacyLogFilter()
    for handler in handlers:
        handler.addFilter(privacy)
    if _ollama_remote():
        log.warning(
            "Ollama endpoint is non-loopback; conversation privacy is not "
            "guaranteed and remote use requires HANDSOFF_ALLOW_REMOTE_OLLAMA=1"
        )


_NOTIFY_DELAY = 0.5        # batch window: repeats inside it fold into one popup
_NOTIFY_BURST = 8          # distinct popups per batch before summarizing
_NOTIFY_MAX_PENDING = 64   # distinct texts kept per batch (rest → overflow)
_NOTIFY_LOCK = threading.Lock()
_NOTIFY_STATE: dict = {"pending": {}, "overflow": 0, "timer": None}

_READER_APP_COOLDOWN = 60.0  # one spoken digest per app per minute, max
_READER_COOLDOWN_LOCK = threading.Lock()
_READER_APP_LAST: dict[str, float] = {}

_ANNOUNCE_LOCK = threading.Lock()  # serialize ALL _speak playback (no overlap)
# Voice-preview cap: `--ptt say` is driven by a GUI button, but it is still a
# socket, so an unbounded payload would let one caller queue minutes of speech.
_SAY_PREVIEW_MAX = 300
_ANNOUNCE_CANCEL = threading.Event()  # cancels the previous announcement's playback
_ANNOUNCE_CANCEL_LOCK = threading.Lock()  # guards _ANNOUNCE_CANCEL rebind (no live race)


def _notify_send(text: str) -> None:
    """One notify-send popup; best-effort, never raises."""
    try:
        subprocess.run(
            ["notify-send", "-a", APP_NAME, "handsoff", text[:300]],
            timeout=5, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _notify_flush() -> None:
    """Emit one batch: ≤BURST popups + a single overflow summary. Never raises."""
    try:
        with _NOTIFY_LOCK:
            st = _NOTIFY_STATE
            st["timer"] = None
            batch = st["pending"]
            overflow = st["overflow"]
            st["pending"] = {}
            st["overflow"] = 0
        items = list(batch.items())
        for msg, n in items[:_NOTIFY_BURST]:
            _notify_send(f"{msg} ×{n}" if n > 1 else msg)
        dropped = overflow + sum(n for _, n in items[_NOTIFY_BURST:])
        if dropped:
            _notify_send(
                f"handsoff: {dropped} more notification"
                f"{'s' if dropped != 1 else ''} (burst folded into this summary)")
    except Exception:
        pass


def _notify_reset() -> None:
    """Clear coalescing + reader-cooldown state (test hook / manual quiet)."""
    try:
        with _NOTIFY_LOCK:
            t = _NOTIFY_STATE.get("timer")
            _NOTIFY_STATE["timer"] = None
            _NOTIFY_STATE["pending"] = {}
            _NOTIFY_STATE["overflow"] = 0
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
        with _READER_COOLDOWN_LOCK:
            _READER_APP_LAST.clear()
    except Exception:
        pass


def notify(text: str) -> None:
    """Desktop notification with swarm coalescing; best-effort, never raises.

    Identical repeats inside one short window fold into a single popup with
    a ×N suffix; distinct-message bursts are capped per window with the
    overflow folded into one summary popup (never dropped silently).
    Thread-safe; an isolated notify still sends exactly one popup.
    """
    try:
        msg = str(text or "")[:300]
        if not msg:
            return
        with _NOTIFY_LOCK:
            st = _NOTIFY_STATE
            st["pending"][msg] = st["pending"].get(msg, 0) + 1
            if len(st["pending"]) > _NOTIFY_MAX_PENDING:
                # drop the oldest distinct text into the overflow count
                old = next(iter(st["pending"]))
                st["overflow"] += st["pending"].pop(old)
            if st["timer"] is None:
                t = threading.Timer(_NOTIFY_DELAY, _notify_flush)
                t.daemon = True
                st["timer"] = t
                t.start()
    except Exception:
        pass


# core.bubble is application-free, so its one notifier comes from here — the
# context menu's "settings app not installed" line. Bound after `notify` exists
# rather than at the load site, which runs earlier in this module's body.
_core_bubble.notify = notify


# ------------------------------------------------------------------- system prompt

# teach the model about user-whitelisted extra commands (e.g. "date, free, grep")
_extras = [c.strip() for c in SETTINGS["extra_allowed_commands"] if c.strip()]
_EXTRAS_NOTE = (
    " The user has also whitelisted: " + ", ".join(_extras) + "."
    if _extras else ""
)

SYSTEM_PROMPT = f"""You are {_wake_name()}, a voice assistant living as a small round bubble on the user's Arch Linux desktop running the niri Wayland compositor. The user holds the bubble, speaks, then releases; you answer out loud via TTS.

WAKE WORD
- You are called "{_wake_name()}" (or "hey {_wake_name()}").
- In hands-free mode you only hear utterances that passed the wake gate. Answer normally; never comment on the wake word.

SPEAKING STYLE
- Your reply is spoken aloud: answer in ONE short sentence (max 25 words), plain text. No markdown, no lists, no emoji. Never mention tools or code unless explicitly asked.
- NEVER repeat or echo the user's words back. They can hear themselves; always answer with new, useful content.

VOICE STOP
- A bare "stop", "quiet", "shut up", "cancel", "nevermind" (alone, not part of a longer request) silences you instantly — playback cuts and no reply is spoken. Never answer those words; they are handled automatically.

DESKTOP CONTROL — run_command
Only these programs are allowed: pactl, playerctl, brightnessctl, niri, spawn, echo, cat, ls, pwd, notify-send, read-only system probes (ps, free, uptime, df, ss, nvidia-smi), read-only git (git status, git diff, git log, git show, git branch, git remote), cargo builds/tests (cargo build, cargo check, cargo test, cargo clippy), and your restart script {RESTART_SCRIPT}.{_EXTRAS_NOTE}
Useful examples:
- Volume: pactl set-sink-volume @DEFAULT_SINK@ -10%  (also +10%, 50%, mute)
- Media: playerctl play-pause, playerctl next, playerctl previous
- Brightness: brightnessctl set 30%
- Windows: niri msg action focus-window-right, focus-window-left, focus-workspace-down, focus-workspace-up, move-window-right, toggle-window-floating, maximize-column, overview
- Launch an app: niri msg action spawn -- alacritty
Never attempt destructive or unsafe commands (sudo, rm, pacman, shutdown, ...). If a request is unsafe, refuse politely in one short sentence.
- Long builds: start_command = background job; poll job_status; finish announced. Same whitelist as run_command.
- handsoff_doctor self-reports hashes/Ollama/mic/niri/systemd. "CONFIRM REQUIRED": NEXT turn confirm_action. "DRY-RUN": nothing ran.

FILES
- read_file(path): read any text file, including your own source code.
- edit_file(path, content): replace a file's whole content. Allowed: own source, sibling split modules, files inside {CONFIG_DIR}/ .

TYPING INTO APPS — type_text / press_keys / copy_text / paste_text
You can type into the FOCUSED window of the desktop (chat boxes, editors, forms).
- type_text(text): types literal text into the focused input (newlines allowed).
- press_keys(combo): presses a key combo like "enter", "ctrl+c", "ctrl+v".
IMPORTANT:
- Call these tools DIRECTLY, never via run_command; the window is already focused. Just type and report what you typed.
- REFUSED means 'not whitelisted', NEVER 'not installed'.
- Never type into a terminal. To replace input: press_keys "ctrl+a" then type_text; to send: type_text then "enter". For a specific app: focus_window first.

OPERATOR — clicking UI elements (only if the 'operator' permission is enabled)
- To click something on screen: run screen_elements (lists numbered text elements with positions), then click_element with the number or (part of the) text. Prefer this over guessing pixels.
- click_at(x, y) is the fallback for icon-only UI. Re-run screen_elements after the screen changes — element numbers go stale.
- A REFUSED click means no 'operator' permission: say so and stop.
- Max a few clicks per request: act, re-scan, report. If it is not working, say what you see and stop.

REMINDERS — set_reminder, list_reminders, cancel_reminder, snooze_reminder, calendar_month
- "remind me to X in N minutes" → set_reminder(wake_name="X", when_due="in N minutes").
- "remind me to X at 18:30" → set_reminder(wake_name="X", when_due="18:30") — HH:MM means the NEXT occurrence (rolls to tomorrow); for a date use "YYYY-MM-DD HH:MM".
- "every day at 9" → repeat_hours=24 (168=weekly). Reminders persist across restarts; anything missed while off is announced at startup.
- Snooze: within ~90s after a one-off fires, a bare "snooze [N minutes]" is handled automatically — no tool call needed. snooze_reminder(name, minutes) re-arms a pending one.
- Before cancel_reminder with an uncertain name, use list_reminders. calendar_month answers "what weekday is the 24th".

WAKE BEHAVIOUR
- When the wake word is required, the user addresses you by name (or the audio spotter detects the keyword). After engaging you answer freely for the engagement window, then go quiet until named again.

MEMORY
- You remember durable facts about the user across sessions: their name, family, likes/dislikes, home, work, pets, favorites. They arrive at the end of the conversation as "Facts you remember about the user".
- Use them naturally: "what's my sister's name" → answer from memory. Never invent a fact that is not listed; if it isn't there, say you don't know and ask.

CALENDAR — read_calendar
- "what\u2019s on my calendar today?" → read_calendar(days=1). If no ICS calendar is configured, say so and suggest handsoff Settings — never invent events.

MUSIC — media_play, media_control, media_volume, now_playing, search_library
- Music lives in MPD. "play music" → media_play() (resume/shuffle). "play Samira Said" → media_play(query=...). "pause"/"next"/"stop the music" → media_control. "volume 30" → media_volume. "what's this song?" → now_playing.
- Vague requests: search_library first. MPD down? Report: systemctl --user start mpd.

AMBIENT ASSISTANCE — notification_reader, pomodoro, watch_file, watch_process
- Notifications are private and OFF by default. Only enable notification_reader when the user explicitly asks; it reads future desktop notifications aloud and supports a comma-separated mute list.
- pomodoro(action="start", work_minutes=25, break_minutes=5) is a repeating work/break timer (status/stop to inspect); announce transitions briefly.
- watch_file and watch_process are bounded, stoppable monitors. Start them only when asked, stop them when no longer useful, and never claim a watcher is active unless the tool confirms it.

EYES & APPS — see_screen, read_screen_text, open_app, focus_window
- see_screen shows the screen as an image; read_screen_text reads text via OCR. Use when the user says "this", "that error", "on my screen".
- open_app waits for the window and reports it; wait_for_window or focus_window before typing. Re-run screen_elements after scrolling; wait before acting on a fresh dialog. Never open_app into a terminal to run commands — a run_command bypass, forbidden.

WINDOW MANAGEMENT — close_window
- 'close firefox' → close_window(app="firefox"); 'close this window' → close_window(app="this"). Polite close (unsaved work prompts). Report what the tool returned; never force-kill.

WORKSPACES — workspace
- 'go to workspace 2' → workspace(action="go", target="2"); 'move firefox to 2' → workspace(action="move", target="firefox to 2"); 'next'/'prev'/'list my workspaces' → workspace(action=...).
- Numbers, niri names and Settings aliases (e.g. 'code') all work. After 'move', the view doesn't follow unless asked.

DESKTOP HOTKEYS — press_hotkey
- press_hotkey fires desktop shortcuts: 'Mod+E' (files), 'Mod+Return' (terminal), 'Mod+F' (fullscreen), 'Mod+V' (talk), 'Mod+Shift+S' (settings). press_keys is for in-app chords; press_hotkey for Mod-prefixed system shortcuts. Never use it to type text.

KNOWLEDGE — get_weather, web_search, lookup_fact, get_datetime
- Anything after your training cutoff: USE THE TOOLS, never guess. Weather → get_weather; "who/what is X" → lookup_fact; recent info → web_search. Say where info came from when it matters.
- Current date/time is provided in the conversation; use get_datetime only when asked for the exact time.

SELF-MODIFICATION
- You ARE the program: {SELF_PATH} plus sibling split modules (settings_schema.py, hardware.py, handsoff-settings.py, core/settings.py, core/__init__.py).
- To change yourself ("make your bubble pink", "add a mute option", "speak faster"):
  1) read_file the file,
  2) edit_file it with the complete new content — valid Python, minimal diff (under 500KB; {SELF_PATH} edits keep '{SELF_MARKER}' exactly),
  3) verify: python -m py_compile on the file and pytest tests/test_policy.py -q, then confirm briefly and run_command "{RESTART_SCRIPT}" to restart.
- Simple tweaks (volume, brightness, windows): use commands, never rewrite yourself."""



# TOOLS is instantiated right after the ToolBelt class body (it needs the
# decorated methods to exist).

# ------------------------------------------------------------------- ollama client


_TOOLS_SUPPORTED = True


def _brain_deps() -> dict:
    return dict(base=OLLAMA_BASE, model=OLLAMA_MODEL, num_ctx=OLLAMA_NUM_CTX,
                guard=_guard_ollama_endpoint, logger=log,
                state={"tools_supported": _TOOLS_SUPPORTED},
                urlopen=urllib.request.urlopen)


def ollama_available() -> bool:
    return _brain.ollama_available(base=OLLAMA_BASE,
                                   guard=_guard_ollama_endpoint,
                                   urlopen=urllib.request.urlopen)


def ollama_chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    return _brain.ollama_chat(messages, tools, **_brain_deps())


def ollama_chat_stream(messages: list[dict], q: "queue.Queue[str | None]",
                       cancel: threading.Event | None = None,
                       tools: list[dict] | None = None) -> dict:
    return _brain.ollama_chat_stream(messages, q, cancel, tools, **_brain_deps())


# ------------------------------------------------------------------------ audio in
# Audio ownership ends at these primitives. ContinuousListener remains in this
# file for now because it owns Assistant/UI lifecycle state and health hooks.


# ---------------------------------------------------------------------------- tools


# ------------------------------------------------------------------ knowledge

_HTTP_UA = "handsoff/1.0 (local voice assistant)"


def _http_get(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000)


# `core.web` owns the search router and the page reader. It is application-free
# and takes RESOLVERS rather than values, which is not decoration: `_http_get` is
# monkeypatched by the whole suite and `searxng_url` changes on a live settings
# save, so binding the function object or the string once would leave both
# silently stale — a patched fetch would be ignored and a reloaded setting would
# never be read. One network seam for searching AND reading.
_web = _load_module("web")
_web.configure(
    http_get=lambda url, timeout=10.0: _http_get(url, timeout),
    searxng_url=lambda: str(SETTINGS.get("searxng_url") or ""),
    logger=log,
)


_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}


def _geocode(place: str) -> tuple[float, float, str] | None:
    q = urllib.parse.quote(place.strip())
    data = json.loads(_http_get(
        "https://geocoding-api.open-meteo.com/v1/search?name=" + q
        + "&count=1&language=en&format=json"))
    res = (data.get("results") or [None])[0]
    if not res:
        return None
    return (float(res["latitude"]), float(res["longitude"]),
            str(res.get("name", place)) + (", " + str(res["country"]) if res.get("country") else ""))


def _ddg_lite(query: str) -> list[tuple[str, str]]:
    """(title, snippet) pairs from DuckDuckGo Lite — the general fallback.

    A thin delegation to the backend in `core.web`, so the search tool, the
    proactive briefing and the severe-weather queries all read one page with one
    parser. The tuple shape and the FIVE-item cap are kept because that is what
    the callers consume: the router's own limit is four, and the briefing's
    count was silently cut to four until a guard caught it.
    """
    return [(r.title, r.snippet) for r in _web.ddg_search(query, limit=5)]


def _wiki_search(query: str) -> list[tuple[str, str]]:
    """(title, snippet) pairs from the MediaWiki search API — same delegation,
    same reason: one implementation of "ask Wikipedia" instead of two."""
    return [(r.title, r.snippet) for r in _web.wikipedia_search(query)]


# -- world events: breaking news + severe-weather headlines -------------------

WORLD_EVENTS_FILE = STATE_DIR / "world-events-seen.json"
WORLD_EVENTS_MAX = 64
WORLD_EVENTS_TTL_S = 36 * 3600  # inside the 24-48h dedup window
_WORLD_EVENTS_LOCK = threading.Lock()

_URGENT_WORDS = ("earthquake", "tsunami", "hurricane", "tornado", "flood",
                 "wildfire", "volcano", "terror", "missile", "airstrike",
                 "nuclear")
_URGENT_PHRASES = ("severe thunderstorm", "tornado warning", "flood warning",
                   "severe heat warning", "heat warning",
                   "severe weather warning")
_SEVERE_WMO = {95, 96, 99, 65, 75, 82}
_SEVERE_WIND_KMH = 75.0


def _world_is_urgent(title: str, snippet: str = "") -> bool:
    """Deterministic severity: keyword/phrase match, no model judgment."""
    t = f"{title or ''} {snippet or ''}".lower()
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in _URGENT_WORDS):
        return True
    return any(p in t for p in _URGENT_PHRASES)


def _world_norm_key(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().lower())[:120]


def _load_world_seen() -> dict:
    """{norm_key: epoch} of spoken/announced events; corrupt → {}.

    The reference time is the NEWEST stamp in the file whenever that is ahead
    of the wall clock. Expiry is measured from it, so a backwards clock step
    (NTP correction, DST fix, VM resume, a manually set clock) cannot expire
    every entry at once and make the bubble announce headlines the user has
    already heard. Entries are still bounded by the same TTL, so the store
    cannot grow or pin itself forever.
    """
    try:
        data = json.loads(WORLD_EVENTS_FILE.read_text(encoding="utf-8"))
        stamps: dict[str, float] = {}
        for k, v in data.items():
            key = str(k).strip()
            if not key:
                continue
            try:
                stamps[key] = float(v)
            except (TypeError, ValueError):
                continue
        if not stamps:
            return {}
        now = max([time.time()] + list(stamps.values()))
        return {k: v for k, v in stamps.items()
                if v > now - WORLD_EVENTS_TTL_S}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _world_seen(key: str) -> bool:
    return bool(key) and key in _load_world_seen()


def _world_mark_seen(titles) -> None:
    """Record spoken/announced headlines (briefing + proactive ONLY)."""
    try:
        with _WORLD_EVENTS_LOCK:
            seen = _load_world_seen()
            now = time.time()
            for t in titles or ():
                k = _world_norm_key(t)
                if k:
                    seen[k] = now
            while len(seen) > WORLD_EVENTS_MAX:
                seen.pop(min(seen, key=seen.get))
            WORLD_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _core_settings._atomic_private_write(WORLD_EVENTS_FILE, json.dumps(seen))
    except Exception:
        log.exception("cannot persist world-events seen store")


def _world_news_queries() -> list:
    """Fixed queries only — the model never chooses them."""
    queries = ["breaking world news"]
    place = str(SETTINGS.get("home_place", "")).strip()
    if place:
        variant = place.split(",")[-1].strip() if "," in place else place
        if variant and variant.lower() not in queries[0]:
            queries.append(f"breaking news {variant}")
    return queries


def _severe_weather_events() -> list:
    """Severe-weather signal from open-meteo (needs home_place)."""
    place = str(SETTINGS.get("home_place", "")).strip()
    if not place:
        return []
    geo = _geocode(place)
    if not geo:
        return []
    lat, lon, where = geo
    data = json.loads(_http_get(
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        "&current=weather_code,wind_speed_10m&forecast_days=1&timezone=auto"))
    cur = data["current"]
    code = int(cur["weather_code"])
    wind = float(cur["wind_speed_10m"])
    if code in _SEVERE_WMO or wind > _SEVERE_WIND_KMH:
        desc = _WMO.get(code, "severe weather")
        return [{"title": (f"Severe weather in {where}: {desc}, "
                           f"wind {wind:.0f} km/h"),
                 "snippet": "", "urgent": True, "source": "weather"}]
    return []


def _world_events(kind: str = "all", limit: int = 5) -> tuple:
    """(events, degraded): fixed-query world headlines. Shared by briefing,
    proactive warnings and the world_events tool. Never raises."""
    try:
        limit = max(1, min(int(limit or 5), 5))
    except (TypeError, ValueError):
        limit = 5
    events: list = []
    degraded = False
    if kind in ("news", "all"):
        try:
            for query in _world_news_queries():
                for title, snip in _ddg_lite(query):
                    events.append({"title": title, "snippet": snip,
                                   "urgent": _world_is_urgent(title, snip),
                                   "source": "news"})
                    if len(events) >= limit:
                        break
                if len(events) >= limit:
                    break
        except Exception:
            log.warning("world news fetch failed", exc_info=True)
            degraded = True
    if kind in ("weather", "all") and len(events) < limit:
        try:
            events.extend(_severe_weather_events())
        except Exception:
            log.warning("world weather fetch failed", exc_info=True)
            degraded = True
    out, keys = [], set()
    for e in events:
        e["key"] = _world_norm_key(e.get("title", ""))
        if not e["key"] or e["key"] in keys:
            continue
        keys.add(e["key"])
        out.append(e)
        if len(out) >= limit:
            break
    return out, degraded




_JSON_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean"}





# -- reminders: persisted, epoch-based, survive restarts --------------------------

REMINDERS_FILE = STATE_DIR / "reminders.json"
REMINDERS_LOCK = threading.RLock()   # serializes EVERY read-modify-write
MAX_REMINDERS = 64
MAX_REMIND_DAYS = 365


def _reminder_store() -> _core_assistant.ReminderStore:
    """Bind the store to the CURRENT module globals before every use.

    A store that captured its path at import time would keep writing the real
    reminders.json after a test (or any caller) rebinds REMINDERS_FILE — the
    exact accident this indirection exists to prevent.
    """
    _REMINDER_STORE.path = REMINDERS_FILE
    _REMINDER_STORE.lock = REMINDERS_LOCK
    return _REMINDER_STORE


def _load_reminders() -> list[dict]:
    """Read reminders.json, dropping entries that are malformed."""
    return _reminder_store().load()


def _save_reminders(items: list[dict]) -> None:
    """Caller MUST hold REMINDERS_LOCK + the reminders.json sidecar flock:
    two concurrent writers would corrupt each other's read-modify-write."""
    _reminder_store().save(items)


def _update_reminders(mutate) -> list[dict]:
    """One serialized read-modify-write transaction: load → mutate → save,
    under REMINDERS_LOCK (threads) plus the reminders.json sidecar flock
    via _settings_file_lock (processes). Every mutation goes through this."""
    return _reminder_store().update(mutate)


def _fmt_due_in(due_epoch: float, now: float | None = None) -> str:
    """Human duration until an absolute due time: 'in 2 hours 5 minutes'."""
    s = max(0, int(round(due_epoch - (time.time() if now is None else now))))
    parts: list[str] = []
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        n, s = divmod(s, size)
        if n:
            parts.append(f"{n} {unit}" + ("s" if n != 1 else ""))
    if s and len(parts) < 2:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return "in " + (" ".join(parts) if parts else "0 seconds")


def _fmt_when(due_epoch: float) -> str:
    """Absolute local time of a due epoch, with an in-… suffix.

    Locale-independent on purpose: the TTS voice is English, and a German
    LC_TIME must not change what gets spoken."""
    due = datetime.datetime.fromtimestamp(due_epoch)
    return (f"{_core_calendar._DAY_NAMES[due.weekday()]} {due.day:02d} "
            f"{_core_calendar._MONTH_NAMES[due.month - 1]} {due.hour:02d}:{due.minute:02d}"
            f" ({_fmt_due_in(due_epoch)})")


def _next_occurrence(h: int, mi: int, se: int, now: float) -> float:
    """Next epoch at local HH:MM(:SS); today if still ahead, else tomorrow.

    The day step is a calendar day, NEVER +86400 s: an 86400 s add across a
    DST shift lands an hour off (fires at 23:00 or 01:00 wall time)."""
    due = datetime.datetime.now().replace(hour=h, minute=mi, second=se,
                                          microsecond=0)
    if due.timestamp() <= now:
        due += datetime.timedelta(days=1)   # calendar day, DST-safe
    return due.timestamp()



# -- audio-level wake spotter (openWakeWord, optional) -----------------------------

_spotter_model = None
_spotter_failed = False
_SPOTTER_FRAME = 1280                   # 80 ms of 16 kHz int16 audio


def _get_spotter():
    """Lazy-load the openWakeWord Model once; None if unavailable."""
    global _spotter_model, _spotter_failed
    if _spotter_model is not None or _spotter_failed:
        return _spotter_model
    try:
        from openwakeword.model import Model
        models = SETTINGS.get("spotter_models") or []
        _spotter_model = Model(wakeword_models=models) if models else Model()
        log.info("wake spotter loaded: %s",
                 sorted(getattr(_spotter_model, "model_names",
                                _spotter_model.models.keys() if hasattr(
                                    _spotter_model, "models") else [])) or "stock")
    except Exception:
        _spotter_failed = True
        _spotter_model = None
        log.exception("wake spotter unavailable — transcript gate stays active")
    return _spotter_model


class WakeSpotter:
    """Streaming openWakeWord detector with a 2-second pre-roll buffer.

    feed() consumes int16 frames at 16 kHz and returns (True, audio) exactly
    once per detection, where audio = pre-roll + the speech captured since the
    hit. The caller keeps feeding to gather trailing speech after the hit."""

    PREROLL_S = 2.0
    MAXWAIT_S = 8.0                 # give up collecting after this long
    SCORE_HIT = 0.5
    SCORE_HOLD = 0.35

    def __init__(self) -> None:
        self._buf: list = []        # pre-roll ring as a list of frames
        self._max_pre = int(self.PREROLL_S * SAMPLE_RATE // _SPOTTER_FRAME)
        self._speech: list = []     # chunks collected after a hit
        self._armed = False         # a hit is pending collection
        self._collected = 0.0       # seconds of audio since the hit
        self._residual = np.empty(0, dtype=np.int16)   # sub-chunk carryover

    def feed(self, frame) -> "tuple[bool, list]":
        """Process one 16 kHz int16 frame (any length). Returns (fired, audio).

        predict() runs EXACTLY once per 1280-sample chunk — the model is
        stateful and double-feeding corrupts its features. Leftover samples
        are carried to the next call (the mic delivers 1024-sample frames)."""
        model = _get_spotter()
        if model is None:
            return False, []
        data = np.concatenate([self._residual, np.asarray(frame).reshape(-1)]) \
            if len(self._residual) else np.asarray(frame).reshape(-1)
        n = _SPOTTER_FRAME
        nfull = len(data) // n
        for i in range(nfull):
            chunk = data[i * n:(i + 1) * n]
            self._buf.append(chunk)
            del self._buf[:-self._max_pre]
            try:
                scores = model.predict(chunk)
            except Exception:
                # a predict failure corrupts openWakeWord's internal stream:
                # drop the residual too, or the next feed() re-feeds stale
                # samples and every subsequent score is garbage
                log.exception("wake spotter predict failed")
                self._residual = np.empty(0, dtype=np.int16)
                return False, []
            hot = max(scores.values()) if scores else 0.0
            if not self._armed and hot > self.SCORE_HIT:
                self._armed = True
                self._collected = 0.0
                self._speech = list(self._buf)     # pre-roll included
            elif self._armed:
                self._speech.append(chunk)
                self._collected += n / SAMPLE_RATE
                if self._collected >= self.MAXWAIT_S or (
                        hot < self.SCORE_HOLD and self._collected > 1.0):
                    self._armed = False
                    out = self._speech
                    self._speech = []
                    return True, out
        self._residual = data[nfull * n:]
        return False, []




# Assistant collaborators (pomodoro/notifications/reminders/ticks): small
# state machines with explicit deps; the Assistant keeps thin delegates.
# One handle, not five re-exported names: the collaborators are reached as
# `_core_assistant.NotificationReader` where the app uses them, and the app's
# namespace stops being a second copy of core.assistant's public names.
_core_assistant = _load_module("assistant")

# The reminder queue's storage logic (parsing, serialized transactions,
# startup catch-up, due-split arithmetic) lives in core.assistant.ReminderStore;
# this instance binds it to the app's real paths, locks and writers, and the
# thin aliases below keep the historical H.* names the ToolBelt's `_dep()`
# contract and the tests use.
_REMINDER_STORE = _core_assistant.ReminderStore(
    REMINDERS_FILE,
    lock=REMINDERS_LOCK,
    file_lock=_core_settings._settings_file_lock(),
    backup=_backup_runtime_json,
    write=_core_settings._atomic_private_write,
    logger=log,
    clock=time.time,
)
# Calendar parsing lives in core.calendar (stdlib-only, no Qt/Assistant). One
# handle again: the briefing, the ToolBelt's host-dependency fallback and the
# tests all reach the helpers as `_core_calendar.*` / `core.calendar.*`.
_core_calendar = _load_module("calendar")


def _record_mic_event(from_state: str, to_state: str) -> None:
    """Append one mic-state transition to the mic-health state file (best
    effort: diagnostics must never break the audio path)."""
    try:
        now = time.time()
        with _MIC_EVENTS_LOCK:
            doc = _load_mic_events()
            doc.setdefault("events", []).append({
                "t": now, "from": from_state, "to": to_state,
                "device": SETTINGS.get("mic_device") or "system default",
            })
            doc["events"] = doc["events"][-MIC_EVENTS_MAX:]
            _core_settings._atomic_private_write(MIC_EVENTS_FILE, json.dumps(doc))
    except Exception:
        log.exception("cannot record mic event")


def _load_mic_events() -> dict:
    """Read the mic-events state file; any corruption or absence -> {}."""
    try:
        doc = json.loads(MIC_EVENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _recent_mic_problems(since: float = 0.0) -> str:
    """Human summary of degraded mic states (silent / open-failing / stalled)
    recorded at or after `since`. Returns '' when the file is absent, empty,
    corrupt or holds only healthy transitions — the briefing omits the
    section then."""
    doc = _load_mic_events()
    events = doc.get("events")
    if not isinstance(events, list):
        return ""
    recent = [e for e in events
              if isinstance(e, dict) and isinstance(e.get("t"), (int, float))
              and e["t"] >= since]
    if not recent:
        return ""
    degraded = ("silent", "open-failing", "stalled")
    counts: dict[str, int] = {}
    last_t = 0.0
    last_to = ""
    for e in recent:
        to = str(e.get("to", ""))
        if to in degraded:
            counts[to] = counts.get(to, 0) + 1
            if e["t"] > last_t:
                last_t, last_to = e["t"], to
    if not counts:
        return ""
    parts = [f"{n}x {s}" for s, n in sorted(counts.items())]
    return (f"Microphone problems since the last briefing: "
            f"{', '.join(parts)}; most recent: {last_to} "
            f"({_fmt_dur(time.time() - last_t)} ago)")


def _cap_label(name) -> str:
    """Human name for a registry in the refusal record."""
    return _CAP_LABELS.get(str(name or ""), str(name or "unknown"))


def _record_cap_refusal(report: dict) -> None:
    """Append one cap refusal to the state file (best effort).

    Best effort for the same reason the mic recorder is: a diagnostic must
    never be able to fail the path it describes, and this one runs inside a
    refusal that has already told the model nothing was created.
    """
    try:
        entry = {k: report.get(k) for k in _CAP_EVENT_FIELDS}
        with _CAP_EVENTS_LOCK:
            doc = _load_cap_refusals()
            events = doc.get("events")
            events = list(events) if isinstance(events, list) else []
            events.append(entry)
            doc["events"] = events[-CAP_EVENTS_MAX:]
            doc["count"] = int(doc.get("count") or 0) + 1
            by = doc.get("by_registry")
            by = dict(by) if isinstance(by, dict) else {}
            name = str(entry.get("registry") or "?")
            by[name] = int(by.get(name) or 0) + 1
            doc["by_registry"] = by
            _core_settings._atomic_private_write(CAP_EVENTS_FILE, json.dumps(doc))
    except Exception:
        log.exception("cannot record cap refusal")


def _load_cap_refusals() -> dict:
    """Read the cap-refusal state file; corruption or absence -> {}."""
    try:
        doc = json.loads(CAP_EVENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _cap_refusal_summary() -> dict:
    """Structured view of past refusals: totals per cap + the most recent.

    `count` is cumulative; `events` in the file is a bounded ring, so the
    totals survive refusals the ring has since dropped.
    """
    doc = _load_cap_refusals()
    events = [e for e in (doc.get("events") or []) if isinstance(e, dict)]
    by = doc.get("by_registry") if isinstance(doc.get("by_registry"), dict) else {}
    return {
        "count": int(doc.get("count") or 0),
        "by_registry": {str(k): int(v or 0) for k, v in by.items()},
        "last": events[-1] if events else None,
    }


def _cap_refusal_note(registry: str = "") -> str:
    """One human line about refusals at a cap; '' when there have been none.

    `registry` narrows it to one cap ('job'); empty covers every cap. The
    journal, job_status and the doctor all render from here, so three surfaces
    cannot end up describing the same refusals differently.
    """
    summary = _cap_refusal_summary()
    if not summary["count"]:
        return ""
    last = summary["last"] or {}
    at = last.get("at")
    age = ""
    if isinstance(at, (int, float)) and at > 0:
        age = f" ({_fmt_dur(max(0.0, time.time() - at))} ago)"
    detail = str(last.get("detail") or "a request")
    if registry:
        total = int(summary["by_registry"].get(registry) or 0)
        if not total:
            return ""
        return (f"the {_cap_label(registry)} cap has refused {total} request(s) "
                f"— most recent {detail}{age} with "
                f"{last.get('held', '?')}/{last.get('cap', '?')} held. A "
                f"refused call starts nothing.")
    parts = ", ".join(f"{n}x {_cap_label(k)}"
                      for k, n in sorted(summary["by_registry"].items()))
    return (f"cap refusals: {summary['count']} recorded ({parts}); most recent "
            f"{_cap_label(last.get('registry'))} — {detail}{age}")


def _mark_briefing_delivered() -> None:
    """Stamp the mic-health state file with the briefing time, so the next
    briefing reports only problems since then (best effort)."""
    try:
        with _MIC_EVENTS_LOCK:
            doc = _load_mic_events()
            doc["last_briefing"] = time.time()
            _core_settings._atomic_private_write(MIC_EVENTS_FILE, json.dumps(doc))
    except Exception:
        log.exception("cannot stamp briefing time")


def _today_events_summary() -> str:
    """Compact today-events line for the morning briefing ('' if none)."""
    try:
        sources = SETTINGS.get("calendar_ics") or []
        if not sources:
            return ""
        now = datetime.datetime.now()
        win_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        win_end = win_start + datetime.timedelta(days=1)
        events: list[dict] = []
        for src in sources:
            events.extend(_core_calendar._ics_events_from_text(_core_calendar._ics_fetch(src),
                                                win_start, win_end))
        return _core_calendar._fmt_events(events)
    except Exception:
        log.exception("briefing calendar summary failed")
        return ""



def _parse_duration(text: str) -> float | None:
    """'in 2 hours 5 minutes' / '45 min' / '3 days' / 'a week' → seconds, or None."""
    t = text.strip().lower()
    if t.startswith("in "):
        t = t[3:]
    units = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
        "w": 604800, "week": 604800, "weeks": 604800,
    }
    total = 0.0
    matched = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?|a|an|one|two|three)\s*([a-z]+)", t):
        if unit not in units:
            return None
        n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(num) or float(num)
        total += n * units[unit]
        matched = True
    if not matched or total <= 0:
        return None
    return total


def _fmt_dur(seconds: float) -> str:
    """'2 hours', '5 minutes', '90 seconds' — shared human duration."""
    s = int(round(seconds))
    if s >= 3600 and s % 3600 == 0:
        return f"{s // 3600} hour" + ("s" if s > 3600 else "")
    if s >= 60 and s % 60 == 0:
        return f"{s // 60} minute" + ("s" if s > 60 else "")
    return f"{s} seconds"


def _due_reminders(items: list[dict], now: float) -> tuple[list[dict], list[dict]]:
    """Split into (fired, kept); the arithmetic lives in core.assistant so the
    store, the worker tick and the tests share one implementation."""
    return _core_assistant.split_due_reminders(items, now)


def _take_missed_reminders() -> list[dict]:
    """Pop reminders that came due while we were off (startup call)."""
    return _reminder_store().take_missed()


# -- snooze: while this offer is live, a bare "snooze" re-arms the just-fired
# one-off reminder without any LLM round-trip.
#
# Both offers are core.registry.Offer objects, which own the arm/read/consume
# sequence AND the lock behind it. They used to be bare dicts armed with
# clear()+update(): two steps, and a reader landing between them saw an EMPTY
# offer for one that exists (or paired one call's payload with another's
# deadline). There is deliberately no lock here to reach for any more.
_snooze_offer = _core_registry.Offer("snooze")
_kill_offer = _core_registry.Offer("kill")
SNOOZE_WINDOW_S = 90.0


def _mpc(*args: str, timeout: float = 8.0) -> str:
    """Run an mpc command against MPD and return stdout.

    Raises RuntimeError with a human-readable message when MPD is down or
    mpc is missing — callers surface that honestly to the user."""
    try:
        r = subprocess.run(["mpc", *args], capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("mpc is not installed") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("mpc timed out") from None
    if r.returncode != 0 and r.stderr.strip():
        err = r.stderr.strip().splitlines()[0]
        if "Connection refused" in err or "Failed to connect" in err:
            raise RuntimeError("MPD is not running — start it with: "
                               "systemctl --user start mpd")
        raise RuntimeError(f"mpc error: {err[:200]}")
    return r.stdout


_SPLIT_EDIT_FILES = frozenset(
    {"settings_schema.py", "hardware.py", "handsoff-settings.py"})
_SPLIT_EDIT_CORE = frozenset({"__init__.py", "settings.py"})


def _editable_roots() -> list[Path]:
    """Dirs whose split-module files the model may edit: the running copy's
    dir, the checkout source dir, and ~/.local/bin (deduped, in order)."""
    roots: list[Path] = []
    for cand in (SELF_PATH.parent, HOME / ".local" / "bin"):
        try:
            roots.append(cand.resolve())
        except OSError:
            roots.append(cand)
    repo = _repo_source_path()
    if repo is not None:
        try:
            roots.append(repo.resolve().parent)
        except OSError:
            roots.append(repo.parent)
    out, seen = [], set()
    for root in roots:
        if str(root) not in seen:
            seen.add(str(root))
            out.append(root)
    return out


def _classify_edit_path(p: Path) -> str:
    """'self' | 'split' | 'config' | '' for a resolved edit_file target."""
    try:
        self_resolved = SELF_PATH.resolve()
    except OSError:
        self_resolved = SELF_PATH
    if p == SELF_PATH or p == self_resolved:
        return "self"
    try:
        config_root = CONFIG_DIR.resolve()
    except OSError:
        config_root = CONFIG_DIR
    if config_root in p.parents:
        return "config"
    for root in _editable_roots():
        if p.parent == root and p.name in _SPLIT_EDIT_FILES:
            return "split"
        if (p.parent.name == "core" and p.parent.parent == root
                and p.name in _SPLIT_EDIT_CORE):
            return "split"
    return ""




# Phase 4e: core.lifecycle owns the turn primitives. The handle IS the seam —
# `_core_lifecycle.next_turn(...)` where the app needs it — so this namespace no
# longer carries `TurnState`/`next_turn` as a second copy (the inline fallback
# for pre-4e bundles went with them: the installer's manifest requires
# core/lifecycle.py, and a missing one is a loud ImportError instead of a second
# implementation silently diverging from the tested one).
_core_lifecycle = _load_module("lifecycle")

# Phase 4c: core.tools owns the extracted runtime. It receives a late-bound host
# proxy so existing module globals and monkeypatch seams stay live, and the app
# SUBCLASSES the belt to inject them — which is the only reason `ToolBelt` is a
# name in this namespace. The pre-4c ImportError fallback went with the
# re-exports: the installer's manifest requires core/tools.py, so a missing one
# is a loud ImportError instead of a stub belt answering "reinstall handsoff".
from types import SimpleNamespace as _SimpleNamespace
_core_tools = _load_module("tools")


#: Modules the host proxy may resolve a name from. Bound once, after every
#: handle exists (see `_ToolDependencies.__getattr__`).
_CORE_HANDLES: tuple = ()


class _ToolDependencies:
    """The host's live globals, with core's own names resolved IN core.

    A name the app still owns comes from here. One that lives in an extracted
    module is read from that module, so the app's namespace stops being a second
    copy of core's public names — `_dep()._ics_fetch`, `_dep().log_decision` and
    the rest keep resolving without handsoff.py re-exporting anything.
    """

    def __getattr__(self, name):
        if name == "DECISIONS_FILE":
            return DECISIONS_FILE
        if name == "CONFIG_DIR":
            return CONFIG_DIR
        if name == "subprocess":
            return subprocess
        try:
            return globals()[name]
        except KeyError:
            pass
        for mod in _CORE_HANDLES:
            try:
                return getattr(mod, name)
            except AttributeError:
                continue
        raise AttributeError(name)
_tool_dependencies = _ToolDependencies()
_CORE_HANDLES = (_core_calendar, _core_settings, _audio, _brain, _core_registry,
                 _core_assistant, _core_lifecycle, _core_tools)

_core_tools.time = time
_core_tools.json = json
_core_tools.shutil = shutil
_core_tools.os = os
_core_tools.Path = Path
_core_tools.log = log
_core_tools._DEFAULT_DEPS = _tool_dependencies
_core_tools._CURRENT.set(_tool_dependencies)


class ToolBelt(_core_tools.ToolBelt):
    """The application's belt: core's runtime with the live host injected."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("dependencies", _tool_dependencies)
        super().__init__(*args, **kwargs)


# core.tools builds its tool list through whatever ToolBelt it is given, so the
# subclass has to be published back BEFORE anything calls build_tools().
_core_tools.ToolBelt = ToolBelt
TOOLS = _core_tools.build_tools()


class _SpeechGate:
    """Energy-based speech detector with an adaptive noise floor.

    Speech starts when `start_frames` consecutive frames exceed
    max(noise_floor*2.5, threshold); it ends after `hangover_frames`
    quiet frames. The floor tracks the room while nobody speaks."""

    def __init__(self, threshold: int, start_frames: int = 2,
                 hangover_frames: int = 14) -> None:
        self.threshold = float(threshold)
        self.start_frames = start_frames
        self.hangover_frames = hangover_frames
        self.floor = max(50.0, self.threshold * 0.5)
        self.in_speech = False
        self._loud = 0
        self._quiet = 0

    def feed(self, rms: float) -> str:
        """Feed one frame's RMS; returns '', 'start' or 'end'."""
        if not self.in_speech:
            self.floor = 0.97 * self.floor + 0.03 * rms
            if rms > max(self.floor * 2.5, self.threshold):
                self._loud += 1
                if self._loud >= self.start_frames:
                    self.in_speech = True
                    self._quiet = 0
                    return "start"
            else:
                self._loud = 0
            return ""
        if rms > max(self.floor * 1.6, self.threshold * 0.7):
            self._quiet = 0
        else:
            self._quiet += 1
        if self._quiet >= self.hangover_frames:
            self.in_speech = False
            self._loud = 0
            return "end"
        return ""

    def reset(self) -> None:
        self.in_speech = False
        self._loud = self._quiet = 0


class ContinuousListener:
    """Always-on microphone gated by _SpeechGate for hands-free operation.

    Frames are discarded while the assistant speaks (its own TTS would
    re-trigger the gate) and while a push-to-talk press holds the bubble."""

    FRAME = 1024                      # 64 ms at 16 kHz
    MAX_UTTERANCE_S = 15.0
    MIN_UTTERANCE_S = 0.3
    REOPEN_S = 3.0                    # no frames at all → stream is dead
    SILENT_REOPEN_S = 45.0            # frames flowing but all zeros → wedged device
    MIC_SILENT_REPORT_S = 20.0        # report 'silent' BEFORE the 45s reopen resets the clock
    SELFHEAL_GRACE_S = 60.0           # degraded this long → restart the capture stream
    SELFHEAL_MAX = 3                  # restarts per streak before journal-only

    def __init__(self, assistant: "Assistant") -> None:
        self._assistant = assistant
        self._running = False
        self._run_id = 0              # generation token: invalidates stale threads
        self._spotter: "WakeSpotter | None" = None
        self._suspended = False
        self._discard = False
        self.gate_open = False           # VAD is currently collecting an utterance
        self._thread: threading.Thread | None = None
        self._stream: sd.InputStream | None = None
        self._frames_seen = 0            # watchdog: callback counter
        self._last_nonzero = 0.0         # watchdog: last frame above digital silence
        self._health_utt = 0             # hourly health line: utterances emitted
        self._health_opens_ok = 0        # hourly health line: successful stream opens
        self._health_opens_failed = 0    # hourly health line: failed open attempts
        self._health_open_device = ""    # device the last successful open used
        self._health_last_open = "never"  # human time of the last successful open
        self._health_state = ""          # last reported state (transition detection)
        self._health_next_summary = 0.0  # monotonic: next unconditional hourly line
        self._health_failing_since = None  # monotonic: open-failure streak start
        self._health_recovered_after = None  # seconds the last failure streak lasted
        self._health_stalled_since = None  # monotonic: zero-frames streak start
        self._ever_started = False       # health reporting starts with the first start()
        self._lock = threading.RLock()   # guards the health snapshot above
        # hourly "mic health" journal line — silent mic failures must be
        # visible without debug logging. Spawned ONCE here (never in start(),
        # which runs on every hands-free toggle): one reporter per process,
        # reporting state=stopped while hands-free is off.
        threading.Thread(target=self._health_loop, name="mic-health",
                         daemon=True).start()

    # -- hourly mic health line -------------------------------------------

    def _health_loop(self) -> None:
        """Greppable 'mic health' journal lines without debug logging: an
        unconditional summary every hour, PLUS an immediate line the moment
        the state changes (silent, stalled, open-failing, recovered, stopped).
        Degraded states log at WARNING, healthy ones at INFO."""
        while True:
            time.sleep(10.0)
            try:
                self._health_tick()
            except Exception:
                reporter = getattr(log, "exception", None)
                if not callable(reporter):
                    reporter = (getattr(log, "error", None)
                                or getattr(log, "warning", None))
                if not callable(reporter):
                    raise
                try:
                    reporter("mic health report failed", exc_info=True)
                except TypeError:
                    # Minimal test/runtime logger seams may not accept logging
                    # kwargs; still report the failure instead of killing the
                    # long-lived health thread in the error handler.
                    reporter("mic health report failed")

    def mic_snapshot(self) -> dict:
        """JSON-ready mic health dict (listener-owned fields); shares the
        state machine with the journal reporter via _health_state_now_locked."""
        with self._lock:
            return {
                "state": self._health_state_now_locked(),
                "device": (self._health_open_device
                           or (str(SETTINGS["mic_device"])
                               if SETTINGS["mic_device"] else "system default")),
                "rate": getattr(self, "_capture_rate", None) or None,
                "frames": self._frames_seen,
                "silent_for": (max(0.0, time.monotonic() - self._last_nonzero)
                               if self._frames_seen else None),
                "last_open": self._health_last_open,
                "opens_ok": self._health_opens_ok,
                "opens_failed": self._health_opens_failed,
                "failing_since": (max(0.0, time.monotonic()
                                      - self._health_failing_since)
                                  if self._health_failing_since else None),
                "stalled": self._health_stalled_since is not None,
                "utterances": self._health_utt,
            }

    def _health_state_now_locked(self) -> str:
        """Classify the current mic state. Caller holds self._lock. Shared by
        the journal reporter and mic_health() so the two can never disagree."""
        if not self._running:
            return "stopped"
        if self._health_failing_since is not None:
            return "open-failing"
        if self._frames_seen and (time.monotonic() - self._last_nonzero
                                  > self.MIC_SILENT_REPORT_S):
            return "silent"
        return "listening"

    def _health_tick(self) -> None:
        with self._lock:
            device = (self._health_open_device
                      or (str(SETTINGS["mic_device"])
                          if SETTINGS["mic_device"] else "system default"))
            state = self._health_state_now_locked()
            stalled = (self._health_stalled_since is not None
                       and state == "listening")
            degraded = state in ("silent", "open-failing")
            changed = state != self._health_state
            last = self._health_state
            capture_rate = getattr(self, "_capture_rate", None) or "-"
            frames = self._frames_seen
            last_nonzero = self._last_nonzero
            last_open = self._health_last_open
            opens_ok = self._health_opens_ok
            opens_failed = self._health_opens_failed
            utterances = self._health_utt
            recovered_after = self._health_recovered_after
            ever_started = self._ever_started

        # Assistant health/resource/world/hardware work may perform I/O and
        # must not make mic_snapshot wait for it.
        try:
            self._assistant._maybe_self_heal(degraded)   # assistant-level policy
        except Exception:
            log.exception("mic self-heal check failed")
        try:
            self._assistant._resource_tick()              # RAM/VRAM crossing alerts
        except Exception:
            log.exception("resource health check failed")
        try:
            self._assistant._world_tick()                 # world warnings (opt-in)
        except Exception:
            log.exception("world warnings check failed")
        try:
            self._assistant._hardware_tick()              # live hardware watch (opt-in)
        except Exception:
            log.exception("hardware watch check failed")

        now = time.monotonic()
        with self._lock:
            # --- transition detection ---------------------------------
            self._health_state = state
            report = changed or self._health_next_summary <= now
            if report:
                self._health_next_summary = now + 3600.0
            persist = changed and (ever_started or state != "stopped")
            dur = (" after %.0fs failing" % recovered_after
                   if last == "open-failing" and recovered_after else "")

        # Journal/state-file work also stays outside the listener lock.
        if report:
            emit = (log.warning if state in ("silent", "open-failing")
                    else log.info)
            emit(
                "mic health: state=%s%s device=%s rate=%s frames=%d "
                "last_nonzero=%.0fs_ago last_open=%s opens_ok=%d "
                "opens_failed=%d utterances=%d",
                state,
                " (stalled — no frames, reopening)" if stalled else "",
                device,
                capture_rate,
                frames,
                max(0.0, now - last_nonzero),
                last_open,
                opens_ok, opens_failed,
                utterances)
        if changed:
            # persist only listeners that really captured (or degraded
            # while trying): a never-started listener (hands-free off
            # since boot, or a bare test instance) has no mic story and
            # must not pollute the state file
            if persist:
                _record_mic_event(last or "boot", state)
            emit = (log.warning if state in ("silent", "open-failing")
                    else log.info)
            emit(
                "mic health: state changed %s -> %s%s",
                last or "(start)", state, dur)

    def start(self) -> None:
        if self._running:
            return
        self._run_id += 1
        run_id = self._run_id
        self._ever_started = True
        old = self._thread
        if old is not None and old.is_alive():
            # a previous thread may still be sleeping up to 10s inside the
            # mic-retry loop; the run_id token makes it exit on wake instead
            # of resuming and opening a second stream
            old.join(timeout=1.0)
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(run_id,),
                                        name="handsfree", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # the owning thread closes the stream in _run(); closing a PortAudio
        # stream from another thread while its callback runs is unsafe
        self._running = False

    def suspend(self) -> None:
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    def restart(self) -> None:
        """Tear down the capture stream and reopen it from scratch — the
        recovery of last resort for a wedged device the watchdog reopen
        cannot fix (dead engine state, wedged resampler, vanished source).
        The old capture thread closes its own stream when it sees the new
        run_id, so joining it is safe."""
        if not self._running:
            return
        self._run_id += 1
        old = self._thread
        self._thread = threading.Thread(target=self._run,
                                        args=(self._run_id,), name="handsfree",
                                        daemon=True)
        self._thread.start()
        if old is not None:
            old.join(timeout=2.0)         # old thread closes its own stream
        log.warning("mic self-heal: capture stream restarted")

    def reset(self) -> None:
        self._discard = True
        if self._spotter is not None:
            self._spotter = WakeSpotter()   # drop any pending detection

    def _process_frame(self, indata: "np.ndarray", gate: "_SpeechGate",
                       frames: list, max_frames: int, min_frames: int) -> None:
        """Feed one audio frame through the gate (runs on the audio thread).

        Frames are dropped while the assistant is speaking (its own TTS must
        not re-trigger the gate) or while a push-to-talk press holds the
        bubble — but never while *this listener itself* is collecting an
        utterance, which also shows as state LISTENING."""
        if self._discard:
            self._discard = False
            gate.reset()
            self.gate_open = False
            frames.clear()
        if self._suspended or self._assistant.state == SPEAKING or (
                self._assistant.state == THINKING and not self.gate_open) or (
                self._assistant.state == LISTENING and not self.gate_open):
            if frames or self.gate_open:
                gate.reset()
                self.gate_open = False
                frames.clear()
            # the device is clearly alive (frames arrive); keep the silence
            # clock warm so TTS playback / PTT holds can't misclassify the
            # mic as 'silent' and trigger self-heal on a healthy device
            self._last_nonzero = time.monotonic()
            return
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        if rms > 0.5:  # anything above exact digital silence proves the device feeds data
            self._last_nonzero = time.monotonic()
        try:
            event = gate.feed(rms)
        except Exception:
            # an exception on the audio thread kills the PortAudio callback and
            # would leave gate_open=True forever (mic stuck "open")
            log.exception("VAD gate error — resetting gate")
            gate.reset()
            self.gate_open = False
            frames.clear()
            return
        if event == "start":
            frames.clear()
            self.gate_open = True
            self._assistant._vad_speech(True)
        if gate.in_speech:
            frames.append(indata.copy())
            self._assistant._emit_level(min(1.0, rms / 2000.0), "mic")
        if event == "end" or len(frames) >= max_frames:
            self.gate_open = False
            self._assistant._vad_speech(False)
            if len(frames) >= min_frames:
                audio = np.concatenate(frames).reshape(-1)
                # convert to the pipeline's 16 kHz domain (no-op at 16 kHz)
                audio = _audio._resample_to_16k(
                    audio, getattr(self, "_capture_rate", SAMPLE_RATE))
                if self._spotter is not None and self._spotter._armed:
                    # spotter caught the wake phrase; VAD caught the command —
                    # merge and mark so the transcript gate is bypassed
                    audio = np.concatenate(
                        [*self._spotter._speech, audio]).reshape(-1)
                    self._assistant._spotter_wake = True
                self._spotter = WakeSpotter()
                self._assistant.sigUtterance.emit(audio)
                self._health_utt += 1
            frames.clear()
        if self._spotter is not None and not self.gate_open:
            # native-rate mics (StreamCam 48 kHz…) must be resampled to the
            # spotter's 16 kHz domain first — feeding 48 kHz frames makes the
            # 1280-sample model window 3x too short and it never fires.
            feed_data = indata.reshape(-1)
            try:
                _rate = int(getattr(self, "_capture_rate", SAMPLE_RATE) or SAMPLE_RATE)
            except Exception:
                _rate = SAMPLE_RATE
            if _rate != SAMPLE_RATE:
                try:
                    feed_data = _audio._resample_to_16k(feed_data, _rate)
                except Exception:
                    log.exception("wake spotter resample failed — skipping frame")
                    return
            fired, pre_audio = self._spotter.feed(feed_data)
            n_samples = sum(len(c) for c in pre_audio)
            if fired and n_samples >= SAMPLE_RATE // 2:
                # the spotter IS the wake gate: emit directly, bypass the
                # transcript wake-word check in _pipeline
                log.info("wake spotter fired (%.1fs of audio)",
                         n_samples / SAMPLE_RATE)
                self.gate_open = False
                self._assistant._spotter_wake = True
                self._assistant.sigUtterance.emit(
                    np.concatenate(pre_audio).reshape(-1))
                self._health_utt += 1
            if fired:
                # only reset the VAD gate when the spotter actually fired:
                # an unconditional reset here zeroed the gate's consecutive-
                # loud-frames counter after EVERY frame, so with the spotter
                # active the VAD could never reach its 2-frame start threshold
                # and hands-free was deaf while push-to-talk still worked
                gate.reset()
                self._discard = True      # drop stale VAD frames after a hit

    def _run(self, run_id: int) -> None:
        max_frames = int(self.MAX_UTTERANCE_S * SAMPLE_RATE / self.FRAME)
        min_frames = int(self.MIN_UTTERANCE_S * SAMPLE_RATE / self.FRAME)
        frames: list[np.ndarray] = []
        gate = _SpeechGate(int(SETTINGS["mic_threshold"]))
        spotter_on = (bool(SETTINGS.get("wake_spotter"))
                      and bool(SETTINGS.get("wake_word_required")))
        if spotter_on and _get_spotter() is not None:
            self._spotter = WakeSpotter()
            log.info("audio wake spotter active (openWakeWord)")
        else:
            self._spotter = None

        def cb(indata, nframes, time_info, status) -> None:
            self._frames_seen += 1        # liveness counter: always, even when suspended
            if status:
                log.warning("hands-free audio: %s", status)
            if self._running and self._run_id == run_id:
                self._process_frame(indata, gate, frames, max_frames, min_frames)

        open_failures = 0
        while self._running and self._run_id == run_id:
            # re-resolve the device each (re)open so settings changes apply live
            device = str(SETTINGS["mic_device"]) if SETTINGS["mic_device"] else None
            try:
                # devices that reject 16 kHz (e.g. StreamCam) are opened at
                # their native rate instead; utterances are resampled to
                # 16 kHz at ingestion so the pipeline is rate-agnostic
                self._stream, rate = _open_input(
                    device, SAMPLE_RATE, self.FRAME, cb)
                _start_stream_owned(self._stream)
            except Exception as e:
                open_failures += 1
                self._health_opens_failed += 1
                if self._health_failing_since is None:
                    self._health_failing_since = time.monotonic()
                log.exception("hands-free listener failed to open the microphone")
                # Never permanently disable over a transient mic problem: USB
                # mics (e.g. the Yeti) can take over a minute to become usable
                # after restarts. After 30 fast tries, slow down to one attempt
                # every 10s and keep trying for as long as hands-free is on.
                if open_failures == 30:
                    self._was_struggling = True
                    notify("hands-free: microphone not ready — still trying "
                           "every 10 seconds")
                if open_failures % 6 == 0:
                    # PortAudio can wedge for the whole process when its
                    # first open races PipeWire startup — fresh devices only
                    # appear after a full terminate/reinitialize.
                    #
                    # But PortAudio is process-global, so this tears down any
                    # stream we are still using: doing it while the bubble is
                    # speaking aborts the interpreter (the recorded crash is
                    # "Fatal Python error: Aborted" inside sounddevice's
                    # OutputStream.__init__ on the _speak thread). Defer while
                    # audio is in use rather than kill the process.
                    if _audio.portaudio_busy():
                        log.warning("PortAudio reinit deferred — a stream is "
                                    "still open after %d failed mic opens",
                                    open_failures)
                    else:
                        try:
                            sd._terminate()
                            sd._initialize()
                            log.warning("PortAudio reinitialized after %d failed "
                                        "mic opens", open_failures)
                        except Exception:
                            log.exception("PortAudio reinit failed")
                time.sleep(2.0 if open_failures < 30 else 10.0)
                continue
            open_failures = 0
            self._capture_rate = rate
            # ponytail: VAD frame budgets scale with the native rate — 1024
            # samples at 48 kHz is 3x shorter than at 16 kHz, so unscaled
            # budgets cut utterances 3x too early on StreamCam-class devices.
            try:
                _scale = float(rate) / float(SAMPLE_RATE) if rate else 1.0
            except Exception:
                _scale = 1.0
            if _scale != 1.0:
                max_frames = int(self.MAX_UTTERANCE_S * SAMPLE_RATE / self.FRAME * _scale)
                min_frames = int(self.MIN_UTTERANCE_S * SAMPLE_RATE / self.FRAME * _scale)
                # cb closes over these names, so the reassignment applies live.
            self._health_opens_ok += 1
            device = getattr(_MIC_LAST_OPEN_DEVICE, "value", device)
            self._health_open_device = device or "system default"
            self._health_last_open = datetime.datetime.now().strftime("%H:%M:%S")
            if self._health_failing_since is not None:
                self._health_recovered_after = (
                    time.monotonic() - self._health_failing_since)
                self._health_failing_since = None    # recovered
            self._health_stalled_since = None
            if rate != SAMPLE_RATE:
                log.info("mic opened at native %d Hz (resampling to %d)",
                         rate, SAMPLE_RATE)
            if getattr(self, "_was_struggling", False):
                self._was_struggling = False
                notify("hands-free: microphone is back")
            log.info("hands-free listening (device: %s)", device or "system default")
            self._last_nonzero = time.monotonic()

            # watchdog: a USB reset can orphan the stream so it keeps "running"
            # while delivering nothing (stalled) or pure digital silence
            last_seen = self._frames_seen
            stalled_since: float | None = None
            while self._running and self._run_id == run_id:
                time.sleep(0.5)
                now = time.monotonic()
                if self._frames_seen == last_seen:
                    if stalled_since is None:
                        stalled_since = now
                        self._health_stalled_since = stalled_since
                    elif now - stalled_since > self.REOPEN_S:
                        log.warning("hands-free mic stream stalled; reopening")
                        break
                    continue
                last_seen = self._frames_seen
                stalled_since = None
                self._health_stalled_since = None
                if now - self._last_nonzero > self.SILENT_REOPEN_S:
                    log.warning("hands-free mic delivers only silence; reopening")
                    break

            try:
                _stop_stream_owned(self._stream)
            except Exception:
                pass
            self._stream = None
            opened = getattr(_MIC_LAST_OPEN_DEVICE, "value", device)
            device = opened or "system default"
            frames.clear()
            gate.reset()
            self.gate_open = False
            if self._running and self._run_id == run_id:
                time.sleep(1.0)           # let a replugged/reset device settle
        if self._run_id == run_id:        # only the current generation owns state
            self._running = False
        self.gate_open = False


# --------------------------------------------------------------------- assistant

_MONOTONIC_BOOT_FLOOR = time.monotonic() - 1.0
"""time.monotonic() is uptime-based (zero at boot), NOT epoch-based — so a
fresh 'never announced' sentinel of 0.0 looks like an announcement made just
before boot whenever the machine has been up less than one cooldown window.
That silently suppressed urgent world/hardware warnings after every reboot.
Announce-cooldown checks go through this floor: a 0.0 sentinel means "never".
"""


def _announce_ok(last: float, cooldown_s: float) -> bool:
    """True when the announce cooldown has elapsed; 0.0/None = never."""
    if not last:
        return True
    return time.monotonic() - max(last, _MONOTONIC_BOOT_FLOOR) >= cooldown_s


class Assistant(QObject):
    """Owns the state machine, the worker pipeline and the conversation."""

    sigState = Signal(str)
    sigLevel = Signal(float)
    sigUtterance = Signal(object)     # hands-free: np.ndarray from the VAD thread
    sigCommand = Signal(str)          # control-socket commands → main thread
    # Class-level so it exists on an instance built with __new__ (tests do this)
    # as well as a normally constructed one; one Assistant per process anyway.
    _gen_lock = threading.Lock()      # makes the generation increment atomic

    def __init__(self) -> None:
        super().__init__()
        self._lifecycle_ensure()
        self._state = IDLE
        self._gen = 0                     # increments per interaction; stale
        self._cancel = threading.Event()  # workers check their own event
        self._recorder: Recorder | None = None
        self._ptt_lock = threading.RLock()  # guards PTT epoch + staleness+submit
        self._ptt_epoch = 0  # PTT-scoped generation: background timers must not kill utterances
        self._ptt_stopping = False  # True while a ptt-stop worker owns stream.stop()
        self._models_ready = threading.Event()
        self._history = self._load_history()
        self._memory = _load_memory()
        self._turn_spoke = False
        self._last_spoken = ""
        self._recently_spoken: list[str] = []   # last TTS lines, for echo rejection
        self._handsfree = bool(SETTINGS.get("handsfree", False))
        self._listener = ContinuousListener(self)
        self._notifications = _core_assistant.NotificationReader(
            spawn=self._start_worker, is_closed=self._is_closed,
            announce=self._announce_now, muted=self._notification_muted,
            popen_factory=lambda *a, **k: subprocess.Popen(*a, **k),
            persist=set_setting)
        self._pomodoro = _core_assistant.PomodoroController(
            announce=self._announce_now, spawn=self._start_worker,
            is_closed=self._is_closed)
        self._tools = ToolBelt(
            on_restart_pending=self._prepare_restart,
            permissions=SETTINGS["permissions"],
            on_notification=self._set_notification_reader,
            on_announce=self._announce_now,
            on_pomodoro=self._set_pomodoro,
            on_cap_refusal=self.announce_cap_refusal,
        )
        # (the refusal announcement shares this channel with job completions,
        # reminders and hands-free confirmations — one serializer, no overlap)
        if bool(SETTINGS.get("notification_reader", False)):
            # Opt-in persistence means the reader should resume after restart;
            # a missing dbus-monitor simply reports an error and leaves it off.
            result = self._set_notification_reader(True)
            if result and result.startswith("ERROR"):
                log.error("notification reader startup: %s", result)
        self._empty_streak = 0                   # consecutive empty transcriptions
        self._wake_until = 0.0                   # monotonic: engagement window expiry
        self._followup_until = 0.0               # monotonic: no-wake-word window after a reply
        # -- mic self-heal (auto-recovery from a wedged capture) -------------
        self._heal_pending_since = None  # monotonic: first degraded sighting
        self._heal_attempts = 0          # restarts for the current degraded streak
        self._heal_last = 0.0            # monotonic: last recovery action (spam guard)
        self._dictation = False          # voice dictation: transcripts get TYPED,
                                         # never sent to the brain (session-only)
        self._spotter_wake = False               # last utterance woke via audio spotter
        self._briefing_done_date = ""            # last day the briefing was spoken
        self._last_transcript = ("", 0, 0.0)  # (text, gen, monotonic) per-utterance
        # Resource alerts are edge-triggered: one announcement per threshold
        # crossing, then re-arm only after usage falls below the threshold.
        self._resource_alerted = {"ram": False, "vram": False}
        self._resource_last = {"ram": None, "vram": None}
        # {registry: (last spoken at, cumulative refusals then)}. Per-registry so
        # a full job cap and a wedged diagnostic do not silence each other, and
        # per-INSTANCE so it cannot outlive the bubble that did the refusing.
        self._cap_spoken: dict[str, tuple[float, int]] = {}
        self._world_last_announce = 0.0  # monotonic: last proactive warning
        self._hardware_note = ""       # 1-2 line change note for the next turn
        self._hardware_last = {}       # change-detection state (in-memory only)
        self._hardware_last_urgent = 0.0  # monotonic: last hardware urgent
        self._hardware_tick_n = 0      # tick parity: GPU util at most every 2nd
        self._pipeline_q: "queue.Queue" = queue.Queue(maxsize=1)
        self._pipeline_submit_lock = threading.Lock()
        self._start_worker(self._pipeline_worker, name="pipeline")
        self.sigUtterance.connect(self._on_utterance)
        self.sigCommand.connect(self._on_command)
        # -- the shared voice level: who published it, how loud, and when --
        # Written from the VAD / push-to-talk / playback threads, so these are
        # plain scalars (atomic enough under the GIL) rather than a lock. The
        # control socket's `level` command reads them back for the Settings →
        # Voice meter, which is how the feed gets diagnosed without watching
        # the bubble repaint.
        self._level_last = 0.0
        self._level_source = "none"
        self._level_at = 0.0
        # Voice-reactive visuals need a level while the bubble is SPEAKING too,
        # and the mic is deliberately blanked then (its own TTS would re-trigger
        # the VAD). core.audio.play_wav therefore reports what it is actually
        # playing through this hook. Best-effort: the feed swallows any error
        # from the GUI side, so a broken widget can never stall playback.
        if "_audio" in globals():
            hook = getattr(_audio, "set_level_hook", None)
            if hook is not None:
                hook(lambda v: self._emit_level(v, "tts"))

    # -- the shared voice level ------------------------------------------------

    def _emit_level(self, value: float, source: str) -> None:
        """Publish one voice level AND remember where it came from.

        All three producers call this instead of `sigLevel.emit` directly — the
        continuous listener's VAD, the push-to-talk recorder and core.audio's
        playback hook. Keeping the bookkeeping at the single point where the
        signal is produced is what lets `level_snapshot()` answer "is the mic
        feeding, or is the bubble's own voice driving the visuals?" rather than
        reporting a bare number that looks identical either way.
        """
        try:
            val = min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return
        self._level_last = val
        self._level_source = source
        if val > 0.0:
            self._level_at = time.monotonic()
        try:
            self.sigLevel.emit(val)
        except Exception:
            # Both callers that matter run on threads whose death is silent and
            # expensive (the PortAudio callback, the TTS playback thread), so a
            # raising slot — realistically a widget deleted mid-shutdown — must
            # not travel back into them. Same contract as core.audio._emit_level.
            log.debug("voice level emit failed", exc_info=True)

    def level_snapshot(self) -> dict:
        """Live voice-level state, served by the control socket's `level`
        command.

        `raw` is the last value a producer emitted and `ui` is the smoothed
        value the bubble's painters read out of `_frame()["level"]` — the number
        every design actually animates from. Reporting both is the whole point:
        it separates "nothing is feeding the level" from "the feed is fine but
        the bubble is not showing it". `age_s` is how long ago a non-zero level
        was seen (None = none since start), which is what exposes a wedged feed
        that is emitting exact zeros.
        """
        bw = getattr(self, "_bubble_widget", None)
        try:
            ui = getattr(bw, "_level_ui", None) if bw is not None else None
        except RuntimeError:
            ui = None                      # widget deleted during shutdown
        try:
            raw = float(getattr(self, "_level_last", 0.0) or 0.0)
            ui = raw if ui is None else float(ui)
            at = float(getattr(self, "_level_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            raw, ui, at = 0.0, 0.0, 0.0
        return {
            "raw": round(raw, 4),
            "ui": round(ui, 4),
            "source": str(getattr(self, "_level_source", "none")),
            "age_s": (round(time.monotonic() - at, 3) if at else None),
            "state": str(self.state),
            "handsfree": bool(getattr(self, "_handsfree", False)),
        }

    # -- health snapshot (mic + brain + TTS) --------------------------------

    def _lifecycle_ensure(self) -> None:
        """Lazily provide lifecycle state for real and __new__ test objects."""
        if not hasattr(self, "_shutdown_event"):
            self._shutdown_event = threading.Event()
        if not hasattr(self, "_closed"):
            self._closed = False
        if not hasattr(self, "_workers"):
            self._workers = set()
        if not hasattr(self, "_workers_lock"):
            self._workers_lock = threading.Lock()

    def _start_worker(self, target, args=(), name: str = "assistant-worker", **_kwargs):
        self._lifecycle_ensure()
        if self._closed:
            return None
        def run() -> None:
            try:
                target(*args)
            finally:
                with self._workers_lock:
                    self._workers.discard(threading.current_thread())
        thread = threading.Thread(target=run, name=name, daemon=True)
        with self._workers_lock:
            if self._closed:
                return None
            self._workers.add(thread)
        thread.start()
        return thread

    def mic_health(self) -> dict:
        """One JSON-ready snapshot of the assistant's vital signs: mic health
        (same state machine as the journal lines), brain (Ollama + model) and
        TTS (chatterbox) status. Served over the control socket as `health`;
        kept free of Qt/logging side effects so it is trivially testable.

        `tts.engine` and `tts.reference` are reported rather than just ready/
        not, because "ready" is not enough to tell a built-in-voice bubble from
        one that failed to condition on a reference clip."""
        snap = {
            "assistant": self.state,
            "handsfree": bool(self._handsfree),
            "followup_armed": _tick_now() < self._followup_until,
        }
        snap["mic"] = self._listener.mic_snapshot()
        snap["brain"] = {
            "model": OLLAMA_MODEL,
            "host": OLLAMA_BASE,
            "reachable": ollama_available(),
        }
        snap["tts"] = {
            "ready": _tts_model is not None,
            "engine": TTS_ENGINE,
            "device": _audio._tts_device or None,
            "reference": TTS_REFERENCE or None,
            "whisper_ready": _whisper_model is not None,
        }
        snap["appearance"] = {
            "look": _appearance_look() or "Custom",
            "design": str(SETTINGS.get("bubble_design", "orb")),
            "size": SETTINGS.get("bubble_size", _core_bubble.WINDOW_PX),
        }
        snap["deployment"] = _deployment_snapshot()
        return snap

    # -- mic self-heal -------------------------------------------------------

    def _bump_gen(self) -> "tuple[int, threading.Event]":
        """Atomically claim the next turn generation and a fresh cancel event.

        `self._gen += 1` followed by `gen = self._gen` is two steps, and the
        callers live on different threads (Qt input, the listener, PTT, the
        reminder worker, the health tick). Interleaved, two turns can be
        claimed with the SAME generation — which is exactly the value the
        staleness checks (`gen != self._gen`) and the gen-keyed transcript
        cache trust, so one utterance can be answered with another's text.
        One lock, one writer: every increment goes through here.
        """
        with self._gen_lock:
            self._gen += 1
            return self._gen, threading.Event()

    def _say_now(self, text: str) -> None:
        """Standalone announcement: speak text outside any turn pipeline."""
        if self._is_closed():
            return
        gen, cancel = self._bump_gen()   # fresh: _cancel may be set
        self._start_worker(
            target=lambda: (self._speak(text, gen, cancel), self._set(gen, IDLE)),
            name="selfheal-tts", daemon=True,
        )

    def say_preview(self, text: str) -> str:
        """Speak a one-off line as a voice preview; returns a status line.

        The settings app asks the RUNNING bubble to do this instead of loading
        its own engine: a second ChatterboxTurboTTS is ~2.7 GB of VRAM in a
        second process, and it would preview a voice the bubble is not actually
        using (the reference clip and rate/volume are applied at the bubble's
        tts_to_wav, not the GUI's).

        Deliberately routed through _announce_now rather than _speak directly:
        a preview is not a turn, so it must not bump the turn generation, and it
        must cancel whatever is playing the way every announcement does —
        otherwise the user hears the preview under the bubble's reply.
        """
        text = " ".join((text or "").split())
        if not text:
            return "error: say needs text"
        truncated = len(text) > _SAY_PREVIEW_MAX
        if truncated:
            text = text[:_SAY_PREVIEW_MAX]
        self._announce_now(text)
        note = f" (truncated to {_SAY_PREVIEW_MAX} chars)" if truncated else ""
        return f"ok: speaking {len(text)} chars with {TTS_ENGINE}{note}"

    def _is_closed(self) -> bool:
        self._lifecycle_ensure()
        return bool(self._closed or self._shutdown_event.is_set())

    def _maybe_self_heal(self, degraded: bool) -> None:
        """Auto-recovery policy, called from the listener's health tick (its
        own thread): after a grace period of continuous degradation while
        hands-free is on, restart the capture stream (fixes wedged PortAudio
        engine state the watchdog reopen can't) and say why; after
        SELFHEAL_MAX tries, keep journaling only until the mic is healthy
        again, which re-arms it."""
        if not degraded:
            self._heal_pending_since = None
            self._heal_attempts = 0
            return
        if not (self._handsfree and bool(SETTINGS.get("mic_selfheal", True))):
            return
        now = time.monotonic()
        if self._heal_pending_since is None:
            self._heal_pending_since = now
            return
        if now - self._heal_pending_since < self._listener.SELFHEAL_GRACE_S:
            return
        if self.state == SPEAKING or now - self._heal_last < 15.0:
            return                      # never talk over a reply; rate-limit
        if self._heal_attempts >= self._listener.SELFHEAL_MAX:
            return                      # stayed degraded: journal lines only
        self._heal_attempts += 1
        self._heal_last = now
        self._heal_pending_since = None
        log.warning("mic self-heal: degraded for %.0fs, restarting capture "
                    "(attempt %d/%d)", self._listener.SELFHEAL_GRACE_S,
                    self._heal_attempts, self._listener.SELFHEAL_MAX)
        self._listener.restart()
        self._say_now("I'm having microphone trouble — restarting my "
                      "listening engine.")

    def _mic_selfheal_rearm(self) -> None:
        """Called on hands-free toggles: a fresh stream is a clean slate."""
        self._heal_pending_since = None
        self._heal_attempts = 0

    # -- resource health --------------------------------------------------------

    @staticmethod
    def _resource_usage() -> dict[str, float | None]:
        """Return RAM and NVIDIA VRAM usage percentages without shelling out
        through the AI command path. Missing telemetry is None, never an
        exception: a machine without NVIDIA is a normal configuration."""
        ram = None
        try:
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    mem[key] = float(value.strip().split()[0])
            total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
            if total and avail is not None:
                ram = max(0.0, min(100.0, (1.0 - avail / total) * 100.0))
        except (OSError, ValueError, ZeroDivisionError):
            log.warning("could not read /proc/meminfo")
        vram = None
        if shutil.which("nvidia-smi"):
            try:
                p = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2,
                )
                rows = []
                for line in p.stdout.splitlines():
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) == 2:
                        used, total = float(parts[0]), float(parts[1])
                        if total > 0:
                            rows.append(used / total * 100.0)
                if rows:
                    vram = max(rows)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                log.debug("nvidia-smi telemetry unavailable", exc_info=True)
        return {"ram": ram, "vram": vram}

    def announce_cap_refusal(self, report: dict, detail: str = "") -> None:
        """Say out loud that a cap turned work away — and not once per refusal.

        A refusal already reaches the journal and the durable record, but both
        are things the user has to go and read. The point of a cap turning work
        away is that something was ASKED for and did not happen, so the bubble
        says so on the channel job completions already use instead of waiting to
        be asked (core/tools._announce_job speaks through the same hook).

        Rate-limited, because the refusal path is retried by nature: a model
        re-calling `start_command`, a health keybind being hammered. An audio
        loop is worse than the invisibility this replaces. The first refusal at
        a cap speaks in full; after that a cap speaks again only once the
        cooldown has passed, and then says how many more requests were turned
        away in the meantime, so a storm is legible without being chatty.

        Never raises: it runs inside a refusal that has already told the model
        nothing was created, and announcing must never be a reason a refusal
        takes a different path.
        """
        try:
            report = report or {}
            registry = str(report.get("registry") or "")
            if not registry or self._is_closed():
                return
            table = getattr(self, "_cap_spoken", None)
            if table is None:
                table = self._cap_spoken = {}
            last, spoken_at = table.get(registry, (0.0, 0))
            if last and not _announce_ok(last, CAP_ANNOUNCE_COOLDOWN):
                # Inside the cooldown: still journalled and still durable, just
                # not repeated out loud.
                log.info("cap refusal not repeated (%s within %.0fs)",
                         registry, CAP_ANNOUNCE_COOLDOWN)
                return
            cap = int(report.get("cap") or 0)
            held = int(report.get("held") or 0)
            seen = int(report.get("count") or 0)
            label = _cap_label(registry)
            where = f" ({held} of {cap})" if cap else ""
            if last:
                missed = max(1, seen - spoken_at)
                text = (f"I still can't do that: the {label} limit is full"
                        f"{where} — {missed} more request"
                        f"{'' if missed == 1 else 's'} turned away since I last"
                        f" said so.")
            else:
                text = (f"I couldn't do that: the {label} limit is full"
                        f"{where}. Nothing was started.")
            table[registry] = (time.monotonic(), seen)
            if len(table) > CAP_ANNOUNCE_MAX:
                for stale in sorted(table, key=lambda k: table[k][0] \
                                    )[0:len(table) - CAP_ANNOUNCE_MAX]:
                    table.pop(stale, None)
            log.warning("cap refusal announced: %s [%s]", text, detail or "-")
            self._announce_now(text)
        except Exception:
            log.exception("cap refusal announcement failed")

    def _resource_tick(self) -> None:
        """Announce RAM/VRAM threshold *crossings* once, and re-arm after
        usage drops below each threshold. Opt-in because spoken alerts can
        interrupt a user's work; health polling remains cheap either way."""
        if not bool(SETTINGS.get("resource_alerts", False)):
            return
        usage = self._resource_usage()
        limits = {
            "ram": float(SETTINGS.get("ram_alert_percent", 90.0)),
            "vram": float(SETTINGS.get("vram_alert_percent", 90.0)),
        }
        labels = {"ram": "system memory", "vram": "GPU memory"}
        for kind, value in usage.items():
            self._resource_last[kind] = value
            if value is None:
                continue
            high = value >= limits[kind]
            if not high:
                self._resource_alerted[kind] = False
                continue
            if self._resource_alerted[kind]:
                continue
            self._resource_alerted[kind] = True
            log.warning("resource alert: %s %.1f%% >= %.1f%%",
                        kind, value, limits[kind])
            self._announce_now(
                f"Warning: {labels[kind]} is at {value:.0f} percent.")

    def _world_tick(self) -> None:
        """Proactive severe-world-event warnings; mirrors _resource_tick.

        Opt-in: poll (cheap on cooldown), per-event seen-store, one global
        cooldown, popup always + spoken unless already speaking.
        """
        if not bool(SETTINGS.get("world_warnings", False)):
            return
        try:
            cooldown_s = float(SETTINGS.get("world_cooldown_min", 60.0)) * 60.0
        except (TypeError, ValueError):
            cooldown_s = 3600.0
        if not _announce_ok(self._world_last_announce, cooldown_s):
            return
        events, _degraded = _world_events("all", 5)
        fresh = [e for e in events
                 if e.get("urgent") and not _world_seen(e.get("key", ""))]
        if not fresh:
            return
        self._world_last_announce = time.monotonic()
        _world_mark_seen([e["title"] for e in fresh])
        text = "World warning: " + "; ".join(e["title"][:140] for e in fresh[:2])
        log.warning("world warning received")
        notify(text)
        if self.state != SPEAKING:
            self._announce_now(text)

    def _hardware_tick(self) -> None:
        """Live hardware watch: cheap sampling + change detection, ~10 s tick.

        Opt-in via hardware_watch (off returns in <1 ms). Cheap signals only
        (loadavg, meminfo, disk_usage, which, mic_snapshot); slow sections
        are peeked from the shared TTL cache when fresh, never forced — the
        nvidia-smi util query is the only in-tick subprocess, at most every
        2nd tick with a 2 s timeout. Never raises (call site also isolates).
        """
        try:
            if not bool(SETTINGS.get("hardware_watch", False)):
                return
            hw = _hardware
            last = self._hardware_last
            self._hardware_tick_n = self._hardware_tick_n + 1
            notes: list = []
            urgent: str | None = None
            # -- mic: consume the listener state machine, never duplicate it
            try:
                mic = self._listener.mic_snapshot()
            except Exception:
                mic = {}
            cur_mic = (mic.get("state"), mic.get("device"))
            prev_mic = last.get("mic")
            if prev_mic is not None and cur_mic != prev_mic:
                if (self._handsfree and cur_mic[0] == "open-failing"
                        and prev_mic[0] != "open-failing"):
                    urgent = (f"Microphone failed ({cur_mic[1]}): "
                              "hands-free is deaf")
                else:
                    notes.append(f"Mic: {prev_mic[0]} → {cur_mic[0]} "
                                 f"({cur_mic[1]})")
            last["mic"] = cur_mic
            # -- cheap stdlib sampling (zero subprocess on this path)
            try:
                ttl_data = (_DOCTOR_TTL.get("data") or {}) \
                    if isinstance(_DOCTOR_TTL, dict) else {}
                ttl_at = (_DOCTOR_TTL.get("at") or {}) \
                    if isinstance(_DOCTOR_TTL, dict) else {}
            except Exception:
                ttl_data, ttl_at = {}, {}
            def _fresh(section: str) -> dict | None:
                try:
                    ttl = hw.TTL.get(section, 0) if hw else 0
                    if (section in ttl_data and time.monotonic()
                            - ttl_at.get(section, 0.0) < ttl):
                        return ttl_data[section]
                except Exception:
                    pass
                return None
            # -- disk floor: crossing announces once, re-arm above threshold
            try:
                disk_gb = float(SETTINGS.get("hardware_disk_gb", 5.0))
            except (TypeError, ValueError):
                disk_gb = 5.0
            disk = hw.disk_free("/") if hw else {"ok": False}
            if isinstance(disk, dict) and disk.get("ok"):
                try:
                    free_gb = float(disk.get("free", 0)) / 2 ** 30
                except (TypeError, ValueError):
                    free_gb = disk_gb
                if free_gb < disk_gb:
                    if not last.get("disk_low"):
                        last["disk_low"] = True
                        urgent = urgent or (f"Disk critically low: "
                                            f"{free_gb:.1f} GiB free")
                else:
                    if last.get("disk_low"):
                        notes.append(f"Disk recovered: {free_gb:.1f} GiB free")
                    last["disk_low"] = False
            # -- GPU: presence every tick (which, no subprocess), util at
            # most every 2nd tick — the single allowed in-tick subprocess
            gpu_util: float | None = None
            try:
                gpu_present = bool(shutil.which("nvidia-smi"))
            except Exception:
                gpu_present = None
            if gpu_present is not None:
                if last.get("gpu_present") is not None \
                        and last["gpu_present"] != gpu_present:
                    notes.append("GPU " + ("appeared" if gpu_present
                                           else "disappeared"))
                last["gpu_present"] = gpu_present
            if gpu_present and self._hardware_tick_n % 2 == 0:
                try:
                    p = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=2)
                    rows = []
                    for line in p.stdout.splitlines():
                        parts = [x.strip() for x in line.split(",")]
                        if len(parts) != 2:
                            continue
                        try:
                            used, total = float(parts[0]), float(parts[1])
                        except ValueError:
                            continue
                        if total > 0:
                            rows.append(used / total * 100.0)
                    if rows:
                        gpu_util = max(rows)
                except Exception:
                    log.debug("hardware watch: nvidia-smi unavailable",
                              exc_info=True)
            # -- VRAM crossing here only when resource_alerts is off (else
            # _resource_tick owns it — never double-announce)
            if gpu_util is not None and not SETTINGS.get("resource_alerts", False):
                try:
                    vlim = float(SETTINGS.get("vram_alert_percent", 90.0))
                except (TypeError, ValueError):
                    vlim = 90.0
                if gpu_util >= vlim:
                    if not last.get("vram_alerted"):
                        last["vram_alerted"] = True
                        urgent = urgent or (f"GPU memory at {gpu_util:.0f}%")
                else:
                    last["vram_alerted"] = False
            # -- Ollama flip from TTL cache only; 2 consecutive fresh-cache
            # misses required (a single blip stays silent)
            ollama = _fresh("ollama")
            if ollama is not None:
                if ollama.get("ok"):
                    last["ollama_miss"] = 0
                    if last.get("ollama_down"):
                        last["ollama_down"] = False
                        notes.append("Ollama is reachable again")
                else:
                    last["ollama_miss"] = last.get("ollama_miss", 0) + 1
                    if last["ollama_miss"] >= 2 \
                            and not last.get("ollama_down"):
                        last["ollama_down"] = True
                        urgent = urgent or "Ollama is down: voice brain offline"
            # -- mic count via TTL-10 audio (peek only, never forced)
            audio = _fresh("audio")
            if isinstance(audio, dict) and self._handsfree \
                    and audio.get("ok") and not audio.get("count") \
                    and last.get("audio_count"):
                urgent = urgent or "Microphone unplugged: no input devices"
            if isinstance(audio, dict) and audio.get("count") is not None:
                last["audio_count"] = audio.get("count")
            # -- channels: note for the next turn, urgent via popup + speech
            if notes:
                self._hardware_note = "\n".join(notes[:2])[:200]
            if urgent is not None:
                try:
                    cd = float(SETTINGS.get("hardware_cooldown_min", 60.0)) * 60.0
                except (TypeError, ValueError):
                    cd = 3600.0
                if _announce_ok(self._hardware_last_urgent, cd):
                    self._hardware_last_urgent = time.monotonic()
                    log.warning("hardware watch: %s", urgent)
                    notify(urgent)
                    if self.state != SPEAKING:
                        self._announce_now(urgent)
        except Exception:
            log.exception("hardware watch tick failed")

    # -- ambient services -------------------------------------------------------

    def _set_notification_reader(self, enabled: bool):
        """Start/stop a session D-Bus notification monitor. Notification text
        is never replayed from history; only future notifications are spoken,
        and muted app names are filtered before TTS."""
        return self._notifications.set_enabled(enabled)

    @staticmethod
    def _dbus_strings(line: str) -> list[str]:
        """Extract ordinary quoted D-Bus string values from monitor output."""
        return _core_assistant.dbus_strings(line)

    @staticmethod
    def _notification_muted(app: str, summary: str, body: str) -> bool:
        """New mute contract: user list matches app (+summary) with word-ish
        semantics (app substring to keep 'Noisy'→'NoisyApp', summary whole
        word, never body); self-mute when app==handsoff or 'handsoff' in
        summary/body."""
        return _core_assistant.notification_muted(
            app, summary, body,
            mute_apps=SETTINGS.get("notification_mute_apps"),
            app_name=APP_NAME)

    def _notification_loop(self, proc, stop: threading.Event) -> None:
        self._notifications.loop(proc, stop)

    def _notification_reader_run(self, stop: threading.Event) -> None:
        """Production wrapper: run loop(), respawning dbus-monitor with
        bounded backoff when its stdout is exhausted."""
        self._notifications.run(stop)


    def _set_pomodoro(self, action: str, work: float, break_minutes: float) -> str:
        """Own the bounded Pomodoro worker and announce work/break transitions."""
        return self._pomodoro.command(action, work, break_minutes)

    def _pomodoro_loop(self, stop: threading.Event) -> None:
        self._pomodoro._loop(stop)

    # -- reminders --------------------------------------------------------------

    def _reminder_worker(self) -> None:
        while not self._shutdown_event.wait(2.0):
            try:
                # drain_due() owns the whole read-modify-write (never nest two
                # flock sidecars: the non-reentrant LOCK_EX would block forever;
                # the store's in-process lock serializes threads instead)
                fired = _reminder_store().drain_due()
                if not fired:
                    continue
                for r in fired:
                    log.info("reminder fired")
                    self.sigCommand.emit("__timer:%s\x1f%s" % (
                        r["name"], float(r.get("repeat_hours") or 0)))
            except Exception as exc:
                # A full disk makes drain_due() raise before it returns the
                # fired entries, so the reminder is never delivered AND never
                # pruned: it silently stops working, one journal line per tick.
                log.exception("reminder worker pass failed")
                self._report_once(
                    "reminder store unavailable", exc,
                    "handsoff: reminders are not firing — "
                    f"{type(exc).__name__}: {str(exc)[:160]}")

    def _settings_watch_worker(self) -> None:
        """Pick up settings.json saves within seconds, no restart required.

        Stats the file every 3 s; on mtime change the reload runs on the Qt
        thread via sigCommand (widget resizes are only legal there). The
        GUI notify is best-effort — this watcher is the backstop, so even a
        hand-edited settings.json applies live."""
        last = 0.0
        try:
            last = SETTINGS_FILE.stat().st_mtime
        except OSError:
            pass
        while not self._shutdown_event.wait(3.0):
            try:
                mtime = SETTINGS_FILE.stat().st_mtime
            except OSError:
                continue
            if mtime != last:
                last = mtime
                self.sigCommand.emit("reload-settings")

    def _announce_missed(self, missed: list[dict]) -> None:
        if self._is_closed():
            return
        names = "; ".join(r["name"] for r in missed[:4])
        extra = f" and {len(missed) - 4} more" if len(missed) > 4 else ""
        msg = f"While I was off, these reminders came due: {names}{extra}."
        log.info("announcing %d missed reminder(s)", len(missed))
        notify(f"handsoff missed reminders: {names}")
        gen, cancel = self._bump_gen()   # fresh: _cancel may be stale
        self._start_worker(
            lambda: (self._speak(msg, gen, cancel), self._set(gen, IDLE)),
            name="missed-rem-tts")

    def _fire_timer(self, name: str, repeat_hours: float = 0) -> None:
        if self._is_closed():
            return
        msg = (f"Reminder: {name}. Say snooze for more time." if not repeat_hours
               else f"Reminder: {name}.")
        log.info("announcing timer")
        notify(f"handsoff timer: {name}")
        self.interrupt()
        # _bump_gen hands back a FRESH cancel event: interrupt() just set
        # self._cancel, and _speak returns immediately on a set event — the
        # reminder would never be spoken
        gen, cancel = self._bump_gen()
        self._start_worker(
            lambda: (self._speak(msg, gen, cancel), self._set(gen, IDLE)),
            name="timer-tts")
        if not repeat_hours:
            # one-off: open the snooze window — a bare "snooze" within 90 s
            # re-arms it from now, no brain round-trip
            _snooze_offer.arm(SNOOZE_WINDOW_S, name=name)
            log.info("snooze window open for %.0fs", SNOOZE_WINDOW_S)

    # -- crash report & recovery -------------------------------------------------

    def _maybe_report_crash(self) -> None:
        """If we crashed recently, say so — silent deaths must not be silent.
        Only a crash.log WITH CONTENT counts: faulthandler creates the empty
        file at every startup, and a clean restart must not be called a crash."""
        try:
            crash = CRASH_LOG
            if not crash.exists() or crash.stat().st_size == 0:
                return
            age = time.time() - crash.stat().st_mtime
            if age < 86400:
                head = crash.read_text(errors="replace")[:200].replace("\n", " ")
                log.warning("previous crash detected (%.0fs ago): %s", age, head)
                gen, cancel = self._bump_gen()
                self._start_worker(
                    lambda: (self._speak(
                        "Heads up: I crashed earlier. It was logged, and I'm running again.",
                        gen, cancel), self._set(gen, IDLE)),
                    name="crash-report")
                # truncate (do NOT unlink): faulthandler still holds the fd
                # opened at startup. Unlinking orphans it — a LATER crash
                # would write to a deleted inode and never be reported again.
                # The fd is O_APPEND, so after truncation the next crash
                # appends from offset 0 and is visible on the next boot.
                _truncate_file_preserving_fd(crash)
        except Exception:
            log.exception("crash report failed")

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._is_closed():
            return
        self._start_worker(self._loader, name="loader")
        if self._handsfree:
            self._listener.start()
        missed = _take_missed_reminders()
        if missed:
            self._start_worker(self._announce_missed, args=(missed,),
                               name="missed-reminders")
        self._start_worker(self._reminder_worker, name="reminders")
        self._start_worker(self._settings_watch_worker, name="settings-watch")

    def shutdown(self) -> None:
        self._lifecycle_ensure()
        if self._closed:
            return
        self._closed = True
        self._shutdown_event.set()
        self._cancel.set()
        with _ANNOUNCE_CANCEL_LOCK:
            try:
                _ANNOUNCE_CANCEL.set()
            except Exception:
                pass
        listener = getattr(self, "_listener", None)
        if listener is not None:
            listener.stop()
        if getattr(self, "_tools", None) is not None:
            self._tools.stop_watchers()
        self._set_notification_reader(False)
        pom = getattr(self, "_pomodoro", None)
        if pom is not None:
            pom.shutdown()
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass
        q = getattr(self, "_pipeline_q", None)
        if q is not None:
            lock = getattr(self, "_pipeline_submit_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._pipeline_submit_lock = lock
            with lock:
                try:
                    pending = q.get_nowait()
                except queue.Empty:
                    pending = None
                if pending is not None:
                    pending[2].set()
                    q.task_done()
                try:
                    q.put_nowait((None, None, None))  # worker unpacks then exits on audio None
                except queue.Full:
                    pass
        deadline = time.monotonic() + SHUTDOWN_JOIN_TIMEOUT
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            if worker is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        with self._workers_lock:
            alive = [w.name for w in self._workers if w.is_alive()]
        if alive:
            log.warning("workers still running at shutdown: %s", alive)

    def _loader(self) -> None:
        if self._is_closed():
            return
        errors: list[str] = []
        try:
            get_whisper()
        except Exception as e:
            errors.append(f"whisper: {e}")
        try:
            get_tts()
        except Exception as e:
            errors.append(f"tts: {e}")
        # Pay the engine's one-time cost now. Measured through the control
        # socket: the first spoken reply after a fresh start took ~1.5 s to
        # first audio and every later one ~0.6 s, because the decoder, flow
        # sampler and vocoder compile their CUDA kernels on first use. The
        # model is already loaded here, so this spends that time where nobody
        # is waiting for an answer.
        try:
            t0 = time.monotonic()
            warm_samples = warm_tts()
            if warm_samples:
                log.info("speech engine warmed (%d samples in %.1fs)",
                         warm_samples, time.monotonic() - t0)
        except Exception:
            log.warning("speech warm-up failed — the first reply will be slow",
                        exc_info=True)
        if not ollama_available():
            errors.append(f"ollama: no server at {OLLAMA_BASE} (systemctl start ollama)")
        else:
            log.info("ollama ok at %s, model %s", OLLAMA_BASE, OLLAMA_MODEL)
        self._models_ready.set()
        if self._is_closed():
            return
        for e in errors:
            log.error("startup: %s", e)
        if errors:
            notify("handsoff: " + " | ".join(errors))
        # warm the LLM now (after whisper/tts, which load first). This is
        # MORE than a VRAM load: it sends the REAL system prompt + tool
        # schemas (+ history) so Ollama's KV cache holds the exact prefix a
        # real turn uses — the first question then only evaluates its own
        # few tokens (~0.4 s) instead of the full ~7 s prefill.
        try:
            t0 = time.time()
            warm_msgs = ([{"role": "system", "content": SYSTEM_PROMPT}]
                         + list(self._history)
                         + [{"role": "user", "content": "hi"}])
            ollama_chat(warm_msgs, TOOLS)
            log.info("LLM warmed in %.1fs (prompt prefix cached)", time.time() - t0)
        except Exception:
            log.exception("LLM warmup failed (will load on first question)")
        try:
            note = json.loads(PENDING_FILE.read_text()).get("note", "")
            PENDING_FILE.unlink(missing_ok=True)
        except Exception:
            note = ""
        self._maybe_report_crash()
        if note:
            log.info("speaking pending restart note")
            self._speak(note, self._gen, self._cancel)
            self._set(self._gen, IDLE)

    # -- state ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def _set(self, gen: int, state: str) -> None:
        """Update state and emit. Guarded: background threads (crash-report,
        snooze-TTS) can outlive the Qt object during teardown — a late emit
        raised 'Signal source has been deleted' (test suite warning, #9)."""
        if self._is_closed() or gen != self._gen:
            return
        self._state = state
        try:
            self.sigState.emit(state)
        except RuntimeError:
            pass                      # Qt object already deleted: shutting down

    # -- UI entry points (main thread) -------------------------------------------

    def _ptt_ensure(self) -> "threading.RLock":
        """Lazy PTT state for bare (__new__) test instances: real __init__
        already created these; tests that bypass it get them here so the
        same lock/epoch path always runs."""
        lock = getattr(self, "_ptt_lock", None)
        if lock is None:
            lock = threading.RLock()
            try:
                self._ptt_lock = lock
            except Exception:
                pass
        if getattr(self, "_ptt_epoch", None) is None:
            try:
                self._ptt_epoch = 0
            except Exception:
                pass
        if getattr(self, "_ptt_stopping", None) is None:
            try:
                self._ptt_stopping = False
            except Exception:
                pass
        return lock

    def _stop_recorder_bounded(self, rec, timeout: float = 3.0):
        """Bounded rec.stop() for the ptt workers (delegates to the shared
        module helper so bare test instances and real ones share one path)."""
        return _stop_recorder_bounded(rec, timeout=timeout)

    def _ptt_stop_owner_finished(self, rec) -> None:
        """Clear PTT ownership only from the thread that finished rec.stop()."""
        if getattr(self, "_ptt_stop_recorder", None) is rec:
            self._ptt_stopping = False
            self._ptt_stop_recorder = None

    def _resume_handsfree_listener(self) -> None:
        """Bring the continuous listener back after a push-to-talk press."""
        if not self._handsfree:
            return
        try:
            self._listener.start()
        except Exception:
            log.exception("push-to-talk: could not restart hands-free listening")

    def begin_listening(self) -> None:
        self.interrupt()
        if self._handsfree:
            # Hands-free owns the mic, but a push-to-talk press must still mean
            # "record me now": park the continuous listener for the duration of
            # the press and record normally, then bring it back on release.
            # One stream at a time, because two InputStreams on one device is
            # what wedges this mic. Previously the press just called
            # suspend() and returned without ever opening a recorder, so with
            # hands-free on the PTT key was a silent no-op with no feedback —
            # which is exactly what "push to talk is broken" looks like.
            self._ptt_handsfree_parked = True
            try:
                self._listener.stop()
            except Exception:
                log.exception("push-to-talk: could not park hands-free listening")
        lock = self._ptt_ensure()
        # press#2 during a wedged stop: wait briefly for the in-flight
        # stop worker, else refuse cleanly instead of a doomed second open.
        if getattr(self, "_ptt_stopping", False):
            deadline = time.monotonic() + 0.6
            while getattr(self, "_ptt_stopping", False) \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            if getattr(self, "_ptt_stopping", False):
                log.warning("begin_listening refused: stop still in flight")
                try:
                    try:
                        _gr = int(getattr(self, "_gen", 0) or 0)
                    except Exception:
                        _gr = 0
                    _cr = threading.Event()
                    try:
                        _mr = getattr(self, "_models_ready", None)
                    except Exception:
                        _mr = None

                    def _speak_busy(_g=_gr, _c=_cr, _m=_mr) -> None:
                        try:
                            if _m is not None:
                                try:
                                    _m.wait(30)
                                except Exception:
                                    pass
                            try:
                                self._speak("Microphone is busy, try again.",
                                            _g, _c)
                            except Exception:
                                pass
                            try:
                                self._set(_g, IDLE)
                            except Exception:
                                pass
                        except Exception:
                            pass

                    threading.Thread(target=_speak_busy, daemon=True).start()
                except Exception:
                    pass
                return
        # no orphan stream: a previous recorder left behind is closed here.
        old = getattr(self, "_recorder", None)
        if old is not None:
            log.warning("begin_listening refused: recorder still owns microphone")
            return
        gen, cancel = self._bump_gen()
        self._cancel = cancel
        with lock:
            try:
                self._ptt_epoch = int(getattr(self, "_ptt_epoch", 0) or 0) + 1
            except Exception:
                pass
        rec = Recorder(
            # push-to-talk is its own source: the Settings meter distinguishes
            # "hands-free is hearing me" from "my PTT key is recording"
            on_level=lambda v: self._emit_level(v, "ptt"),
            device=str(SETTINGS["mic_device"]) if SETTINGS["mic_device"] else None,
            threshold=int(SETTINGS["mic_threshold"]),
        )
        try:
            rec.start()
        except Exception as e:
            log.exception("cannot open microphone")
            self._set(gen, IDLE)
            message = ("I can't open the microphone."
                       if not isinstance(e, ValueError)
                       else "I can't find the configured microphone.")
            threading.Thread(
                target=lambda: (self._models_ready.wait(30), self._speak(
                    message, gen, cancel), self._set(gen, IDLE)),
                daemon=True,
            ).start()
            return
        self._recorder = rec
        self._set(gen, LISTENING)

    def finish_listening(self) -> None:
        # A press that parked the hands-free listener must submit the recorder
        # it opened, not just resume the listener: the old hands-free early
        # return threw the captured audio away, so PTT never produced a turn.
        # The listener is restarted once the recorder is actually released
        # (in _work, below) — never while the mic is still held open.
        parked = bool(getattr(self, "_ptt_handsfree_parked", False))
        self._ptt_handsfree_parked = False
        rec, self._recorder = self._recorder, None
        if rec is None:
            if parked:
                self._resume_handsfree_listener()
            return
        # PTT release must never block the Qt thread: stream.stop()/close()
        # on the flaky Yeti wedges for seconds, and submit's health line can
        # wait on the mic-health lock. Interrupt synchronously FIRST (old
        # pipeline must not keep running behind the new turn), claim the
        # next gen now (a second press invalidates this release), paint
        # THINKING immediately, stop->submit off-thread.
        self.interrupt()
        gen, cancel = self._bump_gen()
        self._cancel = cancel
        self._set(gen, THINKING)
        lock = self._ptt_ensure()
        try:
            ptt_epoch = int(getattr(self, "_ptt_epoch", 0) or 0)
        except Exception:
            ptt_epoch = 0
        try:
            self._ptt_stopping = True
            self._ptt_stop_recorder = rec
            rec._handsoff_stop_done = lambda _rec=rec: self._ptt_stop_owner_finished(_rec)
        except Exception:
            pass

        def _work(_rec=rec, _gen=gen, _epoch=ptt_epoch, _parked=parked) -> None:
            t0 = time.monotonic()
            try:
                audio, wedged = self._stop_recorder_bounded(_rec, timeout=3.0)
            except Exception:
                log.exception("recorder stop failed")
                audio, wedged = None, False
            t1 = time.monotonic()

            def _timing(submit_ms: int) -> None:
                try:
                    n = 0 if audio is None else len(audio)
                    rate = getattr(_rec, "_native_rate", SAMPLE_RATE)
                except Exception:
                    n, rate = 0, SAMPLE_RATE
                fmt = "ptt timing: stop_ms=%d submit_ms=%d frames=%d rate=%s"
                if wedged:
                    fmt += " wedged=1"
                log.info(fmt,
                         int((t1 - t0) * 1000), int(submit_ms), n, rate)

            try:
                # PTT-scoped staleness (not global gen): background timer
                # bumps must never discard a valid utterance; only a newer
                # PTT press (epoch bump) invalidates this release. The
                # check+submit hold one lock to close the ghost-turn window.
                with lock:
                    try:
                        cur_epoch = int(getattr(self, "_ptt_epoch", 0) or 0)
                    except Exception:
                        cur_epoch = _epoch
                    if _epoch != cur_epoch:
                        _timing(0)
                        return
                    try:
                        self.submit_audio(audio)
                    except Exception:
                        log.exception("ptt submit failed")
                        try:
                            if _gen == self._gen:
                                self._set(_gen, IDLE)
                        except Exception:
                            pass
                    finally:
                        try:
                            t2 = time.monotonic()
                            _timing(int((t2 - t1) * 1000))
                        except Exception:
                            pass
            finally:
                if _parked:
                    self._resume_handsfree_listener()

        threading.Thread(target=_work, name="ptt-stop", daemon=True).start()

    def submit_audio(self, audio: np.ndarray | None) -> None:
        """Shared entry point: push-to-talk releases and hands-free utterances.

        A capture that produced nothing is a MIC fault, not a quiet user, so the
        two cases are told apart in the journal and a deliberate key press is
        never dropped in silence: an empty/quiet hands-free utterance is ordinary
        (room noise, a stray syllable) and stays at INFO, while the same on a
        push-to-talk release means the device is muted, wrong, or wedged — the
        one case where the user is standing there waiting for an answer.
        """
        if self._is_closed():
            return
        if audio is None:
            log.warning(
                "no audio from the recorder — the utterance produced nothing "
                "(bounded stop timed out, or the input device failed to open)")
            self._set(self._gen, IDLE)
            return
        peak = int(np.max(np.abs(audio))) if len(audio) else 0
        if len(audio) < SAMPLE_RATE * 0.3 or peak < int(SETTINGS["mic_threshold"]):
            detail = ("frames=%d peak=%d threshold=%d"
                      % (len(audio), peak, int(SETTINGS["mic_threshold"])))
            if self._handsfree:
                log.info("discarding too-short/quiet capture (%s)", detail)
            else:
                log.warning(
                    "push-to-talk capture rejected (%s) — the microphone may be "
                    "muted, the wrong input device, or too far away", detail)
            self._set(self._gen, IDLE)
            return
        # NOTE: the echo check lives in _pipeline now — it needs whisper,
        # which must never run on the Qt GUI thread (it froze the UI after
        # every spoken reply), and this way the audio is transcribed once
        self.interrupt()
        gen, cancel = self._bump_gen()
        self._cancel = cancel
        self._set(gen, THINKING)
        # zero-LLM stop: probe the transcript in parallel; if it is a bare
        # stop command the queued turn is drained before the brain ever runs
        self._log_utterance_health()
        self._maybe_instant_stop(audio, gen)
        # hand off to the single pipeline worker: two overlapping pipelines
        # would race the shared history, per-turn stream state and tool belt (the old
        # thread-per-utterance design let an interrupted-but-still-running
        # turn write history concurrently with the new one)
        if not self._enqueue_pipeline_turn((audio, gen, cancel)):
            self._set(gen, IDLE)

    def _enqueue_pipeline_turn(self, item: tuple) -> bool:
        """Queue one turn without blocking the UI; newest pending work wins."""
        self._lifecycle_ensure()
        if self._is_closed():
            log.info("pipeline submit rejected after shutdown")
            return False
        lock = getattr(self, "_pipeline_submit_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._pipeline_submit_lock = lock
        q = self._pipeline_q
        with lock:
            if self._is_closed():
                log.info("pipeline submit rejected after shutdown")
                return False
            try:
                q.put_nowait(item)
                return True
            except queue.Full:
                try:
                    stale = q.get_nowait()
                except queue.Empty:
                    log.warning("pipeline submit rejected: queue busy")
                    return False
                stale[2].set()
                q.task_done()
                try:
                    q.put_nowait(item)
                    return True
                except queue.Full:
                    log.warning("pipeline submit rejected: queue busy")
                    return False

    def _log_utterance_health(self) -> None:
        """One compact journal line per accepted utterance so a post-mortem
        can correlate a command (gen) with the mic's condition at that exact
        moment — the answer to "I said X and it ignored me, was the mic
        already broken?". Never raises into the submit path."""
        try:
            ln = self._listener
            with ln._lock:
                state = ln._health_state_now_locked()
                device = (ln._health_open_device
                          or (str(SETTINGS["mic_device"])
                              if SETTINGS["mic_device"] else "system default"))
                rate = getattr(ln, "_capture_rate", None) or "-"
                frames = ln._frames_seen
                opens_failed = ln._health_opens_failed
            log.info(
                "utterance health: gen=%d src=%s mic=%s device=%s rate=%s "
                "frames=%d opens_failed=%d heal=%d",
                self._gen, "handsfree" if self._handsfree else "ptt",
                state, device, rate, frames, opens_failed,
                self._heal_attempts)
        except Exception:
            log.exception("utterance health line failed")

    # -- dictation (zero-LLM type-what-I-say) --------------------------------

    _DICTATION_RE = re.compile(
        r"(?:hey\s+\w+[,\s]+)?"
        r"(?:(start|begin|stop|end)\s+)?dictation(?:\s+mode)?[.!]?",
        re.IGNORECASE)

    def _try_dictation(self, text: str, gen: int,
                       cancel: threading.Event) -> bool:
        """Dictation fast path, checked before the wake gate so 'start
        dictation' needs no wake word. Returns True when the utterance was
        consumed (a toggle command, or transcribed speech to type)."""
        if not bool(SETTINGS.get("dictation", True)):
            return False
        m = self._DICTATION_RE.fullmatch(text.strip())
        if m:
            word = (m.group(1) or "").lower()
            on = ({"start": True, "begin": True, "stop": False,
                   "end": False}).get(word, not self._dictation)
            self._set_dictation(on, gen, cancel)
            return True
        if not getattr(self, "_dictation", False):   # bare/test instances
            return False
        # dictating: type the transcript into the focused window — same code
        # path the model's type_text tool uses, so the terminal fail-closed
        # guard and the permission switch apply unchanged
        self._set(gen, THINKING)
        out = self._tools.type_text(text)
        log.info("dictation: typed %d chars", len(text))
        if out.startswith("REFUSED"):
            log.warning("dictation refused: %s", out[:120])
            self._set_dictation(False, gen, cancel)
            self._speak("Dictation stopped — I can't type into the focused "
                        "window.", gen, cancel)
        else:
            self._set(gen, IDLE)
        return True

    def _set_dictation(self, on: bool, gen: int,
                       cancel: threading.Event) -> None:
        self._dictation = bool(on)
        log.info("dictation %s", "on" if on else "off")
        notify("dictation " + ("on" if on else "off"))
        self._set(gen, IDLE)
        self._speak("Dictation on — say stop dictation when done." if on
                    else "Dictation off.", gen, cancel)

    def _pipeline_worker(self) -> None:
        """Single consumer: runs one _pipeline at a time, in utterance order."""
        self._lifecycle_ensure()
        while True:
            audio, gen, cancel = self._pipeline_q.get()
            if audio is None:
                self._pipeline_q.task_done()
                return
            try:
                self._pipeline(audio, gen, cancel)
            except Exception as exc:
                self._report_turn_failure(exc, gen, cancel)
            finally:
                self._pipeline_q.task_done()

    def _report_turn_failure(self, exc: BaseException, gen: int,
                             cancel: threading.Event) -> None:
        """A turn that crashed must be REPORTED, not swallowed.

        This used to be `log.exception("pipeline worker crash")` and nothing
        else. The most common cause is the speech model being unavailable —
        whisper missing, unreadable or damaged, so `transcribe()` raises on
        every single utterance: the user spoke and got NOTHING back, for good,
        while the bubble still looked like it was listening and the journal
        said only "crash".

        Reported once per distinct cause (a broken model fails on every turn;
        repeating the alarm each time is its own bug) with the cause NAMED,
        and the bubble is put back to idle so it is not left stuck mid-turn.
        """
        key = f"{type(exc).__name__}: {exc}"[:160]
        log.error("turn failed: %s", key, exc_info=exc)
        first = self._report_once(
            "turn failed", exc, f"handsoff: that turn failed — {key}")
        try:
            self._set(gen, IDLE)
        except Exception:
            log.exception("could not reset the state after a failed turn")
        if not first or gen != self._gen or self._is_closed():
            return
        try:
            self._speak(f"Sorry — I could not handle that: {key}", gen, cancel)
        except Exception:
            log.exception("could not speak the turn-failure report")

    def _is_stop_utt(self, text: str) -> bool:
        """True for a bare stop command: stop, quiet, shut up, be quiet,
        silence, cancel, nevermind/never mind, that's all, stop it.
        Deliberately narrow (whole utterance, small word set) so normal
        sentences containing e.g. 'stop' are NOT swallowed."""
        words = [w for w in _norm_words(text.lower()) if w]
        if not words:
            return False
        if words and words[0] in _WAKE_FILLER:
            words = words[1:]
        if not words:
            return False
        joined = " ".join(words).strip(".,!?;:")
        return joined in {
            "stop", "stop it", "stop stop", "quiet", "be quiet", "silence",
            "shut up", "cancel", "nevermind", "never mind", "that's all",
            "thats all", "that is all",
        }

    def _maybe_instant_stop(self, audio: np.ndarray, gen: int) -> None:
        """Zero-LLM voice stop: transcribe in a throwaway thread the moment an
        utterance arrives; if it is a bare stop command, the pipeline turn is
        drained (interrupt() already silenced playback) and the transcript is
        handed to the brain anyway so 'stop the music' still works.

        The transcription is reused by _pipeline via _last_transcript, so the
        audio is never transcribed twice. `gen` is THIS utterance's generation,
        captured at submission: the probe thread may finish after a newer
        utterance has bumped _gen, and stamping then would attach this
        utterance's text to the wrong turn (cross-turn transcript reuse)."""
        def _probe() -> None:
            try:
                text = transcribe(audio)
            except Exception:
                log.exception("stop-probe transcription failed")
                return
            self._last_transcript = (text, gen, _tick_now())
            if self._is_stop_utt(text):
                log.info("voice stop: draining pipeline")
                # no lock here: get_nowait() takes the queue's own (non-
                # reentrant) mutex — holding it would deadlock. If the worker
                # dequeues first, _pipeline's stop check still drops it.
                # Only drop THIS utterance's turn (gen <= probe gen): a probe
                # finishing late must never swallow a newer utterance's turn.
                kept = []
                while True:
                    try:
                        item = self._pipeline_q.get_nowait()
                    except queue.Empty:
                        break
                    if item[1] <= gen:
                        self._pipeline_q.task_done()
                    else:
                        kept.append(item)
                for it in kept:
                    self._pipeline_q.put(it)

        threading.Thread(target=_probe, name="stop-probe", daemon=True).start()

    def abort_listening(self) -> None:
        # drag-path cancel: never block the Qt thread on stream.stop()/close()
        # — same off-thread stop worker as finish_listening, audio discarded.
        rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                self._ptt_ensure()
                self._ptt_stopping = True
                self._ptt_stop_recorder = rec
                rec._handsoff_stop_done = lambda _rec=rec: self._ptt_stop_owner_finished(_rec)
            except Exception:
                pass

            def _abort(_rec=rec) -> None:
                try:
                    try:
                        self._stop_recorder_bounded(_rec, timeout=3.0)
                    except Exception:
                        pass
                except Exception:
                    pass
                finally:
                    pass

            threading.Thread(target=_abort, name="ptt-abort", daemon=True).start()
        if self._handsfree:
            try:
                self._listener.resume()
            except Exception:
                pass
        try:
            self._set(self._gen, IDLE)
        except Exception:
            pass

    def interrupt(self) -> None:
        """Barge-in: any press cancels the current pipeline (speech/thought)
        AND any in-flight announcement (bubble-press silences TTS)."""
        self._cancel.set()
        try:
            with _ANNOUNCE_CANCEL_LOCK:
                _ANNOUNCE_CANCEL.set()
        except Exception:
            pass
        self._followup_until = 0.0    # barge-in also closes the follow-up window
        try:
            self._listener.reset()
        except Exception:
            pass

    # -- hands-free & remote control --------------------------------------------

    def _vad_speech(self, active: bool) -> None:
        """Called from the audio thread when the gate opens/closes."""
        if not self._handsfree:
            return
        if active and self.state == IDLE:
            self._set(self._gen, LISTENING)
        elif not active and self.state == LISTENING:
            self._set(self._gen, IDLE)

    def _on_utterance(self, audio: np.ndarray) -> None:
        self.submit_audio(audio)

    def set_handsfree(self, on: bool) -> None:
        on = bool(on)
        if on == self._handsfree:
            return
        self._handsfree = on
        try:
            # read-merge-write, NOT a dump of the in-memory snapshot: the
            # Settings app may have saved changes since our startup (model,
            # permissions, …) and this process's SETTINGS copy is stale —
            # dumping it would silently revert the user's saves.
            # (set_setting does exactly that merge under both locks.)
            saved = set_setting("handsfree", on)
        except OSError:
            saved = False
            log.exception("cannot persist handsfree setting")
        if saved is False:
            # The listener really did start/stop, so the session is fine — but
            # the choice is not on disk, and the next start will not honour it.
            # Saying so is the whole difference between a surprising revert and
            # a known one. (`except OSError` never fired: _persist_setting
            # reports failure, it does not raise.)
            log.warning(
                "hands-free is %s for this session but the setting was NOT "
                "saved — it will revert on restart", "on" if on else "off")
        if on:
            self._listener.start()
        else:
            self._listener.stop()
            self._set(self._gen, IDLE)
        self._mic_selfheal_rearm()   # a fresh stream is a clean slate
        log.info("hands-free %s", "enabled" if on else "disabled")
        notify(f"hands-free {'enabled' if on else 'disabled'}")

    def _reload_settings_live(self) -> None:
        """Re-read settings.json and apply without a restart.

        Runs on the Qt thread (via sigCommand): widget resizes are only
        legal there. Covers the settings dict, derived Ollama/model/audio
        globals, stale STT/TTS model caches, geometry, state colours and
        hands-free — everything the Appearance tab changes."""
        try:
            new = _load_settings()
        except Exception:
            log.exception("live settings reload: cannot load settings.json")
            return
        old_whisper = (SETTINGS.get("whisper_size"), SETTINGS.get("whisper_device"))
        old_voice = SETTINGS.get("tts_reference")
        SETTINGS.clear()
        SETTINGS.update(new)
        reload_derived_settings()
        # Both copies go under `_model_cache_lock`, the same lock the mirror
        # uses: dropping one side while a transcribe/get_tts call is between
        # its push and its load is how a dropped model came back and stayed.
        if (SETTINGS.get("whisper_size"), SETTINGS.get("whisper_device")) != old_whisper:
            with _model_cache_lock:
                globals()["_whisper_model"] = None
                try:
                    _audio._whisper_model = None
                except AttributeError:
                    pass
            log.info("live settings reload: whisper cache dropped (loads on next turn)")
        if SETTINGS.get("tts_reference") != old_voice:
            with _model_cache_lock:
                globals()["_tts_model"] = None
                try:
                    _audio._tts_model = None
                except AttributeError:
                    pass
            log.info("live settings reload: tts cache dropped "
                     "(reconditions on the next turn)")
        # The geometry and palette are re-derived by the `reload_derived_settings()`
        # call above, which now drives core.bubble.configure() — this used to
        # repeat half of that work here, which is exactly how the live path and
        # the startup path drifted apart.
        hf = bool(SETTINGS.get("handsfree", False))
        if hf != self._handsfree:
            self._handsfree = hf
            if hf:
                self._listener.start()
            else:
                self._listener.stop()
                self._set(self._gen, IDLE)
            self._mic_selfheal_rearm()
            log.info("live settings reload: hands-free %s", "on" if hf else "off")
        bw = getattr(self, "_bubble_widget", None)
        if bw is not None:
            try:
                bw.setFixedSize(_core_bubble.WINDOW_PX, _core_bubble.WINDOW_PX)
                bw.update()
            except RuntimeError:
                pass  # widget deleted during shutdown
        log.info("settings reloaded live (no restart)")

    def _on_command(self, action: str) -> None:
        if action.startswith("__timer:"):
            name, _, rep = action[len("__timer:"):].partition("\x1f")
            try:
                rep_h = float(rep) if rep else 0.0
            except ValueError:
                rep_h = 0.0
            self._fire_timer(name, rep_h)
        elif action == "start":
            self.begin_listening()
        elif action == "stop":
            self.finish_listening()
        elif action == "toggle":
            # NOTE: no `and not self._handsfree` here. That guard sent every
            # PTT press straight to the interrupt branch while hands-free was
            # on, so the key did nothing at all — begin_listening() now parks
            # the listener and records for the duration of the press.
            if self.state == LISTENING and self._recorder is not None:
                self.finish_listening()
            elif self.state == IDLE:
                self.begin_listening()
            else:
                self._listener.reset()
                self.interrupt()
                self._set(self._gen, IDLE)
        elif action == "interrupt":
            self._listener.reset()
            self.interrupt()
            self._set(self._gen, IDLE)
        elif action == "handsfree-on":
            self.set_handsfree(True)
            self._confirm_handsfree()
        elif action == "handsfree-off":
            self.set_handsfree(False)
            self._confirm_handsfree()
        elif action == "handsfree":
            self.set_handsfree(not self._handsfree)
            self._confirm_handsfree()
        elif action == "handsfree-status":
            self._confirm_handsfree()
        elif action in ("dictation", "dictation-on", "dictation-off"):
            on = (True if action == "dictation-on" else
                  False if action == "dictation-off"
                  else not self._dictation)
            gen, cancel = self._bump_gen()
            self._set_dictation(on, gen, cancel)
        elif action == "reload-settings":
            self._reload_settings_live()

    def _confirm_handsfree(self) -> None:
        """Speak a short confirmation after a hands-free toggle, including the
        current mic health so a silent/dead mic is obvious immediately."""
        try:
            snap = self.mic_health()
        except Exception:
            log.exception("handsfree confirmation: mic_health failed")
            snap = {}
        mic = (snap.get("mic") or {})
        state = mic.get("state") or "unknown"
        on = self._handsfree
        if on and state in ("silent", "open-failing"):
            spoken = (f"Hands-free on, but I can't hear you — microphone "
                      f"{state}.")
        elif on and state == "stopped":
            # opening race: the capture thread is still bringing the stream up
            spoken = "Hands-free on, mic starting."
        elif on:
            spoken = f"Hands-free on, {state}."
        elif state == "stopped":
            spoken = "Hands-free off."
        else:
            spoken = (f"Hands-free off, but the microphone is still "
                      f"{state}.")
        self._announce_now(spoken)

    def _announce_now(self, text: str) -> None:
        """Speak `text` outside any turn pipeline (no generation, no cancel):
        fresh event, IDLE state, background thread so the caller (a Qt slot)
        returns immediately. Playback itself serializes inside _speak on
        _ANNOUNCE_LOCK (no overlap) and each call cancels the previous
        playback first."""
        global _ANNOUNCE_CANCEL
        if self._is_closed():
            return
        gen = self._gen
        self._set(gen, IDLE)
        # ponytail: atomic cancel-swap under its own lock — no rebind-over-live
        # race, and never blocked behind a long _speak holding _ANNOUNCE_LOCK.
        with _ANNOUNCE_CANCEL_LOCK:
            try:
                _ANNOUNCE_CANCEL.set()  # play_wav honors cancel: stop the old one
            except Exception:
                pass
            cancel = threading.Event()
            _ANNOUNCE_CANCEL = cancel

        def _run(t=text, g=gen, c=cancel):
            self._speak(t, g, c)
            if not c.is_set() and g == self._gen and not self._is_closed():
                self._set(g, IDLE)

        self._start_worker(_run, name="announce")

    # -- pipeline (worker thread) --------------------------------------------------

    def _matches_recent_speech(self, text: str) -> bool:
        """True if `text` (already transcribed) substantially matches what we
        just played aloud — a safety net that stops the mic-from-speaker echo
        loop even when the state machine misses the TTS window. The caller
        owns clearing _recently_spoken."""
        if not self._recently_spoken:
            return False
        try:
            hit = _is_echo(text, self._recently_spoken)
            log.info("echo-check: heard <redacted> -> %s", hit)
            return hit
        except Exception:
            log.exception("echo-check failed")
            return False

    def _try_snooze(self, text: str, gen: int, cancel: threading.Event) -> bool:
        """Fast-path a 'snooze [N minutes]' reply while the offer window is live.

        Runs BEFORE the wake-word gate: a snooze is a direct answer to our own
        announcement and must not require the wake name. Returns True when the
        utterance was consumed."""
        offer, _expired = _snooze_offer.state()
        if offer is None:
            return False
        m = re.fullmatch(
            r"(?:hey\s+\w+[,\s]+)?(?:snooze|remind me again)"
            r"(?:\s+(?:in|for|by|more)?\s*(?:(\d{1,3})\s*)?"
            r"(?:more\s*)?(?:min(?:ute)?s?)?)?",
            text.strip(), re.I)
        if not m:
            return False
        minutes = int(m.group(1)) if m.group(1) else 10
        # Do NOT clear the offer first: a fired one-off reminder is already
        # pruned from reminders.json, so snooze_reminder NEEDS the offer to
        # re-arm it. Clearing early made the spoken snooze fail with "no
        # reminder matching" (verified). Clear only AFTER success.
        out, _err = self._tools.execute(
            "snooze_reminder", {"name": offer["name"], "minutes": minutes})
        if not out.startswith(("ERROR", "REFUSED")):
            _snooze_offer.clear()
        self._set(gen, IDLE)
        threading.Thread(target=lambda: self._speak(out, gen, cancel),
                         name="snooze-tts", daemon=True).start()
        return True

    def _pipeline(self, audio: np.ndarray, gen: int, cancel: threading.Event) -> None:
        try:
            if self._is_closed():
                return
            if not self._models_ready.wait(90):
                self._speak("I'm still loading my models, try again in a moment.", gen, cancel)
                return
            if cancel.is_set():
                return
            # reuse ONLY this turn's stop-probe transcription (matched by
            # generation, not time): a transcript belongs to exactly one
            # utterance — a time-based cache let a fast second utterance
            # execute the FIRST utterance's text (verified regression).
            cached, cached_gen, ts = self._last_transcript
            if cached and cached_gen == gen and _tick_now() - ts < 30:
                text = cached
            else:
                text = transcribe(audio)
            log.info("heard (gen=%d): %s", gen, text)
            # -- voice stop: a bare stop command must NEVER reach the brain
            #    (the interrupt already silenced playback; the brain would
            #    think for seconds and then speak again). Check BEFORE snooze:
            #    'stop' must silence even the snooze offer itself.
            if self._is_stop_utt(text):
                log.info("voice stop (pipeline): silencing, no brain turn")
                self._recently_spoken.clear()
                self._set(gen, IDLE)
                return
            # -- snooze fast-path runs BEFORE the echo check: the announcement
            #    itself says "say snooze", so the user's reply legitimately
            #    repeats our words and must not be discarded as an echo
            if self._try_snooze(text, gen, cancel):
                self._recently_spoken.clear()
                return
            # echo check: discard our own TTS bouncing off the speakers before
            # it can engage the wake word or reach the brain (worker thread —
            # never the GUI thread — and the audio is transcribed only once)
            if self._recently_spoken:
                hit = self._matches_recent_speech(text)
                self._recently_spoken.clear()
                if hit:
                    log.info("discarding echo of my own speech")
                    self._set(gen, IDLE)
                    return
            if cancel.is_set():
                return
            # -- dictation fast path (before the wake gate: 'start dictation'
            #    must work without addressing the assistant; snooze/stop keep
            #    priority above) -------------------------------------------
            if self._try_dictation(text, gen, cancel):
                return
            # -- wake-word gate (hands-free pre-command) -------------------
            if self._spotter_wake:
                self._spotter_wake = False     # audio spotter already gated this
            elif self._handsfree and _tick_now() < self._followup_until:
                # announce-and-listen: a reply just ended; take ONE follow-up
                # utterance without the wake word. The listener was live while
                # the assistant spoke and the echo guard already discarded its
                # own words, so whatever survives to here is the user.
                self._followup_until = 0.0     # exactly one utterance per reply
                log.info("follow-up accepted (no wake word)")
            elif self._handsfree and SETTINGS.get("wake_word_required"):
                now = _tick_now()
                if _is_wake_utt(text):
                    # bare wake name ('assistant' / 'hey assistant'): engage
                    # and confirm out loud (check BEFORE prefix match —
                    # _match_wake returns '' for bare names)
                    self._wake_until = now + float(SETTINGS.get("engage_seconds", 45.0))
                    log.info("wake word — engaged for %ss", SETTINGS.get("engage_seconds"))
                    self._set(gen, IDLE)
                    self._speak(
                        f"Yes? I'm listening for the next "
                        f"{int(SETTINGS.get('engage_seconds', 45.0))} seconds.",
                        gen, cancel)
                    return
                rest = _match_wake(text)
                if rest is not None:
                    self._wake_until = now + float(SETTINGS.get("engage_seconds", 45.0))
                    log.info("wake word — engaged for %ss", SETTINGS.get("engage_seconds"))
                    text = rest
                elif now >= self._wake_until:
                    log.info("ignored (no wake word)")
                    self._set(gen, IDLE)
                    return
            if not text:
                if self._handsfree and SETTINGS.get("wake_word_required") \
                        and _tick_now() < self._wake_until:
                    log.info("ignored (unintelligible while engaged)")
                    self._set(gen, IDLE)
                    return
                self._empty_streak += 1
                self._set(gen, IDLE)
                if self._empty_streak in (2, 4):
                    # gentle recovery: after repeated failures, say so
                    msg = ("I can hear sound but can't make out words. "
                           "Could you speak a bit closer to the microphone?")
                    threading.Thread(
                        target=lambda: self._speak(msg, gen, cancel),
                        name="recover", daemon=True,
                    ).start()
                return
            self._empty_streak = 0
            # rolling memory: capture durable facts NOW, before the token
            # budget trims this turn away — facts survive trimming by design
            facts = _extract_memories(text)
            if facts:
                self._memory = _merge_memories(self._memory, facts)
                _save_memory(self._memory)
                log.info("memory updated (%d fact(s))", len(facts))
            self._brain_turn(text, gen, cancel)
        except Exception:
            log.exception("pipeline failed")
            if not cancel.is_set():
                self._speak("Sorry, something went wrong.", gen, cancel)
        finally:
            if not cancel.is_set():
                self._set(gen, IDLE)

    _BRIEFING_SKIP_PREFIXES = (
        "open ", "close ", "type ", "press ", "run ", "restart", "move ",
        "go ", "switch ", "kill ", "edit ", "screenshot",
    )

    def _maybe_briefing_prefix(self, text: str) -> str:
        """Once a day, on the first conversational utterance, prepend live
        weather so the model delivers a spoken morning briefing."""
        if not SETTINGS.get("briefing"):
            return ""
        today = datetime.date.today().isoformat()
        if self._briefing_done_date == today:
            return ""
        low = text.strip().lower()
        if low.startswith(self._BRIEFING_SKIP_PREFIXES):
            return ""   # a command, not a greeting — don't hijack it
        place = str(SETTINGS.get("home_place", "")).strip()
        body = ""
        if place:
            out, err = self._tools.execute("get_weather", {"place": place})
            if err or out.startswith(("REFUSED", "ERROR")):
                log.info("briefing skipped: %s", out[:80])
                return ""
            body = out
        # ponytail: world news needs no home_place; weather keeps its own.
        events, _degraded = _world_events("all", 5)
        world_lines = []
        if events:
            for e in events[:4]:
                mark = "⚠ " if e.get("urgent") else ""
                world_lines.append(f"- {mark}{e['title']}")
            _world_mark_seen([e["title"] for e in events[:4]])
        self._briefing_done_date = today
        log.info("morning briefing delivered for %s", today)
        cal = _today_events_summary()
        _stamp = _load_mic_events().get("last_briefing")
        mic_probs = _recent_mic_problems(
            _stamp if isinstance(_stamp, (int, float))
            else time.time() - 24 * 3600)
        _mark_briefing_delivered()
        if cal:
            body += ("\n" if body else "") + f"Today\u2019s calendar: {cal}"
        if mic_probs:
            body += ("\n" if body else "") + mic_probs
        if world_lines:
            body += ("\n" if body else "") + "World:\n" + "\n".join(world_lines)
        if not body:
            return ""
        return ("[Daily briefing — greet the user briefly and naturally give "
                "this weather summary FIRST (plus calendar events, mic "
                "problems and world headlines if listed), "
                "then answer their request]\n" + body)

    def _conversation_for(self, text: str) -> list[dict]:
        """Build the full message list for a turn: system prompt + history +
        remembered facts + the user utterance.

        The memory block is the LAST system message (after history) so the
        long prefix — main system prompt + history — stays byte-identical
        across turns and Ollama's KV cache keeps hitting on it; only the
        small memory delta and the new utterance are evaluated."""
        _now = datetime.datetime.now()
        now = (f"{_core_calendar._DAY_NAMES[_now.weekday()]}, {_now.day:02d} "
               f"{_core_calendar._MONTH_NAMES[_now.month - 1]} {_now.year}, "
               f"{_now.hour:02d}:{_now.minute:02d}")
        system = (f"{SYSTEM_PROMPT}\n\nCurrent local date and time: {now}. "
                  "If the user asks about anything that depends on the current "
                  "date (weather today, 'tomorrow', news), use your tools.")
        briefing = self._maybe_briefing_prefix(text)
        user_content = (briefing + "\n\nThe user just said: " + text) if briefing else text
        conversation = [{"role": "system", "content": system}]
        conversation += list(self._history)
        if self._memory:
            facts = "\n".join(f"- {m['v']}" for m in self._memory)
            conversation.append({"role": "system",
                                 "content": "Facts you remember about the user:\n" + facts})
        # ponytail: consumed-once hardware note goes last (same prefix-cache
        # rationale as the memory block) and is cleared on attach.
        hw_note = (getattr(self, "_hardware_note", "") or "")[:200]
        self._hardware_note = ""
        if hw_note:
            conversation.append({"role": "system",
                                 "content": "Live hardware note:\n" + hw_note})
        conversation.append({"role": "user", "content": user_content})
        return conversation

    def _brain_turn(self, text: str, gen: int, cancel: threading.Event) -> None:
        set_turn = getattr(self._tools, "_set_user_turn", None)
        if set_turn is not None:
            set_turn(gen)
        conversation = self._conversation_for(text)
        self._turn_spoke = False
        for _round in range(MAX_TOOL_ROUNDS):
            if cancel.is_set():
                return
            tools = [t for t in TOOLS if SETTINGS["permissions"].get(t["function"]["name"], True)]
            stream_enabled = bool(SETTINGS.get("streaming_tts", True))
            if stream_enabled:
                # speak sentences while the model is still generating; tool
                # calls still collected from the stream so the loop keeps working
                turn = _brain.TurnStream(gen, cancel, queue.Queue())

                def _run_stream() -> None:
                    try:
                        turn.result = ollama_chat_stream(
                            conversation, turn.sentence_q, turn.cancel, tools)
                    except RuntimeError as e:
                        turn.result = {"tool_calls": [], "content": "", "error": str(e)}
                    finally:
                        turn.done.set()

                streamer = threading.Thread(target=_run_stream, daemon=True)
                streamer.start()
                self._speak(None, gen, cancel, sentence_q=turn.sentence_q)
                streamer.join(timeout=2.0)
                if streamer.is_alive():
                    # the model may still be finishing (slow tokens, big
                    # tool-call JSON) — waiting beats silently dropping the
                    # answer and every tool call
                    log.warning("stream worker slow to finish; waiting")
                    streamer.join(timeout=30.0)
                if turn.result is None:
                    if cancel.is_set():
                        return
                    log.error("stream finished without a result")
                    self._speak("Sorry, my brain gave me an empty answer.", gen, cancel)
                    return
                if turn.result.get("error"):
                    # ponytail: speak the HTTP failure; no empty turn appended
                    if cancel.is_set():
                        return
                    log.error("ollama stream: %s", turn.result["error"])
                    self._speak(f"Sorry, my brain is offline. {turn.result['error']}",
                                gen, cancel)
                    return
                tool_calls = turn.result.get("tool_calls") or []
                content = turn.result.get("content", "")
            else:
                # non-streaming fallback: run the blocking call in a helper
                # thread and watch `cancel` — barge-in must not leave the
                # pipeline worker stuck behind a quiet 300 s request. The
                # abandoned HTTP call finishes in its thread; the result is
                # simply discarded (the turn is over).
                box: dict = {}

                def _run_call() -> None:
                    try:
                        box["msg"] = ollama_chat(conversation, tools)
                    except RuntimeError as e:
                        box["err"] = e

                _t = threading.Thread(target=_run_call, name="brain-call",
                                      daemon=True)
                _t.start()
                while _t.is_alive():
                    if cancel.is_set():
                        log.info("non-streaming brain call abandoned (barge-in)")
                        return
                    _t.join(0.2)
                if "err" in box:
                    log.error("ollama: %s", box["err"])
                    self._speak(f"Sorry, my brain is offline. {box['err']}", gen, cancel)
                    return
                msg = box.get("msg") or {}
                content = _brain.strip_thinking(msg.get("content") or "")
                tool_calls = msg.get("tool_calls") or []
                if content:
                    self._speak(content, gen, cancel)
                    if cancel.is_set():
                        return
            if not tool_calls:
                # ponytail: never persist an empty assistant turn (stream
                # failure already spoke + returned; a bare empty reply adds
                # noise to history and the next prompt)
                if not (content or "").strip():
                    log.warning("empty model reply with no tool calls; not appending")
                    break
                conversation.append({"role": "assistant", "content": content})
                break
            conversation.append(
                {"role": "assistant", "content": content, "tool_calls": tool_calls}
            )
            stop_tool_loop = False
            for tc in tool_calls:
                if cancel.is_set():
                    return
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                log.info("tool call: %s (argument names=%s)", name, sorted(args))
                result, _err = self._tools.execute(name, args if isinstance(args, dict) else {})
                entry = {"role": "tool", "tool_name": name, "content": result}
                if self._tools._last_images:
                    entry["images"] = self._tools._last_images
                    log.info("attaching %d screenshot(s) to tool result", len(entry["images"]))
                conversation.append(entry)
                if getattr(self._tools, "_last_confirmation_offer", False):
                    stop_tool_loop = True
                    break
            if stop_tool_loop:
                break
        # only THIS turn may publish history: a newer utterance owns the
        # assistant's memory once it has started (its own pipeline will write)
        if gen == self._gen:
            self._history = _trim_history(
                [m for m in conversation[1:]
                 if m.get("role") != "system"])
            # ^ drop the main system prompt [1:] AND the memory block: the
            # block is injected fresh by _conversation_for on every turn —
            # persisting it would accumulate one stale copy per turn (token
            # bloat, broken KV-cache prefix, and a superseded fact could win)
            _strip_images(self._history)   # screenshots: this turn's model call only
            self._save_history()
        else:
            log.info("turn superseded at history-write; discarding")

    # -- speaking -----------------------------------------------------------------

    def _speak(self, text: str, gen: int, cancel: threading.Event,
               sentence_q: "queue.Queue[str | None] | None" = None) -> None:
        """Speak `text` now. With a sentence queue: speak each sentence as it
        arrives (streaming TTS — playback starts while the model still writes).

        ONE speech serializer: every playback (brain streaming/non-streaming,
        timers, snooze, say_now, crash/missed) funnels through _ANNOUNCE_LOCK
        around playback so two OutputStreams can never overlap. Synthesis is
        intentionally outside the lock so slow TTS does not block other speech
        from reaching its cancellation/playback check."""
        if _tts_model is None and not self._models_ready.is_set():
            self._models_ready.wait(30)
        if sentence_q is None:
            text = (text or "").strip()
            if not text or cancel.is_set():
                return
            self._set(gen, SPEAKING)
            # Armed BEFORE playback: the mic hears our own voice while it
            # plays, so the echo filter needs the text ahead of time.
            self._recently_spoken.append(text)
            del self._recently_spoken[:-2]
            try:
                with tempfile.TemporaryDirectory(dir=str(STATE_DIR)) as td:
                    wav = Path(td) / "tts.wav"
                    tts_to_wav(text, wav)
                    with _ANNOUNCE_LOCK:
                        if not cancel.is_set():
                            log.info("saying response (%d chars)", len(text))
                            _audio.play_wav(wav, cancel)
            except Exception as exc:
                log.exception("TTS failed")
                # "Spoke" must mean the user HEARD it: these three used to be
                # set before synthesis, so an unreadable speech model left the
                # bubble believing it had replied — the follow-up window
                # opened on nothing and the user's next utterance was
                # echo-filtered against a line that was never spoken.
                self._unarm_speech(text)
                self._report_speech_failure(exc)
            else:
                self._last_spoken = text
                self._turn_spoke = True
            # announce-and-listen: a full spoken reply opens a short window in
            # which the NEXT utterance is taken without the wake word. Only
            # after natural completion — an interrupted (barged-in) reply
            # opens nothing, or the barge-in speech would arm its own window.
            if self._turn_spoke and not cancel.is_set() and self._handsfree \
                    and float(SETTINGS.get("followup_seconds", 0.0)) > 0.0:
                self._followup_until = _tick_now() + float(
                    SETTINGS["followup_seconds"])
                log.info("follow-up window open for %ss",
                         SETTINGS["followup_seconds"])
            return
        said: list[str] = []
        while True:
            try:
                sentence = sentence_q.get(timeout=0.5)
            except queue.Empty:
                if cancel.is_set():
                    return
                continue
            if sentence is None:
                break
            self._set(gen, SPEAKING)
            self._recently_spoken.append(sentence)   # before playback: echo
            del self._recently_spoken[:-2]
            try:
                with tempfile.TemporaryDirectory(dir=str(STATE_DIR)) as td:
                    wav = Path(td) / "tts.wav"
                    tts_to_wav(sentence, wav)
                    with _ANNOUNCE_LOCK:
                        if cancel.is_set():
                            return
                        log.info("saying: %s", sentence)
                        _audio.play_wav(wav, cancel)
            except Exception as exc:
                log.exception("TTS failed (streaming)")
                self._unarm_speech(sentence)
                self._report_speech_failure(exc)
                continue
            said.append(sentence)
            self._last_spoken = sentence
            self._turn_spoke = True
        self._last_spoken = " ".join(said)
        # announce-and-listen (streaming path): same arming as above
        if self._turn_spoke and not cancel.is_set() and self._handsfree \
                and float(SETTINGS.get("followup_seconds", 0.0)) > 0.0:
            self._followup_until = _tick_now() + float(
                SETTINGS["followup_seconds"])
            log.info("follow-up window open for %ss", SETTINGS["followup_seconds"])

    def _unarm_speech(self, text: str) -> None:
        """Withdraw `text` from the echo list: it was queued for playback but
        synthesis or playback failed, so it was never actually spoken."""
        try:
            self._recently_spoken.remove(text)
        except ValueError:
            pass

    def _report_once(self, kind: str, exc: BaseException,
                     message: str) -> bool:
        """Notify the user ONCE per distinct cause; True when first.

        The caller has already logged the occurrence (with its traceback);
        this decides whether the PERSON is told. These failures repeat — a
        broken voice fails on every reply, a full disk fails on every tick —
        so a notification per occurrence would be worse than the silence it
        replaces.
        """
        key = f"{kind}: {type(exc).__name__}: {exc}"[:200]
        seen = getattr(self, "_failures_reported", None)
        if seen is None:
            seen = self._failures_reported = set()
        if key in seen:
            return False
        seen.add(key)
        try:
            if not self._is_closed():
                notify(message)
        except Exception:
            log.exception("could not report %s", kind)
        return True

    def _report_speech_failure(self, exc: BaseException) -> None:
        """Tell the user their reply could not be spoken (once per cause)."""
        key = f"{type(exc).__name__}: {str(exc)[:160]}"
        self._report_once("speech synthesis unavailable", exc,
                          f"handsoff: I could not speak that reply — {key}")

    # -- restart bookkeeping ---------------------------------------------------

    def _prepare_restart(self) -> None:
        """Called just before the restart script runs; leaves a note for our next self."""
        note = "" if self._turn_spoke else "I'm back, with my changes applied."
        try:
            _core_settings._atomic_private_write(PENDING_FILE, json.dumps({"note": note}))
        except OSError:
            pass

    # -- history ------------------------------------------------------------------

    @staticmethod
    def _load_history() -> list[dict]:
        try:
            data = json.loads(HISTORY_FILE.read_text())
        except FileNotFoundError:
            return []
        except ValueError:
            _core_settings._quarantine_bad(HISTORY_FILE)
            return []
        except OSError:
            return []
        except Exception:
            return []
        if not isinstance(data, list):
            _core_settings._quarantine_bad(HISTORY_FILE)
            return []
        try:
            msgs = [m for m in data if isinstance(m, dict) and m.get("role") and m.get("content") is not None]
            # heal histories written by older versions, which persisted the
            # per-turn memory block (it is injected fresh each turn instead)
            msgs = [m for m in msgs if m.get("role") != "system"]
            return _trim_history(msgs)
        except Exception:
            return []

    def _save_history(self) -> None:
        try:
            _backup_runtime_json(HISTORY_FILE)
            _core_settings._atomic_private_write(
                HISTORY_FILE, json.dumps(self._history, ensure_ascii=False, indent=1))
        except OSError:
            log.exception("cannot save history")

    def clear_history(self) -> int:
        """Drop the conversation memory and persist the empty history.

        Returns how many messages were dropped. This exists because the
        history is held in `self._history` and `_save_history` rewrites the
        whole file: another process truncating HISTORY_FILE alone would be
        undone on the bubble's next turn, which is why a model switch in
        Settings has to ask the RUNNING bubble to forget as well.
        """
        dropped = len(self._history)
        self._history = []
        self._save_history()
        log.info("conversation history cleared (%d message(s) dropped)", dropped)
        return dropped


# -- rolling memory: durable facts that survive history trimming -------------------

MAX_MEMORY_FACTS = 24


def _extract_memories(text: str) -> list[tuple[str, str]]:
    """Pull durable facts from one user utterance: [(key, fact), ...].

    The key groups replaceable facts (a new 'my name is X' replaces the old
    one); the fact is a sentence the model can read back. The matcher is
    deliberately conservative — it only catches explicit statements, never
    guesses. Facts are extracted at ingest time, so when the token budget
    later trims the conversation, the facts survive."""
    out: list[tuple[str, str]] = []
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return out
    low = t.lower()
    # value matcher: capture until a sentence/coordinate boundary
    _VAL = r"([A-Za-z][\w .'-]*?)(?=\s+(?:and|but|then|,|\.|!|\?)|$)"

    def add(key: str, fact: str) -> None:
        words = fact.split()
        if 4 < len(fact) <= 140 and len(words) <= 16:
            out.append((key, fact))

    # name: "my name is John" / "call me John" / "I'm called John"
    m = re.search(
        r"\b(?:my name is|i'?m called|call me|you can call me)\s+"
        r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", t)
    if m and not re.search(r"\b(?:later|back|tomorrow|soon|again)\b", low):
        add("name", f"the user's name is {m.group(1)}")

    # relationship: "my sister is Anna" / "my brother's name is Tom"
    rel = re.search(
        r"\bmy (sister|brother|mom|mother|dad|father|wife|husband|girlfriend|"
        r"boyfriend|son|daughter|friend|boss|manager|neighbor)(?:'s name)?\s+is\s+"
        r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", t)
    if rel:
        add(f"rel:{rel.group(1)}", f"the user's {rel.group(1)} is {rel.group(2)}")

    # like / dislike: "I like jazz" / "I don't like pineapple"
    like = re.search(
        r"\bi (?:really |totally |absolutely )?(?:like|love|enjoy)\s+"
        r"(.+?)(?:\.|!|\?|,|\band\b|\bbecause\b|$)", low)
    if like and len(like.group(1).split()) <= 6:
        add("like", f"the user likes {like.group(1).strip()}")
    dislike = re.search(
        r"\bi (?:really |totally )?(?:don't like|do not like|hate|can't stand)\s+"
        r"(.+?)(?:\.|!|\?|,|\bbecause\b|$)", low)
    if dislike and len(dislike.group(1).split()) <= 6:
        add("dislike", f"the user dislikes {dislike.group(1).strip()}")

    # home / work / pet / favorite / origin / allergy (re.I keeps name case)
    home = re.search(r"\bi\s+(?:live|living)\s+in\s+" + _VAL, t, re.I)
    if home:
        add("home", f"the user lives in {home.group(1).strip()}")
    work = re.search(r"\bi\s+work\s+(?:at|for)\s+" + _VAL, t, re.I)
    if work:
        add("work", f"the user works at {work.group(1).strip()}")
    pet = re.search(
        r"\bi\s+have\s+(?:a|an)\s+(cat|dog|bird|hamster|rabbit|fish|parrot)"
        r"(?:\s+(?:called|named)\s+([A-Za-z][A-Za-z]*))?", t, re.I)
    if pet:
        name = f" called {pet.group(2)}" if pet.group(2) else ""
        add("pet", f"the user has a {pet.group(1).lower()}{name}")
    fav = re.search(r"\bmy favorite ([a-z]+)\s+is\s+" + _VAL, t, re.I)
    if fav:
        add(f"fav:{fav.group(1).lower()}",
            f"the user's favorite {fav.group(1).lower()} is {fav.group(2).strip()}")
    origin = re.search(r"\bi'?m\s+from\s+" + _VAL, t, re.I)
    if origin:
        add("origin", f"the user is from {origin.group(1).strip()}")
    allergy = re.search(r"\bi'?m\s+allergic\s+to\s+" + _VAL, low, re.I)
    if allergy:
        add("allergy", f"the user is allergic to {allergy.group(1).strip()}")
    return out


def _merge_memories(current: list[dict], new: list[tuple[str, str]]) -> list[dict]:
    """Merge new (key, fact) pairs into the current memory list.

    Replace by key in place (a new name supersedes the old, keeping its
    original position), append genuinely new keys, dedupe identical fact
    text, cap at MAX_MEMORY_FACTS (dropping the oldest)."""
    merged: list[dict] = []
    pos: dict[str, int] = {}
    for item in current:
        k, v = str(item.get("k", "")), str(item.get("v", ""))
        if k and v and k not in pos:
            pos[k] = len(merged)
            merged.append({"k": k, "v": v})
    for k, v in new:
        if k in pos:
            merged[pos[k]]["v"] = v          # replace, keep position
        else:
            pos[k] = len(merged)
            merged.append({"k": k, "v": v})
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in merged:
        vl = item["v"].lower()
        if vl not in seen:
            seen.add(vl)
            deduped.append(item)
    while len(deduped) > MAX_MEMORY_FACTS:
        deduped.pop(0)
    return deduped


def _load_memory() -> list[dict]:
    try:
        data = json.loads(MEMORY_FILE.read_text())
    except FileNotFoundError:
        return []
    except ValueError:
        _core_settings._quarantine_bad(MEMORY_FILE)
        return []
    except OSError:
        return []
    except Exception:
        return []
    if not isinstance(data, list):
        _core_settings._quarantine_bad(MEMORY_FILE)
        return []
    try:
        items = [{"k": str(m.get("k", "")), "v": str(m.get("v", ""))}
                 for m in data if isinstance(m, dict)
                 and str(m.get("k", "")) and str(m.get("v", ""))]
        return _merge_memories(items, [])[:MAX_MEMORY_FACTS]
    except Exception:
        return []


def _save_memory(items: list[dict]) -> None:
    try:
        _backup_runtime_json(MEMORY_FILE)
        _core_settings._atomic_private_write(
            MEMORY_FILE, json.dumps(items, ensure_ascii=False, indent=1))
    except OSError:
        log.exception("cannot save memory")


_ECHO_STOPWORDS = frozenset("""
a an the is are am i you me my your it its this that these those of to in on at
for and or but so do does did can could would should will what when where who
how why please just now ok okay hey there then
""".split())


def _is_echo(text: str, recent: list[str]) -> bool:
    """True if the freshly transcribed capture substantially repeats lines we
    just spoke aloud. Compares distinctive (non-stopword) words: a capture is
    an echo when its single distinctive word matches, or >= 60% of its
    distinctive words appear in the recent TTS text."""
    if not text or not recent:
        return False

    def _words(s: str) -> list[str]:
        return [w for w in re.findall(r"[a-z']+", s.lower())
                if len(w) > 2 and w not in _ECHO_STOPWORDS]

    said = set(_words(" ".join(recent)))
    got = set(_words(text))
    if not got or not said:
        return False
    overlap = sum(1 for w in got if w in said)
    if len(got) == 1:
        return overlap == 1
    return overlap >= max(2, int(0.6 * len(got)))


def _msg_tokens(m: dict) -> int:
    """Rough token estimate for one history message (chars/4, conservative)."""
    c = m.get("content") or ""
    if not isinstance(c, str):
        c = json.dumps(c, ensure_ascii=False)
    n = len(c) // HISTORY_CHARS_PER_TOKEN
    for tc in m.get("tool_calls") or []:
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        n += len(str(fn.get("name", ""))) // HISTORY_CHARS_PER_TOKEN
        n += len(str(fn.get("arguments", ""))) // HISTORY_CHARS_PER_TOKEN
    return n + 4                      # per-message framing overhead


def _trim_history(msgs: list[dict]) -> list[dict]:
    """Trim from the front until the estimated token count fits the budget,
    never splitting an assistant-tool_calls/tool sequence, and always keeping
    the most recent message. The 40-message cap still applies as a hard
    backstop. Token counts are computed once (O(n)), not re-summed per pop."""
    out = list(msgs)
    n = len(out)
    start = 0
    while n - start > MAX_HISTORY_MESSAGES:
        start += (n - start - MAX_HISTORY_MESSAGES)
        while start < n and out[start].get("role") != "user":
            start += 1
    out = out[start:]
    if not out:
        return out
    budget = max(0, _history_budget())
    toks = [_msg_tokens(m) for m in out]
    total = sum(toks)
    n = len(out)
    start = 0
    while n - start > 1 and total > budget:
        total -= toks[start]
        start += 1
        while n - start > 1 and out[start].get("role") != "user":
            total -= toks[start]  # don't orphan tool results / assistant turns
            start += 1
        if out[start].get("role") != "user" and total > budget:
            break                    # single over-budget message: keep it
    return out[start:]


# -------------------------------------------------------------------------- bubble UI
#
# The window's own outline, per design. Most designs are painted inside the
# inscribed ellipse of the square window, so the ellipse IS their shape. A
# design whose outline leaves it (ears, a tail) has to say so here: the mask is
# what decides which painted pixels survive on the desktop, and the previous
# ellipse-for-everything rule is exactly why every design had to be a circle
# and why a design with its own silhouette would arrive with the parts sheared
# off. `design_region` only ever ADDS to the ellipse, so it cannot clip a design
# that used to fit.



# ------------------------------------------------------------------ control socket


PTT_ACTIONS = {"start", "stop", "toggle", "interrupt",
               "handsfree", "handsfree-on", "handsfree-off",
               "handsfree-status", "dictation", "dictation-on", "dictation-off",
               "status", "health", "level", "doctor", "settings", "selftest",
               "reload-settings", "clear-history", "say"}


def _peer_uid(conn: "socket.socket") -> "int | None":
    """uid of the process at the far end of a unix socket, or None if unknown.

    Defence in depth, and it is worth being exact about what it does NOT do.
    The real boundary is the state directory: `_prepare_runtime` refuses to
    start unless STATE_DIR is owner-only and not a symlinked directory, and a
    0700 directory is what stops another USER from reaching the socket at all
    (the inode's own 0600 mode does not gate connect()). SO_PEERCRED reports
    the peer's uid, and a same-UID process — a compromised child of ours, a
    sandboxed app running as us — has the SAME uid, so this cannot exclude
    that case. What it adds is an explicit check that still holds if the
    directory mode is ever loosened, and a log line saying who tried.
    """
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return int(uid)
    except (OSError, AttributeError, struct.error):
        return None                  # non-Linux or no credentials: allow


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

    def __init__(self, assistant: "Assistant") -> None:
        self._assistant = assistant
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._runs = _core_registry.BoundedRegistry("control", 1)
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
        self._diag = _core_registry.BoundedRegistry("diagnostic", 1)

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
            log.warning("cap refusal: %s", self._diag.refusal_line(detail))
            try:
                _record_cap_refusal({**(self._diag.refusal_report() or {}),
                                     "detail": detail})
            except Exception:
                log.exception("cap-refusal recorder failed")
            # ...and say it: a refusal the user asked for by pressing the
            # health keybind must not be discoverable only in the journal.
            try:
                self._assistant.announce_cap_refusal(
                    self._diag.refusal_report() or {}, detail)
            except Exception:
                log.exception("cap-refusal announcement failed")
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
            log.info("control socket already accepting")
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
            _remove_stale_control_socket()
        except OSError:
            log.exception("could not remove control socket during shutdown")

    def _serve(self) -> None:
        server = None
        try:
            if not _prepare_runtime():
                raise OSError("runtime/config directories or files are not private")
            _remove_stale_control_socket()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(CONTROL_SOCK))
            os.chmod(CONTROL_SOCK, 0o600)
            server.listen(4)
            server.settimeout(1.0)
            self._bound_path = str(CONTROL_SOCK)
            self._sock_ident = self._ident_of(CONTROL_SOCK)
            self._server = server
        except OSError as e:
            log.error("control socket unavailable: %s", e)
            if server is not None:
                server.close()
            return
        log.info("control socket at %s", CONTROL_SOCK)
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
                    if str(CONTROL_SOCK) != self._bound_path:
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
                        log.error(
                            "control socket path %s now belongs to another inode — "
                            "'--ptt' reaches whoever owns it, not this bubble "
                            "(a second handsoff, or a stale path from another run)",
                            CONTROL_SOCK)
                    continue
                except OSError:
                    if not self._stop.is_set():
                        log.exception("control socket accept failed")
                    return
                try:
                    conn.settimeout(5.0)
                    # Read the request BEFORE the credential check: closing a
                    # socket that still holds the peer's unread bytes sends
                    # RST, so a refused caller would see a connection reset
                    # instead of the reason. Reading first keeps the refusal
                    # legible (the check still gates every dispatch).
                    raw = conn.recv(1024).decode("utf-8", "replace").strip()
                    # Split the optional argument off BEFORE lowercasing: `say`
                    # carries the text to synthesize, so lowercasing the whole
                    # payload would make the bubble read a different sentence
                    # than the one the user typed.
                    action, _, action_arg = raw.partition(" ")
                    action = action.lower()
                    peer = _peer_uid(conn)
                    if peer is not None and peer != os.getuid():
                        log.warning(
                            "control socket: refusing peer uid %s (ours is %s)",
                            peer, os.getuid())
                        conn.sendall(b"error: not permitted\n")
                        continue

                    def _with_timeout(fn, timeout_s: float):
                        """Slow diagnostics, off the accept thread (see
                        ControlServer._diagnostic_call)."""
                        return self._diagnostic_call(fn, timeout_s)

                    if action in PTT_ACTIONS:
                        if action == "status":
                            reply = (f"state={self._assistant.state} "
                                     f"handsfree={'on' if self._assistant._handsfree else 'off'} "
                                     f"model={OLLAMA_MODEL}")
                        elif action == "health":
                            try:
                                snap = _with_timeout(
                                    self._assistant.mic_health, 4.0)
                                reply = json.dumps(snap, ensure_ascii=False)
                            except Exception as e:  # noqa: BLE001
                                log.exception("health snapshot failed")
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
                                reply = _with_timeout(run_doctor, 4.5)
                            except Exception as e:  # noqa: BLE001
                                log.exception("doctor report failed")
                                reply = f"error: doctor report failed: {e}"
                        elif action == "say":
                            reply = self._assistant.say_preview(action_arg)
                        elif action == "settings":
                            if SETTINGS_APP.exists():
                                subprocess.Popen(
                                    [sys.executable, str(SETTINGS_APP)], start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                )
                                reply = "ok: settings window launched"
                            else:
                                reply = f"ERROR: settings app missing at {SETTINGS_APP}"
                        else:
                            self._assistant.sigCommand.emit(action)
                            reply = f"ok: {action}"
                    else:
                        reply = (f"error: unknown command '{action}'. "
                                 f"commands: {' '.join(sorted(PTT_ACTIONS))}")
                    conn.sendall((reply + "\n").encode("utf-8"))
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
                _remove_stale_control_socket()
            except OSError:
                log.exception("could not remove control socket")


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
            info = CONTROL_SOCK.lstat()
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
        if not self._bound_path or str(CONTROL_SOCK) != self._bound_path:
            return None      # only ever repair the path this server bound
        try:
            # Same guard startup uses: refuses a symlink, a foreign owner, or a
            # non-socket at that path rather than unlinking it.
            _remove_stale_control_socket()
            fresh = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fresh.bind(str(CONTROL_SOCK))
            os.chmod(CONTROL_SOCK, 0o600)
            fresh.listen(4)
            fresh.settimeout(1.0)
        except OSError as e:
            if not self._orphan_reported:
                self._orphan_reported = True
                log.error(
                    "control socket path is gone and could not be re-bound (%s) — "
                    "'--ptt' cannot reach this bubble until it restarts", e)
            return None
        log.warning(
            "control socket path was removed while running — re-bound at %s "
            "(clients that failed in the meantime should retry)", CONTROL_SOCK)
        self._sock_ident = self._ident_of(CONTROL_SOCK)
        self._orphan_reported = False
        try:
            server.close()
        except OSError:
            pass
        self._server = fresh
        return fresh


def _remove_stale_control_socket() -> None:
    """Remove a previous control socket only when it is safe to do so.

    Never unlink a symlink, regular file, foreign-owned inode, or directory.
    A stale socket from our own previous process is the only thing startup may
    replace; anything else is a hard startup error instead of a path-traversal
    or data-loss surprise.
    """
    try:
        info = CONTROL_SOCK.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise OSError(f"refusing symlinked control socket: {CONTROL_SOCK}")
    if info.st_uid != os.getuid():
        raise OSError(f"control socket is not owned by uid {os.getuid()}")
    if not stat.S_ISSOCK(info.st_mode):
        raise OSError(f"control socket path is not a socket: {CONTROL_SOCK}")
    # last-owner wins: re-lstat through the unlink so a swap between the two
    # lstats above and this unlink cannot make us remove someone else's file
    try:
        CONTROL_SOCK.unlink()
    except FileNotFoundError:
        pass


USAGE = """usage: python handsoff.py --ptt <command>   (remote-control a running bubble)

commands:
  start          begin recording now (finish with 'stop')
  stop           finish recording and send it to the assistant
  toggle         start if idle; stop+send if recording; interrupt otherwise
  interrupt      make the bubble stop talking/thinking immediately
  handsfree      toggle continuous hands-free listening
  handsfree-on   enable continuous hands-free listening
  settings       open the settings window (works even if the bubble is dead)  handsfree-off  disable continuous hands-free listening
  handsfree-status  speak the hands-free and microphone health state
  dictation      toggle voice dictation (type what you say, no AI turn)
  dictation-on   enable voice dictation
  dictation-off  disable voice dictation
  reload-settings  re-read settings.json and apply without restart
  clear-history  forget the stored conversation (used when the brain model
                 changes, so a new model cannot parrot the old transcript)
  status         report state, hands-free mode and model
  health         full JSON health: mic, brain (Ollama) and TTS status
  level          live JSON voice level: the signal every bubble design paints
                 from (raw, the smoothed 'ui' value the painters read, which
                 producer sent it and how long ago) — what Settings → Voice
                 polls for its meter
  doctor         human-readable diagnostic: deployment hashes, Ollama, mic, niri, systemd
  say <text>     speak <text> now as a voice preview (Settings → Voice uses this
                 so the preview is the bubble's own voice, not a second model)
  selftest       run the hardware typing checks (launches scratch windows on
                 THIS desktop, types only into them, restores the clipboard)"""


def ptt_client(argv: list[str]) -> int:
    action = argv[0].lower() if argv else ""
    if action not in PTT_ACTIONS:
        sys.stderr.write(USAGE if not action else f"unknown command: {action}\n{USAGE}")
        return 2
    if action == "settings":
        # works even when the bubble is dead: launch the settings app directly
        if SETTINGS_APP.exists():
            subprocess.Popen(
                [sys.executable, str(SETTINGS_APP)], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log.info("settings window launched")
            return 0
        sys.stderr.write(f"handsoff: settings app not installed at {SETTINGS_APP}\n")
        return 1
    if action == "selftest":
        # runs LOCALLY (not via the bubble): it drives its own scratch windows
        # and needs no assistant state, so it must also work when the bubble
        # is dead — same contract as `settings`
        print(run_typing_selftest())
        return 0
    if action == "doctor":
        # the running bubble knows its live mic/brain state — ask it first;
        # but a dead bubble must still report (deployment hashes, systemd,
        # restart script), so fall back to a local run
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(str(CONTROL_SOCK))
            s.sendall(b"doctor")
            s.shutdown(socket.SHUT_WR)
            reply = b""
            while True:
                part = s.recv(4096)
                if not part:
                    break
                reply += part
            print(reply.decode("utf-8", "replace").strip())
            return 0
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            pass
        print("(bubble not running — local report)\n")
        print(run_doctor())
        return 0
    payload = " ".join(argv)
    if action == "say" and not payload.partition(" ")[2].strip():
        sys.stderr.write("usage: python handsoff.py --ptt say <text>\n")
        return 2
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(CONTROL_SOCK))
        s.sendall(payload.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            part = s.recv(1024)
            if not part:
                break
            reply += part
        print(reply.decode("utf-8", "replace").strip())
        return 0
    except (FileNotFoundError, ConnectionRefusedError):
        # Name the path: "not running" is the common case, but the interesting
        # one is a client looking somewhere the bubble is not (a different
        # XDG_STATE_HOME, a test HOME, a stale symlink) — then the message the
        # user sees is the only clue about which socket was tried.
        sys.stderr.write(f"handsoff is not running (no control socket at {CONTROL_SOCK})\n")
        return 1
    except OSError as e:
        sys.stderr.write(f"control socket error: {e}\n")
        return 1


# ------------------------------------------------------------------------------ main


def _truncate_file_preserving_fd(path: Path) -> None:
    """Truncate a crash log without replacing its inode or faulthandler fd."""
    with path.open("r+b") as fh:
        fh.truncate(0)
        fh.flush()


def _strip_images(history: list) -> None:
    """Drop screenshot payloads from history entries — they exist for the
    current model call only; re-sending megabytes of base64 on every later
    turn (and storing them in history.json) just burns tokens and disk."""
    for m in history:
        if isinstance(m, dict):
            m.pop("images", None)


def acquire_lock():
    """One bubble per session: an flock on the state dir.

    Under systemd Restart=always a just-killed instance may hold the lock for
    a moment — a brief retry loop instead of giving up. Failed attempts close
    their handle so nothing leaks."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in range(LOCK_RETRIES + 1):
        if attempt:
            time.sleep(LOCK_RETRY_WAIT)
        fh = None
        try:
            # ponytail: "a+" is O_CREAT|O_RDWR with no O_TRUNC — truncating
            # before flock lets two racers both wipe the pid file; truncate
            # only after the non-blocking lock is held.
            fh = open(LOCK_FILE, "a+")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fh is not None:
                try:
                    fh.close()          # don't leak the handle on a failed attempt
                except OSError:
                    pass
            continue
        os.chmod(LOCK_FILE, 0o600)  # the pid inside is nobody's business
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    log.error(
        "another handsoff instance still holds the lock after %.1fs; exiting",
        LOCK_RETRIES * LOCK_RETRY_WAIT,
    )
    sys.stderr.write("handsoff: another instance holds the lock; exiting.\n")
    return None


def main() -> int:
    if "--ptt" in sys.argv:
        return ptt_client(sys.argv[sys.argv.index("--ptt") + 1:])
    if not _prepare_runtime():
        sys.stderr.write(
            "handsoff: refusing to start because config/state paths are "
            "not private or are redirected by a symlink.\n")
        return 1
    setup_logging()
    sys.excepthook = lambda *a: log.exception("uncaught exception", exc_info=a)
    threading.excepthook = lambda a: log.exception("uncaught thread exception", exc_info=a.exc_type)
    crash_fh = open(CRASH_LOG, "a", buffering=1)
    os.chmod(CRASH_LOG, 0o600)
    faulthandler.enable(crash_fh)  # native aborts (CUDA, Qt)
    log.info("handsoff %s starting (python %s, self=%s)", VERSION, sys.version.split()[0], SELF_PATH)
    log.info(
        "settings: model=%s num_ctx=%s whisper=%s streaming_tts=%s handsfree=%s "
        "wake=%s(name=%s/%ss)",
        OLLAMA_MODEL, OLLAMA_NUM_CTX, WHISPER_SIZE,
        SETTINGS.get("streaming_tts"), SETTINGS.get("handsfree"),
        bool(SETTINGS.get("wake_word_required")), _wake_name(),
        SETTINGS.get("engage_seconds"),
    )
    log.info(
        "brain: ollama %s model %s (ctx %d) | stt: whisper %s | tts: %s (rate %.2f, vol %.2f)",
        OLLAMA_BASE, OLLAMA_MODEL, OLLAMA_NUM_CTX, WHISPER_SIZE,
        f"{TTS_ENGINE} ({TTS_REFERENCE or 'built-in voice'})",
        SETTINGS["tts_rate"], SETTINGS["tts_volume"],
    )
    log.info(
        "mic: %s (threshold %d) | bubble: %d px",
        SETTINGS["mic_device"] or "system default", SETTINGS["mic_threshold"],
        _core_bubble.WINDOW_PX,
    )
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        log.warning("no WAYLAND_DISPLAY/DISPLAY in environment; the window may fail to open")

    lock = acquire_lock()
    if lock is None:
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    QGuiApplication.setDesktopFileName(APP_NAME)  # Wayland app-id → niri window rules

    assistant = Assistant()
    bubble = _core_bubble.BubbleWidget(assistant)
    control = ControlServer(assistant)
    assistant.start()
    control.start()
    app.aboutToQuit.connect(control.stop)
    app.aboutToQuit.connect(assistant.shutdown)
    bubble.show()
    try:
        return app.exec()
    finally:
        control.stop()
        assistant.shutdown()
        try:
            lock.close()
        except OSError:
            pass


# The app is WHOLE: every name above exists. The canonical registration was
# published at the top (`_claim_app_name`) so that a second copy is refused
# before it can touch shared state, and this flag is what tells a loader the
# module it found is finished rather than still executing its own body — see
# `core.app_module`, which hands out only a ready app.
__app_ready__ = True


if __name__ == "__main__":
    sys.exit(main())
