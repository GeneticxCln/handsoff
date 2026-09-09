# handsoff — hardware acceptance checklist

Run this on the real desktop (Arch/CachyOS + niri + Yeti mic + speakers)
after any deployment, and especially after installer, audio, or
desktop-action changes. Every line is meant to be executed, not read.
Nothing here can be fully automated — that is what `tests/` is for.

Legend: ☐ pending · ✅ pass · ❌ fail (note why below the line)

## 1. Deployment trust (P0)

- ☐ `./install.sh` completes; step 4 prints the manifest path and step 8
  succeeds (`~/.config/handsoff/deployment.json` exists with three file rows)
- ☐ `python ~/.local/bin/handsoff.py --ptt doctor` prints
  `deployment: in-sync — installed copy matches the checkout`
- ☐ `python ~/.local/bin/handsoff.py --ptt health` includes a
  `"deployment"` section whose `status` is `in-sync`
- ☐ Settings app health bar shows `deploy: ok`
- ☐ `systemctl --user status handsoff` → active; unit has `Restart=always`

## 2. Startup, restart, crash recovery, shutdown (P0)

- ☐ Fresh start: `systemctl --user start handsoff` → bubble appears, control
  socket answers `--ptt status`
- ☐ Restart: `~/.local/bin/handsoff-restart` → one bubble, no "already
  running" (watch `journalctl --user -u handsoff -f` during the swap)
- ☐ Crash recovery: `kill -SEGV $(pgrep -f 'bin/handsoff.py')` → systemd
  restarts it within ~5 s; `--ptt doctor` mentions the crash log; the bubble
  speaks the short crash explanation
- ☐ Crash-loop guard: simulate 5 rapid failures → the unit stops (start-limit)
  instead of spinning; `systemctl --user reset-failed handsoff` clears it
- ☐ Shutdown: end the graphical session → the bubble exits cleanly
  (`PartOf=graphical-session.target`), no zombie left after re-login

## 3. Yeti capture (P0/P1)

- ☐ Settings → Voice: the Yeti appears in *Input device* and can be selected
- ☐ Live test: level meter follows the room; speech passes the threshold
- ☐ Push-to-talk round trip: hold → speak → release; transcript is correct
- ☐ Hands-free wake: say the wake name; engagement window opens
- ☐ Stop echo rejection: while it speaks a long reply, say "stop" → it goes
  quiet and does not transcribe its own voice
- ☐ Mic self-heal: unplug the Yeti while hands-free → degraded state is
  journaled, spoken, and recovers when replugged (or after the capped retries)

## 4. TTS (P0/P1)

- ☐ Replies are spoken with the piper voice; no echo into the transcript
- ☐ Barge-in: click the bubble while it speaks → playback stops instantly
- ☐ Volume/rate sliders in Settings take effect after Apply

## 5. ydotool / typing (P1)

- ✅ ydotoold is running (Arch user unit: `systemctl --user status ydotool.service`;
   other distros: `ydotoold.service`). Verified 2026-09-09: daemon reachable at
   `/run/user/1000/.ydotool_socket`, doctor reports `ydotool: ok (daemon reachable …)`
- ✅ Type into a focused editor → text lands in the app. Verified 2026-09-09:
   `type_text "handsoff ydotool e2e 2026-09-09"` into gnome-text-editor →
   "typed 31 chars into org.gnome.TextEditor", full-screen OCR read the exact
   string back off the screen
- ☐ Typing into a *focused terminal* is refused; typing with unknown focus
  fails closed with an explanation (unit-tested in tests/test_desktop.py;
  verify once on real hardware with a visible terminal)
- ☐ `press_keys "ctrl+c"` copies from the focused app

## 6. niri IPC & desktop actions (P1)

- ☐ Ask "what windows are open" (run_command `niri msg --json windows` works)
- ☐ `niri_capabilities` lists this niri version's actions (not a hardcoded set)
- ☐ "open files" → the app launches, the window is identified, focused or
  explicitly reported as not focused
- ☐ "move this window to workspace 2" → moves; focus stays unless asked
- ☐ "close firefox" → polite close; windows confirmed gone in the reply
- ☐ scroll tool scrolls the focused app; wait settles a fresh dialog
- ☐ Operator (if enabled): screen_elements → click_element clicks the right
  element; pointer scale is correct on a fractional-scale output

## 7. systemd / niri autostart ownership (P0)

- ☐ Exactly one autostart owner: unit enabled → no `spawn-at-startup` for
  handsoff in `~/.config/niri/config.kdl`
- ☐ After a full reboot the bubble is up exactly once

## 8. Session teardown

- ☐ After logout/login, reminders and hands-free state match expectations
- ☐ `~/.local/state/handsoff/decisions.jsonl` exists and contains today's
  tool decisions (id, tool, target, decision, result)

## Sign-off

| Date | Tester | Commit / deployment sha | Result |
|---|---|---|---|
|  |  |  |  |
