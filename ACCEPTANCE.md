# handsoff — hardware acceptance checklist

Run this on the real desktop (Arch/CachyOS + niri + Yeti mic + speakers)
after any deployment, and especially after installer, audio, or
desktop-action changes. Every line is meant to be executed, not read.
Nothing here can be fully automated — that is what `tests/` is for.

Legend: ☐ pending · ✅ pass · ❌ fail (note why below the line)
Verified-by codes: [auto] scriptable end-to-end on this machine ·
[human] needs ears/eyes/hands at the desk.

## 1. Deployment trust (P0)

- ✅ [auto] `./install.sh` completes; `~/.config/handsoff/deployment.json`
  exists with seven file rows (`handsoff.py`, `handsoff-settings.py`,
  `handsoff-restart`, `settings_schema.py`, `hardware.py`,
  `core/__init__.py`, `core/settings.py`), all `source==installed: True`.
  Re-verified 2026-09-09
  after the ff2f158 run (per-package pacman probe + unit-name loop fired live).
- ✅ [auto] `--ptt doctor` prints
  `deployment: in-sync — installed copy matches the checkout` (2026-09-09)
- ✅ [auto] `--ptt health` includes a `"deployment"` section with
  `status: in-sync` (2026-09-09)
- ✅ [auto] Settings health bar shows `deploy: ok` — verified at the data
  layer: the live bubble health snapshot fed through the Settings app's own
  `_fmt_health()` renders
  `mic: … · brain: ok gemma4:latest · tts/stt: ok · deploy: ok`
  (the string the bar displays verbatim — `gemma4:latest` was the
  configured value on the test box; the shipped default is `qwen3:8b`; pixel-OCR of the Qt window was
  attempted but multi-output screenshots made OCR unreliable)
- ✅ [auto] `systemctl --user status handsoff` → active; unit has
  `Restart=always` (2026-09-09)

## 2. Startup, restart, crash recovery, shutdown (P0)

- ✅ [auto] Fresh start: stop → `systemctl --user start handsoff` → unit
  active, `--ptt status` answers `state=idle handsfree=off model=gemma4:latest`
  (`gemma4:latest` was the configured value on the test box; default `qwen3:8b`)
  (2026-09-09)
- ✅ [auto] Restart: `~/.local/bin/handsoff-restart` → exactly one bubble
  after the swap. **Found and fixed a real race today**: against a
  systemd-owned bubble the script's manual TERM/KILL pass triggered
  `Restart=always` auto-spawn before its own `systemctl restart` (transient
  double bubble, journal 16:39:13–16:39:15). The script now goes systemd-first
  and only kills manually when no unit is managing the bubble. Retest: PASS.
- ✅ [auto] Crash recovery: SEGV on the main pid → systemd restarts within
  ~5 s; doctor mentions the crash log; previous run (15:08–15:09) additionally
  showed the spoken short report and `crash.log` truncated-not-unlinked,
  same inode, mode 0600.
- ✅ [auto] Crash-loop guard: 5+ rapid SEGVs → unit entered `failed` with
  journal `Start request repeated too quickly`; `systemctl --user reset-failed
  handsoff` + start recovered it to active (2026-09-09)
- ☐ [human] Shutdown: end the graphical session → bubble exits cleanly
  (`PartOf=graphical-session.target`), no zombie after re-login. Not
  automatable from inside the session (it *is* the session).

## 3. Yeti capture (P0/P1)

- ☐ [human+auto] Settings → Voice: the Yeti appears in *Input device* and can
  be selected. Enumeration verified 2026-09-09: `Blue Microphones: USB Audio
  (hw:4,0)` is visible to sounddevice (Blue = Yeti vendor), **but the
  currently configured input is the Logitech StreamCam** — the Yeti needs to
  be actually selected in Settings → Voice.
- ☐ [human] Live test: level meter follows the room; speech passes threshold
- ☐ [human] Push-to-talk round trip: hold → speak → release; transcript correct
- ☐ [human] Hands-free wake: say the wake name; engagement window opens
- ☐ [human] Stop echo rejection: while it speaks a long reply, say "stop" →
  quiet, no self-transcription
- ☐ [human] Mic self-heal: unplug the Yeti hands-free → degraded state
  journaled/spoken, recovers on replug (or capped retries)

## 4. TTS (P0/P1)

- ☐ [human, synthesis verified] Replies spoken with the piper voice; no echo
  into the transcript. Engine verified 2026-09-09: `tts_to_wav` synthesized a
  real 1.52 s / 22050 Hz waveform ("acceptance check complete"). Audible
  playback + echo-rejection need ears.
- ☐ [human] Barge-in: click the bubble while it speaks → playback stops
  instantly (needs the bubble audibly speaking; supervised test)
- ☐ [human] Volume/rate sliders in Settings take effect after Apply

## 5. ydotool / typing (P1)

One-pass re-check: `python ~/.local/bin/handsoff.py --ptt selftest` runs the
whole section (daemon, focus plumbing, terminal refusal, type_text landing,
clipboard round-trip) against self-launched scratch windows and restores the
clipboard. First live run 2026-09-09: `verdict: PASS (5/5 checks)`.

- ✅ [auto] ydotoold running (Arch user unit: `ydotool.service`; other
  distros: `ydotoold.service`). Verified 2026-09-09: daemon reachable at
  `/run/user/1000/.ydotool_socket`, doctor reports
  `ydotool: ok (daemon reachable …)`. The installer now enables the right
  unit (`ydotool.service` first) — confirmed in the ff2f158 install run.
- ✅ [auto] Type into a focused editor → text lands in the app (2026-09-09):
  `type_text "handsoff ydotool e2e 2026-09-09"` into gnome-text-editor →
  "typed 31 chars into org.gnome.TextEditor"; full-screen OCR read the exact
  string back off the screen.
- ✅ [auto] Typing into a *focused terminal* is refused (2026-09-09): foot
  focused → `type_text` AND `press_keys` both returned
  "REFUSED: the focused window is a terminal (foot)"; nothing was executed.
  Fail-closed on unknown focus is unit-tested in tests/test_desktop.py.
- ✅ [auto] `press_keys "ctrl+c"` copies from the focused app (2026-09-09):
  ctrl+a then ctrl+c in gnome-text-editor → `wl-paste` returned the exact
  file content byte-for-byte.

## 6. niri IPC & desktop actions (P1)

- ✅ [auto] `run_command "niri msg --json windows"` works (2026-09-09)
- ✅ [auto] `niri_capabilities` lists this niri version's actions, not a
  hardcoded set (2026-09-09)
- ✅ [auto] open_app launches, identifies and focuses: `open_app foot` →
  window appeared, `is_focused: true` (2026-09-09)
- ✅ [auto] Move to workspace: `workspace move "texteditor to 2"` →
  `moved the window to workspace 2`; window verified on workspace idx 2.
  (Note: `workspace_id` in niri's JSON is a global id, NOT the user-facing
  index — verify against `niri msg --json workspaces` idx, as this run did.)
- ✅ [auto] Polite close confirmed gone: `close_window "texteditor"` →
  "closed 1 window(s): … (confirmed gone)", window absent afterwards
- ✅ [auto] scroll scrolls (`scrolled down 2 notch(es)` with the operator
  permission enabled the legitimate way — wheel via ydotool) and `wait`
  settles (2026-09-09)
- ☐ [human, supervised] Operator element clicks: `screen_elements` →
  `click_element` clicks the right element; pointer scale correct on a
  fractional-scale output. Left supervised: a mis-aimed click on the real
  desktop could hit unrelated windows.

## 7. systemd / niri autostart ownership (P0)

- ✅ [auto] Exactly one autostart owner: unit `enabled`, and no handsoff
  `spawn-at-startup` line in `~/.config/niri/config.kdl` (2026-09-09)
- ☐ [human] After a full reboot the bubble is up exactly once (needs a reboot)

## 8. Session teardown

- ☐ [human] After logout/login, reminders and hands-free state match
  expectations
- ✅ [auto] `~/.local/state/handsoff/decisions.jsonl` exists (mode 0600,
  909 rows) and every row carries id, tool, target, decision, result —
  including all of this session's tool calls (2026-09-09)

## Sign-off

| Date | Tester | Commit / deployment sha | Result |
|---|---|---|---|
| 2026-09-09 | Buffy (automated run) | ff2f158 + uncommitted handsoff-restart race fix | All automatable items PASS; human items: Yeti selection, live audio, barge-in, sliders, session teardown, reboot |
| 2026-09-10 | Buffy (automated run) | 38a0764 + PTT/notify/split set | All automatable items PASS (live doctor in-sync); human items unchanged |
| 2026-09-10 | Fixer (repository validation) | uncommitted hygiene hardening | 583 tests passed; deployment and human-only checks not run |
| 2026-09-10 | Buffy (audit closure) | 96bac4b + uncommitted trust set (remote-brain formalization, staged release + rollback, policy rows, CI SHA pins) | 637 tests green, coverage 63.2% ≥ 60, py_compile + bash -n clean; deployment not re-run — installed-drift expected until next `./install.sh`; human-only items unchanged |
| 2026-09-10 | Buffy (live redeploy) | 52f78b7 (running == checkout == installed, sha256 efe870cb…) | Staged-release path ran live: compile+import gates passed, prev release saved to releases/prev, service restarted onto new code; `--ptt doctor` in-sync; `--ptt health` deployment three-way match; loopback brain correctly emits no privacy line; human-only items unchanged |
| 2026-09-10 | Buffy (live redeploy) | 52f78b7 + uncommitted coverage + History-tab set (659 tests green, coverage 71.52% ≥ 70) | Staged-release path ran live again (step 4/8 compile+import gates, prev kept at releases/prev); `--ptt doctor` in-sync; three-way SHA match incl. handsoff-settings.py (c2c11b7c…); installed copy verified to carry the new History tab (Conversation / Durable facts / Decision log); human-only items unchanged |
