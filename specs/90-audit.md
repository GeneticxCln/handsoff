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
