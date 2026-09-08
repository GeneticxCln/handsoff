"""Add an openWakeWord audio spotter to handsoff's hands-free listener.

Design: when enabled (settings: wake_spotter + wake_word_required), 80ms audio
chunks feed an openWakeWord Model continuously (~1-2ms CPU per chunk). A hit
captures the pre-roll + trailing speech and emits it as an utterance that
BYPASSES the transcript wake-word gate (the spotter already did the gating).
Spotter load failure falls back to the transcript gate silently.
Exact-string anchors; aborts on any miss; census-validated."""
from pathlib import Path
import re
import py_compile

p = Path("handsoff.py")
src = p.read_text()
if "_get_spotter" in src:
    raise SystemExit("ALREADY_PATCHED")

def sub_once(old: str, new: str, text: str, label: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f"ANCHOR MISS ({text.count(old)}x): {label}")
    return text.replace(old, new, 1)

tool_names_before = {m.group(1) for m in re.finditer(
    r"@tool\(description=[\s\S]*?def (\w+)\(", src)}

# ---- 1. settings: defaults + coercion -------------------------------------------
src = sub_once(
    '    "calendar_ics": [],        # ICS source(s): https URL(s) and/or .ics file paths\n',
    '    "calendar_ics": [],        # ICS source(s): https URL(s) and/or .ics file paths\n'
    '    "wake_spotter": False,     # openWakeWord audio spotter (near-zero CPU wake)\n'
    '    "spotter_models": [],      # e.g. ["hey jarvis"]; empty = all stock models\n',
    src, "DEFAULT_SETTINGS spotter keys")

src = sub_once(
    '    s["calendar_ics"] = _c[:10]\n',
    '    s["calendar_ics"] = _c[:10]\n'
    '    s["wake_spotter"] = bool(s.get("wake_spotter", False))\n'
    '    _sm = s.get("spotter_models", [])\n'
    '    s["spotter_models"] = ([str(x).strip() for x in _sm if str(x).strip()]\n'
    '                           if isinstance(_sm, list) else [])\n',
    src, "spotter settings coercion")

# ---- 2. spotter loader + pre-roll spotter, after _MONTH_NAMES block ---------------
anchor = "_DAY_NAMES = (\"Monday\", \"Tuesday\", \"Wednesday\", \"Thursday\", \"Friday\",\n              \"Saturday\", \"Sunday\")"
assert src.count(anchor) == 1, "day names anchor"
spotter_block = '''

# -- audio-level wake spotter (openWakeWord, optional) -----------------------------

_spotter_model = None
_spotter_failed = False
_SPOTTER_FRAME = 1280                   # 80 ms of 16 kHz int16 audio


def _get_spotter():
    """Lazy-load the openWakeWord Model once; None if unavailable."""
    global _spotter_model, _spotter_failed
    if _spotter_model is not None or _spotter_failed:
        return _spotter_model
    try:
        from openwakeword.model import Model
        models = SETTINGS.get("spotter_models") or []
        _spotter_model = Model(wakeword_models=models) if models else Model()
        log.info("wake spotter loaded: %s",
                 sorted(getattr(_spotter_model, "model_names",
                                _spotter_model.models.keys() if hasattr(
                                    _spotter_model, "models") else [])) or "stock")
    except Exception:
        _spotter_failed = True
        _spotter_model = None
        log.exception("wake spotter unavailable — transcript gate stays active")
    return _spotter_model


class WakeSpotter:
    """Streaming openWakeWord detector with a 2-second pre-roll buffer.

    feed() consumes int16 frames at 16 kHz and returns (True, audio) exactly
    once per detection, where audio = pre-roll + the speech captured since the
    hit. The caller keeps feeding to gather trailing speech after the hit."""

    PREROLL_S = 2.0
    MAXWAIT_S = 8.0                 # give up collecting after this long

    def __init__(self) -> None:
        self._buf: list = []        # pre-roll ring as a list of frames
        self._max_pre = self.PREROLL_S * SAMPLE_RATE // _SPOTTER_FRAME
        self._speech: list = []     # frames collected after a hit
        self._armed = False         # a hit is pending collection
        self._since_hit = 0.0

    def feed(self, frame) -> "tuple[bool, list]":
        """Process one 16 kHz int16 frame (any length). Returns (fired, audio)."""
        model = _get_spotter()
        if model is None:
            return False, []
        n = _SPOTTER_FRAME
        for i in range(0, len(frame) - n + 1, n):
            chunk = frame[i:i + n]
            self._buf.append(chunk)
            del self._buf[:-self._max_pre]
            scores = model.predict(chunk)
            if not self._armed and any(v > 0.5 for v in scores.values()):
                self._armed = True
                self._since_hit = time.monotonic()
                self._speech = list(self._buf)     # pre-roll included
        if not self._armed:
            return False, []
        self._speech.append(frame)
        self._since_hit += len(frame) / SAMPLE_RATE
        gate = self._since_hit >= self.MAXWAIT_S or (
            not any(v > 0.35 for v in model.predict(frame).values())
            and self._since_hit > 1.0)
        if gate:
            self._armed = False
            return True, self._speech
        return False, []

'''
src = sub_once(anchor, anchor + spotter_block, src, "spotter block after day names")

# ---- 3. ContinuousListener: spotter member + reset hook ---------------------------
src = sub_once(
    "        self._running = False\n        self._run_id = 0              # generation token: invalidates stale threads\n",
    "        self._running = False\n        self._run_id = 0              # generation token: invalidates stale threads\n"
    "        self._spotter: \"WakeSpotter | None\" = None\n",
    src, "listener spotter member")

src = sub_once(
    "    def reset(self) -> None:\n        self._discard = True\n",
    "    def reset(self) -> None:\n        self._discard = True\n        if self._spotter is not None:\n            self._spotter = WakeSpotter()   # drop any pending detection\n",
    src, "reset clears spotter state")

# ---- 4. listener _run: create spotter when enabled ---------------------------------
src = sub_once(
    "        frames: list[np.ndarray] = []\n        gate = _SpeechGate(int(SETTINGS[\"mic_threshold\"]))\n",
    "        frames: list[np.ndarray] = []\n        gate = _SpeechGate(int(SETTINGS[\"mic_threshold\"]))\n"
    "        spotter_on = (bool(SETTINGS.get(\"wake_spotter\"))\n"
    "                      and bool(SETTINGS.get(\"wake_word_required\")))\n"
    "        if spotter_on and _get_spotter() is not None:\n"
    "            self._spotter = WakeSpotter()\n"
    "            log.info(\"audio wake spotter active (openWakeWord)\")\n"
    "        else:\n"
    "            self._spotter = None\n",
    src, "spotter creation in _run")

# ---- 5. _process_frame: spotter path (bypasses transcript gate) ---------------------
src = sub_once(
    "        if event == \"end\" or len(frames) >= max_frames:\n            self.gate_open = False\n            self._assistant._vad_speech(False)\n            if len(frames) >= min_frames:\n                audio = np.concatenate(frames).reshape(-1)\n                self._assistant.sigUtterance.emit(audio)\n            frames.clear()\n",
    "        if event == \"end\" or len(frames) >= max_frames:\n            self.gate_open = False\n            self._assistant._vad_speech(False)\n            if len(frames) >= min_frames:\n                audio = np.concatenate(frames).reshape(-1)\n                if self._spotter is not None:\n                    audio = np.concatenate(\n                        [*self._spotter._speech, audio]).reshape(-1) \\\n                        if self._spotter._armed else audio\n                    self._spotter = WakeSpotter()\n                self._assistant.sigUtterance.emit(audio)\n            frames.clear()\n        if self._spotter is not None and not self.gate_open:\n            fired, pre_audio = self._spotter.feed(indata.reshape(-1))\n            if fired and len(pre_audio) >= SAMPLE_RATE // 2:\n                # the spotter IS the wake gate: emit directly, bypass the\n                # transcript wake-word check in _pipeline\n                log.info(\"wake spotter fired (%.1fs of audio)\",\n                         len(pre_audio) / SAMPLE_RATE)\n                self.gate_open = False\n                self._assistant.sigUtterance.emit(\n                    np.concatenate(pre_audio).reshape(-1))\n",
    src, "spotter path in _process_frame")

# ---- 6. _pipeline: bypass transcript gate for spotter utterances --------------------
# Spotter utterances carry a marker in the assistant: track the last emission.
src = sub_once(
    "        self._wake_until = 0.0                   # monotonic: engagement window expiry\n",
    "        self._wake_until = 0.0                   # monotonic: engagement window expiry\n"
    "        self._spotter_wake = False               # last utterance woke via audio spotter\n",
    src, "assistant spotter_wake flag")

src = sub_once(
    "                self._assistant.sigUtterance.emit(\n                    np.concatenate(pre_audio).reshape(-1))\n",
    "                self._assistant._spotter_wake = True\n                self._assistant.sigUtterance.emit(\n                    np.concatenate(pre_audio).reshape(-1))\n",
    src, "mark spotter wake before emit")

src = sub_once(
    "            # -- wake-word gate (hands-free pre-command) -------------------\n            if self._handsfree and SETTINGS.get(\"wake_word_required\"):\n",
    "            # -- wake-word gate (hands-free pre-command) -------------------\n            if self._spotter_wake:\n                self._spotter_wake = False     # audio spotter already gated this\n            elif self._handsfree and SETTINGS.get(\"wake_word_required\"):\n",
    src, "pipeline bypass for spotter utterances")

# ---- 7. system prompt ----------------------------------------------------------------
src = sub_once(
    '- Before cancel_reminder with an uncertain name, use list_reminders. '
    'calendar_month prints a month grid — use it for "what weekday is the 24th" '
    'or "how many days until…".\n',
    '- Before cancel_reminder with an uncertain name, use list_reminders. '
    'calendar_month prints a month grid — use it for "what weekday is the 24th" '
    'or "how many days until…".\n'
    '\n'
    'WAKE BEHAVIOUR\n'
    '- When the wake word is required, the user addresses you by name (or the '
    'audio spotter detects the keyword). After engaging you answer freely for '
    'the engagement window, then go quiet until named again.\n',
    src, "prompt wake section")

# ---- 8. census + write ----------------------------------------------------------------
tool_names_after = {m.group(1) for m in re.finditer(
    r"@tool\(description=[\s\S]*?def (\w+)\(", src)}
assert tool_names_after == tool_names_before, "tool census changed unexpectedly"

p.write_text(src)
py_compile.compile(str(p), doraise=True)
print("WAKEWORD_PATCH_OK")
