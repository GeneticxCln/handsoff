# handsoff — data

Sources: `settings_schema.py`, `core/settings.py`,
`handsoff.py` paths/caps, `core/calendar.py`, `core/web.py`, `hardware.py`.

## 1. settings.json (65 keys, `SETTINGS_VERSION = 2`)

Precedence: built-in defaults ← environment ← `settings.json`.
Stamped `version` on every write. v0→v2 migration stepwise
(`piper_voice` → `tts_reference`); future versions load-with-warn, never
drop unknown keys; retired keys (`piper_voice`) dropped on load AND write.

Brain: `ollama_host` (`http://127.0.0.1:11434`), `allow_remote_ollama`
False, `model` (`qwen3:8b`), `num_ctx` 32768, `history_tokens` 0 (= auto).
Voice: `whisper_size` tiny, `whisper_device` auto, `tts_reference` "",
`tts_rate` 1.0, `tts_volume` 1.0, `mic_device` "", `mic_threshold` 600,
`handsfree` False, `assistant_name` assistant, `wake_word_required` False,
`engage_seconds` 45.0, `followup_seconds` 6.0, `dictation` True,
`wake_spotter` False, `spotter_models` [hey_jarvis], `mic_selfheal` True.
Bubble: `bubble_size` 128, `bubble_design` orb, `design_image_path` "",
`design_image_{idle,listening,thinking,speaking}` "", `design_pack` "",
`avatar_ring` ring-light, `avatar_deco_color` state, `avatar_tint` state,
`bubble_accent` 0.5, `animation_energy` 1.0, `colors`
`{idle #4f8cff, listening #ff4d5e, thinking #ff9e2c, speaking #3ecf6e}`.
Web/place: `searxng_url` (`http://127.0.0.1:8888`, "" disables),
`home_place` "", `calendar_ics` []. Policy: `permissions` (20, see
10-requirements §8), `extra_allowed_commands` [], `tool_call_times` None,
`max_tool_calls` 0 (= unlimited), `command_policy` {}, `confirm_seconds`
90.0, `dry_run` False. Behaviour: `streaming_tts` True, `autostart` False,
`workspace_aliases` {}, `briefing` False, `world_warnings` False,
`world_cooldown_min` 60.0, `hardware_watch` False,
`hardware_cooldown_min` 60.0, `hardware_disk_gb` 5.0, `resource_alerts`
False, `ram_alert_percent` 90.0, `vram_alert_percent` 90.0,
`idle_release_seconds` 600 (after this much quiet the speech model is dropped
and the LLM release is decided: kept unless the measured reload is worth the
memory it frees; 0 = never release), `llm_release_wait_s_per_gb` 20.0 (the most
next-turn reload seconds one GB of freed VRAM may cost — `size_vram` from
Ollama's `/api/ps` against the reload measured at the last load; 0 = never
weigh the cost), `vram_pressure_floor_mb` 1024 (while less than this much VRAM
is free the release stops waiting for the full window and uses the one below,
so a card the desktop is struggling on is emptied as soon as nothing is being
said or written; 0 = never rush), `vram_pressure_seconds` 30 (the quiet needed
while below that floor; clamped to `idle_release_seconds`, so it can only ever
make a release EARLIER — unreadable means the normal window, because 0 here
means "the first quiet tick"), `speech_yields_to_llm` True (the mirror of the
idle release: a turn whose LLM does not fit on the card may ask the speech
model for its memory first, and speaks its reply with a freshly loaded voice
rather than letting the model be offloaded to the CPU. Nothing is asked for
when the model is already loaded, when the claim fits, when the release would
not make room, when the size could not be read, while something is speaking, or
when this is off),
`notification_reader` False, `notification_mute_apps` [].

Env overrides (only where settings.json has no value): `HANDSOFF_MODEL`,
`HANDSOFF_NUM_CTX`, `HANDSOFF_WHISPER`, `HANDSOFF_VOICE`, `OLLAMA_HOST`,
`HANDSOFF_KEEP_ALIVE` (default `1h`). Install-time: `HANDSOFF_MODEL`,
`HANDSOFF_WHISPER`, `HANDSOFF_WHISPER_REVISION`, `HANDSOFF_TTS_REPO`
(`ResembleAI/chatterbox-turbo`), `HANDSOFF_PYTHON`,
`HANDSOFF_NO_OLLAMA_SERVICE`, `HANDSOFF_ALLOW_REMOTE_OLLAMA` (the same tokens
the send guard accepts). Each install-time override is the FIRST source, not the
only one: with no override, install.sh reads `model`, `whisper_size` and
`ollama_host` from this file, `TTS_REPO_ID` from `core/audio.py` and `APP_NAME`
from `handsoff.py`, and each named fallback (`DEFAULT_MODEL` `qwen3:8b`,
`DEFAULT_WHISPER_SIZE` `tiny`, `OLLAMA_DEFAULT` `http://127.0.0.1:11434`,
`DEFAULT_TTS_REPO`, `DEFAULT_APP_ID`) is pinned to the app's own default by
`tests/test_ops.py::TestInstallerProvisionsWhatTheAppDecided`.

Coercion (`coerce_settings`): enums clamp to vocabularies, numbers to
ranges, bools strict (`_bool_flag` — `bool("false")` is not True),
junk warns once per key per process. Concurrent writes: file lock +
three-way merge (`expected/current/candidate`); conflict raises
`SettingsConflictError` rather than last-writer-wins. Corrupt JSON →
quarantine `*.bad-<ts>-<pid>` + defaults; non-dict → same. All writes
atomic + 0600 (`atomic_private_write`, mkstemp in-dir, no predictable
`.tmp`).

## 2. On-disk map

| Path | Format | Cap / note |
|---|---|---|
| `CONFIG_DIR/settings.json` | JSON + `version` | the 65 keys above |
| `CONFIG_DIR/deployment.json` | `{files: {rel: {source_sha256, installed_sha256}}, git_commit, git_dirty, …}` (`git_*` judged over the SHIPPED paths only, present only inside a git work tree) | written by install.sh; read by `_deployment_snapshot` |
| `CONFIG_DIR/history.json` | [{role, content, images?}] | token-budget trimmed, images stripped on save |
| `CONFIG_DIR/memory.json` | [{key, fact}] | ≤24, oldest dropped |
| `CONFIG_DIR/design-packs/<slug>/` | `pack.json` + art | installed copies only; `<slug>.previous` one generation |
| `CONFIG_DIR/whisper-model/` | SHA256-verified weights | atomic download |
| `STATE_DIR/handsoff.log` | rotating log | privacy filter redacts secrets |
| `STATE_DIR/crash.log` | faulthandler | truncate-not-unlink, same inode, 0600 |
| `STATE_DIR/handsoff.lock` | flock | one bubble per session |
| `STATE_DIR/control.sock` | unix stream | 0700 dir, peer-uid + token (see 50-ops) |
| `STATE_DIR/control.token` | 64 hex chars | `os.urandom(32)`, rotated per process |
| `STATE_DIR/reminders.json` | [{name, due, repeat}] | ≤64, `REMINDERS_LOCK` + sidecar flock |
| `STATE_DIR/mic-health.json` | {events:[…], briefing} | ≤200 transitions |
| `STATE_DIR/cap-refusals.json` | {totals, last} | ≤50; storm-proof |
| `STATE_DIR/self-watch.jsonl` | one line per sampler tick with findings: `{t, findings:[{prefix, kind, since_s}], state}` | ≤200 lines, 0600; written whether or not the speaking switch is on — the file is the diagnosis, the announcement the convenience |
| `STATE_DIR/decisions.jsonl` | JSON lines | ≤500 (`_DECISIONS_MAX`) |
| `STATE_DIR/scratch-quarantine/<YYYY-MM-DD>/` | reclaimed scratch moved verbatim | `SCRATCH_QUARANTINE_TTL_DAYS` (7) days, then dropped |
| `STATE_DIR/state-hygiene.jsonl` | one line per start: `{at, date, scratch_left, size_bytes, entries, swept}` | ≤`STATE_HYGIENE_MAX` (400) readings, 0600; the doctor's week-over-week trend |
| `STATE_DIR/laya-turns.jsonl` | one line per completed turn: `{ts, text, tools}` | append-only; folded by `ci/laya_corpus.py` on every read, 0600 |
| `STATE_DIR/laya-turns.cursor` | `{lines, hash, at}` | how much of the queue was folded; the hash catches a replaced queue |
| `STATE_DIR/laya-corpus.jsonl` | grown corpus rows `{text, family, source, first_seen, last_seen, count}` | written by `ci/laya_corpus.py`, never into the checkout, 0600 |
| `STATE_DIR/world-events-seen.json` | {norm_key: epoch} | dedupes briefings + proactive only |
| `STATE_DIR/pending-restart.json` | {reason, at} | self-edit restart handshake |
| `STATE_DIR/screen.png` | screenshot | overwritten per capture |
| HF hub cache | chatterbox-turbo ≈3.8 GB | usable-check, not merely present |

`CONFIG_DIR = ~/.config/handsoff`, `STATE_DIR = $XDG_STATE_HOME/handsoff`
(`~/.local/state/handsoff`). `_secure_runtime_files()` enforces 0600 +
uid + non-symlink (lstat first: broken symlinks refused) at every start.
The same start runs `_sweep_stale_scratch()`: scratch a killed run left
behind — `tmp*` DIRECTORIES (`TemporaryDirectory(dir=STATE_DIR)`, e.g.
`_speak`'s) and loose `*.tmp` FILES, the only two shapes this runtime
produces there, both directly in STATE_DIR and both older than a 600 s
grace — is reclaimed; symlinks are never followed out, a dirty state dir
is swept with a warning rather than refusing startup. Reclaim is REVERSIBLE:
nothing is deleted, the entry MOVES into `scratch-quarantine/<YYYY-MM-DD>/`
under STATE_DIR (owner-only; a name collision gets a `~N` suffix so both
copies survive, and an archive that cannot be made private — a planted
`scratch-quarantine` symlink is refused — leaves the scratch in place rather
than destroying it). Folders older than `SCRATCH_QUARANTINE_TTL_DAYS` (7) age
out of the archive on the start that finds them; only names that parse as a
date are ever removed, and only directories. The archive exists because the
two shapes are matched by NAME, so it is what makes a false positive
survivable. One sweep per state
dir per start: the control-socket server calls `_prepare_runtime()` a second
time, and the no-op pass must not overwrite what the start reclaimed. What
was reclaimed is then said once, by `_log_swept_scratch()` after logging
exists, from the same `_LAST_SWEEP` the doctor reads.

One reading with no history cannot say whether the state dir is GROWING, which
is the question an operator actually has, so the same start appends its hygiene
numbers to `state-hygiene.jsonl` (`_record_state_hygiene()`, called right after
the sweep, so a row is the POST-reclaim state and carries that start's `swept`
count). One row per start, not per doctor read: the doctor is asked on demand
and a diagnostic must not write state. The file is capped and rewritten
atomically under the state-file lock (the self-watch log's shape), because an
unbounded diagnostic log would be the very leak the line reports. The doctor
compares the newest reading with the newest one at least
`STATE_HYGIENE_TREND_DAYS` (7) older — never a convenient shorter span — and
reports the delta beside the span it came from; a shorter history reports how
much exists and NO deltas, and a torn or foreign line is dropped rather than
fatal (a power cut leaves exactly that). The structured surface ships the same
number as `state_hygiene.trend`, with `null` (never 0) for a delta the history
cannot support.

## 3. Vocabularies

- Designs (14): orb halo reactor bloom droplet cube equalizer crystal saturn
  void sauron pikachu cat image. States (4): idle listening thinking
  speaking. Looks (8): handsoff midnight daylight ember neon allseeing spark
  curious — `look()`/`look_matching()`/`look_names()` in the schema; GUI and
  doctor resolve through `core/settings.look_matching`.
- Avatar decos (10): off ring-light orbit pulse aurora rainbow sparkle comet
  neon flames; tint: state/natural; deco colour: state/rainbow/`#RRGGBB`.
- Packs: `PACK_MAX_FRAMES` 16, fps 0.5–30 (default 6), max 64 entries /
  64 MB per archive, `..`/absolute refused, content-over-suffix
  (`.zip`↔`.hpack` both accepted). Image work canvas 384 px; caches:
  `_IMAGE_CACHE_MAX` 20, `_PACK_CACHE_MAX` 4, feather masks bounded.
- History budget: `HISTORY_CHARS_PER_TOKEN = 4`, reserve 1024, floor 1024.
- Hardware TTLs: audio 10, gpu 15, systemd/compositor/ydotool 30,
  ollama/fastfetch 60, cheap sections 0; `FAILURE_TTL = 5` (failures expire
  fast, successes per TTL). Sections (13): cpu ram gpu audio display mounts
  ollama models stt_tts systemd compositor ydotool fastfetch.
- Web: `SEARCH_TIMEOUT` 6, `READ_TIMEOUT` 8, `TOOL_MAX_CHARS` 6000,
  `READ_MAX_CHARS` 40000, `CACHE_TTL` 300, searxng probe 0.3 s localhost.
- Control: `_CONTROL_REQUEST_MAX` 65536 B, `_CONTROL_READ_BUDGET` 5.0 s.
- Audio: 16 kHz mono int16, TTS 24 kHz, `MAX_PTT_S` 60, whisper sizes
  tiny…large-v3, TTS ref clip ≥5 s.

## 4. Calendars, weather, world

ICS: `https://` required (loopback `http://` allowed — not on the wire),
UA `handsoff/1.0`, 2 MB cap, unfold → parse (`DTSTART/DTEND`, `TZID`,
floating, all-day) → RRULE expand (`FREQ/INTERVAL/COUNT/UNTIL/BYDAY/…`) →
`_fmt_events`. Errors name the redacted label
(`https://host/… (path redacted)`), never the secret URL.
Weather: open-meteo geocode + forecast; severe = `_SEVERE_WMO {95,96,99,
65,75,82}` or wind ≥75 km/h or urgent keywords/phrases. World: fixed
queries (`_world_news_queries`), norm keys lowercased ≤120 chars, cooldowns
60 min defaults.
