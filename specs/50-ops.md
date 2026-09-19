# handsoff — operations

Sources: `install.sh`, `handsoff.py` (`main`, `_deployment_snapshot`,
`ControlServer`, `ptt_client`), `handsoff-restart`, `hardware.py`,
`core/doctor.py`.

## 1. Install (`./install.sh`, Arch/CachyOS + niri)

1. `pacman -Syu` + probes per package: python-pyside6, python-sounddevice,
   python-numpy, ollama, curl (+ Qt/ALSA runtime libs).
2. `pip install --user -r requirements.txt` (numpy, PySide6, sounddevice;
   faster-whisper/openwakeword/onnxruntime lazy) + distro-torch-first
   chatterbox-turbo (`HANDSOFF_TTS_REPO`).
3. Stage to `mktemp -d` (never predictable `staged.$$`): shipped set =
   `TOP_REQUIRED` (`handsoff.py settings_schema.py hardware.py`) +
   `handsoff-settings.py` + `handsoff-restart` + `core/` set
   (`CORE_REQUIRED`: `__init__ registry settings audio brain tools doctor
   lifecycle calendar assistant bubble web theme`) + any further
   `git ls-files '*.py'`; tarball (no git) fallback = declared set only.
   `CORE_REQUIRED` missing → stage fails loudly. `py_compile` gate on stage.
4. Whisper model SHA256-verified atomic download (`HANDSOFF_WHISPER`,
   `HANDSOFF_WHISPER_REVISION`); chatterbox weights prefetched + usability
   checked.
5. Atomic switch `~/.local/bin` (0755 entry points), previous release kept
   for `--rollback`; manifest written to `deployment.json` (per-file
   sha256, whisper sha, model ids).
6. systemd user unit `handsoff.service`: `ExecStart %h/.local/bin/handsoff.py`,
   `Restart=always`, `PartOf=graphical-session.target`,
   `WantedBy=graphical-session.target`. Exactly ONE autostart owner:
   systemd enabled → no niri `spawn-at-startup` (checked, not assumed).
7. niri snippet print (`niri-window-rule.kdl` merge) + `reload-config` hint.
8. Flags: `--help` (pure, CI-smoked), `--rehearsal` (redirect HOME, no host
   changes), `--rollback`, `--uninstall` (manifest loop; `core/` + `~/.local/bin`
   dropped only when empty-or-bytecode — never `rm -rf` a shared dir),
   `--uninstall --purge` (backs up config/state first).

**One source per provisioned value.** A step that provisions something the app
has already decided reads the app rather than repeating the decision:

| step | read from | how |
|---|---|---|
| 4 whisper model | `settings.json` → `whisper_size` | `_resolve_whisper_size`; a size outside `settings_schema.WHISPER_SIZES` is refused with the list named |
| 4 whisper revision | `HANDSOFF_WHISPER_REVISION` | env only — nothing in the app decides it |
| 6 speech repo | `core/audio.py` → `TTS_REPO_ID` | `_app_constant` (`ast`, never an import: `handsoff.py` builds a QApplication) |
| 7 app-id in the niri rule | `handsoff.py` → `APP_NAME` | same reader; the rule text takes `@APP_ID@` and `sed` substitutes it (an unquoted heredoc would treat `$"` as a locale expansion) |
| 8 model | `settings.json` → `model` | `_read_setting` |
| 8 server | `settings.json` → `ollama_host` | `_resolve_ollama_endpoint`, which mirrors the app's own remote rule: a non-loopback host is used only when `allow_remote_ollama` is **strictly** `true` in settings (or the send guard's env tokens), otherwise the script says so and checks loopback, because the bubble refuses that endpoint anyway |

The reader is failure-tolerant by construction (missing file, missing key, an
interpreter that will not answer): it prints nothing and the named `DEFAULT_*`
fallback stands, so no state of the user's config can stop an install. Step 8
points the `ollama` CLI at the same endpoint the app uses (`OLLAMA_HOST`) and
never starts the local service for a remote host. Three literals that were
provisioned twice are gone with it: a configured `whisper_size: small` used to
have `tiny` downloaded and recorded in the manifest; a hardcoded loopback was
probed, started and filled while the bubble talked to another server; and the
speech repo name was a second copy of the one the app reads.

## 2. Deployment truth

`_deployment_snapshot()` (`handsoff.py:758`) hashes running vs repo vs
installed per file over `manifest.files ∪ _DEPLOY_FILES`, compares hashes
(not mtimes). Statuses: `in-sync`, `installed-drift`, `source-unknown`,
`installed-missing`, `running-missing`. Surfaced three ways: `--ptt doctor`
(`deployment: in-sync — …`), `--ptt health` (`"deployment"` section), settings
health bar (`deploy: ok`). No settings values or calendar secrets in the
payload. `--preflight` (`hardware.py main`) runs on a bare checkout (stdlib
only).

## 3. Runtime (`main()`)

`_prepare_runtime()` (private dirs + 0600, symlink-refuse) → logging +
`threading.excepthook` + faulthandler on `crash.log` → version/settings/
brain/mic banner → `acquire_lock()` (flock; second copy exits 0) →
`QApplication` (`setApplicationName`, Wayland app-id for niri rules) →
`Assistant.start()` → `ControlServer.start()` → `bubble.show()` →
`app.exec()` → stop/shutdown/join on quit. Headless without
`WAYLAND_DISPLAY/DISPLAY` warns, window may fail. One bubble per session.

## 4. Control socket (22 verbs)

`PTT_ACTIONS` (`handsoff.py:6500`): start stop toggle interrupt handsfree
handsfree-on handsfree-off handsfree-status dictation dictation-on
dictation-off status health level doctor settings selftest reload-settings
clear-history say preview-pack preview-clear. Read-only (no token):
`status health level doctor handsfree-status`. Everything else needs
`token=<64 hex>` first line (constant-time compare); token lives in
`control.token` (0700 state dir + `_peer_uid` gate). Request caps: 65536 B,
5 s wall budget; single accept thread; runs admitted via
`BoundedRegistry("control", 1)`; diagnostics via `("diagnostic", 1)` (second
request refused by name with the cause in the reply). `ptt_client` maps verbs
to exit codes; `USAGE` lists the surface; unknown verb echoes the sorted set.

## 5. Doctor, health, selftest

`run_doctor()`/`doctor_json()` (`core/doctor.py` via `DoctorDeps`):
deployment hashes, Ollama reachability + model, TTS/STT weight state, mic,
niri/compositor, systemd unit, ydotool socket (DGRAM-first probe mirroring
`ToolBelt._socket_connectable`), voices, restart script, control socket,
crash log, cap-refusal note, appearance look, web backend record
(observed-only: `untried`/ok/named failure). `--ptt health` = JSON snapshot
(status/state/handsfree/model/deployment/look/followup). `--ptt selftest` =
typing checks of ACCEPTANCE §5. Settings health bar renders
`mic: … · brain: ok <model> · tts/stt: ok · deploy: ok` via `_fmt_health()`.

## 6. Restart + crash recovery

`handsoff-restart`: systemd-first (`restart handsoff` when a unit manages
the bubble; manual TERM/KILL only otherwise — the reverse order once caused
a transient double bubble via `Restart=always` racing the script).
Crash: SEGV → systemd restarts ~5 s; next boot speaks a short report and
names the crash log (truncated-not-unlinked, same inode, 0600). Crash-loop
(5+ rapid) → unit `failed` (`Start request repeated too quickly`);
`reset-failed + start` recovers. Session end → clean exit via
`PartOf=graphical-session.target`.

## 7. Environment + deps

Needs: niri, Ollama (`OLLAMA_HOST`, default loopback; remote requires
`allow_remote_ollama` opt-in + guard), MPD+`mpc` (music), `ydotool`
(typing/clicks; socket `$XDG_RUNTIME_DIR/.ydotool_socket` then legacy
`/tmp/.ydotool_socket`), `grim`/`wl-paste`/`wl-copy` (screen/clipboard),
ImageMagick (wallpaper match), `nvidia-smi` (VRAM), D-Bus `dbus-monitor`
(notifications), SearXNG optional localhost:8888. `requirements.txt`:
numpy/PySide6/sounddevice hard; faster-whisper/openwakeword/onnxruntime
lazy; torch/chatterbox owned by install.sh (suite asserts torch never
imported). `requirements-lock.txt` pins CI (numpy 2.5.3, PySide6 6.11.2,
sounddevice 0.5.6, faster-whisper 1.2.1, openwakeword 0.4.0,
onnxruntime 1.29.0, pytest 9.1.1).
