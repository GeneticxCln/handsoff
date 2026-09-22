"""The app's half of the Laya corpus: a completed turn is recorded as it happens.

`ci/laya_corpus.py` derives the family and folds the queue in; the app records
only facts it owns — the utterance, and the tool names the turn called. What is
pinned here is the JOINT between the two: that a completed turn is written
(including the calls the sealed history drops), that a failed write never becomes
a failed turn, that `--ptt health` carries the running count, and that the queue
the app writes is the queue the corpus folds without anyone running a command.
"""
from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

from conftest import HERE as ROOT

sys.path.insert(0, str(ROOT / "ci"))

import laya_corpus as corpus  # noqa: E402


def _call(name: str) -> dict:
    return {"function": {"name": name, "arguments": "{}"}}


class TestTheAppRecordsCompletedTurns:
    def _paths(self, H, monkeypatch, tmp_path: Path) -> Path:
        """Point the app's three corpus paths at a scratch directory."""
        queue = tmp_path / "laya-turns.jsonl"
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", queue)
        monkeypatch.setattr(H, "LAYA_CORPUS_FILE", tmp_path / "laya-corpus.jsonl")
        monkeypatch.setattr(H, "LAYA_TURNS_CURSOR", tmp_path / "laya-turns.cursor")
        return queue

    def test_one_line_per_turn_with_the_utterance_and_the_tools(self, H, monkeypatch,
                                                               tmp_path):
        queue = self._paths(H, monkeypatch, tmp_path)
        H._record_turn_for_corpus(
            "set a timer for ten minutes",
            [{"role": "assistant", "tool_calls": [_call("set_reminder")]}])
        lines = queue.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, lines
        rec = json.loads(lines[0])
        assert rec["text"] == "set a timer for ten minutes"
        assert rec["tools"] == ["set_reminder"]
        assert rec["ts"], "a recorded turn carries when it happened"
        assert queue.stat().st_mode & 0o777 == 0o600, \
            "the user's own sentences are as private as decisions.jsonl"

    def test_the_turn_s_own_messages_are_the_source_and_names_are_deduped(self, H):
        """A tool called twice in one turn is one choice, and the reading is of
        the turn's own messages — the record has to be about what the model
        chose, not about what the decision log happened to keep.
        """
        assert H._turn_tool_names([
            {"role": "assistant",
             "tool_calls": [_call("read_file"), _call("read_file")]},
            {"role": "tool", "tool_name": "read_file", "content": "ok"},
            {"role": "assistant", "tool_calls": [_call("edit_file")]},
            {"role": "assistant", "content": "done"},
        ]) == ["read_file", "edit_file"]

    def test_a_chat_turn_is_recorded_with_no_tools(self, H, monkeypatch, tmp_path):
        """\"Every completed turn\" means every one: a turn that called nothing is
        a real decision (answer directly), and the corpus is what decides
        whether it is usable — the app must not pre-filter it.
        """
        queue = self._paths(H, monkeypatch, tmp_path)
        H._record_turn_for_corpus("hello there", [{"role": "assistant", "content": "hi"}])
        rec = json.loads(queue.read_text(encoding="utf-8").strip())
        assert rec["tools"] == [] and rec["text"] == "hello there"

    def test_a_write_failure_never_breaks_a_turn(self, H, monkeypatch, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", blocker / "laya-turns.jsonl")
        H._record_turn_for_corpus(
            "set a timer", [{"role": "assistant", "tool_calls": [_call("set_reminder")]}])
        assert blocker.read_text(encoding="utf-8") == "not a directory"

    def test_the_utterance_is_capped(self, H, monkeypatch, tmp_path):
        queue = self._paths(H, monkeypatch, tmp_path)
        H._record_turn_for_corpus("x" * (H.LAYA_UTTERANCE_MAX + 500), [])
        rec = json.loads(queue.read_text(encoding="utf-8").strip())
        assert len(rec["text"]) == H.LAYA_UTTERANCE_MAX

    def test_health_counts_the_queue_the_fold_and_the_store(self, H, monkeypatch,
                                                           tmp_path):
        """The running count is the QUEUE (what the app has recorded); `pending`
        is what the next corpus read folds in; `grown_rows` is what the corpus
        holds after dedupe. Three numbers, each named for what it counts.
        """
        queue = self._paths(H, monkeypatch, tmp_path)
        queue.write_text("".join(json.dumps(r) + "\n" for r in (
            {"ts": "t1", "text": "one", "tools": ["set_reminder"]},
            {"ts": "t2", "text": "two", "tools": ["media_play"]})),
            encoding="utf-8")
        (tmp_path / "laya-corpus.jsonl").write_text("".join(
            json.dumps({"text": f"row {i}", "family": "media", "source": "turn:t"}) + "\n"
            for i in range(3)), encoding="utf-8")
        assert H._laya_corpus_counts() == {"turns_recorded": 2, "turns_pending": 2,
                                           "grown_rows": 3}
        (tmp_path / "laya-turns.cursor").write_text(
            json.dumps({"lines": 2, "hash": "x"}), encoding="utf-8")
        assert H._laya_corpus_counts()["turns_pending"] == 0, \
            "a folded queue is not pending work"

    def test_mic_health_carries_the_corpus_section(self, H, monkeypatch, tmp_path):
        """`--ptt health` is where a user sees this, so the key has to be in the
        snapshot the socket serves — and has to build on a host with no reader,
        which is exactly how the suite builds an Assistant.
        """
        self._paths(H, monkeypatch, tmp_path)
        monkeypatch.setattr(H, "ollama_available", lambda: True)
        a = H.Assistant.__new__(H.Assistant)
        a._state = "idle"
        a._handsfree = False
        a._followup_until = 0.0
        a._listener = types.SimpleNamespace(mic_snapshot=dict)
        snap = H.Assistant.mic_health(a)
        assert snap["laya_corpus"] == {"turns_recorded": 0, "turns_pending": 0,
                                       "grown_rows": 0}


class TestTheTurnTailRecordsIt:
    """Driven through the real `_brain_turn` tail, because WHERE the record is
    written is the point: before history is sealed, so a call whose reply never
    came is still recorded as the model's choice."""

    def _assistant(self, H, monkeypatch, tmp_path, rounds: list, offer: bool = False):
        queue = tmp_path / "laya-turns.jsonl"
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", queue)
        monkeypatch.setattr(H, "LAYA_CORPUS_FILE", tmp_path / "laya-corpus.jsonl")
        monkeypatch.setattr(H, "LAYA_TURNS_CURSOR", tmp_path / "laya-turns.cursor")
        a = H.Assistant.__new__(H.Assistant)
        a._tools = types.SimpleNamespace(
            _set_user_turn=lambda *_: None,
            _last_images=None,
            _last_confirmation_offer=offer,
            execute=lambda name, args: (f"did {name}", None))
        a._conversation_for = lambda text: [{"role": "user", "content": text}]
        a._gen = 1
        a._history = []
        a._turn_spoke = False
        a._save_history = lambda: None
        a._speak = lambda text, gen, cancel, sentence_q=None: None
        monkeypatch.setitem(H.SETTINGS, "streaming_tts", False)
        monkeypatch.setitem(H._BRAIN_STATE, "tools_supported", False)
        responses = list(rounds)
        monkeypatch.setattr(H, "ollama_chat",
                            lambda conversation, tools: responses.pop(0))
        return a, queue

    def test_a_completed_turn_lands_in_the_queue(self, H, monkeypatch, tmp_path):
        a, queue = self._assistant(H, monkeypatch, tmp_path, [
            {"content": "", "tool_calls": [_call("get_weather")]},
            {"content": "It is sunny."},
        ])
        H.Assistant._brain_turn(a, "what is the weather", 1, threading.Event())
        rec = json.loads(queue.read_text(encoding="utf-8").strip())
        assert rec["text"] == "what is the weather"
        assert rec["tools"] == ["get_weather"]

    def test_a_confirmation_offer_still_records_the_call_the_history_drops(
            self, H, monkeypatch, tmp_path):
        """Two calls, the first of which stops the turn for a confirmation: the
        second never gets its `role:tool` reply, so `_seal_tool_calls` strips the
        whole call list out of the published history — and those calls are
        exactly the model's routing choice. Recording after the seal loses them.
        """
        a, queue = self._assistant(H, monkeypatch, tmp_path, [
            {"content": "",
             "tool_calls": [_call("kill_process"), _call("media_play")]},
        ], offer=True)
        H.Assistant._brain_turn(a, "kill the thing on port 3000 and play music", 1,
                                threading.Event())
        rec = json.loads(queue.read_text(encoding="utf-8").strip())
        assert rec["tools"] == ["kill_process", "media_play"], rec
        published = [m for m in a._history if m.get("tool_calls")]
        assert not published, "the sealed history must hold no unanswered call"


class TestTheTwoHalvesAgree:
    def test_the_queue_the_app_writes_is_the_queue_the_corpus_folds(
            self, H, monkeypatch, tmp_path):
        """The whole point, end to end and with no command in it: the app records
        a turn, the corpus folds it on its next read, and `--ptt health` reports
        both halves — including the turn the option set cannot use, which is
        counted rather than dropped.
        """
        queue = tmp_path / "laya-turns.jsonl"
        store = tmp_path / "laya-corpus.jsonl"
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", queue)
        monkeypatch.setattr(H, "LAYA_CORPUS_FILE", store)
        monkeypatch.setattr(H, "LAYA_TURNS_CURSOR", tmp_path / "laya-turns.cursor")
        H._record_turn_for_corpus(
            "what is claude doing",
            [{"role": "assistant", "tool_calls": [_call("quant_space_read")]}])
        H._record_turn_for_corpus("hello there", [{"role": "assistant", "content": "hi"}])
        assert H._laya_corpus_counts() == {"turns_recorded": 2, "turns_pending": 2,
                                           "grown_rows": 0}

        built = corpus.build(authored_set=False, store_path=store, turn_log=queue)
        assert [r["family"] for r in built["rows"]] == ["desk"], built["rows"]
        assert built["counts"]["turns"]["recorded"] == 2
        assert built["counts"]["turns"]["unusable"] == 1
        assert H._laya_corpus_counts() == {"turns_recorded": 2, "turns_pending": 0,
                                           "grown_rows": 1}
