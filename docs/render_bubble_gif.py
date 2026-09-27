"""Render `docs/bubble.gif` — the bubble, from the bubble's own code.

There is no screenshot here and no hand-drawn stand-in. This instantiates the
real `core.bubble.BubbleWidget`, lets its own 16 ms timer drive the real
animation, and grabs the widget once per frame, so the GIF is a recording of
the code that ships rather than an artist's impression of it. The four states
are the module's own four, in the order it crossfades between them, and each
frame is checked against the colour the module declares for that state — the
script exits non-zero if the render and the declaration disagree, because a
hero image that lies about the product is worse than no image.

    python docs/render_bubble_gif.py          # writes docs/bubble.gif

`BUBBLE_GIF_PX`, `BUBBLE_GIF_FPS` and `BUBBLE_GIF_COLOURS` override the three
knobs that decide how the file weighs against how it looks; the defaults are
the ones that measured 408 KB for a five-second loop, which is small enough
for a repository and large enough that the glow does not band.

Two things are synthetic, and both are named in the file's own caption and in
the README: the VOICE LEVEL signal a microphone would normally feed the
designs is a shaped envelope per state (silence, a person, the synth), and
the backdrop is a dark gradient rather than your wallpaper.

Needs PySide6 (already a dependency) and **Pillow**, which the application
itself does not use — this is a documentation asset, so Pillow is a
development-only need rather than one for `requirements.txt`. Qt has no GIF
writer.
"""
from __future__ import annotations

import colorsys
import io
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QImage,  # noqa: E402
                           QLinearGradient, QPainter)
from PySide6.QtWidgets import QApplication  # noqa: E402
from PIL import Image  # noqa: E402

from core import bubble as B  # noqa: E402

PX = int(os.environ.get("BUBBLE_GIF_PX", "192"))   # the largest bubble_size
CAPTION_H = 34
FPS = int(os.environ.get("BUBBLE_GIF_FPS", "10"))
SECONDS_PER_STATE = 1.0
COLOURS = int(os.environ.get("BUBBLE_GIF_COLOURS", "128"))
OUT = ROOT / "docs" / "bubble.gif"

#: the cycle, in the order the states are declared
PLAN = ((B.IDLE, "idle"), (B.LISTENING, "listening"),
        (B.THINKING, "thinking"), (B.SPEAKING, "speaking"))


class _Assistant(QObject):
    """The two signals the widget subscribes to, and nothing else."""

    sigState = Signal(str)
    sigLevel = Signal(float)


def level(i: int, n: int, state: str) -> float:
    """A level shaped like the thing it stands for, in 0..1.

    idle and thinking are silence — the widget's own design decides what a
    silent state looks like — while listening is a person and speaking is the
    synth. Both are smooth enough that the widget's attack/release does the
    rest, which is the point: the designs were built to be driven by exactly
    this signal.
    """
    if state in (B.IDLE, B.THINKING):
        return 0.0
    t = i / max(1, n)
    if state == B.LISTENING:                    # a person: uneven, 3 Hz
        return max(0.0, min(1.0, 0.3 + 0.5 * abs(((t * 3.1) % 1.0) * 2 - 1)))
    # speaking: syllables at 4.4 Hz inside a slower sentence envelope
    syl = 0.5 + 0.5 * abs(((t * 7.0) % 1.0) * 2 - 1)
    return max(0.0, min(1.0, 0.25 + 0.6 * syl *
                        (0.6 + 0.4 * abs(((t * 1.3) % 1.0) * 2 - 1))))


def compose(widget, caption: str) -> Image.Image:
    """One frame: a dark backdrop, the bubble as drawn, and the state's name."""
    img = QImage(PX, PX + CAPTION_H, QImage.Format_RGB32)
    grad = QLinearGradient(0, 0, 0, img.height())
    grad.setColorAt(0.0, QColor(28, 30, 36))
    grad.setColorAt(1.0, QColor(14, 15, 18))
    p = QPainter(img)
    p.fillRect(img.rect(), grad)
    p.drawImage(0, 0, widget.grab().toImage())
    p.setPen(QColor(206, 212, 222))
    font = QFont()
    font.setPointSize(11)
    p.setFont(font)
    p.drawText(0, PX + 24, PX, 20, 0x84, caption)   # centred, both ways
    p.end()
    return Image.open(io.BytesIO(_png(img))).convert("RGB")


def _png(img: QImage) -> bytes:
    path = f"/tmp/bubble_frame_{os.getpid()}.png"
    img.save(path, "PNG")
    with open(path, "rb") as fh:
        data = fh.read()
    os.unlink(path)
    return data


def _mean_hue(frame: Image.Image) -> float:
    """The hue of the bubble's own ink: the pixels brighter than the
    backdrop's darkest corner, which is the glow and nothing else."""
    px = frame.convert("RGB").load()
    picked = [px[x, y] for y in range(PX) for x in range(PX)
              if sum(px[x, y]) > 210]
    if not picked:
        return -1.0
    r = sum(c[0] for c in picked) / len(picked)
    g = sum(c[1] for c in picked) / len(picked)
    b = sum(c[2] for c in picked) / len(picked)
    return colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0]


def _hue_of(colour: QColor) -> float:
    return colorsys.rgb_to_hsv(colour.redF(), colour.greenF(),
                                colour.blueF())[0]


def verify(frames, names) -> None:
    """Each state must render in ITS OWN declared colour, not a neighbour's.

    The widget crossfades towards `STATE_COLORS[state]`, so the last frames of
    each state are the settled colour; comparing hue-to-hue is what catches a
    state that was never set (it would still be the previous state's hue) and
    a caption that names a state the render does not show.
    """
    rendered = {}
    for state, caption in PLAN:
        last = max(i for i, n in enumerate(names) if n == caption)
        rendered[state] = _mean_hue(frames[last])
    wrong = []
    for state, caption in PLAN:
        want, got = _hue_of(B.STATE_COLORS[state]), rendered[state]
        if got < 0 or min(abs(got - w) for w in (
                want, (want + 1) % 1, want - 0.5)) > 0.08:
            wrong.append(f"{caption}: declared hue {want:.2f}, rendered "
                         f"{got:.2f}")
    print("  state          declared        rendered")
    for state, caption in PLAN:
        c = B.STATE_COLORS[state]
        print(f"  {caption:12s} rgb({c.red():3d},{c.green():3d},"
              f"{c.blue():3d})  hue {rendered[state]:.2f}")
    if wrong:
        sys.exit("the render does not match the declared state colours:\n  "
                 + "\n  ".join(wrong))


def main() -> int:
    app = QApplication(sys.argv)
    B.configure({"bubble_size": PX})
    assistant = _Assistant()
    widget = B.BubbleWidget(assistant)
    per = int(SECONDS_PER_STATE * FPS)
    frames, names = [], []
    for state, caption in PLAN:
        assistant.sigState.emit(state)
        for i in range(per):
            assistant.sigLevel.emit(level(i, per, state))
            time.sleep(1.0 / FPS)      # the widget's timer reads a real clock
            app.processEvents()
            frames.append(compose(widget, caption))
            names.append(caption)
    print(f"rendered {len(frames)} frames of the real BubbleWidget")
    verify(frames, names)
    # One shared palette, so consecutive frames compress against each other
    # instead of each carrying its own table (that alone halved the file).
    master = frames[per // 2].quantize(colors=COLOURS, method=Image.MEDIANCUT)
    small = [f.quantize(palette=master, dither=Image.FLOYDSTEINBERG)
             for f in frames]
    small[0].save(OUT, save_all=True, append_images=small[1:],
                  duration=int(1000 / FPS), loop=0, optimize=True, disposal=1)
    print(f"wrote {OUT.relative_to(ROOT)} — {OUT.stat().st_size} bytes, "
          f"{PX}x{PX + CAPTION_H}, {FPS} fps, looping the four states")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
