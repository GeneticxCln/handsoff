#!/usr/bin/env bash
# The CI gates, run LOCALLY — same jobs, same order, same environment.
#
# Why this exists: the pipeline's gates are the project's definition of "green",
# but the pipeline is a remote resource with a finite quota, and a run that
# cannot happen is not a gate at all. This script runs the same jobs a
# developer can run here, using the same env vars (.gitlab-ci.yml's coverage
# job sets COVERAGE_PROCESS_START/COVERAGE_FILE — omitting them is what made an
# earlier local measurement read 62% instead of 82%), and prints the same
# failure digest the pipeline's after_script prints.
#
# What it deliberately does NOT do: install anything. CI's apt/pip layers exist
# because it starts from a bare python:-slim image; this runs on the developer's
# own machine, where those dependencies are already the ones the bubble uses.
# A missing one is reported by name instead of being silently pip-installed
# over the environment you are debugging in.
#
# The last gate is `two-writer`, and it is not about the code: this checkout is
# SHARED (an editor, another agent, a `git checkout`), the suite takes minutes,
# and a verdict about a tree that moved while it ran is a false red or a false
# green — the audit's §1 complaint. So the worktree is stamped before the first
# gate, CHECKPOINTED after every gate, and compared after the last one.
#
# A tree that moves mid-run does not throw the run away any more. The collided
# gate's attempt is DISCARDED (its window contains the write, so its result is
# about a mixture), the baseline moves onto the state the collision left, and the
# gate is RE-RUN against it, so the gates after it are judged against one tree.
# The gates BEFORE the collision keep their rows — they describe the tree as it
# was — and the summary says which verdicts describe which tree instead of
# pretending one revision sits behind all of them. A tree that moves AGAIN inside
# the same gate's window stops the run: nothing can be resumed about a tree
# somebody is still writing to.
#
# ci/worktree_stamp.py is the stamp; a directory that is not a git worktree gets
# no opinion (SKIP) rather than a clean bill.
#
# Usage:
#   bash ci/gates.sh                  every gate, in CI's order
#   bash ci/gates.sh --no-order       skip the two ordering re-runs (~5 min)
#   bash ci/gates.sh tests shell      only the named gates
#   bash ci/gates.sh --help
#
# Exit status is 0 only when every gate that ran passed. A gate that cannot run
# on this machine (pytest-cov missing) is SKIPped and said so, because a gate
# that silently passes because it never ran is the failure mode this whole
# script exists to avoid.
#
# A re-run is also compared with the attempt it replaces, and the comparison is
# printed: the same gate, on two trees, giving two answers is the proof that the
# move changed the outcome — where an unchanged answer is evidence that the tree
# the run lost was not the difference. A reader diffing two rows by eye cannot
# tell either from a re-run that never happened.
#
# `clean-checkout` is not about this checkout at all: it lays HEAD down in a
# scratch worktree and runs the freshness guard THERE, because that guard reads
# the test plan, the key census and the spec citations out of files — and this
# checkout can see files that are not committed. A commit whose plan names a test
# file it does not contain passes every gate above and fails the moment anybody
# clones it. Local only, like the stamp: a pipeline checkout is already clean.

set -u

cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD

PYTHON=${PYTHON:-python3}
# CI shuffles with the commit SHA (a different seed every commit, printed by the
# run banner so a red build is reproducible). Locally the same idea: a seed
# derived from the checkout, overridable, and stable for a given tree.
SEED=${HANDSOFF_ORDER_SEED:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}
JUNIT="$ROOT/tests/report.xml"
FIRST_FAILURE="$ROOT/tests/report.first-failure.xml"

ALL_GATES="tests order coverage compile lint links shell smoke clean-checkout two-writer"
WANT_ORDER=1
declare -a WANTED=()
# Which gate failed FIRST. The failure digest below reads the first failing
# junit report, and the two-writer gate fails without ever writing one —
# printing the tests report under a refusal would explain the wrong failure.
FIRST_FAILED=""

usage() {
    # The header, through the last line of the usage list (line 41 — the range has
    # to be kept in step with the comment block it prints).
    sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --no-order) WANT_ORDER=0 ;;
        -*) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
        *) WANTED+=("$1") ;;
    esac
    shift
done
if [ ${#WANTED[@]} -eq 0 ]; then
    for g in $ALL_GATES; do WANTED+=("$g"); done
fi
for g in "${WANTED[@]}"; do
    case " $ALL_GATES " in
        *" $g "*) ;;
        *) echo "unknown gate: $g (known: $ALL_GATES)" >&2; exit 2 ;;
    esac
done
if [ "$WANT_ORDER" = 0 ]; then
    declare -a KEPT=()
    for g in "${WANTED[@]}"; do [ "$g" = order ] || KEPT+=("$g"); done
    WANTED=("${KEPT[@]}")
fi

declare -a REPORT=()
FAILED=0
# Set when a collision inside one gate's window ends the run (see refuse_moving),
# when a collision RESUMED the run, which gates the catch-up re-ran, and how many
# times the baseline has MOVED. The leg is what the summary splits on: a verdict
# from the current leg is about the tree this run is judging, one from an earlier
# leg is history — and a gate re-run after the move is current even when the
# collision that prompted it happened two collisions ago.
STOP=0
RESUMED=0
CAUGHT_UP=""
LEG=1
START_ALL=$SECONDS
# Every attempt's RESULT, in order, as `gate|status` — a discarded attempt
# included, because it produced one, and the whole point of the record is to hold
# it up against the attempt that replaced it.
ATTEMPTS=""
# Set when the two-writer gate is part of this run: the directory the per-gate
# checkpoints live in, outside the worktree. The checkpoints are also what a
# collision is detected by, so this is what makes a resume possible.
STAMP_DIR=""

# Never explain THIS run with the PREVIOUS one's red: the digest reads the first
# failing junit report, and a leftover copy from an earlier session describes a
# different tree — found live, where the digest said "1 failed of 1617" while
# this run collected 1676. Same reason the coverage gate prunes its shards.
rm -f "$JUNIT" "$FIRST_FAILURE"

# The first checkpoint, taken BEFORE the first gate runs (the point of the
# comparison is everything the run did in between). Only when the gate that
# reads it is part of this run: stamping a repo for a gate nobody asked for is
# work for nothing.
for g in "${WANTED[@]}"; do
    [ "$g" = two-writer ] || continue
    STAMP_DIR=$(mktemp -d -t handsoff-stamp-XXXXXX) || STAMP_DIR=""
    if [ -n "$STAMP_DIR" ]; then
        "$PYTHON" ci/worktree_stamp.py --root "$ROOT" --checkpoint "$STAMP_DIR" \
            --label start \
            || echo "could not stamp the worktree — the two-writer gate will SKIP"
    fi
done

# The checkpoints live in /tmp, never in the worktree: a snapshot written INSIDE
# the tree it fingerprints would move the tree it is measuring.
#
# Each snapshot carries the clock it was taken at, so a checkpoint can also say
# roughly WHERE INSIDE its gate's window the write landed: the changed file's own
# last-write time placed between the two checkpoints, printed as `when:` lines
# under the gate's heading. Roughly, and the resumed section says so.
drop_stamp() { [ -n "$STAMP_DIR" ] && rm -rf "$STAMP_DIR" ; return 0 ; }

# One checkpoint per gate, labelled with the gate that just ran. Cheap, and it is
# the only way to answer the question a collision provokes: WHICH gate was it?
#
# The STATUS is deliberately not swallowed: 0 unchanged, 1 the tree moved inside
# this gate's window, 2 no opinion (not a git worktree). 1 is what the caller
# resumes on, so the stamp's last command has to BE the return value — a
# `return 0` after it would report every collision as "unchanged" and turn this
# whole feature off silently.
checkpoint_gate() {
    [ -n "$STAMP_DIR" ] || return 2
    [ "$1" = two-writer ] && return 0     # its own comparison is the last word
    "$PYTHON" ci/worktree_stamp.py --root "$ROOT" --checkpoint "$STAMP_DIR" \
        --label "$1" > /dev/null
}

# Move the baseline onto the state the collision left, because the resumed leg is
# judged against that tree and not against the one the run started with. If it
# cannot happen (no snapshot to move onto) the run does not claim to have resumed:
# the end comparison refuses exactly as it did before this feature existed.
rebase_stamp() {   # gate
    [ -n "$STAMP_DIR" ] || return 1
    "$PYTHON" ci/worktree_stamp.py --root "$ROOT" --rebase "$STAMP_DIR" \
        --label "$1" > /dev/null
}

record() {   # name, status, seconds, note
    REPORT+=("$1|$2|$3|$4|$LEG")
    [ "$2" = FAIL ] && FAILED=1
    return 0
}

# One row, spelled once: the line printed under the gate as it happens and the
# line in the summary are the same format, so the two cannot drift.
print_row() {   # name, status, seconds, note
    printf '%-10s %-4s %ss%s\n' "$1" "$2" "$3" "${4:+   ($4)}"
}

# The leg a gate's LAST KEPT verdict came from — "" when it has none. A VOID row
# is a discarded attempt, not a verdict, and a gate re-run later supersedes the
# row it re-ran: the LAST one is what the run stands behind.
gate_leg() {   # gate
    local want="$1" row name status _ _ leg found=""
    for row in "${REPORT[@]}"; do
        IFS='|' read -r name status _ _ leg <<<"$row"
        [ "$name" = "$want" ] && [ "$status" != VOID ] && found="$leg"
    done
    printf '%s' "$found"
}

# The run's kept verdicts, split by LEG rather than by which collision was last: a
# verdict from the current leg is about the tree this run is judging (AFTER), one
# from an earlier leg was overtaken by a move (CARRIED). The leg is what makes the
# catch-up report itself honestly — the gate it just re-ran is current, while the
# gate a LATER write overtook is not, however recently it ran.
split_verdicts() {   # → AFTER, CARRIED
    AFTER=""; CARRIED=""
    local g leg
    for g in "${WANTED[@]}"; do
        leg=$(gate_leg "$g")
        [ -n "$leg" ] || continue
        if [ "$leg" = "$LEG" ]; then AFTER="$AFTER $g"; else CARRIED="$CARRIED $g"; fi
    done
    AFTER="${AFTER# }"; CARRIED="${CARRIED# }"
}

# Mark the row the catch-up supersedes. The run no longer stands behind it, and a
# summary showing two rows for one gate would leave the reader to guess which is
# the verdict — the note says which, and the new row is written below it.
supersede() {   # gate
    local want="$1" i row name status secs note leg last=""
    for i in "${!REPORT[@]}"; do
        IFS='|' read -r name status secs note leg <<<"${REPORT[$i]}"
        [ "$name" = "$want" ] && [ "$status" != VOID ] && last="$i"
    done
    [ -n "$last" ] || return 0
    IFS='|' read -r name status secs note leg <<<"${REPORT[$last]}"
    REPORT[$last]="$name|$status|$secs|superseded — the catch-up re-ran it against the tree as it now stands|$leg"
}

# Did the move CHANGE anything? A gate that ran twice did so because its earlier
# attempt was a mixture, so the two RESULTS are the two facts worth holding up
# against each other: the same gate, the same suite, two trees, two answers is
# proof the move was the difference, and one answer across every attempt is
# evidence it was not. Stated, because two ROWS in a summary can be diffed by eye
# while the results of one gate across trees cannot. More than two attempts keep
# only the changes — `PASS → FAIL → PASS` says a re-run answered as the first one
# did after a move that flipped it, which a first-against-last reading erases.
compare_attempts() {   # → CHANGED ("gate PASS → FAIL"), RERAN (gates run twice)
    CHANGED=""
    RERAN=""
    local g seen distinct last seq row name status
    for g in "${WANTED[@]}"; do
        seen=0
        distinct=0
        last=""
        seq=""
        for row in $ATTEMPTS; do
            IFS='|' read -r name status <<<"$row"
            [ "$name" = "$g" ] || continue
            seen=$((seen + 1))
            # Only a CHANGE of result goes in the sequence: three attempts that
            # all passed read `PASS`, not `PASS → PASS → PASS`.
            if [ "$status" != "$last" ]; then
                distinct=$((distinct + 1))
                seq="$seq → $status"
                last="$status"
            fi
        done
        [ "$seen" -gt 1 ] || continue
        RERAN="$RERAN $g"
        [ "$distinct" -gt 1 ] || continue
        CHANGED="$CHANGED, $g ${seq# → }"
    done
    RERAN="${RERAN# }"
    CHANGED="${CHANGED#, }"
}

# What a resumed run has to say for itself: the collision (which file moved under
# which gate), WHAT IT CHANGED (the verdicts that differ between an attempt and its
# re-run), which verdicts are about the tree as it now stands, and which were
# carried over from before the move. A section, not a refusal — the point of
# resuming is that the run still has a verdict to give.
report_resume() {
    printf '\n============ resumed ============\n'
    if [ "$STOP" = 1 ]; then
        printf 'the worktree moved while the gates were running, and moved again before the run\n'
        printf 'could certify that window — so it stopped rather than report a mixture:\n'
    else
        printf 'the worktree moved while the gates were running, so the run resumed from the\n'
        printf 'collision instead of refusing the whole thing:\n'
    fi
    cat "$STAMP_DIR/moves.txt"
    # The `when:` lines are a PLACEMENT, not an observation: nobody watches the
    # tree, so the stamp places each changed file's own last-write time inside
    # the window the two checkpoints bracket. Say so where a reader meets them,
    # or "96s into the window" reads as something the gate saw happen.
    if grep -q -- '  - when: ' "$STAMP_DIR/moves.txt" 2>/dev/null; then
        printf "(each when: line places the changed file's own last-write time inside that gate's\n"
        printf " window, so it is roughly which part of the gate the write landed in — not an instant)\n"
    fi
    [ -n "$CAUGHT_UP" ] && printf 'caught up (re-run against the tree as it now stands): %s\n' "$CAUGHT_UP"
    compare_attempts
    if [ -n "$CHANGED" ]; then
        printf 'the move changed the outcome: %s\n' "$CHANGED"
    elif [ -n "$RERAN" ]; then
        printf 'every re-run reached the result its earlier attempt did (%s) — the move\n' "$RERAN"
        printf 'changed no verdict.\n'
    fi
    split_verdicts
    if [ -n "$AFTER" ]; then
        printf 'verdicts about the tree as it now stands: %s\n' "$AFTER"
    else
        printf 'no verdict survived: every attempt under the collided gate straddled a write\n'
    fi
    if [ -n "$CARRIED" ]; then
        printf 'carried over (they describe the tree BEFORE the move): %s\n' "$CARRIED"
    else
        printf 'carried over: none — no verdict above describes an earlier tree\n'
    fi
}

# A second write inside the SAME gate's window: the re-run is evidence about a
# mixture too, so there is nothing left to resume. The run stops rather than
# looping on a tree somebody is writing to, and the remaining gates are not run at
# all — no verdict about this tree is available until the writing stops.
refuse_moving() {   # gate
    printf '\nREFUSED — the worktree moved again while the %s gate was re-running, so there is\n' "$1"
    printf 'nothing left to resume: a tree that keeps moving cannot be certified. Stop the\n'
    printf 'other writer and run the gates again on a still tree; the remaining gates were\n'
    printf 'not run.\n'
    FAILED=1
    [ -n "$FIRST_FAILED" ] || FIRST_FAILED=two-writer
    STOP=1
}

# Keep the FIRST failing report: later gates overwrite tests/report.xml, and the
# digest is worth most for whatever failed first (the pipeline stops there for
# the same reason).
stash_report() {
    [ -f "$JUNIT" ] || return 0
    [ -f "$FIRST_FAILURE" ] && return 0
    cp "$JUNIT" "$FIRST_FAILURE"
}

# The suite runs that feed the junit report. `--junitxml` is not decoration:
# the digest on failure reads that file, exactly like the pipeline's.
pytest_run() {   # env-assignments..., then pytest args
    # `-rf` on top of CI's `-q`: the pipeline's failure list lives in the junit
    # report's Test-summary tab, which a local run does not have. The short
    # summary is that tab's terminal equivalent, and the digest below it is the
    # same one the pipeline's after_script prints.
    env QT_QPA_PLATFORM=offscreen "$PYTHON" -m pytest tests/ -q -rf \
        --junitxml="$JUNIT" "$@"
}

gate_tests() {
    pytest_run
}

# The suite must not care what order it runs in; the default collection order is
# what hides a dependence on it. Two full re-runs: tests shuffled, then only the
# file order shuffled.
gate_order() {
    HANDSOFF_TEST_ORDER_SEED="$SEED" pytest_run || return 1
    HANDSOFF_TEST_ORDER_FILES="$SEED" pytest_run
}

gate_coverage() {
    if ! "$PYTHON" -c "import pytest_cov" 2>/dev/null; then
        echo "pytest-cov is not installed for $PYTHON — cannot measure the floor."
        echo "install it the way CI does:  $PYTHON -m pip install pytest-cov"
        return 2
    fi
    # Stale parallel shards from an earlier run would be combined into this
    # number. Never `.coverage*`: that glob also matches the tracked
    # `.coveragerc`, and deleting it is a config error wearing a coverage
    # failure's clothes.
    rm -f "$ROOT/.coverage" "$ROOT"/.coverage.*
    COVERAGE_PROCESS_START="$ROOT/.coveragerc" COVERAGE_FILE="$ROOT/.coverage" \
        pytest_run --cov=. --cov-config=.coveragerc \
        --cov-report=term-missing --cov-fail-under=70
}

gate_compile() {
    "$PYTHON" ci/compile_all.py
}

# The lint gate. It exists because there was none: a 41,000-line tree with no
# linter is a tree where an unused import, a shadowed name and a `zip()` that
# silently drops a row are all indistinguishable from style, and all three were
# present. The rule set is pinned in ruff.toml and deliberately small — a gate
# that opens with 200 findings nobody reads is decoration.
#
# SKIPs (exit 2, not a pass) when ruff is not installed, because a gate that
# silently succeeds because it never ran is the exact failure this script
# exists to avoid. Install it with: pip install --user 'ruff==0.16.4'
gate_lint() {
    local ruff
    if ! ruff=$(command -v ruff 2>/dev/null); then
        echo "ruff is not on PATH — cannot lint."
        echo "  install: pip install --user 'ruff==0.16.4'"
        return 2
    fi
    # The production sources, spelled out here as well as in ruff.toml so the
    # gate says what it covered even when ruff says nothing at all.
    "$ruff" check --no-cache \
        handsoff.py handsoff-settings.py hardware.py settings_schema.py \
        core/ ci/
}

# A heading link is the only link that fails SILENTLY: rename a heading and
# markdown does not complain — the reader clicks and nothing happens. 137 of
# the 138 anchor links in this repository are the two ledgers' generated
# indexes, so this is the gate that notices a rename stranding a quarter of
# the navigation. It is cheap (stdlib, no network, ~1 s over 1.5 MB of
# markdown) and therefore never SKIPs: there is no dependency to be missing.
gate_links() {
    "$PYTHON" ci/link_check.py
}

gate_shell() {
    local rc=0
    # GitHub side: action refs must stay immutable SHAs.
    if grep -nE 'uses: [^ ]+@v[0-9]+' .github/workflows/ci.yml; then
        echo "ERROR: GH action refs must be pinned to immutable SHAs."
        rc=1
    fi
    # GitLab side: image refs must stay digest-pinned (variable refs OK).
    local bad
    bad=$(grep -nE '^[[:space:]]*image:' .gitlab-ci.yml | grep -v '@' || true)
    if [ -n "$bad" ]; then
        echo "ERROR: CI image refs must be digest-pinned (tag in a comment):"
        echo "$bad"
        rc=1
    fi
    # Every shell script DISCOVERED, not listed — and discovered by SHEBANG,
    # because `handsoff-restart` has no extension: an extension glob misses
    # exactly the file the old hand-written list remembered to include, and the
    # two workflows' lists had already drifted from each other. Only the FIRST
    # line is read, so a shebang quoted inside a fixture cannot drag a Python
    # file into `bash -n` and turn this gate into a false alarm.
    local f first list
    list=$(mktemp) || return 1
    # The find is pruned: without this a virtualenv or node_modules INSIDE the
    # repo got every one of its files probed, and a multi-GB model blob or .wav
    # had its first line read for nothing.
    find . -type f \
        -not -path './.git/*' -not -path './attic/*' \
        -not -path './.freebuff/*' -not -path './.claude-flow/*' \
        -not -path './.swarm/*' -not -path './.agents/*' \
        -not -path './.codex/*' \
        -not -path './.venv/*' -not -path './venv/*' \
        -not -path './env/*' -not -path './node_modules/*' \
        -not -path './build/*' -not -path './dist/*' \
        -not -path './.tox/*' -not -path './.mypy_cache/*' \
        -not -path './site-packages/*' | sort > "$list"
    while IFS= read -r f; do
        # Read at most a shebang's worth with `head -c`, never bash's `read`:
        # `read` consumes until a NEWLINE, so a binary with none is pulled into
        # a shell variable whole. 128 bytes is exact for line 1. `tr -d '\000'`
        # is not cosmetic: command substitution that captures a NUL warns per
        # binary file, so a repo with model blobs would print pages of noise.
        first=$(head -c 128 -- "$f" 2>/dev/null | tr -d '\000' | head -n 1) || continue
        case "$first" in
            '#!'*bash*|'#!'*'/sh'*) ;;
            *) continue ;;
        esac
        echo "--- bash -n $f"
        bash -n "$f" || rc=1
    done < "$list"
    rm -f "$list"
    return $rc
}

gate_smoke() {
    local out
    out=$(bash install.sh --help) || return 1
    echo "$out" | grep -q "Options:" || return 1
    echo "$out" | grep -q -- "--uninstall"
}

# Does a fresh checkout of HEAD agree with itself? The freshness guard reads the
# plan, the key census and the spec citations out of FILES, and in this shared
# checkout it can see work that is not committed — so the one tree nobody ever
# looks at, the committed one, is the one this gate looks at. Found live: bd9cf5a
# was green here and had THREE red guards in a clean checkout (the key census,
# the test plan's file list, and a date read as a module size).
#
# A WORKTREE of HEAD rather than a copy of this one, because a worktree is what a
# reader gets from `git clone` of that commit without a second download. The
# directory is outside the tree — a scratch copy written INSIDE the worktree would
# move the tree the stamp is watching — and it is removed on EVERY path, the
# refusal included, so `git worktree list` never grows a graveyard.
#
# SKIP (2) when there is nothing to lay down (not a git worktree, no commit yet,
# no such guard in HEAD, or git could not make the worktree): a gate that cannot
# run is SAID to be skipped, never counted as a pass.
gate_clean_checkout() {
    local guard="tests/test_specs_freshness.py" dir tree out="" rc=0
    if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "not a git worktree — HEAD cannot be laid down here."
        return 2
    fi
    if ! git -C "$ROOT" rev-parse --verify --quiet HEAD >/dev/null; then
        echo "HEAD has no commit yet — there is no committed tree to check."
        return 2
    fi
    dir=$(mktemp -d -t handsoff-clean-XXXXXX) || {
        echo "could not make a scratch directory for the clean checkout."
        return 2
    }
    tree="$dir/tree"
    if ! git -C "$ROOT" worktree add --detach --quiet "$tree" HEAD >/dev/null 2>&1; then
        rm -rf "$dir"
        echo "could not lay down a clean checkout of HEAD (git worktree add failed)."
        return 2
    fi
    if [ -f "$tree/$guard" ]; then
        out=$(cd "$tree" && QT_QPA_PLATFORM=offscreen "$PYTHON" -m pytest "$guard" -q 2>&1)
        rc=$?
    else
        rc=2
    fi
    # Cleaned up BEFORE any verdict is printed: a leftover worktree pollutes the
    # next run's `git worktree list` (and the developer's), and a refusal is worth
    # nothing if reporting it damages the checkout it reports about.
    git -C "$ROOT" worktree remove --force "$tree" >/dev/null 2>&1 || rm -rf "$tree"
    rm -rf "$dir"
    git -C "$ROOT" worktree prune >/dev/null 2>&1
    if [ "$rc" = 2 ]; then
        echo "HEAD carries no $guard — this checkout has nothing to check."
        return 2
    fi
    if [ "$rc" != 0 ]; then
        printf '%s\n' "$out" | tail -n 25
        echo
        echo "REFUSED — this HEAD does not pass its own freshness guard in a clean"
        echo "checkout: the plan, the key census or a spec citation disagrees with the"
        echo "committed tree, or the guard could not run there (its output is above)."
        echo "THIS checkout can hide it: the files the guard asks about may be here"
        echo "and uncommitted, which is how this gate's class goes unnoticed."
        echo "Reproduce with:"
        echo "  git worktree add --detach /tmp/x HEAD && (cd /tmp/x && $PYTHON -m pytest $guard -q)"
        return 1
    fi
    printf '%s\n' "$out" | tail -n 1
    return 0
}

# The verdict on the TREE, after every gate has run. A collision is not a refusal
# any more — the run resumed at it, and `report_resume` says which verdicts that
# left about which tree — so moves.txt is NOT read here: refusing on it would
# refuse every resumed run, which is the behaviour this gate no longer has. What
# it does is compare the tree against the baseline, which after a resume is the
# state the collision left. That is what still catches the one move no gate can
# absorb: a write that lands after the LAST checkpoint, when there is no gate left
# to re-run. The stamp prints the refusal (and names the files) in that case.
gate_two_writer() {
    [ -n "$STAMP_DIR" ] || return 2             # nothing stamped: no opinion
    # The window the timing lines are placed in starts at the LAST checkpoint,
    # not at the baseline: the write THIS comparison catches is the one no gate
    # saw (it landed after the final checkpoint), and a whole-run window would
    # report it as "4 800s into the run" — true, and no use to anybody. The
    # VERDICT is still against the baseline, which is what a resumed run judges.
    local since=()
    [ -f "$STAMP_DIR/latest.json" ] && since=(--since "$STAMP_DIR/latest.json")
    "$PYTHON" ci/worktree_stamp.py --root "$ROOT" --compare "$STAMP_DIR/first.json" \
        ${since[@]+"${since[@]}"}
}

# ONE ATTEMPT at a gate: the banner, the gate itself, its verdict — and NO row.
# The row is written by the caller only after the checkpoint has had its say,
# because an attempt whose window contains a write is discarded rather than
# reported: a row for it would be a verdict about a tree that existed for part of
# its run and not the rest. (The failure digest is deferred for the same reason —
# a red from a discarded attempt explains a tree the run is not judging.)
attempt_gate() {   # name, banner suffix
    local name="$1" suffix="${2:-}" start=$SECONDS status=PASS
    printf '\n──────────────── %s%s ────────────────\n' "$name" "$suffix"
    # A gate's name is what the user types on the command line (`two-writer`),
    # and a bash function cannot be asked for by that name with a hyphen in
    # front of it cleanly — so the mapping to `gate_two_writer` lives here, in
    # one place, instead of in every gate's own spelling.
    "gate_${name//-/_}"
    case $? in
        0) status=PASS ;;
        2) status=SKIP ;;
        *) status=FAIL ;;
    esac
    GATE_SECS=$((SECONDS - start))
    GATE_STATUS=$status
}

# One gate, run until the run can KEEP a verdict for it. A checkpoint that sees
# the tree move no longer throws the run away: the collided attempt is discarded,
# the baseline moves onto the state the collision left, and the gate is RE-RUN
# against it, so every gate after it is judged against one tree. The gates before
# the collision keep their rows — they describe the tree as it was, and the
# summary says so rather than pretending one revision sits behind all of them.
# One re-run per gate is the whole budget: a window that held TWO writes is not a
# window anything can be certified about, so the run stops there.
run_gate() {   # name, phase ("catch-up" when a move overtook this verdict)
    local name="$1" phase="${2:-}" attempt=1 status secs checkpoint=0 redo="" note=""
    while :; do
        redo=""
        note=""
        if [ "$attempt" -gt 1 ]; then
            redo=" (re-run — the tree moved)"
            note="re-run after the move"
        elif [ "$phase" = catch-up ]; then
            redo=" (catch-up — the move overtook it)"
            note="caught up after the move"
        fi
        attempt_gate "$name" "$redo"
        status=$GATE_STATUS
        secs=$GATE_SECS
        # Recorded whoever keeps or discards it (below): a discarded attempt is
        # not a verdict, and it is still half of the comparison.
        ATTEMPTS="$ATTEMPTS $name|$status"
        checkpoint=0
        checkpoint_gate "$name" || checkpoint=$?
        if [ "$checkpoint" != 1 ]; then
            # Kept: this attempt's window held no write.
            if [ "$status" = FAIL ]; then
                [ -n "$FIRST_FAILED" ] || FIRST_FAILED="$name"
                stash_report
            fi
            record "$name" "$status" "$secs" "$note"
            print_row "$name" "$status" "$secs" "$note"
            return 0
        fi
        note="discarded — it had $status; the tree moved during the attempt"
        record "$name" VOID "$secs" "$note"
        print_row "$name" VOID "$secs" "$note"
        if [ "$attempt" -gt 1 ]; then
            refuse_moving "$name"
            return 0
        fi
        if rebase_stamp "$name"; then
            RESUMED=1
            LEG=$((LEG + 1))
        else
            printf 'the baseline could not be moved onto the state the collision left —\n'
            printf 'the run cannot resume, so the final comparison will refuse it.\n'
        fi
        attempt=2
    done
}

# Re-run the verdicts a move overtook, in run order, against the tree the run is
# judging now. This is what makes a resumed run END as a verdict about one tree
# instead of a report that says "these rows describe an earlier tree" — the point
# of the whole feature, since a partial verdict is exactly what nobody acts on.
#
# Bounded and conditional on purpose:
#   * only when the run has NO failure to report yet. A red is already a verdict
#     (the rows say which tree each one is about), and the failure bookkeeping —
#     which junit report the digest prints — is built for ONE tree's worth of red,
#     so catching up a red run would have to unpick that. Fix the red and run
#     again; the catch-up is for completing a green.
#   * ONE pass. A write inside the catch-up makes the gates re-run before it stale
#     again, and that is reported rather than chased: the split is by leg, so the
#     summary says exactly which verdicts are about which tree.
#   * each gate keeps its own budget inside run_gate (one re-run per window).
catch_up() {
    split_verdicts
    [ -n "$CARRIED" ] || return 0
    if [ "$FAILED" = 1 ]; then
        printf '\nthe move overtook these verdicts: %s\n' "$CARRIED"
        printf 'catch-up skipped: this run has a failure to report, and the rows above say\n'
        printf 'which tree each verdict is about — fix the red and run again.\n'
        return 0
    fi
    printf '\nthe run is catching up: re-running the verdicts the move overtook (%s)\n' "$CARRIED"
    printf 'so this run ends as a verdict about one tree rather than two.\n'
    local g
    for g in $CARRIED; do
        supersede "$g"
        run_gate "$g" catch-up
        CAUGHT_UP="$CAUGHT_UP $g"
        [ "$STOP" = 1 ] && { CAUGHT_UP="${CAUGHT_UP# }"; return 0; }
    done
    CAUGHT_UP="${CAUGHT_UP# }"
}

printf 'handsoff local gates — %s, order seed %s\n' "$PYTHON" "$SEED"

# The tree verdict is last, whatever was asked for: it compares the tree against
# the baseline, and anything running after it would invalidate that comparison. So
# it is held back here and run after the catch-up pass.
TREE_GATE=""
declare -a BODY=()
for g in "${WANTED[@]}"; do
    if [ "$g" = two-writer ]; then TREE_GATE="$g"; else BODY+=("$g"); fi
done

for g in "${BODY[@]}"; do
    run_gate "$g"
    [ "$STOP" = 1 ] && break
done

[ "$STOP" = 1 ] || catch_up

if [ -n "$TREE_GATE" ] && [ "$STOP" != 1 ]; then
    run_gate "$TREE_GATE"
fi

printf '\n============ summary ============\n'
for row in "${REPORT[@]}"; do
    # The leg is the last field and is not printed: it is what the split is
    # computed from. But a verdict from an earlier leg is MARKED where it is read
    # — a bare row in a list invites being read as current, and the resumed
    # section is further down.
    IFS='|' read -r name status secs note leg <<<"$row"
    if [ -z "$note" ] && [ "$leg" != "$LEG" ]; then note="before the move"; fi
    print_row "$name" "$status" "$secs" "$note"
done
printf 'total %ss\n' "$((SECONDS - START_ALL))"

# A resumed run has to say which of its verdicts are about which tree: the
# summary above reads like one run, and only this section can tell a reader that
# the first rows of it describe a tree that no longer exists.
[ "$RESUMED" = 1 ] && report_resume

if [ "$FAILED" = 1 ]; then
    printf '\n============ failures ============\n'
    # The digest is the SUITE's digest, so it is printed for a suite gate only.
    # Under any other first failure it would be a report about a gate that
    # passed, which is how a refusal gets read as a test failure.
    case "$FIRST_FAILED" in
        tests|order|coverage)
            if [ -f "$FIRST_FAILURE" ]; then
                # Same digest the pipeline's after_script prints, minus the MR comment.
                "$PYTHON" ci/pytest_summary.py "$FIRST_FAILURE" || true
            else
                echo "a gate failed before any junit report was written (compile/lint/links/shell/smoke)."
            fi
            ;;
        *)
            echo "first failure: the $FIRST_FAILED gate (its own message is above)."
            ;;
    esac
    drop_stamp
    exit 1
fi
drop_stamp
# The verdict, qualified by what the resume left unresolved: with verdicts carried
# over from before the move, "all gates passed" would be a claim about ONE tree
# that this run cannot make.
if [ "$RESUMED" = 0 ]; then
    printf '\nall gates passed\n'
elif [ -n "$CARRIED" ]; then
    printf '\nevery gate passed, but the carried-over verdicts describe the earlier tree\n'
    printf '(see resumed above).\n'
else
    printf '\nall gates passed — the run resumed, and every verdict describes the tree as it\n'
    printf 'now stands.\n'
fi
