"""handsoff settings schema — the single source of shared defaults.

handsoff.py re-exports this as ``DEFAULT_SETTINGS`` (so ``H.DEFAULT_SETTINGS``
keeps working for tests and any self-edited code), and handsoff-settings.py
imports it directly — the settings GUI no longer needs to load the whole
bubble module just to merge defaults.

Precedence elsewhere: built-in defaults <- environment <- settings.json.
"""
from __future__ import annotations

SETTINGS_VERSION: int = 1   # bumped on incompatible settings.json layout changes

BUBBLE_DESIGNS = ("orb", "halo", "reactor", "bloom", "droplet", "cube", "equalizer", "crystal", "saturn", "void")

DEFAULT_SETTINGS: dict = {
    "ollama_host": "http://127.0.0.1:11434",
    "allow_remote_ollama": False,  # explicit opt-in for a non-loopback server
    "model": "qwen3:8b",
    "num_ctx": 32768,
    "history_tokens": 0,       # 0 = auto: ctx − prompt − reply reserve
    "whisper_size": "tiny",
    "whisper_device": "auto",   # auto=GPU when free VRAM fits, else cpu; or forced "cpu"/"cuda"
    "piper_voice": "",
    "tts_rate": 1.0,
    "tts_volume": 1.0,
    "mic_device": "",
    "mic_threshold": 600,
    "handsfree": False,
    "bubble_size": 128,
    "bubble_design": "orb",  # Appearance tab: orb|halo|reactor|bloom|droplet|cube|equalizer|crystal|saturn|void
    "bubble_accent": 0.5,      # 0..1 accent punch: how hard each shape leans on its
                               # state colour (glow alpha, saturation, comet light)
    "animation_energy": 1.0,   # 0.2..2.0 global animation scale: orbit speed, swirl
                               # speed, hue sweep and comet brightness. 1.0 = the
                               # default feel (the old 'Subtle' preset sits mid-scale)
    "colors": {
        "idle": "#4f8cff", "listening": "#ff4d5e",
        "thinking": "#ff9e2c", "speaking": "#3ecf6e",
    },
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
