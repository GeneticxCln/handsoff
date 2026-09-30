# handsoff

A self-modifying voice assistant that lives as a small round bubble on your
desktop (Arch Linux / CachyOS + niri Wayland). Hold the bubble, speak, release:
it transcribes locally (faster-whisper), answers with a local Ollama model and
speaks back with chatterbox-turbo — its own voice, or a clone of a clip you
pick. Nothing leaves the machine unless you turn that on — the two switches
that can send something out are **off** in a fresh install, and the sentence
is enforced rather than promised: *Internet knowledge* (your search text, the
fact you ask about, and your home place go to DuckDuckGo, Wikipedia,
StackExchange, Hacker News, GitHub and Open-Meteo) and *Third-party page
reader* (when a page cannot be read locally, its address is handed to
r.jina.ai). Both are in Settings → Permissions. Speech, memory, reminders,
the calendar, the desk and every desktop action stay local either way.

![the bubble, cycling its four states](docs/bubble.gif)

*That is the real widget, rendered offscreen by
[`docs/render_bubble_gif.py`](docs/render_bubble_gif.py) — the code that ships,
not a mock-up. Two things in it are synthetic, and both are named: the voice
level a microphone would feed the designs is a shaped envelope per state, and
the backdrop is a dark gradient rather than your wallpaper. Colours are the
module's own: blue idle · red listening · orange thinking · green speaking.*

## What it does

- **Voice** — push-to-talk, or hands-free with a wake word and an optional
  openWakeWord spotter (~80 ms). *Announce-and-listen* re-listens for a few
  seconds after each reply, so a follow-up needs no wake word.
- **Desktop** — volume, brightness, windows, workspaces, launching apps,
  screenshots, typing, compositor hotkeys.
- **Music** — full MPD control: play/pause, search, volume, now playing.
- **Reminders & calendar** — spoken, persistent, snoozable; ICS sources.
- **Knowledge** — weather, web search, facts: tool-driven, never guessed.
- **Memory** — durable facts about you, surviving restarts and trimming.
- **Self-modification** — it can edit its own source, restart into the change,
  and tell you if the new version crashed.
- **Ambient** — opt-in notification reading, Pomodoro, RAM/VRAM alerts, bounded
  file/process watchers.
- **Your Quantum Space desk** — *"what is Claude doing?"* answered from the
  desk itself: which sessions are open and the tail of what each printed,
  read-only over the local channel it opens, and only once it allows this
  assistant by name.

52 tools, declared in one place (`@tool` methods in `core/tools.py`), so
schemas, the system prompt and the permissions cannot drift apart.

## Requirements

Arch Linux or CachyOS with **niri** · a microphone and speakers · an NVIDIA
GPU is strongly recommended (a 26B model wants ~14 GB VRAM; smaller models run
on less).

## Install

```bash
cd ~/Projects/handsoff && ./install.sh
```

It upgrades the system, installs the Python extras, puts `handsoff.py`,
`handsoff-settings.py` and `handsoff-restart` in `~/.local/bin`, downloads a
SHA256-verified whisper model and the speech weights, writes a systemd **user**
service (`Restart=always`), and merges the niri window rule into your
`config.kdl` — it takes a `.bak-handsoff` first, rewrites only its own marked
block, and reloads the compositor if one is running.

The pacman step is the only one that needs your password. For a script, a CI
job, or an agent that cannot answer a prompt, pass `--skip-system-packages`
when those packages are already present — the probes still run and still report
what was found, nothing is installed, and the ollama service is left alone
rather than being started with `sudo` later:

```bash
./install.sh --skip-system-packages
```
Exactly one autostart owner: with the unit enabled the installer will not also
add `spawn-at-startup` to niri.

```bash
systemctl --user start handsoff
```

The window rule lands in a block marked `>>> handsoff managed block`, so a
re-install rewrites that block instead of stacking a second copy, and a block
you pasted in by hand from an older install is replaced rather than left
beside it. To undo the whole thing, restore
`~/.config/niri/config.kdl.bak-handsoff`.

Before installing: `HANDSOFF_MODEL` (default `qwen3:8b`), `HANDSOFF_WHISPER`
(`tiny`…`large-v3`), `HANDSOFF_TTS_REPO`.

## Use it

- **Hold** the left button to talk, release to send. **Drag** to move it.
  **Click** while it speaks to barge in. **Right-click** for restart, settings,
  quit.
- **Fourteen designs** in Settings → Appearance, each reacting to your voice in
  its own way, plus size, an animation-energy scale, colour accents,
  **Match wallpaper**, one-click looks, and image or **design packs** (one
  picture or animation per state, `.hpack` files, preview-before-install). It
  all applies live and nothing extra is stored — the current look is derived.

| Key | Action |
|---|---|
| `Mod+V` | talk toggle: record / send / interrupt |
| `Mod+Shift+V` | interrupt immediately |
| `Mod+Shift+H` | toggle hands-free |
| `Mod+Shift+S` | Settings (works even if the bubble is dead) |

**Voice**: say its name to engage, then 45 s of free conversation; the name may
land mid-sentence and is stripped. *"stop" / "quiet" / "never mind"* silences it
with no LLM involved. Try: *"what's the weather in Berlin?"* · *"remind me to
call mum at 18:30"* → *"snooze 10 minutes"* · *"play some jazz"* · *"open
firefox and type hello into the search box"* · *"move this window to workspace
3"* · *"make your bubble pink"* (it edits itself and restarts).

**Online** (keyless, with a local SearXNG preferred): SearXNG · DuckDuckGo ·
Stack Exchange · Hacker News · GitHub · Wikipedia. A failing backend falls
through to the next and *names* it — `nothing found — ddg (DuckDuckGo): HTTP
503`, never a bare "no results". Page reading fetches on this machine first and
refuses loopback, link-local, private-range and `.local` addresses, including a
public name that *resolves* to one.

**From a script or keybind**, while the bubble runs:

```bash
handsoff.py --ptt status | health | level | doctor | toggle | interrupt \
            | handsfree | settings | clear-history
```

`--ptt doctor` works even when the bubble is dead, and is the first thing to run
when something is wrong.

## Trust

- **Everything is local by default.** Pointing `ollama_host` off-loopback sends
  your history, transcripts, screenshots and tool schemas off the machine, and
  the brain is refused until you allow a remote server in Settings (or set
  `HANDSOFF_ALLOW_REMOTE_OLLAMA=1`). The doctor names **which** channel opted
  in, because the environment variable is a second door Settings cannot show.
- **Credential paths are refused** by one shared denylist across `read_file`,
  `watch_file` and `run_command` — `~/.ssh`, `~/.gnupg`, cloud credentials,
  browser profiles and keyrings, shell history, `*.pem`/`*.key`/`*.env`/
  `credentials` — resolved before the check, so `..` and symlinks are caught,
  and applied to `cat` too, or the shell whitelist would reopen exactly what
  `read_file` refuses. Public keys stay readable.
- **Deployment hashes** — every health snapshot carries sha256s of the running
  code, the installed copy and the checkout; `installed-drift` means re-run
  `install.sh`.
- **Decision log** — every tool decision lands in `decisions.jsonl` with its
  action id, target, verdict and result, so "why did it do that" has an answer.
  The Settings **History** tab shows it next to the conversation and the durable
  facts.

## Permissions

Every dangerous tool has a switch in Settings → Permissions: run commands, read
files, edit files, self-restart, type text, press keys, web access, music (MPD),
screen access, notifications (off), pomodoro/watchers, and the Quantum Space
desk. On top of the switches, enforced in code:

- No pipes, `;`, `&&`, backticks or command substitution in `run_command`; a
  blocklist (sudo, rm, pacman, systemctl, curl…) beats the whitelist.
- `niri … spawn` cannot launch interpreters or pass code flags — checked on the
  executable's basename, so `/usr/bin/niri` gets the same scrutiny as `niri`.
- Typing and key presses refuse when the focused window cannot be verified, and
  always refuse terminals.
- `edit_file` stays inside its own source and config dir; a self-edit must keep
  its marker line and compile.
- **Per-tool policy** — `ALLOW` / `DENY` / `CONFIRM` per tool. `DENY` refuses
  before anything runs; `CONFIRM` offers out loud and runs only after a
  separate next-turn `confirm_action('yes')`. **Dry-run** mode makes the
  desktop-action tools report what they would do instead of doing it.

### What still leaves, even with a local SearXNG

A SearXNG on your own machine moves only the *first* stop of some queries, and
only while it is up. Measured against the routing in `core/web.py`:

| tool | where it goes | what it sends |
|---|---|---|
| `web_search`, general | SearXNG → `lite.duckduckgo.com` → `en.wikipedia.org` | your query text |
| `web_search`, news-shaped ("news", "today", "price"…) | SearXNG → `lite.duckduckgo.com` → `hn.algolia.com` → `en.wikipedia.org` | your query text |
| `web_search`, a question containing "error", "api", "pip", "docker", "python"… (checked before the repo rule) | `api.stackexchange.com` → `hn.algolia.com` → `api.github.com` — SearXNG is the 4th fallback | your query text |
| `web_search`, a question containing "repo", "github", "changelog"… and no tech word | `api.github.com` **first** | your query text |
| `get_weather` | `geocoding-api.open-meteo.com`, then `api.open-meteo.com` | your `home_place`, then its coordinates |
| `lookup_fact` | `en.wikipedia.org` | the thing you asked about |
| `world_events` | `lite.duckduckgo.com` — never SearXNG | fixed queries, plus the last part of `home_place` |
| `read_page` | the site itself | the URL, and it sees your IP. The r.jina.ai fallback is its own switch, off by default |

The proactive senders — the morning briefing and the `world_warnings` poll —
ride the same `web_access` permission the tools check, so with the knowledge
switch off they fetch nothing at all.

So a local SearXNG is worth running and is not a wall: two of the three search
routes do not start there, every route falls through to DuckDuckGo when it is
down, and weather and facts go out. It is not a complete
answer either — SearXNG queries the engines it is configured with, so an engine
still sees the text. What it removes is *handsoff* sending it.

Everything else — speech, memory, reminders, the calendar, the desk, music and
every desktop action — is local whatever is switched on.

## Configure

`~/.config/handsoff/settings.json`, edited by the settings app. The keys worth
knowing: `model`, `whisper_size`, `assistant_name`, `handsfree`, `engage_seconds`,
`followup_seconds`, `wake_spotter`, `mic_device`/`mic_threshold`,
`allow_remote_ollama`, `num_ctx`, `command_policy`, `confirm_seconds`,
`dry_run`, `calendar_ics` (https:// or a local path — a Google "secret iCal
address" is a bearer token, so plain http:// is refused off localhost),
`resource_alerts`, `notification_reader`, `workspace_aliases`.

## Where things live

| Path | |
|---|---|
| `~/.config/handsoff/` | `settings.json`, `history.json`, `memory.json`, `deployment.json`, `design-packs/`, `voice-clips/`, whisper cache |
| `~/.local/state/handsoff/` | `reminders.json`, `decisions.jsonl`, `handsoff.log`, `crash.log`, `control.sock` (0600) |
| `specs/` | the requirements, architecture, tool census, data, ops and test plan — the detail this README points at rather than repeats |
| `GAP_ANALYSIS.md` | the audit ledger and the current roadmap |

## When it misbehaves

```bash
systemctl --user status handsoff          # is it running?
journalctl --user -u handsoff -n 50       # last 50 lines
handsoff.py --ptt doctor                  # the full diagnostic
handsoff-restart                          # always safe: stops, waits for the lock, starts
```

- **Bubble won't start** — the service restarts itself; the next start says why
  it crashed. Check `crash.log` for a native crash.
- **"already running"** — another instance holds the lock; use
  `handsoff-restart`.
- **Mic problems** — `pactl list sources short`, and Settings → Voice's *Live
  test* (transcribes through the bubble's own model) and *Live level meter*
  (`--ptt level`) tell you whether the hardware or the feed is at fault.
  Auto-recover restarts a silent capture stream by itself and says so out loud.
- **Tool errors** — is Ollama up and the model pulled (`ollama list`)? The first
  question after boot is slow while the model warms into VRAM.
- **Hands-free ignores you** — `--ptt status` for the flag, and remember a
  *custom* name is woken by the transcript, not the audio spotter; "stop" while
  it speaks is meant to do nothing.

**Recovery**: a bad deploy rolls back with `./install.sh --rollback` (a failed
switch auto-restores); a bad self-edit has a `.bak` beside it; `./install.sh
--uninstall` keeps your data, `--purge` backs it up to
`~/handsoff-backup-<date>.tar.gz` first and then wipes it.

## Development

```bash
bash ci/gates.sh                     # every gate, in CI's order (~10 min)
bash ci/gates.sh compile lint links shell    # the fast ones (seconds)
python -m pytest tests/ -q           # 3119 tests
git config core.hooksPath githooks   # then every commit runs the suite too
```

`ci/gates.sh` runs the pipeline's jobs against your own interpreter and
installs nothing; `lint` SKIPs with an install hint when ruff is absent,
because a gate that passes because it never ran is not a gate. The suite is
self-contained: a throw-away config and state directory, and a test that leaves a
running worker thread fails.

Reproduce the coverage gate as CI runs it — without these two variables the same
suite reads ~60% and "fails" the floor for no reason:

```bash
COVERAGE_PROCESS_START="$PWD/.coveragerc" COVERAGE_FILE="$PWD/.coverage" \
  python -m pytest tests/ -q --cov=. --cov-config=.coveragerc \
  --cov-report=term-missing --cov-fail-under=70   # 3119 tests, ~86% measured
```

CI (GitHub, mirrored gate-for-gate in `.gitlab-ci.yml`) runs the suite on
Python 3.12–3.14, the coverage floor, an ordering-dependence probe, byte
compilation, ruff, shell syntax and an installer smoke test on every push.

Layout: `handsoff.py` is config → prompt → Ollama client → audio → tools →
`Assistant` → bubble → `main()`; the settings app is separate; `core/` holds
the parts with their own contracts (tools, web, the desk client); `ci/` holds
the gates.

## License

MIT — see [LICENSE](LICENSE).
