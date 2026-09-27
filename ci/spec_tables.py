"""The spec's two count tables, generated from the tree.

`specs/20-architecture.md` §1 prices every module in lines, and
`specs/30-tools-api.md` lists every tool with its gate and its line. Both are
counts OF the code and both were hand-maintained — the same defect the
installer's file list had before it was derived (a module shipped nowhere while
doctor still said in-sync). A number nobody recomputes is a number that is
wrong; these are recomputed here.

    python3 ci/spec_tables.py            # check: exit 1, with what differs
    python3 ci/spec_tables.py --write    # rewrite the tables in place

What is generated, and what is deliberately not: the module set (from the
installer's own declaration, so a module that ships is a module that appears),
the line counts, the tool names, their effective gates, their line numbers, and
the tool count. The PROSE is not derivable — what a module owns and what it must
not import are statements a person makes — so a row's prose is preserved, and a
module that arrives without a row gets one with `TODO` in both cells while
`--check` keeps failing. The row appears; the sentence about it is still owed.
"""
from __future__ import annotations

import ast
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = ROOT / "specs" / "20-architecture.md"
TOOLS_API = ROOT / "specs" / "30-tools-api.md"
INSTALL = ROOT / "install.sh"

ARCH_HEADER = "| Module | Lines | Owns | Must not import |"
TOOLS_HEADER = "| Tool | Gate | L |"
TODO = "TODO"


def _install_var(name: str) -> list:
    """The words of `NAME="a b c"` in install.sh — the installer's own list.

    A declaration this parser can no longer find is a REFUSAL, not an empty
    list: the module table is generated from these names, so a renamed or
    requoted variable would silently shrink it — and a freshly written spec
    would then agree with itself while missing modules.
    """
    match = re.search(rf'^{name}="([^"]*)"', INSTALL.read_text(encoding="utf-8"),
                      re.M)
    if match is None:
        raise SystemExit(
            f"install.sh does not declare {name}=\"…\" in the form this "
            f"generator reads — fix the declaration and the generator together")
    return match.group(1).split()


def modules() -> list:
    """[(label, path)] the architecture table prices, in table order.

    The top-level set is the installer's (declared always-ship + declared
    executables, minus the shell script), because that is the same definition
    the deployment compares against; `core/*.py` is discovered, so a new module
    is priced the moment it exists.
    """
    labels = []
    for name in _install_var("TOP_REQUIRED") + _install_var("TOP_EXECUTABLE"):
        if name.endswith(".py") and name not in labels:
            labels.append(name)
    rows = [(name, ROOT / name) for name in labels]
    rows += [(f"core/{path.name}", path) for path in sorted((ROOT / "core").glob("*.py"))]
    return [(label, path) for label, path in rows if path.exists()]


def module_lines() -> dict:
    return {label: len(path.read_text(encoding="utf-8").splitlines())
            for label, path in modules()}


def _tool_decorator(node: ast.FunctionDef):
    """The `@tool(...)` decorator of a method, in either form, or None."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if getattr(target, "id", "") == "tool" or getattr(target, "attr", "") == "tool":
            return decorator
    return None


def tool_rows() -> list:
    """[(name, effective gate, def line)] in file order, the decorator's own rule.

    `tool()` records `_tool_name = name or function name` and
    `_tool_gates = gates if gates is not None else _tool_name`, so the gate is
    read the same way here: an omitted `gates=` means the tool's own name, and
    `gates=''` is the only ungated form (rendered `—`, as the table always has).
    """
    tree = ast.parse((ROOT / "core" / "tools.py").read_text(encoding="utf-8"))
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        decorator = _tool_decorator(node)
        if decorator is None:
            continue
        keywords = {}
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if isinstance(keyword.value, ast.Constant):
                    keywords[keyword.arg] = keyword.value.value
        name = keywords.get("name") or node.name
        gates = keywords.get("gates", None)
        gate = "" if gates == "" else (gates or name)
        rows.append((name, gate, node.lineno))
    rows.sort(key=lambda row: row[2])
    return rows


def _table_body(text: str, header: str) -> list:
    """The parsed body rows of the markdown table whose header line is `header`."""
    lines = text.splitlines()
    try:
        start = lines.index(header)
    except ValueError as e:
        # `from e`: the ValueError names the list that was searched, and a
        # generator failure with no cause is a failure that reads like the
        # spec is simply worded differently.
        raise SystemExit(f"the table `{header}` is gone from the spec — this "
                         f"generator edits it, so reword both together") from e
    rows = []
    for line in lines[start + 2:]:          # header + separator
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows.append(cells)
    return rows


def _render_table(text: str, header: str, body: list) -> str:
    lines = text.splitlines()
    start = lines.index(header)
    end = start + 2
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    rendered = [f"| {' | '.join(cells)} |" for cells in body]
    return "\n".join(lines[:start + 2] + rendered + lines[end:])


def render_architecture(text: str) -> str:
    live = module_lines()
    body, seen = [], set()
    for cells in _table_body(text, ARCH_HEADER):
        label = cells[0].strip("`")
        if label not in live or label in seen:
            continue
        seen.add(label)
        owns = cells[2] if len(cells) > 2 else TODO
        must_not = cells[3] if len(cells) > 3 else TODO
        body.append([f"`{label}`", str(live[label]), owns, must_not])
    for label in live:
        if label not in seen:
            body.append([f"`{label}`", str(live[label]),
                         f"{TODO} — what it owns",
                         f"{TODO} — what it must not import"])
    return _render_table(text, ARCH_HEADER, body)


def render_tools(text: str) -> str:
    rows = tool_rows()
    body = [[f"`{name}`", f"`{gate}`" if gate else "—", str(line)]
            for name, gate, line in rows]
    text, n = re.subn(r"\*\*\d+ `@tool` methods\*\*",
                      f"**{len(rows)} `@tool` methods**", text, count=1)
    if not n:
        raise SystemExit(
            "the tool-count prose is gone from specs/30-tools-api.md (this "
            "generator rewrites it) — reword both together, or the count it "
            "states goes stale with nothing failing")
    return _render_table(text, TOOLS_HEADER, body)


TARGETS = (
    (ARCHITECTURE, render_architecture, ARCH_HEADER, "architecture module table"),
    (TOOLS_API, render_tools, TOOLS_HEADER, "tool census"),
)


def pending() -> list:
    """[(path, what, old_text, new_text)] for every spec that is not what the tree says."""
    out = []
    for path, render, _header, what in TARGETS:
        text = path.read_text(encoding="utf-8")
        new = render(text)
        if new != text:
            out.append((path, what, text, new))
    return out


def undescribed() -> list:
    """[(path, row)] for a row generated with no sentence about it yet."""
    out = []
    for path, _render, header, _what in TARGETS:
        for cells in _table_body(path.read_text(encoding="utf-8"), header):
            owed = [cell for cell in cells[2:] if cell.startswith(TODO)]
            if owed:
                out.append((path, cells[0]))
    return sorted(set(out))


def main(argv: list) -> int:
    write = "--write" in argv
    stale = pending()
    if write:
        for path, what, _old, new in stale:
            path.write_text(new, encoding="utf-8")
            print(f"rewrote {path.relative_to(ROOT)} ({what})")
        if not stale:
            print("both tables already match the tree")
        # Re-read: the write is supposed to BE the fix, so a non-zero exit after
        # one means the rewrite did not stick (or a row is still undescribed).
        # Exiting 1 on a successful write made the tool report failure for the
        # only thing it is for — found by the sweep that mutated a module.
        stale = pending()
    elif stale:
        for path, what, old, new in stale:
            print(f"STALE: {path.relative_to(ROOT)} — the {what} is not what "
                  f"the tree says:")
            diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                        lineterm="", n=0)
            shown = [line for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---")]
            for line in shown[:30]:
                print("   ", line)
            if len(shown) > 30:
                print(f"    … and {len(shown) - 30} more")
        print("\nrun: python3 ci/spec_tables.py --write")

    owed = undescribed()
    for path, row in owed:
        print(f"{path.relative_to(ROOT)}: {row} has no description — a module "
              f"arrived and nobody said what it owns")
    return 1 if (stale or owed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
