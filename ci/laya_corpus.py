#!/usr/bin/env python3
"""The corpus a Laya fine-tune is allowed to learn from — and how it GROWS.

WHY THIS IS NOT INSIDE THE BAKE-OFF.

`ci/laya_bakeoff.py` measures whether a System-1 engine can pick the right tool
family, and it says no to the zero-shot model: 62% top-1, confident and wrong,
`none` absorbing imperatives. Its own verdict was that fine-tuning is the honest
path — and a fine-tune needs something the bake-off does not have: a corpus with
provenance, that grows as the machine is used, and whose labels are checked
against the belt that actually ships.

ONE TABLE, NOT TWO. The families and the authored rows are imported from the
harness (`FAMILIES`, `AUTHORED`, `mine_real`) rather than restated here, because
two copies of a label set drift and the drift is silent: a fine-tune would be
trained toward families the harness no longer scores. `--report` prints the one
check that keeps them honest — every family's tools against the shipped belt.

WHERE THE GROWTH LIVES. The corpus is grown by the APP, not by a command: every
completed turn is appended to a QUEUE (`laya-turns.jsonl`) in the app's state
directory, holding the two facts the app owns — the utterance, and the tool names
the turn called. The family is derived HERE, by the harness's one rule
(`label_of`), because the label table is the harness's and a copy of it inside
the app is a copy that drifts.

THE QUEUE IS FOLDED ON EVERY READ, so no command is needed for the corpus to
grow: `build()` — what `--report`, `--dump` and the fine-tune all call — folds
whatever the app has recorded since the last fold, merges it into the store, and
advances a CURSOR (`laya-turns.cursor`) holding the number of queue lines already
folded. The fold is idempotent because of the cursor: a re-seen row in the store
means the user really said that sentence again, not that a report ran twice. The
queue is append-only (it is the record that a fold has NOT happened yet —
deleting from it is what would lose a turn whose fold raced the app's write); it
can be deleted BY HAND safely, because a queue shorter than the cursor is
recognised as a replaced one and refolded from the start, which the store's
dedupe absorbs.

Rows mined from the app's own history are the same kind of data — the user's own
sentences — so they too are written to the app's STATE directory, never into this
checkout, and never into a spec. `--grow` remains for that history BACKFILL: it
is a hand-run pass over old `history.json` files, which the turn queue cannot
recover because history is trimmed. The store is deduped by normalised text:
re-mining updates `last_seen`/`count` in place, and the same sentence with two
different labels is reported as a CONFLICT rather than silently relabelled — that
is a disagreement about intent, which is data.

Usage (no torch needed — this file is stdlib only):
    python3 ci/laya_corpus.py --report
    python3 ci/laya_corpus.py --report --checkout .
    python3 ci/laya_corpus.py --grow --real ~/.config/handsoff
    python3 ci/laya_corpus.py --dump /tmp/train.jsonl --real ~/.config/handsoff
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import laya_bakeoff as bakeoff  # noqa: E402  (the one family/row table)


def state_dir() -> pathlib.Path:
    """The app's state directory, read from the environment as the app reads it.

    A store somewhere else than the app's own paths is a corpus that silently
trains on nothing, so this mirrors `handsoff.py`'s `STATE_DIR` exactly:
`$XDG_STATE_HOME/handsoff`, and `~/.local/state/handsoff` when it is unset.
    """
    root = (os.environ.get("XDG_STATE_HOME")
            or str(pathlib.Path.home() / ".local" / "state"))
    return pathlib.Path(root) / "handsoff"


#: default growth store: the app's own state directory, never the checkout
DEFAULT_STORE = state_dir() / "laya-corpus.jsonl"
#: the app's turn queue: one line per completed turn, written as it happens
DEFAULT_TURNS = state_dir() / "laya-turns.jsonl"
#: how much of the queue has already been folded (line count)
DEFAULT_CURSOR = state_dir() / "laya-turns.cursor"

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """The dedupe key: case, punctuation and whitespace are not the label.

    "Set a timer for ten minutes." and "set a timer for 10 minutes" are two
    rows; "set a timer for ten minutes," and "SET A TIMER FOR TEN MINUTES" are
    one. Normalising harder than this (digits, synonyms) would merge rows whose
    words are what the model is being asked to read.
    """
    return _WS.sub(" ", _PUNCT.sub(" ", str(text).lower())).strip()


def families() -> dict:
    """The family table, from the harness (single source of truth)."""
    return dict(bakeoff.FAMILIES)


def belt(checkout: pathlib.Path | None) -> dict | None:
    """{tool: gate} from the checkout's own belt, or None if it cannot be read.

    Best-effort and isolated, exactly like the bake-off's token arithmetic: a
    corpus check must not be the reason a report dies.
    """
    if checkout is None:
        return None
    try:
        sys.path.insert(0, str(checkout))
        import core.tools as core_tools  # type: ignore

        return dict(core_tools.tool_gates())
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"  (belt check skipped: {type(exc).__name__}: {exc})")
        return None


def label_check(checkout: pathlib.Path | None = None) -> dict:
    """Does every label point at tools that exist, and is every tool labelled?

    A family whose tools are all gone is a label nothing can satisfy — the
    fine-tune would be trained toward an option the router could never offer.
    A belt tool in no family is the same defect from the other side: a slice of
    the prompt no option can select (this is how the four desk tools were found
    missing from the table).
    """
    gates = belt(checkout)
    fams = families()
    mapped = {t for _, (_, tools) in fams.items() for t in tools}
    out: dict = {"families": len(fams), "mapped": len(mapped)}
    if gates is None:
        return out
    live = set(gates)
    out["missing"] = sorted(mapped - live)
    out["unmapped"] = sorted(live - mapped)
    out["family_gates"] = {
        fam: sorted({gates[t] or "(ungated)" for t in tools if t in gates})
        for fam, (_, tools) in fams.items() if tools
    }
    return out


def mine(real_dir: pathlib.Path | None) -> list[dict]:
    """(text, family, source) rows mined from the app's own history files."""
    if real_dir is None:
        return []
    return [{"text": text, "family": fam, "source": f"history:{src}"}
            for text, fam, src in bakeoff.mine_real(real_dir)]


def authored(authored_set: bool = True) -> list[dict]:
    if not authored_set:
        return []
    return [{"text": text, "family": fam, "source": "authored"}
            for text, fam in bakeoff.AUTHORED]


def load_store(path: pathlib.Path) -> dict:
    """{norm(text): row} from the growth store; unreadable store = empty one."""
    rows: dict = {}
    if not path.exists():
        return rows
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = norm(row.get("text") or "")
        if key:
            rows[key] = row
    return rows


def merge(store: dict, rows: list[dict], today: str | None = None) -> dict:
    """Fold `rows` into `store`. Returns what changed, including CONFLICTS.

    A row already present keeps its first label and its `first_seen`; only
    `last_seen` and `count` move. A row whose label disagrees is a conflict: it
    is counted and reported, never relabelled — two labels for one sentence is
    a fact about the task, and a corpus that quietly picks one is a corpus that
    cannot be audited.
    """
    today = today or datetime.date.today().isoformat()
    fams = families()
    added, updated, conflicts, unknown = [], [], [], []
    for row in rows:
        key = norm(row.get("text") or "")
        fam = row.get("family")
        if not key or not fam:
            continue
        if fam not in fams:
            unknown.append((row.get("text"), fam))
            continue
        cur = store.get(key)
        if cur is None:
            store[key] = {"text": row["text"], "family": fam,
                          "source": row.get("source") or "?",
                          "first_seen": today, "last_seen": today, "count": 1}
            added.append(key)
            continue
        cur["last_seen"] = today
        cur["count"] = int(cur.get("count") or 1) + 1
        updated.append(key)
        if cur.get("family") != fam:
            conflicts.append((cur.get("text"), cur.get("family"), fam,
                              cur.get("source"), row.get("source")))
    return {"added": added, "updated": updated, "conflicts": conflicts,
            "unknown": unknown}


def _atomic_private_write(path: pathlib.Path, text: str) -> None:
    """Write 0600 via a temp file + rename — the app's own state-file rule.

    Both files here hold the user's own sentences, so they are as private as
    `decisions.jsonl`, and a reader never sees a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def save_store(store: dict, path: pathlib.Path) -> None:
    """Write the store atomically, as the app writes its own state files."""
    rows = sorted(store.values(), key=lambda r: (r.get("family") or "",
                                                 norm(r.get("text") or "")))
    _atomic_private_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                        for r in rows))


def _prefix_hash(lines: list[str], count: int) -> str:
    """A fingerprint of the queue's first `count` lines, as read."""
    return hashlib.sha256("\n".join(lines[:max(0, count)])
                          .encode("utf-8")).hexdigest()


def load_cursor(path: pathlib.Path) -> tuple[int, str]:
    """(lines folded, fingerprint of those lines) — (0, "") when unreadable.

    The fingerprint is what makes the cursor verifiable. A line COUNT alone
    cannot tell "the same queue, more lines" from "a different queue with the
    same count": a hand-replaced or hand-truncated file would leave the cursor
    ahead of the new queue and every later turn would be skipped forever — the
    one failure mode of this design that is silent. A cursor written before the
    fingerprint existed simply mismatches and refolds, which the store's dedupe
    absorbs.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return max(0, int(doc.get("lines"))), str(doc.get("hash") or "")
    except (OSError, TypeError, ValueError):
        return 0, ""


def save_cursor(path: pathlib.Path, lines: int, digest: str) -> None:
    """Advance the cursor; a write that fails just refolds those lines."""
    _atomic_private_write(path, json.dumps({
        "lines": int(lines), "hash": digest,
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }))


def mine_turns(turn_log: pathlib.Path,
               cursor_path: pathlib.Path | None = None) -> tuple[list[dict], dict]:
    """(rows, info) from the app's turn queue — one line per completed turn.

    The queue holds FACTS the app owns (the utterance and the tool names the
    turn called); the family is derived here, by the harness's one rule, and a
    row that selects no family is counted under the harness's own reason rather
    than dropped quietly. `info["cursor"]` is the cursor to save after a
    SUCCESSFUL merge — saving it first would skip lines a failed merge never
    folded.
    """
    info: dict = {"recorded": 0, "folded_lines": 0, "cursor": 0,
                  "cursor_hash": "", "unusable": {}, "unreadable": 0,
                  "replaced": False, "present": False}
    try:
        lines = turn_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], info
    info["present"] = True
    info["recorded"] = sum(1 for line in lines if line.strip())
    total = len(lines)
    cursor, fingerprint = (load_cursor(cursor_path) if cursor_path is not None
                           else (0, ""))
    stale = (cursor, fingerprint) != (0, "")
    if max(0, min(cursor, total)) != cursor \
            or _prefix_hash(lines, cursor) != fingerprint:
        # Not the queue the cursor counted: deleted, replaced, truncated or
        # hand-edited. Fold ALL of it — the store's dedupe absorbs the overlap,
        # and this is what makes deleting the queue by hand safe instead of
        # silently stopping the corpus.
        cursor = 0
        info["replaced"] = stale
    rows: list[dict] = []
    for line in lines[cursor:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            info["unreadable"] += 1
            continue
        if not isinstance(rec, dict):
            info["unreadable"] += 1
            continue
        text = str(rec.get("text") or "").strip()
        tools = [t for t in (rec.get("tools") or []) if isinstance(t, str)]
        fam = bakeoff.label_of(tools)
        if not text or fam is None:
            reason = bakeoff.why_unlabelled(tools) or "no utterance"
            info["unusable"][reason] = info["unusable"].get(reason, 0) + 1
            continue
        rows.append({"text": text, "family": fam,
                     "source": f"turn:{rec.get('ts') or '?'}"})
    info["folded_lines"] = total - cursor
    info["cursor"] = total
    info["cursor_hash"] = _prefix_hash(lines, total)
    return rows, info


def report_merge(stats: dict, label: str) -> None:
    """The parts of a merge a person must see: conflicts and bad labels."""
    for text, was, now, src_a, src_b in stats["conflicts"]:
        print(f"  CONFLICT [{label}] {str(text)[:60]!r}: {was} ({src_a}) vs "
              f"{now} ({src_b})")
    for text, fam in stats["unknown"]:
        print(f"  UNKNOWN label {fam!r} for {str(text)[:60]!r} — not a family")


def build(authored_set: bool = True, real_dir: pathlib.Path | None = None,
          store_path: pathlib.Path | None = None,
          grow: bool = False, turn_log: pathlib.Path | None = None,
          cursor_path: pathlib.Path | None = None,
          fold: bool = True) -> dict:
    """The corpus a fine-tune may use: authored rows + grown rows, deduped.

    The turn queue is folded on EVERY read (see the module docstring), which is
    what makes the corpus grow with no command: the app records each completed
    turn as it happens, and the next read takes it. `grow` is only the history
    backfill. Returns {"rows": [...], "hash": ..., "counts": ...}; the hash
    covers the labelled SET (normalised text + family), so a checkpoint can
    record exactly which corpus it was trained on and a later run can tell
    whether the corpus moved under it.
    """
    store_path = store_path or DEFAULT_STORE
    if turn_log is None:
        # A scratch store means a scratch queue: the queue, its cursor and the
        # store belong to ONE directory, so a caller that redirects the store
        # (the suite, a test) does not fold — or write — the app's real one.
        turn_log = (DEFAULT_TURNS if store_path == DEFAULT_STORE
                    else store_path.parent / DEFAULT_TURNS.name)
    if cursor_path is None:
        cursor_path = turn_log.with_suffix(".cursor")
    store = load_store(store_path)
    counts: dict = {}

    turns: dict = {"recorded": 0, "folded": 0, "folded_lines": 0,
                   "unusable": 0, "replaced": False}
    if fold:
        turn_rows, info = mine_turns(turn_log, cursor_path)
        stats = merge(store, turn_rows)
        turns.update(recorded=info["recorded"], folded=len(turn_rows),
                     folded_lines=info["folded_lines"],
                     unusable=sum(info["unusable"].values()),
                     unusable_reasons=dict(info["unusable"]),
                     unreadable=info["unreadable"], replaced=info["replaced"])
        if turn_rows:
            save_store(store, store_path)
            report_merge(stats, "turn")
        # AFTER the merge, never before: a merge that raised must leave the
        # cursor where it was, so those lines are folded (not skipped) next
        # time. And only when there IS a queue: a missing one has no cursor.
        if info["present"]:
            save_cursor(cursor_path, info["cursor"], info["cursor_hash"])
        if info["folded_lines"]:
            # The reasons, not just the count: "3 unusable" says nothing about
            # whether the option set is too narrow for how this user talks, and
            # the reasons are the harness's own words (one rule, reported by the
            # same function that refuses the row).
            why = "".join(f"\n    {reason} ×{n}"
                          for reason, n in sorted(info["unusable"].items()))
            print(f"  turns: {info['folded_lines']} folded "
                  f"(+{len(stats['added'])} new, {len(stats['updated'])} re-seen) "
                  f"from {info['recorded']} recorded"
                  + (f", {turns['unusable']} unusable{why}" if turns["unusable"] else ""))
    counts["turns"] = turns

    if grow:
        stats = merge(store, mine(real_dir))
        save_store(store, store_path)
        counts.update({"added": len(stats["added"]),
                       "updated": len(stats["updated"]),
                       "conflicts": len(stats["conflicts"]),
                       "unknown_labels": len(stats["unknown"])})
        report_merge(stats, "history")

    rows = authored(authored_set) + list(store.values())
    seen: set = set()
    deduped: list[dict] = []
    for row in rows:
        key = norm(row.get("text") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    digest = hashlib.sha256(
        "\n".join(f"{norm(r['text'])}\t{r['family']}"
                  for r in sorted(deduped, key=lambda r: norm(r["text"])))
        .encode("utf-8")).hexdigest()
    by_source: dict = {}
    by_family: dict = {}
    for row in deduped:
        src = str(row.get("source") or "?")
        bucket = "authored" if src == "authored" else "grown"
        by_source[bucket] = by_source.get(bucket, 0) + 1
        by_family[row["family"]] = by_family.get(row["family"], 0) + 1
    counts.update({"total": len(deduped), "by_source": by_source,
                   "by_family": by_family, "hash": digest})
    return {"rows": deduped, "hash": digest, "counts": counts}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true",
                        help="print the family/label state and stop")
    parser.add_argument("--grow", action="store_true",
                        help="BACKFILL: mine the app's old history.json files "
                             "into the store (the turn queue folds by itself)")
    parser.add_argument("--real", type=pathlib.Path, default=None,
                        help="directory of history.json* files to mine (private)")
    parser.add_argument("--store", type=pathlib.Path, default=DEFAULT_STORE,
                        help=f"the growth store (default: {DEFAULT_STORE})")
    parser.add_argument("--turns", type=pathlib.Path, default=DEFAULT_TURNS,
                        help="the app's turn queue, folded on every read "
                             f"(default: {DEFAULT_TURNS})")
    parser.add_argument("--no-fold", action="store_true",
                        help="skip the turn queue (store + authored rows only)")
    parser.add_argument("--checkout", type=pathlib.Path, default=None,
                        help="a checkout, to check labels against the real belt")
    parser.add_argument("--dump", type=pathlib.Path, default=None,
                        help="write the deduped corpus here (JSONL, outside the checkout)")
    parser.add_argument("--no-authored", action="store_true",
                        help="grown rows only (the authored set is the training default)")
    args = parser.parse_args(argv[1:])

    corpus = build(authored_set=not args.no_authored, real_dir=args.real,
                   store_path=args.store, grow=args.grow,
                   turn_log=args.turns, fold=not args.no_fold)
    counts = corpus["counts"]
    print(f"families           : {len(families())}")
    if args.grow:
        print(f"store              : {args.store}")
        print(f"growth             : +{counts['added']} new, "
              f"{counts['updated']} re-seen, {counts['conflicts']} conflicts, "
              f"{counts['unknown_labels']} unknown labels")
    print(f"corpus             : {counts['total']} rows "
          f"({counts['by_source']})")
    print(f"corpus hash        : {corpus['hash'][:16]}…")
    for fam, n in sorted(counts["by_family"].items()):
        print(f"  {fam:<14} {n:>3}")
    if args.checkout:
        check = label_check(args.checkout.resolve())
        print(f"\nlabel check vs the belt ({args.checkout}): {check.get('mapped')} "
              f"tool names mapped")
        if check.get("missing"):
            print(f"  MISSING from the belt: {', '.join(check['missing'])}")
        if check.get("unmapped"):
            print(f"  in the belt, in NO family: {', '.join(check['unmapped'])}")
    if args.dump:
        target = args.dump.resolve()
        root = HERE.parent.resolve()
        if root == target or root in target.parents:
            print(f"REFUSED: {target} is inside the checkout — mined rows are the "
                  f"user's own sentences and are not written into the repo")
            return 1
        target.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                  for r in corpus["rows"]), encoding="utf-8")
        print(f"\nwrote {counts['total']} rows to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
