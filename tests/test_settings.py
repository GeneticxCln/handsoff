"""Settings tests: app, coercion, health snapshot and status bar."""
from __future__ import annotations

import base64
import importlib.util
import inspect
import re
from collections import deque
import threading
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import (HERE as ROOT, _load, _user_site, method_source,
                      run_driver)

from core import settings as _core_settings

HERE = ROOT   # the repo root (conftest resolves it from conftest.py's parent)


class TestSettingsApp:
    def test_settings_module_loads_with_snippet_writer(self):
        """The settings app imports and ships the keybind-snippet writer."""
        mod = _load("handsoff_settings", HERE / "handsoff-settings.py")
        assert hasattr(mod.SettingsWindow, "_write_keybinds")
        assert hasattr(mod.SettingsWindow, "save")
        # every PTT action the bubble understands is wired into the snippet text
        import inspect
        src = inspect.getsource(mod.SettingsWindow._write_keybinds)
        for action in ("toggle", "interrupt", "handsfree"):
            assert f'"--ptt" "{action}"' in src

    def test_settings_window_reloads_external_changes(self, tmp_path, monkeypatch):
        """merge_settings: disk values win, defaults fill the rest — this is what
        an open settings window re-reads instead of clobbering external writes."""
        mod = _load("handsoff_settings_2", HERE / "handsoff-settings.py")
        disk = {"mic_threshold": 300, "handsfree": False,
                "permissions": {"run_command": False}}
        merged = mod.merge_settings(disk)
        assert merged["mic_threshold"] == 300                 # disk wins
        assert merged["handsfree"] is False                   # disk wins
        assert merged["permissions"]["run_command"] is False  # dict merges key-wise
        assert merged["permissions"]["edit_file"] is True     # …keeping other keys
        assert merged["bubble_size"] == mod.DEFAULT_SETTINGS["bubble_size"]  # default fills
        # empty file -> pure defaults. Compare against the settings app's own
        # schema-derived constant, NOT mod.H's: H loads the *installed* copy
        # first, so cross-module agreement is deployment-sync's job, not this
        # test's.
        assert mod.merge_settings({}) == mod.DEFAULT_SETTINGS

    def test_settings_app_coerces_garbage_values(self):
        """Audit #2 regression: hand-edited garbage ("abc", "1,5", "32k") must
        coerce to defaults inside merge_settings — _load_values' int()/float()
        then see clean types instead of crashing SettingsWindow.__init__ (the
        recovery tool must open even when the config is broken)."""
        mod = _load("handsoff_settings_3", HERE / "handsoff-settings.py")
        for bad in ({"engage_seconds": "abc"}, {"tts_rate": "1,5"},
                    {"num_ctx": "32k"}):
            m = mod.merge_settings(bad)
            for k in bad:
                assert m[k] == mod.H.DEFAULT_SETTINGS[k], (k, m[k])
        # numeric keys come out as real numbers, never strings
        m = mod.merge_settings({"num_ctx": "16384", "tts_rate": "1.25"})
        assert isinstance(m["num_ctx"], int) and m["num_ctx"] == 16384
        assert isinstance(m["tts_rate"], float) and abs(m["tts_rate"] - 1.25) < 1e-9
        # the coercion wiring itself is pinned (not easily removable)
        import inspect
        assert "coerce_settings" in inspect.getsource(mod.merge_settings)

    def test_autostart_defers_to_enabled_systemd_unit(self, monkeypatch):
        """With the systemd unit enabled, checking the autostart checkbox must
        NOT write niri spawn-at-startup (single autostart owner)."""
        mod = _load("handsoff_settings_4", HERE / "handsoff-settings.py")
        monkeypatch.setattr(mod, "systemd_owns_autostart", lambda: True)
        called = []
        monkeypatch.setattr(mod, "set_autostart",
                            lambda enable: called.append(enable) or "wrote")
        msg = mod.apply_autostart(True)
        assert called == [], "spawn-at-startup must not be written"
        assert "NOT added" in msg

    def test_autostart_applies_when_systemd_absent(self, monkeypatch):
        """No systemd unit -> the checkbox still manages the niri spawn line
        (both enable and disable paths)."""
        mod = _load("handsoff_settings_5", HERE / "handsoff-settings.py")
        monkeypatch.setattr(mod, "systemd_owns_autostart", lambda: False)
        called = []
        monkeypatch.setattr(mod, "set_autostart",
                            lambda enable: called.append(enable) or f"wrote {enable}")
        assert mod.apply_autostart(True) == "wrote True"
        assert mod.apply_autostart(False) == "wrote False"
        assert called == [True, False]

    def test_autostart_probe_fails_open_to_niri(self, monkeypatch):
        """systemctl unavailable (no systemd session) -> systemd does NOT own
        autostart, so the niri path stays usable."""
        mod = _load("handsoff_settings_6", HERE / "handsoff-settings.py")
        def boom(*a, **k):
            raise FileNotFoundError("systemctl")
        monkeypatch.setattr(mod.subprocess, "run", boom)
        assert mod.systemd_owns_autostart() is False

    def test_save_routes_through_apply_autostart(self):
        """The GUI save path must call the deferral-aware helper, not
        set_autostart directly (the call site was the original bug)."""
        import inspect
        mod = _load("handsoff_settings_7", HERE / "handsoff-settings.py")
        src = inspect.getsource(mod.SettingsWindow.save)
        assert "apply_autostart(" in src
        assert "set_autostart(" not in src.replace("apply_autostart(", "")

    def test_the_settings_app_never_loads_a_speech_model(self):
        """`Test voice` must speak through the running bubble.

        The old preview built its own piper voice in this process. That cannot
        come back: chatterbox-turbo is ~2.7 GB of VRAM, so a second instance in
        the settings app would fight the bubble for the GPU — and it would
        preview the GUI's own rate/volume/clip rather than the voice the bubble
        will actually use. Pinned on the source, because the tempting "just
        load the model here" fix is invisible until a user's GPU is full.
        """
        import inspect
        mod = _load("handsoff_settings_preview", HERE / "handsoff-settings.py")
        src = inspect.getsource(mod.SettingsWindow.test_voice)
        body = src.split('"""')[2]          # past the docstring, which names it
        assert "say " in body, "the preview must ask the bubble to speak"
        assert "piper" not in body and "chatterbox" not in body, body
        assert "_socket_command" in body, "it must go over the control socket"
        assert mod._VOICE_TEST_LINE and len(mod._VOICE_TEST_LINE) < 200

    def test_the_preview_reports_a_dead_bubble_by_socket_path(self, monkeypatch,
                                                             tmp_path):
        """"Nothing happened" is the failure this whole audit is about: the
        preview has to say the bubble is down, and where it looked."""
        mod = _load("handsoff_settings_preview_down", HERE / "handsoff-settings.py")
        gone = tmp_path / "control.sock"
        monkeypatch.setattr(mod.H, "CONTROL_SOCK", gone)
        assert mod._socket_command(gone, "say hi", timeout=0.5) is None

    def test_every_state_changing_request_carries_the_capability_token(
            self, monkeypatch, tmp_path):
        """A save that only LOOKS applied is the silent-failure shape this
        project keeps re-finding, and this is the request that decides it: the
        bubble re-reads settings.json only when `reload-settings` arrives, so
        that one has to carry the token like any other verb that changes state
        — otherwise Save reports success and the running bubble keeps the old
        values.

        The Voice meter's `level` must NOT read the token file: it is polled
        about twenty times a second.
        """
        mod = _load("handsoff_settings_token", HERE / "handsoff-settings.py")
        token_file = tmp_path / "control.token"
        token_file.write_text("b" * 64 + "\n")
        monkeypatch.setattr(mod.H, "CONTROL_TOKEN", token_file)
        prefix = "token=" + "b" * 64 + "\n"

        for command in ("reload-settings", "clear-history",
                        "say hello there"):
            data = mod._control_request_bytes(command).decode()
            assert data == prefix + command, data
        for verb in sorted(mod.H.PTT_READ_ONLY):
            assert mod._control_request_bytes(verb) == verb.encode(), verb
        # ...and with no token to read it still SENDS, so the refusal comes
        # back with the server's own wording instead of a client guess
        monkeypatch.setattr(mod.H, "CONTROL_TOKEN", tmp_path / "absent.token")
        assert mod._control_request_bytes("reload-settings") == b"reload-settings"


# ---------------------------------------------------------------- keyboard takeover


class TestSettingsCoercion:
    """A bad settings.json value must warn + fall back, never crash startup."""

    def test_bad_num_ctx_falls_back_not_crashes(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"num_ctx": "banana"}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()          # must not raise (was: int() crash at import)
        assert s["num_ctx"] == H.DEFAULT_SETTINGS["num_ctx"]

    def test_valid_values_respected(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"num_ctx": 8192, "mic_threshold": 900,
                                 "max_tool_calls": 30}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["num_ctx"] == 8192 and s["mic_threshold"] == 900
        assert s["max_tool_calls"] == 30

    def test_out_of_range_clamped(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"bubble_size": 9999, "tts_rate": 99}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["bubble_size"] == 192 and s["tts_rate"] == 2.0

    def test_hand_editable_lists_are_capped(self, H):
        """Each of these is a hand-editable list the runtime walks, and an
        unbounded one is unbounded work per turn — spotter models tried per
        utterance, a policy consulted per tool call, an allowlist walked per
        command — or a lookup table that can never match. Its siblings were
        already capped, so the caps above are the missing half of one rule.
        """
        big = H._core_settings.coerce_settings({
            **H.DEFAULT_SETTINGS,
            "extra_allowed_commands": [f"cmd{i}" for i in range(200)],
            "spotter_models": [f"m{i}" for i in range(20)],
            "workspace_aliases": {f"w{i}": f"name{i}" for i in range(200)},
            "command_policy": {f"c{i}": "ALLOW" for i in range(200)},
        })
        assert len(big["extra_allowed_commands"]) == 64
        assert len(big["spotter_models"]) == 5
        assert len(big["workspace_aliases"]) == 50
        assert len(big["command_policy"]) == 64
        # order is preserved (the first N survive), so a deliberate short list
        # is never re-ordered by the cap
        assert big["extra_allowed_commands"][:2] == ["cmd0", "cmd1"]

    def test_truncating_spotter_models_says_so(self, H, caplog):
        """A capped junk value is corrected silently; a truncated LIST is a
        choice to report. The stock default already fills the cap, so the
        first user-added model is entry #6 — exactly the one the silent
        `[:5]` discarded, which is why the wake word "never fired" with no
        journal line to say why (unlike every junk-value warning)."""
        caplog.set_level("INFO", logger="handsoff")
        s = H._core_settings.coerce_settings({
            **H.DEFAULT_SETTINGS,
            "spotter_models": ["alexa", "hey_jarvis", "hey_mycroft",
                               "timer", "weather", "my_custom_wake"]})
        assert s["spotter_models"] == ["alexa", "hey_jarvis", "hey_mycroft",
                                       "timer", "weather"], "cap still applies"
        messages = [r.getMessage() for r in caplog.records]
        assert any("spotter_models" in m and "first 5 of 6" in m
                   for m in messages), \
            f"the truncation must name the key and both counts; got {messages}"

    def test_a_non_string_allowlist_entry_cannot_survive(self, H):
        """The allowlist is matched against command names, so a JSON number or
        null in it is either a matching surprise or dead weight — either way
        it is normalised to a string like the rest of the coercion does."""
        out = H._core_settings.coerce_settings({
            **H.DEFAULT_SETTINGS,
            "extra_allowed_commands": [1, None, "ok", "  ", " two "],
        })
        assert out["extra_allowed_commands"] == ["1", "None", "ok", "two"]

    # `test_every_schema_key_is_touched_by_coercion` lived here and looked for
    # every key as a string literal in coerce_settings' SOURCE, with an
    # exemption set. Coercion is generated from the settings table now, so that
    # guard would find no literals at all — and would have been replaced by
    # nothing if it had merely been deleted. `tests/test_settings_contract.py`
    # carries the stronger version: it counts the rows the coercion APPLIES.

    def test_bubble_design_accepted_and_garbage_falls_back(self, H, tmp_path, monkeypatch):
        """Every shipped design loads; garbage coerces to orb (never a crash).

        Driven from BUBBLE_DESIGNS rather than a hand-copied list: the literal
        tuple that used to live here silently stopped covering new designs
        (it was already missing "sauron" the moment it was added).
        """
        from settings_schema import BUBBLE_DESIGNS
        assert len(BUBBLE_DESIGNS) >= 11, BUBBLE_DESIGNS   # never vacuous
        for name in BUBBLE_DESIGNS:
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"bubble_design": name}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["bubble_design"] == name
        for bad in (" Death Star ", "", None, 123):
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"bubble_design": bad}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["bubble_design"] == "orb"

    def test_avatar_ring_is_a_closed_choice(self, H, tmp_path, monkeypatch):
        """Every decoration in the schema, and junk falls back — a typo must
        not half-decorate, and a name the picker offers must survive the load."""
        from settings_schema import AVATAR_DECOS
        assert len(AVATAR_DECOS) >= 4 and "off" in AVATAR_DECOS, AVATAR_DECOS
        for name in AVATAR_DECOS:
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_ring": name}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_ring"] == name
        for bad in ("Ring Light", "ringlight", "comets", "yes", 1, True, "", None):
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_ring": bad}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_ring"] == "ring-light", bad

    def test_every_decoration_can_be_named_in_the_report(self, H):
        """A slug in the doctor line reads as a bug in the reader.

        The picker offers `AVATAR_DECOS` and the painter draws them; the report
        turns the stored slug into words. The three tables are written in three
        files, so a decoration added to one of them is how the line ends up
        saying `pulse-2` — selectable, drawable and unnameable. `off` is
        exempt because it is never ON, so it never reaches the sentence.
        """
        from settings_schema import AVATAR_DECOS
        missing = [d for d in AVATAR_DECOS
                   if d != "off" and d not in H.DECORATION_LABELS]
        assert not missing, f"decorations the report cannot name: {missing}"
        labels = list(H.DECORATION_LABELS.values())
        assert all(x.strip() for x in labels), labels
        assert len(set(labels)) == len(labels), (
            f"two decorations read the same in the report: {labels}")

    def test_avatar_deco_colour_is_two_words_or_a_literal_hex(self, H, tmp_path,
                                                             monkeypatch):
        """One key, three shapes — and the third is a COLOUR, not a word.

        A hand-edited settings.json is the only way junk can arrive here, and
        it must not reach the painter: the bubble resolves the value with the
        tree's one hex parser, so the settings loader has to admit exactly the
        same strings or the two disagree about which colours exist. `custom`
        is not one of them on purpose — it is the panel's name for "the value
        is a hex", and storing it would leave the bubble reading a word it
        does not know and quietly falling back to the state colour.
        """
        from settings_schema import AVATAR_DECO_COLORS
        assert AVATAR_DECO_COLORS == ("state", "rainbow"), AVATAR_DECO_COLORS
        for good, want in (("state", "state"), ("rainbow", "rainbow"),
                           (" Rainbow ", "rainbow"), ("#4f8cff", "#4f8cff"),
                           ("4f8cff", "4f8cff"), ("#0A0B0C", "#0a0b0c")):
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_deco_color": good}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_deco_color"] == want, good
        for bad in ("custom", "red", "#ff00", "#4f8cffXYZ", "rrggbb", "", None,
                    1, True, "own"):
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_deco_color": bad}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_deco_color"] == "state", bad

    def test_avatar_tint_is_a_closed_choice(self, H, tmp_path, monkeypatch):
        """The colour rule is two values: the state wash, or the art's own."""
        from settings_schema import AVATAR_TINTS
        assert AVATAR_TINTS == ("state", "natural"), AVATAR_TINTS
        for name in AVATAR_TINTS:
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_tint": name}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_tint"] == name
        # ...and the app's own spelling is accepted whatever the case, the same
        # leniency `bubble_design` has: the value is a name, not a token.
        for spelling in ("Natural", "NATURAL", " natural "):
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_tint": spelling}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_tint"] == "natural", spelling
        for bad in ("original", "original colours", "yes", 1, True, "", None,
                    "natural-colours"):
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({"avatar_tint": bad}))
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            assert H._load_settings()["avatar_tint"] == "state", bad

    # ---------------------------------------------------- version + migration

    def test_settings_version_stamped_and_not_a_setting(self, H, tmp_path, monkeypatch):
        """_load_settings stamps the schema version and never exposes it as an
        ordinary setting (unknown-key warning would fire every boot)."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 700}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["version"] == H.SETTINGS_VERSION
        assert s["mic_threshold"] == 700

    def test_settings_future_version_warns_but_loads(self, H, tmp_path, monkeypatch):
        """A settings.json written by a NEWER build must still load (downgrade
        tolerance) — keep the values, warn, don't quarantine."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": 99, "mic_threshold": 500}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["mic_threshold"] == 500
        assert (f.with_suffix(".quarantined")).exists() is False

    def test_settings_migration_clears_version_zero(self, H, tmp_path, monkeypatch):
        """Old files without a version get one stamped (the migrate hook's
        actual job today); unknown keys still warn exactly as before."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": 0, "no_such_key": 1}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["version"] == H.SETTINGS_VERSION

    def test_write_settings_dict_stamps_and_backs_up(self, H, tmp_path, monkeypatch):
        """The shared writer version-stamps settings.json and keeps a one-
        generation .bak so a bad save can be recovered by hand."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 111}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        H._write_settings_dict({"mic_threshold": 222})
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["version"] == H.SETTINGS_VERSION
        assert on_disk["mic_threshold"] == 222
        bak = json.loads(f.with_suffix(".json.bak").read_text(encoding="utf-8"))
        assert bak["mic_threshold"] == 111   # previous generation preserved

    def test_runtime_json_backup_keeps_previous_generation(self, H, tmp_path, monkeypatch):
        """history/memory/reminders writes leave the previous content one
        .bak step behind — a corrupt or truncated write is recoverable."""
        hist = tmp_path / "history.json"
        hist.write_text("[{\"old\": true}]", encoding="utf-8")
        H._backup_runtime_json(hist)
        assert hist.with_suffix(".json.bak").read_text(encoding="utf-8") == "[{\"old\": true}]"
        # overwriting the live file must NOT touch the backup again
        hist.write_text("[]", encoding="utf-8")
        H._backup_runtime_json(hist)
        assert hist.with_suffix(".json.bak").read_text(encoding="utf-8") == "[]"
        hist.unlink()
        H._backup_runtime_json(hist)   # missing file: no-op, no raise


# ------------------------------------------------- monolith split: step (a)


class TestPiperToChatterboxMigration:
    """A settings.json written by the Piper build must load cleanly here.

    The v1 file carries `piper_voice`, a path to a 60 MB .onnx voice that
    chatterbox cannot read at all. Carrying it over as `tts_reference` would
    hand `prepare_conditionals` a file it must reject (it needs > 5 s of audio)
    on EVERY turn — a mute bubble with a tidy value in the GUI. So it is
    dropped, loudly, and the user keeps the built-in voice until they pick a
    clip.
    """

    @staticmethod
    def _load(H, tmp_path, monkeypatch, payload):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps(payload))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        return H._load_settings()

    def test_a_v1_piper_voice_is_dropped_not_renamed(self, H, tmp_path,
                                                    monkeypatch, caplog):
        caplog.set_level("INFO", logger="handsoff")
        s = self._load(H, tmp_path, monkeypatch, {
            "version": 1, "piper_voice": "en_US-lessac-medium.onnx",
            "tts_rate": 1.4, "assistant_name": "cypher"})
        assert "piper_voice" not in s, "the stale key must not survive the load"
        assert s["tts_reference"] == "", "an .onnx voice is not a reference clip"
        assert s["tts_rate"] == 1.4 and s["assistant_name"] == "cypher", \
            "an unrelated setting must not be collateral damage"
        assert s["version"] == H.SETTINGS_VERSION == 2
        # The migration must be what explains the drop. Asserting merely that
        # the line mentions 'piper_voice' is satisfied by the loader's generic
        # `unknown settings key 'piper_voice' — ignored` warning, so the suite
        # would pass with the migration step deleted entirely.
        messages = [r.getMessage() for r in caplog.records]
        assert any("dropped piper_voice" in m for m in messages), \
            f"the migration must say WHAT it dropped; got {messages}"

    def test_a_stale_key_does_not_survive_even_when_the_version_is_current(
            self, H, tmp_path, monkeypatch):
        """The trap that actually happened: the file was stamped v2 by a write
        that still carried piper_voice, so a version-gated migration never ran
        again and the key warned on every start forever.

        Key removal must therefore be schema-driven — 'not in DEFAULT_SETTINGS'
        is the test, not 'the version is old'.
        """
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": H.SETTINGS_VERSION,
                                 "piper_voice": "left-behind.onnx",
                                 "mic_threshold": 640}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        H._SETTINGS_OBJ.settings_file = f
        H._SETTINGS_OBJ.config_dir = tmp_path
        assert H._SETTINGS_OBJ.persist("tts_volume", 0.8) is True
        on_disk = json.loads(f.read_text())
        assert "piper_voice" not in on_disk
        assert on_disk["mic_threshold"] == 640 and on_disk["tts_volume"] == 0.8

    def test_the_drop_survives_a_single_key_save(self, H, tmp_path, monkeypatch):
        """A migration is only real if WRITES cannot resurrect what it removed.

        Single-key persistence read the raw file and merged onto it, so the
        next unrelated save put `piper_voice` straight back — and since the
        schema no longer knows that key, every subsequent start warned
        `unknown settings key 'piper_voice'` for a key the user never wrote.
        """
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": 1, "piper_voice": "en_US-lessac.onnx",
                                 "mic_threshold": 700}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        H._SETTINGS_OBJ.settings_file = f
        H._SETTINGS_OBJ.config_dir = tmp_path
        assert H._SETTINGS_OBJ.persist("tts_rate", 1.5) is True
        on_disk = json.loads(f.read_text())
        assert "piper_voice" not in on_disk, on_disk.get("piper_voice")
        assert on_disk["tts_rate"] == 1.5
        assert on_disk["mic_threshold"] == 700, "the save must not drop settings"
        reloaded = H._load_settings()
        assert "piper_voice" not in reloaded and reloaded["tts_rate"] == 1.5

    def test_a_v2_file_is_left_alone(self, H, tmp_path, monkeypatch):
        s = self._load(H, tmp_path, monkeypatch, {
            "version": 2, "tts_reference": "/tmp/me.wav"})
        assert s["tts_reference"] == "/tmp/me.wav"

    def test_a_write_keeps_keys_from_a_newer_build(self, H, tmp_path,
                                                 monkeypatch):
        """Retired is not the same as unknown, and the difference is data loss.

        Dropping every key the schema does not know is the tidy-looking fix for
        the resurrected `piper_voice`, and it erases the settings of a NEWER
        build sharing the same config: a downgrade would silently delete
        configuration it merely fails to understand. Only the keys a migration
        retired may be removed; everything else survives a write untouched.
        """
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": 1, "piper_voice": "old.onnx",
                                 "wake_word": "cypher",
                                 "mic_threshold": 700}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        H._SETTINGS_OBJ.settings_file = f
        H._SETTINGS_OBJ.config_dir = tmp_path
        assert H._SETTINGS_OBJ.persist("tts_rate", 1.5) is True
        on_disk = json.loads(f.read_text())
        assert "piper_voice" not in on_disk, "a retired key must not come back"
        assert on_disk["wake_word"] == "cypher", (
            "a key this build has never heard of belongs to a newer build's "
            "settings; deleting it on save is silent configuration loss")
        assert on_disk["mic_threshold"] == 700 and on_disk["tts_rate"] == 1.5

    def test_the_schema_no_longer_knows_piper(self):
        import settings_schema
        assert "piper_voice" not in settings_schema.DEFAULT_SETTINGS
        assert settings_schema.DEFAULT_SETTINGS["tts_reference"] == ""
        assert settings_schema.SETTINGS_VERSION >= 2

    def test_the_voice_env_override_points_at_the_reference_clip(
            self, H, tmp_path, monkeypatch):
        """HANDSOFF_VOICE used to select a piper voice. It must now reach the
        live key, or the documented override silently does nothing."""
        monkeypatch.setenv("HANDSOFF_VOICE", "/tmp/clip.wav")
        s = self._load(H, tmp_path, monkeypatch, {})
        assert s["tts_reference"] == "/tmp/clip.wav"

    def test_a_junk_reference_is_coerced_to_a_string(self, H, tmp_path,
                                                   monkeypatch):
        s = self._load(H, tmp_path, monkeypatch, {"tts_reference": ["a", "b"]})
        assert s["tts_reference"] == "", (
            "a list here must not reach the loader, which would try to open it")


class TestSchemaWiring:
    """Adding a setting is one table row plus whatever CONSUMES it.

    A new DEFAULT_SETTINGS key must reach (1) coerce_settings, (2) a control in
    the settings app, and (3) whatever consumes it. (1) and (2) are now both
    generated from `settings_schema.SETTINGS_FIELDS` and guarded in
    `tests/test_settings_contract.py`; what is left for this file is the side a
    table cannot see — that a row a bespoke panel draws is actually named by
    that panel, and that the one no-control bucket stays bookkeeping.
    """

    def test_every_schema_key_reaches_the_settings_app(self, H):
        """The old literal-grep, asked in the form the table makes possible.

        This used to require each key's name to appear in the app's source. That
        is the wrong question now: the control, the load line and the collect
        line are GENERATED from the table, so a key named nowhere in the app is
        exactly what table-driven looks like — and the four keys this test used
        to exempt as deliberately unwired (`whisper_device`, `confirm_seconds`,
        `streaming_tts`, `world_cooldown_min`) have controls in the table
        today. So the literal is kept where it still means something: a row a
        panel draws by name must BE named (or derived from a schema list, the
        same guarantee by another route).
        """
        from settings_schema import SETTINGS_FIELDS, control_for, generated_keys
        source = (HERE / "handsoff-settings.py").read_text()
        rows = {field.key: control_for(field) for field in SETTINGS_FIELDS}
        assert set(rows) == set(H.DEFAULT_SETTINGS), (
            "the table and the shipped defaults disagree — the contract guard "
            "in tests/test_settings_contract.py fails on this too")
        # The per-state picture keys are wired by a LOOP that derives them from
        # the schema (`STATE_IMAGE_KEYS`), so their literals are absent by
        # design — exempted only while that derivation is present, so removing
        # it brings them back as missing rather than passing silently.
        derived = set()
        if '"DESIGN_IMAGE_KEYS"' in source:
            from settings_schema import DESIGN_IMAGE_KEYS
            derived = set(DESIGN_IMAGE_KEYS)
        unnamed = sorted(k for k, control in rows.items()
                         if control == "custom"
                         and f'"{k}"' not in source and k not in derived)
        assert unnamed == [], (
            f"the table says a bespoke panel draws these, and nothing in the "
            f"settings app names them: {unnamed}")
        # The one escape hatch, and it stays small: a row with no control is
        # bookkeeping the BUBBLE writes, not a setting a person edits.
        assert sorted(k for k, control in rows.items()
                      if control == "none") == ["tool_call_times"]
        # The four this test used to exempt now have generated controls, so no
        # exemption list is needed — and keeping one would hide the regression
        # it guards: a control quietly dropped from the table.
        generated = set(generated_keys())
        for key in ("whisper_device", "confirm_seconds", "streaming_tts",
                    "world_cooldown_min"):
            assert key in generated, (
                f"{key} no longer has a control the table generates — it would "
                "be uneditable in the settings app again")


class TestSettingsSplit:
    """Split step (a): the settings machinery lives in core/settings.py behind
    an explicit Settings object; handsoff.py re-exports it under the old
    names. These pins are the refactor's safety net — the H.* contract and
    the path-redirect pattern must not regress while the code moves."""

    def test_three_way_merge_sparse_disk_key_not_leaked(self, H):
        """Regression (bubble design not applying): a key present in the
        GUI's coerced snapshot but absent from the sparse on-disk file must
        not come back from the merge as a deepcopy'd _MISSING sentinel —
        deepcopy breaks the `is not _MISSING` filter and json.dumps then
        raises TypeError, aborting the whole save after the backup step."""
        m = H._core_settings._three_way_merge
        expected = {"model": "m", "bubble_design": "orb", "bubble_size": 128}
        current = {"model": "m", "bubble_size": 97}   # sparse disk file
        candidate = {"model": "m", "bubble_design": "orb", "bubble_size": 97}
        merged, conflict = m(expected, current, candidate, "")
        assert conflict is None
        assert "bubble_design" not in merged
        json.dumps(merged)   # must stay JSON-serializable

    def test_three_way_merge_dual_delete_does_not_leak_the_sentinel(self, H):
        """The OTHER half of the sentinel trap: both sides deleted the key.

        `candidate == current` is true when both are the _MISSING sentinel, so
        the merge took the "take the candidate" branch and `deepcopy(_MISSING)`
        minted a NEW object that is no longer `is _MISSING` — the key came back
        in the written file as a non-serializable sentinel and `save()` raised
        TypeError. The earlier fix covered only the first branch.
        """
        m = H._core_settings._three_way_merge
        sentinel = H._core_settings._MISSING
        # Both sides deleted `dry_run`; `bubble_size` differs at the top level,
        # which is what forces the merge to RECURSE per key — the only way the
        # dual-delete key is ever compared. (When nothing differs at the top the
        # whole candidate is returned and the trap is never reached.)
        expected = {"model": "m", "dry_run": False, "bubble_size": 128}
        current = {"model": "m", "bubble_size": 97}    # disk: dry_run gone
        candidate = {"model": "m", "bubble_size": 128}  # GUI: dry_run gone too
        merged, conflict = m(expected, current, candidate, "")
        assert conflict is None
        assert "dry_run" not in merged, merged
        assert all(v is not sentinel for v in merged.values())
        json.dumps(merged)                # must stay JSON-serializable

    def test_three_way_merge_unchanged_candidate_preserves_disk_extras(self, H):
        """When the GUI changed nothing (candidate == expected), the merge
        returns the disk dict verbatim: foreign runtime keys such as
        tool_call_times must survive a full save."""
        m = H._core_settings._three_way_merge
        expected = {"model": "m"}
        current = {"model": "m", "tool_call_times": [1, 2]}
        merged, conflict = m(expected, current, dict(expected), "")
        assert conflict is None
        assert merged == current

    def test_settings_object_loads_and_persists(self, H, tmp_path, monkeypatch):
        obj = H._core_settings.settings_object(
            tmp_path / "settings.json", tmp_path)
        d = obj.load()
        assert d["version"] == H.SETTINGS_VERSION
        obj.persist("mic_threshold", 777)
        assert obj["mic_threshold"] == 777
        on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert on_disk["mic_threshold"] == 777
        assert on_disk["version"] == H.SETTINGS_VERSION
        # the live dict is the SAME object the object wraps
        obj["handsfree"] = True
        assert obj.as_dict()["handsfree"] is True

    def test_coercion_survives_an_infinite_number(self, H):
        """Python's json parses a bare `Infinity` token, and `int(float('inf'))`
        raises OverflowError — which `_num` did not catch, so ONE crafted (or
        truncated) value in settings.json killed startup inside
        `_load_settings` instead of falling back to the default."""
        assert H.json.loads("Infinity") == float("inf")   # the token parses
        s = {**H.DEFAULT_SETTINGS, "bubble_size": float("inf"),
             "num_ctx": float("nan")}
        out = H._core_settings.coerce_settings(s)
        assert out["bubble_size"] == H.DEFAULT_SETTINGS["bubble_size"]
        assert out["num_ctx"] == H.DEFAULT_SETTINGS["num_ctx"]

    def test_a_runtime_write_is_coerced_before_it_reaches_the_disk(
            self, H, tmp_path, monkeypatch):
        """`set_setting` is the path a TOOL takes, and it stored the model's
        raw argument verbatim. A string in a numeric field then persisted, and
        every later reader that did `int(...)` on it blew up at runtime — or
        silently used the default until the next start."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "m", "version": 2}), encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        H._SETTINGS_OBJ.settings_file = f
        H._SETTINGS_OBJ.config_dir = tmp_path
        assert H._SETTINGS_OBJ.persist("followup_seconds", "nonsense") is True
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["followup_seconds"] == H.DEFAULT_SETTINGS["followup_seconds"]
        assert isinstance(on_disk["followup_seconds"], float)
        # ...and the unrelated keys are exactly as the file had them
        assert on_disk["model"] == "m"

    def test_both_runtime_writers_coerce_MEMORY_not_only_the_disk(
            self, H, tmp_path, monkeypatch):
        """Coercing the disk while storing the raw argument in memory left the
        two disagreeing — the exact thing both writers' docstrings promise they
        cannot do. `mic_threshold` was written to the file as 600 and kept in
        SETTINGS as "junk", so the next PTT release died inside a bare
        `int(...)` on the worker and the hands-free listener's
        `_SpeechGate(int(...))` took the listener thread with it."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "m", "version": H.SETTINGS_VERSION}),
                     encoding="utf-8")

        # (a) the facade the settings app and the bubble share
        obj = H._core_settings.settings_object(f, tmp_path)
        obj.load()
        assert obj.persist("mic_threshold", "junk") is True
        assert obj["mic_threshold"] == H.DEFAULT_SETTINGS["mic_threshold"], \
            "the facade kept the raw argument in memory"

        # (b) the bubble's wrapper, which updates the process-global SETTINGS
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        monkeypatch.setitem(H.SETTINGS, "mic_threshold",
                            H.SETTINGS["mic_threshold"])
        assert H._persist_setting("mic_threshold", "junk") is True
        assert H.SETTINGS["mic_threshold"] == H.DEFAULT_SETTINGS["mic_threshold"], (
            f"memory holds {H.SETTINGS['mic_threshold']!r} while the file holds "
            f"{H.DEFAULT_SETTINGS['mic_threshold']!r}")

    def test_an_unknown_key_still_passes_through_both_writers(
            self, H, tmp_path, monkeypatch):
        """`coerce_setting` must not swallow keys this build has no rule for:
        `set_setting` is the model's escape hatch and the schema is not closed.
        """
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "m", "version": H.SETTINGS_VERSION}),
                     encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        assert H._persist_setting("zz_not_a_schema_key", "raw") is True
        assert H.SETTINGS["zz_not_a_schema_key"] == "raw"
        H.SETTINGS.pop("zz_not_a_schema_key", None)
        assert json.loads(f.read_text(encoding="utf-8"))["zz_not_a_schema_key"] == "raw"

    def test_coercion_never_writes_into_the_callers_object(
            self, H, tmp_path, monkeypatch):
        """The writer must not depend on coercers REPLACING their container.

        Measured 2026-09-19: colours, permissions, command_policy,
        workspace_aliases and spotter_models are each rebuilt rather than
        written into, so neither writer was observably mutating anything. That
        is a property of how they happen to be written, and the two writers
        were relying on it: `probe` shared the caller's nested containers, so
        the first coercer that normalises IN PLACE would have edited the rest
        of the user's file (or the caller's dict) on the way past, silently,
        and only for values that were not already normal. This installs exactly
        such a coercer and holds both writers to the contract.
        """
        from core import settings as _core_settings

        def in_place(s, field, log):
            colors = s.get(field.key)
            if isinstance(colors, dict):
                colors.setdefault("idle", "#000000")   # a write INTO the dict
            s[field.key] = colors or {}

        monkeypatch.setitem(_core_settings._CUSTOM_COERCERS, "colors", in_place)

        # (a) coerce_setting: the object the caller passed stays as it was.
        handed = {"listening": "#ff0000"}
        before = dict(handed)
        H._core_settings.coerce_setting("colors", handed)
        assert handed == before, (
            f"coercion wrote into the caller's dict: {handed}")

        # (b) persist_setting: an unrelated key's save leaves the rest of the
        #     file alone. The file holds ONE state colour, which is the shape a
        #     hand-edited file has — and the state the patched coercer adds.
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"colors": {"listening": "#ff0000"},
                                 "version": H.SETTINGS_VERSION}),
                     encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        monkeypatch.setitem(H.SETTINGS, "mic_threshold",
                            H.SETTINGS["mic_threshold"])
        assert H._persist_setting("mic_threshold", 700) is True
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["colors"] == {"listening": "#ff0000"}, (
            f"saving mic_threshold rewrote the file's colours: {on_disk['colors']}")

    def test_persist_failure_does_not_leave_memory_disagreeing_with_disk(
            self, H, tmp_path, monkeypatch):
        """A swallowed write error used to still update the in-memory copy.

        `_persist_setting` caught OSError and returned silently while
        `Settings.persist` (and the bubble's wrapper, which also re-stamped the
        version and refreshed the derived globals) set `_data[key] = value`
        anyway. The runtime then used — and re-wrote — a value that was never
        on disk. Failure must be reported and the old value kept.
        """
        obj = H._core_settings.settings_object(tmp_path / "settings.json", tmp_path)
        obj.load()
        obj[ "mic_threshold"] = 555
        monkeypatch.setattr(H._core_settings, "_read_settings_for_write",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        assert obj.persist("mic_threshold", 777) is False
        assert obj["mic_threshold"] == 555, "memory changed although the write failed"
        assert json.loads((tmp_path / "settings.json").read_text(
            encoding="utf-8"))["mic_threshold"] != 777

    def test_persist_reports_success(self, H, tmp_path):
        obj = H._core_settings.settings_object(tmp_path / "settings.json", tmp_path)
        obj.load()
        assert obj.persist("mic_threshold", 777) is True
        assert obj["mic_threshold"] == 777

    def test_bubble_wrapper_keeps_globals_when_the_write_fails(
            self, H, tmp_path, monkeypatch):
        """The bubble's wrapper must not refresh derived globals on a failure."""
        monkeypatch.setattr(H, "SETTINGS_FILE", tmp_path / "settings.json")
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        H.SETTINGS["mic_threshold"] = 4242
        refreshed = []
        monkeypatch.setattr(H, "reload_derived_settings",
                            lambda *a, **k: refreshed.append(True))
        monkeypatch.setattr(H._core_settings, "_read_settings_for_write",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        assert H._persist_setting("mic_threshold", 999) is False
        assert H.SETTINGS["mic_threshold"] == 4242
        assert refreshed == [], "derived globals refreshed for an unsaved value"
        H.SETTINGS["mic_threshold"] = 0        # leave the global as we found it

    def test_wrapper_paths_follow_monkeypatched_globals(self, H, tmp_path, monkeypatch):
        """The split must NOT have baked paths in: monkeypatching H's path
        globals (the tests' established pattern) still redirects every
        wrapper — load, persist, and the full-file writer."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 555}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        monkeypatch.setattr(H, "CONFIG_DIR", tmp_path)
        s = H._load_settings()
        assert s["mic_threshold"] == 555
        assert H._SETTINGS_OBJ.settings_file == f          # object follows too
        H._persist_setting("bubble_size", 150)
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["bubble_size"] == 150 and on_disk["mic_threshold"] == 555
        H._write_settings_dict({"model": "m2"})
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["model"] == "m2" and on_disk["version"] == H.SETTINGS_VERSION

    def test_set_setting_uses_wrapper_seam(self, H, tmp_path, monkeypatch):
        """test_regression's seam pin, held at the unit level: set_setting must
        still route through the H._persist_setting wrapper (which adds the
        in-memory update + derived-global refresh).

        The stub models the REAL wrapper's contract — write, update memory,
        return True — because set_setting no longer pre-writes SETTINGS before
        calling it (that pre-write is what let a failed disk write leave memory
        and disk disagreeing). A stub that just records the call would be
        testing nothing about the seam it stands in for.
        """
        calls = []
        monkeypatch.setattr(H, "_persist_setting", lambda k, v: (
            calls.append((k, v)), H.SETTINGS.__setitem__(k, v), True)[-1])
        # H is session-scoped: snapshot the key so the in-memory mutation
        # below is restored at teardown instead of leaking into other tests.
        monkeypatch.setitem(H.SETTINGS, "mic_threshold", H.SETTINGS["mic_threshold"])
        H.set_setting("mic_threshold", 4242)
        assert calls == [("mic_threshold", 4242)]
        assert H.SETTINGS["mic_threshold"] == 4242

    def test_core_and_handsoff_defaults_agree(self, H):
        """One schema instance, two import paths — the SAME objects in a
        healthy tree. Pass on identity, else equality PLUS an identical
        module origin; distinct-but-equal from dual-origin skew (repo vs
        installed core) fails loudly instead of being masked."""
        import core.settings as cs
        core_file = getattr(cs, "__file__", None)
        bubble_file = getattr(getattr(H, "_core_settings", None), "__file__", None)
        same_origin = core_file is not None and core_file == bubble_file
        dbg = ("origin debug: core.settings=%r handsoff=%r bubble-core=%r" % (
            getattr(cs, "__file__", None), getattr(H, "__file__", None),
            bubble_file))
        assert (cs.DEFAULT_SETTINGS is H.DEFAULT_SETTINGS
                or (same_origin and cs.DEFAULT_SETTINGS == H.DEFAULT_SETTINGS)), dbg
        assert same_origin and cs.SETTINGS_VERSION == H.SETTINGS_VERSION, dbg
        assert (_core_settings.coerce_settings is cs.coerce_settings
                or (same_origin and _core_settings.coerce_settings == cs.coerce_settings)), dbg

    def test_loader_refuses_foreign_live_core_settings(self, H, monkeypatch):
        """A foreign module squatting on core.settings must make the shared
        loader refuse (ImportError), never silently satisfy us."""
        import sys
        import types
        import core
        foreign = types.ModuleType("core.settings")
        foreign.__file__ = "/tmp/foreign/core/settings.py"
        monkeypatch.setitem(sys.modules, "core.settings", foreign)
        with pytest.raises(ImportError):
            core.load_module("settings")

    def test_loader_refuses_foreign_live_core_settings_schema(self, H, monkeypatch):
        """Same refusal for core.settings_schema: a live foreign submodule
        blocks the load instead of being swapped under."""
        import sys
        import types
        import core
        foreign = types.ModuleType("core.settings_schema")
        foreign.__file__ = "/tmp/foreign/core/settings_schema.py"
        monkeypatch.setitem(sys.modules, "core.settings_schema", foreign)
        with pytest.raises(ImportError):
            core.load_module("settings_schema")

    def test_wrapper_is_late_bound_for_patch_seam(self, H, monkeypatch):
        """The H._persist_setting wrapper must look the core function up at
        CALL time through _core_settings, so patching the core attr works
        exactly like patching the wrapper. The seam is the module's PUBLIC
        `persist_setting` now — the private step name it used to patch is gone,
        which is the whole point of the rename."""
        seen = []
        monkeypatch.setattr(H._core_settings, "persist_setting",
                            lambda k, v, sf, cd: seen.append(k))
        H._persist_setting("x", 1)
        assert seen == ["x"]

    def test_settings_object_ensure_loaded_and_contains(self, H, tmp_path):
        obj = H._core_settings.settings_object(
            tmp_path / "missing.json", tmp_path, data={"model": "pre-set"})
        assert obj.ensure_loaded()["model"] == "pre-set"   # data= skips disk
        assert "model" in obj and "nope" not in obj
        assert obj.get("nope", "dflt") == "dflt"
        with pytest.raises(KeyError):
            obj["nope"]

    def test_full_save_merges_runtime_key_changed_after_gui_load(self, H, tmp_path):
        """A GUI save must retain a newer, non-conflicting runtime update."""
        cs = H._core_settings
        path = tmp_path / "settings.json"
        gui = cs.settings_object(path, tmp_path)
        base = dict(gui.load())
        runtime = cs.settings_object(path, tmp_path)
        runtime.persist("handsfree", True)

        candidate = dict(base)
        candidate["bubble_size"] = 150
        gui.write_all(candidate, expected_data=base)

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["handsfree"] is True
        assert on_disk["bubble_size"] == 150

    def test_full_save_rejects_conflicting_runtime_update(self, H, tmp_path):
        """A GUI save must not overwrite a newer change to the same key."""
        cs = H._core_settings
        path = tmp_path / "settings.json"
        gui = cs.settings_object(path, tmp_path)
        base = dict(gui.load())
        cs.settings_object(path, tmp_path).persist("bubble_size", 160)

        candidate = dict(base)
        candidate["bubble_size"] = 150
        with pytest.raises(cs.SettingsConflictError):
            gui.write_all(candidate, expected_data=base)

        assert json.loads(path.read_text(encoding="utf-8"))["bubble_size"] == 160

    def test_settings_writes_serialize_through_shared_lock(self, H, tmp_path, monkeypatch):
        """The common writer lock prevents overlapping full writes."""
        cs = H._core_settings
        path = tmp_path / "settings.json"
        entered = threading.Event()
        release = threading.Event()
        active = 0
        maximum = 0
        state_lock = threading.Lock()
        real_write = cs.atomic_private_write

        def slow_write(*args, **kwargs):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            entered.set()
            release.wait(timeout=5)
            try:
                return real_write(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        monkeypatch.setattr(cs, "atomic_private_write", slow_write)
        errors = []

        def write(value):
            try:
                cs.write_settings({"bubble_size": value}, path, tmp_path)
            except BaseException as exc:  # surface worker failures below
                errors.append(exc)

        first = threading.Thread(target=write, args=(150,))
        second = threading.Thread(target=write, args=(151,))
        first.start()
        assert entered.wait(timeout=5)
        second.start()
        time.sleep(0.05)
        with state_lock:
            assert active == 1
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not errors
        assert maximum == 1


class TestHealthCommand:
    """`health` control-socket command: one JSON snapshot of mic, brain and
    TTS status — the mic section shares the reporter's state machine."""

    def _mk_assistant(self, H, listener):
        a = H.Assistant.__new__(H.Assistant)
        a._state = "idle"
        a._handsfree = True
        a._followup_until = 0.0
        a._listener = listener
        return a

    def _mk_listener(self, H, **over):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = True
        ln._frames_seen = 1000
        ln._last_nonzero = time.monotonic()
        ln._capture_rate = 16000
        ln._health_utt = 2
        ln._health_opens_ok = 1
        ln._health_opens_failed = 0
        ln._health_open_device = "TestMic"
        ln._health_last_open = "06:30:00"
        ln._health_failing_since = None
        ln._health_stalled_since = None
        ln._lock = threading.RLock()
        for k, v in over.items():
            setattr(ln, k, v)
        return ln

    def test_snapshot_shape_and_values(self, H, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        a = self._mk_assistant(H, self._mk_listener(H))
        s = a.mic_health()
        assert s["assistant"] == "idle" and s["handsfree"] is True
        assert s["mic"]["state"] == "listening"
        assert s["mic"]["device"] == "TestMic"
        assert s["mic"]["rate"] == 16000
        assert s["mic"]["stalled"] is False and s["mic"]["failing_since"] is None
        assert s["brain"]["reachable"] is True and "model" in s["brain"]
        assert set(s["tts"]) == {"ready", "whisper_ready", "engine",
                                 "device", "reference"}
        assert s["tts"]["engine"] == H.TTS_ENGINE
        json.dumps(s)          # must be JSON-serializable, always

    def test_snapshot_reflects_degraded_mic(self, H, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        ln = self._mk_listener(H, _health_opens_failed=5,
                               _health_failing_since=time.monotonic() - 30)
        a = self._mk_assistant(H, ln)
        s = a.mic_health()
        assert s["mic"]["state"] == "open-failing"
        assert s["mic"]["failing_since"] > 25
        assert s["brain"]["reachable"] is False

    def test_snapshot_silent_state(self, H, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        ln = self._mk_listener(H, _last_nonzero=time.monotonic() - 25.0)
        a = self._mk_assistant(H, ln)
        assert a.mic_health()["mic"]["state"] == "silent"

    def test_state_machine_shared_with_reporter(self, H):
        """mic_snapshot must use the same classifier as the journal reporter."""
        import inspect
        assert "_health_state_now_locked" in inspect.getsource(
            H.ContinuousListener.mic_snapshot)
        assert "_health_state_now_locked()" in inspect.getsource(
            H.ContinuousListener._health_tick)

    @pytest.fixture()
    def server(self, H, tmp_path):
        """Local copy of TestControlSocket's server fixture (fixtures don't
        cross class boundaries): real Assistant + ControlServer on a tmp socket."""
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        sock_path = tmp_path / "control.sock"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(H, "CONTROL_SOCK", sock_path)
        asst = H.Assistant()
        srv = H.ControlServer(asst)
        srv.start()
        deadline, ready = time.time() + 5, False
        while time.time() < deadline:
            try:
                from test_lifecycle import TestControlSocket
                if TestControlSocket._roundtrip(sock_path, "status").startswith("state="):
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(0.05)
        assert ready, "control server never answered"
        try:
            yield H, None, None
        finally:
            srv.stop()                  # the accept loop is a named worker
            monkey.undo()
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def test_health_roundtrip_over_socket(self, server):
        H, _delivered, _app = server
        monkey = pytest.MonkeyPatch()
        monkey.setattr(H, "ollama_available", lambda: True)
        try:
            from test_lifecycle import TestControlSocket
            raw = TestControlSocket._roundtrip(H.CONTROL_SOCK, "health")
        finally:
            monkey.undo()
        payload = json.loads(raw)
        assert payload["mic"]["state"] in ("listening", "silent",
                                           "open-failing", "stopped")
        assert payload["brain"]["reachable"] is True
        # engine/device/reference are reported, not just readiness: "ready"
        # cannot tell the built-in voice from a failed reference clip, and this
        # snapshot is what Settings → Voice and the health bar read.
        assert set(payload["tts"]) == {"ready", "whisper_ready", "engine",
                                       "device", "reference"}
        assert payload["tts"]["engine"] == H.TTS_ENGINE

    def test_health_listed_in_usage_and_actions(self, H):
        assert "health" in H.PTT_ACTIONS
        assert "health" in H.USAGE


class TestSettingsHealthBar:
    """The settings app's status bar shows the running bubble's health
    snapshot live: _health_query fetches, _fmt_health renders, the window
    wires a 3 s timer and stops it on close."""

    def _mod(self):
        return _load("handsoff_settings_hb", HERE / "handsoff-settings.py")

    def test_fmt_health_healthy(self):
        mod = self._mod()
        line = mod._fmt_health({
            "mic": {"state": "listening", "device": "TestMic", "rate": 16000,
                    "utterances": 3, "stalled": False, "failing_since": None},
            "brain": {"reachable": True, "model": "m"},
            "tts": {"ready": True, "whisper_ready": True}})
        assert "mic: listening (TestMic)" in line and "@ 16000 Hz" in line
        assert "3 utt" in line and "brain: ok m" in line and "tts/stt: ok" in line

    def test_fmt_health_degraded_and_partial(self):
        mod = self._mod()
        line = mod._fmt_health({
            "mic": {"state": "silent", "stalled": True, "failing_since": 12.4},
            "brain": {"reachable": False},
            "tts": {"ready": False, "whisper_ready": False}})
        assert "silent" in line and "stalled" in line and "failing 12s" in line
        assert "brain: DOWN" in line and "loading" in line
        # empty/partial snapshots must never raise
        assert "mic: ?" in mod._fmt_health({})
        assert "brain: DOWN" in mod._fmt_health({"mic": {"state": "listening"}})

    def test_query_dead_socket_returns_none(self, tmp_path):
        mod = self._mod()
        assert mod._health_query(tmp_path / "nope.sock") is None
        assert mod._health_query(None) is None

    def test_query_refuses_a_reply_that_is_not_a_json_object(self, tmp_path, monkeypatch):
        """A socket that answered SOMETHING has not answered `health`.

        Driven with a canned reply rather than by racing a real one: which of
        these branches a live socket takes depends on what arrives inside a
        timeout, and a coverage figure that depends on that is a coverage
        figure that moves. Every shape but the last must read as NO ANSWER,
        because the caller renders whatever it gets as a status line and a
        half-parsed reply would be shown as `mic: ?` beside a real timestamp.
        """
        mod = self._mod()
        sock = tmp_path / "c.sock"

        def reply(text):
            monkeypatch.setattr(mod, "_socket_command", lambda *a, **k: text)
            return mod._json_command(sock, "health", 1.0)

        assert reply("pong: not json at all") is None
        assert reply("[1, 2]") is None                 # JSON, but not a snapshot
        assert reply('"just a string"') is None
        assert reply("") is None                       # answered, then closed
        assert reply(None) is None                      # the bubble is down
        assert reply('{"mic": {"state": "listening"}}') == {
            "mic": {"state": "listening"}}

    def test_query_against_real_server(self, H, tmp_path):
        """_health_query speaks to the real ControlServer implementation."""
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        mod = self._mod()
        sock = tmp_path / "c.sock"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(H, "CONTROL_SOCK", sock)
        asst = H.Assistant()
        srv = H.ControlServer(asst)
        srv.start()
        try:
            deadline, snap = time.time() + 5, None
            while time.time() < deadline and snap is None:
                snap = mod._health_query(sock, timeout=2.0)
                if snap is None:
                    time.sleep(0.05)
            assert isinstance(snap, dict) and "mic" in snap and "brain" in snap
        finally:
            srv.stop()
            monkey.undo()
            asst.deleteLater()

    def test_window_wires_timer_and_cleanup(self):
        mod = self._mod()
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert "_health_timer.setInterval(3000)" in src
        assert "_refresh_health" in src
        # Whole methods, not windows of characters: a window makes these fail
        # when a line is ADDED above the one they look for, which says the code
        # moved rather than that the wiring is gone.
        ce = method_source(src, "closeEvent")
        assert "_health_timer.stop()" in ce
        # the fetch must run off the GUI thread (run_bg), not inline
        rf = method_source(src, "_refresh_health")
        assert "self.run_bg(fetch, done)" in rf


class TestHealthTooltip:
    """The health bar's hover tooltip: the full health JSON as escaped
    <pre> text, or a start-the-service hint when the bubble is unreachable."""

    def _mod(self):
        return _load("handsoff_settings_tip", HERE / "handsoff-settings.py")

    def test_tooltip_shows_escaped_json(self):
        mod = self._mod()
        tip = mod._health_tooltip({
            "mic": {"state": "listening", "device": 'Weird "Name" <x> & y'},
            "brain": {"reachable": True, "model": "m"}})
        assert tip.startswith("<pre>") and tip.endswith("</pre>")
        assert "&quot;mic&quot;" in tip and "&quot;listening&quot;" in tip
        # every HTML-significant character must be escaped, incl. in values
        assert "&lt;x&gt;" in tip and "y" in tip
        assert "<x>" not in tip and '"Name"' not in tip

    def test_tooltip_dead_bubble_hint(self):
        mod = self._mod()
        tip = mod._health_tooltip(None)
        assert "systemctl --user start handsoff.service" in tip
        assert "<pre>" not in tip
        # garbage payloads degrade to the hint, never raise
        assert "systemctl" in mod._health_tooltip("junk")
        assert "systemctl" in mod._health_tooltip(42)

    def test_refresh_wires_the_tooltip(self):
        mod = self._mod()
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        rf = method_source(src, "_refresh_health")
        assert "self.health_label.setToolTip(_health_tooltip(result))" in rf


class TestPerToolPolicyUI:
    """Permissions tab: one ALLOW/DENY/CONFIRM row per declared tool, built
    from the live H.TOOLS registry (no raw 'tool = POLICY' text editing)."""

    def test_policy_rows_built_from_registry_and_roundtrip(self):
        """Offscreen construct, flip a policy, collect, reload: the GUI must
        persist per-tool policy without hand-editing settings.json."""
        code = (
            "import importlib.util;"
            "spec = importlib.util.spec_from_file_location("
            "'s', 'handsoff-settings.py');"
            "mod = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(mod);"
            "from PySide6.QtWidgets import QApplication;"
            "app = QApplication([]);"
            "win = mod.SettingsWindow();"
            "rows = win.policy_rows;"
            "assert 'run_command' in rows, sorted(rows)[:5];"
            "rows['run_command'].setCurrentIndex(1);"   # DENY
            "win._collect();"
            "assert win.cfg['command_policy'].get('run_command') == 'DENY', win.cfg['command_policy'];"
            "rows['run_command'].setCurrentIndex(0);"   # back to ALLOW
            "win._collect();"
            "assert 'run_command' not in win.cfg['command_policy'], 'ALLOW must stay out of the map';"
            "win.cfg['command_policy'] = {'open_app': 'CONFIRM'};"
            "win._load_values();"
            "assert rows['open_app'].currentData() == 'CONFIRM';"
            "print('policy rows:', len(rows))"
        )
        # run_driver: the child loads the settings monolith, so it needs the
        # same user-dir sandbox the parent's in-process loads get. Built from
        # `dict(os.environ)` it resolved the developer's HOME — which is what
        # its CONFIG_DIR/SETTINGS_FILE were read from.
        out = run_driver(
            ["-c", code], env_extra={"QT_QPA_PLATFORM": "offscreen",
                                     "QT_QPA_PLATFORMTHEME": "",
                                     "NO_AT_BRIDGE": "1",
                                     "QT_ACCESSIBILITY": "0"},
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert "policy rows:" in out.stdout

    def test_policy_rows_come_from_the_live_registry(self):
        """Row names must be the real tool census from H.TOOLS, so a newly
        declared tool gets a policy row with no GUI change."""
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert "self.policy_rows" in src
        assert 't["function"]["name"] for t in (getattr(H, "TOOLS", None) or [])' in src


class TestSettingsHistoryTab:
    """Regression: SettingsWindow must open with a History tab."""

    def test_window_constructs_with_history_tab_offscreen(self):
        """Build the window in a subprocess under offscreen Qt (in-process
        construction aborts when earlier tests already hold a QCoreApplication)."""
        code = (
            "import importlib.util;"
            "spec = importlib.util.spec_from_file_location("
            "'s', 'handsoff-settings.py');"
            "mod = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(mod);"
            "from PySide6.QtWidgets import QApplication;"
            "app = QApplication([]);"
            "win = mod.SettingsWindow();"
            "texts = [win.tabs.tabText(i) for i in range(win.tabs.count())];"
            "assert 'History' in texts, texts;"
            "print('tabs:', ','.join(texts))"
        )
        out = run_driver(
            ["-c", code], env_extra={"QT_QPA_PLATFORM": "offscreen",
                                     "QT_QPA_PLATFORMTHEME": "",
                                     "NO_AT_BRIDGE": "1",
                                     "QT_ACCESSIBILITY": "0"},
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert "History" in out.stdout


class TestAppearanceLooks:
    """The named Appearance looks: the catalogue, and the derivation of it.

    A look is DATA over the five keys the Appearance tab already owns, and
    "which look is current" is derived from those values rather than stored
    beside them. These pin the two halves that can rot silently: a catalogue
    entry the loader would reject (one click becomes a no-op), and a
    `look_matching` that claims a look the settings do not spell out.
    """

    def test_catalogue_is_valid_against_the_loader_rules(self, H):
        """Every look must be loadable as written, or the click lies."""
        from settings_schema import APPEARANCE_LOOKS, BUBBLE_DESIGNS
        assert len(APPEARANCE_LOOKS) >= 5, APPEARANCE_LOOKS   # never vacuous
        names, labels, colours = set(), set(), set()
        for entry in APPEARANCE_LOOKS:
            name = entry["name"]
            assert name and name == name.strip().lower(), name
            assert name not in names, f"duplicate look name {name!r}"
            names.add(name)
            assert entry["label"].strip() and entry["note"].strip(), entry
            labels.add(entry["label"])
            assert entry["design"] in BUBBLE_DESIGNS, entry
            assert isinstance(entry["bubble_size"], int), entry
            assert 96 <= entry["bubble_size"] <= 192, entry
            assert 0.2 <= float(entry["animation_energy"]) <= 2.0, entry
            assert 0.0 <= float(entry["bubble_accent"]) <= 1.0, entry
            assert set(entry["colors"]) == {"idle", "listening",
                                            "thinking", "speaking"}, entry
            for key, value in entry["colors"].items():
                # the parser's own shape (anchored, optional '#'): anything else
                # is discarded by the loader and by the bubble
                assert re.fullmatch(r"#?[0-9a-fA-F]{6}", str(value)), (key, value)
            # Distinct looks: two entries with identical values would make
            # "which look is this" unanswerable, and the second one unusable.
            fingerprint = (entry["design"], entry["bubble_size"],
                           float(entry["animation_energy"]),
                           float(entry["bubble_accent"]),
                           tuple(sorted((k, str(v).lower())
                                        for k, v in entry["colors"].items())))
            assert fingerprint not in colours, f"{name} duplicates another look"
            colours.add(fingerprint)
        assert len(labels) == len(names)

    def test_each_look_survives_the_settings_loader(self, H, tmp_path, monkeypatch):
        """The end-to-end version of the rule above: write a look, load it back.

        Driven from the catalogue, so a look added later is covered
        automatically. This is the check that catches a value the coercion
        would quietly replace (an out-of-range size, a colour the parser
        discards) — the click would look like it worked while disk held
        something else.
        """
        from settings_schema import APPEARANCE_LOOKS
        for entry in APPEARANCE_LOOKS:
            f = tmp_path / "settings.json"
            f.write_text(json.dumps({
                "bubble_design": entry["design"],
                "bubble_size": entry["bubble_size"],
                "animation_energy": entry["animation_energy"],
                "bubble_accent": entry["bubble_accent"],
                "colors": dict(entry["colors"]),
            }), encoding="utf-8")
            monkeypatch.setattr(H, "SETTINGS_FILE", f)
            loaded = H._load_settings()
            expected = {"bubble_design": entry["design"],
                        "bubble_size": entry["bubble_size"],
                        "animation_energy": entry["animation_energy"],
                        "bubble_accent": entry["bubble_accent"]}
            for key, want in expected.items():
                assert loaded[key] == want, (entry["name"], key, loaded[key])
            assert loaded["colors"] == entry["colors"], entry["name"]
            assert H._core_settings.look_matching(loaded) == entry["name"], (
                f"the loaded {entry['name']!r} no longer reads back as itself")

    def test_the_shipped_defaults_read_as_the_first_look(self, H):
        """A fresh install must show a look, not Custom — and `look_matching`
        must agree with the loader's own idea of the defaults."""
        from settings_schema import APPEARANCE_LOOKS, DEFAULT_SETTINGS
        assert H._core_settings.look_matching(DEFAULT_SETTINGS) == \
            APPEARANCE_LOOKS[0]["name"]
        assert H._core_settings.look_matching(H._load_settings()) == \
            APPEARANCE_LOOKS[0]["name"]

    def test_a_single_differing_value_reads_as_custom(self, H):
        """One nudge is enough to stop being that look — the tab must be able
        to say Custom, and must never round a near-miss up to a named look."""
        from settings_schema import APPEARANCE_LOOKS
        entry = APPEARANCE_LOOKS[-1]
        base = {
            "bubble_design": entry["design"],
            "bubble_size": entry["bubble_size"],
            "animation_energy": entry["animation_energy"],
            "bubble_accent": entry["bubble_accent"],
            "colors": dict(entry["colors"]),
        }
        assert H._core_settings.look_matching(base) == entry["name"]
        for key, value in (("bubble_size", entry["bubble_size"] + 1),
                           ("animation_energy",
                            float(entry["animation_energy"]) + 0.01),
                           ("bubble_accent", float(entry["bubble_accent"]) + 0.01),
                           ("bubble_design", "orb")):
            if value == base.get(key):
                continue
            changed = dict(base)
            changed[key] = value
            assert H._core_settings.look_matching(changed) == "", (key, value)
        for key in entry["colors"]:
            changed = dict(base)
            changed["colors"] = dict(base["colors"])
            changed["colors"][key] = "#123456"
            assert H._core_settings.look_matching(changed) == "", key
        # ...and junk can never match anything (no crash, no false positive)
        for junk in ({}, {"bubble_design": None, "bubble_size": "wide"},
                     {"bubble_design": entry["design"], "bubble_size": "128",
                      "animation_energy": "loud", "colors": None}):
            assert H._core_settings.look_matching(junk) == ""

    def test_the_bubble_reports_the_current_look(self, H):
        """`--ptt health` / doctor name the look; the name comes from the
        settings, so it cannot disagree with what the bubble is drawing."""
        from settings_schema import APPEARANCE_LOOKS
        entry = APPEARANCE_LOOKS[0]
        H.SETTINGS.clear()
        H.SETTINGS.update({"bubble_design": entry["design"],
                           "bubble_size": entry["bubble_size"],
                           "animation_energy": entry["animation_energy"],
                           "bubble_accent": entry["bubble_accent"],
                           "colors": dict(entry["colors"])})
        assert H._appearance_look() == entry["name"]
        assert entry["label"] in H._appearance_note()
        assert entry["design"] in H._appearance_note()
        # one nudge and the bubble says Custom instead of guessing
        H.SETTINGS["bubble_design"] = "cube"
        assert H._appearance_look() == ""
        assert "Custom" in H._appearance_note()
        # the doctor only grows a line when the host has looks to report
        deps = H._build_doctor_deps()
        assert any(line.startswith("appearance:") for line in
                   H._core_doctor._lines(deps))
        assert not any(line.startswith("appearance:") for line in
                       H._core_doctor._lines(H._core_doctor.DoctorDeps()))

    def test_the_appearance_note_names_the_decoration_and_its_colour(self, H):
        """The ring is INDEPENDENT of the state now, so the report has to say
        which colour it wears: \"why is the ring purple in every state\" is
        answered by that word and by nothing else in the report. It stays silent
        when the colour IS the state's, because a line that says `(state)` on
        every default setup is noise in the one place a user looks — and a
        hand-edited junk value must not reach the report either, since the
        resolver falls back to the state colour and the line has to agree.
        """
        H.SETTINGS.clear()
        H.SETTINGS.update({"avatar_ring": "off", "avatar_deco_color": "state"})
        assert "ring light" not in H._appearance_note()
        H.SETTINGS["avatar_ring"] = "rainbow"
        note = H._appearance_note()
        assert H.DECORATION_LABELS["rainbow"] in note, note
        assert "(state)" not in note, note
        H.SETTINGS["avatar_deco_color"] = "rainbow"
        assert "(rainbow)" in H._appearance_note(), H._appearance_note()
        H.SETTINGS["avatar_deco_color"] = "#FF0000"
        assert "(#ff0000)" in H._appearance_note(), H._appearance_note()
        H.SETTINGS["avatar_deco_color"] = "chartreuse"
        note = H._appearance_note()
        assert "(#ff0000)" not in note and "(chartreuse)" not in note, note

    def test_the_look_doctor_reports_says_when_a_preview_is_on_screen(
            self, H, tmp_path):
        """A preview draws art the settings do NOT name.

        That is precisely where the appearance line would describe a look the
        bubble is not drawing — and do it while someone is staring at the
        difference — so the line says it while a preview is up and stops saying
        it the moment the preview is gone.
        """
        import json as _json
        from PySide6.QtGui import QColor, QImage

        H.SETTINGS.clear()
        H.SETTINGS.update({"bubble_design": "orb", "design_pack": "",
                           "design_image_path": ""})
        assert "previewing" not in H._appearance_note()
        source = tmp_path / "cand"
        source.mkdir()
        img = QImage(32, 32, QImage.Format_ARGB32)
        img.fill(QColor(20, 90, 180, 255))
        assert img.save(str(source / "idle.png"))
        (source / "pack.json").write_text(_json.dumps(
            {"name": "Candidate", "states": {"idle": "idle.png"},
             "any": "idle.png"}), encoding="utf-8")
        try:
            assert H._core_bubble.set_pack_preview(source)[0] == "Candidate"
            note = H._appearance_note()
            assert "previewing Candidate (not installed)" in note, note
            assert "orb" in note, (
                "doctor describes the CONFIGURED look and then says what is "
                "actually on screen, rather than silently rewriting it")
        finally:
            H._core_bubble.clear_pack_preview()
        assert "previewing" not in H._appearance_note()
