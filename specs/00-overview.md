# handsoff — spec index + vision

Status: normative for the checkout it ships with. When code and spec disagree,
the code runs and the spec is wrong — fix the spec in the same change.

## What this is

Self-modifying local voice assistant. Small round Qt bubble on Arch/CachyOS +
niri Wayland. Hold-to-talk or hands-free wake, local STT (faster-whisper),
local brain (Ollama), local TTS (chatterbox-turbo), 52 model-callable tools,
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
| `30-tools-api.md` | 52-tool census, gates, whitelist, confirm/job/watcher contracts | `core/tools.py` (AST) |
| `40-data.md` | 65 settings keys, state files, caps, formats | `settings_schema.py`, `core/settings.py` |
| `50-ops.md` | install, deploy manifest, systemd, socket, doctor | `install.sh`, `handsoff.py` |
| `60-test-plan.md` | test inventory, CI gates, coverage | `tests/`, `.github/workflows/ci.yml` |
| `90-audit.md` | audit findings, ranked risks, drift notes | this audit (2026-09-15) |

## Pre-existing docs and their rank

| Doc | Role, not replaced |
|---|---|
| `README.md` | user manual: install, usage, voice loop, tools tour |
| `ACCEPTANCE.md` | hardware checklist: must execute on the real desk, not read |
| `GAP_ANALYSIS.md` | audit log + roadmap: dated defect batches, mutation guards |
| `docs/superpowers/specs/*.md` (4) | point designs: hardware-detector, live-watch, world-events, quality-hardening |
| `docs/superpowers/plans/*.md` (2) | hygiene + quality phase-1 plans |

These specs describe **what the tree promises**. Tutorials stay in `README.md`,
desk verification stays in `ACCEPTANCE.md`, history stays in `GAP_ANALYSIS.md`.

## Scale

- `core/`: 16 modules (sizes and ownership in `20-architecture.md` §1, which is
  GENERATED from the tree — a size copied into prose starts lying that day).
- `handsoff.py` is the app: bootstrap, `Assistant`, `ControlServer`, voice
  pipeline, memory, `main()`; `handsoff-settings.py` is the 6-tab GUI.
- The suite's own inventory is `60-test-plan.md` (where the counts are a dated
  snapshot and the file list is the contract).

Whatever this file states as a COUNT — the core module count here, the tool
census, the settings keys — is checked against the code by
`tests/test_specs_freshness.py`, and the two tables derived from the tree are
checked against `ci/spec_tables.py`. Prose that rots is prose that is wrong:
it carries no numbers it cannot keep.

## Maintenance rule

Every table names its source symbol. A change that moves a constant, adds a
tool, adds a setting, or adds a module MUST update the spec row in the same
diff. Discovered-not-listed gates (`ci/compile_all.py`, `bash -n` by shebang,
`ship_file` via `git ls-files`) exist precisely because hand-maintained lists
drifted before (core/theme.py shipped nowhere while doctor said in-sync).
