"""Historical audit pins: regressions from past review rounds."""
from __future__ import annotations

import ast
import base64
import importlib.util
import inspect
from collections import deque
import threading
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import (HERE as ROOT, _load, _user_site, core_module, pin_offer,
                      run_driver, wait_for)

from core import brain as _core_brain
from core import registry as _core_registry
from core import settings as _core_settings

# Resolved on first use rather than at collection (conftest.core_module).
_core_tools = core_module("tools")

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
        assert wait_for(lambda: "text" in captured), \
            "the timer announcement never reached the speaker"
        assert captured.get("cancel_set") is False, "cancel was pre-set — reminder would be silent"
        assert "tea" in captured.get("text", "")

    def test_logs_are_private_and_redacted(self, H, tmp_path):
        path = tmp_path / "handsoff.log"
        handler = H._PrivateRotatingFileHandler(path, maxBytes=1, backupCount=1)
        handler.addFilter(H._PrivacyLogFilter())
        logger = logging.getLogger("handsoff-test-private")
        logger.handlers[:] = [handler]
        logger.setLevel(logging.INFO)
        try:
            logger.info("heard (gen=1): secret transcript")
            logger.info("run_command: %s", "cat /secret/password")
            logger.info("edit_file content: secret file edit")
            handler.flush()
            assert path.stat().st_mode & 0o777 == 0o600
            assert "secret transcript" not in path.read_text()
            assert "password" not in path.read_text()
            handler.doRollover()
            assert (tmp_path / "handsoff.log.1").stat().st_mode & 0o777 == 0o600
        finally:
            logger.handlers.clear()
            handler.close()

    def test_remote_ollama_requires_explicit_opt_in(self, H, monkeypatch, caplog):
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://192.0.2.10:11434")
        monkeypatch.delenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", raising=False)
        monkeypatch.setattr(H, "_REMOTE_OLLAMA_WARNED", False)
        with pytest.raises(RuntimeError, match="non-loopback"):
            H._guard_ollama_endpoint()
        assert "privacy is not guaranteed" in caplog.text
        monkeypatch.setenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", "1")
        H._guard_ollama_endpoint()

    def test_allow_remote_ollama_is_schema_default(self, H):
        """The remote-brain opt-in is a first-class setting, not a ghost key:
        it must exist in the shipped schema so Settings and migration see it."""
        assert H.DEFAULT_SETTINGS["allow_remote_ollama"] is False

    def test_allow_remote_ollama_opt_in_via_settings(self, H, monkeypatch):
        """allow_remote_ollama: true must satisfy the guard without the env var
        (Settings → Brain checkbox path), while env var alone also works."""
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://192.0.2.10:11434")
        monkeypatch.delenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", raising=False)
        monkeypatch.setattr(H, "_REMOTE_OLLAMA_WARNED", False)
        settings = {**H.DEFAULT_SETTINGS, "allow_remote_ollama": True}
        monkeypatch.setattr(H, "SETTINGS", settings)
        H._guard_ollama_endpoint()          # must NOT raise
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                            "allow_remote_ollama": False})
        with pytest.raises(RuntimeError, match="non-loopback"):
            H._guard_ollama_endpoint()

    def test_coerce_rejects_remote_opt_in_garbage(self, H):
        """Fail-closed: any junk in allow_remote_ollama coerces to False."""
        from core.settings import coerce_settings
        for junk in ("yes", 1, ["x"], {"a": 1}):
            s = coerce_settings({**H.DEFAULT_SETTINGS, "allow_remote_ollama": junk})
            assert s["allow_remote_ollama"] is False, junk
        s = coerce_settings({**H.DEFAULT_SETTINGS, "allow_remote_ollama": True})
        assert s["allow_remote_ollama"] is True

    def test_remote_opt_in_does_not_fail_open_on_uncoerced_settings(self, H, monkeypatch):
        """The opt-in predicate used truthiness, so a caller that seeded
        SETTINGS without coerce_settings (`"yes"`, 1) read as opted-in even
        though the settings contract is strictly `is True`. That is the
        fail-open half of the disagreement: one file, two answers.
        """
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://192.0.2.10:11434")
        monkeypatch.delenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", raising=False)
        monkeypatch.setattr(H, "_REMOTE_OLLAMA_WARNED", False)
        for junk in ("yes", 1, "true", "on", [1], {"a": 1}):
            monkeypatch.setattr(H, "SETTINGS",
                                {**H.DEFAULT_SETTINGS, "allow_remote_ollama": junk})
            assert H._ollama_remote_opted_in() is False, junk
            with pytest.raises(RuntimeError, match="non-loopback"):
                H._guard_ollama_endpoint()

    def test_opt_in_source_names_the_channel(self, H, monkeypatch):
        """One predicate answers for guard, settings contract and doctor."""
        monkeypatch.delenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", raising=False)
        monkeypatch.setattr(H, "SETTINGS",
                            {**H.DEFAULT_SETTINGS, "allow_remote_ollama": True})
        assert H._remote_ollama_optin_source() == "settings"
        monkeypatch.setattr(H, "SETTINGS",
                            {**H.DEFAULT_SETTINGS, "allow_remote_ollama": False})
        assert H._remote_ollama_optin_source() == ""
        monkeypatch.setenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", "yes")
        assert H._remote_ollama_optin_source() == "env"
        assert H._ollama_remote_opted_in() is True

    def test_doctor_names_the_env_opt_in_channel(self, H, monkeypatch):
        """The env var is a second opt-in Settings cannot display, so doctor
        must say the environment is what made the brain remote."""
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://192.0.2.10:11434")
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        monkeypatch.setenv("HANDSOFF_ALLOW_REMOTE_OLLAMA", "1")
        text = H.run_doctor()
        assert "explicitly allowed" in text
        assert "HANDSOFF_ALLOW_REMOTE_OLLAMA" in text
        assert "NOT visible in Settings" in text

    def test_doctor_flags_remote_brain_unless_allowed(self, H, monkeypatch):
        """--ptt doctor must surface the remote-brain trust warning, and must
        show the explicitly-allowed state once opted in."""
        monkeypatch.setattr(H, "OLLAMA_BASE", "http://192.0.2.10:11434")
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        text = H.run_doctor()
        assert "brain privacy: REMOTE" in text
        assert "NOT allowed" in text and "FAILS CLOSED" in text
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                            "allow_remote_ollama": True})
        text = H.run_doctor()
        assert "explicitly allowed" in text

    def test_doctor_silent_for_loopback_brain(self, H):
        """Local Ollama must not grow a privacy line (byte-stable report)."""
        text = H.run_doctor()
        assert "brain privacy" not in text

    def test_settings_gui_wires_remote_opt_in(self):
        """The Settings Brain tab carries the checkbox, and persists it by key.

        The hand-written widget name (`remote_ollama_chk`) and the two cfg lines
        beside it are gone: the control, its load and its save are generated
        from the table, and the widget is reachable as
        `win.allow_remote_ollama` — which is exactly what the offscreen
        `remote_ollama_checkbox_roundtrip` scenario ticks and saves.
        """
        from settings_schema import control_for, fields_by_key
        field = fields_by_key()["allow_remote_ollama"]
        assert control_for(field) == "checkbox", field
        assert field.tab == "brain", field
        assert "remote" in field.title.lower(), field.title

    def test_late_worker_cannot_change_state_after_shutdown(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 3
        a._state = H.THINKING
        a._closed = True
        a.sigState = types.SimpleNamespace(emit=lambda *_: None)
        H.Assistant._set(a, 3, H.IDLE)
        assert a.state == H.THINKING

    def test_announce_missed_uses_fresh_cancel(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 0
        captured = {}
        a._speak = lambda text, gen, cancel, sentence_q=None: captured.update(
            cancel_set=cancel.is_set())
        a._set = lambda gen, state: None
        a._announce_missed([{"name": "pills"}])
        assert wait_for(lambda: "cancel_set" in captured), \
            "the missed-reminder announcement never reached the speaker"
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

    def test_an_unanswered_tool_call_is_stripped_before_it_is_stored(self, H):
        """A turn that stops for a confirmation offer publishes a message whose
        calls were only PARTLY answered. History is the prefix of every later
        request, and a tool call with no matching result is the shape a model
        rejects or mis-conditions on — on every future turn."""
        history = [
            {"role": "user", "content": "do two things"},
            {"role": "assistant", "content": "On it.",
             "tool_calls": [{"id": "a", "function": {"name": "one"}},
                            {"id": "b", "function": {"name": "two"}}]},
            {"role": "tool", "tool_name": "one", "content": "done"},
        ]
        H._seal_tool_calls(history)
        assert "tool_calls" not in history[1]
        assert history[1]["content"] == "On it.", "the words must stay"

    def test_a_fully_answered_batch_keeps_its_plumbing(self, H):
        """Matched by position: the bubble's tool entries carry no id, so an
        id-based match would call every answered call orphaned."""
        history = [
            {"role": "assistant", "content": "On it.",
             "tool_calls": [{"function": {"name": "one"}}]},
            {"role": "tool", "tool_name": "one", "content": "done"},
        ]
        H._seal_tool_calls(history)
        assert history[0]["tool_calls"], (
            "an answered call lost its plumbing — history no longer matches "
            "the conversation the model produced")

    def test_a_call_only_message_disappears_rather_than_emptying(self, H):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "one"}}]},
        ]
        H._seal_tool_calls(history)
        assert [m["role"] for m in history] == ["user"], history

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
            (len(H.SYSTEM_PROMPT) + len(json.dumps(_core_tools.build_tools()))) \
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
        monkeypatch.setattr(H, "get_tts", lambda: None)
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

    def _loader_assistant(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._models_ready = H.threading.Event()
        a._history = []
        a._maybe_report_crash = lambda: None
        a._speak = lambda *x, **k: None
        a._set = lambda *x: None
        a._gen = 0
        a._cancel = H.threading.Event()
        return a

    def test_loader_warms_the_speech_engine(self, H, monkeypatch):
        """The FIRST spoken reply used to be the slow one.

        Measured through the control socket: 1.5 s to first audio on the first
        reply after a fresh start, 0.6 s on every later one — the decoder, flow
        sampler and vocoder compile their CUDA kernels on first use. The loader
        already pays the model load, so it must also pay this.
        """
        warmed = []
        monkeypatch.setattr(H, "get_whisper", lambda: None)
        monkeypatch.setattr(H, "get_tts", lambda: None)
        monkeypatch.setattr(H, "warm_tts", lambda: warmed.append(1) or 128)
        monkeypatch.setattr(H, "ollama_chat", lambda m, t: {})
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        a = self._loader_assistant(H)
        a._loader()
        assert warmed, "the loader never warmed the speech engine"

    def test_a_failed_warm_up_is_not_a_failed_startup(self, H, monkeypatch):
        """A warm-up is an optimisation: if it breaks, the bubble must still
        come up (and say so) rather than lose speech entirely."""
        def boom():
            raise RuntimeError("no CUDA kernels for this shape")

        monkeypatch.setattr(H, "get_whisper", lambda: None)
        monkeypatch.setattr(H, "get_tts", lambda: None)
        monkeypatch.setattr(H, "warm_tts", boom)
        monkeypatch.setattr(H, "ollama_chat", lambda m, t: {})
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        a = self._loader_assistant(H)
        a._loader()                       # must not raise
        assert a._models_ready.is_set(), "startup must still complete"

    def test_the_warm_up_does_not_load_a_model_of_its_own(self, H):
        """core.audio must warm the model it is HANDED.

        If it fell back to loading one itself, a test that stubs get_tts would
        pull 3.8 GB into the test process — and a failed load elsewhere would
        be retried silently at warm-up time.
        """
        fake = types.SimpleNamespace(generate=lambda text: [0.0] * 64)
        assert H._audio.warm_tts(model=None) == 0, "nothing to warm, no load"
        assert H._audio.warm_tts(model=fake) > 0

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


class TestTheShippedSchemaIsWhatTheUserSwitchedOn:
    """The belt is JSON'd into EVERY round, so what ships is a budget the user
    already controls: `permissions` is keyed by FAMILY (one dropdown per family
    in the settings app, `run_command` / `screen_access` / `operator` …), and a
    tool whose family is off can only ever answer "REFUSED: disabled in handsoff
    settings".

    Measured 2026-09-21, before this round: the host filtered on the tool's own
    NAME, which is never a key in that dict — the lookup defaulted to True and
    so NOTHING was ever filtered. A default config shipped 4 schemas (operator
    x3, notifications x1) for tools the belt then refused: 1449 chars, ~362
    tokens, every single round.
    """

    def _default_families(self):
        import settings_schema as _ss
        return dict(_ss.DEFAULT_SETTINGS["permissions"])

    def test_a_family_switched_off_leaves_the_prompt(self, H):
        tools = _core_tools.build_tools()
        perms = self._default_families()
        off = sorted(k for k, v in perms.items() if not v)
        assert off, "this guard is meaningless without a default-off family"
        gates = _core_tools.tool_gates()
        shipped = _core_tools.permitted_tools(tools, perms)
        assert [t for t in shipped
                if gates[t["function"]["name"]] in off] == [], \
            "a switched-off family's schemas are still being paid for"
        saved = len(json.dumps(tools)) - len(json.dumps(shipped))
        assert saved > 1000, f"the saving is meant to be real, got {saved} chars"

    def test_nothing_usable_is_dropped(self, H):
        """The direction that must never regress: with every family on, the
        shipped list IS the belt. A filter that loses a tool loses a capability.
        """
        tools = _core_tools.build_tools()
        assert _core_tools.permitted_tools(tools, {k: True for k in self._default_families()}) \
            == tools

    def test_a_family_the_filter_has_never_heard_of_fails_open(self, H):
        """An empty (or hand-edited, or older-than-this-release) permissions
        dict must not withdraw anything — the belt's own check is
        `self._perm.get(gate, True)`, so the filter must match it. And with
        EVERY family off, only the gateless tools may remain.
        """
        tools = _core_tools.build_tools()
        assert _core_tools.permitted_tools(tools, {}) == tools
        gates = _core_tools.tool_gates()
        shipped = {t["function"]["name"] for t in _core_tools.permitted_tools(
            tools, {k: False for k in self._default_families()})}
        assert shipped == {t["function"]["name"] for t in tools
                           if not gates.get(t["function"]["name"])}

    def test_the_turn_site_filters_on_the_family(self, H):
        """The predicate is only as good as its call site: the round loop must
        call it with live SETTINGS, so a family switched on mid-session is
        offered on the very next round rather than at the next restart."""
        src = Path(ROOT, "handsoff.py").read_text(encoding="utf-8")
        assert 'permitted_tools(TOOLS, SETTINGS["permissions"])' in src
        assert 'SETTINGS["permissions"].get(t["function"]["name"]' not in src, \
            "the name-keyed lookup filtered nothing — permissions is keyed by family"

    def test_a_switched_off_family_can_still_name_its_switch(self, H, monkeypatch):
        """Dropping the schema also drops the tool that used to say "disabled in
        handsoff settings", so the family names travel in the system prompt
        instead and the refusal keeps its fix path."""
        monkeypatch.setitem(H.SETTINGS, "permissions",
                            {"operator": False, "notifications": False,
                             "web_access": True})
        off = H._switched_off_families()
        assert "operator" in off and "notifications" in off
        assert "web_access" not in off, "an enabled family must not be announced as off"


class TestTrimmingTheSchemaCostsNoCapability:
    """Trimming PROSE is free; changing the INTERFACE is a capability change and
    must never happen by accident. These two pin the difference.

    Measured 2026-09-21: the whole belt `json.dumps`s to 17764 chars (~4441
    tokens) and rides in every round — down from 18512 (~4628 tokens) before the
    windows and system families were trimmed (their descriptions went
    1734+1274 -> 1225+1053, and their parameter docs shrank by ~180 more).
    """

    #: sha256 of {name: {params, required, types}} for every tool — the part of
    #: a schema the model can actually call. Update this (and the spec tables)
    #: when the interface deliberately changes; if you only meant to shorten a
    #: description, a failure here means you shortened a capability instead.
    INTERFACE = "7c2229e56c87b1198bef146f9834cb9ee630303dc502c45d958cbd50126ae958"

    #: The whole belt, as requests sends it (ensure_ascii).
    SCHEMA_CEILING = 18_000
    #: Descriptions of the windows + system families — the two the audit named
    #: as 46% of the schema. Measured 2278 today, 3008 before the trim.
    BIG_FAMILY_PROSE_CEILING = 2_400

    def test_the_interface_is_the_shape_this_round_pinned(self, H):
        import hashlib
        by = {t["function"]["name"]: t["function"] for t in H.TOOLS}
        iface = {n: {"params": sorted(by[n]["parameters"]["properties"]),
                     "required": by[n]["parameters"]["required"],
                     "types": {p: s.get("type") for p, s
                               in by[n]["parameters"]["properties"].items()}}
                 for n in sorted(by)}
        blob = json.dumps(iface, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(blob.encode()).hexdigest()
        assert digest == self.INTERFACE, (
            f"the tool interface changed ({len(by)} tools)\n"
            "If that was deliberate — a new tool, a new parameter — update "
            "INTERFACE here and the spec census. If you were only trimming "
            "prose, you have just removed something callable.")

    def test_the_prompt_budget_does_not_drift_back_up(self, H):
        total = len(json.dumps(H.TOOLS))
        assert total <= self.SCHEMA_CEILING, (
            f"the tool schemas are {total} chars (ceiling {self.SCHEMA_CEILING}) "
            "— this is paid in EVERY round, so trim it or raise the ceiling on "
            "purpose")

    def test_the_oversized_families_keep_their_prose_trimmed(self, H):
        by = {t["function"]["name"]: t["function"] for t in H.TOOLS}
        big = ["wait_for_window", "workspace", "niri_capabilities", "see_screen",
               "close_window", "read_screen_text", "focus_window",
               "screen_elements", "click_element", "open_app", "click_at",
               "run_command", "edit_file", "wait", "kill_process", "read_file",
               "get_weather", "confirm_kill", "get_datetime"]
        prose = sum(len(by[n]["description"]) for n in big if n in by)
        assert prose <= self.BIG_FAMILY_PROSE_CEILING, (
            f"the windows+system descriptions are back up to {prose} chars "
            f"(ceiling {self.BIG_FAMILY_PROSE_CEILING}; 3008 before the trim) — "
            "say it in the refusal or the docstring, not in the prompt")


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

    def test_a_stopped_player_is_toggled_with_play_and_keeps_its_queue(self, H, monkeypatch):
        """Checked against a real mpd: a STOPPED player prints no `[state]` line
        in `status`, keeps its queue, and `current` is empty. Toggle read
        `[paused]`, so it "paused" a stopped player (a no-op) and said so, and
        now_playing called the queue empty."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        stopped = "volume: n/a   repeat: off   random: off   single: off   consume: off"
        calls = []

        def fake(*args, timeout=8.0):
            calls.append(tuple(args))
            if args == ("status",):
                return stopped
            if args == ("current",):
                return ""
            if args == ("playlist",):
                return "Alice - Blue Sky.wav\nBob - Red Rain.wav\n"
            return ""
        monkeypatch.setattr(H, "_mpc", fake)
        out, err = belt.execute("media_control", {"action": "toggle"})
        assert not err and out == "music player: play" and calls[-1] == ("play",), (out, calls)
        out, err = belt.execute("now_playing", {})
        assert out == "the music player is stopped (2 songs in the queue)", out

        def one(*args, timeout=8.0):
            return "Only.wav" if args == ("playlist",) else ""
        monkeypatch.setattr(H, "_mpc", one)
        out, err = belt.execute("now_playing", {})
        assert out == "the music player is stopped (1 song in the queue)", out

        monkeypatch.setattr(H, "_mpc", lambda *a, **k: "")
        out, err = belt.execute("now_playing", {})
        assert out == "nothing is playing (the music queue is empty)", out

        def broken(*args, timeout=8.0):
            if args == ("playlist",):
                raise RuntimeError("mpc timed out")
            return ""
        monkeypatch.setattr(H, "_mpc", broken)
        out, err = belt.execute("now_playing", {})
        assert out == "nothing is playing (the music queue is empty)", out

    def test_a_one_song_queue_is_not_plural(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H, "_mpc", lambda *a, **k: "Only.wav" if a == ("playlist",) else "")
        out, err = belt.execute("media_play", {})
        assert out == "playing (queue had 1 song)", out

    def test_a_signed_volume_is_a_change_and_a_decimal_is_read(self, H, monkeypatch):
        """A sign was accepted by the pattern and clamped as an absolute level:
        "+10" set the volume TO 10 and "-10" to 0 — "turn it down a bit" muted
        the music. It is `mpc volume +10` now. A decimal is read, and a huge
        string is a refusal rather than int()'s own ValueError."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        seen = []
        monkeypatch.setattr(H, "_mpc", lambda *a, **k: seen.append(a) or "")
        cases = [("+10", ("volume", "+10"), "music volume up 10%"),
                 ("-10", ("volume", "-10"), "music volume down 10%"),
                 ("-250", ("volume", "-100"), "music volume down 100%"),
                 ("50.0", ("volume", "50"), "music volume set to 50%"),
                 ("49.6", ("volume", "50"), "music volume set to 50%"),
                 ("30%", ("volume", "30"), "music volume set to 30%"),
                 (" 0 ", ("volume", "0"), "music volume set to 0%")]
        for level, call, said in cases:
            seen.clear()
            out, err = belt.execute("media_volume", {"level": level})
            assert not err and out == said and seen == [call], (level, out, seen)
        for bad in ("9" * 5000, "loud", "", "--5", "1e3"):
            seen.clear()
            out, err = belt.execute("media_volume", {"level": bad})
            assert err and "number" in out and not seen, (bad[:10], out, seen)

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

    def test_pipeline_queue_latest_turn_replaces_pending_and_marks_done(self, H):
        """Only pending work is replaceable; dropped work still balances join()."""
        a = H.Assistant.__new__(H.Assistant)
        a._pipeline_q = H.queue.Queue(maxsize=1)
        a._pipeline_submit_lock = H.threading.Lock()
        a._closed = False
        a._shutdown_event = H.threading.Event()
        old_cancel = H.threading.Event()
        new_cancel = H.threading.Event()
        old = (b"old", 1, old_cancel)
        new = (b"new", 2, new_cancel)

        assert a._enqueue_pipeline_turn(old) is True
        assert a._enqueue_pipeline_turn(new) is True
        assert old_cancel.is_set()
        assert a._pipeline_q.get_nowait() is new
        a._pipeline_q.task_done()
        a._pipeline_q.join()

    def test_pipeline_queue_rejects_after_shutdown(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._pipeline_q = H.queue.Queue(maxsize=1)
        a._pipeline_submit_lock = H.threading.Lock()
        a._closed = True
        a._shutdown_event = H.threading.Event()
        assert a._enqueue_pipeline_turn((b"late", 1, H.threading.Event())) is False
        assert a._pipeline_q.empty()

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
        # EXACTLY one system message, first. Ollama answers HTTP 500 ("system
        # message must be at the beginning") for any system message after the
        # first — verified live against qwen3.8:27b, where every turn carrying
        # a tail system note died while the same prompt on gemma4:latest was
        # fine — so the block rides in the user turn instead of its own role.
        assert [m["role"] for m in conv] == ["system", "user"]
        assert "the user's name is Quinton" in conv[-1]["content"]
        assert "Facts you remember" in conv[-1]["content"]
        assert "what is my name" in conv[-1]["content"]
        # and the injected block is remembered, so the history write can keep
        # the plain utterance (the block is re-injected on every turn)
        assert conv[-1]["content"].startswith(a._turn_injected)
        # and with no memory: no extra block
        a._memory = []
        conv2 = a._conversation_for("hello")
        assert [m["role"] for m in conv2] == ["system", "user"]
        assert a._turn_injected == ""

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
        """Legitimate GUI spawn must keep working through the basename check.

        The `which` stub answers per PROGRAM rather than for everything: the
        allowlist also judges a command NAMED BY PATH by identity, and a stub
        that returns the terminal for any question cannot answer that one
        honestly. It used to blanket-return alacritty, which is what a real
        `which` never does, and the interaction only surfaced once the
        identity check started asking.
        """
        monkeypatch.setattr(H.subprocess, "run",
                            lambda argv, **kw: type("R", (), {
                                "returncode": 0, "stdout": "", "stderr": ""})())
        monkeypatch.setattr(
            H.shutil, "which",
            lambda p: f"/usr/bin/{p}" if p in ("alacritty", "niri") else None)
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
        pin_offer(H, monkeypatch, "snooze")   # one offer, on _dep()'s path
        H._snooze_offer.arm(90, name="tea")
        a = H.Assistant.__new__(H.Assistant)
        a._tools = H.ToolBelt(on_restart_pending=lambda: None)
        a._set = lambda *x: None
        a._speak = lambda *x, **k: None
        assert a._try_snooze("snooze 5 minutes", 1, H.threading.Event()) is True
        assert wait_for(lambda: bool(H._load_reminders())), \
            "the snooze was never persisted (worker did not finish)"
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

    def test_toolbelt_dependencies_follow_worker_thread(self, H, monkeypatch,
                                                         tmp_path):
        """A belt made in the main thread keeps DI when execute runs in a worker."""
        monkeypatch.setattr(H, "REMINDERS_FILE", tmp_path / "reminders.json")
        monkeypatch.setattr(H, "REMINDERS_LOCK", H.threading.RLock())
        H.REMINDERS_FILE.write_text("[]")
        # Force the failure mode: an empty worker ContextVar must not replace
        # the dependencies captured by this belt.
        monkeypatch.setattr(H._core_tools, "_DEFAULT_DEPS", types.SimpleNamespace())
        belt = H._core_tools.ToolBelt(
            on_restart_pending=lambda: None, dependencies=H._tool_dependencies)
        result = []
        worker = H.threading.Thread(
            target=lambda: result.append(belt.execute(
                "set_reminder", {"wake_name": "worker", "when_due": "in 2 hours"})))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result and result[0][1] is False, result
        assert H._load_reminders()[0]["name"] == "worker"

    def test_set_dependencies_installs_both_slots(self, H, monkeypatch):
        """The host's installer, not just a belt's constructor.

        Two slots answer different questions and the installer exists because
        they have to move together: a thread that INHERITED a context resolves
        the ContextVar, and a thread with none — a worker, an executor child —
        falls through to the module default. Install only the first and the same
        tool behaves differently depending on which thread ran it, which is the
        whole reason `set_dependencies` is a function rather than two lines in
        the host that someone can do half of.
        """
        import contextvars
        tools = H._core_tools
        fresh = contextvars.ContextVar("test_tools_deps", default=None)
        monkeypatch.setattr(tools, "_CURRENT", fresh)
        # something else in the default slot, so the call is what has to move it
        monkeypatch.setattr(tools, "_DEFAULT_DEPS",
                            types.SimpleNamespace(name="not-the-host"))
        marker = types.SimpleNamespace(name="host-installed")
        tools.set_dependencies(marker)
        assert fresh.get() is marker
        seen = {}

        def worker_body():
            seen["context"] = fresh.get()
            seen["resolved"] = tools._dep()

        worker = H.threading.Thread(target=worker_body)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert seen["context"] is None, seen       # a fresh thread has no context
        assert seen["resolved"] is marker, seen    # so only the default can answer

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


class TestAuditRoundThree:
    """Third external-audit fixes."""

    def test_a_junk_history_budget_falls_back_instead_of_raising(self, H,
                                                                 monkeypatch):
        """`_history_budget` is called on EVERY turn to trim the conversation.

        A bare `int()` on a hand-edited (or pre-coercion) value raised
        ValueError there, which took out the turn — a config typo became a
        bubble that could not answer at all.
        """
        monkeypatch.setitem(H.SETTINGS, "history_tokens", "lots")
        assert H._history_budget() == max(
            1024, H.OLLAMA_NUM_CTX - H._fixed_prompt_tokens() - 1024)

    def test_a_junk_followup_window_closes_instead_of_raising(self, H,
                                                              monkeypatch):
        """`_speak` reads the follow-up window on the speech thread AFTER
        speaking, so a bare `float()` there killed the thread with the sentence
        already spoken — the reply landed but the bubble looked broken."""
        assert H._followup_seconds() > 0
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", "soon")
        assert H._followup_seconds() == 0.0
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", -5)
        assert H._followup_seconds() == 0.0

    def test_a_model_that_refuses_tools_is_remembered_not_re_probed(
            self, H, monkeypatch):
        """Probing was dead telemetry: `_brain_deps` handed `core.brain` a
        FRESH state dict per call, so the recorded refusal was thrown away and
        `_TOOLS_SUPPORTED` stayed True forever — a tool-less model paid the
        same failed 400 round-trip on every single turn."""
        deps = H._brain_deps()
        assert deps["state"] is H._BRAIN_STATE, "state is not the shared dict"
        assert H._BRAIN_STATE["tools_supported"] is True
        H._BRAIN_STATE["tools_supported"] = False
        assert H._brain_deps()["state"]["tools_supported"] is False
        H.reload_derived_settings()
        assert H._BRAIN_STATE["tools_supported"] is False

    def test_the_pipeline_skips_tools_for_a_model_that_refuses_them(
            self, H, monkeypatch):
        """The point of remembering: do not send a tools payload (and eat a
        400) to a model already known to refuse it."""
        seen = []
        monkeypatch.setattr(H, "_BRAIN_STATE", {"tools_supported": False})
        monkeypatch.setattr(H, "ollama_chat",
                            lambda msgs, tools=None: seen.append(tools)
                            or {"content": "hi", "tool_calls": []})
        monkeypatch.setitem(H.SETTINGS, "streaming_tts", False)
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 1
        a._tools = types.SimpleNamespace(_set_user_turn=lambda *_: None)
        a._conversation_for = lambda text: []
        a._speak = lambda *x, **k: None
        a._turn_spoke = False
        a._brain_turn("hi", 1, H.threading.Event())
        assert seen == [[]], seen

    def test_missed_reminders_are_not_consumed_when_the_prune_fails(
            self, H, tmp_path, monkeypatch):
        """If the prune cannot be saved, the reminders are still ON DISK.
        Reporting them as taken announced the same ones again on the next boot
        — and every boot after that."""
        rf = tmp_path / "reminders.json"
        store = H._reminder_store()
        monkeypatch.setattr(store, "path", rf)
        due = [{"name": "gone", "due": H.time.time() - 60}]
        rf.write_text(H.json.dumps(due), encoding="utf-8")
        monkeypatch.setattr(store, "save",
                            lambda items: (_ for _ in ()).throw(OSError("disk")))
        assert store.take_missed() == []
        assert rf.exists(), "the file must be left alone"


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
        # the persist must have replaced the corrupt file with a clean,
        # version-stamped dict (split step (a) stamps every on-disk write)
        assert disk.get("handsfree") is False
        assert disk.get("version") == H.SETTINGS_VERSION

    def test_settings_app_coerces_garbage_values(self, H):
        """Audit #2: a hand-edited settings.json with garbage must not crash
        the settings app (the recovery tool). The app's merge_settings now
        applies the bubble's shared coerce_settings — bad values fall back
        to defaults instead of raising in _load_values' int()/float()."""
        for bad in ({"engage_seconds": "abc"}, {"tts_rate": "1,5"},
                    {"num_ctx": "32k"}):
            merged = H.json.loads(H.json.dumps(H.DEFAULT_SETTINGS))
            merged.update(bad)
            out = _core_settings.coerce_settings(merged)
            for k in bad:
                assert out[k] == H.DEFAULT_SETTINGS[k], (k, out[k])
        # valid values survive untouched
        merged = H.json.loads(H.json.dumps(H.DEFAULT_SETTINGS))
        merged.update({"num_ctx": 16384, "tts_rate": 1.25})
        out = _core_settings.coerce_settings(merged)
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
        # Both watcher registries share one lock, so their caps and their
        # teardown are enforced in the same critical section.
        tb._watch_lock = threading.RLock()
        tb._file_watchers = _core_registry.BoundedRegistry(
            "watch-file", 4, lock=tb._watch_lock)
        tb._process_watchers = _core_registry.BoundedRegistry(
            "watch-process", 4, lock=tb._watch_lock)
        tb._on_notification = kwargs.get("on_notification")
        tb._on_announce = kwargs.get("on_announce")
        tb._on_pomodoro = kwargs.get("on_pomodoro")
        return tb

    def test_reader_rearm_gate_bare_start_refused_while_offer_live(self, H, monkeypatch):
        """The whole point of the re-arm path: after the reader stopped on its
        own (five monitor deaths), the model cannot bring it back at its bare
        word. The offer exists, the flag is off — and 'start' without the
        user's yes is refused with the path back named. The offer is NOT
        consumed by a refusal.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        offers = [(True, True)]          # (needed, live)
        consumed: list = []
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: offers[-1]
        tb._consume_rearm_offer = lambda: consumed.append(1) or {"ok": True}
        out = tb.notification_reader("start")
        assert out.startswith("ERROR: re-enabling needs the user's spoken yes"), out
        assert consumed == [], "a refusal must not consume the offer"
        # ...and the refusal names the sentence the user was told to say.
        assert "spoken yes" in out

    def test_reader_rearm_gate_confirm_yes_consumes_and_enables(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        offers = [(True, True)]
        consumed: list = []
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: offers[-1]
        tb._consume_rearm_offer = lambda: consumed.append(1) or {"ok": True}
        out = tb.notification_reader("start", confirm="yes")
        assert consumed == [1], "yes claims the offer"
        assert "started" in out, out

    def test_reader_rearm_gate_confirm_no_declines_and_consumes(self, H, monkeypatch):
        """'no' must CONSUME the offer, not leave it claimable by a later
        call — matching confirm_kill's semantics, where a declined offer is
        spent either way."""
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        offers = [(True, True)]
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: offers[-1]
        tb._consume_rearm_offer = lambda: {"ok": True}
        out = tb.notification_reader("start", confirm="no")
        assert out.startswith("Left off"), out
        # ...and the second yes cannot resurrect a declined offer.
        tb._on_rearm_offer = lambda: (True, False)
        out2 = tb.notification_reader("start", confirm="yes")
        assert "expired" in out2, out2

    def test_reader_rearm_gate_expired_offer_refused_with_the_path_back(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: None)
        tb._on_rearm_offer = lambda: (True, False)   # needed, NOT live
        out = tb.notification_reader("start", confirm="yes")
        assert out.startswith("ERROR: the notification reader stopped on its own"), out
        assert "expired" in out and "settings app" in out, out

    def test_healthy_reader_start_is_ungated(self, H, monkeypatch):
        """The gate keys on the reader's own gave_up diagnosis — NOT on "an
        offer exists". A reader that never gave up starts at the model's bare
        word exactly as before; this is the anti-false-positive rule the
        self-watch round taught.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: (False, False)  # not needed, no offer
        out = tb.notification_reader("start")
        assert "started" in out, out

    def test_absent_rearm_seam_leaves_start_ungated(self, H, monkeypatch):
        """Belts built with __new__ and no rearm seam at all: the gate is
        absent, so start behaves exactly as before. A host that cannot supply
        the gate cannot have armed an offer either.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        out = tb.notification_reader("start")
        assert "started" in out, out

    def test_reader_rearm_gate_toggle_to_off_is_ungated(self, H, monkeypatch):
        """The gate protects re-enabling, not stopping: with the reader ON,
        a toggle (which turns it OFF) passes through the gate untouched — and
        with the gate's seam present but the reader healthy.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": True})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: (True, True)    # even a live offer...
        calls: list = []
        tb._consume_rearm_offer = lambda: calls.append(1) or {"ok": True}
        out = tb.notification_reader("toggle")
        assert "stopped" in out, out
        assert calls == [], "stopping must not consume a re-arm offer"

    def test_reader_rearm_gate_racing_second_yes_refused(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: (True, True)
        tb._consume_rearm_offer = lambda: None   # the loser of a race
        out = tb.notification_reader("start", confirm="yes")
        assert out.startswith("ERROR: the re-enable offer was just claimed"), out
        assert "notification reader is on" not in out, out

    def test_reader_rearm_gate_refusal_marks_the_one_retryable_refusal(self, H, monkeypatch):
        """The in-turn retry marker lives ONLY on the refusal a retry can fix:
        an unusable confirm while the offer is still live. Nothing else about
        this tool may nudge the model — not a declined offer (that is the
        user's answer), not an expired one (no retry can help), not a healthy
        start, not a race loss. This is the only seam the tool loop reads.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: (True, True)
        tb._consume_rearm_offer = lambda: {"ok": True}
        tb._last_rearm_retry = False
        # The refusal itself: marked, offer NOT consumed, sentence named.
        out = tb.notification_reader("start")
        assert tb._last_rearm_retry is True, "the retryable refusal must be marked"
        assert "confirm='yes'" in out, out
        # A declined offer consumes-and-declines: the user answered, there is
        # nothing to retry, and the marker must not be set.
        tb._last_rearm_retry = False
        out = tb.notification_reader("start", confirm="no")
        assert tb._last_rearm_retry is False
        assert "Left off" in out, out
        # The yes path: consumed and enabled, no marker.
        tb._last_rearm_retry = False
        out = tb.notification_reader("start", confirm="yes")
        assert tb._last_rearm_retry is False
        assert "started" in out, out

    def test_reader_rearm_gate_marker_resets_on_every_execute(self, H, monkeypatch):
        """The marker is per-CALL state like its sibling _last_confirmation_offer:
        the next execute() on the same belt must not inherit a stale retry
        signal from the previous call — a healthy start must never be nudged
        because an earlier call was refused. Driven through execute() (not the
        method directly) because the reset lives in _execute().
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        tb._on_rearm_offer = lambda: (True, True)
        tb._consume_rearm_offer = lambda: {"ok": True}
        # Seed the pieces _execute needs that __new__ skips.
        tb._policy = type("P", (), {"classify": staticmethod(lambda name: "ALLOW"),
                                    "confirm_seconds": staticmethod(lambda: 120)})()
        tb._confirm_running = None
        tb._rate_lock = threading.Lock()
        tb._tool_times = __import__("collections").deque()
        tb._rate_limit = lambda: 0
        monkeypatch.setattr(_core_tools, "log_decision",
                            lambda *a, **k: None)
        out, _err = tb.execute("notification_reader", {"action": "start"})
        assert out.startswith("ERROR: re-enabling needs the user's spoken yes"), out
        assert tb._last_rearm_retry is True
        # The very next execute — a full yes — must find the marker cleared.
        out2, _err2 = tb.execute("notification_reader",
                                 {"action": "start", "confirm": "yes"})
        assert tb._last_rearm_retry is False
        assert "started" in out2, out2

    def test_reader_rearm_gate_marker_absent_on_stub_belts(self, H, monkeypatch):
        """Belts built with __new__ and never through __init__ have no marker
        attribute; the gate must still refuse correctly, and the tool loop's
        getattr reads stay None — 'no marker' is 'no retry', never a crash.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: None)
        assert not hasattr(tb, "_last_rearm_retry")
        tb._on_rearm_offer = lambda: (True, True)
        out = tb.notification_reader("start")
        assert out.startswith("ERROR: re-enabling needs the user's spoken yes"), out
        assert getattr(tb, "_last_rearm_retry", None) is True

    def test_reader_rearm_gate_refusals_are_durable_in_the_decision_log(
            self, H, monkeypatch):
        """A refusal is never only in the model's reply — the house rule the
        injection ledger made concrete: the gate's three refusals lived
        nowhere but the tool result, and decisions.jsonl recorded only the
        policy's ALLOW. Each branch now logs REFUSE with its reason named:
        a bare start while the offer is live, an expired offer, a lost race.
        Consent keeps logging CONFIRM; a healthy start logs nothing at all.
        """
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        seen: list = []
        monkeypatch.setattr(_core_tools, "log_decision",
                            lambda *a, **k: seen.append(a))
        tb._on_rearm_offer = lambda: (True, True)
        tb._consume_rearm_offer = lambda: {"ok": True}
        # Bare start: REFUSE, naming the branch.
        tb.notification_reader("start")
        assert seen[-1][:3] == ("notification_reader", "", "REFUSE"), seen
        assert "bare start" in seen[-1][3], seen
        # A declined offer is an ANSWER: CONFIRM, not REFUSE.
        tb.notification_reader("start", confirm="no")
        assert seen[-1][2] == "CONFIRM" and "declined" in seen[-1][3], seen
        # The yes path: CONFIRM consumed.
        tb.notification_reader("start", confirm="yes")
        assert seen[-1][2] == "CONFIRM" and "consumed" in seen[-1][3], seen
        # Expired offer: REFUSE, pointing at the settings app.
        tb._on_rearm_offer = lambda: (True, False)
        seen.clear()
        tb.notification_reader("start", confirm="yes")
        assert len(seen) == 1 and seen[0][2] == "REFUSE", seen
        assert "expired" in seen[0][3], seen
        # Lost race: REFUSE, naming the claim.
        tb._on_rearm_offer = lambda: (True, True)
        tb._consume_rearm_offer = lambda: None
        seen.clear()
        tb.notification_reader("start", confirm="yes")
        assert len(seen) == 1 and seen[0][2] == "REFUSE", seen
        assert "claimed elsewhere" in seen[0][3], seen
        # A healthy start is not a refusal: nothing logged.
        tb._on_rearm_offer = lambda: (False, False)
        seen.clear()
        out = tb.notification_reader("start")
        assert "started" in out, out
        assert seen == [], seen

    def test_notification_reader_is_private_by_default(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS,
                                             "notification_reader": False})
        tb = self._tb(H, on_notification=lambda enabled: "started" if enabled else "stopped")
        assert tb.notification_reader("status").startswith("notification reader is off")
        assert "started" in tb.notification_reader("start")

    def test_a_raw_string_cannot_turn_the_privacy_gate_on(self, H, monkeypatch):
        """The gate was read as `bool(SETTINGS.get(...))`, and `bool("false")`
        is True — so a value that reached this module from anywhere other than
        `coerce_settings` would START the monitor that reads private desktop
        notifications aloud while looking like it switched it off.

        The host's loader coerces every flag key, so the reachable case today is
        a STANDALONE `core.tools` consumer (or an embedder assigning into
        SETTINGS) — `core.tools` ships as its own module and cannot assume the
        host's coercion ran. The read is strict now, so the gate no longer
        depends on who wrote the dict.
        """
        for raw in ("false", "no", "off", "0", "nonsense", None, []):
            monkeypatch.setitem(H.SETTINGS, "notification_reader", raw)
            assert _core_tools.setting_flag("notification_reader") is False, (
                f"{raw!r} was read as ENABLED — bool({raw!r}) is {bool(raw)!r}")
            tb = self._tb(H, on_notification=lambda enabled: None)
            assert tb.notification_reader("status").startswith(
                "notification reader is off"), raw
        # ...and a real truthy value still reads as on
        monkeypatch.setitem(H.SETTINGS, "notification_reader", True)
        assert _core_tools.setting_flag("notification_reader") is True
        monkeypatch.setitem(H.SETTINGS, "notification_reader", "yes")
        assert _core_tools.setting_flag("notification_reader") is True

    def test_the_dry_run_gate_is_read_strictly_too(self, H, monkeypatch):
        """Same shape, opposite direction: `dry_run` decided with `bool()` and
        set to "false" read as ON, so the safety setting someone believed they
        had turned off silently suppressed every desktop action."""
        for raw in ("false", "nonsense", None, []):
            monkeypatch.setitem(H.SETTINGS, "dry_run", raw)
            assert _core_tools.setting_flag("dry_run") is False, raw
        monkeypatch.setitem(H.SETTINGS, "dry_run", "on")
        assert _core_tools.setting_flag("dry_run") is True

    def test_notification_mute_list_is_bounded_and_persisted(self, H, monkeypatch):
        saved = []
        # Model the real wrapper's contract (persist, update memory, return
        # True): set_setting only updates SETTINGS on a reported success.
        monkeypatch.setattr(H, "_persist_setting", lambda k, v: (
            saved.append((k, v)), H.SETTINGS.__setitem__(k, v), True)[-1])
        monkeypatch.setitem(H.SETTINGS, "notification_mute_apps",
                            H.SETTINGS["notification_mute_apps"])
        tb = self._tb(H, on_notification=lambda enabled: None)
        out = tb.notification_reader("mute", ",".join(f"app{i}" for i in range(40)))
        assert "app0" in out and len(H.SETTINGS["notification_mute_apps"]) == 32
        assert "WARNING" not in out, out
        assert saved and saved[-1][0] == "notification_mute_apps"

    def test_notification_parser_filters_mute(self, H, monkeypatch):
        from core import assistant as assist_mod
        announced: list = []
        reader = assist_mod.NotificationReader(
            spawn=lambda *a, **k: None, is_closed=lambda: False,
            announce=announced.append,
            muted=lambda a, s, b: assist_mod.notification_muted(
                a, s, b, mute_apps=["secret"], app_name="handsoff"),
            popen_factory=None, persist=lambda k, v: None)
        lines = ['signal time=1 interface=org.freedesktop.Notifications member=Notify',
                 '   string "secret-app"', '   uint32 0', '   string ""',
                 '   string "title"', '   string "body"']

        class P:
            stdout = lines
            def poll(self): return None
        reader.loop(P(), threading.Event())
        assert announced == []
        clean = ['signal time=2 interface=org.freedesktop.Notifications member=Notify',
                 '   string "mail"', '   uint32 0', '   string ""',
                 '   string "hi"', '   string "you have mail"']

        class P2:
            stdout = clean
            def poll(self): return None
        reader.loop(P2(), threading.Event())
        assert len(announced) == 1 and "Notification from mail" in announced[0]
        # historical seams stay live for external callers
        assert H.Assistant._dbus_strings('   string "a"') == ["a"]

    def test_pomodoro_delegates_and_validates(self, H):
        calls = []
        tb = self._tb(H, on_notification=lambda enabled: None,
                      on_pomodoro=lambda *args: calls.append(args) or "started")
        assert tb.pomodoro("start", 25, 5) == "started"
        assert calls == [("start", 25.0, 5.0)]
        assert tb.pomodoro("start", 0, 5).startswith("ERROR")

    def test_stop_and_status_do_not_read_the_durations(self, H):
        """A model fills the fields a call does not need — 0 is a common pick —
        and range-checking them for `stop` refused the one command that ends the
        timer, with a complaint about minutes the stop never reads."""
        calls = []
        tb = self._tb(H, on_notification=lambda enabled: None,
                      on_pomodoro=lambda *args: calls.append(args) or "ok")
        for action in ("stop", "status"):
            for junk in ((0, 0), (-5, 999), ("x", None), (float("nan"), 1)):
                assert tb.pomodoro(action, *junk) == "ok", (action, junk)
        assert [c[0] for c in calls] == ["stop"] * 4 + ["status"] * 4
        # ...and starting still validates, whatever else changed
        assert tb.pomodoro("start", 500, 5).startswith("ERROR")
        assert tb.pomodoro("start", "x", 5).startswith("ERROR")

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

    def test_a_junk_value_that_means_off_does_not_probe_or_speak(
            self, H, monkeypatch):
        """`bool("false")` is True — the tick probed the machine and spoke the
        memory alerts the user had asked to be off."""
        for raw in ("false", "no", "off", "0", "nonsense"):
            a = self._assistant(H)
            monkeypatch.setitem(H.SETTINGS, "resource_alerts", raw)
            monkeypatch.setattr(H.Assistant, "_resource_usage", staticmethod(
                lambda: (_ for _ in ()).throw(
                    AssertionError("probed while off"))))
            a._resource_tick()
            assert a.spoken == [], raw

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

    def test_settings_wiring(self):
        """All three rows are table-driven; the hand-written names are gone.

        `resource_chk` / `ram_alert_spin` / `vram_alert_spin` were the widgets
        this file used to require in the settings app's source. The refactor
        replaced them with generated controls (the window exposes them as
        `win.resource_alerts`, `win.ram_alert_percent`, `win.vram_alert_percent`)
        so the subject stays the same and the question moves to the table: one
        checkbox and two spin boxes, all on the Voice page.
        """
        from settings_schema import control_for, fields_by_key
        rows = fields_by_key()
        assert control_for(rows["resource_alerts"]) == "checkbox"
        assert control_for(rows["ram_alert_percent"]) == "spin"
        assert control_for(rows["vram_alert_percent"]) == "spin"
        for key in ("resource_alerts", "ram_alert_percent", "vram_alert_percent"):
            assert rows[key].tab == "voice", key


class TestEveryFlagReadIsStrict:
    """The monolith's boolean settings are read through ONE strict reader.

    `bool(SETTINGS["x"])` INVERTS the most natural way to write "off":
    `"false"`, `"no"` and `"off"` are all truthy strings, so the raw read
    ENABLES the thing it looks like it disables. These are the flags that start
    hands-free, demand the public wake word, type into other windows, read
    private desktop notifications aloud and let a tick probe the machine.

    The host's loader coerces every flag key, so through the host none of this
    can fire today — these guards exist because the read must not depend on WHO
    wrote the dict (`_reload_settings_live` swaps in a whole new one, an
    embedder can assign into it, a test seam replaces it). That is the same
    argument `core.tools.setting_flag` itself is written to, and the monolith
    now reads every flag through it.
    """

    FLAGS = {
        "wake_spotter": False,
        "wake_word_required": False,
        "handsfree": False,
        "notification_reader": False,
        "mic_selfheal": True,
        "resource_alerts": False,
        "world_warnings": False,
        "hardware_watch": False,
        "dictation": True,
        "streaming_tts": True,
        "briefing": False,
    }

    #: every way of writing "off" that `bool()` gets wrong
    OFF_FORMS = ("false", "no", "off", "0", "", "FALSE", "No", 0, 0.0)

    def test_no_raw_truth_test_of_a_flag_survives_in_the_monolith(self, H):
        """The sweep itself.

        A raw `bool(SETTINGS...` read — or an implicit truth test in an
        `if`/`and`/`elif`, which is the same defect with no `bool()` to grep
        for — fails here instead of waiting for someone to notice the bubble
        did the opposite of the setting. `log.info(... %s', SETTINGS.get(x))`
        is a value DISPLAY, not a read, and is left alone.
        """
        offenders = []
        for i, line in enumerate(
                (HERE / "handsoff.py").read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") or "_setting_flag(" in line:
                continue
            if not any(t in line for t in ("bool(", "not ", " and ",
                                           " or ", "if ", "elif ")):
                continue
            for key in self.FLAGS:
                if f'SETTINGS.get("{key}"' in line or \
                        f'SETTINGS["{key}"]' in line:
                    offenders.append(f"{i}: {line.strip()}")
        assert not offenders, (
            "raw truth test on a coerced flag — read it with _setting_flag:\n"
            + "\n".join(offenders))

    def test_every_flag_is_read_through_the_shared_reader(self, H):
        """The other half, so the guard above cannot pass by the reads having
        been deleted instead of fixed."""
        src = (HERE / "handsoff.py").read_text(encoding="utf-8")
        for key in self.FLAGS:
            assert f'_setting_flag("{key}"' in src, key

    def test_the_shared_reader_is_not_a_second_implementation(self, H):
        """One strict flag reader in the tree, not two: the monolith's helper
        must delegate to `core.tools.setting_flag`."""
        body = inspect.getsource(H._setting_flag)
        assert "_core_tools.setting_flag(" in body

    def test_every_way_of_writing_off_reads_as_off(self, H, monkeypatch):
        for key, default in self.FLAGS.items():
            for raw in self.OFF_FORMS:
                monkeypatch.setitem(H.SETTINGS, key, raw)
                assert H._setting_flag(key, default) is False, (
                    f'{key}={raw!r} read as ON — bool({raw!r}) is {bool(raw)!r}')

    def test_a_truthy_form_still_reads_as_on(self, H, monkeypatch):
        """Strictness must not cost a legitimate hand-written "on"."""
        for key, default in self.FLAGS.items():
            for raw in ("true", "yes", "on", "1", "TRUE", 1, True):
                monkeypatch.setitem(H.SETTINGS, key, raw)
                assert H._setting_flag(key, default) is True, (key, raw)

    def test_a_missing_key_reads_its_documented_default(self, H, monkeypatch):
        for key, default in self.FLAGS.items():
            monkeypatch.delitem(H.SETTINGS, key, raising=False)
            assert H._setting_flag(key, default) is default, key

    def test_junk_takes_the_default_and_warns_once_per_key(
            self, H, monkeypatch, caplog):
        """A junk value is not repaired by reading it, and these reads sit on
        TIMER and per-turn paths (`streaming_tts` once per tool round, the
        three tick gates, the wake tests per utterance) — so the journal gets
        one line per key, not one per tick."""
        monkeypatch.setattr(H._core_tools, "_FLAG_WARNED", set())
        monkeypatch.setitem(H.SETTINGS, "resource_alerts", "nonsense")
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                assert H._setting_flag("resource_alerts", False) is False
        said = [r.getMessage() for r in caplog.records
                if "resource_alerts" in r.getMessage()]
        assert len(said) == 1, said
        # ...and a second key is still reported; the bound is per key, not one
        # line for the whole process.
        monkeypatch.setitem(H.SETTINGS, "briefing", "nonsense")
        with caplog.at_level(logging.WARNING):
            H._setting_flag("briefing", False)
        assert [r.getMessage() for r in caplog.records
                if "briefing" in r.getMessage()]

    def test_a_pure_gate_leaves_the_dict_alone(self, H, monkeypatch):
        """No repair: a tick gate or a per-turn switch must not mutate
        SETTINGS from a timer or audio thread for a value nothing re-reads."""
        monkeypatch.setitem(H.SETTINGS, "streaming_tts", "false")
        assert H._setting_flag("streaming_tts", True) is False
        assert H.SETTINGS["streaming_tts"] == "false"

    def test_the_durable_reads_repair_what_they_find(self, H, monkeypatch):
        """`repair=True` is the read-side twin of what `_persist_setting`
        already does on the write side: the dict must not go on holding a value
        two readers disagree about."""
        for key in ("handsfree", "notification_reader"):
            monkeypatch.setitem(H.SETTINGS, key, "false")
            assert H._setting_flag(key, False, repair=True) is False
            assert H.SETTINGS[key] is False, key
        # a real bool is left exactly as it was
        for raw in (True, False):
            monkeypatch.setitem(H.SETTINGS, "handsfree", raw)
            H._setting_flag("handsfree", False, repair=True)
            assert H.SETTINGS["handsfree"] is raw

    def test_repair_never_touches_a_missing_key(self, H, monkeypatch):
        monkeypatch.delitem(H.SETTINGS, "handsfree", raising=False)
        assert H._setting_flag("handsfree", False, repair=True) is False
        assert "handsfree" not in H.SETTINGS


class TestToolResultsCarryTheirKind:
    """A tool result's failure flag is CARRIED, never re-derived from the text.

    `_execute` handed back a bare `(text, err)` and every caller decided for
    itself whether that text was a failure by testing
    `text.startswith("ERROR")` — seven copies of one convention, each free to
    disagree with the others and with the tool that produced the text. The
    convention now has ONE owner (`core.tools.tool_kind`) and the result says
    which kind it is; the flag is a consequence of that, so a typo'd prefix can
    no longer flip a failure into a success.
    """

    #: prose that merely CONTAINS the failure words — none of these is a failure
    NOT_FAILURES = (
        "ERRORS: many of them",
        "REFUSEDLY, the request stood",
        "error: lowercase prose is not the convention",
        "the ERROR was mine",
        "",
    )

    def test_the_result_still_unpacks_as_a_plain_pair(self, H):
        """Every caller and every test does `text, err = belt.execute(...)`.
        Changing what decides `err` must not change that shape."""
        r = H._core_tools.ToolResult("ERROR: boom", "error")
        text, err = r
        assert (text, err) == ("ERROR: boom", True)
        assert r.text == text and r.err is err and r.ok is False
        # and a list/dict round-trip still sees two elements
        assert len(tuple(r)) == 2 and list(r) == ["ERROR: boom", True]

    def test_the_flag_follows_the_kind_not_the_text(self, H):
        """The SAME text is a failure or not depending on the kind it was
        produced with. That is the whole point: the text cannot decide."""
        said = "nothing to see here"
        ok = H._core_tools.ToolResult(said, "ok")
        err = H._core_tools.ToolResult(said, "error")
        assert ok.text == err.text and ok.ok is True and err.err is True

    def test_an_unrecognised_kind_fails_closed(self, H):
        """A kind nobody defined must not read as success."""
        r = H._core_tools.ToolResult("all good, honestly", "banana")
        assert r.kind == "unknown" and r.ok is False and r.err is True

    def test_the_classifier_needs_a_real_word_boundary(self, H):
        kind = H._core_tools.tool_kind
        assert kind("ERROR: boom") == "error"
        assert kind("REFUSED: nope") == "refused"
        assert kind("  REFUSED: leading space") == "refused"
        assert kind("ERROR") == "error"          # bare prefix, end of string
        assert kind("just prose") == "ok"
        for text in self.NOT_FAILURES:
            assert kind(text) == "ok", text

    def test_execute_carries_the_kind_end_to_end(self, H, monkeypatch):
        """Through the real dispatch, not the class: an answer, a refusal and
        an unknown tool each come back with their own kind."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        assert belt.execute("job_status", {}).kind == "ok"
        # a tool that FAILS reports its own failure, rather than the caller
        # finding out by looking at what it said
        failed = belt.execute("read_file", {"path": "/nonexistent/nope.txt"})
        assert failed.kind == "error" and failed.err is True, failed
        # a disabled permission gate refuses before anything is dispatched
        monkeypatch.setitem(belt._perm, "press_keys", False)
        refused = belt.execute("press_keys", {"combo": "ctrl+c"})
        assert refused.kind == "refused" and refused.err is True
        assert refused.text.startswith("REFUSED")
        unknown = belt.execute("no_such_tool", {})
        assert unknown.kind == "error" and unknown.err is True

    def test_no_call_site_sniffs_the_failure_prefix_any_more(self, H):
        """The sweep, in the shape of the flag-read guard next door.

        A `startswith("ERROR")`/`startswith("REFUSED")` anywhere in the runtime
        or the app is a second copy of the convention growing back — and the
        copy that drifts is the one deciding whether work actually happened.

        Parsed with `ast`, not grepped: the docstrings that EXPLAIN this
        convention contain the same words, and a text sweep would flag the
        explanation as the defect.
        """
        import ast
        offenders = []
        for name in ("core/tools.py", "handsoff.py"):
            src = (HERE / name).read_text(encoding="utf-8")
            lines = src.splitlines()
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute)
                        and fn.attr == "startswith"):
                    continue
                for arg in node.args:
                    elems = (arg.elts if isinstance(arg, (ast.Tuple, ast.List))
                             else [arg])
                    for el in elems:
                        v = getattr(el, "value", None)
                        if not isinstance(v, str):
                            continue
                        if v.lstrip().upper().startswith(("ERROR", "REFUSED")):
                            offenders.append(
                                f"{name}:{node.lineno}: "
                                f"{lines[node.lineno - 1].strip()}")
        assert not offenders, (
            "a call site is re-deriving failure from the text — read "
            "`.kind`/`.ok` (or core.tools.tool_kind) instead:\n"
            + "\n".join(offenders))

    def test_the_host_uses_the_one_classifier_at_its_string_seams(self, H):
        """The two host paths that cannot get a ToolBelt result (a direct
        method call and a subsystem that answers in the same shape) still ask
        the shared classifier rather than spelling the prefixes again."""
        src = (HERE / "handsoff.py").read_text(encoding="utf-8")
        assert "_core_tools.tool_kind(" in src


class TestTheStartupFlagReads:
    """The two reads that establish DURABLE state, driven through the real
    `Assistant.__init__` rather than the helper: a junk value must not start
    hands-free, and must not start the monitor that reads private desktop
    notifications aloud."""

    @pytest.fixture()
    def junk_init(self, H, monkeypatch):
        """Factory for a REAL `Assistant.__init__` with a junk flag set.

        The two reads under test are the STARTUP ones, so a `__new__` stub
        would not exercise them at all — and the real `__init__` starts
        workers, so every instance is shut down again. An `Assistant` that
        outlives its test is exactly what the suite's worker-leak fixture
        fails on, and one did before this teardown existed.
        """
        made = []
        monkeypatch.setattr(H.ContinuousListener, "start",
                            lambda self: pytest.fail("listener started"))

        def build(key, raw):
            monkeypatch.setitem(H.SETTINGS, key, raw)
            starts = []
            monkeypatch.setattr(H.Assistant, "_set_notification_reader",
                                lambda self, on: starts.append(on) or "ok")
            a = H.Assistant()
            made.append(a)
            return a, starts

        yield build
        for a in made:
            a.shutdown()

    def test_junk_handsfree_does_not_turn_hands_free_on(self, H, junk_init):
        a, _ = junk_init("handsfree", "false")
        assert a._handsfree is False, (
            "bool('false') is True — hands-free started from a value that asked "
            "for it to be off")
        assert H.SETTINGS["handsfree"] is False   # and the dict agrees now

    def test_junk_notification_reader_does_not_start_the_monitor(
            self, H, junk_init):
        """The privacy gate: this reader announces private desktop
        notifications out loud, so the one direction that must never fail open
        is junk reading as ON."""
        for raw in ("false", "no", "off", "nonsense", None, []):
            a, starts = junk_init("notification_reader", raw)
            assert starts == [], (raw, starts)
            assert H.SETTINGS["notification_reader"] is False, raw

    def test_a_real_true_still_starts_both(self, H, junk_init):
        """...and the flags still work when they are actually on."""
        a, starts = junk_init("notification_reader", True)
        assert starts == [True]
        a, _ = junk_init("handsfree", True)
        assert a._handsfree is True


class TestReminderStoreSeam:
    """The reminder queue was extracted into core.assistant.ReminderStore, and
    the store is rebound to H.REMINDERS_FILE/H.REMINDERS_LOCK on every call.

    That seam is load-bearing: a store that captured its path at construction
    time kept writing the REAL reminders.json from tests (spurious reminders
    spoken out loud). These pins fail if the late binding is ever removed.
    """

    def _redirect(self, H, monkeypatch, tmp_path):
        target = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", target)
        monkeypatch.setattr(H, "REMINDERS_LOCK", H.threading.RLock())
        return target

    def test_aliases_write_to_the_rebound_path(self, H, monkeypatch, tmp_path):
        target = self._redirect(H, monkeypatch, tmp_path)
        H._update_reminders(lambda items: items + [{"name": "seam", "due": 1.0}])
        assert [r["name"] for r in json.loads(target.read_text())] == ["seam"]
        assert [r["name"] for r in H._load_reminders()] == ["seam"]
        # and the store object itself was repointed, not left on the old file
        assert H._REMINDER_STORE.path == target

    def test_take_missed_uses_the_rebound_path(self, H, monkeypatch, tmp_path):
        target = self._redirect(H, monkeypatch, tmp_path)
        now = time.time()
        H._update_reminders(lambda items: [
            {"name": "past", "due": now - 5, "repeat_hours": 0},
            {"name": "future", "due": now + 600, "repeat_hours": 0},
        ])
        assert [r["name"] for r in H._take_missed_reminders()] == ["past"]
        assert [r["name"] for r in H._load_reminders()] == ["future"]
        assert json.loads(target.read_text())[0]["name"] == "future"

    def test_cancelling_the_last_reminder_actually_empties_the_queue(self, H, monkeypatch, tmp_path):
        """cancel_reminder's mutate returns [] when it drops the last entry.

        `mutate(items) or items` treated that falsy result as "no change" and
        saved the loaded list straight back, so cancelling the final reminder
        silently did nothing. mutate() returning [] must be honoured; only an
        explicit None means "edited in place".
        """
        target = self._redirect(H, monkeypatch, tmp_path)
        H._update_reminders(lambda items: items + [{"name": "only", "due": 1.0}])
        assert [r["name"] for r in H._load_reminders()] == ["only"]
        # the real cancel path: core.tools.cancel_reminder builds exactly this
        H._update_reminders(lambda items: [r for r in items if r["name"] != "only"])
        assert H._load_reminders() == []
        assert json.loads(target.read_text()) == []

    def test_in_place_mutation_still_persists_without_a_return(self, H, monkeypatch, tmp_path):
        """A mutate that edits and returns None must not be reverted to None."""
        target = self._redirect(H, monkeypatch, tmp_path)
        H._update_reminders(lambda items: items + [{"name": "kept", "due": 1.0}])
        def _in_place(items):
            items[0]["due"] = 42.0
        H._update_reminders(_in_place)
        assert json.loads(target.read_text())[0]["due"] == 42.0

    def test_due_helper_and_core_share_one_implementation(self, H):
        from core.assistant import split_due_reminders
        items = [{"name": "a", "due": 1.0, "repeat_hours": 0},
                 {"name": "b", "due": 9.0, "repeat_hours": 0}]
        assert H._due_reminders(items, 5.0) == split_due_reminders(items, 5.0)


class _FakeStreamResponse:
    """Minimal NDJSON response: iterable of raw lines, usable as a context mgr."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def _ndjson(*pieces):
    return [(json.dumps({"message": {"role": "assistant", "content": p}}
                        ) + "\n").encode("utf-8") for p in pieces]


def _stream(q, cancel, urlopen):
    """Drive the real streamer (core.brain) with a stubbed urlopen."""
    from core import brain
    return brain.ollama_chat_stream([{"role": "user", "content": "hi"}], q,
                                    cancel, None, base="http://127.0.0.1:9",
                                    model="m", num_ctx=8192, guard=lambda: None,
                                    logger=logging.getLogger("test.stream"),
                                    state={}, urlopen=urlopen)


class TestSystemFirstMessages:
    """Ollama 0.32 refuses the WHOLE request with HTTP 500 ("system message
    must be at the beginning") when a system message follows the first.

    Measured live: the identical list 500s against qwen3.8:27b and is accepted
    by gemma4:latest, so the breakage reads as "broken brain on one model".
    The wire seam every request passes through folds such a note into the user
    turn and names it once in the journal, so a caller that reintroduces the
    shape cannot hand the user a 500 for their next question.
    """

    def _sent(self, H, messages, monkeypatch):
        from core import brain as core_brain
        sent = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"message": {"content": "ok"}}'

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode("utf-8")))
            return _Resp()

        monkeypatch.setattr(core_brain, "_TAIL_SYSTEM_WARNED", False)
        core_brain.ollama_chat(messages, None, base="http://127.0.0.1:9",
                               model="m", num_ctx=8192, guard=lambda: None,
                               logger=logging.getLogger("test.system.first"),
                               urlopen=fake_urlopen)
        return sent

    def test_a_tail_system_note_is_folded_into_the_user_turn(self, H, monkeypatch):
        sent = self._sent(H, [
            {"role": "system", "content": "MAIN"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "Facts you remember:\n- Quinton"},
            {"role": "user", "content": "what is my name"},
        ], monkeypatch)
        assert [m["role"] for m in sent["messages"]] == ["system", "user", "user"]
        assert sent["messages"][0]["content"] == "MAIN"
        assert sent["messages"][-1]["content"] == (
            "Facts you remember:\n- Quinton\n\nwhat is my name")
        assert "Facts you remember" not in sent["messages"][1]["content"]

    def test_a_clean_list_reaches_the_server_untouched(self, H, monkeypatch):
        clean = [{"role": "system", "content": "MAIN"},
                 {"role": "user", "content": "hi"}]
        sent = self._sent(H, clean, monkeypatch)
        assert sent["messages"] == clean

    def test_a_note_with_no_user_turn_still_reaches_the_model(self, H, monkeypatch):
        """Mid-tool-loop there is no user message after the note; the content
        must land somewhere a model can read rather than being dropped."""
        sent = self._sent(H, [
            {"role": "system", "content": "MAIN"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "result"},
            {"role": "system", "content": "Live hardware note:\nmic silent"},
        ], monkeypatch)
        assert [m["role"] for m in sent["messages"]] == ["system", "user", "tool"]
        assert "Live hardware note" in sent["messages"][1]["content"]

    def test_the_fold_is_named_once_in_the_journal(self, H, monkeypatch, caplog):
        from core import brain as core_brain
        logged = []

        class _Log:
            def warning(self, fmt, *args):
                logged.append(fmt % args if args else fmt)

        monkeypatch.setattr(core_brain, "_TAIL_SYSTEM_WARNED", False)
        tail = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
                {"role": "system", "content": "note"}, {"role": "user", "content": "q"}]
        core_brain._messages_system_first(tail, _Log())
        core_brain._messages_system_first(tail, _Log())
        assert len(logged) == 1, logged
        assert "note" in logged[0] and "500" in logged[0]

    def test_the_streaming_path_folds_it_too(self, H, monkeypatch):
        import queue as _queue
        from core import brain as core_brain
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode("utf-8")))
            return _FakeStreamResponse(_ndjson("Fine."))

        monkeypatch.setattr(core_brain, "_TAIL_SYSTEM_WARNED", False)
        q = _queue.Queue()
        core_brain.ollama_chat_stream(
            [{"role": "system", "content": "MAIN"},
             {"role": "user", "content": "hi"},
             {"role": "system", "content": "Facts"},
             {"role": "user", "content": "q"}], q, None, None,
            base="http://127.0.0.1:9", model="m", num_ctx=8192,
            guard=lambda: None, logger=logging.getLogger("test.system.first"),
            state={}, urlopen=fake_urlopen)
        assert [m["role"] for m in sent["messages"]] == ["system", "user", "user"]
        assert sent["messages"][-1]["content"] == "Facts\n\nq"
        assert list(q.queue) == ["Fine.", None], list(q.queue)


class TestStreamedReplyFiltering:
    """The speech filter must be narrow, and must fall silent on a barge-in.

    Two real defects: `not sentence.startswith("<")` silently swallowed
    legitimate replies that merely begin with '<' ("<3", "<5 minutes"), and an
    UNCLOSED <think> block streamed the model's reasoning into TTS. Separately,
    after a barge-in the pending tail was queued anyway and spoken over the
    user.
    """

    def test_unclosed_think_block_never_reaches_speech(self, H):
        assert _core_brain.strip_thinking("Sure. <think>internal reasoning") == "Sure."
        assert _core_brain.strip_thinking("<think>only reasoning") == ""

    def test_closed_think_block_still_stripped(self, H):
        assert _core_brain.strip_thinking("a<think>b</think>c") == "ac"

    def test_legitimate_less_than_replies_are_not_dropped(self, H):
        for good in ("<3 that's sweet", "it's <5 minutes away", "<html> is a tag"):
            assert _core_brain.is_leaked_markup(good) is False, good

    def test_control_tokens_are_still_dropped(self, H):
        for bad in ("<think>", "</think>", "<tool_call>", "<|im_start|>"):
            assert _core_brain.is_leaked_markup(bad) is True, bad

    def test_fallback_filter_matches_core_brain(self, H):
        """The no-core fallback must behave identically to the real filter."""
        from core import brain
        corpus = ["hello <think>secret", "a<think>b</think>c", "<3 sweet",
                  "it's <5 minutes", "<think>only", "plain.",
                  "<tool_call>{}", "<|im_start|>x", "[TOOL_CALLS] junk\nreal",
                  "mixed <think>a</think> b<think>c"]
        for text in corpus:
            assert H._fallback_strip_thinking(text) == brain.strip_thinking(text), text
            assert (H._fallback_is_leaked_markup(text)
                    == brain.is_leaked_markup(text)), text

    def test_fallback_error_reader_matches_core_brain(self, H):
        """The third fallback gets the check the other two have.

        `_read_http_error` cannot BE core's: the branch that uses it is the one
        where `core/brain.py` could not be loaded. So what can be checked is
        that the two agree on the same corpus — and this check is exactly what
        was missing when the host's copy was reached through `_brain`, a name
        that branch never has, so the class could not even be built. The filters
        above have had this test since they were found to drift; the reader was
        the one nobody compared.
        """
        import urllib.error

        def error(body, code=400, reason="Bad Request"):
            return urllib.error.HTTPError("http://127.0.0.1:11434/api/chat",
                                          code, reason, None, io.BytesIO(body))

        bodies = [json.dumps({"error": "model does not support tools"}).encode(),
                  json.dumps({"error": ""}).encode(), b"{}",
                  b"not json at all", b"", b"<html>500</html>"]
        for body in bodies:
            assert (H._fallback_read_http_error(error(body))
                    == _core_brain._read_http_error(error(body))), body
        # the reason must survive as the fallback's answer, or an HTTP error with
        # a non-JSON body loses the only sentence that explains it
        assert H._fallback_read_http_error(
            error(b"<html>oops</html>", code=500, reason="Server Error")) \
            == "Server Error"

    def test_a_less_than_reply_is_actually_streamed(self, H):
        import queue as _queue
        q, cancel = _queue.Queue(), threading.Event()
        resp = _FakeStreamResponse(_ndjson("<3 that's sweet."))
        _stream(q, cancel, lambda req, timeout=None: resp)
        assert "<3 that's sweet." in list(q.queue), list(q.queue)

    def test_barge_in_does_not_speak_the_pending_tail(self, H):
        """Cancel mid-stream: nothing may be queued except the terminator."""
        import queue as _queue
        q, cancel = _queue.Queue(), threading.Event()

        def _lines():
            yield _ndjson("The answer is ")[0]
            cancel.set()            # the user starts talking here
            yield _ndjson("42")[0]

        class _Resp(_FakeStreamResponse):
            def __iter__(self):
                return _lines()

        _stream(q, cancel, lambda req, timeout=None: _Resp([]))
        assert list(q.queue) == [None], list(q.queue)


class TestWatcherPatternSafety:
    """A file watcher runs its pattern on every appended line, once a second,
    on a daemon thread that nothing can interrupt — and `re` has no timeout.
    The pattern is data the model or the user hands us, so the exponential
    shapes are refused up front and the text any single evaluation sees is
    capped."""

    def test_the_refused_shape_really_is_catastrophic(self):
        """Prove the refusal protects against something real, by measuring it.

        `(a+)+$` against a 29-character NON-matching line runs for minutes —
        every added character doubles the work — so a watcher handed this
        pattern would pin a core and stop watching for good. Measured in a
        subprocess so the suite stays fast.

        Through `run_driver` like every other interpreter child: it imports
        only `re`, but one constructor for all of them means the sandbox
        cannot be remembered at one call site and forgotten at the next.
        """
        probe = ("import re\n"
                 "re.compile(r'(a+)+$').search('a' * 28 + '!' + ' ' * 100)\n"
                 "print('returned')\n")
        with pytest.raises(subprocess.TimeoutExpired):
            run_driver(["-c", probe], timeout=2.0,
                       check=True, capture_output=True)

    def test_watch_file_refuses_exponential_patterns(self, H, tmp_path):
        p = tmp_path / "x.log"
        p.write_text("")
        tb = H.ToolBelt(on_restart_pending=lambda: None)
        for bad in ("(a+)+$", r"(\d+)*", "(.*x){4}"):
            out = tb.watch_file(str(p), bad, "start")
            assert out.startswith("REFUSED"), (bad, out)
            assert "exponentially" in out, (bad, out)
        assert tb.watch_file(str(p), "x" * 400, "start").startswith("REFUSED")
        assert tb.watch_file(str(p), action="list").endswith("none")

    def test_ordinary_patterns_are_untouched(self, H, tmp_path):
        """The guard names only shapes it can be sure about: a legitimate
        pattern must never be refused on a guess."""
        p = tmp_path / "x.log"
        p.write_text("")
        tb = H.ToolBelt(on_restart_pending=lambda: None)
        for good in ("ERROR|WARN", r"\b(foo|bar)\b", r"^\d{4}-\d{2}",
                     "(ERROR|WARN): .*", "[a-z]+=[0-9]+"):
            assert "watching" in tb.watch_file(str(p), good, "start"), good
        tb.stop_watchers()

    def test_a_burst_is_bounded_per_poll_and_deferred_not_dropped(self, H, tmp_path):
        """The per-poll line cap must bound work WITHOUT losing lines.

        Examining only the first N lines of a burst while advancing the cursor
        past the whole chunk would drop the rest forever — the cap has to defer
        them to the next poll instead.
        """
        from core.tools import WATCH_LINES_PER_POLL
        p = tmp_path / "burst.log"
        p.write_text("")                     # a watcher arms at the CURRENT end
        total = WATCH_LINES_PER_POLL + 100

        seen: list = []
        stop = threading.Event()
        t = threading.Thread(target=H.ToolBelt._file_watch_loop,
                             args=(p, re.compile("hit"), stop, seen.append),
                             daemon=True)
        t.start()
        first_poll = None
        try:
            # Arming is unobservable by design (a watcher "starts at the
            # current end"), so wait out a full poll interval: arming happens
            # BEFORE the loop's first stop.wait(1.0), so once an iteration has
            # completed the loop is provably parked at offset 0 of a file that
            # was still empty — which makes the burst below unambiguously new
            # data rather than a race with the first stat().
            time.sleep(1.2)
            p.write_text("".join(f"hit {i}\n" for i in range(total)))
            deadline = time.monotonic() + 5
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)
            # long enough for the whole first poll (the emits are microseconds)
            # and short enough that the second poll cannot have started
            time.sleep(0.4)
            first_poll = len(seen)
            deadline = time.monotonic() + 6
            while len(seen) < total and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            stop.set()
            t.join(3.0)
        assert first_poll == WATCH_LINES_PER_POLL, first_poll
        # …and the rest was deferred to the next poll, not skipped
        assert len(seen) == total, len(seen)
        assert [int(s.split()[-1]) for s in seen] == list(range(total))

    def test_the_cursor_counts_bytes_so_a_non_ascii_line_is_announced_once(
            self, H, tmp_path):
        """`position` is a byte offset (it is seeded from st_size and seeked
        to), but the loop read the file as text and advanced by the CHARACTERS
        it consumed. One line with an accent or CJK left the cursor short by the
        difference, so every later poll re-read the tail of that line from the
        middle of a character and announced the same match again as garbled
        fragments ('本語のエラー', '\ufffd\ufffdエラー'). Driven synchronously: the
        stop object's wait() IS the poll clock, so no test sleeps a second."""
        class _Polls:
            def __init__(self, steps):
                self.steps = list(steps)

            def wait(self, timeout=None):
                if not self.steps:
                    return True
                self.steps.pop(0)()
                return False

        p = tmp_path / "app.log"
        p.write_bytes(b"start\n")

        def append(data: bytes):
            def _do():
                with open(p, "ab") as fh:
                    fh.write(data)
            return _do

        seen: list = []
        noop = lambda: None
        stop = _Polls([
            append("ERROR: café déjà vu — 日本語のエラー\n".encode()),
            noop, noop, noop,                      # three quiet polls
            append(b"bad \xff\xfe bytes \xe3\x81 \xe3\x82\xa8\xe3\x83\xa9\xe3\x83\xbc\n"),
            noop, noop,
            append("後 ERROR: エラー ascii tail\n".encode()),
            noop, noop,
        ])
        H.ToolBelt._file_watch_loop(p, re.compile("エラー"), stop, seen.append)
        assert seen == [
            "app.log: ERROR: café déjà vu — 日本語のエラー",
            "app.log: bad \ufffd\ufffd bytes \ufffd エラー",
            "app.log: 後 ERROR: エラー ascii tail",
        ], seen


class _Source:
    """One parsed module for the closure rule: tree, parent map, and path.

    Threaded through the follow because a callee read in ANOTHER module needs
    the parent map of the tree it actually came from — `parents[node]` is only
    meaningful for the tree the node was parsed out of — and because the module
    that file imports from is the one that file names, not the one being
    scanned. `path` is None for a tree with no file (a string in a test), which
    is what keeps such a tree to same-file resolution.
    """

    __slots__ = ("tree", "parents", "path")

    def __init__(self, tree, parents, path=None):
        self.tree = tree
        self.parents = parents
        self.path = path


class TestTheScansShapesAreCheckedProperties:
    """Two shapes a full-tree scan found, as PROPERTIES rather than instances.

    Both were fixed at the one site each was found: a loader that rolled a
    `sys.modules` registration back in an `except Exception` handler (an
    interruption then left a half-executed module registered for the life of
    the process, and every later import adopted the corpse), and a bounded
    registry whose give-back was reachable only on the success path (a raising
    build permanently shrank the cap, so the registry refused work the machine
    could do). Repairing the instance is how such a shape comes back, so the
    properties are checked over the shipped source instead: a third instance
    fails here, where it is cheap.

    What they deliberately do NOT do: the loader rule judges each handler on
    its own, so a rollback in a narrow handler is refused even when a sibling
    handler would also roll back (conservative in the safe direction — it can
    demand a redundant rollback, never accept a hole); and the registry rule
    follows a lease lexically, so it pins the ONE place where caller-supplied
    code runs on behalf of a reservation rather than tracking leases across
    calls, and states that instead of implying more.
    """

    #: The cap that makes a class a bounded registry by SHAPE. Anything that
    #: refuses work at a cap and leases the slots it hands out is one of these,
    #: whatever it is called and wherever it lives.
    CAP_ATTR = "_cap"

    @staticmethod
    def _shipped():
        """Every shipped Python source: tests/ and attic/ are not shipped."""
        skip = {"attic", "tests", ".git", ".venv", "__pycache__", "build"}
        return [p for p in sorted(HERE.rglob("*.py"))
                if not any(part in skip for part in p.relative_to(HERE).parts)]

    @staticmethod
    def _self_attr(node):
        """`self.x` -> "x", else None."""
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            return node.attr
        return None

    @classmethod
    def _leases(cls, node) -> set:
        """Attributes `node` both increments and decrements — i.e. a LEASE.

        A counter with no decrement is a statistic (keys minted, refusals
        recorded) and owes nothing on any path; one with both is capacity being
        held, and that is the kind whose give-back has to be unconditional.
        Both decrement spellings count: `self._x -= 1` and `max(0, self._x - 1)`.
        """
        inc, dec = set(), set()
        for n in ast.walk(node):
            if isinstance(n, ast.AugAssign):
                name = cls._self_attr(n.target)
                if name is None:
                    continue
                if isinstance(n.op, ast.Add):
                    inc.add(name)
                elif isinstance(n.op, ast.Sub):
                    dec.add(name)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "max":
                for arg in n.args:
                    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Sub):
                        name = cls._self_attr(arg.left)
                        if name:
                            dec.add(name)
        return inc & dec

    def _rolls_back_sys_modules(self, handler) -> bool:
        """True when this handler restores or pops a sys.modules registration."""
        for node in ast.walk(handler):
            if (isinstance(node, ast.Subscript)
                    and self._self_attr(node.value) is None
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "modules"):
                return True
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("pop", "setdefault")
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "modules"):
                return True
        return False

    def test_every_loader_rollback_catches_base_exception(self):
        """A rollback that catches `Exception` is a rollback with a hole.

        `except Exception` does not see KeyboardInterrupt, SystemExit or
        MemoryError, so those exit the handler with the half-executed module
        still registered under the name the loader just wrote — against the
        `no half-initialized squat` promise each of these loaders makes in its
        own docstring. A handler may still CONTINUE for an ordinary exception
        and re-raise the rest (the host's candidate loop does exactly that);
        what it may not do is roll back and let a BaseException through with
        the registration left in place.
        """
        offenders = []
        scanned = 0
        for path in self._shipped():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                for handler in node.handlers:
                    if not self._rolls_back_sys_modules(handler):
                        continue
                    scanned += 1
                    caught = handler.type
                    if caught is None:
                        continue          # bare `except:` IS a BaseException catch
                    if isinstance(caught, ast.Name) and caught.id == "BaseException":
                        continue
                    offenders.append(f"{path.relative_to(HERE)}:{handler.lineno} "
                                     f"catches {ast.unparse(caught)}")
        assert scanned >= 3, (
            f"only {scanned} sys.modules rollback(s) found — a sweep that finds "
            "nothing is a broken sweep, not a passing one (the loaders in "
            "core/__init__.py and handsoff.py are its subjects)")
        assert not offenders, (
            "a loader rolls back a sys.modules registration in a handler that "
            "cannot see a BaseException, so an interruption leaves the "
            "half-executed module registered:\n" + "\n".join(offenders))

    def _bounded_registries(self):
        """Every shipped class that bounds admissions at a cap, by shape."""
        found = {}
        for path in self._shipped():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                holds_cap = any(
                    isinstance(n, ast.Assign)
                    and any(self._self_attr(t) == self.CAP_ATTR for t in n.targets)
                    or (isinstance(n, ast.AnnAssign)
                        and self._self_attr(n.target) == self.CAP_ATTR)
                    for n in ast.walk(cls))
                if holds_cap:
                    found[f"{path.relative_to(HERE)}::{cls.name}"] = cls
        return found

    def test_the_bounded_registry_census_is_pinned(self):
        """A second bounded registry must be a decision, not a discovery.

        The discipline below (take under the lock, give back on every path) is
        what makes a cap trustworthy, and it is only as good as the census: a
        new class that refuses work at a cap inherits none of it by default, so
        it fails here and is added deliberately — with these tests taught what
        it leases.
        """
        assert sorted(self._bounded_registries()) == [
            "core/registry.py::BoundedRegistry"], (
            "the set of cap-holding classes changed — teach "
            "TestTheScansShapesAreCheckedProperties what the new one leases "
            "(and that its gives-back are unconditional) before listing it here")

    def test_the_leased_slot_is_taken_under_the_lock(self):
        """The take is the lease, so it happens inside the registry's lock.

        Both halves matter: a take outside the lock races the room check that
        authorised it, and a second take site needs the give-back discipline
        applied there too — which this asserts by counting them.
        """
        registry = self._bounded_registries()["core/registry.py::BoundedRegistry"]
        leased = self._leases(registry)
        # Exactly ONE lease, found by shape rather than by name (so a rename is
        # not a failure) — a SECOND one is a second resource whose give-back
        # these tests would not be checking.
        assert len(leased) == 1, (
            f"leased counters (both incremented and decremented) = "
            f"{sorted(leased) or 'none'} — capacity being held, and this test "
            "checks exactly one")
        takes = [n for n in ast.walk(registry)
                 if isinstance(n, ast.AugAssign)
                 and self._self_attr(n.target) in leased
                 and isinstance(n.op, ast.Add)]
        assert len(takes) == 1, (
            f"{len(takes)} places take the slot; each needs its give-back "
            "checked on every path")
        take = takes[0]
        # Inside the SAME locked block as the room check, not merely inside SOME
        # `with self._lock`: two acquisitions make the take a second step, so a
        # second caller can pass `_has_room` in between and the cap admits one
        # more than it holds — the exact race the one-slot design prevents.
        same_step = False
        for n in ast.walk(registry):
            if not isinstance(n, ast.With):
                continue
            if not any(isinstance(item.context_expr, ast.Attribute)
                       and item.context_expr.attr == "_lock" for item in n.items):
                continue
            holds_take = take.lineno in range(n.lineno, (n.end_lineno or n.lineno) + 1)
            checks_room = any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "_has_room"
                for stmt in n.body for c in ast.walk(stmt))
            if holds_take and checks_room:
                same_step = True
        assert same_step, (
            f"the slot is taken (line {take.lineno}) outside the locked block "
            "that ran the room check, so admitting and taking are two steps")

    def test_caller_supplied_work_gives_the_slot_back_in_a_finally(self):
        """The give-back is a `finally`, not a success-path line.

        `build(key)` is the caller's code: it is the one thing here that can
        raise while a slot is held, and the give-back that used to sit after it
        is exactly how a failed `Popen` shrank the cap for ever. A `finally`
        cannot be made conditional by moving one line, which is why the shape
        is pinned rather than an equivalent `except BaseException: release;
        raise` handler.
        """
        registry = self._bounded_registries()["core/registry.py::BoundedRegistry"]
        leased = self._leases(registry)

        def releases(node) -> bool:
            for n in ast.walk(node):
                if (isinstance(n, ast.AugAssign)
                        and self._self_attr(n.target) in leased
                        and isinstance(n.op, ast.Sub)):
                    return True
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "max":
                    for arg in n.args:
                        if (isinstance(arg, ast.BinOp)
                                and isinstance(arg.op, ast.Sub)
                                and self._self_attr(arg.left) in leased):
                            return True
            return False

        # The methods that DO release (by name), so a `finally` may give the
        # slot back through either of them rather than inline.
        releasers = {fn.name for fn in registry.body
                     if isinstance(fn, ast.FunctionDef)
                     and any(releases(n) for n in fn.body)}
        assert releasers, "nothing in the class gives the leased slot back"

        checked, offenders = 0, []
        for fn in [n for n in registry.body if isinstance(n, ast.FunctionDef)]:
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try):
                    continue
                if not any(isinstance(c, ast.Call)
                           and isinstance(c.func, ast.Name)
                           and c.func.id in params
                           for stmt in node.body for c in ast.walk(stmt)):
                    continue        # not caller-supplied work
                checked += 1
                gives_back = any(
                    releases(stmt) or any(
                        isinstance(c, ast.Call)
                        and getattr(c.func, "attr", None) in releasers
                        for c in ast.walk(stmt))
                    for stmt in node.finalbody)
                if not gives_back:
                    offenders.append(f"{fn.name}() at line {node.lineno}")
        assert checked >= 1, (
            "no caller-supplied call is wrapped in a try any more — this "
            "sweep has lost its subject")
        assert not offenders, (
            "caller-supplied work runs with a slot held and the give-back is "
            "not in the try's `finally`, so a raise here leaks capacity "
            "for ever:\n" + "\n".join(offenders))

    # -- the third shape: a worker defined inside a loop ----------------------

    @staticmethod
    def _assigned_in(node) -> set:
        """Names this statement's body assigns, without entering a nested scope.

        A `def` inside the loop is its own scope, and so is a comprehension: the
        names they bind are theirs, not the loop's, so they cannot be the ones a
        worker reads back on a later pass.
        """
        out: set[str] = set()

        def targets(t) -> None:
            if isinstance(t, ast.Name):
                out.add(t.id)
            elif isinstance(t, ast.Starred):
                targets(t.value)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for el in t.elts:
                    targets(el)

        def visit(n) -> None:
            for child in ast.iter_child_nodes(n):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda, ast.ClassDef,
                                      ast.comprehension)):
                    continue
                if isinstance(child, ast.Assign):
                    for t in child.targets:
                        targets(t)
                elif isinstance(child, (ast.AugAssign, ast.AnnAssign)):
                    targets(child.target)
                elif isinstance(child, (ast.For, ast.AsyncFor)):
                    targets(child.target)
                elif isinstance(child, ast.withitem) \
                        and child.optional_vars is not None:
                    targets(child.optional_vars)
                elif isinstance(child, ast.ExceptHandler) and child.name:
                    out.add(child.name)
                visit(child)

        visit(node)
        return out

    @classmethod
    def _reads_and_defaults(cls, fn) -> tuple:
        """(names the def reads from its enclosing scope, names bound as defaults)."""
        local = {a.arg for a in (fn.args.posonlyargs + fn.args.args
                                 + fn.args.kwonlyargs)}
        if fn.args.vararg:
            local.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            local.add(fn.args.kwarg.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
        bound = {n.id for d in (list(fn.args.defaults)
                                + [d for d in fn.args.kw_defaults if d])
                 for n in ast.walk(d) if isinstance(n, ast.Name)}
        reads = {n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        return reads - local, bound

    @staticmethod
    def _target_into(target, out) -> None:
        """`for a, (b, c) in …`, starred targets and comprehensions, one walker."""
        if isinstance(target, ast.Name):
            out.add(target.id)
        elif isinstance(target, ast.Starred):
            TestTheScansShapesAreCheckedProperties._target_into(target.value, out)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for el in target.elts:
                TestTheScansShapesAreCheckedProperties._target_into(el, out)

    @classmethod
    def _loop_targets(cls, loop) -> set:
        """Names the loop binds through its OWN target: `for action, x in …`.

        Not an assignment inside the body, and missing it was a real hole in
        the first version of this rule: every closure reading the ITEM variable
        — the most ordinary thing a loop closure does — looked bound-and-safe.
        """
        out: set = set()
        if isinstance(loop, (ast.For, ast.AsyncFor)):
            cls._target_into(loop.target, out)
        return out

    #: Calls that CONSUME a closure inside the iteration that made it, so a
    #: closure handed to one cannot outlive the names it reads. Everything NOT
    #: in here is ASSUMED to keep what it is given unless `_consumes` can read
    #: the callee's own body and watch it call the closure in place — the safe
    #: direction either way: the cost of the assumption is an `x=x` default, and
    #: the cost of the other is a worker writing into the next round's object.
    #: `map`/`filter` are deliberately absent — they return a lazy iterator that
    #: outlives the call, which is the opposite of consuming.
    CONSUMING = ("sorted", "min", "max", "any", "all", "sum", "list", "tuple",
                 "set", "frozenset", "dict", "len", "next", "print",
                 "reversed", "id", "repr", "format", "isinstance", "callable")

    @staticmethod
    def _parents(tree) -> dict:
        return {child: parent for parent in ast.walk(tree)
                for child in ast.iter_child_nodes(parent)}

    @staticmethod
    def _enclosing_of(node, tree, parents):
        """The innermost function (or the module) that CONTAINS `node`.

        The whole point of the escape test: a closure is safe as long as every
        use of it is a call inside the iteration, and "every use" has to be
        looked for one level up — in the function that holds the loop.
        """
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Module)):
                return node
        return tree

    @staticmethod
    def _within(node, scope, parents) -> bool:
        while node in parents:
            if node is scope:
                return True
            node = parents[node]
        return False

    @classmethod
    def _handed_on(cls, name, enclosing, scope, parents) -> str:
        """Why `name` is not just a local this iteration calls, or "".

        Two ways out, and both are how a per-iteration worker becomes
        reachable after its iteration: the name is READ as a value somewhere
        (`hold.append(f)`, `return f`), or it is CALLED from outside the loop
        (`f = lambda: x` … then `f()` after the loop — that call sees the last
        pass's `x`). A read that is the callee of a call inside the loop is the
        one case that stays local.
        """
        for node in ast.walk(enclosing):
            if not (isinstance(node, ast.Name) and node.id == name
                    and isinstance(node.ctx, ast.Load)):
                continue
            up = parents.get(node)
            if not (isinstance(up, ast.Call) and up.func is node):
                return f"`{name}` is read as a value"
            if not cls._within(node, scope, parents):
                return f"`{name}` is called from outside the loop"
        return ""

    @classmethod
    def _stored_on(cls, targets, scope, parents, enclosing) -> str:
        """Where an assigned closure goes: an attribute, or a name that leaks."""
        names: set = set()
        for t in targets:
            if isinstance(t, (ast.Attribute, ast.Subscript)):
                return f"stored on {ast.unparse(t)[:40]}"
            cls._target_into(t, names)
        for name in sorted(names):
            why = cls._handed_on(name, enclosing, scope, parents)
            if why:
                return f"bound to `{name}` and {why}"
        return ""

    #: path -> the parsed `_Source`, or None for a file that is not there.
    #: A module is asked for once per call site and never changes mid-run, so
    #: the parse happens once; keying by path keeps a re-run from re-reading
    #: `core/tools.py` for each of its call sites.
    _MODULE_CACHE: dict = {}

    @staticmethod
    def _defs_named(tree, name) -> list:
        return [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name]

    @classmethod
    def _module(cls, path, root):
        """The parsed module at `path`, for a file INSIDE `root`, else None.

        The root bound is the whole safety of crossing a file: `from
        PySide6.QtCore import QTimer` resolves to no file here, and even if a
        name did collide with an installed library, following it would make a
        verdict depend on somebody else's implementation rather than on this
        checkout's code. This is the ONE place the bound is enforced — a
        second copy further out could never be falsified on its own, and a
        guard nobody can fail is not a guard.
        """
        try:
            key = path.resolve()
            key.relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        if key not in cls._MODULE_CACHE:
            source = None
            try:
                tree = ast.parse(key.read_text(encoding="utf-8"), str(key))
            except (OSError, SyntaxError, UnicodeDecodeError):
                tree = None
            if tree is not None:
                source = _Source(tree, cls._parents(tree), key)
            cls._MODULE_CACHE[key] = source
        return cls._MODULE_CACHE[key]

    @classmethod
    def _module_path(cls, dotted, source, root, home=None):
        """The file a dotted module name names inside `root`, else None.

        Two homes are tried, because both are how this repository imports
        itself: the importing file's OWN directory (tests do `from conftest
        import …`; `core/bubble.py` does `from .theme import …`, whose home is
        the package it sits in) and the checkout root (`core/assistant.py` does
        `from core.registry import …`). Whether the file found is inside `root`
        is `_module`'s question: it is asked once, where the file is read, so
        the bound has exactly one expression and one way to fail.
        """
        if root is None or source is None or source.path is None or not dotted:
            return None
        homes = [home if home is not None else source.path.parent, root]
        for base in homes:
            for cand in (base.joinpath(*dotted.split(".")).with_suffix(".py"),
                         base.joinpath(*dotted.split("."), "__init__.py")):
                if cand.is_file():
                    return cand
        return None

    @classmethod
    def _imports(cls, source, root) -> dict:
        """{name in this module: (the file it names, the attribute or None)}.

        `import core.brain` binds `core` and the dotted `core.brain` (both to
        core/brain.py, the package's own `__init__` being a different question);
        `import core.brain as b` binds `b`; `from core.brain import take_it`
        binds `take_it` → (core/brain.py, "take_it"), and `from core import
        registry` binds `registry` → the MODULE itself (attribute None). A
        relative import resolves against the package it sits in.        A star import,
        or any name that is no file here, is simply absent — which the caller
        reads as "assumed to keep what it is given".

        A name imported and then REBOUND at module level (`from harness import
        take_it` … `take_it = keep_it`) is not the import any more, so it is
        dropped from the table rather than followed to a body nobody calls.
        Lexical like the rest of the rule, and stated: only module level is
        checked, so a rebinding inside a function is not seen — and dropping a
        name is the safe direction (it costs a needless `x=x` default, where
        following the wrong body can miss an escape).
        """
        out: dict = {}
        for node in ast.walk(source.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    path = cls._module_path(alias.name, source, root)
                    if path is None:
                        continue
                    out[alias.name] = (path, None)
                    out.setdefault(alias.name.split(".")[0], (path, None))
                    if alias.asname:
                        out[alias.asname] = (path, None)
            elif isinstance(node, ast.ImportFrom):
                if node.level and source.path is None:
                    continue                # relative to what? a string tree
                home = None
                if node.level:
                    home = source.path.parent
                    for _ in range(node.level - 1):
                        home = home.parent
                base = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sub = f"{base}.{alias.name}" if base else alias.name
                    path = cls._module_path(sub, source, root, home)
                    attr = None
                    if path is None and base:
                        path = cls._module_path(base, source, root, home)
                        attr = alias.name
                    if path is not None:
                        out[alias.asname or alias.name] = (path, attr)
        rebound: set = set()
        for node in source.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    cls._target_into(target, rebound)
            elif isinstance(node, ast.AnnAssign):
                cls._target_into(node.target, rebound)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                rebound.add(node.name)
        for name in rebound & set(out):
            del out[name]
        return out

    @staticmethod
    def _dotted_candidates(func) -> list:
        """`a.b.c` → ["a.b.c", "a.b", "a"] — longest first, so the most
        specific module wins. A call on an expression (`Holder().commit`,
        `sink.append`) yields none: its value is not an import.
        """
        parts: list = []
        node = func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return []
        parts.append(node.id)
        parts.reverse()
        return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]

    @classmethod
    def _imported_def(cls, dotted, source, root):
        """The def a dotted name names through THIS module's imports, or None.

        That a name is imported is not yet an answer: `from core.registry
        import BoundedRegistry` binds a CLASS, and looking for a def of that
        name finds none, which is precisely how a class, a constant or an
        imported lambda declines to be followed.
        """
        binding = cls._imports(source, root).get(dotted)
        if binding is not None and binding[1] is not None:
            target = cls._module(binding[0], root)
            defs = [] if target is None else cls._defs_named(target.tree,
                                                            binding[1])
            if len(defs) == 1:
                return defs[0], 0, target
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            head, tail = ".".join(parts[:cut]), parts[cut:]
            if len(tail) != 1:
                continue
            bound = cls._imports(source, root).get(head)
            path = bound[0] if bound is not None and bound[1] is None else None
            if path is None:
                path = cls._module_path(head, source, root)
            target = None if path is None else cls._module(path, root)
            if target is None:
                continue
            defs = cls._defs_named(target.tree, tail[0])
            if len(defs) == 1:
                return defs[0], 0, target
        return None

    @classmethod
    def _definition_of(cls, func, source, root=None):
        """(the def this call names, its `self` offset, the module), or None.

        Same file FIRST, by UNIQUE name — two defs called `take` leave the call
        ambiguous, and following the wrong body is how this rule would start
        missing escapes. Then, when the name came from an import, the module it
        names in this checkout: an imported consumer is read, not assumed. An
        attribute call on a MODULE (`hardware._probe`, `core.brain.take_it`) is
        a function, so no `self` is skipped; one on anything else
        (`self._run(fn)`, `slot.commit(fn)`) is a METHOD, and its first
        parameter is the instance.
        """
        if isinstance(func, ast.Name):
            wanted, offset = func.id, 0
        elif isinstance(func, ast.Attribute):
            wanted, offset = func.attr, 1
        else:
            return None
        same = cls._defs_named(source.tree, wanted)
        if len(same) == 1:
            return same[0], offset, source
        for dotted in cls._dotted_candidates(func):
            hit = cls._imported_def(dotted, source, root)
            if hit is not None:
                return hit
        return None

    @staticmethod
    def _parameter_for(call, arg, callee, offset):
        """The parameter this argument binds to, or None when it is unknowable.

        A `*args` splat shifts the positions, a `**kwargs` splat hides the
        keyword, and a `*fns` parameter collects a tuple the callee can index
        later — none of those can be read, so none of them are guessed at.
        """
        if any(isinstance(a, ast.Starred) for a in call.args):
            return None
        params = list(callee.args.posonlyargs) + list(callee.args.args)
        for i, node in enumerate(call.args):
            if node is arg:
                i += offset
                return params[i].arg if i < len(params) else None
        for kw in call.keywords:
            if kw.value is arg and kw.arg:
                return kw.arg
        return None

    @staticmethod
    def _directly_in(node, func, parents) -> bool:
        """True when `node` runs in `func`'s own body, not a nested scope.

        The distinction the follow turns on: `def take(fn): fn()` uses the
        closure DURING the call, while `def later(fn): Thread(target=lambda:
        fn()).start()` calls it from a worker that outlives the call.
        """
        while node in parents:
            node = parents[node]
            if node is func:
                return True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Lambda)):
                return False
        return False

    @classmethod
    def _consumes(cls, call, arg, source, root=None, chain=frozenset()) -> bool:
        """Does the callee USE this closure during the call, or KEEP it?

        The reason `threading.Thread` is not simply on the CONSUMING list:
        follow the callee instead of assuming. When its definition is in THIS
        tree under a unique name, bind the argument to its parameter and read
        what the body does with it — every use being a call in the callee's OWN
        body means the closure cannot outlive the call, which is exactly the
        case the blunt version demanded an `x=x` default for.

        A use that hands the parameter ON (`def through(fn): take_it(fn)`) is
        followed one level further rather than read as a store, because a
        wrapper that only passes the closure along consumes it exactly as far
        as its target does — and that target is read by the same code. The
        `chain` of (callee, parameter) pairs already walked makes a cycle of
        such wrappers a refusal instead of a loop.

        The callee may live in ANOTHER module of this checkout (`from harness
        import take_it`) — `root` is the checkout the follow is allowed inside,
        and the walk uses that module's own parent map, not the scanned file's.

        A store, a `return`, a hand-off from a NESTED scope (a thread, a timer)
        or a call the target does not consume means it can outlive. Anything
        unreadable — an external callee, a `*args` binding, an ambiguous name —
        falls back to "keeps it", which is the safe direction: the cost is a
        needless default, never a missed escape.
        """
        found = cls._definition_of(call.func, source, root)
        if found is None:
            return False
        callee, offset, here = found
        param = cls._parameter_for(call, arg, callee, offset)
        if param is None:
            return False
        declared = {a.arg for a in (callee.args.posonlyargs + callee.args.args
                                    + callee.args.kwonlyargs)}
        if param not in declared:
            return False
        if (id(callee), param) in chain:
            return False                     # a cycle of wrappers: not chased
        chain = chain | {(id(callee), param)}
        for node in ast.walk(callee):
            if not (isinstance(node, ast.Name) and node.id == param
                    and isinstance(node.ctx, ast.Load)):
                continue
            up = here.parents.get(node)
            if not isinstance(up, ast.Call):
                return False                 # kept, whatever the verb
            if not cls._directly_in(up, callee, here.parents):
                return False                 # called or passed on by a worker
            if up.func is not node and not cls._consumes(up, node, here, root,
                                                         chain):
                return False                 # handed ON to a callee that keeps
        return True

    @classmethod
    def _escapes(cls, fn, scope, source, enclosing, root=None) -> str:
        """How this closure leaves the iteration that defined it, or "".

        "" means CALLED IN THE ITERATION, which is the only case left alone:
        the names it closed over are still the ones the loop had. Every other
        shape is named, so a failure says which way the worker got out —
        stored, threaded, connected, returned, collected.
        """
        parents = source.parents
        if isinstance(scope, (ast.ListComp, ast.SetComp, ast.DictComp,
                              ast.GeneratorExp)):
            up = parents.get(fn)
            if isinstance(up, ast.Call) and up.func is fn:
                return ""                    # (lambda: i)() inside the scope
            return "the comprehension's result, one closure per element"
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A named def is a STATEMENT, not an expression: the question is
            # what happens to its NAME, not what contains it.
            return cls._handed_on(fn.name, enclosing, scope, parents)
        node = fn
        while node is not scope and node in parents:
            up = parents[node]
            if isinstance(up, ast.Call):
                if up.func is node:
                    return ""                   # (lambda: x)()
                callee = getattr(up.func, "attr", None) \
                    or getattr(up.func, "id", None) or ast.unparse(up.func)[:40]
                if callee in cls.CONSUMING:
                    return ""                   # sorted(…, key=lambda: x)
                if cls._consumes(up, node, source, root):
                    return ""                   # a readable def CALLS what it gets
                return f"handed to {callee}()"
            if isinstance(up, ast.Assign):
                return cls._stored_on(up.targets, scope, parents, enclosing)
            if isinstance(up, ast.AnnAssign):
                return cls._stored_on([up.target], scope, parents, enclosing)
            if isinstance(up, (ast.Return, ast.Yield)):
                return "returned"
            if isinstance(up, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
                return "put in a container literal"
            node = up
        return ""

    @classmethod
    def _closure_offenders(cls, tree, label, module=None, root=None) -> tuple:
        """(the closures it saw, by name, offenders) — the rule, one home.

        Two scopes, because they are the same mistake — a loop that rebinds a
        name a nested `def`/`lambda` reads (through its target or in its body),
        and a COMPREHENSION whose variable a closure inside it reads. And one
        condition, which is what this rule is FOR: the closure must be able to
        outlive its iteration. A closure invoked in the iteration it was
        defined in reads the names the loop has at that moment, correctly.

        The names are RETURNED rather than counted because the callers pin
        WHICH closures they expect to be judged (measured 2026-09-19: the
        shipped half called `>= 3` the "three subjects" while the real answer
        was 12 unrelated closures and a PTT worker that is not in a loop at
        all — a count here is a floor a refactor can quietly walk off).

        `module` and `root` are what let the follow cross a FILE: with them, a
        callee imported from another module of this checkout is read instead of
        assumed, and a callee that resolves outside `root` is not followed. A
        tree with no `module` (a sample parsed from a string) stays same-file.
        """
        seen, offenders = [], []
        closures = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        source = _Source(tree, cls._parents(tree), module)
        parents = source.parents
        for scope in ast.walk(tree):
            if isinstance(scope, (ast.For, ast.AsyncFor, ast.While)):
                rebound = cls._assigned_in(scope) | cls._loop_targets(scope)
            elif isinstance(scope, (ast.ListComp, ast.SetComp, ast.DictComp,
                                    ast.GeneratorExp)):
                rebound = set()
                for gen in scope.generators:
                    cls._target_into(gen.target, rebound)
            else:
                continue
            if not rebound:
                continue
            enclosing = cls._enclosing_of(scope, tree, parents)
            for fn in [n for n in ast.walk(scope) if isinstance(n, closures)]:
                seen.append(getattr(fn, "name", "<lambda>"))
                reads, bound = cls._reads_and_defaults(fn)
                leaked = sorted((reads & rebound) - bound)
                if not leaked:
                    continue
                why = cls._escapes(fn, scope, source, enclosing, root)
                if why:
                    offenders.append(
                        (fn.lineno,
                         f"{label}:{fn.lineno} "
                         f"{getattr(fn, 'name', '<lambda>')}() reads {leaked} "
                         f"and escapes the iteration — {why}"))
        return seen, offenders

    #: A sample the rule MUST refuse — and, just as important, must LEAVE
    #: ALONE. The verdict travels WITH each line instead of being parsed back
    #: out of the text or counted: the first version of this sample used
    #: trailing comments as markers, which are Python comments and therefore
    #: not part of the string at all, so its expectation was silently EMPTY
    #: while the rule was working perfectly (measured 2026-09-19).
    #:   verdict = the substring the offender's REASON must name (or None)
    #:
    #: The second half carries the FOLLOW: `take_it(lambda: item2)` and
    #: `Holder().commit(lambda: item6)` are left alone because the callee's own
    #: body is read and it CALLS the closure during the call, while `keep_it`
    #: stores it, `later` calls it from a nested worker, `anyhow` takes `*fns`
    #: where no binding can be read, and `pick` is defined twice so the name is
    #: ambiguous and not followed at all. Each of those is one mutation away
    #: from passing, which is why they are in the sample rather than argued in
    #: a comment.
    CLOSURE_SAMPLE_LINES = (
        ("for item, extra in CASES:", None),
        ("    (lambda: item)()", None),                    # called in place
        ("    sorted(CASES, key=lambda k: item)", None),   # consumed here
        ("    obj.cb = lambda: extra", "stored on obj.cb"),
        ("    sink.append(lambda: item)", "handed to append()"),
        ("    th = threading.Thread(target=lambda: item)", "handed to Thread()"),
        ("def make():", None),
        ("    for _ in range(3):", None),
        ("        box = {}", None),
        ("        def worker():", "read as a value"),      # returned by its name
        ("            return box", None),
        ("        return worker", None),
        ("    return worker", None),
        ("[lambda: i for i in range(3)]", "one closure per element"),
        ("for extra2 in CASES:", None),
        ("    f = lambda: extra2", "bound to `f`"),        # then handed on
        ("    hold.append(f)", None),
        ("    f()", None),
        # -- the callee is FOLLOWED when its body is right here -------------
        ("def take_it(fn):", None),
        ("    fn()", None),
        ("for item2 in CASES:", None),
        ("    take_it(lambda: item2)", None),              # read: CALLED in place
        ("def keep_it(fn):", None),
        ("    hold.append(fn)", None),
        ("for item3 in CASES:", None),
        ("    keep_it(lambda: item3)", "handed to keep_it()"),
        ("def later(fn):", None),
        ("    threading.Timer(1, lambda: fn()).start()", None),
        ("for item4 in CASES:", None),
        ("    later(lambda: item4)", "handed to later()"),  # called from a worker
        ("def anyhow(*fns):", None),
        ("    fns[0]()", None),
        ("for item5 in CASES:", None),
        ("    anyhow(lambda: item5)", "handed to anyhow()"),   # unknowable binding
        ("class Holder:", None),
        ("    def commit(self, fn):", None),
        ("        self.n = 1", None),                   # the arg binds PAST self
        ("        fn()", None),
        ("for item6 in CASES:", None),
        ("    Holder().commit(lambda: item6)", None),      # read: a method call
        ("def pick(fn):", None),
        ("    fn()", None),
        ("def pick(fn):", None),                        # same name twice: not
        ("    hold.append(fn)", None),                  # followed at all
        ("for item7 in CASES:", None),
        ("    pick(lambda: item7)", "handed to pick()"),
        ("def both(fn):", None),
        ("    fn()", None),                                 # called in place...
        ("    hold.append(fn)", None),                     # ...and KEPT: unsafe
        ("for item8 in CASES:", None),
        ("    both(lambda: item8)", "handed to both()"),
        # -- a wrapper that only hands it ON is followed one level further --
        ("def through(fn):", None),
        ("    take_it(fn)", None),                         # passed to a consumer
        ("for item9 in CASES:", None),
        ("    through(lambda: item9)", None),              # read: consumed downstream
        ("def relay(fn):", None),
        ("    keep_it(fn)", None),                         # passed to a keeper
        ("for item10 in CASES:", None),
        ("    relay(lambda: item10)", "handed to relay()"),
        ("def handoff(fn):", None),
        ("    threading.Timer(1, lambda: take_it(fn)).start()", None),
        ("for item11 in CASES:", None),
        ("    handoff(lambda: item11)", "handed to handoff()"),   # from a worker
        ("def ping(fn):", None),
        ("    pong(fn)", None),                            # two wrappers that
        ("def pong(fn):", None),                          # hand it back and
        ("    ping(fn)", None),                            # forth: REFUSED
        ("for item12 in CASES:", None),
        ("    ping(lambda: item12)", "handed to ping()"),
        ("def stash(fn):", None),
        ("    parked = fn", None),                        # kept by an ASSIGNMENT,
        ("for item13 in CASES:", None),                 # not by a call
        ("    stash(lambda: item13)", "handed to stash()"),
    )

    CLOSURE_SAMPLE = "\n".join(line for line, _v in CLOSURE_SAMPLE_LINES) + "\n"

    #: line number -> the substring its reason must contain, for the escapes.
    CLOSURE_SAMPLE_FLAGS = {n: verdict
                            for n, (_line, verdict)
                            in enumerate(CLOSURE_SAMPLE_LINES, 1)
                            if verdict}

    def test_a_worker_defined_in_a_loop_binds_what_it_reads(self):
        """A closure that can OUTLIVE its iteration must bind those names.

        A closure reads the NAME, not the value: `turn` assigned once per pass
        of `for _round in range(MAX_TOOL_ROUNDS)` means a worker that outlives
        its round writes into the NEXT round's object. Measured 2026-09-18, the
        journal carries "stream worker slow to finish; waiting" twice — that is
        the window, and it is the same shape the PTT worker already avoids by
        writing `def _work(_rec=rec, _gen=gen, …)`. Binding is a one-line fix
        and an invisible one, which is exactly why it is checked here.

        WHICH closures get judged is pinned by name, not by a count. The PTT
        worker is the idiom this rule came from but not one of its subjects —
        it is defined in `finish_listening()`, not in a loop — so a `>= 3`
        floor was satisfied by twelve unrelated lambdas while naming nothing
        (measured 2026-09-19). Both brain workers are required by name instead.

        And the rule is about LIFETIME, not about the text: a closure called in
        the iteration that defined it reads the right names and is left alone,
        which the sample pins with its own unmarked lines. The expectation is
        carried line by line (the line, and the word its reason must name) and
        the flagged LINES are compared — a count was satisfied by the wrong
        lines once, and the branch that went blind stayed green because of it
        (2026-09-19).
        """
        seen, planted = self._closure_offenders(
            ast.parse(self.CLOSURE_SAMPLE), "<sample>")
        reasons = dict(planted)
        assert sorted(reasons) == sorted(self.CLOSURE_SAMPLE_FLAGS), (
            "the rule no longer flags exactly what the sample marks as an "
            "escape — a blind sweep reports a clean tree, which is "
            f"indistinguishable from one: flagged={sorted(reasons)} "
            f"expected={sorted(self.CLOSURE_SAMPLE_FLAGS)}")
        for line, want in sorted(self.CLOSURE_SAMPLE_FLAGS.items()):
            assert want in reasons.get(line, ""), (
                f"line {line} of the sample is an escape by a DIFFERENT route "
                f"than the one it names: its reason reads "
                f"{reasons.get(line)!r}, which does not say {want!r} — each "
                "way out (stored, threaded, connected, returned) is diagnosed "
                "by its own branch, and one branch covering for another means "
                "one of them is dead")
            assert self.CLOSURE_SAMPLE_LINES[line - 1][1] == want
        left_alone = [n for n, (_l, verdict) in
                      enumerate(self.CLOSURE_SAMPLE_LINES, 1) if not verdict]
        assert not set(left_alone) & set(reasons), (
            "the rule flagged a closure CALLED IN ITS OWN ITERATION (or a "
            "plain line), where the names it closed over are still the "
            "loop's — the sample carries no verdict there, which is the "
            f"promise that this is a lifetime rule: {planted}")
        assert len(seen) == 20, (
            f"the sample holds 20 closures inside a loop or comprehension, the "
            f"rule saw {len(seen)}: {seen} — a rule that stops finding them "
            "is blind, not clean")

        seen, offenders = [], []
        for path in self._shipped():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            found, bad = self._closure_offenders(
                tree, str(path.relative_to(HERE)), module=path, root=HERE)
            seen += found
            offenders += [msg for _ln, msg in bad]
        for worker in ("_run_stream", "_run_call"):
            assert worker in seen, (
                f"{worker} — a worker handed to a thread from inside "
                "`for _round in range(MAX_TOOL_ROUNDS)` — is not among the "
                f"{len(seen)} closures this sweep judged ({seen}), so the "
                "binding it carries is no longer being checked: it reads "
                "`turn`/`tools`/`box`, which the next pass rebinds, and an "
                "unbound worker writes its result into the NEXT round's turn")
        assert len(seen) >= 3, (
            f"only {len(seen)} closures inside a loop found in the shipped "
            "tree — the sweep has lost its subjects and must not report a "
            "pass")
        assert not offenders, (
            "a function defined inside a loop reads a name the loop rebinds, "
            "unbound: it will see whichever object the next pass put there — "
            "bind it as a default (def f(x=x)):\n" + "\n".join(offenders))

    def test_an_imported_helper_is_read_instead_of_assumed(self, tmp_path):
        """A callee in ANOTHER module of the same checkout is followed too.

        `from harness import take_it` used to be indistinguishable from
        `threading.Thread`, so a helper that calls its closure in place was
        assumed to KEEP it and a loop was told to bind a name it can never read
        again. The consumers here are real files, parsed from real paths, with
        real import statements — the same machinery the shipped sweep uses, not
        a fixture standing in for it — and each part fails a different way if
        the follow is wrong:

          * a helper that CALLS the closure is left alone, one that KEEPS it
            is not, and the one that is left alone must be left alone BECAUSE
            the imported body was read (not because its lambda reads nothing);
          * an ALIASED module (`import harness as h`) resolves too;
          * a name defined twice in the imported module is ambiguous, so it is
            not followed at all;
          * a name imported and then REBOUND at module level is not the import
            any more, so the call is judged on the rebound name (assumed kept)
            rather than on the body of a function nobody calls;
          * a RELATIVE import two levels up (`from ..harness import take_it`)
            resolves against the package it sits in;
          * and the same consumer analysed with a root that does NOT contain
            the helper resolves nothing — the follow never leaves the root.
        """
        pkg = tmp_path / "pkg"
        (pkg / "sub").mkdir(parents=True)
        (pkg / "harness.py").write_text(
            "def take_it(fn):\n"
            "    fn()\n"
            "def keep_it(fn):\n"
            "    hold.append(fn)\n", encoding="utf-8")
        (pkg / "twice.py").write_text(
            "def tap(fn):\n"
            "    fn()\n"
            "def tap(fn):\n"          # the same NAME twice: not followed
            "    hold.append(fn)\n", encoding="utf-8")
        cases = {
            "consumer_direct.py": (
                "from harness import take_it, keep_it\n"
                "for item in CASES:\n"
                "    take_it(lambda: item)\n"
                "for item2 in CASES:\n"
                "    keep_it(lambda: item2)\n"),
            "consumer_alias.py": (
                "import harness as h\n"
                "for item in CASES:\n"
                "    h.take_it(lambda: item)\n"),
            "consumer_twice.py": (
                "from twice import tap\n"
                "for item in CASES:\n"
                "    tap(lambda: item)\n"),
            "consumer_rebound.py": (
                "from harness import take_it, keep_it\n"
                "take_it = keep_it\n"          # rebound: not the import any more
                "for item in CASES:\n"
                "    take_it(lambda: item)\n"),
            "sub/deep.py": (
                "from ..harness import take_it\n"
                "for item in CASES:\n"
                "    take_it(lambda: item)\n"),
        }
        for name, text in cases.items():
            (pkg / name).write_text(text, encoding="utf-8")

        def judge(name, root):
            path = pkg / name
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            return self._closure_offenders(tree, name, module=path, root=root)

        seen, off = judge("consumer_direct.py", tmp_path)
        assert (len(seen), [ln for ln, _ in off]) == (2, [5]), (
            "the imported helper that CALLS the closure must be left alone and "
            f"the one that KEEPS it must not: seen={seen} offenders={off}")
        assert "handed to keep_it()" in off[0][1], off

        seen, off = judge("consumer_alias.py", tmp_path)
        assert (len(seen), [ln for ln, _ in off]) == (1, []), (
            f"`import harness as h` then `h.take_it(…)` is the same helper: "
            f"seen={seen} offenders={off}")

        seen, off = judge("consumer_twice.py", tmp_path)
        assert [ln for ln, _ in off] == [3], (
            "`tap` is defined TWICE in the imported module, so which body runs "
            f"is not knowable and the call is not followed: {off}")
        assert "handed to tap()" in off[0][1], off

        seen, off = judge("consumer_rebound.py", tmp_path)
        assert (len(seen), [ln for ln, _ in off]) == (1, [4]), (
            "`take_it = keep_it` at module level means the name is NOT the "
            "imported helper, so the call is not followed to a consuming body "
            f"and the closure is assumed kept: seen={seen} offenders={off}")
        assert "handed to take_it()" in off[0][1], off

        seen, off = judge("sub/deep.py", tmp_path)
        assert (len(seen), [ln for ln, _ in off]) == (1, []), (
            "`from ..harness import take_it`, two levels up in the package, "
            f"names the same helper: seen={seen} offenders={off}")

        other = tmp_path / "other"
        other.mkdir()
        seen, off = judge("consumer_direct.py", other)
        assert [ln for ln, _ in off] == [3, 5], (
            "with a root that does not contain the helper, nothing resolves and "
            "both closures are assumed to be kept — the follow never leaves "
            f"the root it was handed: {off}")

    def test_the_suite_holds_itself_to_the_worker_binding_rule(self):
        """The tests get the same rule. A fixture or a lambda handed to a
        thread reads the name too, and a suite that leaks state across its own
        cases is how an ORDER dependency gets born — this repository has fixed
        two of those already (the announce worker, the queued command).

        Swept today: 0 offenders in 10 closures inside a loop — nine in
        `for`/`while` bodies and one inside a comprehension, which became a
        subject of its own when comprehension scopes were added to the rule
        (that is why this count is 10 and the round before it measured 9). The
        ten are safe for reasons that are checkable rather than assumed — a
        lambda reading nothing from the loop, a lambda with the name already
        bound as a default, and one reading `idle`/`queue`, which the loop does
        not rebind. That is the point of pinning it: the next one is one line.
        """
        seen, offenders = [], []
        for path in sorted((HERE / "tests").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            found, bad = self._closure_offenders(
                tree, f"tests/{path.name}", module=path, root=HERE)
            seen += found
            offenders += [msg for _ln, msg in bad]
        assert len(seen) >= 10, (
            f"only {len(seen)} closure(s) inside a loop found in the suite — "
            "there are 10 today, so a sweep that finds fewer has lost its "
            "subject and must not report a pass")
        resolved = 0
        for path in sorted((HERE / "tests").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            source = _Source(tree, self._parents(tree), path)
            resolved += sum(1 for _p, attr in self._imports(source, HERE).values()
                            if attr is not None)
        assert resolved >= 5, (
            f"only {resolved} `from … import …` name(s) in the suite resolved to "
            "a file in this checkout — the import table has stopped reading "
            "this repository's own imports, so no callee can be followed "
            "across a module boundary any more")
        assert not offenders, (
            "a closure defined inside a loop in the SUITE reads a name the "
            "loop rebinds, unbound — a leak or an order dependency waiting for "
            "a shuffle to expose it:\n" + "\n".join(offenders))
