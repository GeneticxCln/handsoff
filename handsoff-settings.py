#!/usr/bin/env python
# handsoff-self-marker: this line must be preserved across self-edits
"""
handsoff settings — a small GUI for the handsoff voice assistant bubble.

    python ~/.local/bin/handsoff-settings.py

Edits ~/.config/handsoff/settings.json (the bubble reads it at startup):

    Brain        Ollama host, model picker with capability badges, context size
    Voice        microphone + threshold, whisper size, speech rate/volume,
                 speech rate and volume, test buttons
    Permissions  enable/disable tools, extra whitelisted commands
    Appearance   bubble size and the four state colours (live preview)
    Startup      niri autostart entry, restart the bubble, open the log

Add `--selftest` to run a headless smoke test (no window is shown).
"""
from __future__ import annotations

import copy
import html
import json
import logging
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
from datetime import datetime as _dt
from pathlib import Path

# `core` owns the app's canonical module name and the one loader that can admit
# it (core.load_app_module). Imported rather than restated: two spellings of the
# name is how "is the bubble already loaded?" stopped having an answer. It is a
# light package (stdlib-only at import), so this does not pull the bubble, which
# is exactly what `_LazyHandsoff` exists to defer. An install with no core/
# cannot run this window at all — every helper below comes through the bubble.
import core as _core

HOME = Path.home()


def _import_handsoff():
    """Load the bubble's module for shared paths and helpers.

    ONE shared order everywhere: beside-this-file first, then the installed
    copy — so a repo checkout never silently runs installed code (or vice
    versa) when both exist.

    A bubble ALREADY loaded in this process wins over both, and that is the only
    correct answer: exec'ing a second copy is not a second view of one app, it is
    a second app — its own CONFIG_DIR/STATE_DIR/SETTINGS, its own model mirrors —
    and its module body calls `core.audio.configure(...)`, which repoints the
    SHARED core.audio at the second copy's paths. Measured: loading this app
    in-process next to a bubble repointed `core.audio.WHISPER_MODEL_DIR` at that
    copy's (real) config dir, so the rest of the process silently used another
    app's paths.

    The name and the admission rule are NOT restated here: `core` owns both, and
    `handsoff.py` claims its own name as it loads — the single definition is what
    stops the app and its callers drifting onto different names, which is exactly
    how the second copy used to happen.
    """
    try:
        return _core.load_app_module(
            (Path(__file__).resolve().parent / "handsoff.py",
             HOME / ".local/bin/handsoff.py"))
    except ImportError as e:
        sys.stderr.write(f"handsoff-settings: {e}\n")
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
    """Load settings_schema.py (the single source of DEFAULT_SETTINGS).

    The settings GUI must not pull its defaults through the bubble module: that
    forced a full bubble exec (audio imports, Qt globals) just to merge defaults,
    and duplicated the schema in the pre-2026-09 monolith. The schema is a
    dependency-free dict — and it is a SUPPORT module, so it goes through the
    same loader as every other one (`core.load_module`), which returns the copy
    already in `sys.modules` instead of exec'ing a second one. Loading it by hand
    under a private name gave this process two schema dicts whenever the bubble
    was loaded first, the same defect shape as two apps.
    """
    try:
        return _core.load_module("settings_schema")
    except ImportError as e:
        sys.stderr.write(f"handsoff-settings: {e}\n")
        sys.exit(1)


SCHEMA = _import_settings_schema()
DEFAULT_SETTINGS = SCHEMA.DEFAULT_SETTINGS
SETTINGS_VERSION = SCHEMA.SETTINGS_VERSION


def _page_fields(tab: str) -> tuple:
    """Every row the table puts on one page, in the table's own order.

    A page is a list of rows and nothing else: that is what makes adding a
    setting one line in settings_schema.py rather than a widget here plus a load
    line plus a collect line.
    """
    return tuple(field for field in getattr(SCHEMA, "SETTINGS_FIELDS", ())
                 if getattr(field, "tab", "") == tab)


def _page_groups(page: str) -> tuple:
    """The CARDS of one page, in the order the table declares them.

    A page is a loop over these; the rows of each card are the table's. That is
    what makes placement — which card a setting appears in — part of the one
    declaration, so a new setting needs no line in this file to be visible.
    """
    getter = getattr(SCHEMA, "page_groups", None)
    return tuple(getter(page)) if getter else ()


def _group_fields(page: str, group: str) -> tuple:
    """The rows of one card, in the table's own order."""
    getter = getattr(SCHEMA, "group_fields", None)
    if getter is None:                  # an older schema: the whole page
        return tuple(field for field in getattr(SCHEMA, "SETTINGS_FIELDS", ())
                     if getattr(field, "tab", "") == page)
    return tuple(getter(page, group))


def _group_title(page: str, group: str) -> str:
    """What a card says; the group's own key when this build has no title."""
    getter = getattr(SCHEMA, "group_title", None)
    return getter(page, group) if getter else group


def _group_hint(page: str, group: str) -> str:
    """The line a card shows under its title ("" when it shows none).

    The subtitle belongs to the card rather than to the page that draws it, so
    it lives beside the title in the table — including for the Appearance cards,
    whose subtitles used to be written here as strings the schema could not see.
    """
    getter = getattr(SCHEMA, "group_hint", None)
    return getter(page, group) if getter else ""


class _Rows:
    """Where a card's rows go: a form, or a card's own column of rows.

    One renderer and two layouts, so a page can be written the same way whether
    its cards are `QGroupBox` + `QFormLayout` (Voice, Brain, Permissions) or the
    Appearance tab's framed cards — and a row's placement does not depend on
    which one it is.
    """

    def __init__(self, window, target, style: str) -> None:
        self.window = window
        self.target = target
        self.style = style

    def add(self, control) -> None:
        """Place one control as a labelled row."""
        if control is None:
            return
        if self.style == "card":
            self.target.addLayout(
                self.window._field(control.title, control.row))
        else:
            self.target.addRow(
                "" if control.labelled else control.title, control.row)

    def widget(self, widget) -> None:
        """Place a bare widget (a note, a progress bar, a button row)."""
        if self.style == "card":
            self.target.addWidget(widget)
        else:
            self.target.addRow(widget)

    def layout(self, layout) -> None:
        """Place a layout (the shared rows: two thresholds side by side)."""
        if self.style == "card":
            self.target.addLayout(layout)
        else:
            self.target.addRow(layout)

    def labelled(self, label: str, widget) -> None:
        """Place a widget with its OWN label, in the column labels go in.

        `_field` builds that column for a card (a fixed-width name beside the
        control); a form has one already, so the label goes in its own cell. One
        helper for both, because a row that says what it is should read the same
        on either kind of page.
        """
        if self.style == "card":
            self.target.addLayout(self.window._field(label, widget))
        else:
            self.target.addRow(label, widget)


def _state_image_keys() -> tuple:
    """[(state, setting key)] for the `image` design's pictures per state.

    Derived from the schema's `DESIGN_IMAGE_KEYS`, so the four buttons, the four
    settings this panel writes and the four the bubble reads are ONE list rather
    than three that can drift apart. Empty on an older schema, in which case the
    card simply shows no per-state row instead of raising.
    """
    prefix = "design_image_"
    return tuple((key[len(prefix):], key)
                 for key in getattr(SCHEMA, "DESIGN_IMAGE_KEYS", ())
                 if key.startswith(prefix))


STATE_IMAGE_KEYS = _state_image_keys()


def _state_colour_keys() -> tuple:
    """The state names the colour row swatches, in the schema's own order.

    Derived from the KEYS of `DEFAULT_SETTINGS["colors"]` — which is exactly
    the set `core.settings._coerce_colors` keeps (it drops a key that dict does
    not have and fills a missing one from it), so the row and the loader cannot
    disagree about what a state colour is.

    This used to be a hand-written four-name tuple on the window class, which
    is the same shape of defect that dropped `get_datetime` from `permissions`:
    `_collect` writes the whole `colors` sub-dict from this key set, the
    three-way merge reads a key that is missing from the candidate as a DELETE,
    and the loader puts the default back — so a fifth state declared in the
    schema would be silently re-defaulted on every single save. Reading the
    loader's own key set makes a new state appear in the row by itself.
    """
    colors = DEFAULT_SETTINGS.get("colors")
    if isinstance(colors, dict) and colors:
        return tuple(str(k) for k in colors)
    return tuple(getattr(SCHEMA, "BUBBLE_STATES", ()) or ())


def _deco_colour_words() -> tuple:
    """The word values `avatar_deco_color` accepts besides a literal hex.

    The schema owns this vocabulary (`AVATAR_DECO_COLORS`). The picker row and
    the load path each spelled the pair out by hand, so a third word the schema
    declared would be offered by the combo and still not be selectable here:
    the row would show "custom" for a value that is not a colour of its own,
    and the swatch would open on it as though it were one.
    """
    words = tuple(str(m).strip().lower()
                  for m in getattr(SCHEMA, "AVATAR_DECO_COLORS", ()))
    return words or ("state", "rainbow")


def _core_module(name: str):
    """An extracted module through the shared loader, loaded on first use.

    The settings app reaches core directly instead of through the bubble: the
    bubble stopped re-exporting core's names, and going through it also meant
    this process could only use machinery after a full bubble exec.
    """
    return _core.load_module(name)

H = _LazyHandsoff()

# Same logger name as the bubble, so a live-apply failure lands in handsoff.log
# next to everything else rather than vanishing into the GUI process's stderr.
log = logging.getLogger("handsoff")

try:                                  # core.theme ships with the app's core/ dir;
    from core import theme as _THEME  # a partial install just disables wallpaper
except ImportError:                   # tuning instead of refusing to open
    _THEME = None

import numpy as np  # noqa: E402  (after handsoff, which already required it)
import sounddevice as sd  # noqa: E402

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPalette, QPolygonF, QRadialGradient, QBrush, QPen  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFrame,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

# Spoken by Settings → Voice "Test voice". Kept short: the answer comes back
# over the control socket, and `say` caps the payload anyway.
_VOICE_TEST_LINE = "Hello, I am your desktop assistant."

# The piper voice catalog and its download dialog are GONE. chatterbox-turbo
# ships exactly one built-in voice and conditions on an optional reference
# clip, so there is nothing left to choose from a list of downloadable voices.

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
    # Shared coercion without exec'ing the whole bubble: core.settings owns it,
    # and it is the same function the bubble calls (the bubble stopped
    # re-exporting core's names, so there is nothing to fall back TO).
    return _core_module("settings").coerce_settings(merged)


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
    directory (no predictable .tmp name); existing permissions are kept.

    "Kept" is now true: `mkstemp` creates the temp 0600 and `os.replace` would
    carry that mode onto the destination, silently tightening (or loosening) a
    file the user had chmod'ed. The destination's own mode is copied onto the
    temp BEFORE the replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = None            # new file: leave mkstemp's 0600 alone
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp_name, mode)
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
        # copy2 preserves the SOURCE mode, so backing up a file that was still
        # group/world-readable produced a readable copy of the same transcript.
        # These backups hold conversations and settings: always owner-only.
        os.chmod(bak, 0o600)
    except OSError:
        pass
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


def _reference_note(audio, path: Path) -> str:
    """Describe a reference clip the way the speech engine will judge it.

    Delegates the RULE to core.audio.reference_problem so the settings app and
    the bubble cannot disagree: a clip the GUI calls fine but the engine
    refuses is a mute bubble with a green tick next to it. What is described
    here is only the wording.
    """
    problem = audio.reference_problem(path)
    if problem:
        return f"⚠ {problem}"
    seconds = audio.reference_clip_seconds(path)
    if seconds is None:
        return (f"reference {path.name} — duration unchecked "
                f"(wav, flac, mp3 and m4a clips over "
                f"{audio.MIN_REFERENCE_S:.0f}s are accepted)")
    return f"reference {path.name} ({seconds:.1f}s)"


def paint_design_glyph(p, design: str, cx: float, cy: float, r: float,
                       color: QColor, t: float, k: float) -> None:
    """Mini silhouette of one design, in one colour.

    Lives at module level because TWO widgets draw it — the Appearance
    preview strip and the Look tiles — and a tile that drew its own copy
    could show a shape or a palette the strip (and the bubble) would not.
    """
    import math as _math
    if design == "halo":
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, max(2.0, r * 0.28)))
        p.drawEllipse(QPointF(cx, cy), r * 0.86, r * 0.86)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy), r * 0.12, r * 0.12)
    elif design == "reactor":
        for j, (rr, spd, span) in enumerate(((0.95, 0.5, 1.8), (0.78, -0.4, 1.2), (0.62, 0.8, 2.4))):
            a0 = t * 2 * _math.pi * spd + j
            path = QPainterPath()
            for i in range(17):
                a = a0 - span / 2 + i * (span / 16)
                x, y = cx + _math.cos(a) * r * rr, cy - _math.sin(a) * r * rr
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(color, 2.2, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy), r * 0.14, r * 0.14)
    elif design == "bloom":
        body = QRadialGradient(cx, cy, r)
        c = QColor(color)
        c.setAlpha(150)
        body.setColorAt(0.0, c)
        c.setAlpha(0)
        body.setColorAt(1.0, c)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(body))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(QBrush(QColor(color).lighter(160)))
        p.drawEllipse(QPointF(cx, cy), r * 0.22, r * 0.22)
    elif design == "droplet":
        drop = QPainterPath()
        for i in range(37):
            ang = i * 2 * _math.pi / 36
            tip = _math.exp(-((ang - _math.pi / 2) / 0.55) ** 2)
            rr = r * 0.85 * (1.0 + 0.42 * tip)
            x, y = cx + rr * _math.cos(ang) * 0.92, cy - rr * _math.sin(ang)
            drop.moveTo(x, y) if i == 0 else drop.lineTo(x, y)
        drop.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawPath(drop)
    elif design == "cube":
        rot = t * 0.5
        pts = [(cx + r * 0.9 * _math.cos(rot + i * _math.pi / 3),
                cy - r * 0.9 * _math.sin(rot + i * _math.pi / 3)) for i in range(6)]
        p.setPen(QPen(QColor(color).lighter(140), 1.6))
        p.setBrush(QBrush(QColor(color).darker(130)))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
        p.setPen(QPen(QColor(255, 255, 255, 90), 1.0))
        for x, y in pts:
            p.drawLine(QPointF(cx, cy), QPointF(x, y))
    elif design == "equalizer":
        p.setBrush(Qt.NoBrush)
        for i in range(14):
            a = i * 2 * _math.pi / 14
            ln = r * (0.15 + 0.5 * abs(_math.sin(t * 3 + i * 1.1)))
            p.setPen(QPen(color, 2.4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx + _math.cos(a) * r * 0.35, cy - _math.sin(a) * r * 0.35),
                       QPointF(cx + _math.cos(a) * (r * 0.35 + ln), cy - _math.sin(a) * (r * 0.35 + ln)))
    elif design == "crystal":
        pts = [(cx + r * 0.9 * _math.cos(t * 0.4 + i * _math.pi / 3),
                cy - r * 0.9 * _math.sin(t * 0.4 + i * _math.pi / 3)) for i in range(6)]
        p.setPen(QPen(QColor(color).lighter(140), 1.6))
        p.setBrush(QBrush(QColor(color).darker(150)))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
    elif design == "saturn":
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy), r * 0.5, r * 0.5)
        p.save()
        p.translate(cx, cy)
        p.rotate(-20)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(color).lighter(130), 2.0))
        p.drawEllipse(QPointF(0, 0), r * 0.95, r * 0.32)
        p.restore()
    elif design == "void":
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(5, 5, 8)))
        p.drawEllipse(QPointF(cx, cy), r * 0.85, r * 0.85)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(color).lighter(170), 1.8))
        p.drawEllipse(QPointF(cx, cy), r * 0.85, r * 0.85)
    elif design == "sauron":
        # A slit-pupilled eye wreathed in flame: the mascot designs keep their
        # own palette in the bubble, so the preview shows the same thing (state
        # colour on the corona, not on the eye). The corona is drawn explicitly
        # because without it the state colour measured **4 px** on this glyph —
        # the look's own idle colour was invisible on its tile, the same defect
        # the bubble-side guard found on `void` (8 px). Fire stays fire; the
        # colour rides the corona.
        corona = QColor(color)
        corona.setAlpha(150)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(corona))
        p.drawEllipse(QPointF(cx, cy), r * 0.95, r * 0.52)
        for sign in (-1.0, 1.0):
            flame = QPainterPath()
            flame.moveTo(cx, cy - r * 0.30)
            flame.quadTo(cx + sign * r * 1.10, cy - r * 0.55,
                         cx + sign * r * 0.95, cy + r * 0.30)
            flame.quadTo(cx + sign * r * 0.60, cy + r * 0.20, cx, cy + r * 0.30)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(255, 106, 24, 190)))
            p.drawPath(flame)
        p.setBrush(QBrush(QColor(color).lighter(150)))
        p.drawEllipse(QPointF(cx, cy), r * 0.72, r * 0.30)
        p.setBrush(QBrush(QColor(255, 214, 120)))
        p.drawEllipse(QPointF(cx, cy), r * 0.30, r * 0.22)
        p.setBrush(QBrush(QColor(18, 10, 4)))
        p.drawEllipse(QPointF(cx, cy), r * 0.055, r * 0.20)
    elif design == "pikachu":
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(color).lighter(120)))
        p.drawEllipse(QPointF(cx, cy + r * 0.08), r * 0.74, r * 0.66)
        for sign in (-1.0, 1.0):
            ear = QPainterPath()
            ear.moveTo(cx + sign * r * 0.34, cy - r * 0.16)
            ear.lineTo(cx + sign * r * 0.62, cy - r * 0.92)
            ear.lineTo(cx + sign * r * 0.12, cy - r * 0.44)
            ear.closeSubpath()
            eg = QLinearGradient(QPointF(cx + sign * r * 0.30, cy - r * 0.30),
                                 QPointF(cx + sign * r * 0.62, cy - r * 0.92))
            eg.setColorAt(0.0, QColor(250, 205, 42))
            eg.setColorAt(0.55, QColor(250, 205, 42))
            eg.setColorAt(0.60, QColor(24, 20, 12))
            eg.setColorAt(1.0, QColor(12, 10, 8))
            p.setBrush(QBrush(eg))
            p.drawPath(ear)
        p.setBrush(QBrush(QColor(236, 60, 46)))
        for sign in (-1.0, 1.0):
            p.drawEllipse(QPointF(cx + sign * r * 0.42, cy + r * 0.34),
                          r * 0.15, r * 0.13)
        p.setBrush(QBrush(QColor(26, 20, 14)))
        for sign in (-1.0, 1.0):
            p.drawEllipse(QPointF(cx + sign * r * 0.24, cy - r * 0.06),
                          r * 0.10, r * 0.11)
    elif design == "image":
        # The empty slot: a dashed ring with a diagonal mark. The real picture
        # is drawn by the preview itself (it has to be decoded and tinted, and
        # the Look tiles have no picture to show), so this glyph is the SHAPE of
        # the design — a slot a picture goes in — and deliberately not the orb,
        # which is what an unknown design name falls through to.
        ring = QColor(color).lighter(150)
        pen = QPen(ring, max(1.6, r * 0.13))
        pen.setCapStyle(Qt.FlatCap)
        pen.setDashPattern([3.0, 1.6])
        p.setBrush(Qt.NoBrush)
        p.setPen(pen)
        p.drawEllipse(QPointF(cx, cy), r * 0.86, r * 0.86)
        mark = QColor(color).lighter(190)
        p.setPen(QPen(mark, max(1.2, r * 0.075), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx - r * 0.30, cy + r * 0.30),
                   QPointF(cx + r * 0.30, cy - r * 0.30))
    elif design == "cat":
        # Ears first, then the head over their bases, then the tail behind:
        # the same layering the bubble uses, so the preview cannot show a
        # shape the desktop would not.
        p.setPen(Qt.NoPen)
        for sign in (-1.0, 1.0):
            ear = QPainterPath()
            ear.moveTo(cx + sign * r * 0.20, cy - r * 0.44)
            ear.lineTo(cx + sign * r * 0.86, cy - r * 0.96)
            ear.lineTo(cx + sign * r * 0.64, cy - r * 0.30)
            ear.closeSubpath()
            p.setBrush(QBrush(QColor(color).darker(135)))
            p.setPen(QPen(QColor(color).lighter(150), 1.2))
            p.drawPath(ear)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(color).lighter(125)))
        p.drawEllipse(QPointF(cx, cy + r * 0.10), r * 0.76, r * 0.68)
        p.setBrush(QBrush(QColor(246, 242, 238, 232)))
        p.drawEllipse(QPointF(cx, cy + r * 0.32), r * 0.20, r * 0.13)
        p.setBrush(QBrush(QColor(26, 22, 28)))
        for sign in (-1.0, 1.0):
            p.drawEllipse(QPointF(cx + sign * r * 0.28, cy - r * 0.04),
                          r * 0.13, r * 0.16)
    else:  # orb
        body = QRadialGradient(cx, cy - r * 0.25, r * 1.15)
        body.setColorAt(0.0, QColor(color).lighter(140))
        body.setColorAt(1.0, QColor(color).darker(160))
        p.setBrush(QBrush(body))
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.drawEllipse(QPointF(cx, cy), r, r)


class LookTile(QPushButton):
    """One look, drawn: its shape, its palette, and whether it is current.

    A tile is still a QPushButton — keyboard, focus, tooltips and `isChecked()`
    behave exactly as they did when a look was a text button — but its face is
    painted, so the choice reads as a LOOK rather than as a label. The glyph
    comes from `paint_design_glyph`, the same painter the Appearance preview
    strip uses, so a tile cannot advertise a shape the bubble would not draw;
    the three dots under it are the look's other state colours, and the ring
    marks the current one.

    Animation is driven by a clock the group owns and one shared timer ticks:
    eight tiles must not mean eight 25 Hz timers in the settings process.
    """

    W, H = 76, 62
    TICK_MS = 40
    GLYPH_Y = 22.0
    GLYPH_R = 15.0
    DOTS_Y = GLYPH_Y + 20.0

    def __init__(self, entry: dict, clock: QElapsedTimer, parent=None) -> None:
        super().__init__(parent)
        self.entry = entry
        self._clock = clock
        self._design = str(entry["design"])
        self._colors = {k: QColor(v) for k, v in entry["colors"].items()}
        self.setCheckable(True)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _e) -> None:          # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        try:
            role = QPalette.ColorRole
            checked = self.isChecked()
            rect = QRectF(1.5, 1.5, self.width() - 3.0, self.height() - 3.0)
            if checked:
                p.setBrush(QBrush(self.palette().color(role.Highlight)))
                p.setPen(QPen(self.palette().color(role.HighlightedText), 2.0))
            else:
                p.setBrush(QBrush(self.palette().color(role.Button)))
                p.setPen(QPen(self.palette().color(role.Mid), 1.0))
            p.drawRoundedRect(rect, 6, 6)

            t = self._clock.elapsed() / 1000.0
            # The state list is the schema's, so a fifth state is painted here
            # instead of being missed by a hand-written three-name tuple: the
            # first wears the glyph, the rest are the dots below it.
            _states = _state_colour_keys()
            _lead = _states[0] if _states else "idle"
            paint_design_glyph(p, self._design, self.width() / 2.0,
                               self.GLYPH_Y, self.GLYPH_R,
                               self._colors[_lead], t, 1.0)

            # the rest of the palette, so the tile shows the whole look: one
            # dot per other state, under the glyph
            _dots = [k for k in _states[1:]]
            x = self.width() / 2.0 - 6.0 * (len(_dots) - 1)
            p.setPen(Qt.NoPen)
            for key in _dots:
                if key in self._colors:
                    p.setBrush(QBrush(self._colors[key]))
                    p.drawEllipse(QPointF(x, self.DOTS_Y), 3.0, 3.0)
                x += 12.0

            text = (self.palette().color(role.HighlightedText) if checked
                    else self.palette().color(role.ButtonText))
            p.setPen(QPen(text))
            f = QFont()
            f.setPointSize(7)
            p.setFont(f)
            p.drawText(2, self.height() - 16, self.width() - 4, 14,
                       Qt.AlignHCenter, str(self.entry["label"]))
        finally:
            p.end()


class BubblePreview(QWidget):
    """Four animated glyphs previewing the state colours, size and design."""

    def __init__(self, colors_fn, size_fn, design_fn=None,
                 energy_fn=None, accent_fn=None, image_fn=None,
                 deco_fn=None) -> None:
        super().__init__()
        self._colors_fn = colors_fn
        self._size_fn = size_fn
        self._design_fn = design_fn or (lambda: "orb")
        self._energy_fn = energy_fn or (lambda: 1.0)
        self._accent_fn = accent_fn or (lambda: 0.5)
        # The Decoration card's two choices as the FORM holds them: the ring's
        # name and the value its colour row would write. A callable rather than
        # a saved setting, because the strip shows what is on screen in the
        # window, not what is on disk.
        self._deco_fn = deco_fn or (lambda: ("off", "state"))
        # The `image` design's art, as a `state -> path` resolver, and the cache
        # for its decoded layers: the strip repaints at 30 Hz and this is a
        # user's 4000x4000 photo.
        self._image_fn = image_fn or (lambda _state="": "")
        self._image_cache: dict = {}
        self._bubble_module = None
        self.setMinimumHeight(150)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self._timer.start()

    def drop_cached_art(self) -> None:
        """Forget every decoded picture — a different pack is about to be drawn."""
        self._image_cache.clear()

    @staticmethod
    def _glyph(p, design: str, cx: float, cy: float, r: float,
               color: QColor, t: float, k: float) -> None:
        """Draw one design's silhouette (the shared module-level painter).

        Kept as a method because the strip's own paint and the offscreen tests
        already call it here; the drawing itself lives in
        `paint_design_glyph`, which the Look tiles use too.
        """
        paint_design_glyph(p, design, cx, cy, r, color, t, k)

    def _draw_image_glyph(self, p, cx: float, cy: float, r: float,
                          color: QColor, state: str = "") -> bool:
        """Draw the picture in effect for `state`; False when there is none.

        Takes the STATE because a pack names a different picture per state: the
        four slots must show the four pictures a pack ships, which is the whole
        point of one choice switching several together. The decode and the tint
        come from the bubble module itself (`image_layers` / `tinted_image`),
        so the panel cannot render a picture the desktop would render
        differently — and the fit rule is the same one the painter uses (the
        picture's CIRCUMSCRIBED circle, scaled down to the glyph radius). One
        decode per revision, because this runs from paintEvent.
        """
        value = self._image_fn(state)
        # An ANIMATED pack state is a spec dict: the strip shows its FIRST
        # frame (a decision aid, not a projector) through the same decode and
        # tint as a still, so what it shows is what the desktop will draw.
        if isinstance(value, dict):
            frames = value.get("frames") or []
            value = frames[0] if frames else ""
        path = str(value or "")
        if not path:
            return False
        bubble = self._bubble_module
        if bubble is None:
            try:
                bubble = self._bubble_module = _core_module("bubble")
            except Exception:
                log.debug("preview: bubble module unavailable", exc_info=True)
                return False
        try:
            st = Path(path).stat()
            key = (path, st.st_mtime_ns, st.st_size)
        except OSError:
            return False
        if key not in self._image_cache:
            # Keyed by the FILE, not by "the last one": a pack gives the strip
            # four different pictures, and a single slot would re-decode all
            # four on every painted frame at 30 Hz. A failed revision is
            # remembered as None so an unreadable file costs one attempt.
            layers = None
            try:
                layers = bubble.image_layers(path)
            except Exception:
                log.debug("preview: image decode failed", exc_info=True)
            while len(self._image_cache) >= 8:
                self._image_cache.pop(next(iter(self._image_cache)))
            self._image_cache[key] = layers
        layers = self._image_cache.get(key)
        if not layers:
            return False
        img = layers[0]
        try:
            picture = bubble.tinted_image(img, layers[1], color)
        except Exception:
            log.debug("preview: image tint failed", exc_info=True)
            return False
        half = 0.5 * math.hypot(float(img.width()), float(img.height()))
        if half <= 0.0:
            return False
        scale = r * 0.96 / half
        w, h = float(img.width()) * scale, float(img.height()) * scale
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(QRectF(cx - w / 2.0, cy - h / 2.0, w, h), picture)
        p.restore()
        return True

    def _bubble(self):
        """The bubble module, loaded on first use (None if it cannot load)."""
        if self._bubble_module is None:
            try:
                self._bubble_module = _core_module("bubble")
            except Exception:
                log.debug("preview: bubble module unavailable", exc_info=True)
        return self._bubble_module

    def _draw_deco_glyph(self, p, design, cx, cy, r, color, t, energy) -> None:
        """The ring the Decoration card picks, drawn in the strip.

        The card exists in this window for one reason — to choose what the
        avatar wears — and until now the strip never drew a ring at all, so
        every choice it offered (and the colour row under it) had NO visible
        effect where the user was looking. That is the same "I changed it and
        nothing happened" defect one layer up from the settings keys.

        The geometry mirrors the bubble's own and says so: the band runs from
        the picture's fit (x1.03) to the aperture (x0.94), and the picture's fit
        is 0.76 of the aperture while a ring is on — the constant `_paint_image`
        uses — so the strip cannot show a band the bubble would not draw. The
        colour comes from the bubble's own resolver with the form's value, so
        the preview is not a second implementation of the choice.
        """
        if design != "image" or r <= 1.0:
            return
        name, value = self._deco_fn()
        if not name or name == "off":
            return
        bubble = self._bubble()
        if bubble is None:
            return
        try:
            fit_k = 0.76                 # `_paint_image`'s ring-on fit
            deco = bubble.avatar_deco_colour(color, t, 0.0, energy, value)
            bubble.BubbleWidget._draw_avatar_deco(
                p, cx, cy, r * 1.03, (r / fit_k) * 0.94, name, t, 0.0,
                deco, 1.0, energy)
        except Exception:
            log.debug("preview: decoration draw failed", exc_info=True)

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
        # the same two knobs the bubble reads: energy drives the pulse depth,
        # accent drives the glow punch — the preview must not lie about them
        e = max(0.2, min(2.0, float(self._energy_fn())))
        accent = max(0.0, min(1.0, float(self._accent_fn())))
        for i, (name, color) in enumerate(colors.items()):
            cx = slots[i]
            tt = t + i * 0.9
            if name == "idle":
                r = orb_r + 2.0 * k * e * (1 + math.sin(2 * math.pi * tt / 3.8)) / 2
            elif name == "listening":
                r = orb_r + (4 + 5 * abs(math.sin(2 * math.pi * tt / 0.9))) * k * e
            elif name == "thinking":
                r = orb_r + 2.5 * k * e * math.sin(3 * tt + 1.3)
            else:
                r = orb_r + 4.0 * k * e * (0.5 - 0.5 * math.cos(2 * math.pi * tt / 0.6))
            glow = QColor(color)
            glow.setAlpha(int(max(0, min(255, 50 * (0.55 + 0.9 * accent)))))
            grad = QRadialGradient(cx, cy, r + 6 * k)
            grad.setColorAt(0.0, glow)
            glow.setAlpha(0)
            grad.setColorAt(1.0, glow)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawEllipse(QPointF(cx, cy), r + 6 * k, r + 6 * k)
            design = self._design_fn()
            # The ring goes UNDER the picture, exactly as the bubble paints it
            # (decoration, then stage, then art, then rim) — a light behind a
            # person is the whole reading of the decoration.
            self._draw_deco_glyph(p, design, cx, cy, r, QColor(color), tt, e)
            # The `image` design shows the user's OWN picture here — the one
            # for THIS slot's state, because a pack names a picture per state.
            # A preview that showed the empty slot while the bubble drew the
            # photo would be lying about the one thing the combo above it
            # selects, so the picture goes through the bubble module's own
            # decode and tint (see _draw_image_glyph) and the slot glyph is
            # only the no-file case.
            if design != "image" or not self._draw_image_glyph(
                    p, cx, cy, r, QColor(color), name):
                self._glyph(p, design, cx, cy, r, QColor(color), tt, k)
            p.setPen(QPen(QColor(140, 140, 140)))
            f = QFont()
            f.setPointSize(8)
            p.setFont(f)
            p.drawText(int(cx - 40), int(cy + orb_r + 6 * k + 16), 80, 14,
                       Qt.AlignHCenter, name)
        p.end()

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
            st = None
            try:
                st, rate = H._open_input(device, H.SAMPLE_RATE, self.FRAME, cb)
                st.start()
            except Exception as e:
                # A stream that OPENED but failed to start was dropped here
                # without a close: PortAudio holds the device until the object
                # is collected, so a flapping device leaked one stream per
                # retry. Close it before backing off.
                if st is not None:
                    try:
                        st.close()
                    except Exception:
                        pass
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
            audio = _core_module("audio")._resample_to_16k(audio, rate)
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


def _control_request_bytes(command: str) -> bytes:
    """One control-socket request, carrying the capability token when needed.

    The bubble accepts the read-only verbs without a token and requires one for
    every verb that changes state, because a same-UID process passes the uid
    check. `level` is polled about twenty times a second, so the token file is
    read only for a verb that actually needs it. Falls back to the bare command
    when the bubble module or the token cannot be read — the refusal the server
    sends then explains itself, which is one place instead of two.
    """
    try:
        verb = command.split(" ", 1)[0].strip().lower()
        if verb in H.PTT_READ_ONLY:
            return command.encode("utf-8")
        token = H.CONTROL_TOKEN.read_text(encoding="utf-8").strip()
        if not token:
            return command.encode("utf-8")
        return f"{H._CONTROL_TOKEN_PREFIX}{token}\n{command}".encode("utf-8")
    except Exception:      # noqa: BLE001 - never take the window down for this
        return command.encode("utf-8")


def _socket_command(sock_path, command: str, timeout: float = 1.5) -> "str | None":
    """One request/response round-trip with the running bubble's control
    socket. Returns the raw reply text, or None when the bubble isn't running,
    the socket is stale, or the exchange fails — never raises, because every
    caller is a status readout that must not take the window down with it."""
    if sock_path is None:
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(sock_path))
        s.sendall(_control_request_bytes(command))
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            part = s.recv(65536)
            if not part:
                break
            buf += part
        s.close()
        return buf.decode("utf-8", "replace")
    except Exception:
        return None


def _json_command(sock_path, command: str, timeout: float) -> "dict | None":
    """`command` answered as a JSON object, or None (bad/absent reply)."""
    text = _socket_command(sock_path, command, timeout)
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def _health_query(sock_path, timeout: float = 1.5) -> "dict | None":
    """Ask the running bubble for its JSON health snapshot over the control
    socket. Returns the parsed dict, or None when the bubble isn't running,
    the socket is stale or the answer isn't JSON (never raises)."""
    return _json_command(sock_path, "health", timeout)


def _level_query(sock_path, timeout: float = 0.5) -> "dict | None":
    """Ask the running bubble for its live voice level (the `level` command).

    This is the same signal the bubble's designs paint from: `raw` is the last
    value the audio pipeline emitted and `ui` is the smoothed value that
    reaches `_frame()["level"]` in every painter. Mirrors _health_query.
    """
    return _json_command(sock_path, "level", timeout)


# who last published a level, in words the Voice tab can show
_LEVEL_SOURCES = {"mic": "mic (hands-free)", "ptt": "mic (push-to-talk)",
                  "tts": "the bubble's own voice", "none": "nothing yet"}


class _LevelFeed:
    """Polls the bubble's `level` command on a daemon thread.

    The window must never wait on a socket on the GUI thread — and a busy
    bubble makes that concrete: `doctor`/`health` hold the control server's
    accept loop for seconds, so a synchronous poll would freeze the meter and
    the whole window with it. The polling lives here; widgets only read the
    latest reading. `sock_path_fn` is a callable so the bubble module stays
    lazily loaded (see _LazyHandsoff) and tests can point it elsewhere.
    """

    def __init__(self, sock_path_fn, interval: float = 0.05) -> None:
        self._sock_path_fn = sock_path_fn
        self._interval = interval
        self._latest: "dict | None" = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="level-feed",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def latest(self) -> "dict | None":
        """Most recent snapshot, or None when the bubble never answered."""
        with self._lock:
            return dict(self._latest) if self._latest else None

    def _run(self) -> None:
        while not self._stop.is_set():
            doc = _level_query(self._sock_path_fn(), timeout=0.4)
            with self._lock:
                self._latest = doc
            self._stop.wait(self._interval)


class LevelMeter(QWidget):
    """Live voice-level bar for the Voice tab.

    It reads the SAME value the bubble's designs paint from, so it diagnoses
    the feed rather than the microphone hardware (that is the separate "Live
    test" button above it): `raw` is what the audio pipeline last emitted —
    the mic while listening, the bubble's own voice while it speaks — and
    `designs` is the smoothed value the bubble is animating with. Both are
    shown because the interesting failure is exactly the gap between them.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(34)
        self.setMinimumWidth(220)
        self.setToolTip(
            "Live level from the running bubble over its control socket — the "
            "same signal every bubble design paints from. The bubble emits a "
            "level only while it is listening or speaking, so a flat meter in "
            "a quiet room is expected.")
        self._doc: "dict | None" = None
        self._raw = 0.0
        self._ui = 0.0
        self._peak = 0.0

    def set_reading(self, doc: "dict | None") -> None:
        """Feed one `level` snapshot in (None = the bubble did not answer)."""
        self._doc = doc
        if doc:
            self._raw = max(0.0, min(1.0, float(doc.get("raw") or 0.0)))
            self._ui = max(0.0, min(1.0, float(doc.get("ui") or 0.0)))
            # peak hold with a decay, so a brief loud moment stays visible
            self._peak = max(self._raw, self._peak * 0.90)
        else:
            self._raw = self._ui = self._peak = 0.0
        self.update()

    def text(self) -> str:
        """One plain-text line. The readout label shows exactly this, so the
        reading is assertable without touching pixels."""
        doc = self._doc
        if not doc:
            return ("no reply — the bubble is not running, or busy answering "
                    "doctor/health")
        src = _LEVEL_SOURCES.get(str(doc.get("source")), str(doc.get("source")))
        age = doc.get("age_s")
        age_txt = "never" if age is None else f"{float(age):.1f} s ago"
        return (f"raw {self._raw:.2f}  ·  designs {self._ui:.2f}  ·  {src}  ·  "
                f"last above zero {age_txt}")

    def paintEvent(self, _event) -> None:            # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        try:
            pad = 3
            trough = QRectF(pad, pad, max(1.0, self.width() - 2 * pad),
                            max(1.0, self.height() - 2 * pad))
            p.setPen(Qt.NoPen)
            base = self.palette().color(self.foregroundRole())
            empty = QColor(base)
            empty.setAlpha(38)
            p.setBrush(QBrush(empty))
            p.drawRoundedRect(trough, 3, 3)
            if self._raw > 0.001:
                fill = QRectF(trough)
                fill.setWidth(max(2.0, trough.width() * self._raw))
                p.setBrush(QBrush(QColor(214, 88, 74) if self._raw >= 0.85
                                  else self.palette().color(
                                      QPalette.ColorRole.Highlight)))
                p.drawRoundedRect(fill, 3, 3)
            # a tick where the smoothed value the designs use has reached: it
            # lags the raw bar, and seeing them apart is the whole diagnosis
            if self._ui > 0.001:
                x = trough.left() + trough.width() * self._ui
                p.setBrush(QBrush(base))
                p.drawRect(int(x), int(trough.top()), 2, int(trough.height()))
            if self._peak > 0.02:
                px = trough.left() + trough.width() * self._peak
                p.setBrush(QBrush(QColor(base.red(), base.green(), base.blue(), 120)))
                p.drawRect(int(px) - 1, int(trough.top()), 2, int(trough.height()))
        finally:
            p.end()


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


# --------------------------------------------------------------------------- #
# Controls, built from the field table
# --------------------------------------------------------------------------- #
# A setting used to be spelled out in FOUR places — the schema row, the loader's
# coercion, the widget, and both halves of the load/save path — and the parts
# that are easy to forget drifted, each time as a setting a person could not
# change: a permission key with no checkbox that every save silently
# re-defaulted, and settings (`streaming_tts`, `confirm_seconds`, the two
# cooldowns, the whisper device) that had no control anywhere in the window at
# all. The row now carries how the value is PRESENTED, and this module builds
# the widget, its reader and its writer from it: adding a setting is one row in
# settings_schema.py.
class _Control:
    """One generated control: the widget, how to read it, how to load it.

    `widget` is the thing itself, so a key reads as `win.<key>` — the widget for
    a setting is named after the setting, in the window and in its tests.
    `row` is what a form puts on screen when the two differ (a slider travels
    with its own value label), and `labelled` says the widget already states
    what it is, so its form row needs no label of its own.
    """

    __slots__ = ("key", "widget", "read", "write", "row", "title", "labelled")

    def __init__(self, key, widget, read, write, row=None, title="",
                 labelled=False):
        self.key = key
        self.widget = widget
        self.read = read
        self.write = write
        self.row = row if row is not None else widget
        self.title = title
        self.labelled = labelled


def _slider_text(value: float, field) -> str:
    """The text beside a slider: the value, its unit, no invented precision.

    `show_scale` is for the row whose setting and whose wording count
    differently — the accent is stored as a fraction and read as a percentage —
    so the label says the number a person thinks in without moving the value.
    """
    shown = value * int(field.show_scale or 1)
    unit = field.unit
    if not unit:
        return f"{shown:.{field.decimals}f}"
    sep = " " if unit[0].isalnum() else ""
    return f"{shown:.{field.decimals}f}{sep}{unit}"


def _control_title(field: "object") -> str:
    """The label a generated row draws: the row's title, else its key in words.

    A blank label is a control nobody can identify, so the fallback is not
    cosmetic — and `title` is what the table is expected to declare, which the
    contract guard requires of every generated row.
    """
    return SCHEMA.field_title(field)


def _build_checkbox(win, field: "object") -> _Control:
    box = QCheckBox(_control_title(field), win)
    box.setToolTip(field.tip)
    box.toggled.connect(lambda *_: win._control_changed(field.key))
    return _Control(field.key, box, box.isChecked,
                    lambda value: box.setChecked(bool(value)),
                    title=_control_title(field), labelled=True)


def _build_spin(win, field: "object") -> _Control:
    spin = QSpinBox(win)
    # A float row keeps integer steps: the browser UI never offered a fraction,
    # and ceil/floor of the bounds is what stops `hardware_disk_gb` offering a
    # "warn when free disk drops below 0 GiB" row (its bound is 0.5).
    spin.setRange(int(math.ceil(field.lo if field.lo is not None else 0)),
                  int(math.floor(field.hi if field.hi is not None else 100)))
    if field.step:
        spin.setSingleStep(int(field.step))
    if field.suffix:
        spin.setSuffix(field.suffix)
    if field.zero:
        spin.setSpecialValueText(field.zero)
    if field.tip:
        spin.setToolTip(field.tip)
    spin.valueChanged.connect(lambda *_: win._control_changed(field.key))
    as_float = field.kind == "float"
    return _Control(field.key, spin,
                    (lambda: float(spin.value())) if as_float else spin.value,
                    lambda value: spin.setValue(int(float(value or 0))),
                    title=_control_title(field))


def _build_slider(win, field: "object") -> _Control:
    """A slider, with its value spelled out beside it.

    The widget counts in integers and the setting does not, so `scale` is the
    multiplier between them (0.5–2.0 becomes 50–200). Whatever the row calls the
    value — px, ×, % — is what the label says.
    """
    scale = int(field.scale or 1)
    row = QWidget(win)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    slider = QSlider(Qt.Horizontal, row)
    slider.setRange(int(round((field.lo if field.lo is not None else 0) * scale)),
                    int(round((field.hi if field.hi is not None else 1) * scale)))
    if field.tip:
        slider.setToolTip(field.tip)
    value_label = QLabel("", row)
    value_label.setMinimumWidth(64)
    lay.addWidget(slider, 1)
    lay.addWidget(value_label)
    as_int = field.kind == "int"

    def _value():
        raw = slider.value() / scale
        return int(round(raw)) if as_int else raw

    def _paint(*_args) -> None:
        value_label.setText(_slider_text(_value(), field))

    slider.valueChanged.connect(_paint)
    slider.valueChanged.connect(lambda *_: win._control_changed(field.key))

    def _set(value) -> None:
        lo = field.lo if field.lo is not None else 0
        hi = field.hi if field.hi is not None else 1
        try:
            held = min(hi, max(lo, float(value))) if value is not None else lo
        except (TypeError, ValueError):
            held = lo
        slider.setValue(int(round(held * scale)))
        _paint()

    return _Control(field.key, slider, _value, _set, row=row,
                    title=_control_title(field))


def _build_line(win, field: "object") -> _Control:
    edit = QLineEdit(win)
    if field.tip:
        edit.setToolTip(field.tip)
    if field.placeholder:
        # The example a person needs while the box is EMPTY, from the row that
        # knows what the value looks like — not an "if key ==" branch here.
        edit.setPlaceholderText(field.placeholder)
    edit.textChanged.connect(lambda *_: win._control_changed(field.key))

    def _read() -> str:
        text = edit.text().strip()
        return text.rstrip(field.rstrip) if field.rstrip else text

    return _Control(field.key, edit, _read,
                    lambda value: edit.setText(str(value or "")),
                    title=_control_title(field))


def _build_combo(win, field: "object") -> _Control:
    """A closed choice. What each one is CALLED comes from the row itself.

    A spelling belongs to the choice, so it lives beside the choice in the table
    (`choice_labels`) rather than in a second table here keyed by setting name;
    the value stored is the row's data either way, so a label can never change
    what is saved.
    """
    combo = QComboBox(win)
    spell = dict(getattr(field, "choice_labels", ()) or ())
    for choice in field.choices:
        combo.addItem(spell.get(choice) or str(choice), choice)
    if field.tip:
        combo.setToolTip(field.tip)
    combo.currentIndexChanged.connect(lambda *_: win._control_changed(field.key))

    def _write(value) -> None:
        index = combo.findData(value)
        # An unknown value leaves the row on its first entry rather than on a
        # blank: the loader would have replaced it with the default anyway, and
        # a combo with nothing selected reads as a setting nobody chose.
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _read() -> str:
        data = combo.currentData()
        return str(data if data is not None else (field.choices[0] if field.choices else ""))

    return _Control(field.key, combo, _read, _write, title=_control_title(field))


def _build_lines(win, field: "object") -> _Control:
    """One entry per line, in a box big enough to see a list in."""
    edit = QPlainTextEdit(win)
    # How tall the box wants to be is the row's business (`height`), because only
    # the row knows whether it holds two lines or a script.
    edit.setMinimumHeight(int(field.height or 72))
    if field.tip:
        edit.setToolTip(field.tip)
    if field.placeholder:
        edit.setPlaceholderText(field.placeholder)
    edit.textChanged.connect(lambda *_: win._control_changed(field.key))

    def _read() -> list:
        out = [line.strip() for line in edit.toPlainText().splitlines() if line.strip()]
        return out[:field.cap] if field.cap else out

    return _Control(field.key, edit, _read,
                    lambda value: edit.setPlainText(
                        "\n".join(str(x) for x in (value or []))),
                    title=_control_title(field))


def _build_commas(win, field: "object") -> _Control:
    """A short list on one line, comma separated."""
    edit = QLineEdit(win)
    if field.tip:
        edit.setToolTip(field.tip)
    edit.textChanged.connect(lambda *_: win._control_changed(field.key))

    def _read() -> list:
        out = []
        for item in edit.text().split(","):
            item = item.strip()
            if not item:
                continue
            out.append(item.lower() if field.lower else item)
        return out[:field.cap] if field.cap else out

    return _Control(field.key, edit, _read,
                    lambda value: edit.setText(
                        ", ".join(str(x) for x in (value or []))),
                    title=_control_title(field))


_CONTROL_BUILDERS = {
    "checkbox": _build_checkbox,
    "spin": _build_spin,
    "slider": _build_slider,
    "line": _build_line,
    "combo": _build_combo,
    "lines": _build_lines,
    "commas": _build_commas,
}


class SettingsWindow(QMainWindow):
    # EVERY value the Appearance tab owns; a change to any of them applies live.
    # `colors` and `bubble_size` belong here too: the live-apply only writes when
    # one of these actually differs from disk, so leaving them out meant a
    # colour-only edit was skipped as "this was a load, not an edit" and the
    # new colour never reached settings.json — the tab's "the colours don't
    # apply" complaint.
    # `design_image_path` belongs in this tuple for the same reason `colors`
    # does: a change that moves ONLY it would otherwise be read as a load rather
    # than an edit, and the picture the user just chose would never reach disk —
    # the defect that produced "the colours don't apply", one control along.
    # `design_pack` is in for exactly the same reason: picking a pack moves no
    # other control, and a pack that never reached disk would look like a picker
    # that does nothing.
    # Every key this tab can move. A key MISSING from here is not a cosmetic
    # omission: `_apply_appearance_live` compares these against the disk and
    # reads a difference it is not watching as "a load, not an edit", so the
    # control applies nothing and saves nothing — the exact "I changed it and
    # nothing happened" defect, one layer down from the widget. It has now
    # happened twice (`colors`, then `avatar_deco_color`), which is why the
    # guard for a new control asserts the LIVE APPLY and not merely that the
    # value reached `cfg`.
    # DERIVED from the settings contract (`settings_schema.SETTINGS_FIELDS`):
    # every row marked `live=True` is in this tuple, so the table and the live
    # apply cannot disagree — and the per-state picture keys are rows too, one
    # per state, rather than a join done here. The hand-written list this
    # replaces drifted twice (`colors`, then `avatar_deco_color`), each time as
    # the same complaint: "I changed it and nothing happened".
    APPEARANCE_KEYS = tuple(getattr(SCHEMA, "live_keys", lambda: ())())

    # Every setting this window DRAWS BY HAND — the rows whose control the table
    # cannot build (its `ctrl` is `custom`): the model list (filled from the
    # server), the microphone device list (from the audio server), the policy
    # rows (from the tool registry), the permissions grid, the alias editor, the
    # autostart switch, and the Appearance tab's shape/swatch/pack/picture rows.
    # DERIVED from the table, because the thing a guard has to catch is not "did
    # the window list its bespoke keys" — it cannot get that wrong if it asks —
    # but "was every one of them actually drawn", which is `_bespoke_drawn` held
    # against this set in the GUI guard. A custom row that nothing draws (no
    # `render` of its own, no `also` claiming it) fails the contract guard.
    BESPOKE_KEYS = SCHEMA.bespoke_keys()

    #: The SHAPES this window draws, by name (`settings_schema.RENDER_NAMES`).
    #: A row names one and the row is drawn in it; the value is the method that
    #: does it. The mapping IS the registry: a name with no method is a row
    #: nothing can draw, and a method no name reaches is dead code — both are
    #: contract-guard failures, so the two sets can only ever be one.
    RENDERERS = {
        "model-list": "_render_model_list",
        "device-list": "_render_device_list",
        "mic-test": "_render_mic_test",
        "clip-picker": "_render_clip_picker",
        "alias-editor": "_render_alias_editor",
        "permission-grid": "_render_permission_grid",
        "policy-rows": "_render_policy_rows",
        "design-picker": "_render_design_picker",
        "state-pictures": "_render_state_pictures",
        "fallback-image": "_render_fallback_image",
        "pack-picker": "_render_pack_picker",
        "deco-picker": "_render_deco_picker",
        "deco-colour": "_render_deco_colour",
        "colour-grid": "_render_colour_grid",
        "autostart": "_render_autostart",
    }

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("handsoff settings")
        # A window the SCREEN can hold. Every tab scrolls (see
        # `_scrolling_page`), so this is a preference and not the layout's
        # minimum any more — which is the point: the layout used to ASK for
        # 1679x2399 (the tallest tab page, stacked), a size no 1728-px-tall
        # display can give, so the compositor cut the window off at the screen
        # edge and everything below it — the health line and the whole
        # Save/Quit bar — was drawn past the bottom.
        self.setMinimumSize(560, 420)
        self.resize(*self._preferred_window_size())
        import copy
        self.cfg = copy.deepcopy(H.SETTINGS)
        # The `image` design's art, held as form values like every other
        # control, and seeded from settings so a reload shows the picture (or
        # pack) in use rather than the empty slot the form would otherwise
        # claim.
        self._design_image = str(self.cfg.get("design_image_path") or "")
        # One picture per state, held as form values like everything else. A
        # state missing from this map has no picture of its own and falls back.
        self._design_images = {
            state: str(self.cfg.get(key) or "")
            for state, key in STATE_IMAGE_KEYS}
        self._design_pack = str(self.cfg.get("design_pack") or "")
        # The decoration's own colour, held as a form value like the art. Seeded
        # from settings when the stored value IS a colour, so reloading shows
        # the colour in use; otherwise from the first state colour, so the first
        # click on the swatch opens on something related instead of black.
        self._deco_colour = self._stored_deco_colour()
        # A pack can be LOOKED AT before it is taken. `_preview_art` is the
        # candidate's manifest while the strip above is showing it, and
        # `_preview_scratch` is the temporary folder a pack FILE had to be
        # unpacked into for the strip to draw it (a folder needs no copy). This
        # panel is the only owner of that folder, so `_drop_pack_preview` — which
        # runs on a cancel, on Try it, on a second preview and on close — is the
        # only thing that has to remember it exists.
        self._preview_art: dict | None = None
        self._preview_source = ""
        self._preview_kind = ""
        self._preview_scratch = ""
        # ...and the BUBBLE draws it too, so a look can be judged on the real
        # desktop instead of only in the strip above. The bubble holds that in
        # memory with a short deadline and this HEARTBEAT is what keeps it
        # alive: the first beat starts the preview and each later one renews it,
        # so a window that dies without clearing leaves a bubble that stops
        # previewing on its own. `_preview_live_note` is what the label may say
        # about it, so the panel never claims the desktop shows something it does
        # not.
        self._preview_live_note = ""
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(3000)
        self._preview_timer.timeout.connect(self._renew_live_pack_preview)
        self._model_at_open = str(self.cfg.get("model") or "")
        self._state_dir_ready()
        self._live_probe: _LiveMicProbe | None = None   # live mic test (Voice tab)

        # The Appearance tab applies live: picking a shape (or moving a slider)
        # is a real save 400 ms later, so the bubble repaints without anyone
        # pressing Save. That is what the tab always claimed to do — the
        # separate Save button is for the rest of the form. The timer is
        # debounced so dragging a slider writes once, not per pixel.
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(400)
        self._live_timer.timeout.connect(self._apply_appearance_live)

        # ...and the model picker applies on the same principle, with its own
        # timer so a click cannot be confused with an appearance edit.
        self._model_apply_timer = QTimer(self)
        self._model_apply_timer.setSingleShot(True)
        self._model_apply_timer.setInterval(400)
        self._model_apply_timer.timeout.connect(self._apply_model_live)
        self._model_list_syncing = False

        # Every page goes through `_scrolling_page`: ONE scrolling
        # implementation for all six tabs, so no page can decide how tall the
        # window has to be.
        # Every control this window builds from the table, by setting key, and
        # every key a bespoke panel draws by hand. The two sets together are
        # what a person can change in this window, which is a thing the contract
        # guard can compare against the table (see tests/test_settings_gui.py).
        self._controls: dict = {}
        self._bespoke_drawn: set = set()
        # Every row this window has PLACED, the cards it built, and what it
        # draws by hand: a row the page draws itself (a list from the running
        # system, a grid of swatches, a row two settings share), a card a panel
        # draws whole, and the extra content that belongs under a card. The
        # table is the default for all four, so a page says only where it
        # differs — and the guards read these sets against the table.
        self._drawn_keys: set = set()
        self._cards: set = set()
        self._group_panels: dict = {}
        self._group_tails: dict = {}
        # What each card's subtitle says, when it has one: page copy, kept with
        # the page rather than in the schema.
        tabs = QTabWidget(self)
        self.tabs = tabs
        tabs.addTab(self._scrolling_page(self._brain_tab()), "Brain")
        # the meter polls with its tab
        self._voice_page = self._scrolling_page(self._voice_tab())
        tabs.addTab(self._voice_page, "Voice")
        tabs.addTab(self._scrolling_page(self._permissions_tab()), "Permissions")
        tabs.addTab(self._scrolling_page(self._appearance_tab()), "Appearance")
        tabs.addTab(self._scrolling_page(self._startup_tab()), "Startup")
        self._history_page = self._scrolling_page(self._history_tab())
        tabs.addTab(self._history_page, "History")
        tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(tabs)

        bottom = QWidget(self)
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        # A long status line must not become the WINDOW's minimum width. A
        # QLabel's minimum is the width of its text, so one save message
        # ("Saved to /home/… Memory cleared for the new model…") made this
        # window ask for ~1680 px — and a compositor that gives it less cuts the
        # message and the buttons off the side. Wrapping keeps the sentence
        # whole without dictating the geometry.
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
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

        # Live voice-level meter (Voice tab). The socket polling happens on the
        # feed's own thread and only while that tab is on screen: polling the
        # bubble 20x/s while the user reads the Brain tab would be pure noise,
        # and a synchronous poll would freeze the window on a busy bubble.
        # `lambda: H.CONTROL_SOCK` keeps the bubble module lazily loaded (the
        # settings GUI deliberately execs it only when something needs it).
        self._level_feed = _LevelFeed(lambda: H.CONTROL_SOCK)
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(60)
        self._level_timer.timeout.connect(self._refresh_level)
        self._level_feed_active(self.tabs.currentWidget() is self._voice_page)

        ol.addWidget(bottom)
        self.setCentralWidget(outer)

        self.reload_from_disk()
        self.refresh_models()
        self.refresh_mics()
        self.refresh_tts()

        # if settings.json changes on disk (the bubble persists its hands-free
        # toggle, another window saves, …) reload instead of clobbering it on Save.
        # The reload is suppressed once the user edits anything (event filter
        # below): a bubble-side write (e.g. notification auto-mute) would
        # otherwise reset widgets mid-edit and silently lose the user's pick —
        # the "my bubble shape reverts" bug.
        self._user_edited = False
        self._disk_mtime = self._settings_mtime()
        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(2000)
        self._disk_timer.timeout.connect(self._check_disk_changes)
        self._disk_timer.start()
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        # Any interaction anywhere in the window (or its dialogs) marks the
        # form dirty; the disk-poll auto-reload then stands down until Save
        # or an explicit reload clears the flag.
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            self._user_edited = True
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- helpers

    def _state_dir_ready(self) -> None:
        try:
            H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _refresh_level(self) -> None:
        """Repaint the Voice meter from the feed's latest reading. Never
        blocks: the socket wait happened on the feed's own thread."""
        meter = getattr(self, "level_meter", None)
        if meter is None:
            return
        meter.set_reading(self._level_feed.latest())
        readout = getattr(self, "level_readout", None)
        if readout is not None:
            readout.setText(meter.text())

    def _level_feed_active(self, on: bool) -> None:
        """Start/stop the Voice meter's polling with its tab's visibility."""
        feed = getattr(self, "_level_feed", None)
        timer = getattr(self, "_level_timer", None)
        if feed is None or timer is None:
            return
        if on:
            feed.start()
            if not timer.isActive():
                timer.start()
            self._refresh_level()
        else:
            timer.stop()
            feed.stop()

    def _refresh_health(self) -> None:
        """Poll the running bubble's `health` command off the GUI thread and
        render one compact vitals line (mic, brain, tts) in the status bar."""
        def fetch():
            return _health_query(H.CONTROL_SOCK)

        def done(ok, result):
            self.health_label.setToolTip(_health_tooltip(result))
            self._render_model_in_use(result if ok else None)
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
        if (getattr(self, "_preview_art", None)
                or getattr(self, "_preview_scratch", "")):
            # A previewed pack is drawn on the BUBBLE, and a previewed FILE was
            # unpacked into a folder nothing else knows about. Closing the window
            # is the last chance to take it off the desktop and remove it; the
            # bubble's own deadline is the backstop if this never runs.
            self._drop_pack_preview()
        if self._live_probe is not None:
            self._live_probe.stop()
        if getattr(self, "_health_timer", None) is not None:
            self._health_timer.stop()
        if getattr(self, "_level_timer", None) is not None:
            self._level_timer.stop()
        if getattr(self, "_level_feed", None) is not None:
            self._level_feed.stop()
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

    def _preferred_window_size(self) -> tuple[int, int]:
        """An opening size the screen can actually give this window.

        Read from the screen rather than hard-coded: a request taller than the
        output is what put the Save bar off the bottom edge in the first place,
        and 780x600 was chosen when the window could not be laid out below
        2399 px anyway.
        """
        want_w, want_h = 900, 760
        try:
            screen = QApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                want_w = max(560, min(want_w, avail.width() - 60))
                want_h = max(420, min(want_h, avail.height() - 60))
        except Exception:            # no screen (offscreen platform): the defaults
            log.debug("could not read the screen geometry", exc_info=True)
        return want_w, want_h

    # ------------------------------------------------ controls from the table

    def _build_control(self, field) -> "_Control | None":
        """Build one row's control, or None when a bespoke panel owns the key.

        A row the table marks `custom` or `none` is skipped rather than guessed
        at, so "what the table declares" and "what this window draws" stay
        comparable — the contract guard compares them, instead of the two
drifting apart one forgotten key at a time.
        """
        name = SCHEMA.control_for(field)
        if name in ("custom", "none", ""):
            return None
        builder = _CONTROL_BUILDERS.get(name)
        if builder is None:
            # A row naming a control nothing implements is a setting with no
            # widget. Raising here would take the whole recovery tool down with
            # it (the window is what a person opens when the bubble is broken),
            # so it is named in the journal and skipped: the guard is what fails.
            log.warning("settings: no control named %r for %s", name, field.key)
            return None
        control = builder(self, field)
        setattr(self, field.key, control.widget)
        self._controls[field.key] = control
        return control

    def _control_for_key(self, key: str) -> "_Control | None":
        """The table's control for one key, built and registered (or None).

        For a page whose groups are laid out by hand: the ROW still comes from
        the table (so the widget, its type and its bounds are the schema's), and
        only the placement is the page's business.
        """
        field = SCHEMA.fields_by_key().get(key)
        if field is None:
            log.warning("settings: no table row for %s", key)
            return None
        return self._build_control(field)

    def _control_changed(self, key: str) -> None:
        """A generated control moved: re-apply live when its row lives there."""
        if key in self.APPEARANCE_KEYS:
            self._schedule_appearance_live()

    # ------------------------------------------- a page is its cards, in order

    def _draw_group(self, page: str, group: str, rows: "_Rows") -> None:
        """One card: the table's rows for it, in the table's own order.

        A row is drawn by what the ROW says: the shape it asks for, the
        paragraph it carries, the settings that travel with it. Nothing here
        knows a setting by name, which is the whole point — a new setting, or a
        whole new card, is one line in `settings_schema.py`.
        """
        self._cards.add((page, group))
        for field in _group_fields(page, group):
            if field.key in self._drawn_keys:
                continue
            self._draw_row(field, rows)
            self._drawn_keys.add(field.key)

    def _draw_row(self, field, rows: "_Rows") -> None:
        """Draw one table row: its shape, its control, and what it carries.

        Three things, in one place, all of them the row's own declaration: the
        SHAPE that draws it (`render`), the paragraph under it (`explain`), and
        the settings it brings with it (`also` — the switches whose limits belong
        to them, the four pictures that are one choice). A shape nothing
        implements is named in the journal and skipped rather than raised: this
        window is what a person opens when the bubble is already broken, and the
        contract guard is what fails.
        """
        name = SCHEMA.renderer_for(field)
        if name:
            method = getattr(self, type(self).RENDERERS.get(name, ""), None)
            if method is None:
                log.warning("settings: no %r renderer for %s", name, field.key)
            else:
                method(rows, field)
        else:
            control = self._control_for_key(field.key)
            if control is not None:
                rows.add(control)
        if SCHEMA.control_for(field) == "custom":
            # The table cannot build this control, so whoever drew the row drew
            # it by hand. Recorded HERE rather than in each renderer, so what the
            # window DECLARES and what it DREW cannot drift apart.
            self._bespoke_drawn.add(field.key)
        companions = [(key, label) for key, label in SCHEMA.also_pairs(field)
                      if key not in self._drawn_keys]
        if companions:
            holder = QWidget(self)
            line = QHBoxLayout(holder)
            line.setContentsMargins(0, 0, 0, 0)
            any_drawn = False
            for key, label in companions:
                held = self._row_control(key)
                if held is None:
                    continue          # the shape drew it (the state pictures)
                any_drawn = True
                if label:
                    line.addWidget(QLabel(label, self))
                line.addWidget(held.row)
            line.addStretch(1)
            if any_drawn:
                rows.labelled(field.also_label, holder)
        if field.explain:
            rows.widget(self._muted(field.explain, self))

    def _group_panel(self, page: str, group: str, layout) -> bool:
        """Draw a card that is a PANEL (no rows of its own), if this page has one.

        Returns True when it drew it, so a page can be a loop over the table's
        cards with the bespoke ones named once.
        """
        panel = self._group_panels.get((page, group))
        if panel is None:
            return False
        panel(layout)
        self._cards.add((page, group))
        return True

    def _group_tail(self, page: str, group: str, layout) -> None:
        """Extra content that belongs AFTER a card's rows (a note, a strip)."""
        tail = self._group_tails.get((page, group))
        if tail is not None:
            tail(layout)

    def _row_control(self, key: str):
        """The table's control for one row, marked placed (a companion row).

        A companion is drawn exactly once — with the row it belongs to — and if
        its own control is one the table cannot build, it is recorded as drawn by
        hand for the same reason a rendered row is (`_draw_row`). A companion the
        SHAPE draws itself (the three extra state pictures) returns None here:
        already drawn, still marked.
        """
        self._drawn_keys.add(key)
        field = SCHEMA.fields_by_key().get(key)
        if field is not None and SCHEMA.control_for(field) == "custom":
            self._bespoke_drawn.add(key)
        return self._control_for_key(key)

    def _scrolling_page(self, body: QWidget) -> QWidget:
        """Wrap a tab's body in a scroll area, so the window never asks the
        compositor for a display taller than the content.

        A tab page used to BE its body, and the tab widget's minimum height is
        the tallest page it holds (a stacked layout takes the max). With
        Permissions at ~2300 px that made the WINDOW's minimum 1679x2399 — a
        screen taller than this machine has. A compositor hands the window what
        the screen has while the client keeps its own minimum, so the bottom of
        the window falls off the edge: the health line and the Save / Save &
        restart / Quit bar, which is why picking a different Ollama model never
        reached settings.json, and why the lower half of a tab with no scroll
        area of its own could not be reached at all. Wrapping every body is the
        whole fix: each page's minimum collapses to the scroll area's, the
        window fits the screen, and the rest is scrolling like everything else.
        """
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        return page

    # ------------------------------------------------------------------- brain

    def _brain_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        # One row on this page is drawn here (a list filled from the server);
        # everything else is the table's, including the tool-call limit, which
        # this window never drew before this pass.
        for group in _page_groups("brain"):
            card = QGroupBox(_group_title("brain", group), w)
            form = QFormLayout(card)
            self._draw_group("brain", group, _Rows(self, form, "form"))
            lay.addWidget(card)
            self._group_tail("brain", group, lay)
        hint = QLabel(
            "Tool-capable models (badge “tools”) can run commands and edit their own code.\n"
            "Chat works with any model. Models are pulled with:  ollama pull <name>", w)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)
        return w

    def _render_model_list(self, rows: "_Rows", field) -> None:
        """The picker whose rows come from the SERVER, not from the schema.

        A list, the line that says which model is in use NOW, and the buttons. It
        draws those rows itself (there is no value to type), and the model's name
        is still stored under the row's own key, which is the table's part.
        """
        form = rows.target
        self.model_list = QListWidget(self)
        self.model_list.setMinimumHeight(180)
        # Picking a model IS the action, like picking a shape in Appearance: the
        # click applies and saves it a moment later, so there is no Save button
        # in the middle to miss. Its oldest complaint is "swapping models never
        # applies, the new model is never saved", and the honest reading of that
        # is a picker whose choice only takes effect if someone presses a button
        # somewhere else.
        self.model_list.currentItemChanged.connect(self._on_model_picked)
        form.addRow("Models (\U0001f527 tools = can control the desktop & self-modify)",
                    self.model_list)
        self.model_in_use = QLabel("In use now: \u2026", self)
        self.model_in_use.setWordWrap(True)
        form.addRow("", self.model_in_use)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self.refresh_models)
        self.test_btn = QPushButton("Test selected model", self)
        self.test_btn.clicked.connect(self.test_model)
        row.addWidget(refresh)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        form.addRow(row)

    def _selected_model(self) -> str:
        it = self.model_list.currentItem()
        return it.data(Qt.UserRole) if it else ""

    def _on_model_picked(self, *_args) -> None:
        """A model CLICK starts the apply; a list REBUILD never does.

        `refresh_models` sets the selection programmatically to put back the row
        the user already had, and `QListWidget.clear()` emits
        `currentItemChanged` naming some OTHER item on the way out (measured:
        clearing a list whose current row was 2 emitted the item at row 1).
        Without this guard a plain Refresh would apply a model nobody chose,
        which is the same defect as losing a pick — a rebuild read as a choice.
        """
        if self._model_list_syncing:
            return
        if not self._selected_model():
            return
        self._model_apply_timer.start()

    def _apply_model_live(self) -> None:
        """Save the picked model and tell the running bubble. No button.

        Skips the write when the file already holds this model, which is how a
        plain window load (where the list is restored to the stored model) is
        told apart from a real pick — the same "is this an edit?" rule the
        Appearance tab applies, for the same reason.
        """
        picked = self._selected_model()
        if not picked:
            return
        try:
            on_disk = merge_settings(
                json.loads(H.SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            on_disk = {}
        if on_disk.get("model") == picked:
            return                    # already the model on disk: not an edit
        switched = str(picked) != str(self._model_at_open)
        if not self._save_reported():
            return
        note = ("  The conversation was cleared for it (a backup is kept), "
                "because a new model must not inherit what the old one was "
                "tuned for." if switched else "")
        self._status(f"Applied model {picked} — no Save needed.{note}")
        self._refresh_health()        # and say what the bubble is on NOW

    def _render_model_in_use(self, health: "dict | None") -> None:
        """What the RUNNING bubble is actually on, right beside the picker.

        The vitals line at the bottom of the window says the same thing, but the
        question "did my pick apply?" is asked while looking at the list, so the
        answer has to be there. The bubble's own `model` is the only authority:
        `settings.json` says what was ASKED for, the bubble says what is IN USE,
        and a picker that cannot show the difference is how "the model never
        applies" stays invisible.
        """
        lab = getattr(self, "model_in_use", None)
        if lab is None:
            return
        live = str((health or {}).get("model") or "")
        picked = str(self._selected_model() or self.cfg.get("model") or "")
        if not live:
            lab.setText(f"In use now: the bubble is not answering (picked: "
                        f"{picked or 'nothing'})")
            lab.setStyleSheet("color: palette(mid);")
            return
        if picked and live != picked:
            lab.setText(f"In use now: {live} — {picked} is not applied yet.")
            lab.setStyleSheet("color: orange;")
            return
        lab.setText(f"In use now: {live}")
        lab.setStyleSheet("color: palette(mid);")

    def refresh_models(self) -> None:
        base = (self.ollama_host.text().strip() or DEFAULT_SETTINGS["ollama_host"]).rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "http://" + base
        # Read the selection BEFORE the list is emptied. `clear()` throws the
        # current row away, and the row IS the user's intent — the model they
        # mean, saved or not — so it has to be carried across the rebuild rather
        # than looked up afterwards, when it is already gone.
        keep = self._selected_model()
        # A rebuild, not a choice: everything below (and in `_fill_model_list`,
        # which clears the flag) is suppressed for the pick handler.
        self._model_list_syncing = True
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
            try:
                self._fill_model_list(base, ok, result, keep)
            finally:
                # Always, including the unreachable-server return: a flag left
                # set would make every later pick silently do nothing.
                self._model_list_syncing = False

        self.run_bg(fetch, done)

    def _fill_model_list(self, base: str, ok: bool, result, keep: str = "") -> None:
        """Draw a finished refresh, WITHOUT changing which model is in use.

        `keep` is the row that was under the cursor when the refresh started
        (the caller reads it before emptying the list) and it is put back when
        the server still has that model. The old code restored from
        `cfg["model"]` instead, so a refresh landing after a pick silently put
        the STORED model back under the cursor and the next Save wrote the model
        that was already there: "swapping Ollama models never applies, the new
        model is never saved".

        Restoring this way also sidesteps `QListWidget.clear()`, which emits
        `currentItemChanged` naming some OTHER item (measured: clearing a list
        whose current row was 2 emitted the item at row 1) — a rebuild is not a
        choice, and nothing listens for one.

        The stored model is the fallback for a list that had no selection of its
        own (the first refresh of a window), and nothing is auto-selected when
        neither is in the list: picking row 0 there made the next save quietly
        move off a model the user never touched.
        """
        self.model_list.clear()
        if not ok:
            bad = QListWidgetItem(f"cannot reach {base} — {result}")
            bad.setFlags(Qt.NoItemFlags)
            self.model_list.addItem(bad)
            return
        target = str(keep or "") or str(self.cfg.get("model") or "")
        for name, caps in result:
            badges = "   ·  " + "  ".join(caps) if caps else ""
            it = QListWidgetItem(name + badges)
            it.setData(Qt.UserRole, name)
            it.setToolTip(f"capabilities: {', '.join(caps) or 'none'}")
            self.model_list.addItem(it)
            if name == target:
                self.model_list.setCurrentItem(it)
        if (self.model_list.currentRow() < 0 and self.model_list.count()
                and not target):
            self.model_list.setCurrentRow(0)
        # The rebuild is over: from here a selection change is the user's.
        self._model_list_syncing = False

    def test_model(self) -> None:
        base = (self.ollama_host.text().strip() or DEFAULT_SETTINGS["ollama_host"]).rstrip("/")
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
        # Every row on this page is the table's, in the shape the row asks for:
        # the device list, the microphone test rig, the alias editor and the clip
        # picker are SHAPES, and the rows that carry a second setting under them
        # (the hardware limits, the alert thresholds) say so in their `also`. A
        # new setting here needs one line in settings_schema.py and nothing in
        # this file.
        self._group_panels[("voice", "level")] = self._level_panel
        self._group_tails[("voice", "stt")] = self._stt_note
        self._group_tails[("voice", "tts")] = self._voice_test_row
        # The page is its cards, in the table's order, and each card is the
        # table's rows — so a new setting on this page needs one line in
        # settings_schema.py and nothing here.
        for group in _page_groups("voice"):
            if self._group_panel("voice", group, lay):
                continue
            card = QGroupBox(_group_title("voice", group), w)
            card_lay = QVBoxLayout(card)
            form = QFormLayout()
            card_lay.addLayout(form)
            self._draw_group("voice", group, _Rows(self, form, "form"))
            self._group_tail("voice", group, form)
            lay.addWidget(card)
        lay.addStretch(1)
        return w

    def _render_device_list(self, rows: "_Rows", field) -> None:
        """The input device: a list from the audio server, plus a refresh."""
        row = QHBoxLayout()
        self.mic_combo = QComboBox(self)
        refresh = QPushButton("Refresh", self)
        refresh.clicked.connect(self.refresh_mics)
        row.addWidget(self.mic_combo, 1)
        row.addWidget(refresh)
        rows.target.addRow(field.title, row)
        # Reopening the live test is what makes a device switch observable, so
        # the handler is wired here, beside the widget it belongs to.
        self.mic_combo.currentIndexChanged.connect(
            lambda _i: self._mic_live_restart_if_on())

    def _render_mic_test(self, rows: "_Rows", field) -> None:
        """The noise gate, with both ways of testing the microphone under it.

        The row's OWN control (a spin box, from its table row) plus the rig: the
        one-off test, the live meter, the transcript. A shape that keeps the
        table's control places it itself, which is why this asks for it first.
        """
        control = self._control_for_key(field.key)
        if control is not None:
            rows.add(control)
        self.mic_threshold.valueChanged.connect(
            lambda _v: self._mic_live_restart_if_on())
        mic_test_row = QHBoxLayout()
        self.mic_test_btn = QPushButton("Test microphone (3 s)", self)
        self.mic_test_btn.clicked.connect(self.test_mic)
        self.mic_bar = QProgressBar(self)
        self.mic_bar.setRange(0, 100)
        mic_test_row.addWidget(self.mic_test_btn)
        mic_test_row.addWidget(self.mic_bar, 1)
        rows.target.addRow(mic_test_row)

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
        rows.target.addRow(mic_live_row)
        self.mic_live_event = QLabel("", self)
        self.mic_live_event.setStyleSheet("color: palette(mid);")
        rows.target.addRow(self.mic_live_event)
        self.mic_live_transcript = QLabel("", self)
        self.mic_live_transcript.setWordWrap(True)
        self.mic_live_transcript.setStyleSheet("font-weight: bold;")
        rows.target.addRow("Last transcript", self.mic_live_transcript)

    def _render_alias_editor(self, rows: "_Rows", field) -> None:
        """Workspace aliases: its own editor, one `name = workspace` per line."""
        self.alias_edit = QPlainTextEdit(self)
        self.alias_edit.setMaximumHeight(76)
        self.alias_edit.setPlaceholderText(
            "one per line:  name = workspace number\ne.g.  code = 2")
        aliases = self.cfg.get("workspace_aliases") or {}
        self.alias_edit.setPlainText(
            "\n".join(f"{k} = {v}" for k, v in sorted(aliases.items())))
        rows.target.addRow(self.alias_edit)

    def _render_clip_picker(self, rows: "_Rows", field) -> None:
        """The clip row, with the engine report above it and the status below."""
        self.tts_engine_label = QLabel("", self)
        self.tts_engine_label.setWordWrap(True)
        rows.target.addRow(self.tts_engine_label)

        ref_row = QHBoxLayout()
        ref_control = self._control_for_key(field.key)
        if ref_control is not None:
            ref_control.widget.setPlaceholderText("(built-in voice)")
            ref_row.addWidget(ref_control.row, 1)
        browse = QPushButton("Choose clip…", self)
        browse.clicked.connect(self._pick_reference)
        clear = QPushButton("Clear", self)
        clear.clicked.connect(self._clear_reference)
        ref_row.addWidget(browse)
        ref_row.addWidget(clear)
        rows.target.addRow(field.title, ref_row)

        self.tts_ref_status = QLabel("", self)
        self.tts_ref_status.setWordWrap(True)
        rows.target.addRow(self.tts_ref_status)

    def _stt_note(self, form: QFormLayout) -> None:
        """The whisper card's footnote, under its two table rows."""
        note = QLabel("Smaller = faster. The model downloads on the next bubble start.",
                      self)
        note.setWordWrap(True)
        form.addRow(note)

    def _voice_test_row(self, form: QFormLayout) -> None:
        """The TTS card's Test button, under the two sliders."""
        self.voice_test_btn = QPushButton("Test voice", self)
        self.voice_test_btn.setToolTip(
            "Speak a line through the running bubble. It plays the SAVED voice, "
            "rate and volume, so save first to hear a change.")
        self.voice_test_btn.clicked.connect(self.test_voice)
        form.addRow(self.voice_test_btn)

    def _level_panel(self, layout: QVBoxLayout) -> None:
        """The live-level card: what the BUBBLE is painting with, right now.

        "Live test" above proves the microphone itself works; this proves the
        bubble is publishing the level its designs paint from, which is a
        different failure and was previously invisible from here. No table row
        belongs here — the card has none, which is what makes it a panel.
        """
        group = QGroupBox(_group_title("voice", "level"), self)
        lvl = QVBoxLayout(group)
        self.level_meter = LevelMeter(group)
        lvl.addWidget(self.level_meter)
        self.level_readout = QLabel("waiting for the bubble\u2026", group)
        self.level_readout.setStyleSheet("color: palette(mid);")
        lvl.addWidget(self.level_readout)
        note = QLabel(
            "The exact signal the bubble's designs animate from, polled over "
            "its control socket. <b>Raw</b> is what the audio pipeline last "
            "emitted \u2014 your microphone while it listens, the bubble's own "
            "voice while it speaks; <b>designs</b> is the smoothed value it is "
            "painting with, so a raw bar that never moves the tick means the "
            "feed is arriving but not being shown. The bubble emits a level "
            "only while it is listening or speaking: a flat meter in a quiet "
            "room is normal.", group)
        note.setWordWrap(True)
        lvl.addWidget(note)
        layout.addWidget(group)

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

    def refresh_tts(self) -> None:
        """Report the engine, its weights and the chosen reference clip.

        Replaces the voice combo + download dialog: chatterbox-turbo has ONE
        built-in voice, so there is no catalog left to choose from and the only
        choice is whether to clone an optional clip. The two things that
        actually go wrong are a clip that is too short (the engine asserts
        longer than 5 s, so a 2 s clip raises on EVERY turn) and weights that
        were never downloaded. Both are reported here, while the user can still
        fix them, rather than at the first spoken reply.
        """
        try:
            engine = H.TTS_ENGINE
            weights_ok = H._audio.tts_weights_cached()
        except Exception as e:      # partial/older deployment: say so, don't crash
            self.tts_ref_status.setText(f"speech engine unavailable: {e}")
            return
        self.tts_engine_label.setText(
            f"Engine: {engine} — a neural voice that runs on the GPU. "
            "The built-in voice needs no separate download.")
        if not weights_ok:
            self.tts_ref_status.setText(
                f"⚠ {engine} weights are NOT downloaded — the bubble will be "
                "mute until install.sh fetches them.")
            return
        ref = self.tts_reference.text().strip()
        if not ref:
            self.tts_ref_status.setText(
                f"{engine} weights present · built-in voice")
            return
        self.tts_ref_status.setText(_reference_note(H._audio, Path(ref)))

    def _pick_reference(self) -> None:
        start = self.tts_reference.text().strip() or str(HOME)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a voice reference clip", start,
            "Audio (*.wav *.flac *.mp3 *.m4a *.ogg);;All files (*)")
        if path:
            self.tts_reference.setText(path)
        self.refresh_tts()

    def _clear_reference(self) -> None:
        self.tts_reference.setText("")
        self.refresh_tts()

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
                    thr = self.mic_threshold.value()
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
                                   self.mic_threshold.value())
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
                                   self.mic_threshold.value())

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
        """Preview the bubble's ACTUAL voice, over the control socket.

        The old version loaded its own piper voice in this process. That is
        impossible now and was wrong even then: chatterbox-turbo is ~2.7 GB of
        VRAM, so a second instance inside the settings app would fight the
        bubble for the GPU — and it would preview the GUI's idea of the voice
        (its own rate, volume and clip) rather than what the bubble will
        actually say. The bubble already owns the model, so ask it to speak.
        """
        self.voice_test_btn.setEnabled(False)
        self._status("asking the bubble to speak …")

        def worker():
            reply = _socket_command(H.CONTROL_SOCK, f"say {_VOICE_TEST_LINE}",
                                    timeout=10.0)
            if reply is None:
                raise RuntimeError(
                    f"the bubble is not running (no control socket at "
                    f"{H.CONTROL_SOCK}) — start it, then test again")
            if not reply.strip().startswith("ok"):
                raise RuntimeError(reply.strip())
            return reply.strip()

        def done(ok, result):
            self.voice_test_btn.setEnabled(True)
            self._status(str(result) if ok else f"voice test failed: {result}")

        self.run_bg(worker, done)

    # ------------------------------------------------------------------ memory

    # ------------------------------------------------------------------ history

    def _history_tab(self) -> QWidget:
        """Transparency tab: the three stores the bubble reasons from.

        - Conversation: the rolling history.json the model sees each turn.
        - Durable facts: memory.json facts extracted from conversation that
          survive trimming and restarts.
        - Decision log: decisions.jsonl, one line per tool-policy decision
          (ALLOW / DENY / CONFIRM / DRY-RUN) — the 'why did it do that'
          record."""
        w = QWidget(self)
        lay = QVBoxLayout(w)
        sub = QTabWidget(w)
        sub.addTab(self._conversation_pane(), "Conversation")
        sub.addTab(self._facts_pane(), "Durable facts")
        sub.addTab(self._decisions_pane(), "Decision log")
        lay.addWidget(sub, 1)
        self._refresh_history()
        self._refresh_facts()
        self._refresh_decisions()
        return w

    def _conversation_pane(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        info = QLabel(
            "Recent conversation history. New messages are added here; "
            "clearing it makes the assistant forget everything and start fresh.", w)
        info.setWordWrap(True)
        lay.addWidget(info)
        self.history_view = QPlainTextEdit(w)
        self.history_view.setReadOnly(True)
        lay.addWidget(self.history_view)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", w)
        refresh.clicked.connect(self._refresh_history)
        clear = QPushButton("Clear history", w)
        clear.clicked.connect(self._clear_history)
        row.addWidget(refresh)
        row.addWidget(clear, 1)
        lay.addLayout(row)
        return w

    def _facts_pane(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        info = QLabel(
            "Durable facts the assistant extracted from conversation (name, "
            "family, likes, home\u2026). They survive restarts and trimming. "
            "Forgetting one lets it be re-learned the next time you say it.", w)
        info.setWordWrap(True)
        lay.addWidget(info)
        self.facts_list = QListWidget(w)
        self.facts_list.setAlternatingRowColors(True)
        lay.addWidget(self.facts_list, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", w)
        refresh.clicked.connect(self._refresh_facts)
        forget = QPushButton("Forget selected fact", w)
        forget.clicked.connect(self._forget_fact)
        row.addWidget(refresh)
        row.addWidget(forget, 1)
        lay.addLayout(row)
        return w

    def _refresh_facts(self) -> None:
        """Re-render the facts pane from memory.json. Facts are stored as
        {k: replaceable key, v: sentence} — a new 'my name is X' replaces the
        old name fact — so the viewer shows the same deduped view the model
        sees, not raw file history."""
        try:
            data = json.loads(H.MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = []
        facts: dict[str, str] = {}
        if isinstance(data, list):
            for m in data:
                if (isinstance(m, dict) and str(m.get("k", ""))
                        and str(m.get("v", ""))):
                    facts[str(m["k"])] = str(m["v"])
        self.facts_list.clear()
        if not facts:
            empty = QListWidgetItem("(no durable facts yet)")
            empty.setFlags(Qt.NoItemFlags)
            self.facts_list.addItem(empty)
            return
        for k, v in facts.items():
            it = QListWidgetItem(f"{v}   [{k}]")
            it.setData(Qt.UserRole, v)
            self.facts_list.addItem(it)

    def _forget_fact(self) -> None:
        item = self.facts_list.currentItem()
        if item is None or item.data(Qt.UserRole) is None:
            return
        ok = QMessageBox.question(
            self, "Forget fact",
            f"Make the assistant forget:\n{item.text()}\n\n"
            "It is re-learned if you state it again.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            return
        fact = item.data(Qt.UserRole)
        try:
            data = json.loads(H.MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            self._status(f"cannot read memory.json: {e}")
            return
        kept = [m for m in data if isinstance(m, dict)
                and str(m.get("v", "")) != fact]
        try:
            from core.settings import atomic_private_write
            _backup_keep_n(H.MEMORY_FILE, "bak-facts")
            atomic_private_write(
                H.MEMORY_FILE, json.dumps(kept, ensure_ascii=False, indent=1))
        except OSError as e:
            self._status(f"cannot update memory.json: {e}")
            return
        self._status("fact forgotten. The running bubble keeps its in-memory "
                     "copy until it restarts.")
        self._refresh_facts()

    def _decisions_pane(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        info = QLabel(
            "One line per tool decision (ALLOW / DENY / CONFIRM / DRY-RUN), "
            "kept in decisions.jsonl — the 'why did it do that' record. "
            "Read-only here; the bubble prunes the file itself.", w)
        info.setWordWrap(True)
        lay.addWidget(info)
        self.decisions_view = QPlainTextEdit(w)
        self.decisions_view.setReadOnly(True)
        self.decisions_view.setMaximumBlockCount(20000)
        lay.addWidget(self.decisions_view, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh", w)
        refresh.clicked.connect(self._refresh_decisions)
        row.addWidget(refresh, 1)
        lay.addLayout(row)
        return w

    def _refresh_decisions(self) -> None:
        try:
            raw = H.DECISIONS_FILE.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            self.decisions_view.setPlainText(
                "(no decisions logged yet — the bubble writes one line per "
                "tool call once it runs)")
            return
        except OSError as e:
            self.decisions_view.setPlainText(f"(cannot read decision log: {e})")
            return
        out: list[str] = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            out.append(
                "{ts}  {dec:<8} {tool} \u2192 {target}   ({result})   #{id}".format(
                    ts=str(d.get("ts", "?")), dec=str(d.get("decision", "?")),
                    tool=str(d.get("tool", "?")), target=str(d.get("target", "")),
                    result=str(d.get("result", "")), id=str(d.get("id", ""))))
        if not out:
            self.decisions_view.setPlainText(
                "(decision log has no readable entries)")
            return
        self.decisions_view.setPlainText(
            f"{len(out)} decisions — newest last:\n\n" + "\n".join(out))

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
        note = self._wipe_history("bak-manual")
        if note.startswith("could not"):
            self._status(note.strip())
            return
        self.history_view.setPlainText(f"(history cleared — {note})")
        self._status(f"history cleared ({note}).")

    def _on_tab_changed(self, index: int) -> None:
        try:
            tabs = getattr(self, "tabs", None)
            if tabs is not None and tabs.widget(index) is getattr(
                    self, "_history_page", None):
                self._refresh_history()
                self._refresh_facts()
                self._refresh_decisions()
            # the Voice meter only polls the bubble while its tab is on screen
            self._level_feed_active(
                tabs is not None and tabs.widget(index) is getattr(
                    self, "_voice_page", None))
        except Exception:
            pass

    # ------------------------------------------------------------- permissions

    def _permissions_tab(self) -> QWidget:
        # Every row is the table's, in the card the table puts it in: the
        # permission grid and the per-tool policy rows are SHAPES
        # (`render="permission-grid"` / `"policy-rows"`), and the two rows that
        # needed configuring now say so themselves (the command box's height and
        # example, the search server's placeholder). So a new setting here needs
        # one line in settings_schema.py and nothing at all in this file.
        self._group_tails[("permissions", "extra")] = self._blocked_note
        w = QWidget(self)
        lay = QVBoxLayout(w)
        # The page is its cards, in the table's order, and each card is the
        # table's rows — including the two that used to be placed by hand into
        # a "tail" form of their own (dry-run and the confirmation window are
        # ordinary rows of the policy card, and the table puts them there).
        for group in _page_groups("permissions"):
            if self._group_panel("permissions", group, lay):
                continue
            card = QGroupBox(_group_title("permissions", group), w)
            form = QFormLayout(card)
            self._draw_group("permissions", group, _Rows(self, form, "form"))
            self._group_tail("permissions", group, form)
            lay.addWidget(card)
        lay.addStretch(1)
        return w

    def _render_permission_grid(self, rows: "_Rows", field) -> None:
        """The permission grid: one checkbox per permission the schema declares.

        The KEY SET comes from the schema, not from the dict below. A
        permission declared in `settings_schema.DEFAULT_SETTINGS["permissions"]`
        used to ship with no checkbox at all, and `_collect` then wrote a
        `permissions` dict WITHOUT that key — which the three-way merge resolves
        as a DELETE (the candidate is missing it, the disk still equals what was
        loaded), so `"get_datetime": false` was silently turned back on from the
        defaults by the next Save. Wording stays in `labels`; the fallback keeps
        a brand-new permission visible instead of invisible, and the guard
        asserts the two sets agree in both directions.

        A checkbox carries its own label, so each one is a bare row of the card.
        """
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
        for key in (DEFAULT_SETTINGS.get("permissions") or {}):
            title, desc = labels.get(key, (key.replace("_", " ").capitalize(),
                                           "no description yet"))
            chk = QCheckBox(f"{title} — {desc}", self)
            self.perm_checks[key] = chk
            rows.target.addRow(chk)

    def _blocked_note(self, form: QFormLayout) -> None:
        """What the whitelist can never include, under the box it applies to."""
        blocked = QLabel(
            "Always blocked, no matter what: "
            + ", ".join(H.ToolBelt.BLOCKED[:14]) + " \u2026", self)
        blocked.setWordWrap(True)
        blocked.setStyleSheet("color: #888;")
        form.addRow(blocked)

    def _render_policy_rows(self, rows: "_Rows", field) -> None:
        """One ALLOW / DENY / CONFIRM row per declared tool.

        The rows come from the live registry, not from the schema, which is what
        makes a tool added in `core/tools.py` show up here with no GUI change.
        The dry-run and confirmation rows below them are ordinary table rows of
        this card, in the table's order.
        """
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
        rows.target.addRow(policy_form)
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
            rows.target.addRow(self.policy_edit)

    # -------------------------------------------------------------- appearance

    # ------------------------------------------------- state-colour admission
    # The four state colours are parsed by the bubble with `theme.hex_to_rgb`,
    # which requires a FULL '#rrggbb': an 8-digit value is not truncated and
    # trailing junk is not tolerated. `coerce_settings` filters those values
    # only for type, so a hand-edited "blue" or "#4f8cffXYZ" survived the load,
    # was drawn on the swatch as though it were live, and was then silently
    # discarded by the parser — the bubble kept the old colour while the GUI
    # said otherwise. Admission now uses the parser's own predicate, and a
    # refusal is said out loud instead of being left to be discovered later.
    #
    # The KEY SET is `_state_colour_keys()`, i.e. the loader's own — never a
    # tuple written here (see that function for the failure it caused).
    _HEX6 = re.compile(r"#?[0-9a-fA-F]{6}")

    @classmethod
    def _color_ok(cls, value) -> bool:
        """Whether the bubble's parser would accept this as a state colour."""
        if not isinstance(value, str):
            return False
        if _THEME is not None:
            return _THEME.hex_to_rgb(value) is not None
        # Partial install without core/: fall back to the same anchored shape.
        return cls._HEX6.fullmatch(value.strip()) is not None

    def _adopt_colors(self, raw) -> list:
        """Load the four state colours, refusing anything the bubble discards.

        Returns the (key, value) pairs that were rejected; each is replaced by
        its default, so the swatches show exactly what the bubble will use.
        """
        raw = raw if isinstance(raw, dict) else {}
        defaults = DEFAULT_SETTINGS["colors"]
        clean: dict[str, str] = {}
        rejected: list = []
        for key in _state_colour_keys():
            value = raw.get(key, defaults[key])
            if self._color_ok(value):
                text = str(value).strip()
                # The parser accepts '#rrggbb' or 'rrggbb'; the swatch's Qt
                # stylesheet needs the '#', so store the canonical form.
                clean[key] = text if text.startswith("#") else f"#{text}"
            else:
                clean[key] = defaults[key]
                rejected.append((key, value))
        self._colors = clean
        return rejected

    def _report_rejected_colors(self, rejected, action: str) -> None:
        """Say which colours were refused and what was used instead."""
        detail = ", ".join(f"{key}={value!r}" for key, value in rejected)
        message = (f"rejected {len(rejected)} malformed state colour(s) "
                   f"({detail}) — a colour must be #rrggbb; {action}")
        log.warning("appearance: %s", message)
        self._status(message)

    # ------------------------------------------------------------ appearance
    # The panel is one scrolling column of titled cards. Before this it was a
    # flat stack of unlabelled rows pinned to a page that did not scroll, so a
    # section that grew (the Look row) pushed the live preview past the bottom
    # edge with no scrollbar and nothing on screen to say it was ever there.
    # Two rules now hold it together: nothing is ever unreachable (the page
    # scrolls) and every control says what it is (a card heading, a shared
    # label column, a shared value column).

    FIELD_W = 84          # label column: every field name lines up
    VALUE_W = 74          # readout column: no slider jumps as its value changes

    def _panel_stylesheet(self) -> str:
        """Card and heading look, derived from the LIVE palette.

        Built from QPalette rather than hard-coded colours (and not from CSS
        `palette(...)`, which Qt's stylesheet parser does not honour), so the
        panel reads the same on a light and a dark desktop theme.
        """
        role = QPalette.ColorRole
        pal = self.palette()
        return (
            f"QFrame#card {{ background: {pal.color(role.Base).name()};"
            f" border: 1px solid {pal.color(role.Mid).name()};"
            f" border-radius: 10px; }}"
            "QLabel#cardTitle { font-weight: 600; }"
            f"QLabel#muted {{ color: {pal.color(role.PlaceholderText).name()}; }}"
        )

    def _card(self, title: str, hint: str = ""):
        """One titled card: the panel's only section unit."""
        frame = QFrame(self)
        frame.setObjectName("card")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 12, 14, 14)
        box.setSpacing(10)
        head = QLabel(title, frame)
        head.setObjectName("cardTitle")
        box.addWidget(head)
        if hint:
            sub = QLabel(hint, frame)
            sub.setObjectName("muted")
            sub.setWordWrap(True)
            box.addWidget(sub)
        return frame, box

    def _field(self, label: str, widget: QWidget, value: QLabel | None = None):
        """A labelled control row: name column, control, optional readout."""
        row = QHBoxLayout()
        row.setSpacing(10)
        name = QLabel(label, self)
        name.setObjectName("muted")
        name.setFixedWidth(self.FIELD_W)
        name.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(name)
        row.addWidget(widget, 1)
        if value is not None:
            value.setFixedWidth(self.VALUE_W)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(value)
        return row

    def _muted(self, text: str, parent: QWidget) -> QLabel:
        lab = QLabel(text, parent)
        lab.setObjectName("muted")
        lab.setWordWrap(True)
        return lab

    def _appearance_tab(self) -> QWidget:
        # The BODY only. `_scrolling_page` wraps it like every other tab, so
        # there is one scrolling implementation in this window instead of two
        # that can drift apart (this tab used to own its own, which is how the
        # others came to have none).
        content = QWidget(self)
        content.setStyleSheet(self._panel_stylesheet())
        lay = QVBoxLayout(content)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)
        self.color_buttons: dict[str, QPushButton] = {}
        self.color_swatches: dict[str, QLabel] = {}
        # Adopt before the status bar exists; `_load_values` reports the
        # refusals once it does, so the message is not lost or shown twice.
        self._adopt_colors(self.cfg.get("colors"))
        # Every row on this page is the table's, drawn in the shape the ROW asks
        # for (`render`): the shape, tint, swatch, pack and picture pickers are
        # names in the renderer registry, and the page below is a loop over the
        # table's cards and rows. Nothing here names a setting, so a new row — or
        # a new card — needs one line in settings_schema.py.
        self._group_panels.update({
            ("appearance", "preview"): self._preview_panel,
            ("appearance", "look"): self._look_panel,
            ("appearance", "desktop"): self._desktop_panel,
        })
        # The size note belongs to the sliders, so it travels with their card.
        self._group_tails[("appearance", "motion")] = self._motion_tail
        # The page is its cards, in the table's order, and each card is the
        # table's rows — so a new setting on this page needs one line in
        # settings_schema.py and nothing here. The look tiles come FIRST in the
        # table for the same reason the preview does: the strip is what a look
        # is chosen against, so it is the card at the top of the page, and the
        # page does not have to move a card it already drew to say so.
        for group in _page_groups("appearance"):
            if self._group_panel("appearance", group, lay):
                continue
            card, box = self._card(_group_title("appearance", group),
                                   _group_hint("appearance", group))
            self._draw_group("appearance", group, _Rows(self, box, "card"))
            self._group_tail("appearance", group, box)
            lay.addWidget(card)
        lay.addStretch(1)
        self._paint_color_buttons()
        return content

    def _render_design_picker(self, rows: "_Rows", field) -> None:
        """The shape picker: the names come from the BUBBLE, not from here.

        `BUBBLE_DESIGNS` is what the renderer can paint, so the combo is filled
        from it and the choice is stored under the row's own key.
        """
        self.design_combo = QComboBox(self)
        # display names for designs a bare .capitalize() would flatten
        design_labels = {"sauron": "Eye of Sauron"}
        for name in getattr(SCHEMA, "BUBBLE_DESIGNS", ("orb",)):
            self.design_combo.addItem(design_labels.get(name, name.capitalize()),
                                      name)
        self.design_combo.setToolTip(
            "Bubble shape — applies immediately; the bubble repaints within "
            "seconds. No Save needed.")
        self.design_combo.currentIndexChanged.connect(self._schedule_appearance_live)
        rows.layout(self._field(field.title, self.design_combo))

    def _render_state_pictures(self, rows: "_Rows", field) -> None:
        """ONE row for the four per-state pictures, from the row's own `also`.

        A state with its own file draws it, a state without one draws the
        fallback below, and a state with neither draws the empty slot — the same
        rule a pack follows (`states` then `any`), so the two ways of giving this
        design several pictures obey one precedence instead of two.

        The table gives each picture its own ROW (so the bubble's own loader and
        the live-apply tuple see four settings, one per state) and this shape
        shows them as four buttons on one row, because four rows of one button
        each is a wall of empty space. The other three are claimed by that row's
        `also`, so they are drawn here and nowhere else — and this shape names
        each of them by its state, which the table already says.
        """
        self.state_image_buttons: dict[str, QPushButton] = {}
        if not STATE_IMAGE_KEYS:
            return
        holder = QWidget(self)
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        for i, (state, _key) in enumerate(STATE_IMAGE_KEYS):
            btn = QPushButton(state.capitalize(), holder)
            btn.setMinimumHeight(30)
            btn.clicked.connect(
                lambda _=False, s=state: self._pick_state_image(s))
            self.state_image_buttons[state] = btn
            # TWO to a row, not four: the row's width is the sum of its
            # buttons, and four full state names made this card demand more
            # width than a narrow window's viewport — a card you cannot
            # reach. Two rows of two fit, and read the same.
            grid.addWidget(btn, i // 2, i % 2)
        grid.setColumnStretch(2, 1)
        rows.layout(self._field("Picture per state", holder))
        state_row = QHBoxLayout()
        self.state_image_clear = QPushButton("Clear all states", self)
        self.state_image_clear.setToolTip(
            "Drop every per-state picture; each state goes back to the "
            "fallback picture below")
        self.state_image_clear.clicked.connect(self._clear_state_images)
        state_row.addWidget(self.state_image_clear)
        state_row.addStretch(1)
        rows.layout(state_row)
        self.state_image_label = self._muted("", self)
        rows.widget(self.state_image_label)

    def _render_fallback_image(self, rows: "_Rows", field) -> None:
        """The FALLBACK picture: what a state with no picture of its own draws.

        It lives in the Shape card rather than a card of its own because a shape
        whose art comes from a file is still a shape; it is always visible (a
        picture may be chosen before the design is switched to it) and it
        applies live like the combo, which is the contract this tab promises.
        """
        holder = QWidget(self)
        pick_row = QHBoxLayout(holder)
        pick_row.setContentsMargins(0, 0, 0, 0)
        self.image_button = QPushButton("Choose fallback\u2026", holder)
        self.image_button.setToolTip(
            "The picture the \"Image\" design draws in every state that has no "
            "picture of its own: fitted to the glass, tinted by the state "
            "colour and lit by the voice.")
        self.image_button.clicked.connect(self._pick_design_image)
        self.image_clear = QPushButton("Clear", holder)
        self.image_clear.setToolTip(
            "Go back to the empty-slot frame for the states that fall back")
        self.image_clear.clicked.connect(self._clear_design_image)
        pick_row.addWidget(self.image_button)
        pick_row.addWidget(self.image_clear)
        pick_row.addStretch(1)
        rows.layout(self._field(field.title, holder))
        self.image_label = self._muted("", self)
        rows.widget(self.image_label)

    def _render_pack_picker(self, rows: "_Rows", field) -> None:
        """A pack: the same art as several pictures that switch with the state.

        It sits beside the single file as an alternative SOURCE for this shape's
        art. When one is chosen it is the AUTHORITY and the picture above it is
        ignored — which the label under the row says out loud, because "I picked
        a pack and nothing changed" is the complaint this card exists to answer.
        """
        self.pack_combo = QComboBox(self)
        self.pack_combo.setToolTip(
            "An installed design pack: one picture per state, chosen together "
            "and switched as the bubble changes state.")
        self.pack_combo.currentIndexChanged.connect(self._on_pack_changed)
        self.pack_install = QPushButton("Install pack\u2026", self)
        self.pack_install.setToolTip(
            "Copy a folder holding a pack.json and its pictures into this "
            "install, so the pack keeps working when the folder it came from "
            "moves. Pictures may live in subfolders.")
        self.pack_install.clicked.connect(self._install_design_pack)
        # The same install, from the shape a pack actually travels in: ONE file
        # someone sent you. The archive is unpacked and checked by the bubble
        # module BEFORE anything reaches the installed packs, and from there it
        # is the same code path a folder takes — so a file can only ever do
        # what a folder could already do.
        self.pack_import_file = QPushButton("Import pack file\u2026", self)
        self.pack_import_file.setToolTip(
            "Install a pack that arrived as ONE file (a .hpack someone sent "
            "you). Checked before anything is copied, then copied in, so it "
            "keeps working wherever that file goes afterwards.")
        self.pack_import_file.clicked.connect(self._import_design_pack_file)
        self.pack_clear = QPushButton("Clear", self)
        self.pack_clear.setToolTip("Stop using a pack; go back to one picture")
        self.pack_clear.clicked.connect(self._clear_design_pack)
        # The reverse of Install: write the art ON SCREEN — the selected pack,
        # or the pictures chosen above — into a new folder as a pack, so a look
        # built by hand can be handed to someone else.
        self.pack_export = QPushButton("Export pack\u2026", self)
        self.pack_export.setToolTip(
            "Write the art on screen (this pack, or your own per-state "
            "pictures and fallback) into a new folder as a pack you can share. "
            "Nothing is installed and nothing on screen changes.")
        self.pack_export.clicked.connect(self._export_design_pack)
        # ...and the same art in the shape you can actually SEND: one file,
        # because a folder is not something anyone can attach to a message.
        self.pack_export_file = QPushButton("Export pack file\u2026", self)
        self.pack_export_file.setToolTip(
            "Write the art on screen (this pack, or your own per-state "
            "pictures and fallback) into ONE .hpack file you can send to "
            "someone, who installs it with Import pack file. Nothing is "
            "installed and nothing on screen changes.")
        self.pack_export_file.clicked.connect(self._export_design_pack_file)
        rows.layout(self._field(field.title, self.pack_combo))
        # Two rows on purpose: the first is what a pack does on THIS desktop
        # (install a folder, export a folder, stop using one) and the second is
        # the single-file form of the same two things — the pair you reach for
        # when a look arrives or leaves as an attachment.
        pack_row = QHBoxLayout()
        pack_row.addWidget(self.pack_install)
        pack_row.addWidget(self.pack_export)
        pack_row.addWidget(self.pack_clear)
        pack_row.addStretch(1)
        rows.layout(pack_row)
        file_row = QHBoxLayout()
        file_row.addWidget(self.pack_import_file)
        file_row.addWidget(self.pack_export_file)
        file_row.addStretch(1)
        rows.layout(file_row)
        self.pack_label = self._muted("", self)
        rows.widget(self.pack_label)
        # Look before you leap: a pack can be shown in the strip above WITHOUT
        # being installed, so the choice gets made with the look in front of you
        # instead of blind. Two buttons because a pack arrives in two shapes,
        # the same pairing Install/Import already teaches.
        preview_row = QHBoxLayout()
        self.pack_preview_dir = QPushButton("Preview folder\u2026", self)
        self.pack_preview_dir.setToolTip(
            "Show what a pack FOLDER would look like in all four states, drawn "
            "in the strip above. Nothing is installed and nothing is copied.")
        self.pack_preview_dir.clicked.connect(
            lambda: self._preview_design_pack("folder"))
        self.pack_preview_file = QPushButton("Preview pack file\u2026", self)
        self.pack_preview_file.setToolTip(
            "Show what a .hpack someone sent you would look like in all four "
            "states. The file is unpacked to a temporary folder so the strip "
            "can draw it; nothing is installed.")
        self.pack_preview_file.clicked.connect(
            lambda: self._preview_design_pack("file"))
        preview_row.addWidget(self.pack_preview_dir)
        preview_row.addWidget(self.pack_preview_file)
        preview_row.addStretch(1)
        rows.layout(preview_row)
        self.preview_label = self._muted("", self)
        rows.widget(self.preview_label)
        follow_row = QHBoxLayout()
        self.preview_try = QPushButton("Try it", self)
        self.preview_try.setToolTip(
            "Install the previewed pack and switch to it. THIS is the step that "
            "writes: everything before it only showed you the look.")
        self.preview_try.clicked.connect(self._try_design_pack)
        self.preview_cancel = QPushButton("Cancel preview", self)
        self.preview_cancel.setToolTip(
            "Stop previewing — nothing was installed, and the strip goes "
            "back to the look you actually have.")
        self.preview_cancel.clicked.connect(self._cancel_design_pack_preview)
        follow_row.addWidget(self.preview_try)
        follow_row.addWidget(self.preview_cancel)
        follow_row.addStretch(1)
        rows.layout(follow_row)
        self.preview_label.setVisible(False)
        self._toggle_preview_buttons()

    def _render_deco_picker(self, rows: "_Rows", field) -> None:
        """The decoration picker — a different choice from the art and the shape.

        It is animated, it is the thing the voice visibly drives, and there are
        several to try. The schema owns the closed set (`AVATAR_DECOS`) and
        core.settings coerces to it, so this combo can only hold a name the
        painter implements.
        """
        self.deco_combo = QComboBox(self)
        for _value, _label in (("off", "Off"),
                               ("ring-light", "Ring light"),
                               ("orbit", "Orbiting comets"),
                               ("pulse", "Pulse rings"),
                               ("aurora", "Aurora ribbons"),
                               ("rainbow", "Rainbow ring"),
                               ("sparkle", "Sparkles"),
                               ("comet", "Comet"),
                               ("neon", "Neon tubes"),
                               ("flames", "Flames")):
            self.deco_combo.addItem(_label, _value)
        self.deco_combo.setToolTip(
            "The avatar's decoration: an animated light around the picture. "
            "Each one turns on its own with the animation energy, brightens "
            "and quickens with your voice, and is painted in the colour the "
            "row below chooses. Applies live, no Save needed.")
        self.deco_combo.currentIndexChanged.connect(
            self._schedule_appearance_live)
        self.deco_combo.currentIndexChanged.connect(self._update_deco_hint)
        rows.layout(self._field(field.title, self.deco_combo))
        self.deco_hint = self._muted("", self)
        rows.widget(self.deco_hint)
        self._update_deco_hint()

    def _render_deco_colour(self, rows: "_Rows", field) -> None:
        """The decoration's OWN colour: three answers, and the third is the point.

        "Colour of its own" is a colour the ring keeps while the bubble changes
        state, which is what makes a decoration independent rather than a copy
        of the mood. `rainbow` is the animated one, so the ring moves in colour
        as well as in shape. The words come from the schema
        (`_deco_colour_words`); only the LABELS live here, with a readable
        fallback — so a colour the schema adds is offered the day it is declared
        rather than only once this file is edited too. `custom` is last and is
        never stored (see `_collect`).
        """
        _colour_labels = {"state": "State colour", "rainbow": "Rainbow"}
        self.deco_colour_combo = QComboBox(self)
        for _value in _deco_colour_words():
            self.deco_colour_combo.addItem(
                _colour_labels.get(_value, _value.replace("-", " ").capitalize()),
                _value)
        self.deco_colour_combo.addItem("Colour of its own", "custom")
        self.deco_colour_combo.setToolTip(
            "What the decoration is painted in. State colour tracks the "
            "bubble's mood (the rim carries the state anyway); Rainbow sweeps "
            "through every hue on its own, speeding up with your voice; "
            "Colour of its own keeps one colour whatever the state is.")
        self.deco_colour_combo.currentIndexChanged.connect(
            self._schedule_appearance_live)
        self.deco_colour_combo.currentIndexChanged.connect(
            self._update_deco_colour)
        rows.layout(self._field(field.title, self.deco_colour_combo))
        self.deco_colour_btn = QPushButton("#4F8CFF", self)
        self.deco_colour_btn.setToolTip(
            "Pick the decoration's own colour. The bubble's rim still carries "
            "the state colour, so the mood stays readable.")
        self.deco_colour_btn.clicked.connect(self._pick_deco_colour)
        rows.layout(self._field("Own colour", self.deco_colour_btn))
        self._update_deco_colour()

    def _render_colour_grid(self, rows: "_Rows", field) -> None:
        """The swatch grid: one button per STATE, from the loader's key set."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        for i, key in enumerate(_state_colour_keys()):
            cell = QWidget(self)
            column = QVBoxLayout(cell)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(3)
            btn = QPushButton(key.capitalize(), cell)
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda _=False, k=key: self._pick_color(k))
            self.color_buttons[key] = btn
            swatch = self._muted("", cell)
            swatch.setAlignment(Qt.AlignHCenter)
            self.color_swatches[key] = swatch
            column.addWidget(btn)
            column.addWidget(swatch)
            grid.addWidget(cell, i // 4, i % 4)
        reset = QPushButton("Reset", self)
        reset.setToolTip("Restore the four default state colours")
        reset.clicked.connect(self._reset_colors)
        reset_row = QHBoxLayout()
        reset_row.addStretch(1)
        reset_row.addWidget(reset)
        rows.layout(grid)
        rows.layout(reset_row)

    def _motion_tail(self, box) -> None:
        """What the size slider actually measures, under the sliders."""
        box.addWidget(self._muted(
            "Window size in pixels — the drawn bubble is about 69% of it.",
            self))

    def _preview_panel(self, layout: QVBoxLayout) -> None:
        """The strip: the four states, drawn live from the controls' values.

        A live preview and nothing else — the values it draws are read through
        the callables below on every repaint, so it shows what the page is
        showing rather than a copy taken once at build time.
        """
        card, box = self._card(_group_title("appearance", "preview"),
                               _group_hint("appearance", "preview"))
        self.preview = BubblePreview(
            lambda: {k: QColor(c) for k, c in self._colors.items()},
            lambda: self.bubble_size.value(),
            self._preview_design,
            lambda: self.animation_energy.value() / 100.0,
            lambda: self.bubble_accent.value() / 100.0,
            self._preview_picture,
            self._preview_deco,
        )
        self.preview.setMinimumHeight(168)
        box.addWidget(self.preview)
        layout.addWidget(card)

    def _look_panel(self, layout: QVBoxLayout) -> None:
        """The named looks: one click for the five controls under them."""
        card, box = self._card(_group_title("appearance", "look"),
                               _group_hint("appearance", "look"))
        self._build_look_group(card, box)
        layout.addWidget(card)

    def _desktop_panel(self, layout: QVBoxLayout) -> None:
        """Match the wallpaper: three one-shot actions, no setting of its own.

        No table row belongs here — nothing on this card is remembered — which
        is what makes it a panel rather than a card of rows.
        """
        card, box = self._card(_group_title("appearance", "desktop"),
                               _group_hint("appearance", "desktop"))
        tune_row = QHBoxLayout()
        match_btn = QPushButton("Match wallpaper", card)
        match_btn.setToolTip(
            "Sample the configured wallpaper and retune all four state colours "
            "so the bubble reads clearly against it")
        match_btn.clicked.connect(self._match_wallpaper)
        dark_btn = QPushButton("Dark tuning", card)
        dark_btn.setToolTip("Retune the palette for a dark backdrop (no detection)")
        dark_btn.clicked.connect(lambda: self._apply_wallpaper_tuning(0.05))
        light_btn = QPushButton("Light tuning", card)
        light_btn.setToolTip("Retune the palette for a light backdrop (no detection)")
        light_btn.clicked.connect(lambda: self._apply_wallpaper_tuning(0.95))
        tune_row.addWidget(match_btn)
        tune_row.addWidget(dark_btn)
        tune_row.addWidget(light_btn)
        tune_row.addStretch(1)
        box.addLayout(tune_row)
        layout.addWidget(card)

    # ------------------------------------------------------------------ looks

    def _build_look_group(self, parent: QWidget, box) -> None:
        """The named-look picker: one tile per catalogue entry, wrapped.

        The catalogue is DATA in settings_schema (a look only names the five
        keys the tab already owns), and "which look is current" is DERIVED from
        the widgets — there is no stored name to fall out of step with them.
        That is why the checked tile is recomputed after every change instead
        of being set once and trusted.

        The tiles WRAP four to a row. In one long row they were the widest
        thing on the page and set the panel's minimum width, which is how a
        section ends up dictating the size of the window around it.
        """
        self.look_buttons: dict[str, QPushButton] = {}
        self._looks = tuple(getattr(SCHEMA, "APPEARANCE_LOOKS", ()))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self._look_clock = QElapsedTimer()
        self._look_clock.start()
        for i, entry in enumerate(self._looks):
            tile = LookTile(entry, self._look_clock, parent)
            tile.setToolTip(self._look_tooltip(entry))
            tile.setAccessibleName(str(entry["label"]))
            tile.clicked.connect(
                lambda _=False, name=str(entry["name"]): self._apply_look(name))
            self.look_buttons[str(entry["name"])] = tile
            grid.addWidget(tile, i // 4, i % 4)
        grid.setColumnStretch(4, 1)
        box.addLayout(grid)
        # ONE timer for the whole strip (see LookTile): it exists so the tiles
        # look alive before you click one, and it dies with the card.
        self._look_anim = QTimer(parent)
        self._look_anim.setInterval(LookTile.TICK_MS)
        self._look_anim.timeout.connect(
            lambda: [tile.update() for tile in self.look_buttons.values()])
        self._look_anim.start()
        self.look_label = QLabel("", parent)
        self.look_label.setObjectName("muted")
        box.addWidget(self.look_label)
        if not self._looks:
            # an older settings_schema in a partial install: say so rather than
            # showing an empty card with no explanation
            self.look_label.setText(
                "Look presets are unavailable in this install.")
        self._refresh_look_buttons()

    def _look_tooltip(self, entry: dict) -> str:
        """Spell out exactly what the click will set — it overwrites your own."""
        colors = entry["colors"]
        return (
            f"{entry['note']}\n"
            f"shape {entry['design']} · {entry['bubble_size']} px · "
            f"energy {float(entry['animation_energy']):.1f}× · "
            f"accent {float(entry['bubble_accent']):.0%}\n"
            f"idle {colors['idle']} · listening {colors['listening']} · "
            f"thinking {colors['thinking']} · speaking {colors['speaking']}\n"
            "Sets all five controls and applies live — no Save needed "
            "(this replaces the current shape, size, sliders and colours).")

    def _look_values_now(self) -> dict:
        """The five Appearance values the controls are showing right now."""
        return {
            "bubble_design": self.design_combo.currentData() or "orb",
            "bubble_size": self.bubble_size.value(),
            "animation_energy": self.animation_energy.value() / 100.0,
            "bubble_accent": self.bubble_accent.value() / 100.0,
            "colors": dict(self._colors),
        }

    def _current_look(self) -> str:
        """Name of the look the controls spell out, or "" for Custom."""
        matcher = getattr(SCHEMA, "look_matching", None)
        if matcher is None:
            return ""
        try:
            return matcher(self._look_values_now())
        except Exception:
            log.debug("look lookup failed", exc_info=True)
            return ""

    def _refresh_look_buttons(self) -> None:
        """Check the button the controls match — or none, and say "Custom".

        Called after every appearance change, so nudging one slider drops the
        tick immediately instead of leaving a look selected that the controls
        no longer show.
        """
        buttons = getattr(self, "look_buttons", None)
        if not buttons:
            return
        name = self._current_look()
        for look_name, btn in buttons.items():
            btn.setChecked(look_name == name)
        if name:
            entry = SCHEMA.look(name) if hasattr(SCHEMA, "look") else None
            label = entry["label"] if entry else name
            self.look_label.setText(f"current look: {label}")
        else:
            self.look_label.setText(
                "current look: Custom — these are your own settings")

    def _apply_look(self, name: str) -> None:
        """One click: set every Appearance control from a named look.

        Writes through the SAME debounced live-apply path a slider drag uses,
        so the change reaches settings.json and the running bubble with no
        Save. A look that names a design this build does not ship applies
        NOTHING and says so: applying the other four keys would silently leave
        a half-look behind.
        """
        lookup = getattr(SCHEMA, "look", None)
        entry = lookup(name) if lookup is not None else None
        if entry is None:
            self._status(f"no such look: {name}")
            return
        index = self.design_combo.findData(str(entry["design"]))
        if index < 0:
            self._status(f"look {entry['label']}: this install has no "
                         f"{entry['design']!r} shape — nothing applied")
            return
        self.design_combo.setCurrentIndex(index)
        self.bubble_size.setValue(int(entry["bubble_size"]))
        self.animation_energy.setValue(
            int(round(float(entry["animation_energy"]) * 100)))
        self.bubble_accent.setValue(
            int(round(float(entry["bubble_accent"]) * 100)))
        self._colors = {str(k): str(v) for k, v in entry["colors"].items()}
        self._paint_color_buttons()
        self.preview.update()
        self._refresh_look_buttons()
        self._schedule_appearance_live()

    def _apply_wallpaper_tuning(self, luminance: float) -> None:
        """Retune the four state colours for a dark or light backdrop."""
        if _THEME is None:
            self._status("theme helpers unavailable in this install")
            return
        tuned = _THEME.tune_colors_for_background(self._colors, luminance)
        self._colors = tuned
        self._paint_color_buttons()
        self.preview.update()
        backdrop = "dark" if float(luminance) < 0.5 else "light"
        self._status(f"state colours retuned for a {backdrop} backdrop")
        self._schedule_appearance_live()

    def _match_wallpaper(self) -> None:
        """One click: sample the current wallpaper and retune for it.

        Discovery covers what this desktop actually uses — a shell-owned
        wallpaper directory and its caches, an image OR a video (`ffmpeg` takes
        one frame), and the niri config's spawned-image form — because the
        button used to consult the niri config alone, which names a wallpaper
        only when something like swaybg is spawned from it. Measured on this
        machine: luminance came back None every time, so the click was a no-op
        with a message about ImageMagick, which was installed all along. The
        status line now says which file it sampled, or what it looked at.
        """
        if _THEME is None:
            self._status("theme helpers unavailable in this install")
            return
        try:
            report = _THEME.wallpaper_report(NIRI_CONFIG)
        except OSError as e:
            self._status(f"could not read the wallpaper: {e}")
            return
        luminance = report.get("luminance")
        if luminance is None:
            checked = report.get("checked") or []
            where = (", ".join(Path(p).name for p in checked[:3])
                     if checked else "nothing (no image path in the niri config, "
                     "no shell wallpaper directory and no swww/hyprpaper cache)")
            self._status(f"could not sample the wallpaper — looked at: {where}")
            return
        self._apply_wallpaper_tuning(luminance)
        self._status(f"state colours matched {Path(str(report['path'])).name}"
                     f" (backdrop luminance {float(luminance):.2f})")

    def _paint_color_buttons(self) -> None:
        for key, btn in self.color_buttons.items():
            hexcol = self._colors[key]
            # Readable label on ANY state colour. White was hard-coded, so a
            # light swatch (a daylight idle, a yellow) had an invisible name on
            # it — the one control in the panel you could not read.
            col = QColor(hexcol)
            luma = (0.299 * col.red() + 0.587 * col.green()
                    + 0.114 * col.blue()) / 255.0
            ink = "#101014" if luma > 0.6 else "#ffffff"
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {hexcol}; color: {ink}; "
                f"border: 1px solid #555; border-radius: 6px; }}"
                f"QPushButton:hover {{ border: 1px solid {ink}; }}")
            btn.setToolTip(hexcol)
            swatch = getattr(self, "color_swatches", {}).get(key)
            if swatch is not None:
                swatch.setText(hexcol.upper())

    def _pick_color(self, key: str) -> None:
        col = QColorDialog.getColor(QColor(self._colors[key]), self, f"{key} colour")
        if col.isValid():
            chosen = col.name()
            if not self._color_ok(chosen):
                # The dialog yields '#rrggbb', so this is a guard on the
                # invariant rather than an expected path: nothing may enter
                # `_colors` that the bubble's parser would throw away.
                self._report_rejected_colors([(key, chosen)],
                                             "keeping the current colour")
                return
            self._colors[key] = chosen
            self._paint_color_buttons()
            self.preview.update()
            self._schedule_appearance_live()

    def _reset_colors(self) -> None:
        self._colors = dict(DEFAULT_SETTINGS["colors"])
        self._paint_color_buttons()
        self.preview.update()
        self._schedule_appearance_live()

    # ------------------------------------------------------- live appearance

    def _schedule_appearance_live(self, *_args) -> None:
        """Debounce a live apply; the last change wins.

        Every Appearance control funnels through here, which makes it the one
        place to keep the Look section honest: the tick follows the CONTROLS, so
        a hand edit (or a reload from disk) updates it too, not just a look
        click.
        """
        self._refresh_look_buttons()
        # ...and the pack label follows the SHAPE too: switching away from the
        # Image design turns a pack's "in use" into a statement about what is
        # actually on screen.
        self._refresh_pack_label()
        self._live_timer.start()

    # ------------------------------------------------- the `image` design's art

    def _pick_design_image(self) -> None:
        """Choose the file the `image` design draws, and apply it live.

        Written straight into the form and the live timer is armed, so a chosen
        picture reaches the bubble with no Save — the same contract the shape
        combo has, because this IS picking a shape's art. A cancelled dialog
        writes nothing at all: closing the picker is not an edit.
        """
        start = str(self._design_image or "")
        try:
            start_dir = str(Path(start).parent) if start else str(Path.home())
        except (OSError, ValueError):
            start_dir = str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose the bubble's picture", start_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.svg *.xpm);;"
            "All files (*)")
        if not chosen:
            return                  # cancelled: no write, no live apply
        self._design_image = chosen
        # The per-state rows inherit this picture, so their tooltips and the
        # summary line change with it even though no state's OWN choice did.
        self._refresh_state_image_buttons()
        self._refresh_design_image_label()
        self._use_image_design()
        self._schedule_appearance_live()

    def _clear_design_image(self) -> None:
        """Back to the empty slot — a real edit, and applied live like one."""
        if not self._design_image:
            return
        self._design_image = ""
        self._refresh_state_image_buttons()
        self._refresh_design_image_label()
        self._schedule_appearance_live()

    # ------------------------------------------------------ one per STATE

    def _pick_state_image(self, state: str) -> None:
        """Choose the file THIS state draws, and apply it live.

        The dialog starts in the folder of the picture that state already uses
        (its own, else the fallback), because choosing four pictures usually means
        four files from one folder. A cancelled dialog writes nothing: closing a
        file chooser is not an edit.
        """
        start = str(self._design_images.get(state)
                    or self._design_image or "")
        try:
            start_dir = str(Path(start).parent) if start else str(Path.home())
        except (OSError, ValueError):
            start_dir = str(Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, f"Picture the bubble draws while {state}", start_dir,
            "Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.svg *.xpm);;"
            "All files (*)")
        if not chosen:
            return                  # cancelled: no write, no live apply
        self._design_images[state] = chosen
        self._refresh_state_image_buttons()
        self._refresh_design_image_label()
        self._use_image_design()
        self._schedule_appearance_live()

    def _clear_state_images(self) -> None:
        """Drop every per-state picture; each state falls back. One edit."""
        if not any(self._design_images.get(state) for state, _k in STATE_IMAGE_KEYS):
            return
        for state, _key in STATE_IMAGE_KEYS:
            self._design_images[state] = ""
        self._refresh_state_image_buttons()
        self._refresh_design_image_label()
        self._schedule_appearance_live()

    def _refresh_state_image_buttons(self) -> None:
        """Each state's button names what it draws, or says it falls back.

        The button's TEXT is the state and its TOOLTIP is what that state draws
        right now — the full path, or the fallback it inherits, or that it would
        draw the empty slot. A row of four buttons that only said "Choose" would
        leave "which states have their own picture" unanswerable without
        clicking them.
        """
        buttons = getattr(self, "state_image_buttons", None)
        if not buttons:
            return
        for state, _key in STATE_IMAGE_KEYS:
            btn = buttons.get(state)
            if btn is None:
                continue
            path = str(self._design_images.get(state) or "")
            if path:
                btn.setText(f"{state.capitalize()} \u2713")
                btn.setToolTip(f"{state} draws {path}\nClick to change it.")
            elif self._design_image:
                btn.setText(state.capitalize())
                btn.setToolTip(
                    f"{state} has no picture of its own — it draws the "
                    f"fallback: {self._design_image}\nClick to give it one.")
            else:
                btn.setText(state.capitalize())
                btn.setToolTip(
                    f"{state} has no picture: it draws the empty slot.\n"
                    f"Click to give it one.")
        self._refresh_state_image_label()

    def _refresh_state_image_label(self) -> None:
        """One line per state: the picture in effect, or WHY it cannot draw.

        A per-state picker makes "which of the four is wrong" the whole
        question, so the reason is printed next to the state it belongs to and
        it is the bubble module's own sentence (`design_image_problem`) — the one
        `doctor` prints. A pack overrides all four, and the pack label says so,
        so this line says what the files WOULD draw instead of pretending to
        describe the desktop.
        """
        label = getattr(self, "state_image_label", None)
        if label is None or not STATE_IMAGE_KEYS:
            return
        bubble = None
        try:
            bubble = _core_module("bubble")
        except Exception:
            log.debug("state image check: bubble module unavailable",
                      exc_info=True)
        rows = []
        for state, _key in STATE_IMAGE_KEYS:
            path = str(self._design_images.get(state) or "")
            if not path:
                # Shown in FULL, like an own picture: a state drawing the
                # fallback is exactly the case where "which file is that?" is
                # the question, and a bare file name cannot answer it.
                rows.append(f"{state}: {self._design_image} (fallback)"
                            if self._design_image else f"{state}: empty slot")
                continue
            problem = ""
            if bubble is not None:
                try:
                    problem = bubble.design_image_problem(path)
                except Exception:
                    log.debug("state image check failed", exc_info=True)
            rows.append(f"{state}: \u26a0 {problem}" if problem
                        else f"{state}: {path}")
        label.setText("\n".join(rows))

    def _use_image_design(self) -> None:
        """Point the Design combo at `image`, so chosen art is on screen.

        Every other control in this card applies itself; art that was chosen
        and silently NOT drawn — because the shape is still a painter — is the
        "I picked it and nothing happened" complaint in a new costume. Switching
        the combo arms the live apply itself, so the change reaches the bubble
        the same way a manual pick does.
        """
        combo = getattr(self, "design_combo", None)
        if combo is None:
            return
        index = combo.findData("image")
        if index >= 0 and combo.currentIndex() != index:
            combo.setCurrentIndex(index)

    def _design_picture(self, state: str = ""):
        """The art in effect for `state`, resolved the way the BUBBLE does.

        The precedence (pack, then this state's own picture, then the fallback)
        lives in the bubble module's `picture_for`, so the panel and the desktop
        cannot disagree about which picture a state draws — which is the
        preview's whole job, and exactly where two implementations would drift.
        A still comes back as a path; an ANIMATED pack state comes back as a
        spec dict, and the strip draws its first frame (it is a decision aid,
        not a projector).
        """
        try:
            bubble = _core_module("bubble")
        except Exception:
            log.debug("design picture: bubble module unavailable", exc_info=True)
            return ""
        try:
            return bubble.picture_for(self._design_pack,
                                      self._design_image, state,
                                      self._design_images) or ""
        except Exception:
            log.debug("design picture resolution failed", exc_info=True)
            return ""

    def _on_pack_changed(self, *_args) -> None:
        """The user picked a pack: mirror it into the form, then apply live."""
        combo = getattr(self, "pack_combo", None)
        if combo is None:
            return
        self._design_pack = str(combo.currentData() or "")
        self._refresh_pack_label()
        self._refresh_design_image_label()
        if self._design_pack:
            # Same rule as choosing a picture or installing a pack: art that is
            # selected has to be the art that is DRAWN, so the shape follows.
            self._use_image_design()
        self._schedule_appearance_live()

    def _refresh_pack_combo(self) -> None:
        """Fill the Pack combo from what is installed, keeping the selection.

        Rebuilt rather than appended to: an install lands a NEW folder, so the
        list has to be able to grow without a restart, and rebuilding is what
        lets the combo show a saved pack that is currently missing instead of
        quietly falling back to "no pack".
        """
        combo = getattr(self, "pack_combo", None)
        if combo is None:
            return
        chosen = str(self._design_pack or "")
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("(no pack \u2014 one picture)", "")
            listed = set()
            try:
                packs = _core_module("bubble").installed_packs()
            except Exception:
                log.debug("pack listing failed", exc_info=True)
                packs = []
            for slug, name in packs:
                listed.add(slug)
                combo.addItem(f"{name} ({slug})" if name != slug else slug, slug)
            if chosen and chosen not in listed:
                # A saved pack that is no longer installed is LISTED under its
                # slug, so the combo shows what settings actually say; the
                # label beneath explains why nothing is drawn.
                combo.addItem(f"{chosen} (not installed)", chosen)
            index = combo.findData(chosen)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            combo.blockSignals(False)
        self._refresh_pack_label()

    def _refresh_pack_label(self) -> None:
        """What the pack is doing — or WHY it cannot draw anything.

        The sentence comes from the bubble module (`pack_problem`), the same one
        `doctor` prints, so the panel and the desktop describe one pack with one
        wording instead of two GUIs disagreeing about one folder.
        """
        label = getattr(self, "pack_label", None)
        if label is None:
            return
        slug = str(self._design_pack or "")
        if not slug:
            label.setText(
                "No pack \u2014 the Image design draws the single picture above.")
            return
        problem = ""
        try:
            problem = _core_module("bubble").pack_problem(slug)
        except Exception:
            log.debug("pack check failed", exc_info=True)
        if problem:
            label.setText(f"\u26a0 {problem}")
            return
        # A pack can be SELECTED and still not draw anything: choose one (the
        # shape follows it to `image`), then pick a painter from the combo, and
        # the art is off screen while a label claiming "in use" lies about which
        # source is drawing \u2014 the same class of lie the preview's glyph
        # guards against one card up. So the sentence follows the SHAPE too.
        combo = getattr(self, "design_combo", None)
        design = str(combo.currentData() or "") if combo is not None else ""
        if design != "image":
            label.setText(
                f"Pack {slug} is selected, but the bubble is drawing "
                f"{design or 'another shape'} \u2014 the pack's pictures are not "
                f"on screen.")
            return
        label.setText(f"Pack {slug} is in use \u2014 one picture per state.")

    def _install_design_pack(self) -> None:
        """Copy a pack folder into this install and select it.

        Validation happens in the bubble module BEFORE anything is copied, so a
        refused folder leaves the installed packs untouched. The message shown
        either way is that module's own sentence, in the status line and the
        pack label, so a refusal cannot be mistaken for a success.
        """
        try:
            bubble = _core_module("bubble")
        except Exception:
            self._status("design packs are unavailable in this install")
            return
        try:
            start_dir = str(Path(self._design_pack or "").expanduser())
            if not Path(start_dir).is_dir():
                start_dir = str(Path.home())
        except (OSError, ValueError):
            start_dir = str(Path.home())
        source = QFileDialog.getExistingDirectory(
            self, "Choose a design pack folder", start_dir)
        if not source:
            return                  # cancelled: not an edit
        try:
            slug, message = bubble.install_pack(source)
        except Exception as exc:    # a data folder must never crash the panel
            self._status(f"could not install that pack ({exc})")
            return
        self._adopt_design_pack(slug, message)

    def _import_design_pack_file(self) -> None:
        """Install a pack that arrived as ONE file.

        The same channel as Install pack\u2026, in the shape a pack actually
        travels in: the archive is unpacked and checked by the bubble module
        BEFORE anything reaches the installed packs, and from there it is the
        identical install \u2014 so a file cannot do anything a folder could
        not. The message shown either way is that module's own sentence.
        """
        try:
            bubble = _core_module("bubble")
        except Exception:
            self._status("design packs are unavailable in this install")
            return
        chosen, _chosen_filter = QFileDialog.getOpenFileName(
            self, "Choose a pack file", str(Path.home()),
            "Handsoff pack (*.hpack);;Zip archive (*.zip);;All files (*)")
        if not chosen:
            return                  # cancelled: not an edit
        try:
            slug, message = bubble.install_pack_file(chosen)
        except Exception as exc:    # a data file must never crash the panel
            self._status(f"could not install that pack file ({exc})")
            return
        self._adopt_design_pack(slug, message)

    def _adopt_design_pack(self, slug: str, message: str) -> None:
        """Select the pack an install just landed \u2014 and report either way.

        The status line carries the OUTCOME (installed, or why not) while the
        label always describes the CURRENT state: writing the outcome into the
        label would leave a refusal's sentence describing a pack that is no
        longer what is selected, and the next refresh would overwrite it anyway.
        """
        self._drop_pack_preview()       # an install supersedes anything shown
        self._status(message)
        if not slug:
            self._refresh_pack_label()   # nothing changed: describe the state
            return                  # refused: nothing selected, nothing written
        self._design_pack = slug
        self._refresh_pack_combo()
        self._refresh_design_image_label()
        self._use_image_design()
        self._schedule_appearance_live()

    def _clear_design_pack(self) -> None:
        """Stop using a pack \u2014 a real edit, and applied live like one."""
        if not self._design_pack:
            return
        self._design_pack = ""
        self._refresh_pack_combo()
        self._refresh_design_image_label()
        self._schedule_appearance_live()

    def _export_art(self) -> dict:
        """The appearance settings as the FORM has them, for an export to read.

        An export has to write what is ON SCREEN, and a picture chosen a moment
        ago applies through a debounce \u2014 so `settings.json` can still hold
        the previous one. The form's own values are handed over instead, which
        is why both exports share this rather than each reading the saved file.
        """
        art = dict(self.cfg)
        art["design_pack"] = str(self._design_pack or "")
        art["design_image_path"] = str(self._design_image or "")
        for state, key in STATE_IMAGE_KEYS:
            art[key] = str(self._design_images.get(state) or "")
        return art

    def _ask_export_target(self, title: str):
        """The name and destination an export asked for, or None if cancelled.

        ONE prompt for both export shapes, so a cancelled dialog means the same
        thing whichever button was pressed and neither can ask a different
        question.
        """
        name, ok = QInputDialog.getText(
            self, title, "Pack name (letters, digits, dash):",
            text=str(self._design_pack or "my-look"))
        if not ok or not str(name).strip():
            return None             # cancelled: not an edit
        parent = QFileDialog.getExistingDirectory(
            self, "Choose the folder to write the pack into", str(Path.home()))
        if not parent:
            return None             # cancelled: nothing written
        return str(name).strip(), parent

    def _export_design_pack(self) -> None:
        """Write the art on screen as a pack FOLDER, reported either way.

        A name and a destination are asked for, then handed to the bubble
        module, which writes nothing until the folder it built passes the SAME
        validation an install uses. Nothing is selected or installed afterwards:
        the result is a folder to hand on, not a change to this desktop.
        """
        self._export_pack_like("Export pack", "export_pack")

    def _export_design_pack_file(self) -> None:
        """Write the art on screen as ONE file a look can be sent in.

        The same art as `_export_design_pack` from the same module, in the shape
        that can be attached to a message \u2014 which is the only difference
        between them, and the reason both go through one place.
        """
        self._export_pack_like("Export pack file", "export_pack_file")

    def _export_pack_like(self, title: str, method: str) -> None:
        """Ask for a target, then run one of the module's two exports.

        Both exports refuse, write and describe themselves in that module's own
        words; this only decides WHICH one ran, so the two buttons cannot drift
        into asking different questions or reporting different outcomes.
        """
        try:
            bubble = _core_module("bubble")
        except Exception:
            self._status("design packs are unavailable in this install")
            return
        self._drop_pack_preview()       # an export reads the art, not a preview
        asked = self._ask_export_target(title)
        if asked is None:
            return                  # cancelled: not an edit
        name, parent = asked
        try:
            _written, message = getattr(bubble, method)(
                parent, name, self._export_art())
        except Exception as exc:    # a data folder must never crash the panel
            self._status(f"could not export that pack ({exc})")
            return
        self._status(message)

    def _preview_design(self) -> str:
        """The design the strip draws: the previewed pack's, while previewing one.

        A pack is only ever drawn by the `image` design, so previewing one has to
        show `image` whatever the combo above currently says \u2014 otherwise
        Preview would look as if it did nothing at all to someone whose bubble
        draws an orb, which is precisely the "nothing applies" complaint this
        card exists to answer rather than repeat.
        """
        if self._preview_art:
            return "image"
        return str(self.design_combo.currentData() or "orb")

    def _preview_picture(self, state: str = "") -> str:
        """The picture the strip draws for `state`: the previewed pack's, or yours."""
        art = self._preview_art
        if art:
            return str((art.get("states") or {}).get(state)
                       or art.get("any") or "")
        return self._design_picture(state)

    def _preview_deco(self) -> tuple:
        """The decoration the strip should draw: `(name, colour value)`.

        The colour value is exactly what `_collect` would WRITE — the literal
        hex when the row says the colour is the user's own, the mode word
        otherwise — so the strip shows the pending choice rather than the saved
        one, and the bubble's own resolver can be handed it directly.
        """
        mode = str(self.deco_colour_combo.currentData() or "state")
        value = self._deco_colour if mode == self.DECO_COLOUR_CUSTOM else mode
        return (str(self.deco_combo.currentData() or "off"), str(value))

    def _preview_design_pack(self, kind: str) -> None:
        """Show a pack folder or pack FILE in the strip WITHOUT installing it.

        The candidate is read by the bubble module (`inspect_pack`) and drawn by
        the same strip, through the same resolver and the same decode and tint,
        so what is shown is what an install would give you. A pack that cannot be
        read is refused here in the sentence an INSTALL would use, because a
        preview more forgiving than the install would be showing you something
        you cannot have.
        """
        try:
            bubble = _core_module("bubble")
        except Exception:
            self._status("design packs are unavailable in this install")
            return
        if kind == "file":
            chosen, _chosen_filter = QFileDialog.getOpenFileName(
                self, "Choose a pack file to preview", str(Path.home()),
                "Handsoff pack (*.hpack);;Zip archive (*.zip);;All files (*)")
        else:
            chosen = QFileDialog.getExistingDirectory(
                self, "Choose a pack folder to preview", str(Path.home()))
        if not chosen:
            return                  # cancelled: not an edit
        try:
            art, problem, scratch = bubble.inspect_pack(chosen)
        except Exception as exc:    # a data folder must never crash the panel
            self._status(f"could not read that pack ({exc})")
            return
        self._drop_pack_preview()   # a second preview replaces the first
        if problem or not art:
            sentence = problem or "that pack cannot be read"
            self._status(sentence)
            self.preview_label.setText(f"\u26a0 {sentence}")
            self.preview_label.setVisible(True)
            return
        self._preview_art = art
        self._preview_source = chosen
        self._preview_kind = kind
        self._preview_scratch = scratch
        self._refresh_preview_label()
        self._toggle_preview_buttons()
        self._start_live_pack_preview()
        self._status(f"previewing {art['name']} \u2014 nothing installed yet")

    def _try_design_pack(self) -> None:
        """Install the previewed pack and switch to it.

        A preview is not a second install path: this hands the SAME source to the
        same function Install pack\u2026 uses, so what was shown is what lands, and
        it is COPIED into the install \u2014 which is what keeps it working after
        the folder or file it came from moves.
        """
        source, kind = self._preview_source, self._preview_kind
        if not source:
            return
        try:
            bubble = _core_module("bubble")
        except Exception:
            self._status("design packs are unavailable in this install")
            return
        try:
            slug, message = (bubble.install_pack_file(source) if kind == "file"
                             else bubble.install_pack(source))
        except Exception as exc:    # a data folder must never crash the panel
            self._status(f"could not install that pack ({exc})")
            return
        self._drop_pack_preview()
        self._adopt_design_pack(slug, message)

    def _cancel_design_pack_preview(self) -> None:
        """Stop previewing: nothing was installed, and the strip goes back."""
        if not self._preview_art:
            return
        self._drop_pack_preview()
        self._status("preview cancelled \u2014 nothing was installed")

    def _drop_pack_preview(self) -> None:
        """Forget the previewed pack and remove the art it had to unpack.

        Nothing else owns that temporary folder, so it goes here \u2014 on a
        cancel, on Try it, on a second preview, on any other pack action, and on
        close \u2014 and the strip's decode cache goes with it, because a cached
        decode of the candidate's pictures would otherwise outlive the preview
        and let the strip draw a pack that is no longer being previewed.
        """
        self._stop_live_pack_preview()
        scratch = str(getattr(self, "_preview_scratch", "") or "")
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        self._preview_scratch = ""
        self._preview_art = None
        self._preview_source = ""
        self._preview_kind = ""
        preview = getattr(self, "preview", None)
        if preview is not None:
            preview.drop_cached_art()
        self._refresh_preview_label()
        self._toggle_preview_buttons()

    def _start_live_pack_preview(self) -> None:
        """Draw the candidate on the real bubble too, and keep drawing it.

        The strip answers "what does it look like"; the desktop answers "how
        does it look HERE" — against the wallpaper, at the size this bubble
        really is. The bubble keeps the candidate in memory with a short
        deadline, so the heartbeat IS the mechanism: the first beat starts the
        preview and every later one renews it, which is why a start and a
        keep-alive are one code path instead of two that could disagree.
        """
        path = str(self._preview_scratch or self._preview_source or "")
        if not path:
            return
        self._preview_timer.start()
        self._tell_bubble_preview(f"preview-pack {path}", note=True)

    def _renew_live_pack_preview(self) -> None:
        """(Heartbeat) Offer the candidate again before its deadline runs out."""
        if not self._preview_art:
            self._preview_timer.stop()   # nothing is being previewed any more
            return
        path = str(self._preview_scratch or self._preview_source or "")
        if path:
            self._tell_bubble_preview(f"preview-pack {path}", note=True)

    def _stop_live_pack_preview(self) -> None:
        """Take the candidate off the bubble, stopping the heartbeat FIRST.

        The heartbeat goes first so a renewal already in flight cannot resurrect
        what this clears. One can still land after the clear — both sends are off
        the GUI thread — and that is bounded on purpose: the bubble drops any
        preview nobody renews, so the worst case is a few more seconds of the
        candidate rather than a bubble stuck showing it.
        """
        timer = getattr(self, "_preview_timer", None)
        if timer is not None:
            timer.stop()
        self._preview_live_note = ""
        self._tell_bubble_preview("preview-clear")

    def _tell_bubble_preview(self, payload: str, note: bool = False) -> None:
        """One preview command to the running bubble, off the GUI thread.

        It is a socket round trip with a timeout, and a wedged bubble must not
        freeze a window that may be trying to close. `note` is False for the
        clear: its reply is about a preview that is already gone, and a stale
        note would be shown against the NEXT preview.
        """
        sock = getattr(H, "CONTROL_SOCK", None)
        done = self._note_live_pack_preview if note else (lambda *_a: None)
        self.run_bg(lambda: _socket_command(sock, payload, timeout=1.5), done)

    def _note_live_pack_preview(self, ok, result) -> None:
        """Record whether the DESKTOP is really showing the candidate.

        The label must not claim it is when the bubble is not running: "drawn on
        the bubble" and "only this window shows it" are different facts, and
        telling them apart is the whole reason the preview is pushed live.
        """
        if not self._preview_art:
            return     # the preview was dropped while this reply was in flight
        self._preview_live_note = (
            "\u00b7 drawn on the bubble" if ok and result is not None else
            "\u00b7 the bubble is not running, so only this window shows it")
        self._refresh_preview_label()

    def _refresh_preview_label(self) -> None:
        """Say what is being previewed, and that it is NOT installed yet."""
        label = getattr(self, "preview_label", None)
        if label is None:
            return
        art = self._preview_art
        if not art:
            label.setVisible(False)
            return
        label.setVisible(True)
        label.setText(
            f"Previewing {art['name']} from {self._preview_source} \u2014 "
            f"{len(art.get('states') or {})} state picture(s)"
            + (", plus a fallback" if art.get("any") else "")
            + ". Not installed \u2014 Try it to keep it."
            + (f" {self._preview_live_note}" if self._preview_live_note else ""))

    def _toggle_preview_buttons(self) -> None:
        """Try it and Cancel exist only while there is something to decide."""
        showing = bool(self._preview_art)
        for name in ("preview_try", "preview_cancel"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(showing)

    def _refresh_design_image_label(self) -> None:
        """What the art in effect is \u2014 or WHY it cannot be drawn.

        A pack is the authority when one is selected, so the label describes the
        pack and then the single picture. Each explanation is the bubble
        module's own (`pack_problem` / `design_image_problem`), the sentences
        `doctor` prints and the reasons the bubble draws its empty slot, so the
        panel and the desktop describe one thing with one wording. The path is
        shown in full rather than as a file name: a truncated path is how "it
        points somewhere I did not mean" stays invisible.
        """
        pack = str(self._design_pack or "")
        if pack:
            self.image_label.setText(
                f"A pack is selected ({pack}) \u2014 it draws the pictures, and "
                f"the file above is not used while it is.")
            return
        own = sum(1 for state, _key in STATE_IMAGE_KEYS
                  if self._design_images.get(state))
        every = bool(STATE_IMAGE_KEYS) and own == len(STATE_IMAGE_KEYS)
        path = str(self._design_image or "")
        if not path:
            self.image_label.setText(
                "No fallback picture \u2014 every state has its own picture above."
                if every else
                "No fallback picture \u2014 a state with no picture of its own "
                "draws the empty slot.")
            return
        problem = ""
        try:
            problem = _core_module("bubble").design_image_problem(path)
        except Exception:
            log.debug("design image check failed", exc_info=True)
        if problem:
            self.image_label.setText(f"\u26a0 {problem}")
        elif every:
            # Set but not in use: saying so is the difference between "this file
            # is ignored" and "this file is broken", and clearing one state's
            # picture brings it straight back.
            self.image_label.setText(
                f"{path} \u2014 unused while every state has its own picture.")
        else:
            self.image_label.setText(path)

    def _apply_appearance_live(self) -> None:
        """Write the Appearance values to settings.json and notify the bubble.

        Skips the write when the disk already holds these values, which is how
        a plain window load (where _load_values sets the widgets from disk) is
        told apart from a real edit — no separate 'loading' flag to get stuck.
        """
        try:
            self._collect()
        except Exception:            # a half-built form must not raise here
            log.exception("live appearance apply: could not collect settings")
            return
        wanted = {k: self.cfg.get(k) for k in self.APPEARANCE_KEYS}
        # Compared against the disk through the SAME defaults-and-coercion path a
        # load uses, not against the raw JSON. A settings.json written before a
        # key existed has no entry for it, and `raw.get(k)` then differs from the
        # widget's default for a reason that is not an edit — so a plain reload
        # announced "Applied live: shape, size, ..." and rewrote the file, a
        # message describing an edit nobody made.
        try:
            on_disk = merge_settings(
                json.loads(H.SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            on_disk = {}
        changed = [k for k in self.APPEARANCE_KEYS
                   if on_disk.get(k) != wanted.get(k)]
        if not changed:
            return                   # nothing changed: this was a load, not an edit
        if self._save_reported():
            # name what moved instead of always reporting the shape: the old
            # message made a colour or size change look like it had not been
            # taken (and hid the fact that it never was). A change that lands
            # exactly on a catalogue look says WHICH look, derived from the
            # saved values rather than remembered from the click — so a hand
            # tuned set that happens to match also reports itself as that look.
            # Named by the contract too: each row carries the words a live
            # apply uses, including one per state ("idle picture" says what
            # moved; "design_image_idle" does not).
            labeller = getattr(SCHEMA, "field_label", None)
            pretty = {k: (labeller(k) if labeller else k) for k in changed}
            name = self._current_look()
            entry = (SCHEMA.look(name) if name and hasattr(SCHEMA, "look")
                     else None)
            head = f"Applied look {entry['label']}" if entry else "Applied live"
            self._status(head + ": "
                         + ", ".join(pretty.get(k, k) for k in changed)
                         + ". No restart needed.")

    # ----------------------------------------------------------------- startup

    def _startup_tab(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        # The card is the table's; its one row is drawn here, and says so: its
        # STATE comes from the niri config (`autostart_enabled()`), not from the
        # setting, because the setting is what was ASKED for and the config is
        # what the machine will do — a row showing the stored value would
        # describe something that may not be true.
        for group in _page_groups("startup"):
            card = QGroupBox(_group_title("startup", group), w)
            card_lay = QVBoxLayout(card)
            form = QFormLayout()
            card_lay.addLayout(form)
            self._draw_group("startup", group, _Rows(self, form, "form"))
            lay.addWidget(card)

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

    def _render_autostart(self, rows: "_Rows", field) -> None:
        """The autostart switch, whose state is the CONFIG's, not the setting's.

        The one Startup row the table cannot build: its checked state is read from
        the niri config (and systemd), not from settings.json, so this widget is
        wired to the DESKTOP rather than to the file.
        """
        self.autostart_chk = QCheckBox(field.title, self)
        self.autostart_chk.setToolTip(field.tip)
        self.autostart_chk.setChecked(autostart_enabled())
        rows.target.addRow(self.autostart_chk)
        note = QLabel(
            f"Adds one spawn-at-startup line to {NIRI_CONFIG}\n"
            "(a backup is written next to it before the first change).", self)
        note.setWordWrap(True)
        rows.target.addRow(note)

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
            if self._user_edited:
                # the user has unsaved edits: reloading now would wipe them.
                # Save still wins via the three-way merge; say so.
                self._status(
                    "settings changed on disk — keeping your edits; "
                    "Save will merge both")
                return
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
        self._user_edited = False
        self._load_values()
        # The disk is now the truth for which model the stored history belongs
        # to. Without this a later unrelated save compared against the model
        # from window-open and looked like a model SWITCH — which used to be a
        # harmless backup but now clears the conversation.
        self._model_at_open = str(self.cfg.get("model") or "")

    def _load_values(self) -> None:
        # EVERY control the table draws this window, in one loop: this is the
        # whole load path for a generated control, which is why adding a setting
        # needs no line here. A load is not an edit — the live apply compares
        # against the disk — so writing these values through their own signals
        # is safe, and it is what keeps a paired label (px, ×, %) in step.
        for key, control in self._controls.items():
            control.write(self.cfg.get(key, DEFAULT_SETTINGS.get(key)))
        pol = self.cfg.get("command_policy") or {}
        if isinstance(pol, dict) and self.policy_rows:
            for name, combo in self.policy_rows.items():
                idx = combo.findData(str(pol.get(name, "ALLOW")).upper())
                combo.setCurrentIndex(idx if idx >= 0 else 0)
        elif isinstance(pol, dict) and pol and getattr(self, "policy_edit", None) is not None:
            self.policy_edit.setPlainText(
                "\n".join(f"{k} = {v}" for k, v in sorted(pol.items())))
        _di = self.design_combo.findData(str(self.cfg.get("bubble_design", "orb")))
        self.design_combo.setCurrentIndex(_di if _di >= 0 else 0)
        self._design_image = str(self.cfg.get("design_image_path") or "")
        self._design_images = {
            state: str(self.cfg.get(key) or "")
            for state, key in STATE_IMAGE_KEYS}
        self._design_pack = str(self.cfg.get("design_pack") or "")
        _av = self.deco_combo.findData(
            str(self.cfg.get("avatar_ring") or "ring-light"))
        self.deco_combo.setCurrentIndex(_av if _av >= 0 else 0)
        # The colour row is derived from the stored value: a word selects that
        # word, anything else is a colour of its own and selects `custom` — so
        # the row a user sees always describes what is on disk, and saving
        # without touching it is not an edit.
        self._deco_colour = self._stored_deco_colour()
        _stored = str(self.cfg.get("avatar_deco_color") or "state").strip().lower()
        # A word the schema declares selects its own row; anything else is a
        # colour of its own. Spelling the two words out here meant a third one
        # the schema offered showed up as "custom" — the row describing
        # something other than what is on disk, which is the defect this whole
        # card family is about.
        _want = (_stored if _stored in _deco_colour_words()
                 else self.DECO_COLOUR_CUSTOM)
        _dci = self.deco_colour_combo.findData(_want)
        self.deco_colour_combo.setCurrentIndex(_dci if _dci >= 0 else 0)
        self._update_deco_colour()
        # `avatar_tint` is a GENERATED row now, so the loop over `self._controls`
        # above has already loaded it — the hand-written line that used to sit
        # here existed only because the row was drawn by name.
        self._update_deco_hint()
        self._refresh_state_image_buttons()
        self._refresh_pack_combo()
        self._refresh_design_image_label()
        self.refresh_tts()
        aliases = self.cfg.get("workspace_aliases") or {}
        self.alias_edit.setPlainText(
            "\n".join(f"{k} = {v}" for k, v in sorted(aliases.items())))
        for key, chk in self.perm_checks.items():
            chk.setChecked(bool(self.cfg["permissions"].get(key, True)))
        # `self.cfg` is left as read: `_collect` is what writes the cleaned
        # palette (it always did), and mutating cfg here would leave it
        # disagreeing with `_loaded_cfg` — a reload would then look like an
        # unsaved edit the moment a malformed colour was refused.
        rejected = self._adopt_colors(self.cfg.get("colors"))
        self._paint_color_buttons()
        if rejected:
            self._report_rejected_colors(rejected, "using the default")
        # last: the tick must reflect the values just loaded from disk
        self._refresh_look_buttons()

    # What each decoration IS, in the panel's own words: the picker names them,
    # and this says what you are choosing between without opening the bubble to
    # find out. One dict beside the picker that fills it, so a new decoration
    # cannot ship with a name and no description.
    DECO_HINTS = {
        "off": "Nothing around the picture.",
        "ring-light": "Two arc pairs that rotate, with a dimmer counter-rotating "
                      "pair outside them — a lamp ring.",
        "orbit": "Three glowing comets on their own orbits, each trailing its "
                 "own light.",
        "pulse": "Rings that travel outward and fade — a heartbeat. Speaks "
                 "faster when you do.",
        "aurora": "Three ribbons of light drifting around the edge in slow "
                  "waves.",
        "rainbow": "One thick band holding every hue at once, turning — the "
                   "whole wheel rather than a colour.",
        "sparkle": "Points that pop and fade all round a faint ring, each on "
                   "its own beat.",
        "comet": "A single comet with a long tail sweeping the ring — motion "
                 "you read at a glance.",
        "neon": "A segmented tube that is always lit, with a pulse chasing "
                "round it.",
        "flames": "Tongues of fire licking up all round, running white-hot at "
                  "the tips when you talk.",
    }

    # The decoration's own colour. `custom` is not a settings value: it is the
    # ROW's name for "the stored value is a literal hex", which is how one combo
    # can offer two words and an arbitrary colour without a second dialog open
    # by default. Keeping it out of the schema is deliberate — the schema holds
    # what can be STORED, and what is stored is the hex.
    DECO_COLOUR_CUSTOM = "custom"

    def _update_deco_hint(self) -> None:
        """Say what the chosen decoration does, in the panel's own words."""
        name = str(self.deco_combo.currentData() or "off")
        self.deco_hint.setText(self.DECO_HINTS.get(name, ""))
        if getattr(self, "deco_colour_combo", None) is not None:
            self._update_deco_colour()

    def _stored_deco_colour(self) -> str:
        """The colour to open the swatch on.

        `avatar_deco_color` holds either a mode word or a literal hex, so the
        swatch is seeded from the hex when there is one and from the first
        state colour otherwise. `_color_ok` is the panel's own gate, so a
        hand-edited settings.json cannot seed the swatch with junk the bubble
        would refuse.
        """
        raw = str(self.cfg.get("avatar_deco_color") or "").strip()
        # Every word the schema accepts, plus the empty value, is NOT a colour
        # of its own — the swatch would otherwise open on the word itself.
        if raw.lower() not in _deco_colour_words() + ("",) and self._color_ok(raw):
            return raw
        states = tuple(getattr(SCHEMA, "BUBBLE_STATES", ())) or ("idle",)
        return str(DEFAULT_SETTINGS["colors"].get(states[0]) or "#4f8cff")

    def _update_deco_colour(self) -> None:
        """Enable the swatch only when it is the thing being used.

        A live control that does nothing is the "I changed it and nothing
        applied" defect in miniature, so the swatch is disabled unless the row
        says the stored value is a colour of its own.
        """
        mode = str(self.deco_colour_combo.currentData() or "state")
        custom = mode == self.DECO_COLOUR_CUSTOM
        self.deco_colour_btn.setEnabled(custom)
        if custom:
            self._paint_deco_colour_button()
        # The strip above draws the ring, so a colour change has to repaint it —
        # the same reason every other appearance control does.
        if getattr(self, "preview", None) is not None:
            self.preview.update()

    def _paint_deco_colour_button(self) -> None:
        hexcol = self._deco_colour
        col = QColor(hexcol)
        luma = (0.299 * col.red() + 0.587 * col.green()
                + 0.114 * col.blue()) / 255.0
        ink = "#101014" if luma > 0.6 else "#ffffff"
        self.deco_colour_btn.setText(hexcol.upper())
        self.deco_colour_btn.setStyleSheet(
            f"QPushButton {{ background-color: {hexcol}; color: {ink}; "
            f"border: 1px solid #555; border-radius: 6px; }}"
            f"QPushButton:hover {{ border: 1px solid {ink}; }}")
        self.deco_colour_btn.setToolTip(
            f"The decoration's own colour ({hexcol.upper()}). The bubble's rim "
            f"still carries the state colour, so the mood stays readable.")

    def _pick_deco_colour(self) -> None:
        col = QColorDialog.getColor(QColor(self._deco_colour), self,
                                    "decoration colour")
        if not col.isValid():
            return
        chosen = col.name()
        if not self._color_ok(chosen):
            # The dialog yields '#rrggbb', so this guards the invariant rather
            # than an expected path: nothing may enter the row that the
            # bubble's parser or core.settings would throw away.
            self._report_rejected_colors(
                [("decoration", chosen)], "keeping the current colour")
            return
        self._deco_colour = chosen
        self._paint_deco_colour_button()
        self.preview.update()
        self._schedule_appearance_live()

    def _collect(self) -> list[str]:
        problems: list[str] = []
        # Every generated control, in one loop — the whole save path for a
        # setting the table draws. `coerce_setting` then answers "what does this
        # BECOME" with the loader's own rule, so what is written here is what a
        # reload would keep: an emptied server URL becomes the default instead
        # of an empty string the loader has to repair, a number outside its
        # declared range is clamped now rather than on the next start, and a
        # string in a numeric field never reaches the disk at all. It is the
        # same function the bubble's runtime writers use, so the window cannot
        # disagree with them about what a value means.
        for key, control in self._controls.items():
            self.cfg[key] = _core_module("settings").coerce_setting(
                key, control.read())
        self.cfg["model"] = self._selected_model() or self.cfg["model"]
        self.cfg["mic_device"] = self.mic_combo.currentData() or ""
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
        self.cfg["bubble_design"] = self.design_combo.currentData() or "orb"
        self.cfg["design_image_path"] = str(self._design_image or "")
        for state, key in STATE_IMAGE_KEYS:
            self.cfg[key] = str(self._design_images.get(state) or "")
        self.cfg["design_pack"] = str(self._design_pack or "")
        self.cfg["avatar_ring"] = str(
            self.deco_combo.currentData() or "ring-light")
        # One key, three shapes: the two words, or the literal hex when the row
        # says the colour is the user's own. `custom` itself is never stored —
        # the schema holds what the bubble can READ, and what it reads is the
        # hex.
        _mode = str(self.deco_colour_combo.currentData() or "state")
        self.cfg["avatar_deco_color"] = (
            self._deco_colour if _mode == self.DECO_COLOUR_CUSTOM else _mode)
        # Filled FROM the loaded dict before the swatches are applied, for the
        # same reason `permissions` is: a colour this row has no swatch for must
        # keep the value it had. A key missing from the candidate is a DELETE to
        # the merge, and `_coerce_colors` then fills it from the defaults — the
        # silent re-default this file has already been bitten by once.
        colors = {str(k): str(v) for k, v in (self.cfg.get("colors") or {}).items()}
        colors.update(self._colors)
        self.cfg["colors"] = colors
        # Filled FROM the loaded dict, so a key this tab has no widget for keeps
        # the value it had instead of disappearing — a dropped key is a
        # permission silently re-defaulted (see `_permissions_tab`).
        permissions = dict(self.cfg.get("permissions") or {})
        permissions.update({k: chk.isChecked() for k, chk in self.perm_checks.items()})
        self.cfg["permissions"] = permissions
        policy_map: dict[str, str] = {}
        if self.policy_rows:
            # Start FROM the loaded map, then let the rows speak: an entry this
            # window has no row for (a tool the registry no longer declares, or
            # a name written by hand) must survive the save, because a key
            # missing from the candidate is a DELETE to the three-way merge.
            # The rows still keep settings.json minimal — ALLOW is the default,
            # so a row set to ALLOW is stored as no entry at all.
            policy_map = {str(k): str(v).strip().upper()
                          for k, v in (self.cfg.get("command_policy") or {}).items()}
            for name, combo in self.policy_rows.items():
                rule = str(combo.currentData() or "ALLOW")
                if rule == "ALLOW":
                    policy_map.pop(name, None)
                else:
                    policy_map[name] = rule
        elif getattr(self, "policy_edit", None) is not None:
            for line in self.policy_edit.toPlainText().splitlines():
                if "=" not in line or line.strip().startswith("#"):
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().upper()
                if k and v in ("ALLOW", "DENY", "CONFIRM"):
                    policy_map[k] = v
        self.cfg["command_policy"] = policy_map

        # `extra_allowed_commands` needs no line: it is a `lines` control from
        # the table, so the loop above read it AND capped it at the declared cap.
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
            cleared_note = self._clear_history_for_model_switch()
            self._model_at_open = new_model
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
        self._user_edited = False   # form now matches disk
        # One autostart owner, same rule as the installer: when the systemd
        # user unit manages the bubble, niri spawn-at-startup is NOT added.
        msg = apply_autostart(self.autostart_chk.isChecked())
        warn = f"  WARNINGS: {'; '.join(problems)}" if problems else ""
        live = self._notify_bubble_reloaded()
        live_note = " Applied live." if live else " Bubble unreachable — restart to apply."
        self._status(f"Saved to {H.SETTINGS_FILE}. {msg}{cleared_note}{warn}{live_note}")
        return True

    def _wipe_history(self, tag: str) -> str:
        """Back up, empty and REALLY clear the stored conversation.

        Truncating the file is only half the job. The running bubble holds the
        transcript in `_history` and rewrites the whole file on its next turn,
        so a clear that only touches the file is resurrected seconds later.
        The socket call is what makes it stick; it is best-effort, and the
        returned note says which of the two actually happened.
        """
        try:
            if H.HISTORY_FILE.exists():
                _backup_keep_n(H.HISTORY_FILE, tag)
            _core_module("settings").atomic_private_write(
                H.HISTORY_FILE, "[]")                        # empty JSON list
        except OSError as e:
            return f"could not clear history: {e}"
        reply = self._clear_bubble_history()
        if reply is None:
            return "backup saved; bubble not running (it will start clean)"
        if reply.startswith("ok"):
            return "backup saved; the running bubble was told too"
        return ("saved to disk only — the running bubble refused: "
                f"{reply.strip()}")

    def _clear_history_for_model_switch(self) -> str:
        """Clear what a new model must not inherit.

        This used to take a backup and leave the transcript in place, so the
        new model read exactly the history the comment at the call site blames
        for parroting.
        """
        return "  Memory cleared for the new model (" + self._wipe_history(
            "bak-modelswitch") + ")."

    @staticmethod
    def _clear_bubble_history() -> "str | None":
        """Ask the running bubble to drop its in-memory conversation.

        Best-effort like _notify_bubble_reloaded: None means the bubble is not
        running, in which case the truncated file IS the cleared state."""
        return _socket_command(H.CONTROL_SOCK, "clear-history", timeout=3.0)

    @staticmethod
    def _notify_bubble_reloaded() -> bool:
        """Ask the running bubble to apply settings.json without restart.

        Best-effort: False when the bubble is dead (its restart path covers
        that case) — a notify failure must never fail the save itself."""
        sock = None
        try:
            import socket as _socket
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(str(H.CONTROL_SOCK))
            # This is the request that makes the bubble re-read settings.json,
            # so it carries the capability token like any other verb that
            # changes state — otherwise the save would appear to succeed and
            # the running bubble would keep the old values.
            sock.sendall(_control_request_bytes("reload-settings"))
            sock.settimeout(5.0)
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = sock.recv(256)
                if not chunk:
                    break
                reply += chunk
            return reply.decode("utf-8", "replace").startswith("ok")
        except Exception:
            return False
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    def _on_save(self) -> None:
        self._save_reported()

    def _on_apply(self) -> None:
        if self._save_reported():
            self._on_restart_bubble()

    def _save_reported(self) -> bool:
        """`save()` with its failure MADE VISIBLE.

        The status label is the only place this window speaks, and an exception
        escaping a Qt slot is printed to a stderr nobody sees when the window is
        launched from the bubble's menu — which is exactly the report "I pressed
        Save and nothing happened". Every Qt entry point (Save, Save & restart,
        and the Appearance live apply) goes through here.
        """
        try:
            return self.save()
        except Exception as e:      # noqa: BLE001 - a save must not die silently
            log.exception("settings save failed")
            self._status(f"could not save settings: {e}")
            return False


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
    win.refresh_tts()
    QTimer.singleShot(4000, app.quit)
    app.exec()
    print(f"models listed: {win.model_list.count()}, "
          f"mics: {win.mic_combo.count()}, "
          f"tts: {win.tts_ref_status.text()}")
    ok = win.save()
    print("settings.json written:", H.SETTINGS_FILE, "->", ok)
    return 0 if ok and win.model_list.count() > 0 else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv else main())
