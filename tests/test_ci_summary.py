"""Tests for the CI tooling that runs inside the pipeline.

ci/pytest_summary.py turns a red run into a readable digest and can post it as
an MR note (network, so worth pinning). ci/compile_all.py is the compile gate
the workflows call — it discovers its own file set precisely so a new module
cannot slip past uncompiled, which is worth pinning too. Neither is shipped
app code, but this is the code a maintainer reads first when the gate fails.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from conftest import _load as _load_module

HERE = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    # conftest's loader registers the module in sys.modules before executing it
    # (dataclasses resolves string annotations through sys.modules) and runs the
    # load inside the suite's user-dir sandbox — one loader for every in-process
    # load, so a new one cannot quietly miss the isolation.
    return _load_module(name, HERE / "ci" / filename)


@pytest.fixture(scope="module")
def S():
    return _load("pytest_summary", "pytest_summary.py")


@pytest.fixture(scope="module")
def C():
    return _load("compile_all", "compile_all.py")


@pytest.fixture
def clean_env(monkeypatch):
    """No ambient GitLab variables, so the CI branches are explicit."""
    for name in ("GITLAB_CI", "CI_PROJECT_ID", "CI_MERGE_REQUEST_IID",
                 "CI_OPEN_MERGE_REQUESTS", "CI_API_V4_URL",
                 "GITLAB_SUMMARY_TOKEN", "GITLAB_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


GREEN = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="1"
 tests="711" time="48.8">
<testcase classname="tests.test_theme.TestThemeMath" name="test_scales" time="0.01"/>
<testcase classname="tests.test_audio" name="test_gate" time="0.02">
  <skipped message="no device"/></testcase>
</testsuite></testsuites>
"""

RED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="0"
 tests="4" time="12.3">
<testcase classname="tests.test_audio.TestListener" name="test_mic" time="0.1">
  <failure message="OSError: libasound.so.2: cannot open shared object file"/>
</testcase>
<testcase classname="tests.test_policy" name="test_deny" time="0.1">
  <failure message="AssertionError: 3 != 4"/></testcase>
<testcase classname="tests.test_settings" name="test_ok" time="0.0"/>
<testcase classname="" name="tests.test_collect" time="0.0">
  <error message="collection failure"/></testcase>
</testsuite></testsuites>
"""


def _write(tmp_path, text, name="report.xml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _gitlab_blocks():
    """Top-level key -> its block of text. No yaml dependency for CI config.

    Everything, including `name: value` lines like `.qt_deps: &qt_deps`, is
    a block here — treating only `name:` lines as keys folds the anchors
    into whichever job came before them, which is how a first attempt at
    this test accused `default:` of running pytest.

    A real parser is not an option: PyYAML is not in requirements.txt, and the
    suite jobs install exactly that manifest, so importing it would make these
    tests fail in the container they exist to protect.
    """
    blocks, name, lines = {}, None, []
    text = (HERE / ".gitlab-ci.yml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            if name:
                blocks[name] = "\n".join(lines)
            name = line.split(":", 1)[0].strip()
            lines = []
        elif name:
            lines.append(line)
    if name:
        blocks[name] = "\n".join(lines)
    return blocks


def _gitlab_effective(name, blocks, seen=()):
    """A job's text plus what it inherits — via `extends:` and `<<: *shared`.

    A job that runs pytest through an `extends:` template must be covered
    too, or deleting the apt layer from the template would leave this test
    green and vacuous.
    """
    text = blocks[name]
    for ref in re.findall(r"^\s*extends:\s*([\w.]+)", text, re.M):
        if ref in blocks and ref not in seen:
            text += "\n" + _gitlab_effective(ref, blocks, seen + (name,))
    if "<<: *shared" in text and ".shared" in blocks and ".shared" not in seen:
        text += "\n" + blocks[".shared"]
    return text


class TestSuiteJobsGetTheAudioRuntime:
    """A suite job without `.qt_deps` dies at import, before any test runs.

    `handsoff.py` imports sounddevice, which resolves the PortAudio library
    itself with `ctypes.util.find_library('portaudio')`, and Debian slim ships
    neither it nor the ALSA runtime it links against. So: every job that runs
    pytest must inherit the anchor, and the anchor must install **both**
    packages. Installing only libasound2t64 reads as sufficient and is exactly
    the mistake that kept the tests jobs red — the failure is an import error
    with no test names in it, which is why this is pinned.
    """

    # Module-level so the order job's own guard can read the same config without
    # inheriting every audio-runtime assertion above.
    _blocks = staticmethod(_gitlab_blocks)
    _effective = staticmethod(_gitlab_effective)

    def test_every_job_that_runs_pytest_inherits_the_runtime_libraries(self):
        blocks = self._blocks()
        runners = {
            name: self._effective(name, blocks)
            for name in blocks if not name.startswith(".")
            and "python -m pytest" in self._effective(name, blocks)
        }
        assert runners, "no job runs pytest — this test would be vacuous"
        for name, text in runners.items():
            assert "*qt_deps" in text, (
                f"{name} runs pytest without the `.qt_deps` apt layer, so it "
                f"fails at `import handsoff` with no test names in the log")

    def test_the_runtime_layer_must_not_be_a_single_apt_one_liner(self):
        """One unknown package name used to install NOTHING, silently.

        `apt-get install a b c && …` fails as a whole, so a single bad name
        leaves a container with no audio runtime while the job runs the entire
        suite — hundreds of import errors whose cause is one line at the top of
        the log. It is a script now, and it verifies itself.
        """
        block = self._blocks()[".qt_deps"]
        assert "apt-get install" not in block, (
            "the apt layer must live in the checked script, where a failure can "
            "be attributed to the package that caused it")
        assert "ci/apt_deps.sh" in block
        script = HERE / "ci" / "apt_deps.sh"
        assert script.exists(), "the layer the jobs call must exist"
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0, (
            "the layer must at least parse")

    def test_the_script_installs_portaudio_and_the_alsa_runtime(self):
        text = (HERE / "ci" / "apt_deps.sh").read_text(encoding="utf-8")
        assert "libportaudio2" in text, (
            "sounddevice resolves libportaudio itself; without libportaudio2 "
            "every job dies at import")
        assert "libasound2t64" in text, "PortAudio links the ALSA runtime"
        assert "libdbus-1-3t64 libdbus-1-3" in text, (
            "names that shift with the t64 transition need a fallback — the "
            "missing t64 name is what made the old one-liner install nothing")

    def test_the_script_verifies_what_sounddevice_actually_looks_up(self):
        """Installing a package is not the property; resolving it is."""
        text = (HERE / "ci" / "apt_deps.sh").read_text(encoding="utf-8")
        assert "libportaudio.so.2" in text, "verify the SONAME by file"
        assert "find_library" in text and "portaudio" in text, (
            "sounddevice resolves PortAudio through ctypes, so that lookup is "
            "the one worth checking")
        assert "exit 1" in text, (
            "a missing library must fail the step, not warn and continue")

    def test_the_manifest_names_the_same_library(self):
        """`requirements.txt` documents the dependency the CI layer installs."""
        assert "libportaudio2" in (HERE / "requirements.txt").read_text(
            encoding="utf-8")


class TestShortName:
    """junit's `classname` is a module path plus optional class names."""

    def test_module_level_test(self, S):
        assert S._short_classname("tests.test_audio", "test_gate") == \
            "test_audio.py::test_gate"

    def test_class_test(self, S):
        assert S._short_classname("tests.test_audio.TestListener", "test_mic") == \
            "test_audio.py::TestListener::test_mic"

    def test_nested_class_test(self, S):
        assert S._short_classname(
            "tests.test_a.TestOuter.TestInner", "test_x") == \
            "test_a.py::TestOuter::TestInner::test_x"

    def test_collection_error_uses_name_as_module_path(self, S):
        assert S._short_classname("", "tests.test_collect") == "test_collect.py"

    def test_subdirectory_module(self, S):
        assert S._short_classname("tests.sub.test_a", "test_x") == \
            "sub/test_a.py::test_x"

    def test_bare_name_without_module(self, S):
        assert S._short_classname("", "test_x") == "test_x"


class TestSummarize:
    def test_green_report(self, S, tmp_path):
        summary = S.summarize(_write(tmp_path, GREEN))
        assert summary.ok and summary.found
        assert summary.failed == 0 and summary.total == 711
        assert "all 711 tests passed" in summary.markdown
        assert "1 skipped" in summary.markdown

    def test_red_report_lists_each_failure_with_its_short_name(self, S, tmp_path):
        summary = S.summarize(_write(tmp_path, RED))
        assert not summary.ok
        assert summary.failed == 2            # one <failure> and one <error>
        text = summary.markdown
        assert "test_audio.py::TestListener::test_mic" in text
        assert "test_policy.py::test_deny" in text
        assert "test_collect.py" in text
        assert "AssertionError: 3 != 4" in text

    def test_env_hint_for_missing_alsa(self, S, tmp_path):
        text = S.summarize(_write(tmp_path, RED)).markdown
        assert "**Likely cause:**" in text and "libasound2t64" in text

    def test_env_hint_names_the_right_package_for_a_missing_portaudio(self, S):
        """The digest must not answer this one with libasound2t64.

        These are two different packages and the distinction cost a pipeline:
        sounddevice resolves the PortAudio library itself at import, so the
        trap is that the ALSA hint looks like the right one -- the two can even
        appear in the same traceback.
        """
        got = S._env_hint("OSError: PortAudio library not found")
        assert got and "**Fix:**" in got, got
        # The FIX must be the PortAudio package. Mentioning the ALSA one in the
        # explanation is useful (installing it alone does not help); offering it
        # as the fix is the misdiagnosis that kept CI red.
        fix = got.split("**Fix:**", 1)[1]
        assert "libportaudio2" in fix, got
        assert "libasound2t64" not in fix, got

    def test_a_traceback_naming_both_still_fixes_the_portaudio_hint(self, S):
        """One traceback can carry both strings; the first entry must win."""
        both = ("OSError: PortAudio library not found\n"
                "  OSError: libasound.so.2: cannot open shared object file")
        fix = S._env_hint(both).split("**Fix:**", 1)[1]
        assert "libportaudio2" in fix, fix

    def test_env_hints_cover_each_known_library(self, S):
        for message, expected in (
            ("OSError: PortAudio library not found", "libportaudio2"),
            ("OSError: libasound.so.2: cannot open shared object file", "libasound2t64"),
            ("OSError: libgomp.so.1: cannot open shared object file", "libgomp1"),
            ("ImportError: libGL.so.1: cannot open", "offscreen-Qt"),
            ("ImportError: libEGL.so.1: cannot open", "offscreen-Qt"),
            ("ImportError: libxkbcommon.so.0: cannot open", "offscreen-Qt"),
            ("ImportError: libdbus-1.so.3: cannot open", "dbus"),
            ("ModuleNotFoundError: No module named 'numpy'", "requirements"),
        ):
            got = S._env_hint(message)
            assert got and expected in got, message

    def test_plain_assertion_gets_no_env_hint(self, S):
        assert S._env_hint("AssertionError: 3 != 4") is None

    def test_missing_report_says_so_and_never_raises(self, S, tmp_path):
        summary = S.summarize(tmp_path / "nope.xml")
        assert not summary.found and not summary.ok
        assert "not found" in summary.markdown

    def test_malformed_report_never_raises(self, S, tmp_path):
        summary = S.summarize(_write(tmp_path, "<testsuites><oops"))
        assert not summary.found and "unreadable" in summary.markdown

    def test_long_messages_are_clipped_but_hints_still_fire(self, S, tmp_path):
        long_msg = "libasound.so.2 missing. " + ("x" * 900)
        xml = RED.replace("OSError: libasound.so.2: cannot open shared object file",
                          long_msg)
        text = S.summarize(_write(tmp_path, xml)).markdown
        assert "…" in text and "libasound2t64" in text


class TestMain:
    def test_main_returns_zero_for_green_red_and_missing(self, S, tmp_path, capsys,
                                                        clean_env):
        green = _write(tmp_path, GREEN)
        red = _write(tmp_path, RED)
        assert S.main(["pytest_summary.py", str(green)]) == 0
        assert S.main(["pytest_summary.py", str(red)]) == 0
        assert S.main(["pytest_summary.py", str(tmp_path / "gone.xml")]) == 0
        assert capsys.readouterr().out            # always prints something


class TestEmit:
    def test_plain_output_outside_gitlab(self, S, tmp_path, capsys, clean_env):
        S.emit(S.summarize(_write(tmp_path, RED)))
        out = capsys.readouterr().out
        assert "section_start" not in out
        assert "test_audio.py::TestListener::test_mic" in out

    def test_failures_open_themselves(self, S, tmp_path, capsys, clean_env):
        clean_env.setenv("GITLAB_CI", "true")
        S.emit(S.summarize(_write(tmp_path, RED)))
        out = capsys.readouterr().out
        assert "section_start" in out and "section_end" in out
        assert "[collapsed=true]" not in out

    def test_green_digest_stays_collapsed(self, S, tmp_path, capsys, clean_env):
        clean_env.setenv("GITLAB_CI", "true")
        S.emit(S.summarize(_write(tmp_path, GREEN)))
        assert "[collapsed=true]" in capsys.readouterr().out


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPostNote:
    """The CI job token cannot create notes (GitLab #464591), so the token is
    a masked variable and every failure mode must stay non-fatal."""

    def test_no_token_skips_and_explains(self, S, clean_env, capsys):
        clean_env.setenv("CI_PROJECT_ID", "1")
        clean_env.setenv("CI_MERGE_REQUEST_IID", "2")
        assert S.post_note("body") is None
        assert "GITLAB_SUMMARY_TOKEN is not set" in capsys.readouterr().out

    def test_non_mr_pipeline_skips(self, S, clean_env, capsys):
        clean_env.setenv("GITLAB_SUMMARY_TOKEN", "tok")
        clean_env.setenv("CI_PROJECT_ID", "1")
        assert S.post_note("body") is None
        assert "not a merge request pipeline" in capsys.readouterr().out

    def test_posts_to_the_merge_request_notes_endpoint(self, S, clean_env,
                                                       monkeypatch, capsys):
        clean_env.setenv("GITLAB_SUMMARY_TOKEN", "tok")
        clean_env.setenv("CI_PROJECT_ID", "42")
        clean_env.setenv("CI_MERGE_REQUEST_IID", "7")
        clean_env.setenv("CI_API_V4_URL", "https://gitlab.example/api/v4")
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            seen["body"] = json.loads(request.data.decode("utf-8"))
            seen["method"] = request.get_method()
            return _FakeResponse({"web_url": "https://gitlab.example/-/notes/9"})

        monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
        assert S.post_note("### pytest summary") == "https://gitlab.example/-/notes/9"
        assert seen["url"] == \
            "https://gitlab.example/api/v4/projects/42/merge_requests/7/notes"
        assert seen["headers"]["private-token"] == "tok"
        assert seen["body"] == {"body": "### pytest summary"}
        assert seen["method"] == "POST"

    def test_open_mr_variable_supplies_the_iid(self, S, clean_env, monkeypatch):
        clean_env.setenv("GITLAB_SUMMARY_TOKEN", "tok")
        clean_env.setenv("CI_PROJECT_ID", "42")
        clean_env.setenv("CI_OPEN_MERGE_REQUESTS", "group/proj!31,group/proj!32")
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            return _FakeResponse({"web_url": "u"})

        monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
        S.post_note("body")
        assert seen["url"].endswith("/merge_requests/31/notes")

    def test_network_error_is_swallowed(self, S, clean_env, monkeypatch, capsys):
        clean_env.setenv("GITLAB_SUMMARY_TOKEN", "tok")
        clean_env.setenv("CI_PROJECT_ID", "42")
        clean_env.setenv("CI_MERGE_REQUEST_IID", "7")

        def boom(request, timeout=None):
            raise OSError("connection refused")

        monkeypatch.setattr(S.urllib.request, "urlopen", boom)
        assert S.post_note("body") is None
        assert "could not post" in capsys.readouterr().out

    def test_main_posts_only_with_the_flag(self, S, tmp_path, clean_env,
                                           monkeypatch, capsys):
        red = _write(tmp_path, RED)
        calls = []
        monkeypatch.setattr(S, "post_note", lambda body: calls.append(body) or "u")

        S.main(["pytest_summary.py", str(red)])
        assert calls == []
        S.main(["pytest_summary.py", "--post", str(red)])
        assert calls and "pytest summary" in calls[0]


class TestCompileAll:
    """The compile gate discovers its files, so a new module cannot be skipped.

    Both workflows used to enumerate what to compile: two lists to keep in
    sync, and a silent hole whenever a module was added. That is how
    core/theme.py was never even syntax-checked by the installer's own gate.
    """

    def test_discovers_every_source_except_the_archive(self, C):
        found = {p.relative_to(HERE).as_posix() for p in C.discover(HERE)}
        assert {"handsoff.py", "settings_schema.py", "hardware.py",
                "handsoff-settings.py"} <= found
        assert {"core/theme.py", "core/settings.py"} <= found
        assert "ci/compile_all.py" in found
        assert "tests/test_ci_summary.py" in found
        assert not any(rel.startswith("attic/") for rel in found)

    def test_skips_hidden_directories(self, C, tmp_path):
        """A stray .venv must not decide the gate."""
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "dep.py").write_text("x = 1\n")
        (tmp_path / "ok.py").write_text("x = 1\n")
        assert C.discover(tmp_path) == [tmp_path / "ok.py"]

    def test_broken_source_fails_the_gate(self, C, tmp_path, capsys):
        (tmp_path / "bad.py").write_text("def f(:\n")
        assert C.main(["compile_all.py", str(tmp_path)]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_archive_is_not_compiled(self, C, tmp_path, capsys):
        (tmp_path / "attic").mkdir()
        (tmp_path / "attic" / "rotten.py").write_text("def f(:\n")
        (tmp_path / "ok.py").write_text("x = 1\n")
        assert C.main(["compile_all.py", str(tmp_path)]) == 0
        assert "byte-compiled 1 files" in capsys.readouterr().out

    def test_empty_tree_is_a_failure_not_a_pass(self, C, tmp_path, capsys):
        """A gate that finds nothing is broken, not green."""
        assert C.main(["compile_all.py", str(tmp_path)]) == 1
        assert "refusing to report success" in capsys.readouterr().err

    def test_a_missing_root_is_named_as_the_root(self, C, tmp_path, capsys):
        """A mistyped root used to read as "a tree with no Python in it".

        Both are failures, so the run still refused — but the message pointed
        at the wrong problem, and a gate whose diagnosis can be wrong is one
        somebody works around instead of fixing.
        """
        missing = tmp_path / "not-there"
        assert C.main(["compile_all.py", str(missing)]) == 1
        err = capsys.readouterr().err
        assert "does not exist" in err and "not-there" in err

    def test_a_root_that_is_a_file_is_named_as_such(self, C, tmp_path, capsys):
        """`rglob` on a file raises NotADirectoryError — caught, not traced."""
        a_file = tmp_path / "a_module.py"
        a_file.write_text("x = 1\n")
        assert C.main(["compile_all.py", str(a_file)]) == 1
        err = capsys.readouterr().err
        assert "not a directory" in err and "a_module.py" in err

    def test_the_real_tree_passes(self, C, capsys):
        assert C.main(["compile_all.py", str(HERE)]) == 0
        assert "byte-compiled" in capsys.readouterr().out


class TestShellDiscovery:
    """The shell-syntax gate discovers scripts by SHEBANG, in three places.

    Three copies of one rule is two too many, and this pair HAS drifted (GitLab
    checked `ci/apt_deps.sh` and GitHub did not, so a broken script passed one
    gate and failed the other), which is why the copies are pinned equal here
    rather than trusted. Two hazards the audit named are pinned too: an unpruned
    walk probes every file of a virtualenv or `node_modules`, and a whole-file
    read of a multi-GB blob pulls it into a shell variable.
    """

    SOURCES = ("ci/gates.sh", ".gitlab-ci.yml", ".github/workflows/ci.yml")
    # The prune set that matters in CI: a vendored tree or a build output inside
    # the checkout. The agent/cache dirs below are local-only and only the local
    # runner needs them.
    MUST_PRUNE = (".git", "attic", ".venv", "venv", "env", "node_modules",
                  "build", "dist")

    def _text(self, rel: str) -> str:
        return (HERE / rel).read_text(encoding="utf-8")

    def test_every_copy_prunes_vendored_and_build_directories(self):
        for rel in self.SOURCES:
            text = self._text(rel)
            for d in self.MUST_PRUNE:
                assert f"'./{d}/*'" in text, (
                    f"{rel} walks ./{d} — a virtualenv inside the checkout means "
                    f"every one of its files is probed for a shebang")

    def test_the_local_runner_also_prunes_the_agent_and_cache_dirs(self):
        text = self._text("ci/gates.sh")
        for d in (".freebuff", ".claude-flow", ".swarm", ".agents", ".codex",
                  ".tox", ".mypy_cache", "site-packages"):
            assert f"'./{d}/*'" in text, f"ci/gates.sh walks ./{d}"

    def test_no_copy_reads_a_whole_file_to_look_at_line_one(self):
        """`head -1` (or bash's `read`) consumes until a NEWLINE: on a binary
        without one, that is the whole file. `head -c 128` is exact instead."""
        for rel in self.SOURCES:
            text = self._text(rel)
            assert "head -c 128" in text, f"{rel} does not bound its first-line read"
            assert 'head -1 "$f"' not in text, (
                f"{rel} reads a whole file to find line 1")
            # ...and NULs must be stripped inside the substitution: capturing a
            # NUL makes bash warn once per binary file in the tree.
            assert "tr -d" in text, f"{rel} would warn once per binary file"

    def test_the_copies_agree_on_what_counts_as_a_shell_script(self):
        """Drift in the PATTERN is drift too: one gate accepting what another
        refuses is how a file passes CI and fails on the next machine."""
        for rel in self.SOURCES:
            assert "'#!'*bash*|'#!'*'/sh'*) ;;" in self._text(rel), (
                f"{rel} classifies shebangs differently from the others")

    def test_the_gate_really_skips_a_pruned_tree_when_run(self, tmp_path):
        """Behavioural, not textual: seed a vendored dir whose script WOULD be
        found, run the gate, and require it to be ignored.

        In a tree of its own rather than in the checkout. Seeding `.venv` beside
        the source meant this test wrote into the developer's tree, and that it
        had to SKIP itself whenever a real `.venv` was already there — the one
        machine where the pruning it tests matters most. What the gate reads is
        the tree it stands in (its `find .` IS the discovery), so a copy of the
        script plus the shell file it must find is the whole fixture.
        """
        tree = tmp_path / "gate-tree"
        (tree / "ci").mkdir(parents=True)
        shutil.copy2(HERE / "ci" / "gates.sh", tree / "ci" / "gates.sh")
        shutil.copy2(HERE / "install.sh", tree / "install.sh")
        venv = tree / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "evil.sh").write_text("#!/usr/bin/env bash\n",
                                              encoding="utf-8")
        # A newline-free blob, the file that made an unbounded first-line read
        # pull megabytes into a variable.
        (venv / "blob.bin").write_bytes(b"\x00\x01" * 40000)
        proc = subprocess.run(["bash", "ci/gates.sh", "shell"], cwd=tree,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert ".venv" not in proc.stdout, (
            "the gate probed a vendored tree it is supposed to prune")
        assert "install.sh" in proc.stdout, "the gate found nothing at all"


class TestBothOrderRunsKeepTheirOwnReport:
    """The GitLab `order` job runs the suite twice and used to keep ONE report.

    Both invocations ended in `--junitxml=tests/report.xml`, so the second run's
    report replaced the first's: a FILE-order failure overwrote the seed-order
    run's verdict in GitLab's Test tab, and the single report the shared anchor
    uploads described only whichever run happened to be last. Each run has its
    own file now — and both halves of that (the artifact list and the log
    digest) have to stay wired, or the second run goes back to being invisible.

    The upload is pinned here rather than left to the schema: a job that declares
    `artifacts` REPLACES the one it inherits through `<<: *shared`, so dropping
    the job-level block would silently revert to the single-report upload —
    which is exactly the bug this guard exists for. A junit ARRAY is part of
    GitLab's schema (checked against its own ci.json), so both reports really
    are accepted, not merely listed.
    """

    def test_each_ordered_run_writes_its_own_report(self):
        block = _gitlab_blocks()["order"]
        script = block.split("\n  script:", 1)[1]
        flags = re.findall(r'--junitxml="([^"]+)"', script)
        assert len(flags) == 2, (
            f"the job runs the suite twice and must write two reports: {flags}")
        assert len(set(flags)) == 2, (
            f"both ordered runs write the same file ({flags[0]}) — the second "
            f"run's report then REPLACES the first's")
        assert {pathlib.PurePath(f).name for f in flags} == {
            "report.xml", "report-order-files.xml"}
        # ...and each report belongs to the run directly above it. The digest
        # labels them by order, so a swapped pair would report one run's
        # failures under the other run's name.
        seed = script.index("HANDSOFF_TEST_ORDER_SEED")
        files = script.index("HANDSOFF_TEST_ORDER_FILES")
        first = script.index(flags[0])
        second = script.index(flags[1], first + 1)
        assert seed < first < files < second, (
            "the SEED run must write the first report and the FILE-order run the "
            "second — the artifact list and the digest both assume that order")

    def test_the_job_uploads_both_reports(self):
        block = _gitlab_blocks()["order"]
        assert "artifacts:" in block, (
            "without a job-level artifacts block the inherited single-report "
            "upload returns and the second run's failures vanish from the tab")
        artifacts = block.split("artifacts:", 1)[1].split("after_script:", 1)[0]
        for rel in ("tests/report.xml", "tests/report-order-files.xml"):
            assert f"- {rel}" in artifacts, (
                f"{rel} is written but never uploaded — that run's failures "
                f"would be missing from the Test tab")
        # In the same order the digest reads them, so GitLab's Test tab and the
        # log name the two runs identically. Listed in the other order the tab
        # shows the FILE-order entry first while the note describes the seed
        # run — both present, and a reader comparing them is misled.
        assert (artifacts.index("- tests/report.xml")
                < artifacts.index("- tests/report-order-files.xml")), (
            "the artifact list names the FILE-order report first, so the Test "
            "tab's first entry is not the run the --post note summarizes")
        # The restatement has to be complete: overriding `artifacts` drops the
        # anchor's `when: always`, and without it a failing run uploads nothing.
        assert "when: always" in artifacts and "expire_in:" in artifacts

    def test_the_digest_reads_both_and_posts_one_summary(self):
        block = _gitlab_blocks()["order"]
        after = block.split("after_script:", 1)[1]
        # COMMAND lines, not prose: the comment above this block discusses
        # `--post` in words, and a raw text count cannot tell the two apart.
        lines = after.splitlines()
        digests = [line for line in lines if "ci/pytest_summary.py" in line]
        assert len(digests) == 2, (
            "both reports belong in the log digest; the shared anchor's single "
            "digest is replaced by this one, so it has to name both")
        # WHICH report each digest reads, not just how many there are: the pair
        # is written as `python …/pytest_summary.py` on one line and the report
        # path on the next, so the count stays 2 and both paths stay present
        # when the two are swapped — which silently points the merge-request
        # note at the FILE-order run instead of the seed run it follows.
        idx = [n for n, line in enumerate(lines)
               if "ci/pytest_summary.py" in line]
        targets = [lines[n + 1].strip() for n in idx]
        assert targets[0].startswith('"$CI_PROJECT_DIR/tests/report.xml"'), (
            f"the first digest reads {targets[0]!r}: it follows the job's first "
            f"invocation, so it must read the seed run's report")
        assert targets[1].startswith(
            '"$CI_PROJECT_DIR/tests/report-order-files.xml"'), (
            f"the second digest reads {targets[1]!r}: it follows the job's "
            f"second invocation, so it must read the FILE-order run's report")
        posts = [n for n, line in enumerate(lines)
                 if line.strip().endswith("--post")]
        assert len(posts) == 1, (
            "the merge-request note is one summary — a second --post would "
            "comment on the same pipeline twice")
        assert posts[0] == idx[0] + 1, (
            "--post must ride the FIRST digest: the note then summarizes the "
            "run the artifact list and the first Test-tab entry describe")

    def test_only_the_order_job_overrides_the_shared_upload(self):
        blocks = _gitlab_blocks()
        owners = sorted(name for name in blocks
                        if not name.startswith(".") and "artifacts:" in blocks[name])
        assert owners == ["order"], (
            f"a suite job that declares its own artifacts silently drops the "
            f"shared junit upload: {owners}")
        assert "junit: tests/report.xml" in blocks[".shared"], (
            "the anchor every other suite job inherits must still upload one")
        # ...and every job that runs the suite must carry a report, inherited or
        # its own. The `tests:3.12` / `tests:3.13` pair shares one key here: the
        # block parser splits a key at its FIRST colon, and the two jobs are
        # `extends:`-identical apart from the image.
        runners = {name: _gitlab_effective(name, blocks) for name in blocks
                   if not name.startswith(".")
                   and "python -m pytest" in _gitlab_effective(name, blocks)}
        assert runners, "no job runs pytest — this assertion would be vacuous"
        for name, text in runners.items():
            # The UPLOAD, not the writing flag: `--junitxml=` is in the script of
            # every suite job, so requiring the word "junit" would be satisfied
            # by a job that writes a report and never hands it to GitLab.
            assert "junit: tests/report.xml" in text, (
                f"{name} runs the suite but uploads no junit report, so its "
                f"failures are a log to read rather than a Test-tab entry")


class TestTheDeskRunnerOnlyEverRunsProtectedCode:
    """Every job runs on `desk` — a self-hosted runner on the developer's OWN
    machine — so an untrusted pipeline is an arbitrary-code-execution problem,
    not a test-infrastructure one.

    Audit finding P1 (2026-09-27). Two properties carried it, and both are
    pinned here because both are the kind of edit that looks like tidy-up:

      * `workflow.rules` used to ADMIT `merge_request_event`, so a merge
        request's own `tests/`, `ci/` and `install.sh` ran on the workstation
        — with the runner account's home directory, SSH agent, GPU and audio
        devices. The rule must stay present AND carry `when: never`.
      * `.shared`'s `after_script` runs the CHECKED-OUT tree's
        `ci/pytest_summary.py`, which reads `GITLAB_SUMMARY_TOKEN` from the
        environment — a real personal access token, not a job token, because a
        job token cannot create notes. It must not run on a ref that is not
        protected.

    The first property is the one that matters: refuse the pipeline and there is
    no window for the second to matter. The second is defence in depth for
    anyone who re-admits MR pipelines later without reading this.
    """

    def _workflow_rules(self) -> str:
        text = (HERE / ".gitlab-ci.yml").read_text(encoding="utf-8")
        start = text.index("\nworkflow:\n")
        rest = text[start + 1:]
        end = rest.index("\n\n", rest.index("  rules:"))
        return rest[:end]

    def test_merge_request_pipelines_are_refused_not_admitted(self):
        rules = self._workflow_rules()
        assert "merge_request_event" in rules, (
            "the merge_request_event rule was removed outright. Deleting it "
            "re-admits MR pipelines through the trailing `- if: "
            "$CI_COMMIT_BRANCH`, which matches an MR's source branch. The rule "
            "must stay, with `when: never` — the refusal IS the rule.")
        mr = rules.split('CI_PIPELINE_SOURCE == "merge_request_event"')[1]
        assert "when: never" in mr.split("\n- if:")[0], (
            "merge_request_event is admitted without `when: never`, so an "
            "untrusted MR runs its own tests on the self-hosted desk runner")

    def test_the_token_bearing_after_script_asks_whether_the_ref_is_protected(self):
        shared = _gitlab_blocks()[".shared"]
        assert "CI_COMMIT_REF_PROTECTED" in shared, (
            ".shared's after_script no longer gates on CI_COMMIT_REF_PROTECTED, "
            "so the tree's own ci/pytest_summary.py runs — with "
            "GITLAB_SUMMARY_TOKEN in the environment — on any ref")
        assert "GITLAB_SUMMARY_TOKEN" in shared and "unset" in shared, (
            "the non-protected branch must also UNSET the token, not merely "
            "skip the post: the variable is already in the job's environment")

    def test_the_summary_token_is_still_required_to_post(self):
        """The guard must not have quietly disabled posting on protected refs.

        A fix that made every note stop appearing would pass the two guards
        above. The protected branch still has to run the real script.
        """
        shared = _gitlab_blocks()[".shared"]
        assert "ci/pytest_summary.py" in shared, (
            "the summary digest is how a failure becomes a short list of names "
            "instead of a log to read; it must still run on a protected ref")
        assert "--post" in shared

    def test_no_desk_job_is_left_unguarded_by_the_workflow_rules(self):
        """The rules only help if the jobs inherit them — sanity, not a proxy.

        Every desk job is admitted by the SAME workflow rules, so this asserts
        the file still has one `workflow:` block governing them all, rather
        than a per-job `rules:` that could re-admit something.
        """
        blocks = _gitlab_blocks()
        assert "workflow" in blocks, "the workflow block was removed"
        # The `desk` TAG, not the word: `suite:hosted` runs in a frozen
        # container and merely mentions the desk in a comment, so a substring
        # match would pull a job that is not on the workstation into this
        # assertion — and `suite:hosted` legitimately carries its own `rules:`
        # (web/api only, manual, allowed to fail), which is exactly the shape
        # this test forbids everywhere else.
        desk = sorted(name for name in blocks
                      if not name.startswith(".")
                      and re.search(r"^\s*-\s+desk\s*$", blocks[name], re.M))
        assert desk, "no desk job found — this test would be vacuous"
        for name in desk:
            # A YAML KEY, not the substring: the `shell` job's own trust guard
            # greps for the literal text `^  rules:` inside a shell string, so
            # matching on the word finds the guard that protects this file.
            per_job = re.search(r"^\s{2}rules:", blocks[name], re.M)
            assert per_job is None, (
                f"{name} is a desk job with its own `rules:`; a per-job rule "
                f"can re-admit a pipeline the workflow rules refuse")
