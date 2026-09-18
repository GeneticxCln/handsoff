"""The clean-checkout gate: does a fresh checkout of HEAD agree with itself?

The suite runs in a SHARED checkout, where the freshness guard can see files that
are not committed at all — so the one tree nobody ever looks at is the one a
reader gets, `git clone`. Measured live, and the reason this gate exists: commit
`bd9cf5a` was green here and had THREE red guards in a clean checkout (the
settings-key census counted 64 keys while the committed schema had 59, the test
plan named two test files the commit did not contain, and a dated audit heading —
`## core/lifecycle.py seam exists but has no production caller (2026-09-17)` — was
read as a module's size). Every gate in `ci/gates.sh` passed over all three,
because each reads the working tree.

`clean-checkout` lays HEAD down with `git worktree add --detach` in a scratch
directory OUTSIDE the tree and runs the freshness guard there. Three properties
are worth pinning, and only one of them is arithmetic:

  * the COPY is HEAD, not this checkout — a worktree of the commit, so the gate
    answers a question about what was committed rather than about what happens to
    sit on disk;
  * the cleanup happens on EVERY path, the refusal included, because a leftover
    worktree pollutes `git worktree list` for the next run and for the developer,
    and a refusal is worth nothing if reporting it damages the checkout it
    reports about;
  * the VERDICT is the guard's, not a paraphrase: the refusal says what failed and
    how to reproduce it, and a repo with nothing to check is SKIPPED (2) rather
    than counted as a pass.

The end-to-end class runs the real `ci/gates.sh` in a scratch repo built around a
stand-in freshness guard, in the exact shape of the live failure: the guard is
COMMITTED and the file it asks about is not, so the working tree passes and the
clean checkout does not. That is the property, and it cannot be shown any other
way — a test that only ran the gate on this repo would prove the happy path and
nothing about the gap.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import GIT_PLUMBING, HERE, sandbox_env, shell_function

GATES = HERE / "ci" / "gates.sh"
GUARD = "tests/test_specs_freshness.py"
# What the stub guard asks about: a file whose presence in the plan is the whole
# question. Named for the property, not for the repo.
PROBED = "tests/test_plan_extra.py"

STUB = '''"""A stand-in for the freshness guard: it refuses when a named file is absent.

Built this way on purpose. A committed guard whose subject is an UNCOMMITTED file
passes in the working tree and fails in a clean checkout — which is the shape of
the live failure this gate was written for, and the only shape that tells the
gate apart from the suite that already ran here.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_the_plan_and_the_tree_agree():
    assert (HERE / "tests" / "test_plan_extra.py").exists(), (
        "the plan names a file this checkout does not contain")
'''


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


def _git(repo: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Clean Checkout",
         "-c", "user.email=clean@example.invalid", "-c", "commit.gpgsign=false",
         *args],
        capture_output=True, text=True, env=env, timeout=60, check=False)


def _make_scratch(root: Path, env: dict) -> Path:
    """A repo whose HEAD carries the guard but NOT the file the guard asks about.

    `extra` lands on disk (untracked) so the working tree passes: that is the
    live shape, and the fixture hands both trees to the test.

    A function rather than the fixture's body because one guard needs a repo built
    with an environment of its OWN: the module-scoped `git_env` is constructed
    before any test can plant anything, and the shape worth pinning is exactly the
    one where the ambient environment is poisoned (a pre-commit hook exports
    git's plumbing, and the suite is run by that hook).
    """
    (root / "ci").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copy2(GATES, root / "ci" / "gates.sh")
    (root / "tests" / "test_specs_freshness.py").write_text(STUB, encoding="utf-8")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / PROBED).write_text("def test_extra():\n    assert True\n",
                               encoding="utf-8")
    _git(root, "init", "-q", env=env)
    # The guard is committed; the file it asks about is NOT. `git add` of the
    # whole directory would commit both and the fixture would prove nothing.
    _git(root, "add", "ci/gates.sh", GUARD, "app.py", env=env)
    _git(root, "commit", "-q", "-m", "a guard for a file this commit lacks",
         env=env)
    return root


@pytest.fixture
def scratch(tmp_path: Path, git_env: dict) -> Path:
    return _make_scratch(tmp_path / "scratch", git_env)


def _run_gate(root: Path, git_env: dict, *gates: str):
    return subprocess.run(
        ["bash", "ci/gates.sh", *gates], cwd=str(root), capture_output=True,
        text=True, env=git_env, timeout=300, check=False)


class TestTheCleanCheckoutGate:
    """The wiring: what it copies, where it puts it, and what it does afterwards."""

    def test_it_is_registered_before_the_tree_verdict(self, gates_source):
        line = next(l for l in gates_source.splitlines() if l.startswith("ALL_GATES="))
        gates = line.split('"')[1].split()
        assert "clean-checkout" in gates, "the gate is not registered at all"
        assert gates[-1] == "two-writer", (
            "the tree verdict stays last: it compares the worktree, and a gate "
            "running after it would invalidate that comparison")
        assert gates.index("clean-checkout") == len(gates) - 2, (
            "it belongs beside the tree verdict — the two gates about a tree "
            "rather than about code")

    def test_the_name_reaches_a_function(self, gates_source):
        """`clean-checkout` has to become `gate_clean_checkout`."""
        assert "gate_clean_checkout() {" in gates_source, (
            "the run maps a gate name to `gate_${name//-/_}`, so a hyphenated "
            "name needs the underscore spelling to exist")
        assert 'gate_${name//-/_}' in gates_source

    def test_it_checks_out_head_rather_than_copying_the_worktree(self, gates_source):
        body = shell_function(gates_source, "gate_clean_checkout")
        assert body, "gate_clean_checkout is not defined in ci/gates.sh"
        # The exact invocation, not the words: the refusal prints a reproduce hint
        # that itself reads `git worktree add --detach /tmp/x HEAD`, so a substring
        # check here was satisfied by prose while the command below it could be
        # anything. Found by the mutation sweep, not by reading it.
        assert 'worktree add --detach --quiet "$tree" HEAD' in body, (
            "the tree a reader gets is the COMMIT; a copy of the working tree "
            "would answer the question the suite already answered")
        assert "cp -" not in body and "rsync" not in body and "tar " not in body, (
            "a copy of this checkout would carry the same uncommitted files the "
            "gate exists to discount")

    def test_the_scratch_tree_lives_outside_the_worktree(self, gates_source):
        body = shell_function(gates_source, "gate_clean_checkout")
        assert "mktemp -d" in body and '"$dir"' in body, (
            "the checkout has to be made somewhere disposable")
        assert "mktemp -d -t handsoff-clean" in body, (
            "and outside the tree: a scratch copy written INSIDE the worktree "
            "moves the tree the stamp is watching")
        assert "tree=\"$dir/tree\"" in body, (
            "the worktree goes under that directory, not beside the repo")

    def test_it_cleans_up_before_it_reports(self, gates_source):
        """A refusal that leaves a worktree behind damages the repo it describes."""
        body = shell_function(gates_source, "gate_clean_checkout")
        cleanup = body.index("worktree remove")
        assert "worktree prune" in body, (
            "removing the directory is not enough: the registration is what "
            "`git worktree list` shows")
        refused = body.index("REFUSED")
        assert cleanup < refused, (
            "the cleanup has to happen before the verdict is printed — every "
            "path out of this function, the refusal included")
        assert body.count("return ") >= 4, (
            "each early exit is a path that must not skip the cleanup")

    def test_it_skips_when_it_cannot_lay_head_down(self, gates_source):
        """A gate that cannot run says so, and is never counted as a pass."""
        body = shell_function(gates_source, "gate_clean_checkout")
        assert "not a git worktree" in body, "no worktree: no opinion"
        assert "HEAD has no commit yet" in body, "an unborn HEAD has no tree"
        assert 'HEAD carries no ' in body, (
            "a repo without the freshness guard has nothing for this gate to run")
        assert "worktree add failed" in body, (
            "and a git that cannot make the worktree is a SKIP with a reason, "
            "not a pass")
        assert body.count("return 2") >= 4, (
            "every one of those is a SKIP, spelled as one")

    def test_the_refusal_says_what_failed_and_how_to_reproduce_it(self, gates_source):
        body = shell_function(gates_source, "gate_clean_checkout")
        assert "tail -n 25" in body and "REFUSED" in body, (
            "the guard's own output is the finding; a paraphrase would hide the "
            "assertion that failed")
        assert "Reproduce with" in body and GUARD in body, (
            "the reader has to be able to see it for themselves")
        assert "uncommitted" in body, (
            "and has to be told why this checkout did not show it")


class TestTheHarnessEnvironment:
    """The environment these tests build their repositories in.

    Not about the gate: the harness runs UNDER a pre-commit hook, git exports its
    own plumbing to that hook, and a scratch repository built with the inherited
    environment reads the caller's index. Found the hard way — the three
    end-to-end tests above passed alone and failed only while a commit was in
    progress — so the fix has one home and a guard in each direction.
    """

    def test_the_child_environment_drops_the_caller_s_git_plumbing(self, monkeypatch):
        for name in GIT_PLUMBING:
            monkeypatch.setenv(name, "planted")
        env = sandbox_env()
        left = sorted(name for name in GIT_PLUMBING if name in env)
        assert not left, (
            f"the harness hands git's plumbing to its children: {left} — a "
            f"scratch repository then reads the caller's index, which is how the "
            f"end-to-end gate tests failed under the pre-commit hook and nowhere "
            f"else")
        assert env.get("PATH"), (
            "and the guard must not be vacuous: the child still needs its PATH")

    # Git names its own exec path so a hook can find git's programs. Harmless and
    # deliberately kept: it says where git is, not which repository.
    HARMLESS = ("GIT_EXEC_PATH",)

    def test_the_dropped_list_covers_what_a_hook_is_really_handed(self):
        """Read from a real hook rather than from this file's opinion.

        The list above is only right if it covers what git really passes, and the
        only authority on that is git: a probe hook dumps its own environment, and
        `GIT_INDEX_FILE` — the one that broke the fixture — has to be in it and in
        the list.
        """
        hooks = Path(tempfile.mkdtemp(prefix="handsoff-hookprobe-"))
        dump = hooks / "exported.txt"
        hook = hooks / "pre-commit"
        hook.write_text("#!/usr/bin/env bash\nenv | grep -E '^GIT_' > "
                        f"{dump!s}\n", encoding="utf-8")
        hook.chmod(0o755)
        repo = Path(tempfile.mkdtemp(prefix="handsoff-hookprobe-repo-"))
        env = sandbox_env()
        _git(repo, "init", "-q", env=env)
        subprocess.run(
            ["git", "-C", str(repo), "-c", f"core.hooksPath={hooks}",
             "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
             "commit", "--allow-empty", "-q", "-m", "probe"],
            capture_output=True, text=True, env=env, timeout=60, check=False)
        assert dump.exists(), (
            "the probe hook did not run, so this guard proves nothing")
        exported = {line.split("=", 1)[0] for line in
                    dump.read_text(encoding="utf-8").splitlines() if "=" in line}
        assert "GIT_INDEX_FILE" in exported, (
            f"the probe hook was handed {sorted(exported)} — if the index is not "
            f"among them, git has stopped exporting what broke the fixture and "
            f"this guard needs re-reading, not deleting")
        missed = sorted(name for name in exported
                        if name not in GIT_PLUMBING + self.HARMLESS)
        assert not missed, (
            f"a real hook is handed {missed}, which the harness passes on to a "
            f"child that builds its own repository")


class TestTheCleanCheckoutEndToEnd:
    """The real script, in the exact shape of the live failure."""

    def test_the_worktree_passes_and_the_clean_checkout_still_refuses(self, scratch,
                                                                    git_env):
        """The property, in one test: uncommitted files do not count.

        `tests/test_plan_extra.py` is on disk and untracked, so the guard passes
        HERE and fails in the checkout of HEAD — which is why every gate above
        this one, all of them reading the working tree, passed over `bd9cf5a`.
        """
        root = scratch
        here = subprocess.run(["python3", "-m", "pytest", GUARD, "-q"],
                              cwd=str(root), capture_output=True, text=True,
                              env=git_env, timeout=120, check=False)
        assert here.returncode == 0, (
            f"the fixture must pass in the WORKING TREE, or it proves nothing:\n"
            f"{here.stdout}{here.stderr}")
        out = _run_gate(root, git_env, "clean-checkout")
        assert out.returncode == 1, (
            f"HEAD lacks the file its own guard asks about:\n{out.stdout}")
        assert "REFUSED" in out.stdout and "clean-checkout FAIL" in out.stdout
        assert "test_the_plan_and_the_tree_agree" in out.stdout, (
            "the failure digest names the guard that failed, from the guard")

    def test_the_gate_works_under_a_hook_that_exports_git_plumbing(self, tmp_path,
                                                                  monkeypatch):
        """The shape that found all of this: the suite runs UNDER pre-commit.

        Git hands that hook `GIT_INDEX_FILE=.git/index` plus an author identity,
        and the module-scoped `git_env` cannot show it — it is built before any
        test can plant anything. So the environment here is built AFTER the plant,
        exactly as the hook's child would see it, and the gate has to reach its
        verdict anyway: the scratch repository is its own, and nothing about the
        caller's index may reach into it.
        """
        for name, value in (("GIT_INDEX_FILE", ".git/index"),
                            ("GIT_PREFIX", ""),
                            ("GIT_AUTHOR_DATE", "@1789727041 +0200"),
                            ("GIT_COMMITTER_NAME", "pre-commit")):
            monkeypatch.setenv(name, value)
        env = sandbox_env()
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        root = _make_scratch(tmp_path / "leaked", env)
        out = _run_gate(root, env, "clean-checkout")
        assert "clean-checkout FAIL" in out.stdout, (
            f"the fixture's HEAD must still be refused, or this proves nothing "
            f"about the leak:\n{out.stdout}{out.stderr}")
        assert "could not lay" not in out.stdout, (
            f"the gate could not lay HEAD down: the caller's git plumbing reached "
            f"the scratch repository — this is the pre-commit failure, in one "
            f"test:\n{out.stdout}{out.stderr}")

    def test_a_head_that_agrees_with_itself_passes(self, scratch, git_env):
        root = scratch
        assert _git(root, "add", PROBED, env=git_env).returncode == 0
        assert _git(root, "commit", "-q", "-m", "the file the guard asks about",
                    env=git_env).returncode == 0
        out = _run_gate(root, git_env, "clean-checkout")
        assert out.returncode == 0, (
            f"with the file committed, HEAD agrees with itself:\n{out.stdout}")
        assert "clean-checkout PASS" in out.stdout and "all gates passed" in out.stdout

    def test_it_leaves_no_worktree_behind(self, scratch, git_env):
        """Both verdicts, and the scratch checkout is gone either way."""
        root = scratch
        _run_gate(root, git_env, "clean-checkout")
        first = _git(root, "worktree", "list", env=git_env).stdout.splitlines()
        assert len(first) == 1, (
            f"a refused run left a worktree registered:\n{first}")
        _git(root, "add", PROBED, env=git_env)
        _git(root, "commit", "-q", "-m", "consistent", env=git_env)
        _run_gate(root, git_env, "clean-checkout")
        second = _git(root, "worktree", "list", env=git_env).stdout.splitlines()
        assert len(second) == 1, (
            f"a passing run left a worktree registered:\n{second}")

    def test_a_repo_with_no_such_guard_is_skipped_not_passed(self, scratch, git_env):
        """A gate that never ran must not read as a pass."""
        root = scratch
        assert _git(root, "rm", "-q", "--cached", GUARD, env=git_env).returncode == 0
        (root / GUARD).unlink()
        assert _git(root, "commit", "-q", "-m", "no guard in HEAD",
                    env=git_env).returncode == 0
        out = _run_gate(root, git_env, "clean-checkout")
        assert out.returncode == 0, (
            f"a SKIP is not a failure, and not a pass either:\n{out.stdout}")
        assert "clean-checkout SKIP" in out.stdout, out.stdout
        assert "nothing to check" in out.stdout, (
            "the reason has to be visible, and it has to say what was missing")
