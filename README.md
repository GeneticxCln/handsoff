# handsoff

A self-modifying voice assistant that lives as a small round bubble on your
desktop (Arch Linux / CachyOS + niri Wayland). Hold the bubble, speak, release
— it transcribes locally (faster-whisper), answers with a local Ollama model,
and speaks back with Chatterbox TTS — the built-in voice, or a clone of a
voice clip you pick. Everything runs on your machine.

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
- **Ambient automation** — opt-in desktop notification reading, Pomodoro work /
  break cycles, threshold alerts for RAM/GPU memory, and bounded file/process
  watchers that announce matching failures or exits.
- **Your Quantum Space desk** — "what is Claude doing?" answers from the desk
  itself: which sessions are open in the Quantum Space workbench, and the tail
  of what each one printed. Read-only, over the local channel Quantum Space
  opens for this (its Settings → Control), and only once that desk allows this
  assistant by name.

All tools are declared in one place (`@tool`-decorated methods in
`handsoff.py`); schemas, the system prompt, and permissions stay in sync
automatically. 1317 tests pin the behavior (`python -m pytest tests/`),
split by area: audio, policy, desktop, calendar, settings, lifecycle,
regression, ops, and fault injection — including offscreen-Qt scenarios that
drive the settings GUI itself.

**Fault injection** (`tests/test_fault_injection.py`) breaks one external seam at
a time — Ollama refusing or going quiet, the mic handing back nothing,
dbus-monitor dying, a disk write failing, the control socket vanishing, the
compositor or ydotoold exiting mid-turn, a whisper/speech model going missing or
unreadable, the disk filling up (ENOSPC), and the wall clock stepping backwards
— and asserts the bubble degrades **loudly**: a WARNING/ERROR a person can find
in `journalctl`, a spoken line naming the cause, a reported failure, a toggle
that stops claiming to be on. It also asserts the silence of the alternative —
no fabricated success, no swallowed error, no value reported as saved when it is
not. Injections happen at the boundary (the HTTP opener, the recorder's return
value, the popen factory, the write path, the file object the writer is handed),
never by replacing the code under test.

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
   chatterbox-tts, openwakeword/onnxruntime) into user site-packages
3. Places `handsoff.py`, `handsoff-settings.py`, and `handsoff-restart` in
   `~/.local/bin`
4. Downloads the whisper model (SHA256-verified, atomic) and prefetches the
   chatterbox-turbo speech weights, checking they are actually usable rather
   than merely present
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
| `HANDSOFF_TTS_REPO` | `ResembleAI/chatterbox-turbo` | Hugging Face repo holding the speech weights |

At runtime, environment overrides (only where settings.json has no value):
`HANDSOFF_MODEL`, `HANDSOFF_NUM_CTX`, `HANDSOFF_WHISPER`, `HANDSOFF_VOICE`,
`OLLAMA_HOST`, `HANDSOFF_KEEP_ALIVE` (default `1h` — how long the LLM stays
in VRAM between questions).

## Usage

### The bubble

- **Hold left button** → talk; release → send. Drag to move the bubble.
- **Shape & feel** — the Appearance tab picks one of fourteen designs (orb,
  halo, reactor, bloom, droplet, cube, equalizer, crystal, saturn, void, Eye of
  Sauron, Pikachu, Cat, Image), bubble size, an animation-energy scale (orbit speed,
  swirl, comet brightness) and a colour-accent punch; one click of **Match
  wallpaper** retunes all four state colours for a dark or light backdrop.
  Every control on the tab — shape, size, both sliders, the four state colours,
  wallpaper matching — applies live, with no Save needed. **Every design reacts
  to your voice in its own way**, from one shared level signal: the orb ripples
  outward, the halo sends a brightness wave round its torus, the reactor opens
  its segments and spins up, the bloom shakes extra sparks loose, the droplet
  ripples its skin and drips sooner, the cube flashes its facets in a sweeping
  front, the equalizer's bars are the meter itself, the crystal refracts (its
  inner hex swells and splits hue), saturn's ring carries a travelling wave,
  the void accelerates its infall, the Eye of Sauron narrows its pupil and
  flares its fire, Pikachu charges its cheeks, and the Cat's ears splay and its
  tail swings wider as you talk. Every one of those terms is neutral at
  silence, so a quiet bubble renders exactly as it always did.
- **Looks** — the same tab's **Look** picker sets the whole appearance in one
  click: a named look (Handsoff, Midnight, Daylight, Ember, Neon, All-seeing,
  Spark, Curious) carries a shape, a size, both sliders and all four state
  colours, and applies live like any other control. Each look is *drawn* on its
  own button — its silhouette on a state-coloured glyph, its other three state
  colours as dots beneath — from the same painter the preview strip uses, so a
  button cannot show a shape the bubble would not. Nothing extra is stored:
  the current look is *derived* from the five values, so the tab says "Custom"
  the moment you nudge a slider instead of leaving a stale name on screen, and
  `--ptt health`/`--ptt doctor` name the look the settings actually spell out.
  The two looks built on Pikachu and the Eye of Sauron say so in their
  tooltips: those designs keep their own palettes, so a look's colours tint
  their aura/corona rather than repainting the character.
- **Image** — the last design's art is a FILE of yours, not a painter: click
  **Choose image…** beside the Shape combo and the bubble draws that picture,
  fitted to the glass (its diagonal is fitted to the aperture, so nothing can
  cross the rim), tinted by the state colour with a luminance floor, and lit by
  your voice. No file — or one that will not decode — draws a dashed empty slot
  in the state colour rather than silently falling back to the orb, and
  `--ptt doctor` names the reason either way.
- **Avatar colours & decoration** — two controls that belong to a *character*
  rather than to a shape. **Avatar colours** chooses `State colours` (the art is
  painted in the current state's colour, so the picture can never hide which
  state it is) or `Original colours` (the art keeps the colours you drew it in,
  and the state is carried by the rim and by the decoration instead — a yellow
  character stays yellow in every state, and your voice brightens it rather than
  repainting it). **Decoration** picks an animated ring that lives *behind* the
  avatar, in the band the picture gives up when it is on — nine of them:
  **Ring light** (two arc pairs plus a dimmer counter-rotating pair), **Orbit**
  (bright heads circling the avatar), **Pulse** (rings that travel outward as
  you speak), **Aurora** (a shimmering band whose hues sweep around it),
  **Rainbow ring** (one thick band holding every hue at once, turning),
  **Sparkles** (points that pop and fade all round a faint ring), **Comet** (a
  single long-tailed streak sweeping the ring), **Neon tubes** (a segmented tube
  always lit, with a pulse chasing round it) and **Flames** (tongues of fire
  licking up, running white-hot at the tips when you talk). Each turns with the
  animation-energy setting and brightens and quickens with your voice; because
  the picture's fit shrinks to hand the decoration its band, decoration and art
  can never overlap, and every one of them is clamped inside the aperture.
  **Decoration colour** is where a ring stops being a copy of the bubble's mood:
  `State colour` (the default) tracks the state, `Rainbow` sweeps a hue of its
  own and speeds up with your voice, and `Colour of its own` keeps one colour
  whatever the state is — so a ring can be red while the bubble is thinking
  purple, or a wheel that turns on its own. Choosing either of the independent
  modes cannot cost readability, because the rim is drawn in the state colour
  and is the design's outermost ink. When
  the art carries opaque corners it is cut to a round avatar on a soft
  state-coloured stage, so an ordinary photo reads as an avatar rather than a
  pasted rectangle; character art that already has its own silhouette is left
  exactly as drawn, because masking it could only cut ink on purpose.
- **One picture per state** — the four fields under **Choose image…** let the
  Image design show a *different picture per state* without a pack folder: set
  **idle**/**listening**/**thinking**/**speaking** and each state draws its own
  file, live. A state you leave empty uses the fallback picture beside them; a
  state with a file that will not decode draws the empty slot and the panel
  names *which* state it was ("listening: ⚠ no file at …"), because with four
  slots "which one is broken" is the whole question.
- **Design packs** — instead of four loose files, a **Pack** can give the Image
  design a *different picture per state*, all switched together as the bubble
  changes state. A pack is a folder with a `pack.json` beside its pictures:

  ```json
  {"name": "Optimus",
   "any": "base.png",
   "states": {"idle": "idle.png", "listening": "listen.png",
              "thinking": "think.png", "speaking": "speak.png"}}
  ```

  `states` may name any subset of the four; a state it does not name uses
  `any`; a pack that leaves a state uncovered with no `any` is refused with the
  missing names in the message. Click **Install pack…** and pick the folder: it
  is COPIED into `~/.config/handsoff/design-packs/<name>/`, so it keeps working
  when the folder it came from moves, and installing over the same name keeps
  the old copy as `<name>.previous` for one generation. Every picture must sit
  inside the pack folder — an absolute path, a `..` or a symlink out is refused
  — and a selected pack is the authority: if it cannot be read, the bubble
  draws the empty slot and the reason is shown in the panel and in `doctor`
  (`appearance: look Custom (image, 144 px) — pack optimus`) rather than
  quietly substituting another picture. **Export pack…** is the reverse: it
  writes the art ON SCREEN — the selected pack, or your own per-state pictures
  with the fallback behind them — into a new folder as a pack, so a look you
  built by hand can be handed to someone else. It refuses a folder that already
  exists (nothing of yours is overwritten) and writes nothing at all unless the
  result passes the same validation an install does; a refusal says why in the
  status line and leaves no half-written folder behind.
- **Pack files** — a folder is not something you can attach to a message, so a
  look also travels as ONE file. **Export pack file…** writes the same art as
  **Export pack…** through the same staging step, as `<name>.hpack`: a plain zip
  holding `pack.json` and the pictures, so any archive tool can open it and the
  manifest is the first entry. **Import pack file…** installs one, and it is the
  same install a folder gets — the file is unpacked into a private temporary
  folder and then handed to the identical code path, so a `.hpack` can only ever
  do what a folder could and there is no second set of rules to forget. That
  means it is validated before anything reaches `design-packs/`, keeps one
  `.previous` generation, and is COPIED in rather than linked to the file it
  came from. The suffix is not the contract: a `.zip` someone renamed installs,
  and a `.hpack` that holds no `pack.json` is refused. Because the archive is
  data someone else wrote, four things are checked by name before a byte is
  unpacked — an entry that would leave the folder (`..`, an absolute path, a
  drive letter, a symlink), more entries than a pack can hold, an unpacked size
  over 64 MB, and a manifest at neither the top level nor inside exactly one
  wrapper folder (`a zip that wraps the pack in a folder` is accepted, because
  that is how people actually zip things; two candidates is refused, because
  picking one would install a look you did not choose).
- **Preview pack…** — look before you leap. **Preview folder…** and **Preview
  pack file…** draw a pack you have not installed in the same four-state strip
  the rest of the card uses, through the same resolver, decode and tint, so what
  you see is what an install would give you. Nothing is written: a folder is
  read where it already sits, and a `.hpack` is unpacked into a temporary folder
  that belongs to the panel — removed on cancel, on **Try it**, on a second
  preview, on any other pack action and when the window closes. A pack that
  cannot be read is refused in the sentence an INSTALL would use (and the same
  sentence `doctor` prints), because a preview that were more forgiving than the
  install would be showing you something you cannot have. **Try it** is the step
  that writes: it installs the pack you were shown and switches to it, so the
  copy in `design-packs/` keeps working after the folder or file it came from
  moves. Previewing switches the strip to the `image` design whatever shape your
  bubble currently draws, because otherwise Preview would look as if it did
  nothing.
- **A state can be an ANIMATION, not just a still.** A pack's state value is
  either `"file.png"` or `{"frames": ["a.png", "b.png", …], "fps": 10}` — up to
  16 frames at 0.5–30 fps — and the painter cycles them by time through the
  same decode, cache, tint and fit a still takes, so a character can blink,
  perk up and mouth-flap instead of posing. Every existing pack is unchanged
  (a still is still a string), a broken frame is refused with the exact
  sentence a broken still gets (`no file at missing.png (the idle picture)`),
  and an export of an animated look ships every frame and the fps — a look
  that moves travels as a look that moves. The preview strip shows an
  animation's FIRST frame: a decision aid, not a projector.
- **Avatar decoration** — the Image design can wear a **ring light**: arcs drawn
  *around* the avatar that rotate with the animation energy, brighten and quicken
  with your voice, and carry the state colour. The Appearance tab's **Avatar
  decoration** row is a closed choice (Ring light / Off) and applies live. The
  picture hands the ring its band by shrinking to fit (the picture can never
  overlap its own decoration), the whole design still stays inside the aperture,
  and `doctor` names the ring beside the pack so "what is that light" has an
  answer. Art with OPAQUE corners — an ordinary rectangular photo — is clipped
  to a feathered circle so the avatar is round rather than a pasted rectangle;
  art that already carries its own silhouette (a character PNG) is untouched.
- **The preview is drawn on the BUBBLE too**, so a look can be judged where it
  will actually live — against your wallpaper, beside your other windows, at the
  size this bubble really is — instead of only in a strip inside a settings
  window. The panel pushes the candidate over the control socket
  (`preview-pack <folder>`), and because that has to be revivable it is a
  HEARTBEAT: the bubble holds the candidate in memory with a short deadline and
  every beat renews it, so a panel that is closed, killed or disconnected stops
  renewing and the bubble puts your real look back by itself — nothing is
  installed and nothing is written, which is what makes Cancel a no-op rather
  than an undo. Nothing is trusted either: the bubble re-reads the folder on
  every beat and validates it with the same reading an install uses, so a pack
  it would refuse is refused out loud, and a candidate whose folder is deleted
  or edited mid-preview is dropped rather than drawn from stale state. The label
  says which of the two you are looking at — `drawn on the bubble` or `the
  bubble is not running, so only this window shows it` — and `doctor` grows the
  same clause (`— previewing Candidate (not installed)`) while a preview is up,
  because a preview draws art the settings do not name and the line would
  otherwise describe a look the bubble is not drawing.
- **Cat** is the first design whose outline leaves the bubble's circle: its
  ears are painted outside the inset ellipse, so the window's mask is built per
  design (`design_region`) and follows a live shape change as well as a resize.
  While the bubble is talking it follows its own playback level too (the mic is
  muted during TTS, so `play_wav` reports what it is actually playing), and
  which of the three producers fed the level — your mic while listening, your
  push-to-talk key, or the bubble's own voice — is shown live by the meter in
  **Settings → Voice**.
- **Click** (short press) while it speaks → barge-in: it stops talking.
- **Right-click** → menu (restart, settings, quit).
- Colors: blue idle · red listening · orange thinking · green speaking.

### Keyboard (niri binds from the snippet)

| Key | Action |
|---|---|
| `Mod+V` | talk toggle: start recording / send / interrupt |
| `Mod+Shift+V` | interrupt immediately (stop talking/thinking) |
| `Mod+Shift+H` | toggle hands-free listening |
| `Mod+Shift+S` | open Settings (add manually; works even if the bubble is dead) |

### Voice

**The speech engine is `chatterbox-turbo`.** With no voice clip set it speaks
in the engine's own voice. Pick a clip in **Settings → Voice** (*Voice
reference clip*) and it clones that voice instead; the engine requires **more
than 5 seconds** of audio, so a short sample is refused loudly (in the GUI
before you save it, and by the bubble at load time) rather than leaving you a
mute assistant. *Preview* speaks through the **running bubble** (`--ptt say`)
rather than loading a second copy of the model in the settings process — that
second copy is ~2.7 GB of VRAM. `--ptt say <text>` does the same from a
script, and `--ptt health` reports the engine, the device and which clip is
loaded.

Speed: measured ~3× faster than real time on a GPU (RTF ≈ 0.3), so replies are
synthesized per sentence and stream as they are ready. On CPU it is slower than
real time and the bubble says so at startup.

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

### Looking things up online

Ask *what does this page say*, *why is CUDA failing on Python 3.13*, *what is
this repo* — the AI searches and reads, keylessly, through backends chosen by
the shape of the question:

| backend | what it is for | needs |
|---|---|---|
| SearXNG | general web (aggregated) | a local instance, **probed** not assumed |
| DuckDuckGo (Lite) | general web, the fallback | nothing |
| Stack Exchange | errors, APIs, libraries | nothing (reports its daily quota) |
| Hacker News | releases, discussion | nothing |
| GitHub | repositories, packages | nothing (10 searches/min) |
| Wikipedia | stable facts | nothing |

A failing backend **falls through** to the next one, and every failure is named:
an empty answer reads `nothing found — stackexchange (Stack Exchange): quota
exhausted, ddg (DuckDuckGo): HTTP 503`, never a bare "no results". `--ptt
doctor` reports what has actually been **observed** — `search: ddg ok (2s ago),
stackexchange ok (3s ago, quota 291/300), hn untried …` — so a backend nothing
has asked says `untried` instead of being assumed healthy.

Set a **SearXNG address** in **Settings → Permissions** if you run one (default
`http://127.0.0.1:8888`; empty uses the keyless backends only). Nothing is sent
to a third party for a search: the keyless backends are called directly, and a
local SearXNG keeps the query on this machine.

**Reading a page** (`read_page`, or `web_search` with *read the top results*)
fetches on **this machine first**; the text says which was used — `via local
fetch` or `via Jina Reader (third-party) — a cached snapshot`. The hosted reader
is only used when the local fetch yields nothing usable (the site blocked us, or
a big page with no text, i.e. a JavaScript shell) — a page that is simply short
is read here. Loopback, link-local, private-range and `.local` addresses are
refused, including a public name that **resolves** to one, so a pasted or
injected URL cannot read your router's admin page or a cloud metadata endpoint
into the conversation.

### Remote control (CLI)

Any of these work from a script or keybind, even while the bubble runs:

```bash
python ~/.local/bin/handsoff.py --ptt status      # state, handsfree, model
python ~/.local/bin/handsoff.py --ptt health      # JSON: mic + brain + TTS + deployment
python ~/.local/bin/handsoff.py --ptt level       # JSON: live voice level + which producer fed it
python ~/.local/bin/handsoff.py --ptt doctor      # full diagnostic (works even when the bubble is dead)
python ~/.local/bin/handsoff.py --ptt toggle      # start/stop/interrupt
python ~/.local/bin/handsoff.py --ptt interrupt   # silence it now
python ~/.local/bin/handsoff.py --ptt handsfree   # toggle hands-free
python ~/.local/bin/handsoff.py --ptt handsfree-status  # speak mic state
python ~/.local/bin/handsoff.py --ptt settings    # open Settings
python ~/.local/bin/handsoff.py --ptt clear-history  # forget the stored conversation
                 # (Settings does this for you when the brain model changes, so a
                 #  new model cannot parrot the old transcript)
```

## Trust & self-diagnosis

**Deployment hashes** — every health snapshot carries a `deployment` section:
sha256 of the running code, the installed `~/.local/bin` copy, and the
checkout, plus the installer's `~/.config/handsoff/deployment.json` manifest.
`installed-drift` means the running product is not the tested source —
re-run `install.sh`. The installer writes the manifest; the doctor, the
`--ptt health` line, and the Settings health bar all surface it.

**Doctor** — `python ~/.local/bin/handsoff.py --ptt doctor` (or the
`handsoff_doctor` tool) runs one diagnostic pass: deployment status, Ollama
reachability, TTS/STT readiness, mic visibility, niri IPC, ydotool, restart
script, systemd unit `Restart=`, and the crash log. It works even when the
bubble is dead.

**Decision log** — every tool decision lands in
`~/.local/state/handsoff/decisions.jsonl` with an action id, timestamp, tool,
target, decision (`ALLOW`/`DENY`/`CONFIRM`/`DRY-RUN`) and result, so "why did
it do that" always has an answer.

**Remote brain guard** — pointing `ollama_host` at a non-loopback server sends
your conversation history, voice transcripts, screenshots, and tool schemas
off this machine. The bubble refuses every brain request until you check
*Allow a remote server* in Settings → Brain (`allow_remote_ollama: true`) or
set `HANDSOFF_ALLOW_REMOTE_OLLAMA=1`; a warning is logged either way, and
`--ptt doctor` shows the status and **which channel** opted in (`brain privacy:
REMOTE — explicitly allowed (allow_remote_ollama in Settings)`), because the
environment variable is a second opt-in that Settings cannot display.

**Credential paths are refused** — anything a tool reads can end up in the
conversation (and, with a remote brain, off the machine), so `read_file`,
`watch_file` and `run_command`'s arguments all share one denylist: `~/.ssh`,
`~/.gnupg`, `~/.aws`, `~/.kube`, `~/.docker`, `~/.password-store`, browser
profiles and keyrings, shell history, and secret-shaped names (`*.pem`,
`*.key`, `*.env`, `credentials`, …). The check runs on the resolved path, so
`~/.ssh/../.ssh/id_rsa` and symlinks are caught too, and it applies to `cat` as
well — otherwise the shell whitelist would reopen exactly what `read_file`
refuses. Ask the bubble to *edit* one of your own files instead, or read the
file yourself. Public keys (`id_rsa.pub`) are still readable; they are not
secrets.

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
| **Notifications** | future desktop notification reader; off by default |
| **Pomodoro / Watchers** | work-break timer and bounded file/process monitoring |
| **Quantum Space desk** | read the sessions open in Quantum Space and the tail of what they said — one switch for all four `quant_space_*` tools. Quantum Space keeps its own Control setting and its own allow-list on top of this, and refuses in its own words until you allow the assistant there |

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
- **Centralized policy** — per-tool `ALLOW` / `DENY` / `CONFIRM`
  (`command_policy` in settings.json or the Permissions tab, one dropdown row
  per declared tool). `DENY` refuses
  before anything runs, regardless of permission switches; `CONFIRM` offers
  out loud and runs only after a separate next-turn `confirm_action('yes')`
  — one-turn separation, the same two-step pattern as `kill_process`.
- **Dry-run mode** — with `dry_run: true` the desktop-action tools
  (run_command, open_app, close_window, focus_window, workspace, typing,
  scroll, clicks) *report* what they would do instead of doing it: rehearse
  a scripted sequence before letting it act.

## Configuration

`~/.config/handsoff/settings.json` — edited via the Settings app. Highlights:

| Key | Default | Meaning |
|---|---|---|
| `model` | `qwen3:8b` | any Ollama model tag |
| `allow_remote_ollama` | false | explicit opt-in for a non-loopback Ollama server (see *Remote brain guard*) |
| `num_ctx` | 32768 | context window |
| `history_tokens` | 0 (auto) | history budget; auto = ctx − prompt − reserve |
| `whisper_size` | `tiny` | STT size; `small` is a good speed/accuracy middle |
| `assistant_name` / `wake_word_required` / `engage_seconds` | assistant / false / 45 | wake word behavior |
| `followup_seconds` | 6 (0 = off) | announce-and-listen: seconds after a reply it re-listens without the wake word |
| `wake_spotter` / `spotter_models` | false / `["hey_jarvis"]` | openWakeWord spotter |
| `handsfree` | false | continuous listening |
| `mic_device` / `mic_threshold` | system default / 600 | microphone |
| `home_place` / `calendar_ics` / `briefing` | — | weather place, ICS sources (**https:// or a local path** — a Google "secret iCal address" is a bearer token, so plain `http://` is refused unless it points at localhost), morning briefing (mentions mic problems since last time) |
| `resource_alerts` / `ram_alert_percent` / `vram_alert_percent` | false / 90 / 90 | opt-in crossing alerts for system RAM and NVIDIA VRAM |
| `notification_reader` / `notification_mute_apps` | false / [] | opt-in future desktop notification reader and muted app names; if `dbus-monitor` dies repeatedly the reader stops **and turns itself off** rather than claiming to still be on |
| `workspace_aliases` | `{}` | e.g. `{"code": "2"}` → "go to code" |
| `command_policy` | `{}` | per-tool `ALLOW`/`DENY`/`CONFIRM`; empty = all ALLOW |
| `confirm_seconds` | 90 | how long a CONFIRM offer stays valid |
| `dry_run` | false | desktop actions report instead of act |
| `permissions` | all true | the switches above |

## Files

| Path | Contents |
|---|---|
| `~/.config/handsoff/settings.json` | settings |
| `~/.config/handsoff/history.json` | conversation memory (survives restarts) |
| `~/.config/handsoff/memory.json` | durable facts about you (survives trimming) |
| `~/.config/handsoff/whisper-model/` | STT model cache |
| `~/.config/handsoff/voice-clips/` | voice reference clips for TTS cloning (pick one in Settings → Voice) |
| `~/.local/state/handsoff/reminders.json` | pending reminders |
| `~/.local/state/handsoff/decisions.jsonl` | one JSON line per tool-policy decision (capped) |
| `~/.config/handsoff/deployment.json` | installer manifest: source/installed sha256 per file |
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
- **Live level meter** (Settings → Voice): the same voice-level signal the
  bubble's designs paint from, polled over the bubble's own control socket
  (`--ptt level` prints exactly what the meter shows). *Raw* is what the audio
  pipeline last emitted and *designs* is the smoothed value the bubble is
  animating with, so a moving raw bar next to a stuck designs tick means the
  feed arrives but the bubble is not showing it. `Last above zero` exposes a
  feed that has gone silent. The *Live test* button above it checks the
  microphone hardware instead; this one checks the bubble's feed.
- **Live health bar** (Settings, bottom of the window): while the settings
  app is open, a status line polls the running bubble every 3 s and shows
  its mic state, brain (Ollama) reachability, and TTS/STT readiness —
  mic problems turn it orange.
- Pick a specific device in Settings → *Input device*; raise *Recording
  threshold* if it triggers on noise, lower it if speech is missed
- **Auto-recover** (Settings → Voice, on by default): if the mic stays
  silent or unusable for over a minute while hands-free is on, the bubble
  restarts its capture stream automatically and *says so out loud*; after
  3 failed attempts it keeps journaling until the mic recovers, then
  re-arms. Toggle it off with *Auto-recover the microphone*.
- The listener retries forever and never permanently disables hands-free;
  if PortAudio wedges in-process, restarting the service reinitializes it
- `journalctl --user -u handsoff.service | grep "mic health"` shows a health
  summary hourly **and immediately** on any state change (silent, stalled,
  open-failing, recovery — degraded states log at WARNING, so
  `journalctl -p warning` filters to just the problems)
- Every mic state transition is also persisted to
  `~/.local/state/handsoff/mic-health.json` (capped at 200 entries), and the
  morning briefing mentions any silent / open-failing / stalled episodes
  since its last delivery — so overnight mic trouble greets you with the
  weather
- Known issue on some stacks: a USB mic that can't serve 16 kHz callback
  streams breaks the listener when set as system default — keep the default
  on a device that works and select the other mic in handsoff's Settings

**"I do not have access to…" / tool errors**

- Is Ollama up? `systemctl status ollama`; is the model pulled?
  `ollama list` (pull with `ollama pull <model>`)
- First question after boot can take a few seconds while the model warms
  into VRAM (the bubble does this automatically at startup)

**Hands-free doesn't react**

- `--ptt status` shows `handsfree=on/off` — toggle with `Mod+Shift+H` or the socket
- The wake gate needs the assistant's name; the audio spotter (if enabled)
  wakes on its stock keywords instead ("hey jarvis"…)
- Saying "stop" while it speaks intentionally produces no reply

**Settings app won't open** — `Mod+Shift+S` or
`python ~/.local/bin/handsoff-settings.py` works even when the bubble is dead.

**Music tools report MPD down** — `systemctl --user start mpd`.

## Recovery

- **Bad deploy?** The installer gates every release through a staged copy
  (byte-compile + schema-import smoke) before switching `~/.local/bin`, and
  keeps the previous set at `~/.config/handsoff/releases/prev`:
  `./install.sh --rollback` restores it (a failed switch auto-restores).
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

The gates CI runs are also one command here — same jobs, same order, same
environment, and the same failure digest the pipeline prints. It is the local
answer to "is this push green?", for when the pipeline's minutes are spent or
you would rather know before pushing:

```bash
bash ci/gates.sh                        # every gate, in CI's order (~10 min)
bash ci/gates.sh --no-order             # skip the two ordering re-runs (~5 min)
bash ci/gates.sh shell compile smoke    # only the fast gates (seconds)
```

Each gate is the pipeline's job, run against your own interpreter and the
dependencies the bubble already uses (it installs nothing):

```bash
python -m pytest tests/ -q              # 1317 tests
python -m py_compile handsoff.py handsoff-settings.py
bash -n install.sh

# Re-run in a different order — the suite must not care what order it runs in.
# Seeded and reproducible: the seed is printed in the run header and summary.
HANDSOFF_TEST_ORDER_SEED=1234 python -m pytest tests/ -q   # tests shuffled
HANDSOFF_TEST_ORDER_FILES=7  python -m pytest tests/ -q   # FILE order only
```

Reproduce the coverage gate the way CI runs it — `.coveragerc` sets `parallel =
True` and the offscreen-GUI scenarios are measured in **child processes**, which
only start coverage when `COVERAGE_PROCESS_START` is exported (and they need the
absolute `COVERAGE_FILE` so their shards are combined). Without those two
variables the same suite reports ~60% and "fails" the 70% floor even though
nothing is broken:

```bash
COVERAGE_PROCESS_START="$PWD/.coveragerc" COVERAGE_FILE="$PWD/.coverage" \
  python -m pytest tests/ -q --cov=. --cov-config=.coveragerc \
  --cov-report=term-missing --cov-fail-under=70     # 1317 tests, 83.7%
```

The suite is self-contained: it imports the bubble against a throw-away
config/state directory, so it runs the code against the built-in defaults
rather than your `~/.config/handsoff/settings.json`, and never writes to your
real config, history or control socket. Each test also starts from the same
module state — including the dependency-injection host that `core.tools` and
`core.doctor` read, which a second loaded monolith (the settings-app suites load
one) would otherwise leave pointing at the wrong instance — and fails if it
leaves a stoppable worker thread (watcher, drainer, pomodoro, notification
reader) running. So a test cannot pass because of a neighbour that ran earlier,
or pass while leaking.

Order is not part of the contract either, and collection order is exactly where
a dependence on it hides. CI therefore re-runs the whole suite in two seeded
modes: tests shuffled, and only the file order shuffled (each file's own
sequence intact, which mimics what a developer sees and reads far more clearly
when it fires). Both seeds come from the commit SHA, so the order differs from
commit to commit while a red run stays exactly reproducible from the seed the
banner prints.

CI (`.github/workflows/ci.yml`, mirrored gate-for-gate in `.gitlab-ci.yml` for
the GitLab remote) runs exactly these gates on every push: the suite on
Python 3.12 and 3.13 (offscreen Qt, no audio hardware needed), a coverage
floor job, an ordering-dependence probe (the suite re-run with the tests
shuffled, then with only the file order shuffled), byte-compilation of every
source file, shell syntax checks with supply-chain pin guards, and an installer
smoke test. Background-thread exceptions fail the run via `pytest.ini` rather
than passing silently. Every shell script is checked by SHEBANG rather than by a
list or an extension glob — the two workflows' lists had already drifted apart,
and `handsoff-restart` has no extension to glob. The same gates run locally as
`bash ci/gates.sh`: the pipeline is a finite resource, and a gate that cannot
run is not a gate.

**When the pipeline goes red, read the summary before the log.** Each suite job
writes a junit report (GitLab's native *Test summary* tab and merge-request test
widget), and `ci/pytest_summary.py` prints a short digest — failing test names
with their messages, plus the known Debian-slim environment fixes — in its own
log section, opened automatically on failure and folded away when green. Set a
masked `GITLAB_SUMMARY_TOKEN` variable and the same digest is posted as a merge
request note, where its Markdown renders (`--post`); a CI job token cannot
create notes, so without the variable posting is skipped and the job is
unaffected.
The suite includes an **installed-copy smoke test**: a fake `~/.local/bin`
deployment is booted offscreen and poked over the control socket, so a
checkout that works but deploys broken cannot slip through.

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
settings app is separate (`handsoff-settings.py`); its **History** tab
shows the conversation memory, the durable facts (`memory.json`) with a
forget action, and the tool-decision log (`decisions.jsonl`) — so "why did
it say/do that" has a GUI answer. `tests/fake_ollama.py` fakes the Ollama
API for the streaming tests.

Known limitations: the ICS parser handles DAILY/WEEKLY recurrence plus
EXDATE and RECURRENCE-ID (canceled/moved instances of recurring events are
suppressed/replaced correctly); calendar RRULE MONTHLY/YEARLY fall back to
the single occurrence. See `GAP_ANALYSIS.md` for the current roadmap.
