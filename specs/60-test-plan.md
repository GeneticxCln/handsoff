# handsoff — test plan

Sources: `tests/` (37 files + `conftest.py` + `checkout_guard.py` + `fake_ollama.py`),
`install.sh` (what it provisions from),
`pytest.ini`,
`.coveragerc`, `.github/workflows/ci.yml`, `.gitlab-ci.yml`,
`githooks/pre-commit`, `ci/`.

## 1. Inventory

`tests/` — the FILE LIST is the contract: every `tests/test_*.py` must appear
below, and `test_specs_freshness.py` fails when one does not. The counts are the
2026-09-19 snapshot (1899 collected), so a stale count in this table means
nothing — a missing row does.

| File | `test_*` fns | Area |
|---|---|---|
| `test_audio.py` | 200 | Recorder, resample, whisper/TTS seams, levels, playback cancel |
| `test_regression.py` | 162 | cross-cutting pins: prompt text, tool census, marker, restart internals, source-shape properties (loader rollbacks, leased slots, closures that can outlive their loop iteration — stored, threaded, connected, returned or collected; a call in the iteration that made them is left alone, and a callee is FOLLOWED rather than assumed — in the same file by unique name, and ACROSS FILES when it is imported from another module of this checkout, with the checkout root enforced where the file is read so nothing outside it is ever followed, relative imports resolved against their package, a rebound import name dropped, and an ambiguous one not followed — including through a wrapper that only passes the closure ON, with a cycle of such wrappers refused rather than chased — so a helper that calls its closure in place (directly or via another consumer) is not refused while one that stores it, hands it to a worker, keeps it by assignment, takes it as `*fns` or shares an ambiguous name still is — the rule applied to the shipped source, where the two brain workers are pinned BY NAME, AND to this suite, with a planted sample whose verdict travels line by line so a rule that goes blind cannot report a clean tree) — was a rotted 144 against a real 147 |
| `test_desktop.py` | 119 | niri/typing/windows/clipboard/screens, guards |
| `test_policy.py` | 179 | DecisionPolicy, gates, confirm flow, whitelist/blocked, the secret-path guard judging the name asked for AND what it resolves to, and the rate window's read-modify-write under two callers — plus the `run_command` boundary in depth: git/cargo verb and write-flag holes, the pactl and nvidia-smi write vectors, the niri spawn ROUTES (the `spawn-sh` bypass, and the GUI-app allowlist every case run through BOTH routes so the two spellings cannot disagree — 19 payloads, the refusal that names the list and points at `open_app`, no-arguments, the path/identity pair, the whitelisted `spawn` program sharing that one owner, and a structural pin that the allowlist and the deny-list cannot disagree about an app), and the policy's own SHAPE: an AST walk of the four gating functions asserting no refusal is nested under a condition admitting no executable, and that all 32 refusal classes — each named by a `# refusal:` marker at its own site, so inserting a guard above one cannot re-point the table — are reached by a command under `sys.settrace` — the line, not the message — checked in both directions, with a refusal carrying no name and two refusals sharing one both treated as failures, AND that the verdict the caller is handed is that same class's own message (a verbatim fragment, floor 18 chars) — the half a line trace cannot make, since a guard whose helper is called as a bare statement runs every line and returns nothing — and the SAME walk over the read, edit and process boundaries (`read_file`/`denied_secret_path`/`_secret_reason`, `edit_file`/`_classify_edit_path`, `kill_process`/`confirm_kill`: 31 more classes, 63 in all, one corpus per boundary, each boundary also declaring its HOPS so a hop the corpus never executed — or a guard that stopped executing it — is a failure, and now the SSRF guard in `core/web.py` too (`_public_target`, `_read_fetch` and `read_page`, 16 more classes, 79 in all, every entry through `read_page` so the reason the model reads is the one the guard wrote — with its own measured fragment floor, since that module refuses in short noun phrases rather than sentences) and the desk client in `core/qs_desk.py` (40 more: a HANDLE as the subject rather than a module function, a real discovery file judged by mode and shape and bytes, a scripted wire, the verdict taken from the exception's own `__str__` because that is what the tools speak, and two more rules the walk needed — `error_class`, so a returned value cannot read as a refusal, and a raise of the boundary's own builder treated as a hop — plus the two guards it found missing), and that a tool which FAILS is a message rather than a lost turn, under a DECLARED per-tool exception contract (`raises=`: a declared class is the tool's own refusal spoken unprefixed and logged as a warning, a declared class raised with no message fails closed into the bug arm, and anything else is reported as a bug in the tool — the two measured shapes being the whole message `ERROR: ` from a body raising with no message, and a body `TypeError` misreported as "bad arguments" — with the generic builtins refused as declarations and one call site driven with both classes): a raising tool body, the belt's own pre-dispatch check (the measured lone-surrogate payload end to end, at the belt and through the app's loop), a belt that raises with no guard at all, arguments that are not an object (four shapes, since the log line used to sort the raw parsed value and `sorted()` raised on three of them), and a shutdown signal that must NOT be swallowed |
| `test_design_packs.py` | 99 | packs, image design, palettes, ink guard, and the animation bounds a non-finite fps cannot pass |
| `test_bubble_anim.py` | 11 | the tick driven frame by frame: the step, the level chase, colour/energy, the radius spring |
| `test_settings.py` | 85 | loader/coerce/migrate/merge/lock, looks catalogue, health snapshot, the tool-call-times key that is runtime state and never a user list, and a failed write that raises through the dict protocol too |
| `test_settings_contract.py` | 24 | the field table: coercion, controls, cards, companion rows, the window |
| `test_lifecycle.py` | 99 | generations, staleness, PTT epoch, cancel/done, installer membership + rehearsal, restart-script behaviour (each on a fixture it builds) |
| `test_ops.py` | 173 | deploy snapshot, restart, control socket, health/doctor, and what the installer provisions from (the shipped resolvers RUN against fixture settings: the configured whisper size and its refusal of a size the app would not accept, the configured model, the configured server with the app's strict remote opt-in on both channels, the speech repo read out of `core/audio.py`, the app-id read out of `handsoff.py` and substituted into the niri rule, and every `DEFAULT_*` held against the app's own default — plus the rehearsal end-to-end where step 5 says 'small' and the manifest records it, and where the manifest names the deployed git state: `git_commit` is the real HEAD and `git_dirty` is recomputed over exactly the shipped paths, so an uncommitted test/docs edit beside a clean deploy records clean, and the niri window rule being MERGED rather than printed — driven through the real shell functions sliced out of `install.sh`, covering the user's own config surviving, the backup being the pre-handsoff file and never refreshed, idempotence, duplicate blocks collapsing, a legacy hand-pasted block being upgraded rather than doubled, and the three refusals that keep a broken merge from being a WIPE: a strip that loses the file, a rule that cannot be built, and a backup that cannot be taken) |
| `test_web.py` | 75 | router, backends, cache, reader, SSRF refusals, the redirect walk, and the DNS pin (`connect_to` at the reader and at the host's dial, the seam that cannot pin, the bounded resolver) |
| `test_fault_injection.py` | 56 | boundary breakage, loud-degradation contract, a sparse hardware snapshot, and a PROBE that raises reading as one line rather than a dead doctor |
| `test_control_server.py` | 64 | the control socket WITHOUT the app: a stand-in host, the token gate, the unknown-verb listing, the CLI-only reply naming the HOST's file (not `control_server.py`), names read live per request, two servers reading their own dependencies, a failing request costing that request, the accept slot as a singleton, the module importing neither the app nor Qt, and what the mutation gate found unpinned: the socket's `0600` mode, the `listen`/idle-poll constants, what `stop()` does (flag before close, listener closed, removal that raises), a stale path removed before the bind, the no-token and unavailable paths, the repeated start, and the cap-refusal recorders failing without hiding the refusal; then every verb arm's reply (health, doctor and their timeouts, level, clear-history, say keeping its text's case, the pack preview, settings launched detached or missing), how a request is read (the size ceiling, the read budget, the per-connection timeout, the peer-uid refusal), and the accept loop watching its path (a removed path re-bound, someone else's socket reported once and left alone, a path it never bound not repaired, what the loop leaves behind) with `_socket_path_state` and `_rebind` on their own |
| `test_turn_failures.py` | 19 | what the user HEARS when a turn fails, measured at the speaker rather than at the detection: the real `_brain_turn`, the real `_speak`, the real sentence queue and the real worker threads driven to the last hand before playback, with the spoken line pinned verbatim for every failure mode — the guard refusal (no request sent), the refused connection, a missing model, any other HTTP error, a read that dies mid-stream, an exception that is not an `Exception` (which the worker cannot catch, and which the turn reports as an EMPTY answer), a stream that dies part-way, the tool-less 400 that is not a failure, a speech model that cannot load, a dead playback device, and a barge-in — in BOTH arms, because the two said different sentences for the same event until 2026-09-27, and a survey driven only down the default path could not have seen it. Three silences are pinned AS MEASURED (an empty reply, a stream that dies after some sentences, and any speech failure) because what they should do instead is a product decision nobody has made, and a test that asserted a wish would hide the gap rather than name it |
| `test_sandbox.py` | 80 | secret paths, edit boundaries, command validation, and the write guard's own teeth (the refusal, its decision table, the exemptions, the event rules, the CHILD half — a spawned child is refused, the same child with the variable removed is not, and the shim it runs is the pointer rather than a second copy of the rules — and the DEVELOPER'S USER DIRS, the second rule: `~/.config` and `~/.local/state` refused before the write lands, the message naming the directory and the fix, the roots captured before any sandbox (so they do not follow a load's throw-away HOME), the checkout keeping precedence over the home it sits inside, and one variable per rule) |
| `test_calendar.py` | 54 | ICS parse/RRULE/format, scheme + label guards, and COUNT bounding an INSTANCE inside a week, a month and a year (not only the loop that opens them) |
| `test_ci_summary.py` | 48 | `ci/pytest_summary.py` digest, and the compile gate naming a root that does not exist or is not a directory |
| `test_ci_clean_checkout.py` | 14 | the clean-checkout gate: HEAD in a scratch worktree, the guard run THERE (refusing a commit whose plan/key census/citations disagree with a clean checkout), the cleanup before the verdict, the SKIPs, the wiring, and the HARNESS ENVIRONMENT (git's plumbing — the `GIT_INDEX_FILE` and author identity a pre-commit hook is handed — dropped before a child builds its own repository, with a probe hook as the authority on what git really exports) |
| `test_ci_mutation_gate.py` | 58 | the mutation gate: whether the tests a change SHIPPED would notice if the code it touched were wrong. The WIRING (in `ALL_GATES`, reaching `gate_mutation`, ordered before the tree gates with `two-writer` last, and the `usage()` range still ending where its own comment says — and the function RUN, not just read, so a `return 0` at the top of the body cannot launder a FAIL into a PASS), the SKIP/REFUSE contract (a non-repo, a base that is not a commit, a dirty checkout INCLUDING an untracked file, a diff with no Python to break, a tests-only diff, a rename with no mutant site — each asserting the MESSAGE as well as the 2, because a non-repo returns 2 for three independent reasons and no single-guard mutation is visible through the code alone; and a RED BASELINE, which is a REFUSAL and never a caught count), the RULES (return, compare, invert-if, threshold, drop-call, each pinned in both directions — a touched `if` HEADER is a site and a touched `if` BODY is not, which is the `hardware.py:558` defect; non-compiling mutants are discarded; splicing is byte-exact; and the operator table is looked up the way the rule looks it up, which is the `KeyError: <class 'type'>` crash), the SELECTION (changed tests are never dropped, the same-named test file is the holder, because a test that holds a module often loads it by PATH and never imports it, a file matching the module's name in a COMMENT is named but never run), the TWO VERDICTS (a failing run is `caught` in phase one and `HELD-ELSEWHERE` in phase two — they are separate functions because merging them reported five held-elsewhere mutants and then printed PASS), and seven end-to-end runs of the real gate in scratch repositories: a pinned rule passes, a decorative test fails, a held-elsewhere is its own finding, a red tree is refused, the checkout is never rewritten, the worktree is restored and leaves no admin record under `.git/worktrees/`, and a timeout is neither caught nor survived — and seven IN-PROCESS runs of `main()` as well, because a subprocess is a different process and coverage cannot see one: the measured consequence of omitting them was `ci/mutation_gate.py` at 68% and the project total down from 85.09% to 84.78%, 121 statements of a gate none of them seen. They cover the JSON report (every verdict in `VERDICTS`, every mutant with a note), the cap as a BUDGET (`available` and `run` are two numbers and only the second is a result), a cap of zero being a SKIP rather than a pass, the `--tests` escape hatch, and two runs of one commit picking the same mutants |
| `test_ci_two_writer.py` | 102 | the two-writer gate: the worktree stamp's movement (and the gate artifacts that must not move it — plus the paths that LEAVE a list, the shape a naive rewrite of "what moved" drops), the per-gate checkpoints and the gate they name, the baseline that moves only on an explicit rebase (and then says which tree it is), the CLI refusal, the WHEN lines (the clock each snapshot carries and the verdict ignores, the offset/fraction/band arithmetic placed by hand with `os.utime` against a window the test picked — never a sleep —, each band boundary sampled a point on either side of itself, the three shapes a write can have relative to its window, the two moves with no file clock to ask, the cap that counts what it does not print, and `at`/`--since` gone or stale degrading to a coarser answer instead of a wrong one), the WRITE THAT WAS PUT BACK (a file whose content every hash agrees about while its own write time moved — the shape that used to read as a still tree: a clean tracked file is watched, not only a dirty one, a read is not a write, a real content change is reported once and never also as restored, one nanosecond of difference is enough, ignored artifacts are outside the map, a snapshot from before the write times existed still judges, and the write time is placed in the window), the COMPARISON of a gate's attempts with its re-run (the shipped function on a fabricated record of attempts: a re-run that agreed, one that flipped — the move changing the outcome —, a flip a later re-run undid (`PASS → FAIL → PASS`), repeats not printed as repeated answers, a gate that ran once never compared, and a gate compared only with itself and not with the neighbours its attempts are interleaved with), the wiring in `ci/gates.sh`, eight end-to-end runs of the real script on a scratch repo whose interpreter edits, breaks or WRITES-AND-RESTORES a tracked file on chosen CALLS — a collision that resumes, one with nothing overtaken so no pass runs, one whose overtaken verdict is caught up (so the run ends as a verdict about one tree), one where a write inside the catch-up leaves the split, one where a pre-existing red skips the catch-up, one where a re-run flips a kept verdict and the run prints the flip, one where the write is put back inside a gate (a collision and a re-run with no content difference anywhere), one where it is put back after the LAST checkpoint (the refusal, placed in the tail of the run), and a window holding two writes that stops the run — a source guard that no guard in the file pins a gate's elapsed seconds (a summary row is a SHAPE, and a guard pinned to `0s` reported a correct gate red on a loaded full-suite run before it was fixed), and THE STAMP REFUSING TO GUESS (a tree that HAS a HEAD whose `git diff`/`ls-files` FAILED is spelled unjudgeable instead of read as an empty diff, a truncated snapshot on either side is not "nothing moved", and a clock that went backwards prints no timing line rather than a negative one) |
| `test_desk_retest.py` | 32 | `ci/desk_retest.py`, the ONE command that stages §10's desk retest: the journal reader (`swept N … into DEST`, singular and plural, malformed lines skipped) and the state-line reader (the scratch count rides clause 0 — a `--simulate` run found a reader starting at the second and accusing a truthful line; and a mature `7-day trend … entries` clause must be read before the size clause that also ends in `entries`), the `wake:` whitelist, the paste-ready block (marks, reasons, and the OWED line naming A1–A4 exactly), `--only`/`--dry-run` and the litter a rehearsal may leave, and one end-to-end `--simulate` run whose desk-only checks report `not run here`; the desk-only WIRING pinned by shape because it cannot run in the suite (B1 speaks before it kills, and its child calls what `main()` calls in the same order); and the REAL back end's own constraints, every one of which the first live run found the hard way (the pacer reads the unit's `StartLimitBurst`/`StartLimitIntervalSec` and holds rather than tripping it, readiness is the control socket and never the doctor, the documented `reset-failed` + `start` recovery, and a client-side doctor report refused instead of graded) — plus the run a dead session interrupted, which tripped the limit AGAIN despite the pacer: the in-process start list cannot see the previous run's tail or a `Restart=always` respawn, so the pacer now counts the JOURNAL's starts and keeps one start of reserve under the burst, revives a unit it finds down (`reset-failed` + `start`, held to the budget) before grading anything, and ends the start wait the moment the limiter refuses — plus WHAT THE RUN DELETES (the cleanup boundary: its own `tmpdeskcheck*` probes, state and archive, and nothing else — the first live session's exit deleted B1's archived REAL reply, the exact bytes B2's `aplay` exists to play) |
| `test_laya_corpus.py` | 19 | the corpus a fine-tune learns from: the family table read from the harness rather than restated (proven by moving it), authored labels held to real families, the whole belt labelled, the growth store (a row added once, `first_seen` pinned while `count`/`last_seen` move, a disagreement reported as a CONFLICT instead of rewritten, an unknown label refused, a garbage store reading as empty), and the checkout rule (the default store is outside the tree, a dump inside it is refused, the hash covers the labelled set so a relabelled row is a different corpus) — plus the TURN QUEUE the app fills: one label rule for both ways a row is mined (with the reason a row is refused), the fold that happens on every read rather than behind `--grow`, idempotence proved through the cursor (so a re-seen row means the sentence was really said twice), a hand-replaced queue REFOLDED rather than skipped forever — the fingerprint that makes a line count verifiable — an unusable turn counted under the harness's own reason instead of dropped, the queue/store/cursor all reading as 0600, and the default paths derived from the app's own `$XDG_STATE_HOME` |
| `test_self_watch.py` | 17 | the sampler that watches the watchers: a thread seen then gone is dead (a first sighting only LEARNS the baseline, so startup order never alarms), a frozen probe is wedged after the threshold and a moving one never is, a raising probe degrades to liveness-only without killing the tick, cooldown lets a standing finding re-speak without spamming, recovery silences it, the dead and wedged sentences differ and stay speakable, the never-raises contract of tick/snapshot/announcements under sabotage, nameless threads tolerated, and the app's half — the health section reads on a `__new__` host and the shutdown Event is the loop's only stop signal |
| `test_laya_turns.py` | 10 | the app's half of the corpus: a completed turn recorded with its tools (deduped, capped, 0600, a chat turn recorded with none), the reading taken from the turn's own messages, a failed write never breaking a turn, `--ptt health`'s three counts (recorded, pending, grown) including the key in the snapshot on a host with no notification reader, the tail of `_brain_turn` writing the record BEFORE history is sealed (so a call whose reply never came — the confirmation-offer break — is still recorded as the choice, while the published history holds no unanswered call), and the two halves agreeing end to end with no command in it |
| `test_hardware.py` | 42 | snapshot sections, TTLs, probers, and a whisper directory holding only empty files reading as NOT cached |
| `test_theme.py` | 35 | hex parse, luminance, retune, and the `#` a wallpaper sampler prints |
| `test_hardening.py` | 34 | perms, symlink/0600, token, caps, the no-core fallbacks (audio, brain) |
| `test_registry.py` | 30 | BoundedRegistry admission, Offer arm/consume |
| `test_assistant.py` | 32 | pomodoro/notifications/reminders/watcher ticks, reader health |
| `test_world_events.py` | 24 | fixed queries, severity, seen-store, cooldowns |
| `test_audit_fixes_tools.py` | 2026-09-28 tools.py audit pins: cargo CONFIRM floor, denylist additions (DB/CLI secret files, Flatpak browser profiles, the app's own settings.json), terminal markers, CONFIRM offer names the armed payload, read_file special-file/bounded reads, decision-log refusals, dry_run state-changers, confirm_kill pid re-check, watcher O_NOFOLLOW, open_app blocklist, Super-chord fail-closed, screenshot 0600 |
| `test_audit_fixes_webbrain.py` | 2026-09-28 web/brain/voice audit pins: streaming think-filter across sentence fragments |
| `test_audit_fixes_settings.py` | 2026-09-28 settings audit pins: unknown keys and version survive a full save, fsync'd atomic writes, env-map parity, appearance-only live apply, sticky-key honesty, float spins, corrupt-history labelling, backup 0600 |
| `test_audit_fixes_host.py` | 2026-09-28 app-module audit pins: laya-turns store bounded and 0600, clear-history epoch vs an in-flight publish, deploy floor covers install.sh's set |
| `test_audit_fixes_media.py` | 2026-09-28 media audit pins: ICS quoted/Mozilla TZIDs, zone-correct RRULE expansion, monthly/yearly window jump, lowercase producers, qs_desk read cap, play_wav output watchdog, bounded mic-lock waits |
| `test_notify_coalesce.py` | 15 | notification batching/cooldown |
| `test_hardware_watch.py` | 20 | hardware watch tick, disk/VRAM alerts, and the mic-dead rule (two shapes, once per episode, the way out named) — including the tick's OWN probes held out of it (`shutil.which` stubbed, so no test reads the host's real card) and the alert PRIORITY that came out of that: a dead mic outranks a VRAM notice, and a notice that loses the floor stays armed instead of latching as if it had been said |
| `test_idle_release.py` | 149 | the idle release: the drop and its locks, the quiet-window policy, re-arm vs one-per-spell — and the card's ONE story (every tenant named and adding up to the used bytes, the speech models per model and device, the LLM's residency/split/blob, what the next turn asks for, the release sentence, and the readings the two surfaces share) |
| `test_brain.py` | 85 | `core/brain.py`, the module every spoken sentence comes out of, driven against an injected `urlopen`: the streaming contract as eight failure modes in one table (exactly one terminator on every one, and last), the splitter (nothing lost, spoken twice, or spoken before its full stop arrives — a real thread holding the stream between two lines, with the byte cut at ARBITRARY boundaries), the barge-in (the tail swallowed, what already played kept), the tool-less model (retried once without tools, the refusal remembered rather than re-paid every turn, and the retry carrying its own guard/cancel/keep_alive — a counting stand-in is the only way to see a dropped cancel), the two failure sentences, the request on the wire, the system-first fold as a property over eleven caller-built lists, the readiness probe's five unanswerable shapes, and the readers' defences. Four of its tests exist because writing it found defects |
| `test_rule_copies.py` | 30 | no rule in two places that can disagree: the four grades a duplicated rule can take (MIRROR/SEAM/COPY/DISTINCT), each derived from the tree rather than listed, each with the check it can be held by — a mirror must name the core rule it mirrors (searching the body and NOT the `def` line, which is what made the first version of that check decoration), a seam must name the rule it re-exposes, a copy's two bodies must still be the same code (a hash, so divergence is the failure and no corpus is needed), a DISTINCT entry's bodies must really differ and its reason must be a sentence, plus the compatibility-branch census (every definition inside an `except ImportError`, each naming the test that holds it, keyed by the whole dotted name so a class entry cannot swallow a method added later) and the site check that stops a third copy slipping in under an entry written for two |, the MIRROR DERIVATION widened to the `_fallback_` convention (`FALLBACK_PREFIX`) because all five module-level `_fallback_*` copies were graded NOTHING — not mirror, not seam, not branch — so the family that produced four of this repository's defects was invisible to the census written to find that shape, and a sixth copy would have been invisible too; the mirror count is 24 rather than 19 with no manifest added, a core rule may be PRIVATE (`_fallback_messages_system_first` mirrors `_messages_system_first`) and the `_` variant belongs to that convention alone, since applying it to every name re-graded the `_http_get` COPY and failed a rule the shared-name census already holds. Widening it found two copies nobody had named — `_fallback_is_leaked_markup` and `_fallback_strip_thinking`, one liners with nothing saying they were copies
| `test_precommit_staged.py` | 12 | the pre-commit hook judging the STAGED tree: the index written out once, every file check reading it, the spec guard AND the full suite run inside it (skipped when a cheap leg already failed), a refusal instead of a fall back to the working tree, the cleanup on every path, and the four end-to-end shapes (a partial stage refused, a partially staged BEHAVIOUR refused, both halves committable, the working tree NOT deciding) |
| `test_p0_fixes.py` | 6 | named P0 regressions |
| `test_specs_freshness.py` | 23 | the specs' counts and cells vs the code, the generated tables, and the file/module/spec lists — plus the ONE reader shared with conftest's development-time census warning, which prints the moment a whole-suite session's collection moves README's count (gated to the claim's scope: a partial run collects 85 and would warn 85-vs-2300, found live — it first misfired on `--lf`, which rewrites collection down to the last-failed set and so collected 3 of them, and `--deselect` narrows the same way; `--ff` only reorders, so it still speaks for the suite. The summary echoes the stored verdict rather than recomputing) |
| `test_settings_gui.py` | 1 (+ offscreen subprocess drivers) | Qt GUI incl. 10-design ink guard |
| `test_qs_desk.py` | 77 | the Quantum Space desk client against a REAL `http.server` on an ephemeral port (never a recorded double): the wire (POST to `/`, bearer, no Origin, the lines clamp, and the token re-read on EVERY request — the file is rewritten between two calls and the second must carry the new one), the file that is not ours to trust (stale pid, mode 0644, non-JSON, protocol 2, a live foreign pid — each refused with NO request made), the desk's own sentences relayed verbatim (including its two different `not-granted` ones), the four failure states kept as four sentences, and the family's one gate (off ⇒ nothing reaches the desk, DENY policy wins). The fake answers in the shapes a LIVE desk was measured answering on 2026-09-21 — `control` as `{enabled, clients}` rather than a bare bool, `desk` in `hello` as the whole status object, `lines` on every read, an EMPTY body on 401/403/405/400, session rows whose `kind` is the tile's own (`claude`, `agent` null for a shell), and ids in the app's window-prefixed panel form (`w1_p_1`) — because a fake that is tidier than the desk tests the wrong desk (see `specs/90-audit.md`, "The desk client's first live turn"). The one branch coverage found untested: two tiles that answer to the SAME name, where the user said exactly what the desk calls it and is still refused rather than resolved to the first — the walk's corpus reaches the half-match site only, because both `raise _ambiguous` sites are hops and a corpus keyed by class name has one entry for them. Two more guards come from the second live turn: a session NAME is bounded to 40 characters before it is spoken (the desk names a `run` tile with its whole 171-character command line), and a read with no line breaks is called out as ONE line — while a real line-oriented stream gets no such note, so the note keeps meaning something. Five more (73 now) come from the third live round, where the client's OWN constructor was the defect: `Desk("handsoff")` — a client name where a path belongs — used to become one relative path and answer "Quantum Space isn't running." about a desk with a 0600 file on disk and a live pid in it, so a path that cannot be one is refused (relative path, empty list, non-path), an omitted list searches the profile directories, and a single absolute path is still a path list. The walk over the same client found two more ways of getting the same thing wrong, and both are pinned BY SENTENCE rather than by state name: a discovery file that is not UTF-8 text is refused as `untrusted` ("is not UTF-8 text", never "not valid JSON" — it is not a JSON file at all) and is never dialled, and the sentence is asserted through the TOOL as well, because `read_text`'s `UnicodeDecodeError` is a `ValueError` and so used to pass every `except DeskError` in the four `quant_space_*` tools (six handlers) and reach the user as the tool loop's own "the assistant's own check failed before the tool ran"; and one non-path inside a list is refused by name the way the whole list is, rather than with Python's `argument should be a str or an os.PathLike…`, while the desk that IS running stays reachable by the right call |
| `conftest.py` / `fake_ollama.py` | 0 | module loader (`handsoff_core` + alias), order-shuffle, the user-dir sandbox, the checkout-write audit hook installed for this process, the `sitecustomize` shim that carries it into every python child the suite spawns, fake brain |
| `checkout_guard.py` | 0 | the write property's ONE home: the audit hook, the event table and its per-event rules, the two rules it applies (the checkout, and the developer's real user dirs — the checkout first, because it has exemptions), the exemptions, and the `install()` that both this process and every child call |

## 2. CI gates (GitHub authoritative, GitLab mirrors)

- `tests` (py 3.12 + 3.13 + 3.14, `QT_QPA_PLATFORM=offscreen`): full suite. The
  third leg is the one `install.sh` actually lands on — it takes whatever
  `python3` is on PATH, and on Arch that is 3.14 — so a matrix without it
  graded an interpreter nobody ships.
- `coverage`: `--cov=. --cov-fail-under=70` (+ `COVERAGE_PROCESS_START` for
  offscreen-subprocess drivers, parallel combine).
- `order`: whole suite reshuffled by commit SHA, then file-order shuffled —
  both must pass (ordering-dependence probe).
- `compile`: `ci/compile_all.py` — DISCOVERED `*.py` (attic + hidden
  excluded), `py_compile` without imports; empty discovery fails the gate.
- `lint`: ruff over the production sources with the rule set pinned in
  `ruff.toml` (pyflakes + bugbear + pylint-errors; the test tree is
  deliberately out of scope, and each excluded family is named there with its
  measured finding count). Added because a tree this size with no linter makes
  an unused import, a shadowed name and a `zip()` that silently drops a row all
  look like style — and all three were present. Locally it SKIPs with an
  install hint when ruff is absent, because a gate that passes because it never
  ran is the failure this script exists to avoid.
- `links`: `ci/link_check.py` — every markdown anchor link in the docs resolves
  to a real heading (ATX and setext, numbered by the same GitHub slug the
  ledgers' index uses, plus explicit `<a id>`; fenced code is not scanned on
  either side). A heading link is the one link that fails SILENTLY — rename a
  heading and markdown does not complain — and 137 of the 138 anchor links in
  this repository are the two ledgers' generated indexes, so this is what
  notices a rename stranding a quarter of the navigation. Stdlib only: no
  install, no network, nothing to be missing, so the gate never SKIPs.
- `shell`: actions SHA-pinned (mutable `@vN` fails); `bash -n` over every
  file with a bash/sh shebang (finds extensionless `handsoff-restart`).
- `smoke`: `bash install.sh --help` contains `Options:` + `--uninstall`.
- `clean-checkout`: HEAD is laid down in a scratch `git worktree` (`mktemp -d`,
  outside the repo) and `tests/test_specs_freshness.py` is run THERE. That guard
  reads the test plan, the key census and the spec citations out of FILES, so a
  commit inconsistent with its own plan passes in the checkout that happens to
  hold the uncommitted files and fails in a clone — this gate is the clone.
  `worktree add --detach --quiet "$tree" HEAD`, then the guard's own output (its
  tail, so the failing assertion is the finding) plus a REFUSED block that says
  why this checkout hid it and the one-liner to reproduce. A repo that is not a
  worktree, an unborn HEAD, a `worktree add` that fails, or a HEAD that carries
  no such guard is a SKIP with its reason — never a pass. The worktree is removed
  AND pruned before any verdict is printed, because a refusal that damages the
  checkout it reports about is worth nothing. It is registered before
  `two-writer`, which stays last: that gate compares the worktree, and a scratch
  write after it would invalidate the comparison.
- `mutation`: `ci/mutation_gate.py` asks whether the tests a change SHIPPED
  would notice if the code that change touched were wrong — the question a green
  suite cannot answer, because a green suite and a test that asserts nothing
  are the same observation from the outside. It is the only gate that breaks
  production code on purpose, and it does it in a detached `git worktree` of HEAD
  in a scratch directory, never in the checkout the gate is run from, restoring
  every file byte-identically and removing the worktree on every path. Its own
  BASELINE must be green or it REFUSES (1) rather than scoring every mutant
  "caught" against a red suite — the failure that made this project's first
  mutation run report 11 of 11 while every run failed for want of pytest. It
  SKIPs (2) on anything it cannot measure: no base commit, no Python in the
  diff, no mutant site in the touched lines, a commit that touches production
  and tests separately, or a checkout with uncommitted changes (untracked files
  included — `git diff` does not see them and the file selection globs the
  working tree). A mutant that does not parse is discarded, not scored; a run
  that timed out is `unknown`, counted as neither caught nor survived. Three
  verdicts, because one word was doing two jobs: `caught`, `SURVIVED` (nothing
  in the project notices) and `HELD-ELSEWHERE` (an older test noticed and the
  new one did not, so the new test is decorative) — the last two fail, the
  first is the gate working. The wider set runs lazily, once, and only when a
  survivor exists. What it leaves out it prints. Run over this repository's own
  last five commits it reports two passes, three skips and no false red.
- `two-writer`: `ci/worktree_stamp.py` fingerprints the worktree before the
  first gate (HEAD, one content hash per tracked change, every untracked
  non-ignored file, and ONE WRITE TIME PER PATH — tracked clean or dirty, and
  untracked) and every gate is CHECKPOINTED after it runs. The write times are
  what catch a write that was REVERTED inside a window: the content matches the
  checkpoint's exactly, so no hash can see it, and the file was still written
  under the suite's feet — reported as `written during the window and restored:
  app.py — the content is what the checkpoint read, and only the file's own write
  time moved`, and placed by the same clock. Ignored paths are outside the map
  (the gates rewrite `.coverage` and `tests/report*.xml` every run), and a
  snapshot from before the write times existed still judges content, only without
  the restored rows. A collision is
  not a refusal any more: the collided gate's attempt is reported VOID (its
  window held the write, so its result is about a mixture), the baseline is moved
  onto the state the collision left (`--rebase`, annotated with what it now is),
  and that gate is re-run against it — so every verdict from the collision onward
  is about the tree as it now stands, and the ones before it are printed as
  CARRIED OVER ("they describe the tree BEFORE the move"). Those overtaken
  verdicts are then RE-RUN in order before the tree verdict (`catch-up`), so a
  green run ends as a verdict about ONE tree; the row each re-run supersedes is
  marked, and the split is computed per LEG rather than per collision (a write
  inside the catch-up is reported as leaving the split, not chased). The catch-up
  is skipped when the run already has a red — that is a verdict, and the failure
  digest is built for one tree's red — and runs once, since a second write makes
  the gates it re-ran stale again. A gate that ran twice is also held up against
  itself: each attempt's own RESULT is recorded (a discarded one included — it is
  not a verdict, but it is half of the comparison) and the resumed section prints
  either `the move changed the outcome: compile PASS → FAIL` or, when every
  re-run answered as its earlier attempt did, one line saying so — because two
  rows in a summary can be diffed by eye and the results of one gate across two
  trees cannot. A tree that moves twice inside one gate's
  window stops the run, and a write after the LAST checkpoint — no gate left to
  re-run — refuses it. Each snapshot also carries the CLOCK it was taken at, so a
  checkpoint can say roughly WHERE INSIDE the gate the write landed: each changed
  path's own `mtime` (or the new HEAD's commit time) is placed in the window the
  two checkpoints bracket, as `  - when: app.py was last written 96s into the
  tests gate's window (231s long), about 42% through — the middle of it, at
  14:03:47`, with the resumed section stating that it is a placement rather than
  something the gate watched. The tree verdict passes `--since` (the newest
  checkpoint) so the one write it catches — the write no gate saw — is placed in
  the tail of the run instead of "4 800s into the run", while its VERDICT stays
  against the baseline. Artifacts the gates
  themselves write are ignored through `.gitignore`, which is what keeps the
  gate from refusing its own run; a directory that is not a git worktree gets no
  opinion (SKIP), never a clean bill. The checkpoints live in a temp dir and are
  removed on both exits.
- `pytest.ini`: `PytestUnhandledThreadExceptionWarning` is an ERROR —
  a worker thread raising fails the build.
- pre-commit (`githooks/`, opt-in via `core.hooksPath`): the index is written
  out once (`git checkout-index -a` into a scratch directory) and every check
  reads THAT — staged `py_compile`, staged shebang `bash -n`, the spec guard in
  the staged tree, and the FULL suite in the staged tree too (minutes — no
  duration is copied here, because a copied one rots), so a partially staged
  behaviour change is refused rather than judged by the checkout that holds both
  halves. The suite leg is skipped when a cheaper leg already failed, since the
  suite contains the spec guard. Nothing skips in a file-only copy: the two
  checks that used to (an untracked scratch in a shipped directory, the
  installer's no-git fallback) now bring the repository and the checkout they
  need, so the staged tree and the developer's tree run the same suite — same
  tests, same verdict. Bypass only via `--no-verify`, deliberately.
- GitLab: the same gates, on the DESK RUNNER rather than on a GitLab-hosted
  (instance) runner — a project runner on the developer's own machine (tag
  `desk`), where a job is not billed against the namespace's 400-a-month
  compute-minutes quota. The quota is why: it is one pool for every project
  under the account, this repo's five suite jobs spent it on 13 September, and
  the 364 jobs that followed died with `ci_quota_exceeded` before running a
  single test — so a merge request's pipeline said nothing about the merge
  request. Two things an image would have provided are CHECKED instead of
  assumed: the CPython each job stands in for (`ci/desk_python.sh`, a uv venv
  per matrix version, refusing an interpreter that answers with another
  version) and the runtime libraries (`ci/apt_deps.sh`, which installs on
  Debian and verifies the same two tests — the files by name, and the
  `ctypes.util.find_library('portaudio')` lookup sounddevice performs at import
  — wherever apt does not exist). `suite:hosted` keeps the digest-pinned image
  reachable as a MANUAL, `allow_failure` reference — defined ONLY in pipelines
  started from the web UI or the API, because GitLab refuses every job of a
  pipeline at creation once the minutes are spent, manual jobs included (job
  16625849832: created and finished 7 ms apart, never queued), so a reference
  left in every push pipeline is a red mark that says nothing about the commit.
  The frozen-image leg therefore runs when the question is whether the desk and
  the image agree (or when the desk is offline) and never as a gate.
- Two couplings the desk made visible, fixed where they live rather than hidden
  by serialising around them. On a hosted runner every job owns a container, so
  a test may assume it is alone on the machine; on the desk it is not, and the
  first desk pipeline carrying a change failed BOTH suite legs on
  `test_precommit_staged`'s scratch-tree check, each reporting the other job's
  in-flight `/tmp/handsoff-staged-*` tree as its own leak — its verdict was
  about whoever else was running, which is also true of a developer committing
  while the suite runs. That test now hands the hook a TMPDIR of its own (the
  hook's `mktemp -d -t` reads it) and proves the child honours it before
  asserting nothing was left, so the check can no longer pass vacuously; and
  `TestNoTestJudgesTheSharedTempNamespace` in `test_sandbox.py` fails any test
  that ENUMERATES the machine's temp root (`glob`/`rglob`/`iterdir`/`scandir`/
  `listdir`/`walk` over `tempfile.gettempdir()`), with the shipped line as its
  planted sample. The second collision was a wall clock: the offscreen
  live-probe scenario polled for up to five seconds for the whisper worker, so
  under load the answer arrived after the deadline and a correct strip of code
  failed as `transcribed` — it now JOINS the worker (30 s bound) and asserts the
  state, which is synchronisation rather than patience. The suite jobs also hold
  a `resource_group` (`handsoff-suite`) because the suite genuinely owns real
  devices — GPU speech models, offscreen Qt, the audio server — for its duration. Plus pip cache on the lock file, junit report,
  and the `after_script` digest (`ci/pytest_summary.py --post`) that prints
  failing names + env-fix hints even when the job dies.

## 3. Conventions that keep the suite honest

- Injections happen at the BOUNDARY (HTTP opener, recorder return, popen
  factory, write path, handed file object) — never by replacing the code
  under test (`README.md:51`).
- Faults assert the LOUD contract: journal WARNING/ERROR findable in
  `journalctl` + spoken line naming the cause + reported failure + toggle
  stops claiming on; AND the silence of the alternative: no fabricated
  success, no swallowed error, no value reported saved when it is not.
- Ordering: `HANDSOFF_TEST_ORDER_SEED` / `HANDSOFF_TEST_ORDER_FILES`
  reproduce any CI shuffle locally.
- Offscreen Qt: real bubble + settings GUI driven headless; ten-design ink
  guard pins every clamp (remove one → red); pixel-OCR avoided where
  multi-output screenshots proved unreliable (ACCEPTANCE §1: verified at the
  data layer through `_fmt_health` instead).
- Suite pins what humans forget: system-prompt text, 52-tool census,
  `SELF_MARKER`, restart-script internals, `BUBBLE_DESIGNS`↔schema agreement,
  looks catalogue vs loader bounds, `_DEPLOY_FILES` floor vs manifest.

## 4. Coverage floor (`.coveragerc`, TOTAL ≥70)

Omit: `tests/*`, `attic/*`, root `test.py` scratch, `site-packages`,
`shibokensupport`/`signature_bootstrap` phantoms. The per-file shape is not
restated here: this section used to list a percentage per file, and every one
of them had drifted by the time it was read. `coverage report` prints the
shape as it is today; the gate enforces the floor on TOTAL. The module that
needs tests most is the one only the subprocess GUI drivers exercise, which is
what the driver scenarios in §3 exist for. Raise with tests, never with
omit-patterns.

## 5. What tests cannot cover (desk truth in ACCEPTANCE.md)

 Audible TTS + echo rejection, live PTT/wake round trips, Yeti unplug-heal,
 barge-in timing, wallpaper-match aesthetics, shutdown-with-session,
 crash-loop systemd states. Each is a ☐/✅ line with `[auto]`/`[human]`
 codes and verified-by dates — the suite and the checklist are complements,
 not substitutes. GPU-less CI additionally cannot cover CUDA paths; the
 suite pins `torch` unimported instead.
