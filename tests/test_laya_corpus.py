"""Pins for the Laya corpus builder: labels, growth, and where it may write.

`ci/laya_corpus.py` is the file a fine-tune learns from, so the two things worth
holding down are the ones a corpus quietly gets wrong: a label that points at
nothing, and a store that either grows duplicates or rewrites a disagreement
instead of reporting it. Both are testable without torch — the corpus module is
deliberately stdlib-only, so this file never pulls the optional extra in.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import HERE as ROOT

sys.path.insert(0, str(ROOT / "ci"))

import laya_bakeoff as bakeoff  # noqa: E402
import laya_corpus as corpus  # noqa: E402


class TestTheCorpusKeepsItsLabelsHonest:
    def test_the_family_table_is_the_harness_s_own(self, monkeypatch):
        """Two copies of a label set drift, and the drift is silent: a fine-tune
        would learn families the harness no longer scores. The corpus imports
        the table rather than restating it — proven by moving the harness's.
        """
        monkeypatch.setitem(bakeoff.FAMILIES, "bogus_family",
                            ("a family that exists only in this test", ()))
        assert "bogus_family" in corpus.families()

    def test_every_authored_row_is_labelled_with_a_real_family(self):
        """The authored set is the training default, so a typo in it is a row
        the trainer would refuse (or worse, silently drop)."""
        fams = set(corpus.families())
        bad = sorted({fam for _text, fam in bakeoff.AUTHORED if fam not in fams})
        assert not bad, f"authored rows carry labels that are not families: {bad}"

    def test_a_family_naming_a_tool_the_belt_no_longer_has_is_reported(self):
        """A family whose tools are all gone is an option nothing can satisfy —
        the router could never offer it. Against the real checkout every family
        must still name live tools.
        """
        check = corpus.label_check(ROOT)
        assert check.get("mapped"), "the label check read no tool names at all"
        assert check.get("missing") == [], (
            f"families name tools that are not in the belt: {check.get('missing')}")

    def test_a_belt_tool_in_no_family_is_reported(self):
        """The other direction: a slice of the prompt no option can select. This
        is how the four desk tools were found missing from the table, so the
        report has to keep saying so — and every name it reports must be a real
        belt tool, not a typo of one.
        """
        check = corpus.label_check(ROOT)
        live = set(corpus.belt(ROOT) or {})
        assert live, "the belt did not load — the check under test proved nothing"
        assert set(check["unmapped"]) <= live, (
            "the unmapped list contains names that are not belt tools at all")
        covered = set(check["unmapped"]) | {
            t for _fam, (_d, tools) in corpus.families().items() for t in tools}
        assert live <= covered, f"the report lost sight of {sorted(live - covered)}"


class TestTheTurnQueue:
    """The app records a completed turn; this module folds it. That is where
    "the corpus grows without a command" actually lives, so it is what the
    guards here hold down: one label rule, a fold nobody has to remember, and a
    cursor that cannot go stale in silence."""

    def _queue(self, tmp_path: Path, rows: list) -> Path:
        path = tmp_path / "laya-turns.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def test_one_label_rule_labels_both_ways_a_row_is_mined(self):
        """The history pass and the turn queue must not be labelled by two
        different rules: the same tool set has to get the same answer, and the
        reason it is refused has to name the case the fold reports.
        """
        assert bakeoff.label_of(["set_reminder"]) == "timers"
        assert bakeoff.label_of(["set_reminder", "list_reminders"]) == "timers", \
            "one family, several tools, is one routing case"
        assert bakeoff.label_of([]) is None, "answering directly is not a tool choice"
        assert bakeoff.label_of(["set_reminder", "media_play"]) is None, \
            "a turn spanning two families is two decisions, not one"
        assert bakeoff.label_of(["no_such_tool"]) is None
        assert bakeoff.why_unlabelled(["set_reminder"]) == ""
        assert bakeoff.why_unlabelled([]) == "answered without a tool"
        assert "spans families" in bakeoff.why_unlabelled(
            ["set_reminder", "media_play"])
        assert "no family owns" in bakeoff.why_unlabelled(["no_such_tool"])

    def test_the_fold_happens_without_a_command(self, tmp_path):
        """`build()` is what --report, --dump and the fine-tune all call, and
        the queue is folded there — not behind --grow, which is only the
        history backfill. Nothing has to be remembered or run.
        """
        turns = self._queue(tmp_path, [
            {"ts": "t1", "text": "what is claude doing",
             "tools": ["quant_space_read"]},
            {"ts": "t2", "text": "hello there", "tools": []},
        ])
        built = corpus.build(authored_set=False, store_path=tmp_path / "s.jsonl",
                             turn_log=turns)
        assert built["counts"]["turns"]["recorded"] == 2
        assert built["counts"]["turns"]["folded_lines"] == 2
        assert built["counts"]["turns"]["unusable"] == 1
        assert built["counts"]["by_source"] == {"grown": 1}
        assert [r["family"] for r in built["rows"]] == ["desk"]
        assert built["rows"][0]["source"] == "turn:t1", built["rows"][0]

    def test_a_fold_is_idempotent_and_saying_it_again_counts(self, tmp_path):
        """Re-reading the corpus is the normal case. With the cursor advanced,
        a row counted twice means the user SAID it twice — which is why the
        cursor, not a re-seen counter, is what makes this honest.
        """
        turns = self._queue(tmp_path, [{"ts": "t1", "text": "play some music",
                                        "tools": ["media_play"]}])
        store = tmp_path / "s.jsonl"
        corpus.build(authored_set=False, store_path=store, turn_log=turns)
        assert json.loads(store.read_text(encoding="utf-8"))["count"] == 1
        again = corpus.build(authored_set=False, store_path=store, turn_log=turns)
        assert again["counts"]["turns"]["folded_lines"] == 0, "the cursor did not advance"
        assert json.loads(store.read_text(encoding="utf-8"))["count"] == 1
        turns.write_text(turns.read_text(encoding="utf-8")
                         + json.dumps({"ts": "t2", "text": "Play some music!",
                                       "tools": ["media_play"]}) + "\n",
                         encoding="utf-8")
        third = corpus.build(authored_set=False, store_path=store, turn_log=turns)
        assert third["counts"]["turns"]["folded"] == 1
        assert json.loads(store.read_text(encoding="utf-8"))["count"] == 2

    def test_a_hand_replaced_queue_is_refolded_not_skipped(self, tmp_path):
        """A line COUNT cannot tell "the same queue, more lines" from "another
        queue with the same count": a replaced file would leave the cursor
        ahead of it and every later turn would be skipped FOREVER — the one
        silent failure this design can have. The cursor therefore carries a
        fingerprint of the lines it consumed, and a queue that does not match
        it is folded from the start.
        """
        turns = self._queue(tmp_path, [{"ts": "t1", "text": "play some music",
                                        "tools": ["media_play"]}])
        store = tmp_path / "s.jsonl"
        corpus.build(authored_set=False, store_path=store, turn_log=turns)
        self._queue(tmp_path, [
            {"ts": "x1", "text": "what is the weather", "tools": ["get_weather"]},
            {"ts": "x2", "text": "pause the music", "tools": ["media_control"]},
        ])
        after = corpus.build(authored_set=False, store_path=store, turn_log=turns)
        assert after["counts"]["turns"]["replaced"] is True
        assert sorted(r["text"] for r in after["rows"]) == [
            "pause the music", "play some music", "what is the weather"], \
            "the refold kept the row it had already folded and took the two new ones"
        assert sorted(r["family"] for r in after["rows"]) == ["media", "media", "weather"]

    def test_a_turn_the_option_set_cannot_use_is_counted_not_dropped(self, tmp_path):
        turns = self._queue(tmp_path, [
            {"ts": "t1", "text": "open the window and play music",
             "tools": ["focus_window", "media_play"]},
            {"ts": "t2", "text": "hello there", "tools": []},
            {"ts": "t3", "text": "", "tools": ["set_reminder"]},
        ])
        built = corpus.build(authored_set=False, store_path=tmp_path / "s.jsonl",
                             turn_log=turns)
        turns_info = built["counts"]["turns"]
        assert turns_info["recorded"] == 3
        assert turns_info["folded"] == 0 and turns_info["unusable"] == 3
        assert built["counts"]["total"] == 0, "an unusable turn was trained on"

    def test_the_queue_the_cursor_and_the_store_are_private(self, tmp_path):
        turns = self._queue(tmp_path, [{"ts": "t", "text": "play some music",
                                        "tools": ["media_play"]}])
        store = tmp_path / "s.jsonl"
        corpus.build(authored_set=False, store_path=store, turn_log=turns)
        for path in (store, turns.with_suffix(".cursor")):
            assert path.stat().st_mode & 0o777 == 0o600, path

    def test_the_default_paths_follow_the_app_s_own_state_directory(self, monkeypatch,
                                                                    tmp_path):
        """A store anywhere else than the app's state directory is a corpus
        that silently trains on nothing: `handsoff.py` builds STATE_DIR as
        $XDG_STATE_HOME/handsoff, and these paths are derived the same way.
        """
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert corpus.state_dir() == tmp_path / "handsoff"
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert corpus.state_dir() == Path.home() / ".local" / "state" / "handsoff"
        assert corpus.DEFAULT_STORE.name == "laya-corpus.jsonl"
        assert corpus.DEFAULT_TURNS.name == "laya-turns.jsonl"
        assert corpus.DEFAULT_CURSOR.name == "laya-turns.cursor"


class TestTheGrowthStore:
    def test_a_row_is_added_once_and_re_seen_in_place(self, tmp_path):
        """Re-mining the same history is the normal case (the harness is run by
        hand, repeatedly). It must not duplicate rows, and `count` is how a
        genuinely repeated utterance becomes visible.
        """
        store: dict = {}
        first = corpus.merge(store, [{"text": "Set a timer for ten minutes.",
                                      "family": "timers", "source": "history:a"}],
                             today="2026-09-21")
        assert len(first["added"]) == 1 and not first["updated"]
        again = corpus.merge(store, [{"text": "set a timer for ten minutes",
                                      "family": "timers", "source": "history:b"}],
                            today="2026-09-22")
        assert not again["added"], "punctuation and case are not a new row"
        assert len(again["updated"]) == 1 and len(store) == 1
        row = next(iter(store.values()))
        assert row["count"] == 2 and row["last_seen"] == "2026-09-22"
        assert row["first_seen"] == "2026-09-21", "first_seen must not move"
        assert row["source"] == "history:a", "the label's provenance stays the first one"

    def test_a_disagreement_is_a_conflict_not_a_rewrite(self, tmp_path):
        """Two labels for one sentence is data — the label set is ambiguous for
        that utterance. A store that silently takes the newer one cannot be
        audited, so the conflict is reported and the first label is kept.
        """
        store: dict = {}
        corpus.merge(store, [{"text": "what is claude doing",
                              "family": "system", "source": "history:a"}])
        out = corpus.merge(store, [{"text": "What is Claude doing?",
                                    "family": "web", "source": "history:b"}])
        assert len(out["conflicts"]) == 1
        _text, was, now, _a, _b = out["conflicts"][0]
        assert (was, now) == ("system", "web")
        assert next(iter(store.values()))["family"] == "system"

    def test_a_label_that_is_not_a_family_is_refused(self, tmp_path):
        store: dict = {}
        out = corpus.merge(store, [{"text": "open the pod bay doors",
                                    "family": "spaceship", "source": "x"}])
        assert out["unknown"] and not store, (
            "an unknown label must be refused rather than trained on")

    def test_the_store_round_trips_through_disk(self, tmp_path):
        path = tmp_path / "laya-corpus.jsonl"
        store: dict = {}
        corpus.merge(store, [{"text": "play some music", "family": "media",
                              "source": "history:a"}])
        corpus.save_store(store, path)
        assert corpus.load_store(path) == store
        # an unreadable/garbage store is an empty one, not a crash
        path.write_text("{not json\n\n", encoding="utf-8")
        assert corpus.load_store(path) == {}

    def test_a_torn_last_line_costs_one_row_not_the_whole_store(self, tmp_path):
        """A power cut can leave the store's or the queue's last line cut
        mid-character. Both were read as strict UTF-8 under a handler that names
        only OSError, so the tool died with a UnicodeDecodeError instead of
        skipping the one line it could not read."""
        good = json.dumps({"text": "play some music", "family": "media",
                           "source": "history:a"}).encode("utf-8")
        torn = b'{"text": "caf\xc3'
        store_path = tmp_path / "laya-corpus.jsonl"
        store_path.write_bytes(good + b"\n" + torn)
        rows = corpus.load_store(store_path)
        assert [r["text"] for r in rows.values()] == ["play some music"]

        queue = tmp_path / "laya-turns.jsonl"
        queue.write_bytes(
            json.dumps({"text": "play some jazz", "tools": ["media_play"]}
                       ).encode("utf-8") + b"\n" + torn)
        mined, info = corpus.mine_turns(queue)
        assert info["present"] is True
        assert info["recorded"] == 2
        assert [r["text"] for r in mined] == ["play some jazz"]


class TestTheCorpusNeverWritesIntoTheCheckout:
    def test_the_default_store_is_outside_the_tree(self):
        """Mined rows are the user's own sentences. They live in the app's state
        directory — the same place the app keeps its runtime files — and the
        repository is never the place a private utterance lands.
        """
        assert ROOT not in corpus.DEFAULT_STORE.resolve().parents
        assert corpus.DEFAULT_STORE.name == "laya-corpus.jsonl"

    def test_dumping_the_corpus_inside_the_checkout_is_refused(self, tmp_path):
        target = ROOT / "ci" / "laya-corpus-leak.jsonl"
        rc = corpus.main(["laya_corpus.py", "--dump", str(target),
                          "--store", str(tmp_path / "store.jsonl")])
        assert rc == 1
        assert not target.exists(), "a refused dump still wrote the file"

    def test_a_redirected_store_gets_a_scratch_queue_not_the_real_one(
            self, tmp_path, monkeypatch):
        """A redirected --store must never fold the app's REAL turn queue.

        build() already knew the rule (a scratch store means a scratch queue),
        but main() passed its --turns DEFAULT straight through, defeating it:
        the checkout guard caught the suite advancing the real cursor on
        2026-09-22, after fault-injection turns left laya-turns.jsonl unmined
        and any --store-redirected run advanced it under the developer's home.
        The cursor advances over EVERY line, usable or not — so one junk row
        is enough to prove the point.
        """
        real_queue = tmp_path / "real" / "laya-turns.jsonl"
        real_queue.parent.mkdir()
        real_queue.write_text(
            json.dumps({"ts": "t", "text": "", "tools": []}) + "\n",
            encoding="utf-8")
        monkeypatch.setattr(corpus, "DEFAULT_TURNS", real_queue)
        rc = corpus.main(["laya_corpus.py", "--report",
                          "--store", str(tmp_path / "store.jsonl")])
        assert rc == 0
        real_cursor = real_queue.with_suffix(".cursor")
        assert not real_cursor.exists(), (
            "a redirected store folded and advanced the real turn queue")
        # ...and the scratch queue beside the scratch store is the one read
        # (absent here, so nothing was folded at all).
        scratch_queue = tmp_path / "laya-turns.jsonl"
        assert scratch_queue == corpus.DEFAULT_STORE.parent / corpus.DEFAULT_TURNS.name \
            or not scratch_queue.exists()

    def test_the_hash_covers_the_labelled_set(self, tmp_path):
        """A checkpoint records which corpus trained it. Same rows in another
        order must hash the same; a different label must not.
        """
        store_a = tmp_path / "a.jsonl"
        store_b = tmp_path / "b.jsonl"
        rows = [{"text": "set a timer for ten minutes", "family": "timers",
                 "source": "authored"},
                {"text": "play some music", "family": "media", "source": "authored"}]
        store_a.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        store_b.write_text("".join(json.dumps(r) + "\n" for r in reversed(rows)),
                           encoding="utf-8")
        one = corpus.build(authored_set=False, store_path=store_a)
        two = corpus.build(authored_set=False, store_path=store_b)
        assert one["hash"] == two["hash"], "row order is not part of the corpus"
        store_b.write_text("".join(
            json.dumps({**r, "family": "web"} if r["family"] == "media" else r) + "\n"
            for r in rows), encoding="utf-8")
        three = corpus.build(authored_set=False, store_path=store_b)
        assert three["hash"] != one["hash"], "a relabelled row is a different corpus"

    def test_the_authored_set_is_the_corpus_when_nothing_was_mined(self, tmp_path):
        built = corpus.build(real_dir=None, store_path=tmp_path / "empty.jsonl")
        assert built["counts"]["total"] == len(bakeoff.AUTHORED)
        assert built["counts"]["by_source"] == {"authored": len(bakeoff.AUTHORED)}
        assert len(built["hash"]) == 64


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
