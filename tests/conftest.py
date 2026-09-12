"""Shared fixtures for the handsoff test suite (split out of the old
monolithic test_handsoff.py; see test_*.py modules for the areas)."""
from __future__ import annotations

import copy
import importlib.util
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np   # noqa: F401  (test modules rely on it being imported)
import pytest

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


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register the module in sys.modules. Without this it is absent, so a later
    # `import handsoff` built a SECOND instance with its own SETTINGS, locks and
    # caches — divergence that surfaces as order-dependent flakes rather than as
    # an error. Only handsoff.py gets the bare-name alias: doing it for every
    # module would put `core/audio.py` into sys.modules as "audio" and shadow
    # real imports.
    bare = "handsoff" if Path(path).name == "handsoff.py" else None
    sys.modules[name] = mod
    if bare:
        sys.modules.setdefault(bare, mod)
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        # never leave a half-initialised module behind for the next test
        sys.modules.pop(name, None)
        if bare and sys.modules.get(bare) is mod:
            sys.modules.pop(bare, None)
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
    home = tempfile.mkdtemp(prefix="handsoff-testhome-")
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
    os.environ["HOME"] = home
    os.environ["XDG_STATE_HOME"] = str(Path(home) / ".local" / "state")
    os.environ["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
    try:
        return _load("handsoff_core", HERE / "handsoff.py")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session")
def H():
    return _import_app_with_isolated_config()


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
)

# Worker threads that always have an explicit stop path. Anything here still
# running after the test that started it has finished is a leak, not a slow
# exit: the settle loop below waits for a legitimate unwind first.
_WORKER_THREADS = ("watch-file", "watch-process", "pomodoro",
                   "notification-reader", "drain-")


def _snapshot_state(H) -> dict:
    snap = {}
    for name in _STATE_GLOBALS:
        if not hasattr(H, name):
            continue
        obj = getattr(H, name)
        try:
            snap[name] = (obj, copy.deepcopy(obj))
        except Exception:
            snap[name] = (obj, obj)      # uncopyable: identity restore only
    return snap


def _restore_state(H, snap: dict) -> None:
    for name, (obj, value) in snap.items():
        if getattr(H, name, None) is not obj:
            setattr(H, name, obj)        # a test swapped the object out
        if isinstance(obj, dict) and isinstance(value, dict):
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
    saved = []
    for mod_name, attr in _DI_CONTEXTVARS:
        var = getattr(getattr(H, mod_name, None), attr, None)
        if var is not None:
            saved.append((var, var.get()))
    yield
    for var, value in saved:
        var.set(value)


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
