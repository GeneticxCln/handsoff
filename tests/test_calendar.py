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

    def test_a_non_finite_repeat_is_bad_arguments_and_saves_nothing(
            self, H, monkeypatch, tmp_path):
        """One junk argument must not reach the disk, and must be NAMED as the
        argument it is.

        `float()` accepts "nan", and NaN then defeats every ordinary range
        check — `nan < lo` and `nan > hi` are both False, so set_reminder's
        `if repeat and (repeat < lo or repeat > hi)` waved it through, saved
        `repeat_hours: NaN`, and only then raised while formatting the
        confirmation (measured 2026-09-27). The result outlived the call: NaN
        compares False against 0, so the reminder fired once and never again,
        bare `NaN` is not valid JSON, and list_reminders — which shows EVERY
        reminder — died on it too, reporting "that is a bug in the tool" about
        what was really a junk argument.
        """
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        for junk in ("nan", "NaN", "inf", "-inf", "1e400"):
            out, err = belt.execute("set_reminder",
                                    {"wake_name": "standup",
                                     "when_due": "in 5 minutes",
                                     "repeat_hours": junk})
            assert err, junk
            # named as the ARGUMENT, not as a bug in the tool
            assert "bad arguments" in out and "bug in the tool" not in out, out
        # nothing was written at all, so there is no corrupt value to inherit
        assert not rf.exists()
        # and the binder is where it is caught, for every float tool arg
        for junk in ("nan", "inf"):
            for name, args in (("pomodoro", {"action": "start",
                                             "work_minutes": junk}),
                               ("snooze_reminder", {"name": "x",
                                                    "minutes": junk}),
                               ("wait", {"seconds": junk})):
                res = belt.execute(name, args)
                assert res.kind == "error" and "bad arguments" in res.text, (junk, res)

    def test_the_repeat_range_check_catches_a_non_finite_float_by_itself(
            self, H, monkeypatch, tmp_path):
        """The tool's OWN range check, reached the way a second caller reaches
        it: directly, not through the binder that now rejects non-finite floats.

        The binder is the primary guard, so the range check's own NaN-safety was
        unpinned — reverting it to `repeat and (repeat < lo or repeat > hi)`
        changed no test, because NaN never got that far. Two guards that are
        never both exercised is one guard with a comment. This calls the method
        with a NaN float, which is what a store-loaded or in-process caller
        hands it, and requires the chained comparison to catch it.
        """
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        for junk in (float("nan"), float("inf"), float("-inf")):
            out = belt.set_reminder("standup", "in 5 minutes", junk)
            assert out.startswith("ERROR: repeat_hours must be"), (junk, out)
        assert not rf.exists()   # a refused range must not reach the disk

    def test_a_confirmation_that_cannot_be_built_saves_nothing(
            self, H, monkeypatch, tmp_path):
        """The reply is built BEFORE the store is written, so a failure while
        building it leaves the queue untouched.

        It used to be built after. The store was written, then the sentence was
        formatted, then the exception went back to the model as a tool bug —
        so the queue and the person were told two different things about one
        call (measured 2026-09-27: a non-finite repeat persisted, _fmt_dur
        raised, and the tool reported "that is a bug in the tool, not a
        refusal"). Whatever raises in here now, nothing is saved.
        """
        rf = tmp_path / "reminders.json"
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt.execute("set_reminder", {"wake_name": "call mum",
                                      "when_due": "in 1 hour"})
        assert [r["name"] for r in H.json.loads(rf.read_text())] == ["call mum"]

        def boom(seconds):
            raise RuntimeError("formatter exploded")
        monkeypatch.setattr(H, "_fmt_dur", boom)
        out, err = belt.execute("set_reminder", {"wake_name": "call dad",
                                                 "when_due": "in 2 hours",
                                                 "repeat_hours": 3})
        assert err, out
        # the existing reminder is untouched and the new one was never added
        assert [r["name"] for r in H.json.loads(rf.read_text())] == ["call mum"]

    def test_a_store_poisoned_by_an_older_build_is_repaired_when_it_is_read(
            self, H, monkeypatch, tmp_path):
        """A non-finite repeat already on disk must not stay broken.

        `float(r.get("repeat_hours") or 0)` cannot catch it: `float(nan)`
        SUCCEEDS, and nan is truthy enough to survive the `or 0`. So reading is
        where a value written by an older build gets repaired — otherwise one
        poisoned entry keeps taking down the listing of every other reminder
        for as long as the file exists.
        """
        rf = tmp_path / "reminders.json"
        rf.write_text(H.json.dumps(
            [{"name": "poisoned", "due": H.time.time() + 600,
              "repeat_hours": float("nan")},
             {"name": "healthy", "due": H.time.time() + 1200,
              "repeat_hours": 2}]))
        monkeypatch.setattr(H, "REMINDERS_FILE", rf)
        loaded = H._load_reminders()
        by_name = {r["name"]: r for r in loaded}
        assert by_name["poisoned"]["repeat_hours"] == 0.0
        assert by_name["healthy"]["repeat_hours"] == 2.0
        # the listing survives, and shows the other reminder too
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("list_reminders", {})
        assert not err, out
        assert "poisoned" in out and "healthy" in out, out

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

    def test_a_number_word_is_a_whole_word(self, H):
        """A number word has to be a WHOLE word, and "and" is a connector.

        The pattern had no boundary on its word alternatives, so the `a`
        matched the first LETTER of any word starting with one: "an hour"
        tokenised as ("a", "n") and "2 hours and 30 minutes" as ("a", "nd").
        Both landed on a unit that is not a unit, the parse returned None, and
        set_reminder answered "could not understand when_due" — measured
        2026-09-27, on the most ordinary way to ask for a reminder in English.
        """
        for phrase, want in [("in an hour", 3600),
                             ("in 2 hours and 30 minutes", 9000),
                             ("in an hour and 20 minutes", 4800),
                             ("in one hour", 3600),
                             ("in two hours", 7200),
                             ("in three days", 3 * 86400),
                             ("in 2 hrs and 5 mins", 7500),
                             # "1h30m" still splits at the digit/letter seam,
                             # so the boundary went on the WORDS only
                             ("in 1h30m", 5400),
                             ("in 1.5 hours", 5400)]:
            assert H._parse_duration(phrase) == want, phrase
        # still fails closed on a unit that is not one, connector or not
        for phrase in ("meet at 3pm", "in 2 apples", "in a fortnight",
                       "in 2 apples and 3 hours", "in 2 and 30 minutes"):
            assert H._parse_duration(phrase) is None, phrase

    def test_half_a_span_means_half_of_the_unit_beside_it(self, H):
        """The idiom works whichever side of the phrase the unit sits on.

        "and a half" names a quantity without naming a unit, and the unit is
        wherever the rest of the phrase puts it — so the two orders have to
        agree, or one of them silently means half of what was asked for.
        """
        assert H._parse_duration("in an hour and a half") == 5400
        assert H._parse_duration("in one and a half hours") == 5400
        assert H._parse_duration("in 2 hours and a half") == 9000
        assert H._parse_duration("in a half hour") == 1800
        assert H._parse_duration("in a half hour and 10 minutes") == 2400
        # A half alone, or with no unit to halve, is not a duration
        for phrase in ("half", "in half", "in a half"):
            assert H._parse_duration(phrase) is None, phrase

    def test_half_an_hour_is_thirty_minutes(self, H):
        """"in half an hour" set a reminder for ONE hour: `an hour` was read on
        its own and the fraction in front of it dropped. Same for "half a day"
        (24 hours) and "quarter of an hour" (an hour) — each silently late by
        two to four times, measured 2026-09-29."""
        for phrase, want in [("in half an hour", 1800),
                             ("half an hour", 1800),
                             ("in half a day", 43200),
                             ("in half a minute", 30),
                             ("in half a week", 302400),
                             ("in a quarter of an hour", 900),
                             ("in quarter of an hour", 900),
                             ("in three quarters of an hour", 2700),
                             ("in half an hour and 10 minutes", 2400),
                             # the idioms that already worked must not move
                             ("in a half hour", 1800),
                             ("in an hour and a half", 5400),
                             ("in an hour", 3600)]:
            assert H._parse_duration(phrase) == want, phrase

    def test_a_number_no_unit_claimed_refuses_the_phrase(self, H):
        """`findall` reports what it found and is silent about what it walked
        past, so a phrase with a number left over was answered with the part
        that parsed: "in 1:30 hours" came back as THIRTY hours, "in 1 hour 30"
        as one hour. A wrong due time is worse than "could not understand"."""
        for phrase in ("in 1:30 hours", "in 1 hour 30", "in 2 hours at 5",
                       "in 2 days 09:30", "in 1.5.2 hours", "in 3 3 minutes"):
            assert H._parse_duration(phrase) is None, phrase
        # a stray WORD is harmless, and units still parse in every shape
        assert H._parse_duration("in 5 minutes after the film") == 300
        assert H._parse_duration("in 2 hours 5 minutes") == 7500
        assert H._parse_duration("in 1h30m") == 5400

    def test_an_unattributable_half_is_refused_rather_than_guessed(self, H):
        """"an hour and a half and 20 minutes" names two units, and the half
        belongs to one of them. Charging it against the trailing 20 minutes
        gave 1h20m30s — a reminder half an hour early, which is worse than
        saying nothing, so the ambiguity is refused."""
        assert H._parse_duration("in an hour and a half and 20 minutes") is None

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
        tmr_noon_utc = H.datetime.datetime.combine(
            tmr, H.datetime.time(12, 0)).astimezone(H.datetime.timezone.utc)
        berlin_noon = H.datetime.datetime.combine(
            today, H.datetime.time(12, 0)).astimezone().astimezone(
                __import__("zoneinfo").ZoneInfo("Europe/Berlin"))
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
            # TZID event today: LOCAL noon, spelled as the Berlin wall clock that
            # instant has. A fixed 08:00 Berlin was yesterday for every machine
            # west of about UTC-1 (the whole of the Americas), so the "presence
            # only" claim this comment used to make held only where the author
            # lives. What is under test is the TZID path, not the hour.
            "BEGIN:VEVENT",
            f"DTSTART;TZID=Europe/Berlin:{berlin_noon:%Y%m%dT%H%M%S}",
            f"DTEND;TZID=Europe/Berlin:{berlin_noon + H.datetime.timedelta(minutes=30):%Y%m%dT%H%M%S}",
            "SUMMARY:TZ event",
            "END:VEVENT",
            # tomorrow at LOCAL noon, written in the Z form. It used to be a
            # fixed 18:30 UTC, which is the day after tomorrow for any machine
            # east of about UTC+5:30 (India, China, Japan, Australia, New
            # Zealand), so the multi-day test failed there for want of a
            # timezone — the fixture's subject is the Z spelling, not the hour.
            "BEGIN:VEVENT",
            f"DTSTART:{tmr_noon_utc:%Y%m%dT%H%M%SZ}",
            f"DTEND:{tmr_noon_utc + H.datetime.timedelta(hours=1):%Y%m%dT%H%M%SZ}",
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
        ev = _core_calendar.ics_events_from_text(text, win_s, win_e)
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
        return _core_calendar.ics_events_from_text(
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
        # DATES, not day-of-month ints: `sorted(... .day)` reorders
        # 29, 30, 1 when the window crosses a month boundary, so this test
        # failed on the 29th and 30th of every month no matter what the
        # parser did. `.date()` also sidesteps comparing an aware event start
        # with the naive `start` above.
        days_seen = sorted(e["start"].date() for e in ev)
        assert len(ev) == 3, [e["start"] for e in ev]
        assert days_seen[-1] == (start + H.datetime.timedelta(days=2)).date(), \
            days_seen
        # the day AFTER the UNTIL date must still be excluded
        assert (start + H.datetime.timedelta(days=3)).date() not in days_seen, \
            days_seen

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
        return _core_calendar.ics_events_from_text(text, ws, we)

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


class TestMonthlyAndYearlyRulesFollowTheRFC:
    """Recurrence found by differential fuzzing against `dateutil.rrule`
    (2026-09-29, 4000 random rules: 86 disagreements, none of them a rule the
    suite above had a case for).

    Every test here uses UTC ('Z') times and UTC windows, so the answer cannot
    depend on the machine's zone — the rule's own semantics are the subject.
    """

    UTC = __import__("datetime").timezone.utc

    def _text(self, dtstart, rrule):
        return "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:rfc@test",
            f"DTSTART:{dtstart}", f"DTEND:{dtstart[:8]}T235900Z",
            "SUMMARY:Sync", f"RRULE:{rrule}", "END:VEVENT", "END:VCALENDAR"])

    def _on(self, dtstart, rrule, y, m, d, days=1):
        import datetime as dt
        ws = dt.datetime(y, m, d, tzinfo=self.UTC)
        ev = _core_calendar.ics_events_from_text(
            self._text(dtstart, rrule), ws, ws + dt.timedelta(days=days))
        return sorted(e["start"].astimezone(self.UTC).date().isoformat()
                      for e in ev)

    # -- the window ended before DTSTART's own day-of-month -------------------

    def test_the_last_friday_is_found_on_the_last_friday(self):
        """DTSTART is the first instance (Fri 30 Jan). February's instance is
        the 27th — before the 30th that the loop used as its stand-in for the
        month — so a window ending that day ended the search first and the
        meeting read as "no events" on the day it happens."""
        for day in ((2026, 2, 27), (2026, 3, 27), (2026, 4, 24), (2026, 5, 29)):
            got = self._on("20260130T100000Z", "FREQ=MONTHLY;BYDAY=-1FR", *day)
            assert got == ["%04d-%02d-%02d" % day], (day, got)

    def test_a_first_and_fifteenth_rule_started_on_the_fifteenth(self):
        assert self._on("20260115T090000Z", "FREQ=MONTHLY;BYMONTHDAY=1,15",
                        2026, 3, 1) == ["2026-03-01"]
        assert self._on("20260115T090000Z", "FREQ=MONTHLY;BYMONTHDAY=1,15",
                        2026, 3, 2) == []

    def test_every_single_day_window_agrees_with_a_day_by_day_oracle(self):
        """The shape that hid this: ONE-day windows, at every distance from
        DTSTART, for rules whose days are not DTSTART's day. DTSTART is the
        rule's first instance, as producers write it."""
        import calendar as _c
        import datetime as dt

        def last_day(d):
            return _c.monthrange(d.year, d.month)[1]

        rules = {
            "BYMONTHDAY=1": lambda d: d.day == 1,
            "BYMONTHDAY=1,15": lambda d: d.day in (1, 15),
            "BYMONTHDAY=-1": lambda d: d.day == last_day(d),
            "BYMONTHDAY=31": lambda d: d.day == 31,
            "BYDAY=-1FR": lambda d: d.weekday() == 4 and d.day + 7 > last_day(d),
            "BYDAY=1MO": lambda d: d.weekday() == 0 and d.day <= 7,
            "BYDAY=2SA,4SA": lambda d: (d.weekday() == 5
                                        and (d.day - 1) // 7 in (1, 3)),
        }
        span = 200
        for earliest in (dt.date(2026, 1, 30), dt.date(2026, 1, 15)):
            for rule, matches in rules.items():
                first = next(d for d in (earliest + dt.timedelta(n)
                                         for n in range(62)) if matches(d))
                start = first.strftime("%Y%m%dT100000Z")
                days = [first - dt.timedelta(days=2) + dt.timedelta(n)
                        for n in range(span)]
                got = {d for d in days
                       if self._on(start, f"FREQ=MONTHLY;{rule}",
                                   d.year, d.month, d.day)}
                want = {d for d in days if d >= first and matches(d)}
                assert got == want, (rule, first, sorted(got ^ want)[:6])

    # -- COUNT counts instances, and instances start at DTSTART ---------------

    def test_count_does_not_spend_itself_on_days_before_dtstart(self):
        """DTSTART the 10th, BYMONTHDAY=1,15: January's 1st precedes DTSTART, so
        it is not an instance and must not be counted — COUNT=3 is Jan 15,
        Feb 1, Feb 15, not two."""
        got = self._on("20260110T100000Z", "FREQ=MONTHLY;BYMONTHDAY=1,15;COUNT=3",
                       2026, 1, 1, days=120)
        assert got == ["2026-01-15", "2026-02-01", "2026-02-15"]

    def test_count_survives_the_window_jump_over_the_first_month(self):
        """The credit for months the window jump skips counted the first
        month's pre-DTSTART candidates too, so a window far from DTSTART saw
        the rule end early (or never) depending on the day of DTSTART."""
        got = self._on("20260110T100000Z", "FREQ=MONTHLY;BYMONTHDAY=1,15;COUNT=6",
                       2026, 3, 1, days=90)
        # Jan 15, Feb 1, Feb 15, Mar 1, Mar 15, Apr 1 — six instances, so the
        # window sees the last three (verified against dateutil.rrule).
        assert got == ["2026-03-01", "2026-03-15", "2026-04-01"]

    def test_yearly_count_ignores_the_months_before_dtstart(self):
        got = self._on("20231202T040000Z", "FREQ=YEARLY;BYMONTH=4,11;COUNT=2",
                       2023, 1, 1, days=1500)
        assert got == ["2024-04-02", "2024-11-02"]

    # -- a date the rule names twice is one instance --------------------------

    def test_bymonthday_naming_one_day_twice_lists_it_once(self):
        """February 2025 has 28 days, so -3 IS the 26th."""
        got = self._on("20250126T100000Z", "FREQ=MONTHLY;BYMONTHDAY=26,-3",
                       2025, 2, 1, days=28)
        assert got == ["2025-02-26"]

    def test_the_same_nth_weekday_twice_lists_it_once(self):
        got = self._on("20260102T100000Z", "FREQ=MONTHLY;BYDAY=1FR,1FR",
                       2026, 1, 1, days=31)
        assert got == ["2026-01-02"]

    def test_duplicates_do_not_eat_the_count(self):
        got = self._on("20260102T100000Z", "FREQ=MONTHLY;BYDAY=1FR,1FR;COUNT=2",
                       2026, 1, 1, days=90)
        assert got == ["2026-01-02", "2026-02-06"]

    # -- WEEKLY walks its days in calendar order ------------------------------

    def test_weekly_count_is_spent_in_calendar_order(self):
        """`BYDAY=FR,MO;COUNT=3` from a Monday is Mon, Fri, Mon — written order
        spent the count on Friday first and ended on the wrong day."""
        got = self._on("20260105T090000Z", "FREQ=WEEKLY;BYDAY=FR,MO;COUNT=3",
                       2026, 1, 1, days=60)
        assert got == ["2026-01-05", "2026-01-09", "2026-01-12"]

    def test_weekly_bydays_named_twice_count_once(self):
        got = self._on("20260105T090000Z", "FREQ=WEEKLY;BYDAY=MO,MO;COUNT=2",
                       2026, 1, 1, days=60)
        assert got == ["2026-01-05", "2026-01-12"]


class TestRecurrenceKeepsTheWallClockAcrossDST:
    """A floating time and an all-day date mean "on the reader's own wall
    clock", and were expanded at the reader's UTC offset ON THAT DATE, frozen:
    `naive.astimezone()` returns a fixed-offset tzinfo, not the machine's zone.
    A weekly all-day event first entered in summer (`+02:00`) then sat at
    `00:00+02:00` for ever, which after the clocks change is 23:00 on the
    PREVIOUS day — Thursday's event announced on Wednesday, and not on Thursday.
    A floating 21:45 meeting read 20:45. Found by differential fuzzing against
    `recurring-ical-events` (2026-09-29; 16 of 800 random single-event files).

    Every test pins the machine to the desk's zone (CET/CEST, DST ends
    2026-10-25), because "the clocks changed" is the subject.
    """

    @pytest.fixture(autouse=True)
    def _on_the_desk_clock(self, desk_zone):
        yield

    @staticmethod
    def _ics(*body):
        return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT",
                            "UID:dst@test", "SUMMARY:Ev", *body,
                            "END:VEVENT", "END:VCALENDAR"])

    @staticmethod
    def _local_day(text, y, m, d):
        """The events `read_calendar` would find for ONE local day: it builds a
        naive local-midnight window exactly like this."""
        import datetime as dt
        ws = dt.datetime(y, m, d)
        return [(e["start"].strftime("%a %d %b %H:%M"), e["allday"])
                for e in _core_calendar.ics_events_from_text(
                    text, ws, ws + dt.timedelta(days=1))]

    def test_a_weekly_all_day_event_stays_on_its_day_after_the_clocks_change(self):
        weekly = self._ics("DTSTART;VALUE=DATE:20260604",       # a Thursday, CEST
                           "DTEND;VALUE=DATE:20260605", "RRULE:FREQ=WEEKLY")
        assert self._local_day(weekly, 2026, 12, 3) == [("Thu 03 Dec 00:00", True)]
        assert self._local_day(weekly, 2026, 12, 2) == [], (
            "announced the day BEFORE it happens")

    def test_a_floating_time_keeps_its_clock_time_across_the_change(self):
        floating = self._ics("DTSTART:20260604T214500", "DTEND:20260604T224500",
                             "RRULE:FREQ=WEEKLY")
        assert self._local_day(floating, 2026, 12, 3) == [("Thu 03 Dec 21:45", False)]

    def test_it_holds_the_other_way_round_into_summer(self):
        winter = self._ics("DTSTART:20260115T094500", "DTEND:20260115T104500",
                           "RRULE:FREQ=DAILY;INTERVAL=3")
        # 2026-07-01 is 167 days after 2026-01-15, and 167 = 3 * 55 + 2: not an
        # instance; 2026-07-02 (168 = 3 * 56) is.
        assert self._local_day(winter, 2026, 7, 1) == []
        assert self._local_day(winter, 2026, 7, 2) == [("Thu 02 Jul 09:45", False)]

    def test_a_monthly_all_day_event_crosses_the_change(self):
        monthly = self._ics("DTSTART;VALUE=DATE:20260615",
                            "DTEND;VALUE=DATE:20260616", "RRULE:FREQ=MONTHLY")
        assert self._local_day(monthly, 2026, 12, 15) == [("Tue 15 Dec 00:00", True)]
        assert self._local_day(monthly, 2026, 12, 14) == []

    def test_a_date_the_platform_cannot_place_is_garbage_not_a_crash(self):
        """The zone is asked for its offset lazily, so an unplaceable date has
        to be refused where it is parsed, exactly as the fixed-offset version
        refused it there."""
        for value in ("DTSTART:00010101T000000", "DTSTART;VALUE=DATE:00010101",
                      "DTSTART:99991231T235959", "DTSTART;VALUE=DATE:99991231"):
            assert _core_calendar._ics_parse_dt(value) is None, value

    def test_a_daily_window_opening_on_an_instance_after_dst_still_has_it(self):
        """The DAILY jump was the one frequency without a step of slack. It is
        computed from ELAPSED time while instance i is i WALL-CLOCK days after
        DTSTART, and after Sydney's clocks went back the two differ by an hour:
        a window opening exactly on the 08:00 instance was jumped past it."""
        import datetime as dt
        text = self._ics("DTSTART;TZID=Australia/Sydney:20251124T080000",
                         "DTEND;TZID=Australia/Sydney:20251124T083000",
                         "RRULE:FREQ=DAILY")
        ws = dt.datetime(2026, 5, 5, 22, 0, tzinfo=dt.timezone.utc)   # 08:00 AEST
        ev = _core_calendar.ics_events_from_text(text, ws,
                                                 ws + dt.timedelta(hours=1))
        assert [e["start"].astimezone(dt.timezone.utc).isoformat()
                for e in ev] == ["2026-05-05T22:00:00+00:00"]


class TestICSDailyOldEvent:
    """DAILY expansion must reach the window even when DTSTART is long past.

    The expander used to step from DTSTART one interval at a time under a
    500-instance cap, so a daily event created more than ~500 days ago burned
    its whole budget before reaching today and silently vanished from the
    answer — "no events" for a meeting that is on every single day.
    """

    # The window ends are built with `timedelta`, not `(y, m, today.day + n)`.
    # The tuple form raises `ValueError: day 31 must be in range 1..30` the
    # moment the arithmetic leaves the month, so these three tests measured
    # nothing on the 29th and 30th of a 30-day month — and on the 28th, which
    # is how this was found: 2026-09-28 + 3 days is the 31st of September.
    # A test that fails on a date rather than on a defect is a test with a
    # second, invisible subject, and the rest of this class had already moved
    # to the safe form (`.timetuple()[:3]`); these were the stragglers.

    def _events(self, H, dtstart, rrule, win_s, win_e, summary="Ev"):
        text = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0",
            "BEGIN:VEVENT", "UID:old-daily@test",
            f"DTSTART:{dtstart}", "DTEND:20240101T103000",
            f"SUMMARY:{summary}", f"RRULE:{rrule}",
            "END:VEVENT",
            "END:VCALENDAR",
        ])
        ws = H.datetime.datetime(*win_s)
        we = H.datetime.datetime(*win_e)
        return _core_calendar.ics_events_from_text(text, ws, we)

    def test_a_daily_event_older_than_the_instance_cap_still_appears(self, H):
        # 800 days before today: past the old 500-instance COUNT budget.
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - H.datetime.timedelta(days=800)
        ev = self._events(
            H, start.strftime("%Y%m%dT090000"),            "FREQ=DAILY",
            (today.year, today.month, today.day),
            (today + H.datetime.timedelta(days=1)).timetuple()[:3])
        assert len(ev) == 1, ev
        assert ev[0]["start"].date() == today.date()

    def test_count_still_bounds_absolute_instances(self, H):
        """The jump must not change COUNT's RFC meaning: the cap counts TOTAL
        instances since DTSTART (incl. DTSTART), so an old event with
        COUNT=500 and DTSTART 800 days ago is FINISHED — nothing in the
        window, not a resurrected stream."""
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - H.datetime.timedelta(days=800)
        ev = self._events(
            H, start.strftime("%Y%m%dT090000"),            "FREQ=DAILY;COUNT=500",
            (today.year, today.month, today.day),
            (today + H.datetime.timedelta(days=1)).timetuple()[:3])
        assert ev == [], f"a COUNT-finished recurrence came back from the dead: {ev}"

    def test_interval_alignment_is_preserved_across_the_jump(self, H):
        # INTERVAL=3, DTSTART 901 days ago: 901 % 3 == 1, so the naive
        # "start at the window" shortcut lands on a day the event does NOT
        # occur on. The arithmetic jump must preserve DTSTART's phase.
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - H.datetime.timedelta(days=901)
        ev = self._events(
            H, start.strftime("%Y%m%dT090000"),            "FREQ=DAILY;INTERVAL=3",
            (today.year, today.month, today.day),
            (today + H.datetime.timedelta(days=3)).timetuple()[:3])
        days = [e["start"].date() for e in ev]
        phase = (start.date() - days[0]).days % 3 if days else None
        assert days and phase == 0, (days, phase)

    def test_a_weekly_event_older_than_the_week_cap_still_appears(self, H):
        # The WEEKLY branch's own `w < 200` cap is a distance from DTSTART,
        # not a work bound: past ~4 years a weekly event stopped appearing.
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - H.datetime.timedelta(days=5 * 365)
        text = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0",
            "BEGIN:VEVENT", "UID:old-weekly@test",
            f"DTSTART:{start.strftime('%Y%m%dT090000')}",
            f"DTEND:{start.strftime('%Y%m%dT093000')}",
            "SUMMARY:Old weekly", "RRULE:FREQ=WEEKLY",
            "END:VEVENT", "END:VCALENDAR",
        ])
        win_s = today.replace(hour=0, minute=0, second=0, microsecond=0)
        win_e = win_s + H.datetime.timedelta(days=8)
        ev = _core_calendar.ics_events_from_text(text, win_s, win_e)
        assert ev and ev[0]["summary"] == "Old weekly", ev

    def test_a_weekly_count_that_ended_years_ago_is_not_re_emitted(self, H):
        """COUNT is spent from DTSTART, not from wherever the week jump landed.

        The WEEKLY branch jumps `w` to the first week that can overlap the
        window, and the instance counter `k` still began at 0 — so a
        WEEKLY;COUNT=3 meeting that ENDED years ago was re-emitted as if it
        were just starting (verified 2026-09-18). The DAILY branch derives an
        absolute index and never had this shape; the two disagreeing is what
        made the phantom instances possible.
        """
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - H.datetime.timedelta(days=3 * 365)
        ev = self._events(H, start.strftime("%Y%m%dT090000"),
                          "FREQ=WEEKLY;COUNT=3",
                          (today.year, today.month, today.day),
                          (today + H.datetime.timedelta(days=14)).timetuple()[:3])
        assert ev == [], ev

    def test_a_weekly_count_emits_only_its_remaining_instances(self, H):
        """A COUNT=10 weekly rule four weeks in has SIX instances left.

        The window is eight weeks long, so the difference between counting
        absolute instances and counting from the jump is visible in one array:
        the rule ends at instance #10 (five weeks from today) and nothing may
        appear after it.
        """
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - H.datetime.timedelta(days=28)
        ev = self._events(H, start.strftime("%Y%m%dT090000"),
                          "FREQ=WEEKLY;COUNT=10",
                          (today.year, today.month, today.day),
                          (today + H.datetime.timedelta(days=56)).timetuple()[:3])
        got = [e["start"].date() for e in ev]
        want = [(today + H.datetime.timedelta(weeks=i)).date()
                for i in range(6)]
        assert got == want, (got, want)

    def test_a_weekly_count_stops_mid_week(self, H):
        """COUNT bounds INSTANCES, and a week can hold several matching days.

        The `k < count` check lived only on the outer week loop, so a cap
        reached on Wednesday kept emitting Friday and then the next week's
        days — COUNT=4 over MO,WE,FR produced SIX instances (verified
        2026-09-20). The window is two weeks wide on purpose: the overrun is
        only visible when the cap lands inside a week.
        """
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        monday = today - H.datetime.timedelta(days=today.weekday())
        ev = self._events(H, monday.strftime("%Y%m%dT090000"),
                          "FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=4",
                          (monday.year, monday.month, monday.day),
                          (monday + H.datetime.timedelta(days=15)).timetuple()[:3])
        got = [e["start"].date() for e in ev]
        want = [(monday + H.datetime.timedelta(days=d)).date()
                for d in (0, 2, 4, 7)]
        assert got == want, (got, want)

    def test_a_monthly_count_stops_mid_month(self, H):
        """The same mid-batch cap, in the BYMONTHDAY candidate loop."""
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        first = today.replace(day=1)
        ev = self._events(H, first.strftime("%Y%m%dT090000"),
                          "FREQ=MONTHLY;BYMONTHDAY=1,15;COUNT=3",
                          (first.year, first.month, 1),
                          (first + H.datetime.timedelta(days=60)).timetuple()[:3])
        got = [e["start"].date() for e in ev]
        second = (first + H.datetime.timedelta(days=32)).replace(day=1)
        want = [first.date(), first.replace(day=15).date(), second.date()]
        assert got == want, (got, want)

    def test_a_yearly_count_stops_mid_year(self, H):
        """The same mid-batch cap, in the YEARLY candidate loop."""
        today = H.datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0)
        first = today.replace(month=1, day=1)
        ev = self._events(H, first.strftime("%Y%m%dT090000"),
                          "FREQ=YEARLY;BYMONTH=1,6;BYMONTHDAY=1;COUNT=3",
                          (first.year, 1, 1),
                          (first + H.datetime.timedelta(days=400)).timetuple()[:3])
        got = [e["start"].date() for e in ev]
        want = [first.date(), first.replace(month=6).date(),
                first.replace(year=first.year + 1).date()]
        assert got == want, (got, want)


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
