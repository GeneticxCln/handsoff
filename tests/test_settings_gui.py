"""Offscreen Qt tests for the Settings GUI (handsoff-settings.py).

The settings GUI is the least-covered shipped module and the reason the
suite-wide coverage floor sits at 60 instead of 70. These tests drive the
REAL SettingsWindow through chunky end-to-end scenarios — save/load,
memory/history rendering, keybind snippets, disk-reload, the health
status line, and the per-tool policy rows.

Why subprocesses: in-process construction of the settings window aborts
the whole suite when any earlier test module left a QApplication or live
bubble threads behind (the exact hazard TestSettingsHistoryTab documents
in test_settings.py). Each scenario here therefore runs in a fresh child
process with QT_QPA_PLATFORM=offscreen and every path redirected to a
tmp HOME — no real config, no real bubble, no network, no systemd.
Coverage measures these lines because the child re-executes the module
under the same interpreter and coverage.py's subprocess patching (via
COVERAGE_PROCESS_START + .pth hook) attributes it to this run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HERE, run_driver

GUI_DRIVER = """
import copy
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

# --- offscreen Qt + isolated env: FIRST, before any Qt import
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_PLATFORMTHEME"] = ""
os.environ["NO_AT_BRIDGE"] = "1"
os.environ["QT_ACCESSIBILITY"] = "0"
os.environ["HANDSOFF_PYTHON"] = sys.executable

# Subprocess coverage: pytest-cov 7 dropped the automatic .pth hook, so the
# driver engages measurement itself when the parent run exported
# COVERAGE_PROCESS_START (no-op otherwise; idempotent if a .pth hook already
# started coverage). Data save happens via atexit on the sys.exit below.
try:
    import coverage
    coverage.process_startup()
except Exception:
    pass

home = Path(os.environ["SGUI_HOME"])
config_dir = home / ".config" / "handsoff"
state_dir = home / ".local" / "state" / "handsoff"
config_dir.mkdir(parents=True, exist_ok=True)
state_dir.mkdir(parents=True, exist_ok=True)
niri_dir = home / ".config" / "niri"
niri_dir.mkdir(parents=True, exist_ok=True)
clip_dir = config_dir / "voice-clips"
clip_dir.mkdir(parents=True, exist_ok=True)

import wave as _wave


def _clip(path, seconds, sr=24000):
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\\x00\\x00" * int(seconds * sr))
    return path


# 2 s is exactly the case that used to look fine in the GUI and then made the
# engine raise on EVERY turn (it requires > 5 s); 7 s is a usable reference.
short_clip = _clip(clip_dir / "optimus_clip.wav", 2.0)
ok_clip = _clip(clip_dir / "optimus.wav", 7.0)
(NIRI := niri_dir / "config.kdl").write_text("// niri config\\n")

import importlib.util


def load(name, path):
    # The app goes through its own loader, so this child ends up with ONE bubble
    # under the canonical name the settings app also looks for. A hand-built
    # spec plus exec_module would leave the module unnamed, which the app now
    # refuses (an unnamed load is indistinguishable from a second copy).
    if os.path.basename(path) == "handsoff.py":
        from core import load_app_module
        return load_app_module([path])
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # named BEFORE exec: never an unnamed load
    spec.loader.exec_module(mod)
    return mod


HERE = os.environ["SGUI_HERE"]
settings_app = load("handsoff_settings_gui", os.path.join(HERE, "handsoff-settings.py"))
bubble = load("handsoff_core", os.path.join(HERE, "handsoff.py"))

# --- point the settings app's lazy H proxy at THIS bubble exec, then
# --- rebind every path constant to tmp BEFORE any window is built
settings_app.H.__dict__["_bubble"] = bubble
import threading
settings_app.H.__dict__["_lock"] = threading.Lock()

settings_file = config_dir / "settings.json"
history_file = config_dir / "history.json"
memory_file = config_dir / "memory.json"
decisions_file = state_dir / "decisions.jsonl"
bubble.SETTINGS_FILE = settings_file
bubble.CONFIG_DIR = config_dir
bubble.STATE_DIR = state_dir
bubble.HISTORY_FILE = history_file
bubble.MEMORY_FILE = config_dir / "memory.json"
bubble.DECISIONS_FILE = state_dir / "decisions.jsonl"
bubble.LOG_FILE = state_dir / "handsoff.log"
bubble.CONTROL_SOCK = state_dir / "control.sock"
bubble._audio.tts_weights_cached = lambda: True
bubble.RESTART_SCRIPT = home / "absent" / "handsoff-restart"
bubble.SETTINGS = copy.deepcopy(bubble.DEFAULT_SETTINGS)

import core.settings
bubble._SETTINGS_OBJ = core.settings.settings_object(settings_file, config_dir)

# core/bubble.py owns the window size, the geometry and the palette, so the
# tests reach them through the module that owns them. The app handle above is
# still where the app's own globals live.
appearance = bubble._core_bubble
appearance.SETTINGS = bubble.SETTINGS          # the deepcopy above replaced it
appearance.RESTART_SCRIPT = bubble.RESTART_SCRIPT

settings_app.HOME = home
settings_app.NIRI_CONFIG = NIRI

# --- stub every side door: network, systemd, niri reload, xdg-open


def _no_http(url, payload=None, timeout=10):
    raise urllib.error.URLError(f"stubbed in GUI tests ({url})")


class _Result:
    returncode = 1
    stdout = ""
    stderr = ""


# The REAL helper, kept before the stub replaces the module attribute: the
# round-trip scenario has to call something that actually opens a socket, and
# reaching for `settings_app.http_json` after the stub is reaching for the stub.
real_http_json = settings_app.http_json
settings_app.http_json = _no_http
settings_app.systemd_owns_autostart = lambda: False
settings_app._reload_niri = lambda: None
settings_app.subprocess.run = lambda *a, **k: _Result()
settings_app.subprocess.Popen = lambda *a, **k: None

# The app's own line, so a change to what the bubble is asked to say cannot
# leave this driver asserting a sentence the app no longer sends.
_VOICE_TEST_LINE = settings_app._VOICE_TEST_LINE

# --- seed disk state the scenarios read


def seed(settings=None, history=None):
    settings_file.write_text(json.dumps(settings or {}), encoding="utf-8")
    if history is None:
        history_file.unlink(missing_ok=True)
    else:
        history_file.write_text(json.dumps(history), encoding="utf-8")


from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

app = QApplication([])
win = settings_app.SettingsWindow()

SCENARIOS = {}
TESTS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _spin_health(win, want="querying", timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and want in win.health_label.text():
        app.processEvents()
        time.sleep(0.05)


@scenario
def save_roundtrip():
    seed({"model": "testmodel:latest", "mic_threshold": 700,
          "allow_remote_ollama": True})
    win.reload_from_disk()
    win.ctx_spin.setValue(8192)
    win.thresh_spin.setValue(750)
    win.wake_name_edit.setText("cypher")
    assert win.save() is True
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["num_ctx"] == 8192
    assert on_disk["mic_threshold"] == 750
    assert on_disk["assistant_name"] == "cypher"
    assert on_disk["model"] == "testmodel:latest"
    assert on_disk["allow_remote_ollama"] is True
    assert "Saved to" in win.status_label.text()


@scenario
def save_refuses_without_model():
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    # coercion fills an empty model from defaults, so the refusal path is
    # reached by blanking the in-memory cfg the way a degenerate H would
    win.cfg["model"] = ""
    assert win.save() is False
    assert "pick a model first" in win.status_label.text()


@scenario
def design_change_applies_on_sparse_settings():
    # Regression: a settings.json that predates bubble_design (the key is
    # simply absent on disk) must accept a newly picked shape, and the saved
    # value must land on disk so the bubble repaints it.
    seed({"model": "testmodel:latest", "bubble_size": 97})
    win.reload_from_disk()
    idx = win.design_combo.findData("saturn")
    assert idx >= 0
    win.design_combo.setCurrentIndex(idx)
    assert win.save() is True
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["bubble_design"] == "saturn"
    assert "Applied live" in win.status_label.text() or \
        "Bubble unreachable" in win.status_label.text()


@scenario
def disk_change_does_not_reset_unsaved_edits():
    # Regression: the bubble writes settings.json on its own (notification
    # auto-mute, handsfree toggle). The 2s disk poll must not reset widgets
    # the user has touched since the last save — that silently reverted
    # Appearance picks (the "bubble shape does not apply" bug).
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt as _Qt
    seed({"model": "testmodel:latest", "bubble_size": 97})
    win.reload_from_disk()
    idx = win.design_combo.findData("saturn")
    win.design_combo.setCurrentIndex(idx)
    # a real interaction marks the form dirty through the app-wide filter
    win.show()
    app.processEvents()
    QTest.mouseClick(win, _Qt.MouseButton.LeftButton)
    app.processEvents()
    assert win._user_edited is True
    # meanwhile the bubble persists something unrelated to disk
    seed({"model": "testmodel:latest", "bubble_size": 96})
    win._check_disk_changes()
    # the user's pick survives and the status bar explains the hold
    assert win.design_combo.currentData() == "saturn"
    assert "keeping your edits" in win.status_label.text()
    # an explicit reload still brings disk state back
    win.reload_from_disk()
    assert win.design_combo.currentData() == "orb"
    assert win._user_edited is False


@scenario
def appearance_energy_and_accent_roundtrip():
    # the two new Appearance knobs must survive save/load like every other key,
    # and the live preview must paint from whatever the sliders report
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    win.energy_slider.setValue(160)
    win.accent_slider.setValue(80)
    assert win.energy_label.text() == "1.6×"
    assert win.accent_label.text() == "80%"
    assert win.save() is True
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["animation_energy"] == 1.6
    assert on_disk["bubble_accent"] == 0.8
    win.reload_from_disk()
    assert win.energy_slider.value() == 160
    assert win.accent_slider.value() == 80
    win.preview.resize(420, 160)
    assert not win.preview.grab().isNull()


@scenario
def appearance_changes_apply_without_save():
    # The Appearance tab promises live application and the bubble watches
    # settings.json, so picking a shape must reach disk — and the bubble — with
    # no Save click. The old code wrote nothing until Save: the preview updated
    # (it reads the widgets directly) while the bubble never changed, which the
    # user experiences as "no matter what shape I choose, it never saves".
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    notified = []
    win._notify_bubble_reloaded = lambda: (notified.append(1), True)[1]

    current = win.design_combo.currentData()
    designs = list(getattr(settings_app.SCHEMA, "BUBBLE_DESIGNS", ("orb",)))
    target = next(d for d in designs if d != current)
    win.design_combo.setCurrentIndex(win.design_combo.findData(target))
    win._apply_appearance_live()          # what the debounce timer calls
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["bubble_design"] == target, (
        "a picked shape must persist without pressing Save, got "
        f"{on_disk.get('bubble_design')!r}")
    assert notified, "the live apply must tell the running bubble to repaint"

    # a slider drag writes too, so the sliders are not decoration
    win.energy_slider.setValue(160)
    win._apply_appearance_live()
    assert json.loads(settings_file.read_text())["animation_energy"] == 1.6

    # ...but loading the form is NOT an edit: no write, no bubble poke. This is
    # the guard that keeps a window open (and the 2 s disk poll reloading it)
    # from rewriting settings.json on its own.
    writes = []
    real_save = win.save
    win.save = lambda: (writes.append(1), real_save())[1]
    win.reload_from_disk()
    win._apply_appearance_live()
    assert writes == [], "loading the form must not trigger a live save"
    win.save = real_save


@scenario
def colour_change_applies_without_save():
    # The user's "the colours don't apply". _apply_appearance_live only wrote
    # when one of APPEARANCE_KEYS differed from disk, and `colors` was not in
    # that tuple — so an edit that moved ONLY a colour was read as "a load, not
    # an edit" and silently never reached settings.json. The bubble was fine:
    # it has always rebuilt STATE_COLORS from settings["colors"] on reload.
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    win._colors["idle"] = "#ff00ff"
    win._paint_color_buttons()
    win._apply_appearance_live()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["colors"]["idle"] == "#ff00ff", on_disk["colors"]
    assert "state colours" in win.status_label.text(), win.status_label.text()


@scenario
def malformed_state_colours_rejected_at_entry():
    # A hand-edited settings.json is the only way a malformed state colour can
    # arrive (the dialog yields '#rrggbb'). It used to be drawn on the swatch
    # as though it were in effect while the bubble's parser discarded it, so
    # the GUI said one thing and the bubble did another. Admission now uses the
    # parser's own predicate: a refusal is named out loud and the default is
    # used, and a valid colour is normalised to the form the swatch can render.
    seed({"model": "testmodel:latest",
          "colors": {"idle": "blue", "listening": "#4f8cffXYZ",
                     "thinking": "#12345", "speaking": "00ff00"}})
    win.reload_from_disk()
    defaults = settings_app.DEFAULT_SETTINGS["colors"]
    assert win._colors["idle"] == defaults["idle"], win._colors
    assert win._colors["listening"] == defaults["listening"], win._colors
    assert win._colors["thinking"] == defaults["thinking"], win._colors
    assert win._colors["speaking"] == "#00ff00", win._colors
    message = win.status_label.text()
    assert "rejected 3 malformed state colour(s)" in message, message
    assert "#rrggbb" in message, message
    assert "listening='#4f8cffXYZ'" in message, message
    # The refused value never rides along into the file: the next save writes
    # the default, so the mismatch cannot be rediscovered on every reload.
    assert win.save() is True
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["colors"]["idle"] == defaults["idle"], on_disk["colors"]
    assert on_disk["colors"]["thinking"] == defaults["thinking"]
    assert on_disk["colors"]["speaking"] == "#00ff00", on_disk["colors"]


@scenario
def size_slider_applies_without_save():
    # The size slider only ever relabelled itself: valueChanged was wired to a
    # label update and nothing else, so the bubble never resized live.
    seed({"model": "testmodel:latest", "bubble_size": 97})
    win.reload_from_disk()
    win.size_slider.setValue(150)
    win._apply_appearance_live()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["bubble_size"] == 150
    assert "size" in win.status_label.text(), win.status_label.text()


@scenario
def loading_the_form_is_still_not_an_edit():
    # The guard that keeps the 2s disk poll from rewriting settings.json must
    # survive adding `colors`/`bubble_size` to the compared set: a plain reload
    # reproduces the disk values, so nothing is written.
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    assert win.save() is True                 # normalise the file once
    before = settings_file.read_text(encoding="utf-8")
    writes = []
    real_save = win.save
    win.save = lambda: (writes.append(1), real_save())[1]
    win.reload_from_disk()                    # what the disk poll does
    win._apply_appearance_live()              # the timer that reload starts
    win.save = real_save
    assert writes == [], "a reload must not be mistaken for an edit"
    assert settings_file.read_text(encoding="utf-8") == before
    # ...and that must hold for a file written by an OLDER build, which has no
    # entry at all for the appearance keys added since. Compared against the raw
    # JSON such a key differs from the widget's default for a reason that is not
    # an edit, so a plain reload announced "Applied live: ..." and rewrote the
    # file — a message describing an edit nobody made.
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    raw = settings_file.read_text(encoding="utf-8")
    writes = []
    win.save = lambda: (writes.append(1), real_save())[1]
    win._apply_appearance_live()
    win.save = real_save
    assert writes == [], (
        "a settings.json without the newer keys was read as an edit")
    assert settings_file.read_text(encoding="utf-8") == raw


@scenario
def wallpaper_tuning_buttons_retune_palette():
    # offline: detection fails cleanly and must not touch the palette, while the
    # explicit Dark/Light buttons still retune it without a wallpaper or magick
    import types
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    before = dict(win._colors)
    win._match_wallpaper()
    # The refusal must say what it LOOKED AT, not just that it failed: the
    # button used to consult the niri config alone and blame ImageMagick, which
    # was installed all along, while the real wallpaper was a shell-owned video.
    assert "could not sample the wallpaper" in win.status_label.text()
    assert "looked at" in win.status_label.text()
    assert win._colors == before
    win._apply_wallpaper_tuning(0.05)
    assert win._colors != before
    dark = dict(win._colors)
    assert "dark backdrop" in win.status_label.text()
    win._apply_wallpaper_tuning(0.95)
    assert win._colors != dark
    assert "light backdrop" in win.status_label.text()
    # detected path: a stub detector must actually drive the tuning
    real = settings_app._THEME
    settings_app._THEME = types.SimpleNamespace(
        tune_colors_for_background=real.tune_colors_for_background,
        wallpaper_report=lambda *_a, **_k: {"path": "/w/from-shell.mp4",
                                            "luminance": 0.02,
                                            "checked": ["/w/from-shell.mp4"]})
    try:
        win._colors = dict(before)
        win._match_wallpaper()
        assert win._colors != before
        # ...and it NAMES the file it sampled, so "nothing happened" and
        # "it read the wrong wallpaper" cannot look the same.
        assert "from-shell.mp4" in win.status_label.text(), \
            win.status_label.text()
        assert "0.02" in win.status_label.text()
    finally:
        settings_app._THEME = real
    # and the retuned palette persists through save
    assert win.save() is True
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["colors"] == win._colors


@scenario
def bubble_designs_render_at_energy_extremes():
    # all ten designs share one frame state, so render every design in every
    # state colour at both animation-energy and accent extremes: a broken paint
    # branch or a bad clamp fails here instead of on the desktop
    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        # the widget only wires two signals and stores itself back on the
        # assistant, so a bare stub is enough to exercise every paint branch
        sigState = _Signal()
        sigLevel = _Signal()

    widget = appearance.BubbleWidget(_Stub())
    widget.resize(appearance.WINDOW_PX, appearance.WINDOW_PX)
    from PySide6.QtGui import QImage

    def _pixels():
        # The widget's paint, onto a surface we cleared ourselves. grab()
        # reads the platform's backing store, which is not a valid instrument
        # for a WA_TranslucentBackground widget -- the background is never
        # erased -- the same conclusion that moved the overflow sweep onto a
        # cleared QImage. It also made this measurement depend on the
        # container's surface behaving like the developer's: in CI all 24
        # design/slider pairs reported zero changed pixels on the same Qt
        # version that passes here.
        img = QImage(widget.size(), QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        return bytes(img.constBits())

    designs = list(getattr(settings_app.SCHEMA, "BUBBLE_DESIGNS", ("orb",)))
    states = ("idle", "listening", "thinking", "speaking")
    painted = 0
    for design in designs:
        bubble.SETTINGS["bubble_design"] = design
        for state in states:
            widget._state = state
            base = appearance.STATE_COLORS[state]
            widget._color_ui = [base.redF(), base.greenF(), base.blueF()]
            for energy in (0.2, 1.0, 2.0):
                appearance.ANIM_ENERGY = energy
                for accent in (0.0, 1.0):
                    appearance.BUBBLE_ACCENT = accent
                    assert not widget.grab().isNull()
                    painted += 1
    assert painted == len(designs) * len(states) * 3 * 2

    # The regression that actually matters, and the one this scenario was
    # missing: every design must visibly MOVE when either slider moves.
    # Asserting only that _frame() carries the numbers passed happily while
    # bubble_accent changed literally zero pixels on reactor, droplet and void
    # (<0.1% on five more) and animation_energy did nothing to the idle
    # equalizer -- because only the orb and halo painters ever called the one
    # accent-aware helper. That failure is exactly the user's "on Appearance
    # only the shapes apply".
    class _FrozenClock:
        # a fixed t: without this the orange is dominated by animation phase
        # drift between the two grabs and the measurement is meaningless
        def elapsed(self):
            return 4000

    def _changed(design, key, lo, hi):
        widget._clock = _FrozenClock()
        widget._state = "idle"
        bubble.SETTINGS["bubble_design"] = design
        widget._energy_ui = 0.5
        appearance.ANIM_ENERGY = 1.0
        appearance.BUBBLE_ACCENT = 0.5
        # The knobs live on the bubble module, not the app: setting them on the
        # app left the painters reading their untouched defaults, which made
        # every design look like it ignored both sliders.
        setattr(appearance, key, lo)
        before = _pixels()
        setattr(appearance, key, hi)
        after = _pixels()
        n = min(len(before), len(after))
        return sum(1 for i in range(0, n - 3, 4)
                   if any(x != y for x, y in zip(before[i:i + 3], after[i:i + 3])))

    counts = {f"{d}/{k}": _changed(d, k, lo, hi)
              for d in designs
              for k, lo, hi in (("BUBBLE_ACCENT", 0.0, 1.0),
                                ("ANIM_ENERGY", 0.2, 2.0))}
    weak = [name for name, n in counts.items() if n < 100]
    if weak:
        # Say WHICH instrument failed to see anything: a surface that paints
        # nothing at all is a different problem from one design ignoring a
        # slider, and the two have been confused once already.
        blank = sum(1 for b in _pixels() if b)
        raise AssertionError(
            # all one line on purpose: this text lives inside GUI_DRIVER, so an
            # escaped newline here would be a real newline in the generated
            # source and an unterminated string literal there
            "slider changes no visible pixels on: " + ", ".join(weak) +
            f" | widget {widget.size().width()}x{widget.size().height()}"
            f" | non-zero bytes in a cleared render: {blank}"
            f" | changed-pixel counts: {sorted(set(counts.values()))[:8]}")

    # ...and every design must be its OWN painting. A name in BUBBLE_DESIGNS
    # with no dispatch branch falls through to the orb, so the combo would
    # offer a "shape" that is really the orb (the preview glyph has the same
    # trap, pinned by every_design_has_its_own_preview_glyph).
    widget._clock = _FrozenClock()
    widget._level_ui = widget._level_target = 0.0
    widget._radius_ui = appearance.BUBBLE_R0
    widget._state = "idle"
    bubble.SETTINGS["bubble_design"] = "orb"
    orb_pixels = _pixels()
    same_as_orb = []
    for design in designs:
        if design == "orb":
            continue
        bubble.SETTINGS["bubble_design"] = design
        if _pixels() == orb_pixels:
            same_as_orb.append(design)
    assert same_as_orb == [], (
        "these designs paint exactly like the orb (no dispatch branch): "
        + ", ".join(same_as_orb))

    # the neutral defaults must reproduce the historical framing exactly.
    # _energy_ui is a smoothed chase (it converges during _on_tick), so pin it
    # to the target rather than hoping the event loop got there: the frame's
    # job is to report the converged value, which is what is asserted here.
    appearance.ANIM_ENERGY = 1.0
    appearance.BUBBLE_ACCENT = 0.5
    widget._state = "idle"
    widget._energy_ui = appearance._fx_energy("idle")
    frame = widget._frame()
    assert frame["anim"] == 1.0
    assert frame["accent"] == 0.5
    assert abs(frame["energy"] - appearance._fx_energy("idle")) < 1e-9

    # ...and the slider really moves the target every design reads: monotonic
    # in animation energy, clamped to a sane glow range. (Deliberately stated
    # as a property rather than exact numbers, so a retuned curve is fine.)
    energies = []
    for knob in (0.2, 1.0, 2.0):
        appearance.ANIM_ENERGY = knob
        energies.append(appearance._fx_energy("idle"))
    assert energies == sorted(energies), "more energy must never dim the glow"
    assert all(0.0 <= e <= 1.0 for e in energies), "glow energy must stay bounded"
    appearance.ANIM_ENERGY = 1.0


@scenario
def sauron_eye_reacts_to_voice_level():
    # The Eye of Sauron must react to the voice the way the equalizer bars do:
    # the pupil narrows and the fire flares as the level rises. `level` is the
    # one signal both share — the mic while listening, and (since the mic is
    # blanked during TTS) core.audio.play_wav's playback level while speaking.
    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _FrozenClock:
        def elapsed(self):
            return 4000

    widget = appearance.BubbleWidget(_Stub())
    widget.resize(appearance.WINDOW_PX, appearance.WINDOW_PX)
    widget._clock = _FrozenClock()
    # Stop the 16 ms timer: grab() drains the event queue, so an active timer
    # runs _on_tick between setting `_level_ui` and painting it, chasing the
    # level back toward `_level_target` (0 by default) and making the measured
    # pupil width depend on how many ticks happened to fire. That flake is why
    # this scenario could fail as "4 -> 2" on code that had not changed.
    widget._anim.stop()
    widget._last_tick = 4.0
    widget._state = "idle"
    bubble.SETTINGS["bubble_design"] = "sauron"
    appearance.ANIM_ENERGY = 1.0
    appearance.BUBBLE_ACCENT = 0.5

    def grab(level):
        widget._level_target = level      # nothing left to chase
        widget._level_ui = level
        return widget.grab().toImage()

    def pupil_width(level):
        # Widest near-black run ENCLOSED within the lit iris, over the rows that
        # actually cross the eye. Deliberately geometric rather than a fixed
        # pixel offset, and with an ABSOLUTE cut — three traps here, all hit
        # while writing this: a cut relative to the row's peak drifts with the
        # flare and so "measures" the flare; a row through the exact centre sits
        # on the hot filament and reads bright; and a fixed offset lands on
        # black background as soon as the widget is a different size.
        img = grab(level)
        W, H = img.width(), img.height()
        cx = W // 2
        # A CENTRAL window, not the whole row: once the flames brighten with the
        # level they become the outermost lit pixels, so "everything between the
        # lit pixels" would enclose the dark gap outside the iris and report the
        # pupil getting WIDER under the voice.
        half = max(12, W // 6)
        best = 0
        for y in range(H // 4, 3 * H // 4):
            lum = []
            for x in range(cx - half, cx + half):
                c = img.pixelColor(x, y)
                lum.append(0.299 * c.red() + 0.587 * c.green()
                           + 0.114 * c.blue())
            lit = [i for i, v in enumerate(lum) if v >= 150]
            if len(lit) < 2:
                continue                     # not a row through the iris
            lo, hi = min(lit), max(lit)      # the lit iris span on this row
            inside = sum(1 for i in range(lo, hi + 1) if lum[i] < 45)
            best = max(best, inside)
        return best

    # The property is monotone narrowing, not an absolute pixel count. At the
    # test widget's radius the slit is only a few pixels wide, so a fixed
    # "at least N px" floor is really asserting the widget SIZE — the old
    # `>= 3` passed only because the un-stopped timer let the smoothed level
    # decay before the "loud" grab, making the contrast look bigger.
    widths = [pupil_width(lvl) for lvl in (0.0, 0.5, 1.0)]
    assert widths == sorted(widths, reverse=True), (
        f"the pupil must only ever narrow as the level rises: {widths}")
    quiet, loud = widths[0], widths[-1]
    assert loud < quiet, f"the pupil must narrow under the voice: {quiet} -> {loud}"

    def lit(level):
        img = grab(level)
        n = 0
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if max(c.red(), c.green(), c.blue()) >= 14:
                    n += 1
        return n

    silent, speaking = lit(0.0), lit(1.0)
    assert speaking > silent, (
        f"the fire must flare with the voice: {silent} -> {speaking}")


@scenario
def every_design_reacts_to_voice_level():
    # The Eye reacting was not enough: "reacts to the voice" has to be true of
    # the bubble whichever design is selected. Six painters read `level` and
    # the equalizer faked it with a local sin(t) pulse; the other five ignored
    # the voice completely.
    #
    # The first fix for that folded `level` into f["energy"] once, which made
    # every design react — but made them all react the SAME WAY, one uniform
    # brightness gain for all twelve, so the shapes lost their identities
    # exactly when the bubble is most alive. Each painter now owns its own
    # reaction (the orb ripples, the cube flashes its facets in a sweep, the
    # crystal refracts, the halo sends a wave round its torus, …), and this
    # scenario is what keeps that honest: the shared channel must stay
    # level-independent, AND every design must still move >= 100 px under the
    # voice. Together those two force any new painter to write its own term —
    # drop the bespoke term and the per-design assertion below fails.
    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _FrozenClock:
        # frozen t: the whole point is to isolate the voice, so animation phase
        # must not drift between the two grabs
        def __init__(self, ms=4000):
            self._ms = ms

        def elapsed(self):
            return self._ms

    widget = appearance.BubbleWidget(_Stub())
    widget.resize(appearance.WINDOW_PX, appearance.WINDOW_PX)
    widget._clock = _FrozenClock()
    # Stop the 16 ms animation timer: grab() drains the event queue, so an
    # active timer advances _on_tick's smoothed level/colour/radius BETWEEN the
    # two grabs and the difference stops measuring the voice.
    widget._anim.stop()
    widget._last_tick = 4.0
    # Freeze the radius too: in the listening state _radius_target grows with
    # the level, which is a separate (pre-existing) whole-bubble effect. Left
    # live it changes >100 px on EVERY design and "proves" voice reactivity for
    # designs whose painter ignores the voice entirely — a confound that made
    # this assertion pass on the unfixed code.
    widget._radius_ui = appearance.BUBBLE_R0
    # "listening" is when the mic is live, and it is the state the equalizer
    # meters in; idle bars deliberately breathe on their own instead
    widget._state = "listening"
    widget._energy_ui = 0.5
    appearance.ANIM_ENERGY = 1.0
    appearance.BUBBLE_ACCENT = 0.5
    designs = list(getattr(settings_app.SCHEMA, "BUBBLE_DESIGNS", ("orb",)))

    from PySide6.QtGui import QImage

    def _frame_at(design, level, clock=None):
        if clock is not None:
            widget._clock = clock
        widget._level_target = level       # nothing left to chase
        widget._level_ui = level
        bubble.SETTINGS["bubble_design"] = design
        # Paint onto a surface we cleared ourselves. grab() reads the platform
        # backing store, which is not a valid instrument for a
        # WA_TranslucentBackground widget -- the background is never erased. It
        # reported exactly zero changed pixels on halo/reactor/bloom in CI while
        # the same Qt version here reported thousands, and the three designs
        # share nothing but being measured first, which is what gives the
        # instrument away rather than the painters. Clearing first also makes
        # the equalizer clock comparison below meaningful: on a reused surface a
        # stale frame can make two different clocks look identical.
        img = QImage(widget.size(), QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        return bytes(img.constBits())

    def _changed(design, quiet, loud):
        before = _frame_at(design, quiet)
        after = _frame_at(design, loud)
        n = min(len(before), len(after))
        return sum(1 for i in range(0, n - 3, 4)
                   if any(x != y for x, y in zip(before[i:i + 3], after[i:i + 3])))

    # The shared frame channel must NOT carry the voice any more. That is the
    # crisp half of the regression: `_frame()` used to compute
    # `energy * k_voice` with `k_voice = 1.0 + 0.45 * level`, so re-adding any
    # such lift here lights up every design at once again and this fails
    # immediately, whether or not the painters still have their own terms.
    widget._level_ui = 0.0
    quiet = widget._frame()
    widget._level_ui = 1.0
    loud = widget._frame()
    assert quiet["energy"] == loud["energy"], (
        "the voice has leaked back into the shared energy channel: every design "
        "must own its own reaction instead of one uniform brightness lift")
    assert loud["level"] == 1.0 and quiet["level"] == 0.0, (
        "the level itself must still be published for the painters to read")

    measured = {d: _changed(d, 0.0, 1.0) for d in designs}
    weak = [f"{d}={n}" for d, n in measured.items() if n < 100]
    if weak:
        # Which instrument failed, not just which design: a surface that paints
        # nothing at all is a different problem from a painter that ignores the
        # voice, and the two have already been confused once in this file.
        _frame_at("orb", 1.0)
        img = QImage(widget.size(), QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        blank = sum(1 for b in bytes(img.constBits()) if b)
        raise AssertionError(
            "the voice changes no visible pixels on: " + ", ".join(weak)
            + f" (all: {measured})"
            # one line, no escapes: this text is inside GUI_DRIVER
            + f" | widget {widget.size().width()}x{widget.size().height()}"
            + f" | non-zero bytes in a cleared orb render: {blank}")

    # ...and for the meter specifically, the reaction must come from the shared
    # level rather than a local timer. The equalizer used to derive bar length
    # from `sin(t * 6.0 + i * 1.3)`, which is why it only LOOKED voice-reactive
    # while speaking. Its active branch is now t-free, so two different frozen
    # clocks with the level pinned must render identically — a returning sin(t)
    # pulse fails this immediately, whereas the frozen-clock pixel diff above
    # would still pass it.
    assert "equalizer" in designs, "design list changed; update this assertion"
    a = _frame_at("equalizer", 0.6, _FrozenClock(4000))
    b = _frame_at("equalizer", 0.6, _FrozenClock(9000))
    assert a == b, "equalizer bars still render from a timer, not the shared level"


@scenario
def every_design_shows_the_state_colour():
    # "on Appearance only the shapes apply" was reported twice, and the second
    # time named pikachu and the Eye of Sauron. The voice test above proves the
    # LEVEL reaches every painter; this proves the STATE COLOUR does — a
    # separate channel, read from `f["color"]`.
    #
    # Counting changed PIXELS is not enough here: a 15% veil changes bytes on
    # thousands of pixels while staying a colour change nobody can see, which is
    # exactly why the old floors looked fine to a pixel count. So a pixel only
    # counts when it differs by a visible step in some channel.
    import sys as _sys

    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _FrozenClock:
        def elapsed(self):
            return 4000

    widget = appearance.BubbleWidget(_Stub())
    widget.resize(appearance.WINDOW_PX, appearance.WINDOW_PX)
    widget._clock = _FrozenClock()
    # Freeze EVERYTHING except the colour. `_on_tick` crossfades _color_ui AND
    # the state's motion terms together, so a live timer would move the swirl,
    # the orbit and the wobble between the two grabs and "prove" the colour
    # reaches a painter that ignores it entirely -- the confound the voice test
    # documents for the radius.
    widget._anim.stop()
    widget._last_tick = 4.0
    widget._level_ui = widget._level_target = 0.0
    widget._radius_ui = appearance.BUBBLE_R0
    widget._state = "idle"          # the state is NOT what this measures
    widget._energy_ui = appearance._fx_energy("idle")
    appearance.ANIM_ENERGY = 1.0
    appearance.BUBBLE_ACCENT = 0.5
    from PySide6.QtGui import QImage, QColor

    designs = list(getattr(settings_app.SCHEMA, "BUBBLE_DESIGNS", ("orb",)))

    def _pixels(design, color):
        bubble.SETTINGS["bubble_design"] = design
        c = QColor(color)
        widget._color_ui = [c.redF(), c.greenF(), c.blueF()]
        img = QImage(widget.size(), QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        return bytes(img.constBits())

    def _visible(before, after):
        n = min(len(before), len(after))
        out = 0
        for i in range(0, n - 3, 4):
            if (before[i + 3] == 0) and (after[i + 3] == 0):
                continue
            if max(abs(x - y) for x, y in zip(before[i:i + 3], after[i:i + 3])) >= 48:
                out += 1
        return out

    IDLE = "#2f6fed"
    worst = {}
    for d in designs:
        base = _pixels(d, IDLE)
        counts = [_visible(base, _pixels(d, other))
                  for other in ("#e0435c", "#c8781f", "#1fae62")]
        worst[d] = max(counts)
    # 400 px is deliberately between the broken and working measurements: the
    # design with no body colour (void, black disc + 2px streaks) measured 8
    # when only the rim/streak alphas carried the tint, and the least-visible
    # working design measures ~800. A higher bar would flag real designs as
    # broken; a lower one would let a near-invisible tint through.
    weak = [f"{d}={n}" for d, n in worst.items() if n < 400]
    if weak:
        raise AssertionError(
            "the state colour is invisible on: " + ", ".join(weak)
            + f" | qt {_sys.version_info[:2]}"
            + f" | widget {widget.size().width()}x{widget.size().height()}"
            + f" | all designs: {sorted(worst.items(), key=lambda kv: kv[1])}")


@scenario
def resizing_the_bubble_keeps_its_aperture():
    # "if you make it bigger the design breaks; if you keep it bigger and swap
    # shapes the design mismatches" -- one cause. The mask is set in WIDGET
    # coordinates and Qt keeps it exactly as it was; the bubble set it only in
    # showEvent, so every live size change drew the new, scaled design through
    # the OLD aperture. Measured on the unfixed code: mask 128x128 while the
    # widget was 192x192.
    from PySide6.QtCore import QPoint

    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    widget = appearance.BubbleWidget(_Stub())
    widget.resize(128, 128)
    widget.show()                       # a mask is only real once the widget is
    app.processEvents()
    start = widget.mask().boundingRect()
    assert (start.width(), start.height()) == (128, 128), start
    # exactly what the live settings reload does
    widget.setFixedSize(appearance.WINDOW_PX * 3 // 2, appearance.WINDOW_PX * 3 // 2)
    app.processEvents()
    grown = widget.mask().boundingRect()
    rect = widget.rect()
    assert (grown.width(), grown.height()) == (rect.width(), rect.height()), (
        "the mask did not follow the widget: mask "
        f"{grown.width()}x{grown.height()} vs widget "
        f"{rect.width()}x{rect.height()}")
    # and the shrink path, which is the same setFixedSize call with a smaller
    # number -- the stale-aperture bug is symmetric
    widget.setFixedSize(appearance.WINDOW_PX, appearance.WINDOW_PX)
    app.processEvents()
    back = widget.mask().boundingRect()
    assert (back.width(), back.height()) == (appearance.WINDOW_PX, appearance.WINDOW_PX), back
    # The mask is an ellipse, not the whole rect: a fix that simply widened the
    # aperture to the rectangle would pass the size assertions above.
    assert not widget.mask().contains(QPoint(1, 1)), \
        "mask is no longer elliptical"


@scenario
def a_look_sets_every_appearance_control():
    # The Look picker is one click for the whole look. What must be true for
    # EVERY catalogue entry, or the click is a lie: all five controls move, all
    # five reach settings.json in one apply, the bubble is told once, the
    # section names the look it is showing, and one nudge afterwards stops it
    # claiming that look. Driven from APPEARANCE_LOOKS, so a look added later
    # is covered without touching this scenario.
    from settings_schema import APPEARANCE_LOOKS
    assert len(APPEARANCE_LOOKS) >= 5, APPEARANCE_LOOKS   # never vacuous
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    notes = []
    for entry in APPEARANCE_LOOKS:
        notified = []
        win._notify_bubble_reloaded = lambda: (notified.append(1), True)[1]
        # Land the form on something this look CANNOT already be, so "the click
        # reached disk" is a real assertion. Without it the first look happens
        # to match the seeded defaults, the live apply correctly finds nothing
        # changed and writes nothing — and the scenario would be measuring the
        # seed rather than the click.
        win.size_slider.setValue(96 if entry["bubble_size"] != 96 else 192)
        win._apply_appearance_live()
        win._apply_look(entry["name"])
        win._apply_appearance_live()      # what the debounce timer calls
        on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
        assert on_disk["bubble_design"] == entry["design"], (
            entry["name"], on_disk.get("bubble_design"))
        assert on_disk["bubble_size"] == entry["bubble_size"], entry["name"]
        assert abs(float(on_disk["animation_energy"])
                   - float(entry["animation_energy"])) < 1e-9, entry["name"]
        assert abs(float(on_disk["bubble_accent"])
                   - float(entry["bubble_accent"])) < 1e-9, entry["name"]
        assert on_disk["colors"] == entry["colors"], (entry["name"],
                                                       on_disk["colors"])
        assert notified, f"{entry['name']} must tell the bubble to repaint"
        assert win.design_combo.currentData() == entry["design"]
        assert win.size_slider.value() == entry["bubble_size"]
        assert entry["label"] in win.look_label.text(), \
            (entry["name"], win.look_label.text())
        assert win.look_buttons[entry["name"]].isChecked(), entry["name"]
        assert win.status_label.text().startswith(
            f"Applied look {entry['label']}"), win.status_label.text()
        notes.append(entry["name"])
    # the last look is on screen; nudge the size slider and the section must
    # stop claiming it rather than leaving a stale tick behind
    last = APPEARANCE_LOOKS[-1]
    win.size_slider.setValue(int(last["bubble_size"]) + 1)
    assert "Custom" in win.look_label.text(), win.look_label.text()
    assert not any(b.isChecked() for b in win.look_buttons.values())
    assert len(notes) == len(APPEARANCE_LOOKS)


@scenario
def every_look_is_renderable_and_reacts():
    # A look is a promise about what the bubble shows. Render each one's design
    # in its own palette offscreen and measure the two things that have gone
    # wrong before on this tab: a palette change nobody can see (the void
    # regression, 8 visible pixels) and a design that ignores the voice.
    from PySide6.QtGui import QColor, QImage
    from settings_schema import APPEARANCE_LOOKS

    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _FrozenClock:
        def elapsed(self):
            return 4000

    widget = appearance.BubbleWidget(_Stub())
    widget.resize(appearance.WINDOW_PX, appearance.WINDOW_PX)
    widget._clock = _FrozenClock()
    widget._anim.stop()
    widget._last_tick = 4.0
    widget._radius_ui = appearance.BUBBLE_R0
    widget._state = "idle"
    appearance.ANIM_ENERGY = 1.0
    appearance.BUBBLE_ACCENT = 0.5

    def _pixels(level, color):
        widget._level_target = widget._level_ui = level
        c = QColor(color)
        widget._color_ui = [c.redF(), c.greenF(), c.blueF()]
        img = QImage(widget.size(), QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        return bytes(img.constBits())

    def _visible(before, after):
        n = min(len(before), len(after))
        out = 0
        for i in range(0, n - 3, 4):
            if before[i + 3] == 0 and after[i + 3] == 0:
                continue
            if max(abs(x - y) for x, y in zip(before[i:i + 3],
                                              after[i:i + 3])) >= 48:
                out += 1
        return out

    def _changed(before, after):
        n = min(len(before), len(after))
        return sum(1 for i in range(0, n - 3, 4)
                   if any(x != y for x, y in zip(before[i:i + 3],
                                                 after[i:i + 3])))

    weak = []
    for entry in APPEARANCE_LOOKS:
        bubble.SETTINGS["bubble_design"] = entry["design"]
        colors = entry["colors"]
        base = _pixels(0.0, colors["idle"])
        seen = max(_visible(base, _pixels(0.0, other))
                   for other in (colors["listening"], colors["thinking"],
                                 colors["speaking"]))
        voice = _changed(_pixels(0.0, colors["idle"]),
                         _pixels(1.0, colors["idle"]))
        if seen < 400 or voice < 100:
            weak.append(f"{entry['name']} (colour {seen}, voice {voice})")
    if weak:
        raise AssertionError(
            "these looks do not reach the bubble: " + ", ".join(weak)
            + f" | designs: {[e['design'] for e in APPEARANCE_LOOKS]}")


@scenario
def the_cat_keeps_its_ears_inside_its_own_mask():
    # The cat is the first design whose outline leaves the inscribed ellipse.
    # A mask is what decides which painted pixels survive on the desktop, so
    # two things must hold: the mask covers EVERY pixel the painter lays down
    # (otherwise the ears come back shorn, which is the whole reason a design
    # could never be anything but a circle), and it still keeps the ellipse, so
    # nothing that used to fit can be clipped now.
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QImage, QPainter, QRegion

    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _Clock:
        def __init__(self, ms):
            self.ms = ms

        def elapsed(self):
            return self.ms

    W = appearance.WINDOW_PX
    widget = appearance.BubbleWidget(_Stub())
    widget.resize(W, W)
    widget._anim.stop()
    ellipse = QRegion(QRect(0, 0, W, W), QRegion.Ellipse)
    cat_region = appearance.design_region("cat", W, W)
    assert appearance.design_region("orb", W, W) == ellipse, \
        "the ordinary designs must keep the plain inset ellipse"
    assert ellipse.subtracted(cat_region).isEmpty(), \
        "the cat's mask must still keep the whole ellipse"
    assert not cat_region.subtracted(ellipse).isEmpty(), \
        "the cat's mask must add its own outline, not just re-use the ellipse"
    # ...and the window USES it. A region that nothing applies is the same as
    # no region at all, which is how the stale-aperture bug lived: the shape
    # was correct on paper and never reached the widget.
    bubble.SETTINGS["bubble_design"] = "cat"
    widget.show()
    app.processEvents()
    assert widget.mask() != ellipse, \
        "the cat's mask was computed but never applied to the window"
    # ...and back again: a live shape switch keeps the widget's SIZE, so the
    # mask cannot ride on resizeEvent alone
    bubble.SETTINGS["bubble_design"] = "orb"
    widget.setFixedSize(W, W)
    widget.update()
    app.processEvents()
    assert widget.mask() == ellipse, \
        "the ordinary designs must fall back to the plain inset ellipse"
    bubble.SETTINGS["bubble_design"] = "cat"

    worst = (0, None)
    for clock_ms in (0, 700, 2600, 4000):
        widget._clock = _Clock(clock_ms)
        widget._last_tick = clock_ms / 1000.0
        for state in ("idle", "listening", "thinking", "speaking"):
            widget._state = state
            widget._energy_ui = appearance._fx_energy(state)
            for level in (0.0, 0.5, 1.0):
                for grow in (0.0, 12.0):      # 12 = the listening peak
                    widget._level_target = widget._level_ui = level
                    widget._radius_ui = appearance.BUBBLE_R0 + grow * appearance.GEOM_K
                    img = QImage(W, W, QImage.Format_ARGB32)
                    img.fill(0)
                    painter = QPainter(img)
                    widget._paint_cat(painter, widget._frame())
                    painter.end()
                    outside = sum(
                        1 for y in range(W) for x in range(W)
                        if img.pixelColor(x, y).alpha()
                        and not cat_region.contains(QPoint(x, y)))
                    if outside > worst[0]:
                        worst = (outside, (clock_ms, state, level, grow))
    if worst[0]:
        raise AssertionError(
            f"the cat paints {worst[0]} pixel(s) outside its own mask at "
            f"{worst[1]} — Qt would cut them off on the desktop")


@scenario
def every_design_keeps_ink_inside_its_aperture():
    # A design may draw anything it likes as long as it is inside the glass.
    # Ink past the mask is ink Qt cuts off, so the shape gets a flat edge
    # exactly where it was meant to be round or pointed: the droplet's point
    # and its drip, Saturn's moon, the Eye's flame tips, the tail of a halo
    # that is supposed to fade to nothing. Six designs did it before the
    # APERTURE_R budget existed — every one of those clamps is pinned here, so
    # removing any of them fails this scenario.
    #
    # The bar is alpha 16 of 255 — one bar for every design, deliberately
    # between the two things it must separate: <=4 is what antialiasing a
    # design's own edge against the rasterised rim leaves behind (sub-pixel,
    # invisible, and unavoidable), and the six spills this pins measured 19 to
    # 255.
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QPainter
    from settings_schema import BUBBLE_DESIGNS

    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _Clock:
        def __init__(self, ms):
            self.ms = ms

        def elapsed(self):
            return self.ms

    VISIBLE = 16
    W = appearance.WINDOW_PX
    assert appearance.APERTURE_R <= W / 2.0 - 1.0, (
        "the aperture budget must stay at least a pixel inside the glass, or "
        "the guard below is measuring against a budget the mask contradicts")
    widget = appearance.BubbleWidget(_Stub())
    widget.resize(W, W)
    widget._anim.stop()

    worst = (0, None)
    for design in BUBBLE_DESIGNS:
        region = appearance.design_region(design, W, W)
        outside = [(x, y) for y in range(W) for x in range(W)
                   if not region.contains(QPoint(x, y))]
        for clock_ms in (0, 600, 2600, 4000):
            widget._clock = _Clock(clock_ms)
            for state in ("idle", "listening", "thinking", "speaking"):
                widget._state = state
                widget._energy_ui = appearance._fx_energy(state)
                for level in (0.0, 1.0):
                    # every radius the state machine can ask for: rest, the
                    # idle breathe, the speaking pulse and the listening peak
                    for grow in (0.0, 3.5, 7.0, 12.0):
                        widget._level_target = widget._level_ui = level
                        widget._radius_ui = appearance.BUBBLE_R0 + grow * appearance.GEOM_K
                        img = QImage(W, W, QImage.Format_ARGB32)
                        img.fill(0)
                        painter = QPainter(img)
                        getattr(widget, "_paint_" + design)(painter, widget._frame())
                        painter.end()
                        n = 0
                        peak = 0
                        for x, y in outside:
                            a = img.pixelColor(x, y).alpha()
                            if a >= VISIBLE:
                                n += 1
                                peak = max(peak, a)
                        if n > worst[0]:
                            worst = (n, (design, clock_ms, state, level, grow,
                                         peak))
    if worst[0]:
        design, clock_ms, state, level, grow, peak = worst[1]
        raise AssertionError(
            f"`{design}` paints {worst[0]} pixel(s) of visible ink outside "
            f"its own aperture (worst alpha {peak}) at clock={clock_ms} "
            f"{state} lv={level} radius=+{grow} — Qt would cut them off")

    # ...and the droplet must still BE a teardrop of its own size. A fit that
    # only clamped its height would pass the check above by turning it into a
    # ball, and folding the detaching drip back into the budget would pass it by
    # shrinking the whole drop — so pin the silhouette, and a reach floor at the
    # phase where a drip-inclusive fit is at its most destructive (clock 2600:
    # the drip is 11 px past the rim there, already clipped by the window).
    for clock_ms, state, level, grow in ((0, "idle", 0.0, 0.0),
                                         (0, "idle", 0.0, 3.5),
                                         (600, "listening", 1.0, 12.0),
                                         (2600, "listening", 1.0, 12.0),
                                         (4000, "listening", 1.0, 12.0)):
        widget._clock = _Clock(clock_ms)
        widget._state = state
        widget._energy_ui = appearance._fx_energy(state)
        widget._level_target = widget._level_ui = level
        widget._radius_ui = appearance.BUBBLE_R0 + grow * appearance.GEOM_K
        img = QImage(W, W, QImage.Format_ARGB32)
        img.fill(0)
        painter = QPainter(img)
        widget._paint_droplet(painter, widget._frame())
        painter.end()
        pts = [(x, y) for y in range(W) for x in range(W)
               if img.pixelColor(x, y).alpha() >= VISIBLE]
        assert pts, f"the droplet draws nothing at {state} lv={level}"
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        wide = max(xs) - min(xs) + 1
        tall = max(ys) - min(ys) + 1
        assert tall >= wide * 1.2, (
            f"the droplet is not a teardrop any more at {state} lv={level}: "
            f"{wide}x{tall} — the aperture fit squashed its height")
        reach = max(((x + 0.5 - W / 2.0) ** 2
                     + (y + 0.5 - W / 2.0) ** 2) ** 0.5 for x, y in pts)
        assert reach >= 55.0, (
            f"the droplet collapsed to {reach:.1f} px at {state} lv={level} "
            f"(clock {clock_ms}) — the fit is shrinking the design away "
            f"instead of fitting it")


@scenario
def every_design_has_its_own_preview_glyph():
    # BubblePreview._glyph falls through to the orb for any design it does not
    # know, so a new design would show as an orb in the Appearance preview
    # while the bubble drew something else — the preview would be lying about
    # the very thing the combo above it selects.
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QPainter, QColor
    from settings_schema import BUBBLE_DESIGNS

    preview = settings_app.BubblePreview(
        lambda: {k: QColor("#4f8cff") for k in ("idle",)},
        lambda: 128, lambda: "orb", lambda: 1.0, lambda: 0.5)
    preview.resize(240, 200)
    seen = {}
    for design in BUBBLE_DESIGNS:
        img = QImage(120, 120, QImage.Format_ARGB32)
        img.fill(0)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing, True)
        preview._glyph(painter, design, 60.0, 60.0, 40.0,
                       QColor("#4f8cff"), 4.0, 1.0)
        painter.end()
        seen[design] = bytes(img.constBits())
    duplicates = [d for d in BUBBLE_DESIGNS
                  if d != "orb" and seen.get(d) == seen.get("orb")]
    if duplicates:
        raise AssertionError(
            "these designs render the orb glyph in the preview (unknown "
            "designs fall through): " + ", ".join(duplicates))
    assert len({v for v in seen.values()}) == len(BUBBLE_DESIGNS), (
        "two designs share a preview glyph: "
        + ", ".join(sorted(seen, key=lambda k: seen[k])))


@scenario
@scenario
def the_appearance_panel_persists_the_avatar_decoration():
    # The ring light is a CLOSED choice with exactly two values, and it has to
    # survive the round trip: shown from settings.json into the row, and
    # collected back into the payload Save writes. A combo that renders but
    # never reaches `cfg` is precisely the "I changed it and nothing applies"
    # defect this card exists to answer.
    from settings_schema import AVATAR_DECOS, AVATAR_DECO_COLORS
    values = [win.deco_combo.itemData(i) for i in range(win.deco_combo.count())]
    assert values == list(AVATAR_DECOS), values

    for value in values:
        win.deco_combo.setCurrentIndex(values.index(value))
        win._collect()
        assert win.cfg["avatar_ring"] == value, win.cfg.get("avatar_ring")
        # the picker describes what it is offering, and the sentence FOLLOWS the
        # choice — a stale hint under a new selection is a lie in the panel
        assert win.deco_hint.text().strip(), value
    win.deco_combo.setCurrentIndex(values.index("off"))
    assert win.deco_hint.text() != "", "even Off is described"

    # ...and the load path: the value on disk is the value the row shows
    win.cfg["avatar_ring"] = "aurora"
    win._load_values()
    assert win.deco_combo.currentData() == "aurora", win.deco_combo.currentData()
    assert "ribbon" in win.deco_hint.text().lower(), win.deco_hint.text()

    # The DECORATION'S OWN COLOUR is one key with three shapes, and the third is
    # a literal hex — so the row has to round-trip all three and `custom` itself
    # must never be what gets stored, or the bubble would read a word it does
    # not know and fall back to the state colour quietly.
    modes = [win.deco_colour_combo.itemData(i)
             for i in range(win.deco_colour_combo.count())]
    assert modes == list(AVATAR_DECO_COLORS) + ["custom"], modes
    for mode in AVATAR_DECO_COLORS:
        win.deco_colour_combo.setCurrentIndex(modes.index(mode))
        win._collect()
        assert win.cfg["avatar_deco_color"] == mode, win.cfg.get("avatar_deco_color")
        assert not win.deco_colour_btn.isEnabled(), (
            f"the swatch is live while the row says {mode}, so it does nothing")
    # `custom` is driven through the real CLICK path with the colour dialog
    # stubbed, because the wiring worth pinning is the button's: a swatch that
    # renders but never reaches `cfg` is the same defect one layer down.
    class _FakeDialog:
        @staticmethod
        def getColor(*_a, **_k):
            return settings_app.QColor("#123456")
    settings_app.QColorDialog = _FakeDialog
    win.deco_colour_combo.setCurrentIndex(modes.index("custom"))
    assert win.deco_colour_btn.isEnabled(), "the swatch must be live when it is used"
    win.deco_colour_btn.click()
    assert win._deco_colour == "#123456", win._deco_colour
    assert "#123456" in win.deco_colour_btn.text().lower(), win.deco_colour_btn.text()
    win._collect()
    assert win.cfg["avatar_deco_color"] == "#123456", (
        win.cfg.get("avatar_deco_color"))

    # THE LIVE APPLY, which is the part that was broken and the part a `cfg`
    # assertion cannot see: `_apply_appearance_live` only writes when a key it is
    # WATCHING differs from disk, so a control whose key is missing from
    # APPEARANCE_KEYS applies nothing and saves nothing while still reporting
    # into `cfg`. This is the same defect the state colours had, and the reason
    # this scenario now ends here rather than at the collection above.
    seed({"model": "testmodel:latest"})        # a file on disk to be an EDIT against
    win.reload_from_disk()
    before = json.loads(settings_file.read_text(encoding="utf-8"))
    win.deco_colour_combo.setCurrentIndex(modes.index("rainbow"))
    win._apply_appearance_live()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["avatar_deco_color"] == "rainbow", (
        f"the decoration colour never reached settings.json: "
        f"{on_disk.get('avatar_deco_color')!r} (was {before.get('avatar_deco_color')!r})")
    assert "decoration colour" in win.status_label.text(), win.status_label.text()

    win.deco_colour_combo.setCurrentIndex(modes.index("custom"))
    win.deco_colour_btn.click()                 # the swatch, same path
    win._apply_appearance_live()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["avatar_deco_color"] == "#123456", on_disk["avatar_deco_color"]

    # ...and the load path: whatever is on disk is what the row SHOWS, so saving
    # without touching the control is not an edit.
    win.cfg["avatar_deco_color"] = "#0A0B0C"
    win._load_values()
    assert win.deco_colour_combo.currentData() == "custom", (
        win.deco_colour_combo.currentData())
    assert win._deco_colour == "#0A0B0C", win._deco_colour
    win.cfg["avatar_deco_color"] = "rainbow"
    win._load_values()
    assert win.deco_colour_combo.currentData() == "rainbow"

    # The colour rule round-trips the same way, and it is a real choice: the
    # wash is what painted a character the state hue.
    tints = [win.tint_combo.itemData(i) for i in range(win.tint_combo.count())]
    assert tints == ["state", "natural"], tints
    for value in tints:
        win.tint_combo.setCurrentIndex(tints.index(value))
        win._collect()
        assert win.cfg["avatar_tint"] == value, win.cfg.get("avatar_tint")
    win.cfg["avatar_tint"] = "natural"
    win._load_values()
    assert win.tint_combo.currentData() == "natural"


@scenario
def the_appearance_panel_offers_every_decoration_in_the_schema():
    # A picker is only as good as the set behind it: the schema owns the closed
    # set, core.settings coerces to it, the painters implement it — and the
    # panel must OFFER exactly that, because a decoration nobody can select is
    # a feature that does not exist, and one the picker offers that no painter
    # implements draws nothing (silently).
    from settings_schema import AVATAR_DECOS, AVATAR_TINTS
    offered = [win.deco_combo.itemData(i) for i in range(win.deco_combo.count())]
    assert offered == list(AVATAR_DECOS), (offered, list(AVATAR_DECOS))
    tints = [win.tint_combo.itemData(i) for i in range(win.tint_combo.count())]
    assert tints == list(AVATAR_TINTS), (tints, list(AVATAR_TINTS))

    # Every one of them has a sentence, and the sentence is not the same one
    # repeated: the hints are how a person chooses between them.
    seen = {}
    for i, name in enumerate(offered):
        win.deco_combo.setCurrentIndex(i)
        seen[name] = win.deco_hint.text().strip()
    assert all(seen.values()), seen
    assert len(set(seen.values())) == len(seen), seen


@scenario
def the_strip_shows_the_decoration_and_its_colour():
    # The Decoration card exists in this window for one reason: to choose what
    # the avatar wears. Until this scenario the strip above it drew NO ring at
    # all — so every choice the card offered, including the colour row under it,
    # had no visible effect where the user was looking. Reported as "decoration
    # color and own color rainbow color dont work".
    #
    # What is pinned is the property that was missing: changing the ring, or the
    # colour it is drawn in, CHANGES PIXELS IN THE STRIP, and the colour really
    # is the colour (a red ring puts red ink where the state-coloured one has
    # none) rather than merely "something moved".
    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    win.tabs.setCurrentIndex(names.index("Appearance"))
    win.show()
    settings_app.QApplication.processEvents()
    win.design_combo.setCurrentIndex(win.design_combo.findData("image"))
    win.design_combo.setCurrentIndex(win.design_combo.findData("image"))

    # The strip animates on a REAL clock, so two identical settings drawn a
    # moment apart differ on their own — freezing time is what makes "these two
    # shots are the same" a statement about the settings rather than about how
    # long the process took. (The first run of this scenario failed on exactly
    # that, which is why the pin is here and not in the assertion.)
    class _Frozen:
        def elapsed(self):
            return 1200
    win.preview._clock = _Frozen()

    def shot(deco, mode, custom="#4f8cff"):
        win.deco_combo.setCurrentIndex(
            [win.deco_combo.itemData(i) for i in range(win.deco_combo.count())]
            .index(deco))
        modes = [win.deco_colour_combo.itemData(i)
                 for i in range(win.deco_colour_combo.count())]
        win.deco_colour_combo.setCurrentIndex(modes.index(mode))
        win._deco_colour = custom
        settings_app.QApplication.processEvents()
        from PySide6.QtGui import QImage
        img = QImage(win.preview.size(), QImage.Format_ARGB32)
        img.fill(0)
        win.preview.render(img)
        return img

    def changed(a, b, step=12):
        n = 0
        for y in range(a.height()):
            for x in range(a.width()):
                c1, c2 = a.pixelColor(x, y), b.pixelColor(x, y)
                if (abs(c1.red() - c2.red()) > step
                        or abs(c1.green() - c2.green()) > step
                        or abs(c1.blue() - c2.blue()) > step):
                    n += 1
        return n

    def red_ink(img):
        # Pixels that are red and NOT explainable as the state orb or the art.
        return sum(1 for y in range(img.height()) for x in range(img.width())
                   if (lambda c: c.alpha() > 80 and c.red() > 120
                       and c.red() > c.blue() + 40)(img.pixelColor(x, y)))

    assert win.preview.size().width() > 0, "the strip was never laid out"
    off = shot("off", "state")
    assert changed(off, shot("off", "state")) == 0, (
        "two identical settings drew differently — the measurement is noisy")

    ring_state = shot("ring-light", "state")
    assert changed(off, ring_state) > 200, (
        f"choosing a decoration changed {changed(off, ring_state)} pixels of the "
        f"strip — the card has no visible effect where it lives")
    assert changed(ring_state, shot("orbit", "state")) > 200, (
        "switching decoration changed nothing in the strip")
    assert changed(ring_state, shot("ring-light", "rainbow")) > 0, (
        "the Rainbow row changed nothing in the strip")

    # ...and the colour is the colour: a red ring puts red ink where the
    # state-coloured ring puts none, and a green one does not.
    red = shot("ring-light", "custom", "#ff0000")
    green = shot("ring-light", "custom", "#00ff00")
    assert red_ink(red) > red_ink(ring_state) + 50, (
        f"the own-colour row did not reach the strip: red ink "
        f"{red_ink(red)} vs state ink {red_ink(ring_state)}")
    assert changed(red, green) > 200, (
        "two different own colours drew the same strip")


@scenario
def appearance_panel_is_a_scrolling_column_of_cards():
    # The panel used to be a flat stack of unlabelled rows pinned to a page that
    # did not scroll, so a section that grew pushed the live preview past the
    # bottom edge with no scrollbar and nothing on screen to say it had been
    # there. Two properties are load-bearing, and they are what this pins: the
    # page scrolls, and no section dictates the width of the panel around it.
    from PySide6.QtWidgets import QFrame, QScrollArea

    names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    # the page has to be CURRENT and the window shown: an unraised tab is never
    # laid out, so a geometry check against it would measure nothing at all
    win.tabs.setCurrentIndex(names.index("Appearance"))
    win.show()
    settings_app.QApplication.processEvents()
    page = win.tabs.widget(names.index("Appearance"))
    scroll = page.findChild(QScrollArea)
    assert scroll is not None, "the Appearance page must scroll"
    assert scroll.widgetResizable(), "the panel must resize with the window"
    content = scroll.widget()

    titles, cards = [], []
    lay = content.layout()
    for i in range(lay.count()):
        card = lay.itemAt(i).widget()
        if card is None or card.objectName() != "card":
            continue
        head = card.findChildren(settings_app.QLabel)
        titles.append(head[0].text() if head else "")
        cards.append(card)
    assert cards, "the panel is a column of cards"
    assert len(titles) == len(set(titles)), titles
    for expected in ("Preview", "Look", "Shape", "Motion", "State colours",
                     "Match your desktop"):
        assert expected in titles, (expected, titles)
    assert all(isinstance(c, QFrame) for c in cards)

    # narrow window: the panel must stay reachable rather than running off the
    # side, and a value too tall for the viewport must become scrollable
    win.setFixedSize(520, 430)
    settings_app.QApplication.processEvents()
    view = scroll.viewport()
    assert content.minimumSizeHint().width() <= view.width(), (
        f"the panel demands {content.minimumSizeHint().width()}px inside a "
        f"{view.width()}px viewport — a card you cannot reach")
    assert content.height() > view.height(), (
        "this window should need scrolling; nothing may be clipped instead")
    assert scroll.verticalScrollBar().isVisible()
    for card in cards:
        assert card.width() <= view.width(), (titles[cards.index(card)],
                                              card.width(), view.width())

    # the look tiles WRAP. In one long row they set the panel's minimum width
    # (8 tiles + spacing); wrapped four to a row they cannot.
    n = len(win.look_buttons)
    assert n >= 5, n
    rows = {tile.mapTo(content, tile.rect().topLeft()).y()
            for tile in win.look_buttons.values()}
    assert len(rows) == (n + 3) // 4, (n, sorted(rows))
    assert content.minimumSizeHint().width() < 8 * settings_app.LookTile.W, (
        "the tiles still dictate the panel width: "
        f"{content.minimumSizeHint().width()}px")


@scenario
def look_tiles_are_drawn_from_the_shared_painter():
    # A Look used to be a text button: the picker promised a name and nothing
    # more. Now each look is DRAWN, and a drawn face is a claim about the
    # bubble, so three things are pinned. The whole palette must land on the
    # tile — a face showing only its idle colour is the "nothing applies"
    # complaint in miniature. The glyph must come from the ONE shared painter
    # rather than a private copy, or a tile could advertise a shape the
    # Appearance strip and the bubble would not draw. And the strip must share
    # one timer instead of one per tile.
    import json as _json
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QBrush, QColor, QImage
    from settings_schema import APPEARANCE_LOOKS

    tiles = win.look_buttons
    names = [str(e["name"]) for e in APPEARANCE_LOOKS]
    assert len(APPEARANCE_LOOKS) >= 5, APPEARANCE_LOOKS   # never vacuous
    assert sorted(tiles) == sorted(names), sorted(tiles)

    def face(tile):
        img = QImage(tile.width(), tile.height(), QImage.Format_ARGB32)
        img.fill(0)
        tile.render(img)
        return img

    def visible(before, after):
        n = 0
        for y in range(before.height()):
            for x in range(before.width()):
                a, b = before.pixelColor(x, y), after.pixelColor(x, y)
                if (abs(a.red() - b.red()) + abs(a.green() - b.green())
                        + abs(a.blue() - b.blue())) >= 24:
                    n += 1
        return n

    for entry in APPEARANCE_LOOKS:
        name = str(entry["name"])
        tile = tiles[name]
        assert isinstance(tile, settings_app.LookTile), (name, type(tile))
        assert tile._design == entry["design"], (name, tile._design)
        assert tile.accessibleName() == entry["label"], name
        keep = dict(tile._colors)
        for key in ("idle", "listening", "thinking", "speaking"):
            before = face(tile)
            tile._colors[key] = QColor("#00ff00")
            moved = visible(before, face(tile))
            tile._colors[key] = keep[key]
            floor = 100 if key == "idle" else 20
            assert moved >= floor, (
                f"look {name} ({entry['design']}) shows only {moved} pixel(s) "
                f"of its {key} colour — the tile's face is not the look")

    # ONE shared painter: swap it for a sentinel and every tile must draw the
    # sentinel instead, in catalogue order. A private copy inside LookTile
    # would leave this list empty.
    sentinel_designs = []
    real_glyph = settings_app.paint_design_glyph

    def _sentinel(p, design, cx, cy, r, color, t, k):
        sentinel_designs.append(design)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(255, 0, 255, 255)))
        p.drawRect(QRectF(0.0, 0.0, 10.0, 10.0))

    settings_app.paint_design_glyph = _sentinel
    try:
        for entry in APPEARANCE_LOOKS:
            face(tiles[str(entry["name"])])
    finally:
        settings_app.paint_design_glyph = real_glyph
    assert sentinel_designs == [str(e["design"]) for e in APPEARANCE_LOOKS], \
        sentinel_designs

    # ONE timer for the strip, not one per tile
    for name, tile in tiles.items():
        assert not tile.findChildren(QTimer), (
            f"tile {name} owns its own timer — the strip shares one")
    assert win._look_anim.isActive()
    assert win._look_anim.interval() == settings_app.LookTile.TICK_MS

    # the TILE is the click target, not a helper called behind it
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    target = APPEARANCE_LOOKS[-1]
    tiles[str(target["name"])].click()
    win._apply_appearance_live()
    on_disk = _json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["bubble_design"] == target["design"], \
        on_disk["bubble_design"]
    assert on_disk["colors"] == target["colors"], on_disk["colors"]


@scenario
def voice_tab_level_meter_reads_the_bubble_feed():
    # The Voice tab's meter shows the SAME level signal the bubble's designs
    # paint from, so the feed can be diagnosed without watching the bubble.
    # Two things matter and neither is decorative: the settings app must speak
    # the control socket's `level` protocol (a mocked query would hide a typo
    # in the command name), and the meter must keep `raw` and `designs` apart —
    # a moving raw bar with a stuck designs tick IS the "the feed arrives but
    # the bubble never shows it" failure the meter exists to expose.
    import socket as _socket
    import threading as _threading

    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    assert win.tabs.indexOf(win._voice_page) == 1

    doc = {"raw": 0.42, "ui": 0.31, "source": "mic", "age_s": 1.25,
           "state": "listening", "handsfree": True}

    # -- the wire: the real command over a real unix socket
    sockdir = Path(os.environ["SGUI_HOME"]) / "sock"
    sockdir.mkdir(parents=True, exist_ok=True)
    sock_path = sockdir / "control.sock"
    seen = []

    def _serve():
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(4)
        srv.settimeout(15.0)
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                return
            try:
                seen.append(conn.recv(1024).decode("utf-8", "replace").strip())
                conn.sendall((json.dumps(doc) + "\\n").encode())
            except OSError:
                pass
            finally:
                conn.close()

    _threading.Thread(target=_serve, daemon=True).start()
    got = None
    deadline = time.time() + 5.0
    while got is None and time.time() < deadline:
        got = settings_app._level_query(sock_path)
    assert got == doc, f"settings must read the bubble's level reply, got {got!r}"
    assert seen and seen[0] == "level", (
        f"the meter must ask for `level`, not something else: {seen!r}")
    assert settings_app._level_query(sockdir / "absent.sock") is None

    # -- and the meter text for each truth it has to tell apart
    class _Feed:
        def __init__(self, payload):
            self.payload = payload

        def start(self):
            return None

        def stop(self):
            return None

        def latest(self):
            return self.payload

    win._level_feed = _Feed(doc)
    win._refresh_level()
    text = win.level_meter.text()
    assert "raw 0.42" in text and "designs 0.31" in text
    assert "mic (hands-free)" in text and "1.2 s ago" in text
    assert win.level_readout.text() == text, "the label shows exactly the meter"

    win._level_feed = _Feed({"raw": 0.8, "ui": 0.8, "source": "tts",
                             "age_s": None, "state": "speaking",
                             "handsfree": False})
    win._refresh_level()
    text = win.level_meter.text()
    assert "the bubble's own voice" in text, "playback is its own source"
    assert "last above zero never" in text

    win._level_feed = _Feed(None)
    win._refresh_level()
    assert "no reply" in win.level_meter.text()
    assert not win.level_meter.grab().isNull()   # paintEvent must not raise

    # -- polling follows the tab: no settings window may sit on the bubble's
    # socket (and on its single-threaded accept loop) while you read another tab
    win.tabs.setCurrentWidget(win._voice_page)
    assert win._level_timer.isActive()
    win.tabs.setCurrentIndex(0)
    assert not win._level_timer.isActive()
    win.tabs.setCurrentWidget(win._voice_page)
    assert win._level_timer.isActive()


@scenario
def external_change_reloads_and_reports():
    seed({"model": "third:latest"})
    win._check_disk_changes()
    assert win.cfg["model"] == "third:latest"
    assert "reloaded from disk" in win.status_label.text()
    seed({"model": "testmodel:latest"})
    win._check_disk_changes()
    assert win.cfg["model"] == "testmodel:latest"


@scenario
def missing_settings_file_mtime_is_zero():
    settings_file.unlink(missing_ok=True)
    assert win._settings_mtime() == 0.0


@scenario
def conversation_pane_renders_roles_and_tool_calls():
    seed(history=[
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi",
         "tool_calls": [{"function": {"name": "run_command"}}]},
        {"role": "tool", "content": "ok\\nsecond line"},
    ])
    win._refresh_history()
    text = win.history_view.toPlainText()
    assert "3 messages" in text
    assert "[user] hello there" in text
    assert "calls tool: run_command" in text
    assert "[tool result] ok" in text
    history_file.unlink(missing_ok=True)
    win._refresh_history()
    assert "history is empty" in win.history_view.toPlainText()


@scenario
def clear_history_keeps_backup():
    from PySide6.QtWidgets import QMessageBox

    seed(history=[{"role": "user", "content": "remember this"}])

    class _Yes(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.Yes

    saved = settings_app.QMessageBox
    settings_app.QMessageBox = _Yes
    try:
        win._clear_history()
    finally:
        settings_app.QMessageBox = saved
    assert "history cleared" in win.history_view.toPlainText()
    backups = list(config_dir.glob("history.json.bak-manual.*"))
    assert backups, "clear must keep a backup"


@scenario
def modelswitch_clears_history():
    # A model switch used to take a backup and leave the transcript in place,
    # so the new model inherited exactly the history its own comment blames for
    # parroting. It must now REALLY clear — and only on a real switch.
    seed({"model": "oldmodel:latest"},
         history=[{"role": "user", "content": "tuned for the old model"}])
    win.reload_from_disk()
    win.cfg["model"] = "newmodel:latest"
    assert win.save() is True
    assert json.loads(history_file.read_text(encoding="utf-8")) == []
    assert "Memory cleared" in win.status_label.text()
    assert list(config_dir.glob("history.json.bak-modelswitch.*"))

    # A disk reload must make the DISK's model the new baseline. Without that,
    # a save after an external model change (the window was already open) looks
    # like a switch and destroys a conversation the user never touched.
    kept = [{"role": "user", "content": "fresh conversation"}]
    seed({"model": "thirdmodel:latest"}, history=kept)
    win.reload_from_disk()
    win.thresh_spin.setValue(win.thresh_spin.value() + 1)
    assert win.save() is True
    assert json.loads(history_file.read_text(encoding="utf-8")) == kept
    assert "Memory cleared" not in win.status_label.text()


@scenario
def history_tab_renders_and_truncates():
    seed(history=[{"role": "user", "content": "x" * 500}])
    win._refresh_history()
    text = win.history_view.toPlainText()
    assert "1 messages" in text
    assert len(text) < 400          # per-message truncation
    history_file.write_text("not json at all", encoding="utf-8")
    win._refresh_history()
    assert "history is empty" in win.history_view.toPlainText()


@scenario
def keybinds_snippet_written_to_tmp_config():
    win._write_keybinds()
    snippet = config_dir / "niri-keybinds.kdl"
    assert snippet.exists()
    text = snippet.read_text(encoding="utf-8")
    # the snippet KDL-escapes inner quotes (backslash-quote)
    for action in ("toggle", "interrupt", "handsfree", "dictation"):
        assert action + "\\\\" in text or action + "\\\"" in text or action in text
        assert "--ptt" in text
    assert "snippet written" in win.status_label.text()


@scenario
def restart_and_log_guards():
    win._on_restart_bubble()
    assert "restart script missing" in win.status_label.text()
    win._show_log()
    assert "no log file yet" in win.status_label.text()


@scenario
def apply_autostart_disabled_is_noop():
    msg = settings_app.apply_autostart(False)
    assert "nothing to change" in msg or "unchanged" in msg


@scenario
def health_line_reports_not_running():
    bubble.CONTROL_SOCK = state_dir / "absent.sock"
    win._refresh_health()
    _spin_health(win)
    assert "not running" in win.health_label.text()
    assert "settings still work" in win.health_label.text()
    assert win.health_label.styleSheet() == "color: palette(mid);"


@scenario
def fmt_health_ok_and_degraded():
    ok = {"mic": {"state": "listening", "device": "d" * 50, "rate": 16000,
                  "utterances": 3},
          "brain": {"reachable": True, "model": "m:1"},
          "tts": {"ready": True}, "stt": {"ready": True}}
    line = settings_app._fmt_health(ok)
    assert "mic: listening" in line and "brain: ok m:1" in line
    assert "\\u2026" in line or "\\u2026".encode().decode() in line or "…" in line
    degraded = {"mic": {"state": "silent"},
                "brain": {"reachable": False}, "tts": {}, "stt": {}}
    assert "silent" in settings_app._fmt_health(degraded)


@scenario
def health_query_bad_inputs():
    assert settings_app._health_query(state_dir / "no-such.sock") is None
    assert settings_app._health_query(None) is None


@scenario
def policy_rows_roundtrip():
    assert len(win.policy_rows) >= 40          # the real tool census
    rows = win.policy_rows["run_command"]
    rows.setCurrentIndex(1)                    # DENY
    win._collect()
    assert win.cfg["command_policy"]["run_command"] == "DENY"
    rows.setCurrentIndex(0)                    # ALLOW -> not persisted
    win._collect()
    assert "run_command" not in win.cfg["command_policy"]


@scenario
def remote_ollama_checkbox_roundtrip():
    win.remote_ollama_chk.setChecked(True)
    win._collect()
    assert win.cfg["allow_remote_ollama"] is True
    win.remote_ollama_chk.setChecked(False)
    win._collect()
    assert win.cfg["allow_remote_ollama"] is False


@scenario
def model_picker_and_tts_reference():
    from PySide6.QtWidgets import QListWidgetItem
    from PySide6.QtCore import Qt
    win.model_list.clear()
    it = QListWidgetItem("pickme:latest")
    it.setData(Qt.UserRole, "pickme:latest")
    win.model_list.addItem(it)
    win.model_list.setCurrentItem(it)
    assert win._selected_model() == "pickme:latest"
    # The voice combo + download dialog are gone (one built-in voice); what is
    # left is an optional reference clip, and the panel must report it the way
    # the engine will judge it — a short clip is a MUTE bubble, not a nuance.
    win.refresh_tts()
    assert "chatterbox-turbo" in win.tts_engine_label.text()
    assert win.tts_ref_status.text() == "" or "weights" in win.tts_ref_status.text()
    win.tts_ref_edit.setText(str(short_clip))
    win.refresh_tts()
    assert "⚠" in win.tts_ref_status.text() and "5" in win.tts_ref_status.text(), \
        win.tts_ref_status.text()
    win.tts_ref_edit.setText(str(ok_clip))
    win.refresh_tts()
    assert "optimus" in win.tts_ref_status.text()
    win._collect()
    assert win.cfg["tts_reference"] == str(ok_clip)
    win.tts_ref_edit.setText("")
    win.refresh_tts()
    assert "built-in" in win.tts_ref_status.text(), win.tts_ref_status.text()


@scenario
def tabs_and_colors():
    texts = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    for expected in ("Brain", "Voice", "Permissions",
                     "Appearance", "Startup", "History"):
        assert expected in texts, texts
    assert "Memory" not in texts  # folded into History → Durable facts
    assert set(win._colors) == set(win.cfg["colors"])
    win._colors["idle"] = "#123456"
    win._collect()
    assert win.cfg["colors"]["idle"] == "#123456"


@scenario
def clear_history_via_stubbed_dialog():
    from PySide6.QtWidgets import QMessageBox

    seed(history=[{"role": "user", "content": "bye"}])

    class _Yes(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.Yes

    saved = settings_app.QMessageBox
    settings_app.QMessageBox = _Yes
    try:
        win._clear_history()
    finally:
        settings_app.QMessageBox = saved
    backups = list(config_dir.glob("history.json.bak-manual.*"))
    assert backups and "(history cleared" in win.history_view.toPlainText()

    class _No(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.No

    settings_app.QMessageBox = _No
    try:
        seed(history=[{"role": "user", "content": "keep me"}])
        win._clear_history()
        assert history_file.exists()
        assert history_file.read_text(encoding="utf-8") == json.dumps(
            [{"role": "user", "content": "keep me"}])
    finally:
        settings_app.QMessageBox = saved


@scenario
def autostart_enable_migrates_old_line():
    NIRI.write_text(
        f"// niri config\\n{settings_app.AUTOSTART_LINE_OLD}\\n",
        encoding="utf-8")
    assert "migrated" in settings_app.set_autostart(True)
    text = NIRI.read_text(encoding="utf-8")
    assert text.count(settings_app.AUTOSTART_LINE) == 1
    assert settings_app.AUTOSTART_LINE_OLD not in text
    assert (NIRI.parent / "config.kdl.bak-handsoff").exists()

    # Already-exactly-one new line: idempotent enable, then clean removal.
    NIRI.write_text(
        f"// niri config\\n{settings_app.AUTOSTART_LINE}\\n", encoding="utf-8")
    assert settings_app.set_autostart(True) == "autostart unchanged"
    assert "removed" in settings_app.set_autostart(False)
    assert settings_app.AUTOSTART_LINE not in NIRI.read_text(encoding="utf-8")


@scenario
def history_view_renders_decisions():
    seed(history=[
        {"role": "user", "content": "what time is it?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "get_time"}}]},
        {"role": "tool", "content": "12:34\\nextra"},
        {"role": "assistant", "content": "It is 12:34."},
    ])
    win._refresh_history()
    text = win.history_view.toPlainText()
    assert "4 messages — newest last:" in text
    assert "[user] what time is it?" in text
    assert "calls tool: get_time" in text
    assert "[tool result] 12:34 extra" in text
    assert "[assistant] It is 12:34." in text

    seed(history=[])
    win._refresh_history()
    assert "(history is empty" in win.history_view.toPlainText()


@scenario
def facts_pane_lists_forgets_and_dedups():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    memory_file.write_text(json.dumps([
        {"k": "name", "v": "the user's name is Alice"},
        {"k": "name", "v": "the user's name is Bob"},
        {"k": "dog", "v": "the user has a dog named Rex"},
    ]), encoding="utf-8")
    win._refresh_facts()
    assert win.facts_list.count() == 2     # deduped by key, newest value wins
    texts = [win.facts_list.item(i).text()
             for i in range(win.facts_list.count())]
    assert any("Bob" in t for t in texts)
    assert not any("Alice" in t for t in texts)
    assert any("Rex" in t for t in texts)

    rex_row = next(i for i in range(win.facts_list.count())
                   if "Rex" in win.facts_list.item(i).text())
    win.facts_list.setCurrentRow(rex_row)

    class _Yes(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.Yes

    saved = settings_app.QMessageBox
    settings_app.QMessageBox = _Yes
    try:
        win._forget_fact()
    finally:
        settings_app.QMessageBox = saved
    # only the exact fact is removed from the file; key dupes stay until the
    # bubble's next merge — but the PANE shows the deduped view immediately
    assert json.loads(memory_file.read_text(encoding="utf-8")) == [
        {"k": "name", "v": "the user's name is Alice"},
        {"k": "name", "v": "the user's name is Bob"}]
    assert list(config_dir.glob("memory.json.bak-facts.*")), \
        "forget must keep a backup"
    win._refresh_facts()
    assert win.facts_list.count() == 1
    assert "Bob" in win.facts_list.item(0).text()

    # 'No' in the confirm dialog leaves the fact in place
    win.facts_list.setCurrentRow(0)
    class _No(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.No

    settings_app.QMessageBox = _No
    try:
        win._forget_fact()
    finally:
        settings_app.QMessageBox = saved
    assert len(json.loads(memory_file.read_text(encoding="utf-8"))) == 2

    memory_file.unlink(missing_ok=True)
    win._refresh_facts()
    assert win.facts_list.item(0).text() == "(no durable facts yet)"


@scenario
def decision_log_renders_and_tolerates_garbage():
    win._refresh_decisions()
    assert "no decisions logged yet" in win.decisions_view.toPlainText()

    entries = [
        {"id": "abc-1", "ts": "2026-09-10T12:00:00+02:00",
         "tool": "run_command", "target": "pactl set-sink-mute @DEFAULT_SINK@ 1",
         "decision": "ALLOW", "result": "dispatched"},
        "not json at all\\n",
        {"id": "abc-2", "ts": "2026-09-10T12:00:05+02:00",
         "tool": "reboot_system", "target": "now",
         "decision": "CONFIRM", "result": "proposed"},
    ]
    decisions_file.write_text(
        "".join(e if isinstance(e, str) else json.dumps(e) + "\\n"
                for e in entries),
        encoding="utf-8")
    win._refresh_decisions()
    text = win.decisions_view.toPlainText()
    assert "2 decisions — newest last:" in text
    assert "run_command" in text and "pactl set-sink-mute @DEFAULT_SINK@ 1" in text
    assert "ALLOW" in text and "dispatched" in text
    assert "CONFIRM" in text and "reboot_system" in text
    assert "proposed" in text and "#abc-2" in text
    assert "not json" not in text   # garbage line skipped, not fatal

    decisions_file.unlink(missing_ok=True)
    win._refresh_decisions()
    assert "no decisions logged yet" in win.decisions_view.toPlainText()


@scenario
def the_image_design_draws_the_users_picture():
    # The `image` design's art is a FILE, so everything that can go wrong is
    # about the file: absent, unreadable, a folder, the wrong bytes, or
    # invisible. Three properties are load-bearing and this pins all three.
    #
    #   the picture really reaches the surface — decoding, tinting and fitting
    #     are three separate steps, and a silent failure in any one of them
    #     leaves the empty slot, which reads as "my picture was ignored";
    #   the state colour still wins over it — the picture contributes its
    #     silhouette and luminance, and the 0.45 floor is what stops a black
    #     picture from erasing the colour the user chose;
    #   none of it leaves the aperture, at every radius and voice level, which
    #     is the whole return on fitting the picture's DIAGONAL rather than its
    #     width.
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QImage, QPainter

    class _Signal:
        def connect(self, *_a, **_k):
            return None

    class _Stub:
        sigState = _Signal()
        sigLevel = _Signal()

    class _Clock:
        def __init__(self, ms):
            self.ms = ms

        def elapsed(self):
            return self.ms

    W = appearance.WINDOW_PX
    widget = appearance.BubbleWidget(_Stub())
    widget.resize(W, W)
    widget._anim.stop()
    widget._clock = _Clock(4000)
    widget._state = "idle"
    widget._level_ui = widget._level_target = 0.0
    widget._radius_ui = appearance.BUBBLE_R0
    widget._energy_ui = appearance._fx_energy("idle")
    appearance.ANIM_ENERGY = 1.0
    appearance.BUBBLE_ACCENT = 0.5
    bubble.SETTINGS["bubble_design"] = "image"

    def shot(color="#2f6fed"):
        c = QColor(color)
        widget._color_ui = [c.redF(), c.greenF(), c.blueF()]
        img = QImage(W, W, QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        return img

    def opaque_pixels(img, alpha=200):
        return sum(1 for y in range(W) for x in range(W)
                   if img.pixelColor(x, y).alpha() > alpha)

    def reach_of(img, alpha=200):
        return max(((x + 0.5 - W / 2.0) ** 2 + (y + 0.5 - W / 2.0) ** 2) ** 0.5
                   for y in range(W) for x in range(W)
                   if img.pixelColor(x, y).alpha() > alpha)

    folder = settings_file.parent

    # --- no picture chosen: the empty slot, and NOT the orb. An orb here is
    # --- indistinguishable from a design name with no dispatch branch.
    bubble.SETTINGS["design_image_path"] = ""
    slot = shot()
    slot_px = opaque_pixels(slot)
    assert 100 < slot_px < 2500, slot_px
    assert appearance.design_image() is None
    assert appearance.design_image_problem() == "", (
        "an unchosen picture is the starting state, not a problem")

    # --- a real picture draws, and fills the glass. The art is SQUARE so the
    # --- avatar's ink radius is a stated property of the test (inscribed
    # --- circle of the fit circle), not an accident of an aspect ratio.
    art = folder / "art.png"
    black = QImage(128, 128, QImage.Format_ARGB32)
    black.fill(QColor(0, 0, 0, 255))
    assert black.save(str(art))
    bubble.SETTINGS["design_image_path"] = str(art)
    assert appearance.design_image_problem() == "", (
        appearance.design_image_problem())
    # The ring's coerced DEFAULT is on, and the harness loads a real settings
    # dict — so the no-decoration baseline is pinned here, not assumed.
    appearance.SETTINGS["avatar_ring"] = "off"
    assert appearance.avatar_ring_on() is False
    drawn = shot()
    drawn_px = opaque_pixels(drawn)
    assert drawn_px > slot_px * 5, (
        f"the picture never reached the surface: {drawn_px} opaque pixels "
        f"against the empty slot's {slot_px} — decode, tint and fit are three "
        f"steps and any one of them failing silently looks like this")
    reach = reach_of(drawn)
    assert reach <= appearance.APERTURE_R, (reach, appearance.APERTURE_R)
    # The avatar is ROUND now: full-bleed art is clipped to a feathered circle
    # (`_round_avatar`), so its ink stops inside the fit circle. The floor is
    # spelled out factor by factor and PINNED HERE, not read from the painter:
    # the fit policy 0.88 of the aperture, the breath's deepest sway −2%, the
    # square art's inscribed fraction 1/√2, the feather's solid floor 86%, and
    # an antialiasing margin. A painter that retunes any of these must change
    # this number on purpose — while a fit that shrinks the avatar away still
    # fails the test, which is what it exists to catch.
    floor = (appearance.APERTURE_R * 0.88 * 0.98 * (2 ** -0.5) * 0.86) - 1.5
    assert reach > floor, (
        f"the picture collapsed to {reach:.1f} px (floor {floor:.1f}) — "
        f"the fit is shrinking it away")

    # --- the ring light: decoration AROUND the avatar. It takes its band out
    # --- of the picture's fit by design (0.76 vs 0.88), so ring-on must draw
    # --- arcs the picture does not cover, must keep every arc inside the
    # --- aperture, must visibly shrink the picture into its own circle, and
    # --- must vanish the moment the setting says off.
    a = appearance.APERTURE_R

    def new_ink(img, base):
        # The ink the ring ADDS: opaque in the decorated shot, transparent in
        # the baseline. Measuring an annulus instead would count the rim's own
        # halo (which sits in any band wide enough to hold the arcs) and, worse,
        # would count it as decoration; this difference is exactly the ink the
        # decoration is responsible for. Threshold 25, not 200 — the arcs are
        # decoration, not a filled disc.
        return sum(1 for y in range(W) for x in range(W)
                   if img.pixelColor(x, y).alpha() > 25
                   and base.pixelColor(x, y).alpha() <= 25)

    def reach_within(img, r_cap):
        vals = [((x + 0.5 - W / 2.0) ** 2 + (y + 0.5 - W / 2.0) ** 2) ** 0.5
                for y in range(W) for x in range(W)
                if ((x + 0.5 - W / 2.0) ** 2 + (y + 0.5 - W / 2.0) ** 2) ** 0.5 <= r_cap
                and img.pixelColor(x, y).alpha() > 200]
        return max(vals) if vals else 0.0

    appearance.SETTINGS["avatar_ring"] = "ring-light"
    assert appearance.avatar_ring_on() is True
    lit = shot()
    assert new_ink(lit, drawn) > 40, (
        f"the ring light added almost no ink ({new_ink(lit, drawn)} px)"
        f" — it is drawn under the avatar instead of around it")
    assert reach_of(lit) <= appearance.APERTURE_R, (
        reach_of(lit), appearance.APERTURE_R)
    assert reach_within(lit, 0.5 * a) < reach - 2.0, (
        f"the ring did not take its band out of the picture's fit "
        f"({reach_within(lit, 0.5 * a):.1f} against {reach:.1f})")
    appearance.SETTINGS["avatar_ring"] = "off"
    assert appearance.avatar_ring_on() is False
    assert new_ink(shot(), drawn) == 0, (
        "the ring is still drawn after the setting goes back to off")

    # --- ink stays inside the aperture for BOTH revisions, at every radius the
    # --- state machine can ask for and both voice extremes. Direct paint, the
    # --- same instrument the generic aperture guard uses.
    region = appearance.design_region("image", W, W)
    outside = [(x, y) for y in range(W) for x in range(W)
               if not region.contains(QPoint(x, y))]

    def worst_outside():
        worst = (0, None)
        for clock_ms in (0, 600, 2600, 4000, 9000):
            widget._clock = _Clock(clock_ms)
            for state in ("idle", "listening", "thinking", "speaking"):
                widget._state = state
                widget._energy_ui = appearance._fx_energy(state)
                for level in (0.0, 1.0):
                    for grow in (0.0, 3.5, 7.0, 12.0):
                        widget._level_target = widget._level_ui = level
                        widget._radius_ui = (appearance.BUBBLE_R0
                                             + grow * appearance.GEOM_K)
                        img = QImage(W, W, QImage.Format_ARGB32)
                        img.fill(0)
                        painter = QPainter(img)
                        widget._paint_image(painter, widget._frame())
                        painter.end()
                        n = peak = 0
                        for x, y in outside:
                            a = img.pixelColor(x, y).alpha()
                            if a >= 16:
                                n += 1
                                peak = max(peak, a)
                        if n > worst[0]:
                            worst = (n, (clock_ms, state, level, grow, peak))
        return worst

    assert worst_outside() == (0, None), worst_outside()

    # ...and the whole sweep again for EVERY decoration. The decoration is the
    # outermost ink this design can put down, so each one is a separate claim
    # on the aperture — swept over the decoration names, because "the ring is
    # fine" says nothing about the comets or the ribbons.
    from settings_schema import AVATAR_DECOS
    for deco in AVATAR_DECOS:
        appearance.SETTINGS["avatar_ring"] = deco
        assert worst_outside() == (0, None), (deco, worst_outside())
    appearance.SETTINGS["avatar_ring"] = "off"

    white = QImage(3000, 2000, QImage.Format_ARGB32)   # big AND bright
    white.fill(QColor(255, 255, 255, 255))
    assert white.save(str(art))
    assert worst_outside() == (0, None), worst_outside()
    # ...and the working canvas is BOUNDED, whatever the file's dimensions: a
    # 3000x2000 photo is 24 MB and the tint is a copy per painted frame, so
    # this is a memory ceiling, not a nicety. The drawn rect's diagonal is at
    # most ~169 px at the largest window, so 384 is still oversampled.
    layers = appearance.image_layers(str(art))
    assert layers is not None, "the big picture must still decode"
    assert max(layers[0].width(), layers[0].height()) <= 384, (
        f"the working canvas grew to {layers[0].width()}x{layers[0].height()} "
        f"for a 3000x2000 file — every frame after this one pays for it")

    # --- the state colour still wins over the picture, and the voice still
    # --- moves it: the two channels the Appearance tab promises.

    def frame_at(level, color, clock):
        widget._clock = _Clock(clock)
        widget._level_target = widget._level_ui = level
        return bytes(shot(color).constBits())

    def changed(before, after):
        n = min(len(before), len(after))
        return sum(1 for i in range(0, n - 3, 4)
                   if any(x != y for x, y in zip(before[i:i + 3],
                                                 after[i:i + 3])))

    def visible(before, after):
        n = min(len(before), len(after))
        out = 0
        for i in range(0, n - 3, 4):
            if before[i + 3] == 0 and after[i + 3] == 0:
                continue
            if max(abs(x - y) for x, y in zip(before[i:i + 3],
                                              after[i:i + 3])) >= 48:
                out += 1
        return out

    base = frame_at(0.0, "#2f6fed", 4000)
    steps = [visible(base, frame_at(0.0, other, 4000))
             for other in ("#e0435c", "#c8781f", "#1fae62")]
    assert max(steps) >= 400, (
        f"the state colour changes only {max(steps)} pixel(s) by a visible "
        f"step over a BLACK picture — the colour picker is disabled on the "
        f"design whose art the user chose")
    # ...and the PICTURE ITSELF carries the colour, not only its rim. A rim
    # alone would satisfy the whole-window bar above while the body stayed
    # black: that is the `void` defect (8 visible px of 45 796) with the user's
    # own file as the dark body, and it is what the 0.45 luminance floor in
    # `_shading_layer` exists to prevent. Measured on the CENTRE of the fit
    # circle, which is where the picture is and the rim is not.
    centre = [(x, y) for y in range(W // 2 - 20, W // 2 + 20)
              for x in range(W // 2 - 20, W // 2 + 20)]

    def shot_at(level, color):
        widget._clock = _Clock(4000)
        widget._level_target = widget._level_ui = level
        c = QColor(color)
        widget._color_ui = [c.redF(), c.greenF(), c.blueF()]
        img = QImage(W, W, QImage.Format_ARGB32)
        img.fill(0)
        widget.render(img)
        return img

    ref = shot_at(0.0, "#2f6fed")
    other = shot_at(0.0, "#e0435c")
    lit = 0
    for x, y in centre:
        a, b = ref.pixelColor(x, y), other.pixelColor(x, y)
        if a.alpha() == 0 and b.alpha() == 0:
            continue
        if max(abs(a.red() - b.red()), abs(a.green() - b.green()),
               abs(a.blue() - b.blue())) >= 48:
            lit += 1
    assert lit >= 200, (
        f"only {lit} of the {len(centre)} pixels at the CENTRE of the picture "
        f"show the state colour — the rim is carrying it while the user's own "
        f"picture stays black, which is the `void` defect in a new costume")
    assert changed(frame_at(0.0, "#2f6fed", 4000),
                   frame_at(1.0, "#2f6fed", 4000)) >= 100, (
        "the voice does not reach the picture")

    # --- every way the file can fail is NAMED, and each failure draws the slot
    # --- rather than a blank window.
    missing = folder / "nope.png"
    junk = folder / "junk.png"
    junk.write_text("this is not an image", encoding="utf-8")
    clear = QImage(64, 64, QImage.Format_ARGB32)
    clear.fill(QColor(0, 0, 0, 0))
    invisible = folder / "clear.png"
    assert clear.save(str(invisible))

    cases = {
        str(missing): "no file at",
        str(folder): "is a folder",
        str(junk): "not an image this build can read",
        str(invisible): "transparent",
    }
    for path, phrase in cases.items():
        problem = appearance.design_image_problem(path)
        assert phrase in problem, (path, phrase, problem)
        bubble.SETTINGS["design_image_path"] = path
        assert appearance.design_image() is None, path
        # compared against the no-picture render AT THIS SAME FRAME: the slot's
        # dashes turn with the animation energy, so a capture from earlier in
        # the scenario would differ for reasons that have nothing to do with
        # the broken path
        broken = opaque_pixels(shot())
        bubble.SETTINGS["design_image_path"] = ""
        empty = opaque_pixels(shot())
        # Compared by INK rather than by bytes: rendering a translucent widget
        # differs in a handful of antialiased pixels between two identical
        # frames, which a byte comparison reads as a failure. The stale picture
        # this pins is ~5x the slot's ink, so the tolerance is not blind.
        assert abs(broken - empty) <= 20, (
            f"{path} draws {broken} ink pixels against the empty slot's "
            f"{empty} — a broken path must fall back to the slot instead of "
            f"leaving the last picture that decoded on screen")
    assert opaque_pixels(shot()) > 100, "the empty slot must still be visible"

    # --- doctor says the same sentence the picker shows, and says nothing when
    # --- there is nothing wrong (a partial deps object must not grow a line).
    bubble.SETTINGS["design_image_path"] = str(missing)
    assert "no file at" in bubble._appearance_note(), bubble._appearance_note()
    bubble.SETTINGS["design_image_path"] = ""
    assert "image:" not in bubble._appearance_note(), bubble._appearance_note()

    # --- editing the file on disk repaints without a restart: the revision key
    # --- exists so that a re-export is picked up, not so it is cached forever.
    mid = QImage(180, 120, QImage.Format_ARGB32)
    mid.fill(QColor(128, 128, 128, 255))
    assert mid.save(str(art))
    bubble.SETTINGS["design_image_path"] = str(art)
    first = bytes(shot().constBits())
    bright = QImage(180, 120, QImage.Format_ARGB32)
    bright.fill(QColor(255, 255, 255, 255))
    assert bright.save(str(art))
    assert bytes(shot().constBits()) != first, (
        "an edited picture is still cached: the key must carry mtime/size")

    # --- the Appearance tab reaches the art: it is one of the live keys (the
    # --- defect that made a colour-only edit invisible to the tab), it writes
    # --- to disk without Save, and the name of what moved says so.
    assert "design_image_path" in win.APPEARANCE_KEYS, win.APPEARANCE_KEYS
    assert win.design_combo.findData("image") >= 0, "no picker for the design"
    # ...and a reload picks the saved file up, rather than showing the empty
    # slot the form would otherwise claim while the bubble draws a picture
    # (the shape of "the GUI says one thing and the bubble does another").
    seed({"model": "testmodel:latest", "design_image_path": str(art)})
    win.reload_from_disk()
    assert win._design_image == str(art), win._design_image
    assert win.image_label.text() == str(art), win.image_label.text()

    # the picker itself: a cancelled dialog writes NOTHING (closing a file
    # chooser is not an edit), and a chosen file lands in the form
    real_open = settings_app.QFileDialog.getOpenFileName
    try:
        # cancelling with a picture ALREADY chosen must leave it alone: an
        # unguarded picker reads the empty answer as "clear it", so closing a
        # file chooser silently drops the art (and the live apply writes that)
        settings_app.QFileDialog.getOpenFileName = lambda *a, **k: ("", "")
        win._design_image = str(art)
        win._refresh_design_image_label()
        win._pick_design_image()
        assert win._design_image == str(art), (
            "a cancelled picker changed the chosen picture")
        assert win.image_label.text() == str(art), win.image_label.text()
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(art), ""))
        win._design_image = ""
        win._pick_design_image()
        assert win._design_image == str(art), win._design_image
        assert "No fallback picture" not in win.image_label.text()
    finally:
        settings_app.QFileDialog.getOpenFileName = real_open

    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    win._design_image = str(art)
    win._refresh_design_image_label()
    assert win.image_label.text() == str(art), win.image_label.text()
    win._apply_appearance_live()
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["design_image_path"] == str(art), on_disk.get(
        "design_image_path")
    assert "fallback image" in win.status_label.text(), win.status_label.text()
    win._clear_design_image()
    win._apply_appearance_live()
    assert json.loads(settings_file.read_text())["design_image_path"] == ""
    assert "No fallback picture" in win.image_label.text(), win.image_label.text()

    # --- and the PREVIEW shows the picture, not the slot: a panel showing the
    # --- empty slot while the bubble draws the photo is the preview lying about
    # --- the one thing the combo above it selects.
    surface = QImage(120, 120, QImage.Format_ARGB32)
    surface.fill(0)
    painter = QPainter(surface)
    painter.setRenderHint(QPainter.Antialiasing, True)
    win._design_image = str(art)
    assert win.preview._draw_image_glyph(
        painter, 60.0, 60.0, 40.0, QColor("#4f8cff")) is True
    painter.end()
    assert opaque_pixels(surface, 8) > 0
    win._design_image = str(junk)
    painter = QPainter(surface)
    assert win.preview._draw_image_glyph(
        painter, 60.0, 60.0, 40.0, QColor("#4f8cff")) is False, (
        "an unreadable file must fall back to the slot glyph, not draw junk")
    painter.end()
    # ...and the preview's PAINT actually takes that path. Without this the
    # strip could hold a correct `_draw_image_glyph` and simply never call it:
    # the panel would show the empty slot while the bubble drew the photo,
    # which is the lie the glyph exists to prevent.
    class _FrozenClock:
        def elapsed(self):
            return 4000

    win.preview.resize(240, 200)
    calls = []
    real_draw = settings_app.BubblePreview._draw_image_glyph

    def _recording_draw(self, painter, cx, cy, r, color, state=""):
        out = real_draw(self, painter, cx, cy, r, color, state)
        calls.append(out)
        return out

    def preview_frame(image_path):
        win.design_combo.setCurrentIndex(win.design_combo.findData("image"))
        win._design_image = image_path
        win.preview._clock = _FrozenClock()
        img = QImage(win.preview.width(), win.preview.height(),
                     QImage.Format_ARGB32)
        img.fill(0)
        win.preview.render(img)
        return img

    settings_app.BubblePreview._draw_image_glyph = _recording_draw
    try:
        preview_frame(str(art))
        with_picture = list(calls)
        del calls[:]
        preview_frame(str(junk))
        with_slot = list(calls)
    finally:
        settings_app.BubblePreview._draw_image_glyph = real_draw
    assert len(with_picture) == 4 and all(with_picture), (
        f"the strip's paint did not draw the picture in its four slots "
        f"({with_picture}) — a correct glyph that is never called is a preview "
        f"that lies about the thing the combo above it selects")
    assert len(with_slot) == 4 and not any(with_slot), (
        f"the strip drew an unreadable file instead of falling back "
        f"({with_slot})")

    win._design_image = ""
    assert "No fallback picture" in win.image_label.text()
    art.unlink(missing_ok=True)
    junk.unlink(missing_ok=True)
    invisible.unlink(missing_ok=True)


@scenario
def the_image_design_can_use_an_installed_pack():
    # A PACK is the Image design's art as a FOLDER: a pack.json naming one
    # picture per state, installed from the Appearance tab so ONE choice
    # switches several pictures together. The pack layer is unit-tested in
    # tests/test_design_packs.py; what this pins is the wiring the user touches
    # — the picker lists what is installed, the choice reaches disk and the
    # bubble live, the preview shows the four pictures rather than one, and the
    # reason a pack cannot draw is the same sentence doctor prints.
    import json as _json
    import shutil as _shutil
    from PySide6.QtGui import QColor, QImage

    from settings_schema import BUBBLE_DESIGNS

    folder = settings_file.parent
    packs_root = config_dir / "design-packs"
    appearance.PACKS_DIR = packs_root
    _shutil.rmtree(packs_root, ignore_errors=True)

    # --- a real pack: one distinctly coloured picture per state, so "which
    # --- picture is drawn" is answerable by looking rather than by trusting a
    # --- path (all four being the same file would pass a path check).
    source = folder / "pack-src"
    source.mkdir(parents=True, exist_ok=True)
    tints = {"idle": (220, 60, 60, 255), "listening": (60, 220, 60, 255),
             "thinking": (60, 60, 220, 255), "speaking": (220, 220, 60, 255)}
    for state, rgba in tints.items():
        img = QImage(64, 64, QImage.Format_ARGB32)
        img.fill(QColor(*rgba))
        assert img.save(str(source / (state + ".png")))
    (source / "pack.json").write_text(_json.dumps({
        "name": "Prism",
        "states": {s: s + ".png" for s in tints},
    }), encoding="utf-8")

    slug, message = appearance.install_pack(source)
    assert slug == "prism", (slug, message)
    assert appearance.pack_problem(slug) == ""
    assert (slug, "Prism") in appearance.installed_packs(), (
        appearance.installed_packs())

    # --- one picture per state, and the pack beats the single file: a chosen
    # --- pack is the AUTHORITY, so the file the user picked earlier must not
    # --- stand in for it.
    single = folder / "single.png"
    solo = QImage(40, 40, QImage.Format_ARGB32)
    solo.fill(QColor(255, 255, 255, 255))
    assert solo.save(str(single))
    for state in tints:
        resolved = appearance.picture_for(slug, str(single), state)
        assert os.path.basename(resolved) == state + ".png", (state, resolved)

    # --- the picker lists it, and picking it is an EDIT that reaches disk.
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    win._refresh_pack_combo()
    index = win.pack_combo.findData(slug)
    assert index >= 0, [win.pack_combo.itemText(i)
                        for i in range(win.pack_combo.count())]
    win.pack_combo.setCurrentIndex(index)
    assert win._design_pack == slug, win._design_pack
    assert "image" in BUBBLE_DESIGNS
    assert win.design_combo.currentData() == "image", (
        "choosing art must point the shape at the design that draws it, or the "
        "pick lands on a painter and nothing appears")
    win._apply_appearance_live()
    on_disk = _json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["design_pack"] == slug, on_disk.get("design_pack")
    assert "design pack" in win.status_label.text(), win.status_label.text()
    # ...and the KEY the panel writes is the key the BUBBLE reads: a renamed
    # setting would leave the picker saving a value nothing resolves, which is
    # the same defect one name along.
    bubble.SETTINGS["design_pack"] = slug
    assert os.path.basename(appearance.design_picture("listening")) == (
        "listening.png")
    bubble.SETTINGS["design_pack"] = ""

    # --- the panel tells the truth about which source is drawing it
    assert "is in use" in win.pack_label.text(), win.pack_label.text()
    assert "not used while it is" in win.image_label.text(), (
        win.image_label.text())
    # ...and it stops claiming that the moment the shape is moved back to a
    # painter: a label still saying "in use" while the bubble draws an orb lies
    # about which source is on screen.
    win.design_combo.setCurrentIndex(win.design_combo.findData("orb"))
    assert "not on screen" in win.pack_label.text(), win.pack_label.text()
    assert win._design_pack == slug, "a shape change must not clear the pack"
    win.design_combo.setCurrentIndex(win.design_combo.findData("image"))
    assert "is in use" in win.pack_label.text(), win.pack_label.text()

    # --- the PREVIEW shows the pack's four pictures, not one picture four
    # --- times. The slot's state must reach the glyph, or a pack looks like a
    # --- single image and the whole format is a lie in the panel.
    seen = []
    real_draw = settings_app.BubblePreview._draw_image_glyph

    def _recording(self, painter, cx, cy, r, color, state=""):
        out = real_draw(self, painter, cx, cy, r, color, state)
        seen.append((state, out))
        return out

    class _FrozenClock:
        def elapsed(self):
            return 4000

    settings_app.BubblePreview._draw_image_glyph = _recording
    try:
        win.preview.resize(320, 200)
        win.preview._clock = _FrozenClock()
        img = QImage(win.preview.width(), win.preview.height(),
                     QImage.Format_ARGB32)
        img.fill(0)
        win.preview.render(img)
    finally:
        settings_app.BubblePreview._draw_image_glyph = real_draw
    assert [s for s, ok in seen] == ["idle", "listening", "thinking",
                                     "speaking"], seen
    assert all(ok for _s, ok in seen), seen
    for state in tints:
        assert win._design_picture(state) == appearance.picture_for(
            slug, win._design_image, state), state

    # --- installing through the GUI: the dialog answers, the copy lands, the
    # --- form and the combo follow, and the message is the module's own.
    second = folder / "pack-src-2"
    second.mkdir(parents=True, exist_ok=True)
    img = QImage(48, 48, QImage.Format_ARGB32)
    img.fill(QColor(10, 200, 240, 255))
    assert img.save(str(second / "base.png"))
    (second / "pack.json").write_text(_json.dumps(
        {"name": "Solo", "any": "base.png"}), encoding="utf-8")
    real_dir = settings_app.QFileDialog.getExistingDirectory
    try:
        # Closing the chooser is not an edit: nothing selected, nothing copied.
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: "")
        win._design_pack = slug
        win._install_design_pack()
        assert win._design_pack == slug, (
            "a cancelled folder chooser is not an edit")
        assert not (packs_root / "solo").exists(), (
            "a cancelled chooser installed the pack anyway")
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(second))
        win._install_design_pack()
        assert "installed pack" in win.status_label.text(), (
            win.status_label.text())
        assert "is in use" in win.pack_label.text(), win.pack_label.text()
        # ...and a folder that is NOT a pack is REFUSED with the module's own
        # sentence, leaving the selection alone rather than selecting a dud
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(folder))
        win._install_design_pack()
        assert win._design_pack == "solo", "a refused install must not select"
        assert "no pack.json" in win.status_label.text(), win.status_label.text()
    finally:
        settings_app.QFileDialog.getExistingDirectory = real_dir
    assert win._design_pack == "solo", win._design_pack
    assert win.pack_combo.findData("solo") >= 0, (
        "an install must appear in the picker without a restart")
    win._apply_appearance_live()
    assert _json.loads(settings_file.read_text())["design_pack"] == "solo"
    # ...and the pack is now the preview's source, for every state
    for state in tints:
        assert win._design_picture(state).endswith("base.png"), state

    # --- Clearing goes back to the single picture, live, like every control.
    win._clear_design_pack()
    win._apply_appearance_live()
    assert _json.loads(settings_file.read_text())["design_pack"] == ""
    assert "No pack" in win.pack_label.text(), win.pack_label.text()

    # --- a pack that cannot draw: named in the panel AND in doctor, with ONE
    # --- sentence, and never silently substituted by the single file.
    seed({"model": "testmodel:latest", "design_pack": "ghost"})
    win.reload_from_disk()
    assert win.pack_combo.currentData() == "ghost", (
        "a saved pack that is not installed must still be shown")
    assert "not installed" in win.pack_combo.currentText(), (
        win.pack_combo.currentText())
    assert win._design_picture("idle") == "", "a missing pack must not fall back"
    assert "no installed pack named ghost" in win.pack_label.text(), (
        win.pack_label.text())
    bubble.SETTINGS["design_pack"] = "ghost"
    note = bubble._appearance_note()
    assert "pack ghost" in note, note
    assert "no installed pack named ghost" in note, note
    bubble.SETTINGS["design_pack"] = ""
    assert "pack ghost" not in bubble._appearance_note()

    _shutil.rmtree(packs_root, ignore_errors=True)


@scenario
def the_appearance_panel_exports_the_art_as_a_pack():
    # Install's reverse: the art ON SCREEN -- the selected pack, or the pictures
    # chosen by hand -- is written into a new folder as a pack that can be handed
    # to someone else. What this pins is the wiring the user touches: WHICH art is
    # exported (the form's, because a picture chosen a moment ago applies through
    # a debounce and settings.json may still hold the previous one), that a
    # cancelled dialog writes nothing, and that a refusal is REPORTED rather than
    # leaving the panel looking as if the click did nothing.
    import json as _json
    from PySide6.QtGui import QColor, QImage

    folder = settings_file.parent
    destination = folder / "exported"
    destination.mkdir(parents=True, exist_ok=True)

    def art(name, rgba):
        p = folder / name
        img = QImage(48, 48, QImage.Format_ARGB32)
        img.fill(QColor(*rgba))
        assert img.save(str(p)), name
        return p

    fallback = art("fallback.png", (200, 200, 200, 255))
    idle = art("idle-shot.png", (30, 60, 200, 255))

    seed({"model": "testmodel:latest", "design_image_path": str(fallback)})
    win.reload_from_disk()
    assert win._design_image == str(fallback)

    real_name = settings_app.QInputDialog.getText
    real_dir = settings_app.QFileDialog.getExistingDirectory
    try:
        # --- a cancelled NAME writes nothing at all. Qt hands back the text that
        # --- was typed with ok=False, so the guard has to be `ok` — an empty
        # --- name alone would be caught by the slug check and hide the bug.
        settings_app.QInputDialog.getText = (
            lambda *a, **k: ("Typed But Cancelled", False))
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(destination))
        win._export_design_pack()
        assert list(destination.iterdir()) == [], "a cancelled export wrote files"

        # --- a cancelled DESTINATION writes nothing either
        settings_app.QInputDialog.getText = lambda *a, **k: ("Shared", True)
        settings_app.QFileDialog.getExistingDirectory = lambda *a, **k: ("", "")
        win._export_design_pack()
        assert list(destination.iterdir()) == []

        # --- the art is exported, and an UNSAVED form choice goes with it
        settings_app.QInputDialog.getText = lambda *a, **k: ("My Look", True)
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(destination))
        win._design_image = str(fallback)
        win._design_images["idle"] = str(idle)
        win._export_design_pack()
        exported = destination / "my-look"
        assert exported.is_dir(), sorted(p.name for p in destination.iterdir())
        body = _json.loads((exported / "pack.json").read_text(encoding="utf-8"))
        assert body["name"] == "My Look", body
        assert body["states"] == {"idle": "idle-shot.png"}, body
        assert body["any"] == "fallback.png", body
        assert "exported" in win.status_label.text(), win.status_label.text()
        assert win._design_pack == "", "an export is not an install"

        # --- exporting the same name again is REFUSED and says so: the folder
        # is the user's, and a one-click write must not eat what is in it.
        win._export_design_pack()
        assert "already exists" in win.status_label.text(), win.status_label.text()
        assert (exported / "pack.json").is_file(), "the first export must survive"
    finally:
        settings_app.QInputDialog.getText = real_name
        settings_app.QFileDialog.getExistingDirectory = real_dir


@scenario
def the_appearance_panel_moves_a_look_as_one_file():
    # A folder is not something anyone can attach to a message, so the same two
    # things the card already does -- export the art on screen, install a pack
    # you were given -- also exist as ONE file. What this pins is the wiring the
    # user touches: the new buttons ask the same questions as their folder
    # twins, a cancelled chooser installs nothing, an imported file really
    # becomes the selected pack, and a refused file is REPORTED rather than
    # leaving the panel looking as if the click did nothing.
    import json as _json
    import shutil as _shutil
    import zipfile as _zipfile
    from PySide6.QtGui import QColor, QImage

    folder = settings_file.parent
    packs_root = config_dir / "design-packs"
    appearance.PACKS_DIR = packs_root
    _shutil.rmtree(packs_root, ignore_errors=True)
    destination = folder / "sent"
    destination.mkdir(parents=True, exist_ok=True)

    def art(name, rgba):
        p = folder / name
        img = QImage(48, 48, QImage.Format_ARGB32)
        img.fill(QColor(*rgba))
        assert img.save(str(p)), name
        return p

    fallback = art("file-any.png", (200, 200, 200, 255))
    idle = art("file-idle.png", (30, 60, 200, 255))
    speaking = art("file-speaking.png", (200, 60, 30, 255))

    seed({"model": "testmodel:latest", "design_image_path": str(fallback)})
    win.reload_from_disk()
    win._design_images["idle"] = str(idle)
    win._design_images["speaking"] = str(speaking)

    real_name = settings_app.QInputDialog.getText
    real_dir = settings_app.QFileDialog.getExistingDirectory
    real_open = settings_app.QFileDialog.getOpenFileName
    try:
        # --- closing the file chooser installs nothing
        settings_app.QFileDialog.getOpenFileName = lambda *a, **k: ("", "")
        win._import_design_pack_file()
        assert not packs_root.is_dir() or not list(packs_root.iterdir()), (
            "a cancelled import must not install anything")

        # --- export as ONE file: the art on screen, in a shape you can send
        settings_app.QInputDialog.getText = lambda *a, **k: ("Shared Look", True)
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(destination))
        win._export_design_pack_file()
        written = destination / "shared-look.hpack"
        assert written.is_file(), sorted(p.name for p in destination.iterdir())
        assert _zipfile.is_zipfile(written), "a pack file has to be a plain zip"
        assert "exported" in win.status_label.text(), win.status_label.text()
        assert win._design_pack == "", "an export is not an install"
        with _zipfile.ZipFile(written) as zf:
            body = _json.loads(zf.read("pack.json").decode("utf-8"))
            assert body["states"] == {"idle": "file-idle.png",
                                      "speaking": "file-speaking.png"}, body
            assert body["any"] == "file-any.png", body
            assert zf.read(body["states"]["idle"]) == idle.read_bytes(), (
                "the form's unsaved choice has to be what was exported")

        # --- ...and importing that file back really selects the pack it holds
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(written), "Handsoff pack (*.hpack)"))
        win._import_design_pack_file()
        assert win._design_pack == "shared-look", win.status_label.text()
        assert "installed pack" in win.status_label.text(), (
            win.status_label.text())
        assert win._design_picture("idle") == str(
            packs_root / "shared-look" / "file-idle.png"), (
            win._design_picture("idle"))

        # --- a file that is not an archive is refused in the module's words and
        # --- leaves the pack that IS working selected
        junk = folder / "not-a-pack.hpack"
        junk.write_bytes(b"nope")
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(junk), "Handsoff pack (*.hpack)"))
        win._import_design_pack_file()
        assert "not a zip archive" in win.status_label.text(), (
            win.status_label.text())
        assert win._design_pack == "shared-look", (
            "a refused import must not change what is selected")
        junk.unlink(missing_ok=True)
    finally:
        settings_app.QInputDialog.getText = real_name
        settings_app.QFileDialog.getExistingDirectory = real_dir
        settings_app.QFileDialog.getOpenFileName = real_open
        _shutil.rmtree(packs_root, ignore_errors=True)


@scenario
def the_appearance_panel_previews_a_pack_before_installing_it():
    # Look before you leap: a pack folder or a .hpack can be drawn in the strip
    # WITHOUT being installed, and Try it is the step that actually takes it.
    # What this pins is the sequence a user gets -- the strip follows the
    # candidate rather than the saved selection (including for someone whose
    # bubble draws an orb, where a preview that did nothing would be the old
    # "nothing applies" complaint again), nothing is installed until Try it,
    # cancelling puts the strip back and removes the temporary folder a
    # previewed FILE was unpacked into, and a pack that cannot be read is
    # refused in the sentence an install would use. The candidate is also
    # PUSHED to the running bubble so the look can be judged on the real
    # desktop, with a heartbeat that is what keeps it there and a clear on every
    # way the preview ends (a window that closes without clearing is a bubble
    # that stops previewing by itself, which the bubble's own deadline covers).
    import json as _json
    import shutil as _shutil
    import zipfile as _zipfile
    from PySide6.QtGui import QColor, QImage

    from settings_schema import BUBBLE_STATES

    folder = settings_file.parent
    packs_root = config_dir / "design-packs"
    appearance.PACKS_DIR = packs_root
    _shutil.rmtree(packs_root, ignore_errors=True)
    candidate = folder / "candidate"
    candidate.mkdir(parents=True, exist_ok=True)

    def art(path, rgba):
        img = QImage(48, 48, QImage.Format_ARGB32)
        img.fill(QColor(*rgba))
        assert img.save(str(path)), path
        return path

    shots = {s: art(candidate / f"{s}.png", (20 + 50 * i, 90, 180, 255))
             for i, s in enumerate(BUBBLE_STATES)}
    (candidate / "pack.json").write_text(_json.dumps({
        "name": "Candidate",
        "states": {s: f"{s}.png" for s in BUBBLE_STATES}}), encoding="utf-8")

    seed({"model": "testmodel:latest", "design_image_path": ""})
    win.reload_from_disk()
    assert win.design_combo.currentData() != "image", (
        "the shape must NOT already be `image`, or previewing would prove "
        "nothing about a preview that has to show its own art")

    class _FrozenClock:
        def elapsed(self):
            return 4000

    seen = []
    real_draw = settings_app.BubblePreview._draw_image_glyph

    def _recording(self, painter, cx, cy, r, color, state=""):
        out = real_draw(self, painter, cx, cy, r, color, state)
        seen.append((state, out, str(self._image_fn(state) or "")))
        return out

    real_open = settings_app.QFileDialog.getOpenFileName
    real_dir = settings_app.QFileDialog.getExistingDirectory
    real_name = settings_app.QInputDialog.getText
    # The preview is PUSHED to the running bubble, so the look can be judged on
    # the real desktop rather than only in the strip. The command is recorded
    # and `run_bg` is made SYNCHRONOUS here, so what is asserted is the panel's
    # decisions (which folder, drawn or only-in-this-window, cleared when)
    # rather than a worker thread's timing.
    sent = []
    real_cmd = settings_app._socket_command
    real_run_bg = win.run_bg

    def record(sock, payload, timeout=1.5):
        sent.append(payload)
        return "previewing Whatever on the bubble \u2014 nothing installed"

    def run_bg_now(fn, done):
        try:
            ok, result = True, fn()
        except Exception as exc:        # noqa: BLE001
            ok, result = False, exc
        done(ok, result)

    settings_app.BubblePreview._draw_image_glyph = _recording
    settings_app._socket_command = record
    win.run_bg = run_bg_now
    try:
        win.preview.resize(320, 200)
        win.preview._clock = _FrozenClock()

        # --- preview a FOLDER: the strip draws the candidate, nothing installed
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(candidate))
        win._preview_design_pack("folder")
        assert "Previewing Candidate" in win.preview_label.text(), (
            win.preview_label.text())
        assert not win.preview_try.isHidden(), "Try it appears with a preview"
        assert not packs_root.is_dir() or not list(packs_root.iterdir()), (
            "previewing a folder must not install or copy anything")
        seen.clear()
        img = QImage(win.preview.width(), win.preview.height(),
                     QImage.Format_ARGB32)
        img.fill(0)
        win.preview.render(img)
        assert [s for s, _ok, _p in seen] == list(BUBBLE_STATES), seen
        assert all(ok for _s, ok, _p in seen), seen
        for state, _ok, path in seen:
            assert path == str(candidate / f"{state}.png"), (state, path)

        # --- ...and the BUBBLE draws it too: same folder, nothing installed,
        # --- and the label says where it is so the panel cannot imply more
        assert sent[-1] == f"preview-pack {candidate}", sent
        assert win._preview_timer.isActive(), "the heartbeat holds it up"
        assert "drawn on the bubble" in win.preview_label.text(), (
            win.preview_label.text())
        # the heartbeat is what keeps it up: another beat is another offer
        beats = len(sent)
        win._renew_live_pack_preview()
        assert sent[beats:] == [f"preview-pack {candidate}"], sent

        # --- and it must not CLAIM the desktop shows it when the bubble is not
        # --- running: "drawn on the bubble" and "only this window" are
        # --- different facts, and telling them apart is why it is pushed live
        settings_app._socket_command = lambda *a, **k: None
        win._preview_design_pack("folder")
        assert "only this window shows it" in win.preview_label.text(), (
            win.preview_label.text())
        settings_app._socket_command = record
        win._preview_design_pack("folder")
        assert "drawn on the bubble" in win.preview_label.text(), (
            win.preview_label.text())

        # --- Try it is what installs, and it is what you were shown
        win._try_design_pack()
        assert win._design_pack == "candidate", win.status_label.text()
        assert "installed pack" in win.status_label.text(), win.status_label.text()
        assert (packs_root / "candidate" / "pack.json").is_file()
        assert win.preview_try.isHidden(), "Try it goes away with the preview"
        assert win.preview_label.isHidden(), "the preview label goes with it"
        # Taking it must take the CANDIDATE off the desktop: what is installed
        # now draws itself, and a preview still up would be the panel and the
        # bubble disagreeing about which pack is on screen.
        assert sent[-1] == "preview-clear", sent
        assert not win._preview_timer.isActive(), (
            "the heartbeat has to stop with the preview")

        # --- a pack that cannot be read is refused, and there is nothing to try
        broken = folder / "broken"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "pack.json").write_text(_json.dumps(
            {"name": "Broken", "any": "missing.png"}), encoding="utf-8")
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(broken))
        win._preview_design_pack("folder")
        assert "missing.png" in win.preview_label.text(), win.preview_label.text()
        assert "missing.png" in win.status_label.text(), win.status_label.text()
        assert win.preview_try.isHidden(), (
            "a pack that cannot be read offers nothing to try")

        # --- a FILE preview unpacks to a folder, and cancelling removes it
        pack_file = folder / "sent.hpack"
        with _zipfile.ZipFile(pack_file, "w") as zf:
            zf.writestr("pack.json", _json.dumps({
                "name": "From A File",
                "states": {s: f"{s}.png" for s in BUBBLE_STATES}}))
            for state, path in shots.items():
                zf.writestr(f"{state}.png", Path(path).read_bytes())
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(pack_file), "Handsoff pack (*.hpack)"))
        win._preview_design_pack("file")
        assert "Previewing From A File" in win.preview_label.text(), (
            win.preview_label.text())
        scratch = str(win._preview_scratch or "")
        assert scratch and Path(scratch).is_dir(), scratch
        # The bubble is told to draw the UNPACKED folder: it cannot read a
        # .hpack, and the panel is what owns that temporary folder.
        assert sent[-1] == f"preview-pack {scratch}", sent
        assert f"preview-pack {pack_file}" not in sent, sent
        win._cancel_design_pack_preview()
        assert not Path(scratch).exists(), (
            "cancelling a file preview has to remove what it unpacked")
        assert sent[-1] == "preview-clear", sent
        assert win._preview_live_note == "", (
            "the clear is sent for a preview that is already gone, so its reply "
            "must not write a note that would be shown against the NEXT one")
        # ...and neither may a reply that arrives late: the send is a thread and
        # the drop is not, so this is the one that would say "drawn on the
        # bubble" about a candidate nothing has asked the bubble to draw yet.
        win._note_live_pack_preview(True, "previewing Stale on the bubble")
        assert win._preview_live_note == "", win._preview_live_note
        assert "cancelled" in win.status_label.text(), win.status_label.text()
        assert win.preview_try.isHidden(), "nothing left to decide"
        assert "candidate" in sorted(p.name for p in packs_root.iterdir()), (
            "cancelling a preview must not touch what was already installed")

        # --- Try it on a FILE preview installs the pack that file holds, and
        # --- cleans up what the preview had to unpack to draw it
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(pack_file), "Handsoff pack (*.hpack)"))
        win._preview_design_pack("file")
        scratch = str(win._preview_scratch or "")
        assert scratch and Path(scratch).is_dir(), scratch
        win._try_design_pack()
        assert win._design_pack == "from-a-file", win.status_label.text()
        assert (packs_root / "from-a-file" / "pack.json").is_file()
        assert not Path(scratch).exists(), (
            "Try it has to remove the folder the preview unpacked")
        assert not win.preview._image_cache, (
            "the strip must not keep decoded copies of a pack it no longer "
            "shows \u2014 that is what would let it draw the wrong art")

        # --- a preview is not a mode: any other pack action ends it, because
        # --- the panel must never show one pack while acting on another
        settings_app.QFileDialog.getExistingDirectory = (
            lambda *a, **k: str(candidate))
        win._preview_design_pack("folder")
        assert not win.preview_try.isHidden()
        win._install_design_pack()
        assert win._design_pack == "candidate", win.status_label.text()
        assert win.preview_try.isHidden(), (
            "installing another pack has to end the preview")
        win._preview_design_pack("folder")
        assert not win.preview_try.isHidden()
        settings_app.QInputDialog.getText = lambda *a, **k: ("Typed", False)
        win._export_design_pack()      # cancelled at the name prompt
        assert win.preview_try.isHidden(), (
            "an export writes the art on screen, so a preview has to end "
            "before it asks where to write")

        # --- and closing the window is the last chance to remove it, because
        # --- nothing else knows that folder exists
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(pack_file), "Handsoff pack (*.hpack)"))
        win._preview_design_pack("file")
        scratch = str(win._preview_scratch or "")
        assert scratch and Path(scratch).is_dir(), scratch
        win.close()
        assert not Path(scratch).exists(), (
            "closing the window has to remove the folder a preview unpacked")
        assert sent[-1] == "preview-clear", (
            "closing is also the last chance to take it off the desktop")
    finally:
        settings_app.BubblePreview._draw_image_glyph = real_draw
        settings_app.QFileDialog.getOpenFileName = real_open
        settings_app.QFileDialog.getExistingDirectory = real_dir
        settings_app.QInputDialog.getText = real_name
        settings_app._socket_command = real_cmd
        win.run_bg = real_run_bg
        _shutil.rmtree(packs_root, ignore_errors=True)


@scenario
def the_image_design_takes_one_picture_per_state():
    # One picture for idle/listening/thinking/speaking, chosen in the Shape card,
    # with `design_image_path` behind them as the fallback. What this pins is the
    # wiring: the four settings the panel writes are the four the bubble reads,
    # the preview shows the picture of the state it is drawing, a state with no
    # picture of its own really falls back, and the reason one of them cannot
    # draw names WHICH state — with four slots that is the entire question.
    import json as _json
    from PySide6.QtGui import QColor, QImage

    from settings_schema import DESIGN_IMAGE_KEYS

    folder = settings_file.parent
    states = [key[len("design_image_"):] for key in DESIGN_IMAGE_KEYS]
    assert states == ["idle", "listening", "thinking", "speaking"], states

    def art(name, rgba):
        p = folder / name
        img = QImage(64, 64, QImage.Format_ARGB32)
        img.fill(QColor(*rgba))
        assert img.save(str(p)), name
        return p

    files = {s: art(f"{s}.png", (30 + 40 * i, 60, 200, 255))
             for i, s in enumerate(states)}
    fallback = art("fallback.png", (200, 200, 200, 255))

    # --- the panel offers a slot per state, seeded from disk
    for state in states:
        assert state in win.state_image_buttons, win.state_image_buttons.keys()
    assert all(key in win.APPEARANCE_KEYS for key in DESIGN_IMAGE_KEYS), (
        "a picture chosen for ONE state moves no other control, so a state key "
        "missing from APPEARANCE_KEYS would be read as a load, not an edit")

    seed({"model": "testmodel:latest", "design_image_path": str(fallback)})
    win.reload_from_disk()
    assert win._design_image == str(fallback)
    assert not any(win._design_images.values()), win._design_images
    assert str(fallback) in win.state_image_label.text(), (
        win.state_image_label.text())

    # --- choosing a picture for ONE state: the form, the disk, the shape
    real_open = settings_app.QFileDialog.getOpenFileName
    try:
        settings_app.QFileDialog.getOpenFileName = lambda *a, **k: ("", "")
        win._pick_state_image("idle")
        assert not win._design_images.get("idle"), (
            "a cancelled chooser is not an edit")
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(files["idle"]), ""))
        win._pick_state_image("idle")
        # a SECOND state, so "the state the row belongs to" is proven rather
        # than assumed from one call
        settings_app.QFileDialog.getOpenFileName = (
            lambda *a, **k: (str(files["speaking"]), ""))
        win._pick_state_image("speaking")
    finally:
        settings_app.QFileDialog.getOpenFileName = real_open
    assert win._design_images["idle"] == str(files["idle"])
    assert win._design_images["speaking"] == str(files["speaking"])
    assert not win._design_images["listening"], (
        "picking one state must not fill the others")
    assert win.design_combo.currentData() == "image", (
        "choosing art must point the shape at the design that draws it")
    win._apply_appearance_live()
    on_disk = _json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["design_image_idle"] == str(files["idle"]), on_disk.get(
        "design_image_idle")
    assert on_disk["design_image_speaking"] == str(files["speaking"]), (
        on_disk.get("design_image_speaking"))
    assert "idle picture" in win.status_label.text(), win.status_label.text()
    assert "speaking picture" in win.status_label.text(), (
        win.status_label.text())

    # --- the bubble resolves it the same way: idle has its own, the rest fall
    # --- back, and the KEY the panel wrote is the key the renderer reads
    assert win._design_picture("idle") == str(files["idle"])
    assert win._design_picture("speaking") == str(files["speaking"])
    assert win._design_picture("thinking") == str(fallback)
    assert "(fallback)" in win.state_image_label.text(), (
        "a state inheriting the fallback must say so, or the readout looks "
        "like it has its own picture")
    bubble.SETTINGS["design_image_path"] = str(fallback)
    bubble.SETTINGS["design_image_idle"] = str(files["idle"])
    assert appearance.design_picture("idle") == str(files["idle"])
    assert appearance.design_picture("thinking") == str(fallback)
    assert appearance.art_problem() == ""

    # --- all four, and the slots really draw four DIFFERENT pictures: the
    # --- preview's whole job is answering "what will the bubble show".
    win._design_images.update({s: str(files[s]) for s in states})
    win._refresh_state_image_buttons()
    win._refresh_design_image_label()
    assert "unused while every state has its own picture" in (
        win.image_label.text()), win.image_label.text()
    # ...and with the states covered, the fallback only has to say WHAT it is:
    # set-but-unused and absent are two different sentences.
    swap = win._design_image
    win._design_image = ""
    win._refresh_design_image_label()
    assert "No fallback picture" in win.image_label.text(), (
        win.image_label.text())
    win._design_image = swap
    win._refresh_design_image_label()
    seen = []
    real_draw = settings_app.BubblePreview._draw_image_glyph

    def _recording(self, painter, cx, cy, r, color, state=""):
        out = real_draw(self, painter, cx, cy, r, color, state)
        seen.append((state, out))
        return out

    class _FrozenClock:
        def elapsed(self):
            return 4000

    settings_app.BubblePreview._draw_image_glyph = _recording
    try:
        win.preview.resize(320, 200)
        win.preview._clock = _FrozenClock()
        img = QImage(win.preview.width(), win.preview.height(),
                     QImage.Format_ARGB32)
        img.fill(0)
        win.preview.render(img)
    finally:
        settings_app.BubblePreview._draw_image_glyph = real_draw
    assert [s for s, ok in seen] == states, seen
    assert all(ok for _s, ok in seen), seen
    for state in states:
        assert win._design_picture(state) == str(files[state]), state

    # --- a state's picture that cannot be drawn is reported FOR THAT STATE,
    # --- in the panel and in doctor, with the bubble module's own sentence.
    ghost = folder / "ghost.png"
    win._design_images["listening"] = str(ghost)
    win._refresh_state_image_buttons()
    line = win.state_image_label.text()
    assert "listening: \u26a0 no file at" in line, line
    assert f"idle: {files['idle']}" in line, line
    bubble.SETTINGS["design_image_listening"] = str(ghost)
    note = bubble._appearance_note()
    assert "the listening picture" in note, note
    # The count is of pictures that will RENDER, out of the four states — two
    # keys are set here and one of them cannot be drawn, so 1/4 is the honest
    # number and it cannot contradict the sentence naming the broken one. It is
    # derived from the settings, not from how many the panel is showing.
    assert "picture per state: 1/4" in note, note
    assert "picture per state: 2/4" not in note, note
    win._design_images["listening"] = str(files["listening"])
    win._refresh_state_image_buttons()

    # --- clearing goes back to the fallback for every state, live and on disk
    win._clear_state_images()
    win._apply_appearance_live()
    on_disk = _json.loads(settings_file.read_text(encoding="utf-8"))
    for key in DESIGN_IMAGE_KEYS:
        assert on_disk[key] == "", (key, on_disk[key])
    assert not any(win._design_images.values())
    assert str(fallback) in win.state_image_label.text()
    assert "No fallback picture" not in win.image_label.text(), (
        "the fallback is still set, so the label must describe it")
    win._clear_design_image()
    win._apply_appearance_live()
    assert "No fallback picture" in win.image_label.text(), (
        win.image_label.text())
    for state in states:
        assert f"{state}: empty slot" in win.state_image_label.text(), (
            state, win.state_image_label.text())
    bubble.SETTINGS["design_image_idle"] = ""
    bubble.SETTINGS["design_image_path"] = ""


@scenario
def the_live_mic_probe_closes_a_stream_that_fails_to_start():
    # A stream that OPENED but failed to start was dropped without a close:
    # PortAudio holds the device until the object is collected, so a flapping
    # device leaked one stream per retry. Watch for the close on the failure
    # path, which is the branch nothing else in the suite reaches.
    probe = settings_app._LiveMicProbe()
    closed = []

    class Stream:
        def start(self):
            probe._running = False      # let _run exit after this iteration
            raise RuntimeError("device busy")

        def close(self):
            closed.append("closed")

    settings_app.H._open_input = lambda *a, **k: (Stream(), 16000)
    real_sleep = time.sleep
    settings_app.time.sleep = lambda *_a: None   # no back-off wait in a test
    probe._running = True
    worker = threading.Thread(target=probe._run, args=(None, 300), daemon=True)
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and not closed:
        app.processEvents()
        real_sleep(0.02)
    probe._running = False
    worker.join(5)
    assert closed == ["closed"], "a failed start leaked its PortAudio stream"
    assert "device busy" in probe.snapshot().get("error", ""), probe.snapshot()


@scenario
def refresh_models_lists_pins_and_survives_a_dead_server():
    # refresh_models was only ever driven through the selftest's happy path:
    # the capability badges, the current-model pinning and the dead-server
    # row were the parts nothing executed.
    calls = []

    def fake_http(url, payload=None, timeout=10):
        calls.append(url)
        if url.endswith("/api/tags"):
            return {"models": [{"name": "gemma4:latest"},
                                {"model": "qwen3:8b"}]}
        # /api/show per model: the first answers, the second is broken —
        # one dead capability endpoint must not lose the model from the list.
        # The probe posts the model NAME in the body (the URL is the same for
        # every model), so this matches on the payload — matching on the URL
        # meant the "broken" probe never broke and the model came back badged.
        if "qwen3" in str((payload or {}).get("model", "")):
            raise urllib.error.URLError("show exploded")
        return {"capabilities": ["tools"]}

    settings_app.http_json = fake_http
    win.host_edit.setText("127.0.0.1:11434")     # no scheme: prefix required
    win.cfg["model"] = "gemma4:latest"
    win.refresh_models()
    deadline = time.time() + 5.0
    while time.time() < deadline and win.model_list.count() < 2:
        app.processEvents()
        time.sleep(0.02)
    names = [win.model_list.item(i).text()
             for i in range(win.model_list.count())]
    assert any(n.startswith("gemma4:latest") and "tools" in n for n in names), names
    assert any(n.startswith("qwen3:8b") and "\u00b7" not in n for n in names), \
        "a model whose capability probe failed must still be listed, badgeless"
    assert calls[0] == "http://127.0.0.1:11434/api/tags", calls
    it = win.model_list.currentItem()
    assert it is not None and it.data(Qt.UserRole) == "gemma4:latest", \
        "the configured model must be the selected one"

    # and an unreachable server is a readable row, not an empty list
    def dead_http(url, payload=None, timeout=10):
        raise urllib.error.URLError(f"refused ({url})")

    settings_app.http_json = dead_http
    win.refresh_models()
    deadline = time.time() + 5.0
    while time.time() < deadline and "cannot reach" not in \
            (win.model_list.item(0).text() if win.model_list.count() else ""):
        app.processEvents()
        time.sleep(0.02)
    assert win.model_list.count() == 1
    # Asked as a FLAG TEST, not a number comparison. PySide6's flag enums do not
    # compare equal to ints, so `(flags() & ItemIsEnabled) == 0` is False even
    # when no flag is set — the truth test below is the one that answers the
    # question ("is this row selectable?") on a disabled row AND an enabled one.
    assert not (win.model_list.item(0).flags() & Qt.ItemFlag.ItemIsEnabled), \
        "the unreachable-server row must not be selectable"
    settings_app.http_json = _no_http


@scenario
def tts_reference_pick_clear_and_report():
    # _pick_reference/_clear_reference/refresh_tts: the reference-clip row's
    # whole surface, none of it previously executed.
    from PySide6.QtWidgets import QFileDialog

    seed({"model": "testmodel:latest",
          "tts_reference": str(clip_dir / "optimus.wav")})
    win.reload_from_disk()
    picked = []

    def fake_pick(*_a, **_k):
        picked.append(1)
        return str(clip_dir / "optimus.wav"), ""

    real_pick = QFileDialog.getOpenFileName
    QFileDialog.getOpenFileName = staticmethod(fake_pick)
    try:
        win._pick_reference()
    finally:
        QFileDialog.getOpenFileName = real_pick
    assert picked and win.tts_ref_edit.text().endswith("optimus.wav")
    assert win.tts_ref_status.text(), "the clip report must render"
    win._clear_reference()
    assert win.tts_ref_edit.text() == ""


@scenario
def the_live_probe_gate_captures_and_transcribes_a_utterance():
    # _on_frames is the probe's audio path: gate the frames, accumulate the
    # utterance, hand it to whisper in a worker. Every branch below was
    # uncovered — including the two guards (a superseded worker's answer is
    # discarded; a failing transcribe is reported, not swallowed).
    import threading as _th
    import numpy as _np

    probe = settings_app._LiveMicProbe()
    probe._running = True
    probe._stream = object()
    probe._gate = bubble._SpeechGate(300)
    probe._capture_rate = 16000
    probe._device = "test"
    probe._threshold = 300

    quiet = _np.zeros((1024, 1), dtype=_np.int16)
    loud = _np.full((1024, 1), 9000, dtype=_np.int16)

    # The whisper fake is installed BEFORE the frames, not after: the hangover
    # below ends an utterance and hands it straight to a worker thread, so a
    # fake installed afterwards races the real whisper — which is what made this
    # scenario assert against a transcript the real model had produced.
    results = {"n": 0}

    def fake_transcribe(audio):
        results["n"] += 1
        return f"heard {results['n']}"

    settings_app.H.transcribe = fake_transcribe

    for _ in range(3):                      # room tone: floor tracks, no event
        probe._on_frames(quiet, object(), 234)
    assert probe._last_event == ""
    for _ in range(4):                      # speech starts, frames collect
        probe._on_frames(loud, object(), 234)
    # The frame that OPENS speech is spent on the transition (the gate answers
    # 'start' and the buffer resets), so four loud frames collect three: two
    # trip the gate and the rest are recorded with them.
    assert probe._gate.in_speech and len(probe._frames) >= 3, \
        (probe._gate.in_speech, len(probe._frames))
    for _ in range(16):                     # hangover elapses → captured
        probe._on_frames(quiet, object(), 234)
    assert "speech captured" in probe._last_event, probe._last_event

    # the whisper worker: success, then failure, then supersession
    deadline = time.time() + 5.0
    while time.time() < deadline and probe._last_transcript != "heard 1":
        time.sleep(0.02)
    assert probe._last_transcript == "heard 1"
    assert probe._last_event == "transcribed"

    def boom(audio):
        raise RuntimeError("whisper weights vanished")

    settings_app.H.transcribe = boom
    probe._start_transcribe(quiet.reshape(-1))
    deadline = time.time() + 5.0
    while time.time() < deadline and "transcribe failed" not in probe._last_event:
        time.sleep(0.02)
    assert "whisper weights vanished" in probe._last_event

    # a superseded worker's late answer must NOT overwrite the newer one
    gate_ev = _th.Event()
    settings_app.H.transcribe = lambda a: (gate_ev.wait(5.0), "slow answer")[1]
    probe._start_transcribe(quiet.reshape(-1))
    old_thread = probe._transcribe_thread
    settings_app.H.transcribe = lambda a: "fast answer"
    probe._start_transcribe(quiet.reshape(-1))
    deadline = time.time() + 5.0
    while time.time() < deadline and probe._last_transcript != "fast answer":
        time.sleep(0.02)
    gate_ev.set()
    old_thread.join(5)
    assert probe._last_transcript == "fast answer", \
        "the stale worker's result must be discarded"
    assert probe.snapshot()["transcript"] == "fast answer"


@scenario
def the_mic_live_tick_renders_every_probe_state():
    # _toggle_mic_live / _mic_live_restart_if_on / _mic_live_tick: the labels
    # are the feature — opening, device, gate-open, error, event age-out,
    # transcript age-out and the off reset all had no test.
    assert win.mic_live_state.text() == "idle"
    started = []
    stopped = []

    class _FakeProbe:
        def __init__(self):
            self.snap = {}

        def start(self, device, threshold):
            started.append((device, threshold))

        def stop(self):
            stopped.append(1)

        def snapshot(self):
            return self.snap

    fake = _FakeProbe()
    win._live_probe = fake
    win.mic_live_btn.setChecked(True)
    assert started and started[0] == (win.mic_combo.currentData(),
                                      win.thresh_spin.value())

    def render(snap):
        fake.snap = snap
        win._mic_live_tick()

    render({"peak": 0.0, "error": "", "rate": 0, "device": "x",
            "gate_open": False, "last_event": "", "last_event_age": None,
            "frames": 0, "transcript": "", "transcript_age": None})
    assert win.mic_live_state.text() == "opening \u2026"
    render({"peak": 12.0, "error": "", "rate": 16000, "device": "USB Mic",
            "gate_open": False, "last_event": "", "last_event_age": None,
            "frames": 9, "transcript": "", "transcript_age": None})
    assert "USB Mic @ 16000 Hz" in win.mic_live_state.text()
    assert "hearing speech" not in win.mic_live_state.text()
    render({"peak": 40.0, "error": "", "rate": 16000, "device": "USB Mic",
            "gate_open": True, "last_event": "speech captured",
            "last_event_age": 5.0, "frames": 42, "transcript": "hello there",
            "transcript_age": 3.0})
    assert "hearing speech" in win.mic_live_state.text()
    assert "speech captured \u00b7 5s ago \u00b7 42 frames" in win.mic_live_event.text()
    assert "hello there" in win.mic_live_transcript.text()
    render({"peak": 0.0, "error": "device busy", "rate": 16000, "device": "x",
            "gate_open": False, "last_event": "", "last_event_age": None,
            "frames": 0, "transcript": "", "transcript_age": None})
    assert "error \u2014 device busy" in win.mic_live_state.text()
    render({"peak": 0.0, "error": "", "rate": 16000, "device": "x",
            "gate_open": False, "last_event": "speech started",
            "last_event_age": 120.0, "frames": 7, "transcript": "old",
            "transcript_age": 700.0})
    assert win.mic_live_event.text() == "", "a >90 s event must age out"
    assert win.mic_live_transcript.text() == "\u2014", "a >600 s transcript ages out"

    # a device/threshold change while live restarts the capture
    win._mic_live_restart_if_on()
    assert len(started) == 2
    win.mic_live_btn.setChecked(False)
    assert stopped, "the off branch must stop the probe"
    assert win.mic_live_state.text() == "idle"
    assert win.mic_live_event.text() == ""
    assert win.mic_live_transcript.text() == ""
    win._live_probe = None


@scenario
def mic_test_reports_peak_and_verdict():
    # test_mic's worker + poll: the bar and the good/too-quiet verdict, over
    # a stubbed InputStream (no device) and a fast-forwarded clock (no 3 s).
    import numpy as _np

    real_time = settings_app.time.time
    settings_app.time.time = lambda: real_time() + 3600.0   # past any deadline
    captured = {}

    class _FakeStream:
        def __init__(self, **kw):
            captured["cb"] = kw["callback"]

        def __enter__(self):
            captured["cb"](_np.full((1024, 1), 9000, dtype=_np.int16),
                           1024, None, None)
            return self

        def __exit__(self, *exc):
            return False

    real_stream = settings_app.sd.InputStream
    settings_app.sd.InputStream = _FakeStream
    try:
        win.mic_test_btn.setEnabled(True)
        win.test_mic()
        deadline = time.time() + 5.0
        while time.time() < deadline and "mic peak" not in win.status_label.text():
            app.processEvents()
            time.sleep(0.02)
        assert win.mic_test_btn.isEnabled()
        text = win.status_label.text()
        assert "mic peak" in text and "good signal" in text, text

        captured2 = {}

        class _SilentStream(_FakeStream):
            def __init__(self, **kw):
                captured2["cb"] = kw["callback"]

            def __enter__(self):
                captured2["cb"](_np.zeros((1024, 1), dtype=_np.int16),
                                1024, None, None)
                return self

        settings_app.sd.InputStream = _SilentStream
        win.test_mic()
        deadline = time.time() + 5.0
        while time.time() < deadline and "too quiet" not in win.status_label.text():
            app.processEvents()
            time.sleep(0.02)
        assert "too quiet" in win.status_label.text()
    finally:
        settings_app.sd.InputStream = real_stream
        settings_app.time.time = real_time


@scenario
def voice_test_asks_the_running_bubble():
    # test_voice must speak the control socket's `say` protocol — with the
    # bubble's dead/busy cases reported in the status line, not raised.
    seen = []

    # The status STREAM is recorded, not the label. The label is one mutable
    # slot: `run_bg` delivers `done` through a 120 ms QTimer poll, so a late
    # `done` from the previous call can overwrite the sentence this phase is
    # waiting for — which made this scenario the flakiest in the file, asserting
    # the final widget state instead of what was actually said.
    said: list[str] = []
    real_status = win._status
    win._status = lambda text: (said.append(text), real_status(text))[1]

    def fake_sock(path, command, timeout=1.5):
        seen.append(command)
        return "ok queued"

    settings_app._socket_command = fake_sock
    win.test_voice()
    deadline = time.time() + 5.0
    while time.time() < deadline and not any("ok queued" in s for s in said):
        app.processEvents()
        time.sleep(0.02)
    assert seen == [f"say {_VOICE_TEST_LINE}"], seen
    assert any("ok queued" in s for s in said), said
    assert win.voice_test_btn.isEnabled()

    settings_app._socket_command = lambda *a, **k: None
    win.test_voice()
    deadline = time.time() + 5.0
    while time.time() < deadline and not any("not running" in s for s in said):
        app.processEvents()
        time.sleep(0.02)
    assert any("voice test failed" in s and "not running" in s for s in said), said

    settings_app._socket_command = lambda *a, **k: "ERR overloaded"
    win.test_voice()
    deadline = time.time() + 5.0
    while time.time() < deadline and not any("overloaded" in s for s in said):
        app.processEvents()
        time.sleep(0.02)
    assert "voice test failed: ERR overloaded" in said, said
    settings_app._socket_command = lambda *a, **k: None


@scenario
def model_switch_backup_cycle_is_bounded_and_clears():
    # _backup_keep_n's prune (the cap was only ever exercised with < keep
    # files) and the model-switch wipe that backs up before truncating.
    history_file.write_text(json.dumps(
        [{"role": "user", "content": "remember me"}]), encoding="utf-8")
    note = win._clear_history_for_model_switch()
    assert "Memory cleared" in note and "backup saved" in note
    assert history_file.read_text(encoding="utf-8") == "[]"
    baks = sorted(history_file.parent.glob("history.json.bak-modelswitch.*"))
    assert len(baks) == 1 and baks[0].stat().st_mode & 0o777 == 0o600
    assert "remember me" in baks[0].read_text(encoding="utf-8")

    # six more backups: only `keep` (5) survive, oldest pruned
    for i in range(6):
        history_file.write_text(f"gen {i}", encoding="utf-8")
        settings_app._backup_keep_n(history_file, "bak-modelswitch")
        baks = sorted(history_file.parent.glob("history.json.bak-modelswitch.*"),
                      key=lambda p: p.stat().st_mtime_ns)
        assert len(baks) <= 5, len(baks)
    baks = sorted(history_file.parent.glob("history.json.bak-modelswitch.*"),
                  key=lambda p: p.stat().st_mtime_ns)
    assert len(baks) == 5, len(baks)
    # the newest backup holds the LAST generation, not the first
    assert "gen 5" in baks[-1].read_text(encoding="utf-8")


@scenario
def autostart_set_unset_migrate_and_double_start_guard():
    # apply_autostart/set_autostart: create, idempotence, systemd ownership,
    # old-line migration and removal — the module-level half of the toggle.
    import os as _os

    cfg = home / ".config" / "niri" / "config.kdl"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("// niri config\\n", encoding="utf-8")
    real_niri = settings_app.NIRI_CONFIG
    real_owns = settings_app.systemd_owns_autostart
    settings_app.NIRI_CONFIG = cfg
    settings_app._reload_niri = lambda: None
    try:
        assert settings_app.set_autostart(True) == "autostart line added to niri config"
        text = cfg.read_text(encoding="utf-8")
        assert settings_app.AUTOSTART_LINE in text
        assert settings_app.set_autostart(True) == "autostart unchanged"

        settings_app.systemd_owns_autostart = lambda: True
        msg = settings_app.apply_autostart(True)
        assert "NOT added" in msg and "systemd" in msg, msg
        settings_app.systemd_owns_autostart = real_owns

        # the OLD form must migrate to the sh -c line, not duplicate
        cfg.write_text(
            f'// niri config\\n{settings_app.AUTOSTART_LINE_OLD}\\n', encoding="utf-8")
        msg = settings_app.set_autostart(True)
        assert "migrated" in msg, msg
        text = cfg.read_text(encoding="utf-8")
        assert settings_app.AUTOSTART_LINE in text
        assert settings_app.AUTOSTART_LINE_OLD not in text

        assert settings_app.set_autostart(False) == "autostart line removed from niri config"
        assert settings_app.AUTOSTART_LINE not in cfg.read_text(encoding="utf-8")
        assert (cfg.parent / "config.kdl.bak-handsoff").exists()

        # autostart_enabled reads the same line back
        cfg.write_text(f"{settings_app.AUTOSTART_LINE}\\n", encoding="utf-8")
        assert settings_app.autostart_enabled() is True
        cfg.write_text("// niri config\\n", encoding="utf-8")
        assert settings_app.autostart_enabled() is False
    finally:
        settings_app.NIRI_CONFIG = real_niri
        settings_app.systemd_owns_autostart = real_owns


@scenario
def the_panel_installs_a_previewed_pack():
    # _try_design_pack: the SAME source a preview showed goes to the SAME
    # installer Install pack\u2026 uses, copied; failures and refusals are
    # status lines, never crashes.
    # The app reaches the pack installer through core.bubble (its own
    # `_core_module("bubble")`), NOT through the monolith's name space — which
    # no longer re-exports those functions. Patching the monolith left the real
    # installer to run against a file that does not exist, so this scenario
    # could not pass once it actually ran.
    packs = settings_app._core_module("bubble")
    real_install_file = packs.install_pack_file
    real_install = packs.install_pack
    scratch = home / "pack-scratch"
    scratch.mkdir(exist_ok=True)
    seen = []

    def fake_inspect(path):
        return ({"name": "Demo Pack"}, None, str(scratch))

    def fake_install_file(src):
        seen.append(("file", src))
        return "demo", "pack demo installed"

    def fake_install(src):
        seen.append(("folder", src))
        return "demo", "pack demo installed"

    packs.inspect_pack = fake_inspect
    packs.install_pack_file = fake_install_file
    packs.install_pack = fake_install
    try:
        win._try_design_pack()
        assert win.status_label.text() == "", "no preview \u2192 silent no-op"

        win._preview_art = {"name": "Demo Pack"}
        win._preview_source = str(home / "demo.hpack")
        win._preview_kind = "file"
        win._preview_scratch = str(scratch)
        win._try_design_pack()
        assert seen == [("file", str(home / "demo.hpack"))], seen
        assert "pack demo installed" in win.status_label.text()
        assert win._preview_source == "" and win._preview_art is None, \
            "a successful install forgets the preview"

        def broken_install(src):
            raise RuntimeError("disk full")

        packs.install_pack_file = broken_install
        win._preview_art = {"name": "Demo Pack"}
        win._preview_source = str(home / "demo.hpack")
        win._preview_kind = "file"
        win._preview_scratch = ""
        win._try_design_pack()
        assert "could not install that pack (disk full)" in win.status_label.text()
        assert win._preview_art == {"name": "Demo Pack"}, \
            "a failed install keeps the preview for another Try"
    finally:
        packs.install_pack_file = real_install_file
        packs.install_pack = real_install
        win._drop_pack_preview()


@scenario
def http_json_parses_a_plain_reply():
    # the settings app's only HTTP helper: a real loopback round-trip, through
    # the reference the driver kept before the module attribute was stubbed.
    import threading as _th
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"models": [1, 2]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    _th.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        doc = real_http_json(
            f"http://127.0.0.1:{srv.server_port}/api/tags", timeout=5)
        assert doc == {"models": [1, 2]}
    finally:
        srv.shutdown()

SCENARIO_NAME = sys.argv[1]
if SCENARIO_NAME not in SCENARIOS:
    print(f"unknown scenario {SCENARIO_NAME!r}: {len(SCENARIOS)} registered",
          file=sys.stderr)
    sys.exit(2)
try:
    SCENARIOS[SCENARIO_NAME]()
except SystemExit as e:   # pytest.exit / aborts re-raised cleanly
    sys.exit(int(e.code or 0))
except BaseException:
    import traceback
    traceback.print_exc()
    sys.exit(1)

# The run is ANNOUNCED, at module level, and the parent asserts this line.
# This dispatch once sat INDENTED INSIDE the last scenario, so no scenario ever
# executed: every child built the window, exited 0, and all 65 tests passed
# having tested nothing — which is also where the GUI module's coverage went.
# A token plus the registry size makes that shape unrepresentable: an unreachable
# dispatch prints nothing, and a name with no matching @scenario prints a
# registry count the parent does not expect.
print(f"SCENARIO-RAN {SCENARIO_NAME} registry={len(SCENARIOS)}", flush=True)
sys.exit(0)
"""


def _run_scenario(name: str, tmp_path: Path) -> subprocess.CompletedProcess:
    # run_driver, not dict(os.environ): the driver LOADS two monoliths, and the
    # HOME they resolve their CONFIG_DIR/STATE_DIR from must be the tmp one from
    # the first line of the child, not the developer's (the driver also rebinds
    # the bubble's constants afterwards — belt and braces, but it was the only
    # belt). The runner keeps the real user site-packages on PYTHONPATH, because
    # a redirected HOME hides PySide6.
    #
    # The driver goes on STDIN rather than into a `-c` argument. `-c` is capped
    # by MAX_ARG_STRLEN (128 KiB on Linux) and GUI_DRIVER is the source of every
    # scenario in this file, so the day that text grew past the cap the whole
    # suite broke with `[Errno 7] Argument list too long` — a failure with
    # nothing to do with what the tests test, and one that gets likelier every
    # time a scenario is added. Python treats `-` exactly as it treats `-c` for
    # `sys.path[0]` (the cwd), which is what the driver's imports rely on.
    return run_driver(
        ["-", name], home=tmp_path,
        env_extra={"SGUI_HOME": str(tmp_path), "SGUI_HERE": str(HERE)},
        capture_output=True, text=True, timeout=120, input=GUI_DRIVER,
    )


SCENARIO_NAMES = [
    "save_roundtrip",
    "save_refuses_without_model",
    "design_change_applies_on_sparse_settings",
    "disk_change_does_not_reset_unsaved_edits",
    "appearance_energy_and_accent_roundtrip",
    "appearance_changes_apply_without_save",
    "colour_change_applies_without_save",
    "malformed_state_colours_rejected_at_entry",
    "size_slider_applies_without_save",
    "loading_the_form_is_still_not_an_edit",
    "wallpaper_tuning_buttons_retune_palette",
    "bubble_designs_render_at_energy_extremes",
    "sauron_eye_reacts_to_voice_level",
    "every_design_reacts_to_voice_level",
    "every_design_shows_the_state_colour",
    "resizing_the_bubble_keeps_its_aperture",
    "a_look_sets_every_appearance_control",
    "every_look_is_renderable_and_reacts",
    "the_cat_keeps_its_ears_inside_its_own_mask",
    "every_design_keeps_ink_inside_its_aperture",
    "every_design_has_its_own_preview_glyph",
    "the_image_design_draws_the_users_picture",
    "the_image_design_can_use_an_installed_pack",
    "the_appearance_panel_exports_the_art_as_a_pack",
    "the_appearance_panel_moves_a_look_as_one_file",
    "the_appearance_panel_previews_a_pack_before_installing_it",
    "the_image_design_takes_one_picture_per_state",
    "appearance_panel_is_a_scrolling_column_of_cards",
    "the_appearance_panel_persists_the_avatar_decoration",
    "the_strip_shows_the_decoration_and_its_colour",
    "the_appearance_panel_offers_every_decoration_in_the_schema",
    "look_tiles_are_drawn_from_the_shared_painter",
    "voice_tab_level_meter_reads_the_bubble_feed",
    "the_live_mic_probe_closes_a_stream_that_fails_to_start",
    "external_change_reloads_and_reports",
    "missing_settings_file_mtime_is_zero",
    "conversation_pane_renders_roles_and_tool_calls",
    "clear_history_keeps_backup",
    "modelswitch_clears_history",
    "facts_pane_lists_forgets_and_dedups",
    "decision_log_renders_and_tolerates_garbage",
    "history_tab_renders_and_truncates",
    "keybinds_snippet_written_to_tmp_config",
    "restart_and_log_guards",
    "apply_autostart_disabled_is_noop",
    "autostart_enable_migrates_old_line",
    "history_view_renders_decisions",
    "health_line_reports_not_running",
    "fmt_health_ok_and_degraded",
    "health_query_bad_inputs",
    "policy_rows_roundtrip",
    "remote_ollama_checkbox_roundtrip",
    "model_picker_and_tts_reference",
    "tabs_and_colors",
    "clear_history_via_stubbed_dialog",
    "refresh_models_lists_pins_and_survives_a_dead_server",
    "tts_reference_pick_clear_and_report",
    "the_live_probe_gate_captures_and_transcribes_a_utterance",
    "the_mic_live_tick_renders_every_probe_state",
    "mic_test_reports_peak_and_verdict",
    "voice_test_asks_the_running_bubble",
    "model_switch_backup_cycle_is_bounded_and_clears",
    "autostart_set_unset_migrate_and_double_start_guard",
    "the_panel_installs_a_previewed_pack",
    "http_json_parses_a_plain_reply",
]


@pytest.mark.parametrize("name", SCENARIO_NAMES)
class TestSettingsGui:
    """Every scenario constructs the real window offscreen in a fresh child."""

    def test_scenario(self, name, tmp_path):
        r = _run_scenario(name, tmp_path)
        assert r.returncode == 0, (
            f"scenario {name} failed:\n{r.stderr[-3000:]}")
        # Exit 0 is not proof that anything ran: the child must SAY it ran the
        # scenario, and say how many the driver registered. Without this, a
        # dispatch that is unreachable (indented into a scenario, or dropped)
        # makes every child exit 0 and this whole file pass vacuously — which is
        # what happened, and what silently cost the GUI module its coverage.
        assert f"SCENARIO-RAN {name} registry={len(SCENARIO_NAMES)}" in r.stdout, (
            f"scenario {name} never ran — the child exited 0 without saying so. "
            f"stdout={r.stdout[-400:]!r} stderr={r.stderr[-800:]!r}")
