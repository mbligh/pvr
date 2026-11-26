"""Conservative cross-competition rugby matching from a free-text listing.

No source-specific assumptions, network requests, downloads or writes.
The caller supplies the allowed canonical series IDs and fixture evidence.
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

from matchers import championship, prem, pwr, rugbypass

METHOD = "rugby-teams-date-v1"
UK = ZoneInfo("Europe/London")
SEPARATOR = re.compile(r"\s+(?:v|vs|versus)\.?\s+", re.I)
MONTHS = (
    "january february march april may june july august september october "
    "november december"
).split()
MONTH_NUMBER = {name: i for i, name in enumerate(MONTHS, 1)}
MONTH_NUMBER.update({name[:3]: i for i, name in enumerate(MONTHS, 1)})
MONTH_NUMBER["sept"] = 9
DATE = re.compile(
    r"(?<![\w./-])(?:(?P<iso>\d{4}-\d{2}-\d{2})|"
    r"(?P<day>\d{1,2})(?P<sep>[./-])(?P<month>\d{1,2})(?P=sep)"
    r"(?P<year>\d{4}|\d{2})|"
    r"(?P<nday>\d{1,2})\s+(?P<nmonth>[A-Za-z]+)\s+(?P<nyear>\d{4}))"
    r"(?![\w./-])", re.I)
NON_MATCH = re.compile(
    r"\b(?:highlights?|previews?|trailers?|interviews?|reactions?|"
    r"academy|next[ -]?gen|under[ -]?\d{2}|u[ -]?\d{2}|"
    r"rugby\s+league|rugby\s+sevens|7s)\b", re.I)
WOMEN = re.compile(r"\b(?:women(?:['’]?s)?|ladies)\b|♀", re.I)
MEN = re.compile(r"\bmen(?:['’]?s)?\b|♂", re.I)
# Only recognized presentation labels at the outer edges are removable.
# Never guess team names by substring or strip arbitrary words such as "A".
EDGE_LABEL = re.compile(
    r"(?:full\s+match(?:\s+replay)?|full\s+replay|match\s+replay|"
    r"replay|live|(?:grand\s+)?final|semi[ -]?final|quarter[ -]?final|"
    r"round\s+\d+)", re.I)
EDGE_PUNCT = " \t\r\n|:–—-"
CANCELLED = {"cancelled", "canceled", "superseded", "postponed", "abandoned"}


def _series_set(series_ids):
    if isinstance(series_ids, str):
        raise ValueError("series_ids must be a collection of canonical rugby series IDs")
    series = frozenset(series_ids)
    if not series or any(not isinstance(s, str) or not s.startswith("Rugby_")
                         for s in series):
        raise ValueError("At least one canonical Rugby_ series ID is required")
    return series


def _dates(text):
    matches = list(DATE.finditer(text))
    dates = set()
    for item in matches:
        try:
            if item["iso"]:
                date = dt.date.fromisoformat(item["iso"])
            elif item["day"]:
                year = int(item["year"])
                if len(item["year"]) == 2:
                    year += 2000
                date = dt.date(year, int(item["month"]), int(item["day"]))
            else:
                date = dt.date(int(item["nyear"]),
                               MONTH_NUMBER[item["nmonth"].lower()],
                               int(item["nday"]))
        except (ValueError, KeyError):
            return None, matches, "invalid_date"
        dates.add(date)
    if not dates:
        return None, matches, "undated"
    if len(dates) != 1:
        return None, matches, "ambiguous_date"
    return next(iter(dates)), matches, None


def _trim_labels(text):
    text = text.strip(EDGE_PUNCT)
    while text:
        left = re.match(EDGE_LABEL.pattern + r"(?=$|[\s|:–—-])", text, re.I)
        if left:
            text = text[left.end():].strip(EDGE_PUNCT)
            continue
        right = re.search(r"(?:^|[\s|:–—-])(?:" + EDGE_LABEL.pattern + r")$", text, re.I)
        if right:
            text = text[:right.start()].strip(EDGE_PUNCT)
            continue
        break
    return text


def _parse(text):
    if not isinstance(text, str) or not text.strip():
        return {"status": "invalid_title"}
    if NON_MATCH.search(text):
        return {"status": "not_full_match"}
    date, matches, error = _dates(text)
    if error:
        return {"status": error}
    # Replace the dates with a boundary, not with an empty string: this
    # prevents adjoining text from accidentally becoming a team name.
    matchup = DATE.sub(" | ", text)
    matchup = _trim_labels(matchup)
    sides = SEPARATOR.split(matchup)
    if len(sides) != 2:
        return {"status": "invalid_teams", "recording_date": date.isoformat()}
    sides = tuple(_trim_labels(s) for s in sides)
    if any(not s or "|" in s for s in sides):
        return {"status": "invalid_teams", "recording_date": date.isoformat()}
    return {"status": "parsed", "teams": list(sides),
            "recording_date": date.isoformat()}


def _team(value, series):
    if not isinstance(value, str):
        return ""
    # Calendar decoration is not part of the team identity. Only remove
    # known prefixes. Keep A/XV, age grades and other squad distinctions.
    value = re.sub(r"^\s*\[RWC\]\s*", "", value, flags=re.I)
    if series == pwr.SERIES_ID:
        return pwr.known_team(value) or pwr.normalize(value)
    if series == prem.SERIES_ID:
        return prem.TEAMS.get(prem.normalize(value), prem.normalize(value))
    if series == championship.SERIES_ID:
        return championship.team(value) or prem.normalize(value)
    # The same established club aliases can be useful to future club
    # competitions, while national aliases remain independent.
    key = rugbypass.normalize_team(value)
    return championship.TEAMS.get(key, prem.TEAMS.get(key, key))


def _fixture_date(episode):
    value = episode.get("scheduled")
    if isinstance(value, str):
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return dt.date.fromisoformat(value)
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(UK).date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(episode.get("scheduled_date"))
    except (ValueError, TypeError):
        return None


def match_teams_date(text, series_ids, episodes, *, tolerance_days=1):
    """Evaluate one listing against supplied episode documents.

    Return a JSON-friendly result with status, evidence and (only for a
    unique numbered fixture) episode_id, series_id, season and episode.
    All permitted series are checked together; even an exact-date candidate
    is not selected over another candidate inside the tolerance window.
    """
    allowed = _series_set(series_ids)
    if type(tolerance_days) is not int or not 0 <= tolerance_days <= 7:
        raise ValueError("tolerance_days must be an integer from 0 to 7")
    evidence = _parse(text)
    if evidence["status"] != "parsed":
        return evidence
    sides = evidence["teams"]
    date = dt.date.fromisoformat(evidence["recording_date"])
    evidence.update(method=METHOD, date_basis="title")
    candidates = []
    for episode in episodes:
        series = episode.get("series_id")
        if series not in allowed or str(episode.get("status", "")).lower() in CANCELLED:
            continue
        # An explicit gender marker must never be stripped to match the
        # opposite gender. Bare titles rely on the caller's series allowlist.
        if (WOMEN.search(text) and series.startswith("Rugby_Mens_")
                or MEN.search(text) and series.startswith("Rugby_Womens_")):
            continue
        scheduled = _fixture_date(episode)
        if scheduled is None or abs((date - scheduled).days) > tolerance_days:
            continue
        teams = tuple(_team(s, series) for s in sides)
        if not all(teams) or teams[0] == teams[1]:
            continue
        if teams != (_team(episode.get("home"), series),
                     _team(episode.get("away"), series)):
            continue
        candidates.append((episode, scheduled, teams))
    if not candidates:
        return dict(evidence, status="no_fixture")
    if len(candidates) != 1:
        return dict(evidence, status="ambiguous",
                    candidates=[e["_id"] for e, _, _ in candidates])
    episode, scheduled, teams = candidates[0]
    if (type(episode.get("season")) is not int
            or type(episode.get("episode")) is not int):
        return dict(evidence, status="unnumbered_fixture",
                    fixture_id=episode["_id"], series_id=episode["series_id"])
    return dict(evidence, status="candidate", episode_id=episode["_id"],
                series_id=episode["series_id"], season=episode["season"],
                episode=episode["episode"], normalized_teams=list(teams),
                scheduled_date=scheduled.isoformat(),
                delta_days=(date - scheduled).days)


def load_episodes(database, series_ids):
    """Load the read-only fixture pool once for a batch of listing strings."""
    allowed = _series_set(series_ids)
    return list(database.episodes.find(
        {"series_id": {"$in": sorted(allowed)}},
        {"_id": 1, "series_id": 1, "home": 1, "away": 1, "scheduled": 1,
         "scheduled_date": 1, "status": 1, "season": 1, "episode": 1}))


def match_listing(database, text, series_ids, *, tolerance_days=1):
    """Convenience wrapper for a one-off lookup in Atlas; never writes."""
    allowed = _series_set(series_ids)
    return match_teams_date(text, allowed, load_episodes(database, allowed),
                            tolerance_days=tolerance_days)
