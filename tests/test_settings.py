"""Settings tests: app, coercion, health snapshot and status bar."""
from __future__ import annotations

import base64
import importlib.util
import inspect
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

from conftest import HERE as ROOT, _load, _user_site

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

    def test_every_schema_key_is_touched_by_coercion(self, H):
        """A schema key that coercion never mentions is a silent hole.

        coerce_settings validates key by key, hand-written, so adding a
        DEFAULT_SETTINGS entry and forgetting to validate it ships a setting
        that reaches the bubble raw — a string where a bool is expected, an
        out-of-range number leaking through. Nothing fails when that happens,
        which is why this guard exists.
        """
        body = inspect.getsource(H.coerce_settings)
        # Deliberately passed through: written by the installer, read by
        # nothing in the runtime (a dead schema entry, noted in GAP_ANALYSIS).
        free_form = {"autostart"}
        missing = sorted(k for k in H.DEFAULT_SETTINGS
                         if f'"{k}"' not in body and k not in free_form)
        assert missing == [], (
            f"these settings are never coerced: {missing} — validate them in "
            "coerce_settings, or add them to free_form with a reason")

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
    """Adding a setting takes three edits, and two of them are now guarded.

    A new DEFAULT_SETTINGS key must reach (1) coerce_settings — guarded in
    TestSettingsCoercion — (2) a control in the settings app, and (3) whatever
    consumes it. Forgetting (2) fails nothing and shows nothing: the setting
    just cannot be edited, and nobody notices. This makes that a decision.
    """

    # Consumed by the bubble, deliberately without a settings-app control.
    NO_GUI_CONTROL = {
        "tool_call_times",     # bookkeeping the bubble writes at runtime
        "whisper_device",      # read as WHISPER_DEVICE; defaults to auto-detect
        "confirm_seconds",     # default for the per-tool confirmation policy
        "streaming_tts",       # read by the speech path only
        "world_cooldown_min",  # read by the world-warning ticker
    }

    def test_every_schema_key_reaches_the_settings_app(self, H):
        source = (HERE / "handsoff-settings.py").read_text()
        missing = sorted(k for k in H.DEFAULT_SETTINGS
                         if f'"{k}"' not in source
                         and k not in self.NO_GUI_CONTROL)
        assert missing == [], (
            f"these settings have no settings-app wiring: {missing} — add a "
            "control, or list them in NO_GUI_CONTROL with a reason")


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
        assert (H.coerce_settings is cs.coerce_settings
                or (same_origin and H.coerce_settings == cs.coerce_settings)), dbg

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
        CALL time through _core_settings, so patching the core attr (future
        split steps' seam) works exactly like patching the wrapper."""
        seen = []
        monkeypatch.setattr(H._core_settings, "_persist_setting",
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
        real_write = cs._atomic_private_write

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

        monkeypatch.setattr(cs, "_atomic_private_write", slow_write)
        errors = []

        def write(value):
            try:
                cs._write_settings_dict({"bubble_size": value}, path, tmp_path)
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
            monkey.undo()
            asst.deleteLater()

    def test_window_wires_timer_and_cleanup(self):
        mod = self._mod()
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert "_health_timer.setInterval(3000)" in src
        assert "_refresh_health" in src
        ce = src[src.index("def closeEvent"):src.index("def closeEvent") + 500]
        assert "_health_timer.stop()" in ce
        # the fetch must run off the GUI thread (run_bg), not inline
        rf = src[src.index("def _refresh_health"):
                 src.index("def closeEvent")]
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
        rf = src[src.index("def _refresh_health"):
                 src.index("def _refresh_health") + 900]
        assert "self.health_label.setToolTip(_health_tooltip(result))" in rf


class TestPerToolPolicyUI:
    """Permissions tab: one ALLOW/DENY/CONFIRM row per declared tool, built
    from the live H.TOOLS registry (no raw 'tool = POLICY' text editing)."""

    def test_policy_rows_built_from_registry_and_roundtrip(self):
        """Offscreen construct, flip a policy, collect, reload: the GUI must
        persist per-tool policy without hand-editing settings.json."""
        env = dict(os.environ)
        env.update({
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QPA_PLATFORMTHEME": "",
            "NO_AT_BRIDGE": "1",
            "QT_ACCESSIBILITY": "0",
        })
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
        out = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=120, cwd=str(HERE),
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
        env = dict(os.environ)
        env.update({
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QPA_PLATFORMTHEME": "",
            "NO_AT_BRIDGE": "1",
            "QT_ACCESSIBILITY": "0",
        })
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
        out = subprocess.run(
            [sys.executable, "-c", code],
            env=env, capture_output=True, text=True, timeout=60, cwd=str(HERE),
        )
        assert out.returncode == 0, out.stderr[-2000:]
        assert "History" in out.stdout
