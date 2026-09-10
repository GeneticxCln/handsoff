"""handsoff core package — step (a) of the monolith cut plan.

Step (a) moves the settings machinery (single-source schema, coercion,
migration, locking, atomic writes) into an explicit, importable package.
`handsoff.py` remains the application monolith and re-exports every name so
the `H.*` monkeypatch contract and the deployment-hash contract (one file)
are unchanged; later steps peel audio/brain/tools/ui/doctor the same way.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


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


def load_module(mod_name: str):
    """Load one of handsoff's supporting modules.

    One shared order: beside-this-file -> ~/.local/bin -> plain import
    (repo checkouts/tests, where the repo root is on sys.path), all under
    one origin rule — only a module living in an allowed dir is accepted,
    so a foreign module planted in sys.modules or on sys.path can never
    satisfy us. The sys.modules names are only filled when absent or
    same-origin (never clobbering a foreign entry, never swapping under a
    live foreign submodule), and a failed exec restores whatever was there
    (no half-initialized squat).

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
        if prev_bare is None or _origin_ok(prev_bare):
            sys.modules[mod_name] = mod          # canonical name
        sys.modules[f"core.{mod_name}"] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
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


_schema = load_module("settings_schema")
DEFAULT_SETTINGS = _schema.DEFAULT_SETTINGS
SETTINGS_VERSION = _schema.SETTINGS_VERSION
