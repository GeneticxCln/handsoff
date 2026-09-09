"""Shared fixtures for the handsoff test suite (split out of the old
monolithic test_handsoff.py; see test_*.py modules for the areas)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np   # noqa: F401  (test modules rely on it being imported)
import pytest

HERE = Path(__file__).resolve().parent.parent   # the repo root


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
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def H():
    return _load("handsoff_core", HERE / "handsoff.py")
