# handsoff — gap analysis & improvement roadmap

Date: 2026-09-09 · Phase 0/1/2 of the external task list are closed (see the
Addendum below); remaining consciously-accepted items are at the bottom.

## Addendum — 2026-09-12 third audit batch: turn-counter atomicity, the model
## mirror that undid a reload, and two shared-directory hazards

Six real defects fixed, one finding disproved by measurement, one left as
documented. New guards: **7/7 mutations of the fixes back to their old
behaviour were caught**; 929 tests (was 920) green in six orderings, coverage
78.13% ≥ 70, `ci/compile_all.py` clean.

* **The exported turn counter was not atomic.** `core.lifecycle.next_turn` did
  `counter[0] += 1` then read the value back — load-add-store, then a separate
  read — with no lock. It is re-exported as `H.next_turn`, so any embedder can
  reach it, and the generation is exactly what the staleness checks
  (`gen != self._gen`) and the gen-keyed transcript cache trust: two turns
  claiming the same generation lets one utterance's text be answered in
  another turn. The increment and the read are now one critical section. The
  guard is deterministic rather than a race probe: a list/dict subclass that
  sleeps inside `__getitem__`/`__setitem__` makes the interleaving certain, so
  removing the lock fails every run — the earlier measurement is why, since
  the plain one-liner produced **0 duplicates in 64 000 claims** under the GIL
  and would have made a naive stress test green forever.
* **A settings reload's model drop could be undone by a call in flight.** The
  model caches live in two places for historical reasons — `core.audio` owns
  them, and `handsoff.py` mirrors them so `_speak`, health, doctor and the
  tests can ask `H._tts_model` "is a model loaded?". The mirror was
  read-then-assign, so a drop landing between the two steps was overwritten by
  the stale model the caller had just read, and it **stayed**: the load saw a
  non-None cache, returned the old model, and the adopt published it into the
  mirror again. A voice or device change then silently did nothing until the
  next reload. Three changes: the push reads the module copy **inside** the
  lock (a value read at the call site is read before the lock is taken, so the
  lock would have serialised the write and protected nothing — that was my own
  first attempt, caught by reviewing the ordering rather than by a test), the
  reload's drop takes the same lock, and the adopt refuses to republish a
  model `core.audio` no longer holds.
* **The uninstaller `rm -rf`'d a shared directory.** `~/.local/bin` is a user
  directory, `core` is a plausible name for someone else's package, and the
  wipe ran unconditionally — including when there was **no manifest**, i.e.
  when nothing at all had named the directory's contents. The manifest loop
  already removed every file this install deployed, so the directory is now
  dropped only when it is ours to drop (empty, or holding nothing but bytecode
  we generated) and saying so when it is kept. The no-manifest fallback also
  removes the checkout's own `core/*.py` — the same rule staging uses — because
  the imported floor alone would leave every module added later behind and the
  directory could never become empty.
* **The staging directory was predictable.** `staged.$$` is PID-derived, and
  nothing stops a second install starting while the first runs (the bubble's
  own self-edit restart racing a manual `./install.sh`), in which case both
  stage, gate and switch the **same** directory. It is created with `mktemp -d`
  now, and the releases directory it lives in is created first, or `mktemp`
  fails on a fresh install.
* **Timed-out diagnostics piled up threads.** A diagnostic worker cannot be
  cancelled — it is blocked in an Ollama or `nvidia-smi` call — so starting one
  per request is how repeated `health`/`doctor` against a wedged backend
  accumulates threads that never return. At most one runs at a time, and a
  second request is refused by name instead of joining the pile; the reply now
  carries the cause instead of "see log".
* **A bounded stop that gave up dropped the utterance silently.** `stop()`
  timing out returns `(None, True)` and the caller logs `wedged=1`, but the
  owner thread keeps the mic and finishes anyway — and the audio it captured
  was discarded with the thread, so "push-to-talk did nothing" left no trace
  that anything was ever spoken. The late capture is now reported as a WARNING
  with its frame count.
* **Repo hygiene.** The untracked voice-clip scratch (`optimus.wav` 6.1 MB,
  `optimus_clip.wav`, `output.wav`) and the root `test.py` were one `git add -A`
  from being committed; `.gitignore` now covers root-level audio (scoped with a
  leading slash so a future fixture under `tests/` stays trackable) and a root
  `test.py` (`testpaths = tests`, so such a file is scratch by construction).
  Corrupt-config quarantines (`*.bad-<ts>-<pid>`) are ignored for the same
  reason `*.bak-*` already was.
* **Disproved by measurement: the ydotool probe does not false-positive.** The
  finding was that a unix **DGRAM** `connect()` succeeds with no server, so a
  dead daemon reads as reachable. Measured on this kernel: a socket inode with
  no listener behind it returns **`ECONNREFUSED (111)` for both DGRAM and
  STREAM** `connect()`, so the probe cannot report a dead daemon as reachable.
  What remains is narrower and stated rather than fixed: a daemon that is alive
  but wedged still accepts the connection, because the probe is a connect and
  not a round-trip. A round-trip needs ydotoold's wire struct, and the
  previously proposed `/proc`-inode alternative was already measured to break a
  root-owned daemon — so the honest position is a documented limitation.
* **Left alone, deliberately: the watcher's residual ReDoS surface.** Nested
  quantifiers are refused and the blast radius is one daemon thread whose
  cursor advance is bounded, so an alternation like `(a|aa)+` can still burn
  that thread. It matters only if watcher patterns become user-facing free
  text, which they are not; changing it now would trade a documented bounded
  risk for an unbounded one (`re` has no timeout).

## Addendum — 2026-09-12 speech engine migration: Piper -> chatterbox-turbo

Piper is gone from the tree: the engine is now **chatterbox-turbo**, the voice
is either its built-in one or a clone of a reference clip the user picks, and
the 60 MB `.onnx` voice, the voice catalogue and the download dialog are
deleted. Tests: **920 green** (was 890) in six orderings, coverage **78.00%**
≥ 70, `ci/compile_all.py` clean, and **16 mutations of the new guards back to
the old behaviour were all caught**.

* **The engine swap is wide, not deep.** Piper was referenced in 11 non-test
  files and ~18 test files; the seams were already right (`tts_to_wav(text,
  path, voice_getter)` and a lazily-imported getter), so `core/audio.py` gained
  `get_tts()` with the same contract as `get_whisper` — names the engine, the
  device, the cause and the remedy, and never caches a failure. Output is
  24 kHz 16-bit PCM; `tts_rate` is applied by resampling (the engine has no
  duration control) and `tts_volume` is clipped before quantisation.
* **A reference clip could never have worked without a shim.** Measured on
  this environment (NumPy 2.5.3, chatterbox-tts 0.1.7): `norm_loudness`
  computes `wav * gain_linear`, which NumPy 2 promotes to float64, and the
  s3tokenizer mel matmul then raises *"expected scalar type Float but found
  Double"* — so **every** clip fails, not an odd one. One cast at that seam
  (verified: as-is raises, cast returns tokens `(1, 250)`). It is applied in
  `get_tts()`, and a guard asserts the shim is actually installed rather than
  merely present in the file.
* **The >5 s rule is one shared predicate.** `prepare_conditionals` asserts
  the clip is longer than 5 s, so a two-second sample is not a degraded voice
  but a **mute bubble**. `reference_problem()` answers for the bubble, the
  installer, doctor and the settings GUI, and `get_tts()` checks it *before*
  loading the weights — otherwise a bad clip is reported as "the weights are
  missing or damaged", sending the user after the wrong thing entirely. The
  measured case: the user's own `output.wav` is 2.56 s and is refused;
  `optimus_clip.wav` (10 s) and `optimus.wav` (33 s) are accepted.
* **The settings preview moved into the bubble.** Its old form loaded its own
  voice in the settings process; a second `ChatterboxTurboTTS` would cost
  another ~2.7 GB of VRAM. A new `say` control-socket action previews the
  running bubble's actual voice — same code path, one model. It is queued and
  length-capped (a 2000-word paste must not queue minutes of speech), and the
  preview is synthesized, not spoken, so it cannot be mistaken for a reply.
* **Migration, and the write that undid it.** `piper_voice` -> `tts_reference`
  (`SETTINGS_VERSION` 2). The old value is **dropped, loudly**, not renamed: an
  `.onnx` voice is not a reference clip, and carrying the path over would hand
  `prepare_conditionals` a file it must reject on every turn. Then two real
  defects surfaced, both found by *using* it rather than reading it:
  1. `_read_settings_for_write` read the raw file **without migrating**, so one
     unrelated single-key save put `piper_voice` straight back; and because
     removal was version-gated, a file already stamped v2 that still carried
     the key kept it **forever** — `unknown settings key 'piper_voice'` on every
     single start, for a key the user never wrote. Removal is now key-driven,
     so it cannot be re-stamped out of effect.
  2. The first fix dropped *every* key the schema did not know, and the suite
     caught what that costs: an existing regression test writes a key from a
     **newer** build and requires it to survive. Wholesale filtering silently
     erases configuration on version skew, which is worse than the nuisance it
     cured. Only the keys a migration explicitly **retired** are removed
     (`RETIRED_SETTINGS`, now `("piper_voice",)`, with its own forward-compat
     guard so the over-broad version cannot come back).
* **The xformers warning is a false lead, and it is now settled in the source.**
  `xFormers can't load C++/CUDA extensions` appears at every start and looks
  like the attention path is degraded. It is not: nothing in the TTS path calls
  xformers, the models report no attention implementation, and kernel-level
  evidence shows the attention already running `PyTorchMemEffAttention` (the
  xformers kernel, upstreamed into PyTorch). It runs in **fp32** because that is
  what `from_pretrained(device)` hardcodes — and half precision is not
  available as a fix: a hand-cast fails inside the library's own graph
  (`mat1 and mat2 must have the same dtype, but got Float and Half`). There is
  also no xformers build for torch 2.11+cu130 (0.0.35 targets torch 2.10).
* **The real latency win was the first call, not the steady state.** Measured:
  warm synthesis runs at RTF ≈ 0.3 (86 chars -> 4.40 s of audio in 1.28 s), but
  the **first** synthesis in a process costs 1.47–1.53 s against 0.57–0.69 s
  afterwards. `warm_tts()` now pays that ~0.87 s once at startup, where it is a
  startup cost rather than a penalty on the user's first reply.
* **Verification, not a log line.** A test asserts importing the app never pulls
  in torch or chatterbox (so CI's GPU-less runners still pass), the whole
  reference path was measured end-to-end, and the clone was proven against the
  built-in voice on one model and one sentence: **waveform correlation +0.015**,
  2.96 s vs 2.60 s, f0 **138.3 Hz vs 218.2 Hz** against the clip's 109.6 Hz
  (28.7 Hz away vs 108.6 Hz). A silent fallback would have produced *identical*
  audio. Live, the running bubble spoke 94 chars with `source: "tts"` in the
  level feed (56 non-zero samples, peak raw 0.467).

## Addendum — 2026-09-12 fault injection, second pass: the four remaining
## boundaries (compositor/ydotoold, speech models, ENOSPC, a backwards clock)

Same method as the first pass — break one boundary at the seam and require the
bubble to degrade *loudly* — extended to the four seams that were left: niri and
ydotoold exiting mid-turn, the whisper/piper models missing or unreadable, the
disk filling up, and the wall clock stepping backwards. **Twenty-six new tests**
(`tests/test_fault_injection.py`, now 46) and **six real defects**, none of which
the previous 864 tests could reach. Nine mutations of the fixes back to their old
behaviour were run and **all nine were caught** by the new guards.

* **A crashed turn was reported to nobody.** `_pipeline_worker` swallowed every
  exception from `_pipeline` with `log.exception("pipeline worker crash")` and
  nothing else. The commonest cause is the speech model being unavailable, which
  fails on *every* utterance: the user spoke and got nothing back, for good,
  while the bubble still looked like it was listening and the journal said only
  "crash". It now names the cause, tells the person (a notification plus a spoken
  line when the turn is still current), puts the bubble back to idle, and alarms
  **once per distinct cause** — a broken model fails on every turn, so an alarm
  per turn is its own bug. A stale turn does not talk over a newer one.
* **A reply that could not be spoken was recorded as spoken.** `_speak` wrote
  `_last_spoken`, `_turn_spoke` and `_recently_spoken` *before* synthesis, so a
  missing or unreadable piper voice left the bubble believing it had answered:
  the announce-and-listen window opened on silence and the user's next utterance
  was echo-filtered against a line that was never spoken. The three are now set
  only after playback really happened, the echo entry is withdrawn on failure,
  and the failure is reported once per cause. The echo entry is still armed
  *before* playback — deliberately, because the mic hears our own voice while it
  plays — and in the streaming path a failed sentence is dropped from `said`
  while the sentences that did play are kept.
* **A missing model named neither the path nor the fix.** `get_whisper` let the
  bare library error through ("Unable to open file 'model.bin'") and the CPU
  fallback call was not wrapped at all; `get_piper` did the same for a damaged
  voice. Both now raise a `RuntimeError` naming the model, the directory, the
  original cause and `install.sh`. Neither caches its failure (the model stays
  `None`), so a retry after re-downloading recovers — pinned, because a poisoned
  loader would leave the bubble deaf forever with no way back.
* **A dead ydotool socket was cached for the life of the process.**
  `_YDOTOOL_SOCK_CACHE` returned the first connectable candidate forever, which
  contradicted its own docstring ("a daemon started later must be picked up on
  the next call"): once ydotoold died, typing and clicking stayed broken — with
  an error every time, but permanently — even after the daemon came back on the
  *other* candidate (runtime dir vs the CLI's compiled-in `/tmp` default). The
  cached path is now revalidated and re-probed when it stops answering.
* **A dead compositor was reported as a failed app launch.** `open_app`
  snapshots the window list, spawns, then waits; if niri died in between, every
  poll failed and `_wait_new_window` swallowed it, so the timeout was reported as
  "no new window appeared — it may still be starting (or failed to launch)",
  pointing the user at the wrong thing entirely. The wait now distinguishes "the
  window list was unreachable on *every* poll" and says the compositor IPC is
  down. Keyboard injection is unchanged and still fails closed on unverifiable
  focus.
* **A backwards clock step silently changed a TTL.** `_load_world_seen` kept
  entries newer than `now - TTL`; read through a clock that has stepped *back*,
  `now - TTL` moves back with it, so entries genuinely past the TTL qualify again
  — the seen-store silently stops expiring and those headlines are never
  announced again for the duration of the rollback. The newest stamp in the file
  is now used as the reference when it is ahead of the clock (which is exactly
  the pre-rollback time), while the TTL still bounds the store. Session timing
  was already immune and stays so: `_tick_now()` is `time.monotonic`, pinned by a
  test that steps the wall clock back two hours.
* **A full disk broke a documented contract — and could damage the backup.**
  `set_setting`/`_persist_setting` promise a bool ("did this reach the disk?"),
  and every caller tests `is False` to warn about an unsaved change; an OSError
  from the write escaped instead, so a full disk made those callers crash with a
  traceback instead of saying the change was not saved. The write is now guarded
  and reports `False`. Separately, `_backup_runtime_json` copied straight over the
  old `.bak`, so a failure part-way left the **backup** truncated — the file you
  would restore from, damaged by the very failure backups exist for; it is now a
  temp file + `os.replace` like every other runtime write, with mode 0600 set
  before it becomes visible. The reminder queue had the same shape of hole: an
  ENOSPC makes `drain_due` raise before it returns the fired entries, so the
  reminder is never delivered *and* never pruned — it silently stops working, one
  journal line per tick. The worker now alarms once per cause.

**A tenth finding came from the ordering probe, not from reading code.** With
`HANDSOFF_TEST_ORDER_SEED=deadbeef` — the seed derived from the commit SHA, so CI
would have hit it — the suite went red on
`test_notification_reader_reports_an_unsaved_toggle`, green in collection order.
Root cause, measured not theorised: `core.tools._CURRENT` (and `core.doctor`'s)
is a ContextVar holding the dependency-injection host, and whichever handsoff
instance **loads last** owns it process-wide; the settings-app suites load a
second monolith, so from then on any test that builds a ToolBelt with `__new__`
(there are many) resolves `_dep().SETTINGS` and `_dep().set_setting` against the
*foreign* instance. conftest now restores both ContextVars around every test. A/B
proof on the same seed: neutered fixture → the same single red; restored → 890
green, and green in six orderings (default, seeds `1`/`424242`/`deadbeef`, file
orders `2`/`cafef00d`).

**Gates:** 890 tests, coverage **77.1% ≥ 70**, `ci/compile_all.py` clean, green
in all six orderings above. Deployed three-way equal (`in-sync`, 15/15 files,
`handsoff.py` 751adfea…, `core/tools.py` 926bd37c…, `core/audio.py`
e05c90f9…, `core/settings.py` c6e82551…), service active, no journal warnings
since restart, `--ptt status`/`--ptt level` answering, and every new guard
verified present in the installed copies. Nothing committed — the uncommitted
pile now spans five audit batches plus every feature of the last several turns.


## Addendum — 2026-09-10 full-project audit closure (trust gaps, release
## safety, policy UI, supply-chain pins)

Independent audit of the settled tree; every gate re-run on it: **659 tests
green (2026-09-10)**, `py_compile` clean, `bash -n` clean, coverage 71%
(≥ 70 floor). Findings → work landed the same day:

1. **Remote-brain opt-in formalized.** The runtime guard (`_guard_ollama_endpoint`,
   fail-closed) already existed but its settings key was a ghost — accepted
   from hand-edited JSON only, invisible to Settings and the schema. Now
   first-class: `allow_remote_ollama` in `settings_schema.py`, coerced
   fail-closed (only exactly `true` enables), a Brain-tab checkbox in
   Settings, a doctor line (`brain privacy: REMOTE …`) that works even when
   the bubble is dead, and README documentation under *Remote brain guard*.
2. **Staged-release deployment with rollback.** install.sh no longer copies
   straight into `~/.local/bin`: it stages the complete shipped set, gates it
   (byte-compile of every staged file + schema/core-settings import smoke),
   saves the currently-deployed set to `~/.config/handsoff/releases/prev`,
   then switches — auto-restoring the previous release if any switch step
   fails. `./install.sh --rollback` manually restores a release that
   installed fine but turned out bad. Pinned by `TestStagedRelease` (real
   installer subprocess in rehearsal mode).
3. **Per-tool policy rows in Settings.** The Permissions tab's raw
   `tool = POLICY` text editor is replaced by one ALLOW/DENY/CONFIRM dropdown
   per declared tool, built from the live `H.TOOLS` registry (new tools get
   rows with no GUI change; the text path remains as fallback when the
   registry is unavailable). Only non-ALLOW entries are persisted, matching
   `command_policy`'s empty-map-means-allow semantics.
4. **CI actions pinned to immutable SHAs** (`actions/checkout@3d3c42e5…`
   v7.0.1, `actions/setup-python@5fda3b95…` v7.0.0, resolved via the GitHub
   API), and the shell job now FAILS if a mutable tag ref sneaks back in —
   the old warning-only grep is a regression gate.
5. **Docs reconciled.** Test counts updated 583 → 637; the monolith-split
   status below reflects the executed Phase 4; the hygiene-hardening plan's
   checkboxes are closed against the code.
6. **Settings History tab rebuilt as a transparency view** (same day): the
   old Memory tab — which actually displayed history.json again — is folded
   away, and History now has three panes: **Conversation** (history.json,
   with the pre-existing clear-with-backup), **Durable facts** (the real
   `memory.json` store, listed deduped-by-key like the model sees it, with a
   confirmed Forget action that backs up and atomically rewrites the file),
   and **Decision log** (decisions.jsonl rendered one line per tool-policy
   decision, tolerant of garbage lines). The long-standing "Settings
   History/decision-viewer tab" open item is closed by this.
7. **GitLab CI mirror added.** The remote (gitlab.com/GeneticxClm/handsoff)
   is GitLab, so `.gitlab-ci.yml` now reproduces every GH gate: the test
   matrix (3.12/3.13), the ≥70 coverage job (same subprocess-coverage env),
   byte-compile, shell checks, installer smoke. Supply-chain parity: CI
   images are digest-pinned (tags in comments), and the `shell` job fails on
   any tag-only image ref — the GitLab-side analogue of the GH SHA-pin
   guard. The GH workflow stays the authoritative definition; keep the two
   files in lockstep.

Still open after this pass: the live-host redeploy (done — the appearance
set is deployed and doctor reports `in-sync`; see addendum item 7), the six
human acceptance items in
`ACCEPTANCE.md`, unsupported D-Bus Notify layouts, further settings-GUI
coverage depth (68% now; the remaining gaps are heavy widget-interaction
flows), and the consciously-accepted Phase-3 leftovers (staged release
directory *inside* the runtime + remote-transport policy doc are done; the
full provenance/rollback story for the *manifest* itself remains as-is).


## Addendum — 2026-09-12 six-item critical review (verified, four fixed, two re-scoped)

1. **Remote-Ollama opt-in disagreed with itself — partly real, and the fail-open
   half is now closed.** The claim was that `handsoff.py`'s guard accepted any
   *truthy* `allow_remote_ollama` (`"yes"`, `1`) while `core/settings.py`
   requires exactly `True` and the GUI coerces junk to `False`, so the same
   file could read as opted-in to one and not the other. Verified: the
   disagreement is real in the predicate, but **not reachable through
   `settings.json`** — the bubble's `SETTINGS` comes from the same
   `coerce_settings`, and I checked every junk shape end to end (`"yes"`, `1`,
   `"true"`, `"on"`, `[1]`, `{"a": 1}` all coerce to `False`, with a loud
   warning). It *was* reachable for any caller that seeds `SETTINGS` without
   coercion (tests, a future refactor), so `_remote_ollama_optin_source()` is
   now the single predicate — `is True`, not truthiness — used by the send
   guard and by `doctor` alike. The genuinely useful part of the finding is a
   second opt-in channel nobody could see: `HANDSOFF_ALLOW_REMOTE_OLLAMA` in
   the environment turns the brain remote with **no trace in Settings**, so
   `--ptt doctor` now names which channel opted in (`allow_remote_ollama in
   Settings` vs `HANDSOFF_ALLOW_REMOTE_OLLAMA in the environment — NOT visible
   in Settings`). Guard verified to fail on the old truthy predicate.
2. **Control-socket peer auth — the proposed fix cannot close the stated
   threat.** Verified as described: there is no `SO_PEERCRED` check anywhere,
   and the 0600 mode on the socket *inode* does not gate `connect()`. But the
   claim that this leaves "any same-user process" able to send
   `ptt`/`stop`/`settings` is an accepted risk that peer credentials cannot
   address: `SO_PEERCRED` reports the peer's **uid**, and a compromised child
   of ours or a sandboxed app running as us has *our* uid, so no uid check can
   exclude it. The actual boundary is already in place and is stronger than the
   claim assumed — `_prepare_runtime()` refuses to start unless `STATE_DIR`
   (where `control.sock` lives) is owner-only and not a symlinked directory,
   and it is `0700` on this machine, so another *user* cannot traverse to the
   socket at all. What I added is therefore defence in depth, not a fix:
   `_peer_uid()` logs and refuses a peer whose uid is not ours, which still
   holds if the directory mode is ever loosened, and keeps a record of who
   tried. The refusal reads the request before closing, because closing a
   socket that still holds the peer's unread bytes sends RST and the caller
   would see a connection reset instead of the reason.
3. **Reminders could never be emptied — real, user-visible, fixed.**
   `ReminderStore.update()` did `items = mutate(items) or items`, so
   `core.tools.cancel_reminder`'s `[r for r in items if ...]` returning `[]`
   was falsy, the *loaded* list was saved straight back and **cancelling the
   last remaining reminder silently did nothing**. `mutate` now returns the new
   list or `None` for "edited in place", and only `None` preserves the loaded
   list. Two regression tests (cancel-to-empty persists `[]`; an in-place
   mutation returning `None` is not reverted); both verified to fail on the old
   line.
4. **Racy turn generation — real, and the audit pointed at the wrong code.**
   The finding named `core/lifecycle.next_turn()` (and its duplicate in
   `handsoff.py`), but that helper has **no runtime callers** — it is a facade
   kept alive by its own tests, so fixing it would have changed nothing. The
   live increments are eight inline sites (`_say_now`, `_announce_missed`,
   `_fire_timer`, the crash report, `begin_listening`, `finish_listening`, the
   PTT submit path, the dictation toggle), all now going through
   `Assistant._bump_gen()` under one lock. Measured rather than asserted, and
   the measurement corrected the finding: the plain one-liner
   (`self._gen += 1` then `gen = self._gen`) did **not** reproduce — 64 000
   concurrent claims, zero duplicates under the GIL — but the *wide* shape,
   where `threading.Event()` is constructed between the increment and the read
   (three of the real sites), produced **714 duplicate generations per 32 000
   claims (~2%)**, and 16 930 per 128 000. A duplicate generation is exactly
   what the `gen != self._gen` staleness checks and the gen-keyed transcript
   cache trust, so a second utterance could be answered with the first
   utterance's transcript. The guard is deterministic (a second thread is
   proven to block on the lock) rather than a stress test, because the stress
   test alone passes on the narrow broken shape; a source guard also pins that
   no new `self._gen += 1` appears outside the helper. Both verified to fail on
   the mutated code.
5. **Unrestricted file read into LLM context — real, and there was a second
   door.** `read_file` had no denylist while `cat` sits on the command
   whitelist, so `run_command("cat ~/.ssh/id_rsa")` was an independent route to
   the same content, and `watch_file` announced matched lines from any path.
   One predicate (`denied_secret_path()`) now covers all three: credential
   stores and key material (~/.ssh, ~/.gnupg, ~/.aws, ~/.kube, ~/.docker,
   ~/.password-store, browser profiles, keyrings), shell history, and
   secret-shaped names (`*.pem`, `*.key`, `*.env`, `credentials`, …). It
   matches on the **resolved** path, so `~/.ssh/../.ssh/id_rsa` and a symlink
   cannot slip past; `read_file` refuses *before* the existence check so it is
   not an oracle; and the model's own prompt now says the refusal exists so it
   does not burn turns or ask the user to paste a key. Precision was tuned by
   measurement: an `id_rsa*` glob wrongly refused `~/Documents/id_rsa_notes.md`,
   so the OpenSSH names are exact (`id_rsa.pub` is deliberately readable —
   public keys are not secrets) while `.ssh/` covers the real keys. Seven
   tests, four of them verified to fail on the un-wired code.
6. **Model-switch "memory cleared" didn't clear — real, and deeper than
   stated.** Confirmed: the path took a backup and never truncated, so the new
   model inherited exactly the transcript its own comment blames for
   parroting. But truncating the file is *not* sufficient and the finding stops
   one step short: the bubble holds the transcript in `_history` and
   `_save_history()` rewrites the whole file, so a second process deleting it
   is silently resurrected on the next turn. There is now a `clear-history`
   control action (`Assistant.clear_history()`, documented in `USAGE`) and
   Settings→Brain calls it after truncating, so both writers forget. The same
   defect was sitting in the History tab's own manual **Clear** button, which
   only ever took a backup and told the user to restart — both paths now share
   one `_wipe_history()`. A related hazard turned up while testing: a disk
   reload did not refresh `_model_at_open`, so an unrelated save after an
   external model change looked like a switch — harmless while it only made a
   backup, destructive once the clear is real — so `reload_from_disk()` now
   rebases it. Guard is an offscreen scenario that fails both when the
   truncation is removed and when the rebase is removed.

Accepted consciously / out of scope here: same-uid control-socket callers (see
item 2 — no credential check can separate them; the 0700 state directory is the
boundary and the residual risk is documented rather than papered over), and
`core/lifecycle.next_turn()`'s own counter, which is still unsynchronised but
has no callers (see item 4) — left alone rather than refactored blind.


## Addendum — 2026-09-12 second review batch (items 7–28): nine verified and fixed, one false, twelve unexamined

A second list of "major" findings arrived. Every one was read against the tree
before anything changed. **Nine were real and are fixed; one is a false finding;
twelve were not reached in this pass and are listed as open at the end of this
section rather than quietly implied to be done.**

**7. Settings persist swallowed its own failure — real, fixed.**
`_persist_setting` caught `OSError` and returned, but `Settings.persist` then did
`self._data[key] = value` regardless, and the bubble's wrapper went further still
— updating `SETTINGS`, re-stamping the version and running
`reload_derived_settings()` — so the runtime used, and re-wrote, a value that was
never on disk. It now returns a bool, `persist` only updates the cache on
success, and the wrapper keeps the old value and logs when it did not reach the
file. Three tests, verified to fail on the old code.

**8. Backup sidecars were not hardened — real, with live evidence.**
`shutil.copy2` preserves the *source* mode, so a runtime file copied while it was
still 0644 left a 0644 backup holding the same content. This is not theoretical:
`~/.config/handsoff/history.json.bak-modelswitch` — a full conversation
transcript — is `0644` on this machine, as are `history.json.bak-parrot`,
`.bak-ydotool-err`, `.bak-loop2` and `settings.json.bak-threshold`. New backups
are forced to 0600 (`_backup_runtime_json`, `_backup_keep_n`, `edit_file`'s
`.bak`), and `_secure_runtime_files()` now sweeps `*.bak*` in the config and state
directories on startup, because hardening the live file never touched the
sidecars written before it. Three tests, including one that a symlinked sidecar
is not followed out of the config dir.

**10. ydotool "reachable" false positive — NOT reproducible.** The claim was
that a SOCK_DGRAM unix `connect()` succeeds with no server listening, so a dead
`ydotoold` still probes reachable. Measured directly: bind + close a DGRAM
socket, then connect → **`ECONNREFUSED` (errno 111)**, exactly like SOCK_STREAM.
The kernel refuses when nothing is bound to the path, so `_socket_connectable`
is not fooled. Worth recording because the proposed fix would have made things
*worse*: a `/proc`-based "is a live process holding this inode" check cannot see
a root-owned `ydotoold`, and would have reported the working daemon unreachable.

**11. play_wav leaked a stream and pinned the visual — real, fixed.**
`sd.OutputStream(...)` and `start()` sat *outside* the `try`, so a failure
skipped the final `_emit_level(0.0)` — leaving the bubble's designs frozen at the
last playback level with no further audio coming to release them — and a
`start()` that raised after a successful construction leaked the stream. The
stream is now created inside the `try` with teardown in the `finally`. Two tests
(constructor fails, `start()` fails), both verified to fail on the old shape.

**12. tts_to_wav left a truncated wav — real, fixed.** `wave.open(target, "wb")`
truncates the target immediately, so a synthesis failure left a truncated file
where the last good reply had been. It now synthesizes to a `.part<pid>` sibling
and `os.replace`s it, unlinking the temp on any failure. Two tests (failure keeps
the previous file; success still replaces it), verified to fail on the old code.

**13. Recorder races — real, fixed.** `start()` overwrote `self._stream`, leaking
the device (PortAudio kept it open), and `_cb` appended while `stop()`
concatenated with no lock. `start()` now closes any previous stream first (and
closes the new one if `start()` fails), and a buffer lock guards
`_frames`/`_samples` and `_level`. The lock is deliberately **not** held across
`stream.stop()`: PortAudio joins the callback there, so holding it would
deadlock against `_cb` trying to append — which is why `stop()` stops the stream
first and only then snapshots the buffer.

**14. Bounded stop discarding audio — not examined this pass** (see open list).

**15. `_open_input` hid the root cause — real, fixed.** The retry path did a bare
`except Exception` and re-raised the *second* error, so "invalid sample rate"
hid the real "device busy". The retry now does `raise retry_error from
first_error`, so the original cause is in the traceback.

**16. The thinking filter dropped legitimate replies — real, fixed, and it cut
both ways.** `strip_thinking` only removed *closed* `<think>…</think>` blocks, so
an unterminated one streamed the model's reasoning into TTS; and the stream loop
dropped any sentence merely *starting* with `<`, so "<3 that's sweet" and "it's
<5 minutes away" were silently swallowed before speech. The filter is now a
closed set of real control tokens (`<think>`, `<tool_call>`, `<|...`) and
unclosed blocks are dropped wholesale. The duplicated fallback filter in
`handsoff.py` (used when `core/` is not importable) was hoisted to one
module-level implementation, and a test asserts the fallback and `core.brain`
agree over a corpus — the drift between two copies is how the `<` bug survived.
Five tests, all verified to fail on the old code.

**17. Barge-in spoke the pending tail — real, fixed.** When `cancel` broke the
read loop, the buffered tail was queued anyway (only the terminator followed), so
a partial sentence was spoken *over* the user who had just interrupted. The tail
is now flushed only when the turn was not cancelled. Pinned by a test that sets
`cancel` mid-stream and asserts the queue holds nothing but `None`.

**18. Argument coercion guessed instead of reporting — real, and worse than
claimed.** `bool("false")` is `True`, so a JSON `"false"` silently *inverted* a
flag; junk became `0`. The unlisted half: because the loop always passed every
parameter, an **omitted optional numeric argument was sent as `0` instead of
taking the default its signature documents** — `snooze_reminder(minutes=10)`
failed its own bounds check whenever the model left the argument out, making the
documented default unreachable. Coercion is now strict (`true/false/yes/no/on/off`),
junk raises and surfaces as `ERROR: bad arguments for <tool>: …, got 'soon'`, and
an omitted parameter with a default is simply not passed. Five tests, including
an end-to-end one that snoozes with no `minutes` and asserts ~10 minutes.

### Open — not examined in this pass

Listed explicitly so the gap is visible rather than assumed closed: **9**
(`watch_file` ReDoS / symlink following / existence oracle), **14** (bounded stop
discarding the owner thread's audio), **19** (CONFIRM TOCTOU), **20** (`BoundedJob`
zombies and `output_tail` tailing the head), **21** (notification reader thread
leak), **22** (pomodoro double-start), **23** (ICS over cleartext http), **24**
(whisper mirror resurrection), **25** (GUI saving unvalidated values), **26**
(`install.sh` `rm -rf` on a shared `BIN_DIR`), **27** (`_with_timeout` thread
pile), **28** (CI floor/comment mismatch and the GitLab junit digest on jobs that
write no report).

**Update (same day, later in the session): items 9, 19, 20, 21, 22 and 23 are
closed** — see the fourth addendum below, which also records that 19 was
disproved rather than fixed and that 9 raised a new finding (the secret iCal URL
was echoed into the transcript by the calendar error path). Still open: 14, 24,
25, 26, 27, 28.

Also observed while running the suites, and **not** caused by these changes:
the `TestKillProcess` pair in `tests/test_policy.py` was order- and
environment-sensitive — it matched `sleep` processes by name, so a stray `sleep`
left by an earlier run made `kill_process` report "several match" and the
confirmation assertions fail. **Fixed** in the third-review addendum below.


## Addendum — 2026-09-12 third review batch (concurrency, isolation, install traps)

A narrower batch than the previous two: two real shared-state races, one test
isolation defect that could fail CI for unrelated reasons, and four items checked
and deliberately left alone.

1. **`hardware._probe` mutated the shared TTL cache without a lock.** The cache
dict is shared between `doctor`'s worker thread and the Qt thread's hardware
tick, and the check-then-store was unsynchronized, so a stamp could be written
from a value that had already been superseded — a stale entry coming back
looking freshly probed. A module-level `_TTL_LOCK` now guards the read and the
write. It is deliberately **not** held across `fn()`: the probers shell out
(`nvidia-smi`, `systemctl`, `journalctl`) and serializing them would let one
stalled probe block every other section; two threads may still probe the same
section at once and the last result wins, which is what the lock actually needs
to prevent.
2. **`BoundedJob` could announce the same completion twice.**
`if not job._announced: job._announced = True` is a check-then-set with no lock,
and a spoken turn and a hands-free turn can poll concurrently — both saw `False`
and the finished job was spoken twice. Replaced by `claim_announcement()`, which
returns `True` for exactly one caller.
3. **`tests/conftest.py` loaded `handsoff.py` without registering it in
`sys.modules`.** Several test modules load the monolith by path as
`handsoff_core`; because that name was never registered, a later `import
handsoff` built a **second instance** with its own `SETTINGS`, locks and caches.
Core submodules were already shared (`H.ToolBelt is core.tools.ToolBelt`), so the
divergence was inert in practice — but it is exactly the shape that produces
order-dependent flakes rather than errors, so `_load` now registers the module
(and only for `handsoff.py` aliases the bare name, since doing it for every
module would put `core/audio.py` into `sys.modules` as `"audio"`), and unwinds
on a failed load so no half-initialised module is left for the next test.
4. **`TestKillProcess`'s two-step tests were environment-sensitive.** They
matched `sleep` by name against the *real* `/proc` scan, while
`kill_process` requires an EXACT single match — so any stray `sleep` owned by
the same user (a leftover from an earlier suite, another test's helper, the
developer's own shell) turned the offer into an "several match" ERROR and failed
the test for a reason unrelated to the code under test. Reproduced
deterministically with one stray `sleep`. Discovery is now pinned to the test's
own child; the kill itself still happens for real, and the test still fails if
the offer/confirm handshake regresses (verified by mutation). The real-scan
behaviour keeps its coverage in `test_ambiguous_match_refused`,
`test_no_match_and_other_users_invisible` and `test_port_targeting`.

### Checked and deliberately not changed

* **`ydotool` "reachable" false-positive (item 10)** — **not reproducible**: a
dead DGRAM server returns `ECONNREFUSED (111)` from `connect()`, same as
`SOCK_STREAM`, so liveness was never falsely reported. The `/proc`-inode check
proposed as the fix would additionally have broken a root-owned `ydotoold`
(same socket, different uid in `/proc/net/unix`). No change made.
* **`install.sh` `PYTHON_PKGS` probe** — a stale pacman DB can report a new
package as missing; the script already tells the user to refresh and documents
the trap. Widening it to a blind `pacman -S` would trade a clear message for an
unprompted sync. Left as documented behaviour, not a support trap to paper over.
* **`_resample_to_16k`** — the FFT low-pass has no window, so non-integer
ratios leak a little spectral energy. Quality-only, inaudible at speech rates,
and the alternative (a real filter design) is a dependency-sized change for no
functional gain.
* **`core/__init__.py` module swap** — the loader's bare-name swap really is
gated on `_origin_ok(prev_bare)` and the failure path restores. Subtle but
correct; recorded as fragile rather than rewritten under a race-sensitive
suite.

Suite after this batch: **822 tests, coverage 76.2% ≥ 70**, `ci/compile_all.py`
clean, and the `TestKillProcess` pair passes **with a stray `sleep` deliberately
alive**, which is what the fix was for.


## Addendum — 2026-09-12 fourth review batch (the six open items): five fixed, one disproved, one extra finding

The six items left open by the previous passes, worked one at a time and each
pinned with a guard that was verified to fail on the unfixed code.

1. **`watch_file` ReDoS (item 9).** Real, and measured rather than assumed:
   `(a+)+$` against a 29-character *non-matching* line runs for minutes — each
   added character doubles the work — and a watcher evaluates its pattern once a
   second on a daemon thread that nothing can interrupt, so the watcher dies for
   good and pins a core doing it. Stdlib `re` has no timeout, so three bounds are
   enforced instead: the exponential SHAPE (a quantified group whose own content
   carries a quantifier: `(a+)+`, `(\d+)*`, `(.*x){4}`) is refused with a clear
   message; the pattern length is capped; and the text any single evaluation sees
   is capped. The refusal is deliberately limited to shapes we can name with
   confidence, so a legitimate pattern is never rejected on a guess —
   `(ERROR|WARN): .*` still works. Residual risk is documented rather than
   hidden: an *overlapping-alternation* blowup like `(a|aa)+` is still
   expressible, because telling it apart from a safe `(foo|bar)+` needs
   first-character-set analysis we are not going to do.
   Synthesis caught a bug of my own making here: the first version capped the
   lines examined per poll but still advanced the file cursor past the whole
   chunk, so a burst larger than the cap was **silently dropped forever**. The
   cursor now advances only by the bytes actually examined, which defers the
   rest to the next poll, and the guard tests both halves (bounded per poll AND
   nothing skipped, in order).
2. **CONFIRM TOCTOU (item 19) — disproved.** The finding described the pending
   offer as a snapshot taken under the lock and re-checked after the release.
   It is not: the offer is both read and cleared inside one
   `_confirmation_lock` critical section, so a racing second `confirm_action`
   finds nothing pending and refuses. Pinned with eight real threads (exactly
   one may run the tool). The only thing the post-release read ever did was print
   a second log line, which is what made it *look* like a check-then-act; that
   read is gone. The finding's second half — the offer is not cleared at turn end
   — is by design: the whole feature is "offer in one turn, confirm in the next",
   and the offer is time-boxed by `confirm_seconds`. Narrowing it to "only the
   immediately following turn" was considered and rejected: the turn marker is
   the generation counter, which other events can bump, so the rule would
   sometimes refuse a legitimate confirmation.
3. **`BoundedJob` (item 20).** Three separate defects, all fixed. (a) The drain
   buffer kept the FIRST `MAX_OUTPUT` bytes, so `output_tail` — documented as
   "recent output" — handed back the startup banner and dropped whatever came
   after it for any job that outran the cap; it now keeps the LAST bytes, with
   amortised trimming that halves the buffer at a time. (b) A `SIGKILL` whose
   `wait(timeout=2)` expired was swallowed and the job still reported
   `timeout-killed`, dropping the only reaper the job has and leaving a zombie
   until handsoff exited; it now stays `running` and retries. (c) `read()`
   returns only when *every* writer closes the pipe, and the job runs in its own
   session, so a grandchild that inherited stdout kept the drainer blocked for
   the rest of the session — the reap path now releases it.
4. **Notification reader (item 21).** `set_enabled(False)` never joined the
   worker, so "off" was only a promise: the old loop could still be parked on the
   dead monitor's stdout, and enabling again started a second loop beside it —
   two readers, one holding the previous stop event, both speaking. Disable now
   joins (and clears the reference). The bigger defect was the silent death: five
   dead `dbus-monitor` respawns left `notification_reader=True` with nothing
   listening, so the toggle and the settings file said on and no notification
   would ever be spoken again. Giving up now logs at error level, clears the
   monitor and persists the reader OFF.
5. **Pomodoro (item 22).** The `is_alive()`-then-start pair was an unlocked
   check-then-act, so two overlapping turns could start two workers announcing
   every phase boundary twice; start/stop/transition now share one re-entrant
   lock. The ghost announce was real and is now deterministic: the phase flip
   re-checks under that lock whether a stop landed while its `wait()` was
   expiring, so "pomodoro stopped" can no longer be followed by "back to work".
   `shutdown()` also joins, so "stopped" means the worker is out rather than on
   its way out.
6. **ICS over cleartext (item 23).** A Google "secret iCal address" is a bearer
   credential — whoever reads the URL reads the whole calendar — and the settings
   row has always advertised https while the code accepted `http://` for any
   host. Plain http is now refused off-loopback (`http://localhost` and
   `127.0.0.0/8` stay allowed, since a URL pointing at this machine is not on the
   wire). **New finding while fixing it:** the failure path echoed the *source
   URL* into the transcript (`could not read calendar source(s): http://…/`, and
   `HTTPError` strings carry the full URL), so a failing calendar fetch printed
   the user's secret token into the conversation. Sources are now reported
   scheme+host only, with the reason sanitised.

Suite after this batch: **841 tests, coverage 76.4% ≥ 70**, `ci/compile_all.py`
clean, all eight new guards verified to fail on the mutated (pre-fix) code —
including, for the watcher, that a pattern the guard *refuses* really does hang
a subprocess past a 2-second timeout.


## Addendum — 2026-09-12 test-suite determinism audit (ambient state, ordering, wall-clock)

A whole-suite audit for the defect class just fixed in `TestKillProcess` (tests
that depend on ambient system state, stray processes, ordering, or wall-clock
timing), fixing each at its cause rather than at each symptom. Two of the
findings were systemic and would have kept producing flakes forever.

1. **The suite graded itself against the developer's configuration.** The
   monolith resolves `CONFIG_DIR`/`STATE_DIR` from `HOME`/`XDG_STATE_HOME` at
   import and then READS the `settings.json` it finds, baking the result into
   module-level state: `SETTINGS`, `SYSTEM_PROMPT`, derived globals. On this
   machine `notification_reader` is true, so **every `Assistant()` built
   anywhere in the suite spawned a live `dbus-monitor` and leaked its reader
   thread** — 13 tests were leaving one running — and the system prompt was
   built from a personal assistant name. Tests were also reading and writing the
   real `~/.config/handsoff`, history, memory and control socket. conftest now
   imports the app with `HOME`/`XDG_STATE_HOME`/`XDG_CONFIG_HOME` pointed at a
   throw-away directory and restores them immediately, so the module is
   configured from defaults and the tests are out of the developer's files.
   A guard test asserts the isolation (no real home in the parents of
   `CONFIG_DIR`, `STATE_DIR`, `HISTORY_FILE`, `MEMORY_FILE`, `LOG_FILE`,
   `CONTROL_SOCK`), because the failure mode is silent: everything still passes,
   just against someone else's settings. `test_hardening.py` was also loading
   its OWN second copy of the monolith (bypassing the isolation entirely, and
   giving the suite two module objects with separate locks and caches); it now
   shares the one session-scoped instance.
2. **Two tests only passed because of that configuration** — which is exactly
   how a config-dependent suite hides in plain sight. `_match_wake` fuzzy-matches
   a misheard name by pronunciation skeleton, and the skeleton needs a name of
   four or more letters that shares consonants with what whisper heard;
   `test_match_wake_fuzzy_misheard_name` asserted the whole `cypher`→`Siphon`
   story while inheriting the name from `settings.json`, so on the default name
   it failed. The name is now pinned in the test. `test_prompt_identity_uses_wake_name`
   compared `SYSTEM_PROMPT` (baked at import) against live settings, so it only
   matched when the real config had been renamed.
3. **Ordering is now irrelevant by construction.** Tests hand-edit the
   session-scoped monolith's globals (`SETTINGS["command_policy"]`, `OLLAMA_BASE`,
   the snooze/kill offers, the notify coalescer) and several never restored
   them, so a leak could only surface as a failure in an unrelated *later* file.
   An autouse fixture snapshots and restores that state around every test, so no
   test can be affected by one that ran earlier. Verified: the suite is green in
   collection order, reversed order, and two shuffled file orders.
4. **Leaked worker threads are now failures, at the test that leaked them.**
   An autouse fixture fails a test that leaves a stoppable worker (file/process
   watcher, job drainer, pomodoro, notification reader) running, after a bounded
   settle so a legitimate unwind is not mistaken for a leak. It found the 13
   reader leaks above the first time it ran, and was verified to fire on
   deliberately leaked `watch-file`/`notification-reader` threads.
5. **Wall-clock timing replaced by condition polling.** A fixed `sleep(N)`
   followed by an assertion is a bet on this machine being idle — it can only
   fail spuriously, never catch a bug faster. Converted:
   * the four notify-coalescing tests slept 0.6–0.8 s against a 0.5 s production
     debounce; they now wait on the coalescer's own state (timer cleared,
     nothing pending) plus the expected popup count, which is *stronger*: a
     batch that would send a trailing duplicate still has its timer set, so the
     state is not idle yet. Verified to fail on a mutated flush that sends a
     duplicate.
   * `test_lifecycle`'s `fake_ollama` fixture slept 0.8 s hoping the server had
     bound; it now polls a TCP connect on its ephemeral port and fails with the
     child's exit code if it died. (An HTTP readiness probe was written first
     and would have hung forever — the fake implements only `POST /api/chat`.)
   * four sleeps-then-assert-a-worker-ran sites (reminder/timer/snooze
     announcements, the snooze persistence) now poll through a shared
     `conftest.wait_for`.
   * the `_bump_gen` contention probe slept 0.15 s and hoped the competing
     thread had reached the claim; it now wraps the lock so the test knows the
     claimer is *inside* the acquire, which makes "nothing claimed yet" mean
     blocked rather than not-yet-scheduled. Verified to fail on an unlocked
     `_bump_gen`.
6. **Ambient processes and a hardcoded port.** `test_port_targeting` bound port
   18744 and slept 0.6 s: a fixed port collides with anything else on the
   machine (or a leftover server from an earlier crashed run). It now takes an
   ephemeral port and waits for the listener, failing with the child's status if
   `http.server` exits early. `test_ambiguous_match_refused` scanned the real
   `/proc` for two `sleep`s; it now pins discovery to two entries, since the
   subject is the EXACT-single-match rule. The remaining `sleep`s in the suite
   were reviewed one by one and left alone where they are bounded polling loops
   or deliberate concurrency probes.
7. **Order-independence is now a CI gate, not a manual habit.** Everything above
   made the suite *capable* of passing in any order; nothing stopped the next
   ordering dependence from living in collection order forever, since that is the
   only order anyone runs day to day. `tests/conftest.py` gained two seeded
   shuffle modes (`HANDSOFF_TEST_ORDER_SEED` for the tests, `HANDSOFF_TEST_ORDER_FILES`
   for the file order only, each file's internal sequence intact), and a new
   `order` job in both pipelines re-runs the whole suite in each mode. The seed
   is derived from the commit SHA, so the order differs commit to commit while a
   red build stays exactly reproducible from the seed the run banner prints (the
   terminal summary repeats it, because CI runs `-q`, which suppresses the
   header — a shuffled failure without its seed is not diagnosable). The
   *file-contiguous* mode is not redundant: it is the stricter probe of what one
   file leaves behind for the next, and its failure output is far easier to read.
   The mechanism was proven to bite before it was wired in: a scratch module with
   an intentional writer-before-reader dependence passed in collection order and
   was caught by 3 of 8 shuffle seeds. Verified green across seven orderings
   (HEAD-SHA test-shuffle, HEAD-SHA file-shuffle, plus seeds 1/424242/deadbeef
   and 2/cafef00d), 842 tests each.

Suite after the audit: **842 tests**, coverage 76.2% ≥ 70, `ci/compile_all.py`
clean, green in collection, reversed and shuffled file order. No production code
changed, so the deployed bubble is unchanged (`--ptt doctor` still `in-sync`).

Also fixed while in these files: the `coverage` job's name and header comment
both said **≥ 60%** while the command enforced `--cov-fail-under=70` (and
`.coveragerc` said 70) — the floor itself was right, only the label lied.


## Addendum — 2026-09-12 fault-injection pass (the boundaries, broken on purpose)

Every other suite proves what the bubble does when things work. This pass breaks
one external seam at a time — at the boundary (the HTTP opener, the recorder's
return value, the popen factory, the write path), never by standing in for the
code under test — and asserts the bubble degrades **loudly**: a WARNING/ERROR
findable in `journalctl`, a spoken line naming the cause, a reported failure, a
toggle that stops claiming to be on. It also asserts the *absence* of the silent
alternative: no fabricated success, no swallowed error, no value reported as
saved when it is not. `tests/test_fault_injection.py`, 19 tests, 4.6 s.

**Five real defects, none of which any existing test could have caught:**

1. **Ollama refusing produced the wrong diagnosis** (worst of the five, and it
   hit the user-visible path every time their model server was down).
   `core.brain.ollama_chat_stream` re-raised `urllib.error.URLError` as-is,
   while the non-streaming `ollama_chat` converts it to `RuntimeError` *precisely
   because* `_brain_turn` diagnoses a down brain from `RuntimeError` alone. With
   streaming on (the default) a dead Ollama therefore spoke *"Sorry, my brain
   gave me an empty answer"*, logged "stream finished without a result", and
   threw the real error as an unhandled exception in the streamer thread — the
   actionable part ("cannot reach Ollama at …; `systemctl start ollama`") went
   unspoken. The conversion now happens in the streaming path too, and the test
   drives the real `_brain_turn` with only the opener injected.
2. **A push-to-talk press that produced nothing was dropped in silence.**
   `submit_audio` returned bare — no line, no state, no trace — for `audio is
   None` (bounded stop timed out, device failed to open) and logged a plain
   `INFO` for a capture below the threshold. A muted device and a silent user
   were indistinguishable in the journal. Now the empty case is a WARNING naming
   the two causes, and a rejected capture reports `frames= peak= threshold=`; the
   *hands-free* variant deliberately stays at INFO, because room noise there is
   ordinary and crying wolf would make the loud case worthless.
3. **A failed settings write left memory claiming the change.** `set_setting` —
   the single entry point every mutation is supposed to go through — wrote
   `SETTINGS[key]` *before* calling `_persist_setting`, and ignored its return,
   reintroducing at the entry point exactly the divergence item 7 closed for the
   wrapper's other callers. It now updates memory only on a reported success and
   returns whether the value reached the disk.
4. **The notification toggle lied about an unsaved change.** The
   `notification_reader` tool (and the mute list) reported success regardless of
   the write; both now return a WARNING naming the revert-on-restart. The same
   for `set_handsfree`, whose `except OSError` was dead code — `_persist_setting`
   reports failure, it does not raise.
5. **A control socket that vanished under a live bubble was never noticed.**
   The accept loop keeps listening on an inode no client can reach; `--ptt` said
   "not running", doctor said "not created yet", and the running process said
   nothing at all. The loop now compares the path against the ident captured at
   bind (once per idle second) and either re-binds and says so, or — when
   *another* inode holds the path — logs an error and leaves it alone, because
   unlinking could delete a second live bubble's socket. The CLI also names the
   socket it looked for now.

6. **The installer delivered files the project does not own** — found *after*
   deploying, and only because the deploy was verified: `--ptt doctor` reported
   `installed-drift` minutes after a clean `in-sync`, on a tree where all fifteen
   deployed files hashed equal. The cause was a sixth `.py` in the tracked set: a
   scratch `test.py` the user had made for a TTS experiment. `install.sh` staged
   `"$HERE"/*.py` on the theory that *anything beside handsoff.py is part of the
   app* — so that scratch file was copied into `~/.local/bin` (the user's PATH)
   and hashed into `deployment.json`, and the moment the user edited their own
   experiment, doctor declared the whole bubble stale. A stray name can also
   **collide with a real binary in `$BIN_DIR` and overwrite it** — the same shared
   directory hazard as the uninstall path. Top-level membership is now *the
   declared entry points plus what the repo tracks* (`git ls-files`), with the
   glob kept as the fallback outside a git work tree, so a new module still ships
   the moment it is committed (no list to maintain) while a scratch file never
   leaves the checkout. One helper (`ship_top`) is the single definition, called
   by staging, the manifest generator and the rehearsal check. Verified in an
   isolated copy: a **committed** new module still shipped, an **untracked** one
   was skipped and announced; and on the real tree the rehearsal prints
   `NOT shipping test.py …` and the manifest no longer tracks it, so doctor
   reports `in-sync` again.

**Three findings about my own work, each caught by measuring rather than
assuming:**

* The obvious way to tell whether the path still names our socket — compare
  `os.fstat(sock.fileno())` with `path.lstat()` — **does not work**: on an
  AF_UNIX fd, `fstat` reports the *socket object's* inode, not the filesystem
  entry's (measured: they differ). A draft built on it would have declared the
  socket orphaned every second and re-bound forever. The ident is captured from
  `lstat` right after `bind` instead, and that was measured to be stable and to
  change when another process takes the path.
* The first draft of the rebind **raced shutdown**: `stop()` removed the path,
  the accept loop was sitting in its timeout branch, and the loop re-created the
  socket *after* cleanup — leaving behind the stale inode the class exists to
  avoid. It showed up as a cross-test failure (`clear_history_roundtrip` served
  "cleared 0"), i.e. the very ordering dependence this suite has been auditing
  for. The loop now breaks on the stop flag and `_rebind` refuses while
  stopping, pinned by its own test.

Every guard was verified to fail on the mutated code (six mutations: the
URLError conversion, both halves of the capture path, the `set_setting`
pre-write, both tool reports, the handsfree warning, the orphan check, and the  stop guard). Live verification on the running bubble, not just in tests: the
control socket was deleted with the bubble up, `--ptt status` failed with the
path named, and the bubble re-bound and logged the warning (inode 52035683 →
52035763) with `--ptt status` answering again.

* **The periodic repair first read the wrong thing — the module global.**
  `CONTROL_SOCK` can be reassigned (every test that touches the control socket
  does exactly that), and a server left running from an earlier test then
  repaired *whatever path the global now named*: measured directly, a server
  bound at `a.sock` happily created a fresh 0600 socket at `b.sock` after the
  global moved. In the suite that surfaced as an intermittent — and, because it
  is timing-based, only sometimes reproducible — failure in
  `tests/test_hardening.py::TestSecureFile::test_stale_permissive_socket_self_heals`,
  whose 0777 socket was replaced under it. Ownership is now the path string
  captured at bind: anything else is somebody else's socket, and the server
  touches nothing. (Note the first attempt at proving this guard bit failed
  *because the guard was in two places* — the mutation had to remove both.)
  Three consecutive file-shuffled full runs are green since, where two runs
  before it produced the failure once.

864 tests (20 fault-injection + 2 installer-membership), coverage 76.36% ≥ 70,
`ci/compile_all.py` clean, green in default, shuffled-test and three
file-shuffled orders. Deployed (`in-sync`, 15/15 files three-way equal,
`handsoff.py` f5c7df39…, `core/brain.py` ed42875e…, `core/tools.py`
f60bb35e…), service active, live bubble re-verified (`--ptt status`, `--ptt
level`) and the installer now prints `NOT shipping test.py …` instead of
delivering it.


## Addendum — 2026-09-11 appearance controls, hygiene, reminders extraction

1. **The bubble got its appearance knobs.** Two new settings — `bubble_accent`
   (0–1: how hard each shape leans on its state colour, via saturation and
   glow alpha) and `animation_energy` (0.2–2.0: orbit speed, swirl speed, hue
   sweep, comet brightness and glow energy) — applied in the one shared frame
   state every design reads, so one slider moves all ten shapes instead of ten
   hand-tuned variants. Both defaults are the historical values exactly, so an
   older settings.json renders as before. They live-apply through the existing
   settings watcher (no restart), and the Appearance preview paints from the
   same two functions.
2. **One-click wallpaper matching** (`core/theme.py`, stdlib + ImageMagick only,
   no new Python dependency): the wallpaper path is found on a wallpaper line
   of the niri config, sampled to a mean colour via `magick`, and the four
   state colours are retuned for a dark (brighten + saturate) or light (deepen
   + saturate) backdrop. Detection degrades to `None` and the GUI says so; the
   explicit **Dark tuning** / **Light tuning** buttons always work.
3. **Hygiene pass.** `attic/` is out of both workflows' `py_compile` sets (it
   is a provenance archive, and its README now says so); 1.7 MB of untracked,
   gitignored `ruvector.db`/`.swarm` debris removed; and the untested
   `_MissingAudio` fallback is now driven for real in a child process with
   `core.audio` made unimportable — which immediately found a bug: the nested
   `Recorder` called `self._missing()`, which a nested class cannot inherit, so
   a partial install raised `AttributeError` instead of the intended
   `ImportError`. Fixed.
4. **Reminders extracted — the god-object split continues.** The queue's
   storage logic (parse/prune, serialized read-modify-write transactions,
   startup catch-up, and the pure due-split arithmetic) moved from `handsoff.py`
   into `core.assistant.ReminderStore` + `split_due_reminders`, with the app's
   paths, locks and writers injected; `handsoff.py` keeps thin `H.*` aliases so
   the ToolBelt `_dep()` contract and the existing tests keep working. The
   store is rebound to the *current* globals on every call, because a store that
   captures its path at construction silently writes the real `reminders.json`
   after a test redirects the module global — which is exactly what happened
   during this pass (the queue was overwritten with test entries and several
   were spoken aloud). The file was restored to its one evidenced genuine entry,
   the overwritten content was preserved at
   `reminders.json.testjunk-20260911`, and two regression pins now fail if the
   late binding is ever removed.
5. **Coverage back above the floor.** An offscreen scenario now renders all ten
   designs across the four state colours at both energy and accent extremes,
   which took the paint paths out of the dark: TOTAL 68.35% → 75%,
   `handsoff.py` 59% → 71%, `core/theme.py` 100%, 737 tests, floor 70 holds.
6. **CI failures now explain themselves.** Every suite job in `.gitlab-ci.yml`
   writes a junit report — GitLab's native *Test-summary* tab and merge-request
   test widget — uploads it `when: always`, and runs `ci/pytest_summary.py` in
   `after_script`: a digest of failing test names with their messages, in a
   log section that opens itself on failure and folds away when green. It also
   recognises this project's recurring Debian-slim signatures (missing
   `libasound.so.2`, `libgomp`, offscreen-Qt libraries, failed pip installs)
   and names the exact CI layer to extend, so the ALSA-style failure that
   broke the first pipeline is now self-diagnosing. With a masked
   `GITLAB_SUMMARY_TOKEN` it posts the same digest as a merge-request note,
   where Markdown renders — a job token cannot create notes (GitLab #464591),
   so absence of the variable is detected and skipped, never an error. The
   digest is pinned by `tests/test_ci_summary.py` (24 tests; the module
   measures 94% and is the only non-shipped code kept inside the coverage
   measurement — excluding tested code is how regressions hide).
7. **The first deploy of the appearance work exposed a packaging defect.**
   `core/theme.py` was added to the checkout and reached *nothing*:
   `install.sh` staged, switched, rolled back and hashed core modules from
   four hand-written lists, and `handsoff.py:_DEPLOY_FILES` kept a fifth
   (8 of what are now 15 deployed files). The settings GUI silently fell back
   to "no wallpaper matching", and `--ptt doctor` reported **`in-sync`** the
   whole time — the manifest only hashes files someone remembered to name.
   Both sides are now generated: the installer globs `core/*.py` for staging,
   the switch list, rollback and the manifest (with `CORE_REQUIRED` kept as an
   explicit floor that fails the stage if a hard-imported module disappears),
   and `_deployment_snapshot()` compares `_DEPLOY_FILES` ∪ the manifest's file
   set. A rehearsal now fails loudly if any checkout module is left behind.
   Regression tests pin both, driven from the checkout rather than a list:
   `test_rehearsal_deploys_every_core_module` asserts the deployed set and the
   hashed set each equal `core/*.py`, `test_installer_ships_every_core_module`
   pins the glob design (and rejects a literal `core/...` manifest entry), and
   `test_manifest_drives_the_compared_set` proves a module that exists in the
   checkout but not in the deployment is now reported as `installed-drift`.
   Deployed and verified: 15 manifest rows, `core/theme.py` present with
   matching hashes, `in-sync`, 737 tests, coverage 74.68% ≥ 70.
8. **Audit: every remaining hand-maintained list, and what was done with it.**
   The `core/theme.py` defect was one instance of a pattern, so install.sh and
   the runtime were swept for the rest.

   *Fixed:* (a) the shipped **top-level** module set was written out in seven
   separate steps (rollback, uninstall, staging, the compile gate, the prev
   save, the switch, the manifest); it is now defined once — `TOP_REQUIRED`
   (the hard-import floor) plus `is_exec()` — and everything else globs
   `*.py`, so a module beside `handsoff.py` cannot be forgotten. (b) The
   **uninstall** list is read back from the deployment manifest, so it removes
   exactly what was deployed (and a hand-edited manifest cannot escape the
   tree); the fallback is the required floor. Both paths deliberately avoid
   globbing `~/.local/bin`, a shared user directory — a regression test plants
   an unrelated `user_own_script.py` and requires it to survive. (c) The
   **rehearsal** floor now derives from the same definition, and it fails
   loudly if any checkout module is missing from the deployed set. (d) Both
   workflows' compile gates now call **`ci/compile_all.py`**, which discovers
   sources instead of enumerating them — the old pair of lists had to be kept
   in sync by hand and skipped any newly added file.

   *Guarded, because they were silently unenforced:* every `DEFAULT_SETTINGS`
   key must be referenced by `coerce_settings` (one dead key, `autostart`, is
   exempt) and must reach the settings app or be listed as intentionally
   runtime-only (five: `tool_call_times`, `whisper_device`, `confirm_seconds`,
   `streaming_tts`, `world_cooldown_min`). Adding a setting otherwise ships an
   unvalidated value, or a setting with no control, with nothing failing.

   *Checked and left alone:* the patient/`PTT_ACTIONS` command set has no second
   copy (the GUI sends the same strings and a mismatch is loud, not silent);
   `core/doctor.py`'s `__slots__` dep list is single-file with documented
   safe defaults; and the content lists (day names, terminal markers, blocked
   commands, `VOICE_CATALOG`, `hardware.SECTIONS`) are domain data rather than
   packaging, where drift means a feature gap, not a missing file.
9. **One dead setting found.** `autostart` (`settings_schema.py`) is not read
   anywhere in the runtime and is not coerced; it is exempted explicitly in the
   new guard rather than silently ignored. Removing it would rewrite users'
   `settings.json`, so it is left in place and recorded here.
10. **"Changing the bubble shape does nothing" — the real cause was that the
   Appearance tab never applied live.** The tab's own tooltip promised
   "applies immediately; the bubble repaints within seconds. No Save needed",
   but the design combo had **no change handler at all** and the sliders only
   updated their own number labels. The preview repainted instantly, so it
   *looked* like the change had been taken; the settings file was untouched
   until a separate Save click nobody knew to make. The write path itself was
   fine (driven directly, Save persisted and the bubble's live-reload applied
   it) — the missing piece was the write.
   Fix: `SettingsWindow.APPEARANCE_KEYS` (`bubble_design`, `animation_energy`,
   `bubble_accent`) now debounce a real save 400 ms after any edit — the shape
   combo, both sliders, the colour buttons, wallpaper matching and colour
   reset. A plain window load is told apart from an edit by comparing against
   the file on disk (no "loading" flag to get stuck), so merely opening the
   window writes nothing. Regression scenario
   `test_shape_change_applies_without_pressing_save` drives the real window
   offscreen and asserts the file changes with no Save click; a second scenario
   asserts that opening and closing the window leaves the file byte-identical.
11. **"On Appearance only the shapes apply" — measured, and it was true.** The
   sliders saved correctly (`animation_energy` 1.1 / `bubble_accent` 0.49 were
   on disk), but rendering a frame at slider extremes in a child process with a
   frozen clock showed the accent changing **literally zero pixels** on
   `reactor`, `droplet` and `void` and 2–35 pixels of 45796 on five more — 14
   of 20 design/slider pairs moved nothing. The cause: `bubble_accent` was
   applied only inside `_conic()`, which just `orb` and `halo` ever call; the
   other eight painters draw with `f["color"]` / `f["energy"]`, neither of
   which carried it. `animation_energy` was similarly confined to motion terms
   most designs ignore, and the idle equalizer ignored it outright.
   Fix: both sliders are folded into the shared frame dict once, in `_frame()`
   — the accent punches `f["color"]`/`f["sat"]` and exports `f["glow"]`
   (which `_conic` now reads instead of recomputing), and `anim` scales
   `f["energy"]`, which all ten painters already consume. The void also scales
   its rim/streaks by `glow`, and the equalizer's idle bars by `anim`.
   All twenty pairs now move ≥1501 pixels.
   Two honesty notes: `0.5` is **now** genuinely neutral in both directions
   (`2 * (accent - 0.5)`), so the lower half of the slider reduces accent
   instead of doing nothing — the old `1.0 + 0.35 * accent` was 1.175 at the
   default and could never go below 1.0, contradicting the tab's own "50% is
   the original look". Consequence: at defaults 7 of 10 designs are
   pixel-identical to the previous build and the other three differ by 4–99
   pixels of 45796, because the historical (pre-accent) look is what `1.0`
   restores. `tests/test_settings_gui.py`'s render scenario now asserts the
   pixel response per design rather than only that `_frame()` carries the
   numbers — the weaker assertion that let this ship.
12. **"Push to talk is broken" — it was, whenever hands-free was on.** If the
   continuous listener is running, `_on_command("toggle")` took the `else`
   branch (`elif state == IDLE and not self._handsfree`) and only interrupted,
   while `begin_listening()` called `_listener.suspend()` and returned without
   ever opening a recorder. So with hands-free on the PTT key was a **silent
   no-op with no feedback whatsoever** — and hands-free was the user's stored
   setting. Fix: a PTT press now parks the continuous listener (stop, not
   suspend — one InputStream at a time on this device) and records normally;
   `finish_listening` submits that capture and restarts the listener off the
   Qt thread once the recorder is actually released; the `and not
   self._handsfree` guard is gone. Interrupting while the bubble is speaking
   still wins. Verified live with hands-free on: press → `state=listening`,
   release → a 216064-frame submit and a spoken reply.
   Pinned by `TestPttWorksWithHandsfreeOn` (5 tests; 3 of them fail against the
   previous build, confirmed by mutation run).
13. **"What about the colours — they don't apply" — and the size slider either.**
   Two more instances of a control that looks wired but is not.
   (a) `_apply_appearance_live` writes only when one of `APPEARANCE_KEYS`
   differs from disk — that is how a form *load* is told apart from an edit.
   `colors` was not in that tuple, so an edit that moved **only** a colour
   compared equal, was read as "a load, not an edit", and returned before
   `save()`. The colour never reached `settings.json`; the bubble was fine (it
   has always rebuilt `STATE_COLORS` from `settings["colors"]` on reload, pinned
   in `tests/test_lifecycle.py`). `colors` and `bubble_size` are now in the
   compared set, and the status line names what actually moved instead of always
   claiming the shape — the old message is part of why this read as "only the
   shapes apply".
   (b) `size_slider.valueChanged` was connected to a **label update only**, so
   Bubble size never applied live either. It now schedules the same debounced
   apply as the rest of the tab.
   Both are pinned by new scenarios that fail against the previous build:
   `colour_change_applies_without_save` (asserts the hex lands on disk and the
   status names "state colours") and `size_slider_applies_without_save`. A third,
   `loading_the_form_is_still_not_an_edit`, guards the property the fix could
   have broken — a plain reload (which the 2s disk poll performs) still writes
   nothing.
14. **The recorded crash was a PortAudio teardown race, not the bubble crashing
   on its own.** `~/.local/state/handsoff/handsoff.log` records
   `Fatal Python error: Aborted … Thread-7 (_spea…) sounddevice.py line 915 in
   __init__` — the hands-free listener's recovery path calls `sd._terminate()`
   (process-global: it tears down *every* stream) after six failed mic opens,
   and the recorded abort is that reinit landing mid-playback on the `_speak`
   thread. `core/audio.py` now exposes `portaudio_in_use()` / `portaudio_busy()`
   (a counter, entered by `play_wav`), and the listener **defers** the reinit
   with a warning instead of aborting the interpreter. Pinned by
   `test_portaudio_reinit_is_guarded_while_streams_are_open` (incl. that the
   guard is wired into the reinit path) and
   `test_play_wav_holds_the_portaudio_mark`. The `_MissingAudio` partial-install
   fallback now carries the same guard as an inert no-op — the recovery path
   calls it from *inside* an `except` block, where an `AttributeError` would
   escape.
15. **Eleventh design: "Eye of Sauron"** (`sauron`). A slit-pupilled almond eye
   wreathed in flame, added to `BUBBLE_DESIGNS`, dispatched in `paintEvent`,
   and labelled "Eye of Sauron" in the combo (via a small display-name map, so
   `sauron` does not render as a bare "Sauron").
   Design decision worth recording: like `void`, this one keeps its **own**
   palette — canonical fire (deep red → orange → white-hot core) — because that
   is the design, not a palette choice. The state colour is not ignored, it is
   relocated: it tints the outer corona and the flame wisps, so the Appearance
   colour picker still visibly moves the eye. Both sliders work through the
   shared frame state added in item 11: `energy` drives the blaze, pupil flare
   and flame length, `glow` (the accent) the corona/rim punch.
   That composition was **measured, not assumed**: the first cut put a large
   tinted disc under the eye and only 38.3% of the drawn pixels were warm — the
   state-colour aura had become the subject and the fire the background. Moving
   the tint to a late, thin outer stop and adding a rim of fire took it to
   **77.7%** warm (73.9–80.3% across the energy/accent extremes). Footprint is in
   the family of the existing designs (105/104 half-extent vs orb 100/100, cube
   107/110) and clips nothing. Structural checks confirm the intended form: a
   bright iris with an 18 px dark slit on the centre row and a 76 px vertical
   dark run on the centre column — a slit pupil, not a hole.
   The render scenario is data-driven over `BUBBLE_DESIGNS`, so the new design
   is automatically covered for all four state colours at the slider extremes
   *and* by the ≥100-pixel response assertion. One hand-copied list was retired
   in the process: `test_bubble_design_accepted_and_garbage_falls_back`
   enumerated the ten names as literals and was already silently ignoring
   `sauron`; it now iterates the shipped tuple with a non-empty floor.
16. **The Eye reacts to the voice.** The pupil contracts and the fire flares
   with `level` — the same 0..1 amplitude the equalizer bars follow — measured
   at 11 → 10 → 8 → 6 → 4 near-black pupil pixels across levels 0 … 1.0, a
   17% larger flame footprint, and ~21–33k visibly changed pixels.
   Making this honest in *both* states needed a second piece: during SPEAKING
   the listener returns early (`_process_frame` drops frames while the bubble
   talks, so its own TTS cannot re-trigger the VAD) and never emits `sigLevel`.
   The equalizer therefore only *looked* voice-reactive while speaking — it was
   driven by a time-based pulse. `core/audio.py` now exposes `set_level_hook()`
   and `play_wav` reports the level of what it is actually playing: per 1024-
   sample block (~46 ms at 22050 Hz, ~21 updates/s, and the stream's own
   blocksize so no extra latency), normalised against the clip's own peak so a
   quiet reply still moves the visual and a loud one does not pin it, ending on
   a final 0. `Assistant.__init__` registers `sigLevel.emit`. The feed is
   best-effort: the hook call is wrapped, so a raising hook cannot break audio
   (pinned). Because the bubble's level then tracks its own voice, the
   equalizer's speaking bars became genuinely voice-driven as a side effect.
   Every level term in the painter is written neutral at 0 (× (1.0 - …) or
   + 0 × …), so a silent bubble renders exactly as before.
17. **A real bug this work surfaced by measuring rather than looking:** the
   pupil was drawn while the rim-of-fire **pen** was still active, so the slit
   was outlined in bright orange instead of being a clean dark pupil — and once
   the rim thickened with the voice it covered the slit entirely. Found because
   the reaction measurement returned "0 near-black pixels" instead of a
   narrowing. Fixed with an explicit `setPen(Qt.NoPen)`. Worth recording
   because it is invisible to a "does it render without crashing" test and only
   showed up when the pixels were counted.
   The pixel test to pin this went through three wrong metrics before it was a
   real guard, each failing in a different way: a cut *relative* to the row's
   peak drifted with the flare and so measured the flare (it passed with the
   narrowing deleted); a fixed pixel offset landed on background once the child
   widget was a different size; and `min/max` of all lit pixels on a row
   enclosed the dark gap outside the iris once the flames brightened, reporting
   the pupil getting *wider* under the voice. The working version uses an
   absolute cut, a central window, and only rows that cross the iris — and was
   verified to FAIL when the narrowing is removed.

## Addendum — 2026-09-09 improvement-program closure (settings schema, self-edit
## confirm, voice pinning, VRAM-aware whisper)

Outcomes of the user's gaps/improvements list (583 tests green, 2026-09-10):

### Done
1. **`settings_schema.py` — single source of truth.** `DEFAULT_SETTINGS` and the
   new `SETTINGS_VERSION` live in one module; `handsoff.py` imports it (spec-load
   fallback when loaded beside-file) and `handsoff-settings.py` lost its
   `H.DEFAULT_SETTINGS` passthrough hack. The installer ships the schema (and
   `hardware.py`) to `~/.local/bin` and hashes both in `deployment.json`; the
   installed-copy smoke test stages them.
2. **Self-edit is prompt-injection-safe.** `edit_file` on the running source
   takes a FORCED one-turn CONFIRM round-trip with a unified-diff preview in the
   offer — `command_policy: ALLOW` cannot downgrade it (DENY still wins), and
   invalid payloads (no marker / bad syntax) still get the tool's own refusal
   without a pointless user round-trip.
3. **Settings versioning + runtime backups.** Every `settings.json` write is
   version-stamped through one shared writer (`_write_settings_dict`, used by
   bubble and settings app); `_migrate_settings` stamps/handles old files,
   warns on future versions, and `version` is a meta key, not a setting.
   history/memory/reminders/settings writes keep a one-generation `.bak`.
4. **Custom piper voices must be pinned.** A non-default `PIPER_VOICE_URL`
   without `PIPER_VOICE_SHA256` is now FATAL (was warning-only); the pin
   command is printed, and `HANDSOFF_UNVERIFIED_VOICE=1` is the explicit,
   loud opt-out.
5. **VRAM-aware whisper.** New `whisper_device` setting (`auto` default):
   GPU (`cuda`/float16) only when `nvidia-smi` free VRAM fits the model's
   budget + 1 GB desktop buffer, CPU/int8 otherwise; forced `cpu`/`cuda`
   honored, and a GPU load failure degrades to CPU instead of crashing.
6. **Coverage measured honestly — then raised.** The 60% TOTAL had been
   polluted by PySide6's vendored `shibokensupport` phantom files (now
   omitted). Once `tests/test_settings_gui.py` drove the Qt GUI in offscreen
   subprocesses (with `COVERAGE_PROCESS_START` + `parallel = True` engaging
   subprocess measurement), handsoff-settings.py went 18% → 67%, TOTAL → 71%+ (659 tests),
   and the floor moved to 70.

### Monolith split — step (a) EXECUTED (2026-09-09)
7. **`core/settings.py` extracted.** The settings machinery (coercion,
   migration, cross-process flock, atomic private writes, quarantine,
   backup) lives in `core/settings.py` behind an explicit `Settings` object
   that takes every path as a parameter — core never reaches into handsoff
   globals. `handsoff.py` keeps the old module-level names as thin wrappers
   (the `H.*` monkeypatch contract and patch seams survive; the wrappers are
   late-bound so future steps can patch core directly), `_SETTINGS_OBJ` is
   the explicit object, and the installer ships + hashes `core/`. **Steps
   (b)–(e) are now EXECUTED** (2026-09-10): `core/audio.py` (343 lines),
   `core/brain.py` (174), `core/tools.py` (2661 — every one of the 49
   `@tool` declarations moved out of the monolith, which now has zero),
   `core/doctor.py` (352) and `core/lifecycle.py` (51) exist behind
   "Phase 4" compatibility facades in handsoff.py; the installer ships and
   hashes all of them. Remaining: (f) removing the shim re-exports after a
   release window, per the Phase-4 plan.
8. **ICS MONTHLY/YEARLY recurrence.** `_ics_expand_rrule` covers MONTHLY
    (nth-weekday like 2TU, BYMONTHDAY, DTSTART day-of-month fallback) and
    YEARLY (BYMONTH, BYMONTHDAY, Feb-29 skip) with UNTIL/COUNT bounds,
    pinned by `TestICSMonthlyYearly` (nth-weekday, BYMONTHDAY, BYMONTH,
    UNTIL-bound cases).

**Real bug found by the split's verification run:** announce-cooldown checks
compared against raw `time.monotonic()` (uptime-based), so a fresh
"never announced" 0.0 sentinel looked like a pre-boot announcement and
**silently suppressed urgent world/hardware warnings for the whole first
coldown window after every reboot**. Fixed via `_announce_ok()` with a
boot floor; uptime-independent pins added (the old hardware/world tests
only passed on hosts up longer than 60 minutes).

### Not done (revisited, still open)
- Unsupported/unusual Notify layouts beyond actions/hints trailers,
  Compositor interface for niri-stub testing.
  (Per-tool policy UI, installer `--rehearse` coverage, and the Settings
  History/decision-viewer tab are DONE — see the 2026-09-10 addendum above:
  dropdown policy rows in Settings, the rehearsal e2e running inside the
  suite as `TestStagedRelease`, and the History tab's Conversation /
  Durable facts / Decision log panes.)

## Addendum — 2026-09-09 trust & reliability program (P0–P2 closed)

583 tests green (2026-09-10) across the split suite (`tests/`: audio, policy, desktop,
calendar, settings, lifecycle, regression, ops).

### P0 — the running product is trustworthy
- **Deployment hash reporting** — `_deployment_snapshot()` compares running /
  installed / checkout by sha256 (mtime-independent); surfaced in the
  `health` control command, the Settings health bar (`deploy: ok|drift`),
  and the doctor. The known "installed copy compares against itself" bug is
  fixed and pinned (`TestDeploymentReporting`).
- **Installer manifest** — install.sh writes
  `~/.config/handsoff/deployment.json` (source+installed sha256 per shipped
  file) and prints a post-install `--ptt doctor` step.
- **Doctor** — `run_doctor()` / `--ptt doctor` / `handsoff_doctor` tool: one
  pass over deployment, Ollama, TTS/STT, mic visibility, niri IPC, ydotool,
  restart script, systemd `Restart=`, crash log. Works when the bubble is
  dead (client-side fallback).
- **Stale autostart snippets** — installer now says systemd owns autostart
  instead of the old confusing "remove spawn-at-startup" note.
- **Lifecycle/installed-copy tests** — offscreen launch, restart resilience,
  and a new **installed-copy smoke test**: a fake `~/.local/bin` deployment
  is booted and poked over the control socket (`TestInstalledCopySmoke`).
- **Hardware acceptance checklist** — `ACCEPTANCE.md` (Yeti, TTS echo,
  ydotool, niri IPC, systemd restart/crash-loop/teardown).
- **Runtime hardening (merged from a parallel session)** — `_prepare_runtime()`
  refuses startup when config/state paths are symlinked or not owner-private;
  `ControlServer` owns a stop event + `_remove_stale_control_socket()` that
  only ever unlinks a socket we own (never a symlink/foreign inode), and
  `control.stop()` is wired into `aboutToQuit` so a clean shutdown leaves no
  stale socket inode; the crash report now **truncates** `crash.log` instead
  of unlinking it (faulthandler keeps the fd — unlinking would orphan later
  crashes), pinned by `test_crash_report_ignores_empty_log`.

### P1 — desktop actions are reliable
(landed in the prior session, pinned by tests)
- Live niri capability manifest (`niri_capabilities`, 30 s cache).
- `wait_for_window`, post-action verification on close_window,
  open_app waits for and identifies the launched window.
- Focus verified before every typing operation (`_typing_guard`, fail-closed).
- scroll/wait tools + stale-scan rescan behavior around screen interaction.

### P1 — safer developer automation
- **Bounded jobs** — `start_command` / `job_status`: whitelisted background
  commands with max 4 jobs, 200 KB output, 30 min lifetime cap, kill on
  breach; completion announced via the same channel as watcher alerts.
  Identical refusal policy to run_command (pinned by test).
- **Exact output/status** — job_status returns state, exit code, bounded
  output tail.
- Destructive commands stay denied (BLOCKED list wins; no widening).

### P1 — centralized policy
- **ALLOW / DENY / CONFIRM** via `DecisionPolicy` + `command_policy`
  settings; DENY wins over permission switches; kill_process keeps its own
  two-step flow (not double-gated).
- **One-turn-separated confirmation** — CONFIRM offers out loud;
  `confirm_action('yes')` in a later turn runs the ORIGINAL arguments
  (loop-free via a `_confirm_running` bypass — caught by test).
- **Dry-run mode** — `dry_run: true` makes desktop-action tools report
  instead of act.
- **Decision log** — every decision (incl. DENY/rate-limit/DRY-RUN) appended
  to `~/.local/state/handsoff/decisions.jsonl` with id, ts, tool, target,
  decision, result; capped, and write failures never break tool calls.

### P2 — maintainability & release confidence
- Test file split into `tests/{conftest,audio,policy,desktop,calendar,
  settings,lifecycle,regression,ops}.py` (the monolith is gone).
- Installed-copy tests exist next to checkout tests (see P0).
- `ACCEPTANCE.md` hardware checklist added.
- Pre-commit + CI gates kept, now pointed at `tests/` and compiling the
  split modules.
- Installer, deployed files, service unit, and niri snippets remain
  single-source-of-truth (repo files shipped as-is; manifest added).

Still open, accepted consciously: real-hardware acceptance run (see
`ACCEPTANCE.md`) and richer D-Bus notification formatting. (Per-tool
`command_policy` rows, CI rehearse-style installer coverage, and the
Settings History/decision-viewer tab closed 2026-09-10 — see the top
addendum.)

## Addendum — 2026-09-09 hardening audit (post-merge review)

Edge-case audit of `_prepare_runtime` / `_remove_stale_control_socket` (24
new tests in `tests/test_hardening.py`; suite now 583 green, 2026-09-10):

- **Fixed, was a real wedge**: a stale control socket left permissive
  (e.g. created under umask 000) made `_secure_file` refuse it — and since
  `_prepare_runtime` runs at every startup, the bubble could never start
  again until manual removal. Sockets owned by us are now tightened in
  place (chmod 0600), like regular files already were.
- **Fixed, silent failure**: the doctor never looked at the control-socket
  path; a startup-blocking socket problem (symlink, foreign owner, not a
  socket) is now reported explicitly (`REFUSES STARTUP …`).
- **Fixed, short-circuit**: `all(generator)` in `_secure_runtime_files`
  stopped hardening at the first bad file; now every file is processed and
  the AND is returned.
- **Fixed**: `acquire_lock` now chmods `handsoff.lock` to 0600 explicitly
  (the pid inside is runtime state).
- **Pinned by tests**: symlink (incl. broken), directory, foreign-owned,
  regular-file-at-path refusals for dirs, files, and the socket; live
  socket bind under umask 000 self-heals; `_serve` bails out cleanly
  without binding when `_prepare_runtime` refuses.
- **Accepted consciously**: final-component-only symlink checks (parent
  components are mitigated by the resolved-target uid check); the inherent
  lstat→unlink TOCTOU window in single-user session space.
- Removed a dead pre-computed `.json.tmp` path in `_persist_setting` that
  invited reintroducing the predictable-temp-name race.

## Addendum — 2026-09-08 external audit closure

Every priority finding and most "remaining gaps" from the external audit are
now closed (583 tests green, 2026-09-10):

- Command whitelist bypass via absolute `niri` paths — closed (basename-keyed
  spawn checks; `TestSpawnInterpreterBoundary` extended).
- Cross-turn transcript reuse — closed (per-generation transcript cache).
- Keyboard injection on unknown focus — closed (fail-closed refusal).
- Post-fire spoken snooze — closed (offer cleared only after re-arm).
- Reminder read-modify-write race — closed (`REMINDERS_LOCK` transaction).
- Installer: `pacman -Syu`, single autostart owner, checksum-verified existing
  downloads, per-package python-target probing (CachyOS ships neither
  python-pyside6 nor python-sounddevice; pacman aborts the whole transaction
  on unknown targets — pip fallback), and restart of an already-active
  service so the live bubble runs the just-installed code (pinned in
  tests/test_lifecycle.py).
- Qt teardown emit — guarded; `PytestUnhandledThreadExceptionWarning` fails CI.
- README.md — written (install, permissions, troubleshooting, recovery).
- CI — `.github/workflows/ci.yml` + `pytest.ini`.
- ICS EXDATE / RECURRENCE-ID — implemented + pinned (`TestICSOverrides`).
- Dependency manifest — `requirements.txt` (installer now consumes it).
- Portable config — `niri-window-rule.kdl` no longer hardcodes a username.
- Historical patcher — moved to `attic/` with an explanatory README.
- Git baseline — repo initialized with `.gitignore` (runtime state excluded),
  CI smoke job for the installer, single-source-of-truth restart script, and a
  versioned pre-commit hook (`githooks/`, enable via
  `git config core.hooksPath githooks`) so broken self-edits can't be
  committed — pinned by `TestPrecommitHook`.

Still open, accepted consciously: real-hardware acceptance session (mic, echo,
suspend/resume), and richer D-Bus notification formatting for applications that
emit unusual Notify argument layouts. (Installer rehearsal coverage closed
2026-09-10: `install.sh --rehearsal` runs end-to-end inside the suite.)

## What already works (verified live this session)

- Voice loop: push-to-talk + hands-free VAD, whisper STT, piper TTS, streaming
  sentence-by-sentence speech, barge-in, echo rejection of its own voice
- Brain: local Ollama, tool loop (run_command whitelist, read/edit file with
  self-edit guard), model picker with capability badges, auto memory-clear on
  model switch
- Desktop control: audio/media/brightness/niri commands, virtual keyboard
  (`type_text` / `press_keys` via ydotool) into the focused window
- Ops: niri window rule (round bubble), autostart, keybinds Mod+V / Mod+Shift+V
  / Mod+Shift+H / Mod+Shift+S (settings, works even when the bubble is dead),
  restart script with lock race fixed, faulthandler crash log
- 659 tests, all green (2026-09-10)
- Ambient automation: opt-in notification reader, Pomodoro transitions, RAM/VRAM
  threshold crossings, and bounded file/process watchers; all have Settings
  controls or safe tool gates

## P0 — reliability gaps (the bubble must never be a zombie again)

1. ~~**No crash recovery.**~~ **Done** — systemd user service with
   `Restart=always` + crash-loop guard; the bubble reports its own crash on
   the next start.
2. **VRAM pressure is the likely killer.** whisper `large-v3` (~1.5 GB) + 24B
   Q4 LLM (~14 GB) exceed 16 GB together with desktop usage.
   → Default whisper back to `tiny` (or auto-select by free VRAM at load).
3. ~~**Crash evidence is invisible.**~~ **Done** — startup crash report plus
   the crash-log line in `--ptt doctor`.

## P1 — capability gaps (what users will ask for next)

4. **Window-addressed typing.** `type_text` hits whatever is focused. A
   `focus_window(app_id_substr)` tool built on `niri msg windows` would let the
   AI aim at Firefox/Slack by name ("put this in Firefox") — small step, big win.
5. **Clipboard tools.** `wl-copy` / `wl-paste` as tools: "copy that", "paste it
   into the editor" become trivial and compose with type_text.
6. **Timers & reminders.** "Remind me in 20 minutes" needs a scheduler thread +
   notify/TTS. Frequently requested assistant feature, cheap to add.
7. **Screen awareness.** A `screenshot` tool (grim) + local OCR or a
   multimodal model would answer "what's on my screen". Bigger lift.
8. **"I didn't catch that" loop.** Empty/short transcriptions are silently
   dropped. After 2 failures, the bubble should speak a recovery prompt.
9. **Read-only web lookup.** Everything is offline (by design). A dedicated
   `web_search` tool (fixed endpoint, no arbitrary curl — curl stays blocked)
   would answer weather/news within the existing safety model.

## P2 — hardening & hygiene

10. **niri spawn escape hatch.** `run_command niri msg action spawn -- <app>`
    indirectly launches anything, bypassing the whitelist. Accept consciously
    (single-user box) or constrain spawn args.
11. ~~**CI for the tests.**~~ **Done** — `.github/workflows/ci.yml` runs the
    suite (Python 3.12 + 3.13), `py_compile` on all sources, and `bash -n` on
    both scripts on every push; `pytest.ini` makes background-thread
    exceptions fail the run instead of warning.
12. ~~**Streaming path untested.**~~ **Done** — `TestStreamingChat` drives
    `ollama_chat_stream` against `tests/fake_ollama.py` (sentences, tool calls,
    and the follow-up round).
13. **Settings app additions:** manual "clear memory" button (auto-clear exists
    for model switches), a **History tab** showing what the AI currently
    remembers (demystifies "why did it say that"), and a health panel
    (crash.log tail, model/tool capability, mic device + live level).

## P3 — polish

14. Spoken confirmation before typing into an app the user didn't explicitly
    name (currently prompt-level guidance, not enforced).
15. Say the volume level back when changing it ("volume at 40 percent").
16. Optional wake word ("hey bubble") to complement hands-free VAD.
17. Multi-utterance conversations while speaking (queue follow-up questions
    instead of barge-in-only).

## Addendum — 2026-09-09 second full-project audit (concurrent-hardening review)

Scope: independent review of the hardening change set landed concurrently with the
workspace-index work (settings locking + reload, `run_command` rework,
`_quarantine_bad`, spawn hardening, installer changes, CI/pre-commit, docs). Audited
at the settled state; gates re-run on that exact tree.

**Gates:** `py_compile` + `bash -n` clean; **583 tests green (2026-09-10)**; runtime tool census
**46, unchanged** (grep over `@tool` over-counts because of the docstring example);
doctor functional. The README test count matches the final collected suite.

Findings, ranked:

1. ~~**Deployment drift (live, fix before sign-off):** the installed `~/.local/bin`
   copy predates the combined tree; `--ptt doctor` correctly reports
   `installed-drift` — the trust feature doing its job, but the deployed code is
   behind the tested source. → Re-run `./install.sh` once this change set commits.~~
   **Done 2026-09-10** — live doctor reports in-sync; the deployed copy matches
   the checkout.
2. **Installer default `-Syu` → `-Sy`:** flagged on first read as a silent
   partial-upgrade regression; on inspection it is a *documented* default
   (`-Sy` for fast installs, `HANDSOFF_FULL_UPGRADE=1` selects supported `-Syu`)
   and `test_pacman_python_targets_probed_individually` still pins `-Syu` behind
   the flag. Accepted consciously — but `-Sy` + `-u`-less installs can leave the
   system on a partial upgrade; the note in install.sh documents this.
3. **README hotkey fix verified against the machine:** the live niri config binds
   `Mod+Shift+H` for hands-free (`Mod+H` is niri's own `focus-column-left`), so the
   old doc line was actively wrong. Docs now match config.
4. **Spawn hardening is thorough:** interpreter/terminal-argument bypass routes,
   absolute-path `niri` lookalikes (basename-keyed checks), and blocked basenames
   inside arguments are all closed; boundary tests extended accordingly.
5. **Policy layer survives the rework:** the P1 ALLOW/DENY/CONFIRM
   `DecisionPolicy`, dry-run reporting, one-turn confirmations, and the
   id/tool/target/decision/result decision log are all intact after the
   `run_command`/jobs rewrite.
6. **Infra fixes real and pinned:** restart script exact-pid wait, curl timeouts in
   calendar/web fetch paths, `pytest.ini` `testpaths`, installed scripts deployed
   executable (fixes a real PermissionError), CI rehearse job kept.
7. **Hygiene:** no secret-shaped strings in the diff (grep hits are concurrency
   tokens and token-budget state); earlier installer fixes (per-package python
   probes, restart-if-active) survived the rework; stale "400+ tests" doc counts
   updated.

18. **Every design reacts to the voice, from one shared signal.** The Eye
   reacting was not enough, and "reacts to the voice" has to hold whichever
   design is picked. Measured per design with the clock frozen, only six
   painters read `level` at all (halo, reactor, droplet, equalizer, void,
   sauron) and the equalizer faked its reactivity with a local
   `sin(t * 6.0 + i * 1.3)` pulse — so `orb`, `bloom`, `cube`, `crystal` and
   `saturn` changed **zero** pixels under the voice. The fix folds `level` into
   `f["energy"]` once in `_frame()` (gain exactly `1.0` at level 0, so silence
   renders as before), which every painter already consumes; and the equalizer's
   active bars now take their amplitude from `level` with a fixed per-bar
   shape instead of a timer. All eleven designs move when the voice moves: orb
   6701 px, halo 3731, reactor 7483, bloom 9816, droplet 6945, cube 8244,
   equalizer 2962, crystal 8457, saturn 704, void 1665, sauron 8136 (of 45796).
   New scenario `every_design_reacts_to_voice_level` asserts it per design and
   was verified to fail on the unfixed tree — listing exactly `orb, bloom, cube,
   crystal, saturn = 0`. Two traps were hit while writing it, both worth
   recording: (a) with the radius left live, the listening state's
   `_radius_target` grows the whole bubble with the level, which changes >100 px
   on *every* design and made the assertion pass on the broken code — it now
   pins `_radius_ui`; (b) the shared lift fought the Eye's *bespoke* reaction
   (its pupil width grows with `blaze`, so a voice-lifted `energy` widened the
   pupil while the `lv` term narrowed it, collapsing the measured narrowing
   from `11 -> 4` to `4 -> 2`), so `_frame()` also exports `energy0` — the same
   value **without** the voice — and the Eye draws from that.
   The Eye's own voice test was also a latent flake, not a regression: it never
   stopped the 16 ms timer, so `grab()` ran `_on_tick` between setting
   `_level_ui` and painting it and the smoothed level decayed toward
   `_level_target`; its `>= 3 px` narrowing floor was really asserting the test
   widget's radius and passed only because of that decay. It now stops the
   timer, pins the target, and asserts the property that matters — the pupil
   only ever narrows as the level rises (verified to fail when the `lv`
   narrowing is removed, which reported `[4, 4, 5]`).
19. **Twelfth design: Pikachu** (`pikachu`). A round yellow face — black-tipped
   ears, red cheeks, glinting eyes, a mouth that opens with the voice — added to
   `BUBBLE_DESIGNS`, dispatched in `paintEvent`, and selected as "Pikachu" in the
   Appearance combo. Like `void` and `sauron` it keeps its own palette (that IS
   the design), and relocates the state colour to an aura ring behind the head
   so the colour picker still visibly moves it; `energy` drives the aura, the
   ear sway and the breathing, `glow` (the accent) the aura and cheek bloom, and
   `level` charges the cheeks while the ears prick up.
   Two things were found by measuring instead of trusting the code:
   (a) *The bubble window carries an inscribed-ellipse mask*
   (`setMask(QRegion(rect, Ellipse))` in `showEvent`), so anything painted past
   it is invisible on the desktop. The first cut of the ears reached 1.5 R and
   leaked **818 px** outside the mask in the LISTENING state (where the radius
   grows by `12 * GEOM_K`, leaving only ~1.12 R of headroom). The ear tips were
   retuned into that budget and now leak 0 px across every state/level/anim
   combination; while measuring, `droplet` (251 px) and `sauron` (305 px) were
   found to overflow in the same state — pre-existing, left alone, and recorded
   here rather than silently "fixed" by shrinking art the user has seen.
   (b) *`grab()` is not a valid instrument for a translucent widget.* The first
   mask measurement reported 1753 px of overflow on `bloom`; rendering onto an
   explicitly cleared transparent image showed 0. `WA_TranslucentBackground`
   means the widget never erases its background, so a fresh `QPixmap` can hold
   stale pixels that look exactly like artwork. The overflow sweep was rewritten
   to `QPainter`-render onto a cleared `QImage` and to read alpha — that is why
   the numbers above can be trusted and the earlier ones could not.
   Pikachu's composition was measured the same way (idle: 4864 drawn px, 43%
   opaque, 0 outside the mask; the aura ring is capped to a `0.44 * width`
   circle that is inside the mask for every radius).
20. **The Voice tab can see the feed it is diagnosing.** `Assistant` now owns
   the single level publisher (`_emit_level(value, source)`), which all three
   producers call instead of `sigLevel.emit` directly — the listener's VAD
   (`mic`), the push-to-talk recorder (`ptt`) and `core.audio`'s playback hook
   (`tts`) — and `level_snapshot()` answers a new control-socket `level` command
   with `raw`, the smoothed `ui` value the painters read, `source`, `age_s`,
   `state` and `handsfree`. Settings → Voice draws that as a meter plus a text
   readout.
   Why both numbers: `raw` alone cannot distinguish "the mic never fed the
   level" from "the feed arrived but the bubble is not showing it", which is
   exactly the user-visible complaint the shared-level work was about. `age_s`
   only advances on a **non-zero** level, so a wedged producer emitting exact
   zeros is visible rather than looking freshly alive.
   The polling runs on its own daemon thread (`_LevelFeed`) and only while the
   Voice tab is on screen: a synchronous socket read would freeze the window,
   and `doctor`/`health` hold the control server's single-threaded accept loop
   for seconds, so `level` is deliberately answered inline while those two stay
   wrapped in `_with_timeout`. The bubble module stays lazily loaded (the feed
   takes `lambda: H.CONTROL_SOCK`), and `PTT_ACTIONS` grew `level` — which the
   existing `test_ptt_actions_documented_in_usage` guard immediately caught, so
   the `USAGE` text documents it too.
   Two guards, both verified to fail on the mutated code: the control-socket
   test (removing the `level` branch ⇒ `JSONDecodeError`) and the GUI scenario
   (folding the meter's `ui` into `raw` ⇒ `AssertionError`), the latter driving
   the real `SettingsWindow` against a real unix socket and asserting the
   command actually sent is `level`. Routing the listener through
   `_emit_level` also broke four `TestListenerSelfMute` tests and one playback
   test, because `FakeAssistant` did not carry the new surface — the fake was
   completed (it records the source AND forwards to `sigLevel`, so the existing
   visual feed is pinned too) rather than the production call being made
   defensive. One robustness change did come out of that thought: the emit is
   wrapped, because both callers that matter run on threads whose death is
   silent and expensive (the PortAudio callback, the TTS playback thread) and a
   slot raising must not travel back into them — the same contract
   `core.audio._emit_level` already had.

21. **Each design owns its voice reaction.** Item 18 made every design react to
   the voice by folding `level` into `f["energy"]` once — a uniform brightness
   gain applied to all of them. That was the quick way to make the property
   true, and it flattened exactly what makes the shapes worth having: with one
   gain, twelve designs react identically, so the bubble loses its identity when
   it is most alive. The shared lift is gone; `_frame()` still publishes `level`
   and each painter now writes its own term:

   | design | what the voice does | changed px |
   |---|---|---|
   | orb | ripple rings spread through the glass ring, silhouette shivers, hotspot + rim arc catch it | 1351 |
   | halo | torus thickens outward, comet wave and bead speed up, trailing bead appears, core flares | 4240 |
   | reactor | the three segments open wider, tick ring extends, core flares, spin accelerates | 4396 |
   | bloom | petals (blob wobble) open, mist brightens, extra sparks and a faster swirl | 7463 |
   | droplet | fast skin ripple over the slow swell, drip detaches sooner, rim + streak brighten | 5226 |
   | cube | a flash front sweeps the six facets, edges and spokes flare | 6316 |
   | equalizer | the bars *are* the meter; centre dot pulses | 2273 |
   | crystal | inner hex swells and rotates off-phase, its hue splits away from the body (refraction), glints lengthen | 6328 |
   | saturn | ring thickens, a brightness wave travels it, crescent widens, moon pulled faster | 1205 |
   | void | infall streaks brighten, sparks sweep inward faster, horizon flicker rises 13 Hz → ~24 Hz | 1395 |
   | sauron | pupil contracts, fire flares (item 16, unchanged) | 5977 |
   | pikachu | cheeks charge, ears prick, mouth opens (item 19, unchanged) | 4996 |

   The reactions are also distinct in *where* they act, not just how much: the
   share of the change falling inside the inner half of the bubble runs from 21%
   (void — an outward event horizon) to 70% (saturn — ring and core).
   Two invariants were measured, not assumed:
   *Neutrality*: with the level pinned at 0, all twelve designs render
   **pixel-identical** to the previous tree (sha256 of the rendered buffer per
   design, compared against the pre-change source). Every new term multiplies by
   `1.0` or adds `0` at level 0 — writing the crystal's refracted pen colour
   directly broke this (the resting gem took the state hue) and the comparison
   caught it; it now blends *from* white.
   *Footprint*: these are the first voice terms that add alpha to glows that
   already reach the window's round mask, so the mask sweep was re-run. Bloom,
   cube and crystal went from 0 to 461/175/313 px outside the mask — all of it
   faint (max alpha 32–38 of 255, none above 64) and all of it *hidden by the
   mask*, but a glow ending on the cut at 22% opacity is a visible seam waiting
   to happen, so each of those three now pulls its gradient tail inward by
   `1.0 - 0.20 * level` (exactly 1.0 at silence, so the resting glow is
   untouched). All three are back to 0 outside-mask at every state/level/anim
   combination; the only remaining overflow is the pre-existing droplet
   (251 → 254 px) and saturn (6 → 8 px). Bloom's extra sparks are also capped at
   0.95 R so they cannot sit on the cut.
   The guard is `every_design_reacts_to_voice_level`, extended with the crisp
   half of this regression: `_frame()` at level 0 and at level 1 must produce
   the same `energy`, so re-introducing *any* shared lift fails immediately
   (verified: it reports "the voice has leaked back into the shared energy
   channel"). Together with the existing ≥100 px per design assertion that
   forces every painter to own a term — reconstructed the intermediate state
   (shared lift removed, no bespoke terms) and the scenario names exactly the
   five designs that relied on it: `orb=7, bloom=0, cube=0, crystal=0, saturn=0`.

Accepted consciously / out of scope here: Settings health-bar pixel verification on
the dual-monitor setup (data-level verified in `ACCEPTANCE.md`), unsupported/unusual
D-Bus Notify layouts, and the six human-only acceptance items. Process note: files
churned mid-audit while the other agent worked; all findings above were confirmed
against the settled tree.

## Addendum — 2026-09-12 fifth-review-batch audit (job cap, theme maths, offer atomicity, self-mute)

A seven-item list was checked against the tree before any edit: **six real and
fixed, one disproved by measurement (for the second time).** Every fix is pinned
by a guard that was verified to fail on the mutated code — 8/8 in this batch.

**(1) The background-job cap could be overshot.** `start_command` checked
`len(self._jobs) >= MAX_JOBS` under `_job_lock`, released the lock across
`_validate_command` + `Popen` (slow on purpose — fork/exec must not run under
the lock), then re-acquired it and inserted **without re-checking**. Measured
with eight threads parked inside that window by a `threading.Barrier`: **7 jobs
started against a cap of 4.** The cap is now enforced inside the critical
section that mutates the dict, and the surplus process — already spawned but
never registered, so nothing would ever poll, reap or kill it — is terminated,
reaped (SIGTERM, then SIGKILL with a wait) and has its stdout closed, because an
unread pipe with no drain thread attached wedges the child on write instead of
letting the signal land. The same window carried a second, quieter defect: the
`_on_restart_pending()` callback fired *before* admission, so a job refused at
the cap still left the bubble believing a restart was in flight; it now fires
after insertion, and the guard asserts the callback count equals the number of
jobs actually admitted.

**(2) `core.theme.hex_to_rgb` prefix-matched.** `re.match(r"^#?([0-9a-fA-F]{6})")`
is unanchored at the end: `'#4f8cffXYZ'` returned `(79, 140, 255)` instead of
`None`, and an 8-digit `#RRGGBBAA` was silently **truncated to its first six**,
so a malformed or alpha colour validated as a good 6-digit one and lost its
alpha without a word. Now `fullmatch` with exactly six digits — the convention
`theme.py` already used twelve lines below for ImageMagick output.

**(3) `tune_colors_for_background(colors, None)` raised.** `float(luminance)` was
unguarded while `detect_wallpaper_luminance` returns `None` on **every** failure
path (no config, no wallpaper file, no ImageMagick, unparseable output), and the
function's own docstring promises "a bad detection can never corrupt the user's
palette". Reproduced: `TypeError: float() argument must be … not 'NoneType'`. The
GUI caller guards it, so the defect was latent — but the contract was the
function's, not the caller's to uphold. `None` and junk are now a no-op.

**(4) `coerce_bool_arg(2)` was `True`.** The numeric branch was `bool(raw)`, so
any number at all passed as a flag the model never asked for. Only `0`/`1` are
meaningful; anything else now raises like the other junk shapes.

**(5) The kill offer could be read half-armed.** Arming is `clear()` then
`update()` — two steps on a module-global — while `confirm_kill` copied the dict
and then cleared it. A dict *copy* is atomic under the GIL, but it is the
clear/update *pair* that is not: a reader landing between them saw an **empty**
dict and answered "nothing to confirm" for an offer that exists, or paired one
call's pid with another's deadline. Both sides now share `_KILL_LOCK`, the same
shape `_snooze_offer` already used. Pinned deterministically with a dict
subclass that holds the arm window open, rather than by hoping a stress test
lands in it.

**(6) The self-mute matched substrings.** `notification_muted` muted any
notification whose summary or body merely *contained* the app name, so an app
named `assist` muted "assistant manager update". It now matches on word
boundaries (regex-escaped, so a name full of regex characters stays literal),
while our own popups — `handsoff: started`, `Handsoff says` — still self-mute.

**(7) The ydotool probe — disproved again, with a second harness.** The finding
claims `connect()` on a SOCK_DGRAM unix socket succeeds with no server, so a
dead daemon reads as reachable. Measured with the daemon as a real **exited
child process** (the case the claim describes): a stale socket inode returns
**`ECONNREFUSED (111)` on both DGRAM and STREAM**, so the probe cannot report a
dead daemon as alive. What is true is narrower and already documented: a daemon
that is alive but *wedged* connects successfully, because the probe is a connect
and not a round-trip — and that case still fails loudly at use time, when
`ydotool` itself times out with `ERROR: ydotool timed out`. Closing it would
take a real protocol round-trip; a `/proc`-inode check was already measured to
break a root-owned daemon, so the bounds are stated rather than traded for a
false negative.

Verified: 944 tests green in six orderings (default, shuffled-test seeds
1/424242/deadbeef, shuffled-file seeds 2/cafef00d), coverage **78.02% ≥ 70**,
`ci/compile_all.py` clean (38 files), and `core/theme.py` at 100%.

## Addendum — 2026-09-12 admission control: one owner for every cap and offer

The fifth-review batch fixed the job cap by re-checking the limit inside the
lock that inserts. That was correct, and it was also the fifth time the same
shape had been fixed in a different registry — so the next registry to grow a
cap would have grown the same defect. The shape:

    check the cap while holding the lock  ->  release the lock  ->  do the
    slow thing that creates the resource (fork/exec, spawn a thread, resolve
    a path)  ->  insert WITHOUT re-checking.

`core/registry.py` now owns it. Two primitives, and nothing else in the tree
enforces a capacity or arms an offer.

### `BoundedRegistry` — the cap is enforced on ADMISSION

`reserve()` checks the cap while holding the registry's own lock and counts a
reservation against it immediately; the slow prepare runs holding that
reservation; `commit()` is the only way an entry appears. A second caller is
therefore refused rather than told there is room the first is about to take —
the "check, release, act, insert" window does not exist to be mis-implemented.
There is no caller-visible lock to release at the wrong moment, because there
is no caller-visible lock.

Two consequences worth naming:

* **The surplus-process case is gone by construction.** `start_command` never
  spawns a process it cannot admit, so `_abandon_job` (SIGTERM → SIGKILL →
  close stdout, on a Popen nothing would ever poll, reap or kill) had no case
  left to handle and is deleted. The old test asserted that recovery worked;
  the new one asserts **Popen is called exactly MAX_JOBS times** for eight
  racing callers — a stronger statement of the same property.
* **A failed prepare cannot shrink the cap.** A reservation is a context
  manager: leaving the block without committing cancels it, so a `Popen` that
  cannot exec leaves the registry exactly as it found it. A leaked reservation
  would be silent — the registry counting a job that does not exist, refusing
  work while `job_status` listed nothing to reap.

Keys are minted inside the lock that inserts (`commit(build)` hands the key to
a factory), replacing `self._job_seq += 1` under a lock every caller had to
remember to take.

All three caps are now `BoundedRegistry`: background jobs (`MAX_JOBS`), and the
file and process watchers (4 each). The two watcher registries **share one
lock** on purpose — `stop_watchers()` clears both as a single step, so a
watcher cannot be admitted between the two clears and outlive the shutdown.

### `Offer` — armed, read and consumed in one place

The kill offer's `clear()` + `update()` pair was fixed with a lock in the same
batch. That lock protected one dict. `Offer` protects the shape:

* `arm(window, **fields)` is ONE assignment under its own lock, so a reader can
  never see a half-armed offer — the bug that prompted the lock, now true by
  construction rather than by every future caller remembering.
* `state()` / `consume()` apply the deadline themselves. No call site can
  forget it: `state()` returns `(offer, expired)` and clears a stale offer, so
  even a caller that only checks for `None` cannot act on a closed window.
* `consume()` is the **claim**: the second of two racing confirmations gets
  `None` and must refuse.
* `arm_unless(predicate, ...)` arms only when no live offer matches. A model
  looping on the same call cannot keep pushing its own confirmation deadline
  out, and the read-then-maybe-write pair is one step, so two callers cannot
  both decide to arm.
* The `bool`/`len`/`[]`/`in`/`keys`/`get` surface is read-only observability,
  and reads hand out **copies**. There is deliberately no mutator, so the old
  bare dict — convenient *and* unsafe, because every caller could arm it — has
  no equivalent.

All three offers are `Offer` objects: the kill confirmation, the snooze window,
and the CONFIRM-class tool offer (`ToolBelt._pending_confirm`).

Truncating bounders are deliberately NOT routed through it: reminders keep the
newest 64, the notification coalescer keeps the newest 64 distinct texts, and
memory keeps 24 facts. Those evict, they never refuse, so they are not
admission decisions and sharing the helper would only blur what it means.

### What the refactor found on the way

* **An expired CONFIRM offer could not be re-armed.** The old code decided
  "is this the same call?" by reading the offer and comparing, then armed only
  when it was different; an offer whose window had closed still carried its
  `tool` and `turn`, so a repeat of the same call compared as "still waiting"
  and the offer was left expired. The user's next `yes` was answered with
  "the offer expired". The stale offer is now cleared and re-armed as part of
  that same step, which is what the log line always claimed.
* **`_dep()` resolves per-thread.** It is `_CURRENT` in the calling thread but
  `_DEFAULT_DEPS` in a NEW thread (a thread starts with an empty context). With
  one monolith those are the same object; with two loaded — the settings-GUI
  suite loads a second — a test that arms an offer from a worker thread and
  reads it from the main thread exercises two different offers, and passes or
  fails depending on which monolith imported last. `tests/conftest.py:pin_offer`
  installs one offer on every path. The shuffled order found it; the default
  order had not.

Verified: 966 tests green in six orderings (default, shuffled-test seeds
1/424242/deadbeef, shuffled-file seeds 2/cafef00d), the coverage gate green at
**78.41% ≥ 70** (`--cov=. --cov-config=.coveragerc` with the offscreen-GUI
subprocesses measured, exactly as CI runs it) with `core/registry.py` at
**100%**, `ci/compile_all.py` clean (40 files), and **22/22** mutations of the
new guards back to their old behaviour caught.

## Addendum — 2026-09-12 what a cap refusal leaves behind, and a soak that proves the guard

Three requests in one batch: make a refused job visible after the fact, soak the
caps and offers beyond the barrier-pinned cases, and stop the Appearance tab
accepting colours the parser will later discard. Auditing the suite for the
second and third also turned up two ways the suite had been touching things it
should not: **it loaded a real whisper model during one test and wrote the
developer's real state file during another.**

### 1. A cap refusal is now evidence, not a sentence in a chat reply

The job cap was fixed structurally (admission in `BoundedRegistry`), but the
*event* of a refusal still existed only in the string handed back to the model:
not in the journal, not in the state, not in `--ptt doctor`. That is exactly
backwards — a cap turning real work away is the one in-the-wild signal that the
bubble met its own limits, and the overshoot bug it belongs to had only ever
been found by reading the source, never by anything the running bubble said.

* `BoundedRegistry` now counts refusals and remembers **the shape of the
  moment** (cap, how many held, how many reserved, which occupants, when). The
  count lives there because that is the only place that knows the decision was
  made — so a future registry cannot forget to report one.
* `_refused_at_cap` shouts at WARNING and hands the report to the host, which
  keeps a bounded ring in `~/.local/state/handsoff/cap-refusals.json`
  (`CAP_EVENTS_MAX = 50`, with a cumulative `count` and per-registry totals that
  survive ring eviction, and `_atomic_private_write` 0600 like every other
  state file).
* `job_status` and `--ptt doctor` (text *and* `doctor_json`) render from one
  formatter, so three surfaces cannot describe the same refusals differently.
  The doctor line is printed even when there is nothing to report — "none
  recorded" is a positive finding, not a missing line.
* Every step is best-effort: reporting must never be a reason a refusal takes a
  different path.

### 2. A soak that drives the caps and offers under real overlap

The barrier-pinned tests prove ONE interleaving is safe; they say nothing about
the schedules a running bubble sees. `TestConcurrencySoak` drives genuine
overlapping traffic — six job threads against a cap of four, six
`kill_process`/`confirm_kill` threads, four threads racing one offer's claim, and
a sampler — for a bounded 1.2 s, and asserts only invariants:

* every `start_command` attempt is started **or** refused, and the refusals the
  callers saw equal the registry's own count (the cap is neither overshot nor
  silently dropping work);
* no job id is ever issued twice (keys are minted inside the inserting lock);
* a pid armed by a claim racer is claimed **at most once** — arming and
  consuming in one loop would *not* catch a `consume()` that fails to clear,
  because the counts move together, so claim contention needs separate armer and
  consumer traffic.

The first version of this test missed two of four mutations, which is how the
loop-shape problem above was found. After the fix it catches 3/4 — and the
fourth is not a gap in the test: a `clear()`-then-`update()` arm is invisible
through the public API (`state()` reports an empty offer as *nothing armed*, not
as a partial one), so no black-box test can see it. The deterministic test that
holds the offer's clock open is the instrument that pins that mechanism. That is
stated in the soak's own docstring rather than left as a false sense of
coverage.

### 3. Two ways the suite was leaving the sandbox (found by running it a lot)

* **A real 3 GB whisper load during one test.** `_whisper_model` was found
  holding a live `faster_whisper.transcribe.WhisperModel` for every test after
  `test_settings_gui` — which made `--ptt doctor`'s own test read
  `stt: whisper loaded`. Traced with a teardown hook on thread names: a
  `stop-probe` daemon spawned by `submit_audio` outlived
  `test_fault_injection.py::TestMicReturnsNothing::test_a_good_press_still_reaches_the_brain`
  (which stubs nothing), finished after its own monkeypatch was reverted, and
  published the loaded model into the shared mirror. Fixed at the source (that
  class's `_bare` stubs the probe, which is not what it tests), made structural
  (`stop-probe` and `loader` joined `_WORKER_THREADS`, so a leaked model-loading
  thread now fails its own test), and made harmless (`_whisper_model`,
  `_tts_model`, `TTS_REFERENCE`, `TTS_ENGINE`, `WHISPER_SIZE`, `WHISPER_DEVICE`
  joined `_STATE_GLOBALS`; the snapshot restores non-containers by identity, so a
  model is never deep-copied).
* **The suite was writing the developer's real state file.** One full run grew
  `~/.local/state/handsoff/cap-refusals.json` by four entries, all synthetic
  (`start_command 'echo x'`), written by a refusal raised in `test_ops` but
  *recorded* through a foreign host: `core.tools._DEFAULT_DEPS` is process-wide
  and is replaced by whichever monolith wires it last, and the settings-app
  suites load a second monolith **with the real HOME** (the session fixture
  isolates HOME only for the import of `handsoff_core`). conftest now **pins**
  the tool DI host to the test's own monolith — `_CURRENT` for the calling
  thread and `_DEFAULT_DEPS` for worker threads, which start with an empty
  context — instead of only restoring the previous value. `_core_doctor._CURRENT`
  is deliberately not pinned: it holds a DoctorDeps, not a tool host. Verified by
  before/after counts across a full default run, the coverage run and all six
  orderings: 136 → 136.

### 4. The briefing tests were paying DuckDuckGo

Three briefing tests call `_maybe_briefing_prefix`, which fetches live world
headlines. Measured on the same tree, same day: **20.5 s when the endpoint
throttled, 0.4 s when it did not** — the suite's runtime was a fact about a third
party, which is also why an earlier "why is the suite 45 s slower" question had
a network answer. All three now stub `_world_events`; the class took 0.87 s
after, and the live fetch is not what any of them assert.

### 5. The Appearance tab refuses colours the parser would discard

`coerce_settings` filters state colours only for *type*, so a hand-edited
`"blue"`, `"#4f8cffXYZ"` or `"#12345"` survived the load, was drawn on the
swatch as though it were live, and was then silently dropped by
`theme.hex_to_rgb` — the GUI said one thing and the bubble did another. Admission
now uses the parser's own predicate: a malformed value is replaced by its default
and named out loud in the status bar (`rejected 3 malformed state colour(s)
(idle='blue', …) — a colour must be #rrggbb; using the default`), a valid
`#`-less colour is normalised to the form the swatch's stylesheet can render, and
the colour dialog's result is checked too so nothing can enter `_colors` that the
parser would throw away. `self.cfg` is deliberately *not* mutated at load (it
would then disagree with `_loaded_cfg` and make a plain reload look like an
unsaved edit); `_collect` — which always wrote the palette — carries the cleaned
value into the file on the next save. Pinned by a new offscreen scenario and
**5/5** mutations of the new guards back to the old behaviour caught.

## Addendum — 2026-09-12 the last two caps enforced by hand (reader slot, diagnostic worker)

The previous addendum centralised admission control in `core/registry.py` and said
plainly what was left: the notification reader's thread slot and the control
server's diagnostic-worker cap were still decided by hand. Both are now on the
same primitive, and nothing in the project enforces a capacity itself.

### The shape they were left in

    if self._thread is not None and self._thread.is_alive():   # check
        return "already on"
    proc = self._popen_factory(...)                            # slow prepare
    self._thread = self._spawn(self.run, ...)                  # insert

Two overlapping enables both read a free slot and both started a reader — two
`dbus-monitor` processes, two loops, one of them holding the other's stop event,
every notification spoken twice. The diagnostic cap had the identical shape
(`prev is not None and prev.is_alive()` then spawn), which is why a repeated
`health` against a wedged Ollama piled up threads that can never be cancelled.
Both are now `BoundedRegistry(name, 1)`.

### `reserve(reclaim=...)` — the third shape of the same question

A singleton whose occupant can *die* needs a third answer: "is it still alive?"
and "may I take its slot?" must be ONE step, or two callers both see a corpse and
both start a worker. That predicate is evaluated under the lock that hands the
slot out; a judged-dead occupant leaves the registry and is returned on the
reservation (`.reclaimed`) for the caller to dispose of **outside** the lock —
`core/registry.py` still never touches a process or a thread itself. A predicate
that raises leaves the occupant exactly where it was: the registry is the last
place that should quietly delete something a caller asked about, and a failed
`reserve` is not a refusal (neither is counted).

The reader keeps its slot until its worker is actually gone, which is a fix and
not just a refactor: `off` used to clear `_thread` *before* the join, so a restart
arriving during an unresponsive worker's shutdown could still start a second
reader beside the old one. A worker that outlives its join budget now keeps the
slot, and the reclaim predicate frees it the moment it dies.

### Measured, not assumed

* the reader: two enables released together with the spawn held open started
  **1** reader (before: 2), and the loser is answered "already on" — the
  documented reply, deliberately NOT shouted about as a cap refusal, because a
  repeated toggle is not a cap turning work away (the registry still counts it);
* the diagnostic cap: **8 racing callers → 1 worker, 1 result, 7 refused by
  name**, each refusal durable (`cap refusals: diagnostic-worker`) and logged;
* the refusal sentence is now rendered by the registry (`refusal_line`), so the
  journal, `core/tools.py`'s reply path and the doctor cannot drift into three
  accounts of one event — the existing `"cap refusal: job at 4/4 held"` assertion
  is what pinned that the wording survived the move;
* **13/13 mutations of the new guards back to the old behaviour were caught**,
  including "give the slot back before the slow part" (the original defect),
  "reclaim a live worker", "free the slot before the worker is gone", "never
  dispose of the corpse", "drop the diagnostic record", and "ignore the
  reservation when counting the cap".

988 tests green in six orderings, coverage 78.26 % (≥ 70), `ci/compile_all.py`
clean (40 files), deployed `in-sync` (three-way TRUE, 15/15 files) with
`--ptt health` answering through the new `_diagnostic_call`.

## Addendum — 2026-09-12 a refusal the user can hear (spoken cap refusals)

The refusal record was the previous step's answer to "a refusal is invisible":
counted in the registry, journaled at WARNING, kept in `cap-refusals.json`,
rendered by `job_status` and `--ptt doctor`. All four are things a person has to
go and *read*. The refusal itself happened because something was ASKED for — a
command to run, a file to watch, a health keybind — and did not happen, so the
bubble now says so on the channel job completions, reminders and hands-free
confirmations already use (`Assistant._announce_now`).

### Where the decision lives

`Assistant.announce_cap_refusal(report, detail)` owns the wording and the rate
limit; the belt only knows a cap turned work away. Two call sites feed it:

* `ToolBelt._refused_at_cap` (jobs, file watchers, process watchers) hands the
  refusal to a new `on_cap_refusal` hook — the same seam as `on_announce`, and a
  belt built without a host (tests, embedding) logs exactly as before;
* `ControlServer._diagnostic_call` calls it directly for the diagnostic cap,
  which the user provokes by pressing a health keybind.

### Not once per refusal

The refusal path is retried by nature: a model re-calling `start_command`, a
keybind being hammered, a wedged backend being polled. An audio loop is worse
than the invisibility this replaces, so the announcement is rate-limited like
the reader's per-app cooldown — and, crucially, the *stamp is what was said*,
not the latest attempt: the first refusal at a cap speaks in full ("I couldn't do
that: the background-job limit is full (4 of 4). Nothing was started."), and when
the cooldown has passed the cap speaks again with the delta the cooldown
swallowed ("… 6 more requests turned away since I last said so."). Per registry,
so a full job cap cannot silence a wedged diagnostic; per instance, so it cannot
outlive the bubble; bounded (`CAP_ANNOUNCE_MAX`), so a new registry name cannot
grow the map; never raising, because announcing must not become a reason a
refusal takes a different path.

### Measured

* unit: first refusal speaks with the label and counts; six in a row are one
  sentence; the delayed sentence carries the delta (and "1 more request", not
  "1 requests"); each cap speaks for itself; a closed bubble stays silent; junk
  reports are neither spoken nor raised; a broken speaker is survivable; the map
  is bounded and keeps the NEWEST caps;
* integration, through the real `ControlServer`: **8 racing diagnostics → 1
  worker, 7 refusals, 7 durable records, exactly ONE spoken line**;
* the seam itself: the app's own belt must carry the hook (a mutation removing
  the wiring would otherwise leave the whole suite green);
* **10/10 mutations of the new guards back to the old behaviour were caught.**

### Two things this found in the suite

Adding `announce` to conftest's `_WORKER_THREADS` guard exposed a real leak that
had nothing to do with refusals: `test_handsfree_status_roundtrip` sent
`handsfree-status` over the socket and never processed the Qt event loop, so the
confirmation was delivered during whichever test next called `processEvents()`
— where it *really* spoke, starting a TTS worker for a test that had already
finished. The test now delivers and asserts its own command, and the leak guard
(which had blamed an unrelated test) is honest. Second: an announcement worker
waits up to 30 s on the models-ready event, so a lingering one is a leak with a
fuse — which is why it is a named worker rather than an anonymous thread.

1000 tests green in six orderings (default, shuffled-test seeds
1/424242/deadbeef, shuffled-file seeds 2/cafef00d), coverage 78.38 % (≥ 70),
`ci/compile_all.py` clean (40 files), deployed `in-sync` (15/15 files, three-way
TRUE). A *live* audible refusal needs a stalled backend (the diagnostic cap only
refuses while a previous worker is still running, and `run_doctor` caches its
Ollama probe), so the live claim is the deployment check plus the control-server
integration test rather than a sentence on a healthy bubble.

---

## Accept-loop admission: the last hand-rolled cap

`ControlServer.start()` decided this by hand — `if self._thread is not None and
self._thread.is_alive(): return`, then spawn, then assign the attribute. Two
overlapping `start()` calls both read "no live thread", both cleared the stop
event and both bound a server: the loser's `_remove_stale_control_socket()` +
`bind()` replaced the winner's socket, leaving one accept loop serving an inode
no client could reach — `--ptt` said "not running" while the bubble believed it
was reachable.

The accept loop is now a singleton slot in the same `BoundedRegistry` every
other cap uses. `reserve(..., reclaim=...)` is evaluated under the lock that
hands the slot out, so "the old loop is gone" and "may I have its slot?" are one
decision instead of two reads with a spawn between them, and `_thread` is a view
onto the registered slot rather than a second source of truth. A loop that died
(failed bind, refused runtime) is reclaimed by that same predicate, so "already
running" can never be read off a thread that is gone.

`stop()` gives the slot back only once the loop is actually gone. Freeing it
before the join is how a restart gets a second acceptor beside the old one — the
orphan this class exists to avoid — so a loop that outlived its join budget
keeps the slot and the predicate frees it when it really dies. A repeated
`start()` on a live loop stays the documented no-op it always was: refused by
admission, deliberately not shouted about as a cap refusal (nothing the user
asked for was turned away), though the registry counts it like the reader's
"already on".

### Measured

* eight callers racing `start()` with the spawn held open: **one loop started,
one `control` thread alive, one slot occupied** — on the old check-then-act all
eight spawn;
* a loop that died gets its slot back; a live one keeps it through `stop()`;
* **4/4 mutations caught** — the old check-then-act `start()`, the cap raised to
  8, the reclaim predicate removed, and `stop()` freeing the slot without
  checking.

### A leak the guard then found

Adding `control` to conftest's `_WORKER_THREADS` guard failed four fixtures that
started a real `ControlServer` and never stopped it. The accept loop is a named
worker with a stop path, so each left a live idle thread (and a bound socket)
behind: test_lifecycle's `server` fixture, the local copies in test_audio and
test_settings, and `_health_query`'s real-server test now stop before the socket
path is restored. Same shape as the announce worker the previous batch exposed —
the guard is what keeps a named worker honest.

1016 tests green in seven orderings (default, shuffled-test seeds
1/424242/deadbeef/20260913, shuffled-file seeds 2/cafef00d/7), coverage 78.26 %
(≥ 70), `ci/compile_all.py` clean (41 files), deployed `in-sync` (15/15 files,
three-way TRUE, `--ptt status`/`health` answering). Nothing committed.

---

## HOME isolation: the load no loader can wrap

The sandbox belongs to the LOAD (`conftest._load` enters `isolated_user_dirs`,
and the autouse fixtures load the bubble before any test body runs). Everything
in-process that could resolve the developer's CONFIG_DIR/STATE_DIR went through
that one door — except an **import statement**. A module-scope import runs while
pytest is collecting the test module, so it resolves with the developer's HOME,
and nothing in the load complains: the module works, it is just pointed at the
real config. It is also machine-dependent, because it only shows up where real
config exists.

The four modules that bake user paths at import are now *discovered* from the
source rather than listed (`core/tools.py`'s CONFIG_DIR/STATE_DIR, 
`core/audio.py`'s WHISPER_MODEL_DIR, the bubble, the settings app), and three
guards close the door:

* **no collection-time import** of a discovered module — `from core.tools
  import X`, `from core import tools` and `import core.tools` all resolve to the
  same banned name, and a rename that emptied the discovered set fails the
  guard instead of making it vacuous;
* **the loader refuses to hand back a real-home module**: after `exec_module`,
  `_load` checks the module's own path constants and raises naming every one
  that points into the developer's home. The failure lands on the load, not on
  whichever test later compares paths — or on the developer's disk;
* **a sweep of the live process**: whatever a test loaded and however it loaded
  it, no app module alive in `sys.modules` may hold a path constant under the
  developer's home. This is the assertion the request is actually about, and it
  found two real leaks no structural guard could see.

### What the sweep found

**The settings app exec'd a second bubble.** `_LazyHandsoff` exec's `handsoff.py`
on first attribute use — a moment no loader wraps, because the caller decides
when — and a second bubble is not a second view of one app: it is a second app
with its own CONFIG_DIR/SETTINGS, and its module body calls
`core.audio.configure(...)`, which repointed the SHARED core.audio at that
copy's `WHISPER_MODEL_DIR` (the developer's real one). Two causes, one fix each:
`_import_handsoff()` now returns a bubble already registered in this process
(the name the bubble hashes itself under, which is also the name an embedder
registers it under), and conftest hands the session's registration back around
every test, because two extraction tests deliberately empty `handsoff*` from
`sys.modules` to prove `core/doctor` imports without the monolith — and nothing
put it back. Only the shuffled order caught it: in collection order the eviction
happened before the app's first lazy load, so the key was still there.

### The incident this pass caused, and the guard that now prevents it

One mutation in the sweep removed the sandbox from `_load`, and the write-
through test — which proves a write through a loaded module lands in the
sandbox — wrote where the un-sandboxed load pointed it: **the developer's real
`~/.config/niri/config.kdl`**, 24 bytes of `// written by the suite`. The include
files (keybinds, rules, monitor) were untouched. Recovered from the newest
`nimod` backup and validated with `niri validate` (`config is valid`); the
damaged file is kept as `config.kdl.written-by-suite`. Two changes follow from
it: the test now **refuses to write** when the target is inside the real home
(checking after the write is one write too late — the assertion that noticed was
the report, not the safety catch), and the loader's post-exec refusal means an
unsandboxed load fails before any test body can act on the module.

### Measured

* **6/6 mutations of the isolation caught**, including the one that caused the
  incident — and the real niri config's mtime is byte-for-byte unchanged across
  a sweep that runs it (the safety catch, measured rather than asserted);
* 18 sandbox guards, among them: XDG pinned for the in-process half as well as
  the child env, `run_driver`'s child proving its own HOME is a temp dir, a
  loaded app not exec'ing a second bubble, and the live-process sweep.

1022 tests green in seven orderings (default, shuffled-test seeds
1/424242/deadbeef/20260913, shuffled-file seeds 2/cafef00d/7), coverage 78.21 %
(≥ 70), `ci/compile_all.py` clean (41 files), deployed `in-sync` (15/15 files,
three-way TRUE, `--ptt status`/`health` answering). The shipped change is
`handsoff-settings.py` (a no-op in production, where no process has loaded a
bubble before the app starts). Nothing committed.

## Addendum — 2026-09-13 appearance: the mask, the wallpaper and the black design

Three complaints, reported twice: *the bubble breaks if you make it bigger*, *if
you keep it bigger and swap shapes the design mismatches*, and *colours never
apply when you click "match wallpaper" — they look hardcoded for pikachu and the
Eye of Sauron*. Measured before editing, one cause each; **both designs the user
named do read the palette**, which the fourth item below settles with numbers.

**(1) The mask never followed the widget.** `BubbleWidget.showEvent` set
`setMask(QRegion(self.rect(), QRegion.Ellipse))` — a QRegion mask is in WIDGET
coordinates and Qt keeps it exactly as it was, so the aperture stayed the size
the bubble was born at. Measured: **mask 128×128 while the widget was 192×192**.
That is one cause for both reports: a size change drew the new, scaled design
through the old circle ("bigger breaks it"), and at the larger size every
shape's own scaled geometry was clipped by the same stale smaller ellipse
("swap shapes and it mismatches"). `_apply_mask()` is now called from
`resizeEvent` and `showEvent`, so the aperture is always the current rect; the
live settings reload (`setFixedSize`) goes through the same path. Pinned by
`resizing_the_bubble_keeps_its_aperture`, which grows AND shrinks and also
asserts the mask is still an ellipse (a fix that widened the aperture to the
rectangle would pass the size assertions).

**(2) "Match wallpaper" could never do anything here.** The button consulted the
niri config alone, and a niri config names a wallpaper only when something like
`swaybg` is spawned from it — this desktop's wallpaper is a shell-owned VIDEO in
a cache directory, so `detect_wallpaper_luminance` returned `None` **every
time** and the click was a no-op with a message blaming ImageMagick, which was
installed all along. Discovery now covers what the desktop actually uses: the
niri config (quoted and bare forms), the shell's `wallpaper.directory` setting,
its `large`/`thumbnails` caches, the `swayg` per-output cache and
`hyprpaper.conf`, newest-first, deduplicated, and a **video is sampled by
extracting a frame a second in** (frame 0 of a looped wallpaper is a fade-in to
black, whose "average colour" would tune the palette for a black backdrop).
`wallpaper_report()` returns the path, the luminance and every location tried,
and the status line now names the file it sampled or says what it looked at, so
"nothing happened" and "it read the wrong wallpaper" cannot look the same. Pinned
by five `TestWallpaperDiscovery` tests plus the GUI scenario.

**(3) One design really was colour-blind, and it was not one of the two named.**
Rendering every design offscreen with everything frozen at idle — state, level,
radius, clock — and changing ONLY `self._color_ui` (the colour `_frame()`
publishes as `f["color"]`), then counting pixels that differ by a visible step
(≥ 48 in some channel): **void reported 8 pixels of 45 796** — the black disc is
deliberately black and its only state-coloured elements were ~2 px spiral streaks
at 22/255 and a hairline rim at 40/255. Picking a colour was, in effect,
disabled on that design while working on every other one (next weakest:
equalizer, 804). Void now draws the same broad state-coloured halo the orb uses
for its own dark body: **8 → 1856 px**, mean delta on the band 8.4 → 148.2. The
same instrument measured `pikachu` at **4296** and `sauron` at **2104** visible
pixels *before* any change — they are not hardcoded, and the alpha floors I first
raised on their auras measured as no-ops (annulus mean delta +0.2 on pikachu), so
those speculative edits were **reverted rather than shipped**. Pinned by
`every_design_shows_the_state_colour`; the bar is 400, deliberately between the
broken measurement (8) and the least-visible working design (~800).

**(4) The Appearance tab refuses colours the bubble would discard.**
`coerce_settings` filters state colours only for type, so a hand-edited `"blue"`,
`"#4f8cffXYZ"` or `"#12345"` was drawn on the swatch as though live and then
silently dropped by the parser — the bubble kept the old colour while the GUI
said otherwise. Admission now uses `hex_to_rgb` itself, replaces a rejected value
with its default and says so in the status bar, normalises a `#`-less but valid
colour for the swatch, and checks the dialog's own result; `self.cfg` is
deliberately not mutated at load so a plain reload cannot look like an unsaved
edit.

Two smaller defects were fixed with them: `hex_to_rgb` prefix-matched, so
`'#4f8cffXYZ'` returned `(79,140,255)` and an 8-digit `#RRGGBBAA` was silently
truncated to its first six (now `fullmatch`), and
`tune_colors_for_background(colors, None)` raised `TypeError` while
`detect_wallpaper_luminance` returns `None` on **every** failure path — breaking
that function's own documented promise (now a no-op).

**10/10 mutations back to the old behaviour are caught**: the check-then-act mask,
a rectangular mask, no halo on void, the niri-config-only detector, no video
frame extraction, prefix `hex_to_rgb`, the raising `None` path, a report that
forgets which file it used, and the two colour-admission reversions. 1035 tests
green in seven orderings (default, shuffled-test seeds 1/424242/deadbeef/
20260913, shuffled-file seeds 2/cafef00d/7), coverage **78.56 % ≥ 70** with
`core/theme.py` back at **100 %** after the new discovery branches were covered
(rather than the claim being lowered), `ci/compile_all.py` clean (41 files),
deployed `in-sync`. Nothing committed.

## Addendum — 2026-09-13 one app per process: the canonical module name

The app had no identity. Every loader in the tree invented a name of its own —
`conftest` registered the bubble as `handsoff_core` **and** a bare `handsoff`
alias, `handsoff-settings.py` spec-loaded its own copy under `handsoff_core`, the
offscreen GUI driver used `handsoff_core_gui`, the hardening driver
`handsoff_no_audio` — and the *running* bubble (`python handsoff.py`) was
`__main__`, reachable under no name at all. So "is the app already loaded in this
process?" had no answer anything could ask, and each of those paths could exec a
**second app**: its own CONFIG_DIR/STATE_DIR/SETTINGS and model mirrors, and a
module body that calls `core.audio.configure(...)` — which repoints the SHARED
`core.audio` at whichever copy loaded last. A previous batch fixed one instance
of this (the settings app's lazy loader) by caching under the very name
`conftest` happened to use; that was a coincidence holding the line, not a rule.

### One name, owned by `core`

`core.APP_MODULE_NAME` ("handsoff_core") is the single definition — `core` already
owns module identity (`_origin_ok`, `load_module`) — and `handsoff.py` imports it
rather than respelling it. `handsoff.py` then **registers itself** in
`_claim_app_name()`, at the earliest point the name is available: immediately
after the `core` package loads and *before* anything the body can do to shared
state (the earliest such call, `_audio.configure(...)`, is ~1000 lines below). It
refuses in the two cases that would otherwise run two apps quietly:

* **unnamed** — `module_from_spec(...)` + `exec_module(...)` with no `sys.modules`
  entry executes the app into a namespace nothing can see, which cannot be told
  apart from a duplicate, so the loader is required to name it first;
* **a second copy** — the canonical name already holds a different live module.
  Refused by name, instead of being discovered later as a repointed `core.audio`
  or a diverged SETTINGS.

Two records of one fact back this: the canonical `sys.modules` entry (what every
loader looks up) and an out-of-band `core._APP_INSTANCE`. The second is not
belt-and-braces for its own sake — the suite's extraction tests pop `handsoff*`
from `sys.modules` on purpose, and a lazy loader exec'ing a second bubble through
exactly that window is a bug this repo has already had once.

### One loader, in `core`

`core.load_app_module(candidates)` replaces every hand-built load:

* an app that is already running is **returned, never re-exec'd**;
* the slot is claimed with `sys.modules.setdefault` **before** the module
  executes, so the module finds itself in `sys.modules` (which is what lets the
  app refuse an unnamed load) and two racing loaders execute at most one copy —
  the loser receives the winner's module having exec'd nothing;
* a load that raises **gives the slot back**, so a failed exec cannot leave a
  half-initialised app for the next caller;
* a foreign occupant is refused using the same origin rule as `load_module`.

`__app_ready__`, set as the last statement of `handsoff.py`'s body, is what makes
"already running" mean *finished*: `core.app_module()` hands out only a whole app,
so a caller cannot read `SETTINGS` off a module that is still executing.

### What went with it

* The **bare `handsoff` alias is retired** (`conftest._APP_MODULE_NAMES` is one
  name). Two names for one app is the ambiguity being removed, and a stray
  `import handsoff` now fails loudly rather than silently satisfying itself from
  an alias. `core.doctor`'s "must not import the monolith" test was asserting on
  that bare name, i.e. it would have passed while looking at nothing; it now
  asserts the canonical one.
* `conftest._load` **reuses** the running app for `handsoff.py` instead of
  overwriting the canonical entry — the overwrite would have defeated the whole
  guarantee while still looking like a load.
* The offscreen GUI driver and the hardening driver **name their modules before
  executing them** (the hardening driver keeps a local name on purpose: it wants
  one app in a child, not the deployed identity).
* The settings app's `_import_settings_schema` now goes through
  `core.load_module` like every other support module. Loading it by hand under a
  private name gave that process **two schema dicts** whenever the bubble was
  loaded first — the same defect shape, one size smaller.

### Verified

**9/9 mutations back to the old behaviour are caught**, each at behaviour level
(where two mechanisms guarantee one property, the mutation removes both rather
than pretending to test a layer): a second copy allowed to run, an unnamed load
allowed, the app not publishing its own name, the bare alias restored, the
instance record dropped, the loader exec'ing even when an app is running, a failed
exec not returning the slot, the settings app building the module by hand again,
and the test loader exec'ing the app again. 1042 tests green in seven orderings
(default, shuffled-test seeds 1/424242/deadbeef/20260913, shuffled-file seeds
2/cafef00d/7), coverage **78.57 % ≥ 70**, `ci/compile_all.py` clean (41 files),
deployed `in-sync`. The production path is covered rather than assumed: one guard
child does a plain `import handsoff` and asserts the canonical registration, the
instance record, `__app_ready__` and `app_module()`; another starts the real
bubble as a script (`__main__`) and talks to it over the control socket. Nothing
committed.

## Addendum — named Appearance looks, and the thirteen design (2026-09-13)

The Appearance tab had one control per attribute and no way to say "make it look
like *this*". The ask was a **Look** picker beside the shape picker, plus one more
design; this is what shipped, and what the measurements said about it.

### What a look is — and what it deliberately is not

* **A look is data over the five keys the tab already owns** (`settings_schema.APPEARANCE_LOOKS`:
  design, window size, animation energy, colour accent, the four state colours).
  Clicking one sets those five controls and goes out through the *same* debounced
  live-apply path a slider drag uses, so it reaches `settings.json` and the running
  bubble with no Save and no second apply mechanism. `_collect()`/`save()` were not
  touched.
* **There is no `appearance_look` setting.** A stored name living beside the values
  it describes is a second source of truth that can disagree with them — the GUI
  reading "Neon" while the bubble renders Midnight — which is the failure shape this
  tree has spent weeks deleting. The current look is **derived** by
  `look_matching()` from the five values, so it cannot lie: one nudge of any slider
  flips the section to "Custom" and unticks the button, and the status line names a
  look only when the saved values land exactly on one (`_apply_appearance_live`
  derives it, so a hand-tuned set that happens to match also reports itself).
* **The catalogue must survive the loader.** A look that writes a value coercion
  would replace is a one-click no-op, so `TestAppearanceLooks` validates every entry
  against the loader's own bounds *and* round-trips each one through
  `_load_settings()`, asserting the loaded values still read back as that look.
  Look names are unique, looks are pairwise distinct, and the shipped defaults are
  exactly the first look — so a fresh install shows a look rather than Custom.
* **Eight looks** ship: Handsoff (the defaults), Midnight, Daylight, Ember, Neon,
  All-seeing (the Eye), Spark (Pikachu) and Curious (the cat). The two mascot looks
  keep the canonical palettes those designs are *for*; their tooltips say the state
  colour tints the aura/corona rather than repainting the character, and the render
  guard measures only what is true of them.

### The cat

* A round, **palette-driven** head with ears, whiskers, eyes and a rising tail — no
  canonical colours to fight the picker (the property `void` lacked at 8 visible
  pixels, and the one the user asked for by name). Measured: a state-colour swap
  moves **8011** pixels past a visible step.
* **One voice scalar.** Ears splay, the tail swings wider and lifts, the eyes
  narrow, the inner-ear glow and the halo brighten — all from `_cat_reach(lv, t, anim)`,
  which is 0 at rest and never above 1. That bound is what makes the mask possible.
* **Its own silhouette.** It is the first design painted outside the inset ellipse,
  so `design_region(name, w, h)` replaced the one-ellipse rule in `_apply_mask`, and
  a look/design switch re-applies the aperture **at paint time** as well: a live
  shape change keeps the widget's size, and `setFixedSize` on an unchanged size emits
  no `resizeEvent`, so the size alone was never a sufficient trigger.
* **Measured, not assumed.** Ear and tail geometry is defined once
  (`_cat_ears`/`_cat_tail`) and **shared** by the painter and the mask builder. Two
  real defects came out of the containment sweep (480 renders across 4 states, 5
  levels, 3 radii, 8 clocks): the halo measured **66.6 px against a 64 px mask** at
  the listening peak (fixed by capping it with the window, the same budget Pikachu's
  aura uses), and the mask rasterised from integer polygons shaved the outermost
  anti-aliased pixel of a round cap (fixed by building the mask's strokes a couple of
  pixels wider than the painter's — a mask may keep more than the painter lays down,
  never less). Worst case outside the mask now: **0 px**, and no ink on the window
  border. Rejected as out-of-scope if a design ever needs to reach the corners: the
  window would clip it there anyway (the diagonal budget is ~1.6 r, the axes ~1.14 r).

### Also fixed, found by the new guard

`BubblePreview._glyph` falls through to the orb for any design it does not know, and
**sauron and pikachu were already falling through** — the preview had been drawing an
orb beside a combo that said "Eye of Sauron". All three now have their own glyph, and
`every_design_has_its_own_preview_glyph` fails on a duplicate or a fall-through. The
same trap exists in the bubble's own dispatch, so
`bubble_designs_render_at_energy_extremes` now asserts every name in
`BUBBLE_DESIGNS` renders differently from the orb.

### Verified

**14/14 mutations caught** — the design dropped from the catalogue while a look still
names it, an unparseable look colour, `look_matching` ignoring size, `look_matching`
matching by design alone, a click that applies four of five controls, a click that
sets the widgets but never applies them, a section that keeps its tick after a hand
edit, a mask that ignores the design, a `design_region` that adds nothing, a mask
that misses the animation's extremes, the halo cap removed, a cat that ignores the
voice, the cat's paint branch removed (falls through to the orb), and a preview
missing its glyph. 1051 tests green in seven orderings (default, shuffled-test seeds
1/424242/deadbeef/20260913, shuffled-file seeds 2/cafef00d/7), coverage
**79.65 % ≥ 70**, `ci/compile_all.py` clean (41 files).

## Addendum — drawn Appearance looks (2026-09-13)

The Look picker shipped as text buttons: a name, a tooltip, and nothing that showed
what the name meant. A picker that asks you to choose a LOOK while showing you words is
the same complaint this tab keeps producing — "I clicked it and nothing changed" is
hard to tell apart from "it applied something I cannot see". Each look is now a
`LookTile`: a checkable QPushButton whose face is painted.

### The glyph comes from ONE painter

The tile's silhouette is drawn by `paint_design_glyph`, lifted to module level out of
`BubblePreview._glyph` and now called by both widgets. That is the whole point of the
move: a tile with its own copy of the drawing code could advertise a shape the
Appearance strip — and the bubble — would not draw, which is precisely the class of
lie the previous addendum found in the preview (sauron and pikachu falling through to
the orb). The scenario therefore does not compare pixels against a hard-coded
expectation; it swaps `paint_design_glyph` for a sentinel and asserts every tile drew
the sentinel, in catalogue order, with its own design name. A private copy fails that
loop by leaving it empty.

### Every state colour has to be readable on the face

The instrument is the one the bubble-side guard uses: render the tile, change ONLY one
of the four state colours, and count pixels that differ by a visible step. Idle rides
in the glyph; the other three are dots under it. Measured floors per look: idle
223–732 px, and exactly 32 px for each of the three dots — floors of 100 and 20 sit
safely below the narrowest case without being decorative.

The same instrument caught a real defect in the `allseeing` tile, and it was a defect
of the *shared* painter rather than the tile: the Eye glyph was wreathed in its own
fire palette, so the look's idle colour measured **4 px** — the state colour was
invisible on the tile that claims it. Against `void`'s 8 px, that is the identical bug
one widget up. The sauron branch now draws an explicit state-coloured corona under the
flames; fire stays fire, and the colour rides the corona. Measured 337 px after.

### One timer, not eight

Animation is what makes a look read as a look rather than as a snapshot, but eight
tiles must not mean eight 25 Hz timers in the settings process. The strip owns one
`QElapsedTimer` and one `QTimer` created against the group, so it dies with the
section; every tile reads the shared clock. The guard asserts no tile owns a QTimer
child at all and that the strip's timer is running at the tile's interval — a
mutation that gives each tile its own timer, or that never starts the shared one, is
caught.

### Verified

**21/21 mutations caught** — the seven new ones are: a tile that denies the design it
stands for (hard-coded orb), the three state dots removed, the sauron corona removed
(state colour invisible again), a per-tile timer, a never-started strip timer, the
look demoted back to a plain text button, and the tile no longer being the thing you
click. The fourteen earlier mutations in this sweep still pass after the painter move,
including the one whose anchor had to be re-pointed because the glyph left the class
body. 1052 tests green in seven orderings (default, shuffled-test seeds
1/424242/deadbeef/20260913, shuffled-file seeds 2/cafef00d/7), coverage
**79.73 % ≥ 70**, `ci/compile_all.py` clean (41 files); deployed `in-sync`
(15/15 files, service restarted, `--ptt doctor` reporting `deployment: in-sync` and
`appearance: look Custom (pikachu, 134 px)`, installed
`handsoff-settings.py --selftest` rc 0).

## Addendum — the aperture budget: no design paints on the cut (2026-09-13)

`design_region` is what decides which painted pixels survive on the desktop: the
inscribed ellipse of the square window, plus whatever a design declares of its own (the
cat's ears). Ink past it is ink Qt cuts off, so a shape that is meant to be pointed or
round gets a flat edge exactly there. This ledger has carried that as a known open
defect since the voice-reaction batch — "the only remaining overflow is the pre-existing
droplet (251 → 254 px) and saturn (6 → 8 px)" — and it was larger than that note said.

### What was crossing, measured

One instrument, one sweep (every design × 4 clocks × 4 states × 2 levels × 4 radii,
rendered offscreen with no mask applied, so what is counted is what the mask would
cut). A pixel counts as ink at alpha ≥ 16 of 255, which is a visible step; the residue
antialiasing leaves behind at the rim is ≤ 4.

| design | px outside the rim | worst alpha | what was crossing |
|---|---|---|---|
| `sauron` | 523 | 255 | the flame tips (a round cap extends half the pen past the point it is drawn to, so clamping the point alone still left ink out) |
| `droplet` | 391 | 254 | the teardrop's point through the top and its drip through the bottom, on every frame above silence |
| `bloom` | 2 508 | 19 | the tail of the mist gradient |
| `cube` | 1 396 | 19 | the halo |
| `crystal` | 1 396 | 20 | the halo |
| `saturn` | 16 | 200 | the moon's disc — a white dot sliced flat by the mask |

The three faint ones are the same shape of defect as the loud ones: a gradient whose
outer stops are reached INSIDE the disc it fills never actually fades to zero at the
edge, so "alpha 0 at the boundary" was never true of the pixels between. Clamped, the
fade completes inside the glass.

### One budget, not six constants

`APERTURE_R = WINDOW_PX / 2.0 - 1.0` — the inscribed circle, one pixel in from the true
edge so an antialiased pixel sitting exactly on the boundary is not half outside — is
now the single number every design's outermost reach is measured against. The droplet
is fitted to it with one uniform scale factor (so it is still a teardrop, just one that
stops at the rim rather than being cut by it); the halo designs cap their gradient
radius; saturn caps its moon's orbit; the eye's flame tips clamp to the budget minus
half their own pen width.

### Two clamps measured as no-ops and were NOT shipped

The project's rule is that an edit which moves nothing gets reverted rather than kept
as decoration, so two candidate clamps were removed again after measuring:

* **Pikachu's cheek bloom.** Clamping its reach measured max alpha 11 where it crossed
  the rim — below the bar at which ink is visible. The unfixed expression is back.
* **Saturn's ring.** At 1.02 R it never reaches the glass at any size or level the state
  machine can ask for, so its clamp could not be pinned by any mutation. It is gone; the
  moon's cap (16 px at alpha 200) is what remains.

A third decision went the same way in the other direction: the detaching drip is
deliberately **excluded** from the droplet's fit. Folding it in was tried first and it
shrank the whole droplet (measured fit 0.51 instead of 0.62 at the phase where the drip
is furthest along) to make room for a drip that is already outside the window by then —
its drawn centre reaches y = 136.9 in a 128 px window while it still has alpha, and on
the vertical axis "outside the mask" IS "outside the window rect", so Qt has clipped it
before the mask could. A fine 5 ms sweep of the whole drip cycle at five radii measured
**zero** pixels of any alpha outside the rim with the drip excluded.

### The guard

`every_design_keeps_ink_inside_its_aperture` (tests/test_settings_gui.py) walks all
thirteen designs, four clocks, four states, both level extremes and all four radii the
state machine can ask for, and fails naming the design, the frame and the worst alpha if
any pixel at alpha ≥ 16 lands outside that design's OWN region. It also asserts the
budget itself stays inside the glass (a budget set to the mask edge is a budget that
measures nothing), and pins the droplet's silhouette — height at least 1.2× width, and a
reach floor of 55 px — so a fit that squashed its height into a ball, or that collapsed
it to fit the drip, fails as loudly as one that overflows.

### Verified

**10/10 mutations caught**, each one a revert of a decision above: the droplet drawn to
its natural reach again; the drip folded back into the budget; the fit squashing the
height; an over-clamped fit (0.5); the mist, the cube's halo and the crystal's halo back
past the glass; saturn's moon on its old orbit; the eye's flame tips reaching out again;
and the budget itself set to the glass edge. 1054 tests green in three orderings run
(default, shuffled-test seed 20260913, shuffled-file seed 7), coverage **79.90 % ≥ 70**,
`ci/compile_all.py` clean (41 files); deployed `in-sync` (`--ptt doctor`:
`deployment: in-sync`, running sha256 `8011d3f5…` = checkout).

## Addendum — core's loader failure paths, actually covered (2026-09-13)

`core/__init__.py` was the worst-covered file in the tree at **65%** (54 of 156
statements missed), and every one of those statements was in a *failure* path. That is
the wrong file to leave untested: it is the code whose bugs are "two apps in one
process", "the shared `core.audio` repointed at a second copy's paths", "a
half-initialised app handed out to the next caller". The percentage was never the
point — the unexercised branches were the guarantees.

### What was untested

Nothing exotic; every refusal and every fallback:

* the canonical app name already held by a **foreign** module (and by one that is
  **still initialising**), and the `setdefault` race where two loaders admit exactly one
  copy — the loser must hand back the winner's module;
* a load that **raises mid-exec** giving its slot back, and a candidate the interpreter
  cannot build a spec for;
* `load_module`'s two **foreign-submodule refusals** (at entry, and at the install point
  where a concurrent import could otherwise be overwritten) and its failed-load restore
  — the bare name and the `core.<name>` entry put back exactly as they were;
* the `__import__` fallback, the candidate dedupe, a stat that fails being *skipped*
  rather than treated as absence, and a HOME that cannot be resolved;
* `_repo_root`'s `HANDSOFF_SOURCE_PATH` branch, `_allowed_dirs`' unresolvable-path
  fallbacks, and `_origin_ok` answering **No** rather than raising.

### How it is tested now

`TestLoaderFailurePaths` (tests/test_sandbox.py) adds 19 tests. Two decisions make them
worth anything:

* **The state each branch guards is handed to the test.** The suite runs with the app
  loaded, so the `no_app` fixture removes the registration and the instance record for
  the duration and puts both back — the same rule the autouse registration fixture
  applies, for the same reason (a test that evicted the app and left it evicted is how a
  second bubble got built in an earlier session).
* **The races are forced, not hoped for.** `module_from_spec` is intercepted so the
  competing loader wins at exactly the window the docstring claims is closed. Two
  branches that are otherwise unreachable — restoring a `core.<name>` that a concurrent
  import installed mid-load, and refusing to swap one planted in the same window — are
  pinned this way.

One guard needed sharpening: `test_no_test_builds_a_module_by_hand` flagged **any**
reference to `module_from_spec`, which would have made interception look like a
hand-built load. It now flags the *pair* (`module_from_spec` together with
`exec_module` in one file) — a test may intercept the function, it may not build a
module and run it. Verified against a probe file that does exactly the banned thing:
caught (`test_handbuilt_probe.py:7`).

### Verified

**18/18 mutations caught**, one per guard removed: the foreign-name and
still-initialising refusals, the slot release, the race winner, both spec checks, both
foreign-submodule refusals, the bare-name adoption, the stat-error fallback, the
candidate dedupe, the `__import__` fallback, both failed-load restores, the
`HANDSOFF_SOURCE_PATH` branch, `_origin_ok`, the origin-directory fallback and the bin
candidate's. `core/__init__.py` is **100%** (156/156, was 65%); suite total
**80.32% ≥ 70** (was 79.90%); 1073 tests green in default and shuffled-test order (seed
20260913); `ci/compile_all.py` clean (41 files). The dedupe mutation is pinned by
*observing* the filesystem — the repeated candidate is stat'd exactly once — rather than
by the line being executed.

## Addendum — the bubble is its own module now (2026-09-13)

`BubbleWidget`, its mask geometry and the state palette moved out of `handsoff.py`
into `core/bubble.py` (1 800 lines), which completes the extraction this project has
been running in slices. `handsoff.py` went from 6 689 to 3 863 measured statements; the
monolith keeps the application, the module keeps what can be rendered and measured on
its own.

### Ownership, because that is what was actually broken

Three attempts at "the appearance does not apply" were fixed at the symptom (the mask
was re-applied on resize; the live path was made to rebuild the palette; the designs
were made to read the level signal). The underlying defect was ownership: the window
size and the geometry derived from it were written from **two** places — module
constants at import, and the live settings path inside `Assistant` — so a size change
could leave `APERTURE_R` sized for the previous window while the widget resized.

The module now owns all of it (size, `BUBBLE_R0`, `GLOW_PAD`, `GEOM_K`, `APERTURE_R`,
`STATE_COLORS`, `BUBBLE_ACCENT`, `ANIM_ENERGY`) and `configure()` is the single place
the appearance is derived from settings. Both the cold start and the live save come
through it, which is the point: they cannot disagree. The live path's duplicate
geometry block was deleted rather than moved.

### The host is injected, not reached for

`core/bubble.py` is application-free, the same shape as `core/audio.py` and
`core/tools.py`: it takes `SETTINGS`, `APP_NAME`, `SETTINGS_APP`, `RESTART_SCRIPT` and
`notify` by injection right after the app loads it, and declares placeholders so it
stays importable — and measurable — on its own. The context menu's paths and its
notifier are the host's, not the module's.

A partial install is handled the way `_MissingAudio` already was: a `_MissingBubble`
fallback carries inert defaults so the app still imports, reports and fails loudly on
use, instead of dying at import. `install.sh`'s `CORE_REQUIRED` floor gained `bubble`,
and the installer rehearsal already proves every project core module reaches the
deployed set.

### The guards, and what each one would catch

Five guards in `TestTheBubbleModuleOwnsTheAppearance`, all but one read the source
rather than the running app, because the failure modes are structural:

* **application-free** — no import of the app, and the injectable seams must still
  exist. A module that imports `handsoff` cannot be loaded alone; a seam that
  disappears leaves the host nothing to bind.
* **the host really arrives** — `SETTINGS is` the app's dict (a *copy* is the silent
  version of this bug: every colour and size the bubble reads would be stale),
  `notify is` the app's notifier (an un-injected one is inert, so bubble errors go
  unsaid).
* **no second copy in the app** — no assignment to any appearance name and no
  `BubbleWidget` class anywhere in `handsoff.py`, with the partial-install stub as the
  single, bounded exemption (asserted to sit inside the loader's `except ImportError`
  branch).
* **the app may only bind the host** — every `Store` on `_core_bubble` must be one of
  the five injected names. Writing geometry or a look knob from the app is how the
  second writer comes back.
* **the installer floor is derived, not remembered** — the core modules `handsoff.py`
  requires are read out of its own `from core import …` / `_load_module(…)` uses and
  every one must be in `CORE_REQUIRED`. This is the drift the project has already paid
  for twice (`core/theme.py` shipped nowhere while doctor said `in-sync`; the list was
  hand-maintained in seven places), and it now fails with the missing module's name.

### Verified

**10/10 mutations caught**, one per decision: a constant returning to the app, the
widget class returning, the app writing a look knob onto the module, the notifier never
injected, `SETTINGS` copied instead of shared, the module importing the app, an
injected seam removed, the partial-install stub moved out of its `except ImportError`
branch, `bubble` dropped from the installer floor, and a newly required core module
added without telling the floor (that last one verifies the guard *derives* the list
rather than repeating it).

The 16 test-side failures the extraction left behind are cleared at the cause: five
`play_wav` sites patched by *string* now name `core.audio.play_wav` (where the bubble
actually plays), the no-audio driver reads `_resample_to_16k` through the app's handle
to it, and the appearance tests reach the module that owns the state instead of an app
handle. Three of those repoints were subtler than a rename and are worth recording: the
menu guard patched `QMenu` on the app while the menu is built in the module; the
quit-ordering test read `handsoff.py` for text that now lives in `core/bubble.py`; and
the slider-visibility scenario set the two look knobs on the *app*, so every design
looked like it ignored both sliders while the painters read their own untouched
defaults.

**Coverage is unchanged by the move, measured the way the project's gate measures it.**
With `COVERAGE_PROCESS_START` exported (both CI definitions export it, and
`.coveragerc` documents why: the offscreen-GUI drivers run the bubble in child
processes, whose `parallel` data files pytest-cov combines at session end) the suite
is **12 556 statements at 80.5%**, and `core/bubble.py` reads **94%** — the painters
are measured, through the drivers that actually render them. Against HEAD the totals
are 24 522 at 79.54% versus 24 652 at 79.75% in the *same* un-combined mode, so the
split neither hid nor invented coverage: it moved 1 107 statements (1 010 of them
missed) out of a 4 993-statement `handsoff.py` at 58% into a 1 107-statement module at
94% once the children are counted.

That measurement also produced an incident worth recording: `rm -f .coverage*` matches
`.coveragerc`, so a cleanup between runs deleted the coverage CONFIG, and every report
taken while it was gone silently omitted `tests/*` and measured the un-combined mode —
which is why an earlier pass here reported `core/bubble.py` at 11% and blamed a
combine gap that does not exist. The file is restored and the numbers above are from
the gate's own configuration.

### Found by deploying: the loader was shadowing the stdlib `calendar`

The first deploy of the refactored tree came up with **no speech engines**:

```
ERROR handsoff: startup: whisper: faster_whisper is not installed
  (cannot import name 'timegm' from 'calendar' (/…/.local/bin/core/calendar.py))
ERROR handsoff: startup: tts: chatterbox is not installed (…same…)
```

The bubble was running with `tts.ready: false` and `whisper_ready: false`, and the
message blamed the libraries — which were installed all along. Measured against
`HEAD` in two side-by-side probes, the difference was total:

| | `sys.modules['calendar']` after startup | `timegm` |
|---|---|---|
| HEAD (before) | `/usr/lib/python3.14/calendar.py` | present |
| refactored tree (before the fix) | `/…/core/calendar.py` | **missing** |

**Cause.** `core/calendar.py` shares its name with the standard library, and
`core.load_module` registered every support module under its **bare** name as well as
`core.<name>`. Two consequences compounded:

1. The bare binding clobbered the stdlib entry, so every later `from calendar import
   timegm` — `faster_whisper` and `chatterbox` both do it — got the app's module;
2. the bare name was bound **before** the module's own body ran, so
   `core/calendar.py`'s own `import calendar` (line 9) resolved to the half-built
   module *itself*. The loader planted the shadow and then the module read it.

**Why it appeared only now.** At HEAD, `handsoff.py` reached the module through
`from core.calendar import (…)`, whose chain imported the stdlib `calendar` *first* —
and `load_module` deliberately skips bare registration when the existing entry is not
ours (`prev_bare is None or _origin_ok(prev_bare)`), so the shadow was avoided by
accident of ordering. The refactor replaced those re-export imports with
`_core_calendar = _load_module("calendar")`, which is precisely the call that performs
the bare registration — so the safety margin the old import order provided was removed
without anything noticing. **The suite was green through all of it**, which is the
lesson: the whole `core/` loading path is exercised, but nothing asserted that a
support module may not take a name the interpreter owns.

**Fix.** `load_module` may no longer bind a bare name for anything in
`sys.stdlib_module_names`; `core.<name>` is still registered either way, and every
non-stdlib name keeps its bare alias (so the documented bare-name adoption of
`hardware` is untouched). Two guards force the hazard rather than hoping for it — the
stdlib entry for `calendar` is removed from `sys.modules` for the duration, so the
guard cannot go vacuous on a machine where something else imports it early, and both
directions are pinned: the stdlib must survive *and* a normal support module must keep
its bare name, since "stop binding bare names at all" would be a different bug.

**Verified live:** the redeployed bubble logs `speech engine warmed (17 944 samples in
0.9 s)`, `doctor` reports `tts: chatterbox-turbo (reference optimus_clip.wav) — model
loaded; stt: whisper loaded`, `health` reports `tts.ready: true, device: cuda`, and the
`timegm` errors are gone. **2/2 mutations caught** (the stdlib check removed; bare
registration disabled outright).

## A design whose art is a FILE (the fourteenth shape)

**What was missing.** Every shape in `BUBBLE_DESIGNS` is a Python painter, so a new shape
costs code in four places (the painter, the dispatch, the preview glyph, the aperture fit)
and none of it can be authored by the person using the bubble. `image` is the same
contract with the art moved into data: a file the user picks, drawn to the rules the
painters obey rather than pasted over them.

**Three rules make an imported picture behave like a painted design.** (1) *Fit*: what
gets fitted to the aperture is the picture's CIRCUMSCRIBED circle — its diagonal, not its
width — so the voice can tilt it and no corner can cross the glass, because a circle does
not change under rotation. (2) *Colour*: the state colour owns every opaque pixel, the
picture contributing silhouette and luminance through a 0.45 floor, so a black photo
cannot hide the state — the `void` defect (8 visible px of 45 796) wearing a user's own
file. (3) *No orb fallback*: with no picture chosen, or one that will not decode, the
design draws a dashed empty slot in the state colour, because an orb there is
indistinguishable from a design name with no dispatch branch — a bug this tree has already
shipped once.

**The seam.** Decode and tint are functions of a PATH (`_decoded_image`, `image_layers`,
`tinted_image`), so the settings preview and the bubble share one idea of one file: the
preview calls them with its own one-entry cache instead of reimplementing the fit, and a
panel that showed the empty slot while the bubble drew the photo is a preview lying about
the thing the combo above it selects.

**Two real defects found while verifying — by the new guard, not by reading.** First, a
path that could not be stat'ed left the last picture that *did* decode in `_IMAGE`, so a
moved or deleted file kept drawing the old art: "it ignores what I choose" in a new
costume, caught because the guard compares a broken path's ink against the empty slot's.
Second, and worse, the tint was built per painted frame at the SOURCE size — a 3000x2000
photo meant a 24 MB allocation plus a QPainter over it at 25-60 Hz, and the aperture sweep
(160 renders) segfaulted on it. The decode now downscales ONCE to a bounded working canvas
(`_IMAGE_WORK = 384`, still oversampled against a drawn diagonal of at most ~169 px at the
largest window), and both `_shading_layer` and `tinted_image` refuse to paint on a null
QImage instead of trusting an allocation to succeed.

**Evidence.** 14/14 mutations back to the old behaviour caught, one per decision (missing
dispatch branch, width instead of diagonal, the 0.45 floor removed, a transparent picture
treated as drawable, the stale-picture clear dropped, the canvas cap removed, the doctor
reason blanked, the live-apply key removed, the form no longer collecting the file, the
reload not picking it back up, the cancelled-picker guard dropped, the apply message not
naming the change, the preview call site removed, the preview glyph falling back to the
orb). 1082 tests green in three orderings; coverage **80.72% ≥ 70**; `ci/compile_all.py`
clean (42 files). Live proof, on the running bubble rather than in a test: settings swapped
to the new design → `--ptt health` reports `design: image`, doctor prints
`appearance: look Custom (image, 144 px)`, the journal records two
`settings reloaded live (no restart)` lines with zero tracebacks, and the user's own
settings were restored byte-for-byte afterwards (`design: pikachu`, look `spark`).

## A design's art as several pictures: the design-pack format

**What was missing.** `image` drew ONE file for all four states, so the only way to show a
different picture while listening than while thinking was to edit the setting between
states. A PACK is that art as data: a folder holding `pack.json` beside its pictures, one
picture per state, installed from the Appearance tab so by ONE choice several pictures
switch together as the bubble changes state.

**The format is a contract about a folder someone else wrote.** `{"name", "any", "states"}`;
`states` may name any subset of the four, a state it does not name uses `any`, and a pack
that leaves a state uncovered with no `any` is REPORTED rather than silently drawing an empty
slot for it. Two rules make a foreign folder safe to install, both of them the same rule
`read_file`'s denylist applies: every name resolves INSIDE its own folder through
`_pack_file` (an absolute path, a `..` segment, or a symlink whose target is outside is
refused — `resolve()` is what catches the symlink), and installing COPIES the folder into
`~/.config/handsoff/design-packs/<slug>/` instead of referencing it, so a pack keeps working
when the folder it came from moves. Installing over the same name moves the old copy to
`<slug>.previous` — one generation, replaced next time, never listed as a pack of its own.
`pack_slug()` reduces any name to letters/digits/dash/underscore, so a name can never become
a path.

**One authority, three consumers.** `_validate_pack` decides whether a folder is usable, and
`install_pack` refuses it, `load_pack` resolves it to NOTHING (art assembled from the parts
that happened to check out is the silent half-working this tree keeps deleting) and
`pack_problem` NAMES it — three implementations of "is this pack good" would drift, and the
drift would be invisible: a pack half-drawn while doctor reports it fine. `picture_for` owns
the precedence (a selected pack is the AUTHORITY, so a pack that cannot be read returns `""`
rather than falling back to the single picture — art from a source the user did not choose is
worse than no art), and the settings preview calls it rather than reimplementing it, which is
how the panel and the desktop cannot disagree. `PACKS_DIR` is injected by the host like every
other path (`tests/test_sandbox.py`'s HOST tuple gained it as a PATH, not appearance state)
while the module keeps an XDG fallback, so the settings app — which loads the module on its
own — resolves the same tree without being told.

**A real defect the verification found, not a reading.** A folder whose manifest reached
outside itself (`"any": "../outside.png"`, or an absolute `/etc/hostname`) was refused with
**"names no picture"** — the wrong reason, pointing the user at their JSON syntax instead of
at the entry that escaped; `install_pack` was validating through the lenient builder that
silently DROPS such an entry. Both paths now go through `_validate_pack` and the message names
the offending entry. The sweep also exposed a line of my own as dead: `if problem: built =
None` in `load_pack` could never fire, because `_validate_pack` already returns `None` as its
`built` on every problem path — the guarantee is now stated in that function's contract and
the unreachable branch is gone rather than left as decoration.

**Evidence.** **24/24 mutations** back to the old behaviour caught, one per decision (the
escape check, the symlink containment check, the slug sanitiser, the transparent-picture
refusal, the uncovered-state refusal, the half-built-pack refusal, the pack-has-no-authority
fallback, doctor reporting the single picture for a pack, `.previous` listed as a pack,
generations stacked instead of replaced, a missing pack unnamed, install skipping its own
validation, the setting not stripped, the coercion validating the pack away, the choice not
pointing the shape at the design that draws it, the choice not a live key, the preview
ignoring which state a slot is, doctor not naming the pack, a saved-but-uninstalled pack
hidden from the picker, a refused install selecting the pack, Clear leaving it selected, the
panel claiming a pack is "in use" while the shape is a painter, and the pack dropped from the
preview's resolution). 1107 tests green in three orderings
(default, shuffled-test seed 20260913, shuffled-file seed 7); coverage **80.76% ≥ 70**
measured the gate's own way (`COVERAGE_PROCESS_START`); `ci/compile_all.py` clean (43 files).
Live proof on the RUNNING bubble rather than in a test: a four-picture pack installed through
the DEPLOYED module, settings moved to `design: image` / `design_pack: prism`, `--ptt
reload-settings` → `--ptt health` reports `{'look': 'Custom', 'design': 'image', 'size': 144}`
and doctor prints `appearance: look Custom (image, 144 px) — pack prism`, with two `settings
reloaded live (no restart)` journal lines and zero tracebacks — then the user's own settings
restored byte-for-byte (`design: pikachu`, look `spark`). Nothing committed.

## One picture per state, without a pack folder

**What was missing.** The `image` design drew ONE file for all four states, so the only way to
show a different picture while listening than while thinking was to edit the setting between
states — and a pack was the only way to get per-state art, which meant authoring and
installing a folder for what is often just two pictures.

**The four settings are derived, not respelled.** `settings_schema.DESIGN_IMAGE_KEYS` is built
from `BUBBLE_STATES`, and `core/bubble.py` derives its own `STATE_IMAGE_KEY` from the same
tuple, with a guard asserting the two modules name the same four settings — the failure that
would otherwise hide is a panel saving a key nothing reads, i.e. *"I chose a picture and the
bubble ignored it"* with no error anywhere. Precedence is the SAME rule a pack uses, so there
is one rule instead of two: a state with its own picture draws it, a state without one uses
the fallback (`design_image_path`), a state with neither draws the empty slot, and a selected
pack remains the authority over all of it. `picture_for()` is that rule — called by the
bubble AND by the settings preview, which is what stops the panel describing a different
picture than the desktop draws.

**Every failure names the STATE.** `state_image_problem()` reports the first state whose own
picture cannot be drawn, with the state's name in the sentence, because with four slots
"which one is broken" is the entire question; the panel shows it per row and `doctor` prints
the same sentence, from the same function.

**A real defect the verification found, not a reading.** `doctor` counted the pictures that
were CHOSEN, not the ones that render — so a run with four configured pictures and one broken
file printed `picture per state: 4/4` on the very same line as `image: the thinking picture:
no file at …`. A count of intentions reading as a clean bill of health is precisely the class
of lie this project keeps finding, so the count is now `usable_state_pictures()` (both
questions, both answers: `state_pictures` is what the resolver draws from and keeps a
chosen-but-broken path, `usable_state_pictures` is what will actually render), and the live
proof below is what exposed it.

**Evidence.** **44/44 mutations** back to the old behaviour caught, one per decision — the 20
pack + image ones plus 3 new for this set (a per-state picture dropped when it cannot be read,
the count including pictures that do not render, and `doctor` counting chosen pictures).
**1118 tests green** in three orderings (default, shuffled-test seed 20260913, shuffled-file
seed 7); coverage **80.99% ≥ 70** measured the gate's own way (`COVERAGE_PROCESS_START`);
`ci/compile_all.py` clean (43 files). New guards: the per-state unit cases in
`tests/test_design_packs.py` (34 cases now) and the offscreen scenario
`the_image_design_takes_one_picture_per_state`. Deployed `in-sync` (installed == checkout for
all five touched files) and verified against the RUNNING bubble with four distinct per-state
pictures plus a fallback: doctor prints `appearance: look Custom (image, 144 px) — picture per
state: 4/4`, then pointing `thinking` at a missing file makes it print `… — picture per state:
3/4 — image: the thinking picture: no file at /tmp/pack/live/does-not-exist.png` (the corrected
count doing its job), with `settings reloaded live (no restart)` in the journal and the user's
settings restored **byte-for-byte** (sha256 equal before and after, `design: pikachu`, look
`spark`).

**One flake observed and NOT chased to ground (recorded, not described as a fix).** The
tests-shuffled ordering (seed 20260913) failed once in four attempts with
`ERROR tests/test_hardware.py::TestPromptContext::test_token_cap — PytestUnhandledThreadExceptionWarning`,
while the same file passes in isolation under that same seed and the run passes on re-run.
That warning is reported against whichever test is RUNNING when some other test's thread
dies — `hardware.snapshot` is single-threaded, so the raising thread is not this test's —
which makes it the same class as the `test_audio.py::TestMicHealth` flake recorded with the
pack set: a leak whose blame lands on an innocent bystander. It did not reproduce in the
two full re-runs (one with the message captured to a log) or the three seeded subset runs, so
it is recorded as an open question rather than a closed one. Nothing committed.

## Online lookups: a probed search router and a page reader

**What was missing.** `web_search` scraped `lite.duckduckgo.com` with a regex that required a
double-quoted `class="result-link"`, and the live page uses **single** quotes
(`class='result-link'`). The parser therefore matched nothing, returned an empty list, and the
tool fell through to Wikipedia and reported `(wikipedia)` — every web search has been answering
from the encyclopedia, which is what "DuckDuckGo is kinda limited" actually was. Nothing caught
it because every test stubbed `_http_get`: the fixtures were written by the same person as the
parser, in the same quoting style. The fix accepts either quoting and any attribute order, and
the pin is a fixture **taken from the real response** plus a live check.

**The pieces.** `core/web.py` (application-free, host-injected like `core/bubble.py`) owns the
backends, the router, the reader, a TTL cache and one failure vocabulary. `handsoff.py`'s
`_ddg_lite` / `_wiki_search` became one-line delegations, so `_world_events` (proactive
briefings, severe-weather warnings) upgraded through the same seam — which immediately caught a
regression: the delegation defaulted to the router's four-item limit while the briefing consumed
five, and a guard failed on the shortened feed.

**One tool added, not five.** The tool schema is generated from each signature and the fixed
prompt is the scarce resource, so the four domains live behind `web_search(source=…, read_top=…)`
selected by deterministic regexes over the query (error/API shapes start at Stack Exchange, repo
shapes at GitHub, news at the general pair) and the reader is the one new tool. Measured: 48
tools, `_fixed_prompt_tokens` 6327 of 32768, the two web schemas costing 265 tokens together.

**Backends are keyless, and every one of them was verified live before being relied on.** What
the survey ruled OUT matters as much: Jina's search endpoint answers `401 Unauthorized` without
a token; Mojeek captcha-walls scripts (`JavaScript is required`); a public SearXNG instance
(`searx.be`) answers a non-browser with Cloudflare's `Verifying your browser…` — so "just use a
public one" is not an option and a local instance is the only route to aggregate, private,
keyless web search. Stack Exchange, HN and GitHub answered keyless JSON; the Stack Exchange
payload even carries its own quota (`quota_remaining 299/300`).

**Nothing reports healthy because it is configured.** The record doctor reads is written by
USE: a backend that answered is `ok (<when>, quota …)`, one that failed keeps its reason and
time, one nothing has asked is `untried`, and the SearXNG entry — the single entry whose
availability is a local service — is probed with a localhost connect. This is a deliberate
departure from the plan's "probe every backend on every doctor call": six network probes in a
diagnostic the model can call mid-conversation buys nothing that the router's own observations
do not, and the property that matters (no invented health) is preserved exactly.

**A first real defect the live proof found, in the first live call.** A burst of queries made
DuckDuckGo serve its anti-bot page (`anomaly.js`, `cc=botnet`, a `challenge-form`) — and the code
reported that as **"no results"**. A blocked search and an empty web are different facts, and the
user cannot tell them apart from a bare sentence; the backend now raises a named failure
("DuckDuckGo served a bot challenge — try again later or run a local SearXNG"), the router keeps
it in the record, and a test pins all three outcomes (results / an honest empty / a page that is
not a result page at all).

**A second one, about privacy rather than honesty.** The reader's fallback rule was "local text
under 200 characters", so `example.com` — sixty characters, perfectly readable here — was sent to
a third-party reader for nothing. A page is judged useless locally only when the site blocked us
or when there is a LOT of markup and almost no text (a JavaScript shell); a short page is short.

**Guards and gates.** `tests/test_web.py` (63 cases) drives every backend, the router's order and
fallback, the cache (a repeat makes no request), the cap (one in-flight request per backend —
asserted with a held fetch and a second caller), the failure vocabulary, the reader's extraction,
every anti-bot signature, the local-first/fallback decision in both directions, the disclosure
prefix, the cached-snapshot passthrough, truncation, and the URL refusals including a public name
that RESOLVES to this machine. **35/35 mutations** back to the old behaviour caught, one per
decision — and three of the misses in the first sweep were weak guards rather than bad mutations,
which is the sweep doing its job: the challenge-page fixture was short enough that the useless-page
rule alone triggered the fallback, the resolver guard did not check the searxng setting, and the
`lookup_fact` fake answered the endpoint that made the delegation untestable. **1181 tests green**
in three orderings (default, shuffled-test seed 20260913, shuffled-file seed 7); coverage
**81.49% ≥ 70** with `core/web.py` at **93%**; `ci/compile_all.py` clean (45 files).

**Live proof on the DEPLOYED module, not a fixture.** `~/.local/bin/core/web.py`: Stack Exchange
returned 2 real hits (`quota 291/300`), DuckDuckGo 2 real hits with real URLs, `example.com` read
`via local fetch`, a Reddit thread answered `the site refuses automated readers (network security
block)`, `http://192.168.1.1/` was refused (`is on this machine or a private network`), and
`--ptt doctor` printed `search: searxng not running, ddg ok (2s ago), stackexchange ok (3s ago,
quota 291/300), hn untried, github untried, wikipedia untried` and `reader: local fetch FAILED (no
text in 8427 bytes of markup, 2s ago), Jina fallback refused (network security block, 0s ago)`,
with the deployed copy `in-sync` and the running bubble healthy (`health` ok, `design: pikachu`,
the user's own appearance untouched). Nothing committed.
