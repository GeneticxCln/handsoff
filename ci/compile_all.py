#!/usr/bin/env python3
"""Byte-compile every Python source in the tree — discovered, not listed.

Both workflows used to enumerate the files to compile, which meant two lists
to keep in sync and a silent hole whenever a module was added: that is exactly
how ``core/theme.py`` reached the checkout, was never installed, and was never
even syntax-checked by the installer (a hand-written copy list). Discovery has
no such hole — a new file anywhere below the root is compiled the moment it
lands.

``attic/`` is excluded on purpose: it is a provenance archive, not shipped code
or supported tooling (matching its exclusion from ``.coveragerc``). Hidden
directories are skipped so a stray ``.venv`` cannot decide the job.

py_compile never imports, so this needs no dependencies and catches syntax rot
in any file, including ones no test happens to import.

Usage: compile_all.py [root]   (default: the current directory)
"""
from __future__ import annotations

import pathlib
import py_compile
import sys

EXCLUDED_DIRS = frozenset({"attic"})


def discover(root: pathlib.Path) -> list[pathlib.Path]:
    """Every compilable source below `root`, sorted for a stable report."""
    found: list[pathlib.Path] = []
    for path in sorted(root.rglob("*.py")):
        try:
            parts = path.relative_to(root).parts
        except ValueError:            # pragma: no cover - defensive
            continue
        if EXCLUDED_DIRS.intersection(parts):
            continue
        if any(part.startswith(".") for part in parts):
            continue
        found.append(path)
    return found


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".")
    files = discover(root)
    if not files:
        # A gate that finds nothing is a broken gate, not a passing one.
        print(f"compile_all: no Python sources under {root} — refusing to "
              "report success", file=sys.stderr)
        return 1
    failed: list[pathlib.Path] = []
    for path in files:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"FAIL {path}: {exc}", file=sys.stderr)
            failed.append(path)
    print(f"byte-compiled {len(files)} files"
          + (f" — {len(failed)} FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
