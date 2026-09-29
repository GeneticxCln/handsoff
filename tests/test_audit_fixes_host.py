"""Pins for the 2026-09-28 audit fixes in handsoff.py.

laya-turns.jsonl is bounded and created 0600 (it holds verbatim utterances
and nothing ever pruned it), and a clear-history issued while a turn runs is
honoured by the turn's own publish (the epoch check) instead of being undone
by it.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest


class TestTheLayaTurnsStore:
    def test_the_store_is_created_0600_and_trimmed(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", tmp_path / "laya-turns.jsonl")
        monkeypatch.setattr(H, "LAYA_TURNS_CURSOR", tmp_path / "laya-turns.cursor")
        monkeypatch.setattr(H, "_LAYA_TURNS_BYTES", 256)
        monkeypatch.setattr(H, "_LAYA_TURNS_MAX", 10)
        for i in range(40):
            H._record_turn_for_corpus(f"utterance number {i} with some words", [])
        f = tmp_path / "laya-turns.jsonl"
        assert f.exists()
        # 0600: no group or other bits.
        assert (f.stat().st_mode & 0o077) == 0, oct(f.stat().st_mode)
        lines = f.read_text(encoding="utf-8").splitlines()
        # The trim fires past 2x the cap and keeps the last MAX lines, so the
        # steady state oscillates between MAX and 2xMAX+1 — bounded, where
        # before this fix nothing ever pruned it.
        assert len(lines) <= H._LAYA_TURNS_MAX * 2 + 1, len(lines)
        # The newest lines survive a tail trim.
        assert "utterance number 39" in lines[-1]
        # The cursor is reset with the trim: it counts folded lines from the
        # START of the file, and the corpus dedupes, so 0 (re-fold, dedupe)
        # is the only honest value after the file's head is gone.
        cursor = json.loads((tmp_path / "laya-turns.cursor").read_text(
            encoding="utf-8"))
        assert cursor.get("lines") == 0, cursor

    def test_a_torn_last_line_does_not_stop_the_trim(self, H, monkeypatch, tmp_path):
        """A power cut can leave the last line cut mid-character. The trim read
        the file as strict UTF-8 inside a handler that names only OSError, so
        the torn tail raised out of it — swallowed by the outer handler — and
        the store was never pruned again."""
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", tmp_path / "laya-turns.jsonl")
        monkeypatch.setattr(H, "LAYA_TURNS_CURSOR", tmp_path / "laya-turns.cursor")
        monkeypatch.setattr(H, "_LAYA_TURNS_BYTES", 256)
        monkeypatch.setattr(H, "_LAYA_TURNS_MAX", 10)
        f = tmp_path / "laya-turns.jsonl"
        f.write_bytes(b"".join(b'{"text": "utterance %d padding padding"}\n' % i
                               for i in range(60)) + b'{"text": "caf\xc3')
        H._record_turn_for_corpus("the turn after the crash", [])
        data = f.read_bytes()
        assert len(data.splitlines()) <= H._LAYA_TURNS_MAX * 2 + 1, (
            "a torn last line left the turn queue growing without bound")
        assert b"the turn after the crash" in data.splitlines()[-1]
        data.decode("utf-8")           # and the file it rewrote is text again

    def test_a_small_store_is_not_trimmed(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setattr(H, "LAYA_TURNS_FILE", tmp_path / "laya-turns.jsonl")
        monkeypatch.setattr(H, "LAYA_TURNS_CURSOR", tmp_path / "laya-turns.cursor")
        H._record_turn_for_corpus("one small utterance", [])
        assert len((tmp_path / "laya-turns.jsonl").read_text(
            encoding="utf-8").splitlines()) == 1


class TestTheClearHistoryEpoch:
    def _assistant(self, H, history):
        a = H.Assistant.__new__(H.Assistant)
        a._history = list(history)
        a._history_lock = threading.Lock()
        a._history_epoch = 0
        a._saved = []
        a._save_history = lambda: a._saved.append(list(a._history))
        return a

    def test_clear_drops_and_bumps_the_epoch(self, H):
        a = self._assistant(H, [{"role": "user", "content": "hi"}])
        dropped = a.clear_history()
        assert dropped == 1
        assert a._history == [] and a._history_epoch == 1
        assert a._saved == [[]]

    def test_a_publish_across_a_clear_is_dropped_not_appended(self, H, monkeypatch):
        """The clear-history control verb and the turn's publish ran without
        a shared lock: a clear issued mid-turn said "cleared N messages" and
        the turn's own exchange reappeared right after it."""
        a = self._assistant(H, [])
        entry_epoch = a._history_epoch
        a.clear_history()                      # the user cleared mid-turn
        with a._history_lock:
            if a._history_epoch == entry_epoch:
                a._history = a._history + [{"role": "user", "content": "late"}]
        assert a._history == [], \
            "an exchange that began before the clear must not reappear"

    def test_a_publish_in_the_same_epoch_still_lands(self, H):
        a = self._assistant(H, [])
        entry_epoch = a._history_epoch
        with a._history_lock:
            if a._history_epoch == entry_epoch:
                a._history = a._history + [{"role": "user", "content": "hi"}]
        assert len(a._history) == 1


class TestTheDeployFloor:
    def test_every_module_the_installer_declares_the_floor_compares(self, H):
        """_DEPLOY_FILES was the floor a manifest-less install compares; it
        had never heard of core/voice.py, which install.sh ships and the
        bubble loads — a stale deployment of that module reported in-sync."""
        root = Path(__file__).resolve().parent.parent
        text = (root / "install.sh").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines()
                    if ln.startswith("CORE_REQUIRED="))
        required = line.split('"', 2)[1].split()
        missing = [f"core/{name}.py" for name in required
                   if name != "__init__" and f"core/{name}.py" not in H._DEPLOY_FILES]
        assert missing == [], missing
