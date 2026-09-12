"""Tests for the CI tooling that runs inside the pipeline.

ci/pytest_summary.py turns a red run into a readable digest and can post it as
an MR note (network, so worth pinning). ci/compile_all.py is the compile gate
the workflows call — it discovers its own file set precisely so a new module
cannot slip past uncompiled, which is worth pinning too. Neither is shipped
app code, but this is the code a maintainer reads first when the gate fails.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / "ci" / filename)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the
    # module has to be registered before it executes.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


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

    @staticmethod
    def _blocks():
        """Top-level key -> its block of text. No yaml dependency for CI config.

        Everything, including `name: value` lines like `.qt_deps: &qt_deps`, is
        a block here — treating only `name:` lines as keys folds the anchors
        into whichever job came before them, which is how a first attempt at
        this test accused `default:` of running pytest.
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

    def _effective(self, name, blocks, seen=()):
        """A job's text plus what it inherits — via `extends:` and `<<: *shared`.

        A job that runs pytest through an `extends:` template must be covered
        too, or deleting the apt layer from the template would leave this test
        green and vacuous.
        """
        text = blocks[name]
        for ref in re.findall(r"^\s*extends:\s*([\w.]+)", text, re.M):
            if ref in blocks and ref not in seen:
                text += "\n" + self._effective(ref, blocks, seen + (name,))
        if "<<: *shared" in text and ".shared" in blocks and ".shared" not in seen:
            text += "\n" + blocks[".shared"]
        return text

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

    def test_the_real_tree_passes(self, C, capsys):
        assert C.main(["compile_all.py", str(HERE)]) == 0
        assert "byte-compiled" in capsys.readouterr().out
