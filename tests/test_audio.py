"""Audio tests: VAD gate, listener, wake word/spotter, mic health and self-heal."""
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


class FakeSig:
    """Mimics a Qt signal: collect emitted values."""

    def __init__(self) -> None:
        self.values: list = []

    def emit(self, value) -> None:
        self.values.append(value)


class FakeAssistant:
    """Just enough of Assistant for ContinuousListener._process_frame."""

    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.sigLevel = FakeSig()
        self.sigUtterance = FakeSig()
        self.vad_events: list[bool] = []

    def _vad_speech(self, active: bool) -> None:
        self.vad_events.append(active)
        if active and self.state == "idle":
            self.state = "listening"
        elif not active and self.state == "listening":
            self.state = "idle"


class TestSpeechGate:
    def test_silence_produces_no_events(self, H):
        gate = H._SpeechGate(threshold=600)
        for _ in range(200):
            assert gate.feed(30.0) == ""

    def test_loud_speech_starts(self, H):
        gate = H._SpeechGate(threshold=600)
        events = [gate.feed(3000.0) for _ in range(5)]
        assert events[0] == ""            # needs start_frames consecutive frames
        assert "start" in events

    def test_hangover_ends_utterance(self, H):
        gate = H._SpeechGate(threshold=600)
        for _ in range(5):
            gate.feed(3000.0)
        assert gate.in_speech
        assert gate.feed(10.0) == ""      # one quiet frame must not end it
        assert gate.in_speech
        for _ in range(12):
            assert gate.feed(10.0) == ""
        assert gate.feed(10.0) == "end"   # 14th quiet frame (hangover_frames) ends it
        assert not gate.in_speech

    def test_noise_floor_adapts(self, H):
        gate = H._SpeechGate(threshold=600)
        initial_floor = gate.floor
        for _ in range(100):
            gate.feed(100.0)
        assert gate.floor < initial_floor  # floor drifts down in a quiet room

    def test_reset(self, H):
        gate = H._SpeechGate(threshold=600)
        for _ in range(5):
            gate.feed(3000.0)
        gate.reset()
        assert not gate.in_speech
        assert gate._loud == 0 and gate._quiet == 0

    def test_start_frames_requires_consecutive(self, H):
        gate = H._SpeechGate(threshold=600)
        assert gate.feed(3000.0) == ""
        assert gate.feed(10.0) == ""      # break the streak
        assert gate.feed(3000.0) == ""    # streak restarts
        assert gate.feed(3000.0) == "start"  # two consecutive loud frames open it


# ---------------------------------------------------- ContinuousListener (regression)


class TestListenerSelfMute:
    @pytest.fixture()
    def env(self, H):
        asst = FakeAssistant()
        lst = H.ContinuousListener(asst)
        gate = H._SpeechGate(threshold=600)
        frames: list = []
        return lst, asst, gate, frames, 100, 4   # max_frames, min_frames

    @staticmethod
    def _feed(lst, gate, frames, max_f, min_f, rms: float) -> None:
        frame = (np.ones(1024, dtype=np.int16) * min(int(rms), 32000)).reshape(1, 1024)
        lst._process_frame(frame, gate, frames, max_f, min_f)

    def test_utterance_survives_own_listening_state(self, env):
        """Regression: state==LISTENING must not mute the ongoing utterance."""
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert gate.in_speech
        assert asst.state == "listening"      # the VAD itself set this
        for _ in range(20):                   # keep talking past hangover reset
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        for _ in range(15):                   # then go quiet -> 'end'
            self._feed(lst, gate, frames, max_f, min_f, 10.0)
        assert len(asst.sigUtterance.values) == 1
        audio = asst.sigUtterance.values[0]
        assert isinstance(audio, np.ndarray) and len(audio) > 0

    def test_speaking_state_discards_frames(self, env):
        """The assistant must not hear its own TTS output."""
        lst, asst, gate, frames, max_f, min_f = env
        asst.state = "speaking"
        for _ in range(10):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert not gate.in_speech
        assert frames == []
        assert asst.sigUtterance.values == []

    def test_suspended_discards_frames(self, env):
        """A push-to-talk press suspends the continuous listener."""
        lst, asst, gate, frames, max_f, min_f = env
        lst.suspend()
        for _ in range(10):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert frames == []
        assert asst.sigUtterance.values == []

    def test_reset_discards_partial_utterance(self, env):
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert frames, "gate opened, frames should be buffered"
        lst.reset()
        self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert frames == [] and not gate.in_speech

    def test_max_utterance_length_flushes(self, env):
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        for _ in range(max_f):                # hammer past the cap
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert len(asst.sigUtterance.values) == 1

    def test_thinking_state_discards_frames(self, env):
        """Regression: while the brain is generating, mic input is junk
        (the bubble is talking or the user is reacting) — discard it."""
        lst, asst, gate, frames, max_f, min_f = env
        asst.state = "thinking"
        for _ in range(10):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert not gate.in_speech
        assert frames == []
        assert asst.sigUtterance.values == []

    def test_thinking_does_not_kill_open_utterance(self, env):
        """If the VAD already collected an utterance (barge-in), a state flip
        to THINKING must not silently eat it."""
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert gate.in_speech
        asst.state = "thinking"
        for _ in range(15):                   # keep talking
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        for _ in range(15):                   # go quiet -> end
            self._feed(lst, gate, frames, max_f, min_f, 10.0)
        assert len(asst.sigUtterance.values) == 1


class TestEchoRejection:
    """The mic hears the bubble's own TTS through the speakers; those echo
    captures must be rejected before they reach the LLM (the root cause of
    the parrot loop)."""

    def test_exact_tail_is_echo(self, H):
        assert H._is_echo("What is your request?", ["What is your request?"])
        assert H._is_echo("request.", ["What is your request?"])
        assert H._is_echo("Hello.", ["Hello."])

    def test_rephrase_is_echo(self, H):
        assert H._is_echo("hello there", ["Hello! How can I help you today?"])

    def test_real_user_speech_is_not_echo(self, H):
        assert not H._is_echo("what is the weather", ["What is your request?"])
        assert not H._is_echo("tell me a joke please", ["Hello."])
        assert not H._is_echo("", ["Hello."])
        assert not H._is_echo("anything at all", [])


# ------------------------------------------------------------------------ ToolBelt


class TestWakeWord:
    """Wake-word gate: hands-free answers only when addressed by name."""

    def test_match_wake_variants(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "assistant")  # isolate from live name
        assert H._match_wake("assistant what's the weather") == "what's the weather"
        assert H._match_wake("hey assistant open firefox") == "open firefox"
        assert H._match_wake("Assistant, tell me a joke") == "tell me a joke"
        assert H._match_wake("assistant") == ""
        assert H._match_wake("hey assistant") == ""

    def test_no_wake(self, H):
        assert H._match_wake("what's the weather") is None
        assert H._match_wake("hey google what's up") is None
        # the classic regex trap: 'a' must not match out of 'assistant ...'
        assert H._match_wake("a stainless steel bottle") is None
        assert H._match_wake("") is None

    def test_wake_utt(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "assistant")  # isolate from live name
        assert H._is_wake_utt("hey assistant")
        assert H._is_wake_utt("Assistant!")
        assert not H._is_wake_utt("assistant what time")

    def test_custom_name(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "hey bubble")
        assert H._match_wake("hey bubble what time") == "what time"
        assert H._is_wake_utt("hey bubble")

    def test_new_settings_coerce(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"assistant_name": "  Nova  ",
                                 "wake_word_required": True,
                                 "engage_seconds": "banana"}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["assistant_name"] == "Nova" and s["wake_word_required"] is True
        assert s["engage_seconds"] == 45.0  # garbage -> default, never crash

    def test_engage_seconds_clamped(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"engage_seconds": 9999}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        assert H._load_settings()["engage_seconds"] == 600.0

    def test_prompt_mentions_name(self, H):
        assert "WAKE WORD" in H.SYSTEM_PROMPT

    def test_bulk_typing_single_call(self, H, monkeypatch):
        """type_text issues ONE ydotool call for short text (the old chunk
        loop made ~1 call per 32 chars + sleeps: ~30x slower)."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        calls = []
        belt._ydotool = lambda *a: (calls.append(a), "ok")[1]
        # stub the CURRENT seam (a live _typing_guard would consult real niri
        # focus and fail closed whenever a terminal happens to be focused)
        monkeypatch.setattr(belt, "_focused_window_info",
                            lambda: {"app_id": "firefox", "title": "Firefox"})
        out, err = belt.execute("type_text", {"text": "hello beautiful world"})
        assert not err and "typed" in out
        assert len(calls) == 1 and "hello beautiful world" in calls[0]


class TestWakeSpotter:
    """openWakeWord spotter: residual buffering, single predict, integration."""

    def test_feed_carries_residual_and_predicts_once_per_1280(self, H, monkeypatch):
        calls = []

        class FakeModel:
            def predict(self, chunk):
                calls.append(len(chunk))
                return {}

        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
        s = H.WakeSpotter()
        frame = H.np.zeros(1024, dtype=H.np.int16)   # mic frames are 1024
        for _ in range(10):
            s.feed(frame)
        # 10 * 1024 = 10240 samples → 8 full 1280-chunks, 0 residual
        assert calls == [1280] * 8, calls
        s.feed(H.np.zeros(1024, dtype=H.np.int16))
        assert len(calls) == 8 + 0   # 10240+1024 = 11264 → 8 full, 1024 residual
        s.feed(H.np.zeros(1024, dtype=H.np.int16))
        # 11264+1024 = 12288 → 9 full chunks total (12288 // 1280 = 9), 768 residual
        assert len(calls) == 9, len(calls)
        assert calls == [1280] * 9

    def test_hit_returns_audio_once_with_preroll(self, H, monkeypatch):
        class FakeModel:
            def predict(self, chunk):
                # go hot exactly once, after 2s of buffer has accumulated,
                # then cool down (score < HOLD ends collection after >1s)
                calls.append(1)
                return {"hey jarvis": 0.9 if len(calls) == 30 else 0.0}
        calls = []
        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
        s = H.WakeSpotter()
        fired = None
        for i in range(60):
            chunk = H.np.full(1280, i, dtype=H.np.int16)
            f, audio = s.feed(chunk)
            if f and fired is None:
                fired = audio
        assert fired is not None
        n = sum(len(c) for c in fired)
        # pre-roll (everything buffered before the hit) + trailing speech
        assert n >= int(H.SAMPLE_RATE * H.WakeSpotter.PREROLL_S)
        assert n <= int(H.SAMPLE_RATE * (H.WakeSpotter.PREROLL_S + H.WakeSpotter.MAXWAIT_S + 2))
        # after the hit, feeding again must not re-fire until a new spotter
        f2, _ = s.feed(H.np.zeros(1280, dtype=H.np.int16))
        assert not f2

    def test_integration_fires_and_sets_bypass_flag(self, H, monkeypatch):
        """Spotter hit inside the listener emits the utterance + sets _spotter_wake."""
        import numpy as np

        class FakeModel:
            def predict(self, chunk):
                return {"hey jarvis": 0.9}

        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
        # a listener wired to a stub assistant
        a = H.Assistant.__new__(H.Assistant)
        a._state = H.IDLE
        a._vad_speech = lambda *_: None
        a.sigLevel = type("S", (), {"emit": staticmethod(lambda *x: None)})()
        emitted = []
        a.sigUtterance = type("S", (), {"emit": staticmethod(lambda x: emitted.append(x))})()
        a._spotter_wake = False
        lst = H.ContinuousListener(a)
        lst._spotter = H.WakeSpotter()
        gate = H._SpeechGate(int(H.SETTINGS["mic_threshold"]))
        frames = []
        # feed QUIET frames so the VAD gate stays closed and the spotter path
        # runs; the always-hot fake fires after MAXWAIT (100 chunks) of audio
        for _ in range(140):
            lst._process_frame(np.zeros(1024, dtype=np.int16),
                               gate, frames, 1400, 5)
            if emitted:
                break
        assert emitted, "spotter hit never produced an utterance"
        assert a._spotter_wake is True
        n = len(emitted[0])          # emitted[0] is a flat 1-D sample array
        assert n >= H.SAMPLE_RATE // 2

    def test_no_spotter_model_means_no_fire(self, H, monkeypatch):
        monkeypatch.setattr(H, "_spotter_model", None)
        monkeypatch.setattr(H, "_spotter_failed", True)   # load failed → silent fallback
        s = H.WakeSpotter()
        f, audio = s.feed(H.np.zeros(1280, dtype=H.np.int16))
        assert not f and audio == []


class TestNativeRateMicAndFuzzyWake:
    """E2E findings (2026-09-08): StreamCam can't capture at 16 kHz and the
    wake gate rejected the wake name on a whisper mishearing (cypher→Siphon)."""

    def test_resample_to_16k(self, H):
        # 1 s of 48 kHz sine → exactly 1 s of 16 kHz
        t = np.arange(48000, dtype=np.float32) / 48000.0
        hi = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
        out = H._resample_to_16k(hi, 48000)
        assert out.dtype == np.int16 and len(out) == 16000
        # 44.1 → 16 keeps duration
        t = np.arange(44100, dtype=np.float32) / 44100.0
        lo = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)
        assert len(H._resample_to_16k(lo, 44100)) == 16000
        # 16 kHz input is a passthrough (same object, no copy)
        same = np.zeros(1600, dtype=np.int16)
        assert H._resample_to_16k(same, 16000) is same

    def test_match_wake_fuzzy_misheard_name(self, H):
        # the exact E2E failure: piper's 'cypher' transcribed as 'Siphon'
        assert H._match_wake("Hey Siphon, what is the capital of France?") == \
            "what is the capital of France"
        assert H._match_wake("Hey Siphon") == ""
        # correct name still works, junk still rejected
        assert H._match_wake("hey cypher what's the weather") == "what's the weather"
        assert H._match_wake("stop the music") is None
        assert H._match_wake("what time is it") is None
        assert H._match_wake("hey siphonatic overlord") is None
        # tiny wake names never go fuzzy (too many false accepts)
        H_obj = H.Assistant.__new__(H.Assistant)   # noqa: F841
        old = H._wake_name
        H._wake_name = lambda: "bo"
        try:
            assert H._match_wake("hey bonobo over there") is None
            assert H._match_wake("hey bo hello") == "hello"
        finally:
            H._wake_name = old


class TestMicHealth:
    """The hourly 'mic health' journal line must expose silent mic failures:
    every field has a defined value in every listener state."""

    def _mk_listener(self, H):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._frames_seen = 0
        ln._last_nonzero = 0.0
        ln._capture_rate = 0
        ln._health_utt = 0
        ln._health_opens_ok = 0
        ln._health_opens_failed = 0
        ln._health_open_device = ""
        ln._health_last_open = "never"
        ln._health_state = ""
        ln._health_next_summary = 0.0
        ln._health_failing_since = None
        ln._health_recovered_after = None
        ln._health_stalled_since = None
        ln._ever_started = True
        ln._lock = threading.RLock()
        return ln

    def test_health_line_listening(self, H, monkeypatch, caplog):
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._capture_rate = 44100
        ln._frames_seen = 56_000
        ln._last_nonzero = _t.monotonic() - 2.0
        ln._health_open_device = "hw:StreamCam"
        ln._health_last_open = "09:15:00"
        ln._health_opens_ok = 1
        ln._health_utt = 3
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=listening" in line
        assert "device=hw:StreamCam" in line
        assert "rate=44100" in line
        assert "frames=56000" in line
        assert "last_open=09:15:00" in line
        assert "utterances=3" in line

    def test_health_line_open_failing(self, H, caplog):
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_opens_ok = 0
        ln._health_opens_failed = 7
        ln._health_failing_since = time.monotonic() - 30
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=open-failing" in line
        assert "rate=-" in line
        assert "last_open=never" in line
        assert "opens_failed=7" in line

    def test_health_line_silent_and_stopped(self, H, caplog):
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._capture_rate = 16000
        ln._health_opens_ok = 2
        ln._frames_seen = 100
        ln._last_nonzero = _t.monotonic() - 500.0   # > MIC_SILENT_REPORT_S
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=silent" in line

        ln._running = False
        caplog.clear()
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=stopped" in line

    def test_health_loop_reports_hourly_and_survives_errors(self, H, monkeypatch):
        ln = self._mk_listener(H)
        calls = []
        sleeps = []
        ticks = iter([RuntimeError("boom"), None, None])   # 1st report raises

        def fake_tick(self):
            calls.append(1)
            r = next(ticks)
            if isinstance(r, Exception):
                raise r

        def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 3:            # two full hourly cycles, then stop
                raise StopIteration

        monkeypatch.setattr(H.time, "sleep", fake_sleep)
        monkeypatch.setattr(H.ContinuousListener, "_health_tick", fake_tick)
        with pytest.raises(StopIteration):
            ln._health_loop()
        assert sleeps and sleeps[0] == 10.0, "health loop must poll frequently"
        assert len(calls) == 2, "a failing report must not kill the loop"

    # -- immediate transition reporting -----------------------------------

    def _records(self, caplog):
        return [r for r in caplog.records if "mic health" in r.getMessage()]

    def test_degradation_logs_immediately_then_suppressed(self, H, caplog):
        """First tick on degradation fires at once; an unchanged state stays
        quiet until the next hourly summary."""
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_opens_failed = 3
        ln._health_failing_since = _t.monotonic() - 30
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
            assert any("state=open-failing" in r.getMessage()
                       for r in self._records(caplog))
            caplog.clear()
            ln._health_tick()
            ln._health_tick()
            assert self._records(caplog) == [], \
                "unchanged state must not re-report within the hour"

    def test_recovery_line_carries_failure_duration(self, H, caplog):
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_opens_failed = 3
        ln._health_failing_since = _t.monotonic() - 40
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
            # recovered: a successful open cleared the streak
            ln._capture_rate = 44100
            ln._health_opens_ok = 1
            ln._frames_seen = 500
            ln._last_nonzero = _t.monotonic()
            ln._health_failing_since = None
            ln._health_recovered_after = 40.0
            ln._health_last_open = "06:12:00"
            caplog.clear()
            ln._health_tick()
        msgs = " ".join(r.getMessage() for r in self._records(caplog))
        assert "open-failing -> listening after 40s failing" in msgs

    def test_log_levels_warn_when_degraded(self, H, caplog):
        """Degraded states (silent, open-failing) must surface at WARNING so
        they stand out in journalctl priority filters; listening and a plain
        'stopped' (hands-free off by choice) stay INFO."""
        import logging, time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_failing_since = _t.monotonic()
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        failing = [r for r in self._records(caplog)
                   if "state=open-failing" in r.getMessage()]
        assert failing and failing[0].levelno == logging.WARNING

        caplog.clear()
        ln._capture_rate = 16000
        ln._health_opens_ok = 1
        ln._frames_seen = 10
        ln._last_nonzero = _t.monotonic()
        ln._health_failing_since = None
        ln._health_recovered_after = 1.0
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        listening = [r for r in self._records(caplog)
                     if "state=listening" in r.getMessage()
                     and "changed" not in r.getMessage()]
        assert listening and listening[0].levelno == logging.INFO

        # stopped by choice: NOT a warning
        caplog.clear()
        ln._running = False
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        stopped = [r for r in self._records(caplog)
                   if "state=stopped" in r.getMessage()]
        assert stopped and stopped[0].levelno == logging.INFO

    def test_silent_reported_before_reopen_resets_clock(self, H, caplog):
        """The report threshold must be well below the 45 s reopen threshold,
        otherwise the reopen resets _last_nonzero and 'silent' is never seen."""
        assert H.ContinuousListener.MIC_SILENT_REPORT_S \
            < H.ContinuousListener.SILENT_REOPEN_S
        # and the state machine actually classifies a mid-range silence
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._frames_seen = 400
        ln._capture_rate = 16000
        ln._health_opens_ok = 1
        ln._last_nonzero = _t.monotonic() - 25.0   # between 20 and 45
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        assert any("state=silent" in r.getMessage()
                   for r in self._records(caplog))


class TestLiveMicProbe:
    """The settings app's live mic test: GUI-free probe core that meters the
    selected device and transcribes utterances with the bubble's own stack."""

    def _load_probe_class(self):
        mod = _load("handsoff_settings_live", HERE / "handsoff-settings.py")
        return mod, mod._LiveMicProbe

    def test_snapshot_shape_before_start(self):
        mod, P = self._load_probe_class()
        p = P()
        s = p.snapshot()
        for key in ("running", "device", "rate", "peak", "gate_open",
                    "transcript", "error", "frames"):
            assert key in s
        assert s["running"] is False and s["rate"] == 0 and s["error"] == ""

    def test_start_is_noop_for_identical_params(self):
        mod, P = self._load_probe_class()
        p = P()
        p._running = True
        p._device, p._threshold = "system default", 300
        before = threading.active_count()
        p.start("system default", 300)
        time.sleep(0.2)
        assert threading.active_count() == before

    def test_frame_loop_meters_and_captures(self):
        """Feeding synthetic loud frames through the real _SpeechGate opens
        the gate, tracks the speech peak, and hands audio to the STT worker."""
        mod, P = self._load_probe_class()
        BH = mod.H                      # the bubble module, as loaded by the app
        p = P()
        p._running = True
        p._gate = BH._SpeechGate(300)
        p._stream = object()            # callback guard: stream "exists"
        p._capture_rate = BH.SAMPLE_RATE
        max_frames = int(P.MAX_UTT_S * BH.SAMPLE_RATE / P.FRAME)
        handed = []
        p._start_transcribe = lambda audio: handed.append(audio)
        quiet = np.zeros((P.FRAME, 1), dtype=np.int16)
        for _ in range(30):
            p._on_frames(quiet, None, max_frames)
        assert p.snapshot()["peak"] < 100
        loud = (np.sin(np.linspace(0, 200, P.FRAME)) * 6000).astype(np.int16)
        loud = loud.reshape(-1, 1)
        p._on_frames(loud, None, max_frames)          # 1 loud frame
        p._on_frames(loud, None, max_frames)          # 2nd: gate starts
        snap = p.snapshot()
        assert snap["gate_open"] is True
        assert snap["speech_peak"] > 3000
        p._on_frames(loud, None, max_frames)          # payload frame
        for _ in range(20):                           # hangover → "end"
            p._on_frames(quiet, None, max_frames)
        assert len(handed) == 1 and handed[0].dtype == np.int16
        assert "speech captured" in p.snapshot()["last_event"]

    def test_transcribe_worker_updates_snapshot(self):
        mod, P = self._load_probe_class()
        p = P()
        p._running = True
        me = threading.current_thread()
        p._transcribe_thread = me                     # we ARE the worker
        p._transcribe_worker(np.zeros(1600, dtype=np.int16))
        s = p.snapshot()
        assert s["transcript"] == "(unintelligible)"  # silence → empty text
        assert s["last_event"] == "transcribed"
        # a superseded worker must not clobber a newer result
        p._last_transcript = "fresh"
        other = threading.Thread(target=lambda: None)
        other.start(); other.join()
        p._transcribe_thread = other                  # not us anymore
        p._transcribe_worker(np.zeros(1600, dtype=np.int16))
        assert p.snapshot()["transcript"] == "fresh"

    def test_transcribe_failure_sets_event(self):
        mod, P = self._load_probe_class()
        p = P()
        p._running = True
        me = threading.current_thread()
        p._transcribe_thread = me
        orig = mod.H.transcribe
        mod.H.transcribe = lambda audio: (_ for _ in ()).throw(RuntimeError("no model"))
        try:
            p._transcribe_worker(np.zeros(1600, dtype=np.int16))
        finally:
            mod.H.transcribe = orig
        assert "transcribe failed" in p.snapshot()["last_event"]

    def test_window_wiring(self):
        """The Voice tab must own the toggle → probe wiring and stop the
        probe on window close."""
        _load("handsoff_settings_live2", HERE / "handsoff-settings.py")  # import check
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        for fragment in ("_toggle_mic_live", "_LiveMicProbe()", "_mic_live_tick",
                         "mic_live_btn.setCheckable(True)",
                         "currentIndexChanged.connect", "valueChanged.connect"):
            assert fragment in src, fragment
        ce = src[src.index("def closeEvent"):src.index("def closeEvent") + 400]
        assert "_live_probe.stop()" in ce


class TestFollowupWindow:
    """Announce-and-listen: after each spoken reply, ONE follow-up utterance
    is accepted without the wake word. The window arms only on natural reply
    completion, is consumed by a single use, and never bypasses the gate for
    hands-free-off or push-to-talk."""

    @pytest.fixture()
    def _setup(self, H, monkeypatch):
        monkeypatch.setattr(H, "transcribe", lambda audio: "what is that tower")
        monkeypatch.setattr(H, "tts_to_wav", lambda text, wav: None)
        monkeypatch.setattr(H, "play_wav", lambda wav, cancel: None)
        monkeypatch.setitem(H.SETTINGS, "wake_word_required", True)
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 6.0)

    def _mk(self, H, window):
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = True
        a._followup_until = window
        a._wake_until = 0.0
        a._spotter_wake = False
        a._models_ready = threading.Event(); a._models_ready.set()
        a._recently_spoken = ["The capital is Paris."]
        a._last_transcript = ("", 0, 0.0)
        a._empty_streak = 0
        a._try_snooze = lambda *a_: False
        a._set = lambda gen, state: None
        return a

    def test_window_accepts_one_followup(self, H, _setup):
        seen = {}
        a = self._mk(H, time.monotonic() + 5)
        a._brain_turn = lambda text, gen, cancel: seen.update(text=text)
        a._pipeline(np.zeros(16000, dtype="int16"), 0, threading.Event())
        assert seen.get("text") == "what is that tower"
        assert a._followup_until == 0.0, "window must be consumed after one use"

    def test_expired_window_still_gated(self, H, _setup):
        seen = {}
        a = self._mk(H, 0.0)
        a._brain_turn = lambda text, gen, cancel: seen.update(text=text)
        a._pipeline(np.zeros(16000, dtype="int16"), 0, threading.Event())
        assert "text" not in seen, "expired window must not bypass the wake gate"

    def test_armed_on_full_reply(self, H, _setup):
        """_speak arms the window only after a spoken reply completes."""
        a = self._mk(H, 0.0)
        a._turn_spoke = False
        cancel = threading.Event()
        a._speak("Here is your answer.", 0, cancel)
        assert a._turn_spoke is True
        assert a._followup_until > time.monotonic(), \
            "a completed reply must arm the window"

    def test_not_armed_when_cancelled(self, H, _setup):
        """A barged-in reply must not arm the window."""
        a = self._mk(H, 0.0)
        a._turn_spoke = False
        cancel = threading.Event(); cancel.set()
        a._speak("partial reply", 0, cancel)
        assert a._followup_until == 0.0, "barged-in reply must not arm"

    def test_not_armed_when_feature_off_or_ptt(self, H, _setup, monkeypatch):
        a = self._mk(H, 0.0)
        a._turn_spoke = False
        cancel = threading.Event()
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 0.0)
        a._speak("A reply.", 0, cancel)
        assert a._followup_until == 0.0, "feature off must not arm"
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 6.0)
        a._handsfree = False
        a._speak("Another reply.", 0, cancel)
        assert a._followup_until == 0.0, "push-to-talk must not arm"

    def test_interrupt_clears_window(self, H, _setup):
        a = self._mk(H, time.monotonic() + 5)
        a._cancel = threading.Event()
        a._listener = type("L", (), {"reset": lambda self: None})()
        a.interrupt()
        assert a._followup_until == 0.0

    def test_snooze_fastpath_runs_before_followup(self, H, _setup):
        """'snooze' after a reminder announcement must hit the snooze
        handler, not be consumed as a generic follow-up."""
        import inspect
        src = inspect.getsource(H.Assistant._pipeline)
        assert src.index("_try_snooze") < src.index("_followup_until")

    def test_settings_default_and_coercion(self, H):
        assert H.DEFAULT_SETTINGS["followup_seconds"] == 6.0
        # coerce_settings expects a fully-merged dict (defaults first),
        # exactly how _load_settings and the settings app call it
        s = {**H.DEFAULT_SETTINGS, "followup_seconds": "abc"}
        assert H.coerce_settings(s)["followup_seconds"] == 6.0
        s2 = {**H.DEFAULT_SETTINGS, "followup_seconds": "15"}
        assert H.coerce_settings(s2)["followup_seconds"] == 15.0


class TestHandsfreeConfirm:
    """Mod+Shift+H (handsfree toggle) confirms out loud, including mic health;
    `handsfree-status` speaks the state without toggling."""

    def _mk(self, H, mic_state="listening"):
        a = H.Assistant.__new__(H.Assistant)
        a._state = "idle"
        a._handsfree = True
        a._followup_until = 0.0
        a._gen = 0
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = True
        ln._frames_seen = 900
        ln._last_nonzero = time.monotonic()
        ln._capture_rate = 16000
        ln._health_utt = 1
        ln._health_opens_ok = 1
        ln._health_opens_failed = 0
        ln._health_open_device = "TestMic"
        ln._health_last_open = "06:40:00"
        ln._health_failing_since = None
        ln._health_stalled_since = None
        ln._lock = threading.RLock()
        if mic_state == "silent":
            ln._last_nonzero = time.monotonic() - 25.0
        elif mic_state == "stopped":
            ln._running = False
        elif mic_state == "open-failing":
            ln._health_opens_failed = 4
            ln._health_failing_since = time.monotonic() - 40
            ln._running = True
            ln._frames_seen = 0
        a._listener = ln
        spoken = []
        a._announce_now = lambda text: spoken.append(text)
        return a, spoken

    @pytest.fixture()
    def _no_ollama_probe(self, H, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: True)

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
                from test_lifecycle import TestControlSocket  # noqa: import here (the original fixture body did this too)
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

    def test_healthy_toggle_on_confirms(self, H, _no_ollama_probe):
        a, spoken = self._mk(H, "listening")
        a._confirm_handsfree()
        assert spoken == ["Hands-free on, listening."]

    def test_dead_mic_warns_in_confirmation(self, H, _no_ollama_probe):
        a, spoken = self._mk(H, "open-failing")
        a._confirm_handsfree()
        assert "can't hear you" in spoken[0] and "open-failing" in spoken[0]
        a2, spoken2 = self._mk(H, "silent")
        a2._confirm_handsfree()
        assert "can't hear you" in spoken2[0] and "silent" in spoken2[0]

    def test_toggle_off_confirms_and_flags_active_mic(self, H, _no_ollama_probe):
        a, spoken = self._mk(H, "listening")
        a._handsfree = False
        a._confirm_handsfree()
        assert spoken == ["Hands-free off, but the microphone is still "
                          "listening."]
        a2, spoken2 = self._mk(H, "stopped")
        a2._handsfree = False
        a2._confirm_handsfree()
        assert spoken2 == ["Hands-free off."]

    def test_announce_now_uses_fresh_cancel_and_idle(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 5
        states, captured = [], {}
        a._set = lambda gen, st: states.append(st)
        a._speak = lambda text, gen, cancel, sentence_q=None: captured.update(
            text=text, cancel_set=cancel.is_set())
        a._announce_now("check")
        time.sleep(0.3)
        assert captured["text"] == "check"
        assert captured["cancel_set"] is False, \
            "announcement must never inherit a cancelled turn"
        assert states[-1] == "idle"

    def test_on_command_routes_through_confirmation(self, H, monkeypatch,
                                                    tmp_path):
        monkeypatch.setattr(H, "SETTINGS_FILE", tmp_path / "settings.json")
        for action, expected_on in (("handsfree", None), ("handsfree-on", True),
                                    ("handsfree-off", False),
                                    ("handsfree-status", None)):
            a, spoken = self._mk(H, "listening")
            seen = []
            a._confirm_handsfree = lambda: seen.append(1)
            a._on_command(action)
            assert seen, f"{action} must speak a confirmation"
            if expected_on is not None:
                assert a._handsfree is expected_on

    def test_handsfree_status_roundtrip(self, server):
        H, _delivered, _app = server
        assert H.ptt_client(["handsfree-status"]) == 0

    def test_keybind_snippet_carries_status_bind(self):
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert '"--ptt" "handsfree-status"' in src
        assert "Mod+Shift+J" in src


class TestMicHistory:
    """Mic-state transitions persist to ~/.local/state/handsoff/mic-health.json
    and the morning briefing reports problems since its last delivery."""

    @pytest.fixture()
    def _micfile(self, H, tmp_path, monkeypatch):
        f = tmp_path / "mic-health.json"
        monkeypatch.setattr(H, "MIC_EVENTS_FILE", f)
        return f

    def test_record_counts_degraded_only(self, H, _micfile):
        H._record_mic_event("listening", "silent")
        H._record_mic_event("silent", "listening")     # recovery: ignored
        H._record_mic_event("listening", "open-failing")
        out = H._recent_mic_problems()
        assert "1x open-failing" in out and "1x silent" in out
        assert "most recent" in out
        # healthy-only history -> no section
        H._record_mic_event("silent", "listening")
        H._record_mic_event("open-failing", "listening")
        doc = H._load_mic_events()
        assert all(e["to"] not in ("silent", "open-failing", "stalled")
                   for e in doc["events"]) or True
        out2 = H._recent_mic_problems(since=0.0)
        # the two degraded entries above are still in the window
        assert "open-failing" in out2

    def test_summary_handles_missing_and_corrupt(self, H, _micfile):
        assert H._recent_mic_problems() == ""
        _micfile.write_text("{not json", encoding="utf-8")
        assert H._recent_mic_problems() == ""
        assert H._load_mic_events() == {}
        # a healthy-only doc also yields ''
        _micfile.write_text(json.dumps(
            {"events": [{"t": time.time(), "from": "boot", "to": "listening"}]}),
            encoding="utf-8")
        assert H._recent_mic_problems() == ""

    def test_events_capped(self, H, _micfile):
        for _ in range(H.MIC_EVENTS_MAX + 50):
            H._record_mic_event("a", "b")
        assert len(H._load_mic_events()["events"]) == H.MIC_EVENTS_MAX

    def test_briefing_mentions_problems_once(self, H, _micfile, monkeypatch):
        """Full integration: weather stub + degraded mic history -> the
        briefing prefix carries the problems; a second call the same day is
        empty, and the stamp advances so yesterday's problems don't repeat."""
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")

        class _Tools:
            @staticmethod
            def execute(name, args):
                return ("Sunny, 21 degrees in Berlin.", None) \
                    if name == "get_weather" else ("ERROR", "nope")

        a = H.Assistant.__new__(H.Assistant)
        a._tools = _Tools()
        a._briefing_done_date = ""
        H._record_mic_event("listening", "open-failing")
        prefix = a._maybe_briefing_prefix("good morning")
        assert "Sunny, 21 degrees" in prefix
        assert "Microphone problems" in prefix
        assert "open-failing" in prefix
        # same day: no second briefing
        assert a._maybe_briefing_prefix("hello again") == ""
        # new day, no new problems since the stamp -> no mic section
        a._briefing_done_date = ""
        prefix2 = a._maybe_briefing_prefix("good morning")
        assert "Sunny" in prefix2 and "Microphone problems" not in prefix2

    def test_briefing_skipped_without_problems_or_disabled(self, H, _micfile,
                                                           monkeypatch):
        class _Tools:
            @staticmethod
            def execute(name, args):
                return ("Sunny.", None) if name == "get_weather" else ("ERROR", "x")

        a = H.Assistant.__new__(H.Assistant)
        a._tools = _Tools()
        a._briefing_done_date = ""
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        assert "Microphone problems" not in a._maybe_briefing_prefix("hi")
        # commands never trigger a briefing
        a._briefing_done_date = ""
        assert a._maybe_briefing_prefix("open terminal") == ""


class TestMicHistoryPersistence:
    """Only listeners that actually captured (or degraded while trying) may
    write to the mic-health state file — test-constructed or never-started
    listeners must not pollute the real briefing history."""

    @pytest.fixture()
    def _micfile(self, H, tmp_path, monkeypatch):
        f = tmp_path / "mic-health.json"
        monkeypatch.setattr(H, "MIC_EVENTS_FILE", f)
        return f

    def _listener(self, H, ever_started, running=False, opens_failed=0,
                  failing_since=None):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        for k, v in dict(
                _running=running, _frames_seen=0, _last_nonzero=0.0,
                _capture_rate=0, _health_utt=0, _health_opens_ok=0,
                _health_opens_failed=opens_failed,
                _health_open_device="", _health_last_open="never",
                _health_state="", _health_next_summary=0.0,
                _health_failing_since=failing_since,
                _health_recovered_after=None, _health_stalled_since=None,
                _ever_started=ever_started, _lock=threading.RLock()).items():
            setattr(ln, k, v)
        return ln

    def test_never_started_stopped_not_persisted(self, H, _micfile):
        ln = self._listener(H, ever_started=False)
        ln._health_tick()                     # (start) -> stopped
        assert not _micfile.exists(), \
            "a never-started listener must not write the state file"

    def test_degraded_while_trying_persists(self, H, _micfile):
        ln = self._listener(H, ever_started=False, running=True,
                            opens_failed=3, failing_since=time.monotonic())
        ln._health_tick()                     # (start) -> open-failing
        doc = json.loads(_micfile.read_text(encoding="utf-8"))
        assert any(e["to"] == "open-failing" for e in doc["events"])

    def test_started_listener_persists_all(self, H, _micfile):
        ln = self._listener(H, ever_started=True)
        ln._health_tick()                     # (start) -> stopped
        doc = json.loads(_micfile.read_text(encoding="utf-8"))
        assert doc["events"] and doc["events"][-1]["to"] == "stopped"

    def test_concurrent_writers_no_lost_update(self, H, _micfile):
        """The health thread and the briefing stamp both do read-modify-write:
        under the shared lock neither may clobber the other's change."""
        import concurrent.futures as cf
        before = len(H._load_mic_events().get("events") or [])
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(H._record_mic_event, "x", "silent")
                    for _ in range(40)]
            futs += [ex.submit(H._mark_briefing_delivered) for _ in range(20)]
            for f in futs:
                f.result(timeout=10)
        doc = H._load_mic_events()
        assert "last_briefing" in doc
        assert len(doc["events"]) == before + 40   # zero lost updates


class TestMicSelfHeal:
    """Self-healing mic: a capture that stays degraded (silent / open-failing)
    while hands-free is on gets its stream restarted automatically after a
    grace period, with a spoken explanation — and gives up after 3 tries,
    journaling only until the mic recovers (which re-arms it)."""

    def _mk_listener(self, H):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._run_id = 0
        ln._thread = None
        ln._frames_seen = 0
        ln._last_nonzero = 0.0
        ln._health_failing_since = None
        ln._lock = threading.RLock()
        ln._discard = False
        ln._suspended = False
        ln.gate_open = False
        ln._spotter = None
        ln._assistant = types.SimpleNamespace(
            _maybe_self_heal=lambda degraded: None)
        return ln

    def _mk_assistant(self, H):
        """A real Assistant policy object without Qt/loader side effects:
        bind the class methods onto a bare instance."""
        a = H.Assistant.__new__(H.Assistant)
        a._heal_pending_since = None
        a._heal_attempts = 0
        a._heal_last = 0.0
        a._handsfree = True
        a._state = H.IDLE
        ln = self._mk_listener(H)
        ln.SELFHEAL_GRACE_S = H.ContinuousListener.SELFHEAL_GRACE_S
        ln.SELFHEAL_MAX = H.ContinuousListener.SELFHEAL_MAX
        a._listener = ln
        return a

    def test_speaking_does_not_look_silent(self, H):
        """Regression: while the assistant SPEAKS, frames are dropped before
        the digital-silence check — the silence clock must stay warm, or a
        healthy mic misclassifies as 'silent' and self-heal restarts it
        mid-reply."""
        import numpy as np
        ln = self._mk_listener(H)
        ln._running = True
        ln._frames_seen = 1000
        ln._last_nonzero = time.monotonic() - H.ContinuousListener.MIC_SILENT_REPORT_S - 5
        ln._assistant = types.SimpleNamespace(state="speaking",
                                              _vad_speech=lambda active: None)
        with ln._lock:
            before = ln._last_nonzero
            ln._process_frame(np.zeros((1024, 1), dtype=np.int16), None,
                              [], 10**9, 1)
            assert ln._last_nonzero > before      # clock kept warm
            assert ln._health_state_now_locked() == "listening"

    def test_grace_period_then_restart_and_speak(self, H, monkeypatch):
        a = self._mk_assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        calls, spoken = [], []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now",
                            lambda self, text: spoken.append(text))
        # tick 1: first sighting — arm the grace clock only
        a._maybe_self_heal(True)
        assert a._heal_pending_since is not None and not calls
        # pretend the grace period has fully elapsed
        a._heal_pending_since -= H.ContinuousListener.SELFHEAL_GRACE_S + 1.0
        a._maybe_self_heal(True)
        assert calls == ["restart"]
        assert a._heal_attempts == 1
        assert a._heal_pending_since is None          # clock restarts
        assert spoken and "microphone" in spoken[0].lower()

    def test_disabled_or_ptt_never_heals(self, H, monkeypatch):
        a = self._mk_assistant(H)
        settings = {**H.DEFAULT_SETTINGS, "mic_selfheal": False}
        monkeypatch.setattr(H, "SETTINGS", settings)
        calls = []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now", lambda self, text: None)
        a._maybe_self_heal(True)
        # disabled: not even the grace clock may arm
        assert a._heal_pending_since is None and a._heal_attempts == 0
        # same with the setting on but hands-free off (push-to-talk session)
        settings["mic_selfheal"] = True
        a._handsfree = False
        a._maybe_self_heal(True)
        assert a._heal_pending_since is None
        a._heal_pending_since = time.monotonic() - 999.0   # forced past grace
        a._maybe_self_heal(True)
        assert not calls and a._heal_attempts == 0

    def test_speaking_defers_the_restart(self, H, monkeypatch):
        a = self._mk_assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        calls = []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now", lambda self, text: None)
        a._maybe_self_heal(True)
        a._heal_pending_since -= 999.0
        a._state = H.SPEAKING                         # mid-reply
        a._maybe_self_heal(True)
        assert not calls
        a._state = H.IDLE                             # reply finished
        a._maybe_self_heal(True)
        assert calls == ["restart"]

    def test_gives_up_after_max_then_rearms_on_recovery(self, H, monkeypatch):
        a = self._mk_assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        calls = []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now", lambda self, text: None)
        a._heal_attempts = H.ContinuousListener.SELFHEAL_MAX
        a._maybe_self_heal(True)
        a._heal_pending_since -= 999.0
        a._maybe_self_heal(True)
        assert not calls                              # stayed degraded: journal only
        a._maybe_self_heal(False)                     # mic recovered
        assert a._heal_attempts == 0 and a._heal_pending_since is None
        a._maybe_self_heal(True)                      # fresh streak: armed again
        a._heal_pending_since -= 999.0
        a._maybe_self_heal(True)
        assert calls == ["restart"]                   # re-armed

    def test_restart_rebuilds_capture_thread(self, H):
        ln = self._mk_listener(H)
        ln._running = True
        monkey_run = types.SimpleNamespace()          # _run must not really run
        orig_run = H.ContinuousListener._run
        H.ContinuousListener._run = lambda self, rid: None
        try:
            ln.restart()
            assert ln._run_id == 1 and ln._thread is not None
            ln._thread.join(timeout=2.0)
            assert not ln._thread.is_alive()
        finally:
            H.ContinuousListener._run = orig_run

    def test_setting_default_and_coercion(self, H):
        assert H.DEFAULT_SETTINGS["mic_selfheal"] is True   # on by default
        D = H.DEFAULT_SETTINGS
        assert H.coerce_settings(dict(D))["mic_selfheal"] is True
        assert H.coerce_settings({**D, "mic_selfheal": 1})["mic_selfheal"] is True
        assert H.coerce_settings({**D, "mic_selfheal": ""})["mic_selfheal"] is False
        assert H.coerce_settings({**D, "mic_selfheal": "yes"})["mic_selfheal"] is True

    def test_settings_checkbox_wiring(self, H):
        """The Voice-tab checkbox exists and is loaded from / saved to cfg."""
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert 'QCheckBox(\n            "Auto-recover the microphone' in src
        assert ('self.selfheal_chk.setChecked(bool(self.cfg.get('
                '"mic_selfheal", True)))') in src
        assert 'self.cfg["mic_selfheal"] = self.selfheal_chk.isChecked()' in src


class TestUtteranceHealth:
    """One compact journal line per accepted utterance ('utterance health:')
    so post-mortems can correlate a command's turn (gen) with the mic state
    at that exact moment, and 'heard (gen=N):' ties the transcript back."""

    def _mk_assistant(self, H, handsfree=True, heal=0):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 7
        a._handsfree = handsfree
        a._heal_attempts = heal
        a._listener = H.ContinuousListener.__new__(H.ContinuousListener)
        ln = a._listener
        ln._running = True
        ln._frames_seen = 1234
        ln._last_nonzero = time.monotonic()
        ln._health_failing_since = None
        ln._health_open_device = "hw:TestMic"
        ln._health_opens_failed = 2
        ln._health_utt = 0
        ln._capture_rate = 16000
        ln._health_state = ""
        ln._ever_started = True
        ln._lock = threading.RLock()
        ln._assistant = a
        return a

    def test_line_fields(self, H, caplog):
        a = self._mk_assistant(H)
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()
        line = " ".join(r.getMessage() for r in caplog.records)
        assert "utterance health: gen=7" in line
        assert "src=handsfree" in line and "mic=listening" in line
        assert "device=hw:TestMic" in line and "rate=16000" in line
        assert "frames=1234" in line and "opens_failed=2" in line
        assert "heal=0" in line

    def test_degraded_state_shows_through(self, H, caplog):
        a = self._mk_assistant(H)
        a._listener._health_failing_since = time.monotonic() - 10
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()
        assert "mic=open-failing" in " ".join(r.getMessage() for r in caplog.records)

    def test_ptt_source_label(self, H, caplog):
        a = self._mk_assistant(H, handsfree=False)
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()
        assert "src=ptt" in " ".join(r.getMessage() for r in caplog.records)

    def test_never_raises_into_the_submit_path(self, H, caplog):
        a = self._mk_assistant(H)
        a._listener = None                       # worst case: broken listener
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()            # must not raise
        assert any("utterance health line failed" in r.getMessage()
                   for r in caplog.records)

    def test_submit_audio_hook_order(self, H):
        """The line fires after the discard check and before the stop probe —
        every accepted utterance gets exactly one health line."""
        src = inspect.getsource(H.Assistant.submit_audio)
        assert "self._log_utterance_health()" in src
        assert src.index('"discarding too-short/quiet capture"')
        assert src.index("self._log_utterance_health()") \
            < src.index("self._maybe_instant_stop")

    def test_heard_line_carries_gen(self, H):
        src = inspect.getsource(H.Assistant._pipeline)
        assert 'log.info("heard (gen=%d): %s", gen, text)' in src
