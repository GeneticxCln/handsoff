# handsoff — tools API

Source: `core/tools.py` — **52 `@tool` methods**, counted from the decorators that
define them. The census below is GENERATED (`python3 ci/spec_tables.py --write`);
do not hand-edit it, and do not copy a number out of it into prose.
`gates=None` (decorator omits `gates=`) means "own name"; `gates=''` means
ungated. Descriptions are the pinned model-facing strings in the decorator.

**The effective gate also decides whether a tool is OFFERED.** Every schema here
is JSON'd into every round, and `SETTINGS["permissions"]` is keyed by the gate —
one dropdown per family in the settings app — so a family switched off stops its
schemas being sent at all (`permitted_tools`, applied per round from live
settings): a tool the user cannot call is a tool the prompt should not pay for.
The belt refuses such a call anyway, so nothing is lost by not listing it — and
the names of the switched-off families travel in the system prompt, because the
"disabled in handsoff settings" sentence was the one that named the switch.
Ungated tools always ship, and a gate the filter has never heard of fails OPEN,
matching the belt's own check at call time.

## Census (name | effective gate | line)

| Tool | Gate | L |
|---|---|---|
| `run_command` | `run_command` | 2516 |
| `type_text` | `type_text` | 2832 |
| `press_keys` | `press_keys` | 2895 |
| `press_hotkey` | `press_keys` | 3000 |
| `notification_reader` | `notifications` | 3059 |
| `pomodoro` | `pomodoro` | 3173 |
| `watch_file` | `watchers` | 3274 |
| `watch_process` | `watchers` | 3323 |
| `quant_space_status` | `quant_space` | 3419 |
| `quant_space_sessions` | `quant_space` | 3434 |
| `quant_space_read` | `quant_space` | 3446 |
| `quant_space_check` | `quant_space` | 3481 |
| `workspace` | `run_command` | 3505 |
| `focus_window` | `focus_window` | 3623 |
| `wait_for_window` | `focus_window` | 3669 |
| `wait` | — | 3691 |
| `niri_capabilities` | — | 3792 |
| `close_window` | `run_command` | 3822 |
| `copy_text` | `copy_text` | 3882 |
| `paste_text` | `paste_text` | 3907 |
| `set_reminder` | `reminders` | 3938 |
| `list_reminders` | `reminders` | 4002 |
| `cancel_reminder` | `reminders` | 4018 |
| `snooze_reminder` | `reminders` | 4037 |
| `media_play` | `media` | 4075 |
| `media_control` | `media` | 4111 |
| `media_volume` | `media` | 4134 |
| `now_playing` | `media` | 4157 |
| `search_library` | `media` | 4185 |
| `calendar_month` | `calendar` | 4204 |
| `read_calendar` | `calendar` | 4234 |
| `get_weather` | `web_access` | 4278 |
| `web_search` | `web_access` | 4307 |
| `read_page` | `web_access` | 4333 |
| `world_events` | `web_access` | 4346 |
| `lookup_fact` | `web_access` | 4370 |
| `get_datetime` | `get_datetime` | 4387 |
| `see_screen` | `screen_access` | 4434 |
| `read_screen_text` | `screen_access` | 4452 |
| `kill_process` | `run_command` | 4539 |
| `confirm_kill` | `run_command` | 4572 |
| `confirm_action` | — | 4617 |
| `start_command` | `run_command` | 4705 |
| `job_status` | `run_command` | 4765 |
| `handsoff_doctor` | — | 4813 |
| `screen_elements` | `screen_access` | 4936 |
| `click_element` | `operator` | 4958 |
| `click_at` | `operator` | 4975 |
| `scroll` | `operator` | 4980 |
| `open_app` | `run_command` | 5013 |
| `read_file` | `read_file` | 5108 |
| `edit_file` | `edit_file` | 5165 |

## Result contract

`ToolResult(text, kind)` — the failure flag is CARRIED by the producer, not
re-derived by sniffing text at seven call sites. Text convention (single
definition, `tool_kind`): starts with `ERROR:`/`REFUSED:` (+ word boundary)
= failure; anything else = ok. `execute()` enforces policy → confirm-offer →
rate limit → `_execute` → decision log (`decisions.jsonl`, 500-line cap).

**A failing tool is a message, never a lost turn**, and it is guarded at two
layers because the belt's own DECISION path is not the tool. `_execute` wraps
`_execute_decision` — rate limit, permission gate, `classify`, the CONFIRM
pre-check, the diff previews — in `except Exception`, so a raise in the
assistant's own code before dispatch becomes
`ERROR: <tool> could not be evaluated — the assistant's own check for this
call failed with <Type> before the tool ran, so nothing was dispatched…`
(measured 2026-09-26: a self-edit carrying a lone surrogate made the pre-check's
`compile` raise `ValueError`, and it went out through `execute` into the app's
tool loop, which had no handler — the turn died on "Sorry, something went
wrong"). The claim "nothing was dispatched" is structural, not hopeful: every
line after `log_decision(..., 'dispatched')` is inside the inner handler. The
app's loop then calls `Assistant._tool_result_entry`, which wraps the belt
itself and normalises the arguments to a dict BEFORE logging them (the log
line used to sort the raw parsed value, and `"[{\"a\": 1}, {\"b\": 2}]"`,
`"[7]"` and `"7"` all raised `TypeError` there — measured). Both handlers are
`except Exception`, never `BaseException`, so a shutdown signal still stops the
app.

**And what a tool body may raise is DECLARED, not assumed.** The dispatch had
no contract: every exception became `ERROR: <str(exc)>`, and two measured
shapes came out of that. A body raising with no message produced the entire
message `ERROR: ` — the model is told something failed and given nothing, the
very failure this path exists to prevent. And a `TypeError` from inside a
body was reported as `ERROR: bad arguments for read_file: …`, sending the
model to re-check arguments when the tool was what was broken; that clause
existed to cover the BINDING step, which already had its own handler, so the
type only had to be a bug to be caught by accident.

So `@tool` takes `raises=` — the exception types a body raises on purpose,
one class or a tuple — and the dispatch reads it. Three arms, and each is
pinned by exactly one test (mutations M1–M4 in GAP_ANALYSIS.md):

- **declared** → the tool's own sentence, unprefixed and unedited, kind
  `refused`, logged at warning. Refusing is answering: the desk's ten states
  are written for a person, and wrapping one in `ERROR:` throws away the only
  part the user can act on. A traceback per "the desk is not running" would
  fill the journal with stack traces of things that worked.
- **declared but silent** → fails CLOSED into the bug arm. A class that
  promises a sentence and raises with none is a tool that did not keep its
  promise, and an empty tool message is the same hole `ERROR: ` was.
- **anything else** → a bug, reported as one: the tool, the type, whatever the
  exception said (or that it said nothing), and which of the two things went
  wrong. `ERROR: <tool> failed with <Type>: <what it said> — that is a bug in
  the tool, not a refusal, and the arguments are not what is wrong.`

`raises=` is a claim about a CLASS, and the class has to mean one thing. The
tree declares exactly one contract — `core.qs_desk.DeskError` on the four
`quant_space_*` tools — because it is the only exception in the belt that
means "a refusal, in words written for a person"; every other tool refuses by
returning a string. `raises=RuntimeError` is not a contract, it is an
apology: it would swallow every crash in that tool and report it as a polite
sentence the user then acts on, which is worse than `ERROR:` because it is
believed. `TestTheToolsExceptionContract` refuses the generic builtins, checks
every tool carries a contract at all, and drives one call site with BOTH
classes so the two stories cannot be confused.

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
restart script + `git {status,diff,log,show,branch,remote}` (`branch` is an
allow-list of LISTING flags — a bare name creates a branch, and git accepts a
unique prefix like `--dele`, so nothing else passes: `_git_branch_write`) +
`cargo {build,check,test,clippy}`. Blocked (substring, word-boundaried):
`sudo rm pacman yay paru shutdown poweroff reboot halt mkfs dd kill chmod
chown mount umount curl wget bash sh zsh fish python python3 pip mv cp tar
zip 7z make gcc systemctl journalctl tee xargs env eval exec` (+ interpreter
detection `_is_interpreter`). Refused outright: empty, shell operators
`;|&`$\n\r<>`, unparseable `shlex`, secret-path args, `niri spawn` outside
the app allowlist. `TIMEOUT = 15` s. Every invocation logged with redacted
target (`_log_target`, secret values redacted).

`ps` is whitelisted, and procps' `e` is an UNDASHED keyword that appends every
selected process's environment (`ps ef` is the everyday spelling), so the
keyword is refused while the dashed all-processes flags (`-e`, `-A`, `-ef`,
and a cluster like `-Aew`, which procps parses as flags) stay allowed. Value
slots are read off procps' own arity so a selector that happens to contain an
`e` (`ps -C firefox`, `ps -o etime`, `ps -eo pid,etime,comm`) is not mistaken
for one.

Two more whitelisted programs WRITE, and neither writes through a verb
(`_validate_write_verb`, checked after the whitelist and beside the niri
route). `nvidia-smi`'s `-f/--filename` writes the query output to a file
instead of returning it (`-l/--lms` are `--loop`/`--loop-ms` here, so they are
reads and stay allowed); `pactl` is gated on an ALLOW-list of 26 verbs — the
reads, the volume/mute family and `send-key` — while `load-module`,
`unload-module`, `exit`, `send-message`, the three sample verbs, `suspend-*`,
`set-card-profile`, `set-sink-formats` and `set-port-latency-offset` are each
refused with their own reason, and a verb pactl does not have is refused as
the silent no-op it is (pactl prints "No valid command specified." and exits
0). The two constants are checked against `pactl --help` in both directions, so
a later release cannot reach the audio server unremarked.

**The spawn route opens APPS, not programs.** `niri msg action spawn`,
`niri msg spawn` and the `spawn-sh` spelling all reach the compositor's
launcher, and the gate is an ALLOW-list (`_NIRI_SPAWN_APPS`, ~110 GUI apps
across terminals, browsers, file managers, editors, media, chat and the
system panels) rather than the deny-list it replaced — with a deny-list,
anything installed that was not explicitly forbidden launched, and `touch`,
`id` and `ffmpeg` all did. Three rules, and the order is the order of how bad
the refusal is: an interpreter, a blocked program or git/cargo keeps its own
specific reason; a name that is not on the list is refused with the list's
size and a pointer to `open_app`; a name carrying a directory must BE that
program (`_is_the_program`); and there are NO arguments at all, because the
list holds script hosts (gimp `--batch-interpreter`, inkscape `--actions`,
LibreOffice `macro:///`) whose flags cannot be enumerated for a list the gate
does not own. The whitelisted `spawn` PROGRAM is the same capability with a
different door, so it shares the one owner (`_gui_app_verdict`) rather than
carrying a second copy of the policy. `open_app` remains the general
launcher: any installed app by name, no arguments, and it waits for the
window.

**The policy's own shape is guarded, in every boundary that decides.**
`_ReachabilityWalk` holds the machinery; two test classes run it, one per
group of boundaries. `run_command` (`TestCommandPolicyRefusalReachability`)
walks the four functions that decide whether a command runs
(`_validate_command`, `_validate_write_verb`, `_gui_app_verdict`,
`_validate_niri_spawn`). `read`/`edit`/`kill`/`web`/`desk`
(`TestToolBoundaryRefusalReachability`) walk `read_file`,
`denied_secret_path` and `_secret_reason`; `edit_file` and
`_classify_edit_path`; `kill_process` and `confirm_kill`; the SSRF guard
in `core/web.py`; and the desk client in `core/qs_desk.py` — every class
each of them holds is named and has a corpus entry that provably reaches
it, and the five boundaries hold
**119 refusal classes in all** (32 / 10 / 11 / 10 / 16 / 40).

Three facts per boundary. No refusal may sit under a chain of conditions that
admits no executable — the chain is intersected into a set, `and` included,
and an empty set is a dead guard. (That arm stays with the command boundary:
`exe_base` is what the question is about, and running it over the others
would be a check that cannot fail.) And every refusal class has a command
that PROVABLY reaches it: the call runs under `sys.settrace` and the
refusal's own line must appear in the trace, so reachability does not depend
on a message that can be reworded or on a second guard returning the same text
first. Both directions are checked, so a guard cannot be added without a
command and cannot be switched off quietly.

**A trace proves a line ran, not that anyone was told.** The third fact
closes the shape the first two cannot see: a guard whose helper is called as
a bare statement

    self._validate_write_verb(argv, exe_base)      # verdict dropped

runs every line of the refusal, satisfies the trace, and returns `None`, so
`nvidia-smi -f /tmp/gpu.csv` executes. The walk was green on exactly that
mutation, which is the measurement behind the check. So the value the caller
is handed must BE this class's own message, matched on a verbatim fragment of
it (the longest single string constant of the return expression, so an
f-string hole never invents text the real message lacks, and two classes
sharing an opening cannot be confused). A fragment shorter than
`_MIN_VERDICT_FRAGMENT` (18; the shortest real one is 19, `ERROR: cannot
read `) is reported rather
than matched loosely, so the check cannot pass on a fragment too generic to
identify a class. The two behavioural checks share one run of the corpus
(`_corpus_results` returns the call, the class, the lines executed AND the
verdict), so they cannot disagree about what happened.

**A boundary declares its hops, and the corpus has to run them.** `funcs` maps
each function to what counts as a refusal in it, and the rules grew with the
modules that refuse in different shapes: `"marker"` for a tool-level message
carrying `REFUSED`/`ERROR`, `"reason"` for a guard that answers in its own
sentence with no marker word (`_secret_reason`: every return that is not `None`
is a class), `"raise"` for a module that refuses by EXCEPTION (the desk
client, where the sentence is in the raised call), `"problem"` for one that
hands back a REASON beside a verdict (the SSRF guard: a `(…, problem)` tuple
whose last element is a non-empty string), and `None` for a HOP — a function
that owns no sentence but decides what the caller is told.
`denied_secret_path` (whose return can be dropped on the way to `read_file`),
`_classify_edit_path` (whose KIND decides which refusal fires, and which lives
in `handsoff.py`, so the trace watches two files), the SSRF guard's own
`_resolve_host` and `_hop`, and the desk's `_pid_state` and `session_names`
are declared that way, and a corpus that never executed one is a failure. A
function is declared by being part of the
boundary, so renaming or deleting it fails loudly instead of quietly leaving
the corpus aimed at nothing.

A boundary that refuses by EXCEPTION needs two more statements, and both were
measured rather than designed. `error_class` names the class a refusal IS, so
that a returned VALUE is not read as one — `_as_paths` raises three refusals
and returns a path list, and without it `return discovery_paths()` would be
a class with no sentence. And a raise of another of the boundary's OWN
functions is a HOP, because the sentence belongs to the function that built
it: `raise _ambiguous(wanted, exact)` owns no words, and `_ambiguous` is
where the class and its command live. The desk's verdict is the exception's
own `__str__`, which is what the tools read out loud, so `raises: True` turns
an exception into the verdict the second check looks for — and re-raises
anything that is not the boundary's own, so a broken fixture cannot read as
a clean refusal.

Two more things the second module needed. A boundary's subject need not be the
belt — `core/web.py` is a module and the walk calls it as one, and the desk
client is neither: its subject is a HANDLE (`Desk`), built by the boundary's
own `subject_setup` and wired to a scripted socket, because that is the
object the four `quant_space_*` tools call. A refusal the handle does not
carry is addressed to the module with a leading `@` (`@resolve_session`). A
refusal
whose text lives in a MODULE CONSTANT (`DeskError("not-granted", message or
_NOT_GRANTED_FALLBACK)`) is followed to the constant it names, because the
class that speaks those words is the mapping, not the eleven characters of
`"not-granted"`. The SSRF guard's sentences are short noun phrases by design,
so that boundary carries its own fragment floor (15, measured — `" has no
address"`), and all sixteen of its fragments are distinct. The desk's
sentences are sentences, so the belt's 18 holds with room to spare (the
shortest of its forty is 21, `Quantum Space refused `).

Each refusal carries its own name, as a `# refusal: <name>` comment at the
site (63 in `core/tools.py`, 16 in `core/web.py`, 40 in `core/qs_desk.py` —
119 in all, one per class), and the corpus is keyed by that name. A
source-order index was the first attempt and it was wrong twice over: adding
a guard above another renumbered the whole table, and a message reword would
have broken it as well. The name is neither — it travels with the code, so a
guard can move seventy lines and a refusal can be reworded without the corpus
noticing. A refusal with no marker, or two refusals sharing one, is a failure
with the site named, so a class cannot be added anonymously or made
ambiguous. Where two classes share their text exactly (the two
`confirm_kill` "nothing to confirm" returns) the LINE check is what separates
them, and the fragment check is honest about only being able to see the
message.

It exists because a guard block inserted mid-`_validate_command` once nested
the git write-flag checks inside `if exe_base == 'ps':` — dead code, and one
test was the whole net. Walking the other boundaries found the same
shape of mistake in a different file, plus guards that were MISSING
outright. `edit_file` caught only `SyntaxError` around its compile, and
`compile` raises `ValueError` for a NUL byte or a lone surrogate, so such a
payload escaped the tool and the tool loop, and one malformed edit ended the
whole turn on "Sorry, something went wrong" (`edit_refuses_unencodable_source`).
The desk client had the same shape twice over: `_load` judged the discovery
file's mode, its JSON and its shape, and never whether it was TEXT — and
`read_text` refuses a non-UTF-8 file with `UnicodeDecodeError`, a
`ValueError` and not an `OSError`, so a 0600 file with one bad byte walked
out of all ten states and out of every `except DeskError` in the four desk
tools, and the user was told the ASSISTANT's own check had failed before the
tool ran (`control_file_is_not_text`). And `Desk([path, 7])` reached
`Path(7)` and answered with Python's `argument should be a str or an
os.PathLike…`, naming neither what to pass nor the mistake, while `Desk(7)`
— the same mistake one spelling out — was refused by name
(`a_path_list_holds_a_non_path`).

## Files

- `read_file(path)`: UTF-8, `MAX_READ` 160 KB; refuses secret paths
  (`denied_secret_path`: credential/key/history/browser-profile dirs, secret
  globs, and `/proc/*/environ` + `/proc/*/cmdline`, which the kernel GENERATES
  from a live process rather than storing). "Never ask the user to paste those."
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
`read_page`: local fetch first, Jina Reader fallback **only when the
`hosted_reader` setting is on** (default off since 2026-09-27 — the fallback
sends the target ADDRESS, not the search text, so it has its own switch; an
unwired seam, a False resolver or one that raises all read as off) — and the
text says which served it. SSRF: loopback/link-local/private/`.local` refused, the
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