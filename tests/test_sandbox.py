"""The suite must never resolve the developer's real config or state.

Every module that resolves CONFIG_DIR/STATE_DIR at import bakes whatever HOME it
sees into module constants, so a load is the moment the sandbox has to be in
place — not a fixture, not a convention at the call site. The first load was
sandboxed; a SECOND monolith (the settings app, loaded in-process by several
suites, which also derives `NIRI_CONFIG` — the file `apply_autostart` writes)
was not, so its paths were the developer's, and what a test observed depended on
whose machine ran the suite. These tests pin the FOUR ways that can regress: the
in-process load, the child-process driver, a new hand-built loader, and an
import STATEMENT — the one load no loader can wrap, because the sandbox belongs
to `conftest._load` and a test module is imported by pytest.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

import core
from core import APP_MODULE_NAME, app_instance, app_module, load_app_module

from conftest import HERE as ROOT, _GUARD_ENV, _PROTECTED_ENV, _REAL_HOME, \
    _REAL_USER_DIRS, _checkout_write_target, _load, _user_site, forbidden_write, \
    guarded_user_dirs, isolated_user_dirs, run_driver, sandbox_env, \
    writes_into_the_checkout

HERE = ROOT

# Path constants a loaded monolith carries. Kept as a list of NAMES so a module
# that renames one shows up as "nothing was checked" rather than as a silent
# pass.
PATH_NAMES = ("HOME", "CONFIG_DIR", "STATE_DIR", "SETTINGS_FILE", "HISTORY_FILE",
              "MEMORY_FILE", "DECISIONS_FILE", "LOG_FILE", "CONTROL_SOCK",
              "CAP_EVENTS_FILE", "MIC_EVENTS_FILE", "REMINDERS_FILE",
              "NIRI_CONFIG",
              # core/audio.py's only import-time path, and the reason it is in
              # the banned-import set: it used to land in the developer's
              # whisper-model dir.
              "WHISPER_MODEL_DIR")

# Strings that mean "this child imports or loads the application", whether they
# sit in the argv literal or in a `-c` payload held in a local.
APP_LOAD_MARKERS = ("spec_from_file_location", "module_from_spec", "handsoff",
                    "from core", "import core")


def _real_home() -> Path:
    """The developer's home, captured before any sandbox ran.

    conftest records it at import; re-deriving it from the environment would
    answer with whichever throw-away HOME happens to be live.
    """
    return _REAL_HOME


def _test_trees():
    """Every test module, parsed — the guard must see the whole suite."""
    for path in sorted((HERE / "tests").glob("test_*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _own_calls(func):
    """Calls in THIS function — a nested def gets judged on its own."""
    out = []
    stack = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _subprocess_calls():
    """Every `subprocess.run`/`Popen` in the suite, with its context.

    Yields (path, node, sandboxed, locals). `sandboxed` says whether the
    function reaches the sandbox at all — `conftest.run_driver` for a child
    that must run to completion, `sandbox_env` for one that has to keep
    running (the bubble, the fake Ollama). A call site either applies the
    sandbox or is reported, which is the difference between a rule and a
    convention nobody can forget.
    """
    for path, tree in _test_trees():
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _own_calls(func)
            sandboxed = any(_is_call_to(c, "sandbox_env", None)
                            or _is_call_to(c, "run_driver", None) for c in calls)
            names = _local_strings(func)
            for node in calls:
                if (_is_call_to(node, "subprocess", "run")
                        or _is_call_to(node, "subprocess", "Popen")):
                    yield path, node, sandboxed, names


def _is_call_to(node, name: str, attr) -> bool:
    """`subprocess.run(...)`, `sandbox_env(...)`, `run_driver(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if attr is None:
        return isinstance(func, ast.Name) and func.id == name
    return (isinstance(func, ast.Attribute) and func.attr == attr
            and isinstance(func.value, ast.Name) and func.value.id == name)


def _local_strings(tree) -> dict:
    """One level of `name = "…"` (including implicit concatenation).

    The payload of a `python -c` child is usually a local, and a check that
    only reads literal arguments is satisfied while the property it asserts is
    false a variable away.
    """
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            text = _string_value(node.value)
            if text is not None:
                found[node.targets[0].id] = text
    return found


def _string_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left), _string_value(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _payload_text(node, names) -> str:
    """Every string the call is built from, plus any local it names."""
    parts = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append(sub.value)
        elif isinstance(sub, ast.Name) and sub.id in names:
            parts.append(names[sub.id])
    return " ".join(parts)


def _module_scope(tree):
    """The statements that run when a module is IMPORTED (collection time).

    Deliberately does not descend into `def`/`class` bodies: an import inside a
    test function runs after conftest's autouse sandbox fixtures have loaded the
    bubble, which is a different moment from the module being read by pytest.
    """
    out, stack = [], list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _bakes_user_dirs_at_import(path: Path) -> bool:
    """True when a module resolves HOME/XDG while it is being IMPORTED.

    Discovered from the source rather than listed, so a NEW module that bakes
    paths at import is covered the moment it exists — the failure mode is
    silent and machine-dependent (it only leaks on a machine that has real
    config), so a hand-kept list is exactly how it would come back.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in _module_scope(tree):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == "home":
                return True
            if isinstance(sub, ast.Call) and \
                    getattr(getattr(sub, "func", None), "attr", "") == "expanduser":
                return True
    return False


def _unimportable_names() -> set[str]:
    """Every importable name that would bake the developer's user dirs.

    `core/tools.py` is reachable as both `import core.tools` and `from core
    import tools`, so the IMPORT FROM form is resolved into the same dotted name
    by :func:`_collection_imports`. A filename that is not an identifier (the
    settings app) cannot be imported at all, so it is reached only through a
    spec loader — which the `module_from_spec` guard already bans.
    """
    banned: set[str] = set()
    for path in sorted(HERE.glob("*.py")) + sorted((HERE / "core").glob("*.py")):
        if not _bakes_user_dirs_at_import(path):
            continue
        if path.stem.isidentifier():
            banned.add(f"core.{path.stem}" if path.parent.name == "core"
                       else path.stem)
    return banned


def _collection_imports(path: Path):
    """``(lineno, resolved_name)`` for every import that runs at COLLECTION.

    Both halves of an `ImportFrom` are recorded, because the two ways to reach
    the same module look different: `from core.audio import X` names it in
    `module`, while `from core import audio` only names the package and puts the
    module in `names`. Checking the base's PREFIX covers the first form.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in _module_scope(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue                    # relative: cannot name an app module
            base = node.module or ""
            if base:
                out.append((node.lineno, base))
            for alias in node.names:
                out.append((node.lineno, f"{base}.{alias.name}" if base
                            else alias.name))
    return out


def _imports_banned(name: str, banned: set[str]) -> bool:
    """`core.tools` and `core.tools.anything` are both the banned module."""
    return name in banned or any(name.startswith(f"{b}.") for b in banned)





class TestInProcessLoadsAreSandboxed:
    def test_the_settings_monolith_cannot_see_the_developer_config(self):
        """The leak this exists for: `_load("handsoff_settings", …)`.

        The settings app resolves CONFIG_DIR/SETTINGS_FILE *and* the niri config
        it writes keybind snippets into from HOME at import. Loaded with the real
        HOME, a test read the developer's settings — and a test that exercised
        the autostart path would have written the developer's niri config.
        """
        real = _real_home()
        mod = _load("sandbox_settings_probe", HERE / "handsoff-settings.py")
        checked = []
        for name in PATH_NAMES:
            value = getattr(mod, name, None)
            if value is None or not isinstance(value, (str, Path)):
                continue
            checked.append(name)
            assert not str(value).startswith(str(real)), (
                f"the loaded settings app resolved {name}={value} inside the "
                f"developer's home {real}")
        assert "HOME" in checked and "NIRI_CONFIG" in checked, checked

    def test_loading_the_settings_app_does_not_exec_a_second_bubble(self, H):
        """The app's lazy bubble must be the one the sandbox already loaded.

        `_LazyHandsoff` exec's handsoff.py on first attribute use — a moment no
        loader wraps, because the caller decides when. A second copy is not a
        second view of one app: it is a second app with its own
        CONFIG_DIR/STATE_DIR/SETTINGS, and its module body calls
        `core.audio.configure(...)`, which repoints the SHARED core.audio at
        that copy's paths. Measured under a shuffled order: `--ptt doctor`'s
        whisper dir in this very process became the developer's real
        ~/.config/handsoff/whisper-model.
        """
        app = _load("sandbox_settings_lazy", HERE / "handsoff-settings.py")
        before = sys.modules[APP_MODULE_NAME]
        import core.audio as audio_mod          # the SHARED copy both apps use
        whisper = Path(audio_mod.WHISPER_MODEL_DIR)
        assert not whisper.is_relative_to(_real_home()), whisper
        assert before is H
        assert app.H.SAMPLE_RATE == before.SAMPLE_RATE   # first dereference
        assert app.H._load() is before, \
            "the settings app's lazy bubble is not the running app"
        assert sys.modules[APP_MODULE_NAME] is before, \
            "the settings app exec'd a second bubble in this process"
        assert Path(audio_mod.WHISPER_MODEL_DIR) == whisper, \
            ("a second bubble repointed the shared core.audio at "
             f"{audio_mod.WHISPER_MODEL_DIR}")

    def test_the_loaded_app_is_the_sandboxed_one(self, H):
        """The app's user dirs are the throw-away HOME, and it is the ONLY one.

        This used to load a second bubble to check its paths, which is the very
        thing the canonical registration now refuses — so it checks the app the
        session actually runs on, and separately that asking for another load
        hands back THIS module rather than a fresh one.
        """
        real = _real_home()
        checked = []
        for name in PATH_NAMES:
            value = getattr(H, name, None)
            if value is None or not isinstance(value, (str, Path)):
                continue
            checked.append(name)
            assert not str(value).startswith(str(real)), \
                f"the loaded app resolved {name}={value} in the real home"
        assert "CONFIG_DIR" in checked and "STATE_DIR" in checked, checked
        again = _load("sandbox_core_probe", HERE / "handsoff.py")
        assert again is H, (
            "asking for the app again exec'd a second copy — there is one app "
            "per process and _load must hand back the running one")

    def test_the_load_gives_the_environment_back(self):
        """The sandbox lasts exactly as long as the load.

        Everything else in the suite — subprocess environments, `_user_site`,
        the paths a test builds by hand — expects the real HOME back once the
        module is in.
        """
        before = {k: os.environ.get(k) for k in
                  ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
        _load("sandbox_env_probe", HERE / "hardware.py")
        assert {k: os.environ.get(k) for k in before} == before

    def test_the_sandbox_is_restored_even_when_a_load_raises(self, tmp_path):
        """A module that fails to import must not strand HOME.

        The failing load is the interesting case: if the restore only happened
        on the success path, every later test in the session would resolve the
        throw-away HOME (or the temp dir would leak into the environment).

        The broken module is built in the test's own fixture: the loader takes a
        path, so where the file lives changes nothing, while a module written
        under `tests/` is a write into the shared checkout — and one that a
        killed run leaves behind.
        """
        before = {k: os.environ.get(k) for k in
                  ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
        broken = tmp_path / "_sandbox_boom.py"
        broken.write_text("raise RuntimeError('boom')\n")
        with pytest.raises(RuntimeError):
            _load("sandbox_boom_probe", broken)
        assert {k: os.environ.get(k) for k in before} == before
        assert "sandbox_boom_probe" not in sys.modules

    def test_a_write_through_a_loaded_module_stays_in_the_sandbox(self):
        """The point of the isolation: writes cannot reach the real config."""
        mod = _load("sandbox_write_probe", HERE / "handsoff-settings.py")
        target = Path(mod.NIRI_CONFIG)
        # Refuse BEFORE the write, not after. The assertion below is a report;
        # this one is the safety catch. Measured, the hard way: an unisolated
        # load of this module wrote the developer's real ~/.config/niri/
        # config.kdl, and checking afterwards is one write too late.
        assert not target.is_relative_to(_real_home()), (
            f"the load resolved NIRI_CONFIG at {target} — refusing to write "
            f"outside the sandbox (real home {_real_home()})")
        before = target.stat().st_mtime_ns if target.exists() else None
        mod.NIRI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        mod.NIRI_CONFIG.write_text("// written by the suite\n")
        after = target.stat().st_mtime_ns if target.exists() else None
        assert before != after, "the sandbox write did not land where it should"
        real_niri = _real_home() / ".config" / "niri" / "config.kdl"
        if real_niri.exists():
            assert "written by the suite" not in real_niri.read_text(
                encoding="utf-8", errors="replace"), \
                "a test wrote the developer's niri config"


class TestNoTestWritesInTheCheckout:
    """The suite may not write into the tree it is running against.

    The sibling of the sandbox above, and it fails the same way: not as an error
    at the time, but as a file in a SHARED checkout that nobody meant to leave
    there. Two tests used to do it — one seeded `.venv` and skipped itself
    whenever a real one existed (which is the machine where the pruning it tests
    matters most), the other planted a scratch module beside `handsoff.py` and
    removed it in a `finally`, so an interrupted run left behind exactly the
    untracked file the suite's own lifecycle guard then failed on.

    conftest installs the hook (see the guard beside the user-dir sandbox); these
    tests are its teeth, because a guard that stopped refusing would otherwise be
    indistinguishable from a suite that had nothing to refuse.
    """

    def test_a_write_into_the_checkout_is_refused_before_it_lands(self):
        probe = ROOT / "zz_suite_write_probe.py"
        with pytest.raises(AssertionError, match="wrote inside the checkout"):
            probe.write_text("a test's file\n", encoding="utf-8")
        assert not probe.exists(), (
            "the refusal has to arrive BEFORE the write: a guard that reports "
            "afterwards has already left the file in the checkout")

    def test_removing_a_checkout_file_is_refused_too(self):
        """Create-then-delete is the shape BOTH incidents had, and the one no
        comparison of the tree before and after a test can see."""
        doomed = ROOT / "conftest-probe-that-is-not-there"
        with pytest.raises(AssertionError, match="wrote inside the checkout"):
            doomed.unlink()      # the guard refuses before the FileNotFoundError

    def test_making_a_directory_in_the_checkout_is_refused(self):
        with pytest.raises(AssertionError, match="wrote inside the checkout"):
            (ROOT / "zz_suite_probe_dir").mkdir()
        assert not (ROOT / "zz_suite_probe_dir").exists()

    def test_reading_the_checkout_is_not_a_write(self):
        """The other half of the property: every guard in this suite READS the
        tree, and a hook that refused those would fail the whole file."""
        assert (ROOT / "pytest.ini").read_text(encoding="utf-8")
        assert list((ROOT / "core").glob("*.py"))

    def test_the_flags_decide_not_the_path(self):
        """Pinned on the event, because a mutant that judges the path alone does
        not fail HERE — it fails at COLLECTION, since pytest reads the test files
        themselves, and a red for the wrong reason is not a guard."""
        target = str(ROOT / "pytest.ini")
        assert _checkout_write_target("open", (target, "r", os.O_RDONLY)) == ""
        assert _checkout_write_target(
            "open", (target, "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)) == target
        assert _checkout_write_target(
            "open", (str(ROOT / "__pycache__" / "x.pyc"), "w",
                     os.O_WRONLY | os.O_CREAT)) == ""

    def test_a_dir_fd_relative_name_is_resolved_against_its_directory(self, tmp_path):
        """`shutil.rmtree` unlinks by BARE NAME against a directory fd, so the
        name on its own says nothing: resolved against the cwd it would judge a
        fixture under /tmp as if it were inside the checkout, which is every test
        that tidies up after itself."""
        (tmp_path / "kept.txt").write_text("x", encoding="utf-8")
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            assert _checkout_write_target("os.remove", ("kept.txt", fd)) == ""
        finally:
            os.close(fd)
        # ...and the same bare name with no fd IS the checkout's own cwd, which a
        # test has no business writing into.
        assert _checkout_write_target("os.remove", ("kept.txt", -1)) == \
            str(ROOT / "kept.txt")

    def test_a_fixture_may_symlink_the_checkout_into_itself(self, tmp_path):
        """A symlink stores its target as TEXT and never touches it, so a fixture
        tree linking the app's modules in is not writing into the checkout —
        judging the target refused every suite that builds such a tree."""
        link = tmp_path / "core"
        link.symlink_to(ROOT / "core")
        assert link.is_symlink()
        # ...and the same for a target that is not there YET, which a fixture may
        # link before it creates it: judging the target would refuse this, and an
        # existing target cannot show it, because a creation that can only fail
        # is allowed anyway.
        ghost = tmp_path / "ghost"
        ghost.symlink_to(ROOT / "core" / "not_there_yet.py")
        assert ghost.is_symlink() and not ghost.exists()

    def test_a_link_created_inside_the_checkout_is_refused(self, tmp_path):
        with pytest.raises(AssertionError, match="wrote inside the checkout"):
            (ROOT / "zz_suite_probe_link").symlink_to(tmp_path)
        assert not (ROOT / "zz_suite_probe_link").is_symlink()

    def test_a_creation_that_cannot_succeed_is_not_a_write(self):
        """`os.makedirs(exist_ok=True)` — how pytest makes sure the junit report's
        directory is there — reaches `os.mkdir` on a directory that already
        exists, where the call can only fail and nothing is written. Pinned on
        the event, because getting this wrong does not fail a test: it stops the
        suite from STARTING under `ci/gates.sh`, which is how it was found."""
        existing = str(ROOT / "tests")
        assert os.path.isdir(existing)
        assert _checkout_write_target("os.mkdir", (existing, 0o777, -1)) == ""
        fresh = str(ROOT / "zz_suite_probe_dir")
        assert _checkout_write_target("os.mkdir", (fresh, 0o777, -1)) == fresh
        # ...and removing something that IS there can succeed, so it is a write.
        assert _checkout_write_target("os.rmdir", (existing, -1)) == existing

    #: The same child, run three ways. It takes the checkout as an ARGUMENT rather
    #: than reading the guard variable, because that variable is the wiring under
    #: test and a driver that needed it could not be the control for it.
    CHILD_PROBE = '''\
import pathlib, sys
child = sys.modules.get("checkout_guard")
print("root:", child.root() if child else "NONE")
probe = pathlib.Path(sys.argv[1]) / "zz_child_write_probe.txt"
try:
    probe.write_text("child", encoding="utf-8")
    print("WROTE")
except AssertionError as exc:
    print("REFUSED:", str(exc).splitlines()[0])
print("exists:", probe.exists())
if probe.exists():      # a guard that went quiet must still leave the tree clean
    probe.unlink()
    print("cleaned")
'''

    def test_a_child_the_suite_spawns_is_refused_too(self):
        """The half the first version of this guard stated as a limit, and the
        half that matters most: the suite's children are where most of its
        behaviour runs — every offscreen GUI scenario, every `run_driver` driver
        — so a property that stopped at the parent's process stopped short of
        the interesting part. `sitecustomize` is what carries it: the one hook
        CPython runs in every interpreter at start-up, whatever the argv is."""
        proc = run_driver(["-", str(ROOT)], input=self.CHILD_PROBE,
                          capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "REFUSED:" in proc.stdout, (
            "the child wrote into the checkout with no guard in it:\n"
            + proc.stdout)
        assert f"root: {ROOT}" in proc.stdout, (
            "the child's guard has to judge the SAME tree this process does: one "
            "pointed at the parent directory would refuse this write too, and "
            "would also refuse the legitimate writes beside the checkout")
        assert str(ROOT / "zz_child_write_probe.txt") in proc.stdout
        assert "exists: False" in proc.stdout
        assert not (ROOT / "zz_child_write_probe.txt").exists()

    def test_the_variable_is_what_the_child_guard_hangs_on(self):
        """The control for the test above, without which it would pass for a child
        that simply cannot write anywhere. The SAME child, with the one variable
        removed and nothing else changed — the shim is still first on its path —
        writes the file and tidies up. That is what makes the pairing evidence:
        the shim's presence is not the guard, the suite asking for it is."""
        env = sandbox_env()
        env.pop(_GUARD_ENV)
        proc = subprocess.run([sys.executable, "-", str(ROOT)],
                              input=self.CHILD_PROBE, env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert "root: NONE" in proc.stdout, (
            "the shim armed itself with no variable to hang on: it sits on every "
            "child's path, so it may only act when the suite asks it to")
        assert "WROTE" in proc.stdout and "exists: True" in proc.stdout
        assert "cleaned" in proc.stdout, (
            "the unguarded child was to clean up after itself, so this control "
            "leaves the checkout as it found it")
        assert not (ROOT / "zz_child_write_probe.txt").exists()

    def test_the_child_environment_carries_the_shim_and_the_root(self):
        env = sandbox_env()
        assert env[_GUARD_ENV] == str(ROOT)
        first = env["PYTHONPATH"].split(os.pathsep)[0]
        assert Path(first, "sitecustomize.py").is_file(), (
            "the shim has to be FIRST on the path: one that comes later is one a "
            "developer's own sitecustomize shadows, and the guard never installs")
        shim = Path(first, "sitecustomize.py").read_text(encoding="utf-8")
        assert "checkout_guard" in shim
        # ONE definition: the shim POINTS at the module rather than restating the
        # rules, because two copies drift and the child's would be the wrong one.
        assert "_WRITE_EVENTS" not in shim and "os.O_WRONLY" not in shim
        assert Path(ROOT, "tests", "checkout_guard.py").is_file(), (
            "the shim loads this file by path from the checkout it is told about")

    def test_the_shim_is_not_measured_by_the_coverage_gate(self):
        """A child that starts coverage under COVERAGE_PROCESS_START measures the
        shim too — a one-module file in a scratch directory — and the report then
        refuses to print a TOTAL for it: measured, the first gate run with guarded
        children printed `No source for code: /tmp/handsoff-guard-*/sitecustomize.py`
        instead. Pinned because the omission is invisible until the next gate run."""
        text = (ROOT / ".coveragerc").read_text(encoding="utf-8")
        assert "*/handsoff-guard-*/sitecustomize.py" in text, (
            "the shim's scratch directory is not omitted from coverage, so the "
            "coverage gate will refuse a TOTAL over a file outside the checkout")

    def test_the_predicate_judges_the_boundary_and_the_artifacts(self):
        """The decision table, one row per way it could be wrong."""
        table = [
            # A path under the checkout is a write into it.
            (ROOT / "handsoff.py", True),
            (ROOT / "tests" / "test_sandbox.py", True),
            (ROOT / "core" / "tools.py", True),
            # A SIBLING whose name happens to start with the checkout's path is
            # not inside it — the prefix trap the tools' include-walk had.
            (Path(str(ROOT) + "-evil") / "x.py", False),
            (Path("/tmp") / "x", False),
            # Gitignored artifacts of RUNNING the suite (pytest, coverage).
            (ROOT / "__pycache__" / "x.pyc", False),
            (ROOT / "tests" / "__pycache__" / "x.pyc", False),
            (ROOT / ".coverage", False),
            (ROOT / ".coverage.host.1234.abcd", False),
            (ROOT / ".pytest_cache" / "v" / "cache" / "lastfailed", False),
            (ROOT / "tests" / "report.xml", False),
            (ROOT / "tests" / "report.first-failure.xml", False),
            # pytest builds `.pytest_cache` in this directory and renames it in.
            (ROOT / "pytest-cache-files-abcd1234", False),
            (ROOT / "pytest-cache-files-abcd1234" / "CACHEDIR.TAG", False),
            # ...but only the exact names, and only at the root: anything else
            # beside them is a test's own file.
            (ROOT / ".coverage-backup", True),
            (ROOT / "tests" / "report.py", True),
            (ROOT / "tests" / "notes.xml", True),
            (ROOT / "tests" / "pytest-cache-files-abcd1234", True),
            (ROOT / "pytest-cache-files", True),
        ]
        for path, forbidden in table:
            assert bool(writes_into_the_checkout(path)) is forbidden, (
                f"{path}: expected {'refused' if forbidden else 'allowed'}")

    def test_a_links_source_is_read_but_its_destination_is_written(self, tmp_path):
        """The source/destination rule, pinned on the EVENTS rather than by doing
        it: a hard link needs both names on one filesystem, and a fixture lives
        under /tmp while the checkout does not — `os.link` then fails with EXDEV,
        a red that has nothing to do with the guard. (Measured: it did exactly
        that, which is why this test asserts on the decision instead.)"""
        # The source is inside the checkout and NOT THERE: a path the decision
        # must ignore whether or not it exists (a fixture may link a module it is
        # about to create). Naming an existing one instead would hide a mutant
        # that judged the source behind the rule for creations that cannot
        # succeed — measured, that is exactly what happened.
        source = str(ROOT / "zz_suite_probe_source")
        destination = str(tmp_path / "pytest.ini")      # outside the checkout
        absent = str(ROOT / "zz_suite_probe_link")      # inside it, and not there
        for event in ("os.link", "os.symlink", "shutil.copyfile",
                      "shutil.copystat", "shutil.copymode"):
            assert _checkout_write_target(event, (source, destination, -1)) == "", (
                f"{event} judged its SOURCE as a write")
            assert _checkout_write_target(event, (destination, absent, -1)) == absent, (
                f"{event} did not judge its destination as a write")

    def test_a_fixture_the_test_built_may_be_written(self, tmp_path):
        """The property is about WHERE, not about writing at all — a test's own
        fixture is the fix this guard exists to force, so it must stay writable."""
        target = tmp_path / "artifact.txt"
        target.write_text("mine\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "kept.txt").write_text("x", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "mine\n"
        target.unlink()
        # ...and so must the CLEANUP paths: `shutil.rmtree` walks a tree with a
        # directory fd and unlinks by BARE NAME, so a guard that resolved those
        # names against the cwd would refuse every test that tidies its fixture.
        shutil.rmtree(tmp_path / "sub")


class TestNoTestWritesInTheDeveloperDirs:
    """The checkout is not the only tree a test can reach, and the other one is
    the developer's own.

    The suite runs with the developer's REAL home live — the user-dir sandbox
    above covers a LOAD and restores afterwards — so a test that resolves a config
    or state path without it writes into the developer's `~/.config` or
    `~/.local/state` and leaves no trace of having done so: the file is simply
    there afterwards. Measured, the hard way: the settings app's `apply_autostart`
    wrote the real `~/.config/niri/config.kdl` from a test that only believed it
    was writing into a temp home. The fix then was to sandbox that LOAD; this is
    the property that would have failed the test instead of the developer's disk.

    Same hook, second rule, and the checkout is asked FIRST — it has exemptions of
    its own and it normally lives inside the protected home, so judging the user
    dirs first would refuse pytest's `.pytest_cache` and stop the suite starting.
    """

    def test_a_write_into_the_developer_home_is_refused_before_it_lands(self):
        probe = _real_home() / "zz_suite_home_probe.txt"
        with pytest.raises(AssertionError, match="real user dirs"):
            probe.write_text("a test's file\n", encoding="utf-8")
        assert not probe.exists(), (
            "the refusal has to arrive BEFORE the write: one that reports "
            "afterwards has already put the file in the developer's home")

    def test_a_config_or_a_state_path_is_refused_even_when_nothing_is_there(self):
        """The two shapes the audit found, named: a settings write, and a state
        file appended to. Neither exists in a fresh home, so the guard — not the
        path — is what refuses them."""
        for probe in (_real_home() / ".config" / "handsoff" / "zz_probe.json",
                      _real_home() / ".local" / "state" / "handsoff"
                      / "zz_probe.jsonl"):
            with pytest.raises(AssertionError, match="real user dirs"):
                probe.unlink()      # the guard refuses before the FileNotFoundError
            assert not probe.exists()

    def test_the_message_names_the_directory_and_the_fix(self):
        """What the developer reads when this fires, and it is the whole remedy:
        which directory is the developer's, and what to build instead."""
        with pytest.raises(AssertionError) as excinfo:
            (_real_home() / "zz_probe.txt").write_text("x", encoding="utf-8")
        message = str(excinfo.value)
        assert str(_real_home()) in message, message
        assert "tmp_path" in message and "real user dirs" in message, message

    def test_the_checkout_keeps_precedence_over_the_home_it_sits_in(self):
        """The ordering, pinned on the PREDICATE so it holds wherever the suite
        is run (a file-only copy has no home above it), plus one behavioural pass
        that only bites where the checkout really is inside the home.

        It is not cosmetic: the checkout's gitignored artifacts have to stay
        writable, and a home-first guard refuses them — `ci/gates.sh` could not
        even start, because pytest writes `.pytest_cache` in the tree it was
        started in.
        """
        assert forbidden_write(ROOT / ".coverage") == "", (
            "the checkout's own artifacts are exempt, and the checkout normally "
            "lives inside the home: the checkout rule has to be asked first")
        assert forbidden_write(ROOT / "handsoff.py") == str(ROOT / "handsoff.py"), (
            "a checkout file is refused by the CHECKOUT rule, which is the "
            "message a home-first guard would get wrong too")
        probe = ROOT / "pytest-cache-files-zzprobe"
        probe.mkdir()
        (probe / "CACHEDIR.TAG").write_text("x", encoding="utf-8")
        shutil.rmtree(probe)
        assert not probe.exists()

    def test_the_decision_table_for_the_developer_dirs(self):
        """One row per way the second rule could be wrong."""
        table = [
            (_real_home() / ".config" / "handsoff" / "settings.json", True),
            (_real_home() / ".local" / "state" / "handsoff"
             / "cap-events.jsonl", True),
            (_real_home() / "notes.txt", True),
            # The home DIRECTORY itself: `rmdir` is a write too, and so is
            # anything that would replace or rename it.
            (_real_home(), True),
            # A SIBLING whose name starts the same way is not inside it — the
            # prefix trap, in the rule that was added second.
            (Path(str(_real_home()) + "-backup") / "x", False),
            (Path("/tmp") / "fixture", False),
            # The checkout, judged by its own rule and its own exemptions.
            (ROOT / "handsoff.py", True),
            (ROOT / ".coverage", False),
            (ROOT / "__pycache__" / "x.pyc", False),
        ]
        for path, forbidden in table:
            assert bool(forbidden_write(path)) is forbidden, (
                f"{path}: expected {'refused' if forbidden else 'allowed'}")

    def test_the_dirs_protected_are_the_ones_captured_before_any_sandbox(self):
        """The wiring, and the one property this rule has to have: the roots come
        from conftest's import time, not from whatever HOME says now. A root read
        live would follow every sandbox and protect a throw-away directory while
        the developer's real home stayed open — silently, which is the failure
        mode all of this exists to stop looking like success."""
        assert _REAL_USER_DIRS, "no user dirs were captured: nothing is protected"
        assert str(_real_home()) in _REAL_USER_DIRS
        for directory in _REAL_USER_DIRS:
            assert os.path.isabs(directory) and directory != os.sep, (
                f"{directory}: a relative root, or /, forbids every write there "
                f"is — the guard has to protect the developer's dirs and no more")
        assert tuple(guarded_user_dirs()) == tuple(_REAL_USER_DIRS), (
            "the guard installed in this process was not told the dirs conftest "
            "captured")
        with isolated_user_dirs() as sandbox:
            assert Path.home() == sandbox != _real_home(), (
                "the sandbox did not move HOME, so this test proves nothing")
            assert tuple(guarded_user_dirs()) == tuple(_REAL_USER_DIRS), (
                "the protected dirs moved with the sandbox: they are supposed to "
                "be the developer's, captured before any of this ran")

    def test_the_sandbox_home_stays_writable_from_inside_one(self):
        """The rule is against the CAPTURED paths, not against writing in a home:
        a load's throw-away home is exactly the fixture the sandbox exists to
        create, so refusing it would break every test that exercises the code the
        way the app resolves its own directories."""
        with isolated_user_dirs() as sandbox:
            config = Path(sandbox) / ".config" / "handsoff"
            config.mkdir(parents=True)
            (config / "settings.json").write_text("{}", encoding="utf-8")
            state = Path(sandbox) / ".local" / "state" / "handsoff"
            state.mkdir(parents=True)
            (state / "cap-events.jsonl").write_text("", encoding="utf-8")
            # ...and the developer's real one is refused from in there too, which
            # is the point of a rule that does not follow the live HOME.
            with pytest.raises(AssertionError, match="real user dirs"):
                (_real_home() / "zz_probe.json").write_text("x", encoding="utf-8")

    #: The same child, taking the directory to write into as an ARGUMENT rather
    #: than reading the guard's variables: those are the wiring under test.
    CHILD_HOME_PROBE = '''\
import pathlib, sys
child = sys.modules.get("checkout_guard")
print("protected:", child.protected() if child else "NONE")
probe = pathlib.Path(sys.argv[1]) / "zz_child_home_probe.txt"
try:
    probe.write_text("child", encoding="utf-8")
    print("WROTE")
except AssertionError as exc:
    print("REFUSED:", str(exc).splitlines()[0])
print("exists:", probe.exists())
if probe.exists():      # a guard that went quiet must still leave the home clean
    probe.unlink()
    print("cleaned")
'''

    def test_a_child_the_suite_spawns_is_refused_the_developer_dirs_too(
            self, tmp_path):
        """The suite's children are where most of its behaviour runs, and a child
        cannot answer "which dirs are the developer's?" from its own environment —
        its HOME is a throw-away one — so the parent has to name them."""
        proc = run_driver(["-", str(_real_home())], input=self.CHILD_HOME_PROBE,
                          capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "REFUSED:" in proc.stdout and "real user dirs" in proc.stdout, (
            "the child wrote into the developer's home:\n" + proc.stdout)
        assert f"protected: ('{_real_home()}'" in proc.stdout, (
            "the child has to protect the SAME dirs this process does: it cannot "
            "derive them from its own HOME, which is a throw-away one\n"
            + proc.stdout)
        assert "exists: False" in proc.stdout
        assert not (_real_home() / "zz_child_home_probe.txt").exists()
        # The same child, the same code, a FIXTURE instead: a refusal is only
        # evidence next to a write that still lands where it should.
        ok = run_driver(["-", str(tmp_path)], input=self.CHILD_HOME_PROBE,
                        capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        assert "WROTE" in ok.stdout and "exists: True" in ok.stdout \
            and "cleaned" in ok.stdout, ok.stdout

    def test_the_variable_is_what_the_child_protection_hangs_on(self):
        """The control, and it deliberately does NOT write into the home to prove
        the point the way the checkout half's control does: the difference would
        have to be shown on the developer's real files. So it writes into the
        CHECKOUT instead — refused, because that rule's variable is still set —
        while the child reports protecting no user dirs at all. Two variables,
        two rules, and the removal of one leaves the other working.
        """
        env = sandbox_env()
        env.pop(_PROTECTED_ENV)
        proc = subprocess.run([sys.executable, "-", str(ROOT)],
                              input=self.CHILD_HOME_PROBE, env=env,
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert "protected: ()" in proc.stdout, (
            "the child protected user dirs with nothing naming them:\n"
            + proc.stdout)
        assert "REFUSED:" in proc.stdout and "wrote inside the checkout" \
            in proc.stdout, (
            "dropping the user-dir variable dropped the checkout rule too, so "
            "the two rules are not separate variables\n" + proc.stdout)

    def test_the_child_environment_carries_the_protected_dirs(self):
        env = sandbox_env()
        assert env[_PROTECTED_ENV] == os.pathsep.join(_REAL_USER_DIRS)
        assert env[_PROTECTED_ENV].split(os.pathsep)[0] != env["HOME"], (
            "the child's own HOME is a sandbox, so the dirs it protects have to "
            "be the developer's real ones, named by the parent")
        shim = Path(env["PYTHONPATH"].split(os.pathsep)[0],
                    "sitecustomize.py").read_text(encoding="utf-8")
        assert _PROTECTED_ENV in shim and "protected=" in shim, (
            "the shim has to hand the dirs to install(), or a child has the "
            "checkout rule only")


class TestChildProcessDriversUseTheSameSandbox:
    def test_sandbox_env_pins_home_and_xdg(self, tmp_path):
        env = sandbox_env(tmp_path)
        assert env["HOME"] == str(tmp_path)
        assert env["XDG_CONFIG_HOME"] == str(tmp_path / ".config")
        assert env["XDG_STATE_HOME"] == str(tmp_path / ".local" / "state")

    def test_the_in_process_sandbox_pins_home_and_both_xdg_dirs(self):
        """The same contract as the child env, for the in-process half.

        XDG is not decoration: a developer whose XDG_STATE_HOME points outside
        HOME would otherwise keep resolving their real state directory, and the
        "it is under HOME" assertions would still pass because HOME moved.
        """
        with isolated_user_dirs() as home:
            assert os.environ["HOME"] == str(home)
            assert os.environ["XDG_STATE_HOME"] == str(home / ".local" / "state")
            assert os.environ["XDG_CONFIG_HOME"] == str(home / ".config")

    def test_the_shared_runner_really_sandboxes_the_child_it_starts(self):
        """Measured, not structural: the child's own HOME is a temp dir.

        Without this the sandbox is pinned only by parsing call sites, so a
        `run_driver` that stopped applying its environment would leave a child
        resolving the developer's config while every AST guard stayed green.
        """
        real = _real_home()
        proc = run_driver(
            ["-c",
             "import os; print(os.environ['HOME']);"
             "print(os.path.expanduser('~'))"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert len(lines) == 2, lines
        for value in lines:
            assert not Path(value).is_relative_to(real), (value, real)
        assert Path(lines[0]).name.startswith("handsoff-testhome-"), lines

    def test_sandbox_env_keeps_the_real_user_site_on_pythonpath(self, tmp_path):
        """A redirected HOME hides user site-packages — PySide6 lives there.

        Without this the GUI drivers would fail to import Qt and the failure
        would look like a broken suite rather than a broken sandbox.
        """
        site = _user_site()
        assert site
        parts = sandbox_env(tmp_path)["PYTHONPATH"].split(os.pathsep)
        assert site in parts and str(HERE) in parts

    def test_sandbox_env_creates_its_own_home_when_asked(self):
        env = sandbox_env()
        home = Path(env["HOME"])
        assert home.is_dir() and "handsoff-testhome-" in home.name

    def test_a_child_that_loads_the_app_goes_through_the_one_runner(self):
        """Discovered, not listed: a driver must be launched by conftest.

        The GUI and hardening drivers are STRINGS handed to `python -c`, so no
        import-level guard can see them — and a child that loads a monolith
        resolves the developer's HOME exactly like an in-process load used to.
        A per-FILE "does it mention sandbox_env" check was not enough: it passed
        while one of two scenarios in this very file was already unsandboxed.

        The payload is resolved through ONE level of local assignment
        (`code = "from core import tools…"`), because the earlier version only
        read literal constants — so a probe held in a variable slipped past it
        while the property it checks was already false.
        """
        offenders = []
        for path, node, sandboxed, names in _subprocess_calls():
            if sandboxed:
                continue
            if any(m in _payload_text(node, names) for m in APP_LOAD_MARKERS):
                offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            "these launch a child that loads or imports the app outside "
            f"conftest.run_driver / sandbox_env, so its HOME is the "
            f"developer's: {offenders}")

    def test_no_python_child_is_built_by_hand(self):
        """`[sys.executable, …]` may only be built inside a sandboxed function.

        An interpreter child started by hand inherits the developer's HOME, so
        whatever it imports resolves the real CONFIG_DIR/STATE_DIR — the same
        leak as an unsandboxed in-process load, one process further out. Two
        constructors exist: `conftest.run_driver` (a child that runs to
        completion) and `sandbox_env` (a child that has to keep running, like
        the bubble, the fake Ollama, or a helper's `-c` script).

        The rule is on the List literal rather than on the call, deliberately:
        a helper that builds its own argv (`_job`) is covered as long as THAT
        function takes its environment from the sandbox, instead of being an
        exception nobody wrote down.
        """
        offenders = []
        for path, tree in _test_trees():
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = _own_calls(func)
                sandboxed = any(_is_call_to(c, "sandbox_env", None)
                                or _is_call_to(c, "run_driver", None)
                                for c in calls)
                if sandboxed:
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.List):
                        continue
                    if any(isinstance(e, ast.Attribute) and e.attr == "executable"
                           for e in node.elts):
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            "these build a python child outside conftest.run_driver / "
            f"sandbox_env, so the child resolves the real HOME: {offenders}")


class TestNoLoaderBypassesTheSandbox:
    def test_no_test_builds_a_module_by_hand(self):
        """A hand-built loader is how the isolation is lost without a word.

        `module_from_spec` plus an `exec_module` in test code IS a load that
        never went through conftest's `_load` — i.e. one that resolves the real
        HOME again, silently, because nothing in the load itself complains.
        Parsed with `ast`, so the driver STRINGS (which are already sandboxed by
        env) do not count.

        The pair is what is banned, not the name: a test may INTERCEPT the
        function to pin a race the loader guards (`TestLoaderFailurePaths`
        replaces it, then puts it back), and such a test hand-executes nothing.
        A file that both builds a module and runs it is the load this rule
        exists for, and it is flagged wherever it appears.
        """
        offenders = []
        for path in sorted((HERE / "tests").glob("*.py")):
            if path.name in ("conftest.py", "fake_ollama.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            attrs = [n.attr for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute)]
            if "module_from_spec" in attrs and "exec_module" in attrs:
                first = next(n for n in ast.walk(tree)
                             if isinstance(n, ast.Attribute)
                             and n.attr == "module_from_spec")
                offenders.append(f"{path.name}:{first.lineno}")
        assert offenders == [], (
            "build modules through conftest._load (which sandboxes HOME), not "
            f"by hand: {offenders}")

    def test_the_shared_loader_is_the_one_that_isolates(self):
        """And the isolation is in the LOAD, not at the call site: the loader
        itself must enter the sandbox, or every test that forgets pays."""
        src = (HERE / "tests" / "conftest.py").read_text(encoding="utf-8")
        body = src.split("def _load(name: str, path: Path):", 1)[1]
        body = body.split("\ndef ", 1)[0]
        assert "isolated_user_dirs()" in body, body


class TestOneAppPerProcess:
    """The app registers ITSELF under one name, and every loader reuses it.

    Two copies in one process are not two views of one app. Each copy has its own
    CONFIG_DIR/STATE_DIR/SETTINGS and model mirrors, and each module body calls
    `core.audio.configure(...)`, which repoints the SHARED core.audio at whichever
    copy loaded last — the failure that was measured as "this process's whisper
    dir is the developer's real one". The name was the problem: every loader
    invented its own ("handsoff_core" plus a bare alias in this harness,
    "handsoff_core_gui" in the GUI driver, "handsoff_no_audio" in the hardening
    driver), so "is it already loaded?" had no answer anything could ask.
    """
    def test_the_app_registers_itself_under_one_canonical_name(self, H):
        """One name, published by the app, pointing at the one app."""
        assert H.APP_MODULE_NAME == APP_MODULE_NAME
        assert sys.modules[APP_MODULE_NAME] is H, \
            "the running app is not registered under the canonical name"
        assert app_instance() is H, \
            "the out-of-band instance record is not the running app"
        # No SECOND name for the same module: a bare "handsoff" alias was the
        # other half of the ambiguity (a stray `import handsoff` satisfied
        # itself from it and never noticed it was the app).
        aliases = sorted(name for name, mod in sys.modules.items()
                         if mod is H and name != APP_MODULE_NAME)
        assert aliases == [], (
            f"the app is registered under more than one name: {aliases}")

    def test_a_plain_import_publishes_the_canonical_name_itself(self):
        """No loader involved: `import handsoff` registers the canonical name.

        This is the production path (the bubble is started as a script, and a
        support module can be imported by name), so the app has to publish
        itself — relying on a loader to have registered it first would make the
        guarantee a property of the loaders rather than of the app. In a child,
        because this process already holds the running app and would refuse.
        """
        code = (
            "import sys, json;"
            "import handsoff;"
            "from core import APP_MODULE_NAME, app_instance, app_module;"
            "me = sys.modules['handsoff'];"
            "print(json.dumps({"
            "'canonical': sys.modules.get(APP_MODULE_NAME) is me,"
            "'instance': app_instance() is me,"
            "'ready': getattr(me, '__app_ready__', False) is True,"
            "'app_module': app_module() is me,"
            "'published_name': me.APP_MODULE_NAME}))"
        )
        proc = run_driver(["-c", code], capture_output=True, text=True,
                          timeout=180)
        assert proc.returncode == 0, proc.stderr[-3000:]
        import json as _json
        got = _json.loads(proc.stdout.strip().splitlines()[-1])
        assert got == {"canonical": True, "instance": True, "ready": True,
                       "app_module": True, "published_name": APP_MODULE_NAME}, got

    def test_loading_the_app_returns_the_running_one(self, H):
        """`core.load_app_module` is the ONE loader, and it cannot exec twice."""
        assert load_app_module([HERE / "handsoff.py"]) is H
        assert load_app_module([]) is H          # not even a path needed
        assert app_module() is H

    def test_a_second_copy_cannot_be_executed(self, H):
        """The refusal, at the point where a second app would start running.

        A hand-built load (a spec plus `exec_module`) is what every loader in the
        tree used to do, and it is what the app now refuses — with the duplicate
        named, so the failure is a message rather than a repointed core.audio
        discovered later. The load is sandboxed even here, because the constants
        above the check resolve HOME: the refusal must happen before anything the
        copy can do, which is exactly what is asserted.
        """
        import types
        second = types.ModuleType("handsoff_second_copy")
        second.__file__ = str(HERE / "handsoff.py")
        sys.modules["handsoff_second_copy"] = second
        code = compile((HERE / "handsoff.py").read_text(encoding="utf-8"),
                       str(HERE / "handsoff.py"), "exec")
        try:
            with isolated_user_dirs():
                with pytest.raises(ImportError, match="SECOND copy"):
                    exec(code, second.__dict__)
        finally:
            sys.modules.pop("handsoff_second_copy", None)
        # ...and nothing about the running app was disturbed.
        assert sys.modules[APP_MODULE_NAME] is H
        assert app_instance() is H
        assert H.SETTINGS is sys.modules[APP_MODULE_NAME].SETTINGS

    def test_an_unnamed_load_is_refused(self, H):
        """Registered BEFORE exec, or not at all.

        `module_from_spec` + `exec_module` with no `sys.modules` entry executes
        the app into a namespace nothing can see, which is indistinguishable
        from a duplicate — so it is refused rather than silently tolerated.
        """
        namespace = {"__name__": "handsoff_unnamed", "__file__": str(
            HERE / "handsoff.py")}
        code = compile((HERE / "handsoff.py").read_text(encoding="utf-8"),
                       str(HERE / "handsoff.py"), "exec")
        with isolated_user_dirs():
            with pytest.raises(ImportError, match="without a sys.modules"):
                exec(code, namespace)
        assert app_instance() is H

    def test_the_loader_that_admits_the_app_is_core_s(self):
        """One implementation, and it is the one the app can find ITSELF through.

        The admission is `setdefault` on the canonical name BEFORE executing, so
        the module sees itself in `sys.modules` (the app refuses to run
        unregistered) and two racing loaders execute at most one copy.
        """
        src = (HERE / "core" / "__init__.py").read_text(encoding="utf-8")
        body = src.split("def load_app_module(", 1)[1].split("\ndef ", 1)[0]
        assert "setdefault(APP_MODULE_NAME, fresh)" in body, body
        assert "exec_module(fresh)" in body, body
        assert "sys.modules.pop(APP_MODULE_NAME, None)" in body, (
            "a failed exec must give the slot back or the next caller inherits a "
            "half-initialised app")

    def test_no_shipped_loader_builds_the_app_by_hand(self):
        """The settings app asks core for the app instead of spec-loading it.

        It had the same shape as the removed "load another copy" paths: build a
        module, exec it, then hope nothing else had loaded a bubble. Read from
        source because that is what regresses — the behaviour is covered by the
        in-process tests above, but a re-introduced hand-built load would pass
        them from a process where nothing was loaded yet.
        """
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert "load_app_module(" in src, "the settings app does not use the one loader"
        for shape in ("spec_from_file_location", "module_from_spec"):
            assert shape not in src, (
                f"handsoff-settings.py builds a module by hand again ({shape}): "
                "ask core.load_app_module / core.load_module instead")
        # ...and the drivers, which are strings inside the test files.
        for name in ("test_settings_gui.py", "test_hardening.py"):
            driver = (HERE / "tests" / name).read_text(encoding="utf-8")
            if "exec_module" in driver:
                assert "sys.modules[" in driver, (
                    f"{name}'s driver exec's a module without naming it first")


class TestLoaderFailurePaths:
    """`core`'s loaders have to fail LOUDLY, and give nothing back.

    Every branch below is a way a second app — or a half-built one — could be
    produced instead of a refusal: a foreign module planted under the canonical
    name, an app that is still initialising, a load that raises mid-exec, two
    loaders racing for the same slot, a candidate the interpreter cannot build a
    spec for, a support module that must not be swapped under a live foreign
    submodule, and a failed support load that has to hand back exactly what it
    replaced. They are exercised directly, because the happy path (and the
    module body's own refusal) cannot reach any of them.

    The suite normally runs WITH the app loaded, so the fixture below hands each
    test the state these branches actually guard: nothing registered, nothing
    recorded. Both are put back afterwards — a test that evicted the app and
    left it evicted is how a second bubble got built in an earlier session.
    """

    @pytest.fixture()
    def no_app(self, monkeypatch):
        """A process that has not loaded the app yet, as at start-up."""
        saved = sys.modules.pop(APP_MODULE_NAME, None)
        monkeypatch.setattr(core, "_APP_INSTANCE", None)
        try:
            yield
        finally:
            if saved is not None:
                sys.modules[APP_MODULE_NAME] = saved

    def test_a_load_with_nothing_loadable_raises_and_names_what_it_tried(
            self, no_app, tmp_path):
        """No candidate at all is an ImportError, not a silent empty load."""
        with pytest.raises(ImportError, match="cannot find the application"):
            load_app_module([tmp_path / "absent.py"])
        # A candidate that is not even a path is SKIPPED, not raised: the list
        # comes from callers, and `Path(None)` / `Path(42)` must not be the
        # error the operator sees.
        with pytest.raises(ImportError, match="cannot find the application"):
            load_app_module([None, 42, tmp_path / "absent.py"])

    def test_a_foreign_module_under_the_canonical_name_is_refused(
            self, no_app, tmp_path):
        """A module planted under the app's name is not the app.

        The origin rule is the same one `load_module` uses: a module whose file
        lives outside the allowed dirs can never satisfy a loader, whatever it
        calls itself.
        """
        planted = types.ModuleType(APP_MODULE_NAME)
        planted.__file__ = str(tmp_path / "planted.py")
        sys.modules[APP_MODULE_NAME] = planted
        try:
            with pytest.raises(ImportError,
                               match="refusing to reuse a foreign"):
                load_app_module([HERE / "handsoff.py"])
        finally:
            sys.modules.pop(APP_MODULE_NAME, None)
        assert app_instance() is None, \
            "the loader executed a second copy to 'resolve' the foreign name"

    def test_an_app_that_is_still_initialising_refuses_a_second_load(
            self, no_app):
        """Registered but not ready is a refusal, not a reuse.

        A half-built app is not a second view of one app: its `SETTINGS` is a
        name the module that owns it has not defined yet. The file is a real one
        in an allowed dir, so this is the *ready* check being tested, not the
        origin rule.
        """
        half = types.ModuleType(APP_MODULE_NAME)
        half.__file__ = str(HERE / "handsoff.py")
        sys.modules[APP_MODULE_NAME] = half
        try:
            with pytest.raises(ImportError, match="still initialising"):
                load_app_module([HERE / "handsoff.py"])
        finally:
            sys.modules.pop(APP_MODULE_NAME, None)
        assert app_module() is None

    def test_a_load_that_raises_gives_the_slot_back(self, no_app, tmp_path):
        """A failed exec must not leave a half-initialised app behind.

        `module_from_spec` + `setdefault` registers the name BEFORE the module
        body runs, so a body that raises would otherwise leave a registered
        module with no SETTINGS for the next caller to find and hand out.
        """
        boom = tmp_path / "boom.py"
        boom.write_text("raise SystemExit('boom')\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            load_app_module([boom])
        assert APP_MODULE_NAME not in sys.modules, \
            "the failed load left a half-initialised app registered"
        assert app_instance() is None

    def test_two_racing_loaders_admit_exactly_one_copy(
            self, no_app, monkeypatch):
        """`setdefault` decides, and the loser hands back the WINNER's module.

        The interleaving is forced rather than hoped for: a concurrent loader
        admits its module under the canonical name in the window between this
        loader's `module_from_spec` and its own `setdefault`. The loser must
        return that module WITHOUT executing anything — executing is the second
        app, which is the whole thing this slot exists to prevent.
        """
        winner = types.ModuleType(APP_MODULE_NAME)
        winner.__file__ = str(HERE / "handsoff.py")
        real = importlib.util.module_from_spec

        def racer(spec):
            fresh = real(spec)
            sys.modules.setdefault(APP_MODULE_NAME, winner)
            return fresh

        monkeypatch.setattr(importlib.util, "module_from_spec", racer)
        try:
            assert load_app_module([HERE / "handsoff.py"]) is winner
        finally:
            sys.modules.pop(APP_MODULE_NAME, None)
        assert not hasattr(winner, "SETTINGS"), \
            "the losing loader executed a copy of the app into the winner"

    def test_a_candidate_the_interpreter_cannot_build_a_spec_for_is_skipped(
            self, no_app, monkeypatch, tmp_path):
        """A file this interpreter cannot turn into a module is not the app."""
        cand = tmp_path / "app_like.py"
        cand.write_text("SETTINGS = 1\n", encoding="utf-8")
        monkeypatch.setattr(importlib.util, "spec_from_file_location",
                            lambda *a, **k: None)
        with pytest.raises(ImportError, match="cannot find the application"):
            load_app_module([cand])

    def test_a_support_module_with_an_unbuildable_spec_is_skipped(
            self, monkeypatch, tmp_path):
        """Same rule for `load_module`: skip it, then fail with its own name."""
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "sandbox_spec_mod.py").write_text("VALUE = 4\n",
                                                  encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        monkeypatch.setattr(importlib.util, "spec_from_file_location",
                            lambda *a, **k: None)
        with pytest.raises(ImportError,
                           match="cannot load 'sandbox_spec_mod'"):
            core.load_module("sandbox_spec_mod")

    def test_a_home_that_cannot_be_resolved_does_not_stop_a_load(
            self, monkeypatch, tmp_path):
        """`_allowed_dirs` and the bin candidate both touch HOME.

        Neither may take the whole load down with them: HOME is one origin out of
        several, and a module beside the package has nothing to do with it.
        """
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "synth_nohome.py").write_text("VALUE = 11\n", encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)

        def no_home(cls):
            raise OSError("no home")

        monkeypatch.setattr(Path, "home", classmethod(no_home))
        try:
            mod = core.load_module("synth_nohome")
        finally:
            sys.modules.pop("core.synth_nohome", None)
        assert getattr(mod, "VALUE") == 11
        assert core._origin_ok(mod)

    def test_a_foreign_submodule_appearing_mid_load_is_refused(
            self, monkeypatch, tmp_path):
        """The swap guard is re-checked at the INSTALL point, not only on entry.

        A concurrent import that plants `core.<name>` between the entry check and
        the install would otherwise be overwritten by this load — the same
        "never swap under a live foreign submodule" rule, applied where the swap
        actually happens.
        """
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "synth_swap.py").write_text("VALUE = 7\n", encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        planted = types.ModuleType("core.synth_swap")
        planted.__file__ = str(tmp_path / "outside" / "planted.py")
        real = importlib.util.module_from_spec

        def racer(spec):
            fresh = real(spec)
            sys.modules["core.synth_swap"] = planted
            return fresh

        monkeypatch.setattr(importlib.util, "module_from_spec", racer)
        monkeypatch.delitem(sys.modules, "core.synth_swap", raising=False)
        try:
            with pytest.raises(ImportError,
                               match="refusing to swap foreign live submodule"):
                core.load_module("synth_swap")
        finally:
            sys.modules.pop("core.synth_swap", None)

    def test_a_failed_support_load_restores_what_it_replaced(
            self, monkeypatch, tmp_path):
        """No half-initialised squat: the previous entry comes back unchanged.

        The bare name here is a FOREIGN module on purpose. That is the case the
        loader refuses to overwrite, so the failing load has to put it back
        rather than leaving either its own module or nothing.
        """
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "synth_boom.py").write_text("raise RuntimeError('boom')\n",
                                            encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        prev = types.ModuleType("synth_boom")
        prev.__file__ = str(tmp_path / "outside" / "prev.py")
        monkeypatch.setitem(sys.modules, "synth_boom", prev)
        monkeypatch.delitem(sys.modules, "core.synth_boom", raising=False)
        with pytest.raises(RuntimeError):
            core.load_module("synth_boom")
        assert sys.modules["synth_boom"] is prev, \
            "a failed load left its own module under the bare name"
        assert "core.synth_boom" not in sys.modules

    def test_a_failed_support_load_leaves_no_squat_behind(
            self, monkeypatch, tmp_path):
        """With nothing there before, nothing is there after."""
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "synth_boom2.py").write_text("raise RuntimeError('boom')\n",
                                             encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        monkeypatch.delitem(sys.modules, "synth_boom2", raising=False)
        monkeypatch.delitem(sys.modules, "core.synth_boom2", raising=False)
        with pytest.raises(RuntimeError):
            core.load_module("synth_boom2")
        assert "synth_boom2" not in sys.modules
        assert "core.synth_boom2" not in sys.modules

    def test_a_baseexception_during_a_load_leaves_no_squat_behind(
            self, monkeypatch, tmp_path):
        """KeyboardInterrupt/SystemExit are not `Exception`.

        The rollback caught `Exception`, so a Ctrl-C landing inside a module
        body left a HALF-EXECUTED module in `sys.modules` under both names for
        the life of the process — and every later load adopted that corpse
        instead of the file, so the module could never be loaded again no
        matter how it was fixed. The app-module loader already used
        `BaseException`; this is the support-module twin.
        """
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "synth_sigint.py").write_text(
            "raise KeyboardInterrupt('ctrl-c during import')\n", encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        monkeypatch.delitem(sys.modules, "synth_sigint", raising=False)
        monkeypatch.delitem(sys.modules, "core.synth_sigint", raising=False)
        with pytest.raises(KeyboardInterrupt):
            core.load_module("synth_sigint")
        assert "synth_sigint" not in sys.modules, \
            "a half-executed module stayed under the bare name"
        assert "core.synth_sigint" not in sys.modules, \
            "a half-executed module stayed under the namespaced name"

    def test_a_module_already_imported_by_its_bare_name_is_adopted(
            self, monkeypatch):
        """A plain `import hardware` is not a second copy of it.

        A support module the interpreter already imported (by name, from the
        checkout root) is registered under `core.<name>` and handed back —
        re-exec'ing it would give two copies of a module that owns locks and
        caches, which is the same defect as two apps one level down.
        """
        module = types.ModuleType("hardware")
        module.__file__ = str(HERE / "hardware.py")          # an allowed dir
        monkeypatch.setitem(sys.modules, "hardware", module)
        monkeypatch.delitem(sys.modules, "core.hardware", raising=False)
        assert core.load_module("hardware") is module
        assert sys.modules["core.hardware"] is module

    def test_a_foreign_submodule_already_cached_is_refused(self, monkeypatch,
                                                          tmp_path):
        """The entry check: a planted `core.<name>` is never swapped out."""
        planted = types.ModuleType("core.synth_entry")
        planted.__file__ = str(tmp_path / "planted.py")
        monkeypatch.setitem(sys.modules, "core.synth_entry", planted)
        with pytest.raises(ImportError,
                           match="refusing to swap foreign live submodule"):
            core.load_module("synth_entry")
        assert sys.modules["core.synth_entry"] is planted

    def test_a_candidate_the_filesystem_refuses_falls_through_to_an_import(
            self, monkeypatch, tmp_path):
        """A stat that fails is not a missing module.

        The layout is the INSTALLED one — `~/.local/bin/<name>.py` beside
        `~/.local/bin/core` — so the plain-import fallback is what loads this,
        after the file-system candidate refuses to be stat'd. Both fallbacks have
        to exist: a stat error is not evidence that the module is absent.
        """
        inst = tmp_path / ".local" / "bin"
        pkg = inst / "core"
        pkg.mkdir(parents=True)
        (inst / "sandbox_inst_mod.py").write_text("VALUE = 9\n",
                                                  encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.syspath_prepend(str(inst))
        real_is_file = Path.is_file
        refused = inst / "sandbox_inst_mod.py"

        def broken(self):
            if self == refused:
                raise OSError("stat refused")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", broken)
        monkeypatch.delitem(sys.modules, "sandbox_inst_mod", raising=False)
        monkeypatch.delitem(sys.modules, "core.sandbox_inst_mod",
                            raising=False)
        try:
            mod = core.load_module("sandbox_inst_mod")
        finally:
            sys.modules.pop("sandbox_inst_mod", None)
            sys.modules.pop("core.sandbox_inst_mod", None)
        assert getattr(mod, "VALUE") == 9
        assert str(mod.__file__) == str(refused)

    def test_a_candidate_that_repeats_in_the_list_is_tried_once(
            self, monkeypatch):
        """The candidate list cannot make the loader consider a path twice.

        With `_HERE` at the filesystem root the package dir and its parent are
        the same directory, so both derived candidates are the same string —
        the degenerate case the dedupe exists for, and the same doubling the
        installed layout produces through the bin candidate. Asserted by
        OBSERVING the filesystem, not by coverage: that path is stat'd once.
        """
        monkeypatch.setattr(core, "_HERE", Path("/"))
        counted = Path("/sandbox_dedupe_mod.py")
        asked: list = []
        real_is_file = Path.is_file

        def counting(self):
            if self == counted:
                asked.append(self)
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", counting)
        monkeypatch.delitem(sys.modules, "sandbox_dedupe_mod", raising=False)
        with pytest.raises(ImportError,
                           match="cannot load 'sandbox_dedupe_mod'"):
            core.load_module("sandbox_dedupe_mod")
        assert len(asked) == 1, f"the same candidate was tried {len(asked)}x"

    def test_a_failed_load_gives_back_the_concurrent_module_it_found(
            self, monkeypatch, tmp_path):
        """Also on the way OUT: whatever was there before is what is there after.

        A concurrent import can install `core.<name>` between the entry check and
        the install; a load that then fails has to hand that module back rather
        than popping it, or the failing load breaks a working import.
        """
        pkg = tmp_path / "core"
        pkg.mkdir()
        (pkg / "synth_race.py").write_text("raise RuntimeError('boom')\n",
                                           encoding="utf-8")
        monkeypatch.setattr(core, "_HERE", pkg)
        concurrent = types.ModuleType("core.synth_race")
        concurrent.__file__ = str(pkg / "concurrent.py")      # an allowed dir
        real = importlib.util.module_from_spec

        def racer(spec):
            fresh = real(spec)
            sys.modules["core.synth_race"] = concurrent
            return fresh

        monkeypatch.setattr(importlib.util, "module_from_spec", racer)
        monkeypatch.delitem(sys.modules, "core.synth_race", raising=False)
        with pytest.raises(RuntimeError):
            core.load_module("synth_race")
        assert sys.modules["core.synth_race"] is concurrent, \
            "the failing load took the concurrent module with it"

    def test_the_origin_rule_survives_a_path_that_cannot_be_resolved(
            self, monkeypatch, tmp_path):
        """`resolve()` is a filesystem call, and it can fail.

        The origin union is built at load time, so one unresolvable member — a
        dead network mount, a permission error, an unwritable home — must fall
        back to the path as given instead of raising out of the loader.
        """
        real = Path.resolve

        def broken(self, *a, **k):
            if str(self).startswith(str(tmp_path)):
                raise OSError("resolve refused")
            return real(self, *a, **k)

        src = tmp_path / "src"
        src.mkdir()
        (src / "handsoff.py").write_text("# not executed\n", encoding="utf-8")
        monkeypatch.setattr(Path, "resolve", broken)
        monkeypatch.setattr(core, "_HERE", tmp_path / "core")
        monkeypatch.setenv("HANDSOFF_SOURCE_PATH", str(src))
        monkeypatch.setattr(Path, "home",
                            classmethod(lambda cls: tmp_path / "home"))
        dirs = core._allowed_dirs()
        assert (tmp_path / "core") in dirs, "the package dir was dropped"
        assert src in dirs, "the named checkout was dropped"
        assert src / "core" in dirs, "the named checkout's core/ was dropped"
        assert (tmp_path / "home" / ".local" / "bin") in dirs, \
            "the installed dir was dropped"

    def test_a_module_whose_path_cannot_be_resolved_is_not_ours(
            self, monkeypatch, tmp_path):
        """An unanswerable origin is a NO, not an exception into the caller."""
        real = Path.resolve

        def broken(self, *a, **k):
            if str(self).startswith(str(tmp_path)):
                raise OSError("resolve refused")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "resolve", broken)
        planted = types.ModuleType("planted")
        planted.__file__ = str(tmp_path / "planted.py")
        assert core._origin_ok(planted) is False

    def test_the_source_checkout_can_be_named_explicitly(self, monkeypatch,
                                                        tmp_path):
        """HANDSOFF_SOURCE_PATH is part of the origin union, in both spellings."""
        root = tmp_path / "src"
        root.mkdir()
        (root / "handsoff.py").write_text("# not executed\n", encoding="utf-8")
        monkeypatch.setenv("HANDSOFF_SOURCE_PATH", str(root / "handsoff.py"))
        assert core._repo_root() == root
        monkeypatch.setenv("HANDSOFF_SOURCE_PATH", str(root))
        assert core._repo_root() == root
        # A path that does not name handsoff.py is ignored, not accepted — and
        # with nothing beside the package either, the answer is None rather than
        # a guess at some parent directory.
        monkeypatch.setenv("HANDSOFF_SOURCE_PATH", str(tmp_path / "other"))
        monkeypatch.setattr(core, "_HERE", tmp_path / "nowhere")
        assert core._repo_root() is None


class TestSupportModulesNeverShadowTheStdlib:
    """A support module may share a stdlib name; it may not TAKE that name.

    `core/calendar.py` and the standard library's `calendar` are the same name,
    and `load_module` used to register every support module under its bare name
    as well as `core.<name>`. Two consequences, both measured: the app's
    `sys.modules['calendar']` became `/…/core/calendar.py`, so any later
    `from calendar import timegm` — faster_whisper and chatterbox both do it —
    raised ImportError and the bubble reported the speech engines as "not
    installed"; and because the bare name was bound BEFORE the module's own body
    ran, `core/calendar.py`'s own `import calendar` resolved to the half-built
    module itself, so the shadow was planted by the loader and then read by the
    very file that needed the real one.

    It was silent and order-dependent: at HEAD it did not fire only because
    handsoff.py reached `core.calendar` through a `from core.calendar import …`
    statement, whose chain imported the stdlib first. The hazard is forced here
    rather than hoped for, so the guard cannot go vacuous on a machine where
    something else happens to import `calendar` early.
    """

    def test_a_stdlib_named_support_module_never_takes_the_bare_name(
            self, monkeypatch):
        real = sys.modules.pop("calendar", None)
        monkeypatch.delitem(sys.modules, "core.calendar", raising=False)
        try:
            mod = core.load_module("calendar")
            assert Path(mod.__file__).name == "calendar.py"
            assert Path(mod.__file__).parent.name == "core", mod.__file__
            assert sys.modules["core.calendar"] is mod
            # ...and the stdlib is still reachable under its own name, which is
            # what every third-party `import calendar` from here on will get.
            import calendar as stdlib_calendar
            assert Path(stdlib_calendar.__file__) != Path(mod.__file__), (
                "load_module handed the app's module to `import calendar` — a "
                "third-party `from calendar import timegm` breaks from here")
            assert hasattr(stdlib_calendar, "timegm"), (
                "the stdlib calendar was replaced by one without timegm")
        finally:
            if real is not None:
                sys.modules["calendar"] = real

    def test_a_normal_support_module_keeps_its_bare_name(self, monkeypatch):
        """The rule must not quietly disable bare registration altogether.

        `hardware` is nobody else's name, and it is adopted by bare name today
        (see `test_a_module_already_imported_by_its_bare_name_is_adopted`), so a
        fix that simply stopped binding bare names would break that adoption
        rather than fix anything.
        """
        assert "hardware" not in sys.stdlib_module_names
        monkeypatch.delitem(sys.modules, "hardware", raising=False)
        monkeypatch.delitem(sys.modules, "core.hardware", raising=False)
        mod = core.load_module("hardware")
        assert sys.modules.get("hardware") is mod, (
            "a non-stdlib support module lost its bare registration")
        assert sys.modules["core.hardware"] is mod


class TestImportStatementsCannotBypassTheSandbox:
    def test_no_test_module_imports_a_user_dir_baking_module_at_collection(
            self):
        """The one load no loader can wrap: an import STATEMENT.

        `conftest._load` sandboxes the LOAD, and the autouse fixtures load the
        bubble before any test body runs — so `from core.tools import …` inside
        a test function is safe. A module-scope import is not: pytest imports
        the test module itself, before any fixture, and the module bakes
        CONFIG_DIR/STATE_DIR from whatever HOME is live at that instant — the
        developer's. It is silent because nothing in the load complains, and
        machine-dependent because it only shows up where real config exists.
        """
        banned = _unimportable_names()
        # The discovery itself must work: three modules bake user dirs at
        # import today, and a rename that quietly emptied this set must fail
        # rather than make every check below vacuous.
        assert {"core.tools", "core.audio", "handsoff"} <= banned, banned
        offenders = []
        for path in sorted((HERE / "tests").glob("*.py")):
            if path.name == "fake_ollama.py":
                continue                    # a stub with no imports of ours
            for lineno, name in _collection_imports(path):
                if _imports_banned(name, banned):
                    offenders.append(f"{path.name}:{lineno} imports {name}")
        assert offenders == [], (
            "these import an app module while the test module is being "
            "collected, i.e. with the developer's HOME — at import they bake "
            "CONFIG_DIR/STATE_DIR, and handsoff.py adopts the tools copy "
            f"outright (see the hazard test): {offenders}")

    def test_a_plain_import_really_would_bake_the_developer_home(self):
        """The measurement behind the ban, in a child so the suite is unharmed.

        Deliberately run with the REAL home ("sandboxed" through the one
        constructor, with the real home passed as its home) because THAT IS the
        hazard: a plain `import core.tools` resolves CONFIG_DIR under the
        developer's home, and it is not a private copy — `handsoff.py` reaches
        the tools with a plain `from core import tools`, so `load_module`
        returns the very same object. One import statement at the top of a test
        file would point the whole tool layer at the developer's real state.
        """
        real = _real_home()
        code = ("import sys; sys.path.insert(0, '.');"
                "from core import load_module, tools;"
                "print(tools.CONFIG_DIR);"
                "print(load_module('tools') is tools)")
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(HERE),
            env=sandbox_env(real), capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert lines and lines[0].startswith(str(real)), lines
        assert lines[-1] == "True", (
            "the bubble's `from core import tools` no longer shares the "
            f"package copy — this guard needs re-deriving: {lines}")

    def test_every_app_module_alive_in_the_suite_is_sandboxed(self, H):
        """The guarantee, swept over the PROCESS instead of argued about.

        Whatever a test loaded and however it loaded it, no app module alive in
        `sys.modules` may hold a path constant under the developer's home. This
        is the assertion the request is actually about — the AST guards stop the
        shapes, this one catches anything that got through, in any order.
        """
        real = _real_home()
        repo = Path(HERE).resolve()
        tests_dir = (repo / "tests")
        checked, looked_at, offenders = [], set(), []
        for name, mod in sorted(sys.modules.items()):
            raw = getattr(mod, "__file__", None)
            if not raw:
                continue
            try:
                path = Path(raw).resolve()
            except (OSError, ValueError):
                continue
            if not path.is_relative_to(repo) or path.is_relative_to(tests_dir):
                continue                    # the harness's own files
            for attr in PATH_NAMES:
                value = getattr(mod, attr, None)
                if not isinstance(value, (str, Path)):
                    continue
                checked.append(f"{name}.{attr}")
                looked_at.add(name)
                if str(value).startswith(str(real)):
                    offenders.append(f"{name}.{attr}={value}")
        assert offenders == [], (
            "app module(s) resolved the developer's real user dirs: "
            f"{offenders} (sandbox the load, or use conftest._load)")
        # ...and the sweep really looked at the modules that bake at import.
        assert {"core.tools", "core.audio"} <= looked_at, sorted(looked_at)
        assert any(n.startswith("handsoff") for n in looked_at), sorted(looked_at)


class TestTheBubbleModuleOwnsTheAppearance:
    """The bubble's geometry, palette and 13 designs are their own module.

    Pinned because of how this state used to fail. The window size and the
    geometry derived from it were written from TWO places — module constants at
    import, and the live settings path in the Assistant — and a live size change
    left `APERTURE_R` sized for the old window while the widget resized, so
    growing the bubble clipped the design to the previous circle and swapping
    shapes showed each shape's scaled geometry inside a stale one. The extraction
    answers that with one owner and one derivation (`configure()`), and the host
    injected. So: the module must stay application-free, the app must never
    define or assign the appearance state again, `configure()` must be the only
    thing that writes it, and the injection must really reach the module — an
    un-injected `SETTINGS` silently renders the fallback palette at the default
    size, which looks like "the colours never apply".
    """

    APPEARANCE = ("WINDOW_PX", "BUBBLE_R0", "GLOW_PAD", "GEOM_K", "APERTURE_R",
                  "BUBBLE_ACCENT", "ANIM_ENERGY", "STATE_COLORS", "_BUBBLE_FX",
                  "design_region", "BubbleWidget")
    # Everything the module is allowed to be handed. Anything else written onto
    # it from the app would be a second owner. PACKS_DIR is a PATH the host owns,
    # like SETTINGS_APP and RESTART_SCRIPT — not appearance state: the module
    # still derives everything it draws from `configure()`, and the settings app
    # resolves the same tree through the module's own XDG fallback.
    HOST = ("SETTINGS", "APP_NAME", "SETTINGS_APP", "RESTART_SCRIPT", "notify",
            "PACKS_DIR")

    def _app_tree(self):
        return ast.parse((HERE / "handsoff.py").read_text(encoding="utf-8"))

    def _partial_install_nodes(self, tree):
        """The ONE exemption, and proof it is bounded to the loader's fallback.

        `_MissingBubble` carries the same names as inert defaults so a
        deployment without core/bubble.py still imports, reports and fails
        loudly on use — the same shape `_MissingAudio` has. It is not a second
        owner: it cannot paint anything. Confining it to the `except ImportError`
        branch is what keeps "the app has these names" from being true of a real
        install, so that placement is asserted rather than assumed.
        """
        stub = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                     and n.name == "_MissingBubble"), None)
        assert stub is not None, (
            "the partial-install stub vanished: a deployment missing "
            "core/bubble.py must still import and report, not die at import")
        guarded = [t for t in ast.walk(tree)
                   if isinstance(t, ast.Try)
                   and any(isinstance(h.type, ast.Name)
                           and h.type.id == "ImportError" for h in t.handlers)]
        assert any(any(n is stub for n in ast.walk(t)) for t in guarded), (
            "_MissingBubble is no longer inside an `except ImportError` branch, "
            "so the app may now have a reachable second copy of the appearance")
        return {id(n) for n in ast.walk(stub)}

    def test_the_module_does_not_reach_back_into_the_application(self):
        """Application-free: no import of the app, and no bare app name.

        A module that imports handsoff cannot be loaded on its own, which is the
        whole reason the mask geometry and the painters could be measured
        without a running bubble in the first place.
        """
        tree = ast.parse((HERE / "core" / "bubble.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "handsoff" not in imported, (
            "core/bubble.py imports the application — it must take the host by "
            "injection so it stays loadable and measurable on its own")
        # The names it needs must EXIST as injectable seams, or the host has
        # nothing to bind and the module silently runs on its own defaults.
        defined = {n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef))}
        defined |= {t.id for n in tree.body if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        defined |= {n.target.id for n in tree.body
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
        missing = sorted(set(self.HOST) - defined)
        assert missing == [], (
            f"core/bubble.py no longer declares the injected host seam(s) "
            f"{missing} — handsoff.py has nothing to bind")
        assert {"configure", "BubbleWidget"} <= defined, (
            "the module must own its derivation and its widget")

    def test_the_app_injects_the_whole_host(self, H):
        """What the app hands over at load time is really the app's own objects."""
        b = H._core_bubble
        assert b.SETTINGS is H.SETTINGS, (
            "the bubble module is rendering a different settings dict than the "
            "app loads and saves — every colour and size it reads is stale")
        assert b.APP_NAME == H.APP_NAME
        assert b.SETTINGS_APP == H.SETTINGS_APP, (
            "the context menu must open the installed settings app, not a default")
        assert b.RESTART_SCRIPT == H.RESTART_SCRIPT
        assert b.notify is H.notify, (
            "an un-injected notifier is inert, so a bubble error is silent")

    def test_the_app_defines_no_appearance_state_of_its_own(self):
        """No second copy: not a constant, not a class, in any scope."""
        tree = self._app_tree()
        stub = self._partial_install_nodes(tree)
        offenders = []
        for node in ast.walk(tree):
            if id(node) in stub:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) \
                    and node.id in self.APPEARANCE:
                offenders.append(f"line {node.lineno}: {node.id} = ...")
            elif isinstance(node, ast.ClassDef) and node.name == "BubbleWidget":
                offenders.append(f"line {node.lineno}: class BubbleWidget")
        assert offenders == [], (
            "handsoff.py still owns appearance state; it belongs to "
            f"core/bubble.py: {offenders}")

    def test_the_app_only_ever_binds_the_host(self):
        """`configure()` writes the appearance; the app may not write it directly.

        Binding the host is the app's job. Assigning the geometry, the palette or
        the look knobs from here is how the two writers that caused the stale
        aperture came back.
        """
        writes = []
        for node in ast.walk(self._app_tree()):
            if not isinstance(node, ast.Attribute) or \
                    not isinstance(node.ctx, ast.Store):
                continue
            value = node.value
            if isinstance(value, ast.Name) and value.id == "_core_bubble":
                writes.append((node.lineno, node.attr))
        bad = [(ln, a) for ln, a in writes if a not in self.HOST]
        assert bad == [], (
            "the application writes appearance state onto the module instead of "
            f"letting configure() derive it: {bad}")
        assert {a for _ln, a in writes} >= {"SETTINGS", "notify"}, (
            "the host injection went missing entirely — the module is running "
            "on its placeholders")

    def test_configure_reads_the_dict_it_is_handed(self, H):
        """`configure(settings)` must use THAT dict, not the injected one.

        The palette reader used to reach for the module global, so passing a
        settings dict built from a file on disk silently produced the app's
        current colours — a size change would apply while the colours did not,
        which is the shape of the original "I chose a colour and nothing
        happened" report. Both lookups (explicit dict, injected dict) are
        asserted, plus the junk-colour fallback.
        """
        b = H._core_bubble
        wanted = {"idle": "#111111", "listening": "#222222",
                  "thinking": "#333333", "speaking": "#444444"}
        try:
            b.configure({"bubble_size": 160, "colors": dict(wanted),
                         "bubble_accent": 0.9, "animation_energy": 1.8})
            assert {k: v.name() for k, v in b.STATE_COLORS.items()} == wanted, (
                "an explicit settings dict was ignored — the palette came from "
                "somewhere else")
            assert b.WINDOW_PX == 160 and b.APERTURE_R == 160 / 2.0 - 1.0
            assert (b.BUBBLE_ACCENT, b.ANIM_ENERGY) == (0.9, 1.8)
            b.configure()          # no argument: the injected settings
            assert b.STATE_COLORS["idle"] is not None
            b.configure({"bubble_size": 96, "colors": {"idle": "not-a-colour"}})
            assert b.STATE_COLORS["idle"].name() == "#2f6fed", (
                "a colour the parser rejects must fall back, not render junk")
        finally:
            b.configure(H.SETTINGS)   # put the app's own geometry back

    def test_a_newly_required_core_module_cannot_miss_the_installer_floor(self):
        """The installer's required list must be derived from what the app imports.

        This is the drift the project has already paid for twice: `core/theme.py`
        shipped nowhere while doctor reported `in-sync`, and the list of modules
        was hand-maintained in seven places. The glob is the ceiling; the floor
        exists to fail the stage loudly, so it may not fall behind what
        handsoff.py hard-requires. Derived here by reading handsoff.py, not by
        repeating its list.
        """
        found = set()
        for node in ast.walk(self._app_tree()):
            if isinstance(node, ast.ImportFrom) and node.module == "core":
                for a in node.names:
                    if (HERE / "core" / f"{a.name}.py").exists():
                        found.add(a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    if parts[0] == "core" and len(parts) == 2 \
                            and (HERE / "core" / f"{parts[1]}.py").exists():
                        found.add(parts[1])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_load_module" and node.args \
                    and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) \
                    and (HERE / "core" / f"{node.args[0].value}.py").exists():
                found.add(node.args[0].value)
        assert len(found) >= 8, (
            f"the derivation found only {sorted(found)} — it stopped seeing how "
            "handsoff.py loads core modules, so it proves nothing")

        text = (HERE / "install.sh").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if ln.startswith("CORE_REQUIRED="))
        declared = set(line.split('"')[1].split())
        assert "__init__" in declared, (
            "the floor must name the package itself, or a partial install "
            "missing core/__init__.py would not fail the stage")
        missing = sorted(found - declared)
        assert missing == [], (
            f"handsoff.py requires core module(s) {missing} that CORE_REQUIRED "
            "does not declare — a deployment missing one passes the floor check "
            "and dies on import")
