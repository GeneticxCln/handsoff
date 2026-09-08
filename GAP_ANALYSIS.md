# handsoff — gap analysis & improvement roadmap

Date: 2026-09-06 · All items reflect the state after the reliability hardening
passed in this session (self-mute fix, mic watchdog, echo rejection, streaming
TTS, keyboard takeover, settings opener `Mod+Shift+S`, autostart, crash log).

## Addendum — 2026-09-08 external audit closure

Every priority finding and most "remaining gaps" from the external audit are
now closed (232 tests green):

- Command whitelist bypass via absolute `niri` paths — closed (basename-keyed
  spawn checks; `TestSpawnInterpreterBoundary` extended).
- Cross-turn transcript reuse — closed (per-generation transcript cache).
- Keyboard injection on unknown focus — closed (fail-closed refusal).
- Post-fire spoken snooze — closed (offer cleared only after re-arm).
- Reminder read-modify-write race — closed (`REMINDERS_LOCK` transaction).
- Installer: `pacman -Syu`, single autostart owner, checksum-verified existing
  downloads.
- Qt teardown emit — guarded; `PytestUnhandledThreadExceptionWarning` fails CI.
- README.md — written (install, permissions, troubleshooting, recovery).
- CI — `.github/workflows/ci.yml` + `pytest.ini`.
- ICS EXDATE / RECURRENCE-ID — implemented + pinned (`TestICSOverrides`).
- Dependency manifest — `requirements.txt` (installer now consumes it).
- Portable config — `niri-window-rule.kdl` no longer hardcodes a username.
- Historical patcher — moved to `attic/` with an explanatory README.
- Git baseline — repo initialized with `.gitignore` (runtime state excluded),
  CI smoke job for the installer, single-source-of-truth restart script, and a
  versioned pre-commit hook (`githooks/`, enable via
  `git config core.hooksPath githooks`) so broken self-edits can't be
  committed — pinned by `TestPrecommitHook`.

Still open, accepted consciously: real-hardware acceptance session (mic, echo,
suspend/resume), CI rehearse-runs of the installer, ICS MONTHLY/YEARLY RRULE.

## What already works (verified live this session)

- Voice loop: push-to-talk + hands-free VAD, whisper STT, piper TTS, streaming
  sentence-by-sentence speech, barge-in, echo rejection of its own voice
- Brain: local Ollama, tool loop (run_command whitelist, read/edit file with
  self-edit guard), model picker with capability badges, auto memory-clear on
  model switch
- Desktop control: audio/media/brightness/niri commands, virtual keyboard
  (`type_text` / `press_keys` via ydotool) into the focused window
- Ops: niri window rule (round bubble), autostart, keybinds Mod+V / Mod+Shift+V
  / Mod+Shift+H / Mod+Shift+S (settings, works even when the bubble is dead),
  restart script with lock race fixed, faulthandler crash log
- 232 tests, all green

## P0 — reliability gaps (the bubble must never be a zombie again)

1. **No crash recovery.** The bubble starts at login (niri `spawn-at-startup`)
   but a native abort (e.g. CUDA OOM with whisper-large-v3 + a 24B model on
   16 GB VRAM) leaves it dead until the user notices.
   → Run the bubble as a **systemd user service** with `Restart=on-failure`
   (keep `spawn-at-startup` as fallback). This is the single highest-value fix.
2. **VRAM pressure is the likely killer.** whisper `large-v3` (~1.5 GB) + 24B
   Q4 LLM (~14 GB) exceed 16 GB together with desktop usage.
   → Default whisper back to `tiny` (or auto-select by free VRAM at load).
3. **Crash evidence is invisible.** `~/.local/state/handsoff/crash.log` exists
   but nothing tells the user.
   → On startup, check for a recent crash log and speak/notify "I crashed last
   night, here is why (short version)".

## P1 — capability gaps (what users will ask for next)

4. **Window-addressed typing.** `type_text` hits whatever is focused. A
   `focus_window(app_id_substr)` tool built on `niri msg windows` would let the
   AI aim at Firefox/Slack by name ("put this in Firefox") — small step, big win.
5. **Clipboard tools.** `wl-copy` / `wl-paste` as tools: "copy that", "paste it
   into the editor" become trivial and compose with type_text.
6. **Timers & reminders.** "Remind me in 20 minutes" needs a scheduler thread +
   notify/TTS. Frequently requested assistant feature, cheap to add.
7. **Screen awareness.** A `screenshot` tool (grim) + local OCR or a
   multimodal model would answer "what's on my screen". Bigger lift.
8. **"I didn't catch that" loop.** Empty/short transcriptions are silently
   dropped. After 2 failures, the bubble should speak a recovery prompt.
9. **Read-only web lookup.** Everything is offline (by design). A dedicated
   `web_search` tool (fixed endpoint, no arbitrary curl — curl stays blocked)
   would answer weather/news within the existing safety model.

## P2 — hardening & hygiene

10. **niri spawn escape hatch.** `run_command niri msg action spawn -- <app>`
    indirectly launches anything, bypassing the whitelist. Accept consciously
    (single-user box) or constrain spawn args.
11. ~~**CI for the tests.**~~ **Done** — `.github/workflows/ci.yml` runs the
    suite (Python 3.12 + 3.13), `py_compile` on all sources, and `bash -n` on
    both scripts on every push; `pytest.ini` makes background-thread
    exceptions fail the run instead of warning.
12. ~~**Streaming path untested.**~~ **Done** — `TestStreamingChat` drives
    `ollama_chat_stream` against `tests/fake_ollama.py` (sentences, tool calls,
    and the follow-up round).
13. **Settings app additions:** manual "clear memory" button (auto-clear exists
    for model switches), a **History tab** showing what the AI currently
    remembers (demystifies "why did it say that"), and a health panel
    (crash.log tail, model/tool capability, mic device + live level).

## P3 — polish

14. Spoken confirmation before typing into an app the user didn't explicitly
    name (currently prompt-level guidance, not enforced).
15. Say the volume level back when changing it ("volume at 40 percent").
16. Optional wake word ("hey bubble") to complement hands-free VAD.
17. Multi-utterance conversations while speaking (queue follow-up questions
    instead of barge-in-only).
