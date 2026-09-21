# handsoff — tools API

Source: `core/tools.py` — **52 `@tool` methods**, counted from the decorators that
define them. The census below is GENERATED (`python3 ci/spec_tables.py --write`);
do not hand-edit it, and do not copy a number out of it into prose.
`gates=None` (decorator omits `gates=`) means "own name"; `gates=''` means
ungated. Descriptions are the pinned model-facing strings in the decorator.

## Census (name | effective gate | line)

| Tool | Gate | L |
|---|---|---|
| `run_command` | `run_command` | 1364 |
| `type_text` | `type_text` | 1593 |
| `press_keys` | `press_keys` | 1656 |
| `press_hotkey` | `press_keys` | 1759 |
| `notification_reader` | `notifications` | 1818 |
| `pomodoro` | `pomodoro` | 1856 |
| `watch_file` | `watchers` | 1932 |
| `watch_process` | `watchers` | 1981 |
| `quant_space_status` | `quant_space` | 2074 |
| `quant_space_sessions` | `quant_space` | 2086 |
| `quant_space_read` | `quant_space` | 2097 |
| `quant_space_check` | `quant_space` | 2130 |
| `workspace` | `run_command` | 2153 |
| `focus_window` | `focus_window` | 2271 |
| `wait_for_window` | `focus_window` | 2317 |
| `wait` | — | 2339 |
| `niri_capabilities` | — | 2440 |
| `close_window` | `run_command` | 2470 |
| `copy_text` | `copy_text` | 2530 |
| `paste_text` | `paste_text` | 2542 |
| `set_reminder` | `reminders` | 2557 |
| `list_reminders` | `reminders` | 2610 |
| `cancel_reminder` | `reminders` | 2626 |
| `snooze_reminder` | `reminders` | 2645 |
| `media_play` | `media` | 2683 |
| `media_control` | `media` | 2719 |
| `media_volume` | `media` | 2738 |
| `now_playing` | `media` | 2750 |
| `search_library` | `media` | 2767 |
| `calendar_month` | `calendar` | 2786 |
| `read_calendar` | `calendar` | 2816 |
| `get_weather` | `web_access` | 2860 |
| `web_search` | `web_access` | 2889 |
| `read_page` | `web_access` | 2915 |
| `world_events` | `web_access` | 2928 |
| `lookup_fact` | `web_access` | 2952 |
| `get_datetime` | `get_datetime` | 2969 |
| `see_screen` | `screen_access` | 3012 |
| `read_screen_text` | `screen_access` | 3030 |
| `kill_process` | `run_command` | 3117 |
| `confirm_kill` | `run_command` | 3145 |
| `confirm_action` | — | 3175 |
| `start_command` | `run_command` | 3263 |
| `job_status` | `run_command` | 3312 |
| `handsoff_doctor` | — | 3360 |
| `screen_elements` | `screen_access` | 3460 |
| `click_element` | `operator` | 3482 |
| `click_at` | `operator` | 3499 |
| `scroll` | `operator` | 3504 |
| `open_app` | `run_command` | 3537 |
| `read_file` | `read_file` | 3627 |
| `edit_file` | `edit_file` | 3657 |

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
which served it. SSRF: loopback/link-local/private/`.local` refused, the
redirect chain walked one hop at a time with EVERY hop put back through the
same destination check, and the addresses each hop was checked at handed to
the fetch (`connect_to`) so the address dialled is the address checked — the
name is never resolved a second time behind the check. A hop seam that cannot
be pinned is used, and warned about; the resolver is bounded by
`_DNS_TIMEOUT_S` because `getaddrinfo` has no timeout of its own.
`get_weather`: open-meteo, `home_place` default; severe signals (WMO set,
75 km/h wind) feed briefings, never auto-model-judged.

## Reminders / calendar / ambient signatures

`set_reminder(wake_name, when_due, repeat_hours=0)` — `when_due`: `in …` /
`HH:MM` next occurrence / `YYYY-MM-DD HH:MM`; `repeat_hours` 24/168 common.
`list/cancel/snooze_reminder`. `calendar_month(month='YYYY-MM'|'')`,
`read_calendar(days=1)` (bad `days` refused before config checks).
`notification_reader(action, mute_apps)`, `pomodoro(action, work, break)`,
`media_*` via `mpc`, `world_events(count)` fixed queries only.