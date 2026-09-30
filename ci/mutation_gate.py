#!/usr/bin/env python3
"""A change's tests must be shown to depend on the code the change touched.

A suite that is green says nothing about whether any particular test would
notice a particular change. Measured 2026-09-27, twice, in one session: a
mutation harness that replaced ``HOME`` reported eleven of eleven mutations
caught while every run was failing for want of pytest, and a mirror check
searched the whole function definition and so always found the mirror's own
name on the ``def`` line. Both were reported as passing. A green run and a
green mutation run look identical from the outside, and only one of them means
anything.

So this gate answers one question about a diff: **if the production code it
touched were broken, would the tests it shipped with notice?** Not the whole
tree, and not every test — the changed tests, against mutants of the changed
functions. That is the merge question, and it is the one a review cannot
answer by reading.

Three decisions, each forced by a measurement rather than by taste:

**It never touches this checkout.** The tree it mutates is a detached git
worktree of HEAD, laid down in a scratch directory outside the tree and removed
on every path — the same shape ``gates.sh``'s ``clean-checkout`` gate already
uses. A gate that rewrote source files in a SHARED checkout (an editor, another
agent, a ``git checkout``) would publish mutants to whoever read the file next,
and ``gates.sh`` exists precisely because a verdict about a tree that moved is
worth nothing.

**The baseline must be green before any mutant runs.** If the changed tests do
not pass unmutated, the gate refuses (exit 1) and says so, rather than
reporting every mutant "caught" by a suite that was already red. That refusal
is the whole lesson of the first mutation run of that session, encoded.

**A mutant that does not parse is not a mutant.** Every rewrite is re-parsed
before it is run, and one that does not compile is discarded and counted, never
counted as caught — otherwise a syntax error would be scored as a test failure
and the gate would report coverage it never measured.

The pass criterion is the part worth arguing about, and it was set by running
this against the project's own history rather than by picking a number: see
``PASS_CRITERION`` below, which states what a survivor means and which
survivors are refusals rather than failures.

Usage:
  mutation_gate.py [--root .] [--base REF] [--max N] [--tests FILE ...]
                   [--report FILE] [--keep-worktree DIR] [--ci]

Exit status: 0 pass, 1 fail, 2 skip (a gate that could not run says so).
`--ci` turns the three skips that are about the DIFF (nothing to break, no test
changed, no mutant site) into 0; a skip about the environment stays 2.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

#: What a survivor means, and what the gate does about it. Set by measurement
#: over the project's own commits (see GAP_ANALYSIS.md, "A mutation gate, and
#: what it is allowed to say"), not by taste: a gate that blocks correct work
#: gets deleted, and a gate that only reports gets ignored.
PASS_CRITERION = (
    "a survivor is a mutant the changed tests did not notice, which is the "
    "claim under test"
)

#: The mutation rules. Deliberately few, and each one load-bearing: these are
#: the edits that change what a function DOES rather than how it reads. A rule
#: set that also renamed locals and rewrote log strings would produce survivors
#: no test could reasonably be expected to catch, and a gate whose survivors
#: are unfair is a gate that gets switched off.
RULES = ("return-value", "invert-if", "compare", "threshold", "drop-call")

#: What counts as a production source, and what counts as a test. Discovered by
#: shape, the way ``compile_all.py`` discovers what to compile: a list is a
#: second thing to forget when a module is added.
PRODUCERS = ("core/*.py", "*.py")
TESTS = "tests/test_*.py"


def _log(message: str) -> None:
    print(message, flush=True)


# ------------------------------------------------------------------ the diff


def git(root: pathlib.Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def base_ref(root: pathlib.Path, asked: str | None) -> tuple[str | None, str]:
    """The commit the change is measured against, and why.

    The merge-base with the tracked main branch is what a review actually sees;
    ``HEAD~1`` is the fallback for a branch with no upstream. A tree with
    neither has nothing to diff against, which is a skip and not a pass.
    """
    if asked:
        if git(root, "rev-parse", "--verify", "--quiet", asked)[0] != 0:
            return None, f"{asked} is not a commit in this repository"
        return asked, "as asked"
    code, out = git(root, "merge-base", "HEAD", "origin/main")
    if code == 0 and out:
        return out, "merge-base with origin/main"
    code, out = git(root, "rev-parse", "--verify", "--quiet", "HEAD~1")
    if code == 0 and out:
        return out, "HEAD~1 (no origin/main to merge against)"
    return None, ("no origin/main and no HEAD~1: there is no earlier commit "
                  "to measure a change against")


def changed_paths(root: pathlib.Path, base: str) -> list[str]:
    code, out = git(root, "diff", "--name-only", f"{base}..HEAD")
    if code != 0:
        return []
    return [line for line in out.splitlines() if line]


def uncommitted(root: pathlib.Path) -> bool:
    """True when this checkout is not a clean HEAD — INCLUDING untracked files.

    `git diff --quiet HEAD` is the obvious way to ask and it is wrong here: it
    does not see untracked files, while `test_files_for` globs this checkout's
    `tests/` to decide what to run. An untracked `tests/test_thing.py` would be
    selected and then be missing from the worktree of HEAD, and pytest would
    spend the whole run failing to collect a file that was never committed.
    Measured by writing one and watching the gate walk straight past it.
    """
    code, out = git(root, "status", "--porcelain")
    return code == 0 and bool(out)


def is_production(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    if path.startswith("tests/") or path.startswith("ci/"):
        return False
    return path.count("/") <= 1


def is_test(path: str) -> bool:
    return (path.startswith("tests/test_") and path.endswith(".py")
            and "/" not in path[6:])


def test_files_for(repo: pathlib.Path, production: list[str],
                   changed_tests: list[str], widen: int = 0,
                   ) -> tuple[list[str], list[str], list[str]]:
    """The tests a mutant of `production` could be caught by, and the evidence.

    Returns ``(phase_one, phase_two, named_only)``. Phase one is the claim under
    test — the tests this commit shipped. Phase two is the honesty check, run
    only for a survivor — the tests the project already had. Anything in the
    third list is printed and NEVER run, because a gate that silently drops the
    file that would have caught the mutant is the first false red this project
    hit.

    Two tiers, and the measurement is what stopped there being three:

    1. **The changed tests, always, uncapped.** A cap that dropped one would be
       measuring something other than what the commit shipped; the first run of
       this gate did exactly that, sorted alphabetically, cutting off
       `test_rule_copies.py` — the file the commit was about.

    2. **The test file with the same name as the module.** The project names
       tests after the module they hold (`core/brain.py` →
       `tests/test_brain.py`, `hardware.py` → `tests/test_hardware.py`,
       `core/calendar.py` → `tests/test_calendar.py`). This tier is what makes
       the gate honest on a commit that fixes a bug in `hardware.py` and does
       not touch `test_hardware.py`.

    A third tier — "test files that mention the module's name" — was measured
    over this repository's own five most recent commits and produced **no true
    holder at all**. Every file it found matched the English word or a filename
    string, not the module: a comment in `test_audio.py` ("when the hardware
    does"), a set literal of file names in `test_ci_summary.py`, a docstring
    saying "no hardware" in `test_notify_coalesce.py`. It cost 25 seconds a run
    for `test_audio.py` and found nothing. So it is no longer a tier: the
    mention search still runs, and its results are what the gate PRINTS when a
    survivor needs a human to widen the set by hand.

    A static import check is not available, and that was measured too:
    `test_hardware.py` holds `hardware.py` and never imports it — it goes
    through `conftest._load` by path — so an `ast` walk finds **zero** holders
    for `hardware.py` and **zero** for `core/calendar.py`, and tokenizing finds
    zero too. The naming convention is not a proxy for a signal that does not
    exist; it is the signal.
    """
    stems = {pathlib.Path(p).stem for p in production}
    phase_one = sorted(set(changed_tests))
    phase_two = set(phase_one)
    named: list[str] = []
    for path in sorted((repo / "tests").glob("test_*.py")):
        relative = f"tests/{path.name}"
        if relative in phase_two:
            continue
        if path.stem.removeprefix("test_") in stems:
            phase_two.add(relative)
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(re.search(rf"\b{re.escape(stem)}\b", source) for stem in stems):
            named.append(relative)
    return phase_one, sorted(phase_two), named[widen:]


# ------------------------------------------------------------- the mutants
# A rewrite is a (rule, line, start-offset, end-offset, replacement) applied to
# the file's own text, so everything the mutation does not name stays
# byte-identical. Splicing by position rather than by re-printing the tree is
# what keeps a mutant from being a reformatting of the function it mutates.


class Rewrite:
    __slots__ = ("rule", "lineno", "col", "end_lineno", "end_col", "text")

    def __init__(self, rule, node, text):
        self.rule = rule
        self.lineno = node.lineno
        self.col = node.col_offset
        self.end_lineno = getattr(node, "end_lineno", node.lineno)
        self.end_col = getattr(node, "end_col_offset", node.col_offset)
        self.text = text

    @property
    def label(self) -> str:
        return f"{self.rule} at line {self.lineno}"


def _segment(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


def _rewrites(source: str, tree: ast.AST, touched: set[int]) -> list[Rewrite]:
    """Every mutant of the statements this diff touched, in the rules above.

    Only lines the diff touched: a mutant of an untouched line is a statement
    about the codebase rather than about this change, and the gate's question is
    whether THIS change is covered.

    A statement is a site when a touched line falls inside the part of it the
    rule actually rewrites — for `invert-if`, the CONDITION, not the body.
    Measured, because the first version did not: `hardware.py`'s
    `prompt_context` renames a callee on lines 562-563, which sit in the body
    of an `if` that starts at line 558, and the gate reported that `if` as a
    mutant site. It was a true statement about a line nobody touched, and it
    cost the commit a FAIL on a function the commit did not change.
    """
    out: list[Rewrite] = []

    def spans(node) -> bool:
        end = getattr(node, "end_lineno", node.lineno)
        return any(node.lineno <= line <= end for line in touched)

    def header(node: ast.If) -> bool:
        """True when a touched line is in `if <test>:`, not in its body."""
        end = getattr(node.test, "end_lineno", node.lineno)
        return any(node.lineno <= line <= end for line in touched)

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None and spans(node):
            out.append(Rewrite("return-value", node, "return None"))
        elif isinstance(node, ast.If) and header(node):
            out.append(Rewrite("invert-if", node,
                               _invert(source, node)))
        elif isinstance(node, ast.Compare) and spans(node):
            for op in node.ops:
                # `_FLIP` maps an operator CLASS to its flipped class, so the
                # replacement text is looked up by the class itself. Writing
                # `_OP_TEXT[type(flipped)]` asks for the key `type` — and
                # raised KeyError on the first file with a comparison in a
                # touched line, which is every one of them. The gate crashed
                # on core/brain.py before it ever ran a mutant.
                flipped = _FLIP.get(type(op))
                if flipped:
                    out.append(Rewrite("compare", node,
                                       _segment(source, node).replace(
                                           _OP_TEXT[type(op)],
                                           _OP_TEXT[flipped], 1)))
        elif isinstance(node, ast.Constant) and spans(node) \
                and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool) and node.value not in (0, 1):
            out.append(Rewrite("threshold", node, "0"))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and spans(node):
            out.append(Rewrite("drop-call", node, "pass"))
    return [r for r in out if r.text and r.text.strip()]


_FLIP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
}
_OP_TEXT = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
    ast.Gt: ">", ast.GtE: ">=",
}


def _invert(source: str, node: ast.If) -> str:
    """`if X:` -> `if not (X):`, keeping the body on the same line."""
    text = _segment(source, node)
    head, sep, tail = text.partition(":")
    if not sep or not head.strip().startswith("if"):
        return ""
    return f"if not ({head.strip()[2:].strip()}):{tail}"


def _apply(source: str, rewrite: Rewrite) -> str:
    """The rewritten text, by offset, from the file's own bytes."""
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    begin = starts[rewrite.lineno - 1] + rewrite.col
    end = starts[rewrite.end_lineno - 1] + rewrite.end_col
    return source[:begin] + rewrite.text + source[end:]


def compile_ok(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


# ------------------------------------------------------------- the worktree


class Worktree:
    """A detached checkout of HEAD in a scratch directory, removed on every path.

    The same discipline `gates.sh`'s clean-checkout gate uses, for the same
    reason: the tree is shared, and this one rewrites source files. A mutant
    published into the shared checkout is a mutant the next reader — an editor,
    another agent, a `git checkout` — can read.
    """

    def __init__(self, root: pathlib.Path, keep: str | None = None):
        self.root = root
        self.keep = keep
        self.dir: pathlib.Path | None = None
        self.path: pathlib.Path | None = None

    def __enter__(self) -> pathlib.Path:
        if self.keep:
            self.dir = pathlib.Path(self.keep)
            self.dir.mkdir(parents=True, exist_ok=True)
            self.path = self.dir
            code, _out = git(self.root, "worktree", "add", "--detach",
                             "--force", str(self.path), "HEAD")
            if code != 0:
                raise RuntimeError("git worktree add failed")
            return self.path
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="handsoff-mutation-"))
        self.path = self.dir / "tree"
        code, _out = git(self.root, "worktree", "add", "--detach", "--quiet",
                         str(self.path), "HEAD")
        if code != 0:
            raise RuntimeError("git worktree add failed")
        return self.path

    def __exit__(self, *exc) -> bool:
        if self.keep:
            # `--keep-worktree` is for reading what the gate did to the source,
            # so the removal has to come AFTER this check. It used to: the tree
            # was removed and pruned first and the keep flag was consulted
            # afterwards, so the flag kept the `mkdtemp` directory (which a
            # kept run does not have) and not the tree. Measured by asking for a
            # kept worktree and finding the directory gone.
            git(self.root, "worktree", "prune")
            return False
        git(self.root, "worktree", "remove", "--force", str(self.path))
        git(self.root, "worktree", "prune")
        if self.dir and self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        return False


# ------------------------------------------------------------------ running


def run_tests(tree: pathlib.Path, tests: list[str], timeout: float) -> tuple[int, str]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen",
           "PYTHONPYCACHEPREFIX": tempfile.gettempdir() + "/handsoff-mutation-pyc",
           "COVERAGE_FILE": tempfile.gettempdir() + "/handsoff-mutation-cov/.coverage"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:randomly",
             "-p", "no:cacheprovider", "--no-header", "-x", "--tb=no"],
            cwd=tree, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout or "").strip().splitlines()[-1:][0] \
        if proc.stdout.strip() else ""


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------- main


def mutants_for_file(tree: pathlib.Path, relative: str, base: str,
                     available: set[int]) -> list[tuple[Rewrite, str]]:
    path = tree / relative
    if not path.exists():
        return []
    source = path.read_text(encoding="utf-8")
    try:
        tree_ast = ast.parse(source, filename=str(relative))
    except SyntaxError:
        return []
    out = []
    for rewrite in _rewrites(source, tree_ast, available):
        mutated = _apply(source, rewrite)
        if mutated == source or not compile_ok(mutated):
            continue
        out.append((rewrite, mutated))
    return out


def touched_lines(repo: pathlib.Path, base: str, relative: str) -> set[int]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--unified=0", base, "--", relative],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return set()
    lines: set[int] = set()
    for row in proc.stdout.splitlines():
        if row.startswith("@@"):
            body = row.split("+")[1].split("@@")[0].strip()
            if "," in body:
                start, count = body.split(",")
            else:
                start, count = body, "1"
            first = int(start)
            for offset in range(int(count)):
                lines.add(first + offset)
    return lines


#: What each verdict means, and which of them fail the gate. A table rather
#: than branches buried in `main`'s loop, because the mapping IS the gate's
#: claim and while it lived in the loop no test could reach it: "a timeout is
#: scored as caught" survived a mutation run for exactly that reason.
#:
#: `SURVIVED` and `HELD-ELSEWHERE` both fail. The first means nothing in the
#: project notices; the second means an OLDER test noticed and the new one did
#: not, so the new test is decorative on that line. Neither is a reason to
#: merge a test that pins nothing. `unknown` does not fail: a timeout is not a
#: measurement, and failing on one would be the gate inventing a result.
VERDICTS = ("caught", "SURVIVED", "HELD-ELSEWHERE", "unknown")
FAILING = ("SURVIVED", "HELD-ELSEWHERE")

#: The two phases ask OPPOSITE questions about the same exit status, and
#: treating them as one function is a bug this repository paid for in the hour
#: it was written.
#:
#: Phase one asks "did the tests this commit SHIPPED notice?" — a failing run
#: is the good news. Phase two asks "did a test the commit did NOT touch
#: notice?" — a failing run is STILL the good news, but it is a different
#: answer, and it is the only thing that separates `SURVIVED` from
#: `HELD-ELSEWHERE`.
#:
#: Collapsing them into one function reported five HELD-ELSEWHERE mutants and
#: then printed PASS, because every one of them came back `caught`.
def shipped_verdict(code: int) -> str:
    """Did the tests this commit shipped notice the mutant?"""
    if code == 124:
        return "unknown"
    return "caught" if code != 0 else "SURVIVED"


def preexisting_verdict(code: int) -> str:
    """Did a test the commit did not touch notice it?

    Only asked when phase one passed. A failure here is the good news, and it
    says the module was already held — so the new test is the decorative one.
    """
    if code == 124:
        return "unknown"
    return "HELD-ELSEWHERE" if code != 0 else "SURVIVED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", default=".",
                        help="the checkout to measure")
    parser.add_argument("--base", default=None,
                        help="the commit the change is measured against")
    parser.add_argument("--max", type=int, default=12, dest="cap",
                        help="the most mutants to run (default 12)")
    parser.add_argument("--tests", nargs="*", default=None,
                        help="the test files to run (default: those the diff "
                             "changed, plus the project's holder for the "
                             "touched module)")
    parser.add_argument("--widen", type=int, default=0, dest="widen",
                        help="mention-only test files to ADD to the run (0, "
                             "the default, runs none of them: measured over "
                             "five commits they produced no true holder)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="seconds per run (default 600)")
    parser.add_argument("--report", default=None,
                        help="write the result table as JSON here")
    parser.add_argument("--keep-worktree", default=None,
                        help="lay the worktree down here and keep it")
    parser.add_argument("--ci", action="store_true",
                        help="for a pipeline: a diff with NOTHING this gate can "
                             "measure (no Python it can break, no test changed, "
                             "no mutant site) exits 0 instead of 2, because "
                             "there the diff is the whole input and a docs-only "
                             "or tests-only change is not a failure. A skip "
                             "about the ENVIRONMENT (not a git worktree, no "
                             "HEAD, a dirty checkout, an unresolvable base, a "
                             "worktree that cannot be laid down, a cap of zero) "
                             "still exits 2: a gate that could not run is not "
                             "a pass")
    args = parser.parse_args(argv)
    # The three skips that are about the DIFF, not about whether the gate could
    # run. Locally they stay 2 (a skip is said out loud, and `ci/gates.sh` shows
    # it as SKIP); a pipeline passes `--ci` and a change with nothing to break
    # does not turn the build red.
    nothing_to_measure = 0 if args.ci else 2

    repo = pathlib.Path(args.root).resolve()
    if git(repo, "rev-parse", "--is-inside-work-tree")[0] != 0:
        _log("mutation: not a git worktree — HEAD cannot be laid down here")
        return 2
    if git(repo, "rev-parse", "--verify", "--quiet", "HEAD")[0] != 0:
        _log("mutation: HEAD has no commit yet")
        return 2
    if uncommitted(repo):
        _log("mutation: the checkout has uncommitted changes — refusing to "
             "lay down a worktree beside a tree that is being written to")
        return 2

    base, why = base_ref(repo, args.base)
    if base is None:
        _log(f"mutation: {why}")
        return 2

    changed = changed_paths(repo, base)
    production = sorted(p for p in changed if is_production(p))
    changed_tests = sorted(p for p in changed if is_test(p))
    phase_one, phase_two, left_out = ([], [], [])
    if args.tests:
        phase_one = phase_two = list(args.tests)
    elif production:
        phase_one, phase_two, left_out = test_files_for(
            repo, production, changed_tests, widen=args.widen)
    else:
        phase_one = phase_two = list(changed_tests)
    report: dict = {"base": base, "base_why": why, "changed": changed,
                    "production": production, "changed_tests": changed_tests,
                    "phase_one_tests": phase_one, "phase_two_tests": phase_two,
                    "tests_not_run": left_out,
                    "rules": list(RULES), "pass_criterion": PASS_CRITERION,
                    "mutants": [], "skipped": None}

    if not production:
        # A commit that touches no Python this gate can break. Measured on
        # `177b597` (an installer flag): the only non-test file it changed was
        # `install.sh`, and this branch raised `NameError: name 'tests' is not
        # defined` — a left-over name from before the two-phase split — so the
        # gate CRASHED instead of skipping. A crash is not a verdict, and the
        # commit that proved it was the most ordinary one in the history.
        report["skipped"] = ("no Python source in the diff that this gate can "
                             "break"
                             + (" (only tests changed, and a test with nothing "
                                "to mutate cannot be shown load-bearing)"
                                if phase_one else ""))
        _log(f"mutation: SKIP — {report['skipped']}")
        _write_report(args.report, report)
        return nothing_to_measure
    if not changed_tests and not args.tests:
        # Not a skip to be quiet about: this is the case where the first run
        # of this gate produced a FALSE RED. `hardware.py::_gib` was renamed
        # with no test change; the tests that hold the rename live in
        # `test_hardware.py`, which the commit did not touch, so running only
        # the changed tests reported a survivor for a function that has been
        # tested all along. There is no NEW guard here to show load-bearing,
        # and the honest verdict is a skip that names the holders — not a
        # failure that sends someone to write a test which already exists.
        _log("mutation: SKIP — no test changed, so this commit ships no new "
             "guard to show load-bearing. The tests that hold a change like "
             "this one are usually in files the commit did not touch:")
        for name in sorted(set(phase_one) | set(phase_two)
                         | set(left_out)):
            _log(f"    {name}")
        _log("mutation: run it by hand with: --tests <file> ...")
        report["skipped"] = "no test changed; holders named above"
        _write_report(args.report, report)
        return nothing_to_measure
    if not phase_one:
        report["skipped"] = "no test file to run against the mutants"
        _log(f"mutation: SKIP — {report['skipped']}")
        _write_report(args.report, report)
        return 2

    _log(f"mutation: base {base[:9]} ({why})")
    _log(f"mutation: production {', '.join(production)}")
    _log(f"mutation: shipped     {', '.join(phase_one)}")
    if phase_two != phase_one:
        _log(f"mutation: preexisting {', '.join(set(phase_two) - set(phase_one))}"
             "  (run only to tell a survivor from a hole)")
    if left_out:
        _log(f"mutation: not run    {', '.join(left_out)}")
        _log("mutation:              (files that MENTION the module's name but "
             "never load it — measured over this history, none of them has "
             "ever held one. Widen by hand with --widen N or --tests FILE.)")

    try:
        tree_ctx = Worktree(repo, args.keep_worktree)
        tree = tree_ctx.__enter__()
    except RuntimeError as exc:
        _log(f"mutation: could not lay down a worktree of HEAD ({exc})")
        return 2

    try:
        started = time.monotonic()
        code, tail = run_tests(tree, phase_one, args.timeout)
        if code != 0:
            # The refusal that matters most. Reporting "every mutant caught"
            # against a red suite is how the first mutation run of 2026-09-27
            # reported 11/11 while every run was failing for want of pytest.
            _log(f"mutation: REFUSING — the tests this change ships do not "
                 f"pass on the committed tree ({tail or 'no output'}). A green "
                 "baseline is what makes a caught mutant mean anything.")
            report["refused"] = "baseline red"
            report["baseline"] = tail
            return 1
        _log(f"mutation: baseline green ({tail}) in "
             f"{time.monotonic() - started:.1f}s")

        candidates: list[tuple[str, Rewrite, str]] = []
        for relative in production:
            lines = touched_lines(repo, base, relative)
            if not lines:
                continue
            for rewrite, mutated in mutants_for_file(tree, relative, base, lines):
                candidates.append((relative, rewrite, mutated))
        # deterministic order, so two runs of the same commit are the same gate
        candidates.sort(key=lambda row: (row[0], row[1].lineno, row[1].rule))
        if not candidates:
            _log("mutation: no load-bearing mutant site in the lines this "
                 "diff touched — nothing to measure")
            report["skipped"] = "no mutant site in the touched lines"
            return nothing_to_measure

        available = len(candidates)
        chosen = candidates[:args.cap]
        _log(f"mutation: {available} mutant site(s) in the touched lines, "
             f"running {len(chosen)} (cap {args.cap})"
             + ("" if available == len(chosen)
                else f" — {available - len(chosen)} NOT RUN"))
        if available > len(chosen):
            _log("mutation: the cap is a budget, not a sample to boast about: "
                 "raise --max to widen it")

        originals = {rel: (tree / rel).read_bytes() for rel in production
                     if (tree / rel).exists()}
        before = {rel: digest(tree / rel) for rel in originals}
        caught = survived = elsewhere = unknown = 0
        escalate = sorted(set(phase_two) - set(phase_one))
        wide_checked = False
        try:
            for relative, rewrite, mutated in chosen:
                target = tree / relative
                target.write_text(mutated, encoding="utf-8")
                code, tail = run_tests(tree, phase_one, args.timeout)
                phase_one_verdict = shipped_verdict(code)
                if phase_one_verdict == "unknown":
                    verdict, note = "unknown", "timed out"
                    unknown += 1
                elif phase_one_verdict == "caught":
                    verdict, note = "caught", "the tests this commit shipped"
                    caught += 1
                elif not escalate:
                    verdict, note = "SURVIVED", "no pre-existing test was run"
                    survived += 1
                else:
                    # A survivor of the shipped tests is not yet a hole: the
                    # module may already have been held by tests the commit did
                    # not touch. That question costs a second, wider run, so it
                    # is asked once, lazily, and only when there is a survivor.
                    if not wide_checked:
                        _log(f"mutation: a mutant survived the shipped tests — "
                             f"checking the {len(escalate)} pre-existing "
                             "holder(s) once, to tell a hole from a "
                             "decorative test")
                        wide_checked = True
                    code2, tail2 = run_tests(tree, phase_two, args.timeout)
                    verdict = preexisting_verdict(code2)
                    if verdict == "unknown":
                        note = "timed out in the wider set"
                        unknown += 1
                    elif verdict == "HELD-ELSEWHERE":
                        note = "held by a test this commit did not touch"
                        elsewhere += 1
                    else:
                        note = "no test in the project notices this"
                        survived += 1
                report["mutants"].append(
                    {"file": relative, "rule": rewrite.rule,
                     "line": rewrite.lineno, "verdict": verdict, "detail": tail,
                     "note": note})
                _log(f"  {verdict:14} {relative}:{rewrite.lineno} "
                     f"({rewrite.rule}) — {note}")
        finally:
            for relative, data in originals.items():
                (tree / relative).write_bytes(data)
            for relative in originals:
                now = digest(tree / relative)
                if now != before[relative]:
                    _log(f"mutation: RESTORE FAILED for {relative} — the "
                         "worktree is discarded below whatever the verdict, but "
                         "say it rather than let it pass silently")

        total = len(chosen)
        _log("")
        _log(f"mutation: {caught} caught by the shipped tests, {elsewhere} "
             f"held only by pre-existing tests, {survived} survived, "
             f"{unknown} unknown, of {total} run ({available} available)")
        if unknown:
            _log("mutation: an unknown is a run that timed out — counted as "
                 "neither caught nor survived, because a timeout is not a "
                 "test noticing anything")
        if any(row["verdict"] in FAILING for row in report["mutants"]):
            _log("")
            if survived:
                _log("mutation: FAIL — a mutant of the code this change "
                     "touched passed every test in the project, so if that "
                     "code were wrong nothing would notice:")
            else:
                _log("mutation: FAIL — a mutant of the code this change "
                     "touched passed the tests the change shipped. Not a "
                     "coverage hole: a test the commit did not touch already "
                     "held it. The new test is decorative on that line.")
            for row in report["mutants"]:
                if row["verdict"] in ("SURVIVED", "HELD-ELSEWHERE"):
                    _log(f"    {row['verdict']:14} {row['file']}:{row['line']} "
                         f"({row['rule']}) — {row['note']}")
            _log("")
            _log(f"mutation: {PASS_CRITERION}.")
            _write_report(args.report, report)
            return 1
        if (available - total) and caught == 0:
            # The same reason the run REFUSED, spelled the same way, because a
            # skip the reader cannot see is the failure this whole script
            # exists to avoid. The reason goes in the report AND out loud: the
            # report is only written when `--report` asked for it, and a reason
            # that exists only inside a file nobody opened has told nobody.
            report["skipped"] = (
                f"no mutant was run: {available} site(s) were available and "
                f"{total} were run, so nothing was measured")
            _log(f"mutation: SKIP — {report['skipped']}. A cap of zero mutants "
                 "is not a pass.")
            _write_report(args.report, report)
            return 2
        _log("mutation: PASS — every mutant of the touched lines was caught "
             "by the tests this change shipped")
        _write_report(args.report, report)
        return 0
    finally:
        tree_ctx.__exit__(None, None, None)


def _write_report(path: str | None, report: dict) -> None:
    if not path:
        return
    pathlib.Path(path).write_text(json.dumps(report, indent=2,
                                             sort_keys=True) + "\n",
                                   encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
