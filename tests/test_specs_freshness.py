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
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SPECS = HERE / "specs"

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
