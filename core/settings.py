"""Settings machinery for handsoff — step (a) of the monolith cut plan.

Extracted from handsoff.py as-is (behavior-preserving): the safe-file
primitives, coercion, migration, locking, the loader and the writers now live
in one explicit module that knows nothing about the rest of the bubble.

The one deliberate design change is HOW paths are resolved: the monolith's
functions read module globals (``SETTINGS_FILE`` …), which tests legitimately
monkeypatch on ``handsoff``. Those globals stay the contract on ``handsoff``;
this module takes its paths through a small snapshot object
(:class:`Settings`) supplied by the caller at call time, so it stays
import-clean (no circular import) while ``handsoff`` keeps thin late-bound
wrappers with the exact old names and signatures.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import copy
from pathlib import Path

from . import load_module

_schema = load_module("settings_schema")
DEFAULT_SETTINGS = _schema.DEFAULT_SETTINGS
SETTINGS_VERSION = _schema.SETTINGS_VERSION
# Keys a past version wrote that this build retired; dropped on load AND on
# write, so a read-merge-write cannot resurrect them (see the schema).
RETIRED_SETTINGS = tuple(getattr(_schema, "RETIRED_SETTINGS", ()))
# The Appearance look catalogue lives in the schema (it is data over the same
# five keys the schema already defaults), but the BUBBLE reaches it through
# here: the installed layout resolves settings_schema through a loader the
# settings app set up, so a bare `import settings_schema` in the bubble is not
# the contract. A schema from an older install has no looks at all — that
# reports Custom, which is the truthful answer rather than a crash.
_look_matcher = getattr(_schema, "look_matching", None)
_look_lookup = getattr(_schema, "look", None)


def look_label(name: str) -> str:
    """The display label for a look name, or the name itself if unknown.

    The catalogue's own label ("All-seeing", not "allseeing") so the doctor
    and the GUI name a look the same way.
    """
    entry = _look_lookup(name) if _look_lookup is not None else None
    return str(entry["label"]) if entry else str(name or "")


def look_matching(settings: dict) -> str:
    """The Appearance look `settings` spell out, or "" when none does.

    Derived, never stored: see the catalogue comment in the schema. The bubble
    reports this in `--ptt health` / doctor, so "which look am I running" has a
    one-line answer that cannot disagree with the settings it came from.
    """
    if _look_matcher is None:
        return ""
    try:
        return _look_matcher(settings)
    except Exception:            # a malformed settings dict must not break health
        logging.getLogger("handsoff").debug("look_matching failed", exc_info=True)
        return ""


# ------------------------------------------------------------ safe-file primitives
# Used by settings writes AND re-exported by handsoff.py (where the runtime
# hardening suite exercises them under the same names).

def _secure_file(path: Path) -> bool:
    """Make an existing runtime/config file owner-only, without creating it.

    ``Path.exists()`` is not sufficient here: it returns false for a broken
    symlink, which would let an attacker redirect a later atomic write. Use
    lstat first and reject every symlink, including broken ones. A live Unix
    socket is checked for ownership/mode but is not chmod-ed through a regular
    file path operation on platforms where that is unsupported.
    """
    try:
        if path.is_symlink():
            return False
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        if info.st_uid != os.getuid() or not (
                stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            return False
        if stat.S_ISREG(info.st_mode):
            path.chmod(0o600)
            info = path.stat()
        elif stat.S_ISSOCK(info.st_mode) and (info.st_mode & 0o077):
            # A stale socket from an earlier run under a permissive umask
            # must self-heal, not wedge startup forever: it is OUR file, so
            # tighten it in place (sockets accept chmod on Linux) instead of
            # failing _prepare_runtime until manual removal.
            path.chmod(0o600)
            info = path.stat()
        return ((info.st_mode & 0o077) == 0
                and info.st_uid == os.getuid())
    except OSError:
        return False


def _atomic_private_write(path: Path, text: str) -> None:
    """Write a sensitive text file with mode 0600 and an atomic replacement.

    A unique temporary name prevents unrelated writers from swapping the same
    ``.tmp`` file, while the mode is set before the file becomes visible at
    its final path. The caller still owns any higher-level read/modify/write
    lock needed for its data structure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                    suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        if not _secure_file(path):
            raise OSError(f"refusing insecure runtime file: {path}")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _quarantine_bad(path: Path) -> None:
    """Move a corrupt config/state file aside; never fail-open on garbage."""
    try:
        if not path.exists():
            return
        # ponytail: pid suffix — %S timestamps collide when two processes
        # quarantine the same file within one second (second replace would
        # silently destroy the first bad copy).
        ts = time.strftime("%Y%m%d-%H%M%S")
        bad = path.with_name(f"{path.name}.bad-{ts}-{os.getpid()}")
        path.replace(bad)
        logging.getLogger("handsoff").warning(
            "corrupt %s quarantined to %s; using defaults", path, bad)
    except OSError:
        logging.getLogger("handsoff").warning(
            "corrupt %s could not be quarantined", path, exc_info=True)


def _backup_runtime_json(path: Path) -> None:
    """One-generation .bak beside a runtime JSON file (history, memory,
    reminders, settings). Best-effort: a backup failure must never block the
    write that follows — the atomic write is the real safety mechanism.

    The copy is itself atomic (temp + replace), which matters exactly when the
    disk is full: `shutil.copy2` writes straight over the old `.bak`, so a
    failure part-way left the *backup* truncated — the one file you would
    restore from, damaged by the very failure it exists for. Mode 0600 is set
    before it becomes visible (copy2 preserves the SOURCE's mode, so a runtime
    file that was still 0644 when copied left a 0644 backup holding the same
    transcript).
    """
    try:
        if path.exists():
            dst = Path(str(path) + ".bak")
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as out, path.open("rb") as src:
                    shutil.copyfileobj(src, out)
                os.chmod(tmp, 0o600)
                os.replace(tmp, dst)
            except BaseException:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
    except OSError:
        logging.getLogger("handsoff").debug(
            "backup of %s failed (continuing)", path)


# ------------------------------------------------------------------- coercion

def coerce_settings(s: dict) -> dict:
    """Coerce/validate raw merged settings IN PLACE. Shared by the bubble's
    _load_settings AND the settings app (a hand-edited settings.json must
    never crash either program; the settings app is the recovery tool and
    must open even when the config is garbage)."""
    log = logging.getLogger("handsoff")

    def _num(key: str, cast, lo, hi) -> None:
        """Coerce one numeric setting; on garbage, warn and use the default
        (a bad value must never kill startup or leak through unvalidated)."""
        try:
            s[key] = min(hi, max(lo, cast(s[key])))
        except (TypeError, ValueError):
            log.warning(
                "invalid %s — using default %r",
                key, DEFAULT_SETTINGS[key])
            s[key] = DEFAULT_SETTINGS[key]

    _num("num_ctx", int, 1024, 2 ** 20)
    _num("history_tokens", int, 0, 2 ** 20)     # 0 = auto (3/4 of num_ctx)
    _num("bubble_size", int, 96, 192)
    _num("bubble_accent", float, 0.0, 1.0)
    _num("animation_energy", float, 0.2, 2.0)
    _bd = str(s.get("bubble_design", "orb")).strip().lower()
    s["bubble_design"] = _bd if _bd in _schema.BUBBLE_DESIGNS else DEFAULT_SETTINGS["bubble_design"]
    # The `image` design's art. Expanded, never validated here: the file may be
    # created later or deleted at any time, and DROPPING it to "" on a missing
    # file would silently erase the user's choice the first time they moved a
    # folder. The renderer reports why it cannot draw (`design_image_problem`).
    s["design_image_path"] = os.path.expanduser(
        str(s.get("design_image_path") or "").strip())
    # The same art one picture PER STATE (idle/listening/thinking/speaking).
    # Expanded and stripped for exactly the reasons above — a file that is moved
    # or not yet drawn must not erase the choice — and the keys come from the
    # schema so the setting name and the state it belongs to cannot drift.
    for _key in _schema.DESIGN_IMAGE_KEYS:
        s[_key] = os.path.expanduser(str(s.get(_key) or "").strip())
    # The `image` design's pack: ONE installed folder name. Stripped here and
    # nothing more, for the same reason the path above is not validated: a pack
    # that is temporarily moved or not yet installed must not erase the user's
    # choice. The renderer reports why it cannot draw (`pack_problem`), and the
    # settings app writes the slug it read from `installed_packs()`.
    s["design_pack"] = str(s.get("design_pack") or "").strip()
    # The local SearXNG the search router prefers. Stripped and de-slashed, not
    # validated: a user who has not started one yet gets the keyless backends and
    # a doctor line saying `searxng not running`, which is more useful than
    # erasing the address they typed. An empty value disables the attempt.
    s["searxng_url"] = str(s.get("searxng_url") or "").strip().rstrip("/")
    _num("mic_threshold", int, 50, 10_000)
    _num("tts_rate", float, 0.5, 2.0)
    _num("tts_volume", float, 0.1, 2.0)
    _num("max_tool_calls", int, 0, 10_000)
    _num("ram_alert_percent", float, 50.0, 99.0)
    _num("vram_alert_percent", float, 50.0, 99.0)
    _num("confirm_seconds", float, 5.0, 600.0)
    s["dry_run"] = bool(s.get("dry_run", False))
    _pol = s.get("command_policy")
    if isinstance(_pol, dict):
        s["command_policy"] = {
            str(k).strip(): str(v).strip().upper()
            for k, v in _pol.items()
            if str(k).strip() and str(v).strip().upper() in ("ALLOW", "DENY", "CONFIRM")
        }
    else:
        s["command_policy"] = {}
    s["resource_alerts"] = bool(s.get("resource_alerts", False))
    s["notification_reader"] = bool(s.get("notification_reader", False))
    _nm = s.get("notification_mute_apps", [])
    s["notification_mute_apps"] = ([str(x).strip().lower() for x in _nm if str(x).strip()]
                                   if isinstance(_nm, list) else [])[:32]
    s["handsfree"] = bool(s.get("handsfree", False))
    s["streaming_tts"] = bool(s.get("streaming_tts", True))
    s["wake_word_required"] = bool(s.get("wake_word_required", False))
    s["assistant_name"] = str(s.get("assistant_name", "assistant")).strip() or "assistant"
    _c = s.get("calendar_ics", [])
    if isinstance(_c, str):
        _c = [x.strip() for x in _c.replace(",", "\n").split("\n") if x.strip()]
    elif isinstance(_c, list):
        _c = [str(x).strip() for x in _c if str(x).strip()]
    else:
        _c = []
    s["calendar_ics"] = _c[:10]
    s["wake_spotter"] = bool(s.get("wake_spotter", False))
    s["mic_selfheal"] = bool(s.get("mic_selfheal", True))
    s["dictation"] = bool(s.get("dictation", True))
    _num("followup_seconds", float, 0.0, 120.0)   # 0 = feature off
    _sm = s.get("spotter_models", [])
    s["spotter_models"] = ([str(x).strip() for x in _sm if str(x).strip()]
                           if isinstance(_sm, list) else [])
    try:
        v = float(s.get("engage_seconds", 45.0))
    except (TypeError, ValueError):
        v = 45.0
    s["engage_seconds"] = min(600.0, max(5.0, v))
    aliases = s.get("workspace_aliases")
    s["workspace_aliases"] = (
        {str(k).strip().lower(): str(v).strip()
         for k, v in aliases.items() if str(k).strip() and str(v).strip()}
        if isinstance(aliases, dict) else {})
    s["home_place"] = str(s.get("home_place", "")).strip()
    s["briefing"] = bool(s.get("briefing", False))
    s["world_warnings"] = bool(s.get("world_warnings", False))
    _num("world_cooldown_min", float, 5.0, 1440.0)
    s["hardware_watch"] = bool(s.get("hardware_watch", False))
    _num("hardware_cooldown_min", float, 5.0, 1440.0)
    _num("hardware_disk_gb", float, 0.5, 1000.0)
    # ponytail: permissions fail-closed — garbage must never enable tools
    _perms = s.get("permissions")
    if not isinstance(_perms, dict):
        log.warning("invalid permissions — using defaults (fail-closed)")
        s["permissions"] = dict(DEFAULT_SETTINGS["permissions"])
    else:
        s["permissions"] = {str(k): bool(v) for k, v in _perms.items()
                            if str(k).strip()}
        for k, v in DEFAULT_SETTINGS["permissions"].items():
            s["permissions"].setdefault(k, bool(v))
    # ponytail: unvalidated enums — garbage must fall back to defaults loudly
    _host = str(s.get("ollama_host", "")).strip()
    if not _host:
        log.warning("invalid ollama_host — using default %r",
                    DEFAULT_SETTINGS["ollama_host"])
        _host = str(DEFAULT_SETTINGS["ollama_host"])
    s["ollama_host"] = _host
    # remote-brain opt-in: must be exactly true to enable (fail-closed —
    # truthy junk like "yes" or 1 must NOT silently allow remote sending)
    raw_allow = s.get("allow_remote_ollama", False)
    if raw_allow is True:
        s["allow_remote_ollama"] = True
    else:
        if raw_allow not in (False, None):
            log.warning(
                "invalid allow_remote_ollama %r — using False (fail-closed: "
                "the opt-in must be exactly true)", raw_allow)
        s["allow_remote_ollama"] = False
    _model = str(s.get("model", "")).strip()
    if not _model:
        log.warning("invalid model — using default %r",
                    DEFAULT_SETTINGS["model"])
        _model = str(DEFAULT_SETTINGS["model"])
    s["model"] = _model
    _WHISPER_SIZES = {"tiny", "base", "small", "medium", "large",
                      "large-v1", "large-v2", "large-v3", "turbo"}
    _ws = str(s.get("whisper_size", "")).strip().lower()
    if _ws not in _WHISPER_SIZES:
        log.warning("invalid whisper_size %r — using default %r",
                    s.get("whisper_size"), DEFAULT_SETTINGS["whisper_size"])
        _ws = str(DEFAULT_SETTINGS["whisper_size"])
    s["whisper_size"] = _ws
    _wd = str(s.get("whisper_device", "auto")).strip().lower()
    if _wd not in ("auto", "cpu", "cuda"):
        log.warning("invalid whisper_device %r — using 'auto'", s.get("whisper_device"))
        _wd = "auto"
    s["whisper_device"] = _wd
    if not isinstance(s.get("tts_reference"), str):
        log.warning("invalid tts_reference — using default %r",
                    DEFAULT_SETTINGS["tts_reference"])
        s["tts_reference"] = str(DEFAULT_SETTINGS["tts_reference"])
    else:
        s["tts_reference"] = str(s["tts_reference"]).strip()
    if not isinstance(s.get("mic_device"), str):
        log.warning("invalid mic_device — using default %r",
                    DEFAULT_SETTINGS["mic_device"])
        s["mic_device"] = str(DEFAULT_SETTINGS["mic_device"])
    _colors = s.get("colors")
    if not isinstance(_colors, dict):
        log.warning("invalid colors — using defaults")
        s["colors"] = dict(DEFAULT_SETTINGS["colors"])
    else:
        s["colors"] = {str(k): str(v) for k, v in _colors.items()
                       if k in DEFAULT_SETTINGS["colors"]
                       and isinstance(v, str) and str(v).strip()}
        for k, v in DEFAULT_SETTINGS["colors"].items():
            s["colors"].setdefault(k, v)
    if not isinstance(s["extra_allowed_commands"], list):
        s["extra_allowed_commands"] = []
    if not isinstance(s.get("tool_call_times"), (list, type(None))):
        s["tool_call_times"] = None
    return s


# ------------------------------------------------------------------ migration

def _migrate_settings(data: dict, _slog: "logging.Logger | None" = None) -> dict:
    """Migrate an older settings.json layout to SETTINGS_VERSION.

    Each step upgrades exactly one version, so a v0 file walks every step in
    order. When the layout changes, bump SETTINGS_VERSION in settings_schema.py
    and add a step here — never load a future version (the file was written by
    newer code this process may not understand; keep the values but warn,
    exactly like unknown keys).
    """
    log2 = _slog or logging.getLogger("handsoff")
    try:
        ver = int(data.get("version", 0) or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver > SETTINGS_VERSION:
        log2.warning("settings.json version %d is newer than this build's %d "
                     "— loading anyway, values may be misread", ver, SETTINGS_VERSION)
        return data
    if ver < 2:
        # Piper -> chatterbox-turbo (v2). The old value was a path to a 60 MB
        # .onnx voice; chatterbox cannot read it, and handing it to
        # prepare_conditionals would fail the >5 s reference-clip assertion on
        # every turn. It is DROPPED rather than renamed: a piper voice is not a
        # usable reference clip, and silently keeping a stale path would make
        # the GUI show a voice the bubble cannot load. The user gets the
        # built-in voice until they pick a clip.
        old_voice = data.pop("piper_voice", None)
        # NOTE: do not `setdefault("tts_reference", "")` here. This runs on the
        # data READ FROM DISK, before the defaults/env dict is merged, so an
        # inserted empty string wins the merge and silently kills the
        # HANDSOFF_VOICE override (and any default the schema sets later).
        if old_voice:
            log2.info("settings.json migrated: dropped piper_voice (%s); "
                      "TTS now uses the built-in chatterbox voice", old_voice)
        ver = 2
    if ver < SETTINGS_VERSION:
        log2.info("settings.json migrated v%d -> v%d", ver, SETTINGS_VERSION)
    data["version"] = SETTINGS_VERSION
    return data


# --------------------------------------------------------------------- locking

_SETTINGS_WRITE_LOCK = threading.Lock()   # in-process settings write lock


class SettingsConflictError(RuntimeError):
    """A full save would overwrite a newer value written by another actor."""


def _settings_file_lock():
    """Cross-process file lock (flock on a sidecar, not the data file).

    Shared by settings writes AND reminders.json: pass lock_name to reuse
    the same sidecar-flock pattern for another runtime file in the dir.

    Non-reentrant by construction: flock locks live on the open file
    description, so a second LOCK_EX on the SAME sidecar from the same
    thread (nesting two of these guards) blocks forever instead of
    succeeding — serialize in-process with a threading lock (e.g.
    REMINDERS_LOCK) and never nest this guard with itself.
    """
    import contextlib

    @contextlib.contextmanager
    def _lock(config_dir: Path, lock_name: str = "settings.json.lock"):
        config_dir.mkdir(parents=True, exist_ok=True)
        lock_path = config_dir / lock_name
        # ponytail: os.open(...,0o600) creates owner-only from the start —
        # open()+chmod briefly exposes 0644 under a permissive umask.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
                     0o600)
        fh = os.fdopen(fd, "w")
        try:
            try:
                os.chmod(lock_path, 0o600)   # tighten pre-existing files
            except OSError:
                pass
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
    return _lock


# -------------------------------------------------------------- load + writers

def _load_settings(settings_file: Path) -> dict:
    """Built-in defaults <- environment <- settings.json (the settings app wins)."""
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    env_map = {
        "ollama_host": "OLLAMA_HOST", "model": "HANDSOFF_MODEL",
        "num_ctx": "HANDSOFF_NUM_CTX", "whisper_size": "HANDSOFF_WHISPER",
        "tts_reference": "HANDSOFF_VOICE",
    }
    for key, var in env_map.items():
        if os.environ.get(var):
            s[key] = os.environ[var]
    try:
        raw = settings_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        s = coerce_settings(s)
        s["version"] = SETTINGS_VERSION
        return s
    except OSError:
        s = coerce_settings(s)
        s["version"] = SETTINGS_VERSION
        return s
    try:
        data = json.loads(raw)
    except ValueError:
        _quarantine_bad(settings_file)
        s = coerce_settings(s)
        s["version"] = SETTINGS_VERSION
        return s
    if not isinstance(data, dict):
        _quarantine_bad(settings_file)
        s = coerce_settings(s)
        s["version"] = SETTINGS_VERSION
        return s
    _slog = logging.getLogger("handsoff")
    data = _migrate_settings(data, _slog)
    for k, v in data.items():
        if k == "version":
            continue   # schema meta key, not a setting
        if k not in s:
            # ponytail: silent drops hide typos ("models:" never applies) —
            # warn so the user knows the key was ignored.
            _slog.warning("unknown settings key %r — ignored", k)
            continue
        if isinstance(s[k], dict) and isinstance(v, dict):
            s[k].update(v)
        else:
            s[k] = v
    s["version"] = SETTINGS_VERSION   # meta key: stamped, not merged from disk
    return coerce_settings(s)


_MISSING = object()


def _three_way_merge(expected, current, candidate, path: str):
    """Merge GUI changes onto current data, returning (value, conflict-key)."""
    if candidate == expected:
        if current is _MISSING:
            return _MISSING, None
        # Ponytail: deepcopy(_MISSING) yields a NEW sentinel object that the
        # `is not _MISSING` check below can no longer filter, leaking a non-
        # JSON-serializable object into the written file (TypeError at save).
        return copy.deepcopy(current), None
    if current == expected or candidate == current:
        return copy.deepcopy(candidate), None
    if (isinstance(expected, dict) and isinstance(current, dict)
            and isinstance(candidate, dict)):
        merged = {}
        for key in set(expected) | set(current) | set(candidate):
            value, conflict = _three_way_merge(
                expected.get(key, _MISSING), current.get(key, _MISSING),
                candidate.get(key, _MISSING),
                f"{path}.{key}" if path else str(key))
            if conflict:
                return None, conflict
            if value is not _MISSING:
                merged[key] = value
        return merged, None
    return None, path or "settings"


def _drop_retired_settings(data: dict) -> dict:
    """Remove RETIRED_SETTINGS from a dict that is about to be written.

    Read-merge-write used to operate on the raw file, so a key the migration
    removed was put straight back by the next unrelated save — and, since the
    loader drops retired keys, every start then warned `unknown settings key
    'piper_voice'` forever, for a key the user never wrote.

    Removal is key-driven, not version-gated: a file can be stamped with a
    version whose value changes were written but whose key removal was not (an
    older persist stamped v2 while still carrying piper_voice), and a version
    gate would then never fire again.

    Only *retired* keys go. Unknown keys are kept: they belong to a newer
    build's settings.json, and a write must not erase configuration it merely
    fails to understand (pinned by
    `test_handsfree_toggle_preserves_concurrent_settings_saves`).
    """
    if not RETIRED_SETTINGS:
        return dict(data)
    return {k: v for k, v in data.items() if k not in RETIRED_SETTINGS}


def _read_settings_for_write(settings_file: Path) -> dict:
    """The on-disk dict, converged to this build's schema, as a write base.

    Migrating here is what makes a migration durable; dropping retired keys
    here is what makes it stick even when the file claims a version it does not
    fully honour. This is the one place that decides what a write may contain.
    """
    try:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return _drop_retired_settings(_migrate_settings(loaded))
    except FileNotFoundError:
        return {}
    except ValueError:
        _quarantine_bad(settings_file)
        return {}
    if not isinstance(loaded, dict):
        _quarantine_bad(settings_file)  # valid JSON, wrong shape: never wipe blind
        return {}
    # OSError (permissions, transient I/O) propagates: the caller must abort
    # the write rather than persist a near-empty dict over good data.
    return {}


def _write_settings_dict(data: dict, settings_file: Path, config_dir: Path,
                         *, stamp_version: bool = True,
                         expected_data: dict | None = None) -> dict:
    """Serialize a full settings dict to settings.json: version-stamped,
    backed up one generation, atomic. The single writer both the bubble and
    the settings app use, so every settings.json on disk carries a version."""
    with _SETTINGS_WRITE_LOCK, _settings_file_lock()(config_dir):
        if expected_data is not None:
            # Compare normalized snapshots so an old sparse settings file is
            # compatible with the full dict held by the GUI.
            current = _load_settings(settings_file)
            expected = dict(expected_data)
            candidate = dict(data)
            expected.pop("version", None)
            current.pop("version", None)
            candidate.pop("version", None)
            data, conflict = _three_way_merge(expected, current, candidate, "")
            if conflict:
                raise SettingsConflictError(
                    f"settings changed outside this window: {conflict}")
        else:
            data = dict(data)
        if stamp_version:
            data["version"] = SETTINGS_VERSION
        # Same rule as the single-key writer: a full save must not be able to
        # reintroduce a key this build retired (the GUI's edit dict is built
        # through merge_settings, which can carry one along).
        data = _drop_retired_settings(data)
        _backup_runtime_json(settings_file)
        _atomic_private_write(
            settings_file, json.dumps(data, ensure_ascii=False, indent=1))
        return data


def _persist_setting(key: str, value, settings_file: Path,
                     config_dir: Path) -> bool:
    """Persist one runtime setting without overwriting unrelated settings.

    Returns False when nothing reached the disk. Report it rather than
    returning silently: callers used to update their in-memory copy anyway, so
    a failed write left the runtime using (and believing) a value the next
    start would not read back.
    """
    with _SETTINGS_WRITE_LOCK, _settings_file_lock()(config_dir):
        try:
            data = _read_settings_for_write(settings_file)
        except OSError:
            logging.getLogger("handsoff").warning(
                "persist_setting %r aborted: settings file unreadable", key)
            return False
        data[key] = value
        data["version"] = SETTINGS_VERSION   # every on-disk write is stamped
        # NOTE: _atomic_private_write creates its own uniquely-named temp
        # file; a pre-computed ".json.tmp" path here would reintroduce the
        # predictable-name race that helper exists to prevent.
        _backup_runtime_json(settings_file)
        try:
            _atomic_private_write(
                settings_file, json.dumps(data, ensure_ascii=False, indent=1))
        except OSError as e:
            # A full disk (ENOSPC), a read-only mount or a vanished directory
            # lands here. Returning False is the whole point of this
            # signature: callers report an unsaved change, and one that does
            # `is False` would instead crash the tool call with a traceback.
            # The old file is intact either way — the write is atomic.
            logging.getLogger("handsoff").warning(
                "persist_setting %r failed (%s: %s) — keeping the old value",
                key, type(e).__name__, e)
            return False
        return True


class Settings:
    """Explicit settings facade: loads, holds and persists the settings dict
    for one set of runtime paths. Wrapping the plain dict keeps the entire
    existing `dict`-shaped contract (``SETTINGS["x"]`` everywhere, tests
    rebinding ``H.SETTINGS``) while giving later split steps a single,
    path-correct entry point instead of module-global reach-ins."""

    def __init__(self, settings_file: Path, config_dir: Path,
                 data: "dict | None" = None) -> None:
        self.settings_file = Path(settings_file)
        self.config_dir = Path(config_dir)
        self._data: dict = {} if data is None else data
        self._loaded = data is not None

    # -- dict protocol (read path; writes go through set/load) --------------
    def __getitem__(self, key: str):
        return self._data[key]

    def __setitem__(self, key: str, value) -> None:
        """Persist one setting; use :meth:`as_dict` for an in-memory view."""
        self.persist(key, value)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def as_dict(self) -> dict:
        """The live dict (handsoff re-exports this as its SETTINGS global)."""
        return self._data

    # -- lifecycle -----------------------------------------------------------
    def load(self) -> dict:
        """(Re)load from disk: defaults <- env <- file, coerced and
        version-stamped. Returns and stores the live dict."""
        self._data = _load_settings(self.settings_file)
        self._loaded = True
        return self._data

    def ensure_loaded(self) -> dict:
        return self._data if self._loaded else self.load()

    def persist(self, key: str, value) -> bool:
        """Read-merge-write ONE key through the cross-process lock (no
        derived-global side effects here — the caller owns those).

        Returns whether the value reached the disk; the cached dict is only
        updated when it did, so memory and the file cannot disagree.
        """
        if not _persist_setting(key, value, self.settings_file, self.config_dir):
            return False
        self._data[key] = value
        return True

    def write_all(self, data: dict, *, expected_data: dict | None = None) -> dict:
        """Version-stamped, backed-up full-file write (the settings app's
        save path). ``expected_data`` is the snapshot read by the editor;
        changed keys are merged and conflicting keys are rejected."""
        written = _write_settings_dict(
            data, self.settings_file, self.config_dir,
            expected_data=expected_data)
        self._data = written
        self._loaded = True
        return written

    def backup_runtime_json(self, path: Path) -> None:
        _backup_runtime_json(path)


def settings_object(settings_file: Path, config_dir: Path,
                    data: "dict | None" = None) -> Settings:
    """Build a Settings object; `data` is loaded lazily when omitted (callers
    that already hold a settings dict pass it to avoid a double read)."""
    return Settings(settings_file, config_dir, data=data)
