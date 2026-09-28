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
from . import theme as _theme

_schema = load_module("settings_schema")
DEFAULT_SETTINGS = _schema.DEFAULT_SETTINGS
__all__ = [
    # the lifecycle, in the order a caller meets it: load owns read -> migrate ->
    # coerce -> quarantine; write_settings owns lock -> backup -> drop-retired ->
    # stamp -> atomic replace; persist_setting owns read-merge-write of one key.
    "load_settings", "write_settings", "persist_setting", "coerce_setting",
    "coerce_settings", "Settings", "settings_object", "SettingsConflictError",
    # shared with the rest of the runtime rather than with settings: a private
    # 0600 writer, a 0600 hardening pass, a corrupt-file quarantine, a
    # sidecar-flock guard (settings.json AND reminders.json use it) and a
    # one-generation backup.
    "atomic_private_write", "secure_file", "quarantine_file",
    "cross_process_lock", "backup_runtime_json",
    # looks catalogue readers the settings window and the bubble both use
    "look_label", "look_matching", "SETTINGS_VERSION",
]

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

def secure_file(path: Path) -> bool:
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


def atomic_private_write(path: Path, text: str) -> None:
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
        if not secure_file(path):
            raise OSError(f"refusing insecure runtime file: {path}")
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def quarantine_file(path: Path) -> None:
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


def backup_runtime_json(path: Path) -> None:
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

# The same vocabulary `core.tools.coerce_bool_arg` accepts for model-supplied
# arguments, spelled here because `core.settings` is imported BY tools (so it
# cannot import tools back) and because the two answer the same question in the
# same words: "false" means false.
_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "0", ""})


def _bool_flag(value, default: bool) -> bool:
    """A settings flag, read STRICTLY — because `bool("false")` is True.

    A hand-edited settings.json is the one way junk arrives, and this tree
    treats it as hostile everywhere else; these flags went through plain
    `bool()`, which INVERTS the most natural way to write "off": "false", "no"
    and "off" are all truthy strings. For `permissions.*` and
    `notification_reader` that is not cosmetic — it silently ENABLES mouse
    control and private-notification reading on a file that asked for them to be
    off, which is the one direction that must never fail open. Unknown junk
    therefore takes the DEFAULT, so an enabling flag stays closed.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Only 0 and 1 are meaningful, for the same reason `coerce_bool_arg`
        # refuses the rest: `bool(2)` is True, a flag nobody asked for.
        return bool(value) if value in (0, 1) else default
    token = str(value).strip().lower()
    if token in _BOOL_TRUE:
        return True
    if token in _BOOL_FALSE:
        return False
    return default


def coerce_settings(s: dict) -> dict:
    """Coerce/validate raw merged settings IN PLACE. Shared by the bubble's
    load_settings AND the settings app (a hand-edited settings.json must
    never crash either program; the settings app is the recovery tool and
    must open even when the config is garbage).

    GENERATED from `settings_schema.SETTINGS_FIELDS` — one table row per
    setting, so a key cannot be coerced here and forgotten there. Adding a
    setting means adding a row, and `tests/test_settings_contract.py` fails if
    the table and the shipped defaults disagree.
    """
    log = logging.getLogger("handsoff")
    for field in _schema.SETTINGS_FIELDS:
        _apply_field(s, field, log)
    return s


def _apply_field(s: dict, field, log) -> None:
    """One table row's coercion. Never raises on DATA: a hand-edited file must
    not stop the bubble from starting or the settings app from opening."""
    key, kind = field.key, field.kind
    default = DEFAULT_SETTINGS.get(key)

    if kind == "bool":
        # The strict reader: `bool("false")` is True, which is how a
        # hand-edited "false" used to ENABLE mouse control or private-
        # notification reading.
        s[key] = _bool_flag(s.get(key, default), bool(default))
        return

    if kind in ("int", "float"):
        cast = int if kind == "int" else float
        try:
            s[key] = min(field.hi, max(field.lo, cast(s[key])))
        except (TypeError, ValueError, OverflowError):
            # OverflowError: `int(float("inf"))` — and Python's json parses a
            # bare `Infinity` token, so one crafted value in settings.json
            # would otherwise kill startup inside load_settings.
            log.warning("invalid %s — using default %r", key, default)
            s[key] = default
        return

    if kind == "str":
        # Must ALREADY be a string: `str(5)` would turn a wrong type into a
        # path that a later open() fails on, which is worse than the default.
        raw = s.get(key)
        if not isinstance(raw, str):
            log.warning("invalid %s — using default %r", key, default)
            s[key] = str(default)
        else:
            s[key] = raw.strip()
        return

    if kind == "path":
        # Expanded, never validated: the file may be created later or deleted at
        # any time, and DROPPING the value to "" on a missing file would
        # silently erase the choice the first time the user moved a folder. The
        # renderer reports why it cannot draw (`design_image_problem`).
        s[key] = os.path.expanduser(str(s.get(key) or "").strip())
        return

    if kind == "text":
        text = str(s.get(key) or "").strip()
        if field.rstrip:
            # A URL keeps working with or without its trailing slash, and the
            # two forms must not compare unequal when they name one instance.
            text = text.rstrip(field.rstrip)
        if not text and field.fallback:
            if field.warn:
                log.warning("invalid %s — using default %r", key, default)
            text = str(default)
        s[key] = text
        return

    if kind in ("choice", "colour"):
        value = str(s.get(key) or "").strip().lower()
        hex_ok = (kind == "colour" and _theme.hex_to_rgb(value) is not None)
        if value not in field.choices and not hex_ok:
            # A `colour` also accepts a literal hex, through the SAME parser
            # everything else validates colours with (`core.theme.hex_to_rgb`,
            # fullmatch), so "#4f8cffXYZ" and "red" are refused here exactly as
            # the swatch refuses them — a colour the panel would reject must not
            # survive a hand-edit of settings.json either.
            if field.warn:
                log.warning("invalid %s %r — using default %r",
                            key, s.get(key), default)
            value = str(default).strip().lower()
        s[key] = value
        return

    if kind == "str_list":
        raw = s.get(key)
        items = ([str(x).strip() for x in raw if str(x).strip()]
                 if isinstance(raw, list) else [])
        if field.lower:
            items = [x.lower() for x in items]
        s[key] = items[:field.cap] if field.cap else items
        return

    if kind == "custom":
        coercer = _CUSTOM_COERCERS.get(field.coerce)
        if coercer is None:
            # A table row naming a coercer that does not exist is a coding
            # error, not bad data: `tests/test_settings_contract.py` refuses to
            # let it reach a run, and failing loudly here beats a privacy flag
            # quietly losing its validation.
            raise RuntimeError(
                f"settings field {key!r} names custom coercer "
                f"{field.coerce!r}, which does not exist")
        coercer(s, field, log)
        return

    raise RuntimeError(f"settings field {key!r} has unknown kind {kind!r}")


# ------------------------------------------------------------------ custom
# Rows whose coercion is not one of the declarative kinds. Each takes
# (settings, field, log) and mutates the one key; the ROW still declares the
# key, so the contract guard covers them exactly like the simple ones.

def _coerce_allow_remote_ollama(s: dict, field, log) -> None:
    """The remote-brain opt-in: must be exactly true (fail-closed — truthy junk
    like "yes" or 1 must NOT silently allow sending the conversation away)."""
    raw_allow = s.get(field.key, False)
    if raw_allow is True:
        s[field.key] = True
    else:
        if raw_allow not in (False, None):
            log.warning(
                "invalid allow_remote_ollama %r — using False (fail-closed: "
                "the opt-in must be exactly true)", raw_allow)
        s[field.key] = False


def _coerce_calendar_ics(s: dict, field, log) -> None:
    """A list of ICS sources, also accepted as ONE comma/newline string (how a
    user pastes a couple of URLs). Capped: each entry is a network fetch."""
    raw = s.get(field.key, [])
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace(",", "\n").split("\n") if x.strip()]
    elif isinstance(raw, list):
        raw = [str(x).strip() for x in raw if str(x).strip()]
    else:
        raw = []
    s[field.key] = raw[:10]


def _coerce_colors(s: dict, field, log) -> None:
    """The four state colours, keyed by state name. Unknown keys are dropped
    and missing ones filled from the defaults, so the renderer never has to ask
    whether a state has a colour."""
    colors = s.get(field.key)
    if not isinstance(colors, dict):
        log.warning("invalid colors — using defaults")
        s[field.key] = dict(DEFAULT_SETTINGS["colors"])
        return
    s[field.key] = {str(k): str(v) for k, v in colors.items()
                    if k in DEFAULT_SETTINGS["colors"]
                    and isinstance(v, str) and str(v).strip()}
    for k, v in DEFAULT_SETTINGS["colors"].items():
        s[field.key].setdefault(k, v)


def _coerce_command_policy(s: dict, field, log) -> None:
    """tool -> ALLOW | DENY | CONFIRM. Capped like every other hand-editable
    mapping here, and for the same reason: it is consulted on every tool call."""
    policy = s.get(field.key)
    rules = ({
        str(k).strip(): str(v).strip().upper()
        for k, v in policy.items()
        if str(k).strip()
        and str(v).strip().upper() in _schema.POLICY_RULES
    } if isinstance(policy, dict) else {})
    s[field.key] = dict(list(rules.items())[:64])


def _coerce_permissions(s: dict, field, log) -> None:
    """ponytail: permissions fail-closed — garbage must never enable tools."""
    perms = s.get(field.key)
    if not isinstance(perms, dict):
        log.warning("invalid permissions — using defaults (fail-closed)")
        s[field.key] = dict(DEFAULT_SETTINGS["permissions"])
        return
    # Each permission through the strict reader, defaulting CLOSED for a name
    # the defaults do not know — an unknown permission is one nothing grants, so
    # it cannot become "yes" by being written down.
    s[field.key] = {
        str(k): _bool_flag(v, bool(DEFAULT_SETTINGS["permissions"].get(str(k), False)))
        for k, v in perms.items() if str(k).strip()}
    for k, v in DEFAULT_SETTINGS["permissions"].items():
        s[field.key].setdefault(k, bool(v))


def _coerce_spotter_models(s: dict, field, log) -> None:
    """Wake-word models, capped like its siblings: each entry is a model the
    spotter will try, so an unbounded list is unbounded work per utterance."""
    raw = s.get(field.key, [])
    spotter = ([str(x).strip() for x in raw if str(x).strip()]
               if isinstance(raw, list) else [])
    if len(spotter) > 5:
        # The stock default ALREADY fills the cap (alexa, hey_jarvis,
        # hey_mycroft, timer, weather), so the first user-added entry was the
        # 6th model — and a silent `[:5]` discarded it: the wake word never
        # fired and nothing in the journal said why. Unlike a junk value, a
        # truncated list is not an error to correct, it is a choice to report.
        log.warning(
            "spotter_models: keeping the first 5 of %d — the rest never fire "
            "(the cap is per-utterance work, raise it in core/settings.py "
            "if you really want more)", len(spotter))
    s[field.key] = spotter[:5]


def _coerce_tool_call_times(s: dict, field, log) -> None:
    """The per-belt call-time deque is runtime state, never a user value: a
    hand-edited list is discarded rather than trusted as call history.

    The code used to KEEP any list it found — the one shape this docstring
    names as discarded — and nothing in the app ever writes the key, so every
    list on disk is hand-edited by definition (verified 2026-09-20)."""
    s[field.key] = None


def _coerce_workspace_aliases(s: dict, field, log) -> None:
    """'code' -> '2' — one alias per workspace is the whole feature, and a
    hand-edited file is not a place to grow a lookup table of arbitrary size."""
    aliases = s.get(field.key)
    pairs = ({str(k).strip().lower(): str(v).strip()
              for k, v in aliases.items()
              if str(k).strip() and str(v).strip()}
             if isinstance(aliases, dict) else {})
    s[field.key] = dict(list(pairs.items())[:50])


_CUSTOM_COERCERS = {
    "allow_remote_ollama": _coerce_allow_remote_ollama,
    "calendar_ics": _coerce_calendar_ics,
    "colors": _coerce_colors,
    "command_policy": _coerce_command_policy,
    "permissions": _coerce_permissions,
    "spotter_models": _coerce_spotter_models,
    "tool_call_times": _coerce_tool_call_times,
    "workspace_aliases": _coerce_workspace_aliases,
}


# ------------------------------------------------------------------ migration

# The two switches that put something of the user's on someone else's server.
# A default is a choice made FOR the user, which is why these two are the ones
# v3 applies retroactively rather than only to a fresh file.
_KNOWLEDGE_SWITCHES = ("web_access", "hosted_reader")


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
    if ver < 3:
        # v2 -> v3: the two switches that send something off this machine are
        # OFF for an install that never chose them.
        #
        # `web_access` defaulted True in v2, and the settings app writes the
        # WHOLE dict on every save, so an install nobody has touched carries
        # `web_access: true` in exactly the same shape as one where the user
        # went looking for it and turned it on. Nothing in the file records
        # which of the two happened, and a marker invented now cannot
        # retroactively separate them either.
        #
        # So this takes the side the README promises — off — and says so
        # loudly, with the way back, because a value silently taken away is the
        # thing users stop trusting a migration for. The alternative, keeping
        # it, is the defect this step exists to remove: a default that ships
        # query text and page addresses to third parties and is on because
        # nobody ever opened the settings.
        #
        # A file with NO `permissions` key is deliberately left alone. The
        # merge below then applies the v3 default, which is the same answer
        # without inserting a key the user never wrote — the mistake the v1 step
        # above warns about in the other direction.
        perms = data.get("permissions")
        if isinstance(perms, dict):
            turned_off = [k for k in _KNOWLEDGE_SWITCHES
                          if perms.get(k) is not False]
            for k in _KNOWLEDGE_SWITCHES:
                perms[k] = False
            if turned_off:
                log2.warning(
                    "settings.json migrated v2 -> v3: switched %s OFF. These "
                    "send your search text (and, for the page reader, the "
                    "address of the page you are reading) to third parties, "
                    "and the v2 defaults had them on, so an install that was "
                    "never touched inherited that. Settings -> Permissions "
                    "switches them back on.",
                    " and ".join(turned_off))
        ver = 3
    if ver < SETTINGS_VERSION:
        log2.info("settings.json migrated v%d -> v%d", ver, SETTINGS_VERSION)
    data["version"] = SETTINGS_VERSION
    return data


# --------------------------------------------------------------------- locking

_SETTINGS_WRITE_LOCK = threading.Lock()   # in-process settings write lock


class SettingsConflictError(RuntimeError):
    """A full save would overwrite a newer value written by another actor."""


def cross_process_lock():
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

def load_settings(settings_file: Path) -> dict:
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
        quarantine_file(settings_file)
        s = coerce_settings(s)
        s["version"] = SETTINGS_VERSION
        return s
    if not isinstance(data, dict):
        quarantine_file(settings_file)
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
        # The same sentinel trap, reached by the DUAL-DELETE path: when both
        # disk and GUI deleted a key, `candidate is current is _MISSING`, so
        # `candidate == current` fires and `deepcopy(candidate)` clones the
        # sentinel into something `is not _MISSING` — the key then reappears in
        # the written file as a non-serializable object.
        if candidate is _MISSING:
            return _MISSING, None
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
        quarantine_file(settings_file)
        return {}
    if not isinstance(loaded, dict):
        quarantine_file(settings_file)  # valid JSON, wrong shape: never wipe blind
        return {}
    # OSError (permissions, transient I/O) propagates: the caller must abort
    # the write rather than persist a near-empty dict over good data.
    return {}


def write_settings(data: dict, settings_file: Path, config_dir: Path,
                         *, stamp_version: bool = True,
                         expected_data: dict | None = None) -> dict:
    """Serialize a full settings dict to settings.json: version-stamped,
    backed up one generation, atomic. The single writer both the bubble and
    the settings app use, so every settings.json on disk carries a version."""
    with _SETTINGS_WRITE_LOCK, cross_process_lock()(config_dir):
        if expected_data is not None:
            # Compare normalized snapshots so an old sparse settings file is
            # compatible with the full dict held by the GUI.
            current = load_settings(settings_file)
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
        backup_runtime_json(settings_file)
        atomic_private_write(
            settings_file, json.dumps(data, ensure_ascii=False, indent=1))
        return data


def coerce_setting(key: str, value):
    """What `key` will hold after a save-and-reload of `value`.

    The one answer to "what does this value BECOME", for the two runtime
    writers. They used to write the disk the coerced value and MEMORY the raw
    argument — so the two docstrings promising that "memory and the file cannot
    disagree" were both false: `persist_setting("mic_threshold", "junk")`
    wrote 600 and left `"junk"` in `SETTINGS`, and the next PTT release died in
    a bare `int(...)` while the hands-free listener's `_SpeechGate(int(...))`
    took the listener thread down with it.

    Unknown keys pass through untouched (`coerce_settings` only rewrites the
    keys it knows), which is what keeps `set_setting` usable for a key this
    build has no rule for.
    """
    # `value` is copied, not shared, for the same reason as `data` below and
    # with the same measurement behind it: a coercer that replaced its
    # container is why nothing was ever observed to change, and the copy is what
    # keeps "what does this value BECOME" a question rather than a mutation once
    # one of them normalises in place.
    probe = {**copy.deepcopy(DEFAULT_SETTINGS), key: copy.deepcopy(value)}
    coerce_settings(probe)
    return probe[key]


def persist_setting(key: str, value, settings_file: Path,
                     config_dir: Path) -> bool:
    """Persist one runtime setting without overwriting unrelated settings.

    Returns False when nothing reached the disk. Report it rather than
    returning silently: callers used to update their in-memory copy anyway, so
    a failed write left the runtime using (and believing) a value the next
    start would not read back.
    """
    with _SETTINGS_WRITE_LOCK, cross_process_lock()(config_dir):
        try:
            data = _read_settings_for_write(settings_file)
        except OSError:
            logging.getLogger("handsoff").warning(
                "persist_setting %r aborted: settings file unreadable", key)
            return False
        data[key] = value
        # Coerce what we are about to write, not just what we read: this is the
        # path a TOOL takes (`set_setting`), and it used to store the model's
        # raw argument verbatim. A string where a number belongs then persisted,
        # and every later reader that does `int(...)` on it (history budget,
        # follow-up window) blew up at runtime — or worse, survived until the
        # next start silently used the default.
        #
        # `probe` is only there to validate: `coerce_settings` assumes every key
        # is present (it indexes `s[key]`), while `data` is whatever the file
        # happens to hold. So it is completed from the defaults, coerced, and
        # only THIS key's coerced value is written back — the rest of the file
        # is left byte-for-byte as the user had it.
        #
        # `data` is deep-copied so that last sentence is structural rather than
        # a property of how today's coercers happen to be written. MEASURED
        # 2026-09-19, with the shared-container merge this replaces: colours,
        # permissions, command_policy, workspace_aliases and spotter_models are
        # all REPLACED by their coercer, never written into, so no user file was
        # being rewritten by a save of an unrelated key — this is not a live
        # defect. What it removes is the dependence on that staying true: one
        # future coercer that normalises in place (`colors["idle"] = ...`) would
        # have edited the rest of the file on the way past, silently, and only
        # for users whose values were not already normal. The guard in
        # tests/test_settings.py installs exactly such a coercer.
        probe = {**copy.deepcopy(DEFAULT_SETTINGS), **copy.deepcopy(data)}
        coerce_settings(probe)
        data[key] = probe[key]
        data["version"] = SETTINGS_VERSION   # every on-disk write is stamped
        # NOTE: atomic_private_write creates its own uniquely-named temp
        # file; a pre-computed ".json.tmp" path here would reintroduce the
        # predictable-name race that helper exists to prevent.
        backup_runtime_json(settings_file)
        try:
            atomic_private_write(
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
        """Persist one setting; use :meth:`as_dict` for an in-memory view.

        Raising on a failed write is the dict protocol's rule — the silent
        no-op this used to be is how `obj[k] = v` reads as done while the disk
        still holds the old value. The bool-returning writer is :meth:`persist`.
        """
        if not self.persist(key, value):
            raise OSError(f"could not persist setting {key!r}")

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
        self._data = load_settings(self.settings_file)
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
        if not persist_setting(key, value, self.settings_file, self.config_dir):
            return False
        # The COERCED value, not the argument: see `coerce_setting` for the
        # divergence this closes.
        self._data[key] = coerce_setting(key, value)
        return True

    def write_all(self, data: dict, *, expected_data: dict | None = None) -> dict:
        """Version-stamped, backed-up full-file write (the settings app's
        save path). ``expected_data`` is the snapshot read by the editor;
        changed keys are merged and conflicting keys are rejected."""
        written = write_settings(
            data, self.settings_file, self.config_dir,
            expected_data=expected_data)
        self._data = written
        self._loaded = True
        return written

    def backup_runtime_json(self, path: Path) -> None:
        backup_runtime_json(path)


def settings_object(settings_file: Path, config_dir: Path,
                    data: "dict | None" = None) -> Settings:
    """Build a Settings object; `data` is loaded lazily when omitted (callers
    that already hold a settings dict pass it to avoid a double read)."""
    return Settings(settings_file, config_dir, data=data)
