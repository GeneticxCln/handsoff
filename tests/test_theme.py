"""Bubble palette tuning (core/theme.py): wallpaper detection + backdrop tuning.

The Appearance tab's "match wallpaper" path is three separable pieces — colour
maths, config parsing and an ImageMagick sample — so each is pinned here
without a display, a wallpaper or ImageMagick installed.
"""
from __future__ import annotations

import pytest

from core import theme
from core.settings import coerce_settings
from settings_schema import DEFAULT_SETTINGS


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _runner(returncode=0, stdout=""):
    """A subprocess.run stand-in that records the commands it was handed."""
    calls: list = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return _Result(returncode, stdout)

    run.calls = calls
    return run


class TestThemeMath:
    def test_hex_rgb_round_trip(self):
        assert theme.hex_to_rgb("#4f8cff") == (79, 140, 255)
        assert theme.hex_to_rgb("4f8cff") == (79, 140, 255)
        assert theme.hex_to_rgb("nope") is None
        assert theme.hex_to_rgb("") is None
        assert theme.rgb_to_hex((79, 140, 255)) == "#4f8cff"
        assert theme.rgb_to_hex((-5, 300, 7)) == "#00ff07"

    def test_relative_luminance_bounds_and_ordering(self):
        assert theme.relative_luminance("#ffffff") == pytest.approx(1.0, abs=1e-6)
        assert theme.relative_luminance("#000000") == pytest.approx(0.0, abs=1e-6)
        assert theme.relative_luminance("junk") is None
        assert (theme.relative_luminance("#000000")
                < theme.relative_luminance("#808080")
                < theme.relative_luminance("#ffffff"))


class TestColorTuning:
    colors = {"idle": "#4f8cff", "listening": "#ff4d5e",
              "thinking": "#ff9e2c", "speaking": "#3ecf6e"}

    def test_dark_backdrop_brightens_every_state(self):
        out = theme.tune_colors_for_background(self.colors, 0.02)
        for key, value in out.items():
            assert (theme.relative_luminance(value)
                    > theme.relative_luminance(self.colors[key])), key

    def test_light_backdrop_deepens_every_state(self):
        out = theme.tune_colors_for_background(self.colors, 0.98)
        for key, value in out.items():
            assert (theme.relative_luminance(value)
                    < theme.relative_luminance(self.colors[key])), key

    def test_zero_strength_is_identity(self):
        assert theme.tune_colors_for_background(
            self.colors, 0.02, strength=0.0) == self.colors

    def test_strength_scales_the_effect(self):
        half = theme.tune_colors_for_background(self.colors, 0.02, strength=0.5)
        full = theme.tune_colors_for_background(self.colors, 0.02, strength=1.0)
        assert (theme.relative_luminance(half["idle"])
                < theme.relative_luminance(full["idle"]))

    def test_invalid_entries_and_keys_pass_through(self):
        out = theme.tune_colors_for_background({"a": "not-a-colour"}, 0.1)
        assert out == {"a": "not-a-colour"}
        assert sorted(theme.tune_colors_for_background(self.colors, 0.1)) == \
            sorted(self.colors)


class TestWallpaperDetection:
    def test_config_finds_quoted_and_bare_paths(self):
        quoted = 'spawn-at-startup "swaybg" "-i" "/w/a.png"\n'
        assert theme.wallpaper_path_from_config(quoted, exists=lambda p: True) == "/w/a.png"
        bare = "exec feh --bg-fill /w/b.jpg\n"
        assert theme.wallpaper_path_from_config(bare, exists=lambda p: True) == "/w/b.jpg"

    def test_missing_file_is_not_a_candidate(self):
        quoted = 'spawn-at-startup "swaybg" "-i" "/w/a.png"\n'
        assert theme.wallpaper_path_from_config(quoted, exists=lambda p: False) is None

    def test_unrelated_lines_are_ignored(self):
        text = ('spawn-at-startup "foot"\n'
                'bind "Mod+D" { spawn "fuzzel"; }\n'
                "layout { gaps 8 }\n")
        assert theme.wallpaper_path_from_config(text, exists=lambda p: True) is None

    def test_sample_luminance_parses_magick_output(self):
        run = _runner(0, "1A1A1A\n")
        lum = theme.sample_image_luminance("/w/a.png", runner=run)
        assert lum == pytest.approx(theme.relative_luminance("#1a1a1a"))
        assert run.calls and run.calls[0][0] == "magick"

    def test_sample_luminance_degrades_without_raising(self):
        assert theme.sample_image_luminance("/w/a.png", runner=_runner(1, "")) is None
        assert theme.sample_image_luminance("/w/a.png", runner=_runner(0, "zzz")) is None

        def boom(*_a, **_k):
            raise FileNotFoundError("no magick here")

        assert theme.sample_image_luminance("/w/a.png", runner=boom) is None

    def test_detect_reads_config_then_samples(self, tmp_path):
        cfg = tmp_path / "config.kdl"
        cfg.write_text('spawn-at-startup "swaybg" "-i" "/w/a.png"\n', encoding="utf-8")
        lum = theme.detect_wallpaper_luminance(
            cfg, runner=_runner(0, "FFFFFF"), exists=lambda p: True)
        assert lum == pytest.approx(1.0, abs=1e-6)

    def test_detect_returns_none_when_undetectable(self, tmp_path):
        assert theme.detect_wallpaper_luminance(tmp_path / "absent.kdl") is None
        cfg = tmp_path / "config.kdl"
        cfg.write_text("layout { }\n", encoding="utf-8")
        assert theme.detect_wallpaper_luminance(
            cfg, runner=_runner(0, "FFFFFF"), exists=lambda p: True) is None


class TestBubbleAnimationKnobs:
    """The bubble side: live reload must refresh both knobs, and the neutral
    defaults must reproduce the historical glow curve exactly."""

    class _AudioStub:
        @staticmethod
        def configure(**_kwargs):
            return None

    def test_reload_refreshes_the_knobs(self, H, monkeypatch):
        monkeypatch.setattr(H, "_audio", self._AudioStub())
        monkeypatch.setitem(H.SETTINGS, "animation_energy", 1.8)
        monkeypatch.setitem(H.SETTINGS, "bubble_accent", 0.9)
        H.reload_derived_settings()
        assert H.ANIM_ENERGY == pytest.approx(1.8)
        assert H.BUBBLE_ACCENT == pytest.approx(0.9)

    def test_neutral_defaults_match_the_historical_curve(self, H, monkeypatch):
        monkeypatch.setattr(H, "_audio", self._AudioStub())
        monkeypatch.setitem(H.SETTINGS, "animation_energy", 1.0)
        monkeypatch.setitem(H.SETTINGS, "bubble_accent", 0.5)
        H.reload_derived_settings()
        for state in ("idle", "listening", "thinking", "speaking"):
            assert H._fx_energy(state) == pytest.approx(H._BUBBLE_FX[state][3])

    def test_energy_scales_glow_and_clamps(self, H, monkeypatch):
        monkeypatch.setattr(H, "ANIM_ENERGY", 0.2)
        assert H._fx_energy("thinking") < H._BUBBLE_FX["thinking"][3]
        monkeypatch.setattr(H, "ANIM_ENERGY", 2.0)
        assert H._fx_energy("thinking") == pytest.approx(1.0)   # clamped

    def test_out_of_range_settings_are_clamped_on_reload(self, H, monkeypatch):
        monkeypatch.setattr(H, "_audio", self._AudioStub())
        monkeypatch.setitem(H.SETTINGS, "animation_energy", 99.0)
        monkeypatch.setitem(H.SETTINGS, "bubble_accent", -5.0)
        H.reload_derived_settings()
        assert H.ANIM_ENERGY == 2.0
        assert H.BUBBLE_ACCENT == 0.0


class TestAppearanceCoercion:
    """The two new Appearance keys must clamp like the rest of the schema."""

    def _coerced(self, **over):
        return coerce_settings({**DEFAULT_SETTINGS, **over})

    def test_defaults_are_the_neutral_look(self):
        c = self._coerced()
        assert c["animation_energy"] == 1.0
        assert c["bubble_accent"] == 0.5

    def test_energy_and_accent_are_clamped(self):
        assert self._coerced(animation_energy=9.0)["animation_energy"] == 2.0
        assert self._coerced(animation_energy=0.0)["animation_energy"] == 0.2
        assert self._coerced(bubble_accent=3)["bubble_accent"] == 1.0
        assert self._coerced(bubble_accent=-2)["bubble_accent"] == 0.0

    def test_garbage_falls_back_to_defaults(self):
        c = self._coerced(animation_energy="fast", bubble_accent="punchy")
        assert c["animation_energy"] == DEFAULT_SETTINGS["animation_energy"]
        assert c["bubble_accent"] == DEFAULT_SETTINGS["bubble_accent"]
