"""The pre-commit hook judges the STAGED tree, not the working tree.

Measured rather than theorised: two commits on `main` (`bd9cf5a`, `baf58c5`) each
passed this hook and were refused by a clean checkout of themselves. The second is
the whole shape in one line — the module whose size the architecture table prices
was staged, and the regenerated table was not, so the WORKING tree held both
halves and agreed with itself while the COMMIT held one and not the other. Every
check the hook ran read a file on disk, which is a different tree from the one the
commit would contain.

So the hook writes the index out (`git checkout-index -a --prefix=…`) and the
file-shaped checks read THAT: the byte-compile leg, the shebang/bash-n leg, and a
new spec-vs-tree leg. The end-to-end class below builds a scratch repo around a
stand-in freshness guard whose subject is a plan file, because the property under
test is not the guard — it is WHICH TREE the guard is asked about:

  * a partial stage is refused (the shape that shipped);
  * staging both halves is committable;
  * the working tree does NOT decide — a commit whose staged tree is consistent
    goes through even when the working copy beside it is not.

The full suite still runs against the working tree, and that is a stated limit
rather than an oversight: a file-only copy has no `.git`, parts of the suite
legitimately read this repository, and a hook that refuses honest commits is worse
than one that misses. That question is asked of the staged tree by the guard leg.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import HERE, sandbox_env

HOOK = HERE / "githooks" / "pre-commit"
# What the stand-in guard asks about: a plan entry, so the smoke is a file the
# hook materialises rather than a real module the generator would have to price.
PLAN = "PLAN.md"
INDEX = "INDEX.txt"

STUB = '''"""A stand-in for the freshness guard: the plan must name every entry.

Deliberately about FILES rather than about code, because what the hook has to get
right is which tree it hands a check, not what the check concludes. Read against
the staged tree it can only pass when both halves of a change were staged; read
against the working tree it passes for a commit that holds neither.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_the_plan_names_every_index_entry():
    plan = (HERE / "PLAN.md").read_text(encoding="utf-8")
    for name in (HERE / "INDEX.txt").read_text(encoding="utf-8").split():
        assert f"- {name}" in plan, f"the plan does not name {name}"
'''


@pytest.fixture(scope="module")
def hook_source() -> str:
    return HOOK.read_text(encoding="utf-8")


def _env() -> dict:
    """The harness's child environment: no inherited git plumbing, no prompts."""
    env = sandbox_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Hook Probe",
         "-c", "user.email=hook@example.invalid", "-c", "commit.gpgsign=false",
         "-c", f"core.hooksPath={repo / 'githooks'}", *args],
        capture_output=True, text=True, env=env, timeout=300, check=False)


def _write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo whose hook is the real one and whose guard is a stand-in."""
    root = tmp_path / "hooked"
    (root / "tests").mkdir(parents=True)
    (root / "githooks").mkdir()
    shutil.copy2(HOOK, root / "githooks" / "pre-commit")
    _write(root, "tests/test_specs_freshness.py", STUB)
    _write(root, INDEX, "alpha\n")
    _write(root, PLAN, "# plan\n- alpha\n")
    env = _env()
    assert _git(root, "init", "-q", env=env).returncode == 0
    assert _git(root, "add", "-A", env=env).returncode == 0
    first = _git(root, "commit", "-q", "-m", "start", env=env)
    assert first.returncode == 0, (
        f"the consistent starting state must be committable under the hook:\n"
        f"{first.stdout}{first.stderr}")
    return root


class TestTheHookReadsTheStagedTree:
    """The wiring: which tree each check is handed."""

    def test_it_materialises_the_index_into_a_scratch_directory(self, hook_source):
        """`checkout-index` writes exactly the index; nothing else does."""
        assert "checkout-index" in hook_source and "--prefix=\"$staged/\"" in hook_source, (
            "the hook has to write the staged tree out before it can read it — "
            "a worktree checks out HEAD, and stashing to test would disturb the "
            "developer's other work")
        assert "mktemp -d -t handsoff-staged" in hook_source, (
            "and somewhere disposable, outside the checkout: a scratch tree "
            "written inside the worktree would move the tree the two-writer gate "
            "stamps")

    def test_every_file_check_reads_the_staged_copy(self, hook_source):
        """The heart of it: no check may read the file on disk any more."""
        assert hook_source.count('"$staged/$f"') >= 2, (
            "both loops — byte-compile and shebang/bash-n — have to read the "
            "staged copy")
        for bare in ('py_compile "$f"', 'head -1 "$f"', 'bash -n "$f"',
                     '[ -f "$f" ]'):
            assert bare not in hook_source, (
                f"`{bare}` reads the working tree, which is the defect this hook "
                f"was fixed for")

    def test_it_refuses_instead_of_falling_back_to_the_working_tree(self, hook_source):
        """A hook that cannot make the scratch tree must not answer anyway."""
        after = hook_source[hook_source.index("mktemp -d"):]
        refusal = after[:after.index("trap ")]
        assert "could not make a scratch directory" in refusal, (
            "the mktemp failure has to say what it will not do instead")
        assert "exit 1" in refusal, (
            "and refuse: silently judging the working tree would answer a "
            "different question")
        assert "could not write the staged tree out" in hook_source, (
            "a `checkout-index` that fails is the same refusal, not a skip")

    def test_it_cleans_up_on_every_path(self, hook_source):
        assert "trap 'rm -rf \"$staged\"' EXIT" in hook_source, (
            "the scratch tree must go on the refusal path too, and the refusal "
            "path is where a trap is the only way to be sure")

    def test_the_spec_guard_runs_against_the_staged_tree(self, hook_source):
        leg = hook_source[hook_source.index('if [ -f "$staged/tests/test_specs_freshness.py"'):
                          hook_source.index("# 4. The full suite")]
        assert 'cd "$staged"' in leg and "pytest tests/test_specs_freshness.py" in leg, (
            "the spec-vs-tree check is the one part of the suite whose verdict is "
            "about files, and it is the whole point of materialising the index")
        assert "fail=1" in leg, "and its failure refuses the commit"

    def test_the_full_suite_still_runs_against_the_working_tree(self, hook_source):
        """Stated as a limit rather than left to look like an oversight."""
        suite = hook_source[hook_source.index("# 4. The full suite"):]
        command = [l for l in suite.splitlines()
                   if "pytest tests/ -q" in l and "specs_freshness" not in l]
        assert command, "the suite leg is gone"
        assert "$staged" not in command[0], (
            "a file-only copy has no `.git` and parts of the suite read this "
            "repository, so the suite stays on the working tree — the guard leg "
            "is what asks the file-shaped question of the commit")


class TestTheStagedTreeDecides:
    """End to end, with the real hook and a real `git commit`."""

    def test_a_partial_stage_is_refused(self, repo):
        """The shape that shipped: the code staged, its spec row only here.

        The working tree holds both halves and agrees with itself, which is why
        every earlier check — this hook included — called it good.
        """
        env = _env()
        _write(repo, INDEX, "alpha\nbeta\n")
        _write(repo, PLAN, "# plan\n- alpha\n- beta\n")
        assert _git(repo, "add", INDEX, env=env).returncode == 0
        out = _git(repo, "commit", "-q", "-m", "partial", env=env)
        said = out.stdout + out.stderr          # git forwards hook output on stderr
        assert out.returncode != 0, (
            f"a commit holding the entry but not its plan row must be refused:\n"
            f"{said}")
        assert "agree with its own spec" in said, (
            f"and the refusal has to name the staged tree as the subject:\n{said}")
        assert "the working tree holds both halves" in said, (
            "and say why the developer cannot see it for themselves")

    def test_staging_both_halves_is_committable(self, repo):
        env = _env()
        _write(repo, INDEX, "alpha\nbeta\n")
        _write(repo, PLAN, "# plan\n- alpha\n- beta\n")
        assert _git(repo, "add", INDEX, PLAN, env=env).returncode == 0
        out = _git(repo, "commit", "-q", "-m", "both halves", env=env)
        assert out.returncode == 0, (
            f"the honest commit has to go through:\n{out.stdout}{out.stderr}")

    def test_the_working_tree_does_not_decide(self, repo):
        """The complement: a broken working copy must not refuse a good commit.

        Staged consistent, working copy inconsistent — the hook's question is
        about the commit, so this is committable. It is also why the guard leg
        cannot be replaced by the working-tree suite.
        """
        env = _env()
        _write(repo, INDEX, "alpha\nbeta\n")
        _write(repo, PLAN, "# plan\n- alpha\n- beta\n")
        assert _git(repo, "add", INDEX, PLAN, env=env).returncode == 0
        _write(repo, PLAN, "# plan\n- alpha\n")          # the working copy only
        out = _git(repo, "commit", "-q", "-m", "staged tree is the question",
                   env=env)
        assert out.returncode == 0, (
            f"the staged tree agrees with itself, so the commit stands:\n"
            f"{out.stdout}{out.stderr}")
        shown = _git(repo, "show", "--stat", "--oneline", "HEAD", env=env).stdout
        assert PLAN in shown and INDEX in shown, (
            "and both halves are in the commit, which is what made it consistent")

    def test_a_staged_syntax_error_is_refused_even_when_the_working_copy_is_fixed(
            self, repo):
        """The byte-compile leg's own version of the same defect."""
        env = _env()
        _write(repo, "broken.py", "def broken(:\n    pass\n")
        assert _git(repo, "add", "broken.py", env=env).returncode == 0
        _write(repo, "broken.py", "def fixed():\n    return 1\n")   # on disk only
        out = _git(repo, "commit", "-q", "-m", "staged copy is broken", env=env)
        assert out.returncode != 0, (
            f"the commit contains the broken bytes:\n{out.stdout}{out.stderr}")

    def test_it_leaves_no_scratch_directory_behind(self, repo):
        """Both verdicts, counted the only way a leftover can be seen."""
        env = _env()
        before = set(Path(tempfile.gettempdir()).glob("handsoff-staged-*"))
        _write(repo, INDEX, "alpha\nbeta\n")
        _write(repo, PLAN, "# plan\n- alpha\n- beta\n")
        _git(repo, "add", INDEX, env=env)
        assert _git(repo, "commit", "-q", "-m", "refused", env=env).returncode != 0
        assert _git(repo, "add", PLAN, env=env).returncode == 0
        assert _git(repo, "commit", "-q", "-m", "accepted", env=env).returncode == 0
        after = set(Path(tempfile.gettempdir()).glob("handsoff-staged-*"))
        assert after == before, (
            f"a run left a scratch tree behind: {sorted(after - before)}")
