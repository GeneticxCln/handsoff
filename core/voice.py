"""Stateless voice primitives: the pieces of the speech pipeline that need no
assistant, no Qt and no host state of their own.

This is the seam the host binds with thin aliases (`handsoff.py` keeps the
historical `_SpeechGate`/`_match_wake`/`_open_input` names, mostly as
one-line delegations), the same contract every other core module uses:
host context arrives as parameters — the settings dict, the logger, the
`sounddevice` module, the mic operation lock — never as an import of the
application module, so core/voice.py is testable alone and the app's
monkeypatch seams stay live on the host side.

Deliberately NOT here: `ContinuousListener`, `Recorder` and the host's
`_speak` — they own assistant/UI lifecycle state and per-process health
hooks, and the host documents why they stay with it.
"""
from __future__ import annotations

import logging
import re

# Same logger name as the other core modules: a mic teardown that gave up on
# the lock has to land in handsoff.log beside everything else.
log = logging.getLogger("handsoff")

#: How long a stream start/stop may wait for the mic-operation lock before it
#: gives up with a named failure instead of blocking forever. The lock's owner
#: thread keeps it for as long as its native PortAudio call takes, and a
#: permanently wedged stream.stop() used to hold it FOREVER — every later PTT
#: press then leaked a thread blocked on acquire, and the hands-free silence
#: watchdog blocked its own capture thread. One bounded wait turns "wedged
#: forever" into one warning and a live caller.
MIC_LOCK_TIMEOUT_S = 3.0


# ---------------------------------------------------------------- speech gate

class SpeechGate:
    """Energy-based speech detector with an adaptive noise floor.

    Speech starts when `start_frames` consecutive frames exceed
    max(noise_floor*2.5, threshold); it ends after `hangover_frames`
    quiet frames. The floor tracks the room while nobody speaks."""

    def __init__(self, threshold: int, start_frames: int = 2,
                 hangover_frames: int = 14) -> None:
        self.threshold = float(threshold)
        self.start_frames = start_frames
        self.hangover_frames = hangover_frames
        self.floor = max(50.0, self.threshold * 0.5)
        self.in_speech = False
        self._loud = 0
        self._quiet = 0

    def feed(self, rms: float) -> str:
        """Feed one frame's RMS; returns '', 'start' or 'end'."""
        if not self.in_speech:
            self.floor = 0.97 * self.floor + 0.03 * rms
            if rms > max(self.floor * 2.5, self.threshold):
                self._loud += 1
                if self._loud >= self.start_frames:
                    self.in_speech = True
                    self._quiet = 0
                    return "start"
            else:
                self._loud = 0
            return ""
        if rms > max(self.floor * 1.6, self.threshold * 0.7):
            self._quiet = 0
        else:
            self._quiet += 1
        if self._quiet >= self.hangover_frames:
            self.in_speech = False
            self._loud = 0
            return "end"
        return ""

    def reset(self) -> None:
        self.in_speech = False
        self._loud = self._quiet = 0


# ------------------------------------------------------------- wake matching

WAKE_FILLER = {"hey", "ok", "okay", "hi", "yo"}


def norm_words(text: str) -> list[str]:
    return [w.strip(".,!?;:") for w in (text or "").split()]


def wake_skeleton(word: str) -> str:
    """Pronunciation-ish skeleton: consonants only, c/k/q/x/z→s, h/w dropped,
    liquids/nasals (r/l/m) unified to n. Whistle-down of whisper mishearings
    like 'cypher'→'Siphon' (both → 'spn'): wake matching must err toward
    LISTENING, not toward ignoring its own user."""
    w = word.lower().translate(str.maketrans("", "", "hw"))
    w = w.translate(str.maketrans("ckqxz", "sssss"))
    w = "".join(ch for ch in w if ch not in "aeiouy")
    return w.replace("r", "n").replace("m", "n").replace("l", "n")


def is_wake_utt(text: str, name_words: list[str]) -> bool:
    """True when the whole utterance is just the wake name ('hey assistant')."""
    words = [w for w in norm_words(text.lower()) if w]
    if words == name_words:
        return True
    return (len(words) == len(name_words) + 1 and words[0] in WAKE_FILLER
            and words[1:] == name_words)


def skeleton_match(seg: list[str], name_words: list[str]) -> bool:
    """Same words, or the same pronunciation skeleton (Siphon ~ cypher)."""
    if seg == name_words:
        return True
    if not all(len(nw) >= 4 for nw in name_words):
        return False                     # never fuzzy on tiny names
    return (len(seg) == len(name_words)
            and [wake_skeleton(w) for w in seg]
            == [wake_skeleton(nw) for nw in name_words])


def match_wake(text: str, name_words: list[str]) -> str | None:
    """If `text` starts with the wake name, return the rest (possibly '').
    Accepts 'name ...', 'hey name ...', 'name, ...'. None if no wake word.
    Word-token based, name tried before the filler skip (so a custom name
    that itself starts with 'hey' still works). Falls back to a fuzzy
    pronunciation-skeleton match for misheard names (Siphon~cypher)."""
    words = norm_words(text)
    if not words:
        return None
    lw = [w.lower() for w in words]
    for skip in (0, 1):
        if skip and (len(lw) <= skip or lw[skip - 1] not in WAKE_FILLER):
            continue
        # one implementation of "same words, or the same pronunciation
        # skeleton" is shared with wake_anywhere (see skeleton_match): two
        # copies is how the fuzzy rule and the strict rule drift apart.
        if skeleton_match(lw[skip:skip + len(name_words)], name_words):
            return " ".join(words[skip + len(name_words):])
    return None


#: How many words a transcript may hold for the name-anywhere rule to apply.
#: A name that is not at the start of a SHORT utterance is a mishearing or a
#: split chunk — the two ways a custom name gets lost, since no openWakeWord
#: model exists for it and the transcript is the only door left. A long
#: sentence that merely mentions the name is somebody talking ABOUT the
#: assistant, and engaging on it would answer a remark addressed to a person.
WAKE_ANYWHERE_WORDS = 8


def wake_anywhere(text: str, name_words: list[str]) -> str | None:
    """Engagement when the wake name is not at the START of a short utterance.

    `match_wake` looks at the first token, and the one after a filler. That is
    right for a name whisper heard where it was said. A custom name also needs
    the other door open: there is no openWakeWord model for it (the spotter
    fires only for its own), so the transcript is the ONLY way to wake it — and
    a split chunk ("… so, Cypher, what's the weather") or a mishearing puts the
    name somewhere else entirely. Same word matching and same fuzzy skeleton as
    `match_wake`, any position, bounded to an utterance short enough to be an
    attempt to wake the bubble. Returns the text with the name removed
    (possibly '') or None when it is not there at all.
    """
    words = norm_words(text)
    if not words or len(words) > WAKE_ANYWHERE_WORDS:
        return None
    lw = [w.lower() for w in words]
    n = len(name_words)
    for i in range(len(lw) - n + 1):
        if not skeleton_match(lw[i:i + n], name_words):
            continue
        head, tail = words[:i], words[i + n:]
        # a filler that was about to introduce the name goes with it, on
        # whichever side of the name it landed
        if head and head[-1].lower() in WAKE_FILLER:
            head = head[:-1]
        elif tail and tail[0].lower() in WAKE_FILLER:
            tail = tail[1:]
        return " ".join(head + tail)
    return None


# --------------------------------------------------------------- echo filter

ECHO_STOPWORDS = set("""
a an the is are am i you me my your it its this that these those of to in on at
for and or but so do does did can could would should will what when where who
how why please just now ok okay hey there then
""".split())


def is_echo(text: str, recent: list[str]) -> bool:
    """True if the freshly transcribed capture substantially repeats lines we
    just spoke aloud. Compares distinctive (non-stopword) words: a capture is
    an echo when its single distinctive word matches, or >= 60% of its
    distinctive words appear in the recent TTS text."""
    if not text or not recent:
        return False

    def _words(s: str) -> list[str]:
        return [w for w in re.findall(r"[a-z']+", s.lower())
                if len(w) > 2 and w not in ECHO_STOPWORDS]

    said = set(_words(" ".join(recent)))
    got = set(_words(text))
    if not got or not said:
        return False
    overlap = sum(1 for w in got if w in said)
    if len(got) == 1:
        return overlap == 1
    return overlap >= max(2, int(0.6 * len(got)))


# -------------------------------------------------------------- wake spotter

class WakeSpotter:
    """Streaming openWakeWord detector with a 2-second pre-roll buffer.

    feed() consumes int16 frames at 16 kHz and returns (True, audio) exactly
    once per detection, where audio = pre-roll + the speech captured since the
    hit. The caller keeps feeding to gather trailing speech after the hit.

    The clock (sample rate) and the model's chunk size are the HOST's audio
    facts, so they arrive as constructor parameters rather than imports: the
    model loader and its cache stay with the host, which owns the
    process-global spotter state and the test seams around it.
    """

    PREROLL_S = 2.0
    MAXWAIT_S = 8.0                 # give up collecting after this long
    SCORE_HIT = 0.5
    SCORE_HOLD = 0.35

    def __init__(self, sample_rate: int, frame: int, model=None, log=None) -> None:
        self._sample_rate = sample_rate
        self._frame = frame
        self._get_model = model      # host's loader (late-read, test-patchable)
        self._log = log
        self._buf: list = []         # pre-roll ring as a list of frames
        # At least one frame: a degenerate window (a frame longer than the
        # whole pre-roll) computes to 0, and `del self._buf[:-0]` deletes
        # NOTHING — the ring would grow one frame per feed for as long as the
        # process listens.
        self._max_pre = max(1, int(self.PREROLL_S * sample_rate // frame))
        self._speech: list = []      # chunks collected after a hit
        self._armed = False          # a hit is pending collection
        self._collected = 0.0        # seconds of audio since the hit
        self._residual = None        # sub-chunk carryover (numpy, host supplies)

    def feed(self, frame) -> "tuple[bool, list]":
        """Process one 16 kHz int16 frame (any length). Returns (fired, audio).

        predict() runs EXACTLY once per chunk — the model is stateful and
        double-feeding corrupts its features. Leftover samples are carried to
        the next call (the mic delivers 1024-sample frames)."""
        import numpy as np
        if self._residual is None:
            self._residual = np.empty(0, dtype=np.int16)
        model = self._get_model() if callable(self._get_model) else self._get_model
        if model is None:
            return False, []
        data = np.concatenate([self._residual, np.asarray(frame).reshape(-1)]) \
            if len(self._residual) else np.asarray(frame).reshape(-1)
        n = self._frame
        nfull = len(data) // n
        for i in range(nfull):
            chunk = data[i * n:(i + 1) * n]
            self._buf.append(chunk)
            del self._buf[:-self._max_pre]
            try:
                scores = model.predict(chunk)
            except Exception:
                # a predict failure corrupts openWakeWord's internal stream:
                # drop the residual too, or the next feed() re-feeds stale
                # samples and every subsequent score is garbage
                (self._log.exception if self._log is not None
                 else lambda m, **k: None)("wake spotter predict failed")
                self._residual = np.empty(0, dtype=np.int16)
                return False, []
            hot = max(scores.values()) if scores else 0.0
            if not self._armed and hot > self.SCORE_HIT:
                self._armed = True
                self._collected = 0.0
                self._speech = list(self._buf)     # pre-roll included
            elif self._armed:
                self._speech.append(chunk)
                self._collected += n / self._sample_rate
                if self._collected >= self.MAXWAIT_S or (
                        hot < self.SCORE_HOLD and self._collected > 1.0):
                    self._armed = False
                    out = self._speech
                    self._speech = []
                    return True, out
        self._residual = data[nfull * n:]
        return False, []


# ------------------------------------------------------- mic open primitives

def available_input_devices(sd, log) -> list[dict]:
    """Return input-capable devices without allowing a failed query to escape."""
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    if isinstance(devices, dict):
        devices = [devices]
    result = []
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        try:
            if int(device.get("max_input_channels", 0) or 0) > 0:
                result.append(device)
        except (TypeError, ValueError):
            continue
    return result


def can_capture_channels(info) -> int:
    """How many input channels this device entry actually offers.

    Zero means the entry exists in PortAudio's list but is OUTPUT-only — an
    HDMI output, a playback-only USB endpoint — so it can never carry a
    microphone. Such an entry is a GHOST of a mic: it answers every
    `query_devices` and opens nothing.

    Returns -1 for "not stated" (the key is absent), which is NOT a ghost.

    Measured 2026-09-29 on this machine, and the reason this is a predicate
    rather than a comment: `query_devices(name, kind="input")` RAISES
    ValueError for those names, so the name-keyed check the self-heal already
    does catches them. The unfiltered `query_devices()` list is where they show
    up with `max_input_channels == 0`, and that list is what the candidate
    sweep below walks — so a sweep over it must skip them or it will try to
    open a speaker as a microphone.
    """
    if not isinstance(info, dict):
        return 0
    # ABSENT is not ZERO. PortAudio reports `max_input_channels` on a real
    # entry, but a stub — and sounddevice's own `default`/`pulse` proxies in
    # some builds — can hand back a dict without the key at all. Reading a
    # missing key as 0 invents a ghost: the two tests that pin the
    # present-but-busy contract failed the moment this did it, because their
    # entries carry no channel count and were reported as output-only. So an
    # absent key means "unspecified" and is NOT evidence of a ghost; only an
    # explicit 0 is.
    if "max_input_channels" not in info:
        return -1
    try:
        return max(0, int(info.get("max_input_channels") or 0))
    except (TypeError, ValueError):
        return -1


def device_is_available(device, devices: list[dict]) -> bool:
    want = str(device).strip().casefold()
    for info in devices:
        name = str(info.get("name", "")).strip().casefold()
        if want == name or want in name:
            return True
    return False


def open_input_unlocked(device, rate: int, blocksize: int, cb, *,
                        sd, log, mic_lock, last_open) -> tuple:
    """Open input, handling stale configured names without querying them.

    `mic_lock` is the host's microphone operation lock — PortAudio is
    process-global, so stream construction/teardown serialize through one
    owner — and `last_open` is the threading.local where the actually-opened
    device is recorded for health reporting."""
    configured = device is not None and str(device).strip()
    # `device is None` means "the system default", NOT "nothing to choose" —
    # and those differ exactly when the default is broken. Measured 2026-09-29
    # on the deployed copy: a stale pin self-healed to `None`, the recorder
    # opened the bare default, and a push-to-talk turn ended `wedged=1
    # frames=0` with the utterance discarded while two working microphones sat
    # in the device list — because with nothing configured there was no
    # `candidates` sweep to reach them. So the list is always gathered, and
    # `None` becomes the LAST candidate rather than the only one.
    devices = available_input_devices(sd, log)
    candidates = [device]
    if not configured and devices:
        # no pin: still prefer a real input over an unverified default, and
        # keep the default as the final fallback
        names = [str(info.get("name", "")) for info in devices
                 if str(info.get("name", ""))]
        if device is None:
            candidates = names + [None]
        else:
            candidates = [device] + names
    if configured and not device_is_available(device, devices):
        log.warning("configured microphone %r is unavailable; falling back to "
                    "system default", device)
        # Named input-capable devices first, the bare default (`None`) LAST.
        # Measured 2026-09-29: a stale PulseAudio default can name a device
        # that is present but dead, and opening it first is how a turn ends
        # with `wedged=1 frames=0` — the utterance discarded — while two
        # working microphones sat in the list behind it. No ghost filter is
        # repeated here on purpose: `available_input_devices` above already
        # drops the 0-channel entries, and a second, untested copy of that
        # rule is the kind of thing that silently rots.
        candidates = [str(info.get("name", "")) for info in devices
                      if str(info.get("name", ""))]
        candidates.append(None)

    last_error = None
    for candidate in candidates:
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=blocksize, callback=cb,
                                    device=candidate)
            last_open.value = candidate
            return stream, rate
        except Exception as exc:
            last_error = exc
            try:
                info = (sd.query_devices(candidate, kind="input")
                        if candidate is not None else
                        sd.query_devices(kind="input"))
                native = int(float(info.get("default_samplerate") or rate))
            except Exception:
                native = rate
            if native != rate:
                try:
                    stream = sd.InputStream(samplerate=native, channels=1,
                                            dtype="int16", blocksize=blocksize,
                                            callback=cb, device=candidate)
                    last_open.value = candidate
                    return stream, native
                except Exception as exc2:
                    last_error = exc2
    if last_error is not None:
        raise last_error
    raise ValueError("no input device available")


def mic_device_to_open(configured, audio, log) -> tuple:
    """(device, fell_back) — the system default when the pinned device is gone.

    A pinned device carries its ALSA card index inside its name
    ('Blue Microphones: USB Audio (hw:4,0)'), and that index moves when the
    hardware does. Measured on this machine: the Yeti pinned at hw:4,0 while it
    was card 3 after a replug, so every open failed with 'Cannot get card index
    for 4' and push-to-talk died outright — instead of recording from the
    microphone the machine actually has. The bubble was unusable over a stale
    index, which is a settings value, not a hardware fact.

    `query_devices` is the check, and deliberately the only one: a device that
    EXISTS but is busy still queries fine and still fails at open, which is a
    different problem with a different answer (retry), and quietly opening the
    default there would record from the wrong microphone without saying so.
    `None` is how this app says "the system default" everywhere else.
    """
    # The loader coerces this key to a str (measured: null, 5, "  ", ["a"] and
    # {"x": 1} all become ""), so `configured` is a string in every path the
    # file writes -- but SETTINGS is a plain dict that embedders and tests
    # assign into directly, and `str(None)` is the TRUTHY string "None": a
    # device name no machine has, which would have warned and notified on every
    # open. Anything that is not a string means "nothing pinned".
    if isinstance(configured, str):
        device = configured.strip() or None
    else:
        device = None
    if device is None or audio is None:
        return device, False
    try:
        info = audio.sd.query_devices(device, kind="input")
        # A device can be PRESENT and still be a ghost of a microphone: it
        # enumerates, answers every query, and offers no input channels to
        # capture with. The name-keyed ValueError below cannot see that shape,
        # because the query SUCCEEDS — so it is checked here, on the entry the
        # query handed back, and a zero-channel entry is treated as the
        # absence it is. Measured 2026-09-29: pinning a mic to a playback-only
        # endpoint passed the old check and then failed at every open.
        if (isinstance(info, dict)
                and can_capture_channels(info) == 0):
            log.warning("configured microphone %r enumerates but offers no "
                        "input channels (output-only device) — using the "
                        "system default instead", device)
            return None, True
    except ValueError as e:
        # ValueError is sounddevice's own "No input device matching '<name>'" —
        # a CONFIRMED absence, and the only answer that justifies standing in
        # for the user's choice with the default (measured: the same type for a
        # stale ALSA pin and for a name that never existed).
        log.warning("configured microphone %r is not on this machine (%s) — "
                    "using the system default instead", device, e)
        return None, True
    except Exception as e:                  # noqa: BLE001 -- unknown is not absent
        # A broken audio backend, or a wiring mistake in this function itself,
        # must NOT read as "the device is gone": keeping the pin makes the open
        # fail loudly, which is the old and honest shape. Only a known absence
        # substitutes anything.
        log.warning("could not check whether microphone %r is present (%s) — "
                    "keeping it", device, e)
        return device, False
    return device, False


def stop_stream_owned(stream, mic_lock, timeout_s: float = MIC_LOCK_TIMEOUT_S) -> None:
    """Run stream teardown under the same owner as InputStream construction.

    Bounded: the lock's owner may be wedged INSIDE a native stream.stop() (the
    exact case the bounded PTT stop exists for), and waiting forever for it
    leaks a thread per press and stalls the hands-free watchdog. On timeout
    the stream is left untouched — PortAudio is process-global and this thread
    is not the owner — the caller drops its reference, and the wedge is named
    in the journal rather than hung on."""
    if stream is None:
        return
    if not mic_lock.acquire(timeout=timeout_s):
        log.warning(
            "mic operation lock still held after %.1fs — a stream stop is "
            "wedged; abandoning this teardown without touching the stream "
            "(the owner thread keeps the lock until its native call returns)",
            timeout_s)
        return
    try:
        # `close` runs whether or not `stop` did: a stream the device already
        # dropped raises from stop(), and skipping the close() behind it left
        # the PortAudio stream open for the life of the process (sounddevice has
        # no finalizer). The stop's own error still propagates for the caller
        # to log.
        try:
            stream.stop()
        finally:
            stream.close()
    finally:
        mic_lock.release()


def start_stream_owned(stream, mic_lock,
                       timeout_s: float = MIC_LOCK_TIMEOUT_S) -> None:
    """Start a stream under the same owner, bounded like `stop_stream_owned`.

    A wedged teardown holding the lock used to block the hands-free reopen
    forever — its own capture thread hung on this acquire. On timeout the
    stream is left NOT started: the listener's stall watchdog reopens it on
    its own schedule, and the wedge is named in the journal."""
    if not mic_lock.acquire(timeout=timeout_s):
        log.warning(
            "mic operation lock still held after %.1fs — a stream stop is "
            "wedged; refusing to start another stream on top of it",
            timeout_s)
        return
    try:
        stream.start()
    finally:
        mic_lock.release()
