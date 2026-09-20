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

The same hook also refuses a write into the developer's REAL user directories,
because the checkout is not the only tree a test can reach. The suite runs with
the developer's own HOME live — the user-dir sandbox swaps it for the duration of
a LOAD and restores it — so a test that resolves a config or state path without
going through that sandbox writes into the developer's `~/.config` or
`~/.local/state` and reports nothing: the file is simply there afterwards.
Measured, the hard way: the settings app's `apply_autostart` wrote the real
`~/.config/niri/config.kdl` from a test that only believed it was writing into a
temp home, and the fix was to sandbox the LOAD — nothing else noticed the file.
The roots are named by whichever process installs the guard rather than read from
the environment: conftest captures them at import, before any sandbox runs, and a
CHILD's HOME is a throw-away one, so only a process that remembers the real paths
can protect them.

Exempt, for the checkout only: the tooling's own gitignored output, which pytest
and coverage write rather than a test — bytecode caches, `.pytest_cache` (and the
`pytest-cache-files-*` directory pytest builds it in and renames into place, which
is what a suite run in a file-only copy writes first), `.ruff_cache`, the coverage
data file and its shards, and the junit reports the gates ask pytest for. The
checkout rule is asked FIRST for exactly this reason: the checkout normally lives
inside the developer's home, so judging the user dirs first would refuse pytest's
own artifacts.
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

#: The developer's real user directories, set by `install()` — the paths a load
#: that MISSED the sandbox bakes, and a test then writes into. Same refusal as the
#: checkout root: empty means the guard does not know them yet, and answering
#: "allowed" to everything is how a guard that was never told looks identical to a
#: suite that had nothing to refuse.
_PROTECTED: tuple = ()

#: Whether the audit hook has been added in this process. An audit hook cannot
#: be removed once added, so a second AGREEING `install()` (a fixture re-arming
#: after a reload) must not register a second hook — the checks above all pass,
#: which is exactly when the duplicate used to land.
_INSTALLED = False


def install(root, protected=()) -> None:
    """Arm the guard for `root` and the developer's `protected` dirs, here.

    An audit hook cannot be removed once added, so this is deliberately a
    one-way switch: whoever asks first (the suite's conftest, or the shim in a
    child) decides what is protected, and a second call that disagrees would be a
    bug worth shouting about rather than quietly re-pointing the guard — so the
    tree and the user dirs are both checked, not overwritten.

    `protected` is passed IN rather than read from the environment, because a
    child's HOME is a throw-away one: only the process that still remembers the
    developer's real paths can name them, and `tests/conftest.py` captures them
    at import time, before any sandbox runs.
    """
    global _ROOT, _ROOT_PREFIX, _PROTECTED, _INSTALLED
    root = os.path.normpath(str(root))
    if _ROOT and _ROOT != root:
        raise RuntimeError(
            f"the checkout-write guard is already installed for {_ROOT} and was "
            f"asked for {root}: one process, one checkout")
    directories = []
    for path in protected:
        path = os.path.normpath(str(path))
        # `/` is every path there is, and a relative name is not a directory: a
        # root either of those would protect would forbid all writing at all.
        if not path or path == os.sep or not os.path.isabs(path):
            continue
        if path not in directories:
            directories.append(path)
    if _PROTECTED and tuple(directories) != _PROTECTED:
        raise RuntimeError(
            f"the write guard already protects {_PROTECTED} and was asked for "
            f"{tuple(directories)}: one process, one set of user dirs")
    _ROOT = root
    _ROOT_PREFIX = root + os.sep
    _PROTECTED = tuple(directories)
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED = True


def root() -> str:
    """The checkout this process is guarding, or "" before `install()`.

    Public because a CHILD has to be able to answer it: the shim loads this
    module by path, so "which tree is this process judging?" is the one question
    a test cannot otherwise ask a child, and a guard pointed at the parent
    directory would still refuse the write the test is watching for.
    """
    return _ROOT


def protected() -> tuple:
    """The developer's user dirs this process refuses to let a test write into.

    Public for the same reason `root()` is: a CHILD has to be able to answer it.
    Its own HOME is a sandbox, so a guard that read the dirs from its environment
    instead of being TOLD them would protect the wrong home — silently, and in
    the process where most of the suite's behaviour runs.
    """
    return _PROTECTED


def _require_installed() -> None:
    """Refuse to answer before `install()`: an unconfigured guard allows everything."""
    if not _ROOT:
        raise RuntimeError(
            "the write guard was asked about a path before install() told it "
            "which checkout to judge — refusing to answer, because an "
            "unconfigured guard allows everything")


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

    ONE definition of this half of the property, so the hook and the tests that
    guard it can never disagree about what it forbids: "" means allowed (outside
    the checkout, or an artifact of the tooling), anything else is the path to
    refuse. `forbidden_write` is the two halves as one answer.
    """
    _require_installed()
    target = _writable_path(path, dir_fd)
    if not target or not target.startswith(_ROOT_PREFIX):
        return ""
    return "" if _exempt_checkout_artifact(target) else target


def _in(path: str, root: str) -> bool:
    """Containment by BOUNDARY, not by shared prefix: `/home/u-data` is not
    inside `/home/u`. The string-prefix version of this admitted a sibling
    directory whose name merely started the same way."""
    return path == root or path.startswith(root + os.sep)


def _protected_root_of(path: str) -> str:
    """The developer's user dir this path is inside, or "" when it is not."""
    for directory in _PROTECTED:
        if _in(path, directory):
            return directory
    return ""


def writes_into_the_developer_dirs(path, dir_fd=None) -> str:
    """The developer's real user-dir file this would write, or "" when it may.

    A path INSIDE the checkout is not this predicate's business: the checkout
    rule owns everything under it, exemptions included, and the checkout normally
    lives in the developer's home — so asking this one first would refuse
    pytest's own `.coverage` and `.pytest_cache` and stop the suite from running.
    """
    _require_installed()
    target = _writable_path(path, dir_fd)
    if not target or target.startswith(_ROOT_PREFIX):
        return ""
    return target if _protected_root_of(target) else ""


def forbidden_write(path, dir_fd=None) -> str:
    """The path this write may not land on for ANY reason, or "" — one answer.

    The hook and the tests both ask this, so the two cannot disagree about the
    ORDER the rules are applied in: the checkout first, because it is the
    narrower rule and it has exemptions, then the developer's user dirs.
    """
    return writes_into_the_checkout(path, dir_fd) \
        or writes_into_the_developer_dirs(path, dir_fd)


def target_of_event(event: str, args) -> str:
    """The offending path in one audit event, or "" when it writes nothing here."""
    if event == "open":
        flags = args[2] if len(args) > 2 else None
        if not isinstance(flags, int) or not flags & _WRITE_FLAGS:
            return ""
        return forbidden_write(args[0])
    spec = _WRITE_EVENTS.get(event)
    if spec is None:
        return ""
    positions, fd_index = spec
    fd = args[fd_index] if fd_index is not None and len(args) > fd_index else None
    for position in positions:
        if position >= len(args):
            continue
        target = forbidden_write(args[position], fd)
        if not target:
            continue
        if event in _CREATE_ONLY_EVENTS and os.path.exists(target):
            continue
        return target
    return ""


def _message(target: str, event: str) -> str:
    # Which rule refused is decided by WHERE the path is, not by re-running the
    # predicates: a message that asked "which root matched" would, for a path the
    # user-dir rule refused and no captured root covers, fall through and blame
    # the checkout — a message about the wrong tree, from the guard itself.
    if not target.startswith(_ROOT_PREFIX):
        root = _protected_root_of(target) or "the developer's real user dirs"
        return (
            f"a test wrote into the developer's real user dirs: {target} "
            f"({event}), under {root}\n"
            f"That is not a fixture: the suite runs with the developer's own HOME "
            f"live, so a path resolved without the user-dir sandbox reads and "
            f"writes their real config and state, and nothing downstream can tell "
            f"it happened. Build the file under tmp_path, or point HOME/XDG_* at a "
            f"throw-away home (`conftest.isolated_user_dirs` for a load, "
            f"`conftest.sandbox_env` for a child) if the code under test resolves "
            f"them itself.")
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
