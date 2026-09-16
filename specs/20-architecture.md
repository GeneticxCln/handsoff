# handsoff — architecture

Sources: `handsoff.py` (7057), `core/__init__.py` (306), `core/*.py`,
`settings_schema.py`, `hardware.py`, `handsoff-settings.py` (4812).

## 1. Module map

| Module | Lines | Owns | Must not import |
|---|---|---|---|
| `handsoff.py` | 7057 | bootstrap loader, `Assistant`, `ControlServer`, voice pipeline, memory, `main()` | — (host) |
| `core/__init__.py` | 306 | `APP_MODULE_NAME="handsoff_core"`, `claim_app_instance`, `load_module`, origin rule, stdlib-shadow guard | app globals |
| `core/tools.py` | 3330 | 48 `@tool`s, `ToolBelt`, `DecisionPolicy`, `BoundedJob`, whitelist, secret guard, `ToolResult` | `handsoff` (DI only) |
| `core/bubble.py` | 4069 | `BubbleWidget`, 14 painters, palette, packs, preview TTL 6 s | app globals (injected `SETTINGS`) |
| `core/settings.py` | 874 | loader/writer/migrate/coerce/lock/merge, `Settings` object | app globals (paths as params) |
| `core/audio.py` | 824 | `Recorder`, resample, whisper/TTS getters, `play_wav`, level hook | `handsoff` (`configure()` only) |
| `core/web.py` | 972 | 6 backends, `_route`, TTL cache, `read_page`, SSRF guard | `handsoff` (resolvers via `configure()`) |
| `core/calendar.py` | 548 | ICS fetch/unfold/parse/RRULE/format, scheme + label guards | anything (stdlib only) |
| `core/assistant.py` | 595 | `PomodoroController`, `NotificationReader`, `ReminderStore`, mute/parse helpers | `handsoff` |
| `core/doctor.py` | 515 | `run_doctor`/`doctor_json` via `DoctorDeps` | `handsoff` (deps injected) |
| `core/registry.py` | 435 | `BoundedRegistry` (admission under lock), `Offer` (arm/read/consume) | — |
| `core/brain.py` | 215 | `ollama_chat`/`ollama_chat_stream`, `TurnStream`, markup filter | — |
| `core/theme.py` | 313 | `hex_to_rgb`, luminance, wallpaper match retune (Qt-free) | Qt |
| `core/lifecycle.py` | 62 | `TurnState`, `next_turn` (atomic increment) | Qt/Assistant |
| `settings_schema.py` | 319 | 59 defaults, vocabularies, looks catalogue, `look_matching` | — |
| `hardware.py` | 529 | 13-section `snapshot()`, TTLs, injectable probers, `--preflight` | SETTINGS/Qt/audio |
| `handsoff-settings.py` | 4812 | 6-tab GUI, offscreen-capable, loads schema without the bubble | bubble module |

Dependency direction: `handsoff.py` → `core.*` via `_load_module` handles
(`_core_tools`, `_core_bubble`, `_core_settings`, `_brain`, `_audio`,
`_core_registry`, `_core_assistant`, `_core_lifecycle`, `_core_calendar`,
`_core_doctor`, `_hardware`). Core modules reach back ONLY through injected
deps. `core/calendar.py` + `core/brain.py` + `core/registry.py` +
`core/lifecycle.py` are dependency-free leaves.

## 2. Bootstrap (why it reads paranoid)

1. `_load_core_package()` finds `core/` beside the file → `~/.local/bin` →
   origin-checked import. Never a bare import a foreign `sys.path` entry could
   satisfy (`handsoff.py:234`).
2. `_claim_app_name()` registers under `core.APP_MODULE_NAME` BEFORE any
   shared-state touch (`core.audio.configure`). A second copy (tests alias,
   settings app, offscreen driver, hardening driver) is refused before it can
   repoint `core.audio` at its own mirrors (`handsoff.py:334`,
   `core/__init__.py:101`).
3. `_origin_ok` + `_STDLIB_NAMES`: support modules load only from allowed
   dirs; stdlib names (notably `calendar`) bind as `core.<name>` so a bare
   `calendar` never shadows stdlib for third-party imports
   (`core/__init__.py:205`).
4. Derived globals (`OLLAMA_BASE/MODEL/NUM_CTX`, `WHISPER_*`, `TTS_REFERENCE`)
   refresh via `reload_derived_settings()` after any model/ctx/host change.

Monolith cut (in progress): (a) settings → `core/settings.py`, (4c) tool
runtime → `core/tools.py` + host `ToolBelt` subclass, (4d) doctor →
`core/doctor.py`, (4e) turn primitives → `core/lifecycle.py`. Still in
`handsoff.py`: `Assistant`, `ControlServer`, listeners, memory, wiring.

## 3. Dependency injection seams

- `core/tools.py`: `_DEFAULT_DEPS` + `_CURRENT: ContextVar`, host publishes
  `_ToolDependencies` (`_CORE_HANDLES` tuple) and rebinds
  `_core_tools.ToolBelt = ToolBelt` before `build_tools()`
  (`handsoff.py:3136`). Tests keep the historical `H.*` monkeypatch surface.
- `core/bubble.py`: module-level `SETTINGS`/`APP_NAME`/`SETTINGS_APP`/
  `RESTART_SCRIPT` set by the host post-load; inert defaults keep it
  importable alone.
- `core/web.py`: `configure()` takes RESOLVERS (callables called per use),
  never values — reloads and monkeypatches stay live.
- `core/doctor.py`: `DoctorDeps` struct; partial deps still render (safe
  defaults per field).
- `core/audio.py`: `configure(sample_rate, whisper_*, tts_*, settings, logger)`.

## 4. Concurrency

Qt main thread owns `Assistant`/`BubbleWidget`/`ControlServer`. Everything
slow runs on daemon threads: mic health loop, recorder run, per-turn
`_run_stream`/`_run_call`, speak workers, PTT stop/abort probes, control
accept loop, notification reader, file/process watcher loops, single
diagnostic worker. Shutdown joins with `SHUTDOWN_JOIN_TIMEOUT = 0.25`.

- Admission: every cap is a `core.registry.BoundedRegistry`; slow prepare
  (fork/exec, thread spawn) runs holding a reservation, so "check then insert"
  cannot double-admit (previously 7 jobs vs cap 4).
- Offers: `snooze`/`kill`/`confirm` are `Offer` objects — arm/read/consume
  under one lock with a deadline (`confirm_seconds` 90, `SNOOZE_WINDOW_S` 90,
  `PREVIEW_TTL_S` 6). No `clear()+update()` pairs, no call-site deadline
  checks.
- Generations: `Assistant._gen` under `_gen_lock`; exported counter via
  `core.lifecycle.next_turn` under `_COUNTER_LOCK` — increment+read is one
  critical section (duplicate generations answered one utterance with
  another's text). PTT has its own epoch (`_ptt_epoch`) so background timers
  cannot kill utterances. Staleness checks (`gen != self._gen`) + gen-keyed
  transcript cache trust exactly this.
- Registries live: `job` 4, `watch-file` 4, `watch-process` 4, `diagnostic` 1,
  `control` 1, `notification-reader` 1. Refusals persist to
  `cap-refusals.json` (50) AND are spoken (cooldown 60 s, max 8 labels).

## 5. Assistant state machine

Constants `IDLE/LISTENING/THINKING/SPEAKING = "idle"/...` defined once per
side (`handsoff.py:1511`, `core/bubble.py:65`). `Assistant._state` mutates
only via `_set(gen, state)` (stale generations cannot paint); `sigState`
drives the bubble. Typical turn: `IDLE → LISTENING` (PTT press / wake) →
`THINKING` (submit off-thread on release) → `SPEAKING` (TTS) → `IDLE`;
`followup_until` (monotonic) re-arms one no-wake utterance per reply;
barge-in closes it.

## 6. Voice pipeline

`Recorder` (16 kHz mono int16, `_MIC_OPERATION_LOCK` serializes
construct/teardown, bounded `stop()` never aborts a live native call) →
`_SpeechGate` (adaptive floor) → `ContinuousListener` (hands-free VAD thread)
→ whisper `transcribe` (FFT-brickwall `_resample_to_16k`, native-rate retry
keeping the ORIGINAL error chained) → `ollama_chat_stream` into per-turn
`TurnStream` queue (exactly one terminator) → sentence filter
(`is_leaked_markup`/`strip_thinking`) → chatterbox `synthesize` (24 kHz
float, `resample_speed` for `tts_rate`, `reference_clip_seconds` ≥5 s for
clones) → `play_wav(path, cancel)`. Late captures after a bounded-stop
timeout are reported as WARNING with frame counts, never dropped silently.

## 7. Smells being managed (not new work)

- `handsoff.py` is still the whole: 7057 lines. Cut proceeds one seam at a
  time with the `H.*` monkeypatch contract pinned by tests — no big-bang
  rewrite.
- Dual caches by history: `core.audio` owns model caches, `handsoff.py`
  mirrors `_tts_model`/`_whisper_model`. Push reads the module copy INSIDE
  the lock; reload drops under the same lock; adopt refuses to republish a
  model `core.audio` no longer holds (ordering reviewed, not just tested).
- `_DEPLOY_FILES` (8 entries, `handsoff.py:628`) is a FLOOR for
  manifest-less installs; the manifest glob is the ceiling. Any new module
  MUST reach install.sh `CORE_REQUIRED` (13 names today) — see `90-audit.md`.
