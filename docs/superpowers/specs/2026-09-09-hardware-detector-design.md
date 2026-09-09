# Hardware detector design (2026-09-09)

How `run_doctor` / `doctor_json` learn about the machine without rotting into
a second, parallel probing stack. Status: **approved — implement as below.**

## Problem

`run_doctor()` probes Ollama, the mic, niri, ydotool, systemd and voices
inline. Every new consumer (system prompt context, `--ptt doctor`, control
socket JSON) re-probes or copy-pastes, so slow calls (`nvidia-smi`, Ollama
with no server) run once per consumer and failures surface differently in
each place.

## Approaches considered

1. **On-demand snapshot + TTL-lite (recommended).** One `hardware.snapshot()`
   call returns a plain JSON dict for every section; slow sections are cached
   for a few seconds in a caller-owned dict. No threads, no daemons, no
   import-time cost. A `--ptt doctor` run probes each slow thing at most once;
   per-turn prompt use reuses the cache.
2. **Consolidate into handsoff.py helpers.** Keeps one file but preserves the
   real problem: probing stays tangled with formatting, untestable without
   Qt/audio, and every consumer still pays full probe cost.
3. **Background poller thread.** Fresh data always, but adds lifecycle,
   locking and shutdown ordering to a codebase that just removed a thread
   class of bugs. Rejected: the data goes stale in seconds anyway and doctor
   output is read rarely.

Recommendation is (1): `hardware.py` owns probing, `handsoff.py` owns
formatting. Consumers: `run_doctor` (text, byte-stable), `doctor_json`
(+`hardware` / `prompt_context` keys), the 5-line system-prompt builder.

## Module boundary (`hardware.py`, ~300 lines)

- **stdlib + pathlib only.** No Qt, sounddevice, whisper, piper imports —
  safe to import anywhere, including `--preflight` on a bare checkout.
- **Never imports `SETTINGS`.** All configuration arrives via `ctx` (plain
  dict of strings); missing keys fall back to env / `$HOME` conventions.
- **Never raises.** Every section is `try/except` guarded; failures become
  degraded dicts (`{"ok": False, "error": ...}`). `snapshot()` itself has an
  outer guard returning a minimal `{"ts": ..., <section>: degraded}` dict.

## Function list

- `snapshot(ctx, probers=None, ttl_cache=None, *, force=False) -> dict`
  Keys: `ts, cpu, ram, gpu, audio, display, mounts, ollama, models, stt_tts,
  systemd, compositor, ydotool` (13 keys, always all present).
- `prompt_context(snap, max_chars=600) -> str`
  At most 5 lines: desktop/niri+Wayland, default mic, Ollama model,
  whisper/piper voices, control-socket mounts. Truncated to `max_chars`.
- `main(argv) -> int`
  `--preflight`: prints `snapshot()` as JSON, always exit 0 (degraded data
  still prints; only `--help` short-circuits).

## TTL / failure policy

Per-section TTL (seconds): fast sections `cpu, ram, display, mounts,
models, stt_tts` are cheap syscalls — **TTL 0, always fresh, zero
subprocess cost**. Slow sections: `audio: 10, gpu: 15, systemd: 30,
compositor: 30, ydotool: 30, ollama: 60`.

`ttl_cache` is caller-owned: `{"at": {section: monotonic}, "data":
{section: section-dict}}`. A section is re-probed when missing, expired, or
`force=True`. `run_doctor` passes a **fresh `{}`** (byte-stable `--ptt
doctor` output that honors test mocks); the prompt builder shares a module
`_DOCTOR_TTL` dict.

Slow probers are **injected** (`probers` dict) so tests never touch real
hardware: `query_devices, ollama_tags, nvidia_smi, niri_windows,
ydotool_which, ydotool_socket, socket_connectable`. Defaults: lazy
`sounddevice` import, `urllib` GET `/api/tags` (timeout 2.5s),
`nvidia-smi -L` (timeout 2s), `niri msg --json windows` (timeout 3s),
`shutil.which`, socket probe.

## Consumers (handsoff.py wiring — run_doctor/doctor_json only)

- `_doctor_ctx()` builds `ctx` from module globals (`OLLAMA_BASE`,
  `SETTINGS`, dir constants). `_doctor_probers()` wraps existing seams
  (`ToolBelt._niri_msg`, `_ydotool_socket`, `_socket_connectable`,
  `shutil.which`) as lambdas evaluated at call time so `monkeypatch` keeps
  working.
- `run_doctor` formats the snapshot into the **existing line strings**
  (brain/mic/niri/ydotool); deployment, voices-loaded, systemd, restart,
  crash-log and control-socket lines stay inline. No `hardware` module →
  legacy inline probes (same strings).
- `doctor_json` adds `hardware` (full snapshot) and `prompt_context`
  (5-line string); existing keys unchanged.

## Error handling

Section failure → degraded dict, never an exception, never a missing key.
Distinctions the doctor text needs are preserved: niri `refused`
(nonzero exit) vs `error` (exception); ydotool `installed: False` (healthy
absent) vs unreachable; GPU absent (`present: False`, healthy) vs probe
error; empty voice dir (`onnx: []` + note) vs unreadable dir (degraded).

## Tests (`tests/test_hardware.py`, ~15 cases, no real hardware)

Degraded-on-failure per section (all probers raising); mic input filter /
default-device match / empty-device list; Ollama down + timeout; stt_tts
empty dir + onnx-without-json; mounts missing socket; TTL cache-hit makes
zero prober calls + `force=True` re-probes; prompt builder token cap +
verbatim paths; all-probers-explode never raises with minimal ctx.

## Self-review

- No placeholders: all function names, TTL values, timeouts and key names
  above match `hardware.py` exactly (`TTL`, `snapshot`, `prompt_context`,
  `main --preflight`; 13 snapshot keys as listed).
- No contradiction: "never raises" holds including `main` (outer guard,
  exit 0); "TTL 0 = zero subprocess" holds because fast sections use only
  `os`/`Path` reads; `run_doctor` freshness vs prompt-builder sharing is
  split by separate `ttl_cache` dicts on purpose (correctness for
  `--ptt doctor`, savings per turn).
- Out of scope (not touched): `handsoff-settings.py`, `install.sh`,
  deployment of `hardware.py` to `~/.local/bin` (installer change needs its
  own review); no commit (orchestrator owns git).
