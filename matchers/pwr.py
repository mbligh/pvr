"""Premiership Women's Rugby release-to-fixture matching.

Pure functions only: no database access, network requests, or media downloads.
The fixture database supplies canonical seasons and episode numbers.
"""
import datetime as dt
import re
import unicodedata
from zoneinfo import ZoneInfo

SERIES_ID = "Rugby_Womens_PWR"
UK = ZoneInfo("Europe/London")

# Canonical names supplied for the PWR competition.
TEAMS = (
    "Bristol Bears",
    "Exeter Chiefs",
    "Gloucester-Hartpury",
    "Harlequins",
    "Leicester Tigers",
    "Loughborough Lightning",
    "Sale Sharks",
    "Saracens",
    "Trailfinders Women",
)

# Use known abbreviations and punctuation variants, not arbitrary edit-distance
# guesses which can silently turn unrelated teams into PWR participants.
ALIASES = {
    "Bristol Bears": ("Bristol Bears", "Bristol"),
    "Exeter Chiefs": ("Exeter Chiefs", "Exeter"),
    "Gloucester-Hartpury": ("Gloucester-Hartpury", "Gloucester Hartpury"),
    "Harlequins": ("Harlequins", "Quins"),
    "Leicester Tigers": ("Leicester Tigers", "Leicester"),
    "Loughborough Lightning": ("Loughborough Lightning", "Loughborough"),
    "Sale Sharks": ("Sale Sharks", "Sale"),
    "Saracens": ("Saracens", "Sarries"),
    "Trailfinders Women": ("Trailfinders Women", "Trailfinders", "Ealing Trailfinders"),
}

VERSUS = re.compile(r"(?<![a-z\d])(?:vs?\.?|versus)(?![a-z\d])", re.I)
NON_MATCH = re.compile(
    r"\b(?:highlights?|preview|trailer|interview|reaction|next[ -]?gen|"
    r"academy|under[ -]?\d\d|u\d\d)\b", re.I
)


def normalize(text):
    """Fold case, punctuation, accents and gender suffixes for team matching."""
    if not isinstance(text, str):
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).split()
    return " ".join(word for word in words if word not in ("women", "womens", "ladies"))


def known_team(value):
    """Resolve a known team name or exact documented alias."""
    value = normalize(value)
    return next((team for team, aliases in ALIASES.items()
                 if value in {normalize(a) for a in aliases}), None)


def side_team(fragment, side):
    """Recognize a PWR team at the end of the left or start of the right side."""
    fragment = normalize(fragment)
    candidates = set()
    for team, aliases in ALIASES.items():
        for alias in aliases:
            alias = normalize(alias)
            if side == "left":
                matched = fragment == alias or fragment.endswith(" " + alias)
            else:
                matched = fragment == alias or fragment.startswith(alias + " ")
            if matched:
                candidates.add(team)
    return next(iter(candidates)) if len(candidates) == 1 else None


def title_teams(title):
    if not isinstance(title, str) or NON_MATCH.search(title):
        return None
    separators = list(VERSUS.finditer(title))
    if len(separators) != 1:
        return None
    split = separators[0]
    home = side_team(title[:split.start()], "left")
    away = side_team(title[split.end():], "right")
    return (home, away) if home and away and home != away else None


def recording_date(release):
    """Interpret timestamps in UK time, where the PWR match was played."""
    value = release.get("published")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return timestamp.date()
    return timestamp.astimezone(UK).date()


def fixture_date(episode):
    scheduled = episode.get("scheduled")
    if isinstance(scheduled, dt.datetime):
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=dt.timezone.utc)
        return scheduled.astimezone(UK).date()
    if isinstance(scheduled, str):
        try:
            return dt.datetime.fromisoformat(
                scheduled.replace("Z", "+00:00")).astimezone(UK).date()
        except ValueError:
            pass
    # Date-only fixtures have no trustworthy kickoff time.
    value = episode.get("scheduled_date")
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def match_release(release, episodes, tolerance_days=2):
    """Return a match result; never invent season/episode or choose ties.

    A missing historical season means 'no_fixture', not a match in the
    available season. Preserve source dates as evidence only.
    """
    title = release.get("title", "")
    if release.get("available") is False:
        return {"status": "unavailable"}
    teams = title_teams(title)
    if teams is None:
        return {"status": "not_pwr_match"}
    date = recording_date(release)
    if date is None:
        return {"status": "undated", "teams": list(teams)}
    candidates = []
    for episode in episodes:
        if episode.get("series_id") != SERIES_ID:
            continue
        fixture_teams = (known_team(episode.get("home")),
                         known_team(episode.get("away")))
        if fixture_teams != teams:
            continue
        scheduled = fixture_date(episode)
        if scheduled is None:
            continue
        delta = (date - scheduled).days
        if abs(delta) <= tolerance_days:
            candidates.append((episode, delta))
    if not candidates:
        return {"status": "no_fixture", "teams": list(teams),
                "recording_date": date.isoformat()}
    if len(candidates) > 1:
        return {"status": "ambiguous", "teams": list(teams),
                "recording_date": date.isoformat(),
                "candidates": [e["_id"] for e, _ in candidates]}
    episode, delta = candidates[0]
    return {
        "status": "candidate",
        "episode_id": episode["_id"],
        "series_id": SERIES_ID,
        "season": episode["season"],
        "episode": episode.get("episode"),
        "teams": list(teams),
        "recording_date": date.isoformat(),
        "scheduled_date": fixture_date(episode).isoformat(),
        "delta_days": delta,
    }
