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
import shutil
import subprocess
import sys
import tempfile
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


def _generated_lines(headers) -> set:
    """(file, 1-based line) of every line inside a generated table.

    The generator owns these lines, so they are the only place a size may
    appear; everything else in the specs is prose a person maintains.
    """
    out = set()
    for name, header in headers:
        lines = (SPECS / name).read_text(encoding="utf-8").splitlines()
        try:
            start = lines.index(header)
        except ValueError:
            raise AssertionError(
                f"the generated table `{header}` is gone from specs/{name} — "
                f"the generator edits it and this guard skips it, so the two "
                f"have to be reworded together")
        row = start + 1                      # header + separator (0-based)
        while row <= len(lines) and (row <= start + 2
                                     or lines[row - 1].startswith("|")):
            out.add((name, row))
            row += 1
    return out


DEBT_HEADER = "| Module | Names still reached | Why this is debt, not a seam |"


def _map_rows() -> list:
    """[(module, owns, must-not-import)] from the architecture map's table.

    Read through the generator's own table parser, so the map has one reader of
    its shape: the module set in these rows is the set it prices and the set the
    deployment ships.
    """
    spec_tables = _generator()
    rows = []
    for cells in spec_tables._table_body(_spec("20-architecture.md"),
                                         spec_tables.ARCH_HEADER):
        label = cells[0].strip("`")
        rows.append((label, cells[2].strip() if len(cells) > 2 else "",
                     cells[3].strip() if len(cells) > 3 else ""))
    assert len(rows) >= 15, (
        f"the map now has {len(rows)} rows — it had 17, so this guard has "
        f"stopped reading the table it checks")
    return rows


def _mentions(path: Path) -> tuple:
    """(identifiers, string text) the module's own AST contains.

    Deliberately loose: an ownership cell claims a name, and the question is
    whether that name is still *somewhere* in the module — as a def, a class, an
    attribute, a parameter, or inside a string literal (which is how a CLI flag
    like `--preflight` is claimed).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    identifiers = set()
    strings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef,
                               ast.AsyncFunctionDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.alias):
            identifiers.add((node.asname or node.name).split(".")[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    return identifiers, "\n".join(strings)


def _largest_public_class(path: Path):
    """(name, lines) of the biggest non-private top-level class, or None."""
    body = ast.parse(path.read_text(encoding="utf-8")).body
    classes = [n for n in body
               if isinstance(n, ast.ClassDef) and not n.name.startswith("_")]
    if not classes:
        return None
    biggest = max(classes, key=lambda c: c.end_lineno - c.lineno)
    return biggest.name, biggest.end_lineno - biggest.lineno


def _module_aliases(tree) -> dict:
    """{local name -> module label} for the handles a module holds.

    Three ways this tree binds one, and all three were found by looking: an
    import, a literal-argument load (`_audio = _load_module("audio")`), and the
    settings module's own loader (`_core_settings = _load_core_package()`) —
    settings is loaded before the core package exists, so it cannot use the
    shared one. The last case has no literal to read, so it uses the naming
    convention the architecture map already states: the host's handles are
    `_core_<module>`.
    """
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("core"):
                    found[a.asname or a.name.split(".")[-1]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if node.module.startswith("core"):
                    found[a.asname or a.name] = f"{node.module}.{a.name}"
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            helper = getattr(fn, "id", "") or getattr(fn, "attr", "")
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            arg = node.value.args[0] if node.value.args else None
            if helper in ("load_module", "_load_module", "load_core_package") \
                    and isinstance(arg, ast.Constant) \
                    and isinstance(arg.value, str):
                for target in targets:
                    found[target] = "core." + arg.value
            elif helper == "_load_core_package":
                for target in targets:
                    if target.startswith("_core_"):
                        found[target] = "core." + target[len("_core_"):]
    return found


def _shipped_trees() -> tuple:
    """(labels, {label: AST}) for the modules the deployment ships."""
    labels = [label for label, _path in _generator().modules()]
    return labels, {label: ast.parse((HERE / label).read_text(encoding="utf-8"))
                    for label in labels}


def _top_level_names(tree) -> set:
    """Names assigned or defined at MODULE level (not inside a class or def)."""
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        for target in getattr(node, "targets", []):
            if isinstance(target, ast.Name):
                out.add(target.id)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def _private_crossings() -> dict:
    """{module: {private name crossed from outside: {files}}} for shipped modules.

    Three forms are read, because the tree uses three, and each was found by
    looking rather than by guessing:

      * `alias._name`, alias from an import or from `load_module("x")` — how the
        host reaches `_audio._tts_model`;
      * `from core.x import _name` — how the settings app reaches one;
      * `_core_module("settings")._name` — a module looked up BY NAME, which is
        still a real crossing.

    Deliberately NOT counted: an attribute on any other call (`_dep()._x`).
    That is the injected dependency object — the HOST's own namespace — not
    another module's, and attributing those by which module happens to define
    the name reported twelve false positives out of `core/tools.py` alone
    (`_dep()._geocode`, `_dep()._mpc`, …).

    Every form above names the target explicitly, so a local name of the same
    name does NOT excuse it: the host defines its own `_tts_model` mirror AND
    assigns into `core.audio`'s copy, and the second one is the crossing. A name
    defined at module level in the accessing file is only excluded for the
    UNQUALIFIED forms, and there are none left. Dunders are never crossings.
    Tests are out of scope: this is about the shipped modules, the same set the
    map prices.
    """
    labels, trees = _shipped_trees()
    by_key = {}
    for label in labels:
        by_key[label] = label
        by_key[Path(label).stem] = label
        by_key[f"core.{Path(label).stem}"] = label
    crossings = {}
    for user in labels:
        aliases = _module_aliases(trees[user])
        for node in ast.walk(trees[user]):
            name, owner = None, None
            if isinstance(node, ast.Attribute) and node.attr.startswith("_") \
                    and not node.attr.startswith("__"):
                name = node.attr
                base = node.value
                if isinstance(base, ast.Name):
                    owner = by_key.get(aliases.get(base.id, ""))
                elif isinstance(base, ast.Call):
                    helper = getattr(base.func, "id", "") or getattr(
                        base.func, "attr", "")
                    if helper in ("_core_module", "load_module", "_load_module") \
                            and base.args \
                            and isinstance(base.args[0], ast.Constant) \
                            and isinstance(base.args[0].value, str):
                        looked_up = base.args[0].value
                        owner = by_key.get(looked_up) or by_key.get(f"core.{looked_up}")
            elif isinstance(node, ast.ImportFrom) and node.module \
                    and node.module.startswith("core"):
                for a in node.names:
                    if a.name.startswith("_") and not a.name.startswith("__"):
                        target = by_key.get(node.module)
                        if target and target != user:
                            crossings.setdefault(target, {}).setdefault(
                                a.name, set()).add(user)
                continue
            if not name or not owner or owner == user:
                continue
            crossings.setdefault(owner, {}).setdefault(name, set()).add(user)
    return crossings


def _qualified_seams() -> dict:
    """{module: {name: {files that reach it by module-qualified access}}}.

    `web.search`, `_audio.play_wav`, `_core.bubble.install_pack` — a real
    dependency. Bare names and attribute access on the injected facade are NOT
    counted: a core module reaching for the app's globals says nothing about
    what that module exposes, and counting it made every host global look like a
    seam of every module.
    """
    shipped, trees = _shipped_trees()
    by_key = {}
    for label in shipped:
        by_key[label] = label
        by_key[Path(label).stem] = label
        by_key[f"core.{Path(label).stem}"] = label

    seams = {}
    for user, tree in trees.items():
        found = _module_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if not isinstance(base, ast.Name) or base.id == "H":
                continue
            owner = by_key.get(found.get(base.id, ""))
            if owner and owner != user:
                seams.setdefault(owner, {}).setdefault(node.attr, set()).add(user)
    return seams


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

    def test_the_map_names_only_symbols_that_still_exist(self):
        """An ownership sentence naming a symbol the module no longer has.

        The map is how someone finds the seam they are about to change, so a
        name that was renamed or cut is a pointer into nothing — and it is the
        one part of the table no generator can produce: what a module owns is a
        sentence a person writes. Checked against the module's own AST, so the
        row has to keep saying true things.
        """
        problems = []
        for label, owns, _must_not in _map_rows():
            identifiers, strings = _mentions(HERE / label)
            for chunk in re.findall(r"`([^`]+)`", owns):
                parts = [word for word in re.split(r"[^A-Za-z0-9_]+", chunk) if word]
                if not parts:
                    continue
                if not any(word in identifiers or word in strings for word in parts):
                    problems.append(
                        f"{label}: the map names `{chunk}` and none of {parts} is "
                        f"in the module")
        assert not problems, (
            "an ownership sentence names something that is no longer there:\n  "
            + "\n  ".join(problems))

    def test_the_map_names_each_modules_biggest_job(self):
        """A module that gained a job must stop describing only its old one.

        Two calibrated rules, rather than "name every public symbol" — that
        would make a cell a second copy of `__all__` and turn every new function
        into a two-file change:

          * the LARGEST public class in the module is named. One name per
            module, and by construction the biggest thing it does, so the day a
            new subsystem outgrows the old one the row has to say so.
          * a public name that TWO or more other modules reach by
            module-qualified access is an interface, not an internal.

        Not covered, and stated rather than implied: a job that exactly one
        other module reaches. The map is also checked in the other direction — a
        module arriving at all is the generator's `TODO` row.
        """
        seams = _qualified_seams()
        rows = _map_rows()
        assert seams, "no module-qualified access found anywhere — nothing read"
        problems = []
        for label, owns, must_not in rows:
            cell = f"{owns} {must_not}"
            biggest = _largest_public_class(HERE / label)
            if biggest and not re.search(rf"\b{re.escape(biggest[0])}\b", cell):
                problems.append(
                    f"{label}: its largest class `{biggest[0]}` ({biggest[1]} lines) "
                    f"is not named in the map")
            shared = {name: users for name, users in seams.get(label, {}).items()
                      if len(users) >= 2 and not name.startswith("_")}
            for name, users in sorted(shared.items()):
                if not re.search(rf"\b{re.escape(name)}\b", cell):
                    problems.append(
                        f"{label}: `{name}` is reached by {len(users)} modules "
                        f"({', '.join(sorted(users))}) and the map never names it")
        assert not problems, (
            "the map describes a module in terms it has outgrown:\n  "
            + "\n  ".join(problems))
        # anti-vacuity: a module the rest of the tree reaches into must name at
        # least one of the names it is reached by, or the rule above could pass
        # on a row whose symbols have all been renamed out from under it.
        speaking = []
        for label, owns, must_not in rows:
            reached = seams.get(label, {})
            if reached and not any(
                    re.search(rf"\b{re.escape(name)}\b", f"{owns} {must_not}")
                    for name in reached):
                speaking.append(label)
        assert not speaking, (
            f"{speaking} are reached by other modules and the map names none of "
            f"those names — the row is describing something else")

    def test_the_must_not_import_column_is_a_checkable_claim(self):
        """The fourth column is a claim about imports, and imports are readable.

        Each cell is a prohibition (before the parenthesis) optionally followed
        by the mechanism that makes it true. Every prohibition must be a term
        this guard knows how to check — an unreadable term is a claim nothing
        checks, which is the failure mode this whole file exists to end — and
        every backticked mechanism symbol must exist in the module it qualifies
        (`configure()` in a row means the module still has one).
        """
        # Module names are kept DOTTED as well as top-level: `import core.bubble`
        # registers the top-level name `core`, so a check that only read the
        # first component could never see the import it exists to forbid. (The
        # sweep missed exactly that, which is why it is spelled out here.)
        def dotted_and_tops(text):
            dotted = set()
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Import):
                    dotted |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    dotted.add(node.module)
            return dotted, {name.split(".")[0] for name in dotted}

        def names_of(qualified):
            return lambda kind: any(
                m == f"core.{kind}" or m.endswith(f".{kind}") for m in qualified)

        predicates = {
            "—": lambda k: None,
            "handsoff": lambda k: ("imports the host module"
                                   if "handsoff" in k["tops"] else None),
            "app globals": lambda k: ("imports the host module"
                                      if "handsoff" in k["tops"] else None),
            "Qt": lambda k: ("imports Qt" if any(
                m.startswith(("PySide", "PyQt")) for m in k["dotted"]) else None),
            "Assistant": lambda k: ("names Assistant"
                                    if "Assistant" in k["names"] else None),
            "SETTINGS": lambda k: ("names SETTINGS"
                                   if "SETTINGS" in k["names"] else None),
            "audio": lambda k: ("imports core.audio"
                                if names_of(k["dotted"])("audio") else None),
            "bubble module": lambda k: ("imports the bubble module"
                                        if names_of(k["dotted"])("bubble") else None),
            "anything": lambda k: None,      # narrowed by the mechanism
        }
        stdlib = set(sys.stdlib_module_names)
        seen = []
        problems = []
        for label, _owns, must_not in _map_rows():
            prohibition = re.split(r"[(\n]", must_not)[0]
            terms = [t.strip().strip("`") for t in re.split(r"[/,]", prohibition)]
            terms = [t for t in terms if t]
            dotted, tops = dotted_and_tops(
                (HERE / label).read_text(encoding="utf-8"))
            identifiers, _strings = _mentions(HERE / label)
            known = {"dotted": dotted, "tops": tops, "names": identifiers}
            for term in terms or ["—"]:
                if term not in predicates:
                    problems.append(
                        f"{label}: the guard does not know how to check the "
                        f"prohibition `{term}` — teach it or reword the cell")
                    continue
                seen.append(term)
                complaint = predicates[term](known)
                if complaint:
                    problems.append(f"{label}: {complaint}, and its row forbids it")
            if "stdlib only" in must_not:
                outside = sorted(m for m in tops
                                 if m not in stdlib and m not in ("core",))
                if outside:
                    problems.append(
                        f"{label}: the row says stdlib only, and it imports "
                        f"{outside}")
            # mechanisms live INSIDE the parentheses; the prohibition in front
            # of them is backticked too, and it names the host on purpose.
            for chunk in re.findall(r"`([^`]+)`", " ".join(
                    re.findall(r"\(([^)]*)\)", must_not))):
                words = [w for w in re.split(r"[^A-Za-z0-9_]+", chunk) if w]
                if words and not any(w in identifiers for w in words):
                    problems.append(
                        f"{label}: the mechanism `{chunk}` names something the "
                        f"module does not have")
        assert not problems, "\n  ".join(problems)
        # anti-vacuity: the vocabulary above has to be used by the table, and
        # the checks that actually forbid something have to be exercised.
        assert len(set(seen)) >= 6, sorted(set(seen))
        assert "handsoff" in seen and "Qt" in seen, sorted(set(seen))

    def test_a_private_name_that_crosses_a_module_boundary_is_declared(self):
        """The other direction: an internal must not leak out of its module.

        A name with a leading underscore is the module saying "not my
        interface". When another shipped module reaches it anyway, one of the
        two has to give: either the name stops being private, or the module
        declares it — `core/audio.py.__all__` already declares three private
        names, so the convention exists, and nothing reads `__all__` at runtime,
        so declaring costs nothing and makes the seam readable.

        The two renames this pass made are the other resolution, and the honest
        one for a name that four modules use: `atomic_private_write` and
        `MIC_OPERATION_LOCK` were private names crossing boundaries, and lost
        the underscore. The four mirror names stayed private because the
        host<->audio mirror protocol is documented as ordering-sensitive, so
        they are declared instead: a coupling that exists is better stated than
        renamed.

        Second rule, and the reason it is here: a DECLARED private that is
        crossed must be named in the map's row for that module, so a blessed
        seam cannot hide in a list nobody opens.
        """
        crossings = _private_crossings()
        assert sum(len(names) for names in crossings.values()) >= 4, (
            f"only {crossings} — the detector has stopped finding the crossings "
            f"it is supposed to check")
        # The map carries a DECLARED-DEBT table, because eleven crossed names
        # could not be renamed in this pass: `_load_settings` alone has 37 sites
        # and `_DEFAULT_DEPS` 30, most of them monkeypatch string names in tests.
        # A debt row is a promise with an expiry, so both directions fail — a
        # crossing that is not listed, and a listed name that is no longer
        # crossed or has since been promoted.
        debt = {}
        text = _spec("20-architecture.md")
        if DEBT_HEADER in text:
            for cells in _generator()._table_body(text, DEBT_HEADER):
                for name in re.findall(r"`([^`]+)`", cells[1]):
                    debt[name] = cells[0].strip("`")
        declared_any = 0
        listed = set()
        problems = []
        for label, names in crossings.items():
            for name in names:
                if debt.get(name) == label:
                    listed.add(name)
        rows = dict((label, owns) for label, owns, _must_not in _map_rows())
        for label, names in sorted(crossings.items()):
            table = ast.parse((HERE / label).read_text(encoding="utf-8"))
            exported = set()
            for node in table.body:
                if not isinstance(node, ast.Assign):
                    continue
                if any(getattr(t, "id", "") == "__all__" for t in node.targets):
                    exported = {e.value for e in node.value.elts
                                if isinstance(e, ast.Constant)}
            for name, owner in debt.items():
                if owner == label and name in exported:
                    problems.append(
                        f"§1a lists {label}.{name} as debt and {label} now "
                        f"declares it in __all__ — the row is stale, delete it")
            for name, users in sorted(names.items()):
                where = f"{', '.join(sorted(users))} reaches {label}.{name}"
                if name in debt:
                    continue
                if name not in exported:
                    problems.append(
                        f"{where} and {label} does not declare it in __all__ — "
                        f"drop the underscore, declare the seam, or add a debt "
                        f"row to §1a saying why it cannot be renamed yet")
                    continue
                declared_any += 1
                if not re.search(rf"\b{re.escape(name)}\b", rows.get(label, "")):
                    problems.append(
                        f"{where}, {label} declares it in __all__, and its map "
                        f"row never names it")
        stale = sorted(name for name in debt if name not in listed)
        assert not stale, (
            f"§1a lists {stale} as debt and nothing reaches them any more — a "
            f"paid debt left in the table is how a transition becomes a parking "
            f"space: delete the row (and the ones beside it for that module)")
        assert declared_any, "no declared private seam was exercised — nothing read"
        assert not problems, (
            "a private name crosses a module boundary undeclared:\n  "
            + "\n  ".join(problems))

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

    def test_every_line_citation_points_at_a_line_that_exists(self):
        """A citation is `module.py:NNN`; the file and the line must still be there.

        What a citation MEANT cannot be checked — no guard reads intent — so
        this catches a rename, a cut, or a number left past the end of a module
        that shrank, and NOT the case of a citation that lands inside the file
        on the wrong line. That is the honest size of it, and it is stated
        because both citations corrected in this pass were the second kind: the
        pointer survived, the number had moved.
        """
        citations = []
        for path in sorted(SPECS.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                for target, at in re.findall(r"`?([\w/]+\.py)`?:(\d+)", line):
                    citations.append((f"{path.name}:{number}", target, int(at)))
        assert len(citations) >= 10, (
            f"only {len(citations)} citations were found — the specs used to "
            f"carry 17, so this guard has stopped reading them (a reworded "
            f"citation is a citation nothing checks)")
        dead = []
        for where, target, at in citations:
            module = HERE / target
            if not module.exists():
                dead.append(f"{where} cites {target}, which is gone")
                continue
            count = len(module.read_text(encoding="utf-8").splitlines())
            if at > count:
                dead.append(f"{where} cites {target}:{at}, which has {count} lines")
        assert not dead, "a spec cites a line that is not there:\n  " + "\n  ".join(dead)

    def test_no_spec_restates_a_module_size_outside_the_tables(self):
        """A size copied out of §1 starts lying the day the file it prices changes.

        Three of the numbers corrected when §1 became generated were exactly
        this: the audit restated four file sizes and two had gone wrong, and the
        test plan timed the suite at 2.5 min when it took 3.5. Keeping a size
        *in* the generated table is safe; copying it into a sentence is the
        defect — nobody recomputes a number in a sentence, which is how the
        installer's file list went wrong one level up.

        Line *citations* (`handsoff.py:6350`) are the useful form and are
        stripped first: they say where a thing is, which the table cannot, and
        two of them had also gone stale. A number under three digits beside a
        module name is not matched — `handsoff-settings.py` really is a 6-tab
        GUI, and that is a count of tabs, not of lines.
        """
        spec_tables = _generator()
        labels = sorted({label for label, _path in spec_tables.modules()})
        assert labels, "the generator prices no modules — nothing is being read"
        generated = _generated_lines({("20-architecture.md",
                                      spec_tables.ARCH_HEADER),
                                     ("30-tools-api.md",
                                      spec_tables.TOOLS_HEADER)})
        offenders = []
        for path in sorted(SPECS.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if (path.name, number) in generated:
                    continue
                text_ = re.sub(r"`?[\w/]+\.py`?:\d+", "", line)   # citations
                if not re.search(r"\d{3,}", text_):
                    continue
                named = [label for label in labels if label in text_]
                if named:
                    offenders.append(
                        f"{path.name}:{number} restates {named} beside a "
                        f"number — {line.strip()[:70]!r}")
        assert not offenders, (
            "a spec states a shipped module's size in prose:\n  "
            + "\n  ".join(offenders)
            + "\nspecs/20-architecture.md §1 is generated — point at it instead "
            "of copying out of it")

    def test_write_mode_leaves_the_gate_green(self):
        """`--write` is the fix, so it must exit 0 once it has written.

        It did not: `main()` judged the run on the staleness it found BEFORE
        rewriting, so a successful write still exited 1 — the tool reporting
        failure for the only thing it is for. A sweep that mutated a module's
        line count is what surfaced it. The check runs the real tool against a
        throwaway tree whose only wrong number is a line count, so nothing in
        the real specs is touched to test it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "ci").mkdir()
            (tree / "specs").mkdir()
            shutil.copy2(HERE / "ci" / "spec_tables.py", tree / "ci" / "spec_tables.py")
            for name in ("20-architecture.md", "30-tools-api.md"):
                shutil.copy2(SPECS / name, tree / "specs" / name)
            (tree / "core").symlink_to(HERE / "core")
            for name in ("install.sh", "handsoff.py", "settings_schema.py",
                         "handsoff-settings.py", "hardware.py"):
                (tree / name).symlink_to(HERE / name)
            # one wrong number, in the file the generator prices
            arch = tree / "specs" / "20-architecture.md"
            arch.write_text(re.sub(r"\| `handsoff\.py` \| \d+ \|",
                                   "| `handsoff.py` | 1 |",
                                   arch.read_text(encoding="utf-8")),
                            encoding="utf-8")
            stale = subprocess.run(
                [sys.executable, str(tree / "ci" / "spec_tables.py")],
                capture_output=True, text=True, cwd=str(tree), env=sandbox_env(),
                timeout=120)
            assert stale.returncode == 1, (
                f"a wrong line count did not fail the check: {stale.stdout}")
            wrote = subprocess.run(
                [sys.executable, str(tree / "ci" / "spec_tables.py"), "--write"],
                capture_output=True, text=True, cwd=str(tree), env=sandbox_env(),
                timeout=120)
            assert wrote.returncode == 0, (
                f"--write exited {wrote.returncode} after rewriting:\n"
                f"{wrote.stdout}\n{wrote.stderr}")
            assert "rewrote" in wrote.stdout, wrote.stdout
            after = subprocess.run(
                [sys.executable, str(tree / "ci" / "spec_tables.py")],
                capture_output=True, text=True, cwd=str(tree), env=sandbox_env(),
                timeout=120)
            assert after.returncode == 0, (
                f"the tree `--write` left is still stale:\n{after.stdout}")

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
