# Live hardware watch design (2026-09-09)

Per-tick hardware awareness so the assistant notices the machine changing
under it. Status: **implemented 2026-09-09, pinned by test_hardware/watch/world_events.**

## Problem

The assistant is blind between turns: a mic unplugged mid-handsfree, a
disk filling during a long job, Ollama dying, or VRAM (with
`resource_alerts` off) crossing go unnoticed until the user complains.
The doctor only reports when asked.

## Approaches considered

1. **Tick-enrichment (recommended).** `Assistant._hardware_tick()` runs
   inside the existing ~10 s `_health_tick`, next to `_resource_tick` /
   `_world_tick`, sampling only cheap signals and reading TTL-gated slow
   sections from the shared cache without forcing them. No new threads,
   no new dependencies, no new timing source.
2. **Sampler thread.** Own cadence and lifecycle, own shutdown ordering —
   a new thread class of bugs for data that is consumed at turn cadence
   anyway. Rejected.
3. **Alerts-only (urgent popups, no notes).** Cheaper, but the far more
   common case is a small contextual change ("mic moved to Yeti") that
   belongs in the next turn, not in a popup. Rejected as the whole
   design; urgent popups remain one of the two channels.

Recommendation is (1).

## Per-tick sampling budget

Cheap every tick (stdlib/syscalls only, zero subprocess): `os.getloadavg`,
`/proc/meminfo` %, `shutil.disk_usage` via new `hardware.disk_free()`,
`shutil.which("nvidia-smi")` presence, and `mic_snapshot()` (listener
fields only — the state machine is consumed, never duplicated).
Slow sections (ollama/fastfetch/niri/socket) are **peeked from the shared
`_DOCTOR_TTL` cache only when fresh, never forced** by the tick.
At most one slow subprocess per tick: the nvidia-smi util query, and only
on every 2nd tick (tick-parity gate) with a 2 s timeout.

## Change detection

All comparisons against `self._hardware_last` (plain dict, in-memory
only): mic `(state, device)` tuple, `disk_low` latch, `gpu_present` flag,
`vram_alerted` latch, `ollama_miss` counter + `ollama_down` latch, tick
parity counter. RAM/VRAM latch semantics reuse `_resource_tick`'s:
crossing announces once, silence while still high, re-arm below
threshold — and the VRAM branch is skipped outright when
`resource_alerts` is on (no double-announce). Disk identical latch on
`hardware_disk_gb`. Ollama needs 2 consecutive fresh-cache failures (a
single blip stays silent); recovery clears the latch and posts a note.

## AI channels

- **Non-urgent** (mic moved/changed, GPU flip, Ollama back, disk
  re-armed): short-lived `self._hardware_note` (1–2 lines, ≤200 chars).
  `_conversation_for` appends it as the last system message (same
  prefix-cache rationale as the memory block) and clears it —
  consumed-once, so the system prefix stays byte-identical otherwise.
- **Urgent only** (mic unplugged mid-handsfree, disk critically low,
  Ollama down, VRAM crossing with `resource_alerts` off): `notify()`
  popup (coalesced) + `_announce_now` unless `state == SPEAKING`, under
  one global `hardware_cooldown_min` cooldown. No quiet-hours v1.

## Flags

`hardware_watch` (bool, default False — off the tick returns in <1 ms
and the prompt gains nothing), `hardware_cooldown_min` (float, default
60, numeric-validated 5–1440), `hardware_disk_gb` (float, default 5,
numeric-validated 0.5–1000). Settings UI: checkbox row near
briefing/world-warnings plus cooldown/disk spins; no new tab.

## Never-raise

`_hardware_tick` wraps its whole body (every probe is best-effort with a
degraded fallback) and the `_health_tick` call site isolates it
try/except like its neighbors. A failed GPU query, unreadable meminfo,
or a poisoned TTL cache degrades to fewer signals, never an exception —
including when probers themselves raise (falls back to last-good cache
entries where present).

## Tests (`tests/test_hardware_watch.py`, mocked probers, no hardware)

Fast tick makes zero subprocess calls; mic change posts a note exactly
once and `_conversation_for` consumes it exactly once; crossings
announce once / stay silent while high / re-announce after re-arm;
global cooldown suppresses a second urgent; a single Ollama blip stays
silent; an exploding prober layer never raises and last-good cache data
is honored; watch off returns fast with no prompt gain.

## Self-review

- No placeholders: every function, flag, default, TTL and threshold
  above matches the implementation (`hardware.disk_free`,
  `Assistant._hardware_tick/_hardware_note/_hardware_last/
  _hardware_last_urgent/_hardware_tick_n`, `hardware_watch`,
  `hardware_cooldown_min` default 60, `hardware_disk_gb` default 5).
- No contradiction: "no new threads" holds (health-tick call site);
  "at most one slow subprocess" holds (parity-gated util query is the
  only in-tick subprocess); "never forced" holds (slow sections are
  cache peeks); "consumed-once" holds (note cleared on attach).
- Out of scope (not touched): quiet hours, per-event cooldowns, trend
  graphs. No commit (orchestrator owns git).
