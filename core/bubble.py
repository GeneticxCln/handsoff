"""The bubble window: mask geometry, the state palette, and the 13 designs.

Extracted from `handsoff.py`. This module OWNS the appearance state — the
window size and the geometry derived from it, the per-state palette, and the
two look knobs (accent, animation energy). The host injects the few things this
module must not reach for itself (`SETTINGS`, the state names, `notify`, the
paths its context menu acts on) right after loading it, the same way
`core/tools.py` takes its host.

`configure()` is the single place the appearance is derived from settings, so a
settings save and a cold start cannot disagree about the geometry.

Application-free by injection: nothing here imports or reaches into handsoff.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QImage,
    QImageReader,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# The tree's ONE hex parser. Imported rather than re-spelled so `#4f8cffXYZ` is
# refused identically here, in `core.settings`, at the swatch and in doctor.
# `theme` is Qt-free, so this cannot make the bubble depend on a display.
from .theme import hex_to_rgb

# --------------------------------------------------------------- injected host
# Set by handsoff.py immediately after this module loads (the same shape as
# core.tools' `time` / `log` / `_DEFAULT_DEPS` injection). The defaults below
# exist so the module is importable on its own, not so anything should rely on
# them.
SETTINGS: dict = {}
APP_NAME = "handsoff"
# Same logger name as the bubble's, so a preview that expires or is refused
# lands in handsoff.log beside everything else (see core/audio.py).
log = logging.getLogger("handsoff")
SETTINGS_APP = None
RESTART_SCRIPT = None
IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"


def notify(*_args, **_kwargs) -> None:
    """Replaced by the host's notify(); inert until then."""
    return None


# ------------------------------------------------------------- appearance state
HOLD_MS = 140                  # press-and-hold threshold before recording starts
DRAG_PX = 14                   # movement before a press becomes a drag

def _state_color(key: str, fallback: str, palette: dict | None = None) -> QColor:
    """A state colour out of `palette`, or the fallback.

    `palette` is explicit because `configure(settings)` accepts a settings dict:
    reading the module global here would quietly ignore the dict it was handed
    and build the palette from whatever the app last injected.
    """
    src = (SETTINGS.get("colors") or {}) if palette is None else palette
    c = QColor(src.get(key, fallback))
    return c if c.isValid() else QColor(fallback)


# Dark Siri-style palette: these are the *swirl glow* hues around the dark orb.
STATE_COLORS: dict = {}          # rebuilt by configure()

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


WINDOW_PX = 0                    # set by configure()
# Appearance-tab live knobs: accent punch (0..1) and global animation energy
# (0.2..2.0).  Defaults are the neutral values, so an old settings.json that
# predates these keys keeps rendering exactly as before.
BUBBLE_ACCENT = 0.5         # set by configure()
ANIM_ENERGY = 1.0           # set by configure()
BUBBLE_R0 = 0.0                  # idle bubble radius; configure()
GLOW_PAD = 0.0                   # glow ring; configure()
GEOM_K = 0.0                     # radius scale; configure()
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
APERTURE_R = 0.0                 # set by configure()


def configure(settings: dict | None = None) -> None:
    """(Re)derive the window size, the geometry, the palette and the look knobs.

    THE one place the appearance comes from settings. Startup and a live
    settings save both come through here, and that is the point: they used to
    diverge. The live path resized the window and rebuilt the palette while
    `APERTURE_R` kept whatever the module was imported with, so a size change
    left the apertures sized for the old window.
    """
    global WINDOW_PX, BUBBLE_R0, GLOW_PAD, GEOM_K, APERTURE_R
    global BUBBLE_ACCENT, ANIM_ENERGY
    src = SETTINGS if settings is None else settings
    try:
        WINDOW_PX = min(192, max(96, int(src.get("bubble_size", WINDOW_PX or 144))))
    except (TypeError, ValueError):
        WINDOW_PX = WINDOW_PX or 144
    BUBBLE_R0 = WINDOW_PX * 44.0 / 128.0   # idle bubble radius
    GLOW_PAD = WINDOW_PX * 7.0 / 128.0     # glow ring; fits inside the mask
    GEOM_K = WINDOW_PX / 128.0             # scale for all radius offsets
    APERTURE_R = WINDOW_PX / 2.0 - 1.0
    for attr, default, lo, hi in (("bubble_accent", 0.5, 0.0, 1.0),
                                  ("animation_energy", 1.0, 0.2, 2.0)):
        try:
            value = min(hi, max(lo, float(src.get(attr, default))))
        except (TypeError, ValueError):
            value = default
        if attr == "bubble_accent":
            BUBBLE_ACCENT = value
        else:
            ANIM_ENERGY = value
    colors = src.get("colors") or {}
    for key, fallback in (("idle", "#2f6fed"), ("listening", "#e0435c"),
                          ("thinking", "#c8781f"), ("speaking", "#1fae62")):
        STATE_COLORS[key] = _state_color(key, fallback, colors)


# ------------------------------------------------------- the `image` design
# `image` is a design whose art is the user's own file. These rules are what
# make an imported picture behave like a painted design instead of a sticker
# pasted over one, and every one of them is pinned by a scenario in
# tests/test_settings_gui.py:
#
#   fit     the image's CIRCUMSCRIBED circle is fitted inside the aperture, so
#           the voice can rotate it as hard as it likes without pushing a corner
#           past the glass — a circle does not change under rotation. That makes
#           the ink guard pass by construction instead of by luck, and it is why
#           a square image is not simply scaled to the diameter.
#   colour  the state colour owns every opaque pixel: the image contributes its
#           silhouette (alpha) and its luminance (shading) through a 0.45 floor,
#           so a dark picture cannot make the state colour invisible — that is
#           the `void` defect (8 visible px of 45 796) wearing a user's file,
#           and the Appearance colour picker has to keep meaning something.
#   voice   its own reaction, neutral at silence like every other painter.
#   no file a dashed frame in the state colour, NEVER a fall back to the orb: an
#           orb there is indistinguishable from a design name with no dispatch
#           branch, which is a bug this tree has already shipped once.
#
# The art may come from one file (`design_image_path`) or from a PACK — a folder
# with a manifest and one picture per state, installed from the Appearance tab,
# so a single choice switches several pictures together. See the pack section
# below; the resolution order lives in `picture_for()`.
#
# The picture cache: revision key -> {"image", "shade", "pixmap"}. A DICT rather
# than one slot, because with a pack the bubble draws a DIFFERENT FILE per state
# and a single slot would re-decode a photo every time the state changed.
#
# The cap must cover one state's whole ANIMATION, not just the four states: an
# animated pack is `PACK_MAX_FRAMES` distinct files per state, and a 4-slot FIFO
# cycles 16 keys through 4 slots — so every frame advance evicted the frame it
# was about to need, and a 6 fps animation re-decoded (load + scale + shade)
# several times a second, sustained, for a picture the still path never pays for.
# `PACK_MAX_FRAMES + the four state stills` keeps the animation resident while
# the other states' entries are the ones that rotate out; a guard pins the two
# constants together so this cannot drift below the pack limit.
_IMAGE_CACHE: dict = {}
_IMAGE_CACHE_MAX = 20
# (w, h) -> QImage: the feathered ellipse alpha mask `_round_avatar` applies to
# full-bleed pictures. Bounded in `_feather_mask`; sizes are few and stable.
_FEATHER_MASKS: dict = {}

# The working canvas for the picture, in pixels. A user's 3000x2000 photo is
# 24 MB, and this module builds a TINTED COPY of it per painted frame while the
# bubble repaints at 25-60 Hz — that is how a bubble becomes a space heater, and
# it is a memory ceiling that grows with whatever file someone points at. At the
# largest window the bubble allows (192 px) the drawn rect's diagonal is at most
# ~169 px, so 384 keeps the picture oversampled at every size while capping both
# the memory and the per-frame work at a constant.
_IMAGE_WORK = 384

# The widest raster a decode may ask for, as a backstop for the formats whose
# reader ignores a scaled decode (Qt checks the DECLARED size against this
# before touching the file). Qt's own default was 1024 MB — the ONLY ceiling an
# image bomb met, and a few hundred bytes of PNG can declare a raster that
# size. Half of that is far above any real photograph (a 50 MP frame is
# ~200 MB of ARGB32) and still halves the worst case.
_IMAGE_DECODE_LIMIT_MB = 512
if hasattr(QImageReader, "setAllocationLimit"):
    QImageReader.setAllocationLimit(_IMAGE_DECODE_LIMIT_MB)


def _decoded_image(path):
    """The file as a bounded ARGB32 image, or None when it will not decode.

    Downscaling happens HERE, at the source, rather than at every paint: that is
    what makes an enormous photo cost the same as a small one at every frame
    after the first, and what keeps the memory ceiling a constant instead of a
    function of whatever file someone points at (see `_IMAGE_WORK`).

    The decode itself is bounded, not just its result: `QImage(str(path))`
    materialised the file at its OWN dimensions first — so a few hundred bytes
    of PNG declaring a 30000x30000 raster was a ~3.6 GB ask, with Qt's
    allocation ceiling the only thing that said no — and the 20-slot cache
    re-decoded a pack's four states through that path. QImageReader scales at
    the source (where the format allows it) straight to `_IMAGE_WORK`, the
    largest canvas the widget ever paints, so a bomb decodes to the size it is
    drawn at and normal art is pixel-for-pixel the same shape it always was —
    only reached without the full-size intermediate.
    """
    try:
        p = Path(str(path)).expanduser()
        if not p.is_file():
            return None
    except (OSError, ValueError, TypeError):
        return None
    reader = QImageReader(str(p))
    reader.setAutoTransform(True)   # the EXIF orientation QImage() applied
    full = reader.size()            # header read, where the format allows one
    if full.isValid() and max(full.width(), full.height()) > _IMAGE_WORK:
        reader.setScaledSize(full.scaled(_IMAGE_WORK, _IMAGE_WORK,
                                         Qt.KeepAspectRatio))
    img = reader.read()
    if img.isNull():
        return None
    img = img.convertToFormat(QImage.Format_ARGB32)
    if max(img.width(), img.height()) > _IMAGE_WORK:
        # A format whose reader ignored ScaledSize: the old downscale at the
        # source is still the answer, and the allocation limit above is what
        # kept its decode from eating the machine.
        img = img.scaled(_IMAGE_WORK, _IMAGE_WORK, Qt.KeepAspectRatio,
                         Qt.SmoothTransformation)
        img = img.convertToFormat(QImage.Format_ARGB32)
    return img


def image_layers(path):
    """(image, luminance) for a file, or None when there is nothing to draw.

    The DECODE half of the `image` design, expressed as a function of a path
    rather than of the setting, because two processes need it: the bubble, and
    the settings app's preview. The preview keeps its own one-entry cache and
    calls this, so the picture the panel shows and the picture on the desktop
    cannot drift into two different ideas of one file.

    "Nothing to draw" deliberately covers BOTH a file that will not decode and
    one whose every pixel is transparent: a picture that renders as literally
    nothing is indistinguishable on the desktop from a design that failed to
    paint, so both come back as None and the painter draws its empty slot. The
    alpha scan is done once per revision here rather than once per frame.
    Neither layer depends on the state colour, so a caller may hold them across
    a colour change.
    """
    img = _decoded_image(path)
    if img is None or not _has_ink(img):
        return None
    shade = _shading_layer(img)
    if shade is None:
        return None
    return img, shade


def _image_entry(path):
    """The decoded layers for ONE revision of `path`, or None when unusable.

    Keyed by (path, mtime, size): the mtime is what makes editing the file on
    disk repaint without a restart, and a FAILED revision is remembered as an
    empty entry so an unreadable file costs one decode attempt rather than one
    per frame.

    The key carries the PATH, which is what makes this safe where a single-slot
    cache was not: a broken path returns None instead of whatever picture
    happened to be decoded last, so a moved file cannot leave stale art on
    screen. A repaint runs at 25-60 Hz, and re-reading a picture there is how a
    bubble becomes a disk hog.
    """
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except (OSError, ValueError, TypeError):
        return None
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    entry = {"image": None, "shade": None, "pixmap": None}
    layers = image_layers(raw)
    if layers is not None:
        entry["image"], entry["shade"] = layers
    while len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
    _IMAGE_CACHE[key] = entry
    return entry


def _entry_pixmap(entry):
    """`entry`'s QPixmap, built on FIRST request rather than at decode.

    The decode path must stay usable without a QGuiApplication (the pack tests
    run app-free over QImages only) — and `QPixmap.fromImage` aborts without
    one. The pixmap is a paint-path concern, so it is built here, on demand,
    where an app is guaranteed to exist.
    """
    if entry["pixmap"] is None and entry["image"] is not None:
        entry["pixmap"] = QPixmap.fromImage(entry["image"])
    return entry["pixmap"]


def design_image(state: str = "", t: float = 0.0):
    """The art in effect for `state` as a QPixmap, or None.

    An ANIMATION resolves its frame from `t` here — the same cache, decode and
    fit as a still, one frame at a time — so the painter never knows the
    difference beyond receiving a time.
    """
    return _design_art_pixmap(design_picture(state), t)


def _design_art_image(value, t: float = 0.0):
    """`value` (path or animation spec) as the QImage to draw now, or None.

    The app-free twin of `_design_art_pixmap`: same frame resolution, same
    cache, one step earlier — tests (and anything without a QGuiApplication)
    compare art as QImages, and the pixmap stays a paint-path concern.
    """
    if isinstance(value, dict):
        frames = value.get("frames") or []
        if not frames:
            return None
        fps = value.get("fps") or PACK_FPS_DEFAULT
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            fps = PACK_FPS_DEFAULT
        idx = int(t * max(0.5, fps)) % len(frames)
        value = frames[idx]
    entry = _image_entry(value)
    return None if entry is None else entry["image"]


def _design_art_pixmap(value, t: float = 0.0):
    """`value` (path or animation spec) as the QPixmap to draw now, or None."""
    if isinstance(value, dict):
        frames = value.get("frames") or []
        if not frames:
            return None
        fps = value.get("fps") or PACK_FPS_DEFAULT
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            fps = PACK_FPS_DEFAULT
        # Frame index from time; a period longer than any frame delay, so a
        # 0-fps spec cannot freeze or divide by zero (validated, but the paint
        # path never trusts a manifest twice).
        idx = int(t * max(0.5, fps)) % len(frames)
        value = frames[idx]
    entry = _image_entry(value)
    return None if entry is None else _entry_pixmap(entry)


def design_image_tinted(color: QColor, glow: float = 1.0,
                        energy: float = 0.12, level: float = 0.0,
                        state: str = "", t: float = 0.0):
    """The art in effect for `state`, in the state colour, or None.

    None is the ONE signal a painter uses to draw the empty slot instead, so
    "there is nothing to draw here" has a single cause and a single
    consequence — whether the cause is no picture at all, an unreadable one, or
    a pack with nothing for this state. `t` picks an animation's frame; a still
    ignores it.
    """
    value = design_picture(state)
    if isinstance(value, dict):
        frames = value.get("frames") or []
        if not frames:
            return None
        fps = value.get("fps") or PACK_FPS_DEFAULT
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            fps = PACK_FPS_DEFAULT
        idx = int(t * max(0.5, fps)) % len(frames)
        entry = _image_entry(frames[idx])
    else:
        entry = _image_entry(value)
    if entry is None or entry["image"] is None:
        return None
    if avatar_natural():
        # A natural render needs no luminance layer — the picture's own pixels
        # are the picture — so it cannot be blocked by one failing to compute.
        return tinted_image(entry["image"], entry["shade"], color,
                            glow, energy, level, natural=True)
    if entry["shade"] is None:
        return None
    return tinted_image(entry["image"], entry["shade"], color,
                        glow, energy, level)


def _shading_layer(img) -> QImage:
    """Luminance with the 0.45 floor — the layer that shades the state colour.

    SourceOver with white at 45% is exactly `v + 0.45 * (255 - v)`, which is the
    floor: no opaque pixel can come out darker than 45% of the state colour, so
    the picture can never hide which state the bubble is in. Computed once per
    file revision rather than per frame.
    """
    grey = img.convertToFormat(QImage.Format_Grayscale8)
    out = QImage(grey.size(), QImage.Format_ARGB32)
    if out.isNull():
        # A failed allocation: say so, because a QPainter over a null QImage is
        # undefined behaviour rather than an exception. Callers treat a missing
        # shade as "nothing to draw" and fall back to the empty slot.
        return None
    out.fill(Qt.transparent)
    q = QPainter(out)
    q.drawImage(0, 0, grey)
    q.setOpacity(0.45)
    q.fillRect(out.rect(), Qt.white)
    q.end()
    return out.convertToFormat(QImage.Format_Grayscale8)


def _has_ink(img) -> bool:
    """Whether any pixel is visible at all — read from the alpha channel.

    Byte-sliced rather than sampled: a 1-px figure in a 4000-px picture is
    exactly what a stride would miss, and bytes()[3::4] is the alpha plane of an
    ARGB32 image with no Python loop over pixels.
    """
    data = bytes(img.convertToFormat(QImage.Format_ARGB32).constBits())
    return len(data) >= 4 and max(data[3::4]) > 8


def design_image_problem(path: str | None = None) -> str:
    """Why the chosen image will not render, or "" when it will.

    This module is the only thing in the tree that decodes an image, so the
    sentence the settings picker shows and the reason the bubble falls back to
    its placeholder are the same sentence — two GUIs disagreeing about one file
    is the failure this shape avoids. An empty setting is not a problem (it is
    the state every install starts in); a *missing* file is a problem, because
    it means the bug is the path rather than the choice.
    """
    raw = str(path if path is not None
              else (SETTINGS.get("design_image_path") or "")).strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    try:
        if not p.exists():
            return f"no file at {p}"
        if not p.is_file():
            return f"{p} is a folder, not an image"
    except OSError as exc:
        return f"cannot read {p} ({exc.strerror or exc})"
    img = _decoded_image(p)
    if img is None:
        return (f"{p.name} is not an image this build can read "
                "(PNG, JPEG, WebP, GIF and SVG all work)")
    if not _has_ink(img):
        return f"every pixel of {p.name} is transparent — it would be invisible"
    return ""


# --------------------------------------------------- the `image` design's packs
# A PACK is a folder with a `pack.json` beside its pictures, one picture per
# state, so ONE choice switches several pictures together as the bubble changes
# state:
#
#     {"name": "Optimus",
#      "any": "base.png",
#      "states": {"idle": "idle.png", "listening": "listen.png",
#                 "thinking": "think.png", "speaking": "speak.png"}}
#
# `states` may name any subset of the four; a state it does not name uses `any`.
# A pack that leaves a state uncovered AND has no `any` is REPORTED and those
# states draw the empty slot — the same answer either way rather than a silent
# one. `any` is the ONE fallback key — one documented name rather than a list of
# synonyms to keep in step.
#
# `_validate_pack` is the single authority on whether a folder is usable, and
# all three consumers go through it: `install_pack` refuses to copy it,
# `load_pack` refuses to resolve it (so the bubble draws its empty slot rather
# than art assembled from the parts that happened to check out), and
# `pack_problem` NAMES it. Three implementations of "is this pack good" would
# drift, and the drift would be invisible — a pack half-drawn while doctor
# reports it fine.
#
# Installing COPIES the folder into the packs directory instead of referencing
# it: a pack keeps working when the folder it came from moves, and the bubble
# only ever reads one known tree. Installing over a pack of the same name moves
# the old one to `<slug>.previous` (one generation, replaced next time) rather
# than deleting it.
#
# Every name in a manifest resolves INSIDE its own folder and nowhere else: an
# absolute path or a `..` segment is refused rather than resolved. A pack is
# data someone else wrote — possibly someone else entirely — and it must not be
# able to point the bubble at arbitrary files, the same reason `read_file`
# carries a denylist.
PACK_MANIFEST = "pack.json"
PACK_STATES = (IDLE, LISTENING, THINKING, SPEAKING)
# An animation is a LIST of frames cycled by time; these bound what a pack may
# ask the painter to hold. 16 frames of a bounded decode is a constant memory
# ceiling; 0.5-30 fps covers "slow breathing" to "fast flap" and refuses junk
# (a string, 0, 1000) with one sentence instead of a division by zero.
PACK_MAX_FRAMES = 16
PACK_FPS_MIN = 0.5
PACK_FPS_MAX = 30.0
PACK_FPS_DEFAULT = 6.0
# The setting that holds one picture PER STATE, derived from the state names so
# a rename cannot desync the setting from the state it belongs to. The schema's
# `DESIGN_IMAGE_KEYS` is the same four names (it cannot import this module — the
# settings app loads it without Qt — and a test asserts the two agree).
STATE_IMAGE_KEY = {state: f"design_image_{state}" for state in PACK_STATES}
PACK_DIR_NAME = "design-packs"
PACKS_DIR = None        # set by the host; see packs_dir() for the fallback
_PACK_CACHE: dict = {}  # (slug, mtime_ns, size) -> manifest or None
_PACK_CACHE_MAX = 4
# A pack can also travel as ONE file, because a folder is not something you can
# send someone: the archive is a plain zip holding the same manifest and
# pictures, so it opens with any tool, and the extension only tells a desktop
# what to do with it. The content is the contract, never the suffix —
# `install_pack_file` accepts a `.zip` someone renamed to `.hpack` or the other
# way round, because a person who received a file should not have to fix its
# name before it works.
PACK_EXT = ".hpack"
# Hard caps on what an archive is allowed to become. A pack file is an
# ATTACHMENT from someone else, so unpacking it is the one place in this module
# that runs untrusted input through the filesystem: a manifest and a handful of
# pictures is the whole legitimate shape, and both a zip bomb and a folder of
# ten thousand entries are refused by name rather than unpacked.
PACK_MAX_ENTRIES = 64
PACK_MAX_BYTES = 64 * 1024 * 1024


def packs_dir():
    """Where installed packs live, or None when nothing can name a directory.

    The host sets `PACKS_DIR`, as it does every other path this module needs.
    The fallback exists so a module-only import still resolves packs instead of
    silently reporting "none installed" — and it follows the same XDG rule the
    host uses, so the two cannot disagree about where a pack was installed.
    """
    if PACKS_DIR is not None:
        try:
            return Path(PACKS_DIR)
        except (TypeError, ValueError):
            return None
    try:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or str(Path.home() / ".config"))
    except (OSError, ValueError, TypeError):
        return None
    return Path(base) / (APP_NAME or "handsoff") / PACK_DIR_NAME


def pack_slug(value) -> str:
    """`value` as ONE directory name: letters, digits, dash and underscore.

    Anything else becomes a dash, so a pack called "Optimus Prime / v2" installs
    as `optimus-prime-v2`, and a name can never become a path.
    """
    slug = "".join(ch if (ch.isalnum() or ch in "-_") else "-"
                   for ch in str(value or "").strip().lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:48]


def _pack_file(folder, rel):
    """`rel` resolved inside `folder`, or None when it would leave it."""
    text = str(rel or "").strip()
    if not text:
        return None
    named = Path(text)
    if named.is_absolute() or ".." in named.parts:
        return None
    try:
        root = Path(folder).resolve()
        full = (root / named).resolve()
        if full != root and root not in full.parents:
            return None          # a symlink out of the pack is still out
    except (OSError, ValueError):
        return None
    return str(full)


def _resolve_art(value):
    """A manifest value as the resolver's output: a path or an animation spec.

    A still stays a path STRING; an animation stays a spec dict — the caller
    (the painter, the exports) branches on that. One function because
    `design_picture`, `picture_for` and `effective_art` must all answer the
    same way about the same manifest.
    """
    if _is_animation(value):
        anim, _p = pack_animation(value)
        return anim
    return str(value or "")


def pack_animation(raw, folder=None):
    """`raw` as an animation spec: ({"frames": [abs paths], "fps": float}, "").

    A state's manifest value is EITHER a plain string (a still picture, every
    existing pack) OR an object `{"frames": [paths], "fps": n}` — an ANIMATION.
    This is the one reader of that shape, returning `(spec, "")` or
    `(None, problem)`, so validation, the resolver, `pack_problem` and the
    exports cannot grow four ideas of what an animation is.

    `folder` resolves frame paths absolutely when given; when None the frames
    stay as given (the validator passes it, the picker's form passes None and
    resolves later). A spec is NEVER half-returned: an empty frame list, a
    frame that would leave the pack, more than `PACK_MAX_FRAMES`, or an
    fps outside its bounds is a refusal naming the first thing wrong.
    """
    if isinstance(raw, str):
        return {"frames": [raw], "fps": PACK_FPS_DEFAULT}, ""
    if not isinstance(raw, dict):
        return None, "an animation must be \"file.png\" or {\"frames\": […], \"fps\": n}"
    frames_raw = raw.get("frames")
    if not isinstance(frames_raw, list) or not frames_raw:
        return None, "an animation needs a non-empty \"frames\" list"
    if len(frames_raw) > PACK_MAX_FRAMES:
        return None, (f"an animation holds at most {PACK_MAX_FRAMES} frames "
                      f"(this one names {len(frames_raw)})")
    frames = []
    for rel in frames_raw:
        text = str(rel or "").strip()
        if not text:
            return None, "an animation's frames must be file names, not empty"
        found = _pack_file(folder, text) if folder is not None else text
        if not found:
            return None, (f"the frame '{text}' is outside the pack — a picture "
                          f"has to sit beside it")
        frames.append(found)
    fps_raw = raw.get("fps", PACK_FPS_DEFAULT)
    try:
        fps = float(fps_raw)
    except (TypeError, ValueError):
        return None, f"\"fps\" must be a number ({fps_raw!r} is not)"
    # No separate NaN test: every comparison against a NaN is False, so the
    # range check below already refuses `float("nan")` and `float("inf")`. The
    # `or fps != fps` that used to sit here was dead code that read as if a NaN
    # could slip past the bounds.
    if not PACK_FPS_MIN <= fps <= PACK_FPS_MAX:
        return None, (f"\"fps\" must be between {PACK_FPS_MIN:g} and "
                      f"{PACK_FPS_MAX:g} ({fps:g} is not)")
    return {"frames": frames, "fps": fps}, ""


def _is_animation(raw) -> bool:
    """Whether a manifest value is the animation shape (dict with frames)."""
    return isinstance(raw, dict) and isinstance(raw.get("frames"), list)


def _build_manifest(slug: str, folder, raw):
    """Validate one parsed manifest; None when it names nothing drawable."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip() or slug
    if _is_animation(raw.get("any")):
        anim, _p = pack_animation(raw["any"], folder)
        fallback = anim if anim else _pack_file(folder, raw.get("any"))
    else:
        fallback = _pack_file(folder, raw.get("any"))
    states = {}
    named = raw.get("states")
    if isinstance(named, dict):
        for state in PACK_STATES:
            value = named.get(state)
            if not value:
                continue
            if _is_animation(value):
                anim, _p = pack_animation(value, folder)
                found = anim if anim else None
            else:
                found = _pack_file(folder, value)
            if found:
                states[state] = found
    if not fallback and not states:
        return None
    return {"slug": slug, "name": name, "any": fallback, "states": states}


def _shown(path, root) -> str:
    """`path` as a message should say it: relative to `root` when one was given.

    A pack that arrived as a FILE is unpacked into a temporary folder before it
    can be judged, and a sentence naming `/tmp/.handsoff-preview-ab12/missing.png`
    tells the user nothing they can act on. The caller passes the folder it
    unpacked into, so the refusal names the ENTRY instead — `missing.png` — which
    is what is actually wrong with the archive in their hand. A pack read where
    it sits keeps its full path, because there the path IS the information.
    """
    if root is None:
        return str(path)
    try:
        return str(Path(path).relative_to(root))
    except (ValueError, TypeError):
        return str(path)


def _validate_pack(folder, raw, slug: str = "", display_root=None) -> tuple:
    """Check one parsed manifest against its folder: (problem, built).

    `problem` is "" when the pack can draw, and otherwise ONE sentence naming
    the first thing wrong and the entry it is wrong about. `built` is the
    manifest `load_pack` returns, and it is None whenever `problem` is
    non-empty — a caller must never draw a half-built pack. The single
    authority behind install, load and report (see the section comment).

    `display_root` only changes how a path is SPOKEN (see `_shown`); it can
    never change the verdict, so the same pack is refused and accepted by the
    same rules whether it is being installed or only looked at.
    """
    if not isinstance(raw, dict):
        return f"{PACK_MANIFEST} must be a JSON object", None
    named = raw.get("states") if isinstance(raw.get("states"), dict) else {}
    checked = []
    for where, rel in (("any", raw.get("any")),) + tuple(
            (state, named.get(state)) for state in PACK_STATES):
        if not rel:
            continue
        # A state is either a picture or an ANIMATION; the animation's frames
        # are checked exactly like pictures, so an unreadable frame is refused
        # with the same sentence a broken still would get.
        if _is_animation(rel):
            anim, problem = pack_animation(rel, folder)
            if problem:
                return f"the {where} animation: {problem}", None
            checked.append((where, anim["frames"]))
            continue
        found = _pack_file(folder, rel)
        if not found:
            return (f"{PACK_MANIFEST} names '{rel}' for {where}, which is "
                    f"outside the pack — a picture has to sit beside it", None)
        checked.append((where, found))
    if not checked:
        return "the pack names no pictures", None
    for where, found in checked:
        for p_text in (found if isinstance(found, list) else [found]):
            p = Path(p_text)
            if not p.exists():
                return (f"no file at {_shown(p, display_root)} "
                        f"(the {where} picture)", None)
            if not p.is_file():
                return (f"{_shown(p, display_root)} is a folder, not an image "
                        f"(the {where} picture)", None)
            img = _decoded_image(p)
            if img is None:
                return (f"{p.name} is not an image this build can read "
                        f"(the {where} picture)", None)
            if not _has_ink(img):
                return (f"every pixel of {p.name} is transparent — the {where} "
                        f"picture would be invisible", None)
    if not raw.get("any"):
        missing = [s for s in PACK_STATES if s not in named or not named.get(s)]
        if missing:
            return (f"no picture for {', '.join(missing)} and no 'any' — "
                    f"those states would draw the empty slot", None)
    return "", _build_manifest(slug, folder, raw)


def _forget_pack(slug: str) -> None:
    """Drop cached manifests for `slug` — an install replaces what they say."""
    for key in [k for k in _PACK_CACHE if k[0] == slug]:
        _PACK_CACHE.pop(key, None)


def load_pack(pack):
    """An installed pack's manifest, or None. Cached per manifest revision.

    Returns {"slug", "name", "any", "states"} with ABSOLUTE picture paths; a
    manifest that names nothing usable is None. The cache key carries the
    manifest's mtime and size, so editing `pack.json` — or installing over a
    pack — takes effect without a restart.
    """
    slug = pack_slug(pack)
    root = packs_dir()
    if not slug or root is None:
        return None
    folder = Path(root) / slug
    manifest = folder / PACK_MANIFEST
    try:
        st = manifest.stat()
        key = (slug, st.st_mtime_ns, st.st_size)
    except (OSError, ValueError, TypeError):
        return None
    if key in _PACK_CACHE:
        return _PACK_CACHE[key]
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    # A pack with ANY problem resolves to nothing (`_validate_pack`'s contract:
    # `built` is None whenever `problem` is non-empty): art assembled from the
    # parts that happened to check out is the silent half-working this tree
    # keeps having to delete. `pack_problem` names what is wrong instead.
    _problem, built = _validate_pack(folder, raw, slug)
    while len(_PACK_CACHE) >= _PACK_CACHE_MAX:
        _PACK_CACHE.pop(next(iter(_PACK_CACHE)))
    _PACK_CACHE[key] = built
    return built


def state_pictures(settings=None) -> dict:
    """{state: path} for the states with a picture of their OWN, others omitted.

    Reads the per-state settings (`design_image_<state>`) and drops the empty
    ones, so "does this state have its own picture" is one question with one
    answer — asked by the resolver, by `doctor` and by the settings panel, which
    is what stops the three describing the same settings differently.
    """
    src = SETTINGS if settings is None else settings
    out = {}
    for state in PACK_STATES:
        raw = str((src or {}).get(STATE_IMAGE_KEY[state]) or "").strip()
        if raw:
            out[state] = raw
    return out


def picture_for(pack, single, state: str = "", per_state=None):
    """The art in effect for `state`, by precedence, or "" for the slot.

    A pack is the AUTHORITY: setting one means its pictures are the art, so a
    pack that cannot be read returns "" (the empty slot) rather than silently
    substituting a picture the user did not choose; `pack_problem()` names the
    reason. Without a pack, a state that has its OWN picture uses it, and a state
    that has none falls back to `single` — the same shape as a pack's `states`
    plus its `any`, so the two ways of giving one design several pictures obey
    one rule instead of two.

    Takes the values the CALLER already holds rather than reading the settings,
    because two processes need it: the bubble (which reads settings) and the
    settings app's preview (which reads its own form). ONE implementation of the
    precedence, instead of two that can disagree about which picture is on
    screen.
    """
    slug = pack_slug(pack)
    if slug:
        manifest = load_pack(slug)
        if manifest is None:
            return ""
        value = manifest["states"].get(str(state)) or manifest["any"] or ""
        return _resolve_art(value)
    own = str((per_state or {}).get(str(state)) or "").strip()
    return own or str(single or "").strip()


def design_picture(state: str = "") -> str:
    """The picture the `image` design draws in `state`, from the settings.

    A live PREVIEW wins over the settings: while the Appearance panel is showing
    a candidate (`pack_preview`), the bubble draws that pack's pictures — the
    point of a preview is to be judged on the real desktop, and a desktop still
    showing the old art would be the preview only existing in the panel.
    """
    live = pack_preview()
    if live is not None:
        return _resolve_art(live["states"].get(str(state))
                            or live["any"] or "")
    return picture_for(SETTINGS.get("design_pack"),
                       SETTINGS.get("design_image_path"), state,
                       state_pictures(SETTINGS))


def avatar_deco() -> str:
    """Which decoration the avatar wears: "off" or one of the animations.

    The painter calls this per frame, so the closed choice is read at the same
    place everything else about the look is — a reload changes the decoration
    on the next frame with no separate notification path to forget. An
    unrecognised value is "off": a decoration is drawn from a name, and a name
    nothing implements must draw nothing rather than something arbitrary.
    """
    name = str(SETTINGS.get("avatar_ring") or "").strip().lower()
    return name if name in DECORATIONS else "off"


def avatar_ring_on() -> bool:
    """Whether the avatar wears ANY decoration (the band is reserved)."""
    return avatar_deco() != "off"


# The two WORDS `avatar_deco_color` accepts; anything else must be a literal
# hex. Declared here as well as in `settings_schema` because this module cannot
# import the schema (the installed layout resolves it through a loader) — and
# pinned by a guard asserting the two tuples are EQUAL, so a mode added to one
# is not a mode the other refuses.
DECO_COLOR_MODES = ("state", "rainbow")


def avatar_deco_color() -> str:
    """What the decoration is coloured with: a mode word, or a literal hex.

    Read per frame next to everything else about the look, so a reload changes
    it with no separate notification path to forget. Anything unrecognised is
    "state", which is the honest fallback: the state colour is the one answer
    that is always available, and a ring left uncoloured would be the "nothing
    applied" defect this card exists to answer.

    A hex is validated through `core.theme.hex_to_rgb` — the tree's ONE hex
    parser, fullmatch — so `#4f8cffXYZ` is refused here exactly as the swatch
    refuses it. Named Qt colours (`red`) are refused too, because they would
    make `settings.json` mean something different in another Qt version.
    """
    raw = str(SETTINGS.get("avatar_deco_color") or "").strip().lower()
    if raw in DECO_COLOR_MODES:
        return raw
    if hex_to_rgb(raw) is not None:
        return raw
    return "state"


def avatar_deco_colour(state_color, t: float, lv: float, anim: float,
                       mode: str = ""):
    """The colour the decoration is drawn in, INDEPENDENT of the state.

    Three sources in one place, because the painters own their motion and
    nothing else — the colour is decided here so that "the ring is its own
    colour" is one decision rather than a term each painter has to remember:

      * `state` — the state colour, the default, unchanged behaviour;
      * `rainbow` — a hue that sweeps on its own, sped up by the animation
        energy and the voice, so the ring MOVES in colour as well as in shape;
      * a literal hex — the colour the user picked, exactly.

    Independence cannot cost readability: the rim is drawn in the state colour
    and is the design's outermost ink, so which state the bubble is in stays
    answerable however the decoration is coloured.

    `mode` lets a caller draw a value it HOLDS but has not saved — the settings
    app's preview strip, which shows the form rather than the disk. The default
    reads the settings, so the bubble still has exactly one source of truth and
    the preview is not a second implementation of the choice.
    """
    mode = mode or avatar_deco_color()
    if mode == "state":
        return QColor(state_color)
    if mode == "rainbow":
        # A full turn every ~8 s at energy 1, up to ~2.5x that at the voice
        # ceiling. Saturation stays below 1 so the ring reads as light rather
        # than as a colour wheel, and lightness tracks the voice so a loud
        # rainbow is a brighter one — the same "brighter with the voice"
        # contract every decoration already has.
        hue = (t * 0.12 * anim * (1.0 + 0.35 * lv)) % 1.0
        light = min(0.80, 0.52 + 0.10 * lv + 0.05 * max(0.0, anim - 1.0))
        return QColor.fromHslF(hue, 0.78, max(0.44, light))
    return QColor(mode)


def avatar_natural() -> bool:
    """Whether the picture keeps its own colours instead of the state wash.

    The wash is what makes a photo read as the bubble's mood, and what makes a
    drawn character impossible: every silhouette comes out the state hue. When
    this is on the state is carried by the rim and the decoration instead, so
    "which state am I in" is still answerable without repainting the art.
    """
    return str(SETTINGS.get("avatar_tint") or "").strip().lower() == "natural"


def installed_packs() -> list:
    """[(slug, display name)] for everything installed, name-sorted.

    A pack whose manifest is broken is LISTED, under its folder name, rather
    than hidden: it is exactly the one the user needs to be told about.
    """
    root = packs_dir()
    if root is None:
        return []
    try:
        folders = sorted((p for p in Path(root).iterdir() if p.is_dir()),
                         key=lambda p: p.name.lower())
    except OSError:
        return []
    out = []
    for folder in folders:
        if folder.name.endswith(".previous"):
            continue
        manifest = load_pack(folder.name)
        out.append((folder.name, manifest["name"] if manifest else folder.name))
    return out


def pack_problem(pack=None) -> str:
    """Why a pack will not render what it promises, or "" when it will.

    One sentence naming the first thing wrong and the file it is wrong about —
    the sentence the picker shows and the reason the bubble draws its empty
    slot, so the panel, the desktop and `doctor` cannot disagree. No pack
    selected is not a problem: it is the state every install starts in.
    """
    slug = pack_slug(SETTINGS.get("design_pack") if pack is None else pack)
    if not slug:
        return ""
    root = packs_dir()
    if root is None:
        return "design packs are not available in this install"
    folder = Path(root) / slug
    manifest = folder / PACK_MANIFEST
    try:
        if not folder.is_dir():
            return f"no installed pack named {slug}"
        if not manifest.is_file():
            return f"{slug} has no {PACK_MANIFEST} — a pack needs one"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        return f"{slug}/{PACK_MANIFEST} is not valid JSON ({exc})"
    except OSError as exc:
        return f"cannot read {slug}/{PACK_MANIFEST} ({exc.strerror or exc})"
    problem, _built = _validate_pack(folder, raw, slug)
    return f"{slug}: {problem}" if problem else ""


def state_image_problem(per_state=None) -> str:
    """Why the per-state picture in effect will not render, or "".

    Reports the FIRST state whose own picture cannot be drawn, NAMING the state:
    with four picture slots, "which one is broken" is the entire question, and a
    sentence without the state name would send the user through four rows
    looking for it. One sentence, the same one the panel shows.
    """
    pics = state_pictures(SETTINGS) if per_state is None else per_state
    for state in PACK_STATES:
        path = str(pics.get(state) or "")
        if not path:
            continue
        problem = design_image_problem(path)
        if problem:
            return f"the {state} picture: {problem}"
    return ""


def usable_state_pictures(settings=None) -> dict:
    """{state: path} for the states whose OWN picture will actually be drawn.

    `state_pictures` answers "which states have a picture of their own" — a
    settings question. This answers "which of those will render", which is what
    a COUNT has to be if it is honest: reporting 4/4 on the same line that names
    a broken picture is a count of intentions, not of pictures, and the whole
    point of the per-state sentence is that it cannot disagree with the bubble.
    """
    return {state: path
            for state, path in state_pictures(settings).items()
            if not design_image_problem(path)}


def art_problem() -> str:
    """Why the `image` design will not render what is chosen, or "".

    ONE sentence for `doctor`, by the same precedence the renderer uses: the
    selected pack when there is one (it is the authority), otherwise the first
    broken per-state picture, otherwise the single fallback picture.
    """
    if pack_slug(SETTINGS.get("design_pack")):
        return pack_problem()
    problem = state_image_problem()
    return problem or design_image_problem()


def _read_pack_folder(folder, fallback_name="", label="", relative=False) -> tuple:
    """Read and judge a folder that already holds a pack: (manifest, problem).

    ONE reading of what a pack CONTAINS — the manifest, the slug it installs
    under, and the validation every consumer has to agree on. `install_pack`
    calls it and then copies; `inspect_pack` calls it and then draws. That is
    what stops a PREVIEW being more forgiving than an install: the same sentence
    refuses both, so a pack you were shown is a pack that will install.

    `label` names the thing the USER chose, which is not always the folder: a
    pack that arrived as an archive is unpacked into a temporary directory whose
    name means nothing to anyone, so the caller passes the file's own name and
    every sentence names something the user has heard of.

    `relative` speaks a picture's path relative to the pack instead of in full.
    That is what an unpacked ARCHIVE wants, because the full path is a temporary
    folder the user has never seen; a folder being installed keeps full paths,
    where the path is the information.

    Returns (None, problem) when the pack cannot draw, and the manifest
    `load_pack` returns when it can — never a half-built one.
    """
    folder = Path(folder)
    where = str(label or folder.name)
    try:
        manifest = folder / PACK_MANIFEST
        if not manifest.is_file():
            return None, f"{where} has no {PACK_MANIFEST} — a pack needs one"
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, f"{where}/{PACK_MANIFEST} is not valid JSON ({exc})"
    except OSError as exc:
        return None, f"cannot read {folder} ({exc.strerror or exc})"
    if not isinstance(raw, dict):
        return None, f"{PACK_MANIFEST} must be a JSON object"
    slug = (pack_slug(raw.get("name")) or pack_slug(fallback_name)
            or pack_slug(folder.name))
    if not slug:
        return None, "the pack needs a name with at least one letter or digit"
    # The SAME validator the renderer uses, so a folder this refuses would not
    # have drawn anything anyway — and it is refused with the sentence a broken
    # installed pack is reported with, not a second wording.
    problem, built = _validate_pack(folder, raw, slug,
                                    folder if relative else None)
    if problem:
        return None, f"{where}: {problem}"
    return built, ""


def install_pack(source, fallback_name="", label="", relative=False) -> tuple:
    """Copy the pack folder at `source` into the packs directory.

    Returns (slug, message): slug is "" when nothing was installed, and the
    message says what happened in the same words either way, so a caller has one
    thing to show and no exception to catch. The source is validated BEFORE
    anything is copied, so a refusal leaves the installed packs untouched.

    `fallback_name` names the pack when its manifest carries no `name` of its
    own. A folder installs under its own folder name; a pack that arrived as an
    ARCHIVE is unpacked into a private temporary folder whose name means nothing
    to anyone, so `install_pack_file` passes the file's own stem here and the
    pack ends up named after the file the user chose. `label` and `relative`
    likewise make its refusals speak about what the user chose — see
    `_read_pack_folder`.
    """
    root = packs_dir()
    if root is None:
        return "", "design packs are not available in this install"
    src = Path(str(source)).expanduser()
    try:
        if not src.is_dir():
            return "", f"{src} is not a folder"
    except OSError as exc:
        return "", f"cannot read {src} ({exc.strerror or exc})"
    built, problem = _read_pack_folder(src, fallback_name, label, relative)
    if problem:
        return "", problem
    slug = str(built["slug"])
    target = Path(root) / slug
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        previous = Path(root) / f"{slug}.previous"
        if target.exists():
            if previous.exists():
                shutil.rmtree(previous)
            target.rename(previous)
        shutil.copytree(src, target)
    except OSError as exc:
        return "", f"cannot install {slug} ({exc.strerror or exc})"
    _forget_pack(slug)                 # the install replaced what the cache says
    if load_pack(slug) is None:
        return "", (f"{slug} installed, but its pictures could not be read — "
                    f"check {PACK_MANIFEST} and the files it names")
    return slug, (f"installed pack {built['name']} as {slug} "
                  f"({len(built['states'])} state picture(s)"
                  + (", plus a fallback)" if built["any"] else ")"))


def inspect_pack(source) -> tuple:
    """Read a pack FOLDER or pack FILE without installing it: what it would be.

    Returns (art, problem, scratch). `art` is the manifest shape `load_pack`
    returns — {"slug", "name", "any", "states"} with absolute picture paths —
    and it is None when nothing can be drawn; `problem` is then the sentence an
    INSTALL would refuse with, because a preview that were more forgiving than an
    install would be a preview of something you cannot have. `scratch` is a
    temporary folder holding the art when it had to be unpacked out of a FILE,
    and the CALLER owns it: it removes it when it stops showing that pack. A
    folder is read where it already sits, so its scratch is "" and looking at a
    folder never copies, moves or writes anything.

    Nothing reaches the install, which is the whole point: the panel can show
    you a pack — drawing the very pictures an install would copy, judged by the
    very validator that governs it — before you decide to take it.
    """
    try:
        src = Path(str(source)).expanduser()
    except (OSError, ValueError, TypeError):
        return None, "that is not a pack folder or file", ""
    try:
        if src.is_dir():
            built, problem = _read_pack_folder(src)
            return (None, problem, "") if problem else (built, "", "")
        if not src.is_file():
            return None, f"{src.name or src} is not a pack folder or file", ""
    except OSError as exc:
        return None, f"cannot read {src} ({exc.strerror or exc})", ""
    try:
        scratch = tempfile.mkdtemp(prefix=".handsoff-preview-")
    except OSError as exc:
        return None, f"cannot unpack {src.name} ({exc.strerror or exc})", ""
    problem, folder = _extract_pack_archive(src, scratch, src.name)
    if not problem:
        built, problem = _read_pack_folder(folder, src.stem, src.name,
                                          relative=True)
        if not problem:
            return built, "", scratch
    shutil.rmtree(scratch, ignore_errors=True)
    return None, problem, ""


# ------------------------------------------------------------- live preview
# A pack the Appearance panel is LOOKING AT, drawn by the bubble on the real
# desktop before anything is installed.
#
# The panel's strip answers "what does it look like"; only the desktop answers
# "how does it look HERE" — against the wallpaper, beside the other windows, at
# the size this bubble actually is. Handing a look to the bubble is what makes
# the difference between those two questions exist at all.
#
# The candidate is held in MEMORY and never copied: installing is still the only
# thing that writes to the packs directory. But it is judged by the same
# `_read_pack_folder` that `install_pack` uses, so a preview is still a pack that
# would install — one sentence refuses both.
#
# The deadline is what ends it. The panel renews while it is previewing, so a
# panel that dies, is closed, or loses its connection simply stops renewing and
# the bubble puts the real look back by itself. Nothing needs cleaning up here
# because nothing was written; a previewed FILE's temporary folder stays the
# PANEL's to remove, and a leaked one is a few files in /tmp, never a wrong look
# on the desktop.
PREVIEW_TTL_S = 6.0

_PREVIEW_LOCK = threading.Lock()
_PREVIEW: dict = {"manifest": None, "name": "", "source": "", "at": 0.0}


def pack_preview() -> "dict | None":
    """The manifest of the pack the bubble is SHOWING but has not installed.

    None when there is no live preview — which includes one that was live and
    has run out of renewals, because the deadline is checked on READ rather
    than by a timer: an answer that comes from the same read as the data cannot
    be stale between the timer and the paint that follows it.
    """
    now = time.monotonic()
    with _PREVIEW_LOCK:
        manifest, renewals, name = (_PREVIEW["manifest"], _PREVIEW["at"],
                                    _PREVIEW["name"])
        if manifest is None:
            return None
        if now - renewals <= PREVIEW_TTL_S:
            return manifest
        _PREVIEW.update({"manifest": None, "name": "", "source": "",
                         "at": 0.0})
    log.info("pack preview expired (no renewal from the settings panel): %s",
             name)
    return None


def set_pack_preview(source) -> tuple:
    """Draw the pack folder at `source` on the bubble without installing it.

    Returns (name, message) in `install_pack`'s own shape: `name` is "" when
    `source` is not a pack that would install, and `message` says what happened
    either way, so the panel has one line to show and no exception to catch.

    A refusal CLEARS whatever was being previewed: the caller asked to show
    this instead, so leaving the previous candidate on the desktop would be
    showing art nobody asked for.

    Called repeatedly with the same folder — that is the panel's heartbeat — so
    it must be cheap and idempotent. Re-reading it each time is deliberate: a
    candidate whose folder is deleted or edited mid-preview is then DETECTED and
    dropped, instead of the bubble drawing from state that no longer matches the
    disk.
    """
    built, problem = None, ""
    try:
        src = Path(str(source)).expanduser()
        built, problem = ((None, f"{src} is not a folder") if not src.is_dir()
                          else _read_pack_folder(src))
    except (OSError, ValueError, TypeError) as exc:
        problem = f"cannot read {source} ({exc})"
    if built is None:
        clear_pack_preview()
        return "", problem or "that is not a pack folder"
    name = str(built["name"])
    with _PREVIEW_LOCK:
        _PREVIEW.update({"manifest": built, "name": name, "source": str(src),
                         "at": time.monotonic()})
    return name, f"previewing {name} on the bubble \u2014 nothing installed"


def clear_pack_preview() -> str:
    """Stop drawing a previewed pack: the bubble goes back to its own look.

    Says whether anything was being previewed, because "I stopped it" and
    "there was nothing to stop" are different facts and this reply is the only
    place a caller can learn them.
    """
    with _PREVIEW_LOCK:
        name = str(_PREVIEW["name"] or "")
        _PREVIEW.update({"manifest": None, "name": "", "source": "", "at": 0.0})
    return (f"stopped previewing {name}" if name
            else "no pack was being previewed")


def preview_note() -> str:
    """One sentence for `doctor` while a preview is live, or "".

    A preview draws art the settings do not name, so without this the
    appearance line would describe a look the bubble is NOT drawing — and would
    do it exactly when someone is staring at the difference.
    """
    live = pack_preview()
    return f"previewing {live['name']} (not installed)" if live else ""


def design_in_effect() -> str:
    """The design the bubble is drawing: `image` while a pack is previewed.

    A pack is only ever drawn by the `image` design, so a preview that swapped
    the PICTURE but left the shape alone would change nothing on the desktop for
    anyone whose bubble draws an orb — the same "nothing applies" the panel's
    own strip forces `image` to avoid. This is the ONE place that decides, so
    the painter and the window mask cannot disagree about which shape is up.
    """
    if pack_preview() is not None:
        return "image"
    return str(SETTINGS.get("bubble_design", "orb")).strip().lower()


def effective_art(settings=None) -> tuple:
    """The art the bubble is DRAWING: (fallback path, {state: path}).

    ONE definition of "what is on screen", by the precedence the renderer uses:
    a selected pack is the authority, otherwise the per-state pictures with the
    fallback behind them. `export_pack` reads it rather than re-deciding, so
    sharing a look cannot export art other than the art in effect — and a pack
    that cannot be read yields NOTHING here, like everywhere else, instead of
    exporting the files it happened to name.
    """
    src = SETTINGS if settings is None else settings
    src = src if isinstance(src, dict) else {}
    slug = pack_slug(src.get("design_pack"))
    if slug:
        manifest = load_pack(slug)
        if manifest is None:
            return "", {}
        return (_resolve_art(manifest.get("any") or ""),
                {s: _resolve_art(p) for s, p in (manifest.get("states") or {}).items()})
    return (str(src.get("design_image_path") or "").strip(),
            state_pictures(src))


def _export_target(parent, name, suffix: str) -> tuple:
    """Where an export may write, or why it may not: (slug, path, problem).

    Both exports refuse the same three things in the same words, so the folder
    and the single file cannot drift into disagreeing about what a legal
    destination is. `suffix` is what makes the difference between them — "" for
    a folder named after the slug, `.hpack` for the one file.
    """
    slug = pack_slug(name)
    if not slug:
        return "", None, "the pack needs a name with at least one letter or digit"
    try:
        root = Path(str(parent)).expanduser()
        if not root.is_dir():
            return "", None, f"{root} is not a folder"
    except (OSError, ValueError, TypeError):
        return "", None, "that destination is not a folder"
    target = root / f"{slug}{suffix}"
    if target.exists():
        return "", None, (f"{target} already exists — export into a folder that "
                           f"does not, so nothing you already have is overwritten")
    return slug, target, ""


def _stage_art(parent, slug, name, settings) -> tuple:
    """Assemble the art in effect into a checked hidden folder under `parent`.

    Returns (staging, built, problem). `problem` is "" exactly when `staging`
    holds a pack `_validate_pack` accepts; on a refusal the folder is removed
    HERE and `staging` is None, so the two states can never be confused by a
    caller that forgot to clean up. ONE assembly serves both exports — the
    folder and the single file — because the two must never disagree about what
    a look IS.

    The refusal comes back already worded for the user, including the plain "no
    pictures to export", so each caller shows one sentence it did not compose.
    """
    fallback, states = effective_art(settings)
    if not fallback and not states:
        return None, None, ("no pictures to export — give a state a picture "
                            "(or select a pack) first")
    used: dict = {}

    def _inside(label: str, path: str) -> str:
        """A file name for `path` that no other picture in this pack uses.

        Two states may name the same file — that is one copy, named once — but
        two DIFFERENT files called `idle.png` must not overwrite each other, so a
        collision is prefixed with the state it belongs to.
        """
        base = Path(path).name or f"{label}.png"
        candidate, n = base, 0
        while candidate in used and used[candidate] != path:
            n += 1
            candidate = f"{label if n == 1 else f'{label}{n}'}-{base}"
        used[candidate] = path
        return candidate

    manifest: dict = {"name": str(name).strip() or slug}
    named: dict = {}
    copies: list = []

    def _copy_one(label: str, source_text: str) -> str:
        source = os.path.expanduser(source_text)
        file_name = _inside(label, source)
        copies.append((source, file_name))
        return file_name

    def _copy_art(label: str, value) -> "dict | str | None":
        """One state's art into staging: its manifest value, or None.

        A STILL copies as before; an ANIMATION copies every frame and exports
        the spec — so an animated look shares as animated, never as only its
        first frame. `_validate_pack` re-judges the result below, so the spec
        written here is the spec the pack will be read back with.
        """
        if isinstance(value, dict):
            anim, problem = pack_animation(value)
            if problem or not anim:
                return None
            frames = [_copy_one(f"{label}-frame-{i}", f)
                      for i, f in enumerate(anim["frames"])]
            return {"frames": frames, "fps": anim["fps"]}
        path = str(value or "").strip()
        return _copy_one(label, path) if path else None

    for state in PACK_STATES:
        if states.get(state):
            entry = _copy_art(state, states[state])
            if entry is not None:
                named[state] = entry
    if fallback:
        entry = _copy_art("any", fallback)
        if entry is not None:
            manifest["any"] = entry
    if named:
        manifest["states"] = named
    staging = None
    try:
        staging = tempfile.mkdtemp(dir=str(parent), prefix=f".{slug}-")
        for source, file_name in copies:
            shutil.copy2(source, Path(staging) / file_name)
        (Path(staging) / PACK_MANIFEST).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        problem, built = _validate_pack(staging, manifest, slug)
        if problem:
            shutil.rmtree(staging, ignore_errors=True)
            return None, None, f"{slug}: {problem}"
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return None, None, f"cannot export {slug} ({exc.strerror or exc})"
    return staging, built, ""


def export_pack(parent, name, settings=None) -> tuple:
    """Write the art in effect as a NEW pack folder under `parent`.

    Returns (folder, message): folder is "" when nothing was written, and the
    message says what happened either way, so a caller has one thing to show and
    no exception to catch. What is written is the art the bubble is drawing —
    the selected pack's pictures, or your own per-state pictures with the
    fallback behind them — so a look built by hand becomes a folder that can be
    handed to someone else.

    Two rules keep a one-click write safe. `parent/<slug>` must not already
    exist: an export never eats a folder the user already has. And the pack is
    assembled in a hidden staging folder and checked by `_validate_pack`, the
    SAME authority `install_pack` uses, BEFORE it is moved into place — so a
    folder this writes is one install will accept, and a refusal (an unreadable
    picture, a state left uncovered with no fallback) removes what it wrote
    instead of leaving half a pack in the user's directory.
    """
    slug, target, problem = _export_target(parent, name, "")
    if problem:
        return "", problem
    staging, built, problem = _stage_art(target.parent, slug, name, settings)
    if problem:
        return "", problem
    try:
        Path(staging).rename(target)
    except OSError as exc:
        return "", f"cannot export {slug} ({exc.strerror or exc})"
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return str(target), (f"exported {built['name']} to {target} "
                         f"({len(built['states'])} state picture(s)"
                         + (", plus a fallback)" if built["any"] else ")"))


def export_pack_file(parent, name, settings=None) -> tuple:
    """Write the art in effect as ONE file — a pack someone can send.

    Returns (path, message) exactly as `export_pack` returns (folder, message),
    and writes the same art by the same rules: the two differ only in what they
    hand back, a folder or a single `.hpack` that can be attached to a message.
    A folder is not something you can hand someone, which is the whole reason
    this exists — `install_pack_file` reads it back.

    The archive is written to a hidden file and MOVED into place afterwards, so
    a half-written pack can never be mistaken for a finished one, and the
    destination must not already exist for the same reason the folder export
    refuses: a one-click write never eats something you already have.
    """
    slug, target, problem = _export_target(parent, name, PACK_EXT)
    if problem:
        return "", problem
    staging, built, problem = _stage_art(target.parent, slug, name, settings)
    if problem:
        return "", problem
    written = None
    try:
        handle, written = tempfile.mkstemp(dir=str(target.parent),
                                           prefix=f".{slug}-", suffix=PACK_EXT)
        os.close(handle)
        _write_pack_archive(Path(written), staging)
        os.replace(written, target)
        written = None
    except OSError as exc:
        return "", f"cannot export {slug} ({exc.strerror or exc})"
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if written:
            try:
                os.unlink(written)
            except OSError:
                pass
    return str(target), (f"exported {built['name']} to {target} "
                         f"({len(built['states'])} state picture(s)"
                         + (", plus a fallback)" if built["any"] else ")"))


def _write_pack_archive(archive, folder) -> None:
    """Zip a validated staging folder into one pack file.

    Pictures are STORED and the manifest DEFLATED: a PNG is already compressed,
    so deflating it spends time to save nothing, while `pack.json` is text and
    is the one entry a person who opens the archive will actually read — the
    manifest is written FIRST, so the file reads in the order it makes sense.
    """
    root = Path(folder)
    entries = sorted(root.rglob("*"),
                     key=lambda p: (p.name != PACK_MANIFEST, str(p)))
    with zipfile.ZipFile(archive, "w") as zf:
        for entry in entries:
            if not entry.is_file():
                continue
            arcname = entry.relative_to(root).as_posix()
            zf.write(entry, arcname,
                     compress_type=(zipfile.ZIP_DEFLATED
                                    if arcname == PACK_MANIFEST
                                    else zipfile.ZIP_STORED))


def _too_big(label: str) -> str:
    """The one refusal for a pack file larger than a pack can be."""
    return (f"{label} unpacks to more than {PACK_MAX_BYTES // (1 << 20)} MB "
            f"— more than a design pack can be")


def _archive_escape(members):
    """The first entry name that would not stay inside the pack, or None.

    An archive is data someone else wrote, so this is checked BEFORE anything is
    unpacked: an absolute path, a `..` segment, a Windows drive letter or a
    symlink entry is a name that would put a file where the recipient never
    agreed to. Python's own extractor sanitises some of this SILENTLY; refusing
    by name is the honest version, because a pack that had to be quietly
    rewritten to be safe is not the pack that was sent.
    """
    # A symlink is refused even though `zipfile` writes such an entry as plain
    # text rather than a link: the ENTRY is asking to be a link, and a pack is a
    # folder of pictures — letting it through would turn "you sent a symlink"
    # into "your picture is not an image", which names the wrong problem.
    for member in members:
        text = str(member.filename or "")
        parts = Path(text.replace("\\", "/")).parts
        if (not text or text.startswith("/") or ".." in parts
                or (parts and ":" in parts[0])):
            return text or "(an unnamed entry)"
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            return text
    return None


def _manifest_folder(root, label: str) -> tuple:
    """Which folder `root` holds the manifest in: (problem, folder).

    A pack file is written with `pack.json` at its top level, but a person
    sharing a pack usually zips the FOLDER — so one top-level folder holding a
    manifest is accepted as the pack too, and the two shapes a real recipient
    will meet both work. More than one candidate is refused rather than guessed
    at: an archive holding two packs is a question only its sender can answer,
    and picking one would silently install the wrong look.
    """
    try:
        entries = list(Path(root).iterdir())
    except OSError as exc:
        return (f"cannot read what {label} unpacked to "
                f"({exc.strerror or exc})"), None
    found = []
    if (Path(root) / PACK_MANIFEST).is_file():
        found.append(Path(root))
    found += [p for p in entries
              if p.is_dir() and (p / PACK_MANIFEST).is_file()]
    if not found:
        return f"{label} has no {PACK_MANIFEST} — a pack file needs one", None
    if len(found) > 1:
        return (f"{label} holds more than one {PACK_MANIFEST} — a pack file "
                f"holds one pack"), None
    return "", found[0]


def _extract_pack_archive(archive, dest, label: str) -> tuple:
    """Unpack one pack file into `dest`: (problem, folder holding the manifest).

    Every refusal here is about not trusting the archive: it must BE a zip, it
    must not hold more entries than a pack has, its unpacked total must fit what
    a pack can be, and no entry may name a way out of the folder.

    The size guard reads the archive's OWN declared sizes, and that is the right
    number rather than a lazy one: the extractor below honours those sizes, so
    the declared total genuinely bounds what unpacking can cost — a header that
    understates it yields a short entry whose CRC then fails, and a header that
    overstates it is refused HERE, before a byte is written. That is why a size
    refusal leaves `dest` empty, and why this check does not need a second
    opinion measured during the write.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            if len(members) > PACK_MAX_ENTRIES:
                return (f"{label} holds {len(members)} entries — more than a "
                        f"design pack has"), None
            if sum(m.file_size for m in members) > PACK_MAX_BYTES:
                return _too_big(label), None
            escape = _archive_escape(members)
            if escape:
                return (f"{label} holds an entry that could leave the pack "
                        f"({escape}) — a pack file may only hold its own "
                        f"pictures"), None
            for member in members:
                if member.is_dir():
                    continue
                target = Path(dest) / Path(*member.filename.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
    except zipfile.BadZipFile as exc:
        return (f"{label} is not a pack file — it is not a zip archive "
                f"({exc})"), None
    except (OSError, ValueError) as exc:
        return f"cannot unpack {label} ({exc})", None
    return _manifest_folder(dest, label)


def install_pack_file(source) -> tuple:
    """Install a pack from ONE file — the archive `export_pack_file` writes.

    Returns (slug, message) exactly as `install_pack` does, because it IS an
    install: the file is unpacked into a private folder and then handed to the
    same code path, so a pack file can only ever do what a pack FOLDER could
    already do — the same `_validate_pack` authority, the same copy into the
    install, the same one `.previous` generation. What is new is where the
    folder came from, which is why none of the safety rules had to be written a
    second time and none of them can be forgotten on the way in.

    The file's own stem names the pack when its manifest has no `name`, because
    the private folder it was unpacked into is named for nobody.
    """
    if packs_dir() is None:
        return "", "design packs are not available in this install"
    try:
        src = Path(str(source)).expanduser()
    except (OSError, ValueError, TypeError):
        return "", "that is not a pack file"
    label = src.name or "that file"
    try:
        if not src.is_file():
            return "", f"{label} is not a file"
    except OSError as exc:
        return "", f"cannot read {label} ({exc.strerror or exc})"
    try:
        staging = tempfile.mkdtemp(prefix=".handsoff-pack-")
    except OSError as exc:
        return "", f"cannot unpack {label} ({exc.strerror or exc})"
    try:
        problem, folder = _extract_pack_archive(src, staging, label)
        if problem:
            return "", problem
        return install_pack(folder, src.stem, label, relative=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _image_lights(color: QColor, glow: float, energy: float, level: float):
    """The two ends of the tint gradient: accent, animation energy, voice.

    `glow` carries the accent slider, `energy` carries animation energy (the one
    frame value both sliders reach), `level` is the voice. Both sliders and the
    voice are neutral at their defaults — accent 0.5, silence — so a quiet
    bubble at the default settings renders one fixed tint of the picture, which
    is the rule every painter in this file follows.
    """
    hue = max(color.hueF(), 0.0)
    sat, lit = color.hslSaturationF(), color.lightnessF()
    lift = 0.30 * (glow - 1.0) + 0.22 * level + 0.55 * (energy - 0.12)
    top = QColor.fromHslF(hue, sat, min(0.94, max(0.32, lit + 0.36 + lift)), 1.0)
    bottom = QColor.fromHslF(hue, sat,
                             min(0.82, max(0.14, lit - 0.04 + 0.55 * lift)), 1.0)
    return top, bottom


def tinted_image(img, shade, color: QColor, glow: float = 1.0,
                 energy: float = 0.12, level: float = 0.0,
                 natural: bool = False):
    """An image as a state-coloured silhouette that keeps its own luminance.

    Composition ops only — SourceIn to lay the state colour into the image's
    alpha, Multiply to shade that colour with the picture's luminance, then
    DestinationIn to put the silhouette back (Multiply's own alpha is opaque, so
    without the last step the window would fill with the picture's bounding
    rectangle). All three run in C++, which is what makes this safe per frame; a
    per-pixel Python loop over a user's 4000x4000 photo at 25 Hz is how a bubble
    becomes a space heater.

    Takes the two layers rather than reading the cache, because two processes
    want this: the bubble, and the settings app's preview.
    """
    if img is None or shade is None or img.isNull():
        return None
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    if out.isNull():
        return None
    out.fill(Qt.transparent)
    q = QPainter(out)
    q.drawImage(0, 0, img)
    if natural:
        # The picture KEEPS its colours. Two passes only, both of which a
        # character can survive: a state-coloured veil low enough to read as a
        # cast of light rather than a repaint (so "which state" still colours
        # the face), then a WHITE brighten driven by the voice — a character
        # that literally lights up when spoken to, without its hue changing.
        # The veil is a CAST of the state colour, and its ceiling is what makes
        # that true: at full voice it is under a fifth of the picture, because
        # a stronger wash is the repaint this mode exists to stop — a yellow
        # character that turns blue when it speaks has not been kept.
        veil = QColor(color)
        veil.setAlpha(int(min(52.0, (10 + 12 * glow) + 18 * level)))
        q.setCompositionMode(QPainter.CompositionMode_SourceAtop)
        q.fillRect(out.rect(), veil)
        # The voice's real channel here is BRIGHTNESS: a screen pass that lifts
        # the art toward the light without touching its hue, so the character
        # visibly lights up when spoken to and stays itself.
        lift = int(min(150.0, 10.0 + 30.0 * energy + 95.0 * level))
        if lift > 0:
            hot = QColor(255, 255, 255, lift)
            q.setCompositionMode(QPainter.CompositionMode_Screen)
            q.setOpacity(min(1.0, 0.22 + 0.5 * max(level, 0.0)))
            q.fillRect(out.rect(), hot)
            q.setOpacity(1.0)
        q.end()
        return out
    q.setCompositionMode(QPainter.CompositionMode_SourceIn)
    grad = QLinearGradient(0.0, 0.0, 0.0, float(img.height()))
    top, bottom = _image_lights(color, glow, energy, level)
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bottom)
    q.fillRect(out.rect(), QBrush(grad))
    q.setCompositionMode(QPainter.CompositionMode_Multiply)
    q.drawImage(0, 0, shade)
    q.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    q.drawImage(0, 0, img)
    q.end()
    return out


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
    # The inscribed ellipse is the aperture for every design painted inside it,
    # `image` included: that design fits its picture's CIRCUMSCRIBED circle to
    # the aperture budget, so its tank of ink is a circle strictly smaller than
    # this ellipse and no union is needed (a rotation cannot change a circle).
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

    def __init__(self, assistant) -> None:
        # Deliberately untyped: this module is application-free, so it does not
        # know (or import) the app's Assistant class. It needs the object, not
        # the name.
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
        # radius spring: a damped chase (zeta ~0.72), so it MISSES the target
        # once by ~2-4% of the travel and rings down inside ~0.3 s of frames.
        # Measured and pinned in tests/test_bubble_anim.py — an earlier comment
        # here claimed "no overshoot", which the arithmetic never did.
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
        # `design_in_effect`, not the setting: a live pack preview is drawn by
        # the `image` design whatever shape the settings name.
        design = design_in_effect()
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
        elif design == "image":
            self._paint_image(p, f)
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
        for ear, sign in zip(_cat_ears(cx, cy, R, reach), (-1.0, 1.0),
                             strict=True):
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

    def _paint_image(self, p: QPainter, f: dict) -> None:
        """The user's own picture as the bubble: fitted, tinted, voice-lit.

        The rules stated at the top of this section are enforced here. What
        gets fitted to the aperture is the picture's CIRCUMSCRIBED circle — its
        DIAGONAL, not its width — which is why the voice can tilt it without a
        corner crossing the glass: a circle does not change under rotation. So
        `budget` (the aperture) is respected by construction rather than by
        luck, for the picture, its tilt, the rim's pen and the rim glow alike.

        This design deliberately ignores the state RADIUS pulse the other
        painters follow (`f["radius"]`): it fills the glass, because a picture
        that swells and shrinks on every state change reads as a glitch rather
        than as a mood. The state is carried by the colour and the rim, the
        voice by breath, tilt and ignition — the same division of labour, less
        the jiggle.
        """
        cx, cy, t = f["cx"], f["cy"], f["t"]
        color = f["color"]
        lv = min(1.0, max(0.0, float(f["level"])))
        energy = float(f["energy"])
        glow = float(f.get("glow", 1.0))
        anim = float(f.get("anim", 1.0))
        budget = APERTURE_R
        if budget <= 0.0:
            return
        # The picture BREATHES: the voice lifts its scale and the animation
        # energy sets how far the idle sway travels. The 0.88 base with a 1.09
        # ceiling is what leaves the rim (the outermost element drawn) inside
        # the aperture at every value either slider can hold. Neutral at
        # silence: lv contributes none of the 5%, and the sway is 2% of radius.
        deco = avatar_deco()
        ring_on = deco != "off"
        # The ring light lives in the band the smaller fit vacates, so the
        # picture and the decoration can never overlap: when the ring is on,
        # the picture's fit shrinks to hand it its band. The rim stays the
        # outermost ink of the design either way.
        fit_k = 0.76 if ring_on else 0.88
        breath = 1.0 + 0.05 * lv + 0.02 * anim * math.sin(t * 2.4 * anim)
        fit = budget * fit_k * breath
        if ring_on:
            # The band the picture vacated: from just outside its own edge to
            # just inside the aperture. `0.94` leaves room for the widest pen a
            # decoration may use, so no decoration can touch the glass edge.
            self._draw_avatar_deco(p, cx, cy, fit * 1.03, budget * 0.94, deco,
                                   t, lv, color, glow, anim)
        # The stage the avatar stands on: a soft disc in the state colour UNDER
        # the art. A transparent-background character (the normal case for a
        # pack) would otherwise be drawn straight onto the wallpaper, so the
        # figure reads as cut out rather than placed; alpha stays low so the
        # art is never competing with its own backdrop.
        self._draw_avatar_stage(p, cx, cy, fit, lv, color, glow)
        picture = design_image_tinted(color, glow, energy, lv, self._state, t)
        if picture is not None and not picture.isNull():
            self._draw_image_art(p, picture, cx, cy, fit, t, lv, anim)
        else:
            self._draw_image_slot(p, cx, cy, fit, t, lv, anim, color, glow)
        self._draw_image_rim(p, cx, cy, fit, lv, color, glow,
                             natural=avatar_natural())

    @staticmethod
    def _needs_rounding(img) -> bool:
        """Whether an avatar picture has OPAQUE corners — a full-bleed rect.

        Art that already carries its own silhouette (transparent corners, the
        way a character PNG is drawn) is left alone: masking it could only cut
        ink it drew on purpose. A rectangular photo has four opaque corners
        and is the thing that reads as a pasted rectangle rather than an
        avatar. Four pixel reads per frame, on the C++ side — nothing.
        """
        if img is None or img.isNull():
            return False
        w, h = img.width(), img.height()
        return any(img.pixelColor(x, y).alpha() > 200
                   for x, y in ((1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)))

    @staticmethod
    def _feather_mask(w: int, h: int):
        """A soft-edged ellipse alpha mask, cached by size.

        Solid to 86% of the inscribed radius, faded out by 97% — the feather
        is what makes a masked photo read as an avatar instead of a sticker.
        Cached per (w, h): the mask depends on nothing else, and the picture
        size is stable across frames, so the gradient is built once.
        """
        key = (w, h)
        cached = _FEATHER_MASKS.get(key)
        if cached is not None:
            return cached
        m = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        if m.isNull():
            return None
        m.fill(Qt.transparent)
        q = QPainter(m)
        q.setRenderHint(QPainter.Antialiasing, True)
        r = 0.5 * min(w, h)
        grad = QRadialGradient(QPointF(w / 2.0, h / 2.0), r)
        white = QColor(255, 255, 255, 255)
        grad.setColorAt(0.0, white)
        grad.setColorAt(0.86, white)
        edge = QColor(255, 255, 255, 0)
        grad.setColorAt(0.97, edge)
        grad.setColorAt(1.0, edge)
        q.setCompositionMode(QPainter.CompositionMode_Source)
        q.setPen(Qt.NoPen)
        q.setBrush(QBrush(grad))
        q.drawEllipse(QPointF(w / 2.0, h / 2.0), r, r)
        q.end()
        if len(_FEATHER_MASKS) > 8:          # bounded: sizes are few and stable
            _FEATHER_MASKS.clear()
        _FEATHER_MASKS[key] = m
        return m

    @staticmethod
    def _round_avatar(picture):
        """The picture clipped to a feathered circle — or unchanged.

        Only full-bleed art is masked (see `_needs_rounding`); the offscreen
        composite is the same size as the picture, so the draw below is the
        only consumer and nothing else can see the intermediate.
        """
        if not BubbleWidget._needs_rounding(picture):
            return picture
        w, h = picture.width(), picture.height()
        mask = BubbleWidget._feather_mask(w, h)
        if mask is None:
            return picture
        out = QImage(picture.size(), QImage.Format_ARGB32_Premultiplied)
        if out.isNull():
            return picture
        out.fill(Qt.transparent)
        q = QPainter(out)
        q.setCompositionMode(QPainter.CompositionMode_Source)
        q.drawImage(0, 0, picture)
        q.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        q.drawImage(0, 0, mask)
        q.end()
        return out

    @staticmethod
    def _draw_image_art(p: QPainter, picture, cx: float, cy: float, fit: float,
                        t: float, lv: float, anim: float) -> None:
        """The picture, scaled to the fit circle and tilted by the voice.

        The scale comes from the DIAGONAL, so the drawn rect's own corners
        touch the fit circle exactly and cannot leave it at any angle — the
        tilt really is free, which is the whole return on fitting the diagonal
        instead of the width. 3.5 degrees at full voice is small on purpose: a
        tilt you notice as rotation is a tilt that fights the picture.
        """
        iw, ih = float(picture.width()), float(picture.height())
        half = 0.5 * math.hypot(iw, ih)          # circumscribed radius
        if half <= 0.0:
            return
        scale = fit / half
        w, h = iw * scale, ih * scale
        picture = BubbleWidget._round_avatar(picture)
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.translate(cx, cy)
        p.rotate(3.5 * lv * math.sin(t * 1.9 * anim))
        p.drawImage(QRectF(-w / 2.0, -h / 2.0, w, h), picture)
        p.restore()

    @staticmethod
    def _draw_avatar_stage(p: QPainter, cx: float, cy: float, fit: float,
                           lv: float, color: QColor, glow: float) -> None:
        """The soft disc behind the avatar: contrast, and a floor to stand on.

        Sized to the picture's own fit (1.04x, so it peeks out from behind the
        art's edge instead of underlapping it) and lit by the voice, because
        this is part of the same body of light the rim and ring belong to.
        """
        r = fit * 1.04
        if r <= 0.0:
            return
        grad = QRadialGradient(QPointF(cx, cy), r)
        inner = QColor(color).darker(150)
        inner.setAlpha(int(min(255.0, (58 + 70 * lv) * glow)))
        grad.setColorAt(0.0, inner)
        edge = QColor(color)
        edge.setAlpha(int(min(255.0, (22 + 30 * lv) * glow)))
        grad.setColorAt(0.72, edge)
        edge.setAlpha(0)
        grad.setColorAt(1.0, edge)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r, r)

    @staticmethod
    def _draw_avatar_deco(p: QPainter, cx: float, cy: float, lo: float,
                          hi: float, name: str, t: float, lv: float,
                          color: QColor, glow: float, anim: float) -> None:
        """The avatar's decoration: one ANIMATION, chosen by name.

        Painted FIRST, before the picture and the rim, so the avatar always
        sits over its own decoration — a light behind a person, not on them.

        Every decoration lives between `lo` (just outside the picture's fit) and
        `hi` (inside the aperture), so no decoration can fight the picture for
        space or leave the glass: the GEOMETRY is the caller's, and each painter
        only owns its own motion. Motion speed comes from the animation energy
        and the voice brightens and quickens it — the same division of labour
        every design in this file follows.
        """
        if hi <= lo:
            return
        painter = DECORATIONS.get(str(name))
        if painter is None:
            return
        # ONE place decides the decoration's colour, so "the ring is its own
        # colour" is a decision rather than a term each painter has to
        # remember. The painters own their MOTION and nothing else.
        painter(p, cx, cy, lo, hi, t, lv,
                avatar_deco_colour(color, t, lv, anim), glow, anim)

    @staticmethod
    def _deco_ring_light(p: QPainter, cx: float, cy: float, lo: float,
                         hi: float, t: float, lv: float, color: QColor,
                         glow: float, anim: float) -> None:
        """A ring light: two arc pairs, and a dimmer pair counter-rotating.

        A ring LIGHT has to read as light: the base alpha is high enough that
        the arcs are the brightest thing outside the avatar at silence, and the
        voice drives both brightness and rotation from there.
        """
        r = (lo + hi) * 0.5
        width = max(2.2, (hi - lo) * 0.42)
        base = QColor(color).lighter(160)
        base.setAlpha(int(min(255.0, (120 + 90 * lv) * glow)))
        pen = QPen(base, width, Qt.SolidLine, Qt.RoundCap)
        p.save()
        p.translate(cx, cy)
        p.rotate(t * 55.0 * anim * (1.0 + 0.45 * lv))
        p.setBrush(Qt.NoBrush)
        p.setPen(pen)
        # Two arc pairs opposite each other — a ring light's broken segments,
        # not a full circle (that is the rim's job).
        for start in (0.0, 105.0, 180.0, 285.0):
            p.drawArc(QRectF(-r, -r, 2.0 * r, 2.0 * r),
                      int(start * 16.0), int(55.0 * 16.0))
        # The counter-arc pair, dimmer and slightly larger, turning the other
        # way — the second half of the "light" reading.
        outer = r + width * 0.9
        dim = QColor(color).lighter(140)
        dim.setAlpha(int(min(255.0, (70 + 60 * lv) * glow)))
        p.setPen(QPen(dim, width * 0.55, Qt.SolidLine, Qt.RoundCap))
        p.rotate(-2.0 * t * 55.0 * anim * (1.0 + 0.45 * lv))
        for start in (40.0, 220.0):
            p.drawArc(QRectF(-outer, -outer, 2.0 * outer, 2.0 * outer),
                      int(start * 16.0), int(70.0 * 16.0))
        p.restore()

    @staticmethod
    def _deco_orbit(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                    t: float, lv: float, color: QColor, glow: float,
                    anim: float) -> None:
        """Three comets orbiting at different radii, each with a fading tail.

        The tail is drawn as dots falling back along the same orbit rather than
        as a stroked path: a path would have to be built per frame, and the
        comet is a point with a smear, not a shape the eye tracks.
        """
        p.setPen(Qt.NoPen)
        band = hi - lo
        for i in range(3):
            r = lo + band * ((i + 1) / 4.0)
            a = (t * (1.9 - 0.5 * i) * anim * (1.0 + 0.5 * lv)) + i * 2.1
            head = max(1.5, band * 0.16) * (1.0 + 0.3 * lv)
            hot = QColor(color).lighter(205)
            hot.setAlpha(int(min(255.0, (150 + 105 * lv) * glow)))
            p.setBrush(hot)
            p.drawEllipse(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)),
                          head, head)
            for k in range(1, 7):
                fade = (1.0 - k / 7.0) ** 1.6
                rad = head * (0.85 - 0.1 * k)
                if rad <= 0.35:
                    break
                tail = QColor(color).lighter(170)
                tail.setAlpha(int(min(255.0, (120 + 90 * lv) * glow * fade)))
                aa = a - k * 0.17
                p.setBrush(tail)
                p.drawEllipse(QPointF(cx + r * math.cos(aa),
                                      cy + r * math.sin(aa)), rad, rad)

    @staticmethod
    def _deco_pulse(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                    t: float, lv: float, color: QColor, glow: float,
                    anim: float) -> None:
        """Rings travelling outward and fading — a heartbeat around the avatar."""
        band = hi - lo
        p.setBrush(Qt.NoBrush)
        for i in range(3):
            phase = (t * 0.55 * anim * (1.0 + 0.6 * lv) + i / 3.0) % 1.0
            fade = 1.0 - phase
            ring = QColor(color).lighter(180)
            ring.setAlpha(int(min(255.0, (95 + 120 * lv) * glow * fade)))
            width = max(1.2, band * 0.16 * (0.4 + 0.6 * fade))
            p.setPen(QPen(ring, width))
            r = lo + band * phase
            p.drawEllipse(QPointF(cx, cy), r, r)

    @staticmethod
    def _deco_aurora(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                     t: float, lv: float, color: QColor, glow: float,
                     anim: float) -> None:
        """Three ribbons of light drifting around the avatar in slow waves."""
        band = hi - lo
        p.setBrush(Qt.NoBrush)
        for band_i in range(3):
            base_r = lo + band * (0.28 + 0.26 * band_i)
            amp = band * (0.10 + 0.04 * band_i)
            k = 3 + band_i * 2
            speed = (1.3 + 0.5 * band_i) * anim
            path = QPainterPath()
            n = 72
            for i in range(n + 1):
                ang = i * 2 * math.pi / n
                r = base_r + amp * math.sin(k * ang + t * speed * 2.0 + band_i)
                x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            col = QColor(color).lighter(190 if band_i % 2 == 0 else 150)
            col.setAlpha(int(min(255.0, (90 + 95 * lv) * glow)))
            p.setPen(QPen(col, max(1.2, band * 0.13), Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)

    @staticmethod
    def _deco_rainbow(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                      t: float, lv: float, color: QColor, glow: float,
                      anim: float) -> None:
        """A full hue wheel stroked as ONE ring, turning.

        Not a circle in a single colour: the point of a rainbow ring is that
        every angle is a different hue, so it is a CONICAL gradient. The sweep
        is anchored at the decoration's OWN hue, which is what makes this mean
        something under every colour choice instead of ignoring the picker —
        `state` hangs the wheel off the state hue, a custom colour hangs it off
        that colour, and `rainbow` mode feeds it a hue that is already moving.
        The first and last stop are the same colour, so the wheel has no seam.
        """
        band = hi - lo
        r = lo + band * 0.5
        # A BAND, not a hairline: the wheel is the decoration, so the ring
        # fills the space the picture gave up (0.86 of the band at its outer
        # edge) rather than drawing a thin line through the middle of it.
        width = max(2.0, band * 0.72)
        base = QColor(color)
        h0 = max(base.hueF(), 0.0)
        sat = max(0.55, base.hslSaturationF())
        light = min(0.86, max(0.46, base.lightnessF() + 0.10 + 0.10 * lv))
        grad = QConicalGradient(QPointF(cx, cy),
                                -90.0 + t * 40.0 * anim * (1.0 + 0.4 * lv))
        alpha = int(min(255.0, (130 + 95 * lv) * glow))
        for i in range(13):
            f = i / 12.0
            col = QColor.fromHslF((h0 + f) % 1.0, sat, light)
            col.setAlpha(alpha)
            grad.setColorAt(f, col)          # f = 1.0 is h0 again: no seam
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(grad), width, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(QPointF(cx, cy), r, r)

    @staticmethod
    def _deco_sparkle(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                      t: float, lv: float, color: QColor, glow: float,
                      anim: float) -> None:
        """Twinkling points around a faint ring.

        The faint circle is the RING and the points are what the eye follows:
        each pops on its own phase and is dark for most of its cycle, so the
        ring reads as alive rather than as a band that is always lit.
        """
        band = hi - lo
        p.setBrush(Qt.NoBrush)
        faint = QColor(color).lighter(135)
        faint.setAlpha(int(min(255.0, (60 + 45 * lv) * glow)))
        p.setPen(QPen(faint, max(1.0, band * 0.10)))
        p.drawEllipse(QPointF(cx, cy), lo + band * 0.5, lo + band * 0.5)
        p.setPen(Qt.NoPen)
        n = 9
        for i in range(n):
            phase = (t * (0.55 + 0.35 * ((i * 7) % 5) / 4.0) * anim
                     * (1.0 + 0.5 * lv) + i * 0.37) % 1.0
            # a SPIKE, not a sine: nothing for most of the cycle, blazing at
            # the top of it. A slowly breathing dot would be a lamp, not a
            # sparkle.
            w = 1.0 - min(1.0, abs(phase - 0.5) * 4.0)
            if w <= 0.0:
                continue
            ang = i * (2.0 * math.pi / n) + t * 0.25 * anim
            rr = lo + band * (0.30 + 0.40 * ((i * 3) % 4) / 3.0)
            size = max(0.8, band * (0.10 + 0.12 * w))
            hot = QColor(color).lighter(190)
            hot.setAlpha(int(min(255.0, (150 + 105 * lv) * glow * w)))
            p.setBrush(hot)
            p.drawEllipse(QPointF(cx + rr * math.cos(ang),
                                  cy + rr * math.sin(ang)), size, size)

    @staticmethod
    def _deco_comet(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                    t: float, lv: float, color: QColor, glow: float,
                    anim: float) -> None:
        """ONE comet with a long tail, sweeping the ring.

        Deliberately not `orbit`: orbit is three short-tailed points at three
        radii and reads as lights, this is a single streak whose TAIL is the
        point of it, so it reads as motion at a glance. The head rides the
        OUTER part of the band and the tail sweeps back and inward, which is
        what a comet looks like — and it puts the brightest ink at 0.89 of the
        band rather than at the middle, so the decoration uses the room it was
        given instead of crowding the picture. The tail is dots along that arc
        rather than a stroked path: a path would have to be built per frame,
        and it is a smear, not a shape the eye tracks.
        """
        band = hi - lo
        a = t * 2.6 * anim * (1.0 + 0.6 * lv)
        p.setPen(Qt.NoPen)
        for k in range(24, -1, -1):
            f = k / 24.0
            r = lo + band * (0.74 - 0.42 * f)
            rad = max(0.5, band * (0.15 - 0.11 * f))
            head = QColor(color).lighter(int(205 - 65 * f))
            head.setAlpha(int(min(255.0, (150 + 105 * lv) * glow
                                  * (1.0 - f) ** 1.5)))
            p.setBrush(head)
            aa = a - f * 1.5
            p.drawEllipse(QPointF(cx + r * math.cos(aa), cy + r * math.sin(aa)),
                          rad, rad)

    @staticmethod
    def _deco_neon(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                   t: float, lv: float, color: QColor, glow: float,
                   anim: float) -> None:
        """A segmented neon tube with a pulse chasing round it.

        The tube is ALWAYS there, dim, and the chase is what moves — which is
        what makes this read as a lit sign rather than as arcs blinking on and
        off. The chase is one lit segment plus two lagging halos in the same
        colour, so the smear is light falling off instead of a second colour.
        """
        band = hi - lo
        r = lo + band * 0.5
        width = max(2.0, band * 0.40)
        seg, pitch = 22.0, 30.0
        rect = QRectF(cx - r, cy - r, 2.0 * r, 2.0 * r)
        p.setBrush(Qt.NoBrush)
        dim = QColor(color).lighter(120)
        dim.setAlpha(int(min(255.0, (48 + 40 * lv) * glow)))
        p.setPen(QPen(dim, width, Qt.SolidLine, Qt.FlatCap))
        for i in range(max(6, int(360.0 / pitch))):
            p.drawArc(rect, int(i * pitch * 16.0), int(seg * 16.0))
        pos = (t * 0.55 * anim * (1.0 + 0.5 * lv)) % 1.0
        for k in range(3):
            lit = QColor(color).lighter(200 - 25 * k)
            lit.setAlpha(int(min(255.0, (140 + 110 * lv) * glow
                                 * (1.0 - 0.37 * k))))
            p.setPen(QPen(lit, width * (1.0 + 0.4 * k), Qt.SolidLine, Qt.FlatCap))
            start = (pos * 360.0 - k * 26.0) % 360.0
            p.drawArc(rect, int(start * 16.0), int(seg * 16.0))

    @staticmethod
    def _deco_flames(p: QPainter, cx: float, cy: float, lo: float, hi: float,
                     t: float, lv: float, color: QColor, glow: float,
                     anim: float) -> None:
        """Tongues of fire licking up around the avatar.

        Each tongue is a tapered cone whose height flickers on its own phase,
        running to near-white at the tip: fire is the one decoration the eye
        reads as WARMTH, and a flame drawn in one flat tone reads as a spike.
        The tallest tongue the flicker can produce is 0.72 of the band, taken
        from the band's own inner edge — so no position of the animation can
        reach `hi`, by construction rather than by clamping.
        """
        band = hi - lo
        base_r = lo + band * 0.18
        p.setPen(Qt.NoPen)
        n = 11
        for i in range(n):
            ang = i * (2.0 * math.pi / n)
            ph = (t * (1.5 + 0.35 * ((i * 5) % 3)) * anim
                  * (1.0 + 0.5 * lv) + i * 0.7) % 1.0
            # two flames per tongue per cycle, so the ring never looks synced
            flick = 0.5 + 0.5 * math.sin(ph * 4.0 * math.pi + i)
            height = band * (0.26 + 0.46 * flick)
            ca, sa = math.cos(ang), math.sin(ang)
            bx, by = cx + base_r * ca, cy + base_r * sa
            tipx, tipy = cx + (base_r + height) * ca, cy + (base_r + height) * sa
            grad = QLinearGradient(bx, by, tipx, tipy)
            mid = QColor(color).lighter(150)
            mid.setAlpha(int(min(255.0, (120 + 100 * lv) * glow)))
            tip = QColor(color).lighter(235)
            tip.setAlpha(int(min(255.0, (70 + 120 * lv) * glow)))
            grad.setColorAt(0.0, mid)
            grad.setColorAt(1.0, tip)
            p.setBrush(QBrush(grad))
            w = band * 0.26
            waist = base_r + height * 0.55
            path = QPainterPath()
            path.moveTo(bx - w * sa, by + w * ca)
            path.quadTo(cx + waist * ca, cy + waist * sa, tipx, tipy)
            path.quadTo(cx + waist * ca, cy + waist * sa,
                        bx + w * sa, by - w * ca)
            path.closeSubpath()
            p.drawPath(path)

    @staticmethod
    def _draw_image_slot(p: QPainter, cx: float, cy: float, fit: float,
                         t: float, lv: float, anim: float, color: QColor,
                         glow: float) -> None:
        """The empty slot: a dashed ring in the state colour, never an orb.

        A fall back to the orb here would be indistinguishable from a design
        name with no dispatch branch — a defect this tree has already shipped
        once — so "no picture chosen" is drawn as an empty slot, and the actual
        REASON is named by `doctor` and by the settings picker. The dashes
        march with the voice and turn with the animation energy, so the slot
        reacts like every other design rather than sitting there as decoration.
        """
        width = max(2.0, fit * 0.062)
        ring = QColor(color).lighter(150)
        ring.setAlpha(int(min(255.0, (150 + 90 * lv) * glow)))
        pen = QPen(ring, width)
        pen.setCapStyle(Qt.FlatCap)
        pen.setDashPattern([3.0, 1.6])
        pen.setDashOffset(6.0 * lv)              # the voice marches the dashes
        p.save()
        p.translate(cx, cy)
        p.rotate(t * 12.0 * anim)                # ...and the energy turns them
        rr = fit - width * 0.5
        p.setBrush(Qt.NoBrush)
        p.setPen(pen)
        p.drawEllipse(QPointF(0.0, 0.0), rr, rr)
        p.restore()
        # the slot's own mark: a diagonal dash, so the frame reads as EMPTY
        # rather than as a design whose painter failed to run
        mark = QColor(color).lighter(190)
        mark.setAlpha(int(min(255.0, (110 + 120 * lv) * glow)))
        d = fit * 0.34
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(mark, max(1.6, width * 0.55), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx - d, cy + d), QPointF(cx + d, cy - d))

    @staticmethod
    def _draw_image_rim(p: QPainter, cx: float, cy: float, fit: float,
                        lv: float, color: QColor, glow: float,
                        natural: bool = False) -> None:
        """The state-colour rim: why a photo can never hide which state it is.

        A picture may be any colour at all — that is the point of choosing one
        — so the state colour is carried by a ring around the fit circle whose
        alpha grows with the voice, exactly like the orb's halo and for the
        same reason (a body of the user's choosing on a wallpaper of the
        user's choosing). The glow is painted first so the pen lands on it, and
        the pen's outer edge is the outermost ink this design puts down — the
        one thing the aperture budget has to hold.
        """
        width = max(1.5, APERTURE_R * 0.030)
        r = fit - width * 0.5                   # the pen's centreline
        outer = min(APERTURE_R, r + width)
        halo = QRadialGradient(QPointF(cx, cy), outer)
        hc = QColor(color)
        hc.setAlpha(int(min(255.0, (70 + 130 * lv) * glow)))
        if natural:
            # The halo is a RING, not a disc. Filling the disc puts ~27% of the
            # state colour over the picture, which — with the veil — is the
            # repaint "Original colours" exists to stop: a yellow character
            # came out blue even with the wash switched off. Transparent until
            # past the art, bright at the rim, gone at the aperture.
            clear = QColor(hc)
            clear.setAlpha(0)
            halo.setColorAt(0.0, clear)
            halo.setColorAt(0.70, clear)
            halo.setColorAt(1.0, hc)
        else:
            halo.setColorAt(max(0.0, min(1.0, (r - width) / outer)), hc)
            hc.setAlpha(0)
            halo.setColorAt(1.0, hc)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), outer, outer)
        pen = QColor(color).lighter(170)
        pen.setAlpha(int(min(255.0, (150 + 105 * lv) * glow)))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(pen, width + 0.7 * lv))
        p.drawEllipse(QPointF(cx, cy), r, r)

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
        name = design_in_effect()
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


# The decorations, by name: the closed set `avatar_deco()` validates against and
# the painters it dispatches to. ONE table, so a name cannot be valid but
# unimplemented (which would draw nothing, silently) or implemented but invalid
# (which the picker could never select). Defined after the class because the
# painters are its static methods.
DECORATIONS = {
    "ring-light": BubbleWidget._deco_ring_light,
    "orbit": BubbleWidget._deco_orbit,
    "pulse": BubbleWidget._deco_pulse,
    "aurora": BubbleWidget._deco_aurora,
    "rainbow": BubbleWidget._deco_rainbow,
    "sparkle": BubbleWidget._deco_sparkle,
    "comet": BubbleWidget._deco_comet,
    "neon": BubbleWidget._deco_neon,
    "flames": BubbleWidget._deco_flames,
}
