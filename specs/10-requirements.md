# handsoff — requirements

Sources: `settings_schema.py` (65 keys, `DEFAULT_SETTINGS`, `BUBBLE_DESIGNS`,
`APPEARANCE_LOOKS`), `core/tools.py` (`@tool`, `DecisionPolicy`, `ALLOWED`,
`BLOCKED`), `handsoff.py` (`PTT_ACTIONS`, `PTT_READ_ONLY`, assistant states).

## 1. Voice loop

- R1.1 Push-to-talk: hold bubble left button → record → release → transcribe →
  answer → speak. 60 s cap per press (`core/audio.py:484` `MAX_PTT_S`).
- R1.2 Hands-free: wake-name utterance or openWakeWord spotter opens an
  engagement window (`engage_seconds` default 45.0); `followup_seconds`
  default 6.0 re-listens after each reply without the wake word.
- R1.3 Barge-in: click bubble while speaking stops playback instantly.
- R1.4 Dictation mode types transcripts into the focused window, no LLM turn.
- R1.5 Echo rejection: own TTS lines never re-enter as user turns
  (`_is_echo`, `_recently_spoken`).
- R1.6 Mic self-heal (`mic_selfheal` default True): wedged mic restarts with a
  spoken explanation; unplug hands-free degrades loudly, recovers on replug.

## 2. Brain

- R2.1 Ollama chat + streaming (`core/brain.py`: `ollama_chat`,
  `ollama_chat_stream`), guard-injected, never raising past the caller as data
  loss — failures are `ToolResult` errors, never fabricated success.
- R2.2 Tool calling: schemas generated from `@tool` signature + docstring;
  strict arg coercion (`coerce_bool_arg`, `coerce_number_arg`) — malformed args
  refuse as "bad arguments", never guess 0/True.
- R2.3 History budget: estimated tokens chars/4, auto = `num_ctx` − fixed
  prompt − 1024 reply reserve, floor 1024 (`handsoff.py:1421`).
- R2.4 Durable memory: ≤24 facts (`MAX_MEMORY_FACTS`), extracted per
  utterance, merged, surviving history trim and restarts (`memory.json`).
- R2.5 Control-token hygiene: `<think>`/`tool_calls`/`im_start` leakage
  stripped before TTS (`core/brain.py:20`), sentences starting `<3`/`<5 min`
  MUST survive.

## 3. Tools (52, full census in `30-tools-api.md`)

Desktop (gated `run_command` unless noted): `run_command`, `workspace`,
`close_window`, `kill_process`+`confirm_kill`, `start_command`+`job_status`,
`open_app`, `press_hotkey` (gate `press_keys`), typing `type_text`,
`press_keys`, `focus_window`, `wait_for_window` (gate `focus_window`),
`wait`+`niri_capabilities` (ungated), clipboard `copy_text`/`paste_text`,
screen `see_screen`/`read_screen_text`/`screen_elements` (gate
`screen_access`), pointer `click_element`/`click_at`/`scroll` (gate `operator`,
default OFF), files `read_file`/`edit_file`, diagnostics `handsoff_doctor`
(ungated), meta `confirm_action` (ungated), `get_datetime`.
Knowledge (gate `web_access`): `get_weather`, `web_search`, `read_page`,
`world_events`, `lookup_fact`. Music (gate `media`): `media_play`,
`media_control`, `media_volume`, `now_playing`, `search_library`. Time
(gate `calendar`): `calendar_month`, `read_calendar`. Personal (gate
`reminders`): `set_reminder`, `list_reminders`, `cancel_reminder`,
`snooze_reminder`. Ambient (own gates): `notification_reader`,
`pomodoro`, `watch_file`, `watch_process`. Another app (gate
`quant_space`): `quant_space_status`, `quant_space_sessions`,
`quant_space_read`, `quant_space_check` — read-only, and the desk on the
other side keeps its own Control switch and allow-list, so these relay what
it says rather than deciding for it (`core/qs_desk.py`).

## 4. Bubble + settings GUI

- R4.1 Four states `idle/listening/thinking/speaking`, one shared level
  signal; 14 designs (`settings_schema.py:47`); every design neutral at
  silence; window mask = inscribed ellipse + per-design `design_region`.
- R4.2 Eight one-click looks (`handsoff` = shipped defaults … `curious`);
  current look DERIVED via `look_matching()`, never stored beside the values.
- R4.3 `image` design: user art fitted by circumscribed circle, washed in
  state colour (floor 0.45), own voice reaction, dashed placeholder when
  empty — never falls back to orb. Packs: folder or `.hpack` zip, caps
  16 frames / 0.5–30 fps / 64 entries / 64 MB.
- R4.4 Settings GUI tabs: Brain, Voice, Permissions, Appearance, Startup,
  History (Conversation / Durable facts / Decision log sub-tabs). Appearance
  applies live, no Save.

## 5. Reminders, calendar, ambient

- R5.1 Reminders persist across restarts (`reminders.json`, cap 64, horizon
  365 d), one serialized read-modify-write (`REMINDERS_LOCK` + sidecar flock),
  snooze re-arms ≤90 s after firing (`SNOOZE_WINDOW_S`).
- R5.2 ICS sources: https only (loopback http allowed), 2 MB fetch cap,
  secret URL never echoed (host-only label), RRULE expansion, month grids.
- R5.3 Notifications reader OFF by default, per-app mute + cooldown.
- R5.4 Pomodoro work/break with spoken transitions, bounded single worker.
- R5.5 File watchers ≤4 + process watchers ≤4, 24 h TTL, regex danger guard
  (length 300, exponential-shape refuse, 4000-char line cap).
- R5.6 Opt-in hardware watch + world-event warnings, both cooldown-gated
  (defaults 60 min), deterministic severity (keywords/WMO/wind), never model
  judgment.

## 6. Self-modification

- R6.1 `edit_file` may replace own source + sibling split modules + files
  under `CONFIG_DIR/` only; writes `.bak`, must `py_compile`, self-edits keep
  `SELF_MARKER`; preview compiles before write.
- R6.2 Restart via `handsoff-restart` (systemd-first); pending-restart record;
  crash spoken + logged on next boot.

## 7. Non-functional

- N1 Local-first: STT/LLM/TTS run on this machine; network only for
  weather/search/reader/ICS/calendar-fetch and explicit `run_command`.
- N2 Privacy: state/config dirs + files owner-only 0600, symlinks refused
  (`core/settings.py:_secure_file`); clipboard/notifications/screen/mic are
  permission-gated; secret paths (credential stores, keys, shell history,
  browser profiles) refused at `read_file`/`watch_file`/`run_command(cat)`;
  ICS token never logged or spoken; remote Ollama needs explicit opt-in.
- N3 Loud degradation: every fault-injection seam (Ollama down, mic empty,
  dbus dead, ENOSPC, missing models, wedged daemon, backward clock) produces
  journal WARNING/ERROR + spoken cause + doctor line. No swallowed errors,
  no success reported when the write did not happen.
- N4 Bounded everything: jobs 4, watchers 4+4, diagnostics 1, control runs 1,
  PTT 60 s, read 160 KB / write 2 MB / self-edit 500 KB, control request
  64 KB + 5 s budget, history/lookups TTLs, refusal stores capped (see
  `40-data.md`). A refusal names the cap, the occupants, and the remedy.
- N5 Deterministic suite: coverage TOTAL ≥70, any-order green
  (shuffled twice in CI), thread-crash fails the build, every shipped file
  byte-compiles, every shell script `bash -n`, installer `--help` smokes.
- N6 Platform: Arch/CachyOS + niri, NVIDIA strongly recommended (26B ≈
  14 GB VRAM), mic + speakers, Ollama, MPD for music, ydotoold for typing.

## 8. Permission model (20 keys, `settings_schema.py:296`)

Defaults ON: run/read/edit/self_restart/type/press_keys/web/media/screen/
paste/copy/reminders/calendar/focus/get_datetime/pomodoro/watchers/
quant_space. Defaults OFF: `operator` (real pointer), `notifications`
(private).
`dry_run` False, `confirm_seconds` 90.0, `command_policy` per-tool
ALLOW/DENY/CONFIRM, `extra_allowed_commands` extends the shell whitelist.
`press_hotkey` rides the `press_keys` gate; `workspace`, `close_window`,
`kill_process`, `start_command`, `job_status`, `open_app` ride `run_command`;
`wait_for_window` rides `focus_window`; the four `quant_space_*` tools ride
the ONE gate `quant_space`. `wait`, `niri_capabilities`, `confirm_action`,
`handsoff_doctor`, `get_datetime` are ungated by design (read-only or
second-step).
