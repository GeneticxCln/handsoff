"""The two-writer gate: a run on a tree that moved is not evidence about either revision.

`ci/gates.sh` stamps the worktree before the first gate, checkpoints after every
one, and compares after the last. When a checkpoint sees the tree move, the run
RESUMES instead of being thrown away: the collided gate's attempt is discarded
(its window held the write, so its result is about a mixture), the baseline moves
onto the state the collision left, and that gate is re-run against it — so every
gate from the collision onward is judged against one tree. The gates before it
keep their rows and are reported as CARRIED OVER, because they describe the tree
as it was. Three things are worth pinning, and none is arithmetic:

  * the STAMP — that it moves on every way a tree can change (an edit, a new
    file, a deletion, a commit) and that it does NOT move when the gates write
    their own artifacts, which is what makes the gate usable rather than
    self-refusing. That second half is a claim about the repo's REAL
    `.gitignore`, so it is tested by copying that file into a scratch worktree
    and letting git decide — if somebody un-ignores `.coverage` or
    `tests/report*.xml`, this is the test that notices;
  * the BASELINE — that a checkpoint never replaces it (a baseline rewritten at
    every checkpoint forgets an early change), while an explicit `--rebase` at a
    collision does move it, and says which tree it now is;
  * the WIRING — that the baseline exists before any gate runs, that a collision
    RESUMES instead of refusing, that the run stops when one gate's window holds
    two writes, that the split between carried-over and current verdicts is
    reported, and that a two-writer failure does not print the SUITE's failure
    digest (which would explain the wrong failure);
  * the COMPARISON — that a gate which ran twice is held up against itself: the
    same gate answering PASS and then FAIL is the one thing the rows and the
    stamp together cannot say, and it is reported as the move changing the
    outcome rather than left as two rows for a reader to diff;
  * the CLOCK — that the `when:` lines place a real write inside a real window,
    at the offset the file's own `mtime` says, in the words each of its three
    shapes needs (inside, before, after), and that they degrade to NOTHING
    rather than to a wrong answer when there is no clock to read. Every placement
    below is made with `os.utime` against a window the test chose, because a
    guard that waited for time to pass would be a race, not a property.

Every stamp here is taken by the shipped `ci/worktree_stamp.py`, through its CLI
for the refusal and the rebase, so the test proves the mechanism the gate actually
runs rather than a re-implementation of it. The end-to-end runs are the real
`ci/gates.sh` on a scratch repo whose interpreter is a wrapper that edits a
tracked file — the writer is counted in interpreter CALLS, not seconds, so each
one lands in a known gate without a single sleep.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import _load as _load_module, run_driver, sandbox_env

HERE = Path(__file__).resolve().parent.parent
GATES = HERE / "ci" / "gates.sh"
STAMP = HERE / "ci" / "worktree_stamp.py"


@pytest.fixture(scope="module")
def W():
    return _load_module("worktree_stamp", STAMP)


@pytest.fixture(scope="module")
def gates_source() -> str:
    return GATES.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def git_env() -> dict:
    """A git that cannot read the developer's config, and cannot prompt."""
    env = sandbox_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str, env: dict, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Stamp Test",
         "-c", "user.email=stamp@example.invalid", "-c", "commit.gpgsign=false",
         *args],
        capture_output=True, text=True, env=env, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


@pytest.fixture
def repo(tmp_path: Path, git_env: dict) -> Path:
    """A scratch worktree that uses THIS repo's real ignore rules."""
    root = tmp_path / "worktree"
    (root / "tests").mkdir(parents=True)
    (root / ".gitignore").write_text((HERE / ".gitignore").read_text(encoding="utf-8"),
                                     encoding="utf-8")
    (root / "app.py").write_text("print('one')\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n",
                                                encoding="utf-8")
    (root / "notes.md").write_text("untracked to begin with\n", encoding="utf-8")
    _git(root, "init", "-q", env=git_env)
    _git(root, "add", ".gitignore", "app.py", "tests/test_app.py", env=git_env)
    _git(root, "commit", "-q", "-m", "initial", env=git_env)
    return root


def _shell_function(source: str, name: str) -> str:
    """The body of a bash function, from its opening brace to its closing one.

    `conftest.method_source` slices a PYTHON `def`, and the gate this file pins
    is a shell function — asking it for one returns the empty string, which
    would make the wiring assertions below vacuous.
    """
    lines = source.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith(f"{name}() {{")), None)
    if start is None:
        return ""
    for i in range(start + 1, len(lines)):
        if lines[i] == "}":
            return "\n".join(lines[start:i + 1])
    return ""


def _cli(*args: str) -> subprocess.CompletedProcess:
    """The shipped CLI, run the way ci/gates.sh runs it.

    Through conftest's sandboxed runner, not a hand-built child: the stamp's own
    `git` calls then resolve a throw-away HOME instead of the developer's
    config, and the suite's "no python child built by hand" guard stays true.
    """
    return run_driver([str(STAMP), *args], cwd=HERE, capture_output=True,
                      text=True, timeout=120)


class TestTheStamp:
    """What moves it, and what deliberately does not."""

    def test_the_same_tree_stamps_the_same(self, W, repo):
        """One tree, two reads: only the clock differs, and the verdict is blind
        to it — `differences` never reads `at`, which is what lets the same
        snapshot carry the window a `when:` line is placed in."""
        before, after = W.snapshot(repo), W.snapshot(repo)
        assert W.fingerprint(before) == W.fingerprint(after), (
            "a stamp that changes on its own would refuse every run")
        assert W.differences(before, after) == [], (
            "the clock must not move the stamp")
        assert after["at"] >= before["at"], (
            "a window with no width cannot place anything")

    def test_an_edit_to_a_tracked_file_moves_it_and_names_it(self, W, repo):
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        after = W.snapshot(repo)
        assert before != after
        moved = W.differences(before, after)
        assert any("app.py" in line for line in moved), (
            f"the refusal has to NAME what moved, got {moved!r}")

    def test_the_refusal_names_only_what_moved(self, W, repo, git_env):
        """A shared checkout is mostly dirty; point at the move, not the dirt.

        The first version of this stamp hashed the whole diff, so a refusal on
        this repo named all 28 files that were already modified when the run
        started — a message that sends the reader hunting for the one a second
        writer actually touched.
        """
        (repo / "tests" / "test_app.py").write_text("def test_ok():\n    assert 1\n",
                                                    encoding="utf-8")
        (repo / "notes.md").write_text("dirty before the run\n", encoding="utf-8")
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        joined = " ".join(W.differences(before, W.snapshot(repo)))
        assert "app.py" in joined
        assert "test_app.py" not in joined and "notes.md" not in joined, (
            f"only the move may be named, got: {joined!r}")

    def test_an_already_dirty_file_that_changes_again_is_named(self, W, repo):
        """The case a per-file hash exists for, and a constant cannot do.

        A file that was already dirty when the run started, edited AGAIN while
        it ran: the set of dirty paths does not change, so only the per-file
        content hash can see it — and the sweep found exactly this hole in the
        guards (replacing every hash with one constant left the suite green).
        """
        (repo / "app.py").write_text("print('dirty before the run')\n",
                                     encoding="utf-8")
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('and edited during it')\n",
                                     encoding="utf-8")
        joined = " ".join(W.differences(before, W.snapshot(repo)))
        assert "app.py" in joined, (
            f"an already-dirty file edited again must still be named: {joined!r}")

    def test_a_mode_change_moves_it_without_changing_content(self, W, repo):
        """A chmod moves no content, so the whole diff is hashed as well.

        Both shapes of it: a CLEAN file that only gains a mode is named like any
        other new change, while a file that was already dirty (so the per-file
        mapping sees identical content) is caught by the diff hash instead.
        """
        before = W.snapshot(repo)
        (repo / "tests" / "test_app.py").chmod(0o755)
        joined = " ".join(W.differences(before, W.snapshot(repo)))
        assert "test_app.py" in joined, "a clean file's mode change is a change"

        (repo / "app.py").write_text("print('already dirty')\n", encoding="utf-8")
        dirty = W.snapshot(repo)
        (repo / "app.py").chmod(0o755)
        joined = " ".join(W.differences(dirty, W.snapshot(repo)))
        assert "mode change" in joined, (
            "a mode change is invisible to a content hash, which is why the "
            "whole diff is hashed as well")

    def test_an_index_only_change_is_deliberately_not_a_tree_change(self, W, repo,
                                                                    git_env):
        """`git add` moves the index, not the files the suite imported.

        A limit, stated where a reader will find it: this gate is about the TREE
        a run read, so staging alone is not a second writer. A `git checkout`
        or `stash` does move the worktree, and is caught.
        """
        before = W.snapshot(repo)
        _git(repo, "update-index", "--chmod=+x", "app.py", env=git_env)
        assert W.differences(before, W.snapshot(repo)) == [], (
            "an index-only change does not alter what the suite tested")

    def test_the_shapes_that_leave_a_list_are_named_too(self, W, repo):
        """The refactor's own risk: "what moved" has ONE computation now, and the
        paths that LEAVE a list are the ones a naive rewrite drops.

        A tracked file restored to HEAD's bytes (`no longer dirty`) and an
        untracked file deleted both stop appearing in their list, and both are
        still trees the suite read differently.
        """
        (repo / "app.py").write_text("print('dirty when the run started')\n",
                                     encoding="utf-8")
        (repo / "notes.md").write_text("dirty before the run\n", encoding="utf-8")
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('one')\n", encoding="utf-8")
        (repo / "notes.md").unlink()
        joined = " ".join(W.differences(before, W.snapshot(repo)))
        assert "no longer dirty app.py" in joined, (
            f"a path leaving the dirty list is a change, not silence: {joined!r}")
        assert "untracked file vanished: notes.md" in joined, joined

    def test_a_new_untracked_file_moves_it(self, W, repo):
        before = W.snapshot(repo)
        (repo / "scratch.py").write_text("x = 1\n", encoding="utf-8")
        after = W.snapshot(repo)
        assert before != after
        assert any("scratch.py" in line and "appeared" in line
                   for line in W.differences(before, after))

    def test_an_edit_to_an_untracked_file_moves_it(self, W, repo):
        before = W.snapshot(repo)
        (repo / "notes.md").write_text("edited mid-run\n", encoding="utf-8")
        after = W.snapshot(repo)
        assert before != after, (
            "an untracked file is still code the suite may have imported")
        assert any("notes.md" in line for line in W.differences(before, after))

    def test_a_commit_moves_it(self, W, repo, git_env):
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('committed by the second writer')\n",
                                     encoding="utf-8")
        _git(repo, "add", "app.py", env=git_env)
        _git(repo, "commit", "-q", "-m", "mid-run commit", env=git_env)
        after = W.snapshot(repo)
        assert before != after
        assert any("HEAD moved" in line for line in W.differences(before, after)), (
            "a commit is a second writer too")

    def test_a_deletion_moves_it(self, W, repo):
        before = W.snapshot(repo)
        (repo / "app.py").unlink()
        after = W.snapshot(repo)
        assert before != after
        assert any("app.py" in line for line in W.differences(before, after))

    def test_the_gates_own_artifacts_do_not_move_it(self, W, repo):
        """The gate writes these on every run; counting them would refuse itself.

        This is the repo's REAL `.gitignore` being asked, not a list kept here:
        the scratch worktree was built from it, so un-ignoring `.coverage` or
        `tests/report*.xml` fails this test instead of turning every future gate
        run into a refusal.
        """
        before = W.snapshot(repo)
        (repo / ".coverage").write_bytes(b"\x00shard")
        (repo / ".coverage.host.1234.5678").write_bytes(b"\x00shard")
        (repo / "tests" / "report.xml").write_text("<testsuite/>\n", encoding="utf-8")
        (repo / "tests" / "report.first-failure.xml").write_text(
            "<testsuite/>\n", encoding="utf-8")
        (repo / "tests" / "__pycache__").mkdir(exist_ok=True)
        (repo / "tests" / "__pycache__" / "app.cpython-311.pyc").write_bytes(b"\x00")
        (repo / ".pytest_cache").mkdir(exist_ok=True)
        (repo / ".pytest_cache" / "CACHEDIR.TAG").write_text("x\n", encoding="utf-8")
        after = W.snapshot(repo)
        assert W.differences(before, after) == [], (
            "the artifacts the gates write moved the stamp — the gate would "
            f"refuse its own run: {W.differences(before, after)}")
        assert W.fingerprint(before) == W.fingerprint(after)

    def test_outside_a_worktree_it_has_no_opinion(self, W, tmp_path):
        data = W.snapshot(tmp_path)          # a tarball install: no git at all
        assert data["vcs"] == "none"
        assert "git worktree" in data["why"]
        assert W.differences(data, W.snapshot(tmp_path)), (
            "'no opinion' must never render as 'unchanged'")

    def test_a_junk_snapshot_is_not_an_empty_diff(self, W, repo):
        """A snapshot somebody hand-edited is a fact, not a clean tree."""
        before = W.snapshot(repo)
        assert W.differences({"vcs": "git"}, before), (
            "a snapshot missing its fields cannot be read as 'nothing moved'")


class TestAWriteThatCameAndWent:
    """A write put back inside a window: no content hash can see it, the clock can.

    Why this shape matters more than the others: it is the one that used to read
    as a STILL tree. Both snapshots agree about every byte of the file, so the
    hash-based refusal was silent, and the suite's verdict was about a path it had
    read in two different states. The evidence such a write cannot help leaving is
    the file's own write time, which is why the stamp carries one per path; the
    clocks here are set with `os.utime` instead of being waited for, so the guards
    are exact rather than a race.
    """

    @staticmethod
    def _put_back(path: Path, when: float) -> None:
        """Write the same bytes back and stamp a chosen write time."""
        path.write_bytes(path.read_bytes())
        os.utime(path, (when, when))

    @staticmethod
    def _without_clocks(data: dict) -> dict:
        """A snapshot as one from before the write times existed."""
        return {key: value for key, value in data.items() if key != "written"}

    def test_a_write_put_back_moves_the_stamp_though_no_hash_does(self, W, repo):
        before = W.snapshot(repo)
        self._put_back(repo / "app.py", before["at"] + 5)
        after = W.snapshot(repo)
        assert W.fingerprint(before) == W.fingerprint(after), (
            "the content is identical — which is exactly why this shape was "
            "invisible to a content hash")
        moved = W.differences(before, after)
        assert len(moved) == 1 and moved[0].startswith(
            "written during the window and restored: app.py "), (
            f"a reverted write is still a second writer, and one line of it: {moved!r}")
        assert "tracked files changed" not in moved[0], (
            "the content did NOT change, so a line saying it did would be a "
            "sentence that contradicts itself")

    def test_a_write_one_nanosecond_later_is_still_a_write(self, W, repo):
        """Sub-second, because that is where a save-and-revert lands.

        The clock is `st_mtime_ns` rather than `st_mtime`'s float seconds for
        exactly this case: a write that comes and goes inside the same second as
        the snapshot before it would otherwise read as a still tree.
        """
        before = W.snapshot(repo)
        stamp = before["written"]["app.py"]
        target = repo / "app.py"
        os.utime(target, ns=(stamp + 1, stamp + 1))
        if target.stat().st_mtime_ns == stamp:
            pytest.skip("this filesystem rounds write times to the second")
        assert stamp // 10**9 == (stamp + 1) // 10**9, (
            "the two clocks are in the same second on purpose")
        assert any("app.py" in line for line in W.differences(before, W.snapshot(repo))), (
            "a write well inside one second is still a write")

    def test_an_untracked_file_put_back_is_named_once(self, W, repo):
        """The map covers untracked files too, and they get the same one line."""
        before = W.snapshot(repo)
        self._put_back(repo / "notes.md", before["at"] + 5)
        moved = W.differences(before, W.snapshot(repo))
        assert len(moved) == 1 and "notes.md" in moved[0] and "restored" in moved[0], (
            f"an untracked file is code the suite may have imported: {moved!r}")

    def test_a_clean_tracked_file_is_watched_too(self, W, repo):
        """The case a watch over DIRTY files only would miss.

        A clean file that is edited and put back is in no content map either
        time, so `written` has to cover every tracked file, not just the ones a
        `git diff` happened to list.
        """
        data = W.snapshot(repo)
        assert "app.py" in data["written"] and "tests/test_app.py" in data["written"]
        assert "notes.md" in data["written"], (
            "an untracked file is still code the suite may have imported")
        before = W.snapshot(repo)
        self._put_back(repo / "tests" / "test_app.py", before["at"] + 5)
        assert any("test_app.py" in line
                   for line in W.differences(before, W.snapshot(repo))), (
            "a clean file's write is a write")

    def test_a_read_is_not_a_write(self, W, repo):
        before = W.snapshot(repo)
        (repo / "app.py").read_text(encoding="utf-8")
        assert W.differences(before, W.snapshot(repo)) == [], (
            "stamping twice must not accuse the tree of moving")

    def test_a_content_change_is_reported_once_and_not_as_restored(self, W, repo):
        """The one shape that could be reported twice: dirty, then dirty again.

        That write moves the content AND the write time, and only the content
        line may mention it — two lines for one fact would make a refusal look
        like two writers.
        """
        (repo / "app.py").write_text("print('dirty when the run started')\n",
                                     encoding="utf-8")
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('and edited during it')\n",
                                     encoding="utf-8")
        joined = " ".join(W.differences(before, W.snapshot(repo)))
        assert "rewritten app.py" in joined, joined
        assert "restored" not in joined, (
            "a write that CHANGED the content is not also a write that put it "
            f"back: one line per fact, got {joined!r}")

    def test_the_write_times_are_not_part_of_the_fingerprint(self, W, repo):
        data = W.snapshot(repo)
        assert "written" in data, "the stamp has to carry the evidence at all"
        assert "written" not in W.fingerprint(data), (
            "'the same tree stamps the same' is a claim about CONTENT; write "
            "times are clocks, like `at`")

    def test_the_artifacts_the_gates_rewrite_are_not_watched(self, W, repo):
        """The rule must not fire on the run's own writes.

        The gates rewrite `.coverage`, `tests/report*.xml` and `__pycache__` on
        every run; a write-time map that covered ignored paths would turn every
        future run into a refusal of itself.
        """
        (repo / ".coverage").write_bytes(b"\x00one")
        (repo / "tests" / "report.xml").write_text("<a/>\n", encoding="utf-8")
        before = W.snapshot(repo)
        (repo / ".coverage").write_bytes(b"\x00two")
        (repo / "tests" / "report.xml").write_text("<b/>\n", encoding="utf-8")
        after = W.snapshot(repo)
        assert W.differences(before, after) == [], (
            "the gates rewrite their own artifacts every run, so those writes "
            "belong to the run")
        assert ".coverage" not in before["written"] \
            and "tests/report.xml" not in before["written"], (
                "an ignored path has no write time in the map at all")

    def test_a_snapshot_from_before_the_write_times_existed_still_judges(self, W, repo):
        """No evidence is a quieter answer, never a wrong one."""
        before = W.snapshot(repo)
        self._put_back(repo / "app.py", before["at"] + 5)
        after = W.snapshot(repo)
        assert W.differences(self._without_clocks(before),
                             self._without_clocks(after)) == [], (
            "the older pair has no write times to compare, which is silence "
            "rather than a verdict")
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        changed = W.snapshot(repo)
        assert any("app.py" in line for line in W.differences(
            self._without_clocks(after), self._without_clocks(changed))), (
            "and it still reports what it CAN see")

    def test_the_reverted_write_is_placed_in_the_window(self, W, repo):
        """The clock is the only evidence there is, so it is also the telling.

        The window is opened earlier than the earlier snapshot the way the tree
        verdict's `--since` opens it later, because a unit test's own window is
        milliseconds wide and a placement inside it could not be chosen by hand.
        """
        before = W.snapshot(repo)
        opened = before["at"] - 5
        self._put_back(repo / "app.py", opened + 2)
        after = W.snapshot(repo)
        notes = W.when_notes(repo, before, after,
                             window="the tests gate's window",
                             opened_at=opened)
        assert notes, "the write time is the whole evidence; it has to be printed"
        assert "app.py was last written" in notes[0], notes
        assert "into the tests gate's window" in notes[0], notes


class TestTheCheckpoints:
    """WHICH gate the tree moved under — the fact one end-to-end stamp cannot give."""

    def test_a_checkpoint_names_the_gate_the_tree_moved_under(self, W, repo, tmp_path):
        stamps = tmp_path / "stamps"
        assert W.checkpoint(repo, stamps, "start") == (0, [])
        (repo / "app.py").write_text("print('moved under coverage')\n",
                                     encoding="utf-8")
        status, moved = W.checkpoint(repo, stamps, "coverage")
        assert status == 1 and moved
        log = (stamps / "moves.txt").read_text(encoding="utf-8")
        assert "during the coverage gate:" in log
        assert "app.py" in log, f"the interval has to name the file too: {log!r}"

        # The next gate sees no change, so it adds nothing: the interval is the
        # gate that actually moved the tree, not every gate after it.
        assert W.checkpoint(repo, stamps, "shell") == (0, [])
        assert (stamps / "moves.txt").read_text(encoding="utf-8") == log

    def test_a_checkpoint_sees_a_write_that_was_reverted(self, W, repo, tmp_path):
        """A tree that LOOKS still, and is not: the shape the clock is for.

        Nothing about the content moved, so this checkpoint read as "unchanged"
        before the stamp carried write times — and the gate that ran underneath
        the write kept a verdict about a file it may have read in two states.
        """
        stamps = tmp_path / "stamps"
        assert W.checkpoint(repo, stamps, "start") == (0, [])
        target = repo / "app.py"
        target.write_bytes(target.read_bytes())
        os.utime(target, (time.time() + 1, time.time() + 1))
        status, moved = W.checkpoint(repo, stamps, "tests")
        assert status == 1 and moved, "a reverted write is a collision, not a still tree"
        log = (stamps / "moves.txt").read_text(encoding="utf-8")
        assert "during the tests gate:" in log, log
        assert "written during the window and restored: app.py" in log, log
        assert "when: app.py" in log, (
            f"the write time is the evidence, so it has to be placed: {log!r}")

    def test_an_unchanged_run_writes_no_log(self, W, repo, tmp_path):
        stamps = tmp_path / "stamps"
        W.checkpoint(repo, stamps, "start")
        W.checkpoint(repo, stamps, "tests")
        assert not (stamps / "moves.txt").exists() or not \
            (stamps / "moves.txt").read_text(encoding="utf-8")

    def test_the_first_snapshot_is_never_replaced(self, W, repo, tmp_path):
        """The verdict is about the tree the run STARTED with.

        A baseline rewritten on every checkpoint would quietly forget a change
        made early in the run and report the tree as still.
        """
        stamps = tmp_path / "stamps"
        opening = W.snapshot(repo)
        W.checkpoint(repo, stamps, "start")
        (repo / "app.py").write_text("print('early change')\n", encoding="utf-8")
        W.checkpoint(repo, stamps, "tests")
        (repo / "app.py").write_text("print('later change')\n", encoding="utf-8")
        W.checkpoint(repo, stamps, "coverage")
        first = json.loads((stamps / "first.json").read_text(encoding="utf-8"))
        assert W.fingerprint(first) == W.fingerprint(opening)
        assert W.differences(first, W.snapshot(repo)), (
            "the end comparison must still see a change made three gates ago")

    def test_latest_is_the_newest_snapshot(self, W, repo, tmp_path):
        stamps = tmp_path / "stamps"
        W.checkpoint(repo, stamps, "start")
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        W.checkpoint(repo, stamps, "tests")
        latest = json.loads((stamps / "latest.json").read_text(encoding="utf-8"))
        assert W.fingerprint(latest) == W.fingerprint(W.snapshot(repo))
        numbered = sorted(p.name for p in stamps.glob("[0-9][0-9][0-9]-*.json"))
        assert numbered == ["001-start.json", "002-tests.json"], (
            "one numbered snapshot per checkpoint, in order, beside the two "
            "fixed names (first.json, latest.json)")
        assert (stamps / "first.json").exists()

    def test_a_checkpoint_outside_git_has_no_opinion(self, W, tmp_path):
        plain = tmp_path / "tarball"
        plain.mkdir()
        (plain / "app.py").write_text("x = 1\n", encoding="utf-8")
        stamps = tmp_path / "stamps"
        assert W.checkpoint(plain, stamps, "start")[0] == 2, (
            "the FIRST checkpoint of a non-git tree is no opinion too, not OK")

    def test_a_truncated_checkpoint_is_not_a_previous_state(self, W, repo, tmp_path):
        stamps = tmp_path / "stamps"
        W.checkpoint(repo, stamps, "start")
        (stamps / "latest.json").write_text("{not json", encoding="utf-8")
        status, moved = W.checkpoint(repo, stamps, "tests")
        assert status == 0 and moved == [], (
            "a checkpoint that cannot be read is 'no previous', not a refusal")

    def test_a_rebase_moves_the_baseline_onto_the_collision(self, W, repo, tmp_path):
        """The one act that MAY replace `first.json`, and the reason it has to.

        A checkpoint must never rewrite the baseline (the test above this one), but
        a resumed run is judged against the tree its re-run started from — the
        state the collision left. So the baseline moves on explicit request only,
        and it carries what it now is, because the closing line of a resumed run
        would otherwise say "since the run started" about a tree it never saw.
        """
        stamps = tmp_path / "stamps"
        W.checkpoint(repo, stamps, "start")
        (repo / "app.py").write_text("print('moved under coverage')\n",
                                     encoding="utf-8")
        W.checkpoint(repo, stamps, "coverage")
        opening = json.loads((stamps / "first.json").read_text(encoding="utf-8"))
        assert W.differences(opening, W.snapshot(repo)), (
            "before the rebase the baseline is the pre-collision tree — which is "
            "what the old refusal was about")

        assert W.rebase(stamps, "coverage") == 0
        rebased = json.loads((stamps / "first.json").read_text(encoding="utf-8"))
        assert rebased["baseline"] == "the tree moved during the coverage gate", (
            "the baseline has to say which tree it is")
        assert W.differences(rebased, W.snapshot(repo)) == [], (
            "the rebased baseline is the state the re-run starts from")

    def test_a_rebase_with_no_checkpoint_has_no_opinion(self, W, tmp_path):
        """"Cannot resume" must never be spelled "resumed"."""
        assert W.rebase(tmp_path / "empty", "coverage") == 2
        plain = tmp_path / "tarball"
        plain.mkdir()
        (plain / "app.py").write_text("x = 1\n", encoding="utf-8")
        stamps = tmp_path / "stamps"
        W.checkpoint(plain, stamps, "start")       # no git: no usable snapshot
        assert W.rebase(stamps, "coverage") == 2, (
            "a snapshot with no git tree cannot become a baseline")


class TestWhenInsideTheWindow:
    """HOW FAR INTO a gate's window the writer landed, not only which gate it was.

    The clock is the writer's own: a changed file's `mtime` is when its bytes
    were last written. Nothing here watches the tree, so the answer is a
    PLACEMENT inside the span two checkpoints bracket rather than an observation,
    and every placement in this class is made by hand with `os.utime` against a
    window the test picked. A guard that slept until the offset "looked about
    right" would be a race wearing a property's clothes.
    """

    @staticmethod
    def _place(path: Path, when: float) -> None:
        """Put a file's last write at an exact second."""
        os.utime(path, (when, when))

    def _pair(self, W, repo, *, written_at, opened_at, closed_at):
        """Two snapshots with one file edited between them, at chosen clocks.

        The bytes written carry the clock, so consecutive calls inside one test
        change the file every time: a helper that wrote the same content twice
        would hand the second call NO changed path to place, and a guard would
        then pass on an empty answer instead of on the placement.
        """
        before = W.snapshot(repo)
        (repo / "app.py").write_text(f"print({written_at!r})\n", encoding="utf-8")
        after = W.snapshot(repo)
        self._place(repo / "app.py", written_at)
        before["at"], after["at"] = opened_at, closed_at
        return before, after

    def test_a_write_is_placed_inside_the_gate_window(self, W, repo):
        before, after = self._pair(W, repo, written_at=1_084.0, opened_at=1_000.0,
                                   closed_at=1_200.0)
        notes = W.when_notes(repo, before, after, window="the tests gate's window")
        assert len(notes) == 1, notes
        line = notes[0]
        assert line.startswith("app.py was last written ")
        assert "84s into the tests gate's window" in line
        assert "(200s long)" in line
        assert "about 42% through — the middle of it" in line
        assert time.strftime("%H:%M:%S", time.localtime(1_084.0)) in line, (
            "the reader wants the wall clock too, on their own clock")

    def test_a_narrower_window_moves_the_placement(self, W, repo):
        """`opened_at` is how the tree verdict places a write no gate saw.

        The same file, the same clock, two windows: the placement follows the
        window, so a caller that opens it at the newest checkpoint gets an answer
        about the tail of the run rather than about the whole of it — while the
        paths that moved still come from the pair the verdict is about.
        """
        before, after = self._pair(W, repo, written_at=1_084.0, opened_at=1_000.0,
                                   closed_at=1_200.0)
        wide = W.when_notes(repo, before, after, window="the run's window")[0]
        narrow = W.when_notes(repo, before, after,
                              window="the window since the last checkpoint",
                              opened_at=1_080.0)[0]
        assert "84s into the run's window" in wide, wide
        assert "4.0s into the window since the last checkpoint" in narrow, narrow
        assert "(120s long)" in narrow and "about 3% through" in narrow, narrow

    def test_a_window_too_short_for_a_fraction_says_so_by_omitting_it(self, W, repo):
        """A 0.3s window has no meaningful "42% through"; the offset still stands.

        The end-to-end runs below are exactly this shape — an interpreter call is
        milliseconds — so the omission is a case that actually ships.
        """
        before, after = self._pair(W, repo, written_at=1_000.1, opened_at=1_000.0,
                                   closed_at=1_000.3)
        line = W.when_notes(repo, before, after,
                            window="the compile gate's window")[0]
        assert "0.10s into the compile gate's window" in line
        assert "%" not in line, (
            "a fraction of a window too short to have parts is a made-up number")

    def test_every_band_boundary_is_where_the_words_say(self, W, repo):
        """The band is the scannable half of the answer, and its boundaries are
        only reachable at fractions one example would not pick.

        Each boundary is sampled a percentage point on either side of itself, so a
        boundary nudged in either direction reports the wrong words for a write
        that really is 39% of the way through a gate.
        """
        cases = {18: "the very start of it", 22: "the early part of it",
                 78: "the early part of it", 82: "the middle of it",
                 118: "the middle of it", 122: "the later part of it",
                 178: "the later part of it", 182: "the very end of it"}
        for offset, band in cases.items():
            before, after = self._pair(W, repo, written_at=1_000.0 + offset,
                                       opened_at=1_000.0, closed_at=1_200.0)
            line = W.when_notes(repo, before, after,
                                window="the tests gate's window")[0]
            assert band in line, f"{offset}s of a 200s window: {line}"

    def test_a_write_before_the_window_is_said_to_be_before_it(self, W, repo):
        """The earlier checkpoint read that file early and finished late.

        Clamping this to zero would read as "at the very start of the gate" — a
        different fact about a different write.
        """
        before, after = self._pair(W, repo, written_at=958.0, opened_at=1_000.0,
                                   closed_at=1_022.0)
        line = W.when_notes(repo, before, after, window="the shell gate's window")[0]
        assert "42s BEFORE the shell gate's window opened" in line
        assert "while that write was still landing" in line
        assert "%" not in line

    def test_a_write_at_the_close_is_not_given_a_fraction(self, W, repo):
        """An `mtime` after the closing checkpoint: a fraction of a window the
        write is outside of would be invented."""
        before, after = self._pair(W, repo, written_at=1_300.0, opened_at=1_000.0,
                                   closed_at=1_200.0)
        line = W.when_notes(repo, before, after, window="the tests gate's window")[0]
        assert "at the very end of the tests gate's window or just after it" in line
        assert "%" not in line

    def test_a_deletion_has_no_clock_to_ask(self, W, repo):
        """And the empty answer must not take the refusal down with it."""
        before = W.snapshot(repo)
        (repo / "app.py").unlink()
        after = W.snapshot(repo)
        assert W.differences(before, after), "the deletion is still a move"
        assert W.when_notes(repo, before, after,
                            window="the tests gate's window") == [], (
            "a vanished path has no mtime: nothing to place, and no error either")

    def test_a_head_move_is_timed_by_the_commit_itself(self, W, repo, git_env):
        """The one move with no file to ask has a clock of its own."""
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('committed mid-run')\n", encoding="utf-8")
        _git(repo, "add", "app.py", env=git_env)
        _git(repo, "commit", "-q", "-m", "mid-run", env=git_env)
        after = W.snapshot(repo)
        notes = W.when_notes(repo, before, after, window="the tests gate's window",
                             opened_at=after["at"] - 100)
        assert any(note.startswith("the new HEAD (") for note in notes), (
            f"a HEAD move is timed by its commit: {notes}")

    def test_a_snapshot_with_no_clock_still_yields_a_verdict(self, W, repo):
        """Graceful degradation: an older snapshot loses the timing, never the
        refusal and never to a crash."""
        before = W.snapshot(repo)
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        after = W.snapshot(repo)
        before.pop("at")
        assert W.differences(before, after), "the verdict does not need a clock"
        assert W.when_notes(repo, before, after,
                            window="the tests gate's window") == []

    def test_the_when_lines_are_capped_and_the_rest_are_counted(self, W, repo):
        """A checkout that moves hundreds of files: point at them, do not bury the
        reader — and never drop them in silence."""
        names = [f"many{i}.py" for i in range(W.MAX_WHEN_LINES + 3)]
        for name in names:
            (repo / name).write_text("x = 1\n", encoding="utf-8")
        before = W.snapshot(repo)
        for name in names:
            (repo / name).write_text("x = 2\n", encoding="utf-8")
        after = W.snapshot(repo)
        notes = W.when_notes(repo, before, after, window="the tests gate's window")
        assert len(notes) == W.MAX_WHEN_LINES + 1, notes
        assert all("was last written" in note for note in notes[:-1])
        assert notes[-1] == "(and 3 more changed path(s), not timed)", notes[-1]

    def test_the_checkpoint_log_carries_the_when_lines(self, W, repo, tmp_path):
        """The log is what the resumed section prints, so the placement has to
        reach it, under the gate's own heading, beside what moved."""
        stamps = tmp_path / "stamps"
        W.checkpoint(repo, stamps, "start")
        (repo / "app.py").write_text("print('moved under coverage')\n",
                                     encoding="utf-8")
        assert W.checkpoint(repo, stamps, "coverage")[0] == 1
        log = (stamps / "moves.txt").read_text(encoding="utf-8")
        assert "during the coverage gate:" in log
        assert "  - tracked files changed: " in log
        assert "  - when: app.py was last written " in log, (
            f"the placement has to reach the log the resumed section prints: {log!r}")
        assert "into the coverage gate's window" in log, (
            "and it has to name the window those seconds are measured inside")


class TestTheCli:
    """The refusal itself, through the command the gate runs."""

    def test_a_checkpoint_through_the_cli_says_which_gate_moved_it(self, repo, tmp_path):
        stamps = tmp_path / "stamps"
        first = _cli("--root", str(repo), "--checkpoint", str(stamps),
                     "--label", "start")
        assert first.returncode == 0 and "unchanged" in first.stdout
        (repo / "app.py").write_text("print('moved')\n", encoding="utf-8")
        second = _cli("--root", str(repo), "--checkpoint", str(stamps),
                      "--label", "coverage")
        assert second.returncode == 1
        assert "moved" in second.stdout and "app.py" in second.stdout
        assert "during the coverage gate:" in (stamps / "moves.txt").read_text(
            encoding="utf-8")

    def test_compare_refuses_a_write_that_was_reverted(self, repo, tmp_path):
        """The verdict, through the CLI the gate runs: content identical, refused.

        This is the shape that used to end a run GREEN with no collision at all.
        """
        baseline = tmp_path / "baseline.json"
        assert _cli("--root", str(repo), "--save", str(baseline)).returncode == 0
        target = repo / "app.py"
        target.write_bytes(target.read_bytes())
        os.utime(target, (time.time() + 1, time.time() + 1))
        out = _cli("--root", str(repo), "--compare", str(baseline))
        assert out.returncode == 1, (
            f"a write that was put back is still a second writer:\n{out.stdout}")
        assert "REFUSED" in out.stdout, out.stdout
        assert "written during the window and restored: app.py" in out.stdout

    def test_compare_can_write_the_next_snapshot_in_the_same_pass(self, repo, tmp_path):
        """One stamp of the tree per gate, not two."""
        baseline, nxt = tmp_path / "a.json", tmp_path / "b.json"
        assert _cli("--root", str(repo), "--save", str(baseline)).returncode == 0
        (repo / "app.py").write_text("print('moved')\n", encoding="utf-8")
        out = _cli("--root", str(repo), "--compare", str(baseline),
                   "--save", str(nxt))
        assert out.returncode == 1, "the verdict is still a refusal"
        assert json.loads(nxt.read_text(encoding="utf-8"))["tracked"], (
            "the new snapshot has to be written, or the chain of checkpoints "
            "breaks on the first change")

    def test_save_then_compare_is_clean(self, repo, tmp_path):
        stamp = tmp_path / "baseline.json"
        save = _cli("--root", str(repo), "--save", str(stamp))
        assert save.returncode == 0, save.stderr
        assert "stamp:" in save.stdout
        again = _cli("--root", str(repo), "--compare", str(stamp))
        assert again.returncode == 0
        assert "unchanged" in again.stdout

    def test_compare_refuses_an_edited_tree(self, repo, tmp_path):
        stamp = tmp_path / "baseline.json"
        assert _cli("--root", str(repo), "--save", str(stamp)).returncode == 0
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        out = _cli("--root", str(repo), "--compare", str(stamp))
        assert out.returncode == 1, (
            "a moved worktree must REFUSE the run, not warn about it")
        assert "REFUSED" in out.stdout
        assert "app.py" in out.stdout, "the refusal has to say what moved"
        assert "cannot be trusted" in out.stdout
        assert any("cannot be trusted as evidence about either revision: "
                   "re-run it on a still tree" in line
                   for line in out.stdout.splitlines()), (
            f"the refusal sentence prints as ONE physical line: {out.stdout!r}")

    def test_a_missing_snapshot_is_not_a_pass(self, repo, tmp_path):
        out = _cli("--root", str(repo), "--compare", str(tmp_path / "gone.json"))
        assert out.returncode == 2, "an unreadable baseline is 'no opinion'"
        assert "cannot compare" in out.stdout

    def test_a_rebased_baseline_names_the_tree_the_verdict_is_about(self, repo,
                                                                    tmp_path):
        """A resumed run's closing line, and its refusal, both name a TREE.

        `--compare` against a rebased baseline is the resumed leg's verdict, so
        "unchanged since the run started" would name a tree this run never tested;
        and a move after the rebase is the SECOND one, not the first.
        """
        stamps = tmp_path / "stamps"
        first = _cli("--root", str(repo), "--checkpoint", str(stamps),
                     "--label", "start")
        assert first.returncode == 0
        (repo / "app.py").write_text("print('moved')\n", encoding="utf-8")
        assert _cli("--root", str(repo), "--checkpoint", str(stamps),
                    "--label", "coverage").returncode == 1

        rebase = _cli("--root", str(repo), "--rebase", str(stamps),
                      "--label", "coverage")
        assert rebase.returncode == 0, rebase.stdout
        assert "baseline moved onto the state after the coverage gate" in rebase.stdout

        clean = _cli("--root", str(repo), "--compare", str(stamps / "first.json"))
        assert clean.returncode == 0, clean.stdout
        assert "unchanged since the tree moved during the coverage gate" in clean.stdout, (
            "the pass line has to name the tree it is about")

        (repo / "app.py").write_text("print('moved again')\n", encoding="utf-8")
        again = _cli("--root", str(repo), "--compare", str(stamps / "first.json"))
        assert again.returncode == 1
        assert "changed again after the run resumed" in again.stdout, (
            "the second move is not the first one — the run already resumed past it")

    def test_a_rebase_through_the_cli_needs_a_checkpoint(self, repo, tmp_path):
        out = _cli("--root", str(repo), "--rebase", str(tmp_path / "empty"),
                   "--label", "tests")
        assert out.returncode == 2, (
            "a rebase that cannot happen is 'no opinion', never 'resumed'")
        assert "cannot rebase" in out.stdout

    def test_the_refusal_places_the_write_in_time(self, repo, tmp_path):
        """Naming the file answers "what"; the reader's next question is "when"."""
        stamp = tmp_path / "baseline.json"
        assert _cli("--root", str(repo), "--save", str(stamp)).returncode == 0
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        out = _cli("--root", str(repo), "--compare", str(stamp))
        assert out.returncode == 1
        assert "  - when: app.py was last written " in out.stdout, (
            "the refusal has to place the write in time:\n" + out.stdout)
        assert "since the baseline" in out.stdout

    def test_the_window_can_be_narrowed_to_the_last_checkpoint(self, repo, tmp_path):
        """A post-checkpoint write is placed against the checkpoint, not the run.

        The write this comparison catches is the one no gate saw, so a whole-run
        window would report it as "4 800s into the run" — true and useless. The
        VERDICT stays the baseline's; only the clock's window moves.
        """
        stamps = tmp_path / "stamps"
        assert _cli("--root", str(repo), "--checkpoint", str(stamps),
                    "--label", "start").returncode == 0
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        assert _cli("--root", str(repo), "--checkpoint", str(stamps),
                    "--label", "shell").returncode == 1
        out = _cli("--root", str(repo), "--compare", str(stamps / "first.json"),
                   "--since", str(stamps / "latest.json"))
        assert out.returncode == 1
        assert "REFUSED — the worktree changed while the gates were running" in out.stdout, (
            "the verdict is still the baseline's")
        assert "the window since the last checkpoint" in out.stdout, (
            "the tail of the run is the window, not the whole of it:\n" + out.stdout)

    def test_a_stale_or_missing_since_never_opens_the_window_early(self, repo,
                                                                   tmp_path):
        """`--since` narrows the window and cannot widen it.

        A snapshot older than the verdict's own baseline (or one that cannot be
        read) would place the write in a span the verdict is not about — a worse
        answer than the coarse one, and one a reader cannot detect.
        """
        stamps = tmp_path / "stamps"
        stale = tmp_path / "stale.json"
        assert _cli("--root", str(repo), "--save", str(stale)).returncode == 0
        assert _cli("--root", str(repo), "--checkpoint", str(stamps),
                    "--label", "start").returncode == 0
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        old = _cli("--root", str(repo), "--compare", str(stamps / "first.json"),
                   "--since", str(stale))
        assert old.returncode == 1 and "since the baseline" in old.stdout, (
            "an older --since must be ignored, not used:\n" + old.stdout)
        gone = _cli("--root", str(repo), "--compare", str(stamps / "first.json"),
                    "--since", str(tmp_path / "gone.json"))
        assert gone.returncode == 1 and "since the baseline" in gone.stdout, (
            "an unreadable --since is the coarse window, never a failure:\n"
            + gone.stdout)

    def test_the_checkpoint_prints_the_when_line(self, repo, tmp_path):
        stamps = tmp_path / "stamps"
        assert _cli("--root", str(repo), "--checkpoint", str(stamps),
                    "--label", "start").returncode == 0
        (repo / "app.py").write_text("print('two')\n", encoding="utf-8")
        out = _cli("--root", str(repo), "--checkpoint", str(stamps),
                   "--label", "coverage")
        assert out.returncode == 1
        assert "when: app.py was last written " in out.stdout, (
            "a hand-run checkpoint wants the placement too:\n" + out.stdout)

    def test_outside_git_compare_refuses_to_judge(self, tmp_path):
        stamp = tmp_path / "baseline.json"
        plain = tmp_path / "tarball"
        plain.mkdir()
        (plain / "app.py").write_text("x = 1\n", encoding="utf-8")
        save = _cli("--root", str(plain), "--save", str(stamp))
        assert save.returncode == 0
        assert "no opinion" in save.stdout
        out = _cli("--root", str(plain), "--compare", str(stamp))
        assert out.returncode == 2, (
            "a directory with no git cannot be judged 'unchanged'")
        assert "no opinion" in out.stdout


class TestTheResumeEndToEnd:
    """The real gates script, on a scratch repo, with a writer inside a known gate.

    Not a mock and not a paraphrase: `ci/gates.sh` is copied whole, the gates it
    runs are the ones it discovers, and the "second writer" is the interpreter
    itself — a `$PYTHON` wrapper that appends to a tracked file on chosen CALLS.
    The first call is the baseline stamp, so a write counted from there lands
    provably inside a known gate with no sleep and no timing. The call counter and
    the write marker live OUTSIDE the worktree: the first version kept them in
    `root`, where counting a call made an untracked file appear and every
    checkpoint saw a move of its own making.
    """

    @pytest.fixture
    def scratch(self, tmp_path, git_env):
        root = tmp_path / "scratch"
        markers = tmp_path / "markers"
        markers.mkdir()
        (root / "ci").mkdir(parents=True)
        for name in ("gates.sh", "compile_all.py", "worktree_stamp.py"):
            shutil.copy2(HERE / "ci" / name, root / "ci" / name)
        shutil.copy2(HERE / ".gitignore", root / ".gitignore")
        (root / "app.py").write_text("x = 1\n", encoding="utf-8")
        (root / "other.py").write_text("y = 2\n", encoding="utf-8")
        wrapper = tmp_path / "python-that-writes.sh"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'MARKERS="{markers}"\n'
            f'ROOT="{root}"\n'
            'COUNT=$(cat "$MARKERS/calls" 2>/dev/null || echo 0)\n'
            "COUNT=$((COUNT + 1))\n"
            'printf "%s" "$COUNT" > "$MARKERS/calls"\n'
            # Three kinds of second writer, all keyed on interpreter CALLS: one
            # appends to a file the gates compile (moves the tree), one breaks a
            # file (turns the attempt red), and one writes and PUTS IT BACK inside
            # the same call — so every checkpoint finds the content it expects and
            # the file's own write time is the only thing the write leaves behind.
            # A write that also breaks is how the discarded-attempt path gets a
            # failure to discard.
            'WRITE_ON=$(cat "$MARKERS/write-on" 2>/dev/null || echo 2)\n'
            'case ",$WRITE_ON," in\n'
            '    *",$COUNT,"*)\n'
            '        printf "\\n# moved by the second writer\\n" >> "$ROOT/app.py"\n'
            '        : > "$MARKERS/wrote"\n'
            "        ;;\n"
            "esac\n"
            'BREAK_ON=$(cat "$MARKERS/break-on" 2>/dev/null || echo "")\n'
            'case ",$BREAK_ON," in\n'
            '    *",$COUNT,"*)\n'
            '        printf "def broken(:\n" > "$ROOT/other.py"\n'
            '        : > "$MARKERS/broke"\n'
            "        ;;\n"
            "esac\n"
            # `cp` back over itself writes the same bytes and stamps a new mtime,
            # which is exactly what an editor that saves and reverts leaves.
            'REVERT_ON=$(cat "$MARKERS/revert-on" 2>/dev/null || echo "")\n'
            'case ",$REVERT_ON," in\n'
            '    *",$COUNT,"*)\n'
            '        cp "$ROOT/app.py" "$MARKERS/app.before"\n'
            '        printf "\\n# written and put straight back\\n" >> "$ROOT/app.py"\n'
            '        cp "$MARKERS/app.before" "$ROOT/app.py"\n'
            '        : > "$MARKERS/reverted"\n'
            "        ;;\n"
            "esac\n"
            f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        (markers / "write-on").write_text("2", encoding="utf-8")
        _git(root, "init", "-q", env=git_env)
        _git(root, "add", ".gitignore", "app.py", "other.py", "ci", env=git_env)
        _git(root, "commit", "-q", "-m", "initial", env=git_env)
        return root, wrapper, markers

    def _run(self, scratch, git_env, *gates: str):
        root, wrapper, markers = scratch
        env = dict(git_env, PYTHON=str(wrapper))
        out = subprocess.run(["bash", "ci/gates.sh", *gates], cwd=str(root),
                             capture_output=True, text=True, env=env,
                             timeout=300, check=False)
        assert (markers / "wrote").exists() or (markers / "broke").exists() \
            or (markers / "reverted").exists(), (
                "the probe did not fire; the test proves nothing")
        return out

    def test_the_run_resumes_from_the_collision_instead_of_refusing(self, scratch,
                                                                   git_env):
        """The request, in one line: a collision re-runs from the collision.

        The write fires on interpreter call 2 — the compile gate itself — so that
        attempt is evidence about a mixture: it is reported VOID, the baseline
        moves onto the tree it left behind, and the gate is re-run. Everything the
        run then says is a verdict about the tree as it now stands.
        """
        out = self._run(scratch, git_env, "compile", "two-writer")
        assert out.returncode == 0, (
            f"a collision resumes rather than throwing the run away:\n{out.stdout}")
        assert "REFUSED" not in out.stdout, (
            "the refusal is what this replaces — the tree moved once, and once is "
            "absorbable")
        assert "resumed ============" in out.stdout
        assert "during the compile gate:" in out.stdout and "app.py" in out.stdout
        assert "VOID" in out.stdout and "discarded — it had PASS" in out.stdout, (
            "the collided attempt is discarded, and the run says so")
        assert "re-run after the move" in out.stdout, (
            "the re-run's row is what the run stands behind")
        assert "worktree unchanged since the tree moved during the compile gate" in out.stdout, (
            "the end comparison is against the tree the re-run started from")
        assert "carried over: none" in out.stdout, (
            "nothing ran before the collision, so no verdict is about an older tree")
        assert "  - when: app.py was last written " in out.stdout, (
            "the resumed section has to say roughly WHERE in the gate the write "
            f"landed, not only that it did:\n{out.stdout}")
        assert "into the compile gate's window" in out.stdout
        assert "not an instant" in out.stdout, (
            "and that it is a placement the stamp made, not something it watched")
        assert "all gates passed — the run resumed, and every verdict describes the tree" \
            in out.stdout, (
                "with nothing carried over, the run CAN say it passed on one tree")
        assert "every re-run reached the result its earlier attempt did (compile)" \
            in out.stdout and "changed no verdict" in out.stdout, (
                "a re-run that AGREED is worth one line of its own, or a reader "
                "cannot tell 'the move changed nothing' from 'nothing was compared'")
        assert "the move changed the outcome" not in out.stdout, (
            "this re-run answered as its attempt did; claiming a change would be "
            "the opposite false report")

    def test_a_write_inside_the_catch_up_leaves_the_split(self, scratch, git_env):
        """One pass, and the split is by LEG rather than by which collision was last.

        Interpreter calls 4 and 8: the shell gate's window (so the catch-up is
        prompted) and the checkpoint that closes the catch-up's own compile run.
        That attempt is discarded and re-run, which leaves the SHELL verdict — kept
        two legs ago — overtaken again. A split keyed on the last collision would
        call everything current here; the legs say otherwise, and the run ends with
        a warning rather than a false green.
        """
        root, wrapper, markers = scratch
        (markers / "write-on").write_text("4,8", encoding="utf-8")
        out = subprocess.run(
            ["bash", "ci/gates.sh", "compile", "shell", "two-writer"],
            cwd=str(root), capture_output=True, text=True,
            env=dict(git_env, PYTHON=str(wrapper)), timeout=300, check=False)
        assert (markers / "wrote").exists()
        assert out.returncode == 0, out.stdout
        assert "during the shell gate:" in out.stdout and "during the compile gate:" in out.stdout
        assert "caught up (re-run against the tree as it now stands): compile" in out.stdout
        assert "verdicts about the tree as it now stands: compile two-writer" in out.stdout
        assert "carried over (they describe the tree BEFORE the move): shell" in out.stdout, (
            "the shell verdict was kept before the second move, whatever that "
            "collision was called")
        assert "every gate passed, but the carried-over verdicts describe the earlier tree" \
            in out.stdout
        assert out.stdout.count("the run is catching up") == 1, (
            "one catch-up pass: a write inside it is reported, not chased")

    def test_the_catch_up_is_skipped_when_the_run_has_a_red(self, scratch, git_env):
        """A red is already a verdict; the catch-up is for completing a green.

        The scratch repo's `other.py` is broken BEFORE the run (committed), so the
        compile red is not the write's doing and survives the resume; the move then
        lands in the shell gate's window. The run says which tree the red belongs to
        and leaves it there instead of re-running a red tree.
        """
        root, wrapper, markers = scratch
        (root / "other.py").write_text("def broken(:\n", encoding="utf-8")
        _git(root, "add", "other.py", env=git_env)
        _git(root, "commit", "-q", "-m", "a pre-existing red", env=git_env)
        (markers / "write-on").write_text("4", encoding="utf-8")
        out = subprocess.run(
            ["bash", "ci/gates.sh", "compile", "shell", "two-writer"],
            cwd=str(root), capture_output=True, text=True,
            env=dict(git_env, PYTHON=str(wrapper)), timeout=300, check=False)
        assert (markers / "wrote").exists()
        assert out.returncode == 1, out.stdout
        assert "the move overtook these verdicts: compile" in out.stdout
        assert "catch-up skipped: this run has a failure to report" in out.stdout
        assert "carried over (they describe the tree BEFORE the move): compile" in out.stdout
        # The row's SHAPE is the property; the number beside it is the machine's.
        # Pinned to a literal second once, this guard reported a correct gate red
        # on a loaded full-suite run, for the same tree that passed alone.
        assert re.search(r"compile\s+FAIL\s+\d+s\s+\(before the move\)",
                         out.stdout), (
            f"the carried red is marked where it is read:\n{out.stdout}")
        assert "first failure: the compile gate" in out.stdout
        assert out.stdout.count("──────────────── compile") == 1, (
            "a skipped catch-up does not re-run the gate")

    def test_a_discarded_attempts_failure_is_not_the_runs_failure(self, scratch,
                                                                 git_env):
        """A red from a mixture must not be printed as this run's verdict.

        Call 2 both moves the tree and breaks a file, so the compile attempt is
        red AND discarded; the re-run is red on a still tree and therefore KEPT.
        The run has to distinguish the two: one is a discarded red, the other is
        the run's first failure.
        """
        root, wrapper, markers = scratch
        (markers / "break-on").write_text("2", encoding="utf-8")
        out = subprocess.run(["bash", "ci/gates.sh", "compile", "two-writer"],
                             cwd=str(root), capture_output=True, text=True,
                             env=dict(git_env, PYTHON=str(wrapper)), timeout=300,
                             check=False)
        assert (markers / "broke").exists() and (markers / "wrote").exists()
        assert out.returncode == 1, out.stdout
        assert "discarded — it had FAIL" in out.stdout, (
            f"the discarded red is reported as discarded:\n{out.stdout}")
        assert "compile    FAIL" in out.stdout, (
            "the re-run's red IS the verdict, and it is kept")
        assert "REFUSED" not in out.stdout, (
            "the re-run ran on a still tree: a failure is not a collision")
        assert "first failure: the compile gate" in out.stdout

    def test_the_run_catches_up_so_it_ends_as_a_verdict_about_one_tree(self, scratch,
                                                                      git_env):
        """The point of the feature: the overtaken verdict is re-run, not carried.

        Interpreter call 4 is the checkpoint that closes the SHELL gate's window
        (baseline, compile, its checkpoint, then this one), so the collision is
        attributed to the shell gate — and the compile verdict, judged against the
        tree the write left behind, is overtaken. The run re-runs it, in order,
        before the tree verdict, so it ends with every verdict about one tree
        instead of a report about two.
        """
        root, wrapper, markers = scratch
        (markers / "write-on").write_text("4", encoding="utf-8")
        out = subprocess.run(
            ["bash", "ci/gates.sh", "compile", "shell", "two-writer"],
            cwd=str(root), capture_output=True, text=True,
            env=dict(git_env, PYTHON=str(wrapper)), timeout=300, check=False)
        assert (markers / "wrote").exists()
        assert out.returncode == 0, out.stdout
        assert "during the shell gate:" in out.stdout
        assert "the run is catching up: re-running the verdicts the move overtook (compile)" \
            in out.stdout, f"the catch-up has to announce itself:\n{out.stdout}"
        assert "──────────────── compile (catch-up — the move overtook it)" in out.stdout, (
            "and label the attempt it is making")
        assert "(caught up after the move)" in out.stdout
        assert "superseded — the catch-up re-ran it against the tree as it now stands" \
            in out.stdout, (
                "the row the run no longer stands behind is marked as such")
        assert "caught up (re-run against the tree as it now stands): compile" in out.stdout
        assert "verdicts about the tree as it now stands: compile shell two-writer" in out.stdout
        assert "carried over: none — no verdict above describes an earlier tree" in out.stdout, (
            "after a clean catch-up there is nothing left to warn about")
        assert "all gates passed — the run resumed, and every verdict describes the tree" \
            in out.stdout
        assert out.stdout.count("──────────────── two-writer") == 1, (
            "the tree verdict runs once, after the catch-up — a second one would be "
            "a verdict about an earlier tree")
        assert out.stdout.index("(catch-up — the move overtook it)") \
            < out.stdout.index("──────────────── two-writer"), (
                "the catch-up has to finish BEFORE the tree verdict, or the verdict "
                "is about a tree the catch-up then changed")

    def test_a_re_run_that_flips_says_the_move_changed_the_outcome(self, scratch,
                                                                 git_env):
        """The comparison the resume owes: did the move MATTER, or was the answer stable?

        Interpreter call 4 closes the SHELL gate's window and breaks `other.py`,
        so the compile verdict — kept before the move, on a tree that still
        compiled — is overtaken by it and re-run against a tree that no longer
        does. Same gate, same suite, two trees, two answers: the run prints the
        flip, because that IS the evidence the move was the difference, and two
        rows in a summary can only invite a reader to diff them by eye.
        """
        root, wrapper, markers = scratch
        # ONLY the break: the fixture's default write fires on call 2, inside the
        # compile gate's own window, which stops the run on the second move before
        # any verdict can be overtaken.
        (markers / "write-on").write_text("", encoding="utf-8")
        (markers / "break-on").write_text("4", encoding="utf-8")
        out = subprocess.run(
            ["bash", "ci/gates.sh", "compile", "shell", "two-writer"],
            cwd=str(root), capture_output=True, text=True,
            env=dict(git_env, PYTHON=str(wrapper)), timeout=300, check=False)
        assert (markers / "broke").exists(), "the probe did not fire"
        assert "during the shell gate:" in out.stdout, (
            f"the break is the write, so it is attributed to the gate it landed in:\n"
            f"{out.stdout}")
        assert "the move changed the outcome: compile PASS → FAIL" in out.stdout, (
            f"a re-run that flipped is the proof the move mattered:\n{out.stdout}")
        assert "changed no verdict" not in out.stdout, (
            "one section cannot both report a flip and claim nothing changed")
        assert out.returncode == 1 and "first failure: the compile gate" in out.stdout, (
            "and the flipped verdict is the run's red, not a note beside a green")

    def test_a_write_that_is_put_back_still_resumes_the_run(self, scratch, git_env):
        """The end-to-end shape of the whole feature, at a real gate's expense.

        Interpreter call 2 is the compile gate itself, and the wrapper writes
        `app.py` and copies the original bytes back before it returns — so the
        checkpoint that closes that gate finds every hash where it left it and
        the file's write time moved. Before the stamp carried write times this run
        ended GREEN with no collision at all, on a suite that had just read the
        file mid-edit.
        """
        root, wrapper, markers = scratch
        (markers / "write-on").write_text("", encoding="utf-8")
        (markers / "revert-on").write_text("2", encoding="utf-8")
        out = subprocess.run(["bash", "ci/gates.sh", "compile", "two-writer"],
                             cwd=str(root), capture_output=True, text=True,
                             env=dict(git_env, PYTHON=str(wrapper)), timeout=300,
                             check=False)
        assert (markers / "reverted").exists(), "the probe did not fire"
        assert out.returncode == 0, (
            f"the write is absorbed by a re-run, not refused:\n{out.stdout}")
        assert "during the compile gate:" in out.stdout
        assert "written during the window and restored: app.py" in out.stdout, (
            f"the collision has to say the content was identical:\n{out.stdout}")
        assert "  - when: app.py was last written " in out.stdout, (
            "the write time is the entire evidence, so it is placed in the window")
        assert "discarded — it had PASS" in out.stdout and "re-run after the move" \
            in out.stdout, "the gate that ran underneath the write is re-run"
        assert "every re-run reached the result its earlier attempt did (compile)" \
            in out.stdout, "and the two attempts are held up against each other"
        assert "all gates passed — the run resumed" in out.stdout, out.stdout

    def test_a_reverted_write_after_the_last_checkpoint_refuses(self, scratch, git_env):
        """The other end of the same rule: no gate left to re-run.

        Interpreter call 4 is the tree verdict's own comparison, and the wrapper
        puts the file back before that snapshot is taken — the write lands after
        the last checkpoint, which is exactly the window `--since` opens, so the
        refusal has to place it there.
        """
        root, wrapper, markers = scratch
        (markers / "write-on").write_text("", encoding="utf-8")
        (markers / "revert-on").write_text("4", encoding="utf-8")
        out = subprocess.run(["bash", "ci/gates.sh", "compile", "two-writer"],
                             cwd=str(root), capture_output=True, text=True,
                             env=dict(git_env, PYTHON=str(wrapper)), timeout=300,
                             check=False)
        assert (markers / "reverted").exists(), "the probe did not fire"
        assert out.returncode == 1, out.stdout
        assert "REFUSED — the worktree changed while the gates were running" \
            in out.stdout, out.stdout
        assert "written during the window and restored: app.py" in out.stdout, (
            f"the refusal has to name the reverted write:\n{out.stdout}")
        assert "first failure: the two-writer gate" in out.stdout, (
            "a tree verdict, not a suite failure")

    def test_a_clean_resume_with_nothing_overtaken_does_not_catch_up(self, scratch,
                                                                   git_env):

        """A collision in the FIRST gate overtakes nothing, so there is no pass."""
        out = self._run(scratch, git_env, "compile", "two-writer")
        assert out.returncode == 0, out.stdout
        assert "catching up" not in out.stdout and "caught up" not in out.stdout, (
            "nothing was overtaken: a second pass would be work for nothing")

    def test_a_second_move_under_one_gate_stops_the_run(self, scratch, git_env):
        """One re-run per gate is the budget; a window with two writes has no verdict.

        Calls 2 and 5 are the compile gate and its re-run, so the tree moves twice
        inside one gate's window. Nothing can be resumed about that, so the run
        stops and says why rather than looping on a tree somebody is writing to.
        """
        root, wrapper, markers = scratch
        (markers / "write-on").write_text("2,5", encoding="utf-8")
        out = subprocess.run(["bash", "ci/gates.sh", "compile", "two-writer"],
                             cwd=str(root), capture_output=True, text=True,
                             env=dict(git_env, PYTHON=str(wrapper)), timeout=300,
                             check=False)
        assert (markers / "wrote").exists()
        assert out.returncode == 1, out.stdout
        assert "REFUSED — the worktree moved again while the compile gate was re-running" \
            in out.stdout, f"the second move is named as such:\n{out.stdout}"
        assert "a tree that keeps moving cannot be certified" in out.stdout
        assert "the remaining gates were" in out.stdout
        assert "stopped rather than report a mixture" in out.stdout, (
            "the section must not claim the run resumed when it stopped")
        assert "no verdict survived" in out.stdout, (
            "and it must not offer a split of verdicts that do not exist")
        assert "──────────────── two-writer" not in out.stdout, (
            "the run stops: a gate that never ran cannot be reported")
        assert "every re-run reached the result its earlier attempt did (compile)" \
            in out.stdout, (
                "both attempts were discarded, and holding their two results against "
                "each other is still the only evidence about whether the writing "
                "changed anything")

    def test_a_still_run_says_unchanged(self, scratch, git_env):
        """The same scratch repo without a write: no resume, no VOID, no caveat."""
        root, _, _ = scratch
        env = dict(git_env, PYTHON=sys.executable)
        out = subprocess.run(["bash", "ci/gates.sh", "compile", "two-writer"],
                             cwd=str(root), capture_output=True, text=True,
                             env=env, timeout=300, check=False)
        assert out.returncode == 0, out.stdout
        assert "worktree unchanged since the run started" in out.stdout
        assert "all gates passed" in out.stdout
        assert "resumed" not in out.stdout and "VOID" not in out.stdout, (
            "a run with nothing to report must not manufacture a section about it")


class TestTheVerdictComparison:
    """Whether the move CHANGED an answer — the shipped comparison, on fabrications.

    Neither the rows nor the stamp can say this: the summary's rows look like
    different gates, and the stamp only knows the tree moved, never whether that
    mattered. So the real `compare_attempts` is extracted from `ci/gates.sh` and
    run over a fabricated record of attempts, because the shapes a real run can
    reach are only two (a re-run agrees, or it flips one way), while what the
    function has to do with three attempts — collapse the repeats, keep every
    change — is decided by the same code and cannot be built end to end: a write
    inside one window and its undo are invisible to an endpoint comparison.
    """

    @staticmethod
    def _compare(gates_source: str, attempts: str, wanted: str) -> dict:
        """The real function, on a fabricated `ATTEMPTS` record, in its own shell."""
        body = _shell_function(gates_source, "compare_attempts")
        assert body, "compare_attempts is not defined in ci/gates.sh"
        script = ("set -u\n"
                  f"WANTED=({wanted})\n"
                  f"ATTEMPTS='{attempts}'\n"
                  "CHANGED=''\n"
                  "RERAN=''\n"
                  f"{body}\n"
                  "compare_attempts\n"
                  'printf "CHANGED=%s\\nRERAN=%s\\n" "$CHANGED" "$RERAN"\n')
        proc = subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=60)
        assert proc.returncode == 0, f"the comparison failed to run: {proc.stderr}"
        out = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines())
        assert set(out) == {"CHANGED", "RERAN"}, proc.stdout
        return out

    def test_a_re_run_that_agreed_is_named_as_no_change(self, gates_source):
        got = self._compare(gates_source, "compile|PASS compile|PASS", "compile")
        assert got == {"CHANGED": "", "RERAN": "compile"}, got

    def test_a_re_run_that_flipped_is_the_move_changing_the_outcome(self, gates_source):
        got = self._compare(gates_source, "compile|PASS compile|FAIL", "compile")
        assert got["CHANGED"] == "compile PASS → FAIL", got
        assert got["RERAN"] == "compile", got

    def test_an_attempt_discarded_as_a_mixture_is_compared_by_its_own_result(
            self, gates_source):
        """A discarded attempt is not a verdict, and it is half the comparison.

        The record carries the attempt's own result — the row says VOID, because
        the row is what the run stands behind — so a mixture's red giving way to a
        green on the tree the collision left reads as the flip it is.
        """
        got = self._compare(gates_source, "compile|FAIL compile|PASS", "compile")
        assert got["CHANGED"] == "compile FAIL → PASS", got

    def test_every_answer_after_a_change_is_kept_not_only_the_last(
            self, gates_source):
        """`PASS → FAIL → PASS` is three answers, and the middle one is the finding.

        Collapsing repeats is not the same as keeping only the endpoints: a
        first-against-last reading erases a flip a re-run then undid, which is
        exactly the shape that says the same gate gave two answers on two trees.
        """
        got = self._compare(gates_source,
                            "compile|PASS compile|FAIL compile|PASS", "compile")
        assert got["CHANGED"] == "compile PASS → FAIL → PASS", got
        assert got["RERAN"] == "compile", got

    def test_repeats_are_not_printed_as_repeated_answers(self, gates_source):
        got = self._compare(gates_source, "compile|FAIL compile|FAIL compile|FAIL",
                            "compile")
        assert got == {"CHANGED": "", "RERAN": "compile"}, got

    def test_a_gate_that_ran_once_is_never_compared(self, gates_source):
        """Only a re-run produces two results to hold up against each other."""
        got = self._compare(gates_source, "compile|PASS shell|FAIL", "compile shell")
        assert got == {"CHANGED": "", "RERAN": ""}, got

    def test_an_attempt_in_another_gate_is_not_this_gates_comparison(
            self, gates_source):
        """The record is flat and in run order; the match has to be on the name."""
        got = self._compare(gates_source,
                            "tests|FAIL compile|PASS shell|FAIL compile|PASS",
                            "compile shell")
        assert got == {"CHANGED": "", "RERAN": "compile"}, (
            "a gate is compared with itself, never with the neighbours its attempts "
            f"are interleaved with — `tests` ran once, and its red is nobody else's: "
            f"{got}")

    def test_two_gates_that_both_flipped_are_both_named_in_run_order(self, gates_source):
        got = self._compare(gates_source,
                            "compile|PASS compile|FAIL shell|FAIL shell|PASS",
                            "compile shell")
        assert got["CHANGED"] == "compile PASS → FAIL, shell FAIL → PASS", got
        assert got["RERAN"] == "compile shell", got


class TestTheGateWiring:
    """That ci/gates.sh runs that refusal, at the right moment, once."""

    def test_the_gate_is_registered_last(self, gates_source):
        line = next(l for l in gates_source.splitlines() if l.startswith("ALL_GATES="))
        gates = line.split('"')[1].split()
        assert "two-writer" in gates, "the gate is not registered at all"
        assert gates[-1] == "two-writer", (
            "it compares the tree AFTER the other gates; anywhere else it would "
            "judge a window that excludes them")

    def test_the_gate_name_maps_to_its_function(self, gates_source):
        """`two-writer` has to reach `gate_two_writer`.

        Found by running it: `"gate_$name"` looked for `gate_two-writer` and the
        first live run of the gate reported `command not found` — a FAIL for the
        wrong reason, which is exactly what this gate exists to prevent.
        """
        assert '"gate_${name//-/_}"' in gates_source, (
            "a hyphenated gate name cannot be spelled as a bash function")

    def test_the_baseline_is_taken_before_any_gate_runs(self, gates_source):
        save = gates_source.index('--checkpoint "$STAMP_DIR"')
        first_run = gates_source.index('run_gate "$g"')
        assert save < first_run, (
            "a baseline taken after the first gate cannot see what that gate did")

    def test_every_gate_is_checkpointed_under_its_own_name(self, gates_source):
        body = _shell_function(gates_source, "run_gate")
        assert 'checkpoint_gate "$name"' in body, (
            "without a checkpoint per gate the refusal cannot say which one it was")
        helper = _shell_function(gates_source, "checkpoint_gate")
        assert '"$1" = two-writer' in helper, (
            "two-writer's own comparison is the last word; checkpointing it after "
            "the fact adds nothing")
        assert '--label "$1"' in helper, "the label IS the attribution"

    def test_a_collision_is_not_a_refusal_on_its_own(self, gates_source):
        """The property this feature IS: moves.txt is a report, not a verdict.

        Refusing on it is what threw the whole run away; the collided gate is
        re-run against the tree as it now stands, and the gate's own comparison —
        against the baseline, which after a resume IS that tree — decides.
        """
        body = _shell_function(gates_source, "gate_two_writer")
        assert body, "gate_two_writer is not defined in ci/gates.sh"
        assert "first.json" in body and "--compare" in body, (
            "the end comparison is against the baseline the run is judging")
        assert "moves.txt" not in body and "REFUSED" not in body, (
            "a collision is reported by report_resume; refusing on it here would "
            "throw away the run the resume just saved")

    def test_the_tree_verdict_opens_its_window_at_the_last_checkpoint(self, gates_source):
        """The write the final comparison catches is the one no gate saw.

        So the clock's window is the tail of the run — the window since the last
        checkpoint — while the VERDICT stays the baseline's, which is the tree a
        resumed run is judged against.
        """
        body = _shell_function(gates_source, "gate_two_writer")
        assert "--since" in body and "latest.json" in body, (
            "a post-checkpoint write has to be placed against the checkpoint, not "
            "against the whole run")
        assert "--compare" in body and "first.json" in body, (
            "and the verdict is still the baseline's")

    def test_the_resumed_section_explains_the_when_lines(self, gates_source):
        """A placement reads as an observation unless it says it is a placement."""
        body = _shell_function(gates_source, "report_resume")
        assert body, "report_resume is not defined in ci/gates.sh"
        assert "when:" in body and "moves.txt" in body, (
            "the resumed section prints the log, so it has to say what the when: "
            "lines are")

    def test_the_resumed_run_reports_the_split(self, gates_source):
        body = _shell_function(gates_source, "report_resume")
        assert body, "report_resume is not defined in ci/gates.sh"
        assert "moves.txt" in body and "resumed" in body, (
            "the section has to say what moved, and that the run resumed")
        assert "no verdict survived" in body and "stopped rather than report a mixture" \
            in body, (
                "a run that STOPPED must not describe itself as having resumed")
        split = _shell_function(gates_source, "split_verdicts")
        assert 'gate_leg "$g"' in split and '"$leg" = "$LEG"' in split, (
            "the split is by LEG: which collision was last says nothing about a "
            "verdict the catch-up has since re-run")
        leg = _shell_function(gates_source, "gate_leg")
        assert '!="$1"' not in leg and 'VOID' in leg and 'found="$leg"' in leg, (
            "a gate's leg comes from its LAST KEPT row: a discarded attempt is not "
            "a verdict, and an earlier row is superseded by a later one")

    def test_the_attempt_record_is_written_before_keep_or_discard(self, gates_source):
        """The record is what the comparison reads, so a discarded attempt is in it.

        Recorded inside the KEPT branch only, this would compare a re-run with
        itself: a discarded attempt is not a verdict, but its result is half of
        the answer to whether the move changed anything.
        """
        body = _shell_function(gates_source, "run_gate")
        assert body, "run_gate is not defined in ci/gates.sh"
        record = 'ATTEMPTS="$ATTEMPTS $name|$status"'
        assert record in body, "a gate's attempts are not recorded at all"
        assert body.index(record) < body.index('[ "$checkpoint" != 1 ]'), (
            "the record has to be taken before the checkpoint decides whether this "
            "attempt is kept or discarded")
        line = next(l for l in body.splitlines() if record in l)
        assert "VOID" not in line, (
            "the record holds the attempt's own result; the row is where VOID lives")

    def test_the_comparison_is_reported_in_the_resumed_section(self, gates_source):
        body = _shell_function(gates_source, "report_resume")
        assert body, "report_resume is not defined in ci/gates.sh"
        assert "compare_attempts" in body, (
            "the resumed section is where a reader learns what the move changed")
        assert "the move changed the outcome:" in body, (
            "a difference between an attempt and its re-run IS the finding, so it "
            "gets named")
        assert "changed no verdict" in body, (
            "and the agreeing case gets a line too, or silence reads as 'not "
            "compared'")
        assert 'elif [ -n "$RERAN" ]; then' in body, (
            "the agreeing line is behind the re-run list on purpose: an `else` "
            "would print it — with an empty list — for a resumed run that compared "
            "nothing. Held as a SHAPE, because every reachable resume has at least "
            "one re-run: the comparison is reached only when a gate was re-run")
        helper = _shell_function(gates_source, "compare_attempts")
        assert 'RERAN="$RERAN $g"' in helper and "distinct" in helper, (
            "a gate is named as re-run whatever it answered, and its ANSWERS are "
            "what decide whether anything changed")
        assert "VOID" not in helper, (
            "the comparison reads results; a discarded attempt's result counts")

    def test_the_catch_up_re_runs_only_what_the_move_overtook(self, gates_source):
        """Exactly the carried gates, in order, once — and only when there are any."""
        body = _shell_function(gates_source, "catch_up")
        assert body, "catch_up is not defined in ci/gates.sh"
        assert '[ -n "$CARRIED" ] || return 0' in body, (
            "a run with nothing overtaken must not start a pass")
        assert 'for g in $CARRIED' in body, (
            "the pass re-runs the carried gates, not the whole list")
        assert 'run_gate "$g" catch-up' in body, (
            "and it runs them through the same per-gate machinery")
        assert 'supersede "$g"' in body, (
            "the row it overtakes is marked, or the summary shows two verdicts")
        assert "while" not in body, "one pass: a moving tree is reported, not chased"

    def test_the_catch_up_is_skipped_when_the_run_has_a_red(self, gates_source):
        body = _shell_function(gates_source, "catch_up")
        assert body.index('"$FAILED" = 1') < body.index('for g in $CARRIED'), (
            "the red check has to come BEFORE the re-runs: the failure bookkeeping "
            "(which junit report the digest prints) is built for one tree's red")

    def test_the_tree_gate_runs_after_the_catch_up(self, gates_source):
        assert gates_source.index('[ "$STOP" = 1 ] || catch_up') \
            < gates_source.index('run_gate "$TREE_GATE"'), (
            "the tree verdict compares against the baseline, and a gate running "
            "after it would invalidate the comparison")
        assert '[ "$g" = two-writer ]' in gates_source and "TREE_GATE=" in gates_source, (
            "two-writer is held back out of the body loop rather than run in place")

    def test_a_carried_verdict_is_marked_where_it_is_read(self, gates_source):
        """A bare row in a summary invites being read as current."""
        tail = gates_source[gates_source.index("============ summary"):]
        assert 'note="before the move"' in tail and '[ "$leg" != "$LEG" ]' in tail, (
            "a verdict from an earlier leg is labelled in the summary itself")

    @staticmethod
    def _pinned_elapsed_lines(text: str) -> list:
        """Lines whose literal pins a gate's elapsed seconds. Per line, because
        a literal is a line: a greedy scan swallows the newlines between a quote
        and a number that belongs to the line after it."""
        return [line for line in text.splitlines()
                if re.search(r'"[^"]*(?:PASS|FAIL|VOID) \d+s', line)]

    def test_no_guard_pins_a_gates_elapsed_seconds(self):
        """A gate's seconds are a measurement, not a property.

        Found by a full-suite run of a correct tree: the carried-red guard
        asserted the summary row down to the second, the same tree printed a
        different number under load, and the gate was reported red for it. The
        row's SHAPE is the property; the number beside it belongs to the machine.
        """
        # Both directions, so the check cannot be vacuous: it has to SEE a pinned
        # second when there is one. The sample is built by concatenation so this
        # file does not itself contain the literal it forbids.
        sample = 'x = "compile    FAIL ' + "0s" + '   (before the move)"'
        assert self._pinned_elapsed_lines(sample), (
            "the check cannot see the shape it forbids")
        source = Path(__file__).read_text(encoding="utf-8")
        found = self._pinned_elapsed_lines(source)
        assert not found, (
            f"these guards pin a gate's elapsed time: {found}")

    def test_the_collided_gate_is_re_run_on_a_moved_baseline(self, gates_source):
        body = _shell_function(gates_source, "run_gate")
        assert 'checkpoint_gate "$name"' in body, (
            "the checkpoint is what sees the collision")
        assert 'rebase_stamp "$name"' in body and "attempt=2" in body, (
            "the collision re-runs the gate, against a baseline moved onto the "
            "tree the collision left")
        assert '--rebase' in _shell_function(gates_source, "rebase_stamp"), (
            "the baseline moves through the stamp, not by copying files here")

    def test_the_checkpoint_status_is_not_swallowed(self, gates_source):
        """A `return 0` after the stamp reports every collision as unchanged.

        That is a silent off switch for the whole feature, so the helper's LAST
        command is what gets asserted — the status has to BE the return value.
        """
        helper = _shell_function(gates_source, "checkpoint_gate")
        assert helper, "checkpoint_gate is not defined in ci/gates.sh"
        lines = [line for line in helper.splitlines()[1:]
                 if line.strip() and line.strip() != "}"]
        # A wrapped command is ONE statement, so join the continuations before
        # asking which statement the helper ends with.
        statements = []
        for line in lines:
            if statements and statements[-1].rstrip().endswith("\\"):
                statements[-1] = statements[-1].rstrip()[:-1] + " " + line.strip()
            else:
                statements.append(line)
        assert "worktree_stamp.py" in statements[-1], (
            f"the stamp call has to be the last thing in the helper: "
            f"{statements[-1]!r}")
        assert "|| true" not in helper, (
            "`|| true` masks the status just as completely as a return would")

    def test_a_discarded_attempt_cannot_become_the_runs_failure(self, gates_source):
        """The failure bookkeeping belongs to the KEPT attempt, not the attempt.

        FIRST_FAILED picks the junit report the digest prints, so a discarded
        attempt setting it would explain the run's red with a tree the run is not
        judging — the same defect as the stale report this digest already had.
        """
        attempt = _shell_function(gates_source, "attempt_gate")
        assert attempt, "attempt_gate is not defined in ci/gates.sh"
        assert "FIRST_FAILED" not in attempt and "stash_report" not in attempt, (
            "a discarded attempt collects a verdict and nothing else")
        assert "stash_report" in _shell_function(gates_source, "run_gate"), (
            "and it still happens where the attempt is kept, or a real failure "
            "loses its digest")

    def test_the_stop_message_is_written_in_whole_lines(self, gates_source):
        """Found live in the first refusal: a backslash-newline inside SINGLE
        quotes is a LITERAL backslash, so the message printed
        `...was re-running, so there is\\`. No assertion about words saw it."""
        body = _shell_function(gates_source, "refuse_moving")
        assert body, "refuse_moving is not defined in ci/gates.sh"
        assert "moved again" in body and "cannot be certified" in body, (
            "the stop has to say what happened in the reader's terms")
        for line in body.splitlines():
            assert not line.rstrip().endswith("\\"), (
                f"a continued line inside single quotes prints its backslash: {line!r}")
        assert "STOP=1" in body and "FAILED=1" in body, (
            "the run stops, and the summary knows it did not pass")

    def test_the_outer_loop_honours_the_stop(self, gates_source):
        assert '[ "$STOP" = 1 ] && break' in gates_source, (
            "a stopped run must not start the next gate")

    def test_the_checkpoints_live_outside_the_worktree(self, gates_source):
        line = next(l for l in gates_source.splitlines() if "STAMP_DIR=$(mktemp" in l)
        assert "mktemp -d" in line and "ROOT" not in line, (
            "snapshots written inside the tree they fingerprint move the tree")
        cleanup = _shell_function(gates_source, "drop_stamp")
        assert "rm -rf" in cleanup, "the whole checkpoint directory goes, not one file"

    def test_the_gate_compares_rather_than_restamping(self, gates_source):
        body = _shell_function(gates_source, "gate_two_writer")
        assert body, "gate_two_writer is not defined in ci/gates.sh"
        assert "--compare" in body, "the gate must compare against the baseline"
        assert "--save" not in body, (
            "re-stamping would always find the tree matching itself")

    def test_the_refusal_is_cleaned_up_on_every_exit(self, gates_source):
        assert gates_source.count("drop_stamp") >= 3, (
            "the temp baseline has to be removed on the failure exit and on the "
            "passing one")

    def test_a_previous_runs_report_cannot_be_the_digest(self, gates_source):
        """The digest reads the FIRST failing junit report; a leftover copy from
        an earlier session explained a red from a different tree (found live:
        the digest said "1 failed of 1617" while this run collected 1676)."""
        prune = gates_source.index('rm -f "$JUNIT" "$FIRST_FAILURE"')
        assert prune < gates_source.index('run_gate "$g"'), (
            "stale reports have to be dropped before the gates write their own")

    def test_the_suite_digest_is_not_printed_for_a_refusal(self, gates_source):
        tail = gates_source[gates_source.index('if [ "$FAILED" = 1 ]'):]
        assert "tests|order|coverage" in tail, (
            "the junit digest belongs to the suite gates; under a two-writer "
            "refusal it would report a gate that passed")
        assert "its own message is above" in tail, (
            "the refusal has to point at its own message")

    def test_the_stamp_script_is_not_shell_checked(self, gates_source):
        """`bash -n` on a Python file would be a false failure in the shell gate."""
        assert STAMP.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")
        shell_case = next(l for l in gates_source.splitlines()
                          if l.strip().startswith("'#!'*bash*"))
        assert "'/sh'*" in shell_case and "python" not in shell_case, (
            "the shell gate discovers scripts by shebang; a pattern that matched "
            "python would run `bash -n` over the stamp script")
