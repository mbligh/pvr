"""Verified 2026 Nations Championship round assignments, score-free.

The official competition publishes six regular rounds (July 1-3 and
November 4-6) plus a six-match Finals Weekend labeled Round 7. Only
number a complete, validated six-match cross-hemisphere fixture week.
"""
import datetime as dt
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

from icalendar import Calendar

UK = ZoneInfo("Europe/London")
NORTH = frozenset(("england", "france", "ireland", "italy", "scotland", "wales"))
SOUTH = frozenset(("argentina", "australia", "fiji", "japan", "newzealand", "southafrica"))
# Monday of each published 2026 fixture weekend, from the official match centre.
WINDOWS_2026 = {
    dt.date(2026, 6, 29): 1,  # 4-5 July
    dt.date(2026, 7, 6): 2,   # 11-12 July
    dt.date(2026, 7, 13): 3,  # 18-19 July
    dt.date(2026, 11, 2): 4,  # 6-8 November
    dt.date(2026, 11, 9): 5,  # 13-15 November
    dt.date(2026, 11, 16): 6, # 21 November
    dt.date(2026, 11, 23): 7, # 27-29 November finals
}
ROUND_WINDOWS = {2026: WINDOWS_2026}
VERSUS = re.compile(r"\s+(?:vs?\.?|versus)\s+", re.I)


def team_key(value):
    key = re.sub(r"[^a-z0-9]", "", value.casefold())
    return {"allblacks": "newzealand", "wallabies": "australia",
            "springboks": "southafrica"}.get(key, key)


def verified_rounds(calendar):
    """Map UID -> official round; suppress incomplete or ambiguous windows.

    Never infer a round merely from chronological order or the existence
    of an unrelated past round. Ignore unsupported future tournament years
    until their official windows have been confirmed.
    """
    if not isinstance(calendar, Calendar):
        raise TypeError("Expected an iCalendar")
    groups = defaultdict(list)
    for event in calendar.walk("VEVENT"):
        if "DTSTART" not in event:
            continue
        when = event.decoded("DTSTART")
        if not isinstance(when, dt.datetime):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UK)
        day = when.astimezone(UK).date()
        monday = day - dt.timedelta(days=day.weekday())
        round_no = ROUND_WINDOWS.get(day.year, {}).get(monday)
        if round_no is None:
            continue
        uid = str(event.get("UID", "")).strip()
        teams = VERSUS.split(str(event.get("SUMMARY", "")), maxsplit=1)
        if not uid or len(teams) != 2:
            continue
        home, away = (team_key(t) for t in teams)
        groups[(day.year, round_no)].append((uid, home, away))

    result = {}
    for (_year, round_no), fixtures in groups.items():
        if len(fixtures) != 6:
            continue
        seen_uids = {uid for uid, _, _ in fixtures}
        teams = [t for _, a, b in fixtures for t in (a, b)]
        if (len(seen_uids) != 6 or len(set(teams)) != 12 or
                set(teams) != NORTH | SOUTH or
                any(not ((a in NORTH and b in SOUTH) or
                         (b in NORTH and a in SOUTH))
                    for _, a, b in fixtures)):
            continue
        result.update((uid, round_no) for uid, _, _ in fixtures)
    return result
