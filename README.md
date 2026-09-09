# handsoff

A self-modifying voice assistant that lives as a small round bubble on your
desktop (Arch Linux / CachyOS + niri Wayland). Hold the bubble, speak, release
— it transcribes locally (faster-whisper), answers with a local Ollama model,
and speaks back with Piper TTS. Everything runs on your machine.

![states](https://img.shields.io/badge/states-idle%20·%20listening%20·%20thinking%20·%20speaking-blue)

## What it can do

- **Voice loop** — push-to-talk (hold the bubble) or hands-free with a wake
  word, plus an optional openWakeWord audio spotter that reacts in ~80 ms.
  Announce-and-listen: after each reply it briefly listens again without the
  wake word (`followup_seconds`, settings → Voice), so "hey assistant, what's
  the weather? … and tomorrow?" just works.
- **Desktop control** — volume, brightness, windows, workspaces, app launch,
  screenshots (vision), typing into apps, compositor hotkeys.
- **Music** — full MPD control: play/pause, search, volume, now-playing.
- **Reminders & calendar** — spoken reminders that persist across restarts,
  snooze by voice, month grids, and ICS calendar sources (Google Calendar
  secret iCal URL, Nextcloud, local files).
- **Knowledge** — weather (open-meteo), web search (DuckDuckGo), facts
  (Wikipedia), all tool-driven, never guessed.
- **Memory** — remembers durable facts about you (name, family, likes, home,
  pets…) across restarts; they survive conversation trimming.
- **Self-modification** — the assistant can edit its own source, restart into
  the new version, and reports if it crashed.

All 29 tools are declared in one place (`@tool`-decorated methods in
`handsoff.py`); schemas, the system prompt, and permissions stay in sync
automatically. 250 tests pin the behavior (`python -m pytest test_handsoff.py`).

## Requirements

- Arch Linux or CachyOS with the **niri** Wayland compositor
- An NVIDIA GPU is strongly recommended (a 26B model needs ~14 GB VRAM;
  smaller models run on less — see *Model choice* below)
- A microphone and speakers

## Installation

```bash
cd ~/Projects/handsoff        # the checkout
./install.sh
```

The installer:

1. Upgrades the system and installs packages (`pacman -Syu`, supported Arch
   policy) — python-pyside6, python-sounddevice, python-numpy, ollama, curl
2. Installs the Python extras from `requirements.txt` (faster-whisper,
   piper-tts, openwakeword/onnxruntime) into user site-packages
3. Places `handsoff.py`, `handsoff-settings.py`, and `handsoff-restart` in
   `~/.local/bin`
4. Downloads the whisper model and a Piper voice (SHA256-verified, atomic)
5. Writes a systemd **user service** (`handsoff.service`, `Restart=always`)
   so a crash never leaves you without the bubble
6. Prints the niri config snippet to merge (`niri-window-rule.kdl`)

Then merge the snippet and start it:

```bash
# merge ~/.config/handsoff/niri-window-rule.kdl into ~/.config/niri/config.kdl
niri msg action reload-config
systemctl --user start handsoff
```

Exactly one autostart owner is configured: if the systemd unit is enabled the
installer will **not** also add `spawn-at-startup` to niri.

### Optional overrides (before running install.sh)

| Variable | Default | Meaning |
|---|---|---|
| `HANDSOFF_MODEL` | `qwen3:8b` | Ollama model to pull/use |
| `HANDSOFF_WHISPER` | `tiny` | whisper size (`tiny`…`large-v3`) |
| `PIPER_VOICE_URL` | en_US lessac medium | any piper `.onnx` URL — set `PIPER_VOICE_SHA256`/`PIPER_VOICE_JSON_SHA256` too or the download is unverified (with a warning) |

At runtime, environment overrides (only where settings.json has no value):
`HANDSOFF_MODEL`, `HANDSOFF_NUM_CTX`, `HANDSOFF_WHISPER`, `HANDSOFF_VOICE`,
`OLLAMA_HOST`, `HANDSOFF_KEEP_ALIVE` (default `1h` — how long the LLM stays
in VRAM between questions).

## Usage

### The bubble

- **Hold left button** → talk; release → send. Drag to move the bubble.
- **Click** (short press) while it speaks → barge-in: it stops talking.
- **Right-click** → menu (restart, settings, quit).
- Colors: blue idle · red listening · orange thinking · green speaking.

### Keyboard (niri binds from the snippet)

| Key | Action |
|---|---|
| `Mod+V` | talk toggle: start recording / send / interrupt |
| `Mod+Shift+V` | interrupt immediately (stop talking/thinking) |
| `Mod+H` | toggle hands-free listening |
| `Mod+Shift+S` | open Settings (add manually; works even if the bubble is dead) |

### Voice

Say the assistant's name (default "assistant", configurable — e.g. "cypher")
to engage in hands-free mode; you then have an engagement window (default
45 s) of free conversation. While it speaks, saying **"stop"**, **"quiet"**,
**"shut up"**, **"never mind"** silences it instantly — no LLM involved.

Example things to say:

- *"what's the weather in Berlin?"* · *"who wrote Neuromancer?"*
- *"remind me to call mum at 18:30"* → later: *"snooze 10 minutes"*
- *"play some jazz"* · *"what's this song?"* · *"music volume 30"*
- *"open firefox and type hello into the search box"*
- *"move this window to workspace 3"* · *"close spotify"*
- *"what's on my calendar today?"*
- *"make your bubble pink"* — it edits its own source and restarts

### Remote control (CLI)

Any of these work from a script or keybind, even while the bubble runs:

```bash
python ~/.local/bin/handsoff.py --ptt status      # state, handsfree, model
python ~/.local/bin/handsoff.py --ptt toggle      # start/stop/interrupt
python ~/.local/bin/handsoff.py --ptt interrupt   # silence it now
python ~/.local/bin/handsoff.py --ptt handsfree   # toggle hands-free
python ~/.local/bin/handsoff.py --ptt settings    # open Settings
```

## Permissions

The Settings app (`python ~/.local/bin/handsoff-settings.py`, or right-click
the bubble → Settings…) has a permission switch for every dangerous tool:

| Permission | Controls |
|---|---|
| **Run commands** | the whitelisted shell: pactl, playerctl, brightnessctl, niri, echo, cat, ls, pwd, notify-send, restart script |
| **Read files / Edit files** | filesystem access (self-edit guard: only its own source + `~/.config/handsoff/`) |
| **Self-restart** | the restart script |
| **Type text / Press keys** | virtual keyboard into the focused window |
| **Web access** | weather, search, facts, calendar fetch |
| **Control your music (MPD)** | media tools |
| **Screen access** | screenshots + OCR |

Safety boundaries enforced in code (not just the prompt):

- No pipes/`;`/`&&`/backticks/command substitution in `run_command`
- A blocklist (sudo, rm, pacman, systemctl, curl…) wins over the whitelist
- `niri … spawn` cannot launch interpreters or pass code flags (`-e`, `-c`,
  `--eval`) — checked on the executable's basename, so `/usr/bin/niri` gets
  the same scrutiny as `niri`
- Typing/keys **refuse** when the focused window cannot be verified, and
  always refuse terminals (injected text there would execute)
- `edit_file` refuses to touch anything outside its own source and its config
  dir; self-edits must keep the marker line and compile
- Tool-call rate limiting is available in Settings (default: unlimited)

## Configuration

`~/.config/handsoff/settings.json` — edited via the Settings app. Highlights:

| Key | Default | Meaning |
|---|---|---|
| `model` | `qwen3:8b` | any Ollama model tag |
| `num_ctx` | 32768 | context window |
| `history_tokens` | 0 (auto) | history budget; auto = ctx − prompt − reserve |
| `whisper_size` | `tiny` | STT size; `small` is a good speed/accuracy middle |
| `assistant_name` / `wake_word_required` / `engage_seconds` | assistant / false / 45 | wake word behavior |
| `followup_seconds` | 6 (0 = off) | announce-and-listen: seconds after a reply it re-listens without the wake word |
| `wake_spotter` / `spotter_models` | false / `["hey_jarvis"]` | openWakeWord spotter |
| `handsfree` | false | continuous listening |
| `mic_device` / `mic_threshold` | system default / 600 | microphone |
| `home_place` / `calendar_ics` / `briefing` | — | weather place, ICS sources, morning briefing |
| `workspace_aliases` | `{}` | e.g. `{"code": "2"}` → "go to code" |
| `permissions` | all true | the switches above |

## Files

| Path | Contents |
|---|---|
| `~/.config/handsoff/settings.json` | settings |
| `~/.config/handsoff/history.json` | conversation memory (survives restarts) |
| `~/.config/handsoff/memory.json` | durable facts about you (survives trimming) |
| `~/.config/handsoff/whisper-model/` | STT model cache |
| `~/.config/handsoff/piper-voice/` | TTS voice |
| `~/.local/state/handsoff/reminders.json` | pending reminders |
| `~/.local/state/handsoff/handsoff.log` | rotating log (1 MB × 2) |
| `~/.local/state/handsoff/crash.log` | native-crash traceback (faulthandler) |
| `~/.local/state/handsoff/control.sock` | local control socket (mode 0600) |

## Troubleshooting

**Bubble won't start / died silently**

```bash
systemctl --user status handsoff        # is it running?
journalctl --user -u handsoff -n 50     # last 50 log lines
cat ~/.local/state/handsoff/crash.log   # native crash traceback, if any
systemctl --user restart handsoff
```

The service restarts itself (`Restart=always`). On the next start after a
crash the assistant tells you it crashed and why (short version).

**"already running" on manual start** — another instance holds
`~/.local/state/handsoff/handsoff.lock`. Use `~/.local/bin/handsoff-restart`
instead of starting by hand; it waits for the lock.

**Microphone problems**

- Check the device: `pactl list sources short`
- **Live mic test** (Settings → Voice): toggle *Live test* to open the
  selected device continuously — the level meter follows the room, and any
  speech that passes the threshold is transcribed with the bubble's own
  whisper model, shown under *Last transcript*. Use it to verify a mic (and
  tune the threshold) before switching the bubble to it.
- Pick a specific device in Settings → *Input device*; raise *Recording
  threshold* if it triggers on noise, lower it if speech is missed
- The listener retries forever and never permanently disables hands-free;
  if PortAudio wedges in-process, restarting the service reinitializes it
- Known issue on some stacks: a USB mic that can't serve 16 kHz callback
  streams breaks the listener when set as system default — keep the default
  on a device that works and select the other mic in handsoff's Settings

**"I do not have access to…" / tool errors**

- Is Ollama up? `systemctl status ollama`; is the model pulled?
  `ollama list` (pull with `ollama pull <model>`)
- First question after boot can take a few seconds while the model warms
  into VRAM (the bubble does this automatically at startup)

**Hands-free doesn't react**

- `--ptt status` shows `handsfree=on/off` — toggle with `Mod+H` or the socket
- The wake gate needs the assistant's name; the audio spotter (if enabled)
  wakes on its stock keywords instead ("hey jarvis"…)
- Saying "stop" while it speaks intentionally produces no reply

**Settings app won't open** — `Mod+Shift+S` or
`python ~/.local/bin/handsoff-settings.py` works even when the bubble is dead.

**Music tools report MPD down** — `systemctl --user start mpd`.

## Recovery

- **Bad self-edit?** Every `edit_file` writes a `.bak` next to the file. The
  bubble also refuses to restart into a source that doesn't compile or lost
  its self-marker; restore the `.bak` and run `~/.local/bin/handsoff-restart`.
- **Weird state?** `~/.local/bin/handsoff-restart` is always safe: it stops
  the service (if present), waits for the lock, and starts a fresh instance.
- **Start from scratch (keep models/history):** `./install.sh --uninstall`
  keeps `~/.config/handsoff` and `~/.local/state/handsoff`.
- **Full wipe (with automatic backup):** `./install.sh --uninstall --purge`
  tars config+state to `~/handsoff-backup-<date>.tar.gz` before deleting.
- **Conversation gone weird:** delete `~/.config/handsoff/history.json`
  (memory.json keeps long-term facts); or clear facts by deleting
  `~/.config/handsoff/memory.json`.

## Uninstall

```bash
./install.sh --uninstall            # remove program + service, keep data
./install.sh --uninstall --purge    # also wipe config/state (backs up first)
```

## Development

```bash
python -m pytest test_handsoff.py -q     # 250 tests
python -m py_compile handsoff.py handsoff-settings.py
bash -n install.sh
```

CI (`.github/workflows/ci.yml`) runs exactly these gates on every push: the
suite on Python 3.12 and 3.13 (offscreen Qt, no audio hardware needed),
byte-compilation of every source file, and shell syntax checks. Background-
thread exceptions fail the run via `pytest.ini` rather than passing silently.

**Pre-commit gate** — the repo ships `githooks/pre-commit`, which runs the
compile, shell-syntax, and full-suite gates before every commit, so a broken
self-edit cannot be committed. Enable it after cloning (one time):

```bash
git config core.hooksPath githooks
```

A regression test (`TestPrecommitHook`) pins the hook's presence; bypass
deliberately with `git commit --no-verify`.

Layout of `handsoff.py`: config → system prompt → Ollama client → audio
(STT/TTS) → tools → Assistant state machine → Bubble UI → main(). The
settings app is separate (`handsoff-settings.py`); `tests/fake_ollama.py`
fakes the Ollama API for the streaming tests.

Known limitations: the ICS parser handles DAILY/WEEKLY recurrence plus
EXDATE and RECURRENCE-ID (canceled/moved instances of recurring events are
suppressed/replaced correctly); calendar RRULE MONTHLY/YEARLY fall back to
the single occurrence. See `GAP_ANALYSIS.md` for the current roadmap.
