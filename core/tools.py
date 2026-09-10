"""Extracted tool, policy, validation, and confirmation runtime.

This module is deliberately application-free.  The host injects a dependency
object whose attributes provide settings, paths, callbacks, and I/O helpers.
"""
from __future__ import annotations

import ast as _ast
import base64
import datetime
import difflib
import functools
import inspect
import json
import logging
import os
import random
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from collections import deque
from pathlib import Path
from contextvars import ContextVar


class _InjectedProxy:
    def __init__(self, fallback):
        self._fallback = fallback

    def __getattr__(self, name):
        try:
            return getattr(_dep(), name)
        except AttributeError:
            target = getattr(_dep(), "subprocess", self._fallback)
            return getattr(target, name)

# Defaults keep direct core.tools imports useful; hosts replace these through DI.
CONFIG_DIR = Path.home() / ".config" / "handsoff"
HOME = Path.home()
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local/state"))) / "handsoff"
_CURRENT = ContextVar("handsoff_tools_dependencies", default=None)
_DEFAULT_DEPS = type("_DefaultDependencies", (), {})()
_DECISIONS_LOCK = threading.Lock()
_DECISIONS_MAX = 500
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"

def _dep():
    return _CURRENT.get() or _DEFAULT_DEPS


def _instance_dep(instance):
    """Resolve captured DI, while keeping __new__-constructed test seams usable."""
    return getattr(instance, "_dependencies", None) or _CURRENT.get() or _DEFAULT_DEPS


def _default_log_metadata(value, kind="text"):
    return str(value)


MAX_REMINDERS = 64
MAX_REMIND_DAYS = 365
RESTART_SCRIPT = HOME / ".local/bin/handsoff-restart"
SELF_MARKER = "# handsoff-self-marker: this line must be preserved across self-edits"
SELF_PATH = Path(__file__).resolve()
SETTINGS_FILE = CONFIG_DIR / "settings.json"
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_WMO = {}
_SPLIT_EDIT_FILES = frozenset({"settings_schema.py", "hardware.py", "handsoff-settings.py"})
_SNOOZE_LOCK = threading.Lock()
_snooze_offer = {}
_kill_offer = {}
_DEFAULT_DEPS._log_metadata = _default_log_metadata
_DEFAULT_DEPS.log = log = logging.getLogger("handsoff.tools")
_DEFAULT_DEPS.STATE_DIR = STATE_DIR
_DEFAULT_DEPS.DECISIONS_FILE = DECISIONS_FILE
_DEFAULT_DEPS.CONFIG_DIR = CONFIG_DIR
_DEFAULT_DEPS.HOME = HOME
_DEFAULT_DEPS.SETTINGS = {}
_DEFAULT_DEPS.RESTART_SCRIPT = RESTART_SCRIPT
_DEFAULT_DEPS.SELF_PATH = SELF_PATH
_DEFAULT_DEPS.SELF_MARKER = SELF_MARKER

# Host replacements (notably H.subprocess in tests) remain visible through DI.
import subprocess as _subprocess
subprocess = _InjectedProxy(_subprocess)
def build_tools():
    out = []
    seen = set()
    for name in dir(ToolBelt):
        fn = getattr(ToolBelt, name)
        if not callable(fn) or not getattr(fn, "_is_tool", False):
            continue
        if id(fn) in seen:
            continue
        seen.add(id(fn))
        params = _param_schema(fn)
        required = fn._tool_required
        if required is None:
            sig = inspect.signature(fn)
            required = [p for p, info in params.items()
                        if sig.parameters[p].default is inspect.Parameter.empty]
        out.append({"type": "function", "function": {
            "name": fn._tool_name,
            "description": (fn._tool_description or
                             (inspect.getdoc(fn) or "").split("\n\n")[0].strip()),
            "parameters": {"type": "object", "properties": params,
                           "required": required},
        }})
    return out


_JSON_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean"}

def log_decision(tool: str, target: str, decision: str, result: str='dispatched') -> None:
    """Append one JSON line to ~/.local/state/handsoff/decisions.jsonl.

    Every tool decision — ALLOW, DENY, CONFIRM, DRY-RUN — lands here with an
    action id, so 'why did it do that' always has an answer. Best-effort:
    a failed log write must never break the tool call itself."""
    entry = {'id': f'{int(time.time() * 1000):x}-{random.randrange(1 << 16):04x}', 'ts': datetime.datetime.now().astimezone().isoformat(timespec='seconds'), 'tool': tool, 'target': _dep()._log_metadata(target), 'decision': decision, 'result': result}
    try:
        with _DECISIONS_LOCK:
            _dep().STATE_DIR.mkdir(parents=True, exist_ok=True)
            decision_file = _dep().DECISIONS_FILE
            with decision_file.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
            os.chmod(decision_file, 384)
            try:
                with decision_file.open('r', encoding='utf-8') as fh:
                    lines = fh.readlines()
                if len(lines) > _DECISIONS_MAX * 2:
                    _dep()._atomic_private_write(decision_file, ''.join(lines[-_DECISIONS_MAX:]))
            except OSError:
                pass
    except Exception:
        _dep().log.debug('decision log write failed', exc_info=True)

class DecisionPolicy:
    """Central tool policy: every tool call classifies to ALLOW, DENY or
    CONFIRM before it runs.

    - ALLOW: run normally (the default; per-tool permission gates still apply).
    - DENY:  the user disabled this tool in settings['command_policy'] —
             refused before any code runs, regardless of permission switches.
    - CONFIRM: the tool self-manages a one-turn-separated spoken confirmation
             (like kill_process → confirm_kill): the first call only proposes,
             the second call — a separate model turn after the user heard the
             offer — executes. Tools classified CONFIRM must be two-step.
    Dry-run mode (settings['dry_run']) makes desktop actions REPORT what they
    would do without doing it — for rehearsing a scripted sequence.
    """

    def __init__(self, settings: 'dict | None'=None) -> None:
        self._settings = settings if settings is not None else _dep().SETTINGS

    def classify(self, tool: str) -> str:
        pol = self._settings.get('command_policy') or {}
        if not isinstance(pol, dict):
            return 'ALLOW'
        v = pol.get(tool)
        if isinstance(v, str) and v.strip().upper() in ('ALLOW', 'DENY', 'CONFIRM'):
            return v.strip().upper()
        return 'ALLOW'

    def is_denied(self, tool: str) -> bool:
        return self.classify(tool) == 'DENY'

    def request_confirm(self, tool: str) -> bool:
        """True when a CONFIRM-classified tool should stop and offer."""
        return self.classify(tool) == 'CONFIRM'

    def confirm_seconds(self) -> float:
        try:
            s = float(self._settings.get('confirm_seconds', 90.0))
        except (TypeError, ValueError):
            s = 90.0
        return min(max(s, 5.0), 600.0)

    @staticmethod
    def is_desktop_action(tool: str) -> bool:
        """Tools that change the desktop (or spawn work) and thus honour
        dry-run mode."""
        return tool in ('run_command', 'start_command', 'open_app', 'close_window', 'focus_window', 'workspace', 'type_text', 'press_keys', 'press_hotkey', 'scroll', 'click_element', 'click_at', 'copy_text', 'paste_text')

class BoundedJob:
    """A long-running whitelisted command with a hard cap and bounded output.

    `start_command` runs e.g. a test suite in the background, stores the
    Popen here, and `job_status` polls it. Everything is bounded: max jobs,
    output bytes, lifetime — so a runaway job cannot eat the machine."""
    MAX_JOBS = 4
    MAX_OUTPUT = 200000
    MAX_LIFETIME_S = 1800.0

    def __init__(self, job_id: str, command: str, proc: subprocess.Popen) -> None:
        self.id = job_id
        self.command = command
        self.proc = proc
        self.started = time.monotonic()
        self._announced = False
        self._out_parts: list[str] = []
        self._out_len = 0
        self._out_lock = threading.Lock()
        self._drain_done = threading.Event()
        self._drain_thread: threading.Thread | None = None
        if proc.stdout is not None:
            self._drain_thread = threading.Thread(target=self._drain, name=f'drain-{job_id}', daemon=True)
            self._drain_thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.proc.stdout.read(8192)
                if not chunk:
                    break
                with self._out_lock:
                    if self._out_len < self.MAX_OUTPUT:
                        keep = chunk[:self.MAX_OUTPUT - self._out_len]
                        self._out_parts.append(keep)
                        self._out_len += len(keep)
        except (OSError, ValueError):
            pass
        finally:
            self._drain_done.set()

    def output_tail(self, limit: int=4096) -> str:
        """Last `limit` chars of the RETAINED head (never blocks on the pipe).

        The buffer keeps the FIRST MAX_OUTPUT bytes of the job's output and
        discards the rest (still draining so the child never blocks) — so
        this is a tail of the head, not of unbounded full output. Callers
        must take the explicit reap join (_join_drain with the full budget)
        before reading a finished job.
        """
        with self._out_lock:
            s = ''.join(self._out_parts)
        return s[-limit:] if len(s) > limit else s

    def _join_drain(self, timeout: float=0.0) -> None:
        """Join the drainer: status polls use 0 (never stall the status
        path); the explicit reap before reading a finished job's output
        passes the full budget so the tail is complete."""
        t = self._drain_thread
        if t is not None and t.is_alive() and (not self._drain_done.is_set()):
            t.join(timeout=timeout)

    def poll(self) -> tuple[str, bool]:
        """(state, done): 'running' | 'done' | 'timeout-killed'.

        Reaping goes through Popen.poll() ONLY: a raw waitpid here would
        reap the child behind Popen's back and returncode would stay None
        forever (a job that finished but never reads as finished)."""
        if self.proc.returncode is not None:
            self._join_drain()
            return ('done', True)
        if time.monotonic() - self.started > self.MAX_LIFETIME_S:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self._join_drain()
            return ('timeout-killed', True)
        self.proc.poll()
        if self.proc.returncode is not None:
            self._join_drain()
            return ('done', True)
        return ('running', False)

    def status_text(self, state: str | None=None, done: bool | None=None) -> str:
        if state is None or done is None:
            state, done = self.poll()
        elapsed = time.monotonic() - self.started
        if state == 'running':
            return f'job {self.id}: still running ({elapsed:.0f}s) — {self.command}'
        if state == 'timeout-killed':
            return f'job {self.id}: KILLED after {BoundedJob.MAX_LIFETIME_S:.0f}s (lifetime cap) — {self.command}'
        rc = self.proc.returncode
        return f'job {self.id}: finished, exit code {rc} ({elapsed:.0f}s) — {self.command}'

def tool(func=None, *, name=None, gates=None, aliases=None, description=None, required=None):
    """Mark a ToolBelt method as an AI-callable tool.

    The Ollama JSON schema is generated automatically from the function's
    signature, type hints and docstring — so one tool is ONE function:

        @tool(gates="press_keys", aliases={"combo": ("keys", "key")})
        def my_tool(self, combo: str) -> str:
            # docstring: "Fire a desktop/compositor shortcut like 'Mod+E'."
            # then 'combo: e.g. Mod+E' lines become param descriptions

    gates:       permission key required (defaults to the tool's own name;
                 pass "" for no gate)
    aliases:     {param: (alt names...)} small models sometimes emit
    description: pinned model-facing description (default: docstring para 1)
    required:    override required-params list (default: params w/o defaults)
    """

    def wrap(f):
        f._is_tool = True
        f._tool_name = name or f.__name__
        f._tool_gates = gates if gates is not None else f._tool_name
        f._tool_aliases = dict(aliases or {})
        f._tool_description = description
        f._tool_required = required
        return f
    return wrap(func) if func else wrap

def _param_schema(func) -> dict:
    """{param: {'type': ..., 'description': ...}} from signature + docstring."""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ''
    params = {}
    for line in doc.splitlines():
        m = re.match('^(\\w+):\\s+(.+)$', line.strip())
        if m:
            params[m.group(1)] = m.group(2).strip()
    out = {}
    for pname, p in sig.parameters.items():
        if pname in ('self',):
            continue
        hint = p.annotation if p.annotation is not inspect.Parameter.empty else str
        if isinstance(hint, str):
            hint = {'bool': bool, 'int': int, 'float': float, 'str': str}.get(hint, str)
        typ = _JSON_TYPE.get(hint if hint in _JSON_TYPE else str, 'string')
        entry = {'type': typ}
        if pname in params:
            entry['description'] = params[pname]
        out[pname] = entry
    return out

class ToolBelt:
    """The assistant's hands: one safe shell command, file read, file edit."""
    ALLOWED = {'pactl', 'playerctl', 'brightnessctl', 'niri', 'spawn', 'echo', 'cat', 'ls', 'pwd', 'notify-send', 'ps', 'free', 'uptime', 'df', 'ss', 'nvidia-smi'}
    _CARGO_OK = {'build', 'check', 'test', 'clippy'}
    _GIT_READ = {'status', 'diff', 'log', 'show', 'branch', 'remote'}
    _GIT_DELETE_FLAGS = {'d', 'D', 'delete'}
    BLOCKED = ('sudo', 'rm', 'pacman', 'yay', 'paru', 'shutdown', 'poweroff', 'reboot', 'halt', 'mkfs', 'dd', 'kill', 'chmod', 'chown', 'mount', 'umount', 'curl', 'wget', 'bash', 'sh', 'zsh', 'fish', 'python', 'python3', 'pip', 'mv', 'cp', 'tar', 'zip', '7z', 'make', 'gcc', 'systemctl', 'journalctl', 'tee', 'xargs', 'env', 'eval', 'exec')
    MAX_READ = 160000
    MAX_WRITE = 2000000
    TIMEOUT = 15

    def __init__(self, on_restart_pending: 'callable', permissions: dict | None=None, on_timer: 'callable | None'=None, on_notification: 'callable | None'=None, on_announce: 'callable | None'=None, on_pomodoro: 'callable | None'=None, dependencies=None) -> None:
        self._dependencies = dependencies or _DEFAULT_DEPS
        self._deps = self._dependencies
        _CURRENT.set(self._dependencies)
        self._on_restart_pending = on_restart_pending
        self._on_timer = on_timer
        self._on_notification = on_notification
        self._on_announce = on_announce
        self._on_pomodoro = on_pomodoro
        self._policy = DecisionPolicy()
        self._jobs: dict[str, BoundedJob] = {}
        self._job_seq = 0
        self._job_lock = threading.Lock()
        self._pending_confirm: dict | None = None
        self._confirm_lock = threading.RLock()
        self._user_turn_marker = 0
        self._last_confirmation_offer = False
        self._confirm_running: str | None = None
        self._last_images: list[str] = []
        self._elements_ts: float = 0.0
        self._pointer_scale: float = 1.0
        self._watch_lock = threading.RLock()
        self._file_watchers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._process_watchers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._tool_times: deque[float] = deque(maxlen=60)
        self._perm = {'run_command': True, 'read_file': True, 'edit_file': True, 'self_restart': True, **(permissions or {})}

    def _set_user_turn(self, marker: int) -> None:
        """Stamp tool calls belonging to one explicit user utterance."""
        self._user_turn_marker = max(getattr(self, '_user_turn_marker', 0), int(marker))

    def _confirmation_lock(self):
        lock = getattr(self, '_confirm_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._confirm_lock = lock
        return lock

    def _is_self_edit(self, args: dict) -> bool:
        """True when this edit_file call targets handsoff.py itself."""
        try:
            p = Path(str(args.get('path') or '')).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return p == _dep().SELF_PATH

    def _edit_confirm_kind(self, args: dict) -> str:
        """'self' | 'split' | '' — the CONFIRM floor covers the running bubble
        AND the split support modules (any editable .py outside CONFIG_DIR);
        a prompt-injected edit under ALLOW must still round-trip the user."""
        try:
            p = Path(str(args.get('path') or '')).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return ''
        kind = _dep()._classify_edit_path(p)
        return kind if kind in ('self', 'split') else ''

    def _self_edit_needs_confirm(self, args: dict) -> bool:
        """True for a self/split edit whose payload already passes the tool's
        static checks (marker for self-edits + syntax). Those checks stay in
        edit_file; here they just ensure the CONFIRM offer only gates writes
        that would otherwise land immediately — invalid payloads fall through
        to the tool's own refusal with no user round-trip."""
        content = args.get('content')
        if not isinstance(content, str):
            return False
        kind = self._edit_confirm_kind(args)
        if not kind:
            return False
        if kind == 'self' and _dep().SELF_MARKER not in content:
            return False
        try:
            compile(content, 'self-edit-preview', 'exec')
        except SyntaxError:
            return False
        return True

    @staticmethod
    def _self_edit_preview(args: dict, limit: int=800) -> str:
        """Unified diff of the proposed self-edit against the live source,
        for the spoken-then-shown confirmation offer."""
        import difflib
        try:
            old = _dep().SELF_PATH.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            return f'(current source unreadable, no diff: {e})'
        new = str(args.get('content') or '')
        out = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile='handsoff.py (running)', tofile='handsoff.py (proposed)'))
        if not out:
            return '(proposed content is identical to the running source)'
        if len(out) > limit:
            out = out[:limit] + f'\n… (diff truncated, {len(out) - limit} more chars)'
        return out

    @staticmethod
    def _split_edit_preview(args: dict, limit: int=800) -> str:
        """Unified diff of a proposed split-module edit against its live file."""
        import difflib
        try:
            target = Path(str(args.get('path') or '')).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return '(unresolvable target, no diff)'
        try:
            old = target.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            return f'(current file unreadable, no diff: {e})'
        new = str(args.get('content') or '')
        out = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=f'{target.name} (running)', tofile=f'{target.name} (proposed)'))
        if not out:
            return '(proposed content is identical to the running file)'
        if len(out) > limit:
            out = out[:limit] + f'\n… (diff truncated, {len(out) - limit} more chars)'
        return out

    def _announce_job(self, text: str) -> None:
        """Speak a job completion through the assistant's announcement path
        (same channel as watcher alerts); a bare ToolBelt without one logs."""
        if self._on_announce is not None:
            try:
                self._on_announce(text)
            except Exception:
                _dep().log.exception('job announcement failed')
        else:
            _dep().log.info('job announcement completed')

    def _tool_methods(self) -> dict:
        """{tool name: bound method} for every @tool-decorated method."""
        cache = getattr(self, '_tool_cache', None)
        if cache is None:
            cache = {}
            for attr in dir(self):
                fn = getattr(self, attr, None)
                if callable(fn) and getattr(fn, '_is_tool', False):
                    cache[fn._tool_name] = fn
            self._tool_cache = cache
        return cache

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        token = _CURRENT.set(_instance_dep(self))
        try:
            return self._execute(name, args)
        finally:
            _CURRENT.reset(token)

    def _execute(self, name: str, args: dict) -> tuple[str, bool]:
        self._last_images = []
        self._last_confirmation_offer = False
        limit = int(_dep().SETTINGS.get('max_tool_calls') or 0)
        now = time.monotonic()
        while self._tool_times and now - self._tool_times[0] > 60:
            self._tool_times.popleft()
        if limit > 0:
            if len(self._tool_times) >= limit:
                log_decision(name, json.dumps(args)[:120], 'RATE-LIMITED', 'refused: rate limit')
                return (f'REFUSED: tool-call rate limit reached ({limit} calls/60s) — stop calling tools, answer from what you have, or wait', True)
            self._tool_times.append(now)
        fn = self._tool_methods().get(name)
        if fn is None:
            return (f'unknown tool: {name}', True)
        gate = fn._tool_gates
        if gate and (not self._perm.get(gate, True)):
            log_decision(name, json.dumps(args)[:120], 'DENY', 'refused: permission gate disabled')
            return (f"REFUSED: the '{gate}' tool is disabled in handsoff settings", True)
        if name in ('kill_process', 'confirm_kill'):
            verdict = 'ALLOW'
        else:
            verdict = self._policy.classify(name)
        if name in ('run_command', 'start_command') and Path(str(args.get('command', '')).split(' ', 1)[0]).name == 'cargo' and (verdict != 'DENY'):
            verdict = 'CONFIRM'
        if name == 'edit_file' and verdict != 'DENY' and (getattr(self, '_confirm_running', None) != name) and self._self_edit_needs_confirm(args):
            verdict = 'CONFIRM'
        target = json.dumps(args, sort_keys=True)[:200] if args else ''
        if verdict == 'DENY':
            log_decision(name, target, 'DENY', 'refused: command_policy DENY')
            return (f"REFUSED: '{name}' is DENIED by the user's command policy (handsoff settings) — do not retry this turn", True)
        if verdict == 'CONFIRM' and getattr(self, '_confirm_running', None) != name:
            with self._confirmation_lock():
                pending = self._pending_confirm
                if pending is None or pending.get('tool') != name or pending.get('turn') != getattr(self, '_user_turn_marker', 0):
                    self._pending_confirm = {'tool': name, 'args': dict(args), 'until': time.monotonic() + self._policy.confirm_seconds(), 'turn': getattr(self, '_user_turn_marker', 0)}
                log_decision(name, target, 'CONFIRM', 'offered; awaiting confirm_action')
            if pending is not None and pending.get('tool') == name and (pending.get('turn') == getattr(self, '_user_turn_marker', 0)):
                log_decision(name, target, 'CONFIRM', 'still awaiting confirm_action')
            self._last_confirmation_offer = True
            extra = ''
            if name == 'edit_file' and self._is_self_edit(args):
                extra = '\nDIFF PREVIEW (proposed change to handsoff.py):\n' + self._self_edit_preview(args)
            elif name == 'edit_file' and self._edit_confirm_kind(args) == 'split':
                try:
                    _split_name = Path(str(args.get('path') or '')).expanduser().name
                except (OSError, RuntimeError, ValueError):
                    _split_name = 'split module'
                extra = f'\nDIFF PREVIEW (proposed change to {_split_name}):\n' + self._split_edit_preview(args)
            return (f"CONFIRM REQUIRED: about to call '{name}' with {target or 'no arguments'}. Nothing happened yet. The user must hear this offer and reply; call confirm_action(answer='yes') in the NEXT turn to run it, or confirm_action(answer='no') to cancel." + extra, True)
        dry_run = bool(_dep().SETTINGS.get('dry_run')) and DecisionPolicy.is_desktop_action(name)
        if dry_run:
            log_decision(name, target, 'DRY-RUN', 'reported; nothing executed')
            return (f"DRY-RUN: {name} would run with {target or 'no arguments'}. Nothing was executed (dry_run is enabled in settings). Describe the plan to the user and stop.", True)
        log_decision(name, target, verdict if verdict != 'CONFIRM' else 'ALLOW', 'dispatched')
        alias_map = fn._tool_aliases
        sig = inspect.signature(fn)
        kwargs = {}
        for pname, p in sig.parameters.items():
            if pname == 'self':
                continue
            raw = args.get(pname)
            if raw is None and pname in alias_map:
                for alt in alias_map[pname]:
                    if args.get(alt) is not None:
                        raw = args[alt]
                        break
            if raw is None and p.default is inspect.Parameter.empty:
                raw = ''
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
            if isinstance(ann, str):
                ann = {'bool': bool, 'int': int, 'float': float, 'str': str}.get(ann, str)
            if ann is bool:
                kwargs[pname] = bool(raw) if not isinstance(raw, bool) else raw
            elif ann is int:
                try:
                    kwargs[pname] = int(raw or 0)
                except (TypeError, ValueError):
                    kwargs[pname] = 0
            elif ann is float:
                try:
                    kwargs[pname] = float(raw or 0.0)
                except (TypeError, ValueError):
                    kwargs[pname] = 0.0
            else:
                kwargs[pname] = str(raw) if raw is not None else ''
        try:
            out = fn(**kwargs)
            return (out, out.startswith('ERROR') or out.startswith('REFUSED'))
        except TypeError as e:
            return (f'ERROR: bad arguments for {name}: {e}', True)
        except Exception as e:
            _dep().log.exception('tool %s failed', name)
            return (f'ERROR: {e}', True)

    def _validate_command(self, command: str) -> tuple[list | None, str, str | None, bool]:
        """Validate without executing: shlex.split + policy checks.

        Returns (argv, exe_base, None, is_restart) when allowed, else
        (None, '', error, False). Never executes anything (start_command must
        not double-exec via run_command) and never arms the restart hook —
        the caller arms it ONLY after a successful launch.
        """
        cmd = command.strip()
        if not cmd:
            return (None, '', 'REFUSED: empty command', False)
        if any((c in cmd for c in ';|&`$\n\r<>')):
            return (None, '', 'REFUSED: shell operators (pipes, ;, &&, redirects) are not allowed', False)
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return (None, '', f'REFUSED: cannot parse command ({e})', False)
        if not argv:
            return (None, '', 'REFUSED: empty command', False)
        argv[0] = os.path.expanduser(argv[0])
        low = cmd.lower()
        exe_base = Path(argv[0]).name
        _unblocked = ''
        if exe_base in ('git', 'cargo'):
            verb = next((a for a in argv[1:] if not a.startswith('-')), '')
            if exe_base == 'git' and verb in self._GIT_READ or (exe_base == 'cargo' and verb in self._CARGO_OK):
                _unblocked = exe_base
            else:
                return (None, '', f"REFUSED: '{exe_base} {verb or '(no verb)'}' is not allowed — git is read-only (status/diff/log/show/branch/remote), cargo only builds/tests", False)
            if _unblocked == 'git' and any((a.lstrip('-') in self._GIT_DELETE_FLAGS for a in argv[2:] if a.startswith('-'))):
                return (None, '', 'REFUSED: deleting branches (git branch -d/-D) is not allowed', False)
        for bad in self.BLOCKED:
            if bad == _unblocked:
                continue
            if re.search(f'(^|\\W){re.escape(bad)}(\\W|$)', low):
                return (None, '', f"REFUSED: '{bad}' is not on the safe whitelist (destructive commands are forbidden)", False)
        exe = argv[0]
        is_restart = exe == str(_dep().RESTART_SCRIPT) or Path(exe).name == _dep().RESTART_SCRIPT.name
        if is_restart:
            if not self._perm.get('self_restart', True):
                return (None, '', 'REFUSED: self-restart is disabled in handsoff settings', False)
            if not (_dep().RESTART_SCRIPT.exists() and os.access(_dep().RESTART_SCRIPT, os.X_OK)):
                return (None, '', f'ERROR: restart script missing at {_dep().RESTART_SCRIPT} — run install.sh', False)
        allowed = set(self.ALLOWED) | {c.strip() for c in _dep().SETTINGS['extra_allowed_commands'] if c.strip()}
        if not is_restart:
            if exe_base not in allowed and exe_base != _unblocked:
                return (None, '', f"REFUSED: '{exe}' is not on the safe shell-command whitelist. Note: REFUSED does NOT mean the program is missing — it only means you may not run it via run_command. If it is one of your own tools (like ydotool for typing), use that tool instead. Allowed: " + ', '.join(sorted(allowed)) + f', {_dep().RESTART_SCRIPT}', False)
        _dep().log.info('run_command: %s', _dep()._log_metadata(cmd, 'command'))
        if Path(argv[0]).name == 'niri' and 'spawn' in argv:
            err = self._validate_niri_spawn(argv)
            if err:
                return (None, '', err, False)
        return (argv, exe_base, None, is_restart)
    _INTERPRETERS = ('python', 'python3', 'node', 'perl', 'ruby', 'lua', 'php', 'bash', 'sh', 'zsh', 'fish', 'pwsh', 'busybox')

    @staticmethod
    def _is_interpreter(base: str) -> bool:
        """Exact-or-versioned interpreter match (no substring overblock).

        Matches `python`, `python3`, `python3.11` — but NOT `shutter`,
        `bashful`, `shellcheck` or `phosphor`, which merely contain one.
        """
        b = (base or '').lower()
        return any((b == tok or re.fullmatch(f'{re.escape(tok)}[\\d.]+', b) for tok in ToolBelt._INTERPRETERS))

    def _validate_niri_spawn(self, argv: list) -> str | None:
        """Spawn-specific capability checks (no execution)."""
        i = argv.index('spawn')
        rest = [a for a in argv[i + 1:] if a != '--']
        target = os.path.basename(rest[0]) if rest else ''
        low_target = target.lower()
        if not target:
            return 'REFUSED: niri spawn needs a program to launch'
        if any((re.search(f'(^|\\W){re.escape(b)}(\\W|$)', low_target) for b in self.BLOCKED)):
            return f"REFUSED: niri spawn of '{target}' is blocked — spawn must not bypass the blocked-programs list"
        if self._is_interpreter(low_target):
            return f"REFUSED: spawning interpreter '{target}' is not allowed — launch GUI apps by name instead (or use open_app)"
        if low_target in ('git', 'cargo'):
            return f"REFUSED: spawning '{target}' is not allowed — use run_command, which gates git/cargo by verb"
        for extra in rest[1:]:
            eb = os.path.basename(extra).lower()
            if not eb or eb.startswith('-'):
                continue
            if any((re.search(f'(^|\\W){re.escape(b)}(\\W|$)', eb) for b in self.BLOCKED)):
                return f"REFUSED: niri spawn arg '{extra}' is blocked — spawn must not bypass the blocked-programs list"
            if self._is_interpreter(eb) or eb in ('git', 'cargo'):
                return f"REFUSED: niri spawn arg '{extra}' is not allowed"
        _terms = ('alacritty', 'kitty', 'foot', 'konsole', 'xterm', 'urxvt', 'wezterm', 'warp', 'ghostty', 'tilix', 'terminator', 'qterminal', 'gnome-terminal', 'xfce4-terminal', 'ptyxis', 'stterm', 'terminology', 'console', 'terminal')
        if low_target in _terms and len(rest) > 1:
            return 'REFUSED: spawning a terminal with arguments is not allowed'
        if shutil.which(rest[0]) is None:
            return f"ERROR: no program named '{target}' is installed"
        arg_str = ' '.join((shlex.quote(a) for a in rest[1:])).lower()
        if ' -e ' in f' {arg_str} ' or '--command' in arg_str or '--eval' in arg_str or ('--print' in arg_str) or ('--script' in arg_str) or ('-x' == arg_str.strip()) or (' -x ' in f' {arg_str} ') or ('-c' == arg_str.strip()) or ('source ' in arg_str) or ('.lua' in arg_str) or ('.js' in arg_str) or ('.py' in arg_str):
            return 'REFUSED: passing script/code flags to spawned programs is not allowed'
        return None

    @tool(description='Run one safe whitelisted command (pactl, playerctl, brightnessctl, niri, spawn, echo, cat, ls, pwd, notify-send, system probes like ps/free/uptime/df/ss/nvidia-smi, read-only git (status/diff/log/show/branch/remote/stash), cargo build/check/test/clippy, restart script). Single command only — pipes/; /&& are refused.')
    def run_command(self, command: str) -> str:
        """Run a whitelisted shell command.

        command: e.g. 'pactl set-sink-volume @DEFAULT_SINK@ -10%'
        """
        argv, exe_base, err, is_restart = self._validate_command(command)
        if err:
            return err
        exe = argv[0]
        try:
            timeout = 240.0 if exe_base == 'cargo' else self.TIMEOUT
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return f'ERROR: program not found: {exe}'
        except subprocess.TimeoutExpired:
            return f'ERROR: command timed out after {timeout:.0f}s'
        if is_restart:
            self._on_restart_pending()
        out = f'exit code {proc.returncode}\nstdout:\n{proc.stdout.strip()}\nstderr:\n{proc.stderr.strip()}'
        return out[:2000]
    YDOTOOL_SOCKET = '/tmp/.ydotool_socket'
    _YDOTOOL_SOCK_CACHE: list = []

    @classmethod
    def _ydotool_socket(cls) -> str:
        """Socket path the ydotool CLI should talk to.

        The user-level ydotoold (Arch's ydotool.service) listens on
        $XDG_RUNTIME_DIR/.ydotool_socket, but the CLI's compiled-in default
        is /tmp/.ydotool_socket — so without YDOTOOL_SOCKET set, every
        type/click dies with 'failed to connect'. Probe both and prefer the
        one that actually answers. Failures are not cached: a daemon started
        later must be picked up on the next call.
        """
        if cls._YDOTOOL_SOCK_CACHE:
            return cls._YDOTOOL_SOCK_CACHE[0]
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        candidates = ([os.path.join(runtime, '.ydotool_socket')] if runtime else []) + [cls.YDOTOOL_SOCKET]
        for path in candidates:
            if cls._socket_connectable(path):
                cls._YDOTOOL_SOCK_CACHE.append(path)
                return path
        return candidates[0]

    @staticmethod
    def _socket_connectable(path: str) -> bool:
        """True only if `path` is a socket file that accepts a connection.

        ydotoold 1.x binds SOCK_DGRAM, so the probe must try DGRAM first —
        a stream connect to it fails with EPROTOTYPE even when the daemon
        is alive and reachable.
        """
        try:
            if not stat.S_ISSOCK(os.stat(path).st_mode):
                return False
        except OSError:
            return False
        for sock_type in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            s = socket.socket(socket.AF_UNIX, sock_type)
            try:
                s.settimeout(1.0)
                s.connect(path)
                return True
            except OSError:
                continue
            finally:
                s.close()
        return False
    _KEYCODES = {'enter': 28, 'return': 28, 'esc': 1, 'escape': 1, 'tab': 15, 'space': 57, 'backspace': 14, 'delete': 111, 'insert': 110, 'home': 102, 'end': 107, 'pageup': 104, 'pagedown': 109, 'up': 103, 'down': 108, 'left': 105, 'right': 106, 'capslock': 58, 'printscreen': 99, 'f1': 59, 'f2': 60, 'f3': 61, 'f4': 62, 'f5': 63, 'f6': 64, 'f7': 65, 'f8': 66, 'f9': 67, 'f10': 68, 'f11': 87, 'f12': 88}
    _MOD_CODES = {'ctrl': 29, 'alt': 56, 'shift': 42, 'meta': 125, 'super': 125}
    _CHAR_CODES = {**{c: 16 + i for i, c in enumerate('qwertyuiop')}, **{c: 30 + i for i, c in enumerate('asdfghjkl')}, **{c: 44 + i for i, c in enumerate('zxcvbnm')}, **{str(n): n + 1 for n in range(1, 10)}, '0': 11}
    _MAX_TYPE = 20000

    def _ydotool(self, *args: str) -> str:
        env = dict(os.environ)
        env['YDOTOOL_SOCKET'] = self._ydotool_socket()
        try:
            proc = subprocess.run(['ydotool', *args], capture_output=True, text=True, env=env, timeout=20 + 0.01 * sum((len(a) for a in args)))
        except FileNotFoundError:
            return 'ERROR: ydotool not installed (pacman -S ydotool)'
        except subprocess.TimeoutExpired:
            return 'ERROR: ydotool timed out'
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or 'unknown error').strip()
            return f'ERROR: ydotool failed: {msg}'
        return 'ok'

    @staticmethod
    def _niri_msg(*args: str, timeout: float=8.0) -> subprocess.CompletedProcess:
        return subprocess.run(['niri', *args], capture_output=True, text=True, timeout=timeout)

    @classmethod
    def _niri_windows(cls) -> list[dict]:
        """One live window-list poll. Raises RuntimeError when the niri IPC
        is unreachable or answers garbage — callers decide whether that is
        fatal or ignorable."""
        try:
            r = cls._niri_msg('msg', '--json', 'windows')
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or 'niri refused').strip()[:160])
            return json.loads(r.stdout or '[]')
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f'niri IPC unavailable ({e})') from e
    _WS_IDX_TTL = 10.0
    _WS_IDX_LOCK = threading.Lock()
    _WS_IDX_CACHE: dict = {}

    @classmethod
    def _workspace_idx_map(cls, refresh: bool=False) -> dict:
        """{workspace_id: idx} from the live workspace list (TTL-cached)."""
        with cls._WS_IDX_LOCK:
            c = cls._WS_IDX_CACHE
            if not refresh and c and (time.monotonic() - c['at'] < cls._WS_IDX_TTL):
                return c['map']
            mapping: dict = {}
            try:
                r = cls._niri_msg('msg', '--json', 'workspaces')
                if r.returncode == 0:
                    for ws in json.loads(r.stdout or '[]'):
                        if ws.get('id') is not None and ws.get('idx') is not None:
                            mapping[ws['id']] = ws['idx']
            except Exception:
                pass
            cls._WS_IDX_CACHE = {'at': time.monotonic(), 'map': mapping}
            return mapping

    @classmethod
    def _workspace_idx_of(cls, win: dict) -> int | None:
        """The user-facing workspace index of window `win` (None if unknown)."""
        wid = win.get('workspace_id')
        if wid is None:
            return None
        return cls._workspace_idx_map().get(wid)

    @classmethod
    def _win_label(cls, w: dict) -> str:
        """Compact human label: 'app_id: title (workspace N)'. The workspace
        number is the user-facing INDEX — the raw workspace_id is a global id
        that can differ from what the user sees (multi-output setups)."""
        label = f"{w.get('app_id') or '?'}: {(w.get('title') or '')[:60]}"
        idx = cls._workspace_idx_of(w)
        if idx is not None:
            label += f' (workspace {idx})'
        return label

    @staticmethod
    def _win_matches(w: dict, q: str) -> bool:
        """Case-insensitive substring match on app-id or title."""
        return q in str(w.get('app_id', '')).lower() or q in str(w.get('title', '')).lower()

    @staticmethod
    def _win_listing(wins: list[dict], limit: int=12) -> str:
        return ' | '.join((ToolBelt._win_label(w) for w in wins[:limit]))

    def _focused_window_info(self) -> dict | None:
        """Best-effort info about the focused window (niri)."""
        try:
            for w in self._niri_windows():
                if w.get('is_focused'):
                    return w
        except Exception:
            pass
        return None
    _TERMINAL_MARKERS = ('terminal', 'konsole', 'alacritty', 'kitty', 'foot', 'xterm', 'urxvt', 'wezterm', 'warp', 'ghostty', 'stterm', 'st-', 'tilix', 'terminator', 'qterminal', 'gnome-terminal', 'xfce4-terminal', 'ptyxis', 'console')
    _TERMINAL_PANEL_MARKERS = ('terminal', 'output', 'repl')

    def _focused_is_terminal(self) -> str | None:
        """App-id/title of the focused window if it looks like a terminal, else None.

        Raises RuntimeError when focus CANNOT be determined (niri IPC dead or
        no focused window): keyboard injection must FAIL CLOSED — typing into
        an unidentified window could execute text in a terminal."""
        return self._terminal_marker(self._typing_guard())

    def _typing_guard(self) -> dict:
        """The verified focused window for a keyboard-injection operation.

        EVERY typing operation calls this immediately before injecting keys:
        focus may have moved since the model decided what to type (the user
        clicked elsewhere, a dialog opened). Raises RuntimeError when focus
        cannot be verified (fail-closed, as _focused_is_terminal)."""
        w = self._focused_window_info()
        if w is None:
            raise RuntimeError('cannot verify the focused window (niri IPC unavailable) — refusing to inject keys')
        return w

    @classmethod
    def _terminal_marker(cls, w: dict) -> str | None:
        """App-id/title of window `w` if it looks like a terminal, else None."""
        app_id = str(w.get('app_id', '') or '').lower()
        title = str(w.get('title', '') or '').lower()
        if any((t in app_id for t in cls._TERMINAL_MARKERS)):
            return app_id or title
        if any((t in title for t in cls._TERMINAL_PANEL_MARKERS)):
            return (app_id or title or 'unknown-window') + ' (terminal panel)'
        if not app_id.strip():
            if title.strip():
                _dep().log.warning('typing target has empty app_id but safe title %r — allowing', title)
                return None
            return 'unknown-window (unidentified — fail-closed)'
        return None

    @tool(description='Type text into the focused window via virtual keyboard. Newlines allowed. Never type into a terminal.', aliases={'text': ('content', 'string', 'body')})
    def type_text(self, text: str) -> str:
        """Type text into the focused window.

        text: The exact text to type.
        """
        if not text:
            return 'REFUSED: nothing to type'
        if len(text) > self._MAX_TYPE:
            return f'REFUSED: text too long (>{self._MAX_TYPE} chars)'
        try:
            target = self._typing_guard()
        except RuntimeError as e:
            return f'REFUSED: {e}'
        if (term := self._terminal_marker(target)) is not None:
            return f'REFUSED: the focused window is a terminal ({term}); typing into terminals is forbidden'
        typed = 0
        skipped = 0
        _CHUNK = 512
        for off in range(0, len(text), _CHUNK):
            if off:
                try:
                    target = self._typing_guard()
                except RuntimeError as e:
                    return f'REFUSED: {e} (typed {typed}/{len(text)} chars before focus became unverifiable)'
                if (term := self._terminal_marker(target)) is not None:
                    return f'REFUSED: focus moved to a terminal ({term}) after {typed} chars — typing aborted'
            piece = text[off:off + _CHUNK]
            r = self._ydotool('type', '--key-delay', '6', '--', piece)
            if r != 'ok':
                break
            typed += len(piece)
        else:
            r = 'ok'
        if r == 'ok':
            pass
        elif typed == 0:
            try:
                target = self._typing_guard()
            except RuntimeError as e:
                return f'REFUSED: {e}'
            if (term := self._terminal_marker(target)) is not None:
                return f'REFUSED: focus moved to a terminal ({term}) before the retry — typing aborted'
            ascii_text = text.replace('—', '-').replace('–', '-').replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'").replace('…', '...')
            ascii_text = unicodedata.normalize('NFKD', ascii_text).encode('ascii', 'ignore').decode('ascii')
            r = self._ydotool('type', '--key-delay', '6', '--', ascii_text)
            if r != 'ok':
                return 'ERROR: typing failed entirely'
            typed = len(ascii_text)
            skipped = max(0, len(text) - len(ascii_text))
        else:
            return f'ERROR: typing failed after {typed}/{len(text)} chars: {r}'
        if typed == 0:
            return 'ERROR: typing failed entirely'
        note = f'typed {typed} chars into {self._win_label(target)}'
        if skipped:
            note += f' ({skipped} chars skipped: unsupported characters)'
        end_focus = self._focused_window_info()
        if end_focus is not None and end_focus.get('id') != target.get('id'):
            note += f' — WARNING: focus moved to {self._win_label(end_focus)} during typing; some text may have landed there'
        self._mark_elements_stale()
        _dep().log.info('type_text: %d chars into %s (skipped %d)', typed, self._win_label(target), skipped)
        return note

    @tool(description="Press a key combo in the focused window: 'enter', 'ctrl+c', 'ctrl+a', 'alt+tab'.", aliases={'combo': ('keys', 'key', 'combination')})
    def press_keys(self, combo: str) -> str:
        """Press an in-app key combination.

        combo: e.g. 'ctrl+enter', 'ctrl+a', 'escape'
        """
        c = combo.strip().lower()
        if not c:
            return 'REFUSED: empty combo'
        try:
            term = self._focused_is_terminal()
        except RuntimeError as e:
            return f'REFUSED: {e}'
        if term:
            return f'REFUSED: the focused window is a terminal ({term}); sending keys to terminals is forbidden'
        if len(c) > 64:
            return 'REFUSED: combo too long'
        parts = [p.strip() for p in c.split('+') if p.strip()]
        if not parts:
            return 'REFUSED: empty combo'
        mods = []
        keys = []
        for p in parts:
            if p in self._MOD_CODES:
                mods.append(self._MOD_CODES[p])
            elif p in self._KEYCODES:
                keys.append(self._KEYCODES[p])
            elif len(p) == 1 and p in self._CHAR_CODES:
                keys.append(self._CHAR_CODES[p])
            else:
                return f"REFUSED: unknown key '{p}'"
        if not keys:
            return 'REFUSED: combo needs a non-modifier key'
        argv = ['key']
        for m in mods:
            argv += [f'{m}:1']
        for k in keys:
            argv += [f'{k}:1', f'{k}:0']
        for m in reversed(mods):
            argv += [f'{m}:0']
        r = self._ydotool(*argv)
        if r == 'ok':
            self._mark_elements_stale()
        return r
    _HOTKEY_NAMES = {'return': 28, 'enter': 28, 'space': 57, 'tab': 15, 'esc': 1, 'escape': 1, 'backspace': 14, 'delete': 111, 'del': 111, 'up': 103, 'down': 108, 'left': 105, 'right': 106, 'home': 102, 'end': 107, 'pageup': 104, 'pagedown': 109, 'print': 99, 'insert': 110, 'minus': 12, 'equal': 13, 'comma': 51, 'period': 52, 'slash': 53, 'semicolon': 39}

    @staticmethod
    def _super_binding_known(combo: str) -> bool:
        """Best-effort check that a Super chord is bound in niri config.

        Follows `include "..."` lines from config.kdl (binds live in
        cfg/keybinds.kdl). True when found OR the config is unreadable
        (legacy allow). False only when readable config lacks the chord
        (it would reach the focused app).
        """
        try:
            base = _dep().HOME / '.config/niri'
            texts: list[str] = [(base / 'config.kdl').read_text(encoding='utf-8').lower()]
        except OSError:
            return True
        try:
            seen: set[str] = set()
            for m in re.findall('include\\s+"([^"]+)"', texts[0]):
                if len(seen) >= 20:
                    break
                inc = (base / m).resolve() if not m.startswith('/') else Path(m)
                try:
                    if str(inc) in seen or not str(inc).startswith(str(base)):
                        continue
                    seen.add(str(inc))
                    if inc.is_file() and inc.stat().st_size < 500000:
                        texts.append(inc.read_text(encoding='utf-8').lower())
                except OSError:
                    continue
        except Exception:
            pass

        def _code(text: str) -> str:
            return '\n'.join((ln.split('//', 1)[0] for ln in text.splitlines()))
        norm = combo.strip().lower().replace(' ', '')
        norm = norm.replace('meta+', 'mod+').replace('super+', 'mod+').replace('win+', 'mod+').replace('logo+', 'mod+')
        blob = '\n'.join((_code(t) for t in texts))
        return norm in blob

    @tool(description="Fire a desktop/compositor shortcut: 'Mod+E' (files), 'Mod+Return' (terminal), 'Mod+F' (fullscreen), 'Mod+Shift+S' (settings). System-wide actions; for in-app chords use press_keys.", gates='press_keys', aliases={'combo': ('keys', 'key')})
    def press_hotkey(self, combo: str) -> str:
        """Fire a compositor hotkey.

        combo: e.g. 'Mod+E', 'Mod+Return', 'Mod+Shift+V'

        Mod maps to Super.
        """
        c = combo.strip().lower().replace(' ', '')
        if not c:
            return 'REFUSED: empty combo'
        if len(c) > 64:
            return 'REFUSED: combo too long'
        parts = [p for p in c.split('+') if p]
        if not parts:
            return 'REFUSED: empty combo'
        mods = []
        keys = []
        has_super = False
        for p in parts:
            if p in ('mod', 'meta', 'super', 'win', 'logo'):
                mods.append(125)
                has_super = True
            elif p in ('ctrl', 'control'):
                mods.append(29)
            elif p == 'alt':
                mods.append(56)
            elif p == 'shift':
                mods.append(42)
            elif p in self._KEYCODES:
                keys.append(self._KEYCODES[p])
            elif p in self._HOTKEY_NAMES:
                keys.append(self._HOTKEY_NAMES[p])
            elif len(p) == 1 and p in self._CHAR_CODES:
                keys.append(self._CHAR_CODES[p])
            else:
                return f"REFUSED: unknown key '{p}'"
        if not keys:
            return 'REFUSED: combo needs a non-modifier key (e.g. Mod+E)'
        if not (has_super and self._super_binding_known(c)):
            try:
                term = self._focused_is_terminal()
            except RuntimeError as e:
                return f'REFUSED: {e}'
            if term:
                return f'REFUSED: the focused window is a terminal ({term}); only bound compositor hotkeys (Mod/Super) may be sent to terminals'
        argv = ['key']
        for m in mods:
            argv += [f'{m}:1']
        for k in keys:
            argv += [f'{k}:1', f'{k}:0']
        for m in reversed(mods):
            argv += [f'{m}:0']
        r = self._ydotool(*argv)
        if r == 'ok':
            self._mark_elements_stale()
            _dep().log.info('press_hotkey: %s', c)
        return r

    @tool(gates='notifications', description='Read future desktop notifications aloud. Actions: start, stop, toggle, status, or mute (mute_apps is comma-separated app names). Private and disabled by default.')
    def notification_reader(self, action: str='status', mute_apps: str='') -> str:
        action = str(action or 'status').strip().lower()
        if action == 'mute':
            apps = [x.strip().lower() for x in str(mute_apps or '').split(',') if x.strip()]
            _dep().set_setting('notification_mute_apps', apps[:32])
            return 'notification mute list set to: ' + (', '.join(apps) or '(empty)')
        if action not in ('start', 'stop', 'toggle', 'status'):
            return 'ERROR: action must be start, stop, toggle, status or mute'
        current = bool(_dep().SETTINGS.get('notification_reader', False))
        if action == 'toggle':
            current = not current
        elif action == 'start':
            current = True
        elif action == 'stop':
            current = False
        if action != 'status':
            _dep().set_setting('notification_reader', current)
            if self._on_notification is not None:
                result = self._on_notification(current)
                if result:
                    return result
        muted = ', '.join(_dep().SETTINGS.get('notification_mute_apps') or []) or 'none'
        return f"notification reader is {('on' if current else 'off')}; muted apps: {muted}"

    @tool(gates='pomodoro', description='Start or control a repeating Pomodoro timer. action: start, stop, status. work_minutes defaults to 25 and break_minutes to 5.')
    def pomodoro(self, action: str='status', work_minutes: float=25, break_minutes: float=5) -> str:
        action = str(action or 'status').strip().lower()
        if action not in ('start', 'stop', 'status'):
            return 'ERROR: action must be start, stop or status'
        callback = self._on_pomodoro
        if callback is None:
            return 'ERROR: pomodoro controller is unavailable'
        try:
            work_minutes = float(work_minutes)
            break_minutes = float(break_minutes)
        except (TypeError, ValueError):
            return 'ERROR: work_minutes and break_minutes must be numbers'
        if not (1 <= work_minutes <= 120 and 1 <= break_minutes <= 60):
            return 'ERROR: work_minutes must be 1-120 and break_minutes 1-60'
        return callback(action, work_minutes, break_minutes)

    def _watch_emit(self, text: str) -> None:
        """Send watcher alerts to the configured assistant announcement path,
        while retaining a desktop notification as a visible fallback."""
        _dep().log.warning('watcher alert received')
        _dep().notify('handsoff watcher: ' + text)
        if self._on_announce is not None:
            try:
                self._on_announce(text)
            except Exception:
                _dep().log.exception('watcher announcement failed')

    @staticmethod
    def _file_watch_loop(path: Path, pattern: re.Pattern, stop: threading.Event, emit) -> None:
        try:
            position = path.stat().st_size
        except OSError:
            position = 0
        deadline = time.monotonic() + 24 * 3600
        while not stop.wait(1.0) and time.monotonic() < deadline:
            try:
                size = path.stat().st_size
                if size < position:
                    position = 0
                if size == position:
                    continue
                with path.open('r', encoding='utf-8', errors='replace') as fh:
                    fh.seek(position)
                    chunk = fh.read(min(size - position, 128000))
                    position = fh.tell()
                for line in chunk.splitlines():
                    if pattern.search(line):
                        emit(f'{path.name}: {line.strip()[:240]}')
            except OSError:
                emit(f'file watcher lost {path}')
                return
            except Exception:
                _dep().log.exception('file watcher failed for %s', path)
                return

    @staticmethod
    def _process_watch_loop(name: str, stop: threading.Event, emit) -> None:
        seen = False
        deadline = time.monotonic() + 24 * 3600
        while not stop.wait(1.0) and time.monotonic() < deadline:
            present = any((n.lower() == name.lower() for _, n in ToolBelt._same_user_procs()))
            if seen and (not present):
                emit(f'process {name} exited')
                return
            seen = present

    @tool(gates='watchers', description='Watch a text file for new lines matching a regular expression. action=start/stop/list; start requires path and pattern. Max four file watchers, each stops on deletion or after 24 hours.')
    def watch_file(self, path: str='', pattern: str='', action: str='start') -> str:
        action = str(action or 'start').strip().lower()
        p = Path(str(path or '')).expanduser().resolve()
        key = str(p)
        if action == 'list':
            with self._watch_lock:
                return 'file watchers: ' + (', '.join(self._file_watchers) or 'none')
        if action == 'stop':
            with self._watch_lock:
                item = self._file_watchers.pop(key, None)
            if item:
                item[0].set()
                return f'stopped watching {p}'
            return f'no file watcher for {p}'
        if action != 'start':
            return 'ERROR: action must be start, stop or list'
        if not p.is_file():
            return f'ERROR: no readable file: {p}'
        try:
            rx = re.compile(str(pattern or ''))
        except re.error as e:
            return f'ERROR: invalid pattern: {e}'
        if not rx.pattern:
            return 'ERROR: pattern must not be empty'
        with self._watch_lock:
            if key not in self._file_watchers and len(self._file_watchers) >= 4:
                return 'ERROR: maximum of four file watchers reached'
            old = self._file_watchers.pop(key, None)
            if old:
                old[0].set()
            stop = threading.Event()
            thread = threading.Thread(target=self._file_watch_loop, args=(p, rx, stop, self._watch_emit), name='watch-file', daemon=True)
            self._file_watchers[key] = (stop, thread)
            thread.start()
        return f'watching {p} for /{rx.pattern}/ (starts at the current end)'

    @tool(gates='watchers', description='Watch one exact same-user process name and announce when it exits. action=start/stop/list; max four process watchers.')
    def watch_process(self, name: str='', action: str='start') -> str:
        action = str(action or 'start').strip().lower()
        name = str(name or '').strip()
        if action == 'list':
            with self._watch_lock:
                return 'process watchers: ' + (', '.join(self._process_watchers) or 'none')
        if not name or len(name) > 128 or (not re.fullmatch('[A-Za-z0-9_.@+-]+', name)):
            return 'ERROR: process name must be an exact simple name'
        if action == 'stop':
            with self._watch_lock:
                item = self._process_watchers.pop(name.lower(), None)
            if item:
                item[0].set()
                return f'stopped watching process {name}'
            return f'no process watcher for {name}'
        if action != 'start':
            return 'ERROR: action must be start, stop or list'
        with self._watch_lock:
            if name.lower() not in self._process_watchers and len(self._process_watchers) >= 4:
                return 'ERROR: maximum of four process watchers reached'
            old = self._process_watchers.pop(name.lower(), None)
            if old:
                old[0].set()
            stop = threading.Event()
            thread = threading.Thread(target=self._process_watch_loop, args=(name, stop, self._watch_emit), name='watch-process', daemon=True)
            self._process_watchers[name.lower()] = (stop, thread)
            thread.start()
        return f'watching process {name} for exit'

    def stop_watchers(self) -> None:
        with self._watch_lock:
            items = list(self._file_watchers.values()) + list(self._process_watchers.values())
            self._file_watchers.clear()
            self._process_watchers.clear()
        for stop, _thread in items:
            stop.set()

    @tool(description="Control niri workspaces: 'go' (switch), 'move' (send window), 'next'/'prev' (one workspace), 'list' (overview).", gates='run_command', aliases={'action': ('cmd', 'command', 'op'), 'target': ('arg', 'value', 'ref')})
    def workspace(self, action: str, target: str='') -> str:
        """Control workspaces.

        action: 'go', 'move', 'next', 'prev' or 'list'
        target: workspace number/name, or '<app> to <workspace>' for 'move'
        """
        act = action.strip().lower()

        def _resolve(ref: str) -> str:
            """Personal alias → number/name ('go to code'); otherwise pass
            through (niri accepts numbers and its own workspace names)."""
            r = re.sub('^\\s*(the\\s+)?(workspace|ws)\\s+', '', ref.strip(), flags=re.IGNORECASE)
            aliases = _dep().SETTINGS.get('workspace_aliases') or {}
            return aliases.get(r.strip().lower(), r)
        t = _resolve(target)

        def _niri(*args: str, timeout: float=8) -> subprocess.CompletedProcess:
            return subprocess.run(['niri', *args], capture_output=True, text=True, timeout=timeout)
        if act in ('go', 'goto', 'switch', 'focus', 'jump'):
            if not t:
                return 'ERROR: name a workspace (number or name)'
            r = _niri('msg', 'action', 'focus-workspace', t)
            if r.returncode != 0:
                return f'ERROR: niri refused ({(r.stderr or r.stdout).strip()})'
            _dep().log.info('workspace: focus %s', t)
            return f'switched to workspace {t}'
        if act in ('next', 'down'):
            r = _niri('msg', 'action', 'focus-workspace-down')
            return 'moved to the workspace below' if r.returncode == 0 else f'ERROR: niri refused ({(r.stderr or r.stdout).strip()})'
        if act in ('prev', 'previous', 'up'):
            r = _niri('msg', 'action', 'focus-workspace-up')
            return 'moved to the workspace above' if r.returncode == 0 else f'ERROR: niri refused ({(r.stderr or r.stdout).strip()})'
        if act in ('move', 'send'):
            if not t:
                return "ERROR: name the target workspace (or '<app> to <workspace>')"
            m = re.split('\\s+to\\s+', t, maxsplit=1, flags=re.IGNORECASE)
            if len(m) == 2:
                app, ref = (m[0].strip(), _resolve(m[1]))
                try:
                    raw = _niri('msg', '--json', 'windows').stdout
                    wins = json.loads(raw or '[]')
                except Exception as e:
                    return f'ERROR: cannot list windows ({e})'
                cands = [w for w in wins if app.lower() in (str(w.get('app_id', '')) + ' ' + str(w.get('title', ''))).lower()]
                if not cands:
                    return f"ERROR: no window matching '{app}'"
                wid = cands[0].get('id')
            else:
                ref = t
                focused = next((w for w in json.loads(_niri('msg', '--json', 'windows').stdout or '[]') if w.get('is_focused')), None)
                if focused is None:
                    return 'ERROR: no focused window to move'
                wid = focused.get('id')
            r = _niri('msg', 'action', 'move-window-to-workspace', '--window-id', str(wid), ref)
            if r.returncode != 0:
                return f'ERROR: niri refused ({(r.stderr or r.stdout).strip()})'
            want = int(ref) if str(ref).strip().isdigit() else None
            moved = None
            if want is not None:
                try:
                    for _ in range(5):
                        time.sleep(0.15)
                        cur = next((w for w in json.loads(_niri('msg', '--json', 'windows', timeout=3).stdout or '[]') if w.get('id') == wid), None)
                        if cur and self._workspace_idx_map(refresh=True).get(cur.get('workspace_id')) == want:
                            moved = True
                            break
                except Exception:
                    pass
            if moved:
                _dep().log.info('workspace: move → %s (verified)', ref)
                return f'moved the window to workspace {ref} (verified)'
            _dep().log.info('workspace: move → %s (unconfirmed)', ref)
            return f"moved the window to workspace {ref}, but the move is NOT confirmed yet — re-check with 'list my workspaces' before typing there"
        if act in ('list', 'show', 'status'):
            try:
                wss = json.loads(_niri('msg', '--json', 'workspaces').stdout or '[]')
                wins = json.loads(_niri('msg', '--json', 'windows').stdout or '[]')
            except Exception as e:
                return f'ERROR: cannot read workspaces ({e})'
            out = []
            for ws in sorted(wss, key=lambda x: x.get('idx', 0)):
                label = f"ws{ws.get('idx')}"
                aliases = _dep().SETTINGS.get('workspace_aliases') or {}
                aka = [k for k, v in aliases.items() if v == str(ws.get('idx'))]
                if ws.get('name'):
                    label += f" ({ws['name']})"
                if aka:
                    label += f" [{', '.join(aka)}]"
                if ws.get('is_focused'):
                    label += ' [current]'
                here = [str(w.get('title') or w.get('app_id') or '?')[:28] for w in wins if w.get('workspace_id') == ws.get('id')]
                out.append(f'{label}: ' + (' | '.join(here[:6]) or '(empty)'))
            return 'workspaces: ' + ' ;; '.join(out) if out else 'no workspaces'
        return f"ERROR: unknown workspace action '{act}' — use go / move / next / prev / list"
    _WIN_POLL_S = 0.25

    def _wait_window_match(self, q: str, timeout: float, ids_before: set | None=None) -> dict | None:
        """Poll the live window list until a window matching `q` appears.

        With ids_before, only windows NOT in that set are considered (used by
        open_app to identify the freshly launched window). Returns the window
        dict or None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                wins = self._niri_windows()
            except RuntimeError:
                wins = []
            for w in wins:
                if ids_before is not None and w.get('id') in ids_before:
                    continue
                if self._win_matches(w, q):
                    return w
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._WIN_POLL_S)

    @tool(description="Focus a desktop window by app name or title substring ('firefox', 'slack', 'alacritty'). Confirms the window really got focus before returning.", aliases={'app': ('window',)})
    def focus_window(self, app: str) -> str:
        """Focus a window by name.

        app: app-id or title substring, e.g. 'firefox'
        """
        q = app.strip().lower()
        if not q:
            return 'REFUSED: name the app or window title to focus'
        try:
            wins = self._niri_windows()
        except RuntimeError as e:
            return f'ERROR: cannot list windows ({e})'
        cands = [w for w in wins if self._win_matches(w, q)]
        if not cands:
            return 'ERROR: no window matching ' + q + '. Open windows: ' + self._win_listing(wins)
        w = cands[0]
        try:
            r = self._niri_msg('msg', 'action', 'focus-window', '--id', str(w.get('id')))
        except Exception as e:
            return f'ERROR: focus failed: {e}'
        if r.returncode != 0:
            return f"ERROR: focus failed: {(r.stderr or 'unknown').strip()}"
        note = f'focused {self._win_label(w)}'
        confirmed = self._wait_focus_id(w.get('id'), 2.0)
        if confirmed:
            note += ' (focus confirmed)'
        else:
            note += ' (focus NOT confirmed yet — it may still be switching)'
        _dep().log.info('focus_window: %s', note)
        return note

    def _wait_focus_id(self, win_id, timeout: float) -> bool:
        """True when window `win_id` reports is_focused within `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                wins = self._niri_windows()
            except RuntimeError:
                return False
            if any((w.get('id') == win_id and w.get('is_focused') for w in wins)):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._WIN_POLL_S)

    @tool(description='Wait until a window matching the name exists (apps take a moment to appear after launch). Returns the window and whether it is focused. Use after open_app, or when a window is slow to appear.', gates='focus_window', aliases={'app': ('window', 'name')})
    def wait_for_window(self, app: str, timeout: float=10.0) -> str:
        """Wait for a window to exist.

        app: app-id or title substring to wait for
        timeout: seconds to wait before giving up (max 30)
        """
        q = app.strip().lower()
        if not q:
            return 'REFUSED: name the app or window title to wait for'
        timeout = min(max(float(timeout or 10.0), 0.5), 30.0)
        w = self._wait_window_match(q, timeout)
        if w is None:
            try:
                listing = self._win_listing(self._niri_windows())
            except RuntimeError:
                listing = 'window list unavailable (niri IPC down?)'
            return f"ERROR: no window matching '{q}' appeared within {timeout:g}s. Open windows: {listing}"
        focus = 'and focused' if w.get('is_focused') else 'but NOT focused — call focus_window before typing'
        _dep().log.info('wait_for_window: %s %s', self._win_label(w), focus)
        return f'window ready: {self._win_label(w)} ({focus})'

    @tool(description='Sleep for `seconds` (0.5-30, default 1) before the next action: lets an app finish drawing, an animation settle, or a dialog appear. Prefer wait_for_window when waiting for an app window.', gates='', aliases={'seconds': ('secs', 'delay', 'duration')})
    def wait(self, seconds: float=1.0) -> str:
        """Wait a moment.

        seconds: how long to sleep (0.5 to 30)
        """
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            s = 1.0
        s = min(max(s, 0.5), 30.0)
        time.sleep(s)
        _dep().log.info('wait: %.1fs', s)
        return f'waited {s:.1f}s'
    _MANIFEST_TTL = 30.0
    _MANIFEST_LOCK = threading.Lock()
    _MANIFEST_CACHE: dict | None = None

    @classmethod
    def _niri_manifest(cls, refresh: bool=False) -> dict:
        """The live capability manifest, cached for _MANIFEST_TTL seconds."""
        with cls._MANIFEST_LOCK:
            c = cls._MANIFEST_CACHE
            if not refresh and c is not None and (time.monotonic() - c['at'] < cls._MANIFEST_TTL):
                return c['data']
            data = cls._build_manifest()
            cls._MANIFEST_CACHE = {'at': time.monotonic(), 'data': data}
            return data

    @staticmethod
    def _niri_help_names(argv: list[str], section: str) -> list[str]:
        """Enum names from a niri help text: the lines indented exactly two
        spaces under `section:` ('Actions:', …), until the next left-flush
        section. Tolerates missing niri (returns [])."""
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=8)
            text = (r.stdout or '') + '\n' + (r.stderr or '')
        except Exception:
            return []
        names: list[str] = []
        inside = False
        for line in text.splitlines():
            if not inside:
                if line.strip() == section + ':':
                    inside = True
                continue
            if not line.startswith('  '):
                if line.strip():
                    break
                continue
            m = re.fullmatch('  ([a-z0-9][a-z0-9-]*)\\s*', line)
            if m:
                names.append(m.group(1))
        return names

    @classmethod
    def _build_manifest(cls) -> dict:
        """Poll niri for everything the desktop-action tools depend on.
        Every piece is optional: a dead IPC just leaves that piece absent."""
        m: dict = {}

        def _json_cli(*args: str):
            r = cls._niri_msg(*args)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or 'niri refused').strip()[:120])
            return json.loads(r.stdout or 'null')
        try:
            v = _json_cli('msg', '--json', 'version')
            m['version'] = str(v.get('compositor') or v.get('cli') or '?')
        except Exception:
            m['version_error'] = 'niri IPC unavailable'
        try:
            wins = cls._niri_windows()
            m['windows'] = {'count': len(wins), 'focused': next((cls._win_label(w) for w in wins if w.get('is_focused')), None), 'placement': {str(w.get('app_id') or '?'): cls._workspace_idx_of(w) for w in wins if w.get('app_id')}}
        except Exception:
            pass
        try:
            wss = _json_cli('msg', '--json', 'workspaces')
            m['workspaces'] = {'count': len(wss), 'indices': sorted((ws.get('idx') for ws in wss or [] if ws.get('idx') is not None))}
        except Exception:
            pass
        try:
            outs = _json_cli('msg', '--json', 'outputs')
            try:
                focused = _json_cli('msg', '--json', 'focused-output').get('name')
            except Exception:
                focused = None
            m['outputs'] = [{'name': name, 'make': str((o or {}).get('make') or '?')[:24], 'model': str((o or {}).get('model') or '?')[:24], 'scale': ((o or {}).get('logical') or {}).get('scale', 1.0), 'focused': name == focused} for name, o in (outs or {}).items()]
        except Exception:
            pass
        try:
            kl = _json_cli('msg', '--json', 'keyboard-layouts')
            names = [str(n) for n in (kl or {}).get('names') or []]
            m['keyboard_layouts'] = {'names': names, 'current': names[(kl or {}).get('current_idx') or 0] if names else None}
        except Exception:
            pass
        acts = cls._niri_help_names(['niri', 'msg', 'action', '--help'], 'Actions')
        if acts:
            m['actions'] = acts
        return m

    @tool(description='Live capability manifest of the niri compositor: version, open windows, workspaces, outputs (name and scale — needed to convert screenshot pixels to pointer coordinates), keyboard layouts, and every supported action on THIS version. Cached ~30s; refresh=true forces a fresh poll.', gates='', aliases={'refresh': ('force',)})
    def niri_capabilities(self, refresh: bool=False) -> str:
        """Report what the running compositor supports right now.

        refresh: true to bypass the 30s cache
        """
        m = self._niri_manifest(refresh=bool(refresh))
        out: list[str] = []
        if 'version' in m:
            out.append(f"niri {m['version']}")
        elif 'version_error' in m:
            out.append(f"niri: {m['version_error']}")
        w = m.get('windows')
        if w:
            out.append(f"windows: {w['count']}" + (f", focused: {w['focused']}" if w.get('focused') else ''))
        if 'workspaces' in m:
            out.append(f"workspaces: {m['workspaces']['count']}")
        outs = m.get('outputs')
        if outs:
            out.append('outputs: ' + ' ;; '.join((f"{o['name']} {o['model']} scale {o.get('scale', 1.0)}" + (' (focused)' if o.get('focused') else '') for o in outs)))
        kl = m.get('keyboard_layouts')
        if kl and kl.get('names'):
            out.append('keyboard layouts: ' + ', '.join(kl['names']) + (f" [current: {kl['current']}]" if kl.get('current') else ''))
        acts = m.get('actions')
        if acts:
            shown = ', '.join(acts[:60])
            more = f' … (+{len(acts) - 60} more)' if len(acts) > 60 else ''
            out.append(f'actions ({len(acts)}): {shown}{more}')
        return '\n'.join(out) or 'niri capability manifest unavailable'

    @tool(description="Close an app's windows by name ('firefox', 'spotify') or 'this' for the focused one. Polite close (like Alt+F4): unsaved work prompts the user. Never force-kills.", gates='run_command', aliases={'app': ('window', 'name')})
    def close_window(self, app: str) -> str:
        """Close window(s) politely.

        app: app name or title fragment, e.g. 'firefox'; 'this' = focused window
        """
        q = app.strip().lower()
        if not q:
            return "REFUSED: name the app or window to close (or pass 'this' for the focused window)"
        try:
            raw = subprocess.run(['niri', 'msg', '--json', 'windows'], capture_output=True, text=True, timeout=8)
            wins = json.loads(raw.stdout or '[]')
        except Exception as e:
            return f'ERROR: cannot list windows ({e})'
        if q in ('this', 'focused', 'current'):
            targets = [w for w in wins if w.get('is_focused')]
            if not targets:
                return 'ERROR: no focused window'
        else:
            targets = [w for w in wins if self._win_matches(w, q)]
            if not targets:
                return 'ERROR: no window matching ' + q + '. Open windows: ' + self._win_listing(wins)
        targets = [w for w in targets if 'handsoff' not in str(w.get('app_id', '')).lower()]
        if not targets:
            return 'REFUSED: I will not close my own bubble — if a restart is needed, use self_restart'
        closed, failed = ([], [])
        for w in targets:
            try:
                r = self._niri_msg('msg', 'action', 'close-window', '--id', str(w.get('id')))
            except Exception as e:
                failed.append(f'{self._win_label(w)} ({e})')
                continue
            label = self._win_label(w)
            (closed if r.returncode == 0 else failed).append(label)
        if failed:
            return 'ERROR: close failed for: ' + ' | '.join(failed)
        lingering = list(closed)
        if closed:
            ids = {self._win_label(t): t.get('id') for t in targets}
            deadline = time.monotonic() + 2.0
            while lingering and time.monotonic() < deadline:
                try:
                    wins = self._niri_windows()
                except RuntimeError:
                    break
                live_ids = {w.get('id') for w in wins}
                lingering = [lbl for lbl in closed if ids.get(lbl) in live_ids]
                if lingering:
                    time.sleep(self._WIN_POLL_S)
        gone = [lbl for lbl in closed if lbl not in lingering]
        if gone:
            _dep().log.info('close_window: %s', '; '.join(gone))
        note = ''
        if gone:
            note += f'closed {len(gone)} window(s): ' + ' | '.join(gone)
            note += ' (confirmed gone)'
        if lingering:
            note += '; still open: ' + ' | '.join(lingering) + ' — the app may be asking about unsaved work'
        return note or 'ERROR: nothing was closed'

    @tool(gates='copy_text', description='Copy text to the Wayland clipboard.', aliases={'text': ('content',)})
    def copy_text(self, text: str) -> str:
        if not text:
            return 'REFUSED: nothing to copy'
        try:
            subprocess.run(['wl-copy', '--', text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except FileNotFoundError:
            return 'ERROR: wl-clipboard is not installed (pacman -S wl-clipboard)'
        except subprocess.TimeoutExpired:
            return 'ERROR: wl-copy timed out'
        return f'copied {len(text)} chars to the clipboard'

    @tool(description='Read the clipboard content (wayland). Use to check what the user copied or to inspect before pasting.')
    def paste_text(self) -> str:
        try:
            r = subprocess.run(['wl-paste', '--no-newline'], capture_output=True, text=True, timeout=8)
        except FileNotFoundError:
            return 'ERROR: wl-clipboard is not installed (pacman -S wl-clipboard)'
        except subprocess.TimeoutExpired:
            return 'ERROR: wl-paste timed out'
        data = r.stdout or ''
        if not data:
            return 'clipboard is empty'
        head = data[:120].replace('\n', ' ')
        more = f' (+{len(data) - 120} more chars)' if len(data) > 120 else ''
        return f'clipboard holds {len(data)} chars: {head!r}{more}'

    @tool(gates='reminders', description="Set a spoken reminder. when_due: 'in 45 minutes', '18:30' (next occurrence), or 'YYYY-MM-DD HH:MM'. repeat_hours>0 recurs (24=daily, 168=weekly).", aliases={'when_due': ('when', 'due', 'at', 'time', 'in', 'seconds'), 'wake_name': ('name', 'label', 'what', 'text'), 'repeat_hours': ('repeat', 'every')})
    def set_reminder(self, wake_name: str, when_due: str, repeat_hours: float=0) -> str:
        name = str(wake_name or '').strip()
        arg = str(when_due or '').strip()
        if not name or not arg:
            return "ERROR: need a reminder name and a due time ('in 45 minutes', '18:30', or 'YYYY-MM-DD HH:MM')"
        now = time.time()
        due: float | None = None
        if re.fullmatch('\\d+(\\.\\d+)?', arg):
            due = now + float(arg)
        elif (m := re.fullmatch('(\\d{1,2}):(\\d{2})(:\\d{2})?', arg)):
            h, mi = (int(m.group(1)), int(m.group(2)))
            se = int(m.group(3)[1:]) if m.group(3) else 0
            if h < 24 and mi < 60 and (se < 60):
                due = _dep()._next_occurrence(h, mi, se, now)
        elif (m := re.fullmatch('(\\d{4})-(\\d{1,2})-(\\d{1,2})(?:[ T](\\d{1,2}):(\\d{2}))?', arg)):
            h = int(m.group(4)) if m.group(4) else 9
            mi = int(m.group(5)) if m.group(5) else 0
            try:
                due = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), h, mi).timestamp()
            except ValueError:
                due = None
        if due is None:
            tot = _dep()._parse_duration(arg)
            if tot is not None:
                due = now + tot
        if due is None:
            return "ERROR: could not understand when_due. Use 'in N minutes/hours/days/weeks', 'HH:MM' (next occurrence), or 'YYYY-MM-DD HH:MM'"
        if due <= now - 5:
            return 'ERROR: that time is already in the past'
        if due - now > _dep().MAX_REMIND_DAYS * 86400:
            return 'ERROR: reminders can be at most a year ahead'
        try:
            repeat = float(repeat_hours)
        except (TypeError, ValueError):
            return 'ERROR: repeat_hours must be a number of hours'
        if repeat and (repeat < 1 / 60 or repeat > 24 * 31):
            return 'ERROR: repeat_hours must be between ~1 minute and a month'
        name = name[:200]
        try:

            def _set(items: list[dict]) -> list[dict]:
                items = [r for r in items if r['name'].lower() != name.lower()]
                items.append({'name': name, 'due': round(due, 3), 'repeat_hours': repeat})
                items.sort(key=lambda r: r['due'])
                del items[_dep().MAX_REMINDERS:]
                return items
            _dep()._update_reminders(_set)
        except OSError as e:
            return f'ERROR: could not save reminder ({type(e).__name__})'
        rep = f', repeating every {_dep()._fmt_dur(repeat * 3600)}' if repeat else ''
        return f"reminder '{name}' set for {_dep()._fmt_when(due)}{rep}"

    @tool(gates='reminders', description='List pending reminders, soonest first.')
    def list_reminders(self) -> str:
        now = time.time()
        items = [r for r in _dep()._load_reminders() if r['due'] > now - 86400]
        if not items:
            return 'no pending reminders'
        out = []
        for r in items[:16]:
            try:
                rep_h = float(r.get('repeat_hours') or 0)
            except (TypeError, ValueError):
                rep_h = 0.0
            rep = f' (repeats every {_dep()._fmt_dur(rep_h * 3600)})' if rep_h else ''
            out.append(f"{r['name']} {_dep()._fmt_when(r['due'])}{rep}")
        return 'reminders: ' + '; '.join(out)

    @tool(gates='reminders', description='Cancel a pending reminder by (part of its) name.')
    def cancel_reminder(self, name: str) -> str:
        name = str(name or '').strip().lower()
        if not name:
            return 'ERROR: which reminder? (see list_reminders)'
        items = _dep()._load_reminders()
        matches = [r for r in items if r['name'].lower() == name or (len(name) >= 3 and r['name'].lower().startswith(name))]
        if not matches:
            return f'no reminder matching {name!r}'
        if len(matches) > 1:
            listing = ', '.join((f"{m['name']} ({_dep()._fmt_when(m['due'])})" for m in matches[:6]))
            return f'several match: {listing} — cancel_reminder the exact name'
        victim = matches[0]['name'].lower()
        try:
            _dep()._update_reminders(lambda items: [r for r in items if r['name'].lower() != victim])
        except OSError as e:
            return f'ERROR: could not save reminders ({type(e).__name__})'
        return f"cancelled reminder {matches[0]['name']!r} (was {_dep()._fmt_when(matches[0]['due'])})"

    @tool(gates='reminders', description='Snooze a reminder: re-arm it N minutes from now. Works while pending or ~90s after it fired.')
    def snooze_reminder(self, name: str, minutes: float=10) -> str:
        name = str(name or '').strip().lower()
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            return 'ERROR: minutes must be a number'
        if not 0.1 <= minutes <= 24 * 60:
            return 'ERROR: minutes must be between 0.1 and 1440'
        items = _dep()._load_reminders()
        matches = [r for r in items if r['name'].lower() == name or (len(name) >= 3 and r['name'].lower().startswith(name))]
        if not matches:
            with _dep()._SNOOZE_LOCK:
                offer = dict(_dep()._snooze_offer) if _dep()._snooze_offer else None
            if offer and time.monotonic() < offer['until'] and (name == offer['name'].lower() or offer['name'].lower().startswith(name)):
                return self._snooze(offer['name'], minutes, 0)
            return f"no reminder matching {name!r} — if it already fired, say 'snooze' within 90 seconds of the announcement"
        if len(matches) > 1:
            listing = ', '.join((f"{m['name']} ({_dep()._fmt_when(m['due'])})" for m in matches[:6]))
            return f'several match: {listing} — snooze_reminder the exact name'
        return self._snooze(matches[0]['name'], minutes, float(matches[0].get('repeat_hours') or 0))

    def _snooze(self, name: str, minutes: float, repeat_hours: float) -> str:
        due = time.time() + minutes * 60
        try:

            def _rearm(items: list[dict]) -> list[dict]:
                items = [r for r in items if r['name'].lower() != name.lower()]
                items.append({'name': name, 'due': round(due, 3), 'repeat_hours': repeat_hours})
                items.sort(key=lambda r: r['due'])
                del items[_dep().MAX_REMINDERS:]
                return items
            _dep()._update_reminders(_rearm)
        except OSError as e:
            return f'ERROR: could not save reminder ({type(e).__name__})'
        return f"reminder '{name}' snoozed until {_dep()._fmt_when(due)}"

    @tool(gates='media', description='Play music from MPD. No query: resume or shuffle something random. With query: replace queue with matching songs and play.')
    def media_play(self, query: str='') -> str:
        query = str(query or '').strip()
        try:
            if not query:
                queue = _dep()._mpc('playlist').splitlines()
                if queue:
                    _dep()._mpc('play')
                    return f'playing (queue had {len(queue)} songs)'
                paths = _dep()._mpc('search', 'filename', '').splitlines()
                if not paths:
                    return 'ERROR: the MPD library is empty — nothing to play'
                picks = random.sample(paths, min(20, len(paths)))
                _dep()._mpc('clear')
                _dep()._mpc('add', *picks)
                _dep()._mpc('play')
                return f'shuffled {len(picks)} random songs from your library and started playing'
            paths: list[str] = []
            for field in ('title', 'artist', 'album', 'filename'):
                found = _dep()._mpc('search', field, query).splitlines()
                for p in found:
                    if p not in paths:
                        paths.append(p)
                if paths:
                    break
            if not paths:
                return f"ERROR: nothing in the library matches {query!r} — try search_library to see what's available"
            picks = paths[:100]
            _dep()._mpc('clear')
            _dep()._mpc('add', *picks)
            _dep()._mpc('play')
            extra = f' (and {len(paths) - 1} more matches queued)' if len(paths) > 1 else ''
            return f'now playing {picks[0]}{extra}'
        except RuntimeError as e:
            return f'ERROR: {e}'

    @tool(gates='media', description="Control the music player: action is one of 'play' (resume), 'pause', 'toggle', 'stop', 'next', 'previous'.")
    def media_control(self, action: str) -> str:
        a = str(action or '').strip().lower()
        mapped = {'play': ['play'], 'pause': ['pause'], 'stop': ['stop'], 'next': ['next'], 'skip': ['next'], 'previous': ['prev'], 'prev': ['prev'], 'back': ['prev']}
        if a == 'toggle':
            try:
                paused = '[paused]' in _dep()._mpc('status')
                _dep()._mpc('play' if paused else 'pause')
                return f"music player: {('play' if paused else 'pause')}"
            except RuntimeError as e:
                return f'ERROR: {e}'
        if a not in mapped:
            return 'ERROR: action must be one of play, pause, toggle, stop, next, previous'
        try:
            _dep()._mpc(*mapped[a])
            return f'music player: {a}'
        except RuntimeError as e:
            return f'ERROR: {e}'

    @tool(gates='media', description="Set the music player's volume (0-100). This is the music output volume, not the whole system volume.")
    def media_volume(self, level: str) -> str:
        s = str(level or '').strip().rstrip('%')
        if not re.fullmatch('[+-]?\\d+', s):
            return 'ERROR: level must be a number 0-100'
        level = max(0, min(100, int(s)))
        try:
            _dep()._mpc('volume', str(level))
            return f'music volume set to {level}%'
        except RuntimeError as e:
            return f'ERROR: {e}'

    @tool(gates='media', description="What is currently playing: song, playing/paused state, position, volume, queue length. Use for 'what's this song?'.")
    def now_playing(self) -> str:
        try:
            cur = _dep()._mpc('current').strip()
            status = _dep()._mpc('status')
        except RuntimeError as e:
            return f'ERROR: {e}'
        if not cur:
            return 'nothing is playing (the music queue is empty)'
        state = 'playing' if '[playing]' in status else 'paused'
        vol = re.search('volume:\\s*(\\d+)%', status)
        queue = re.search('\\[\\w+\\]\\s+#(\\d+)/(\\d+)', status)
        pos = ''
        if queue:
            pos = f', song {queue.group(1)} of {queue.group(2)} in the queue'
        return f"{cur} [{state}{pos}{(', volume ' + vol.group(1) + '%' if vol else '')}]"

    @tool(gates='media', description='Search the music library (artist/title/album/filename). Use before playing vague requests. Returns up to 15 paths.')
    def search_library(self, query: str) -> str:
        query = str(query or '').strip()
        if not query:
            return 'ERROR: what should I search for?'
        try:
            paths: list[str] = []
            for field in ('title', 'artist', 'album', 'filename'):
                for p in _dep()._mpc('search', field, query).splitlines():
                    if p not in paths:
                        paths.append(p)
            if not paths:
                return f'no songs in the library match {query!r}'
            head = paths[:15]
            extra = f' — and {len(paths) - 15} more' if len(paths) > 15 else ''
            return f'{len(paths)} match(es): ' + '; '.join(head) + extra + ' — play one with media_play'
        except RuntimeError as e:
            return f'ERROR: {e}'

    @tool(gates='calendar', description="Print a calendar month grid with today marked * — reason about weekdays/day counts. month: 'YYYY-MM' or empty.")
    def calendar_month(self, month: str='') -> str:
        arg = str(month or '').strip().lower()
        today = datetime.date.today()
        if not arg or arg in ('this', 'current', 'now'):
            y, mo = (today.year, today.month)
        elif (m := re.fullmatch('(\\d{4})-(\\d{1,2})', arg)):
            y, mo = (int(m.group(1)), int(m.group(2)))
        else:
            return "ERROR: month must look like 'YYYY-MM' (or empty for this month)"
        try:
            first = datetime.date(y, mo, 1)
        except ValueError:
            return f'ERROR: no such month {y}-{mo:02d}'
        nxt_y, nxt_mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        ndays = (datetime.date(nxt_y, nxt_mo, 1) - first).days
        head = f'{_dep()._MONTH_NAMES[mo - 1]} {y}'.center(21).rstrip()
        rows = [head, 'Mo Tu We Th Fr Sa Su']
        row = '   ' * first.weekday()
        for d in range(1, ndays + 1):
            mark = '*' if d == today.day and y == today.year and (mo == today.month) else ' '
            row += f'{d:2d}{mark}'
            if (first.weekday() + d) % 7 == 0:
                rows.append(row.rstrip())
                row = ''
        if row.strip():
            rows.append(row.rstrip())
        rows.append(f'today is {_dep()._DAY_NAMES[today.weekday()]} {today.day:02d} {_dep()._MONTH_NAMES[today.month - 1][:3]} {today.year}')
        return '\n'.join(rows)

    @tool(gates='calendar', description='Read upcoming events from the configured ICS calendar source(s). days=1 = today, 2 = today+tomorrow.', aliases={'days': ('how_many_days', 'range')})
    def read_calendar(self, days: int=1) -> str:
        sources = _dep().SETTINGS.get('calendar_ics') or []
        if not sources:
            return "no calendar is configured — add an ICS source in handsoff Settings (a Google Calendar 'secret iCal address' URL or a local .ics file path)"
        try:
            days = max(1, min(14, int(days)))
        except (TypeError, ValueError):
            return 'ERROR: days must be a number (1-14)'
        now = datetime.datetime.now()
        win_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        win_end = win_start + datetime.timedelta(days=days)
        events: list[dict] = []
        bad: list[str] = []
        for src in sources:
            if re.match('^https?://', src, re.I) and (not self._perm.get('web_access', True)):
                return "REFUSED: fetching calendar URLs needs the 'web_access' permission, disabled in handsoff settings"
            try:
                events.extend(_dep()._ics_events_from_text(_dep()._ics_fetch(src), win_start, win_end))
            except Exception as e:
                bad.append(f'{src} ({type(e).__name__})')
        if bad and (not events):
            return 'ERROR: could not read calendar source(s): ' + '; '.join(bad)
        if not events:
            return f'no events in the next {days} day(s)'
        label = 'Today' if days == 1 else f'Next {days} days'
        out = _dep()._fmt_events(events)
        if bad:
            out += f"  [unreadable: {'; '.join(bad)}]"
        return f'{label}: {out}'

    @tool(description="Current weather + today/tomorrow forecast for a place, e.g. 'Berlin', 'New York', 'Tokyo'. Omit the place to use the user's home place.", gates='web_access', aliases={'place': ('city',)})
    def get_weather(self, place: str='') -> str:
        place = (place or str(_dep().SETTINGS.get('home_place', ''))).strip()
        if not place:
            return "ERROR: name a place, e.g. 'Hamburg'"
        try:
            geo = _dep()._geocode(place)
            if not geo:
                return f'ERROR: unknown place: {place}'
            lat, lon, where = geo
            data = json.loads(_dep()._http_get(f'https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code&forecast_days=2&timezone=auto'))
            cur = data['current']
            desc = _dep()._WMO.get(int(cur['weather_code']), 'changeable sky')
            d = data['daily']
            lines = [f"Weather in {where} (local time {cur['time'][-5:]}):", f"now: {cur['temperature_2m']}°C, feels like {cur['apparent_temperature']}°C, {desc}, humidity {cur['relative_humidity_2m']}%, wind {cur['wind_speed_10m']} km/h", f"today: {d['temperature_2m_min'][0]}–{d['temperature_2m_max'][0]}°C, {_dep()._WMO.get(int(d['weather_code'][0]), 'changeable')}, precip {d['precipitation_sum'][0]} mm", f"tomorrow: {d['temperature_2m_min'][1]}–{d['temperature_2m_max'][1]}°C, {_dep()._WMO.get(int(d['weather_code'][1]), 'changeable')}, precip {d['precipitation_sum'][1]} mm"]
            return '\n'.join(lines)
        except Exception as e:
            _dep().log.warning('get_weather failed: %s', e)
            return f'ERROR: weather lookup failed ({type(e).__name__})'

    @tool(description='Search the public web for current information (news, prices, scores, facts that may have changed recently). Returns result titles with snippets.', gates='web_access', aliases={'query': ('q',)})
    def web_search(self, query: str) -> str:
        if not query:
            return 'REFUSED: empty query'
        try:
            results = _dep()._ddg_lite(query)
            source = 'web'
            if not results:
                results = _dep()._wiki_search(query)
                source = 'wikipedia'
            if not results:
                return f'ERROR: no results for {query!r}'
            lines = [f"Results for '{query}' ({source}):"]
            for t, s in results[:4]:
                lines.append(f'- {t}: {s[:220]}')
            return '\n'.join(lines)
        except Exception as e:
            _dep().log.warning('web_search failed: %s', e)
            return f'ERROR: web search failed ({type(e).__name__})'

    @tool(description='World news headlines.', gates='web_access', aliases={'count': ('n', 'limit')})
    def world_events(self, count: int=4) -> str:
        """Show current world headlines."""
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = 4
        n = max(1, min(n, 5))
        events, degraded = _dep()._world_events('all', n)
        if not events:
            return 'ERROR: world news unavailable (offline?)' if degraded else 'no world headlines right now'
        lines = ['World headlines:']
        for e in events:
            mark = '⚠ ' if e.get('urgent') else ''
            lines.append(f"- {mark}{e['title']}")
        return '\n'.join(lines)

    @tool(description='Look up an encyclopedia summary about a person, place, thing or concept (Wikipedia). Better than web_search for stable facts.', gates='web_access', aliases={'topic': ('subject', 'query')})
    def lookup_fact(self, topic: str) -> str:
        if not topic:
            return 'REFUSED: name a topic'
        try:
            t = urllib.parse.quote(topic.strip().replace(' ', '_'))
            data = json.loads(_dep()._http_get('https://en.wikipedia.org/api/rest_v1/page/summary/' + t))
            if data.get('type') == 'standard' and data.get('extract'):
                return f"{data.get('title')}: {data['extract'][:1200]}"
            hits = _dep()._wiki_search(topic)
            if hits:
                return 'Top matches: ' + '; '.join((f'{a} — {b[:120]}' for a, b in hits[:3]))
            return f'ERROR: nothing found on {topic!r}'
        except Exception as e:
            _dep().log.warning('lookup_fact failed: %s', e)
            return f'ERROR: fact lookup failed ({type(e).__name__})'

    @tool(description='Current local date, weekday and time.')
    def get_datetime(self) -> str:
        now = datetime.datetime.now()
        return f'Local date and time: {_dep()._DAY_NAMES[now.weekday()]}, {now.day:02d} {_dep()._MONTH_NAMES[now.month - 1]} {now.year}, {now.hour:02d}:{now.minute:02d} (timezone {now.astimezone().tzname()}). Unix epoch: {int(now.timestamp())}'
    SCREENSHOT_FILE = _dep().STATE_DIR / 'screen.png'

    @staticmethod
    def _shot_path(region: str):
        """Build a safe grim argv. Returns (argv_tail, path) or None."""
        path = ToolBelt.SCREENSHOT_FILE
        if not region:
            return ([], path)
        try:
            g = re.fullmatch('(\\d{1,5})[ ,xX]+(\\d{1,5})[ ,xX]+(\\d{1,5})[ ,xX]+(\\d{1,5})', region.strip())
            if not g:
                return None
            x, y, w, hgt = (int(v) for v in g.groups())
            if not (0 <= x <= 20000 and 0 <= y <= 20000 and (0 < w <= 20000) and (0 < hgt <= 20000)):
                return None
            return (['-g', f'{x},{y} {w}x{hgt}'], path)
        except Exception:
            return None

    def _take_screenshot(self, region: str='', scale_down: bool=True) -> str:
        """Capture the screen (or a region) to SCREENSHOT_FILE. Returns error or ''."""
        built = self._shot_path(region)
        if built is None:
            return "ERROR: region must be 'x y width height' in pixels"
        tail, path = built
        try:
            path.unlink(missing_ok=True)
            argv = ['grim']
            if scale_down:
                argv += ['-s', '0.6']
            proc = subprocess.run(argv + tail + [str(path)], capture_output=True, text=True, timeout=15)
            if proc.returncode != 0 or not path.exists():
                return 'ERROR: screenshot failed: ' + (proc.stderr or 'unknown').strip()[:200]
        except FileNotFoundError:
            return 'ERROR: grim is not installed (pacman -S grim)'
        except subprocess.TimeoutExpired:
            return 'ERROR: screenshot timed out'
        return ''

    @tool(description="Take a screenshot and see it as an image. Use for 'what's on my screen', non-text UI, verifying what you typed.", gates='screen_access')
    def see_screen(self, question: str='', region: str='') -> str:
        """Look at the screen with vision.

        question: what you want to find out from the screen
        region: optional 'x y width height' in pixels
        """
        err = self._take_screenshot(region)
        if err:
            return err
        try:
            b64 = base64.b64encode(self.SCREENSHOT_FILE.read_bytes()).decode('ascii')
        except OSError as e:
            return f'ERROR: cannot read screenshot: {e}'
        self._last_images = [b64]
        hint = f' (user asks: {question[:200]})' if question else ''
        return f'Screenshot captured and attached as an image{hint}.'

    @tool(description='Read all visible text on screen via OCR (fast, no vision model). Best for reading articles, chats, code or error messages on screen.', gates='screen_access')
    def read_screen_text(self, region: str='') -> str:
        """Read screen text via OCR.

        region: optional 'x y width height' in pixels
        """
        err = self._take_screenshot(region, scale_down=False)
        if err:
            return err
        try:
            proc = subprocess.run(['tesseract', str(self.SCREENSHOT_FILE), 'stdout', '--psm', '3'], capture_output=True, text=True, timeout=40)
        except FileNotFoundError:
            return 'ERROR: tesseract is not installed (pacman -S tesseract)'
        except subprocess.TimeoutExpired:
            return 'ERROR: OCR timed out'
        text = ' '.join(proc.stdout.split())
        if not text:
            return 'OCR found no readable text on screen.'
        _dep().log.info('read_screen_text: %d chars', len(text))
        return f'Text on screen: {text[:4000]}'
    KILL_CONFIRM_S = 60.0

    @staticmethod
    def _same_user_procs() -> list[tuple[int, str]]:
        """[(pid, name)] for every process owned by the CURRENT user — the
        AI can never see (or kill) other users' processes, including root."""
        out = []
        me = os.getuid() if hasattr(os, 'getuid') else -1
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f'/proc/{entry}/status', encoding='ascii', errors='replace') as fh:
                    fields = dict((line.split(':', 1) for line in fh if ':' in line))
                if int(fields.get('Uid', '-1\t-1').split()[0]) != me:
                    continue
                name = fields.get('Name', '').strip()
                if name:
                    out.append((int(entry), name))
            except (OSError, ValueError, KeyError, IndexError):
                continue
        return out

    @staticmethod
    def _port_owner(port: int) -> list[int]:
        """PIDs of same-user processes with a LISTEN socket on this port
        (parsed from /proc/net/tcp{,6}; no external tools)."""
        inodes: set[str] = set()
        for path in ('/proc/net/tcp', '/proc/net/tcp6'):
            try:
                with open(path, encoding='ascii') as fh:
                    next(fh)
                    for line in fh:
                        f = line.split()
                        if len(f) < 10 or f[3] != '0A':
                            continue
                        try:
                            if int(f[1].split(':')[1], 16) == port:
                                inodes.add(f[9])
                        except (ValueError, IndexError):
                            continue
            except OSError:
                continue
        pids = []
        me = os.getuid() if hasattr(os, 'getuid') else -1
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            try:
                with open(f'/proc/{entry}/status', encoding='ascii', errors='replace') as fh:
                    fields = dict((line.split(':', 1) for line in fh if ':' in line))
                if int(fields.get('Uid', '-1\t-1').split()[0]) != me:
                    continue
                for fd in os.listdir(f'/proc/{entry}/fd'):
                    try:
                        link = os.readlink(f'/proc/{entry}/fd/{fd}')
                    except OSError:
                        continue
                    if link.startswith('socket:['):
                        ino = link[8:-1]
                        if ino in inodes:
                            pids.append(int(entry))
                            break
            except (OSError, ValueError, KeyError, IndexError):
                continue
        return pids

    @tool(gates='run_command', description="Stop (SIGTERM) one of the user's own processes by EXACT name or by the port it listens on. Two steps: kill_process first shows the match and asks to confirm; then confirm_kill('yes') actually stops it.")
    def kill_process(self, target: str) -> str:
        target = str(target or '').strip()
        if not target:
            return 'ERROR: name the process or the port it listens on'
        cands: list[tuple[int, str]] = []
        if target.isdigit() and 0 < int(target) <= 65535:
            port = int(target)
            for pid in self._port_owner(port):
                name = next((n for p, n in self._same_user_procs() if p == pid), str(pid))
                cands.append((pid, name))
        else:
            low = target.lower()
            cands = [(p, n) for p, n in self._same_user_procs() if n.lower() == low]
        if not cands:
            return f"ERROR: no process of yours matches {target!r} (exact name or listening port; other users' processes are invisible)"
        if len(cands) > 1:
            listing = ', '.join((f'{n} (pid {p})' for p, n in cands[:6]))
            return f'ERROR: {len(cands)} processes match — kill_process needs an EXACT single match, these all match: {listing}'
        pid, name = cands[0]
        if pid == os.getpid():
            return 'REFUSED: that is me — for a restart of the assistant, ask me to restart myself instead'
        if name == 'systemd':
            return 'REFUSED: systemd --user manages your whole session — killing it would stop every user service, including me'
        _dep()._kill_offer.clear()
        _dep()._kill_offer.update({'pid': pid, 'name': name, 'until': time.monotonic() + self.KILL_CONFIRM_S})
        _dep().log.info('kill_process: offered pid %d (%s), awaiting confirm', pid, name)
        return f"About to stop {name} (pid {pid}). Nothing happened yet — call confirm_kill('yes') to stop it, or confirm_kill('no') to cancel."

    @tool(gates='run_command', description="Second step of kill_process: confirm_kill('yes') stops the offered process; confirm_kill('no') cancels the offer.")
    def confirm_kill(self, answer: str='yes') -> str:
        offer = dict(_dep()._kill_offer) if _dep()._kill_offer else None
        if not offer:
            return 'ERROR: nothing to confirm — call kill_process first'
        if time.monotonic() >= offer['until']:
            _dep()._kill_offer.clear()
            return 'ERROR: the kill offer expired — run kill_process again'
        ans = str(answer or 'yes').strip().lower()
        if ans not in ('yes', 'no', 'y', 'n'):
            return 'ERROR: answer with yes or no'
        if ans in ('no', 'n'):
            _dep()._kill_offer.clear()
            _dep().log.info('kill_process: cancelled by user/model')
            return 'Cancelled — nothing was stopped.'
        pid, name = (offer['pid'], offer['name'])
        _dep()._kill_offer.clear()
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return f'{name} (pid {pid}) already exited.'
        except PermissionError:
            return f'ERROR: not allowed to stop {name} (pid {pid})'
        _dep().log.warning('kill_process: SIGTERM pid %d (%s) confirmed', pid, name)
        return f'Stopped {name} (pid {pid}) (SIGTERM sent).'

    @tool(description="Second step for a CONFIRM-offered tool: 'yes' runs, 'no' cancels.", gates='', aliases={'answer': ('confirm', 'reply')})
    def confirm_action(self, answer: str='yes') -> str:
        with self._confirmation_lock():
            offer = self._pending_confirm
            if not offer:
                return 'ERROR: nothing to confirm — no CONFIRM-class tool call is pending'
            if time.monotonic() >= offer['until']:
                self._pending_confirm = None
                return 'ERROR: the confirmation offer expired — make the request again'
            current_turn = getattr(self, '_user_turn_marker', 0)
            if current_turn <= offer.get('turn', 0):
                return 'ERROR: confirmation was requested in the same turn; try again in a later user turn'
            ans = str(answer or 'yes').strip().lower()
            if ans not in ('yes', 'no', 'y', 'n'):
                return 'ERROR: answer with yes or no'
            if ans in ('no', 'n'):
                tool = offer['tool']
                self._pending_confirm = None
                _dep().log_decision(tool, '', 'CONFIRM', 'cancelled by user')
                _dep().log.info('confirm_action: %s cancelled', tool)
                return f'Cancelled — {tool} was not run.'
            self._pending_confirm = None
            tool, args = (offer['tool'], offer['args'])
        _dep().log_decision(tool, json.dumps(args)[:120], 'CONFIRM', 'confirmed; running')
        self._confirm_running = tool
        try:
            out, err = self.execute(tool, args)
        finally:
            self._confirm_running = None
        _dep().log_decision(tool, json.dumps(args)[:120], 'EXECUTED', 'refused/errored' if err else 'ok')
        return out
    JOB_ANNOUNCE_S = 20.0

    @tool(description='Run a whitelisted command as a background job.', gates='run_command')
    def start_command(self, command: str) -> str:
        """Run a whitelisted command as a bounded background job.

        command: same single whitelisted command run_command accepts
        """
        with self._job_lock:
            if len(self._jobs) >= BoundedJob.MAX_JOBS:
                return f"ERROR: job limit reached ({BoundedJob.MAX_JOBS}) — check or reap with job_status first: {', '.join(sorted(self._jobs))}"
        argv, _exe, err, is_restart = self._validate_command(command)
        if err:
            return err
        _dep().log.info('start_command: %s', _dep()._log_metadata(command, 'command'))
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as e:
            return f'ERROR: launch failed: {e}'
        if is_restart:
            self._on_restart_pending()
        with self._job_lock:
            self._job_seq += 1
            job_id = f'job-{self._job_seq}'
            self._jobs[job_id] = BoundedJob(job_id, command.strip(), proc)
        return f'started {job_id}: {command.strip()} — it runs in the background; call job_status to check it. I will announce when it finishes.'

    @tool(description='State/output of start_command jobs; finished announced', gates='run_command', aliases={'job_id': ('id', 'job')})
    def job_status(self, job_id: str='') -> str:
        """Report state/output of background jobs.

        job_id: a specific job id, or empty for all jobs
        """
        with self._job_lock:
            jobs = dict(self._jobs)
        if job_id:
            job = jobs.get(job_id.strip())
            if job is None:
                known = ', '.join(sorted(jobs)) or 'none'
                return f'ERROR: no job {job_id!r} (jobs: {known})'
            jobs = {job_id.strip(): job}
        if not jobs:
            return 'no background jobs'
        lines: list[str] = []
        reaped: list[str] = []
        for jid, job in sorted(jobs.items()):
            state, done = job.poll()
            status = job.status_text(state, done)
            if done:
                if not job._announced:
                    job._announced = True
                    self._announce_job(status)
                job._join_drain(timeout=5.0)
                out = job.output_tail(4096)
                out = (out or '').strip()
                if len(out) > BoundedJob.MAX_OUTPUT:
                    out = out[:BoundedJob.MAX_OUTPUT] + ' …(truncated)'
                lines.append(f"{status}\noutput:\n{out or '(no output)'}")
                if state != 'running':
                    reaped.append(jid)
            else:
                lines.append(status)
        with self._job_lock:
            for jid in reaped:
                self._jobs.pop(jid, None)
        return '\n\n'.join(lines)

    @tool(description='Self-diagnostic: deployment hashes, Ollama, TTS/STT, mic, niri, systemd. Read-only', gates='')
    def handsoff_doctor(self) -> str:
        """Run the doctor diagnostic and return the report."""
        return _dep().run_doctor()

    @staticmethod
    def _parse_tsv(tsv: str) -> list[dict]:
        """Tesseract TSV -> line-level elements with pixel boxes.

        Rows: level page block par line word left top width height conf text.
        Words are grouped by (block, par, line) into one element per visual
        line, keeping the union bounding box and the mean confidence."""
        rows: dict[tuple, list] = {}
        for line in tsv.splitlines()[1:]:
            parts = line.split('\t')
            if len(parts) < 12:
                continue
            try:
                level, blk, par, ln = (int(parts[0]), int(parts[2]), int(parts[3]), int(parts[4]))
                x, y, w, h, conf = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]), float(parts[10]))
            except ValueError:
                continue
            word = parts[11].strip()
            if level != 5 or not word or conf < 30:
                continue
            rows.setdefault((blk, par, ln), []).append((x, y, w, h, word))
        out = []
        for words in rows.values():
            words.sort(key=lambda t: t[0])
            x0 = min((w[0] for w in words))
            y0 = min((w[1] for w in words))
            x1 = max((w[0] + w[2] for w in words))
            y1 = max((w[1] + w[3] for w in words))
            out.append({'text': ' '.join((w[4] for w in words)), 'x': (x0 + x1) // 2, 'y': (y0 + y1) // 2, 'w': x1 - x0, 'h': y1 - y0})
        out.sort(key=lambda e: (e['y'], e['x']))
        return out[:80]

    def _screen_elements_fmt(self) -> str:
        listing = '\n'.join((f"{i + 1}. {e['text'][:70]!r} at ({e['x']},{e['y']})" for i, e in enumerate(getattr(self, '_elements', [])[:80])))
        return listing

    def _operator_click(self, x: int, y: int, what: str) -> str:
        if not self._perm.get('operator', False):
            return "REFUSED: mouse control ('operator') is disabled in handsoff settings"
        try:
            x, y = (int(x), int(y))
            assert 0 <= x <= 20000 and 0 <= y <= 20000
        except (TypeError, ValueError, AssertionError):
            return f'ERROR: invalid click target ({x}, {y})'
        pre_note = self._stale_scan_note()
        scale = getattr(self, '_pointer_scale', 1.0) or 1.0
        px = max(0, int(round(x / scale)))
        py = max(0, int(round(y / scale)))
        r1 = self._ydotool('mousemove', '-a', '-x', str(px), '-y', str(py))
        if r1 != 'ok':
            return f'ERROR: mouse move failed: {r1}'
        r2 = self._ydotool('click', '0xC0')
        if r2 != 'ok':
            return f'ERROR: click failed: {r2}'
        self._mark_elements_stale()
        conv = '' if scale == 1.0 else f' → pointer ({px},{py})'
        _dep().log.info('operator: clicked %s at (%d,%d)%s', what, x, y, conv)
        return f'clicked {what} at ({x},{y}){conv}{pre_note}'

    def _detect_pointer_scale(self) -> float:
        """Pointer-space scale of the focused output.

        grim screenshots and tesseract coordinates are PHYSICAL pixels while
        ydotool mousemove -a moves the LOGICAL pointer — clicks must divide
        by this scale. Falls back to 1.0 when niri cannot be asked (CI, IPC
        down): on scale-1 setups the no-op is exactly right."""
        try:
            r = self._niri_msg('msg', '--json', 'focused-output')
            out = json.loads(r.stdout or 'null')
            scale = float((out.get('logical') or {}).get('scale') or 1.0)
            if scale > 0:
                return scale
        except Exception:
            pass
        return 1.0

    def _mark_elements_stale(self) -> None:
        """Forget the freshness of the last screen_elements scan — the
        screen just changed (typing, keys, click, scroll, launch…)."""
        self._elements_ts = 0.0

    def _stale_scan_note(self) -> str:
        """Warning suffix when the cached element scan may be outdated."""
        ts = getattr(self, '_elements_ts', 0.0)
        if not ts:
            return ''
        age = time.monotonic() - ts
        if age > 90:
            return f' (element scan is {age:.0f}s old — the screen may have changed; run screen_elements again)'
        return ''

    @tool(gates='screen_access', description='List clickable text elements on screen with numbers and positions. Run this before click_element; re-run after anything changes — clicks, typing, scrolling and launches all make the last scan stale, and click_element will warn when it is.')
    def screen_elements(self) -> str:
        if not hasattr(self, '_elements'):
            self._elements = []
        err = self._take_screenshot('', scale_down=False)
        if err:
            return err
        try:
            proc = subprocess.run(['tesseract', str(self.SCREENSHOT_FILE), 'stdout', 'tsv'], capture_output=True, text=True, timeout=40)
        except FileNotFoundError:
            return 'ERROR: tesseract is not installed (pacman -S tesseract)'
        except subprocess.TimeoutExpired:
            return 'ERROR: OCR timed out'
        self._elements = self._parse_tsv(proc.stdout)
        if not self._elements:
            self._mark_elements_stale()
            return 'No clickable text elements found on screen.'
        self._pointer_scale = self._detect_pointer_scale()
        self._elements_ts = time.monotonic()
        _dep().log.info('screen_elements: %d lines (pointer scale %.2f)', len(self._elements), self._pointer_scale)
        return f'{len(self._elements)} clickable text elements (coordinates are screen pixels; pointer scale {self._pointer_scale:g}):\n' + self._screen_elements_fmt()

    @tool(gates='operator', description='Click a text element from the last screen_elements scan by its number or (part of) its text. Run screen_elements first.', aliases={'ref': ('element', 'name', 'label', 'target')})
    def click_element(self, ref: str) -> str:
        els = getattr(self, '_elements', [])
        if not els:
            return 'ERROR: no element scan yet — run screen_elements first'
        ref_s = str(ref).strip().lower()
        pick = None
        if ref_s.isdigit() and 1 <= int(ref_s) <= len(els):
            pick = els[int(ref_s) - 1]
        else:
            exact = [e for e in els if ref_s == e['text'].strip().lower()]
            part = [e for e in els if ref_s in e['text'].strip().lower()]
            pick = (exact or part or [None])[0]
        if pick is None:
            return f'ERROR: no element matching {ref!r} — run screen_elements again and pick from the list'
        return self._operator_click(pick['x'], pick['y'], repr(pick['text'][:40]))

    @tool(gates='operator', description='Click at absolute pixel coordinates. Prefer click_element with a screen_elements scan.')
    def click_at(self, x: int, y: int) -> str:
        return self._operator_click(x, y, 'target')
    _SCROLL_SIGNS = {'up': 1, 'down': -1, 'right': 1, 'left': -1}

    @tool(gates='operator', description='Scroll the mouse wheel by `amount` notches (default 3): direction up / down / left / right. Affects whatever window is under the pointer — click_element or click_at first to aim it. Content moves, so re-run screen_elements before clicking anything after.', aliases={'direction': ('dir', 'way'), 'amount': ('notches', 'clicks', 'lines')})
    def scroll(self, direction: str='down', amount: int=3) -> str:
        """Scroll the mouse wheel.

        direction: 'up', 'down', 'left' or 'right'
        amount: wheel notches (1-25)
        """
        if not getattr(self, '_perm', {}).get('operator', False):
            return "REFUSED: mouse control ('operator') is disabled in handsoff settings"
        d = str(direction or 'down').strip().lower()
        if d not in self._SCROLL_SIGNS:
            return 'REFUSED: direction must be up, down, left or right'
        try:
            n = int(amount)
        except (TypeError, ValueError):
            n = 3
        n = max(1, min(n, 25))
        pre_note = self._stale_scan_note()
        sign = self._SCROLL_SIGNS[d]
        if d in ('up', 'down'):
            argv = ('mousemove', '-w', '-x', '0', '-y', str(sign * n))
        else:
            argv = ('mousemove', '-w', '-x', str(sign * n), '-y', '0')
        r = self._ydotool(*argv)
        if r != 'ok':
            return f'ERROR: scroll failed: {r}'
        self._mark_elements_stale()
        _dep().log.info('scroll: %s x%d', d, n)
        return f'scrolled {d} {n} notch(es){pre_note}'
    APP_ALIASES = {'browser': ('firefox', 'chromium', 'google-chrome-stable'), 'web': ('firefox', 'chromium', 'google-chrome-stable'), 'internet': ('firefox', 'chromium'), 'files': ('nautilus', 'dolphin', 'thunar', 'nemo'), 'file manager': ('nautilus', 'dolphin', 'thunar'), 'calculator': ('kcalc', 'qalculate-gtk', 'gnome-calculator', 'xcalc'), 'music': ('spotify', 'lollypop', 'rhythmbox', 'elisa'), 'settings': ('gnome-control-center', 'systemsettings', 'xfce4-settings'), 'text editor': ('gedit', 'kate', 'mousepad', 'gnome-text-editor'), 'editor': ('gedit', 'kate', 'mousepad'), 'terminal': ('foot', 'alacritty', 'kitty')}
    OPEN_APP_WAIT_S = 12.0
    _WIN_GRACE_S = 3.0

    @tool(description="Launch a desktop app by name ('firefox', 'spotify', 'files'…), wait for its window to appear and report which window it is. Then focus_window to aim typing at it.", gates='run_command', aliases={'app': ('name',)})
    def open_app(self, app: str) -> str:
        a = app.strip().lower()
        if not a:
            return 'REFUSED: name the app to open'
        candidates = self.APP_ALIASES.get(a, (a,))
        resolved = None
        chosen = a
        for cand in candidates:
            cand = re.sub('[^a-z0-9._-]', '', cand)
            if not cand or cand in ('sudo', 'bash', 'sh', 'python', 'python3', 'xterm'):
                return f"REFUSED: won't open '{app}'"
            resolved = shutil.which(cand)
            if resolved:
                chosen = cand
                break
        if resolved is None:
            return f"ERROR: no program matching '{app}' is installed"
        try:
            ids_before = {w.get('id') for w in self._niri_windows()}
        except RuntimeError:
            ids_before = None
        try:
            subprocess.Popen(['niri', 'msg', 'action', 'spawn', '--', resolved], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return f'ERROR: launch failed: {e}'
        _dep().log.info('open_app: %s', chosen)
        if ids_before is None:
            return f'launched {chosen} (could not verify the window — niri window list unavailable)'
        w, already = self._wait_new_window(ids_before, chosen, self.OPEN_APP_WAIT_S)
        if w is None:
            return f'launched {chosen}, but no new window appeared within {self.OPEN_APP_WAIT_S:.0f}s — it may still be starting (or failed to launch); try wait_for_window or focus_window'
        if already:
            _dep().log.info('open_app: %s was already open', chosen)
            return f'{chosen} is already open — window: {self._win_label(w)} (NOT focused — call focus_window to raise it)'
        note = f'launched {chosen} — window ready: {self._win_label(w)}'
        note += ' (focused)' if w.get('is_focused') else ' (NOT focused — call focus_window before typing)'
        self._mark_elements_stale()
        return note

    def _wait_new_window(self, ids_before: set, name: str, timeout: float) -> tuple[dict | None, bool]:
        """Wait for the launched app's window after spawn.

        Priority: (1) a NEW window matching `name`; (2) after a short grace
        period, any NEW window; (3) an EXISTING window matching `name` — the
        app was likely already running and mapped nothing. Returns
        (window, already_open); (None, False) when nothing appeared."""
        start = time.monotonic()
        deadline = start + timeout
        fallback: dict | None = None
        while True:
            try:
                wins = self._niri_windows()
            except RuntimeError:
                wins = []
            fresh = [w for w in wins if w.get('id') not in ids_before]
            for w in fresh:
                if name and self._win_matches(w, name):
                    return (w, False)
            if fallback is None and fresh:
                fallback = fresh[0]
            now = time.monotonic()
            if now - start >= self._WIN_GRACE_S:
                if fallback is not None:
                    return (fallback, False)
                for w in wins:
                    if name and self._win_matches(w, name):
                        return (w, True)
            if now >= deadline:
                return (None, False)
            time.sleep(self._WIN_POLL_S)

    @tool(description='Read a UTF-8 text file (any path, including your own source). May be truncated for very large files.')
    def read_file(self, path: str) -> str:
        """Read a text file.

        path: File path, ~ expanded.
        """
        p = Path(path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.exists():
            return f'ERROR: no such file: {p}'
        if p.is_dir():
            return 'ERROR: path is a directory, not a file'
        try:
            data = p.read_bytes()
        except OSError as e:
            return f'ERROR: cannot read {p}: {e}'
        if b'\x00' in data[:4096]:
            return f'ERROR: {p} looks like a binary file'
        text = data.decode('utf-8', errors='replace')
        if len(text) > self.MAX_READ:
            text = text[:self.MAX_READ] + f'\n...[truncated, file is larger than {self.MAX_READ} chars]'
        return text

    @tool(description=f"Replace a file's content. Allowed: own source + sibling split modules, files under {_dep().CONFIG_DIR}/. .bak backup; Python must compile, self-edits keep the marker.")
    def edit_file(self, path: str, content: str) -> str:
        """Overwrite a text file.

        path: File path, ~ expanded.
        content: The complete new file content.
        """
        p = Path(path).expanduser().resolve()
        if len(content) > self.MAX_WRITE:
            return 'REFUSED: content too large'
        kind = _dep()._classify_edit_path(p)
        if not kind:
            return f"REFUSED: you may only edit your own source ({_dep().SELF_PATH}), the split modules beside it or in {_dep().HOME / '.local/bin'} ({', '.join(sorted(_dep()._SPLIT_EDIT_FILES))}, core/*), or files inside {_dep().CONFIG_DIR}/"
        is_self = kind == 'self'
        if p in (_dep().SETTINGS_FILE, _dep().SETTINGS_FILE.with_suffix('.json')) or p.name.startswith('settings.json'):
            return 'REFUSED: settings.json controls your own permissions — the user manages it via the settings app'
        if is_self:
            if _dep().SELF_MARKER not in content:
                return f"REFUSED: self-edit must keep the marker line '{_dep().SELF_MARKER}'"
        if is_self or (kind == 'split' and p.suffix == '.py'):
            try:
                compile(content, str(p), 'exec')
            except SyntaxError as e:
                return f'REFUSED: new source does not compile: {e}'
            try:
                import py_compile as _py_compile
                with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8') as tf:
                    tf.write(content)
                    _tmp_self = tf.name
                try:
                    _py_compile.compile(_tmp_self, doraise=True)
                finally:
                    try:
                        os.unlink(_tmp_self)
                    except OSError:
                        pass
            except Exception as e:
                return f'REFUSED: new source fails py_compile: {e}'
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                shutil.copy2(p, str(p) + '.bak')
            _dep()._atomic_private_write(p, content)
        except OSError as e:
            return f'ERROR: cannot write {p}: {e}'
        _dep().log.info('edit_file wrote %d bytes to %s', len(content), _dep()._log_metadata(p, 'path'))
        note = f'wrote {len(content)} bytes to {p}'
        if is_self:
            note += f" — verify first: python -m py_compile {p} && python -m pytest tests/test_policy.py -q, then run_command '{_dep().RESTART_SCRIPT}' to restart into the new version"
        elif kind == 'split':
            note += f" — run_command '{_dep().RESTART_SCRIPT}' to restart into the new version"
        return note
