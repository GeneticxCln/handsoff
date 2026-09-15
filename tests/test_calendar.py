"""Calendar and reminder tests: ICS parsing, overrides, snooze."""
from __future__ import annotations

import base64
import importlib.util
import inspect
from collections import deque
import threading
import io
import json
import logging
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

from conftest import (HERE as ROOT, _load, _user_site, core_module, pin_offer,
                      wait_for)

from core import calendar as _core_calendar

# Resolved on first use, inside the sandbox (a direct `from core import tools`
# would bake the developer's real user dirs in at collection time).
_core_tools = core_module("tools")

HERE = ROOT   # the repo root (conftest resolves it from conftest.py's parent)


class TestRemindersAndCalendar:
    """Persisted reminders + calendar_month (added 2026-09)."""

    def test_reminder_hhmm_rolls_to_tomorrow(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder",
                                {"wake_name": "pills", "when_due": "23:59"})
        assert not err, out
        due = H.json.loads(rf.read_text())[0]["due"]
        assert 0 < due - H.time.time() <= 86400

    def test_reminder_full_datetime_and_weekday(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder",
                                {"wake_name": "x", "when_due": "2027-03-05 09:30"})
        assert not err and "Friday" in out, out   # 5 Mar 2027 is a Friday

    def test_reminder_rejects_past_garbage_and_far_future(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("set_reminder",
                                {"wake_name": "y", "when_due": "2020-01-01 10:00"})
        assert err and "past" in out, out
        out, err = belt.execute("set_reminder",
                                {"wake_name": "z", "when_due": "soonish"})
        assert err and "could not understand" in out, out
        out, err = belt.execute("set_reminder",
                                {"wake_name": "far", "when_due": "in 400 days"})
        assert err and "year ahead" in out, out
        assert not rf.exists()  # nothing persisted from failed attempts

    def test_list_and_cancel_with_prefix_and_ambiguity(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt.execute("set_reminder", {"wake_name": "call mum", "when_due": "in 1 hour"})
        belt.execute("set_reminder", {"wake_name": "call dad", "when_due": "in 2 hours"})
        out, err = belt.execute("list_reminders", {})
        assert not err and "call mum" in out and "call dad" in out, out
        out, err = belt.execute("cancel_reminder", {"name": "call"})
        assert not err and "several match" in out, out
        out, err = belt.execute("cancel_reminder", {"name": "call mum"})
        assert not err and "cancelled" in out, out
        assert [r["name"] for r in H.json.loads(rf.read_text())] == ["call dad"]
        out, err = belt.execute("cancel_reminder", {"name": "nope"})
        assert not err and "no reminder" in out, out

    def test_due_reminders_repeat_advance_no_backlog(self, H):
        now = H.time.time()
        fired, kept = H._due_reminders(
            [{"name": "r", "due": now - 3600, "repeat_hours": 1},
             {"name": "one", "due": now - 10, "repeat_hours": 0}], now)
        assert [f["name"] for f in fired] == ["r", "one"]
        assert [k["name"] for k in kept] == ["r"]
        assert 0 < kept[0]["due"] - now <= 3600   # next step, never a backlog

    def test_take_missed_reminders_persists_prune(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        now = H.time.time()
        rf.write_text(H.json.dumps(
            [{"name": "old", "due": now - 60, "repeat_hours": 0},
             {"name": "future", "due": now + 600, "repeat_hours": 0}]))
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        missed = H._take_missed_reminders()
        assert [m["name"] for m in missed] == ["old"]
        assert [r["name"] for r in H.json.loads(rf.read_text())] == ["future"]

    def test_a_startup_store_failure_does_not_kill_the_bubble(
            self, H, monkeypatch, caplog):
        """take_missed catches the prune-save OSError, but a failure BEFORE it
        (an unreadable reminders.json, a wedged sidecar flock, anything else
        the store does not convert to []) used to propagate out of start() —
        which main() calls before the first turn, so the whole bubble died at
        startup over a reminder. The worker path has wrapped the same store in
        try/except for exactly this reason; the startup twin does now too, and
        the person is told once instead of the process vanishing.
        """
        class Boom:
            def take_missed(self):
                raise RuntimeError("flock sidecar wedged")

        monkeypatch.setattr(H, "_reminder_store", lambda: Boom())
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = False          # start() reads it before the reminder path
        a._failures_reported = set()
        told = []
        monkeypatch.setattr(a, "_report_once",
                            lambda kind, exc, msg: told.append(msg) or True)
        started = []
        monkeypatch.setattr(H.Assistant, "_start_worker",
                            lambda self, target, args=(), name="w":
                            started.append(name))
        monkeypatch.setattr(H.Assistant, "_is_closed", lambda self: False)
        monkeypatch.setattr(H.Assistant, "_loader", lambda self: None)
        monkeypatch.setattr(H.Assistant, "_reminder_worker", lambda self: None)
        monkeypatch.setattr(H.Assistant,
                            "_settings_watch_worker", lambda self: None)
        monkeypatch.setattr(H, "_load_mic_events", lambda *a, **k: [])
        with caplog.at_level(logging.ERROR):
            a.start()          # must NOT raise
        assert "missed-reminders" not in started, started
        assert "reminders" in started and "settings-watch" in started, started
        assert told and "reminders are unavailable" in told[0], told
        assert any("startup reminders unavailable" in r.getMessage()
                   for r in caplog.records)

    def test_a_healthy_startup_still_announces_missed(self, H, monkeypatch,
                                                      tmp_path):
        rf = tmp_path / "reminders.json"
        rf.write_text(H.json.dumps(
            [{"name": "old", "due": H.time.time() - 60, "repeat_hours": 0}]))
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        a = H.Assistant.__new__(H.Assistant)
        a._handsfree = False          # start() reads it before the reminder path
        started = []
        monkeypatch.setattr(H.Assistant, "_start_worker",
                            lambda self, target, args=(), name="w":
                            started.append((name, args)))
        monkeypatch.setattr(H.Assistant, "_is_closed", lambda self: False)
        monkeypatch.setattr(H.Assistant, "_loader", lambda self: None)
        monkeypatch.setattr(H.Assistant, "_reminder_worker", lambda self: None)
        monkeypatch.setattr(H.Assistant,
                            "_settings_watch_worker", lambda self: None)
        monkeypatch.setattr(H, "_load_mic_events", lambda *a, **k: [])
        a.start()
        missed = [args for name, args in started if name == "missed-reminders"]
        assert missed and missed[0][0][0]["name"] == "old", started

    def test_calendar_month_grid_and_errors(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("calendar_month", {})
        assert not err, out
        lines = out.splitlines()
        assert lines[1].split() == ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
        today = H.datetime.date.today()
        assert f"{today.day:2d}*" in out
        out2, err = belt.execute("calendar_month", {"month": "2024-12"})
        assert not err and "December" in out2, out2
        assert out2.splitlines()[2].endswith(" 1")  # 1 Dec 2024 was a Sunday
        out3, err = belt.execute("calendar_month", {"month": "garbage"})
        assert err and "ERROR" in out3, out3
        out4, err = belt.execute("calendar_month", {"month": "2026-13"})
        assert err and "ERROR" in out4, out4

    def test_parse_duration_variants(self, H):
        assert H._parse_duration("in 90 minutes") == 5400
        assert H._parse_duration("2 hours 5 minutes") == 2 * 3600 + 5 * 60
        assert H._parse_duration("a week") == 604800
        assert H._parse_duration("45 min") == 2700
        assert H._parse_duration("3 days") == 3 * 86400
        assert H._parse_duration("bananas") is None
        assert H._parse_duration("in 0 minutes") is None

    def test_prompt_documents_reminders(self, H):
        assert "set_reminder" in H.SYSTEM_PROMPT
        assert "calendar_month" in H.SYSTEM_PROMPT
        assert "set_timer" not in H.SYSTEM_PROMPT


class TestCalendarICS:
    """ICS calendar reading: parser, tool, refusals, briefing summary."""

    @staticmethod
    def _make_ics(H):
        """Build a fixture with dynamic dates so it never rots."""
        today = H.datetime.date.today()
        tmr = today + H.datetime.timedelta(days=1)
        d = lambda dt: dt.strftime("%Y%m%d")
        return "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0",
            # floating local 09:00 today, daily recurrence
            "BEGIN:VEVENT",
            f"DTSTART:{d(today)}T090000",
            f"DTEND:{d(today)}T093000",
            "SUMMARY:Team sync", "LOCATION:Teams",
            "RRULE:FREQ=DAILY;INTERVAL=1",
            "END:VEVENT",
            # folded summary, floating 14:00 today
            "BEGIN:VEVENT",
            f"DTSTART:{d(today)}T140000",
            f"DTEND:{d(today)}T143000",
            "SUMMARY:Folded long\r\n  title here",
            "END:VEVENT",
            # TZID event today (time may shift if run outside Berlin; presence-only)
            "BEGIN:VEVENT",
            f"DTSTART;TZID=Europe/Berlin:{d(today)}T080000",
            f"DTEND;TZID=Europe/Berlin:{d(today)}T083000",
            "SUMMARY:TZ event",
            "END:VEVENT",
            # tomorrow 18:30 UTC (Z form)
            "BEGIN:VEVENT",
            f"DTSTART:{d(tmr)}T183000Z",
            f"DTEND:{d(tmr)}T193000Z",
            "SUMMARY:Gym with Max",
            "END:VEVENT",
            # all-day tomorrow
            "BEGIN:VEVENT",
            f"DTSTART;VALUE=DATE:{d(tmr)}",
            "SUMMARY:Entry deadline",
            "END:VEVENT",
            # every 2 days starting yesterday: fires tomorrow, NOT today
            "BEGIN:VEVENT",
            f"DTSTART:{d(today - H.datetime.timedelta(days=1))}T120000",
            "SUMMARY:Bi-daily", "RRULE:FREQ=DAILY;INTERVAL=2",
            "END:VEVENT",
            "END:VCALENDAR", "",
        ])

    def test_read_calendar_refuses_a_boolean_window(self, H, monkeypatch,
                                                    tmp_path):
        """`int(True)` is 1: a model that answered `days: true` got a silent
        one-day window. A boolean is not a number here — refused at the
        schema's argument coercion, and again inside the tool for any caller
        that reaches it without the schema."""
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_calendar", {"days": True})
        assert err and "expected a number" in out, (out, err)
        # Direct method call: no schema in the way, so the tool's own guard.
        out = belt.read_calendar(True)
        assert "must be a number" in out, out

    def test_read_calendar_today(self, H, monkeypatch, tmp_path):
        ics = tmp_path / "cal.ics"
        ics.write_text(self._make_ics(H), encoding="utf-8")
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", [str(ics)])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_calendar", {"days": 1})
        assert not err, out
        assert "Team sync" in out and "Teams" in out
        assert "09:00" in out and "09:30" in out          # floating local
        assert "Folded long title here" in out            # RFC unfold
        assert "TZ event" in out                          # TZID path
        assert "Bi-daily" not in out                      # recurrence skips today
        assert "Gym" not in out                           # tomorrow out of window

    def test_read_calendar_multiday_all_day(self, H, monkeypatch, tmp_path):
        ics = tmp_path / "cal.ics"
        ics.write_text(self._make_ics(H), encoding="utf-8")
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", [str(ics)])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_calendar", {"days": 2})
        assert not err, out
        assert "Gym with Max" in out and "Entry deadline" in out
        assert "all day" in out

    def test_no_calendar_configured(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", [])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_calendar", {"days": 1})
        assert not err and "no calendar is configured" in out, out

    def test_url_source_needs_web_access(self, H, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "calendar_ics",
                            ["https://example.com/cal.ics"])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt._perm["web_access"] = False
        out, err = belt.execute("read_calendar", {"days": 1})
        assert err and "REFUSED" in out and "web_access" in out, out

    def test_unreadable_source_reports_honestly(self, H, monkeypatch, tmp_path):
        monkeypatch.setitem(H.SETTINGS, "calendar_ics",
                            [str(tmp_path / "missing.ics")])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_calendar", {"days": 1})
        assert err and "could not read calendar source" in out, out

    def test_briefing_summary_uses_local_files(self, H, monkeypatch, tmp_path):
        ics = tmp_path / "cal.ics"
        ics.write_text(self._make_ics(H), encoding="utf-8")
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", [str(ics)])
        assert "Team sync" in H._today_events_summary()
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", [])
        assert H._today_events_summary() == ""

    def test_calendar_ics_coercion_string_to_list(self, H, monkeypatch, tmp_path):
        cfg = tmp_path / "settings.json"
        cfg.write_text(H.json.dumps(
            {"calendar_ics": "a.ics, https://x/y.ics"}), encoding="utf-8")
        monkeypatch.setattr(H, "SETTINGS_FILE", cfg)
        assert H._load_settings()["calendar_ics"] == ["a.ics", "https://x/y.ics"]

    def test_prompt_documents_read_calendar(self, H):
        assert "read_calendar" in H.SYSTEM_PROMPT
        assert "never invent events" in H.SYSTEM_PROMPT


class TestSnooze:
    """Snooze: offer window, fast-path parser, re-arm persistence."""

    def test_snooze_reminder_tool_rearms(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt.execute("set_reminder", {"wake_name": "tea", "when_due": "in 1 hour"})
        out, err = belt.execute("snooze_reminder", {"name": "tea", "minutes": 15})
        assert not err and "snoozed until" in out, out
        due = H.json.loads(rf.read_text())[0]["due"]
        assert 14.5 * 60 < due - H.time.time() <= 15.5 * 60

    # The offers are core.registry.Offer objects now: arm(window) replaces
    # clear()+update() in one step under the offer's own lock, and state()
    # applies the deadline itself.

    def test_snooze_after_fire_within_window(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        pin_offer(H, monkeypatch, "snooze")     # one offer, on _dep()'s path
        H._snooze_offer.arm(90, name="tea")
        out, err = belt.execute("snooze_reminder", {"name": "tea", "minutes": 5})
        assert not err and "snoozed until" in out, out   # re-armed although pruned
        assert H.json.loads(rf.read_text())[0]["name"] == "tea"

    def test_snooze_expired_window_rejected(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        pin_offer(H, monkeypatch, "snooze")
        H._snooze_offer.arm(-1, name="old")      # armed, window already closed
        out, err = belt.execute("snooze_reminder", {"name": "old", "minutes": 5})
        assert not err and "no reminder matching" in out, out

    def test_snooze_bounds_and_ambiguity(self, H, monkeypatch, tmp_path):
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("snooze_reminder", {"name": "x", "minutes": 99999})
        assert err and "between" in out, out
        out, err = belt.execute("snooze_reminder", {"name": "x", "minutes": "abc"})
        # junk used to be coerced to 0, which then produced a confusing bounds
        # error; a malformed argument is now reported as exactly that
        assert err and "bad arguments" in out and "abc" in out, out
        belt.execute("set_reminder", {"wake_name": "alpha one", "when_due": "in 1 hour"})
        belt.execute("set_reminder", {"wake_name": "alpha two", "when_due": "in 2 hours"})
        out, err = belt.execute("snooze_reminder", {"name": "alpha", "minutes": 5})
        assert not err and "several match" in out, out

    def test_fire_timer_opens_window_only_for_oneoffs(self, H):
        a = H.Assistant.__new__(H.Assistant)
        a.interrupt = lambda: None
        a._gen = 0
        a._speak = lambda *x, **k: None
        a._set = lambda *x: None
        H._snooze_offer.clear()
        H.Assistant._fire_timer(a, "tea", 0)
        assert H._snooze_offer["name"] == "tea"
        H.Assistant._fire_timer(a, "rep", 24)
        assert H._snooze_offer["name"] == "tea"   # repeating: no window
        assert H._snooze_offer["until"] > H.time.monotonic()

    def test_try_snooze_fastpath_consumes_utterance(self, H, monkeypatch):
        calls = {}
        H._snooze_offer.arm(90, name="tea")
        a = H.Assistant.__new__(H.Assistant)
        a._tools = H.ToolBelt(on_restart_pending=lambda: None)
        a._tools.execute = lambda name, args: _core_tools.ToolResult("snoozed!")
        a._set = lambda *x: None
        a._speak = lambda *x, **k: calls.setdefault("spoken", True)
        assert a._try_snooze("snooze 5 minutes", 1, H.threading.Event()) is True
        assert wait_for(lambda: calls.get("spoken")), \
            "snooze confirmation was not spoken"
        assert not H._snooze_offer, "offer window must close after use"

    def test_a_refused_snooze_keeps_the_offer_armed(self, H):
        """The offer is the only thing that can re-arm a fired one-off, so it
        may close on a snooze that HAPPENED and not on one that was refused.

        The refusal here is phrased WITHOUT the `ERROR:`/`REFUSED:` prefix on
        purpose: that is the case the text-based read got wrong. A failure
        whose wording does not happen to match a grep reads as success to a
        caller sniffing the string, and the user's reminder is dropped.
        """
        H._snooze_offer.arm(90, name="tea")
        a = H.Assistant.__new__(H.Assistant)
        a._tools = H.ToolBelt(on_restart_pending=lambda: None)
        a._tools.execute = lambda name, args: _core_tools.ToolResult(
            "no reminder matching 'tea' — it may have already been pruned",
            "error")
        a._set = lambda *x: None
        a._speak = lambda *x, **k: None
        assert a._try_snooze("snooze 5 minutes", 1, H.threading.Event()) is True
        assert H._snooze_offer, "a refused snooze must leave the offer armed"

    def test_try_snooze_ignores_normal_speech(self, H):
        H._snooze_offer.arm(90, name="tea")
        a = H.Assistant.__new__(H.Assistant)
        assert a._try_snooze("what's the weather", 1, H.threading.Event()) is False


class TestICSOverrides:
    """EXDATE + RECURRENCE-ID handling (audit 'calendar completeness' gap)."""

    def _override_ics(self, H):
        """Daily 10:00 master with: EXDATE today, CANCELLED override tomorrow,
        MOVED override day+2 (new time + title)."""
        today = H.datetime.date.today()
        d = lambda dt: dt.strftime("%Y%m%d")
        return "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0",
            "BEGIN:VEVENT", "UID:uid-1@test",
            f"DTSTART:{d(today)}T100000", f"DTEND:{d(today)}T103000",
            "SUMMARY:Standup", "RRULE:FREQ=DAILY",
            f"EXDATE:{d(today)}T100000",
            "END:VEVENT",
            "BEGIN:VEVENT", "UID:uid-1@test",
            f"RECURRENCE-ID:{d(today + H.datetime.timedelta(days=1))}T100000",
            f"DTSTART:{d(today + H.datetime.timedelta(days=1))}T100000",
            "STATUS:CANCELLED", "SUMMARY:Standup",
            "END:VEVENT",
            "BEGIN:VEVENT", "UID:uid-1@test",
            f"RECURRENCE-ID:{d(today + H.datetime.timedelta(days=2))}T100000",
            f"DTSTART:{d(today + H.datetime.timedelta(days=2))}T150000",
            f"DTEND:{d(today + H.datetime.timedelta(days=2))}T160000",
            "SUMMARY:Standup (moved)",
            "END:VEVENT",
            "END:VCALENDAR",
        ])

    def test_exdate_and_recurrence_overrides(self, H):
        text = self._override_ics(H)
        win_s = H.datetime.datetime.now().replace(hour=0, minute=0,
                                                  second=0, microsecond=0)
        win_e = win_s + H.datetime.timedelta(days=3)
        ev = _core_calendar._ics_events_from_text(text, win_s, win_e)
        starts = {(e["start"].strftime("%d%H"), e["summary"]) for e in ev}
        today = H.datetime.date.today()
        # EXDATE suppressed today's instance
        assert not any(s.startswith(today.strftime("%d"))
                       for s, _ in starts), starts
        # STATUS:CANCELLED override suppressed tomorrow's instance
        tmr = (today + H.datetime.timedelta(days=1)).strftime("%d")
        assert not any(s.startswith(tmr) for s, _ in starts), starts
        # moved override surfaces at its NEW time with the new title...
        day2 = (today + H.datetime.timedelta(days=2)).strftime("%d")
        assert any(s.startswith(day2 + "15") and t == "Standup (moved)"
                   for s, t in starts), starts
        # ...and the original 10:00 slot is suppressed
        assert not any(s.startswith(day2 + "10") for s, _ in starts), starts

    def test_read_calendar_tool_reports_cancellation(self, H, monkeypatch,
                                                     tmp_path):
        ics = tmp_path / "cal.ics"
        ics.write_text(self._override_ics(H), encoding="utf-8")
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", [str(ics)])
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_calendar", {"days": 3})
        assert not err, out
        assert "Standup (moved)" in out
        # the cancelled day shows nothing at 10:00 for that date
        tmr = (H.datetime.date.today()
               + H.datetime.timedelta(days=1)).strftime("%d")
        for line in out.splitlines():
            if line.strip().startswith(tmr):
                assert "10:00" not in line, line


class TestICSDurationAndUntil:
    """Two RFC 5545 fields the parser used to ignore silently."""

    def _event(self, H, body: list[str], days: int = 2):
        text = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0",
                            "BEGIN:VEVENT", "UID:u@test", *body,
                            "END:VEVENT", "END:VCALENDAR"])
        win_s = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        return _core_calendar._ics_events_from_text(
            text, win_s, win_s + H.datetime.timedelta(days=days))

    def test_duration_replaces_the_fabricated_hour(self, H):
        """A VEVENT may carry DURATION INSTEAD of DTEND. Ignoring it gave every
        such event a made-up one-hour length, so a three-hour shift was
        reported as an hour long."""
        today = H.datetime.date.today().strftime("%Y%m%d")
        ev = self._event(H, [f"DTSTART:{today}T090000",
                             "DURATION:PT3H", "SUMMARY:Shift"])
        assert len(ev) == 1, ev
        assert ev[0]["dur"] == H.datetime.timedelta(hours=3)

    def test_duration_decides_the_window_overlap(self, H):
        """The length is what the range query is answered from: an event whose
        REAL duration ends before the window opened was reported anyway when
        the duration was guessed at an hour."""
        # YESTERDAY 02:30 + 30 min: ended long before today's window opened.
        yesterday = (H.datetime.date.today()
                     - H.datetime.timedelta(days=1)).strftime("%Y%m%d")
        ev = self._event(H, [f"DTSTART:{yesterday}T023000",
                             "DURATION:PT30M", "SUMMARY:Done and dusted"])
        assert ev == [], f"an event that ended before the window surfaced: {ev}"

    def test_a_negative_duration_is_refused_not_flipped_positive(self, H):
        """RFC 5545's leading sign means "this duration points BACKWARDS", and
        the chunk parser never saw the sign: "P-1D" came back as +1 day and
        "PT-5M" as +5 minutes, so a reminder-style negative duration widened the
        overlap window instead of shrinking it."""
        for bad in ("P-1D", "PT-5M", "-PT5M", "P1DT+2H", "PT+30M"):
            assert _core_calendar._ics_duration(bad) is None, bad

    def test_fractions_are_rfc_legal_and_parsed_exactly(self, H):
        """ISO 8601 allows a decimal on the smallest component (`PT0.5H` is
        thirty minutes); RFC 5545's own ABNF has none, but generators emit
        both — and the old chunk regex skipped the `.`, so `PT0.5H` parsed as
        `5H` (FIVE HOURS) and `PT1H30M15.5S` silently lost 10.5 seconds.
        A 30-minute meeting covered a 5-hour overlap window and events
        appeared on the wrong day.
        """
        from datetime import timedelta
        assert _core_calendar._ics_duration("PT0.5H") == timedelta(minutes=30)
        assert _core_calendar._ics_duration("PT1H30M15.5S") == timedelta(
            hours=1, minutes=30, seconds=15.5)
        assert _core_calendar._ics_duration("P0.5W") == timedelta(days=3.5)
        # malformed decimals refuse the whole value rather than parsing past
        for bad in ("PT1H.5M",      # a dot between chunks
                    "PT0.5.5H",     # two dots in one number
                    "PT.5H",        # a fraction with no integer part
                    "PT1HM",        # a letter with no number
                    "P1.DD"):       # junk after a decimal
            assert _core_calendar._ics_duration(bad) is None, bad
        today = H.datetime.date.today().strftime("%Y%m%d")
        ev = self._event(H, [f"DTSTART:{today}T090000",
                             "DURATION:P-1D", "SUMMARY:Reminder"])
        assert len(ev) == 1, ev
        # Refused, so the length falls back to the fabricated hour — NOT to the
        # +1 day a sign-blind parser produced.
        assert ev[0]["dur"] == H.datetime.timedelta(hours=1), ev[0]["dur"]

    def test_date_only_until_includes_that_whole_day(self, H):
        """A DATE-valued UNTIL parses to that day's MIDNIGHT, which then
        excluded same-day instances starting later — the last occurrence of a
        recurrence silently vanished (RFC 5545: UNTIL is inclusive)."""
        start = H.datetime.datetime.now().replace(
            hour=9, minute=0, second=0, microsecond=0)
        d = lambda dt: dt.strftime("%Y%m%d")
        # three days of instances, UNTIL is the LAST day as a bare date
        ev = self._event(H, [
            f"DTSTART:{start.strftime('%Y%m%d')}T090000",
            f"RRULE:FREQ=DAILY;UNTIL={d(start + H.datetime.timedelta(days=2))}",
            "SUMMARY:Standup"], days=4)
        days_seen = sorted(e["start"].day for e in ev)
        assert len(ev) == 3, [e["start"] for e in ev]
        assert days_seen[-1] == (start + H.datetime.timedelta(days=2)).day
        # the day AFTER the UNTIL date must still be excluded
        assert (start + H.datetime.timedelta(days=3)).day not in days_seen

    def test_a_plain_timed_until_is_unchanged(self, H):
        """The fix must not widen a DATE-TIME UNTIL, which was already exact."""
        start = H.datetime.datetime.now().replace(
            hour=9, minute=0, second=0, microsecond=0)
        stop = start + H.datetime.timedelta(days=1)
        ev = self._event(H, [
            f"DTSTART:{start.strftime('%Y%m%d')}T090000",
            "RRULE:FREQ=DAILY;UNTIL=" + stop.strftime("%Y%m%dT%H%M%S"),
            "SUMMARY:Standup"], days=4)
        assert len(ev) == 2, [e["start"] for e in ev]


class TestICSMonthlyYearly:
    """MONTHLY/YEARLY RRULE expansion (handsoff.py _ics_expand_rrule)."""

    def _events(self, H, dtstart, rrule, win_s, win_e, summary="Ev"):
        text = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0",
            "BEGIN:VEVENT", "UID:uid-m@test",
            f"DTSTART:{dtstart}", f"DTEND:{dtstart[:8]}T110000",
            f"SUMMARY:{summary}", f"RRULE:{rrule}",
            "END:VEVENT",
            "END:VCALENDAR",
        ])
        ws = H.datetime.datetime(*win_s)
        we = H.datetime.datetime(*win_e)
        return _core_calendar._ics_events_from_text(text, ws, we)

    def test_monthly_nth_weekday(self, H):
        # 2nd Tuesday: Jan 13 / Feb 10 / Mar 10 / Apr 14 2026
        ev = self._events(H, "20260113T100000", "FREQ=MONTHLY;BYDAY=2TU",
                          (2026, 1, 1), (2026, 5, 1))
        assert [(e["start"].month, e["start"].day) for e in ev] == [
            (1, 13), (2, 10), (3, 10), (4, 14)]

    def test_monthly_bymonthday(self, H):
        ev = self._events(H, "20260115T100000", "FREQ=MONTHLY;BYMONTHDAY=15",
                          (2026, 1, 1), (2026, 4, 16))
        assert [(e["start"].month, e["start"].day) for e in ev] == [
            (1, 15), (2, 15), (3, 15), (4, 15)]

    def test_yearly_bymonth(self, H):
        ev = self._events(H, "20260115T100000", "FREQ=YEARLY;BYMONTH=1,7",
                          (2026, 1, 1), (2027, 1, 1))
        assert [(e["start"].month, e["start"].day) for e in ev] == [
            (1, 15), (7, 15)]

    def test_until_bounds(self, H):
        # UNTIL is inclusive: Jan 1-3 only, nothing after
        ev = self._events(H, "20260101T100000",
                          "FREQ=DAILY;UNTIL=20260103T100000",
                          (2026, 1, 1), (2026, 1, 10))
        assert [e["start"].day for e in ev] == [1, 2, 3]


class TestCalendarSourceSafety:
    """A Google-style "secret iCal address" is a bearer credential: whoever
    reads the URL reads the whole calendar (where the user is and who they
    meet). The settings row has always advertised https, so that is what is
    enforced — and an error message must not become the leak."""

    def _belt(self, H, monkeypatch, sources):
        monkeypatch.setitem(H.SETTINGS, "calendar_ics", sources)
        return H.ToolBelt(on_restart_pending=lambda: None)

    def test_cleartext_http_source_is_refused(self, H, monkeypatch):
        belt = self._belt(H, monkeypatch,
                          ["http://calendar.example.com/ical/me/private-TOKEN/basic.ics"])
        out, err = belt.execute("read_calendar", {"days": 1})
        assert err, out
        assert "https://" in out, out

    def test_the_token_never_enters_the_transcript(self, H, monkeypatch):
        belt = self._belt(H, monkeypatch,
                          ["http://calendar.example.com/ical/me/private-TOKEN/basic.ics"])
        out, _err = belt.execute("read_calendar", {"days": 1})
        assert "private-TOKEN" not in out, out

    def test_loopback_and_https_stay_allowed(self, H):
        for ok in ("https://example.com/cal.ics",
                   "http://localhost:8080/cal.ics",
                   "http://127.0.0.1/cal.ics",
                   "http://127.0.0.1:5232/cal.ics",
                   "http://[::1]:8080/cal.ics",
                   "/tmp/cal.ics",
                   "~/.cal/cal.ics"):
            assert _core_calendar._ics_scheme_error(ok) is None, ok

    def test_offending_http_names_the_problem(self, H):
        why = _core_calendar._ics_scheme_error("http://calendar.example.com/secret/basic.ics")
        assert why and "https://" in why, why
        # a user:pass@ prefix must not fool the loopback check either
        assert _core_calendar._ics_scheme_error("http://evil.example.com@127.0.0.1/cal.ics") is None

    def test_error_label_redacts_the_url(self, H):
        label = _core_calendar._ics_source_label(
            "https://calendar.google.com/calendar/ical/me%40x/private-ABC123/basic.ics")
        assert "private-ABC123" not in label and "me%40x" not in label, label
        assert label.startswith("https://calendar.google.com/"), label
        assert _core_calendar._ics_source_label("/tmp/cal.ics") == "/tmp/cal.ics"
