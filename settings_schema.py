"""handsoff settings schema — the single source of shared defaults.

handsoff.py re-exports this as ``DEFAULT_SETTINGS`` (so ``H.DEFAULT_SETTINGS``
keeps working for tests and any self-edited code), and handsoff-settings.py
imports it directly — the settings GUI no longer needs to load the whole
bubble module just to merge defaults.

Precedence elsewhere: built-in defaults <- environment <- settings.json.
"""
from __future__ import annotations

from typing import NamedTuple

SETTINGS_VERSION: int = 3   # bumped on incompatible settings.json layout changes
                            # v2: piper_voice -> tts_reference (Piper -> chatterbox)
                            # v3: the two knowledge switches default OFF, and an
                            #     install carrying the v2 defaults gets them
                            #     turned off rather than inheriting a leak
                            #     nobody chose (see core/settings.py _migrate)

# Keys a past version wrote that this build deliberately retired. They are
# dropped from settings.json on the next WRITE as well as on load, because
# otherwise a read-merge-write puts them straight back: the loader then warns
# `unknown settings key` on every single start, for a key the user never wrote.
#
# This list is deliberately explicit and narrow. Dropping *every* key the schema
# does not know would be simpler, and wrong: a settings.json written by a NEWER
# build carries keys this build has never heard of, and erasing them on save
# destroys configuration on version skew. Unknown-but-not-retired keys survive a
# write untouched; the loader keeps warning about them, which is how the user
# learns a newer build wrote them.
RETIRED_SETTINGS = ("piper_voice",)

# The bubble shapes. `image` is the odd one out and deliberately so: it is not a
# painter, it is the user's own file, drawn to the SAME rules as the painters so
# that importing a picture does not mean leaving the design contract:
#
#   fit      the image's circumscribed circle is fitted inside the aperture, so
#            a rotation cannot push a corner past the glass (a circle does not
#            change under rotation) and the ink guard passes by construction;
#   colour   the state colour owns every opaque pixel — the image contributes
#            alpha and luminance, with a 0.45 floor so a dark picture can never
#            make the state colour invisible (the `void` defect, 8 visible px of
#            45 796, in a new costume);
#   voice    its own reaction (breath, ignite, wobble, rim glow), neutral at
#            silence like every other painter;
#   no file  a dashed frame in the state colour, never a fall back to the orb —
#            an orb there is indistinguishable from a missing dispatch branch.
#
# `design_image_path` and `design_pack` below are what it draws: one file, or a
# pack (a folder with a `pack.json` and one picture per state) when several
# pictures should switch together as the bubble changes state.
BUBBLE_DESIGNS = ("orb", "halo", "reactor", "bloom", "droplet", "cube", "equalizer", "crystal", "saturn", "void", "sauron", "pikachu", "cat", "image")
# Decoration drawn AROUND the avatar picture (the `image` design). A closed
# set: junk coerces to the default in core/settings, so a typo cannot leave the
# avatar half-decorated. Each name is an ANIMATION, not a still.
AVATAR_DECOS = ("off", "ring-light", "orbit", "pulse", "aurora", "rainbow",
                "sparkle", "comet", "neon", "flames")
# What the decoration is COLOURED with, independent of the state. A closed set
# of two WORDS — "state" (the state colour, the default: a decoration that
# cannot hide which state the bubble is in) and "rainbow" (a hue that sweeps on
# its own, so the ring is a colour of its own rather than a copy of the mood) —
# plus a literal "#RRGGBB", which is the third answer and cannot be spelled as
# a word. `core.settings` accepts the two words or a hex and coerces anything
# else, so a typo cannot leave the ring half-coloured.
AVATAR_DECO_COLORS = ("state", "rainbow")
# How the picture is coloured. "state" washes it in the state colour (the
# original behaviour: a photo reads as the bubble's mood), "natural" keeps the
# art's OWN colours — the only setting under which a drawn character can be
# yellow, since the wash otherwise paints every silhouette the state hue.
AVATAR_TINTS = ("state", "natural")
# The four states the bubble paints, in the order the Appearance tab shows
# them. `colors` is keyed by these names, the preview draws one slot per name,
# and the `image` design takes one picture per name — so the vocabulary exists
# once, here, instead of being respelled in the schema, the renderer and the GUI.
BUBBLE_STATES = ("idle", "listening", "thinking", "speaking")

# The `image` design's per-state settings, derived from the names above so a
# rename cannot desync the setting from the state it belongs to.
DESIGN_IMAGE_KEYS = tuple(f"design_image_{state}" for state in BUBBLE_STATES)


# ------------------------------------------------------------------ looks
# One-click whole looks for the Appearance tab: a look sets the five keys the
# tab already owns — design, window size, animation energy, colour accent and
# the four state colours — through the same live-apply path a slider uses.
#
# There is deliberately NO ``appearance_look`` setting. A stored name beside
# the values it claims to describe is a second source of truth that can
# disagree with them (the GUI reading "Neon" while the bubble renders Midnight),
# which is the exact failure this tree keeps removing. `look_matching()` DERIVES
# the current look from the values, so it cannot lie: it returns "" whenever
# the settings spell out no look at all, and the tab shows "Custom".
#
# Every value here must satisfy the bounds the loader enforces (see
# core/settings.py) and every colour must be parseable — a look that writes a
# value the loader would reject turns one click into a silent no-op, so
# tests/test_settings.py validates the catalogue against those same rules
# rather than trusting it. Look names are also unique, and the `handsoff` look
# is EXACTLY the shipped defaults, so a fresh install reads as a look instead
# of as Custom.
APPEARANCE_LOOKS: tuple = (
    {"name": "handsoff", "label": "Handsoff", "design": "orb",
     "bubble_size": 128, "animation_energy": 1.0, "bubble_accent": 0.5,
     "colors": {"idle": "#4f8cff", "listening": "#ff4d5e",
                "thinking": "#ff9e2c", "speaking": "#3ecf6e"},
     "note": "the shipped look — the defaults, one click away"},
    {"name": "midnight", "label": "Midnight", "design": "orb",
     "bubble_size": 132, "animation_energy": 0.7, "bubble_accent": 0.35,
     "colors": {"idle": "#3f57d6", "listening": "#b03a6e",
                "thinking": "#7b5cf0", "speaking": "#2f9e95"},
     "note": "cool, slow and quiet — for a dark wallpaper"},
    {"name": "daylight", "label": "Daylight", "design": "halo",
     "bubble_size": 128, "animation_energy": 0.9, "bubble_accent": 0.85,
     "colors": {"idle": "#1b4bbf", "listening": "#c01f2e",
                "thinking": "#b3660a", "speaking": "#137a3f"},
     "note": "the default palette darkened — for a light wallpaper"},
    {"name": "ember", "label": "Ember", "design": "reactor",
     "bubble_size": 144, "animation_energy": 1.7, "bubble_accent": 0.9,
     "colors": {"idle": "#ff7a1a", "listening": "#e0231c",
                "thinking": "#ffc14d", "speaking": "#ff5a2e"},
     "note": "hot, fast and wide awake"},
    {"name": "neon", "label": "Neon", "design": "equalizer",
     "bubble_size": 136, "animation_energy": 1.9, "bubble_accent": 1.0,
     "colors": {"idle": "#00e5ff", "listening": "#ff2da0",
                "thinking": "#c77dff", "speaking": "#7cff3d"},
     "note": "loud: maximum saturation, speed and glow"},
    {"name": "allseeing", "label": "All-seeing", "design": "sauron",
     "bubble_size": 144, "animation_energy": 1.4, "bubble_accent": 0.8,
     "colors": {"idle": "#b06a12", "listening": "#ff3b14",
                "thinking": "#ffb02e", "speaking": "#ff7a3d"},
     "note": "the Eye keeps its own fire — these tint the corona"},
    {"name": "spark", "label": "Spark", "design": "pikachu",
     "bubble_size": 144, "animation_energy": 1.6, "bubble_accent": 0.9,
     "colors": {"idle": "#3b6fe0", "listening": "#e0402e",
                "thinking": "#e0a92a", "speaking": "#2fbf6a"},
     "note": "Pikachu keeps its fur — these tint the aura"},
    {"name": "curious", "label": "Curious", "design": "cat",
     "bubble_size": 144, "animation_energy": 1.1, "bubble_accent": 0.6,
     "colors": {"idle": "#5aa9e6", "listening": "#ef5d7a",
                "thinking": "#f0a44a", "speaking": "#57c98b"},
     "note": "the cat: ears up, tail moving, body in the state colour"},
)


def look_names() -> tuple:
    """The catalogue's names, in display order."""
    return tuple(entry["name"] for entry in APPEARANCE_LOOKS)


def look(name: str):
    """The catalogue entry `name` names, or None."""
    wanted = str(name or "").strip().lower()
    for entry in APPEARANCE_LOOKS:
        if entry["name"] == wanted:
            return entry
    return None


def look_setting_key(name: str) -> str:
    """The SETTING a catalogue key names.

    The catalogue writes `design` where the setting is `bubble_design` — the
    one place the two vocabularies differ. The mapping is DERIVED rather than
    listed (a key that is already a setting stays as it is; otherwise the
    `bubble_`-prefixed name is checked), so a renamed setting cannot leave the
    catalogue pointing at a key that no longer exists: `labels` are checked by
    `tests/test_settings_contract.py` against the table itself.
    """
    text = str(name or "").strip()
    if text in DEFAULT_SETTINGS or not text:
        return text
    candidate = f"bubble_{text}"
    return candidate if candidate in DEFAULT_SETTINGS else text


def _look_color(value) -> str:
    """A colour in a comparable form ('#rrggbb' lower case, or as written)."""
    text = str(value or "").strip().lower()
    if not text:
        return text
    return text if text.startswith("#") else "#" + text


def _look_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def look_matching(settings) -> str:
    """The look whose five values equal `settings`, or "" when none does.

    Derived on purpose (see the catalogue comment): the Appearance tab calls
    this after every change, so "which look is current" is answered by the
    settings themselves. A value that is not a number can never match, hence
    the NaN comparison — junk must read as Custom rather than crashing the tab.
    """
    if not isinstance(settings, dict):
        return ""
    colors = settings.get("colors")
    colors = colors if isinstance(colors, dict) else {}
    # Compared across the WHOLE entry rather than the five fields it happens to
    # hold today: a value the catalogue gains later (another key a look sets)
    # takes part in the match by existing, instead of being forgotten here —
    # this is how "Neon" could be shown while the bubble rendered something
    # else.
    for entry in APPEARANCE_LOOKS:
        if all(_look_matches(settings, colors, key, value)
               for key, value in entry.items()
               if key not in ("name", "label", "note")):
            return entry["name"]
    return ""


def _look_matches(settings, colors: dict, key: str, want) -> bool:
    """Does `settings` hold `want` for the catalogue key `key`?"""
    if key == "colors":
        if not isinstance(want, dict):
            return False
        return all(_look_color(colors.get(name)) == _look_color(value)
                   for name, value in want.items())
    got = settings.get(look_setting_key(key))
    if isinstance(want, (int, float)) and not isinstance(want, bool):
        # NaN never matches, which is what makes a junk value read as Custom
        # rather than raising inside the tab.
        return abs(_look_number(got) - float(want)) <= 1e-6
    return str(got or "").strip().lower() == str(want).strip().lower()

DEFAULT_SETTINGS: dict = {
    "ollama_host": "http://127.0.0.1:11434",
    "allow_remote_ollama": False,  # explicit opt-in for a non-loopback server
    "model": "qwen3:8b",
    "num_ctx": 32768,
    "history_tokens": 0,       # 0 = auto: ctx − prompt − reply reserve
    "whisper_size": "tiny",
    "whisper_device": "auto",   # auto=GPU when free VRAM fits, else cpu; or forced "cpu"/"cuda"
    "tts_reference": "",   # optional >=5 s clip to clone; "" = built-in voice
    "tts_rate": 1.0,       # applied by resampling chatterbox's 24 kHz output
    "tts_volume": 1.0,
    "mic_device": "",
    "mic_threshold": 600,
    "handsfree": False,
    "bubble_size": 128,
    "bubble_design": "orb",  # Appearance tab; must be one of BUBBLE_DESIGNS above
                              # (APPEARANCE_LOOKS below sets this with the rest)
    "design_image_path": "",   # the `image` design's art: any file Qt can decode
                               # (PNG/JPEG/WebP/SVG). Empty or unreadable = a
                               # dashed placeholder frame, and the reason is
                               # reported by `design_image_problem()`. This is
                               # the FALLBACK state's picture: a state below with
                               # a picture of its own uses that instead.
    # One picture per state, chosen in the same card. A state with its own file
    # draws it; a state with none uses `design_image_path`; a state with neither
    # draws the empty slot. This is the per-state form of the art WITHOUT a pack
    # folder (DESIGN_IMAGE_KEYS above names the four).
    "design_image_idle": "",
    "design_image_listening": "",
    "design_image_thinking": "",
    "design_image_speaking": "",
    "design_pack": "",         # the same art as a PACK: one folder name under
                               # design-packs/ naming a picture per state. When
                               # set it is the authority and every picture above
                               # is ignored; the reason it cannot draw is
                               # reported by `pack_problem()`.
    "avatar_ring": "ring-light",  # decoration AROUND the avatar: one of
                               # AVATAR_DECOS above (off / ring light / orbit /
                               # pulse / aurora). Each is animated, brightens
                               # and quickens with the voice, and carries the
                               # state colour. Applies to the `image` design.
    "avatar_deco_color": "state",  # what the decoration is coloured with:
                               # AVATAR_DECO_COLORS above ("state" tracks the
                               # state colour, "rainbow" sweeps a hue of its
                               # own) or a literal "#RRGGBB" the user picked.
                               # INDEPENDENT of the state on purpose: the rim
                               # still carries the state colour, so a ring of
                               # the user's choosing cannot hide the mood.
    "avatar_tint": "state",    # one of AVATAR_TINTS above: "state" washes the
                               # picture in the state colour, "natural" keeps
                               # the picture's OWN colours (the state is then
                               # carried by the rim and the decoration), which
                               # is what a drawn character needs to stay itself.
    "bubble_accent": 0.5,      # 0..1 accent punch: how hard each shape leans on its
                               # state colour (glow alpha, saturation, comet light)
    "animation_energy": 1.0,   # 0.2..2.0 global animation scale: orbit speed, swirl
                               # speed, hue sweep and comet brightness. 1.0 = the
                               # default feel (the old 'Subtle' preset sits mid-scale)
    "colors": {
        "idle": "#4f8cff", "listening": "#ff4d5e",
        "thinking": "#ff9e2c", "speaking": "#3ecf6e",
    },
    # The local SearXNG the search router prefers when one answers. A local
    # instance is the only way to search the whole web keylessly without a
    # hosted middleman: public instances answer non-browsers with a Cloudflare
    # challenge (measured), so "just use a public one" is not an option. Empty
    # disables the attempt — the router still has the keyless backends — and the
    # instance is probed by a localhost connect, never assumed from this string.
    "searxng_url": "http://127.0.0.1:8888",
    "permissions": {
        "run_command": True, "read_file": True,
        "edit_file": True, "self_restart": True,
        "type_text": True, "press_keys": True,
        # OFF by default, and this is the README's promise made true rather
        # than a caution that is easier to ignore. The audit of 2026-09-27
        # measured a stock install with this key ON: the user had never turned
        # anything on, and `web_search` was already sending their literal query
        # to DuckDuckGo Lite, StackExchange, HN Algolia and the GitHub API,
        # `lookup_fact` to Wikipedia and `get_weather` their `home_place` to
        # Open-Meteo — against README.md:7, "Nothing leaves the machine unless
        # you turn that on."
        #
        # So the first line of code that turns a switch on must be the USER.
        # Everything degrades honestly from here: the schemas are filtered out
        # of the model's tool list entirely (core.tools.permitted_tools), and
        # `_switched_off_families` puts the family name in the system prompt so
        # a request for the weather is answered by naming the switch rather
        # than by guessing. The setting is a checkbox in Settings → Permissions
        # labelled "Internet knowledge", and the label now names the endpoints
        # instead of calling them "fixed".
        "web_access": False,
        # `read_page`'s third-party fallback is a SEPARATE switch, also off.
        # When a local fetch yields nothing usable, the reader can hand the
        # target URL to r.jina.ai — a different kind of leak from a search
        # query, because it discloses WHICH page the user is on, not what they
        # asked. Folding it into `web_access` meant one checkbox whose label
        # ("read-only, fixed endpoints") described neither. Defaulting this off
        # is what makes README.md:7 true of the shipped configuration: with
        # both keys off, nothing leaves the machine.
        "hosted_reader": False,
        "media": True,
        "screen_access": True,
        "operator": False,    # mouse control (click_element/click_at) — OFF by
                              # default; enabling lets the AI move and click
                              # the real pointer
        "paste_text": True,   # reading the user's clipboard gets its own switch
        "copy_text": True,    # writing the user's clipboard
        "reminders": True,    # create/list/cancel/snooze spoken reminders
        "calendar": True,     # read ICS calendars, print month grids
        "focus_window": True,  # raise/focus arbitrary windows by name
        "get_datetime": True,  # trivially safe; kept gated for uniformity
        "notifications": False,  # desktop notifications are private by default
        "pomodoro": True,
        "watchers": True,
        "quant_space": True,  # read the Quantum Space desk (which sessions are
                               # open, and the tail of what they said). ON, but
                               # it is the SECOND switch: the desk keeps its own
                               # Control setting and its own allow-list, and
                               # refuses with a sentence naming Settings →
                               # Control until the user consents THERE.
    },
    "extra_allowed_commands": [],
    "tool_call_times": None,          # filled per-ToolBelt: deque of monotonic times
    "max_tool_calls": 0,             # 0 = no limit; set an int to rate-limit tool calls
    "command_policy": {},            # tool -> ALLOW | DENY | CONFIRM (empty = all ALLOW)
    "confirm_seconds": 90.0,         # how long a CONFIRM offer stays valid
    "dry_run": False,                # desktop actions report instead of act
    "streaming_tts": True,
    "autostart": False,
    "assistant_name": "assistant",
    "wake_word_required": False,
    "engage_seconds": 45.0,
    "workspace_aliases": {},   # 'code': '2' → "go to code" just works
    "home_place": "",          # weather without naming a place; powers the briefing
    "calendar_ics": [],        # ICS source(s): https URL(s) and/or .ics file paths
    "wake_spotter": False,     # openWakeWord audio spotter (near-zero CPU wake)
    "mic_selfheal": True,      # auto-restart a wedged mic + spoken explanation
    "dictation": True,         # 'start dictation' types transcripts, no LLM turn
    "spotter_models": ["hey_jarvis"],   # stock: alexa, hey_jarvis, hey_mycroft, timer, weather
    "followup_seconds": 6.0,   # announce-and-listen: no-wake-word window after a reply
    "briefing": False,         # daily briefing on the first wake word
    "world_warnings": False,   # opt-in proactive severe world-event warnings
    "world_cooldown_min": 60.0,  # min minutes between world warnings
    "hardware_watch": False,   # opt-in live hardware watch on the health tick
    "hardware_cooldown_min": 60.0,  # min minutes between hardware urgents
    "hardware_disk_gb": 5.0,   # disk-free floor (GiB) for the low-disk warning
    "resource_alerts": False,  # opt-in RAM/VRAM threshold announcements
    "ram_alert_percent": 90.0,
    "vram_alert_percent": 90.0,
    "idle_release_seconds": 600,  # give the models' memory back after this
                                  # much quiet; 0 = never release
    "llm_release_wait_s_per_gb": 20.0,  # keep the LLM when handing its memory
                                  # back would cost more than this much
                                  # next-turn wait per GB freed; 0 = never weigh
                                  # the cost (always release)
    "vram_pressure_floor_mb": 1024,  # shorten the idle release while the card
                                  # has less than this much free; 0 = never
                                  # rush, always use the full window
    "vram_pressure_seconds": 30,  # the quiet needed while below that floor
    "speech_yields_to_llm": True,  # a turn that cannot fit its LLM may ask
                                   # the speech model to give the card back
                                   # (it reloads in seconds)
    "notification_reader": False,  # desktop notifications are private by default
    "self_watch": False,           # the agent watching itself (announcements off
                                   # by default; --ptt health carries the section
                                   # either way)
    "notification_mute_apps": [],
}


# ------------------------------------------------------- the settings contract
# ONE row per setting, and every layer reads it: `core.settings.coerce_settings`
# is generated from these rows, and the Appearance tab derives both the keys its
# live-apply watches and the names it reports from them.
#
# Why a table instead of four edits. A setting used to be declared in
# DEFAULT_SETTINGS, coerced in core/settings.py, collected from its widget in
# handsoff-settings.py, and listed again in the tab's live-apply tuple — and the
# last one drifted twice (`colors`, then `avatar_deco_color`), each time as "I
# changed it and nothing happened". A new key that is missing from this table
# now FAILS THE SUITE (`tests/test_settings_contract.py`) instead of failing
# silently in a tab.
#
# The DEFAULT is deliberately NOT repeated here: it lives in DEFAULT_SETTINGS
# and is looked up by key, so there is exactly one place to change a shipped
# value. A row that also carried it would be the fifth list, not the first.
#
# The generated coercion was compared against the hand-written one it replaces
# on 929 crafted inputs (every key probed with junk, wrong types, out-of-range
# numbers and null; plus whole-file shapes). 896 cases are byte-identical, and
# the 33 that differ are THREE deliberate corrections, each of them the old code
# being wrong — all pinned by tests/test_settings_contract.py:
#   * `autostart` was never coerced at all, so a hand-edited `"false"` read as
#     True (the fail-open this table exists to stop);
#   * a JSON null for a text setting became the literal string "None" — a model
#     named "None", a host named "None";
#   * the branch meant to strip `mic_device` stripped `tts_reference` twice
#     instead, so "  Yeti  " kept its spaces and matched no device.
#
# kind        what the coercion does (see core.settings._apply_field)
#   bool      strict reader, default when a junk truthy string appears
#   int/float clamp into [lo, hi], warn on junk
#   text      strip; `fallback=True` uses the default when empty
#   str       must already be a str, else the default (warn)
#   path      expanduser + strip, never validated (a moved file must not erase
#             the choice)
#   choice    one of `choices`, else the default (`warn=True` says so aloud)
#   colour    one of `choices`, or a literal #RRGGBB
#   str_list  list of non-empty strings, capped at `cap`, `lower=` if case folds
#   custom    `coerce=` names a function in core.settings; the row still
#             DECLARES the key, so the contract guard covers it either way
# live        part of the Appearance tab's live-apply set
# label       what a live apply calls it ("Applied live: <label>")
# tab       which page of the settings window owns the control ("" = none:
#           the key is written by the app at runtime, not by a person)
# title     the form label. A live apply calls the setting its `label`, which
#           is shorter ("size" vs "Bubble size"), so the two are separate.
# ctrl      the control to build, one of CONTROL_NAMES; "" derives it from the
#           kind, "custom" means a bespoke panel in the window owns the key,
#           "none" means no widget at all (runtime bookkeeping).
# tip       the tooltip, when the control needs one.
# suffix/step/zero/scale/unit/decimals
#           how a control PRESENTS its value: what follows it, how far one step
#           moves, what zero reads as, and — for sliders — the multiplier to the
#           widget's integer range plus the text shown beside it.
# show_scale  what the row says beside a slider is the value times this, for the
#           setting whose storage and whose wording count differently (the
#           accent is stored as a fraction and read as a percentage)
# render    the SHAPE that draws this row, when a generated control cannot
#           express it — one of RENDER_NAMES. A row whose `ctrl` is "custom"
#           must name one, or be drawn as another row's companion (`also`); a
#           row that keeps its generated control may name one too, in which case
#           the shape draws the row AND is responsible for placing that control
#           (the microphone test is the switch plus a meter and a live test).
# explain   a paragraph the row shows under itself, in the words a person reads
# also      settings drawn WITH this row, underneath it — `("key", "short label")`
#           pairs, or bare keys when the shape names them itself (the four
#           state pictures). A claimed row is skipped at its own table position,
#           so it is drawn exactly once, and its card is this row's card.
# also_label  what the companion line says
# placeholder  the greyed example a text control shows while it is empty
# height    how tall a multi-line box is, in pixels (0 = the control's own)
# choice_labels  `(value, words)` pairs for a combo, when a bare value is not
#           something a person should have to read ("natural" -> "Original
#           colours"). Presentation only: what is stored is the value.
class Field(NamedTuple):
    key: str
    kind: str
    lo: float = None
    hi: float = None
    choices: tuple = ()
    cap: int = None
    lower: bool = False
    warn: bool = True
    fallback: bool = False
    live: bool = False
    label: str = ""
    coerce: str = ""
    rstrip: str = ""
    note: str = ""
    tab: str = ""
    title: str = ""
    #: which CARD of that page the row lives in (see PAGE_GROUPS). The page is a
    #: list of cards and the rows are the table's, so a new setting lands in its
    #: card without a line of window code — the placement that used to be the
    #: third hand-written edit.
    group: str = ""
    ctrl: str = ""
    tip: str = ""
    suffix: str = ""
    step: float = None
    zero: str = ""
    scale: int = 1
    unit: str = ""
    decimals: int = 0
    show_scale: int = 1
    #: the SHAPE that draws this row (see RENDER_NAMES); "" = the control below
    render: str = ""
    #: a paragraph under the row, for the setting that needs explaining
    explain: str = ""
    #: settings drawn WITH this one: (key, short label) pairs, or bare keys
    also: tuple = ()
    also_label: str = ""
    placeholder: str = ""
    height: int = 0
    #: (value, words) pairs, so a choice can read in a person's words
    choice_labels: tuple = ()


def _f(key: str, kind: str, **kw) -> Field:
    return Field(key, kind, **kw)


#: What a control is called in a Field row. `auto` picks from the kind, and the
#: window implements every one of these by name — so a row that names a control
#: nothing implements is a settings window that cannot draw the setting, which
#: the contract guard fails on rather than discovering later.
CONTROL_NAMES = ("checkbox", "spin", "slider", "line", "combo", "lines",
                 "commas", "custom", "none")

#: kind -> control, for a row that names none. A kind with no entry ("custom",
#: "colour") has no obvious one control — its row must say which it is, so the
#: contract guard refuses a row that would otherwise draw nothing.
KIND_CONTROLS = {
    "bool": "checkbox",
    "int": "spin",
    "float": "spin",
    "text": "line",
    "str": "line",
    "path": "line",
    "choice": "combo",
    "str_list": "lines",
}


#: The SHAPES a row can ask to be drawn in, when a generated control cannot
#: express it. Same contract as CONTROL_NAMES: the window implements every one of
#: these by name, so a row naming a shape nothing implements is a setting with no
#: widget — which the contract guard fails on rather than discovering later.
#:
#: The names say what the row LOOKS like, never which setting it is: a shape that
#: reads a list from the running system, a grid, a picker. That is the point of
#: putting them here — the window keeps a vocabulary of shapes, and the table
#: decides which row wants which, so no page names a setting any more.
RENDER_NAMES = (
    "model-list",        # the picker filled from the running server
    "device-list",       # the picker filled from the audio server
    "mic-test",          # a control plus its meter, test and live transcript
    "clip-picker",       # a control plus choose/play/clear for a sound file
    "alias-editor",      # an add/remove list of name -> path pairs
    "permission-grid",   # one checkbox per permission the schema declares
    "policy-rows",       # one ALLOW/DENY/CONFIRM row per declared tool
    "design-picker",     # the combo filled from the bubble's designs
    "state-pictures",    # one picture per state, on one row
    "fallback-image",    # the picture a state with none of its own draws
    "pack-picker",       # a folder of pictures that switch together
    "deco-picker",       # the decoration, plus what the chosen one does
    "deco-colour",       # the decoration's own colour, with its swatch
    "colour-grid",       # one swatch per state
    "autostart",         # a switch whose state is the DESKTOP's, not the file's
)


def control_for(field: Field) -> str:
    """The control a row asks for; its kind decides when it names none."""
    return field.ctrl or KIND_CONTROLS.get(field.kind, "")


def renderer_for(field: Field) -> str:
    """The shape that draws one row; "" when the row's own control does."""
    return field.render


def rendered_keys() -> tuple:
    """The rows the window draws in a named shape, in table order."""
    return tuple(field.key for field in SETTINGS_FIELDS if field.render)


def also_pairs(field: Field) -> tuple:
    """[(key, label)] drawn WITH this row, labels optional.

    A bare key means the SHAPE names the companion itself — the four state
    pictures are labelled by their state, which the table already says, so
    spelling a label per picture here would be a second copy of it.
    """
    out = []
    for entry in field.also:
        if isinstance(entry, str):
            out.append((entry, ""))
        else:
            key, label = entry
            out.append((str(key), str(label)))
    return tuple(out)


def claimed_keys() -> dict:
    """key -> the row it is drawn WITH, for every companion.

    A claimed row is skipped at its own table position, so it reaches the screen
    once and in the card that owns it. The contract guard checks the claims are
    well formed (declared keys, no row claiming itself, nobody claimed twice,
    and every companion in its owner's card).
    """
    claims: dict = {}
    for field in SETTINGS_FIELDS:
        for key, _label in also_pairs(field):
            claims[key] = field.key
    return claims


def bespoke_keys() -> frozenset:
    """The rows the window must draw ITSELF: their control is `custom`.

    Derived from the table rather than written out, so the window's declaration
    of "what I drew" and the table cannot drift; what a guard can still catch is
    a custom row that nothing draws (see `rendered_keys` and `claimed_keys`).
    """
    return frozenset(field.key for field in SETTINGS_FIELDS
                     if control_for(field) == "custom")


def generated_keys() -> tuple:
    """The settings the window must GENERATE a control for, in table order.

    Everything that is not here is either drawn by name (a row whose control is
    `custom`) or has no control at all (`none`, runtime bookkeeping) — and the
    three sets together have to account for every setting in the table, which is
    what the contract and GUI guards check. So this is the one list the window's
    `_controls` is held against: a row that reaches this list and produces no
    widget is a setting no one can change, and the window logs the skip instead
    of failing (it is the tool you open when the bubble is broken).
    """
    return tuple(field.key for field in SETTINGS_FIELDS
                 if control_for(field) not in ("none", "custom", ""))


def field_title(field: Field) -> str:
    """The form label: the declared title, else the key in words."""
    if field.title:
        return field.title
    words = field.key.replace("_", " ")
    return words[:1].upper() + words[1:]


#: The whisper model sizes the loader will accept.
WHISPER_SIZES = ("tiny", "base", "small", "medium", "large",
                 "large-v1", "large-v2", "large-v3", "turbo")
#: Roughly what each size costs to download. It lives HERE, beside the list of
#: choices, because it is one of the things a person is choosing between — and
#: because the row that shows it can then spell its choices from the same table
#: instead of a second one in the window keyed by setting name.
WHISPER_SIZE_HINT = {
    "tiny": "~75 MB", "tiny.en": "~75 MB", "base": "~145 MB",
    "base.en": "~145 MB", "small": "~500 MB", "medium": "~1.5 GB",
    "large-v3": "~3 GB",
}
#: Backends `whisper_device` may name.
WHISPER_DEVICES = ("auto", "cpu", "cuda")
#: The three answers `command_policy` accepts per tool.
POLICY_RULES = ("ALLOW", "DENY", "CONFIRM")

#: The CARDS each page is made of, in the order they are drawn:
#: (page, group, title). A page is its cards and its cards are these, so the
#: window renders `PAGE_GROUPS` and nothing else — a row lands in its card by
#: naming it in `group=`, and the cards appear IN THIS ORDER, which is what makes
#: "a new setting needs one line in the table" true for placement too, not just
#: for the widget, the load and the save. The ORDER here is the draw order, so a
#: page never has to build a card and then move it. A group with NO rows is a
#: card a panel draws whole (the live mic level, the wallpaper match, the look
#: tiles): the window supplies it by name, and `the_pages_are_the_tables_cards
#: _in_the_tables_order` holds the two sets against each other — so a declared
#: card nothing draws (an empty frame) or a panel registered for a card that has
#: rows (which swallows them) fails instead of shipping.
#:
#: The fourth element is the line a card shows under its title, in the words a
#: person reads. It is table data for the same reason the title is: it belongs to
#: the card, not to whichever page happens to draw it, and it is the last piece
#: of a card that used to live in handsoff-settings.py.
PAGE_GROUPS: tuple = (
    ("brain", "server", "Ollama server", ""),
    ("voice", "microphone", "Microphone", ""),
    ("voice", "level", "Live level (from the bubble)", ""),
    ("voice", "hands-free", "Hands-free listening", ""),
    ("voice", "aliases", "Workspace aliases (voice shortcuts)", ""),
    ("voice", "stt", "Speech-to-text (faster-whisper)", ""),
    ("voice", "tts", "Text-to-speech", ""),
    ("permissions", "tools", "Tools the assistant may use", ""),
    ("permissions", "extra", "Extra whitelisted commands (one per line)", ""),
    ("permissions", "policy",
     "Command policy (ALLOW / DENY / CONFIRM per tool)", ""),
    # The strip and the look tiles sit at the TOP of the page: the preview is
    # what every control below it is judged against, and a look is one click
    # that sets all four of them — so they are the first two cards the table
    # declares rather than cards built last and moved up with insertWidget.
    ("appearance", "preview", "Preview",
     "Idle \u00b7 listening \u00b7 thinking \u00b7 speaking — live, no Save "
     "needed."),
    ("appearance", "look", "Look",
     "One click sets the shape, the size, the motion and all four colours."),
    ("appearance", "shape", "Shape", ""),
    ("appearance", "decoration", "Decoration",
     "A light worn AROUND the avatar. It turns with the animation energy and "
     "brightens and quickens with your voice. Shows when the design is Image "
     "— a picture as the bubble."),
    ("appearance", "motion", "Motion",
     "Energy scales every design's speed; accent scales how hard it leans on "
     "the state colour."),
    ("appearance", "colours", "State colours",
     "One colour per state. Click a swatch to change it."),
    ("appearance", "desktop", "Match your desktop",
     "Samples your wallpaper and retunes all four colours so the bubble reads "
     "clearly against it."),
    ("startup", "autostart", "Autostart", ""),
)


def _group_row(page: str, group: str):
    """The declared row for one card, or None when nothing declares it."""
    for row in PAGE_GROUPS:
        if row[0] == page and row[1] == group:
            return row
    return None


def page_groups(page: str) -> tuple:
    """The cards of one page, in DRAW order."""
    return tuple(row[1] for row in PAGE_GROUPS if row[0] == page)


def group_title(page: str, group: str) -> str:
    """What the card says. A group with no title is a coding error."""
    row = _group_row(page, group)
    return row[2] if row else group


def group_hint(page: str, group: str) -> str:
    """The line a card shows under its title ("" when it shows none)."""
    row = _group_row(page, group)
    return row[3] if row and len(row) > 3 else ""


def group_fields(page: str, group: str) -> tuple:
    """The rows of one card, in the table's own order."""
    return tuple(field for field in SETTINGS_FIELDS
                 if field.tab == page and field.group == group)


def panel_groups() -> frozenset:
    """The cards a PANEL draws: declared, and holding no rows of their own."""
    return frozenset((row[0], row[1]) for row in PAGE_GROUPS
                     if not group_fields(row[0], row[1]))


SETTINGS_FIELDS: tuple = (
    # -- brain ------------------------------------------------------------------
    _f("ollama_host", "text", fallback=True, tab="brain", title="Server URL", group="server"),
    _f("allow_remote_ollama", "custom", coerce="allow_remote_ollama", tab="brain",
       title="Allow a remote server (send history, screenshots & schemas off "
             "this machine)", ctrl="checkbox",
       tip="handsoff refuses to talk to a non-loopback Ollama server until this "
           "is checked (or HANDSOFF_ALLOW_REMOTE_OLLAMA=1 is set). The bubble's "
           "conversation history, voice transcripts, and tool schemas leave "
           "your machine when the server is remote.", group="server"),
    _f("model", "text", fallback=True, tab="brain", title="Model",
       ctrl="custom", render="model-list",
       tip="The list is filled from the server, and picking a row applies it "
           "immediately — no Save needed.", group="server"),
    _f("num_ctx", "int", lo=1024, hi=2 ** 20, tab="brain",
       title="Context size", suffix=" tokens", step=1024, group="server"),
    _f("history_tokens", "int", lo=0, hi=2 ** 20, tab="brain",
       title="History budget", suffix=" tokens", step=512,
       tip="0 = automatic (context size minus the system prompt + tool schemas "
           "and a reply reserve). History is trimmed to this token budget so "
           "long conversations never exceed the model's context.", group="server"),
    _f("max_tool_calls", "int", lo=0, hi=10_000, tab="brain",
       title="Tool-call rate limit", suffix=" /min", zero="unlimited",
       tip="Safety limit on tool calls per minute (0 = unlimited). Stops the "
           "AI if it ever gets stuck in a tool-calling loop.", group="server"),
    # -- voice ------------------------------------------------------------------
    _f("mic_device", "str", tab="voice", title="Input device", ctrl="custom",
       render="device-list",
       tip="The device list comes from the audio server, so it is filled at "
           "open time rather than drawn from the table.", group="microphone"),
    _f("mic_threshold", "int", lo=50, hi=10_000, tab="voice",
       title="Recording threshold", step=50, render="mic-test",
       tip="Peak loudness needed to accept a recording (noise gate).", group="microphone"),
    _f("handsfree", "bool", tab="voice",
       title="Continuous listening — talk without pressing anything",
       explain="When on, the microphone stays open and the bubble turns red "
               "while you speak; after a short pause your utterance is sent "
               "automatically. The mic is muted while the assistant talks, so "
               "it never hears itself.",
       tip="A voice-activity gate detects speech and auto-sends each utterance.", group="hands-free"),
    _f("assistant_name", "text", fallback=True, warn=False, tab="voice",
       title="Assistant name", group="hands-free"),
    _f("wake_word_required", "bool", tab="voice",
       title="Only listen when addressed by name (\u201chey assistant\u201d)",
       tip="On: the assistant ignores everything until you say its name, then "
           "stays engaged for the window below.", group="hands-free"),
    _f("engage_seconds", "float", lo=5.0, hi=600.0, tab="voice",
       title="Stay engaged after the wake word", suffix=" s", group="hands-free"),
    _f("followup_seconds", "float", lo=0.0, hi=120.0, tab="voice",
       title="Follow-up window after a reply", suffix=" s", zero="Off",
       tip="After each spoken reply, listen for one follow-up without the wake "
           "word (0 = off).", group="hands-free"),
    _f("home_place", "text", tab="voice", title="Home place (weather)", group="hands-free"),
    _f("briefing", "bool", tab="voice",
       title="Morning briefing — weather on the first \u201chello\u201d each day",
       tip="Needs a home place. On the first conversational utterance each day, "
           "weather (plus calendar and mic problems) is gathered and spoken "
           "first.", group="hands-free"),
    _f("world_warnings", "bool", tab="voice",
       title="World warnings — speak up about severe world events as they "
             "break",
       tip="Opt-in. Checks breaking-news and severe-weather headlines on the "
           "existing tick and speaks only what looks severe.", group="hands-free"),
    _f("world_cooldown_min", "float", lo=5.0, hi=1440.0, tab="voice",
       title="Minutes between world warnings", suffix=" min", group="hands-free"),
    _f("hardware_watch", "bool", tab="voice",
       title="Hardware watch — note machine changes, warn when critical",
       # its two limits are drawn WITH it rather than as rows of their own, so
       # the card reads as one setting with its numbers under it
       also=(("hardware_cooldown_min", "Urgent cooldown"),
             ("hardware_disk_gb", "Disk floor")),
       also_label="Hardware watch limits",
       tip="Opt-in. Samples cheap health signals on the existing tick and "
           "speaks only what crosses a threshold.", group="hands-free"),
    _f("hardware_cooldown_min", "float", lo=5.0, hi=1440.0, tab="voice",
       title="Minutes between hardware warnings", suffix=" min", group="hands-free"),
    _f("hardware_disk_gb", "float", lo=0.5, hi=1000.0, tab="voice",
       title="Warn when free disk drops below", suffix=" GiB", group="hands-free"),
    _f("calendar_ics", "custom", coerce="calendar_ics", tab="voice",
       title="Calendar .ics files", ctrl="commas",
       tip="One or more .ics URLs or file paths, comma separated.", group="hands-free"),
    _f("wake_spotter", "bool", tab="voice",
       title="Audio wake spotter — detect the keyword before speech-to-text",
       tip="Uses openWakeWord (about 1 ms of CPU per chunk) to detect the wake "
           "phrase locally, so a wake word costs no transcription.", group="hands-free"),
    _f("spotter_models", "custom", coerce="spotter_models", tab="voice",
       title="Wake words to detect", ctrl="commas",
       tip="openWakeWord model names, comma separated. The stock set fills the "
           "table's cap, so a model added past it is dropped — the loader "
           "warns when that happens.", group="hands-free"),
    _f("mic_selfheal", "bool", tab="voice",
       title="Auto-recover the microphone — restart the audio stream and say so",
       tip="If the microphone stays silent or unusable for over a minute while "
           "it should be live, the capture stream is reopened.", group="hands-free"),
    _f("resource_alerts", "bool", tab="voice",
       title="Resource alerts — warn when RAM or GPU memory is nearly full",
       also=(("ram_alert_percent", "RAM"), ("vram_alert_percent", "GPU")),
       also_label="Alert thresholds",
       tip="Opt-in spoken alerts on threshold crossings. Alerts fire once while "
           "the pressure lasts, not once per tick.", group="hands-free"),
    _f("ram_alert_percent", "float", lo=50.0, hi=99.0, tab="voice",
       title="RAM alert threshold", suffix=" %", group="hands-free"),
    _f("vram_alert_percent", "float", lo=50.0, hi=99.0, tab="voice",
       title="VRAM alert threshold", suffix=" %", group="hands-free"),
    _f("idle_release_seconds", "int", lo=0, hi=86400, step=60, tab="voice",
       title="Free memory when idle", suffix=" s",
       tip="Unload the speech model (about 3 GB of GPU memory) and ask Ollama "
           "to drop the model after this much quiet. They load again on the "
           "next question, so a short setting costs a slower first word after "
           "idle — and a long one keeps your card full for as long as the "
           "bubble sits idle. 0 = never release.", group="hands-free"),
    _f("llm_release_wait_s_per_gb", "float", lo=0.0, hi=3600.0, step=5.0,
       tab="voice",
       title="Keep the model if reloading it costs more than this",
       suffix=" s/GB",
       tip="The speech model is always unloaded (a few seconds to reload). "
           "The language model is only dropped when handing its memory back "
           "is worth the wait: this is the most next-turn seconds one GB of "
           "freed GPU memory may cost. A model that fits on the card reloads "
           "in seconds and is released; a big one Ollama has split across CPU "
           "and GPU can take minutes to come back, so it is kept. 0 = never "
           "weigh the cost. The doctor line `llm memory` shows what the "
           "current model would do.", group="hands-free"),
    _f("vram_pressure_floor_mb", "int", lo=0, hi=16384, step=256, tab="voice",
       title="Free memory early when VRAM is below this", suffix=" MB",
       tip="An idle release normally waits the whole quiet window. Below this "
           "much free GPU memory it stops waiting and uses the shorter window "
           "below, so a card the desktop is struggling on empties as soon as "
           "nothing is being said or written. The models load again on the "
           "next question. 0 = never rush (always the normal window). The "
           "doctor line `gpu headroom` shows the free memory this compares "
           "against.", group="hands-free"),
    _f("vram_pressure_seconds", "int", lo=0, hi=3600, step=5, tab="voice",
       title="Quiet needed while VRAM is below that", suffix=" s",
       tip="The window the idle release uses while the card is below the floor "
           "above. 0 = release at the first tick that finds the bubble idle. "
           "It is clamped to the normal window (it can only make a release "
           "EARLIER), and it is ignored when the floor is 0 or the release "
           "itself is off.", group="hands-free"),
    _f("speech_yields_to_llm", "bool", tab="voice",
       title="Let a turn borrow the speech model's GPU memory",
       tip="The mirror of the idle release: when a turn's language model does "
           "not fit on the card and the speech model is holding part of it, "
           "the speech model gives its memory back first and the answer is "
           "spoken with a freshly loaded voice — a few seconds — instead of "
           "Ollama offloading half the model to the CPU, where every token "
           "costs a multiple of the on-card price. Nothing is released when "
           "the claim already fits, when the model is already loaded, when "
           "the release would not make room, or while something is speaking. "
           "The doctor line `gpu headroom` shows what would be asked for.",
       group="hands-free"),
    _f("notification_reader", "bool", tab="voice",
       title="Read desktop notifications aloud (opt-in)",
       tip="Private by default. When enabled, future notifications are spoken; "
           "nothing already on screen is read, and no text is stored.", group="hands-free"),
    _f("self_watch", "bool", tab="voice",
       title="Watch my own worker threads and speak up when one dies or wedges",
       tip="The agent watching itself: the sampler inventories its own long-lived "
           "threads and their progress signals, and announces a dead or stuck "
           "component instead of failing quietly. Findings are also visible in "
           "--ptt health under self_watch.", group="hands-free"),
    _f("notification_mute_apps", "str_list", cap=32, lower=True, tab="voice",
       title="Apps never read aloud", ctrl="commas",
       tip="App names, comma separated, matched case-insensitively.", group="hands-free"),
    _f("dictation", "bool", tab="voice",
       title="Voice dictation — 'start dictation' types what you say into the "
             "focused window",
       tip="Zero-cost dictation: transcripts are typed into whatever window "
           "has focus, with no AI turn and no commands.", group="hands-free"),
    _f("streaming_tts", "bool", tab="voice",
       title="Speak while the model is still writing (streaming TTS)",
       tip="Sentences are spoken as they arrive instead of after the whole "
           "answer, so the reply starts sooner. Off: one voice clip per reply.", group="hands-free"),
    _f("workspace_aliases", "custom", coerce="workspace_aliases",
       tab="voice", title="Workspace aliases", ctrl="custom",
       render="alias-editor",
       explain="Say \u201chey assistant, go to code\u201d and it switches to the "
               "matching workspace. Numbers stay unchanged \u2014 pure voice "
               "alias, nothing is renamed.",
       tip="Its own editor: one 'name = workspace' per line, so an alias is "
           "readable rather than a dict on one line.", group="aliases"),
    _f("whisper_size", "choice", choices=WHISPER_SIZES, tab="voice",
       title="Speech-to-text model",
       # the size a person is choosing BETWEEN, beside the name — the same data
       # the table lists the choices from, so the two cannot disagree
       choice_labels=tuple((size, f"{size}  ({WHISPER_SIZE_HINT[size]})")
                           for size in WHISPER_SIZES
                           if size in WHISPER_SIZE_HINT),
       group="stt"),
    _f("whisper_device", "choice", choices=WHISPER_DEVICES, tab="voice",
       title="Speech-to-text device",
       tip="Where whisper runs. `auto` follows the model; `cpu` keeps the GPU "
           "free for the language model and voice.", group="stt"),
    _f("tts_reference", "str", tab="voice", title="Voice clip",
       render="clip-picker",
       explain="Optional: a clip longer than 5 seconds to clone. Leave empty "
               "for the built-in voice.", group="tts"),
    _f("tts_rate", "float", lo=0.5, hi=2.0, tab="voice", title="Speech rate",
       ctrl="slider", scale=100, unit="\u00d7", decimals=2, group="tts"),
    _f("tts_volume", "float", lo=0.1, hi=2.0, tab="voice", title="Volume",
       ctrl="slider", scale=100, unit="\u00d7", decimals=2, group="tts"),
    # -- search, tools and permissions ------------------------------------------
    _f("permissions", "custom", coerce="permissions", tab="permissions",
       title="Permissions", ctrl="custom", render="permission-grid",
       group="tools"),
    _f("searxng_url", "text", rstrip="/", tab="permissions",
       title="SearXNG URL",
       placeholder="http://127.0.0.1:8888   (empty: keyless backends only)",
       tip="A local SearXNG to search through, if you run one. Best results "
           "and no rate limits; the built-in readers are used when empty.", group="tools"),
    # `hosted_reader` is a PERMISSION, not a top-level row: the permission grid
    # renders the `permissions` dict and its own label table
    # (handsoff-settings.py:3204), which is why every other switch in here —
    # screen_access, operator, notifications — has no `SETTINGS_FIELDS` row
    # either. A top-level row would break the table/defaults contract in
    # tests/test_settings_contract.py, which is the test that stops a setting
    # being declared in one place and read from another. The runtime reads it
    # through `SETTINGS["permissions"]`; see the host seam in handsoff.py.
    _f("extra_allowed_commands", "str_list", cap=64, tab="permissions",
       title="Extra allowed commands", ctrl="lines", height=110,
       placeholder="e.g.\ngrep\ndate\nfree",
       tip="One command per line. It is ALLOWED outright, without asking — "
           "everything else keeps the policy above.", group="extra"),
    _f("command_policy", "custom", coerce="command_policy", tab="permissions",
       title="Per-tool policy", ctrl="custom", render="policy-rows",
       group="policy"),
    _f("dry_run", "bool", tab="permissions",
       title="Dry-run mode — desktop actions report what they would do, and "
             "change nothing",
       tip="Every desktop action is described instead of performed, which is "
           "the safe way to watch what the AI reaches for.", group="policy"),
    _f("confirm_seconds", "float", lo=5.0, hi=600.0, tab="permissions",
       title="Confirmation window", suffix=" s",
       tip="How long the bubble waits for a spoken yes/no after asking to do "
           "something consequential.", group="policy"),
    # -- appearance: everything the Appearance tab owns applies live ------------
    _f("bubble_design", "choice", choices=BUBBLE_DESIGNS, warn=False,
       # the ROW says "Design" because the CARD it sits in is already called
       # Shape: the row names the choice, the card names the thing
       live=True, label="shape", tab="appearance", title="Design",
       ctrl="custom", render="design-picker", group="shape"),
    # NOT custom any more: a two-word choice is a combo the table can build, and
    # it says in its own row how each word is spelled
    _f("avatar_tint", "choice", choices=AVATAR_TINTS, warn=False, live=True,
       label="picture colours", tab="appearance", title="Picture colours",
       choice_labels=(("state", "State colours"),
                      ("natural", "Original colours")),
       tip="What colour the picture itself is drawn in. \"State colours\" "
           "washes it in the state colour — a photo reads as the bubble's "
           "mood. \"Original colours\" keeps the art's own palette, which is "
           "what a drawn character needs to stay itself; the state is then "
           "carried by the rim and the decoration. Applies live.",
       group="shape"),
    # Four pictures are ONE choice drawn ON ONE ROW: the first state's row draws
    # all four (the shape names each by its state), and the other three are
    # claimed by it, so they are not drawn again at their own table positions.
    *[_f(f"design_image_{_state}", "path", live=True,
         label=f"{_state} picture", tab="appearance",
         title=f"{_state.title()} picture", ctrl="custom", group="shape",
         render="state-pictures" if _state == BUBBLE_STATES[0] else "",
         also=(tuple(f"design_image_{s}" for s in BUBBLE_STATES[1:])
               if _state == BUBBLE_STATES[0] else ()))
      for _state in BUBBLE_STATES],
    _f("design_image_path", "path", live=True, label="fallback image",
       tab="appearance", title="Fallback image", ctrl="custom",
       render="fallback-image", group="shape"),
    _f("design_pack", "text", live=True, label="design pack",
       tab="appearance", title="Design pack", ctrl="custom",
       render="pack-picker", group="shape"),
    _f("avatar_ring", "choice", choices=AVATAR_DECOS, warn=False,
       live=True, label="decoration", tab="appearance", title="Decoration",
       ctrl="custom", render="deco-picker", group="decoration"),
    _f("avatar_deco_color", "colour", choices=AVATAR_DECO_COLORS, warn=False,
       live=True, label="decoration colour", tab="appearance",
       title="Decoration colour", ctrl="custom", render="deco-colour",
       group="decoration"),
    _f("bubble_size", "int", lo=96, hi=192, live=True, label="size",
       tab="appearance", title="Size", ctrl="slider", unit="px", group="motion"),
    _f("animation_energy", "float", lo=0.2, hi=2.0, live=True,
       label="animation energy", tab="appearance", title="Energy",
       ctrl="slider", scale=100, unit="\u00d7", decimals=1,
       tip="Scales orbit speed, swirl speed, hue sweep and comet brightness "
           "together.", group="motion"),
    _f("bubble_accent", "float", lo=0.0, hi=1.0, live=True,
       label="colour accent", tab="appearance", title="Accent",
       # the setting is a fraction and the row says a percentage: the slider
       # counts in the same integers, the LABEL shows the number a person thinks in
       ctrl="slider", scale=100, unit="%", show_scale=100,
       tip="How hard each shape leans on its state colour: saturation and "
           "brightness follow the mood, and the accent sets how far.", group="motion"),
    _f("colors", "custom", coerce="colors", live=True, label="state colours",
       tab="appearance", title="State colours", ctrl="custom",
       render="colour-grid", group="colours"),
    # -- behaviour --------------------------------------------------------------
    _f("autostart", "bool", note="newly coerced (was raw)", tab="startup",
       title="Start handsoff when niri starts", ctrl="custom",
       render="autostart",
       tip="Adds one spawn-at-startup line to the niri config (a backup is "
           "written next to it before the first change).", group="autostart"),
    _f("tool_call_times", "custom", coerce="tool_call_times", ctrl="none",
       note="runtime bookkeeping: the rate limiter's own timestamps, never typed", group=""),
)


def fields_by_key() -> dict:
    """The table as a mapping, for the guard and for the coerce loop."""
    return {field.key: field for field in SETTINGS_FIELDS}


def live_keys() -> tuple:
    """Keys the Appearance tab's live apply watches — derived, never listed."""
    return tuple(field.key for field in SETTINGS_FIELDS if field.live)


def field_label(key: str) -> str:
    """What a live apply calls `key`: its declared label, else the key."""
    field = fields_by_key().get(key)
    return (field.label or key) if field else key


def field_kinds() -> tuple:
    """Every kind the table may use (the generator implements all of them)."""
    return ("bool", "int", "float", "text", "str", "path", "choice", "colour",
            "str_list", "custom")
