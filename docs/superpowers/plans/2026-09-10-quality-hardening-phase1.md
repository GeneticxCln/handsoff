# Quality Hardening Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** Close the two critical safety gaps: model confirmation must require a later user turn, and purge must never delete state when its backup fails.

**Architecture:** Keep existing policy/tool APIs and installer flags. Add a turn-bound confirmation token to the existing pending-confirm state, and make purge a verified backup transaction before deletion. No new dependencies or broad refactor.

**Tech Stack:** Python 3.12–3.14, pytest, Bash, tar, existing Qt/Ollama runtime.

## Global Constraints

- Preserve ALLOW/DENY/CONFIRM policy semantics for existing callers.
- Preserve non-purge uninstall behavior.
- No new dependencies.
- Never delete configuration or state after a failed or unverifiable backup.
- Run focused red-first tests, full `python3 -m pytest tests/ -q`, Python compile checks, and `bash -n install.sh`.

---

### Task 1: Turn-bound confirmation

**Files:**
- Modify: `handsoff.py` confirmation state, `ToolBelt.execute`, `confirm_action`, `_brain_turn`
- Test: `tests/test_policy.py`, `tests/test_lifecycle.py`

**Interfaces:**
- Existing `confirm_action()` tool signature remains unchanged for model compatibility.
- Add an internal user-turn/generation marker to pending confirmation; do not expose secrets or prompt content in logs.

- [ ] Add failing tests proving a confirmation created in turn N cannot be accepted by `confirm_action` in turn N, while a matching explicit request in turn N+1 can accept it.
- [ ] Add tests for timeout, replacement by a newer pending action, and DENY precedence.
- [ ] Change `_brain_turn` to stop the current tool loop when execution returns a confirmation offer; persist the pending action with the current turn marker.
- [ ] Change `confirm_action` to require a later matching user-turn marker and clear the pending action atomically on accept/reject.
- [ ] Run focused policy/lifecycle tests and the full suite.

### Task 2: Fail-safe purge

**Files:**
- Modify: `install.sh` purge branch
- Test: `tests/test_ops.py`

**Interfaces:**
- Preserve `install.sh --uninstall --purge` CLI behavior and existing backup naming intent.
- Backup archive must be private, unique, readable after creation, and verified before deletion.

- [ ] Add failing shell-level tests or deterministic helper tests for tar failure and missing archive; assert config/state remain present.
- [ ] Create a unique archive under `umask 077`, capture tar status, require the archive to exist and be readable, and set mode `0600`.
- [ ] Delete config/state only after all backup checks pass; report the archive path on success and the reason on abort.
- [ ] Preserve ordinary uninstall without `--purge`.
- [ ] Run `bash -n install.sh`, focused ops tests, and the full suite.

### Task 3: Integration gate

**Files:**
- No new production files.

- [ ] Review combined diff for policy bypasses, accidental data deletion, and test pollution.
- [ ] Compile changed Python files.
- [ ] Run the complete suite with no failures or unhandled thread warnings.
- [ ] Run installer help and a dry/rehearsal purge failure test without modifying the real user state.
- [ ] Update the quality-hardening design status only after verification.
- [ ] Deploy only after all gates pass; commit and push only when explicitly requested.

## Later phases

After Phase 1 passes, execute separate plans for lifecycle/speech, settings/deployment, and incremental extraction. Do not combine those broad changes with this safety patch.
