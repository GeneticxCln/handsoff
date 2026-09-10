#!/usr/bin/env python
# handsoff-self-marker: this line must be preserved across self-edits
"""
handsoff settings — a small GUI for the handsoff voice assistant bubble.

    python ~/.local/bin/handsoff-settings.py

Edits ~/.config/handsoff/settings.json (the bubble reads it at startup):

    Brain        Ollama host, model picker with capability badges, context size
    Voice        microphone + threshold, whisper size, piper voice downloads,
                 speech rate and volume, test buttons
    Permissions  enable/disable tools, extra whitelisted commands
    Appearance   bubble size and the four state colours (live preview)
    Startup      niri autostart entry, restart the bubble, open the log

Add `--selftest` to run a headless smoke test (no window is shown).
"""
from __future__ import annotations

import importlib.util
import copy
import hashlib
import html
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import datetime as _dt
from pathlib import Path

HOME = Path.home()


def _import_handsoff():
    """Load the bubble's module for shared paths and helpers.

    ONE shared order everywhere: beside-this-file first, then the installed
    copy — so a repo checkout never silently runs installed code (or vice
    versa) when both exist."""
    for cand in (Path(__file__).resolve().parent / "handsoff.py",
                 HOME / ".local/bin/handsoff.py"):
        if cand.exists():
            spec = importlib.util.spec_from_file_location("handsoff_core", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.stderr.write("handsoff-settings: cannot find handsoff.py — install handsoff first\n")
    sys.exit(1)


class _LazyHandsoff:
    """Defer exec'ing the 8621-line bubble until first attribute use.

    Importing handsoff.py pulls the whole bubble (audio/Qt) just to reach
    shared paths/helpers; the settings GUI paid that at startup for no
    reason. Reads behave as before — the first real attribute execs the
    bubble once and caches it — and monkeypatch.setattr(H, ...) in tests
    keeps working (instance attrs shadow the bubble until deleted).

    Hardening: a load lock so concurrent first uses exec exactly once;
    dunder probes (copy/hasattr/pickle) fail fast without exec'ing the
    bubble, and missing plain attrs raise from the cached bubble without
    re-exec; instance-attr shadowing via __setattr__/__delattr__ below is
    deliberate shadowing (never a write-through to the bubble module)."""
    def __init__(self) -> None:
        self.__dict__["_bubble"] = None
        self.__dict__["_lock"] = threading.Lock()

    def _load(self):
        mod = self.__dict__["_bubble"]
        if mod is None:
            with self.__dict__["_lock"]:
                mod = self.__dict__["_bubble"]
                if mod is None:
                    mod = _import_handsoff()
                    self.__dict__["_bubble"] = mod
        return mod

    def __getattr__(self, name: str):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)  # probe: never exec the bubble for dunders
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value) -> None:
        # Explicit shadow: e.g. tests' monkeypatch.setattr lands here and
        # wins over the bubble attr until deleted; the bubble itself is
        # never written through.
        self.__dict__[name] = value

    def __delattr__(self, name: str) -> None:
        # Unshadow: deleting the instance attr re-exposes the bubble attr.
        del self.__dict__[name]

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(dir(self._load())))


def _import_settings_schema():
    """Load settings_schema.py (the single source of DEFAULT_SETTINGS) directly.

    The settings GUI must not pull its defaults through the bubble module:
    that forced a full bubble exec (audio imports, Qt globals) just to merge
    defaults, and duplicated the schema in the pre-2026-09 monolith. The
    schema is a dependency-free dict.
    """
    for cand in (Path(__file__).resolve().parent / "settings_schema.py",
                 HOME / ".local/bin/settings_schema.py"):
        if cand.exists():
            spec = importlib.util.spec_from_file_location("handsoff_settings_schema", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.stderr.write("handsoff-settings: cannot find settings_schema.py — install handsoff first\n")
    sys.exit(1)


SCHEMA = _import_settings_schema()
DEFAULT_SETTINGS = SCHEMA.DEFAULT_SETTINGS
SETTINGS_VERSION = SCHEMA.SETTINGS_VERSION

H = _LazyHandsoff()

import numpy as np  # noqa: E402  (after handsoff, which already required it)
import sounddevice as sd  # noqa: E402

from PySide6.QtCore import QElapsedTimer, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QRadialGradient, QBrush, QPen  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

WHISPER_SIZES = {          # size key -> approx download size
    "tiny": "~75 MB", "tiny.en": "~75 MB", "base": "~145 MB",
    "base.en": "~145 MB", "small": "~500 MB", "medium": "~1.5 GB",
    "large-v3": "~3 GB",
}

VOICE_CATALOG = [          # (label, quality, onnx url on rhasspy/piper-voices)
    ("English (US) — lessac", "medium (~63 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"),
    ("English (US) — amy", "low (~25 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx"),
    ("English (US) — ryan", "high (~120 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx"),
    ("English (GB) — alan", "low (~25 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/low/en_GB-alan-low.onnx"),
    ("German — thorsten", "medium (~63 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx"),
    ("French — siwis", "medium (~63 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx"),
    ("Spanish — carlfm", "x-low (~20 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx"),
    ("Italian — riccardo", "x-low (~20 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/it/it_IT/riccardo/x_low/it_IT-riccardo-x_low.onnx"),
    ("Russian — dmitri", "medium (~63 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium.onnx"),
    ("Ukrainian — ukrainian-tts", "medium (~63 MB)",
     "https://huggingface.co/rhasspy/piper-voices/resolve/main/uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx"),
]

NIRI_CONFIG = Path(os.environ.get("NIRI_CONFIG", str(HOME / ".config/niri/config.kdl")))
AUTOSTART_LINE = f'spawn-at-startup "sh" "-c" "exec python \\"{HOME}/.local/bin/handsoff.py\\""'
AUTOSTART_LINE_OLD = f'spawn-at-startup "python" "{HOME}/.local/bin/handsoff.py"'
AUTOSTART_COMMENT = "// handsoff voice assistant bubble"


def _autostart_hit(line: str) -> bool:
    """Either dialect counts: old `spawn "python" ...` installs are
    recognized (and migrated on enable) but only the new form is written."""
    return AUTOSTART_LINE in line or AUTOSTART_LINE_OLD in line


def merge_settings(data: dict) -> dict:
    """defaults <- settings.json values (dicts merge key-wise, like the bubble
    does), then run the SHARED coercion from handsoff.py: a hand-edited
    settings.json with garbage values ("1,5", "32k", "abc") must produce a
    working UI, not a crashed recovery tool."""
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy of defaults
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in merged:
                continue
            if isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k].update(v)
            else:
                merged[k] = v
    # Shared coercion without exec'ing the whole bubble: core.settings is the
    # module handsoff.py itself re-exports coerce_settings from (same function
    # object), so prefer it; the lazy bubble is only a fallback (e.g. partial
    # installs without core/).
    try:
        from core.settings import coerce_settings
    except ImportError:
        coerce_settings = H.coerce_settings
    return coerce_settings(merged)


def http_json(url: str, payload: dict | None = None, timeout: int = 10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def autostart_enabled() -> bool:
    try:
        return any(_autostart_hit(line) for line in NIRI_CONFIG.read_text().splitlines())
    except OSError:
        return False


def _reload_niri() -> None:
    try:
        subprocess.run(["niri", "msg", "action", "load-config-file"],
                       timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def systemd_owns_autostart() -> bool:
    """True when the systemd user unit is enabled: systemd then owns the
    bubble's autostart and niri spawn-at-startup must NOT be added (single
    autostart owner — same rule the installer applies)."""
    try:
        return subprocess.run(
            ["systemctl", "--user", "is-enabled", "handsoff.service"],
            capture_output=True, text=True, timeout=5,
        ).returncode == 0
    except Exception:
        return False


def apply_autostart(enable: bool) -> str:
    """Autostart part of a settings save; returns the human message.

    Defers to systemd when the user unit is enabled: adding niri
    spawn-at-startup then would double-start the bubble (its instance lock
    blocks the second bubble, but ownership/restart behaviour gets murky).
    """
    if enable and systemd_owns_autostart():
        return ("Autostart: systemd already manages handsoff — niri "
                "spawn-at-startup NOT added (double-start guard).")
    return set_autostart(enable)


def set_autostart(enable: bool) -> str:
    """Add or remove the spawn-at-startup line in the niri config (with backup)."""
    try:
        if not NIRI_CONFIG.exists():
            if not enable:
                return "no niri config found — nothing to change"
            NIRI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            NIRI_CONFIG.write_text("// niri config\n")
        lines = NIRI_CONFIG.read_text().splitlines()
        has_new = any(AUTOSTART_LINE in line for line in lines)
        has = has_new or any(AUTOSTART_LINE_OLD in line for line in lines)
        bak = NIRI_CONFIG.with_name(NIRI_CONFIG.name + ".bak-handsoff")
        if enable:
            n_new = sum(1 for line in lines if AUTOSTART_LINE in line)
            n_old = sum(1 for line in lines if AUTOSTART_LINE_OLD in line)
            if n_new == 1 and n_old == 0:
                return "autostart unchanged"
            if not bak.exists():
                shutil.copy2(NIRI_CONFIG, bak)
            stripped = [line for line in lines
                        if not _autostart_hit(line) and line.strip() != AUTOSTART_COMMENT]
            if stripped and stripped[-1].strip():
                stripped.append("")
            stripped.append(AUTOSTART_COMMENT)
            stripped.append(AUTOSTART_LINE)  # exactly one NEW line, OLD gone
            _atomic_text_write(NIRI_CONFIG, "\n".join(stripped) + "\n")
            _reload_niri()
            return ("autostart line added to niri config" if not has
                    else "autostart line migrated to sh -c form")
        if not enable and has:
            if not bak.exists():
                shutil.copy2(NIRI_CONFIG, bak)
            lines = [line for line in lines
                     if not _autostart_hit(line) and line.strip() != AUTOSTART_COMMENT]
            _atomic_text_write(NIRI_CONFIG, "\n".join(lines) + "\n")
            _reload_niri()
            return "autostart line removed from niri config"
        return "autostart unchanged"
    except OSError as e:
        return f"error editing {NIRI_CONFIG}: {e}"


def _atomic_text_write(path: Path, text: str) -> None:
    """Atomically replace a text file via a unique temp file in the same
    directory (no predictable .tmp name); existing permissions are kept."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _backup_keep_n(path: Path, tag: str, keep: int = 5) -> None:
    """Copy `path` to a unique timestamped `path.<tag>.<stamp>` backup,
    pruning older ones (by mtime) so at most `keep` remain. The pid+counter
    suffix keeps concurrent writers from sharing a name — no lock needed."""
    _backup_keep_n.seq += 1
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(
        f"{path.name}.{tag}.{stamp}-p{os.getpid()}-{_backup_keep_n.seq}")
    shutil.copy2(path, bak)
    try:
        olds = sorted(path.parent.glob(f"{path.name}.{tag}.*"),
                      key=lambda p: p.stat().st_mtime_ns)
    except OSError:
        return
    for stale in olds[:-keep]:
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass


_backup_keep_n.seq = 0


def _voice_want_sha(name: str) -> str:
    """Expected sha256 for a voice file, like install.sh's download_verified:
    the default lessac voice is pinned (env-overridable); anything else has
    no known checksum."""
    env, default = {
        "en_US-lessac-medium.onnx": (
            "PIPER_VOICE_SHA256",
            "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f"),
        "en_US-lessac-medium.onnx.json": (
            "PIPER_VOICE_JSON_SHA256",
            "efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0"),
    }.get(name, ("", ""))
    return os.environ.get(env, default) if env else ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    """Persist a rename: best-effort directory fsync (no-op where unsupported)."""
    try:
        fd = os.open(os.fspath(path), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class BubblePreview(QWidget):
    """Four animated orbs previewing the state colours and bubble size."""

    def __init__(self, colors_fn, size_fn) -> None:
        super().__init__()
        self._colors_fn = colors_fn
        self._size_fn = size_fn
        self.setMinimumHeight(150)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self._timer.start()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        colors = self._colors_fn()
        k = self._size_fn() / 128.0
        w, h = self.width(), self.height()
        orb_r = 22.0 * k
        cy = h / 2 - 8
        slots = [w * (i + 0.5) / 4 for i in range(4)]
        t = self._clock.elapsed() / 1000.0
        for i, (name, color) in enumerate(colors.items()):
            cx = slots[i]
            tt = t + i * 0.9
            if name == "idle":
                r = orb_r + 2.0 * k * (1 + math.sin(2 * math.pi * tt / 3.8)) / 2
            elif name == "listening":
                r = orb_r + (4 + 5 * abs(math.sin(2 * math.pi * tt / 0.9))) * k
            elif name == "thinking":
                r = orb_r + 2.5 * k * math.sin(3 * tt + 1.3)
            else:
                r = orb_r + 4.0 * k * (0.5 - 0.5 * math.cos(2 * math.pi * tt / 0.6))
            glow = QColor(color)
            glow.setAlpha(50)
            grad = QRadialGradient(cx, cy, r + 6 * k)
            grad.setColorAt(0.0, glow)
            glow.setAlpha(0)
            grad.setColorAt(1.0, glow)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawEllipse(QPointF(cx, cy), r + 6 * k, r + 6 * k)
            body = QRadialGradient(cx, cy - r * 0.25, r * 1.15)
            body.setColorAt(0.0, QColor(color).lighter(140))
            body.setColorAt(1.0, QColor(color).darker(160))
            p.setBrush(QBrush(body))
            p.setPen(QPen(QColor(255, 255, 255, 45), 1))
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.setPen(QPen(QColor(140, 140, 140)))
            f = QFont()
            f.setPointSize(8)
            p.setFont(f)
            p.drawText(int(cx - 40), int(cy + orb_r + 6 * k + 16), 80, 14,
                       Qt.AlignHCenter, name)
        p.end()


class VoiceDownloadDialog(QDialog):
    """Pick a piper voice from a small catalog or any direct .onnx URL."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Download a piper voice")
        self.resize(560, 380)
        self._cancel = threading.Event()

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Voices go to ~/.config/handsoff/piper-voice/"))
        self.list = QListWidget(self)
        for label, quality, url in VOICE_CATALOG:
            it = QListWidgetItem(f"{label}  ·  {quality}")
            it.setData(Qt.UserRole, url)
            self.list.addItem(it)
        self.list.setCurrentRow(0)
        lay.addWidget(self.list)

        row = QHBoxLayout()
        row.addWidget(QLabel("or URL:"))
        self.url_edit = QLineEdit(self)
        self.url_edit.setPlaceholderText("https://…/voice.onnx  (a .json config must sit next to it)")
        row.addWidget(self.url_edit)
        lay.addLayout(row)

        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.status = QLabel("", self)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.dl_btn = QPushButton("Download", self)
        self.dl_btn.clicked.connect(self._download)
        close = QPushButton("Close", self)
        close.clicked.connect(self.reject)
        btns.addWidget(self.dl_btn)
        btns.addStretch(1)
        btns.addWidget(close)
        lay.addLayout(btns)

    # -- download ---------------------------------------------------------------

    def _pick_url(self) -> str | None:
        sel = self.list.currentItem()
        url = self.url_edit.text().strip() if self.url_edit.text().strip() else (
            sel.data(Qt.UserRole) if sel else None)
        if url and not url.lower().startswith("https://"):
            # mirror the installer's HANDSOFF_UNVERIFIED_VOICE=1 escape: an
            # explicit operator opt-in, never the default.
            if os.environ.get("HANDSOFF_UNVERIFIED_VOICE") != "1":
                self.status.setText("URL must use https://")
                return None
            self.status.setText(
                "WARNING: non-https voice URL allowed "
                "(HANDSOFF_UNVERIFIED_VOICE=1)")
        if url and not url.endswith(".onnx"):
            self.status.setText("URL must point to a .onnx file")
            return None
        return url

    def _download(self) -> None:
        url = self._pick_url()
        if not url:
            return
        name = url.rsplit("/", 1)[-1]
        dest = H.PIPER_VOICE_DIR / name
        prog = {"pct": -1, "msg": "", "done": False, "err": None}

        def fetch(target: Path, src: str) -> bool:
            """Download one voice file; True when bytes were fetched. The
            existing dest is re-verified first (never silently accept a
            truncated earlier download — a truncated voice crashes piper)."""
            want = _voice_want_sha(target.name)
            if target.exists():
                ok = (_sha256_file(target) == want) if want \
                    else target.stat().st_size > 0
                if ok:
                    prog["msg"] = f"{target.name} already present"
                    return False
                target.unlink(missing_ok=True)
            H.PIPER_VOICE_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".",
                                            suffix=".part", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as f:
                    req = urllib.request.Request(src, headers={"User-Agent": "handsoff/1.0"})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        total = int(r.headers.get("Content-Length") or 0)
                        got = 0
                        while True:
                            if self._cancel.is_set():
                                raise InterruptedError("cancelled")
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            prog["pct"] = int(got * 100 / total) if total else -1
                            prog["msg"] = f"{target.name}: {got // 1024 // 1024} MB"
                        f.flush()
                        os.fsync(f.fileno())
                if want and _sha256_file(Path(tmp_name)) != want:
                    raise ValueError(f"{target.name} checksum mismatch")
                os.replace(tmp_name, target)
                _fsync_dir(target.parent)
            except BaseException:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return True

        def worker() -> None:
            try:
                got_onnx = fetch(dest, url)
                cfg = Path(str(dest) + ".json")
                got_cfg = fetch(cfg, url + ".json")
                if not (got_onnx or got_cfg):
                    prog["msg"] = f"{name} already present"
                elif not _voice_want_sha(dest.name):
                    # ponytail: custom URL, no pinned checksum — warn loudly and
                    # print the pin so the next download (or the bubble's own
                    # PIPER_VOICE_SHA256 gate) can verify it.
                    try:
                        digest = _sha256_file(dest)
                    except OSError:
                        digest = ""
                    prog["msg"] = (f"saved {name} (WARNING: unverified — pin with "
                                   f"PIPER_VOICE_SHA256={digest})" if digest
                                   else f"saved {name} (WARNING: unverified)")
                else:
                    prog["msg"] = f"saved {name}"
            except Exception as e:
                prog["err"] = e
            prog["done"] = True

        self.dl_btn.setEnabled(False)
        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            if prog["pct"] >= 0:
                self.bar.setValue(prog["pct"])
            self.status.setText(prog.get("msg", ""))
            if prog["done"]:
                self.dl_btn.setEnabled(True)
                if prog["err"]:
                    self.status.setText(f"download failed: {prog['err']}")
                else:
                    self.bar.setValue(100)
                    QTimer.singleShot(700, self.accept)
                return
            QTimer.singleShot(150, poll)

        poll()


class _LiveMicProbe:
    """GUI-free core of the settings app's live mic test: continuously opens
    the selected input device via handsoff's own _open_input (so native-rate
    fallback behaves exactly like the bubble), gates frames with the bubble's
    real _SpeechGate, and runs whisper on collected utterances in a worker
    thread. The GUI only polls `snapshot()` and never blocks the UI thread.

    Everything is created lazily in start(): instantiating this object must
    not touch audio devices or models (tests construct it freely)."""

    FRAME = 1024
    MAX_UTT_S = 15.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._device: str | None = None
        self._threshold = 300
        self._stream = None
        self._capture_rate = 0
        self._gate = None
        self._frames: list = []
        self._spot_peak = 0.0
        self._speech_peak = 0.0
        self._last_event = ""
        self._last_event_at = 0.0
        self._last_transcript = ""
        self._last_transcript_at = 0.0
        self._error = ""
        self._frames_seen = 0
        self._last_nonzero = 0.0
        self._transcribe_thread = None
        self._gen = 0               # run generation: a stale _run exits
        self._thread = None         # current _run thread (joined on restart)

    # -- lifecycle -------------------------------------------------------

    def start(self, device: str | None, threshold: int) -> None:
        """Begin capturing from `device` ('' or None = system default).
        Safe to call repeatedly while running: a changed device or threshold
        restarts capture, otherwise it is a no-op."""
        device = device or None
        threshold = int(threshold)
        with self._lock:
            if self._running and device == self._device \
                    and threshold == self._threshold:
                return
            self._running = True
            self._gen += 1          # stale the old run so it exits itself
            my_gen = self._gen
            old = self._thread
            self._thread = None
        if old is not None and old.is_alive():
            old.join(timeout=2.0)   # best-effort: gen guard covers a lingerer
        t = threading.Thread(target=self._run, args=(device, threshold),
                             name="mic-live-test", daemon=True)
        with self._lock:
            if my_gen != self._gen or not self._running:
                return              # stop() (or a newer start) won during join
            self._thread = t
        t.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._gen += 1          # stale any _run, incl. one start() is joining
        self._close_stream()

    def _close_stream(self) -> None:
        st = self._stream
        self._stream = None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass

    def _run(self, device: str | None, threshold: int) -> None:
        # one capture loop per (device, threshold) change; exits when a newer
        # run has taken over (token) or stop() cleared _running
        token = object()
        with self._lock:
            gen = self._gen
            self._device, self._threshold = device, threshold
            self._gate = H._SpeechGate(threshold)
            self._frames = []
            self._last_event = ""
        max_frames = int(self.MAX_UTT_S * H.SAMPLE_RATE / self.FRAME)
        fail_sleep = 2.0

        def cb(indata, nframes, time_info, status) -> None:
            self._on_frames(indata, token, max_frames)

        while True:
            with self._lock:
                if gen != self._gen or not self._running or self._device != device \
                        or self._threshold != threshold:
                    return
            try:
                st, rate = H._open_input(device, H.SAMPLE_RATE, self.FRAME, cb)
                st.start()
            except Exception as e:
                with self._lock:
                    self._error = f"cannot open device: {e}"
                    self._stream = None
                    self._capture_rate = 0
                time.sleep(fail_sleep)
                fail_sleep = min(10.0, fail_sleep * 1.5)
                continue
            fail_sleep = 2.0
            with self._lock:
                self._stream = st
                self._capture_rate = rate
                self._error = ""
                self._frames_seen = 0
                self._last_nonzero = time.monotonic()
            while True:
                time.sleep(0.5)
                with self._lock:
                    if gen != self._gen or not self._running or self._device != device \
                            or self._threshold != threshold:
                        if self._stream is st:  # close only OUR stream
                            self._close_stream()
                        return
                    if self._stream is not st:      # replaced by a newer run
                        return

    # -- audio path (PortAudio callback thread) ---------------------------

    def _on_frames(self, indata, token, max_frames: int) -> None:
        # bookkeeping stays under the lock; the resample + STT handoff run
        # unlocked (the PortAudio callback must never block on slow work)
        with self._lock:
            if not self._running or self._stream is None:
                return
            gate = self._gate
            if gate is None:
                return
            self._frames_seen += 1
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            if rms > 0.5:
                self._last_nonzero = time.monotonic()
            self._spot_peak = max(self._spot_peak * 0.9, rms)
            event = gate.feed(rms)
            if event == "start":
                self._frames = []
                self._speech_peak = 0.0
                self._last_event = "speech started"
                self._last_event_at = time.monotonic()
            if gate.in_speech:
                self._frames.append(indata.copy())
                self._speech_peak = max(self._speech_peak, rms)
            utter = None
            if event == "end" or len(self._frames) >= max_frames:
                if len(self._frames) >= 4:      # ≥ ~0.26 s: real speech
                    utter = (np.concatenate(self._frames).reshape(-1),
                             self._capture_rate)
                self._frames = []
                gate.reset()
        if utter is not None:
            audio, rate = utter
            audio = H._resample_to_16k(audio, rate)
            with self._lock:
                self._start_transcribe(audio)
                self._last_event = "speech captured (%.1fs)" % (
                    len(audio) / H.SAMPLE_RATE)
                self._last_event_at = time.monotonic()

    def _start_transcribe(self, audio: np.ndarray) -> None:
        """Hand one utterance to the whisper worker (drops an older in-flight
        utterance rather than queueing — a live meter wants fresh results)."""
        t = threading.Thread(target=self._transcribe_worker, args=(audio,),
                             name="mic-live-stt", daemon=True)
        self._transcribe_thread = t
        t.start()

    def _transcribe_worker(self, audio: np.ndarray) -> None:
        try:
            text = H.transcribe(audio)
        except Exception as e:
            with self._lock:
                if self._transcribe_thread is threading.current_thread():
                    self._last_event = f"transcribe failed: {e}"
                    self._last_event_at = time.monotonic()
            return
        with self._lock:
            if self._transcribe_thread is not threading.current_thread():
                return                          # superseded by a newer utterance
            self._last_transcript = text or "(unintelligible)"
            self._last_transcript_at = time.monotonic()
            self._last_event = "transcribed"
            self._last_event_at = time.monotonic()

    # -- GUI-facing snapshot ----------------------------------------------

    def snapshot(self) -> dict:
        """Current state for the meter UI; cheap, lock-held, no blocking."""
        with self._lock:
            silent_for = max(
                0.0, time.monotonic() - self._last_nonzero) \
                if self._frames_seen else None
            return {
                "running": self._running,
                "device": self._device or "system default",
                "rate": self._capture_rate,
                "peak": self._spot_peak,
                "speech_peak": self._speech_peak,
                "gate_open": bool(self._gate.in_speech) if self._gate else False,
                "last_event": self._last_event,
                "last_event_age": (time.monotonic() - self._last_event_at
                                   if self._last_event_at else None),
                "transcript": self._last_transcript,
                "transcript_age": (time.monotonic() - self._last_transcript_at
                                   if self._last_transcript_at else None),
                "error": self._error,
                "frames": self._frames_seen,
                "silent_for": silent_for,
            }


def _health_query(sock_path, timeout: float = 1.5) -> "dict | None":
    """Ask the running bubble for its JSON health snapshot over the control
    socket. Returns the parsed dict, or None when the bubble isn't running,
    the socket is stale or the answer isn't JSON (never raises)."""
    if sock_path is None:
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(b"health")
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            part = s.recv(65536)
            if not part:
                break
            buf += part
        s.close()
        doc = json.loads(buf.decode("utf-8", "replace"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def _fmt_health(snap: dict) -> str:
    """One compact status-bar line from a health snapshot."""
    mic = snap.get("mic") or {}
    brain = snap.get("brain") or {}
    tts = snap.get("tts") or {}
    mic_txt = f"mic: {mic.get('state', '?')}"
    if mic.get("device"):
        dev = str(mic["device"])
        mic_txt += f" ({dev[:38]}…)" if len(dev) > 40 else f" ({dev})"
    if mic.get("rate"):
        mic_txt += f" @ {mic['rate']} Hz"
    if mic.get("stalled"):
        mic_txt += " · stalled"
    if mic.get("failing_since") is not None:
        mic_txt += f" · failing {int(mic['failing_since'])}s"
    if mic.get("utterances"):
        mic_txt += f" · {mic['utterances']} utt"
    brain_txt = ("brain: ok " + str(brain.get("model", ""))
                 if brain.get("reachable") else "brain: DOWN")
    voice_ok = bool(tts.get("ready"))
    stt_ok = bool(tts.get("whisper_ready"))
    if voice_ok and stt_ok:
        tts_txt = "tts/stt: ok"
    elif not voice_ok and not stt_ok:
        tts_txt = "tts/stt: loading\u2026"
    else:
        tts_txt = "tts: ok, stt loading\u2026" if voice_ok else "stt: ok, voice loading\u2026"
    dep = snap.get("deployment") or {}
    if dep.get("status") == "in-sync":
        deploy_txt = "deploy: ok"
    elif dep.get("status"):
        deploy_txt = f"deploy: {dep['status']}"
    else:
        deploy_txt = ""
    parts = [p for p in (mic_txt, brain_txt, tts_txt, deploy_txt) if p]
    return " · ".join(parts)


def _health_tooltip(snap: dict | None) -> str:
    """Full health JSON for the status bar's hover tooltip — every field the
    bubble knows, one mouse-over away. Escaped so device names containing
    angle brackets or ampersands render instead of vanishing."""
    if not isinstance(snap, dict):
        return ("The bubble is not running (or is still starting).\n"
                "Start it with:  systemctl --user start handsoff.service")
    body = json.dumps(snap, indent=2, sort_keys=True, ensure_ascii=False)
    return ("<pre>" + html.escape(body) + "</pre>")


class SettingsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("handsoff settings")
        self.resize(780, 600)
        import copy
        self.cfg = copy.deepcopy(H.SETTINGS)
        self._model_at_open = str(self.cfg.get("model") or "")
        self._state_dir_ready()
        self._live_probe: _LiveMicProbe | None = None   # live mic test (Voice tab)

        tabs = QTabWidget(self)
        self.tabs = tabs
        tabs.addTab(self._brain_tab(), "Brain")
        tabs.addTab(self._voice_tab(), "Voice")
        tabs.addTab(self._permissions_tab(), "Permissions")
        tabs.addTab(self._memory_tab(), "Memory")
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._startup_tab(), "Startup")
        self._history_page = self._history_tab()
        tabs.addTab(self._history_page, "History")
        tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(tabs)

        bottom = QWidget(self)
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("", self)
        save = QPushButton("Save", self)
        save.clicked.connect(self._on_save)
        apply_btn = QPushButton("Save & restart bubble", self)
        apply_btn.clicked.connect(self._on_apply)
        quit_btn = QPushButton("Quit bubble", self)
        quit_btn.clicked.connect(self._on_quit_bubble)
        bl.addWidget(self.status_label, 1)
        bl.addWidget(quit_btn)
        bl.addWidget(save)
        bl.addWidget(apply_btn)

        outer = QWidget(self)
        ol = QVBoxLayout(outer)
        ol.addWidget(tabs, 1)

        # live bubble-vitals line (health command via the control socket),
        # refreshed every 3 s while the window is open
        self.health_label = QLabel("bubble health: querying\u2026", self)
        self.health_label.setStyleSheet("color: palette(mid);")
        ol.addWidget(self.health_label)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(3000)
        self._health_timer.timeout.connect(self._refresh_health)
        self._health_timer.start()
        QTimer.singleShot(300, self._refresh_health)

        ol.addWidget(bottom)
        self.setCentralWidget(outer)

        self.reload_from_disk()
        self.refresh_models()
        self.refresh_mics()
        self.refresh_voices()

        # if settings.json changes on disk (the bubble persists its hands-free
        # toggle, another window saves, …) reload instead of clobbering it on Save
        self._disk_mtime = self._settings_mtime()
        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(2000)
        self._disk_timer.timeout.connect(self._check_disk_changes)
        self._disk_timer.start()

    # ---------------------------------------------------------------- helpers

    def _state_dir_ready(self) -> None:
        try:
            H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _refresh_health(self) -> None:
        """Poll the running bubble's `health` command off the GUI thread and
        render one compact vitals line (mic, brain, tts) in the status bar."""
        def fetch():
            return _health_query(H.CONTROL_SOCK)

        def done(ok, result):
            self.health_label.setToolTip(_health_tooltip(result))
            if ok and result is not None:
                self.health_label.setText(_fmt_health(result))
                degraded = ((result.get("mic") or {}).get("state")
                            in ("silent", "open-failing"))
                self.health_label.setStyleSheet(
                    "color: orange;" if degraded else "color: palette(mid);")
            else:
                self.health_label.setText(
                    "bubble health: not running (settings still work)")
                self.health_label.setStyleSheet("color: palette(mid);")

        self.run_bg(fetch, done)

    def closeEvent(self, event) -> None:            # noqa: N802 (Qt naming)
        """Stop the live mic test (and its stream + whisper worker) and the
        health poller on close, so the settings app never holds the mic or
        keeps polling after the window is gone."""
        if self._live_probe is not None:
            self._live_probe.stop()
        if getattr(self, "_health_timer", None) is not None:
            self._health_timer.stop()
        super().closeEvent(event)

    def run_bg(self, fn, done) -> None:
        box: dict = {}

        def worker() -> None:
            try:
                box["ok"], box["result"] = True, fn()
            except Exception as e:  # noqa: BLE001
                box["ok"], box["result"] = False, e
            box["done"] = True

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            if box.get("done"):
                done(box["ok"], box["result"])
            else:
                QTimer.singleShot(120, poll)

        QTimer.singleShot(120, poll)

    def _status(self, text: str) -> None:
        self.status_label.setText(text)

    # ------------------------------------------------------------------- brain

    def _brain_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        host_group = QGroupBox("Ollama server", w)
        form = QFormLayout(host_group)
        self.host_edit = QLineEdit(self.cfg["ollama_host"], self)
        form.addRow("Server URL", self.host_edit)
        self.remote_ollama_chk = QCheckBox(
            "Allow a remote server (send history, screenshots & schemas off this machine)", self)
        self.remote_ollama_chk.setToolTip(
            "handsoff refuses to talk to a non-loopback Ollama server until this is "
            "checked (or HANDSOFF_ALLOW_REMOTE_OLLAMA=1 is set). The bubble's "
            "conversation history, voice transcripts, and tool schemas leave your "
            "machine when the server is remote.")
        form.addRow("", self.remote_ollama_chk)
        self.model_list = QListWidget(self)
        self.model_list.setMinimumHeight(180)
        form.addRow("Models (🔧 tools = can control the desktop & self-modify)", self.model_list)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self.refresh_models)
        self.test_btn = QPushButton("Test selected model", self)
        self.test_btn.clicked.connect(self.test_model)
        row.addWidget(refresh)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        form.addRow(row)
        self.ctx_spin = QSpinBox(self)
        self.ctx_spin.setRange(1024, 131072)
        self.ctx_spin.setSingleStep(1024)
        self.ctx_spin.setSuffix(" tokens")
        form.addRow("Context size", self.ctx_spin)
        self.hist_spin = QSpinBox(self)
        self.hist_spin.setRange(0, 131072)
        self.hist_spin.setSingleStep(512)
        self.hist_spin.setSuffix(" tokens")
        self.hist_spin.setToolTip(
            "0 = automatic (context size minus the system prompt + tool "
            "schemas and a reply reserve). History is trimmed to this token "
            "budget so long conversations never exceed the model's context.")
        form.addRow("History budget", self.hist_spin)
        self.toolrate_spin = QSpinBox(self)
        self.toolrate_spin.setRange(0, 600)
        self.toolrate_spin.setSpecialValueText("unlimited")
        self.toolrate_spin.setSuffix(" /min")
        self.toolrate_spin.setToolTip(
            "Safety limit on tool calls per minute (0 = unlimited). "
            "Stops the AI if it ever gets stuck in a tool-calling loop.")
        form.addRow("Tool-call rate limit", self.toolrate_spin)
        lay.addWidget(host_group)
        hint = QLabel(
            "Tool-capable models (badge “tools”) can run commands and edit their own code.\n"
            "Chat works with any model. Models are pulled with:  ollama pull <name>", w)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    def _selected_model(self) -> str:
        it = self.model_list.currentItem()
        return it.data(Qt.UserRole) if it else ""

    def refresh_models(self) -> None:
        base = (self.host_edit.text().strip() or DEFAULT_SETTINGS["ollama_host"]).rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "http://" + base
        self.model_list.clear()
        loading = QListWidgetItem("loading …")
        loading.setFlags(Qt.NoItemFlags)
        self.model_list.addItem(loading)

        def fetch():
            tags = http_json(base + "/api/tags", timeout=5)
            out = []
            for m in tags.get("models", []):
                name = m.get("name") or m.get("model") or ""
                caps: list[str] = []
                try:
                    caps = http_json(base + "/api/show", {"model": name}, timeout=8).get("capabilities") or []
                except Exception:
                    pass
                out.append((name, caps))
            return out

        def done(ok, result):
            self.model_list.clear()
            if not ok:
                bad = QListWidgetItem(f"cannot reach {base} — {result}")
                bad.setFlags(Qt.NoItemFlags)
                self.model_list.addItem(bad)
                return
            for name, caps in result:
                badges = "   ·  " + "  ".join(caps) if caps else ""
                it = QListWidgetItem(name + badges)
                it.setData(Qt.UserRole, name)
                it.setToolTip(f"capabilities: {', '.join(caps) or 'none'}")
                self.model_list.addItem(it)
                if name == self.cfg["model"]:
                    self.model_list.setCurrentItem(it)
            if self.model_list.currentRow() < 0 and self.model_list.count():
                self.model_list.setCurrentRow(0)

        self.run_bg(fetch, done)

    def test_model(self) -> None:
        base = (self.host_edit.text().strip() or DEFAULT_SETTINGS["ollama_host"]).rstrip("/")
        model = self._selected_model() or self.cfg["model"]
        self.test_btn.setEnabled(False)
        self._status(f"testing {model} …")

        def fetch():
            t0 = time.time()
            reply = http_json(base + "/api/chat", {
                "model": model, "stream": False, "think": False,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "options": {"num_ctx": 2048},
            }, timeout=120)
            text = (reply.get("message") or {}).get("content", "").strip()
            return f"{model} replied “{text[:60]}” in {time.time() - t0:.1f}s"

        def done(ok, result):
            self.test_btn.setEnabled(True)
            self._status(str(result) if ok else f"test failed: {result}")

        self.run_bg(fetch, done)

    # ------------------------------------------------------------------- voice

    def _voice_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)

        mic_group = QGroupBox("Microphone", w)
        form = QFormLayout(mic_group)
        mic_row = QHBoxLayout()
        self.mic_combo = QComboBox(self)
        mic_refresh = QPushButton("Refresh", self)
        mic_refresh.clicked.connect(self.refresh_mics)
        mic_row.addWidget(self.mic_combo, 1)
        mic_row.addWidget(mic_refresh)
        form.addRow("Input device", mic_row)
        self.thresh_spin = QSpinBox(self)
        self.thresh_spin.setRange(50, 5000)
        self.thresh_spin.setSingleStep(50)
        self.thresh_spin.setToolTip("Peak loudness needed to accept a recording (noise gate)")
        form.addRow("Recording threshold", self.thresh_spin)
        mic_test_row = QHBoxLayout()
        self.mic_test_btn = QPushButton("Test microphone (3 s)", self)
        self.mic_test_btn.clicked.connect(self.test_mic)
        self.mic_bar = QProgressBar(self)
        self.mic_bar.setRange(0, 100)
        mic_test_row.addWidget(self.mic_test_btn)
        mic_test_row.addWidget(self.mic_bar, 1)
        form.addRow(mic_test_row)

        # -- live test mode: continuous meter + last transcript -------------
        mic_live_row = QHBoxLayout()
        self.mic_live_btn = QPushButton("Live test", self)
        self.mic_live_btn.setCheckable(True)
        self.mic_live_btn.setToolTip(
            "Open the selected microphone continuously: the meter follows the "
            "room, and anything that passes the threshold above is transcribed "
            "with the same whisper model the bubble uses — so you can verify a "
            "device before switching to it. Runs on its own stream; if the "
            "bubble's hands-free is also capturing, some mics split levels "
            "between the two listeners.")
        self.mic_live_btn.toggled.connect(self._toggle_mic_live)
        mic_live_row.addWidget(self.mic_live_btn)
        self.mic_live_state = QLabel("idle", self)
        mic_live_row.addWidget(self.mic_live_state, 1)
        form.addRow(mic_live_row)
        self.mic_live_event = QLabel("", self)
        self.mic_live_event.setStyleSheet("color: palette(mid);")
        form.addRow(self.mic_live_event)
        self.mic_live_transcript = QLabel("", self)
        self.mic_live_transcript.setWordWrap(True)
        self.mic_live_transcript.setStyleSheet("font-weight: bold;")
        form.addRow("Last transcript", self.mic_live_transcript)
        self.thresh_spin.valueChanged.connect(
            lambda _v: self._mic_live_restart_if_on())
        self.mic_combo.currentIndexChanged.connect(
            lambda _i: self._mic_live_restart_if_on())
        lay.addWidget(mic_group)

        hf_group = QGroupBox("Hands-free listening", w)
        hfl = QVBoxLayout(hf_group)
        self.hf_chk = QCheckBox("Continuous listening — talk without pressing anything", self)
        self.hf_chk.setToolTip("A voice-activity gate detects speech and auto-sends each utterance")
        hfl.addWidget(self.hf_chk)
        hf_note = QLabel(
            "When on, the microphone stays open and the bubble turns red while you speak; "
            "after a short pause your utterance is sent automatically. The mic is muted "
            "while the assistant talks, so it never hears itself.", w)
        hf_note.setWordWrap(True)
        hfl.addWidget(hf_note)

        wake_form = QFormLayout()
        self.wake_name_edit = QLineEdit(str(self.cfg.get("assistant_name", "assistant")), w)
        self.wake_name_edit.setMaximumWidth(220)
        wake_form.addRow("Assistant name", self.wake_name_edit)
        self.wake_chk = QCheckBox(
            "Only listen when addressed by name (\u201chey assistant\u201d)", w)
        self.wake_chk.setToolTip(
            "On: the assistant ignores everything until you say its name, then stays "
            "engaged for the window below. Off: it answers every utterance it hears.")
        wake_form.addRow(self.wake_chk)
        self.wake_secs = QSpinBox(w)
        self.wake_secs.setRange(5, 600)
        self.wake_secs.setSuffix(" s")
        self.wake_secs.setValue(int(float(self.cfg.get("engage_seconds", 45.0))))
        wake_form.addRow("Stay engaged after the wake word", self.wake_secs)
        self.followup_secs = QSpinBox(w)
        self.followup_secs.setRange(0, 60)
        self.followup_secs.setSuffix(" s")
        self.followup_secs.setSpecialValueText("Off")
        self.followup_secs.setToolTip(
            "After each spoken reply, listen for one follow-up without the wake "
            "word for this many seconds (0 = off). Requires hands-free and the "
            "wake-word gate to be on.")
        self.followup_secs.setValue(
            int(float(self.cfg.get("followup_seconds", 0.0))))
        wake_form.addRow("Follow-up window after a reply", self.followup_secs)
        self.home_edit = QLineEdit(str(self.cfg.get("home_place", "")), w)
        self.home_edit.setPlaceholderText("e.g. Hamburg")
        self.home_edit.setMaximumWidth(220)
        wake_form.addRow("Home place (weather)", self.home_edit)
        self.brief_chk = QCheckBox(
            "Morning briefing — weather on the first \u201chello\u201d each day", w)
        self.brief_chk.setToolTip(
            "Needs a home place. On the first conversational utterance each day, "
            "the assistant greets you with the live weather before answering.")
        wake_form.addRow(self.brief_chk)
        self.world_warn_chk = QCheckBox(
            "World warnings — speak up about severe world events as they break", w)
        self.world_warn_chk.setToolTip(
            "Opt-in. Checks breaking-news and severe-weather headlines on the "
            "existing health tick and announces urgent ones (popup always, "
            "spoken unless already speaking), at most once per cooldown.")
        wake_form.addRow(self.world_warn_chk)
        self.hw_watch_chk = QCheckBox(
            "Hardware watch — note machine changes, warn when critical", w)
        self.hw_watch_chk.setToolTip(
            "Opt-in. Samples cheap health signals on the existing tick and "
            "tells the next turn about changes (popup + spoken only when "
            "urgent: mic lost, disk critically low, Ollama down).")
        wake_form.addRow(self.hw_watch_chk)
        hw_row = QHBoxLayout()
        self.hw_cool_spin = QSpinBox(w)
        self.hw_cool_spin.setRange(5, 1440)
        self.hw_cool_spin.setSuffix(" min")
        self.hw_cool_spin.setToolTip("Min minutes between hardware urgents.")
        self.hw_disk_spin = QSpinBox(w)
        self.hw_disk_spin.setRange(1, 1000)
        self.hw_disk_spin.setSuffix(" GiB")
        self.hw_disk_spin.setToolTip("Warn when free disk drops below this.")
        hw_row.addWidget(QLabel("Urgent cooldown", w))
        hw_row.addWidget(self.hw_cool_spin)
        hw_row.addWidget(QLabel("Disk floor", w))
        hw_row.addWidget(self.hw_disk_spin)
        hw_row.addStretch(1)
        wake_form.addRow("Hardware watch", hw_row)
        self.cal_edit = QLineEdit(
            ", ".join(self.cfg.get("calendar_ics") or []), w)
        self.cal_edit.setPlaceholderText(
            "Google Calendar secret iCal URL, or /path/to/calendar.ics")
        self.cal_edit.setMaximumWidth(220)
        wake_form.addRow("Calendar (ICS URL or file)", self.cal_edit)
        self.spotter_chk = QCheckBox(
            "Audio wake spotter — detect the keyword before speech-to-text", w)
        self.spotter_chk.setToolTip(
            "Uses openWakeWord (~1 ms CPU per chunk) to detect the wake phrase at "
            "the audio level and reacts in under 100 ms. Requires 'only listen when "
            "addressed by name'. Stock keywords: hey jarvis, hey mycroft, alexa, timer.")
        wake_form.addRow(self.spotter_chk)
        self.spotter_edit = QLineEdit(
            ", ".join(self.cfg.get("spotter_models") or []), w)
        self.spotter_edit.setPlaceholderText(
            "empty = all stock models, or e.g. hey jarvis, timer")
        self.spotter_edit.setMaximumWidth(220)
        wake_form.addRow("Spotter keywords", self.spotter_edit)
        self.selfheal_chk = QCheckBox(
            "Auto-recover the microphone — restart the audio stream and say so "
            "when it stays broken", w)
        self.selfheal_chk.setToolTip(
            "If the microphone stays silent or unusable for over a minute while "
            "hands-free is on, restart the capture stream automatically and "
            "announce it out loud. Gives up after 3 tries (keeps logging until "
            "the mic recovers, then re-arms).")
        wake_form.addRow(self.selfheal_chk)
        self.resource_chk = QCheckBox(
            "Resource alerts — warn when RAM or GPU memory is nearly full", w)
        self.resource_chk.setToolTip(
            "Opt-in spoken alerts on threshold crossings. Alerts fire once while "
            "usage is high and re-arm only after it drops below the threshold.")
        wake_form.addRow(self.resource_chk)
        resource_row = QHBoxLayout()
        self.ram_alert_spin = QSpinBox(w)
        self.ram_alert_spin.setRange(50, 99)
        self.ram_alert_spin.setSuffix("%")
        self.vram_alert_spin = QSpinBox(w)
        self.vram_alert_spin.setRange(50, 99)
        self.vram_alert_spin.setSuffix("%")
        resource_row.addWidget(QLabel("RAM", w))
        resource_row.addWidget(self.ram_alert_spin)
        resource_row.addWidget(QLabel("GPU", w))
        resource_row.addWidget(self.vram_alert_spin)
        resource_row.addStretch(1)
        wake_form.addRow("Alert thresholds", resource_row)
        self.notification_chk = QCheckBox(
            "Read desktop notifications aloud (opt-in)", w)
        self.notification_chk.setToolTip(
            "Private by default. When enabled, future notifications are spoken; "
            "use the assistant's notification_reader mute action for noisy apps.")
        wake_form.addRow(self.notification_chk)
        self.notification_mute_edit = QLineEdit(
            ", ".join(self.cfg.get("notification_mute_apps") or []), w)
        self.notification_mute_edit.setPlaceholderText("muted app names, comma-separated")
        self.notification_mute_edit.setMaximumWidth(260)
        wake_form.addRow("Muted notification apps", self.notification_mute_edit)
        self.dictation_chk = QCheckBox(
            "Voice dictation — 'start dictation' types what you say into the "
            "focused window (no AI turn); Mod+Shift+D toggles", w)
        self.dictation_chk.setToolTip(
            "Zero-cost dictation: transcripts are typed into whatever window is "
            "focused, exactly like the model's own typing tool — terminals are "
            "refused fail-closed. Toggle by voice ('start dictation' / 'stop "
            "dictation', no wake word needed) or the Mod+Shift+D keybind.")
        wake_form.addRow(self.dictation_chk)
        hfl.addLayout(wake_form)
        lay.addWidget(hf_group)

        ws_group = QGroupBox("Workspace aliases (voice shortcuts)", w)
        wsl = QVBoxLayout(ws_group)
        self.alias_edit = QPlainTextEdit(w)
        self.alias_edit.setMaximumHeight(76)
        self.alias_edit.setPlaceholderText(
            "one per line:  name = workspace number\ne.g.  code = 2")
        aliases = self.cfg.get("workspace_aliases") or {}
        self.alias_edit.setPlainText(
            "\n".join(f"{k} = {v}" for k, v in sorted(aliases.items())))
        wsl.addWidget(self.alias_edit)
        wsl_note = QLabel(
            "Say \u201chey assistant, go to code\u201d and it switches to the matching "
            "workspace. Numbers stay unchanged \u2014 pure voice alias, nothing is renamed.", w)
        wsl_note.setWordWrap(True)
        wsl.addWidget(wsl_note)
        lay.addWidget(ws_group)

        stt_group = QGroupBox("Speech-to-text (faster-whisper)", w)
        stt_form = QFormLayout(stt_group)
        self.whisper_combo = QComboBox(self)
        for size, approx in WHISPER_SIZES.items():
            self.whisper_combo.addItem(f"{size}   ({approx} download)", size)
        stt_form.addRow("Model size", self.whisper_combo)
        note = QLabel("Smaller = faster. The model downloads on the next bubble start.", w)
        note.setWordWrap(True)
        stt_form.addRow(note)
        lay.addWidget(stt_group)

        tts_group = QGroupBox("Text-to-speech (piper)", w)
        tts_form = QFormLayout(tts_group)
        voice_row = QHBoxLayout()
        self.voice_combo = QComboBox(self)
        voice_dl = QPushButton("Download voice…", self)
        voice_dl.clicked.connect(self._download_voice)
        voice_row.addWidget(self.voice_combo, 1)
        voice_row.addWidget(voice_dl)
        tts_form.addRow("Voice", voice_row)

        def slider_row(slider: QSlider, label: QLabel) -> QHBoxLayout:
            r = QHBoxLayout()
            r.addWidget(slider, 1)
            r.addWidget(label)
            return r

        self.rate_slider = QSlider(Qt.Horizontal, self)
        self.rate_slider.setRange(50, 200)
        self.rate_label = QLabel("", self)
        self.rate_slider.valueChanged.connect(
            lambda v: self.rate_label.setText(f"{v / 100:.2f}×"))
        tts_form.addRow("Speech rate", slider_row(self.rate_slider, self.rate_label))

        self.vol_slider = QSlider(Qt.Horizontal, self)
        self.vol_slider.setRange(10, 200)
        self.vol_label = QLabel("", self)
        self.vol_slider.valueChanged.connect(
            lambda v: self.vol_label.setText(f"{v / 100:.2f}×"))
        tts_form.addRow("Volume", slider_row(self.vol_slider, self.vol_label))

        self.voice_test_btn = QPushButton("Test voice", self)
        self.voice_test_btn.clicked.connect(self.test_voice)
        tts_form.addRow(self.voice_test_btn)
        lay.addWidget(tts_group)
        lay.addStretch(1)
        return w

    def refresh_mics(self) -> None:
        self.mic_combo.clear()
        self.mic_combo.addItem("System default", "")
        try:
            devs = sd.query_devices()
            default_in = sd.default.device[0]
            names: list[str] = []
            for i, d in enumerate(devs):
                if d.get("max_input_channels", 0) > 0:
                    name = d.get("name", f"device {i}")
                    mark = "  [system default]" if i == default_in else ""
                    entry = f"{name}{mark}"
                    if entry not in names:
                        names.append(entry)
            for entry in names:
                self.mic_combo.addItem(entry, entry.replace("  [system default]", ""))
        except Exception as e:
            self._status(f"cannot list microphones: {e}")
        idx = self.mic_combo.findData(self.cfg["mic_device"])
        self.mic_combo.setCurrentIndex(max(0, idx))

    def refresh_voices(self) -> None:
        self.voice_combo.clear()
        self.voice_combo.addItem("(first voice file found)", "")
        try:
            for onnx in sorted(H.PIPER_VOICE_DIR.glob("*.onnx")):
                self.voice_combo.addItem(onnx.name, onnx.name)
        except OSError:
            pass
        idx = self.voice_combo.findData(self.cfg["piper_voice"])
        self.voice_combo.setCurrentIndex(max(0, idx))

    def _download_voice(self) -> None:
        dlg = VoiceDownloadDialog(self)
        dlg.exec()
        self.refresh_voices()

    def test_mic(self) -> None:
        device = self.mic_combo.currentData() or None
        self.mic_test_btn.setEnabled(False)
        self._status("recording from microphone …")
        box: dict = {"peak": 0.0, "done": False, "err": None}

        def worker() -> None:
            try:
                def cb(indata, frames, time_info, status) -> None:
                    rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
                    box["peak"] = max(box["peak"], rms)

                with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                    blocksize=1024, callback=cb, device=device):
                    deadline = time.time() + 3.0
                    while time.time() < deadline:
                        time.sleep(0.05)
            except Exception as e:  # noqa: BLE001
                box["err"] = e
            box["done"] = True

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            self.mic_bar.setValue(min(100, int(box["peak"] / 30)))
            if box["done"]:
                self.mic_test_btn.setEnabled(True)
                if box["err"]:
                    self._status(f"microphone test failed: {box['err']}")
                else:
                    thr = self.thresh_spin.value()
                    verdict = "good signal" if box["peak"] >= thr else "too quiet — lower the threshold?"
                    self._status(f"mic peak {box['peak']:.0f} (threshold {thr}) — {verdict}")
                return
            QTimer.singleShot(100, poll)

        poll()

    # -- live mic test mode -----------------------------------------------

    def _toggle_mic_live(self, on: bool) -> None:
        if on:
            if self._live_probe is None:
                self._live_probe = _LiveMicProbe()
            self._live_probe.start(self.mic_combo.currentData(),
                                   self.thresh_spin.value())
            self._mic_live_tick()
        else:
            if self._live_probe is not None:
                self._live_probe.stop()
            self.mic_live_state.setText("idle")
            self.mic_live_event.setText("")
            self.mic_live_transcript.setText("")

    def _mic_live_restart_if_on(self) -> None:
        """Device/threshold changed while live mode is on: restart capture
        against the new selection."""
        if getattr(self, "mic_live_btn", None) is not None \
                and self.mic_live_btn.isChecked() \
                and self._live_probe is not None:
            self._live_probe.start(self.mic_combo.currentData(),
                                   self.thresh_spin.value())

    def _mic_live_tick(self) -> None:
        """Poll the probe snapshot ~5x/s and refresh meter, state, transcript."""
        if not self.mic_live_btn.isChecked():
            return
        snap = self._live_probe.snapshot()
        self.mic_bar.setValue(min(100, int(snap["peak"] / 30)))
        if snap["error"]:
            state = f"error — {snap['error']}"
        elif snap["rate"] == 0:
            state = "opening …"
        else:
            state = (f"{snap['device']} @ {snap['rate']} Hz"
                     + (" — hearing speech" if snap["gate_open"] else ""))
        self.mic_live_state.setText(state)
        ev = snap["last_event"]
        if ev and snap["last_event_age"] is not None \
                and snap["last_event_age"] < 90:
            self.mic_live_event.setText(
                f"{ev} · {int(snap['last_event_age'])}s ago · "
                f"{snap['frames']} frames")
        else:
            self.mic_live_event.setText("")
        if snap["transcript"] and snap["transcript_age"] is not None \
                and snap["transcript_age"] < 600:
            self.mic_live_transcript.setText(
                f"“{snap['transcript']}”  ({int(snap['transcript_age'])}s ago)")
        else:
            self.mic_live_transcript.setText("—")
        QTimer.singleShot(200, self._mic_live_tick)

    def test_voice(self) -> None:
        self.voice_test_btn.setEnabled(False)
        self._status("synthesizing …")
        voice_name = self.voice_combo.currentData() or ""
        rate = self.rate_slider.value() / 100.0
        vol = self.vol_slider.value() / 100.0

        def worker():
            # load the SELECTED voice directly: the shared H globals
            # (PIPER_VOICE_NAME / _piper_voice cache) must not be disturbed
            import piper
            from piper import SynthesisConfig
            onnx = (H.PIPER_VOICE_DIR / voice_name
                    if voice_name
                    else next(iter(sorted(H.PIPER_VOICE_DIR.glob("*.onnx"))), None))
            if onnx is None or not onnx.exists():
                raise FileNotFoundError(
                    f"no piper voice (*.onnx) in {H.PIPER_VOICE_DIR} — download one first")
            try:
                voice = piper.PiperVoice.load(str(onnx), config_path=str(onnx) + ".json")
            except TypeError:  # very old piper builds without config_path
                voice = piper.PiperVoice.load(str(onnx))
            cfg = SynthesisConfig(length_scale=1.0 / max(0.5, rate), volume=max(0.1, vol))
            H.STATE_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix="voice-test-", suffix=".wav",
                                            dir=str(H.STATE_DIR))
            os.close(fd)
            wav = Path(tmp_name)
            try:
                with wave.open(str(wav), "wb") as f:
                    voice.synthesize_wav("Hello, I am your desktop assistant.", f, syn_config=cfg)
                H.play_wav(wav, threading.Event())
            finally:
                try:
                    wav.unlink(missing_ok=True)
                except OSError:
                    pass
            return "voice test played"

        def done(ok, result):
            self.voice_test_btn.setEnabled(True)
            self._status(str(result) if ok else f"voice test failed: {result}")

        self.run_bg(worker, done)

    # ------------------------------------------------------------------ memory

    def _memory_tab(self) -> QWidget:
        """Show what the AI currently remembers — demystifies 'why did it say
        that' (stale/poisoned history was the root cause of the parrot bug)."""
        w = QWidget(self)
        lay = QVBoxLayout(w)
        info = QLabel(
            "The bubble's conversation memory. New messages are added here; "
            "clearing it makes the assistant forget everything and start fresh.", w)
        info.setWordWrap(True)
        lay.addWidget(info)
        self.memory_view = QPlainTextEdit(self)
        self.memory_view.setReadOnly(True)
        lay.addWidget(self.memory_view)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self._refresh_memory)
        clear = QPushButton("Clear memory (fresh start)", self)
        clear.clicked.connect(self._clear_memory)
        row.addWidget(refresh)
        row.addWidget(clear, 1)
        lay.addLayout(row)
        self._refresh_memory()
        return w

    def _refresh_memory(self) -> None:
        try:
            data = json.loads(H.HISTORY_FILE.read_text(encoding="utf-8"))
            msgs = [m for m in data if isinstance(m, dict) and m.get("role")]
        except Exception:
            msgs = []
        if not msgs:
            self.memory_view.setPlainText("(memory is empty — fresh start)")
            return
        lines = [f"{len(msgs)} messages — newest last:", ""]
        for m in msgs:
            role = m.get("role")
            if m.get("tool_calls"):
                names = ", ".join(
                    (tc.get("function") or {}).get("name", "?")
                    for tc in m["tool_calls"])
                lines.append(f"[assistant] calls tool: {names}")
                continue
            if role == "tool":
                content = str(m.get("content"))[:150].replace("\n", " ")
                lines.append(f"[tool result] {content}")
                continue
            content = str(m.get("content"))[:150].replace("\n", " ")
            lines.append(f"[{role}] {content}")
        self.memory_view.setPlainText("\n".join(lines))

    def _clear_memory(self) -> None:
        try:
            if H.HISTORY_FILE.exists():
                _backup_keep_n(H.HISTORY_FILE, "bak-manual")
            self.memory_view.setPlainText("(memory cleared — backup saved)")
            self._status("memory cleared (backup saved). Restart the bubble to apply.")
        except OSError as e:
            self._status(f"cannot clear memory: {e}")

    # ------------------------------------------------------------------ history

    def _history_tab(self) -> QWidget:
        """Recent conversation history — same history.json store the bubble
        persists (and the Memory tab reads)."""
        w = QWidget(self)
        lay = QVBoxLayout(w)
        info = QLabel(
            "Recent conversation history. New messages are added here; "
            "clearing it makes the assistant forget everything and start fresh.", w)
        info.setWordWrap(True)
        lay.addWidget(info)
        self.history_view = QPlainTextEdit(self)
        self.history_view.setReadOnly(True)
        lay.addWidget(self.history_view)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self._refresh_history)
        clear = QPushButton("Clear history", self)
        clear.clicked.connect(self._clear_history)
        row.addWidget(refresh)
        row.addWidget(clear, 1)
        lay.addLayout(row)
        self._refresh_history()
        return w

    def _refresh_history(self) -> None:
        try:
            data = json.loads(H.HISTORY_FILE.read_text(encoding="utf-8"))
            msgs = [m for m in data if isinstance(m, dict) and m.get("role")]
        except Exception:
            msgs = []
        if not msgs:
            self.history_view.setPlainText("(history is empty — fresh start)")
            return
        lines = [f"{len(msgs)} messages — newest last:", ""]
        for m in msgs:
            role = m.get("role")
            if m.get("tool_calls"):
                names = ", ".join(
                    (tc.get("function") or {}).get("name", "?")
                    for tc in m["tool_calls"])
                lines.append(f"[assistant] calls tool: {names}")
                continue
            if role == "tool":
                content = str(m.get("content"))[:150].replace("\n", " ")
                lines.append(f"[tool result] {content}")
                continue
            content = str(m.get("content"))[:150].replace("\n", " ")
            lines.append(f"[{role}] {content}")
        self.history_view.setPlainText("\n".join(lines))

    def _clear_history(self) -> None:
        ok = QMessageBox.question(
            self, "Clear history",
            "Forget the recent conversation history? A backup is kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            return
        try:
            if H.HISTORY_FILE.exists():
                _backup_keep_n(H.HISTORY_FILE, "bak-manual")
            self.history_view.setPlainText("(history cleared — backup saved)")
            self._status("history cleared (backup saved). Restart the bubble to apply.")
        except OSError as e:
            self._status(f"cannot clear history: {e}")

    def _on_tab_changed(self, index: int) -> None:
        try:
            if getattr(self, "tabs", None) is not None and self.tabs.widget(index) is getattr(
                    self, "_history_page", None):
                self._refresh_history()
        except Exception:
            pass

    # ------------------------------------------------------------- permissions

    def _permissions_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        group = QGroupBox("Tools the assistant may use", w)
        form = QFormLayout(group)
        self.perm_checks: dict[str, QCheckBox] = {}
        labels = {
            "run_command": ("Run desktop commands", "whitelisted only: pactl, playerctl, "
                            "brightnessctl, niri, echo, cat, ls, pwd, notify-send"),
            "read_file": ("Read files", "any text file, including its own source code"),
            "edit_file": ("Write files", "only its own source and ~/.config/handsoff/"),
            "self_restart": ("Restart itself", "needed to apply self-modifications"),
            "type_text": ("Type into apps", "virtual keyboard into the focused window "
                          "(chat boxes, editors) via ydotool"),
            "press_keys": ("Press keys in apps", "key combos like enter/ctrl+v in the "
                           "focused window via ydotool"),
            "paste_text": ("Read your clipboard", "wl-paste: the AI can read whatever "
                           "you last copied — passwords included"),
            "copy_text": ("Write your clipboard", "wl-copy: the AI can replace whatever "
                          "you last copied"),
            "reminders": ("Reminders", "create, list, cancel and snooze spoken "
                          "reminders"),
            "calendar": ("Calendar", "read your ICS calendars and print month "
                         "grids"),
            "focus_window": ("Focus windows", "raise any window by (part of its) "
                             "title — e.g. 'bring up the calculator'"),
            "web_access": ("Internet knowledge", "weather (Open-Meteo), facts (Wikipedia), "
                           "web search (DuckDuckGo), world warnings — read-only, fixed endpoints"),
            "screen_access": ("See the screen", "screenshots + OCR of your display; the "
                              "AI can look at what you look at"),
            "operator": ("Mouse control", "move the pointer and click UI elements "
                         "(click_element by OCR text, click_at by pixel) — needed "
                         "for 'operate this app for me'"),
            "media": ("Control your music (MPD)", "play/pause/skip/search your MPD "
                      "library and set the music volume"),
            "notifications": ("Read desktop notifications", "opt-in future notification "
                              "reader; muted apps are filtered"),
            "pomodoro": ("Pomodoro timer", "work/break timer with spoken transitions"),
            "watchers": ("File/process watchers", "bounded monitors that announce matching "
                         "lines or process exits"),
        }
        for key, (title, desc) in labels.items():
            chk = QCheckBox(f"{title} — {desc}", self)
            self.perm_checks[key] = chk
            form.addRow(chk)
        lay.addWidget(group)

        extra_group = QGroupBox("Extra whitelisted commands (one per line)", w)
        el = QVBoxLayout(extra_group)
        self.extra_edit = QPlainTextEdit(self)
        self.extra_edit.setMaximumHeight(110)
        self.extra_edit.setPlaceholderText("e.g.\ngrep\ndate\nfree")
        el.addWidget(self.extra_edit)
        blocked = QLabel(
            "Always blocked, no matter what: " + ", ".join(H.ToolBelt.BLOCKED[:14]) + " …", w)
        blocked.setWordWrap(True)
        blocked.setStyleSheet("color: #888;")
        el.addWidget(blocked)
        lay.addWidget(extra_group)

        # centralized ALLOW/DENY/CONFIRM policy + dry-run rehearsal mode
        pol_group = QGroupBox("Command policy (ALLOW / DENY / CONFIRM per tool)", w)
        pl = QVBoxLayout(pol_group)
        # one row per declared tool, straight from the live registry — a tool
        # added in core/tools.py shows up here with no GUI change
        policy_form = QFormLayout()
        self.policy_rows: dict[str, QComboBox] = {}
        try:
            tool_names = sorted(
                t["function"]["name"] for t in (getattr(H, "TOOLS", None) or []))
        except (KeyError, TypeError, AttributeError):
            tool_names = []
        for name in tool_names:
            combo = QComboBox(self)
            combo.addItem("ALLOW", "ALLOW")
            combo.addItem("DENY", "DENY")
            combo.addItem("CONFIRM", "CONFIRM")
            policy_form.addRow(QLabel(name, self), combo)
            self.policy_rows[name] = combo
        pl.addLayout(policy_form)
        if not self.policy_rows:
            # registry unavailable (e.g. H degenerate in recovery mode): keep
            # the raw text path so policy editing still works
            self.policy_edit = QPlainTextEdit(self)
            self.policy_edit.setMaximumHeight(96)
            self.policy_edit.setPlaceholderText(
                "one per line:  tool = POLICY\n"
                "e.g.\nrun_command = DENY\nopen_app = CONFIRM\n"
                "CONFIRM asks the user out loud and runs only after a separate "
                "'yes' reply")
            pl.addWidget(self.policy_edit)
        self.dryrun_chk = QCheckBox(
            "Dry-run mode — desktop actions report what they would do, "
            "without doing it", self)
        pl.addWidget(self.dryrun_chk)
        lay.addWidget(pol_group)
        lay.addStretch(1)
        return w

    # -------------------------------------------------------------- appearance

    def _appearance_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        self.color_buttons: dict[str, QPushButton] = {}
        self._colors: dict[str, str] = dict(self.cfg["colors"])

        size_row = QHBoxLayout()
        self.size_slider = QSlider(Qt.Horizontal, self)
        self.size_slider.setRange(96, 192)
        self.size_label = QLabel("", self)
        self.size_slider.valueChanged.connect(
            lambda v: self.size_label.setText(f"{v} px window  ≈  {int(v * 0.69)} px bubble"))
        size_row.addWidget(QLabel("Bubble size", self))
        size_row.addWidget(self.size_slider, 1)
        size_row.addWidget(self.size_label)
        lay.addLayout(size_row)

        colors_row = QHBoxLayout()
        for key, title in (("idle", "Idle"), ("listening", "Listening"),
                           ("thinking", "Thinking"), ("speaking", "Speaking")):
            btn = QPushButton(title, self)
            btn.setFixedHeight(30)
            btn.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            self.color_buttons[key] = btn
            colors_row.addWidget(btn)
        reset = QPushButton("Reset", self)
        reset.clicked.connect(self._reset_colors)
        colors_row.addWidget(reset)
        lay.addLayout(colors_row)

        self.preview = BubblePreview(
            lambda: {k: QColor(c) for k, c in self._colors.items()},
            lambda: self.size_slider.value(),
        )
        lay.addWidget(self.preview)
        self._paint_color_buttons()
        return w

    def _paint_color_buttons(self) -> None:
        for key, btn in self.color_buttons.items():
            hexcol = self._colors[key]
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {hexcol}; color: white; "
                f"border: 1px solid #555; border-radius: 4px; }}")
            btn.setToolTip(hexcol)

    def _pick_color(self, key: str) -> None:
        col = QColorDialog.getColor(QColor(self._colors[key]), self, f"{key} colour")
        if col.isValid():
            self._colors[key] = col.name()
            self._paint_color_buttons()

    def _reset_colors(self) -> None:
        self._colors = dict(DEFAULT_SETTINGS["colors"])
        self._paint_color_buttons()

    # ----------------------------------------------------------------- startup

    def _startup_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        group = QGroupBox("Autostart", w)
        gl = QVBoxLayout(group)
        self.autostart_chk = QCheckBox("Start handsoff when niri starts", self)
        self.autostart_chk.setChecked(autostart_enabled())
        gl.addWidget(self.autostart_chk)
        note = QLabel(
            f"Adds one spawn-at-startup line to {NIRI_CONFIG}\n"
            "(a backup is written next to it before the first change).", w)
        note.setWordWrap(True)
        gl.addWidget(note)
        lay.addWidget(group)

        btns = QHBoxLayout()
        restart = QPushButton("Restart bubble now", self)
        restart.clicked.connect(self._on_restart_bubble)
        log_btn = QPushButton("Show log", self)
        log_btn.clicked.connect(self._show_log)
        kb_btn = QPushButton("Write niri keybind snippet", self)
        kb_btn.clicked.connect(self._write_keybinds)
        btns.addWidget(restart)
        btns.addWidget(log_btn)
        btns.addWidget(kb_btn)
        btns.addStretch(1)
        lay.addLayout(btns)

        paths = QLabel(
            f"source: {H.SELF_PATH}\nconfig: {H.CONFIG_DIR}\nlog: {H.LOG_FILE}", w)
        paths.setStyleSheet("color: #888; font-family: monospace; font-size: 11px;")
        lay.addWidget(paths)
        lay.addStretch(1)
        return w

    def _write_keybinds(self) -> None:
        exe = f"{HOME}/.local/bin/handsoff.py"
        path = H.CONFIG_DIR / "niri-keybinds.kdl"
        # inner shell words, KDL-escaped at write time so the snippet uses
        # the same sh -c exec dialect as niri-window-rule.kdl
        kdl = lambda s: s.replace('"', '\\"')  # noqa: E731
        toggle = f'"{exe}" "--ptt" "toggle"'
        interrupt = f'"{exe}" "--ptt" "interrupt"'
        handsfree = f'"{exe}" "--ptt" "handsfree"'
        status = f'"{exe}" "--ptt" "handsfree-status"'
        dictation = f'"{exe}" "--ptt" "dictation"'
        settings = f'"{exe}" "--ptt" "settings"'
        try:
            H.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "// handsoff — keyboard control. Merge these into the binds { ... } section\n"
                "// of ~/.config/niri/config.kdl, then reload niri's config.\n\n"
                "// press once to start talking, press again to send\n"
                f'    Mod+V repeat=false {{ spawn "sh" "-c" "exec python {kdl(toggle)}"; }}\n'
                "// make the bubble stop talking / thinking immediately\n"
                f'    Mod+Shift+V repeat=false {{ spawn "sh" "-c" "exec python {kdl(interrupt)}"; }}\n'
                "// toggle continuous hands-free listening on/off "
                "(confirms mic health out loud)\n"
                f'    Mod+Shift+H repeat=false {{ spawn "sh" "-c" "exec python {kdl(handsfree)}"; }}\n'
                "// ask the assistant to speak its hands-free / mic health state\n"
                f'    Mod+Shift+J repeat=false {{ spawn "sh" "-c" "exec python {kdl(status)}"; }}\n'
                f'    // voice dictation: what you say is TYPED into the focused window (no AI turn)\n'
                f'    Mod+Shift+D repeat=false {{ spawn "sh" "-c" "exec python {kdl(dictation)}"; }}\n'
                "// open the settings window (works even when the bubble is dead)\n"
                f'    Mod+Shift+S repeat=false {{ spawn "sh" "-c" "exec python {kdl(settings)}"; }}\n',
                encoding="utf-8",
            )
        except OSError as e:
            self._status(f"cannot write snippet: {e}")
            return
        self._status(f"snippet written to {path} — merge it into your binds {{ … }} section")

    def _on_restart_bubble(self) -> None:
        if H.RESTART_SCRIPT.exists():
            try:
                subprocess.Popen([str(H.RESTART_SCRIPT)], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (FileNotFoundError, OSError) as e:
                self._status(f"cannot restart bubble: {e}")
                return
            self._status("bubble restarting …")
        else:
            self._status("restart script missing — run install.sh")

    def _on_quit_bubble(self) -> None:
        def worker():
            # systemd owns the bubble: a bare SIGTERM is a "clean exit" and
            # Restart=always resurrects it — stop the unit instead
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "stop", "handsoff.service"],
                    capture_output=True, text=True, timeout=15)
                if r.returncode == 0:
                    return "bubble stopped (systemd unit)"
            except (OSError, subprocess.TimeoutExpired):
                pass
            killed = 0
            pg = subprocess.run(["pgrep", "-f", r"handsoff\.py"], capture_output=True,
                                text=True, timeout=10)
            for pid in pg.stdout.split():
                try:
                    exe = Path(f"/proc/{pid}/exe").resolve()
                    cmdline = Path(f"/proc/{pid}/cmdline")
                    if exe.name.startswith("python") and cmdline.exists() \
                            and f"{HOME}/.local/bin/handsoff.py" in \
                            cmdline.read_bytes().decode("utf-8", "replace").split("\x00"):
                        import signal
                        os.kill(int(pid), signal.SIGTERM)
                        killed += 1
                except (OSError, ValueError):
                    continue  # pid vanished mid-scan
            return f"stopped {killed} bubble process(es)"

        def done(ok, result):
            self._status(str(result) if ok else f"failed: {result}")

        self.run_bg(worker, done)

    def _show_log(self) -> None:
        if H.LOG_FILE.exists():
            try:
                subprocess.Popen(["xdg-open", str(H.LOG_FILE)])
            except (FileNotFoundError, OSError) as e:
                self._status(f"cannot open log: {e}")
                return
            self._status("opening log …")
        else:
            self._status("no log file yet — start the bubble first")

    # ------------------------------------------------------------ load & save

    def _settings_mtime(self) -> float:
        try:
            return H.SETTINGS_FILE.stat().st_mtime_ns
        except OSError:
            return 0.0

    def _check_disk_changes(self) -> None:
        mtime = self._settings_mtime()
        if mtime != self._disk_mtime:
            self._disk_mtime = mtime
            self.reload_from_disk()
            self._status("settings reloaded from disk (changed outside this window)")

    def reload_from_disk(self) -> None:
        """Re-read settings.json; an open window must never clobber external writes."""
        try:
            data = json.loads(H.SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.cfg = merge_settings(data)
        self._loaded_cfg = copy.deepcopy(self.cfg)
        self._load_values()

    def _load_values(self) -> None:
        self.ctx_spin.setValue(int(self.cfg["num_ctx"]))
        self.remote_ollama_chk.setChecked(
            bool(self.cfg.get("allow_remote_ollama", False)))
        self.hist_spin.setValue(int(self.cfg.get("history_tokens", 0)))
        self.toolrate_spin.setValue(int(self.cfg.get("max_tool_calls", 0)))
        pol = self.cfg.get("command_policy") or {}
        if isinstance(pol, dict) and self.policy_rows:
            for name, combo in self.policy_rows.items():
                idx = combo.findData(str(pol.get(name, "ALLOW")).upper())
                combo.setCurrentIndex(idx if idx >= 0 else 0)
        elif isinstance(pol, dict) and pol and getattr(self, "policy_edit", None) is not None:
            self.policy_edit.setPlainText(
                "\n".join(f"{k} = {v}" for k, v in sorted(pol.items())))
        self.dryrun_chk.setChecked(bool(self.cfg.get("dry_run", False)))
        self.thresh_spin.setValue(int(self.cfg["mic_threshold"]))
        wi = self.whisper_combo.findData(self.cfg["whisper_size"])
        self.whisper_combo.setCurrentIndex(max(0, wi))
        self.rate_slider.setValue(int(float(self.cfg["tts_rate"]) * 100))
        self.vol_slider.setValue(int(float(self.cfg["tts_volume"]) * 100))
        self.size_slider.setValue(int(self.cfg["bubble_size"]))
        self.hf_chk.setChecked(bool(self.cfg.get("handsfree", False)))
        self.wake_name_edit.setText(str(self.cfg.get("assistant_name", "assistant")))
        self.wake_chk.setChecked(bool(self.cfg.get("wake_word_required", False)))
        self.wake_secs.setValue(int(float(self.cfg.get("engage_seconds", 45.0))))
        self.followup_secs.setValue(
            int(float(self.cfg.get("followup_seconds", 0.0))))
        self.home_edit.setText(str(self.cfg.get("home_place", "")))
        self.cal_edit.setText(", ".join(self.cfg.get("calendar_ics") or []))
        self.spotter_chk.setChecked(bool(self.cfg.get("wake_spotter", False)))
        self.spotter_edit.setText(", ".join(self.cfg.get("spotter_models") or []))
        self.selfheal_chk.setChecked(bool(self.cfg.get("mic_selfheal", True)))
        self.resource_chk.setChecked(bool(self.cfg.get("resource_alerts", False)))
        self.ram_alert_spin.setValue(int(float(self.cfg.get("ram_alert_percent", 90.0))))
        self.vram_alert_spin.setValue(int(float(self.cfg.get("vram_alert_percent", 90.0))))
        self.notification_chk.setChecked(bool(self.cfg.get("notification_reader", False)))
        self.notification_mute_edit.setText(", ".join(self.cfg.get("notification_mute_apps") or []))
        self.dictation_chk.setChecked(bool(self.cfg.get("dictation", True)))
        self.brief_chk.setChecked(bool(self.cfg.get("briefing", False)))
        self.world_warn_chk.setChecked(bool(self.cfg.get("world_warnings", False)))
        self.hw_watch_chk.setChecked(bool(self.cfg.get("hardware_watch", False)))
        self.hw_cool_spin.setValue(int(float(self.cfg.get("hardware_cooldown_min", 60.0))))
        self.hw_disk_spin.setValue(int(float(self.cfg.get("hardware_disk_gb", 5.0))))
        aliases = self.cfg.get("workspace_aliases") or {}
        self.alias_edit.setPlainText(
            "\n".join(f"{k} = {v}" for k, v in sorted(aliases.items())))
        self.extra_edit.setPlainText("\n".join(self.cfg["extra_allowed_commands"]))
        for key, chk in self.perm_checks.items():
            chk.setChecked(bool(self.cfg["permissions"].get(key, True)))
        self._colors = dict(self.cfg["colors"])
        self._paint_color_buttons()

    def _collect(self) -> list[str]:
        problems: list[str] = []
        self.cfg["ollama_host"] = self.host_edit.text().strip() or DEFAULT_SETTINGS["ollama_host"]
        self.cfg["allow_remote_ollama"] = self.remote_ollama_chk.isChecked()
        self.cfg["model"] = self._selected_model() or self.cfg["model"]
        self.cfg["num_ctx"] = self.ctx_spin.value()
        self.cfg["history_tokens"] = self.hist_spin.value()
        self.cfg["max_tool_calls"] = self.toolrate_spin.value()
        self.cfg["whisper_size"] = self.whisper_combo.currentData() or "tiny"
        self.cfg["piper_voice"] = self.voice_combo.currentData() or ""
        self.cfg["tts_rate"] = self.rate_slider.value() / 100.0
        self.cfg["tts_volume"] = self.vol_slider.value() / 100.0
        self.cfg["mic_device"] = self.mic_combo.currentData() or ""
        self.cfg["mic_threshold"] = self.thresh_spin.value()
        self.cfg["handsfree"] = self.hf_chk.isChecked()
        self.cfg["assistant_name"] = (
            self.wake_name_edit.text().strip() or DEFAULT_SETTINGS["assistant_name"])
        self.cfg["wake_word_required"] = self.wake_chk.isChecked()
        self.cfg["engage_seconds"] = float(self.wake_secs.value())
        self.cfg["followup_seconds"] = float(self.followup_secs.value())
        self.cfg["home_place"] = self.home_edit.text().strip()
        self.cfg["briefing"] = self.brief_chk.isChecked()
        self.cfg["world_warnings"] = self.world_warn_chk.isChecked()
        self.cfg["hardware_watch"] = self.hw_watch_chk.isChecked()
        self.cfg["hardware_cooldown_min"] = float(self.hw_cool_spin.value())
        self.cfg["hardware_disk_gb"] = float(self.hw_disk_spin.value())
        self.cfg["calendar_ics"] = [
            x.strip() for x in self.cal_edit.text().split(",") if x.strip()]
        self.cfg["wake_spotter"] = self.spotter_chk.isChecked()
        self.cfg["spotter_models"] = [
            x.strip() for x in self.spotter_edit.text().split(",") if x.strip()]
        self.cfg["mic_selfheal"] = self.selfheal_chk.isChecked()
        self.cfg["resource_alerts"] = self.resource_chk.isChecked()
        self.cfg["ram_alert_percent"] = float(self.ram_alert_spin.value())
        self.cfg["vram_alert_percent"] = float(self.vram_alert_spin.value())
        self.cfg["notification_reader"] = self.notification_chk.isChecked()
        self.cfg["notification_mute_apps"] = [
            x.strip().lower() for x in self.notification_mute_edit.text().split(",") if x.strip()][:32]
        self.cfg["dictation"] = self.dictation_chk.isChecked()
        alias_map = {}
        for lineno, line in enumerate(self.alias_edit.toPlainText().splitlines(), 1):
            if not line.strip() or line.strip().startswith("#"):
                continue
            if "=" not in line and ":" not in line:
                problems.append(
                    f"workspace alias line {lineno} needs 'name = value' — not saved")
                continue
            k, _, v = line.replace(":", "=").partition("=")
            k, v = k.strip().lower(), v.strip()
            if k and v:
                alias_map[k] = v
        self.cfg["workspace_aliases"] = alias_map
        self.cfg["bubble_size"] = self.size_slider.value()
        self.cfg["colors"] = dict(self._colors)
        self.cfg["permissions"] = {k: chk.isChecked() for k, chk in self.perm_checks.items()}
        policy_map: dict[str, str] = {}
        if self.policy_rows:
            # minimal map: ALLOW is the default, keep settings.json clean
            for name, combo in self.policy_rows.items():
                if combo.currentData() != "ALLOW":
                    policy_map[name] = combo.currentData()
        elif getattr(self, "policy_edit", None) is not None:
            for line in self.policy_edit.toPlainText().splitlines():
                if "=" not in line or line.strip().startswith("#"):
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().upper()
                if k and v in ("ALLOW", "DENY", "CONFIRM"):
                    policy_map[k] = v
        self.cfg["command_policy"] = policy_map
        self.cfg["dry_run"] = self.dryrun_chk.isChecked()
        self.cfg["extra_allowed_commands"] = [
            line.strip() for line in self.extra_edit.toPlainText().splitlines() if line.strip()]
        self.cfg["autostart"] = self.autostart_chk.isChecked()
        return problems

    def save(self) -> bool:
        problems = self._collect()
        if not self.cfg["model"]:
            self._status("pick a model first (Brain tab)")
            return False
        blocked_chosen = [c for c in self.cfg["extra_allowed_commands"]
                          if any(c == bad or c.split("/")[-1] == bad for bad in H.ToolBelt.BLOCKED)]
        if blocked_chosen:
            # save everything else: drop the refused entries, report them
            self.cfg["extra_allowed_commands"] = [
                c for c in self.cfg["extra_allowed_commands"] if c not in blocked_chosen]
            problems.append("not saved, always blocked: " + ", ".join(blocked_chosen))
        new_model = str(self.cfg["model"])
        cleared_note = ""
        if new_model != self._model_at_open:
            # a new model must not inherit a conversation tuned for the old one:
            # the old history is the #1 cause of parroting after a model switch
            try:
                if H.HISTORY_FILE.exists():
                    _backup_keep_n(H.HISTORY_FILE, "bak-modelswitch")
                self._model_at_open = new_model
                cleared_note = "  Memory cleared for the new model (backup saved)."
            except OSError:
                pass
        try:
            # shared writer: version-stamps settings.json and keeps a one-
            # generation backup, so the bubble can migrate layouts safely
            written = H._SETTINGS_OBJ.write_all(
                self.cfg, expected_data=self._loaded_cfg)
        except H._core_settings.SettingsConflictError as e:
            self._status(str(e) + ". Reload before saving.")
            return False
        except OSError as e:
            self._status(f"cannot save settings: {e}")
            return False
        self.cfg = written
        self._loaded_cfg = copy.deepcopy(written)
        self._disk_mtime = self._settings_mtime()
        # One autostart owner, same rule as the installer: when the systemd
        # user unit manages the bubble, niri spawn-at-startup is NOT added.
        msg = apply_autostart(self.autostart_chk.isChecked())
        warn = f"  WARNINGS: {'; '.join(problems)}" if problems else ""
        self._status(f"Saved to {H.SETTINGS_FILE}. {msg}{cleared_note}{warn}")
        return True

    def _on_save(self) -> None:
        self.save()

    def _on_apply(self) -> None:
        if self.save():
            self._on_restart_bubble()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("handsoff-settings")
    win = SettingsWindow()
    win.show()
    return app.exec()


def selftest() -> int:
    app = QApplication(sys.argv)
    win = SettingsWindow()
    win.resize(780, 600)
    win.refresh_mics()
    win.refresh_models()
    win.refresh_voices()
    QTimer.singleShot(4000, app.quit)
    app.exec()
    print(f"models listed: {win.model_list.count()}, "
          f"mics: {win.mic_combo.count()}, voices: {win.voice_combo.count()}")
    ok = win.save()
    print("settings.json written:", H.SETTINGS_FILE, "->", ok)
    return 0 if ok and win.model_list.count() > 0 else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
