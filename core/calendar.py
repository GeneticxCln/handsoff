"""ICS calendar parsing and formatting for handsoff (stdlib only).

Pure functions: no settings, no Qt, no Assistant. handsoff.py re-exports
every name so the `H.*` monkeypatch contract keeps working; core.tools
reaches them through its host dependencies object.
"""
from __future__ import annotations

import calendar
import datetime
import logging
import re
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

# Same logger name as every other core module, so a calendar whose events are
# dropped lands in handsoff.log beside everything else.
log = logging.getLogger("handsoff")

_HTTP_UA = "handsoff/1.0 (local voice assistant)"

def _http_get(url: str, timeout: float = 10.0) -> bytes:
    """Fetch URL bytes with the handsoff user agent (cap 2 MB)."""
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000)

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")

MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December")


_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _ics_scheme_error(source: str):
    """Why `source` must not be fetched over the network, or None if it may.

    A Google-style "secret iCal address" is a bearer credential: anyone who
    sees the URL can read the whole calendar, and the calendar says where the
    user is and who they meet. Over cleartext http a MITM gets both the token
    and the contents, while the settings row has always advertised https — so
    that is what is enforced. Loopback stays allowed, because a URL pointing
    at this machine's own port is not on the wire and breaking a local
    calendar server would be pointless pedantry.
    """
    m = re.match(r"^(https?)://([^/?#]*)", source, re.I)
    if not m or m.group(1).lower() == "https":
        return None
    host = m.group(2).rsplit("@", 1)[-1]        # drop any user:pass@
    if host.startswith("["):                    # [::1]:8080
        host = host[1:host.index("]")] if "]" in host else host[1:]
    else:
        host = host.split(":")[0]
    if host.lower() in _LOOPBACK or host.startswith("127."):
        return None
    return ("plain http:// sends the calendar and its secret iCal token in "
            "clear text — use https:// (http:// is allowed for localhost)")


def _ics_source_label(source: str) -> str:
    """A source reduced to something safe to say out loud.

    The whole point of a "secret iCal address" is that the URL *is* the
    password, so it must never be echoed into the conversation — an error
    message is not a reason to leak it.
    """
    m = re.match(r"^(https?)://([^/?#]*)", str(source), re.I)
    if not m:
        return str(source)
    host = m.group(2).rsplit("@", 1)[-1]
    return f"{m.group(1).lower()}://{host or '?'}/… (path redacted)"


def ics_fetch(source: str) -> str:
    """Read an ICS calendar from an https URL or a local file path."""
    if re.match(r"^https?://", source, re.I):
        refusal = _ics_scheme_error(source)
        if refusal:
            raise ValueError(refusal)
        return _http_get(source, timeout=15).decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8", errors="replace")


def _ics_unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a continuation line starts with space/tab."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


class _SystemLocal(datetime.tzinfo):
    """The machine's own zone, as a ZONE rather than as the offset it has today.

    A floating time ("09:00", no zone) and an all-day date both mean "on the
    wall clock of wherever the reader is", and both used to be given
    `naive.astimezone()`. That returns a FIXED-OFFSET tzinfo: the machine's UTC
    offset on that one date, frozen. Recurrence is wall-clock arithmetic in the
    zone the datetime carries, so a weekly all-day event first entered in
    summer (`+02:00`) was expanded at `00:00+02:00` for ever — and after the
    clocks changed that instant is 23:00 on the PREVIOUS day. The assistant
    then announced Thursday's event on Wednesday (and not on Thursday), and a
    floating 21:45 meeting read as 20:45, for every event that crossed a DST
    change (found by differential fuzzing against `recurring-ical-events`,
    2026-09-29). Zones named by TZID never had this: they carry a ZoneInfo.

    This class carries the SYSTEM's zone the same way. It asks the platform for
    the offset at each wall time it is asked about, so `dtstart + 7 days` lands
    on the same local clock time whatever the offset did in between.
    """

    def utcoffset(self, dt):
        return dt.replace(tzinfo=None).astimezone().utcoffset()

    def tzname(self, dt):
        return dt.replace(tzinfo=None).astimezone().tzname()

    def dst(self, dt):
        return None                 # not known separately from the offset

    def fromutc(self, dt):
        # `dt` carries this tzinfo and holds UTC wall fields (the tzinfo
        # protocol); the answer is the same instant on the local wall clock,
        # keeping the fold flag the platform sets for the repeated hour.
        local = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        return local.replace(tzinfo=self)

    def __repr__(self) -> str:
        return "<system local zone>"


_LOCAL = _SystemLocal()


def _ics_tzid(tzid: str) -> "zoneinfo.ZoneInfo | None":
    """The zone a TZID names, or None when no tzdata on this system matches it.

    Two real-world spellings beyond a plain IANA name land here, and both used
    to drop their whole event: the QUOTED form Outlook emits
    (``DTSTART;TZID="America/New_York":…``) and Mozilla's globally-unique form
    (``/mozilla.org/20070129_1/America/New_York`` — every Thunderbird export),
    whose ``/<vendor>/<version>/`` prefix RFC 5545 allows and whose zone is
    everything after the SECOND slash. A name no zoneinfo matches still returns
    None: the caller decides what a skip costs.
    """
    name = str(tzid or "").strip().strip('"').strip()
    if name.startswith("/"):
        parts = name.split("/")
        if len(parts) >= 4:      # '', vendor, version, the zone (may hold /)
            name = "/".join(parts[3:])
    try:
        return zoneinfo.ZoneInfo(name)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return None


def _ics_parse_dt_checked(prop: str) -> "tuple[datetime.datetime | None, bool]":
    """(datetime, zone_failed) for a DTSTART/DTEND/… property value.

    `zone_failed` is True only when the value is a well-formed datetime that
    fails for the ONE reason a correct producer can still produce: its TZID
    names a zone this system has no tzdata for. That is the skip
    `ics_events_from_text` counts and reports — a lost meeting must be
    distinguishable in the journal from a garbage line.

    The datetime comes back in the frame the property itself names — UTC for
    the Z form, its TZID zone otherwise, the system's local zone only for a
    floating time. Recurrence arithmetic happens IN that frame (RFC 5545's
    recurring wall time), and every comparison against another aware datetime
    is instant-based, so the frame costs nothing at a boundary and keeps the
    event's own wall clock across DST and across zones.
    """
    if ":" not in prop:
        return None, False
    head, _, value = prop.partition(":")
    value = value.strip()
    params = dict(p.split("=", 1) for p in head.split(";")[1:] if "=" in p)
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            day = datetime.datetime.strptime(value[:8], "%Y%m%d")
            day = day.replace(tzinfo=_LOCAL)
            day.utcoffset()      # a date the platform cannot place is garbage
            return day, False
        dt = datetime.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        elif (tzid := params.get("TZID")):
            zone = _ics_tzid(tzid)
            if zone is None:
                return None, True
            dt = dt.replace(tzinfo=zone)
        elif dt.tzinfo is None:
            # A floating time names no zone, so it keeps the system's own —
            # the interpretation it always had (aware comparisons below
            # refuse a naive datetime outright). As a ZONE (`_SystemLocal`),
            # not a snapshot of today's offset: see that class.
            dt = dt.replace(tzinfo=_LOCAL)
            dt.utcoffset()       # a time the platform cannot place is garbage
        return dt, False
    except (ValueError, OverflowError, OSError, zoneinfo.ZoneInfoNotFoundError):
        return None, False


def _ics_parse_dt(prop: str) -> "datetime.datetime | None":
    """Parse a DTSTART/DTEND property → aware datetime (None on garbage).

    Handles 'YYYYMMDDTHHMMSSZ' (UTC), ';TZID=…' (zoneinfo — including the
    quoted and Mozilla-prefixed spellings real producers emit), floating local
    time, and all-day 'VALUE=DATE' / 8-digit dates (local midnight). The
    datetime keeps the frame the property named; see
    `_ics_parse_dt_checked` for the why and for the zone-failure signal."""
    dt, _ = _ics_parse_dt_checked(prop)
    return dt


def _ics_allday(prop: str) -> bool:
    if ":" not in prop:
        return False
    head, _, value = prop.partition(":")
    if "VALUE=DATE" in head:
        return True
    return bool(re.fullmatch(r"\d{8}", value.strip()))


def _ics_add_months(dt: "datetime.datetime", months: int) -> "datetime.datetime":
    """Add calendar months, clamping the day to the month's length
    (Jan 31 + 1 month -> Feb 28/29). Used for INTERVAL progression."""
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=y, month=m,
                      day=min(dt.day, _ics_month_length(y, m)))


def _ics_month_length(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _ics_month_day(year: int, month: int, n: int,
                   dtstart: "datetime.datetime") -> "datetime.datetime | None":
    """Day n of a month (negative = from the end) at DTSTART's time of day;
    None when the month has no such day (BYMONTHDAY=31 in February)."""
    length = _ics_month_length(year, month)
    day = length + 1 + n if n < 0 else n
    if not 1 <= day <= length:
        return None
    return dtstart.replace(year=year, month=month, day=day)


def _ics_nth_weekday(year: int, month: int, nth: int, weekday: int,
                     dtstart: "datetime.datetime") -> "datetime.datetime | None":
    """The nth (1..5) or nth-from-end (-1..-5) weekday of a month at
    DTSTART's time; None when that weekday does not occur nth times."""
    if nth > 0:
        first = datetime.date(year, month, 1)
        day = 1 + (weekday - first.weekday()) % 7 + (nth - 1) * 7
    else:
        last = _ics_month_length(year, month)
        last_wd = datetime.date(year, month, last).weekday()
        day = last - (last_wd - weekday) % 7 + (nth + 1) * 7
    if not 1 <= day <= _ics_month_length(year, month):
        return None
    return dtstart.replace(year=year, month=month, day=day)


def _ics_expand(dtstart: "datetime.datetime", rrule: str,
                win_start: "datetime.datetime", win_end: "datetime.datetime",
                dur: "datetime.timedelta") -> list["datetime.datetime"]:
    """RRULE expansion: DAILY, WEEKLY, MONTHLY and YEARLY.

    Supported parts: INTERVAL, BYDAY (WEEKLY weekday sets; MONTHLY nth
    weekdays like 2MO / -1FR), BYMONTHDAY, BYMONTH (YEARLY), UNTIL and
    COUNT. RFC 5545 semantics: UNTIL is inclusive; COUNT bounds the TOTAL
    instance count including DTSTART; instances before DTSTART do not
    exist. BYSETPOS and friends remain an honest single-occurrence
    fallback, not silent data loss.

    The arithmetic runs in DTSTART'S OWN FRAME (UTC for the Z form, its
    TZID zone otherwise): an aware datetime plus a timedelta is wall-clock
    arithmetic in the zone it carries, which is what a recurring wall time
    means — so a Z-encoded DAILY rule holds its UTC instant across DST and
    a TZID rule holds its local wall time even when the system's zone (and
    therefore the window's) differs. Every boundary check below compares
    aware datetimes, which is instant-based and frame-independent.
    """
    one = [dtstart] if dtstart < win_end and dtstart + dur > win_start else []
    if not rrule:
        return one
    parts = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = (parts.get("FREQ") or "").upper()
    # RFC COUNT is the TOTAL instance count INCLUDING DTSTART; when the rule
    # omits it the recurrence is unbounded and the window ends it. The old
    # `or 500` fused the two meanings: a bounded-looking default quietly
    # FINISHED every old unbounded recurrence 500 instances after DTSTART —
    # so a daily meeting created more than 500 days ago vanished from "today".
    _ABS = 10**9                              # stand-in when RFC COUNT is absent
    try:
        interval = max(1, int(parts.get("INTERVAL") or 1))
        count = int(parts["COUNT"]) if parts.get("COUNT") else _ABS
    except ValueError:
        return one
    # UNTIL is inclusive; a date-only UNTIL parses to that day's midnight.
    until: "datetime.datetime | None" = None
    if parts.get("UNTIL"):
        until = _ics_parse_dt(f"UNTIL:{parts['UNTIL']}")
        # A DATE-valued UNTIL is inclusive of that whole day. Parsed as written
        # it is that day's MIDNIGHT, which then excluded same-day instances
        # starting later in the day (RFC 5545: "the UNTIL rule part defines a
        # DATE or DATE-TIME value … inclusive").
        if until is not None and re.fullmatch(r"\d{8}", parts["UNTIL"].strip()):
            until = until.replace(hour=23, minute=59, second=59,
                                  microsecond=999999)

    def want(t: "datetime.datetime") -> bool:
        if t < dtstart or t >= win_end or t + dur <= win_start:
            return False
        return until is None or t <= until

    out: list = []
    k = 0                                     # absolute instance counter
    # Window-hit guard, NOT the recurrence cap: RFC COUNT bounds TOTAL
    # instances and is honoured separately below; this bound stops an
    # unterminated rule from marching towards `until` (years away) when the
    # window stopped matching — the thing the old `i < 500` also did.
    misses = 0
    MAX_WINDOW_MISSES = 400
    if freq == "DAILY":
        # Jump straight to the window: stepping from DTSTART one interval at a
        # time burned the fixed instance cap on instances nobody asked about,
        # so a daily event created more than ~500 days ago silently stopped
        # appearing (the loop hit its cap before reaching today) — "no events"
        # for a meeting that is on every single day. The first occurrence that
        # could still overlap the window is derived arithmetically; the counter
        # counts ABSOLUTE instances, so COUNT keeps its RFC meaning (total
        # instances since DTSTART, incl. DTSTART).
        first_index = 0
        if win_start > dtstart:
            gap = (win_start - dur) - dtstart
            if gap > datetime.timedelta(0):
                step = datetime.timedelta(days=interval)
                # One instance EARLIER than the strict answer, like the other
                # three frequencies' jumps. `gap` is elapsed time, but instance
                # i sits at DTSTART plus i WALL-CLOCK days, and across a DST
                # change those differ by the hour the clocks moved — so a
                # window opening exactly on an instance, after the change, was
                # jumped PAST it (a Sydney 08:00 daily event vanished from the
                # window that begins at 08:00). An instance short of the window
                # costs one `want()` and is skipped.
                first_index = gap // step
        i = first_index
        while i < count:
            t = dtstart + datetime.timedelta(days=i * interval)
            if t >= win_end:
                break
            if want(t):
                out.append(t)
            else:
                # Only a past UNTIL can miss here (first_index already puts t
                # past DTSTART and inside the overlap), so the misses bound
                # stops a march to a far-future UNTIL.
                misses += 1
                if misses > MAX_WINDOW_MISSES:
                    break
            i += 1
    elif freq == "WEEKLY":
        wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        # SORTED and de-duplicated: COUNT is spent in the order instances fall
        # in the calendar, so `BYDAY=FR,MO;COUNT=3` has to reach Monday before
        # Friday within a week. Walked as written it spent the count on the
        # Friday first and ended the rule on the wrong day (a differential run
        # against dateutil, 2026-09-29), and `BYDAY=MO,MO` counted one day twice.
        days = sorted({wd[d] for d in parts.get("BYDAY", "").split(",")
                       if d in wd}) or [dtstart.weekday()]
        # Midnight-aligned on purpose. Subtracting whole DAYS from DTSTART
        # cannot change its clock time, so a week0 built the obvious way still
        # carried it — and adding `hours=dtstart.hour` on top of that doubled
        # the time of day: a Monday 09:00 weekly event read back at 18:00, and
        # an evening one rolled into the next day. The day offset is applied to
        # that date and DTSTART's time is added exactly once below, which is
        # the only shape in which the two cannot both apply.
        week0 = (dtstart - datetime.timedelta(days=dtstart.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        clock = (dtstart.hour, dtstart.minute, dtstart.second,
                 dtstart.microsecond)
        w = 0
        # Jump to the first week that could still overlap the window — the
        # same family as the DAILY jump: 200 iterations from DTSTART is about
        # four years, after which a weekly event silently vanished.
        if win_start - dur > week0 + datetime.timedelta(weeks=interval):
            ahead = (win_start - dur) - week0
            w = max(0, ahead // datetime.timedelta(weeks=interval) - 1)
        # COUNT IS COUNTED FROM DTSTART, INCLUDING THE WEEKS THE JUMP SKIPPED.
        # The jump above moves `w` to the first week that can overlap the
        # window, but `k` — the counter the COUNT bound is compared against —
        # still began at 0, so a WEEKLY;COUNT=3 meeting that ENDED years ago was
        # re-emitted as if it were just beginning (verified 2026-09-18). DAILY
        # derives an absolute index and never had this shape, which is what made
        # the two branches disagree. Instances in weeks [0, w): the days on or
        # after DTSTART's own weekday in week 0 (an earlier weekday in that week
        # is still before DTSTART and does not exist), then every matching day
        # in each later week. With no COUNT the counter is only compared against
        # the unbounded stand-in, so this changes nothing there.
        if w > 0:
            k = (sum(1 for d in days if d >= dtstart.weekday())
                 + (w - 1) * len(days))
        # 200 weeks CAPS THE DISTANCE FROM DTSTART, not the work done: with
        # the jump above the loop now starts near the window, so bound it by
        # the distance to the window instead (a fortnight past win_end is
        # more than enough to cover an INTERVAL > 1's next match).
        w_cap = w + 24
        while w < w_cap and k < count:
            base = week0 + datetime.timedelta(weeks=w * interval)
            if base > win_end:
                break
            for d in days:
                # COUNT bounds INSTANCES, and a week can hold several
                # (BYDAY=MO,WE,FR): the check lived only on the `while` above,
                # so a cap reached mid-week kept emitting the rest of that
                # week's days — phantoms past the rule's own end (verified
                # 2026-09-20: COUNT=4 over MO,WE,FR emitted SIX).
                if k >= count:
                    break
                t = base + datetime.timedelta(days=d)
                t = t.replace(hour=clock[0], minute=clock[1], second=clock[2],
                              microsecond=clock[3])
                if t >= dtstart:
                    k += 1
                    if want(t):
                        out.append(t)
                    elif t + dur <= win_start:
                        # Still short of the window (the week jump lands
                        # within a fortnight of it): bounded so an old rule
                        # cannot march to a far-future UNTIL.
                        misses += 1
                        if misses > MAX_WINDOW_MISSES:
                            return out
            w += 1
    elif freq == "MONTHLY":
        wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        byday = parts.get("BYDAY", "").strip()
        monthday = parts.get("BYMONTHDAY", "").strip()
        nth_days: "list[tuple[int, int]] | None" = None
        if byday:
            nth_days = []
            for d in byday.split(","):
                m_ = re.fullmatch(r"([+-]?\d+)([A-Z]{2})", d.strip())
                if not m_ or m_.group(2) not in wd:
                    return one   # ordinal-less BYDAY in MONTHLY: fallback
                nth_days.append((int(m_.group(1)), wd[m_.group(2)]))
        # BYMONTHDAY parsed ONCE, before the jump: the not-a-number refusal
        # used to live inside the month loop and fires on the same first bad
        # value either way, but the candidates helper below wants numbers.
        month_numbers: "list[int] | None" = None
        if not nth_days and monthday:
            month_numbers = []
            for s_ in monthday.split(","):
                try:
                    month_numbers.append(int(s_))
                except ValueError:
                    return one

        def month_candidates(year: int, month: int) -> list:
            """The rule's occurrences inside one month, at DTSTART's time."""
            cands: list = []
            if nth_days:
                for nth, day in nth_days:
                    t = _ics_nth_weekday(year, month, nth, day, dtstart)
                    if t:
                        cands.append(t)
            elif month_numbers is not None:
                for n in month_numbers:
                    t = _ics_month_day(year, month, n, dtstart)
                    if t:
                        cands.append(t)
            else:
                # RFC: no BY* -> repeat DTSTART's day-of-month; months
                # lacking that day (31st in February) have no occurrence.
                t = _ics_month_day(year, month, dtstart.day, dtstart)
                if t:
                    cands.append(t)
            # A date the rule names twice is ONE instance (RFC 5545: duplicates
            # are ignored): BYMONTHDAY=26,-3 is the same day in February, and
            # `1FR,1FR` is one Friday. Left in, the meeting was listed twice
            # and COUNT was spent on both.
            return sorted(set(cands))

        m = 0
        # Jump straight to the window — the DAILY/WEEKLY family: stepping from
        # DTSTART one interval at a time burned the 240-month cap on months
        # nobody asked about, so a monthly event created in 2000 and queried in
        # 2026 enumerated 20 years and returned no events at all for a meeting
        # that still happens every month. The first interval step that could
        # still overlap the window is derived arithmetically (one step early,
        # because a month's candidates can sit anywhere inside it).
        anchor = win_start - dur
        if anchor > dtstart:
            months_ahead = (anchor.year - dtstart.year) * 12 \
                + (anchor.month - dtstart.month)
            if months_ahead > 0:
                m = max(0, months_ahead // interval - 1)
                # COUNT IS COUNTED FROM DTSTART, INCLUDING THE SKIPPED MONTHS —
                # the same correction the WEEKLY jump makes in weeks. Every
                # candidate in a skipped month is after DTSTART (they all sit
                # in a later month), so the loop below would have incremented
                # `k` for each; crediting the same count keeps COUNT's RFC
                # meaning (total instances since DTSTART, incl. DTSTART).
                for step in range(m):
                    skipped = _ics_add_months(dtstart, step * interval)
                    # Only candidates ON or AFTER DTSTART are instances: the
                    # first month's earlier days ("the 1st" for a rule whose
                    # DTSTART is the 15th) do not exist, so crediting them
                    # spent COUNT on days that never happened.
                    k += sum(1 for c in month_candidates(skipped.year,
                                                         skipped.month)
                             if c >= dtstart)
        # Caps the WORK, not the distance from DTSTART (the jump put the
        # window in reach): every interval step the window itself spans, plus
        # the step the jump may have landed early and the one the window's
        # own first candidate can sit past — with the old 240 kept as the
        # absolute backstop a pathological rule can never exceed.
        span = ((win_end.year - anchor.year) * 12
                + (win_end.month - anchor.month))
        m_cap = m + max(1, min(span // interval + 3, 240))
        while m < m_cap and k < count:
            base = _ics_add_months(dtstart, m * interval)
            # The MONTH's first instant is the bound, not `base`. `base` is
            # DTSTART's own day-of-month carried into this month, and a rule's
            # days are not DTSTART's day: "the last Friday" first met on the
            # 30th has its February instance on the 27th, "the 1st and 15th"
            # started on the 15th has its March instance on the 1st — both
            # BEFORE `base`, so `base > win_end` ended the search a day short
            # and the meeting read as "no events" on the very day it happens.
            month_start = base.replace(day=1, hour=0, minute=0, second=0,
                                       microsecond=0)
            if month_start >= win_end:
                break
            for t in month_candidates(base.year, base.month):
                if t < dtstart:
                    continue        # before DTSTART is not an instance
                if k >= count:      # same mid-batch cap as WEEKLY's days
                    break
                k += 1
                if want(t):
                    out.append(t)
            m += 1
    elif freq == "YEARLY":
        months: list[int] = []
        for x in parts.get("BYMONTH", "").split(","):
            x = x.strip()
            if not x:
                continue
            if not x.lstrip("-").isdigit():
                return one
            months.append(int(x))
        monthday = parts.get("BYMONTHDAY", "").strip()
        # Parsed once, before the jump (see the MONTHLY branch): a value that
        # is not a number is the same refusal it always was.
        month_numbers: "list[int] | None" = None
        if monthday:
            month_numbers = []
            for s_ in monthday.split(","):
                try:
                    month_numbers.append(int(s_))
                except ValueError:
                    return one

        def year_candidates(year: int) -> list:
            """The rule's occurrences inside one year, at DTSTART's time."""
            cands: list = []
            for mo in months or [dtstart.month]:
                if not 1 <= mo <= 12:
                    continue
                if month_numbers is not None:
                    for n in month_numbers:
                        t = _ics_month_day(year, mo, n, dtstart)
                        if t:
                            cands.append(t)
                else:
                    try:
                        cands.append(dtstart.replace(year=year, month=mo))
                    except ValueError:
                        pass   # Feb 29 in a non-leap year: no occurrence
            return sorted(set(cands))     # a date named twice is one instance

        y = 0
        # Jump straight to the window — the same family as DAILY/WEEKLY/
        # MONTHLY: a yearly event created in 1990 enumerated its 20-year cap
        # on years nobody asked about and returned no events at all.
        anchor = win_start - dur
        if anchor > dtstart:
            years_ahead = anchor.year - dtstart.year
            if years_ahead > 0:
                y = max(0, years_ahead // interval - 1)
                # COUNT IS COUNTED FROM DTSTART (the WEEKLY/MONTHLY
                # correction): every candidate in a skipped year is in a
                # later year than DTSTART's, so the loop below would have
                # counted each one.
                for step in range(y):
                    # Instances only: candidates before DTSTART (BYMONTH=4,11
                    # with a December DTSTART names two of them in DTSTART's
                    # own year) never happened and are not counted.
                    k += sum(1 for c in year_candidates(
                        dtstart.year + step * interval) if c >= dtstart)
        # Caps the WORK, not the distance from DTSTART: the years the window
        # itself spans, plus slack — the old 20 kept as the absolute backstop.
        span = win_end.year - anchor.year
        y_cap = y + max(1, min(span // interval + 2, 20))
        while y < y_cap and k < count:
            year = dtstart.year + y * interval
            if year > win_end.year:
                break
            for t in year_candidates(year):
                if t < dtstart:
                    continue        # before DTSTART is not an instance
                if k >= count:      # same mid-batch cap as WEEKLY's days
                    break
                k += 1
                if want(t):
                    out.append(t)
            y += 1
    else:
        return one
    return out


def _ics_duration(value: str) -> "datetime.timedelta | None":
    """RFC 5545 DURATION (`P1DT2H30M`, `PT45M`, `PT0.5H`, `P2W`) → timedelta.

    A VEVENT may carry DURATION INSTEAD of DTEND. Ignoring it (the old
    behaviour) fabricated a one-hour duration for every such event, so a
    two-hour class covered the wrong window and any event whose real duration
    did not overlap the queried range was still reported as if it did.

    A decimal fraction is ISO 8601-legal on the smallest component
    (`PT0.5H` is thirty minutes) — RFC 5545's own ABNF has no decimals, but
    real-world generators emit both shapes, so accepting them with EXACT
    arithmetic is strictly safer than refusing. Whatever the citation, the
    old chunk regex skipped the `.`, so `0.5H` was read as the chunk `5H`: a
    30-minute meeting became FIVE HOURS and `PT1H30M15.5S` lost 10.5 seconds.
    Decimals are accepted per chunk and the timedelta arithmetic keeps the
    exact value; a chunk that is not a clean number+unit is refused rather
    than partially parsed.
    """
    text = str(value or "").strip().upper()
    # A leading sign is RFC 5545's "this duration points BACKWARDS" (a reminder
    # before the event), which this model has no place for — and the chunk
    # parser below never saw the sign, so "P-1D" came back as +1 day and
    # "PT-5M" as +5 minutes: a negative duration silently became positive and
    # shifted the overlap window the wrong way. Refused rather than guessed.
    if not text.startswith("P") or text.endswith("T") or "-" in text or "+" in text:
        return None
    date_part, _, time_part = text[1:].partition("T")
    units = {"W": "weeks", "D": "days"}
    total = datetime.timedelta()
    seen = False

    # One chunk = a (possibly decimal) number followed by its unit letter. The
    # digits and fraction belong to the unit that FOLLOWS them, so `15.5S`
    # parses as 15.5 seconds and `0.5H` as half an hour — while a stray second
    # dot or a bare letter refuses the whole value rather than being silently
    # skipped (the old `\d*[A-Z]` findall skipped exactly that `.`).
    _chunk = re.compile(r"(\d+(?:\.\d+)?)([A-Z])")

    def _tokens(part: str) -> "list[str] | None":
        """The duration chunks of one part, or None when it is malformed."""
        tokens, pos = [], 0
        for m in _chunk.finditer(part):
            if part[pos:m.start()]:
                return None              # junk between chunks (`1H.5M`)
            tokens.append(m.group(0))
            pos = m.end()
        if part[pos:]:
            return None                  # trailing junk (a lone `.`, letters)
        return tokens

    def _num(chunk: str, table: dict) -> bool:
        nonlocal total, seen
        if not chunk:
            return True
        m = _chunk.fullmatch(chunk)
        if not m or m.group(2) not in table:
            return False
        # Fractions are RFC-legal only on the smallest unit of the part, but
        # timedelta accepts them anywhere and refusing a legal-if-unusual
        # `P0.5W` buys nothing — the VALUE is what the overlap window needs.
        total += datetime.timedelta(**{table[m.group(2)]: float(m.group(1))})
        seen = True
        return True

    date_tokens = _tokens(date_part)
    if date_tokens is None or not all(_num(c, units) for c in date_tokens):
        return None
    _t = {"H": "hours", "M": "minutes", "S": "seconds"}
    time_tokens = _tokens(time_part)
    if time_tokens is None or not all(_num(c, _t) for c in time_tokens):
        return None
    return total if seen else None


def ics_events_from_text(text: str, win_start: "datetime.datetime",
                          win_end: "datetime.datetime") -> list[dict]:
    """Parse VEVENTs overlapping [win_start, win_end); expands recurrences.

    Handles the two RFC 5545 overlap rules that calendars (Google, Nextcloud)
    actually emit:
    - EXDATE lines cancel specific instances of the recurrence;
    - a later VEVENT with a RECURRENCE-ID is a per-instance override of the
      master event sharing its UID: STATUS:CANCELLED removes that instance,
      anything else (moved time, new summary) replaces it.
    Overrides whose UID has no master in the file degrade to standalone
    events (their RECURRENCE-ID then equals their own start — harmless)."""
    if win_start.tzinfo is None:
        win_start = win_start.astimezone()
    if win_end.tzinfo is None:
        win_end = win_end.astimezone()

    props = ("SUMMARY", "LOCATION", "DTSTART", "DTEND", "DURATION", "RRULE",
             "EXDATE", "RECURRENCE-ID", "UID", "STATUS")
    raws: list[dict] = []          # every VEVENT in file order
    cur: dict | None = None
    for ln in _ics_unfold(text):
        # Case-insensitive on purpose: RFC 5545 names are case-insensitive and
        # real producers emit lowercase ("begin:vevent") — the old exact match
        # read such a whole file as containing no events at all.
        if ln.strip().upper() == "BEGIN:VEVENT":
            cur = {"EXDATE": []}
        elif ln.strip().upper() == "END:VEVENT":
            if cur:
                raws.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            name = ln.split(":", 1)[0].split(";")[0].strip().upper()
            if name in props:
                if name == "EXDATE":
                    cur["EXDATE"].append(ln)   # repeated lines accumulate
                elif name not in cur:
                    # date/rrule/id fields keep the FULL line (TZID/VALUE
                    # params matter for parsing); text fields keep the value
                    cur[name] = ln if name in ("DTSTART", "DTEND", "RRULE",
                                               "RECURRENCE-ID") \
                        else ln.split(":", 1)[1].strip()
                    # DURATION is stored as its bare value (`PT1H30M`), not a
                    # full line: it has no property parameters this parser
                    # needs, and `_ics_duration` reads it on its own.

    # masters: VEVENTs without RECURRENCE-ID (overrides are per-instance and
    # never the recurrence itself); drop duplicate-UID re-exports
    masters: list[dict] = []
    seen_uids: set[str] = set()
    for e in raws:
        if e.get("RECURRENCE-ID"):
            continue
        uid = e.get("UID")
        if uid:
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
        masters.append(e)

    # overrides keyed by UID: instance datetime → cancellation / replacement
    cancelled: dict[str, set] = {}
    moved: dict[str, dict] = {}
    for e in raws:
        when = _ics_parse_dt(e.get("RECURRENCE-ID", ""))
        if when is None:
            continue
        uid = e.get("UID", "")
        if (e.get("STATUS") or "").strip().upper() == "CANCELLED":
            cancelled.setdefault(uid, set()).add(when)
        else:
            moved.setdefault(uid, {})[when] = e

    events: list[dict] = []
    zone_skips = 0
    for e in masters:
        ds, zone_failed = _ics_parse_dt_checked(e.get("DTSTART", ""))
        if ds is None:
            if zone_failed:
                zone_skips += 1
            continue
        de = _ics_parse_dt(e.get("DTEND", ""))
        dur = (de - ds) if de is not None else \
            (_ics_duration(e.get("DURATION", "")) or datetime.timedelta(0))
        if dur <= datetime.timedelta(0):
            dur = datetime.timedelta(hours=1)
        rrule = (e.get("RRULE") or "").split(":", 1)[-1]

        # instances to suppress: EXDATEs, overridden/canceled RECURRENCE-IDs.
        # The EXDATE head params (e.g. TZID) apply to every comma value.
        uid = e.get("UID", "")
        skip = set(cancelled.get(uid, ()))
        skip.update(moved.get(uid, {}).keys())
        for xl in e.get("EXDATE", []):
            head, sep, values = xl.partition(":")
            for piece in values.split(","):
                piece = piece.strip()
                if piece:
                    xd = _ics_parse_dt(f"{head}:{piece}" if sep else piece)
                    if xd is not None:
                        skip.add(xd)

        for start in _ics_expand(ds, rrule, win_start, win_end, dur):
            if start in skip:
                continue
            events.append({
                # Back to the SYSTEM frame for the answer: the recurrence was
                # expanded in the event's own zone (the arithmetic above), and
                # what a person asked about is their own clock — the frame
                # this function always returned.
                "start": start.astimezone(), "dur": dur,
                "summary": e.get("SUMMARY") or "(no title)",
                "location": e.get("LOCATION", ""),
                "allday": _ics_allday(e.get("DTSTART", "")),
            })

    # moved overrides surface at their NEW time (if inside the window)
    for bywhen in moved.values():
        for ov in bywhen.values():
            ods, ov_zone_failed = _ics_parse_dt_checked(ov.get("DTSTART", ""))
            if ods is None:
                if ov_zone_failed:
                    zone_skips += 1
                continue
            if not (win_start <= ods < win_end):
                continue
            ode = _ics_parse_dt(ov.get("DTEND", ""))
            odur = (ode - ods) if ode is not None else \
                (_ics_duration(ov.get("DURATION", "")) or datetime.timedelta(0))
            if odur <= datetime.timedelta(0):
                odur = datetime.timedelta(hours=1)
            events.append({
                "start": ods.astimezone(), "dur": odur,
                "summary": ov.get("SUMMARY") or "(no title)",
                "location": ov.get("LOCATION", ""),
                "allday": _ics_allday(ov.get("DTSTART", "")),
            })
    if zone_skips:
        # An event dropped here used to vanish without a trace: the line was
        # valid, its zone just is not in this system's tzdata (a thin
        # container, a moved install). One warning naming the count is the
        # honest signal — silent loss read as "no events today".
        log.warning(
            "calendar: %d event(s) skipped — their TZID names a zone this "
            "system has no tzdata for", zone_skips)
    return events


def fmt_events(events: list[dict]) -> str:
    """'Mon 07 Sep 09:00–10:00: Team sync @ Teams' — locale-independent."""
    lines = []
    for e in sorted(events, key=lambda x: x["start"])[:40]:
        s = e["start"]
        day = (f"{DAY_NAMES[s.weekday()][:3]} {s.day:02d} "
               f"{MONTH_NAMES[s.month - 1][:3]}")
        when = "all day" if e["allday"] else f"{s.hour:02d}:{s.minute:02d}"
        if not e["allday"] and e["dur"] > datetime.timedelta(0):
            end = s + e["dur"]
            if end.date() == s.date():
                when += f"\u2013{end.hour:02d}:{end.minute:02d}"
        loc = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"{day} {when}: {e['summary']}{loc}")
    return "; ".join(lines)
