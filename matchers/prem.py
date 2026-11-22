"""Strict Gallagher PREM league replay matching; cup games remain unmatched."""
import datetime as dt
import re
import unicodedata

from matchers.pwr import fixture_date

SERIES_ID = "Rugby_Mens_Prem"
TEAMS = {
    "bath": "Bath", "bath rugby": "Bath",
    "bristol bears": "Bristol Bears", "bristol": "Bristol Bears",
    "exeter chiefs": "Exeter Chiefs", "exeter": "Exeter Chiefs",
    "gloucester": "Gloucester", "gloucester rugby": "Gloucester",
    "harlequins": "Harlequins", "quins": "Harlequins",
    "leicester tigers": "Leicester Tigers", "leicester": "Leicester Tigers",
    "newcastle": "Newcastle", "newcastle falcons": "Newcastle",
    "newcastle red bulls": "Newcastle",
    "northampton saints": "Northampton Saints", "northampton": "Northampton Saints",
    "sale sharks": "Sale Sharks", "sale": "Sale Sharks",
    "saracens": "Saracens", "sarries": "Saracens",
}
HEADLINE = re.compile(
    r"^\s*(.+?)\s+v(?:s\.?)?\s+(.+?)\s+-\s+Full Match Replay\s+-", re.I)


def normalize(text):
    if not isinstance(text, str):
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split())


def teams_from_title(title):
    match = HEADLINE.search(title or "")
    if not match:
        return None
    teams = (TEAMS.get(normalize(match.group(1))),
             TEAMS.get(normalize(match.group(2))))
    return teams if all(teams) and teams[0] != teams[1] else None


def match_release(release, episodes, tolerance_days=2):
    if release.get("available") is False:
        return {"status": "unavailable"}
    meta = release.get("metadata") or {}
    title = release.get("title", "")
    if meta.get("competition") != "league" or re.search(r"\bcup\b", title, re.I):
        return {"status": "not_league"}
    teams = teams_from_title(title)
    if not teams:
        return {"status": "not_prem_match"}
    try:
        date = dt.date.fromisoformat(release["published"])
    except (KeyError, TypeError, ValueError):
        return {"status": "undated", "teams": list(teams)}
    candidates = []
    for episode in episodes:
        if (episode.get("series_id") != SERIES_ID or
                episode.get("status") in ("cancelled", "canceled", "superseded") or
                not isinstance(episode.get("episode"), int)):
            continue
        if (TEAMS.get(normalize(episode.get("home"))),
                TEAMS.get(normalize(episode.get("away")))) != teams:
            continue
        scheduled = fixture_date(episode)
        if scheduled is not None and abs((date - scheduled).days) <= tolerance_days:
            candidates.append((episode, scheduled))
    if not candidates:
        return {"status": "no_fixture", "teams": list(teams),
                "recording_date": date.isoformat()}
    if len(candidates) > 1:
        return {"status": "ambiguous", "candidates": [e["_id"] for e, _ in candidates],
                "teams": list(teams), "recording_date": date.isoformat()}
    episode, scheduled = candidates[0]
    return {"status": "candidate", "episode_id": episode["_id"],
            "series_id": SERIES_ID, "season": episode["season"],
            "episode": episode["episode"], "teams": list(teams),
            "recording_date": date.isoformat(),
            "scheduled_date": scheduled.isoformat(),
            "delta_days": (date - scheduled).days}
