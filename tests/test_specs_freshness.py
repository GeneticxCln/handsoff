"""The specs are a claim about this checkout; this is what holds them to it.

`specs/00-overview.md` sets its own rule — "when code and spec disagree, the
code runs and the spec is wrong — fix the spec in the same change" — and a rule
with nothing behind it is how a spec's numbers rot. This file counts the things
the specs state as numbers, from the source of truth each spec names for
ITSELF, and reads every place a spec states one back out.

What is pinned, and what deliberately is not:

  * PINNED — the structural counts (tools, settings keys, permission keys, PTT
    verbs, core modules). A spec that is wrong about one of these misleads
    about what exists, and a reader has no way to tell it is wrong.
  * NOT PINNED — line counts and per-file test counts. A size is a dated
    snapshot ("Scale (measured ...)"), and pinning one makes every edit a
    two-file change for no contract gained; when a snapshot is old, its own
    date says so. Structure is what gets checked instead: every core module
    named in the architecture map, every test file in the test plan, every spec
    in the index. Those are the lists a rename or an addition must not slip
    past, and they cannot be dated away.

A pattern that stops matching is a FAILURE, not a skip: a reworded spec has to
be reworded together with the guard that reads it, or the guard goes quietly
vacuous — the defect this whole file exists to refuse.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from conftest import _load as _load_module, sandbox_env

HERE = Path(__file__).resolve().parent.parent
SPECS = HERE / "specs"


def _generator():
    """`ci/spec_tables.py`, through the suite's one loader (sandboxed home)."""
    return _load_module("spec_tables", HERE / "ci" / "spec_tables.py")

# (spec file, pattern) for each count, taken from the sentence the spec states it
# in. Only the number is captured.
CLAIMS = {
    "tools": (
        ("00-overview.md", r"(\d+) model-callable tools"),
        ("00-overview.md", r"\| (\d+)-tool census"),
        ("10-requirements.md", r"## 3\. Tools \((\d+),"),
        ("20-architecture.md", r"\| (\d+) `@tool`s,"),
        ("30-tools-api.md", r"\*\*(\d+) `@tool` methods\*\*"),
        ("60-test-plan.md", r"(\d+)-tool census"),
        ("90-audit.md", r"AST census = (\d+)"),
        ("90-audit.md", r"(\d+) tools,"),
    ),
    "settings_keys": (
        ("00-overview.md", r"\| (\d+) settings keys"),
        ("10-requirements.md", r"\((\d+) keys, `DEFAULT_SETTINGS`"),
        ("20-architecture.md", r"\| (\d+) defaults,"),
        ("40-data.md", r"settings\.json \((\d+) keys"),
        ("40-data.md", r"the (\d+) keys above"),
        ("90-audit.md", r"(\d+)\s+settings keys"),
    ),
    "permissions": (
        ("10-requirements.md", r"Permission model \((\d+) keys"),
        ("40-data.md", r"`permissions` \((\d+), see"),
    ),
    "ptt_verbs": (
        ("50-ops.md", r"Control socket \((\d+) verbs\)"),
        ("90-audit.md", r"(\d+) PTT verbs"),
    ),
    "core_modules": (
        ("00-overview.md", r"core/`: (\d+) modules"),
        ("20-architecture.md", r"CORE_REQUIRED` \((\d+) names today\)"),
        ("90-audit.md", r"ships (\d+) core modules"),
    ),
}


def _spec(name: str) -> str:
    return (SPECS / name).read_text(encoding="utf-8")


def _tool_count() -> int:
    """AST census of core/tools.py, the source `30-tools-api.md` names."""
    tree = ast.parse((HERE / "core" / "tools.py").read_text(encoding="utf-8"))
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if getattr(target, "id", "") == "tool" or getattr(target, "attr", "") == "tool":
                found += 1
                break
    return found


def _module_literal(path: Path, name: str) -> ast.AST:
    """The Set/Dict literal assigned to a module-level name, parsed not imported.

    `handsoff.py` is the whole app: importing it to count 22 strings would drag
    Qt, the settings loader and the core package into a guard about a text file.
    """
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        targets = getattr(node, "targets", [])
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == name for t in targets):
            return node.value
    raise AssertionError(f"{name} is no longer assigned at module level in {path.name}")


def _counts() -> dict:
    import settings_schema  # Qt-free by design: the settings app loads it alone

    defaults = settings_schema.DEFAULT_SETTINGS
    verbs = _module_literal(HERE / "handsoff.py", "PTT_ACTIONS")
    return {
        "tools": _tool_count(),
        "settings_keys": len(defaults),
        "permissions": len(defaults["permissions"]),
        "ptt_verbs": len(verbs.elts),
        "core_modules": len(list((HERE / "core").glob("*.py"))),
    }


LIVE = _counts()


def _assert_every_statement_matches(quantity: str) -> None:
    live = LIVE[quantity]
    for spec, pattern in CLAIMS[quantity]:
        text = _spec(spec)
        found = re.findall(pattern, text)
        assert found, (
            f"{spec} no longer states the {quantity} count in the form this "
            f"guard reads ({pattern!r}). Either the sentence moved or it was "
            f"reworded: update specs/{spec} and this pattern TOGETHER, so the "
            f"claim stays checked instead of going quietly unchecked")
        for stated in found:
            assert int(stated) == live, (
                f"{spec} says {stated} {quantity} — the code has {live}. "
                f"The spec is wrong (specs/00-overview.md: the code runs)")


class TestSpecFreshness:
    def test_the_tool_census_matches_every_place_a_spec_states_it(self):
        _assert_every_statement_matches("tools")

    def test_the_settings_key_census_matches_every_place_a_spec_states_it(self):
        _assert_every_statement_matches("settings_keys")

    def test_the_permission_key_census_matches_every_place_a_spec_states_it(self):
        _assert_every_statement_matches("permissions")

    def test_the_ptt_verb_census_matches_every_place_a_spec_states_it(self):
        _assert_every_statement_matches("ptt_verbs")

    def test_the_core_module_census_matches_every_place_a_spec_states_it(self):
        _assert_every_statement_matches("core_modules")

    def test_the_architecture_map_lists_exactly_the_core_package(self):
        """Both directions: a new module must appear, a deleted one must go.

        This is the drift the spec's own maintenance rule names, and the one a
        count alone cannot see — 13 modules stays true while `core/theme.py`
        is swapped for a different one.
        """
        text = _spec("20-architecture.md")
        on_disk = sorted(p.name for p in (HERE / "core").glob("*.py"))
        listed = sorted(set(re.findall(r"\| `core/([a-z_]+\.py)` \|", text)))
        missing = [name for name in on_disk if name not in listed]
        assert not missing, (
            f"core/{missing} is on disk and not in the architecture map — a "
            f"module nobody declared is a module nobody knows is there")
        stale = [name for name in listed if name not in on_disk]
        assert not stale, f"the architecture map lists core/{stale}, which is gone"

    def test_the_test_plan_lists_every_test_file(self):
        """The inventory's COUNTS are a dated snapshot; its FILE LIST is not."""
        text = _spec("60-test-plan.md")
        on_disk = sorted(p.name for p in (HERE / "tests").glob("test_*.py"))
        missing = [name for name in on_disk if f"`{name}`" not in text]
        assert not missing, (
            f"tests/{missing} exists and the test plan does not name it — the "
            f"plan is how someone finds the guard they are about to weaken")
        listed = set(re.findall(r"\| `(test_[a-z_]+\.py)` \|", text))
        stale = sorted(name for name in listed if name not in on_disk)
        assert not stale, f"the test plan names tests/{stale}, which is gone"

    def test_the_index_names_only_specs_that_exist(self):
        index = _spec("00-overview.md")
        named = sorted(set(re.findall(r"`(\d\d-[a-z-]+\.md)`", index)))
        assert named, ("the index no longer names its specs in backticks, which "
                       "is the form this guard reads")
        missing = [name for name in named if not (SPECS / name).exists()]
        assert not missing, f"the index lists {missing}, which do not exist"
        on_disk = sorted(p.name for p in SPECS.glob("*.md"))
        unlisted = [name for name in on_disk if name not in named]
        assert not unlisted, (
            f"specs/{unlisted} is not in the index — a spec nobody links is a "
            f"spec nobody reads")

    def test_the_tables_are_what_the_generator_produces(self):
        """The two count tables cannot drift, because they are not written by hand.

        Both were hand-maintained and both were wrong: the architecture table
        priced `settings_schema.py` at 319 lines when it was 974, and the tool
        census was off by one on a tool whose decorator grew a line. The
        generator reads the module set from the installer's own declaration and
        the tools from the decorators that define them, so this test is the
        thing that keeps the spec honest — `python3 ci/spec_tables.py --write`
        is how a person updates it, deliberately, in the same commit.
        """
        spec_tables = _generator()
        stale = spec_tables.pending()
        assert not stale, (
            "the spec's generated tables are not what the tree says: "
            + ", ".join(f"{path.name} ({what})" for path, what, _o, _n in stale)
            + " — run: python3 ci/spec_tables.py --write")
        owed = spec_tables.undescribed()
        assert not owed, (
            "a module arrived with no sentence about it (what it owns, what it "
            f"must not import): {owed}")

    def test_the_generated_tables_agree_with_the_census(self):
        """Two readers of the same fact must not disagree (tools, at least).

        The architecture table is priced in lines, so it says nothing about the
        tool count; the census does, and the generator derives it from the same
        AST this file counts. If they ever diverge, one of them is reading the
        file wrong — which is the failure a generated table is supposed to end.
        """
        spec_tables = _generator()
        assert len(spec_tables.tool_rows()) == LIVE["tools"]
        labels = [label for label, _path in spec_tables.modules()]
        assert labels.count("core/__init__.py") == 1, labels
        assert len([name for name in labels if name.startswith("core/")]) == LIVE["core_modules"]

    def test_the_generator_exits_zero_on_a_current_tree(self):
        """The gates call it as a COMMAND; in-process checks do not prove that.

        `--check` is the form `ci/gates.sh` and a reviewer run, so its exit
        status is the whole contract: a script that raises while importing, or
        that prints a complaint and still exits 0, would read as clean in both
        places while the tables rot.
        """
        # sandbox_env, not os.environ: a child started by hand resolves the
        # developer's CONFIG_DIR/STATE_DIR (tests/test_sandbox.py pins that rule).
        proc = subprocess.run(
            [sys.executable, str(HERE / "ci" / "spec_tables.py")],
            capture_output=True, text=True, cwd=str(HERE),
            env=sandbox_env(), timeout=120)
        assert proc.returncode == 0, (
            f"ci/spec_tables.py exited {proc.returncode}:\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert "STALE" not in proc.stdout, proc.stdout

    def test_the_census_is_not_vacuous(self):
        """Every quantity in the table was really read, and from a real file."""
        for quantity, claims in CLAIMS.items():
            assert claims, f"{quantity} has no claims — nothing is being checked"
            for spec, _pattern in claims:
                assert (SPECS / spec).exists(), f"{spec} is named and missing"
            assert LIVE[quantity] > 0, quantity
        # floors: a reworded spec cannot silently thin the table out, because
        # the table shrinking is itself the failure this test refuses.
        assert len(CLAIMS["tools"]) >= 8
        assert len(CLAIMS["settings_keys"]) >= 6
        assert len(CLAIMS["core_modules"]) >= 3
