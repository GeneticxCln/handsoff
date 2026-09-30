"""handsoff core package — step (a) of the monolith cut plan.

Step (a) moves the settings machinery (single-source schema, coercion,
migration, locking, atomic writes) into an explicit, importable package.
`handsoff.py` remains the application monolith and re-exports every name so
the `H.*` monkeypatch contract and the deployment-hash contract (one file)
are unchanged; later steps peel audio/brain/tools/ui/doctor the same way.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- app identity
# The ONE name the application registers ITSELF under, and the only name any
# loader in the tree should have to know. It lives here, beside the other
# module-identity rules (`_origin_ok`, `load_module`), because the app, the
# settings app and every test harness compare against the same string instead of
# each repeating one — and repeated names are how "is the app already loaded?"
# stopped having an answer, so each loader exec'd its own copy:
#
#   conftest            -> "handsoff_core" + a bare "handsoff" alias
#   handsoff-settings   -> "handsoff_core" (its own spec load)
#   offscreen GUI driver-> "handsoff_core_gui"
#   hardening driver    -> "handsoff_no_audio"
#
# Two copies in one process is not two views of one app: it is two apps, each
# with its own CONFIG_DIR/STATE_DIR/SETTINGS and model mirrors, and each module
# body calls `core.audio.configure(...)` — which repoints the SHARED core.audio
# at whichever copy ran last. `handsoff.py` claims this name before it can do
# that, and refuses to run when the name is already held by a live module.
APP_MODULE_NAME = "handsoff_core"


def _repo_root() -> Path | None:
    """The source checkout dir, if discoverable (explicit env or a handsoff.py
    beside this package's parent) — part of the union origin with ~/.local/bin."""
    explicit = os.environ.get("HANDSOFF_SOURCE_PATH")
    if explicit:
        cand = Path(explicit).expanduser()
        cand = cand / "handsoff.py" if cand.is_dir() else cand
        if cand.name == "handsoff.py":
            return cand.parent
    if (_HERE.parent / "handsoff.py").is_file():
        return _HERE.parent
    return None


def _allowed_dirs() -> set[Path]:
    """Dirs a handsoff support module may load from (one origin rule,
    shared with handsoff.py's bootstrap): the union of the source checkout
    (beside this package, i.e. the repo root, or HANDSOFF_SOURCE_PATH) and
    ~/.local/bin — each incl. its core/ subdir."""
    dirs: set[Path] = set()
    for cand in (_HERE, _HERE.parent):
        try:
            dirs.add(cand.resolve())
        except OSError:
            dirs.add(cand)
    repo = _repo_root()
    if repo is not None:
        for cand in (repo, repo / "core"):
            try:
                dirs.add(cand.resolve())
            except OSError:
                dirs.add(cand)
    try:
        bin_dir = Path.home() / ".local" / "bin"
        for cand in (bin_dir, bin_dir / "core"):
            try:
                dirs.add(cand.resolve())
            except OSError:
                dirs.add(cand.absolute())
    except Exception:
        pass
    return dirs


def _origin_ok(mod: object) -> bool:
    """True when a cached module lives in an allowed dir."""
    try:
        parent = Path(getattr(mod, "__file__", "") or "").resolve().parent
    except OSError:
        return False
    return parent in _allowed_dirs()


#: The app module running in THIS process, recorded out of band from
#: `sys.modules`. A registration can be popped — the suite's extraction tests do
#: it deliberately — and the app is still running; the lazy loader that exec'd a
#: second bubble got in through exactly that window. Two records of one fact,
#: and either one is enough to refuse a duplicate.
_APP_INSTANCE = None


def app_instance():
    """The app module recorded for this process, ready or not, or None."""
    return _APP_INSTANCE


def claim_app_instance(mod):
    """Record `mod` as this process's app, refusing a DIFFERENT live one.

    Called by the app itself as it loads. The refusal is the guarantee the
    project keeps re-deriving: two copies in one process are two apps — separate
    SETTINGS, separate model mirrors, and a module body each that calls
    `core.audio.configure(...)`, repointing the SHARED core.audio at whichever
    copy ran last.
    """
    global _APP_INSTANCE
    existing = _APP_INSTANCE
    if existing is not None and existing is not mod:
        raise ImportError(
            "handsoff: refusing to run a SECOND copy of the app in this "
            f"process — the app is already running from "
            f"{getattr(existing, '__file__', '?')}; reuse it "
            "(core.app_instance() / core.app_module()) instead of loading "
            "another")
    _APP_INSTANCE = mod
    return mod


def app_module():
    """The FINISHED app module for this process, or None.

    Anything that needs the app asks here instead of exec'ing the file. A module
    that is still executing its own body is deliberately not returned: a
    half-built app is not a second view of one app, it is a way to read
    `SETTINGS` before the module that owns it has one.
    """
    for mod in (_APP_INSTANCE, sys.modules.get(APP_MODULE_NAME)):
        if mod is None or not _origin_ok(mod):
            continue
        if getattr(mod, "__app_ready__", False):
            return mod
    return None


def load_app_module(candidates):
    """Return the process's ONE app module, exec'ing a candidate at most once.

    `candidates` are tried in order and only existing files are considered.
    The concrete guarantees, because each one is a way the old loaders could
    produce a second app:

    * **An app that is already running is returned, never re-exec'd.** This is
      the whole point: the running copy owns CONFIG_DIR/STATE_DIR/SETTINGS and
      the model mirrors, and a second exec is a second app.
    * **The slot is claimed BEFORE the module executes.** The module therefore
      finds itself in `sys.modules` — `handsoff.py` refuses to run unregistered,
      because an unnamed load cannot be told apart from a duplicate — and two
      racing loaders admit exactly one copy (`setdefault`), the loser receiving
      the winner's module without executing anything.
    * **A load that raises gives the slot back**, so a failed exec cannot leave
      a half-initialised app behind for the next caller to find.
    * **A foreign occupant is refused by name**, using the same origin rule as
      `load_module`, so a module planted under the canonical name can never
      satisfy a loader.
    """
    ready = app_module()
    if ready is not None:
        return ready
    running = _APP_INSTANCE if _APP_INSTANCE is not None \
        else sys.modules.get(APP_MODULE_NAME)
    if running is not None:
        if not _origin_ok(running):
            raise ImportError(
                f"handsoff core: refusing to reuse a foreign "
                f"{APP_MODULE_NAME} ({getattr(running, '__file__', '?')})")
        raise ImportError(
            f"handsoff core: the app is still initialising "
            f"({getattr(running, '__file__', '?')}) — it refuses to load twice")
    for cand in candidates:
        try:
            cand = Path(cand)
            if not cand.is_file():
                continue
        except (OSError, TypeError, ValueError):
            continue
        spec = importlib.util.spec_from_file_location(APP_MODULE_NAME, cand)
        if spec is None or spec.loader is None:
            continue
        fresh = importlib.util.module_from_spec(spec)
        winner = sys.modules.setdefault(APP_MODULE_NAME, fresh)
        if winner is not fresh:
            return winner              # another loader admitted first
        try:
            spec.loader.exec_module(fresh)
        except BaseException:
            if sys.modules.get(APP_MODULE_NAME) is fresh:
                sys.modules.pop(APP_MODULE_NAME, None)
            raise
        return fresh
    raise ImportError(
        "handsoff core: cannot find the application — expected handsoff.py "
        "beside this package, in the checkout root or in ~/.local/bin "
        f"(tried {[str(c) for c in candidates]})")


# Names the interpreter owns. A support module may share one — `calendar`
# does — and binding it BARE would hand our file to every later importer of
# that name, permanently: a third-party `from calendar import timegm` inside
# faster_whisper or chatterbox then raises ImportError and the app reports the
# library as "not installed". Worse, the shadowing was silent and
# order-dependent: the bare binding also ran BEFORE the module's own body, so
# `core/calendar.py`'s own `import calendar` resolved to itself instead of the
# stdlib, which is how the real calendar first failed to load — the loader
# planted the shadow that its own module then read. `core.<name>` is still
# registered either way, and every non-stdlib name keeps its bare alias.
_STDLIB_NAMES = frozenset(getattr(sys, "stdlib_module_names", ()))


def load_module(mod_name: str):
    """Load one of handsoff's supporting modules.

    One shared order: beside-this-file -> ~/.local/bin -> plain import
    (repo checkouts/tests, where the repo root is on sys.path), all under
    one origin rule — only a module living in an allowed dir is accepted,
    so a foreign module planted in sys.modules or on sys.path can never
    satisfy us. The sys.modules names are only filled when absent or
    same-origin (never clobbering a foreign entry, never swapping under a
    live foreign submodule, and never taking a stdlib name — see
    `_STDLIB_NAMES`), and a failed exec restores whatever was there (no
    half-initialized squat).

    Registered as 'core.<name>' so reimports are cached.
    """
    cached = sys.modules.get(f"core.{mod_name}")
    if cached is not None and _origin_ok(cached):
        return cached
    if cached is not None:
        raise ImportError(
            f"handsoff core: refusing to swap foreign live submodule "
            f"core.{mod_name} ({getattr(cached, '__file__', '?')})")
    cached_own = sys.modules.get(mod_name)
    if cached_own is not None and _origin_ok(cached_own):
        sys.modules[f"core.{mod_name}"] = cached_own
        return cached_own
    try:
        bin_cand = Path.home() / ".local" / "bin" / f"{mod_name}.py"
    except Exception:
        bin_cand = None
    cands = [_HERE / f"{mod_name}.py", _HERE.parent / f"{mod_name}.py"]
    if bin_cand is not None and bin_cand not in cands:
        cands.append(bin_cand)
    seen: set[str] = set()
    for cand in cands:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not cand.is_file():
                continue
        except OSError:
            continue
        spec = importlib.util.spec_from_file_location(mod_name, cand)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        prev_bare = sys.modules.get(mod_name)
        prev_core = sys.modules.get(f"core.{mod_name}")
        if prev_core is not None and not _origin_ok(prev_core):
            raise ImportError(
                f"handsoff core: refusing to swap foreign live submodule "
                f"core.{mod_name} ({getattr(prev_core, '__file__', '?')})")
        bind_bare = ((prev_bare is None or _origin_ok(prev_bare))
                     and mod_name not in _STDLIB_NAMES)
        if bind_bare:
            sys.modules[mod_name] = mod          # canonical name
        sys.modules[f"core.{mod_name}"] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            if bind_bare:
                if prev_bare is None:
                    sys.modules.pop(mod_name, None)
                else:
                    sys.modules[mod_name] = prev_bare
            if prev_core is None:
                sys.modules.pop(f"core.{mod_name}", None)
            else:
                sys.modules[f"core.{mod_name}"] = prev_core
            raise
        return mod
    try:
        __import__(mod_name)
        mod = sys.modules[mod_name]
        if _origin_ok(mod):
            sys.modules[f"core.{mod_name}"] = mod
            return mod
    except (ImportError, KeyError, TypeError):
        pass
    raise ImportError(
        f"handsoff core: cannot load {mod_name!r} — expected "
        f"{_HERE / mod_name}.py beside this package or in the checkout root")


# ------------------------------------------------------------ unix-socket paths
# `sockaddr_un.sun_path` holds 108 bytes on Linux (107 plus the NUL). The control
# socket lives under STATE_DIR, so a long HOME — or an XDG_STATE_HOME pointed at a
# deep directory: a container, a CI scratch dir, a test sandbox — made `bind()`
# raise "AF_UNIX path too long". The bubble logged one line and ran on WITHOUT a
# control socket: push-to-talk (`--ptt`, the niri keybinding), the settings app's
# live controls and "reload settings" all failed as "not running".
_SUN_PATH_MAX = 107


@contextlib.contextmanager
def unix_address(path):
    """The string to hand `bind()`/`connect()` for the socket file at `path`.

    A path that fits is passed through untouched. One that does not is reached
    through an `O_PATH` descriptor on its parent directory, addressed as
    `/proc/self/fd/<n>/<name>`: the kernel resolves that to the same file with a
    name of a few bytes, so the socket keeps living in the private state directory
    (no second location to secure, and every path-based check — `lstat`, `chmod`,
    the stale-socket sweep — still sees the real path). The descriptor is open
    only for the duration of the `with`; `bind`/`connect` resolve the name when
    called, so nothing needs it afterwards.
    """
    text = os.fspath(path)
    if len(os.fsencode(text)) <= _SUN_PATH_MAX:
        yield text
        return
    parent, name = os.path.split(text)
    fd = os.open(parent or ".", os.O_PATH | os.O_DIRECTORY)
    try:
        yield f"/proc/self/fd/{fd}/{name}"
    finally:
        os.close(fd)


_schema = load_module("settings_schema")
DEFAULT_SETTINGS = _schema.DEFAULT_SETTINGS
SETTINGS_VERSION = _schema.SETTINGS_VERSION
