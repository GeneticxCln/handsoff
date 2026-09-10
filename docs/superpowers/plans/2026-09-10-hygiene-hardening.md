# Hygiene Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining low-risk UI, speech, deployment-provenance, coverage, and documentation gaps without changing the voice assistant's core product behavior.

**Architecture:** Keep the existing monolithic runtime and route only playback serialization, menu state, and deployment metadata through existing helpers. Add focused regression tests around the untested branches rather than restructuring the application.

**Tech Stack:** Python 3.12–3.14, PySide6, sounddevice, pytest, bash, systemd user service.

## Global Constraints

- No new dependencies.
- Preserve the existing PTT, notification, and speech behavior except where serialization prevents overlap.
- Keep `HANDSOFF_WHISPER_REVISION` configurable, but record a resolved model digest when available.
- Every behavioral change gets a focused regression test.
- Run `python3 -m py_compile` on changed Python files and `python3 -m pytest tests/ -q` before deployment.

---

### Task 1: UI and speech hygiene

**Files:**
- Modify: `handsoff.py:8300-8360` speech playback, `handsoff.py:8790-8840` bubble mouse handlers
- Test: `tests/test_audio.py`, `tests/test_lifecycle.py`

**Interfaces:**
- Preserve `_speak(text, gen, cancel)` and `BubbleWidget._hold_fired()` signatures.
- Add only a small menu-open guard and keep `_ANNOUNCE_LOCK` around playback, not synthesis.

- [ ] Add a regression test that starts the hold timer, opens the right-click menu, and verifies `_hold_fired()` does not call `begin_listening()` while the menu is open.
- [ ] Add a speech test proving synthesis may overlap but `play_wav()` calls are serialized and a later cancel prevents stale playback.
- [ ] Add `self._menu_open` state, stop the hold timer on right-click, and guard `_hold_fired()`.
- [ ] Move `tts_to_wav()` outside `_ANNOUNCE_LOCK`; retain the lock only around `play_wav()` and cancellation state.
- [ ] Run the focused tests, then the full suite.

### Task 2: Deployment provenance

**Files:**
- Modify: `install.sh:197-210` whisper download/manifest, `handsoff.py:455-610` doctor output
- Test: `tests/test_ops.py`

**Interfaces:**
- Preserve `deployment.json` compatibility with existing `files`, `whisper_model`, `whisper_revision`, and `python` fields.
- Add `whisper_sha256` only when the resolved model directory can be hashed.

- [ ] Add a helper that hashes the whisper model files deterministically and records the digest in `deployment.json`.
- [ ] Keep revision override support while recording the resolved revision returned by the downloader when available; never fail install solely because metadata is unavailable.
- [ ] Extend doctor output with whisper revision/digest and Python executable/version.
- [ ] Add manifest and doctor assertions to `tests/test_ops.py`.
- [ ] Run installer syntax checks and focused tests.

### Task 3: Coverage and documentation drift

**Files:**
- Modify: `.coveragerc`, `pytest.ini`, `requirements.txt`, `README.md`, `GAP_ANALYSIS.md`, `ACCEPTANCE.md`, `docs/superpowers/specs/*.md`
- Test: existing suite only; no production behavior change

- [ ] Keep the measured coverage comment at approximately 61% and omit only known generated bootstrap files.
- [ ] Update test counts to the current collected count after the final suite run.
- [ ] Record the current audit/deployment sign-off without deleting prior history.
- [ ] Mark shipped design specs as implemented and narrow the remaining D-Bus gap to unsupported layouts.
- [ ] Verify no stale `440+`, `550 tests`, or `approved — implement` strings remain.

### Task 4: Missing branch tests

**Files:**
- Test: `tests/test_ops.py`, `tests/test_policy.py`, `tests/test_notify_coalesce.py`, `tests/test_audio.py`, `tests/test_settings.py`

- [ ] Cover split-module CONFIRM floor, partial deployment source, streaming fallback terminator, preview unreadable/identical/truncated cases, notify back-to-back messages, and unverified voice URL opt-in.
- [ ] Cover PTT stale epoch, stop exception, submit exception, and bounded stop branches.
- [ ] Run coverage and ensure total remains at least 60%.

### Task 5: Verification and delivery

**Files:**
- No additional source files.

- [ ] Run `python3 -m py_compile` on every changed Python file.
- [ ] Run `python3 -m pytest tests/ -q` and record the exact count.
- [ ] Run `bash -n install.sh handsoff-restart`.
- [ ] Run `HANDSOFF_SKIP_SYSTEM_PKGS=1 HANDSOFF_NO_OLLAMA_SERVICE=1 bash install.sh`.
- [ ] Verify `--ptt status`, `--ptt doctor`, systemd active state, and deployment manifest in-sync.
- [ ] Inspect `git diff`, `git status`, and recent log; commit only intended files and push only after explicit approval.
