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

# Lines that plausibly set or spawn a wallpaper.  `background`/`wallpaper`
# cover niri's own block and the common shell wrappers.
_WALLPAPER_HINTS = ("swaybg", "swww", "hyprpaper", "feh", "nitrogen",
                    "background", "wallpaper")

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})")


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    """'#4f8cff' -> (79, 140, 255); None when the string is not a hex colour."""
    m = _HEX_RE.match(str(value or "").strip())
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


def _retune(rgb: tuple[int, int, int], shift: float, sat: float) -> tuple[int, int, int]:
    """Push a colour away from grey by `sat` then lift/sink it by `shift`."""
    r, g, b = rgb
    mean = (r + g + b) / 3.0
    out = []
    for c in (r, g, b):
        spread = mean + (c - mean) * sat
        out.append(max(0.0, min(255.0, spread + shift)))
    return out[0], out[1], out[2]


def tune_colors_for_background(colors: dict[str, str], luminance: float,
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
    dark = float(luminance) < 0.5
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


def sample_image_luminance(path: str, *, runner=subprocess.run) -> float | None:
    """Average colour of one image via ImageMagick -> relative luminance.

    Returns None when ImageMagick is missing, the file is unreadable or the
    output is not a colour: detection is a convenience, never a hard failure.
    """
    cmd = ["magick", str(path), "-resize", "1x1", "-format", "%[hex:p{0,0}]", "info:"]
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    raw = (getattr(result, "stdout", "") or "").strip()
    hexcol = raw[:6]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hexcol):
        return None
    return relative_luminance("#" + hexcol)


def detect_wallpaper_luminance(config_file: Path | str, *,
                               runner=subprocess.run,
                               exists=os.path.isfile) -> float | None:
    """Luminance of the configured wallpaper, or None when undetectable."""
    try:
        text = Path(config_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    path = wallpaper_path_from_config(text, exists=exists)
    if not path:
        return None
    return sample_image_luminance(path, runner=runner)
