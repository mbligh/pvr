"""Match Rugby24 senior men's Six Nations replays to canonical fixtures."""
import datetime as dt
import re

from matchers.pwr import fixture_date
from matchers.rugbypass import normalize_team

SERIES_ID = "Rugby_Mens_6N"
TEAMS = {"england", "france", "ireland", "italy", "scotland", "wales"}
SIX = re.compile(r"\b(?:six|6)\s+nations\b", re.I)
EXCLUDED = re.compile(r"\b(?:women(?:[’']?s)?|u\s*\d{2}|under[- ]?\d{2}|summer series)\b", re.I)
FULL = re.compile(r"\bfull match replay\b", re.I)
SEPARATOR = re.compile(r"\s+v(?:s\.?)?\s+", re.I)
# Actual Rugby24 headings include "France vs England Six Nations Rugby
# Full Match Replay 14 March 2026", as well as the newer dashed format.
BOUNDARY = re.compile(
    r"\s+(?:-\s+|(?:six|6)\s+nations\b|\d{1,2}\s+[A-Za-z]+\s+\d{4}\b|full match replay\b)",
    re.I)


def team(value):
    value = normalize_team(value)
    return value if value in TEAMS else None


def teams_from_title(title):
    if not SIX.search(title) or not FULL.search(title) or EXCLUDED.search(title):
        return None
    sides = SEPARATOR.split(BOUNDARY.split(title, maxsplit=1)[0].strip())
    if len(sides) != 2:
        return None
    teams = tuple(team(side) for side in sides)
    return teams if all(teams) and teams[0] != teams[1] else None


def match_release(release, episodes, tolerance_days=2):
    if release.get("available") is False:
        return {"status": "unavailable"}
    title = release.get("title") or ""
    meta = release.get("metadata") or {}
    if (release.get("source") != "rugby24"
            or meta.get("competition") != "sixnations"
            or not SIX.search(title) or EXCLUDED.search(title)):
        return {"status": "not_sixnations"}
    teams = teams_from_title(title)
    if not teams:
        return {"status": "not_sixnations_match"}
    try:
        date = dt.date.fromisoformat(release["published"])
    except (KeyError, ValueError, TypeError):
        return {"status": "undated", "teams": list(teams)}
    candidates = []
    for episode in episodes:
        if (episode.get("series_id") != SERIES_ID
                or episode.get("status") in ("cancelled", "canceled", "superseded")
                or not isinstance(episode.get("episode"), int)):
            continue
        sides = (team(episode.get("home")), team(episode.get("away")))
        if sides != teams:
            continue
        scheduled = fixture_date(episode)
        if scheduled is not None and abs((date - scheduled).days) <= tolerance_days:
            candidates.append((episode, scheduled))
    if not candidates:
        return {"status": "no_fixture", "teams": list(teams),
                "recording_date": date.isoformat()}
    if len(candidates) > 1:
        return {"status": "ambiguous", "teams": list(teams),
                "recording_date": date.isoformat(),
                "candidates": [e["_id"] for e, _ in candidates]}
    episode, scheduled = candidates[0]
    return {"status": "candidate", "episode_id": episode["_id"],
            "series_id": SERIES_ID, "season": episode["season"],
            "episode": episode["episode"], "teams": list(teams),
            "recording_date": date.isoformat(),
            "scheduled_date": scheduled.isoformat(),
            "delta_days": (date - scheduled).days}
