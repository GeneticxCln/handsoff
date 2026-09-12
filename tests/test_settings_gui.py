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

from conftest import HERE

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
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HERE = os.environ["SGUI_HERE"]
settings_app = load("handsoff_settings_gui", os.path.join(HERE, "handsoff-settings.py"))
bubble = load("handsoff_core_gui", os.path.join(HERE, "handsoff.py"))

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

settings_app.HOME = home
settings_app.NIRI_CONFIG = NIRI

# --- stub every side door: network, systemd, niri reload, xdg-open


def _no_http(url, payload=None, timeout=10):
    raise urllib.error.URLError(f"stubbed in GUI tests ({url})")


class _Result:
    returncode = 1
    stdout = ""
    stderr = ""


settings_app.http_json = _no_http
settings_app.systemd_owns_autostart = lambda: False
settings_app._reload_niri = lambda: None
settings_app.subprocess.run = lambda *a, **k: _Result()
settings_app.subprocess.Popen = lambda *a, **k: None

# --- seed disk state the scenarios read


def seed(settings=None, history=None):
    settings_file.write_text(json.dumps(settings or {}), encoding="utf-8")
    if history is None:
        history_file.unlink(missing_ok=True)
    else:
        history_file.write_text(json.dumps(history), encoding="utf-8")


from PySide6.QtWidgets import QApplication

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


@scenario
def wallpaper_tuning_buttons_retune_palette():
    # offline: detection fails cleanly and must not touch the palette, while the
    # explicit Dark/Light buttons still retune it without a wallpaper or magick
    import types
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    before = dict(win._colors)
    win._match_wallpaper()
    assert "could not detect the wallpaper" in win.status_label.text()
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
        detect_wallpaper_luminance=lambda *_a, **_k: 0.02)
    try:
        win._colors = dict(before)
        win._match_wallpaper()
        assert win._colors != before
        assert "dark backdrop" in win.status_label.text()
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

    widget = bubble.BubbleWidget(_Stub())
    widget.resize(bubble.WINDOW_PX, bubble.WINDOW_PX)
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
            base = bubble.STATE_COLORS[state]
            widget._color_ui = [base.redF(), base.greenF(), base.blueF()]
            for energy in (0.2, 1.0, 2.0):
                bubble.ANIM_ENERGY = energy
                for accent in (0.0, 1.0):
                    bubble.BUBBLE_ACCENT = accent
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
        bubble.ANIM_ENERGY = 1.0
        bubble.BUBBLE_ACCENT = 0.5
        setattr(bubble, key, lo)
        before = _pixels()
        setattr(bubble, key, hi)
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

    # the neutral defaults must reproduce the historical framing exactly.
    # _energy_ui is a smoothed chase (it converges during _on_tick), so pin it
    # to the target rather than hoping the event loop got there: the frame's
    # job is to report the converged value, which is what is asserted here.
    bubble.ANIM_ENERGY = 1.0
    bubble.BUBBLE_ACCENT = 0.5
    widget._state = "idle"
    widget._energy_ui = bubble._fx_energy("idle")
    frame = widget._frame()
    assert frame["anim"] == 1.0
    assert frame["accent"] == 0.5
    assert abs(frame["energy"] - bubble._fx_energy("idle")) < 1e-9

    # ...and the slider really moves the target every design reads: monotonic
    # in animation energy, clamped to a sane glow range. (Deliberately stated
    # as a property rather than exact numbers, so a retuned curve is fine.)
    energies = []
    for knob in (0.2, 1.0, 2.0):
        bubble.ANIM_ENERGY = knob
        energies.append(bubble._fx_energy("idle"))
    assert energies == sorted(energies), "more energy must never dim the glow"
    assert all(0.0 <= e <= 1.0 for e in energies), "glow energy must stay bounded"
    bubble.ANIM_ENERGY = 1.0


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

    widget = bubble.BubbleWidget(_Stub())
    widget.resize(bubble.WINDOW_PX, bubble.WINDOW_PX)
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
    bubble.ANIM_ENERGY = 1.0
    bubble.BUBBLE_ACCENT = 0.5

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

    widget = bubble.BubbleWidget(_Stub())
    widget.resize(bubble.WINDOW_PX, bubble.WINDOW_PX)
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
    widget._radius_ui = bubble.BUBBLE_R0
    # "listening" is when the mic is live, and it is the state the equalizer
    # meters in; idle bars deliberately breathe on their own instead
    widget._state = "listening"
    widget._energy_ui = 0.5
    bubble.ANIM_ENERGY = 1.0
    bubble.BUBBLE_ACCENT = 0.5
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


if __name__ == "__main__":
    name = sys.argv[1]
    try:
        SCENARIOS[name]()
    except SystemExit as e:   # pytest.exit / aborts re-raised cleanly
        sys.exit(int(e.code or 0))
    except BaseException as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
"""


def _run_scenario(name: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "SGUI_HOME": str(tmp_path),
        "SGUI_HERE": str(HERE),
    })
    return subprocess.run(
        [sys.executable, "-c", GUI_DRIVER, name],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(HERE),
    )


SCENARIO_NAMES = [
    "save_roundtrip",
    "save_refuses_without_model",
    "design_change_applies_on_sparse_settings",
    "disk_change_does_not_reset_unsaved_edits",
    "appearance_energy_and_accent_roundtrip",
    "appearance_changes_apply_without_save",
    "colour_change_applies_without_save",
    "size_slider_applies_without_save",
    "loading_the_form_is_still_not_an_edit",
    "wallpaper_tuning_buttons_retune_palette",
    "bubble_designs_render_at_energy_extremes",
    "sauron_eye_reacts_to_voice_level",
    "every_design_reacts_to_voice_level",
    "voice_tab_level_meter_reads_the_bubble_feed",
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
]


@pytest.mark.parametrize("name", SCENARIO_NAMES)
class TestSettingsGui:
    """Every scenario constructs the real window offscreen in a fresh child."""

    def test_scenario(self, name, tmp_path):
        r = _run_scenario(name, tmp_path)
        assert r.returncode == 0, (
            f"scenario {name} failed:\n{r.stderr[-3000:]}")
