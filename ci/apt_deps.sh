#!/usr/bin/env bash
# Runtime libraries the handsoff suite needs on a Debian slim image.
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
set -uo pipefail

apt-get update -qq
status=0

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
for pkg in bash libgl1 libegl1 libxkbcommon0 libfontconfig1 libportaudio2 libgomp1; do
    install_one "$pkg" || status=1
done
rm -rf /var/lib/apt/lists/*

# Refresh the linker cache: sounddevice resolves PortAudio through
# `ctypes.util.find_library`, which reads ldconfig's cache, not the filesystem.
command -v ldconfig >/dev/null 2>&1 && ldconfig

missing=0
for so in libportaudio.so.2 libasound.so.2 libgomp.so.1 libGL.so.1 libEGL.so.1 \
          libxkbcommon.so.0 libdbus-1.so.3 libglib-2.0.so.0 libfontconfig.so.1; do
    if ! ls /usr/lib/*/"$so" /lib/*/"$so" >/dev/null 2>&1; then
        echo "FATAL: $so is not installed — the suite cannot import the app" >&2
        missing=1
    fi
done

# The same lookup sounddevice performs at import. Checking it explicitly means
# a present-but-uncached library is reported as such, with ldconfig's own view
# printed, instead of surfacing as hundreds of identical import errors.
if ! python3 -c "import ctypes.util, sys; sys.exit(0 if ctypes.util.find_library('portaudio') else 1)"; then
    echo "FATAL: libportaudio.so.2 does not resolve via ctypes.util.find_library" >&2
    echo "  files: $(ls /usr/lib/*/libportaudio* 2>/dev/null | tr '\n' ' ')" >&2
    ldconfig -p 2>/dev/null | grep -i portaudio >&2 || echo "  ldconfig has no portaudio entry" >&2
    missing=1
fi

if [ "$status" != "0" ] || [ "$missing" != "0" ]; then
    echo "FATAL: CI runtime libraries are incomplete (install=$status verify=$missing)" >&2
    exit 1
fi
echo "    runtime libraries present and resolving"
