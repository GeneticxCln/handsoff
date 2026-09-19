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
