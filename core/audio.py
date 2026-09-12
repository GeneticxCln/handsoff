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
# serialize on _MIC_OPERATION_LOCK. A bounded stop runs rec.stop() on an owner
# thread holding the lock; a timeout returns control WITHOUT touching the
# stream — the owner finishes the state transition and fires the recorder's
# _handsoff_stop_done finalizer (never abort a live native call from a second
# thread).
_MIC_OPERATION_LOCK = threading.RLock()
_MIC_OPERATION_STATE_LOCK = threading.Lock()
_MIC_OPERATION_OWNER = None


def configure(*, sample_rate: int = 16_000, whisper_size: str = "base",
              whisper_device: str = "auto", whisper_model_dir: Path | None = None,
              tts_reference: str = "", tts_device: str = "auto",
              settings: dict | None = None,
              logger: logging.Logger | None = None) -> None:
    """Set application-owned paths/settings without importing the application."""
    global SAMPLE_RATE, WHISPER_SIZE, WHISPER_DEVICE
    global WHISPER_MODEL_DIR, TTS_REFERENCE, TTS_DEVICE, SETTINGS, log
    SAMPLE_RATE = int(sample_rate)
    WHISPER_SIZE = str(whisper_size)
    WHISPER_DEVICE = str(whisper_device)
    if whisper_model_dir is not None:
        WHISPER_MODEL_DIR = Path(whisper_model_dir)
    TTS_REFERENCE = str(tts_reference or "")
    TTS_DEVICE = str(tts_device or "auto")
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
    global _MIC_OPERATION_OWNER
    box: dict = {}

    def _call() -> None:
        global _MIC_OPERATION_OWNER
        _MIC_OPERATION_LOCK.acquire()
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
            _MIC_OPERATION_LOCK.release()

    th = threading.Thread(target=_call, name="ptt-stop-native", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, True
    return box.get("audio"), False


_whisper_model = None
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
_TTS_VRAM_MB = 3_000            # measured ~2.7 GB for turbo on a 16 GB card
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


def _whisper_device_choice(size: str, free_mb: "int | None") -> tuple[str, str]:
    if free_mb is None:
        return ("cpu", "int8")
    need = _WHISPER_VRAM_MB.get(size, 3600)
    return (("cuda", "float16") if free_mb >= need + 1024 else ("cpu", "int8"))


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
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                device, compute = "cpu", "int8"
                if not _whisper_cpu_fallback:
                    if WHISPER_DEVICE == "cuda":
                        device, compute = "cuda", "float16"
                    elif WHISPER_DEVICE == "auto":
                        device, compute = _whisper_device_choice(
                            WHISPER_SIZE, _nvidia_free_vram_mb())
                log.info("loading whisper '%s' on %s (%s) from %s",
                         WHISPER_SIZE, device, compute, WHISPER_MODEL_DIR)
                try:
                    _whisper_model = WhisperModel(
                        WHISPER_SIZE, device=device, compute_type=compute,
                        download_root=str(WHISPER_MODEL_DIR), local_files_only=True)
                except Exception:
                    if device == "cpu":
                        raise
                    log.exception("whisper %s load failed — falling back to cpu", device)
                    _whisper_model = WhisperModel(
                        WHISPER_SIZE, device="cpu", compute_type="int8",
                        download_root=str(WHISPER_MODEL_DIR), local_files_only=True)
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


def tts_device_choice(free_mb: "int | None", device: str = "auto") -> str:
    """Which device to load the speech model on.

    `auto` prefers CUDA but only when the card can actually hold the model
    (~2.7 GB measured) alongside whisper and the LLM. An unreadable nvidia-smi
    is not evidence of a GPU, so it means cpu — and the caller says so LOUDLY,
    because CPU synthesis is not real time and a spoken turn may not keep up.
    """
    if device in ("cuda", "cpu"):
        return device
    if not _torch_cuda_available():
        return "cpu"
    if free_mb is not None and free_mb < _TTS_VRAM_MB:
        return "cpu"
    return "cuda"


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
            device = tts_device_choice(_nvidia_free_vram_mb(), TTS_DEVICE)
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
    "tts_weights_cached", "tts_weights_dir", "MIN_REFERENCE_S", "WARM_TEXT",
    "warm_tts",
    "reference_clip_seconds", "reference_problem",
    "tts_to_wav", "play_wav", "configure",
    "portaudio_in_use", "portaudio_busy", "set_level_hook",
]
