"""Match Rugby24 Champions Cup full-match replays to official EPCR fixtures.

A recording must explicitly identify the European Champions Cup, and the
home team, away team and recording date must identify exactly one fixture.
No fuzzy or cross-series matches, and no assumptions based on a round alone.
"""
import datetime as dt
import re

from matchers.prem import normalize
from matchers.pwr import fixture_date

SERIES_ID = "Rugby_Mens_Championship"
# The spellings on Rugby24 and the 2026/27 official EPCR schedule differ.
# Values are canonical comparison keys, not display-name replacements.
TEAMS = {
    "10bet lions": "lions", "lions": "lions",
    "bath": "bath", "bath rugby": "bath",
    "bordeaux begles": "bordeaux", "union bordeaux begles": "bordeaux",
    "bristol": "bristol", "bristol bears": "bristol",
    "cardiff": "cardiff", "cardiff rugby": "cardiff",
    "clermont": "clermont", "clermont auvergne": "clermont",
    "connacht": "connacht", "connacht rugby": "connacht",
    "dhl stormers": "stormers", "stormers": "stormers",
    "exeter": "exeter", "exeter chiefs": "exeter",
    "glasgow": "glasgow", "glasgow warriors": "glasgow",
    "gloucester": "gloucester", "gloucester rugby": "gloucester",
    "la rochelle": "la rochelle",
    "leicester": "leicester", "leicester tigers": "leicester",
    "leinster": "leinster", "leinster rugby": "leinster",
    "montpellier": "montpellier",
    "munster": "munster", "munster rugby": "munster",
    "northampton": "northampton", "northampton saints": "northampton",
    "pau": "pau",
    "racing 92": "racing", "racing": "racing",
    "sale": "sale", "sale sharks": "sale",
    "saracens": "saracens",
    "stade francais": "stade francais",
    "stade francais paris": "stade francais",
    "toulouse": "toulouse",
    "vodacom bulls": "bulls", "bulls": "bulls",
}
# The site uses both the current "- Full Match Replay - DATE" convention
# and an older "DATE Full Match Replay European Champions Cup" heading.
CURRENT = re.compile(
    r"^\s*(.+?)\s+v(?:s\.?)?\s+(.+?)\s+-\s+Full Match Replay\s+-", re.I)
HISTORICAL = re.compile(
    r"^\s*(.+?)\s+v(?:s\.?)?\s+(.+?)\s+"
    r"\d{1,2}\s+[A-Za-z]+\s+\d{4}\s+Full Match Replay\b", re.I)
CHAMPIONS = re.compile(r"\b(?:european\s+)?champions\s+cup\b", re.I)
CHALLENGE = re.compile(r"\bchallenge\s+cup\b", re.I)


def team(value):
    return TEAMS.get(normalize(value))


def teams_from_title(title):
    text = str(title or "")
    found = CURRENT.search(text) or HISTORICAL.search(text)
    if found is None:
        return None
    sides = (team(found.group(1)), team(found.group(2)))
    if any(x is None for x in sides) or sides[0] == sides[1]:
        return None
    return sides


def match_release(release, episodes, tolerance_days=2):
    if release.get("available") is False:
        return {"status": "unavailable"}
    title = release.get("title") or ""
    meta = release.get("metadata") or {}
    if (meta.get("competition") != "champions-cup"
            or not CHAMPIONS.search(title) or CHALLENGE.search(title)):
        return {"status": "not_championship"}
    teams = teams_from_title(title)
    if not teams:
        return {"status": "not_championship_match"}
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
