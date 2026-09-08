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
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path.home()


def _import_handsoff():
    """Load the bubble's module for shared paths, defaults and helpers."""
    for cand in (HOME / ".local/bin/handsoff.py", Path(__file__).resolve().parent / "handsoff.py"):
        if cand.exists():
            spec = importlib.util.spec_from_file_location("handsoff_core", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.stderr.write("handsoff-settings: cannot find handsoff.py — install handsoff first\n")
    sys.exit(1)


H = _import_handsoff()

import numpy as np  # noqa: E402  (after handsoff, which already required it)
import sounddevice as sd  # noqa: E402

from PySide6.QtCore import QElapsedTimer, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QRadialGradient, QBrush, QPen  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QSlider, QSpinBox,
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
AUTOSTART_LINE = f'spawn-at-startup "python" "{HOME}/.local/bin/handsoff.py"'
AUTOSTART_COMMENT = "// handsoff voice assistant bubble"


def merge_settings(data: dict) -> dict:
    """defaults <- settings.json values (dicts merge key-wise, like the bubble
    does), then run the SHARED coercion from handsoff.py: a hand-edited
    settings.json with garbage values ("1,5", "32k", "abc") must produce a
    working UI, not a crashed recovery tool."""
    merged = json.loads(json.dumps(H.DEFAULT_SETTINGS))  # deep copy of defaults
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in merged:
                continue
            if isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k].update(v)
            else:
                merged[k] = v
    return H.coerce_settings(merged)


def http_json(url: str, payload: dict | None = None, timeout: int = 10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def autostart_enabled() -> bool:
    try:
        return any(AUTOSTART_LINE in line for line in NIRI_CONFIG.read_text().splitlines())
    except OSError:
        return False


def _reload_niri() -> None:
    try:
        subprocess.run(["niri", "msg", "action", "load-config-file"],
                       timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def set_autostart(enable: bool) -> str:
    """Add or remove the spawn-at-startup line in the niri config (with backup)."""
    try:
        if not NIRI_CONFIG.exists():
            if not enable:
                return "no niri config found — nothing to change"
            NIRI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            NIRI_CONFIG.write_text("// niri config\n")
        lines = NIRI_CONFIG.read_text().splitlines()
        has = any(AUTOSTART_LINE in line for line in lines)
        bak = NIRI_CONFIG.with_name(NIRI_CONFIG.name + ".bak-handsoff")
        if enable and not has:
            if not bak.exists():
                shutil.copy2(NIRI_CONFIG, bak)
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(AUTOSTART_COMMENT)
            lines.append(AUTOSTART_LINE)
            NIRI_CONFIG.write_text("\n".join(lines) + "\n")
            _reload_niri()
            return "autostart line added to niri config"
        if not enable and has:
            if not bak.exists():
                shutil.copy2(NIRI_CONFIG, bak)
            lines = [line for line in lines
                     if AUTOSTART_LINE not in line and line.strip() != AUTOSTART_COMMENT]
            NIRI_CONFIG.write_text("\n".join(lines) + "\n")
            _reload_niri()
            return "autostart line removed from niri config"
        return "autostart unchanged"
    except OSError as e:
        return f"error editing {NIRI_CONFIG}: {e}"


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

        def fetch(target: Path, src: str) -> None:
            tmp = target.with_suffix(target.suffix + ".part")
            req = urllib.request.Request(src, headers={"User-Agent": "handsoff/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
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
            os.replace(tmp, target)

        def worker() -> None:
            try:
                if not dest.exists():
                    fetch(dest, url)
                else:
                    prog["msg"] = f"{name} already present"
                cfg = Path(str(dest) + ".json")
                if not cfg.exists():
                    prog["msg"] = f"{cfg.name}: downloading"
                    fetch(cfg, url + ".json")
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


class SettingsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("handsoff settings")
        self.resize(780, 600)
        import copy
        self.cfg = copy.deepcopy(H.SETTINGS)
        self._model_at_open = str(self.cfg.get("model") or "")
        self._state_dir_ready()

        tabs = QTabWidget(self)
        tabs.addTab(self._brain_tab(), "Brain")
        tabs.addTab(self._voice_tab(), "Voice")
        tabs.addTab(self._permissions_tab(), "Permissions")
        tabs.addTab(self._memory_tab(), "Memory")
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._startup_tab(), "Startup")
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
        base = (self.host_edit.text().strip() or H.DEFAULT_SETTINGS["ollama_host"]).rstrip("/")
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
        base = (self.host_edit.text().strip() or H.DEFAULT_SETTINGS["ollama_host"]).rstrip("/")
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

    def test_voice(self) -> None:
        self.voice_test_btn.setEnabled(False)
        self._status("synthesizing …")
        voice_name = self.voice_combo.currentData() or ""
        rate = self.rate_slider.value() / 100.0
        vol = self.vol_slider.value() / 100.0

        def worker():
            global_backup = H.PIPER_VOICE_NAME
            try:
                H.PIPER_VOICE_NAME = voice_name
                H._piper_voice = None   # fresh synth for the SELECTED voice,
                                        # incl. "(first voice file found)"
                voice = H.get_piper()
                from piper import SynthesisConfig
                cfg = SynthesisConfig(length_scale=1.0 / max(0.5, rate), volume=max(0.1, vol))
                wav = H.STATE_DIR / "voice-test.wav"
                with __import__("wave").open(str(wav), "wb") as f:
                    voice.synthesize_wav("Hello, I am your desktop assistant.", f, syn_config=cfg)
                H.play_wav(wav, threading.Event())
                wav.unlink(missing_ok=True)
            finally:
                H.PIPER_VOICE_NAME = global_backup
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
                H.HISTORY_FILE.replace(
                    H.HISTORY_FILE.with_suffix(".json.bak-manual"))
            self.memory_view.setPlainText("(memory cleared — backup saved)")
            self._status("memory cleared (backup saved). Restart the bubble to apply.")
        except OSError as e:
            self._status(f"cannot clear memory: {e}")

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
            "web_access": ("Internet knowledge", "weather (Open-Meteo), facts (Wikipedia), "
                           "web search (DuckDuckGo) — read-only, fixed endpoints"),
            "screen_access": ("See the screen", "screenshots + OCR of your display; the "
                              "AI can look at what you look at"),
            "media": ("Control your music (MPD)", "play/pause/skip/search your MPD "
                      "library and set the music volume"),
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
        self._colors = dict(H.DEFAULT_SETTINGS["colors"])
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
        try:
            H.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "// handsoff — keyboard control. Merge these into the binds { ... } section\n"
                "// of ~/.config/niri/config.kdl, then reload niri's config.\n\n"
                "// press once to start talking, press again to send\n"
                f'    Mod+V repeat=false {{ spawn "python" "{exe}" "--ptt" "toggle"; }}\n'
                "// make the bubble stop talking / thinking immediately\n"
                f'    Mod+Shift+V repeat=false {{ spawn "python" "{exe}" "--ptt" "interrupt"; }}\n'
                "// toggle continuous hands-free listening on/off\n"
                f'    Mod+Shift+H repeat=false {{ spawn "python" "{exe}" "--ptt" "handsfree"; }}\n'
                "// open the settings window (works even when the bubble is dead)\n"
                f'    Mod+Shift+S repeat=false {{ spawn "python" "{exe}" "--ptt" "settings"; }}\n',
                encoding="utf-8",
            )
        except OSError as e:
            self._status(f"cannot write snippet: {e}")
            return
        self._status(f"snippet written to {path} — merge it into your binds {{ … }} section")

    def _on_restart_bubble(self) -> None:
        if H.RESTART_SCRIPT.exists():
            subprocess.Popen([str(H.RESTART_SCRIPT)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                exe = Path(f"/proc/{pid}/exe").resolve()
                if exe.name.startswith("python") and Path(f"/proc/{pid}/cmdline").exists() \
                        and f"{HOME}/.local/bin/handsoff.py" in \
                        Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").split("\x00"):
                    import signal
                    os.kill(int(pid), signal.SIGTERM)
                    killed += 1
            return f"stopped {killed} bubble process(es)"

        def done(ok, result):
            self._status(str(result) if ok else f"failed: {result}")

        self.run_bg(worker, done)

    def _show_log(self) -> None:
        if H.LOG_FILE.exists():
            subprocess.Popen(["xdg-open", str(H.LOG_FILE)])
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
        self._load_values()

    def _load_values(self) -> None:
        self.ctx_spin.setValue(int(self.cfg["num_ctx"]))
        self.hist_spin.setValue(int(self.cfg.get("history_tokens", 0)))
        self.toolrate_spin.setValue(int(self.cfg.get("max_tool_calls", 0)))
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
        self.home_edit.setText(str(self.cfg.get("home_place", "")))
        self.cal_edit.setText(", ".join(self.cfg.get("calendar_ics") or []))
        self.spotter_chk.setChecked(bool(self.cfg.get("wake_spotter", False)))
        self.spotter_edit.setText(", ".join(self.cfg.get("spotter_models") or []))
        self.brief_chk.setChecked(bool(self.cfg.get("briefing", False)))
        aliases = self.cfg.get("workspace_aliases") or {}
        self.alias_edit.setPlainText(
            "\n".join(f"{k} = {v}" for k, v in sorted(aliases.items())))
        self.extra_edit.setPlainText("\n".join(self.cfg["extra_allowed_commands"]))
        for key, chk in self.perm_checks.items():
            chk.setChecked(bool(self.cfg["permissions"].get(key, True)))
        self._colors = dict(self.cfg["colors"])
        self._paint_color_buttons()

    def _collect(self) -> None:
        self.cfg["ollama_host"] = self.host_edit.text().strip() or H.DEFAULT_SETTINGS["ollama_host"]
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
            self.wake_name_edit.text().strip() or H.DEFAULT_SETTINGS["assistant_name"])
        self.cfg["wake_word_required"] = self.wake_chk.isChecked()
        self.cfg["engage_seconds"] = float(self.wake_secs.value())
        self.cfg["home_place"] = self.home_edit.text().strip()
        self.cfg["briefing"] = self.brief_chk.isChecked()
        self.cfg["calendar_ics"] = [
            x.strip() for x in self.cal_edit.text().split(",") if x.strip()]
        self.cfg["wake_spotter"] = self.spotter_chk.isChecked()
        self.cfg["spotter_models"] = [
            x.strip() for x in self.spotter_edit.text().split(",") if x.strip()]
        alias_map = {}
        for line in self.alias_edit.toPlainText().splitlines():
            if "=" not in line and ":" not in line:
                continue
            k, _, v = line.replace(":", "=").partition("=")
            k, v = k.strip().lower(), v.strip()
            if k and v:
                alias_map[k] = v
        self.cfg["workspace_aliases"] = alias_map
        self.cfg["bubble_size"] = self.size_slider.value()
        self.cfg["colors"] = dict(self._colors)
        self.cfg["permissions"] = {k: chk.isChecked() for k, chk in self.perm_checks.items()}
        self.cfg["extra_allowed_commands"] = [
            line.strip() for line in self.extra_edit.toPlainText().splitlines() if line.strip()]
        self.cfg["autostart"] = self.autostart_chk.isChecked()

    def save(self) -> bool:
        self._collect()
        if not self.cfg["model"]:
            self._status("pick a model first (Brain tab)")
            return False
        new_model = str(self.cfg["model"])
        cleared_note = ""
        if new_model != self._model_at_open:
            # a new model must not inherit a conversation tuned for the old one:
            # the old history is the #1 cause of parroting after a model switch
            try:
                if H.HISTORY_FILE.exists():
                    H.HISTORY_FILE.replace(
                        H.HISTORY_FILE.with_suffix(".json.bak-modelswitch"))
                self._model_at_open = new_model
                cleared_note = "  Memory cleared for the new model (backup saved)."
            except OSError:
                pass
        blocked_chosen = [c for c in self.cfg["extra_allowed_commands"]
                          if any(c == bad or c.split("/")[-1] == bad for bad in H.ToolBelt.BLOCKED)]
        try:
            H.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = H.SETTINGS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, H.SETTINGS_FILE)
        except OSError as e:
            self._status(f"cannot save settings: {e}")
            return False
        # One autostart owner, same rule as the installer: if the systemd
        # user unit manages the bubble, spawn-at-startup would double-start
        # it (the lock blocks the second bubble but ownership gets murky).
        if self.autostart_chk.isChecked():
            try:
                unit_enabled = subprocess.run(
                    ["systemctl", "--user", "is-enabled", "handsoff.service"],
                    capture_output=True, text=True, timeout=5,
                ).returncode == 0
            except Exception:
                unit_enabled = False
            if unit_enabled:
                self._status("Saved to settings. Autostart: systemd already owns it "
                             "— niri spawn-at-startup NOT added.")
                return True
        msg = set_autostart(self.autostart_chk.isChecked())
        warn = f"  (ignored, always blocked: {', '.join(blocked_chosen)})" if blocked_chosen else ""
        self._status(f"Saved to {H.SETTINGS_FILE}. {msg}{warn}{cleared_note}")
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
