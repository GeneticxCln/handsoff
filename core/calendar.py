"""ICS calendar parsing and formatting for handsoff (stdlib only).

Pure functions: no settings, no Qt, no Assistant. handsoff.py re-exports
every name so the `H.*` monkeypatch contract keeps working; core.tools
reaches them through its host dependencies object.
"""
from __future__ import annotations

import calendar
import datetime
import re
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path

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


def _ics_parse_dt(prop: str) -> "datetime.datetime | None":
    """Parse a DTSTART/DTEND property → local datetime (None on garbage).

    Handles 'YYYYMMDDTHHMMSSZ' (UTC), ';TZID=…' (zoneinfo), floating local
    time, and all-day 'VALUE=DATE' / 8-digit dates (local midnight)."""
    if ":" not in prop:
        return None
    head, _, value = prop.partition(":")
    value = value.strip()
    params = dict(p.split("=", 1) for p in head.split(";")[1:] if "=" in p)
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.datetime.strptime(value[:8], "%Y%m%d").astimezone()
        dt = datetime.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        elif (tzid := params.get("TZID")):
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo(tzid))
        return dt.astimezone()
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return None


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
                first_index = gap // step + 1
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
        days = [wd[d] for d in parts.get("BYDAY", "").split(",")
                if d in wd] or [dtstart.weekday()]
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
        m = 0
        while m < 240 and k < count:
            base = _ics_add_months(dtstart, m * interval)
            if base > win_end:
                break
            cands: list = []
            if nth_days:
                for nth, day in nth_days:
                    t = _ics_nth_weekday(base.year, base.month, nth, day,
                                         dtstart)
                    if t:
                        cands.append(t)
            elif monthday:
                for s_ in monthday.split(","):
                    try:
                        n = int(s_)
                    except ValueError:
                        return one
                    t = _ics_month_day(base.year, base.month, n, dtstart)
                    if t:
                        cands.append(t)
            else:
                # RFC: no BY* -> repeat DTSTART's day-of-month; months
                # lacking that day (31st in February) have no occurrence.
                t = _ics_month_day(base.year, base.month, dtstart.day, dtstart)
                if t:
                    cands.append(t)
            for t in sorted(cands):
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
        y = 0
        while y < 20 and k < count:
            year = dtstart.year + y * interval
            if year > win_end.year:
                break
            cands: list = []
            for mo in months or [dtstart.month]:
                if not 1 <= mo <= 12:
                    continue
                if monthday:
                    for s_ in monthday.split(","):
                        try:
                            n = int(s_)
                        except ValueError:
                            return one
                        t = _ics_month_day(year, mo, n, dtstart)
                        if t:
                            cands.append(t)
                else:
                    try:
                        cands.append(dtstart.replace(year=year, month=mo))
                    except ValueError:
                        pass   # Feb 29 in a non-leap year: no occurrence
            for t in sorted(cands):
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
        if ln.strip() == "BEGIN:VEVENT":
            cur = {"EXDATE": []}
        elif ln.strip() == "END:VEVENT":
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
    for e in masters:
        ds = _ics_parse_dt(e.get("DTSTART", ""))
        if ds is None:
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
                "start": start, "dur": dur,
                "summary": e.get("SUMMARY") or "(no title)",
                "location": e.get("LOCATION", ""),
                "allday": _ics_allday(e.get("DTSTART", "")),
            })

    # moved overrides surface at their NEW time (if inside the window)
    for bywhen in moved.values():
        for ov in bywhen.values():
            ods = _ics_parse_dt(ov.get("DTSTART", ""))
            if ods is None or not (win_start <= ods < win_end):
                continue
            ode = _ics_parse_dt(ov.get("DTEND", ""))
            odur = (ode - ods) if ode is not None else \
                (_ics_duration(ov.get("DURATION", "")) or datetime.timedelta(0))
            if odur <= datetime.timedelta(0):
                odur = datetime.timedelta(hours=1)
            events.append({
                "start": ods, "dur": odur,
                "summary": ov.get("SUMMARY") or "(no title)",
                "location": ov.get("LOCATION", ""),
                "allday": _ics_allday(ov.get("DTSTART", "")),
            })
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
