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
            tts_model=H._tts_model,
            tts_engine=H.TTS_ENGINE,
            tts_reference=H.TTS_REFERENCE,
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
        "tts_model", "tts_engine", "tts_reference", "whisper_model",
        "deployment_snapshot",
        "hardware_snapshot", "hardware_prompt_context", "doctor_ttl",
        "niri_msg", "ydotool_socket", "socket_connectable",
        "sys_version_info", "restart_script", "systemd_unit_file",
        "control_sock", "crash_log", "remote_ollama_allowed",
        "remote_ollama_optin_source",
        "cap_refusal_note", "cap_refusals",
        "appearance_look", "web_lines", "stop_attribution_health",
        "unexplained_stops", "boot_stop_audit",
        "gpu_lines", "gpu_headroom",
        "shutil", "sounddevice", "log",
    )

    def __init__(self, **kw: Any) -> None:
        # Defaults: everything a partial or absent host would set to
        # a sensible "no-data" / "raise" value. The doctor must still
        # run end-to-end with only paths supplied.
        self.ollama_base: str = ""
        self.ollama_model: str = ""
        self.ollama_available: Callable[[], bool] = lambda: False
        self.tts_model: Any = None
        self.tts_engine: str = ""
        self.tts_reference: str = ""
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
        self.remote_ollama_allowed: Callable[[], bool] | None = None
        self.remote_ollama_optin_source: Callable[[], str] | None = None
        self.stop_attribution_health: Callable[[], dict] | None = None
        # Stops the tripwire shipped too late to see: listed until an
        # autopsy explains one or the ledger supersedes the whole item.
        self.unexplained_stops: Callable[[], dict] | None = None
        # The boot-time stop-audit's verdict (the host runs the audit once at
        # startup; None means the host never adjudicated, which renders as
        # silence rather than a false "passed").
        self.boot_stop_audit: Callable[[], dict] | None = None
        # Cap refusals: how often a bounded registry has turned real work away.
        # A host that does not record them reports none, which is also the
        # truthful answer for a host that has no registries to bound.
        self.cap_refusal_note: Callable[[], str] = lambda: ""
        self.cap_refusals: Callable[[], dict] = lambda: {}
        # The Appearance look the settings spell out. A host without looks
        # reports "", and the line is then omitted entirely, so this cannot
        # change the output of a partial deps object.
        self.appearance_look: Callable[[], str] = lambda: ""
        # What the host has OBSERVED online: one line per capability, or none at
        # all for a host that does not look anything up. Deliberately a callable
        # returning LINES rather than a status the doctor formats itself — the
        # host owns the vocabulary (which backends exist, which reader was used)
        # and the doctor owns the placement, so the two cannot disagree.
        self.web_lines: Callable[[], list] = lambda: []
        # The card's WHOLE story: every tenant on it, what this bubble's speech
        # models hold, what the LLM holds, and what the next turn would ask for.
        # Lines for the same reason as the two above (the host owns the
        # vocabulary), plus the structured form the JSON surface needs — one
        # dict behind both, so the words and the numbers cannot drift. There is
        # deliberately no second `llm_lines` dep: the LLM's memory was reported
        # by a line here AND counted by a line there, which is two descriptions
        # of one card.
        self.gpu_lines: Callable[[], list] = lambda: []
        self.gpu_headroom: Callable[[], dict] = lambda: {}
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


def _is_remote_base(base: str) -> bool:
    """True when the Ollama base URL is empty-host or non-loopback.

    Mirrors handsoff's ``_ollama_remote`` classification so the doctor can
    flag a remote brain even when the bubble is dead (module import avoided:
    keep this dependency-free rather than importing the app).
    """
    import ipaddress
    import urllib.parse
    try:
        host = urllib.parse.urlparse(str(base or "")).hostname
        if not host:
            return True
        if host.lower() == "localhost":
            return False
        return not ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return True


def _tts_line(deps: "DoctorDeps") -> str:
    """One line naming the speech engine, its voice, and whether it is up.

    "voice loaded" used to be the whole story. That cannot distinguish the
    built-in voice from a reference clip, and names no engine — so a bubble
    that failed to condition on its clip, or that is still running the previous
    engine, read exactly like a healthy one. Doctor is the tool the user runs
    when speech is wrong, so it has to answer "which engine, which voice".
    """
    engine = deps.tts_engine or "tts"
    voice = (f"reference {Path(deps.tts_reference).name}" if deps.tts_reference
             else "built-in voice")
    state = "model loaded" if deps.tts_model is not None else "model NOT loaded yet"
    return f"tts: {engine} ({voice}) — {state}"


def _appearance_lines(deps: "DoctorDeps") -> list[str]:
    """The Appearance look line, when the host has looks to report.

    A host that does not supply the dep yields no line at all, so this cannot
    change the output of a partial deps object (the legacy path stays as it
    was). The name is derived from the settings, never stored beside them, so
    the line cannot disagree with what the bubble is actually drawing.
    """
    try:
        note = deps.appearance_look() or ""
    except Exception:
        note = ""
    return [f"appearance: {note}"] if note else []


def _web_lookup_lines(deps: "DoctorDeps") -> list[str]:
    """The search and reader lines, when the host looks things up online.

    Empty for a host without them, so a partial deps object prints exactly what
    it printed before. The lines are produced by the host (see `core/web.py`):
    they name what has actually been OBSERVED, including `untried` for a
    backend nothing has asked yet — a doctor line that says `ok` because a
    backend is configured is the false-positive this project keeps removing.
    """
    try:
        lines = deps.web_lines() or []
    except Exception:
        lines = []
    return [str(line) for line in lines if line]


def _gpu_story_lines(deps: "DoctorDeps") -> list[str]:
    """The card's story, when the host has one to tell.

    Empty for a host without the dep, exactly like the appearance and web
    lines, so a partial deps object prints what it printed before. The content
    is the host's: it is the process that knows what it is holding, and it is
    the SAME collector `gpu_headroom` returns — the section's sentences and the
    JSON's numbers are one reading, not two that agree.
    """
    try:
        lines = deps.gpu_lines() or []
    except Exception:
        lines = []
    return [str(line) for line in lines if line]


def _remote_brain_lines(deps: "DoctorDeps") -> list[str]:
    """Trust warning appended after the brain line when the endpoint is not
    on this machine: conversation history, screenshots, and tool schemas
    leave the device, and the guard fails closed until explicitly allowed."""
    if not _is_remote_base(deps.ollama_base):
        return []
    opted = False
    getter = getattr(deps, "remote_ollama_allowed", None)
    if getter is not None:
        try:
            opted = bool(getter())
        except Exception:
            opted = False
    if opted:
        # Name the channel. The env var is a SECOND opt-in that Settings
        # cannot show, so a user must be able to learn from doctor that their
        # brain is remote because of the environment, not the checkbox.
        source = ""
        src_getter = getattr(deps, "remote_ollama_optin_source", None)
        if src_getter is not None:
            try:
                source = str(src_getter() or "")
            except Exception:
                source = ""
        where = {
            "settings": "allow_remote_ollama in Settings",
            "env": "HANDSOFF_ALLOW_REMOTE_OLLAMA in the environment "
                   "— NOT visible in Settings",
        }.get(source, "allow_remote_ollama")
        return [f"brain privacy: REMOTE — explicitly allowed ({where})"]
    return [
        "brain privacy: REMOTE and NOT allowed — history, screenshots, and "
        "schemas leave this machine; every request FAILS CLOSED until "
        "allow_remote_ollama (Settings) or HANDSOFF_ALLOW_REMOTE_OLLAMA=1 "
        "opts in",
    ]


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
        # `.get(...) or {}` rather than `snap[...]`: the snapshot comes from the
        # host, and a section it did not fill must read as "no reading" in a
        # DIAGNOSTIC, never as a KeyError that takes down the whole doctor —
        # which is the tool you reach for precisely when something is wrong.
        if (snap.get("ollama") or {}).get("ok"):
            lines.append(
                f"brain: Ollama reachable at {deps.ollama_base} "
                f"(model {deps.ollama_model})")
        else:
            lines.append(
                f"brain: OLLAMA UNREACHABLE at {deps.ollama_base} — "
                "`systemctl status ollama`, then `ollama pull "
                + deps.ollama_model + "`")
        lines.extend(_remote_brain_lines(deps))

        lines.append(
            f"{_tts_line(deps)}; "
            f"stt: {'whisper loaded' if deps.whisper_model is not None else 'whisper NOT loaded yet'}")
        lines.extend(_gpu_story_lines(deps))
        lines.extend(_appearance_lines(deps))
        lines.extend(_web_lookup_lines(deps))

        audio = snap.get("audio") or {}
        if audio.get("ok") and audio.get("count"):
            lines.append(f"mic: {audio['count']} input device(s) visible")
        elif audio.get("ok"):
            lines.append("mic: NO input devices visible — check the mic is plugged in")
        else:
            lines.append(f"mic: audio subsystem error: {audio.get('error', 'unknown')}")

        comp = snap.get("compositor") or {}
        if comp.get("ok"):
            lines.append(f"niri IPC: ok ({comp.get('windows', 0)} window(s))")
        elif comp.get("error"):
            lines.append(f"niri IPC: UNAVAILABLE ({comp['error']}) — desktop actions will fail")
        else:
            lines.append("niri IPC: refused — desktop actions will fail")

        ydo = snap.get("ydotool") or {}
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
        lines.extend(_remote_brain_lines(deps))

        lines.append(
            f"{_tts_line(deps)}; "
            f"stt: {'whisper loaded' if deps.whisper_model is not None else 'whisper NOT loaded yet'}")
        lines.extend(_gpu_story_lines(deps))
        lines.extend(_appearance_lines(deps))
        lines.extend(_web_lookup_lines(deps))

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
            try:
                reachable = deps.socket_connectable(sock)
            except Exception as e:
                # A probe that raises is a LINE, never a dead doctor: this is
                # the tool somebody runs precisely when something is wrong,
                # and the niri probe beside it was already held to this rule.
                lines.append(f"ydotool: probe failed ({e})")
            else:
                if reachable:
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

    # Stop attribution: the unit's ExecStop probe records WHO stops this unit.
    # A stop with a named caller is the one evidence the restart killer leaves,
    # so doctor reports the last one here — reported even when empty, so "none"
    # is a finding (no stop observed / probe not shipped) rather than silence.
    sa = getattr(deps, "stop_attribution_health", None)
    if sa is not None:
        try:
            h = sa() or {}
        except Exception:
            h = {"present": False}
        last = h.get("last")
        if last:
            callers = last.get("callers") or []
            if callers:
                c = callers[0]
                chain = " <- ".join(
                    (hc.get("exe") or "?") for hc in (c.get("chain") or [])[:3])
                when = str(last.get("ts") or "")[:19]
                lines.append(
                    f"stop attribution: last stop {when} by "
                    f"{c.get('exe') or '?'} ({chain})"
                    + (f" +{len(callers) - 1} more caller(s)" if len(callers) > 1 else ""))
            else:
                if last.get("shutdown"):
                    lines.append(
                        f"stop attribution: last stop {str(last.get('ts') or '')[:19]} "
                        "ran inside the session-shutdown sweep (exit.target) — "
                        "expected, not an anomaly")
                else:
                    lines.append(
                        f"stop attribution: last stop {str(last.get('ts') or '')[:19]} "
                        "had NO visible caller (session shutdown or direct D-Bus call)")
        elif h.get("present"):
            lines.append("stop attribution: ledger present, no stop recorded yet")
        else:
            lines.append(
                "stop attribution: no ledger — probe not shipped or no stop "
                "since it landed (install.sh wires it as ExecStop=)")
        # The pattern is a finding in its own right — even when the LATEST stop
        # was attributed, because a killer that alternates attributed and
        # invisible stops must not slip out between two clean-looking lines.
        ghost = h.get("ghost_pattern") or {}
        if isinstance(ghost, dict) and ghost.get("seen"):
            pairs = int(ghost.get("pairs") or 0)
            lines.append(
                f"stop attribution: PATTERN — {pairs} invisible stop(s) followed "
                f"an attributed one within "
                f"{int(ghost.get('window_min') or 0)} min (last "
                f"{str(ghost.get('last_ghost_ts') or '')[:19]}) — the "
                "invisible-killer shape, not a session shutdown")

    # The stops the tripwire shipped too late to see. An open item with no
    # home is how a mystery quietly disappears; this line keeps them visible
    # until an autopsy explains an entry or a later attributed catch
    # supersedes the list.
    us = getattr(deps, "unexplained_stops", None)
    if us is not None:
        try:
            u = us() or {}
        except Exception:
            u = {}
        open_items = u.get("open") or []
        if open_items:
            newest = max((str(i.get("ts"))[:16] for i in open_items
                          if i.get("ts")), default="?")
            lines.append(
                f"stop attribution: {len(open_items)} stop(s) remain "
                f"UNEXPLAINED (newest {newest}) — predates the tripwire; "
                "listed until explained or superseded")
        elif u.get("superseded_by"):
            lines.append(
                f"stop attribution: no unexplained stops — superseded by the "
                f"attributed catch at {str(u.get('superseded_by'))[:19]}")

    # The verdict the running process computed for the stop that PRECEDED its
    # own boot. Empty dict = this host never adjudicated (the audit is wired
    # at startup; older hosts and partial deps render silence, not a claim).
    ba = getattr(deps, "boot_stop_audit", None)
    if ba is not None:
        try:
            b = ba() or {}
        except Exception:
            b = {}
        if b:
            lines.append(
                f"stop attribution: boot audit {b.get('verdict', '?')} at "
                f"{str(b.get('ts'))[:19]} ({b.get('summary', '')})")

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

    # Did a cap ever turn real work away? That refusal is the one in-the-wild
    # signal that the bubble met its own limits, and it used to live only in the
    # model's reply — invisible to anyone reading the journal or the state, and
    # the overshoot this module's sibling bug was ABOUT was found by reading the
    # source instead. Reported last, and reported even when there is nothing to
    # report, so "none" is a positive finding rather than a missing line.
    note = ""
    cap_note = getattr(deps, "cap_refusal_note", None)
    if cap_note is not None:
        try:
            note = str(cap_note() or "")
        except Exception:
            note = ""
    lines.append(note or
                 "cap refusals: none recorded — no cap has turned work away")
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
    try:
        refusals = deps.cap_refusals() or {}
    except Exception:
        refusals = {}
    if not isinstance(refusals, dict):
        refusals = {}
    out["cap_refusals"] = {
        "count": refusals.get("count", 0),
        "by_registry": refusals.get("by_registry", {}),
        "last": refusals.get("last"),
    }
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
    try:
        sa = deps.stop_attribution_health() or {}
    except Exception:
        sa = {}
    out["stop_attribution"] = {
        "present": bool(sa.get("present")),
        "total": int(sa.get("total") or 0),
        "last": sa.get("last"),
    }
    if sa.get("present"):
        # Mirrors cap_refusals: when the ledger exists the pattern is ALWAYS
        # reported, and `unexplained` is itself a finding ("no pattern" must
        # be distinguishable from "never checked").
        ghost = sa.get("ghost_pattern")
        if isinstance(ghost, dict):
            out["stop_attribution"]["ghost_pattern"] = dict(ghost)
    us_dep = getattr(deps, "unexplained_stops", None)
    try:
        us = (us_dep() if us_dep is not None else {}) or {}
    except Exception:
        us = {}
    out["unexplained_stops"] = {
        "open": list(us.get("open") or []),
        "superseded_by": us.get("superseded_by") or "",
    }
    ba_dep = getattr(deps, "boot_stop_audit", None)
    try:
        b = (ba_dep() if ba_dep is not None else {}) or {}
    except Exception:
        b = {}
    if b:
        out["boot_stop_audit"] = {
            "verdict": b.get("verdict"), "ts": b.get("ts"),
            "summary": b.get("summary"),
        }
    # Only when the host measured or estimated something: a host without the
    # dep leaves the JSON exactly as it was, the same way its text report is
    # unchanged. The dict is passed through as the host built it — this module
    # formats, it does not adjudicate.
    try:
        headroom = deps.gpu_headroom() or {}
    except Exception:
        headroom = {}
    if isinstance(headroom, dict) and headroom:
        out["gpu_headroom"] = dict(headroom)
    snap = deps.hardware_snapshot(deps.doctor_ttl)
    if snap is not None:
        out["hardware"] = snap
        out["prompt_context"] = deps.hardware_prompt_context()
    return out
