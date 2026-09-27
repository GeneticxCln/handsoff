#!/usr/bin/env bash
# The CPython a CI job asked for, on a machine that is not the pinned image.
#
# Why this exists: GitLab-hosted (instance) runners are billed to the namespace's
# 400 free compute minutes a month, all three projects under this account share
# that pool, and this repo's five suite jobs spent it by 13 September — after
# which every job on an instance runner failed with `ci_quota_exceeded` before
# running a single test. So the gates run on the desk runner, a project runner on
# the developer's own machine, where a job is not billed at all.
#
# That machine is Arch, and its system python is 3.14 — the newest leg of the
# matrix and newer than the 3.12/3.13 legs its other jobs ask for. Running a
# leg on the desk's own interpreter instead of the one it names would trade one
# kind of red for another — a green about a python nobody asked for, or a red
# about one nobody runs — so the interpreter each job would have got from its
# image is fetched by uv into a per-version venv under ~/.cache, and reused
# warm. 3.14 is a leg now (the one install.sh would use here), and it needs no
# special case: the script is version-agnostic and checks the answer.
#
# Prints the venv's bin directory on STDOUT and nothing else, so a job can write
#     - export PATH="$(bash ci/desk_python.sh):$PATH"
# and then run the same lines the hosted job runs — `python -m pip install …`,
# `pip install -r requirements.txt -c requirements-lock.txt`, `python -m pytest …`.
# Every diagnostic goes to stderr for that reason.
#
# The version is CHECKED, not assumed: a venv that answers with something other
# than PY_VERSION fails here rather than running the suite on an interpreter
# nobody asked for.
set -euo pipefail

version=${PY_VERSION:?PY_VERSION must name the CPython version this job would have been given by its image}
root=${HANDSOFF_CI_VENVS:-$HOME/.cache/handsoff-ci}
venv="$root/py$version"

if ! command -v uv >/dev/null 2>&1; then
    echo "FATAL: uv is not on PATH — the desk runner needs it to provide python $version" >&2
    echo "  install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

# --seed so the venv has pip: the jobs upgrade pip and install the manifest with
# it, exactly as the pinned images do.
if [ ! -x "$venv/bin/python" ]; then
    echo "  creating the python $version venv at $venv" >&2
    uv venv --seed --python "$version" "$venv" >&2
fi

got=$("$venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [ "$got" != "$version" ]; then
    echo "FATAL: $venv answers with python $got, and this job asked for $version" >&2
    echo "  remove it and run again: rm -rf $venv" >&2
    exit 1
fi

printf '%s\n' "$venv/bin"
