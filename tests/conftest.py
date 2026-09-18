"""Shared fixtures for the handsoff test suite (split out of the old
monolithic test_handsoff.py; see test_*.py modules for the areas)."""
from __future__ import annotations

import atexit
import contextlib
import copy
import functools
import gc
import importlib.util
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path

import numpy as np   # noqa: F401  (test modules rely on it being imported)
import pytest

# The app's canonical module name, from its ONE definition. Imported at conftest
# import time (before any sandbox): core/__init__.py resolves no user directory
# while it is imported — only inside its functions — so this cannot bake the
# developer's HOME into anything.
from core import APP_MODULE_NAME, app_instance, app_module

from core import registry as _core_registry

# The checkout-write property has ONE home (`tests/checkout_guard.py`) because it
# has to hold in two kinds of process — see the block that installs it below.
from checkout_guard import forbidden_write, install as install_checkout_guard, \
    protected as guarded_user_dirs, target_of_event as _checkout_write_target, \
    writes_into_the_checkout, writes_into_the_developer_dirs  # noqa: F401

HERE = Path(__file__).resolve().parent.parent   # the repo root


# ------------------------------------------------------- collection order
# The suite must not care what order it runs in — but collection order is what a
# developer sees every day, so an ordering dependence hides in it and surfaces
# only when something unrelated changes the file list. CI therefore re-runs the
# suite in a different order on purpose. Two seeded modes, both reproducible:
#
#   HANDSOFF_TEST_ORDER_SEED=<seed>   shuffle the tests themselves
#                                     (order within files and across files)
#   HANDSOFF_TEST_ORDER_FILES=<seed>  shuffle the FILE order only, keeping each
#                                     file's internal sequence intact
#
# The file-contiguous mode is not redundant: it is a stricter probe of what a
# *file* leaves behind for the next one, and a failure there is far easier to
# read than a shuffled one. The seed is printed in the run header so a red
# build can be reproduced exactly.


def _order_banner() -> str:
    seed = os.environ.get("HANDSOFF_TEST_ORDER_SEED")
    if seed:
        return f"test order: SHUFFLED (seed {seed})"
    files_seed = os.environ.get("HANDSOFF_TEST_ORDER_FILES")
    if files_seed:
        return f"test order: FILE ORDER SHUFFLED (seed {files_seed})"
    return ""


def pytest_report_header(config):
    return _order_banner() or None


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # Also in the summary: CI runs `-q`, which suppresses the header, and a
    # shuffled run that failed is useless without the seed that reproduces it.
    banner = _order_banner()
    if banner:
        terminalreporter.write_line(banner)


def pytest_collection_modifyitems(session, config, items):
    seed = os.environ.get("HANDSOFF_TEST_ORDER_SEED")
    if seed:
        random.Random(seed).shuffle(items)
        return
    files_seed = os.environ.get("HANDSOFF_TEST_ORDER_FILES")
    if not files_seed:
        return
    groups: dict[str, list] = {}
    for item in items:
        path = str(getattr(item, "path", None) or item.fspath)
        groups.setdefault(path, []).append(item)
    order = list(groups)
    random.Random(files_seed).shuffle(order)
    items[:] = [item for path in order for item in groups[path]]


def wait_for(pred, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll `pred` until it is true or `timeout` elapses; return its last value.

    The suite's replacement for `time.sleep(0.2); assert something_happened`.
    These paths hand work to a worker thread, which needs *a chance to run* —
    not a specific number of milliseconds. A fixed sleep asserts the author's
    machine speed; polling asserts the work happened, and a slow box just waits
    longer. Every call site was converted from a sleep that could only fail
    spuriously (never catch a real bug faster).
    """
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(interval)


def _user_site() -> str:
    """The real user site-packages path (computed with the real HOME)."""
    import site
    try:
        return site.getusersitepackages()
    except Exception:
        return ""


# ------------------------------------------------------------ user-dir sandbox
# Every module that resolves CONFIG_DIR/STATE_DIR at import bakes whatever HOME
# it sees into module constants. That was guaranteed for the FIRST load only:
# several suites load a SECOND monolith in-process (the settings app, which also
# derives `NIRI_CONFIG` — the file `apply_autostart` writes), with the
# developer's real HOME. So their paths were the developer's: a test read the
# real settings.json, and the answer depended on whose machine ran the suite.
# The sandbox therefore belongs to the LOAD, not to one fixture, so it covers
# whatever a test loads, however many times.
_SANDBOX_VARS = ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")

# The developer's real home, captured while conftest is IMPORTED — before any
# sandbox runs, and therefore still the real one whenever a later check needs to
# ask "is this path the developer's?".
_REAL_HOME = Path(os.path.expanduser("~"))

# The developer's real user dirs, for the OTHER half of the write guard: the
# checkout is not the only tree a test can reach. The suite runs with the real
# HOME live (the sandbox covers a LOAD, and restores it afterwards), so a test
# that resolves a config or state path without that sandbox writes into the
# developer's ~/.config or ~/.local/state and nothing downstream can tell.
# Captured at the same moment as `_REAL_HOME`, and passed to every child rather
# than derived there: a child's HOME is a throw-away one, so only a process that
# still remembers the real paths can protect them. Same three roots the sandbox
# pins, which is what "the developer's user dirs" means in this suite.
_REAL_USER_DIRS = tuple(dict.fromkeys(
    [str(_REAL_HOME)] +
    [os.path.normpath(os.environ[var]) for var in _SANDBOX_VARS[1:]
     if os.environ.get(var)]))

# Path constants a loaded module bakes from HOME/XDG at import. `_load` uses
# them to REFUSE a load that kept the developer's real ones, so the property is
# enforced by the one thing every load goes through rather than by each test
# remembering to look.
_SANDBOX_PATH_ATTRS = (
    "HOME", "CONFIG_DIR", "STATE_DIR", "SETTINGS_FILE", "HISTORY_FILE",
    "MEMORY_FILE", "DECISIONS_FILE", "LOG_FILE", "CONTROL_SOCK",
    "CAP_EVENTS_FILE", "MIC_EVENTS_FILE", "REMINDERS_FILE", "NIRI_CONFIG",
    "WHISPER_MODEL_DIR",
)


def _assert_load_stayed_in_the_sandbox(mod, name: str) -> None:
    """Refuse a loaded module that kept the developer's real user dirs.

    A load that resolves CONFIG_DIR/STATE_DIR outside the sandbox is the leak
    the sandbox exists to prevent AND the one nothing else notices: the module
    works perfectly, it just reads and writes the developer's real config and
    state. Checking it here, at the load, means the failure lands on the load
    (with the attribute named) instead of on whichever test later happens to
    compare paths — or, worse, on the developer's disk.

    Measured motivation: a mutation that removed the sandbox from this loader
    let the settings app write its `NIRI_CONFIG` — the developer's real
    ~/.config/niri/config.kdl — from a test that only *believed* it was writing
    into a temp home.
    """
    offenders = []
    for attr in _SANDBOX_PATH_ATTRS:
        value = getattr(mod, attr, None)
        if not isinstance(value, (str, Path)):
            continue
        try:
            resolved = Path(value)
        except (TypeError, ValueError):
            continue
        if resolved.is_relative_to(_REAL_HOME):
            offenders.append(f"{attr}={value}")
    if offenders:
        raise AssertionError(
            f"conftest._load: {name} resolved the developer's real user dirs "
            f"({'; '.join(offenders)}) — the load was not sandboxed")


# Every sandbox home this run created. Removed at process exit, not when the
# load finishes: the module's constants keep pointing at it for the session.
_SANDBOX_HOMES: list[str] = []


@atexit.register
def _remove_sandbox_homes() -> None:
    while _SANDBOX_HOMES:
        shutil.rmtree(_SANDBOX_HOMES.pop(), ignore_errors=True)


@contextlib.contextmanager
def isolated_user_dirs(prefix: str = "handsoff-testhome-"):
    """Point HOME/XDG at a throw-away directory for the duration of a load.

    Restored on the way out, so nothing else in the suite (subprocess
    environments, path helpers like `_user_site`) moves. Not deleted on the
    way out: the loaded module's constants point into it for the rest of the
    session, and a test that writes through them is writing into the sandbox —
    which is the point. Every sandbox is registered and removed when the
    PYTEST PROCESS exits, so a suite run no longer litters /tmp with one home
    per module load.
    """
    home = tempfile.mkdtemp(prefix=prefix)
    _SANDBOX_HOMES.append(home)
    saved = {k: os.environ.get(k) for k in _SANDBOX_VARS}
    os.environ.update({
        "HOME": home,
        "XDG_STATE_HOME": str(Path(home) / ".local" / "state"),
        "XDG_CONFIG_HOME": str(Path(home) / ".config"),
    })
    try:
        yield Path(home)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# What git exports to whatever it runs — a pre-commit hook sees the first of
# these above all — and what a child that builds its OWN repository must not
# inherit. One constant because a guard checks it against a real hook's
# environment, and a copy in the test file would be a second list to drift.
GIT_PLUMBING = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_PREFIX",
                "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_PARAMETERS",
                "GIT_CONFIG_COUNT", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "GIT_COMMITTER_DATE", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR")


def sandbox_env(home=None) -> dict:
    """A child-process environment whose user dirs are a throw-away directory.

    A driver that loads a monolith in a SUBPROCESS resolves the same paths, so
    it needs the same sandbox — and a redirected HOME hides user site-packages,
    which is where PySide6 and sounddevice live, so the real user-site path is
    kept on PYTHONPATH alongside the repo root.
    """
    env = dict(os.environ)
    # GIT'S PLUMBING DOES NOT BELONG TO A CHILD THAT MAKES ITS OWN REPOSITORY.
    # Git exports its own environment to whatever it runs — a pre-commit hook
    # sees GIT_INDEX_FILE=.git/index, GIT_PREFIX and an author identity — and the
    # suite IS run by that hook, so a scratch repository built with the inherited
    # environment gets a pointer into the caller's index: `git worktree add`
    # failed inside the scratch repo, and the three end-to-end gate tests failed
    # only while a commit was in progress. The harness supplies the repository,
    # the identity and the config itself, so git's own plumbing is dropped here
    # rather than passed on (found by committing, not by reading).
    for name in GIT_PLUMBING:
        env.pop(name, None)
    home = Path(home or tempfile.mkdtemp(prefix="handsoff-testhome-"))
    env.update({
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    })
    # THE CHECKOUT-WRITE GUARD TRAVELS WITH THE CHILD, and `sitecustomize` is how:
    # it is the one hook CPython runs in EVERY interpreter at start-up, whatever
    # the argv is (`-c`, a script, `-m pytest`, something bash started), so the
    # shim directory goes FIRST on the path and names the checkout through the
    # environment. A child is not a lesser case — the offscreen GUI scenarios and
    # every driver the suite runs are children, so they are where the property was
    # blind until it learned to travel.
    env[_GUARD_ENV] = str(HERE)
    # A child's HOME is a sandbox, so it cannot answer "which dirs are the
    # developer's?" from its own environment — the parent NAMES them, and the shim
    # hands them to the same module.
    env[_PROTECTED_ENV] = os.pathsep.join(_REAL_USER_DIRS)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_guard_shim_dir(), str(HERE), _user_site(),
                    env.get("PYTHONPATH", "")) if p)
    return env


# --------------------------------- no test writes where it must not, anywhere
# ONE property, ONE home for it — `tests/checkout_guard.py` — because it has to
# hold in TWO kinds of process (the suite's own, and every python child it spawns:
# `run_driver` drivers, the offscreen GUI scenarios, any `python3` a bash child
# starts) and over TWO trees (the checkout, and the developer's real user dirs,
# which the checkout normally sits inside). This block installs it here and
# exports the means of installing it there; the full story — why it is enforced at
# the write rather than diffed afterwards, what is exempt, and the limits — is the
# module's docstring.
_GUARD_ENV = "HANDSOFF_CHECKOUT_GUARD"
#: The developer's real user dirs, for the child to protect. Separate from the
#: variable above because it answers a different question: a child points at the
#: same checkout but cannot name the developer's home from its own environment.
_PROTECTED_ENV = "HANDSOFF_GUARD_USER_DIRS"
_GUARD_SHIM = '''\
"""Install the suite's checkout-write guard in this child.

Written by `tests/conftest.py` into a scratch directory that goes FIRST on the
child's PYTHONPATH, and run by `site` at interpreter start-up — the one hook every
python child has, whatever its argv (`-c`, a script, `-m pytest`, something bash
started). Without HANDSOFF_CHECKOUT_GUARD set it does nothing, so its presence
changes nothing outside the suite; with it set, the checkout's own guard module is
what decides, so parent and child cannot drift about what is forbidden.

The developer's user dirs come from a SECOND variable and are handed to
`install()`: this child's own HOME is a throw-away one, so the paths to protect
can only come from a parent that still remembers the real ones.
"""
import importlib.util
import os
import sys
from pathlib import Path

_root = os.environ.get("HANDSOFF_CHECKOUT_GUARD")
if _root:
    _path = Path(_root) / "tests" / "checkout_guard.py"
    if _path.is_file():
        _spec = importlib.util.spec_from_file_location("checkout_guard", _path)
        _module = importlib.util.module_from_spec(_spec)
        sys.modules.setdefault("checkout_guard", _module)
        _spec.loader.exec_module(_module)
        _module.install(
            _root,
            protected=tuple(d for d in os.environ.get(
                "HANDSOFF_GUARD_USER_DIRS", "").split(os.pathsep) if d))
'''
_GUARD_SHIM_DIR: list[str] = []


@atexit.register
def _remove_guard_shim() -> None:
    while _GUARD_SHIM_DIR:
        shutil.rmtree(_GUARD_SHIM_DIR.pop(), ignore_errors=True)


def _guard_shim_dir() -> str:
    """The scratch directory holding the shim, made once per session.

    Not the checkout: the suite may not write there — that is this very property
    — and the shim belongs to the suite rather than to the tree.
    """
    if not _GUARD_SHIM_DIR:
        directory = tempfile.mkdtemp(prefix="handsoff-guard-")
        Path(directory, "sitecustomize.py").write_text(_GUARD_SHIM,
                                                      encoding="utf-8")
        _GUARD_SHIM_DIR.append(directory)
    return _GUARD_SHIM_DIR[0]


install_checkout_guard(HERE, protected=_REAL_USER_DIRS)




def method_source(source: str, name: str) -> str:
    """The whole body of the method `name` in `source`, and nothing else.

    Several tests assert that a WIRING exists — "closeEvent stops the mic probe",
    "_refresh_health runs the fetch off the GUI thread" — and the honest way to
    ask that is about the METHOD. It runs from its `def` to the first line that
    begins a new member at the same indentation (or the end of the file).

    Slicing a fixed number of CHARACTERS instead — `src[index("def closeEvent"):
    index(...) + 500]` — makes the test fail the day somebody adds a line above
    the one it is looking for. That failure says "the code moved", not "the
    wiring is gone", and it happened for real: adding the preview cleanup at the
    top of `closeEvent` pushed `_live_probe.stop()` past a 400-character window
    and turned two honest tests red for no reason.
    """
    lines = source.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(rf"\s*def {re.escape(name)}\s*\(", line)), None)
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    for i in range(start + 1, len(lines)):
        if not lines[i].strip():
            continue
        here = len(lines[i]) - len(lines[i].lstrip())
        if here <= indent and not lines[i].lstrip().startswith("#"):
            return "\n".join(lines[start:i])
    return "\n".join(lines[start:])


def shell_function(source: str, name: str) -> str:
    """The whole body of the bash function `name` in `source`, and nothing else.

    The shell twin of `method_source`, for the guards that read `ci/gates.sh`: a
    bash function ends at a `}` in column zero, and asking for a fixed slice of
    characters instead is the same mistake `method_source` documents — the test
    then fails the day somebody adds a line above the one it looks for, and says
    "the code moved" rather than "the wiring is gone".

    One home for both callers (the gate tests and the clean-checkout gate tests):
    two copies of "what is a shell function body" could disagree about a nested
    `}` or a heredoc, and the file that would be wrong is the one nobody reads.
    """
    lines = source.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith(f"{name}() {{")), None)
    if start is None:
        return ""
    for i in range(start + 1, len(lines)):
        if lines[i] == "}":
            return "\n".join(lines[start:i + 1])
    return ""


def run_driver(argv, *, home=None, cwd=None, env_extra=None, **kwargs):
    """Run a driver that LOADS a monolith, in a sandboxed child process.

    The child resolves CONFIG_DIR/STATE_DIR from HOME exactly like an in-process
    load, so it needs the same sandbox — and no import-level guard can see it,
    because a driver is a string handed to `python -c`. One constructor for all
    of them, so the sandbox cannot be remembered at one call site and forgotten
    at the next (the shape the in-process loader had).

    `env_extra` is for the driver's own switches (offscreen Qt, its home
    variable); `**kwargs` go to `subprocess.run`. A driver whose SOURCE is long
    belongs on STDIN (`["-", ...]` plus `input=`): `-c` is capped by
    MAX_ARG_STRLEN, which turns a growing driver into an `Argument list too
    long` failure that has nothing to do with what the tests test.
    """
    env = sandbox_env(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, *argv], env=env,
                          cwd=str(cwd or HERE), **kwargs)


def _load(name: str, path: Path):
    """Load a module by path, sandboxed, under the ONE name the app knows.

    `handsoff.py` is special by design, not by convention: it registers ITSELF
    under the canonical name (`core.APP_MODULE_NAME`) as it loads and refuses to
    run a second copy, so this loader registers it under that same name rather
    than one of its own. The bare `handsoff` alias is deliberately NOT set any
    more: two names for one app is what made "is it already loaded?" ambiguous,
    and a stray `import handsoff` now fails loudly (the app refuses a duplicate)
    instead of silently building a second app with its own SETTINGS and caches.
    """
    is_app = Path(path).name == "handsoff.py"
    if is_app:
        # ONE app per process, so "load the app again" is "hand back the app":
        # executing the file a second time is a second app (its own SETTINGS,
        # its own model mirrors, and a module body that repoints the SHARED
        # core.audio) — which the app itself now refuses. Asking for it here
        # returns the running module instead, whatever name the caller used.
        running = app_module() or app_instance()
        if running is not None:
            return running
    spec = importlib.util.spec_from_file_location(
        APP_MODULE_NAME if is_app else name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register the module in sys.modules BEFORE executing it. Without this it is
    # absent, so a later load built a SECOND instance with its own SETTINGS,
    # locks and caches — divergence that surfaces as order-dependent flakes
    # rather than as an error — and the app cannot find itself. Only the app and
    # its own modules: doing it for every module would put `core/audio.py` into
    # sys.modules as "audio" and shadow real imports.
    sys.modules[name if not is_app else APP_MODULE_NAME] = mod
    try:
        # The sandbox is part of the LOAD, not of a fixture: whatever a test
        # loads — the settings app, the bubble, a core module, thirty times —
        # resolves its user directories inside a throw-away HOME.
        with isolated_user_dirs():
            spec.loader.exec_module(mod)
            # ...and the load may not KEEP the developer's paths either: the
            # sandbox moved HOME, but only the module knows whether it read it.
            _assert_load_stayed_in_the_sandbox(mod, name)
    except BaseException:
        # never leave a half-initialised module behind for the next test
        for key in {name, APP_MODULE_NAME if is_app else name}:
            if sys.modules.get(key) is mod:
                sys.modules.pop(key, None)
        raise
    return mod


def _import_app_with_isolated_config():
    """Load the monolith against a throw-away config/state directory.

    handsoff.py resolves CONFIG_DIR and STATE_DIR from HOME/XDG_STATE_HOME at
    import and then READS the settings.json it finds there, baking the result
    into module-level state (SETTINGS, SYSTEM_PROMPT, derived globals). Run as
    authored, the suite therefore graded itself against whoever ran it: on this
    machine `notification_reader` is true, so every `Assistant()` built anywhere
    in the tests spawned a live dbus-monitor and leaked a reader thread, and the
    system prompt was built from a personal assistant name. Pointing HOME at an
    empty directory for the duration of the import gives every run the same
    defaults — and keeps the tests out of the developer's real ~/.config,
    ~/.local/state and control socket. HOME is restored immediately afterwards,
    so nothing else in the suite (subprocess environments, path helpers) moves.
    """
    # One sandbox implementation, used by this load and by every other in-process
    # load in the suite (`_load`), so the property cannot be true here and false
    # for the next monolith a test happens to import.
    with isolated_user_dirs():
        return _load("handsoff_core", HERE / "handsoff.py")


@pytest.fixture(scope="session")
def H():
    return _import_app_with_isolated_config()


class _CoreModule:
    """A core submodule resolved on FIRST USE rather than at import.

    Two of them — `core.tools` and `core.audio` — bake CONFIG_DIR/STATE_DIR from
    whatever HOME is live when they are first imported, and pytest imports a
    test module BEFORE any fixture runs. A module-scope `from core import tools`
    therefore points the whole tool layer at the developer's real state, which
    `TestImportStatementsCannotBypassTheSandbox` exists to catch.

    Tests still want the short `_core_tools.BoundedJob` spelling, so this defers
    the import to the first attribute access — which happens inside a test body,
    i.e. after the autouse fixtures have loaded the app under
    `isolated_user_dirs()`. By then `core.tools` is already in `sys.modules`, so
    the import resolves to that sandboxed copy instead of re-executing it.

    The suite has no collection-time use of either name (verified by walking the
    ASTs for attribute reads in module and class scope), so this cannot become
    the thing it is avoiding: there is no moment before the sandbox at which it
    resolves.
    """

    __slots__ = ("_name", "_mod")

    def __init__(self, name: str):
        self._name = name
        self._mod = None

    def _resolve(self):
        """The module, imported only once the sandboxed app is in place.

        Refusing before then is the point: it is what makes the deferred import
        safe by construction rather than by convention. If anything ever does
        resolve this earlier, it fails with a sentence instead of silently
        baking the developer's real state into the tool layer.
        """
        mod = self._mod
        if mod is None:
            if app_module() is None:
                raise RuntimeError(
                    f"core.{self._name} was requested before the sandboxed app "
                    "loaded; import it inside the test body instead of at "
                    "module scope")
            mod = self._mod = importlib.import_module(f"core.{self._name}")
        return mod

    def __getattr__(self, attr):
        # Dunders are answered WITHOUT resolving. pytest's collector probes
        # every module-level object with `getattr(obj, "__test__", False)` while
        # collecting the test module — i.e. before any fixture, with the
        # developer's HOME live — so a proxy that resolved on a dunder lookup
        # would bake exactly what it exists to avoid. It would also be collected
        # as a test candidate, which it is not.
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return getattr(self._resolve(), attr)

    def __setattr__(self, attr, value):
        # Transparent for patching and for identity reads: `setattr(_core_tools,
        # "x", …)` must reach the module, never land on this handle.
        if attr in _CoreModule.__slots__:
            object.__setattr__(self, attr, value)
        else:
            setattr(self._resolve(), attr, value)

    def __repr__(self):
        return f"<core module handle {self._name!r}>"


def core_module(name: str):
    """The sandboxed `core.<name>`, resolved on first use (see `_CoreModule`)."""
    return _CoreModule(name)


def pin_offer(H, monkeypatch, name: str):
    """Pin ONE offer object onto every path `_dep()` can resolve.

    The tools reach the module-level offers through `_dep()`, which is
    `_CURRENT` in the calling thread but `_DEFAULT_DEPS` in a NEW thread (a
    thread starts with an empty context) — and once a second handsoff instance
    has been imported, those two can belong to DIFFERENT modules, each with its
    own `_kill_offer`. Patching the global on one module therefore made a test
    pass or fail depending on which monolith imported last, which the shuffled
    test order caught. Shadowing the slot on each host leaves exactly one
    offer, on the single path both the main thread and workers take.

    `name` is the bare offer name: 'kill' -> H._kill_offer.
    """
    offer = _core_registry.Offer(name)
    monkeypatch.setattr(H, f"_{name}_offer", offer)
    hosts = [getattr(H, "_tool_dependencies", None)]
    tools = getattr(H, "_core_tools", None)
    if tools is not None:
        hosts.append(getattr(tools, "_DEFAULT_DEPS", None))
        try:
            hosts.append(tools._CURRENT.get())
        except Exception:
            pass
    for host in hosts:
        if host is not None and host is not offer:
            monkeypatch.setattr(host, f"_{name}_offer", offer)
    return offer


# ---------------------------------------------------------------- isolation
# The monolith is loaded ONCE per session, so anything a test writes into it
# survives into every later file. Several suites hand-edit those globals
# instead of monkeypatching (SETTINGS["command_policy"], OLLAMA_BASE, the
# snooze/kill offer dicts, the notify coalescer state). Nothing restored them,
# which is how a test comes to fail because of a *different* file that ran
# earlier — the exact class of defect that is invisible in collection order and
# only shows up when the order changes. Restoring the state around every test
# removes the possibility by construction rather than by remembering.
_STATE_GLOBALS = (
    "SETTINGS", "_kill_offer", "_snooze_offer", "_NOTIFY_STATE",
    "OLLAMA_BASE", "OLLAMA_MODEL", "_READER_APP_LAST",
    # The loaded-model mirrors. A daemon worker that outlives its test (the
    # stop-probe transcribes audio; the loader warms whisper/tts) can finish
    # AFTER the monkeypatch that stubbed it has been reverted, load the real
    # model and publish it here — measured: `stt: whisper loaded` in `--ptt
    # doctor` for every test that ran later. Restoring the mirror around each
    # test means a leaked load cannot change what the NEXT test observes.
    "_whisper_model", "_tts_model", "TTS_REFERENCE", "TTS_ENGINE",
    "WHISPER_SIZE", "WHISPER_DEVICE",
    # The tool-support memory and the prompt-token cache derived from it. A turn
    # against a model that refuses a tools payload records the refusal HERE, on
    # the module, for the rest of the process — so a test that drove such a turn
    # decided for every later test whether "the model already knows tools".
    # Measured: with HANDSOFF_TEST_ORDER_SEED=20260914,
    # test_a_model_that_refuses_tools_is_remembered_not_re_probed asserted a
    # fresh True and got a False recorded by an earlier file.
    "_BRAIN_STATE", "_TOOLS_SUPPORTED", "_FIXED_PROMPT_TOKENS",
)

# Worker threads that always have an explicit stop path. Anything here still
# running after the test that started it has finished is a leak, not a slow
# exit: the settle loop below waits for a legitimate unwind first.
_WORKER_THREADS = ("watch-file", "watch-process", "pomodoro",
                   "notification-reader", "control", "drain-",
                   # Speaks on its own channel (job completions, resource
                   # crossings, cap refusals). A test that lets one run waits
                   # 30s on the models-ready event before it even synthesizes,
                   # so a lingering announce worker is a leak with a fuse.
                   "announce",
                   # Both load or transcribe models on a daemon thread. A test
                   # that leaves one running can finish a load after its own
                   # monkeypatch is gone — a real multi-GB load in the suite,
                   # writing into globals the next test reads. Waiting here
                   # fails that test instead of the one that follows it.
                   "stop-probe", "loader")


def _is_restorable(obj) -> bool:
    """A registry/offer restores itself.

    `core.registry.Offer` carries a lock, so it cannot be deep-copied — and the
    identity fallback below would then leave one test's armed offer live for
    every test after it. Snapshotting its CONTENTS is the only way to hand the
    next test the same state its neighbours saw.
    """
    return (callable(getattr(obj, "snapshot_state", None))
            and callable(getattr(obj, "restore_state", None)))


def _snapshot_state(H) -> dict:
    snap = {}
    for name in _STATE_GLOBALS:
        if not hasattr(H, name):
            continue
        obj = getattr(H, name)
        if _is_restorable(obj):
            snap[name] = (obj, obj.snapshot_state())
            continue
        if isinstance(obj, (dict, list)):
            try:
                snap[name] = (obj, copy.deepcopy(obj))
                continue
            except Exception:
                pass
        # Scalars restore by identity, and a loaded whisper/chatterbox model
        # MUST: deep-copying one would clone gigabytes for no benefit, since
        # `_restore_state` re-assigns the original object anyway.
        snap[name] = (obj, obj)
    return snap


def _restore_state(H, snap: dict) -> None:
    for name, (obj, value) in snap.items():
        if getattr(H, name, None) is not obj:
            setattr(H, name, obj)        # a test swapped the object out
        if _is_restorable(obj):
            obj.restore_state(value)
        elif isinstance(obj, dict) and isinstance(value, dict):
            obj.clear()
            obj.update(value)
        elif isinstance(obj, list) and isinstance(value, list):
            obj[:] = value
        else:
            setattr(H, name, value)


@pytest.fixture(autouse=True)
def _module_state_is_restored(H):
    """Hand every test the same module state its neighbours saw."""
    snap = _snapshot_state(H)
    yield
    _restore_state(H, snap)


#: Every thread a bubble or its listener starts. Named here so the teardown
#: below can ask "is any of this still running?" with one `threading.enumerate()`
#: — cheap enough for all 1400 tests — and walk the garbage collector only when
#: the answer is yes.
_BUBBLE_THREADS = frozenset({
    "mic-health",       # ContinuousListener.__init__
    "handsfree",        # ContinuousListener.start()
    "pipeline",         # Assistant.__init__: unpacks a turn and runs it
    "loader",           # Assistant.start(): models and warm-up
    "reminders", "settings-watch", "missed-reminders", "announce",
})


def _live_bubble_threads() -> set:
    """Ids of the live threads a bubble owns. Threads, not objects: the thread is
    what leaks, and it is observable whether or not anything still points at the
    listener that started it."""
    return {id(t) for t in threading.enumerate()
            if t.is_alive() and t.name in _BUBBLE_THREADS}


def _fold_up_bubbles(H, built) -> None:
    """Close every listener, and shut down every bubble, still alive in here.

    Listeners are found by walking the garbage collector rather than through a
    registry: the product has no business keeping a list of its objects for the
    tests' sake, and a leaked one is exactly the object that is NOT registered
    anywhere — it was dropped while its thread kept running.

    Bubbles come from `built`, the set the constructor watcher fills. Sniffing
    for a "real enough" bubble by its attributes was tried and measured: ~20
    tests build an assistant with `__new__` on purpose, some of them leaving it
    half-built and one injecting a `_shutdown_event` whose `set()` RAISES to
    exercise a failure path — calling shutdown() on those two failed teardown
    for a reason that had nothing to do with threads. Only `__init__` spawns the
    threads, so only `__init__` is the marker.
    """
    listener_cls = getattr(H, "ContinuousListener", None)
    for obj in gc.get_objects():
        if listener_cls is not None and type(obj) is listener_cls:
            obj.close()
    for bubble in list(built):
        if not getattr(bubble, "_closed", False):
            bubble.shutdown()


@pytest.fixture(scope="session")
def _bubbles_built(H):
    """Every Assistant that actually ran `Assistant.__init__`, for teardown.

    A constructor watcher rather than a heuristic, because the marker has to be
    exact: the `pipeline` worker and the mic reporter exist only in a bubble the
    constructor built, and those are the threads this teardown is folding up.
    """
    built: "weakref.WeakSet" = weakref.WeakSet()
    real_init = H.Assistant.__init__

    @functools.wraps(real_init)
    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        built.add(self)

    H.Assistant.__init__ = init
    try:
        yield built
    finally:
        H.Assistant.__init__ = real_init


@pytest.fixture(autouse=True)
def _microphones_are_put_down(H, _bubbles_built):
    """Every listener a test builds is CLOSED at teardown; a leak fails the test.

    `ContinuousListener.__init__` spawns a `mic-health` reporter, and until the
    stop path landed NOTHING could end it: `stop()` ends the capture stream,
    `restart()` swaps the capture thread, and the reporter looped forever. The
    suite therefore accumulated them — measured on one shuffled gate run, **4 922
    test boundaries began with a live `mic-health` thread** left behind by an
    earlier file — and a reporter whose 10 s poll came due during a later test
    was steered by whatever that test had patched process-wide (`time.sleep` is
    one module object; `_health_tick` used to be patched on the CLASS). Two
    order dependencies in this suite had exactly that cause.

    Closing here rather than at each construction site is the point: ~20 places
    build an Assistant, and a rule that must be remembered in twenty places is
    forgotten in the twenty-first — and the failure it produces is blamed on the
    test that came next, not on the one that leaked.

    The assertion is the other half. Closing everything means the count must
    return to zero, so a listener that cannot be closed fails HERE, in the test
    that ran it, rather than quietly arming a neighbour.
    """
    before = _live_bubble_threads()
    yield
    if _live_bubble_threads() - before:
        _fold_up_bubbles(H, _bubbles_built)
        still = sorted({t.name for t in threading.enumerate()
                        if t.is_alive() and id(t) not in before
                        and t.name in _BUBBLE_THREADS})
        assert not still, (
            f"{still} outlived this test and could not be stopped: a thread "
            f"that survives its test shares every process-wide seam the next "
            f"one patches (`time.sleep` is one module object; `_health_tick` "
            f"used to be patched on the CLASS), which is how two order "
            f"dependencies got into this suite")


# `core.tools._CURRENT` (and core.doctor's) hold the dependency-injection host:
# whichever handsoff instance loaded — or constructed a ToolBelt — LAST owns them,
# process-wide. Several suites load a SECOND monolith to exercise the settings app,
# and from then on a test that builds a ToolBelt with `__new__` (there are many)
# resolves `_dep().SETTINGS` and `_dep().set_setting` against that foreign instance.
# Measured, not theorised: with `HANDSOFF_TEST_ORDER_SEED=deadbeef` the settings-app
# tests ran first and test_fault_injection's unsaved-toggle test then saw the other
# module's settings — green in collection order, red under the shuffle. Restoring
# the vars around every test removes the ordering dependence at its cause.
_DI_CONTEXTVARS = (("_core_tools", "_CURRENT"), ("_core_doctor", "_CURRENT"))


@pytest.fixture(autouse=True)
def _di_host_is_restored(H):
    """Hand every test THIS monolith as the tool DI host, then restore.

    Restoring the previous value was not enough. A monolith loaded here (the
    settings app, say) can be dropped from `sys.modules` while its deps object
    lives on inside `_DEFAULT_DEPS` — and, if it was imported with the real
    HOME, so do its STATE paths. Measured: `test_ops`' cap-overlap test had its
    four refusals recorded through such an orphan, which appended four entries
    to the developer's real ~/.local/state/handsoff/cap-refusals.json.

    Pinning `_CURRENT` covers the calling thread; worker threads start with an
    empty context and fall through to `_DEFAULT_DEPS`, so that is pinned too.
    
    `_core_doctor` is deliberately NOT pinned: its `_CURRENT` holds a
    DoctorDeps, not a tool host, and only its previous value is restored.
    """
    saved = []
    for mod_name, attr in _DI_CONTEXTVARS:
        var = getattr(getattr(H, mod_name, None), attr, None)
        if var is not None:
            saved.append((var, var.get()))
    tools = getattr(H, "_core_tools", None)
    host = getattr(H, "_tool_dependencies", None)
    prior_default = getattr(tools, "_DEFAULT_DEPS", None) if tools else None
    if tools is not None and host is not None:
        tools._DEFAULT_DEPS = host
        var = getattr(tools, "_CURRENT", None)
        if var is not None:
            var.set(host)
    yield
    for var, value in saved:
        var.set(value)
    if tools is not None and prior_default is not None:
        tools._DEFAULT_DEPS = prior_default


# The ONE name the app is registered under. `core` owns the string (the app,
# the settings app and this harness all compare against the same constant), so
# it is imported rather than respelled here.
_APP_MODULE_NAMES = (APP_MODULE_NAME,)


@pytest.fixture(autouse=True)
def _app_registration_is_restored(H):
    """Put the session's app modules back if a test evicted them.

    Two extraction tests deliberately empty every `handsoff*` entry from
    `sys.modules` to prove `core/doctor` imports without the monolith, and
    nothing put the registration back. The next thing that lazily loads a
    bubble — the settings app's `_LazyHandsoff`, which exec's handsoff.py on
    first attribute use, a moment no loader wraps — then built its OWN copy
    with the developer's real HOME, and that copy's module body calls
    `core.audio.configure(...)`, repointing the SHARED core.audio at the real
    ~/.config/handsoff/whisper-model. Measured under a shuffled order.

    Restoring around every test is the same rule the module-globals and DI-host
    fixtures apply: shared process state a test hand-edits is handed back the
    way it was found.
    """
    present = {name: sys.modules.get(name) for name in _APP_MODULE_NAMES}
    yield
    for name, mod in present.items():
        if mod is not None and sys.modules.get(name) is not mod:
            sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _no_worker_thread_leaks():
    """Fail at the source when a test leaves a stoppable worker running.

    Every thread named below has a stop path, so one that outlives its test is
    a leak. The settle loop gives a legitimate unwind its chance before
    complaining, so this asserts the leak rather than a stopwatch.
    """
    before = {t.ident for t in threading.enumerate()}
    yield
    deadline = time.monotonic() + 2.0
    leaked: list = []
    while True:
        leaked = [t for t in threading.enumerate()
                  if t.ident not in before and t.is_alive()
                  and (t.name or "").startswith(_WORKER_THREADS)]
        if not leaked or time.monotonic() > deadline:
            break
        time.sleep(0.01)
    assert not leaked, (
        "worker thread(s) still running after the test: "
        + ", ".join(f"{t.name!r}" for t in leaked)
        + " — give each one its stop path (stop_watchers / shutdown / reap)")
