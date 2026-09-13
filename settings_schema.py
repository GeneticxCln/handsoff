"""handsoff settings schema — the single source of shared defaults.

handsoff.py re-exports this as ``DEFAULT_SETTINGS`` (so ``H.DEFAULT_SETTINGS``
keeps working for tests and any self-edited code), and handsoff-settings.py
imports it directly — the settings GUI no longer needs to load the whole
bubble module just to merge defaults.

Precedence elsewhere: built-in defaults <- environment <- settings.json.
"""
from __future__ import annotations

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
    want_design = str(settings.get("bubble_design", "")).strip().lower()
    want_size = _look_number(settings.get("bubble_size"))
    want_energy = _look_number(settings.get("animation_energy"))
    want_accent = _look_number(settings.get("bubble_accent"))
    for entry in APPEARANCE_LOOKS:
        if want_design != entry["design"]:
            continue
        if want_size != float(entry["bubble_size"]):
            continue
        if abs(want_energy - float(entry["animation_energy"])) > 1e-6:
            continue
        if abs(want_accent - float(entry["bubble_accent"])) > 1e-6:
            continue
        if any(_look_color(colors.get(key)) != _look_color(value)
               for key, value in entry["colors"].items()):
            continue
        return entry["name"]
    return ""

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
