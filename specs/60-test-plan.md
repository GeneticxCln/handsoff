# handsoff — test plan

Sources: `tests/` (25 files + `conftest.py` + `fake_ollama.py`), `pytest.ini`,
`.coveragerc`, `.github/workflows/ci.yml`, `.gitlab-ci.yml`,
`githooks/pre-commit`, `ci/`.

## 1. Inventory

`tests/` — the FILE LIST is the contract: every `tests/test_*.py` must appear
below, and `test_specs_freshness.py` fails when one does not. The counts are the
2026-09-16 snapshot (1455 collected; 1353 `test_*` functions), so a stale count
in this table means nothing — a missing row does.

| File | `test_*` fns | Area |
|---|---|---|
| `test_audio.py` | 151 | Recorder, resample, whisper/TTS seams, levels, playback cancel |
| `test_regression.py` | 144 | cross-cutting pins: prompt text, tool census, marker, restart internals |
| `test_desktop.py` | 119 | niri/typing/windows/clipboard/screens, guards |
| `test_policy.py` | 100 | DecisionPolicy, gates, confirm flow, whitelist/blocked |
| `test_design_packs.py` | 98 | packs, image design, palettes, ink guard |
| `test_settings.py` | 80 | loader/coerce/migrate/merge/lock, looks catalogue |
| `test_settings_contract.py` | 24 | the field table: coercion, controls, cards, companion rows, the window |
| `test_lifecycle.py` | 80 | generations, staleness, PTT epoch, cancel/done |
| `test_ops.py` | 67 | deploy snapshot, restart, control socket, health/doctor |
| `test_web.py` | 64 | router, backends, cache, reader, SSRF refusals |
| `test_fault_injection.py` | 52 | boundary breakage, loud-degradation contract |
| `test_sandbox.py` | 52 | secret paths, edit boundaries, command validation |
| `test_calendar.py` | 45 | ICS parse/RRULE/format, scheme + label guards |
| `test_ci_summary.py` | 42 | `ci/pytest_summary.py` digest |
| `test_hardware.py` | 35 | snapshot sections, TTLs, probers |
| `test_theme.py` | 34 | hex parse, luminance, retune |
| `test_hardening.py` | 34 | perms, symlink/0600, token, caps, the no-core fallbacks (audio, brain) |
| `test_registry.py` | 29 | BoundedRegistry admission, Offer arm/consume |
| `test_assistant.py` | 25 | pomodoro/notifications/reminders/watcher ticks |
| `test_world_events.py` | 24 | fixed queries, severity, seen-store, cooldowns |
| `test_notify_coalesce.py` | 15 | notification batching/cooldown |
| `test_hardware_watch.py` | 13 | hardware watch tick, disk/VRAM alerts |
| `test_p0_fixes.py` | 6 | named P0 regressions |
| `test_specs_freshness.py` | 19 | the specs' counts and cells vs the code, the generated tables, and the file/module/spec lists |
| `test_settings_gui.py` | 1 (+ offscreen subprocess drivers) | Qt GUI incl. 10-design ink guard |
| `conftest.py` / `fake_ollama.py` | 0 | module loader (`handsoff_core` + alias), order-shuffle, fake brain |

## 2. CI gates (GitHub authoritative, GitLab mirrors)

- `tests` (py 3.12 + 3.13, `QT_QPA_PLATFORM=offscreen`): full suite.
- `coverage`: `--cov=. --cov-fail-under=70` (+ `COVERAGE_PROCESS_START` for
  offscreen-subprocess drivers, parallel combine).
- `order`: whole suite reshuffled by commit SHA, then file-order shuffled —
  both must pass (ordering-dependence probe).
- `compile`: `ci/compile_all.py` — DISCOVERED `*.py` (attic + hidden
  excluded), `py_compile` without imports; empty discovery fails the gate.
- `shell`: actions SHA-pinned (mutable `@vN` fails); `bash -n` over every
  file with a bash/sh shebang (finds extensionless `handsoff-restart`).
- `smoke`: `bash install.sh --help` contains `Options:` + `--uninstall`.
- `pytest.ini`: `PytestUnhandledThreadExceptionWarning` is an ERROR —
  a worker thread raising fails the build.
- pre-commit (`githooks/`, opt-in via `core.hooksPath`): staged
  `py_compile` + shebang `bash -n` + FULL suite (minutes — no duration is
  copied here, because a copied one rots) when shipped sources/workflows
  change; bypass only via `--no-verify`, deliberately.
- GitLab: same gates + pip cache on the lock file, junit report, and
  `after_script` digest (`ci/pytest_summary.py --post`) that prints failing
  names + env-fix hints even when the job dies.

## 3. Conventions that keep the suite honest

- Injections happen at the BOUNDARY (HTTP opener, recorder return, popen
  factory, write path, handed file object) — never by replacing the code
  under test (`README.md:51`).
- Faults assert the LOUD contract: journal WARNING/ERROR findable in
  `journalctl` + spoken line naming the cause + reported failure + toggle
  stops claiming on; AND the silence of the alternative: no fabricated
  success, no swallowed error, no value reported saved when it is not.
- Ordering: `HANDSOFF_TEST_ORDER_SEED` / `HANDSOFF_TEST_ORDER_FILES`
  reproduce any CI shuffle locally.
- Offscreen Qt: real bubble + settings GUI driven headless; ten-design ink
  guard pins every clamp (remove one → red); pixel-OCR avoided where
  multi-output screenshots proved unreliable (ACCEPTANCE §1: verified at the
  data layer through `_fmt_health` instead).
- Suite pins what humans forget: system-prompt text, 48-tool census,
  `SELF_MARKER`, restart-script internals, `BUBBLE_DESIGNS`↔schema agreement,
  looks catalogue vs loader bounds, `_DEPLOY_FILES` floor vs manifest.

## 4. Coverage floor (`.coveragerc`, TOTAL ≥70)

Omit: `tests/*`, `attic/*`, root `test.py` scratch, `site-packages`,
`shibokensupport`/`signature_bootstrap` phantoms. The per-file shape is not
restated here: this section used to list a percentage per file, and every one
of them had drifted by the time it was read. `coverage report` prints the
shape as it is today; the gate enforces the floor on TOTAL. The module that
needs tests most is the one only the subprocess GUI drivers exercise, which is
what the driver scenarios in §3 exist for. Raise with tests, never with
omit-patterns.

## 5. What tests cannot cover (desk truth in ACCEPTANCE.md)

 Audible TTS + echo rejection, live PTT/wake round trips, Yeti unplug-heal,
 barge-in timing, wallpaper-match aesthetics, shutdown-with-session,
 crash-loop systemd states. Each is a ☐/✅ line with `[auto]`/`[human]`
 codes and verified-by dates — the suite and the checklist are complements,
 not substitutes. GPU-less CI additionally cannot cover CUDA paths; the
 suite pins `torch` unimported instead.
