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
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import APP_MODULE_NAME, app_instance, app_module, load_app_module

from conftest import HERE as ROOT, _REAL_HOME, _load, _user_site, \
    isolated_user_dirs, run_driver, sandbox_env

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

    def test_the_sandbox_is_restored_even_when_a_load_raises(self):
        """A module that fails to import must not strand HOME.

        The failing load is the interesting case: if the restore only happened
        on the success path, every later test in the session would resolve the
        throw-away HOME (or the temp dir would leak into the environment).
        """
        before = {k: os.environ.get(k) for k in
                  ("HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
        broken = HERE / "tests" / "_sandbox_boom.py"
        broken.write_text("raise RuntimeError('boom')\n")
        try:
            with pytest.raises(RuntimeError):
                _load("sandbox_boom_probe", broken)
        finally:
            broken.unlink(missing_ok=True)
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

        `module_from_spec(...)` in test code means a load that does not go
        through conftest's `_load` — i.e. one that resolves the real HOME again,
        silently, because nothing in the load itself complains. Parsed with
        `ast`, so the driver STRINGS (which are already sandboxed by env) do not
        count.
        """
        offenders = []
        for path in sorted((HERE / "tests").glob("*.py")):
            if path.name in ("conftest.py", "fake_ollama.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) \
                        and node.attr == "module_from_spec":
                    offenders.append(f"{path.name}:{node.lineno}")
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
