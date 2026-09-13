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
_SETTINGS_WRITE_LOCK = threading.Lock()
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

# Admission control for every bounded registry and every expiring offer. The
# classes are re-exported here because tests and the settings app reach them
# through H.* — the same seam as every other extracted core module.
BoundedRegistry = _core_registry.BoundedRegistry
Offer = _core_registry.Offer

# Phase 4c compatibility facade: core.tools owns the extracted runtime; this
# host supplies the existing globals and callbacks so historical monkeypatch
# seams and public names remain stable.

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

        _error = _read_http_error

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
# layout). handsoff.py keeps the OLD module-level names as thin late-bound
# wrappers — the H.* monkeypatch contract is unchanged. core.settings never
# reaches back into handsoff globals: every path is a parameter.

_SETTINGS_WRITE_LOCK = _core_settings._SETTINGS_WRITE_LOCK


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
    """One doctor line: the look, the design and the window size."""
    look = _core_settings.look_label(_appearance_look()) or "Custom"
    design = str(SETTINGS.get("bubble_design", "orb"))
    try:
        size = int(SETTINGS.get("bubble_size", WINDOW_PX))
    except (TypeError, ValueError):
        size = WINDOW_PX
    return f"look {look} ({design}, {size} px)"


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


_secure_file = _core_settings._secure_file

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
    return all([_secure_file(path) for path in paths])


_atomic_private_write = _core_settings._atomic_private_write

_quarantine_bad = _core_settings._quarantine_bad

def _prepare_runtime() -> bool:
    """Create the runtime roots privately and harden existing state files."""
    for directory in (CONFIG_DIR, STATE_DIR, WHISPER_MODEL_DIR):
        if not _private_dir(directory):
            return False
    return _secure_runtime_files()


coerce_settings = _core_settings.coerce_settings  # shared by bubble + settings app

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
                                + len(json.dumps(build_tools()))) \
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
    # Appearance-tab animation knobs (bubble repaints from these every frame)
    global BUBBLE_ACCENT, ANIM_ENERGY
    BUBBLE_ACCENT = min(1.0, max(0.0, float(SETTINGS.get("bubble_accent", 0.5))))
    ANIM_ENERGY = min(2.0, max(0.2, float(SETTINGS.get("animation_energy", 1.0))))
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
HOLD_MS = 140                  # press-and-hold threshold before recording starts
DRAG_PX = 14                   # movement before a press becomes a drag

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"


def _state_color(key: str, fallback: str) -> QColor:
    c = QColor(SETTINGS["colors"].get(key, fallback))
    return c if c.isValid() else QColor(fallback)


# Dark Siri-style palette: these are the *swirl glow* hues around the dark orb.
STATE_COLORS = {
    IDLE: _state_color("idle", "#2f6fed"),
    LISTENING: _state_color("listening", "#e0435c"),
    THINKING: _state_color("thinking", "#c8781f"),
    SPEAKING: _state_color("speaking", "#1fae62"),
}

# Per-state animation recipe: swirl speed (turns/s), glow boost, hue sweep
# (deg) and target energy driving halo/specular intensity.
_BUBBLE_FX = {
    IDLE: (0.20, 0.10, 25.0, 0.12),
    LISTENING: (0.60, 0.30, 50.0, 0.55),
    SPEAKING: (0.55, 0.32, 90.0, 0.60),
    THINKING: (0.95, 0.40, 140.0, 0.78),
}


def _fx_energy(state: str) -> float:
    """Target glow energy for a state, scaled by the animation-energy setting.

    1.0 (the default) reproduces the historical curve exactly; the slider
    lifts or calms halo/specular intensity for every design at once.
    """
    base = _BUBBLE_FX.get(state, _BUBBLE_FX[IDLE])[3]
    return min(1.0, max(0.0, base * (0.4 + 0.6 * ANIM_ENERGY)))


WINDOW_PX = SETTINGS["bubble_size"]   # transparent window; bubble is ~69% of it
# Appearance-tab live knobs: accent punch (0..1) and global animation energy
# (0.2..2.0).  Defaults are the neutral values, so an old settings.json that
# predates these keys keeps rendering exactly as before.
BUBBLE_ACCENT = float(SETTINGS.get("bubble_accent", 0.5))
ANIM_ENERGY = float(SETTINGS.get("animation_energy", 1.0))
BUBBLE_R0 = WINDOW_PX * 44.0 / 128.0  # idle bubble radius
GLOW_PAD = WINDOW_PX * 7.0 / 128.0    # glow ring thickness; fits inside the mask
GEOM_K = WINDOW_PX / 128.0            # scale for all radius offsets
# The window's mask is the inscribed ellipse of a SQUARE window — which is a
# circle of this radius — so "inside the aperture" is a distance test, and this
# is the budget every design's outermost reach has to fit in. One pixel inside
# the true edge, because an antialiased pixel sitting exactly on the boundary is
# half of it outside. Six designs used to exceed it, measured with the guard in
# tests/test_settings_gui.py: `droplet` put its point and its drip through it
# (391 px, alpha 254 — opaque), `sauron` its fire tips (523 px, alpha 255),
# `saturn` its moon (16 px, alpha 200), and `bloom`, `cube` and `crystal` the
# faint tails of their halos (1 396–2 508 px at alpha 19–20). Each of those
# clamps is pinned by that guard — remove one and it fails.
APERTURE_R = WINDOW_PX / 2.0 - 1.0
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

_resample_to_16k = _audio._resample_to_16k

# PortAudio is process-global.  In particular, stream.stop() must not overlap
# another InputStream construction or a close from a different thread.
# The lock lives in core.audio (single owner); these are aliases so the
# historical H._MIC_OPERATION_* seams keep working.
_MIC_OPERATION_LOCK = _audio._MIC_OPERATION_LOCK
_MIC_OPERATION_STATE_LOCK = _audio._MIC_OPERATION_STATE_LOCK
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
    if not _MIC_OPERATION_LOCK.acquire(timeout=0.6):
        raise RuntimeError("microphone operation still in flight")
    try:
        return _open_input_unlocked(device, rate, blocksize, cb)
    finally:
        _MIC_OPERATION_LOCK.release()


def _stop_stream_owned(stream) -> None:
    """Run stream teardown under the same owner as InputStream construction."""
    if stream is None:
        return
    _MIC_OPERATION_LOCK.acquire()
    try:
        stream.stop()
        stream.close()
    finally:
        _MIC_OPERATION_LOCK.release()


def _start_stream_owned(stream) -> None:
    _MIC_OPERATION_LOCK.acquire()
    try:
        stream.start()
    finally:
        _MIC_OPERATION_LOCK.release()


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
        return _resample_to_16k(audio, getattr(self, "_native_rate", SAMPLE_RATE))


def _stop_recorder_bounded(rec, timeout: float = 3.0):
    """Stop without aborting a live native call from a second thread.

    Thin seam over core.audio: the owner protocol (mic lock, _handsoff_stop_*
    recorder attributes) is implemented once in core.audio so the abort-based
    duplicate cannot diverge again.
    """
    return _audio._stop_recorder_bounded(rec, timeout=timeout)
_whisper_model = None
_whisper_cpu_fallback = False
_TRANSCRIBE_LOCK = _audio._TRANSCRIBE_LOCK
_whisper_lock = _audio._whisper_lock
_CUDA_ERR_RE = _audio._CUDA_ERR_RE
_WHISPER_VRAM_MB = _audio._WHISPER_VRAM_MB
_tts_lock = _audio._tts_lock
_tts_model = None
_nvidia_free_vram_mb = _audio._nvidia_free_vram_mb
_whisper_device_choice = _audio._whisper_device_choice
_is_cuda_error = _audio._is_cuda_error


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


play_wav = _audio.play_wav


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


strip_thinking = _brain.strip_thinking
is_leaked_markup = _brain.is_leaked_markup


def ollama_chat_stream(messages: list[dict], q: "queue.Queue[str | None]",
                       cancel: threading.Event | None = None,
                       tools: list[dict] | None = None) -> dict:
    return _brain.ollama_chat_stream(messages, q, cancel, tools, **_brain_deps())


_read_http_error = _brain._read_http_error


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
    q = urllib.parse.quote(query)
    page = _http_get("https://lite.duckduckgo.com/lite/?q=" + q, timeout=10).decode("utf-8", "replace")
    titles = re.findall(r'class="result-link"[^>]*>(.*?)</a>', page, flags=re.S)
    snips = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', page, flags=re.S)
    clean = lambda s: _html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    return [(clean(t), clean(s)) for t, s in zip(titles, snips)][:5]


def _wiki_search(query: str) -> list[tuple[str, str]]:
    q = urllib.parse.quote(query)
    data = json.loads(_http_get(
        "https://en.wikipedia.org/w/api.php?action=query&list=search"
        "&format=json&srlimit=4&srsearch=" + q))
    out = []
    for hit in (data.get("query") or {}).get("search") or []:
        title = str(hit.get("title", ""))
        snippet = re.sub(r"<[^>]+>", "", str(hit.get("snippet", "")))
        out.append((title, _html_mod.unescape(snippet)))
    return out


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
            _atomic_private_write(WORLD_EVENTS_FILE, json.dumps(seen))
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


def _reminder_store() -> ReminderStore:
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
    return (f"{_DAY_NAMES[due.weekday()]} {due.day:02d} "
            f"{_MONTH_NAMES[due.month - 1]} {due.hour:02d}:{due.minute:02d}"
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
from core.assistant import NotificationReader, PomodoroController, ReminderStore
from core.assistant import dbus_strings as _core_dbus_strings
from core.assistant import notification_muted as _core_notification_muted
from core.assistant import split_due_reminders as _split_due_reminders

# The reminder queue's storage logic (parsing, serialized transactions,
# startup catch-up, due-split arithmetic) lives in core.assistant.ReminderStore;
# this instance binds it to the app's real paths, locks and writers, and the
# thin aliases below keep the historical H.* names the ToolBelt's `_dep()`
# contract and the tests use.
_REMINDER_STORE = ReminderStore(
    REMINDERS_FILE,
    lock=REMINDERS_LOCK,
    file_lock=_core_settings._settings_file_lock(),
    backup=_backup_runtime_json,
    write=_atomic_private_write,
    logger=log,
    clock=time.time,
)
# Calendar parsing lives in core.calendar (stdlib-only, no Qt/Assistant).
# These aliases preserve the historical H.* names used by tests, the
# briefing, and the ToolBelt host-dependency fallback.
from core.calendar import (
    _DAY_NAMES,
    _MONTH_NAMES,
    _fmt_events,
    _ics_add_months,
    _ics_allday,
    _ics_events_from_text,
    _ics_expand,
    _ics_fetch,
    _ics_month_day,
    _ics_month_length,
    _ics_nth_weekday,
    _ics_parse_dt,
    _ics_scheme_error,
    _ics_source_label,
    _ics_unfold,
)
# Re-exported for the H.* test seam and the ToolBelt host fallback.
_CALENDAR_EXPORTS = (
    _DAY_NAMES, _MONTH_NAMES, _fmt_events, _ics_add_months, _ics_allday,
    _ics_events_from_text, _ics_expand, _ics_fetch, _ics_month_day,
    _ics_month_length, _ics_nth_weekday, _ics_parse_dt, _ics_scheme_error,
    _ics_source_label, _ics_unfold,
)


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
            _atomic_private_write(MIC_EVENTS_FILE, json.dumps(doc))
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
            _atomic_private_write(CAP_EVENTS_FILE, json.dumps(doc))
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
            _atomic_private_write(MIC_EVENTS_FILE, json.dumps(doc))
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
            events.extend(_ics_events_from_text(_ics_fetch(src),
                                                win_start, win_end))
        return _fmt_events(events)
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
    return _split_due_reminders(items, now)


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




# Phase 4e compatibility facade: core.lifecycle provides minimal turn primitives.
try:
    from core import lifecycle as _core_lifecycle
except ImportError:
    _core_lifecycle = None

if _core_lifecycle is not None:
    TurnState = _core_lifecycle.TurnState
    next_turn = _core_lifecycle.next_turn
else:
    # Fallback inline definitions for pre-Phase-4e installed bundles.
    import threading
    from dataclasses import dataclass
    from typing import Any

    @dataclass(slots=True)
    class TurnState:
        generation: int
        cancel: threading.Event
        done: threading.Event
        result: Any = None

    def next_turn(counter: list[int] | dict) -> TurnState:
        if isinstance(counter, list):
            counter[0] += 1
            gen = counter[0]
        else:
            counter["gen"] = counter.get("gen", 0) + 1
            gen = counter["gen"]
        return TurnState(
            generation=gen,
            cancel=threading.Event(),
            done=threading.Event(),
            result=None,
        )

# Phase 4c compatibility facade.  The extracted module receives a late-bound
# host proxy so existing module globals and monkeypatch seams stay live.
from types import SimpleNamespace as _SimpleNamespace
try:
    from core import tools as _core_tools
except ImportError:
    # Compatibility with pre-Phase-4c installed bundles that do not yet carry
    # core/tools.py.  A fresh install always ships the extracted module.
    _core_tools = None


class _ToolDependencies:
    def __getattr__(self, name):
        if name == "DECISIONS_FILE":
            return DECISIONS_FILE
        if name == "CONFIG_DIR":
            return CONFIG_DIR
        if name == "subprocess":
            return subprocess
        try:
            return globals()[name]
        except KeyError as e:
            raise AttributeError(name) from e


_tool_dependencies = _ToolDependencies()
if _core_tools is not None:
    _core_tools.time = time
    _core_tools.json = json
    _core_tools.shutil = shutil
    _core_tools.os = os
    _core_tools.Path = Path
    _core_tools.log = log
    _core_tools._DEFAULT_DEPS = _tool_dependencies
    _core_tools._CURRENT.set(_tool_dependencies)
    class ToolBelt(_core_tools.ToolBelt):
        """Compatibility constructor that injects live host dependencies."""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("dependencies", _tool_dependencies)
            super().__init__(*args, **kwargs)

    _core_tools.ToolBelt = ToolBelt
    DecisionPolicy = _core_tools.DecisionPolicy
    BoundedJob = _core_tools.BoundedJob
    tool = _core_tools.tool
    _param_schema = _core_tools._param_schema
    build_tools = _core_tools.build_tools
    log_decision = _core_tools.log_decision
else:
    class DecisionPolicy:
        def __init__(self, settings=None): self._settings = settings or SETTINGS
        def classify(self, tool):
            value = (self._settings.get("command_policy") or {}).get(tool, "ALLOW")
            return value.strip().upper() if isinstance(value, str) and value.strip().upper() in {"ALLOW", "DENY", "CONFIRM"} else "ALLOW"
        def is_denied(self, tool): return self.classify(tool) == "DENY"
        def request_confirm(self, tool): return self.classify(tool) == "CONFIRM"
        def confirm_seconds(self): return 90.0
        @staticmethod
        def is_desktop_action(tool): return False

    class BoundedJob: pass
    def tool(func=None, **kwargs):
        def wrap(fn): return fn
        return wrap(func) if func else wrap
    def _param_schema(func): return {}
    def build_tools(): return []
    def log_decision(*args, **kwargs): return None
    class ToolBelt:
        _last_images = []
        _last_confirmation_offer = False
        def __init__(self, *args, **kwargs): pass
        def execute(self, name, args): return f"ERROR: tool runtime unavailable in this old install; reinstall handsoff", True
        def _set_user_turn(self, marker): pass
        def _tool_methods(self): return {}
        @staticmethod
        def _niri_msg(*args, **kwargs): return subprocess.run(["niri", *args], capture_output=True, text=True, timeout=kwargs.get("timeout", 8))
        @classmethod
        def _ydotool_socket(cls): return "/tmp/.ydotool_socket"
        @staticmethod
        def _socket_connectable(path): return False
    TOOLS = build_tools()

if _core_tools is not None:
    TOOLS = build_tools()


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
                audio = _resample_to_16k(
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
                    feed_data = _resample_to_16k(feed_data, _rate)
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


_TurnStream = _brain.TurnStream


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
        self._notifications = NotificationReader(
            spawn=self._start_worker, is_closed=self._is_closed,
            announce=self._announce_now, muted=self._notification_muted,
            popen_factory=lambda *a, **k: subprocess.Popen(*a, **k),
            persist=set_setting)
        self._pomodoro = PomodoroController(
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
            "size": SETTINGS.get("bubble_size", WINDOW_PX),
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
        return _core_dbus_strings(line)

    @staticmethod
    def _notification_muted(app: str, summary: str, body: str) -> bool:
        """New mute contract: user list matches app (+summary) with word-ish
        semantics (app substring to keep 'Noisy'→'NoisyApp', summary whole
        word, never body); self-mute when app==handsoff or 'handsoff' in
        summary/body."""
        return _core_notification_muted(
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
        global WINDOW_PX, BUBBLE_R0, GLOW_PAD, GEOM_K
        try:
            WINDOW_PX = min(192, max(96, int(SETTINGS.get("bubble_size", WINDOW_PX))))
        except (TypeError, ValueError):
            pass
        BUBBLE_R0 = WINDOW_PX * 44.0 / 128.0
        GLOW_PAD = WINDOW_PX * 7.0 / 128.0
        GEOM_K = WINDOW_PX / 128.0
        for _key, _fb in (("idle", "#2f6fed"), ("listening", "#e0435c"),
                          ("thinking", "#c8781f"), ("speaking", "#1fae62")):
            STATE_COLORS[_key] = _state_color(_key, _fb)
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
                bw.setFixedSize(WINDOW_PX, WINDOW_PX)
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
        now = (f"{_DAY_NAMES[_now.weekday()]}, {_now.day:02d} "
               f"{_MONTH_NAMES[_now.month - 1]} {_now.year}, "
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
                turn = _TurnStream(gen, cancel, queue.Queue())

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
                content = strip_thinking(msg.get("content") or "")
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
                            play_wav(wav, cancel)
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
                        play_wav(wav, cancel)
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
            _atomic_private_write(PENDING_FILE, json.dumps({"note": note}))
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
            _quarantine_bad(HISTORY_FILE)
            return []
        except OSError:
            return []
        except Exception:
            return []
        if not isinstance(data, list):
            _quarantine_bad(HISTORY_FILE)
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
            _atomic_private_write(
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
        _quarantine_bad(MEMORY_FILE)
        return []
    except OSError:
        return []
    except Exception:
        return []
    if not isinstance(data, list):
        _quarantine_bad(MEMORY_FILE)
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
        _atomic_private_write(
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

# The cat's limbs, as fractions of the animated radius. One definition, used by
# the painter and by the mask builder: a mask derived separately from the
# drawing is how a design ends up silently cut off.
# The horizontal budget is the tight one: the radius grows ~28% at the
# listening peak, so a limb may only reach ~1.14 r sideways or ~1.14 r up
# before the WINDOW itself clips it. The corners, where both coordinates stay
# under the edge, allow ~1.6 r — which is why the ears and the tail's tip live
# on the diagonal, and why the rest of the tail curls up the right side rather
# than sweeping out sideways.
CAT_EAR_BASE_IN = (0.16, 0.50)     # (x, -y)  inner base, on the head
CAT_EAR_BASE_OUT = (0.62, 0.30)    # (x, -y)  outer base, on the head
CAT_EAR_REST = (0.84, 0.92)        # (x, -y)  apex at rest
CAT_EAR_VOICE = (0.14, 0.12)       # extra outward reach at full voice
CAT_TAIL_WIDTH = 0.20              # stroke width, fractions of the radius
CAT_TAIL_START = (0.62, 0.28)      # (x, y)   where the tail leaves the body
CAT_TAIL_CTRL = ((0.94, 0.16), (0.92, -0.36))   # the two control points
CAT_TAIL_END = (0.86, -0.74)       # the tip at rest
CAT_TAIL_LIFT = 0.18               # how far the voice lifts the tail's tip


def _cat_reach(level: float, t: float, anim: float) -> float:
    """The cat's animation phase: 0 at rest, and never above 1.

    Everything the ears and the tail do runs through this ONE bounded scalar.
    That is what lets the window mask be computed on resize instead of per
    frame: the limbs' geometry at any instant is `_cat_ears(..., reach)` for
    some reach in [0, 1], so the mask can cover the whole span once.
    """
    lv = min(1.0, max(0.0, float(level)))
    sway = 0.90 + 0.10 * math.sin(t * 3.1 * max(0.2, float(anim)))
    return min(1.0, max(0.0, (0.22 + 0.78 * lv) * sway))


def _cat_ears(cx: float, cy: float, r: float, reach: float) -> list:
    """The cat's two ears: the painter's shape AND the mask's shape."""
    out = []
    for sign in (-1.0, 1.0):
        base_in = QPointF(cx + sign * r * CAT_EAR_BASE_IN[0],
                          cy - r * CAT_EAR_BASE_IN[1])
        base_out = QPointF(cx + sign * r * CAT_EAR_BASE_OUT[0],
                           cy - r * CAT_EAR_BASE_OUT[1])
        tip = QPointF(cx + sign * r * (CAT_EAR_REST[0] + CAT_EAR_VOICE[0] * reach),
                      cy - r * (CAT_EAR_REST[1] + CAT_EAR_VOICE[1] * reach))
        out.append(QPolygonF([base_in, tip, base_out]))
    return out


def _cat_tail(cx: float, cy: float, r: float, reach: float) -> QPainterPath:
    """The cat's tail: the same single definition the mask reads.

    The voice swings it outward and lifts the tip; `reach` bounds both, so the
    extreme is `_cat_tail(..., 1.0)` plus half the stroke width — which is what
    `_cat_region` grows its region from.
    """
    c1x, c1y = CAT_TAIL_CTRL[0]
    c2x, c2y = CAT_TAIL_CTRL[1]
    sx, sy = CAT_TAIL_START
    path = QPainterPath(QPointF(cx + r * sx, cy + r * sy))
    path.cubicTo(QPointF(cx + r * (c1x + 0.04 * reach), cy + r * (c1y - 0.04 * reach)),
                 QPointF(cx + r * (c2x + 0.08 * reach), cy + r * (c2y - 0.10 * reach)),
                 QPointF(cx + r * (CAT_TAIL_END[0] + 0.06 * reach),
                         cy + r * (CAT_TAIL_END[1] - CAT_TAIL_LIFT * reach)))
    return path


def _cat_tail_width(r: float) -> float:
    return max(1.6, r * CAT_TAIL_WIDTH)


def _cat_region(w: int, h: int) -> QRegion:
    """Everything the cat paints that the inscribed ellipse would cut off.

    Built from the painter's own `_cat_ears`/`_cat_tail`, sampled across the
    whole reach span and grown by a margin, so the mask can only ever keep MORE
    ink than the painter puts down. The radius is the animation's maximum
    (`BUBBLE_R0 + 12 * GEOM_K`, the listening peak): a mask is computed on
    resize, never per frame, so it has to cover the biggest frame, not the one
    on screen when the size changed.
    """
    r = w * 56.0 / 128.0
    cx, cy = w / 2.0, h / 2.0
    region = QRegion()
    # Both strokes are a couple of pixels WIDER than the painter's: the region
    # is rasterised from integer polygons, and a mask built at exactly the
    # painted width shaves the outermost antialiased pixel of a round cap
    # (measured: two lit pixels left outside at the tail's tip, on the peak).
    # A mask may always keep more than the painter lays down; never less.
    tail_stroker = QPainterPathStroker()
    tail_stroker.setWidth(_cat_tail_width(r) + 2.0)
    # The painter OUTLINES an ear as well as filling it, so the mask has to
    # cover the pen as well as the polygon: an outline half a pen-width outside
    # the triangle is real ink, and "the mask is the polygon" leaves a fringe of
    # it outside the aperture (measured: a stray lit pixel at the ear's inner
    # edge, on the listening peak).
    ear_stroker = QPainterPathStroker()
    ear_stroker.setWidth(max(2.0, r * 0.028) + 1.0)
    for reach in (0.0, 0.5, 1.0):
        for poly in _cat_ears(cx, cy, r, reach):
            grown = QPolygonF([
                QPointF(cx + (p.x() - cx) * 1.06, cy + (p.y() - cy) * 1.06)
                for p in poly])
            region = region.united(QRegion(grown.toPolygon()))
            ear_path = QPainterPath()
            ear_path.addPolygon(poly)
            ear_path.closeSubpath()
            region = region.united(QRegion(
                ear_stroker.createStroke(ear_path)
                .toFillPolygon().toPolygon()))
        region = region.united(QRegion(
            tail_stroker.createStroke(_cat_tail(cx, cy, r, reach))
            .toFillPolygon().toPolygon()))
    return region


def design_region(name: str, w: int, h: int) -> QRegion:
    """The window outline for a design: the inscribed ellipse, plus its own.

    Called from `BubbleWidget._apply_mask` on every resize and show, so the
    aperture always matches the CURRENT rect (a QRegion mask does not rescale
    with its widget).
    """
    w, h = int(w), int(h)
    region = QRegion(QRect(0, 0, w, h), QRegion.Ellipse)
    if str(name or "").strip().lower() == "cat":
        # A 2 px slack on the ellipse, for this design only: its halo is the
        # largest round body in the set, and un-grown it measures 66.6 px
        # against a 64 px mask at the listening peak — one antialiased pixel
        # short of the aperture (measured). Keeping a little MORE than the
        # painter puts down is the only safe direction for a mask.
        region = region.united(QRegion(QRect(-2, -2, w + 4, h + 4),
                                       QRegion.Ellipse))
        region = region.united(_cat_region(w, h))
    return region


class BubbleWidget(QWidget):
    """The 144×144 always-on-top bubble window (~96 px visible circle)."""

    def __init__(self, assistant: Assistant) -> None:
        super().__init__()
        self._assistant = assistant
        self._state = IDLE
        self._level_target = 0.0
        self._level_ui = 0.0
        self._pressing = False
        self._dragging = False
        self._manual_drag = False
        self._listening = False
        self._menu_open = False
        self._press_pos = None
        self._drag_last = None
        self._last_tick = 0.0
        _c0 = STATE_COLORS[IDLE]
        self._color_ui = [_c0.redF(), _c0.greenF(), _c0.blueF()]
        self._radius_ui = None
        self._radius_vel = 0.0
        self._energy_ui = _fx_energy(IDLE)
        # Which design the current mask was cut for. A live shape switch
        # changes the window's outline without changing its size, and
        # setFixedSize on an unchanged size emits no resizeEvent — so the
        # aperture has to follow the design too, or a cat's mask would linger
        # over an orb (harmless: it can only keep more; but "the mask matches
        # the design" should be a fact, not a coincidence of sizes).
        self._mask_design: str | None = None

        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint       # honoured on X11; niri floats us instead
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(WINDOW_PX, WINDOW_PX)
        self.setWindowTitle(APP_NAME)
        self.setCursor(Qt.PointingHandCursor)

        self._clock = QElapsedTimer()
        self._clock.start()
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._on_tick)
        self._anim.start()

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(HOLD_MS)
        self._hold.timeout.connect(self._hold_fired)

        assistant.sigState.connect(self.set_state)
        assistant.sigLevel.connect(self.set_level)
        try:
            assistant._bubble_widget = self  # live settings reload resizes us
        except AttributeError:
            pass

    # -- slots -----------------------------------------------------------------

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def set_level(self, level: float) -> None:
        self._level_target = level

    def _on_tick(self) -> None:
        now = self._clock.elapsed() / 1000.0
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now
        # voice level: fast attack, gentle release (frame-rate independent)
        k = 1.0 - math.exp(-dt * (24.0 if self._level_target > self._level_ui else 7.0))
        self._level_ui += (self._level_target - self._level_ui) * k
        # state color crossfade — no hard pops on state change
        tgt = STATE_COLORS.get(self._state, STATE_COLORS[IDLE])
        kc = 1.0 - math.exp(-dt * 5.0)
        cu = self._color_ui
        cu[0] += (tgt.redF() - cu[0]) * kc
        cu[1] += (tgt.greenF() - cu[1]) * kc
        cu[2] += (tgt.blueF() - cu[2]) * kc
        # animation energy follows the state (halo/specular intensity)
        ke = 1.0 - math.exp(-dt * 4.0)
        self._energy_ui += (_fx_energy(self._state) - self._energy_ui) * ke
        # radius spring: critically-damped-ish chase, settles without overshoot
        want = self._radius_target(now)
        if self._radius_ui is None:
            self._radius_ui, self._radius_vel = want, 0.0
        else:
            acc = (want - self._radius_ui) * 110.0 - self._radius_vel * 15.0
            self._radius_vel += acc * dt
            self._radius_ui += self._radius_vel * dt
        self.update()

    def _radius_target(self, t: float) -> float:
        if self._state == LISTENING:
            return BUBBLE_R0 + (3 + 9 * self._level_ui) * GEOM_K
        if self._state == SPEAKING:
            return BUBBLE_R0 + 7 * GEOM_K * (0.5 - 0.5 * math.cos(2 * math.pi * t / 0.6))
        if self._state == THINKING:
            return BUBBLE_R0
        return BUBBLE_R0 + 3.5 * GEOM_K * math.sin(2 * math.pi * t / 3.8)

    # -- painting ----------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        f = self._frame()
        design = str(SETTINGS.get("bubble_design", "orb")).strip().lower()
        if design != self._mask_design:
            # the shape changed under us (settings reload / self-edit): the
            # aperture follows the DESIGN, and a size that did not change will
            # never deliver the resizeEvent that used to be the only trigger
            self._apply_mask()
        if design == "halo":
            self._paint_halo(p, f)
        elif design == "reactor":
            self._paint_reactor(p, f)
        elif design == "bloom":
            self._paint_bloom(p, f)
        elif design == "droplet":
            self._paint_droplet(p, f)
        elif design == "cube":
            self._paint_cube(p, f)
        elif design == "equalizer":
            self._paint_equalizer(p, f)
        elif design == "crystal":
            self._paint_crystal(p, f)
        elif design == "saturn":
            self._paint_saturn(p, f)
        elif design == "void":
            self._paint_void(p, f)
        elif design == "sauron":
            self._paint_sauron(p, f)
        elif design == "pikachu":
            self._paint_pikachu(p, f)
        elif design == "cat":
            self._paint_cat(p, f)
        else:
            self._paint_orb(p, f)
        p.end()

    def _frame(self) -> dict:
        """Shared per-frame animation state for every bubble design.

        `animation_energy` scales every motion term (orbit trip, swirl speed,
        hue sweep) and `bubble_accent` scales how hard the state colour punches
        through (saturation and glow alpha), so both Appearance sliders move
        every design at once instead of needing hand-tuned variants.

        The voice is deliberately NOT part of this dict's `energy` any more.
        Folding `level` into `energy` was the quick way to make every design
        react (it replaced six painters reading `level` and an equalizer that
        faked its reactivity with a local `sin(t)` pulse), but it made them all
        react the SAME way — every shape brightened by the same gain, so the
        designs lost their identities exactly when the bubble is most alive.
        `level` is published here and each painter now owns its own reaction:
        the orb ripples, the cube flashes its facets, the crystal refracts, the
        halo sends a wave round its torus, and so on. Every one of those terms
        is written to be neutral at level 0 (multiplied by 1.0, or added as 0),
        so a silent bubble renders exactly as it did before.
        """
        cx, cy = self.width() / 2, self.height() / 2
        t = self._clock.elapsed() / 1000.0
        color = QColor.fromRgbF(*self._color_ui)
        swirl_speed, swirl_boost, hue_speed, _fx_e = _BUBBLE_FX.get(self._state, _BUBBLE_FX[IDLE])
        k_anim, accent = ANIM_ENERGY, BUBBLE_ACCENT
        radius = self._radius_ui if self._radius_ui is not None else self._radius_target(t)
        energy = min(1.0, max(0.0, self._energy_ui))
        # one shared voice signal for every design: 0.0 in silence, 1.0 loud.
        # Painters read it from here and each does its own thing with it.
        lv = min(1.0, max(0.0, float(self._level_ui)))
        # orbiting key light: one full 360° trip per orbit period
        orbit_hz = {"idle": 0.06, "listening": 0.28, "thinking": 0.42, "speaking": 0.33}.get(self._state, 0.06)
        la = 3 * math.pi / 4 + t * 2 * math.pi * orbit_hz * k_anim
        lx, ly = math.cos(la), -math.sin(la)  # screen pos: y grows downward
        # Both Appearance sliders are folded into the shared frame state HERE,
        # once, because only two painters ever called _conic() — the rest draw
        # with f["color"] / f["energy"], so an accent applied inside _conic()
        # alone measured as literally zero changed pixels on reactor, droplet,
        # void and <0.1% on five more. Every painter reads
        # this dict, so applying it here is what makes the sliders reach all
        # every design instead of just the orb.
        #
        # 0.5 is neutral in BOTH directions (gain 1.0), so the default settings
        # render exactly as before and the slider is honest on the way down as
        # well: the old `1.0 + 0.35 * accent` was 1.175 at the default and could
        # never reduce saturation, so the lower half of the slider did nothing.
        a = 2.0 * (accent - 0.5)                  # -1 .. +1, 0 at the default
        sat = min(1.0, max(0.0, color.hslSaturationF() * (1.0 + 0.35 * a)))
        glow = 1.0 + 0.45 * a
        hue = color.hueF()
        punched = QColor.fromHslF(hue if hue >= 0.0 else 0.0, sat,
                                  color.lightnessF(), 1.0)
        return {
            "cx": cx, "cy": cy, "t": t, "color": punched, "radius": radius,
            # animation_energy scales the glow every design already paints from,
            # not just the motion terms most of them ignore; neutral at 1.0.
            # The voice is NOT folded in: see the docstring. (There used to be an
            # `energy0` alongside it — the same value without the voice lift,
            # for the one painter whose geometry the lift corrupted. With the
            # lift gone the two are identical, so `energy0` is retired rather
            # than kept around as a second name for the same number.)
            "energy": energy * (0.6 + 0.4 * k_anim),
            "level": lv, "la": la, "lx": lx, "ly": ly,
            "inner": radius * (1.0 - 0.34 - 0.10 * swirl_boost),
            "base_hue": max(0.0, hue),
            "sat": sat, "glow": glow,
            "swirl_speed": swirl_speed * k_anim, "swirl_boost": swirl_boost,
            "hue_speed": hue_speed * k_anim, "accent": accent, "anim": k_anim,
        }

    def _conic(self, f: dict, angle_deg: float, alpha: int, comet: bool = False) -> QConicalGradient:
        g = QConicalGradient(f["cx"], f["cy"], angle_deg)
        # bubble_accent drives the glow's punch; the factor is computed once in
        # _frame() so "neutral at 0.5" lives in one place and the painters that
        # avoid _conic() still respond.
        alpha = int(max(0, min(255, alpha * float(f.get("glow", 1.0)))))
        if comet:
            # asymmetric comet head + fading tail: rotation is unmistakable.
            # animation energy brightens head and tail, not just their speed.
            lift = 1.0 + 0.10 * (float(f.get("anim", 1.0)) - 1.0)
            stops = tuple((pos, min(1.0, light * lift), a) for pos, light, a in (
                (0.00, 0.78, 1.00), (0.12, 0.66, 0.90), (0.30, 0.58, 0.60),
                (0.55, 0.52, 0.36), (0.80, 0.50, 0.28), (1.00, 0.78, 1.00)))
        else:
            stops = tuple((i / 5.0, 0.60 + 0.10 * f["swirl_boost"], alpha / 255.0) for i in range(6))
        first = None
        for pos, light, a in stops:
            c = QColor.fromHslF(
                (f["base_hue"] + (0.5 - abs(0.5 - pos)) * f["hue_speed"] / 360.0) % 1.0,
                f["sat"], light, a if comet else alpha / 255.0,
            )
            if first is None:
                first = QColor(c)
            g.setColorAt(pos, c)
        g.setColorAt(1.0, first)
        return g

    def _paint_orb(self, p: QPainter, f: dict) -> None:
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy = f["radius"], f["energy"]
        la, lx, ly, inner = f["la"], f["lx"], f["ly"], f["inner"]
        swirl_speed = f["swirl_speed"]

        # The voice ripples the whole silhouette: the glass visibly shivers with
        # the room. Levels are neutral at 0, so a silent bubble stays round.
        lv = min(1.0, max(0.0, float(f["level"])))
        # organic silhouette: thinking wobbles, speaking ripples, rest stay round
        wob_amt = 1.0 if self._state == THINKING else (0.45 if self._state == SPEAKING else 0.0)
        wob_amt = max(wob_amt, 0.45 * lv)
        outer_path = QPainterPath()
        if wob_amt > 0.0:
            outer_path = self._wobble_path(cx, cy, radius, t, wob_amt)
        else:
            outer_path.addEllipse(QPointF(cx, cy), radius, radius)
        p.setPen(Qt.NoPen)

        # faint state-colored halo so the dark orb reads on dark wallpapers
        halo = QRadialGradient(QPointF(cx, cy), radius + GLOW_PAD)
        halo_color = QColor(color)
        halo_color.setAlpha(int(30 + 45 * energy))
        halo.setColorAt(radius / (radius + GLOW_PAD), halo_color)
        halo_color.setAlpha(0)
        halo.setColorAt(1.0, halo_color)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), radius + GLOW_PAD, radius + GLOW_PAD)

        # --- Siri-style rotating swirl, two counter-rotating layers ---
        inner_path = QPainterPath()
        inner_path.addEllipse(QPointF(cx, cy), inner, inner)
        ring_path = outer_path.subtracted(inner_path)
        p.save()
        p.setClipPath(ring_path)
        p.setBrush(QBrush(self._conic(f, -t * 360.0 * swirl_speed, 255, comet=True)))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        thin = QPainterPath()
        thin.addEllipse(QPointF(cx, cy), radius, radius)
        thin_inner = QPainterPath()
        thin_inner.addEllipse(QPointF(cx, cy), inner * 1.12, inner * 1.12)
        p.setClipPath(thin.subtracted(thin_inner))
        p.setBrush(QBrush(self._conic(f, t * 360.0 * swirl_speed * 0.6 + 40.0, int(60 + 90 * energy))))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        # radial falloff: darken toward the core with translucent black over the ring
        shade = QRadialGradient(QPointF(cx, cy), radius)
        shade.setColorAt(inner / radius, QColor(10, 12, 18, 235))
        shade.setColorAt(1.0, QColor(10, 12, 18, 0))
        p.setClipPath(ring_path)
        p.setBrush(QBrush(shade))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        p.restore()

        # --- voice ripples: two rings spreading through the glass, from the
        #     core outward, fading as they go. This is the orb's own reaction —
        #     it used to just brighten like every other design.
        if lv > 0.004:
            p.save()
            p.setClipPath(ring_path)
            p.setBrush(Qt.NoBrush)
            for i in range(2):
                ph = (t * (0.55 + 0.3 * i) + i * 0.5) % 1.0
                rr = inner + (radius - inner) * ph
                c = QColor(color).lighter(150)
                c.setAlpha(int(min(255.0, 190 * lv * (1.0 - ph))))
                p.setPen(QPen(c, max(1.2, radius * 0.04 * (1.0 - 0.4 * ph)),
                              Qt.SolidLine, Qt.RoundCap))
                p.drawEllipse(QPointF(cx, cy), rr, rr)
            p.restore()

        # --- 3D glass core: lit hemisphere facing the key light ---
        tint = QColor(
            int(26 * 0.88 + color.red() * 0.12),
            int(28 * 0.88 + color.green() * 0.12),
            int(36 * 0.88 + color.blue() * 0.12),
        )
        core = QRadialGradient(
            QPointF(cx + lx * inner * 0.55, cy + ly * inner * 0.55), inner * 1.6)
        core.setColorAt(0.0, QColor(150, 158, 180))
        core.setColorAt(0.35, tint)
        core.setColorAt(0.75, QColor(20, 22, 29))
        core.setColorAt(1.0, QColor(10, 11, 15))
        p.setBrush(QBrush(core))
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawEllipse(QPointF(cx, cy), inner, inner)
        # contact depth: soft shadow pooled opposite the light + floor bounce
        depth = QRadialGradient(
            QPointF(cx - lx * inner * 0.45, cy - ly * inner * 0.45), inner * 1.1)
        depth.setColorAt(0.55, QColor(0, 0, 0, 0))
        depth.setColorAt(1.0, QColor(0, 0, 0, int(60 + 50 * energy)))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(depth))
        p.drawEllipse(QPointF(cx, cy), inner, inner)
        bounce = QColor(color)
        bounce.setAlpha(int(14 + 26 * energy))
        p.setBrush(bounce)
        p.drawEllipse(QPointF(cx, cy + inner * 0.52), inner * 0.55, inner * 0.26)

        # --- specular life: breathing hotspot + slow-drifting crescent. The
        #     hotspot catches the voice, the way a lamp glints when you speak.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255,
                          int(min(255.0, 55 + 15 * math.sin(2 * math.pi * t / 1.7) + 90 * lv))))
        p.drawEllipse(
            QPointF(cx + lx * inner * 0.50, cy + ly * inner * 0.50), inner * 0.30, inner * 0.21
        )
        crescent = QPainterPath()
        crescent.addEllipse(QPointF(cx, cy), inner * 0.86, inner * 0.86)
        crescent_inner = QPainterPath()
        crescent_inner.addEllipse(QPointF(cx, cy), inner * 0.78, inner * 0.78)
        p.save()
        p.setClipPath(crescent.subtracted(crescent_inner))
        p.setBrush(QBrush(self._conic(f, -t * 360.0 * 0.05 + 135.0, 70)))
        p.drawEllipse(QPointF(cx, cy), inner, inner)
        p.restore()
        # rim light: dim base ring + bright arc parked under the orbiting light
        rim = QColor(color)
        rim.setAlpha(int(50 + 30 * energy))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rim, max(1.0, inner * 0.05)))
        p.drawEllipse(QPointF(cx, cy), inner * 0.97, inner * 0.97)
        arc = QPainterPath()
        for i in range(21):
            a = la - 0.6 + i * (1.2 / 20)
            x, y = cx + math.cos(a) * inner * 0.97, cy - math.sin(a) * inner * 0.97
            arc.moveTo(x, y) if i == 0 else arc.lineTo(x, y)
        hot = QColor(color)
        hot.setAlpha(int(min(255.0, 140 + 60 * energy + 60 * lv)))
        p.setPen(QPen(hot, max(1.5, inner * 0.075 * (1.0 + 0.5 * lv)),
                      Qt.SolidLine, Qt.RoundCap))
        p.drawPath(arc)

    def _paint_halo(self, p: QPainter, f: dict) -> None:
        """Hollow torus: the wallpaper shows through the middle."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy = f["radius"], f["energy"]
        # The voice swells the torus and drives a brightness wave round it: the
        # ring itself thickens outward as you speak. Neutral at level 0.
        lv = min(1.0, max(0.0, float(f["level"])))
        thick = radius * (0.16 + 0.05 * energy + 0.16 * lv)
        outer, inner_r = radius, radius - thick
        p.setPen(Qt.NoPen)
        halo = QRadialGradient(QPointF(cx, cy), radius + GLOW_PAD)
        halo_color = QColor(color)
        halo_color.setAlpha(int(25 + 40 * energy))
        halo.setColorAt(0.0, QColor(0, 0, 0, 0))
        halo.setColorAt(max(0.0, inner_r / (radius + GLOW_PAD)), QColor(0, 0, 0, 0))
        halo.setColorAt(max(0.0, outer / (radius + GLOW_PAD)), halo_color)
        halo_color.setAlpha(0)
        halo.setColorAt(1.0, halo_color)
        p.drawEllipse(QPointF(cx, cy), radius + GLOW_PAD, radius + GLOW_PAD)
        # glassy disc inside, barely there
        disc = QRadialGradient(QPointF(cx, cy), inner_r)
        disc.setColorAt(0.0, QColor(255, 255, 255, 14))
        disc.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(disc))
        p.drawEllipse(QPointF(cx, cy), inner_r, inner_r)
        # torus with an orbiting comet head
        ring = QPainterPath()
        ring.addEllipse(QPointF(cx, cy), outer, outer)
        hole = QPainterPath()
        hole.addEllipse(QPointF(cx, cy), inner_r, inner_r)
        p.save()
        p.setClipPath(ring.subtracted(hole))
        # the comet wave travels faster the louder the room is
        p.setBrush(QBrush(self._conic(f, -t * 360.0 * f["swirl_speed"] * (1.0 + 1.1 * lv),
                                     230, comet=True)))
        p.drawEllipse(QPointF(cx, cy), outer, outer)
        p.restore()
        # bright bead parked on the ring at a known angle (phase-exact), with a
        # trailing bead behind it that only shows up with the voice
        bead_a = t * 2 * math.pi * f["swirl_speed"] * (1.0 + 1.1 * lv)
        bead_r = (outer + inner_r) / 2
        p.setBrush(QColor(255, 255, 255, int(min(255.0, 200 + 55 * lv))))
        p.drawEllipse(QPointF(cx + math.cos(bead_a) * bead_r, cy - math.sin(bead_a) * bead_r),
                      thick * 0.32 * (1.0 + 0.35 * lv), thick * 0.32 * (1.0 + 0.35 * lv))
        if lv > 0.004:
            tail = QColor(255, 255, 255, int(min(255.0, 150 * lv)))
            p.setBrush(QBrush(tail))
            for k in (0.35, 0.75):
                ta = bead_a - k
                p.drawEllipse(
                    QPointF(cx + math.cos(ta) * bead_r, cy - math.sin(ta) * bead_r),
                    thick * (0.22 - 0.08 * k) * lv, thick * (0.22 - 0.08 * k) * lv)
        # breathing core dot, flaring with the voice
        dot_r = radius * 0.07 * (1.0 + 0.3 * math.sin(2 * math.pi * t / 1.2)) * (1.0 + 0.5 * lv)
        dot = QColor(color)
        dot.setAlpha(int(min(255.0, 150 + 80 * energy + 70 * lv)))
        p.setBrush(QBrush(dot))
        p.drawEllipse(QPointF(cx, cy), dot_r, dot_r)

    def _arc(self, p: QPainter, cx: float, cy: float, r: float, a0: float, span: float,
             color: QColor, width: float) -> None:
        """Bright polyline arc from a0-span/2 to a0+span/2 (math angles)."""
        path = QPainterPath()
        for i in range(25):
            a = a0 - span / 2 + i * (span / 24)
            x, y = cx + math.cos(a) * r, cy - math.sin(a) * r
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(path)

    def _paint_reactor(self, p: QPainter, f: dict) -> None:
        """Segmented tech ring: three arcs, tick marks, pulsing core."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy = f["radius"], f["energy"]

        k = GEOM_K
        p.setPen(Qt.NoPen)
        halo = QRadialGradient(QPointF(cx, cy), radius + GLOW_PAD)
        halo_c = QColor(color)
        halo_c.setAlpha(int(20 + 30 * energy))
        halo.setColorAt(0.0, QColor(0, 0, 0, 0))
        halo.setColorAt(max(0.0, radius / (radius + GLOW_PAD)), halo_c)
        halo_c.setAlpha(0)
        halo.setColorAt(1.0, halo_c)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), radius + GLOW_PAD, radius + GLOW_PAD)
        # The voice is the reactor's throttle: the segments open wider, the tick
        # ring pushes outward and the whole thing spins up. Neutral at level 0.
        lv = min(1.0, max(0.0, float(f["level"])))
        disc = QColor(color)
        disc.setAlpha(int(16 + 34 * lv))
        p.setBrush(QBrush(disc))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        react = 1.0 + lv * 1.5  # voice-reactive spin
        segs = ((0.98, 0.50, 1.75, 3.2), (0.86, -0.35, 1.22, 2.4), (0.74, 0.80, 2.44, 1.8))
        for rr, spd, span, wdt in segs:
            c = QColor(color)
            c.setAlpha(int(min(255.0, 120 + 90 * energy + 60 * lv)))
            self._arc(p, cx, cy, radius * rr, t * 2 * math.pi * spd * react,
                      span * (1.0 + 0.45 * lv), c, wdt * k * (1.0 + 0.25 * lv))
        # tick ring, slow drift, extending outward with the voice
        for i in range(12):
            a = i * math.pi / 6 + t * 0.15
            long_tick = i % 3 == 0
            r1 = radius * 0.62
            r2 = radius * ((0.68 if long_tick else 0.65) + 0.07 * lv)
            c = QColor(color)
            c.setAlpha(int(min(255.0, (130 if long_tick else 70) + 60 * lv)))
            p.setPen(QPen(c, (2.0 if long_tick else 1.2) * k))
            p.drawLine(QPointF(cx + math.cos(a) * r1, cy - math.sin(a) * r1),
                       QPointF(cx + math.cos(a) * r2, cy - math.sin(a) * r2))
        core_r = radius * 0.10 * (1.0 + 0.35 * energy + 0.5 * lv)
        core_c = QColor(color)
        core_c.setAlpha(230)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(core_c))
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)
        p.setBrush(QColor(255, 255, 255, 160))
        p.drawEllipse(QPointF(cx, cy), core_r * 0.4, core_r * 0.4)

    def _paint_bloom(self, p: QPainter, f: dict) -> None:
        """Edgeless glow blob: soft radial bloom with drifting sparks."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy = f["radius"], f["energy"]
        # The voice makes the bloom breathe: the petals (the blob's own wobble)
        # open wider, the mist thickens, and more sparks come out. The outer mist
        # radius is deliberately NOT grown — it already reaches the window's
        # round mask, so growing it would just clip. Neutral at level 0.
        lv = min(1.0, max(0.0, float(f["level"])))
        p.setPen(Qt.NoPen)
        # The mist reaches 1.35 R, which is past the glass at every size above
        # rest: measured 2 508 pixels outside the rim before this clamp, the
        # most opaque of them at alpha 19. Faint — but a gradient that is meant
        # to fade to nothing ended on the cut instead, because its outer stops
        # are reached INSIDE the disc it fills: "alpha 0 at the edge" was never
        # true of the pixels between. Clamped, the fade completes inside the
        # glass.
        mist_r = min(radius * 1.35, APERTURE_R)
        mist = QRadialGradient(QPointF(cx, cy), mist_r)
        mist_c = QColor(color)
        mist_c.setAlpha(int(min(255.0, 70 + 60 * energy + 80 * lv)))
        mist.setColorAt(0.0, mist_c)
        mist_c.setAlpha(0)
        # The brighter the voice makes the mist, the further in its tail is
        # pulled, so the extra alpha dies before the window's round mask instead
        # of being cut off on it. At level 0 the stop lands exactly on 1.0, so
        # the resting glow is untouched.
        mist.setColorAt(1.0 - 0.20 * lv, mist_c)
        mist.setColorAt(1.0, mist_c)
        p.setBrush(QBrush(mist))
        p.drawEllipse(QPointF(cx, cy), mist_r, mist_r)
        blob = self._wobble_path(cx, cy, radius * 0.72, t * 0.7, 0.6 + 0.6 * lv)
        body = QRadialGradient(QPointF(cx, cy - radius * 0.2), radius)
        top = QColor(color).lighter(150)
        top.setAlpha(int(150 + 60 * energy))
        body.setColorAt(0.0, top)
        mid = QColor(color)
        mid.setAlpha(110)
        body.setColorAt(0.55, mid)
        low = QColor(color).darker(170)
        low.setAlpha(0)
        body.setColorAt(1.0, low)
        p.setBrush(QBrush(body))
        p.drawPath(blob)
        # bright breathing heart
        heart_r = radius * 0.20 * (1.0 + 0.18 * math.sin(2 * math.pi * t / 1.4)) * (1.0 + 0.5 * lv)
        heart = QColor(color).lighter(170)
        heart.setAlpha(220)
        p.setBrush(QBrush(heart))
        p.drawEllipse(QPointF(cx, cy), heart_r, heart_r)
        # drifting sparks on golden-angle orbits: louder voice, faster swirl,
        # brighter specks, and extra ones shaken loose from the core. The orbit
        # radii stay under the mask (worst case 1.08 R).
        for i in range(5 + int(3 * lv)):
            a = t * (0.3 + 0.07 * i + 0.9 * lv) + i * 2.39996
            # capped at 0.95 R: the first five are unchanged (0.81 R max), and
            # the extra ones the voice shakes loose stay clear of the round mask
            # instead of sitting right on the cut
            r = radius * min(0.95, 0.45 + 0.09 * i)
            s = QColor(255, 255, 255, int(min(255.0, 70 + 150 * lv)))
            p.setBrush(QBrush(s))
            rr = 1.6 * (1.0 + 0.6 * lv)
            p.drawEllipse(QPointF(cx + math.cos(a) * r, cy - math.sin(a) * r), rr, rr)

    def _paint_droplet(self, p: QPainter, f: dict) -> None:
        """Teardrop that stretches with voice level and drips while listening."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, level = f["radius"], f["energy"], f["level"]
        R = radius * 0.92
        # The voice is surface tension here: a fast, small-amplitude ripple runs
        # round the skin (on top of the slow idle swell) and the drip detaches
        # sooner. Both are neutral at level 0 — the ripple term vanishes exactly.
        lv = min(1.0, max(0.0, float(level)))
        stretch = 1.0 + 0.28 * level + (0.08 if self._state == LISTENING else 0.0)
        # The shape is built first and then FITTED to the glass with one scale
        # factor, because a teardrop drawn to its natural reach puts its point
        # through the top of the aperture and its drip through the bottom —
        # measured: opaque ink outside the rim on every frame above silence,
        # 391 px of it at alpha 254 on the worst one. At rest it already fits
        # (mostly), so the resting droplet barely moves; as the voice stretches
        # it, the drop stops at the rim instead of being cut by it.
        skin = []
        for i in range(73):
            ang = i * 2 * math.pi / 72
            tip = math.exp(-((ang - math.pi / 2) / 0.55) ** 2)
            r = R * (1.0 + 0.45 * tip + 0.04 * math.sin(3 * ang + 3.0 * t)
                     + 0.03 * lv * math.sin(7 * ang - 5.0 * t))
            skin.append((r * math.cos(ang) * 0.92, -r * math.sin(ang) * stretch))
        drip_ph = ((t * (0.7 + 1.6 * lv)) % 1.0
                   if self._state == LISTENING else None)
        drip_dy = drip_rx = drip_ry = 0.0
        if drip_ph is not None:
            drip_rx = R * 0.10 * (1.0 - drip_ph * 0.5) * (1.0 + 0.4 * lv)
            drip_ry = R * 0.13 * (1.0 - drip_ph * 0.5) * (1.0 + 0.4 * lv)
            drip_dy = R * stretch + drip_ph * R * 0.9 + drip_ry
        rim_w = max(1.2, R * 0.045 * (1.0 + 0.35 * lv))
        # The drip is deliberately NOT part of this fit. Folding it in was tried
        # and REVERTED: it shrank the whole droplet (measured fit 0.51 instead
        # of 0.62 at the phase where the drip is furthest along) to make room
        # for a drip that is already outside the window by then — its drawn
        # centre reaches y=136.9 in a 128 px window while it still has alpha,
        # and on the vertical axis "outside the mask" IS "outside the window",
        # so Qt has clipped it before the mask could. A fine 5 ms sweep of the
        # whole cycle at five radii measured ZERO pixels of any alpha outside
        # the rim with the drip excluded.
        reach = max([math.hypot(x, y) for x, y in skin])
        # the rim is a stroke, so half of it is outside the path it outlines
        budget = max(1.0, APERTURE_R - rim_w * 0.5)
        fit = min(1.0, budget / reach) if reach else 1.0
        drop = QPainterPath()
        for i, (x, y) in enumerate(skin):
            x, y = cx + x * fit, cy + y * fit
            drop.moveTo(x, y) if i == 0 else drop.lineTo(x, y)
        drop.closeSubpath()
        p.setPen(Qt.NoPen)
        body = QLinearGradient(cx, cy - R * stretch * fit, cx, cy + R * fit)
        hi = QColor(color).lighter(165)
        hi.setAlpha(235)
        body.setColorAt(0.0, hi)
        mid = QColor(color)
        mid.setAlpha(220)
        body.setColorAt(0.55, mid)
        lo = QColor(color).darker(180)
        lo.setAlpha(235)
        body.setColorAt(1.0, lo)
        p.setBrush(QBrush(body))
        p.drawPath(drop)
        # specular streak down the lit side
        p.setBrush(QColor(255, 255, 255, int(min(255.0, 50 + 20 * energy + 80 * lv))))
        p.drawEllipse(QPointF(cx - R * 0.28 * fit, cy - R * 0.35 * stretch * fit),
                      R * 0.13 * (1.0 + 0.35 * lv) * fit, R * 0.22 * stretch * fit)
        # detaching drip while listening; the voice makes it detach sooner and
        # more often, since the whole drop is agitated
        if drip_ph is not None:
            drip = QColor(color)
            drip.setAlpha(int(min(255.0, (1.0 - drip_ph) * (200 + 55 * lv))))
            p.setBrush(QBrush(drip))
            p.drawEllipse(QPointF(cx, cy + drip_dy * fit),
                          drip_rx * fit, drip_ry * fit)
        rim = QColor(color).lighter(140)
        rim.setAlpha(int(min(255.0, 120 + 80 * energy + 60 * lv)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rim, rim_w))
        p.drawPath(drop)

    def _paint_cube(self, p: QPainter, f: dict) -> None:
        """Tumbling isometric glass cube catching the orbiting light."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, la = f["radius"], f["energy"], f["la"]
        # The voice flashes the facets: a front sweeps around the six faces and
        # lights each one as it passes, so the cube reads as a ring of lamps
        # rather than one uniformly brighter blob. `flash` is exactly 0 at
        # level 0, so a silent cube is pixel-identical to before.
        lv = min(1.0, max(0.0, float(f["level"])))
        front = (t * 1.6) % 1.0 * 6.0
        R = radius * 0.95
        bob = math.sin(2 * math.pi * t / 3.0) * R * 0.04
        cy += bob
        rot = t * 2 * math.pi * 0.08
        verts = [(cx + R * math.cos(rot + i * math.pi / 3),
                  cy - R * math.sin(rot + i * math.pi / 3)) for i in range(6)]
        p.setPen(Qt.NoPen)
        # The halo is 1.3 R, so at the listening peak its tail crossed the rim
        # (measured 1 396 pixels outside it, most opaque alpha 19) — faint, but
        # it is ink on the cut, and the outer stops land INSIDE the disc, so
        # the intended fade to zero never reaches the edge itself
        halo_r = min(R * 1.3, APERTURE_R)
        halo = QRadialGradient(QPointF(cx, cy), halo_r)
        hc = QColor(color)
        hc.setAlpha(int(min(255.0, 25 + 35 * energy + 60 * lv)))
        halo.setColorAt(0.7, hc)
        hc.setAlpha(0)
        halo.setColorAt(0.0, hc)
        # tail pulled in by the voice, so the brighter halo fades out before the
        # window's round mask rather than ending on it; 1.0 at level 0
        halo.setColorAt(1.0 - 0.20 * lv, hc)
        halo.setColorAt(1.0, hc)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)
        for i in range(6):
            x1, y1 = verts[i]
            ang = rot + (i + 0.5) * math.pi / 3
            facing = 0.55 + 0.45 * math.cos(ang - la)
            tri = QPainterPath()
            tri.moveTo(cx, cy)
            tri.lineTo(x1, y1)
            tri.lineTo(*verts[(i + 1) % 6])
            tri.closeSubpath()
            # shortest way round the ring of six faces, for the flash front
            d = min(abs(i - front), 6.0 - abs(i - front))
            flash = max(0.0, 1.0 - d * 1.6) * lv
            fill = QColor(color).darker(max(40, int(170 - 90 * facing - 70 * flash)))
            fill.setAlpha(int(min(255.0, 150 + 60 * facing + 90 * flash)))
            p.setBrush(QBrush(fill))
            p.drawPath(tri)
        edge = QColor(color).lighter(150)
        edge.setAlpha(int(min(255.0, 140 + 80 * energy + 70 * lv)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(edge, max(1.2, R * 0.04)))
        hex_path = QPainterPath()
        for i, (vx, vy) in enumerate(verts):
            hex_path.moveTo(vx, vy) if i == 0 else hex_path.lineTo(vx, vy)
        hex_path.closeSubpath()
        p.drawPath(hex_path)
        p.setPen(QPen(QColor(255, 255, 255, int(90 + 120 * lv)), 1.0))
        for vx, vy in verts:
            p.drawLine(QPointF(cx, cy), QPointF(vx, vy))

    def _paint_equalizer(self, p: QPainter, f: dict) -> None:
        """Ring of 28 audio bars driven by mic level; flatline at idle."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, level = f["radius"], f["energy"], f["level"]
        anim = float(f.get("anim", 1.0))
        n = 28
        r_in = radius * 0.42
        p.setBrush(Qt.NoBrush)
        for i in range(n):
            a = i * 2 * math.pi / n
            if self._state == IDLE:
                # the idle bars must scale with animation_energy too: they were
                # the one path that ignored it, so the slider did nothing on
                # this design at idle — the state it sits in most of the time.
                # `anim` is 1.0 at the default, so the look is unchanged there.
                length = radius * 0.06 * (1.0 + 0.5 * math.sin(t * 2.2 + i * 0.7)) * anim
            else:
                # Amplitude comes from the shared `level` signal, NOT a local
                # sin(t) pulse: these bars are a meter, so they must show the
                # voice rather than a timer that merely correlated with it.
                # The per-bar factor is fixed geometry, so the ring keeps its
                # shape but has no invented rhythm.
                shape = 0.45 + 0.55 * (0.5 + 0.5 * math.cos(i * 2.39996))
                length = radius * (0.05 + 0.55 * level * shape
                                   + 0.06 * energy * (0.5 + 0.5 * math.cos(i * 2.0)))
            x1, y1 = cx + math.cos(a) * r_in, cy - math.sin(a) * r_in
            x2, y2 = cx + math.cos(a) * (r_in + length), cy - math.sin(a) * (r_in + length)
            c = QColor(color)
            c.setAlpha(int(90 + 130 * min(1.0, length / (radius * 0.5))))
            p.setPen(QPen(c, max(1.5, 2 * math.pi * radius / n * 0.32), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        dot_r = radius * 0.10 * (1.0 + 0.4 * level)
        dot = QColor(color)
        dot.setAlpha(220)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(dot))
        p.drawEllipse(QPointF(cx, cy), dot_r, dot_r)
        p.setBrush(QColor(255, 255, 255, 170))
        p.drawEllipse(QPointF(cx, cy), dot_r * 0.4, dot_r * 0.4)

    def _paint_crystal(self, p: QPainter, f: dict) -> None:
        """Rotating faceted gem with glinting edges."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, la = f["radius"], f["energy"], f["la"]
        # The voice refracts this one: the inner hex swells and its hue splits
        # away from the body colour, so the gem looks like it is bending light
        # rather than merely brightening. Every term is neutral at level 0.
        lv = min(1.0, max(0.0, float(f["level"])))
        R = radius * 0.95
        rot = t * 2 * math.pi * 0.06
        p.setPen(Qt.NoPen)
        # same clamp as the cube: 1.3 R crossed the rim at the listening peak
        # (measured 1 396 pixels past it, most opaque alpha 20)
        halo_r = min(R * 1.3, APERTURE_R)
        halo = QRadialGradient(QPointF(cx, cy), halo_r)
        hc = QColor(color)
        hc.setAlpha(int(min(255.0, 30 + 40 * energy + 50 * lv)))
        halo.setColorAt(0.75, hc)
        hc.setAlpha(0)
        halo.setColorAt(0.0, hc)
        # tail pulled in by the voice, so the brighter halo fades out before the
        # window's round mask rather than ending on it; 1.0 at level 0
        halo.setColorAt(1.0 - 0.20 * lv, hc)
        halo.setColorAt(1.0, hc)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)
        gem = QColor(color)
        gem.setAlpha(int(70 + 45 * lv))
        p.setBrush(QBrush(gem))
        gem_path = QPainterPath()
        verts = []
        for i in range(6):
            vx, vy = cx + R * math.cos(rot + i * math.pi / 3), cy - R * math.sin(rot + i * math.pi / 3)
            verts.append((vx, vy))
            gem_path.moveTo(vx, vy) if i == 0 else gem_path.lineTo(vx, vy)
        gem_path.closeSubpath()
        p.drawPath(gem_path)
        # facet spokes + counter-rotating inner hex, which the voice pushes
        # outward and tints away from the body colour (the refraction):
        p.setPen(QPen(QColor(255, 255, 255, int(min(255.0, 70 + 120 * lv))), 1.0))
        for vx, vy in verts:
            p.drawLine(QPointF(cx, cy), QPointF(vx, vy))
        in_rot = -rot * 1.5 - 1.1 * lv
        in_path = QPainterPath()
        for i in range(6):
            vx = cx + R * (0.55 + 0.22 * lv) * math.cos(in_rot + i * math.pi / 3)
            vy = cy - R * (0.55 + 0.22 * lv) * math.sin(in_rot + i * math.pi / 3)
            in_path.moveTo(vx, vy) if i == 0 else in_path.lineTo(vx, vy)
        in_path.closeSubpath()
        split = QColor.fromHslF((f["base_hue"] + 0.32 * lv) % 1.0,
                                min(1.0, float(f["sat"]) + 0.25 * lv), 0.72)
        # blended FROM white, so level 0 is exactly the white line this used to
        # be: writing `split` directly made the resting gem take the state hue
        refract = QColor(int(255 + (split.red() - 255) * lv),
                         int(255 + (split.green() - 255) * lv),
                         int(255 + (split.blue() - 255) * lv))
        refract.setAlpha(int(min(255.0, 90 + 140 * lv)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(refract, 1.2 + 1.2 * lv))
        p.drawPath(in_path)
        # glints on the two edges nearest the light, lengthening with the voice
        best = sorted(range(6), key=lambda i: abs(((rot + i * math.pi / 3) - la + math.pi) % (2 * math.pi) - math.pi))[:2]
        for i in best:
            self._arc(p, cx, cy, R * 0.99, rot + i * math.pi / 3 + math.pi / 6,
                      0.5 * (1.0 + 0.6 * lv),
                      QColor(255, 255, 255, int(min(255.0, 150 + 70 * energy + 80 * lv))),
                      max(1.5, R * 0.05 * (1.0 + 0.6 * lv)))
        edge = QColor(color).lighter(150)
        edge.setAlpha(int(min(255.0, 150 + 70 * energy + 60 * lv)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(edge, max(1.2, R * 0.035 * (1.0 + 0.4 * lv))))
        p.drawPath(gem_path)

    def _paint_saturn(self, p: QPainter, f: dict) -> None:
        """Ringed planet with an orbiting moon."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, lx, ly = f["radius"], f["energy"], f["lx"], f["ly"]
        # The voice travels round the rings: the band swells, a brightness wave
        # runs along it and the moon is pulled faster. Neutral at level 0.
        lv = min(1.0, max(0.0, float(f["level"])))
        tilt, flat = -0.35, 0.32

        def _ring_pt(rr: float, a: float) -> tuple:
            ex, ey = math.cos(a) * rr, math.sin(a) * rr * flat
            rx = ex * math.cos(tilt) - ey * math.sin(tilt)
            ry = ex * math.sin(tilt) + ey * math.cos(tilt)
            return cx + rx, cy + ry

        p.setPen(Qt.NoPen)
        pr = radius * 0.52
        # The moon orbits at 1.30 R, which is wider than the glass once the
        # bubble is at its listening size: measured, its disc crossed the rim
        # (16 px at alpha 200 — a white dot sliced flat by the mask). It is
        # capped to the aperture so the outermost thing Saturn draws is inside
        # the rim. (Its ring, at 1.02 R, never crosses and so is NOT clamped —
        # an aperture clamp there measured as a no-op and was not kept.)
        ring_pen = max(1.5, radius * 0.06 * (1.0 + 0.5 * lv))
        ring_r = radius * 1.02
        # back half of the ring (behind the planet)
        back = QPainterPath()
        for i in range(37):
            a = math.pi + i * (math.pi / 36)
            x, y = _ring_pt(ring_r, a)
            back.moveTo(x, y) if i == 0 else back.lineTo(x, y)
        rc = QColor(color)
        rc.setAlpha(int(min(255.0, 110 + 70 * energy + 60 * lv)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rc, ring_pen, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(back)
        # glass planet
        globe = QRadialGradient(QPointF(cx + lx * pr * 0.5, cy + ly * pr * 0.5), pr * 1.6)
        globe.setColorAt(0.0, QColor(int(120 + 90 * lv), int(128 + 80 * lv),
                                     int(150 + 70 * lv)))
        mid = QColor(color)
        mid.setAlpha(235)
        globe.setColorAt(0.4, mid)
        globe.setColorAt(1.0, QColor(8, 9, 13))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(globe))
        p.drawEllipse(QPointF(cx, cy), pr, pr)
        p.setBrush(QColor(255, 255, 255, int(min(255.0, 70 + 90 * lv))))
        p.drawEllipse(QPointF(cx + lx * pr * 0.45, cy + ly * pr * 0.45),
                      pr * 0.22 * (1.0 + 0.5 * lv), pr * 0.15 * (1.0 + 0.4 * lv))
        # front half of the ring (in front of the planet)
        front = QPainterPath()
        for i in range(37):
            a = i * (math.pi / 36)
            x, y = _ring_pt(ring_r, a)
            front.moveTo(x, y) if i == 0 else front.lineTo(x, y)
        rc.setAlpha(int(min(255.0, 170 + 60 * energy + 60 * lv)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rc, max(2.0, radius * 0.075 * (1.0 + 0.5 * lv)),
                      Qt.SolidLine, Qt.RoundCap))
        p.drawPath(front)
        # a brightness wave travels round the front of the ring with the voice
        if lv > 0.004:
            wa = t * (1.3 + 2.4 * lv)
            wave = QPainterPath()
            for i in range(13):
                x, y = _ring_pt(ring_r, wa - 0.45 + i * (0.9 / 12))
                wave.moveTo(x, y) if i == 0 else wave.lineTo(x, y)
            wc = QColor(255, 255, 255, int(min(255.0, 210 * lv)))
            wave_pen = max(2.5, radius * 0.09)
            p.setPen(QPen(wc, wave_pen, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(wave)
        # moon on a wider orbit, pulled faster by the voice
        ma = t * (0.9 + 0.7 * lv)
        mx, my = _ring_pt(min(radius * 1.30, APERTURE_R - 2.2), ma)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawEllipse(QPointF(mx, my), 2.2, 2.2)

    def _paint_void(self, p: QPainter, f: dict) -> None:
        """Voice void: black disc with a level-flared event horizon."""
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, level = f["radius"], f["energy"], f["level"]
        # The voice accelerates the infall: the spiral streaks brighten, the
        # sparks sweep inward faster and the horizon's flicker runs at a higher
        # rate, so it looks like it is feeding rather than just glowing.
        lv = min(1.0, max(0.0, float(level)))
        R = radius * 0.9
        p.setPen(Qt.NoPen)
        # A broad state-coloured halo, the same answer the orb uses for the same
        # problem (a dark body on a dark wallpaper). Without it this design's
        # only state-coloured pixels were the ~2px streaks and the hairline rim:
        # swapping the state colour changed EIGHT pixels by a visible step out
        # of 45796, so the Appearance colour picker was, in effect, disabled on
        # this design while working on every other one.
        glow0 = float(f.get("glow", 1.0))
        halo = QRadialGradient(QPointF(cx, cy), R + GLOW_PAD)
        hc = QColor(color)
        hc.setAlpha(int(min(255.0, (96 + 74 * lv) * glow0)))
        halo.setColorAt(R / (R + GLOW_PAD), hc)
        hc.setAlpha(0)
        halo.setColorAt(1.0, hc)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), R + GLOW_PAD, R + GLOW_PAD)
        p.setBrush(QBrush(QColor(3, 3, 5)))
        p.drawEllipse(QPointF(cx, cy), R, R)
        # the disc itself is deliberately black, so the accent has only these
        # thin elements to act on: without this the slider measured 16 changed
        # pixels out of 45796 — i.e. nothing. `glow` is 1.0 at the default.
        #
        # The alpha FLOORS are raised for the same reason sauron's and
        # pikachu's are: at rest this design's state colour measured **10**
        # pixels above a visible step out of 45796, because the streaks were
        # drawn at 22/255 behind a black disc and the rim at 40/255. Picking a
        # colour therefore looked like it did nothing on the one design with no
        # body colour to carry it — measured against the least-visible design
        # in the set (equalizer, 804 px) as the bar.
        glow = float(f.get("glow", 1.0))
        # infalling spiral streaks
        for k, (rr, spd, span) in enumerate(((0.80, 1.2, 1.2), (0.66, -0.9, 1.0), (0.52, 1.6, 0.8))):
            c = QColor(color).lighter(160)
            c.setAlpha(int(min(255.0, (86 + 54 * energy + 80 * lv) * glow)))
            self._arc(p, cx, cy, R * rr, t * 2 * math.pi * spd * 0.25 + k * 2.1, span, c,
                      max(1.0, R * 0.03 * (1.0 + 0.5 * lv)))
        # spiralling infall sparks, sweeping inward faster the louder it is
        for i in range(8):
            ph = (t * (0.22 + 0.55 * lv) + i / 8) % 0.75
            rr = R * (0.88 - ph)
            a = i * 2.4 + t * (1.0 + i * 0.1)
            s = QColor(color).lighter(170)
            s.setAlpha(int(min(255.0, (1.0 - ph) * (110 + 80 * energy + 90 * lv) * glow)))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(s))
            p.drawEllipse(QPointF(cx + math.cos(a) * rr, cy - math.sin(a) * rr), 1.6, 1.6)
        # event horizon: hairline at idle, flaring with voice — and its flicker
        # runs faster as the level rises (13 Hz at rest, ~24 Hz at full voice)
        flicker = 0.7 + 0.3 * math.sin(t * (13.0 + 11.0 * lv))
        rim_c = QColor(color).lighter(180)
        rim_c.setAlpha(int(min(255.0, (120 + 15 * math.sin(t * 0.8)
                                        + 190 * min(1.0, level * flicker + energy * 0.15)) * glow)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rim_c, max(1.0, R * (0.02 + 0.10 * level * flicker) * (0.6 + 0.4 * glow)),
                            Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(QPointF(cx, cy), R, R)

    def _paint_sauron(self, p: QPainter, f: dict) -> None:
        """The Eye of Sauron: a slit-pupilled eye wreathed in flame.

        The fire is deliberately canonical — deep red through orange to a
        white-hot core — because that is the design, not a palette choice. The
        surrounding corona is tinted by the state colour instead, so the
        Appearance colour picker still visibly moves it, and both sliders still
        work: `energy` drives the blaze/pupil flare and `glow` (the colour
        accent) the corona's punch. Like `void`, this design keeps its own
        identity rather than taking the state colour as its body.
        """
        cx, cy, t, tint = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, level = f["radius"], f["energy"], f["level"]
        glow = float(f.get("glow", 1.0))
        anim = float(f.get("anim", 1.0))
        lx, ly = f["lx"], f["ly"]
        R = radius * 1.02

        # Voice reaction. `level` is the smoothed 0..1 amplitude the equalizer
        # bars already follow: the mic while listening, and — via
        # core.audio.play_wav — the actual playback while speaking, since the
        # mic is blanked then. Every term below is written so that level == 0
        # reproduces the previous rendering exactly (multiplied by 1.0, or
        # added as 0), so a silent bubble looks unchanged.
        lv = min(1.0, max(0.0, float(level)))
        lv2 = lv * lv                     # a quieter floor, so a murmur is subtle

        # two out-of-phase sines: a single one reads as a sine wave, two read
        # as fire. `blaze` is the flickering intensity the whole eye shares.
        flick = 0.78 + 0.16 * math.sin(t * 6.1) + 0.06 * math.sin(t * 17.3 + 1.1)
        blaze = min(1.0, max(0.0, energy * 1.15 + 0.10 * anim + 0.35 * lv2)) * flick

        p.setPen(Qt.NoPen)
        # -- corona: fire first, with the state tint only as a thin outer aura.
        # Ordering matters: a big tinted disc repainted most of the eye's own
        # pixels (measured 38% warm, i.e. the aura was the subject and the fire
        # the background). The tint now starts late and fades quickly.
        # the aura is the outermost thing the eye draws, and at the top of the
        # size range it reached past the glass (measured 523 pixels at alpha up
        # to 255)
        halo_r = min(R * 1.12, APERTURE_R)
        halo = QRadialGradient(QPointF(cx, cy), halo_r)
        hot = QColor(int(120 + 135 * blaze), int(40 + 90 * blaze), int(8 + 30 * blaze))
        hot.setAlpha(int(min(255.0, (70 + 110 * blaze) * glow)))
        halo.setColorAt(0.0, hot)
        halo.setColorAt(0.74, hot)
        mid = QColor(tint)
        # the aura brightens with the voice too: the fire spreads outward
        mid.setAlpha(int(min(255.0, 38 * glow * (1.0 + 0.9 * lv2))))
        halo.setColorAt(0.90, mid)
        mid.setAlpha(0)
        halo.setColorAt(1.0, mid)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # -- flame tongues licking outward, each on its own rhythm. The voice
        #    reaches them further and brightens them, so speaking sets the eye
        #    alight rather than just tinting it.
        for i in range(14):
            ang = i * 2 * math.pi / 14 + 0.12 * math.sin(t * 1.4 + i)
            lick = 0.62 + 0.38 * math.sin(t * (3.1 + (i % 5) * 0.7) + i * 1.9)
            pen = max(1.2, R * 0.085 * (0.6 + 0.6 * lick))
            # the tongues are capped by the glass AND by their own round caps:
            # a stroke's cap extends half the pen past the point it is drawn to,
            # so clamping the point alone still leaves ink outside.
            outer = min(APERTURE_R - pen * 0.5,
                        R * (1.02 + 0.30 * lick * (0.5 + 0.5 * blaze)
                             + 0.22 * lv2 * lick))
            x0, y0 = cx + math.cos(ang) * R * 0.82, cy - math.sin(ang) * R * 0.82
            x1, y1 = cx + math.cos(ang) * outer, cy - math.sin(ang) * outer
            fc = QColor(int(200 + 55 * blaze), int(70 + 105 * blaze),
                        int(10 + 30 * blaze))
            fc.setAlpha(int(min(255.0, (90 + 130 * lick) * glow * (1.0 + 0.75 * lv2))))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(fc, pen, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x0, y0), QPointF(x1, y1))

        # -- sclera: a wide almond (two quadratic arcs meeting at the corners)
        sx, sy = R * 0.98, R * 0.60
        lens = QPainterPath()
        lens.moveTo(cx - sx, cy)
        lens.quadTo(cx, cy - sy * 1.5, cx + sx, cy)
        lens.quadTo(cx, cy + sy * 1.5, cx - sx, cy)
        eye = QRadialGradient(QPointF(cx, cy), R)
        eye.setColorAt(0.0, QColor(255, 236, 170))
        eye.setColorAt(0.45, QColor(int(215 + 40 * blaze),
                                    int(105 + 70 * blaze), 18))
        eye.setColorAt(1.0, QColor(84, 16, 4))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(eye))
        p.drawPath(lens)
        # -- rim of fire hugging the eye: the Eye's signature edge
        rim = QColor(255, int(150 + 70 * blaze), int(40 + 40 * blaze))
        rim.setAlpha(int(min(255.0, (150 + 90 * blaze) * glow * (1.0 + 0.5 * lv))))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rim, max(1.2, R * 0.045), Qt.SolidLine, Qt.RoundCap))
        p.drawPath(lens)

        # -- pupil: the vertical slit, drifting slightly toward the key light.
        #    It NARROWS as the voice rises (the pupil contracts) and lengthens a
        #    touch, which is the readable cue that the eye is reacting.
        px = cx + lx * R * 0.06
        py = cy + ly * R * 0.04
        pw = R * (0.085 + 0.035 * blaze) * (1.0 - 0.46 * lv)
        ph = sy * (0.92 - 0.18 * blaze) * (1.0 + 0.06 * lv)
        # NoPen, explicitly: the rim-of-fire pen is still active here, and it
        # outlined the slit in bright orange — the pupil read as a hot ring
        # instead of a dark slit (and vanished entirely once the rim thickened).
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(6, 2, 2))
        p.drawEllipse(QPointF(px, py), pw, ph)
        # a hot filament inside the slit, so it reads as fire rather than a hole
        fil = QColor(255, 214 - int(40 * blaze), 120)
        fil.setAlpha(int(min(255.0, 150 * glow * (1.0 + 0.5 * lv))))
        p.setBrush(fil)
        p.drawEllipse(QPointF(px, py), max(0.8, pw * 0.30), max(1.5, ph * 0.80))
        # wet glint, brightening with the voice
        p.setBrush(QColor(255, 255, 255, int(min(255.0, 120 + 80 * min(1.0, level)))))
        p.drawEllipse(QPointF(px - pw * 1.3, py - ph * 0.35),
                      max(0.9, R * 0.035), max(0.9, R * 0.028))

    def _paint_pikachu(self, p: QPainter, f: dict) -> None:
        """Pikachu: a round yellow face, black-tipped ears and charging cheeks.

        Like `void` and `sauron`, this design keeps its own canonical palette —
        Pikachu yellow, black ear tips, red cheeks — because that IS the design,
        not a palette choice. The state colour is relocated to an aura ring
        BEHIND the head, so the Appearance colour picker still visibly moves it.
        Both shared sliders and the voice reach it through the frame state every
        painter consumes: `energy` drives the aura, the ear sway and the
        breathing, `glow` (the accent) punches the aura and the cheek bloom, and
        `level` charges the cheeks the way the games do — the bloom swells, a
        hot core appears, the ears prick up and the mouth opens as the voice
        rises. Every level term multiplies by 1.0 or adds 0 at level 0, so a
        silent bubble renders the resting Pikachu exactly.

        Geometry note: every number below is constrained by ONE hard limit.
        The window carries an inscribed-ellipse mask (BubbleWidget.showEvent),
        and the worst case is LISTENING at full level, where the radius has
        already grown to `BUBBLE_R0 + 12 * GEOM_K` — leaving only 1.12 R of
        headroom from the centre. Points past that are silently cut off on the
        desktop, which is why the ears stop short of where the silhouette would
        ideally reach. `pikachu_stays_inside_the_window_mask` pins the bound.
        """
        cx, cy, t, tint = f["cx"], f["cy"], f["t"], f["color"]
        radius, energy, level = f["radius"], f["energy"], f["level"]
        glow = float(f.get("glow", 1.0))
        anim = float(f.get("anim", 1.0))
        R = radius

        lv = min(1.0, max(0.0, float(level)))
        lv2 = lv * lv                     # a quieter floor: a murmur is subtle

        # breathing at rest, and a small lift of the whole face with the voice
        scale = (1.0 + 0.014 * math.sin(t * 2.2) * anim) * (1.0 + 0.035 * lv2)
        head_rx, head_ry = R * 0.72 * scale, R * 0.60 * scale

        # -- aura: the ONLY place the state colour lives, so the colour picker
        #    clearly moves this design while the mascot keeps its own palette.
        #    Capped to a 0.44 * width circle: that is inside the mask ellipse at
        #    every radius, so the glow itself can never be clipped, and the ring
        #    hugs the head rather than flooding the whole window.
        p.setPen(Qt.NoPen)
        halo_r = min(R * 1.12, 0.44 * self.width())
        inner_f = min(0.80, max(head_rx, head_ry) / halo_r * 0.94)
        clear = QColor(tint)
        clear.setAlpha(0)
        lit = QColor(tint)
        lit.setAlpha(int(min(255.0, (26 + 62 * energy) * glow * (1.0 + 0.85 * lv2))))
        halo = QRadialGradient(QPointF(cx, cy), halo_r)
        halo.setColorAt(0.0, clear)
        halo.setColorAt(inner_f, clear)
        halo.setColorAt(min(0.97, inner_f + 0.18), lit)
        halo.setColorAt(1.0, clear)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # -- ears: drawn FIRST so the head hides their bases. A hard-stop
        #    gradient along the ear axis gives the black tip without needing a
        #    second path to trace the ear's outline. Each ear flicks on its own
        #    phase, sways with the animation energy, and pricks up with the
        #    voice — the whole reason the tip offsets are budgeted so tightly.
        for sign in (-1.0, 1.0):
            phase = 0.0 if sign > 0 else 2.1
            flick = 1.0 + 0.03 * math.sin(t * 6.4 * anim + phase)
            tilt = 0.10 * math.sin(t * 5.1 * anim + phase) + 0.06 * lv
            bx1, by1 = cx + sign * R * 0.38, cy - R * 0.02
            bx2, by2 = cx + sign * R * 0.08, cy - R * 0.40
            tx = cx + sign * (R * 0.52 + tilt * R) * flick
            ty = cy - R * 0.80 * flick
            ear = QPainterPath()
            ear.moveTo(bx1, by1)
            ear.quadTo(cx + sign * R * 0.44, cy - R * 0.72, tx, ty)
            ear.quadTo(cx + sign * R * 0.24, cy - R * 0.58, bx2, by2)
            ear.closeSubpath()
            eg = QLinearGradient(QPointF(bx1, by1), QPointF(tx, ty))
            eg.setColorAt(0.0, QColor(255, 232, 116))
            eg.setColorAt(0.55, QColor(250, 202, 38))
            eg.setColorAt(0.585, QColor(32, 26, 13))
            eg.setColorAt(1.0, QColor(11, 10, 7))
            p.setBrush(QBrush(eg))
            p.drawPath(ear)

        # -- head
        hx, hy = cx, cy + R * 0.10 * scale
        head = QRadialGradient(QPointF(cx - R * 0.22, cy - R * 0.14), head_rx * 1.60)
        head.setColorAt(0.0, QColor(255, 245, 160))
        head.setColorAt(0.55, QColor(250, 205, 42))
        head.setColorAt(1.0, QColor(206, 142, 12))
        p.setBrush(QBrush(head))
        p.drawEllipse(QPointF(hx, hy), head_rx, head_ry)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(146, 92, 6, 190), max(1.0, R * 0.030)))
        p.drawEllipse(QPointF(hx, hy), head_rx, head_ry)

        # -- cheeks: the whole point of a Pikachu reacting to a voice. The red
        #    disc is always there; the bloom and the hot core are what the voice
        #    adds, so silence is the resting face.
        cheek_r = R * (0.135 + 0.035 * lv) * scale
        for sign in (-1.0, 1.0):
            ccx = hx + sign * R * 0.40 * scale
            ccy = hy + R * 0.30 * scale
            # (An aperture clamp on this bloom was tried and REVERTED: measured
            # at alpha 11 where it crossed the rim, i.e. below the threshold at
            # which ink is visible, so it was a no-op dressed as a fix.)
            br = cheek_r * (2.1 + 0.9 * lv) * (1.0 + 0.10 * math.sin(t * 9.0 * anim + sign))
            bloom = QRadialGradient(QPointF(ccx, ccy), br)
            b = QColor(255, 58, 36)
            b.setAlpha(int(min(255.0, (34 + 165 * lv2) * glow)))
            bloom.setColorAt(0.0, b)
            bz = QColor(255, 58, 36)
            bz.setAlpha(0)
            bloom.setColorAt(1.0, bz)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(bloom))
            p.drawEllipse(QPointF(ccx, ccy), br, br)
            p.setBrush(QColor(236, 60, 46))
            p.drawEllipse(QPointF(ccx, ccy), cheek_r, cheek_r * 0.86)
            if lv > 0.01:               # a hot core: the cheeks are charging
                core = QColor(255, 216, 150, int(min(255.0, 215 * lv)))
                p.setBrush(core)
                p.drawEllipse(QPointF(ccx, ccy), cheek_r * 0.42, cheek_r * 0.36)

        # -- eyes: solid with a glint, which catches as the bubble animates
        eye_r = R * 0.105 * scale
        p.setPen(Qt.NoPen)
        for sign in (-1.0, 1.0):
            ex = hx + sign * R * 0.22 * scale
            ey = hy - R * 0.12 * scale
            p.setBrush(QColor(26, 20, 14))
            p.drawEllipse(QPointF(ex, ey), eye_r, eye_r * 1.12)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawEllipse(QPointF(ex - eye_r * 0.30, ey - eye_r * 0.44),
                          eye_r * 0.36, eye_r * 0.36)

        # -- nose and mouth. The mouth opens a little with the voice: a
        #    speaking bubble reads as talking rather than just glowing.
        p.setBrush(QColor(44, 32, 14))
        p.drawEllipse(QPointF(hx, hy + R * 0.10 * scale), R * 0.030, R * 0.023)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(62, 42, 16, 225), max(1.0, R * 0.032),
                      Qt.SolidLine, Qt.RoundCap))
        mw = R * 0.17 * scale
        my = hy + R * 0.27 * scale
        for sign in (-1.0, 1.0):
            mp = QPainterPath()
            mp.moveTo(hx, my - R * 0.025)
            mp.quadTo(hx + sign * mw * 0.55, my + R * (0.09 + 0.06 * lv), hx + sign * mw, my)
            p.drawPath(mp)

    def _paint_cat(self, p: QPainter, f: dict) -> None:
        """Cat: a state-coloured head with ears, whiskers and a moving tail.

        Palette: unlike `void`, `sauron` and `pikachu`, this design keeps no
        canonical colours of its own — head, ears and tail are painted IN the
        state colour, so a colour click repaints it (the property the three
        mascots trade away for identity). Only the muzzle, the eyes and the
        whiskers are deliberately light, the way a real cat's are.

        Voice: the ears rise and splay, the tail swings wider and lifts, the
        eyes narrow, and the inner-ear glow and the halo brighten — every term
        driven by ONE bounded scalar (`_cat_reach`), neutral at level 0, so a
        silent bubble is the resting cat. That single scalar is also what lets
        the window mask be built once from the same geometry instead of per
        frame.

        Silhouette: the ear tips leave the inscribed ellipse at the corners, so
        this design declares its own region in `design_region` — without it Qt
        silently shears the tips off on the desktop.
        """
        cx, cy, t, color = f["cx"], f["cy"], f["t"], f["color"]
        R = f["radius"]
        lv = min(1.0, max(0.0, float(f["level"])))
        anim = float(f.get("anim", 1.0))
        glow = float(f.get("glow", 1.0))
        energy = float(f["energy"])
        reach = _cat_reach(lv, t, anim)

        def shade(light: int, alpha: int = 255) -> QColor:
            c = QColor(color).lighter(int(light))
            c.setAlpha(int(max(0, min(255, alpha))))
            return c

        # -- halo: the state colour over a dark wallpaper, and what the accent
        #    slider punches. `void` needed the same fix for the same reason.
        #    Capped by the WINDOW, not just by R: the radius grows ~28% at the
        #    listening peak, and 1.18 of that peak measures 66.6 px against a
        #    64 px mask -- i.e. the halo was the one part of this design the
        #    desktop silently cut (591 px of it, measured). Same budget
        #    pikachu's aura uses, and for the same reason.
        halo_r = min(R * 1.18, 0.47 * self.width())
        hc = shade(100, int(min(255.0, (58 + 44 * energy) * glow * (1.0 + 0.5 * lv))))
        hg = QRadialGradient(QPointF(cx, cy), halo_r)
        hg.setColorAt(min(0.95, (R * 0.66) / halo_r), hc)
        hg.setColorAt(1.0, shade(100, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(hg))
        p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # -- tail: behind the body, so it reads as growing out of it
        tail = QColor(color).lighter(125)
        tail.setAlpha(int(min(255.0, (170 + 60 * lv) * glow)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(tail, _cat_tail_width(R), Qt.SolidLine, Qt.RoundCap))
        p.drawPath(_cat_tail(cx, cy, R, reach))

        # -- ears: before the head, which hides their bases. The inner ear is
        #    the state colour lightened and it brightens with the voice, so the
        #    cat visibly prickles; the rim keeps the ear readable on its own.
        for ear, sign in zip(_cat_ears(cx, cy, R, reach), (-1.0, 1.0)):
            p.setBrush(QBrush(shade(76)))
            p.setPen(QPen(shade(155), max(1.0, R * 0.028)))
            p.drawPolygon(ear)
            tip_x = (CAT_EAR_REST[0] + CAT_EAR_VOICE[0] * reach) * 0.84
            tip_y = (CAT_EAR_REST[1] + CAT_EAR_VOICE[1] * reach) * 0.84
            p.setBrush(QBrush(shade(int(min(255.0, 150 + 40 * lv + 30 * (glow - 1.0))))))
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([
                QPointF(cx + sign * R * 0.24, cy - R * 0.44),
                QPointF(cx + sign * R * tip_x, cy - R * tip_y),
                QPointF(cx + sign * R * 0.52, cy - R * 0.34)]))

        # -- head: a radial body, not a flat disc, so the state colour reads as
        #    fur rather than as a painted circle
        hx, hy = cx, cy + R * 0.08
        head = QRadialGradient(QPointF(cx - R * 0.26, cy - R * 0.22), R * 1.30)
        head.setColorAt(0.0, shade(168))
        head.setColorAt(0.62, shade(100))
        head.setColorAt(1.0, shade(134))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(head))
        p.drawEllipse(QPointF(hx, hy), R * 0.74, R * 0.66)

        # -- whiskers: state-tinted, lifted and brightened by the voice
        p.setPen(QPen(shade(210, int(min(255.0, (150 + 95 * lv) * glow))),
                      max(1.0, R * 0.022), Qt.SolidLine, Qt.RoundCap))
        for sign in (-1.0, 1.0):
            for i, (dy, spread, length) in enumerate(
                    ((0.10, 0.16, 0.34), (0.20, 0.12, 0.36), (0.30, 0.06, 0.30))):
                lift = 0.06 * lv * (1 + i)
                x0 = hx + sign * R * 0.40
                y0 = hy + R * (dy - 0.10)
                p.drawLine(QPointF(x0, y0),
                           QPointF(x0 + sign * R * length,
                                   y0 - R * (spread + lift)))

        # -- eyes: almond, narrowing with the voice, with a glint and a
        #    state-coloured rim that brightens as the level rises
        eye_ry = R * 0.17 * (1.0 - 0.42 * lv)
        eye_rx = R * 0.13 * (1.0 + 0.14 * lv)
        for sign in (-1.0, 1.0):
            ex = hx + sign * R * 0.27
            ey = hy - R * 0.10
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(24, 20, 26))
            p.drawEllipse(QPointF(ex, ey), eye_rx, eye_ry)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawEllipse(QPointF(ex - eye_rx * 0.28, ey - eye_ry * 0.40),
                          eye_rx * 0.30, eye_ry * 0.30)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(shade(190, int(min(255.0, 90 + 130 * lv))),
                          max(1.0, R * 0.020)))
            p.drawEllipse(QPointF(ex, ey), eye_rx * 1.25, eye_ry * 1.20)

        # -- muzzle, nose and mouth: the light patch is what makes the rest of
        #    the head unmistakably a face
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(246, 242, 238, 232))
        p.drawEllipse(QPointF(hx, hy + R * 0.30), R * 0.19, R * 0.12)
        p.setBrush(QColor(214, 96, 118))
        p.drawPolygon(QPolygonF([
            QPointF(hx - R * 0.045, hy + R * 0.22),
            QPointF(hx + R * 0.045, hy + R * 0.22),
            QPointF(hx, hy + R * 0.28)]))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(96, 78, 84, 230), max(1.0, R * 0.020),
                      Qt.SolidLine, Qt.RoundCap))
        for sign in (-1.0, 1.0):
            mouth = QPainterPath(QPointF(hx, hy + R * 0.28))
            mouth.quadTo(QPointF(hx + sign * R * 0.10, hy + R * (0.36 + 0.05 * lv)),
                         QPointF(hx + sign * R * 0.17, hy + R * 0.30))
            p.drawPath(mouth)

    @staticmethod
    def _wobble_path(cx: float, cy: float, r0: float, t: float, amt: float = 1.0) -> QPainterPath:
        path = QPainterPath()
        n = 72
        a1, a2 = r0 * 0.115 * amt, r0 * 0.08 * amt
        for i in range(n + 1):
            ang = i * 2 * math.pi / n
            r = r0 + a1 * math.sin(3 * ang + 4.2 * t) + a2 * math.sin(5 * ang - 3.1 * t)
            x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        path.closeSubpath()
        return path

    # -- mouse: push-to-talk + drag ---------------------------------------------

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._pressing = True
            self._dragging = False
            self._listening = False
            self._press_pos = e.globalPosition().toPoint()
            self._assistant.interrupt()  # barge-in: silence current speech/thought
            self._hold.start()
        elif e.button() == Qt.RightButton:
            self._hold.stop()
            self._menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e) -> None:
        if not (self._pressing and not self._dragging):
            if self._manual_drag and self._dragging:
                g = e.globalPosition().toPoint()
                self.move(self.pos() + g - self._drag_last)
                self._drag_last = g
            return
        g = e.globalPosition().toPoint()
        if (g - self._press_pos).manhattanLength() >= DRAG_PX:
            self._dragging = True
            self._hold.stop()
            self._listening = False
            self._assistant.abort_listening()
            self._start_system_drag(g)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.LeftButton:
            return
        self._hold.stop()
        was_dragging, self._dragging = self._dragging, False
        self._pressing = False
        self._manual_drag = False
        if was_dragging:
            self.setCursor(Qt.PointingHandCursor)
            return
        if self._listening:
            self._listening = False
            self._assistant.finish_listening()

    def _hold_fired(self) -> None:
        if self._pressing and not self._dragging and not self._menu_open:
            self._listening = True
            self._assistant.begin_listening()

    def _start_system_drag(self, g) -> None:
        handle = self.windowHandle()
        try:
            moved = handle is not None and handle.startSystemMove()
        except Exception:
            moved = False
        if not moved:  # X11-style manual move fallback
            self._manual_drag = True
            self._drag_last = g
            self.setCursor(Qt.ClosedHandCursor)

    # -- menu ------------------------------------------------------------------

    def _menu(self, gpos) -> None:
        self._menu_open = True
        try:
            m = QMenu(self)
            act_settings = m.addAction("Settings…")
            m.addSeparator()
            act_hf = m.addAction("Hands-free: on" if self._assistant._handsfree
                                 else "Hands-free: off")
            act_interrupt = m.addAction("Interrupt (stop talking)")
            m.addSeparator()
            act_restart = m.addAction("Restart (reload code)")
            act_quit = m.addAction("Quit")
            chosen = m.exec(gpos)
        finally:
            self._menu_open = False
        if chosen == act_quit:
            # under systemd, plain quit would be resurrected by Restart=always:
            # stop the unit first, then exit quietly
            try:
                subprocess.run(
                    ["systemctl", "--user", "stop", "handsoff.service"],
                    timeout=10, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            QApplication.quit()
        elif chosen == act_settings:
            self._open_settings()
        elif chosen == act_hf:
            self._assistant.set_handsfree(not self._assistant._handsfree)
        elif chosen == act_interrupt:
            self._assistant._on_command("interrupt")
        elif chosen == act_restart:
            if RESTART_SCRIPT.exists():
                subprocess.Popen(
                    [str(RESTART_SCRIPT)], start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                QApplication.quit()

    def _open_settings(self) -> None:
        if SETTINGS_APP.exists():
            subprocess.Popen(
                [sys.executable, str(SETTINGS_APP)], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            notify(f"handsoff: settings app not installed at {SETTINGS_APP}")

    def _apply_mask(self) -> None:
        """Re-shape the window to its CURRENT rect — a mask does not resize.

        A QRegion mask is in widget coordinates and Qt keeps it exactly as it
        was when the widget is resized, so the mask set at show time stayed the
        size the bubble was born at. Every live size change then drew the new,
        scaled design through the OLD aperture: growing it clipped the design to
        the        previous circle, and swapping shapes while big showed each shape's
        scaled geometry inside a smaller stale one — the "bigger breaks it, and
        then the shapes don't match" report. Measured: mask 128x128 while the
        widget was 192x192.

        The shape itself comes from `design_region`: the inscribed ellipse for
        every design that is painted inside it, plus the design's own outline
        where it has one (the cat's ears), so a design with a silhouette of its
        own is not silently cut back to a circle on the desktop.
        """
        name = str(SETTINGS.get("bubble_design", "orb")).strip().lower()
        self.setMask(design_region(name, self.width(), self.height()))
        self._mask_design = name

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        super().resizeEvent(event)
        self._apply_mask()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        super().showEvent(event)
        self._apply_mask()

    def closeEvent(self, _e) -> None:
        self._assistant.shutdown()


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
        self._runs = BoundedRegistry("control", 1)
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
        SETTINGS["mic_device"] or "system default", SETTINGS["mic_threshold"], WINDOW_PX,
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
    bubble = BubbleWidget(assistant)
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
