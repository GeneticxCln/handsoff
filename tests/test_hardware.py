"""Hardware snapshot tests: degraded dicts, mic matching, TTL, prompt cap.

No real hardware: every slow prober is injected (real local sockets only
for the ydotool connectability unit tests). Style mirrors
test_hardening.py (module loaded by path, tmp sandbox dirs)."""
from __future__ import annotations

import json
import socket
import types
from pathlib import Path

import pytest

from conftest import HERE as ROOT, _load as _load_module

HERE = ROOT   # the repo root


def _load_hw():
    # Through conftest's loader, so this in-process load resolves its user
    # directories in the same throw-away HOME as every other one. It is not a
    # hand-built spec: that is how the sandbox gets lost without a word.
    return _load_module("hardware_mod", HERE / "hardware.py")


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


def _weights(tmp_path, *sizes: int) -> Path:
    """A fake HF hub snapshot: snapshots/<rev>/*.safetensors, or incomplete."""
    root = tmp_path / "chatterbox-turbo"
    snap = root / "snapshots" / "deadbeef"
    snap.mkdir(parents=True, exist_ok=True)
    for i, size in enumerate(sizes):
        (snap / f"file{i}.safetensors").write_bytes(b"x" * size)
    return root


def _ctx(tmp_path, **over):
    tdir = _weights(tmp_path, 8)
    wdir = tmp_path / "whisper-model"
    wdir.mkdir(exist_ok=True)
    (wdir / "tiny.pt").write_text("fake")
    ctx = {
        "ollama_base": "http://127.0.0.1:9", "ollama_model": "qwen3:8b",
        "whisper_size": "tiny", "mic_device": "", "tts_engine": "chatterbox-turbo",
        "whisper_model_dir": str(wdir), "tts_weights_dir": str(tdir),
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
        assert "note" not in snap["audio"]

    def test_a_mic_name_matching_nothing_is_reported(self, HW, tmp_path):
        """The fallback to the first input is right (a renamed mic must not
        deafen the bubble), but it was SILENT: a misconfigured `mic_device`
        read as healthy with no hint the configured name matched nothing."""
        devs = [{"name": "Built-in Microphone", "max_input_channels": 1}]
        snap = HW.snapshot(_ctx(tmp_path, mic_device="blue yeti x"),
                           _ok_probers(query_devices=lambda: devs), {})
        audio = snap["audio"]
        assert audio["default"] == "Built-in Microphone"   # still falls back
        assert "blue yeti x" in audio["note"], audio
        assert audio["ok"] is True                          # healthy, not down

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
    def test_missing_weights_are_reported_not_assumed(self, HW, tmp_path):
        """The murmur that matters is 3.8 GB away: a bubble without these is
        mute, and "the directory exists" is not the question."""
        empty = tmp_path / "empty-weights"
        empty.mkdir()
        snap = HW.snapshot(_ctx(tmp_path, tts_weights_dir=str(empty)),
                           _ok_probers(), {})
        stt = snap["stt_tts"]
        assert stt["tts_cached"] is False and stt["ok"] is False
        assert "install.sh" in stt["note"], stt["note"]
        assert stt["tts_engine"] == "chatterbox-turbo"

    def test_a_half_downloaded_snapshot_is_not_cached(self, HW, tmp_path):
        """An interrupted fetch leaves snapshots/<rev>/ with no blobs — which
        a bare is_dir() reports as ready, skipping the download that the very
        next spoken turn needs."""
        root = tmp_path / "half"
        (root / "snapshots" / "deadbeef").mkdir(parents=True)
        snap = HW.snapshot(_ctx(tmp_path, tts_weights_dir=str(root)),
                           _ok_probers(), {})
        assert snap["stt_tts"]["tts_cached"] is False
        assert snap["stt_tts"]["ok"] is False
        # ...and an empty file is not weights either
        (root / "snapshots" / "deadbeef" / "s3gen.safetensors").write_bytes(b"")
        snap = HW.snapshot(_ctx(tmp_path, tts_weights_dir=str(root)),
                           _ok_probers(), {})
        assert snap["stt_tts"]["tts_cached"] is False
        (root / "snapshots" / "deadbeef" / "s3gen.safetensors").write_bytes(b"w")
        snap = HW.snapshot(_ctx(tmp_path, tts_weights_dir=str(root)),
                           _ok_probers(), {})
        assert snap["stt_tts"]["tts_cached"] is True
        assert snap["stt_tts"]["ok"] is True

    def test_ok_means_both_directions_not_just_the_voice(self, HW, tmp_path):
        """`ok` mirrored `tts_cached` alone, so a bubble with working speech and
        NO whisper model reported `ok: True` — a deaf assistant described as
        healthy. The prompt context reads both flags; the summary now does too."""
        no_whisper = tmp_path / "no-whisper"     # never created
        snap = HW.snapshot(
            _ctx(tmp_path, whisper_model_dir=str(no_whisper)), _ok_probers(), {})
        stt = snap["stt_tts"]
        assert stt["tts_cached"] is True and stt["whisper_cached"] is False
        assert stt["ok"] is False, "a missing whisper model must not read ok"
        assert "whisper" in stt["note"], stt["note"]

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

    def test_a_failure_is_not_cached_for_the_full_ttl(self, HW, tmp_path):
        """A failing probe was stamped as freshly probed, so ONE transient blip
        on a long-TTL section (ollama: 60 s) kept the bubble reporting "down"
        for a full minute after the daemon was back."""
        ollama_ttl = HW.TTL["ollama"]
        assert ollama_ttl > HW.FAILURE_TTL, "this test needs a long-TTL section"
        calls: list = []

        def flaky(base):
            calls.append(1)
            raise RuntimeError("connection refused")

        cache: dict = {}
        probers = _ok_probers(ollama_tags=flaky)
        HW.snapshot(_ctx(tmp_path), probers, cache)
        assert calls, "the failing prober never ran"
        # The stamp is backdated so the entry expires after FAILURE_TTL, not
        # after the section's TTL: still "fresh" (so the failure is reported
        # rather than re-probed on every tick), but for 5 s instead of 60.
        stale = cache["at"]["ollama"]
        assert HW.time.monotonic() - stale < ollama_ttl
        assert HW.time.monotonic() - stale >= ollama_ttl - HW.FAILURE_TTL - 1
        # ...and the failure was reported THROUGH the cache this time
        snap = HW.snapshot(_ctx(tmp_path), probers, cache)
        assert snap["ollama"]["ok"] is False
        # The backdated stamp expires FAILURE_TTL after the probe, i.e. once
        # `now - stamp` reaches the section TTL. Rewind to exactly that point
        # and the recovered daemon is seen — under the old stamp, which started
        # at `now`, this moment would still be 55 s away.
        cache["at"]["ollama"] = HW.time.monotonic() - ollama_ttl
        healthy = _ok_probers()
        snap = HW.snapshot(_ctx(tmp_path), healthy, cache)
        assert snap["ollama"]["ok"] is True, "recovery was still hidden"

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
        # The voice line names the ENGINE and whether its weights are present.
        # It used to list the .onnx files; with one built-in voice the useful
        # fact is not which voice but whether the 3.8 GB are there at all.
        assert "chatterbox-turbo" in text and "cached" in text

    def test_a_mute_bubble_is_visible_in_the_prompt_context(self, HW, tmp_path):
        """The AI reads this line when the user says "you're not talking" —
        it must not claim a cached voice when the weights are absent."""
        empty = tmp_path / "nothing"
        empty.mkdir()
        snap = HW.snapshot(_ctx(tmp_path, tts_weights_dir=str(empty)),
                           _ok_probers(), {})
        text = HW.prompt_context(snap)
        assert "NOT downloaded" in text, text


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

    def test_empty_output_is_degraded_not_a_silent_success(self, HW, tmp_path):
        """`fastfetch -j` that failed is not "a machine with no facts".

        The exit status was ignored and `.stdout` returned whatever happened, so
        an empty result parsed as an empty machine and the section reported
        ok: True with every field blank — which is worse than a missing binary,
        because a missing binary says so and this said nothing.
        """
        snap = HW.snapshot(_ctx(tmp_path),
                           self._probers(fastfetch_json=lambda: ""), {})
        ff = snap["fastfetch"]
        assert ff["ok"] is False and "no output" in ff["error"], ff

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


class TestTtlCacheIsSharedSafely:
    """The TTL cache is read and written by two threads at once.

    doctor runs on the control server's worker thread while the Qt thread's
    hardware tick peeks the same dict, so the cache check and store have to
    happen under one lock. Nothing in hardware.py was synchronised before.
    """

    def test_cache_access_is_locked(self, HW):
        import threading
        cache: dict = {}
        entered, finished = threading.Event(), threading.Event()

        def _worker():
            entered.set()
            HW._probe("gpu", lambda: {"ok": True}, cache, False)
            finished.set()

        HW._TTL_LOCK.acquire()
        try:
            t = threading.Thread(target=_worker)
            t.start()
            assert entered.wait(2.0)
            assert not finished.wait(0.25), "_probe touched the cache without the lock"
        finally:
            HW._TTL_LOCK.release()
        t.join(2.0)
        assert finished.is_set()
        assert cache["data"]["gpu"] == {"ok": True}

    def test_cache_still_short_circuits_within_ttl(self, HW):
        calls: list = []
        cache: dict = {}
        probe = lambda: (calls.append(1), {"ok": True})[1]   # noqa: E731
        first, cached = HW._probe("gpu", probe, cache, False)
        again, cached2 = HW._probe("gpu", probe, cache, False)
        assert first == again == {"ok": True}
        assert cached is False and cached2 is True
        assert len(calls) == 1, "the prober ran twice inside the TTL"

    def test_force_bypasses_the_cache(self, HW):
        calls: list = []
        cache: dict = {}
        probe = lambda: (calls.append(1), {"ok": True})[1]   # noqa: E731
        HW._probe("gpu", probe, cache, False)
        HW._probe("gpu", probe, cache, True)
        assert len(calls) == 2

    def test_degraded_previous_result_is_carried_forward(self, HW):
        cache: dict = {}
        HW._probe("gpu", lambda: {"ok": True, "name": "gpu0"}, cache, False)
        def _boom():
            raise RuntimeError("nvidia-smi wedged")
        out, cached = HW._probe("gpu", _boom, cache, True)
        assert out["ok"] is True and out["degraded"] is True
        assert "nvidia-smi wedged" in out["error"]


class TestModelCacheProbe:
    """The two `iterdir` reads that could take the whole section down.

    `is_dir()` is its own stat, so it answers a different question than "may I
    enumerate this": a cache directory that exists and cannot be READ raised
    straight out of the probe, and the whisper one was outside the guard that
    the TTS side had.
    """

    def test_an_unreadable_snapshots_directory_is_not_cached(self, HW, tmp_path,
                                                             monkeypatch):
        root = tmp_path / "hub"
        rev = root / "snapshots" / "rev"
        rev.mkdir(parents=True)
        (rev / "blob.bin").write_bytes(b"x")
        actual = Path.iterdir

        def deny(self):
            if self.name == "snapshots":
                raise PermissionError("denied")
            return actual(self)

        monkeypatch.setattr(Path, "iterdir", deny)
        assert HW._snapshot_cached(root) is False

    def test_an_unreadable_whisper_directory_degrades_the_section(
            self, HW, tmp_path, monkeypatch):
        whisper = tmp_path / "whisper"
        whisper.mkdir()
        actual = Path.iterdir

        def deny(self):
            if self.name == "whisper":
                raise PermissionError("denied")
            return actual(self)

        monkeypatch.setattr(Path, "iterdir", deny)
        out = HW._stt_tts({"tts_weights_dir": str(tmp_path / "none"),
                           "whisper_model_dir": str(whisper),
                           "whisper_size": "small"})
        assert out["ok"] is False and out.get("error"), out

    def test_a_populated_snapshot_reads_as_cached(self, HW, tmp_path):
        root = tmp_path / "hub"
        rev = root / "snapshots" / "rev"
        rev.mkdir(parents=True)
        (rev / "blob.bin").write_bytes(b"x")
        assert HW._snapshot_cached(root) is True
        assert HW._snapshot_cached(tmp_path / "absent") is False
