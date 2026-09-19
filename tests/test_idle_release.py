"""Giving the models' memory back when the bubble has been quiet.

Why this file exists. An audit of a full 16 GB card measured 15.2 GB in use with
~1 GB free, and at that point nvidia-drm stopped being able to allocate DISPLAY
buffers (`Failed to allocate NVKMS memory for GEM object`) — the desktop
glitched, and libinput reported 20-30 ms of input lag. ~3 GB of that card was
this process holding the speech model, and 9.7 GB was an Ollama model the bubble
had asked to keep for an HOUR after the last turn. So the release is about the
machine, not about the bubble's own speed — which is why the tests below are
mostly about when it must NOT fire (mid-turn, mid-recording, mid-speech) and
about the cheap thing being cheap (one request per quiet spell, not one a tick).

The two halves are not the same bargain, and the tests say so. Handing back the
speech model costs a few seconds to undo, so it always goes. Handing back the
LLM can cost MINUTES — measured on that same machine, an 18 GB model Ollama had
split across CPU and GPU took 218.9 s to come back while holding 9.7 GB of the
card — so it is only released when the memory is worth the wait, and both the
journal and the doctor say which way it went and why.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import time

import pytest

from conftest import HERE as ROOT, core_module


@pytest.fixture()
def audio():
    return core_module("audio")


@pytest.fixture()
def brain():
    return core_module("brain")


@pytest.fixture()
def idle(H, monkeypatch):
    """An app whose window is 10 minutes and whose clock the test can rewind.

    The VRAM pressure floor is pinned to 0 here, so the suite's baseline is a
    card with room on it: without that, whether a test's release fires early
    would depend on how full the DEVELOPER's GPU happens to be — which is the
    opposite of a test. Tests that want pressure set the floor and the reading
    explicitly (`_pressure`), and the shipped defaults are pinned by
    `test_the_shipped_defaults_are_the_pressure_and_the_short_window`.
    """
    monkeypatch.setattr(H, "SETTINGS",
                        {**H.DEFAULT_SETTINGS, "idle_release_seconds": 600,
                         "vram_pressure_floor_mb": 0})
    monkeypatch.setattr(H, "_gpu_last_use", H._tick_now())
    monkeypatch.setattr(H, "_gpu_released", False)
    monkeypatch.setattr(H, "_audio", H._audio, raising=False)
    # Both are module state the tick writes; a fresh dict per test keeps one
    # test's measurement out of the next one's verdict.
    monkeypatch.setattr(H, "_llm_load",
                        {"model": "", "seconds": None, "at": 0.0})
    monkeypatch.setattr(H, "_llm_reload",
                        {"pending": False, "started": 0.0})
    # The cached free-VRAM reading is module state the tick refreshes; a fresh
    # one per test keeps one test's card out of the next one's decision.
    monkeypatch.setattr(H, "_vram_sample", {"at": 0.0, "free_mb": None})
    # The LLM's size, once read, is cached for ten minutes on the turn path; a
    # fresh record per test keeps one test's model out of the next one's ask.
    # `resident`/`resident_at` are the same record's other half — what Ollama
    # reported loaded, which the descriptive surfaces read.
    # `resident_at` is set FRESH with `resident` None: the descriptive surfaces
    # take a cached residency, so pinning it here is what keeps a test's story
    # off the developer's live Ollama. A test that wants a residency sets one
    # (through `_server`), which is also how the turn path fills the cache.
    monkeypatch.setattr(H, "_llm_footprint",
                        {"model": "", "mb": None, "need_mb": None, "at": 0.0,
                         "resident": None, "resident_at": time.time(),
                         "resident_model": H.OLLAMA_MODEL})
    return H


def _story_line(H, label: str) -> str:
    """The card section's line with this indented label, or a loud failure.

    The section is the unit: a test that asserted on a LINE INDEX would pass
    while the fact it cares about moved to another line, which is exactly the
    confusion one story is supposed to end.
    """
    lines = H._vram_story_lines()
    for line in lines:
        if line.strip().startswith(label + ":"):
            return line
    raise AssertionError(f"no {label!r} line in {lines}")


def _story_text(H) -> str:
    return "\n".join(H._vram_story_lines())


def _server(H, monkeypatch, *, size_gb=None, vram_gb=None, model=None,
            tags_gb=None):
    """Ollama as the idle release sees it: `/api/ps` to ask, `/api/generate` to drop.

    `size_gb=None` means nothing is resident, which is a real measurement and
    must stay distinct from an endpoint that does not answer at all
    (`_no_answer` below) — they lead to opposite decisions.

    `tags_gb` is `/api/tags`, the catalogue a turn reads to price the model it is
    about to load (the model that is NOT resident has no `/api/ps` entry yet).
    Left at None it answers an empty catalogue, which the reader treats as an
    unknown size rather than as a model of zero.
    """
    body = json.dumps({"models": [] if size_gb is None else [{
        "name": model or H.OLLAMA_MODEL,
        "model": model or H.OLLAMA_MODEL,
        "size": int(size_gb * 1024 ** 3),
        "size_vram": int((vram_gb or 0.0) * 1024 ** 3),
        "expires_at": "2026-09-16T21:00:00Z"}]}).encode("utf-8")
    tags = json.dumps({"models": [] if tags_gb is None else [{
        "name": model or H.OLLAMA_MODEL,
        "model": model or H.OLLAMA_MODEL,
        "size": int(tags_gb * 1024 ** 3)}]}).encode("utf-8")

    class _Server:
        def __init__(self):
            self.asks = []

        def __call__(self, req, timeout=None):
            self.asks.append(req.full_url)
            if req.full_url.endswith("/api/ps"):
                return _Reply(body)
            if req.full_url.endswith("/api/tags"):
                return _Reply(tags)
            return _Reply()

        def urls(self, suffix):
            return [u for u in self.asks if u.endswith(suffix)]

    srv = _Server()
    monkeypatch.setattr(H.urllib.request, "urlopen", srv)
    return srv


def _no_answer(H, monkeypatch):
    """A model server that is not there: every request raises."""
    asks = []

    def urlopen(req, timeout=None):
        asks.append(req.full_url)
        raise OSError("connection refused")

    monkeypatch.setattr(H.urllib.request, "urlopen", urlopen)
    return asks


def _stub_brain(H, monkeypatch, **calls):
    """`core.brain` as the host's wrappers see it: no HTTP, calls recorded."""
    seen = []

    def rec(name):
        def call(*args, **kwargs):
            seen.append(name)
            return {}
        return staticmethod(call)

    namespace = {name: rec(name) for name in calls}
    monkeypatch.setattr(H, "_brain_deps", lambda: {})
    monkeypatch.setattr(H, "_brain", type("B", (), namespace))
    return seen


def _assistant(H, *, state="idle", queued=0, recording=False):
    """A bare Assistant: `_idle_release_tick` reads state, the queue and locks."""
    a = H.Assistant.__new__(H.Assistant)
    a._state = state
    a._pipeline_q = queue.Queue()
    for item in range(queued):
        a._pipeline_q.put(item)
    if recording:
        a._recorder = object()
    return a


class _Reply:
    def __init__(self, body=b'{"done": true}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Unloads:
    """Records what Ollama was asked to do."""

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append({"url": req.full_url,
                              "body": json.loads(req.data.decode("utf-8"))})
        return _Reply()

    def bodies(self):
        return [r["body"] for r in self.requests]


def _loaded(H, monkeypatch, audio):
    """Both copies of the TTS cache, as a real load leaves them."""
    model = object()
    monkeypatch.setattr(H, "_tts_model", model)
    monkeypatch.setattr(audio, "_tts_model", model)
    return model


# -- the drop itself ---------------------------------------------------------

class TestDropModels:
    def test_it_drops_the_model_and_clears_the_device(self, audio, monkeypatch):
        model = object()
        monkeypatch.setattr(audio, "_tts_model", model)
        monkeypatch.setattr(audio, "_tts_device", "cuda")

        dropped = audio.drop_models()

        assert dropped["tts"] is True
        assert audio._tts_model is None
        assert audio._tts_device == "", (
            "the next load must re-decide cuda-vs-cpu against the free memory "
            "of THAT moment — which is the point of having released anything")

    def test_a_busy_generation_means_not_now_and_keeps_the_model(self, audio, monkeypatch):
        model = object()
        monkeypatch.setattr(audio, "_tts_model", model)

        with audio._TTS_RUN_LOCK:
            dropped = audio.drop_models()

        assert dropped["tts"] is False
        assert audio._tts_model is model, "a model in use must never be dropped"
        assert audio.drop_models()["tts"] is True, "...and must drop once it is free"

    def test_cpu_whisper_is_left_alone_and_cuda_whisper_is_dropped(self, audio, monkeypatch):
        monkeypatch.setattr(audio, "_whisper_model", object())
        monkeypatch.setattr(audio, "_whisper_device_used", "cpu")

        assert audio.drop_models()["whisper"] is False, (
            "a cpu whisper holds no GPU memory; dropping it only costs the "
            "next turn a reload")

        monkeypatch.setattr(audio, "_whisper_device_used", "cuda")
        assert audio.drop_models()["whisper"] is True
        assert audio._whisper_model is None

    def test_it_never_imports_torch(self, audio, monkeypatch):
        monkeypatch.setattr(audio, "_tts_model", object())
        monkeypatch.delitem(sys.modules, "torch", raising=False)

        audio.drop_models()

        assert "torch" not in sys.modules, (
            "an idle tick must not drag torch in to hand back memory")

    def test_it_empties_the_cuda_cache_when_torch_is_already_there(self, audio, monkeypatch):
        calls = []

        class _Cuda:
            @staticmethod
            def empty_cache():
                calls.append(1)

        monkeypatch.setitem(sys.modules, "torch", type("T", (), {"cuda": _Cuda}))
        monkeypatch.setattr(audio, "_tts_model", object())

        dropped = audio.drop_models()

        assert calls == [1]
        assert dropped["cache_cleared"] is True

    def test_nothing_loaded_is_not_an_error(self, audio):
        assert audio.drop_models() == {"tts": False, "whisper": False,
                                       "cache_cleared": False}


# -- when the host releases --------------------------------------------------

class TestIdleRelease:
    def test_it_releases_after_the_window_and_unloads_the_llm(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle)._idle_release_tick()

        assert idle._tts_model is None and audio._tts_model is None, (
            "both copies must go, or the mirror would say a model is loaded")
        assert len(unloads.requests) == 1, unloads.requests
        assert unloads.requests[0]["url"].endswith("/api/generate")
        assert unloads.requests[0]["body"]["keep_alive"] == 0, (
            "the release is the same knob every turn sets the other way")
        assert unloads.requests[0]["body"]["model"] == idle.OLLAMA_MODEL

    def test_it_waits_for_the_window(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 100)

        _assistant(idle)._idle_release_tick()

        assert idle._tts_model is not None
        assert unloads.requests == []

    def test_zero_turns_it_off(self, idle, monkeypatch, audio):
        monkeypatch.setitem(idle.SETTINGS, "idle_release_seconds", 0)
        _loaded(idle, monkeypatch, audio)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 10 ** 6)

        _assistant(idle)._idle_release_tick()

        assert idle._tts_model is not None and unloads.requests == []

    def test_junk_means_off_rather_than_a_crash(self, idle, monkeypatch, audio):
        monkeypatch.setitem(idle.SETTINGS, "idle_release_seconds", "whenever")
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 10 ** 6)

        _assistant(idle)._idle_release_tick()          # must not raise

        assert idle._tts_model is not None

    @pytest.mark.parametrize("state", ["listening", "thinking", "speaking"])
    def test_a_bubble_that_is_doing_something_keeps_its_models(self, idle, monkeypatch,
                                                              audio, state):
        _loaded(idle, monkeypatch, audio)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle, state=state)._idle_release_tick()

        assert idle._tts_model is not None and unloads.requests == []

    def test_a_queued_turn_keeps_them(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle, queued=1)._idle_release_tick()

        assert idle._tts_model is not None

    def test_a_recording_keeps_them(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle, recording=True)._idle_release_tick()

        assert idle._tts_model is not None

    def test_speech_in_flight_keeps_them(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        with idle._ANNOUNCE_LOCK:
            _assistant(idle)._idle_release_tick()

        assert idle._tts_model is not None, (
            "the lock is held around playback; releasing under it would drop "
            "the model a sentence is being synthesized with")

    def test_one_release_per_quiet_spell(self, idle, monkeypatch, audio):
        """The tick fires every second; the request must not.

        The clock is rewound past the window before every tick, so this is five
        ticks that ALL satisfy "the window elapsed" — the only thing that can
        hold it to one is the release marking itself done. Without that, a
        bubble sitting idle overnight asks Ollama to unload once per tick, to
        say nothing changed (and each ask is a real HTTP round trip).
        """
        _loaded(idle, monkeypatch, audio)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        a = _assistant(idle)

        for _ in range(5):
            monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
            a._idle_release_tick()

        assert len(unloads.requests) == 1, unloads.bodies()

    def test_a_use_re_arms_the_window(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
        a = _assistant(idle)
        a._idle_release_tick()
        assert len(unloads.requests) == 1

        idle._touch_gpu()                                # a turn happened
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
        a._idle_release_tick()

        assert len(unloads.requests) == 2, (
            "after a release, the next quiet spell must release again — the "
            "bubble does not get one release per process")

    def test_the_getters_are_what_counts_as_use(self, idle, monkeypatch, audio):
        """Every speech and chat path goes through a getter, and they touch."""
        monkeypatch.setattr(idle, "_gpu_last_use", 0.0)
        monkeypatch.setattr(idle, "_gpu_released", True)
        monkeypatch.setattr(idle, "_push_model", lambda attr: None)
        monkeypatch.setattr(audio, "get_tts", lambda: object())
        monkeypatch.setattr(audio, "get_whisper", lambda: object())
        monkeypatch.setattr(audio, "transcribe", lambda *a, **k: "text")

        idle.get_tts()
        assert idle._gpu_released is False and idle._gpu_last_use > 0.0
        monkeypatch.setattr(idle, "_gpu_last_use", 0.0)
        monkeypatch.setattr(idle, "_gpu_released", True)
        idle.get_whisper()
        assert idle._gpu_released is False and idle._gpu_last_use > 0.0

    def test_the_next_use_loads_again_and_the_two_copies_agree(self, idle, monkeypatch, audio):
        """The resurrection rule: a reload after a drop must publish both.

        The stub has to leave the module cache holding what it returns, exactly
        as a real load does: `_adopt_model` publishes only when core.audio
        still holds the model it was handed, and that identity check IS the
        rule under test. Stubbing the getter alone would test the stub — and a
        getter that returns without loading is precisely the shape that pulls a
        real 3 GB model into this process (see the file docstring).
        """
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
        _assistant(idle)._idle_release_tick()
        assert idle._tts_model is None and audio._tts_model is None

        fresh = object()

        def load_it_again():
            audio._tts_model = fresh            # what a real load leaves behind
            return fresh

        monkeypatch.setattr(audio, "get_tts", load_it_again)

        assert idle.get_tts() is fresh
        assert idle._tts_model is fresh, (
            "a load after a release must republish the mirror — the settings "
            "reload path dropped one side only and the stale model came back")

    def test_a_chat_turn_re_arms_it_too_not_only_speech(self, idle, monkeypatch):
        """The LLM is the other half of what the release gives back.

        A turn that answers without speaking is still a turn that asked Ollama
        for a long keep-alive, so the chat wrappers must count as use exactly
        as the speech getters do — otherwise the one thing that re-armed the
        window last was a sentence, and a silent conversation leaves the model
        resident until the next spoken one.
        """
        monkeypatch.setattr(idle, "_brain_deps", lambda: {})
        brain = type("B", (), {"ollama_chat": staticmethod(lambda *a, **k: {}),
                              "ollama_chat_stream": staticmethod(lambda *a, **k: {})})
        monkeypatch.setattr(idle, "_brain", brain)

        for call in (lambda: idle.ollama_chat([]),
                     lambda: idle.ollama_chat_stream([], queue.Queue())):
            monkeypatch.setattr(idle, "_gpu_last_use", 0.0)
            monkeypatch.setattr(idle, "_gpu_released", True)
            call()
            assert idle._gpu_released is False and idle._gpu_last_use > 0.0, call

    def test_a_bundle_without_the_brain_reader_still_releases_the_speech(self, idle, monkeypatch, audio):
        """The no-core/brain bundle has no `ollama_unload`; that is not a crash.

        `unload_ollama` is reached from a timer, so a missing caller must
        return False (the model stays resident, the bubble keeps working) —
        not raise an AttributeError inside the health tick.
        """
        monkeypatch.setattr(idle, "_brain", type("B", (), {})())

        assert idle.unload_ollama() is False

    def test_settings_reload_and_idle_release_agree_on_the_copies(self, idle, monkeypatch, audio):
        """Both drop paths clear the same two places."""
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)

        _assistant(idle)._idle_release_tick()

        assert idle._tts_model is None
        assert audio._tts_model is None
        assert audio._tts_device == ""


# -- the rule itself: pure, so every branch is a test -------------------------

class TestResidentProbe:
    """`core.brain.ollama_resident` — the split, and the difference between
    "nothing is loaded" and "nobody answered".

    A model bigger than the card is served partly from system memory, and only
    `/api/ps` says how much of it is really on the GPU. That number is what the
    release weighs, so a probe that quietly reported a unit-shifted value would
    decide the whole question with a parsing accident.
    """

    def _ask(self, brain, body, model="qwen3:8b"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        asks = []

        def urlopen(req, timeout=None):
            asks.append(req.full_url)
            return _Reply(body)

        return brain.ollama_resident(
            base="http://127.0.0.1:11434", model=model, guard=lambda: None,
            logger=logging.getLogger("handsoff.brain"), urlopen=urlopen), asks

    def test_it_reports_the_resident_split_in_gib(self, brain):
        out, asks = self._ask(brain, {"models": [{
            "name": "qwen3:8b", "model": "qwen3:8b",
            "size": int(5.2 * 1024 ** 3), "size_vram": int(5.1 * 1024 ** 3),
            "expires_at": "2026-09-16T21:00:00Z"}]})

        assert out["loaded"] is True
        assert out["size"] == pytest.approx(5.2, abs=0.01)
        assert out["size_vram"] == pytest.approx(5.1, abs=0.01)
        assert asks == ["http://127.0.0.1:11434/api/ps"]

    def test_it_matches_either_name_field(self, brain):
        out, _ = self._ask(brain, {"models": [{"name": "",
                                                "model": "qwen3:8b",
                                                "size": int(5.2 * 1024 ** 3),
                                                "size_vram": int(5.2 * 1024 ** 3)}]})
        assert out["loaded"] is True

    def test_an_empty_server_is_a_measurement_not_an_unknown(self, brain):
        out, _ = self._ask(brain, {"models": []})
        assert out == {"loaded": False, "size": None, "size_vram": None,
                       "expires_at": ""}, (
            "'nothing is loaded' and 'nobody answered' lead to different "
            "decisions, so they cannot both be None")

    def test_a_garbage_answer_is_an_unknown(self, brain):
        for body in (b"not json", b"[]", {"models": "no"}, {"nope": 1}):
            out, _ = self._ask(brain, body)
            assert out is None, body

    def test_a_unit_shifted_size_is_refused_rather_than_believed(self, brain):
        out, _ = self._ask(brain, {"models": [{
            "name": "qwen3:8b", "model": "qwen3:8b",
            "size": 5200, "size_vram": 5200}]})   # megabytes, not bytes
        assert out["size_vram"] is None, (
            "5200 bytes is not a model; believing it would price 0.000005 GB "
            "and skip a release for a parsing reason")

    def test_a_reported_zero_stays_zero(self, brain):
        out, _ = self._ask(brain, {"models": [{
            "name": "qwen3:8b", "model": "qwen3:8b",
            "size": int(5.2 * 1024 ** 3), "size_vram": 0}]})
        assert out["size_vram"] == 0.0

    def test_an_unreachable_server_is_an_unknown(self, brain):
        def urlopen(req, timeout=None):
            raise OSError("connection refused")

        out = brain.ollama_resident(base="http://127.0.0.1:11434",
                                    model="qwen3:8b", guard=lambda: None,
                                    logger=logging.getLogger("handsoff.brain"),
                                    urlopen=urlopen)
        assert out is None


class TestModelFootprint:
    """`core.brain.ollama_model_size_mb` — the claim side of the mirror.

    `/api/ps` can only price a model that is LOADED, and this is asked before a
    turn loads one: the catalogue's blob size is what a full offload of that
    model costs the card. The number decides whether the speech model is asked
    to move, so an unreadable answer has to be None ("nothing was weighed")
    rather than zero ("fits anywhere"), and the unit is guarded the same way
    `ollama_resident` guards its split.
    """

    def _ask(self, brain, body, model="qwen3:8b"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        asks = []

        def urlopen(req, timeout=None):
            asks.append(req.full_url)
            return _Reply(body)

        return brain.ollama_model_size_mb(
            base="http://127.0.0.1:11434", model=model, guard=lambda: None,
            logger=logging.getLogger("handsoff.brain"), urlopen=urlopen), asks

    def test_it_reports_the_blob_size_in_mib(self, brain):
        mb, asks = self._ask(brain, {"models": [{
            "name": "qwen3:8b", "model": "qwen3:8b",
            "size": int(5.0 * 1024 ** 3)}]})

        assert mb == 5_120, f"5 GiB of model is 5120 MiB, not {mb}"
        assert asks == ["http://127.0.0.1:11434/api/tags"]

    def test_it_matches_either_name_field(self, brain):
        mb, _ = self._ask(brain, {"models": [{"name": "",
                                               "model": "qwen3:8b",
                                               "size": int(5.0 * 1024 ** 3)}]})
        assert mb == 5_120

    def test_a_size_rounds_up_never_down(self, brain):
        """Under-claiming the card is how a voice is evicted for nothing."""
        mb, _ = self._ask(brain, {"models": [{
            "name": "qwen3:8b", "model": "qwen3:8b",
            "size": 5_000 * 1024 ** 2 + 1}]})
        assert mb == 5_001

    def test_a_model_that_is_not_there_is_an_unknown(self, brain):
        assert self._ask(brain, {"models": []})[0] is None
        assert self._ask(brain, {"models": [{
            "name": "other:7b", "model": "other:7b",
            "size": int(5.0 * 1024 ** 3)}]})[0] is None, (
            "another model's size is not this model's footprint")

    def test_a_unit_shifted_size_is_refused_rather_than_believed(self, brain):
        mb, _ = self._ask(brain, {"models": [{
            "name": "qwen3:8b", "model": "qwen3:8b", "size": 5200}]})
        assert mb is None, (
            "5200 bytes is not a model; believing it would price 0 MB and ask "
            "the speech model for the card on every turn")

    def test_a_garbage_answer_is_an_unknown(self, brain):
        for body in (b"not json", b"[]", {"models": "no"}, {"nope": 1},
                     {"models": [{"name": "qwen3:8b", "size": None}]}):
            assert self._ask(brain, body)[0] is None, body

    def test_an_unreachable_server_is_an_unknown(self, brain):
        def urlopen(req, timeout=None):
            raise OSError("connection refused")

        assert brain.ollama_model_size_mb(
            base="http://127.0.0.1:11434", model="qwen3:8b",
            guard=lambda: None, logger=logging.getLogger("handsoff.brain"),
            urlopen=urlopen) is None


class TestReleaseVerdict:
    """`core.brain.ollama_release_verdict` — the cost/benefit rule.

    The numbers are the ones the machine this was written for produced: an
    18 GB model Ollama had split 53/47 across CPU and GPU held 9.7 GB of a
    16 GB card and took 218.9 s to reload (22.6 s/GB — kept), while an 8 GB
    model that fits took ~41 s for 5.2 GB (7.9 s/GB — released).
    """

    SPLIT = {"loaded": True, "size": 18.0, "size_vram": 9.68}
    FITS = {"loaded": True, "size": 5.2, "size_vram": 5.2}

    def _v(self, brain, resident, *, reload_s, budget=20.0, model="m:1"):
        return brain.ollama_release_verdict(resident, reload_s=reload_s,
                                            wait_s_per_gb=budget, model=model)

    def test_a_split_model_is_kept_and_the_reason_names_its_numbers(self, brain):
        verdict = self._v(brain, self.SPLIT, reload_s=218.9, model="qwen3.8:27b")

        assert verdict["release"] is False
        note = verdict["note"]
        assert "9.7 GB" in note and "219 s" in note and "22.6 s/GB > 20" in note
        assert "llm_release_wait_s_per_gb" in note, (
            "a skip that does not name the lever is a decision the user cannot "
            "argue with")
        assert verdict["wait_per_gb"] == pytest.approx(22.62, abs=0.01)
        assert verdict["model"] == "qwen3.8:27b"

    def test_a_model_that_fits_is_released(self, brain):
        verdict = self._v(brain, self.FITS, reload_s=41.0, model="qwen3:8b")
        assert verdict["release"] is True
        assert "5.2 GB" in verdict["note"] and "7.9 s/GB ≤ 20" in verdict["note"]

    def test_the_budget_is_a_ceiling_so_exactly_it_releases(self, brain):
        verdict = self._v(brain, self.FITS, reload_s=104.0, budget=20.0)
        assert verdict["release"] is True

    def test_a_zero_budget_never_weighs_the_cost(self, brain):
        verdict = self._v(brain, self.SPLIT, reload_s=218.9, budget=0)
        assert verdict["release"] is True
        assert "not weighed" in verdict["note"]

    def test_a_budget_nobody_can_read_is_not_a_reason_to_hold_memory(self, brain):
        verdict = self._v(brain, self.SPLIT, reload_s=218.9, budget="often")
        assert verdict["release"] is True

    def test_an_unreadable_endpoint_releases_as_before(self, brain):
        verdict = self._v(brain, None, reload_s=218.9)
        assert verdict["release"] is True
        assert "unknown" in verdict["note"]

    def test_nothing_resident_is_not_a_request_worth_sending(self, brain):
        verdict = self._v(brain, {"loaded": False}, reload_s=218.9)
        assert verdict["release"] is False
        assert "nothing resident" in verdict["note"]

    def test_a_cpu_only_model_frees_no_gpu_memory(self, brain):
        verdict = self._v(brain, {"loaded": True, "size": 18.0,
                                  "size_vram": 0.0}, reload_s=218.9)
        assert verdict["release"] is False
        assert "CPU only" in verdict["note"]

    def test_an_unreadable_split_is_not_called_cpu_only(self, brain):
        verdict = self._v(brain, {"loaded": True, "size": 18.0,
                                  "size_vram": None}, reload_s=218.9)
        assert verdict["release"] is False
        assert "size_vram" in verdict["note"]

    def test_an_unmeasured_reload_cannot_weigh_anything(self, brain):
        verdict = self._v(brain, self.SPLIT, reload_s=None)
        assert verdict["release"] is True
        assert "not measured" in verdict["note"]

    def test_a_reload_of_zero_seconds_is_free(self, brain):
        assert self._v(brain, self.SPLIT, reload_s=0) ["release"] is True


# -- the host's half: measuring the reload, and acting on the verdict ---------

class TestModelAwareRelease:
    def test_a_split_model_is_kept_and_the_tick_says_why(self, idle, monkeypatch,
                                                        audio, caplog):
        _loaded(idle, monkeypatch, audio)
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        srv = _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        with caplog.at_level(logging.INFO, logger="handsoff"):
            _assistant(idle)._idle_release_tick()

        assert srv.urls("/api/generate") == [], (
            "the LLM half must not go back when the ledger says the wait is "
            "worse than the memory")
        assert srv.urls("/api/ps"), "the decision has to rest on a measurement"
        assert idle._tts_model is None and audio._tts_model is None, (
            "the speech model reloads in seconds and always goes back")
        line = " ".join(r.getMessage() for r in caplog.records)
        assert "llm kept" in line and "22.6 s/GB > 20" in line, line

    def test_a_model_that_fits_is_unloaded(self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        srv = _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle)._idle_release_tick()

        assert len(srv.urls("/api/generate")) == 1

    def test_an_unmeasured_reload_still_releases(self, idle, monkeypatch, audio):
        """No measurement, no argument: the machine comes first, as before."""
        _loaded(idle, monkeypatch, audio)
        srv = _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle)._idle_release_tick()

        assert len(srv.urls("/api/generate")) == 1

    def test_an_ollama_that_does_not_answer_still_releases(self, idle, monkeypatch,
                                                          audio):
        _loaded(idle, monkeypatch, audio)
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        asks = _no_answer(idle, monkeypatch)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle)._idle_release_tick()

        assert any(u.endswith("/api/generate") for u in asks), (
            "an unreachable model server is not evidence that the model is "
            "worth keeping")

    def test_a_kept_model_is_not_re_asked_every_tick_and_is_reopened_next_spell(
            self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        srv = _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)
        a = _assistant(idle)

        for _ in range(5):
            monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
            a._idle_release_tick()

        assert len(srv.urls("/api/ps")) == 1, (
            "a keep is a decision for the quiet spell, not a probe per second")

        idle._touch_gpu()                                # the user asks again
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
        a._idle_release_tick()

        assert len(srv.urls("/api/ps")) == 2, (
            "the next quiet spell must be free to answer differently")

    def test_a_real_release_arms_the_measurement_of_its_own_reload(self, idle,
                                                                  monkeypatch, audio):
        """Nothing else re-measures: if the release does not arm the probe, the
        number the next decision rests on is only ever the startup warm."""
        _loaded(idle, monkeypatch, audio)
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)

        assert idle._release_llm()["unloaded"] is True
        assert idle._llm_reload["pending"] is True

        idle._llm_reload["pending"] = False
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)   # ...now it is a big one
        _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)

        assert idle._release_llm()["release"] is False
        assert idle._llm_reload["pending"] is False, (
            "a kept model is not reloaded, so there is nothing to measure")

    def test_junk_in_the_budget_releases_rather_than_holds(self, idle, monkeypatch,
                                                          audio):
        monkeypatch.setitem(idle.SETTINGS, "llm_release_wait_s_per_gb", "lots")
        _loaded(idle, monkeypatch, audio)
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        srv = _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)

        _assistant(idle)._idle_release_tick()

        assert idle._llm_release_wait_s_per_gb() == 0.0
        assert len(srv.urls("/api/generate")) == 1

    def test_a_bundle_without_the_policy_releases_as_before(self, idle, monkeypatch,
                                                           audio):
        """No `ollama_release_verdict` in core/brain means no weighing.

        That bundle has no unload caller either, so the tick cannot actually
        drop the model — what it must do is still DECIDE to, and to say so
        rather than quietly holding memory it cannot even measure.
        """
        _loaded(idle, monkeypatch, audio)
        monkeypatch.setattr(idle, "_brain", type("B", (), {})())

        verdict = idle._release_llm()

        assert verdict["release"] is True
        assert verdict["unloaded"] is False
        assert "no release policy" in verdict["note"]

        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 601)
        _assistant(idle)._idle_release_tick()          # must not raise
        assert idle._tts_model is None


class TestTheSpeechModelAsksTheLlmForTheCard:
    """The host half of the reclaim: who is asked, and which tenant moved.

    `core/audio._ask_for_the_card` calls `_reclaim_gpu_for_speech` when a
    speech load was refused for want of room. The policy is the SAME verdict
    the idle release weighs, because it answers the same question — is this
    memory worth the reload it costs? — so a measured 30 s/GB keeps the model
    and the speech model is the one that gives way.
    """

    def _pool(self, H, monkeypatch, *readings):
        """nvidia-smi's (free, total) as the reclaim sees it, one per read."""
        rest = list(readings)
        monkeypatch.setattr(
            H, "_vram_pool_mb",
            lambda: ((rest.pop(0) if len(rest) > 1 else rest[0]), 16_380))

    def test_the_llm_gives_way_when_the_reload_is_worth_it(self, idle,
                                                          monkeypatch, audio):
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)     # 5.6 s/GB <= 20
        srv = _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)
        self._pool(idle, monkeypatch, 1_500, 9_000)

        out = idle._reclaim_gpu_for_speech("3400 MB claim refused: no room")

        assert len(srv.urls("/api/generate")) == 1, "the LLM has to be asked"
        assert out["gave_way"] is True and out["freed_mb"] == 7_500
        assert "ollama dropped" in out["detail"], out["detail"]
        assert "7500 MB came back" in out["detail"], out["detail"]
        assert idle._llm_reload["pending"] is True, (
            "the speech model caused this reload, so the next turn must "
            "measure what it cost")

    def test_the_llm_keeps_the_card_when_the_reload_costs_more(self, idle,
                                                             monkeypatch, audio):
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)    # 30.0 s/GB > 20
        srv = _server(idle, monkeypatch, size_gb=18.0, vram_gb=7.3)
        self._pool(idle, monkeypatch, 1_500, 9_000)

        out = idle._reclaim_gpu_for_speech("3400 MB claim refused: no room")

        assert srv.urls("/api/generate") == [], (
            "a measured reload this expensive keeps the model — the speech "
            "model is the one that gives way")
        assert out["gave_way"] is False and out["freed_mb"] is None
        assert "did not give the card back" in out["detail"], out["detail"]
        assert "30.0 s/GB > 20" in out["detail"], out["detail"]

    def test_nothing_resident_is_not_an_eviction(self, idle, monkeypatch, audio):
        srv = _server(idle, monkeypatch)                 # /api/ps: empty
        self._pool(idle, monkeypatch, 1_500, 1_500)

        out = idle._reclaim_gpu_for_speech("no room")

        assert srv.urls("/api/generate") == [], (
            "nothing to give back is not a reason to ask")
        assert out["gave_way"] is False
        assert "nothing resident" in out["detail"], out["detail"]

    def test_an_ollama_that_does_not_answer_says_it_could_not_be_asked(
            self, idle, monkeypatch, audio):
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        asks = _no_answer(idle, monkeypatch)
        self._pool(idle, monkeypatch, 1_500, 1_500)

        out = idle._reclaim_gpu_for_speech("no room")

        assert any(u.endswith("/api/generate") for u in asks), (
            "the ask is the whole point; whether it answers is a separate fact")
        assert out["gave_way"] is False
        assert "did not answer" in out["detail"], out["detail"]

    def test_a_card_that_cannot_be_measured_is_not_called_zero(self, idle,
                                                              monkeypatch, audio):
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)
        monkeypatch.setattr(idle, "_vram_pool_mb", lambda: None)

        out = idle._reclaim_gpu_for_speech("no room")

        assert out["gave_way"] is True and out["freed_mb"] is None, (
            "an unmeasurable gain is not a gain of zero")
        assert "could not be re-read" in out["detail"], out["detail"]

    def test_the_gain_waits_for_the_driver_to_show_it(self, idle, monkeypatch,
                                                     audio):
        """An unload is asynchronous: the memory appears a moment later.

        Measured live: 2 218 MB free immediately after an 8.2 GB unload, 10 417
        MB a moment later — so a reclaim that took the first reading reported
        "0 MB came back" for a reclaim that freed 8 GB. The poll is bounded and
        this test drives it with a budget of zero sleep, so nothing is really
        waited for here.
        """
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)
        monkeypatch.setattr(idle, "_RECLAIM_POLL_SECONDS", 0.0)
        self._pool(idle, monkeypatch, 2_218, 2_218, 10_417)

        out = idle._reclaim_gpu_for_speech("no room")

        assert out["gave_way"] is True and out["freed_mb"] == 8_199, out
        assert "8199 MB came back" in out["detail"], out["detail"]

    def test_a_card_that_never_shows_the_gain_is_not_reported_as_one(
            self, idle, monkeypatch, audio):
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)
        monkeypatch.setattr(idle, "_RECLAIM_SETTLE_SECONDS", 0.0)
        self._pool(idle, monkeypatch, 2_218)             # never moves

        out = idle._reclaim_gpu_for_speech("no room")

        assert out["gave_way"] is True, "the model was dropped — that is a fact"
        assert out["freed_mb"] == 0, "...and the card showing nothing is another"
        assert "had not shown the memory" in out["detail"], out["detail"]

    def test_the_cached_reading_is_dropped_after_a_reclaim(self, idle,
                                                          monkeypatch, audio):
        """The card changed a moment ago: a 30 s old sample is a wrong one."""
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)
        idle._vram_sample["at"] = idle._tick_now()
        self._pool(idle, monkeypatch, 1_500, 9_000)

        idle._reclaim_gpu_for_speech("no room")

        assert idle._vram_sample["at"] == 0.0, (
            "the pressure decision and the doctor must see the new reading")

    def test_the_hook_is_wired_into_the_audio_module(self, idle, monkeypatch):
        """A policy nobody installed is a policy that never runs.

        Two things are checked, because the behavioural one alone is not
        enough: a settings reload re-configures core/audio with the same hook,
        so a build that installed it at only ONE of the two sites still passes
        the callable check in this process (the reload site masks the
        import-time one). The count pins both — and pins why: the module-level
        call is the one that is live at startup, before any reload.
        """
        seen = []
        monkeypatch.setattr(
            idle, "_reclaim_gpu_for_speech",
            lambda reason="": seen.append(reason) or {"gave_way": False})

        assert callable(idle._audio._GPU_RECLAIM), (
            "core/audio has to be configured with the host's reclaim policy")
        idle._audio._GPU_RECLAIM("the claim was refused")

        assert seen == ["the claim was refused"], (
            "the hook hands the refusal reason to the policy that decides")

        source = (ROOT / "handsoff.py").read_text(encoding="utf-8")
        assert source.count("gpu_reclaim=lambda") == 2, (
            "both configure() calls install the hook — the import-time one "
            "(live before any reload) and the settings-reload one")


class TestTheTurnAsksTheSpeechModel:
    """The mirror of the reclaim: a turn that needs the card asks the VOICE.

    `_reclaim_gpu_for_speech` had the speech loader asking the LLM to move, and
    weighing the LLM's reload before it did. Here the LLM is the one that needs
    room and the speech model is the one that can move — it reloads in seconds,
    where Ollama's own answer to a full card is to offload half the model to the
    CPU. The tests below are mostly about the ways it must NOT happen: nothing of
    ours on the card, a claim that already fits, a model already loaded, a
    release that would not make room, a size nobody could read, something
    speaking, and the setting turned off.
    """

    def _speech_on_card(self, idle, monkeypatch, mb=3_400):
        """`core.audio` reporting the speech model resident on the card."""
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {
            "tts_mb": mb, "whisper_mb": 0, "total_mb": mb, "tts_loaded": mb > 0,
            "whisper_loaded": False, "tts_device": "cuda" if mb else "",
            "whisper_device": ""})

    def _pool(self, idle, monkeypatch, *readings):
        """nvidia-smi's (free, total) as the ask sees it, one per read."""
        rest = list(readings)
        monkeypatch.setattr(
            idle, "_vram_pool_mb",
            lambda: ((rest.pop(0) if len(rest) > 1 else rest[0]), 16_380))

    def _drops(self, idle, monkeypatch, dropped=None):
        """`_release_models` as a release: recorded, no real model involved."""
        calls = []

        def release():
            calls.append(True)
            return dropped or {"tts": True, "whisper": False,
                               "cache_cleared": False}

        monkeypatch.setattr(idle, "_release_models", release)
        return calls

    def test_the_speech_model_gives_the_card_back_for_a_turn(self, idle,
                                                             monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        srv = _server(idle, monkeypatch, tags_gb=5.0)   # /api/ps: nothing loaded
        drops = self._drops(idle, monkeypatch)
        idle._vram_sample["at"] = idle._tick_now()      # a live cached reading
        self._pool(idle, monkeypatch, 3_000, 6_400)

        out = idle._free_the_card_for_the_llm("a turn that needs the LLM")

        assert out["gave_way"] is True and out["freed_mb"] == 3_400, out
        assert "the speech model" in out["detail"]
        assert "3400 MB came back" in out["detail"], out["detail"]
        assert "the voice reloads in seconds" in out["detail"], out["detail"]
        assert drops == [True]
        assert srv.urls("/api/tags"), (
            "the model's size is read before the voice is spent: without it "
            "nothing was weighed")
        assert srv.urls("/api/ps"), "...and only then is residency worth asking"
        assert idle._vram_sample["at"] == 0.0, (
            "the card changed a moment ago — the cached reading is now wrong")

    def test_room_on_the_card_is_not_a_reason_to_ask(self, idle, monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        srv = _server(idle, monkeypatch, tags_gb=5.0)
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 9_000)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False and "fits already" in out["detail"]
        assert drops == [], "the voice is not spent on room the card already has"
        # Residency IS asked even here, and deliberately: the claim itself is
        # "what is still missing", so a turn cannot be weighed without it. The
        # cost is one localhost GET; the alternative (weigh the whole blob and
        # ask residency only once it fails) is what reported a fully resident
        # model as memory the card refused.
        assert srv.urls("/api/ps"), "the claim counts what is already loaded"

    def test_a_resident_model_is_not_weighed_by_its_whole_blob(
            self, idle, monkeypatch):
        """The live defect, as a test: a resident model needs no room.

        Measured 2026-09-18, deployed bubble: `gemma4:12b` resident in full
        (8.0 GB of it on the card) with 1.7 GB free. Every turn logged
        `7207 MB claim refused: 8231 MB needed (1024 MB reserve) but only 5083
        MB is free` and reported that it "needed the card" — while the turn had
        nothing to load, and the number it weighed included the memory the
        model already held.
        """
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, size_gb=7.8, vram_gb=7.8, tags_gb=7.04)
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 1_683)

        out = idle._free_the_card_for_the_llm("a turn that needs the LLM")

        assert out["gave_way"] is False and drops == []
        assert "already on the card in full" in out["detail"], out["detail"]
        assert "claim refused" not in out["detail"], (
            "a resident model is not a claim the card has to find room for")

    def test_a_split_model_is_weighed_by_its_unmet_remainder(
            self, idle, monkeypatch):
        """The other half: a split model asks only for the missing part.

        Ollama had `size_vram` of a 8.0 GB model at 7.5 GB, so the turn needs
        512 MB more — not the 8.0 GB blob. That difference is the decision:
        the card cannot hold the blob, and the voice therefore stays, even
        though 512 MB is room the release really would make.
        """
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, size_gb=8.0, vram_gb=7.5, tags_gb=8.0)
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 500, 3_900)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is True, out
        assert drops == [True]
        assert "512 MB claim" in out["detail"], (
            f"the unmet remainder is the claim, not the whole model: {out}")

    def test_a_model_already_loaded_is_not_a_model_to_make_room_for(
            self, idle, monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, size_gb=5.0, vram_gb=5.0, tags_gb=5.0)
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 3_000)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False and "already on the card" in out["detail"]
        assert drops == [], (
            "this turn loads nothing, so evicting the voice would only cost a "
            "reload on the reply")

    def test_memory_that_would_not_make_room_keeps_the_voice(self, idle,
                                                            monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, tags_gb=20.0)
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 2_000)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False
        assert "would not make room" in out["detail"], out["detail"]
        assert drops == [], (
            "handing back memory that cannot change the outcome is a pure loss")

    def test_a_size_nobody_could_read_does_not_evict_the_voice(self, idle,
                                                              monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch)                     # empty catalogue
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 2_000)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False and "could not be read" in out["detail"]
        assert drops == []

    def test_nothing_of_ours_on_the_card_is_nothing_to_ask_for(self, idle,
                                                              monkeypatch):
        self._speech_on_card(idle, monkeypatch, mb=0)   # e.g. a cpu voice
        drops = self._drops(idle, monkeypatch)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False
        assert "holds nothing on the card" in out["detail"]
        assert drops == []

    def test_the_setting_can_forbid_the_ask(self, idle, monkeypatch):
        monkeypatch.setitem(idle.SETTINGS, "speech_yields_to_llm", False)
        self._speech_on_card(idle, monkeypatch)
        drops = self._drops(idle, monkeypatch)
        reads = []
        monkeypatch.setattr(idle, "_vram_pool_mb",
                            lambda: reads.append(True) or (3_000, 16_380))

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False and "is off" in out["detail"]
        assert drops == [] and reads == [], (
            "a feature that is off must not even read the card")

    def test_the_gain_waits_for_the_driver_to_show_it(self, idle, monkeypatch):
        """Same asynchrony as the reclaim: the reading follows the unload."""
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, tags_gb=5.0)
        self._drops(idle, monkeypatch)
        monkeypatch.setattr(idle, "_RECLAIM_POLL_SECONDS", 0.0)
        self._pool(idle, monkeypatch, 3_000, 3_000, 6_400)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is True and out["freed_mb"] == 3_400, out

    def test_a_card_that_never_shows_the_gain_is_not_reported_as_one(
            self, idle, monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, tags_gb=5.0)
        self._drops(idle, monkeypatch)
        monkeypatch.setattr(idle, "_RECLAIM_SETTLE_SECONDS", 0.0)
        self._pool(idle, monkeypatch, 3_000)           # never moves

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is True           # the model was dropped
        assert out["freed_mb"] == 0             # ...and the card showed nothing
        assert "had not shown the memory" in out["detail"], out["detail"]

    def test_models_mid_use_are_not_torn_out(self, idle, monkeypatch):
        """A held model lock is 'not now', not a release that half happened."""
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, tags_gb=5.0)
        self._drops(idle, monkeypatch, dropped={"tts": False, "whisper": False,
                                                "cache_cleared": False})
        self._pool(idle, monkeypatch, 3_000)

        out = idle._free_the_card_for_the_llm()

        assert out["gave_way"] is False
        assert "left the card as it was" in out["detail"], out["detail"]

    def test_something_speaking_postpones_the_ask(self, idle, monkeypatch):
        self._speech_on_card(idle, monkeypatch)
        _server(idle, monkeypatch, tags_gb=5.0)
        drops = self._drops(idle, monkeypatch)
        self._pool(idle, monkeypatch, 3_000)

        assert idle._ANNOUNCE_LOCK.acquire(blocking=False)
        try:
            out = idle._free_the_card_for_the_llm()
        finally:
            idle._ANNOUNCE_LOCK.release()

        assert out["gave_way"] is False and "left alone" in out["detail"]
        assert drops == [], "the sentence being read must not lose its voice"

    def test_the_size_is_read_once_per_model_not_once_per_turn(self, idle,
                                                              monkeypatch):
        """This sits on the turn path: one HTTP call per model, not per turn."""
        srv = _server(idle, monkeypatch, tags_gb=5.0)

        assert idle._llm_footprint_mb() == 5_120
        assert idle._llm_footprint_mb() == 5_120
        assert len(srv.urls("/api/tags")) == 1, srv.urls("/api/tags")

        monkeypatch.setattr(idle, "OLLAMA_MODEL", "other:7b")
        assert idle._llm_footprint_mb() is None, (
            "a swapped model starts a fresh read rather than inheriting the "
            "previous model's size")
        assert len(srv.urls("/api/tags")) == 2

    def test_the_streaming_turn_asks_and_a_background_call_does_not(
            self, idle, monkeypatch):
        seen = []
        monkeypatch.setattr(
            idle, "_free_the_card_for_the_llm",
            lambda reason="": seen.append(reason) or {"gave_way": False})
        _stub_brain(idle, monkeypatch, ollama_chat=..., ollama_chat_stream=...)

        idle.ollama_chat_stream([{"role": "user", "content": "hi"}],
                                queue.Queue())
        assert seen, "the turn the user is waiting for may ask the voice"

        idle.ollama_chat([{"role": "user", "content": "hi"}])
        assert len(seen) == 1, (
            "a background call (memory extraction, tool round-trip) must not "
            "evict the voice for work nobody is waiting for")

    def test_a_raising_ask_cannot_kill_the_turn(self, idle, monkeypatch):
        def explode(reason=""):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(idle, "_free_the_card_for_the_llm", explode)
        _stub_brain(idle, monkeypatch, ollama_chat_stream=...)

        assert idle.ollama_chat_stream([], queue.Queue()) == {}, (
            "a broken memory probe is not a reason to lose the answer")

    def test_the_headroom_line_says_what_a_turn_would_ask(self, idle,
                                                         monkeypatch):
        _nvidia(idle, monkeypatch, pool="3000, 16380",
                procs=f"{os.getpid()}, 3400")
        self._speech_on_card(idle, monkeypatch)
        idle._llm_footprint.update({"model": idle.OLLAMA_MODEL, "mb": 5_000,
                                    "at": time.time()})

        line = _story_line(idle, "next turn")

        assert "asks 4.9 GB" in line, line
        assert "this bubble's 3.3 GB goes back to make room" in line, line
        state = idle.doctor_json()["gpu_headroom"]["next_turn"]
        assert state["would_yield"] is True and state["claim_mb"] == 5_000
        assert state["speech_gives_mb"] == state["held_mb"] > 0

    def test_the_line_says_when_the_voice_keeps_its_memory(self, idle,
                                                          monkeypatch):
        _nvidia(idle, monkeypatch, pool="10000, 16380",
                procs=f"{os.getpid()}, 3400")
        self._speech_on_card(idle, monkeypatch)
        idle._llm_footprint.update({"model": idle.OLLAMA_MODEL, "mb": 26_000,
                                    "at": time.time()})

        line = _story_line(idle, "next turn")

        assert "asks 25.4 GB" in line, line
        assert "would not make room" in line, line
        assert idle.doctor_json()["gpu_headroom"]["next_turn"][
            "would_yield"] is False, "the voice cannot make room for 26 GB"
        assert idle.doctor_json()["gpu_headroom"]["next_turn"][
            "speech_gives_mb"] == 0

    def test_the_line_says_when_the_ask_is_off_or_unread(self, idle,
                                                       monkeypatch):
        _nvidia(idle, monkeypatch, pool="3000, 16380",
                procs=f"{os.getpid()}, 3400")
        self._speech_on_card(idle, monkeypatch)
        # The cache says "asked, and the size could not be read" — pinning it is
        # what makes this the unread case rather than a read of whichever model
        # the developer's Ollama happens to be serving.
        monkeypatch.setattr(idle, "_llm_footprint",
                            {"model": idle.OLLAMA_MODEL, "mb": None,
                             "need_mb": None, "at": time.time(),
                             "resident": None, "resident_at": time.time()})

        line = _story_line(idle, "next turn")   # no size read yet
        assert "the LLM's size has not been read yet" in line, line

        monkeypatch.setitem(idle.SETTINGS, "speech_yields_to_llm", False)
        line = _story_line(idle, "next turn")
        assert "never asks the speech model for the card " \
               "(speech_yields_to_llm off)" in line, line

    def test_the_fit_line_names_a_model_that_can_never_be_resident(
            self, idle, monkeypatch):
        """The question nothing else in the diagnostics asks, and the one that
        decides whether a turn takes seconds or minutes.

        Measured live on 2026-09-18: `qwen3.8:27b` at 17.7 GB on a 16.0 GB
        card, 7 min 49 s from key release to spoken reply — while `brain:`
        said "reachable" and the card's line counted free bytes. The same turn
        took 10 s after the model was changed to a 7.6 GB one.
        """
        monkeypatch.setattr(idle, "_llm_footprint_mb", lambda: 18_124)   # 17.7 GB
        out = idle._brain_fit_note(16_380)
        assert "can NEVER be fully offloaded" in out, out
        assert "MINUTES" in out and "Settings" in out, out

        # A model that fits with room for the voice, and one that just fits.
        monkeypatch.setattr(idle, "_llm_footprint_mb", lambda: 7_782)    # 7.6 GB
        assert "stay on the GPU" in idle._brain_fit_note(16_380)
        monkeypatch.setattr(idle, "_llm_footprint_mb", lambda: 14_500)
        assert "little is left for speech" in idle._brain_fit_note(16_380)

        # Nothing measured, nothing claimed — the same rule the release
        # verdict follows, so an unreadable card or model cannot invent advice.
        monkeypatch.setattr(idle, "_llm_footprint_mb", lambda: None)
        assert idle._brain_fit_note(16_380) == ""
        monkeypatch.setattr(idle, "_llm_footprint_mb", lambda: 7_782)
        assert idle._brain_fit_note(None) == ""
        assert idle._brain_fit_note("junk") == ""

        # ...and it rides the doctor's own line list, so `--ptt doctor` says it.
        _nvidia(idle, monkeypatch, pool="10000, 16380",
                procs=f"{os.getpid()}, 3400")
        lines = idle._vram_story_lines()
        assert any("brain fit:" in ln for ln in lines), lines


class TestReloadMeasurement:
    """The number the policy rests on has to be measured, and belongs to a model.

    Taken at the first sentence after a release: the model is back in memory,
    the prompt re-prefilled, the first words out — everything past that is
    generation the user is already hearing, which is why timing the whole call
    would report a long answer as a slow reload.
    """

    def test_the_first_sentence_after_a_release_is_the_measurement(self, idle, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(idle, "time", type("T", (), {
            "monotonic": staticmethod(lambda: clock["t"]),
            "time": staticmethod(lambda: clock["t"])}))
        _stub_brain(idle, monkeypatch, ollama_chat_stream=1)
        idle._llm_reload["pending"] = True
        q = idle._SentenceQueue()

        idle.ollama_chat_stream([], q)          # arms the probe at the ask
        clock["t"] += 219.0                     # ...the model reloads...
        q.put("Hello there.")

        assert idle._measured_llm_reload() == pytest.approx(219.0)
        assert idle._llm_load["model"] == idle.OLLAMA_MODEL
        assert idle._llm_reload["pending"] is False

    def test_the_end_of_stream_is_not_the_first_sentence(self, idle, monkeypatch):
        _stub_brain(idle, monkeypatch, ollama_chat_stream=1)
        idle._llm_reload["pending"] = True
        q = idle._SentenceQueue()

        idle.ollama_chat_stream([], q)
        q.put(None)                             # terminator: no words spoken

        assert idle._measured_llm_reload() is None
        assert idle._llm_reload["pending"] is True, (
            "the stream ending without a sentence must not be priced as one")

    def test_a_non_streaming_call_cannot_price_a_reload(self, idle, monkeypatch):
        """Its whole length is the answer, not the load."""
        _stub_brain(idle, monkeypatch, ollama_chat=1)
        idle._llm_reload["pending"] = True
        idle._llm_reload["started"] = 1.0

        idle.ollama_chat([])

        assert idle._llm_reload["pending"] is False
        assert idle._measured_llm_reload() is None, (
            "a long reply must not be recorded as an expensive reload")

    def test_a_warm_load_never_makes_a_cold_reload_look_cheap(self, idle):
        """The number the policy weighs has to be a cold reload.

        This machine logged 4.7 s for loading a model that was already
        resident, minutes after a 218.9 s cold load of the SAME model. The
        release causes the cold one; believing the 4.7 would argue for a
        release whose cost it cannot see.
        """
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        idle._note_llm_load(idle.OLLAMA_MODEL, 4.7)

        assert idle._measured_llm_reload() == pytest.approx(218.9)
        assert idle._llm_load["last"] == pytest.approx(4.7), (
            "the newest measurement is still recorded — it is the slowest that "
            "is weighed")

    def test_a_new_model_starts_its_own_record(self, idle, monkeypatch):
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        monkeypatch.setattr(idle, "OLLAMA_MODEL", "tiny:1b")

        idle._note_llm_load("tiny:1b", 5.0)

        assert idle._measured_llm_reload() == pytest.approx(5.0), (
            "the big model's worst load must not follow the new model around")

    def test_the_measurement_belongs_to_the_model_it_was_taken_on(self, idle, monkeypatch):
        idle._note_llm_load(idle.OLLAMA_MODEL, 219.0)
        assert idle._measured_llm_reload() == pytest.approx(219.0)

        monkeypatch.setattr(idle, "OLLAMA_MODEL", "tiny:1b")

        assert idle._measured_llm_reload() is None, (
            "the new model's reload is not the old model's reload")

    @pytest.mark.parametrize("junk", [None, "slow", -1.0, 0.0])
    def test_a_measurement_nobody_can_use_is_not_recorded(self, idle, junk):
        idle._note_llm_load(idle.OLLAMA_MODEL, junk)
        assert idle._measured_llm_reload() is None

    def test_the_startup_warm_is_where_the_first_measurement_comes_from(
            self, idle, monkeypatch):
        """The loader's warm call is the number the first decision rests on."""
        clock = {"t": 100.0}
        monkeypatch.setattr(idle, "time", type("T", (), {
            "time": staticmethod(lambda: clock["t"]),
            "monotonic": staticmethod(lambda: clock["t"])}))

        def slow_warm(*_args, **_kwargs):
            clock["t"] += 41.5                    # the model loads
            return {}

        monkeypatch.setattr(idle, "ollama_chat", slow_warm)
        assistant = idle.Assistant.__new__(idle.Assistant)
        assistant._history = []

        assistant._warm_llm()

        assert idle._measured_llm_reload() == pytest.approx(41.5)

    def test_a_failed_warm_records_nothing_and_does_not_raise(self, idle, monkeypatch):
        """An unmeasured reload releases; a fabricated one might not."""
        def dead(*_args, **_kwargs):
            raise RuntimeError("cannot reach Ollama")

        monkeypatch.setattr(idle, "ollama_chat", dead)
        assistant = idle.Assistant.__new__(idle.Assistant)
        assistant._history = []

        assistant._warm_llm()                        # must not raise

        assert idle._measured_llm_reload() is None


class TestTheReleaseLine:
    """The card section's `release:` line — the policy, inside the story.

    It used to be a line of its own (`llm memory:`), which is how the LLM's
    memory came to be reported TWICE: once as a policy there and once as a
    number in the headroom line. The sentence is unchanged; where it lives is
    what changed.
    """

    def test_it_says_which_way_it_would_go_with_the_numbers(self, idle, monkeypatch):
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        idle._llm_load["at"] = time.time() - 120.0
        _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)

        sentence = idle._llm_release_sentence()

        assert "after 600s idle the release would KEEP" in sentence
        assert idle.OLLAMA_MODEL in sentence
        assert "22.6 s/GB > 20" in sentence
        assert "slowest of 1 load; last measured 2 minutes ago" in sentence, (
            "the number is a measurement, so the line has to say how old it is "
            "and whether it is the worst case")

    def test_it_would_unload_a_model_that_fits(self, idle, monkeypatch):
        idle._note_llm_load(idle.OLLAMA_MODEL, 41.0)
        _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)

        sentence = idle._llm_release_sentence()

        assert "would UNLOAD" in sentence and "7.9 s/GB ≤ 20" in sentence

    def test_an_off_release_says_off_instead_of_predicting(self, idle, monkeypatch):
        monkeypatch.setitem(idle.SETTINGS, "idle_release_seconds", 0)
        srv = _server(idle, monkeypatch, size_gb=5.2, vram_gb=5.2)

        assert idle._llm_release_sentence() == \
            "idle release is OFF (idle_release_seconds 0)"
        assert srv.asks == [], "an off switch must not probe the model server"

    def test_the_doctor_prints_the_sentence_inside_the_card_section(
            self, idle, monkeypatch):
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)

        text = idle.run_doctor()

        release = _story_line(idle, "release")
        assert "after 600s idle the release would KEEP" in release, release
        assert "22.6 s/GB > 20" in release, release
        assert "\n  release: " in text, (
            "the release belongs to the card's section, not to a line of its "
            "own beside it")

    def test_a_host_without_the_collector_prints_nothing_new(self):
        """A partial deps object is what the legacy path is tested with."""
        doctor = core_module("doctor")
        deps = doctor.DoctorDeps(ollama_base="http://127.0.0.1:11434",
                                 ollama_model="m",
                                 ollama_available=lambda: True)
        assert doctor._gpu_story_lines(deps) == []
        assert not hasattr(deps, "llm_lines"), (
            "the LLM's memory is reported by the card's one collector now")


def _nvidia(H, monkeypatch, *, pool=None, procs=None):
    """nvidia-smi as the headroom probe sees it: canned text per query.

    Patched at `_nvidia_query` — the one place that shells out — so these tests
    drive the REAL parsers with the shapes the real command prints, including
    the `N/A` a driver that cannot answer returns.
    """
    def query(*args):
        joined = " ".join(args)
        if "memory.free" in joined:
            return pool
        if "compute-apps" in joined:
            return procs
        return None
    monkeypatch.setattr(H, "_nvidia_query", query)


class TestGpuHeadroom:
    """The card's section — its own arithmetic, built from the driver's rows.

    The line that would have made the desktop glitch legible BEFORE it happened:
    how much of the card is left, how much of what it lost is THIS process, and
    whether the idle release is about to give its own share back. The audit that
    prompted the release had to measure free VRAM by hand; the bubble read it
    all along and said it nowhere (`resource_alerts` defaults off and the
    doctor's GPU section stops at the card's name).

    Two rules the tests below are really about: an answer nobody gave is never
    rendered as a number (0 MB free / 0.0 GB held are claims, not defaults), and
    a number the driver ATTRIBUTED to this pid is not presented as the same kind
    of evidence as one added up from the loader tables.
    """

    def test_the_pool_is_the_first_gpu_and_na_is_not_a_reading(self, idle):
        assert idle._parse_vram_pool("10061, 16380") == (10061, 16380)
        assert idle._parse_vram_pool("10061, 16380\n2000, 16380") == (10061, 16380)
        assert idle._parse_vram_pool("N/A, 16380") is None, (
            "'0 MB free' is the loudest claim this line can make")
        assert idle._parse_vram_pool("") is None
        assert idle._parse_vram_pool("garbage") is None
        assert idle._parse_vram_pool("10061, 0") is None, "a zero-total GPU"

    def test_only_this_process_is_counted(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch,
                procs=f"1941, gslapper, 436\n"
                      f"{os.getpid()}, /usr/bin/python3, 3172\n"
                      f"{os.getpid()}, /usr/bin/python3, 100\n"
                      f"35088, chrome, 719")
        assert idle._own_vram_mb() == 3272, (
            "the compositor's, ollama's and the wallpaper's memory is not the "
            "bubble's — and folding it in overstates what a release can free")

    def test_an_unattributable_query_is_not_a_zero(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, procs="N/A, N/A, N/A")
        assert idle._own_vram_mb() is None
        _nvidia(idle, monkeypatch, procs="")
        assert idle._own_vram_mb() == 0, (
            "the query answered and this pid is not on the card: a measurement "
            "of nothing, not an unknown")

    def test_a_process_name_holding_commas_is_one_tenant(self, idle):
        """nvidia-smi prints the whole argv, and a browser's is full of commas.

        Splitting on every comma turns one 382 MB tenant into a dozen
        unparsable fragments — and the card's total stops adding up, which is
        the one property this whole section rests on.
        """
        rows = idle._parse_card_tenants(
            "161850, /opt/google/chrome/chrome --type=gpu-process "
            "--field-trial-handle=3?i=1,2?3,382")
        assert rows == [{"pid": 161850,
                         "name": "/opt/google/chrome/chrome --type=gpu-process "
                                 "--field-trial-handle=3?i=1,2?3",
                         "mb": 382}], rows

    def test_a_driver_that_says_there_are_no_processes_answered(self, idle):
        """`No running processes found` is an answer; `N/A` is not an answer."""
        assert idle._parse_card_tenants("No running processes found") == []
        assert idle._parse_card_tenants("") == []
        assert idle._parse_card_tenants("N/A, N/A, N/A") is None
        assert idle._parse_card_tenants(None) is None

    def test_the_line_names_free_vram_and_this_bubbles_measured_share(
            self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380",
                procs=f"{os.getpid()}, 3172")

        lines = idle._vram_story_lines()

        assert lines[0].startswith("card: 9.8 GB free of 16.0 GB (39% used)"), \
            lines[0]
        tenants = _story_line(idle, "tenants")
        assert "this bubble holds 3.1 GB (measured)" in tenants, tenants

    def test_the_header_names_the_card_when_the_driver_reports_one(
            self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="NVIDIA GeForce RTX 4060 Ti, 10061, 16380")

        assert idle._vram_story_lines()[0].startswith(
            "card: NVIDIA GeForce RTX 4060 Ti — 9.8 GB free of 16.0 GB")

    def test_a_driver_that_cannot_attribute_falls_back_and_says_so(
            self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380", procs=None)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {
            "tts_mb": 3000, "whisper_mb": 0, "total_mb": 3000,
            "tts_loaded": True, "whisper_loaded": False,
            "tts_device": "cuda", "whisper_device": ""})

        info = idle._vram_headroom()

        assert info["bubble_source"] == "estimated"
        assert info["bubble_mb"] == 3000
        tenants = _story_line(idle, "tenants")
        assert "estimated from the loader tables" in tenants
        assert "the driver attributed no memory to a pid" in tenants, (
            "an estimate has to say WHY it is one — 'about 2.9 GB' with no "
            "cause reads like a measurement, which is the confusion this "
            "section exists to avoid")
        assert "what else is on the card could not be attributed" in tenants, (
            "when the driver cannot attribute anything, claiming 'no other "
            "process is on the card' would be inventing an answer")

    def test_a_measured_zero_is_not_the_same_as_an_estimate(self, idle, monkeypatch):
        """Answering 'this pid holds nothing' is better evidence than a guess."""
        _nvidia(idle, monkeypatch, pool="10061, 16380", procs="1941, gslapper, 436")

        info = idle._vram_headroom()

        assert info["bubble_mb"] == 0 and info["bubble_source"] == "measured"
        assert "holds 0.0 GB (measured)" in _story_line(idle, "tenants")

    def test_no_gpu_at_all_is_said_rather_than_zeroed(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool=None, procs=None)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {})

        info = idle._vram_headroom()

        assert info["free_mb"] is None and info["bubble_source"] == "unknown"
        lines = idle._vram_story_lines()
        assert "free VRAM unknown" in lines[0]
        assert "this bubble's own share could not be read" in \
            _story_line(idle, "tenants")

    def test_a_broken_probe_returns_no_line_instead_of_raising(self, idle,
                                                               monkeypatch):
        def explode():
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(idle, "_vram_headroom", explode)

        assert idle._vram_story_lines() == [], (
            "doctor is the tool for when things are already wrong")

    def test_the_probe_asks_the_driver_the_two_questions(self, idle, monkeypatch):
        seen = []

        class _Proc:
            returncode = 0

            def __init__(self, out):
                self.stdout = out

        def run(argv, **kwargs):
            seen.append(argv)
            return _Proc("10061, 16380" if "--query-gpu" in argv[1]
                         else f"{os.getpid()}, /usr/bin/python3, 512")

        monkeypatch.setattr(idle.subprocess, "run", run)

        info = idle._vram_headroom()

        assert any("--query-gpu=name,memory.free,memory.total" in a[1]
                   for a in seen), "the card's own name is part of the header"
        assert any("--query-compute-apps=pid,process_name,used_memory" in a[1]
                   for a in seen), (
            "naming the tenants needs the process column; a pid and its bytes "
            "alone cannot say WHO is on the card")
        assert info["free_mb"] == 10061 and info["bubble_mb"] == 512
        assert info["card"]["name"] == "" and info["tenants"]["attributed"]

    def test_a_wedged_driver_is_an_answer_not_an_exception(self, idle, monkeypatch):
        def run(argv, **kwargs):
            raise OSError("nvidia-smi is gone")

        monkeypatch.setattr(idle.subprocess, "run", run)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {})

        info = idle._vram_headroom()

        assert info["free_mb"] is None and info["bubble_mb"] == 0

    def test_a_nonzero_exit_is_not_read_as_output(self, idle, monkeypatch):
        """`nvidia-smi` prints its complaint on stdout and exits non-zero.

        Parsing that complaint is how a driver error becomes a number.
        """
        class _Proc:
            returncode = 9
            stdout = "10061, 16380\n"

        monkeypatch.setattr(idle.subprocess, "run",
                            lambda *a, **k: _Proc())
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {})

        info = idle._vram_headroom()

        assert info["free_mb"] is None and info["bubble_source"] == "unknown", (
            "a failed query is not a reading")
        assert "free VRAM unknown" in idle._vram_story_lines()[0]

    # -- the third fact: what the release is about to do ---------------------

    def test_off_when_the_window_is_zero(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380", procs=None)
        monkeypatch.setitem(idle.SETTINGS, "idle_release_seconds", 0)

        assert idle._vram_headroom()["idle_release"]["state"] == "off"
        assert _story_line(idle, "release") == \
            "  release: idle release is OFF (idle_release_seconds 0)"

    def test_pending_says_how_much_quiet_is_left(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380", procs=None)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 120.0)

        rel = idle._vram_headroom()["idle_release"]

        assert rel["state"] == "pending"
        assert rel["due_in_s"] == pytest.approx(480.0, abs=1.0)
        assert "in 8 minutes of quiet" in _story_line(idle, "release")

    def test_due_is_not_the_same_answer_as_pending(self, idle, monkeypatch):
        """The window elapsed and the tick has not run: the bubble is busy.

        Collapsing the two would read a bubble that is mid-turn as one that
        just booted, which is the opposite of what a reader wants to know.
        """
        _nvidia(idle, monkeypatch, pool="10061, 16380", procs=None)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 700.0)

        rel = idle._vram_headroom()["idle_release"]

        assert rel["state"] == "due" and rel["due_in_s"] == 0.0
        assert "DUE after 10 minutes of quiet" in _story_line(idle, "release")

    def test_released_says_the_next_use_re_arms_it(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380", procs=None)
        monkeypatch.setattr(idle, "_gpu_released", True)

        assert idle._vram_headroom()["idle_release"]["state"] == "released"
        assert "already fired in this quiet spell" in _story_line(idle, "release")

    # -- one dict behind both surfaces ---------------------------------------

    def test_the_doctor_prints_the_section(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380",
                procs=f"{os.getpid()}, /usr/bin/python3, 3172")

        text = idle.run_doctor()

        assert "card: 9.8 GB free of 16.0 GB" in text
        assert "this bubble holds 3.1 GB (measured)" in text
        assert "\n  tenants: " in text and "\n  speech: " in text, (
            "the four facts a user has about their card are one section")
        assert "\n  llm: " in text and "\n  next turn: " in text

    def test_json_carries_the_whole_story(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10061, 16380",
                procs=f"{os.getpid()}, /usr/bin/python3, 3172")

        head = idle.doctor_json()["gpu_headroom"]

        assert set(head) == {"card", "free_mb", "total_mb", "bubble_mb",
                             "bubble_source", "tenants", "speech", "llm",
                             "next_turn", "idle_release", "llm_release"}
        assert head["free_mb"] == 10061 and head["total_mb"] == 16380
        assert head["bubble_mb"] == 3172 and head["bubble_source"] == "measured"
        assert head["card"] == {"name": "", "free_mb": 10061,
                                "total_mb": 16380, "used_mb": 6319}
        assert head["idle_release"]["state"] == "pending"
        assert set(head["next_turn"]) == {"enabled", "held_mb", "claim_mb",
                                         "free_mb", "already_resident",
                                         "would_yield", "speech_gives_mb",
                                         "note"}, (
            "what a turn will ask for: the policy, what there is to give, what "
            "the LLM needs, and the verdict")
        assert head["llm_release"]["window_s"] == pytest.approx(600.0)
        assert isinstance(head["llm_release"]["sentence"], str)

    def test_a_host_without_the_probe_adds_nothing_to_either_surface(self):
        """The same contract the llm line keeps: a partial deps is unchanged."""
        doctor = core_module("doctor")
        deps = doctor.DoctorDeps(ollama_base="http://127.0.0.1:11434",
                                 ollama_model="m",
                                 ollama_available=lambda: True)
        assert doctor._gpu_story_lines(deps) == []

        token = doctor.set_dependencies(doctor.DoctorDeps())
        try:
            assert "gpu_headroom" not in doctor.doctor_json()
        finally:
            doctor.reset_dependencies(token)


def _residency_is_cold(H, monkeypatch):
    """Let the section ASK: invalidate the cached residency so it probes.

    The `idle` fixture pins a FRESH record with `resident` None, which is what
    keeps every other test off the developer's live Ollama. A test that wants a
    real residency (through `_server`) starts from a cold cache instead.
    """
    monkeypatch.setattr(H, "_llm_footprint",
                        {**H._llm_footprint, "resident": None,
                         "resident_at": 0.0, "resident_model": ""})


class TestTheCardSection:
    """One story for the card: every tenant, and what the next turn asks for.

    The four facts a user with a glitching desktop actually has — who is on the
    card, what the bubble's own speech models hold, what the LLM holds, and what
    the next turn will do — are ONE section built from ONE dict, which is also
    what `doctor_json` publishes. These tests pin each line's substance and the
    property that makes it a story rather than four numbers: the tenants add up
    to the used bytes on the header line.
    """

    def test_every_tenant_is_named(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=(
            "1941, gslapper, 436\n"
            f"{os.getpid()}, /usr/bin/python3, 3172\n"
            "4242, /usr/local/bin/ollama runner, 7000\n"
            "161850, /opt/google/chrome/chrome --type=gpu-process "
            "--field-trial-handle=1,2,382\n"))

        tenants = _story_line(idle, "tenants")

        assert "this bubble holds 3.1 GB (measured)" in tenants, tenants
        assert "an Ollama process holds 6.8 GB (pid 4242)" in tenants, tenants
        assert ("2 other processes hold 0.8 GB (gslapper 0.4 GB, chrome 0.4 GB)"
                in tenants), tenants
        assert "the driver attributes 0.7 GB to no process" in tenants, tenants
        # The parts are the used bytes: 436 + 3172 + 7000 + 382 + 700 = 11690,
        # which is what the card is missing from its 16380 total. A story whose
        # numbers do not add up is three numbers, not a story.
        head = idle.doctor_json()["gpu_headroom"]
        assert (head["card"]["used_mb"] == head["bubble_mb"]
                + head["tenants"]["llm_mb"] + head["tenants"]["others_mb"]
                + head["tenants"]["unattributed_mb"])

    def test_the_turn_that_asks_nothing_says_so(self, idle, monkeypatch):
        """A resident model is the case the old arithmetic got wrong."""
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        _residency_is_cold(idle, monkeypatch)
        _server(idle, monkeypatch, size_gb=7.0, vram_gb=7.0, tags_gb=7.0)

        assert "is resident 7.0 GB and ALL of it is on the card" in \
            _story_line(idle, "llm")
        assert "asks nothing" in _story_line(idle, "next turn")
        head = idle.doctor_json()["gpu_headroom"]
        assert head["llm"]["need_mb"] == 0
        assert head["next_turn"]["already_resident"] is True

    def test_a_split_model_is_described_as_split(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        _residency_is_cold(idle, monkeypatch)
        _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68, tags_gb=18.0)

        line = _story_line(idle, "llm")

        assert "is SPLIT" in line, line
        assert "9.7 GB on the card of 18.0 GB" in line, line
        assert "8.3 GB is served from system memory" in line, line
        head = idle.doctor_json()["gpu_headroom"]
        assert head["llm"]["on_card_mb"] == 9912
        assert head["llm"]["offloaded_mb"] == head["llm"]["need_mb"] == 8520, (
            "what a turn must load IS the offloaded remainder")

    def test_an_unreadable_residency_is_not_a_guess(self, idle, monkeypatch):
        """`/api/ps` down, `/api/tags` answering: the size is known, the split is
        not — and 'not loaded' is a measurement nobody made."""
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        _residency_is_cold(idle, monkeypatch)
        _server(idle, monkeypatch, tags_gb=7.0)      # /api/ps says nothing loaded

        def nowhere(req, timeout=None):
            if req.full_url.endswith("/api/ps"):
                raise OSError("connection refused")
            return _Reply(json.dumps({"models": [{
                "name": idle.OLLAMA_MODEL, "model": idle.OLLAMA_MODEL,
                "size": int(7.0 * 1024 ** 3)}]}).encode("utf-8"))

        monkeypatch.setattr(idle.urllib.request, "urlopen", nowhere)

        assert "could not be read" in _story_line(idle, "llm")
        assert idle.doctor_json()["gpu_headroom"]["llm"]["loaded"] is None, (
            "an endpoint that did not answer is not 'nothing is loaded'")
        assert idle.doctor_json()["gpu_headroom"]["llm"]["blob_mb"] == 7168

    def test_the_speech_line_names_both_models_and_their_device(self, idle,
                                                               monkeypatch):
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {
            "tts_mb": 1900, "whisper_mb": 1200, "total_mb": 3100,
            "tts_loaded": True, "whisper_loaded": True,
            "tts_device": "cuda", "whisper_device": "cuda"})

        line = _story_line(idle, "speech")

        assert ("whisper 1.2 GB on cuda and the speech model 1.9 GB on cuda"
                in line), line
        assert "3.0 GB together" in line, line
        assert idle.doctor_json()["gpu_headroom"]["speech"]["whisper_mb"] == 1200

    def test_nothing_loaded_names_which_half_is_missing(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {
            "tts_mb": 1900, "whisper_mb": 0, "total_mb": 1900,
            "tts_loaded": True, "whisper_loaded": False,
            "tts_device": "cuda", "whisper_device": ""})

        line = _story_line(idle, "speech")

        assert "the speech model 1.9 GB on cuda" in line, line
        assert "whisper" not in line, (
            "a model that is not on the card is not counted into the total")
        assert idle.doctor_json()["gpu_headroom"]["speech"]["whisper_mb"] == 0

    def test_nothing_loaded_at_all_says_both_halves(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {
            "tts_mb": 0, "whisper_mb": 0, "total_mb": 0,
            "tts_loaded": False, "whisper_loaded": False,
            "tts_device": "", "whisper_device": ""})

        assert ("nothing of this bubble's is on the card (the speech model "
                 "and whisper not loaded)") in _story_line(idle, "speech")

    def test_the_descriptive_read_is_cached_and_the_turn_keeps_it_warm(
            self, idle, monkeypatch):
        """Doctor describes; it must not probe the model server per line."""
        _residency_is_cold(idle, monkeypatch)
        srv = _server(idle, monkeypatch, size_gb=7.0, vram_gb=7.0, tags_gb=7.0)

        assert idle._resident_llm_cached() == idle._resident_llm_cached()
        assert len(srv.urls("/api/ps")) == 1, srv.asks

        idle._llm_need_mb(idle._resident_llm())     # a turn's live decision
        idle._resident_llm_cached()

        assert len(srv.urls("/api/ps")) == 2, (
            "the turn's reading is the section's — the cache is what the two "
            "surfaces share")

    def test_a_swapped_model_does_not_inherit_the_old_residency(
            self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="4690, 16380", procs=None)
        _residency_is_cold(idle, monkeypatch)
        srv = _server(idle, monkeypatch, size_gb=7.0, vram_gb=7.0, tags_gb=7.0)
        assert idle._resident_llm_cached()["loaded"] is True
        assert idle._resident_llm_cached()["loaded"] is True
        assert len(srv.urls("/api/ps")) == 1, (
            "one read, cached — and the cache is stamped for the model it was "
            "read for, which is what the swap below tests")

        monkeypatch.setattr(idle, "OLLAMA_MODEL", "a-smaller:4b")
        srv2 = _server(idle, monkeypatch, size_gb=4.0, vram_gb=4.0, tags_gb=4.0)
        holdings = idle._vram_headroom()["llm"]

        assert srv2.urls("/api/ps"), (
            "a residency belongs to the model it was read for: after a swap the "
            "section must ASK rather than describe the previous model")
        assert holdings["model"] == "a-smaller:4b"
        assert holdings["size_mb"] == 4096, "the NEW model's size, not the old"


class TestTheCardSectionIsNotMachines:
    """The section's numbers come from the probes, never from this machine."""

    def test_every_line_survives_a_card_that_cannot_be_asked(
            self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool=None, procs=None)
        monkeypatch.setattr(idle._audio, "gpu_footprint_mb", lambda: {})

        lines = idle._vram_story_lines()

        assert lines[0].startswith("card: free VRAM unknown"), lines[0]
        for label in ("tenants", "speech", "llm", "next turn", "release"):
            assert _story_line(idle, label), label

    def test_the_section_is_the_same_reading_as_the_json(self, idle, monkeypatch):
        """One dict behind both, so the words and the numbers cannot drift."""
        _nvidia(idle, monkeypatch, pool="4690, 16380",
                procs=f"{os.getpid()}, /usr/bin/python3, 3172")

        head = idle.doctor_json()["gpu_headroom"]
        text = idle.run_doctor()

        assert f"card: {head['free_mb'] / 1024:.1f} GB free" in text
        assert f"this bubble holds {head['bubble_mb'] / 1024:.1f} GB " \
               "(measured)" in text
        again = idle._vram_headroom()
        for key in ("card", "tenants", "speech"):
            assert head[key] == again[key], key
        # `*_age_s` counts the seconds since the reading, so it moves between
        # two calls by design; everything else about the LLM must not.
        at_rest = lambda holdings: {k: v for k, v in holdings.items()
                                    if not k.endswith("_age_s")}
        assert at_rest(head["llm"]) == at_rest(again["llm"])
        assert head["next_turn"]["claim_mb"] == again["next_turn"]["claim_mb"]


def _free_vram(H, monkeypatch, free_mb):
    """Free VRAM as the pressure decision sees it — the cached reading seam."""
    monkeypatch.setattr(H, "_free_vram_sample", lambda: free_mb)


def _pressure(H, monkeypatch, free_mb, *, floor_mb=1024, seconds=30):
    """Put the card under the floor: the floor, the short window, the reading.

    Returns the decision, so a test can state what it expects in one line.
    """
    monkeypatch.setitem(H.SETTINGS, "vram_pressure_floor_mb", floor_mb)
    monkeypatch.setitem(H.SETTINGS, "vram_pressure_seconds", seconds)
    _free_vram(H, monkeypatch, free_mb)
    return H._idle_release_window()


class TestVramPressure:
    """A full card shortens the release window — and the journal says why.

    The release exists because the DESKTOP was starved: 15.2 of 16.4 GB used,
    ~1 GB free, and nvidia-drm failing to allocate display buffers. Waiting the
    whole configured window while that is true is the wrong trade even though
    the release itself is right. Pressure therefore shortens WHEN and never
    WHAT: the LLM's keep/release verdict is untouched, and every busy check
    still applies.
    """

    def test_the_shipped_defaults_are_the_floor_and_the_short_window(
            self, idle, monkeypatch):
        """The suite runs with the floor pinned OFF, so the shipped pair needs
        its own guard or a change to it would be invisible here."""
        assert idle.DEFAULT_SETTINGS["vram_pressure_floor_mb"] == 1024
        assert idle.DEFAULT_SETTINGS["vram_pressure_seconds"] == 30
        assert idle.DEFAULT_SETTINGS["idle_release_seconds"] == 600

    def test_above_the_floor_the_configured_window_stands(self, idle, monkeypatch):
        _pressure(idle, monkeypatch, 8192)

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == pytest.approx(600.0)
        assert pressure["under"] is False and pressure["reason"] == ""

    def test_below_the_floor_the_window_shrinks_and_says_why(self, idle, monkeypatch):
        _pressure(idle, monkeypatch, 900)

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == pytest.approx(30.0), (
            "the point is to stop waiting — and the window in force is the one "
            "the tick, the journal and the doctor all read")
        assert "0.9 GB free is below the 1.0 GB floor" in pressure["reason"]
        assert "600s window is 30s" in pressure["reason"]

    def test_the_exact_floor_is_not_pressure(self, idle, monkeypatch):
        _pressure(idle, monkeypatch, 1024)

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == pytest.approx(600.0)
        assert pressure["under"] is False

    def test_a_floor_of_zero_never_rushes(self, idle, monkeypatch):
        monkeypatch.setitem(idle.SETTINGS, "vram_pressure_floor_mb", 0)
        _free_vram(idle, monkeypatch, 10)

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == pytest.approx(600.0)
        assert pressure["under"] is False

    def test_an_off_release_outranks_a_full_card(self, idle, monkeypatch):
        """An explicit "never release" is the user's word against the card's."""
        _pressure(idle, monkeypatch, 1)
        monkeypatch.setitem(idle.SETTINGS, "idle_release_seconds", 0)

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == 0.0 and pressure["under"] is False

    def test_an_unreadable_card_is_not_evidence_of_pressure(self, idle, monkeypatch):
        """A driver that will not answer must not cost the models a reload."""
        _pressure(idle, monkeypatch, None)

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == pytest.approx(600.0)
        assert pressure["under"] is False
        assert "could not be read" in pressure["reason"]

    def test_the_pressured_window_can_only_ever_be_earlier(self, idle, monkeypatch):
        pressure = _pressure(idle, monkeypatch, 10, seconds=9000)

        assert pressure["window_s"] == pytest.approx(600.0)
        assert pressure["under"] is False, (
            "a window that is not shorter is not an early release")

    def test_junk_never_rush_a_release(self, idle, monkeypatch):
        monkeypatch.setitem(idle.SETTINGS, "vram_pressure_floor_mb", "lots")
        _free_vram(idle, monkeypatch, 10)
        assert idle._idle_release_window()["window_s"] == pytest.approx(600.0)
        assert idle._vram_pressure_floor_mb() == 0.0

        _pressure(idle, monkeypatch, 10, seconds="soon")

        pressure = idle._idle_release_window()

        assert pressure["window_s"] == pytest.approx(600.0), (
            "an unreadable window must not mean 'release on the first quiet "
            "tick'")
        assert pressure["under"] is False, (
            "junk falls back to the configured window, so no release came "
            "early — and the reason still reports the floor")
        assert "below the 1.0 GB floor" in pressure["reason"]
        assert "not shorter" in pressure["reason"]

    def test_zero_seconds_means_the_first_quiet_tick(self, idle, monkeypatch):
        pressure = _pressure(idle, monkeypatch, 10, seconds=0)

        assert pressure["window_s"] == 0.0

    def test_the_reading_is_cached_and_re_read_after_the_cadence(
            self, idle, monkeypatch):
        reads = []

        def pool():
            reads.append(1)
            return (900, 16380)

        monkeypatch.setattr(idle, "_vram_pool_mb", pool)
        monkeypatch.setattr(idle, "_vram_sample", {"at": 0.0, "free_mb": None})

        assert idle._free_vram_sample() == 900
        assert idle._free_vram_sample() == 900
        assert len(reads) == 1, (
            "the idle tick runs every second and this shells out; a probe per "
            "tick would spend more CPU than the release saves")

        monkeypatch.setattr(idle, "_vram_sample", {
            "at": idle._tick_now() - idle._VRAM_SAMPLE_SECONDS - 1,
            "free_mb": 900})
        idle._free_vram_sample()

        assert len(reads) == 2

    def test_an_unknown_card_is_cached_as_an_answer_not_re_read_forever(
            self, idle, monkeypatch):
        monkeypatch.setattr(idle, "_vram_pool_mb", lambda: None)
        monkeypatch.setattr(idle, "_vram_sample", {"at": 0.0, "free_mb": None})

        assert idle._free_vram_sample() is None
        assert idle._vram_sample["at"] > 0.0, (
            "'could not be asked' is cached too, or every tick shells out")

    # -- the tick ------------------------------------------------------------

    def test_the_tick_fires_early_under_pressure_and_says_why(
            self, idle, monkeypatch, audio, caplog):
        _loaded(idle, monkeypatch, audio)
        _pressure(idle, monkeypatch, 900)
        unloads = _Unloads()
        monkeypatch.setattr(idle.urllib.request, "urlopen", unloads)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 31)

        with caplog.at_level(logging.INFO, logger="handsoff"):
            _assistant(idle)._idle_release_tick()

        assert idle._tts_model is None and audio._tts_model is None, (
            "31 s of quiet is past the pressured window, not the 600 s one")
        assert idle._gpu_released is True
        line = " ".join(r.getMessage() for r in caplog.records)
        assert "idle 30s (early —" in line, line
        assert "0.9 GB free is below the 1.0 GB floor" in line, line
        assert "600s window is 30s" in line, line

    def test_without_pressure_the_same_31_seconds_is_not_enough(
            self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        _pressure(idle, monkeypatch, 9000)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 31)

        _assistant(idle)._idle_release_tick()

        assert idle._tts_model is not None

    def test_a_busy_bubble_still_postpones_under_pressure(
            self, idle, monkeypatch, audio):
        _loaded(idle, monkeypatch, audio)
        _pressure(idle, monkeypatch, 900)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 10 ** 5)

        for shape in ({"state": "thinking"}, {"queued": 1},
                      {"recording": True}):
            _assistant(idle, **shape)._idle_release_tick()

        assert idle._tts_model is not None, (
            "pressure changes the window, never the busy checks")

    def test_pressure_shortens_when_not_what(self, idle, monkeypatch, audio):
        """The split model the verdict keeps is STILL kept under pressure."""
        _loaded(idle, monkeypatch, audio)
        _pressure(idle, monkeypatch, 900)
        idle._note_llm_load(idle.OLLAMA_MODEL, 218.9)
        srv = _server(idle, monkeypatch, size_gb=18.0, vram_gb=9.68)
        monkeypatch.setattr(idle, "_gpu_last_use", idle._tick_now() - 31)

        _assistant(idle)._idle_release_tick()

        assert idle._tts_model is None, "the release did fire, early"
        assert srv.urls("/api/generate") == [], (
            "the LLM is still the verdict's decision, not the pressure's")
        assert "22.6 s/GB > 20" in idle._llm_release_verdict()["note"]

    # -- the two surfaces ----------------------------------------------------

    def test_the_doctor_line_names_the_pressure(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="900, 16380", procs=None)
        monkeypatch.setitem(idle.SETTINGS, "vram_pressure_floor_mb", 1024)

        line = _story_line(idle, "release")

        assert "in 30 seconds of quiet" in line
        assert ("(VRAM pressure — 0.9 GB free is below the 1.0 GB floor, so the "
                "600s window is 30s)") in line, line

    def test_json_carries_the_pressure_fields(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="900, 16380", procs=None)
        monkeypatch.setitem(idle.SETTINGS, "vram_pressure_floor_mb", 1024)

        rel = idle.doctor_json()["gpu_headroom"]["idle_release"]

        assert rel["window_s"] == pytest.approx(30.0)
        assert rel["configured_s"] == pytest.approx(600.0)
        assert rel["under_pressure"] is True
        assert rel["floor_mb"] == pytest.approx(1024.0)
        assert "below the 1.0 GB floor" in rel["pressure_reason"]

    def test_a_released_spell_does_not_explain_itself_with_todays_reading(
            self, idle, monkeypatch):
        """The current reading says nothing about why a past release fired."""
        _nvidia(idle, monkeypatch, pool="900, 16380", procs=None)
        monkeypatch.setitem(idle.SETTINGS, "vram_pressure_floor_mb", 1024)
        monkeypatch.setattr(idle, "_gpu_released", True)

        line = _story_line(idle, "release")

        assert "already fired in this quiet spell" in line
        assert "VRAM pressure" not in line

    def test_no_pressure_means_no_clause(self, idle, monkeypatch):
        _nvidia(idle, monkeypatch, pool="10240, 16380", procs=None)

        line = _story_line(idle, "release")

        assert "in 10 minutes of quiet" in line
        assert "VRAM pressure" not in line
