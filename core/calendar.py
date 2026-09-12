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

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
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


def _ics_fetch(source: str) -> str:
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
    try:
        interval = max(1, int(parts.get("INTERVAL") or 1))
        count = int(parts.get("COUNT") or 500)
    except ValueError:
        return one
    # UNTIL is inclusive; a date-only UNTIL parses to that day's midnight.
    until: "datetime.datetime | None" = None
    if parts.get("UNTIL"):
        until = _ics_parse_dt(f"UNTIL:{parts['UNTIL']}")

    def want(t: "datetime.datetime") -> bool:
        if t < dtstart or t >= win_end or t + dur <= win_start:
            return False
        return until is None or t <= until

    out: list = []
    k = 0                                     # absolute instance counter
    if freq == "DAILY":
        i = 0
        while i < count:
            t = dtstart + datetime.timedelta(days=i * interval)
            if t >= win_end:
                break
            if want(t):
                out.append(t)
            i += 1
    elif freq == "WEEKLY":
        wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        days = [wd[d] for d in parts.get("BYDAY", "").split(",")
                if d in wd] or [dtstart.weekday()]
        week0 = dtstart - datetime.timedelta(days=dtstart.weekday())
        w = 0
        while w < 200 and k < count:
            base = week0 + datetime.timedelta(weeks=w * interval)
            if base > win_end:
                break
            for d in days:
                t = base + datetime.timedelta(
                    days=d, hours=dtstart.hour, minutes=dtstart.minute,
                    seconds=dtstart.second)
                if t >= dtstart:
                    k += 1
                    if want(t):
                        out.append(t)
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


def _ics_events_from_text(text: str, win_start: "datetime.datetime",
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

    props = ("SUMMARY", "LOCATION", "DTSTART", "DTEND", "RRULE",
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
        de = _ics_parse_dt(e.get("DTEND", "")) or ds
        dur = de - ds
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
            ode = _ics_parse_dt(ov.get("DTEND", "")) or ods
            odur = ode - ods
            if odur <= datetime.timedelta(0):
                odur = datetime.timedelta(hours=1)
            events.append({
                "start": ods, "dur": odur,
                "summary": ov.get("SUMMARY") or "(no title)",
                "location": ov.get("LOCATION", ""),
                "allday": _ics_allday(ov.get("DTSTART", "")),
            })
    return events


def _fmt_events(events: list[dict]) -> str:
    """'Mon 07 Sep 09:00–10:00: Team sync @ Teams' — locale-independent."""
    lines = []
    for e in sorted(events, key=lambda x: x["start"])[:40]:
        s = e["start"]
        day = (f"{_DAY_NAMES[s.weekday()][:3]} {s.day:02d} "
               f"{_MONTH_NAMES[s.month - 1][:3]}")
        when = "all day" if e["allday"] else f"{s.hour:02d}:{s.minute:02d}"
        if not e["allday"] and e["dur"] > datetime.timedelta(0):
            end = s + e["dur"]
            if end.date() == s.date():
                when += f"\u2013{end.hour:02d}:{end.minute:02d}"
        loc = f" @ {e['location']}" if e["location"] else ""
        lines.append(f"{day} {when}: {e['summary']}{loc}")
    return "; ".join(lines)
