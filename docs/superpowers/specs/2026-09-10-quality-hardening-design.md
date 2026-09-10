# Quality Hardening Design

**Status:** approved roadmap; implementation proceeds in gated phases.

## Goal

Improve the assistant's safety, reliability, and maintainability without a
large rewrite or new dependencies.

## Scope and order

Work is deliberately staged:

1. Safety boundaries: confirmation turn binding and purge backup safety.
2. Lifecycle and speech: shutdown ownership, announcement state recovery,
   per-turn Ollama state, and bounded pipeline work.
3. Settings and deployment: one persistence boundary, reproducible artifacts,
   private logs, and transactional installation.
4. Incremental extraction: audio, brain/turn execution, then tools/policy.

Each phase must leave the full test suite green before the next phase starts.
No phase introduces asyncio, an actor framework, a state-machine dependency, or
a generic LLM abstraction.

## Phase 1: safety boundaries

### Confirmation

Every pending confirmation records the originating user-turn identifier and
the requested action. When a tool returns `CONFIRM`, the current model/tool
loop stops immediately. `confirm_action()` is accepted only from a later user
turn that explicitly corresponds to the pending request and before its timeout.

The existing policy levels remain intact: DENY still rejects, ALLOW still
executes, and CONFIRM still presents a human gate. Confirmation through a
model tool call is retained for compatibility, but the model cannot satisfy
the gate within the same turn that created it.

Required tests cover same-turn rejection, next-turn acceptance, timeout,
replacement by a newer pending action, and DENY precedence.

### Purge

`install.sh --uninstall --purge` creates a unique private archive with
`umask 077`, verifies the archive command succeeded and the archive is
readable, then deletes configuration/state. Any backup or verification failure
aborts before deletion and leaves the original data untouched. Existing
non-purge uninstall behavior is unchanged.

Required tests cover tar failure, missing archive, successful backup, private
archive mode, and no deletion on failure.

## Phase 2: lifecycle and speech

### Assistant lifecycle

`Assistant` owns one shutdown event and a registry of long-lived workers.
Shutdown marks the assistant closed, cancels current work, stops external
inputs, wakes queue consumers, and performs bounded joins. Workers reject new
work after closure. Late Qt signals are ignored through the existing generation
guard and closed-state check.

Shutdown is bounded and observable: it completes within a documented limit and
logs workers that did not terminate. It must not wait indefinitely on native
audio, network, or model operations.

### Speech state

Every announcement owns its complete lifecycle: request, speak, cancellation,
and conditional return to IDLE. A stale announcement cannot overwrite a newer
turn's state. Piper synthesis and playback use one bounded speech scheduler;
cancelled announcements do not accumulate unbounded worker threads.

### LLM turns and backpressure

Each Ollama turn has local generation, cancellation, sentence queue, result,
and completion state. No canceled stream may write to a later turn's shared
result slot. The pipeline queue has explicit bounded behavior; the recommended
policy is max size one with latest utterance winning and dropped work marked
complete.

Required tests cover canceled old streams, queue pressure, announcement state
recovery, shutdown timing, late signals, and speech cancellation.

## Phase 3: settings and deployment

`core.settings` becomes the authoritative file/lock/persistence boundary.
Runtime single-key writes and GUI full saves both perform their merge under the
same lock. GUI saves detect a changed file and merge or reject rather than
silently overwriting a newer runtime update. Restart-required settings remain
explicitly restart-required.

Runtime logs default to metadata rather than raw transcripts, spoken text,
memory facts, commands, or sensitive tool payloads. Log files and rotations
are owner-only, with a documented retention/purge path.

Deployment records immutable model/dependency provenance where available and
uses a staged release directory. A release is compiled/import-smoke-tested
before an atomic switch; the previous known-good release remains available for
rollback. Installer rehearsal tests run without modifying the host.

Remote Ollama use is explicit opt-in, requires an appropriate transport
policy, and warns that history, screenshots, schemas, and user data leave the
machine. High-impact tools retain explicit policy/confirmation gates.

## Phase 4: incremental extraction

Extraction follows proven lifecycle boundaries:

1. `core/audio.py`: Recorder, listener/VAD, Whisper, Piper, and speech
   scheduling.
2. `core/brain.py`: Ollama client, turn ownership, history/memory orchestration.
3. `core/tools.py`: ToolBelt, policy, command validation, and confirmation.

Compatibility imports remain in `handsoff.py` for one release per boundary.
Each extraction is mechanical first; behavior changes are separate commits.

## Quality gates

Every phase requires:

- focused red-first regression tests;
- `python3 -m py_compile` for changed Python files;
- `python3 -m pytest tests/ -q` with no failures or unhandled thread warnings;
- deployment/doctor checks where installer/runtime files changed;
- explicit review of privacy, permissions, and cancellation behavior.

Human acceptance remains required for real microphone capture, TTS/barge-in,
Yeti unplug/replug, reboot/session teardown, and desktop operator behavior.

## Explicit non-goals

- No full rewrite of the 9k-line runtime in one change.
- No asyncio migration.
- No new framework or dependency for state management.
- No plugin architecture for individual tools.
- No feature expansion until safety and lifecycle gates pass.
