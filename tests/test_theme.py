"""Bubble palette tuning (core/theme.py): wallpaper detection + backdrop tuning.

The Appearance tab's "match wallpaper" path is three separable pieces — colour
maths, config parsing and an ImageMagick sample — so each is pinned here
without a display, a wallpaper or ImageMagick installed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

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


def _ffmpeg_writes(run):
    """Make a stub runner actually produce the frame ffmpeg was asked for.

    `sample_image_luminance` checks the output file is non-empty, because a
    zero-return ffmpeg that wrote nothing is a failure it must not read as a
    black wallpaper. A stub that returns 0 without writing is that case.
    """
    inner = run

    def writing(cmd, **_kwargs):
        result = inner(cmd, **_kwargs)
        if cmd and cmd[0] == "ffmpeg" and result.returncode == 0 and cmd[-1] != cmd[0]:
            Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return result

    writing.calls = run.calls
    return writing


class TestThemeMath:
    def test_hex_rgb_round_trip(self):
        assert theme.hex_to_rgb("#4f8cff") == (79, 140, 255)
        assert theme.hex_to_rgb("4f8cff") == (79, 140, 255)
        assert theme.hex_to_rgb("nope") is None
        assert theme.hex_to_rgb("") is None

    def test_hex_rgb_is_anchored_to_exactly_six_digits(self):
        """An unanchored prefix match accepted '#4f8cffXYZ' and silently
        TRUNCATED an 8-digit '#RRGGBBAA' to its first six digits — so a
        malformed or alpha colour validated as a good 6-digit one."""
        assert theme.hex_to_rgb("#4f8cffXYZ") is None
        assert theme.hex_to_rgb("#4f8cffaa") is None
        assert theme.hex_to_rgb("#4f8cf") is None
        assert theme.hex_to_rgb(" #4f8cff ") == (79, 140, 255)
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

    def test_undetected_backdrop_is_a_no_op_not_a_crash(self):
        """detect_wallpaper_luminance returns None on EVERY failure path (no
        config, no file, no ImageMagick, unparseable output), and that None
        used to reach float() and raise TypeError — breaking this function's
        own documented promise that a bad detection can never corrupt the
        palette."""
        assert theme.tune_colors_for_background(self.colors, None) == self.colors
        assert theme.tune_colors_for_background(self.colors, "junk") == self.colors

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
            cfg, runner=_runner(0, "FFFFFF"), exists=lambda p: True,
            home=tmp_path)
        assert lum == pytest.approx(1.0, abs=1e-6)

    def test_detect_returns_none_when_undetectable(self, tmp_path):
        # `home` is pinned: discovery walks the shell's wallpaper directory and
        # caches as well as the niri config, so with the default home this test
        # reads the DEVELOPER's real wallpaper and a machine with one turns
        # "nothing is detectable" into a passing measurement of that machine.
        assert theme.detect_wallpaper_luminance(
            tmp_path / "absent.kdl", home=tmp_path) is None
        cfg = tmp_path / "config.kdl"
        cfg.write_text("layout { }\n", encoding="utf-8")
        assert theme.detect_wallpaper_luminance(
            cfg, runner=_runner(0, "FFFFFF"), exists=lambda p: True,
            home=tmp_path) is None


class TestWallpaperDiscovery:
    """The button consults more than the niri config, because on this desktop
    the wallpaper is a shell-owned VIDEO and the config names nothing."""

    def test_video_wallpaper_is_sampled_from_an_extracted_frame(self, tmp_path):
        """ImageMagick cannot read mp4; the frame comes out via ffmpeg first.
        Frame 0 of a looping wallpaper is a fade-in to black, so the sample is
        taken a second in."""
        video = tmp_path / "loop.mp4"
        video.write_bytes(b"not really a video")
        # ffmpeg "succeeds" by writing the frame it was asked for; a stub that
        # returns 0 without producing the file is the failure path, not this one.
        run = _ffmpeg_writes(_runner(0, "102030"))
        lum = theme.sample_image_luminance(str(video), runner=run)
        assert lum == pytest.approx(theme.relative_luminance("#102030"))
        assert [c[0] for c in run.calls] == ["ffmpeg", "magick"], run.calls
        assert ["-ss", "1"] == run.calls[0][run.calls[0].index("-ss"):][:2]
        # The extracted frame is a temp file: it must not be left behind.
        frame = run.calls[1][1]
        assert frame.endswith(".png") and not Path(frame).exists()

    def test_a_video_that_cannot_be_decoded_is_not_a_style(self, tmp_path):
        video = tmp_path / "loop.mp4"
        video.write_bytes(b"junk")
        run = _ffmpeg_writes(_runner(1, ""))
        assert theme.sample_image_luminance(str(video), runner=run) is None
        # ffmpeg failed, so ImageMagick was never asked to read the video.
        assert [c[0] for c in run.calls] == ["ffmpeg"], run.calls

    def test_candidates_are_ordered_existing_and_deduplicated(self, tmp_path):
        (tmp_path / ".config" / "niri").mkdir(parents=True)
        cfg = tmp_path / ".config" / "niri" / "config.kdl"
        cfg.write_text('spawn-at-startup "swaybg" "-i" "/w/from-config.png"\n',
                       encoding="utf-8")
        wall = tmp_path / "wallpapers"
        wall.mkdir()
        (wall / "a.mp4").write_bytes(b"x")
        (wall / "b.png").write_bytes(b"y")
        os.utime(wall / "b.png", (100, 100))          # older
        os.utime(wall / "a.mp4", (200, 200))          # newer -> the one on screen
        (tmp_path / ".config" / "noctalia").mkdir(parents=True)
        (tmp_path / ".config" / "noctalia" / "settings.json").write_text(
            json.dumps({"wallpaper": {"directory": str(wall)}}), encoding="utf-8")
        found = theme.wallpaper_candidates(cfg, home=tmp_path,
                                           exists=lambda p: True)
        assert found[0] == "/w/from-config.png"        # the config form wins
        assert str(wall / "a.mp4") in found
        assert len(found) == len(set(found))

    def test_detect_finds_a_shell_owned_wallpaper_when_the_config_names_one_none(
            self, tmp_path):
        """The button's whole failure: the niri config names a wallpaper only
        when something like swaybg is spawned from it, and on this desktop the
        wallpaper is a shell-owned video in a cache directory. Detection
        therefore has to reach the shell's own places, not the config alone."""
        (tmp_path / ".config" / "niri").mkdir(parents=True)
        cfg = tmp_path / ".config" / "niri" / "config.kdl"
        cfg.write_text("layout { }\n", encoding="utf-8")   # names nothing
        cache = tmp_path / ".cache" / "noctalia" / "images" / "wallpapers" / "large"
        cache.mkdir(parents=True)
        (cache / "loop.mp4").write_bytes(b"x")
        run = _ffmpeg_writes(_runner(0, "FFFFFF"))
        lum = theme.detect_wallpaper_luminance(
            cfg, runner=run, home=tmp_path, exists=lambda p: True)
        assert lum == pytest.approx(1.0, abs=1e-6)
        assert [c[0] for c in run.calls] == ["ffmpeg", "magick"]

    def test_a_media_file_that_vanishes_between_listing_and_use(self, tmp_path,
                                                              monkeypatch):
        """A directory entry can stop being usable at any step: not a file,
        gone, or unstattable. Each is a `continue`, never an exception."""
        directory = tmp_path / "wallpapers"
        directory.mkdir()
        (directory / "real.mp4").write_bytes(b"x")
        (directory / "vanished.png").write_bytes(b"x")

        class _Entry:
            def __init__(self, path):
                self.path = str(path)
                self.name = path.name

            def is_file(self):
                if self.name == "unreadable.png":
                    raise OSError("unreadable")
                return Path(self.path).is_file()

            def stat(self):
                if self.name == "vanished.png":
                    raise OSError("gone")
                return Path(self.path).stat()

        def scandir(path):
            if str(path) != str(directory):
                raise FileNotFoundError(path)
            return [_Entry(directory / n) for n in
                    ("real.mp4", "unreadable.png", "vanished.png")]

        monkeypatch.setattr(theme.os, "scandir", scandir)
        found = theme._newest_media(str(directory))
        assert found == str(directory / "real.mp4")

        # ...and a media entry the injected `exists` rejects is skipped, which is
        # how a test in a sandbox drives which file "is" on screen
        assert theme._newest_media(str(directory),
                                   exists=lambda p: False) is None
        assert theme._newest_media(str(tmp_path / "absent")) is None

    def test_a_frame_that_cannot_be_cleaned_up_is_still_cleaned_up_quietly(
            self, tmp_path, monkeypatch):
        """The extracted frame is a temp file removed in a `finally`; a failed
        remove must not turn a failed sample into a traceback."""
        video = tmp_path / "loop.mp4"
        video.write_bytes(b"x")
        calls = []

        def run(cmd, **_k):
            calls.append(cmd)
            if cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
                return _Result(0, "")
            return _Result(1, "")            # magick fails: no colour found

        monkeypatch.setattr(theme.os, "unlink",
                            lambda _p: (_ for _ in ()).throw(OSError("locked")))
        assert theme.sample_image_luminance(str(video), runner=run) is None
        assert [c[0] for c in calls] == ["ffmpeg", "magick"]

    def test_report_says_what_it_looked_at_when_it_found_nothing(self, tmp_path):
        """A silent no-op is the complaint this replaces: the user clicks and
        nothing happens, with no way to tell what was consulted."""
        report = theme.wallpaper_report(tmp_path / "absent.kdl", home=tmp_path)
        assert report["path"] is None and report["luminance"] is None
        assert isinstance(report["checked"], list)

    def test_a_missing_ffmpeg_is_not_a_style_failure(self, tmp_path, monkeypatch):
        """Neither the decoder nor the unlink may raise into the click path."""
        video = tmp_path / "loop.mp4"
        video.write_bytes(b"junk")

        def no_ffmpeg(*_a, **_k):
            raise FileNotFoundError("no ffmpeg here")

        assert theme.sample_image_luminance(str(video), runner=no_ffmpeg) is None
        # ...and a temp frame that cannot be removed must not turn a failed
        # sample into a traceback either.
        monkeypatch.setattr(theme.os.path, "getsize",
                            lambda _p: (_ for _ in ()).throw(OSError("gone")))
        monkeypatch.setattr(theme.os, "unlink",
                            lambda _p: (_ for _ in ()).throw(OSError("locked")))
        assert theme.sample_image_luminance(str(video),
                                           runner=_ffmpeg_writes(_runner(0, "FFFFFF"))) is None

    def test_candidate_directories_that_are_missing_or_junk(self, tmp_path):
        """Every discovery source is optional: none may raise, and a directory
        that does not exist is simply not a candidate."""
        # no shell settings, no caches, no hyprpaper: nothing, but no raise
        assert theme.wallpaper_candidates(None, home=tmp_path) == []
        # junk shell settings: unreadable JSON is not a crash
        noct = tmp_path / ".config" / "noctalia"
        noct.mkdir(parents=True)
        (noct / "settings.json").write_text("not json", encoding="utf-8")
        assert theme.wallpaper_candidates(None, home=tmp_path) == []

    def test_swww_and_hyprpaper_caches_are_places_too(self, tmp_path):
        skips = []
        swww = tmp_path / ".cache" / "swww"
        out = swww / "DP-1"
        out.mkdir(parents=True)
        (out / "current.png").write_bytes(b"x")
        (out / "notes.txt").write_bytes(b"x")        # wrong extension
        (out / "subdir").mkdir()                      # not a file
        (out / "broken.png").symlink_to(out / "nope.png")   # dangling
        hypr = tmp_path / ".config" / "hypr"
        hypr.mkdir(parents=True)
        (hypr / "hyprpaper.conf").write_text(
            "preload = ~/w/all.png\nwallpaper = DP-2,~/.wallpapers/hypr.png\n",
            encoding="utf-8")
        found = theme.wallpaper_candidates(
            None, home=tmp_path,
            exists=lambda p: skips.append(p) or not str(p).endswith(
                ("nope.png", "broken.png")))
        assert str(out / "current.png") in found
        assert not any(p.endswith("notes.txt") for p in found)
        # hyprpaper's `wallpaper = OUTPUT,PATH` form resolves through the same
        # parser the niri config uses
        assert any(p.endswith("hypr.png") for p in skips)

    def test_report_names_the_file_it_sampled(self, tmp_path):
        (tmp_path / ".config" / "niri").mkdir(parents=True)
        cfg = tmp_path / ".config" / "niri" / "config.kdl"
        cfg.write_text('spawn-at-startup "swaybg" "-i" "/w/a.png"\n', encoding="utf-8")
        report = theme.wallpaper_report(cfg, runner=_runner(0, "FFFFFF"),
                                        home=tmp_path, exists=lambda p: True)
        assert report["path"] == "/w/a.png"
        assert report["luminance"] == pytest.approx(1.0, abs=1e-6)


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
        assert H._core_bubble.ANIM_ENERGY == pytest.approx(1.8)
        assert H._core_bubble.BUBBLE_ACCENT == pytest.approx(0.9)

    def test_neutral_defaults_match_the_historical_curve(self, H, monkeypatch):
        monkeypatch.setattr(H, "_audio", self._AudioStub())
        monkeypatch.setitem(H.SETTINGS, "animation_energy", 1.0)
        monkeypatch.setitem(H.SETTINGS, "bubble_accent", 0.5)
        H.reload_derived_settings()
        for state in ("idle", "listening", "thinking", "speaking"):
            assert H._core_bubble._fx_energy(state) == pytest.approx(H._core_bubble._BUBBLE_FX[state][3])

    def test_energy_scales_glow_and_clamps(self, H, monkeypatch):
        monkeypatch.setattr(H._core_bubble, "ANIM_ENERGY", 0.2)
        assert H._core_bubble._fx_energy("thinking") < H._core_bubble._BUBBLE_FX["thinking"][3]
        monkeypatch.setattr(H._core_bubble, "ANIM_ENERGY", 2.0)
        assert H._core_bubble._fx_energy("thinking") == pytest.approx(1.0)   # clamped

    def test_out_of_range_settings_are_clamped_on_reload(self, H, monkeypatch):
        monkeypatch.setattr(H, "_audio", self._AudioStub())
        monkeypatch.setitem(H.SETTINGS, "animation_energy", 99.0)
        monkeypatch.setitem(H.SETTINGS, "bubble_accent", -5.0)
        H.reload_derived_settings()
        assert H._core_bubble.ANIM_ENERGY == 2.0
        assert H._core_bubble.BUBBLE_ACCENT == 0.0


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
