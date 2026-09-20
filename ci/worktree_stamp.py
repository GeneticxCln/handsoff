#!/usr/bin/env python3
"""The worktree's fingerprint, so a run can refuse to be judged on a tree that moved.

Why this exists: this checkout is shared. The suite runs for minutes while an
editor, another agent or a `git checkout` can write to the SAME files, and the
result is the false red the audit kept hitting — a gate that fails on a tree
that is not the one it started with, or worse, passes on a mixture of two. A
green run on a moving tree is not evidence about either revision, so the run has
to say so instead of reporting a verdict.

What is stamped:

  * HEAD, because a commit or a checkout mid-run means the tree under test
    changed identity;
  * the content of every TRACKED change, ONE HASH PER FILE — so the refusal can
    name the file that moved instead of listing every file that happened to be
    dirty when the run started, which on a shared checkout is most of them. The
    whole `git diff HEAD` is hashed as well, because a MODE change moves
    nothing a content hash can see;
  * every untracked file git does NOT ignore, with its own hash — an untracked
    file is still code the suite may have imported;
  * and, for every tracked file and every untracked one, the file's OWN
    LAST-WRITE TIME. Content alone cannot see a write that was REVERTED inside a
    window — the two snapshots agree about the bytes — and the writer still moved
    underneath the suite: a gate may have read the version that is now gone. The
    evidence such an edit leaves behind is its write time, so a path whose content
    a checkpoint agrees about while its write time moved is reported as
    `written during the window and restored`. Reading a file never moves it, and
    files git ignores are outside the map, which is what keeps this from refusing
    the gates' own artifacts. It is a write time, not an intention: a `touch`
    reads exactly like an edit that was put back, and a file CREATED and deleted
    inside one window is in neither map and still leaves no trace (a per-path
    record cannot name a path that is in neither snapshot).

What is deliberately NOT stamped: everything git ignores. The gates write
`.coverage`, `tests/report*.xml` and `__pycache__` on every run, and a stamp
that counted those would refuse every run it was added to — including this one.
The INDEX alone is not stamped either, and that is a decision rather than an
oversight: `git diff HEAD` is the worktree against HEAD, which is what the suite
actually imported, so a `git add` or `update-index` that changes only the index
changes nothing that was tested (pinned by a test that says so).
That reliance on `.gitignore` is a mechanism, not a hope: `tests/test_ci_two_writer.py`
copies this repo's real ignore file into a scratch worktree and asserts that
creating those artifacts does not move the stamp.

Exit codes for `--compare` follow ci/gates.sh's convention: 0 unchanged, 1 the
tree moved (the refusal), 2 cannot judge — a missing snapshot, unreadable JSON,
or a directory that is not a git worktree (a tarball install has no HEAD and no
ignore rules, so the honest answer is "no opinion", never "fine").

`--checkpoint DIR --label NAME` is how the run learns WHEN it moved. After every
gate, ci/gates.sh takes one: it compares the tree against the newest snapshot in
DIR and, if they differ, appends the difference under the label of the gate that
just ran (`during the coverage gate:` …). The verdict at the end is therefore two
answers instead of one — WHICH files moved, and which gate they moved under —
which is the difference between "the run is untrustworthy" and "the run is
untrustworthy and here is the gate to look at").

HOW FAR INTO that window the write landed is the third answer, and it is the one
a reader needs to tell a collision from a coincidence: a change 8 seconds into a
4-minute suite is somebody editing while the tests ran, while the same change 4
seconds before the end is usually an artifact of the run settling. Every snapshot
carries the wall clock it finished at (`at`), so a changed file's OWN clock — its
`mtime`, when its bytes were last written — can be placed in the window the two
snapshots bracket: how many seconds in, roughly what fraction through, and at what
clock time. It is ROUGHLY and it says so, because the window is bracketed by
checkpoints rather than watched: the resolution is "which part of this gate",
never an instant. A `mtime` BEFORE the window is a real shape rather than an
error — the earlier snapshot read that file before the write and finished after it
— and it is said in those words instead of being clamped to zero, because a
clamped zero reads as "at the very start of the gate", which is a different fact.
A HEAD move has no file to ask, so it is timed by the commit itself (`%ct`).

The names in DIR are fixed so the shell needs no bookkeeping: `first.json`
(written once per LEG of a run — see `--rebase`), `latest.json`,
`NNN-<label>.json` per checkpoint, and `moves.txt` (empty on a still run; the
`when:` lines live in it, one per changed path, under the gate's own heading).

`--rebase DIR --label NAME` is what turns a collision into a RESUMED run instead
of a discarded one. The gates after a collision are judged against the tree as it
now stands, so ci/gates.sh moves the baseline onto the newest checkpoint (the
state the re-run starts from) and annotates it: `"baseline": "the tree moved
during the coverage gate"`. Without that move, the end comparison would be a
verdict about a tree that no longer exists — which is the refusal this replaces —
and without the annotation the run's closing line would say "since the run
started" about a tree it did not start with. So the baseline moves only on
explicit request, once per leg, and it carries the reason it moved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 60
# The fields a snapshot needs for a pair to be judgeable at all. `at` is
# deliberately NOT here: a snapshot from before the clock existed still gives a
# correct verdict, only without the timing lines. Neither is `written` (one
# write time per path): a snapshot from before that evidence existed judges
# content exactly as well — it just cannot report a write that was reverted.
REQUIRED_FIELDS = ("head", "tracked", "tracked_diff", "tracked_paths",
                   "untracked")
# How many paths get their own `when:` line. A `git checkout` can move hundreds,
# and past a handful the lines stop pointing at anything — the rest are COUNTED,
# not dropped in silence.
MAX_WHEN_LINES = 8
# Below this, a fraction of the window is noise ("3% through 0.4s"); the absolute
# offset and the clock are still reported.
MIN_WINDOW_FOR_PERCENT_S = 2.0


def _git(root: Path, *args: str) -> tuple[int, str]:
    """Run one git command in `root`; (returncode, stdout). Never raises."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def _content(path: Path) -> str:
    """One hash for one path, with the non-file cases spelled out.

    A deleted file, a submodule directory and a symlink are all "changed" in
    ways a content hash would miss, so each gets its own spelling rather than
    the empty string a missing file would otherwise produce.
    """
    if path.is_symlink():
        try:
            return f"symlink:{os.readlink(path)}"
        except OSError as exc:
            return f"symlink-unreadable:{type(exc).__name__}"
    if path.is_dir():
        return "directory"
    if not path.exists():
        return "deleted"
    return _sha256(path)


def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        # A file that cannot be read is a fact about the tree, not a reason to
        # report the tree as unchanged: it is spelled out so two snapshots
        # cannot agree by both failing to read the same file.
        return f"unreadable:{type(exc).__name__}"


def _write_time(path: Path) -> int | None:
    """One file's own last-write time, in nanoseconds, or None if it has none.

    Nanoseconds rather than `st_mtime`'s float seconds, because the whole point
    is a write that landed and was put back inside ONE gate: a whole-second clock
    would call that a still tree. `lstat`, so a symlink is judged by the link
    rather than by whatever it points at, and a path that is not there (a
    deletion, a submodule) simply records nothing.
    """
    try:
        return path.lstat().st_mtime_ns
    except OSError:
        return None


def snapshot(root: Path) -> dict:
    """The fingerprint of `root` right now, as JSON-able data."""
    root = Path(root)
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return {"vcs": "none", "root": str(root), "at": time.time(),
                "why": "not a git worktree (nothing to compare)"}
    head_code, head_out = _git(root, "rev-parse", "HEAD")
    head = head_out.strip() if head_code == 0 else ""
    diff_code, diff = _git(root, "diff", "HEAD", "--no-color", "--binary")
    names_code, names_out = _git(root, "diff", "HEAD", "--name-only", "-z")
    listed_code, listed = _git(root, "ls-files", "--others",
                               "--exclude-standard", "-z")
    known_code, known = _git(root, "ls-files", "-z")
    # An unborn HEAD (a fresh `git init`) has nothing to diff: empty, not an
    # error. But a tree that HAS a HEAD and whose git commands still failed is
    # a tree this gate could not READ, and calling that an empty diff is the
    # false pass the gate exists to refuse — so it is spelled unjudgeable (no
    # REQUIRED_FIELDS, so `_moves` answers None) rather than "nothing moved".
    if head_code == 0 and (diff_code != 0 or names_code != 0
                           or listed_code != 0 or known_code != 0):
        return {"vcs": "git", "root": str(root), "at": time.time(),
                "why": "git could not read this worktree (cannot judge)"}
    diff_text = diff if diff_code == 0 else ""
    paths = sorted(part for part in names_out.split("\0") if part) \
        if names_code == 0 else []
    tracked = {rel: _content(root / rel) for rel in paths}
    untracked = {}
    for rel in sorted(part for part in listed.split("\0") if part):
        untracked[rel] = _content(root / rel)
    # One write time per path, for files whose CONTENT the next snapshot may
    # agree about: every tracked file (a clean one can be edited and put back
    # too, and the change would otherwise never appear) plus every untracked
    # file. IGNORED paths are not here on purpose — the gates rewrite
    # `.coverage`, `tests/report*.xml` and `__pycache__` every run, and a rule
    # that fired on those would refuse the run that wrote them.
    written = {}
    for rel in sorted(part for part in known.split("\0") if part):
        stamp = _write_time(root / rel)
        if stamp is not None:
            written[rel] = stamp
    for rel in untracked:
        stamp = _write_time(root / rel)
        if stamp is not None:
            written[rel] = stamp
    return {
        "vcs": "git",
        "root": str(root),
        "head": head,
        "tracked": tracked,
        "tracked_paths": sorted(tracked),
        "tracked_diff": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
        "tracked_bytes": len(diff_text),
        "untracked": untracked,
        # Path -> the file's own last-write time. Not content, so `fingerprint`
        # leaves it out, and `differences` reads it only where two snapshots
        # already AGREE about a path's content: that pair is a write that came
        # and went, which no hash can see.
        "written": written,
        # WHEN the scan finished. Every byte above was read at or before this,
        # which is what makes it an honest right edge for a window: a difference
        # between two snapshots happened after the earlier `at` and before the
        # later one, so a changed file's own mtime can be placed inside it.
        "at": time.time(),
    }


def fingerprint(data: dict) -> dict:
    """A snapshot's CONTENT, without its clocks.

    Two snapshots of an unchanged tree differ in `at` and in `written` (one
    write time per path) and in nothing else, while the refusal itself is
    `differences`, which reads those clocks to report a REVERTED write but never
    to decide that a file's content moved. Spelled out so "the same tree stamps
    the same" is a claim about the content a run actually judges, rather than an
    accident of comparing two dicts that happen to include the time of day.
    """
    return {key: value for key, value in data.items()
            if key not in ("at", "written")}


def _moves(before: dict, after: dict):
    """Every (kind, relation, path) the two snapshots disagree about, or None.

    None is "cannot be judged" — not a git worktree, or a snapshot missing its
    own fields — and callers must never read it as "nothing moved".

    The flat shape exists because there are two consumers: `differences` renders
    these rows as sentences, and `when_notes` asks the clock about exactly the
    paths they name. Two computations of "what moved" could disagree, and a
    refusal that names a file the timing lines then ignore is worse than no
    timing at all.
    """
    if before.get("vcs") != "git" or after.get("vcs") != "git":
        return None
    # Key PRESENCE, not truthiness: an unborn HEAD with nothing untracked is a
    # legitimate (empty) snapshot, while one missing its own fields is a file
    # somebody truncated — and reading that as "nothing moved" is exactly the
    # false pass this gate exists to refuse. Judged on BOTH sides: a truncated
    # `after` used to sail through as "nothing moved" too.
    if not set(REQUIRED_FIELDS) <= set(before) \
            or not set(REQUIRED_FIELDS) <= set(after):
        return None
    rows = []
    for kind in ("tracked", "untracked"):
        was, now = before.get(kind) or {}, after.get(kind) or {}
        for rel in sorted(set(now) - set(was)):
            rows.append((kind, "appeared", rel))
        for rel in sorted(set(was) - set(now)):
            rows.append((kind, "vanished", rel))
        for rel in sorted(set(was) & set(now)):
            if was[rel] != now[rel]:
                rows.append((kind, "changed", rel))
    # A write that was REVERTED inside the window leaves the loops above nothing
    # to find — the two snapshots agree about the bytes — and it is still a
    # second writer, because the suite read that path while it was different. The
    # only evidence left behind is the file's own write time, so a path the two
    # snapshots agree about (same recorded content) whose write time moved is a
    # row of its own. A pair of snapshots from before that evidence existed
    # simply has none: never a wrong answer, only a quieter one.
    if "written" in before and "written" in after:
        for rel in sorted(set(before["written"]) & set(after["written"])):
            if before["written"][rel] == after["written"][rel]:
                continue
            if _recorded(before, rel) != _recorded(after, rel):
                continue        # a content difference: already named above
            kind = "tracked" if _tracked_path(before, rel) else "untracked"
            rows.append((kind, "restored", rel))
    return rows


def _recorded(data: dict, rel: str) -> str | None:
    """The content a snapshot holds for one path, or None when it holds none.

    None is a CLEAN tracked file: it has no hash because it matches HEAD, which
    is a fact two snapshots can agree about — the case a reverted write leaves
    behind.
    """
    for kind in ("tracked", "untracked"):
        mapping = data.get(kind) or {}
        if rel in mapping:
            return mapping[rel]
    return None


def _tracked_path(data: dict, rel: str) -> bool:
    """Whether `rel` is a TRACKED path in this snapshot (dirty or clean)."""
    if rel in (data.get("tracked") or {}):
        return True
    return rel in (data.get("written") or {}) and rel not in (data.get("untracked") or {})


def _restored(rows: list) -> list[str]:
    """The paths a write touched and put back, which no content hash can see."""
    return sorted(rel for _, how, rel in rows if how == "restored")


def differences(before: dict, after: dict) -> list[str]:
    """Every way the tree moved, in sentences that name what moved."""
    if before.get("vcs") != "git" or after.get("vcs") != "git":
        return ["the tree could not be stamped (not a git worktree), so a "
                "change during the run cannot be ruled out"]
    rows = _moves(before, after)
    if rows is None:
        return ["the saved stamp is missing its own fields, so it cannot be "
                "compared"]
    out = []
    if before.get("head") != after.get("head"):
        out.append(f"HEAD moved: {before.get('head') or '(none)'} -> "
                   f"{after.get('head') or '(none)'}")
    # A restored write is not a content change, so it is kept OUT of the content
    # lines: `tracked files changed: ...` above a path whose bytes did not change
    # would be a sentence that contradicts itself.
    listed = [row for row in rows if row[0] == "tracked" and row[1] != "restored"]
    if listed:
        # Only the paths that MOVED, which is the point of per-file hashes: on a
        # shared checkout most of the tree is already dirty, and naming all of it
        # would send the reader hunting for the one a second writer touched.
        out.append("tracked files changed: " + _listing(
            [rel for _, how, rel in listed if how == "appeared"],
            [rel for _, how, rel in listed if how == "vanished"],
            [rel for _, how, rel in listed if how == "changed"]))
    elif before.get("tracked_diff") != after.get("tracked_diff"):
        out.append("the tracked diff moved without any file's content changing "
                   "(a mode change, or a deletion/rename edge)")
    for kind, how, rel in rows:
        if kind == "untracked" and how != "restored":
            out.append(f"untracked file {how}: {rel}")
    restored = _restored(rows)
    if restored:
        out.append("written during the window and restored: "
                   + ", ".join(restored)
                   + " — the content is what the checkpoint read, and only the "
                     "file's own write time moved")
    return out


def _listing(appeared: list, vanished: list, changed: list) -> str:
    parts = []
    if appeared:
        parts.append("newly dirty " + ", ".join(appeared))
    if vanished:
        parts.append("no longer dirty " + ", ".join(vanished))
    if changed:
        parts.append("rewritten " + ", ".join(changed))
    return "; ".join(parts) or "the diff moved without changing the file list"


def _clock(when: float) -> str:
    """A writer's timestamp on the reader's own wall clock."""
    return time.strftime("%H:%M:%S", time.localtime(when))


def _secs(seconds: float) -> str:
    """Seconds as a reader wants them: precise when small, whole when not."""
    if abs(seconds) < 1:
        return f"{seconds:.2f}s"
    if abs(seconds) < 10:
        return f"{seconds:.1f}s"
    return f"{round(seconds):.0f}s"


def _band(ratio: float) -> str:
    """Where in a window a fraction falls, in words a reader can scan."""
    if ratio < 0.1:
        return "the very start of it"
    if ratio < 0.4:
        return "the early part of it"
    if ratio < 0.6:
        return "the middle of it"
    if ratio < 0.9:
        return "the later part of it"
    return "the very end of it"


def _when(what: str, written: float, opened: float, width: float,
          window: str) -> str:
    """One sentence placing a writer's timestamp inside a window."""
    if written < opened:
        return (f"{what} was last written {_secs(opened - written)} BEFORE "
                f"{window} opened ({_clock(written)}), so the previous "
                f"checkpoint read it while that write was still landing")
    offset = written - opened
    if offset > width:
        return (f"{what} was last written at the very end of {window} or just "
                f"after it ({_clock(written)})")
    through = ""
    if width >= MIN_WINDOW_FOR_PERCENT_S:
        ratio = offset / width
        through = f", about {round(ratio * 100)}% through — {_band(ratio)}"
    return (f"{what} was last written {_secs(offset)} into {window} "
            f"({_secs(width)} long){through}, at {_clock(written)}")


def _commit_time(root: Path) -> float | None:
    """When HEAD was committed — the clock for the one move with no file to ask."""
    code, out = _git(root, "log", "-1", "--format=%ct")
    if code != 0:
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def when_notes(root: Path, before: dict, after: dict, *, window: str,
               opened_at: float | None = None) -> list[str]:
    """Roughly WHEN inside the window the writes landed, in sentences.

    The clock is the writer's own. A changed file's `mtime` is when its bytes were
    last written, which is the question a reader of a refusal actually has — "was
    this edited while the suite ran, or before it started?" — and nothing in this
    process is watching the tree, so a timestamp the writer left behind is the
    only evidence there is.

    `window` names the span in the reader's terms ("the tests gate's window").
    `opened_at` lets a caller open it later than `before`: the tree verdict does,
    because the write it catches is the one no gate saw and a whole-run window
    would say "4 800s into the run" about a file written seconds earlier.

    Empty when there is no clock to consult — a pair that cannot be judged, a
    snapshot from before the clock existed (no `at`), a vanished path (a deletion
    has no mtime). Never an error: this is a second, weaker answer about WHEN,
    and the refusal is about WHAT.
    """
    if before.get("vcs") != "git" or after.get("vcs") != "git":
        return []
    rows = _moves(before, after)
    if rows is None:
        return []
    opened = before.get("at") if opened_at is None else opened_at
    closed = after.get("at")
    if not isinstance(opened, (int, float)) or not isinstance(closed, (int, float)):
        return []
    opened, width = float(opened), float(closed) - float(opened)
    if width <= 0:
        # A clock that went backwards (NTP, a manual change) makes every offset
        # and fraction below a lie; "no opinion" beats a negative one.
        return []
    notes = []
    if before.get("head") != after.get("head"):
        committed = _commit_time(root)
        if committed is not None:
            notes.append(_when(f"the new HEAD ({(after.get('head') or '(none)')[:12]})",
                               committed, opened, width, window))
    seen = set()
    for _, _, rel in rows:
        if rel in seen:
            continue
        seen.add(rel)
        try:
            written = (root / rel).stat().st_mtime
        except OSError:
            continue          # a deletion has no clock to ask
        notes.append(_when(rel, written, opened, width, window))
    if len(notes) > MAX_WHEN_LINES:
        notes = notes[:MAX_WHEN_LINES] + [
            f"(and {len(notes) - MAX_WHEN_LINES} more changed path(s), not timed)"]
    return notes


def _slug(label: str) -> str:
    """A filename that says which gate, whatever the label contains."""
    keep = [ch if (ch.isalnum() or ch in "._-") else "-" for ch in label.strip()]
    return ("".join(keep).strip("-") or "gate")[:60]


def checkpoint(root: Path, directory: Path, label: str,
               report: list | None = None) -> tuple[int, list[str]]:
    """One checkpoint: has the tree moved since the previous one, and how?

    Returns (0 unchanged, 1 moved, 2 no opinion) plus what moved, and records the
    answer under DIR. The status is for the caller to ignore if it likes —
    ci/gates.sh takes a checkpoint after every gate and lets the two-writer gate
    do the refusing — while the log is the point: it says which gate the tree
    moved under, a fact no end-to-end comparison can recover, and roughly where
    inside that gate's window the write landed.

    `report`, when given, collects those `when` sentences for a caller that wants
    to print them as well (the CLI does); the log gets them either way.
    """
    directory.mkdir(parents=True, exist_ok=True)
    previous = directory / "latest.json"
    before = None
    if previous.exists():
        try:
            before = json.loads(previous.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = None            # a truncated checkpoint is "no previous"
    now = snapshot(root)
    sequences = []
    for path in directory.glob("*-*.json"):
        head = path.name.split("-", 1)[0]
        if head.isdigit():
            sequences.append(int(head))
    name = f"{1 + max(sequences or [0]):03d}-{_slug(label)}.json"
    payload = json.dumps(now, indent=2) + "\n"
    (directory / name).write_text(payload, encoding="utf-8")
    previous.write_text(payload, encoding="utf-8")
    # Written ONCE: it is what the run started from, which is what the verdict
    # has to be about, however many checkpoints follow it.
    first = directory / "first.json"
    if not first.exists():
        first.write_text(payload, encoding="utf-8")
    # The order matters: a tarball install gets "no opinion" from its FIRST
    # checkpoint too, not only from the ones after it. There is no previous
    # snapshot to compare against there, and "nothing to compare" must not read
    # as "unchanged".
    if now.get("vcs") != "git":
        return 2, []
    if before is None:
        return 0, []
    if before.get("vcs") != "git":
        return 2, []
    moved = differences(before, now)
    if moved:
        timing = when_notes(root, before, now,
                            window=f"the {label} gate's window")
        if report is not None:
            report.extend(timing)
        with open(directory / "moves.txt", "a", encoding="utf-8") as handle:
            handle.write(f"during the {label} gate:\n")
            for line in moved:
                handle.write(f"  - {line}\n")
            for line in timing:
                handle.write(f"  - when: {line}\n")
    return (1 if moved else 0), moved


def rebase(directory: Path, label: str) -> int:
    """Move the baseline onto the newest checkpoint: one LEG of a run, not one run.

    Called by ci/gates.sh at a collision, BEFORE the collided gate is re-run, so
    that everything the run says from then on is about one tree: the one the
    re-run and the gates after it were measured against. Returns 0 moved, 2 no
    opinion (no snapshot to move onto, or a snapshot that is not a git tree) —
    the caller treats that as "cannot resume", never as "resumed".
    """
    first = directory / "first.json"
    try:
        data = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 2
    if not isinstance(data, dict) or data.get("vcs") != "git":
        return 2
    data["baseline"] = f"the tree moved during the {label} gate"
    try:
        first.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return 2
    return 0


def _describe(data: dict) -> str:
    if data.get("vcs") != "git":
        return f"stamp: no opinion — {data.get('why')}"
    head = (data.get("head") or "(no commit yet)")[:12]
    return (f"stamp: {head}, {len(data.get('tracked') or {})} tracked "
            f"change(s), {len(data.get('untracked') or {})} untracked file(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="the worktree to stamp")
    parser.add_argument("--save", metavar="FILE",
                        help="write the snapshot here (the baseline)")
    parser.add_argument("--compare", metavar="FILE",
                        help="compare the tree against that snapshot")
    parser.add_argument("--checkpoint", metavar="DIR",
                        help="checkpoint into DIR, appending what moved under "
                             "the --label of the gate that just ran")
    parser.add_argument("--rebase", metavar="DIR",
                        help="move the baseline onto the newest checkpoint, so a "
                             "resumed run is judged against the tree the "
                             "re-run started from")
    parser.add_argument("--since", metavar="FILE",
                        help="with --compare: the newest checkpoint, so the "
                             "timing lines are placed in the window since THAT "
                             "snapshot rather than since the baseline (the "
                             "verdict is unchanged)")
    parser.add_argument("--label", default="gate",
                        help="the gate being checkpointed or rebased (with "
                             "--checkpoint/--rebase)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.rebase:
        if rebase(Path(args.rebase), args.label) != 0:
            print(f"cannot rebase {args.rebase}: there is no git checkpoint to "
                  f"move the baseline onto")
            return 2
        print(f"baseline moved onto the state after the {args.label} gate")
        return 0
    if args.checkpoint:
        notes: list = []
        status, moved = checkpoint(root, Path(args.checkpoint), args.label, notes)
        if status == 2:
            print(f"checkpoint {args.label}: no opinion (not a git worktree)")
            return 2
        print(f"checkpoint {args.label}: "
              + (f"moved — {'; '.join(moved)}" if moved else "unchanged"))
        for note in notes:
            print(f"  when: {note}")
        return status
    if args.compare:
        try:
            with open(args.compare, "r", encoding="utf-8") as handle:
                before = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"cannot compare: the saved stamp could not be read ({exc})")
            return 2
        now = snapshot(root)
        if before.get("vcs") != "git" or now.get("vcs") != "git":
            # A tarball install has no HEAD and no ignore rules, so "did it
            # move?" has no answer here. No opinion, never a clean bill.
            print("cannot judge: no opinion — not a git worktree, so a change "
                  "during the run cannot be ruled out")
            return 2
        # Which tree this verdict is about: a baseline written by the run's first
        # checkpoint is the one it started with, while a REBASED one (a resumed
        # run) is the state after a collision — saying "since the run started"
        # about that would name the wrong tree.
        baseline = before.get("baseline") or "start"
        moved = differences(before, now)
        # The clock's window, which is NOT the verdict's baseline: the write this
        # comparison catches is the one no gate saw (it landed after the last
        # checkpoint), so `--since` opens the window at that checkpoint. A
        # whole-run window would place it "4 800s into the run" — true, and
        # useless. Only LATER snapshots narrow the window, and one that is
        # missing, unreadable or older than the baseline is ignored rather than
        # used: a window the verdict is not about would be a worse answer than a
        # coarse one.
        opened, window = before, "the window since the baseline"
        if args.since:
            try:
                with open(args.since, "r", encoding="utf-8") as handle:
                    since = json.load(handle)
            except (OSError, ValueError):
                since = None
            if (isinstance(since, dict)
                    and isinstance(since.get("at"), (int, float))
                    and isinstance(before.get("at"), (int, float))
                    and since["at"] >= before["at"]):
                opened = since
                window = "the window since the last checkpoint"
        timing = when_notes(root, before, now, window=window,
                            opened_at=opened.get("at"))
        # A caller that wants both the verdict AND the new snapshot (the run's
        # per-gate checkpoints) asks for both in one call: one stamp of the tree
        # per gate, not two.
        if args.save:
            try:
                Path(args.save).write_text(json.dumps(now, indent=2) + "\n",
                                           encoding="utf-8")
            except OSError as exc:
                print(f"could not write the stamp to {args.save}: {exc}")
        if not moved:
            print("worktree unchanged since "
                  + ("the run started" if baseline == "start" else baseline))
            return 0
        print("REFUSED — the worktree changed "
              + ("while the gates were running:" if baseline == "start"
                 else "again after the run resumed:"))
        for line in moved:
            print(f"  - {line}")
        for line in timing:
            print(f"  - when: {line}")
        print("This run cannot be trusted as evidence about either revision: "
              "re-run it on a still tree (see ci/worktree_stamp.py).")
        return 1
    data = snapshot(root)
    if args.save:
        try:
            Path(args.save).write_text(json.dumps(data, indent=2) + "\n",
                                       encoding="utf-8")
        except OSError as exc:
            print(f"could not write the stamp to {args.save}: {exc}")
            return 2
        print(_describe(data))
        return 0
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
