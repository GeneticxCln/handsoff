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
        assert merged["bubble_size"] == mod.H.DEFAULT_SETTINGS["bubble_size"]  # default fills
        assert mod.merge_settings({}) == mod.H.DEFAULT_SETTINGS  # empty file -> pure defaults

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
        assert set(s["tts"]) == {"ready", "whisper_ready"}
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
        assert set(payload["tts"]) == {"ready", "whisper_ready"}

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
