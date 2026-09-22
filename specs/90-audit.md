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
   install.sh `CORE_REQUIRED` ships 14 core modules. A manifest-driven
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
- `my_tool` (line 441) is a docstring example inside `tool()` — a docstring
  defines no tool. AST census = 52.
- `stop_watchers` is a plain method, not an `@tool` (it is what the two
  watchers need to be stopped together).
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
   count is read from the source of truth the spec names for itself — 52 tools,
   64 settings keys, 22 PTT verbs, 14 core modules, plus 20 permission keys —
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
6. **DONE (2026-09-18):** the clean-checkout gate asks about HEAD, so it told a
   developer AFTER the commit existed, and the class it found in the history — a
   PARTIAL STAGE, where the working tree is self-consistent and the commit is not
   — was invisible to every check run against the working tree, the pre-commit
   hook included. That is exactly how that pair of commits landed. The hook now
   writes the index out once (`git checkout-index -a` into a scratch directory
   from `mktemp -d`) and the file-shaped checks read THAT: the byte-compile leg,
   the shebang/bash-n leg, and a new spec-vs-tree leg that runs the freshness
   guard inside the staged tree — seconds, and no `.git` required. It refuses,
   rather than falling back to the working tree, when it cannot make the scratch
   directory; it removes it through a trap on every path, the refusal included;
   and the refusal says why the developer   cannot see the problem themselves, which is that the working tree holds both
   halves of the change. **The full suite runs against the staged tree too, as of
   the same day:** the earlier version kept it in the working tree on the
   assumption that a file-only copy needs a `.git`, and that assumption was wrong
   — measured, the whole suite passes in a tree with no repository at all. Two
   checks still skipped there when that sentence was written (an untracked
   scratch in a shipped directory, the installer's no-git fallback); both now
   build what they were missing and nothing skips in a copy at all — see item 7.
   One test had to be fixed to make that true rather than assumed: the
   installer's membership-rule test ran its git half against the developer's
   checkout, so in a copy it died on `set -e` where `git ls-files` failed — it now
   builds its own repository in `tmp_path` and, in doing so, gained the assertion
   it was missing (a TRACKED module outside the declared set must ship, which is
   the point of consulting git at all), and one test asserting the tracked-list
   rule no longer needs a git work tree beside it.

7. **DONE (2026-09-18):** the two skips that remained in a file-only copy — both
   of them questions about the checkout the suite happened to be run in rather
   than about the code. The rehearsal test refused to run without an ambient
   repository, on a belief that had gone stale (that the installer's no-git
   fallback was the glob) and while mutating the developer's checkout to make its
   point: it planted a scratch module beside `handsoff.py` and removed it in a
   `finally`, so an interrupted run left exactly the file the sibling guard
   fails on. Both checks now bring their own fixtures. The rehearsal builds a
   checkout of the shipped files under `tmp_path` — the two globs the installer
   decides about, the script itself (its own directory IS the checkout), the
   artifact installed by name, the requirement pair validated before the pip
   skip — and runs two shapes of it: no repository (the tarball, and the hook's
   staged copy), where the fallback is the declared set, and a repository it
   initialises itself, where a STAGED module ships while an unstaged scratch is
   refused as untracked. `git add` and not `git commit` is what "tracked" means
   here, because that is the question the installer asks. The directory guard
   runs its tracker-free checks in any tree and asks git where git can answer,
   with the teeth of the tracked rule pinned by a fixture that builds a
   repository and plants an unstaged scratch — so the rule is exercised in a
   copy, where no repository exists to ask. It also checks a scratch-SHAPED
   name, which the tracked test cannot: a COMMITTED experiment is "owned", and
   `test.py` was exactly that. Installing both shapes then exposed an ambiguity
   in the installer's own output — it decided "no repository" from an EMPTY
   tracked list, so a repository whose index held nothing was told to edit
   `install.sh` when the remedy was to stage the file. Whether a repository was
   found is now remembered (`HAVE_GIT`) rather than inferred, and both refusal
   reasons are pinned, since the advice they carry is the part a user acts on.

8. **DONE (2026-09-18):** the suite must not write inside the checkout it is
   running against, and that is now checked where it happens rather than hoped
   for — see the section at the end of this file. One site was known from the
   pass before it and the GUARD found two more, which is the argument for a
   property over a fix: each of the three was a test building its fixture in the
   wrong place (a `.venv` seeded beside the source, which also forced the test to
   skip itself on the very machines where the pruning it proves matters; a probe
   module planted beside `handsoff.py`, whose interrupted run left behind exactly
   the file the suite's OWN lifecycle guard then failed on; a module that raises
   on import, written under `tests/` to prove a failed load cannot strand the
   sandbox). All three build under `tmp_path` now, so a run can no longer leave
   anything in the tree.
9. **DONE (2026-09-18):** a child process the suite spawns is now held to the same
   rule — see the section at the end of this file. `sitecustomize` is what carries
   it (the one hook CPython runs in every interpreter at start-up, whatever the
   argv is), `sandbox_env` is the one constructor every child goes through, and
   the shim decides nothing itself: it loads `tests/checkout_guard.py` from the
   checkout it is told about, so parent and child cannot drift about what is
   forbidden. Measured, and this is the part worth knowing: no python child the
   suite spawns writes into the checkout today, so the property was blind rather
   than violated — 1 801 tests pass with every child armed.
10. **DONE (2026-09-18):** the same hook now refuses a write into the developer's
   REAL user directories, which is the tree the checkout sits inside — see the
   section at the end of this file. The failure this closes is quiet by
   construction: the suite runs with the real HOME live (the user-dir sandbox
   covers a LOAD and restores afterwards), so a test that resolves a config or
   state path without it writes to `~/.config` or `~/.local/state` and nothing
   downstream can tell it happened — measured once already, when the settings
   app's `apply_autostart` wrote the real `~/.config/niri/config.kdl`. The roots
   are captured before any sandbox runs and handed to children explicitly (a
   child's HOME is a throw-away one, so it cannot name them itself), and the
   checkout rule is applied FIRST because it has exemptions and normally lives
   inside the protected home.

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
make-time by the gate, and item 6 of the follow-ups above has since closed the
other half: the hook judges the STAGED tree now, so the partial stage that
shipped these two commits is refused at the moment of committing instead of by
the next clone.

**Limits.** The verdict is the gate's: self-consistency of the plan, the census,
the citations and the generated tables, never correctness of the code — a PASS is
"a clone would not refuse it", not "it is right". Eighty-six commits cannot be
asked at all, and the earliest of them predate the spec set entirely. The sweep
ran against this checkout's `main`, which was level with the remote at the time,
so it judged the published head and not some local variant of it.

## The suite may not write into the checkout — CLOSED (2026-09-18)

**What was asked.** Make it a checked property that no test writes inside the
repository — fail the suite when a test's writes touch a path under the checkout
instead of a fixture it built — and fix the site that does.

**Why it cannot be a comparison of the tree before and after a test.** Both
incidents this closes were CREATE-THEN-DELETE: a probe module planted beside
`handsoff.py` and removed in a `finally`, and a `.venv` seeded beside the source
and removed in one. A diff cannot see either — the file is gone by the time
anything could look — and what an interrupted run leaves behind is exactly the
file such a test would have removed, which is how one of them ended up failing
its own neighbour's guard. So the rule is enforced AT THE WRITE: `tests/conftest.py`
installs an audit hook that refuses, before the syscall runs, any write, create,
rename, metadata change or removal whose target resolves inside the repository.
Nothing lands, the message names the file, and the failure lands on the test that
tried rather than on whichever neighbour later trips over the leftovers. Its own
tests sit beside the sandbox that owns the other half of the same idea (the suite
must not resolve the developer's real user directories), and the property's tests
are what make the guard a checked claim — a guard that went quiet is
indistinguishable from a suite with nothing to refuse.

**Three sites had the habit, and two of them were found by the guard rather than
by reading.** The known one seeded `.venv` inside the checkout to prove the shell
gate prunes a vendored tree, and it had to skip itself whenever a real `.venv` was
already there — the machine where that pruning matters most — while writing and
removing a directory in the developer's tree to make its point. It copies the gate
and the one shell file it must find into a tree of its own under `tmp_path` now:
the gate reads the tree it STANDS IN, so a copy is the whole fixture, and the
vendored directory is seeded there. The other two write a file and then take it
away — a broken-syntax probe at the checkout root for the pre-commit hook's
compile leg, and a module that raises on import written under `tests/` to prove a
failed load cannot strand the sandbox. Both take a path, neither cares where the
file lives, and both now live in the fixture.

**What the suite found in the guard, in the order it found it.** The first draft
judged a LINK'S SOURCE as a write, which refused every fixture that symlinks the
app's modules into a scratch tree — the freshness guard builds exactly that, so
the suite went red immediately, and the rule is the destination only (a symlink
stores its target as text, and a hard link leaves the source's contents alone). A
copy with no repository then refused pytest's own start-up, because `.pytest_cache`
is built inside a `pytest-cache-files-*` directory that is renamed into place — its
CONTENTS are written at the root first — so that directory is exempt alongside the
cache it becomes. A bare name arriving against a directory fd is resolved against
THAT directory, not the working one: `shutil.rmtree` walks a tree and unlinks by
bare name, so reading those names the obvious way judged a fixture under /tmp as
if it were inside the checkout and refused every test that tidies up after itself.
And the gates found the third, because a gate is a second way of running the same
suite: `ci/gates.sh` asks pytest for a junit report under `tests/`, pytest makes
sure that directory exists — `os.makedirs(exist_ok=True)` reaches `os.mkdir` on a
directory that IS there — and the hook refused a creation that could only have
failed, so the `tests` gate went red on `tests` before a single test ran. The rule
now says what the syscall would: a creation that cannot succeed writes nothing
(`os.mkdir` and a link's destination when the path already exists), while removal,
a write-open, a rename, a truncation and a metadata change are judged whether or
not they could succeed.

**Exempt, and why the list is short.** The tooling's gitignored output: bytecode
caches, `.pytest_cache`, `.ruff_cache`, the coverage data file and its parallel
shards, the junit reports the gates ask pytest for, and the cache-probe directory
above. Those are written by pytest and coverage rather than by a test, and without
them the suite would refuse its own machinery. Everything else under the root is a
test's, spec set and root ledgers included.

**Teeth.** Twelve tests in `tests/test_sandbox.py` — the refusal at the write, a
removal, a directory creation, a read that must NOT be refused, the decision table
(with the sibling-prefix trap, every exemption and the names that must stay
refused), the flags rule, the directory-fd rule, the link source/destination rule,
the rule for creations that cannot succeed, and a fixture that must stay writable
including the `rmtree` that tidies it — and a sweep of 16 mutants with 16 caught,
0 missed, every restore sha256-verified: the hook never installed, the refusal
unreachable, only `open` judged, the boundary turned back into a string prefix,
the allowlist swallowing everything, the cache-probe and coverage exemptions
dropped, reads counted as writes, a link's target judged again, the directory-fd
resolution dropped, the cannot-succeed rule dropped (the case the gates hit), the
junit exemption widened to any `.xml` under `tests/`, and the three repaired sites
put back the way they were. Three mutants earned their keep by failing for the
WRONG reason first, which is the sweep doing its job on the tests rather than on
the guard: the hard-link row began as a real `os.link` from the checkout into
`tmp_path`, where the kernel's `Invalid cross-device link` is a red that says
nothing about the guard; the `.venv` mutant first reused the checkout root, so
`shutil.copy2` raised `SameFileError` before the guard was consulted; and the
symlink row named an EXISTING target, which the cannot-succeed rule then allowed —
hiding a mutant that judged the source — so the pin names a path that is not there
yet, which is also the honest case (a fixture links a module before creating it).

**Measured.** The whole suite in a copy of the working tree with no `.git`
anywhere: **1798 passed, 0 skipped in 245 s** (the 1786 the previous entry
measured, plus these twelve). The checkout and gate figures are in the run entry
in `GAP_ANALYSIS.md`. The guard costs nothing measurable: with it installed the
copy runs in the same band (245–255 s) the suite measured before it existed, and
the measurement has a control — a pytest plugin that makes `addaudithook` a no-op
turns the four refusal tests red while the allowed ones stay green, which is what
makes "these tests do the refusing" a claim rather than a hope.

**Limits.** It covers the suite's process, and (since the same day) the python
children it spawns — item 9 above, closed at the end of this file. A BASH child
is not a Python interpreter and cannot be reached by a `sitecustomize`; the
`python3` such a child starts does inherit the shim, which is where an installer
run would be judged. The `open` audit event carries no directory fd, so a write opened by bare
name against such a descriptor would be judged against the working directory — a
refusal rather than a missed write, and nothing in the suite opens a file for
writing that way. The exemptions are judged by NAME, so a test that deliberately
created `pytest-cache-files-…` at the root would be allowed to write there; that
is the price of a prefix rather than a syscall-level fact, and it costs more to
attempt than it gains.

## The guard travels into the children the suite spawns — CLOSED (2026-09-18)

**What was asked.** Extend the checkout-write guard into the children the suite
spawns, so the offscreen GUI scenarios are held to the same rule. The limit the
previous section ended on was real and large: the property held in the suite's own
process, and the child processes are where most of its behaviour actually runs —
every offscreen GUI scenario, every `run_driver` driver, everything a bash child
starts.

**How it travels, and why that hook.** `sitecustomize` is the one module CPython
tries to import in EVERY interpreter at start-up, whatever the argv is (`-c`, a
script read from stdin, `-m pytest`, something a shell started), so no call site
has to be taught anything: `sandbox_env` — already the one constructor every child
the suite spawns goes through — writes one `sitecustomize.py` into a scratch
directory, puts that directory FIRST on the child's `PYTHONPATH`, and names the
checkout in `HANDSOFF_CHECKOUT_GUARD`. The shim is a POINTER, not a second copy of
the rules: it loads `tests/checkout_guard.py` from the checkout it was told about
and calls its `install()`. That is the design decision worth stating — two copies
of "what is forbidden" would drift, and the child's copy would be the wrong one.
First-on-the-path is not decoration either: a developer's own `sitecustomize`
would otherwise shadow it, and the guard would silently not install.

**The property had to move to be shareable, and that is how it is written down.**
The rules left `tests/conftest.py` for `tests/checkout_guard.py`, which both kinds
of process load; conftest now installs it for this process and exports the means
of installing it in a child. Everything the previous section established stayed
true — enforced at the write, the event table, the exemptions, the limits — and
the sweep was re-pointed at the new home rather than left passing against a file
that no longer holds the code (13 of its 22 mutants would otherwise have reported
SKIP).

**Measured, and the measurement is the good news.** The whole suite with every
child armed: **1 802 passed**, no child refused anything — so no python child the
suite spawns writes into the checkout today. The property was BLIND rather than
violated, which is the honest description of what closing this changed: nothing
was broken, and nothing was watching. In a copy of the working tree with no `.git`
anywhere: 1 802 passed, 0 skipped in 249 s.

**One thing guarded children broke, which the coverage gate found.** A child that
starts coverage under `COVERAGE_PROCESS_START` measures the shim as well, and the
report then refuses to print a TOTAL over a file outside the checkout: the first
run printed `No source for code: /tmp/handsoff-guard-*/sitecustomize.py` where the
summary belongs. The scratch directory is omitted in `.coveragerc` now, with the
reason next to the pattern, and a test pins the omission because it is invisible
until the next gate run.

**Teeth: 4 more tests in `tests/test_sandbox.py`** (16 in the class) — a real
child, spawned the way the suite spawns them, is refused and leaves nothing
behind, and it is asked WHICH TREE it is judging (a guard pointed at the parent
directory would refuse the write the test watches for, and would also refuse the
legitimate writes beside the checkout, so the answer is the assertion); the SAME
child with the one variable removed and nothing else changed writes the file and
cleans up, which is what makes the pair evidence rather than a hope — the shim's
presence is not the guard, the suite asking for it is; the wiring itself, the
first-on-the-path rule plus the no-second-copy rule; and the coverage omission
above. **23/23 mutations caught, 0 missed, every restore sha256-verified** — the 16
of the previous section, re-pointed, plus seven aimed at the travel and the
omission: the variable never exported, the shim
directory never on the path, the shim directory put LAST instead of first, the
shim installing the guard for the wrong tree, the shim pointing at a module that
is not the guard, and the shim arming itself when nothing asked it to. That last
mutant is worth recording for what its first draft taught: `if _root:` → `if True:`
with the root left as None was MISSED, because a `sitecustomize` that RAISES is
swallowed by `site` with one line on stderr — the child ran on unguarded and the
control passed. The mutant that bites is the decline a real one would choose
(`or os.getcwd()`), and the control is what catches it.

**Limits.** A bash child (`bash ci/gates.sh`, `git`, `install.sh` itself) is not a
Python interpreter and cannot be reached by a `sitecustomize`; the `python3` a
bash child starts does inherit the shim, which is where an installer run would be
judged. The shim directory goes first on `PYTHONPATH`, which shadows a developer's
own `sitecustomize` inside the suite's children — the price of the one hook that
every child has. A child spawned with a hand-built environment rather than
`sandbox_env()` does not carry the guard, which the wiring test pins at the
constructor; the children that do that today are bash and git, not Python. And the
shim's silent no-op when the guard module is missing at the root it was given is
deliberate — a child that died at start-up would fail for a reason unrelated to
what it was testing — with the module's presence pinned at the source instead.

## The guard refuses the developer's real user dirs too — CLOSED (2026-09-18)

**What was asked.** The checkout is not the only tree a test can reach. Extend the
same hook to the developer's real user directories, so a test that hand-writes
`~/.config` or `~/.local/state` fails instead of landing there.

**Why it is a different failure from the checkout rule.** The suite runs with the
developer's OWN home live: the user-dir sandbox swaps HOME/XDG for the duration of
a LOAD and restores it, so a test that resolves a config or state path by any
other route — a module loaded before the sandbox, a child built with a hand-made
environment, a path resolved from `pwd` or from a constant — writes into the
developer's real files and leaves nothing behind to say so. Not hypothetical, and
not caught by anything: the settings app's `apply_autostart` wrote the real
`~/.config/niri/config.kdl` from a test that only believed it was writing into a
temp home, and the fix then was to sandbox that load. Nothing noticed the file.
The checkout rule would not have fired either — the path is a sibling of the
checkout, not a file in it.

**The roots are passed in, never read from the environment.** `install(root,
protected=...)` takes them, conftest captures them while it is imported — the same
moment and the same reason as `_REAL_HOME`, before any sandbox can move HOME — and
names them for a child in `HANDSOFF_GUARD_USER_DIRS`, a SECOND variable beside the
checkout one. The child cannot answer the question itself: its HOME is a
throw-away directory, so a guard that derived the roots from its own environment
would protect a sandbox home and leave the developer's real one open, silently.
Two variables, two rules, and the control removes one to show the other still
works.

**The checkout is asked first, and the order is load-bearing.** The checkout
normally lives inside the protected home, so judging the user dirs first would
refuse pytest's own `.pytest_cache` — the suite could not start. A checkout path
is therefore exempt exactly as before (`.coverage*`, `report*.xml`, bytecode and
cache directories), and a checkout file is still refused by the checkout rule,
which is the message a home-first guard would get wrong. Both halves are pinned:
the predicate table, and a test that creates and removes a `pytest-cache-files-*`
directory at the root of the tree.

**The sweep found a weakness in the guard itself, which is the reason to run it.**
The mutant that made the rule follow the LIVE home instead of the captured paths
was, at first, refused with the message about the CHECKOUT: `_message` asked "which
protected root matched this path?" and, when none did, fell through to the
checkout text. The refused path was in a sandbox home and the message named the
wrong tree — from the guard, to a developer trying to read it. The dispatcher now
asks WHERE the path is, which is the same question the hook asked, so the two
cannot disagree. Two more mutants shaped the sweep itself: the blunt form of
"reads count as writes" (drop the flags check for every path) used to be caught at
collection and now dies even earlier, in the interpreter's start-up reads of the
developer's user site-packages — so it proves nothing about this guard and the
mutant is scoped to the checkout, where its evidence is the refusal text (reading
is how the runner loads its own files, so no test assertion is reachable).

**Measured.** The whole suite in the working tree: **1 812 passed**. The same tree
in a copy with no `.git` anywhere: **1 812 passed, 0 skipped in 264 s** — where the
protected roots still name the developer's real home, so the copy is held to the
rule too. No test writes into the developer's home today, which is the second time
this property has been BLIND rather than violated, and the honest description of
what closing it changed.

**Teeth: 10 more tests in `tests/test_sandbox.py`** (68 → 78), in
`TestNoTestWritesInTheDeveloperDirs`: the refusal before the write lands, at a
direct home path and at the two real shapes (`~/.config/handsoff/…`,
`~/.local/state/handsoff/…`), the message naming the directory and the fix, the
predicate table (including the sibling-prefix trap in the rule added second, and
the home directory itself), the checkout's precedence, the roots captured before
any sandbox and NOT moved by one, the sandbox home staying writable from inside a
load, a child the suite spawns refusing it while the same child writing into a
fixture is allowed, the per-rule variables with the control that removes one, and
the environment plus shim carrying the roots. **31/31 mutations caught, 0 missed,
every restore sha256-verified** — the 23 of the previous section re-pointed, plus
eight for this rule: the protected root pointing at the wrong directory, the guard
installed without the dirs, the child never told them, the shim ignoring what it
was handed, the hook dropping the second rule, the boundary becoming a string
prefix, the checkout losing its precedence, and the rule following the live HOME.

**Limits.** The same three the child half states (a bash child is not a Python
interpreter; a hand-built environment does not carry the shim; the shim is silent
when the guard module is missing) apply here unchanged. The roots are the same
three the sandbox pins — HOME and the two XDG dirs the app resolves — so a
`XDG_CACHE_HOME` pointing outside the home is NOT protected: caches are not what
this project's user dirs mean, and widening it would refuse third-party cache
writes (fontconfig, Qt) that no test controls. And the rule protects PATHS, not
owning tests: a write that reaches the real home through a child spawned with a
hand-built environment is still out of reach, which is the same seam, stated the
same way.

## The card tells one story — every tenant, and what the next turn asks for (2026-09-19)

**What was asked.** Give the bubble one VRAM story: a doctor section that names
every tenant on the card, what the speech models hold, what the LLM holds, and
what the next turn would ask for.

**Why it was not one story.** Three lines each answered a piece of it — `brain
fit:` (can this model EVER fit the card), `llm memory:` (what an idle release
would do with the model), `gpu headroom:` (free bytes, this bubble's share, the
release's state) — and none of them could be checked against the others by eye.
The number a user needs first is the one nobody printed: **who took the rest of
the card**. The audit that produced the idle release had to measure free VRAM by
hand for exactly that reason.

**One collector, one section, two surfaces.** `handsoff._vram_headroom()` builds
one dict — `card`, `tenants`, `speech`, `llm`, `next_turn`, `idle_release`,
`llm_release` — and `_vram_story_lines()` renders THAT dict as a section:

```
card: NVIDIA GeForce RTX 4060 Ti — 10.1 GB free of 16.0 GB (37% used)
  tenants: this bubble holds 3.1 GB (measured); an Ollama process holds 6.8 GB
  (pid 4242); 2 other processes hold 0.8 GB (gslapper 0.4 GB, chrome 0.4 GB);
  the driver attributes 0.7 GB to no process
  speech: whisper 1.2 GB on cuda and the speech model 1.9 GB on cuda — 3.0 GB
  together, all of it this bubble's
  llm: gemma4:12b is resident 7.0 GB and ALL of it is on the card (blob 7.0 GB)
  next turn: asks nothing — the LLM is already on the card in full, so a turn
  loads nothing
  release: in 8 minutes of quiet; after 600s idle the release would KEEP …
  brain fit: gemma4:12b needs 7.0 GB of the card's 16.0 GB, leaving room …
```

`doctor_json` publishes the same dict (as `gpu_headroom`, its documented key),
so the words and the numbers are one reading rather than two that agree. The
doctor's `llm_lines` dep is RETIRED with the `llm memory:` line it fed: the LLM's
memory was being reported twice — as a policy there and as a number in the
headroom line — which is the defect a single story removes.

**The tenants are the new part, and the driver is asked properly.** One query
(`--query-compute-apps=pid,process_name,used_memory`) classifies each attributed
process: this pid is the bubble, a process whose name carries `ollama`/`llama` is
the LLM, everything else is named. Two things the section states rather than
implies: the LLM classification is a READING of how Ollama ships its runner (a
runtime that renames itself lands under `other` and its memory still appears),
and the bytes the driver attributes to no process at all get their own clause —
which is what makes the parts add up to the used bytes on the header line, the
property that turns four numbers into a story (and a test).

**The comma trap, found live.** nvidia-smi prints the process's whole command
line in the name column, and a browser's gpu-process row carries its entire argv
— commas included (measured here: `/opt/google/chrome/chrome --type=gpu-process
--field-trial-handle=3?i=1,2?3`). Splitting the row on every comma turns one 382 MB
tenant into a dozen unparsable fragments and the card's total stops adding up, so
the row is split at the FIRST and the LAST comma, and a two-column reply (no name
column, an older nvidia-smi) is still a tenant — unnamed, but counted.

**Speech, LLM, and the ask.** Speech comes from `core.audio.gpu_footprint_mb` —
the loader tables the idle release and the turn's yield verdict already weigh — so
per-model bytes, device and the total describe the same models those decisions
move. The LLM's half reads `/api/tags` for the blob and `/api/ps` for the split,
with `need_mb` = `size - size_vram` (the unmet remainder a turn actually asks
for). The `next turn:` line runs the SAME `yield_to_llm_verdict` the turn runs, in
the same order, and short-circuits on the case the old arithmetic got wrong: a
fully resident model asks for NOTHING and says so, instead of being weighed
against its whole blob.

**Two real defects the work exposed, both fixed at the cause.**

1. *An unreadable residency was reported as "NOT loaded".* `ollama_resident`
   answers `{"loaded": False}` when `/api/ps` answers with no models and `None`
   when it cannot be asked; collapsing the two would have made a wedged endpoint
   read as a measurement. The section's `llm:` line keeps them apart ("could not
   be read" vs "is NOT loaded"), pinned by a test whose stub fails `/api/ps`
   while `/api/tags` still answers.
2. *The cold-cache order wiped the residency it had just read.* `_llm_footprint_mb`
   dropped the record's residency whenever the MODEL key did not match — which is
   true of a cold record, so the section probed, then the blob read reset it, and
   the next line probed again. The residency is now stamped with the model it was
   read for (`resident_model`), which is the honest form of the same rule (a
   residency belongs to a model) and makes a swap read as cold instead of
   inheriting the previous model's answer.

**Measured on the final bytes.** `tests` **PASS — 1 874 passed in 261 s** (was
1 860); `coverage` **PASS, 84.90%** (2 593 missing of 17 169) ≥ 70;
`compile`/`shell`/`smoke`/`clean-checkout` PASS; specs freshness **19 passed**
after the generated module table was rewritten from the code. Live section from
this machine, run through the real loader: the header names the RTX 4060 Ti,
the tenants line names four processes plus the 1.6 GB nobody is attributed, the
`speech:` line names what is loaded, the `llm:` line names the configured model
and its blob, and `release:` carries both the window and the verdict.

**Teeth: 14 new tests in `tests/test_idle_release.py`** (135 → 149 `def test_`,
140 → 154 collected): every tenant named; the parts add up to the used bytes;
the header names the card; a comma-bearing process name is one tenant; a driver
that cannot attribute says so rather than claiming an empty card; the resident
model's turn asks nothing; a split model is described as split with the
remainder as the ask; an unreadable residency is not "not loaded"; the speech
line names both models and their device and refuses to count what is not on the
card; the descriptive read is cached while the turn keeps it warm; a swapped
model does not inherit the old residency; every line survives a card that cannot
be asked; and the section and the JSON are the same reading. **11/11 mutations
caught, 0 missed, every restore sha256-verified**: the row split at the first
comma, an Ollama runner classified as somebody else, the LLM's bytes attributed
to nobody, only the LLM counted as attributed, the unattributed residue dropped,
a resident model asked for its whole blob, the speech device dropped, an
unreadable residency reported as not-loaded, the descriptive read never cached,
the residency unstamped, and the host wiring the section out of the doctor.

**Stated limits.** The tenants are the driver's attribution, so a driver that
reports no per-process memory leaves the section saying exactly that (and the
bubble's own share falls back to the loader tables, labelled as an estimate).
The LLM's tenant appears under its PROCESS NAME, which is a reading of Ollama's
naming and not an interface it publishes. The bubble's measured share and the
speech models' table footprint are different measurements of different things
(the first includes CUDA context and library overhead), and the section says
which is which rather than reconciling them. And the section is a snapshot: it
is built when doctor runs, so it cannot show a card that filled a second ago.

## The address that was checked is the address that is fetched — CLOSED (2026-09-19)

**What was asked.** Close the DNS-rebinding TOCTOU in the web reader: pin the
address that was validated for the actual fetch and bound the resolver, or state
precisely why pinning cannot work there.

**The hole, in one sentence.** `_public_target` validated a NAME and the
transport resolved that name AGAIN, so a name server was free to answer the
policy with a public address and the socket with `127.0.0.1` (or
`169.254.169.254`) a moment later — and no check on the name can close that,
because the two answers are the whole mechanism. The check and the fetch have to
be about the same value.

**Pinning works here, and the shape is what makes it a pin.** The policy now
returns the addresses it approved (`_public_target`), `_read_fetch` carries them
into the fetch (`connect_to`), and the host dials them: `_http_get_hop` routes a
pinned hop to `_pinned_get`, which builds an `http.client` connection through
`_pinned_connection`. That subclass replaces ONE callable — the instance's
`_create_connection`, which is the single place `http.client` builds its socket —
with `_dial_pinned`, which connects to the address literals in the order the
policy checked them. `self.host` is untouched, so the Host header, the SNI and
the certificate check stay about the name the user asked for: this is a pin, not
a rewrite to an IP. It is safe to hand the transport literals precisely because
the pin is what removed the name from that last step.

**Every hop carries its OWN pin, and that is a second bug avoided.** The walk
rechecks each Location through `_public_target` (so a redirect is just another
untrusted address) and passes the NEW hop's addresses; carrying the first hop's
pin forward would have fetched the last hop at the first hop's host — the same
class of mistake as not checking the redirect at all.

**Why a proxy had to be bypassed rather than used.** An `HTTP_PROXY` in the
environment is another party that resolves the name itself, which is exactly the
resolution this exists to delete, and `urllib`'s opener owns both that and the
hostname lookup. The pinned path is therefore `http.client` directly, and the two
things that costs are stated at the site rather than discovered: no proxy on a
pinned fetch, and `Accept-Encoding: identity` because that path does no
decompression.

**The seam is DETECTED, and degradation is loud.** `_accepts_connect_to` reads
the injected function's signature, so a partial install (or a test double) still
reads in one shot instead of losing the feature — but it is WARNED about, and the
warning names BOTH losses (unchecked redirects, and the name resolved a second
time). Writing the tests found the second silent path: `_hop` returning None
(seam gone mid-walk) fell back to the one-shot fetch with NO warning at all,
which would have read as a pinned fetch in the journal. It says so now.

**The resolver is bounded, because `getaddrinfo` has no timeout.** `_DNS_TIMEOUT_S
= 5.0` runs the lookup on a daemon thread and joins it: the reader walks away
rather than holding a spoken turn open on a name server that never answers. That
is a bound, not a cancellation — the thread is not interruptible, and a truly
stuck resolver leaves one behind (stated in the spec).

**Measured while designing, then guarded offline.** The mechanism was prototyped
against a real host before it was written: dialling `example.com`'s address while
`server_hostname` stayed `example.com` negotiated that certificate, and a foreign
name on the same connection was refused — so the pin reaches the socket without
changing what TLS checks. Every guard since is offline: the host test drives a
real `ThreadingHTTPServer` on loopback through `_http_get_hop` with a pin, using
`http://example.invalid:PORT/…` — a name reserved by RFC 2606 that cannot
resolve, so reaching the server at all IS the proof — and asserts the server saw
`Host: example.invalid` and the path intact. A second test records what the
dial hands the resolver and asserts it was the checked literal and never the URL's
name.

**A defect the pin's own path exposed, fixed here.** Checking the policy function
the pin depends on turned up an address shape that RAISED out of `read_page`:
`urlsplit` defers the port check to its `.port` property, which raises
`ValueError` for `:99999` or `:abc`, and nothing caught it — so a model-supplied
address reached the tool wrapper's broad `except`, which could only say
`ERROR: could not read that page (ValueError)` and log a warning. Every other bad
address gets a refusal with a sentence naming what was wrong, and an untrusted
address is not a special case, so the port is read under `try` and the answer is
`… has a port that is not a number between 0 and 65535`, still with nothing
fetched. Three refusal cases pin it (out of range, non-numeric, negative).

**Teeth: 11 tests in `tests/test_web.py` (64 → 75 test functions, 86 → 89
collected).**
`TestTheCheckedAddressIsTheOneFetched` (the policy returns the addresses it
checked; the first hop is handed the pin; every redirect hop gets its own new
pin; an unpinnable seam is warned about and still reads; the no-seam fallback and
the vanished seam both state what they give up; a stuck name fails inside the
bound) plus four in `TestHandsoffWiring` (the injected hop seam advertises
`connect_to`; the pinned fetch dials the checked address and keeps the name; the
pin replaces the dial and nothing else — `conn.host`/`conn.port` untouched; a
second checked address is still tried; the name is never resolved). **11/11
mutations caught, 0 missed, every restore sha256-verified**: the seam never
handed the pin, the walk forgetting the first hop's pin, the policy returning no
addresses, an unpinnable seam used silently, the resolver left unbounded, the
pinned connection dialling by name again, the hop fetch ignoring `connect_to`,
only the first checked address dialled, the seam dropping the parameter, the two
warnings silenced, and the malformed port raising again.

**Measured on the final bytes.** `tests` **PASS — 1 860 passed in 266 s**;
`coverage` **PASS, 84.75%** (2 595 missing of 17 017) ≥ 70; `compile`, `shell`,
`smoke` and `clean-checkout` PASS; specs freshness **19 passed** after the
generated module table was rewritten from the code. The tree is **checkout only**
as of this section: `doctor` reads `deployment: installed-drift`, because
`core/web.py` (and `handsoff.py`, whose earlier round was also never deployed)
differ from `~/.local/bin/…`.

## The installer stopped deciding what the app already decided (2026-09-19)

**The finding.** The pass that fixed step 8 judging `qwen3:8b` while the bubble
ran `gemma4:12b` was read at the time as one hardcoded default. It was a class:
four more places in `install.sh` provisioned something the app already decides,
and each duplicate could drift in the same direction — the script preparing a
machine for a configuration nobody has.

* **Step 4 downloaded `WHISPER_SIZE="${HANDSOFF_WHISPER:-tiny}"`** while the
  bubble loads `SETTINGS["whisper_size"]`. A user who set `small` got `tiny`
  fetched into the cache the bubble reads **from**, and `tiny` recorded in the
  deployment manifest — the bubble then fetched `small` itself at the first
  reply, or failed to offline, with the manifest agreeing with nobody. A size
  the app would not accept was passed straight to `WhisperModel(...)`, which is
  a traceback mid-install instead of a refusal naming `WHISPER_SIZES`.
* **Step 8 probed `curl … http://127.0.0.1:11434/api/tags`** while the app reads
  `OLLAMA_BASE = SETTINGS["ollama_host"]`, and the `ollama` CLI behind `pull`
  and `show` resolved its own default endpoint. On a machine pointed at a remote
  brain the script reported "not running", ran `sudo systemctl enable --now
  ollama` (starting a local server the bubble never talks to), pulled gigabytes
  into it, and judged **that** server's tool support.
* **Step 6 primed `HANDSOFF_TTS_REPO:-ResembleAI/chatterbox-turbo`**, a second
  copy of `core.audio.TTS_REPO_ID` — the constant the bubble builds its cache
  path from. This one had already bitten: piper → chatterbox renamed the engine,
  and the copy would have primed 3.8 GB the app never reads while the script's
  own "snapshot is usable" check passed.
* **Step 7 wrote a niri rule for `app-id=r#"^handsoff$"#`** while the app sets
  `setDesktopFileName(APP_NAME)`. A renamed id stops the rule matching silently:
  the bubble would simply stop floating where the user put it, with a snippet
  that still looks right.

**The fix.** Two failure-tolerant readers — `_read_setting` over
`settings.json`, `_app_constant` over a module-level literal via `ast` (never an
import: `handsoff.py` builds a QApplication at import) — and one resolver shape
(`_resolve_whisper_size`, `_resolve_ollama_endpoint`, assignment-time reads for
the model, repo and app-id). The fallbacks stay for a bare machine, named in one
block (`DEFAULT_MODEL`, `DEFAULT_WHISPER_SIZE`, `DEFAULT_TTS_REPO`,
`DEFAULT_APP_ID`, `OLLAMA_DEFAULT`) so one test can hold all five against the
app's own defaults. `_resolve_ollama_endpoint` mirrors the app's remote rule
exactly: settings compared **strictly** (`is True`, core's fail-closed contract),
then the send guard's env tokens; without either, it says the bubble would refuse
that endpoint and checks loopback instead. The niri rule takes an `@APP_ID@`
placeholder and `sed` substitutes it — an unquoted heredoc would read `$"` as a
bash locale expansion.

**Teeth: 6 new tests in `tests/test_ops.py`** (70 → 76 collected) — the
configured size, its refusal of `huge` with the list named, the env knob still
winning, the bare-machine default; the endpoint in five shapes (loopback port,
bare `host:port` gaining the scheme, a real bool opt-in, a hand-edited string
that is NOT one, the env token, and the un-opted remote falling back with the
warning); the repo read out of `core/audio.py` **with the constant renamed in a
copy of the module and the answer following**; the app-id read out of
`handsoff.py` with the rule carrying no copy of it; every fallback against
`DEFAULT_SETTINGS`/`WHISPER_SIZES`/`TTS_REPO_ID`/`APP_NAME`; and a rehearsal
end-to-end where a seeded `whisper_size: small` makes step 5 say 'small', the
manifest record `small`, and step 8 name the configured server. **18/18
mutations caught, 0 missed, every restore sha256-verified** (the hardcoded size
back, the resolution ignoring settings, the manifest recording a literal, the
accepted-size check gone, the hardcoded model back, loopback back, the opt-in
read loosely, the remote branch never falling back, step 8 probing a literal,
the CLI keeping its own server, the local service started for a remote host, the
repo literal back, the `ast` reader returning nothing, the rule naming its own
id, the placeholder never substituted, and each of the three fallbacks drifting).

**Stated limits.** `_read_setting` prints only scalars: a list or dict setting
is not a value any caller here can use, and printing one as text would be a
lie — so `spotter_models`-shaped settings resolve to their fallback by
construction. The readers fail soft by design, so a settings file that cannot be
parsed provisions the defaults rather than refusing the install (said in the
notes only where the value is refused for being out of range). `_app_constant`
reads module-level literals; a constant that becomes computed would resolve to
its fallback. And the CLI tools the app shells out to (`ydotool`, `grim`,
`tesseract`, `mpc`, `wl-clipboard`) have **no** app-side single source to read —
they are named at their call sites with their own `pacman -S` advice — so the
package list remains a provisioning decision this script owns, not a duplicate of
the app's.

**Measured on the final bytes.** `tests` **PASS — 1 880 passed in 258 s**;
`coverage` **PASS, 84.9%** ≥ 70; `compile`, `shell`, `smoke` PASS; specs
freshness 19 passed after the test-plan row and snapshot sentence were
corrected. The tree is **checkout only**: `install.sh` is not in the deployment
manifest, so the in-sync verdict is unaffected by this section.

**Stated limits (the earlier section).** The pin covers the READER's hop seam. The plain `http_get`
seam still resolves names, and deliberately so: its URLs are the module's own
backend constants, and the one place a model-supplied address rides through it is
the Jina fallback, whose HOST is a fixed reader and whose own hops are walked and
pinned like any other. A pinned fetch bypasses an environment proxy and asks for
identity encoding (both above). A host that injects no hop seam reads unpinned and
is warned. The bound on DNS is a thread the reader abandons, not a cancel. And the
watcher's `(a|aa)+` ReDoS remains open, as stated in the section before this one.

## A 200-comment full-tree scan: seven real defects, and the four claims that were wrong (2026-09-19)

Method: a full-repository OCR scan (32 files, 200 comments) triaged against source,
one claim at a time, by reading the code it cites and reproducing each one that
turned out to be real. The scan's own severity labels were not trusted: three of
its `[bug · critical/high]` findings do not survive contact with the file they
quote, and several of its `[maintainability · low]` ones do.

**Fixed, each with a guard that fails on the unfixed code (9/9 mutations
caught, every restore sha256-checked):**

1. **The notification reader could hot-spin at full CPU.** A `loop()` pass that
   *raises* is not exhaustion: the monitor can still `poll()` as alive, so the
   respawn branch is skipped and the call was re-entered immediately, logging one
   traceback per pass for as long as the reader stayed enabled — while still
   reporting itself enabled. `attempts` was only incremented on the spawn path, so
   it never gave up. A raising pass now spends an attempt and backs off
   (1s→30s), so a permanent error gives up like a dead monitor.
2. **A raising `build` leaked a registry slot for ever.** `_release` was reachable
   only on the success path, so a `Popen` that failed under the lock left the
   reservation counted: the cap then refused work the machine could do, and
   nothing pointed at the failure. Released before the re-raise, in the same
   `try` that already documents "a failed prepare gives the slot back".
3. **`GenerationCounter.value` was the one writer that skipped the lock.** The
   setter writes the field `claim()` read-modify-writes, so a planted generation
   could land between the read and the store and two turns would share a number —
   the collision the class exists to prevent. Tests are the only caller today;
   the lock makes that an invariant rather than a convention.
4. **The doctor died on a snapshot that filled only some sections.**
   `snap["audio"]`/`["compositor"]`/`["ydotool"]`/`["ollama"]` raised
   KeyError, against `DoctorDeps`' own documented contract ("a partial deps object
   still produces a coherent report"). Now `.get(...) or {}`, so a section the
   host did not fill reads as no-reading — and the doctor is what you run when
   something is already wrong.
5. **A `BaseException` during a support-module load left a half-executed module
   in `sys.modules` under both names**, for the life of the process, so every
   later load adopted the corpse instead of the file. The app-module loader
   already caught `BaseException`; this is the support-module twin.
6. **`handsoff-restart` faked success with an interpreter that cannot run.** The
   launch is `nohup ... &` and its pid is echoed whether or not the process
   survived, so a typo in `HANDSOFF_PYTHON` stopped a healthy bubble and left
   nothing running while printing "handsoff restarted (pid N)". Checked before
   anything is killed; the scan's *"command injection"* framing is wrong (the
   value is quoted everywhere, so metacharacters are one unusable path).
7. **`hardware.first()` handed a non-record back to its `.get()` callers**, so
   fastfetch answering one type with a scalar (or a list of scalars) raised
   `AttributeError` inside the section — a machine that is merely unusual read as
   a crashed probe. `strang()`'s `v or ""` was the same shape for a falsy 0; both
   tightened. Stated limit: no current caller passes a falsy numeric to `strang`,
   so that half has no mutation behind it — it removes a trap, not a live bug.

Also hardened with the measured reason written at the site: `persist_setting`
and `coerce_setting` now deep-copy the containers they hand to coercion (MEASURED:
all five nested coercers replace their container rather than writing into it, so
neither writer was observably mutating anything — the copy removes the dependence
on that staying true); `os.scandir` iterators in `core/theme.py` are closed
without breaking the injected-listing seam the suite uses; the GitHub workflow
pins `permissions: contents: read`, cancels superseded runs and bounds every job
with a timeout; and the GitLab `order` job's second run no longer overwrites the
first one's junit report.

**Refuted, with the mechanism rather than an opinion:**

- **`settings_schema` `also=` (labelled `[bug · critical]`).** `also=(tuple(...)
  if … else ())` is *grouping parentheses*, not a tuple — so the first state's
  field gets a FLAT three-key tuple and the others get `()`. `also_pairs()`
  unpacks exactly two per entry and the module would not import otherwise; the
  scan's "tuple containing a tuple → too many values to unpack" would fail at
  collection, and 1899 tests collect.
- **`.coveragerc`'s `*/site-packages/*` omit.** Narrowing it to
  `*/.venv/**/site-packages/*` would stop matching this machine's real layout
  (`~/.local/lib/python3.14/site-packages`, where `perth`/`diffusers` live) and
  CI's `/usr/local/lib/python3.x/site-packages` — reintroducing exactly the
  phantom-file pollution the comment explains.
- **The GitLab pip-cache key.** `PIP_CACHE_DIR` is pip's *download* cache:
  content-addressed and selected by interpreter tags, so a 3.12 wheel is never
  handed to a 3.13 job. The scan compares it to a venv or a `--find-links` cache.
- **`sys.stdlib_module_names` on Python < 3.10.** The project's floor is 3.12
  (CI matrix 3.12/3.13; the unit runs 3.14), so the fallback it asks for cannot
  be reached.
- **`_allowed_dirs()` caching the resolved dirs at import.** The suite rebinds
  HOME per test on purpose; a module-level cache would poison every later test
  with the first sandbox's home — the opposite of the property the sandbox tests
  exist to hold.

**Noise, judged and left alone (not worth a diff, with the reason):** the
`except Exception: pass` volumes in the doctor's probes (the degraded state is
already printed in the line itself, so a debug log adds nothing the doctor's own
output does not say); `quarantine_file`'s pid+timestamp suffix vs a uuid (the
same-second collision it worries about is already closed by the pid, and the
suffix stays readable); `load_settings`'s JSON round-trip vs `deepcopy` (it also
normalises tuples to lists, which is what the file format needs); the
`_STDLIB_NAMES`/`__import__` fallback popping a foreign module (removing it would
silently flip `bind_bare` for the next load — a behaviour change with no
observable benefit); and `tests/fake_ollama.py`'s malformed-request handling (a
test double whose caller is the suite itself).

## The re-audit after the fix rounds: four real defects, and five claims that were noise (2026-09-19)

Everything below was measured on the tree as it stood at `58b71f9`, with four
independent instruments, because a repeat of the same scan that produced the
last round's list would mostly repeat its findings:

1. **ruff by rule, not by count** — 969 findings, but the density is the story:
   `UP037` (quoted annotations), `I001` (import order) and `F401` are style, and
   the rules that can name a defect were read one by one (`B023`, `PLR0124`,
   `RUF013`, `PLW0602`, `SIM115`, `DTZ*`, `S110`, `PLW1510`).
2. **an AST sweep for shapes no rule covers** — identical arms on both sides of
   a ternary, identical if/else bodies, `assert` outside tests, duplicate dict
   keys, and handlers that swallow a `BaseException`-wide error with no log.
3. **an adversarial probe of the live predicates** — `_validate_command` and
   `denied_secret_path`, driven with 23 command forms and 14 paths.
4. **the running bubble's own journal** — 3 402 lines, because a real failure
   that has already happened on this machine beats any amount of reading.

### Real, fixed, each with a guard that fails on the broken code

**The typing selftest's FAIL row named no reason.** `run_typing_selftest` read
`str(out) if not err else str(out)` — both arms the same expression — so a
failed `type_text` landing check printed the text a *success* produces, and the
refusal that caused it was dropped. This is the one row a person reads when
typing is broken, and it said `typed …`. The `err` half is a bool (the failure
flag `ToolResult` carries), so the useful half is the tool's own text plus the
`kind` that separates `refused` from `error`. Guard:
`tests/test_ops.py::test_selftest_failure_names_the_reason`, which drives the
whole stage with a fake window world and asserts the reason is IN the row.

**A brain worker could write its result into the next round's turn.** `_brain_turn`
runs `for _round in range(MAX_TOOL_ROUNDS)`, and `turn`, `tools` and `box` are
assigned once per pass. Both worker closures read those names from the enclosing
scope, and a closure reads the NAME, not the value — so a worker that outlives
its round writes `turn.result` into whatever object the next pass put there.
That window is not hypothetical: the journal carries `stream worker slow to
finish; waiting` twice (2026-09-18 21:18 and 21:23), and the code's own comment
says the worker may still be finishing after 32 s. The PTT path already avoided
this shape (`def _work(_rec=rec, _gen=gen, …)`); the two brain workers now match
it. Guard: a **new source-shape property** in
`tests/test_regression.py::TestTheScansShapesAreCheckedProperties` — a `def`
inside a loop that reads a name the loop assigns is refused unless it is bound as
a default, with a `scanned >= 3` self-check so a sweep that finds nothing is a
broken sweep rather than a passing one.

**A dead NaN term that read like a safety net.** `pack_animation` refused
`not MIN <= fps <= MAX or fps != fps`. The second term can never be the reason:
every comparison against a NaN is False, so the bounds already refuse `nan` and
`inf`. Kept as a test rather than a comment, because a manifest is somebody
else's file and Python's `json` parses both `NaN` and `Infinity` into `float`s.
Guard: `tests/test_design_packs.py::test_a_non_finite_fps_is_refused_by_the_bounds`,
which also installs an in-range animation so the check cannot pass by refusing
everything.

**A `global` with nothing to assign.** `_stop_recorder_bounded` declared
`global _MIC_OPERATION_OWNER` while only the nested `_call` assigns it; the outer
statement was a no-op that read as if the outer function owned the transition.
Removed. Also `search_note`/`reader_note` typed `now: float = None` while the
body branches on `None` — an annotation that lies to every reader and checker;
now `float | None`.

**Mutation sweep: 4/4 caught, 0 missed, every restore verified green** — the
selftest row back to naming nothing, each worker unbound again, and the fps
bounds replaced by the dead term.

### Refuted — five things that look like defects and are not

- **`cat` can read `~/.config/handsoff/settings.json`, and that is by design.**
  The probe flagged it, then the code refuted the probe: `read_file` refuses only
  `denied_secret_path` matches, and settings.json is the app's own configuration,
  not a credential. What is refused is *writing* it (a different predicate,
  `_classify_edit_path`), because it also carries the permission switches. The
  probe's expectation was wrong, not the validator.
- **`PLW1510` (45 `subprocess.run` without `check=`) is already handled at the
  call site.** Every shipped site either returns/examines `.returncode`
  (`nvidia-smi`, `git`, `fastfetch`, `mpc`, `pgrep`, `systemctl`) or raises on a
  non-zero exit (`hardware.cmd`). The rule flags the *absence of the argument*,
  not the absence of the check — turning it on wholesale would rewrite working
  code into `check=True` plus try/except for nothing.
- **Naive datetimes are normalised downstream, deliberately.** The 13 `DTZ*`
  sites build local-time windows and local-time displays (`upcoming_events`,
  `_today_events_summary`, `_fmt_when`, `_next_occurrence`), and
  `ics_events_from_text` converts a naive window with `.astimezone()` in its
  first three lines — the aware-vs-naive comparison that would raise is not
  reachable. The all-day DATE branch does the same before comparing.
- **`except Exception: pass` at 56 sites is teardown, not swallowing.** Read one
  by one, they are `t.cancel()`, closing a stream at shutdown, terminating a
  scratch process, a best-effort desktop notification, a timing callback. Each
  is a failure whose answer is "carry on", and several sit in `finally` blocks
  where logging would be the only alternative.
- **The hardware prober's `niri_windows` returning a `CompletedProcess` while
  every other prober returns a string is handled, not a leak.**
  `_compositor` checks `.returncode` on exactly that object before parsing.

### Stated limits

The AST sweep judges a handler's `except` type and whether it logs, so a narrow
handler that swallows something meaningful still passes; the closure rule is
lexical (it does not follow a worker's lifetime); the probe exercised the
validator and the secret-path predicate, not `edit_file`'s path classifier,
whose own tests already cover it; the two `dt`/`_dt` annotation fixes have no
mutation behind them because nothing at runtime can observe a type hint; and the
journal evidence is from this machine's session on 2026-09-18, so the "slow to
finish" window it documents is the shape that was fixed, not a reproduction of a
lost turn.

## The closure rule turned inward: the suite, and the two shapes the rule could not see (2026-09-19)

The rule written in the previous round swept the SHIPPED source. The obvious
next question is whether it holds for the thing that runs it, and answering that
found more wrong with the RULE than with the suite.

**The suite is clean, and that is a measured statement rather than an
assumption.** 33 test files, 548 loops, and 9 closures defined inside a loop. All
nine read nothing the loop rebinds, or have it bound already: three
`slot.commit(lambda key: None)` lambdas that read nothing at all, two
`_idle_release` lambdas reading `idle`/`queue` (rebound nowhere — the loop binds
`call`), a `_confirm_handsfree` lambda whose `seen` is now bound at definition
(this round's predecessor), `hook=lambda reason, a=answer: a`, and a
`lambda enabled: None` reading only its own parameter. The rule reports 0
offenders, and the sweep's own subject count is pinned (`scanned >= 9`) so a rule
that quietly stops finding the nine cannot pass as a clean suite.

**But two shapes were invisible to it, and both are the shape it exists for.**

1. **A loop's own TARGET was never counted as a rebinding.** `rebound` came from
   `_assigned_in(loop)` — assignments in the BODY — so `for action, expected in
   CASES:` bound nothing the rule knew about, and a closure reading the ITEM
   variable, the most ordinary thing a loop closure does, looked safe. Every
   `for x in …` in the tree was exempt. This is exactly the shape my own
   throwaway sweep had, and the reason the first `0 hits` it printed was not
   trustworthy until a planted sample proved it could see anything at all.
2. **Comprehension scopes were not scopes.** `[lambda: i for i in range(3)]` is
   the classic late-binding error — `i` is rebound per element — and the rule
   walked past it because a comprehension is not a `for` statement.

Both are fixed, and the fix is pinned by a **planted sample**, one shape per
scope, whose exact offender LINES are asserted rather than their count: a count
would have been satisfied by the two body shapes while the comprehension branch
sat disabled. That was not hypothetical either — the first version of this very
assertion let a mutation disabling the comprehension branch stay GREEN (measured
2026-09-19), which is the same class of mistake the rule is about: a check that
cannot fail is not a check.

**Teeth: 4/4 mutations caught, 0 missed, every restore verified green** — the
shipped brain worker unbound again; a SUITE lambda reading the loop's `seen`
unbound again (the case that motivated the sweep); the rule reverted to body-only
rebinding; and the comprehension branch disabled.

**Stated limits:** the rule was lexical, so it judged a closure by the names in
its source, not by whether it actually outlives its iteration — a `def` called
immediately inside the loop was flagged too, which is the safe direction. Two
details in this paragraph did not survive the next round's measurement and are
corrected by the section below (2026-09-19): the sweep's floor `scanned >= 9` is
now 10, and the shipped tree does not have "three subjects" — it has twelve
closures inside loops, and the PTT worker is not among them (it is defined in
`finish_listening()`, not in a loop), so the two brain workers are pinned by name
instead. The suite sweep still covers `tests/*.py` only, not the two harness
modules in `ci/`.

## The closure rule judges lifetime now: an escape is not a mention (2026-09-19)

Last round's rule answered a proxy question — *does this closure SIT inside a
loop* — so it refused a `def` invoked in the iteration that defined it just as
readily as a worker handed to a thread. Here is the sharper rule. It asks the
question the docstring already claimed: **can this closure outlive its
iteration?** Five ways out are recognised, and each is diagnosed in its own
words, because "one branch covering for another" would mean one of them is dead:

- **stored** — `obj.cb = lambda: extra` is `stored on obj.cb`;
- **handed to a callee that keeps it** — `sink.append(lambda: item)` is `handed
  to append()`, `threading.Thread(target=lambda: item)` is `handed to Thread()`;
- **bound to a name that leaks** — `f = lambda: extra2` … `hold.append(f)` is
  ``bound to `f` and `f` is read as a value``, a named `def` returned by its name
  is `` `worker` is read as a value ``;
- **collected** — a container literal holding it;
- **a comprehension's element** — `[lambda: i for i in range(3)]`, one closure
  per element, because `i` is rebound as the comprehension runs.

And the one shape left ALONE: a closure **called in the place it was written** —
`(lambda: item)()`, `sorted(…, key=lambda k: item)` — because the names it closed
over are still the loop's at that moment. The callees that consume what they are
given are listed (`CONSUMING`); everything else is assumed to KEEP it, which is
the safe direction: the cost of the assumption is an `x=x` default, and the cost
of the other is a worker writing into the next round's object.

**The sample was lying, and the rule was fine.** The planted sample marked its
escapes with trailing `# FLAG` comments — which are *Python comments*, not part
of the string being parsed — so `CLOSURE_SAMPLE_FLAGS`, read back out of the
text, was **EMPTY** while the rule was working perfectly. A sample with an empty
expectation accepts any rule at all. The verdict now travels WITH each line: a
tuple of (source line, the word its reason must name). The assertion compares the
flagged LINES, then asserts each line's REASON contains that word, then asserts
every unmarked line was left alone — so a branch answering in another branch's
wording fails, and a rule that flags an in-place call fails.

**A count floor was standing in for a subject.** The shipped half asserted
`scanned >= 3` and its message called the subjects "the PTT worker and the two
brain workers". Neither half of that survived measurement: the sweep judges
**12** closures inside loops across the shipped files, and the PTT worker is not
one of them — `_work` is defined in `finish_listening()`, not in a loop, so the
rule has never seen it. The idiom this rule came from was never one of its
subjects. `_closure_offenders` now returns the closures it judged **by name**,
the shipped half requires `_run_stream` and `_run_call` by name, and the suite
half's floor moved 9 → 10 because one of its subjects lives in a comprehension —
a scope the rule could not see two rounds ago.

**Teeth: 8/8 mutations caught, 0 missed, every restore verified green** — every
closure called in place (rule blind to escaping); an in-place call treated as an
escape; the stored-on branch answering with the handed-to word; the name-following
branch killed; the sample's own verdict moved onto a line its reason no longer
matches; a shape dropped from the sample; the shipped brain worker renamed out of
the pinned subjects; and the shipped brain worker unbound again (which the rule
now reports as `` `_run_stream` is read as a value ''`, naming the route out
instead of merely objecting).

**Measured on the final bytes:** `tests` **1 920 passed in 252.8 s**; `coverage`
**85.29%** (2 542 missing of 17 275) ≥ 70; freshness **19 passed**. The count
stays 1 920 because this round sharpened existing checks rather than adding a
test. **No deploy and no gate run:** no shipped file changed — the whole round is
`tests/test_regression.py` plus the two spec surfaces — so `--ptt doctor` still
reads `deployment: in-sync` on running `134c3c8661cc…`, and the full gate set is
the next step's work rather than this one's.

**Stated limits:** the rule still judges lifetime from the TEXT — a closure
handed to an unknown callee that happens to consume it synchronously is refused
(conservative, and the reason `CONSUMING` exists as an explicit list); an escape
through an unbound name that the loop does not rebind is out of scope, because
the rule only asks about names the loop rebinds; and the suite half still covers
`tests/*.py` only, not the two harness modules in `ci/`.

### The callee is read now, not assumed (2026-09-19)

The follow-up to that rule: it knew a closure handed to `sorted` is consumed
(because `sorted` is on a list of builtins) and assumed **every other callee
keeps what it is given**. That assumption is safe but expensive — it demands an
`x=x` default from a helper that calls its closure and forgets it.

So the rule now *reads* the callee when it can. `_definition_of` resolves the
call target by **unique name** in the same file — a `Name` (`take_it(...)`) or an
`Attribute` (`Holder().commit(...)`, which is a METHOD, so its positional
arguments are bound past `self`). `_parameter_for` binds the closure argument to
the parameter it lands in, and refuses to guess through a `*args` splat or a
`*fns` parameter, where no binding can be read. `_consumes` then walks the
callee's body: **every use of that parameter must be a call in the callee's own
body**, and `_directly_in` is what distinguishes `def take_it(fn): fn()` — used
during the call — from `def later(fn): Timer(1, lambda: fn()).start()`, called
from a worker that outlives it. Anything unreadable (an external callee, a
splat, an ambiguous name) keeps the old answer: assume it keeps it.

**What the follow finds in this tree, measured:** 73 closures are passed as an
argument to a call whose target is a unique def in the same file; **7 of them are
called in place** — every one of them a `hardware._probe("section", lambda: …)`
prober, and `_probe` does call `fn()` in its own body (line 156) — and 66 are
kept or handed on. 149 def names in the tree appear more than once in their file,
so they are never followed. **None of the seven is currently a subject of the
rule**, because they sit in `snapshot()`'s `try`, not in a loop: the follow is
what the *next* call site gets, and the sample is where it is pinned today.

**The sample grew the other half**, seven shapes that each fail a different way
if the follow is wrong: `take_it` (called in place → left alone), `Holder().commit`
(a method, so the argument binds past `self` → left alone), `keep_it` (stores the
parameter), `later` (calls it from a nested worker), `anyhow(*fns)` (a binding
that cannot be read), `pick` (defined **twice**, so the name is ambiguous and not
followed at all) and `both` (calls it *and* keeps it — the case that must not be
read as consent from the happy half). Every one of those is one mutation away
from passing, which is why they are lines in the sample rather than arguments in
a comment.

### A wrapper that only passes the closure ON is followed too (2026-09-19)

One level of indirection was still a false escape: `def through(fn): take_it(fn)`
was read as a store, because the parameter's use is an argument of another call
rather than a call of it. It is a *pass-through*, and the honest answer is the
one its target gives: the wrapper consumes the closure exactly as far as
`take_it` does. So a use that hands the parameter on is now followed with the
same code, from the callee's own body only — from a nested scope (`threading.
Timer(1, lambda: take_it(fn))`) the call outlives the wrapper and the refusal
stands. The `chain` of (callee, parameter) pairs already walked makes a cycle of
wrappers (`ping` → `pong` → `ping`) a refusal instead of an infinite descent,
and a wrapper whose target KEEPS the closure is refused just as it was.

**Measured in this tree: no such chain exists yet.** Of the closures handed to a
readable callee, 7 are called in place, 66 are kept or handed on, and **0 arrive
through a pass-through** — so, like the follow itself, this half is pinned by the
sample today and is what the next wrapper inherits. The sample grew four shapes
that each fail a different way: `through` (passed to a consumer → left alone),
`relay` (passed to a *keeper* → refused), `handoff` (passed on from a nested
worker → refused) and `ping`/`pong` (a cycle → refused), plus `stash`, whose
parameter is kept by an **assignment** rather than a call.

**Teeth: 10/10 mutations on this half caught, 0 missed, restoring green** — the
follow removed (back to assuming every callee keeps it); every readable callee
assumed to consume; a call from a nested worker read as an in-place call; an
ambiguous name followed anyway (first definition wins); a method's argument bound
WITHOUT skipping `self`; the first in-place call taken as consent with a later
store ignored; a parameter kept by an assignment read as an in-place use; a
wrapper that only passes the closure ON refused as a keeper; the cycle guard
removed; and the `chain` not threaded into the recursion. The last two fail
loudly (`RecursionError`) rather than quietly, which is the guard doing its job.
One of the ten was **unfalsifiable before `stash` existed** — no sample shape kept
a parameter without a call, so the mutation stayed green for the right reason (it
had nothing to break), which is why the shape was added rather than the mutation
dropped. Together with the 8/8 on the lifetime rule: **18/18, 0 missed**.

**Stated limits of the follow:** resolution is by unique name only, so a helper
whose name is defined twice in its file is never followed (conservative: a
needless default, never a missed escape); it reads the callee in the SAME file,
so a helper imported from another module is assumed to keep its closure; a
`*args`/`*fns` binding is refused rather than guessed; a cycle of wrappers is
refused rather than resolved (there is no fixed point to find, and refusing is
the direction that cannot miss an escape); and — **corrected here, the point of
the section above** — a parameter that is only ever *passed on* is no longer read
as kept: it is followed through, so the limit this section used to state as
"the rule reads one level, deliberately" is closed; and
an object's method reached through an attribute is resolved by name only, so a
`commit` defined once in a file with 30 classes is followed — correct here, and
worth knowing if a name like `run` ever becomes unique by accident.

## The closure rule follows the helper into its own module — the root bound is enforced where the file is read (2026-09-19)

**The limit this closes was stated by the section above in so many words:** the
follow read the callee's body only when the def was in the SAME file, so `from
harness import take_it` — a helper that calls its closure in place — was
indistinguishable from `threading.Thread`, and a loop was told to bind a name it
can never read again. The rule now resolves across the files of this checkout.

**How the follow crosses a file.** One parsed-module object (`_Source`) carries a
tree, its OWN parent map and its path — and the parent map is not decoration:
`parents[node]` is only meaningful for the tree the node was parsed out of, so
the walk into a callee in another file must use that module's map, and a mutation
that uses the scanned file's map instead is caught (the argument's use resolves
to nothing there, and the callee is read as a keeper). `_module(path, root)` is
the ONE place a file is read and the ONE place the root bound is enforced: a path
outside the checkout — or a name that only exists in an installed library —
returns nothing and the call is not followed. A single expression, deliberately:
a second copy of the bound further out could never be falsified on its own, and a
check nobody can fail is not a check. `_module_path` tries two homes, because
both are how this repository imports itself — the importing file's own directory
(`tests/` do `from conftest import …`; a package does `from .theme import …`)
and the checkout root (`core/assistant.py` does `from core.registry import …`) —
and a RELATIVE import counts its levels up from the importing file's directory
(`from ..harness import take_it` in `pkg/sub/deep.py` names `pkg/harness.py`).
`_imports` builds `{name in this module: (file, attribute or None)}` from every
`Import`/`ImportFrom`: `import core.brain` binds both `core.brain` and `core`,
`import harness as h` binds `h`, `from twice import tap` binds `tap` →
`(twice.py, "tap")`, and a star import or a name that is no file here is simply
absent — which the caller reads as "assumed to keep what it is given". A dotted
CALL name is tried longest first (`h.take_it` → the module itself, then `h`), so
the most specific module wins.

**That a name is imported is not yet an answer** — which is what `_imported_def`
exists for: `from core.registry import BoundedRegistry` binds a CLASS, and
looking for a def of that name finds none, so a class, a constant or an imported
lambda declines to be followed exactly as it should. An attribute binding is
followed only when the imported module defines that name **exactly once**; defined
twice leaves the call ambiguous and it is not followed at all, because guessing
the wrong body is how this rule would start missing escapes.

**And one hole was found while writing it.** The import table is built from every
`Import` node in the module, so a name imported and then REBOUND at module level
(`from harness import take_it` … `take_it = keep_it`) was still resolved to the
imported body — a verdict about a function nobody calls, and in the unsafe
direction: the imported `take_it` consumes its closure, so a call that actually
runs a KEEPER would have been left alone as consumed. Names rebound at module
level (assignment, annotation, or a module-level `def`/`class` of the same name)
are now dropped from the table, and the call falls back to "assumed to keep" —
the direction whose cost is a needless `x=x` default. Lexical like the rest of
the rule, and stated: only module level is checked, so a rebinding inside a
function is not seen.

**The sample is now real files parsed from real paths.**
`test_an_imported_helper_is_read_instead_of_assumed` writes a package under
`tmp_path` — `harness.py` (`take_it` calls its closure, `keep_it` stores it),
`twice.py` (a `tap` defined twice) — and four consumers judged through the same
helper the shipped sweep calls, so the machinery under test is the machinery that
runs. Each part fails a different way if the follow is wrong: the consumer of
`take_it` must be left alone while the consumer of `keep_it` must be flagged
(`(2, [5])`); the ALIASED module (`import harness as h`, then `h.take_it(…)`)
resolves; a name defined twice in the imported module is ambiguous and not
followed; a name rebound at module level is not the import any more and the call
is judged as kept; the RELATIVE import two levels up resolves; and the SAME
consumer file judged with a root that does not contain the helper resolves
nothing — both closures assumed kept — so the follow never leaves the root it was
handed.

**Measured reach in this tree: no such chain exists yet.** The shipped sweep
judges **12** closures inside loops and resolves **0** call sites across a file
boundary, so this half is pinned by the sample today and is what the next
imported consumer inherits. What IS pinned in the shipped and suite sweeps is
that the import table still reads this repository: the suite half requires **≥ 5**
`from … import …` names to resolve to a file in the checkout — measured, **151
of 185** bindings across 33 test files (the shipped tree: 6 of 23 across 21
files) — a floor rather than a count, so a rename can starve it visibly instead
of silently.

**Teeth: 13/13 mutations caught, 0 missed, every restore verified green** —
eleven on the cross-module half (the root bound dropped so a file outside the
checkout is followed too; a relative import resolved against the root only;
`import x as y` no longer binding `y`; an ambiguous name followed anyway;
imported names never resolved at all; the follow dropping the root so no callee
outside the file resolves; the cross-module walk using the SCANNED file's parent
map; a name rebound at module level followed anyway; a wrapper that only passes
the closure ON read as a keeper; the cycle guard removed, which fails loudly as
`RecursionError`; and a callee that calls its closure in place read as a KEEPER)
plus two controls proving the refactor did not blunt the earlier verdicts (a
shipped brain worker unbound again; comprehension scopes not scopes again).

**Stated limits:** the import table is lexical — a name imported and rebound
inside a FUNCTION is still resolved to the import; a star import is not read at
all; a name defined twice in the imported module is refused rather than guessed;
the same-named file in the importing file's own directory wins over the checkout
root (the order this repository's two import styles require); and everything
outside the checkout is assumed to keep what it is given. All of them are
conservative in the same direction — a needless `x=x` default, never a missed
escape — except the function-level rebinding, which is stated as a hole rather
than dressed up.

**Measured on the final bytes:** the `tests` gate **PASS — 1 921 passed in
257 s** (1 920 before: this round adds the imported-consumer test); `order`
**PASS**, seed shuffle **1 921 in 261 s** and file-order shuffle **1 921 in
260 s**; `coverage` **PASS, 85.29%** (2 542 missing of 17 275) ≥ 70; `compile`,
`shell` and `smoke` PASS; `clean-checkout` **PASS** (freshness **19 passed in
3.2 s**); `two-writer` **PASS**, the worktree unchanged through the run. And the
`specs/60-test-plan.md` row for `test_regression.py` moved 153 → 154, because
this round does add a test rather than sharpen one.

## The OCR scan triaged against the source: twenty-one fixes with guards, and every other finding written down with the reason it is not work (2026-09-20)

**The scan** is the offline reviewer's pass of 2026-09-20 — 231 comments across
32 files, every file it read handed a verdict whether or not it found anything.
This round read each finding against THIS tree, and where a finding was about a
Python API rather than about this code, against CPython itself: the
checkout-write guard's event→arguments map was checked by installing an audit
hook and reading what CPython really passes to it (`os.mkdir(path, mode,
dir_fd)`, `os.remove(path, dir_fd)`, `os.symlink(src, dst, dir_fd)`, and a
`tempfile.mkstemp` whose sole argument IS the created path). Twenty-one findings
had teeth and are fixed, each with the guard that now holds it. **The rest are
dismissed here, one line each with the reason** — a dismissal is a claim about
the scanner's model of this code, and it is worth exactly as much as the reason
somebody wrote down for it.

### What had teeth, and what holds each one now

| where | what was wrong | held by |
|---|---|---|
| `core/calendar.py:296` | `COUNT` was checked only on the WEEK loop, so a rule whose cap lands mid-week kept emitting the rest of that week: `FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=4` produced **six** instances, materialising meetings the rule itself says do not exist (verified) | `tests/test_calendar.py` `test_a_weekly_count_stops_mid_week` |
| `core/calendar.py:359/399` | the same mid-batch cap missing from the MONTHLY and YEARLY candidate loops | `test_a_monthly_count_stops_mid_month`, `test_a_yearly_count_stops_mid_year` |
| `core/tools.py:547` | `denied_secret_path` judged only the RESOLVED path, which is the one thing a symlink defeats: `id_rsa -> /tmp/key` resolved to a name no rule knows, so reading a private key was ALLOWED — the docstring promised "symlinks cannot slip past" and the code let exactly that through. Both directions are now judged (the name asked for, and what it resolves to) | `tests/test_policy.py` `test_predicate_denies_a_secret_symlinked_away` (a secret symlinked OUT, and a plain name resolving INTO `.ssh`) |
| `core/tools.py:1126` | the 60 s rate window is a read-modify-write, and `execute` really does run on two threads at once (a barge-in starts the next turn while the old worker finishes), so two callers meeting inside the check both passed the same last slot | `TestToolRateLimit::test_two_callers_meeting_at_the_last_slot_admit_one` (the interleaving STRETCHED — the stamp sleeps 200 ms — so it fails on every run without the lock, not sometimes) |
| `core/settings.py:858` | `Settings.__setitem__` was `self.persist(...)` and dropped the False on the floor: `obj[k] = v` read as done while the disk still held the old value | `tests/test_settings.py` (the failed-write test gained the dict-protocol half) |
| `core/settings.py:458` | `_coerce_tool_call_times` KEPT any list it found — the one shape its own docstring names as discarded. Nothing in the app ever writes the key, so every list on disk is hand-edited by definition | `test_tool_call_times_is_runtime_state_never_a_user_list` |
| `core/audio.py:156` | `stream.close()` sat in the same `try` as `stream.stop()`, so a `stop()` that raised skipped the close — leaking the PortAudio stream whose reference had already been dropped, on a device that had just disappeared | structural; no separate test (below) |
| `core/brain.py:453` | `full += piece` per spoken sentence — quadratic in response length, and `sentence`-sized chunks make that a real O(n²) on a long answer | the streaming tests (behaviour unchanged: one `join`) |
| `core/theme.py:198` | `raw[:6]` sliced BEFORE stripping the `#`, so a prefixed `#rrggbb` became `#rrggb`, failed the shape check, and read as "no luminance" — every wallpaper-tuned look silently stopped tuning | `tests/test_theme.py` `test_sample_luminance_accepts_a_hash_prefixed_hex` |
| `core/theme.py:73` | `_retune` was annotated `-> tuple[int, int, int]` while returning floats | (annotation only) |
| `hardware.py:348` | `any(wdir.iterdir())` counted a directory holding only zero-byte or half-downloaded files as a CACHED whisper model — "cached" in every status line while the first spoken turn failed. The flat whisper dir skipped `_snapshot_cached`'s non-empty-file rule, and the same `iterdir` sat outside the per-entry swallow | `tests/test_hardware.py` `test_a_zero_byte_whisper_file_is_not_cached` (+ the non-empty control) |
| `core/doctor.py:425` | the ydotool probe called the HOST's `socket_connectable` outside any guard: a probe that raised took the whole doctor down — in the tool somebody runs precisely BECAUSE something is wrong. The niri probe beside it was already held to the rule | `tests/test_fault_injection.py` `test_a_raising_probe_is_a_line_not_a_dead_doctor` (which also asserts the rest of the report still happened) |
| `ci/compile_all.py:45` | a missing root (and a root that is a file) fell through to discovery and reported the same "no Python sources" a genuinely empty tree gives — a refusal that named the wrong problem | `tests/test_ci_summary.py` `test_a_missing_root_is_named_as_the_root`, `test_a_root_that_is_a_file_is_named_as_such` |
| `ci/spec_tables.py:39` | `_install_var` returned `[]` for a declaration it could no longer find, so a renamed or requoted variable would silently shrink the architecture table — and a freshly `--write`n spec would agree with itself while missing modules | the freshness gate: the generator's output is compared with the specs, and a shrunken table is STALE |
| `ci/spec_tables.py:153` | `render_tools` rewrote the `**N `@tool` methods**` prose with `sub` and never checked the sentence still existed — reword it and the count goes stale with nothing failing | `subn` + refusal (its mutation cannot be falsified today: the sentence IS there — stated, not dressed up) |
| `ci/worktree_stamp.py:184` | a `git diff`/`ls-files` that FAILED in a tree that HAS a HEAD was read as an empty diff and an empty untracked list — the false "nothing moved" this gate exists to refuse | `tests/test_ci_two_writer.py` `test_a_git_failure_in_a_headed_tree_is_unjudgeable` |
| `ci/worktree_stamp.py:262` | `_moves` validated only the BEFORE snapshot's fields, so a truncated `after` read as every tracked file vanishing — or as "nothing moved", depending on which side lost them | `test_a_truncated_after_snapshot_is_not_a_pass` |
| `ci/worktree_stamp.py:466` | a clock that went backwards (NTP, a manual change) made every offset and fraction in the WHEN lines a lie | `test_a_backwards_clock_gets_no_timing_lines` |
| `tests/checkout_guard.py:120` | a second AGREEING `install()` added a SECOND audit hook (hooks cannot be removed, and every guard above it passes — which is exactly the case the duplicate landed in) | the flag, exercised by the suite's own install path; no separate test (below) |
| `core/tools.py:3053`, `core/audio.py:281` | two names written and never read again — `JOB_ANNOUNCE_S` (superseded) and `_TTS_FLOAT32_PATCHED` (which an early return on it would have made WRONG: a reloaded model needs the patch again) | removed; no test (a dead name has no behaviour to pin) |
| `core/brain.py:172` | the unload-failure journal line called a failure a SKIP — the docstring beside it says the call was made and failed, and the wording sends the reader looking for a policy that declined instead of a host that refused | (wording; the exception type was already logged) |

**Two fixes have no new test, stated rather than implied:** the audio stream
`stop`/`close` split (a leak on a device that vanished — the shape is the fix)
and the guard's `_INSTALLED` flag (an audit hook cannot be listed back, so the
only observable is "no second hook", and the suite installs once per process).
Both were verified by reading the code path, not by a red test.

**One mutation cannot be falsified at all:** removing the `subn`-vs-`sub`
check in `render_tools` changes nothing while the sentence it guards is still
in `specs/30-tools-api.md`. That is the honest state of a guard whose subject is
a FUTURE edit; it is here so the next person who rewords that sentence knows the
guard was deliberate.

**The generated tables had to move, and the freshness gate is what said so.**
Every fix above shifts line numbers in `core/tools.py`, `core/audio.py`,
`core/settings.py`, `core/calendar.py`, `core/brain.py`, `core/theme.py`,
`hardware.py` and `core/doctor.py`, and `specs/20-architecture.md` states each
module's size while `specs/30-tools-api.md` states every tool's line — so the
suite went red on `test_the_tables_are_what_the_generator_produces` before a
human noticed, and `ci/spec_tables.py --write` rewrote both. That is the gate
working, and it is why the ledger row below carries hashes for two files this
round never meant to touch.

### Dismissed, and why

**Pytest configuration.** `testpaths = tests` is the only test tree (the plan's
own rows enumerate `tests/*.py`, and a new directory would be a plan change
before it was a discovery change). The `PytestUnhandledThreadExceptionWarning`
filter needs pytest ≥ 7 and this environment runs 9.1.1, pinned in
`requirements-lock.txt`. The coverage-floor comment: `70` is stated in
`.coveragerc` AND in the CI job, and the coverage gate is what reads it.

**`ci/pytest_summary.py`, `ci/spec_tables.py`.** The digest's totals come from
junit's own suite attributes — pytest wrote them, and a recount from the cases
would be a DIFFERENT number, not a better one. `_short_classname`'s inputs are
dotted module paths (pinned by `tests/test_ci_summary.py:217`), never a path.
The `PRIVATE-TOKEN` header cannot surface in `HTTPError.__str__` (`HTTP Error
NNN: …`), and the note is posted as a HEADER, not a body. `_case_msg`'s `elif`
is unreachable — same `find`, same result. `module[:1] == ["tests"]` is the
list-slice idiom for a possibly-empty list. `ENV_HINTS` substring matching is a
HINT table whose two PortAudio entries are ordered by measured need (the
comment says which pipeline this came from). The `SystemExit` in `_table_body`
is this tool's failure idiom (it has no broader error contract), and the
whitespace/`strip('`')` findings describe tables this generator itself writes.
`tool_rows` walking the whole AST instead of `tree.body`: every `@tool` is a
METHOD of `ToolBelt` — a top-level-only walk would find ZERO and the census test
would fail immediately.

**`core/__init__.py`.** The loaders' singleton is enforced by
`sys.modules.setdefault` BEFORE the module executes, which the docstring calls
out as the mechanism (a second racer receives the winner's module); a lock would
serialise an import without adding a guarantee. `load_module`'s path candidates
all contain the file the literal name asks for, and every caller passes a
literal. The `_allowed_dirs` swallow is deliberate, and the fallback import's
`TypeError` catch is the refusal path (narrowing it turns a broken module into a
traceback instead of the named `ImportError`).

**`attic/patch_wakeword.py` (every finding).** `attic/` is a provenance
archive: excluded from the compile gate, from `.coverage`, and imported by
nothing in the tree (checked). Its defects are history, not surface.

**`core/audio.py`.** The cited f-string logging line already uses lazy
`%`-formatting (the scan's own text says so). The level-callback swallow runs
inside the PortAudio callback — a persistently failing UI metronome must not
flood the journal, and the meter is cosmetic. No lock-order violation exists:
`get_whisper` takes only `_whisper_lock`, and a reload under it serialises the
transcribers that were already queued behind it. The RMS allocation is a
1024-sample block per block. `wav_path` is built by `synthesize()` under
`STATE_DIR` and no model input reaches it. A corrupt reference clip is reported
by the engine at load, so the duration read is best-effort by design.

**`core/lifecycle.py`.** `next_turn` is called once per turn, not in a loop;
no caller passes a list (grep: none — the list branch exists for external
callers, and `counter[0]` is its documented shape); the value setter has one
documented test-only caller.

**`core/doctor.py`.** The text-vs-JSON cache asymmetry is deliberate and
commented ("Fresh cache: doctor must see live state" — the text path must read
the card NOW; the JSON/control path uses the TTL). The `Restart=` substring
match in the text path is tolerant ON PURPOSE (a commented or spaced
`Restart=` still means auto-restart is configured) while the JSON path keeps the
strict regex as the machine contract. `sys_version_info or sys.version_info`:
an empty tuple is FALSY, so the fallback already happens — the premise is
inverted. An empty `ollama_model` in a failure line is cosmetic; the setting is
validated at load.

**`core/brain.py`.** The once-per-process warning flag's worst case is one
duplicated log line, and the flag write is atomic under the GIL. The broad
`except` in the stream guard logs and RE-RAISES with the traceback intact. The
Korean characters in `strip_thinking` are the scan's own admitted display
corruption — the source has plain `think` tags. `state=None` is the documented
contract for a caller without a state dict (the host always passes one).

**`ci/worktree_stamp.py`.** A timeout and a failure now BOTH become the
cannot-judge shape, so conflating them no longer produces a verdict. The
checkpoint/rebase locking findings: `gates.sh` is serial by construction (one
run per worktree, per-run `mktemp -d` state), and a `FileExistsError` lock would
wedge the gate after a crashed run behind a stale lock — atomic writes alone do
not fix a read-modify-write, and inventing a lock here is how the gate starts
refusing runs for reasons that have nothing to do with the tree. The WHEN
lines' clock is the file's OWN mtime by design (the timing tests place writes
with `os.utime` against a chosen window); the snapshot's recorded nanoseconds
answer the different question (reverted-write evidence) and are already used
there.

**`core/assistant.py`.** The notification reader already backs off and spends
an attempt on the pass-raise path (that was the earlier full-CPU spin fix); an
explicit `kill` is a preference, not a property. The pomodoro announce callback
is injected by the host and does not re-enter the controller (the lock is an
RLock for the tests' command paths). `take_missed`'s swallow is deliberate and
commented ("hand back nothing and let the next tick fire them normally") —
re-raising would take the missed-reminder path down on a transient write error.
`ATTEMPT_BUDGET` is a documented ceiling on total restarts while enabled, not a
failure counter, and the health surface reports it. dbus-monitor prints only
string arguments, and `Notify`'s string order is app/icon/summary/body — exactly
the slice used (replaces-id is uint32). The mute check's fail-open is deliberate
and commented ("a bug in the check is a reason to say less, not more"). The
`dbus-monitor` argv is a list (no shell) and its quotes are match-rule syntax.
`update()`'s `None` sentinel is documented with a test speaking to it —
changing it is an interface break. Hour-scale repeats at sub-second tolerance
make the `//` exactness question moot, and the pomodoro snapshot dict is
replaced wholesale under the lock.

**`core/registry.py`.** The "corpse reservation" branch is unreachable given the
invariant the code enforces (entries + reservations ≤ cap, so a reclaimable
entry implies a free slot) — and the comment says exactly that. `_cancel`'s
`None` branch is never taken by its only caller, so no double decrement exists.
The read locks are absent because registries hold a handful of entries by design
(caps 4-8). `Offer.__len__` defines TRUTHINESS for an armed offer, which is what
the mapping reads; "number of fields" is not what the class means. Expiry on
read is the documented contract. A released-never-settled slot is the
documented state after a raising build.

**`core/settings.py`.** The JSON round-trip doubles as the serializability
check the typed contract relies on. The `loaded` shape check has no
undefined-name path (exceptions return first) — the outer check is the
quarantine path for valid-JSON-wrong-shape. The `BaseException` catch is
REQUIRED, not sloppy: cleanup must also run for `KeyboardInterrupt`, and the
repo has a checked property that loader rollbacks catch `BaseException`. The
cross-process lock is not reentrant because its two users (`write_settings`,
`persist_setting`) do not nest; making it reentrant would hide a real nesting
bug. `_three_way_merge` only ever stores values it has checked are not
`_MISSING`, and the tests assert no sentinel survives (disk data is JSON and
cannot contain one anyway). `calendar_ics.split` is correct (the scan says so).
`persist_setting`'s deep copy is deliberate and commented.

**`settings_schema.py`.** `load_settings` deep-copies the defaults on every load
and nothing mutates the module-level dict, so the "mutable default" surface does
not exist. A typo'd catalogue label fails `tests/test_settings_contract.py` — a
stronger guard than a runtime warning. `fields_by_key` is a dict comprehension
over ~90 rows on a tab redraw. `"str": "line"` IS in `KIND_CONTROLS` (the scan
misread). The catalogue's colours are all valid hex. `tts_reference` names its
control through `render=`, which is what the GUI dispatches on (and the settings
GUI suite passes). `RETIRED_SETTINGS` is applied on load AND write. The
`spotter_models` cap lives in its coercer, which is where the tip says it is.

**`hardware.py`.** The probe-failure race is bounded by `FAILURE_TTL` (≤ 5 s)
between two probes of the SAME section, and a CAS would buy machinery for a
self-healing case. The per-probe swallows are the design (sections are
independent, and a probe returns its own error string). `fastfetch`'s "produced
no output" is the diagnosis the caller can act on. The ydotool socket probe only
CONNECTS (sends nothing), so a false "reachable" is cosmetic, and
`XDG_RUNTIME_DIR` is already preferred over the compiled-in path.

**`core/web.py`.** The resolver-thread leak is a documented trade-off ("a
wedged resolver leaves one daemon thread behind per attempt; the timeout is
reported rather than hidden"), and the proposed `socket.setdefaulttimeout` is
process-global while not bounding `getaddrinfo` everywhere. The cache key cannot
collide: one component is normalised to a fixed backend name and the other is an
int, so no `|` can appear in either. The SearXNG probe is a 0.3 s connect on
localhost, on the doctor path. `html_to_text`'s broad catch is documented ("a
malformed page is not an error, just a short one"). The hop-seam fallback WARNS
(not silent) and the shipped host always injects the seam
(`handsoff.py:3914/3959`); the comment weighs reading nothing against one-shot
for a partial install, and that is the decision. The duplicated `traceback`
alternative in `_TECH` is harmless. `clean` comes from `_public_url` validation,
so the Jina URL cannot be steered. `read_results`' per-backend caps and
politeness are the contract. `_purge_locked` pops the oldest in a loop (no
recursion) and `or ""` already handles `None`.

**`core/tools.py`.** `_FLAG_WARNED`'s worst case is a duplicated once-per-process
warning. `_import_smoke`'s two proposed prescriptions contradict each other and
the current narrow catch is deliberate: only the failures this check can
diagnose become a reason string, and an import that raises on the module's own
terms is reported by its traceback, not by the smoke test.

**`tests/checkout_guard.py`, `tests/conftest.py`.** The guard is Linux-only
because the whole app is (niri, PortAudio, systemd) — the `/proc/self/fd`
resolution is the platform, not an assumption to abstract. The fail-open for an
unreadable event shape is deliberate and documented (a shape the guard does not
understand is one it does not judge); the mapped shapes are the ones verified
against CPython. The `os.environ` mutation is single-process by construction
(no xdist plugin is installed and `pytest.ini` adds none) and `os.environ` is
process-global by nature. The deepcopy fallback is documented, and the objects
that fail it are models and locks where identity IS the intent. The gc fold-up
is what finds unregistered listeners, and its cost is bounded by the listeners
that exist.

**`core/theme.py`, `core/calendar.py`, `tests/fake_ollama.py`.** An escaped
quote inside a niri wallpaper path does not appear in this config and the
parser's simple tokenisation is the config's actual grammar. `genexp` style. The
calendar's MONTHLY `k` IS initialised — `k = 0` sits once before the branch at
line 214 and is shared by every frequency (the scan's "UnboundLocalError" is a
misfire). `first_index` is only set under `if gap > timedelta(0)`. All-day
events materialising at local midnight is the design, and overlap uses the
event's duration. Reading the user's own `.ics` path from settings is the
feature, and the parser ignores non-ICS content. Negative durations are refused
on purpose (commented). A malformed `Content-Length` in the fake Ollama server
is not a supported input for a test double, and the crash is loud and inside a
test.

**Teeth measured, not asserted: 13/13 mutations caught, 0 missed, every
restore byte-exact.** Each fix was reverted in turn — the weekly `COUNT` cap
removed; the monthly/yearly mid-batch cap removed; `hexcol = raw[:6]` restored;
`any(wdir.iterdir())` restored; the secret guard resolving only; the rate
window unlocked; `_coerce_tool_call_times` keeping a list again; `__setitem__`
silent again; the doctor's probe unguarded (`raise` in its except); the root
check removed from `compile_all`; the worktree's git failure read as an empty
diff; `_moves` validating only BEFORE; and the backwards-clock guard disabled —
and each one turned exactly the new guard red. **A caveat worth keeping:** the
first attempt at this sweep was killed by the harness mid-case, and a SIGKILL
cannot run a `finally`, so `core/tools.py` was left holding the unfixed
`return _secret_reason(p)`. It was put back from the byte-exact backup, the diff
was read to confirm it, and that case was re-run (caught, restored, verified).
The restore discipline only works while the process can run its handlers; the
backup is what saves the run after a hard kill — which is exactly the shape that
bit the earlier round.

**Measured on the final bytes:** the `tests` gate **PASS — 1 936 passed in
252 s** (1 921 before: fifteen new guards); `order` **PASS** — seed shuffle
**1 936 in 261 s**, file-order shuffle **1 936 in 257 s**; `coverage` **PASS,
85.33%** (2 541 missing of 17 316) ≥ 70; `compile` **PASS** (54 files
byte-compiled); `shell` and `smoke` PASS; `clean-checkout` **PASS** (the
freshness guard **19 passed in 3.6 s** in a scratch worktree of HEAD);
`two-writer` **PASS**, "worktree unchanged since the run started". The plan's
per-file rows for the touched suites were brought to their measured counts
(they are not pinned, and two were already quietly stale before this round).

**This tree is now the running bubble.** Eight SHIPPED files changed —
`core/audio.py`, `core/brain.py`, `core/calendar.py`, `core/doctor.py`,
`core/settings.py`, `core/theme.py`, `core/tools.py` and `hardware.py` — so
the deploy this section originally recorded as owed was made on 2026-09-20:
`HANDSOFF_SKIP_SYSTEM_PKGS=1 ./install.sh` at 09:04, both stage gates passed
(byte-compile + schema import), the service restarted to PID 160114, and
`--ptt doctor` reads `deployment: in-sync — installed copy matches the
checkout`. All 18 manifest entries verify against both their source hash and
their installed hash; the eight changed modules are byte-identical between
the checkout and `~/.local/bin`; and the journal from the restart shows
whisper loaded, chatterbox-turbo warmed on CUDA and `ollama ok`, with no
import error anywhere.

## Another app's desk, over its own channel: one leaf module, one gate, four sentences a user can tell apart (2026-09-20)

**What this is.** Quantum Space — the sibling workbench on this machine —
publishes a local, opt-in, consent-gated control channel for a trusted program:
a discovery file beside its own settings, and five READ-ONLY JSON-RPC methods on
loopback. `core/qs_desk.py` is the client half of that contract, and
`core/tools.py` gains four tools on it: `quant_space_status` (`hello`, then
`desk.status` — "is it there, what is open"), `quant_space_sessions`,
`quant_space_read` (one session's tail, its id resolved against the desk's own
list rather than guessed), and `quant_space_check`, the diagnostic that names
WHICH state the link is in. Nothing was added to the other side: this round is a
client, and the contract it speaks was frozen before it started.

**Not built on `core/web.py`, deliberately.** That module's whole job is the
opposite of this one: its `_public_target` refuses loopback and private
addresses, and `read_page` walks a public redirect chain. On a loopback channel
that refusal is the hazard, not the protection, so the transport is stdlib
`http.client` — which also ignores the proxy environment variables `urllib`
honours, one more way a request meant for this machine could leave it.

**Four rules, each with its reason and its own test.** The discovery file is a
CLAIM by whatever wrote it, not evidence about the app: a mode that is not
private, a protocol number this client does not speak, and a live pid that is not
ours are each a REFUSAL that stops the search, and the test asserts the fake
bridge saw no request at all. The token is read PER REQUEST and never cached — the
same rule handsoff's own control token follows, and for the same reason, so the
file is rewritten with a second token between two calls and the second request
must carry the new one. A client that cannot read the credential still SENDS the
request, mirroring `_control_payload`: nothing here decides on the user's behalf
that "control is off", because the refusal belongs to the server that owns the
rule and it has its own wording for it. And the desk's sentence IS the answer —
its refusals are already written for a person, so they are surfaced verbatim,
with a test pinning that the message is character-for-character the desk's.

**Four states, four sentences, and one of them is the desk's.** A user hears
"Quantum Space isn't running" (no file, or a pid that is gone — a STALE file, not
"connection refused"), the desk's own sentence when it refuses on its own rules,
and "that session isn't on the desk any more". One wire reason covers two
situations — Control switched off, and this client not yet on the desk's
allow-list — and the body that knows which is the desk, so both sentences are
ITS; a client that composed its own would collapse two problems with two
different fixes into one line. Pinned by test as distinct strings, including that
the two `not-granted` refusals stay two different sentences.

**A refusal is never only in the model's reply.** A call the desk turns away goes
to the journal at WARNING, to `decisions.jsonl` (which the settings app renders
in its own Decision log pane), and onto the desktop through the same notification
path every other event the user has to know about uses — with the SAME wording
every time, so the host's coalescing folds a model looping on the call into one
popup instead of a swarm. Deliberately no second cooldown here: that would be a
worse second answer to a question the host already answers.

**One gate for the family, and a consent that is not ours to give.** All four
tools carry `gates='quant_space'`, the way `press_keys` covers two tools and
`watchers` two more, because the user's question is one question; the per-tool
`command_policy` still gives ALLOW / DENY / CONFIRM underneath. The key defaults
ON, like `watchers` and `screen_access`, and it is the SECOND switch: the desk
keeps its own Control setting and its own allow-list, and refuses in its own
words until the user consents THERE. So the assistant cannot talk its way onto
somebody else's desk by being allowed to read one.

**The census moved:** four more tools, one more permission key, one more core
module. Every place a spec states one of those counts was updated — the index's
two, the requirements' tool line and permission section, the architecture map
(its new row, its `@tool` sentence and its `CORE_REQUIRED` count), the two
generated tables (`ci/spec_tables.py --write`), the data spec's policy line, the
ops spec's quoted list, the test plan's row for the new file, and this spec's own
three. `install.sh`'s `CORE_REQUIRED` gained `qs_desk`: a tarball install stages
that floor, so a module missing from it compiles here and is absent on the
deployed machine, which `tests/test_sandbox.py` is what catches. `README.md`
gained the feature line and the permission row a user reads before flipping it.

**The tests drive a REAL socket.** `tests/test_qs_desk.py` starts an
`http.server` on an ephemeral port in a thread and scripts the desk's own
behaviour behind it, rather than recording calls against a mock — because half of
what this client must get right IS the wire: a POST to the root path, a bearer
header, NO `Origin` (which the desk refuses outright, so a web page cannot reach
it), and the HTTP status checked before the body is worth parsing. Pinned there:
a stale pid, a world-readable file, a body that is not JSON, a protocol number
that is not this one, and a live foreign pid — each refused with ZERO requests
made; the status codes for a bad Origin or Host, a wrong verb, a bad token, an
over-size body and a notification answer; the desk's own `not-granted`,
`session-not-found` and `no-output` reasons; the line clamp at both ends (and
that zero sends no count at all, so the desk's own useful tail applies); the
token absent from every repr, every error message and every decision-log line;
and the family's one gate — off means nothing reaches the desk, and a `DENY`
policy wins over it. The suite needs no Quantum Space installed, no port open
and nothing in the developer's own `~/.config`: each test builds its own profile
under a throw-away root and points the environment at it, which is also what
proves the module reads the environment rather than this machine.

**Measured on the final bytes, all seven gates green:** `tests` **PASS —
1 997 passed in 287 s** (1 936 before: 61 new tests); `order` **PASS** in BOTH
orderings (seed d985fc7, **1 997** each, 578 s); `coverage` **PASS, 85.42%**
(2 583 missing of 17 716) ≥ 70; `compile` **PASS** (56 files byte-compiled);
`shell` and `smoke` **PASS**; `clean-checkout` **PASS** (the freshness guard,
19 passed in 3.5 s, in a scratch worktree of HEAD); `two-writer` **PASS** —
"worktree unchanged since the run started". The two new files were STAGED before
that run, because two guards ask the index rather than the checkout: one refuses
an untracked module in a directory the installer ships by glob, and the other
compares the deployed set against what git owns.

**Stated limits.** The status line speaks `control` and `windows` only when they
are a plain bool or int — a client that guesses at another app's nested shapes
invents facts the user then hears as fact — so a nested control object is
reported as nothing rather than as prose. A read is trimmed to its tail
(the desk's own `truncated` flag is stated as well), because a coding agent's
scrollback is not a document and "what is it doing" is a question about now. The
folder in a read's label comes from the session list, since the read answer does
not carry one. There is no cap on how many desk reads one turn may make:
`command_policy` and the existing tool-call limit are the two controls this tree
already has, and a third would be a second answer to a question already
answered. And **nothing was deployed**: three SHIPPED files changed
(`core/tools.py`, `settings_schema.py`, `install.sh`), so the running bubble is
still the previous bytes and `--ptt doctor` will read out-of-sync against this
checkout until `install.sh` runs — deliberately not done from an unmerged
branch, because a branch is not a release.

---

## The desk client's first live turn: what a real Quant Space actually says, and the four places the fake bridge was flattering us (2026-09-21)

**What was run.** One real push-to-talk turn — *"what is Claude doing?"* — against
a real Quantum Space desk, on this machine, with the client from this checkout
deployed (`--ptt doctor` → `in-sync`; 19 manifest files hash-matched). The desk
was a real build of the app (the bridge-bearing one, `release/linux-unpacked`)
run against its own dev profile, holding a real Claude tile in
`/tmp/qs-desk-demo`, with Control switched on and `handsoff` on its allow-list —
the state Settings → Control produces.

**What the turn did, from the journal.** `ptt timing: frames=71680 rate=16000`
(4.5 s of audio captured) → whisper transcribed it → `tool call:
quant_space_status (argument names=[])` at 09:44:17 → and it spoke, from the real
desk: *"Claude is currently active in a session within the qs-desk-demo
folder."* The tool result the model was given, verbatim out of `history.json`:

> Quantum Space is running (v0.5.1) with the qs-desk-demo folder open. one
> session: claude in the qs-desk-demo folder. Control is on for handsoff and 1
> window.

`decisions.jsonl` carries the app's own entry for it (`quant_space_status`,
ALLOW, dispatched), which is the Decision-log pane doing its job.

**The finding that mattered, and it was one syllable.** The prompt that specified
this feature froze the profile directory as `Quantum Space` — with the syllable
the app does not have. The app's own product name is **Quant Space**; its built
`app.asar` carries that string 272 times and "Quantum Space" zero times, and a
dev run appends `-dev` to the *same* basename (`review-profile.js`:
`normalPath + (packaged ? '' : '-dev')`). So the first live call was:

```
paths: ~/.config/Quantum Space/control.json      — absent
       ~/.config/Quantum Space-dev/control.json  — absent
refused state='not-running' — "Quantum Space isn't running."
```

…against a desk that was **listening, granted, and holding a live Claude
session** at `~/.config/Quant Space-dev/control.json`. The frozen contract's
spelling made a running desk invisible, and no test could have caught it: the
fake writes the file wherever the test tells it to, so the fake agreed with the
contract and both were wrong about the machine. `APP_DIRS` now probes the app's
own name first and keeps the handoff's spelling as a tolerated fallback (a file
that exists and is not ours still stops the search, so this is not a guess about
which desk answered), pinned by a test that names the measurement.

**Four more places the fake was flattering, all now aligned to what the desk
answers.** (1) `desk.status.control` is the desk's own object,
`{"enabled": true, "clients": ["handsoff"]}`, not the bare bool the fake used —
and `describe_status` only spoke a bool, so against every REAL desk the client
said nothing about the consent the whole feature rests on. Caught live: the
status line read "…1 window." with `control` silently dropped. It now speaks
"Control is on for handsoff", and the shape we *don't* know is still not guessed
at (a `control` whose `enabled` is not a bool still says nothing). (2) `hello`'s
`desk` is the whole status object, not a word for "live". (3) `session.read`
also returns **`lines`** — how many it actually returned (45 of 2000 asked for),
which the contract's table does not list. (4) A non-POST, a bad token, an Origin
header and bad JSON come back with **empty bodies** — 405/401/403/400 and
nothing else — so the client keys off the code because there is nothing else to
key off; the fake used to send a JSON body and could have hidden a client that
read the body.

**The contract's most optimistic sentence.** It promises the read's `text` has
"already had ANSI/OSC stripped on the desk's side, so what you get is
speakable". For a Claude TUI that is false, and the fake — whose read text is
`"running the tests\n12 passed"` — could never show it. What the desk really hands
back from a live agent is prose with the **spaces eaten**, box-drawing rules, and
the same repaint twice:

```
Quicksafetycheck:Isthis
a projectyoucreatedor
❯No,exit
────────────────────────
```

The cause is the desk's own stripping (cursor-positioned repaints are not
sequence-free prose), and the fix is NOT ours to make: rewriting that here would
be this client inventing the agent's words. So the read is relayed exactly as the
desk stripped it, and a guard now pins that relaying — including the honest
consequence: a spoken `quant_space_read` of a TUI can be hard to listen to, and
making it pretty belongs on the desk's side, where the raw stream is.

**Three things found in the sibling project, which are not mine to fix from
here.** (1) The app the user is actually running — the AppImage in
`release/`, built 17:46 — **predates `control-bridge.js` entirely** (17:46 vs the
source written 18:43): `desk.sessions`, `control.json` and `[control] control
bridge listening` appear ZERO times in it, while the `linux-unpacked` build from
20:46 contains all three. So "built and live-verified" was true of the newer
build; a normal install of that AppImage would leave this client saying "isn't
running" forever, with the app visibly open. (2) In the sibling's current working
tree `wireAcpLive` is **imported and never called** (`main.js:13`; the only other
references are its definition and export), so `acp:start`/`acp:send`/`acp:kill`
are never registered and a chat/agent tile cannot start — live, the renderer's
`acp:start` came back `No handler registered`. The built bundle has the same shape
(three hits: definition, export, require — no call site), which is why the live
turn used a `kind: 'claude'` tile, the legacy spawn path that still works.
(3) `desk.status.folder` was `null` on the first call and `/tmp/qs-desk-demo` a
minute later: the window's folder registers after the bridge starts, so a client
must not read `folder: null` as "no folder open". This client's read labels come
from the session list for exactly that reason, and now have a live example behind
them.

**How the utterance got in, stated because it was not a held microphone.** The
acoustic route was tried first and failed twice: `--ptt stop` reported
`frames=0`, and a direct 4-second `parecord` of the same room wrote a **0-byte**
file, so the room path is currently dead on this machine (speaker output at 0.33,
the Yeti's ALSA profile named differently from the 2026-09-18 note:
`…-00.analog-stereo`, not `….2`). The turn that worked injected the phrase
digitally: a `module-null-sink` named `qsptt`, the default source pointed at its
monitor for the length of one press, `paplay --device=qsptt`, then the original
default source restored and the module unloaded (verified restored afterwards).
That is a real capture through the app's own `system default` path, and it is
**not** the same as a human pressing the button — §3's microphone items still need
a person, and the room path being dead is itself worth a look.

**Evidence.** `tests/test_qs_desk.py` **65 passed** (4 new tests and one
rewritten: the real product name is probed first, the desk's own `control` object
is spoken with its grant, an unknown shape is still silent, a real `w1_p_1` id is
handed straight back, and the mangled tail is relayed unchanged). The real wire
was captured method by method — all five methods, the line clamp at 1 and at
99999, and nine failure shapes — with the status codes matching the contract's
table exactly (403 Origin, 405 verb, 401 token, 400 JSON, 200 + `error` object
with the desk's own `reason` and sentence). `tests/test_specs_freshness.py` 19
passed after `ci/spec_tables.py --write` regenerated the module table (`core/qs_desk.py`
687 → **722** lines); `ruff` clean on both changed files.

**Stated limits.** The live desk was the app's **dev profile**, not the installed
one — a second instance had to be used because the user's own running copy holds
the single-instance lock, and the config directory was chosen by `--user-data`
rather than by the app's own default. So the *product name* fact is measured from
the app's source and its built bundle, while the end-to-end run used a profile
the app would also accept. The read path is proven against a Claude tile's trust
prompt, not against an agent mid-task: a session with a full transcript of
generated output has more repaints and more box rules, and nothing here proves
that reads stay useful at that size.

---

## The second ask, and the size question answered with numbers: the read is fine for a stream and theatre for a TUI (2026-09-21)

**The follow-up question was "ask again, against a session with a real
transcript of generated output, and say whether the read stays useful at that
size". The first honest answer is that no agent on this machine can generate
output today.** Claude Code's configured model is
`nvidia/nemotron-3-ultra-550b-a55b:free`, which its own catalog rejects
(`There's an issue with the selected model … It may not exist or you may not have
access to it`), and `codex exec` answers `You've hit your usage limit … try again
at Oct 6th, 2026`. The user's own Claude transcripts are thin for the same
reason (their biggest real one is 84 events whose last assistant block is a
`402 Token Harbor balance is at $0`). So the desk was given two REAL sessions
that could exist anyway: a `run` tile streaming `ollama run gemma4:12b` through a
"write a 120-line markdown report" prompt, and the Claude TUI itself, owned by a
shell tile (a `kind: 'claude'` tile **disappears from the desk when the CLI
exits**, which is how the first Claude session was lost mid-round — worth knowing
for anyone asking about a tile that has quit).

**Measured, both at once, one `session.read` each (asked 200 lines, then 2 000 —
identical answers, so nothing was being cut):**

| session | returned `lines` | `truncated` | chars | newlines | box glyphs | word-like |
| --- | --- | --- | --- | --- | --- | --- |
| `ollama run` report (`w1_p_1`, kind `run`) | **174** | false | 6 586 | 173 | 0 | 80 % |
| Claude TUI (`w1_p_1`, kind `claude`) | **1** | false | 2 173 | **0** | **362** | 66 % |

**So the usefulness has nothing to do with size.** The 174-line stream reads
fine: the tail is coherent markdown — `### 3.2 ANSI Escape Sequences`, prose with
its spaces intact — and its only noise is a 429-character Braille spinner frame
and the shell's own echoed command line at the head. The TUI is not useful at
*any* size: a cursor-positioned screen has **no line breaks at all**, so 2 173
characters arrive as one line, two thirds of whose "words" are glued
(`Claude Codev2.1.278`, `Keptmodelasth-orchestra`, `YourToken Harbor`), wrapped in
362 box-drawing characters, and the content is chrome — version banner, model
name, a billing line, the 402 error, timing, effort level, mode — with the one
surviving conversation fragment mangled (`the  is a test reply if      you see my 
 test`).

**And the desk's own flag is wrong in exactly that case, which is theirs to
fix.** It answered `lines: 1, truncated: false` about a whole screen, because
`read()` counts newlines (`text.split('\n')`): a screen with none is "one line,
not truncated" no matter how much of the 64 KB buffer it filled. A caller asking
for 200 lines is told nothing was cut when a screen was handed over. Called out
here with the numbers rather than worked around, because the fix belongs where
the raw stream is (`truncated` should be judged against the buffer, not against
the newline count).

**The most consequential finding of the round was not in the client at all: the
same question asked twice was answered from memory, and the memory was stale.**
Turn 1 (09:44) called the tool — `history.json` holds `[assistant]
tool_calls=1`, then the `[tool]` result off the real desk, then the spoken
answer. Turn 2 (10:06), the identical phrase, produced `[assistant] len=71
tool_calls=0` and said "Claude is currently active in a session within the
qs-desk-demo folder" — while the desk's real state at that moment was a `claude`
session in the **Quantum Shell** folder plus an ollama session in qs-desk-demo.
The model read the previous turn's tool result out of its own history and
repeated it. Nothing in the client can prevent that; `--ptt clear-history` is the
lever that already exists for it (its stated purpose is that a new model cannot
parrot the old transcript), and this is the measured case for pulling it when the
thing being asked about moves.

**Two defects this measurement found in the client, both fixed with guards.**
(1) A session's `name` is spoken, and the desk names a `run` tile with its WHOLE
command line — measured at 171 characters — so "what is open" used to read a
prompt aloud. Names are now bounded to 40 characters with the cut shown
(`ollama run gemma4:12b "Write a long tec…`), in the spoken line and in the
model's index alike, while `resolve_session` still matches on the full name.
(2) A read with no line breaks is now called out as ONE line, so a screen cannot
pass as a one-line session; a real line-oriented stream gets no such note
(pinned by its own test, because a note that fires on everything says nothing).
`tests/test_qs_desk.py` is **68 passed** (3 new).

**Stated limits, and one unresolved.** The "at size" stream was an Ollama report
rather than a working coding agent's scrollback — there is no working coding
agent on this machine today, so the *writer* is not the one the feature exists
for, though the stream shape is. The desk instance **exited on its own twice**
during this half (no OOM entry in the user journal; the log simply stops), and the
second capture produced no audio at all (`no audio from the recorder`) on the
same injected-audio path that had just worked, so the repeat ask could not be
re-run after `clear-history`. Both are recorded rather than smoothed.

All three of those limits were taken to the bottom the same day — the desk's
exit, the silent capture, and the missing coding agent — and the constructor
mistake that paid for the search is written up in "The second live turn's three
limits, measured to the bottom" below. The paragraphs above are left as they
were written, because each sentence in them was true at the time.


## The second live turn's three limits, measured to the bottom — and the client's own constructor was the defect (2026-09-21)

The section above ends with three things left open: the desk that "exited on its
own twice", the capture that produced no audio on a path that had just worked,
and the read never having been proven against a working coding agent. All three
are closed here, and one of them was the client's own fault.

### Why the desk kept going away: it quits by itself, cleanly, in minutes — and the trace was dying with it

The instrumentation was the first defect, and it was mine. The launch wrapper was
`bash -c '"$BIN" …'`, so the wrapper's own `/proc/self/cmdline` contained the
binary path — and `pkill -f <binary>`, the very command used to stop the app,
killed the wrapper too. The wrapper therefore never wrote its exit line, which is
exactly why the earlier account reads "the log simply stops". A watcher whose
argv holds no part of that pattern (the binary path comes from a file) recorded
the truth on the first try:

```
launch 2026-09-21T10:32:41 pid=198420 …
EXIT status=0 at 2026-09-21T10:37:27 after 285.6 s
```

**It exits by itself, cleanly, and not on a fixed clock.** Measured lifetimes on
this machine: 17.0 s, 22.7 s, 35.1 s, 44.1 s, 47.3 s, 285.6 s, ~243 s, ~244 s.
Every one was `status=0` or a systemd `Result=success`/`ExecMainStatus=0`, with
no Crashpad dump, no OOM line in the kernel journal, and 22 GB of RAM free. The
compositor's own event stream shows the window arrive and then leave
(`WindowOpenedOrChanged {id:40, pid:317613}` … `WindowClosed {id:40}`), and once
it is gone niri has no `Quant Space` window left at all — so the sentence a user
hears and the screen they see agree.

What it is **not**, each eliminated by measurement rather than argument:

* **not my shell's lifecycle** — the same deaths happen under `systemd-run
  --scope` and as a `systemd-run --user` *service*, i.e. outside the cgroup my
  commands run in (my shell lives in `app-niri-noctalia-1435.scope`);
* **not content** — an *empty* desk, no tiles, no agent, died in 44.1 s;
* **not the GPU path** — `--ozone-platform=x11` and the Wayland default died
  within a second of each other (4 min 3 s and 4 min 4 s), so the "Vulkan is not
  compatible with wayland" error it logs is noise, not the trigger;
* **not the updater** — `build.publish` is null and `installNow` is reachable
  only from a click;
* **not app-level window code** — there is no `window.close()` outside the
  `--shot` screenshot mode, and the only `process.exit(0)` is the tail of the
  quit handler.

It is **not only the dev build either**, and the same event explains the user's own
copy: their *installed* AppImage, up and serving for 1 h 39 min, exited at 11:05:38
— systemd logged its scope's consumption at that second
(`app-niri-nautilus-22894.scope: Consumed 4min 20.716s CPU time over 1h 39min
47.291s`). The *Install current AppImage from folder* window on the desktop
afterwards belongs to an install they began; the update **poller is not the
trigger** — it is notify-only, it never runs in development, `installNow` is
reachable only from a click, and `auto.autoInstallOnAppQuit = true` installs at
quit rather than causing one.

### What the exit actually is: a `Mod+Q` chord, and the app's own answer to it

Pinned by instrumenting the sibling's **packaged build**, not their source tree —
their tree is on `fix/ci-tests` with uncommitted work, so the copy lives at
`/tmp/qs-implant` (`asar extract`, ONE added line `require('./quit-trace.js')`,
repack). The tracer logs every `app` event, every window event and every `close`
**with a stack**, plus a 10 s heartbeat, and the renderer speaks through console
markers (`ELECTRON_ENABLE_LOGGING=1`).

The trigger is the user's own niri binding, `Mod+Q { close-window; }`
(`~/.config/niri/cfg/keybinds.kdl:37`), acting on whichever window held focus:

* the spontaneous close (11:41:42, 174.81 s into the run) arrived with a stack
  holding **only the tracer and Node's own `emit`** — *no application frame* — and
  the renderer **never called `window.close()`**, so no code in Quant Space closed
  it;
* the renderer recorded the chord's own signature **177 ms before** the main-side
  close: `[RIMPLANT] keydown key=Meta ctrl=false meta=false alt=false`, then
  `beforeunload` → `pagehide` → `visibilitychange -> hidden` → `unload`. A bare
  `Meta` is exactly what `Mod+Q` leaves behind — niri consumes the `Q`;
* the app's answer to losing its last window is Electron's default path:
  `window 1 event: closed` → `app event: window-all-closed []` → `app.quit()`
  called from **`main.js:593:76`** → `before-quit` → `will-quit` → `process 'exit'
  event, code=0`. Nothing else had happened inside the app first: the last IPC
  channel before the close was at **+0.47 s**, 174 seconds earlier;
* **positive control**: `ydotool key 125:1 16:1 16:0 125:0` (Meta+Q) against the
  focused window reproduced the chain byte-for-byte at **+0.92 s** — the same
  `keydown key=Meta`, the same frameless `close`, then `window-all-closed`,
  `app.quit()` from `main.js:593:76` and exit 0;
* **elimination**: the same instrumented bytes under a nested headless weston
  (`--backend=headless-backend.so --socket=qs-weston`) — a compositor niri cannot
  send a close request into — ran **+120 s** with `visible=true focused=false
  destroyed=false` and RSS flat at 215 MB. Nothing in the app closes its own
  window on any timer.

So the lifetimes (17.0 s … 285.6 s … 1 h 39 m 47 s) are not a schedule: they are
*when a chord landed on a window that had focus*. Under niri these windows get
focus within half a second of opening (`event: focus` at +0.54 s in the traced
run). **And the immunity of a blurred desk was then measured rather than argued:**
two instrumented clones launched seconds apart on their own profiles, the second
holding focus, and one chord closed only it — the blurred clone recorded **0 `close`
events**, logged no `keydown` at all, and was still running when the chord had
passed (niri gave it focus 8 ms after the other's exit). What a trace cannot recover
is *intent*: the spontaneous run shows the desk re-focused 0.96 s before its close
(`event: focus` at +173.85 s), which is what being selected and then closed looks
like and equally what a chord aimed at the frontmost window looks like. The measured
statement is the narrow one — `close-window` closes the focused window, and the desk
was the focused window. What matters for
this feature is that the desk's exit is *audible* rather than mysterious: their
`before-quit` removes the discovery file, so the client's answer is the true one
("Quantum Space isn't running."), not "connection refused" about a port nobody
owns.

### The silent capture: an assignment to an audio source that does not exist yet is swallowed

`pactl set-default-source qsmic`, issued in the same breath as
`module-remap-source` creates that source, is **dropped silently**: pactl exits
0, prints nothing, and `pactl get-default-source` still names the Yeti. The
bubble then opened the real microphone, which heard the room and nothing else —
which is what "no audio from the recorder" was.

The harness fix is the assertion, and it is now in front of every turn rather
than behind it: play the phrase into the null sink *while* recording the default
source, and require signal before spending a turn. Measured on the check itself —
peak 32767, rms 3777, 32 301 samples above −36 dBFS — and then on both turns that
followed it: `ptt timing: frames=97280 rate=16000` and `frames=93184 rate=16000`,
against the zero of the failed capture.

### A real coding agent on the local model: what a coding agent's scrollback actually is

Every *installed* agent still cannot generate on this machine: Claude Code's
configured model is rejected by its own catalog, `codex exec` is out of quota
until 6 October, `opencode` does not start at all (`SQLiteError: no such column:
replacement_seq`), goose's configured provider is a hosted one, and the user's
own stored transcripts end at `402 Token Harbor balance is at $0`.

But goose speaks Ollama — `GOOSE_PROVIDER=ollama GOOSE_MODEL=qwen3:8b` — which
makes a **real coding agent** work a **real folder** locally. Measured: it read
the files, ran `python3 -m pytest -q .` for real, and its tile holds the literal
`1 passed in 0.01s` under goose's own `▸ shell` block. So the writer the feature
exists for now has a live example, and the read was measured on three shapes
through the same client:

| session on the desk | `lines` | chars | box glyphs | word-like | `truncated` |
| --- | --- | --- | --- | --- | --- |
| goose agent report (its own 73 lines) | 77 | 4 734 | 0 | 90 % | false |
| Ollama markdown stream (previous turn) | 174 | 6 586 | 0 | 80 % | false |
| Claude Code TUI screen | 1 | 2 173 | 362 | 66 % | false |

**Usefulness tracks orientation, not size.** An agent's own prose arrives
line-perfect (longest line 260 characters, and `truncated: false` because 4 734
is inside the 8 000-character cap). A cursor-positioned screen has no line breaks
at all, so two thousand characters of chrome arrive as one line. And the local 8B
model is a weak tool user: asked to survey the module it emitted `shell` with a
missing `command` and goose printed `Error: Failed to parse arguments: missing
field command`; asked for a 60-line report with the module embedded in the prompt
it produced *no* assistant text at all (two sessions of 37 kB prompt); asked for
a report with no tools it answered in 73 lines. A coding agent on a local model
is real, and it fails like one.

### The failure path, tested by accident

The third live turn ("Read me what the agent on my desk has been doing.",
`clear-history` first, so the model could not parrot) captured `frames=93184`,
transcribed, and called `quant_space_read` — and by then the desk had gone. The
tool results were `ERROR: Quantum Space isn't running.` twice, and the spoken
answer was *"Quantum Space is not currently running, so I cannot see what the
agent is doing."* That is the designed behaviour under the hardest condition
there is — the desk dying mid-turn in another app's short life — and it is the
opposite of the two failure modes this feature was built to avoid: no hang, and
no invented answer. Nine minutes earlier, on the same injection path, the same
turn made **four** tool calls and relayed the desk's own `session-gone` sentence
for a name the model had guessed (`That session isn't on the desk any more. Open
now: shell, /tmp/qs-agent-task2.sh.`).

### And the defect that cost the hour: `Desk("handsoff")`

The client's constructor takes `paths` first. The natural mistake — the CLIENT
name where a path was wanted — became one relative path, matched no file, and
answered **"Quantum Space isn't running."** about a desk that was running, with a
0600 discovery file on disk and a live pid inside it. A wrong state is worse than
a crash here: it is indistinguishable from the truth, and it sent this exercise
looking for an app that had not gone anywhere.

Fixed in `_as_paths`: a relative path, an empty list and a non-path are refused
as the programming errors they are (naming the shape wanted and the `client=`
that would have been right), an omitted list searches the profile directories —
the same list `connect()` uses — and one absolute path is still a path list. Five
new guards hold it, including the one that says a *correct* construction still
reaches the desk that is running. `tests/test_qs_desk.py` is **73 passed**.
## The gates stop drawing on a spent quota — the desk runner, and the two couplings that five suites on one machine made visible (2026-09-21)

**The symptom was a pipeline that said nothing about the commit it judged.**
Isolate them one at a time and the picture is arithmetic, not mystery: this
namespace's 400 free compute minutes a month are **one pool** for every project
under the account, and this repo's five suite jobs had spent ~235 of them by
mid-September (measured per job from the API: `coverage` 86 min, `tests:3.13`
61, `tests:3.12` 60, `order` 26). The last job that actually executed was
`tests:3.12` at **08:47 UTC on 13 September**; the first refusal was `smoke` at
09:31; and **365 jobs in the last 60 pipelines** never started at all, each one
created and finished milliseconds apart with `failure_reason:
ci_quota_exceeded`. The last pipeline that ran to a verdict is **#2844293191
(13 Sep 07:18, 14.7 minutes, all 7 jobs on instance runners)** — that is what one
push cost, and it is why the burn is worth fixing at the runner and not by
quietly running fewer gates.

**The fix is a runner, not a smaller suite.** A project runner on the
developer's machine — id **56559829**, tag `desk`, shell executor,
`gitlab-runner` **19.4.0** with its binary checked against the release's own
`aeebda64…`, running as the user service `gitlab-runner-desk.service`, config in
`~/.gitlab-runner/config.toml` at mode 0600, scoped to this project and
`quant-space`, untagged jobs refused — where a job is **not billed**. Every job
in `.gitlab-ci.yml` carries `tags: [desk]` except the frozen-image reference.
The one assumption worth testing was tested, in the useful direction: GitLab.com
**does** schedule jobs on a private runner while the namespace's quota is spent
(the first desk pipeline was created and picked up in three seconds), and only
*instance* runners are blocked — which is exactly why GitLab's own docs list
private runners as the mitigation.

**Two things the image used to provide have to be provided another way, and
both are CHECKED rather than assumed.** `ci/desk_python.sh` fetches the CPython
each job stands in for into a per-version `uv` venv and **refuses an interpreter
that answers with another version** — the desk's system python is 3.14, newer
than both matrix legs, and a green about a python nobody ships would be a
different kind of red. `ci/apt_deps.sh` still installs on Debian and now
**ensures** where apt does not exist (the same two tests on the desk: the files
by name, and the `ctypes.util.find_library('portaudio')` lookup sounddevice
performs at import), which is what keeps every pytest job inheriting the runtime
layer instead of the anchor quietly becoming decorative.

**The measurement that decided the shape.** The first green pipeline on the desk
is **#2867642545**, and every number below is read out of its job traces:

| job | what it ran | desk time |
|---|---|---|
| `coverage` | suite + `--cov-fail-under=70` | 328 s — **1 938 passed**, TOTAL **82%** (3 966 missing of 21 717) |
| `order` | shuffled, then file-order shuffled | 498 s — **1 938 passed** each (244 s + 245 s, seed `b73a955…`) |
| `tests:3.12` | suite | 246 s — **1 938 passed** |
| `tests:3.13` | suite | 253 s — **1 938 passed** |
| `compile` / `shell` / `smoke` | byte-compile, pins, `--help` | 3.6 s / 3.5 s / 3.4 s |

**22.3 minutes of machine time, 0 billed minutes, 7 of 7 green.** The coverage
figure being *lower* than the desk's own ~85% is not a regression: CI exports
`COVERAGE_PROCESS_START`, so the offscreen-GUI children are measured too, and
more measured statements with the same untested edges read as a smaller
percentage — the documented difference, now stated where the number is.

**The suite jobs hold a `resource_group`, and that is measured rather than
cautious.** On a hosted runner every job owns a fresh container; on a desk they
would own the same GPU, audio server and `/tmp`. The first desk pipeline carrying
a change collided **twice in one run**, and both were real couplings rather than
noise: `test_precommit_staged`'s scratch-tree check compared the machine's whole
`/tmp/handsoff-staged-*` set before and after, so the OTHER job's in-flight tree
read as its own leak (both legs, same seconds), and the offscreen live-probe
scenario's whisper worker was still answering when its wall-clock deadline
expired. Both are fixed where they live: the scratch check now hands the hook a
`TMPDIR` and judges only that directory (the hook's own `mktemp -d -t
handsoff-staged-XXXXXX` honours it, which is why the fix works), and the GUI
scenario **joins** its worker instead of running out a deadline.

**A new guard holds the shape that made a neighbour look like a leak**
(`tests/test_sandbox.py`): a test that enumerates the machine's temp root — or
anything else shared — to judge its own work fails, and the message says so.
Mutation-checked: a planted leak in `githooks/pre-commit` is caught, and a test
that forgets its own `TMPDIR` is caught, each restore sha256-verified.

**The frozen-image reference was moved out of push pipelines, and that needed
measuring rather than reasoning.** Once the namespace's minutes are spent GitLab
refuses **every** job of a pipeline at creation — manual jobs included. Job
16625849832 was created and finished **7 ms** apart, never queued, with
`failure_reason: ci_quota_exceeded`, so leaving `when: manual` in place put a red
mark on every push that said nothing about the commit. It is now defined only in
pipelines started from the web UI or the API, where a human asking the question
"do the desk and the image agree?" actually starts one.

**Stated limits.** The desk job runs a uv-managed 3.12 or 3.13, but the kernel,
libc and Qt are the desk's rather than the digest-pinned Debian image's — that
question is what `suite:hosted` remains for, and it has **not** been run since
moving (it spends minutes, which is the point of it being manual), so parity
between desk and image is asserted by construction here, not by a fresh
measurement. A job **waits** for the desk runner rather than failing over to an
instance runner, so a machine that is off means a pipeline that hangs, not a
pipeline that bills. Bytecode is pointed at `/tmp`
(`PYTHONPYCACHEPREFIX`), because the desk interpreter lives under `$HOME` and
the suite's own checkout-write guard refuses a test write into the developer's
real user dirs — with no artifact exemption, deliberately; measured: without the
redirect the guard refuses the run at the first stdlib import. And the
serialisation **trades wall time for a verdict** (~22 min of machine time per
push instead of ~15), which is the honest price of a green that is about the
commit rather than about its neighbours. The desk machine time is not billed,
but it is not free either.

## A System-1 decision engine measured before it is trusted — and the desk work is on no remote (2026-09-21)

**Two questions, one round: is a typed-decision model worth wiring into the turn,
and what does a fresh look at the tree see that the ledger does not?** The first
was answered with a harness rather than an opinion; the second turned up a
feature that only exists on this machine.

### The prize, measured

The turn offers **all 48 tool schemas on every round** — `json.dumps(H.TOOLS)` is
16 680 characters, **~4 165 tokens** — and the system prompt another 2 172, so
`_fixed_prompt_tokens()` is **6 342 of a 32 768-token window**: the tool list
alone is **12.7%**, paid again on every tool round (`_brain_turn`, and the list
is rebuilt per round from the live permission map). Nothing anywhere decides
what KIND of request this is before the 12B model is asked: the only pre-model
predicates in the tree are `core/web.py`'s search-backend `_route` and
`_classify_edit_path`'s edit-risk read.

### The engine, measured on this machine

Laya **0.3.4** is installed (pipx venv, python 3.14, torch 2.14.0+cu130) with
**2 369 MB** of checkpoints cached and **no console script** — `command -v laya`
finds nothing, because it is a library. Measured here, offline: **load 13.8–15.7 s
per checkpoint**, **1 607 MiB resident** on the card, **p50 28–34 ms** warm on GPU
(54 ms on a longer option set), **p50 538 ms / max 1.09 s on CPU**, and its
`detect_script` helper 0.004–0.039 ms in pure Python.

### The bake-off, and its verdict

`ci/laya_bakeoff.py` — new, and deliberately **not a gate**: it needs torch and a
GPU, neither of which is a dependency of this project (the suite asserts torch is
never imported), so it is run by hand under the interpreter that has Laya. It
scores two corpora and never merges them: **63 authored cases** (every family,
several phrasings, including the recogniser's own damage — *"hey seifer tell me
the way they're outside"*, *"something in the chat"*, *"it's time for play
music"*) and the **real pairs mined from the app's own history files**, labelled
by the tool the 12B model actually chose. Private utterances are read and never
written back into this repository.

| corpus | top-1 | recall@2 | recall@3 | recall@5 |
|---|---|---|---|---|
| authored, plain wording (63) | **39/63 — 62%** | 73% | 83% | 94% |
| authored, strict wording | 35/63 — 56% | 78% | 83% | 89% |
| real pairs (7) | 3/7 | 43% | 43% | 71% |

**Three things kill it as a gate, and the second is the interesting one.**
(1) 62% top-1 is not a routing decision anyone should ship. (2) **Its
confidence is saturated and wrong**: p50 and max are both **1.00**, so the
README's "act automatically above 0.85" rule is meaningless on this schema —
measured on the same cases where the choice itself was wrong. (3) The error is
*systematic*, not noise: `none` absorbs imperative requests — `web -> none` ×5,
`windows -> none` ×2, `system -> none` ×2, `timers -> none` ×2 — and the strict
rewording, written to forbid exactly that, moved top-1 **down** (56%) while
lifting recall@2 to 78%. The option set is what the model is failing on, and it
fails confidently.

### What it CAN do, and why the cheaper fix is elsewhere

As an *advisory* widener (offer the top-k families, keep the full belt as
fallback) recall@3 is **83%** — a real number, and the honest reading of it is
"three families in four". The token arithmetic says that is not where the fat is:
`windows` (11 tools) and `system` (9) carry **1 915 of the 4 165 tokens** — 46%
of the whole schema — so a learned router that narrows to three families saves
~1 800 tokens while risking the right family 17% of the time, whereas splitting
or trimming two oversized families saves the same tokens with **no** accuracy
risk. Laya's own README is explicit that the base checkpoints are near chance on
its typed-decisions benchmark and that fine-tuning is where the value is; that is
what the harness now exists to grade, against a corpus that grows with use.

### Found while measuring: the desk work is on no remote

Mining the corpus meant reading the app's own `history.json`, and a turn in it
calls **`quant_space_read`** — a tool that does not exist in this checkout.
`grep -c quant_space` over `handsoff.py` and `core/tools.py` on `main` is **0**.
The work lives on **`feat/quant-space-desk`**, which is **local-only** (`origin`
has one head, `main`), carries `core/qs_desk.py` plus the four desk tools and
their tests, and is **diverged, not behind**: 3 commits only on the branch, 4
only on `main`, so it cannot fast-forward. The running bubble is that branch's
bytes. So the deployed assistant has a capability that exists on exactly one
machine, in a working copy, with no remote copy and no merge request.

### Stated limits

The authored corpus is **my writing**, not ground truth about what this user
says; the real corpus is **7 cases** (the app's history is 6 entries plus a
handful of backups — that is the whole of it), so it is reported separately and
is a signal, not a verdict. Only the `typed-decisions` checkpoint was measured
(not `multilingual`, not the `Router`), and only on this card. The harness scores
families, so its accuracy bounds the *narrowing* idea and says nothing about
choosing a single tool. And the unmerged-branch finding is a fact about this
working copy as it stands today: it says nothing about intent, only that no
remote can restore it if this disk does not.

## The section-4 TTS walk: what the machine half closed, and three things it could not (2026-09-21)

Section 4's first two items were walked at the machine. The ledger records the
hand's verdict and the measurement separately, because these items exist to be
judged by a person: a sink monitor proves sound left the app and cannot prove it
reached an ear, and that gap is the whole reason the items are marked `[human]`.

**Replies audible — heard, in the configured voice.** Before the test the output
path was checked (sink unmuted and active, `tts_volume` at unity), because a muted
sink looks identical to a working one from inside the process. On the machine
side, `history.json` stayed BYTE-IDENTICAL across 34 s of OPEN MICROPHONE while
the bubble spoke a 161-character sentence containing its own wake word, and again
across a 262-character one: the transcript holds nothing the bubble said.

Three findings are recorded rather than rounded off:

* **The rule that refused the captures was the wake word, not the echo guard.**
  Both were stopped by `ignored (no wake word)` while the echo check answered
  "not my own speech" (`False`) on both. So "the bubble does not transcribe
  itself" is established by the wake-word rule here; the echo detector's own
  branch was never the one that fired on these two captures.
* **An utterance inside the follow-up window was still refused for want of a wake
  word.** It arrived after the window opened (18:13:06.136) and was decided
  inside it (18:13:11.937), yet took the wake-word path. Either the edge is
  sub-second or the window is not honoured; a single sample cannot tell the two
  apart. Open question, not counted as a pass.
* **Barge-in truncates decisively, but not literally instantly, and the click
  half is unsettled.** With speech detected from the sink and the interrupt sent
  only once it had been audible for 3 s, a 300-character announcement whose
  natural length is 20 s (control) stopped after 4.3 s of speech, and
  `state=idle` followed. With Chrome and speech-dispatcher muted so only the
  bubble was on the sink, about a second of speech-level audio followed the
  interrupt — longer than `play_wav`'s own cancel granularity (`_PLAY_BLOCK` of
  1024 frames), so it is recorded as an open finding. The click half cannot be
  settled by machine: the bubble interrupts on PRESS, before the hold-to-record
  timer — the right verb in the right order — but it also sets
  `Qt.WindowDoesNotAcceptFocus`, so a click cannot be confirmed by a focus
  change, and neither niri's reported tile position nor the window rule's anchor
  produced a PTT capture when pressed. Supervising the tap remains the test.

**Found while setting the item up, and it answers section 3:** the configured
default input opens and then never returns a frame (a `RuntimeError` on every
reopen, zero frames, `stalled`), and the Yeti is absent from `sounddevice`'s
INPUT list entirely — it appears only as an output. The capture was moved to the
StreamCam, which opens at 16 kHz and delivers frames, set through the same
setting the Voice pane writes and applied without a restart.

## The oversized tool families: what a trim is worth, and the filter that never filtered (2026-09-21)

The Laya round's arithmetic put the cheap half of its prize here — `windows`
(11 tools) and `system` (9) carrying **1 915 of the 4 165 schema tokens**, 46% of
the belt, so that trimming them bought the same prefill a three-family router
would, with no accuracy risk. This round went after it, and priced the ceiling
first.

**Re-measured on the merged belt (52 tools).** The whole thing `json.dumps` to
**18 512 chars ≈ 4 628 tokens**, and the two families are **7 299 chars ≈ 1 825
tokens — 39%** (the share is below the audit's 46% because the desk merge added a
family of its own). Within them the cost splits three ways: descriptions
**3 008**, parameter docs **1 506**, and JSON scaffolding **~141 chars per tool**
over 19 tools ≈ **2 679** — and that last number is the ceiling of any prose trim:
`{"type": "function", "function": …}` is not prose, and only FEWER tools remove
it.

**What the trim bought: 748 chars ≈ 187 tokens.** Descriptions 3 008 → 2 278, the
two families' parameter docs 1 506 → 1 479, the whole belt 18 512 → 17 764. The
rule held to is that a capability the model must DISCOVER keeps its words: the
whitelist's trivial members (`echo`, `cat`, `ls`, `pwd`) stay, because that is how
a model learns it may list a directory; what went was restatement and mechanism —
"needed to turn screenshot pixels into pointer coordinates" became "for
pixel-to-pointer conversion", "Run screen_elements first" went because the scan
tool's own description already says it, and the same parameter sentence shipped
twice ("optional 'x y width height' in pixels") became one shorter line.

**The real lever was a filter that never filtered.** The round loop shipped
`[t for t in TOOLS if SETTINGS["permissions"].get(t["function"]["name"], True)]`
— and the permissions dict is keyed by FAMILY (one dropdown per family in the
settings app: `run_command`, `screen_access`, `operator`…), never by a tool's own
name. That lookup missed every time, defaulted to `True`, and so **nothing was
ever dropped at all**: a default config shipped **4 schemas belonging to families
that are OFF by default** (`operator` ×3, `notifications` ×1) — **1 449 chars
≈ 362 tokens in every round** — for tools the belt answers with `REFUSED: the
'operator' tool is disabled in handsoff settings`. A capability the user cannot
call is a capability the prompt should not pay for, and this one was being paid
for twice: once in prefill, once in the round the model spent calling it.

**Fixed at the predicate, not the call site.** `permitted_tools` filters on the
tool's own GATE, from live settings, rebuilt every round, so a family switched on
mid-session is offered on the very next round; ungated tools always ship; a gate
the filter has never heard of fails OPEN, matching the belt's own check at call
time. `tool_gates()` and the schema builder now share ONE walk of the belt,
because a second copy of that loop is exactly how a tool ends up in the prompt but
not in the gate map. **On a default config the schema sent every round goes
18 512 → 16 315 chars (52 → 48 tools): 2 197 chars ≈ 549 tokens — 12% of the
belt** — and the four schemas removed are exactly the four that could only ever
refuse.

**The refusal keeps its fix path.** Dropping a schema also drops the tool whose
refusal NAMED the switch, so the family names travel in the system prompt instead
(one line, written only when something is off — about 150 chars against the 1 449
saved), and the spoken answer can still point at the settings app rather than
guess at a result.

**What each family is worth, since that dial is now real** (chars per round; a
quarter of that in tokens): `run_command` 2 811, `quant_space` 1 905,
`web_access` 1 834, `media` 1 423, `reminders` 1 136, `operator` 1 073,
`screen_access` 1 048, `focus_window` 768, `watchers` 738, `press_keys` 736,
`calendar` 566, `edit_file` 484, `notifications` 376, `pomodoro` 369,
`read_file` 335, `type_text` 319, `paste_text` 246, `copy_text` 211,
`get_datetime` 184.

**Teeth: 8 guards.** A switched-off family leaves the prompt, and the saving is
asserted to be more than a rounding error; with every family ON the shipped list
IS the belt, so the filter cannot lose a tool; an unknown gate fails OPEN and with
every family off only the gateless tools remain; the turn site filters on the
family, with the name-keyed lookup asserted GONE from the source; a switched-off
family can still name its switch. Then the two that keep prose and interface
apart: the tool interface is pinned as a sha256 over {name, params, required,
types} for all 52 tools, so a description may shrink but something callable
cannot silently disappear; the whole belt has a ceiling, and the two oversized
families' descriptions have one too (2 400, against 3 008 before).

**One thing deliberately NOT changed, recorded as an open item:** the startup
warm-up still sends the FULL belt, and the suite pins that by name ("warmup must
pass the full tool schemas"). Its rationale is that the warm-up should hold the
exact prefix a turn will use — which is no longer strictly true once a family is
off, so the first question after a restart can re-prefill the tools block. That is
a design call about what the cache is for, not a trim, so it is stated rather than
taken.

**Stated limits.** The reconstruction of "saves ~1 800 tokens" is arithmetic on
today's 52-tool belt, not a measurement of the audit's 11+9 plan. The scaffolding
price (~141 chars per tool) is measured, and it is the part no prose can reach.
The trim is deliberately partial: every enumeration that names a discoverable
capability survived. The venue is a DEFAULT config — a user with every family on
gets the 748-char trim and none of the 1 449, which is the point, because that
dial belongs to the user. And the interface digest is a SHAPE check: it proves
nothing about behaviour, so the behavioural evidence is the other thing measured
here — that the four schemas removed were the four the belt refused at call time,
and that the belt's own gate check is untouched.

## The Laya fine-tune: measured, and the answer is "not yet, but the calibration is" (2026-09-21)

The bake-off said fine-tuning was the honest path, so this pass took it: a
fine-tune on THIS project's own tool-family decision, graded by that same
harness, against a corpus that grows as the machine is used.

### A family the belt had and no option could offer

`--report` compares the harness's family table against the shipped belt, and it
found the gap the table itself could not: **four desk tools belonged to no
option** — the family that shipped 1 905 characters of schema in the previous
round was invisible to the router, so the token arithmetic was pricing four
tools as unroutable and a `quant_space_*` request could only ever be answered by
whichever family looked closest. A `desk` family now exists (with its speech), and
the check reports **0 missing and 0 unmapped over 52 tool names**. Labels are
read from the harness's own table, never restated here, because two copies of a
label set drift silently and the fine-tune would then be trained toward options
the harness no longer scores.

### The corpus, and how it grows

The authored set is the harness's (69 rows, hand-labelled speech — including a
recogniser's own damage). Real rows are MINED from the app's own history files:
utterance plus the tool the real 12B model called, mapped to a family through the
belt's own gate map. They live in `laya-corpus.jsonl` in the app's state
directory — never in this checkout — deduped by normalised text with `first_seen`,
`last_seen` and `count`, and a second disagreement about the same sentence is
reported as a CONFLICT rather than silently relabelled. Eight real rows exist
today; re-mining is idempotent (+0 new, 8 re-seen, 0 conflicts, 0 unknown labels),
and **six of those eight sentences were already in the authored set** — the
written corpus predicts what this user actually says. The corpus a fine-tune saw
was therefore **71 rows** (69 authored + 2 mined and not already covered), in 12
families; its content hash `83e4c83f16736100…` is recorded inside the checkpoint
it produced, so a later run can tell whether the corpus moved under it.

### What was trained, and on what evidence

The task is the harness's own `choice` question over its own option table,
converted with the runtime's own `_to_internal`, sequenced with the runtime's own
`build_sequence`, scored by the library's own strictly proper scoring rule
(`proper_reward`) plus a small action loss toward "answer directly". Every number
is **stratified 5-fold cross-validation with the zero-shot model scored on the
SAME folds**, seeded (an unseeded run printed 57% and then 66% on identical folds
— which is how the noise floor got measured), and every metric is taken on RAW
logits because a positive scale cannot move a ranking.

| stage | what learns | zero-shot -> fine-tuned, top-1 | ECE (raw) |
| --- | --- | --- | --- |
| `head` | 26.5 M params, encoder frozen | 64→66, 61→63, 63→62 (+1 ± 2) | 0.40 → 0.29 |
| `last` | 75.5 M (head + 4 of 28 encoder layers) | 64→76, 61→73, 63→78 (**+12, +12, +15**) | 0.40 → 0.18 |
| `full` | all 421 M | **did not fit**: OOM beside a bubble holding 3.69 GiB on a 16 GB card | — |

So the frozen encoder WAS the limit: adapting four of twenty-eight encoder layers
is worth about **+13 points of top-1** and halves the calibration error, while
training only the head is worth nothing that survives the seed spread. A held-out
checkpoint (trained on 50 rows, its fold-1 rows never seen) was then graded
end-to-end through the library's own loader: **zero-shot 14/21 (67%) → fine-tuned
16/21 (76%)**, recall@3 86% → 90%, and with the checkpoint's temperature table
pinned to 1.0 the accuracy is IDENTICAL — the ranking/calibration split proves
itself at runtime. On the rows it trained on the same checkpoint scores 93%, and
that number is memorisation, not evidence.

### The finding that is ready to use: the confidence table is wrong for this task

The checkpoint calibrates by OPTION COUNT, and this task's 12-option question
lands in its widest bucket, `choice:11+`, where the table says **0.1006**. That is
why the bake-off saw confidence pinned at 1.00 while the model was wrong: the
scale, not the model, is what saturates. Measured on raw logits, a turn's own
confidence is unusable as a gate:

* log-score: raw **−0.96 / −1.05 / −1.00** (three seeds) vs **AS SHIPPED −1.98 /
  −2.30 / −2.07** vs fitted −0.82 / −0.92 / −0.86. The shipped table costs about
  **a full nat per decision**.
* ECE: raw 0.38–0.40, as shipped 0.34–0.38, with a temperature fitted on our own
  corpus 0.29 — and 0.16–0.19 for the encoder-adapted model. The fitted value is
  **0.51–0.91 across folds** (median 0.62 zero-shot, 0.56 fine-tuned), i.e. the
  shipped number is roughly **seven times too sharp** for an eleven-plus-option
  choice.

The saved checkpoint therefore writes the fitted value into that bucket, which is
a calibration fix that cannot change a single decision.

### Verdict

* **Do not wire the fine-tune as an accuracy play yet.** +13 points measured on
  71 rows of in-house speech, held-out n = 21, folds of 10–21 rows: the direction
  is clear and the size is not. The corpus has to grow about tenfold first, and
  the store now does that by itself every time the harness is run with `--grow`.
* **Do use the calibration number** the moment any confidence is treated as a
  gate to act on: as shipped, the checkpoint's own "act above 0.85" rule would
  fire on almost every request.
* `full` fine-tuning is a card question, not a modelling one, and it is the
  stage most likely to overfit 71 rows; it should be re-tried with the bubble's
  models released before it is read as a capability limit.

### Stated limits

A MINED label is what the 12B model DID, not intent — one of the eight real rows
is "It's time for play music" labelled `system` because that is the tool the app
reached for, where a person would say media. That is why the mined set is
reported separately and never merged into the authored number. The authored rows
are my writing, not a sample of this user's speech. Folds of 10–21 rows make the
per-fold spread the error bar, so the min/max is printed beside every mean.
The state an utterance is rendered into is the harness's own `{"utterance": …}`
JSON, in training and grading alike: parity, but a plainer encoding might lift
both sides and would invalidate the earlier baseline, so it was not changed
mid-flight. `--stage last` was tried at four layers only (not swept), one
checkpoint was saved, and the encoder-adapted model's held-out grade rests on a
single 21-row fold.

The corpus store also counts MINING PASSES, not user turns (`count` 5 today
because this pass ran the miner repeatedly) — it is a re-seen counter, and
nothing should read it as usage.

## The corpus grows without a command: the app records the turn, the queue is folded on read (2026-09-21)

The fine-tune round ended with "the corpus has to grow about tenfold first", and
the store it grows into was fed by a hand-run `--grow` pass over old
`history.json` files. That has two defects a measurement cannot fix: the pass is
a command somebody has to remember, and history is TRIMMED — the turns most
worth learning from are exactly the ones that age out of it. This round moved the
recording to the moment a turn completes.

**What the app records, and what it deliberately does not.** Every completed turn
is appended to a queue in the app's state directory (`laya-turns.jsonl`, 0600):
the utterance, capped at 400 characters, and the TOOL NAMES the turn called.
No family. The family table is the harness's (`FAMILIES`), and it is not the
belt's gate map — `run_command` alone spans the `system` and `windows` families —
so an app-side label would be a second, drifting copy of a vocabulary it cannot
derive. The queue therefore holds facts the app owns, and
`ci/laya_corpus.py` derives the label where the table lives.

**The fold is a side effect of reading, not a command.** `build()` — what
`--report`, `--dump` and the fine-tune all call — folds whatever the queue holds,
merges it into the store (dedupe, `count`, conflicts), and advances a cursor.
`--grow` remains as the history BACKFILL, which the queue cannot replace: those
old turns are only in `history.json`.

**The label rule is one function, used by both paths.** `mine_real` (history) and
the queue fold both ask the harness's `label_of`, and the fold reports what it
could not use through the harness's `why_unlabelled` — so the accounting cannot
disagree with the rule that produced it. What that rule refuses is worth naming,
because it is not new: a turn that called NOTHING (it answered directly, which
the authored set already covers, and admitting chat would swamp the routing
task), a turn that called a tool no family owns, and a turn spanning TWO families
(not one routing decision but two). All three are recorded and COUNTED, not
dropped, and the report names the reason rather than a total: across the first two
probe queues on this machine, **4 of 7 recorded turns were unusable — three
because they called nothing and one because it spanned two families** — which is
the corpus saying its option set is narrower than the app's behaviour, not a bug
in the fold.

**Two findings from building it, both worth more than the feature.**

* A line-count cursor has a SILENT failure mode. Round one tracked "lines
  already folded" as a number, and a hand-replaced queue with the same line count
  — or a longer one — leaves the cursor ahead of the new file, so every later
  turn is skipped forever while health reports a healthy queue. Found by trying
  the case rather than reasoning about it. The cursor now carries a fingerprint
  of the lines it consumed; a queue that does not match it is treated as a
  replaced one and refolded from the start, which the store's dedupe absorbs.
  That is also what makes deleting the queue by hand SAFE instead of silently
  fatal.
* The suite's checkout-write guard caught a real leak in the same cut: `build()`
  with a scratch store still wrote the DEFAULT cursor into the developer's real
  state directory, because the queue and the cursor defaulted independently of
  the store. They now come from ONE directory — a scratch store means a scratch
  queue — and a missing queue writes no cursor at all.

**What `--ptt health` says now** (`laya_corpus` in the snapshot): `turns_recorded`
— the running count of completed turns the app has queued — `turns_pending`, what
the next corpus read folds in, and `grown_rows`, what the store holds after
dedupe. None of the three is computed by running the corpus: the app reads two
JSON-lines files and a cursor.

**Stated limits.** The queue is append-only and unbounded by design — it is the
record that a fold has NOT happened yet, so trimming it is what would lose a turn
whose fold raced the app's write; at ~150 bytes a turn it is the smallest file in
the state directory, and deleting it by hand is safe (see above). A turn that
aborted, was cancelled or produced nothing is not recorded: "completed" is the
gate, and an aborted turn has no choice to learn from. The store's `count` now
means "times the user really said this" for queue rows, while a history-mined row
still bumps once per `--grow` pass — the old re-seen caveat, now confined to the
backfill path. And the recording is per TURN, so a turn that called one tool four
times contributes one row: the decision being learned is the family choice, not
the call volume.

**A third finding, from running the gates rather than the feature: the control
socket's own request bound could overshoot by one `recv`.** The read loop checked
`_CONTROL_REQUEST_MAX` BEFORE reading and then asked for a fixed 64 KiB, so a
request delivered in pieces could land one chunk past the ceiling. It surfaced as
a single red test under a loaded full-suite run (101996 bytes buffered for a
65536-byte bound) while passing in isolation — the shape of a flake, and it was
not one: the test was right about the bound. The loop now asks for what is LEFT
of the ceiling, which makes the bound exact and that test deterministic.
