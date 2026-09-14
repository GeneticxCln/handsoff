"""Desktop-action tests: typing, screens, windows, workspaces, operator, niri manifest."""
from __future__ import annotations

import base64
import contextvars
import importlib.util
import inspect
from collections import deque
import threading
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import HERE as ROOT, _load, _user_site, core_module

from core import settings as _core_settings

_core_tools = core_module("tools")   # resolved on first use, inside the sandbox

HERE = ROOT   # the repo root (conftest resolves it from conftest.py's parent)


class TestKeyboardTakeover:
    """type_text / press_keys: virtual-keyboard injection guards."""

    @pytest.fixture()
    def belt(self, H):
        return H.ToolBelt(on_restart_pending=lambda: None)

    def test_type_text_invokes_ydotool(self, belt, monkeypatch):
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        calls = []
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("type_text", {"text": "hello world"})
        assert not err and "typed 11" in out
        assert calls == [("type", "--key-delay", "6", "--", "hello world")]

    def test_an_unidentifiable_window_is_never_typed_into(self, belt,
                                                          monkeypatch):
        """A window with NO app_id was allowed whenever its title was
        non-empty, on the theory that a title identifies the target. It does
        not — a terminal can be titled anything — and the rest of this class
        exists to fail CLOSED on exactly that. `press_keys` had the same hole."""
        typed: list = []
        monkeypatch.setattr(
            belt.__class__, "_focused_window_info",
            lambda self: {"app_id": "", "title": "untitled - bash"})
        monkeypatch.setattr(belt.__class__, "_ydotool",
                            lambda self, *a: typed.append(a) or "ok")
        out, err = belt.execute("type_text", {"text": "rm -rf /"})
        assert err and "REFUSED" in out, out
        out, err = belt.execute("press_keys", {"combo": "ctrl+d"})
        assert err and "REFUSED" in out, out
        assert typed == [], "keys were injected into an unidentified window"

    def test_type_text_chunks_long_input(self, belt, monkeypatch):
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        calls = []
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        chunk = belt._TYPE_CHUNK
        text = "x" * (chunk * 2)
        out, err = belt.execute("type_text", {"text": text})
        assert not err and f"typed {len(text)}" in out
        # One injection per focus-verified chunk, in order, nothing re-sent.
        # Written against the class constant, not a literal: the chunk size is
        # a safety knob (it is the window in which focus could change mid-type)
        # and this guard is about the CHUNKING, not about any particular size.
        assert len(calls) == 2, calls
        assert calls[0][-1] == text[:chunk]
        assert calls[1][-1] == text[chunk:]

    def test_type_text_retries_nonascii_as_ascii(self, belt, monkeypatch):
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        calls = []
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ERROR: ydotool failed" if len(calls) == 1 else "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("type_text", {"text": "café — ok"})
        assert not err
        assert len(calls) == 2                       # first attempt failed, retry succeeded
        assert calls[1][-1] == "cafe - ok"           # transliterated fallback

    def test_type_text_refuses_empty_and_huge(self, belt):
        out, err = belt.execute("type_text", {"text": ""})
        assert err and out.startswith("REFUSED")
        out, err = belt.execute("type_text", {"text": "x" * (belt._MAX_TYPE + 1)})
        assert err and out.startswith("REFUSED")


class TestYdotooldSocket:
    """The ydotool CLI's compiled-in default is /tmp/.ydotool_socket, but the
    user-level daemon (Arch: ydotool.service) listens on $XDG_RUNTIME_DIR —
    and binds SOCK_DGRAM, which a stream probe can never connect to. Every
    type/click used to die with 'failed to connect' while the daemon ran fine."""

    @pytest.fixture()
    def belt(self, H):
        H.ToolBelt._YDOTOOL_SOCK_CACHE.clear()
        yield H.ToolBelt(on_restart_pending=lambda: None)
        H.ToolBelt._YDOTOOL_SOCK_CACHE.clear()

    def _bind_dgram(self, path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(str(path))
        return s

    def test_probe_accepts_dgram_daemon(self, belt, tmp_path):
        sock = self._bind_dgram(tmp_path / "d")
        try:
            assert belt._socket_connectable(str(tmp_path / "d"))
        finally:
            sock.close()
        assert not belt._socket_connectable(str(tmp_path / "d"))  # dead path

    def test_probe_rejects_non_sockets(self, belt, tmp_path):
        f = tmp_path / "f"
        f.write_text("not a socket")
        assert not belt._socket_connectable(str(f))
        assert not belt._socket_connectable(str(tmp_path / "missing"))

    def test_resolver_prefers_runtime_socket(self, belt, tmp_path, monkeypatch):
        monkeypatch.setattr(belt, "YDOTOOL_SOCKET", str(tmp_path / "legacy"))
        runtime = tmp_path / "run"
        runtime.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        s = self._bind_dgram(runtime / ".ydotool_socket")
        try:
            assert belt._ydotool_socket() == str(runtime / ".ydotool_socket")
        finally:
            s.close()

    def test_resolver_falls_back_to_legacy_socket(self, belt, tmp_path, monkeypatch):
        monkeypatch.setattr(belt.__class__, "YDOTOOL_SOCKET", str(tmp_path / "legacy"))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        s = self._bind_dgram(tmp_path / "legacy")
        try:
            assert belt._ydotool_socket() == str(tmp_path / "legacy")
        finally:
            s.close()

    def test_resolver_survives_no_daemon(self, belt, tmp_path, monkeypatch):
        """No daemon anywhere: return the first candidate (the call itself
        will fail loudly) instead of raising."""
        monkeypatch.setattr(belt.__class__, "YDOTOOL_SOCKET", str(tmp_path / "none"))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert belt._ydotool_socket() == str(tmp_path / "none")

    def test_ydotool_call_points_env_at_resolved_socket(self, H, belt, tmp_path, monkeypatch):
        target = str(tmp_path / "legacy")
        monkeypatch.setattr(belt.__class__, "YDOTOOL_SOCKET", target)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        s = self._bind_dgram(tmp_path / "legacy")
        try:
            seen = {}
            class R:
                returncode, stdout, stderr = 0, "", ""
            def fake_run(argv, **kw):
                seen.update(argv=argv, env=kw.get("env"))
                return R()
            monkeypatch.setattr(H.subprocess, "run", fake_run)
            assert belt._ydotool("type", "--", "hi") == "ok"
            assert seen["argv"] == ["ydotool", "type", "--", "hi"]
            assert seen["env"]["YDOTOOL_SOCKET"] == target
        finally:
            s.close()

    def test_doctor_reports_daemon_reachability(self, H, monkeypatch):
        """Doctor must distinguish 'binary present but daemon dead' from ok —
        the binary check alone called a broken setup healthy."""
        monkeypatch.setattr(H.shutil, "which", lambda n: "/usr/bin/ydotool")
        monkeypatch.setattr(H.ToolBelt, "_ydotool_socket",
                            classmethod(lambda cls: "/nonexistent/ydotool"))
        monkeypatch.setattr(H.ToolBelt, "_socket_connectable", staticmethod(lambda p: False))
        text = H.run_doctor()
        assert "UNREACHABLE" in text
        assert "ydotool.service" in text          # tells the user how to fix it
        monkeypatch.setattr(H.ToolBelt, "_socket_connectable", staticmethod(lambda p: True))
        text = H.run_doctor()
        assert "daemon reachable" in text

    def test_press_keys_enter(self, belt, monkeypatch):
        calls = []
        # hermetic: never depend on the live desktop focus (CI has no niri,
        # and a focused terminal on a dev box must not change the outcome)
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("press_keys", {"combo": "enter"})
        assert not err and out == "ok"
        assert calls == [("key", "28:1", "28:0")]

    def test_press_keys_ctrl_a(self, belt, monkeypatch):
        calls = []
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("press_keys", {"combo": "ctrl+a"})
        assert not err
        assert calls == [("key", "29:1", "30:1", "30:0", "29:0")]

    def test_press_keys_unknown_key_refused(self, belt, monkeypatch):
        # hermetic focus: assert the KEY-VALIDATION refusal, not an accidental
        # fail-closed or terminal-guard refusal from whatever is on screen
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        out, err = belt.execute("press_keys", {"combo": "ctrl+frobnicate"})
        assert err and "unknown key" in out

    def test_press_keys_modifiers_only_refused(self, belt, monkeypatch):
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        out, err = belt.execute("press_keys", {"combo": "ctrl+shift"})
        assert err and out.startswith("REFUSED")

    # -- press_hotkey: compositor shortcuts (Mod/Super = niri Mod) ----------

    def test_press_hotkey_mod_e_argv(self, belt, monkeypatch):
        calls = []
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("press_hotkey", {"combo": "Mod+E"})
        assert not err and out == "ok"
        # Super down (125), e down/up (18), Super up — in that order
        assert calls == [("key", "125:1", "18:1", "18:0", "125:0")]

    def test_press_hotkey_mod_return(self, belt, monkeypatch):
        calls = []
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("press_hotkey", {"combo": "Mod+Return"})
        assert not err
        assert calls == [("key", "125:1", "28:1", "28:0", "125:0")]

    def test_press_hotkey_synonym_and_case_insensitive(self, belt, monkeypatch):
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        calls = []
        def fake_ydotool(self, *args):
            calls.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("press_hotkey", {"combo": "super + t"})
        assert not err
        assert calls == [("key", "125:1", "20:1", "20:0", "125:0")]

    def test_press_hotkey_no_terminal_guard_for_super(self, belt, monkeypatch):
        """Super-chords are intercepted by the compositor, never seen by the
        app — so they stay allowed even when a terminal is focused."""
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "foot", "title": "foot"})
        monkeypatch.setattr(belt.__class__, "_ydotool", lambda self, *a: "ok")
        out, err = belt.execute("press_hotkey", {"combo": "Mod+E"})
        assert not err and out == "ok"

    def test_press_hotkey_terminal_guard_without_super(self, belt, monkeypatch):
        """No-Super chords reach the focused app: blocked in terminals."""
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "foot", "title": "foot"})
        monkeypatch.setattr(belt.__class__, "_ydotool", lambda self, *a: "ok")
        out, err = belt.execute("press_hotkey", {"combo": "ctrl+r"})
        assert err and "terminal" in out

    def test_press_hotkey_unknown_and_mods_only_refused(self, belt):
        out, err = belt.execute("press_hotkey", {"combo": "Mod+frobnicate"})
        assert err and "unknown key" in out
        out, err = belt.execute("press_hotkey", {"combo": "Mod+Shift"})
        assert err and "non-modifier" in out

    def test_press_hotkey_permission_gate(self, belt, H):
        belt._perm["press_keys"] = False
        out, err = belt.execute("press_hotkey", {"combo": "Mod+E"})
        assert err and out.startswith("REFUSED")

    # -- close_window: graceful close with self-preservation ----------------

    def test_close_window_by_name(self, belt, monkeypatch):
        wins = [
            {"id": 1, "app_id": "firefox", "title": "Mozilla Firefox", "is_focused": False},
            {"id": 2, "app_id": "foot", "title": "term", "is_focused": True},
        ]
        closed = []
        def fake_run(argv, **kw):
            if argv[1:3] == ["msg", "--json"]:
                class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
                return P()
            closed.append(argv)
            wins[:] = [w for w in wins if str(w["id"]) != argv[-1]]
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("close_window", {"app": "firefox"})
        assert not err and "closed 1" in out and "firefox" in out
        assert "confirmed gone" in out             # post-action verification
        assert closed and closed[0][-1] == "1"

    def test_close_window_lingering_reported(self, belt, monkeypatch):
        """A polite close can be declined (unsaved-work dialog): the tool
        must admit the window survived instead of claiming success."""
        wins = [{"id": 1, "app_id": "gedit", "title": "unsaved.txt *",
                 "is_focused": False}]
        def fake_run(argv, **kw):
            class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("close_window", {"app": "gedit"})
        assert not err and "still open" in out and "gedit" in out

    def test_close_this_targets_focused_window(self, belt, monkeypatch):
        wins = [
            {"id": 2, "app_id": "foot", "title": "term", "is_focused": True},
            {"id": 5, "app_id": "firefox", "title": "FF", "is_focused": False},
        ]
        closed = []
        def fake_run(argv, **kw):
            if argv[1:3] == ["msg", "--json"]:
                class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
                return P()
            closed.append(argv)
            wins[:] = [w for w in wins if str(w["id"]) != argv[-1]]
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("close_window", {"app": "this"})
        assert not err and "closed 1" in out and "foot" in out
        assert closed and closed[0][-1] == "2"

    def test_close_window_refuses_self(self, belt, monkeypatch):
        """The bubble must never be able to close itself via this tool."""
        wins = [
            {"id": 3, "app_id": "handsoff", "title": "handsoff bubble", "is_focused": False},
            {"id": 4, "app_id": "firefox", "title": "FF", "is_focused": False},
        ]
        closed = []
        def fake_run(argv, **kw):
            if argv[1:3] == ["msg", "--json"]:
                class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
                return P()
            closed.append(argv)
            wins[:] = [w for w in wins if str(w["id"]) != argv[-1]]
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("close_window", {"app": "handsoff"})
        assert err and "will not close my own bubble" in out
        assert closed == []
        # firefox in the same list is still closable
        out, err = belt.execute("close_window", {"app": "firefox"})
        assert not err and "closed 1" in out

    def test_close_window_no_match_lists_windows(self, belt, monkeypatch):
        wins = [{"id": 2, "app_id": "foot", "title": "term", "is_focused": True}]
        def fake_run(argv, **kw):
            class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("close_window", {"app": "thunderbird"})
        assert err and "no window matching" in out and "foot" in out

    def test_close_window_empty_refused(self, belt):
        out, err = belt.execute("close_window", {"app": ""})
        assert err and out.startswith("REFUSED")

    def test_close_window_permission_gate(self, belt, H):
        belt._perm["run_command"] = False
        out, err = belt.execute("close_window", {"app": "firefox"})
        assert err and out.startswith("REFUSED")

    def test_type_text_permission_gate(self, belt, H):
        belt._perm["type_text"] = False
        out, err = belt.execute("type_text", {"text": "hi"})
        assert err and out.startswith("REFUSED") and "disabled" in out
        belt._perm["press_keys"] = False
        out, err = belt.execute("press_keys", {"combo": "enter"})
        assert err and out.startswith("REFUSED") and "disabled" in out

    def test_tools_schema_advertises_new_tools(self, H):
        names = {t["function"]["name"] for t in H.TOOLS}
        assert {"type_text", "press_keys"} <= names

    def test_argument_aliases_tolerated(self, belt, monkeypatch):
        """Local models sometimes name the argument 'keys' or 'content';
        the tool must not punish them with an empty-argument refusal."""
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: {"app_id": "firefox", "title": "Firefox"})
        seen = []
        def fake_ydotool(self, *args):
            seen.append(args)
            return "ok"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("press_keys", {"keys": "enter"})
        assert not err and ("key", "28:1", "28:0") in seen
        out, err = belt.execute("type_text", {"content": "hi"})
        assert not err and "typed 2" in out

    def test_focus_window_matches_and_focuses(self, belt, monkeypatch):
        wins = [
            {"id": 1, "app_id": "google-chrome", "title": "Docs"},
            {"id": 7, "app_id": "firefox", "title": "Mozilla Firefox"},
        ]
        focused = []
        def fake_run(argv, **kw):
            if argv[1:3] == ["msg", "--json"]:
                class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
                return P()
            focused.append(argv)
            # stateful: the focused window now reports is_focused
            for w in wins:
                w["is_focused"] = str(w["id"]) == argv[-1]
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("focus_window", {"app": "firefox"})
        assert not err and "focused firefox" in out
        assert "focus confirmed" in out            # post-action verification
        assert focused and "--id" in focused[0] and "7" in focused[0]

    def test_focus_window_unconfirmed_reported(self, belt, monkeypatch):
        """If the window never reports is_focused, the tool must say so —
        'niri accepted the command' is not 'the window has focus'."""
        wins = [{"id": 1, "app_id": "foot", "title": "term"}]
        def fake_run(argv, **kw):
            class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("focus_window", {"app": "foot"})
        assert not err and "NOT confirmed" in out

    def test_focus_window_no_match_lists_windows(self, belt, monkeypatch):
        wins = [{"id": 1, "app_id": "foot", "title": "terminal"}]
        def fake_run(argv, **kw):
            class P: returncode = 0; stdout = json.dumps(wins); stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("focus_window", {"app": "thunderbird"})
        assert err and "no window matching" in out and "foot" in out

    def test_clipboard_roundtrip_tools(self, belt, monkeypatch):
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            class P:
                returncode = 0; stderr = ""; stdout = "" if argv[0] == "wl-copy" \
                    else "hello clipboard"
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("copy_text", {"text": "hello clipboard"})
        assert not err and "copied 15" in out
        out, err = belt.execute("paste_text", {})
        assert not err and "hello clipboard" in out

    def test_set_reminder_persists(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder",
                                {"wake_name": "tea", "when_due": "in 90 minutes"})
        assert not err and "in 1 hour 30 minutes" in out, out
        rows = H.json.loads(rf.read_text())
        assert len(rows) == 1 and rows[0]["name"] == "tea"
        assert abs(rows[0]["due"] - (H.time.time() + 5400)) < 5

    def test_web_tools_exist_and_gate(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        names = {t["function"]["name"] for t in H.TOOLS}
        assert {"get_weather", "web_search", "lookup_fact", "get_datetime"} <= names
        belt._perm["web_access"] = False
        out, err = belt.execute("get_weather", {"place": "Berlin"})
        assert err and out.startswith("REFUSED") and "web_access" in out

    def test_weather_and_fact_live(self, H):
        """Live network smoke test (skipped when offline)."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        try:
            out, err = belt.execute("get_weather", {"place": "Berlin"})
        except Exception:
            pytest.skip("offline")
        if err and "failed" in out:
            pytest.skip("offline")
        assert not err and "°C" in out
        out2, err2 = belt.execute("lookup_fact", {"topic": "Blue Yeti"})
        assert not err2 and "Blue" in out2


class TestScreenVision:
    """see_screen / read_screen_text / open_app: the bubble's eyes and hands."""

    def test_see_screen_attaches_image(self, H, tmp_path, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.ToolBelt, "SCREENSHOT_FILE", tmp_path / "screen.png")
        fake_png = b"\x89PNG\r\n\x1a\n" + b"x" * 64
        def fake_run(argv, **kw):
            (tmp_path / "screen.png").write_bytes(fake_png)
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("see_screen", {"question": "what is open?"})
        assert not err and "attached" in out
        assert len(belt._last_images) == 1
        assert base64.b64decode(belt._last_images[0]) == fake_png

    def test_see_screen_bad_region(self, H, tmp_path, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.ToolBelt, "SCREENSHOT_FILE", tmp_path / "screen.png")
        out, err = belt.execute("see_screen", {"region": "attack; rm"})
        assert err and "region must be" in out
        assert belt._last_images == []

    def test_read_screen_text_runs_ocr(self, H, tmp_path, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.ToolBelt, "SCREENSHOT_FILE", tmp_path / "screen.png")
        def fake_run(argv, **kw):
            if argv[0] == "grim":
                (tmp_path / "screen.png").write_bytes(b"\x89PNG fake")
            class P:
                returncode = 0; stderr = ""
                stdout = "Hello\nScreen" if argv[0] == "tesseract" else ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        out, err = belt.execute("read_screen_text", {})
        assert not err and "Hello Screen" in out

    def test_screen_permission_gate(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt._perm["screen_access"] = False
        out, err = belt.execute("see_screen", {})
        assert err and out.startswith("REFUSED") and "screen_access" in out
        out, err = belt.execute("read_screen_text", {})
        assert err and out.startswith("REFUSED")

    def test_open_app_resolves_alias_and_blocks_hostile(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        spawned = []
        def fake_run(argv, **kw):
            if argv[0] == "niri":
                spawned.append(argv)
                class P0: pass
                return P0()
            class P:
                returncode = 0; stdout = "/usr/bin/foot"; stderr = ""
            return P()
        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: None)
        # Resolution is `shutil.which`, so faking only the subprocess left this
        # test needing a real `foot` in the image: with none it returned
        # "no program matching 'terminal'" and the alias assertions never ran.
        monkeypatch.setattr(H.shutil, "which",
                            lambda n: f"/usr/bin/{n}" if n == "foot" else None)
        out, err = belt.execute("open_app", {"app": "terminal"})
        assert not err and "launched foot" in out
        out, err = belt.execute("open_app", {"app": "python"})
        assert err and out.startswith("REFUSED")
        out, err = belt.execute("open_app", {"app": "toolkit; rm -rf"})
        assert err


class TestWorkspaceTool:
    """Natural-language workspace control (niri)."""

    def _belt(self, H, monkeypatch, fake_niri):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(belt.__class__, "_niri_windows",
                            lambda self: [], raising=False)
        return belt

    def test_go_switch(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        calls = []
        monkeypatch.setattr(belt.__class__, "_niri_run",
                            lambda self, *a: calls.append(a), raising=False)
        import subprocess as sp
        class R:
            returncode = 0; stdout = ""; stderr = ""
        monkeypatch.setattr(H.subprocess, "run",
                            lambda cmd, **k: (calls.append(cmd), R())[1])
        out, err = belt.execute("workspace", {"action": "go", "target": "2"})
        assert not err and "switched to workspace 2" in out
        assert any("focus-workspace" in " ".join(c) and " 2" in " ".join(c)
                   for c in calls)

    def test_move_with_app_and_word_strip(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        import subprocess as sp
        class R:
            returncode = 0; stdout = ""; stderr = ""
        wins = [{"id": 7, "app_id": "foot", "title": "term", "workspace_id": 1}]
        def fake_run(cmd, **k):
            if "--json" in cmd and "windows" in cmd:
                m = R(); m.stdout = json.dumps(wins); return m
            return R()
        monkeypatch.setattr(H.subprocess, "run", fake_run)
        out, err = belt.execute("workspace",
                                {"action": "move", "target": "foot to the workspace 3"})
        assert not err and "moved the window to workspace 3" in out

    def test_unknown_action_refused(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("workspace", {"action": "dance"})
        assert err and "unknown workspace action" in out

    def test_list_renders_names_aliases_focus_and_windows(self, H, monkeypatch):
        """The LIST branch is what the model reads to decide where to type —
        every decoration it reports must come from real data: the workspace
        name, the user's aliases for that index, which one is focused, and the
        windows on each. It was the one workspace branch with no test."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        wss = [{"idx": 1, "id": 11, "name": "chat", "is_focused": False},
               {"idx": 2, "id": 22, "is_focused": True}]
        wins = [{"id": 7, "workspace_id": 11, "app_id": "foot", "title": "term"},
                {"id": 8, "workspace_id": 22, "app_id": "firefox",
                 "title": ""},
                {"id": 9, "workspace_id": 999, "app_id": "ghost",
                 "title": "lost window"}]
        class R:
            returncode = 0; stderr = ""; stdout = ""
        def fake_run(cmd, **k):
            r = R()
            if "--json" in cmd and "workspaces" in cmd:
                r.stdout = json.dumps(wss)
            elif "--json" in cmd and "windows" in cmd:
                r.stdout = json.dumps(wins)
            return r
        monkeypatch.setattr(H.subprocess, "run", fake_run)
        monkeypatch.setitem(H.SETTINGS, "workspace_aliases", {"work": "2"})
        out, err = belt.execute("workspace", {"action": "list"})
        assert not err, out
        assert "ws1 (chat)" in out, out                     # the niri name
        assert "[work]" in out, out                        # the user's alias
        assert "ws2" in out and "[current]" in out, out     # the focused one
        # window label: TITLE when there is one, app_id when not (ws2's
        # firefox has an empty title and must still be listed)
        assert "term" in out, out
        assert "firefox" in out, out
        assert "ghost" not in out, out                     # foreign workspace id
        assert "(empty)" not in out, out                   # both have windows

    def test_list_empty_and_foreign_windows(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        class R:
            returncode = 0; stderr = ""; stdout = ""
        def fake_run(cmd, **k):
            r = R()
            if "--json" in cmd and "workspaces" in cmd:
                r.stdout = json.dumps([{"idx": 1, "id": 5}])
            elif "--json" in cmd and "windows" in cmd:
                r.stdout = json.dumps([])
            return r
        monkeypatch.setattr(H.subprocess, "run", fake_run)
        out, err = belt.execute("workspace", {"action": "list"})
        assert not err and "ws1: (empty)" in out, out

    def test_list_niri_failure_is_an_error_not_a_crash(self, H, monkeypatch):
        """A dead compositor is a tool ERROR, not an exception through the
        turn — the model needs the sentence to tell the user, not a traceback.
        The list branch calls the CLOSURE `_niri` (plain subprocess.run), not
        the classmethod `_niri_msg`, so the seam to break is subprocess.run."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        def boom(cmd, **k):
            raise RuntimeError("niri socket gone")
        monkeypatch.setattr(H.subprocess, "run", boom)
        out, err = belt.execute("workspace", {"action": "list"})
        assert err, "an exception through execute() means the turn died"
        assert "ERROR: cannot read workspaces" in out, out

    def test_in_prompt(self, H):
        assert "WORKSPACES" in H.SYSTEM_PROMPT

    def test_workspace_idx_map_and_label(self, H, monkeypatch):
        """niri's window JSON carries a global workspace_id that can differ
        from the user-facing index (multi-output setups); labels must show
        the INDEX, resolved through a cached id→idx map."""
        wss = [{"id": 1, "idx": 2, "output": "DP-3"},
               {"id": 9, "idx": 1, "output": "DP-3"}]
        class R:
            returncode, stdout, stderr = 0, json.dumps(wss), ""
        monkeypatch.setattr(H.ToolBelt, "_WS_IDX_CACHE", {})
        monkeypatch.setattr(H.subprocess, "run",
                            lambda cmd, **k: (lambda m: m)(R()))
        mapping = H.ToolBelt._workspace_idx_map(refresh=True)
        assert mapping == {1: 2, 9: 1}
        win = {"app_id": "foot", "title": "term", "workspace_id": 1}
        label = H.ToolBelt._win_label(win)
        assert label == "foot: term (workspace 2)"   # NOT 'workspace 1'
        assert H.ToolBelt._workspace_idx_of(
            {"app_id": "x", "workspace_id": 99}) is None

    def test_idx_map_ttl_caches_and_refreshes(self, H, monkeypatch):
        calls = []
        class R:
            returncode, stdout, stderr = 0, json.dumps(
                [{"id": 1, "idx": 2}]), ""
        def fake_run(cmd, **k):
            calls.append(cmd)
            return R()
        monkeypatch.setattr(H.ToolBelt, "_WS_IDX_CACHE", {})
        monkeypatch.setattr(H.subprocess, "run", fake_run)
        H.ToolBelt._workspace_idx_map(refresh=True)
        H.ToolBelt._workspace_idx_map()            # served from cache
        assert len(calls) == 1
        H.ToolBelt._workspace_idx_map(refresh=True)  # explicit refresh
        assert len(calls) == 2

    def test_manifest_exposes_idx_not_raw_ids(self, H, monkeypatch):
        monkeypatch.setattr(H.ToolBelt, "_WS_IDX_CACHE", {})
        monkeypatch.setattr(H.ToolBelt, "_niri_windows", classmethod(
            lambda cls: [{"id": 5, "app_id": "foot", "title": "t",
                          "workspace_id": 1, "is_focused": True}]))
        class R:
            returncode, stdout, stderr = 0, json.dumps(
                [{"id": 1, "idx": 2}]), ""
        monkeypatch.setattr(H.subprocess, "run", lambda cmd, **k: R())
        m = H.ToolBelt._build_manifest()
        assert m["windows"]["placement"] == {"foot": 2}
        assert m["workspaces"]["indices"] == [2]
        assert "workspace_id" not in json.dumps(m["windows"])
        assert "workspace_id" not in json.dumps(m["workspaces"])

    def test_move_verifies_by_idx(self, H, monkeypatch):
        """After a move the tool must poll the fresh id→idx map and confirm
        against the user-facing index — niri accepting is not the window
        having moved."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.ToolBelt, "_WS_IDX_CACHE", {})
        polls = {"n": 0}
        moved_win = {"id": 7, "app_id": "foot", "title": "term",
                     "workspace_id": 9, "is_focused": True}

        class R:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, **k):
            cmd_s = " ".join(cmd)
            if "move-window-to-workspace" in cmd_s:
                return R()
            if "workspaces" in cmd_s:
                return R.__class__.__new__(R) if False else _R(
                    json.dumps([{"id": 9, "idx": 3}]))
            # windows: first poll still shows old placement, then moved
            polls["n"] += 1
            return _R(json.dumps([moved_win if polls["n"] > 1 else
                                  {**moved_win, "workspace_id": 1}]))

        def _R(stdout):
            r = R(); r.stdout = stdout; return r

        monkeypatch.setattr(H.subprocess, "run", fake_run)
        monkeypatch.setattr(H.time, "sleep", lambda s: None)
        out, err = belt.execute("workspace", {"action": "move",
                                              "target": "foot to 3"})
        assert not err
        assert "moved the window to workspace 3 (verified)" in out

    def test_move_unconfirmed_reported(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.ToolBelt, "_WS_IDX_CACHE", {})
        win = {"id": 7, "app_id": "foot", "title": "term",
               "workspace_id": 1, "is_focused": True}

        class R:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(cmd, **k):
            cmd_s = " ".join(cmd)
            if "workspaces" in cmd_s:
                r = R(); r.stdout = json.dumps([{"id": 9, "idx": 3}]); return r
            if "windows" in cmd_s:
                r = R(); r.stdout = json.dumps([win]); return r
            return r2

        r2 = R()
        monkeypatch.setattr(H.subprocess, "run", fake_run)
        monkeypatch.setattr(H.time, "sleep", lambda s: None)
        out, err = belt.execute("workspace", {"action": "move",
                                              "target": "foot to 3"})
        assert not err
        assert "NOT confirmed" in out

    def test_no_shadowed_docstrings(self, H):
        import ast
        tree = ast.parse((HERE / "handsoff.py").read_text())
        def check(node, where):
            body = node.body if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)) else []
            seen_doc = False
            for st in body:
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) \
                        and isinstance(st.value.value, str):
                    assert not seen_doc, f"shadowed docstring in {where}"
                    seen_doc = True
                else:
                    seen_doc = False
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                check(n, n.name)


class TestWorkspaceAliasesAndBriefing:
    """Personal workspace aliases + the daily morning briefing."""

    def test_alias_resolution(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "workspace_aliases", {"code": "2"})
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        calls = []
        class R:
            returncode = 0; stdout = ""; stderr = ""
        monkeypatch.setattr(H.subprocess, "run",
                            lambda cmd, **k: (calls.append(cmd), R())[1])
        out, err = belt.execute("workspace", {"action": "go", "target": "code"})
        assert not err and "switched to workspace 2" in out
        assert any("focus-workspace" in " ".join(c) and c[-1] == "2" for c in calls)

    def test_alias_settings_coerce(self, H, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"workspace_aliases": {"Code": " 2 ", "": "x", 3: "1"}}))
        monkeypatch.setattr(H, "SETTINGS_FILE", f)
        s = H._load_settings()
        assert s["workspace_aliases"] == {"code": "2", "3": "1"}

    def test_weather_uses_home_place(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Testdorf")
        monkeypatch.setattr(H, "_geocode", lambda p: (0.0, 0.0, p))
        monkeypatch.setattr(H, "_http_get", lambda url: json.dumps({
            "current": {"temperature_2m": 1, "apparent_temperature": 1,
                        "relative_humidity_2m": 1, "weather_code": 0,
                        "wind_speed_10m": 1, "time": "2026-09-07T10:00"},
            "daily": {"temperature_2m_max": [2, 3], "temperature_2m_min": [0, 1],
                      "precipitation_sum": [0, 0], "weather_code": [0, 0]}}))
        out = belt.get_weather("")
        assert "Testdorf" in out   # real method pulled the place from settings

    @staticmethod
    def _brief_stub(H, tools):
        """Assistant is a QObject; carry the real method on a plain stub."""
        class Stub:
            _briefing_done_date = ""
            _BRIEFING_SKIP_PREFIXES = H.Assistant._BRIEFING_SKIP_PREFIXES
            _maybe_briefing_prefix = H.Assistant._maybe_briefing_prefix
        s = Stub()
        s._tools = tools
        return s

    def test_briefing_once_daily_and_skips_commands(self, H, monkeypatch):
        tools = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(tools.__class__, "execute",
                            lambda self, n, args: ("weather: 20C sunny", False))
        a = self._brief_stub(H, tools)
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        # The world-headline half of the briefing is a live DuckDuckGo fetch;
        # stubbing it keeps this test's runtime off a third party's uptime (it
        # measured 10 s under throttling and ~0 s otherwise).
        monkeypatch.setattr(H, "_world_events", lambda *a, **k: ([], False))
        p1 = a._maybe_briefing_prefix("good morning")
        assert "weather: 20C" in p1
        assert a._briefing_done_date  # marked done
        assert a._maybe_briefing_prefix("what time is it") == ""  # once per day
        a._briefing_done_date = ""
        assert a._maybe_briefing_prefix("open firefox") == ""     # commands skip

    def test_briefing_off_by_default(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.SETTINGS, "briefing": False})
        a = self._brief_stub(H, tools=None)
        assert a._maybe_briefing_prefix("good morning") == ""
        assert H.DEFAULT_SETTINGS["briefing"] is False


class TestDictationMode:
    """Zero-LLM dictation: 'start dictation' (no wake word) arms typing;
    subsequent transcripts are typed into the focused window via the same
    type_text path the model uses (terminal guard + permission apply)."""

    def _mk(self, H, monkeypatch):
        monkeypatch.setattr(H, "SETTINGS", {**H.DEFAULT_SETTINGS})
        a = H.Assistant.__new__(H.Assistant)
        a._dictation = False
        a._handsfree = True
        a._gen = 0
        a._state = H.IDLE
        a._tools = types.SimpleNamespace(type_text=lambda t: "ok")
        a.spoken = []
        a._speak = lambda text, gen, cancel: a.spoken.append(text)
        a._set = lambda gen, s: None
        return a

    def test_toggle_phrases(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        for phrase, want in [("start dictation", True), ("stop dictation", False),
                             ("hey assistant, dictation", True),
                             ("dictation mode", True), ("Dictation!", True)]:
            a._dictation = not want
            assert a._try_dictation(phrase, 1, threading.Event()) is True
            assert a._dictation == want, phrase

    def test_dictated_text_is_typed_not_sent_to_brain(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        a._try_dictation("start dictation", 1, threading.Event())
        typed = []
        a._tools = types.SimpleNamespace(type_text=lambda t: typed.append(t) or "ok")
        assert a._try_dictation("type this please", 2, threading.Event()) is True
        assert typed == ["type this please"]

    def test_normal_utterances_pass_through_when_off(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        assert a._try_dictation("what's the weather in Berlin", 1,
                                threading.Event()) is False
        assert a._dictation is False

    def test_setting_off_disables_even_the_toggle(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        H.SETTINGS["dictation"] = False
        assert a._try_dictation("start dictation", 1, threading.Event()) is False
        assert a._dictation is False

    def test_a_junk_value_that_means_off_disables_it_too(self, H, monkeypatch):
        """`bool("false")` is True — dictation typed transcripts into whatever
        window was focused, for a value that asked for it to be off.

        Only the forms that PARSE as false: dictation's default is ON, so
        unparseable junk takes the default (what the loader stores for junk
        too) — the two must agree, which is the whole point of the pass.
        """
        for raw in ("false", "no", "off", "0", ""):
            a = self._mk(H, monkeypatch)
            monkeypatch.setitem(H.SETTINGS, "dictation", raw)
            assert a._try_dictation("start dictation", 1,
                                    threading.Event()) is False, raw
            assert a._dictation is False, raw

    def test_terminal_refusal_stops_dictation_and_explains(self, H, monkeypatch):
        a = self._mk(H, monkeypatch)
        a._dictation = True
        a._tools = types.SimpleNamespace(
            type_text=lambda t: "REFUSED: the focused window is a terminal (kitty)")
        assert a._try_dictation("secret commands", 1, threading.Event()) is True
        assert a._dictation is False
        assert any("Dictation stopped" in s for s in a.spoken)

    def test_socket_actions_exist(self, H):
        assert {"dictation", "dictation-on", "dictation-off"} <= H.PTT_ACTIONS
        usage = H.__dict__.get("USAGE", "") or inspect.getsource(H)[
            H.__dict__.get("_usage_start", 0):]
        src = inspect.getsource(H)
        assert "dictation-on   enable voice dictation" in src

    def test_setting_and_checkbox_wiring(self, H):
        assert H.DEFAULT_SETTINGS["dictation"] is True
        D = H.DEFAULT_SETTINGS
        assert _core_settings.coerce_settings({**D, "dictation": 0})["dictation"] is False
        assert _core_settings.coerce_settings(dict(D))["dictation"] is True
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert 'self.cfg["dictation"] = self.dictation_chk.isChecked()' in src
        assert 'Mod+Shift+D' in src and '"dictation"' in src


class TestOperator:
    """OCR-grounded clicking (Self-Operating-Computer pattern, Wayland
    edition): screen_elements scans clickable text lines via tesseract TSV,
    click_element clicks by number/text, click_at by pixel — all behind the
    operator permission (default OFF)."""

    TSV = ("level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t100\t200\t60\t20\t95\tFile\n"
           "5\t1\t1\t1\t1\t2\t170\t200\t50\t20\t95\tEdit\n"
           "5\t1\t1\t1\t2\t1\t100\t300\t80\t20\t92\tSettings\n")

    def _tb(self, H, operator=True):
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"], "operator": operator}
        tb._elements = H.ToolBelt._parse_tsv(self.TSV)
        calls = []
        tb._ydotool = lambda *args: calls.append(args) or "ok"
        return tb, calls

    def test_parse_tsv_groups_lines_and_centers(self, H):
        els = H.ToolBelt._parse_tsv(self.TSV)
        assert [e["text"] for e in els] == ["File Edit", "Settings"]
        assert els[0]["x"] == 160 and els[0]["y"] == 210
        assert els[1]["x"] == 140 and els[1]["y"] == 310

    def test_parse_tsv_drops_junk_and_low_conf(self, H):
        tsv = self.TSV + "5\t1\t2\t1\t1\t1\t0\t0\t0\t0\t-1\t~\n"
        els = H.ToolBelt._parse_tsv(tsv)
        assert len(els) == 2                      # junk row dropped
        assert H.ToolBelt._parse_tsv("garbage") == []

    def test_click_element_disabled_by_default(self, H):
        tb, _ = self._tb(H, operator=False)
        r = tb.click_element("Settings")
        assert r.startswith("REFUSED") and "operator" in r

    def test_click_element_by_text_and_number(self, H):
        tb, calls = self._tb(H)
        assert "clicked" in tb.click_element("settings")
        assert calls[0] == ("mousemove", "-a", "-x", "140", "-y", "310")
        assert calls[1] == ("click", "0xC0")
        tb.click_element("1")
        assert calls[2] == ("mousemove", "-a", "-x", "160", "-y", "210")

    def test_click_element_exact_wins_over_partial(self, H):
        tb, calls = self._tb(H)
        tb._elements = tb._elements + [{"text": "Settings page",
                                        "x": 5, "y": 5, "w": 2, "h": 2}]
        tb.click_element("Settings")
        assert calls[0][3] == "140"               # exact 'Settings', not partial

    def test_click_element_no_scan(self, H):
        tb, _ = self._tb(H)
        tb._elements = []
        assert "screen_elements" in tb.click_element("File")

    def test_click_element_no_match(self, H):
        tb, calls = self._tb(H)
        r = tb.click_element("nonexistent button")
        assert r.startswith("ERROR") and "screen_elements" in r
        assert not calls                          # nothing was clicked

    def test_click_at_and_bounds(self, H):
        tb, calls = self._tb(H)
        assert "clicked" in tb.click_at(500, 300)
        assert calls[0] == ("mousemove", "-a", "-x", "500", "-y", "300")
        assert tb.click_at(-5, 100).startswith("ERROR")
        assert tb.click_at(999999, 1).startswith("ERROR")

    def test_screen_elements_registers_and_lists(self, H, monkeypatch, tmp_path):
        tb, _ = self._tb(H)
        monkeypatch.setattr(tb, "_take_screenshot", lambda *a, **k: "")
        monkeypatch.setattr(H.ToolBelt, "SCREENSHOT_FILE", tmp_path / "s.png")
        monkeypatch.setattr(H, "subprocess", types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(
                stdout=self.TSV, returncode=0),
            TimeoutExpired=subprocess.TimeoutExpired))
        out = tb.screen_elements()
        assert "2 clickable" in out and "1. 'File Edit'" in out
        assert tb._elements and tb._elements[0]["text"] == "File Edit"

    def test_operator_tool_registered(self, H):
        reg = H.ToolBelt(on_restart_pending=lambda: None)
        names = set(reg._tool_methods().keys())
        assert {"screen_elements", "click_element", "click_at"} <= names

    def test_settings_row_exists(self, H):
        src = (HERE / "handsoff-settings.py").read_text(encoding="utf-8")
        assert '"operator":' in src
        assert H.DEFAULT_SETTINGS["permissions"]["operator"] is False


class TestWindowWaits:
    """wait_for_window: aim the typing at a window that actually exists."""

    def _belt(self, H):
        return H.ToolBelt(on_restart_pending=lambda: None)

    def test_found_reports_focus_state(self, H, monkeypatch):
        belt = self._belt(H)
        monkeypatch.setattr(H.ToolBelt, "_WIN_POLL_S", 0.01)
        wins = [{"id": 4, "app_id": "foot", "title": "term", "is_focused": True}]
        monkeypatch.setattr("subprocess.run", lambda argv, **kw: type(
            "P", (), {"returncode": 0, "stdout": json.dumps(wins),
                      "stderr": ""})())
        out, err = belt.execute("wait_for_window", {"app": "foot"})
        assert not err and "window ready: foot: term" in out
        assert "and focused" in out

    def test_found_unfocused_advises_focus(self, H, monkeypatch):
        belt = self._belt(H)
        monkeypatch.setattr(H.ToolBelt, "_WIN_POLL_S", 0.01)
        wins = [{"id": 4, "app_id": "gedit", "title": "notes", "is_focused": False}]
        monkeypatch.setattr("subprocess.run", lambda argv, **kw: type(
            "P", (), {"returncode": 0, "stdout": json.dumps(wins),
                      "stderr": ""})())
        out, err = belt.execute("wait_for_window", {"app": "gedit"})
        assert not err and "NOT focused" in out and "focus_window" in out

    def test_timeout_lists_open_windows(self, H, monkeypatch):
        belt = self._belt(H)
        monkeypatch.setattr(H.ToolBelt, "_WIN_POLL_S", 0.01)
        wins = [{"id": 1, "app_id": "foot", "title": "term", "is_focused": False}]
        monkeypatch.setattr("subprocess.run", lambda argv, **kw: type(
            "P", (), {"returncode": 0, "stdout": json.dumps(wins),
                      "stderr": ""})())
        out, err = belt.execute("wait_for_window",
                                {"app": "thunderbird", "timeout": 0.5})
        assert err and "no window matching 'thunderbird'" in out
        assert "foot: term" in out

    def test_empty_refused_and_clamped(self, H, monkeypatch):
        belt = self._belt(H)
        out, err = belt.execute("wait_for_window", {"app": " "})
        assert err and out.startswith("REFUSED")
        # a huge timeout is clamped to 30s (a runaway wait must not hang the loop)
        sleeps = []
        monkeypatch.setattr(H.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(H.ToolBelt, "_wait_window_match",
                            lambda self, q, t, ids_before=None: None)
        belt.execute("wait_for_window", {"app": "x", "timeout": 999})
        # clamping happens before the wait; verify via the clamp math directly
        assert min(max(999.0, 0.5), 30.0) == 30.0

    def test_registered(self, H):
        names = set(H.ToolBelt(on_restart_pending=lambda: None)
                    ._tool_methods().keys())
        assert {"wait_for_window", "wait", "niri_capabilities", "scroll"} <= names


class TestOpenAppIdentification:
    """open_app must wait for and identify the launched window."""

    def _run_open(self, H, monkeypatch, wins, spawn_adds=None,
                  which="/usr/bin/foot", wait=0.4):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.shutil, "which", lambda p: which)
        monkeypatch.setattr(H.ToolBelt, "_WIN_POLL_S", 0.01)
        monkeypatch.setattr(H.ToolBelt, "_WIN_GRACE_S", 0.05)
        monkeypatch.setattr(H.ToolBelt, "OPEN_APP_WAIT_S", wait)

        def fake_popen(argv, **kw):
            if spawn_adds:
                wins.extend(spawn_adds)
            return None

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        monkeypatch.setattr("subprocess.run", lambda argv, **kw: type(
            "P", (), {"returncode": 0, "stdout": json.dumps(wins),
                      "stderr": ""})())
        out, err = belt.execute("open_app", {"app": "terminal"})
        return out, err

    def test_reports_new_window(self, H, monkeypatch):
        wins = [{"id": 1, "app_id": "firefox", "title": "Docs", "is_focused": True}]
        out, err = self._run_open(
            H, monkeypatch, wins,
            spawn_adds=[{"id": 9, "app_id": "foot", "title": "term",
                         "is_focused": False}])
        assert not err
        assert "launched foot" in out and "window ready: foot: term" in out
        assert "NOT focused" in out and "focus_window" in out

    def test_prefers_name_match_over_first_new_window(self, H, monkeypatch):
        """A stray notification window may map before the app: the name
        match must win over 'any new window'."""
        wins = []
        out, err = self._run_open(
            H, monkeypatch, wins,
            spawn_adds=[{"id": 5, "app_id": "xdg-desktop-portal-gtk",
                         "title": "splash", "is_focused": False},
                        {"id": 6, "app_id": "foot", "title": "term",
                         "is_focused": False}])
        assert not err and "window ready: foot: term" in out

    def test_already_open_detected(self, H, monkeypatch):
        wins = [{"id": 3, "app_id": "foot", "title": "term", "is_focused": False}]
        out, err = self._run_open(H, monkeypatch, wins, spawn_adds=None)
        assert not err and "already open" in out and "foot: term" in out

    def test_no_window_admitted(self, H, monkeypatch):
        wins = []
        out, err = self._run_open(H, monkeypatch, wins, spawn_adds=None,
                                  wait=0.3)
        assert not err and "no new window appeared" in out

    def test_ipc_down_still_launches(self, H, monkeypatch):
        """Broken niri IPC must not stop the launch — it just downgrades
        the report (window identification unavailable)."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H.shutil, "which", lambda p: "/usr/bin/foot")
        def fake_run(argv, **kw):
            raise OSError("no niri socket")
        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: None)
        out, err = belt.execute("open_app", {"app": "foot"})
        assert not err and "launched foot" in out and "could not verify" in out


class TestTypingFocusChecks:
    """Focus is verified immediately before every keyboard injection."""

    def _belt(self, H):
        return H.ToolBelt(on_restart_pending=lambda: None)

    def test_post_typing_focus_move_warns(self, H, monkeypatch):
        belt = self._belt(H)
        focuses = [dict(app_id="firefox", title="Firefox", id=1),
                   dict(app_id="gedit", title="gedit", id=2)]
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: focuses.pop(0))
        monkeypatch.setattr(belt.__class__, "_ydotool", lambda self, *a: "ok")
        out, err = belt.execute("type_text", {"text": "hi"})
        assert not err and "typed 2" in out
        assert "WARNING: focus moved to gedit: gedit" in out

    def test_failed_type_reverifies_focus_before_retry(self, H, monkeypatch):
        """The non-ASCII retry must re-run the terminal guard: focus may
        have moved onto a terminal after the failed first attempt."""
        belt = self._belt(H)
        focuses = [dict(app_id="firefox", title="Firefox", id=1),
                   dict(app_id="foot", title="term", id=2)]
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: focuses.pop(0))
        yd = []
        def fake_ydotool(self, *args):
            yd.append(args)
            return "ERROR: ydotool failed"
        monkeypatch.setattr(belt.__class__, "_ydotool", fake_ydotool)
        out, err = belt.execute("type_text", {"text": "café"})
        assert err and out.startswith("REFUSED") and "terminal" in out
        assert len(yd) == 1              # no keys reached the terminal

    def test_type_marks_scan_stale(self, H, monkeypatch):
        belt = self._belt(H)
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: dict(app_id="firefox",
                                              title="Firefox", id=1))
        monkeypatch.setattr(belt.__class__, "_ydotool", lambda self, *a: "ok")
        belt._elements_ts = H.time.monotonic()
        belt.execute("type_text", {"text": "hi"})
        assert belt._elements_ts == 0.0

    def test_names_target_window(self, H, monkeypatch):
        belt = self._belt(H)
        monkeypatch.setattr(belt.__class__, "_focused_window_info",
                            lambda self: dict(app_id="kate",
                                              title="report.txt", id=7))
        monkeypatch.setattr(belt.__class__, "_ydotool", lambda self, *a: "ok")
        out, err = belt.execute("type_text", {"text": "hello"})
        assert not err and "into kate: report.txt" in out


class TestNiriManifest:
    """The live capability manifest: actions parsed from niri's own help."""

    HELP = ("Perform an action\n\nUsage: niri msg action <ACTION>\n\n"
            "Actions:\n"
            "  quit\n          Exit niri\n"
            "  spawn\n          Spawn a command\n"
            "  focus-window\n          Focus a window by id\n"
            "  close-window\n          Close the focused window\n"
            "\nOptions:\n  -h, --help  Print help\n")

    def test_help_names_parsed(self, H, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda argv, **kw: type(
            "P", (), {"returncode": 0, "stdout": self.HELP, "stderr": ""})())
        names = H.ToolBelt._niri_help_names(["niri", "msg", "action", "--help"],
                                            "Actions")
        assert names == ["quit", "spawn", "focus-window", "close-window"]

    def test_help_names_tolerate_missing_niri(self, H, monkeypatch):
        def boom(argv, **kw):
            raise FileNotFoundError("niri")
        monkeypatch.setattr("subprocess.run", boom)
        assert H.ToolBelt._niri_help_names(["niri"], "Actions") == []

    def test_manifest_build_and_format(self, H, monkeypatch):
        monkeypatch.setattr(H.ToolBelt, "_MANIFEST_CACHE", None)
        def fake_run(argv, **kw):
            out = ""
            if argv[-4:] == ["--json", "version"] or argv[-1] == "version":
                out = json.dumps({"compositor": "26.04 (test)"})
            elif argv[-1] == "windows":
                out = json.dumps([{"id": 1, "app_id": "foot", "title": "term",
                                   "is_focused": True}])
            elif argv[-1] == "workspaces":
                out = json.dumps([{"idx": 1}, {"idx": 2}])
            elif argv[-1] == "outputs":
                out = json.dumps({"DP-1": {"make": "M", "model": "X",
                                           "logical": {"scale": 1.25}}})
            elif argv[-1] == "focused-output":
                out = json.dumps({"name": "DP-1", "logical": {"scale": 1.25}})
            elif argv[-1] == "keyboard-layouts":
                out = json.dumps({"names": ["English (US)"], "current_idx": 0})
            elif argv[-4:] == ["niri", "msg", "action", "--help"]:
                out = self.HELP
            return type("P", (), {"returncode": 0, "stdout": out,
                                  "stderr": ""})()
        monkeypatch.setattr("subprocess.run", fake_run)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("niri_capabilities", {"refresh": True})
        assert not err
        assert "niri 26.04 (test)" in out
        assert "windows: 1" in out and "focused: foot: term" in out
        assert "workspaces: 2" in out
        assert "DP-1" in out and "scale 1.25" in out
        assert "keyboard layouts: English (US)" in out
        assert "focus-window" in out and "close-window" in out

    def test_manifest_cached(self, H, monkeypatch):
        monkeypatch.setattr(H.ToolBelt, "_MANIFEST_CACHE", None)
        calls = []
        monkeypatch.setattr(H.ToolBelt, "_build_manifest",
                            classmethod(lambda cls: calls.append(1) or {}))
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt.niri_capabilities()          # builds
        belt.niri_capabilities()          # cached
        assert len(calls) == 1
        belt.niri_capabilities(refresh=True)   # forces a rebuild
        assert len(calls) == 2

    def test_detect_pointer_scale(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr("subprocess.run", lambda argv, **kw: type(
            "P", (), {"returncode": 0,
                      "stdout": json.dumps({"name": "DP-1",
                                            "logical": {"scale": 1.25}}),
                      "stderr": ""})())
        assert belt._detect_pointer_scale() == 1.25
        def garbage(argv, **kw):
            return type("P", (), {"returncode": 0, "stdout": "not json",
                                  "stderr": ""})()
        monkeypatch.setattr("subprocess.run", garbage)
        assert belt._detect_pointer_scale() == 1.0


class TestScrollAndWait:
    """scroll: wheel via ydotool; wait: bounded sleep between actions."""

    def _belt(self, H, operator=True):
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"], "operator": operator}
        # These direct-method tests model a completed screen_elements scan;
        # click_element must still enforce the operator permission itself.
        tb._elements = [
            {"text": "File", "x": 160, "y": 210, "w": 60, "h": 20},
            {"text": "Settings", "x": 140, "y": 310, "w": 80, "h": 20},
        ]
        tb._elements_ts = H.time.monotonic()
        calls = []
        tb._ydotool = lambda *args: calls.append(args) or "ok"
        return tb, calls

    def test_scroll_directions_and_signs(self, H):
        tb, calls = self._belt(H)
        assert "scrolled down 3" in tb.scroll("down", 3)
        assert calls[-1] == ("mousemove", "-w", "-x", "0", "-y", "-3")
        tb.scroll("up")
        assert calls[-1] == ("mousemove", "-w", "-x", "0", "-y", "3")
        tb.scroll("right", 5)
        assert calls[-1] == ("mousemove", "-w", "-x", "5", "-y", "0")
        tb.scroll("left", 2)
        assert calls[-1] == ("mousemove", "-w", "-x", "-2", "-y", "0")

    def test_scroll_gates_and_bounds(self, H):
        tb, calls = self._belt(H, operator=False)
        assert tb.scroll("down").startswith("REFUSED")
        assert calls == []
        tb, calls = self._belt(H)
        assert tb.scroll("sideways").startswith("REFUSED")
        tb.scroll("down", 99)
        assert calls[-1][-1] == "-25"    # clamped to 25 notches

    def test_scroll_marks_scan_stale(self, H):
        tb, _ = self._tb_wait(H)
        tb._elements_ts = H.time.monotonic()
        tb.scroll("down")
        assert tb._elements_ts == 0.0

    def _tb_wait(self, H):
        tb = H.ToolBelt.__new__(H.ToolBelt)
        tb._perm = {**H.DEFAULT_SETTINGS["permissions"], "operator": True}
        tb._ydotool = lambda *args: "ok"
        return tb, None

    def test_wait_bounds_and_reports(self, H, monkeypatch):
        tb = H.ToolBelt(on_restart_pending=lambda: None)
        slept = []
        monkeypatch.setattr(H.time, "sleep", lambda s: slept.append(s))
        out, err = tb.execute("wait", {"seconds": 0.1})
        assert not err and "waited 0.5s" in out and slept == [0.5]
        tb.execute("wait", {"seconds": 500})
        assert slept[-1] == 30.0

    def test_click_respects_pointer_scale(self, H):
        tb, calls = self._belt(H)
        tb._pointer_scale = 2.0
        tb.click_element("Settings")
        # screen pixels (140,310) ÷ scale 2 → pointer (70,155)
        assert calls[0] == ("mousemove", "-a", "-x", "70", "-y", "155")
        assert "pointer (70,155)" in tb.click_element("Settings")

    def test_stale_scan_warns_on_click(self, H):
        tb, calls = self._belt(H)
        tb._elements_ts = H.time.monotonic() - 120
        out = tb.click_element("File")
        assert "element scan is" in out and "screen_elements" in out
        assert tb._elements_ts == 0.0    # click invalidates the scan

    def test_click_pass_through_at_scale_one(self, H):
        tb, calls = self._belt(H)
        tb._pointer_scale = 1.0
        tb.click_at(500, 300)
        assert calls[0] == ("mousemove", "-a", "-x", "500", "-y", "300")


class TestSuperBindingKnown:
    """press_hotkey's Super-chord check walks the niri config AND its
    `include "..."` files — the walk (escape, dedupe, size cap) is where a
    chord actually bound in cfg/keybinds.kdl must be found, and was the one
    branch with no test."""

    @pytest.fixture()
    def niri_home(self, monkeypatch, tmp_path):
        """_super_binding_known reads _dep().HOME/.config/niri — point that
        seam at a tmp home: a fresh ContextVar makes _dep() resolve to
        _DEFAULT_DEPS (no ToolBelt host can leak in), and the deps' HOME is
        the tmp path."""
        base = tmp_path / ".config" / "niri"
        base.mkdir(parents=True)
        var = contextvars.ContextVar("tools_deps_test")
        var.set(None)                    # mimic "no host": _dep() → _DEFAULT_DEPS
        monkeypatch.setattr(_core_tools, "_CURRENT", var)
        monkeypatch.setattr(_core_tools._DEFAULT_DEPS, "HOME", tmp_path)
        return base

    @staticmethod
    def _write_cfg(base, text, name="config.kdl"):
        cfg = base / name
        cfg.write_text(text, encoding="utf-8")
        return cfg

    def test_found_in_included_keybinds(self, niri_home):
        """The real layout: binds live in an included file, not config.kdl."""
        self._write_cfg(niri_home, '// niri config\ninclude "cfg/keybinds.kdl"\n')
        inc = niri_home / "cfg" / "keybinds.kdl"
        inc.parent.mkdir()
        inc.write_text('binds { Mod+T { spawn "foot"; } }\n', encoding="utf-8")
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is True
        assert _core_tools.ToolBelt._super_binding_known("super+t") is True

    def test_absent_from_readable_config_is_false(self, niri_home):
        """Readable config WITHOUT the chord must say False — that is the
        whole point of the check (the chord would reach the focused app)."""
        self._write_cfg(niri_home, 'binds { Mod+E { spawn "nautilus"; } }\n')
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is False
        # the comment-stripped scan must not see the chord inside a comment
        self._write_cfg(niri_home, '// binds { Mod+T; }\n')
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is False

    def test_unreadable_config_allows(self, monkeypatch, tmp_path):
        """Legacy allow: no config (or unreadable) must NOT block hotkeys."""
        base = tmp_path / ".config" / "niri"
        base.mkdir(parents=True)                      # dir exists, no config.kdl
        var = contextvars.ContextVar("tools_deps_test")
        var.set(None)                    # mimic "no host": _dep() → _DEFAULT_DEPS
        monkeypatch.setattr(_core_tools, "_CURRENT", var)
        monkeypatch.setattr(_core_tools._DEFAULT_DEPS, "HOME", tmp_path)
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is True

    def test_escape_outside_config_dir_is_refused(self, niri_home):
        """An include pointing outside ~/.config/niri must be ignored: the
        chord must not be 'found' from a file the config dir does not own."""
        self._write_cfg(niri_home, 'include "../elsewhere/keybinds.kdl"\n')
        outside = niri_home.parent / "elsewhere" / "keybinds.kdl"
        outside.parent.mkdir()
        outside.write_text("binds { Mod+T; }\n", encoding="utf-8")
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is False

    def test_a_sibling_directory_prefix_is_not_inside(self, niri_home):
        """Containment was a STRING prefix, and `/…/.config/niri-evil/x.kdl`
        starts with `/…/.config/niri` — so a sibling directory fed text into the
        chord match. It must be refused by PATH, not by prefix."""
        self._write_cfg(niri_home, 'include "../niri-evil/keybinds.kdl"\n')
        evil = niri_home.parent / "niri-evil"
        evil.mkdir()
        (evil / "keybinds.kdl").write_text("binds { Mod+T; }\n",
                                            encoding="utf-8")
        assert str(evil).startswith(str(niri_home)), \
            "test setup: the sibling is not a string-prefix of the config dir"
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is False

    def test_an_absolute_include_outside_the_config_dir_is_refused(self, niri_home):
        """An absolute include is not resolved against the base, so
        containment is the only thing standing between the walk and any file
        on the disk."""
        elsewhere = niri_home.parent.parent / "outside.kdl"
        elsewhere.write_text("binds { Mod+T; }\n", encoding="utf-8")
        self._write_cfg(niri_home, f'include "{elsewhere}"\n')
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is False

    def test_include_cycle_terminates(self, niri_home):
        """Two files including each other must not hang or crash — the
        dedupe (`seen`) stops a cycle from re-reading a file forever."""
        self._write_cfg(niri_home, 'include "a.kdl"\ninclude "final.kdl"\n')
        (niri_home / "a.kdl").write_text('include "b.kdl"\n', encoding="utf-8")
        (niri_home / "b.kdl").write_text('include "a.kdl"\n', encoding="utf-8")
        (niri_home / "final.kdl").write_text("binds { Mod+T; }\n",
                                             encoding="utf-8")
        # terminates (no hang) AND the dedupe must have left room: final.kdl
        # is a second top-level include, so a walk that re-follows the a/b
        # cycle forever would exhaust the 20-slot budget before reaching it.
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is True

    def test_include_walk_is_capped_at_twenty(self, niri_home):
        """The walk follows EXACTLY 20 includes — bounded, so a hostile
        config cannot turn one lookup into a crawl. Pinned from both sides:
        the chord in the 20th included file IS found, in the 21st is NOT —
        an off-by-one or a removed cap goes red either way."""
        n = 25
        incs = " ".join(f'include "k{i:02d}.kdl"' for i in range(n))
        self._write_cfg(niri_home, incs + "\n")
        for i in range(n):
            marker = "Mod+T" if i == 19 else ("Mod+Z" if i == 20 else "x")
            (niri_home / f"k{i:02d}.kdl").write_text(
                f"binds {{ {marker}; }}\n", encoding="utf-8")
        # k19 is the 20th include: found. k20 would be the 21st: not.
        assert _core_tools.ToolBelt._super_binding_known("mod+t") is True
        assert _core_tools.ToolBelt._super_binding_known("mod+z") is False

    @pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0,
                        reason="root reads anything; unreadable-file path unreachable")
    def test_unreadable_include_is_skipped(self, niri_home):
        """An include that cannot be READ is skipped, not fatal — the walk
        continues over it."""
        self._write_cfg(niri_home, 'include "locked.kdl"\n')
        locked = niri_home / "locked.kdl"
        locked.write_text("binds { Mod+T; }\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            if os.access(locked, os.R_OK):      # e.g. ACLs override the mode
                pytest.skip("file still readable")
            assert _core_tools.ToolBelt._super_binding_known("mod+t") is False
        finally:
            locked.chmod(0o644)                 # tmp_path cleanup can rmtree

    def test_readable_later_include_is_still_followed(self, niri_home):
        """A skipped include must not ABORT the walk: an unreadable file is
        passed over and a later, readable include is still followed."""
        self._write_cfg(niri_home,
                        'include "locked.kdl"\ninclude "fine.kdl"\n')
        locked = niri_home / "locked.kdl"
        locked.write_text("binds { Mod+Q; }\n", encoding="utf-8")
        locked.chmod(0o000)
        (niri_home / "fine.kdl").write_text("binds { Mod+T; }\n",
                                            encoding="utf-8")
        try:
            if os.access(locked, os.R_OK):
                pytest.skip("file still readable")
            assert _core_tools.ToolBelt._super_binding_known("mod+t") is True
        finally:
            locked.chmod(0o644)                 # tmp_path cleanup can rmtree
