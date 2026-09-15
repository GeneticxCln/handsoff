"""handsoff settings schema — the single source of shared defaults.

handsoff.py re-exports this as ``DEFAULT_SETTINGS`` (so ``H.DEFAULT_SETTINGS``
keeps working for tests and any self-edited code), and handsoff-settings.py
imports it directly — the settings GUI no longer needs to load the whole
bubble module just to merge defaults.

Precedence elsewhere: built-in defaults <- environment <- settings.json.
"""
from __future__ import annotations

from typing import NamedTuple

SETTINGS_VERSION: int = 2   # bumped on incompatible settings.json layout changes
                            # v2: piper_voice -> tts_reference (Piper -> chatterbox)

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
        "web_access": True,
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
    "notification_reader": False,  # desktop notifications are private by default
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


def _f(key: str, kind: str, **kw) -> Field:
    return Field(key, kind, **kw)


#: The whisper model sizes the loader will accept.
WHISPER_SIZES = ("tiny", "base", "small", "medium", "large",
                 "large-v1", "large-v2", "large-v3", "turbo")
#: Backends `whisper_device` may name.
WHISPER_DEVICES = ("auto", "cpu", "cuda")
#: The three answers `command_policy` accepts per tool.
POLICY_RULES = ("ALLOW", "DENY", "CONFIRM")

SETTINGS_FIELDS: tuple = (
    # -- brain ---------------------------------------------------------------
    _f("ollama_host", "text", fallback=True),
    _f("allow_remote_ollama", "custom", coerce="allow_remote_ollama"),
    _f("model", "text", fallback=True),
    _f("num_ctx", "int", lo=1024, hi=2 ** 20),
    _f("history_tokens", "int", lo=0, hi=2 ** 20),
    # -- voice ---------------------------------------------------------------
    _f("whisper_size", "choice", choices=WHISPER_SIZES),
    _f("whisper_device", "choice", choices=WHISPER_DEVICES),
    _f("tts_reference", "str"),
    _f("tts_rate", "float", lo=0.5, hi=2.0),
    _f("tts_volume", "float", lo=0.1, hi=2.0),
    _f("mic_device", "str"),
    _f("mic_threshold", "int", lo=50, hi=10_000),
    _f("handsfree", "bool"),
    # -- appearance: everything the Appearance tab owns applies live ---------
    _f("bubble_size", "int", lo=96, hi=192, live=True, label="size"),
    _f("bubble_design", "choice", choices=BUBBLE_DESIGNS, warn=False,
       live=True, label="shape"),
    _f("design_image_path", "path", live=True, label="fallback image"),
    *[_f(f"design_image_{_state}", "path", live=True,
         label=f"{_state} picture") for _state in BUBBLE_STATES],
    _f("design_pack", "text", live=True, label="design pack"),
    _f("avatar_ring", "choice", choices=AVATAR_DECOS, warn=False,
       live=True, label="decoration"),
    _f("avatar_deco_color", "colour", choices=AVATAR_DECO_COLORS, warn=False,
       live=True, label="decoration colour"),
    _f("avatar_tint", "choice", choices=AVATAR_TINTS, warn=False,
       live=True, label="picture colours"),
    _f("bubble_accent", "float", lo=0.0, hi=1.0, live=True,
       label="colour accent"),
    _f("animation_energy", "float", lo=0.2, hi=2.0, live=True,
       label="animation energy"),
    _f("colors", "custom", coerce="colors", live=True, label="state colours"),
    # -- search --------------------------------------------------------------
    _f("searxng_url", "text", rstrip="/"),
    # -- tools and permissions ----------------------------------------------
    _f("permissions", "custom", coerce="permissions"),
    _f("extra_allowed_commands", "str_list", cap=64),
    _f("tool_call_times", "custom", coerce="tool_call_times"),
    _f("max_tool_calls", "int", lo=0, hi=10_000),
    _f("command_policy", "custom", coerce="command_policy"),
    _f("confirm_seconds", "float", lo=5.0, hi=600.0),
    _f("dry_run", "bool"),
    # -- behaviour -----------------------------------------------------------
    _f("streaming_tts", "bool"),
    # Coerced here where it was not before: the app reads it as a flag, and an
    # uncoerced `"false"` reads as True (the fail-open this table exists to
    # stop). The one intentional behaviour change of this refactor, pinned by
    # name in the differential test.
    _f("autostart", "bool", note="newly coerced (was raw)"),
    _f("assistant_name", "text", fallback=True, warn=False),
    _f("wake_word_required", "bool"),
    _f("engage_seconds", "float", lo=5.0, hi=600.0),
    _f("workspace_aliases", "custom", coerce="workspace_aliases"),
    _f("home_place", "text"),
    _f("calendar_ics", "custom", coerce="calendar_ics"),
    _f("wake_spotter", "bool"),
    _f("mic_selfheal", "bool"),
    _f("dictation", "bool"),
    _f("spotter_models", "custom", coerce="spotter_models"),
    _f("followup_seconds", "float", lo=0.0, hi=120.0),
    _f("briefing", "bool"),
    _f("world_warnings", "bool"),
    _f("world_cooldown_min", "float", lo=5.0, hi=1440.0),
    _f("hardware_watch", "bool"),
    _f("hardware_cooldown_min", "float", lo=5.0, hi=1440.0),
    _f("hardware_disk_gb", "float", lo=0.5, hi=1000.0),
    _f("resource_alerts", "bool"),
    _f("ram_alert_percent", "float", lo=50.0, hi=99.0),
    _f("vram_alert_percent", "float", lo=50.0, hi=99.0),
    _f("notification_reader", "bool"),
    _f("notification_mute_apps", "str_list", cap=32, lower=True),
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
