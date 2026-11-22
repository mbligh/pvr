"""Conservative Six Nations fixture-round numbering.

Only assign new round numbers when the source contains a complete six-team,
five-round, 15-match round robin in a calendar year. Public ICS feeds lack
round labels, so simply counting weeks in an incomplete feed is unsafe.
"""
import datetime as dt
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
VERSUS = re.compile(r"\s+(?:vs?\.?|versus)\s+", re.I)


def team_key(value):
    # Source ICS decorations (flag emoji, gender words) are not identifiers.
    # Strip symbols; keep "Women" to distinguish this series if necessary.
    return re.sub(r"[^a-z]", "", value.casefold())


def fixture_rounds(calendar):
    """Return UID -> round for fully validated calendar years only.

    This assigns no new episodes for incomplete, duplicate, or irregular
    source schedules. Previously stored assignments are handled by the caller.
    """
    by_year = defaultdict(list)
    for event in calendar.walk("VEVENT"):
        if "DTSTART" not in event:
            continue
        start = event.decoded("DTSTART")
        if not isinstance(start, dt.datetime):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UK)
        local = start.astimezone(UK)
        uid = str(event.get("UID", "")).strip()
        teams = VERSUS.split(str(event.get("SUMMARY", "")), maxsplit=1)
        if not uid or len(teams) != 2:
            continue
        a, b = (team_key(team) for team in teams)
        if not a or not b or a == b:
            continue
        monday = local.date() - dt.timedelta(days=local.weekday())
        by_year[local.year].append((uid, a, b, monday))

    rounds = {}
    for year, matches in by_year.items():
        if len(matches) != 15 or len({match[0] for match in matches}) != 15:
            continue
        weeks = defaultdict(list)
        all_teams = set()
        all_pairs = set()
        for uid, a, b, monday in matches:
            weeks[monday].append((uid, a, b))
            all_teams.update((a, b))
            all_pairs.add(frozenset((a, b)))
        # 15 unique pairs among exactly six teams = a complete round robin.
        if len(all_teams) != 6 or len(all_pairs) != 15 or len(weeks) != 5:
            continue
        if any(len(items) != 3 or
               len({team for _, a, b in items for team in (a, b)}) != 6
               for items in weeks.values()):
            continue
        for round_no, monday in enumerate(sorted(weeks), 1):
            rounds.update((uid, round_no) for uid, _, _ in weeks[monday])
    return rounds
