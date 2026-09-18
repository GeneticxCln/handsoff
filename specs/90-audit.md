# handsoff — audit 2026-09-15

Method: structural reads + `grep`/`ast` census + `wc` + `pytest --collect-only`
(1390). Five scout subagents dispatched; all five hit free-model rate limits
and returned nothing — findings below are first-hand, each with its source.

## Verdict

No proper spec engineering existed: README (manual) + ACCEPTANCE (desk
checklist) + GAP_ANALYSIS (audit log) + 4 point designs + 2 plans, but no
vision/requirements/architecture/API/data/ops/test-plan. Created as
`specs/00/10/20/30/40/50/60` (this file = `90`). Ship it.

## Strengths (keep)

- Single sources: schema owns defaults+vocabularies+looks; `@tool` owns
  schemas+prompt+permissions; manifest glob owns the shipped set; `Offer` /
  `BoundedRegistry` own admission; `look_matching()` derives the look.
- Loud-degradation culture: boundary fault injection, no fabricated success,
  refusal records on disk + spoken + journaled.
- Deterministic gates: coverage floor, order shuffle ×2, thread-crash=error,
  discovered compile/shell sets, installer smoke, versioned pre-commit.
- Secret hygiene: 0600 + lstat-symlink-refuse, redacted log targets, ICS
  label redaction, secret-path predicate at three entries, token-gated
  mutating socket verbs.

## Ranked risks

1. **`_DEPLOY_FILES` drift — CLOSED (2026-09-16).** `handsoff.py:644` is still a
   top-level floor (the shipped top-level files plus three core modules), while
   install.sh `CORE_REQUIRED` ships 13 core modules. A manifest-driven
   install was already safe — the manifest glob is unioned over the tuple — so
   the exposure was the manifest-less or hand-rolled install, which compared
   only the floor and reported `in-sync` while modules differed.
   `_deployment_snapshot()` now adds the checkout's own `core/*.py` set to the
   compared set whenever a checkout is known: it is the same glob install.sh
   stages by, so a module that exists only in the checkout is drift by
   construction. `tests/test_ops.py::TestDeploymentReporting` pins both halves —
   a manifest-less install reports a module the deployment never received as
   `installed-drift`, and against the real tree and the real `install.sh` every
   module `CORE_REQUIRED` declares is compared. With no manifest AND no
   checkout the floor is all there is to compare, which is why it stays.
2. **Dual model-mirror caches (mitigated, watch).** `core/audio` owns caches;
   `handsoff.py` mirrors `_tts_model`/`_whisper_model` for `_speak`/health/
   doctor/tests. Read-then-assign once let a reload drop be overwritten by a
   stale in-flight read. Current fix (push reads module copy inside the
   lock, drop takes the same lock, adopt refuses republish of a dropped
   model) is ordering-sensitive — any new reader/writer of the mirrors must
   take the same lock or the defect returns.
3. **Monolith mass (accepted, cutting).** The four largest files are priced in
   `20-architecture.md` §1, which is generated from the tree; no size is
   restated here, because a restated size is a size that rots (this item used
   to carry four of them, and two had gone wrong). Cut plan works
   ((a)/4c/4d/4e done) but every new feature landed in `handsoff.py` lengthens
   the critical path the suite + pre-commit already spend minutes guarding.
   Rule: new seams go to `core/` with a `H.*` alias, never new globals in the
   app.
4. **Control-token trust boundary is same-UID (documented).** 0700 dir +
   peer-uid stop other users; a same-UID compromised child passes uid and is
   stopped only by the token — which any process that can read `STATE_DIR`
   can also read. Mutating verbs are safe from sandboxed-app accidents, not
   from a same-user attacker. Do not oversell it.
5. **Regex watcher residual (documented).** Length + exponential-shape guards
   hold; `(a|aa)+` overlap blowup remains expressible. Blast radius is one
   daemon watcher thread; per-line work bounded by `WATCH_LINE_MAX`. Revisit
   only with first-charset analysis, not more length caps.
6. **Ydotool probe is connect-not-round-trip (documented).** Alive-but-wedged
   daemon reads reachable. Round-trip needs the wire struct; `/proc`-inode
   alternative breaks root-owned daemons (measured). Keep the limitation
   stated in doctor output.
7. **Root scratch near-misses (mitigated).** `optimus*.wav`/`output.wav`/
   `test.py` once nearly shipped/committed. `.gitignore` now covers
   root-level audio + `test.py`; installer ships declared+tracked only.
   Watch: any new root scratch extension reopens the hole — prefer `attic/`
   (excluded from cover/compile/ship) or `tests/` fixtures.
8. **Settings GUI is the recovery tool and the biggest unmeasured surface.**
   ~68% via offscreen drivers; multi-output OCR unreliable (fell back to
   data-layer assertions). Appearance-tab live-apply paths need a human eye
   after palette/painter changes — keep the ten-design ink guard green.

## No-inference notes (checked, not assumed)

- No `eval/exec(untrusted)`: only `compile()` for self-edit preview/verify
  (`core/tools.py:852,3315,3324`) + `app.exec()` (Qt) + `__import__` in the
  origin-checked loader. No `pickle`, no `os.system`, no `shell=True`.
- Subprocess is argv-list only (`subprocess.run([...])`), never a shell
  string; shell operators are refused before `shlex.split`.
- `my_tool` (line 441) is a docstring example inside `tool()` — NOT the
  49th tool. AST census = 48.
- `stop_watchers` is a plain method, not a 49th `@tool`.
- `paste_text`/`get_datetime`/`read_file` etc. with no explicit `gates=`
  default to their OWN name — gated, not ungated. Only `gates=''` is ungated
  (`wait`, `niri_capabilities`, `confirm_action`, `handsoff_doctor`).
- Test arithmetic: ~1300 `def test_` vs 1390 collected (parametrization).
  Quote collected when talking CI, functions when talking files.

## Follow-ups (small, ordered)

1. DONE (2026-09-16) — derived rather than listed, and pinned against the
   installer's own declaration: see risk 1. `_DEPLOY_FILES` stays the
   top-level floor; the compared set is the floor plus the checkout's
   `core/*.py` plus the manifest.
2. DONE (2026-09-16) — `tests/test_specs_freshness.py`, its own file rather
   than a class in `test_regression.py` (and listed in `60-test-plan.md`): each
   count is read from the source of truth the spec names for itself — 48 tools,
   64 settings keys, 22 PTT verbs, 13 core modules, plus 19 permission keys —
   and every place a spec states one is read back and compared. It also pins
   STRUCTURE, which a count cannot see: every core module in the architecture
   map, every test file in the test plan, every spec in this index. Size claims
   are deliberately left unpinned, because a line count is a dated snapshot.
   First run, one real gap: `test_settings_contract.py` was in no row.
3. Keep GAP_ANALYSIS append-only; link new batches here, do not merge.
4. Next cut seam: voice pipeline (`Recorder`/`ContinuousListener`/speak)
   behind a `core/voice.py` handle — the largest coherent block left in the
   app that tests already address through `H.*`. Item #1 below (the unwired
   lifecycle module) is done; this is the remaining one.
5. Two-writer gate: `ci/gates.sh` runs the suite against a worktree another
   thread may be editing, so a mid-run write reads as a false red. Refuse the
   run when the tree changed while it ran (compare the hashes it starts and
   ends with) instead of leaving a red that says nothing about the code.
   **DONE (2026-09-17):** `ci/worktree_stamp.py` fingerprints the worktree
   before the first gate and the `two-writer` gate compares after the last one,
   naming the files that moved; the gates' own artifacts are excluded through
   the repo's real ignore rules, so the gate cannot refuse its own run.
   **RESUMED (2026-09-17):** a collision no longer discards the run. The collided
   gate's attempt is dropped, the baseline moves onto the state the collision
   left, and the gate is re-run against it; verdicts from before the move are
   printed as CARRIED OVER, and a second write inside one gate's window stops the
   run. The honest consequence is stated rather than hidden: a resumed run is NOT
   a verdict about one revision — unless the collision was in its first gate, in
   which case it is. **CAUGHT UP (2026-09-17):** and unless the run completes
   itself, which it now does — the overtaken verdicts are re-run in order before
   the tree verdict, so a GREEN run ends as a verdict about one tree, with the
   row it supersedes marked and the split computed per leg. Skipped when the run
   already has a red (a red is a verdict; the digest is built for one tree's red),
   and one pass only: a write inside the catch-up leaves the split and says so.
   **PLACED (2026-09-17):** the verdict could name WHICH gate the tree moved under
   but not WHEN inside it, and that is the difference a reader needs to tell a
   collision from a coincidence — a write 90 seconds into a four-minute suite is
   somebody editing while the tests ran, while the same write just before the
   checkpoint is usually the run settling. Each snapshot now records the clock it
   finished at, so a checkpoint places every changed path's own `mtime` (and a
   HEAD move's commit time) inside the window its two checkpoints bracket, as
   `  - when: install.sh was last written 96s into the tests gate's window (231s
   long), about 42% through — the middle of it, at 14:03:47`. Nothing watches the
   tree, so it is a placement and not an observation, and every shape it cannot
   place says so in words rather than inventing a number: an `mtime` before the
   window is "written 42s BEFORE … the previous checkpoint read it while that
   write was still landing" (the earlier snapshot read that file early and
   finished late) instead of a clamp to zero, an `mtime` past the close is "at the
   very end of … or just after it", a deletion has no clock at all, a window too
   short to have parts loses the fraction but keeps the offset, and a snapshot
   from before the clock existed loses the timing rather than the refusal. The
   tree verdict passes `--since` — the newest checkpoint — because the write it
   catches is the one no gate saw; its VERDICT is still the baseline's, and a
   `--since` that is missing, unreadable or older than that baseline is ignored
   rather than used, since a window the verdict is not about is worse than a
   coarse one. Found
   while building it: the failure digest read a junit report left by a PREVIOUS
   session (it described 1 617 collected tests while the run collected 1 680),
   so stale reports are now pruned at the start of every run. The engine that
   carried the shared-checkout red is the same one this entry was opened by —
   an audit run whose numbers came from a tree another thread was editing.
   **COMPARED (2026-09-18):** the last unanswered question about a resume was
   whether the move MATTERED. The gate could name the file, the gate and roughly
   when — and a reader still had to diff two rows to see that `compile` passed on
   one tree and failed on the other, which is the one finding that says the write
   was the difference rather than noise. Each attempt's own RESULT is recorded as
   it happens — a discarded attempt included, since it is not a verdict but it is
   half of the comparison — and the resumed section prints
   `the move changed the outcome: compile PASS → FAIL`, collapsing repeats and
   keeping every change (`PASS → FAIL → PASS` says a re-run answered as the first
   attempt did after a move that flipped it, which a first-against-last reading
   erases). When every re-run answered as its earlier attempt did the section says
   that in one line too: silence would be indistinguishable from "not compared".
   **REVERTED (2026-09-18):** the gate's oldest stated limit was that "a write
   reverted before its checkpoint leaves no trace", because the fingerprint is an
   endpoint comparison of CONTENT — and the shape it misses is the one where the
   suite read a file mid-edit while every hash the run holds says nothing changed.
   Every tracked file (clean as well as dirty) and every untracked one now carries
   its OWN LAST-WRITE TIME, so a path the two snapshots agree about whose write
   time moved is reported as `written during the window and restored: app.py — the
   content is what the checkpoint read, and only the file's own write time moved`,
   placed in the window by the same clock. It is nanosecond precision because that
   is where a save-and-revert lands, the map is built over `git ls-files` plus
   non-ignored untracked files (the gates rewrite their own artifacts every run, so
   ignored paths must stay outside it), a real content change is still reported
   ONCE — as content — and a snapshot from before the field existed judges content
   exactly as well, only without the restored rows.
6. The clean-checkout gate (see the section below) asks about HEAD, so it tells a
   developer AFTER the commit exists. The class it found in the history — a
   PARTIAL STAGE, where the working tree is self-consistent and the commit is not
   — is invisible to every check run against the working tree, the pre-commit
   hook included, which is exactly why that pair of commits landed. Next: have
   the hook judge the STAGED tree (a checkout of the index) rather than the
   working tree — the same device the gate already uses, one step earlier.

## core/lifecycle.py seam exists but has no production caller — CLOSED (2026-09-17)

**Observation (then):** `core/lifecycle.py` exported `TurnState` and
`next_turn`, and `tests/test_lifecycle.py` verified the module loads without
heavy deps and that concurrent calls produce distinct generations. The
architecture doc (`specs/20-architecture.md:25`) listed it as a dependency-free
leaf describing "atomic increment" for turn coordination, but the host comment
at `handsoff.py:3822` claiming "`_core_lifecycle.next_turn(...)` where the app
needs it" was not true: no production code called it, so the extracted module
was a facade while the live increment sat in `Assistant._bump_gen`.

**What the finding missed:** the duplication was worse than an unused module.
The host kept the number as an int ATTRIBUTE (`self._gen`) beside the module's
own counter, so the value the staleness checks and the gen-keyed transcript
cache trust had two possible homes.

**Resolution:** the module is now the counter's home and the host reads it.
`GenerationCounter` holds the storage and the lock, `claim()` returns the whole
`TurnState` (generation plus the fresh cancel/done events) from inside one
critical section, and `new_counter()` owns the `{"gen": n}` shape so a caller
cannot hand-roll a dict `next_turn` does not advance. `Assistant._gen` became a
PROPERTY over that counter (`handsoff.py:4744`) instead of a second copy, and
`_bump_gen` delegates to `claim()`. The host's `_gen_lock` was kept, re-scoped:
it now guards the one-time CREATION of the per-instance counter, because two
counters for one instance hand out the same generation — the defect the atomic
claim exists to prevent — and `__new__`-built instances (tests skip `__init__`)
have no counter until something asks for one.

**Verification (the gap this entry named):** closed by four guards. The
deterministic probe was re-pointed at the module's `_COUNTER_LOCK` (a claim
that ran outside it must be observed blocked), a spy on
`GenerationCounter.claim` fails if `_bump_gen` ever increments around the
module, a property/view test fails if `_gen` stops reading the counter (a
stored copy is how they would drift), and a source guard bans the raw increment
from the host entirely while asserting the module still holds it.

## A commit is only consistent with the checkout that made it — CLOSED (2026-09-18)

**The gap, named by the entry above and left open in it.** The freshness guard
reads the test plan, the settings-key census and the spec citations out of FILES,
and every file it reads is committed while the checkout it runs in also holds
files the commit does not. So a commit can be green here and red in a clone: the
entry above closed that gap BY HAND, once (a fresh `git worktree` of HEAD, the
whole suite green there), and said so itself — *nothing in the gate set does
that*, so the next commit that adds a test file or regenerates a spec row can
leave the same gap for whoever checks out `main` next. The local suite stays green
because the working tree HAS the files the plan names; only a clone can see the
mismatch.

**Resolution: a gate that is the clone.** `clean-checkout` (ninth gate, registered
between `smoke` and `two-writer`) lays HEAD down in a scratch `git worktree` —
`mktemp -d -t handsoff-clean-XXXXXX`, deliberately OUTSIDE the repo, because a
scratch tree written inside the worktree would move the very tree `two-writer`
stamps — and runs `tests/test_specs_freshness.py` there under
`QT_QPA_PLATFORM=offscreen`. It runs the GUARD rather than the suite on purpose:
that guard is the part of the suite whose verdict is about files rather than about
behaviour, and it costs seconds instead of minutes.

**What it says when it refuses** is the guard's own output — its tail, because the
failing assertion IS the finding and a paraphrase would hide which one failed —
then a REFUSED block naming the class and the one-liner to reproduce it. **What it
does when it cannot run** is a SKIP with its reason, never a pass: not a git
worktree, an unborn HEAD, a scratch directory it cannot make, a `worktree add`
that fails, or a HEAD that carries no such guard. **What it does before it says
anything** is clean up — `worktree remove`, `rm -rf`, `worktree prune`, all before
any verdict is printed, because a refusal that leaves a worktree behind damages
the checkout it reports about, and `git worktree list` is how the developer would
find it. It is registered before `two-writer`, which stays last: that gate
compares the worktree, so anything running after it would invalidate the
comparison.

**Found by committing: the suite is run BY the pre-commit hook, and git exports
its plumbing to that hook.** The first commit attempt was refused by the hook
after three of the new end-to-end tests failed — only there, and every time they
were run alone. The hook is handed `GIT_INDEX_FILE=.git/index`, `GIT_PREFIX` and
an author identity, the fixture's environment was `dict(os.environ)`, and a
scratch repository built with the caller's index therefore cannot lay HEAD down:
`git worktree add` failed inside it and the gate SKIPPED. The root cause is not
the gate but the harness — its child now drops git's own plumbing (one constant,
`GIT_PLUMBING`) before the repository, the identity and the config it supplies
itself, and two guards hold it: one plants the variables and asserts the child
environment is clean, and one reads what a real probe hook is handed and asserts
that list covers it. The probe is the authority on purpose — it is what caught two
names missing from the list when it was written by hand.

**Verification.** Fourteen guards in `tests/test_ci_clean_checkout.py` (seven
wiring, four end-to-end, three on the harness environment), on a scratch repo
whose guard is committed and whose needed file is NOT: the worktree passes, the
clean checkout refuses, no worktree
is left behind, a repo with no such guard is SKIPPED rather than passed, and the
wiring pins the exact invocation, the disposal directory, the cleanup-before-verdict
order, every SKIP path and the refusal's content. **20/20 mutations caught, 2/2
probes green, 0 misses, every restore sha256-verified** (`/tmp/clean_sweep/sweep.py`,
five of them aimed at the harness environment)
— and the sweep earned its keep: the wiring guard read `"worktree add --detach" in
body`, which the gate's OWN reproduce hint satisfies (it prints `git worktree add
--detach /tmp/x HEAD`), so two mutants that took `--detach` off the command that
actually runs stayed green. The assertion pins `worktree add --detach --quiet
"$tree" HEAD` now, and a mutant spelling `--detach` as its short form `-d` is
caught like the rest.

**Live, three shapes.** On this (dirty) checkout the gate PASSes in four seconds,
with the guard's nineteen tests run inside the scratch worktree of HEAD. Against
`bd9cf5a` — the commit from this thread that the hand check found inconsistent —
it REFUSES with all three failures printed: the key census, the plan naming two
untracked tests, and a DATE read as a size. And a fresh scratch commit of the same
class (the new test file committed, its plan row deleted) REFUSES naming exactly
which file the plan does not mention.

**The gate's own limit, stated:** it judges the class the freshness guard knows
about — the plan's file list, the key census, the citations — not "a clean
checkout passes everything", which is a whole suite per run and is CI's job; and
it judges HEAD, so a commit that agrees with itself while being wrong about the
code is not its business.

## The spec set's first two commits a clone refuses — and why the hook was green (2026-09-18)

**What was asked.** That gate answers a question about ONE commit — would a fresh
checkout of it pass its own freshness guard? — so the next question is the commits
already on `main`. Each one was laid down in a scratch worktree of itself and the
gate run there, carrying over only `ci/gates.sh`: the gate is the instrument, and
everything it measures has to be the commit's own. **One correction, found by
running it:** copying today's whole `ci/` into an old tree also carries today's
spec GENERATOR into it, and the guard then compares that commit's generated tables
against a generator they were never written for. Two of `baf58c5`'s five failures
in the first run were that artifact, not drift, and the figures here are from the
run where only the gate travels.

**What is judgeable.** `main` holds ninety-seven commits and ELEVEN of them carry
the freshness guard, which arrived with the spec set (`3ebcd64`, 2026-09-16). All
eleven were judged, and the other eighty-six predate it and come back SKIP — not
"consistent", merely unaskable by this gate, which is also most of the project's
history. The verdicts are nine PASS and two FAIL.

**The two refusals are the pair from 2026-09-18, four minutes apart, and they are
one shape.** `bd9cf5a` fails three guards: the key census (the audit stated a count
five higher than the committed schema, because those keys lived only in the
working tree), the test plan (it named two test files the commit does not
contain), and the size-in-prose guard (a DATED heading naming `core/lifecycle.py`
was read as a line count — the date-stripping fix was not in yet). `baf58c5` fails
five: the same census, the plan naming one absent test file, the same dated
heading, and the generated architecture table, which prices `core/bubble.py` three
lines short of the file that very commit contains — reported twice, once as the
table mismatch and once as the generator refusing to exit zero.

**Why the pre-commit hook was green, which is the finding rather than the trivia.**
`baf58c5` staged `core/bubble.py` and its new test file — and NOT the regenerated
architecture table, which was sitting in the working tree. The hook runs the suite
against the WORKING TREE, where table and file agreed, so it passed and the commit
went through; four minutes later the next commit swept the regenerated table in,
and there the row agrees. `bd9cf5a` is the same shape one step worse: the plan
counted keys and test files that the working tree held and the commit did not. **A
partial stage therefore ships a commit whose own spec disagrees with its own code,
and every check the project had — the hook included — reads the working tree.**
That is precisely the gap the clean-checkout gate closes, and it is why the class
turned up in history exactly once, in the two commits made while a large amount of
neighbouring work sat uncommitted beside them.

**Fixed forward.** Every commit after the pair PASSes, current `main` included, so
nothing on the branch is broken now; the refusals are historical facts about those
two revisions, visible only by checking them out. The class is now refused at
make-time by the gate, and item 6 of the follow-ups above is the half that is
still missing: the hook tests the working tree, so the write-time check the gate
performs could still be defeated by the same partial stage.

**Limits.** The verdict is the gate's: self-consistency of the plan, the census,
the citations and the generated tables, never correctness of the code — a PASS is
"a clone would not refuse it", not "it is right". Eighty-six commits cannot be
asked at all, and the earliest of them predate the spec set entirely. The sweep
ran against this checkout's `main`, which was level with the remote at the time,
so it judged the published head and not some local variant of it.
