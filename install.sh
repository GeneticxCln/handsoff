#!/usr/bin/env bash
# handsoff installer — Arch Linux / CachyOS + niri (Wayland)
#
# Installs system + pip packages, downloads the whisper model and a piper
# voice, and places handsoff.py + the restart script in ~/.local/bin.
# Run it from the directory containing handsoff.py:
#
#   ./install.sh
#
# Overrides (optional): HANDSOFF_MODEL, HANDSOFF_WHISPER, HANDSOFF_WHISPER_REVISION,
#   PIPER_VOICE_URL, HANDSOFF_PYTHON, HANDSOFF_NO_OLLAMA_SERVICE
set -euo pipefail

# No-arg flags that must never touch the system: the CI smoke test runs these
# so installer drift (syntax rot, broken early flow) is caught on every push.
case "${1:-}" in
    -h|--help)
        sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        echo ""
        echo "Options:"
        echo "  --help            show this help"
        echo "  --rehearsal       install into HANDSOFF_REHEARSAL_ROOT without host changes"
        echo "  --rollback        restore the previous release saved by the last install"
        echo "  --uninstall       remove binaries, unit and snippet"
        echo "  --uninstall --purge  also wipe config/state (backs up first)"
        exit 0
        ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REHEARSAL=0
if [ "${1:-}" = "--rehearsal" ]; then
    REHEARSAL=1
    : "${HANDSOFF_REHEARSAL_ROOT:?HANDSOFF_REHEARSAL_ROOT is required for --rehearsal}"
    HOME="$HANDSOFF_REHEARSAL_ROOT"
    XDG_STATE_HOME="$HOME/.local/state"
    export HOME XDG_STATE_HOME
    echo "==> rehearsal mode: target HOME=$HOME"
fi
BIN_DIR="$HOME/.local/bin"
CONF_DIR="$HOME/.config/handsoff"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/handsoff"

case "${1:-}" in
    --rollback)
        # Manual rollback path: the switch itself auto-restores on failure;
        # this is for a release that INSTALLED fine but turns out to be bad.
        PREV="$CONF_DIR/releases/prev"
        if [ ! -f "$PREV/handsoff.py" ]; then
            echo "FATAL: no previous release saved at $PREV — nothing to roll back to" >&2
            exit 1
        fi
        echo "==> Rolling back to the previous release ($PREV)"
        mkdir -p "$BIN_DIR/core"
        for f in handsoff.py handsoff-restart handsoff-settings.py \
                 settings_schema.py hardware.py \
                 core/__init__.py core/settings.py core/audio.py \
                 core/brain.py core/tools.py core/doctor.py core/lifecycle.py; do
            [ -f "$PREV/$f" ] || continue
            case "$f" in
                handsoff.py|handsoff-restart|handsoff-settings.py) m=755 ;;
                *) m=644 ;;
            esac
            install -m "$m" "$PREV/$f" "$BIN_DIR/$f"
        done
        echo "    previous release restored — restart the bubble to load it:"
        echo "      systemctl --user restart handsoff   (or: ~/.local/bin/handsoff-restart)"
        exit 0
        ;;
esac

WHISPER_SIZE="${HANDSOFF_WHISPER:-tiny}"
OLLAMA_MODEL="${HANDSOFF_MODEL:-qwen3:8b}"
VOICE_URL_DEFAULT="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
PIPER_VOICE_URL="${PIPER_VOICE_URL:-$VOICE_URL_DEFAULT}"
# ponytail: resolve once; venv's python3 shadows system when activated.
PYBIN="${HANDSOFF_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
WHISPER_REVISION="${HANDSOFF_WHISPER_REVISION:-main}"

if [ "${1:-}" = "--uninstall" ]; then
    echo "==> Uninstalling handsoff"
    if [ "${2:-}" = "--purge" ]; then
        # Never remove user data until a private, independently readable
        # archive has been created and validated.
        umask 077
        backup=""
        if ! backup="$(mktemp "$HOME/handsoff-backup-$(date +%Y%m%d-%H%M%S)-XXXXXX.tar.gz")"; then
            echo "FATAL: could not create the handsoff backup archive; nothing removed" >&2
            exit 1
        fi
        if ! tar czf "$backup" -C "$HOME" \
                .config/handsoff .local/state/handsoff; then
            rm -f -- "$backup"
            echo "FATAL: handsoff backup archive failed; nothing removed" >&2
            exit 1
        fi
        if ! chmod 600 "$backup" \
                || [ ! -f "$backup" ] || [ ! -r "$backup" ] || [ ! -s "$backup" ] \
                || ! backup_listing="$(tar tzf "$backup" 2>/dev/null)" \
                || [ -z "$backup_listing" ]; then
            rm -f -- "$backup"
            echo "FATAL: handsoff backup archive failed verification; nothing removed" >&2
            exit 1
        fi
    fi
    systemctl --user disable --now handsoff.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/handsoff.service" \
          "$BIN_DIR/handsoff.py" "$BIN_DIR/handsoff-restart" \
          "$BIN_DIR/handsoff-settings.py" \
          "$BIN_DIR/settings_schema.py" "$BIN_DIR/hardware.py"
    rm -rf "$BIN_DIR/core"
    systemctl --user daemon-reload 2>/dev/null || true
    # kill only real bubble processes: python executable + EXACT cmdline match.
    # A bare `pkill -f handsoff.py` would kill bystanders whose cmdline merely
    # mentions the path (an editor with the file open, a running pytest run).
    for pid in $(pgrep -f 'handsoff\.py' 2>/dev/null || true); do
        exe="$(readlink "/proc/$pid/exe" 2>/dev/null)" || continue
        case "$(basename "$exe")" in
            python|python3|python[0-9].*) ;;
            *) continue ;;   # never touch editors, tails, shells
        esac
        tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null \
            | grep -qxF "$BIN_DIR/handsoff.py" || continue
        kill -TERM "$pid" 2>/dev/null || true
    done
    if [ "${2:-}" = "--purge" ]; then
        rm -rf "$CONF_DIR" "$STATE_DIR"
        echo "    config + models removed (backup: $backup)"
    else
        echo "    kept $CONF_DIR and $STATE_DIR (history, models)."
        echo "    full wipe:  $0 --uninstall --purge"
    fi
    echo "handsoff uninstalled."
    exit 0
fi

echo "==> [1/8] System packages (pacman)"
# No forced refresh or full-system upgrade by default: `pacman -Sy` without
# `-u` is a partial upgrade (fresh DB + stale installed packages) that can
# break the system, and `-Syu` rewrites the whole system on every install
# run. The default installs missing packages from the local DB without
# refreshing it (`-S --needed`, no `-y`); refresh explicitly with
# `sudo pacman -Sy` yourself beforehand if the DB is stale, or opt into the
# supported full-upgrade policy explicitly:
#   HANDSOFF_FULL_UPGRADE=1 ./install.sh
# Feature deps provisioned here so advertised tools work out of the box:
# ydotool (typing/keys), wl-clipboard (clipboard), grim (screenshots),
# tesseract (OCR), mpc (music control).
# Python targets are probed per-package first: CachyOS (and other Arch
# derivatives) don't ship python-pyside6/python-sounddevice in their repos,
# and pacman aborts the WHOLE transaction on any unknown target. Whatever
# pacman can't provide comes from requirements.txt, so a skip is safe.
PYTHON_PKGS="python-pyside6 python-sounddevice python-numpy python-pip"
ARCH_PKGS=""
PACMAN="sudo pacman"
# HANDSOFF_SKIP_SYSTEM_PKGS=1: trust the system packages are already present
# (CI, redeploys from a non-interactive shell where sudo cannot prompt, or a
# pre-provisioned box). Package probes still run; only the transaction is
# skipped.
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping system package probes and transaction (rehearsal)"
    PACMAN=""
else
    for p in $PYTHON_PKGS; do
        if pacman -Si "$p" >/dev/null 2>&1; then
            ARCH_PKGS="$ARCH_PKGS $p"
        else
            echo "    note: $p is not in this distro's repos — requirements.txt provides it via pip"
        fi
    done
fi
if [ "${HANDSOFF_SKIP_SYSTEM_PKGS:-0}" = "1" ]; then
    echo "    skipping system package transaction (HANDSOFF_SKIP_SYSTEM_PKGS=1)"
    PACMAN=""
fi
if [ "${HANDSOFF_FULL_UPGRADE:-0}" = "1" ]; then
    echo "    full system upgrade requested (supported Arch -Syu policy)"
    [ -n "$PACMAN" ] && $PACMAN -Syu --needed --noconfirm $ARCH_PKGS \
        alsa-utils ollama curl \
        ydotool wl-clipboard grim tesseract mpc
else
    echo "    installing missing packages from the local DB (no refresh, no full upgrade)"
    [ -n "$PACMAN" ] && $PACMAN -S --needed --noconfirm $ARCH_PKGS \
        alsa-utils ollama curl \
        ydotool wl-clipboard grim tesseract mpc
fi

echo "==> [2/8] Python packages (pip, user site)"
# Single source of truth: the manifest. Core deps also come from pacman above;
# this covers the lazy-imported extras (faster-whisper, piper-tts,
# openwakeword/onnxruntime for the wake spotter) at verified floors.
PIP_REQUIREMENTS=("-r" "$HERE/requirements.txt")
if [ -f "$HERE/requirements-lock.txt" ]; then
    if ! "${PYBIN}" - "$HERE/requirements.txt" "$HERE/requirements-lock.txt" <<'PY_EOF'
import re
import sys
from pathlib import Path

def names(path):
    result = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", line)
        if not match or line.startswith(("-", "--")):
            continue
        result[match.group(1).lower().replace("_", "-")] = match.group(2).strip()
    return result

requirements = names(sys.argv[1])
lock = names(sys.argv[2])
missing = [name for name in requirements if name not in lock]
unpinned = [name for name in requirements if not lock.get(name, "").startswith("==")]
if missing or unpinned:
    print("lock is inconsistent: "
          + ("missing " + ", ".join(missing) if missing else "")
          + ("; not exact-pinned " + ", ".join(unpinned) if unpinned else ""),
          file=sys.stderr)
    raise SystemExit(1)
PY_EOF
    then
        echo "FATAL: requirements-lock.txt is inconsistent with requirements.txt" >&2
        exit 1
    fi
    PIP_REQUIREMENTS+=("-c" "$HERE/requirements-lock.txt")
fi
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping pip transaction (rehearsal)"
else
    "${PYBIN}" -m pip install --user --break-system-packages --upgrade \
        "${PIP_REQUIREMENTS[@]}"
fi

echo "==> [3/8] Directories"
mkdir -p "$BIN_DIR" "$CONF_DIR/whisper-model" "$CONF_DIR/piper-voice" "$STATE_DIR"

echo "==> [4/8] Staging the release (compile-gated, rollback-able)"
# Nothing is installed until a complete staged copy has passed the compile
# and import gates; the currently-deployed set is kept at
# $CONF_DIR/releases/prev so `install.sh --rollback` can restore it.
RELEASES_DIR="$CONF_DIR/releases"
STAGE_DIR="$RELEASES_DIR/staged.$$"
PREV_DIR="$RELEASES_DIR/prev"
mkdir -p "$STAGE_DIR/core"
stage_fail() {
    echo "    FATAL: $1" >&2
    rm -rf "$STAGE_DIR"
    exit 1
}
# --- collect the shipped set into the stage (live files untouched yet)
install -m 755 "$HERE/handsoff.py" "$STAGE_DIR/handsoff.py" \
    || stage_fail "could not stage handsoff.py"
if [ -f "$HERE/handsoff-settings.py" ]; then
    install -m 755 "$HERE/handsoff-settings.py" "$STAGE_DIR/handsoff-settings.py" \
        || stage_fail "could not stage handsoff-settings.py"
fi
# Supporting modules imported next to the bubble (schema = single source of
# DEFAULT_SETTINGS; hardware = the lazy-imported hardware/world watch; core/ =
# the extracted runtime). All must exist or the installed copy dies on import.
for mod in settings_schema hardware; do
    if [ -f "$HERE/$mod.py" ]; then
        install -m 644 "$HERE/$mod.py" "$STAGE_DIR/$mod.py" \
            || stage_fail "could not stage $mod.py"
    else
        stage_fail "$HERE/$mod.py is missing but required by handsoff.py"
    fi
done
if [ -f "$HERE/core/__init__.py" ] && [ -f "$HERE/core/settings.py" ] \
        && [ -f "$HERE/core/audio.py" ] && [ -f "$HERE/core/brain.py" ] \
        && [ -f "$HERE/core/tools.py" ] && [ -f "$HERE/core/doctor.py" ] \
        && [ -f "$HERE/core/lifecycle.py" ]; then
    for m in __init__ settings audio brain tools doctor lifecycle; do
        install -m 644 "$HERE/core/$m.py" "$STAGE_DIR/core/$m.py" \
            || stage_fail "could not stage core/$m.py"
    done
else
    stage_fail "$HERE/core/ is missing but required by handsoff.py"
fi
install -m 755 "$HERE/handsoff-restart" "$STAGE_DIR/handsoff-restart" \
    || stage_fail "could not stage handsoff-restart"
# --- gate 1: every staged Python file must byte-compile before it can ship
STAGED_PY=("$STAGE_DIR/handsoff.py" "$STAGE_DIR/settings_schema.py" "$STAGE_DIR/hardware.py")
[ -f "$STAGE_DIR/handsoff-settings.py" ] && STAGED_PY+=("$STAGE_DIR/handsoff-settings.py")
STAGED_PY+=("$STAGE_DIR"/core/*.py)
"${PYBIN}" -m py_compile "${STAGED_PY[@]}" \
    || stage_fail "staged sources failed to byte-compile — refusing to deploy"
# --- gate 2: the dependency-free trust modules must import from the stage
( cd "$STAGE_DIR" && "${PYBIN}" -c 'import settings_schema, core.settings' ) \
    || stage_fail "staged settings schema failed the import smoke — refusing to deploy"
# --- save the currently-deployed set as the rollback target
HAD_PREV=0
if [ -f "$BIN_DIR/handsoff.py" ]; then
    rm -rf "$PREV_DIR.staging" "$PREV_DIR"
    mkdir -p "$PREV_DIR.staging/core"
    for f in handsoff.py handsoff-settings.py settings_schema.py hardware.py handsoff-restart; do
        [ -f "$BIN_DIR/$f" ] && cp -p "$BIN_DIR/$f" "$PREV_DIR.staging/$f"
    done
    for f in "$BIN_DIR"/core/*.py; do
        [ -f "$f" ] && cp -p "$f" "$PREV_DIR.staging/core/"
    done
    mv "$PREV_DIR.staging" "$PREV_DIR"
    HAD_PREV=1
fi
# --- the switch: install staged files; any failure restores the previous set
SWITCH_FILES_755="handsoff.py handsoff-restart"
[ -f "$STAGE_DIR/handsoff-settings.py" ] \
    && SWITCH_FILES_755="$SWITCH_FILES_755 handsoff-settings.py"
SWITCH_FILES_644="settings_schema.py hardware.py core/__init__.py core/settings.py core/audio.py core/brain.py core/tools.py core/doctor.py core/lifecycle.py"
switch_fail() {
    echo "    FATAL: $1 — restoring the previous release" >&2
    if [ "$HAD_PREV" = "1" ]; then
        mkdir -p "$BIN_DIR/core"
        for f in $SWITCH_FILES_755 $SWITCH_FILES_644; do
            [ -f "$PREV_DIR/$f" ] || continue
            case "$f" in
                handsoff.py|handsoff-restart|handsoff-settings.py) m=755 ;;
                *) m=644 ;;
            esac
            install -m "$m" "$PREV_DIR/$f" "$BIN_DIR/$f" 2>/dev/null || true
        done
        echo "    previous release restored; deployment unchanged (retry or report)" >&2
    else
        echo "    no previous release existed — ~/.local/bin may be partially populated" >&2
    fi
    rm -rf "$STAGE_DIR"
    exit 1
}
mkdir -p "$BIN_DIR/core"
for f in $SWITCH_FILES_755; do
    install -m 755 "$STAGE_DIR/$f" "$BIN_DIR/$f" || switch_fail "switch failed at $f"
done
for f in $SWITCH_FILES_644; do
    install -m 644 "$STAGE_DIR/$f" "$BIN_DIR/$f" || switch_fail "switch failed at $f"
done
rm -rf "$STAGE_DIR"
if [ "$HAD_PREV" = "1" ]; then
    echo "    staged release verified (compile + schema import) and switched"
    echo "    previous release kept at $PREV_DIR — 'install.sh --rollback' restores it"
else
    echo "    staged release verified (compile + schema import) and switched (first install)"
fi
echo "==> [5/8] Downloading whisper '$WHISPER_SIZE' model (one time)"
if [ "$REHEARSAL" = "1" ]; then
    printf 'rehearsal placeholder\n' > "$CONF_DIR/whisper-model/rehearsal.txt"
    WHISPER_RESOLVED_REVISION="$WHISPER_REVISION"
else
    WHISPER_RESOLVED_REVISION="$(${PYBIN} - "$WHISPER_SIZE" "$CONF_DIR/whisper-model" "$WHISPER_REVISION" <<'PY_EOF'
import sys
from faster_whisper import utils
from faster_whisper import WhisperModel

size, output_dir, revision = sys.argv[1:]
WhisperModel(size, device="cpu", compute_type="int8", download_root=output_dir,
             revision=revision)
resolved = ""
try:
    from huggingface_hub import HfApi
    repo_id = getattr(utils, "_MODELS", {}).get(size, size)
    resolved = HfApi().model_info(repo_id, revision=revision).sha or ""
except Exception:
    pass
print(resolved)
PY_EOF
)"
    if [ -z "$WHISPER_RESOLVED_REVISION" ]; then
        WHISPER_RESOLVED_REVISION="$WHISPER_REVISION"
    fi
fi
echo "whisper model ready"
# faster-whisper fetches via huggingface_hub (content-hashed blobs, verified
# on download) — no separate sha256 manifest to check like the piper voice.
# Fail loudly on an empty cache instead of booting deaf on a partial fetch.
if [ -z "$(ls -A "$CONF_DIR/whisper-model" 2>/dev/null)" ]; then
    echo "    FATAL: whisper model download produced no files in $CONF_DIR/whisper-model" >&2
    exit 1
fi

# Deployment manifest: which checkout state produced the installed copy, per
# file. Model metadata is best-effort: the model is usable even when the
# downloader cannot expose a resolved revision or a file cannot be read.
mkdir -p "$CONF_DIR"
sha_of() { sha256sum "$1" 2>/dev/null | awk '{print $1}' || echo null; }
atomic_write() {
    local dest="$1" mode="$2" tmp
    tmp="$(mktemp "${dest}.tmp.XXXXXX")" || {
        echo "FATAL: could not stage $dest" >&2
        return 1
    }
    if ! cat > "$tmp" || ! chmod "$mode" "$tmp" || ! mv -f "$tmp" "$dest"; then
        rm -f -- "$tmp"
        echo "FATAL: could not atomically install $dest" >&2
        return 1
    fi
}
whisper_sha256=""
if whisper_sha256="$(${PYBIN} - "$CONF_DIR/whisper-model" <<'PY_EOF'
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
try:
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    for path in files:
        rel = path.relative_to(root).as_posix().encode()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    print(digest.hexdigest())
except (OSError, ValueError):
    pass
PY_EOF
)"; then :; else whisper_sha256=""; fi
manifest_whisper_sha256=""
[ -n "$whisper_sha256" ] && manifest_whisper_sha256="  \"whisper_sha256\": \"$whisper_sha256\","
atomic_write "$CONF_DIR/deployment.json" 600 <<MANIFEST_EOF
{
  "installed_at": "$(date -Is)",
  "source_dir": "$HERE",
  "whisper_model": "$WHISPER_SIZE",
  "whisper_revision": "$WHISPER_RESOLVED_REVISION",
$manifest_whisper_sha256
  "python": "$PYBIN",
  "files": {
    "handsoff.py": {"source_sha256": "$(sha_of "$HERE/handsoff.py")", "installed_sha256": "$(sha_of "$BIN_DIR/handsoff.py")"},
    "handsoff-settings.py": {"source_sha256": "$(sha_of "$HERE/handsoff-settings.py")", "installed_sha256": "$(sha_of "$BIN_DIR/handsoff-settings.py")"},
    "settings_schema.py": {"source_sha256": "$(sha_of "$HERE/settings_schema.py")", "installed_sha256": "$(sha_of "$BIN_DIR/settings_schema.py")"},
    "hardware.py": {"source_sha256": "$(sha_of "$HERE/hardware.py")", "installed_sha256": "$(sha_of "$BIN_DIR/hardware.py")"},
    "core/__init__.py": {"source_sha256": "$(sha_of "$HERE/core/__init__.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/__init__.py")"},
    "core/settings.py": {"source_sha256": "$(sha_of "$HERE/core/settings.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/settings.py")"},
    "core/audio.py": {"source_sha256": "$(sha_of "$HERE/core/audio.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/audio.py")"},
    "core/brain.py": {"source_sha256": "$(sha_of "$HERE/core/brain.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/brain.py")"},
    "core/tools.py": {"source_sha256": "$(sha_of "$HERE/core/tools.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/tools.py")"},
    "core/doctor.py": {"source_sha256": "$(sha_of "$HERE/core/doctor.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/doctor.py")"},
    "core/lifecycle.py": {"source_sha256": "$(sha_of "$HERE/core/lifecycle.py")", "installed_sha256": "$(sha_of "$BIN_DIR/core/lifecycle.py")"},
    "handsoff-restart": {"source_sha256": "$(sha_of "$HERE/handsoff-restart")", "installed_sha256": "$(sha_of "$BIN_DIR/handsoff-restart")"}
  }
}
MANIFEST_EOF

echo "==> [6/8] Downloading piper voice (sha256-verified)"
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping voice download (rehearsal)"
else
voice="$(basename "$PIPER_VOICE_URL")"
VOICE_SHA256="${PIPER_VOICE_SHA256:-5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f}"
VOICE_JSON_SHA256="${PIPER_VOICE_JSON_SHA256:-efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0}"
download_verified() {  # <url> <dest> <expected-sha256-or-empty>
    local url="$1" dest="$2" want="$3" got
    # existing files are verified too: an interrupted earlier download must
    # not be silently accepted (a truncated voice crashes piper at runtime)
    if [ -f "$dest" ] && [ -n "$want" ]; then
        got="$(sha256sum "$dest" | awk '{print $1}')"
        if [ "$got" == "$want" ]; then
            return 0
        fi
        echo "    existing $(basename "$dest") failed checksum — re-downloading" >&2
        rm -f "$dest"
    fi
    local tmp="$dest.part.$$"
    curl -fL --retry 3 --connect-timeout 15 --max-time 300 -o "$tmp" "$url"
    if [ -z "$want" ]; then
        # URL overridden without a SHA256: user's explicit choice — install
        # unverified but say so loudly (default voice stays strictly checked)
        echo "    WARNING: $(basename "$dest") downloaded WITHOUT checksum verification" >&2
        mv "$tmp" "$dest"
        return 0
    fi
    got="$(sha256sum "$tmp" | awk '{print $1}')"
    if [ "$got" != "$want" ]; then
        echo "    FATAL: checksum mismatch for $(basename "$dest")" >&2
        echo "      expected $want" >&2
        echo "      got      $got" >&2
        rm -f "$tmp"
        exit 1
    fi
    mv "$tmp" "$dest"
}
# a custom URL MUST come with a hash: the voice is spoken audio the user
# cannot visually audit, so "warning-only" trust was a supply-chain hole.
# To use a new voice, pin it first:
#   curl -fsSL "$PIPER_VOICE_URL" | sha256sum
#   ...then export PIPER_VOICE_SHA256=<digest> (and _JSON_SHA256 for the .json).
# HANDSOFF_UNVERIFIED_VOICE=1 restores the old warning-only behavior, loudly,
# for airgapped/experimental setups — an explicit, deliberate choice.
if [ "$PIPER_VOICE_URL" != "$VOICE_URL_DEFAULT" ] \
        && [ -z "${PIPER_VOICE_SHA256:-}" ] \
        && [ "${HANDSOFF_UNVERIFIED_VOICE:-0}" != "1" ]; then
    echo "    FATAL: custom PIPER_VOICE_URL requires PIPER_VOICE_SHA256" >&2
    echo "      pin it:  curl -fsSL '$PIPER_VOICE_URL' | sha256sum" >&2
    echo "      (or HANDSOFF_UNVERIFIED_VOICE=1 to accept an unverified voice)" >&2
    exit 1
fi
if [ "$PIPER_VOICE_URL" != "$VOICE_URL_DEFAULT" ] \
        && [ -z "${PIPER_VOICE_SHA256:-}" ]; then
    echo "    WARNING (HANDSOFF_UNVERIFIED_VOICE=1): $voice downloads WITHOUT checksum verification" >&2
    VOICE_SHA256=""
fi
download_verified "$PIPER_VOICE_URL" "$CONF_DIR/piper-voice/$voice" "$VOICE_SHA256"
download_verified "$PIPER_VOICE_URL.json" "$CONF_DIR/piper-voice/$voice.json" "$VOICE_JSON_SHA256"
fi

echo "==> [7/8] systemd user service (auto-restart if the bubble dies)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
atomic_write "$SYSTEMD_DIR/handsoff.service" 644 <<UNIT_EOF
[Unit]
Description=handsoff voice assistant bubble
After=graphical-session.target
PartOf=graphical-session.target
# crash-loop guard: stop after 5 failures in 120s (MUST live in [Unit], not [Service])
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$PYBIN %h/.local/bin/handsoff.py
# Restart=always: recover from clean exits too (stray SIGTERM, Quit menu click,
# app.quit()) — the only quiet exit we honour is a real desktop shutdown (PartOf).
Restart=always
RestartSec=3
TimeoutStartSec=30

[Install]
WantedBy=graphical-session.target
UNIT_EOF
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping systemd activation (rehearsal)"
elif systemctl --user daemon-reload 2>/dev/null; then
    # ydotoold must be running for ydotool (typing/keys) to work at all.
    # Arch's USER unit is ydotool.service (it starts ydotoold); other distros
    # ship ydotoold.service — enable whichever exists, warn only if neither.
    YDOTOOL_UNIT=""
    for u in ydotool.service ydotoold.service; do
        if systemctl --user enable --now "$u" 2>/dev/null; then
            YDOTOOL_UNIT="$u"
            break
        fi
    done
    if [ -n "$YDOTOOL_UNIT" ]; then
        echo "    ydotoold running via $YDOTOOL_UNIT — typing tools online"
    else
        echo "    WARN: no ydotoold unit found (tried ydotool.service, ydotoold.service) —"
        echo "    typing tools will error until the daemon runs"
    fi
    systemctl --user enable handsoff.service 2>/dev/null || true
    # a bubble that is ALREADY running keeps executing the old code until it
    # is restarted — the manifest would report in-sync while the live process
    # serves stale logic (exactly the drift the doctor exists to expose).
    if systemctl --user is-active --quiet handsoff.service; then
        echo "    bubble is running — restarting to load the new code"
        systemctl --user restart handsoff.service || true
    fi
    if systemctl --user is-active --quiet handsoff.service; then
        echo "    installed + running with the new code: journalctl --user -u handsoff -f"
    else
        echo "    installed + enabled: systemctl --user start handsoff   (auto-restarts on crash)"
    fi
    echo "    note: systemd now owns autostart — do NOT also add handsoff to niri's spawn-at-startup"
else
    echo "    WARN: systemd user session not reachable; keeping niri spawn-at-startup as the autostart"
fi

echo "==> [8/8] Checking ollama"
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping ollama checks (rehearsal)"
elif ! curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; then
    # ponytail: never touch the system service when pkgs are skipped or opted out.
    if [ "${HANDSOFF_SKIP_SYSTEM_PKGS:-0}" = "1" ] || [ "${HANDSOFF_NO_OLLAMA_SERVICE:-0}" = "1" ]; then
        echo "    ollama not running — leaving the service alone (skip/opt-out)"
    else
        echo "    starting the ollama service ..."
        sudo systemctl enable --now ollama || true
        sleep 2
    fi
fi
if [ "$REHEARSAL" != "1" ] && ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
    echo "    pulling $OLLAMA_MODEL (a few GB, one time) ..."
    ollama pull "$OLLAMA_MODEL" || echo "    WARN: pull failed — run 'ollama pull $OLLAMA_MODEL' later"
fi
if [ "$REHEARSAL" != "1" ] && ! ollama show "$OLLAMA_MODEL" 2>/dev/null | grep -qi 'tools'; then
    echo "    WARN: $OLLAMA_MODEL may not support tool calling — desktop control and"
    echo "    self-modification need a tools-capable model (e.g. qwen3:8b, llama3.1:8b)."
    echo "    handsoff will still chat, but set HANDSOFF_MODEL to enable tools."
fi

atomic_write "$CONF_DIR/niri-window-rule.kdl" 644 <<'NIRI_EOF'
// handsoff voice-assistant bubble — merge into ~/.config/niri/config.kdl
window-rule {
    match app-id=r#"^handsoff$"#
    open-floating true
    default-floating-position x=16 y=16 relative-to="bottom-right"
    focus-ring { off; }
    border { off; }
    shadow { off; }
}
NIRI_EOF
# ONE autostart owner: systemd (if the user unit is actually enabled) OR
# niri spawn-at-startup — never both. SYSTEMD_EDITOR says nothing about
# whether systemd manages the app; check the real unit state instead.
if [ "$REHEARSAL" = "1" ] || systemctl --user is-enabled handsoff.service >/dev/null 2>&1; then
    # systemd owns the bubble — print the manual keybind for settings,
    # do NOT write spawn-at-startup (a unit restart would spawn duplicates)
    echo "     (systemd manages autostart; add 'Mod+Shift+S => spawn settings' to niri manually)"
else
    printf '\n// launch at startup:\nspawn-at-startup "%s" "%s"\n' \
        "$PYBIN" "$BIN_DIR/handsoff.py" >> "$CONF_DIR/niri-window-rule.kdl"
    echo "     (no systemd: added spawn-at-startup — check the path contains no wrong username)"
fi

if [ "$REHEARSAL" = "1" ]; then
    "${PYBIN}" - "$CONF_DIR/deployment.json" <<'PY_EOF'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
assert manifest["files"]
PY_EOF
    for required in \
        "$BIN_DIR/handsoff.py" "$BIN_DIR/settings_schema.py" "$BIN_DIR/hardware.py" \
        "$BIN_DIR/core/__init__.py" "$BIN_DIR/core/settings.py" "$BIN_DIR/core/doctor.py" \
        "$BIN_DIR/core/lifecycle.py" \
        "$BIN_DIR/handsoff-restart" \
        "$SYSTEMD_DIR/handsoff.service" "$CONF_DIR/niri-window-rule.kdl"; do
        [ -f "$required" ] || { echo "FATAL: rehearsal missing $required" >&2; exit 1; }
    done
    echo "rehearsal complete: copied files, manifest, unit, and niri snippet verified"
    exit 0
fi

echo
echo "handsoff installed."
echo "  1. Merge $CONF_DIR/niri-window-rule.kdl into ~/.config/niri/config.kdl"
echo "     then: niri msg action reload-config"
if systemctl --user is-active --quiet handsoff.service 2>/dev/null; then
    echo "  2. running with the new code — verify with:  python ~/.local/bin/handsoff.py --ptt doctor"
else
    echo "  2. Start it now with:  systemctl --user start handsoff  (or: python ~/.local/bin/handsoff.py)"
fi
echo "  3. Verify the deployment:  python ~/.local/bin/handsoff.py --ptt doctor"
echo "  4. Uninstall anytime with:  $0 --uninstall"
