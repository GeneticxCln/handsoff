# handsoff — gap analysis & improvement roadmap

Date: 2026-09-09 · Phase 0/1/2 of the external task list are closed (see the
Addendum below); remaining consciously-accepted items are at the bottom.

## Addendum — 2026-09-09 trust & reliability program (P0–P2 closed)

403 tests green across the split suite (`tests/`: audio, policy, desktop,
calendar, settings, lifecycle, regression, ops).

### P0 — the running product is trustworthy
- **Deployment hash reporting** — `_deployment_snapshot()` compares running /
  installed / checkout by sha256 (mtime-independent); surfaced in the
  `health` control command, the Settings health bar (`deploy: ok|drift`),
  and the doctor. The known "installed copy compares against itself" bug is
  fixed and pinned (`TestDeploymentReporting`).
- **Installer manifest** — install.sh writes
  `~/.config/handsoff/deployment.json` (source+installed sha256 per shipped
  file) and prints a post-install `--ptt doctor` step.
- **Doctor** — `run_doctor()` / `--ptt doctor` / `handsoff_doctor` tool: one
  pass over deployment, Ollama, TTS/STT, mic visibility, niri IPC, ydotool,
  restart script, systemd `Restart=`, crash log. Works when the bubble is
  dead (client-side fallback).
- **Stale autostart snippets** — installer now says systemd owns autostart
  instead of the old confusing "remove spawn-at-startup" note.
- **Lifecycle/installed-copy tests** — offscreen launch, restart resilience,
  and a new **installed-copy smoke test**: a fake `~/.local/bin` deployment
  is booted and poked over the control socket (`TestInstalledCopySmoke`).
- **Hardware acceptance checklist** — `ACCEPTANCE.md` (Yeti, TTS echo,
  ydotool, niri IPC, systemd restart/crash-loop/teardown).
- **Runtime hardening (merged from a parallel session)** — `_prepare_runtime()`
  refuses startup when config/state paths are symlinked or not owner-private;
  `ControlServer` owns a stop event + `_remove_stale_control_socket()` that
  only ever unlinks a socket we own (never a symlink/foreign inode), and
  `control.stop()` is wired into `aboutToQuit` so a clean shutdown leaves no
  stale socket inode; the crash report now **truncates** `crash.log` instead
  of unlinking it (faulthandler keeps the fd — unlinking would orphan later
  crashes), pinned by `test_crash_report_ignores_empty_log`.

### P1 — desktop actions are reliable
(landed in the prior session, pinned by tests)
- Live niri capability manifest (`niri_capabilities`, 30 s cache).
- `wait_for_window`, post-action verification on close_window,
  open_app waits for and identifies the launched window.
- Focus verified before every typing operation (`_typing_guard`, fail-closed).
- scroll/wait tools + stale-scan rescan behavior around screen interaction.

### P1 — safer developer automation
- **Bounded jobs** — `start_command` / `job_status`: whitelisted background
  commands with max 4 jobs, 200 KB output, 30 min lifetime cap, kill on
  breach; completion announced via the same channel as watcher alerts.
  Identical refusal policy to run_command (pinned by test).
- **Exact output/status** — job_status returns state, exit code, bounded
  output tail.
- Destructive commands stay denied (BLOCKED list wins; no widening).

### P1 — centralized policy
- **ALLOW / DENY / CONFIRM** via `DecisionPolicy` + `command_policy`
  settings; DENY wins over permission switches; kill_process keeps its own
  two-step flow (not double-gated).
- **One-turn-separated confirmation** — CONFIRM offers out loud;
  `confirm_action('yes')` in a later turn runs the ORIGINAL arguments
  (loop-free via a `_confirm_running` bypass — caught by test).
- **Dry-run mode** — `dry_run: true` makes desktop-action tools report
  instead of act.
- **Decision log** — every decision (incl. DENY/rate-limit/DRY-RUN) appended
  to `~/.local/state/handsoff/decisions.jsonl` with id, ts, tool, target,
  decision, result; capped, and write failures never break tool calls.

### P2 — maintainability & release confidence
- Test file split into `tests/{conftest,audio,policy,desktop,calendar,
  settings,lifecycle,regression,ops}.py` (the monolith is gone).
- Installed-copy tests exist next to checkout tests (see P0).
- `ACCEPTANCE.md` hardware checklist added.
- Pre-commit + CI gates kept, now pointed at `tests/` and compiling the
  split modules.
- Installer, deployed files, service unit, and niri snippets remain
  single-source-of-truth (repo files shipped as-is; manifest added).

Still open, accepted consciously: real-hardware acceptance run (see
`ACCEPTANCE.md`), CI rehearse-runs of the full installer, ICS MONTHLY/YEARLY
RRULE, richer D-Bus notification formatting, and Settings UI rows for
per-tool command_policy (the JSON path and dry-run checkbox exist).

## Addendum — 2026-09-09 hardening audit (post-merge review)

Edge-case audit of `_prepare_runtime` / `_remove_stale_control_socket` (24
new tests in `tests/test_hardening.py`; total 429):

- **Fixed, was a real wedge**: a stale control socket left permissive
  (e.g. created under umask 000) made `_secure_file` refuse it — and since
  `_prepare_runtime` runs at every startup, the bubble could never start
  again until manual removal. Sockets owned by us are now tightened in
  place (chmod 0600), like regular files already were.
- **Fixed, silent failure**: the doctor never looked at the control-socket
  path; a startup-blocking socket problem (symlink, foreign owner, not a
  socket) is now reported explicitly (`REFUSES STARTUP …`).
- **Fixed, short-circuit**: `all(generator)` in `_secure_runtime_files`
  stopped hardening at the first bad file; now every file is processed and
  the AND is returned.
- **Fixed**: `acquire_lock` now chmods `handsoff.lock` to 0600 explicitly
  (the pid inside is runtime state).
- **Pinned by tests**: symlink (incl. broken), directory, foreign-owned,
  regular-file-at-path refusals for dirs, files, and the socket; live
  socket bind under umask 000 self-heals; `_serve` bails out cleanly
  without binding when `_prepare_runtime` refuses.
- **Accepted consciously**: final-component-only symlink checks (parent
  components are mitigated by the resolved-target uid check); the inherent
  lstat→unlink TOCTOU window in single-user session space.
- Removed a dead pre-computed `.json.tmp` path in `_persist_setting` that
  invited reintroducing the predictable-temp-name race.

## Addendum — 2026-09-08 external audit closure

Every priority finding and most "remaining gaps" from the external audit are
now closed (455 tests green):

- Command whitelist bypass via absolute `niri` paths — closed (basename-keyed
  spawn checks; `TestSpawnInterpreterBoundary` extended).
- Cross-turn transcript reuse — closed (per-generation transcript cache).
- Keyboard injection on unknown focus — closed (fail-closed refusal).
- Post-fire spoken snooze — closed (offer cleared only after re-arm).
- Reminder read-modify-write race — closed (`REMINDERS_LOCK` transaction).
- Installer: `pacman -Syu`, single autostart owner, checksum-verified existing
  downloads, per-package python-target probing (CachyOS ships neither
  python-pyside6 nor python-sounddevice; pacman aborts the whole transaction
  on unknown targets — pip fallback), and restart of an already-active
  service so the live bubble runs the just-installed code (pinned in
  tests/test_lifecycle.py).
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
suspend/resume), CI rehearse-runs of the installer, ICS MONTHLY/YEARLY RRULE,
and richer D-Bus notification formatting for applications that emit unusual
Notify argument layouts.

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
- 455 tests, all green
- Ambient automation: opt-in notification reader, Pomodoro transitions, RAM/VRAM
  threshold crossings, and bounded file/process watchers; all have Settings
  controls or safe tool gates

## P0 — reliability gaps (the bubble must never be a zombie again)

1. ~~**No crash recovery.**~~ **Done** — systemd user service with
   `Restart=always` + crash-loop guard; the bubble reports its own crash on
   the next start.
2. **VRAM pressure is the likely killer.** whisper `large-v3` (~1.5 GB) + 24B
   Q4 LLM (~14 GB) exceed 16 GB together with desktop usage.
   → Default whisper back to `tiny` (or auto-select by free VRAM at load).
3. ~~**Crash evidence is invisible.**~~ **Done** — startup crash report plus
   the crash-log line in `--ptt doctor`.

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

## Addendum — 2026-09-09 second full-project audit (concurrent-hardening review)

Scope: independent review of the hardening change set landed concurrently with the
workspace-index work (settings locking + reload, `run_command` rework,
`_quarantine_bad`, spawn hardening, installer changes, CI/pre-commit, docs). Audited
at the settled state; gates re-run on that exact tree.

**Gates:** `py_compile` + `bash -n` clean; **455 tests green**; runtime tool census
**46, unchanged** (grep over `@tool` over-counts because of the docstring example);
doctor functional. The README "440+ tests" bump is accurate.

Findings, ranked:

1. **Deployment drift (live, fix before sign-off):** the installed `~/.local/bin`
   copy predates the combined tree; `--ptt doctor` correctly reports
   `installed-drift` — the trust feature doing its job, but the deployed code is
   behind the tested source. → Re-run `./install.sh` once this change set commits.
2. **Installer default `-Syu` → `-Sy`:** flagged on first read as a silent
   partial-upgrade regression; on inspection it is a *documented* default
   (`-Sy` for fast installs, `HANDSOFF_FULL_UPGRADE=1` selects supported `-Syu`)
   and `test_pacman_python_targets_probed_individually` still pins `-Syu` behind
   the flag. Accepted consciously — but `-Sy` + `-u`-less installs can leave the
   system on a partial upgrade; the note in install.sh documents this.
3. **README hotkey fix verified against the machine:** the live niri config binds
   `Mod+Shift+H` for hands-free (`Mod+H` is niri's own `focus-column-left`), so the
   old doc line was actively wrong. Docs now match config.
4. **Spawn hardening is thorough:** interpreter/terminal-argument bypass routes,
   absolute-path `niri` lookalikes (basename-keyed checks), and blocked basenames
   inside arguments are all closed; boundary tests extended accordingly.
5. **Policy layer survives the rework:** the P1 ALLOW/DENY/CONFIRM
   `DecisionPolicy`, dry-run reporting, one-turn confirmations, and the
   id/tool/target/decision/result decision log are all intact after the
   `run_command`/jobs rewrite.
6. **Infra fixes real and pinned:** restart script exact-pid wait, curl timeouts in
   calendar/web fetch paths, `pytest.ini` `testpaths`, installed scripts deployed
   executable (fixes a real PermissionError), CI rehearse job kept.
7. **Hygiene:** no secret-shaped strings in the diff (grep hits are concurrency
   tokens and token-budget state); earlier installer fixes (per-package python
   probes, restart-if-active) survived the rework; stale "400+ tests" doc counts
   updated.

Accepted consciously / out of scope here: Settings health-bar pixel verification on
the dual-monitor setup (data-level verified in `ACCEPTANCE.md`), and the six
human-only acceptance items. Process note: files churned mid-audit while the other
agent worked; all findings above were confirmed against the settled tree.