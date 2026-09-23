# Stop-attribution design (2026-09-23)

How the app answers "who stopped the unit?" — and the arc that question took
from mystery to resolution. Status: **implemented 2026-09-22/23, pinned by
test_lifecycle.py::TestStopProbe and test_ops.py::TestDoctor; the on-disk
format and rule are documented in specs/50-ops.md §6.**

## Problem

Three fault-injection turns in a row lost their answers to a systemd stop
that arrived seconds after the answer was submitted. The user journal logged
`Stopping handsoff voice assistant bubble…` — a stop *job*, so unit
management produced it — but never WHO invoked it: after the job completes,
the caller is gone, and no post-hoc sweep of journals or /proc can recover a
process that already exited. A diagnostic gap, not a code bug.

## The one instant the invoker is visible

A stop job blocks its caller until it completes. So the invoker is still
sitting in /proc at the moment the job *begins* — and `ExecStop=` runs at
exactly that instant. The probe (`handsoff-stop-probe`, wired as the unit's
`ExecStop=`) scans /proc for unit-management clients (`systemctl`/`busctl`/
`dbus-send`/`gdbus`) whose cmdline mentions handsoff, excludes python exes
as a second net (the bubble's own cmdline names handsoff.py — a stop must
never be attributed to the thing being stopped), and appends one JSONL line
to `stop-attribution.jsonl` (0600): callers with pid, exe, 120-char cmdline
head, and ≤6 hops of ancestry newest-first — the chain that names the agent
behind a `bash -c` wrapper. Always exits 0: a failing ExecStop marks the
whole unit failed, a worse outcome than a missed attribution. Two env seams
(`HANDSOFF_PROC_ROOT`, `HANDSOFF_STOP_LOG`) exist for the suite only.

Semantics that matter: ExecStop runs ONLY on stop jobs — a crash respawn
under `Restart=always` never invokes it, because "it died" and "someone
stopped it" are different events and the journal covers the first.
`callers:[]` is itself information: a session shutdown, a direct D-Bus call,
or a caller already gone when the probe looked.

## Reading it back: health, doctor, and the pattern rule

The reader (`handsoff.py::_stop_attribution_health`) adjudicates; doctor
formats — the standing division of labor. Health tails the ledger (last 3,
per-caller exe + cmdline head + chain, `total`); doctor renders one line
beside the systemd-unit section, reported even when empty ("none" is a
finding): named caller, invisible caller, no-ledger, each with its own
sentence.

**The ghost pattern** (`_ghost_pattern` in the reader): an unattributed stop
that FOLLOWS an attributed one within 60 min (last 50 rows, parsed
datetimes, negative gaps never count) is the restart-killer shape — someone
stopped the unit twice and hid the second time. It fires even when the
LATEST stop was attributed: a killer that alternates clean and invisible
stops must not slip out between two clean-looking lines. `seen: false` is
reported whenever the ledger exists — the cap-refusals idiom: "no pattern"
must be distinguishable from "never checked".

## The false positive, and what it taught

The rule's first real fire was benign: the 21:34:06 stop, 105 s after that
evening's deploy restart, was the machine powering down for the night
(logind: `poweroff requested from client PID 173724 ('systemctl')`; `last
-x`: down 21:34:16 → 09:48:22). A poweroff is the session's own sweep — the
biggest session shutdown there is — and the wording "not a session shutdown"
was wrong for it.

Fix: the probe detects the sweep at ExecStop time — primary signal is the
`invocation:exit.target` symlink under `$XDG_RUNTIME_DIR/systemd/units/`,
read bus-free because the session bus may already be dead mid-poweroff
(`systemctl is-active exit.target` is the belt) — and annotates the ledger
line `"shutdown":1` with an honest note. The pattern exempts annotated
stops, and doctor renders a shutdown ghost as `expected, not an anomaly`.
Pre-annotation rows default `shutdown:false`, so old ledgers behave exactly
as before (the rule cannot re-judge history the ledger does not carry).

## The autopsy, and the closure

The tripwire's founding mystery — the 18:26 / 18:30 / 18:41–43 stops of boot
−1, mid-session, machine up, no caller — resolved by journal autopsy:
FOUR events (18:43:39 was hiding inside the "41–43" span), each a RESTART
job (Stopped+Started same second, fresh startup banner), each carrying the
injection battle's own fingerprint — `handsfree=True` in the first two
banners, `handsfree=False` in the last two, the flag states the passes set
between their steps. Zero oomd lines, no logind sweep, clean exits. Verdict:
the fault-injection passes' own step-boundary restarts — the experiment
chasing itself, pre-tripwire, which is why no caller line exists. The open
list (`_OPEN_UNEXPLAINED_STOPS`) emptied with the verdicts kept as the
comment above it; the anchor (`_OPEN_UNEXPLAINED_STOPS_LATEST`) outlives the
list, so supersession — DERIVED, strictly: the recurrence must return (an
unattributed non-shutdown ghost) and then be caught (an attributed
non-shutdown catch after it) — still works against any future recurrence.
The naive version ("any attributed catch after the incidents") would have
closed the item on day one, because the installer's own restart qualifies.

## What the rehearsal proved, live

One `systemctl --user stop` grew the ledger and named the invoking shell
verbatim, ancestry to the Freebuff desktop; doctor rendered both caller
shapes on real data (attributed, and the unattributed poweroff ghost). Every
deploy since is itself an attributed restart — the installer's own restart
fires the probe, so each deployment demonstrates the mechanism for free.

## Where everything is pinned

* Probe behaviour (attribution, chain shape, bubble exclusion, shutdown
  annotation, wiring): `tests/test_lifecycle.py::TestStopProbe`, executed
  against a fake /proc tree.
* Reader shapes, pattern rule, exemption, doctor lines, supersession:
  `tests/test_ops.py::TestDoctor` — including the "60 min" wording verbatim,
  which is what keeps the spec's figures honest (the freshness guard pins
  structure, not sizes).
* On-disk format and rule prose: `specs/50-ops.md` §6.
* The ledger itself: `~/.local/state/handsoff/stop-attribution.jsonl`.

## Open edges

CLOSED (2026-09-23): `--ptt health` now carries the same `unexplained_stops`
block doctor_json has — both surfaces read `_unexplained_stops_health()`, and
the equality is pinned by `TestDoctor` (the parity contract IS the equality).
Still open: the first annotated real sweep (a night poweroff) is expected on
a future boot and will retire the last unannotated PATTERN from the ledger's
history.
