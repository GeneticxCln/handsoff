"""Audio capture, speech recognition, synthesis, and playback primitives.

This module deliberately has no dependency on :mod:`handsoff`.  The application
configures it once the settings and application logger exist; the monolith keeps
thin compatibility shims for its historical module-level names.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import wave
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import sounddevice as sd


SAMPLE_RATE = 16_000
WHISPER_SIZE = "base"
WHISPER_DEVICE = "auto"
WHISPER_MODEL_DIR = Path.home() / ".config" / "handsoff" / "whisper-model"
# Text-to-speech engine. Chatterbox-turbo (ResembleAI) replaced Piper: the
# weights are a 3.8 GB Hugging Face repo rather than a 60 MB .onnx voice, the
# model is neural and runs on the GPU, and it returns float samples at TTS_SR
# instead of writing a wav itself.
TTS_ENGINE = "chatterbox-turbo"
TTS_REPO_ID = "ResembleAI/chatterbox-turbo"
TTS_SR = 24_000                 # chatterbox's S3GEN_SR, fixed by the model
TTS_DEVICE = "auto"             # auto | cuda | cpu
TTS_REFERENCE = ""              # optional >=5 s clip; "" = the built-in voice
SETTINGS: dict = {"tts_rate": 1.0, "tts_volume": 1.0}
log = logging.getLogger("handsoff")

# Process-wide mic-operation ownership: InputStream construction and teardown
# serialize on MIC_OPERATION_LOCK. A bounded stop runs rec.stop() on an owner
# thread holding the lock; a timeout returns control WITHOUT touching the
# stream — the owner finishes the state transition and fires the recorder's
# _handsoff_stop_done finalizer (never abort a live native call from a second
# thread).
MIC_OPERATION_LOCK = threading.RLock()
_MIC_OPERATION_STATE_LOCK = threading.Lock()
_MIC_OPERATION_OWNER = None


def configure(*, sample_rate: int = 16_000, whisper_size: str = "base",
              whisper_device: str = "auto", whisper_model_dir: Path | None = None,
              tts_reference: str = "", tts_device: str = "auto",
              gpu_reclaim=None,
              settings: dict | None = None,
              logger: logging.Logger | None = None) -> None:
    """Set application-owned paths/settings without importing the application."""
    global SAMPLE_RATE, WHISPER_SIZE, WHISPER_DEVICE
    global WHISPER_MODEL_DIR, TTS_REFERENCE, TTS_DEVICE, SETTINGS, log
    global _GPU_RECLAIM
    SAMPLE_RATE = int(sample_rate)
    WHISPER_SIZE = str(whisper_size)
    WHISPER_DEVICE = str(whisper_device)
    if whisper_model_dir is not None:
        WHISPER_MODEL_DIR = Path(whisper_model_dir)
    TTS_REFERENCE = str(tts_reference or "")
    TTS_DEVICE = str(tts_device or "auto")
    if gpu_reclaim is not None:
        _GPU_RECLAIM = gpu_reclaim if callable(gpu_reclaim) else None
    if settings is not None:
        SETTINGS = settings
    if logger is not None:
        log = logger


def _resample_to_16k(data: np.ndarray, rate: int) -> np.ndarray:
    """Resample flat int16 audio to SAMPLE_RATE (FFT brickwall, anti-aliased).

    Linear interpolation folds everything above 8 kHz back into the voice
    band (a 12 kHz whine lands on 4 kHz at full strength); truncating the
    spectrum instead is a near-ideal lowpass for any ratio with no new
    dependency. A 60 s capture is ~12 MB — no chunking needed."""
    if rate == SAMPLE_RATE or data.size == 0:
        return data
    n_in = int(data.size)
    n_out = max(1, int(round(n_in * SAMPLE_RATE / float(rate))))
    spectrum = np.fft.rfft(data.astype(np.float32))
    kept = np.zeros(n_out // 2 + 1, dtype=np.complex64)
    m = min(len(spectrum), len(kept))
    kept[:m] = spectrum[:m]
    out = np.fft.irfft(kept, n_out) * (n_out / n_in)
    return np.clip(out, -32768, 32767).astype(np.int16)


def _open_input(device, rate: int, blocksize: int, cb) -> tuple:
    """Open a mono int16 stream, retrying once at the native device rate."""
    try:
        return sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                              blocksize=blocksize, callback=cb, device=device), rate
    except Exception as first_error:
        try:
            info = (sd.query_devices(device, kind="input") if device is not None
                    else sd.query_devices(kind="input"))
            native = int(float(info.get("default_samplerate") or rate))
        except Exception:
            native = rate
        if native == rate:
            raise
        try:
            stream = sd.InputStream(samplerate=native, channels=1, dtype="int16",
                                    blocksize=blocksize, callback=cb, device=device)
        except Exception as retry_error:
            # Chaining keeps the ORIGINAL cause: on its own the retry's error
            # (e.g. "invalid sample rate") hides the real one ("device busy"),
            # which is exactly what a user needs to see.
            raise retry_error from first_error
        return stream, native


class Recorder:
    """16 kHz mono int16 microphone capture with live RMS levels."""

    MAX_PTT_S = 60.0  # ponytail: a stuck press cannot grow memory forever.

    def __init__(self, on_level: "callable", device: str | None = None,
                 threshold: int = 600) -> None:
        self._on_level = on_level
        self._device = device or None
        self._threshold = threshold
        self._frames: list[np.ndarray] = []
        self._samples = 0
        self._level = 0.0
        self._stream: sd.InputStream | None = None
        # Guards _frames/_samples between the PortAudio callback thread and the
        # thread calling start()/stop(). It is never held across stream.stop():
        # PortAudio joins the callback there, so taking the lock around it could
        # deadlock against _cb trying to append.
        self._buf_lock = threading.Lock()

    def start(self) -> None:
        # Close any stream from a previous start FIRST: dropping the reference
        # leaked the device (PortAudio kept it open) and a double-start could
        # have two callbacks appending into the same buffer.
        self._close_stream()
        with self._buf_lock:
            self._frames = []
            self._samples = 0
            self._level = 0.0
        self._stream, self._native_rate = _open_input(
            self._device, SAMPLE_RATE, 1024, self._cb)
        try:
            self._stream.start()
        except BaseException:
            self._close_stream()
            raise

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:
            log.exception("failed to close input stream")

    def _cb(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("audio input: %s", status)
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        with self._buf_lock:
            self._frames.append(indata.copy())
            self._samples += int(indata.size)
            cap = int(self.MAX_PTT_S * float(getattr(self, "_native_rate", SAMPLE_RATE)))
            while self._frames and self._samples > cap:
                old = self._frames.pop(0)
                self._samples -= int(old.size)
            self._level = 0.25 * rms + 0.75 * self._level
            level = self._level
        try:
            self._on_level(min(1.0, level / 2000.0))
        except Exception:
            pass

    def stop(self) -> np.ndarray | None:
        # Stop the stream before touching the buffer: stop() returns only once
        # the callback has finished, which is what makes the snapshot below
        # whole rather than a list being mutated mid-concat.
        self._close_stream()
        with self._buf_lock:
            frames = self._frames
            self._frames = []
            self._samples = 0
        if not frames:
            return None
        audio = np.concatenate(frames).reshape(-1)
        return _resample_to_16k(audio, getattr(self, "_native_rate", SAMPLE_RATE))


def _stop_recorder_bounded(rec, timeout: float = 3.0):
    """Stop without aborting a live native call from a second thread.

    The stop thread owns the process-wide mic lock until rec.stop() returns.
    A timeout only returns control to the caller; it never touches the
    stream. The owner thread performs the final state transition.
    """
    box: dict = {}

    def _call() -> None:
        global _MIC_OPERATION_OWNER
        MIC_OPERATION_LOCK.acquire()
        with _MIC_OPERATION_STATE_LOCK:
            _MIC_OPERATION_OWNER = threading.current_thread()
            try:
                rec._handsoff_stop_owner = _MIC_OPERATION_OWNER
            except Exception:
                pass
        try:
            try:
                box["audio"] = rec.stop()
            except Exception:
                log.exception("recorder stop failed")
                box["audio"] = None
            # A bounded stop that gave up is not the same as a silent press:
            # this owner thread is still holding the mic and will hand back the
            # utterance the caller stopped waiting for. It cannot be delivered
            # any more (the turn is gone), so it is SAID rather than dropped,
            # otherwise "push-to-talk did nothing" has no trace to explain it.
            if box.get("abandoned"):
                late = box.get("audio")
                log.warning(
                    "recorder stop finished after the caller gave up — "
                    "discarding a late capture of %d frames (missed utterance)",
                    0 if late is None else len(late))
        finally:
            callback = getattr(rec, "_handsoff_stop_done", None)
            with _MIC_OPERATION_STATE_LOCK:
                _MIC_OPERATION_OWNER = None
                try:
                    rec._handsoff_stop_owner = None
                except Exception:
                    pass
            try:
                if callable(callback):
                    callback()
            except Exception:
                log.exception("recorder stop finalizer failed")
            MIC_OPERATION_LOCK.release()

    th = threading.Thread(target=_call, name="ptt-stop-native", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        # Tell the owner its result will not be collected, so the late capture
        # is reported instead of vanishing with the thread.
        box["abandoned"] = True
        return None, True
    return box.get("audio"), False


_whisper_model = None
_whisper_device_used = ""        # what the LOADED model actually got
_whisper_lock = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()
_whisper_cpu_fallback = False
_CUDA_ERR_RE = re.compile(r"cuda|cublas|cudnn", re.IGNORECASE)
_tts_model = None
_tts_device = ""
_tts_lock = threading.Lock()
# Generation is serialized separately from loading: the model holds per-voice
# conditionals as mutable state (`self.conds`), and two generations can overlap
# — a spoken reply and the settings app's voice preview. Loading does not hold
# this, so a slow first load never blocks a reply that is already speaking.
_TTS_RUN_LOCK = threading.Lock()
# Measured, not guessed: turbo's weights are ~2.7 GB, but the PROCESS holds
# 3 172–3 312 MiB on the card while generating (nvidia-smi for our pid on this
# 16 GB card, whisper on cpu so the figure is speech alone — the rest is the
# CUDA context and the per-voice conditionals). The budget reserves the larger
# number, because the smaller one is what the model occupies at rest rather
# than what asking it to speak takes.
_TTS_VRAM_MB = 3_400
_TTS_FLOAT32_PATCHED = False
_WHISPER_VRAM_MB = {
    "tiny": 600, "base": 800, "small": 1400, "medium": 2600,
    "large": 3600, "large-v1": 3600, "large-v2": 3600, "large-v3": 3600,
    "turbo": 3000,
}


def _nvidia_free_vram_mb() -> "int | None":
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().splitlines()[0].strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


# The room a loader must leave unused on the card. The compositor, the
# wallpaper and the settings window all draw from the same GPU, and a card with
# nothing left free is how the desktop starts glitching rather than the model
# failing to load — so a claim that would take the last of the card is refused
# before it is made, by one comparison rather than two.
_VRAM_RESERVE_MB = 1024

# How the speech model asks another tenant for the card before it gives way.
# The host injects this through `configure(gpu_reclaim=...)` because what a
# reclaim COSTS (the LLM it evicts has to load again on the next question, and
# that price is the application's to measure and weigh) belongs to the host,
# while asking for it belongs to the loader. None means "no such policy here" —
# a partial install, the settings app, a bundle without the host — and then the
# refusal simply stands.
_GPU_RECLAIM = None


def _int_or_none(value) -> "int | None":
    """The reading as an int, or None when it is not a number at all.

    `N/A`, `[N/A]`, a blank field and a junk value are not zero free memory and
    not a figure in MB: they are the ABSENCE of a measurement, and the loader's
    response to that is the same as to an unreadable card. `inf`/`nan` are here
    because Python's json module parses bare `Infinity` into a float.
    """
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return None


def vram_budget(free_mb, *, claim_mb, owner: str, entitled_mb=0,
                reserve_mb=_VRAM_RESERVE_MB) -> dict:
    """ONE budget for the card, consulted by both model loaders.

    Two loaders each comparing their own size against the same free-VRAM
    reading can each pass while the card can hold only one of them: 3.4 GB of
    speech and 3.9 GB of whisper "fit" in 4.5 GB free when neither decision
    knows about the other. This is the single arithmetic both ask instead, so a
    claim is refused when it would not fit after the reserve and after the
    memory another tenant is entitled to.

    `free_mb` is the driver's LIVE reading, so it already excludes whatever
    this process has resident. A caller passes `entitled_mb` only for a model
    that is not loaded yet: withholding a loaded model's size again would
    reserve its memory twice.

    `entitled_mb` is the asymmetry between the two tenants, in one place:
    whisper yields to the speech model (a spoken reply is what the user is
    waiting for; the ears can lose a few hundred ms of latency), and speech
    yields to nothing — a resident whisper is inside `free_mb` already. An
    unreadable card is unknown room, never room: `free_mb is None` refuses the
    claim, so the two loaders cannot answer "can't tell" differently.

    The result carries its own arithmetic, because the caller has to be able to
    say WHY a model went to cpu instead of leaving the choice unexplained.
    """
    claim = max(0, _int_or_none(claim_mb) or 0)
    entitled = max(0, _int_or_none(entitled_mb) or 0)
    reserve = max(0, _int_or_none(reserve_mb) or 0)
    free = _int_or_none(free_mb)
    held = f"{reserve} MB reserve"
    if entitled:
        held += f" and {entitled} MB held back for the speech model"
    out = {"owner": str(owner), "claim_mb": claim, "entitled_mb": entitled,
           "reserved_mb": reserve, "free_mb": free, "available_mb": None,
           "fits": False, "reason": ""}
    if free is None:
        out["reason"] = (f"{claim} MB claim refused: the card's free memory "
                         f"could not be read, and an unreadable card is not "
                         f"room")
        return out
    available = free - reserve - entitled
    out["available_mb"] = available
    out["fits"] = claim <= available
    if out["fits"]:
        out["reason"] = (f"{claim} MB claim fits: {free} MB free less {held} "
                         f"= {available} MB available")
    else:
        out["reason"] = (f"{claim} MB claim refused: {claim + reserve + entitled} MB "
                         f"needed ({held}) but only {free} MB is free")
    return out


def yield_to_llm_verdict(free_mb, claim_mb, *, held_mb,
                         reserve_mb=_VRAM_RESERVE_MB) -> dict:
    """Should the models THIS process holds give the card to the LLM? Why?

    The mirror of `_ask_for_the_card`. There the speech loader was refused and
    asked the LLM to move; here a TURN needs the card and the tenant that can
    move is this process's own speech model — the cheap one to reload (seconds),
    where Ollama's own answer to a full card is to offload half the model to the
    CPU and serve every token at a fraction of the speed.

    `held_mb` is what this process occupies on the card (`gpu_footprint_mb`),
    `claim_mb` what the LLM needs (the model's own size, from Ollama), and
    `free_mb` the driver's reading — which already EXCLUDES `held_mb`, so the
    question "would releasing help?" is the same budget asked again with that
    memory put back, rather than a second kind of arithmetic.

    Four ways this answers NO, and each is a different fact:

      * nothing of ours is on the card (cpu speech, nothing loaded) — there is
        nothing to give back;
      * the claim fits as things stand — the card does not need the memory, and
        releasing it would cost a reload on the next reply for nothing;
      * the card could not be read — an unreadable card is not pressure, and
        what this decision spends is a reload, so an unknown is not a reason to
        spend it (the speech path releases on an unknown because its alternative
        is a stalled utterance; this one's alternative is only that Ollama makes
        the offload decision for itself);
      * the claim does not fit even with the memory back — the model is larger
        than the card, or someone else holds the rest, so the release would hand
        back memory that cannot change the outcome.

    NO is also the answer when `claim_mb` is unknown: nothing was weighed, and
    evicting a voice on a guess is exactly the kind of unmeasured decision this
    pair of functions exists to avoid.

    `tight` in the result means "the claim does not fit as things stand", which
    is the condition under which the caller has something worth saying in the
    journal — the mundane answers (fits already, nothing held) are the common
    case and are not news.
    """
    free = _int_or_none(free_mb)
    claim = _int_or_none(claim_mb)
    held = max(0, _int_or_none(held_mb) or 0)
    out = {"yield": False, "tight": False, "held_mb": held, "claim_mb": claim,
           "free_mb": free, "available_after_mb": None, "note": ""}
    if held <= 0:
        out["note"] = ("this process holds nothing on the card, so a turn has "
                       "nothing to ask it for")
        return out
    if claim is None:
        out["note"] = ("how much the LLM needs could not be read, so nothing "
                       f"was asked for the card ({held} MB is held here)")
        return out
    if free is None:
        out["note"] = ("the card's free memory could not be read, so the "
                       f"speech model was not asked for it ({held} MB is held "
                       "here)")
        return out
    already = vram_budget(free, claim_mb=claim, owner="llm",
                          reserve_mb=reserve_mb)
    if already["fits"]:
        out["note"] = f"the LLM's claim fits already: {already['reason']}"
        return out
    after = vram_budget(free + held, claim_mb=claim, owner="llm",
                        reserve_mb=reserve_mb)
    out["tight"] = True
    out["available_after_mb"] = after["available_mb"]
    if not after["fits"]:
        out["note"] = (f"releasing the {held} MB this process holds would not "
                       f"make room: {after['reason']}")
        return out
    out["yield"] = True
    out["note"] = (f"{held} MB of this card is this process's own models, and "
                   f"the LLM's claim fits once they are back "
                   f"({after['reason']})")
    return out


def _whisper_vram_mb(size: str) -> int:
    """The table's footprint for a whisper size, defaulting to the largest."""
    return int(_WHISPER_VRAM_MB.get(str(size or "").lower(), 3600))


def _speech_vram_claim_mb() -> int:
    """What the speech model is entitled to before whisper's claim.

    Zero when speech is configured for cpu (nothing will want the card), and
    zero once it is resident on cuda — its memory is inside the driver's free
    reading by then, so reserving it a second time would withhold room nobody
    is using. Anything else (`auto`, an explicit `cuda`) means the voice is
    still coming and whisper must leave it room.
    """
    if str(TTS_DEVICE).strip().lower() == "cpu":
        return 0
    if _tts_model is not None and str(_tts_device or "").strip().lower() == "cuda":
        return 0
    return _TTS_VRAM_MB


def _ask_for_the_card(plan: dict, device: str = "auto") -> dict:
    """Ask the other tenant for the card before the speech model gives way.

    A refusal here means the card had no room left, and the tenant usually
    holding it is the LLM — which the HOST can ask to let go (`keep_alive: 0`).
    The policy is the host's, so this calls the injected hook and then re-plans
    against a reading taken AFTERWARDS: a reclaim that frees nothing must never
    turn a refusal into a claim, and a reclaim cannot be a way to guess at a
    card that could not be read in the first place.

    The hook's own sentence is kept in `plan["reclaim"]` so the loader can say
    in the journal WHICH tenant gave way — the LLM, or the speech model.
    Whisper does NOT ask: it is the ears, it yields to speech by design, and a
    startup whisper load that evicted the LLM would fight the model the bubble
    had just warmed.
    """
    reclaim = _GPU_RECLAIM
    if reclaim is None or plan["free_mb"] is None:
        return plan
    try:
        outcome = reclaim(plan["reason"])
    except Exception:
        log.warning("the gpu reclaim hook raised — the refusal stands",
                    exc_info=True)
        return plan
    note = str(outcome.get("detail") or "") if isinstance(outcome, dict) else ""
    gave_way = bool(outcome.get("gave_way")) if isinstance(outcome, dict) else False
    if not gave_way:
        if note:
            plan["reclaim"] = note
        return plan
    again = _tts_plan(_nvidia_free_vram_mb(), device)
    again["reclaim"] = note
    return again


def _whisper_plan(size: str, free_mb: "int | None") -> dict:
    """The whisper device AND the budget that decided it."""
    plan = vram_budget(free_mb, claim_mb=_whisper_vram_mb(size),
                       owner="whisper", entitled_mb=_speech_vram_claim_mb())
    plan["device"] = "cuda" if plan["fits"] else "cpu"
    plan["compute"] = "float16" if plan["fits"] else "int8"
    return plan


def _whisper_device_choice(size: str, free_mb: "int | None") -> tuple[str, str]:
    """The device and compute type for a whisper size on this card.

    Kept as a two-value answer because the host's partial-install shim and the
    no-audio probe in the suite both call it that way; the reason travels in
    `_whisper_plan`, which is what the loader logs.
    """
    plan = _whisper_plan(size, free_mb)
    return (plan["device"], plan["compute"])


def _empty_cuda_cache() -> bool:
    """Hand torch's cached blocks back, without importing torch to do it.

    Looked up in `sys.modules` rather than imported: this runs on an idle tick,
    and the suite asserts that a test never drags torch in. When nothing has
    imported torch there is no allocator to empty and no allocation to give
    back, so the answer is simply False.
    """
    torch = sys.modules.get("torch")
    cuda = getattr(torch, "cuda", None) if torch is not None else None
    if cuda is None:
        return False
    try:
        cuda.empty_cache()
    except Exception:
        log.debug("torch.cuda.empty_cache() failed", exc_info=True)
        return False
    return True


def drop_models(logger=None) -> dict:
    """Drop the loaded models so their memory goes back to the machine.

    This is what an idle bubble calls (handsoff's `_idle_release_tick`), and it
    is safe by construction rather than by timing luck:

      * both locks are taken NON-blocking, because a load or a generation owns
        the model object it is using. A busy lock means "not now, ask again on
        the next tick" — never a blocked tick or a torn model.
      * whisper is dropped only when it was loaded on CUDA. On cpu it holds no
        GPU memory, and dropping it would cost a reload on the next turn for
        nothing.
      * `_tts_device` is cleared with the model, so the NEXT load re-decides
        cuda-vs-cpu against the free memory of that moment (which is the whole
        point of having released anything).

    Nothing else is required for the bubble to keep working: `get_tts`,
    `get_whisper` and the chat path all load on demand, so the next utterance
    or turn pays a load and nothing else changes. Returns what was dropped.
    """
    global _tts_model, _tts_device, _whisper_model, _whisper_device_used
    out = {"tts": False, "whisper": False, "cache_cleared": False}
    if _TTS_RUN_LOCK.acquire(blocking=False):
        try:
            with _tts_lock:
                if _tts_model is not None:
                    _tts_model = None
                    _tts_device = ""
                    out["tts"] = True
        finally:
            _TTS_RUN_LOCK.release()
    if _whisper_device_used == "cuda" and _TRANSCRIBE_LOCK.acquire(blocking=False):
        try:
            with _whisper_lock:
                if _whisper_model is not None:
                    _whisper_model = None
                    _whisper_device_used = ""
                    out["whisper"] = True
        finally:
            _TRANSCRIBE_LOCK.release()
    if out["tts"] or out["whisper"]:
        out["cache_cleared"] = _empty_cuda_cache()
        (logger or log).info(
            "released idle models (tts=%s whisper=%s cuda cache=%s)",
            out["tts"], out["whisper"], out["cache_cleared"])
    return out


def gpu_footprint_mb() -> dict:
    """Which of this process's loaded models occupy the card, and how much.

    The ESTIMATE half of the host's headroom line. `nvidia-smi` can usually say
    what a pid holds, but not on every driver, and a bubble that cannot be
    measured must still be able to say what it believes it is holding instead
    of reporting nothing.

    Only a model loaded on CUDA contributes: a model on cpu occupies no card
    memory, so counting it would make the number wrong in exactly the case the
    idle release exists for (and would claim a release could hand back memory
    it never had). Sizes come from the same tables the device choices use
    (`_TTS_VRAM_MB`, `_WHISPER_VRAM_MB`), so this cannot drift from what the
    loaders decided a model needs. These are FOOTPRINTS, not measurements —
    the measured number is the one the driver attributes to the pid.

    The devices are reported even at zero, because "tts on cpu" is the fact
    that explains the zero, and a reader who sees only `0` cannot tell a cpu
    model from a missing one.
    """
    tts_loaded = _tts_model is not None
    whisper_loaded = _whisper_model is not None
    tts_device = (_tts_device or "cpu") if tts_loaded else ""
    whisper_device = (_whisper_device_used or "cpu") if whisper_loaded else ""
    tts_mb = _TTS_VRAM_MB if tts_device == "cuda" else 0
    whisper_mb = (_WHISPER_VRAM_MB.get(str(WHISPER_SIZE).lower(), 3600)
                  if whisper_device == "cuda" else 0)
    return {
        "tts_mb": tts_mb,
        "whisper_mb": whisper_mb,
        "total_mb": tts_mb + whisper_mb,
        "tts_loaded": tts_loaded,
        "whisper_loaded": whisper_loaded,
        "tts_device": tts_device,
        "whisper_device": whisper_device,
    }


def get_whisper():
    """The loaded whisper model, or a RuntimeError that names the cause.

    `local_files_only=True` means a missing or unreadable model directory can
    never be fetched on the fly: the loader raises, and the *bare* library
    error ("Unable to open file 'model.bin'") names neither the directory nor
    the fix. Every failure here is wrapped so the message the user is told —
    `_loader` at startup, the pipeline's turn-failure report — says which
    model, from where, and what to do. Nothing is cached: the model stays None
    after a failure, so the next attempt (after install.sh) retries cleanly.
    """
    global _whisper_model, _whisper_device_used
    with _whisper_lock:
        if _whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                device, compute, why = "cpu", "int8", ""
                if not _whisper_cpu_fallback:
                    if WHISPER_DEVICE == "cuda":
                        device, compute = "cuda", "float16"
                    elif WHISPER_DEVICE == "auto":
                        plan = _whisper_plan(WHISPER_SIZE, _nvidia_free_vram_mb())
                        device, compute = plan["device"], plan["compute"]
                        if device == "cpu":
                            why = plan["reason"]
                if why:
                    # The fallback costs latency on every utterance, so it is
                    # said at WARNING with its own arithmetic rather than
                    # buried in the load line.
                    log.warning("whisper '%s' on cpu — %s", WHISPER_SIZE, why)
                log.info("loading whisper '%s' on %s (%s) from %s",
                         WHISPER_SIZE, device, compute, WHISPER_MODEL_DIR)
                try:
                    _whisper_model = WhisperModel(
                        WHISPER_SIZE, device=device, compute_type=compute,
                        download_root=str(WHISPER_MODEL_DIR), local_files_only=True)
                    _whisper_device_used = device
                except Exception:
                    if device == "cpu":
                        raise
                    log.exception("whisper %s load failed — falling back to cpu", device)
                    _whisper_model = WhisperModel(
                        WHISPER_SIZE, device="cpu", compute_type="int8",
                        download_root=str(WHISPER_MODEL_DIR), local_files_only=True)
                    _whisper_device_used = "cpu"
            except ImportError as exc:
                raise RuntimeError(
                    f"faster_whisper is not installed ({exc}) — run install.sh") from exc
            except Exception as exc:
                raise RuntimeError(
                    f"cannot load the whisper '{WHISPER_SIZE}' model from "
                    f"{WHISPER_MODEL_DIR} ({type(exc).__name__}: {exc}) — the "
                    f"model directory is missing, unreadable or damaged; run "
                    f"install.sh to re-download it") from exc
        return _whisper_model


MIN_REFERENCE_S = 5.0


def reference_clip_seconds(path) -> "float | None":
    """Duration of a voice reference clip, or None when it cannot be read.

    Only wav can be measured with the standard library, and the engine also
    accepts flac/mp3/m4a — so None means "unknown", never "invalid".
    """
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return None


def reference_problem(path) -> "str | None":
    """Why the engine will refuse `path` as a reference clip, or None.

    `prepare_conditionals` asserts the clip is longer than MIN_REFERENCE_S, and
    that assert fires on EVERY turn — so a two-second clip is not a degraded
    voice, it is a mute bubble. Both the bubble and the settings app ask this
    one question, so both give the same answer.
    """
    p = Path(str(path))
    if not p.exists():
        return f"voice clip {p} does not exist"
    seconds = reference_clip_seconds(p)
    if seconds is not None and seconds <= MIN_REFERENCE_S:
        return (f"voice clip {p.name} is {seconds:.1f}s — {TTS_ENGINE} needs "
                f"longer than {MIN_REFERENCE_S:.0f}s")
    return None


def hf_hub_cache() -> Path:
    """The Hugging Face hub cache directory huggingface_hub would use.

    Computed from the environment rather than imported from huggingface_hub:
    doctor and the installer both ask this question, and neither should pay a
    multi-second import to answer it.
    """
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(var)
        if value:
            return Path(value).expanduser()
    hf_home = os.environ.get("HF_HOME")
    base = (Path(hf_home).expanduser() if hf_home
            else Path.home() / ".cache" / "huggingface")
    return base / "hub"


def tts_weights_dir() -> Path:
    """Where the speech model's weights live once downloaded."""
    return hf_hub_cache() / ("models--" + TTS_REPO_ID.replace("/", "--"))


def tts_weights_cached() -> bool:
    """True when the model's weights are already on disk.

    A missing blob is the difference between a 10 s startup and a 3.8 GB
    download, so the installer, doctor and the settings app all report it
    rather than discovering it mid-turn.
    """
    try:
        root = tts_weights_dir()
        snapshots = root / "snapshots"
        if not snapshots.is_dir():
            return False
        for snap in snapshots.iterdir():
            if not snap.is_dir():
                continue
            if any(p.suffix == ".safetensors" and p.stat().st_size > 0
                   for p in snap.iterdir() if p.is_file()):
                return True
        return False
    except OSError:
        return False


def _torch_cuda_available() -> bool:
    """True only when torch reports a usable CUDA device. Never raises."""
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _tts_plan(free_mb: "int | None", device: str = "auto") -> dict:
    """The speech device AND the budget that decided it.

    `auto` puts the model on the card only when the shared budget says it fits
    after the reserve. An unreadable nvidia-smi is not evidence of a GPU, so it
    means cpu — and the caller says so LOUDLY, because CPU synthesis is not
    real time and a spoken turn may not keep up.

    An explicitly configured device is not a budget question — the user chose
    it — but the budget is still computed and reported, because "configured
    cuda with 1.4 GB free" is the fact that explains a load failing a moment
    later.
    """
    plan = vram_budget(free_mb, claim_mb=_TTS_VRAM_MB, owner="speech")
    configured = str(device).strip().lower()
    if configured in ("cuda", "cpu"):
        plan["device"] = configured
        plan["authority"] = "configured"
        return plan
    plan["authority"] = "budget"
    if not _torch_cuda_available():
        plan["device"] = "cpu"
        plan["reason"] = ("no CUDA device is visible to torch, so the card's "
                          "free memory is not the question")
        return plan
    plan["device"] = "cuda" if plan["fits"] else "cpu"
    return plan


def tts_device_choice(free_mb: "int | None", device: str = "auto") -> str:
    """Which device to load the speech model on (`auto` asks the budget)."""
    return _tts_plan(free_mb, device)["device"]


def _patch_float32_norm(model) -> None:
    """Keep the model's loudness normalisation in float32.

    Measured on this environment (NumPy 2.5.3, chatterbox-tts 0.1.7):
    `norm_loudness` computes `wav * gain_linear`, which NumPy 2 promotes to
    float64, and `s3tokenizer.forward`'s mel matmul then raises
    "expected scalar type Float but found Double" — so EVERY reference clip
    fails, not merely an odd one. Casting at this single seam repairs the whole
    path (verified: as-is raises, cast returns tokens (1, 250)).
    """
    global _TTS_FLOAT32_PATCHED
    original = getattr(model, "norm_loudness", None)
    if original is None or not callable(original):
        return

    def norm_loudness_float32(wav, sr, *args, **kwargs):
        return np.asarray(original(wav, sr, *args, **kwargs), dtype=np.float32)

    try:
        model.norm_loudness = norm_loudness_float32
    except (AttributeError, TypeError):   # slots/proxy: leave the model alone
        return
    _TTS_FLOAT32_PATCHED = True


# NOTE on xformers (measured, so nobody re-opens this): xformers 0.0.35 is the
# newest release and its C++/CUDA extension is built against torch 2.10+cu128,
# so with the installed torch 2.11+cu130 it imports but every op raises
# NotImplementedError. It is also unnecessary: the only importer here is
# diffusers' optional-dependency probe, nothing in the speech path calls it, and
# the attention kernel the CUDA profiler actually observes during generation is
# `fmha_cutlassF_f32...PyTorchMemEffAttention` — xformers' memory-efficient
# attention, which upstreamed into PyTorch and is reached through SDPA. The
# fp32 variant (rather than FlashAttention, which needs fp16/bf16) is forced by
# the engine running in float32: `from_pretrained(device)` offers no dtype and
# a hand-cast fails inside its own graph (mat1/mat2 Float vs Half).

def get_tts():
    """The loaded speech model, or a RuntimeError naming the cause.

    Mirrors `get_whisper`'s contract, because the failure modes are the same
    and they arrive on the hottest path in the app: a failure names the engine,
    the device and the remedy (the bare library error names none of them), and
    nothing is cached — the model stays None so the next attempt after
    `install.sh` retries cleanly instead of leaving the bubble mute forever.
    """
    global _tts_model, _tts_device
    with _tts_lock:
        if _tts_model is None:
            try:
                from chatterbox.tts_turbo import ChatterboxTurboTTS
            except ImportError as exc:
                raise RuntimeError(
                    f"chatterbox is not installed ({exc}) — run install.sh") from exc
            plan = _tts_plan(_nvidia_free_vram_mb(), TTS_DEVICE)
            if plan["device"] == "cpu" and plan["authority"] == "budget":
                plan = _ask_for_the_card(plan, TTS_DEVICE)
            device = plan["device"]
            # Which tenant moved is the fact that explains what the next
            # question will cost, so it is said in the journal either way.
            reclaim = plan.get("reclaim") or ""
            if reclaim and device == "cuda":
                log.info("speech model takes the card — %s", reclaim)
            if TTS_REFERENCE:
                # Checked BEFORE the load: the message below is about weights,
                # and a bad clip would otherwise be reported as "the weights are
                # damaged", sending the user after the wrong thing entirely.
                problem = reference_problem(TTS_REFERENCE)
                if problem:
                    raise RuntimeError(
                        f"{problem} — clear the voice clip in Settings → Voice "
                        f"to use the built-in voice, or pick a longer one")
            if device == "cpu":
                log.warning(
                    "chatterbox is loading on CPU: synthesis is slower than "
                    "real time there (measured ~3x FASTER than real time on a "
                    "GPU), so spoken replies will lag behind the conversation")
                if plan["authority"] == "budget":
                    log.warning("speech model fell back to cpu — %s%s",
                                plan["reason"],
                                f"; {reclaim}" if reclaim else "")
            elif plan["authority"] == "configured" and not plan["fits"]:
                log.warning("speech model is configured on %s — %s",
                            device, plan["reason"])
            log.info("loading %s on %s (voice: %s)", TTS_ENGINE, device,
                     TTS_REFERENCE or "built-in")
            try:
                model = ChatterboxTurboTTS.from_pretrained(device=device)
                _patch_float32_norm(model)
                if TTS_REFERENCE:
                    model.prepare_conditionals(TTS_REFERENCE)
                    log.info("voice conditionals prepared from %s", TTS_REFERENCE)
                _tts_model = model
                _tts_device = device
            except Exception as exc:
                raise RuntimeError(
                    f"cannot load {TTS_ENGINE} on {device} "
                    f"({type(exc).__name__}: {exc}) — the model weights are "
                    f"missing, unreadable or damaged, or the card cannot hold "
                    f"them (needs ~{_TTS_VRAM_MB} MB); run install.sh to "
                    f"download them") from exc
        return _tts_model


def _as_float_samples(wav) -> np.ndarray:
    """Whatever the engine returned, as flat float32.

    Chatterbox hands back a torch tensor shaped (1, N) (and a plain numpy
    array from a test double), so both shapes are accepted here rather than
    making every caller depend on torch.
    """
    if hasattr(wav, "detach"):                     # torch tensor
        wav = wav.detach().cpu().numpy()
    data = np.asarray(wav, dtype=np.float32)
    if data.ndim > 1:
        data = data.reshape(-1) if data.shape[0] == 1 else data[:, 0]
    return np.ascontiguousarray(data, dtype=np.float32)


def resample_speed(samples: np.ndarray, speed: float) -> np.ndarray:
    """Rewrite `samples` to play `speed` times faster under an unchanged header.

    Time-compression, so the pitch rises (a tape rewind); that is the honest
    trade for an engine with no duration control. Anti-aliased where torch is
    importable (a folded alias is audible on a voice), and a plain linear
    interpolation otherwise so the path never depends on torch being present.
    """
    # A non-finite rate is not merely useless: `int(round(n / nan))` raises, so
    # a NaN reaching this exported helper would crash the render path. The
    # production caller already filters with _clamp_setting; this keeps the
    # function honest on its own terms.
    if (not np.isfinite(speed) or speed <= 0 or samples.size == 0
            or abs(speed - 1.0) < 1e-3):
        return samples
    n_out = max(1, int(round(samples.size / float(speed))))
    if n_out == samples.size:
        return samples
    try:
        import torch
        import torchaudio.functional as AF
        out = AF.resample(torch.from_numpy(np.asarray(samples, dtype=np.float32)),
                          TTS_SR, max(1, int(round(TTS_SR / float(speed)))))
        return np.asarray(out.numpy(), dtype=np.float32)
    except Exception:
        x = np.arange(samples.size, dtype=np.float64)
        xi = np.linspace(0.0, samples.size - 1.0, num=n_out)
        return np.interp(xi, x, samples).astype(np.float32)


# Short on purpose: the token decoder is the dominant per-utterance cost, so a
# long warm-up would add startup time to save less of it.
WARM_TEXT = "Ready."


def warm_tts(model=None) -> int:
    """Synthesize a throwaway phrase so the first real reply is a warm one.

    Measured on this machine (16 GB card, chatterbox-turbo, fp32): the FIRST
    synthesis after a load takes ~1.5 s to first audio through the bubble and
    every later one ~0.6 s — the decoder, the flow sampler and the vocoder all
    compile and autotune their CUDA kernels on first use. Startup already pays
    the load, so this finishes the job while nothing is waiting on a reply.

    Returns the number of samples produced, or 0 when there is nothing to warm
    (no model loaded: a failed load, a partial install, or a test double).
    Never raises for that case — the caller logs and carries on.
    """
    if model is None:
        return 0
    return int(synthesize(WARM_TEXT, model=model).size)


def _clamp_setting(key: str, low: float, high: float, default: float) -> float:
    """A settings value as a bounded float; junk becomes the default."""
    try:
        value = float(SETTINGS.get(key, default))
    except (TypeError, ValueError):
        return default
    if not np.isfinite(value):
        return default
    return min(max(value, low), high)


def synthesize(text: str, model=None) -> np.ndarray:
    """One utterance as flat int16 samples at TTS_SR, with both knobs applied.

    `generate` is serialized on _TTS_RUN_LOCK: the model carries the voice
    conditionals as mutable state, and a spoken reply can overlap the settings
    app's preview.
    """
    engine = model if model is not None else get_tts()
    with _TTS_RUN_LOCK:
        raw = engine.generate(text)
    samples = _as_float_samples(raw)
    samples = resample_speed(samples, _clamp_setting("tts_rate", 0.5, 2.0, 1.0))
    volume = _clamp_setting("tts_volume", 0.0, 4.0, 1.0)
    if abs(volume - 1.0) > 1e-6:
        samples = samples * np.float32(volume)
    # Clipped, not wrapped: the GUI allows 2.0 and a clipped sample is audible
    # distortion rather than a silent no-op or a burst of noise.
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)


def _is_cuda_error(exc: BaseException) -> bool:
    return isinstance(exc, (RuntimeError, OSError)) and bool(
        _CUDA_ERR_RE.search(str(exc)))


def transcribe(audio_int16: np.ndarray, model_getter=None) -> str:
    global _whisper_model, _whisper_cpu_fallback
    with _TRANSCRIBE_LOCK:
        getter = model_getter or get_whisper
        model = getter()

        def _read(segments):
            return " ".join(s.text.strip() for s in segments).strip()

        try:
            segments, _info = model.transcribe(
                audio_int16.astype(np.float32) / 32768.0, vad_filter=True,
                language="en", beam_size=1)
            return _read(segments)
        except (RuntimeError, OSError) as exc:
            if _whisper_cpu_fallback or not _is_cuda_error(exc):
                raise
            log.warning("whisper CUDA broken (libcublas…), falling back to CPU "
                        "for this session: %s", exc)
            with _whisper_lock:
                _whisper_model = None
                _whisper_cpu_fallback = True
            segments, _info = getter().transcribe(
                audio_int16.astype(np.float32) / 32768.0, vad_filter=True,
                language="en", beam_size=1)
            return _read(segments)


def tts_to_wav(text: str, wav_path: Path, voice_getter=None) -> None:
    """Synthesize to a temp file, then atomically replace the target.

    Opening the target for writing truncates it immediately, so a synthesis
    failure used to leave a truncated (or empty) wav at the real path, which
    the next caller would happily play as garbage. The caller now never sees a
    partial file: either the old one is still there or the complete new one is.

    The output is 24 kHz 16-bit mono PCM (`TTS_SR`), which is what chatterbox
    produces: `play_wav` reads the rate from this header, so playback and the
    level meter need no changes for the higher rate.
    """
    samples = synthesize(text, model=(voice_getter or get_tts)())
    tmp = wav_path.with_name(f"{wav_path.name}.part{os.getpid()}")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TTS_SR)
            w.writeframes(samples.tobytes())
        os.replace(tmp, wav_path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# PortAudio is process-global, so sd._terminate()/_initialize() (the
# hands-free listener's recovery path for a wedged device) tears down EVERY
# stream at once. Running that while this process is speaking aborts the whole
# interpreter -- the recorded crash is "Fatal Python error: Aborted" inside
# sounddevice's OutputStream.__init__ on the _speak thread, i.e. the reinit
# landing mid-playback. The listener therefore asks before it reinitializes.
_portaudio_users = 0
_portaudio_lock = threading.Lock()


@contextmanager
def portaudio_in_use():
    """Mark a stretch in which a PortAudio stream must not be torn down."""
    global _portaudio_users
    with _portaudio_lock:
        _portaudio_users += 1
    try:
        yield
    finally:
        with _portaudio_lock:
            _portaudio_users -= 1


def portaudio_busy() -> bool:
    """True while a stream this process owns is open (playback or capture)."""
    with _portaudio_lock:
        return _portaudio_users > 0


# Playback level feed. The mic level is deliberately dropped while the bubble
# speaks (its own TTS would re-trigger the VAD), so a voice-reactive visual had
# nothing to follow during speech — that is why the equalizer fell back to a
# time-based pulse. play_wav reports the level of what it is actually playing
# instead. Registered once via set_level_hook(); best-effort, and never allowed
# to break playback.
_level_hook = None
_level_hook_lock = threading.Lock()


# 1024 samples per level update (~43 ms at the speech engine's 24 kHz, ~23/s)
# so the flare moves with the voice instead of stepping once per playback
# block. The stream's own blocksize is 1024, so this adds no buffering latency.
_PLAY_BLOCK = 1024


def set_level_hook(fn) -> None:
    """Register a callable receiving a 0..1 playback level, or None to clear."""
    global _level_hook
    with _level_hook_lock:
        _level_hook = fn


def _emit_level(value: float) -> None:
    with _level_hook_lock:
        hook = _level_hook
    if hook is None:
        return
    try:
        hook(value)
    except Exception:
        pass          # a broken visual must never take the audio path down


def play_wav(path: Path, cancel: threading.Event) -> None:
    """Blocking playback; returns early if cancel is set (barge-in).

    Also feeds the playback level to the level hook so a voice-reactive bubble
    can follow what it is saying, and reports a final 0 when it stops.
    """
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]
    # Normalise against THIS clip's own peak: a quietly-synthesised reply must
    # still move the visual, and a loud one must not simply pin it at 1.0. The
    # floor keeps near-silence from being amplified into full scale.
    try:
        peak = float(np.max(np.abs(data))) if data.size else 0.0
    except Exception:
        peak = 0.0
    scale = max(1200.0, peak * 0.55)
    # The stream is created INSIDE the try and its teardown is in the finally.
    # Both matter: OutputStream()/start() used to sit outside, so a failure
    # skipped the final _emit_level(0.0) (leaving the bubble's designs pinned
    # at the last playback level with no more audio coming to release them) and
    # leaked the stream when start() raised after a successful construction.
    stream = None
    try:
        with portaudio_in_use():
            stream = sd.OutputStream(samplerate=sr, channels=1, dtype="int16",
                                     blocksize=1024)
            stream.start()
            for i in range(0, len(data), _PLAY_BLOCK):
                if cancel.is_set():
                    break
                block = data[i: i + _PLAY_BLOCK]
                stream.write(block.reshape(-1, 1))
                try:
                    rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
                except Exception:
                    rms = 0.0
                _emit_level(min(1.0, rms / scale))
    finally:
        _emit_level(0.0)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


__all__ = [
    "SAMPLE_RATE", "TTS_SR", "TTS_ENGINE", "TTS_REFERENCE", "Recorder",
    "_resample_to_16k", "_open_input",
    "_stop_recorder_bounded", "get_whisper", "get_tts", "transcribe",
    "synthesize", "resample_speed", "tts_device_choice", "TTS_REPO_ID",
    "vram_budget", "yield_to_llm_verdict",
    "tts_weights_cached", "tts_weights_dir", "MIN_REFERENCE_S", "WARM_TEXT",
    "warm_tts",
    "reference_clip_seconds", "reference_problem",
    "tts_to_wav", "play_wav", "configure", "drop_models",
    "gpu_footprint_mb",
    "portaudio_in_use", "portaudio_busy", "set_level_hook",
    "MIC_OPERATION_LOCK",
    # The lock above and the four names below are the seam the HOST shares with
    # this module rather than a second copy of it: the host takes the lock around
    # PortAudio construction and teardown, mirrors the two model caches through
    # it (`_push_model`/`_adopt_model` in handsoff.py), carries the cpu-fallback
    # flag across the call, and reads `_tts_device` for the health snapshot.
    # They are declared here because a private name that crosses a module
    # boundary has to be declared somewhere the reader can see —
    # `tests/test_specs_freshness.py` fails on one that is not — and because the
    # whole interface of this module should be readable in one place.
    "_tts_model", "_whisper_model", "_whisper_cpu_fallback", "_tts_device",
]
