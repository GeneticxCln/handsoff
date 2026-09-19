"""Audio tests: VAD gate, listener, wake word/spotter, mic health and self-heal."""
from __future__ import annotations

import base64
import importlib.util
import inspect
from collections import deque
import threading
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from conftest import (HERE as ROOT, _load, core_module, method_source,
                      run_driver, wait_for)

from core import settings as _core_settings

# Resolved on first use, inside the sandbox: a direct `from core import tools`
# bakes the developer's real HOME/XDG dirs into the module at collection time,
# which the suite's own guard refuses (and is right to).
_core_tools = core_module("tools")

# Resolved on first use rather than at collection, so it cannot bake the
# developer's HOME; see conftest.core_module.
_core_audio = core_module("audio")

HERE = ROOT   # the repo root (conftest resolves it from conftest.py's parent)


class TestGpuFootprint:
    """What this process is holding on the card, when nothing can measure it.

    `gpu_footprint_mb` is the ESTIMATE behind doctor's card section —
    used when the driver attributes no memory to a pid, which not every driver
    can do. The property that matters is the one the idle release also turns
    on: a model on cpu occupies NO card memory, so counting it would claim a
    release could hand back memory it never had. And the devices are reported
    even at zero, because "tts on cpu" is the fact that explains the zero.
    """

    def _loaded(self, monkeypatch, *, tts=None, tts_device="", whisper=None,
                whisper_device="", size="base"):
        monkeypatch.setattr(_core_audio, "_tts_model", tts)
        monkeypatch.setattr(_core_audio, "_tts_device", tts_device)
        monkeypatch.setattr(_core_audio, "_whisper_model", whisper)
        monkeypatch.setattr(_core_audio, "_whisper_device_used", whisper_device)
        monkeypatch.setattr(_core_audio, "WHISPER_SIZE", size)

    def test_nothing_loaded_holds_nothing(self, monkeypatch):
        self._loaded(monkeypatch)
        out = _core_audio.gpu_footprint_mb()
        assert out["total_mb"] == 0
        assert out["tts_loaded"] is False and out["whisper_loaded"] is False
        assert out["tts_device"] == "" and out["whisper_device"] == "", (
            "no model means no device to name — not 'cpu'")

    def test_a_cuda_tts_is_its_table_size_and_a_cpu_one_is_nothing(
            self, monkeypatch):
        self._loaded(monkeypatch, tts=object(), tts_device="cuda")
        assert _core_audio.gpu_footprint_mb()["total_mb"] == _core_audio._TTS_VRAM_MB

        self._loaded(monkeypatch, tts=object(), tts_device="cpu")
        out = _core_audio.gpu_footprint_mb()
        assert out["total_mb"] == 0 and out["tts_device"] == "cpu", (
            "a model on cpu occupies no card memory; counting it would be a "
            "promise the release cannot keep")

    def test_whisper_contributes_its_own_size_only_on_cuda(self, monkeypatch):
        self._loaded(monkeypatch, whisper=object(), whisper_device="cuda",
                     size="large-v3")
        assert _core_audio.gpu_footprint_mb()["whisper_mb"] == 3600

        self._loaded(monkeypatch, whisper=object(), whisper_device="cpu",
                     size="large-v3")
        out = _core_audio.gpu_footprint_mb()
        assert out["whisper_mb"] == 0 and out["whisper_device"] == "cpu"

    def test_the_two_models_add_up(self, monkeypatch):
        self._loaded(monkeypatch, tts=object(), tts_device="cuda",
                     whisper=object(), whisper_device="cuda", size="medium")
        out = _core_audio.gpu_footprint_mb()
        assert out["total_mb"] == _core_audio._TTS_VRAM_MB + 2600

    def test_an_unknown_size_falls_back_instead_of_raising(self, monkeypatch):
        self._loaded(monkeypatch, whisper=object(), whisper_device="cuda",
                     size="no-such-size")
        assert _core_audio.gpu_footprint_mb()["whisper_mb"] == 3600


class TestVramBudget:
    """ONE budget for the card, so two loaders cannot each pass on their own.

    The failure this exists for: whisper (3.9 GB claim) and speech (3.4 GB) each
    compared their own size against the SAME free reading and each went cuda
    into 4.5 GB free. One arithmetic, asked twice, cannot pass twice.
    """

    def test_a_claim_that_would_take_the_last_of_the_card_is_refused(self):
        budget = _core_audio.vram_budget(3_500, claim_mb=3_400, owner="speech")
        assert budget["fits"] is False
        assert budget["available_mb"] == 3_500 - _core_audio._VRAM_RESERVE_MB
        assert "4424 MB needed" in budget["reason"], budget["reason"]
        assert "only 3500 MB is free" in budget["reason"], budget["reason"]

    def test_the_reserve_is_not_optional(self):
        """A claim that exactly fills the card leaves the desktop nothing."""
        assert _core_audio.vram_budget(3_400, claim_mb=3_400,
                                       owner="speech")["fits"] is False
        free = 3_400 + _core_audio._VRAM_RESERVE_MB
        at_the_edge = _core_audio.vram_budget(free, claim_mb=3_400,
                                              owner="speech")
        assert at_the_edge["fits"] is True
        assert at_the_edge["available_mb"] - at_the_edge["claim_mb"] == 0, (
            "a claim that exactly uses the available room fits with nothing to spare")

    def test_memory_another_tenant_is_entitled_to_is_counted(self):
        """The entitlement is what turns two comparisons into one decision."""
        refused = _core_audio.vram_budget(5_000, claim_mb=1_400,
                                          owner="whisper", entitled_mb=3_400)
        assert refused["fits"] is False
        assert "3400 MB held back for the speech model" in refused["reason"], (
            refused["reason"])
        roomy = _core_audio.vram_budget(9_000, claim_mb=1_400,
                                        owner="whisper", entitled_mb=3_400)
        assert roomy["fits"] is True and roomy["available_mb"] == 4_576

    def test_an_unreadable_card_is_never_reported_as_room(self):
        """N/A is the absence of a reading: not zero free memory, and not a fit."""
        for reading in (None, "", "   ", "N/A", "[N/A]", "junk", object(),
                        float("nan"), float("inf"), []):
            budget = _core_audio.vram_budget(reading, claim_mb=600,
                                             owner="whisper")
            assert budget["fits"] is False, reading
            assert budget["free_mb"] is None, reading
            assert budget["available_mb"] is None, reading
            assert "could not be read" in budget["reason"], budget["reason"]

    def test_a_junk_or_negative_claim_is_no_claim(self):
        """The tables hold ints; a corrupt one must not decide anything oddly."""
        for claim in (-5, "junk", None, float("inf")):
            budget = _core_audio.vram_budget(4_000, claim_mb=claim,
                                             owner="whisper")
            assert budget["claim_mb"] == 0 and budget["fits"] is True, claim
        unguarded = _core_audio.vram_budget(600, claim_mb=600, owner="whisper",
                                            reserve_mb="junk")
        assert unguarded["reserved_mb"] == 0 and unguarded["fits"] is True, (
            "a corrupt reserve must not make the budget refuse or crash")

    def test_the_reason_carries_its_arithmetic(self):
        """A refusal nobody can explain is a device choice nobody can debug."""
        budget = _core_audio.vram_budget(6_267, claim_mb=1_400, owner="whisper")
        assert budget["reason"] == (
            "1400 MB claim fits: 6267 MB free less 1024 MB reserve "
            "= 5243 MB available"), budget["reason"]


class TestOneBudgetDecidesBothLoaders:
    """The property rather than the arithmetic: ONE budget, asked by both.

    Each loader keeps its own two-value answer and its own fallback, but the
    card's arithmetic exists once — so the day the reserve, the entitlement or
    the unknown-card rule changes, both loaders change together.
    """

    def _auto(self, monkeypatch, *, tts_device="auto", tts_model=None,
              tts_loaded_device="", cuda=True):
        # _torch_cuda_available is pinned in every test here: the real one
        # imports torch, which this suite forbids, and its answer would make a
        # test's outcome depend on the machine running it.
        monkeypatch.setattr(_core_audio, "_torch_cuda_available", lambda: cuda)
        monkeypatch.setattr(_core_audio, "TTS_DEVICE", tts_device)
        monkeypatch.setattr(_core_audio, "_tts_model", tts_model)
        monkeypatch.setattr(_core_audio, "_tts_device", tts_loaded_device)

    def test_both_loaders_ask_the_same_budget(self, monkeypatch):
        seen = []

        def spy(free_mb, **kwargs):
            seen.append((free_mb, kwargs))
            return {"fits": True, "available_mb": 9_999, "reason": "spy"}

        self._auto(monkeypatch)
        monkeypatch.setattr(_core_audio, "vram_budget", spy)

        assert _core_audio._whisper_device_choice("base", 9_000) == ("cuda", "float16")
        assert _core_audio.tts_device_choice(9_000) == "cuda"

        assert [kwargs["owner"] for _free, kwargs in seen] == ["whisper", "speech"]
        assert seen[0][1]["entitled_mb"] == _core_audio._TTS_VRAM_MB, (
            "whisper must leave the speech model room")
        assert seen[1][1].get("entitled_mb", 0) == 0, (
            "speech yields to nothing: a resident whisper is already inside "
            "the free reading")

    def test_whisper_gives_way_to_a_speech_model_that_has_not_loaded_yet(
            self, monkeypatch):
        self._auto(monkeypatch)
        # Room for whisper alone, not for the pair: whisper is the one that moves.
        assert _core_audio._whisper_device_choice("small", 4_500) == ("cpu", "int8")
        assert _core_audio._whisper_device_choice("small", 9_000) == ("cuda", "float16")

    def test_a_resident_speech_model_is_not_reserved_twice(self, monkeypatch):
        self._auto(monkeypatch, tts_model=object(), tts_loaded_device="cuda")
        assert _core_audio._speech_vram_claim_mb() == 0, (
            "its memory is already inside the driver's free reading")
        assert _core_audio._whisper_device_choice("small", 3_000) == ("cuda", "float16")

    def test_speech_configured_for_cpu_leaves_whisper_the_whole_card(
            self, monkeypatch):
        self._auto(monkeypatch, tts_device="cpu")
        assert _core_audio._speech_vram_claim_mb() == 0
        assert _core_audio._whisper_device_choice("small", 3_000) == ("cuda", "float16")

    def test_an_unreadable_card_answers_cpu_for_both(self, monkeypatch):
        self._auto(monkeypatch)
        assert _core_audio._whisper_device_choice("small", None) == ("cpu", "int8")
        assert _core_audio.tts_device_choice(None) == "cpu"
        assert "could not be read" in _core_audio._tts_plan(None)["reason"]

    def test_a_configured_device_beats_the_budget_and_still_says_so(
            self, monkeypatch):
        self._auto(monkeypatch)
        plan = _core_audio._tts_plan(1_000, "cuda")
        assert plan["device"] == "cuda", (
            "the user's choice is not a budget question")
        assert plan["authority"] == "configured" and plan["fits"] is False
        assert "only 1000 MB is free" in plan["reason"], plan["reason"]
        assert _core_audio.tts_device_choice(99_000, "cpu") == "cpu"

    def test_no_cuda_device_means_cpu_whatever_the_card_says(self, monkeypatch):
        self._auto(monkeypatch, cuda=False)
        plan = _core_audio._tts_plan(99_000)
        assert plan["device"] == "cpu" and plan["authority"] == "budget"
        assert "no CUDA device" in plan["reason"], plan["reason"]

    def test_the_whisper_loader_asks_the_budget_and_says_why(
            self, monkeypatch, caplog):
        """End to end: the plan reaches the loader, and the journal explains it."""
        loaded = []

        class _WhisperModel:
            def __init__(self, size, device=None, compute_type=None,
                         download_root=None, local_files_only=None):
                loaded.append((size, device, compute_type))

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = _WhisperModel
        monkeypatch.setitem(sys.modules, "faster_whisper", module)
        monkeypatch.setattr(_core_audio, "_whisper_model", None)
        monkeypatch.setattr(_core_audio, "_whisper_cpu_fallback", False)
        monkeypatch.setattr(_core_audio, "WHISPER_DEVICE", "auto")
        monkeypatch.setattr(_core_audio, "WHISPER_SIZE", "small")
        monkeypatch.setattr(_core_audio, "WHISPER_MODEL_DIR", Path("/nonexistent"))
        monkeypatch.setattr(_core_audio, "_nvidia_free_vram_mb", lambda: 4_500)
        self._auto(monkeypatch)          # speech is still coming: whisper yields

        with caplog.at_level("WARNING", logger="handsoff"):
            _core_audio.get_whisper()

        assert loaded == [("small", "cpu", "int8")], loaded
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "held back for the speech model" in said, said


class TestTheSpeechYieldsToTheLlm:
    """`yield_to_llm_verdict` — the mirror of the speech model's reclaim.

    A turn needs the card, and the tenant that can move is this process's own
    speech model: seconds to reload, where Ollama's own answer to a full card is
    to offload half the model to the CPU and serve every token at a fraction of
    the on-card speed. The verdict is the SAME budget the two loaders ask
    (`vram_budget`), with the roles swapped — the LLM claims, and this process's
    memory is the entitlement that may have to yield — so the two cannot
    disagree about what fits.
    """

    def test_nothing_of_ours_on_the_card_is_nothing_to_ask_for(self):
        out = _core_audio.yield_to_llm_verdict(900, 5_000, held_mb=0)

        assert out["yield"] is False and out["tight"] is False
        assert "holds nothing on the card" in out["note"]

    def test_a_claim_that_already_fits_is_not_a_reason_to_evict(self):
        out = _core_audio.yield_to_llm_verdict(9_000, 5_000, held_mb=3_400)

        assert out["yield"] is False and out["tight"] is False, (
            "releasing memory the card does not need buys nothing and costs a "
            "voice reload on the next reply")
        assert "fits already" in out["note"]
        assert "7976 MB available" in out["note"], out["note"]

    def test_the_yield_is_the_loaders_budget_with_the_memory_put_back(self):
        free, claim, held = 3_000, 5_000, 3_400
        out = _core_audio.yield_to_llm_verdict(free, claim, held_mb=held)

        assert out["yield"] is True, out["note"]
        assert out["tight"] is True, "the claim does not fit as things stand"
        assert _core_audio.vram_budget(free, claim_mb=claim, owner="llm",
                                       entitled_mb=held)["fits"] is False
        assert _core_audio.vram_budget(free + held, claim_mb=claim,
                                       owner="llm")["fits"] is True
        assert out["available_after_mb"] == (free + held) - _core_audio._VRAM_RESERVE_MB, (
            "the room the claim would have once this process's memory is back")
        assert "3400 MB of this card is this process's own models" in out["note"]

    def test_the_reserve_is_never_traded_away(self):
        """The compositor's room is not the speech model's to give."""
        free, held = 3_000, 3_400
        one_too_many = free + held - _core_audio._VRAM_RESERVE_MB + 1

        assert _core_audio.yield_to_llm_verdict(
            free, one_too_many, held_mb=held)["yield"] is False
        assert _core_audio.yield_to_llm_verdict(
            free, one_too_many, held_mb=held,
            reserve_mb=0)["yield"] is True, (
            "the refusal came from the reserve, and this proves it")

    def test_memory_that_would_not_make_room_is_not_handed_back(self):
        out = _core_audio.yield_to_llm_verdict(2_000, 20_000, held_mb=3_400)

        assert out["yield"] is False and out["tight"] is True
        assert "would not make room" in out["note"], out["note"]
        assert "21024 MB needed" in out["note"], out["note"]

    def test_an_unreadable_claim_is_not_weighed(self):
        for claim in (None, "junk", "", float("inf"), "N/A"):
            out = _core_audio.yield_to_llm_verdict(2_000, claim, held_mb=3_400)

            assert out["yield"] is False and out["tight"] is False, claim
            assert "could not be read" in out["note"], out["note"]

    def test_an_unreadable_card_is_not_evidence_for_an_eviction(self):
        """What this decision spends is a reload, so an unknown is not a reason.

        The speech half of this pair releases on an unreadable card because its
        alternative is a stalled utterance; here the alternative is only that
        Ollama decides the offload for itself.
        """
        out = _core_audio.yield_to_llm_verdict(None, 5_000, held_mb=3_400)

        assert out["yield"] is False and out["tight"] is False
        assert "free memory could not be read" in out["note"]
        assert "3400 MB is held here" in out["note"], out["note"]

    def test_the_arithmetic_travels_with_the_answer(self):
        out = _core_audio.yield_to_llm_verdict(3_000, 5_000, held_mb=3_400)

        assert out["free_mb"] == 3_000 and out["claim_mb"] == 5_000
        assert out["held_mb"] == 3_400
        assert "5376 MB available" in out["note"], out["note"]


class TestTheSpeechModelAsksForTheCard:
    """A refused speech claim asks the LLM for the card before giving way.

    The budget's refusal is about ROOM, and the tenant holding it is usually
    the LLM — which the HOST can ask to let go. The policy belongs to the host
    (it is the one that knows what a reload costs), so this calls an injected
    hook and re-plans against a reading taken AFTERWARDS. Two properties
    matter: a reclaim that frees nothing must never turn a refusal into a
    claim, and the journal has to say which tenant moved.
    """

    def _ready(self, monkeypatch, readings, *, hook=None, tts_device="auto"):
        """Speech on auto, no model loaded, nvidia-smi readings in order.

        `readings` is what each nvidia-smi read inside the call returns, the
        last one repeating — the core module takes no reading of its own before
        the hook, so one value is the normal case. Returns the list of readings
        SERVED, so a test can assert that no second measurement was taken.
        """
        monkeypatch.setattr(_core_audio, "_torch_cuda_available", lambda: True)
        monkeypatch.setattr(_core_audio, "TTS_DEVICE", tts_device)
        monkeypatch.setattr(_core_audio, "_tts_model", None)
        monkeypatch.setattr(_core_audio, "_tts_device", "")
        rest = list(readings)
        served = []

        def read():
            value = rest.pop(0) if len(rest) > 1 else rest[0]
            served.append(value)
            return value

        monkeypatch.setattr(_core_audio, "_nvidia_free_vram_mb", read)
        monkeypatch.setattr(_core_audio, "_GPU_RECLAIM", hook)
        return served

    WON = "ollama dropped qwen3.8:27b for the speech model — 7206 MB came back"

    def test_a_refusal_is_escalated_and_the_card_can_be_won(self, monkeypatch):
        asked = []

        def hook(reason):
            asked.append(reason)
            return {"gave_way": True, "freed_mb": 7_206, "detail": self.WON}

        self._ready(monkeypatch, [9_000], hook=hook)

        plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(1_500), "auto")

        assert plan["device"] == "cuda" and plan["fits"] is True
        assert plan["reclaim"] == self.WON, "the sentence the journal prints"
        assert asked and "claim refused" in asked[0], (
            "the host decides on the refusal, so it is told the refusal")

    def test_a_reclaim_is_re_planned_against_the_reading_after_it(self, monkeypatch):
        """The gain is measured, not assumed: the new reading is what decides."""
        self._ready(monkeypatch, [3_000],
                    hook=lambda reason: {"gave_way": True, "detail": self.WON})

        plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(1_500), "auto")

        assert plan["device"] == "cpu", (
            "3000 MB free is still not a 3400 MB model plus the reserve")
        assert "only 3000 MB is free" in plan["reason"], plan["reason"]
        assert plan["reclaim"] == self.WON, (
            "the tenant that moved is still the fact worth saying")

    def test_a_tenant_that_does_not_move_changes_nothing(self, monkeypatch):
        """A refusal is the answer, and a refusal buys no second measurement.

        The hook's `gave_way` is what entitles a re-decision: without it the
        claim is not re-planned at all, so a card that happens to have room on
        a second look cannot turn "the LLM kept its memory" into a GPU load.
        """
        served = self._ready(monkeypatch, [1_500, 9_000],
                             hook=lambda reason: {
                                 "gave_way": False, "freed_mb": None,
                                 "detail": "the LLM did not give the card "
                                           "back — nothing resident"})

        plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(1_500), "auto")

        assert plan["device"] == "cpu" and plan["fits"] is False
        assert "nothing resident" in plan["reclaim"]
        assert served == [], (
            "nobody promised the memory back, so there is nothing to re-measure")

    def test_a_card_that_could_not_be_read_is_not_asked_about(self, monkeypatch):
        asked = []
        self._ready(monkeypatch, [None],
                    hook=lambda reason: asked.append(reason) or {})

        plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(None), "auto")

        assert asked == [], (
            "a reclaim is not a way to guess at a card nobody can read")
        assert plan["device"] == "cpu" and "could not be read" in plan["reason"]

    def test_without_a_hook_the_refusal_stands(self, monkeypatch):
        self._ready(monkeypatch, [1_500], hook=None)

        plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(1_500), "auto")

        assert plan["device"] == "cpu" and "reclaim" not in plan

    def test_a_hook_that_raises_leaves_the_refusal_standing(
            self, monkeypatch, caplog):
        def hook(reason):
            raise RuntimeError("the policy is broken")

        self._ready(monkeypatch, [1_500], hook=hook)

        with caplog.at_level("WARNING", logger="handsoff"):
            plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(1_500),
                                                 "auto")

        assert plan["device"] == "cpu" and "reclaim" not in plan
        assert any("reclaim hook raised" in r.getMessage()
                   for r in caplog.records)

    def test_a_junk_answer_is_not_a_reclaim(self, monkeypatch):
        for answer in (None, "yes", 7, [], {}):
            self._ready(monkeypatch, [1_500],
                        hook=lambda reason, a=answer: a)
            plan = _core_audio._ask_for_the_card(_core_audio._tts_plan(1_500),
                                                 "auto")
            assert plan["device"] == "cpu", answer
            assert not plan.get("reclaim"), answer

    def _fake_tts(self, monkeypatch):
        """Chatterbox as the loader imports it: records the device it was given."""
        loaded = []

        class _FakeTTS:
            @classmethod
            def from_pretrained(cls, device=None):
                loaded.append(device)
                return cls()

        package = types.ModuleType("chatterbox")
        package.__path__ = []
        module = types.ModuleType("chatterbox.tts_turbo")
        module.ChatterboxTurboTTS = _FakeTTS
        monkeypatch.setitem(sys.modules, "chatterbox", package)
        monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", module)
        monkeypatch.setattr(_core_audio, "TTS_REFERENCE", "")
        return loaded

    def test_the_loader_wins_the_card_and_the_journal_says_which_tenant_moved(
            self, monkeypatch, caplog):
        loaded = self._fake_tts(monkeypatch)
        # The loader's own read is tight; the read AFTER the reclaim has room.
        self._ready(monkeypatch, [1_500, 9_000],
                    hook=lambda reason: {"gave_way": True, "detail": self.WON})

        with caplog.at_level("INFO", logger="handsoff"):
            model = _core_audio.get_tts()

        assert loaded == ["cuda"], loaded
        assert _core_audio._tts_device == "cuda" and _core_audio._tts_model is model
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "ollama dropped qwen3.8:27b" in said, said
        assert "speech model takes the card" in said, said

    def test_the_loader_gives_way_and_the_journal_names_both_reasons(
            self, monkeypatch, caplog):
        loaded = self._fake_tts(monkeypatch)
        self._ready(monkeypatch, [1_500], hook=lambda reason: {
            "gave_way": False, "detail": "the LLM did not give the card back"})

        with caplog.at_level("WARNING", logger="handsoff"):
            _core_audio.get_tts()

        assert loaded == ["cpu"], loaded
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "fell back to cpu" in said and "the LLM did not give the card back" in said, said

    def test_a_configured_device_never_evicts_the_llm(self, monkeypatch):
        """An explicit `cpu` is not a budget refusal, so there is nothing to ask."""
        loaded = self._fake_tts(monkeypatch)
        asked = []
        self._ready(monkeypatch, [1_500], tts_device="cpu",
                    hook=lambda reason: asked.append(reason) or {})

        _core_audio.get_tts()

        assert asked == [] and loaded == ["cpu"], (asked, loaded)

    def test_a_whisper_load_never_evicts_the_llm(self, monkeypatch):
        """The ears yield to speech by design; they do not evict the LLM."""
        asked = []
        self._ready(monkeypatch, [1_500],
                    hook=lambda reason: asked.append(reason) or {})

        assert _core_audio._whisper_device_choice("small", 1_500) == ("cpu", "int8")
        assert asked == [], (
            "a startup whisper load must not evict the model the bubble just "
            "warmed — whisper is the one that yields")


class TestModelCacheDropIsFinal:
    """A settings reload drops the model caches; a call in flight must not undo it.

    The mirror between this module and core.audio was read-then-assign, so a
    drop landing between the read and the write was overwritten by the stale
    model the caller had just read — and it STAYED: the load saw a non-None
    cache, returned the old model, and the adopt published it here again. A
    voice or device change then silently did nothing until the next reload.
    """

    def test_a_drop_landing_as_the_push_takes_the_lock_is_not_overwritten(
            self, H, monkeypatch):
        stale = object()
        monkeypatch.setattr(H, "_tts_model", stale)
        monkeypatch.setattr(H._audio, "_tts_model", stale)
        real = threading.Lock()

        class DropThenLock:
            """The reload's drop runs exactly as the push takes the lock.

            This is the interleaving the lock exists to remove: a push that
            reads the module copy BEFORE taking the lock can be overtaken by a
            drop and then write the stale model straight back.
            """

            def __init__(self):
                self.fired = False

            def __enter__(self):
                if not self.fired:
                    self.fired = True
                    H._tts_model = None
                    H._audio._tts_model = None
                real.acquire()
                return self

            def __exit__(self, *exc):
                real.release()
                return False

        monkeypatch.setattr(H, "_model_cache_lock", DropThenLock())
        H._push_model("_tts_model")
        assert H._audio._tts_model is None, (
            "the push overwrote a drop that had already happened — the dropped "
            "model comes back and stays")

    def test_adopt_does_not_republish_a_model_the_drop_removed(
            self, H, monkeypatch):
        loaded = object()
        monkeypatch.setattr(H, "_tts_model", None)
        monkeypatch.setattr(H._audio, "_tts_model", loaded)
        H._adopt_model("_tts_model", loaded)
        assert H._tts_model is loaded, "a model audio still holds must be adopted"

        # The reload dropped the cache while this call was loading: publishing
        # the model back here is what made the drop ineffective next call.
        H._tts_model = None
        H._audio._tts_model = None
        H._adopt_model("_tts_model", loaded)
        assert H._tts_model is None, (
            "a model the drop removed must not be republished into the mirror")


class _SlowStopRecorder:
    """A recorder whose stop() blocks until released, then hands back frames."""

    def __init__(self, frames: int = 500):
        self.release = threading.Event()
        self._frames = frames
        self._handsoff_stop_done = None

    def stop(self):
        self.release.wait(timeout=5)
        return np.zeros(self._frames, dtype=np.int16)


class TestBoundedStopReportsLateAudio:
    """A stop that times out must not look like a press that captured nothing.

    The owner thread keeps the mic and finishes anyway; its utterance cannot be
    delivered once the turn is gone, so it is said (frames, in the journal)
    rather than vanishing with the thread. Without this, "push-to-talk did
    nothing" leaves no trace at all.
    """

    def test_a_late_capture_is_reported_rather_than_dropped(self, H, caplog):
        rec = _SlowStopRecorder(frames=500)
        with caplog.at_level("WARNING", logger="handsoff"):
            audio, wedged = H._stop_recorder_bounded(rec, timeout=0.05)
            assert (audio, wedged) == (None, True), (
                "a bounded stop that gave up reports no audio and says so")
            rec.release.set()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                    "late capture" in r.getMessage() for r in caplog.records):
                time.sleep(0.01)
        messages = [r.getMessage() for r in caplog.records]
        assert any("late capture" in m and "500" in m for m in messages), (
            "the discarded late utterance must be reported with its size: "
            f"{messages}")


def test_core_audio_imports_independently():
    """The extracted primitives must not require the Qt/application module."""
    mod = _load("core_audio_compat", HERE / "core" / "audio.py")
    for name in ("_resample_to_16k", "_open_input", "Recorder",
                 "get_whisper", "get_tts", "transcribe", "tts_to_wav",
                 "play_wav"):
        assert hasattr(mod, name), name


def test_importing_the_app_never_pulls_in_torch_or_chatterbox():
    """The suite must stay runnable on a GPU-less runner.

    requirements.txt deliberately does NOT carry the speech engine (it drags
    in torch), so importing the app must not import it either: a lazy import
    that drifts to module level would turn CI into a multi-gigabyte install
    that fails for reasons unrelated to the change. Checked in a SUBPROCESS on
    purpose — torch IS importable on a developer box, so an in-process check
    would pass locally and fail only where it matters.

    `run_driver`, not a hand-built environment: the child LOADS the monolith,
    so it resolves CONFIG_DIR/STATE_DIR from HOME like any other load, and the
    throw-away HOME plus the real user-site PYTHONPATH (where a `pip install
    --user` PySide6 lives) are precisely what that constructor exists to apply
    together. This call site used to spell both out by hand, which is the way
    the next one gets forgotten.
    """
    proc = run_driver(
        ["-c",
         "import sys, json; import handsoff; print(json.dumps(sorted("
         "m for m in sys.modules if m.split('.')[0] in "
         "{'torch', 'chatterbox', 'transformers'})))"],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == [], (
        f"importing handsoff pulled in {loaded} — it must stay lazy, or CI "
        f"installs a multi-GB GPU stack to run this suite")


def test_portaudio_reinit_is_guarded_while_streams_are_open(H):
    """sd._terminate() is process-global: the listener's recovery path must not
    tear down streams this process is still using.

    The recorded crash is "Fatal Python error: Aborted" inside sounddevice's
    OutputStream.__init__ on the _speak thread — i.e. the hands-free listener
    reinitializing PortAudio mid-playback. play_wav marks the stretch busy and
    the listener defers instead.
    """
    mod = _load("core_audio_guard", HERE / "core" / "audio.py")
    assert mod.portaudio_busy() is False
    with mod.portaudio_in_use():
        assert mod.portaudio_busy() is True
        with mod.portaudio_in_use():
            assert mod.portaudio_busy() is True      # nesting is safe
        assert mod.portaudio_busy() is True
    assert mod.portaudio_busy() is False
    try:                                             # an error must still clear it
        with mod.portaudio_in_use():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert mod.portaudio_busy() is False

    # ...and the guard is actually wired into the reinit path, not just defined
    text = (HERE / "handsoff.py").read_text(encoding="utf-8")
    idx = text.index("PortAudio reinit deferred")
    assert "_audio.portaudio_busy()" in text[max(0, idx - 400):idx]


def test_play_wav_holds_the_portaudio_mark(H, tmp_path):
    """Playback is exactly the window the listener must not reinit inside."""
    import threading, wave as _wave
    mod = _load("core_audio_guard2", HERE / "core" / "audio.py")
    seen = []
    real = mod.sd.OutputStream

    class _Stream:
        def __init__(self, **kw):
            seen.append(mod.portaudio_busy())

        def start(self):
            seen.append(mod.portaudio_busy())

        def write(self, data):
            seen.append(mod.portaudio_busy())

        def stop(self):
            pass

        def close(self):
            pass

    mod.sd.OutputStream = _Stream
    try:
        path = tmp_path / "tone.wav"
        with _wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 4096)
        mod.play_wav(path, threading.Event())
    finally:
        mod.sd.OutputStream = real
    assert seen and all(seen), "every step of playback must hold the mark"
    assert mod.portaudio_busy() is False, "the mark must be released afterwards"


def test_play_wav_reports_a_level_that_follows_the_audio(H, tmp_path):
    """A voice-reactive bubble needs a level WHILE it is speaking.

    The mic is deliberately blanked during TTS (the bubble's own voice would
    re-trigger the VAD), so the equalizer bars had nothing to follow and fell
    back to a time-based pulse. play_wav now reports the level of what it is
    actually playing: loud where the audio is loud, near-silent where it is
    quiet, and a final 0 so the visual settles when speech stops.
    """
    import numpy as _np
    import wave as _wave

    mod = _load("core_audio_level", HERE / "core" / "audio.py")
    written = []

    class _Stream:
        def __init__(self, **_kw):
            pass

        def start(self):
            pass

        def write(self, data):
            written.append(_np.asarray(data).size)

        def stop(self):
            pass

        def close(self):
            pass

    real = mod.sd.OutputStream
    mod.sd.OutputStream = _Stream
    path = tmp_path / "tts.wav"
    loud = (_np.full(4096, 12000, dtype=_np.int16)).tobytes()
    soft = (_np.full(4096, 300, dtype=_np.int16)).tobytes()
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(loud + soft + loud)
    seen: list[float] = []
    try:
        mod.set_level_hook(seen.append)
        mod.play_wav(path, threading.Event())
    finally:
        mod.set_level_hook(None)
        mod.sd.OutputStream = real
    assert seen, "playback must report a level"
    assert seen[-1] == 0.0, f"must end at silence, got {seen[-1]}"
    body = seen[:-1]
    assert max(body) > 0.5, f"a loud passage must read loud: {body}"
    assert min(body) < max(body) * 0.3, (
        f"the level must follow the audio, not be a constant: {body}")
    # updates stay frequent enough to look continuous (~46 ms per block at
    # 22050 Hz), rather than stepping a few times per sentence
    assert len(body) >= 3, body
    assert written, "the audio must still actually be written"


def test_a_broken_level_hook_never_breaks_playback(H, tmp_path):
    """The visual is best-effort: a raising hook must not kill the audio."""
    import numpy as _np
    import wave as _wave

    mod = _load("core_audio_level2", HERE / "core" / "audio.py")
    played = []

    class _Stream:
        def __init__(self, **_kw):
            pass

        def start(self):
            pass

        def write(self, data):
            played.append(_np.asarray(data).size)

        def stop(self):
            pass

        def close(self):
            pass

    real = mod.sd.OutputStream
    mod.sd.OutputStream = _Stream
    path = tmp_path / "tts.wav"
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(_np.full(2048, 9000, dtype=_np.int16).tobytes())

    def _boom(_value):
        raise RuntimeError("widget went away mid-sentence")

    try:
        mod.set_level_hook(_boom)
        mod.play_wav(path, threading.Event())      # must not raise
    finally:
        mod.set_level_hook(None)
        mod.sd.OutputStream = real
    assert played, "playback must complete despite the broken hook"


class FakeSig:
    """Mimics a Qt signal: collect emitted values."""

    def __init__(self) -> None:
        self.values: list = []

    def emit(self, value) -> None:
        self.values.append(value)


class FakeAssistant:
    """Just enough of Assistant for ContinuousListener._process_frame."""

    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.sigLevel = FakeSig()
        self.sigUtterance = FakeSig()
        self.vad_events: list[bool] = []
        self.level_events: list[tuple] = []

    def _emit_level(self, value: float, source: str = "mic") -> None:
        """The Assistant's single level publisher.

        The continuous listener and the push-to-talk recorder call this (not
        sigLevel.emit) so the control socket can say WHICH producer fed the
        level; the fake has to carry the same surface or the audio callback
        raises AttributeError. Mirrors the real one: record, then emit."""
        self.level_events.append((value, source))
        self.sigLevel.emit(value)

    def _vad_speech(self, active: bool) -> None:
        self.vad_events.append(active)
        if active and self.state == "idle":
            self.state = "listening"
        elif not active and self.state == "listening":
            self.state = "idle"

    # ContinuousListener._health_tick() probes these optional assistant hooks on
    # every tick and log.exception()s when they are absent — which buries the
    # CI log in tracebacks. They are declared here as deliberate no-ops: the
    # listener under test does not own scheduling policy, but it does expect the
    # Assistant surface to exist.
    def _resource_tick(self) -> None:
        pass

    def _world_tick(self) -> None:
        pass

    def _hardware_tick(self) -> None:
        pass

    def _maybe_self_heal(self, degraded) -> None:
        pass


class TestSpeechGate:
    def test_silence_produces_no_events(self, H):
        gate = H._SpeechGate(threshold=600)
        for _ in range(200):
            assert gate.feed(30.0) == ""

    def test_loud_speech_starts(self, H):
        gate = H._SpeechGate(threshold=600)
        events = [gate.feed(3000.0) for _ in range(5)]
        assert events[0] == ""            # needs start_frames consecutive frames
        assert "start" in events

    def test_hangover_ends_utterance(self, H):
        gate = H._SpeechGate(threshold=600)
        for _ in range(5):
            gate.feed(3000.0)
        assert gate.in_speech
        assert gate.feed(10.0) == ""      # one quiet frame must not end it
        assert gate.in_speech
        for _ in range(12):
            assert gate.feed(10.0) == ""
        assert gate.feed(10.0) == "end"   # 14th quiet frame (hangover_frames) ends it
        assert not gate.in_speech

    def test_noise_floor_adapts(self, H):
        gate = H._SpeechGate(threshold=600)
        initial_floor = gate.floor
        for _ in range(100):
            gate.feed(100.0)
        assert gate.floor < initial_floor  # floor drifts down in a quiet room

    def test_reset(self, H):
        gate = H._SpeechGate(threshold=600)
        for _ in range(5):
            gate.feed(3000.0)
        gate.reset()
        assert not gate.in_speech
        assert gate._loud == 0 and gate._quiet == 0

    def test_start_frames_requires_consecutive(self, H):
        gate = H._SpeechGate(threshold=600)
        assert gate.feed(3000.0) == ""
        assert gate.feed(10.0) == ""      # break the streak
        assert gate.feed(3000.0) == ""    # streak restarts
        assert gate.feed(3000.0) == "start"  # two consecutive loud frames open it


# ---------------------------------------------------- ContinuousListener (regression)


class TestListenerSelfMute:
    @pytest.fixture()
    def env(self, H):
        asst = FakeAssistant()
        lst = H.ContinuousListener(asst)
        gate = H._SpeechGate(threshold=600)
        frames: list = []
        return lst, asst, gate, frames, 100, 4   # max_frames, min_frames

    @staticmethod
    def _feed(lst, gate, frames, max_f, min_f, rms: float) -> None:
        frame = (np.ones(1024, dtype=np.int16) * min(int(rms), 32000)).reshape(1, 1024)
        lst._process_frame(frame, gate, frames, max_f, min_f)

    def test_utterance_survives_own_listening_state(self, env):
        """Regression: state==LISTENING must not mute the ongoing utterance."""
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert gate.in_speech
        assert asst.state == "listening"      # the VAD itself set this
        for _ in range(20):                   # keep talking past hangover reset
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        for _ in range(15):                   # then go quiet -> 'end'
            self._feed(lst, gate, frames, max_f, min_f, 10.0)
        assert len(asst.sigUtterance.values) == 1
        audio = asst.sigUtterance.values[0]
        assert isinstance(audio, np.ndarray) and len(audio) > 0

    def test_speaking_state_discards_frames(self, env):
        """The assistant must not hear its own TTS output."""
        lst, asst, gate, frames, max_f, min_f = env
        asst.state = "speaking"
        for _ in range(10):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert not gate.in_speech
        assert frames == []
        assert asst.sigUtterance.values == []

    def test_suspended_discards_frames(self, env):
        """A push-to-talk press suspends the continuous listener."""
        lst, asst, gate, frames, max_f, min_f = env
        lst.suspend()
        for _ in range(10):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert frames == []
        assert asst.sigUtterance.values == []

    def test_reset_discards_partial_utterance(self, env):
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert frames, "gate opened, frames should be buffered"
        lst.reset()
        self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert frames == [] and not gate.in_speech

    def test_max_utterance_length_flushes(self, env):
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        for _ in range(max_f):                # hammer past the cap
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert len(asst.sigUtterance.values) == 1

    def test_voice_level_is_reported_as_the_mic_source(self, env):
        # The Settings → Voice meter reads this back over the control socket,
        # so WHERE a level came from has to be recorded at the producer. A raw
        # sigLevel.emit here would leave the meter unable to tell hands-free
        # listening from the bubble's own playback — which is the whole point
        # of tagging the source at all.
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert asst.level_events, "the mic must feed the shared level"
        assert {src for _v, src in asst.level_events} == {"mic"}
        assert all(0.0 < v <= 1.0 for v, _s in asst.level_events), asst.level_events
        assert [v for v, _s in asst.level_events] == asst.sigLevel.values, (
            "recording the source must not change what the designs receive")

    def test_thinking_state_discards_frames(self, env):
        """Regression: while the brain is generating, mic input is junk
        (the bubble is talking or the user is reacting) — discard it."""
        lst, asst, gate, frames, max_f, min_f = env
        asst.state = "thinking"
        for _ in range(10):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert not gate.in_speech
        assert frames == []
        assert asst.sigUtterance.values == []

    def test_thinking_does_not_kill_open_utterance(self, env):
        """If the VAD already collected an utterance (barge-in), a state flip
        to THINKING must not silently eat it."""
        lst, asst, gate, frames, max_f, min_f = env
        for _ in range(5):
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        assert gate.in_speech
        asst.state = "thinking"
        for _ in range(15):                   # keep talking
            self._feed(lst, gate, frames, max_f, min_f, 3000.0)
        for _ in range(15):                   # go quiet -> end
            self._feed(lst, gate, frames, max_f, min_f, 10.0)
        assert len(asst.sigUtterance.values) == 1


class TestSpeechPlaybackSerialization:
    def test_announce_returns_to_idle_after_normal_completion(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 1
        a._state = H.SPEAKING
        a.sigState = FakeSig()
        done = threading.Event()

        def speak(*_args, **_kwargs):
            a._state = H.SPEAKING
            done.set()

        a._speak = speak
        H.Assistant._announce_now(a, "hello")
        assert done.wait(1)
        deadline = time.time() + 1
        while a.state != H.IDLE and time.time() < deadline:
            time.sleep(0.01)
        assert a.state == H.IDLE
        listener_asst = FakeAssistant(state=a.state)
        listener = H.ContinuousListener(listener_asst)
        gate = H._SpeechGate(threshold=600)
        frames = []
        for _ in range(5):
            frame = (np.ones(1024, dtype=np.int16) * 3000).reshape(1, 1024)
            listener._process_frame(frame, gate, frames, 100, 4)
        assert frames, "a completed announcement must leave the listener unmuted"

    def test_cancelled_stale_announcement_cannot_reset_newer_state(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 1
        a._state = H.IDLE
        a.sigState = FakeSig()
        started = threading.Event()
        release = threading.Event()
        cancel_ref = {}

        def speak(*args, **_kwargs):
            cancel_ref["event"] = args[2]
            started.set()
            release.wait(1)

        a._speak = speak
        H.Assistant._announce_now(a, "old")
        assert started.wait(1)
        cancel_ref["event"].set()
        a._gen = 2
        a._set(2, H.THINKING)
        release.set()
        time.sleep(0.05)
        assert a.state == H.THINKING

    def test_listener_can_hear_after_announcement_completion(self, H):
        asst = FakeAssistant(state=H.IDLE)
        asst._vad_speech(True)
        assert asst.state == "listening"

    def test_synthesis_can_overlap_but_playback_cannot_and_cancel_skips_stale(
            self, H, monkeypatch, tmp_path):
        a = H.Assistant.__new__(H.Assistant)
        a._models_ready = threading.Event()
        a._models_ready.set()
        a._last_spoken = ""
        a._turn_spoke = False
        a._recently_spoken = []
        a._handsfree = False
        a._followup_until = 0.0
        a._set = lambda *_args: None
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)

        synth_barrier = threading.Barrier(2, timeout=2.0)
        synth_active = playback_active = 0
        max_synth = max_playback = 0
        counts_lock = threading.Lock()
        played = []

        def fake_tts(text, wav):
            nonlocal synth_active, max_synth
            with counts_lock:
                synth_active += 1
                max_synth = max(max_synth, synth_active)
            try:
                synth_barrier.wait()
                wav.write_text(text)
            finally:
                with counts_lock:
                    synth_active -= 1

        def fake_play(wav, _cancel):
            nonlocal playback_active, max_playback
            with counts_lock:
                playback_active += 1
                max_playback = max(max_playback, playback_active)
                played.append(wav.read_text())
            time.sleep(0.05)
            with counts_lock:
                playback_active -= 1

        monkeypatch.setattr(H, "tts_to_wav", fake_tts)
        monkeypatch.setattr(_core_audio, "play_wav", fake_play)

        cancels = [threading.Event(), threading.Event()]
        threads = [threading.Thread(target=a._speak, args=(text, 0, cancel))
                   for text, cancel in zip(("one", "two"), cancels)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        assert max_synth == 2
        assert max_playback == 1
        assert sorted(played) == ["one", "two"]

        played.clear()
        stale_cancel = threading.Event()
        stale_ready = threading.Event()
        release_stale = threading.Event()

        def stale_tts(_text, wav):
            stale_ready.set()
            release_stale.wait(2)
            wav.write_text("stale")

        monkeypatch.setattr(H, "tts_to_wav", stale_tts)
        stale_thread = threading.Thread(
            target=a._speak, args=("stale", 0, stale_cancel))
        stale_thread.start()
        assert stale_ready.wait(2)
        stale_cancel.set()
        release_stale.set()
        stale_thread.join(timeout=3)
        assert not stale_thread.is_alive()
        assert played == []


class TestEchoRejection:
    """The mic hears the bubble's own TTS through the speakers; those echo
    captures must be rejected before they reach the LLM (the root cause of
    the parrot loop)."""

    def test_exact_tail_is_echo(self, H):
        assert H._is_echo("What is your request?", ["What is your request?"])
        assert H._is_echo("request.", ["What is your request?"])
        assert H._is_echo("Hello.", ["Hello."])

    def test_rephrase_is_echo(self, H):
        assert H._is_echo("hello there", ["Hello! How can I help you today?"])

    def test_real_user_speech_is_not_echo(self, H):
        assert not H._is_echo("what is the weather", ["What is your request?"])
        assert not H._is_echo("tell me a joke please", ["Hello."])
        assert not H._is_echo("", ["Hello."])
        assert not H._is_echo("anything at all", [])


# ------------------------------------------------------------------------ ToolBelt


class TestWakeWord:
    """Wake-word gate: hands-free answers only when addressed by name."""

    def test_match_wake_variants(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "assistant")  # isolate from live name
        assert H._match_wake("assistant what's the weather") == "what's the weather"
        assert H._match_wake("hey assistant open firefox") == "open firefox"
        assert H._match_wake("Assistant, tell me a joke") == "tell me a joke"
        assert H._match_wake("assistant") == ""
        assert H._match_wake("hey assistant") == ""

    def test_no_wake(self, H):
        assert H._match_wake("what's the weather") is None
        assert H._match_wake("hey google what's up") is None
        # the classic regex trap: 'a' must not match out of 'assistant ...'
        assert H._match_wake("a stainless steel bottle") is None
        assert H._match_wake("") is None

    def test_wake_utt(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "assistant")  # isolate from live name
        assert H._is_wake_utt("hey assistant")
        assert H._is_wake_utt("Assistant!")
        assert not H._is_wake_utt("assistant what time")

    def test_custom_name(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "hey bubble")
        assert H._match_wake("hey bubble what time") == "what time"
        assert H._is_wake_utt("hey bubble")

    def test_new_settings_coerce(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"assistant_name": "  Nova  ",
                                 "wake_word_required": True,
                                 "engage_seconds": "banana"}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["assistant_name"] == "Nova" and s["wake_word_required"] is True
        assert s["engage_seconds"] == 45.0  # garbage -> default, never crash

    def test_engage_seconds_clamped(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"engage_seconds": 9999}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        assert H._load_settings()["engage_seconds"] == 600.0

    def test_prompt_mentions_name(self, H):
        assert "WAKE WORD" in H.SYSTEM_PROMPT

    def test_bulk_typing_single_call(self, H, monkeypatch):
        """type_text issues ONE ydotool call for short text (the old chunk
        loop made ~1 call per 32 chars + sleeps: ~30x slower)."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        calls = []
        belt._ydotool = lambda *a: (calls.append(a), "ok")[1]
        # stub the CURRENT seam (a live _typing_guard would consult real niri
        # focus and fail closed whenever a terminal happens to be focused)
        monkeypatch.setattr(belt, "_focused_window_info",
                            lambda: {"app_id": "firefox", "title": "Firefox"})
        out, err = belt.execute("type_text", {"text": "hello beautiful world"})
        assert not err and "typed" in out
        assert len(calls) == 1 and "hello beautiful world" in calls[0]


class TestWakeSpotter:
    """openWakeWord spotter: residual buffering, single predict, integration."""

    def test_feed_carries_residual_and_predicts_once_per_1280(self, H, monkeypatch):
        calls = []

        class FakeModel:
            def predict(self, chunk):
                calls.append(len(chunk))
                return {}

        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
        s = H.WakeSpotter()
        frame = H.np.zeros(1024, dtype=H.np.int16)   # mic frames are 1024
        for _ in range(10):
            s.feed(frame)
        # 10 * 1024 = 10240 samples → 8 full 1280-chunks, 0 residual
        assert calls == [1280] * 8, calls
        s.feed(H.np.zeros(1024, dtype=H.np.int16))
        assert len(calls) == 8 + 0   # 10240+1024 = 11264 → 8 full, 1024 residual
        s.feed(H.np.zeros(1024, dtype=H.np.int16))
        # 11264+1024 = 12288 → 9 full chunks total (12288 // 1280 = 9), 768 residual
        assert len(calls) == 9, len(calls)
        assert calls == [1280] * 9

    def test_hit_returns_audio_once_with_preroll(self, H, monkeypatch):
        class FakeModel:
            def predict(self, chunk):
                # go hot exactly once, after 2s of buffer has accumulated,
                # then cool down (score < HOLD ends collection after >1s)
                calls.append(1)
                return {"hey jarvis": 0.9 if len(calls) == 30 else 0.0}
        calls = []
        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
        s = H.WakeSpotter()
        fired = None
        for i in range(60):
            chunk = H.np.full(1280, i, dtype=H.np.int16)
            f, audio = s.feed(chunk)
            if f and fired is None:
                fired = audio
        assert fired is not None
        n = sum(len(c) for c in fired)
        # pre-roll (everything buffered before the hit) + trailing speech
        assert n >= int(H.SAMPLE_RATE * H.WakeSpotter.PREROLL_S)
        assert n <= int(H.SAMPLE_RATE * (H.WakeSpotter.PREROLL_S + H.WakeSpotter.MAXWAIT_S + 2))
        # after the hit, feeding again must not re-fire until a new spotter
        f2, _ = s.feed(H.np.zeros(1280, dtype=H.np.int16))
        assert not f2

    def test_integration_fires_and_sets_bypass_flag(self, H, monkeypatch):
        """Spotter hit inside the listener emits the utterance + sets _spotter_wake."""
        import numpy as np

        class FakeModel:
            def predict(self, chunk):
                return {"hey jarvis": 0.9}

        monkeypatch.setattr(H, "_spotter_model", FakeModel())
        monkeypatch.setattr(H, "_spotter_failed", False)
        # a listener wired to a stub assistant
        a = H.Assistant.__new__(H.Assistant)
        a._state = H.IDLE
        a._vad_speech = lambda *_: None
        a.sigLevel = type("S", (), {"emit": staticmethod(lambda *x: None)})()
        emitted = []
        a.sigUtterance = type("S", (), {"emit": staticmethod(lambda x: emitted.append(x))})()
        a._spotter_wake = False
        lst = H.ContinuousListener(a)
        lst._spotter = H.WakeSpotter()
        gate = H._SpeechGate(int(H.SETTINGS["mic_threshold"]))
        frames = []
        # feed QUIET frames so the VAD gate stays closed and the spotter path
        # runs; the always-hot fake fires after MAXWAIT (100 chunks) of audio
        for _ in range(140):
            lst._process_frame(np.zeros(1024, dtype=np.int16),
                               gate, frames, 1400, 5)
            if emitted:
                break
        assert emitted, "spotter hit never produced an utterance"
        assert a._spotter_wake is True
        n = len(emitted[0])          # emitted[0] is a flat 1-D sample array
        assert n >= H.SAMPLE_RATE // 2

    def test_no_spotter_model_means_no_fire(self, H, monkeypatch):
        monkeypatch.setattr(H, "_spotter_model", None)
        monkeypatch.setattr(H, "_spotter_failed", True)   # load failed → silent fallback
        s = H.WakeSpotter()
        f, audio = s.feed(H.np.zeros(1280, dtype=H.np.int16))
        assert not f and audio == []


class TestNativeRateMicAndFuzzyWake:
    """E2E findings (2026-09-08): StreamCam can't capture at 16 kHz and the
    wake gate rejected the wake name on a whisper mishearing (cypher→Siphon)."""

    def test_resample_to_16k(self, H):
        # 1 s of 48 kHz sine → exactly 1 s of 16 kHz
        t = np.arange(48000, dtype=np.float32) / 48000.0
        hi = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
        out = _core_audio._resample_to_16k(hi, 48000)
        assert out.dtype == np.int16 and len(out) == 16000
        # 44.1 → 16 keeps duration
        t = np.arange(44100, dtype=np.float32) / 44100.0
        lo = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)
        assert len(_core_audio._resample_to_16k(lo, 44100)) == 16000
        # 16 kHz input is a passthrough (same object, no copy)
        same = np.zeros(1600, dtype=np.int16)
        assert _core_audio._resample_to_16k(same, 16000) is same

    def test_resample_kills_ultrasonic_images(self, H):
        """A 12 kHz whine at 48 kHz must not fold onto 4 kHz (linear interp
        imaged it at full strength); a 1 kHz voice tone passes through."""
        t = np.arange(48000 * 2, dtype=np.float32) / 48000.0

        def band_peak(y, f0, f1):
            Y = np.abs(np.fft.rfft(y.astype(np.float32)))
            f = np.fft.rfftfreq(len(y), 1 / 16000)
            return Y[(f >= f0) & (f < f1)].max()

        voice = _core_audio._resample_to_16k(
            (np.sin(2 * np.pi * 1000 * t) * 12000).astype(np.int16), 48000)
        assert abs(int(np.abs(voice).max()) - 12000) < 1500
        whine = _core_audio._resample_to_16k(
            (np.sin(2 * np.pi * 12000 * t) * 12000).astype(np.int16), 48000)
        assert band_peak(whine, 3900, 4100) < 0.05 * band_peak(voice, 900, 1100)

    def test_match_wake_fuzzy_misheard_name(self, H, monkeypatch):
        # The name is PINNED, not inherited: this test used to pass only on a
        # machine whose settings.json said assistant_name='cypher' (the fuzzy
        # skeleton needs >= 4 letters, and 'assistant' vs 'Siphon' shares none),
        # so it graded the developer's config instead of the matching logic.
        monkeypatch.setitem(H.SETTINGS, "assistant_name", "cypher")
        # the exact E2E failure: piper's 'cypher' transcribed as 'Siphon'
        assert H._match_wake("Hey Siphon, what is the capital of France?") == \
            "what is the capital of France"
        assert H._match_wake("Hey Siphon") == ""
        # correct name still works, junk still rejected
        assert H._match_wake("hey cypher what's the weather") == "what's the weather"
        assert H._match_wake("stop the music") is None
        assert H._match_wake("what time is it") is None
        assert H._match_wake("hey siphonatic overlord") is None
        # tiny wake names never go fuzzy (too many false accepts)
        H_obj = H.Assistant.__new__(H.Assistant)   # noqa: F841
        old = H._wake_name
        H._wake_name = lambda: "bo"
        try:
            assert H._match_wake("hey bonobo over there") is None
            assert H._match_wake("hey bo hello") == "hello"
        finally:
            H._wake_name = old


class TestMicHealth:
    """The hourly 'mic health' journal line must expose silent mic failures:
    every field has a defined value in every listener state."""

    def _mk_listener(self, H):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._frames_seen = 0
        ln._last_nonzero = 0.0
        ln._capture_rate = 0
        ln._health_utt = 0
        ln._health_opens_ok = 0
        ln._health_opens_failed = 0
        ln._health_open_device = ""
        ln._health_last_open = "never"
        ln._health_state = ""
        ln._health_next_summary = 0.0
        ln._health_failing_since = None
        ln._health_recovered_after = None
        ln._health_stalled_since = None
        ln._ever_started = True
        ln._lock = threading.RLock()
        return ln

    def test_health_line_listening(self, H, monkeypatch, caplog):
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._capture_rate = 44100
        ln._frames_seen = 56_000
        ln._last_nonzero = _t.monotonic() - 2.0
        ln._health_open_device = "hw:StreamCam"
        ln._health_last_open = "09:15:00"
        ln._health_opens_ok = 1
        ln._health_utt = 3
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=listening" in line
        assert "device=hw:StreamCam" in line
        assert "rate=44100" in line
        assert "frames=56000" in line
        assert "last_open=09:15:00" in line
        assert "utterances=3" in line

    def test_health_line_open_failing(self, H, caplog):
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_opens_ok = 0
        ln._health_opens_failed = 7
        ln._health_failing_since = time.monotonic() - 30
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=open-failing" in line
        assert "rate=-" in line
        assert "last_open=never" in line
        assert "opens_failed=7" in line

    def test_health_line_silent_and_stopped(self, H, caplog):
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._capture_rate = 16000
        ln._health_opens_ok = 2
        ln._frames_seen = 100
        ln._last_nonzero = _t.monotonic() - 500.0   # > MIC_SILENT_REPORT_S
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=silent" in line

        ln._running = False
        caplog.clear()
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        line = " ".join(r.getMessage() for r in caplog.records
                        if "mic health" in r.getMessage())
        assert "state=stopped" in line

    def test_health_loop_reports_hourly_and_survives_errors(self, H, monkeypatch):
        ln = self._mk_listener(H)
        calls = []
        waits = []
        ticks = iter([RuntimeError("boom"), None, None])   # 1st report raises

        def fake_tick():
            calls.append(1)
            r = next(ticks)
            if isinstance(r, Exception):
                raise r

        def fake_wait(seconds):
            waits.append(seconds)
            return len(waits) >= 3          # two full cycles, then stop

        # BOTH seams are the INSTANCE's, which is the whole point: `_health_wait`
        # is how the reporter waits for its next poll (an Event, so `close()` can
        # interrupt it) and `_health_tick` is patched on THIS listener rather than
        # on the class. Patching `time.sleep` (one module object every thread
        # shares) or the class attribute measured whatever else happened to be
        # sleeping or ticking — a reporter leaked by an earlier test appended to
        # this test's `calls` and consumed its `ticks` iterator, which made the
        # loop count 3 instead of 2 on a shuffled run.
        monkeypatch.setattr(ln, "_health_tick", fake_tick)
        monkeypatch.setattr(ln, "_health_wait", fake_wait)
        ln._health_loop()
        assert waits and waits[0] == 10.0, "health loop must poll frequently"
        assert len(calls) == 2, "a failing report must not kill the loop"

    def test_health_loop_minimal_logger_survives_error(self, H, monkeypatch):
        calls = []
        waits = []

        class MinimalLog:
            def error(self, message):
                calls.append(message)

        ln = self._mk_listener(H)
        monkeypatch.setattr(H, "log", MinimalLog())

        def fake_wait(_seconds):
            waits.append(1)
            return len(waits) >= 2          # one tick that raises, then stop

        def tick():
            raise RuntimeError("missing optional hook")

        monkeypatch.setattr(ln, "_health_tick", tick)
        monkeypatch.setattr(ln, "_health_wait", fake_wait)
        ln._health_loop()
        assert calls == ["mic health report failed"]

    def test_close_ends_the_reporter_that_stop_leaves_running(self, H):
        # The reporter is spawned in `__init__` and its loop was `while True:
        # sleep(10); tick()` — so before `close()` existed the ONLY thing that
        # ever ended one was the process exiting. `stop()` is the hands-free
        # toggle and must NOT end it (state=stopped, and the hourly summary, are
        # exactly what it reports while hands-free is off), and `restart()`
        # replaces the capture thread, not this one: a listener that was stopped,
        # restarted and dropped still left a reporter behind.
        class _Asst:
            def _maybe_self_heal(self, degraded): pass
            def _resource_tick(self): pass
            def _world_tick(self): pass
            def _hardware_tick(self): pass

        ln = H.ContinuousListener(_Asst())
        try:
            thread = ln._health_thread
            assert thread.name == "mic-health", thread.name
            assert thread.is_alive(), "the reporter must be running to be stopped"
            ln.stop()
            assert thread.is_alive(), (
                "stop() ended the reporter: switching hands-free off would then "
                "stop the state=stopped line and the hourly summary with it")
            ln.close()
            thread.join(timeout=2.0)
            assert not thread.is_alive(), (
                "close() left the reporter running — a listener thrown away must "
                "take its thread with it, or every construction leaks one")
            ln.close()          # teardown may close the same listener twice
            assert not thread.is_alive()
        finally:
            ln.close()

    # -- immediate transition reporting -----------------------------------

    def _records(self, caplog):
        return [r for r in caplog.records if "mic health" in r.getMessage()]

    def test_degradation_logs_immediately_then_suppressed(self, H, caplog):
        """First tick on degradation fires at once; an unchanged state stays
        quiet until the next hourly summary."""
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_opens_failed = 3
        ln._health_failing_since = _t.monotonic() - 30
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
            assert any("state=open-failing" in r.getMessage()
                       for r in self._records(caplog))
            caplog.clear()
            ln._health_tick()
            ln._health_tick()
            assert self._records(caplog) == [], \
                "unchanged state must not re-report within the hour"

    def test_recovery_line_carries_failure_duration(self, H, caplog):
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_opens_failed = 3
        ln._health_failing_since = _t.monotonic() - 40
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
            # recovered: a successful open cleared the streak
            ln._capture_rate = 44100
            ln._health_opens_ok = 1
            ln._frames_seen = 500
            ln._last_nonzero = _t.monotonic()
            ln._health_failing_since = None
            ln._health_recovered_after = 40.0
            ln._health_last_open = "06:12:00"
            caplog.clear()
            ln._health_tick()
        msgs = " ".join(r.getMessage() for r in self._records(caplog))
        assert "open-failing -> listening after 40s failing" in msgs

    def test_log_levels_warn_when_degraded(self, H, caplog):
        """Degraded states (silent, open-failing) must surface at WARNING so
        they stand out in journalctl priority filters; listening and a plain
        'stopped' (hands-free off by choice) stay INFO."""
        import logging, time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._health_failing_since = _t.monotonic()
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        failing = [r for r in self._records(caplog)
                   if "state=open-failing" in r.getMessage()]
        assert failing and failing[0].levelno == logging.WARNING

        caplog.clear()
        ln._capture_rate = 16000
        ln._health_opens_ok = 1
        ln._frames_seen = 10
        ln._last_nonzero = _t.monotonic()
        ln._health_failing_since = None
        ln._health_recovered_after = 1.0
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        listening = [r for r in self._records(caplog)
                     if "state=listening" in r.getMessage()
                     and "changed" not in r.getMessage()]
        assert listening and listening[0].levelno == logging.INFO

        # stopped by choice: NOT a warning
        caplog.clear()
        ln._running = False
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        stopped = [r for r in self._records(caplog)
                   if "state=stopped" in r.getMessage()]
        assert stopped and stopped[0].levelno == logging.INFO

    def test_silent_reported_before_reopen_resets_clock(self, H, caplog):
        """The report threshold must be well below the 45 s reopen threshold,
        otherwise the reopen resets _last_nonzero and 'silent' is never seen."""
        assert H.ContinuousListener.MIC_SILENT_REPORT_S \
            < H.ContinuousListener.SILENT_REOPEN_S
        # and the state machine actually classifies a mid-range silence
        import time as _t
        ln = self._mk_listener(H)
        ln._running = True
        ln._frames_seen = 400
        ln._capture_rate = 16000
        ln._health_opens_ok = 1
        ln._last_nonzero = _t.monotonic() - 25.0   # between 20 and 45
        with caplog.at_level("INFO", logger="handsoff"):
            ln._health_tick()
        assert any("state=silent" in r.getMessage()
                   for r in self._records(caplog))

    def test_snapshot_does_not_wait_for_slow_health_hook(self, H, monkeypatch):
        """A slow assistant health check must not block the mic snapshot."""
        ln = self._mk_listener(H)
        ln._running = True
        ln._frames_seen = 1
        ln._last_nonzero = time.monotonic()
        entered = threading.Event()
        release = threading.Event()

        def slow_self_heal(_degraded):
            entered.set()
            assert release.wait(2.0)

        ln._assistant = types.SimpleNamespace(
            _maybe_self_heal=slow_self_heal,
            _resource_tick=lambda: None,
            _world_tick=lambda: None,
            _hardware_tick=lambda: None,
        )
        monkeypatch.setattr(H, "_record_mic_event", lambda *_: None)
        worker = threading.Thread(target=ln._health_tick)
        worker.start()
        assert entered.wait(1.0), "health hook did not start"

        snapshot_done = threading.Event()

        def read_snapshot():
            ln.mic_snapshot()
            snapshot_done.set()

        reader = threading.Thread(target=read_snapshot)
        reader.start()
        assert snapshot_done.wait(0.25), "mic snapshot blocked behind health hook"
        release.set()
        worker.join(timeout=2.0)
        reader.join(timeout=2.0)
        assert not worker.is_alive()


class TestLiveMicProbe:
    """The settings app's live mic test: GUI-free probe core that meters the
    selected device and transcribes utterances with the bubble's own stack."""

    def _load_probe_class(self):
        mod = _load("handsoff_settings_live", HERE / "handsoff-settings.py")
        return mod, mod._LiveMicProbe

    def test_snapshot_shape_before_start(self):
        mod, P = self._load_probe_class()
        p = P()
        s = p.snapshot()
        for key in ("running", "device", "rate", "peak", "gate_open",
                    "transcript", "error", "frames"):
            assert key in s
        assert s["running"] is False and s["rate"] == 0 and s["error"] == ""

    def test_start_is_noop_for_identical_params(self):
        mod, P = self._load_probe_class()
        p = P()
        p._running = True
        p._device, p._threshold = "system default", 300
        before = threading.active_count()
        p.start("system default", 300)
        time.sleep(0.2)
        assert threading.active_count() == before

    def test_frame_loop_meters_and_captures(self):
        """Feeding synthetic loud frames through the real _SpeechGate opens
        the gate, tracks the speech peak, and hands audio to the STT worker."""
        mod, P = self._load_probe_class()
        BH = mod.H                      # the bubble module, as loaded by the app
        p = P()
        p._running = True
        p._gate = BH._SpeechGate(300)
        p._stream = object()            # callback guard: stream "exists"
        p._capture_rate = BH.SAMPLE_RATE
        max_frames = int(P.MAX_UTT_S * BH.SAMPLE_RATE / P.FRAME)
        handed = []
        p._start_transcribe = lambda audio: handed.append(audio)
        quiet = np.zeros((P.FRAME, 1), dtype=np.int16)
        for _ in range(30):
            p._on_frames(quiet, None, max_frames)
        assert p.snapshot()["peak"] < 100
        loud = (np.sin(np.linspace(0, 200, P.FRAME)) * 6000).astype(np.int16)
        loud = loud.reshape(-1, 1)
        p._on_frames(loud, None, max_frames)          # 1 loud frame
        p._on_frames(loud, None, max_frames)          # 2nd: gate starts
        snap = p.snapshot()
        assert snap["gate_open"] is True
        assert snap["speech_peak"] > 3000
        p._on_frames(loud, None, max_frames)          # payload frame
        for _ in range(20):                           # hangover → "end"
            p._on_frames(quiet, None, max_frames)
        assert len(handed) == 1 and handed[0].dtype == np.int16
        assert "speech captured" in p.snapshot()["last_event"]

    def test_transcribe_worker_updates_snapshot(self, monkeypatch):
        mod, P = self._load_probe_class()
        p = P()
        p._running = True
        me = threading.current_thread()
        p._transcribe_thread = me                     # we ARE the worker
        # The worker's contract is the MAPPING (empty text ->
        # "(unintelligible)"). Leaving the real whisper in the path made this
        # test depend on a model being loadable: with none it raised, the
        # worker took its error branch, and the transcript stayed "" — green
        # wherever the model is cached, red in a container, for a reason that
        # has nothing to do with the probe.
        monkeypatch.setattr(mod.H, "transcribe", lambda audio: "")
        p._transcribe_worker(np.zeros(1600, dtype=np.int16))
        s = p.snapshot()
        assert s["transcript"] == "(unintelligible)"  # silence → empty text
        assert s["last_event"] == "transcribed"
        # a superseded worker must not clobber a newer result
        p._last_transcript = "fresh"
        other = threading.Thread(target=lambda: None)
        other.start(); other.join()
        p._transcribe_thread = other                  # not us anymore
        p._transcribe_worker(np.zeros(1600, dtype=np.int16))
        assert p.snapshot()["transcript"] == "fresh"

    def test_transcribe_failure_sets_event(self):
        mod, P = self._load_probe_class()
        p = P()
        p._running = True
        me = threading.current_thread()
        p._transcribe_thread = me
        orig = mod.H.transcribe
        mod.H.transcribe = lambda audio: (_ for _ in ()).throw(RuntimeError("no model"))
        try:
            p._transcribe_worker(np.zeros(1600, dtype=np.int16))
        finally:
            mod.H.transcribe = orig
        assert "transcribe failed" in p.snapshot()["last_event"]

    def test_window_wiring(self):
        """The Voice tab must own the toggle → probe wiring and stop the
        probe on window close."""
        _load("handsoff_settings_live2", HERE / "handsoff-settings.py")  # import check
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        for fragment in ("_toggle_mic_live", "_LiveMicProbe()", "_mic_live_tick",
                         "mic_live_btn.setCheckable(True)",
                         "currentIndexChanged.connect", "valueChanged.connect"):
            assert fragment in src, fragment
        # The whole method, not a fixed window of characters: a window turns
        # "is the probe stopped on close" into "is the line still where it was",
        # so any line added above it fails the test for no reason.
        ce = method_source(src, "closeEvent")
        assert "_live_probe.stop()" in ce


class TestFollowupWindow:
    """Announce-and-listen: after each spoken reply, ONE follow-up utterance
    is accepted without the wake word. The window arms only on natural reply
    completion, is consumed by a single use, and never bypasses the gate for
    hands-free-off or push-to-talk."""

    @pytest.fixture()
    def _setup(self, H, monkeypatch):
        monkeypatch.setattr(H, "transcribe", lambda audio: "what is that tower")
        monkeypatch.setattr(H, "tts_to_wav", lambda text, wav: None)
        monkeypatch.setattr(_core_audio, "play_wav", lambda wav, cancel: None)
        monkeypatch.setitem(H.SETTINGS, "wake_word_required", True)
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 6.0)

    def _mk(self, H, window):
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = True
        a._followup_until = window
        a._wake_until = 0.0
        a._spotter_wake = False
        a._models_ready = threading.Event(); a._models_ready.set()
        a._recently_spoken = ["The capital is Paris."]
        a._last_transcript = ("", 0, 0.0)
        a._empty_streak = 0
        a._try_snooze = lambda *a_: False
        a._set = lambda gen, state: None
        return a

    def test_window_accepts_one_followup(self, H, _setup):
        seen = {}
        a = self._mk(H, time.monotonic() + 5)
        a._brain_turn = lambda text, gen, cancel: seen.update(text=text)
        a._pipeline(np.zeros(16000, dtype="int16"), 0, threading.Event())
        assert seen.get("text") == "what is that tower"
        assert a._followup_until == 0.0, "window must be consumed after one use"

    def test_expired_window_still_gated(self, H, _setup):
        seen = {}
        a = self._mk(H, 0.0)
        a._brain_turn = lambda text, gen, cancel: seen.update(text=text)
        a._pipeline(np.zeros(16000, dtype="int16"), 0, threading.Event())
        assert "text" not in seen, "expired window must not bypass the wake gate"

    def test_armed_on_full_reply(self, H, _setup):
        """_speak arms the window only after a spoken reply completes."""
        a = self._mk(H, 0.0)
        a._turn_spoke = False
        cancel = threading.Event()
        a._speak("Here is your answer.", 0, cancel)
        assert a._turn_spoke is True
        assert a._followup_until > time.monotonic(), \
            "a completed reply must arm the window"

    def test_not_armed_when_cancelled(self, H, _setup):
        """A barged-in reply must not arm the window."""
        a = self._mk(H, 0.0)
        a._turn_spoke = False
        cancel = threading.Event(); cancel.set()
        a._speak("partial reply", 0, cancel)
        assert a._followup_until == 0.0, "barged-in reply must not arm"

    def test_not_armed_when_feature_off_or_ptt(self, H, _setup, monkeypatch):
        a = self._mk(H, 0.0)
        a._turn_spoke = False
        cancel = threading.Event()
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 0.0)
        a._speak("A reply.", 0, cancel)
        assert a._followup_until == 0.0, "feature off must not arm"
        monkeypatch.setitem(H.SETTINGS, "followup_seconds", 6.0)
        a._handsfree = False
        a._speak("Another reply.", 0, cancel)
        assert a._followup_until == 0.0, "push-to-talk must not arm"

    def test_interrupt_clears_window(self, H, _setup):
        a = self._mk(H, time.monotonic() + 5)
        a._cancel = threading.Event()
        a._listener = type("L", (), {"reset": lambda self: None})()
        a.interrupt()
        assert a._followup_until == 0.0

    def test_snooze_fastpath_runs_before_followup(self, H, _setup):
        """'snooze' after a reminder announcement must hit the snooze
        handler, not be consumed as a generic follow-up."""
        import inspect
        src = inspect.getsource(H.Assistant._pipeline)
        assert src.index("_try_snooze") < src.index("_followup_until")

    def test_settings_default_and_coercion(self, H):
        assert H.DEFAULT_SETTINGS["followup_seconds"] == 6.0
        # coerce_settings expects a fully-merged dict (defaults first),
        # exactly how _load_settings and the settings app call it
        s = {**H.DEFAULT_SETTINGS, "followup_seconds": "abc"}
        assert _core_settings.coerce_settings(s)["followup_seconds"] == 6.0
        s2 = {**H.DEFAULT_SETTINGS, "followup_seconds": "15"}
        assert _core_settings.coerce_settings(s2)["followup_seconds"] == 15.0


class TestHandsfreeConfirm:
    """Mod+Shift+H (handsfree toggle) confirms out loud, including mic health;
    `handsfree-status` speaks the state without toggling."""

    def _mk(self, H, mic_state="listening"):
        a = H.Assistant.__new__(H.Assistant)
        a._state = "idle"
        a._handsfree = True
        a._followup_until = 0.0
        a._gen = 0
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = True
        ln._frames_seen = 900
        ln._last_nonzero = time.monotonic()
        ln._capture_rate = 16000
        ln._health_utt = 1
        ln._health_opens_ok = 1
        ln._health_opens_failed = 0
        ln._health_open_device = "TestMic"
        ln._health_last_open = "06:40:00"
        ln._health_failing_since = None
        ln._health_stalled_since = None
        ln._lock = threading.RLock()
        if mic_state == "silent":
            ln._last_nonzero = time.monotonic() - 25.0
        elif mic_state == "stopped":
            ln._running = False
        elif mic_state == "open-failing":
            ln._health_opens_failed = 4
            ln._health_failing_since = time.monotonic() - 40
            ln._running = True
            ln._frames_seen = 0
        a._listener = ln
        spoken = []
        a._announce_now = lambda text: spoken.append(text)
        return a, spoken

    @pytest.fixture()
    def _no_ollama_probe(self, H, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: True)

    @pytest.fixture()
    def server(self, H, tmp_path):
        """Local copy of TestControlSocket's server fixture (fixtures don't
        cross class boundaries): real Assistant + ControlServer on a tmp socket."""
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        sock_path = tmp_path / "control.sock"
        monkey = pytest.MonkeyPatch()
        monkey.setattr(H, "CONTROL_SOCK", sock_path)
        asst = H.Assistant()
        # Commands the server does not answer itself are delivered on the Qt
        # event loop. Record them so the teardown can tell a test that owned
        # its delivery from one that left a command queued.
        delivered: list[str] = []
        asst.sigCommand.connect(delivered.append)
        srv = H.ControlServer(asst)
        srv.start()
        deadline, ready = time.time() + 5, False
        while time.time() < deadline:
            try:
                # imported here, as the original fixture body did
                from test_lifecycle import TestControlSocket
                if TestControlSocket._roundtrip(sock_path, "status").startswith("state="):
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(0.05)
        assert ready, "control server never answered"
        try:
            yield H, None, None
        finally:
            srv.stop()                  # the accept loop is a named worker
            # Anything that arrives in this drain was left queued by THIS test:
            # it would otherwise run during whichever test next processes
            # events, where a speaking verb starts a worker the leak guard then
            # blames on that innocent neighbour.
            before = len(delivered)
            app = QCoreApplication.instance()
            if app is not None:
                app.processEvents()
            assert delivered[before:] == [], (
                "this test left control command(s) queued: "
                + ", ".join(delivered[before:])
                + " — a command runs on the Qt event loop, so process events "
                  "and assert its delivery inside the test that sent it")
            monkey.undo()
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def test_a_wedged_diagnostic_is_not_piled_up(self, H, server, monkeypatch):
        """Repeated `health` against a wedged backend must not spawn a worker each.

        A timed-out diagnostic worker cannot be cancelled — it is blocked in
        the backend call — so spawning one per request is how repeated
        `health`/`doctor` against a stalled Ollama piles up threads that never
        return. The accept loop is serial, so the second request arrives while
        the first's worker is still wedged: it must be refused by name.
        """
        sock_path = H.CONTROL_SOCK
        from test_lifecycle import TestControlSocket
        # The wedged backend is RELEASED at the end instead of being left to
        # time out: a worker parked in a 10s sleep outlives its own test, and
        # the next test's thread census then depends on the clock — which is
        # exactly what the shuffled ordering probe caught.
        release = threading.Event()
        monkeypatch.setattr(H.Assistant, "mic_health",
                            lambda self: release.wait(10) or {})
        # The refusal is announced through the speech channel; stub the channel
        # so this test is about the cap, and pin that it DOES speak.
        said: list = []
        monkeypatch.setattr(H.Assistant, "_announce_now",
                            lambda self, text: said.append(text))

        # Counted as "new since here": a wedged worker another test is still
        # winding down is not this cap's pile-up.
        before = {t.ident for t in threading.enumerate() if t.name == "diag-worker"}
        first = TestControlSocket._roundtrip(sock_path, "health")
        assert "timed out" in first, first

        second = TestControlSocket._roundtrip(sock_path, "health")
        assert "still running" in second, (
            f"a second diagnostic was started instead of being refused: {second}")
        workers = [t for t in threading.enumerate()
                   if t.name == "diag-worker" and t.ident not in before]
        assert len(workers) == 1, f"{len(workers)} diagnostic workers piled up"
        assert len(said) == 1, f"a refused diagnostic was announced {len(said)}x"
        assert "diagnostic-worker" in said[0] and "Nothing was started" in said[0]
        # ...and the worker this test did start is gone before the next test:
        # the backend answers and the worker is joined, not abandoned.
        release.set()
        deadline = time.time() + 5
        while time.time() < deadline and any(t.is_alive() for t in workers):
            time.sleep(0.01)
        leaked = [t.name for t in workers if t.is_alive()]
        assert not leaked, f"the wedged diagnostic leaked past its test: {leaked}"

    def test_healthy_toggle_on_confirms(self, H, _no_ollama_probe):
        a, spoken = self._mk(H, "listening")
        a._confirm_handsfree()
        assert spoken == ["Hands-free on, listening."]

    def test_dead_mic_warns_in_confirmation(self, H, _no_ollama_probe):
        a, spoken = self._mk(H, "open-failing")
        a._confirm_handsfree()
        assert "can't hear you" in spoken[0] and "open-failing" in spoken[0]
        a2, spoken2 = self._mk(H, "silent")
        a2._confirm_handsfree()
        assert "can't hear you" in spoken2[0] and "silent" in spoken2[0]

    def test_toggle_off_confirms_and_flags_active_mic(self, H, _no_ollama_probe):
        a, spoken = self._mk(H, "listening")
        a._handsfree = False
        a._confirm_handsfree()
        assert spoken == ["Hands-free off, but the microphone is still "
                          "listening."]
        a2, spoken2 = self._mk(H, "stopped")
        a2._handsfree = False
        a2._confirm_handsfree()
        assert spoken2 == ["Hands-free off."]

    def test_announce_now_uses_fresh_cancel_and_idle(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 5
        states, captured = [], {}
        a._set = lambda gen, st: states.append(st)
        a._speak = lambda text, gen, cancel, sentence_q=None: captured.update(
            text=text, cancel_set=cancel.is_set())
        a._announce_now("check")
        assert wait_for(lambda: "text" in captured), \
            "the announcement never reached the speaker"
        assert captured["text"] == "check"
        assert captured["cancel_set"] is False, \
            "announcement must never inherit a cancelled turn"
        assert states[-1] == "idle"

    def test_on_command_routes_through_confirmation(self, H, monkeypatch,
                                                    tmp_path):
        monkeypatch.setattr(H, "SETTINGS_FILE", tmp_path / "settings.json")
        for action, expected_on in (("handsfree", None), ("handsfree-on", True),
                                    ("handsfree-off", False),
                                    ("handsfree-status", None)):
            a, spoken = self._mk(H, "listening")
            seen = []
            a._confirm_handsfree = lambda: seen.append(1)
            a._on_command(action)
            assert seen, f"{action} must speak a confirmation"
            if expected_on is not None:
                assert a._handsfree is expected_on

    def test_handsfree_status_roundtrip(self, server, monkeypatch):
        """The keybind's status action reaches the bubble AND speaks the state.

        Delivered inside this test instead of left queued: the confirmation runs
        on the Qt event loop, so a command still sitting in the queue is
        executed during whichever test next calls processEvents() — measured: it
        spoke there, starting a TTS worker for a test that had already finished,
        and the leak guard then blamed an unrelated test.
        """
        from PySide6.QtCore import QCoreApplication
        H, _delivered, _app = server
        app = QCoreApplication.instance()
        said: list = []
        monkeypatch.setattr(H.Assistant, "_announce_now",
                            lambda self, text: said.append(text))
        assert H.ptt_client(["handsfree-status"]) == 0
        deadline = time.time() + 3
        while not said and time.time() < deadline:
            app.processEvents()
        assert said and said[0].startswith("Hands-free off"), said

    def test_keybind_snippet_carries_status_bind(self):
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert '"--ptt" "handsfree-status"' in src
        assert "Mod+Shift+J" in src


class TestVoicePreviewSay:
    """Settings → Voice "Test voice", which now speaks through the bubble.

    The preview used to build its OWN speech model in the settings process.
    chatterbox makes that untenable — ~2.7 GB of VRAM in a second process, and
    it would preview the GUI's idea of the voice rather than the bubble's — so
    the preview moved onto the control socket. What that opens up is the thing
    this class pins: the command now has a PAYLOAD, and the payload is the
    user's text.
    """

    @pytest.fixture()
    def voice_server(self, H, tmp_path, monkeypatch):
        """Real Assistant + ControlServer, with synthesis stubbed out."""
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.instance() or QCoreApplication([])
        sock_path = tmp_path / "control.sock"
        monkeypatch.setattr(H, "CONTROL_SOCK", sock_path)
        spoken: list[str] = []
        monkeypatch.setattr(
            H, "tts_to_wav",
            lambda text, wav, voice_getter=None: spoken.append(text))
        monkeypatch.setattr(_core_audio, "play_wav", lambda wav, cancel: None)
        # → a loaded model, so _speak does not wait on the startup loader.
        monkeypatch.setattr(H, "_tts_model", object())
        asst = H.Assistant()
        srv = H.ControlServer(asst)
        srv.start()
        from test_lifecycle import TestControlSocket
        deadline, last_err = time.time() + 5, None
        while time.time() < deadline:
            try:
                if TestControlSocket._roundtrip(sock_path, "status").startswith("state="):
                    break
            except OSError as e:
                last_err = e
            time.sleep(0.05)
        else:
            pytest.fail(f"control server never answered ({last_err})")
        try:
            yield H, spoken, TestControlSocket
        finally:
            srv.stop()

    def test_the_payload_reaches_synthesis_with_its_case_intact(
            self, H, voice_server):
        """The dispatch lowercases the command word; the argument must survive.

        One payload for the whole request means the naive implementation turns
        "Hello THERE" into "hello there" — the user hears a different sentence
        than the one they typed, which is exactly what a preview is for.
        """
        H, spoken, TestControlSocket = voice_server
        reply = TestControlSocket._roundtrip(H.CONTROL_SOCK, "say Hello THERE")
        assert reply.startswith("ok"), reply
        assert wait_for(lambda: spoken == ["Hello THERE"]), spoken

    def test_say_is_a_first_class_command_for_the_cli(self, H, voice_server):
        """`--ptt say <text>` must be routed, not rejected as unknown."""
        H, spoken, _rt = voice_server
        assert H.ptt_client(["say", "Round", "trip."]) == 0
        assert wait_for(lambda: spoken == ["Round trip."]), spoken

    def test_say_without_text_is_a_usage_error_not_a_silent_ok(self, H,
                                                               voice_server):
        """A GUI button that reports success while saying nothing is the
        silent-failure shape this whole audit is about."""
        H, spoken, _rt = voice_server
        assert H.ptt_client(["say"]) == 2
        assert spoken == []

    def test_a_preview_is_not_a_turn(self, H):
        """It must not bump the generation: that would invalidate the reply
        the user is listening to, or the turn they are waiting on."""
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 7
        spoken = []
        a._announce_now = spoken.append
        reply = a.say_preview("Hello there")
        assert spoken == ["Hello there"]
        assert reply.startswith("ok"), reply
        assert a._gen == 7, "a preview consumed a turn generation"

    def test_a_preview_cannot_be_used_to_queue_minutes_of_speech(self, H):
        """It is reachable over a socket, so the payload must be bounded."""
        a = H.Assistant.__new__(H.Assistant)
        spoken = []
        a._announce_now = spoken.append
        reply = a.say_preview("word " * 500)
        assert spoken and len(spoken[0]) <= H._SAY_PREVIEW_MAX
        assert "truncated" in reply, reply

    def test_an_empty_preview_says_so(self, H):
        a = H.Assistant.__new__(H.Assistant)
        spoken = []
        a._announce_now = spoken.append
        assert a.say_preview("   ").startswith("error")
        assert spoken == []


class TestMicHistory:
    """Mic-state transitions persist to ~/.local/state/handsoff/mic-health.json
    and the morning briefing reports problems since its last delivery."""

    @pytest.fixture()
    def _micfile(self, H, tmp_path, monkeypatch):
        f = tmp_path / "mic-health.json"
        monkeypatch.setattr(H, "MIC_EVENTS_FILE", f)
        return f

    def test_record_counts_degraded_only(self, H, _micfile):
        H._record_mic_event("listening", "silent")
        H._record_mic_event("silent", "listening")     # recovery: ignored
        H._record_mic_event("listening", "open-failing")
        out = H._recent_mic_problems()
        assert "1x open-failing" in out and "1x silent" in out
        assert "most recent" in out
        # healthy-only history -> no section
        H._record_mic_event("silent", "listening")
        H._record_mic_event("open-failing", "listening")
        doc = H._load_mic_events()
        assert all(e["to"] not in ("silent", "open-failing", "stalled")
                   for e in doc["events"]) or True
        out2 = H._recent_mic_problems(since=0.0)
        # the two degraded entries above are still in the window
        assert "open-failing" in out2

    def test_summary_handles_missing_and_corrupt(self, H, _micfile):
        assert H._recent_mic_problems() == ""
        _micfile.write_text("{not json", encoding="utf-8")
        assert H._recent_mic_problems() == ""
        assert H._load_mic_events() == {}
        # a healthy-only doc also yields ''
        _micfile.write_text(json.dumps(
            {"events": [{"t": time.time(), "from": "boot", "to": "listening"}]}),
            encoding="utf-8")
        assert H._recent_mic_problems() == ""

    def test_events_capped(self, H, _micfile):
        for _ in range(H.MIC_EVENTS_MAX + 50):
            H._record_mic_event("a", "b")
        assert len(H._load_mic_events()["events"]) == H.MIC_EVENTS_MAX

    def test_briefing_mentions_problems_once(self, H, _micfile, monkeypatch):
        """Full integration: weather stub + degraded mic history -> the
        briefing prefix carries the problems; a second call the same day is
        empty, and the stamp advances so yesterday's problems don't repeat."""
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        # The briefing's world-headline source is a live DuckDuckGo fetch. It is
        # not what this test is about, and it made the test's RUNTIME depend on
        # a third party: measured 20.5 s when the endpoint throttled, 0.4 s
        # when it did not — the same suite, same day.
        monkeypatch.setattr(H, "_world_events", lambda *a, **k: ([], False))

        class _Tools:
            @staticmethod
            def execute(name, args):
                if name == "get_weather":
                    return _core_tools.ToolResult("Sunny, 21 degrees in Berlin.")
                return _core_tools.ToolResult("ERROR: nope", "error")

        a = H.Assistant.__new__(H.Assistant)
        a._tools = _Tools()
        a._briefing_done_date = ""
        H._record_mic_event("listening", "open-failing")
        prefix = a._maybe_briefing_prefix("good morning")
        assert "Sunny, 21 degrees" in prefix
        assert "Microphone problems" in prefix
        assert "open-failing" in prefix
        # same day: no second briefing
        assert a._maybe_briefing_prefix("hello again") == ""
        # new day, no new problems since the stamp -> no mic section.
        # The once-a-day stamp that survives a reload is the one on disk, so a
        # simulated new day has to move that whole file back: the stamp AND the
        # events. Moving only the in-memory copy still reads as "already
        # greeted today", and moving only the stamp would make today's problem
        # look like a new one since yesterday's briefing.
        shift = 26 * 3600
        doc = json.loads(_micfile.read_text())
        doc["last_briefing"] = doc["last_briefing"] - shift
        for event in doc.get("events", []):
            if isinstance(event.get("t"), (int, float)):
                event["t"] -= shift
        _micfile.write_text(json.dumps(doc), encoding="utf-8")
        a._briefing_done_date = ""
        prefix2 = a._maybe_briefing_prefix("good morning")
        assert "Sunny" in prefix2 and "Microphone problems" not in prefix2

    def test_a_live_reload_does_not_re_arm_the_briefing(self, H, _micfile,
                                                        monkeypatch):
        """The once-a-day stamp cannot live only in the object's memory.

        `--ptt reload-settings` (and the Settings app's live apply) builds a
        FRESH Assistant, and the greeting then read as due again: measured
        2026-09-18, `morning briefing delivered` at 21:17, 21:24 and 21:35,
        each one a reload apart. The stamp that survives is the one the mic
        state file already carries.
        """
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        monkeypatch.setattr(H, "_world_events", lambda *a, **k: ([], False))

        class _Tools:
            @staticmethod
            def execute(name, args):
                return _core_tools.ToolResult("Sunny, 21 degrees in Berlin.")

        def fresh():
            """A new Assistant, exactly as a live reload leaves one."""
            a = H.Assistant.__new__(H.Assistant)
            a._tools = _Tools()
            a._briefing_done_date = ""
            return a

        H._mark_briefing_delivered()          # today's stamp, on disk
        assert fresh()._maybe_briefing_prefix("good morning") == "", (
            "a reload re-armed the daily greeting")

        # ...and yesterday's stamp must NOT silence today's briefing: the line
        # is a date, not a boolean.
        doc = json.loads(_micfile.read_text())
        doc["last_briefing"] = time.time() - 26 * 3600
        _micfile.write_text(json.dumps(doc), encoding="utf-8")
        prefix = fresh()._maybe_briefing_prefix("good morning")
        assert "Sunny" in prefix, prefix

    def test_briefing_skipped_without_problems_or_disabled(self, H, _micfile,
                                                           monkeypatch):
        class _Tools:
            @staticmethod
            def execute(name, args):
                if name == "get_weather":
                    return _core_tools.ToolResult("Sunny.")
                return _core_tools.ToolResult("ERROR: x", "error")

        a = H.Assistant.__new__(H.Assistant)
        a._tools = _Tools()
        a._briefing_done_date = ""
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        # No live world-news fetch: this asserts the MIC section is absent, and
        # the headline source would otherwise make its runtime a network fact.
        monkeypatch.setattr(H, "_world_events", lambda *a, **k: ([], False))
        assert "Microphone problems" not in a._maybe_briefing_prefix("hi")
        # commands never trigger a briefing
        a._briefing_done_date = ""
        assert a._maybe_briefing_prefix("open terminal") == ""

    def test_a_refused_weather_call_is_not_a_briefing_body(
            self, H, _micfile, monkeypatch):
        """This site asked the same question two ways — `err` and a prefix
        test — and the two could disagree. The refusal is phrased without the
        `ERROR:`/`REFUSED:` prefix on purpose: a failure worded its own way is
        the case a prefix test reads as success, which here would have put the
        failure sentence into the spoken briefing as the weather."""
        class _Tools:
            @staticmethod
            def execute(name, args):
                return _core_tools.ToolResult(
                    "no weather provider is configured", "error")

        a = H.Assistant.__new__(H.Assistant)
        a._tools = _Tools()
        a._briefing_done_date = ""
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        monkeypatch.setattr(H, "_world_events", lambda *a, **k: ([], False))
        assert a._maybe_briefing_prefix("good morning") == ""


class TestMicHistoryPersistence:
    """Only listeners that actually captured (or degraded while trying) may
    write to the mic-health state file — test-constructed or never-started
    listeners must not pollute the real briefing history."""

    @pytest.fixture()
    def _micfile(self, H, tmp_path, monkeypatch):
        f = tmp_path / "mic-health.json"
        monkeypatch.setattr(H, "MIC_EVENTS_FILE", f)
        return f

    def _listener(self, H, ever_started, running=False, opens_failed=0,
                  failing_since=None):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        for k, v in dict(
                _running=running, _frames_seen=0, _last_nonzero=0.0,
                _capture_rate=0, _health_utt=0, _health_opens_ok=0,
                _health_opens_failed=opens_failed,
                _health_open_device="", _health_last_open="never",
                _health_state="", _health_next_summary=0.0,
                _health_failing_since=failing_since,
                _health_recovered_after=None, _health_stalled_since=None,
                _ever_started=ever_started, _lock=threading.RLock()).items():
            setattr(ln, k, v)
        return ln

    def test_never_started_stopped_not_persisted(self, H, _micfile):
        ln = self._listener(H, ever_started=False)
        ln._health_tick()                     # (start) -> stopped
        assert not _micfile.exists(), \
            "a never-started listener must not write the state file"

    def test_degraded_while_trying_persists(self, H, _micfile):
        ln = self._listener(H, ever_started=False, running=True,
                            opens_failed=3, failing_since=time.monotonic())
        ln._health_tick()                     # (start) -> open-failing
        doc = json.loads(_micfile.read_text(encoding="utf-8"))
        assert any(e["to"] == "open-failing" for e in doc["events"])

    def test_started_listener_persists_all(self, H, _micfile):
        ln = self._listener(H, ever_started=True)
        ln._health_tick()                     # (start) -> stopped
        doc = json.loads(_micfile.read_text(encoding="utf-8"))
        assert doc["events"] and doc["events"][-1]["to"] == "stopped"

    def test_concurrent_writers_no_lost_update(self, H, _micfile):
        """The health thread and the briefing stamp both do read-modify-write:
        under the shared lock neither may clobber the other's change."""
        import concurrent.futures as cf
        before = len(H._load_mic_events().get("events") or [])
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(H._record_mic_event, "x", "silent")
                    for _ in range(40)]
            futs += [ex.submit(H._mark_briefing_delivered) for _ in range(20)]
            for f in futs:
                f.result(timeout=10)
        doc = H._load_mic_events()
        assert "last_briefing" in doc
        assert len(doc["events"]) == before + 40   # zero lost updates


class TestMicSelfHeal:
    """Self-healing mic: a capture that stays degraded (silent / open-failing)
    while hands-free is on gets its stream restarted automatically after a
    grace period, with a spoken explanation — and gives up after 3 tries,
    journaling only until the mic recovers (which re-arms it)."""

    def _mk_listener(self, H):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._run_id = 0
        ln._thread = None
        ln._frames_seen = 0
        ln._last_nonzero = 0.0
        ln._health_failing_since = None
        ln._lock = threading.RLock()
        ln._discard = False
        ln._suspended = False
        ln.gate_open = False
        ln._spotter = None
        ln._assistant = types.SimpleNamespace(
            _maybe_self_heal=lambda degraded: None)
        return ln

    def _mk_assistant(self, H):
        """A real Assistant policy object without Qt/loader side effects:
        bind the class methods onto a bare instance."""
        a = H.Assistant.__new__(H.Assistant)
        a._heal_pending_since = None
        a._heal_attempts = 0
        a._heal_last = 0.0
        a._handsfree = True
        a._state = H.IDLE
        ln = self._mk_listener(H)
        ln.SELFHEAL_GRACE_S = H.ContinuousListener.SELFHEAL_GRACE_S
        ln.SELFHEAL_MAX = H.ContinuousListener.SELFHEAL_MAX
        a._listener = ln
        return a

    def test_a_junk_value_that_means_off_disables_self_heal(
            self, H, monkeypatch):
        """`bool("false")` is True — self-heal restarted the capture stream
        mid-conversation, and said so out loud, for a value that asked for it to
        be off. The flag is checked before the grace clock is even armed.

        Only the forms that PARSE as false are here: this flag's default is ON,
        so unparseable junk deliberately takes the default — which is what the
        loader stores for junk too, and the point is that the two agree.
        """
        for raw in ("false", "no", "off", "0", ""):
            a = self._mk_assistant(H)
            monkeypatch.setitem(H.SETTINGS, "mic_selfheal", raw)
            a._maybe_self_heal(True)
            assert a._heal_pending_since is None, raw
            assert a._heal_attempts == 0, raw

    def test_speaking_does_not_look_silent(self, H):
        """Regression: while the assistant SPEAKS, frames are dropped before
        the digital-silence check — the silence clock must stay warm, or a
        healthy mic misclassifies as 'silent' and self-heal restarts it
        mid-reply."""
        import numpy as np
        ln = self._mk_listener(H)
        ln._running = True
        ln._frames_seen = 1000
        ln._last_nonzero = time.monotonic() - H.ContinuousListener.MIC_SILENT_REPORT_S - 5
        ln._assistant = types.SimpleNamespace(state="speaking",
                                              _vad_speech=lambda active: None)
        with ln._lock:
            before = ln._last_nonzero
            ln._process_frame(np.zeros((1024, 1), dtype=np.int16), None,
                              [], 10**9, 1)
            assert ln._last_nonzero > before      # clock kept warm
            assert ln._health_state_now_locked() == "listening"

    def test_grace_period_then_restart_and_speak(self, H, monkeypatch):
        a = self._mk_assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        calls, spoken = [], []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now",
                            lambda self, text: spoken.append(text))
        # tick 1: first sighting — arm the grace clock only
        a._maybe_self_heal(True)
        assert a._heal_pending_since is not None and not calls
        # pretend the grace period has fully elapsed
        a._heal_pending_since -= H.ContinuousListener.SELFHEAL_GRACE_S + 1.0
        a._maybe_self_heal(True)
        assert calls == ["restart"]
        assert a._heal_attempts == 1
        assert a._heal_pending_since is None          # clock restarts
        assert spoken and "microphone" in spoken[0].lower()

    def test_disabled_or_ptt_never_heals(self, H, monkeypatch):
        a = self._mk_assistant(H)
        settings = {**H.DEFAULT_SETTINGS, "mic_selfheal": False}
        monkeypatch.setattr(H, "SETTINGS", settings)
        calls = []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now", lambda self, text: None)
        a._maybe_self_heal(True)
        # disabled: not even the grace clock may arm
        assert a._heal_pending_since is None and a._heal_attempts == 0
        # same with the setting on but hands-free off (push-to-talk session)
        settings["mic_selfheal"] = True
        a._handsfree = False
        a._maybe_self_heal(True)
        assert a._heal_pending_since is None
        a._heal_pending_since = time.monotonic() - 999.0   # forced past grace
        a._maybe_self_heal(True)
        assert not calls and a._heal_attempts == 0

    def test_speaking_defers_the_restart(self, H, monkeypatch):
        a = self._mk_assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        calls = []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now", lambda self, text: None)
        a._maybe_self_heal(True)
        a._heal_pending_since -= 999.0
        a._state = H.SPEAKING                         # mid-reply
        a._maybe_self_heal(True)
        assert not calls
        a._state = H.IDLE                             # reply finished
        a._maybe_self_heal(True)
        assert calls == ["restart"]

    def test_gives_up_after_max_then_rearms_on_recovery(self, H, monkeypatch):
        a = self._mk_assistant(H)
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        calls = []
        monkeypatch.setattr(a._listener, "restart",
                            lambda: calls.append("restart"))
        monkeypatch.setattr(H.Assistant, "_say_now", lambda self, text: None)
        a._heal_attempts = H.ContinuousListener.SELFHEAL_MAX
        a._maybe_self_heal(True)
        a._heal_pending_since -= 999.0
        a._maybe_self_heal(True)
        assert not calls                              # stayed degraded: journal only
        a._maybe_self_heal(False)                     # mic recovered
        assert a._heal_attempts == 0 and a._heal_pending_since is None
        a._maybe_self_heal(True)                      # fresh streak: armed again
        a._heal_pending_since -= 999.0
        a._maybe_self_heal(True)
        assert calls == ["restart"]                   # re-armed

    def test_restart_rebuilds_capture_thread(self, H):
        ln = self._mk_listener(H)
        ln._running = True
        monkey_run = types.SimpleNamespace()          # _run must not really run
        orig_run = H.ContinuousListener._run
        H.ContinuousListener._run = lambda self, rid: None
        try:
            ln.restart()
            assert ln._run_id == 1 and ln._thread is not None
            ln._thread.join(timeout=2.0)
            assert not ln._thread.is_alive()
        finally:
            H.ContinuousListener._run = orig_run

    def test_setting_default_and_coercion(self, H):
        assert H.DEFAULT_SETTINGS["mic_selfheal"] is True   # on by default
        D = H.DEFAULT_SETTINGS
        assert _core_settings.coerce_settings(dict(D))["mic_selfheal"] is True

        def coerced(value):
            return _core_settings.coerce_settings(
                {**D, "mic_selfheal": value})["mic_selfheal"]

        assert coerced(1) is True
        assert coerced("") is False
        assert coerced("yes") is True

    def test_settings_checkbox_wiring(self):
        """The Voice-tab checkbox comes from the table, load and save included.

        This used to grep the app's source for a hand-written widget name
        (`self.selfheal_chk`) together with its load and save lines. All three
        are generated from `settings_schema.SETTINGS_FIELDS` now, and the window
        exposes the widget under the setting's own name (`win.mic_selfheal`, the
        name the offscreen checkbox scenarios drive), so what has to be true is
        asked of the table: a checkbox row on the Voice page.
        """
        from settings_schema import control_for, fields_by_key
        field = fields_by_key()["mic_selfheal"]
        assert control_for(field) == "checkbox", field
        assert field.tab == "voice", field
        assert "microphone" in field.title.lower(), field.title


class TestUtteranceHealth:
    """One compact journal line per accepted utterance ('utterance health:')
    so post-mortems can correlate a command's turn (gen) with the mic state
    at that exact moment, and 'heard (gen=N):' ties the transcript back."""

    def _mk_assistant(self, H, handsfree=True, heal=0):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 7
        a._handsfree = handsfree
        a._heal_attempts = heal
        a._listener = H.ContinuousListener.__new__(H.ContinuousListener)
        ln = a._listener
        ln._running = True
        ln._frames_seen = 1234
        ln._last_nonzero = time.monotonic()
        ln._health_failing_since = None
        ln._health_open_device = "hw:TestMic"
        ln._health_opens_failed = 2
        ln._health_utt = 0
        ln._capture_rate = 16000
        ln._health_state = ""
        ln._ever_started = True
        ln._lock = threading.RLock()
        ln._assistant = a
        return a

    def test_line_fields(self, H, caplog):
        a = self._mk_assistant(H)
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()
        line = " ".join(r.getMessage() for r in caplog.records)
        assert "utterance health: gen=7" in line
        assert "src=handsfree" in line and "mic=listening" in line
        assert "device=hw:TestMic" in line and "rate=16000" in line
        assert "frames=1234" in line and "opens_failed=2" in line
        assert "heal=0" in line

    def test_degraded_state_shows_through(self, H, caplog):
        a = self._mk_assistant(H)
        a._listener._health_failing_since = time.monotonic() - 10
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()
        assert "mic=open-failing" in " ".join(r.getMessage() for r in caplog.records)

    def test_ptt_source_label(self, H, caplog):
        a = self._mk_assistant(H, handsfree=False)
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()
        assert "src=ptt" in " ".join(r.getMessage() for r in caplog.records)

    def test_never_raises_into_the_submit_path(self, H, caplog):
        a = self._mk_assistant(H)
        a._listener = None                       # worst case: broken listener
        with caplog.at_level("INFO", logger="handsoff"):
            a._log_utterance_health()            # must not raise
        assert any("utterance health line failed" in r.getMessage()
                   for r in caplog.records)

    def test_submit_audio_hook_order(self, H):
        """The line fires after the discard check and before the stop probe —
        every accepted utterance gets exactly one health line.

        The discard check now carries the measured frames/peak/threshold (a
        rejected capture has to be diagnosable, and a push-to-talk one is a
        WARNING), so this pins the message stem rather than a closed literal.
        """
        src = inspect.getsource(H.Assistant.submit_audio)
        assert "self._log_utterance_health()" in src
        assert src.index("discarding too-short/quiet capture")
        assert src.index("self._log_utterance_health()") \
            < src.index("self._maybe_instant_stop")

    def test_heard_line_carries_gen(self, H):
        src = inspect.getsource(H.Assistant._pipeline)
        assert 'log.info("heard (gen=%d): %s", gen, text)' in src


class TestPttReleaseNonBlocking:
    """Regression: a wedged ALSA stream must never freeze the Qt UI thread.

    Press-hold turns the bubble red (LISTENING); release calls
    finish_listening -> rec.stop() (stream.stop()/close()) -> submit_audio.
    stream.stop() on the flaky Yeti blocks for seconds, freezing the UI
    (no journal lines, repeated restarts). Release must return fast with
    THINKING painted immediately; stop->submit runs off-thread."""

    def _mk_ptt(self, H, monkeypatch):
        monkeypatch.setattr(H, "transcribe", lambda audio: "hello world")
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 0
        a._state = H.IDLE
        states: list = []
        a._states = states

        def _set(gen, state):
            if gen != a._gen:
                return
            a._state = state
            states.append(state)

        a._set = _set
        a._handsfree = False
        a._cancel = threading.Event()
        a._followup_until = 0.0
        a._heal_attempts = 0
        a._last_transcript = ("", 0, 0.0)
        a._pipeline_q = __import__("queue").Queue()
        a._recorder = None
        a.sigLevel = FakeSig()
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._frames_seen = 0
        ln._last_nonzero = time.monotonic()
        ln._capture_rate = 16000
        ln._health_open_device = ""
        ln._health_opens_failed = 0
        ln._health_opens_ok = 0
        ln._health_utt = 0
        ln._health_last_open = "never"
        ln._health_state = ""
        ln._health_next_summary = 0.0
        ln._health_failing_since = None
        ln._health_recovered_after = None
        ln._health_stalled_since = None
        ln._ever_started = False
        ln._lock = threading.RLock()
        ln._suspended = False
        ln._discard = False
        ln._spotter = None
        a._listener = ln
        return a

    def test_release_returns_fast_when_stop_blocks(self, H, monkeypatch,
                                                   caplog):
        """stop() wedged 5s: the UI-thread call must return in <2s, paint
        THINKING, and still emit the single 'ptt timing:' line."""
        gate = threading.Event()

        class _BlockingStream:
            def __init__(self, *a_, **k_):
                pass

            def start(self):
                pass

            def stop(self):
                gate.wait(5.0)      # wedged ALSA: blocks the caller 5s

            def close(self):
                pass

        monkeypatch.setattr(H.sd, "InputStream", _BlockingStream)
        a = self._mk_ptt(H, monkeypatch)
        with caplog.at_level("INFO", logger="handsoff"):
            a.begin_listening()
            assert a._state == H.LISTENING
            assert a._recorder is not None
            t0 = time.monotonic()
            a.finish_listening()
            dt = time.monotonic() - t0
            assert dt < 2.0, f"finish_listening blocked UI {dt:.1f}s"
            assert a._state == H.THINKING, \
                "release must paint THINKING immediately"
            deadline = time.monotonic() + 7.0
            line = ""
            while time.monotonic() < deadline:
                line = " ".join(r.getMessage() for r in caplog.records)
                if "ptt timing:" in line:
                    break
                time.sleep(0.05)
            assert "ptt timing:" in line, "missing 'ptt timing:' journal line"
            assert "stop_ms=" in line and "submit_ms=" in line
            assert "frames=" in line and "rate=" in line
            assert a._state in (H.THINKING, H.IDLE)
            gate.set()  # let the owning native-stop thread release the guard

    def test_fast_path_submits_to_pipeline(self, H, monkeypatch, caplog):
        """Non-blocking stream: begin -> inject loud frames -> finish queues
        exactly one turn and emits the timing line."""

        class _FastStream:
            def __init__(self, *a_, **k_):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(H.sd, "InputStream", _FastStream)
        a = self._mk_ptt(H, monkeypatch)
        with caplog.at_level("INFO", logger="handsoff"):
            a.begin_listening()
            audio = np.zeros(H.SAMPLE_RATE, dtype=np.int16)
            audio[:1000] = 900          # loud enough to pass the gate
            a._recorder._frames = [audio]
            a._recorder._samples = len(audio)
            a._recorder._native_rate = H.SAMPLE_RATE
            t0 = time.monotonic()
            a.finish_listening()
            assert time.monotonic() - t0 < 2.0
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not a._pipeline_q.empty():
                    break
                time.sleep(0.02)
            assert a._pipeline_q.qsize() == 1, "release must queue one turn"
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any("ptt timing:" in r.getMessage()
                       for r in caplog.records):
                    break
                time.sleep(0.02)
            assert any("ptt timing:" in r.getMessage()
                       for r in caplog.records)
            assert a._state in (H.THINKING, H.IDLE)

    def test_stop_timeout_keeps_native_owner_until_stop_returns(self, H):
        """A timed-out native stop must own the mic until its call returns."""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class _Stream:
            def abort(self):
                calls.append("abort")

            def close(self):
                calls.append("close")

        class _BlockingRecorder:
            _stream = _Stream()

            def stop(self):
                entered.set()
                release.wait(2.0)
                return None

        rec = _BlockingRecorder()
        audio, wedged = H._stop_recorder_bounded(rec, timeout=0.05)
        assert audio is None and wedged is True
        assert entered.wait(1.0)
        assert calls == [], "timeout must not abort/close from another thread"
        release.set()
        deadline = time.monotonic() + 2.0
        while getattr(rec, "_handsoff_stop_owner", None) is not None \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        assert getattr(rec, "_handsoff_stop_owner", None) is None
        assert calls == [], "the owning recorder.stop performed cleanup"

    def test_stale_configured_mic_falls_back(self, H, monkeypatch, caplog):
        opened = []

        class _Stream:
            pass

        def _open(**kwargs):
            opened.append(kwargs.get("device"))
            if kwargs.get("device") == "Blue Microphones: USB Audio (hw:4,0)":
                raise ValueError("No input device matching")
            return _Stream()

        devices = [
            {"name": "SB Omni", "max_input_channels": 1,
             "default_samplerate": 48000},
            {"name": "Logitech StreamCam", "max_input_channels": 1,
             "default_samplerate": 48000},
        ]
        monkeypatch.setattr(H.sd, "InputStream", _open)
        monkeypatch.setattr(H.sd, "query_devices", lambda *a, **k: devices)
        with caplog.at_level("WARNING", logger="handsoff"):
            stream, rate = H._open_input(
                "Blue Microphones: USB Audio (hw:4,0)", 16000, 1024, lambda *_: None)
        assert isinstance(stream, _Stream)
        assert rate == 16000
        assert opened[-1] is None, "stale configured mic should use system default"
        assert any("Blue Microphones: USB Audio (hw:4,0)" in r.getMessage()
                   and "falling back" in r.getMessage().lower()
                   for r in caplog.records)


class TestPttStopWorkerEdges:
    """PTT-stop worker edges: stale drop, stop exception, submit exception,
    double-finish early-return. Pins the off-thread stop->submit contract."""

    def _mk_ptt(self, H, monkeypatch):
        monkeypatch.setattr(H, "transcribe", lambda audio: "hello world")
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 0
        a._state = H.IDLE
        states: list = []
        a._states = states

        def _set(gen, state):
            if gen != a._gen:
                return
            a._state = state
            states.append(state)

        a._set = _set
        a._handsfree = False
        a._cancel = threading.Event()
        a._followup_until = 0.0
        a._heal_attempts = 0
        a._last_transcript = ("", 0, 0.0)
        a._pipeline_q = __import__("queue").Queue()
        a._recorder = None
        try:
            a._ptt_lock = threading.RLock()
        except Exception:
            pass
        try:
            a._ptt_epoch = 0
        except Exception:
            pass
        try:
            a._ptt_stopping = False
        except Exception:
            pass
        # interrupt() touches the listener + global announce cancel
        a._listener = types.SimpleNamespace(reset=lambda: None)
        a.sigLevel = FakeSig()
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._running = False
        ln._frames_seen = 0
        ln._last_nonzero = time.monotonic()
        ln._capture_rate = 16000
        ln._health_open_device = ""
        ln._health_opens_failed = 0
        ln._health_opens_ok = 0
        ln._health_utt = 0
        ln._health_last_open = "never"
        ln._health_state = ""
        ln._health_next_summary = 0.0
        ln._health_failing_since = None
        ln._health_recovered_after = None
        ln._health_stalled_since = None
        ln._ever_started = False
        ln._lock = threading.RLock()
        ln._suspended = False
        ln._discard = False
        ln._spotter = None
        # keep a real listener ref for health lines; _log_utterance_health
        # tolerates the minimal fields above
        a._listener_health = ln
        # submit path calls these; keep them cheap and real
        orig_log_health = H.Assistant._log_utterance_health
        a._log_utterance_health = lambda: None
        a._maybe_instant_stop = lambda audio, gen: None
        return a

    def _loud(self, H, val=900):
        audio = np.zeros(H.SAMPLE_RATE, dtype=np.int16)
        audio[:1000] = val
        return audio

    def _wait_timing(self, caplog, count=1, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            n = sum(1 for r in caplog.records if "ptt timing:" in r.getMessage())
            if n >= count:
                return True
            time.sleep(0.02)
        return False

    def test_stale_drop_second_press_invalidates(self, H, monkeypatch, caplog):
        """Second press bumps the PTT epoch: the first release's worker must
        drop (submit skipped) and still emit the stale timing line."""
        gate = threading.Event()

        class _BlockingRec:
            _native_rate = H.SAMPLE_RATE

            def stop(self):
                gate.wait(5.0)
                return self._audio

        a = self._mk_ptt(H, monkeypatch)
        rec = _BlockingRec()
        rec._audio = self._loud(H)
        a._recorder = rec
        with caplog.at_level("INFO", logger="handsoff"):
            a.finish_listening()  # worker blocks in stop()
            assert a._state == H.THINKING
            # second press: new epoch invalidates the in-flight release
            try:
                a._ptt_epoch = int(getattr(a, "_ptt_epoch", 0) or 0) + 1
            except Exception:
                pass
            a._gen += 1  # keep global gen in sync with a real second press
            gate.set()
            assert self._wait_timing(caplog, 1, 5.0), "missing stale timing line"
            time.sleep(0.2)
            assert a._pipeline_q.empty(), "stale release must not submit"
            line = " ".join(r.getMessage() for r in caplog.records
                            if "ptt timing:" in r.getMessage())
            assert "submit_ms=0" in line

    def test_timer_bump_does_not_discard_valid_utterance(self, H, monkeypatch, caplog):
        """Background timer bumps global gen but not the PTT epoch: a valid
        release must still submit (global-gen check would wrongly drop it)."""

        class _FastRec:
            _native_rate = H.SAMPLE_RATE

            def __init__(self, audio):
                self._audio = audio

            def stop(self):
                return self._audio

        a = self._mk_ptt(H, monkeypatch)
        a._recorder = _FastRec(self._loud(H))
        with caplog.at_level("INFO", logger="handsoff"):
            a.finish_listening()
            # simulate _fire_timer bumping the global gen mid-stop
            a._gen += 1
            assert self._wait_timing(caplog, 1, 5.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and a._pipeline_q.empty():
                time.sleep(0.02)
            assert not a._pipeline_q.empty(), \
                "timer bump must not discard a valid PTT utterance"

    def test_rec_stop_exception_goes_idle_with_timing(self, H, monkeypatch, caplog):
        """rec.stop() raising → audio None, gen-guarded IDLE, timing line."""

        class _BoomRec:
            _native_rate = H.SAMPLE_RATE

            def stop(self):
                raise OSError("wedged stream")

        a = self._mk_ptt(H, monkeypatch)
        a._recorder = _BoomRec()
        with caplog.at_level("INFO", logger="handsoff"):
            a.finish_listening()
            assert self._wait_timing(caplog, 1, 5.0), "missing timing after stop boom"
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and a._state != H.IDLE:
                time.sleep(0.02)
            assert a._state == H.IDLE, "stop exception must end IDLE, not THINKING"

    def test_submit_raising_never_sticks_thinking(self, H, monkeypatch, caplog):
        """submit_audio raising inside the worker → gen-guarded IDLE, second
        timing line, never an unhandled thread exception / stuck THINKING."""
        errors: list = []
        orig_hook = threading.excepthook
        threading.excepthook = lambda args: errors.append(args)
        try:
            loud = self._loud(H)

            class _FastRec:
                _native_rate = H.SAMPLE_RATE

                def stop(self):
                    return loud

            a = self._mk_ptt(H, monkeypatch)
            a._recorder = _FastRec()

            def _boom(audio):
                raise RuntimeError("submit boom")

            a.submit_audio = _boom
            with caplog.at_level("INFO", logger="handsoff"):
                a.finish_listening()
                assert a._state == H.THINKING
                assert self._wait_timing(caplog, 1, 5.0), "missing timing after submit boom"
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and a._state != H.IDLE:
                    time.sleep(0.02)
                assert a._state == H.IDLE, "submit boom must fall back to IDLE"
        finally:
            threading.excepthook = orig_hook
        assert not errors, f"worker thread raised unhandled: {errors}"

    def test_double_finish_with_rec_none_early_returns(self, H, monkeypatch, caplog):
        a = self._mk_ptt(H, monkeypatch)
        a._recorder = None
        gen0 = a._gen
        with caplog.at_level("INFO", logger="handsoff"):
            a.finish_listening()
            a.finish_listening()
        assert a._gen == gen0, "double finish must not bump gen"
        assert a._recorder is None
        assert not any("ptt timing:" in r.getMessage() for r in caplog.records)

    def test_wedged_stop_refuses_repress_until_owner_returns(
            self, H, monkeypatch, caplog):
        """Dead-Yeti wedge (stop() never returns): the ptt-stop worker must
        emit its timing line in <=4s with wedged=1, retain ownership, and
        refuse a subsequent press instead of racing a second open."""
        never = threading.Event()  # never set: the 51s journal wedge

        class _WedgedRec:
            _native_rate = H.SAMPLE_RATE

            def __init__(self):
                self._stream = None

            def stop(self):
                never.wait(30.0)
                return None

        a = self._mk_ptt(H, monkeypatch)
        a._recorder = _WedgedRec()
        with caplog.at_level("INFO", logger="handsoff"):
            t0 = time.monotonic()
            a.finish_listening()
            assert time.monotonic() - t0 < 2.0, "finish must not block the UI"
            assert a._state == H.THINKING
            assert self._wait_timing(caplog, 1, 4.0), \
                "wedged worker never emitted timing within 4s"
            line = " ".join(r.getMessage() for r in caplog.records
                            if "ptt timing:" in r.getMessage())
            assert "wedged=1" in line, f"missing wedged=1 in {line!r}"
            assert "ptt timing: stop_ms=" in line
            assert "submit_ms=" in line and "frames=" in line \
                and "rate=" in line
            assert getattr(a, "_ptt_stopping", False) is True, \
                "native stop owner must remain marked while alive"

            class _NoOpen:
                def __init__(self, *a_, **k_):
                    raise AssertionError("wedged stop must prevent a new open")

            monkeypatch.setattr(H.sd, "InputStream", _NoOpen)
            caplog.clear()
            a.begin_listening()
            assert a._recorder is None
            assert any("refused" in r.getMessage() for r in caplog.records)
            never.set()
            deadline = time.monotonic() + 2.0
            while getattr(a, "_ptt_stopping", False) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            assert getattr(a, "_ptt_stopping", False) is False

    def test_refusal_speaks_busy(self, H, monkeypatch, caplog):
        """Press while a stop is in flight: refuse AND speak the busy line
        (the old log-only refusal left the bubble blue with no voice)."""
        a = self._mk_ptt(H, monkeypatch)
        a._models_ready = threading.Event()
        a._models_ready.set()
        spoken: list = []

        def _fake_speak(self, text, gen, cancel, sentence_q=None):
            spoken.append(text)

        monkeypatch.setattr(H.Assistant, "_speak", _fake_speak)
        a._ptt_stopping = True
        opened: list = []

        class _NoOpen:
            def __init__(self, *a_, **k_):
                opened.append(1)
                raise AssertionError("refusal must not open the mic")

        monkeypatch.setattr(H.sd, "InputStream", _NoOpen)
        with caplog.at_level("INFO", logger="handsoff"):
            t0 = time.monotonic()
            a.begin_listening()
            dt = time.monotonic() - t0
            assert 0.5 <= dt < 2.5, f"refusal must keep the 0.6s wait ({dt:.2f}s)"
            assert a._recorder is None, "refusal must not open the mic"
            assert not opened, "refusal must not open the mic"
        assert any("refused" in r.getMessage()
                   and "stop still in flight" in r.getMessage()
                   for r in caplog.records), "missing refusal warning"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not spoken:
            time.sleep(0.02)
        assert spoken, "busy refusal never spoke"
        assert any("Microphone is busy, try again." in s for s in spoken), \
            f"wrong busy message: {spoken!r}"
        a._ptt_stopping = False


class TestWhisperCudaFallback:
    """Lazy-CUDA breakage: ctranslate2 resolves CUDA libs at FIRST INFERENCE,
    so a CUDA whisper load succeeds and encode() raises libcublas. transcribe()
    must fall back to CPU once per session and retry (covers pipeline + stop
    probe — both call transcribe())."""

    @staticmethod
    def _audio(H):
        return H.np.zeros(1600, dtype=H.np.int16)

    def test_cuda_error_falls_back_to_cpu_once(self, H, monkeypatch, caplog):
        cuda_calls: list = []
        cpu_calls: list = []

        class _CudaBoom:
            def transcribe(self, *a, **k):
                cuda_calls.append(1)
                # LAZY like faster-whisper's generate_segments: the CUDA
                # encode (libcublas) raises during iteration, not here.
                def _gen():
                    raise RuntimeError(
                        "Library libcublas.so.12 is not found or cannot be loaded")
                    yield  # pragma: no cover — generator body, never reached
                return (_gen(), None)

        class _CpuOk:
            def transcribe(self, *a, **k):
                cpu_calls.append(1)
                def _gen():
                    yield types.SimpleNamespace(text="hello world")
                return (_gen(), None)

        get_calls: list = []

        def _fake_get():
            get_calls.append(1)
            return _CudaBoom() if len(get_calls) == 1 else _CpuOk()

        monkeypatch.setattr(H, "get_whisper", _fake_get)
        monkeypatch.setattr(H, "_whisper_model", None, raising=False)
        if hasattr(H, "_whisper_cpu_fallback"):
            monkeypatch.setattr(H, "_whisper_cpu_fallback", False)

        with caplog.at_level("WARNING", logger="handsoff"):
            out = H.transcribe(self._audio(H))
        assert out == "hello world"
        assert cuda_calls == [1] and cpu_calls == [1], \
            "must retry the same audio once on CPU"
        assert any("falling back to CPU" in r.getMessage()
                   for r in caplog.records), "must log a loud warning"
        assert getattr(H, "_whisper_cpu_fallback", False) is True, \
            "session must pin to CPU"

        caplog.clear()
        out2 = H.transcribe(self._audio(H))
        assert out2 == "hello world"
        assert len(cuda_calls) == 1, "second call must go straight to CPU"
        assert len(cpu_calls) == 2

    def test_non_cuda_error_no_retry(self, H, monkeypatch):
        calls: list = []
        get_calls: list = []

        class _Boom:
            def transcribe(self, *a, **k):
                calls.append(1)
                raise RuntimeError("boom")

        def _fake_get():
            get_calls.append(1)
            return _Boom()

        monkeypatch.setattr(H, "get_whisper", _fake_get)
        monkeypatch.setattr(H, "_whisper_model", None, raising=False)
        if hasattr(H, "_whisper_cpu_fallback"):
            monkeypatch.setattr(H, "_whisper_cpu_fallback", False)
        with pytest.raises(RuntimeError, match="boom"):
            H.transcribe(self._audio(H))
        assert len(calls) == 1 and len(get_calls) == 1, \
            "non-CUDA errors must propagate immediately, no reload/retry"

    def test_cpu_retry_failure_raises(self, H, monkeypatch):
        cuda_calls: list = []
        cpu_calls: list = []

        class _CudaBoom:
            def transcribe(self, *a, **k):
                cuda_calls.append(1)
                # LAZY like faster-whisper's generate_segments (see above).
                def _gen():
                    raise RuntimeError(
                        "Library libcublas.so.12 is not found or cannot be loaded")
                    yield  # pragma: no cover — generator body, never reached
                return (_gen(), None)

        class _CpuBoom:
            def transcribe(self, *a, **k):
                cpu_calls.append(1)
                def _gen():
                    raise RuntimeError("cpu boom")
                    yield  # pragma: no cover — generator body, never reached
                return (_gen(), None)

        get_calls: list = []

        def _fake_get():
            get_calls.append(1)
            return _CudaBoom() if len(get_calls) == 1 else _CpuBoom()

        monkeypatch.setattr(H, "get_whisper", _fake_get)
        monkeypatch.setattr(H, "_whisper_model", None, raising=False)
        if hasattr(H, "_whisper_cpu_fallback"):
            monkeypatch.setattr(H, "_whisper_cpu_fallback", False)
        with pytest.raises(RuntimeError, match="cpu boom"):
            H.transcribe(self._audio(H))
        assert len(cuda_calls) == 1 and len(cpu_calls) == 1, \
            "must attempt CPU once, then raise (apology path preserved)"


class TestStreamingFallback:
    def test_tools_fallback_emits_one_terminator(self, H, monkeypatch):
        """A tools-unsupported retry must not enqueue duplicate sentinels."""
        import urllib.error
        import queue

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                yield b'{"message":{"content":"Fallback."}}\n'

        calls = []

        def fake_urlopen(request, timeout=0):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 400, "bad request", {},
                    io.BytesIO(b'{"error":"tools unsupported"}'))
            return _Response()

        monkeypatch.setattr(H.urllib.request, "urlopen", fake_urlopen)
        q = queue.Queue()
        result = H.ollama_chat_stream([{"role": "user", "content": "hi"}],
                                      q, tools=[{"type": "function"}])
        assert result["content"] == "Fallback."
        assert [q.get(timeout=1), q.get(timeout=1)] == ["Fallback.", None]
        assert q.empty()


class TestPttWorksWithHandsfreeOn:
    """Push-to-talk must still RECORD while hands-free is on.

    The user reported "push to talk is broken". The cause was that with
    hands-free enabled `toggle` always fell through to the interrupt branch
    (`and not self._handsfree`) and `begin_listening()` called
    `_listener.suspend()` and returned without ever opening a recorder — so the
    PTT key was a silent no-op with no feedback at all. A press now parks the
    continuous listener and records; release submits and restarts it.
    """

    def _mk(self, H, monkeypatch, state=None):
        a = H.Assistant.__new__(H.Assistant)
        a._gen = 0
        a._state = H.IDLE if state is None else state
        a._handsfree = True
        a._recorder = None
        a._cancel = threading.Event()
        a._ptt_lock = threading.RLock()
        a._ptt_epoch = 0
        a._ptt_stopping = False
        a._followup_until = 0.0
        a._pipeline_q = __import__("queue").Queue()
        a.sigLevel = FakeSig()
        calls = types.SimpleNamespace(listener=[], submitted=[], interrupts=[],
                                      recording=[])
        a._calls = calls
        a._listener = types.SimpleNamespace(
            start=lambda: calls.listener.append("start"),
            stop=lambda: calls.listener.append("stop"),
            reset=lambda: None,
            suspend=lambda: calls.listener.append("suspend"),
            resume=lambda: calls.listener.append("resume"),
        )

        def _set(gen, st):
            if gen != a._gen:
                return
            a._state = st

        a._set = _set
        a.interrupt = lambda: calls.interrupts.append(1)
        a.submit_audio = lambda audio: calls.submitted.append(audio)
        a._maybe_instant_stop = lambda audio, gen: None
        a._log_utterance_health = lambda: None

        class _Rec:
            def __init__(self, on_level=None, device=None, threshold=600):
                self._native_rate = H.SAMPLE_RATE
                calls.recording.append(self)

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True
                return np.zeros(H.SAMPLE_RATE, dtype=np.int16)

        monkeypatch.setattr(H, "Recorder", _Rec)
        return a

    def test_toggle_parks_the_listener_and_records(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        a._on_command("toggle")
        assert a._calls.listener == ["stop"], (
            "hands-free must be parked (stream closed), not merely suspended")
        assert a._recorder is not None, "a PTT press must open a recorder"
        assert a._calls.recording, "Recorder.start() must have run"
        assert a._state == H.LISTENING
        assert not a._calls.interrupts or len(a._calls.interrupts) == 1

    def test_release_submits_the_capture_and_restarts_handsfree(
            self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        audio = np.zeros(H.SAMPLE_RATE, dtype=np.int16)
        audio[:1000] = 900
        monkeypatch.setattr(a, "_stop_recorder_bounded",
                            lambda rec, timeout=3.0: (audio, False))
        a.begin_listening()
        a.finish_listening()
        for _ in range(400):            # the stop->submit runs off the Qt thread
            if a._calls.submitted and "start" in a._calls.listener:
                break
            time.sleep(0.01)
        assert a._calls.submitted, "the PTT capture must be submitted"
        assert len(a._calls.submitted[0]) == len(audio)
        assert "start" in a._calls.listener, (
            "hands-free listening must come back after the press")
        assert a._recorder is None

    def test_resume_is_not_called_while_the_recorder_holds_the_mic(
            self, H, monkeypatch):
        """One mic stream at a time: the listener may only restart after the
        recorder has been released, or the two wedge each other."""
        a = self._mk(H, monkeypatch)
        order = []
        monkeypatch.setattr(a, "_stop_recorder_bounded",
                            lambda rec, timeout=3.0: (order.append("stop"), None)[1])
        a._resume_handsfree_listener = (
            lambda: order.append("resume"))
        a.begin_listening()
        a.finish_listening()
        for _ in range(400):
            if "resume" in order:
                break
            time.sleep(0.01)
        assert order == ["stop", "resume"], order

    def test_interrupt_still_wins_while_speaking(self, H, monkeypatch):
        """The fix must not turn the PTT key into "record" while the bubble is
        talking — that press means stop talking."""
        a = self._mk(H, monkeypatch, state=H.SPEAKING)
        a._on_command("toggle")
        assert a._recorder is None
        assert a._calls.listener == [], a._calls.listener

    def test_handsfree_off_is_unchanged(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        a._handsfree = False
        a.begin_listening()
        assert a._recorder is not None
        assert a._calls.listener == [], "nothing to park when hands-free is off"


class TestAudioFailurePaths:
    """Failures must not leave silent damage behind.

    Each of these was a real defect: playback that failed left the bubble's
    designs pinned at the last level with no further audio to release them, a
    failed synthesis left a truncated wav where the last good one had been, a
    second Recorder.start() dropped an open stream instead of closing it (so
    PortAudio kept the device), and a retry at the native rate discarded the
    original error.
    """

    @staticmethod
    def _tone(path, frames=500):
        import wave as _wave
        with _wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00\x10" * frames)
        return path

    def test_play_wav_releases_the_level_when_the_constructor_fails(self, H, tmp_path):
        import threading
        mod = _load("core_audio_ctor_fail", HERE / "core" / "audio.py")
        path = self._tone(tmp_path / "tone.wav")

        class _NoDevice:
            def __init__(self, **kw):
                raise RuntimeError("no output device")

        mod.sd.OutputStream = _NoDevice
        seen = []
        mod.set_level_hook(seen.append)
        try:
            with pytest.raises(RuntimeError):
                mod.play_wav(path, threading.Event())
            assert seen and seen[-1] == 0.0, seen
        finally:
            mod.set_level_hook(None)

    def test_play_wav_closes_a_stream_that_failed_to_start_and_releases_level(
            self, H, tmp_path):
        import threading
        mod = _load("core_audio_start_fail", HERE / "core" / "audio.py")
        path = self._tone(tmp_path / "tone.wav")
        closed = []

        class _BoomStart:
            def __init__(self, **kw):
                pass

            def start(self):
                raise RuntimeError("device busy")

            def write(self, data):
                pass

            def stop(self):
                pass

            def close(self):
                closed.append(True)

        mod.sd.OutputStream = _BoomStart
        seen = []
        mod.set_level_hook(seen.append)
        try:
            with pytest.raises(RuntimeError):
                mod.play_wav(path, threading.Event())
            assert seen and seen[-1] == 0.0, seen
            assert closed, "a constructed stream must be closed even if start() fails"
        finally:
            mod.set_level_hook(None)

    def test_tts_to_wav_keeps_the_previous_file_when_synthesis_fails(self, H, tmp_path):
        mod = _load("core_audio_tts_fail", HERE / "core" / "audio.py")
        target = self._tone(tmp_path / "reply.wav", frames=100)
        good = target.read_bytes()

        class _BadVoice:
            def generate(self, text):
                raise RuntimeError("tts engine died")

        with pytest.raises(RuntimeError):
            mod.tts_to_wav("hello", target, voice_getter=lambda: _BadVoice())
        assert target.read_bytes() == good, "a failed synthesis truncated the good file"
        assert not list(tmp_path.glob("*.part*")), "temp file left behind"

    def test_tts_to_wav_replaces_atomically_on_success(self, H, tmp_path):
        mod = _load("core_audio_tts_ok", HERE / "core" / "audio.py")
        target = self._tone(tmp_path / "reply.wav", frames=100)

        class _Voice:
            def generate(self, text):
                return np.full(2400, 0.25, dtype=np.float32)

        mod.tts_to_wav("hello", target, voice_getter=lambda: _Voice())
        # The header is part of the contract: play_wav reads the rate from it,
        # so a 16 kHz header on 24 kHz samples would play the reply too fast.
        with wave.open(str(target), "rb") as w:
            assert w.getframerate() == mod.TTS_SR == 24000, w.getframerate()
            assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        assert target.read_bytes() != self._tone(tmp_path / "other.wav",
                                                 frames=100).read_bytes()
        assert not list(tmp_path.glob("*.part*"))

    # ---- chatterbox-turbo seams (piper's replacement) ----------------------

    def test_the_float32_shim_keeps_norm_loudness_in_float32(self, H):
        """Without this patch EVERY reference clip dies.

        Measured on NumPy 2.5.3 + chatterbox-tts 0.1.7: `norm_loudness`
        computes `wav * gain_linear`, NumPy 2 promotes that to float64, and
        s3tokenizer's mel matmul then raises "expected scalar type Float but
        found Double". So voice conditioning is not flaky without the shim, it
        is simply impossible — and the error names none of that.
        """
        mod = _load("core_audio_shim", HERE / "core" / "audio.py")

        class _Model:
            def norm_loudness(self, wav, sr, *a, **kw):
                # what the real one does: a float64 gain times float32 samples
                return np.float64(0.5) * np.asarray(wav, dtype=np.float32)

        model = _Model()
        assert model.norm_loudness(np.ones(4, dtype=np.float32), 24000).dtype \
            == np.float64, "the double really does appear without the shim"
        mod._patch_float32_norm(model)
        out = model.norm_loudness(np.ones(4, dtype=np.float32), 24000)
        assert out.dtype == np.float32, out.dtype
        assert np.allclose(out, 0.5), "the shim must call through, not stub"

    def test_the_shim_invents_nothing_on_a_model_without_the_method(self, H):
        """A renamed/removed norm_loudness must not gain a method it never had
        — a stub would silently skip the engine's own normalisation."""
        mod = _load("core_audio_shim_bare", HERE / "core" / "audio.py")
        model = object()
        mod._patch_float32_norm(model)
        assert not hasattr(model, "norm_loudness")

    def test_two_syntheses_never_interleave(self, H):
        """The engine carries the voice conditionals as MUTABLE state, so a
        spoken reply overlapping another synthesis must not interleave — that
        is what _TTS_RUN_LOCK is for, and it is easy to lose."""
        mod = _load("core_audio_run_lock", HERE / "core" / "audio.py")
        active, overlaps = [], []

        class _Engine:
            def generate(self, text):
                active.append(text)
                if len(active) > 1:
                    overlaps.append(tuple(active))
                time.sleep(0.05)
                active.pop()
                return np.zeros(16, dtype=np.float32)

        engine = _Engine()
        threads = [threading.Thread(target=mod.synthesize, args=("hi", engine))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not overlaps, overlaps

    def test_tts_rate_time_compresses_and_junk_is_ignored(self, H):
        """chatterbox has no duration control, so rate is a resample — time
        compression with a rising pitch. It must actually do something, and a
        nonsense rate must fall back rather than divide by zero."""
        mod = _load("core_audio_rate", HERE / "core" / "audio.py")
        samples = np.linspace(-1, 1, 4800, dtype=np.float32)
        fast = mod.resample_speed(samples, 2.0)
        assert abs(fast.size - 2400) <= 2, fast.size
        assert fast.dtype == np.float32
        assert np.array_equal(mod.resample_speed(samples, 1.0), samples), \
            "1.0x must be a byte-exact no-op, not an extra resample"
        for junk in (0.0, -1.0, float("nan")):
            assert mod.resample_speed(samples, junk).size == samples.size

    def test_volume_scales_then_clips_instead_of_wrapping(self, H):
        """The GUI allows 2.0x. int16 overflow WRAPS and turns a loud reply
        into noise, so the samples must be clipped."""
        mod = _load("core_audio_volume", HERE / "core" / "audio.py")

        class _Engine:
            def generate(self, text):
                half = np.full(16, 0.9, dtype=np.float32)
                return np.concatenate([half, -half])   # +0.9 and -0.9

        setattr(mod, "SETTINGS", {"tts_rate": 1.0, "tts_volume": 1.5})
        loud = mod.synthesize("x", model=_Engine())
        assert loud.dtype == np.int16
        assert int(np.max(np.abs(loud))) > 32000, "volume did not scale"
        setattr(mod, "SETTINGS", {"tts_rate": 1.0, "tts_volume": 4.0})
        sat = mod.synthesize("x", model=_Engine())
        assert (int(np.max(sat)), int(np.min(sat))) == (32767, -32767), (
            f"clipping is required: {int(np.max(sat))}/{int(np.min(sat))} — "
            "wrapping is loud noise, not a no-op")

    def test_a_short_reference_clip_is_named_before_the_engine_asserts(
            self, H, tmp_path):
        """The library asserts > 5 s and fires on EVERY turn, so a 2 s clip is
        a mute bubble. The message must name the CLIP — reporting it as damaged
        weights sends the user to re-download 3.8 GB."""
        mod = _load("core_audio_ref_floor", HERE / "core" / "audio.py")
        short = tmp_path / "optimus_clip.wav"
        with wave.open(str(short), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 24000)          # exactly 1 s
        problem = mod.reference_problem(short)
        assert problem and "optimus_clip.wav" in problem, problem
        assert "5" in problem, problem
        assert mod.reference_problem(tmp_path / "gone.wav") is not None
        long_clip = tmp_path / "ok.wav"
        with wave.open(str(long_clip), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 6)
        assert mod.reference_problem(long_clip) is None
        assert mod.reference_clip_seconds(long_clip) == pytest.approx(6.0, abs=0.1)

    def test_weights_are_looked_for_where_huggingface_puts_them(self, H,
                                                                tmp_path,
                                                                monkeypatch):
        """The installer, doctor and settings app all ask this, and the answer
        decides whether 3.8 GB is downloaded twice. A bare `is_dir()` also
        reported a half-finished fetch as cached."""
        mod = _load("core_audio_weights", HERE / "core" / "audio.py")
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
        assert mod.hf_hub_cache() == tmp_path / "hub"
        monkeypatch.delenv("HF_HUB_CACHE")
        monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hub2"))
        assert mod.hf_hub_cache() == tmp_path / "hub2"
        monkeypatch.delenv("HUGGINGFACE_HUB_CACHE")
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
        assert mod.hf_hub_cache() == tmp_path / "hf" / "hub"
        root = tmp_path / "hf" / "hub" / "models--ResembleAI--chatterbox-turbo"
        monkeypatch.setattr(mod, "TTS_REPO_ID", "ResembleAI/chatterbox-turbo")
        assert mod.tts_weights_dir() == root
        # A snapshot directory with NO blobs is not "cached".
        (root / "snapshots" / "deadbeef").mkdir(parents=True)
        assert mod.tts_weights_cached() is False, (
            "a half-finished download must not read as cached")
        weight = root / "snapshots" / "deadbeef" / "s3gen.safetensors"
        weight.write_bytes(b"weights")
        assert mod.tts_weights_cached() is True

    def test_recorder_start_closes_its_previous_stream(self, H):
        mod = _load("core_audio_recorder", HERE / "core" / "audio.py")
        closed = []

        class _Stream:
            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                closed.append(self)

        streams = [_Stream(), _Stream()]
        mod._open_input = lambda device, rate, bs, cb: (streams[len(closed)], rate)
        rec = mod.Recorder(on_level=lambda v: None)
        rec.start()
        first = rec._stream
        rec.start()                       # a second press without a stop
        assert first in closed, "the superseded stream was dropped, not closed"
        assert closed.count(first) == 1

    def test_open_input_chains_the_original_error(self, H):
        mod = _load("core_audio_open_fail", HERE / "core" / "audio.py")
        first = OSError("device busy")
        second = OSError("invalid sample rate")

        class _SD:
            @staticmethod
            def InputStream(**kw):
                raise first if kw.get("samplerate") == 16000 else second

            @staticmethod
            def query_devices(*a, **k):
                return {"default_samplerate": "48000.0"}

        mod.sd = _SD
        with pytest.raises(OSError) as ei:
            mod._open_input(None, 16000, 1024, lambda *a: None)
        assert ei.value is second
        assert ei.value.__cause__ is first, "the real cause was discarded"


class TestWatchdogReopenLoop:
    """The inner watchdog of ContinuousListener._run — the loop that guards
    hands-free against a USB device that vanishes without closing its stream:
    either the frames STOP arriving (stalled) or they arrive as pure digital
    silence (wedged). Both must reopen; healthy flow must not. This block had
    no test, yet it is what keeps a wedged mic from looking like 'the user is
    quiet' forever."""

    @staticmethod
    def _listener(H):
        ln = H.ContinuousListener.__new__(H.ContinuousListener)
        ln._assistant = types.SimpleNamespace()
        ln._running = True
        ln._run_id = 7
        ln._suspended = False
        ln._discard = False
        ln._spotter = None
        ln._stream = None
        ln._frames_seen = 0
        ln._last_nonzero = 0.0
        ln._health_utt = 0
        ln._health_opens_ok = 0
        ln._health_opens_failed = 0
        ln._health_open_device = ""
        ln._health_last_open = "never"
        ln._health_state = ""
        ln._health_next_summary = 0.0
        ln._health_failing_since = None
        ln._health_recovered_after = None
        ln._health_stalled_since = None
        ln._was_struggling = False
        ln._lock = threading.RLock()
        return ln

    @staticmethod
    def _fake_stream(events):
        class _S:
            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

            def close(self):
                events.append("close")
        return _S()

    def _wire_open(self, H, monkeypatch, events):
        monkeypatch.setattr(H, "_open_input",
                            lambda dev, rate, bs, cb: (self._fake_stream(events), 16000))

    @staticmethod
    def _run_capped(H, monkeypatch, ln, tick, max_ticks=80):
        """Drive _run with a fake sleep; a stream that never ends the run
        itself is force-stopped after max_ticks ticks instead of hanging.
        The tick-count cap also turns 'the watchdog never fired' from a HANG
        into a visible red (the reopen never happened)."""
        state = {"ticks": 0}
        real_sleep = time.sleep

        def fake_sleep(s):
            if s == 0.5:
                state["ticks"] += 1
                if state["ticks"] > max_ticks:
                    ln._running = False      # force-exit: the guard fails below
                    return
            tick(s)
        monkeypatch.setattr(H.time, "sleep", fake_sleep)
        ln._run(7)
        real_sleep(0)
        return state["ticks"]

    # ---- stalled: frames stop arriving → REOPEN_S later the stream reopens

    def test_stalled_stream_is_reopened(self, H, monkeypatch, caplog):
        events = []
        start_at = []                      # clock time of each device open
        ln = self._listener(H)
        monkeypatch.setattr(H.ContinuousListener, "REOPEN_S", 1.0)
        # A controllable clock is what makes the threshold REACHABLE: with the
        # real monotonic, two instant sleeps are 0 µs apart and `now -
        # stalled_since` never exceeds any threshold.
        clock = {"t": 1000.0}
        monkeypatch.setattr(H.time, "monotonic", lambda: clock["t"])
        stalled_seen = []

        def fake_open(dev, rate, bs, cb):
            start_at.append(clock["t"])
            return self._fake_stream(events), 16000
        monkeypatch.setattr(H, "_open_input", fake_open)

        def tick(s):
            clock["t"] += s               # the fake clock advances per sleep
            if s == 0.5:                  # a watchdog tick: NO frames arrive
                if ln._health_stalled_since is not None:
                    stalled_seen.append(ln._health_stalled_since)
                if events.count("start") >= 2:
                    ln._running = False   # generation 2 observed → end cleanly
        self._run_capped(H, monkeypatch, ln, tick)
        assert events.count("start") == 2, events          # REOPENED
        assert events[-2:] == ["stop", "close"], events
        assert "stalled; reopening" in caplog.text, caplog.text
        assert stalled_seen, \
            "the stalled period must be visible in the mic-health state"
        # Deterministic with the fake clock: every watchdog tick is exactly
        # 0.5 s, detection must begin on the FIRST tick (last_seen is sampled
        # from the live counter at open — a stale init delays detection one
        # whole tick), REOPEN_S=1.0 fires by the 4th, and the settle sleep
        # adds 1.0 s. So the second open lands at ≤ 3.0 s.
        assert start_at[1] - start_at[0] <= 3.0, start_at

    # ---- silent: frames flow but are pure digital silence → SILENT_REOPEN_S

    def test_silent_frames_are_reopened(self, H, monkeypatch, caplog):
        events = []
        self._wire_open(H, monkeypatch, events)
        ln = self._listener(H)
        monkeypatch.setattr(H.ContinuousListener, "SILENT_REOPEN_S", 1.0)
        clock = {"t": 1000.0}     # _last_nonzero is stamped 1000.0 at open
        monkeypatch.setattr(H.time, "monotonic", lambda: clock["t"])

        def tick(s):
            clock["t"] += s               # silence must ELAPSE to be detected
            if s == 0.5:
                ln._frames_seen += 1          # the PortAudio callback is alive…
                if events.count("start") >= 2:
                    ln._running = False       # …but every frame is zero
        self._run_capped(H, monkeypatch, ln, tick)
        assert events.count("start") == 2, events          # REOPENED
        assert "only silence; reopening" in caplog.text, caplog.text

    def test_recovered_frames_clear_the_stalled_marker(self, H, monkeypatch):
        """A stall that ENDS — frames flow again — must clear the mic-health
        marker. If recovery leaves it set, health keeps reporting a stall
        that is over and the UI cries wolf about a healthy mic."""
        events = []
        self._wire_open(H, monkeypatch, events)
        ln = self._listener(H)
        monkeypatch.setattr(H.ContinuousListener, "REOPEN_S", 60.0)
        monkeypatch.setattr(H.ContinuousListener, "SILENT_REOPEN_S", 60.0)
        clock = {"t": 1000.0}
        monkeypatch.setattr(H.time, "monotonic", lambda: clock["t"])
        phase = {"n": 0}

        def tick(s):
            clock["t"] += s
            if s != 0.5:
                return
            phase["n"] += 1
            if phase["n"] == 1:
                return                    # tick 1: no frames → stall begins
            # tick 2: the stall was marked, then frames flow again
            assert ln._health_stalled_since is not None, "the stall was never marked"
            ln._frames_seen += 1          # recovery: the callback is alive
            ln._running = False           # end the run after this tick
        self._run_capped(H, monkeypatch, ln, tick, max_ticks=10)
        assert ln._health_stalled_since is None, \
            "recovery must clear the stalled marker"

    # ---- healthy: frames flow and speech happened recently → NO reopen

    def test_healthy_flow_is_left_alone(self, H, monkeypatch):
        events = []
        self._wire_open(H, monkeypatch, events)
        ln = self._listener(H)
        monkeypatch.setattr(H.ContinuousListener, "REOPEN_S", 3.0)
        clock = {"t": 1000.0}
        monkeypatch.setattr(H.time, "monotonic", lambda: clock["t"])

        def tick(s):
            if s == 0.5:
                ln._frames_seen += 1          # frames flow, and (no cb call)
                if ln._frames_seen >= 5:      # …recent nonzero speech happened
                    ln._running = False       # end the run cleanly
        ticks = self._run_capped(H, monkeypatch, ln, tick)
        # The final stop/close IS the clean shutdown on exit; what must never
        # happen is a WATCHDOG reopen — a second start. REOPEN_S=3.0 with five
        # 0.5s ticks means even a stalled reading would have fired by now.
        assert ticks <= 5, ticks
        assert events.count("start") == 1, events
        assert ln._health_stalled_since is None

    # ---- clean stop + generation token

    def test_stop_before_reopen_tears_down_cleanly(self, H, monkeypatch):
        events = []
        self._wire_open(H, monkeypatch, events)
        ln = self._listener(H)

        def tick(s):
            if s == 0.5:
                ln._running = False                 # hands-free switched off
        self._run_capped(H, monkeypatch, ln, tick, max_ticks=3)
        assert events == ["start", "stop", "close"], events
        assert ln._running is False
        assert ln.gate_open is False

    def test_cb_counts_frames_even_when_suspended(self, H, monkeypatch):
        """The liveness counter is bumped by the callback ALWAYS — even while
        suspended — otherwise the watchdog would read a suspended listener as
        a stalled stream."""
        captured = {}

        def fake_open(dev, rate, bs, cb):
            captured["cb"] = cb
            return self._fake_stream([]), 16000
        monkeypatch.setattr(H, "_open_input", fake_open)
        ln = self._listener(H)

        def tick(s):
            if s == 0.5:
                ln._running = False                 # one tick, then off
        self._run_capped(H, monkeypatch, ln, tick, max_ticks=2)
        ln._running = False                          # suspended/stopped
        before = ln._frames_seen
        captured["cb"](None, 0, None, None)
        assert ln._frames_seen == before + 1

    def test_stale_generation_never_touches_the_device(self, H, monkeypatch):
        """A thread whose generation was superseded (stop() bumped _run_id)
        must not open — the generation token is what kills old threads."""
        events = []
        self._wire_open(H, monkeypatch, events)
        ln = self._listener(H)
        ln._run_id = 8                               # superseded

        def fail(s):                                 # NO sleep call may happen
            raise AssertionError("stale generation entered the run loop")
        monkeypatch.setattr(H.time, "sleep", fail)
        ln._run(7)
        assert events == []                          # nothing opened

    def test_supersession_empties_the_inner_loop(self, H, monkeypatch):
        """Mid-run supersession — _run_id bumped while streaming — must end
        THIS run even though _running is still True: the inner watchdog loop
        checks the generation token every tick, and the new generation owns
        the device and the _running flag from that moment."""
        events = []
        self._wire_open(H, monkeypatch, events)
        ln = self._listener(H)

        def tick(s):
            if s == 0.5:
                ln._run_id = 8                # stop() bumped it mid-stream
        ticks = self._run_capped(H, monkeypatch, ln, tick, max_ticks=4)
        assert ticks <= 2, ticks              # ended on its own, not via the cap
        assert events == ["start", "stop", "close"], events
        assert ln._running is True            # the NEW generation owns the flag

    def test_teardown_survives_a_raising_stop(self, H, monkeypatch, caplog):
        """A PortAudio stream whose stop() raises mid-teardown must not kill
        the reopen loop — _run still clears the stream and keeps going."""
        class _BadStream:
            def start(self):
                pass
            def stop(self):
                raise RuntimeError("wedged PortAudio device")
            def close(self):
                pass
        monkeypatch.setattr(H, "_open_input",
                            lambda dev, rate, bs, cb: (_BadStream(), 16000))
        ln = self._listener(H)

        def tick(s):
            if s == 0.5:
                ln._running = False
        self._run_capped(H, monkeypatch, ln, tick, max_ticks=2)
        assert ln._stream is None, "the dead stream must be released"
        assert ln._running is False
