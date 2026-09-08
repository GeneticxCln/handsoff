#!/usr/bin/env bash
# handsoff installer — Arch Linux / CachyOS + niri (Wayland)
#
# Installs system + pip packages, downloads the whisper model and a piper
# voice, and places handsoff.py + the restart script in ~/.local/bin.
# Run it from the directory containing handsoff.py:
#
#   ./install.sh
#
# Overrides (optional): HANDSOFF_MODEL, HANDSOFF_WHISPER, PIPER_VOICE_URL
set -euo pipefail

# No-arg flags that must never touch the system: the CI smoke test runs these
# so installer drift (syntax rot, broken early flow) is caught on every push.
case "${1:-}" in
    -h|--help)
        sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        echo ""
        echo "Options:"
        echo "  --help            show this help"
        echo "  --uninstall       remove binaries, unit and snippet"
        echo "  --uninstall --purge  also wipe config/state (backs up first)"
        exit 0
        ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CONF_DIR="$HOME/.config/handsoff"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/handsoff"

WHISPER_SIZE="${HANDSOFF_WHISPER:-tiny}"
OLLAMA_MODEL="${HANDSOFF_MODEL:-qwen3:8b}"
PIPER_VOICE_URL="${PIPER_VOICE_URL:-https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx}"

if [ "${1:-}" = "--uninstall" ]; then
    echo "==> Uninstalling handsoff"
    systemctl --user disable --now handsoff.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/handsoff.service" \
          "$BIN_DIR/handsoff.py" "$BIN_DIR/handsoff-restart" \
          "$BIN_DIR/handsoff-settings.py"
    systemctl --user daemon-reload 2>/dev/null || true
    pkill -TERM -f '[h]andsoff.py' 2>/dev/null || true
    if [ "${2:-}" = "--purge" ]; then
        tar czf "$HOME/handsoff-backup-$(date +%Y%m%d).tar.gz" \
            -C "$HOME" .config/handsoff .local/state/handsoff 2>/dev/null || true
        rm -rf "$CONF_DIR" "$STATE_DIR"
        echo "    config + models removed (backup: ~/handsoff-backup-*.tar.gz)"
    else
        echo "    kept $CONF_DIR and $STATE_DIR (history, models)."
        echo "    full wipe:  $0 --uninstall --purge"
    fi
    echo "handsoff uninstalled."
    exit 0
fi

echo "==> [1/8] System packages (pacman)"
# -Sy alone would create an unsupported partial-upgrade state on Arch:
# refresh the DB AND upgrade the system in one supported transaction.
echo "    refreshing databases and upgrading the system (supported Arch policy)"
sudo pacman -Syu --needed --noconfirm \
    python-pyside6 python-sounddevice python-numpy python-pip \
    alsa-utils ollama curl

echo "==> [2/8] Python packages (pip, user site)"
# Single source of truth: the manifest. Core deps also come from pacman above;
# this covers the lazy-imported extras (faster-whisper, piper-tts,
# openwakeword/onnxruntime for the wake spotter) at verified floors.
python -m pip install --user --break-system-packages --upgrade \
    -r "$HERE/requirements.txt"

echo "==> [3/8] Directories"
mkdir -p "$BIN_DIR" "$CONF_DIR/whisper-model" "$CONF_DIR/piper-voice" "$STATE_DIR"

echo "==> [4/8] Placing handsoff.py, settings app and restart script"
install -m 644 "$HERE/handsoff.py" "$BIN_DIR/handsoff.py"
if [ -f "$HERE/handsoff-settings.py" ]; then
    install -m 644 "$HERE/handsoff-settings.py" "$BIN_DIR/handsoff-settings.py"
fi
# Single source of truth: the repo's handsoff-restart is shipped as-is
# (a heredoc duplicate here silently drifted from it once already).
install -m 755 "$HERE/handsoff-restart" "$BIN_DIR/handsoff-restart"
echo "==> [5/8] Downloading whisper '$WHISPER_SIZE' model (one time)"
python - "$WHISPER_SIZE" "$CONF_DIR/whisper-model" <<'PY_EOF'
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1], device="cpu", compute_type="int8", download_root=sys.argv[2])
print("whisper model ready")
PY_EOF

echo "==> [6/8] Downloading piper voice (sha256-verified)"
voice="$(basename "$PIPER_VOICE_URL")"
VOICE_SHA256="${PIPER_VOICE_SHA256:-5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f}"
VOICE_JSON_SHA256="${PIPER_VOICE_JSON_SHA256:-efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0}"
download_verified() {  # <url> <dest> <expected-sha256>
    local url="$1" dest="$2" want="$3" got
    # existing files are verified too: an interrupted earlier download must
    # not be silently accepted (a truncated voice crashes piper at runtime)
    if [ -f "$dest" ]; then
        got="$(sha256sum "$dest" | awk '{print $1}')"
        if [ "$got" == "$want" ]; then
            return 0
        fi
        echo "    existing $(basename "$dest") failed checksum — re-downloading" >&2
        rm -f "$dest"
    fi
    local tmp="$dest.part.$$"
    curl -fL --retry 3 -o "$tmp" "$url"
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
download_verified "$PIPER_VOICE_URL" "$CONF_DIR/piper-voice/$voice" "$VOICE_SHA256"
download_verified "$PIPER_VOICE_URL.json" "$CONF_DIR/piper-voice/$voice.json" "$VOICE_JSON_SHA256"

echo "==> [7/8] systemd user service (auto-restart if the bubble dies)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/handsoff.service" <<'UNIT_EOF'
[Unit]
Description=handsoff voice assistant bubble
After=graphical-session.target
PartOf=graphical-session.target
# crash-loop guard: stop after 5 failures in 120s (MUST live in [Unit], not [Service])
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
ExecStart=/usr/bin/python %h/.local/bin/handsoff.py
# Restart=always: recover from clean exits too (stray SIGTERM, Quit menu click,
# app.quit()) — the only quiet exit we honour is a real desktop shutdown (PartOf).
Restart=always
RestartSec=3
TimeoutStartSec=30

[Install]
WantedBy=graphical-session.target
UNIT_EOF
if systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable handsoff.service 2>/dev/null || true
    echo "    installed + enabled: systemctl --user start handsoff   (auto-restarts on crash)"
    echo "    note: remove 'spawn-at-startup' for handsoff from niri's autostart to avoid double-start"
else
    echo "    WARN: systemd user session not reachable; keeping niri spawn-at-startup as the autostart"
fi

echo "==> [8/8] Checking ollama"
if ! curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; then
    echo "    starting the ollama service ..."
    sudo systemctl enable --now ollama || true
    sleep 2
fi
if ! ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
    echo "    pulling $OLLAMA_MODEL (a few GB, one time) ..."
    ollama pull "$OLLAMA_MODEL" || echo "    WARN: pull failed — run 'ollama pull $OLLAMA_MODEL' later"
fi
if ! ollama show "$OLLAMA_MODEL" 2>/dev/null | grep -qi 'tools'; then
    echo "    WARN: $OLLAMA_MODEL may not support tool calling — desktop control and"
    echo "    self-modification need a tools-capable model (e.g. qwen3:8b, llama3.1:8b)."
    echo "    handsoff will still chat, but set HANDSOFF_MODEL to enable tools."
fi

cat > "$CONF_DIR/niri-window-rule.kdl" <<'NIRI_EOF'
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
if systemctl --user is-enabled handsoff.service >/dev/null 2>&1; then
    # systemd owns the bubble — print the manual keybind for settings,
    # do NOT write spawn-at-startup (a unit restart would spawn duplicates)
    echo "     (systemd manages autostart; add 'Mod+Shift+S => spawn settings' to niri manually)"
else
    printf '\n// launch at startup:\nspawn-at-startup "python" "%s"\n' \
        "$BIN_DIR/handsoff.py" >> "$CONF_DIR/niri-window-rule.kdl"
    echo "     (no systemd: added spawn-at-startup — check the path contains no wrong username)"
fi

echo
echo "handsoff installed."
echo "  1. Merge $CONF_DIR/niri-window-rule.kdl into ~/.config/niri/config.kdl"
echo "     then: niri msg action reload-config"
echo "  2. Start it now with:  systemctl --user start handsoff  (or: python ~/.local/bin/handsoff.py)"
echo "  3. Uninstall anytime with:  $0 --uninstall"

