#!/usr/bin/env bash
# handsoff installer — Arch Linux / CachyOS + niri (Wayland)
#
# Installs system + pip packages, downloads the whisper model and the speech
# weights, and places handsoff.py + the restart script in ~/.local/bin.
# Run it from the directory containing handsoff.py:
#
#   ./install.sh
#
# Overrides (optional): HANDSOFF_MODEL, HANDSOFF_WHISPER, HANDSOFF_WHISPER_REVISION,
#   HANDSOFF_TTS_REPO, HANDSOFF_PYTHON, HANDSOFF_NO_OLLAMA_SERVICE
set -euo pipefail

# No-arg flags that must never touch the system: the CI smoke test runs these
# so installer drift (syntax rot, broken early flow) is caught on every push.
case "${1:-}" in
    -h|--help)
        # 2..11 is the whole comment block: the previous 2..10 range cut the
        # second "Overrides" line, so --help silently hid half the documented
        # environment knobs (including HANDSOFF_TTS_REPO).
        sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# --- the shipped set, defined once ------------------------------------------
# Staging, the compile gate, the rollback save, the switch, rollback and the
# deployment manifest all derive from these facts, because the version of this
# script that hand-listed its files shipped core/theme.py nowhere while
# `--ptt doctor` still reported in-sync: a new module was compiled from the
# checkout, never installed and never hashed, so nothing could notice. The
# same list was duplicated in seven places and could drift in seven ways.
#   * the top-level *.py files the PROJECT owns are part of the app,
#   * handsoff-restart is the one non-Python artifact, and
#   * TOP_EXECUTABLE names the entry points installed 0755.
# TOP_REQUIRED is the floor: what handsoff.py hard-imports. Losing one of those
# must fail the stage loudly rather than deploy an app that dies on import.
TOP_REQUIRED="handsoff.py settings_schema.py hardware.py"
TOP_EXECUTABLE="handsoff.py handsoff-settings.py handsoff-restart"
# core/ ships as a SET. CORE_REQUIRED is the floor — the modules handsoff.py
# hard-imports — and it fails the stage loudly if one of them disappears; the
# glob over core/*.py is the ceiling. It used to be a hand-maintained copy list,
# which meant a newly added module (core/theme.py) was compiled from the checkout
# but never installed, and because the deployment manifest enumerated files the
# same way, doctor reported in-sync while the feature was simply absent (the
# settings GUI quietly fell back to "no wallpaper matching").
CORE_REQUIRED="__init__ registry settings audio brain tools doctor lifecycle calendar assistant bubble web theme"
is_exec() {   # 0 when the basename is an entry point (installed 0755)
    case " $TOP_EXECUTABLE " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- which top-level files belong to the project ----------------------------
# A bare glob shipped whatever happened to sit beside handsoff.py. It really
# happened: a scratch `test.py` from a TTS experiment was copied into
# ~/.local/bin (the user's PATH) and recorded in the deployment manifest, so
# the next edit to that scratch file made `--ptt doctor` declare the whole
# installation "installed-drift" — a false alarm about the bubble, raised by a
# file the bubble does not use. A stray name can also COLLIDE with a real
# binary in $BIN_DIR and overwrite it.
#
# So: the declared entry points always ship, plus every top-level *.py the repo
# actually tracks. Discovery stays automatic (a new module ships once it is
# committed — no list to maintain), while a scratch file never leaves the
# checkout.
#
# Outside a git work tree (a tarball install) there is NO "what does the
# project own" signal to consult, and the previous fallback — ship whatever
# the glob finds — is precisely how that scratch file got out: a tarball built
# from the working directory (not from `git archive`) carries the untracked
# scratch along and the installer would ship and hash it. So the no-git
# fallback is the DECLARED set, not the glob. A module that belongs to the
# project has to be named in TOP_REQUIRED/CORE_REQUIRED — which is also the
# floor the stage below already validates, so there is still exactly one
# answer to "what ships" and it is written down.
#
# Whether a repository was found is REMEMBERED (HAVE_GIT) rather than inferred
# from an empty tracked list. Those are not the same answer: a repo whose index
# holds nothing also ships only the declared set, but the reason is that nothing
# there is staged YET — so the advice belongs to "git add it", not to "edit
# install.sh". Keying the message on an empty list sent that user to the wrong
# file (and made the two cases indistinguishable to anyone reading the output).
#
# ADDING A MODULE: commit it (git installs pick it up automatically) and add
# it to CORE_REQUIRED if it lives in core/, or a tarball install will not ship
# it. The stage fails loudly on a missing CORE_REQUIRED entry, so the mistake
# is caught at install time rather than at first import.
TRACKED_PY=""
HAVE_GIT=0
if command -v git >/dev/null 2>&1 \
    && git -C "$HERE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    HAVE_GIT=1
    # repo-relative paths, exactly as the globs below name them
    TRACKED_PY="$(git -C "$HERE" ls-files -- '*.py' 2>/dev/null || true)"
fi
DECLARED_PY="$TOP_REQUIRED handsoff-settings.py"
for m in $CORE_REQUIRED; do
    DECLARED_PY="$DECLARED_PY core/$m.py"
done
ship_file() {   # $1 = repo-relative path; 0 → it is part of the project
    case " $DECLARED_PY " in
        *" $1 "*) return 0 ;;           # declared entry points always ship
    esac
    [ "$HAVE_GIT" = "1" ] || return 1   # no repo: nothing beyond the declared set
    printf '%s\n' "$TRACKED_PY" | grep -qx -- "$1"
}
ship_top() { ship_file "$1"; }
ship_core() { ship_file "core/$1"; }

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
        # prev/ only ever holds files this installer put there, so restoring
        # the whole tree is safe — and globbing it means a module added since
        # prev was saved still comes back.
        for f in "$PREV"/*.py "$PREV"/handsoff-restart; do
            [ -f "$f" ] || continue
            base="$(basename "$f")"
            if is_exec "$base"; then m=755; else m=644; fi
            install -m "$m" "$f" "$BIN_DIR/$base"
        done
        # core/ restores by glob for the same reason it ships by glob: a
        # hand-maintained list here means a rollback silently drops whatever
        # module was added since the list was written.
        for f in "$PREV"/core/*.py; do
            [ -f "$f" ] || continue
            install -m 644 "$f" "$BIN_DIR/core/$(basename "$f")"
        done
        echo "    previous release restored — restart the bubble to load it:"
        echo "      systemctl --user restart handsoff   (or: ~/.local/bin/handsoff-restart)"
        exit 0
        ;;
esac

WHISPER_SIZE="${HANDSOFF_WHISPER:-tiny}"
# Which model step 8 judges and pulls. Resolved just below, once the interpreter
# is known: the app reads its model from settings.json (`model` — see
# `OLLAMA_MODEL = str(SETTINGS["model"])`), and the deployment default is only
# the last resort. The monolith has no `HANDSOFF_MODEL` in its unit either, so a
# machine that has been running a while has the user's choice on disk and the
# hardcoded name is a model NOTHING loads: this machine's settings.json said
# `gemma4:12b` while step 8 warned about `qwen3:8b`, and on a host that did not
# already have that model the installer would have PULLED it (several GB) and
# then judged its tool support — work about the wrong model, reported to a user
# who never chose it.
OLLAMA_MODEL=""
# Speech engine weights. chatterbox-turbo replaced Piper, so this is a
# Hugging Face repo (a directory of safetensors) rather than a single .onnx
# voice file. huggingface_hub fetches content-addressed blobs and verifies
# them on download, so there is no sha256 to pin here.
TTS_REPO="${HANDSOFF_TTS_REPO:-ResembleAI/chatterbox-turbo}"
# ponytail: resolve once; venv's python3 shadows system when activated.
PYBIN="${HANDSOFF_PYTHON:-$(command -v python3 2>/dev/null || echo /usr/bin/python3)}"
WHISPER_REVISION="${HANDSOFF_WHISPER_REVISION:-main}"

# The model the app is actually configured with, read from the same file the app
# reads. Every failure (no file yet, no key, an interpreter that will not answer)
# prints nothing and leaves the default below in force, so a first install on a
# bare machine still has a model to pull and nothing here can stop the install.
_configured_model() {
    "$PYBIN" - "$CONF_DIR/settings.json" <<'PY_EOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        value = json.load(fh).get("model")
except Exception:
    value = None
if isinstance(value, str) and value.strip():
    sys.stdout.write(value.strip())
PY_EOF
}
OLLAMA_MODEL="${HANDSOFF_MODEL:-$(_configured_model)}"
[ -n "$OLLAMA_MODEL" ] || OLLAMA_MODEL="qwen3:8b"

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
    rm -f "$HOME/.config/systemd/user/handsoff.service"
    # Remove exactly what was deployed, read back from the manifest the
    # installer wrote — it is generated from the shipped set, so a module added
    # later is removed too. ~/.local/bin is a shared user directory and is
    # never globbed for deletion; the fallback is the required floor.
    _deployed=0
    PY="${PYBIN:-$(command -v python3 2>/dev/null || true)}"
    if [ -n "$PY" ] && [ -f "$CONF_DIR/deployment.json" ]; then
        _manifest_files="$("$PY" - "$CONF_DIR/deployment.json" 2>/dev/null <<'PY_EOF' || true
import json, sys
# Only relative paths inside the deployment tree are ever named, so a corrupt
# or hand-edited manifest cannot point the uninstaller outside ~/.local/bin.
try:
    files = json.load(open(sys.argv[1]))["files"]
except Exception:
    raise SystemExit(0)
for rel in sorted(files):
    parts = rel.split("/")
    if rel.startswith("/") or ".." in parts or "" in parts:
        continue
    print(rel)
PY_EOF
)"
        for rel in $_manifest_files; do
            rm -f "$BIN_DIR/$rel" && _deployed=1
        done
    fi
    if [ "$_deployed" = "0" ]; then
        # No manifest (an older install, or it was removed): fall back to the
        # hard-imported floor plus the two optional/non-Python artifacts.
        for rel in $TOP_REQUIRED handsoff-settings.py handsoff-restart; do
            rm -f "$BIN_DIR/$rel"
        done
        for m in $CORE_REQUIRED; do
            rm -f "$BIN_DIR/core/$m.py"
        done
        # The checkout IS the module list (this script lives in it), which is
        # the same rule staging uses. The floor alone would leave every module
        # added later (core/theme.py, …) behind, so the directory could never
        # become empty enough to remove and uninstall would litter.
        for src in "$HERE"/core/*.py; do
            if [ -f "$src" ]; then
                rm -f "$BIN_DIR/core/$(basename "$src")"
            fi
        done
    fi
    # Never `rm -rf` this directory. ~/.local/bin is a SHARED user directory and
    # `core` is a plausible name for somebody else's package; the manifest loop
    # above has already removed every file this install deployed. Drop the
    # directory only when it is ours to drop — empty, or holding nothing but
    # bytecode we generated.
    if [ -d "$BIN_DIR/core" ]; then
        if find "$BIN_DIR/core" -mindepth 1 \
                ! -name '__pycache__' ! -name '*.pyc' 2>/dev/null | head -n1 | grep -q .; then
            echo "    kept $BIN_DIR/core — it still holds files this install did not deploy"
        else
            rm -rf "$BIN_DIR/core"
        fi
    fi
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
# this covers the lazy-imported extras (faster-whisper, openwakeword/
# onnxruntime for the wake spotter) at verified floors. The speech engine is
# deliberately NOT here — see step [2b/8] below.
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

echo "==> [2b/8] Speech engine (chatterbox-turbo)"
# The engine is kept OUT of requirements.txt on purpose: it pulls torch, and CI
# installs the manifest on GPU-less runners, where that would cost every job
# gigabytes and buy nothing (the suite asserts torch is never imported).
# install.sh owns it instead — the same way it owns ollama and the whisper
# model. Torch comes from the distro when the distro has it: the packaged build
# matches the system CUDA/ROCm stack, while PyPI's default Linux wheel drags its
# own ~2-3 GB of NVIDIA libraries.
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping speech engine (rehearsal)"
elif "${PYBIN}" -c "import chatterbox.tts_turbo" >/dev/null 2>&1; then
    # tts_turbo is this engine's entry point AND it imports torch, so a
    # successful import proves the whole stack — which keeps a redeploy from
    # re-resolving gigabytes on every run.
    echo "    chatterbox-turbo already importable — skipping"
else
    TORCH_PKGS=""
    for p in python-torch python-torchaudio; do
        if pacman -Si "$p" >/dev/null 2>&1; then
            TORCH_PKGS="$TORCH_PKGS $p"
        fi
    done
    if [ -n "$TORCH_PKGS" ] && [ -n "$PACMAN" ]; then
        echo "    installing distro torch:$TORCH_PKGS"
        $PACMAN -S --needed --noconfirm $TORCH_PKGS \
            || echo "    note: distro torch install failed — pip will provide it" >&2
    else
        echo "    no distro torch — pip will pull its default wheel (large)"
    fi
    "${PYBIN}" -m pip install --user --break-system-packages --upgrade \
        "chatterbox-tts>=0.1.7"
    if ! "${PYBIN}" -c "import chatterbox.tts_turbo" >/dev/null 2>&1; then
        echo "    FATAL: chatterbox-turbo is not importable after install — the" >&2
        echo "      bubble would boot mute. Install it by hand and re-run:" >&2
        echo "        ${PYBIN} -m pip install --user chatterbox-tts" >&2
        exit 1
    fi
    echo "    speech engine ready"
fi

echo "==> [3/8] Directories"
mkdir -p "$BIN_DIR" "$CONF_DIR/whisper-model" "$CONF_DIR/releases" "$STATE_DIR"

echo "==> [4/8] Staging the release (compile-gated, rollback-able)"
# Nothing is installed until a complete staged copy has passed the compile
# and import gates; the currently-deployed set is kept at
# $CONF_DIR/releases/prev so `install.sh --rollback` can restore it.
RELEASES_DIR="$CONF_DIR/releases"
# mktemp, never a PID-suffixed name: the PID is predictable and two installs
# can run at once (the bubble's own self-edit restart racing a manual run),
# which would have them staging into, gating and switching the SAME directory.
STAGE_DIR="$(mktemp -d "$RELEASES_DIR/staged.XXXXXX")" || {
    echo "    FATAL: could not create a staging directory under $RELEASES_DIR" >&2
    exit 1
}
PREV_DIR="$RELEASES_DIR/prev"
mkdir -p "$STAGE_DIR/core"
stage_fail() {
    echo "    FATAL: $1" >&2
    rm -rf "$STAGE_DIR"
    exit 1
}
# --- collect the shipped set into the stage (live files untouched yet)
# Globs, not lists, but only over files the project OWNS: whatever the repo
# tracks, plus the declared entry points, and TOP_REQUIRED fails the stage if a
# hard-imported module has gone missing. Membership is decided in one place
# (ship_file) so staging, the manifest and the rehearsal check cannot disagree.
skip_unowned() {   # $1 = repo-relative path that failed the membership test
    if [ "$HAVE_GIT" = "1" ]; then
        echo "    NOT shipping $1 — untracked in git, so it is not part of the project"
        echo "      (commit it if it is a module; it would land in $BIN_DIR)"
    else
        echo "    NOT shipping $1 — not a declared module, and this is not a git"
        echo "      checkout, so there is nothing to ask. If it belongs to the"
        echo "      project, name it in TOP_REQUIRED/CORE_REQUIRED in install.sh."
    fi
}
for src in "$HERE"/*.py; do
    base="$(basename "$src")"
    ship_top "$base" || { skip_unowned "$base"; continue; }
    if is_exec "$base"; then m=755; else m=644; fi
    install -m "$m" "$src" "$STAGE_DIR/$base" \
        || stage_fail "could not stage $base"
done
for mod in $TOP_REQUIRED; do
    [ -f "$HERE/$mod" ] \
        || stage_fail "$HERE/$mod is missing but required by handsoff.py"
done
# core/ ships as a SET, staged by glob; the floor (CORE_REQUIRED, defined with
# the rest of the shipped set at the top) fails the stage loudly if a
# hard-imported module disappears.
for m in $CORE_REQUIRED; do
    [ -f "$HERE/core/$m.py" ] \
        || stage_fail "$HERE/core/$m.py is missing but required by handsoff.py"
done
for src in "$HERE"/core/*.py; do
    base="$(basename "$src")"
    ship_core "$base" || { skip_unowned "core/$base"; continue; }
    install -m 644 "$src" "$STAGE_DIR/core/$base" \
        || stage_fail "could not stage core/$base"
done
install -m 755 "$HERE/handsoff-restart" "$STAGE_DIR/handsoff-restart" \
    || stage_fail "could not stage handsoff-restart"
# --- gate 1: every staged Python file must byte-compile before it can ship
STAGED_PY=("$STAGE_DIR"/*.py)
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
    # Save the deployed copy of every file we ship — taken from the stage's own
    # list, so an unrelated .py a user keeps in ~/.local/bin is never swept into
    # prev and then restored over something later.
    for f in "$STAGE_DIR"/*.py "$STAGE_DIR/handsoff-restart"; do
        base="$(basename "$f")"
        [ -f "$BIN_DIR/$base" ] && cp -p "$BIN_DIR/$base" "$PREV_DIR.staging/$base"
    done
    # Driven by the STAGED set, not by a sweep of $BIN_DIR/core: the same
    # reason the switch above is built from the stage. A glob over the
    # destination swept every .py a user happens to keep in ~/.local/bin/core
    # into prev, and `--rollback` then restored those foreign files over the
    # release. Only what this installer ships is a rollback target.
    for f in "$STAGE_DIR"/core/*.py; do
        base="$(basename "$f")"
        [ -f "$BIN_DIR/core/$base" ] \
            && cp -p "$BIN_DIR/core/$base" "$PREV_DIR.staging/core/$base"
    done
    mv "$PREV_DIR.staging" "$PREV_DIR"
    HAD_PREV=1
fi
# --- the switch: install staged files; any failure restores the previous set
# Built from the staged tree rather than listed: whatever was staged -- and
# therefore byte-compiled by the gate above -- is exactly what switches in.
SWITCH_FILES_755="handsoff-restart"
SWITCH_FILES_644=""
for f in "$STAGE_DIR"/*.py; do
    base="$(basename "$f")"
    if is_exec "$base"; then
        SWITCH_FILES_755="$SWITCH_FILES_755 $base"
    else
        SWITCH_FILES_644="$SWITCH_FILES_644 $base"
    fi
done
for f in "$STAGE_DIR"/core/*.py; do
    SWITCH_FILES_644="$SWITCH_FILES_644 core/$(basename "$f")"
done
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
# on download) — like the speech weights, there is no separate sha256 to check.
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
# Hash every core module staged (glob, not a list) so doctor's drift check
# covers a new module the moment it ships -- the reason the missing
# core/theme.py went unnoticed is that the manifest never mentioned it.
manifest_core_files=""
for f in "$HERE"/core/*.py; do
    base="$(basename "$f")"
    rel="core/$base"
    ship_core "$base" || continue     # same membership rule as staging
    manifest_core_files="$manifest_core_files
    \"$rel\": {\"source_sha256\": \"$(sha_of "$HERE/$rel")\", \"installed_sha256\": \"$(sha_of "$BIN_DIR/$rel")\"},"
done
# Same for the top-level modules: discovered beside handsoff.py, so doctor's
# drift check and `--uninstall` both learn about a new file automatically.
manifest_top_files=""
for f in "$HERE"/*.py; do
    rel="$(basename "$f")"
    # Same membership rule as staging, from the same helper: a file that is not
    # shipped must not be hashed into the manifest either, or doctor's drift
    # check tracks something the bubble never uses.
    ship_top "$rel" || continue
    manifest_top_files="$manifest_top_files
    \"$rel\": {\"source_sha256\": \"$(sha_of "$HERE/$rel")\", \"installed_sha256\": \"$(sha_of "$BIN_DIR/$rel")\"},"
done
atomic_write "$CONF_DIR/deployment.json" 600 <<MANIFEST_EOF
{
  "installed_at": "$(date -Is)",
  "source_dir": "$HERE",
  "whisper_model": "$WHISPER_SIZE",
  "whisper_revision": "$WHISPER_RESOLVED_REVISION",
$manifest_whisper_sha256
  "python": "$PYBIN",
  "files": {
$manifest_top_files
$manifest_core_files
    "handsoff-restart": {"source_sha256": "$(sha_of "$HERE/handsoff-restart")", "installed_sha256": "$(sha_of "$BIN_DIR/handsoff-restart")"}
  }
}
MANIFEST_EOF

echo "==> [6/8] Speech weights ($TTS_REPO)"
# chatterbox-turbo replaced the 60 MB piper .onnx voice: the engine is a 3.8 GB
# neural model on the Hugging Face hub, so there is nothing left to checksum-pin
# here — huggingface_hub fetches content-addressed blobs and verifies each one
# as it downloads. What the installer must still guarantee is that the cache it
# leaves behind is USABLE: an interrupted or blocked fetch used to be invisible
# until the first spoken reply, which is the worst moment to learn the bubble is
# mute. So the fetch is primed here and then verified.
if [ "$REHEARSAL" = "1" ]; then
    echo "    skipping speech weights (rehearsal)"
else
    "${PYBIN}" - "$TTS_REPO" <<'PY_EOF'
import os
import sys
from pathlib import Path

REPO = sys.argv[1]


def hub_cache() -> Path:
    """The hub cache huggingface_hub will use.

    Mirrors core.audio.hf_hub_cache() exactly (and hardware.py's copy): the
    installer must prime the SAME directory the bubble reads, or the download is
    paid for twice.
    """
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            return Path(os.environ[var]).expanduser()
    hf_home = os.environ.get("HF_HOME")
    base = (Path(hf_home).expanduser() if hf_home
            else Path.home() / ".cache" / "huggingface")
    return base / "hub"


def snapshot_ok(root: Path) -> bool:
    """True when the cache holds a snapshot with real weights.

    `root.is_dir()` is not the question: the cache nests files under
    snapshots/<revision>/, so a fetch that started and died leaves directories
    with no blobs — reported as "cached" by a weaker check, and the bubble then
    fails on every turn.
    """
    snapshots = root / "snapshots"
    if not snapshots.is_dir():
        return False
    for snap in snapshots.iterdir():
        if snap.is_dir() and any(
                p.is_file() and p.stat().st_size > 0
                for p in snap.glob("*.safetensors")):
            return True
    return False


root = hub_cache() / ("models--" + REPO.replace("/", "--"))
if snapshot_ok(root):
    print(f"    {REPO} already cached at {root}")
else:
    print(f"    downloading {REPO} (about 3.8 GB on the first run)")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit(f"    FATAL: huggingface_hub is missing ({exc}) — it "
                         f"arrives with faster-whisper; re-run install.sh")
    try:
        snapshot_download(REPO)
    except Exception as exc:
        raise SystemExit(f"    FATAL: could not download {REPO}: {exc}")

if not snapshot_ok(root):
    raise SystemExit(
        f"    FATAL: {REPO} has no usable weights under {root} — the bubble "
        f"would start MUTE and only fail on its first spoken reply. Check the "
        f"network or proxy, then re-run install.sh.")
print(f"    speech weights ready: {root}")
PY_EOF
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
    # The floor derives from the same one definition as everything else, so it
    # cannot drift; the non-Python artifacts are named explicitly.
    for required in $TOP_REQUIRED handsoff-restart; do
        [ -f "$BIN_DIR/$required" ] \
            || { echo "FATAL: rehearsal missing $BIN_DIR/$required" >&2; exit 1; }
    done
    for required in "$SYSTEMD_DIR/handsoff.service" "$CONF_DIR/niri-window-rule.kdl"; do
        [ -f "$required" ] || { echo "FATAL: rehearsal missing $required" >&2; exit 1; }
    done
    # Every module the PROJECT owns must have reached the deployed set -- the
    # exact check that would have caught the missing core/theme.py, and it runs
    # on every rehearsal from now on. Membership is the same `ship_top` rule
    # staging used, so this asserts what was supposed to ship, not what happens
    # to be sitting in the checkout (a scratch test.py is not ours to deliver).
    for src in "$HERE"/*.py; do
        base="$(basename "$src")"
        ship_top "$base" || continue
        [ -f "$BIN_DIR/$base" ] \
            || { echo "FATAL: rehearsal did not deploy $base" >&2; exit 1; }
    done
    for src in "$HERE"/core/*.py; do
        base="$(basename "$src")"
        ship_core "$base" || continue
        [ -f "$BIN_DIR/core/$base" ] \
            || { echo "FATAL: rehearsal did not deploy core/$base" >&2; exit 1; }
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
