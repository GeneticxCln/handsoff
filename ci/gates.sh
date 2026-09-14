#!/usr/bin/env bash
# The CI gates, run LOCALLY — same jobs, same order, same environment.
#
# Why this exists: the pipeline's gates are the project's definition of "green",
# but the pipeline is a remote resource with a finite quota, and a run that
# cannot happen is not a gate at all. This script runs the same six jobs a
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

ALL_GATES="tests order coverage compile shell smoke"
WANT_ORDER=1
declare -a WANTED=()

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
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
START_ALL=$SECONDS

record() {   # name, status, seconds
    REPORT+=("$1|$2|$3")
    [ "$2" = FAIL ] && FAILED=1
    return 0
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

run_gate() {
    local name="$1" start=$SECONDS status=PASS
    printf '\n──────────────── %s ────────────────\n' "$name"
    "gate_$name"
    case $? in
        0) status=PASS ;;
        2) status=SKIP ;;
        *) status=FAIL; stash_report ;;
    esac
    record "$name" "$status" "$((SECONDS - start))"
    printf '%-10s %s (%ss)\n' "$name" "$status" "$((SECONDS - start))"
}

printf 'handsoff local gates — %s, order seed %s\n' "$PYTHON" "$SEED"
for g in "${WANTED[@]}"; do
    run_gate "$g"
done

printf '\n============ summary ============\n'
for row in "${REPORT[@]}"; do
    IFS='|' read -r name status secs <<<"$row"
    printf '%-10s %-4s %ss\n' "$name" "$status" "$secs"
done
printf 'total %ss\n' "$((SECONDS - START_ALL))"

if [ "$FAILED" = 1 ]; then
    printf '\n============ failures ============\n'
    if [ -f "$FIRST_FAILURE" ]; then
        # Same digest the pipeline's after_script prints, minus the MR comment.
        "$PYTHON" ci/pytest_summary.py "$FIRST_FAILURE" || true
    else
        echo "a gate failed before any junit report was written (compile/shell/smoke)."
    fi
    exit 1
fi
printf '\nall gates passed\n'
