# World-events warnings design (2026-09-09)

Morning-briefing world headlines + opt-in proactive severe-event warnings.
Status: **approved — implement as below.**

## Problem

The briefing covers weather, calendar and mic health but never the world:
major news breaks and the assistant stays silent, and there is no channel
for "tell me immediately if something terrible happens". Each consumer
(briefing, proactive check, model tool) fetching its own headlines would
triple DDG traffic and triple the failure modes.

## Approaches considered

1. **Shared helper (recommended).** One `_world_events(kind, limit)` next
   to `_ddg_lite` owns fetching, capping and severity tagging; briefing,
   `_world_tick` and the `world_events` tool only format. One fetch path,
   one severity list, one dedup store.
2. **Separate fetchers per consumer.** Duplicates queries, caps and error
   handling three times; drift guaranteed. Rejected.
3. **Model-only (tool, no briefing/proactive).** No new fetching code, but
   leaves the actual gap (unprompted awareness) unfilled. Rejected.

Recommendation is (1).

## Fetcher

`_world_events(kind="all", limit=5)` in handsoff.py next to `_ddg_lite`.
Fixed queries only — the model never chooses them:

- kind `news`: `"breaking world news"`, plus a home-place variant
  (`"breaking news {country}"` when home_place contains a comma,
  else `"breaking news {home_place}"` when set).
- kind `weather`: severe-weather signal only — geocode home_place, read
  open-meteo `current` weather_code + wind_speed_10m; an event exists iff
  code ∈ {95, 96, 99, 65, 75, 82} or wind > 75 km/h (same WMO table the
  `get_weather` tool reads). No home_place → no weather events.
- kind `all`: news then weather, de-duplicated by normalized title key,
  total capped at 5.

Reuses `_ddg_lite` / `_http_get` (urllib, 10 s timeout). Never raises:
returns `(events, degraded)`; any fetch failure sets `degraded=True` and
keeps whatever partial results exist. Events are dicts
`{title, snippet, urgent, source, key}` with HTML already stripped by
`_ddg_lite`.

## Severity

Deterministic, no model judgment. Urgent (warning-worthy,
proactive-eligible) iff a whole-word match on earthquake, tsunami,
hurricane, tornado, flood, wildfire, volcano, terror, missile, airstrike,
nuclear, or a phrase match on "severe thunderstorm", "tornado warning",
"flood warning", "severe heat warning", "heat warning", "severe weather
warning" — or a severe WMO code / wind from the weather path. Everything
else is digest filler (top 3–4 titles, one line each) for the briefing and
the tool.

## Dedup

`STATE_DIR/world-events-seen.json`: `{norm_key: epoch}`, cap ~64 (drop
oldest), TTL 36 h (inside the required 24–48 h window), atomic write via
`_atomic_private_write`, lock-guarded read-modify-write. Corrupt file →
treat as empty (never crash the briefing).

Mark semantics: the **briefing marks what it speaks**, the **proactive
tick checks-then-marks** what it announces, and the **tool NEVER marks**
(reading headlines must not consume warnings).

## Per-mode wiring

- **Briefing** (`_maybe_briefing_prefix`): `World:` section after the
  weather/calendar block. Inherits the once-daily date, skip-prefixes and
  `briefing` flag. News needs no home_place; the weather part keeps its
  home_place requirement; a failed weather lookup still skips silently
  (offline → silent skip).
- **Proactive** (`_world_tick`, mirrors `_resource_tick`): opt-in
  `world_warnings` flag (default False), called from the existing
  `_health_tick` (no new thread). Per-event seen-store check + one global
  `~world_cooldown_min` cooldown (default 60). Popup (`notify`) always,
  spoken (`_announce_now`) unless `state == SPEAKING`. No quiet-hours v1.
- **Tool** `world_events(count=4)`, `@tool gates="web_access"` (reuses the
  existing permission, no new key). `count` clamped to 1–5. Read-only:
  never touches the seen store.

## Flags

`world_warnings` (bool, default False), `world_cooldown_min` (float,
default 60, numeric-validated 5–1440). Settings UI: checkbox row next to
the briefing checkbox, no new tab; `web_access` description extended to
mention world warnings.

## Error handling

Offline / bad HTML / geocode miss / corrupt seen store: degrade to fewer
or no headlines, never raise into the briefing, the tick, or the tool.
The tool reports `ERROR: world news unavailable` only when degraded AND
empty.

## Tests (`tests/test_world_events.py`, mocked `_http_get`, no live network)

Fetcher cap / degraded-on-failure / HTML-strip; severity urgent vs
filler; dedup suppress + mark semantics incl. tool-never-marks; briefing
once-daily + skip-prefixes + `World:` section + offline-weather-only;
proactive global cooldown (no refetch) + SPEAKING suppresses spoken copy;
tool REFUSED when `web_access` is False + count clamp.

## Self-review

- No placeholders: every function, flag, default, TTL and cap above
  matches the implementation (`_world_events`, `_world_is_urgent`,
  `_world_norm_key`, `_load_world_seen`, `_world_seen`,
  `_world_mark_seen`, `_severe_weather_events`, `_world_tick`,
  `world_events`, `WORLD_EVENTS_MAX=64`, `WORLD_EVENTS_TTL_S=36h`,
  `world_cooldown_min` default 60).
- No contradiction: "tool never marks" holds (no store call on the tool
  path); "briefing inherits skip-prefixes" holds (World fetch happens
  after the skip check); "no new thread" holds (`_health_tick` call site);
  "no new permission key" holds (`gates="web_access"`).
- Out of scope (not touched): quiet hours, per-event cooldowns, country
  resolution beyond the comma rule. No commit (orchestrator owns git).
