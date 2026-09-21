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
