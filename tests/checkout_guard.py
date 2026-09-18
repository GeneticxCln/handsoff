"""The suite may not write inside the checkout it is running against.

ONE home for that property, because it has to hold in two kinds of process. The
suite runs against the developer's tree, and `githooks/pre-commit` runs it against
a COPY of the commit (it materialises the index into a scratch directory), so a
test that writes a file INTO the tree it is running against couples those two, and
the file outlives the run when the run is interrupted. Measured, the hard way: one
test planted a scratch module beside `handsoff.py` and removed it in a `finally`,
so a killed run left exactly the untracked file the suite's OWN lifecycle guard
then failed on, and another seeded `.venv` inside the checkout and skipped itself
whenever a real one was present — which is the machine where the pruning it tests
matters most.

The property is enforced WHERE THE WRITE HAPPENS rather than diffed afterwards: an
audit hook refuses it at the syscall wrapper, so nothing lands, the message names
the file, and the failure lands on the test that tried. Create-then-delete is
caught too — the shape both incidents had, and the one no comparison of the tree
before and after a test can see.

Both processes, because the suite's children write too: `tests/conftest.py` calls
`install()` for the suite's own process, and a `sitecustomize` shim written into a
scratch directory on the child's PYTHONPATH calls it for every python child the
suite spawns (the offscreen GUI scenarios, `run_driver` drivers, and any `python3`
a bash child starts). A child is not a lesser case: it is where the largest part
of the suite's behaviour actually runs.

Exempt: the tooling's own gitignored output, which pytest and coverage write
rather than a test — bytecode caches, `.pytest_cache` (and the
`pytest-cache-files-*` directory pytest builds it in and renames into place, which
is what a suite run in a file-only copy writes first), `.ruff_cache`, the coverage
data file and its shards, and the junit reports the gates ask pytest for.
"""
from __future__ import annotations

import os
import sys

#: Root-level names pytest builds its own artifacts through. `.pytest_cache` is
#: created in a `pytest-cache-files-*` directory that is then RENAMED into place
#: (`_pytest.cacheprovider._make_cachedir`), so its contents are written first —
#: which happens in a copy, where the cache directory does not exist yet. That is
#: not a corner: it is the file-only copy `githooks/pre-commit` runs the suite in.
_CHECKOUT_ARTIFACT_PREFIXES = ("pytest-cache-files-",)

_CHECKOUT_ARTIFACT_DIRS = frozenset({"__pycache__", ".pytest_cache",
                                     ".ruff_cache", "htmlcov"})

#: The audit events that mean "this writes": event -> (which arguments hold
#: paths, and which argument holds the directory fd those paths are relative to).
#: `open` is separate because its intent is in its FLAGS, not in an argument.
_WRITE_EVENTS = {
    "os.mkdir": ((0,), 2), "os.rmdir": ((0,), 1), "os.remove": ((0,), 1),
    "os.chmod": ((0,), 2), "os.chown": ((0,), 3), "os.utime": ((0,), 3),
    "os.truncate": ((0,), None),
    # Both names are judged: renaming a file OUT of the checkout changes it too.
    "os.rename": ((0, 1), None),
    # Only the DESTINATION for a link: a symlink stores its target as text and
    # never touches it, and a hard link leaves the source's contents alone — so
    # `tmp_tree/core -> checkout/core` is a file written in the tmp tree, not in
    # the checkout. Judging the source refused that, which every suite that
    # symlinks the app's modules into a fixture tree does.
    "os.link": ((1,), None), "os.symlink": ((1,), 2),
    # ...whereas a copy's SOURCE is only read — the destination is the write.
    "shutil.copyfile": ((1,), None), "shutil.copymode": ((1,), None),
    "shutil.copystat": ((1,), None), "shutil.copytree": ((1,), None),
    "shutil.rmtree": ((0,), 1), "tempfile.mkstemp": ((0,), None),
}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

#: Events that can only FAIL when their target already exists, so calling one on
#: an existing path writes nothing at all. `os.makedirs(exist_ok=True)` is how
#: pytest makes sure the junit report's directory is there, and it reaches
#: `os.mkdir` on a directory that IS there — refusing that stopped the suite from
#: starting under `ci/gates.sh` (measured: the `tests` gate went red on `tests`).
_CREATE_ONLY_EVENTS = ("os.mkdir", "os.link", "os.symlink")

#: The checkout this module judges, set by `install()`. Empty until then, and the
#: predicates REFUSE to answer rather than guessing: a guard that has not been told
#: which tree it protects would allow everything, which is the failure mode that
#: looks like success.
_ROOT = ""
_ROOT_PREFIX = ""


def install(root) -> None:
    """Arm the guard for `root`, in this process. Idempotent, and never undone.

    An audit hook cannot be removed once added, so this is deliberately a
    one-way switch: whoever asks first (the suite's conftest, or the shim in a
    child) decides the tree, and a second call for a different root would be a
    bug worth shouting about rather than quietly re-pointing the guard.
    """
    global _ROOT, _ROOT_PREFIX
    root = os.path.normpath(str(root))
    if _ROOT and _ROOT != root:
        raise RuntimeError(
            f"the checkout-write guard is already installed for {_ROOT} and was "
            f"asked for {root}: one process, one checkout")
    _ROOT = root
    _ROOT_PREFIX = root + os.sep
    sys.addaudithook(_hook)


def root() -> str:
    """The checkout this process is guarding, or "" before `install()`.

    Public because a CHILD has to be able to answer it: the shim loads this
    module by path, so "which tree is this process judging?" is the one question
    a test cannot otherwise ask a child, and a guard pointed at the parent
    directory would still refuse the write the test is watching for.
    """
    return _ROOT


def _writable_path(path, dir_fd=None) -> str:
    """The path an event writes, absolute — resolved the way the CALLER meant it.

    A relative name is not relative to the process cwd, necessarily: `shutil.rmtree`
    walks a tree with a directory fd and unlinks by bare name (`os.remove('x', fd)`),
    so resolving those against the cwd would judge a fixture under /tmp as if it were
    inside the checkout — a false refusal in every test that cleans up after itself.

    A path-like is accepted as well as a str: an audit event always hands over
    a str, but the guard's own tests pass Paths, and a bare fd has no path at all.
    """
    try:
        path = os.fspath(path)
    except TypeError:
        return ""
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str) or not path:
        return ""
    if os.path.isabs(path):
        return os.path.normpath(path)
    base = os.getcwd()
    if isinstance(dir_fd, int) and dir_fd >= 0:
        try:
            base = os.readlink(f"/proc/self/fd/{dir_fd}")
        except OSError:
            pass
    return os.path.normpath(os.path.join(base, path))


def _exempt_checkout_artifact(path: str) -> bool:
    """A gitignored artifact of running the suite, rather than a test's file."""
    parts = path[len(_ROOT_PREFIX):].split(os.sep)
    if any(p in _CHECKOUT_ARTIFACT_DIRS for p in parts):
        return True
    if parts[0].startswith(_CHECKOUT_ARTIFACT_PREFIXES):
        return True
    name = parts[-1]
    if len(parts) == 1:
        return name == ".coverage" or name.startswith(".coverage.")
    # tests/report.xml and tests/report.first-failure.xml: the junit output the
    # gates ask pytest for.
    return (len(parts) == 2 and parts[0] == "tests"
            and name.startswith("report") and name.endswith(".xml"))


def writes_into_the_checkout(path, dir_fd=None) -> str:
    """The file this path would write inside the checkout, or "" when it may.

    ONE definition of the property, so the hook and the tests that guard it can
    never disagree about what it forbids: "" means allowed (outside the checkout,
    or an artifact of the tooling), anything else is the path to refuse.
    """
    if not _ROOT:
        raise RuntimeError(
            "the checkout-write guard was asked about a path before install() "
            "told it which checkout to judge — refusing to answer, because an "
            "unconfigured guard allows everything")
    target = _writable_path(path, dir_fd)
    if not target or not target.startswith(_ROOT_PREFIX):
        return ""
    return "" if _exempt_checkout_artifact(target) else target


def target_of_event(event: str, args) -> str:
    """The offending path in one audit event, or "" when it writes nothing here."""
    if event == "open":
        flags = args[2] if len(args) > 2 else None
        if not isinstance(flags, int) or not flags & _WRITE_FLAGS:
            return ""
        return writes_into_the_checkout(args[0])
    spec = _WRITE_EVENTS.get(event)
    if spec is None:
        return ""
    positions, fd_index = spec
    fd = args[fd_index] if fd_index is not None and len(args) > fd_index else None
    for position in positions:
        if position >= len(args):
            continue
        target = writes_into_the_checkout(args[position], fd)
        if not target:
            continue
        if event in _CREATE_ONLY_EVENTS and os.path.exists(target):
            continue
        return target
    return ""


def _message(target: str, event: str) -> str:
    return (
        f"a test wrote inside the checkout: {target} ({event})\n"
        f"Build it under tmp_path — a fixture the test owns — instead. The suite "
        f"runs against the developer's tree AND, under githooks/pre-commit, "
        f"against a copy of the commit, so a test that writes into the tree it is "
        f"running against couples the two and an interrupted run leaves the file "
        f"behind. Gitignored tooling output is exempt ({', '.join(
            sorted(_CHECKOUT_ARTIFACT_DIRS))}, .coverage*, report*.xml).")


def _hook(event, args):
    if event != "open" and event not in _WRITE_EVENTS:
        return
    try:
        target = target_of_event(event, args)
    except Exception:
        # An event shape this guard cannot read is one it does not judge. The
        # property is "no write of a test's lands in the tree", and a hook that
        # crashed on an unexpected argument list would fail the suite for a
        # reason that has nothing to do with that property.
        return
    if target:
        raise AssertionError(_message(target, event))
