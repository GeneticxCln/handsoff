#!/usr/bin/env python
"""Hardware snapshot for the handsoff doctor (stdlib + pathlib only).

`snapshot()` returns a plain JSON dict for every section; slow sections are
cached in a caller-owned TTL dict. Never raises, never imports SETTINGS
(config arrives via `ctx`), no Qt/audio imports — `--preflight` is safe
on a bare checkout.
"""
from __future__ import annotations

import json
import os
import shutil
import socket as _socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Slow-section TTLs. Cheap-syscall sections (cpu/ram/display/mounts/models/
# stt_tts) are TTL 0: always fresh, zero subprocess cost.
TTL = {"audio": 10.0, "gpu": 15.0, "systemd": 30.0,
       "compositor": 30.0, "ydotool": 30.0, "ollama": 60.0, "fastfetch": 60.0}

SECTIONS = ("cpu", "ram", "gpu", "audio", "display", "mounts",
            "ollama", "models", "stt_tts", "systemd", "compositor",
            "ydotool", "fastfetch")


def disk_free(path: str | Path = "/") -> dict:
    """Free-space probe for the tick fast path (stdlib only, no subprocess)."""
    try:
        u = shutil.disk_usage(str(path or "/"))
        return {"ok": True, "total": u.total, "free": u.free}
    except OSError as e:
        return _deg(e)

def _deg(error: Exception | str) -> dict:
    """Failure as data, never an exception."""
    return {"ok": False, "error": str(error)[:200]}

def _default_probers() -> dict:
    """Slow ops (with timeouts); every one is injectable for tests."""
    def query_devices():
        import sounddevice as sd  # lazy: audio stack stays optional
        return sd.query_devices()

    def ollama_tags(base: str) -> str:
        with urllib.request.urlopen(base.rstrip("/") + "/api/tags",
                                    timeout=2.5) as r:
            return r.read(1_000_000).decode("utf-8")

    def cmd(*argv: str, timeout: float):
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "refused").strip()[:160])
        return p.stdout

    return {
        "query_devices": query_devices,
        "ollama_tags": ollama_tags,
        "nvidia_smi": lambda: cmd("nvidia-smi", "-L", timeout=2),
        "niri_windows": lambda: subprocess.run(
            ["niri", "msg", "--json", "windows"], capture_output=True,
            text=True, timeout=3),
        "ydotool_which": lambda: shutil.which("ydotool"),
        "socket_connectable": _sock_ok,
        "fastfetch_which": lambda: shutil.which("fastfetch"),
        "fastfetch_json": lambda: subprocess.run(
            ["fastfetch", "-j"], capture_output=True, text=True,
            timeout=3).stdout,
    }

_LEGACY_YDOTOOL_SOCKET = "/tmp/.ydotool_socket"


def _ydotool_candidates() -> list:
    """Ordered socket candidates, ToolBelt order: runtime first, legacy second."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return ([os.path.join(runtime, ".ydotool_socket")] if runtime else []) \
        + [_LEGACY_YDOTOOL_SOCKET]


def _sock_ok(sock: str) -> bool:
    """True if `sock` is a socket file that accepts a connection.

    Mirrors ToolBelt._socket_connectable exactly: ydotoold 1.x binds
    SOCK_DGRAM, so DGRAM is tried first — a stream-only probe fails with
    EPROTOTYPE even when the daemon answers.
    """
    try:
        if not stat.S_ISSOCK(os.stat(sock).st_mode):
            return False
    except OSError:
        return False
    for sock_type in (_socket.SOCK_DGRAM, _socket.SOCK_STREAM):
        s = _socket.socket(_socket.AF_UNIX, sock_type)
        try:
            s.settimeout(1.0)
            s.connect(sock)
            return True
        except OSError:
            continue
        finally:
            s.close()
    return False

def _probe(section: str, fn, ttl_cache: dict | None, force: bool):
    """TTL-lite: cached slow sections skip the prober entirely."""
    at = (ttl_cache or {}).get("at", {})
    data = (ttl_cache or {}).get("data", {})
    if (ttl_cache is not None and not force and section in data
            and time.monotonic() - at.get(section, 0.0) < TTL[section]):
        return data[section], True
    try:
        probed = fn()
    except Exception as e:  # ponytail: failure is data, not an exception
        prev = (ttl_cache or {}).get("data", {}).get(section)
        if isinstance(prev, dict) and prev.get("ok"):
            probed = {**prev, "degraded": True, "error": str(e)[:200]}
        else:
            probed = _deg(e)
    if ttl_cache is not None:
        ttl_cache.setdefault("at", {})[section] = time.monotonic()
        ttl_cache.setdefault("data", {})[section] = probed
    return probed, False

def snapshot(ctx: dict | None = None, probers: dict | None = None,
             ttl_cache: dict | None = None, *, force: bool = False) -> dict:
    """Full snapshot. Never raises; all 13 keys always present."""
    ctx = dict(ctx or {})
    probers = {**_default_probers(), **(probers or {})}
    out: dict = {"ts": time.time()}
    try:
        out["cpu"] = _cpu()
        out["ram"] = _ram()
        out["gpu"], _ = _probe("gpu", lambda: _gpu(probers), ttl_cache, force)
        out["audio"], _ = _probe(
            "audio", lambda: _audio(ctx, probers), ttl_cache, force)
        out["display"] = _display(ctx)
        out["mounts"] = _mounts(ctx)
        out["ollama"], _ = _probe(
            "ollama", lambda: _ollama(ctx, probers), ttl_cache, force)
        out["models"] = {"ok": True,
                         "ollama_model": str(ctx.get("ollama_model", ""))}
        out["stt_tts"] = _stt_tts(ctx)
        out["systemd"], _ = _probe(
            "systemd", lambda: _systemd(ctx), ttl_cache, force)
        out["compositor"], _ = _probe(
            "compositor", lambda: _compositor(probers), ttl_cache, force)
        out["ydotool"], _ = _probe(
            "ydotool", lambda: _ydotool(probers), ttl_cache, force)
        out["fastfetch"], _ = _probe(
            "fastfetch", lambda: _fastfetch(probers), ttl_cache, force)
    except Exception as e:  # last resort: minimal dict, still no raise
        out.setdefault("error", str(e)[:200])
        for section in SECTIONS:
            out.setdefault(section, {"ok": False, "error": "aborted"})
    return out

def _cpu() -> dict:
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    return {"ok": True, "count": os.cpu_count() or 0, "load1": load}

def _ram() -> dict:
    try:
        mem = {k.strip(): int(v.strip().split()[0]) // 1024
               for line in Path("/proc/meminfo").read_text().splitlines()
               for k, _, v in [line.partition(":")]
               if v.strip()[:1].isdigit()}
        return {"ok": True, "total_mb": mem.get("MemTotal", 0),
                "avail_mb": mem.get("MemAvailable", 0)}
    except (OSError, ValueError) as e:
        return _deg(e)

def _gpu(probers: dict) -> dict:
    try:
        text = probers["nvidia_smi"]()
    except FileNotFoundError:
        return {"ok": True, "present": False}  # healthy absent, not an error
    except Exception as e:
        return _deg(e)
    line = (text or "").strip().splitlines()
    return {"ok": True, "present": True,
            "name": line[0].split(" (UUID")[0] if line else "nvidia"}

def _audio(ctx: dict, probers: dict) -> dict:
    try:
        devs = [d for d in (probers["query_devices"]() or [])
                if isinstance(d, dict) and d.get("max_input_channels", 0) > 0]
    except Exception as e:
        return _deg(e)
    names = [str(d.get("name", "?")) for d in devs]
    if not names:
        return {"ok": True, "count": 0, "inputs": [],
                "note": "no input devices visible"}
    want = str(ctx.get("mic_device", "")).strip().lower()
    return {"ok": True, "count": len(names), "inputs": names, "default": next(
        (n for n in names if want and want in n.lower()), names[0])}

def _display(ctx: dict) -> dict:
    return {"ok": True,
            "wayland_display": str(ctx.get("wayland_display")
                                   or os.environ.get("WAYLAND_DISPLAY", "")),
            "display": str(ctx.get("display")
                           or os.environ.get("DISPLAY", ""))}

def _mounts(ctx: dict) -> dict:
    sock, state = str(ctx.get("control_sock") or ""), str(ctx.get("state_dir") or "")
    if sock and not Path(sock).exists():
        return {"ok": False, "sock_present": False, "sock_path": sock,
                "note": "control socket not created yet"}
    try:
        writable = os.access(state or ".", os.W_OK) if state else True
    except OSError:
        writable = False
    return {"ok": True, "sock_present": bool(sock), "sock_path": sock,
            "state_writable": writable}

def _ollama(ctx: dict, probers: dict) -> dict:
    try:
        data = json.loads(probers["ollama_tags"](
            str(ctx.get("ollama_base") or "http://127.0.0.1:11434")) or "{}")
    except Exception as e:
        return _deg(e)
    return {"ok": True, "models": [str(m.get("name", ""))
                                   for m in data.get("models", [])
                                   if isinstance(m, dict)]}

def _stt_tts(ctx: dict) -> dict:
    try:
        vdir = Path(str(ctx.get("piper_voice_dir") or ""))
        wdir = Path(str(ctx.get("whisper_model_dir") or ""))
        onnx = sorted(p.name for p in vdir.glob("*.onnx")) if vdir.is_dir() else []
    except OSError as e:
        return _deg(e)
    cached = any(wdir.iterdir()) if wdir.is_dir() else False
    if not onnx:
        return {"ok": False, "whisper_cached": cached, "onnx": [],
                "note": "no piper voices installed"}
    ok_map = {n: (vdir / (n + ".json")).exists() for n in onnx}
    if not all(ok_map.values()):
        return {"ok": False, "whisper_cached": cached, "onnx": onnx,
                "onnx_json_ok": ok_map, "note": "voice missing .json config"}
    return {"ok": True, "whisper_size": str(ctx.get("whisper_size", "")),
            "whisper_cached": cached, "onnx": onnx, "onnx_json_ok": ok_map}

def _systemd(ctx: dict) -> dict:
    unit = Path(str(ctx.get("systemd_unit_file") or ""))
    if not unit.exists():
        return {"ok": True, "present": False, "auto_restart": False}
    try:
        txt = unit.read_text(encoding="utf-8")
    except OSError as e:
        return _deg(e)
    import re as _re
    return {"ok": True, "present": True, "auto_restart": bool(
        _re.search(r"^Restart=(always|on-failure|on-abnormal)$", txt, _re.M))}

def _compositor(probers: dict) -> dict:
    try:
        r = probers["niri_windows"]()
    except Exception as e:
        return _deg(e)
    try:
        if r.returncode != 0:
            return {"ok": False, "refused": True, "note": "niri refused"}
        return {"ok": True, "windows": len(json.loads(r.stdout or "[]"))}
    except Exception as e:
        return _deg(e)

def _ydotool(probers: dict) -> dict:
    """Resolve the first connectable candidate; report its path + reachability."""
    try:
        if not probers["ydotool_which"]():
            return {"ok": True, "installed": False}
        check = probers.get("socket_connectable") or _sock_ok
        paths = _ydotool_candidates()
        for path in paths:
            try:
                if check(path):
                    return {"ok": True, "installed": True,
                            "socket": path, "reachable": True}
            except Exception:
                continue
        return {"ok": True, "installed": True,
                "socket": paths[0], "reachable": False}
    except Exception as e:
        return _deg(e)

def _gib(n) -> int:
    """Bytes → GiB, truncating; non-numeric → 0 (exact input, no guessing)."""
    try:
        return int(float(n)) // (1024 ** 3)
    except (TypeError, ValueError):
        return 0


def _fastfetch(probers: dict) -> dict:
    """Optional fastfetch section: OS/host/cpu/gpu/mem/display/disk facts.

    Missing binary → degraded (no subprocess attempted). Entries whose
    result is null (Separator/Break/Colors/WMTheme/TerminalFont/DE) are
    skipped; Shell is never trusted (it reflects the invoker, not the user).
    Prober failures propagate to _probe (last-good/degraded handling).
    """
    if not probers["fastfetch_which"]():
        return {"ok": False, "installed": False,
                "note": "fastfetch not installed"}
    raw = probers["fastfetch_json"]()
    try:
        mods = json.loads(raw or "[]")
        by_type = {m.get("type"): m.get("result") for m in mods
                   if isinstance(m, dict) and m.get("result") is not None}
    except Exception as e:
        return _deg(e)
    strang = lambda v: str(v or "")
    first = lambda v: (v or [{}])[0] if isinstance(v, list) else (v or {})
    os_r, host, cpu = first(by_type.get("OS")), first(by_type.get("Host")), \
        first(by_type.get("CPU"))
    gpus = [strang(g.get("name")) for g in (by_type.get("GPU") or [])
            if isinstance(g, dict) and g.get("name")]
    mem, disk = first(by_type.get("Memory")), {}
    for d in (by_type.get("Disk") or []):
        if isinstance(d, dict) and d.get("mountpoint") == "/":
            disk = d
            break
    disps = [{"name": strang(d.get("name")),
              "width": (d.get("preferred") or {}).get("width", 0),
              "height": (d.get("preferred") or {}).get("height", 0)}
             for d in (by_type.get("Display") or []) if isinstance(d, dict)]
    wm = first(by_type.get("WM"))
    pkgs = first(by_type.get("Packages"))
    up = first(by_type.get("Uptime"))
    b = ((disk.get("bytes") or {}) if isinstance(disk.get("bytes"), dict)
         else {})
    return {
        "ok": True,
        "os": strang(os_r.get("prettyName") or os_r.get("name")),
        "os_id": strang(os_r.get("id")),
        "host": f"{strang(host.get('vendor'))} {strang(host.get('name'))}".strip(),
        "kernel": strang(first(by_type.get("Kernel")).get("release")),
        "cpu": strang(cpu.get("cpu")),
        "cpu_logical": ((cpu.get("cores") or {}).get("logical", 0)
                        if isinstance(cpu.get("cores"), dict) else 0),
        "gpu": gpus,
        "memory_total": mem.get("total", 0) if isinstance(mem.get("total"), int) else 0,
        "displays": disps,
        "wm": strang(wm.get("prettyName") or wm.get("processName")),
        "wm_protocol": strang(wm.get("protocolName")),
        "disk_total": b.get("total", 0), "disk_free": b.get("free", 0),
        "packages": pkgs.get("pacman", pkgs.get("all", 0)),
        "uptime_s": up.get("uptime", 0) if isinstance(up.get("uptime"), int) else 0,
        "locale": by_type.get("Locale") if isinstance(by_type.get("Locale"), str) else "",
    }


def prompt_context(snap: dict, max_chars: int = 600) -> str:
    """At most 5 lines (char-capped): desktop, mic, brain, voices, sys-facts."""
    comp, audio = snap.get("compositor") or {}, snap.get("audio") or {}
    ollama, stt = snap.get("ollama") or {}, snap.get("stt_tts") or {}
    mounts, disp = snap.get("mounts") or {}, snap.get("display") or {}
    ff = snap.get("fastfetch") or {}
    desk = (f"Desktop: niri ({comp.get('windows', 0)} windows) on "
            f"{disp.get('wayland_display') or disp.get('display') or 'unknown display'}"
            if comp.get("ok") else "Desktop: niri IPC unavailable")
    if ff.get("ok"):
        d0 = (ff.get("displays") or [{}])[0]  # exact display values, no guessing
        if d0.get("name"):
            desk += f" · {d0['name']} {d0.get('width', 0)}x{d0.get('height', 0)}"
    mic = (f"Mic: {audio.get('default')} ({audio.get('count')} inputs)"
           if audio.get("ok") and audio.get("count")
           else "Mic: no input devices visible")
    brain = (f"Brain: {snap.get('models', {}).get('ollama_model', '?')} via Ollama "
             f"({len(ollama.get('models', []))} models pulled)"
             if ollama.get("ok") else "Brain: ollama unreachable")
    voices = ", ".join((stt.get("onnx") or [])[:2]) or "none"
    voice = (f"Voices: whisper {stt.get('whisper_size', '?')} "
             f"({'cached' if stt.get('whisper_cached') else 'not cached'}), piper: {voices}")
    sock = mounts.get("sock_path") or "control socket"
    if ff.get("ok"):
        gpus = ", ".join(ff.get("gpu") or []) or "no GPU"
        sys_line = (f"Sys: {ff.get('os')} · {ff.get('host')} · "
                    f"{ff.get('cpu')} · {gpus} · "
                    f"RAM {_gib(ff.get('memory_total'))} GiB · "
                    f"Disk {_gib(ff.get('disk_free'))} GiB free · "
                    f"Socket: {sock} ({'present' if mounts.get('sock_present') else 'missing'})")
    else:
        sys_line = (f"Control socket: {sock} "
                    f"({'present' if mounts.get('sock_present') else 'missing'})")
    return "\n".join([desk, mic, brain, voice, sys_line])[:max_chars]

def main(argv: list[str] | None = None) -> int:
    """`--preflight`: JSON snapshot on stdout; always exit 0, never raises."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print("usage: hardware.py [--preflight]  (prints JSON snapshot)")
        return 0
    home = Path.home()
    ctx = {
        "ollama_base": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        "ollama_model": os.environ.get("HANDSOFF_MODEL", ""),
        "whisper_size": os.environ.get("HANDSOFF_WHISPER", ""),
        "mic_device": "",
        "whisper_model_dir": str(home / ".config/handsoff/whisper-model"),
        "piper_voice_dir": str(home / ".config/handsoff/piper-voice"),
        "control_sock": str(home / ".local/state/handsoff/control.sock"),
        "state_dir": str(home / ".local/state/handsoff"),
        "systemd_unit_file": str(home / ".config/systemd/user/handsoff.service"),
    }
    try:
        print(json.dumps(snapshot(ctx, None, {}), indent=1))
    except Exception as e:  # last resort: degraded JSON still exits 0
        print(json.dumps({"ok": False, "error": str(e)[:200]}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
