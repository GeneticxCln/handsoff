"""The mutation gate: is a new test's guard shown to be load-bearing?

A green suite and a test that asserts nothing are the same observation from the
outside, and this project was bitten by that twice in one session: a mutation
harness that replaced ``HOME`` reported eleven of eleven mutations caught while
every run was failing for want of pytest, and a mirror check that searched a
whole function definition always found the mirror's own name on the ``def``
line. Both were reported as passing. ``ci/mutation_gate.py`` exists so that a
new test cannot merge holding nothing, and this file pins the four defects that
were measured while it was being written — each one a case where the gate
reported something true about the wrong thing:

  * **The holder search matched an English word.** Selecting tests that mention
    the changed module by name pulled in ``test_audio.py`` (4,795 lines, 235
    tests, 25 s) for a commit about ``hardware.py``, because a comment there
    said "when the hardware does". Every other match was a FILENAME string in a
    set literal. Selection is now by the project's own convention — a test file
    named after the module — because a static import check cannot work here at
    all: ``test_hardware.py`` holds ``hardware.py`` and never imports it (it
    goes through ``conftest._load`` by path), so an ``ast`` walk finds zero
    holders and tokenizing finds zero too.

  * **A touched line inside an ``if`` BODY marked the whole ``if``.** Commit
    ``dee37b5`` renames a callee on lines 562-563 of ``prompt_context``; those
    lines sit in the body of an ``if`` that begins at 558, so the gate offered
    to invert that ``if`` and charged the commit for it. A true statement about
    a line nobody touched.

  * **The comparison rule crashed on its first real input.** ``_OP_TEXT`` is
    keyed by operator class, so the replacement text is looked up as
    ``_OP_TEXT[flipped]``. The first version wrote ``_OP_TEXT[type(flipped)]``,
    which asks for the key ``type`` — a ``KeyError`` on every file with a
    comparison in a touched line, which is every file worth mutating. The gate
    had never got as far as running a mutant.

  * **A survivor was one word wide.** "The shipped tests did not notice" and
    "nothing in the project notices" are different findings, and conflating
    them is the false red this gate was born from. They are now separate
    verdicts, and the wider set is run lazily, once, and only when a survivor
    exists.

The end-to-end class runs the real gate in a scratch repository with a real
pytest, because the properties that matter — a survivor fails, a caught mutant
passes, a red baseline refuses — cannot be shown by testing functions.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (GIT_PLUMBING, HERE, _load, sandbox_env,
                      shell_function)

GATE = HERE / "ci" / "mutation_gate.py"
GATES = HERE / "ci" / "gates.sh"


@pytest.fixture(autouse=True)
def _no_gits_own_plumbing(monkeypatch):
    """Git's exported environment dropped from THIS process, not just children.

    `sandbox_env()` scrubs `GIT_PLUMBING` for the drivers it hands a child, and
    it exists because the suite is run by this project's own pre-commit hook:
    a hook exports `GIT_INDEX_FILE`, `GIT_PREFIX` and an author identity to
    whatever it runs, so a scratch repository built with the inherited
    environment gets a pointer into the caller's index. `sandbox_env` could not
    help the classes here, because they call `main()` IN-PROCESS — the whole
    point of them is that the mutant loop is covered at all — and so they
    inherited the hook's own environment and ran `git worktree add` against
    handsoff's index.

    Measured by committing, not by reading: the first `git commit` of this work
    was REFUSED by its own pre-commit hook with 8 failures here, every one of
    them `could not lay down a worktree of HEAD`, while the same 49 tests
    passed in the working tree and passed in a staged-tree copy run on its own.
    Only the combination — a suite the hook is running, with in-process gate
    runs — produced it, which is exactly the shape CI never exercises and
    exactly the shape the hook exists to catch.
    """
    for name in GIT_PLUMBING:
        monkeypatch.delenv(name, raising=False)


def _load_gate():
    """`ci/mutation_gate.py` through conftest's loader, like every other ci/ script.

    Not a hand-built `spec_from_file_location`: `tests/test_sandbox.py` refuses
    a test file that pairs `module_from_spec` with `exec_module`, because that
    is a load which resolves the developer's real HOME and complains about
    nothing. The guard fired on the first run of this file.
    """
    return _load("mutation_gate", GATE)


class _File:
    """Just enough of a path for `mutants_for_file`, which reads and nothing else."""

    def __init__(self, text: str):
        self._text = text

    def exists(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8") -> str:
        del encoding
        return self._text

    def __truediv__(self, _other):
        return self


class _Repo:
    def __init__(self, text: str):
        self._file = _File(text)

    def __truediv__(self, _other):
        return self._file


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


@pytest.fixture(scope="module")
def gates_source() -> str:
    return GATES.read_text(encoding="utf-8")


# --------------------------------------------------------------- the wiring


class TestTheGateIsWired:
    def test_it_is_registered(self, gates_source):
        line = next(l for l in gates_source.splitlines()
                    if l.startswith("ALL_GATES="))
        assert "mutation" in line.split('"')[1].split(), (
            "the gate is not in ALL_GATES, so it never runs")

    def test_the_name_reaches_a_function(self, gates_source):
        """`mutation` has to become `gate_mutation`."""
        assert "gate_mutation() {" in gates_source, (
            "the run maps a gate name to `gate_${name//-/_}`")
        assert 'gate_${name//-/_}' in gates_source

    def test_the_function_actually_runs_the_gate(self, gates_source):
        """A `gate_mutation` that returns 0 without running anything passes the
        test above and reports a PASS forever.

        Measured: a `return 0` planted at the top of the body was caught by
        nothing, because the only test that mentioned the function read its
        name out of the source. So the body is checked for the two things that
        make it a gate: it RUNS the script, and it hands back the script's
        status rather than a constant.
        """
        body = shell_function(gates_source, "gate_mutation")
        assert "ci/mutation_gate.py" in body, (
            "gate_mutation never invokes the script it is named for")
        assert re.search(r'\breturn\s+"?\$rc', body), (
            "gate_mutation never returns the status it captured")

    @pytest.mark.parametrize("script_exit,expected", [(0, 0), (1, 1), (2, 2)])
    def test_the_function_hands_back_what_the_script_said(
            self, gates_source, tmp_path, script_exit, expected):
        """Run the extracted function against a stub script.

        Reading the body is not enough, and that is measured: a `return 0`
        planted at the TOP of the body still contained every line the
        source-reading test looked for, and went green. Only executing it
        shows that a FAIL from the gate cannot be laundered into a PASS.
        """
        (tmp_path / "ci").mkdir()
        (tmp_path / "ci" / "mutation_gate.py").write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            f"sys.exit({script_exit})\n", encoding="utf-8")
        # `shell_function` hands back the function WITH its header line, and
        # the body uses the two variables `gates.sh` sets at the top of the
        # script — so both are provided here rather than left undefined.
        script = (f'PYTHON="{sys.executable}"\nROOT="{tmp_path}"\n'
                  f'{shell_function(gates_source, "gate_mutation")}\n'
                  f'gate_mutation\necho "STATUS=$?"\n')
        done = subprocess.run(["bash", "-c", script], cwd=str(tmp_path),
                              capture_output=True, text=True, timeout=60,
                              check=False)
        assert f"STATUS={expected}" in done.stdout, (
            f"the script exited {script_exit} and the gate function reported "
            f"{done.stdout.strip()!r} — a {expected} was expected\n"
            f"body was:\n{shell_function(gates_source, 'gate_mutation')}")

    def test_it_runs_before_the_tree_gates(self, gates_source):
        """`mutation` needs a commit to diff; `two-writer` must stay last."""
        line = next(l for l in gates_source.splitlines()
                    if l.startswith("ALL_GATES="))
        gates = line.split('"')[1].split()
        assert gates[-1] == "two-writer", (
            "the tree verdict compares the worktree and must stay last")
        assert gates.index("mutation") < gates.index("clean-checkout")

    def test_the_usage_block_still_ends_where_it_claims(self, gates_source):
        """`usage()` prints `sed -n '2,41p'`; line 41 must be the last usage line.

        The range is a comment's promise about itself, and it is the kind that
        goes stale the moment a paragraph is added above the list.
        """
        line = next(l for l in gates_source.splitlines()
                    if "sed -n '2,41p'" in l)
        del line
        assert gates_source.splitlines()[40].strip() == "#   bash ci/gates.sh --help", (
            "line 41 moved: the header comment grew, so usage() no longer "
            "prints through the end of the usage list")

    def test_the_script_documents_its_own_exit_codes(self):
        text = GATE.read_text(encoding="utf-8")
        assert "Exit status: 0 pass, 1 fail, 2 skip" in text, (
            "the gate's three outcomes are the runner's contract and have to "
            "be stated where somebody will read them")


# ------------------------------------------------- the skip/refuse contract


class TestItRefusesRatherThanGuesses:
    """A gate that cannot run says so. It must never exit 0 having run nothing."""

    def test_a_directory_that_is_not_a_git_tree_skips(self, gate, tmp_path,
                                                      capsys):
        assert gate.main(["--root", str(tmp_path)]) == 2
        # The exit code alone cannot hold this. A non-repo returns 2 for three
        # independent reasons — no worktree, no HEAD, no base commit — so
        # removing any ONE guard left the code at 2 and a mutation run reported
        # all three as survivors. The message is what names the guard.
        assert "not a git worktree" in capsys.readouterr().out

    def test_a_base_that_is_not_a_commit_skips(self, gate, repo, capsys):
        assert gate.main(["--root", str(repo), "--base", "no-such-ref"]) == 2
        assert "not a commit" in capsys.readouterr().out

    def test_a_refusal_says_which_guard_stopped_it(self, gate, repo, capsys):
        """Each precondition has to be identifiable from outside, or a reader
        who removed the wrong one has nothing to go on."""
        gate.main(["--root", str(repo), "--base", "no-such-ref"])
        out = capsys.readouterr().out
        assert "mutation: SKIP" in out or "mutation:" in out
        assert "no-such-ref" in out, (
            f"the refusal does not name the ref it rejected: {out!r}")

    def test_a_dirty_checkout_refuses_to_lay_down_a_worktree(self, gate, repo):
        """The gate mutates SOURCE. Beside a tree somebody is writing to, a
        worktree is a race, so a dirty checkout is a refusal, not a warning."""
        (repo / "widget.py").write_text("BROKEN = 1\n", encoding="utf-8")
        assert gate.main(["--root", str(repo)]) == 2

    def test_no_production_in_the_diff_skips(self, gate, docs_only_repo):
        assert gate.main(["--root", str(docs_only_repo)]) == 2

    def test_a_test_with_nothing_to_mutate_skips(self, gate, test_only_repo):
        """Tests-only change: there is no production code to break, and saying
        "0 of 0 caught, PASS" would be a pass that measured nothing."""
        assert gate.main(["--root", str(test_only_repo)]) == 2

    def test_a_rename_with_no_mutant_site_skips(self, gate, rename_repo):
        """`dee37b5` renamed a function and wrote a docstring. Measured: 14
        touched lines, ZERO mutant sites — every rule needs a statement to
        rewrite and the diff had none. The honest verdict is a skip."""
        assert gate.main(["--root", str(rename_repo)]) == 2

    def test_a_red_baseline_refuses_rather_than_reporting_everything_caught(
            self, gate, red_baseline_repo, capsys):
        """The failure this gate exists to prevent.

        Measured 2026-09-27: a harness that could not find pytest scored 11 of
        11 mutations "caught" because a crashed run is not a passing one. So a
        red baseline is exit 1 and the word REFUSING, never a caught count.
        """
        code = gate.main(["--root", str(red_baseline_repo)])
        out = capsys.readouterr().out
        assert code == 1, f"expected a refusal, got {code}"
        assert "REFUSING" in out
        assert "caught by the shipped tests" not in out, (
            "a caught count was printed for a baseline that never passed")


# ------------------------------------------------------- the mutation rules


def _mutants(gate, source: str, touched: set[int]):
    tree = ast.parse(source)
    return [(r.rule, r.lineno, r.text) for r in gate._rewrites(source, tree, touched)]


class TestTheRules:
    def test_a_return_becomes_none(self, gate):
        rules = _mutants(gate, "def f():\n    return 7\n", {2})
        assert ("return-value", 2, "return None") in rules

    def test_a_comparison_is_flipped(self, gate):
        found = _mutants(gate, "def f(a, b):\n    return a < b\n", {2})
        assert any(rule == "compare" and "<=" in text
                   for rule, _line, text in found), found

    def test_the_comparison_rule_asks_for_the_key_the_table_has(self, gate):
        """`_FLIP` yields a CLASS, so the text is `_OP_TEXT[flipped]`.

        Written the other way it raises `KeyError: <class 'type'>` on the first
        comparison in a touched line, which is every file worth mutating, and
        the gate had never got as far as running a mutant when this was
        measured.

        The table alone is not the check, and that is the second half of the
        lesson: the first version of this test read the two tables and passed
        with `_OP_TEXT[type(flipped)]` in place, because nothing ever LOOKED
        THE ANSWER UP. So the lookup is performed here, the way the rule does
        it, and a table that cannot answer raises.
        """
        assert gate._OP_TEXT[gate._FLIP[ast.Lt]] == "<="
        assert gate._OP_TEXT[ast.Lt] == "<"
        for _op, flipped in gate._FLIP.items():
            assert flipped in gate._OP_TEXT, f"{flipped} has no replacement text"
        # The rule looks the operator up by `type(op)`, and `op` there is an
        # INSTANCE (`ast.Lt()`); `type(ast.Lt)` is `type` itself, which is the
        # KeyError this whole entry is about.
        instance = ast.Lt()
        looked_up = gate._OP_TEXT[gate._FLIP[type(instance)]]
        assert looked_up == "<=", (
            "looking the flipped operator up the way the rule looks it up "
            "fails, so the rule would raise instead of rewriting")

    def test_an_if_in_the_touched_lines_is_inverted(self, gate):
        found = _mutants(gate, "def f(x):\n    if x:\n        return 1\n", {2})
        assert any(rule == "invert-if" and "if not (x)" in text
                   for rule, _line, text in found), found

    def test_a_touched_line_in_the_BODY_does_not_invert_the_if(self, gate):
        """The `hardware.py:558` defect, as a rule.

        `dee37b5` renames a callee on lines 562-563 of `prompt_context`, which
        are in the body of an `if` starting at 558. The first version counted
        that as an invert-if site and charged the commit a mutation of forty
        lines it had not touched.
        """
        source = ("def f(x):\n"
                  "    if x:\n"
                  "        y = 1\n"
                  "        y = 2\n"
                  "        return y\n")
        body_touched = _mutants(gate, source, {3, 4, 5})
        assert not [r for r in body_touched if r[0] == "invert-if"], (
            f"an if whose BODY was touched was offered for inversion: {body_touched}")
        header_touched = _mutants(gate, source, {2})
        assert [r for r in header_touched if r[0] == "invert-if"], (
            "the if's own header WAS touched and must still be a site")

    def test_a_mutant_that_does_not_parse_is_discarded(self, gate, monkeypatch):
        """A rewrite the gate cannot compile is dropped, not run.

        Otherwise a syntax error is scored as a test failure, and the gate
        reports coverage it never measured — the same shape as the 11-of-11
        run that was failing for want of pytest.
        """
        source = "def f(x):\n    return x\n"
        bad = gate.Rewrite("return-value", ast.parse(source).body[0],
                           "return (")
        monkeypatch.setattr(gate, "_rewrites",
                            lambda *a, **k: [bad, bad, bad])
        assert gate.mutants_for_file(_Repo(source), "app.py", "HEAD", {2}) == [], (
            "a non-compiling mutant was returned for running")

    def test_compile_ok_says_so(self, gate):
        assert gate.compile_ok("x = 1\n") is True
        assert gate.compile_ok("def f(:\n") is False

    def test_the_untouched_bytes_are_byte_identical(self, gate):
        """Splicing by offset, not re-printing the tree: a mutant that reflowed
        the function would be measuring a reformatting."""
        source = "def f():\n    if True:\n        return 7\n\n\nX = 1\n"
        rewrite = gate._rewrites(source, ast.parse(source), {3})[0]
        mutated = gate._apply(source, rewrite)
        assert mutated.endswith("\n\n\nX = 1\n")
        assert mutated.count("\n") == source.count("\n")


# ------------------------------------------------------ the test selection


class TestWhichTestsItRuns:
    def test_the_changed_tests_are_never_dropped(self, gate, selection_repo):
        """Sorted alphabetically, the old cap of eight cut off
        `test_rule_copies.py` — the file the commit was about."""
        repo, production, changed = selection_repo
        phase_one, _two, _left = gate.test_files_for(repo, production, changed)
        for name in changed:
            assert name in phase_one, f"a changed test was dropped: {name}"

    def test_a_test_named_after_the_module_is_a_holder(self, gate, selection_repo):
        """`test_hardware.py` holds `hardware.py` and never imports it, so the
        project's naming convention is the only signal that works here."""
        repo, _production, _changed = selection_repo
        _one, phase_two, _left = gate.test_files_for(repo, ["hardware.py"], [])
        assert "tests/test_hardware.py" in phase_two

    def test_the_word_in_a_comment_is_named_never_run(self, gate, selection_repo):
        """`test_audio.py` says "when the hardware does" in a comment.

        Measured over five commits, the mention search found no true holder at
        all — so it is not a tier any more. The file is still NAMED, because a
        gate that quietly drops a file is the false red this gate was born
        from; it is just never run.
        """
        repo, _production, _changed = selection_repo
        one, two, named = gate.test_files_for(repo, ["hardware.py"], [])
        assert "tests/test_audio.py" not in one + two, (
            "a comment containing the word put a 4,795-line file in the run")
        assert "tests/test_audio.py" in named, (
            "it was dropped silently — the reader cannot widen a set they "
            "cannot see")

    def test_what_it_leaves_out_it_names(self, gate, selection_repo):
        repo, production, _changed = selection_repo
        _one, _two, named = gate.test_files_for(repo, production, [])
        assert named, "a search that finds nothing says nothing"


# ------------------------------------------------- the end-to-end behaviour


#: A production file with one rule whose boundary is OBSERVABLE.
#:
#: The first version of this fixture clamped a value, and the gate reported a
#: genuine survivor: `if value < low` mutated to `if value <= low` changes
#: nothing, because the branch returns `low` and `value == low` already returns
#: the same number. That is an equivalent mutant, and it is worth more to the
#: project that this file records why than that it avoided: a gate that cannot
#: tell an equivalent mutant from a real hole says so rather than pretending.
#: `describe` puts the boundary where a test can see it.
APP = '''"""A stand-in module holding one rule, for the gate to break."""


def describe(count):
    """`many` above three, `few` at three and below."""
    if count > 3:
        return "many"
    return "few"
'''

TEST_PINNING = '''"""A test that actually pins the rule the gate is about to break."""
from app import describe


def test_above_the_boundary():
    assert describe(4) == "many"


def test_at_the_boundary():
    # The line that makes `>` distinguishable from `>=`.
    assert describe(3) == "few"


def test_below_the_boundary():
    assert describe(0) == "few"
'''

#: A test file with the right NAME and no assertion about the rule at all.
TEST_DECORATIVE = '''"""Named as if it tested the app. It does not."""


def test_the_module_imports():
    import app
    assert app is not None
'''


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Mutation Gate",
         "-c", "user.email=gate@example.invalid", "-c", "commit.gpgsign=false",
         *args],
        capture_output=True, text=True, timeout=60, check=False)


def _new_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    return root


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)


@pytest.fixture
def git_env() -> dict:
    env = sandbox_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@pytest.fixture
def repo(tmp_path: Path, git_env: dict) -> Path:
    """A repo whose HEAD changes the rule AND SHIPS a test that pins it.

    Both in ONE commit, which is the point: a commit that touches production
    and tests separately has no diff against which to measure, and the gate
    skips instead — measured, not assumed, on `177b597`.
    """
    root = _new_repo(tmp_path / "repo")
    (root / "app.py").write_text("def describe(count):\n    return '?'\n",
                                 encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_nothing_yet():\n"
                                                "    assert True\n",
                                                encoding="utf-8")
    (root / "ci").mkdir()
    shutil.copy2(GATE, root / "ci" / "mutation_gate.py")
    _commit_all(root, "a rule nobody holds")
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(TEST_PINNING, encoding="utf-8")
    _commit_all(root, "a rule, and a test that pins it")
    return root


@pytest.fixture
def red_baseline_repo(repo: Path) -> Path:
    """The same change, with a test that fails on the committed tree.

    Amended into the change's own commit: the gate measures a DIFF, and a
    follow-up commit that touches only tests gives it nothing to measure.
    """
    (repo / "tests" / "test_app.py").write_text(
        TEST_PINNING + "\n\ndef test_broken():\n    assert describe(3) == 'lots'\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "--amend", "--no-edit")
    return repo


@pytest.fixture
def docs_only_repo(tmp_path: Path) -> Path:
    root = _new_repo(tmp_path / "docs")
    (root / "README.md").write_text("# hello\n", encoding="utf-8")
    _commit_all(root, "a readme")
    (root / "README.md").write_text("# hello again\n", encoding="utf-8")
    _commit_all(root, "an edit to a readme")
    return root


@pytest.fixture
def test_only_repo(tmp_path: Path) -> Path:
    root = _new_repo(tmp_path / "testsonly")
    (root / "app.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n",
                                                encoding="utf-8")
    _commit_all(root, "something to mutate")
    (root / "tests" / "test_app.py").write_text("def test_x():\n    assert X == 1\n",
                                                encoding="utf-8")
    _commit_all(root, "a test change with no production change")
    return root


@pytest.fixture
def rename_repo(tmp_path: Path) -> Path:
    """`dee37b5` in miniature: a rename and a docstring, no statement to rewrite."""
    root = _new_repo(tmp_path / "rename")
    (root / "app.py").write_text("def _gib(n):\n    return int(n)\n",
                                 encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_gib():\n"
                                                "    assert _gib(2) == 2\n",
                                                encoding="utf-8")
    _commit_all(root, "a private helper")
    (root / "app.py").write_text(
        'def _gib_or_zero(n):\n    """Bytes to GiB; anything unreadable is 0."""\n'
        "    return int(n)\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(
        "from app import _gib_or_zero\n\n\ndef test_gib():\n"
        "    assert _gib_or_zero(2) == 2\n", encoding="utf-8")
    _commit_all(root, "the same rule under a name that says which one it is")
    return root


@pytest.fixture
def held_elsewhere_repo(tmp_path: Path) -> Path:
    """A change whose NEW test pins nothing, beside an OLDER one that does.

    The phase-two tier exists exactly for this: `test_app.py` is the project's
    holder for `app.py` by name, so it is run when a mutant survives the tests
    the commit shipped. Here that older test catches the mutant, so the finding
    is not "nothing notices" but "the NEW test is decorative on this line" —
    a different sentence, a different fix, and the one that is easy to miss.
    """
    root = _new_repo(tmp_path / "held")
    (root / "app.py").write_text("def describe(count):\n    return '?'\n",
                                 encoding="utf-8")
    (root / "tests").mkdir()
    (root / "ci").mkdir()
    shutil.copy2(GATE, root / "ci" / "mutation_gate.py")
    (root / "tests" / "test_app.py").write_text(TEST_PINNING, encoding="utf-8")
    _commit_all(root, "a rule, and the test that holds it")
    # Now the change: same production, a brand new test that holds nothing, and
    # the old holder left exactly where it was. The two land in one commit, so
    # the diff has both a production file and a test file in it.
    _git(root, "reset", "-q", "--hard", "HEAD~1")
    (root / "app.py").write_text(APP, encoding="utf-8")
    (root / "tests" / "test_new_rule.py").write_text(TEST_DECORATIVE,
                                                     encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(TEST_PINNING, encoding="utf-8")
    _commit_all(root, "a rule, a new test that holds nothing, and the old one")
    return root


@pytest.fixture
def selection_repo(tmp_path: Path) -> tuple[Path, list[str], list[str]]:
    """A tree shaped like this one: the word 'hardware' in three places that
    are not imports, and one test file that really holds the module."""
    tests = tmp_path / "sel" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_hardware.py").write_text("def test_hw():\n    assert True\n",
                                             encoding="utf-8")
    (tests / "test_audio.py").write_text(
        "# index moves when the hardware does.\n" + "X = 1\n" * 200,
        encoding="utf-8")
    (tests / "test_ci_summary.py").write_text(
        'NAMES = {"handsoff.py", "settings_schema.py", "hardware.py"}\n',
        encoding="utf-8")
    (tests / "test_brain.py").write_text("# no hardware here\n", encoding="utf-8")
    (tests / "test_new_rule.py").write_text("def test_new():\n    assert True\n",
                                            encoding="utf-8")
    return tmp_path / "sel", ["hardware.py"], ["tests/test_new_rule.py"]


def _run(root: Path, *extra: str):
    """The real gate, in a real subprocess, sandboxed.

    `sandbox_env` is not optional here: the gate lays a git worktree down and
    runs pytest inside it, and an unsandboxed child resolves the developer's
    real HOME. `tests/test_sandbox.py` enforces it on the List literal rather
    than the call, and it fired on the first run of this file.
    """
    env = sandbox_env()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        [sys.executable, str(root / "ci" / "mutation_gate.py"),
         "--root", str(root), *extra],
        capture_output=True, text=True, env=env, timeout=900, check=False)


class TestTheVerdicts:
    """`SURVIVED` and `HELD-ELSEWHERE` are different findings.

    Collapsing them is what produced this gate's first false red: one word was
    doing two jobs, and "the new test did not notice" was reported as "nothing
    notices", which sends somebody to write a test that already exists.

    The two phases are separate functions on purpose. They ask the same
    question of the same exit status and want OPPOSITE answers, and merging
    them is a bug this file caught the same hour it was written: five
    HELD-ELSEWHERE mutants came back `caught` and the gate printed PASS.
    """

    def test_phase_one_calls_a_failure_a_catch(self, gate):
        assert gate.shipped_verdict(1) == "caught"
        assert gate.shipped_verdict(0) == "SURVIVED"
        assert gate.shipped_verdict(124) == "unknown"

    def test_phase_two_calls_a_failure_a_different_thing(self, gate):
        """The same failing run is a `caught` in one phase and a
        `HELD-ELSEWHERE` in the other, and the difference is the whole reason
        the second phase exists."""
        assert gate.preexisting_verdict(1) == "HELD-ELSEWHERE"
        assert gate.preexisting_verdict(0) == "SURVIVED"
        assert gate.preexisting_verdict(124) == "unknown"

    def test_both_phases_agree_about_a_timeout(self, gate):
        assert gate.shipped_verdict(124) == gate.preexisting_verdict(124)

    def test_both_survivors_fail_and_a_catch_does_not(self, gate):
        assert set(gate.FAILING) == {"SURVIVED", "HELD-ELSEWHERE"}, (
            "a catch is the gate working; a held-elsewhere is a new test that "
            "pins nothing even though an old one does")
        assert "caught" not in gate.FAILING
        assert "unknown" not in gate.FAILING, (
            "a timeout must not fail the gate: nothing was measured")

    def test_every_verdict_named_is_reachable(self, gate):
        produced = {gate.shipped_verdict(c) for c in (0, 1, 124)}
        produced |= {gate.preexisting_verdict(c) for c in (0, 1, 124)}
        assert produced == set(gate.VERDICTS), (
            f"unreachable: {set(gate.VERDICTS) - produced}; "
            f"undeclared: {produced - set(gate.VERDICTS)}")


class TestTheGateInProcess:
    """`main()` called directly, so the mutant loop is measured at all.

    The end-to-end class runs the gate as a SUBPROCESS, which is the honest
    shape for a script CI invokes — and which leaves `main()` and the loop
    invisible to coverage, because a child process is a different process. The
    measured consequence was `ci/mutation_gate.py` at 68% and the project's
    total at 84.78%, down from 85.09%: 121 statements of a gate, none of them
    seen. Same script, same scratch repository, same verdicts — in this
    process, so the lines that decide PASS from FAIL are covered.
    """

    def test_a_pinned_rule_passes_in_process(self, gate, repo, capsys):
        assert gate.main(["--root", str(repo)]) == 0, capsys.readouterr().out
        assert "PASS" in capsys.readouterr().out

    def test_a_decorative_test_fails_in_process(self, gate, repo, capsys):
        _git(repo, "reset", "-q", "--hard", "HEAD~1")
        (repo / "app.py").write_text(APP, encoding="utf-8")
        (repo / "tests" / "test_app.py").write_text(TEST_DECORATIVE,
                                                   encoding="utf-8")
        _commit_all(repo, "a rule, and a test that asserts nothing about it")
        capsys.readouterr()
        assert gate.main(["--root", str(repo)]) == 1
        assert "SURVIVED" in capsys.readouterr().out

    def test_the_report_is_written_as_json(self, gate, repo, tmp_path):
        """The report is what a pipeline reads; a run that computes verdicts
        and cannot say them afterwards is a gate with a secret."""
        out = tmp_path / "report.json"
        assert gate.main(["--root", str(repo), "--report", str(out)]) == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["base"] and report["production"] == ["app.py"]
        assert report["rules"] == list(gate.RULES)
        assert report["pass_criterion"] == gate.PASS_CRITERION
        assert report["mutants"], "a run with mutants wrote none"
        assert all(m["verdict"] in gate.VERDICTS for m in report["mutants"])
        assert all(m["note"] for m in report["mutants"]), (
            "a mutant was reported without saying what held it")

    def test_the_cap_is_reported_as_a_budget_not_a_result(self, gate, repo,
                                                          capsys):
        """`available` and `run` are two numbers, and only the second is a
        result. Printing the first as though it were the second is how a
        partial measurement reads as a complete one."""
        assert gate.main(["--root", str(repo), "--max", "1"]) == 0
        out = capsys.readouterr().out
        assert "NOT RUN" in out, out
        assert "budget, not a sample" in out, out

    def test_a_zero_cap_measures_nothing_and_says_so(self, gate, repo, capsys):
        """A cap of zero is not a pass. The first version fell through to the
        PASS line with nothing run, which is the one thing this gate exists to
        stop."""
        code = gate.main(["--root", str(repo), "--max", "0"])
        out = capsys.readouterr().out
        assert code == 2, f"a cap of 0 reported {code}: {out}"
        assert "no mutant was run" in out, out

    def test_the_tests_flag_overrides_the_selection(self, gate, repo, capsys):
        """When somebody names the tests, the gate runs those and stops
        guessing — the escape hatch the SKIP message points at."""
        assert gate.main(["--root", str(repo), "--tests",
                          "tests/test_app.py"]) == 0
        assert "tests/test_app.py" in capsys.readouterr().out

    def test_the_run_is_deterministic(self, gate, repo):
        """Two runs of one commit must pick the same mutants, or the gate is
        measuring a different thing each time it is asked."""
        first = gate.main(["--root", str(repo), "--report",
                           str(repo.parent / "one.json")])
        second = gate.main(["--root", str(repo), "--report",
                            str(repo.parent / "two.json")])
        assert first == second == 0
        one = json.loads((repo.parent / "one.json").read_text(encoding="utf-8"))
        two = json.loads((repo.parent / "two.json").read_text(encoding="utf-8"))
        assert [(m["file"], m["line"], m["rule"], m["verdict"])
                for m in one["mutants"]] == \
               [(m["file"], m["line"], m["rule"], m["verdict"])
                for m in two["mutants"]]


class TestItEndToEnd:
    """The real gate, a real pytest, a real git worktree — in a scratch repo."""

    def test_a_pinned_rule_passes(self, repo: Path):
        done = _run(repo)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "PASS" in done.stdout
        assert "SURVIVED" not in done.stdout

    def test_a_decorative_test_fails(self, repo: Path):
        """The whole point. A test named for the module, asserting nothing about
        the rule it supposedly covers, must NOT be able to merge."""
        # Rewound and re-committed as ONE change: production and test together.
        # A follow-up commit touching only tests gives the gate no production
        # diff, and it skips — correctly, and not what this test is about.
        _git(repo, "reset", "-q", "--hard", "HEAD~1")
        (repo / "app.py").write_text(APP, encoding="utf-8")
        (repo / "tests" / "test_app.py").write_text(TEST_DECORATIVE,
                                                   encoding="utf-8")
        _commit_all(repo, "a rule, and a test that asserts nothing about it")
        done = _run(repo)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "SURVIVED" in done.stdout, done.stdout

    def test_a_held_elsewhere_is_reported_as_its_own_finding(
            self, held_elsewhere_repo: Path):
        """Not a coverage hole, and not a pass either.

        An older test holds the rule, so "nothing notices" would be false; the
        new test notices nothing, so "the change is covered" would also be
        false. The gate has to say which of the two it found, because the two
        lead to different work.
        """
        done = _run(held_elsewhere_repo)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "HELD-ELSEWHERE" in done.stdout, done.stdout
        assert "not a coverage hole" in done.stdout.lower(), (
            "the message does not say that an older test already held it")

    def test_it_leaves_the_checkout_alone(self, repo: Path):
        """It rewrites production source. In THIS checkout that would publish a
        mutant to the next reader — an editor, another agent, a `git checkout`."""
        before = {p.name: p.read_bytes() for p in repo.glob("*.py")}
        done = _run(repo)
        assert done.returncode == 0, done.stdout + done.stderr
        after = {p.name: p.read_bytes() for p in repo.glob("*.py")}
        assert before == after, "the gate rewrote the checkout it was run from"
        listed = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                                capture_output=True, text=True,
                                env=sandbox_env(), check=False)
        assert len(listed.stdout.strip().splitlines()) == 1, (
            f"a worktree was left behind: {listed.stdout}")

    def test_the_worktree_is_gone_even_when_it_refuses(self, repo: Path):
        """A refusal is worth nothing if reporting it damages the tree it
        reports about — the discipline `clean-checkout` already follows.

        AMENDED rather than added as a new commit: a tests-only commit has no
        production diff to measure, and the gate skips before it ever lays a
        worktree down. That skip is correct, and it is not what this test is
        about.
        """
        (repo / "tests" / "test_app.py").write_text(
            TEST_PINNING + "\n\ndef test_broken():\n    assert False\n",
            encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--no-verify", "--amend", "--no-edit")
        assert _run(repo).returncode == 1
        listed = subprocess.run(["git", "worktree", "list"], cwd=str(repo),
                                capture_output=True, text=True,
                                env=sandbox_env(), check=False)
        assert listed.stdout.count("worktree") == 1, (
            f"the refusal left a worktree behind: {listed.stdout}")

    def test_it_restores_the_worktree_it_mutated(self, repo: Path, tmp_path: Path):
        """The mutants are written over real source files, and put back.

        `--keep-worktree` is how the property is observable from outside: the
        tree is laid down and NOT removed, so the files can be read afterwards.
        Measured: replacing the restore with `pass` was caught by nothing,
        because the surviving test only checked the files in the checkout the
        gate was run FROM — which the gate never touches. A mutant left in a
        tree somebody then reads is the failure this gate was built to avoid.
        """
        keep = tmp_path / "kept"
        before = (repo / "app.py").read_bytes()
        done = _run(repo, "--keep-worktree", str(keep))
        assert done.returncode == 0, done.stdout + done.stderr
        tree = keep / "app.py"
        assert tree.exists(), f"the worktree was not laid down: {done.stdout}"
        assert tree.read_bytes() == before, (
            "a mutant was left in the worktree's source after the run")

    def test_it_leaves_no_worktree_metadata_behind(self, repo: Path):
        """A deleted directory is hidden by `git worktree list` automatically,
        so that is NOT the check — the leftover is the admin record in
        `.git/worktrees/`, which is what `git worktree prune` exists to clear
        and what a reader of `git worktree list` would trip over later.

        Measured: dropping the `worktree remove` from `__exit__` was caught by
        nothing, because by then the directory was gone and the list looked
        clean.
        """
        assert _run(repo).returncode == 0
        admin = repo / ".git" / "worktrees"
        left = sorted(p.name for p in admin.iterdir()) if admin.is_dir() else []
        assert not left, f"worktree admin records left behind: {left}"

    def test_a_timeout_is_neither_caught_nor_survived(self, gate, tmp_path):
        """A run that timed out noticed nothing.

        Scoring it as caught would report a measurement that never happened;
        scoring it as survived would send somebody to write a test for a line
        the suite simply never got to. It is `unknown`, and it is reported as
        its own number so neither of the other two can absorb it.

        The scoring is checked where it happens. The first version of this test
        called `run_tests` and asserted 124, which stayed green when the
        CONSUMER of that 124 was changed to score a timeout as caught — the
        marker and the verdict are a layer apart, and only the verdict is the
        claim.
        """
        code, tail = gate.run_tests(tmp_path, ["tests/test_nothing.py"],
                                    timeout=0.001)
        assert code == 124, f"a timeout must be 124, not a verdict: {tail!r}"
        assert "timed out" in tail
        for scorer in (gate.shipped_verdict, gate.preexisting_verdict):
            verdict = scorer(124)
            assert verdict == "unknown", (
                f"a timeout was scored {verdict!r} by {scorer.__name__}")
            assert verdict not in gate.FAILING, (
                "a timeout must not fail the gate: nothing was measured")
