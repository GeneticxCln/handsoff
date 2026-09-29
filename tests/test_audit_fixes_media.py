"""Pins for the 2026-09-28 audit fixes in the media/IO modules.

core/calendar.py: TZID spellings real producers emit (quoted, Mozilla-prefixed)
no longer drop their events, and a zone this system has no tzdata for is
COUNTED and warned about instead of vanishing; recurrence arithmetic runs in
the event's own zone (RFC 5545's recurring wall time) instead of stepping wall
clock in the system zone; MONTHLY/YEARLY rules jump to the window like the
DAILY/WEEKLY ones; and component matching is case-insensitive.

core/audio.py + core/voice.py: every mic-lock acquirer on the watchdog/reopen/
stop paths is bounded with a named failure (a permanently wedged stream.stop()
used to leak a blocked thread per press and stall the hands-free watchdog), and
play_wav carries an OUTPUT watchdog so a wedged output device returns instead
of pinning the bubble in SPEAKING forever.

core/bubble.py: design-pack art decodes through a bounded QImageReader, so an
image bomb is refused at its declared size instead of being materialised first.

core/qs_desk.py: Desk.call reads at most MAX_BODY_BYTES, and an over-cap
answer becomes the module's named refusal shape.
"""
from __future__ import annotations

import datetime
import http.client
import json
import logging
import os
import struct
import threading
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

from conftest import HERE as ROOT
from conftest import _load, core_module
from core import calendar as _cal
from core import qs_desk as _qs


# ----------------------------------------------------------------- helpers

def _png(path: Path, w: int, h: int, rgb=(90, 140, 200)) -> None:
    """A valid truecolor PNG, written by hand (no Qt needed to build one)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b""))


def _bomb_png(path: Path, w: int, h: int) -> None:
    """A PNG that DECLARES enormous dimensions and carries almost no data.

    Whatever guard fires first — the allocation limit against the declared
    size, or the decode running out of scanlines — the answer must be a None,
    never a multi-hundred-megabyte raster."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x40\x40\x40" * 200, 6))
        + chunk(b"IEND", b""))


def _wav(path: Path, frames: int = 8192) -> Path:
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.full(frames, 3000, dtype=np.int16).tobytes())
    return path


@pytest.fixture()
def tz_fixed():
    """Pin the SYSTEM zone for one test and put the machine's own back.

    The bug being pinned was arithmetic in whatever zone the machine happened
    to run, so the test chooses a zone with the DST behaviour it needs and
    restores afterwards. `time.tzset` re-reads TZ, so it must run AFTER the
    environment is put back — hence this fixture rather than monkeypatch.
    """
    sentinel = object()
    old = os.environ.get("TZ", sentinel)

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    try:
        yield _set
    finally:
        if old is sentinel:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def _ics(*body: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *body,
                        "END:VCALENDAR"])


def _event(uid: str, dtstart: str, rrule: str = "", dtend: str = "",
           summary: str = "Ev") -> str:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:{dtstart}"]
    if dtend:
        lines.append(f"DTEND:{dtend}")
    lines.append(f"SUMMARY:{summary}")
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


UTC = datetime.timezone.utc


# ---------------------------------------------- TZID spellings (finding 1)

class TestTheTzidSpellings:
    """A TZID that names a zone must not drop its event because of HOW it was
    spelled: Outlook quotes the name, Thunderbird prefixes Mozilla's."""

    def test_a_quoted_tzid_parses(self):
        dt = _cal._ics_parse_dt(
            'DTSTART;TZID="America/New_York":20260307T090000')
        assert dt is not None
        assert dt.utcoffset() == datetime.timedelta(hours=-5)   # EST, March 7

    def test_a_mozilla_prefixed_tzid_parses(self):
        dt = _cal._ics_parse_dt(
            "DTSTART;TZID=/mozilla.org/20070129_1/America/New_York"
            ":20260307T090000")
        assert dt is not None
        assert dt.utcoffset() == datetime.timedelta(hours=-5)

    def test_a_quoted_tzid_event_is_found(self):
        text = _ics(_event("q@t", '20260307T090000',
                           dtend="20260307T093000")
                    .replace("DTSTART:", 'DTSTART;TZID="Europe/Berlin":'))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 8, tzinfo=UTC)
        ev = _cal.ics_events_from_text(text, ws, we)
        assert [e["summary"] for e in ev] == ["Ev"]

    def test_an_unknown_zone_still_skips_and_is_counted(self, caplog):
        text = _ics(_event("u@t", "20260307T090000", dtend="20260307T093000")
                    .replace("DTSTART:", "DTSTART;TZID=Mars/Olympus:"))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 8, tzinfo=UTC)
        with caplog.at_level(logging.WARNING, logger="handsoff"):
            ev = _cal.ics_events_from_text(text, ws, we)
        assert ev == [], "an unresolvable zone still skips the event"
        assert "tzdata" in caplog.text and "1 event" in caplog.text, \
            f"the skip went unreported: {caplog.text!r}"

    def test_two_unknown_zones_count_two(self, caplog):
        text = _ics(
            _event("u1@t", "20260307T090000").replace(
                "DTSTART:", "DTSTART;TZID=Mars/Olympus:"),
            _event("u2@t", "20260307T100000").replace(
                "DTSTART:", 'DTSTART;TZID="Nowhere/Nothing":'))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 8, tzinfo=UTC)
        with caplog.at_level(logging.WARNING, logger="handsoff"):
            _cal.ics_events_from_text(text, ws, we)
        assert "2 event" in caplog.text, caplog.text

    def test_a_garbage_dtstart_is_not_counted_as_a_zone_skip(self, caplog):
        text = _ics(_event("g@t", "not-a-date"))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 8, tzinfo=UTC)
        with caplog.at_level(logging.WARNING, logger="handsoff"):
            ev = _cal.ics_events_from_text(text, ws, we)
        assert ev == []
        assert "tzdata" not in caplog.text, \
            "garbage is not a zone failure and must not be reported as one"


# ----------------------------------- recurrence in the event's zone (finding 2)

class TestTheRecurrenceZone:
    """Interval arithmetic belongs to the EVENT's zone, not the system's: a
    Z-encoded rule holds its UTC instant across DST; a TZID rule holds its own
    wall clock even when the system's zone differs."""

    def test_a_z_daily_recurrence_holds_its_utc_instant_across_dst(
            self, tz_fixed):
        # The US spring-forward is 2026-03-08 02:00 local; the system zone is
        # pinned to New York so the OLD code (wall-clock stepping in the
        # system zone) drifts an hour right through this window.
        tz_fixed("America/New_York")
        text = _ics(_event("z@t", "20260307T140000Z",
                           dtend="20260307T143000Z", rrule="FREQ=DAILY"))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 12, tzinfo=UTC)
        ev = _cal.ics_events_from_text(text, ws, we)
        starts = [e["start"].astimezone(UTC) for e in ev]
        assert [s.day for s in starts] == [7, 8, 9, 10, 11]
        assert all(s.hour == 14 and s.minute == 0 for s in starts), starts

    def test_a_tzid_event_recurs_at_its_own_wall_time_in_another_zone(
            self, tz_fixed):
        # System zone has NO DST (Honolulu); the event's zone does (New York).
        # The old code stepped the wall clock in Honolulu, so the event's New
        # York wall time drifted across the US transition; now it cannot.
        tz_fixed("Pacific/Honolulu")
        text = _ics(_event("ny@t", "20260307T090000",
                           dtend="20260307T093000", rrule="FREQ=DAILY")
                    .replace("DTSTART:", "DTSTART;TZID=America/New_York:")
                    .replace("DTEND:", "DTEND;TZID=America/New_York:"))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 12, tzinfo=UTC)
        ev = _cal.ics_events_from_text(text, ws, we)
        ny = _cal._ics_tzid("America/New_York")
        walls = [e["start"].astimezone(ny) for e in ev]
        assert len(walls) == 5, walls
        assert all(w.hour == 9 and w.minute == 0 for w in walls), walls
        # ...and the window really does span the transition (EST -> EDT)
        assert {w.utcoffset() for w in walls} == {
            datetime.timedelta(hours=-5), datetime.timedelta(hours=-4)}

    def test_the_daily_jump_still_reaches_the_window_for_a_z_event(self):
        # 800 days of daily instances between DTSTART and the window: the
        # arithmetic jump must still land inside it, in the Z frame.
        today = datetime.datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = today - datetime.timedelta(days=800)
        text = _ics(_event("zold@t", start.strftime("%Y%m%dT090000Z"),
                           dtend=start.strftime("%Y%m%dT093000Z"),
                           rrule="FREQ=DAILY"))
        ev = _cal.ics_events_from_text(text, today, today + datetime.timedelta(
            days=1))
        assert len(ev) == 1, ev
        assert ev[0]["start"].astimezone(UTC).hour == 9, ev

    def test_the_answer_stays_in_the_system_frame(self, tz_fixed):
        # ics_events_from_text always returned starts in the system zone; the
        # event-zone arithmetic above must not change what a caller reads.
        tz_fixed("Europe/Berlin")
        text = _ics(_event("f@t", "20260307T140000Z",
                           dtend="20260307T143000Z"))
        ws = datetime.datetime(2026, 3, 7, tzinfo=UTC)
        we = datetime.datetime(2026, 3, 8, tzinfo=UTC)
        ev = _cal.ics_events_from_text(text, ws, we)
        assert ev[0]["start"].tzinfo is not None
        assert ev[0]["start"].utcoffset() == datetime.timedelta(hours=1), ev


# -------------------------------- MONTHLY/YEARLY jump-to-window (finding 7)

class TestTheMonthlyYearlyJump:
    """MONTHLY and YEARLY rules get the same jump the DAILY/WEEKLY branches
    already had: enumerate from the window, not from DTSTART."""

    def _events(self, dtstart: str, rrule: str, win_s, win_e):
        text = _ics(_event("jump@t", dtstart, dtend=dtstart[:8] + "T110000",
                           rrule=rrule))
        return _cal.ics_events_from_text(text, win_s, win_e)

    def test_a_monthly_event_from_2000_is_found_in_the_current_window(self):
        ws = datetime.datetime(2026, 9, 1)
        we = datetime.datetime(2026, 10, 1)
        ev = self._events("20000105T090000", "FREQ=MONTHLY", ws, we)
        assert [(e["start"].year, e["start"].month, e["start"].day)
                for e in ev] == [(2026, 9, 5)], ev

    def test_a_yearly_event_from_1990_is_found_in_its_window(self):
        ws = datetime.datetime(2026, 1, 1)
        we = datetime.datetime(2027, 1, 1)
        ev = self._events("19900615T090000", "FREQ=YEARLY", ws, we)
        assert [(e["start"].year, e["start"].month, e["start"].day)
                for e in ev] == [(2026, 6, 15)], ev

    def test_interval_alignment_survives_the_monthly_jump(self):
        # INTERVAL=12 from Jan 2000: the jump must land one step early and let
        # the loop carry to a month the rule actually occurs in — a January,
        # every twelfth month (the window below spans January 2027).
        ws = datetime.datetime(2027, 1, 1)
        we = datetime.datetime(2027, 2, 1)
        ev = self._events("20000105T090000", "FREQ=MONTHLY;INTERVAL=12", ws, we)
        assert [(e["start"].year, e["start"].month, e["start"].day)
                for e in ev] == [(2027, 1, 5)], ev
        # ...and a window the rule does NOT reach reads as empty, not as a
        # re-anchored stream emitting anyway.
        ev2 = self._events("20000105T090000", "FREQ=MONTHLY;INTERVAL=12",
                           datetime.datetime(2026, 9, 1),
                           datetime.datetime(2026, 10, 1))
        assert ev2 == [], ev2

    def test_a_monthly_count_that_ended_years_ago_is_not_re_emitted(self):
        ws = datetime.datetime(2026, 9, 1)
        we = datetime.datetime(2026, 10, 1)
        ev = self._events("20000105T090000", "FREQ=MONTHLY;COUNT=3", ws, we)
        assert ev == [], f"a COUNT-finished rule came back from the dead: {ev}"

    def test_a_monthly_count_emits_its_remaining_instances(self):
        # COUNT=380 monthly from Jan 2000: 31 spent by Aug 2026 (Jan 2000 is
        # instance #1), so Sep 2026 (#380) and Oct 2026 are the last two.
        ws = datetime.datetime(2026, 9, 1)
        we = datetime.datetime(2026, 11, 1)
        ev = self._events("20000105T090000", "FREQ=MONTHLY;COUNT=380", ws, we)
        assert [e["start"].day for e in ev] == [5, 5], ev
        assert [e["start"].month for e in ev] == [9, 10], ev

    def test_a_pathological_rule_stays_bounded(self):
        # BYMONTHDAY=32 matches no month, ever: the loop must still end (and
        # fast) rather than marching a full cap from DTSTART.
        t0 = time.monotonic()
        ev = self._events("20000105T090000", "FREQ=MONTHLY;BYMONTHDAY=32",
                          datetime.datetime(2026, 9, 1),
                          datetime.datetime(2026, 10, 1))
        assert ev == []
        assert time.monotonic() - t0 < 5.0, "the cap did not bound the rule"

    def test_a_yearly_rule_that_never_matches_stays_bounded(self):
        t0 = time.monotonic()
        ev = self._events("19900615T090000", "FREQ=YEARLY;BYMONTHDAY=31",
                          datetime.datetime(2026, 1, 1),
                          datetime.datetime(2027, 1, 1))
        assert ev == [], ev
        assert time.monotonic() - t0 < 5.0


# ------------------------------- case-insensitive components (finding 8)

class TestTheCaseInsensitiveComponents:
    def test_a_lowercase_producer_yields_events(self):
        text = ("begin:vcalendar\r\nbegin:vevent\r\nuid:low@t\r\n"
                "dtstart:20260928T100000\r\ndtend:20260928T110000\r\n"
                "summary:lower case\r\nend:vevent\r\nend:vcalendar")
        ev = _cal.ics_events_from_text(text, datetime.datetime(2026, 9, 28),
                                       datetime.datetime(2026, 9, 29))
        assert [e["summary"] for e in ev] == ["lower case"], ev

    def test_mixed_case_still_yields_events(self):
        text = _ics("begin:VEVENT\r\nUID:mix@t\r\nDtStArT:20260928T100000\r\n"
                    "SUMMARY:mixed\r\nend:vevent")
        ev = _cal.ics_events_from_text(text, datetime.datetime(2026, 9, 28),
                                       datetime.datetime(2026, 9, 29))
        assert [e["summary"] for e in ev] == ["mixed"], ev


# ------------------------------------ bounded mic-lock acquires (finding 3)

class _HeldStream:
    """A stream double that records teardown calls and never arrives."""

    def __init__(self):
        self.calls: list[str] = []

    def stop(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")

    def start(self):
        self.calls.append("start")


class TestTheMicLockIsBounded:
    """A wedged stream.stop() owns MIC_OPERATION_LOCK forever; every acquirer
    that serves the watchdog/reopen/stop paths must give up with a named
    failure instead of blocking forever."""

    @staticmethod
    def _holder(lock: threading.Lock):
        release = threading.Event()
        got = threading.Event()

        def hold():
            lock.acquire()
            got.set()
            release.wait(30)
            lock.release()

        th = threading.Thread(target=hold, daemon=True)
        th.start()
        assert got.wait(5), "the holder could not take the lock"
        return release

    def test_stop_stream_owned_gives_up_instead_of_blocking(self, caplog):
        voice = _load("audit_media_voice_stop", ROOT / "core" / "voice.py")
        lock = threading.Lock()
        release = self._holder(lock)
        stream = _HeldStream()
        try:
            with caplog.at_level(logging.WARNING, logger="handsoff"):
                t0 = time.monotonic()
                voice.stop_stream_owned(stream, lock, timeout_s=0.2)
                elapsed = time.monotonic() - t0
        finally:
            release.set()
        assert elapsed < 5.0, f"stop blocked for {elapsed:.1f}s"
        assert stream.calls == [], "the stream was touched without the lock"
        assert "wedged" in caplog.text, caplog.text

    def test_start_stream_owned_gives_up_instead_of_blocking(self, caplog):
        voice = _load("audit_media_voice_start", ROOT / "core" / "voice.py")
        lock = threading.Lock()
        release = self._holder(lock)
        stream = _HeldStream()
        try:
            with caplog.at_level(logging.WARNING, logger="handsoff"):
                t0 = time.monotonic()
                voice.start_stream_owned(stream, lock, timeout_s=0.2)
                elapsed = time.monotonic() - t0
        finally:
            release.set()
        assert elapsed < 5.0, f"start blocked for {elapsed:.1f}s"
        assert stream.calls == [], "the stream was started without the lock"
        assert "wedged" in caplog.text, caplog.text

    def test_stop_and_start_work_when_the_lock_is_free(self):
        voice = _load("audit_media_voice_ok", ROOT / "core" / "voice.py")
        lock = threading.Lock()
        stream = _HeldStream()
        voice.stop_stream_owned(stream, lock, timeout_s=0.2)
        voice.start_stream_owned(stream, lock, timeout_s=0.2)
        assert stream.calls == ["stop", "close", "start"]

    def test_bounded_stop_gives_up_when_the_lock_is_stuck(self, caplog):
        mod = _load("audit_media_audio_stuck", ROOT / "core" / "audio.py")
        monkey_release = threading.Event()
        holder = self._holder(mod.MIC_OPERATION_LOCK)
        try:
            with caplog.at_level(logging.WARNING, logger="handsoff"):
                t0 = time.monotonic()
                out = mod._stop_recorder_bounded(type("R", (), {})())
                elapsed = time.monotonic() - t0
        finally:
            holder.set()
        assert out == (None, False), out
        assert elapsed < 5.0, f"the stop waited {elapsed:.1f}s"
        assert "wedged" in caplog.text, caplog.text
        lingering = [t for t in threading.enumerate()
                     if t.name == "ptt-stop-native"]
        assert lingering == [], "the gave-up stop leaked its owner thread"

    def test_bounded_stop_still_delivers_audio_when_the_lock_is_free(self):
        mod = _load("audit_media_audio_ok", ROOT / "core" / "audio.py")

        class _Rec:
            def stop(self):
                return np.full(16, 500, dtype=np.int16)

        out = mod._stop_recorder_bounded(_Rec())
        assert out[1] is False and out[0] is not None and len(out[0]) == 16


# ------------------------------------ play_wav output watchdog (finding 4)

class TestTheOutputWatchdog:
    """stream.write() blocks unboundedly on a wedged output device, and the
    caller holds _ANNOUNCE_LOCK while it blocks. A monitor feeds the deadline
    per successful write and aborts the stream when a write goes unanswered."""

    @staticmethod
    def _audio():
        return _load("audit_media_audio_play", ROOT / "core" / "audio.py")

    def test_a_wedged_write_returns_within_the_watchdog_window(self, caplog,
                                                               tmp_path,
                                                               monkeypatch):
        mod = self._audio()
        monkeypatch.setattr(mod, "OUTPUT_WATCHDOG_S", 0.5)
        real = mod.sd.OutputStream

        class _Wedged:
            def __init__(self, **_kw):
                self.aborts = 0
                self._unblock = threading.Event()

            def start(self):
                pass

            def write(self, data):
                self._unblock.wait(30)          # the wedge
                raise RuntimeError("write interrupted")   # abort unblocks it

            def abort(self):
                self.aborts += 1
                self._unblock.set()

            def stop(self):
                pass

            def close(self):
                pass

        stream = _Wedged()
        monkeypatch.setattr(mod.sd, "OutputStream", lambda **_kw: stream)
        try:
            with caplog.at_level(logging.WARNING, logger="handsoff"):
                t0 = time.monotonic()
                mod.play_wav(_wav(tmp_path / "wedge.wav"), threading.Event())
                elapsed = time.monotonic() - t0
        finally:
            monkeypatch.setattr(mod.sd, "OutputStream", real, raising=False)
        assert elapsed < 8.0, f"playback hung {elapsed:.1f}s past the watchdog"
        assert stream.aborts == 1, "the monitor never aborted the stream"
        assert "wedged" in caplog.text, caplog.text

    def test_a_healthy_stream_never_trips_the_watchdog(self, caplog, tmp_path,
                                                       monkeypatch):
        mod = self._audio()
        real = mod.sd.OutputStream
        writes = []

        class _Healthy:
            def __init__(self, **_kw):
                pass

            def start(self):
                pass

            def write(self, data):
                writes.append(np.asarray(data).size)

            def abort(self):            # must never be reached
                raise AssertionError("watchdog fired on a healthy stream")

            def stop(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(mod.sd, "OutputStream", lambda **_kw: _Healthy())
        try:
            with caplog.at_level(logging.WARNING, logger="handsoff"):
                mod.play_wav(_wav(tmp_path / "ok.wav"), threading.Event())
        finally:
            monkeypatch.setattr(mod.sd, "OutputStream", real, raising=False)
        assert sum(writes) == 8192, "the whole clip must still be written"
        assert "wedged" not in caplog.text, caplog.text

    def test_cancel_is_still_honoured_between_blocks(self, tmp_path,
                                                     monkeypatch):
        mod = self._audio()
        real = mod.sd.OutputStream
        cancel = threading.Event()
        writes = []

        class _Cancelling:
            def __init__(self, **_kw):
                pass

            def start(self):
                pass

            def write(self, data):
                writes.append(np.asarray(data).size)
                cancel.set()            # barge-in on the first block

            def stop(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(mod.sd, "OutputStream",
                            lambda **_kw: _Cancelling())
        try:
            mod.play_wav(_wav(tmp_path / "cancel.wav"), cancel)
        finally:
            monkeypatch.setattr(mod.sd, "OutputStream", real, raising=False)
        assert writes == [1024], writes

    def test_a_pre_set_cancel_writes_nothing(self, tmp_path, monkeypatch):
        mod = self._audio()
        real = mod.sd.OutputStream
        writes = []

        class _Counting:
            def __init__(self, **_kw):
                pass

            def start(self):
                pass

            def write(self, data):
                writes.append(np.asarray(data).size)

            def stop(self):
                pass

            def close(self):
                pass

        monkeypatch.setattr(mod.sd, "OutputStream", lambda **_kw: _Counting())
        try:
            mod.play_wav(_wav(tmp_path / "pre.wav"), threading.Event())
        finally:
            monkeypatch.setattr(mod.sd, "OutputStream", real, raising=False)
        assert writes, "sanity: unset cancel must play"


# ------------------------------------ bounded design-pack decode (finding 5)

class TestTheBoundedDecode:
    """Design-pack art is user-supplied: the decode itself is bounded to the
    size the widget ever paints, and a bomb is refused, not materialised."""

    @staticmethod
    def _bubble():
        return core_module("bubble")

    def test_a_large_picture_decodes_bounded(self, tmp_path):
        bubble = self._bubble()
        p = tmp_path / "big.png"
        _png(p, 1600, 800)
        img = bubble._decoded_image(p)
        assert img is not None
        assert (img.width(), img.height()) == (
            bubble._IMAGE_WORK, bubble._IMAGE_WORK // 2), (img.width(),
                                                           img.height())
        assert img.format() == img.Format.Format_ARGB32

    def test_a_small_picture_decodes_at_its_own_size(self, tmp_path):
        bubble = self._bubble()
        p = tmp_path / "small.png"
        _png(p, 100, 50)
        img = bubble._decoded_image(p)
        assert img is not None and (img.width(), img.height()) == (100, 50)

    def test_an_image_bomb_is_refused_not_allocated(self, tmp_path):
        bubble = self._bubble()
        p = tmp_path / "bomb.png"
        _bomb_png(p, 12000, 12000)          # 576 MB as ARGB32
        t0 = time.monotonic()
        img = bubble._decoded_image(p)
        assert time.monotonic() - t0 < 5.0
        assert img is None, (img.width(), img.height())

    def test_garbage_bytes_return_none(self, tmp_path):
        bubble = self._bubble()
        p = tmp_path / "junk.png"
        p.write_bytes(b"definitely not a png" * 10)
        assert bubble._decoded_image(p) is None

    def test_a_missing_file_returns_none(self, tmp_path):
        bubble = self._bubble()
        assert bubble._decoded_image(tmp_path / "nope.png") is None


# --------------------------------------- bounded response body (finding 6)

class _FakeResponse:
    def __init__(self, payload: bytes, reads: list):
        self.status = 200
        self._payload = payload
        self._reads = reads

    def read(self, amt=-1):
        self._reads.append(amt)
        if amt is None or amt < 0:
            return self._payload
        return self._payload[:amt]


class _FakeConnection:
    """One canned 200 answer; records how much the client asked to read."""

    payload = b""
    reads: list = []

    def __init__(self, *_a, **_k):
        pass

    def request(self, *_a, **_k):
        pass

    def getresponse(self):
        return _FakeResponse(type(self).payload, type(self).reads)

    def close(self):
        pass


class TestTheResponseBodyCap:
    """Desk.call reads an answer through a cap; an over-cap answer becomes the
    module's named refusal shape instead of an unbounded buffer."""

    @staticmethod
    def _desk(tmp_path):
        ctl = tmp_path / "control.json"
        ctl.write_text(json.dumps({"protocol": 1, "port": 59999,
                                   "token": "t", "pid": os.getpid()}))
        ctl.chmod(0o600)
        return _qs.Desk(paths=[ctl])

    def test_an_over_cap_answer_is_a_named_refusal(self, tmp_path,
                                                   monkeypatch):
        desk = self._desk(tmp_path)
        monkeypatch.setattr(http.client, "HTTPConnection", _FakeConnection)
        _FakeConnection.reads = []
        _FakeConnection.payload = b"x" * (_qs.MAX_BODY_BYTES + 8)
        with pytest.raises(_qs.DeskError) as exc:
            desk.ping()
        assert exc.value.state == "error"
        assert "4 MiB" in str(exc.value)
        assert max(_FakeConnection.reads) <= _qs.MAX_BODY_BYTES + 1, \
            "the read itself must stay bounded, not only the verdict"

    def test_an_answer_exactly_at_the_cap_is_read(self, tmp_path, monkeypatch):
        desk = self._desk(tmp_path)
        monkeypatch.setattr(http.client, "HTTPConnection", _FakeConnection)
        body = json.dumps({"result": {"ok": True}}).encode()
        _FakeConnection.reads = []
        _FakeConnection.payload = body + b" " * (_qs.MAX_BODY_BYTES - len(body))
        assert desk.ping() == {"ok": True}

    def test_a_normal_answer_is_unaffected(self, tmp_path, monkeypatch):
        desk = self._desk(tmp_path)
        monkeypatch.setattr(http.client, "HTTPConnection", _FakeConnection)
        _FakeConnection.reads = []
        _FakeConnection.payload = json.dumps({"result": {"sessions": []}}).encode()
        assert desk.ping() == {"sessions": []}
