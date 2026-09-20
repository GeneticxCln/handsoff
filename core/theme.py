"""Bubble palette tuning: make the state colours pop against the desktop.

The Appearance tab lets the user retune the four state colours by hand.  The
one-click "match wallpaper" path needs two things this module owns, both kept
free of Qt so they are testable without a display:

* reading the current wallpaper from the niri config and averaging it with
  ImageMagick (the same external-tool style the rest of the app uses for grim,
  pactl, ydotool — no new Python dependency), and
* retuning a colour dict for a dark or light backdrop: brighten + saturate on
  dark, deepen + saturate on light, so every one of the ten bubble shapes
  reads clearly instead of dissolving into the background.

Every entry point degrades to a no-op/None rather than raising: the settings
app is the recovery tool and must never die because a wallpaper is missing.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Raster extensions worth looking for in a wallpaper command line.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# Video wallpapers are first-class here: this desktop's wallpaper is an mp4
# looped by the shell, and "match wallpaper" that can only read a still image
# is a button that can never do anything for the people who use one. A video is
# sampled by extracting a frame first (ImageMagick cannot read mp4).
_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".gif")
_MEDIA_EXTS = _IMAGE_EXTS + _VIDEO_EXTS

# Lines that plausibly set or spawn a wallpaper.  `background`/`wallpaper`
# cover niri's own block and the common shell wrappers.
_WALLPAPER_HINTS = ("swaybg", "swww", "hyprpaper", "feh", "nitrogen",
                    "background", "wallpaper")

# EXACTLY six digits, anchored at both ends (fullmatch, not match): the old
# unanchored prefix match accepted '#4f8cffXYZ' and silently TRUNCATED an
# 8-digit '#RRGGBBAA' to its first six, so a malformed or alpha colour
# validated as a good 6-digit one and then lost its alpha without a word.
_HEX_RE = re.compile(r"#?([0-9a-fA-F]{6})")


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    """'#4f8cff' -> (79, 140, 255); None when the string is not a hex colour."""
    m = _HEX_RE.fullmatch(str(value or "").strip())
    if not m:
        return None
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def relative_luminance(value: str) -> float | None:
    """WCAG relative luminance (0 = black, 1 = white); None for bad input."""
    rgb = hex_to_rgb(value)
    if rgb is None:
        return None
    parts = []
    for channel in rgb:
        c = channel / 255.0
        parts.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = parts
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _retune(rgb: tuple[int, int, int], shift: float,
            sat: float) -> tuple[float, float, float]:
    """Push a colour away from grey by `sat` then lift/sink it by `shift`."""
    r, g, b = rgb
    mean = (r + g + b) / 3.0
    out = []
    for c in (r, g, b):
        spread = mean + (c - mean) * sat
        out.append(max(0.0, min(255.0, spread + shift)))
    return out[0], out[1], out[2]


def tune_colors_for_background(colors: dict[str, str], luminance: float | None,
                               *, strength: float = 1.0) -> dict[str, str]:
    """Retune `colors` so they stand out against a backdrop of `luminance`.

    Dark backdrop: lift and saturate (a hint of glow reads on near-black).
    Light backdrop: deepen and saturate (a pale tint washes out on white).
    Invalid values pass through untouched, and `strength` 0 is the identity —
    a bad detection can never corrupt the user's palette.
    """
    strength = max(0.0, min(1.0, float(strength)))
    if strength == 0.0:
        return dict(colors)
    try:
        lum = float(luminance)
    except (TypeError, ValueError):
        # detect_wallpaper_luminance returns None on EVERY failure path (no
        # config, no wallpaper file, no ImageMagick, unparseable output), and
        # that None used to reach float() and raise TypeError — breaking the
        # contract this function's own docstring states. Junk is a no-op too.
        return dict(colors)
    dark = lum < 0.5
    shift = (26.0 if dark else -30.0) * strength
    sat = 1.0 + (0.18 if dark else 0.22) * strength
    out: dict[str, str] = {}
    for key, value in colors.items():
        rgb = hex_to_rgb(value)
        out[key] = rgb_to_hex(_retune(rgb, shift, sat)) if rgb else value
    return out


def wallpaper_path_from_config(text: str, *, exists=os.path.isfile) -> str | None:
    """First existing image path on a wallpaper line of a niri config.

    Accepts both quoted (`swaybg -i "~/w.png"`) and bare (`feh --bg-fill w.png`)
    argument styles; `exists` is injectable so tests need no real files.
    """
    for line in str(text or "").splitlines():
        low = line.lower()
        if not any(hint in low for hint in _WALLPAPER_HINTS):
            continue
        for token in re.findall(r'"[^"]+"|\S+', line):
            candidate = token.strip("\"'")
            if candidate.lower().endswith(_IMAGE_EXTS):
                path = os.path.expanduser(candidate)
                if exists(path):
                    return path
    return None


def _is_video(path: str) -> bool:
    return str(path).lower().endswith(_VIDEO_EXTS)


def _video_frame(path: str, *, runner=subprocess.run) -> str | None:
    """One frame of a video wallpaper as a temp PNG, or None.

    `-ss 1` matters: frame 0 of a looping wallpaper is usually a fade-in to
    black, whose "average colour" would tune the palette for a black backdrop.
    A second in, the loop is showing what the user actually looks at, and a
    clip shorter than that still yields its first frame.
    """
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="handsoff-wallpaper-", suffix=".png")
    os.close(fd)
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(path),
           "-frames:v", "1", tmp]
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        result = None
    ok = getattr(result, "returncode", 1) == 0
    try:
        ok = ok and os.path.getsize(tmp) > 0
    except OSError:
        ok = False
    if not ok:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return tmp


def sample_image_luminance(path: str, *, runner=subprocess.run) -> float | None:
    """Average colour of one image or video -> relative luminance.

    Returns None when ImageMagick (or ffmpeg, for a video) is missing, the file
    is unreadable or the output is not a colour: detection is a convenience,
    never a hard failure.
    """
    frame = None
    target = str(path)
    if _is_video(target):
        frame = _video_frame(target, runner=runner)
        if frame is None:
            return None
        target = frame
    cmd = ["magick", target, "-resize", "1x1", "-format", "%[hex:p{0,0}]",
           "info:"]
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        result = None
    finally:
        if frame is not None:
            try:
                os.unlink(frame)
            except OSError:
                pass
    if result is None or getattr(result, "returncode", 1) != 0:
        return None
    raw = (getattr(result, "stdout", "") or "").strip()
    # The '#' is dropped BEFORE the slice: a prefixed "#rrggbb" used to leave
    # '#rrggb', fail the shape check, and read as "no luminance" — a silent
    # false negative for every look that tunes against the wallpaper.
    hexcol = raw.lstrip("#")[:6]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hexcol):
        return None
    return relative_luminance("#" + hexcol)


def _newest_media(directory, *, exists=os.path.isfile) -> str | None:
    """Newest image/video in a directory tree level, or None."""
    best, best_mtime = None, -1.0
    try:
        # The iterator holds a directory fd, so it is closed on EVERY path — an
        # exception while reading the directory otherwise leaves it to the
        # collector, and this runs per state per look. `getattr(it, "close")`
        # rather than `with`: the suite injects a plain list here (the
        # listing seam in tests/test_theme.py), and a bare list is still a
        # perfectly good answer to "what is in this directory".
        it = os.scandir(directory)
        try:
            entries = list(it)
        finally:
            close = getattr(it, "close", None)
            if close is not None:
                close()
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        if not entry.name.lower().endswith(_MEDIA_EXTS):
            continue
        if not exists(entry.path):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = entry.path, mtime
    return best


def _noctalia_directory(home: Path) -> str | None:
    """`wallpaper.directory` from the shell's own settings, if it names one."""
    import json
    try:
        doc = json.loads((home / ".config" / "noctalia" / "settings.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    directory = (doc.get("wallpaper") or {}).get("directory") if isinstance(
        doc.get("wallpaper"), dict) else None
    return str(directory) if directory else None


def wallpaper_candidates(config_file: Path | str | None = None, *,
                         home: Path | str | None = None,
                         exists=os.path.isfile) -> list[str]:
    """Every place this desktop could be keeping its wallpaper, in order.

    A niri config names a wallpaper only when something like swaybg is spawned
    from it; a shell-owned wallpaper (noctalia here) is a directory in the
    shell's settings plus caches, and the wallpaper itself may be a VIDEO.
    Returns existing candidates only, and every candidate is filtered through
    the injected `exists`, so a test can drive the order without touching the
    real filesystem.
    """
    home = Path(home) if home is not None else Path.home()
    found: list[str] = []

    def add(path) -> None:
        if not path:
            return
        text = os.path.expanduser(str(path))
        if exists(text) and text not in found:
            found.append(text)

    if config_file:
        try:
            text = Path(config_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        add(wallpaper_path_from_config(text, exists=exists))

    add(_newest_media(_noctalia_directory(home) or "", exists=exists))
    for sub in ("large", "thumbnails"):
        add(_newest_media(home / ".cache" / "noctalia" / "images" /
                          "wallpapers" / sub, exists=exists))
    # swww keeps one file per output; the newest is the one on screen now.
    try:
        # Closed explicitly, for the same reason as above and with the same
        # tolerance for an injected listing.
        it = os.scandir(home / ".cache" / "swww")
        try:
            for entry in it:
                if entry.is_dir():
                    add(_newest_media(entry.path, exists=exists))
        finally:
            close = getattr(it, "close", None)
            if close is not None:
                close()
    except OSError:
        pass
    try:
        hypr = (home / ".config" / "hypr" / "hyprpaper.conf").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        hypr = ""
    add(wallpaper_path_from_config(hypr, exists=exists))
    return found


def wallpaper_report(config_file: Path | str | None = None, *,
                     runner=subprocess.run, home: Path | str | None = None,
                     exists=os.path.isfile) -> dict:
    """What "match wallpaper" found: path, luminance, and everything tried.

    A single luminance cannot explain itself: a user who clicks the button and
    sees nothing needs to know WHICH file was sampled (or that none was found),
    so `checked` is part of the answer rather than a log line somewhere.
    """
    checked = wallpaper_candidates(config_file, home=home, exists=exists)
    for path in checked:
        luminance = sample_image_luminance(path, runner=runner)
        if luminance is not None:
            return {"path": path, "luminance": luminance, "checked": checked}
    return {"path": None, "luminance": None, "checked": checked}


def detect_wallpaper_luminance(config_file: Path | str | None = None, *,
                               runner=subprocess.run, home=None,
                               exists=os.path.isfile) -> float | None:
    """Luminance of the current wallpaper, or None when undetectable."""
    return wallpaper_report(config_file, runner=runner, home=home,
                            exists=exists)["luminance"]
