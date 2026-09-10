"""Doctor diagnostics: deployment + dependency snapshot.

This module is deliberately application-free. The host injects a
``DoctorDeps`` object whose attributes provide paths, probes, and
runtime state (Ollama endpoint, voices, control socket path, etc.).
No global is read from ``handsoff`` — the host wires the seam by
calling :func:`set_dependencies` or by passing ``deps`` to the
top-level helpers.

Public surface (kept stable across the monolith cut):
    * :func:`run_doctor` — human-readable diagnostic text
    * :func:`doctor_json` — machine-readable JSON for the control socket

The legacy fallback path (no ``hardware`` sibling module) is byte-
stable: ``--ptt doctor`` and the ``handsoff_doctor`` tool diff against
the strings this module emits, so the wording, the line order, and
the field values must not change.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger("handsoff.doctor")


# --------------------------------------------------------------- DI plumbing

_CURRENT: ContextVar["DoctorDeps | None"] = ContextVar(
    "handsoff_doctor_dependencies", default=None)


class DoctorDeps:
    """Container for every host-side value the doctor needs.

    Attribute names mirror the handsoff module-level names so a host
    can build this with a one-liner::

        DoctorDeps(
            ollama_base=H.OLLAMA_BASE,
            ollama_model=H.OLLAMA_MODEL,
            ollama_available=H.ollama_available,
            piper_voice=H._piper_voice,
            whisper_model=H._whisper_model,
            deployment_snapshot=H._deployment_snapshot,
            hardware_snapshot=getattr(H, "_doctor_snapshot", None),
            hardware_prompt_context=getattr(H, "_hardware_prompt_context", None),
            doctor_ttl=getattr(H, "_DOCTOR_TTL", {}),
            niri_msg=ToolBelt._niri_msg,
            ydotool_socket=ToolBelt._ydotool_socket,
            socket_connectable=ToolBelt._socket_connectable,
            sys_version_info=sys.version_info,
            restart_script=H.RESTART_SCRIPT,
            systemd_unit_file=H.SYSTEMD_UNIT_FILE,
            control_sock=H.CONTROL_SOCK,
            crash_log=H.CRASH_LOG,
            shutil=shutil,
            sounddevice=None,
        )

    All attributes are optional; missing ones fall back to the legacy
    safe-default behavior (niri probe unavailable, no ollama check,
    etc.) so a partial deps object still produces a coherent report.
    """

    __slots__ = (
        "ollama_base", "ollama_model", "ollama_available",
        "piper_voice", "whisper_model",
        "deployment_snapshot",
        "hardware_snapshot", "hardware_prompt_context", "doctor_ttl",
        "niri_msg", "ydotool_socket", "socket_connectable",
        "sys_version_info", "restart_script", "systemd_unit_file",
        "control_sock", "crash_log",
        "shutil", "sounddevice", "log",
    )

    def __init__(self, **kw: Any) -> None:
        # Defaults: everything a partial or absent host would set to
        # a sensible "no-data" / "raise" value. The doctor must still
        # run end-to-end with only paths supplied.
        self.ollama_base: str = ""
        self.ollama_model: str = ""
        self.ollama_available: Callable[[], bool] = lambda: False
        self.piper_voice: Any = None
        self.whisper_model: Any = None
        self.deployment_snapshot: Callable[[], dict] = lambda: {
            "status": "source-unknown", "running_path": "", "running_sha256": None,
            "repo_path": None, "manifest": {}}
        self.hardware_snapshot: Callable[[dict | None], dict | None] = lambda _ttl: None
        self.hardware_prompt_context: Callable[[], str] = lambda: ""
        self.doctor_ttl: dict = {}
        self.niri_msg: Callable[..., Any] = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("niri_msg not configured"))
        self.ydotool_socket: Callable[[], str] = lambda: ""
        self.socket_connectable: Callable[[str], bool] = lambda _p: False
        self.sys_version_info: tuple = sys.version_info
        self.restart_script: Path = Path("/nonexistent/handsoff-restart")
        self.systemd_unit_file: Path = Path("/nonexistent/handsoff.service")
        self.control_sock: Path = Path("/nonexistent/control.sock")
        self.crash_log: Path = Path("/nonexistent/crash.log")
        self.shutil = shutil
        self.sounddevice = None
        self.log = log
        for k, v in kw.items():
            setattr(self, k, v)


def _dep() -> DoctorDeps:
    d = _CURRENT.get()
    return d if d is not None else DoctorDeps()


def set_dependencies(deps: DoctorDeps) -> Any:
    """Install a deps object for the current context. Returns a token
    suitable for :func:`reset_dependencies`."""
    return _CURRENT.set(deps)


def reset_dependencies(token: Any) -> None:
    _CURRENT.reset(token)


# --------------------------------------------------------------- report

def _lines(deps: DoctorDeps) -> list[str]:
    lines: list[str] = []
    d = deps.deployment_snapshot()
    status = d.get("status", "source-unknown")
    deploy_note = {
        "in-sync": "installed copy matches the checkout",
        "installed-drift": (
            "INSTALLED COPY IS STALE — the running code is not the checkout. "
            "Re-run install.sh to deploy the tested source."),
        "source-unknown": "no checkout found (nothing to compare against)",
        "installed-missing": "no copy at ~/.local/bin/handsoff.py — run install.sh",
        "running-missing": "running source unreadable",
    }.get(status, status)
    lines.append(f"deployment: {status} — {deploy_note}")
    manifest = d.get("manifest") if isinstance(d.get("manifest"), dict) else {}
    revision = manifest.get("whisper_revision") or "unknown"
    digest = manifest.get("whisper_sha256") or "unknown"
    lines.append(f"whisper: revision {revision}; sha256 {digest}")
    python_exe = manifest.get("python") or sys.executable
    vi = deps.sys_version_info or sys.version_info
    python_version = ".".join(str(part) for part in vi[:3])
    lines.append(f"python: {python_exe} ({python_version})")
    lines.append(f"  running: {d.get('running_path', '')}")
    if d.get("running_sha256"):
        lines.append(f"  running sha256: {d['running_sha256'][:16]}…")
    if d.get("repo_path"):
        lines.append(f"  checkout: {d['repo_path']}")

    # ponytail: probe once via hardware.snapshot(); format the same strings
    # so --ptt doctor output stays byte-stable. Fresh cache: doctor must see
    # live state, never a TTL entry. Legacy probes below run only when the
    # sibling module is absent.
    snap = deps.hardware_snapshot({})
    if snap is not None:
        if snap["ollama"].get("ok"):
            lines.append(
                f"brain: Ollama reachable at {deps.ollama_base} "
                f"(model {deps.ollama_model})")
        else:
            lines.append(
                f"brain: OLLAMA UNREACHABLE at {deps.ollama_base} — "
                "`systemctl status ollama`, then `ollama pull "
                + deps.ollama_model + "`")

        lines.append(
            f"tts: {'voice loaded' if deps.piper_voice is not None else 'voice NOT loaded yet'}; "
            f"stt: {'whisper loaded' if deps.whisper_model is not None else 'whisper NOT loaded yet'}")

        audio = snap["audio"]
        if audio.get("ok") and audio.get("count"):
            lines.append(f"mic: {audio['count']} input device(s) visible")
        elif audio.get("ok"):
            lines.append("mic: NO input devices visible — check the mic is plugged in")
        else:
            lines.append(f"mic: audio subsystem error: {audio.get('error', 'unknown')}")

        comp = snap["compositor"]
        if comp.get("ok"):
            lines.append(f"niri IPC: ok ({comp.get('windows', 0)} window(s))")
        elif comp.get("error"):
            lines.append(f"niri IPC: UNAVAILABLE ({comp['error']}) — desktop actions will fail")
        else:
            lines.append("niri IPC: refused — desktop actions will fail")

        ydo = snap["ydotool"]
        if not ydo.get("installed", True):
            lines.append("ydotool: NOT INSTALLED (typing tools will fail)")
        elif ydo.get("reachable"):
            lines.append(f"ydotool: ok (daemon reachable at {ydo.get('socket')})")
        else:
            lines.append(
                f"ydotool: daemon UNREACHABLE (no socket at {ydo.get('socket')}) — "
                "start it: systemctl --user enable --now ydotool.service")
    else:
        if deps.ollama_available():
            lines.append(
                f"brain: Ollama reachable at {deps.ollama_base} "
                f"(model {deps.ollama_model})")
        else:
            lines.append(
                f"brain: OLLAMA UNREACHABLE at {deps.ollama_base} — "
                "`systemctl status ollama`, then `ollama pull "
                + deps.ollama_model + "`")

        lines.append(
            f"tts: {'voice loaded' if deps.piper_voice is not None else 'voice NOT loaded yet'}; "
            f"stt: {'whisper loaded' if deps.whisper_model is not None else 'whisper NOT loaded yet'}")

        sd = deps.sounddevice
        if sd is not None:
            try:
                devs = [dd for dd in sd.query_devices()
                        if dd.get("max_input_channels", 0) > 0]
                if devs:
                    lines.append(f"mic: {len(devs)} input device(s) visible")
                else:
                    lines.append("mic: NO input devices visible — check the mic is plugged in")
            except Exception as e:
                lines.append(f"mic: audio subsystem error: {e}")
        else:
            try:
                import sounddevice as _sd  # type: ignore
                devs = [dd for dd in _sd.query_devices()
                        if dd.get("max_input_channels", 0) > 0]
                if devs:
                    lines.append(f"mic: {len(devs)} input device(s) visible")
                else:
                    lines.append("mic: NO input devices visible — check the mic is plugged in")
            except Exception as e:
                lines.append(f"mic: audio subsystem error: {e}")

        try:
            r = deps.niri_msg("msg", "--json", "windows")
            if getattr(r, "returncode", 1) == 0:
                n = len(json.loads(r.stdout or "[]"))
                lines.append(f"niri IPC: ok ({n} window(s))")
            else:
                lines.append("niri IPC: refused — desktop actions will fail")
        except Exception as e:
            lines.append(f"niri IPC: UNAVAILABLE ({e}) — desktop actions will fail")

        if deps.shutil.which("ydotool"):
            sock = deps.ydotool_socket()
            if deps.socket_connectable(sock):
                lines.append(f"ydotool: ok (daemon reachable at {sock})")
            else:
                lines.append(
                    f"ydotool: daemon UNREACHABLE (no socket at {sock}) — "
                    "start it: systemctl --user enable --now ydotool.service")
        else:
            lines.append("ydotool: NOT INSTALLED (typing tools will fail)")

    if deps.restart_script.exists():
        lines.append(f"restart script: present at {deps.restart_script}")
    else:
        lines.append(
            f"restart script: MISSING at {deps.restart_script} — run install.sh")

    unit = deps.systemd_unit_file
    if unit.exists():
        txt = ""
        try:
            txt = unit.read_text(encoding="utf-8")
        except OSError:
            pass
        if "Restart=always" in txt or "Restart=on-failure" in txt:
            lines.append("systemd unit: present, auto-restart configured")
        else:
            lines.append("systemd unit: present but has NO Restart= — crashes stay dead")
    else:
        lines.append("systemd unit: not installed (autostart falls back to niri spawn)")

    if deps.crash_log.exists():
        try:
            age = time.time() - deps.crash_log.stat().st_mtime
            lines.append(f"crash log: exists, last modified {age / 3600:.1f}h ago")
        except OSError:
            lines.append("crash log: exists (age unknown)")
    else:
        lines.append("crash log: none (no native crashes recorded)")

    # can the bubble even bind its control socket? a symlinked/permissive
    # path fails _prepare_runtime at startup and the failure was silent
    try:
        info = deps.control_sock.lstat()
        if stat.S_ISLNK(info.st_mode):
            lines.append("control socket: REFUSES STARTUP — path is a symlink")
        elif info.st_uid != os.getuid():
            lines.append("control socket: REFUSES STARTUP — not owned by you")
        elif not stat.S_ISSOCK(info.st_mode):
            lines.append("control socket: REFUSES STARTUP — path is not a socket")
        else:
            lines.append("control socket: ok")
    except FileNotFoundError:
        lines.append("control socket: not created yet (bubble not running?)")
    except OSError as e:
        lines.append(f"control socket: lstat failed ({e})")
    return lines


def run_doctor() -> str:
    """One human-readable diagnostic pass over everything the bubble needs.

    Read-only, side-effect-free (the Ollama probe is a 2 s GET). Served via
    ``--ptt doctor`` and the ``handsoff_doctor`` tool; the AI reads it to
    fix itself instead of guessing.
    """
    return "\n".join(_lines(_dep()))


def doctor_json() -> dict:
    """Machine-readable doctor output for the control socket."""
    deps = _dep()
    d = deps.deployment_snapshot()
    out: dict = {"deployment": d}
    if deps.crash_log.exists():
        try:
            out["crash_log_age_hours"] = round(
                (time.time() - deps.crash_log.stat().st_mtime) / 3600.0, 2)
        except OSError:
            pass
    out["restart_script"] = deps.restart_script.exists()
    out["systemd_unit"] = {
        "present": deps.systemd_unit_file.exists(),
        "auto_restart": False,
    }
    if deps.systemd_unit_file.exists():
        try:
            txt = deps.systemd_unit_file.read_text(encoding="utf-8")
            out["systemd_unit"]["auto_restart"] = bool(
                re.search(r"^Restart=(always|on-failure|on-abnormal)$", txt, re.M))
        except OSError:
            pass
    snap = deps.hardware_snapshot(deps.doctor_ttl)
    if snap is not None:
        out["hardware"] = snap
        out["prompt_context"] = deps.hardware_prompt_context()
    return out
