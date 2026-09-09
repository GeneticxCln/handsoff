"""Hardware snapshot tests: degraded dicts, mic matching, TTL, prompt cap.

No real hardware: every slow prober is injected (real local sockets only
for the ydotool connectability unit tests). Style mirrors
test_hardening.py (module loaded by path, tmp sandbox dirs)."""
from __future__ import annotations

import importlib.util
import json
import socket
import types
from pathlib import Path

import pytest

from conftest import HERE as ROOT

HERE = ROOT   # the repo root


def _load_hw():
    spec = importlib.util.spec_from_file_location(
        "hardware_mod", HERE / "hardware.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def HW():
    return _load_hw()


def _boom(*a, **k):
    raise RuntimeError("no hardware in tests")


def _ok_probers(**over):
    """All-succeeding probers; callers override per case."""
    p = {
        "query_devices": lambda: [{"name": "Yeti Stereo", "max_input_channels": 2}],
        "ollama_tags": lambda base: json.dumps({"models": [{"name": "qwen3:8b"}]}),
        "nvidia_smi": lambda: "GPU 0: Fake RTX (UUID: GPU-123)\n",
        "niri_windows": lambda: types.SimpleNamespace(returncode=0, stdout="[{}]"),
        "ydotool_which": lambda: "/usr/bin/ydotool",
        "socket_connectable": lambda sock: True,
    }
    p.update(over)
    return p


def _ctx(tmp_path, **over):
    vdir = tmp_path / "piper-voice"
    vdir.mkdir(exist_ok=True)
    (vdir / " voice.onnx".strip()).write_text("fake")
    (vdir / "voice.onnx.json").write_text("{}")
    wdir = tmp_path / "whisper-model"
    wdir.mkdir(exist_ok=True)
    (wdir / "tiny.pt").write_text("fake")
    ctx = {
        "ollama_base": "http://127.0.0.1:9", "ollama_model": "qwen3:8b",
        "whisper_size": "tiny", "mic_device": "", "piper_voice": "",
        "whisper_model_dir": str(wdir), "piper_voice_dir": str(vdir),
        "control_sock": str(tmp_path / "control.sock"),
        "state_dir": str(tmp_path),
        "systemd_unit_file": str(tmp_path / "handsoff.service"),
    }
    ctx.update(over)
    return ctx


class TestSnapshotShape:
    def test_all_keys_present_degraded(self, HW, tmp_path):
        boom = {k: _boom for k in (
            "query_devices", "ollama_tags", "nvidia_smi", "niri_windows",
            "ydotool_which", "ydotool_socket", "socket_connectable",
            "fastfetch_which", "fastfetch_json")}
        snap = HW.snapshot(_ctx(tmp_path), boom, {})
        assert set(snap) == set(HW.SECTIONS) | {"ts"}
        for section in HW.SECTIONS:
            assert isinstance(snap[section], dict), section

    def test_never_raises_minimal_ctx(self, HW):
        boom = {k: _boom for k in (
            "query_devices", "ollama_tags", "nvidia_smi", "niri_windows",
            "ydotool_which", "ydotool_socket", "socket_connectable",
            "fastfetch_which", "fastfetch_json")}
        snap = HW.snapshot({}, boom, None)  # minimal ctx, no cache either
        assert len(snap) == 14


class TestAudio:
    def test_failure_is_degraded(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path),
                           _ok_probers(query_devices=_boom), {})
        assert snap["audio"] == {"ok": False, "error": "no hardware in tests"}

    def test_filters_to_inputs_only(self, HW, tmp_path):
        devs = [{"name": "Speakers", "max_input_channels": 0},
                {"name": "Yeti Stereo", "max_input_channels": 2},
                {"name": "Monitor", "max_input_channels": 0}]
        snap = HW.snapshot(_ctx(tmp_path),
                           _ok_probers(query_devices=lambda: devs), {})
        assert snap["audio"]["inputs"] == ["Yeti Stereo"]

    def test_default_matches_configured_mic(self, HW, tmp_path):
        devs = [{"name": "Built-in", "max_input_channels": 1},
                {"name": "Yeti Stereo Microphone", "max_input_channels": 2}]
        snap = HW.snapshot(_ctx(tmp_path, mic_device="yeti"),
                           _ok_probers(query_devices=lambda: devs), {})
        assert snap["audio"]["default"] == "Yeti Stereo Microphone"

    def test_empty_device_list(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path),
                           _ok_probers(query_devices=lambda: []), {})
        assert snap["audio"]["count"] == 0 and "note" in snap["audio"]


class TestOllama:
    def test_down_is_degraded(self, HW, tmp_path):
        def down(base):
            raise ConnectionError("refused")
        snap = HW.snapshot(_ctx(tmp_path), _ok_probers(ollama_tags=down), {})
        assert snap["ollama"]["ok"] is False

    def test_timeout_is_degraded(self, HW, tmp_path):
        def slow(base):
            raise TimeoutError("timed out")
        snap = HW.snapshot(_ctx(tmp_path), _ok_probers(ollama_tags=slow), {})
        assert snap["ollama"] == {"ok": False, "error": "timed out"}


class TestSttTtsMounts:
    def test_empty_voice_dir(self, HW, tmp_path):
        empty = tmp_path / "empty-voices"
        empty.mkdir()
        snap = HW.snapshot(_ctx(tmp_path, piper_voice_dir=str(empty)),
                           _ok_probers(), {})
        assert snap["stt_tts"]["onnx"] == []

    def test_onnx_without_json_flagged(self, HW, tmp_path):
        vdir = tmp_path / "voices"
        vdir.mkdir()
        (vdir / "solo.onnx").write_text("fake")  # no .json sidecar
        snap = HW.snapshot(_ctx(tmp_path, piper_voice_dir=str(vdir)),
                           _ok_probers(), {})
        assert snap["stt_tts"]["onnx_json_ok"] == {"solo.onnx": False}
        assert snap["stt_tts"]["ok"] is False

    def test_missing_socket(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path), _ok_probers(), {})
        assert snap["mounts"]["sock_present"] is False
        assert "control.sock" in snap["mounts"]["sock_path"]


class TestTTL:
    # probers behind TTL>0 sections (systemd reads a file directly: no prober)
    SLOW = {"query_devices", "nvidia_smi", "ollama_tags", "niri_windows",
            "ydotool_which", "socket_connectable"}

    def test_cache_hit_makes_zero_prober_calls(self, HW, tmp_path):
        calls: list = []

        def count(name):
            def inner(*a, **k):
                calls.append(name)
                return _ok_probers()[name](*a, **k)
            return inner

        probers = {k: count(k) for k in (
            "query_devices", "ollama_tags", "nvidia_smi", "niri_windows",
            "ydotool_which", "socket_connectable")}
        cache: dict = {}
        HW.snapshot(_ctx(tmp_path), probers, cache)
        slow = [c for c in calls if c in self.SLOW]
        assert slow, "slow probers never ran"
        calls.clear()
        HW.snapshot(_ctx(tmp_path), probers, cache)
        assert [c for c in calls if c in self.SLOW] == []

    def test_force_reprobes(self, HW, tmp_path):
        calls: list = []
        probers = _ok_probers(ollama_tags=lambda base: calls.append(1) or "{}")
        cache: dict = {}
        HW.snapshot(_ctx(tmp_path), probers, cache)
        HW.snapshot(_ctx(tmp_path), probers, cache, force=True)
        assert len(calls) == 2


class TestPromptContext:
    def test_token_cap(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path), _ok_probers(), {})
        assert len(HW.prompt_context(snap)) <= 600
        assert len(HW.prompt_context(snap, max_chars=50)) <= 50
        assert len(HW.prompt_context(snap).splitlines()) <= 5

    def test_exact_paths_verbatim(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path), _ok_probers(), {})
        text = HW.prompt_context(snap)
        assert str(tmp_path / "control.sock") in text  # socket path exact
        assert "voice.onnx" in text                    # voice file exact


def _ff_json():
    """Minimal fastfetch -j payload: real shapes + error + invoker Shell."""
    mods = [
        {"type": "Title", "result": {"userName": "quinton"}},
        {"type": "Separator", "result": None},
        {"type": "OS", "result": {"prettyName": "CachyOS", "id": "cachyos"}},
        {"type": "Host", "result": {"vendor": "Micro-Star", "name": "MS-7D53"}},
        {"type": "Kernel", "result": {"release": "7.2.3-1-cachyos"}},
        {"type": "Shell", "result": {"exe": "timeout", "exeName": "timeout"}},
        {"type": "Display", "result": [
            {"name": "MO32U", "preferred": {"width": 3840, "height": 2160}}]},
        {"type": "DE", "result": None},
        {"type": "WM", "result": {"prettyName": "niri", "protocolName": "Wayland"}},
        {"type": "WMTheme", "result": None},
        {"type": "TerminalFont", "result": None},
        {"type": "CPU", "result": {"cpu": "AMD Ryzen 7 5800XT",
                                   "cores": {"logical": 16}}},
        {"type": "GPU", "result": [{"name": "GeForce RTX 4060 Ti"}]},
        {"type": "Memory", "result": {"total": 33566052352}},
        {"type": "Disk", "result": [
            {"mountpoint": "/", "bytes": {"total": 1006450229248,
                                          "free": 371291201536}}]},
        {"type": "Packages", "result": {"pacman": 1487}},
        {"type": "Uptime", "result": {"uptime": 10509010}},
        {"type": "Locale", "result": "en_US.UTF-8"},
        {"type": "Break", "result": None},
        {"type": "Colors", "result": None},
    ]
    return json.dumps(mods)


class TestFastfetch:
    def _probers(self, **over):
        p = _ok_probers(fastfetch_which=lambda: "/usr/bin/fastfetch",
                        fastfetch_json=lambda: _ff_json())
        p.update(over)
        return p

    def test_missing_binary_degraded_zero_subprocess(self, HW, tmp_path):
        calls: list = []
        p = self._probers(fastfetch_which=lambda: None,
                          fastfetch_json=lambda: calls.append(1) or "")
        snap = HW.snapshot(_ctx(tmp_path), p, {})
        assert snap["fastfetch"] == {"ok": False, "installed": False,
                                     "note": "fastfetch not installed"}
        assert calls == []

    def test_error_entries_skipped_and_shell_ignored(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path), self._probers(), {})
        ff = snap["fastfetch"]
        assert ff["ok"] is True
        assert ff["os"] == "CachyOS" and ff["host"] == "Micro-Star MS-7D53"
        assert ff["cpu"] == "AMD Ryzen 7 5800XT"
        assert "timeout" not in json.dumps(ff)  # invoker Shell never trusted
        assert "shell" not in {k.lower() for k in ff}

    def test_timeout_keeps_last_good(self, HW, tmp_path):
        cache: dict = {}
        HW.snapshot(_ctx(tmp_path), self._probers(), cache)
        def slow():
            raise TimeoutError("timed out")
        snap = HW.snapshot(_ctx(tmp_path), self._probers(fastfetch_json=slow),
                           cache, force=True)
        ff = snap["fastfetch"]
        assert ff["degraded"] is True and ff["cpu"] == "AMD Ryzen 7 5800XT"

    def test_ttl_hit_zero_calls_and_force_reprobes(self, HW, tmp_path):
        calls: list = []
        p = self._probers(fastfetch_json=lambda: calls.append(1) or _ff_json())
        cache: dict = {}
        HW.snapshot(_ctx(tmp_path), p, cache)
        assert calls, "fastfetch prober never ran"
        calls.clear()
        HW.snapshot(_ctx(tmp_path), p, cache)
        assert calls == []
        HW.snapshot(_ctx(tmp_path), p, cache, force=True)
        assert len(calls) == 1

    def test_prompt_cap_holds_with_fastfetch(self, HW, tmp_path):
        snap = HW.snapshot(_ctx(tmp_path), self._probers(), {})
        text = HW.prompt_context(snap)
        assert len(text) <= 600 and len(text.splitlines()) <= 5
        for exact in ("CachyOS", "MS-7D53", "5800XT", "4060 Ti", "MO32U",
                      "3840x2160", "31 GiB"):
            assert exact in text, exact


def _live_dgram(tmp_path, name="ydotool.sock"):
    """A bound SOCK_DGRAM unix socket, like ydotoold 1.x (no listen)."""
    path = str(tmp_path / name)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    return s, path


class TestYdotoolSockets:
    def test_dgram_socket_is_connectable(self, HW, tmp_path):
        """Regression: STREAM-only probing says False for a live DGRAM daemon
        (EPROTOTYPE on stream connect) even though it answers."""
        s, path = _live_dgram(tmp_path)
        try:
            assert HW._sock_ok(path) is True
        finally:
            s.close()

    def test_stream_socket_is_connectable(self, HW, tmp_path):
        path = str(tmp_path / "stream.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(path)
            s.listen(1)
            assert HW._sock_ok(path) is True
        finally:
            s.close()

    def test_missing_path_is_false(self, HW, tmp_path):
        assert HW._sock_ok(str(tmp_path / "ghost.sock")) is False

    def test_non_socket_file_is_false(self, HW, tmp_path):
        f = tmp_path / "plain.txt"
        f.write_text("not a socket")
        assert HW._sock_ok(str(f)) is False

    def test_candidates_runtime_first(self, HW, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert HW._ydotool_candidates() == [
            str(tmp_path / ".ydotool_socket"), "/tmp/.ydotool_socket"]
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert HW._ydotool_candidates() == ["/tmp/.ydotool_socket"]

    def test_section_reports_resolved_path(self, HW, tmp_path, monkeypatch):
        s, path = _live_dgram(tmp_path, ".ydotool_socket")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        try:
            snap = HW.snapshot(
                _ctx(tmp_path),
                _ok_probers(ydotool_which=lambda: "/usr/bin/ydotool",
                            socket_connectable=HW._sock_ok),
                {})
        finally:
            s.close()
        ydo = snap["ydotool"]
        assert ydo == {"ok": True, "installed": True,
                       "socket": path, "reachable": True}

    def test_section_prefers_runtime_over_legacy(self, HW, tmp_path, monkeypatch):
        run, legacy = tmp_path / "run", tmp_path / "legacy"
        run.mkdir()
        legacy.mkdir()
        s_live, live = _live_dgram(run, ".ydotool_socket")
        s_leg, leg = _live_dgram(legacy, "legacy.sock")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
        monkeypatch.setattr(HW, "_LEGACY_YDOTOOL_SOCKET", leg)
        try:
            snap = HW.snapshot(
                _ctx(tmp_path),
                _ok_probers(ydotool_which=lambda: "/usr/bin/ydotool",
                            socket_connectable=HW._sock_ok),
                {})
        finally:
            s_live.close()
            s_leg.close()
        assert snap["ydotool"]["socket"] == live
