"""Live hardware watch: cheap ticks, change notes, urgent alerts.

Probers mocked (no hardware, no subprocess); the mic listener is stubbed.
Style follows test_world_events.py (session H, tmp state where needed).
"""
from __future__ import annotations

import time
import types

import pytest

from conftest import HERE as ROOT

HERE = ROOT   # the repo root


@pytest.fixture()
def watch_settings(H, monkeypatch):
    monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                        "hardware_watch": True,
                                        "hardware_cooldown_min": 60.0,
                                        "hardware_disk_gb": 5.0,
                                        "resource_alerts": False,
                                        "briefing": False})
    H._DOCTOR_TTL.clear()
    return H.SETTINGS


def _assistant(H, monkeypatch, state="idle", handsfree=False):
    a = H.Assistant.__new__(H.Assistant)
    a._hardware_note = ""
    a._hardware_last = {}
    a._hardware_last_urgent = 0.0
    a._hardware_tick_n = 0
    a._state = state
    a._handsfree = handsfree
    a._listener = types.SimpleNamespace(
        mic_snapshot=lambda: {"state": "listening",
                              "device": "Yeti Stereo"})
    said, popped = [], []
    a._announce_now = said.append
    monkeypatch.setattr(H, "notify", popped.append)
    return a, said, popped


@pytest.fixture()
def quiet_box(H, monkeypatch):
    """No GPU binary, generous disk: the tick stays fully quiet."""
    monkeypatch.setattr(H.shutil, "which", lambda name: None)
    monkeypatch.setattr(H._hardware, "disk_free",
                        lambda path="/": {"ok": True, "total": 10 ** 12,
                                          "free": 100 * 2 ** 30})
    return H


class TestOff:
    def test_off_returns_fast_with_no_gain(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                            "hardware_watch": False})
        a = H.Assistant.__new__(H.Assistant)
        a._hardware_note = ""
        a._hardware_last = {}
        a._hardware_last_urgent = 0.0
        a._hardware_tick_n = 0
        start = time.monotonic()
        a._hardware_tick()
        assert time.monotonic() - start < 0.5
        assert a._hardware_note == ""

    def test_a_junk_value_that_means_off_does_not_run_the_tick(
            self, H, monkeypatch):
        """`bool("false")` is True — the whole tick ran (probes, the one
        in-tick subprocess) for a watch the user turned off. The flag is checked
        before the tick counter advances, so that counter is the witness."""
        for raw in ("false", "no", "off", "nonsense"):
            a, said, popped = _assistant(H, monkeypatch)
            monkeypatch.setitem(H.SETTINGS, "hardware_watch", raw)
            a._hardware_tick()
            assert a._hardware_tick_n == 0, raw        # no work at all
            assert a._hardware_note == "", raw
            assert said == [] and popped == [], raw


class TestFastTick:
    def test_zero_subprocess_calls(self, H, watch_settings, quiet_box,
                                   monkeypatch):
        calls = []
        monkeypatch.setattr(H.subprocess, "run",
                            lambda *a, **k: calls.append(a) or (_ for _ in ()
                                                                ).throw(
                                AssertionError("subprocess in fast tick")))
        a, _, _ = _assistant(H, monkeypatch)
        a._hardware_tick()
        a._hardware_tick()
        assert calls == []

    def test_no_qt_or_audio_imports(self, H):
        import re
        src = (HERE / "hardware.py").read_text(encoding="utf-8")
        assert "disk_usage" in src
        top = [ln for ln in src.splitlines()
               if re.match(r"^(import|from)\s", ln)]
        for banned in ("PySide6", "sounddevice", "whisper", "piper"):
            assert not any(banned in ln for ln in top), top


class TestMicNote:
    def test_change_notes_once_and_consumes_once(self, H, watch_settings,
                                                 quiet_box, monkeypatch):
        a, said, popped = _assistant(H, monkeypatch)
        a._hardware_tick()  # baseline: records, says nothing
        assert a._hardware_note == "" and said == [] and popped == []
        a._listener = types.SimpleNamespace(
            mic_snapshot=lambda: {"state": "silent", "device": "Yeti Stereo"})
        a._hardware_tick()
        assert a._hardware_note and len(a._hardware_note) <= 200
        assert said == [] and popped == []  # non-urgent: no announce/popup
        # consumed-once via _conversation_for
        a._history, a._memory = [], []
        a._briefing_done_date = "2999-01-01"  # keep briefing out of the way
        conv = H.Assistant._conversation_for(a, "hi")
        assert any("Live hardware note" in str(m.get("content", ""))
                   for m in conv)
        assert a._hardware_note == ""
        conv2 = H.Assistant._conversation_for(a, "hi again")
        assert not any("Live hardware note" in str(m.get("content", ""))
                       for m in conv2)


class TestCrossings:
    def _low_disk(self, H, monkeypatch, free_gb):
        monkeypatch.setattr(H._hardware, "disk_free",
                            lambda path="/": {"ok": True, "total": 10 ** 12,
                                              "free": free_gb * 2 ** 30})

    def test_disk_crossing_announces_once_then_rearms(self, H, watch_settings,
                                                      quiet_box, monkeypatch):
        monkeypatch.setattr(H.shutil, "which", lambda name: None)
        a, said, popped = _assistant(H, monkeypatch)
        self._low_disk(H, monkeypatch, 100.0)
        a._hardware_tick()  # healthy baseline
        assert said == [] and popped == []
        self._low_disk(H, monkeypatch, 1.0)
        a._hardware_tick()  # crossing: urgent
        assert len(said) == 1 and len(popped) == 1
        a._hardware_tick()  # still low: silent
        assert len(said) == 1 and len(popped) == 1
        self._low_disk(H, monkeypatch, 100.0)
        a._hardware_tick()  # re-arm, no fanfare beyond a note
        assert len(said) == 1 and len(popped) == 1
        self._low_disk(H, monkeypatch, 1.0)
        a._hardware_last_urgent = 0.0  # cooldown expired since the 1st urgent
        a._hardware_tick()  # crossing again: announces again
        assert len(said) == 2 and len(popped) == 2

    def test_vram_branch_still_runs_for_a_junk_off(self, H, watch_settings,
                                                   quiet_box, monkeypatch):
        """The VRAM crossing is announced HERE only while `resource_alerts` is
        off (`_resource_tick` owns it otherwise, so it is never said twice).
        The two gates live in different functions, so reading one strictly and
        leaving the other raw opened a hole with no owner: a junk "false" made
        `_resource_tick` hand the crossing over and this branch then skip it.
        """
        class _NvidiaSmi:
            stdout = "95, 100\n"

        monkeypatch.setattr(H.shutil, "which",
                            lambda name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: _NvidiaSmi())
        monkeypatch.setitem(H.SETTINGS, "resource_alerts", "false")
        monkeypatch.setitem(H.SETTINGS, "vram_alert_percent", 90.0)
        a, said, popped = _assistant(H, monkeypatch)
        a._hardware_tick_n = 1        # the util query runs on every 2nd tick
        a._hardware_tick()
        assert any("GPU memory" in s for s in said), (said, popped)

    def test_speaking_suppresses_spoken_copy(self, H, watch_settings,
                                             quiet_box, monkeypatch):
        monkeypatch.setattr(H.shutil, "which", lambda name: None)
        a, said, popped = _assistant(H, monkeypatch, state="speaking")
        self._low_disk(H, monkeypatch, 1.0)
        a._hardware_last["disk_low"] = False
        a._hardware_tick()
        assert said == [] and len(popped) == 1  # popup always, no TTS

    def test_cooldown_suppresses_second_urgent(self, H, watch_settings,
                                               quiet_box, monkeypatch):
        monkeypatch.setattr(H.shutil, "which", lambda name: None)
        a, said, popped = _assistant(H, monkeypatch)
        self._low_disk(H, monkeypatch, 1.0)
        a._hardware_tick()  # disk urgent
        assert len(popped) == 1
        # a second, different urgent inside the cooldown window stays quiet
        H._DOCTOR_TTL["data"] = {"ollama": {"ok": False, "error": "refused"}}
        H._DOCTOR_TTL["at"] = {"ollama": time.monotonic()}
        a._hardware_last["ollama_miss"] = 1
        self._low_disk(H, monkeypatch, 100.0)
        a._hardware_tick()
        assert len(said) == 1 and len(popped) == 1

    def test_ollama_single_blip_silent(self, H, watch_settings, quiet_box,
                                       monkeypatch):
        monkeypatch.setattr(H.shutil, "which", lambda name: None)
        a, said, popped = _assistant(H, monkeypatch)
        H._DOCTOR_TTL["data"] = {"ollama": {"ok": False, "error": "refused"}}
        H._DOCTOR_TTL["at"] = {"ollama": time.monotonic()}
        a._hardware_tick()  # first miss: silent
        assert said == [] and popped == []
        a._hardware_tick()  # second consecutive miss: urgent
        assert len(said) == 1 and len(popped) == 1

    def test_ollama_recovery_posts_note(self, H, watch_settings, quiet_box,
                                        monkeypatch):
        monkeypatch.setattr(H.shutil, "which", lambda name: None)
        a, said, popped = _assistant(H, monkeypatch)
        a._hardware_last["ollama_down"] = True
        H._DOCTOR_TTL["data"] = {"ollama": {"ok": True, "models": []}}
        H._DOCTOR_TTL["at"] = {"ollama": time.monotonic()}
        a._hardware_tick()
        assert said == [] and popped == []
        assert "Ollama" in a._hardware_note


class TestRobustness:
    def test_exploding_probers_never_raise(self, H, watch_settings, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("everything is on fire")

        monkeypatch.setattr(H.subprocess, "run", boom)
        monkeypatch.setattr(H.shutil, "which", boom)
        monkeypatch.setattr(H.shutil, "disk_usage", boom)
        monkeypatch.setattr(H.os, "getloadavg", boom)
        a = H.Assistant.__new__(H.Assistant)
        a._hardware_note = ""
        a._hardware_last = {}
        a._hardware_last_urgent = 0.0
        a._hardware_tick_n = 0
        a._state = "idle"
        a._handsfree = False
        a._listener = types.SimpleNamespace(mic_snapshot=boom)
        a._announce_now = lambda t: (_ for _ in ()).throw(AssertionError())
        a._hardware_tick()  # must not raise

    def test_last_good_cache_honored(self, H, watch_settings, quiet_box,
                                     monkeypatch):
        monkeypatch.setattr(H.shutil, "which", lambda name: None)
        a, said, popped = _assistant(H, monkeypatch)
        # last-good merged entry (ok + degraded) counts as reachable
        H._DOCTOR_TTL["data"] = {"ollama": {"ok": True, "models": [],
                                            "degraded": True}}
        H._DOCTOR_TTL["at"] = {"ollama": time.monotonic()}
        a._hardware_last["ollama_miss"] = 1
        a._hardware_tick()
        assert a._hardware_last["ollama_miss"] == 0
        assert said == [] and popped == []
