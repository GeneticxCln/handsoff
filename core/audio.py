"""Audio capture, speech recognition, synthesis, and playback primitives.

This module deliberately has no dependency on :mod:`handsoff`.  The application
configures it once the settings and application logger exist; the monolith keeps
thin compatibility shims for its historical module-level names.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd


SAMPLE_RATE = 16_000
WHISPER_SIZE = "base"
WHISPER_DEVICE = "auto"
WHISPER_MODEL_DIR = Path.home() / ".config" / "handsoff" / "whisper-model"
PIPER_VOICE_DIR = Path.home() / ".config" / "handsoff" / "piper-voice"
PIPER_VOICE_NAME = ""
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
              piper_voice_dir: Path | None = None,
              piper_voice_name: str = "", settings: dict | None = None,
              logger: logging.Logger | None = None) -> None:
    """Set application-owned paths/settings without importing the application."""
    global SAMPLE_RATE, WHISPER_SIZE, WHISPER_DEVICE
    global WHISPER_MODEL_DIR, PIPER_VOICE_DIR, PIPER_VOICE_NAME, SETTINGS, log
    SAMPLE_RATE = int(sample_rate)
    WHISPER_SIZE = str(whisper_size)
    WHISPER_DEVICE = str(whisper_device)
    if whisper_model_dir is not None:
        WHISPER_MODEL_DIR = Path(whisper_model_dir)
    if piper_voice_dir is not None:
        PIPER_VOICE_DIR = Path(piper_voice_dir)
    PIPER_VOICE_NAME = str(piper_voice_name or "")
    if settings is not None:
        SETTINGS = settings
    if logger is not None:
        log = logger


def _resample_to_16k(data: np.ndarray, rate: int) -> np.ndarray:
    """Resample flat int16 audio to SAMPLE_RATE (linear interp, speech-grade)."""
    if rate == SAMPLE_RATE or data.size == 0:
        return data
    # ponytail: chunked interp bounds peak memory for long captures.
    out: list[np.ndarray] = []
    _CH = 480_000
    ratio = SAMPLE_RATE / float(rate)
    for off in range(0, data.size, _CH):
        seg = data[off: off + _CH]
        duration = seg.size / float(rate)
        target_n = max(1, int(round(seg.size * ratio)))
        x_old = np.linspace(0.0, duration, num=seg.size, endpoint=False)
        x_new = np.linspace(0.0, duration, num=target_n, endpoint=False)
        out.append(np.interp(x_new, x_old, seg.astype(np.float32)).astype(np.int16))
    return np.concatenate(out) if out else data[:0]


def _open_input(device, rate: int, blocksize: int, cb) -> tuple:
    """Open a mono int16 stream, retrying once at the native device rate."""
    try:
        return sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                              blocksize=blocksize, callback=cb, device=device), rate
    except Exception:
        try:
            info = (sd.query_devices(device, kind="input") if device is not None
                    else sd.query_devices(kind="input"))
            native = int(float(info.get("default_samplerate") or rate))
        except Exception:
            native = rate
        if native == rate:
            raise
        stream = sd.InputStream(samplerate=native, channels=1, dtype="int16",
                                blocksize=blocksize, callback=cb, device=device)
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

    def start(self) -> None:
        self._frames = []
        self._samples = 0
        self._stream, self._native_rate = _open_input(
            self._device, SAMPLE_RATE, 1024, self._cb)
        self._stream.start()

    def _cb(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("audio input: %s", status)
        self._frames.append(indata.copy())
        self._samples += int(indata.size)
        cap = int(self.MAX_PTT_S * float(getattr(self, "_native_rate", SAMPLE_RATE)))
        while self._frames and self._samples > cap:
            old = self._frames.pop(0)
            self._samples -= int(old.size)
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        self._level = 0.25 * rms + 0.75 * self._level
        try:
            self._on_level(min(1.0, self._level / 2000.0))
        except Exception:
            pass

    def stop(self) -> np.ndarray | None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.exception("failed to close input stream")
        if not self._frames:
            return None
        audio = np.concatenate(self._frames).reshape(-1)
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
_piper_voice = None
_piper_lock = threading.Lock()
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
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
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
        return _whisper_model


def get_piper():
    global _piper_voice
    with _piper_lock:
        if _piper_voice is None:
            import piper
            onnx = (PIPER_VOICE_DIR / PIPER_VOICE_NAME if PIPER_VOICE_NAME
                    else next(iter(sorted(PIPER_VOICE_DIR.glob("*.onnx"))), None))
            if onnx is None or not onnx.exists():
                raise FileNotFoundError(
                    f"no piper voice (*.onnx) in {PIPER_VOICE_DIR} — run install.sh")
            log.info("loading piper voice %s", onnx.name)
            try:
                _piper_voice = piper.PiperVoice.load(
                    str(onnx), config_path=str(onnx) + ".json")
            except TypeError:
                _piper_voice = piper.PiperVoice.load(str(onnx))
        return _piper_voice


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
    voice = (voice_getter or get_piper)()
    with wave.open(str(wav_path), "wb") as w:
        if hasattr(voice, "synthesize_wav"):
            cfg = None
            try:
                from piper import SynthesisConfig
                cfg = SynthesisConfig(
                    length_scale=1.0 / float(SETTINGS["tts_rate"]),
                    volume=float(SETTINGS["tts_volume"]))
            except Exception:
                cfg = None
            voice.synthesize_wav(text, w, syn_config=cfg)
        else:
            voice.synthesize(text, w)


def play_wav(path: Path, cancel: threading.Event) -> None:
    """Blocking playback; returns early if cancel is set (barge-in)."""
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch)[:, 0]
    stream = sd.OutputStream(samplerate=sr, channels=1, dtype="int16", blocksize=1024)
    stream.start()
    try:
        for i in range(0, len(data), 4096):
            if cancel.is_set():
                break
            stream.write(data[i: i + 4096].reshape(-1, 1))
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


__all__ = [
    "SAMPLE_RATE", "Recorder", "_resample_to_16k", "_open_input",
    "_stop_recorder_bounded", "get_whisper", "get_piper", "transcribe",
    "tts_to_wav", "play_wav", "configure",
]
