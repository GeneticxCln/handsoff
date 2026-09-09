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
    whisper size, piper voice + speech rate/volume, tool permissions, bubble
    size and colours, and the niri autostart entry.
    Precedence: built-in defaults <- environment <- settings.json.

Environment overrides (apply only where settings.json has no value):
    HANDSOFF_MODEL, OLLAMA_HOST, HANDSOFF_NUM_CTX, HANDSOFF_WHISPER, HANDSOFF_VOICE,
    HANDSOFF_KEEP_ALIVE

Files:
    ~/.config/handsoff/settings.json    settings written by the settings app
    ~/.config/handsoff/whisper-model/   faster-whisper model cache
    ~/.config/handsoff/piper-voice/     piper voice (.onnx + .onnx.json)
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
import json
import logging
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
import zoneinfo
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
    from PySide6.QtCore import QElapsedTimer, QObject, QPointF, Qt, QTimer, Signal
    from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QGuiApplication,
        QPainter,
        QPainterPath,
        QPen,
        QRadialGradient,
        QRegion,
    )
    from PySide6.QtWidgets import QApplication, QMenu, QWidget
except ImportError:
    _missing("PySide6", "pyside6", "python-pyside6")

# --------------------------------------------------------------------------- config

VERSION = "1.0.0"
APP_NAME = "handsoff"
HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "handsoff"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / APP_NAME
WHISPER_MODEL_DIR = CONFIG_DIR / "whisper-model"
PIPER_VOICE_DIR = CONFIG_DIR / "piper-voice"
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
_SETTINGS_WRITE_LOCK = threading.Lock()
SELF_MARKER = "# handsoff-self-marker: this line must be preserved across self-edits"

DEFAULT_SETTINGS: dict = {
    "ollama_host": "http://127.0.0.1:11434",
    "model": "qwen3:8b",
    "num_ctx": 32768,
    "history_tokens": 0,       # 0 = auto: ctx − prompt − reply reserve
    "whisper_size": "tiny",
    "piper_voice": "",
    "tts_rate": 1.0,
    "tts_volume": 1.0,
    "mic_device": "",
    "mic_threshold": 600,
    "handsfree": False,
    "bubble_size": 128,
    "colors": {
        "idle": "#4f8cff", "listening": "#ff4d5e",
        "thinking": "#ff9e2c", "speaking": "#3ecf6e",
    },
    "permissions": {
        "run_command": True, "read_file": True,
        "edit_file": True, "self_restart": True,
        "type_text": True, "press_keys": True,
        "web_access": True,
        "media": True,
        "screen_access": True,
        "operator": False,    # mouse control (click_element/click_at) — OFF by
                              # default; enabling lets the AI move and click
                              # the real pointer
        "paste_text": True,   # reading the user's clipboard gets its own switch
        "copy_text": True,    # writing the user's clipboard
        "reminders": True,    # create/list/cancel/snooze spoken reminders
        "calendar": True,     # read ICS calendars, print month grids
        "focus_window": True,  # raise/focus arbitrary windows by name
        "get_datetime": True,  # trivially safe; kept gated for uniformity
        "notifications": False,  # desktop notifications are private by default
        "pomodoro": True,
        "watchers": True,
    },
    "extra_allowed_commands": [],
    "tool_call_times": None,          # filled per-ToolBelt: deque of monotonic times
    "max_tool_calls": 0,             # 0 = no limit; set an int to rate-limit tool calls
    "command_policy": {},            # tool -> ALLOW | DENY | CONFIRM (empty = all ALLOW)
    "confirm_seconds": 90.0,         # how long a CONFIRM offer stays valid
    "dry_run": False,                # desktop actions report instead of act
    "streaming_tts": True,
    "autostart": False,
    "assistant_name": "assistant",
    "wake_word_required": False,
    "engage_seconds": 45.0,
    "workspace_aliases": {},   # 'code': '2' → "go to code" just works
    "home_place": "",          # weather without naming a place; powers the briefing
    "calendar_ics": [],        # ICS source(s): https URL(s) and/or .ics file paths
    "wake_spotter": False,     # openWakeWord audio spotter (near-zero CPU wake)
    "mic_selfheal": True,      # auto-restart a wedged mic + spoken explanation
    "dictation": True,         # 'start dictation' types transcripts, no LLM turn
    "spotter_models": ["hey_jarvis"],   # stock: alexa, hey_jarvis, hey_mycroft, timer, weather
    "followup_seconds": 6.0,   # announce-and-listen: no-wake-word window after a reply
    "briefing": False,         # daily briefing on the first wake word
    "world_warnings": False,   # opt-in proactive severe world-event warnings
    "world_cooldown_min": 60.0,  # min minutes between world warnings
    "hardware_watch": False,   # opt-in live hardware watch on the health tick
    "hardware_cooldown_min": 60.0,  # min minutes between hardware urgents
    "hardware_disk_gb": 5.0,   # disk-free floor (GiB) for the low-disk warning
    "resource_alerts": False,  # opt-in RAM/VRAM threshold announcements
    "ram_alert_percent": 90.0,
    "vram_alert_percent": 90.0,
    "notification_reader": False,  # desktop notifications are private by default
    "notification_mute_apps": [],
}

SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEPLOYMENT_FILE = CONFIG_DIR / "deployment.json"
SYSTEMD_UNIT_FILE = HOME / ".config/systemd/user/handsoff.service"


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


def _deployment_snapshot() -> dict:
    """Describe the code actually running and whether it matches the checkout.

    This is deliberately based on hashes, not mtimes: a stale installed copy
    can have a newer timestamp after a failed deployment.  No settings values
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
    if not current_hash:
        status = "running-missing"
    elif not repo_hash:
        status = "source-unknown"
    elif not installed_hash:
        status = "installed-missing"
    elif same_installed_repo:
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
        "manifest": manifest,
    }


# --------------------------------------------------------------- doctor report

try:
    import hardware as _hardware
except ImportError:  # installed copy without the sibling module (yet)
    _hardware = None

_DOCTOR_TTL: dict = {}  # shared TTL cache for prompt-context snapshots


def _doctor_ctx() -> dict:
    """handsoff globals → hardware ctx (never the reverse: no SETTINGS import)."""
    return {
        "ollama_base": OLLAMA_BASE, "ollama_model": OLLAMA_MODEL,
        "whisper_size": WHISPER_SIZE, "piper_voice": PIPER_VOICE_NAME,
        "mic_device": str(SETTINGS.get("mic_device") or ""),
        "whisper_model_dir": str(WHISPER_MODEL_DIR),
        "piper_voice_dir": str(PIPER_VOICE_DIR),
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


def run_doctor() -> str:
    """One human-readable diagnostic pass over everything the bubble needs.

    Read-only, side-effect-free (the Ollama probe is a 2 s GET). Served via
    `--ptt doctor` and the `handsoff_doctor` tool; the AI reads it to fix
    itself instead of guessing.
    """
    lines: list[str] = []

    d = _deployment_snapshot()
    status = d["status"]
    deploy_note = {
        "in-sync": "installed copy matches the checkout",
        "installed-drift": (
            "INSTALLED COPY IS STALE — the running code is not the checkout. "
            "Re-run install.sh to deploy the tested source."),
        "source-unknown": "no checkout found (nothing to compare against)",
        "installed-missing": "no copy at ~/.local/bin/handsoff.py — run install.sh",
        "running-missing": "running source unreadable",
    }.get(status, status)
    lines.append(f"deployment: {status} — {deploy_note}")
    lines.append(f"  running: {d['running_path']}")
    if d["running_sha256"]:
        lines.append(f"  running sha256: {d['running_sha256'][:16]}…")
    if d["repo_path"]:
        lines.append(f"  checkout: {d['repo_path']}")

    # ponytail: probe once via hardware.snapshot(); format the same strings
    # so --ptt doctor output stays byte-stable. Fresh cache: doctor must see
    # live state, never a TTL entry. Legacy probes below run only when the
    # sibling module is absent.
    snap = _doctor_snapshot({})
    if snap is not None:
        if snap["ollama"].get("ok"):
            lines.append(f"brain: Ollama reachable at {OLLAMA_BASE} (model {OLLAMA_MODEL})")
        else:
            lines.append(
                f"brain: OLLAMA UNREACHABLE at {OLLAMA_BASE} — "
                "`systemctl status ollama`, then `ollama pull " + OLLAMA_MODEL + "`")

        lines.append(f"tts: {'voice loaded' if _piper_voice is not None else 'voice NOT loaded yet'}; "
                     f"stt: {'whisper loaded' if _whisper_model is not None else 'whisper NOT loaded yet'}")

        audio = snap["audio"]
        if audio.get("ok") and audio.get("count"):
            lines.append(f"mic: {audio['count']} input device(s) visible")
        elif audio.get("ok"):
            lines.append("mic: NO input devices visible — check the mic is plugged in")
        else:
            lines.append(f"mic: audio subsystem error: {audio.get('error', 'unknown')}")

        comp = snap["compositor"]
        if comp.get("ok"):
            lines.append(f"niri IPC: ok ({comp.get('windows', 0)} window(s))")
        elif comp.get("error"):
            lines.append(f"niri IPC: UNAVAILABLE ({comp['error']}) — desktop actions will fail")
        else:
            lines.append("niri IPC: refused — desktop actions will fail")

        ydo = snap["ydotool"]
        if not ydo.get("installed", True):
            lines.append("ydotool: NOT INSTALLED (typing tools will fail)")
        elif ydo.get("reachable"):
            lines.append(f"ydotool: ok (daemon reachable at {ydo.get('socket')})")
        else:
            lines.append(f"ydotool: daemon UNREACHABLE (no socket at {ydo.get('socket')}) — "
                         "start it: systemctl --user enable --now ydotool.service")
    else:
        if ollama_available():
            lines.append(f"brain: Ollama reachable at {OLLAMA_BASE} (model {OLLAMA_MODEL})")
        else:
            lines.append(
                f"brain: OLLAMA UNREACHABLE at {OLLAMA_BASE} — "
                "`systemctl status ollama`, then `ollama pull " + OLLAMA_MODEL + "`")

        lines.append(f"tts: {'voice loaded' if _piper_voice is not None else 'voice NOT loaded yet'}; "
                     f"stt: {'whisper loaded' if _whisper_model is not None else 'whisper NOT loaded yet'}")

        try:
            import sounddevice as _sd
            devs = [dd for dd in _sd.query_devices() if dd.get("max_input_channels", 0) > 0]
            if devs:
                lines.append(f"mic: {len(devs)} input device(s) visible")
            else:
                lines.append("mic: NO input devices visible — check the mic is plugged in")
        except Exception as e:
            lines.append(f"mic: audio subsystem error: {e}")

        try:
            r = ToolBelt._niri_msg("msg", "--json", "windows")
            if r.returncode == 0:
                n = len(json.loads(r.stdout or "[]"))
                lines.append(f"niri IPC: ok ({n} window(s))")
            else:
                lines.append("niri IPC: refused — desktop actions will fail")
        except Exception as e:
            lines.append(f"niri IPC: UNAVAILABLE ({e}) — desktop actions will fail")

        if shutil.which("ydotool"):
            sock = ToolBelt._ydotool_socket()
            if ToolBelt._socket_connectable(sock):
                lines.append(f"ydotool: ok (daemon reachable at {sock})")
            else:
                lines.append(f"ydotool: daemon UNREACHABLE (no socket at {sock}) — "
                             "start it: systemctl --user enable --now ydotool.service")
        else:
            lines.append("ydotool: NOT INSTALLED (typing tools will fail)")

    if RESTART_SCRIPT.exists():
        lines.append(f"restart script: present at {RESTART_SCRIPT}")
    else:
        lines.append(f"restart script: MISSING at {RESTART_SCRIPT} — run install.sh")

    unit = SYSTEMD_UNIT_FILE
    if unit.exists():
        txt = ""
        try:
            txt = unit.read_text(encoding="utf-8")
        except OSError:
            pass
        if "Restart=always" in txt or "Restart=on-failure" in txt:
            lines.append("systemd unit: present, auto-restart configured")
        else:
            lines.append("systemd unit: present but has NO Restart= — crashes stay dead")
    else:
        lines.append("systemd unit: not installed (autostart falls back to niri spawn)")

    if CRASH_LOG.exists():
        try:
            age = time.time() - CRASH_LOG.stat().st_mtime
            lines.append(f"crash log: exists, last modified {age / 3600:.1f}h ago")
        except OSError:
            lines.append("crash log: exists (age unknown)")
    else:
        lines.append("crash log: none (no native crashes recorded)")

    # can the bubble even bind its control socket? a symlinked/permissive
    # path fails _prepare_runtime at startup and the failure was silent
    try:
        info = CONTROL_SOCK.lstat()
        if stat.S_ISLNK(info.st_mode):
            lines.append("control socket: REFUSES STARTUP — path is a symlink")
        elif info.st_uid != os.getuid():
            lines.append("control socket: REFUSES STARTUP — not owned by you")
        elif not stat.S_ISSOCK(info.st_mode):
            lines.append("control socket: REFUSES STARTUP — path is not a socket")
        else:
            lines.append("control socket: ok")
    except FileNotFoundError:
        lines.append("control socket: not created yet (bubble not running?)")
    except OSError as e:
        lines.append(f"control socket: lstat failed ({e})")
    return "\n".join(lines)


def doctor_json() -> dict:
    """Machine-readable doctor output for the control socket."""
    d = _deployment_snapshot()
    out: dict = {"deployment": d}
    if CRASH_LOG.exists():
        try:
            out["crash_log_age_hours"] = round(
                (time.time() - CRASH_LOG.stat().st_mtime) / 3600.0, 2)
        except OSError:
            pass
    out["restart_script"] = RESTART_SCRIPT.exists()
    out["systemd_unit"] = {
        "present": SYSTEMD_UNIT_FILE.exists(),
        "auto_restart": False,
    }
    if SYSTEMD_UNIT_FILE.exists():
        try:
            txt = SYSTEMD_UNIT_FILE.read_text(encoding="utf-8")
            out["systemd_unit"]["auto_restart"] = bool(
                re.search(r"^Restart=(always|on-failure|on-abnormal)$", txt, re.M))
        except OSError:
            pass
    snap = _doctor_snapshot(_DOCTOR_TTL)
    if snap is not None:
        out["hardware"] = snap
        out["prompt_context"] = _hardware_prompt_context()
    return out


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


def log_decision(tool: str, target: str, decision: str,
                 result: str = "dispatched") -> None:
    """Append one JSON line to ~/.local/state/handsoff/decisions.jsonl.

    Every tool decision — ALLOW, DENY, CONFIRM, DRY-RUN — lands here with an
    action id, so 'why did it do that' always has an answer. Best-effort:
    a failed log write must never break the tool call itself."""
    entry = {
        "id": f"{int(time.time() * 1000):x}-{random.randrange(1 << 16):04x}",
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": tool,
        "target": str(target)[:200],
        "decision": decision,
        "result": result,
    }
    try:
        with _DECISIONS_LOCK:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with DECISIONS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.chmod(DECISIONS_FILE, 0o600)
            try:
                with DECISIONS_FILE.open("r", encoding="utf-8") as fh:
                    lines = fh.readlines()
                if len(lines) > _DECISIONS_MAX * 2:
                    # ponytail: atomic write, not a predictable .tmp swap
                    _atomic_private_write(
                        DECISIONS_FILE, "".join(lines[-_DECISIONS_MAX:]))
            except OSError:
                pass   # trim is cosmetic; the append already succeeded
    except Exception:
        # the decision log must NEVER break the tool call it records
        log.debug("decision log write failed", exc_info=True)


class DecisionPolicy:
    """Central tool policy: every tool call classifies to ALLOW, DENY or
    CONFIRM before it runs.

    - ALLOW: run normally (the default; per-tool permission gates still apply).
    - DENY:  the user disabled this tool in settings['command_policy'] —
             refused before any code runs, regardless of permission switches.
    - CONFIRM: the tool self-manages a one-turn-separated spoken confirmation
             (like kill_process → confirm_kill): the first call only proposes,
             the second call — a separate model turn after the user heard the
             offer — executes. Tools classified CONFIRM must be two-step.
    Dry-run mode (settings['dry_run']) makes desktop actions REPORT what they
    would do without doing it — for rehearsing a scripted sequence.
    """

    def __init__(self, settings: "dict | None" = None) -> None:
        self._settings = settings if settings is not None else SETTINGS

    def classify(self, tool: str) -> str:
        pol = self._settings.get("command_policy") or {}
        if not isinstance(pol, dict):
            return "ALLOW"
        v = pol.get(tool)
        if isinstance(v, str) and v.strip().upper() in ("ALLOW", "DENY", "CONFIRM"):
            return v.strip().upper()
        return "ALLOW"

    def is_denied(self, tool: str) -> bool:
        return self.classify(tool) == "DENY"

    def request_confirm(self, tool: str) -> bool:
        """True when a CONFIRM-classified tool should stop and offer."""
        return self.classify(tool) == "CONFIRM"

    def confirm_seconds(self) -> float:
        try:
            s = float(self._settings.get("confirm_seconds", 90.0))
        except (TypeError, ValueError):
            s = 90.0
        return min(max(s, 5.0), 600.0)

    @staticmethod
    def is_desktop_action(tool: str) -> bool:
        """Tools that change the desktop (or spawn work) and thus honour
        dry-run mode."""
        return tool in ("run_command", "start_command", "open_app",
                        "close_window", "focus_window", "workspace",
                        "type_text", "press_keys", "press_hotkey", "scroll",
                        "click_element", "click_at", "copy_text",
                        "paste_text")


# ------------------------------------------------------------ bounded jobs


class BoundedJob:
    """A long-running whitelisted command with a hard cap and bounded output.

    `start_command` runs e.g. a test suite in the background, stores the
    Popen here, and `job_status` polls it. Everything is bounded: max jobs,
    output bytes, lifetime — so a runaway job cannot eat the machine."""

    MAX_JOBS = 4
    MAX_OUTPUT = 200_000
    MAX_LIFETIME_S = 1800.0

    def __init__(self, job_id: str, command: str, proc: subprocess.Popen) -> None:
        self.id = job_id
        self.command = command
        self.proc = proc
        self.started = time.monotonic()
        self._announced = False
        # ponytail: drain thread avoids pipe deadlock (child blocks at ~64k
        # when nobody reads). Bounded to MAX_OUTPUT; overflow is discarded
        # but still drained so the child never blocks.
        self._out_parts: list[str] = []
        self._out_len = 0
        self._out_lock = threading.Lock()
        # ponytail: drain-done event lets poll() join the drainer before a
        # reap, so job_status never reports a truncated tail for output that
        # already arrived (the pipe EOF races the process exit).
        self._drain_done = threading.Event()
        self._drain_thread: threading.Thread | None = None
        if proc.stdout is not None:
            self._drain_thread = threading.Thread(
                target=self._drain, name=f"drain-{job_id}", daemon=True)
            self._drain_thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.proc.stdout.read(8192)
                if not chunk:
                    break
                with self._out_lock:
                    if self._out_len < self.MAX_OUTPUT:
                        keep = chunk[: self.MAX_OUTPUT - self._out_len]
                        self._out_parts.append(keep)
                        self._out_len += len(keep)
        except (OSError, ValueError):
            pass
        finally:
            self._drain_done.set()

    def output_tail(self, limit: int = 4096) -> str:
        """Last `limit` chars of the RETAINED head (never blocks on the pipe).

        The buffer keeps the FIRST MAX_OUTPUT bytes of the job's output and
        discards the rest (still draining so the child never blocks) — so
        this is a tail of the head, not of unbounded full output. Callers
        must join the drain (via poll()) before reading a finished job.
        """
        with self._out_lock:
            s = "".join(self._out_parts)
        return s[-limit:] if len(s) > limit else s

    def _join_drain(self) -> None:
        """Wait (bounded) for the drainer to consume post-exit pipe bytes."""
        t = self._drain_thread
        if t is not None and t.is_alive() and not self._drain_done.is_set():
            t.join(timeout=5.0)

    def poll(self) -> tuple[str, bool]:
        """(state, done): 'running' | 'done' | 'timeout-killed'.

        Reaping goes through Popen.poll() ONLY: a raw waitpid here would
        reap the child behind Popen's back and returncode would stay None
        forever (a job that finished but never reads as finished)."""
        if self.proc.returncode is not None:
            self._join_drain()
            return "done", True
        if time.monotonic() - self.started > self.MAX_LIFETIME_S:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2)   # finalize returncode
            except Exception:
                pass
            self._join_drain()
            return "timeout-killed", True
        self.proc.poll()
        if self.proc.returncode is not None:
            self._join_drain()
            return "done", True
        return "running", False

    def status_text(self) -> str:
        state, done = self.poll()
        elapsed = time.monotonic() - self.started
        if state == "running":
            return f"job {self.id}: still running ({elapsed:.0f}s) — {self.command}"
        if state == "timeout-killed":
            return (f"job {self.id}: KILLED after {BoundedJob.MAX_LIFETIME_S:.0f}s "
                    f"(lifetime cap) — {self.command}")
        rc = self.proc.returncode
        return f"job {self.id}: finished, exit code {rc} ({elapsed:.0f}s) — {self.command}"


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


def _secure_file(path: Path) -> bool:
    """Make an existing runtime/config file owner-only, without creating it.

    ``Path.exists()`` is not sufficient here: it returns false for a broken
    symlink, which would let an attacker redirect a later atomic write. Use
    lstat first and reject every symlink, including broken ones. A live Unix
    socket is checked for ownership/mode but is not chmod-ed through a regular
    file path operation on platforms where that is unsupported.
    """
    try:
        if path.is_symlink():
            return False
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        if info.st_uid != os.getuid() or not (
                stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            return False
        if stat.S_ISREG(info.st_mode):
            path.chmod(0o600)
            info = path.stat()
        elif stat.S_ISSOCK(info.st_mode) and (info.st_mode & 0o077):
            # A stale socket from an earlier run under a permissive umask
            # must self-heal, not wedge startup forever: it is OUR file, so
            # tighten it in place (sockets accept chmod on Linux) instead of
            # failing _prepare_runtime until manual removal.
            path.chmod(0o600)
            info = path.stat()
        return ((info.st_mode & 0o077) == 0
                and info.st_uid == os.getuid())
    except OSError:
        return False


def _secure_runtime_files() -> bool:
    """Harden files that can contain secrets, transcripts, or control state.

    Deliberately no short-circuit: every file gets hardened even when an
    earlier one is bad, and the AND of the results is returned."""
    paths = (SETTINGS_FILE, HISTORY_FILE, MEMORY_FILE, CRASH_LOG,
             PENDING_FILE, LOCK_FILE, LOG_FILE, CONTROL_SOCK, MIC_EVENTS_FILE,
             REMINDERS_FILE)
    # materialize first: all(generator) short-circuits, which would leave
    # every file after a bad one unhardened
    return all([_secure_file(path) for path in paths])


def _atomic_private_write(path: Path, text: str) -> None:
    """Write a sensitive text file with mode 0600 and an atomic replacement.

    A unique temporary name prevents unrelated writers from swapping the same
    ``.tmp`` file, while the mode is set before the file becomes visible at
    its final path. The caller still owns any higher-level read/modify/write
    lock needed for its data structure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                    suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        if not _secure_file(path):
            raise OSError(f"refusing insecure runtime file: {path}")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _quarantine_bad(path: Path) -> None:
    """Move a corrupt config/state file aside; never fail-open on garbage."""
    try:
        if not path.exists():
            return
        # ponytail: pid suffix — %S timestamps collide when two processes
        # quarantine the same file within one second (second replace would
        # silently destroy the first bad copy).
        ts = time.strftime("%Y%m%d-%H%M%S")
        bad = path.with_name(f"{path.name}.bad-{ts}-{os.getpid()}")
        path.replace(bad)
        logging.getLogger("handsoff").warning(
            "corrupt %s quarantined to %s; using defaults", path, bad)
    except OSError:
        logging.getLogger("handsoff").warning(
            "corrupt %s could not be quarantined", path, exc_info=True)


def _prepare_runtime() -> bool:
    """Create the runtime roots privately and harden existing state files."""
    for directory in (CONFIG_DIR, STATE_DIR, WHISPER_MODEL_DIR, PIPER_VOICE_DIR):
        if not _private_dir(directory):
            return False
    return _secure_runtime_files()


def coerce_settings(s: dict) -> dict:
    """Coerce/validate raw merged settings IN PLACE. Shared by the bubble's
    _load_settings AND the settings app (a hand-edited settings.json must
    never crash either program; the settings app is the recovery tool and
    must open even when the config is garbage)."""
    log = logging.getLogger("handsoff")

    def _num(key: str, cast, lo, hi) -> None:
        """Coerce one numeric setting; on garbage, warn and use the default
        (a bad value must never kill startup or leak through unvalidated)."""
        try:
            s[key] = min(hi, max(lo, cast(s[key])))
        except (TypeError, ValueError):
            log.warning(
                "invalid %s — using default %r",
                key, DEFAULT_SETTINGS[key])
            s[key] = DEFAULT_SETTINGS[key]

    _num("num_ctx", int, 1024, 2 ** 20)
    _num("history_tokens", int, 0, 2 ** 20)     # 0 = auto (3/4 of num_ctx)
    _num("bubble_size", int, 96, 192)
    _num("mic_threshold", int, 50, 10_000)
    _num("tts_rate", float, 0.5, 2.0)
    _num("tts_volume", float, 0.1, 2.0)
    _num("max_tool_calls", int, 0, 10_000)
    _num("ram_alert_percent", float, 50.0, 99.0)
    _num("vram_alert_percent", float, 50.0, 99.0)
    _num("confirm_seconds", float, 5.0, 600.0)
    s["dry_run"] = bool(s.get("dry_run", False))
    _pol = s.get("command_policy")
    if isinstance(_pol, dict):
        s["command_policy"] = {
            str(k).strip(): str(v).strip().upper()
            for k, v in _pol.items()
            if str(k).strip() and str(v).strip().upper() in ("ALLOW", "DENY", "CONFIRM")
        }
    else:
        s["command_policy"] = {}
    s["resource_alerts"] = bool(s.get("resource_alerts", False))
    s["notification_reader"] = bool(s.get("notification_reader", False))
    _nm = s.get("notification_mute_apps", [])
    s["notification_mute_apps"] = ([str(x).strip().lower() for x in _nm if str(x).strip()]
                                    if isinstance(_nm, list) else [])[:32]
    s["handsfree"] = bool(s.get("handsfree", False))
    s["streaming_tts"] = bool(s.get("streaming_tts", True))
    s["wake_word_required"] = bool(s.get("wake_word_required", False))
    s["assistant_name"] = str(s.get("assistant_name", "assistant")).strip() or "assistant"
    _c = s.get("calendar_ics", [])
    if isinstance(_c, str):
        _c = [x.strip() for x in _c.replace(",", "\n").split("\n") if x.strip()]
    elif isinstance(_c, list):
        _c = [str(x).strip() for x in _c if str(x).strip()]
    else:
        _c = []
    s["calendar_ics"] = _c[:10]
    s["wake_spotter"] = bool(s.get("wake_spotter", False))
    s["mic_selfheal"] = bool(s.get("mic_selfheal", True))
    s["dictation"] = bool(s.get("dictation", True))
    _num("followup_seconds", float, 0.0, 120.0)   # 0 = feature off
    _sm = s.get("spotter_models", [])
    s["spotter_models"] = ([str(x).strip() for x in _sm if str(x).strip()]
                           if isinstance(_sm, list) else [])
    try:
        v = float(s.get("engage_seconds", 45.0))
    except (TypeError, ValueError):
        v = 45.0
    s["engage_seconds"] = min(600.0, max(5.0, v))
    aliases = s.get("workspace_aliases")
    s["workspace_aliases"] = (
        {str(k).strip().lower(): str(v).strip()
         for k, v in aliases.items() if str(k).strip() and str(v).strip()}
        if isinstance(aliases, dict) else {})
    s["home_place"] = str(s.get("home_place", "")).strip()
    s["briefing"] = bool(s.get("briefing", False))
    s["world_warnings"] = bool(s.get("world_warnings", False))
    _num("world_cooldown_min", float, 5.0, 1440.0)
    s["hardware_watch"] = bool(s.get("hardware_watch", False))
    _num("hardware_cooldown_min", float, 5.0, 1440.0)
    _num("hardware_disk_gb", float, 0.5, 1000.0)
    # ponytail: permissions fail-closed — garbage must never enable tools
    _perms = s.get("permissions")
    if not isinstance(_perms, dict):
        log.warning("invalid permissions — using defaults (fail-closed)")
        s["permissions"] = dict(DEFAULT_SETTINGS["permissions"])
    else:
        s["permissions"] = {str(k): bool(v) for k, v in _perms.items()
                            if str(k).strip()}
        for k, v in DEFAULT_SETTINGS["permissions"].items():
            s["permissions"].setdefault(k, bool(v))
    # ponytail: unvalidated enums — garbage must fall back to defaults loudly
    _host = str(s.get("ollama_host", "")).strip()
    if not _host:
        log.warning("invalid ollama_host — using default %r",
                    DEFAULT_SETTINGS["ollama_host"])
        _host = str(DEFAULT_SETTINGS["ollama_host"])
    s["ollama_host"] = _host
    _model = str(s.get("model", "")).strip()
    if not _model:
        log.warning("invalid model — using default %r",
                    DEFAULT_SETTINGS["model"])
        _model = str(DEFAULT_SETTINGS["model"])
    s["model"] = _model
    _WHISPER_SIZES = {"tiny", "base", "small", "medium", "large",
                      "large-v1", "large-v2", "large-v3", "turbo"}
    _ws = str(s.get("whisper_size", "")).strip().lower()
    if _ws not in _WHISPER_SIZES:
        log.warning("invalid whisper_size %r — using default %r",
                    s.get("whisper_size"), DEFAULT_SETTINGS["whisper_size"])
        _ws = str(DEFAULT_SETTINGS["whisper_size"])
    s["whisper_size"] = _ws
    if not isinstance(s.get("piper_voice"), str):
        log.warning("invalid piper_voice — using default %r",
                    DEFAULT_SETTINGS["piper_voice"])
        s["piper_voice"] = str(DEFAULT_SETTINGS["piper_voice"])
    else:
        s["piper_voice"] = str(s["piper_voice"]).strip()
    if not isinstance(s.get("mic_device"), str):
        log.warning("invalid mic_device — using default %r",
                    DEFAULT_SETTINGS["mic_device"])
        s["mic_device"] = str(DEFAULT_SETTINGS["mic_device"])
    _colors = s.get("colors")
    if not isinstance(_colors, dict):
        log.warning("invalid colors — using defaults")
        s["colors"] = dict(DEFAULT_SETTINGS["colors"])
    else:
        s["colors"] = {str(k): str(v) for k, v in _colors.items()
                       if k in DEFAULT_SETTINGS["colors"]
                       and isinstance(v, str) and str(v).strip()}
        for k, v in DEFAULT_SETTINGS["colors"].items():
            s["colors"].setdefault(k, v)
    if not isinstance(s["extra_allowed_commands"], list):
        s["extra_allowed_commands"] = []
    if not isinstance(s.get("tool_call_times"), (list, type(None))):
        s["tool_call_times"] = None
    return s


def _load_settings() -> dict:
    """Built-in defaults <- environment <- settings.json (the settings app wins)."""
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    env_map = {
        "ollama_host": "OLLAMA_HOST", "model": "HANDSOFF_MODEL",
        "num_ctx": "HANDSOFF_NUM_CTX", "whisper_size": "HANDSOFF_WHISPER",
        "piper_voice": "HANDSOFF_VOICE",
    }
    for key, var in env_map.items():
        if os.environ.get(var):
            s[key] = os.environ[var]
    try:
        raw = SETTINGS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return coerce_settings(s)
    except OSError:
        return coerce_settings(s)
    try:
        data = json.loads(raw)
    except ValueError:
        _quarantine_bad(SETTINGS_FILE)
        return coerce_settings(s)
    if not isinstance(data, dict):
        _quarantine_bad(SETTINGS_FILE)
        return coerce_settings(s)
    _slog = logging.getLogger("handsoff")
    for k, v in data.items():
        if k not in s:
            # ponytail: silent drops hide typos ("models:" never applies) —
            # warn so the user knows the key was ignored.
            _slog.warning("unknown settings key %r — ignored", k)
            continue
        if isinstance(s[k], dict) and isinstance(v, dict):
            s[k].update(v)
        else:
            s[k] = v
    return coerce_settings(s)


def _settings_file_lock():
    """Cross-process settings lock (flock on a sidecar, not the data file)."""
    import contextlib

    @contextlib.contextmanager
    def _lock():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(CONFIG_DIR / "settings.json.lock", "w")
        try:
            # ponytail: open() honors umask (often 0644) — force owner-only.
            try:
                os.chmod(CONFIG_DIR / "settings.json.lock", 0o600)
            except OSError:
                pass
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
    return _lock()


def _persist_setting(key: str, value) -> None:
    """Persist one runtime setting without overwriting unrelated settings."""
    with _SETTINGS_WRITE_LOCK, _settings_file_lock():
        data = {}
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
            else:
                _quarantine_bad(SETTINGS_FILE)
        except FileNotFoundError:
            pass
        except ValueError:
            _quarantine_bad(SETTINGS_FILE)
        except OSError:
            pass
        data[key] = value
        # NOTE: _atomic_private_write creates its own uniquely-named temp
        # file; a pre-computed ".json.tmp" path here would reintroduce the
        # predictable-name race that helper exists to prevent.
        _atomic_private_write(
            SETTINGS_FILE, json.dumps(data, ensure_ascii=False, indent=1))
        SETTINGS[key] = value
        reload_derived_settings()


def set_setting(key: str, value) -> None:
    """Single entry point for every SETTINGS mutation.

    Takes the in-process lock AND the cross-process file lock (via
    _persist_setting's read-merge-write), refreshes the in-memory copy and
    derived globals — no caller may use a raw ``SETTINGS[k] = v`` write and
    bypass the lock. Updates SETTINGS first so a mocked _persist_setting
    (tests) still leaves the in-memory state honest.
    """
    SETTINGS[key] = value
    _persist_setting(key, value)


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
PIPER_VOICE_NAME = SETTINGS["piper_voice"]


def reload_derived_settings() -> None:
    """Refresh derived globals after a model/ctx/host change.

    Frozen OLLAMA_* / token-budget / tool-support state otherwise survives a
    settings save until restart. Resets the prompt-token cache and re-arms
    tool-support probing when the model changes.
    """
    global OLLAMA_BASE, OLLAMA_MODEL, OLLAMA_NUM_CTX
    global WHISPER_SIZE, PIPER_VOICE_NAME
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
    PIPER_VOICE_NAME = SETTINGS.get("piper_voice", PIPER_VOICE_NAME)

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
WINDOW_PX = SETTINGS["bubble_size"]   # transparent window; bubble is ~69% of it
BUBBLE_R0 = WINDOW_PX * 44.0 / 128.0  # idle bubble radius
GLOW_PAD = WINDOW_PX * 7.0 / 128.0    # glow ring thickness; fits inside the mask
GEOM_K = WINDOW_PX / 128.0            # scale for all radius offsets
LOCK_RETRIES = 40                     # lock wait on restart: 40 x 0.25s = 10s
LOCK_RETRY_WAIT = 0.25

log = logging.getLogger("handsoff")


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        from logging.handlers import RotatingFileHandler
        # 1 MB per file, 2 rotations: the state dir can't grow forever
        handlers.append(RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=2, delay=True))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


_NOTIFY_DELAY = 0.5        # batch window: repeats inside it fold into one popup
_NOTIFY_BURST = 8          # distinct popups per batch before summarizing
_NOTIFY_MAX_PENDING = 64   # distinct texts kept per batch (rest → overflow)
_NOTIFY_LOCK = threading.Lock()
_NOTIFY_STATE: dict = {"pending": {}, "overflow": 0, "timer": None}

_READER_APP_COOLDOWN = 60.0  # one spoken digest per app per minute, max
_READER_COOLDOWN_LOCK = threading.Lock()
_READER_APP_LAST: dict[str, float] = {}


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
Only these programs are allowed: pactl, playerctl, brightnessctl, niri, spawn, echo, cat, ls, pwd, notify-send, read-only system probes (ps, free, uptime, df, ss, nvidia-smi), read-only git (git status, git diff, git log, git show, git branch, git remote, git stash), cargo builds/tests (cargo build, cargo check, cargo test, cargo clippy), and your restart script {RESTART_SCRIPT}.{_EXTRAS_NOTE}
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
- edit_file(path, content): replace the whole content of a file. You may only write your own source file and files inside {CONFIG_DIR}/ .

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
- You ARE the program: your entire source is the single Python file {SELF_PATH}.
- When the user asks you to change or extend yourself ("make your bubble pink", "add a mute option", "speak faster"):
  1) read_file {SELF_PATH},
  2) edit_file {SELF_PATH} with the complete updated file — valid Python, minimal changes, keeping the marker line '{SELF_MARKER}' exactly as-is,
  3) reply with one short sentence confirming the change, then run_command "{RESTART_SCRIPT}" to restart into your new self.
- For simple settings (volume, brightness, window control) just use commands; do not rewrite yourself."""

def build_tools() -> list[dict]:
    """Generate the Ollama tool schema list from every @tool method.

    Single source of truth: the ToolBelt methods themselves. Signature gives
    names/types/required, the first docstring paragraph is the model-facing
    description, and `param: text` docstring lines become param descriptions.
    """
    out = []
    belt_attrs = [a for a in vars(ToolBelt).values()
                  if callable(a) and getattr(a, "_is_tool", False)]
    for fn in belt_attrs:
        params = _param_schema(fn)
        req = fn._tool_required
        if req is None:
            # params without defaults are required
            sig = inspect.signature(fn)
            req = [p for p, info in params.items()
                   if sig.parameters[p].default is inspect.Parameter.empty]
        out.append({
            "type": "function",
            "function": {
                "name": fn._tool_name,
                "description": (fn._tool_description
                                or (inspect.getdoc(fn) or "").split("\n\n")[0].strip()),
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": req,
                },
            },
        })
    return out


# TOOLS is instantiated right after the ToolBelt class body (it needs the
# decorated methods to exist).

# ------------------------------------------------------------------- ollama client


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=3) as r:
            json.loads(r.read().decode("utf-8"))
        return True
    except Exception:
        return False


_TOOLS_SUPPORTED = True  # flipped off permanently if the model can't do tool calls


def ollama_chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """One /api/chat round trip. Raises RuntimeError with a human-readable cause."""
    global _TOOLS_SUPPORTED
    if tools and not _TOOLS_SUPPORTED:
        tools = None
    payload: dict = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        # keep the model resident: without this Ollama unloads it after 5 min
        # idle and the next question pays a ~90 s reload (measured).
        "keep_alive": os.environ.get("HANDSOFF_KEEP_ALIVE", "1h"),
        "options": {"temperature": 0.3, "num_ctx": OLLAMA_NUM_CTX},
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        OLLAMA_BASE + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("message") or {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = e.reason
        if e.code == 400 and tools and "tool" in detail.lower():
            _TOOLS_SUPPORTED = False
            log.warning("model %s does not support tools; continuing without", OLLAMA_MODEL)
            return ollama_chat(messages, None)
        if e.code == 404 and "model" in str(detail).lower():
            detail += f" — run: ollama pull {OLLAMA_MODEL}"
        raise RuntimeError(f"Ollama error {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"cannot reach Ollama at {OLLAMA_BASE} ({e.reason}). "
            "Start it with: systemctl start ollama"
        ) from None


def strip_thinking(text: str) -> str:
    """Clean model output for speaking: strip <think> blocks and the
    `[TOOL_CALLS]{...}` template junk some models prepend."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    t = re.sub(r"^\s*\[TOOL_CALLS\][^\n]*(?:\n|$)", "", t, flags=re.MULTILINE)
    return t.strip()


def ollama_chat_stream(messages: list[dict], q: "queue.Queue[str | None]",
                       cancel: threading.Event | None = None,
                       tools: list[dict] | None = None) -> list[dict]:
    """Stream /api/chat, pushing cleaned sentence chunks (then None) into `q`.

    Complete tool calls seen in the stream are collected and returned, so
    the caller can keep the tool loop alive while speaking content as it
    arrives. On a 400 'tools unsupported' error, retries once without tools.
    Any error pushes None after emitting what arrived so far."""
    global _TOOLS_SUPPORTED
    if tools and not _TOOLS_SUPPORTED:
        tools = None
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "think": False,
        "keep_alive": os.environ.get("HANDSOFF_KEEP_ALIVE", "1h"),
        "options": {"temperature": 0.3, "num_ctx": OLLAMA_NUM_CTX},
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        OLLAMA_BASE + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    buf = ""
    full = ""
    tool_calls: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                msg_chunk = chunk.get("message") or {}
                tc = msg_chunk.get("tool_calls")
                if tc:
                    tool_calls.extend(tc)
                piece = msg_chunk.get("content") or ""
                if piece:
                    buf += piece
                    full += piece
                    # speak complete sentences as soon as they arrive
                    while True:
                        m = re.search(r"[.!?…](\s|$)", buf)
                        if not m:
                            break
                        sentence, buf = buf[: m.end()], buf[m.end():]
                        sentence = strip_thinking(sentence).strip()
                        if sentence and not sentence.startswith("<"):
                            q.put(sentence)
        tail = strip_thinking(buf).strip()
        if tail and not tail.startswith("<"):
            q.put(tail)
    except urllib.error.HTTPError as e:
        if e.code == 400 and tools and "tool" in _read_http_error(e).lower():
            _TOOLS_SUPPORTED = False
            log.warning("model %s does not support tools; continuing without", OLLAMA_MODEL)
            return ollama_chat_stream(messages, q, cancel, None)
        # ponytail: never swallow — the caller must speak the failure and
        # must not append an empty assistant turn as if the model replied.
        detail = _read_http_error(e)
        log.error("streaming chat HTTP error: %s", e)
        raise RuntimeError(f"Ollama error {e.code}: {detail}") from None
    except Exception:
        log.exception("streaming chat failed")
        raise
    finally:
        q.put(None)
    return {"tool_calls": tool_calls, "content": strip_thinking(full).strip()}


def _read_http_error(e: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(e.read().decode("utf-8")).get("error", ""))
    except Exception:
        return str(e.reason)


# ------------------------------------------------------------------------ audio in


def _resample_to_16k(data: np.ndarray, rate: int) -> np.ndarray:
    """Resample flat int16 audio to SAMPLE_RATE (linear interp, speech-grade).

    Some input devices (e.g. Logitech StreamCam) cannot capture at 16 kHz at
    all; they are opened at their native rate and every frame is converted
    here so the pipeline always sees 16 kHz audio."""
    if rate == SAMPLE_RATE or data.size == 0:
        return data
    # ponytail: chunked interp — one giant linspace pair is 2x float64
    # copies of the whole capture; 480k-sample chunks bound the peak.
    out: list[np.ndarray] = []
    _CH = 480_000
    ratio = SAMPLE_RATE / float(rate)
    for off in range(0, data.size, _CH):
        seg = data[off: off + _CH]
        duration = seg.size / float(rate)
        target_n = max(1, int(round(seg.size * ratio)))
        x_old = np.linspace(0.0, duration, num=seg.size, endpoint=False)
        x_new = np.linspace(0.0, duration, num=target_n, endpoint=False)
        out.append(np.interp(x_new, x_old, seg.astype(np.float32)).astype(np.int16))
    return np.concatenate(out) if out else data[:0]


def _open_input(device, rate: int, blocksize: int, cb) -> tuple:
    """Open a mono int16 InputStream at `rate`; if the device rejects that
    rate (PortAudio 'Invalid sample rate'), retry once at the device's
    native default rate. Returns (stream, actual_rate)."""
    try:
        return sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                              blocksize=blocksize, callback=cb, device=device), rate
    except Exception:
        try:
            info = (sd.query_devices(device, kind="input") if device is not None
                    else sd.query_devices(kind="input"))
            native = int(float(info.get("default_samplerate") or rate))
        except Exception:
            native = rate
        if native == rate:
            raise
        stream = sd.InputStream(samplerate=native, channels=1, dtype="int16",
                                blocksize=blocksize, callback=cb, device=device)
        return stream, native


class Recorder:
    """16 kHz mono int16 microphone capture with live RMS levels."""

    MAX_PTT_S = 60.0  # ponytail: PTT is bounded — a stuck press can't OOM

    def __init__(self, on_level: "callable", device: str | None = None,
                 threshold: int = 600) -> None:
        self._on_level = on_level
        self._device = device or None
        self._threshold = threshold
        self._frames: list[np.ndarray] = []
        self._samples = 0
        self._level = 0.0
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        self._frames = []
        self._samples = 0
        # devices that can't capture at 16 kHz (StreamCam…) are opened at
        # their native rate; stop() resamples everything to 16 kHz for STT
        self._stream, self._native_rate = _open_input(
            self._device, SAMPLE_RATE, 1024, self._cb)
        self._stream.start()

    def _cb(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("audio input: %s", status)
        self._frames.append(indata.copy())
        self._samples += int(indata.size)
        # ponytail: drop oldest frames past the 60s cap (by native rate)
        cap = int(self.MAX_PTT_S * float(getattr(self, "_native_rate", SAMPLE_RATE)))
        while self._frames and self._samples > cap:
            old = self._frames.pop(0)
            self._samples -= int(old.size)
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        self._level = 0.25 * rms + 0.75 * self._level
        try:
            self._on_level(min(1.0, self._level / 2000.0))
        except Exception:
            pass

    def stop(self) -> np.ndarray | None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.exception("failed to close input stream")
        if not self._frames:
            return None
        audio = np.concatenate(self._frames).reshape(-1)
        return _resample_to_16k(audio, getattr(self, "_native_rate", SAMPLE_RATE))


# ------------------------------------------------------------------------ STT / TTS

_whisper_model = None
_whisper_lock = threading.Lock()
_piper_voice = None
_piper_lock = threading.Lock()


def get_whisper():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel  # lazy: heavy import

            log.info("loading whisper '%s' from %s", WHISPER_SIZE, WHISPER_MODEL_DIR)
            _whisper_model = WhisperModel(
                WHISPER_SIZE, device="cpu", compute_type="int8",
                download_root=str(WHISPER_MODEL_DIR),
                local_files_only=True,  # model is cached; never block startup on HF network
            )
        return _whisper_model


def get_piper():
    global _piper_voice
    with _piper_lock:
        if _piper_voice is None:
            import piper  # lazy: heavy import

            onnx = (
                PIPER_VOICE_DIR / PIPER_VOICE_NAME
                if PIPER_VOICE_NAME
                else next(iter(sorted(PIPER_VOICE_DIR.glob("*.onnx"))), None)
            )
            if onnx is None or not onnx.exists():
                raise FileNotFoundError(
                    f"no piper voice (*.onnx) in {PIPER_VOICE_DIR} — run install.sh"
                )
            log.info("loading piper voice %s", onnx.name)
            try:
                _piper_voice = piper.PiperVoice.load(str(onnx), config_path=str(onnx) + ".json")
            except TypeError:  # very old piper builds without config_path
                _piper_voice = piper.PiperVoice.load(str(onnx))
        return _piper_voice



def transcribe(audio_int16: np.ndarray) -> str:
    segments, _info = get_whisper().transcribe(
        audio_int16.astype(np.float32) / 32768.0, vad_filter=True,
        language="en", beam_size=1,
    )
    return " ".join(s.text.strip() for s in segments).strip()


def tts_to_wav(text: str, wav_path: Path) -> None:
    voice = get_piper()
    with wave.open(str(wav_path), "wb") as w:
        if hasattr(voice, "synthesize_wav"):      # piper-tts >= 1.2
            cfg = None
            try:
                from piper import SynthesisConfig
                cfg = SynthesisConfig(
                    length_scale=1.0 / float(SETTINGS["tts_rate"]),
                    volume=float(SETTINGS["tts_volume"]),
                )
            except Exception:
                cfg = None
            voice.synthesize_wav(text, w, syn_config=cfg)
        else:                                     # older API
            voice.synthesize(text, w)


def play_wav(path: Path, cancel: threading.Event) -> None:
    """Blocking playback; returns early if `cancel` is set (barge-in)."""
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]
    stream = sd.OutputStream(samplerate=sr, channels=1, dtype="int16", blocksize=1024)
    stream.start()
    try:
        for i in range(0, len(data), 4096):
            if cancel.is_set():
                break
            stream.write(data[i : i + 4096].reshape(-1, 1))
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


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
    """{norm_key: epoch} of spoken/announced events; corrupt → {}."""
    try:
        data = json.loads(WORLD_EVENTS_FILE.read_text(encoding="utf-8"))
        now = time.time()
        return {str(k): float(v) for k, v in data.items()
                if str(k).strip() and float(v) > now - WORLD_EVENTS_TTL_S}
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


def tool(func=None, *, name=None, gates=None, aliases=None, description=None,
         required=None):
    """Mark a ToolBelt method as an AI-callable tool.

    The Ollama JSON schema is generated automatically from the function's
    signature, type hints and docstring — so one tool is ONE function:

        @tool(gates="press_keys", aliases={"combo": ("keys", "key")})
        def my_tool(self, combo: str) -> str:
            # docstring: "Fire a desktop/compositor shortcut like 'Mod+E'."
            # then 'combo: e.g. Mod+E' lines become param descriptions

    gates:       permission key required (defaults to the tool's own name;
                 pass "" for no gate)
    aliases:     {param: (alt names...)} small models sometimes emit
    description: pinned model-facing description (default: docstring para 1)
    required:    override required-params list (default: params w/o defaults)
    """
    def wrap(f):
        f._is_tool = True
        f._tool_name = name or f.__name__
        f._tool_gates = gates if gates is not None else f._tool_name
        f._tool_aliases = dict(aliases or {})
        f._tool_description = description
        f._tool_required = required
        return f
    return wrap(func) if func else wrap


_JSON_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _param_schema(func) -> dict:
    """{param: {'type': ..., 'description': ...}} from signature + docstring."""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    params = {}
    for line in doc.splitlines():
        m = re.match(r"^(\w+):\s+(.+)$", line.strip())
        if m:
            params[m.group(1)] = m.group(2).strip()
    out = {}
    for pname, p in sig.parameters.items():
        if pname in ("self",):
            continue
        hint = p.annotation if p.annotation is not inspect.Parameter.empty else str
        if isinstance(hint, str):   # `from __future__ import annotations`
            hint = {"bool": bool, "int": int, "float": float,
                    "str": str}.get(hint, str)
        typ = _JSON_TYPE.get(hint if hint in _JSON_TYPE else str, "string")
        entry = {"type": typ}
        if pname in params:
            entry["description"] = params[pname]
        out[pname] = entry
    return out



# -- reminders: persisted, epoch-based, survive restarts --------------------------

REMINDERS_FILE = STATE_DIR / "reminders.json"
REMINDERS_LOCK = threading.RLock()   # serializes EVERY read-modify-write
MAX_REMINDERS = 64
MAX_REMIND_DAYS = 365


def _load_reminders() -> list[dict]:
    """Read reminders.json, dropping entries that are malformed."""
    try:
        data = json.loads(REMINDERS_FILE.read_text())
        if not isinstance(data, list):
            return []
        out = []
        for r in data:
            if not isinstance(r, dict) or not isinstance(r.get("name"), str) \
                    or not isinstance(r.get("due"), (int, float)):
                continue
            try:
                r["repeat_hours"] = float(r.get("repeat_hours") or 0)
            except (TypeError, ValueError):
                r["repeat_hours"] = 0.0
            out.append(r)
        return out
    except (OSError, ValueError):
        return []


def _save_reminders(items: list[dict]) -> None:
    """Caller MUST hold REMINDERS_LOCK: the tmp filename is fixed, so two
    concurrent writers would corrupt each other's swap."""
    _atomic_private_write(REMINDERS_FILE, json.dumps(items, indent=1))


def _update_reminders(mutate) -> list[dict]:
    """One serialized read-modify-write transaction: load → mutate → save,
    all under REMINDERS_LOCK. Every reminder mutation goes through this."""
    with REMINDERS_LOCK:
        items = _load_reminders()
        items = mutate(items) or items
        _save_reminders(items)
        return items


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


_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")

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


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December")


def _ics_fetch(source: str) -> str:
    """Read an ICS calendar from an https URL or a local file path."""
    if re.match(r"^https?://", source, re.I):
        return _http_get(source, timeout=15).decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")


def _ics_unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a continuation line starts with space/tab."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_parse_dt(prop: str) -> "datetime.datetime | None":
    """Parse a DTSTART/DTEND property → local datetime (None on garbage).

    Handles 'YYYYMMDDTHHMMSSZ' (UTC), ';TZID=…' (zoneinfo), floating local
    time, and all-day 'VALUE=DATE' / 8-digit dates (local midnight)."""
    if ":" not in prop:
        return None
    head, _, value = prop.partition(":")
    value = value.strip()
    params = dict(p.split("=", 1) for p in head.split(";")[1:] if "=" in p)
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.datetime.strptime(value[:8], "%Y%m%d").astimezone()
        dt = datetime.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        elif (tzid := params.get("TZID")):
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo(tzid))
        return dt.astimezone()
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return None


def _ics_allday(prop: str) -> bool:
    if ":" not in prop:
        return False
    head, _, value = prop.partition(":")
    if "VALUE=DATE" in head:
        return True
    return bool(re.fullmatch(r"\d{8}", value.strip()))


def _ics_expand(dtstart: "datetime.datetime", rrule: str,
                win_start: "datetime.datetime", win_end: "datetime.datetime",
                dur: "datetime.timedelta") -> list["datetime.datetime"]:
    """Basic RRULE expansion: DAILY and WEEKLY (with INTERVAL/BYDAY/COUNT).
    Anything else (MONTHLY, YEARLY, BYSETPOS…) falls back to the single
    occurrence — honest limitation, not silent data loss."""
    one = [dtstart] if dtstart < win_end and dtstart + dur > win_start else []
    if not rrule:
        return one
    parts = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = (parts.get("FREQ") or "").upper()
    try:
        interval = max(1, int(parts.get("INTERVAL") or 1))
        count = int(parts.get("COUNT") or 500)
    except ValueError:
        return one
    out: list = []
    if freq == "DAILY":
        cur, i = dtstart, 0
        while cur < win_end and i < count:
            if cur + dur > win_start:
                out.append(cur)
            cur += datetime.timedelta(days=interval)
            i += 1
    elif freq == "WEEKLY":
        wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        days = [wd[d] for d in parts.get("BYDAY", "").split(",") if d in wd]             or [dtstart.weekday()]
        week0 = dtstart - datetime.timedelta(days=dtstart.weekday())
        w = 0
        while w < 200 and len(out) < count:
            base = week0 + datetime.timedelta(weeks=w * interval)
            if base > win_end:
                break
            for d in days:
                t = base + datetime.timedelta(
                    days=d, hours=dtstart.hour, minutes=dtstart.minute,
                    seconds=dtstart.second)
                if t < win_end and t + dur > win_start:
                    out.append(t)
            w += 1
    else:
        return one
    return out


def _ics_events_from_text(text: str, win_start: "datetime.datetime",
                          win_end: "datetime.datetime") -> list[dict]:
    """Parse VEVENTs overlapping [win_start, win_end); expands recurrences.

    Handles the two RFC 5545 overlap rules that calendars (Google, Nextcloud)
    actually emit:
    - EXDATE lines cancel specific instances of the recurrence;
    - a later VEVENT with a RECURRENCE-ID is a per-instance override of the
      master event sharing its UID: STATUS:CANCELLED removes that instance,
      anything else (moved time, new summary) replaces it.
    Overrides whose UID has no master in the file degrade to standalone
    events (their RECURRENCE-ID then equals their own start — harmless)."""
    if win_start.tzinfo is None:
        win_start = win_start.astimezone()
    if win_end.tzinfo is None:
        win_end = win_end.astimezone()

    props = ("SUMMARY", "LOCATION", "DTSTART", "DTEND", "RRULE",
             "EXDATE", "RECURRENCE-ID", "UID", "STATUS")
    raws: list[dict] = []          # every VEVENT in file order
    cur: dict | None = None
    for ln in _ics_unfold(text):
        if ln.strip() == "BEGIN:VEVENT":
            cur = {"EXDATE": []}
        elif ln.strip() == "END:VEVENT":
            if cur:
                raws.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            name = ln.split(":", 1)[0].split(";")[0].strip().upper()
            if name in props:
                if name == "EXDATE":
                    cur["EXDATE"].append(ln)   # repeated lines accumulate
                elif name not in cur:
                    # date/rrule/id fields keep the FULL line (TZID/VALUE
                    # params matter for parsing); text fields keep the value
                    cur[name] = ln if name in ("DTSTART", "DTEND", "RRULE",
                                               "RECURRENCE-ID") \
                        else ln.split(":", 1)[1].strip()

    # masters: VEVENTs without RECURRENCE-ID (overrides are per-instance and
    # never the recurrence itself); drop duplicate-UID re-exports
    masters: list[dict] = []
    seen_uids: set[str] = set()
    for e in raws:
        if e.get("RECURRENCE-ID"):
            continue
        uid = e.get("UID")
        if uid:
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
        masters.append(e)

    # overrides keyed by UID: instance datetime → cancellation / replacement
    cancelled: dict[str, set] = {}
    moved: dict[str, dict] = {}
    for e in raws:
        when = _ics_parse_dt(e.get("RECURRENCE-ID", ""))
        if when is None:
            continue
        uid = e.get("UID", "")
        if (e.get("STATUS") or "").strip().upper() == "CANCELLED":
            cancelled.setdefault(uid, set()).add(when)
        else:
            moved.setdefault(uid, {})[when] = e

    events: list[dict] = []
    for e in masters:
        ds = _ics_parse_dt(e.get("DTSTART", ""))
        if ds is None:
            continue
        de = _ics_parse_dt(e.get("DTEND", "")) or ds
        dur = de - ds
        if dur <= datetime.timedelta(0):
            dur = datetime.timedelta(hours=1)
        rrule = (e.get("RRULE") or "").split(":", 1)[-1]

        # instances to suppress: EXDATEs, overridden/canceled RECURRENCE-IDs.
        # The EXDATE head params (e.g. TZID) apply to every comma value.
        uid = e.get("UID", "")
        skip = set(cancelled.get(uid, ()))
        skip.update(moved.get(uid, {}).keys())
        for xl in e.get("EXDATE", []):
            head, sep, values = xl.partition(":")
            for piece in values.split(","):
                piece = piece.strip()
                if piece:
                    xd = _ics_parse_dt(f"{head}:{piece}" if sep else piece)
                    if xd is not None:
                        skip.add(xd)

        for start in _ics_expand(ds, rrule, win_start, win_end, dur):
            if start in skip:
                continue
            events.append({
                "start": start, "dur": dur,
                "summary": e.get("SUMMARY") or "(no title)",
                "location": e.get("LOCATION", ""),
                "allday": _ics_allday(e.get("DTSTART", "")),
            })

    # moved overrides surface at their NEW time (if inside the window)
    for bywhen in moved.values():
        for ov in bywhen.values():
            ods = _ics_parse_dt(ov.get("DTSTART", ""))
            if ods is None or not (win_start <= ods < win_end):
                continue
            ode = _ics_parse_dt(ov.get("DTEND", "")) or ods
            odur = ode - ods
            if odur <= datetime.timedelta(0):
                odur = datetime.timedelta(hours=1)
            events.append({
                "start": ods, "dur": odur,
                "summary": ov.get("SUMMARY") or "(no title)",
                "location": ov.get("LOCATION", ""),
                "allday": _ics_allday(ov.get("DTSTART", "")),
            })
    return events


def _fmt_events(events: list[dict]) -> str:
    """'Mon 07 Sep 09:00–10:00: Team sync @ Teams' — locale-independent."""
    lines = []
    for e in sorted(events, key=lambda x: x["start"])[:40]:
        s = e["start"]
        day = (f"{_DAY_NAMES[s.weekday()][:3]} {s.day:02d} "
               f"{_MONTH_NAMES[s.month - 1][:3]}")
        when = "all day" if e["allday"] else f"{s.hour:02d}:{s.minute:02d}"
        if not e["allday"] and e["dur"] > datetime.timedelta(0):
            end = s + e["dur"]
            if end.date() == s.date():
                when += f"\u2013{end.hour:02d}:{end.minute:02d}"
        loc = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"{day} {when}: {e['summary']}{loc}")
    return "; ".join(lines)


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
    """Split into (fired, kept). Repeats advance in whole repeat-steps so a
    sleep/restart never loses one, and never machine-guns a backlog."""
    fired, kept = [], []
    for r in items:
        if r["due"] > now:
            kept.append(r)
            continue
        fired.append(r)
        step = float(r.get("repeat_hours") or 0) * 3600
        if step > 0:
            ahead = r["due"] + max(1, int((now - r["due"]) // step) + 1) * step
            kept.append({**r, "due": ahead})
    return fired, kept


def _take_missed_reminders() -> list[dict]:
    """Pop reminders that came due while we were off (startup call)."""
    with REMINDERS_LOCK:
        items = _load_reminders()
        if not items:
            return []
        fired, kept = _due_reminders(items, time.time())
        if fired:
            try:
                _save_reminders(kept)
            except OSError:
                log.exception("could not prune fired reminders")
    return fired


# -- snooze: while this offer is live, a bare "snooze" re-arms the just-fired
# one-off reminder without any LLM round-trip
_snooze_offer: dict = {}        # {"name": str, "until": time.monotonic()}
_SNOOZE_LOCK = threading.Lock()
_kill_offer: dict = {}          # pending kill confirmation: {"pid", "name", "until"}
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


class ToolBelt:
    """The assistant's hands: one safe shell command, file read, file edit."""

    ALLOWED = {
        "pactl", "playerctl", "brightnessctl", "niri", "spawn", "echo",
        "cat", "ls", "pwd", "notify-send",
        # read-only system probes: answer "what's eating my CPU/RAM/disk/GPU"
        "ps", "free", "uptime", "df", "ss", "nvidia-smi",
    }
    # git/cargo are allowed ONLY with read-only (or explicitly-safe) verbs —
    # they stay in BLOCKED for every other use, so a bare 'git' or 'cargo'
    # (or e.g. 'git push') is still refused.
    _CARGO_OK = {"build", "check", "test", "clippy"}
    _GIT_READ = {"status", "diff", "log", "show", "branch", "remote"}
    _GIT_DELETE_FLAGS = {"d", "D", "delete"}   # 'git branch -D x' mutates
    BLOCKED = (
        "sudo", "rm", "pacman", "yay", "paru", "shutdown", "poweroff", "reboot",
        "halt", "mkfs", "dd", "kill", "chmod", "chown", "mount", "umount",
        "curl", "wget", "bash", "sh", "zsh", "fish", "python", "python3",
        "pip", "mv", "cp", "tar", "zip", "7z", "make", "gcc",
        "systemctl", "journalctl", "tee", "xargs", "env", "eval", "exec",
    )
    MAX_READ = 160_000
    MAX_WRITE = 2_000_000
    TIMEOUT = 15

    def __init__(self, on_restart_pending: "callable",
                 permissions: dict | None = None,
                 on_timer: "callable | None" = None,
                 on_notification: "callable | None" = None,
                 on_announce: "callable | None" = None,
                 on_pomodoro: "callable | None" = None) -> None:
        self._on_restart_pending = on_restart_pending
        self._on_timer = on_timer
        self._on_notification = on_notification
        self._on_announce = on_announce
        self._on_pomodoro = on_pomodoro
        self._policy = DecisionPolicy()
        # bounded background jobs (start_command): job_id -> BoundedJob
        self._jobs: dict[str, BoundedJob] = {}
        self._job_seq = 0
        self._job_lock = threading.Lock()
        # pending CONFIRM offers (confirm_action): {tool, until}
        self._pending_confirm: dict | None = None
        self._confirm_running: str | None = None   # bypass while running the confirmed call
        self._last_images: list[str] = []
        # operator state: OCR element scan freshness + focused-output pointer
        # scale (screen pixels / scale = pointer coordinates); refreshed by
        # every screen_elements scan, invalidated by anything that changes
        # the screen (_mark_elements_stale)
        self._elements_ts: float = 0.0
        self._pointer_scale: float = 1.0
        self._watch_lock = threading.RLock()
        self._file_watchers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._process_watchers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        # screenshots are attached to the next tool result via _last_images
        self._tool_times: deque[float] = deque(maxlen=60)   # rate-limit window
        self._perm = {
            "run_command": True, "read_file": True,
            "edit_file": True, "self_restart": True, **(permissions or {}),
        }

    # -- public dispatch -----------------------------------------------------

    def _announce_job(self, text: str) -> None:
        """Speak a job completion through the assistant's announcement path
        (same channel as watcher alerts); a bare ToolBelt without one logs."""
        if self._on_announce is not None:
            try:
                self._on_announce(text)
            except Exception:
                log.exception("job announcement failed")
        else:
            log.info("%s", text)

    def _tool_methods(self) -> dict:
        """{tool name: bound method} for every @tool-decorated method."""
        cache = getattr(self, "_tool_cache", None)
        if cache is None:
            cache = {}
            for attr in dir(self):
                fn = getattr(self, attr, None)
                if callable(fn) and getattr(fn, "_is_tool", False):
                    cache[fn._tool_name] = fn
            self._tool_cache = cache
        return cache

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        self._last_images = []
        # rate limiting: bounded tool calls per 60s window (runaway-loop guard)
        limit = int(SETTINGS.get("max_tool_calls") or 0)
        now = time.monotonic()
        while self._tool_times and now - self._tool_times[0] > 60:
            self._tool_times.popleft()
        if limit > 0:
            if len(self._tool_times) >= limit:
                log_decision(name, json.dumps(args)[:120], "RATE-LIMITED",
                             "refused: rate limit")
                return (f"REFUSED: tool-call rate limit reached ({limit} calls/60s) — "
                        "stop calling tools, answer from what you have, or wait"), True
            self._tool_times.append(now)
        fn = self._tool_methods().get(name)
        if fn is None:
            return f"unknown tool: {name}", True
        # permission gate declared on the tool itself
        gate = fn._tool_gates
        if gate and not self._perm.get(gate, True):
            log_decision(name, json.dumps(args)[:120], "DENY",
                         "refused: permission gate disabled")
            return f"REFUSED: the '{gate}' tool is disabled in handsoff settings", True
        # centralized policy: ALLOW / DENY / CONFIRM (one-turn separation).
        # kill_process/confirm_kill manage their own two-step confirm and stay
        # out of this path.
        if name in ("kill_process", "confirm_kill"):
            verdict = "ALLOW"
        else:
            verdict = self._policy.classify(name)
        target = json.dumps(args, sort_keys=True)[:200] if args else ""
        if verdict == "DENY":
            log_decision(name, target, "DENY", "refused: command_policy DENY")
            return (f"REFUSED: '{name}' is DENIED by the user's command policy "
                    "(handsoff settings) — do not retry this turn"), True
        if verdict == "CONFIRM" and getattr(self, "_confirm_running", None) != name:
            # Strict one-turn separation: ONLY confirm_action('yes') runs a
            # CONFIRM-class call. A repeated direct call never executes — it
            # just re-surfaces the standing offer (fail-safe: the worst case
            # is the user hearing the offer twice).
            if (self._pending_confirm is None
                    or self._pending_confirm.get("tool") != name):
                self._pending_confirm = {
                    "tool": name, "args": dict(args),
                    "until": time.monotonic() + self._policy.confirm_seconds(),
                }
                log_decision(name, target, "CONFIRM", "offered; awaiting confirm_action")
            else:
                log_decision(name, target, "CONFIRM", "still awaiting confirm_action")
            return (f"CONFIRM REQUIRED: about to call '{name}' with {target or 'no arguments'}. "
                    "Nothing happened yet. The user must hear this offer and "
                    "reply; call confirm_action(answer='yes') in the NEXT turn "
                    "to run it, or confirm_action(answer='no') to cancel."), True
        # dry-run: desktop actions report instead of act
        dry_run = bool(SETTINGS.get("dry_run")) and DecisionPolicy.is_desktop_action(name)
        if dry_run:
            log_decision(name, target, "DRY-RUN", "reported; nothing executed")
            return (f"DRY-RUN: {name} would run with {target or 'no arguments'}. "
                    "Nothing was executed (dry_run is enabled in settings). "
                    "Describe the plan to the user and stop."), True
        log_decision(name, target, verdict if verdict != "CONFIRM" else "ALLOW",
                     "dispatched")
        # accept common argument-name slips local models make
        alias_map = fn._tool_aliases
        sig = inspect.signature(fn)
        kwargs = {}
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            raw = args.get(pname)
            if raw is None and pname in alias_map:
                for alt in alias_map[pname]:
                    if args.get(alt) is not None:
                        raw = args[alt]
                        break
            if raw is None and p.default is inspect.Parameter.empty:
                raw = ""
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
            if isinstance(ann, str):   # `from __future__ import annotations`
                ann = {"bool": bool, "int": int, "float": float,
                       "str": str}.get(ann, str)
            if ann is bool:
                kwargs[pname] = bool(raw) if not isinstance(raw, bool) else raw
            elif ann is int:
                try:
                    kwargs[pname] = int(raw or 0)
                except (TypeError, ValueError):
                    kwargs[pname] = 0
            elif ann is float:
                try:
                    kwargs[pname] = float(raw or 0.0)
                except (TypeError, ValueError):
                    kwargs[pname] = 0.0
            else:
                kwargs[pname] = str(raw) if raw is not None else ""
        try:
            out = fn(**kwargs)
            return out, out.startswith("ERROR") or out.startswith("REFUSED")
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}", True
        except Exception as e:
            log.exception("tool %s failed", name)
            return f"ERROR: {e}", True

    # -- run_command ----------------------------------------------------------

    def _validate_command(self, command: str) -> tuple[list | None, str, str | None, bool]:
        """Validate without executing: shlex.split + policy checks.

        Returns (argv, exe_base, None, is_restart) when allowed, else
        (None, '', error, False). Never executes anything (start_command must
        not double-exec via run_command) and never arms the restart hook —
        the caller arms it ONLY after a successful launch.
        """
        cmd = command.strip()
        if not cmd:
            return None, "", "REFUSED: empty command", False
        if any(c in cmd for c in ";|&`$\n\r<>"):
            return None, "", "REFUSED: shell operators (pipes, ;, &&, redirects) are not allowed", False
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return None, "", f"REFUSED: cannot parse command ({e})", False
        if not argv:
            return None, "", "REFUSED: empty command", False
        argv[0] = os.path.expanduser(argv[0])
        low = cmd.lower()
        exe_base = Path(argv[0]).name
        _unblocked = ""
        if exe_base in ("git", "cargo"):
            verb = next((a for a in argv[1:] if not a.startswith("-")), "")
            if (exe_base == "git" and verb in self._GIT_READ) or (
                    exe_base == "cargo" and verb in self._CARGO_OK):
                _unblocked = exe_base
            else:
                return None, "", (f"REFUSED: '{exe_base} {verb or '(no verb)'}' is not "
                        "allowed — git is read-only (status/diff/log/show/"
                        "branch/remote), cargo only builds/tests"), False
            if _unblocked == "git" and any(
                    a.lstrip("-") in self._GIT_DELETE_FLAGS
                    for a in argv[2:] if a.startswith("-")):
                return None, "", "REFUSED: deleting branches (git branch -d/-D) is not allowed", False
        for bad in self.BLOCKED:
            if bad == _unblocked:
                continue
            if re.search(rf"(^|\W){re.escape(bad)}(\W|$)", low):
                return None, "", f"REFUSED: '{bad}' is not on the safe whitelist (destructive commands are forbidden)", False
        exe = argv[0]
        is_restart = exe == str(RESTART_SCRIPT) or Path(exe).name == RESTART_SCRIPT.name
        if is_restart:
            if not self._perm.get("self_restart", True):
                return None, "", "REFUSED: self-restart is disabled in handsoff settings", False
            if not (RESTART_SCRIPT.exists() and os.access(RESTART_SCRIPT, os.X_OK)):
                return None, "", f"ERROR: restart script missing at {RESTART_SCRIPT} — run install.sh", False
        allowed = set(self.ALLOWED) | {
            c.strip() for c in SETTINGS["extra_allowed_commands"] if c.strip()
        }
        if not is_restart:
            if exe_base not in allowed and exe_base != _unblocked:
                return None, "", (
                    f"REFUSED: '{exe}' is not on the safe shell-command whitelist. "
                    "Note: REFUSED does NOT mean the program is missing — it only "
                    "means you may not run it via run_command. If it is one of your "
                    "own tools (like ydotool for typing), use that tool instead. "
                    "Allowed: " + ", ".join(sorted(allowed)) + f", {RESTART_SCRIPT}"
                ), False
        log.info("run_command: %s", cmd)
        if Path(argv[0]).name == "niri" and "spawn" in argv:
            err = self._validate_niri_spawn(argv)
            if err:
                return None, "", err, False
        return argv, exe_base, None, is_restart

    _INTERPRETERS = ("python", "python3", "node", "perl", "ruby", "lua",
                     "php", "bash", "sh", "zsh", "fish", "pwsh", "busybox")

    @staticmethod
    def _is_interpreter(base: str) -> bool:
        """Exact-or-versioned interpreter match (no substring overblock).

        Matches `python`, `python3`, `python3.11` — but NOT `shutter`,
        `bashful`, `shellcheck` or `phosphor`, which merely contain one.
        """
        b = (base or "").lower()
        return any(b == tok or re.fullmatch(rf"{re.escape(tok)}[\d.]+", b)
                   for tok in ToolBelt._INTERPRETERS)

    def _validate_niri_spawn(self, argv: list) -> str | None:
        """Spawn-specific capability checks (no execution)."""
        i = argv.index("spawn")
        rest = [a for a in argv[i + 1:] if a != "--"]
        target = os.path.basename(rest[0]) if rest else ""
        low_target = target.lower()
        if not target:
            return "REFUSED: niri spawn needs a program to launch"
        if any(re.search(rf"(^|\W){re.escape(b)}(\W|$)", low_target)
               for b in self.BLOCKED):
            return (f"REFUSED: niri spawn of '{target}' is blocked — spawn "
                    "must not bypass the blocked-programs list")
        if self._is_interpreter(low_target):
            return (f"REFUSED: spawning interpreter '{target}' is not allowed — "
                    "launch GUI apps by name instead (or use open_app)")
        if low_target in ("git", "cargo"):
            return (f"REFUSED: spawning '{target}' is not allowed — use "
                    "run_command, which gates git/cargo by verb")
        # ponytail: any extra arg whose basename is blocked/an interpreter is
        # a spawn bypass (e.g. `spawn -- alacritty bash`, `spawn -- foo -- python x`)
        for extra in rest[1:]:
            eb = os.path.basename(extra).lower()
            if not eb or eb.startswith("-"):
                continue
            if any(re.search(rf"(^|\W){re.escape(b)}(\W|$)", eb) for b in self.BLOCKED):
                return (f"REFUSED: niri spawn arg '{extra}' is blocked — spawn "
                        "must not bypass the blocked-programs list")
            if self._is_interpreter(eb) or eb in ("git", "cargo"):
                return (f"REFUSED: niri spawn arg '{extra}' is not allowed")
        # ponytail: terminal emulators with args are shell-exec by another name
        _terms = ("alacritty", "kitty", "foot", "konsole", "xterm", "urxvt",
                  "wezterm", "warp", "ghostty", "tilix", "terminator",
                  "qterminal", "gnome-terminal", "xfce4-terminal", "ptyxis",
                  "stterm", "terminology", "console", "terminal")
        if low_target in _terms and len(rest) > 1:
            return ("REFUSED: spawning a terminal with arguments is not allowed")
        if shutil.which(rest[0]) is None:
            return f"ERROR: no program named '{target}' is installed"
        arg_str = " ".join(shlex.quote(a) for a in rest[1:]).lower()
        if (" -e " in f" {arg_str} " or "--command" in arg_str
                or "--eval" in arg_str or "--print" in arg_str
                or "--script" in arg_str or "-x" == arg_str.strip()
                or " -x " in f" {arg_str} " or "-c" == arg_str.strip()
                or "source " in arg_str or ".lua" in arg_str
                or ".js" in arg_str or ".py" in arg_str):
            return ("REFUSED: passing script/code flags to spawned programs "
                    "is not allowed")
        return None

    @tool(description=(
        "Run one safe whitelisted command (pactl, playerctl, brightnessctl, "
        "niri, spawn, echo, cat, ls, pwd, notify-send, system probes like "
        "ps/free/uptime/df/ss/nvidia-smi, read-only git (status/diff/log/"
        "show/branch/remote/stash), cargo build/check/test/clippy, restart "
        "script). Single command only — pipes/; /&& are refused."))
    def run_command(self, command: str) -> str:
        """Run a whitelisted shell command.

        command: e.g. 'pactl set-sink-volume @DEFAULT_SINK@ -10%'
        """
        argv, exe_base, err, is_restart = self._validate_command(command)
        if err:
            return err
        exe = argv[0]
        try:
            timeout = 240.0 if exe_base == "cargo" else self.TIMEOUT
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            return f"ERROR: program not found: {exe}"
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout:.0f}s"
        if is_restart:
            # ponytail: arm only after the restart actually launched — a
            # refused/missing/failed restart must not set pending state.
            self._on_restart_pending()
        out = f"exit code {proc.returncode}\nstdout:\n{proc.stdout.strip()}\nstderr:\n{proc.stderr.strip()}"
        return out[:2000]

    # -- keyboard takeover (Wayland virtual input via ydotool) -----------------

    YDOTOOL_SOCKET = "/tmp/.ydotool_socket"   # legacy daemon location
    _YDOTOOL_SOCK_CACHE: list = []             # [resolved path] once found

    @classmethod
    def _ydotool_socket(cls) -> str:
        """Socket path the ydotool CLI should talk to.

        The user-level ydotoold (Arch's ydotool.service) listens on
        $XDG_RUNTIME_DIR/.ydotool_socket, but the CLI's compiled-in default
        is /tmp/.ydotool_socket — so without YDOTOOL_SOCKET set, every
        type/click dies with 'failed to connect'. Probe both and prefer the
        one that actually answers. Failures are not cached: a daemon started
        later must be picked up on the next call.
        """
        if cls._YDOTOOL_SOCK_CACHE:
            return cls._YDOTOOL_SOCK_CACHE[0]
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        candidates = ([os.path.join(runtime, ".ydotool_socket")] if runtime else []) \
            + [cls.YDOTOOL_SOCKET]
        for path in candidates:
            if cls._socket_connectable(path):
                cls._YDOTOOL_SOCK_CACHE.append(path)
                return path
        return candidates[0]

    @staticmethod
    def _socket_connectable(path: str) -> bool:
        """True only if `path` is a socket file that accepts a connection.

        ydotoold 1.x binds SOCK_DGRAM, so the probe must try DGRAM first —
        a stream connect to it fails with EPROTOTYPE even when the daemon
        is alive and reachable.
        """
        try:
            if not stat.S_ISSOCK(os.stat(path).st_mode):
                return False
        except OSError:
            return False
        for sock_type in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            s = socket.socket(socket.AF_UNIX, sock_type)
            try:
                s.settimeout(1.0)
                s.connect(path)
                return True
            except OSError:
                continue
            finally:
                s.close()
        return False

    # linux event keycodes for named keys
    _KEYCODES = {
        "enter": 28, "return": 28, "esc": 1, "escape": 1, "tab": 15,
        "space": 57, "backspace": 14, "delete": 111, "insert": 110,
        "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
        "up": 103, "down": 108, "left": 105, "right": 106,
        "capslock": 58, "printscreen": 99,
        "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
        "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    }
    _MOD_CODES = {"ctrl": 29, "alt": 56, "shift": 42, "meta": 125, "super": 125}
    # US-QWERTY keycodes for single characters (press_keys only; type_text
    # handles arbitrary text itself)
    _CHAR_CODES = {
        **{c: 16 + i for i, c in enumerate("qwertyuiop")},
        **{c: 30 + i for i, c in enumerate("asdfghjkl")},
        **{c: 44 + i for i, c in enumerate("zxcvbnm")},
        **{str(n): n + 1 for n in range(1, 10)}, "0": 11,
    }
    _MAX_TYPE = 20_000

    def _ydotool(self, *args: str) -> str:
        env = dict(os.environ)
        # the CLI's built-in default is /tmp/.ydotool_socket; point it at the
        # daemon that actually answers (user-runtime socket preferred)
        env["YDOTOOL_SOCKET"] = self._ydotool_socket()
        try:
            proc = subprocess.run(
                ["ydotool", *args], capture_output=True, text=True, env=env,
                # typing takes ~6 ms per char on the "type" path: the timeout
                # must scale with payload size or long texts (cap 20k chars
                # = ~2 min of typing) die with a false "timed out".
                timeout=20 + 0.01 * sum(len(a) for a in args),
            )
        except FileNotFoundError:
            return "ERROR: ydotool not installed (pacman -S ydotool)"
        except subprocess.TimeoutExpired:
            return "ERROR: ydotool timed out"
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "unknown error").strip()
            return f"ERROR: ydotool failed: {msg}"
        return "ok"

    # -- niri IPC plumbing (one place for every window query) -----------------

    @staticmethod
    def _niri_msg(*args: str, timeout: float = 8.0) -> subprocess.CompletedProcess:
        return subprocess.run(["niri", *args], capture_output=True, text=True,
                              timeout=timeout)

    @classmethod
    def _niri_windows(cls) -> list[dict]:
        """One live window-list poll. Raises RuntimeError when the niri IPC
        is unreachable or answers garbage — callers decide whether that is
        fatal or ignorable."""
        try:
            r = cls._niri_msg("msg", "--json", "windows")
            if r.returncode != 0:
                raise RuntimeError(
                    (r.stderr or r.stdout or "niri refused").strip()[:160])
            return json.loads(r.stdout or "[]")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"niri IPC unavailable ({e})") from e

    # workspace id → user-facing idx: niri's windows JSON carries a global
    # id (multi-output values look arbitrary, e.g. id 1 = idx 2 on DP-3),
    # while users and the workspace tool speak INDEX. Resolve through the
    # live workspace list, cached briefly.
    _WS_IDX_TTL = 10.0
    _WS_IDX_LOCK = threading.Lock()
    _WS_IDX_CACHE: dict = {}          # {"at": monotonic, "map": {id: idx}}

    @classmethod
    def _workspace_idx_map(cls, refresh: bool = False) -> dict:
        """{workspace_id: idx} from the live workspace list (TTL-cached)."""
        with cls._WS_IDX_LOCK:
            c = cls._WS_IDX_CACHE
            if not refresh and c and time.monotonic() - c["at"] < cls._WS_IDX_TTL:
                return c["map"]
            mapping: dict = {}
            try:
                r = cls._niri_msg("msg", "--json", "workspaces")
                if r.returncode == 0:
                    for ws in json.loads(r.stdout or "[]"):
                        if ws.get("id") is not None and ws.get("idx") is not None:
                            mapping[ws["id"]] = ws["idx"]
            except Exception:
                pass
            cls._WS_IDX_CACHE = {"at": time.monotonic(), "map": mapping}
            return mapping

    @classmethod
    def _workspace_idx_of(cls, win: dict) -> int | None:
        """The user-facing workspace index of window `win` (None if unknown)."""
        wid = win.get("workspace_id")
        if wid is None:
            return None
        return cls._workspace_idx_map().get(wid)

    @classmethod
    def _win_label(cls, w: dict) -> str:
        """Compact human label: 'app_id: title (workspace N)'. The workspace
        number is the user-facing INDEX — the raw workspace_id is a global id
        that can differ from what the user sees (multi-output setups)."""
        label = f"{w.get('app_id') or '?'}: {(w.get('title') or '')[:60]}"
        idx = cls._workspace_idx_of(w)
        if idx is not None:
            label += f" (workspace {idx})"
        return label

    @staticmethod
    def _win_matches(w: dict, q: str) -> bool:
        """Case-insensitive substring match on app-id or title."""
        return (q in str(w.get("app_id", "")).lower()
                or q in str(w.get("title", "")).lower())

    @staticmethod
    def _win_listing(wins: list[dict], limit: int = 12) -> str:
        return " | ".join(ToolBelt._win_label(w) for w in wins[:limit])

    def _focused_window_info(self) -> dict | None:
        """Best-effort info about the focused window (niri)."""
        try:
            for w in self._niri_windows():
                if w.get("is_focused"):
                    return w
        except Exception:
            pass
        return None

    _TERMINAL_MARKERS = ("terminal", "konsole", "alacritty", "kitty", "foot",
                         "xterm", "urxvt", "wezterm", "warp", "ghostty",
                         "stterm", "st-", "tilix", "terminator", "qterminal",
                         "gnome-terminal", "xfce4-terminal", "ptyxis", "console")
    # ponytail: editors are NOT terminals — typing into code/zed/emacs/vim
    # is the normal case. Only refuse when the TITLE shows a terminal
    # panel (integrated Terminal / Output / REPL view would execute text).
    _TERMINAL_PANEL_MARKERS = ("terminal", "output", "repl")

    def _focused_is_terminal(self) -> str | None:
        """App-id/title of the focused window if it looks like a terminal, else None.

        Raises RuntimeError when focus CANNOT be determined (niri IPC dead or
        no focused window): keyboard injection must FAIL CLOSED — typing into
        an unidentified window could execute text in a terminal."""
        return self._terminal_marker(self._typing_guard())

    def _typing_guard(self) -> dict:
        """The verified focused window for a keyboard-injection operation.

        EVERY typing operation calls this immediately before injecting keys:
        focus may have moved since the model decided what to type (the user
        clicked elsewhere, a dialog opened). Raises RuntimeError when focus
        cannot be verified (fail-closed, as _focused_is_terminal)."""
        w = self._focused_window_info()
        if w is None:
            raise RuntimeError("cannot verify the focused window (niri IPC "
                               "unavailable) — refusing to inject keys")
        return w

    @classmethod
    def _terminal_marker(cls, w: dict) -> str | None:
        """App-id/title of window `w` if it looks like a terminal, else None."""
        app_id = str(w.get("app_id", "") or "").lower()
        title = str(w.get("title", "") or "").lower()
        if any(t in app_id for t in cls._TERMINAL_MARKERS):
            return app_id or title
        # ponytail: an editor (or anything else) showing a terminal panel
        # in its title would still execute typed text — refuse on that.
        if any(t in title for t in cls._TERMINAL_PANEL_MARKERS):
            return (app_id or title or "unknown-window") + " (terminal panel)"
        # ponytail: empty app_id with a known-safe title is an IPC glitch,
        # not a terminal — allow with a warning instead of a hard refuse.
        if not app_id.strip():
            if title.strip():
                log.warning("typing target has empty app_id but safe title %r — allowing",
                            title)
                return None
            return "unknown-window (unidentified — fail-closed)"
        return None

    @tool(description=(
        "Type text into the focused window via virtual keyboard. Newlines "
        "allowed. Never type into a terminal."),
        aliases={"text": ("content", "string", "body")})
    def type_text(self, text: str) -> str:
        """Type text into the focused window.

        text: The exact text to type.
        """
        if not text:
            return "REFUSED: nothing to type"
        if len(text) > self._MAX_TYPE:
            return f"REFUSED: text too long (>{self._MAX_TYPE} chars)"
        # capability boundary: terminals must never receive injected text
        # (they would execute it). FAIL CLOSED: if focus can't be verified,
        # refuse — an unidentified window might be a terminal.
        try:
            target = self._typing_guard()
        except RuntimeError as e:
            return f"REFUSED: {e}"
        if (term := self._terminal_marker(target)) is not None:
            return (f"REFUSED: the focused window is a terminal ({term}); "
                    "typing into terminals is forbidden")
        # ponytail: chunked typing (512 chars) with focus re-verified per
        # chunk — a long burst can outlive a focus change; abort on move.
        typed = 0
        skipped = 0
        _CHUNK = 512
        for off in range(0, len(text), _CHUNK):
            if off:
                try:
                    target = self._typing_guard()
                except RuntimeError as e:
                    return (f"REFUSED: {e} (typed {typed}/{len(text)} chars "
                            "before focus became unverifiable)")
                if (term := self._terminal_marker(target)) is not None:
                    return (f"REFUSED: focus moved to a terminal ({term}) "
                            f"after {typed} chars — typing aborted")
            piece = text[off: off + _CHUNK]
            r = self._ydotool("type", "--key-delay", "6", "--", piece)
            if r != "ok":
                break
            typed += len(piece)
        else:
            r = "ok"
        if r == "ok":
            pass  # typed already counts completed chunks
        elif typed == 0:
            # retry once: uinput is unreliable for non-ASCII in ydotool 1.x.
            # Focus is RE-VERIFIED first — the failed attempt may have taken
            # long enough for focus to move (same fail-closed rules).
            try:
                target = self._typing_guard()
            except RuntimeError as e:
                return f"REFUSED: {e}"
            if (term := self._terminal_marker(target)) is not None:
                return (f"REFUSED: focus moved to a terminal ({term}) "
                        "before the retry — typing aborted")
            ascii_text = (text.replace("\u2014", "-").replace("\u2013", "-")
                          .replace("\u201c", '"').replace("\u201d", '"')
                          .replace("\u2018", "'").replace("\u2019", "'")
                          .replace("\u2026", "..."))
            ascii_text = unicodedata.normalize(
                "NFKD", ascii_text).encode("ascii", "ignore").decode("ascii")
            r = self._ydotool("type", "--key-delay", "6", "--", ascii_text)
            if r != "ok":
                return "ERROR: typing failed entirely"
            typed = len(ascii_text)
            skipped = max(0, len(text) - len(ascii_text))
        else:
            return f"ERROR: typing failed after {typed}/{len(text)} chars: {r}"
        if typed == 0:
            return "ERROR: typing failed entirely"
        # post-action verification: a long burst can outlive a focus change
        # (the user clicked elsewhere mid-type) — report where the text may
        # have landed instead of silently claiming success.
        note = f"typed {typed} chars into {self._win_label(target)}"
        if skipped:
            note += f" ({skipped} chars skipped: unsupported characters)"
        end_focus = self._focused_window_info()
        if end_focus is not None and end_focus.get("id") != target.get("id"):
            note += (f" — WARNING: focus moved to {self._win_label(end_focus)} "
                     "during typing; some text may have landed there")
        self._mark_elements_stale()          # screen content changed
        log.info("type_text: %d chars into %s (skipped %d)",
                 typed, self._win_label(target), skipped)
        return note

    @tool(description=(
        "Press a key combo in the focused window: 'enter', 'ctrl+c', "
        "'ctrl+a', 'alt+tab'."),
        aliases={"combo": ("keys", "key", "combination")})
    def press_keys(self, combo: str) -> str:
        """Press an in-app key combination.

        combo: e.g. 'ctrl+enter', 'ctrl+a', 'escape'
        """
        c = combo.strip().lower()
        if not c:
            return "REFUSED: empty combo"
        # same fail-closed verified-focus guard as type_text
        try:
            term = self._focused_is_terminal()
        except RuntimeError as e:
            return f"REFUSED: {e}"
        if term:
            return (f"REFUSED: the focused window is a terminal ({term}); "
                    "sending keys to terminals is forbidden")
        if len(c) > 64:
            return "REFUSED: combo too long"
        parts = [p.strip() for p in c.split("+") if p.strip()]
        if not parts:
            return "REFUSED: empty combo"
        mods = []
        keys = []
        for p in parts:
            if p in self._MOD_CODES:
                mods.append(self._MOD_CODES[p])
            elif p in self._KEYCODES:
                keys.append(self._KEYCODES[p])
            elif len(p) == 1 and p in self._CHAR_CODES:
                keys.append(self._CHAR_CODES[p])
            else:
                return f"REFUSED: unknown key '{p}'"
        if not keys:
            return "REFUSED: combo needs a non-modifier key"
        argv = ["key"]
        for m in mods:
            argv += [f"{m}:1"]
        for k in keys:
            argv += [f"{k}:1", f"{k}:0"]
        for m in reversed(mods):
            argv += [f"{m}:0"]
        r = self._ydotool(*argv)
        if r == "ok":
            self._mark_elements_stale()      # e.g. enter submits, screen changed
        return r

    # niri named keys for press_hotkey (evdev codes beyond letters/digits)
    _HOTKEY_NAMES = {
        "return": 28, "enter": 28, "space": 57, "tab": 15,
        "esc": 1, "escape": 1, "backspace": 14, "delete": 111, "del": 111,
        "up": 103, "down": 108, "left": 105, "right": 106,
        "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
        "print": 99, "insert": 110, "minus": 12, "equal": 13,
        "comma": 51, "period": 52, "slash": 53, "semicolon": 39,
    }

    @staticmethod
    def _super_binding_known(combo: str) -> bool:
        """Best-effort check that a Super chord is bound in niri config.

        Follows `include "..."` lines from config.kdl (binds live in
        cfg/keybinds.kdl). True when found OR the config is unreadable
        (legacy allow). False only when readable config lacks the chord
        (it would reach the focused app).
        """
        try:
            base = HOME / ".config/niri"
            texts: list[str] = [(base / "config.kdl").read_text(encoding="utf-8").lower()]
        except OSError:
            return True
        # ponytail: one-level include follow, bounded (no glob, no recursion bomb)
        try:
            seen: set[str] = set()
            for m in re.findall(r'include\s+"([^"]+)"', texts[0]):
                if len(seen) >= 20:
                    break
                inc = (base / m).resolve() if not m.startswith("/") else Path(m)
                try:
                    if str(inc) in seen or not str(inc).startswith(str(base)):
                        continue
                    seen.add(str(inc))
                    if inc.is_file() and inc.stat().st_size < 500_000:
                        texts.append(inc.read_text(encoding="utf-8").lower())
                except OSError:
                    continue
        except Exception:
            pass
        # ponytail: match real bind lines only — a chord mentioned in a
        # `// comment` is not bound, and must not lift the terminal guard.
        def _code(text: str) -> str:
            return "\n".join(ln.split("//", 1)[0] for ln in text.splitlines())
        norm = combo.strip().lower().replace(" ", "")
        norm = norm.replace("meta+", "mod+").replace("super+", "mod+").replace("win+", "mod+").replace("logo+", "mod+")
        blob = "\n".join(_code(t) for t in texts)
        return norm in blob

    @tool(description=(
        "Fire a desktop/compositor shortcut: 'Mod+E' (files), 'Mod+Return' "
        "(terminal), 'Mod+F' (fullscreen), 'Mod+Shift+S' (settings). "
        "System-wide actions; for in-app chords use press_keys."),
        gates="press_keys",
        aliases={"combo": ("keys", "key")})
    def press_hotkey(self, combo: str) -> str:
        """Fire a compositor hotkey.

        combo: e.g. 'Mod+E', 'Mod+Return', 'Mod+Shift+V'

        Mod maps to Super.
        """
        c = combo.strip().lower().replace(" ", "")
        if not c:
            return "REFUSED: empty combo"
        if len(c) > 64:
            return "REFUSED: combo too long"
        parts = [p for p in c.split("+") if p]
        if not parts:
            return "REFUSED: empty combo"
        mods = []
        keys = []
        has_super = False
        for p in parts:
            if p in ("mod", "meta", "super", "win", "logo"):
                mods.append(125)  # Super — compositor sees this first
                has_super = True
            elif p in ("ctrl", "control"):
                mods.append(29)
            elif p == "alt":
                mods.append(56)
            elif p == "shift":
                mods.append(42)
            elif p in self._KEYCODES:
                keys.append(self._KEYCODES[p])
            elif p in self._HOTKEY_NAMES:
                keys.append(self._HOTKEY_NAMES[p])
            elif len(p) == 1 and p in self._CHAR_CODES:
                keys.append(self._CHAR_CODES[p])
            else:
                return f"REFUSED: unknown key '{p}'"
        if not keys:
            return "REFUSED: combo needs a non-modifier key (e.g. Mod+E)"
        # ponytail: Super-bypass only when the chord is actually bound in the
        # compositor (else the focused app receives it). Binding check is
        # best-effort: unreadable config keeps the legacy allow.
        if not (has_super and self._super_binding_known(c)):
            try:
                term = self._focused_is_terminal()
            except RuntimeError as e:
                return f"REFUSED: {e}"
            if term:
                return (f"REFUSED: the focused window is a terminal ({term}); "
                        "only bound compositor hotkeys (Mod/Super) may be sent to terminals")
        argv = ["key"]
        for m in mods:
            argv += [f"{m}:1"]
        for k in keys:
            argv += [f"{k}:1", f"{k}:0"]
        for m in reversed(mods):
            argv += [f"{m}:0"]
        r = self._ydotool(*argv)
        if r == "ok":
            self._mark_elements_stale()      # compositor shortcuts change the screen
            log.info("press_hotkey: %s", c)
        return r

    # -- ambient controls ------------------------------------------------------

    @tool(gates="notifications", description=(
        "Read future desktop notifications aloud. Actions: start, stop, "
        "toggle, status, or mute (mute_apps is comma-separated app names). "
        "Private and disabled by default."))
    def notification_reader(self, action: str = "status", mute_apps: str = "") -> str:
        action = str(action or "status").strip().lower()
        if action == "mute":
            apps = [x.strip().lower() for x in str(mute_apps or "").split(",")
                    if x.strip()]
            set_setting("notification_mute_apps", apps[:32])
            return "notification mute list set to: " + (", ".join(apps) or "(empty)")
        if action not in ("start", "stop", "toggle", "status"):
            return "ERROR: action must be start, stop, toggle, status or mute"
        current = bool(SETTINGS.get("notification_reader", False))
        if action == "toggle":
            current = not current
        elif action == "start":
            current = True
        elif action == "stop":
            current = False
        if action != "status":
            set_setting("notification_reader", current)
            if self._on_notification is not None:
                result = self._on_notification(current)
                if result:
                    return result
        muted = ", ".join(SETTINGS.get("notification_mute_apps") or []) or "none"
        return f"notification reader is {'on' if current else 'off'}; muted apps: {muted}"

    @tool(gates="pomodoro", description=(
        "Start or control a repeating Pomodoro timer. action: start, stop, "
        "status. work_minutes defaults to 25 and break_minutes to 5."))
    def pomodoro(self, action: str = "status", work_minutes: float = 25,
                 break_minutes: float = 5) -> str:
        action = str(action or "status").strip().lower()
        if action not in ("start", "stop", "status"):
            return "ERROR: action must be start, stop or status"
        callback = self._on_pomodoro
        if callback is None:
            return "ERROR: pomodoro controller is unavailable"
        try:
            work_minutes = float(work_minutes)
            break_minutes = float(break_minutes)
        except (TypeError, ValueError):
            return "ERROR: work_minutes and break_minutes must be numbers"
        if not (1 <= work_minutes <= 120 and 1 <= break_minutes <= 60):
            return "ERROR: work_minutes must be 1-120 and break_minutes 1-60"
        return callback(action, work_minutes, break_minutes)

    def _watch_emit(self, text: str) -> None:
        """Send watcher alerts to the configured assistant announcement path,
        while retaining a desktop notification as a visible fallback."""
        log.warning("watcher alert: %s", text)
        notify("handsoff watcher: " + text)
        if self._on_announce is not None:
            try:
                self._on_announce(text)
            except Exception:
                log.exception("watcher announcement failed")

    @staticmethod
    def _file_watch_loop(path: Path, pattern: re.Pattern, stop: threading.Event,
                         emit) -> None:
        try:
            position = path.stat().st_size
        except OSError:
            position = 0
        deadline = time.monotonic() + 24 * 3600
        while not stop.wait(1.0) and time.monotonic() < deadline:
            try:
                size = path.stat().st_size
                if size < position:       # rotation/truncation: start at zero
                    position = 0
                if size == position:
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(position)
                    chunk = fh.read(min(size - position, 128_000))
                    position = fh.tell()
                for line in chunk.splitlines():
                    if pattern.search(line):
                        emit(f"{path.name}: {line.strip()[:240]}")
            except OSError:
                emit(f"file watcher lost {path}")
                return
            except Exception:
                log.exception("file watcher failed for %s", path)
                return

    @staticmethod
    def _process_watch_loop(name: str, stop: threading.Event, emit) -> None:
        seen = False
        deadline = time.monotonic() + 24 * 3600
        while not stop.wait(1.0) and time.monotonic() < deadline:
            present = any(n.lower() == name.lower() for _, n in ToolBelt._same_user_procs())
            if seen and not present:
                emit(f"process {name} exited")
                return
            seen = present

    @tool(gates="watchers", description=(
        "Watch a text file for new lines matching a regular expression. "
        "action=start/stop/list; start requires path and pattern. Max four "
        "file watchers, each stops on deletion or after 24 hours."))
    def watch_file(self, path: str = "", pattern: str = "", action: str = "start") -> str:
        action = str(action or "start").strip().lower()
        p = Path(str(path or "")).expanduser().resolve()
        key = str(p)
        if action == "list":
            with self._watch_lock:
                return "file watchers: " + (", ".join(self._file_watchers) or "none")
        if action == "stop":
            with self._watch_lock:
                item = self._file_watchers.pop(key, None)
            if item:
                item[0].set()
                return f"stopped watching {p}"
            return f"no file watcher for {p}"
        if action != "start":
            return "ERROR: action must be start, stop or list"
        if not p.is_file():
            return f"ERROR: no readable file: {p}"
        try:
            rx = re.compile(str(pattern or ""))
        except re.error as e:
            return f"ERROR: invalid pattern: {e}"
        if not rx.pattern:
            return "ERROR: pattern must not be empty"
        with self._watch_lock:
            if key not in self._file_watchers and len(self._file_watchers) >= 4:
                return "ERROR: maximum of four file watchers reached"
            old = self._file_watchers.pop(key, None)
            if old:
                old[0].set()
            stop = threading.Event()
            thread = threading.Thread(target=self._file_watch_loop,
                                      args=(p, rx, stop, self._watch_emit),
                                      name="watch-file", daemon=True)
            self._file_watchers[key] = (stop, thread)
            thread.start()
        return f"watching {p} for /{rx.pattern}/ (starts at the current end)"

    @tool(gates="watchers", description=(
        "Watch one exact same-user process name and announce when it exits. "
        "action=start/stop/list; max four process watchers."))
    def watch_process(self, name: str = "", action: str = "start") -> str:
        action = str(action or "start").strip().lower()
        name = str(name or "").strip()
        if action == "list":
            with self._watch_lock:
                return "process watchers: " + (", ".join(self._process_watchers) or "none")
        if not name or len(name) > 128 or not re.fullmatch(r"[A-Za-z0-9_.@+-]+", name):
            return "ERROR: process name must be an exact simple name"
        if action == "stop":
            with self._watch_lock:
                item = self._process_watchers.pop(name.lower(), None)
            if item:
                item[0].set()
                return f"stopped watching process {name}"
            return f"no process watcher for {name}"
        if action != "start":
            return "ERROR: action must be start, stop or list"
        with self._watch_lock:
            if name.lower() not in self._process_watchers and len(self._process_watchers) >= 4:
                return "ERROR: maximum of four process watchers reached"
            old = self._process_watchers.pop(name.lower(), None)
            if old:
                old[0].set()
            stop = threading.Event()
            thread = threading.Thread(target=self._process_watch_loop,
                                      args=(name, stop, self._watch_emit),
                                      name="watch-process", daemon=True)
            self._process_watchers[name.lower()] = (stop, thread)
            thread.start()
        return f"watching process {name} for exit"

    def stop_watchers(self) -> None:
        with self._watch_lock:
            items = list(self._file_watchers.values()) + list(self._process_watchers.values())
            self._file_watchers.clear()
            self._process_watchers.clear()
        for stop, _thread in items:
            stop.set()

    # -- window focus & clipboard ---------------------------------------------

    @tool(description=(
        "Control niri workspaces: 'go' (switch), 'move' (send window), "
        "'next'/'prev' (one workspace), 'list' (overview)."),
        gates="run_command",
        aliases={"action": ("cmd", "command", "op"), "target": ("arg", "value", "ref")})
    def workspace(self, action: str, target: str = "") -> str:
        """Control workspaces.

        action: 'go', 'move', 'next', 'prev' or 'list'
        target: workspace number/name, or '<app> to <workspace>' for 'move'
        """
        act = action.strip().lower()

        def _resolve(ref: str) -> str:
            """Personal alias → number/name ('go to code'); otherwise pass
            through (niri accepts numbers and its own workspace names)."""
            r = re.sub(r"^\s*(the\s+)?(workspace|ws)\s+", "", ref.strip(),
                       flags=re.IGNORECASE)
            aliases = SETTINGS.get("workspace_aliases") or {}
            return aliases.get(r.strip().lower(), r)

        t = _resolve(target)

        def _niri(*args: str, timeout: float = 8) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["niri", *args], capture_output=True, text=True, timeout=timeout)

        if act in ("go", "goto", "switch", "focus", "jump"):
            if not t:
                return "ERROR: name a workspace (number or name)"
            r = _niri("msg", "action", "focus-workspace", t)
            if r.returncode != 0:
                return f"ERROR: niri refused ({(r.stderr or r.stdout).strip()})"
            log.info("workspace: focus %s", t)
            return f"switched to workspace {t}"

        if act in ("next", "down"):
            r = _niri("msg", "action", "focus-workspace-down")
            return "moved to the workspace below" if r.returncode == 0 else \
                f"ERROR: niri refused ({(r.stderr or r.stdout).strip()})"

        if act in ("prev", "previous", "up"):
            r = _niri("msg", "action", "focus-workspace-up")
            return "moved to the workspace above" if r.returncode == 0 else \
                f"ERROR: niri refused ({(r.stderr or r.stdout).strip()})"

        if act in ("move", "send"):
            if not t:
                return "ERROR: name the target workspace (or '<app> to <workspace>')"
            m = re.split(r"\s+to\s+", t, maxsplit=1, flags=re.IGNORECASE)
            if len(m) == 2:
                app, ref = m[0].strip(), _resolve(m[1])
                try:
                    raw = _niri("msg", "--json", "windows").stdout
                    wins = json.loads(raw or "[]")
                except Exception as e:
                    return f"ERROR: cannot list windows ({e})"
                cands = [w for w in wins if app.lower() in
                         (str(w.get("app_id", "")) + " " + str(w.get("title", ""))).lower()]
                if not cands:
                    return f"ERROR: no window matching '{app}'"
                wid = cands[0].get("id")
            else:
                ref = t
                # focused window: niri moves the FOCUSED window when only a
                # target is given
                focused = next((w for w in json.loads(
                    (_niri("msg", "--json", "windows").stdout or "[]"))
                    if w.get("is_focused")), None)
                if focused is None:
                    return "ERROR: no focused window to move"
                wid = focused.get("id")
            r = _niri("msg", "action", "move-window-to-workspace",
                      "--window-id", str(wid), ref)
            if r.returncode != 0:
                return f"ERROR: niri refused ({(r.stderr or r.stdout).strip()})"
            # verify by user-facing idx: niri accepting the command is not
            # the window having moved (and idx, not the global id, is what
            # the user means by 'workspace N')
            want = int(ref) if str(ref).strip().isdigit() else None
            moved = None
            if want is not None:
                try:
                    # ponytail: bounded verify (5 x 0.15s polls, 3s IPC cap) —
                    # a hung niri must not block the turn for ~2min.
                    for _ in range(5):
                        time.sleep(0.15)
                        cur = next((w for w in json.loads(
                            _niri("msg", "--json", "windows", timeout=3).stdout or "[]")
                            if w.get("id") == wid), None)
                        # refresh the id→idx cache every poll: the cached map
                        # predates the move and would read the OLD workspace
                        if cur and self._workspace_idx_map(
                                refresh=True).get(
                                cur.get("workspace_id")) == want:
                            moved = True
                            break
                except Exception:
                    pass
            if moved:
                log.info("workspace: move → %s (verified)", ref)
                return f"moved the window to workspace {ref} (verified)"
            log.info("workspace: move → %s (unconfirmed)", ref)
            return (f"moved the window to workspace {ref}, but the move is "
                    "NOT confirmed yet — re-check with 'list my workspaces' "
                    "before typing there")

        if act in ("list", "show", "status"):
            try:
                wss = json.loads(_niri("msg", "--json", "workspaces").stdout or "[]")
                wins = json.loads(_niri("msg", "--json", "windows").stdout or "[]")
            except Exception as e:
                return f"ERROR: cannot read workspaces ({e})"
            out = []
            for ws in sorted(wss, key=lambda x: x.get("idx", 0)):
                label = f"ws{ws.get('idx')}"
                aliases = SETTINGS.get("workspace_aliases") or {}
                aka = [k for k, v in aliases.items() if v == str(ws.get("idx"))]
                if ws.get("name"):
                    label += f" ({ws['name']})"
                if aka:
                    label += f" [{', '.join(aka)}]"
                if ws.get("is_focused"):
                    label += " [current]"
                here = [str(w.get("title") or w.get("app_id") or "?")[:28]
                        for w in wins if w.get("workspace_id") == ws.get("id")]
                out.append(f"{label}: " + (" | ".join(here[:6]) or "(empty)"))
            return "workspaces: " + " ;; ".join(out) if out else "no workspaces"

        return (f"ERROR: unknown workspace action '{act}' — "
                "use go / move / next / prev / list")

    # polling cadence for window waits: niri maps windows instantly on spawn,
    # but apps take 0.5-3 s to map their first window; 0.25 s is invisible to
    # the user yet catches fast launchers on the very first poll
    _WIN_POLL_S = 0.25

    def _wait_window_match(self, q: str, timeout: float,
                           ids_before: set | None = None) -> dict | None:
        """Poll the live window list until a window matching `q` appears.

        With ids_before, only windows NOT in that set are considered (used by
        open_app to identify the freshly launched window). Returns the window
        dict or None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                wins = self._niri_windows()
            except RuntimeError:
                wins = []
            for w in wins:
                if ids_before is not None and w.get("id") in ids_before:
                    continue
                if self._win_matches(w, q):
                    return w
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._WIN_POLL_S)

    @tool(description=(
        "Focus a desktop window by app name or title substring "
        "('firefox', 'slack', 'alacritty'). Confirms the window really got "
        "focus before returning."),
        aliases={"app": ("window",)})
    def focus_window(self, app: str) -> str:
        """Focus a window by name.

        app: app-id or title substring, e.g. 'firefox'
        """
        q = app.strip().lower()
        if not q:
            return "REFUSED: name the app or window title to focus"
        try:
            wins = self._niri_windows()
        except RuntimeError as e:
            return f"ERROR: cannot list windows ({e})"
        cands = [w for w in wins if self._win_matches(w, q)]
        if not cands:
            return ("ERROR: no window matching " + q + ". Open windows: "
                    + self._win_listing(wins))
        w = cands[0]
        try:
            r = self._niri_msg("msg", "action", "focus-window",
                               "--id", str(w.get("id")))
        except Exception as e:
            return f"ERROR: focus failed: {e}"
        if r.returncode != 0:
            return f"ERROR: focus failed: {(r.stderr or 'unknown').strip()}"
        # post-action verification: 'niri accepted the command' is not
        # 'the window has focus'. Poll until it is (or admit we can't tell).
        note = f"focused {self._win_label(w)}"
        confirmed = self._wait_focus_id(w.get("id"), 2.0)
        if confirmed:
            note += " (focus confirmed)"
        else:
            note += " (focus NOT confirmed yet — it may still be switching)"
        log.info("focus_window: %s", note)
        return note

    def _wait_focus_id(self, win_id, timeout: float) -> bool:
        """True when window `win_id` reports is_focused within `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                wins = self._niri_windows()
            except RuntimeError:
                return False
            if any(w.get("id") == win_id and w.get("is_focused") for w in wins):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._WIN_POLL_S)

    @tool(description=(
        "Wait until a window matching the name exists (apps take a moment to "
        "appear after launch). Returns the window and whether it is focused. "
        "Use after open_app, or when a window is slow to appear."),
        gates="focus_window",
        aliases={"app": ("window", "name")})
    def wait_for_window(self, app: str, timeout: float = 10.0) -> str:
        """Wait for a window to exist.

        app: app-id or title substring to wait for
        timeout: seconds to wait before giving up (max 30)
        """
        q = app.strip().lower()
        if not q:
            return "REFUSED: name the app or window title to wait for"
        timeout = min(max(float(timeout or 10.0), 0.5), 30.0)
        w = self._wait_window_match(q, timeout)
        if w is None:
            try:
                listing = self._win_listing(self._niri_windows())
            except RuntimeError:
                listing = "window list unavailable (niri IPC down?)"
            return (f"ERROR: no window matching '{q}' appeared within "
                    f"{timeout:g}s. Open windows: {listing}")
        focus = ("and focused" if w.get("is_focused")
                 else "but NOT focused — call focus_window before typing")
        log.info("wait_for_window: %s %s", self._win_label(w), focus)
        return f"window ready: {self._win_label(w)} ({focus})"

    @tool(description=(
        "Sleep for `seconds` (0.5-30, default 1) before the next action: "
        "lets an app finish drawing, an animation settle, or a dialog "
        "appear. Prefer wait_for_window when waiting for an app window."),
        gates="",
        aliases={"seconds": ("secs", "delay", "duration")})
    def wait(self, seconds: float = 1.0) -> str:
        """Wait a moment.

        seconds: how long to sleep (0.5 to 30)
        """
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            s = 1.0
        s = min(max(s, 0.5), 30.0)
        time.sleep(s)
        log.info("wait: %.1fs", s)
        return f"waited {s:.1f}s"

    # -- live capability manifest (what THIS compositor can do right now) ------
    # The AI must not guess niri's surface: actions differ across versions, so
    # the manifest is polled live (cached briefly) instead of hardcoded.

    _MANIFEST_TTL = 30.0
    _MANIFEST_LOCK = threading.Lock()
    _MANIFEST_CACHE: dict | None = None   # {"at": monotonic, "data": dict}

    @classmethod
    def _niri_manifest(cls, refresh: bool = False) -> dict:
        """The live capability manifest, cached for _MANIFEST_TTL seconds."""
        with cls._MANIFEST_LOCK:
            c = cls._MANIFEST_CACHE
            if (not refresh and c is not None
                    and time.monotonic() - c["at"] < cls._MANIFEST_TTL):
                return c["data"]
            data = cls._build_manifest()
            cls._MANIFEST_CACHE = {"at": time.monotonic(), "data": data}
            return data

    @staticmethod
    def _niri_help_names(argv: list[str], section: str) -> list[str]:
        """Enum names from a niri help text: the lines indented exactly two
        spaces under `section:` ('Actions:', …), until the next left-flush
        section. Tolerates missing niri (returns [])."""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=8)
            text = (r.stdout or "") + "\n" + (r.stderr or "")
        except Exception:
            return []
        names: list[str] = []
        inside = False
        for line in text.splitlines():
            if not inside:
                if line.strip() == section + ":":
                    inside = True
                continue
            if not line.startswith("  "):
                if line.strip():
                    break                     # next left-flush section
                continue
            m = re.fullmatch(r"  ([a-z0-9][a-z0-9-]*)\s*", line)
            if m:
                names.append(m.group(1))
        return names

    @classmethod
    def _build_manifest(cls) -> dict:
        """Poll niri for everything the desktop-action tools depend on.
        Every piece is optional: a dead IPC just leaves that piece absent."""
        m: dict = {}

        def _json_cli(*args: str):
            r = cls._niri_msg(*args)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "niri refused").strip()[:120])
            return json.loads(r.stdout or "null")

        try:
            v = _json_cli("msg", "--json", "version")
            m["version"] = str(v.get("compositor") or v.get("cli") or "?")
        except Exception:
            m["version_error"] = "niri IPC unavailable"
        try:
            wins = cls._niri_windows()
            m["windows"] = {
                "count": len(wins),
                "focused": next((cls._win_label(w) for w in wins
                                 if w.get("is_focused")), None),
                # user-facing placement: app_id -> workspace INDEX (never the
                # raw workspace_id, which is a global id, not what users see)
                "placement": {str(w.get("app_id") or "?"): cls._workspace_idx_of(w)
                              for w in wins if w.get("app_id")},
            }
        except Exception:
            pass
        try:
            wss = _json_cli("msg", "--json", "workspaces")
            m["workspaces"] = {
                "count": len(wss),
                # sorted user-facing indices so the model can reason about
                # 'workspace 2' correctly on multi-output setups
                "indices": sorted(ws.get("idx") for ws in (wss or [])
                                  if ws.get("idx") is not None),
            }
        except Exception:
            pass
        try:
            outs = _json_cli("msg", "--json", "outputs")
            try:
                focused = _json_cli("msg", "--json", "focused-output").get("name")
            except Exception:
                focused = None
            m["outputs"] = [
                {"name": name,
                 "make": str((o or {}).get("make") or "?")[:24],
                 "model": str((o or {}).get("model") or "?")[:24],
                 "scale": ((o or {}).get("logical") or {}).get("scale", 1.0),
                 "focused": name == focused}
                for name, o in (outs or {}).items()]
        except Exception:
            pass
        try:
            kl = _json_cli("msg", "--json", "keyboard-layouts")
            names = [str(n) for n in (kl or {}).get("names") or []]
            m["keyboard_layouts"] = {
                "names": names,
                "current": (names[(kl or {}).get("current_idx") or 0]
                            if names else None),
            }
        except Exception:
            pass
        acts = cls._niri_help_names(["niri", "msg", "action", "--help"],
                                    "Actions")
        if acts:
            m["actions"] = acts
        return m

    @tool(description=(
        "Live capability manifest of the niri compositor: version, open "
        "windows, workspaces, outputs (name and scale — needed to convert "
        "screenshot pixels to pointer coordinates), keyboard layouts, and "
        "every supported action on THIS version. Cached ~30s; refresh=true "
        "forces a fresh poll."),
        gates="",
        aliases={"refresh": ("force",)})
    def niri_capabilities(self, refresh: bool = False) -> str:
        """Report what the running compositor supports right now.

        refresh: true to bypass the 30s cache
        """
        m = self._niri_manifest(refresh=bool(refresh))
        out: list[str] = []
        if "version" in m:
            out.append(f"niri {m['version']}")
        elif "version_error" in m:
            out.append(f"niri: {m['version_error']}")
        w = m.get("windows")
        if w:
            out.append(f"windows: {w['count']}"
                       + (f", focused: {w['focused']}" if w.get("focused") else ""))
        if "workspaces" in m:
            out.append(f"workspaces: {m['workspaces']['count']}")
        outs = m.get("outputs")
        if outs:
            out.append("outputs: " + " ;; ".join(
                f"{o['name']} {o['model']} scale {o.get('scale', 1.0)}"
                + (" (focused)" if o.get("focused") else "")
                for o in outs))
        kl = m.get("keyboard_layouts")
        if kl and kl.get("names"):
            out.append("keyboard layouts: " + ", ".join(kl["names"])
                       + (f" [current: {kl['current']}]" if kl.get("current") else ""))
        acts = m.get("actions")
        if acts:
            shown = ", ".join(acts[:60])
            more = f" … (+{len(acts) - 60} more)" if len(acts) > 60 else ""
            out.append(f"actions ({len(acts)}): {shown}{more}")
        return "\n".join(out) or "niri capability manifest unavailable"

    @tool(description=(
        "Close an app's windows by name ('firefox', 'spotify') or 'this' for "
        "the focused one. Polite close (like Alt+F4): unsaved work prompts "
        "the user. Never force-kills."),
        gates="run_command",
        aliases={"app": ("window", "name")})
    def close_window(self, app: str) -> str:
        """Close window(s) politely.

        app: app name or title fragment, e.g. 'firefox'; 'this' = focused window
        """
        q = app.strip().lower()
        if not q:
            return ("REFUSED: name the app or window to close "
                    "(or pass 'this' for the focused window)")
        try:
            raw = subprocess.run(
                ["niri", "msg", "--json", "windows"],
                capture_output=True, text=True, timeout=8,
            )
            wins = json.loads(raw.stdout or "[]")
        except Exception as e:
            return f"ERROR: cannot list windows ({e})"
        if q in ("this", "focused", "current"):
            targets = [w for w in wins if w.get("is_focused")]
            if not targets:
                return "ERROR: no focused window"
        else:
            targets = [w for w in wins if self._win_matches(w, q)]
            if not targets:
                return ("ERROR: no window matching " + q + ". Open windows: "
                        + self._win_listing(wins))
        targets = [w for w in targets
                   if "handsoff" not in str(w.get("app_id", "")).lower()]
        if not targets:
            return ("REFUSED: I will not close my own bubble — "
                    "if a restart is needed, use self_restart")
        closed, failed = [], []
        for w in targets:
            try:
                r = self._niri_msg("msg", "action", "close-window",
                                   "--id", str(w.get("id")))
            except Exception as e:
                failed.append(f"{self._win_label(w)} ({e})")
                continue
            label = self._win_label(w)
            (closed if r.returncode == 0 else failed).append(label)
        if failed:
            return "ERROR: close failed for: " + " | ".join(failed)
        # post-action verification: a polite close can be declined (unsaved
        # work opens a dialog) — check the windows are really gone.
        lingering = list(closed)
        if closed:
            ids = {self._win_label(t): t.get("id") for t in targets}
            deadline = time.monotonic() + 2.0
            while lingering and time.monotonic() < deadline:
                try:
                    wins = self._niri_windows()
                except RuntimeError:
                    break
                live_ids = {w.get("id") for w in wins}
                lingering = [lbl for lbl in closed if ids.get(lbl) in live_ids]
                if lingering:
                    time.sleep(self._WIN_POLL_S)
        gone = [lbl for lbl in closed if lbl not in lingering]
        if gone:
            log.info("close_window: %s", "; ".join(gone))
        note = ""
        if gone:
            note += f"closed {len(gone)} window(s): " + " | ".join(gone)
            note += " (confirmed gone)"
        if lingering:
            note += ("; still open: " + " | ".join(lingering)
                     + " — the app may be asking about unsaved work")
        return note or "ERROR: nothing was closed"

    @tool(gates="copy_text", description="Copy text to the Wayland clipboard.",
          aliases={"text": ("content",)})
    def copy_text(self, text: str) -> str:
        if not text:
            return "REFUSED: nothing to copy"
        try:
            # DEVNULL, not capture_output: wl-copy forks a background child
            # that SERVES the clipboard and inherits our stdout pipe — with
            # capture_output, communicate() blocks on that inherited pipe
            # until the timeout and a SUCCESSFUL copy reports a false
            # "wl-copy timed out". No pipe, no false wait.
            subprocess.run(["wl-copy", "--", text],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=8)
        except FileNotFoundError:
            return "ERROR: wl-clipboard is not installed (pacman -S wl-clipboard)"
        except subprocess.TimeoutExpired:
            return "ERROR: wl-copy timed out"
        return f"copied {len(text)} chars to the clipboard"

    @tool(description=(
        "Read the clipboard content (wayland). Use to check what the user "
        "copied or to inspect before pasting."))
    def paste_text(self) -> str:
        try:
            r = subprocess.run(["wl-paste", "--no-newline"],
                               capture_output=True, text=True, timeout=8)
        except FileNotFoundError:
            return "ERROR: wl-clipboard is not installed (pacman -S wl-clipboard)"
        except subprocess.TimeoutExpired:
            return "ERROR: wl-paste timed out"
        data = r.stdout or ""
        if not data:
            return "clipboard is empty"
        head = data[:120].replace("\n", " ")
        more = f" (+{len(data) - 120} more chars)" if len(data) > 120 else ""
        return f"clipboard holds {len(data)} chars: {head!r}{more}"

    @tool(gates="reminders", description=(
        "Set a spoken reminder. when_due: 'in 45 minutes', '18:30' (next "
        "occurrence), or 'YYYY-MM-DD HH:MM'. repeat_hours>0 recurs "
        "(24=daily, 168=weekly)."),
        aliases={"when_due": ("when", "due", "at", "time", "in", "seconds"),
                 "wake_name": ("name", "label", "what", "text"),
                 "repeat_hours": ("repeat", "every")})
    def set_reminder(self, wake_name: str, when_due: str, repeat_hours: float = 0) -> str:
        name = str(wake_name or "").strip()
        arg = str(when_due or "").strip()
        if not name or not arg:
            return ("ERROR: need a reminder name and a due time "
                    "('in 45 minutes', '18:30', or 'YYYY-MM-DD HH:MM')")
        now = time.time()
        due: float | None = None
        if re.fullmatch(r"\d+(\.\d+)?", arg):                          # bare number = seconds
            due = now + float(arg)
        elif (m := re.fullmatch(r"(\d{1,2}):(\d{2})(:\d{2})?", arg)):  # 18:30 / 09:05:00
            h, mi = int(m.group(1)), int(m.group(2))
            se = int(m.group(3)[1:]) if m.group(3) else 0
            if h < 24 and mi < 60 and se < 60:
                due = _next_occurrence(h, mi, se, now)
        elif (m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", arg)):
            h = int(m.group(4)) if m.group(4) else 9
            mi = int(m.group(5)) if m.group(5) else 0
            try:
                due = datetime.datetime(int(m.group(1)), int(m.group(2)),
                                        int(m.group(3)), h, mi).timestamp()
            except ValueError:
                due = None
        if due is None:                                      # 'in 2 hours 5 minutes'
            tot = _parse_duration(arg)
            if tot is not None:
                due = now + tot
        if due is None:
            return ("ERROR: could not understand when_due. Use 'in N minutes/hours/"
                    "days/weeks', 'HH:MM' (next occurrence), or 'YYYY-MM-DD HH:MM'")
        if due <= now - 5:
            return "ERROR: that time is already in the past"
        if due - now > MAX_REMIND_DAYS * 86400:
            return "ERROR: reminders can be at most a year ahead"
        try:
            repeat = float(repeat_hours)
        except (TypeError, ValueError):
            return "ERROR: repeat_hours must be a number of hours"
        if repeat and (repeat < 1 / 60 or repeat > 24 * 31):
            return "ERROR: repeat_hours must be between ~1 minute and a month"
        name = name[:200]
        try:
            def _set(items: list[dict]) -> list[dict]:
                items = [r for r in items if r["name"].lower() != name.lower()]
                items.append({"name": name, "due": round(due, 3),
                              "repeat_hours": repeat})
                items.sort(key=lambda r: r["due"])
                del items[MAX_REMINDERS:]
                return items
            _update_reminders(_set)
        except OSError as e:
            return f"ERROR: could not save reminder ({type(e).__name__})"
        rep = f", repeating every {_fmt_dur(repeat * 3600)}" if repeat else ""
        return f"reminder '{name}' set for {_fmt_when(due)}{rep}"

    @tool(gates="reminders",
        description="List pending reminders, soonest first.")
    def list_reminders(self) -> str:
        now = time.time()
        items = [r for r in _load_reminders() if r["due"] > now - 86400]
        if not items:
            return "no pending reminders"
        out = []
        for r in items[:16]:
            try:
                rep_h = float(r.get("repeat_hours") or 0)
            except (TypeError, ValueError):
                rep_h = 0.0
            rep = (f" (repeats every {_fmt_dur(rep_h * 3600)})" if rep_h else "")
            out.append(f"{r['name']} {_fmt_when(r['due'])}{rep}")
        return "reminders: " + "; ".join(out)

    @tool(gates="reminders",
        description="Cancel a pending reminder by (part of its) name.")
    def cancel_reminder(self, name: str) -> str:
        name = str(name or "").strip().lower()
        if not name:
            return "ERROR: which reminder? (see list_reminders)"
        items = _load_reminders()
        matches = [r for r in items if r["name"].lower() == name
                   or (len(name) >= 3 and r["name"].lower().startswith(name))]
        if not matches:
            return f"no reminder matching {name!r}"
        if len(matches) > 1:
            listing = ", ".join(f"{m['name']} ({_fmt_when(m['due'])})"
                                for m in matches[:6])
            return f"several match: {listing} — cancel_reminder the exact name"
        victim = matches[0]["name"].lower()
        try:
            # match by NAME inside the transaction: the pre-lock snapshot's
            # objects are not the ones _update_reminders loads (identity
            # matching silently cancelled nothing)
            _update_reminders(
                lambda items: [r for r in items
                               if r["name"].lower() != victim])
        except OSError as e:
            return f"ERROR: could not save reminders ({type(e).__name__})"
        return f"cancelled reminder {matches[0]['name']!r} (was {_fmt_when(matches[0]['due'])})"

    @tool(gates="reminders",
        description="Snooze a reminder: re-arm it N minutes from now. "
                      "Works while pending or ~90s after it fired.")
    def snooze_reminder(self, name: str, minutes: float = 10) -> str:
        name = str(name or "").strip().lower()
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return "ERROR: minutes must be a number"
        if not (0.1 <= minutes <= 24 * 60):
            return "ERROR: minutes must be between 0.1 and 1440"
        items = _load_reminders()
        matches = [r for r in items if r["name"].lower() == name
                   or (len(name) >= 3 and r["name"].lower().startswith(name))]
        if not matches:
            # the reminder may have JUST fired (it was pruned from the file):
            # within the offer window it can still be re-armed by name
            with _SNOOZE_LOCK:
                offer = dict(_snooze_offer) if _snooze_offer else None
            if offer and time.monotonic() < offer["until"] and (
                    name == offer["name"].lower()
                    or offer["name"].lower().startswith(name)):
                return self._snooze(offer["name"], minutes, 0)
            return (f"no reminder matching {name!r} — if it already fired, say "
                    f"'snooze' within 90 seconds of the announcement")
        if len(matches) > 1:
            listing = ", ".join(f"{m['name']} ({_fmt_when(m['due'])})"
                                for m in matches[:6])
            return f"several match: {listing} — snooze_reminder the exact name"
        return self._snooze(matches[0]["name"], minutes,
                            float(matches[0].get("repeat_hours") or 0))

    def _snooze(self, name: str, minutes: float, repeat_hours: float) -> str:
        due = time.time() + minutes * 60
        try:
            def _rearm(items: list[dict]) -> list[dict]:
                items = [r for r in items if r["name"].lower() != name.lower()]
                items.append({"name": name, "due": round(due, 3),
                              "repeat_hours": repeat_hours})
                items.sort(key=lambda r: r["due"])
                del items[MAX_REMINDERS:]
                return items
            _update_reminders(_rearm)
        except OSError as e:
            return f"ERROR: could not save reminder ({type(e).__name__})"
        return f"reminder '{name}' snoozed until {_fmt_when(due)}"

    # -- media (MPD) ---------------------------------------------------------

    @tool(gates="media",
          description="Play music from MPD. No query: resume or shuffle "
                      "something random. With query: replace queue with "
                      "matching songs and play.")
    def media_play(self, query: str = "") -> str:
        query = str(query or "").strip()
        try:
            if not query:
                queue = _mpc("playlist").splitlines()
                if queue:
                    _mpc("play")
                    return f"playing (queue had {len(queue)} songs)"
                paths = _mpc("search", "filename", "").splitlines()
                if not paths:
                    return "ERROR: the MPD library is empty — nothing to play"
                picks = random.sample(paths, min(20, len(paths)))
                _mpc("clear")
                _mpc("add", *picks)
                _mpc("play")
                return f"shuffled {len(picks)} random songs from your library and started playing"
            paths: list[str] = []
            for field in ("title", "artist", "album", "filename"):
                found = _mpc("search", field, query).splitlines()
                for p in found:
                    if p not in paths:
                        paths.append(p)
                if paths:
                    break          # most specific field that matched
            if not paths:
                return (f"ERROR: nothing in the library matches {query!r} — "
                        "try search_library to see what's available")
            picks = paths[:100]
            _mpc("clear")
            _mpc("add", *picks)
            _mpc("play")
            extra = f" (and {len(paths) - 1} more matches queued)" if len(paths) > 1 else ""
            return f"now playing {picks[0]}{extra}"
        except RuntimeError as e:
            return f"ERROR: {e}"

    @tool(gates="media",
          description="Control the music player: action is one of 'play' "
                      "(resume), 'pause', 'toggle', 'stop', 'next', 'previous'.")
    def media_control(self, action: str) -> str:
        a = str(action or "").strip().lower()
        mapped = {"play": ["play"], "pause": ["pause"], "stop": ["stop"],
                  "next": ["next"], "skip": ["next"],
                  "previous": ["prev"], "prev": ["prev"], "back": ["prev"]}
        # this mpc build: bare 'pause' is a PURE pause (idempotent, never
        # resumes) and 'pause 1' is rejected — so toggle checks state itself
        if a == "toggle":
            try:
                paused = "[paused]" in _mpc("status")
                _mpc("play" if paused else "pause")
                return f"music player: {'play' if paused else 'pause'}"
            except RuntimeError as e:
                return f"ERROR: {e}"
        if a not in mapped:
            return ("ERROR: action must be one of play, pause, toggle, stop, "
                    "next, previous")
        try:
            _mpc(*mapped[a])
            return f"music player: {a}"
        except RuntimeError as e:
            return f"ERROR: {e}"

    @tool(gates="media",
          description="Set the music player's volume (0-100). This is the music "
                      "output volume, not the whole system volume.")
    def media_volume(self, level: str) -> str:
        # str param + own parse: the int schema coercion would silently turn
        # garbage into 0 and blast the volume down
        s = str(level or "").strip().rstrip("%")
        if not re.fullmatch(r"[+-]?\d+", s):
            return "ERROR: level must be a number 0-100"
        level = max(0, min(100, int(s)))
        try:
            _mpc("volume", str(level))
            return f"music volume set to {level}%"
        except RuntimeError as e:
            return f"ERROR: {e}"

    @tool(gates="media",
          description="What is currently playing: song, playing/paused state, "
                      "position, volume, queue length. Use for 'what's this song?'.")
    def now_playing(self) -> str:
        try:
            cur = _mpc("current").strip()
            status = _mpc("status")
        except RuntimeError as e:
            return f"ERROR: {e}"
        if not cur:
            return "nothing is playing (the music queue is empty)"
        state = "playing" if "[playing]" in status else "paused"
        vol = re.search(r"volume:\s*(\d+)%", status)
        queue = re.search(r"\[\w+\]\s+#(\d+)/(\d+)", status)
        pos = ""
        if queue:
            pos = f", song {queue.group(1)} of {queue.group(2)} in the queue"
        return f"{cur} [{state}{pos}{', volume ' + vol.group(1) + '%' if vol else ''}]"

    @tool(gates="media",
          description="Search the music library (artist/title/album/filename). "
                      "Use before playing vague requests. Returns up to 15 paths.")
    def search_library(self, query: str) -> str:
        query = str(query or "").strip()
        if not query:
            return "ERROR: what should I search for?"
        try:
            paths: list[str] = []
            for field in ("title", "artist", "album", "filename"):
                for p in _mpc("search", field, query).splitlines():
                    if p not in paths:
                        paths.append(p)
            if not paths:
                return f"no songs in the library match {query!r}"
            head = paths[:15]
            extra = f" — and {len(paths) - 15} more" if len(paths) > 15 else ""
            return (f"{len(paths)} match(es): " + "; ".join(head) + extra
                    + " — play one with media_play")
        except RuntimeError as e:
            return f"ERROR: {e}"

    @tool(gates="calendar",
          description="Print a calendar month grid with today marked * — "
                      "reason about weekdays/day counts. month: 'YYYY-MM' or empty.")
    def calendar_month(self, month: str = "") -> str:
        arg = str(month or "").strip().lower()
        today = datetime.date.today()
        if not arg or arg in ("this", "current", "now"):
            y, mo = today.year, today.month
        elif (m := re.fullmatch(r"(\d{4})-(\d{1,2})", arg)):
            y, mo = int(m.group(1)), int(m.group(2))
        else:
            return "ERROR: month must look like 'YYYY-MM' (or empty for this month)"
        try:
            first = datetime.date(y, mo, 1)
        except ValueError:
            return f"ERROR: no such month {y}-{mo:02d}"
        nxt_y, nxt_mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        ndays = (datetime.date(nxt_y, nxt_mo, 1) - first).days
        head = f"{_MONTH_NAMES[mo - 1]} {y}".center(21).rstrip()
        rows = [head, "Mo Tu We Th Fr Sa Su"]
        row = "   " * first.weekday()
        for d in range(1, ndays + 1):
            mark = "*" if (d == today.day and y == today.year and mo == today.month) else " "
            row += f"{d:2d}{mark}"
            if (first.weekday() + d) % 7 == 0:
                rows.append(row.rstrip())
                row = ""
        if row.strip():
            rows.append(row.rstrip())
        rows.append(f"today is {_DAY_NAMES[today.weekday()]} {today.day:02d} "
                    f"{_MONTH_NAMES[today.month - 1][:3]} {today.year}")
        return "\n".join(rows)

    @tool(gates="calendar", description=(
        "Read upcoming events from the configured ICS calendar source(s). "
        "days=1 = today, 2 = today+tomorrow."),
        aliases={"days": ("how_many_days", "range")})
    def read_calendar(self, days: int = 1) -> str:
        sources = SETTINGS.get("calendar_ics") or []
        if not sources:
            return ("no calendar is configured — add an ICS source in handsoff "
                    "Settings (a Google Calendar 'secret iCal address' URL or a "
                    "local .ics file path)")
        try:
            days = max(1, min(14, int(days)))
        except (TypeError, ValueError):
            return "ERROR: days must be a number (1-14)"
        now = datetime.datetime.now()
        win_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        win_end = win_start + datetime.timedelta(days=days)
        events: list[dict] = []
        bad: list[str] = []
        for src in sources:
            if re.match(r"^https?://", src, re.I) \
                    and not self._perm.get("web_access", True):
                return ("REFUSED: fetching calendar URLs needs the 'web_access' "
                        "permission, disabled in handsoff settings")
            try:
                events.extend(_ics_events_from_text(_ics_fetch(src),
                                                    win_start, win_end))
            except Exception as e:
                bad.append(f"{src} ({type(e).__name__})")
        if bad and not events:
            return "ERROR: could not read calendar source(s): " + "; ".join(bad)
        if not events:
            return f"no events in the next {days} day(s)"
        label = "Today" if days == 1 else f"Next {days} days"
        out = _fmt_events(events)
        if bad:
            out += f"  [unreadable: {'; '.join(bad)}]"
        return f"{label}: {out}"

    # -- knowledge (read-only internet, fixed safe endpoints) ------------------

    @tool(description=(
        "Current weather + today/tomorrow forecast for a place, e.g. "
        "'Berlin', 'New York', 'Tokyo'. Omit the place to use the user's "
        "home place."),
        gates="web_access",
        aliases={"place": ("city",)})
    def get_weather(self, place: str = "") -> str:
        place = (place or str(SETTINGS.get("home_place", ""))).strip()
        if not place:
            return "ERROR: name a place, e.g. 'Hamburg'"
        try:
            geo = _geocode(place)
            if not geo:
                return f"ERROR: unknown place: {place}"
            lat, lon, where = geo
            data = json.loads(_http_get(
                f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
                "weather_code,wind_speed_10m"
                "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
                "weather_code&forecast_days=2&timezone=auto"))
            cur = data["current"]
            desc = _WMO.get(int(cur["weather_code"]), "changeable sky")
            d = data["daily"]
            lines = [
                f"Weather in {where} (local time {cur['time'][-5:]}):",
                f"now: {cur['temperature_2m']}°C, feels like {cur['apparent_temperature']}°C, "
                f"{desc}, humidity {cur['relative_humidity_2m']}%, wind {cur['wind_speed_10m']} km/h",
                f"today: {d['temperature_2m_min'][0]}–{d['temperature_2m_max'][0]}°C, "
                f"{_WMO.get(int(d['weather_code'][0]), 'changeable')}, "
                f"precip {d['precipitation_sum'][0]} mm",
                f"tomorrow: {d['temperature_2m_min'][1]}–{d['temperature_2m_max'][1]}°C, "
                f"{_WMO.get(int(d['weather_code'][1]), 'changeable')}, "
                f"precip {d['precipitation_sum'][1]} mm",
            ]
            return "\n".join(lines)
        except Exception as e:
            log.warning("get_weather failed: %s", e)
            return f"ERROR: weather lookup failed ({type(e).__name__})"

    @tool(description=(
        "Search the public web for current information (news, prices, "
        "scores, facts that may have changed recently). Returns result "
        "titles with snippets."),
        gates="web_access",
        aliases={"query": ("q",)})
    def web_search(self, query: str) -> str:
        if not query:
            return "REFUSED: empty query"
        try:
            results = _ddg_lite(query)
            source = "web"
            if not results:
                results = _wiki_search(query)
                source = "wikipedia"
            if not results:
                return f"ERROR: no results for {query!r}"
            lines = [f"Results for '{query}' ({source}):"]
            for t, s in results[:4]:
                lines.append(f"- {t}: {s[:220]}")
            return "\n".join(lines)
        except Exception as e:
            log.warning("web_search failed: %s", e)
            return f"ERROR: web search failed ({type(e).__name__})"

    @tool(description=("World news headlines."),
        gates="web_access",
        aliases={"count": ("n", "limit")})
    def world_events(self, count: int = 4) -> str:
        """Show current world headlines."""
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = 4
        n = max(1, min(n, 5))
        events, degraded = _world_events("all", n)
        if not events:
            return ("ERROR: world news unavailable (offline?)"
                    if degraded else "no world headlines right now")
        lines = ["World headlines:"]
        for e in events:
            mark = "⚠ " if e.get("urgent") else ""
            lines.append(f"- {mark}{e['title']}")
        # ponytail: read-only — reading headlines must never consume warnings
        return "\n".join(lines)

    @tool(description=(
        "Look up an encyclopedia summary about a person, place, thing or "
        "concept (Wikipedia). Better than web_search for stable facts."),
        gates="web_access",
        aliases={"topic": ("subject", "query")})
    def lookup_fact(self, topic: str) -> str:
        if not topic:
            return "REFUSED: name a topic"
        try:
            t = urllib.parse.quote(topic.strip().replace(" ", "_"))
            data = json.loads(_http_get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + t))
            if data.get("type") == "standard" and data.get("extract"):
                return f"{data.get('title')}: {data['extract'][:1200]}"
            # no direct article: fall back to search
            hits = _wiki_search(topic)
            if hits:
                return "Top matches: " + "; ".join(f"{a} — {b[:120]}" for a, b in hits[:3])
            return f"ERROR: nothing found on {topic!r}"
        except Exception as e:
            log.warning("lookup_fact failed: %s", e)
            return f"ERROR: fact lookup failed ({type(e).__name__})"

    @tool(description="Current local date, weekday and time.")
    def get_datetime(self) -> str:
        now = datetime.datetime.now()
        return (f"Local date and time: {_DAY_NAMES[now.weekday()]}, "
                f"{now.day:02d} {_MONTH_NAMES[now.month - 1]} {now.year}, "
                f"{now.hour:02d}:{now.minute:02d} "
                f"(timezone {now.astimezone().tzname()}). "
                f"Unix epoch: {int(now.timestamp())}")

    # -- eyes: screen vision (screenshot attached as an image to the result) ---

    SCREENSHOT_FILE = STATE_DIR / "screen.png"

    @staticmethod
    def _shot_path(region: str):
        """Build a safe grim argv. Returns (argv_tail, path) or None."""
        path = ToolBelt.SCREENSHOT_FILE
        if not region:
            return [], path
        try:
            g = re.fullmatch(
                r"(\d{1,5})[ ,xX]+(\d{1,5})[ ,xX]+(\d{1,5})[ ,xX]+(\d{1,5})",
                region.strip())
            if not g:
                return None
            x, y, w, hgt = (int(v) for v in g.groups())
            if not (0 <= x <= 20000 and 0 <= y <= 20000
                    and 0 < w <= 20000 and 0 < hgt <= 20000):
                return None
            return ["-g", f"{x},{y} {w}x{hgt}"], path
        except Exception:
            return None

    def _take_screenshot(self, region: str = "", scale_down: bool = True) -> str:
        """Capture the screen (or a region) to SCREENSHOT_FILE. Returns error or ''."""
        built = self._shot_path(region)
        if built is None:
            return "ERROR: region must be 'x y width height' in pixels"
        tail, path = built
        try:
            path.unlink(missing_ok=True)
            argv = ["grim"]
            if scale_down:
                argv += ["-s", "0.6"]
            proc = subprocess.run(argv + tail + [str(path)],
                                  capture_output=True, text=True, timeout=15)
            if proc.returncode != 0 or not path.exists():
                return "ERROR: screenshot failed: " + (proc.stderr or "unknown").strip()[:200]
        except FileNotFoundError:
            return "ERROR: grim is not installed (pacman -S grim)"
        except subprocess.TimeoutExpired:
            return "ERROR: screenshot timed out"
        return ""

    @tool(description=(
        "Take a screenshot and see it as an image. Use for 'what's on my "
        "screen', non-text UI, verifying what you typed."),
        gates="screen_access")
    def see_screen(self, question: str = "", region: str = "") -> str:
        """Look at the screen with vision.

        question: what you want to find out from the screen
        region: optional 'x y width height' in pixels
        """
        err = self._take_screenshot(region)
        if err:
            return err
        try:
            b64 = base64.b64encode(self.SCREENSHOT_FILE.read_bytes()).decode("ascii")
        except OSError as e:
            return f"ERROR: cannot read screenshot: {e}"
        self._last_images = [b64]
        hint = f" (user asks: {question[:200]})" if question else ""
        return f"Screenshot captured and attached as an image{hint}."

    @tool(description=(
        "Read all visible text on screen via OCR (fast, no vision model). "
        "Best for reading articles, chats, code or error messages on "
        "screen."),
        gates="screen_access")
    def read_screen_text(self, region: str = "") -> str:
        """Read screen text via OCR.

        region: optional 'x y width height' in pixels
        """
        err = self._take_screenshot(region, scale_down=False)
        if err:
            return err
        try:
            proc = subprocess.run(
                ["tesseract", str(self.SCREENSHOT_FILE), "stdout", "--psm", "3"],
                capture_output=True, text=True, timeout=40)
        except FileNotFoundError:
            return "ERROR: tesseract is not installed (pacman -S tesseract)"
        except subprocess.TimeoutExpired:
            return "ERROR: OCR timed out"
        text = " ".join(proc.stdout.split())
        if not text:
            return "OCR found no readable text on screen."
        log.info("read_screen_text: %d chars", len(text))
        return f"Text on screen: {text[:4000]}"

    # -- process management (scoped: same-user, exact match, confirmed) --------

    KILL_CONFIRM_S = 60.0          # how long a spoken 'confirm kill' stays valid

    @staticmethod
    def _same_user_procs() -> list[tuple[int, str]]:
        """[(pid, name)] for every process owned by the CURRENT user — the
        AI can never see (or kill) other users' processes, including root."""
        out = []
        me = os.getuid() if hasattr(os, "getuid") else -1
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/status", encoding="ascii",
                          errors="replace") as fh:
                    fields = dict(line.split(":", 1)
                                  for line in fh if ":" in line)
                if int(fields.get("Uid", "-1\t-1").split()[0]) != me:
                    continue
                name = fields.get("Name", "").strip()
                if name:
                    out.append((int(entry), name))
            except (OSError, ValueError, KeyError, IndexError):
                continue
        return out

    @staticmethod
    def _port_owner(port: int) -> list[int]:
        """PIDs of same-user processes with a LISTEN socket on this port
        (parsed from /proc/net/tcp{,6}; no external tools)."""
        inodes: set[str] = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path, encoding="ascii") as fh:
                    next(fh)                       # header
                    for line in fh:
                        f = line.split()
                        if len(f) < 10 or f[3] != "0A":   # 0A = LISTEN
                            continue
                        try:
                            if int(f[1].split(":")[1], 16) == port:
                                inodes.add(f[9])
                        except (ValueError, IndexError):
                            continue
            except OSError:
                continue
        pids = []
        me = os.getuid() if hasattr(os, "getuid") else -1
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/status", encoding="ascii",
                          errors="replace") as fh:
                    fields = dict(line.split(":", 1)
                                  for line in fh if ":" in line)
                if int(fields.get("Uid", "-1\t-1").split()[0]) != me:
                    continue
                for fd in os.listdir(f"/proc/{entry}/fd"):
                    try:
                        link = os.readlink(f"/proc/{entry}/fd/{fd}")
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        ino = link[8:-1]
                        if ino in inodes:
                            pids.append(int(entry))
                            break
            except (OSError, ValueError, KeyError, IndexError):
                continue
        return pids

    @tool(gates="run_command", description=(
        "Stop (SIGTERM) one of the user's own processes by EXACT name or by "
        "the port it listens on. Two steps: kill_process first shows the match "
        "and asks to confirm; then confirm_kill('yes') actually stops it."))
    def kill_process(self, target: str) -> str:
        target = str(target or "").strip()
        if not target:
            return "ERROR: name the process or the port it listens on"
        cands: list[tuple[int, str]] = []
        if target.isdigit() and 0 < int(target) <= 65535:
            port = int(target)
            for pid in self._port_owner(port):
                name = next((n for p, n in self._same_user_procs()
                             if p == pid), str(pid))
                cands.append((pid, name))
        else:
            low = target.lower()
            cands = [(p, n) for p, n in self._same_user_procs()
                     if n.lower() == low]
        if not cands:
            return (f"ERROR: no process of yours matches {target!r} "
                    "(exact name or listening port; other users' processes "
                    "are invisible)")
        if len(cands) > 1:
            listing = ", ".join(f"{n} (pid {p})" for p, n in cands[:6])
            return (f"ERROR: {len(cands)} processes match — kill_process needs "
                    f"an EXACT single match, these all match: {listing}")
        pid, name = cands[0]
        if pid == os.getpid():
            return ("REFUSED: that is me — for a restart of the assistant, "
                    "ask me to restart myself instead")
        if name == "systemd":
            # the user's own systemd --user manager: killing it would end
            # every user service (including this bubble) at once
            return ("REFUSED: systemd --user manages your whole session — "
                    "killing it would stop every user service, including me")
        _kill_offer.clear()
        _kill_offer.update({"pid": pid, "name": name,
                            "until": time.monotonic() + self.KILL_CONFIRM_S})
        log.info("kill_process: offered pid %d (%s), awaiting confirm", pid, name)
        return (f"About to stop {name} (pid {pid}). Nothing happened yet — "
                "call confirm_kill('yes') to stop it, or confirm_kill('no') "
                "to cancel.")

    @tool(gates="run_command", description=(
        "Second step of kill_process: confirm_kill('yes') stops the offered "
        "process; confirm_kill('no') cancels the offer."))
    def confirm_kill(self, answer: str = "yes") -> str:
        offer = dict(_kill_offer) if _kill_offer else None
        if not offer:
            return "ERROR: nothing to confirm — call kill_process first"
        if time.monotonic() >= offer["until"]:
            _kill_offer.clear()
            return "ERROR: the kill offer expired — run kill_process again"
        ans = str(answer or "yes").strip().lower()
        if ans not in ("yes", "no", "y", "n"):
            return "ERROR: answer with yes or no"
        if ans in ("no", "n"):
            _kill_offer.clear()
            log.info("kill_process: cancelled by user/model")
            return "Cancelled — nothing was stopped."
        pid, name = offer["pid"], offer["name"]
        _kill_offer.clear()
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return f"{name} (pid {pid}) already exited."
        except PermissionError:
            return f"ERROR: not allowed to stop {name} (pid {pid})"
        log.warning("kill_process: SIGTERM pid %d (%s) confirmed", pid, name)
        return f"Stopped {name} (pid {pid}) (SIGTERM sent)."

    # -- centralized confirm (one-turn separation for CONFIRM-class tools) ----

    @tool(description=(
        "Second step for a CONFIRM-offered tool: 'yes' runs, 'no' cancels."),
        gates="", aliases={"answer": ("confirm", "reply")})
    def confirm_action(self, answer: str = "yes") -> str:
        offer = self._pending_confirm
        if not offer:
            return "ERROR: nothing to confirm — no CONFIRM-class tool call is pending"
        if time.monotonic() >= offer["until"]:
            self._pending_confirm = None
            return "ERROR: the confirmation offer expired — make the request again"
        ans = str(answer or "yes").strip().lower()
        if ans not in ("yes", "no", "y", "n"):
            return "ERROR: answer with yes or no"
        if ans in ("no", "n"):
            tool = offer["tool"]
            self._pending_confirm = None
            log_decision(tool, "", "CONFIRM", "cancelled by user")
            log.info("confirm_action: %s cancelled", tool)
            return f"Cancelled — {tool} was not run."
        # yes: run the offered call now with the ORIGINAL arguments. The
        # _confirm_running bypass stops the inner execute() from making a
        # fresh offer (which would loop offers forever).
        self._pending_confirm = None
        tool, args = offer["tool"], offer["args"]
        log_decision(tool, json.dumps(args)[:120], "CONFIRM", "confirmed; running")
        self._confirm_running = tool
        try:
            out, err = self.execute(tool, args)
        finally:
            self._confirm_running = None
        log_decision(tool, json.dumps(args)[:120], "EXECUTED",
                     "refused/errored" if err else "ok")
        return out

    # -- bounded background jobs (long-running whitelisted commands) ----------

    JOB_ANNOUNCE_S = 20.0   # first job_status poll that fast announces on finish

    @tool(description=(
        "Run a whitelisted command as a background job."),
        gates="run_command")
    def start_command(self, command: str) -> str:
        """Run a whitelisted command as a bounded background job.

        command: same single whitelisted command run_command accepts
        """
        with self._job_lock:
            if len(self._jobs) >= BoundedJob.MAX_JOBS:
                return (f"ERROR: job limit reached ({BoundedJob.MAX_JOBS}) — "
                        f"check or reap with job_status first: "
                        f"{', '.join(sorted(self._jobs))}")
        # identical gate text as run_command on refusal: the policy is the
        # whitelist, not the execution mode. Validate WITHOUT executing —
        # run_command() would run the command synchronously first (double-exec).
        argv, _exe, err, is_restart = self._validate_command(command)
        if err:
            return err
        log.info("start_command: %s", command.strip())
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
        except OSError as e:
            return f"ERROR: launch failed: {e}"
        if is_restart:
            self._on_restart_pending()
        with self._job_lock:
            self._job_seq += 1
            job_id = f"job-{self._job_seq}"
            self._jobs[job_id] = BoundedJob(job_id, command.strip(), proc)
        return (f"started {job_id}: {command.strip()} — it runs in the "
                "background; call job_status to check it. I will announce "
                "when it finishes.")

    @tool(description=(
        "State/output of start_command jobs; finished announced"),
        gates="run_command", aliases={"job_id": ("id", "job")})
    def job_status(self, job_id: str = "") -> str:
        """Report state/output of background jobs.

        job_id: a specific job id, or empty for all jobs
        """
        with self._job_lock:
            jobs = dict(self._jobs)
        if job_id:
            job = jobs.get(job_id.strip())
            if job is None:
                known = ", ".join(sorted(jobs)) or "none"
                return f"ERROR: no job {job_id!r} (jobs: {known})"
            jobs = {job_id.strip(): job}
        if not jobs:
            return "no background jobs"
        lines: list[str] = []
        reaped: list[str] = []
        for jid, job in sorted(jobs.items()):
            state, done = job.poll()
            if done:
                if not job._announced:
                    job._announced = True
                    self._announce_job(job.status_text())
                # bounded output tail from the drain thread (never read()
                # the pipe after poll — the child would block at ~64k)
                out = job.output_tail(4096)
                out = (out or "").strip()
                if len(out) > BoundedJob.MAX_OUTPUT:
                    out = out[:BoundedJob.MAX_OUTPUT] + " …(truncated)"
                lines.append(f"{job.status_text()}\noutput:\n{out or '(no output)'}")
                if state != "running":
                    reaped.append(jid)
            else:
                lines.append(job.status_text())
        with self._job_lock:
            for jid in reaped:
                self._jobs.pop(jid, None)
        return "\n\n".join(lines)

    # -- doctor (deployment + dependency diagnostics) --------------------------

    @tool(description=(
        "Self-diagnostic: deployment hashes, Ollama, TTS/STT, mic, niri, "
        "systemd. Read-only"),
        gates="")
    def handsoff_doctor(self) -> str:
        """Run the doctor diagnostic and return the report."""
        return run_doctor()

    # -- operator: element-grounded clicking (Self-Operating-Computer pattern) --

    @staticmethod
    def _parse_tsv(tsv: str) -> list[dict]:
        """Tesseract TSV -> line-level elements with pixel boxes.

        Rows: level page block par line word left top width height conf text.
        Words are grouped by (block, par, line) into one element per visual
        line, keeping the union bounding box and the mean confidence."""
        rows: dict[tuple, list] = {}
        for line in tsv.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12:
                continue
            try:
                level, blk, par, ln = (int(parts[0]), int(parts[2]),
                                       int(parts[3]), int(parts[4]))
                x, y, w, h, conf = (int(parts[6]), int(parts[7]),
                                    int(parts[8]), int(parts[9]),
                                    float(parts[10]))
            except ValueError:
                continue
            word = parts[11].strip()
            if level != 5 or not word or conf < 30:
                continue
            rows.setdefault((blk, par, ln), []).append((x, y, w, h, word))
        out = []
        for words in rows.values():
            words.sort(key=lambda t: t[0])
            x0 = min(w[0] for w in words)
            y0 = min(w[1] for w in words)
            x1 = max(w[0] + w[2] for w in words)
            y1 = max(w[1] + w[3] for w in words)
            out.append({"text": " ".join(w[4] for w in words),
                        "x": (x0 + x1) // 2, "y": (y0 + y1) // 2,
                        "w": x1 - x0, "h": y1 - y0})
        out.sort(key=lambda e: (e["y"], e["x"]))
        return out[:80]

    def _screen_elements_fmt(self) -> str:
        listing = "\n".join(
            f"{i + 1}. {e['text'][:70]!r} at ({e['x']},{e['y']})"
            for i, e in enumerate(getattr(self, "_elements", [])[:80]))
        return listing

    def _operator_click(self, x: int, y: int, what: str) -> str:
        if not self._perm.get("operator", False):
            return ("REFUSED: mouse control ('operator') is disabled in "
                    "handsoff settings")
        try:
            x, y = int(x), int(y)
            assert 0 <= x <= 20000 and 0 <= y <= 20000
        except (TypeError, ValueError, AssertionError):
            return f"ERROR: invalid click target ({x}, {y})"
        pre_note = self._stale_scan_note()
        # screen pixels (grim/OCR) → pointer space (logical): divide by the
        # focused output's scale; 1.0 when unknown = plain pass-through
        scale = getattr(self, "_pointer_scale", 1.0) or 1.0
        px = max(0, int(round(x / scale)))
        py = max(0, int(round(y / scale)))
        r1 = self._ydotool("mousemove", "-a", "-x", str(px), "-y", str(py))
        if r1 != "ok":
            return f"ERROR: mouse move failed: {r1}"
        r2 = self._ydotool("click", "0xC0")
        if r2 != "ok":
            return f"ERROR: click failed: {r2}"
        self._mark_elements_stale()
        conv = "" if scale == 1.0 else f" → pointer ({px},{py})"
        log.info("operator: clicked %s at (%d,%d)%s", what, x, y, conv)
        return f"clicked {what} at ({x},{y}){conv}{pre_note}"

    def _detect_pointer_scale(self) -> float:
        """Pointer-space scale of the focused output.

        grim screenshots and tesseract coordinates are PHYSICAL pixels while
        ydotool mousemove -a moves the LOGICAL pointer — clicks must divide
        by this scale. Falls back to 1.0 when niri cannot be asked (CI, IPC
        down): on scale-1 setups the no-op is exactly right."""
        try:
            r = self._niri_msg("msg", "--json", "focused-output")
            out = json.loads(r.stdout or "null")
            scale = float((out.get("logical") or {}).get("scale") or 1.0)
            if scale > 0:
                return scale
        except Exception:
            pass
        return 1.0

    def _mark_elements_stale(self) -> None:
        """Forget the freshness of the last screen_elements scan — the
        screen just changed (typing, keys, click, scroll, launch…)."""
        self._elements_ts = 0.0

    def _stale_scan_note(self) -> str:
        """Warning suffix when the cached element scan may be outdated."""
        ts = getattr(self, "_elements_ts", 0.0)
        if not ts:
            return ""
        age = time.monotonic() - ts
        if age > 90:
            return (f" (element scan is {age:.0f}s old — the screen may "
                    "have changed; run screen_elements again)")
        return ""

    @tool(gates="screen_access", description=(
        "List clickable text elements on screen with numbers and positions. "
        "Run this before click_element; re-run after anything changes — "
        "clicks, typing, scrolling and launches all make the last scan "
        "stale, and click_element will warn when it is."))
    def screen_elements(self) -> str:
        if not hasattr(self, "_elements"):
            self._elements = []
        err = self._take_screenshot("", scale_down=False)
        if err:
            return err
        try:
            proc = subprocess.run(
                ["tesseract", str(self.SCREENSHOT_FILE), "stdout", "tsv"],
                capture_output=True, text=True, timeout=40)
        except FileNotFoundError:
            return "ERROR: tesseract is not installed (pacman -S tesseract)"
        except subprocess.TimeoutExpired:
            return "ERROR: OCR timed out"
        self._elements = self._parse_tsv(proc.stdout)
        if not self._elements:
            self._mark_elements_stale()
            return "No clickable text elements found on screen."
        # refresh the pointer scale alongside the scan it will be applied to
        self._pointer_scale = self._detect_pointer_scale()
        self._elements_ts = time.monotonic()
        log.info("screen_elements: %d lines (pointer scale %.2f)",
                 len(self._elements), self._pointer_scale)
        return (f"{len(self._elements)} clickable text elements "
                f"(coordinates are screen pixels; pointer scale "
                f"{self._pointer_scale:g}):\n" + self._screen_elements_fmt())

    @tool(gates="operator", description=(
        "Click a text element from the last screen_elements scan by its "
        "number or (part of) its text. Run screen_elements first."),
        aliases={"ref": ("element", "name", "label", "target")})
    def click_element(self, ref: str) -> str:
        els = getattr(self, "_elements", [])
        if not els:
            return "ERROR: no element scan yet — run screen_elements first"
        ref_s = str(ref).strip().lower()
        pick = None
        if ref_s.isdigit() and 1 <= int(ref_s) <= len(els):
            pick = els[int(ref_s) - 1]
        else:
            exact = [e for e in els if ref_s == e["text"].strip().lower()]
            part = [e for e in els if ref_s in e["text"].strip().lower()]
            pick = (exact or part or [None])[0]
        if pick is None:
            return (f"ERROR: no element matching {ref!r} — run screen_elements "
                    "again and pick from the list")
        return self._operator_click(pick["x"], pick["y"],
                                    repr(pick["text"][:40]))

    @tool(gates="operator", description=(
        "Click at absolute pixel coordinates. Prefer click_element with a "
        "screen_elements scan."))
    def click_at(self, x: int, y: int) -> str:
        return self._operator_click(x, y, "target")

    # ydotool wheel mode passes -y straight to REL_WHEEL with no sign flip,
    # and libinput defines REL_WHEEL +1 as wheel-up; horizontal REL_HWHEEL
    # +1 is tilt-right. Flip these signs if a ydotool update inverts them.
    _SCROLL_SIGNS = {"up": 1, "down": -1, "right": 1, "left": -1}

    @tool(gates="operator", description=(
        "Scroll the mouse wheel by `amount` notches (default 3): direction "
        "up / down / left / right. Affects whatever window is under the "
        "pointer — click_element or click_at first to aim it. Content "
        "moves, so re-run screen_elements before clicking anything after."),
        aliases={"direction": ("dir", "way"),
                 "amount": ("notches", "clicks", "lines")})
    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        """Scroll the mouse wheel.

        direction: 'up', 'down', 'left' or 'right'
        amount: wheel notches (1-25)
        """
        # Keep the capability boundary inside the method as well as in the
        # dispatcher.  Unit callers and any future internal route must not be
        # able to bypass the operator permission by invoking scroll directly.
        if not getattr(self, "_perm", {}).get("operator", False):
            return ("REFUSED: mouse control ('operator') is disabled in "
                    "handsoff settings")
        d = str(direction or "down").strip().lower()
        if d not in self._SCROLL_SIGNS:
            return "REFUSED: direction must be up, down, left or right"
        try:
            n = int(amount)
        except (TypeError, ValueError):
            n = 3
        n = max(1, min(n, 25))
        pre_note = self._stale_scan_note()
        sign = self._SCROLL_SIGNS[d]
        if d in ("up", "down"):
            argv = ("mousemove", "-w", "-x", "0", "-y", str(sign * n))
        else:
            argv = ("mousemove", "-w", "-x", str(sign * n), "-y", "0")
        r = self._ydotool(*argv)
        if r != "ok":
            return f"ERROR: scroll failed: {r}"
        self._mark_elements_stale()
        log.info("scroll: %s x%d", d, n)
        return f"scrolled {d} {n} notch(es){pre_note}"

    # -- app launching (focused, safe aliases) ---------------------------------

    APP_ALIASES = {
        "browser": ("firefox", "chromium", "google-chrome-stable"),
        "web": ("firefox", "chromium", "google-chrome-stable"),
        "internet": ("firefox", "chromium"),
        "files": ("nautilus", "dolphin", "thunar", "nemo"),
        "file manager": ("nautilus", "dolphin", "thunar"),
        "calculator": ("kcalc", "qalculate-gtk", "gnome-calculator", "xcalc"),
        "music": ("spotify", "lollypop", "rhythmbox", "elisa"),
        "settings": ("gnome-control-center", "systemsettings", "xfce4-settings"),
        "text editor": ("gedit", "kate", "mousepad", "gnome-text-editor"),
        "editor": ("gedit", "kate", "mousepad"),
        "terminal": ("foot", "alacritty", "kitty"),
    }

    # how long open_app waits for the launched app to map a window before
    # giving up on identification (browsers/IDEs take 2-8 s on a cold start)
    OPEN_APP_WAIT_S = 12.0
    # how long open_app trusts "any new window" / "already open" fallbacks
    # before them: within the grace period a name-matched window may still
    # appear, which beats both
    _WIN_GRACE_S = 3.0

    @tool(description=(
        "Launch a desktop app by name ('firefox', 'spotify', 'files'…), "
        "wait for its window to appear and report which window it is. "
        "Then focus_window to aim typing at it."),
        gates="run_command",
        aliases={"app": ("name",)})
    def open_app(self, app: str) -> str:
        a = app.strip().lower()
        if not a:
            return "REFUSED: name the app to open"
        candidates = self.APP_ALIASES.get(a, (a,))
        resolved = None
        chosen = a
        for cand in candidates:
            cand = re.sub(r"[^a-z0-9._-]", "", cand)   # strict binary-name charset
            if not cand or cand in ("sudo", "bash", "sh", "python", "python3", "xterm"):
                return f"REFUSED: won't open '{app}'"
            resolved = shutil.which(cand)
            if resolved:
                chosen = cand
                break
        if resolved is None:
            return f"ERROR: no program matching '{app}' is installed"
        # snapshot BEFORE spawning: the launched window is identified as the
        # new one that appears (spawn alone says nothing — the app may fail,
        # fork, or already be running)
        try:
            ids_before = {w.get("id") for w in self._niri_windows()}
        except RuntimeError:
            ids_before = None
        try:
            subprocess.Popen(
                ["niri", "msg", "action", "spawn", "--", resolved],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            return f"ERROR: launch failed: {e}"
        log.info("open_app: %s", chosen)
        if ids_before is None:
            return (f"launched {chosen} (could not verify the window — "
                    "niri window list unavailable)")
        w, already = self._wait_new_window(ids_before, chosen,
                                           self.OPEN_APP_WAIT_S)
        if w is None:
            return (f"launched {chosen}, but no new window appeared within "
                    f"{self.OPEN_APP_WAIT_S:.0f}s — it may still be starting "
                    "(or failed to launch); try wait_for_window or focus_window")
        if already:
            log.info("open_app: %s was already open", chosen)
            return (f"{chosen} is already open — window: {self._win_label(w)} "
                    "(NOT focused — call focus_window to raise it)")
        note = f"launched {chosen} — window ready: {self._win_label(w)}"
        note += (" (focused)" if w.get("is_focused")
                 else " (NOT focused — call focus_window before typing)")
        self._mark_elements_stale()
        return note

    def _wait_new_window(self, ids_before: set, name: str,
                         timeout: float) -> tuple[dict | None, bool]:
        """Wait for the launched app's window after spawn.

        Priority: (1) a NEW window matching `name`; (2) after a short grace
        period, any NEW window; (3) an EXISTING window matching `name` — the
        app was likely already running and mapped nothing. Returns
        (window, already_open); (None, False) when nothing appeared."""
        start = time.monotonic()
        deadline = start + timeout
        fallback: dict | None = None
        while True:
            try:
                wins = self._niri_windows()
            except RuntimeError:
                wins = []
            fresh = [w for w in wins if w.get("id") not in ids_before]
            for w in fresh:
                if name and self._win_matches(w, name):
                    return w, False
            if fallback is None and fresh:
                fallback = fresh[0]
            now = time.monotonic()
            if now - start >= self._WIN_GRACE_S:
                if fallback is not None:
                    return fallback, False
                for w in wins:
                    if name and self._win_matches(w, name):
                        return w, True
            if now >= deadline:
                return None, False
            time.sleep(self._WIN_POLL_S)

    # -- read_file -------------------------------------------------------------

    @tool(description=(
        "Read a UTF-8 text file (any path, including your own source). "
        "May be truncated for very large files."))
    def read_file(self, path: str) -> str:
        """Read a text file.

        path: File path, ~ expanded.
        """
        p = Path(path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.exists():
            return f"ERROR: no such file: {p}"
        if p.is_dir():
            return "ERROR: path is a directory, not a file"
        try:
            data = p.read_bytes()
        except OSError as e:
            return f"ERROR: cannot read {p}: {e}"
        if b"\x00" in data[:4096]:
            return f"ERROR: {p} looks like a binary file"
        text = data.decode("utf-8", errors="replace")
        if len(text) > self.MAX_READ:
            text = text[: self.MAX_READ] + f"\n...[truncated, file is larger than {self.MAX_READ} chars]"
        return text

    # -- edit_file -------------------------------------------------------------

    @tool(description=(
        f"Replace a file's content. Allowed: your own source "
        f"{HOME / '.local/bin/handsoff.py'} and files under {CONFIG_DIR}/. "
        "Writes a .bak backup; self-edits must keep the marker and compile."))
    def edit_file(self, path: str, content: str) -> str:
        """Overwrite a text file.

        path: File path, ~ expanded.
        content: The complete new file content.
        """
        p = Path(path).expanduser().resolve()
        if len(content) > self.MAX_WRITE:
            return "REFUSED: content too large"
        try:
            config_root = CONFIG_DIR.resolve()
        except OSError:
            config_root = CONFIG_DIR
        is_self = p == SELF_PATH
        if not (is_self or config_root in p.parents):
            return (
                f"REFUSED: you may only edit {SELF_PATH} or files inside {CONFIG_DIR}/"
            )
        # capability boundary: settings.json holds the tool permissions; a
        # self-edit could silently re-enable a tool the user turned off.
        # The user changes permissions through the settings app only.
        if p in (SETTINGS_FILE, SETTINGS_FILE.with_suffix(".json")) or \
                p.name.startswith("settings.json"):
            return "REFUSED: settings.json controls your own permissions — the user manages it via the settings app"
        if is_self:
            if SELF_MARKER not in content:
                return f"REFUSED: self-edit must keep the marker line '{SELF_MARKER}'"
            try:
                compile(content, str(p), "exec")
            except SyntaxError as e:
                return f"REFUSED: new source does not compile: {e}"
            # ponytail: stdlib py_compile gate (no new deps) — compile()
            # checks syntax; py_compile proves the artifact byte-compiles
            # exactly as the restart will load it.
            try:
                import py_compile as _py_compile
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".py", delete=False,
                        encoding="utf-8") as tf:
                    tf.write(content)
                    _tmp_self = tf.name
                try:
                    _py_compile.compile(_tmp_self, doraise=True)
                finally:
                    try:
                        os.unlink(_tmp_self)
                    except OSError:
                        pass
            except Exception as e:
                return f"REFUSED: new source fails py_compile: {e}"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                shutil.copy2(p, str(p) + ".bak")
            # ponytail: atomic write via mkstemp, not predictable .tmp
            _atomic_private_write(p, content)
        except OSError as e:
            return f"ERROR: cannot write {p}: {e}"
        log.info("edit_file wrote %d bytes to %s", len(content), p)
        note = f"wrote {len(content)} bytes to {p}"
        if is_self:
            note += (f" — verify first: python -m py_compile {p} && "
                     f"python -m pytest tests/test_policy.py -q, then "
                     f"run_command '{RESTART_SCRIPT}' to restart into the new version")
        return note


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
                log.exception("mic health report failed")

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
            # --- transition detection ---------------------------------
            changed = state != self._health_state
            last = self._health_state
            self._health_state = state
            report = changed or self._health_next_summary <= time.monotonic()
            if report:
                self._health_next_summary = time.monotonic() + 3600.0
                emit = (log.warning if state in ("silent", "open-failing")
                        else log.info)
                emit(
                    "mic health: state=%s%s device=%s rate=%s frames=%d "
                    "last_nonzero=%.0fs_ago last_open=%s opens_ok=%d "
                    "opens_failed=%d utterances=%d",
                    state,
                    " (stalled — no frames, reopening)" if stalled else "",
                    device,
                    getattr(self, "_capture_rate", None) or "-",
                    self._frames_seen,
                    max(0.0, time.monotonic() - self._last_nonzero),
                    self._health_last_open,
                    self._health_opens_ok, self._health_opens_failed,
                    self._health_utt)
            if changed:
                # persist only listeners that really captured (or degraded
                # while trying): a never-started listener (hands-free off
                # since boot, or a bare test instance) has no mic story and
                # must not pollute the state file
                if self._ever_started or state != "stopped":
                    _record_mic_event(last or "boot", state)
                dur = ""
                if last == "open-failing" and self._health_recovered_after:
                    dur = " after %.0fs failing" % self._health_recovered_after
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
            self._assistant.sigLevel.emit(min(1.0, rms / 2000.0))
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
            fired, pre_audio = self._spotter.feed(indata.reshape(-1))
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
                self._stream.start()
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
                    # appear after a full terminate/reinitialize
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
            self._health_opens_ok += 1
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
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            frames.clear()
            gate.reset()
            self.gate_open = False
            if self._running and self._run_id == run_id:
                time.sleep(1.0)           # let a replugged/reset device settle
        if self._run_id == run_id:        # only the current generation owns state
            self._running = False
        self.gate_open = False


# --------------------------------------------------------------------- assistant


class Assistant(QObject):
    """Owns the state machine, the worker pipeline and the conversation."""

    sigState = Signal(str)
    sigLevel = Signal(float)
    sigUtterance = Signal(object)     # hands-free: np.ndarray from the VAD thread
    sigCommand = Signal(str)          # control-socket commands → main thread

    def __init__(self) -> None:
        super().__init__()
        self._state = IDLE
        self._gen = 0                     # increments per interaction; stale
        self._cancel = threading.Event()  # workers check their own event
        self._recorder: Recorder | None = None
        self._models_ready = threading.Event()
        self._history = self._load_history()
        self._memory = _load_memory()
        self._turn_spoke = False
        self._last_spoken = ""
        self._recently_spoken: list[str] = []   # last TTS lines, for echo rejection
        self._handsfree = bool(SETTINGS.get("handsfree", False))
        self._listener = ContinuousListener(self)
        self._notification_proc = None
        self._notification_stop = None
        self._notification_thread = None
        self._pomodoro_stop = None
        self._pomodoro_thread = None
        self._pomodoro_state = None
        self._tools = ToolBelt(
            on_restart_pending=self._prepare_restart,
            permissions=SETTINGS["permissions"],
            on_notification=self._set_notification_reader,
            on_announce=self._announce_now,
            on_pomodoro=self._set_pomodoro,
        )
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
        self._world_last_announce = 0.0  # monotonic: last proactive warning
        self._hardware_note = ""       # 1-2 line change note for the next turn
        self._hardware_last = {}       # change-detection state (in-memory only)
        self._hardware_last_urgent = 0.0  # monotonic: last hardware urgent
        self._hardware_tick_n = 0      # tick parity: GPU util at most every 2nd
        self._pipeline_q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._pipeline_worker, name="pipeline",
                         daemon=True).start()
        self.sigUtterance.connect(self._on_utterance)
        self.sigCommand.connect(self._on_command)

    # -- health snapshot (mic + brain + TTS) --------------------------------

    def mic_health(self) -> dict:
        """One JSON-ready snapshot of the assistant's vital signs: mic health
        (same state machine as the journal lines), brain (Ollama + model) and
        TTS (piper) status. Served over the control socket as `health`; kept
        free of Qt/logging side effects so it is trivially testable."""
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
            "ready": _piper_voice is not None,
            "whisper_ready": _whisper_model is not None,
        }
        snap["deployment"] = _deployment_snapshot()
        return snap

    # -- mic self-heal -------------------------------------------------------

    def _say_now(self, text: str) -> None:
        """Standalone announcement: speak text outside any turn pipeline."""
        self._gen += 1
        gen, cancel = self._gen, threading.Event()   # fresh: _cancel may be set
        threading.Thread(
            target=lambda: (self._speak(text, gen, cancel), self._set(gen, IDLE)),
            name="selfheal-tts", daemon=True,
        ).start()

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
        if time.monotonic() - self._world_last_announce < cooldown_s:
            return
        events, _degraded = _world_events("all", 5)
        fresh = [e for e in events
                 if e.get("urgent") and not _world_seen(e.get("key", ""))]
        if not fresh:
            return
        self._world_last_announce = time.monotonic()
        _world_mark_seen([e["title"] for e in fresh])
        text = "World warning: " + "; ".join(e["title"][:140] for e in fresh[:2])
        log.warning("world warning: %s", text)
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
                if time.monotonic() - self._hardware_last_urgent >= cd:
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
        if enabled:
            if self._notification_thread is not None and self._notification_thread.is_alive():
                return "notification reader is already on"
            try:
                self._notification_proc = subprocess.Popen(
                    ["dbus-monitor", "--session",
                     "interface='org.freedesktop.Notifications',member='Notify'"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1, start_new_session=True)
            except FileNotFoundError:
                self._notification_proc = None
                set_setting("notification_reader", False)
                return "ERROR: dbus-monitor is not installed"
            except OSError as e:
                self._notification_proc = None
                set_setting("notification_reader", False)
                return f"ERROR: notification monitor failed: {e}"
            stop = threading.Event()
            self._notification_stop = stop
            self._notification_thread = threading.Thread(
                target=self._notification_loop, args=(self._notification_proc, stop),
                name="notification-reader", daemon=True)
            self._notification_thread.start()
            log.info("desktop notification reader enabled")
            return "notification reader enabled"
        stop = self._notification_stop
        proc = self._notification_proc
        self._notification_stop = None
        self._notification_proc = None
        if stop is not None:
            stop.set()
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        log.info("desktop notification reader disabled")
        return "notification reader disabled"

    @staticmethod
    def _dbus_strings(line: str) -> list[str]:
        """Extract ordinary quoted D-Bus string values from monitor output."""
        return re.findall(r'(?<!\\)"((?:\\.|[^"\\])*)"', line)

    def _notification_loop(self, proc, stop: threading.Event) -> None:
        values: list[str] = []
        try:
            for line in proc.stdout or ():
                if stop.is_set():
                    return
                if line.startswith("signal ") and "member=Notify" in line:
                    values = []
                    continue
                if not values and not line.lstrip().startswith("string"):
                    continue
                values.extend(self._dbus_strings(line))
                # Notify's signature is (app, replaces-id, icon, summary,
                # body, actions, hints, expire-time). dbus-monitor prints the
                # uint32/arrays separately, so the four strings we need are
                # app, icon, summary, body — do not wait for a fifth string.
                if len(values) >= 4:
                    app, _icon, summary, body = values[:4]
                    values = []
                    muted = [str(x).lower() for x in SETTINGS.get("notification_mute_apps", [])]
                    muted.append(APP_NAME.lower())  # never echo our own popups
                    if any(m and m in app.lower() for m in muted):
                        log.info("notification muted from %s", app)
                        continue
                    now = time.monotonic()
                    with _READER_COOLDOWN_LOCK:
                        last = _READER_APP_LAST.get(app.lower())
                        if last is not None and now - last < _READER_APP_COOLDOWN:
                            log.info("notification cooldown suppresses %s", app)
                            continue
                        _READER_APP_LAST[app.lower()] = now
                    text = f"Notification from {app}: {summary}"
                    if body.strip():
                        text += f". {body.strip()}"
                    try:
                        self._announce_now(text[:500])
                    except Exception:
                        log.exception("notification announcement failed")
        except (OSError, ValueError):
            if not stop.is_set():
                log.exception("notification reader stopped unexpectedly")
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except (AttributeError, OSError):
                    pass

    def _set_pomodoro(self, action: str, work: float, break_minutes: float) -> str:
        """Own the bounded Pomodoro worker and announce work/break transitions."""
        if action == "status":
            state = self._pomodoro_state
            if not state:
                return "pomodoro is off"
            remaining = max(0, int(state["until"] - time.monotonic()))
            return (f"pomodoro is in {state['phase']} phase with "
                    f"{remaining // 60} minutes remaining")
        if action == "stop":
            if self._pomodoro_stop is not None:
                self._pomodoro_stop.set()
            self._pomodoro_stop = None
            self._pomodoro_state = None
            return "pomodoro stopped"
        if self._pomodoro_thread is not None and self._pomodoro_thread.is_alive():
            return "pomodoro is already running"
        stop = threading.Event()
        self._pomodoro_stop = stop
        self._pomodoro_state = {"phase": "work", "until": time.monotonic() + work * 60,
                                "work": work, "break": break_minutes}
        self._pomodoro_thread = threading.Thread(
            target=self._pomodoro_loop, args=(stop,), name="pomodoro", daemon=True)
        self._pomodoro_thread.start()
        self._announce_now(f"Pomodoro started: {work:.0f} minutes of work.")
        return f"pomodoro started: {work:.0f} minute work and {break_minutes:.0f} minute break"

    def _pomodoro_loop(self, stop: threading.Event) -> None:
        phase = "work"
        while not stop.is_set():
            state = self._pomodoro_state
            if not state:
                return
            if stop.wait(max(0.05, state["until"] - time.monotonic())):
                return
            phase = "break" if phase == "work" else "work"
            minutes = state["break"] if phase == "break" else state["work"]
            self._pomodoro_state = {**state, "phase": phase,
                                    "until": time.monotonic() + minutes * 60}
            self._announce_now(
                f"Pomodoro: {('break' if phase == 'break' else 'back to work')} "
                f"for {minutes:.0f} minutes.")

    # -- reminders --------------------------------------------------------------

    def _reminder_worker(self) -> None:
        while True:
            time.sleep(2.0)
            try:
                with REMINDERS_LOCK:
                    items = _load_reminders()
                    fired, kept = [], items
                    if items:
                        fired, kept = _due_reminders(items, time.time())
                    if fired and kept != items:
                        _save_reminders(kept)
                if not fired:
                    continue
                for r in fired:
                    log.info("reminder fired: %s", r["name"])
                    self.sigCommand.emit("__timer:%s\x1f%s" % (
                        r["name"], float(r.get("repeat_hours") or 0)))
            except Exception:
                log.exception("reminder worker pass failed")

    def _announce_missed(self, missed: list[dict]) -> None:
        names = "; ".join(r["name"] for r in missed[:4])
        extra = f" and {len(missed) - 4} more" if len(missed) > 4 else ""
        msg = f"While I was off, these reminders came due: {names}{extra}."
        log.info("announcing %d missed reminder(s)", len(missed))
        notify(f"handsoff missed reminders: {names}")
        self._gen += 1
        gen, cancel = self._gen, threading.Event()   # fresh: _cancel may be stale
        threading.Thread(
            target=lambda: (self._speak(msg, gen, cancel), self._set(gen, IDLE)),
            name="missed-rem-tts", daemon=True,
        ).start()

    def _fire_timer(self, name: str, repeat_hours: float = 0) -> None:
        msg = (f"Reminder: {name}. Say snooze for more time." if not repeat_hours
               else f"Reminder: {name}.")
        log.info("announcing timer: %s", name)
        notify(f"handsoff timer: {name}")
        self.interrupt()
        self._gen += 1
        # a FRESH cancel event: interrupt() just set self._cancel, and _speak
        # returns immediately on a set event — the reminder would never be spoken
        gen, cancel = self._gen, threading.Event()
        threading.Thread(
            target=lambda: (self._speak(msg, gen, cancel), self._set(gen, IDLE)),
            name="timer-tts", daemon=True,
        ).start()
        if not repeat_hours:
            # one-off: open the snooze window — a bare "snooze" within 90 s
            # re-arms it from now, no brain round-trip
            with _SNOOZE_LOCK:
                _snooze_offer.clear()
                _snooze_offer.update(name=name,
                                     until=time.monotonic() + SNOOZE_WINDOW_S)
            log.info("snooze window open for %r (%.0fs)", name, SNOOZE_WINDOW_S)

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
                self._gen += 1
                gen, cancel = self._gen, threading.Event()
                threading.Thread(
                    target=lambda: (self._speak(
                        "Heads up: I crashed earlier. It was logged, and I'm running again.",
                        gen, cancel), self._set(gen, IDLE)),
                    name="crash-report", daemon=True,
                ).start()
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
        threading.Thread(target=self._loader, name="loader", daemon=True).start()
        if self._handsfree:
            self._listener.start()
        missed = _take_missed_reminders()
        if missed:
            threading.Thread(target=self._announce_missed, args=(missed,),
                             name="missed-reminders", daemon=True).start()
        threading.Thread(target=self._reminder_worker, name="reminders", daemon=True).start()

    def shutdown(self) -> None:
        self._cancel.set()
        self._listener.stop()
        if getattr(self, "_tools", None) is not None:
            self._tools.stop_watchers()
        self._set_notification_reader(False)
        if self._pomodoro_stop is not None:
            self._pomodoro_stop.set()
        self._pomodoro_stop = None
        self._pomodoro_state = None
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass

    def _loader(self) -> None:
        errors: list[str] = []
        try:
            get_whisper()
        except Exception as e:
            errors.append(f"whisper: {e}")
        try:
            get_piper()
        except Exception as e:
            errors.append(f"piper: {e}")
        if not ollama_available():
            errors.append(f"ollama: no server at {OLLAMA_BASE} (systemctl start ollama)")
        else:
            log.info("ollama ok at %s, model %s", OLLAMA_BASE, OLLAMA_MODEL)
        self._models_ready.set()
        for e in errors:
            log.error("startup: %s", e)
        if errors:
            notify("handsoff: " + " | ".join(errors))
        # warm the LLM now (after whisper/piper, which load first). This is
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
        if gen != self._gen:
            return
        self._state = state
        try:
            self.sigState.emit(state)
        except RuntimeError:
            pass                      # Qt object already deleted: shutting down

    # -- UI entry points (main thread) -------------------------------------------

    def begin_listening(self) -> None:
        self.interrupt()
        if self._handsfree:
            # the continuous listener owns the mic; a press just barges in
            self._listener.suspend()
            return
        self._gen += 1
        self._cancel = threading.Event()
        gen, cancel = self._gen, self._cancel
        rec = Recorder(
            on_level=self.sigLevel.emit,
            device=str(SETTINGS["mic_device"]) if SETTINGS["mic_device"] else None,
            threshold=int(SETTINGS["mic_threshold"]),
        )
        try:
            rec.start()
        except Exception as e:
            log.exception("cannot open microphone")
            self._set(gen, IDLE)
            threading.Thread(
                target=lambda: (self._models_ready.wait(30), self._speak(
                    "I can't open the microphone.", gen, cancel), self._set(gen, IDLE)),
                daemon=True,
            ).start()
            return
        self._recorder = rec
        self._set(gen, LISTENING)

    def finish_listening(self) -> None:
        if self._handsfree:
            self._listener.resume()
            return
        rec, self._recorder = self._recorder, None
        if rec is None:
            return
        try:
            audio = rec.stop()
        except Exception:
            log.exception("recorder stop failed")
            audio = None
        self.submit_audio(audio)

    def submit_audio(self, audio: np.ndarray | None) -> None:
        """Shared entry point: push-to-talk releases and hands-free utterances."""
        if audio is None:
            self._set(self._gen, IDLE)
            return
        if len(audio) < SAMPLE_RATE * 0.3 \
                or int(np.max(np.abs(audio))) < int(SETTINGS["mic_threshold"]):
            log.info("discarding too-short/quiet capture")
            self._set(self._gen, IDLE)
            return
        # NOTE: the echo check lives in _pipeline now — it needs whisper,
        # which must never run on the Qt GUI thread (it froze the UI after
        # every spoken reply), and this way the audio is transcribed once
        self.interrupt()
        self._gen += 1
        self._cancel = threading.Event()
        gen, cancel = self._gen, self._cancel
        self._set(gen, THINKING)
        # zero-LLM stop: probe the transcript in parallel; if it is a bare
        # stop command the queued turn is drained before the brain ever runs
        self._log_utterance_health()
        self._maybe_instant_stop(audio, gen)
        # hand off to the single pipeline worker: two overlapping pipelines
        # would race the shared history, _stream_result and tool belt (the old
        # thread-per-utterance design let an interrupted-but-still-running
        # turn write history concurrently with the new one)
        self._pipeline_q.put((audio, gen, cancel))

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
        log.info("dictation: typed %d chars (%s...)", len(text), text[:40])
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
        while True:
            audio, gen, cancel = self._pipeline_q.get()
            try:
                self._pipeline(audio, gen, cancel)
            except Exception:
                log.exception("pipeline worker crash")
            finally:
                self._pipeline_q.task_done()

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
                log.info("voice stop: draining pipeline for %r", text)
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
        rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass
        if self._handsfree:
            self._listener.resume()
        self._set(self._gen, IDLE)

    def interrupt(self) -> None:
        """Barge-in: any press cancels the current pipeline (speech/thought)."""
        self._cancel.set()
        self._followup_until = 0.0    # barge-in also closes the follow-up window
        self._listener.reset()

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
            set_setting("handsfree", on)
        except OSError:
            log.exception("cannot persist handsfree setting")
        if on:
            self._listener.start()
        else:
            self._listener.stop()
            self._set(self._gen, IDLE)
        self._mic_selfheal_rearm()   # a fresh stream is a clean slate
        log.info("hands-free %s", "enabled" if on else "disabled")
        notify(f"hands-free {'enabled' if on else 'disabled'}")

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
            if self.state == LISTENING and self._recorder is not None:
                self.finish_listening()
            elif self.state == IDLE and not self._handsfree:
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
            self._gen += 1
            gen, cancel = self._gen, threading.Event()
            self._set_dictation(on, gen, cancel)

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
        returns immediately."""
        gen = self._gen
        self._set(gen, IDLE)
        threading.Thread(
            target=lambda: self._speak(text, gen, threading.Event()),
            name="announce", daemon=True,
        ).start()

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
            log.info("echo-check: heard %r -> %s", text, hit)
            return hit
        except Exception:
            log.exception("echo-check failed")
            return False

    def _try_snooze(self, text: str, gen: int, cancel: threading.Event) -> bool:
        """Fast-path a 'snooze [N minutes]' reply while the offer window is live.

        Runs BEFORE the wake-word gate: a snooze is a direct answer to our own
        announcement and must not require the wake name. Returns True when the
        utterance was consumed."""
        with _SNOOZE_LOCK:
            offer = dict(_snooze_offer) if _snooze_offer else None
            if offer and time.monotonic() >= offer["until"]:
                _snooze_offer.clear()
                offer = None
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
            with _SNOOZE_LOCK:
                _snooze_offer.clear()
        self._set(gen, IDLE)
        threading.Thread(target=lambda: self._speak(out, gen, cancel),
                         name="snooze-tts", daemon=True).start()
        return True

    def _pipeline(self, audio: np.ndarray, gen: int, cancel: threading.Event) -> None:
        try:
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
                log.info("follow-up accepted (no wake word): %s", text)
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
                    log.info("ignored (no wake word): %s", text)
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
                log.info("memory: %s", [f[1] for f in facts])
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
                q: "queue.Queue[str | None]" = queue.Queue()
                self._stream_result = None

                def _run_stream() -> None:
                    # local clear: a stale leftover from a previous turn must
                    # never be mistaken for THIS turn's result
                    self._stream_result = None
                    try:
                        self._stream_result = ollama_chat_stream(conversation, q, cancel, tools)
                    except RuntimeError as e:
                        self._stream_result = {"tool_calls": [], "content": "", "error": str(e)}

                streamer = threading.Thread(target=_run_stream, daemon=True)
                streamer.start()
                self._speak(None, gen, cancel, sentence_q=q)
                streamer.join(timeout=2.0)
                if streamer.is_alive():
                    # the model may still be finishing (slow tokens, big
                    # tool-call JSON) — waiting beats silently dropping the
                    # answer and every tool call
                    log.warning("stream worker slow to finish; waiting")
                    streamer.join(timeout=30.0)
                if self._stream_result is None:
                    if cancel.is_set():
                        return
                    log.error("stream finished without a result")
                    self._speak("Sorry, my brain gave me an empty answer.", gen, cancel)
                    return
                if self._stream_result.get("error"):
                    # ponytail: speak the HTTP failure; no empty turn appended
                    if cancel.is_set():
                        return
                    log.error("ollama stream: %s", self._stream_result["error"])
                    self._speak(f"Sorry, my brain is offline. {self._stream_result['error']}",
                                gen, cancel)
                    return
                tool_calls = self._stream_result.get("tool_calls") or []
                content = self._stream_result.get("content", "")
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
                log.info("tool call: %s(%s)", name, list(args))
                result, _err = self._tools.execute(name, args if isinstance(args, dict) else {})
                entry = {"role": "tool", "tool_name": name, "content": result}
                if self._tools._last_images:
                    entry["images"] = self._tools._last_images
                    log.info("attaching %d screenshot(s) to tool result", len(entry["images"]))
                conversation.append(entry)
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
        arrives (streaming TTS — playback starts while the model still writes)."""
        if _piper_voice is None and not self._models_ready.is_set():
            self._models_ready.wait(30)
        if sentence_q is None:
            text = (text or "").strip()
            if not text or cancel.is_set():
                return
            self._last_spoken = text
            self._turn_spoke = True
            self._set(gen, SPEAKING)
            self._recently_spoken.append(text)
            del self._recently_spoken[:-2]
            try:
                with tempfile.TemporaryDirectory(dir=str(STATE_DIR)) as td:
                    wav = Path(td) / "tts.wav"
                    tts_to_wav(text, wav)
                    if not cancel.is_set():
                        log.info("saying: %s", text)
                        play_wav(wav, cancel)
            except Exception:
                log.exception("TTS failed")
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
            self._last_spoken = sentence
            self._turn_spoke = True
            self._set(gen, SPEAKING)
            said.append(sentence)
            self._recently_spoken.append(sentence)
            del self._recently_spoken[:-2]
            try:
                with tempfile.TemporaryDirectory(dir=str(STATE_DIR)) as td:
                    wav = Path(td) / "tts.wav"
                    tts_to_wav(sentence, wav)
                    if cancel.is_set():
                        return
                    log.info("saying: %s", sentence)
                    play_wav(wav, cancel)
            except Exception:
                log.exception("TTS failed (streaming)")
        self._last_spoken = " ".join(said)
        # announce-and-listen (streaming path): same arming as above
        if self._turn_spoke and not cancel.is_set() and self._handsfree \
                and float(SETTINGS.get("followup_seconds", 0.0)) > 0.0:
            self._followup_until = _tick_now() + float(
                SETTINGS["followup_seconds"])
            log.info("follow-up window open for %ss", SETTINGS["followup_seconds"])

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
            _atomic_private_write(
                HISTORY_FILE, json.dumps(self._history, ensure_ascii=False, indent=1))
        except OSError:
            log.exception("cannot save history")


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
    backstop."""
    out = list(msgs)
    while len(out) > MAX_HISTORY_MESSAGES:
        out.pop(0)
        while out and out[0].get("role") != "user":
            out.pop(0)
        if not out:
            break
    budget = max(0, _history_budget())

    def _total(seq: list[dict]) -> int:
        return sum(_msg_tokens(m) for m in seq)

    while out and _total(out) > budget and len(out) > 1:
        out.pop(0)
        while out and out[0].get("role") != "user" and len(out) > 1:
            out.pop(0)               # don't orphan tool results / assistant turns
        if out and out[0].get("role") != "user" and _total(out) > budget:
            break                    # single over-budget message: keep it
    return out


# -------------------------------------------------------------------------- bubble UI


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
        self._press_pos = None
        self._drag_last = None

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

    # -- slots -----------------------------------------------------------------

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def set_level(self, level: float) -> None:
        self._level_target = level

    def _on_tick(self) -> None:
        self._level_ui += (self._level_target - self._level_ui) * 0.35
        self.update()

    # -- painting ----------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx, cy = self.width() / 2, self.height() / 2
        t = self._clock.elapsed() / 1000.0
        color = STATE_COLORS.get(self._state, STATE_COLORS[IDLE])

        if self._state == LISTENING:
            radius = BUBBLE_R0 + (3 + 9 * self._level_ui) * GEOM_K
            swirl_speed, swirl_boost, hue_speed = 0.55, 0.30, 50.0
        elif self._state == SPEAKING:
            radius = BUBBLE_R0 + 7 * GEOM_K * (0.5 - 0.5 * math.cos(2 * math.pi * t / 0.6))
            swirl_speed, swirl_boost, hue_speed = 0.50, 0.32, 90.0
        elif self._state == THINKING:
            radius = BUBBLE_R0
            swirl_speed, swirl_boost, hue_speed = 0.85, 0.40, 140.0
        else:  # idle: slow breathing
            radius = BUBBLE_R0 + 3.5 * GEOM_K * math.sin(2 * math.pi * t / 3.8)
            swirl_speed, swirl_boost, hue_speed = 0.18, 0.10, 25.0

        base_hue = max(0.0, color.hueF())
        inner = radius * (1.0 - 0.34 - 0.10 * swirl_boost)   # dark core

        # --- Siri-style rotating swirl: conic hue sweep clipped to the rim ring ---
        conic = QConicalGradient(cx, cy, -t * 360.0 * swirl_speed)
        first_stop = None
        for i in range(6):
            pos = i / 5.0
            shifted = QColor.fromHslF(
                (base_hue + (0.5 - abs(0.5 - pos)) * hue_speed / 360.0) % 1.0,
                min(1.0, color.hslSaturationF() * 1.15),
                0.60 + 0.10 * swirl_boost,
                1.0,
            )
            if first_stop is None:
                first_stop = QColor(shifted)
            conic.setColorAt(pos, shifted)
        conic.setColorAt(1.0, first_stop)
        p.setPen(Qt.NoPen)

        # faint state-colored halo so the dark orb reads on dark wallpapers
        halo = QRadialGradient(QPointF(cx, cy), radius + GLOW_PAD)
        halo_color = QColor(color)
        halo_color.setAlpha(55 if self._state != IDLE else 35)
        halo.setColorAt(radius / (radius + GLOW_PAD), halo_color)
        halo_color.setAlpha(0)
        halo.setColorAt(1.0, halo_color)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), radius + GLOW_PAD, radius + GLOW_PAD)

        p.save()
        path = QPainterPath()
        path.addEllipse(QPointF(cx, cy), radius, radius)
        inner_path = QPainterPath()
        inner_path.addEllipse(QPointF(cx, cy), inner, inner)
        ring_path = path.subtracted(inner_path)
        p.setClipPath(ring_path)
        p.setBrush(QBrush(conic))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        # radial falloff: darken toward the core with translucent black over the ring
        shade = QRadialGradient(QPointF(cx, cy), radius)
        shade.setColorAt(inner / radius, QColor(10, 12, 18, 235))
        shade.setColorAt(1.0, QColor(10, 12, 18, 0))
        p.setBrush(QBrush(shade))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        p.restore()

        # --- dark glass core ---
        core = QRadialGradient(QPointF(cx, cy - inner * 0.3), inner * 1.35)
        core.setColorAt(0.0, QColor(40, 44, 54))
        core.setColorAt(0.7, QColor(24, 26, 33))
        core.setColorAt(1.0, QColor(14, 15, 20))
        p.setBrush(QBrush(core))
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawEllipse(QPointF(cx, cy), inner, inner)

        # --- specular highlight + rim light ---
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 34))
        p.drawEllipse(
            QPointF(cx - inner * 0.30, cy - inner * 0.42), inner * 0.30, inner * 0.20
        )
        # thin colored rim light between core and swirl
        rim = QColor(color)
        rim.setAlpha(80)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(rim, max(1.0, inner * 0.05)))
        p.drawEllipse(QPointF(cx, cy), inner * 0.97, inner * 0.97)
        p.end()

    @staticmethod
    def _wobble_path(cx: float, cy: float, r0: float, t: float) -> QPainterPath:
        path = QPainterPath()
        n = 72
        a1, a2 = r0 * 0.115, r0 * 0.08
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
        if self._pressing and not self._dragging:
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

    def showEvent(self, _e) -> None:
        self.setMask(QRegion(self.rect(), QRegion.Ellipse))

    def closeEvent(self, _e) -> None:
        self._assistant.shutdown()


# ------------------------------------------------------------------ control socket


PTT_ACTIONS = {"start", "stop", "toggle", "interrupt",
               "handsfree", "handsfree-on", "handsfree-off",
               "handsfree-status", "dictation", "dictation-on", "dictation-off",
               "status", "health", "doctor", "settings", "selftest"}


class ControlServer:
    """Unix-socket remote control so niri keybinds can drive the bubble.

    The server owns its socket and stop event. This matters on a clean Qt
    shutdown: a daemon thread that outlives the widget can otherwise keep a
    stale socket inode around until the process is killed, confusing the next
    startup and making a failed launch look like a live bubble.
    """

    def __init__(self, assistant: "Assistant") -> None:
        self._assistant = assistant
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="control", daemon=True)
        self._thread.start()

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
                    continue
                except OSError:
                    if not self._stop.is_set():
                        log.exception("control socket accept failed")
                    return
                try:
                    conn.settimeout(5.0)
                    action = conn.recv(1024).decode("utf-8", "replace").strip().lower()
                    if action in PTT_ACTIONS:
                        if action == "status":
                            reply = (f"state={self._assistant.state} "
                                     f"handsfree={'on' if self._assistant._handsfree else 'off'} "
                                     f"model={OLLAMA_MODEL}")
                        elif action == "health":
                            try:
                                reply = json.dumps(
                                    self._assistant.mic_health(),
                                    ensure_ascii=False)
                            except Exception:
                                log.exception("health snapshot failed")
                                reply = "error: health snapshot failed (see log)"
                        elif action == "doctor":
                            try:
                                reply = run_doctor()
                            except Exception:
                                log.exception("doctor report failed")
                                reply = "error: doctor report failed (see log)"
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
  status         report state, hands-free mode and model
  health         full JSON health: mic, brain (Ollama) and TTS status
  doctor         human-readable diagnostic: deployment hashes, Ollama, mic, niri, systemd
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
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(str(CONTROL_SOCK))
        s.sendall(action.encode("utf-8"))
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
        sys.stderr.write("handsoff is not running (no control socket)\n")
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
        SETTINGS["piper_voice"] or "(first *.onnx in piper-voice/)",
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


if __name__ == "__main__":
    sys.exit(main())
