# handsoff — architecture

Sources: `handsoff.py`, `core/__init__.py`, `core/*.py`, `settings_schema.py`,
`hardware.py`, `handsoff-settings.py`. The Line column of §1 is GENERATED from
the tree (`python3 ci/spec_tables.py --write`) — do not hand-edit it, and do not
copy a size out of it into prose.

## 1. Module map

| Module | Lines | Owns | Must not import |
|---|---|---|---|
| `handsoff.py` | 10780 | bootstrap loader, `Assistant`, `ControlServer`, voice pipeline, memory, `main()` | — (host) |
| `core/__init__.py` | 306 | `APP_MODULE_NAME="handsoff_core"`, `claim_app_instance`, `load_module`, origin rule, stdlib-shadow guard | app globals |
| `core/tools.py` | 4780 | 52 `@tool`s, `ToolBelt`, `set_dependencies` (the host installs its runtime with this), `DecisionPolicy`, `BoundedJob`, whitelist, secret guard, `ToolResult` | `handsoff` (DI only) |
| `core/bubble.py` | 4077 | `BubbleWidget`, 14 painters, palette, packs, preview TTL 6 s | app globals (injected `SETTINGS`) |
| `core/settings.py` | 925 | the lifecycle as three calls — `load_settings` (read → migrate → coerce → quarantine), `write_settings` (lock → backup → drop-retired → stamp → atomic replace), `persist_setting` (read-merge-write one key) — plus `Settings`/`settings_object`, `coerce_setting`, and the utilities the runtime shares: `atomic_private_write`, `secure_file`, `quarantine_file`, `backup_runtime_json`, `cross_process_lock` | app globals (paths as params) |
| `core/audio.py` | 1260 | `Recorder`, resample (`_resample_to_16k`), whisper/TTS getters, `play_wav`, level hook, and `drop_models` (the idle release: both locks taken NON-blocking, whisper only when it was on cuda, `_tts_device` cleared so the next load re-decides against that moment's free memory), `gpu_footprint_mb` (what the loaded models are expected to occupy on the card — the estimate doctor's headroom line falls back to when the driver attributes no memory to a pid; a model on cpu counts zero), `vram_budget` (the ONE arithmetic both loaders ask before claiming the card, so speech and whisper cannot each pass on their own: a claim is refused unless it fits after the `_VRAM_RESERVE_MB` reserve and after the speech model's entitlement, whisper being the tenant that yields, and an unreadable card is never room); `_ask_for_the_card` (a refused speech claim asks the host's injected `gpu_reclaim` hook for the card before giving way, and re-plans against a reading taken afterwards — the loader asks, the host weighs what a reclaim costs, and the journal says which tenant moved); `yield_to_llm_verdict` (the MIRROR of that, in the same arithmetic: a turn whose LLM does not fit asks this process's own models for the card, and the second question is the first with their memory put back, so "would releasing help?" is not a second kind of budget; nothing held, a claim that already fits, an unreadable card or claim, and memory that would not change the outcome all answer no); the seam shared with the host: `MIC_OPERATION_LOCK`, `_stop_recorder_bounded`, `_tts_device`, the `gpu_reclaim` hook `configure()` installs, and the mirrored caches it drops through that lock (`_tts_model`, `_whisper_model`, `_whisper_cpu_fallback`) | `handsoff` (`configure()` only) |
| `core/web.py` | 1100 | 6 backends behind `search`, `Result`, `_route`, TTL cache, `read_page`, and the two-stage SSRF guard: `_public_target` validates ONE address, `_read_fetch` walks the redirect chain a hop at a time with every hop rechecked, and the addresses it approved travel with the fetch (`connect_to`) so the address dialled is the address checked; a name is looked up under `_DNS_TIMEOUT_S` | `handsoff` (resolvers via `configure()`) |
| `core/calendar.py` | 624 | `ics_fetch`/`ics_events_from_text`/`fmt_events` (its whole interface, promoted from five private names), ICS unfold/parse/RRULE, `DAY_NAMES`/`MONTH_NAMES`, scheme + label guards | anything (stdlib only) |
| `core/qs_desk.py` | 858 | the QUANTUM SPACE DESK CLIENT (read-only, loopback, another app's control channel): `Desk` resolves the discovery file and calls the desk PER REQUEST (the token is re-read, never cached, and the pid is checked with kill(0) so a stale file is not a connection), `DeskError` keeps the refusals DISTINCT (not-running / untrusted / protocol / unreachable / auth / not-granted / session-gone / no-output), `discovery_paths`, `resolve_session` (id, name, or a half-remembered name, refused when ambiguous), the voice shaping: `describe_sessions`/`describe_status`/`describe_read`/`describe_state`/`session_index`, and `_as_paths` — the guard that `Desk`'s path list is a path list, because measured live on 2026-09-21 the natural mistake (`Desk("handsoff")`, a CLIENT name where a path was wanted) became one relative path and answered "Quantum Space isn't running." about a desk that was running: a wrong state reads exactly like the truth, so a relative path, an empty list or a non-path is now refused as the programming error it is | anything (stdlib only) |
| `core/assistant.py` | 968 | `PomodoroController`, `NotificationReader`, `ReminderStore`, mute/parse helpers | `handsoff` |
| `core/doctor.py` | 824 | `run_doctor`/`doctor_json` via `DoctorDeps` — text lines and one structured dict per host dep (appearance, web, the card's story: `gpu_lines` renders as the section and `gpu_headroom` is the same dict), so both surfaces describe one reading | `handsoff` (deps injected) |
| `core/registry.py` | 446 | `BoundedRegistry` (admission under lock), `Offer` (arm/read/consume) | — |
| `core/brain.py` | 549 | `ollama_chat`/`ollama_chat_stream`, `ollama_unload` (`keep_alive: 0` on the same knob every turn sets the other way), `ollama_resident` (`/api/ps`: what is loaded and how much of it is on the card), `ollama_model_size_mb` (`/api/tags`: what a model that is NOT loaded yet would cost the card — the claim side of the mirror below), `ollama_release_verdict` (keep the LLM when the measured reload costs more than the memory it frees), `TurnStream`, markup filter | — |
| `core/theme.py` | 337 | `hex_to_rgb`, luminance, wallpaper match retune (Qt-free) | Qt |
| `core/lifecycle.py` | 127 | `TurnState`, `GenerationCounter`/`new_counter`, `next_turn` — the program's only turn-generation increment, under its own lock; `Assistant._bump_gen` claims through the counter | Qt/Assistant |
| `core/selfwatch.py` | 297 | the SELF-WATCH sampler (the push half of health): `SelfWatch.tick` inventories the long-lived named threads (`LONG_LIVED_PREFIXES`, one-shots deliberately unmatched) and reports a component *dead* once seen and gone — the first tick learns the baseline instead of alarming on startup order — or *wedged* when its host probe (`sync_fns`, a value that changes while the component works: beat counters) freezes for `WEDGED_AFTER_S`; findings re-arm their spoken announcement per cooldown and `announcement_text` keeps the two sentences distinct (not running / stuck); every entry point swallows to an error field, because a sampler that could kill its host loop would be the failure it exists to catch | anything (stdlib only) |
| `settings_schema.py` | 1049 | 65 defaults, `DEFAULT_SETTINGS`/`SETTINGS_VERSION`, `Field` rows (`SETTINGS_FIELDS`), `POLICY_RULES`, vocabularies, looks catalogue, `look_matching` | — |
| `hardware.py` | 597 | 13-section `snapshot()`, TTLs, injectable probers, `--preflight` | SETTINGS/Qt/audio |
| `handsoff-settings.py` | 5413 | `SettingsWindow` — 6-tab GUI, offscreen-capable, loads schema without the bubble | bubble module |
| `core/voice.py` | 428 | the STATELESS voice primitives the host binds with thin aliases: `SpeechGate` (energy VAD, adaptive floor), the wake vocabulary and matching rules (`norm_words`, `is_wake_utt`, `match_wake`, `skeleton_match`/`wake_skeleton`, `wake_anywhere` + `WAKE_FILLER`/`WAKE_ANYWHERE_WORDS`), `is_echo` + stopwords (the mic-from-speaker filter), `WakeSpotter` (openWakeWord pre-roll detector; model, clock and chunk size arrive as constructor params so the model cache and its test seams stay with the host), and the mic open/device primitives (`available_input_devices`, `device_is_available`, `open_input_unlocked`, `mic_device_to_open`, `stop_stream_owned`, `start_stream_owned` — sd, logger, mic lock and last-open record are parameters). ContinuousListener, Recorder and the host's `_speak` stay in `handsoff.py`: assistant/UI lifecycle state and health hooks | anything (stdlib + numpy locally; sd/logger/lock as parameters) |

Dependency direction: `handsoff.py` → `core.*` via `_load_module` handles
(`_core_tools`, `_core_bubble`, `_core_settings`, `_brain`, `_audio`,
`_core_registry`, `_core_assistant`, `_core_lifecycle`, `_core_calendar`,
`_core_doctor`, `_hardware`). Core modules reach back ONLY through injected
deps. `core/calendar.py` + `core/brain.py` + `core/registry.py` +
`core/lifecycle.py` + `core/qs_desk.py` are dependency-free leaves.

### 1a. Private names reached across a boundary: all paid

A leading underscore is a module saying "not my interface".
`tests/test_specs_freshness.py` fails when another shipped module reaches such a
name anyway. There are three ways to clear it, and each of the six names this
pass crossed was cleared by the one that matched what the name actually was:

1. **Promote it.** Calendar had five private names reached and NO public one, so
the five lost the underscore — the boundary existed only as a naming convention.
2. **Point the caller at the doorway that already exists.** Settings' eight were
steps of ONE lifecycle the module already owned in outline (`Settings.load`
`persist`/`write_all` called them), so the seven genuine entry points became
`load_settings`, `write_settings`, `persist_setting`, `backup_runtime_json`,
`secure_file`, `quarantine_file` and `cross_process_lock`. The eighth,
`_migrate_settings`, stayed private — loading migrates internally — and the
host's wrapper around it turned out to have no callers, so it was deleted.
3. **Give the state a setter.** `core/tools.py`'s `_DEFAULT_DEPS`/`_CURRENT` are
injected STATE, not functions, and the host was rebinding both by name:
`_core_tools.set_dependencies(deps)` now installs them together, which is also
the only way to keep them together — set the ContextVar alone and a tool running
on a worker thread silently falls back to the stdlib-only defaults.

`core/brain.py`'s `_read_http_error` looked like the one remaining crossing and
was in fact never a caller: the only reference to it sat inside the
`except ImportError:` branch in `handsoff.py`, where `_brain` is the name that
branch does not have — so it raised NameError while the class was being BUILT,
and the legacy bundle could not start at all. The host's copy is now a module
level function (`_fallback_read_http_error`) beside the two filters that already
live there for exactly this reason, and `tests/test_hardening.py` boots the app
with `core/brain.py` unloadable to drive it.

The guard still reads a declared-debt table here — module, names still reached,
and why each cannot be renamed yet — and it is empty: a row that is no longer
reached, or a name since promoted or declared, would FAIL it, because a debt row
is a transition, not a parking space.

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
  (`handsoff.py:3289`). Tests keep the historical `H.*` monkeypatch surface.
- `core/bubble.py`: module-level `SETTINGS`/`APP_NAME`/`SETTINGS_APP`/
  `RESTART_SCRIPT` set by the host post-load; inert defaults keep it
  importable alone.
- `core/web.py`: `configure()` takes RESOLVERS (callables called per use),
  never values — reloads and monkeypatches stay live. The reader's hop seam is
  `http_get_hop(url, timeout, connect_to=…)`: `connect_to` is the address list
  the policy checked for THAT url, so the transport dials the checked address
  while the Host header, the SNI and the certificate check stay about the name.
  The seam is DETECTED (`_accepts_connect_to`) rather than required, and a seam
  without it is warned about, never silently unpinned.
- `core/doctor.py`: `DoctorDeps` struct; partial deps still render (safe
  defaults per field). The card is ONE dep pair and deliberately not two: the
  host builds one dict (`_vram_headroom`), `gpu_lines` renders it as the
  section and `gpu_headroom` publishes it as JSON, so a memory story cannot be
  told twice with different numbers.
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
- Generations: the counter IS `core.lifecycle`'s (`GenerationCounter`, claimed
  through `next_turn`), and `Assistant._gen` is a property reading it, so there
  is no second copy of the number for the staleness checks (`gen != self._gen`)
  or the gen-keyed transcript cache to disagree with — the module holds the lock
  across the increment AND the fresh events (duplicate generations answered one
  utterance with another's text), and the host's `_gen_lock` is left guarding
  the one-time creation of the per-instance counter, which must be a single
  shared object before any increment can be atomic. PTT has its own epoch
  (`_ptt_epoch`) so background timers cannot kill utterances.
- Registries live: `job` 4, `watch-file` 4, `watch-process` 4, `diagnostic` 1,
  `control` 1, `notification-reader` 1. Refusals persist to
  `cap-refusals.json` (50) AND are spoken (cooldown 60 s, max 8 labels).

## 5. Assistant state machine

Constants `IDLE/LISTENING/THINKING/SPEAKING = "idle"/...` defined once per
side (`handsoff.py:1927`, `core/bubble.py:65`). `Assistant._state` mutates
only via `_set(gen, state)` (stale generations cannot paint); `sigState`
drives the bubble. Typical turn: `IDLE → LISTENING` (PTT press / wake) →
`THINKING` (submit off-thread on release) → `SPEAKING` (TTS) → `IDLE`;
`followup_until` (monotonic) re-arms one no-wake utterance per reply;
barge-in closes it.

## 6. Voice pipeline

`Recorder` (16 kHz mono int16, `MIC_OPERATION_LOCK` serializes
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

- `handsoff.py` is still the whole (its size is in §1). Cut proceeds one seam
  at a time with the `H.*` monkeypatch contract pinned by tests — no big-bang
  rewrite.
- Dual caches by history: `core.audio` owns model caches, `handsoff.py`
  mirrors `_tts_model`/`_whisper_model`. Push reads the module copy INSIDE
  the lock; reload drops under the same lock; adopt refuses to republish a
  model `core.audio` no longer holds (ordering reviewed, not just tested).
- `_DEPLOY_FILES` (8 entries, `handsoff.py:644`) is a FLOOR for
  manifest-less installs; the manifest glob is the ceiling. Any new module
  MUST reach install.sh `CORE_REQUIRED` (16 names today) — see `90-audit.md`.