# handsoff — spec index + vision

Status: normative for the checkout it ships with. When code and spec disagree,
the code runs and the spec is wrong — fix the spec in the same change.

## What this is

Self-modifying local voice assistant. Small round Qt bubble on Arch/CachyOS +
niri Wayland. Hold-to-talk or hands-free wake, local STT (faster-whisper),
local brain (Ollama), local TTS (chatterbox-turbo), 48 model-callable tools,
settings GUI, systemd service, control socket for keybinds.

## Non-goals

- Not portable. niri + Arch/CachyOS + Wayland only. No GNOME/KDE/Windows/macOS.
- No cloud account, no hosted brain by default (`allow_remote_ollama: False`).
- No mouse-first agent: pointer tools exist but `operator` permission is OFF.
- No silent failure: fault paths degrade loudly (journal + spoken cause).

## Map

| Spec | Contents | Single source of truth |
|---|---|---|
| `00-overview.md` | this file | — |
| `10-requirements.md` | functional + non-functional + permission model | `settings_schema.py`, `core/tools.py` |
| `20-architecture.md` | modules, DI, threads, state machine, voice pipeline | `handsoff.py`, `core/*` |
| `30-tools-api.md` | 48-tool census, gates, whitelist, confirm/job/watcher contracts | `core/tools.py` (AST) |
| `40-data.md` | 59 settings keys, state files, caps, formats | `settings_schema.py`, `core/settings.py` |
| `50-ops.md` | install, deploy manifest, systemd, socket, doctor | `install.sh`, `handsoff.py` |
| `60-test-plan.md` | test inventory, CI gates, coverage | `tests/`, `.github/workflows/ci.yml` |
| `90-audit.md` | audit findings, ranked risks, drift notes | this audit (2026-09-15) |

## Pre-existing docs and their rank

| Doc | Role, not replaced |
|---|---|
| `README.md` (729 lines) | user manual: install, usage, voice loop, tools tour |
| `ACCEPTANCE.md` (214 lines) | hardware checklist: must execute on the real desk, not read |
| `GAP_ANALYSIS.md` (4025 lines) | audit log + roadmap: dated defect batches, mutation guards |
| `docs/superpowers/specs/*.md` (4) | point designs: hardware-detector, live-watch, world-events, quality-hardening |
| `docs/superpowers/plans/*.md` (2) | hygiene + quality phase-1 plans |

These specs describe **what the tree promises**. Tutorials stay in `README.md`,
desk verification stays in `ACCEPTANCE.md`, history stays in `GAP_ANALYSIS.md`.

## Scale (measured 2026-09-15)

- `handsoff.py` 7057 lines (app + Assistant + ControlServer + voice pipeline).
- `handsoff-settings.py` 4812 lines (6-tab GUI).
- `core/`: 13 modules — `__init__` 306, `assistant` 595, `audio` 824,
  `brain` 215, `bubble` 4069, `calendar` 548, `doctor` 515, `lifecycle` 62,
  `registry` 435, `settings` 874, `theme` 313, `tools` 3330, `web` 972.
- `settings_schema.py` 319, `hardware.py` 529.
- Suite: 1390 collected / ~1300 `test_*` functions across 23 test files.

## Maintenance rule

Every table names its source symbol. A change that moves a constant, adds a
tool, adds a setting, or adds a module MUST update the spec row in the same
diff. Discovered-not-listed gates (`ci/compile_all.py`, `bash -n` by shebang,
`ship_file` via `git ls-files`) exist precisely because hand-maintained lists
drifted before (core/theme.py shipped nowhere while doctor said in-sync).
