#!/usr/bin/env python3
"""Turn a pytest junit XML report into a concise CI job summary.

GitLab renders this three ways, so a failure never needs log digging:

  1. the junit report itself — the native *Test summary* tab and the merge
     request test widget list every failing test with its message;
  2. this script's digest, printed in its own collapsible log section (opened
     automatically when something failed, collapsed when everything passed);
  3. optionally, the same digest posted as a merge request note (`--post`),
     where the Markdown below is actually rendered.

The digest contains:

  * one line per failing test: short name + the assertion message;
  * the project's known Linux-environment failure signatures (missing ALSA
    runtime, OpenMP, offscreen-Qt libraries) with the one-line fix — the exact
    class of failure that broke the first Debian-slim pipeline.

Stdlib only (xml.etree, urllib, no pytest import): it runs in ``after_script``
even when pip-install never happened. It never exits non-zero — a summary must
not turn a green job red.

Usage: pytest_summary.py [--post] [junit-path]   (default tests/report.xml)

Posting needs a masked access token, because a CI job token cannot create
notes (GitLab issue #464591): set ``GITLAB_SUMMARY_TOKEN`` (or
``GITLAB_API_TOKEN``) as a masked, protected CI variable. Without it — or
outside a merge request pipeline — posting degrades to a log line.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

DEFAULT_REPORT = "tests/report.xml"

# Known Debian-slim environment failures -> the CI layer that fixes them.
# Order matters: first match wins (most specific first).
ENV_HINTS: list[tuple[str, str, str]] = [
    # Two DIFFERENT packages, and confusing them is how a pipeline stayed red:
    # sounddevice resolves the PortAudio library itself at import time, so
    # without libportaudio2 every job dies before a single test runs. The ALSA
    # runtime is a separate layer that PortAudio then links against. This entry
    # must come first: "PortAudio library not found" is sounddevice's own
    # message, while libasound.so.2 appears in a different failure (the dlopen
    # of the ALSA runtime, after PortAudio loaded).
    ("PortAudio library not found",
     "the PortAudio library is missing (sounddevice looks it up with "
     "`ctypes.util.find_library('portaudio')` at import, so this fails before "
     "any test runs — and libasound2t64 alone does NOT fix it)",
     "add the lib to ci `.qt_deps`: libportaudio2"),
    ("libasound.so.2",
     "ALSA runtime missing (PortAudio links it; this is not the same package "
     "as libportaudio2)",
     "add the lib to ci `.qt_deps`: libasound2t64"),
    ("libgomp",
     "OpenMP runtime missing (onnxruntime / ctranslate2 wheels need it)",
     "add the lib to ci `.qt_deps`: libgomp1"),
    ("libGL.so.1", "offscreen-Qt runtime library missing",
     "extend the apt layer in .gitlab-ci.yml `.qt_deps`"),
    ("libEGL", "offscreen-Qt runtime library missing",
     "extend the apt layer in .gitlab-ci.yml `.qt_deps`"),
    ("libxkbcommon", "offscreen-Qt runtime library missing",
     "extend the apt layer in .gitlab-ci.yml `.qt_deps`"),
    ("libglib-2.0", "glib runtime missing (Qt needs it)",
     "extend the apt layer in .gitlab-ci.yml `.qt_deps`"),
    ("libdbus", "dbus runtime missing (Qt offscreen needs it)",
     "extend the apt layer in .gitlab-ci.yml `.qt_deps`"),
    ("No module named",
     "a pip dependency failed to install (pip step is verbose — search the log)",
     "check requirements.txt / requirements-lock.txt"),
]

FAIL_STYLE = (
    "color:#a33;font-weight:bold;text-decoration:underline"
)
PASS_STYLE = "color:#3a3;font-weight:bold"

SECTION = "pytest_summary"


@dataclass
class Summary:
    """A rendered digest plus enough counts for the caller to colour it."""

    markdown: str
    failed: int = 0
    total: int = 0
    found: bool = True

    @property
    def ok(self) -> bool:
        return self.found and self.failed == 0


def _short_classname(classname: str, name: str) -> str:
    """pytest's ``classname`` -> a readable ``test_audio.py::TestX::test_y``.

    ``classname`` is the module's dotted path optionally followed by class
    names: ``tests.test_audio`` for a module-level test, but
    ``tests.test_audio.TestListener`` when the test lives in a class (nested
    classes append more). Class names start uppercase by convention, which is
    the only signal junit gives us to find where the module path ends.
    """
    parts = [p for p in (classname or "").split(".") if p]
    if not parts and "." in name:
        # A collection error: pytest supplies no classname and puts the module
        # node path in `name`, so render it as the module it names.
        parts, name = name.split("."), ""
    cut = len(parts)
    for i, part in enumerate(parts):
        if part[:1].isupper():
            cut = i
            break
    module, classes = parts[:cut], parts[cut:]
    if module[:1] == ["tests"]:
        module = module[1:]
    out = [f"{'/'.join(module)}.py"] if module else []
    out.extend(classes)
    if name:
        out.append(name)
    return "::".join(p for p in out if p)


# The junit widget carries the full traceback; the digest only needs enough of
# the message to recognise the failure.
MAX_MSG = 400


def _clip(text: str, limit: int = MAX_MSG) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _case_msg(case: ET.Element) -> str:
    for tag, attr in (("failure", "message"), ("error", "message")):
        el = case.find(tag)
        if el is not None:
            msg = _one_line(el.get(attr) or "")
            if not msg:
                msg = _one_line(el.text or "")
            if msg:
                return msg
        elif case.find(tag) is not None:
            return f"({tag}, no message)"
    skipped = case.find("skipped")
    if skipped is not None:
        return f"skipped: {_one_line(skipped.get('message') or '')}"
    return ""


def _env_hint(msg: str) -> str | None:
    low = msg.lower()
    for needle, what, fix in ENV_HINTS:
        if needle.lower() in low:
            return f"**Likely cause:** {what}. **Fix:** {fix}."
    return None


def summarize(path: pathlib.Path) -> Summary:
    """Render `path` into a summary. Unreadable reports never raise."""
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError:
        return Summary(
            "### pytest summary\n\n:no_entry: junit report not found — the "
            "suite likely crashed before pytest could write it. Read the job log.",
            found=False)
    except ET.ParseError as e:
        return Summary(
            f"### pytest summary\n\n:warning: junit report unreadable ({e}) — "
            "read the job log.", found=False)

    suites = ([root] if root.tag == "testsuite"
              else root.findall("testsuite"))
    cases = [c for s in suites for c in s.iter("testcase")]
    failures = [c for c in cases
                if c.find("failure") is not None or c.find("error") is not None]
    skipped = [c for c in cases if c.find("skipped") is not None]
    total = sum(int(s.get("tests", 0) or 0) for s in suites) or len(cases)
    errors = sum(int(s.get("errors", 0) or 0) for s in suites)
    fails = sum(int(s.get("failures", 0) or 0) for s in suites)

    lines: list[str] = []
    lines.append("### pytest summary")
    lines.append("")
    if not failures and not errors:
        lines.append(
            f":white_check_mark: **all {total} tests passed**"
            f"{f' · {len(skipped)} skipped' if skipped else ''}")
        lines.append("")
        lines.append("<span style='" + PASS_STYLE + "'>PASS</span>")
        return Summary("\n".join(lines), failed=0, total=total)

    lines.append(
        f":x: **{fails + errors} failed** of {total} "
        f"({len(skipped)} skipped) — see the failing list below")
    lines.append("")
    for c in failures[:15]:
        lines.append(
            f"- `{_short_classname(c.get('classname', ''), c.get('name', ''))}`"
            f" — {_clip(_case_msg(c)) or '(no message)'}")
    if len(failures) > 15:
        lines.append(f"- …and {len(failures) - 15} more (full list in the job log)")
    lines.append("")

    for c in failures[:3]:
        hint = _env_hint(_case_msg(c))
        if hint:
            lines.append(f"> {hint}")
            break

    if errors and not fails:
        lines.append("")
        lines.append("Collection/import errors usually mean the environment, "
                     "not the code: check the first pip/apt step in the log.")
    return Summary("\n".join(lines), failed=fails + errors, total=total)


# --- optional merge request note -------------------------------------------

def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _iid_from_open_mrs(value: str | None) -> str | None:
    """CI_OPEN_MERGE_REQUESTS looks like "group/proj!123,group/proj!124"."""
    for entry in (value or "").split(","):
        if "!" in entry:
            tail = entry.rsplit("!", 1)[1].strip()
            if tail.isdigit():
                return tail
    return None


def post_note(body: str) -> str | None:
    """Post `body` as a merge request note; return a URL, or None.

    Best-effort by design: a missing token, a commit pipeline with no merge
    request, or any API/network error degrades to a log line and returns None
    so the job's own result is never affected.
    """
    token = _env("GITLAB_SUMMARY_TOKEN", "GITLAB_API_TOKEN")
    if not token:
        print("summary: GITLAB_SUMMARY_TOKEN is not set — skipping the merge "
              "request note (the junit report and the digest above still apply)")
        return None
    project = _env("CI_PROJECT_ID")
    iid = _env("CI_MERGE_REQUEST_IID") or _iid_from_open_mrs(
        os.environ.get("CI_OPEN_MERGE_REQUESTS"))
    if not (project and iid):
        print("summary: not a merge request pipeline — skipping the note")
        return None

    api = (_env("CI_API_V4_URL") or "https://gitlab.com/api/v4").rstrip("/")
    url = f"{api}/projects/{project}/merge_requests/{iid}/notes"
    request = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"summary: could not post the merge request note ({e}) — the "
              "digest above is authoritative")
        return None
    return str(payload.get("web_url") or payload.get("id") or "posted")


# --- output ----------------------------------------------------------------

def emit(summary: Summary) -> None:
    """Print the digest, as a GitLab collapsible section when running in CI."""
    if not os.environ.get("GITLAB_CI"):
        print(summary.markdown)
        return
    now = int(time.time())
    # Failures open themselves; a green digest stays folded away.
    options = "" if not summary.ok else "[collapsed=true]"
    header = ("pytest summary — failures "
              if not summary.ok else "pytest summary")
    print(f"\x1b[0Ksection_start:{now}:{SECTION}{options}\r\x1b[0K{header}")
    print(summary.markdown)
    print(f"\x1b[0Ksection_end:{now}:{SECTION}\r\x1b[0K")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--post"]
    post = "--post" in argv[1:]
    path = pathlib.Path(args[0] if args else DEFAULT_REPORT)
    summary = summarize(path)
    emit(summary)
    if post:
        link = post_note(summary.markdown)
        if link:
            print(f"summary: posted the merge request note ({link})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
