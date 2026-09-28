# handsoff — operations

Sources: `install.sh`, `handsoff.py` (`main`, `_deployment_snapshot`,
`ControlServer`, `ptt_client`), `handsoff-restart`, `hardware.py`,
`core/doctor.py`.

## 1. Install (`./install.sh`, Arch/CachyOS + niri)

1. `pacman -Syu` + probes per package: python-pyside6, python-sounddevice,
   python-numpy, ollama, curl (+ Qt/ALSA runtime libs).
2. `pip install --user -r requirements.txt` (numpy, PySide6, sounddevice;
   faster-whisper/openwakeword/onnxruntime lazy) + distro-torch-first
   chatterbox-turbo (`HANDSOFF_TTS_REPO`).
3. Stage to `mktemp -d` (never predictable `staged.$$`): shipped set =
   `TOP_REQUIRED` (`handsoff.py settings_schema.py hardware.py`) +
   `handsoff-settings.py` + `handsoff-restart` + `core/` set
   (`CORE_REQUIRED`: `__init__ registry settings audio brain tools doctor
   lifecycle calendar assistant bubble web theme qs_desk`) + any further
   `git ls-files '*.py'`; tarball (no git) fallback = declared set only.
   `CORE_REQUIRED` missing → stage fails loudly. `py_compile` gate on stage.
4. Whisper model SHA256-verified atomic download (`HANDSOFF_WHISPER`,
   `HANDSOFF_WHISPER_REVISION`); chatterbox weights prefetched + usability
   checked.
5. Atomic switch `~/.local/bin` (0755 entry points), previous release kept
   for `--rollback`; manifest written to `deployment.json` (per-file
   sha256, whisper sha, model ids, and the deployed git state: `git_commit`
   + `git_dirty`, the dirty flag judged over the shipped paths only so
   uncommitted test/docs edits beside a clean deploy stay honest).
6. systemd user unit `handsoff.service`: `ExecStart %h/.local/bin/handsoff.py`,
   `Restart=always`, `PartOf=graphical-session.target`,
   `WantedBy=graphical-session.target`. Exactly ONE autostart owner:
   systemd enabled → no niri `spawn-at-startup` (checked, not assumed).
7. niri window rule MERGED into `~/.config/niri/config.kdl` (honouring
   `NIRI_CONFIG`), inside a marker-delimited managed block: a non-empty
   `.bak-handsoff` taken before the first write and never overwritten, an
   atomic replace, the pre-marker block this installer used to ask the reader
   to paste by hand recognised and replaced rather than doubled, and a
   best-effort `niri msg action load-config-file` that reports rather than
   fails when no compositor is running. The `niri-window-rule.kdl` snippet is
   still written, as the copy the rehearsal verifies.
8. Flags: `--help` (pure, CI-smoked), `--rehearsal` (redirect HOME, no host
   changes), `--rollback`, `--uninstall` (manifest loop; `core/` + `~/.local/bin`
   dropped only when empty-or-bytecode — never `rm -rf` a shared dir),
   `--uninstall --purge` (backs up config/state first).

**One source per provisioned value.** A step that provisions something the app
has already decided reads the app rather than repeating the decision:

| step | read from | how |
|---|---|---|
| 4 whisper model | `settings.json` → `whisper_size` | `_resolve_whisper_size`; a size outside `settings_schema.WHISPER_SIZES` is refused with the list named |
| 4 whisper revision | `HANDSOFF_WHISPER_REVISION` | env only — nothing in the app decides it |
| 6 speech repo | `core/audio.py` → `TTS_REPO_ID` | `_app_constant` (`ast`, never an import: `handsoff.py` builds a QApplication) |
| 7 app-id in the niri rule | `handsoff.py` → `APP_NAME` | same reader; the rule text takes `@APP_ID@` and `sed` substitutes it (an unquoted heredoc would treat `$"` as a locale expansion) |
| 8 model | `settings.json` → `model` | `_read_setting` |
| 8 server | `settings.json` → `ollama_host` | `_resolve_ollama_endpoint`, which mirrors the app's own remote rule: a non-loopback host is used only when `allow_remote_ollama` is **strictly** `true` in settings (or the send guard's env tokens), otherwise the script says so and checks loopback, because the bubble refuses that endpoint anyway |

The reader is failure-tolerant by construction (missing file, missing key, an
interpreter that will not answer): it prints nothing and the named `DEFAULT_*`
fallback stands, so no state of the user's config can stop an install. Step 8
points the `ollama` CLI at the same endpoint the app uses (`OLLAMA_HOST`) and
never starts the local service for a remote host. Three literals that were
provisioned twice are gone with it: a configured `whisper_size: small` used to
have `tiny` downloaded and recorded in the manifest; a hardcoded loopback was
probed, started and filled while the bubble talked to another server; and the
speech repo name was a second copy of the one the app reads.

Step 8's tool-capability check is tri-state. It reads only the `Capabilities`
section printed by `ollama show`: an exact `tools` entry confirms support, a
successful parse without it warns, and a failed command or unrecognized output
is explicitly unanswerable and skips the warning. A transient `ollama show`
failure must never be presented as evidence that a model lacks tools.

## 2. Deployment truth

`_deployment_snapshot()` (`handsoff.py:758`) hashes running vs repo vs
installed per file over `manifest.files ∪ _DEPLOY_FILES`, compares hashes
(not mtimes). Statuses: `in-sync`, `installed-drift`, `source-unknown`,
`installed-missing`, `running-missing`. Surfaced three ways: `--ptt doctor`
(`deployment: in-sync — …`), `--ptt health` (`"deployment"` section), settings
health bar (`deploy: ok`). No settings values or calendar secrets in the
payload. `--preflight` (`hardware.py main`) runs on a bare checkout (stdlib
only).

## 3. Runtime (`main()`)

`_prepare_runtime()` (private dirs + 0600, symlink-refuse, stale-scratch
sweep ARCHIVED under `scratch-quarantine/<date>/` for
`SCRATCH_QUARANTINE_TTL_DAYS` days rather than deleted, recorded into
`_LAST_SWEEP` for the doctor; latched once per state dir
per start, because `ControlServer._serve()` calls `_prepare_runtime()` again
and a second no-op sweep would overwrite the reclaim's own record) →
logging + `_log_swept_scratch()` (the sweep runs before the journal exists:
it has to, the rotated log lives in the state dir it cleans; the same start
appends its post-reclaim reading to the capped `state-hygiene.jsonl` trend,
`_record_state_hygiene()`) +
`threading.excepthook` + faulthandler on `crash.log` → version/settings/
brain/mic banner → `acquire_lock()` (flock; second copy exits 0) →
`QApplication` (`setApplicationName`, Wayland app-id for niri rules) →
`Assistant.start()` → `ControlServer.start()` → `bubble.show()` →
`app.exec()` → stop/shutdown/join on quit. Headless without
`WAYLAND_DISPLAY/DISPLAY` warns, window may fail. One bubble per session.

## 4. Control socket (23 verbs)

`PTT_ACTIONS` (`handsoff.py:6500`): start stop toggle interrupt handsfree
handsfree-on handsfree-off handsfree-status dictation dictation-on
dictation-off status health level doctor settings selftest reload-settings
clear-history say preview-pack preview-clear stop-audit. Read-only (no token):
`status health level doctor handsfree-status`. Everything else needs
`token=<64 hex>` first line (constant-time compare); token lives in
`control.token` (0700 state dir + `_peer_uid` gate). The doctor's `state:`
line (and the JSON surface's `state_hygiene` key) reports the state dir's
hygiene from ONE host collector: scratch-shaped entries present (the same
`_scratch_shaped` predicate the sweep removes by, minus the age gate), the
last sweep's result (`_LAST_SWEEP`: when, how many removed, whether the dir
was unreadable), and total size/entry count — a host without the dep prints
no line. Request caps: 65536 B,
5 s wall budget; single accept thread; runs admitted via
`BoundedRegistry("control", 1)`; diagnostics via `("diagnostic", 1)` (second
request refused by name with the cause in the reply). `ptt_client` maps verbs
to exit codes; `USAGE` lists the surface; unknown verb echoes the sorted set.

## 5. Doctor, health, selftest

`run_doctor()`/`doctor_json()` (`core/doctor.py` via `DoctorDeps`):
deployment hashes, Ollama reachability + model, TTS/STT weight state, mic,
niri/compositor, systemd unit, ydotool socket (DGRAM-first probe mirroring
`ToolBelt._socket_connectable`), voices, restart script, control socket,
crash log, cap-refusal note, appearance look, web backend record
(observed-only: `untried`/ok/named failure). `--ptt health` = JSON snapshot
(assistant/handsfree/followup_armed + `mic`, `notifications`, `brain`, `tts`,
`appearance`, `deployment` sections). `notifications` is the reader's own
vital signs — `state` (off/running/retrying/stopping/**stalled**/gave-up),
`passes`, `notifications`, `failures`, `attempts_used`/`attempts_budget`,
`backoff_seconds`, `pass_seconds`, `last_failure{where,error,age_seconds}` —
because a reader that says nothing is the healthy state AND was the state a
wedged one sat in; `stalled` (the setting says on, nothing listening) is the
one state the reader cannot report about itself, so the host passes the live
setting in. `--ptt selftest` = typing checks of ACCEPTANCE §5. Settings health bar renders
`mic: … · brain: ok <model> · tts/stt: ok · deploy: ok` via `_fmt_health()`.

## 6. Restart + crash recovery

`handsoff-restart`: systemd-first (`restart handsoff` when a unit manages
the bubble; manual TERM/KILL only otherwise — the reverse order once caused
a transient double bubble via `Restart=always` racing the script).
Crash: SEGV → systemd restarts ~5 s; next boot speaks a short report and
names the crash log (truncated-not-unlinked, same inode, 0600). Crash-loop
(5+ rapid) → unit `failed` (`Start request repeated too quickly`);
`reset-failed + start` recovers. Session end → clean exit via
`PartOf=graphical-session.target`.

The stop-attribution ledger (`handsoff-stop-probe`, wired as the unit's
`ExecStop=`): a stop job's invoker is visible in /proc only while the job
blocks it, so the probe runs at exactly that instant and appends one JSON
line per stop to `stop-attribution.jsonl` (0600) — the callers (pid, exe,
120-char cmdline head, ≤6-hop ancestry newest-first), a `shutdown` flag and
a note. ExecStop runs ONLY on stop jobs (explicit stop/restart/session
teardown); a crash respawn under `Restart=always` does not run it — "it
died" and "someone stopped it" are different events, and the journal covers
the first. `callers:[]` is itself information: a session shutdown, a direct
D-Bus call, or a caller already gone when the probe looked. The `shutdown`
flag marks stops that ran inside the session-exit sweep, detected bus-free
via the `invocation:exit.target` symlink under `$XDG_RUNTIME_DIR` (the bus
may already be dead mid-poweroff; `systemctl is-active exit.target` is the
belt) — a night poweroff 105 s after a deploy restart is the benign pair
that used to read as the killer shape. The ghost-pattern rule
(`_ghost_stop_pattern`, health `stop_attribution.ghost_pattern`): an
unattributed NON-shutdown stop that FOLLOWS an attributed one within 60 min
(last 50 rows, parsed datetimes, negative gaps never count) is the
invisible-killer shape and doctor renders a PATTERN line — even when the
latest stop was attributed, because a killer that alternates must not slip
between two clean lines; `seen: false` is reported whenever the ledger
exists (the cap-refusals idiom: "none" must be distinguishable from
"never checked"), and a shutdown-annotated ghost renders `expected, not an
anomaly`. The reader adjudicates; doctor formats.

`ci/desk_retest.py` is the desk retest in one command (ACCEPTANCE §10's
automated half): it plants `tmpdeskcheck*` probes in the real state dir,
restarts the unit once per check that needs a fresh start, reads the journal
and the doctor's `state:`/`wake:` lines back, and prints a paste-ready
evidence block whose last line names the items only a person can close (A1–A4,
a real voice). `--only B1,C4` narrows it, `--dry-run` changes nothing, and
`--simulate` rehearses the whole thing against a throwaway state dir with the
checkout's own code and no systemd (B1's SIGKILL and A5's arming warning then
report themselves desk-only);on the real backend B1 speaks aloud and kills the bubble mid-synthesis on
purpose, and B5 briefly renames the real
`scratch-quarantine`. Its cleanup deletes only its own `tmpdeskcheck*`
probes: B1's archived REAL reply survives the run for §10's B2 `aplay` and
leaves through the archive's own 7-day TTL (the first live session deleted it
at exit, found 2026-09-25). Its restarts are PACED to the unit's own
`StartLimitBurst`/`StartLimitIntervalSec`, counted from the journal (every
start systemd actually performed, including a previous run's tail and
`Restart=always` respawns) and kept one start under the burst — the first two
live runs tripped `start-limit-hit` and left the bubble down minutes, which
is why pacing reads the journal rather than a process-local list. Readiness
is the control socket rather than the doctor (`doctor` answers for a dead
bubble from a LOCAL report, `specs/50-ops.md` §5); a start that does not come
back is recovered with the documented `reset-failed` + `start`; a client-side
doctor report is refused rather than graded; and a run that begins on a
`failed` unit revives it before judging anything. Exit 0 when every automated item passed — a desk tool,
never a CI gate; its guards are `tests/test_desk_retest.py`.

## 7. Environment + deps

Needs: niri, Ollama (`OLLAMA_HOST`, default loopback; remote requires
`allow_remote_ollama` opt-in + guard), MPD+`mpc` (music), `ydotool`
(typing/clicks; socket `$XDG_RUNTIME_DIR/.ydotool_socket` then legacy
`/tmp/.ydotool_socket`), `grim`/`wl-paste`/`wl-copy` (screen/clipboard),
ImageMagick (wallpaper match), `nvidia-smi` (VRAM), D-Bus `dbus-monitor`
(notifications), SearXNG optional localhost:8888. `requirements.txt`:
numpy/PySide6/sounddevice hard; faster-whisper/openwakeword/onnxruntime
lazy; torch/chatterbox owned by install.sh (suite asserts torch never
imported). `requirements-lock.txt` pins CI (numpy 2.5.3, PySide6 6.11.2,
sounddevice 0.5.6, faster-whisper 1.2.1, openwakeword 0.4.0,
onnxruntime 1.29.0, pytest 9.1.1).
