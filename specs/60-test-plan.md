# handsoff — test plan

Sources: `tests/` (30 files + `conftest.py` + `checkout_guard.py` + `fake_ollama.py`),
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
| `test_regression.py` | 144 | cross-cutting pins: prompt text, tool census, marker, restart internals |
| `test_desktop.py` | 119 | niri/typing/windows/clipboard/screens, guards |
| `test_policy.py` | 100 | DecisionPolicy, gates, confirm flow, whitelist/blocked |
| `test_design_packs.py` | 98 | packs, image design, palettes, ink guard |
| `test_bubble_anim.py` | 11 | the tick driven frame by frame: the step, the level chase, colour/energy, the radius spring |
| `test_settings.py` | 82 | loader/coerce/migrate/merge/lock, looks catalogue |
| `test_settings_contract.py` | 24 | the field table: coercion, controls, cards, companion rows, the window |
| `test_lifecycle.py` | 99 | generations, staleness, PTT epoch, cancel/done, installer membership + rehearsal, restart-script behaviour (each on a fixture it builds) |
| `test_ops.py` | 76 | deploy snapshot, restart, control socket, health/doctor, and what the installer provisions from (the shipped resolvers RUN against fixture settings: the configured whisper size and its refusal of a size the app would not accept, the configured model, the configured server with the app's strict remote opt-in on both channels, the speech repo read out of `core/audio.py`, the app-id read out of `handsoff.py` and substituted into the niri rule, and every `DEFAULT_*` held against the app's own default — plus the rehearsal end-to-end where step 5 says 'small' and the manifest records it) |
| `test_web.py` | 75 | router, backends, cache, reader, SSRF refusals, the redirect walk, and the DNS pin (`connect_to` at the reader and at the host's dial, the seam that cannot pin, the bounded resolver) |
| `test_fault_injection.py` | 55 | boundary breakage, loud-degradation contract, a sparse hardware snapshot |
| `test_sandbox.py` | 79 | secret paths, edit boundaries, command validation, and the write guard's own teeth (the refusal, its decision table, the exemptions, the event rules, the CHILD half — a spawned child is refused, the same child with the variable removed is not, and the shim it runs is the pointer rather than a second copy of the rules — and the DEVELOPER'S USER DIRS, the second rule: `~/.config` and `~/.local/state` refused before the write lands, the message naming the directory and the fix, the roots captured before any sandbox (so they do not follow a load's throw-away HOME), the checkout keeping precedence over the home it sits inside, and one variable per rule) |
| `test_calendar.py` | 45 | ICS parse/RRULE/format, scheme + label guards |
| `test_ci_summary.py` | 42 | `ci/pytest_summary.py` digest |
| `test_ci_clean_checkout.py` | 14 | the clean-checkout gate: HEAD in a scratch worktree, the guard run THERE (refusing a commit whose plan/key census/citations disagree with a clean checkout), the cleanup before the verdict, the SKIPs, the wiring, and the HARNESS ENVIRONMENT (git's plumbing — the `GIT_INDEX_FILE` and author identity a pre-commit hook is handed — dropped before a child builds its own repository, with a probe hook as the authority on what git really exports) |
| `test_ci_two_writer.py` | 102 | the two-writer gate: the worktree stamp's movement (and the gate artifacts that must not move it — plus the paths that LEAVE a list, the shape a naive rewrite of "what moved" drops), the per-gate checkpoints and the gate they name, the baseline that moves only on an explicit rebase (and then says which tree it is), the CLI refusal, the WHEN lines (the clock each snapshot carries and the verdict ignores, the offset/fraction/band arithmetic placed by hand with `os.utime` against a window the test picked — never a sleep —, each band boundary sampled a point on either side of itself, the three shapes a write can have relative to its window, the two moves with no file clock to ask, the cap that counts what it does not print, and `at`/`--since` gone or stale degrading to a coarser answer instead of a wrong one), the WRITE THAT WAS PUT BACK (a file whose content every hash agrees about while its own write time moved — the shape that used to read as a still tree: a clean tracked file is watched, not only a dirty one, a read is not a write, a real content change is reported once and never also as restored, one nanosecond of difference is enough, ignored artifacts are outside the map, a snapshot from before the write times existed still judges, and the write time is placed in the window), the COMPARISON of a gate's attempts with its re-run (the shipped function on a fabricated record of attempts: a re-run that agreed, one that flipped — the move changing the outcome —, a flip a later re-run undid (`PASS → FAIL → PASS`), repeats not printed as repeated answers, a gate that ran once never compared, and a gate compared only with itself and not with the neighbours its attempts are interleaved with), the wiring in `ci/gates.sh`, eight end-to-end runs of the real script on a scratch repo whose interpreter edits, breaks or WRITES-AND-RESTORES a tracked file on chosen CALLS — a collision that resumes, one with nothing overtaken so no pass runs, one whose overtaken verdict is caught up (so the run ends as a verdict about one tree), one where a write inside the catch-up leaves the split, one where a pre-existing red skips the catch-up, one where a re-run flips a kept verdict and the run prints the flip, one where the write is put back inside a gate (a collision and a re-run with no content difference anywhere), one where it is put back after the LAST checkpoint (the refusal, placed in the tail of the run), and a window holding two writes that stops the run — and a source guard that no guard in the file pins a gate's elapsed seconds (a summary row is a SHAPE, and a guard pinned to `0s` reported a correct gate red on a loaded full-suite run before it was fixed) |
| `test_hardware.py` | 40 | snapshot sections, TTLs, probers |
| `test_theme.py` | 34 | hex parse, luminance, retune |
| `test_hardening.py` | 34 | perms, symlink/0600, token, caps, the no-core fallbacks (audio, brain) |
| `test_registry.py` | 30 | BoundedRegistry admission, Offer arm/consume |
| `test_assistant.py` | 26 | pomodoro/notifications/reminders/watcher ticks |
| `test_world_events.py` | 24 | fixed queries, severity, seen-store, cooldowns |
| `test_notify_coalesce.py` | 15 | notification batching/cooldown |
| `test_hardware_watch.py` | 13 | hardware watch tick, disk/VRAM alerts |
| `test_idle_release.py` | 149 | the idle release: the drop and its locks, the quiet-window policy, re-arm vs one-per-spell — and the card's ONE story (every tenant named and adding up to the used bytes, the speech models per model and device, the LLM's residency/split/blob, what the next turn asks for, the release sentence, and the readings the two surfaces share) |
| `test_precommit_staged.py` | 12 | the pre-commit hook judging the STAGED tree: the index written out once, every file check reading it, the spec guard AND the full suite run inside it (skipped when a cheap leg already failed), a refusal instead of a fall back to the working tree, the cleanup on every path, and the four end-to-end shapes (a partial stage refused, a partially staged BEHAVIOUR refused, both halves committable, the working tree NOT deciding) |
| `test_p0_fixes.py` | 6 | named P0 regressions |
| `test_specs_freshness.py` | 19 | the specs' counts and cells vs the code, the generated tables, and the file/module/spec lists |
| `test_settings_gui.py` | 1 (+ offscreen subprocess drivers) | Qt GUI incl. 10-design ink guard |
| `conftest.py` / `fake_ollama.py` | 0 | module loader (`handsoff_core` + alias), order-shuffle, the user-dir sandbox, the checkout-write audit hook installed for this process, the `sitecustomize` shim that carries it into every python child the suite spawns, fake brain |
| `checkout_guard.py` | 0 | the write property's ONE home: the audit hook, the event table and its per-event rules, the two rules it applies (the checkout, and the developer's real user dirs — the checkout first, because it has exemptions), the exemptions, and the `install()` that both this process and every child call |

## 2. CI gates (GitHub authoritative, GitLab mirrors)

- `tests` (py 3.12 + 3.13, `QT_QPA_PLATFORM=offscreen`): full suite.
- `coverage`: `--cov=. --cov-fail-under=70` (+ `COVERAGE_PROCESS_START` for
  offscreen-subprocess drivers, parallel combine).
- `order`: whole suite reshuffled by commit SHA, then file-order shuffled —
  both must pass (ordering-dependence probe).
- `compile`: `ci/compile_all.py` — DISCOVERED `*.py` (attic + hidden
  excluded), `py_compile` without imports; empty discovery fails the gate.
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
- GitLab: same gates + pip cache on the lock file, junit report, and
  `after_script` digest (`ci/pytest_summary.py --post`) that prints failing
  names + env-fix hints even when the job dies.

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
- Suite pins what humans forget: system-prompt text, 48-tool census,
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
