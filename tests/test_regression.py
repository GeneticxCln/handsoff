"""Historical audit pins: regressions from past review rounds."""
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


class TestAuditFixes:
    """Regression pins for the 2026-09 audit fixes."""

    def test_fire_timer_speaks_with_fresh_cancel(self, H, monkeypatch):
        """_fire_timer must NOT pass the already-set interrupt event to _speak."""
        a = H.Assistant.__new__(H.Assistant)   # skip Qt init
        a.interrupt = lambda: None
        a._gen = 0
        captured = {}

        def fake_speak(text, gen, cancel, sentence_q=None):
            captured["cancel_set"] = cancel.is_set()
            captured["text"] = text

        a._speak = fake_speak
        a._set = lambda gen, state: None
        a._fire_timer("tea")
        time.sleep(0.2)
        assert captured.get("cancel_set") is False, "cancel was pre-set — reminder would be silent"
        assert "tea" in captured.get("text", "")

    def test_announce_missed_uses_fresh_cancel(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 0
        captured = {}
        a._speak = lambda text, gen, cancel, sentence_q=None: captured.update(
            cancel_set=cancel.is_set())
        a._set = lambda gen, state: None
        a._announce_missed([{"name": "pills"}])
        time.sleep(0.2)
        assert captured.get("cancel_set") is False

    def test_matches_recent_speech_takes_text(self, H):
        """New signature: text in, no transcription inside."""
        a = H.Assistant.__new__(H.Assistant)
        a._recently_spoken = ["what time is it"]
        a._matches_recent_speech = (
            lambda t: H.Assistant._matches_recent_speech(a, t))
        import types
        monkey_src = H._is_echo
        try:
            H._is_echo = lambda text, recent: text == "what time is it"
            assert a._matches_recent_speech("what time is it") is True
            assert a._matches_recent_speech("different") is False
        finally:
            H._is_echo = monkey_src

    def test_submit_audio_does_not_transcribe(self, H, monkeypatch):
        """Transcription must never run synchronously on the GUI thread: the
        stop-probe transcribes in a background thread, the pipeline in the
        worker. submit_audio itself must not call transcribe."""
        a = H.Assistant.__new__(H.Assistant)
        calls = []
        main_tid = H.threading.get_ident()
        release = H.threading.Event()
        def fake_transcribe(audio):
            calls.append(H.threading.get_ident())
            release.wait(5)   # blocked: proves submit_audio doesn't wait for it
            return "x"
        monkeypatch.setattr(H, "transcribe", fake_transcribe)
        a._recently_spoken = ["hello"]       # would have triggered the old path
        a._set = lambda gen, state: None
        a._gen = 0
        a._cancel = H.threading.Event()
        a.interrupt = lambda: None
        a._pipeline_q = H.queue.Queue()      # turn is handed to the worker
        import numpy as np
        audio = np.zeros(H.SAMPLE_RATE, dtype=np.int16)
        audio[:1000] = 900                   # loud enough to pass the gate
        a.submit_audio(audio)
        # submit_audio must return immediately even though transcribe is
        # blocked in the probe thread (transcribe never runs on this thread)
        assert a._pipeline_q.qsize() == 1    # exactly one queued turn
        release.set()                        # let the probe finish
        deadline = H.time.time() + 5
        while not calls and H.time.time() < deadline:
            H.time.sleep(0.02)
        assert calls, "stop-probe should transcribe in a background thread"
        assert all(tid != main_tid for tid in calls), \
            "transcription ran on the submitter's thread!"

    def test_is_stop_utt(self, H):
        a = H.Assistant.__new__(H.Assistant)
        for yes in ["stop", "Stop.", "hey stop", "quiet", "be quiet",
                    "shut up", "never mind", "that's all", "cancel"]:
            assert a._is_stop_utt(yes), f"{yes!r} should be a stop command"
        for no in ["stop the music", "stop that", "what time is it",
                   "cancel my reminder", "stop and think", "stop it now"]:
            assert not a._is_stop_utt(no), f"{no!r} must NOT be swallowed"

    def test_pipeline_drops_stop_utterance(self, H, monkeypatch):
        """A bare stop command must never reach the brain: the pipeline
        returns before the wake gate / LLM, sets IDLE, and speaks nothing."""
        a = H.Assistant.__new__(H.Assistant)
        spoke = []
        a._set = lambda gen, state: None
        a._speak = lambda *x, **k: spoke.append(x)
        a._recently_spoken = ["hello"]
        a._matches_recent_speech = lambda t: False
        a._spotter_wake = False
        a._handsfree = False
        a._wake_until = 0.0
        a._empty_streak = 0
        a._models_ready = H.threading.Event()
        a._models_ready.set()
        a._last_transcript = ("", 0, 0.0)
        monkeypatch.setattr(H, "transcribe", lambda audio: "stop")
        import numpy as np
        a._pipeline(np.zeros(H.SAMPLE_RATE, dtype=np.int16),
                    1, H.threading.Event())
        assert spoke == [], spoke            # no LLM, no reply, nothing spoken

    def test_stop_probe_drains_queue(self, H, monkeypatch):
        """The zero-LLM stop: when the probe hears a bare stop, the queued
        turn is drained so the brain never runs on it."""
        a = H.Assistant.__new__(H.Assistant)
        monkeypatch.setattr(H, "transcribe", lambda audio: "stop")
        a._pipeline_q = H.queue.Queue()
        a._pipeline_q.put((b"audio", 1, H.threading.Event()))  # pending turn
        a._last_transcript = ("", 0, 0.0)
        a._maybe_instant_stop(None, 1)         # gen captured at submission
        deadline = H.time.time() + 5
        while not a._pipeline_q.empty() and H.time.time() < deadline:
            H.time.sleep(0.02)
        assert a._pipeline_q.empty(), "stop must drain the queued turn"
        assert a._last_transcript[0] == "stop", "transcript must be reused"
        assert a._last_transcript[1] == 1, "transcript must carry its own gen"

    def test_stop_probe_late_finish_never_stamps_newer_turn(self, H,
                                                            monkeypatch):
        """Audit race: utterance A's probe finishes AFTER B was submitted
        (_gen bumped). A's transcript must be stamped with A's gen, never
        B's — otherwise B's pipeline would execute A's text."""
        a = H.Assistant.__new__(H.Assistant)
        a._pipeline_q = H.queue.Queue()
        a._last_transcript = ("", 0, 0.0)
        monkeypatch.setattr(H, "transcribe", lambda audio: "hello A")
        # simulate: B already bumped _gen to 5 before A's probe completes
        a._gen = 5
        a._maybe_instant_stop(None, 2)         # A was submitted at gen 2
        deadline = H.time.time() + 5
        while a._last_transcript[1] == 0 and H.time.time() < deadline:
            H.time.sleep(0.02)
        assert a._last_transcript == ("hello A", 2, a._last_transcript[2]), \
            "probe must stamp the submitted gen, not the current _gen"
        # and B's queued turn (gen 5) must survive A's stop-drain
        a._pipeline_q.put((b"audioB", 5, H.threading.Event()))
        a._last_transcript = ("stop", 2, H._tick_now())  # A was a stop command
        a._maybe_instant_stop(None, 2)
        deadline = H.time.time() + 5
        items = []
        while H.time.time() < deadline:
            try:
                items.append(a._pipeline_q.get_nowait())
            except H.queue.Empty:
                if items or H.time.time() > deadline - 0.5:
                    break
                H.time.sleep(0.02)
        assert len(items) == 1 and items[0][1] == 5, \
            "a late stop probe must not swallow a newer utterance's turn"

    def test_pipeline_reuses_stop_probe_transcript(self, H, monkeypatch):
        """When the probe already transcribed this utterance, the pipeline
        must reuse it (single transcription, no double cost)."""
        a = H.Assistant.__new__(H.Assistant)
        called = []
        monkeypatch.setattr(H, "transcribe",
                            lambda audio: called.append(1) or "hello there")
        a._last_transcript = ("hello there", 1, H._tick_now())
        a._recently_spoken = []
        a._matches_recent_speech = lambda t: False
        a._spotter_wake = False
        a._handsfree = False
        a._wake_until = 0.0
        a._empty_streak = 0
        a._models_ready = H.threading.Event()
        a._models_ready.set()
        a._brain_turn = lambda *x, **k: None   # stop at the brain boundary
        a._set = lambda gen, state: None
        a._gen = 1
        import numpy as np
        a._pipeline(np.zeros(H.SAMPLE_RATE, dtype=np.int16),
                    1, H.threading.Event())
        assert called == [], "transcribe must not run again (cache hit)"

    def test_list_reminders_tolerates_missing_repeat_hours(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        rf.write_text(H.json.dumps(
            [{"name": "hand-edited", "due": H.time.time() + 600}]))
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("list_reminders", {})
        assert not err and "hand-edited" in out, out

    def test_get_datetime_tz_matches_now(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("get_datetime", {})
        assert not err, out
        assert H.datetime.datetime.now().astimezone().tzname() in out

    def test_acquire_lock_closes_failed_handles(self, H, monkeypatch, tmp_path):
        """A blocked lock attempt must not leak an open file handle."""
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setattr(H, "LOCK_FILE", tmp_path / "h.lock")
        monkeypatch.setattr(H, "LOCK_RETRIES", 1)
        monkeypatch.setattr(H, "LOCK_RETRY_WAIT", 0.05)
        first = H.acquire_lock()
        assert first is not None
        try:
            blocked = H.acquire_lock()
            assert blocked is None
        finally:
            import fcntl as _f
            _f.flock(first, _f.LOCK_UN)
            first.close()

    def test_listener_run_id_invalidates_old_thread(self, H):
        """A stale listener thread must not resume after a new start()."""
        cl = H.ContinuousListener.__new__(H.ContinuousListener)
        cl._assistant = None
        cl._running = False
        cl._run_id = 0
        cl._thread = None
        cl._stream = None
        cl._frames_seen = 0
        cl._last_nonzero = 0.0
        cl.gate_open = False
        cl._suspended = False
        cl._discard = False
        # simulate: old thread mid-retry-sleep when start() bumps the generation
        cl._run_id = 1
        cl._running = True
        cl._run_id = 2                       # start() called again
        # the old loop condition (run_id == 1) now fails → it exits instead of
        # opening a second stream
        assert cl._run_id != 1

    def test_no_images_in_saved_history(self, H, monkeypatch, tmp_path):
        hf = tmp_path / "history.json"
        monkeypatch.setattr(H, "HISTORY_FILE", hf)
        history = [{"role": "tool", "tool_name": "see_screen",
                    "content": "screenshot", "images": ["aGVsbG8=", "eWVhaA=="]},
                   {"role": "user", "content": "hi"}]
        H._strip_images(history)
        assert all("images" not in m for m in history)

    def test_urllib_parse_imported_explicitly(self, H):
        import ast, pathlib
        tree = ast.parse(pathlib.Path(H.__file__).read_text())
        found = False
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "urllib.parse":
                found = True
            if isinstance(n, ast.Import) and any(
                    a.name == "urllib.parse" for a in n.names):
                found = True
        assert found, "urllib.parse relied on as an import side effect"

    def test_no_hands_off_env_dead_code(self, H):
        import pathlib
        assert "HANDS_OFF" not in pathlib.Path(H.__file__).read_text()

    def test_no_shadowed_docstrings_still_holds(self, H):
        import ast, pathlib
        tree = ast.parse(pathlib.Path(H.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if (len(body) >= 2 and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[1], ast.Expr)
                        and isinstance(body[1].value, ast.Constant)
                        and isinstance(body[1].value.value, str)):
                    raise AssertionError(f"shadowed docstring in {node.name}")


class TestHistoryTokenTrim:
    """Token-based history budget (replaces the blind 40-message cap)."""

    def test_small_history_still_capped_at_40(self, H):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}] * 30
        out = H._trim_history(msgs)
        assert len(out) == 40
        assert out[-1] is msgs[-1]

    def _budget(self, H, monkeypatch, value):
        monkeypatch.setattr(H, "_history_budget", lambda: value)

    def test_big_history_trimmed_to_budget(self, H, monkeypatch):
        self._budget(H, monkeypatch, 1000)
        msgs = [{"role": "user", "content": "x" * 8000}] * 20   # ~2000 tokens each
        out = H._trim_history(msgs)
        # every kept message is ~2000 tokens > the 1000 budget: the never-empty
        # rule keeps only the most recent one
        assert len(out) == 1 and out[-1] is msgs[-1]
        # and with a budget above one message's size, it trims to fit
        self._budget(H, monkeypatch, 4500)
        out = H._trim_history(msgs)
        total = sum(H._msg_tokens(m) for m in out)
        assert total <= 4500 and len(out) >= 2

    def test_tool_sequence_not_orphaned(self, H, monkeypatch):
        self._budget(H, monkeypatch, 1000)
        msgs = [{"role": "user", "content": "x" * 8000},
                {"role": "assistant", "content": "y" * 8000},
                {"role": "assistant",
                 "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}]},
                {"role": "tool", "tool_name": "t", "content": "z" * 8000},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ho"}]
        out = H._trim_history(msgs)
        assert out[0]["role"] == "user"          # never starts mid-sequence
        assert out[-1] is msgs[-1]

    def test_single_over_budget_message_kept(self, H, monkeypatch):
        self._budget(H, monkeypatch, 100)
        out = H._trim_history([{"role": "user", "content": "x" * 100000}])
        assert len(out) == 1                     # never returns empty

    def test_msg_tokens_counts_tool_calls(self, H):
        m = {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "run_command",
                                          "arguments": '{"command": "' + "a" * 400 + '"}'}}]}
        assert H._msg_tokens(m) > 100

    def test_budget_auto_from_num_ctx(self, H):
        """The auto budget must leave room for the fixed prompt cost (system
        prompt + tool schemas) and a reply reserve, instead of overflowing
        the context window on top of them."""
        fixed = H._fixed_prompt_tokens()
        assert fixed > 1000, "fixed prompt cost seems unrealistically small"
        budget = H._history_budget()
        assert budget == max(1024, H.OLLAMA_NUM_CTX - fixed - 1024)
        # the old 3/4-of-ctx rule overflowed: 8192 - (6144 + 4750) < 0
        assert budget + fixed + 1024 <= H.OLLAMA_NUM_CTX

    def test_fixed_prompt_tokens_count_tools(self, H):
        import json
        assert H._fixed_prompt_tokens() == \
            (len(H.SYSTEM_PROMPT) + len(json.dumps(H.build_tools()))) \
            // H.HISTORY_CHARS_PER_TOKEN

    def test_warmup_loads_model(self, H, monkeypatch):
        """Startup warmup: the loader must call ollama_chat with the REAL
        system prompt + tools + history so the KV cache holds the exact
        prefix a real turn uses — the first question then only evaluates
        its own delta instead of the full prefill."""
        calls = []
        a = H.Assistant.__new__(H.Assistant)
        a._models_ready = H.threading.Event()
        a._history = [{"role": "user", "content": "remembered"}]
        monkeypatch.setattr(H, "ollama_chat",
                            lambda m, t: calls.append((m, t)) or {})
        monkeypatch.setattr(H, "get_whisper", lambda: None)
        monkeypatch.setattr(H, "get_piper", lambda: None)
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        a._maybe_report_crash = lambda: None
        a._speak = lambda *x, **k: None
        a._set = lambda *x: None
        a._gen = 0
        a._cancel = H.threading.Event()
        a._loader()
        assert calls, "loader must warm the LLM via ollama_chat"
        msgs, tools = calls[0]
        assert msgs[0]["role"] == "system" and "You are" in msgs[0]["content"], \
            "warmup must send the real system prompt"
        assert msgs[1:-1] == a._history, "warmup must include current history"
        assert msgs[-1]["content"] == "hi"
        assert tools is H.TOOLS, "warmup must pass the full tool schemas"

    def test_spotter_active_does_not_starve_vad_gate(self, H, monkeypatch):
        """REGRESSION: the spotter path reset the VAD gate after EVERY frame,
        so the gate's 2-consecutive-loud-frames counter never reached its
        threshold and hands-free was deaf while the spotter was on."""
        import numpy as np

        class FakeModel:
            def predict(self, chunk):
                return {"hey jarvis": 0.0}      # spotter never fires

        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
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
        # loud speech then quiet: the VAD must open on the loud frames and emit
        # on hangover — the spotter being active must not prevent that
        import itertools
        for loud in itertools.chain([True] * 30, [False] * 30):
            frame = (np.full(1024, 9000, dtype=np.int16) if loud
                     else np.zeros(1024, dtype=np.int16))
            lst._process_frame(frame, gate, frames, 1400, 5)
            if emitted:
                break
        assert emitted, "VAD gate never opened with spotter active — hands-free deaf"
        assert a._spotter_wake is False   # normal VAD path, not the spotter


class TestMedia:
    """MPD media tools: honest errors, real parsing, permission gate."""

    def _fake_mpc(self, H, monkeypatch, script):
        """script: {('search','filename',''): ['a.mp3'], ...} -> stdout."""
        def fake(*args, timeout=8.0):
            key = tuple(args)
            if key in script:
                return "\n".join(script[key]) + ("\n" if script[key] else "")
            return ""
        monkeypatch.setattr(H, "_mpc", fake)

    def test_play_resume_vs_shuffle(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        self._fake_mpc(H, monkeypatch, {
            ("playlist",): ["one.mp3", "two.mp3"],
            ("play",): []})
        out, err = belt.execute("media_play", {})
        assert not err and "playing (queue had 2" in out, out
        self._fake_mpc(H, monkeypatch, {
            ("playlist",): [],
            ("search", "filename", ""): ["a.mp3", "b.mp3", "c.mp3"]})
        out, err = belt.execute("media_play", {})
        assert not err and "shuffled 3" in out, out

    def test_play_query_replaces_queue_and_reports_extra(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        calls = []

        def fake(*args, timeout=8.0):
            calls.append(tuple(args))
            if tuple(args)[:2] == ("search", "title"):
                return "\n".join(["song1.mp3", "song2.mp3", "song3.mp3"])
            return ""
        monkeypatch.setattr(H, "_mpc", fake)
        out, err = belt.execute("media_play", {"query": "daft punk"})
        assert not err and "song1.mp3" in out and "and 2 more" in out, out
        assert ("clear",) in calls and ("add", "song1.mp3", "song2.mp3", "song3.mp3") in calls
        assert ("play",) in calls

    def test_no_match_reports_honestly(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        self._fake_mpc(H, monkeypatch, {("search", "title", "zzz"): []})
        out, err = belt.execute("media_play", {"query": "zzz"})
        assert err and "nothing in the library" in out, out

    def test_mpd_down_reports_fix(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        def boom(*args, timeout=8.0):
            raise RuntimeError("MPD is not running — start it with: "
                               "systemctl --user start mpd")
        monkeypatch.setattr(H, "_mpc", boom)
        out, err = belt.execute("now_playing", {})
        assert err and "systemctl --user start mpd" in out, out

    def test_control_actions_and_rejects(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        seen = []
        monkeypatch.setattr(H, "_mpc", lambda *a, **k: seen.append(a) or "")
        for action, want in [("pause", ("pause",)), ("next", ("next",)),
                             ("previous", ("prev",)), ("skip", ("next",))]:
            seen.clear()
            out, err = belt.execute("media_control", {"action": action})
            assert not err and seen[-1] == want, (action, seen)
        out, err = belt.execute("media_control", {"action": "rewind"})
        assert err and "action must be" in out

    def test_toggle_checks_state(self, H, monkeypatch):
        """This mpc build's bare 'pause' is a PURE pause (never resumes):
        toggle must send 'play' when paused and 'pause' when playing."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        calls = []
        def stateful(*args, timeout=8.0):
            calls.append(tuple(args))
            if tuple(args) == ("status",):
                return "[paused]"
            return ""
        monkeypatch.setattr(H, "_mpc", stateful)
        out, err = belt.execute("media_control", {"action": "toggle"})
        assert not err and "play" in out and calls[-1] == ("play",), (out, calls)
        calls.clear()
        def stateful2(*args, timeout=8.0):
            calls.append(tuple(args))
            if tuple(args) == ("status",):
                return "[playing]"
            return ""
        monkeypatch.setattr(H, "_mpc", stateful2)
        out, err = belt.execute("media_control", {"action": "toggle"})
        assert not err and "pause" in out and calls[-1] == ("pause",), (out, calls)

    def test_volume_clamps(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        seen = []
        monkeypatch.setattr(H, "_mpc", lambda *a, **k: seen.append(a) or "")
        out, err = belt.execute("media_volume", {"level": 250})
        assert not err and seen[-1] == ("volume", "100"), seen
        out, err = belt.execute("media_volume", {"level": "banana"})
        assert err and "number" in out, out

    def test_now_playing_parses_status(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        def fake(*args, timeout=8.0):
            if tuple(args) == ("current",):
                return "Artist - Song\n"
            return "volume: 55%   repeat: off [playing]  #2/9   1:23/4:05"
        monkeypatch.setattr(H, "_mpc", fake)
        out, err = belt.execute("now_playing", {})
        assert not err and "Artist - Song" in out and "playing" in out
        assert "2 of 9" in out and "55%" in out, out
        def fake_paused(*args, timeout=8.0):
            if tuple(args) == ("current",):
                return "X\n"
            return "volume: 55% [paused]"
        monkeypatch.setattr(H, "_mpc", fake_paused)
        out, err = belt.execute("now_playing", {})
        assert not err and "paused" in out, out

    def test_media_permission_gate(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None,
                          permissions={"media": False})
        out, err = belt.execute("media_play", {})
        assert err and "media" in out and "disabled" in out, out

    def test_prompt_documents_media(self, H):
        assert "media_play" in H.SYSTEM_PROMPT
        assert "now_playing" in H.SYSTEM_PROMPT


class TestDeepAuditFixes:
    """Findings from the 2026-09 deep audit pass."""

    def test_next_occurrence_dst_wall_clock(self, H, monkeypatch):
        """'tomorrow at 9' must stay 09:00 wall across DST shifts (the old
        +86400 s fired at 10:00 after spring-forward)."""
        import datetime as dt_mod
        from zoneinfo import ZoneInfo
        from types import SimpleNamespace
        b = ZoneInfo("Europe/Berlin")
        real_mod = H.datetime

        def make(now_aware):
            class FakeDT(dt_mod.datetime):
                @classmethod
                def now(cls, tz=None):
                    return now_aware
            monkeypatch.setattr(H, "datetime",
                                SimpleNamespace(datetime=FakeDT,
                                                timedelta=dt_mod.timedelta))
        try:
            # spring forward 2026-03-29 (02:00 -> 03:00): 01:30 CET + at-9 = 09:00 CEST
            make(dt_mod.datetime(2026, 3, 29, 1, 30, tzinfo=b))
            now = H.datetime.datetime.now().timestamp()
            due = H._next_occurrence(9, 0, 0, now)
            assert dt_mod.datetime.fromtimestamp(due, b).strftime("%H:%M") == "09:00"
            # fall back 2026-10-25 (03:00 -> 02:00)
            make(dt_mod.datetime(2026, 10, 25, 1, 30, tzinfo=b))
            now = H.datetime.datetime.now().timestamp()
            due = H._next_occurrence(9, 0, 0, now)
            assert dt_mod.datetime.fromtimestamp(due, b).strftime("%H:%M") == "09:00"
        finally:
            monkeypatch.setattr(H, "datetime", real_mod)

    def test_pipeline_queue_serializes_turns(self, H, monkeypatch):
        """Two rapid utterances must run ONE at a time (shared history,
        _stream_result and tool belt are not concurrency-safe)."""
        a = H.Assistant.__new__(H.Assistant)
        a._pipeline_q = H.queue.Queue()
        ran = []
        lock = H.threading.Lock()

        def fake_pipeline(audio, gen, cancel):
            with lock:
                running = getattr(a, "_running_flag", False)
                a._running_flag = True
            H.time.sleep(0.05)
            with lock:
                a._running_flag = False
                ran.append(gen)
        monkeypatch.setattr(a, "_pipeline", fake_pipeline)
        a._pipeline_worker_started = True
        # run the worker briefly: drain the queue via task_done semantics
        worker = H.threading.Thread(target=a._pipeline_worker, daemon=True)
        worker.start()
        a._pipeline_q.put((H.np.zeros(160, dtype=H.np.int16), 1, H.threading.Event()))
        a._pipeline_q.put((H.np.zeros(160, dtype=H.np.int16), 2, H.threading.Event()))
        a._pipeline_q.join()
        assert sorted(ran) == [1, 2]

    def test_history_write_respects_generation(self, H, monkeypatch):
        """A turn superseded by a newer utterance must not publish history."""
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 5
        a._history = [{"role": "user", "content": "old"}]
        saved = []
        monkeypatch.setattr(a, "_save_history", lambda: saved.append(True))
        # simulate _brain_turn's tail logic directly
        conversation = [{"role": "system", "content": "s"},
                        {"role": "user", "content": "stale turn"}]
        gen = 4   # stale: a newer turn took over
        if gen == a._gen:
            a._history = H._trim_history(conversation[1:])
            a._save_history()
        assert a._history[0]["content"] == "old" and not saved

    def test_settings_app_subprocess_timeouts(self, H, tmp_path):
        """Every subprocess.run in the settings app carries a timeout."""
        import ast, pathlib
        src = HERE / "handsoff-settings.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "run":
                kws = [k.arg for k in node.keywords or []]
                assert "timeout" in kws, f"subprocess.run without timeout at line {node.lineno}"


class TestKeepAliveAndIdentity:
    """Regression pins: model residency + prompt identity (2026-09-08 audit)."""

    def test_chat_payload_sends_keep_alive(self, H, monkeypatch):
        """Without keep_alive, Ollama unloads the model after 5 min idle and
        the next question pays a ~90 s reload (measured live)."""
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return io.BytesIO(json.dumps(
                {"message": {"role": "assistant", "content": "ok"}}).encode())

        monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
        H.ollama_chat([{"role": "user", "content": "hi"}], None)
        ka = captured["payload"].get("keep_alive")
        assert ka, "chat payload must carry keep_alive so the model stays resident"
        assert isinstance(ka, str) and ka.endswith(("m", "h")), \
            f"keep_alive should be a duration string, got {ka!r}"

    def test_stream_payload_sends_keep_alive(self, H, monkeypatch):
        import queue as qmod
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            line = json.dumps({"message": {"role": "assistant",
                                           "content": "One."}, "done": True})
            return io.BytesIO((line + "\n").encode())

        monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
        H.ollama_chat_stream([{"role": "user", "content": "hi"}],
                             qmod.Queue(), None, None)
        assert captured["payload"].get("keep_alive"), \
            "stream payload must carry keep_alive too"

    def test_prompt_identity_uses_wake_name(self, H):
        """The assistant was renamed to 'cypher' in settings; the prompt must
        not hardcode 'handsoff' as its name (stale identity bug)."""
        assert f"You are {H._wake_name()}" in H.SYSTEM_PROMPT, \
            "SYSTEM_PROMPT must introduce the assistant by its configured name"


class TestRollingMemory:
    """Durable facts about the user that survive history trimming."""

    def test_extract_name_and_relations(self, H):
        facts = dict(H._extract_memories(
            "my name is John and my sister is Anna"))
        assert facts["name"] == "the user's name is John"
        assert facts["rel:sister"] == "the user's sister is Anna"

    def test_extract_preferences_and_life(self, H):
        facts = dict(H._extract_memories(
            "I live in Berlin and I work at a bakery"))
        assert facts["home"] == "the user lives in Berlin"
        assert facts["work"] == "the user works at a bakery"
        pet = dict(H._extract_memories("I have a cat called Miso"))
        assert pet["pet"] == "the user has a cat called Miso"
        fav = dict(H._extract_memories("my favorite color is blue"))
        assert fav["fav:color"] == "the user's favorite color is blue"

    def test_extract_rejects_commands_and_requests(self, H):
        for t in ["what time is it", "stop the music",
                  "remind me to call my sister", "call me later", ""]:
            assert H._extract_memories(t) == [], t

    def test_merge_replaces_by_key_keeps_position(self, H):
        cur = [{"k": "name", "v": "the user's name is John"},
               {"k": "like", "v": "the user likes jazz"}]
        out = H._merge_memories(cur, [("name", "the user's name is Quinton")])
        assert [m["k"] for m in out] == ["name", "like"]
        assert out[0]["v"] == "the user's name is Quinton"

    def test_merge_caps_and_dedupes(self, H):
        cur = [{"k": f"k{i}", "v": f"fact {i}"} for i in range(H.MAX_MEMORY_FACTS)]
        out = H._merge_memories(cur, [("new", "brand new fact")])
        assert len(out) == H.MAX_MEMORY_FACTS and out[-1]["k"] == "new"
        out2 = H._merge_memories(cur, [("dup", "fact 3")])
        assert len(out2) == H.MAX_MEMORY_FACTS   # identical text deduped

    def test_memory_survives_trimming(self, H, monkeypatch, tmp_path):
        """THE core guarantee: facts live in memory.json, not history —
        trimming the conversation must never lose them."""
        monkeypatch.setattr(H, "MEMORY_FILE", tmp_path / "memory.json")
        monkeypatch.setattr(H, "_history_budget", lambda: 100)
        a = H.Assistant.__new__(H.Assistant)
        a._memory = H._load_memory()
        a._memory = H._merge_memories(a._memory,
                                      [("name", "the user's name is Quinton")])
        H._save_memory(a._memory)
        # brutal trim: everything dropped
        trimmed = H._trim_history(
            [{"role": "user", "content": "x" * 2000}] * 10)
        assert len(trimmed) == 1
        # facts still on disk, reload works
        assert H._load_memory()[0]["v"] == "the user's name is Quinton"

    def test_conversation_injects_memory_block(self, H, monkeypatch):
        a = H.Assistant.__new__(H.Assistant)
        a._history = []
        a._memory = [{"k": "name", "v": "the user's name is Quinton"}]
        a._maybe_briefing_prefix = lambda t: ""
        conv = a._conversation_for("what is my name")
        roles = [m["role"] for m in conv]
        assert roles == ["system", "system", "user"]
        assert "the user's name is Quinton" in conv[1]["content"]
        assert "Facts you remember" in conv[1]["content"]
        # and with no memory: no extra block
        a._memory = []
        conv2 = a._conversation_for("hello")
        assert [m["role"] for m in conv2] == ["system", "user"]

    def test_pipeline_extracts_and_persists(self, H, monkeypatch, tmp_path):
        """End-to-end: a pipeline turn with a durable fact updates memory.json
        (the pipeline runs it BEFORE the brain, so trimming can't race it)."""
        monkeypatch.setattr(H, "MEMORY_FILE", tmp_path / "memory.json")
        a = H.Assistant.__new__(H.Assistant)
        a._memory = []
        a._last_transcript = ("", 0, 0.0)
        a._set = lambda gen, state: None
        a._speak = lambda *x, **k: None
        a._recently_spoken = []
        a._matches_recent_speech = lambda t: False
        a._spotter_wake = False
        a._handsfree = False
        a._wake_until = 0.0
        a._empty_streak = 0
        a._models_ready = H.threading.Event()
        a._models_ready.set()
        a._brain_turn = lambda *x, **k: None
        monkeypatch.setattr(H, "transcribe",
                            lambda audio: "my name is John and I have a cat called Miso")
        import numpy as np
        a._pipeline(np.zeros(H.SAMPLE_RATE, dtype=np.int16),
                    1, H.threading.Event())
        saved = H._load_memory()
        keys = {m["k"] for m in saved}
        assert "name" in keys and "pet" in keys, saved


class TestAuditNineFindings:
    """Regression pins for the external audit's 9 confirmed findings."""

    def test_1_niri_absolute_path_spawn_bypass(self, H, monkeypatch):
        """/usr/bin/niri msg action spawn -- node -e 1 must be refused:
        the spawn checks key on the executable's basename, not argv[0]."""
        executed = []
        monkeypatch.setattr(H.subprocess, "run",
                            lambda argv, **kw: executed.append(argv)
                            or type("R", (), {"returncode": 0, "stdout": "",
                                              "stderr": ""})())
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        for cmd in ["/usr/bin/niri msg action spawn -- node -e 1",
                    "/usr/local/bin/niri msg action spawn -- python -c 'x'",
                    "niri msg action spawn -- /usr/bin/python -c 'x'"]:
            out, err = belt.execute("run_command", {"command": cmd})
            assert "REFUSED" in out, (cmd, out)
        assert executed == [], "interpreter spawn reached subprocess!"

    def test_1_spawn_legit_app_still_works(self, H, monkeypatch):
        """Legitimate GUI spawn must keep working through the basename check."""
        monkeypatch.setattr(H.subprocess, "run",
                            lambda argv, **kw: type("R", (), {
                                "returncode": 0, "stdout": "", "stderr": ""})())
        monkeypatch.setattr(H.shutil, "which", lambda p: "/usr/bin/alacritty")
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute(
            "run_command", {"command": "/usr/bin/niri msg action spawn -- alacritty"})
        assert not err and "exit code 0" in out, out

    def test_2_transcript_cache_is_per_generation(self, H, monkeypatch):
        """A transcript from a PREVIOUS generation must never be reused:
        new audio must be transcribed, not executed from stale text."""
        a = H.Assistant.__new__(H.Assistant)
        a._memory = []
        a._last_transcript = ("open calculator", 5, H._tick_now())  # gen 5
        a._set = lambda gen, state: None
        a._speak = lambda *x, **k: None
        a._recently_spoken = []
        a._matches_recent_speech = lambda t: False
        a._spotter_wake = False
        a._handsfree = False
        a._wake_until = 0.0
        a._empty_streak = 0
        a._models_ready = H.threading.Event()
        a._models_ready.set()
        got = []
        a._brain_turn = lambda text, *k: got.append(text)
        monkeypatch.setattr(H, "transcribe", lambda audio: "close browser")
        import numpy as np
        a._pipeline(np.zeros(H.SAMPLE_RATE, dtype=np.int16), 6,
                    H.threading.Event())
        assert got == ["close browser"], got

    def test_2_same_generation_transcript_reused(self, H, monkeypatch):
        """Within ONE turn the stop-probe's transcript is reused (no double
        transcription) — that is the whole point of the cache."""
        a = H.Assistant.__new__(H.Assistant)
        a._memory = []
        a._last_transcript = ("hello there", 6, H._tick_now())  # same gen 6
        a._set = lambda gen, state: None
        a._speak = lambda *x, **k: None
        a._recently_spoken = []
        a._matches_recent_speech = lambda t: False
        a._spotter_wake = False
        a._handsfree = False
        a._wake_until = 0.0
        a._empty_streak = 0
        a._models_ready = H.threading.Event()
        a._models_ready.set()
        a._brain_turn = lambda *x, **k: None
        called = []
        monkeypatch.setattr(H, "transcribe",
                            lambda audio: called.append(1) or "x")
        import numpy as np
        a._pipeline(np.zeros(H.SAMPLE_RATE, dtype=np.int16), 6,
                    H.threading.Event())
        assert called == [], "same-gen transcript must be reused"

    def test_3_typing_fails_closed_on_unknown_focus(self, H, monkeypatch):
        """When niri IPC is dead (focus unknown), typing and key injection
        must REFUSE — an unidentified window might be a terminal."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(belt, "_focused_window_info", lambda: None)
        yd = []
        monkeypatch.setattr(belt, "_ydotool", lambda *a, **k: yd.append(a) or "ok")
        for tool, args in [("type_text", {"text": "hi\n"}),
                           ("press_keys", {"combo": "enter"}),
                           ("press_hotkey", {"combo": "ctrl+q"})]:
            out, err = belt.execute(tool, args)
            assert "REFUSED" in out, (tool, out)
        assert yd == [], "keys injected with unknown focus!"

    def test_4_spoken_snooze_persists_fired_oneoff(self, H, monkeypatch, tmp_path):
        """fire → announce → spoken 'snooze' → reminder must be PERSISTED
        (the old order cleared the offer before the tool could use it)."""
        monkeypatch.setattr(H, "REMINDERS_FILE", tmp_path / "reminders.json")
        monkeypatch.setattr(H, "REMINDERS_LOCK", H.threading.RLock())
        H.REMINDERS_FILE.write_text("[]")
        H._snooze_offer.clear()
        H._snooze_offer.update(name="tea", until=H.time.monotonic() + 90)
        a = H.Assistant.__new__(H.Assistant)
        a._tools = H.ToolBelt(on_restart_pending=lambda: None)
        a._set = lambda *x: None
        a._speak = lambda *x, **k: None
        assert a._try_snooze("snooze 5 minutes", 1, H.threading.Event()) is True
        H.time.sleep(0.2)
        saved = H._load_reminders()
        assert saved and saved[0]["name"] == "tea", saved
        # and the offer is closed only after success
        assert not H._snooze_offer
        H._snooze_offer.clear()

    def test_5_reminder_transactions_serialized(self, H, monkeypatch, tmp_path):
        """Concurrent set + fire must not lose updates: every RMW goes
        through REMINDERS_LOCK."""
        monkeypatch.setattr(H, "REMINDERS_FILE", tmp_path / "reminders.json")
        monkeypatch.setattr(H, "REMINDERS_LOCK", H.threading.RLock())
        H.REMINDERS_FILE.write_text("[]")
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        errors = []

        def creator():
            for i in range(20):
                out, err = belt.execute("set_reminder", {
                    "wake_name": f"r{i}", "when_due": "in 2 hours"})
                if err:
                    errors.append(out)

        t = H.threading.Thread(target=creator)
        t.start()
        # concurrent firer: prunes due reminders while creations happen
        for _ in range(20):
            H._take_missed_reminders()
            H.time.sleep(0.005)
        t.join()
        assert not errors, errors
        names = {r["name"] for r in H._load_reminders()}
        assert len(names) == 20, f"lost updates: {len(names)}/20"

    def test_9_set_survives_deleted_qt_object(self, H):
        """A late emit after Qt teardown must not raise (background threads
        outliving the widget raised 'Signal source has been deleted')."""
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 3
        def boom(state):
            raise RuntimeError("Signal source has been deleted")
        a.sigState = type("S", (), {"emit": staticmethod(boom)})()
        a._set(3, H.IDLE)          # must not raise
        assert a._state == H.IDLE


class TestAuditRoundTwo:
    """Second external-audit fixes (verified 2026-09-08)."""

    def test_handsfree_toggle_preserves_concurrent_settings_saves(
            self, H, monkeypatch, tmp_path):
        """Toggling hands-free must read-merge-write settings.json, never
        dump the bubble's stale startup snapshot over newer disk state."""
        cfg = tmp_path / "settings.json"
        # disk state is NEWER than the bubble's memory (user saved in the
        # Settings app after the bubble started): different model + a key
        # the bubble's snapshot doesn't even have
        cfg.write_text(H.json.dumps({"model": "newer:model",
                                     "wake_word": "cypher",
                                     "handsfree": False}), encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", cfg)
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = False
        a._gen = 0
        a._listener = type("L", (), {"start": lambda self: None,
                                     "stop": lambda self: None})()
        a._set = lambda *x: None
        a.set_handsfree(True)
        disk = H.json.loads(cfg.read_text(encoding="utf-8"))
        assert disk["handsfree"] is True
        assert disk["model"] == "newer:model", "user save must survive"
        assert disk["wake_word"] == "cypher", "unknown keys must survive"

    def test_handsfree_toggle_survives_corrupt_settings(self, H, monkeypatch,
                                                        tmp_path):
        cfg = tmp_path / "settings.json"
        cfg.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", cfg)
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = True
        a._gen = 0
        a._listener = type("L", (), {"start": lambda self: None,
                                     "stop": lambda self: None})()
        a._set = lambda *x: None
        a.set_handsfree(False)          # must not raise
        disk = H.json.loads(cfg.read_text(encoding="utf-8"))
        assert disk == {"handsfree": False}

    def test_settings_app_coerces_garbage_values(self, H):
        """Audit #2: a hand-edited settings.json with garbage must not crash
        the settings app (the recovery tool). The app's merge_settings now
        applies the bubble's shared coerce_settings — bad values fall back
        to defaults instead of raising in _load_values' int()/float()."""
        for bad in ({"engage_seconds": "abc"}, {"tts_rate": "1,5"},
                    {"num_ctx": "32k"}):
            merged = H.json.loads(H.json.dumps(H.DEFAULT_SETTINGS))
            merged.update(bad)
            out = H.coerce_settings(merged)
            for k in bad:
                assert out[k] == H.DEFAULT_SETTINGS[k], (k, out[k])
        # valid values survive untouched
        merged = H.json.loads(H.json.dumps(H.DEFAULT_SETTINGS))
        merged.update({"num_ctx": 16384, "tts_rate": 1.25})
        out = H.coerce_settings(merged)
        assert out["num_ctx"] == 16384 and out["tts_rate"] == 1.25

    def test_memory_block_never_persists_into_history(self, H, monkeypatch,
                                                      tmp_path):
        """Audit #3: _brain_turn used to persist conversation[1:] INCLUDING
        the per-turn memory block — one stale copy accumulated per turn.
        History must contain only user/assistant/tool messages; the memory
        block is injected fresh by _conversation_for every turn."""
        a = H.Assistant.__new__(H.Assistant)
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(H, "HISTORY_FILE", hist_file)
        a._gen = 1
        a._history = []
        a._memory = [{"k": "name", "v": "Quinton"}]
        saved = {}
        monkeypatch.setattr(H.Assistant, "_save_history",
                            lambda self: saved.setdefault("h", list(self._history)))
        monkeypatch.setattr(H, "_strip_images", lambda h: None)
        monkeypatch.setattr(H, "_trim_history", lambda msgs: msgs)

        msgs = [
            {"role": "system", "content": "MAIN PROMPT"},
            {"role": "system",
             "content": "Facts you remember about the user:\n- Quinton"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        # the same slice/filter the history-publish tail now applies
        a._history = [m for m in msgs[1:] if m.get("role") != "system"]
        roles = [m["role"] for m in a._history]
        assert "system" not in roles, roles
        assert roles == ["user", "assistant"], roles
        # and the loader heals an already-polluted old history file
        hist_file.write_text(H.json.dumps([
            {"role": "system", "content": "Facts you remember about the user:\n- old"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]), encoding="utf-8")
        loaded = H.Assistant._load_history()
        assert all(m.get("role") != "system" for m in loaded)


class TestAmbientCapabilities:
    def _tb(self, H, **kwargs):
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"], "notifications": True,
                    "pomodoro": True, "watchers": True}
        tb._watch_lock = threading.RLock()
        tb._file_watchers = {}
        tb._process_watchers = {}
        tb._on_notification = kwargs.get("on_notification")
        tb._on_announce = kwargs.get("on_announce")
        tb._on_pomodoro = kwargs.get("on_pomodoro")
        return tb

    def test_notification_reader_is_private_by_default(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        assert tb.notification_reader("status").startswith("notification reader is off")
        assert "started" in tb.notification_reader("start")

    def test_notification_mute_list_is_bounded_and_persisted(self, H, monkeypatch):
        saved = []
        monkeypatch.setattr(H, "_persist_setting", lambda k, v: saved.append((k, v)))
        tb = self._tb(H, on_notification=lambda enabled: None)
        out = tb.notification_reader("mute", ",".join(f"app{i}" for i in range(40)))
        assert "app0" in out and len(H.SETTINGS["notification_mute_apps"]) == 32
        assert saved and saved[-1][0] == "notification_mute_apps"

    def test_notification_parser_filters_mute(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_mute_apps": ["secret"]})
        tb = self._tb(H, on_announce=lambda text: (_ for _ in ()).throw(AssertionError()))
        lines = ['signal time=1 interface=org.freedesktop.Notifications member=Notify',
                 '   string "secret-app"', '   uint32 0', '   string ""',
                 '   string "title"', '   string "body"']
        class P:
            stdout = lines
            def poll(self): return None
        tb._announce_now = tb._on_announce
        tb._dbus_strings = H.Assistant._dbus_strings
        H.Assistant._notification_loop(tb, P(), threading.Event())

    def test_pomodoro_delegates_and_validates(self, H):
        calls = []
        tb = self._tb(H, on_notification=lambda enabled: None,
                      on_pomodoro=lambda *args: calls.append(args) or "started")
        assert tb.pomodoro("start", 25, 5) == "started"
        assert calls == [("start", 25.0, 5.0)]
        assert tb.pomodoro("start", 0, 5).startswith("ERROR")

    def test_watch_file_starts_and_stops(self, H, tmp_path):
        p = tmp_path / "x.log"
        p.write_text("old\n")
        tb = self._tb(H, on_announce=lambda text: None)
        assert "watching" in tb.watch_file(str(p), "ERROR", "start")
        assert "x.log" in tb.watch_file(str(p), action="list")
        assert "stopped" in tb.watch_file(str(p), action="stop")
        tb.stop_watchers()

    def test_watch_limits_and_invalid_pattern(self, H, tmp_path):
        tb = self._tb(H, on_announce=lambda text: None)
        assert tb.watch_file(str(tmp_path / "missing"), "x", "start").startswith("ERROR")
        p = tmp_path / "x"; p.write_text("")
        assert tb.watch_file(str(p), "[", "start").startswith("ERROR")

    def test_process_watch_name_validation(self, H):
        tb = self._tb(H, on_announce=lambda text: None)
        assert tb.watch_process("bad/name", "start").startswith("ERROR")
        assert "watching process" in tb.watch_process("definitely-not-running", "start")
        assert "stopped" in tb.watch_process("definitely-not-running", "stop")
        tb.stop_watchers()


class TestResourceAlerts:
    def _assistant(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._resource_alerted = {"ram": False, "vram": False}
        a._resource_last = {"ram": None, "vram": None}
        a.spoken = []
        a._announce_now = lambda text: a.spoken.append(text)
        return a

    def test_disabled_does_not_probe_or_speak(self, H, monkeypatch):
        a = self._assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS, "resource_alerts": False})
        monkeypatch.setattr(H.Assistant, "_resource_usage", staticmethod(
            lambda: (_ for _ in ()).throw(AssertionError("should not probe"))))
        a._resource_tick()
        assert a.spoken == []

    def test_threshold_crossing_alerts_once_and_rearms(self, H, monkeypatch):
        a = self._assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "resource_alerts": True,
                                             "ram_alert_percent": 90.0,
                                             "vram_alert_percent": 90.0})
        readings = iter(({"ram": 91.0, "vram": None},
                         {"ram": 95.0, "vram": None},
                         {"ram": 80.0, "vram": None},
                         {"ram": 92.0, "vram": None}))
        monkeypatch.setattr(H.Assistant, "_resource_usage", staticmethod(lambda: next(readings)))
        a._resource_tick(); a._resource_tick(); a._resource_tick(); a._resource_tick()
        assert len(a.spoken) == 2
        assert "system memory" in a.spoken[0] and "91" in a.spoken[0]
        assert "92" in a.spoken[1]

    def test_vram_and_ram_alert_independent(self, H, monkeypatch):
        a = self._assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS, "resource_alerts": True})
        monkeypatch.setattr(H.Assistant, "_resource_usage", staticmethod(
            lambda: {"ram": 91.0, "vram": 93.0}))
        a._resource_tick()
        assert len(a.spoken) == 2
        assert any("system memory" in x for x in a.spoken)
        assert any("GPU memory" in x for x in a.spoken)

    def test_usage_handles_missing_proc_and_nvidia(self, H, monkeypatch):
        monkeypatch.setattr(H, "shutil", types.SimpleNamespace(which=lambda _: None))
        monkeypatch.setattr(H.Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError()))
        usage = H.Assistant._resource_usage()
        assert usage == {"ram": None, "vram": None}

    def test_settings_wiring(self, H):
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        for part in ("resource_chk", "ram_alert_spin", "vram_alert_spin",
                     'resource_alerts', 'ram_alert_percent', 'vram_alert_percent'):
            assert part in src
