#!/usr/bin/env bash
# The runtime libraries the handsoff suite needs — installed where apt exists,
# verified everywhere else.
#
# This is a script rather than one `apt-get install` line in the CI YAML, for a
# reason that cost several pipelines: ONE unknown package name makes apt fail
# the whole request ("E: Unable to locate package …"), the `&&` chain then
# installs nothing at all, and the job runs on to execute the entire suite,
# which dies at `import handsoff` in every test. The result is a 4 MB wall of
# import errors whose actual cause is a single line near the top — and because
# `libdbus-1-3t64` does not exist in this image, that line had been failing
# since the layer was written, so neither the ALSA runtime nor PortAudio was
# ever installed, however the CI comment described it.
#
# So: each package is installed on its own, names that move with Debian's t64
# transition are tried in order, and the SONAMEs the suite actually loads are
# verified afterwards — by file, not through ldconfig's cache — so a missing
# library fails HERE, by name, instead of 900 times at import.
#
# The desk runner (a project runner on the developer's Arch machine, where the
# gates run because instance-runner minutes are a 400-a-month quota this repo
# exceeds) has no apt-get at all, and has no need of one: the libraries below are
# the ones the bubble itself runs against. There the install half is skipped and
# the VERIFY half is the whole layer — the same two tests, so a missing library
# fails here by name either way, and the property this script exists for (the
# suite can import the app before it runs) holds on both runners.
set -uo pipefail

# The interpreter that will run the suite, not necessarily `python3` on PATH: a
# desk job points PATH at the venv for its python version (ci/desk_python.sh)
# before calling this, so the lookup below is checked with the interpreter the
# suite will actually import sounddevice under.
PY=${PYTHON:-python3}

status=0
missing=0

if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq

    # t64 names exist in trixie, the plain names in bookworm; whichever this image
    # has is the one that lands. Any of them failing is a hard failure below.
    install_one() {
        local pkg
        for pkg in "$@"; do
            if apt-get install -yqq --no-install-recommends "$pkg" >/dev/null 2>&1; then
                echo "    installed $pkg"
                return 0
            fi
        done
        echo "FATAL: none of these packages exist in this image: $*" >&2
        return 1
    }

    install_one libglib2.0-0t64 libglib2.0-0 || status=1
    install_one libdbus-1-3t64 libdbus-1-3 || status=1
    # sounddevice links this; libportaudio2 below is the library it looks UP.
    install_one libasound2t64 libasound2 || status=1

    # bash: the Docker executor's shell. libgl*/libegl/libxkbcommon/libfontconfig:
    # offscreen Qt. libgomp1: onnxruntime/ctranslate2 wheels. libportaudio2: what
    # `ctypes.util.find_library('portaudio')` has to resolve at `import sounddevice`.
    # git: install.sh derives the shipped set from `git ls-files` and falls back to
    # globbing without it, so a git-less job tests the fallback while the tests
    # assert the tracked-set rule. procps: the read-only system probes the policy
    # tests actually execute (`uptime`, `free`).
    for pkg in bash libgl1 libegl1 libxkbcommon0 libfontconfig1 libportaudio2 \
               libgomp1 git procps; do
        install_one "$pkg" || status=1
    done
    rm -rf /var/lib/apt/lists/*

    # Refresh the linker cache: sounddevice resolves PortAudio through
    # `ctypes.util.find_library`, which reads ldconfig's cache, not the filesystem.
    command -v ldconfig >/dev/null 2>&1 && ldconfig
else
    echo "    no apt-get here — verifying the runtime instead of installing it"
fi

# Where the libraries live differs by distribution, and the difference is not
# cosmetic: Debian's multiarch puts them one directory deeper
# (/usr/lib/x86_64-linux-gnu/) while Arch keeps them in /usr/lib and /lib
# directly. A check that only knew the multiarch shape would call a working Arch
# machine empty — the same class of mistake as the single `apt-get install` line
# this script replaced, where the check described an image nobody was running.
lib_dirs="/usr/lib/*/ /lib/*/ /usr/lib/ /lib/ /usr/local/lib/"

for so in libportaudio.so.2 libasound.so.2 libgomp.so.1 libGL.so.1 libEGL.so.1 \
          libxkbcommon.so.0 libdbus-1.so.3 libglib-2.0.so.0 libfontconfig.so.1; do
    found=0
    for dir in $lib_dirs; do
        if ls "$dir$so" >/dev/null 2>&1; then
            found=1
            break
        fi
    done
    if [ "$found" != "1" ]; then
        echo "FATAL: $so is not installed — the suite cannot import the app" >&2
        missing=1
    fi
done

# The same lookup sounddevice performs at import. Checking it explicitly means
# a present-but-uncached library is reported as such, with ldconfig's own view
# printed, instead of surfacing as hundreds of identical import errors.
if ! "$PY" -c "import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library('portaudio') else 1)"; then
    echo "FATAL: libportaudio.so.2 does not resolve via ctypes.util.find_library" >&2
    echo "  interpreter: $PY ($("$PY" -c 'import sys; print(sys.executable)' 2>/dev/null || echo '?'))" >&2
    echo "  files: $(ls /usr/lib/*/libportaudio* /usr/lib/libportaudio* /lib/libportaudio* 2>/dev/null | tr '\n' ' ')" >&2
    ldconfig -p 2>/dev/null | grep -i portaudio >&2 || echo "  ldconfig has no portaudio entry" >&2
    missing=1
fi

if [ "$status" != "0" ] || [ "$missing" != "0" ]; then
    echo "FATAL: CI runtime libraries are incomplete (install=$status verify=$missing)" >&2
    exit 1
fi
echo "    runtime libraries present and resolving"
