"""Shared fixtures for the handsoff test suite (split out of the old
monolithic test_handsoff.py; see test_*.py modules for the areas)."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np   # noqa: F401  (test modules rely on it being imported)
import pytest

# The app's canonical module name, from its ONE definition. Imported at conftest
# import time (before any sandbox): core/__init__.py resolves no user directory
# while it is imported — only inside its functions — so this cannot bake the
# developer's HOME into anything.
from core import APP_MODULE_NAME, app_instance, app_module

from core import registry as _core_registry

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


@contextlib.contextmanager
def isolated_user_dirs(prefix: str = "handsoff-testhome-"):
    """Point HOME/XDG at a throw-away directory for the duration of a load.

    Restored on the way out, so nothing else in the suite (subprocess
    environments, path helpers like `_user_site`) moves. Not deleted: the
    loaded module's constants point into it for the rest of the session, and a
    test that writes through them is writing into the sandbox — which is the
    point.
    """
    home = tempfile.mkdtemp(prefix=prefix)
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


def sandbox_env(home=None) -> dict:
    """A child-process environment whose user dirs are a throw-away directory.

    A driver that loads a monolith in a SUBPROCESS resolves the same paths, so
    it needs the same sandbox — and a redirected HOME hides user site-packages,
    which is where PySide6 and sounddevice live, so the real user-site path is
    kept on PYTHONPATH alongside the repo root.
    """
    env = dict(os.environ)
    home = Path(home or tempfile.mkdtemp(prefix="handsoff-testhome-"))
    env.update({
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    })
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(HERE), _user_site(), env.get("PYTHONPATH", "")) if p)
    return env


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
