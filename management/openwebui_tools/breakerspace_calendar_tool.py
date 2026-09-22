"""
title: Breakerspace Calendar
author: Nicola Ferralis
version: 2026.9.18.1
license: MIT
description: Live lookup of MIT DMSE Breakerspace events — trainings, lab-assistant hours, lounge reservations — straight from the LibCal iCal feed. Answers date questions exactly, instead of relying on retrieval.
"""

# Install: Open WebUI → Workspace → Tools → "+" → paste this whole file → Save.
# Then enable it on your preset: Workspace → Models → <preset> → Tools → tick
# "Breakerspace Calendar". Function Calling must be Native (the default).
#
# Why a tool and not documents: "what's on Thursday?" is a DATE question, and vector
# retrieval is poor at dates — a note mentioning "25 September" is not reliably
# nearer to "Thursday" than one about "24 September". A tool filters by date
# exactly, is always current, and says plainly when nothing is scheduled, which is
# what stops a model from inventing an event to be helpful.
#
# Scope: EVENTS (trainings, lab-assistant hours, reservations/notices). Instrument
# BOOKINGS live in LibCal "spaces" and need LibCal's authenticated API — not here.
#
# Standard library only: nothing to pip-install inside the Open WebUI container.

import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field

try:                                    # present in Python 3.9+, but the container may
    from zoneinfo import ZoneInfo       # lack the tz database; there's a fallback below
except Exception:                       # pragma: no cover
    ZoneInfo = None


# ------------------------------------------------------------- iCal parsing

def _unfold(text):
    """RFC 5545: a line starting with a space or tab continues the previous one."""
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def _parse_ics(text):
    """[{summary, start, end, all_day, tzid, description, location, category, url,
    organizer, uid}] — a deliberately small parser: LibCal's feed is plain VEVENTs
    with no recurrence rules, and a dependency-free file is worth more here."""
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *params = head.split(";")
        p = dict(x.split("=", 1) for x in params if "=" in x)
        name = name.upper()
        if name in ("DTSTART", "DTEND"):
            cur[name] = (value.strip(), p)
        elif name == "ORGANIZER":
            cur["organizer"] = p.get("CN", "").strip('"') or value.replace("MAILTO:", "")
        else:
            cur[name.lower()] = _unescape(value.strip())
    return events


# ------------------------------------------------------------- time handling

def _second_sunday(year, month):
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
    return d + timedelta(days=7)


def _first_sunday(year, month):
    d = date(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


class _USEastern:
    """Fallback for containers without a tz database: US Eastern, DST from the
    second Sunday of March to the first Sunday of November (2:00 local)."""

    def utcoffset_for(self, utc_dt):
        y = utc_dt.year
        start = datetime.combine(_second_sunday(y, 3), datetime.min.time(),
                                 timezone.utc) + timedelta(hours=7)    # 02:00 EST
        end = datetime.combine(_first_sunday(y, 11), datetime.min.time(),
                               timezone.utc) + timedelta(hours=6)      # 02:00 EDT
        return timedelta(hours=-4) if start <= utc_dt < end else timedelta(hours=-5)

    def to_local(self, utc_dt):
        return (utc_dt + self.utcoffset_for(utc_dt)).replace(tzinfo=None)

    def now(self):
        return self.to_local(datetime.now(timezone.utc))


class _ZoneTZ:
    def __init__(self, zone):
        self.zone = zone

    def to_local(self, utc_dt):
        return utc_dt.astimezone(self.zone).replace(tzinfo=None)

    def now(self):
        return datetime.now(self.zone).replace(tzinfo=None)


def _tz(name):
    if ZoneInfo is not None:
        try:
            return _ZoneTZ(ZoneInfo(name))
        except Exception:
            pass
    return _USEastern()        # right for the Breakerspace; best effort elsewhere


def _to_local(value, params, tz):
    """(naive local datetime, all_day) from an iCal DTSTART/DTEND value."""
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    if value.endswith("Z"):
        utc = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return tz.to_local(utc), False
    # floating or TZID-qualified local time: LibCal only uses its own zone
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S"), False


# ------------------------------------------------------------- date phrases

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _window(when, now):
    """(start, end, label, understood) for a plain-language time window.

    The model is asked to pass the user's own words ("thursday", "next week") rather
    than compute dates, because models are unreliable about what today is. The tool
    knows the date; the model doesn't need to."""
    w = (when or "").strip().lower()
    today = datetime(now.year, now.month, now.day)
    day = timedelta(days=1)

    def span(a, b, label):
        return a, b, label, True

    if w in ("", "upcoming", "soon", "next", "coming up"):
        return span(now, today + 8 * day, "the next 7 days")
    if w in ("today", "tonight"):
        return span(today, today + day, "today")
    if w == "tomorrow":
        return span(today + day, today + 2 * day, "tomorrow")
    if w in ("this week", "week"):
        end = today + (7 - today.weekday()) * day            # through Sunday
        return span(today, end, "the rest of this week")
    if w == "next week":
        start = today + (7 - today.weekday()) * day           # next Monday
        return span(start, start + 7 * day, "next week")
    if w in ("this weekend", "weekend"):
        sat = today + ((5 - today.weekday()) % 7) * day
        return span(sat, sat + 2 * day, "this weekend")
    if w in ("this month", "month"):
        nxt = datetime(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return span(today, nxt, "the rest of this month")
    m = re.fullmatch(r"(?:next|coming)?\s*(\d{1,3})\s*days?", w)
    if m:
        n = int(m.group(1))
        return span(now, today + (n + 1) * day, f"the next {n} days")
    m = re.fullmatch(r"(next\s+|this\s+)?(" + "|".join(_WEEKDAYS) + r"|mon|tue|tues|wed|"
                     r"thu|thur|thurs|fri|sat|sun)", w)
    if m:
        key = m.group(2)
        idx = next(i for i, d in enumerate(_WEEKDAYS) if d.startswith(key[:3]))
        ahead = (idx - today.weekday()) % 7
        if m.group(1) and m.group(1).strip() == "next" and ahead == 0:
            ahead = 7
        d = today + ahead * day
        return span(d, d + day, d.strftime("%A %-d %B %Y"))
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:to|until|through|-|–|\.\.)\s*(\d{4}-\d{2}-\d{2})", w)
    if m:
        a = datetime.fromisoformat(m.group(1))
        b = datetime.fromisoformat(m.group(2)) + day
        return span(a, b, f"{m.group(1)} to {m.group(2)}")
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}", w)
    if m:
        d = datetime.fromisoformat(w)
        return span(d, d + day, d.strftime("%A %-d %B %Y"))
    if w in _MONTHS or w[:3] in [x[:3] for x in _MONTHS] and len(w) <= 4:
        mi = next(i for i, x in enumerate(_MONTHS) if x.startswith(w[:3])) + 1
        year = today.year + (mi < today.month)
        start = datetime(year, mi, 1)
        end = datetime(year + (mi == 12), mi % 12 + 1, 1)
        return span(start, end, start.strftime("%B %Y"))
    # Not understood: fall back to a sensible default AND say so, so the model can
    # retry with an ISO date instead of presenting the default as the answer.
    return now, today + 8 * day, "the next 7 days", False


# ------------------------------------------------------------- the tool

class Tools:
    class Valves(BaseModel):
        ICAL_URL: str = Field(
            default="https://breakerspace.libcal.com/ical_subscribe.php?cid=19408",
            description="LibCal iCal feed (Calendar page → iCal → subscription link)")
        TIMEZONE: str = Field(default="America/New_York",
                              description="Local time zone for dates and times")
        CACHE_SECONDS: int = Field(default=900,
                                   description="Re-fetch at most this often (the feed "
                                               "publishes a 15-minute refresh)")
        MAX_EVENTS: int = Field(default=25, description="Cap on events returned")
        DESCRIPTION_CHARS: int = Field(default=280,
                                       description="Description text per event")

    def __init__(self):
        self.valves = self.Valves()
        self._cache = (0.0, None)

    def _events(self):
        fetched, events = self._cache
        if events is not None and time.time() - fetched < self.valves.CACHE_SECONDS:
            return events, fetched
        req = urllib.request.Request(self.valves.ICAL_URL,
                                     headers={"User-Agent": "OpenWebUI-BreakerspaceCalendar"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        events = _parse_ics(text)
        self._cache = (time.time(), events)
        return events, self._cache[0]

    async def breakerspace_calendar(self, when: str = "upcoming", category: str = "",
                                    keyword: str = "", __event_emitter__=None) -> str:
        """
        Look up MIT DMSE Breakerspace events from the official LibCal calendar:
        instrument trainings, lab-assistant hours, lab tours, and lounge or room
        reservations. Use this for ANY question about when something happens at the
        Breakerspace, what is scheduled, or who is on duty — do not answer those from
        memory or from documents. It does not cover individual instrument bookings.

        :param when: The time window in the user's own words. Accepts: "upcoming", "today", "tomorrow", a weekday such as "thursday" or "next tuesday", "this week", "next week", "this weekend", "next 14 days", a month such as "october", an ISO date "2026-09-25", or a range "2026-09-25 to 2026-10-02". Prefer passing the user's words over computing a date yourself.
        :param category: Optional filter on the event category, e.g. "training", "lab assistant", "reserved". Leave empty for all.
        :param keyword: Optional word to match in the title, description or location, e.g. "SEM", "XRD", "Raman", "lounge". Leave empty for all.
        :return: The matching events with local dates and times, or an explicit statement that none are scheduled in that window.
        """
        tz = _tz(self.valves.TIMEZONE)
        now = tz.now()

        async def status(msg, done=False):
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                                         "data": {"description": msg, "done": done}})

        await status("Checking the Breakerspace calendar…")
        try:
            events, fetched = self._events()
        except Exception as e:
            await status("Calendar unavailable", True)
            return (f"The Breakerspace calendar could not be reached ({type(e).__name__}: "
                    f"{e}). Tell the user it is unavailable right now and point them to "
                    f"https://breakerspace.mit.edu/calendar.html — do not guess events.")

        start, end, label, understood = _window(when, now)
        cat, kw = category.strip().lower(), keyword.strip().lower()
        rows = []
        for ev in events:
            if "DTSTART" not in ev:
                continue
            s, all_day = _to_local(*ev["DTSTART"], tz)
            e = _to_local(*ev["DTEND"], tz)[0] if "DTEND" in ev else s
            if e <= start or s >= end:          # overlaps the window at all?
                continue
            if cat and cat not in ev.get("categories", "").lower():
                continue
            hay = " ".join(ev.get(k, "") for k in ("summary", "description", "location")).lower()
            if kw and kw not in hay:
                continue
            rows.append((s, e, all_day, ev))
        rows.sort(key=lambda r: r[0])

        header = [f"Breakerspace calendar (LibCal). Today is {now:%A %-d %B %Y}, "
                  f"{now:%H:%M} local time ({self.valves.TIMEZONE}).",
                  f"Window: {label}."]
        if not understood:
            header.append(f'Note: "{when}" was not understood; showing {label}. Retry '
                          f'with an ISO date like 2026-09-25 if that is not what the '
                          f'user meant.')
        if cat or kw:
            header.append("Filters: " + ", ".join(
                x for x in (f"category contains '{category}'" if cat else "",
                            f"text contains '{keyword}'" if kw else "") if x) + ".")

        if not rows:
            await status("No events in that window", True)
            return "\n".join(header + [
                "No events are scheduled in this window. Say so plainly; do not invent "
                "events. Instrument bookings are not part of this calendar."])

        shown = rows[:self.valves.MAX_EVENTS]
        lines = header + [f"{len(rows)} event(s) found"
                          + (f", showing the first {len(shown)}" if len(rows) > len(shown) else "")
                          + ":"]
        for s, e, all_day, ev in shown:
            if all_day:
                when_txt = f"{s:%a %-d %b %Y} (all day)"
            elif s.date() == e.date():
                when_txt = f"{s:%a %-d %b %Y}, {s:%H:%M}–{e:%H:%M}"
            else:
                when_txt = f"{s:%a %-d %b %H:%M} – {e:%a %-d %b %H:%M}"
            lines.append(f"- {when_txt} · {ev.get('summary', '(untitled)')}")
            meta = [x for x in (ev.get("location"), ev.get("categories"),
                                f"contact {ev['organizer']}" if ev.get("organizer") else "")
                    if x]
            if meta:
                lines.append(f"  {' · '.join(meta)}")
            desc = re.sub(r"\s+", " ", ev.get("description", "")).strip()
            if desc:
                cut = self.valves.DESCRIPTION_CHARS
                lines.append(f"  {desc[:cut]}{'…' if len(desc) > cut else ''}")
            if ev.get("url"):
                lines.append(f"  {ev['url']}")
        age = int((time.time() - fetched) / 60)
        lines.append(f"(Source: LibCal feed, fetched {age} min ago. Give times in local "
                     f"time as listed and include the event link when useful.)")
        await status(f"Found {len(rows)} event(s)", True)
        return "\n".join(lines)
