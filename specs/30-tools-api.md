# handsoff — tools API

Source: `core/tools.py` — **48 `@tool` methods**, counted from the decorators that
define them. The census below is GENERATED (`python3 ci/spec_tables.py --write`);
do not hand-edit it, and do not copy a number out of it into prose.
`gates=None` (decorator omits `gates=`) means "own name"; `gates=''` means
ungated. Descriptions are the pinned model-facing strings in the decorator.

## Census (name | effective gate | line)

| Tool | Gate | L |
|---|---|---|
| `run_command` | `run_command` | 1132 |
| `type_text` | `type_text` | 1351 |
| `press_keys` | `press_keys` | 1414 |
| `press_hotkey` | `press_keys` | 1517 |
| `notification_reader` | `notifications` | 1576 |
| `pomodoro` | `pomodoro` | 1614 |
| `watch_file` | `watchers` | 1690 |
| `watch_process` | `watchers` | 1739 |
| `workspace` | `run_command` | 1778 |
| `focus_window` | `focus_window` | 1896 |
| `wait_for_window` | `focus_window` | 1942 |
| `wait` | — | 1964 |
| `niri_capabilities` | — | 2065 |
| `close_window` | `run_command` | 2095 |
| `copy_text` | `copy_text` | 2155 |
| `paste_text` | `paste_text` | 2167 |
| `set_reminder` | `reminders` | 2182 |
| `list_reminders` | `reminders` | 2235 |
| `cancel_reminder` | `reminders` | 2251 |
| `snooze_reminder` | `reminders` | 2270 |
| `media_play` | `media` | 2308 |
| `media_control` | `media` | 2344 |
| `media_volume` | `media` | 2363 |
| `now_playing` | `media` | 2375 |
| `search_library` | `media` | 2392 |
| `calendar_month` | `calendar` | 2411 |
| `read_calendar` | `calendar` | 2441 |
| `get_weather` | `web_access` | 2485 |
| `web_search` | `web_access` | 2514 |
| `read_page` | `web_access` | 2540 |
| `world_events` | `web_access` | 2553 |
| `lookup_fact` | `web_access` | 2577 |
| `get_datetime` | `get_datetime` | 2594 |
| `see_screen` | `screen_access` | 2637 |
| `read_screen_text` | `screen_access` | 2655 |
| `kill_process` | `run_command` | 2742 |
| `confirm_kill` | `run_command` | 2770 |
| `confirm_action` | — | 2800 |
| `start_command` | `run_command` | 2889 |
| `job_status` | `run_command` | 2933 |
| `handsoff_doctor` | — | 2981 |
| `screen_elements` | `screen_access` | 3077 |
| `click_element` | `operator` | 3099 |
| `click_at` | `operator` | 3116 |
| `scroll` | `operator` | 3121 |
| `open_app` | `run_command` | 3154 |
| `read_file` | `read_file` | 3244 |
| `edit_file` | `edit_file` | 3274 |

## Result contract

`ToolResult(text, kind)` — the failure flag is CARRIED by the producer, not
re-derived by sniffing text at seven call sites. Text convention (single
definition, `tool_kind`): starts with `ERROR:`/`REFUSED:` (+ word boundary)
= failure; anything else = ok. `execute()` enforces policy → confirm-offer →
rate limit → `_execute` → decision log (`decisions.jsonl`, 500-line cap).

## Policy

`DecisionPolicy.classify`: ALLOW / DENY / CONFIRM per `command_policy`;
`confirm_action('yes'/'no')` settles the pending `Offer`
(`confirm_seconds` 90). `kill_process(target)` is itself two-step:
shows the exact-name/port match, then `confirm_kill('yes')` SIGTERMs only a
same-user process. `is_desktop_action` marks what `dry_run` turns into
report-instead-of-act.

## run_command whitelist (`ToolBelt.ALLOWED`, L723)

Allowed base: `pactl playerctl brightnessctl niri spawn echo cat ls pwd
notify-send ps free uptime df ss nvidia-smi` + `extra_allowed_commands` +
restart script + `git {status,diff,log,show,branch,remote}` (no `-d/-D`) +
`cargo {build,check,test,clippy}`. Blocked (substring, word-boundaried):
`sudo rm pacman yay paru shutdown poweroff reboot halt mkfs dd kill chmod
chown mount umount curl wget bash sh zsh fish python python3 pip mv cp tar
zip 7z make gcc systemctl journalctl tee xargs env eval exec` (+ interpreter
detection `_is_interpreter`). Refused outright: empty, shell operators
`;|&`$\n\r<>`, unparseable `shlex`, secret-path args, `niri spawn` outside
the app allowlist. `TIMEOUT = 15` s. Every invocation logged with redacted
target (`_log_target`, secret values redacted).

## Files

- `read_file(path)`: UTF-8, `MAX_READ` 160 KB; refuses secret paths
  (`denied_secret_path`: credential/key/history/browser-profile dirs, secret
  globs). "Never ask the user to paste those."
- `edit_file(path, content)`: allowed = own source + sibling split modules
  (`_SPLIT_EDIT_FILES`, `_SPLIT_EDIT_CORE`) + files under `CONFIG_DIR/`;
  `.bak` backup; `MAX_WRITE` 2 MB, `MAX_SELF_EDIT` 500 KB; Python must
  `compile()` + `py_compile`; self-edits must preserve `SELF_MARKER`.
  Preview path compiles before write (`compile(content,…, 'exec')`).

## Jobs, watchers, screen, operator

- Jobs: `BoundedJob.MAX_JOBS = 4` via `BoundedRegistry("job")`; slow
  fork/exec holds a reservation; `job_status` reports/tails; limit refusal
  names the occupants. Output bounded; drain threads never block shutdown.
- Watchers: 4 file + 4 process (`watch_file`/`watch_process`/`stop_watchers`
  — note: `stop_watchers` is a plain method, not a tool). File loop polls 1 s,
  500 lines/poll, 4000-char line cap, stops on deletion or 24 h. Pattern
  guard: length ≤300 + quantified-group-with-quantifier refuse
  (`(a+)+`, `(\d+)*`); residual `(a|aa)+` documented, blast radius one daemon
  thread. Process loop matches one exact same-user name, announces exit.
- Screen: `see_screen(question, region)` vision + `read_screen_text(region)`
  OCR + `screen_elements()` scan-then-`click_*` with stale-scan warnings and
  pointer-scale detection. `region` validated; screenshots to
  `STATE_DIR/screen.png`.
- Operator (`operator` OFF by default): `click_element(ref)` by scan number /
  text, `click_at(x, y)` absolute (prefer scan), `scroll(direction, amount)`.
  `open_app` launches by name, waits for the window, reports which.

## Knowledge

`core/web.py`: `BACKENDS = searxng ddg stackexchange hn github wikipedia`;
`_route` is regexes over the query (error→SE, repo→GitHub, news→general),
fallback walk with per-backend failure record (`failure_reasons()`),
one in-flight request per backend via `BoundedRegistry`, TTL cache 300 s,
per-backend timeout 6 s, reader 8 s / 40 000 chars / 6000 into context.
`read_page`: local fetch first, Jina Reader fallback — and the text says
which served it. SSRF: loopback/link-local/private/`.local` refused.
`get_weather`: open-meteo, `home_place` default; severe signals (WMO set,
75 km/h wind) feed briefings, never auto-model-judged.

## Reminders / calendar / ambient signatures

`set_reminder(wake_name, when_due, repeat_hours=0)` — `when_due`: `in …` /
`HH:MM` next occurrence / `YYYY-MM-DD HH:MM`; `repeat_hours` 24/168 common.
`list/cancel/snooze_reminder`. `calendar_month(month='YYYY-MM'|'')`,
`read_calendar(days=1)` (bad `days` refused before config checks).
`notification_reader(action, mute_apps)`, `pomodoro(action, work, break)`,
`media_*` via `mpc`, `world_events(count)` fixed queries only.