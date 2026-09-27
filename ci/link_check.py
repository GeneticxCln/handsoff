#!/usr/bin/env python3
"""Verify that every markdown anchor link in the docs resolves to a real heading.

    python ci/link_check.py          # exit 1, with every dead link and where

WHY IT IS ITS OWN THING. A heading link is the one kind of link that fails
SILENTLY: rename a heading, and markdown does not complain, the reader clicks,
and nothing happens. 137 of the 138 anchor links in this repository are the two
generated ledgers' tables of contents, so a single renamed heading can strand a
quarter of the navigation in a file nobody re-reads. The guard that existed
compared `ci/doc_index.py` with its own output, which by construction cannot see
a link that resolves to nothing — it only asks whether the generator agrees with
itself. This walks the LINKS on the page and asks the question the reader's
browser asks.

THE SLUG IS NOT REIMPLEMENTED HERE. `ci/doc_index.py` holds github-slugger's
function, pinned against that project's own test fixtures in
`tests/test_specs_freshness.py::test_the_index_slugs_match_githubs_own_fixtures`.
A second copy would be a second opinion that can drift from the first, and
"which of the two is right" is not a question a docs gate should be raising.

WHAT COUNTS AS AN ANCHOR — deliberately wider than the index's, because this
asks what the RENDERER has, not what the generator publishes: every ATX heading
(`## Title`) and every setext heading (a non-blank line underlined with `===` or
`---`), numbered by the shared page-wide duplicate rule, plus `<a id="x">`,
which GitHub passes through as real HTML. Fenced code produces no headings and
its contents are not links, on either side.

TWO SHAPES ARE DELIBERATELY NOT BELIEVED, because inventing an anchor the
renderer never made is how a "link checker" starts passing lies:
`## Title {#custom}` is kramdown syntax that GitHub does not interpret — it
renders as literal text, so the anchor a browser would find is the SLUG OF THE
WHOLE STRING, and a link to `#custom` resolves to nothing. And a `#L12`
fragment is GitHub's code-file line anchor, which rendered markdown does not
have either, so it is reported dead: that is the truth about it.

No `--write`: the two ledgers are fixed by `ci/doc_index.py --write`, and a
link this finds anywhere else is a link a human wrote on purpose.
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import doc_index  # noqa: E402 - the shared, fixture-pinned slug

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Directories whose markdown is not the project's documentation. `attic` is a
#: graveyard of old trees, `.freebuff` and `.codex` are agent scratch: a dead
#: link in someone's scratch file is their business, not a red build.
SKIP_DIRS = {".git", "attic", ".freebuff", ".codex", "node_modules",
             "__pycache__", ".pytest_cache", ".ruff_cache", "venv", ".venv"}

_ATX = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
_EXPLICIT_ID = re.compile(r"<(?:a|span|div|section)\b[^>]*\bid=\"([^\"]+)\"", re.I)
_FENCE = re.compile(r"^\s*```")
#: An inline link, optionally angle-bracketed and optionally titled:
#: `[t](#a)`, `[t](<path with spaces> "title")`, `[t](path#frag 'title')`.
_LINK = re.compile(r"\]\(\s*<?([^)>]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")
#: A link definition, for reference-style links: `[label]: #anchor`.
_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^>\s]+)>?")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def markdown_files(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    """Every markdown file the documentation owns, in a stable order.

    `root` is a parameter so the guard test can point the checker at a fixture
    tree: a checker that can only read the real repository is a checker whose
    behaviour is only ever observed through one input, and one input is what a
    weakened checker looks like. See
    `tests/test_specs_freshness.py::test_the_link_checker_reports_exactly_the_dead_links_a_fixture_contains`.
    """
    out = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        out.append(rel)
    return out


def _fenced(lines: list[str]) -> list[bool]:
    """Which lines are inside a fenced code block (the ``` toggle flips it)."""
    inside = False
    flags = []
    for line in lines:
        if _FENCE.match(line):
            inside = not inside
            flags.append(True)        # the fence line itself is not content
        else:
            flags.append(inside)
    return flags


def page_anchors(text: str) -> set[str]:
    """Every anchor this page carries, as a renderer would number them.

    ATX and setext headings share one duplicate counter (GitHub numbers across
    both), hand-written ids join the set unnumbered, and fenced code
    contributes nothing.
    """
    lines = text.splitlines()
    fenced = _fenced(lines)
    titles: list[str] = []
    i = 0
    while i < len(lines):
        if fenced[i]:
            i += 1
            continue
        atx = _ATX.match(lines[i])
        if atx:
            # The title is taken WHOLE: a `{#id}` on the end is not stripped,
            # because GitHub does not read it (see the module docstring) and
            # stripping it here would hand the reader an anchor the renderer
            # never made.
            titles.append(atx.group(2))
            i += 1
            continue
        # Setext: a non-blank content line underlined with = or -. A blank
        # line above a `---` is a horizontal rule, which is what the ledgers
        # actually contain, so the non-blank test is load-bearing.
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        nxt_fenced = fenced[i + 1] if i + 1 < len(fenced) else False
        if (lines[i].strip() and _SETEXT.match(nxt) and not nxt_fenced
                and not lines[i].lstrip().startswith(("#", "-", "*", "+", "|", ">", "="))):
            titles.append(lines[i].strip())
            i += 2                    # the underline is consumed with its title
            continue
        i += 1
    return set(doc_index.number_anchors(titles)) | set(_EXPLICIT_ID.findall(text))


def links(text: str) -> list[tuple[int, str]]:
    """(line number, target) for every link in the document, fences excluded."""
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    for n, (line, fenced) in enumerate(zip(lines, _fenced(lines), strict=True), 1):
        if fenced:
            continue
        for m in _LINK.finditer(line):
            words = m.group(1).split()
            if words:
                out.append((n, words[0]))
        d = _DEFINITION.match(line)
        if d:
            out.append((n, d.group(1)))
    return out


def check(root: pathlib.Path = ROOT) -> list[tuple[str, int, str, str]]:
    """Every link that does not resolve, as (file, line, target, why)."""
    pages = {rel: (root / rel).read_text(encoding="utf-8", errors="replace")
             for rel in markdown_files(root)}
    anchors = {rel: page_anchors(text) for rel, text in pages.items()}
    problems: list[tuple[str, int, str, str]] = []
    for rel, text in pages.items():
        for line_no, target in links(text):
            if _SCHEME.match(target) or target.startswith("//"):
                continue                    # a URL; nothing local to resolve
            filepart, _, frag = target.partition("#")
            if not filepart:
                if frag and frag not in anchors[rel]:
                    problems.append((rel.as_posix(), line_no, target,
                                     f"no heading produces #{frag}"))
                continue
            target_path = (root / rel.parent / filepart).resolve()
            try:
                rel_target = target_path.relative_to(root)
            except ValueError:
                problems.append((rel.as_posix(), line_no, target,
                                 "points outside the repository"))
                continue
            if not target_path.exists():
                problems.append((rel.as_posix(), line_no, target,
                                 "file does not exist"))
                continue
            if not frag:
                continue                    # plain file link: existence is all
            if rel_target.suffix.lower() != ".md":
                continue                    # a code-file line anchor, not a heading
            if rel_target not in anchors:
                problems.append((rel.as_posix(), line_no, target,
                                 f"{rel_target.as_posix()} is not a scanned file"))
                continue
            if frag not in anchors[rel_target]:
                problems.append((rel.as_posix(), line_no, target,
                                 f"{rel_target.as_posix()} has no heading #{frag}"))
    return problems


def main(argv: list[str]) -> int:
    if argv:
        print(__doc__.splitlines()[0])
        print(f"unexpected argument: {argv[0]!r} (this checker takes none)")
        return 2
    problems = check()
    files = markdown_files()
    if not problems:
        print(f"links: {len(files)} markdown files, every anchor link resolves")
        return 0
    print(f"DEAD LINKS: {len(problems)} anchor link(s) resolve to nothing\n")
    for rel, line_no, target, why in problems:
        print(f"  {rel}:{line_no}  {target}\n      {why}")
    print("\n  the two ledgers' indexes are regenerated with:")
    print("    python ci/doc_index.py --write")
    print("  anything else is a link someone wrote — fix the heading or the link.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
