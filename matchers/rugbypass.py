"""Conservative RugbyPass replay matching for numbered international fixtures.

A release is linked only when its *competition label* identifies one series,
the home/away national sides match exactly after documented aliases, and its
replay publication time is near exactly one scheduled fixture. Nations
Championship takes precedence over the duplicated men's-international
calendar entries. No match is guessed from a team's name alone.
"""
import datetime as dt
import re
import unicodedata

MENS = "rugby-mens-intl"
WOMENS = "rugby-womens-intl"
NATIONS = "rugby-nations"
SERIES = (MENS, WOMENS, NATIONS)
METHOD = "rugbypass-national-teams-date-v1"

# Explicit aliases, not substring matching, so Australia A and England A
# cannot be confused with the national test sides.
ALIASES = {
    "all blacks": "new zealand",
    "wallabies": "australia",
    "springboks": "south africa",
    "usa": "united states",
    "us": "united states",
    "united states of america": "united states",
    "eagles": "united states",
    "hong kong": "hong kong china",
}
SEPARATOR = re.compile(r"\s+(?:v|vs|versus)\.?\s+", re.IGNORECASE)
FULL_REPLAY = re.compile(r"^full match replay$", re.IGNORECASE)
MENS_LABEL = re.compile(
    r"\bmen(?:'|’)s\s+international\b|\bnations cup\b|"
    r"\bpacific nations cup\b|\brugby(?:'|’)s greatest rivalry\b|"
    r"\bmen(?:'|’)s test\b", re.IGNORECASE)
WOMENS_LABEL = re.compile(
    r"\bwomen(?:'|’)s\s+international\b|\bwxv\b|"
    r"\bwomen(?:'|’)s rugby\b", re.IGNORECASE)
NATIONS_LABEL = re.compile(r"\bnations championship\b", re.IGNORECASE)


def normalize_team(value):
    """Fold flags, gender prefixes, 'NC:' and typographic punctuation."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"^\s*(?:nc\s*[:\-]\s*)+", "", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    value = re.sub(r"^(?:nc\s+|womens?\s+)", "", value)
    value = re.sub(r"\s+(?:women|womens)$", "", value).strip()
    return ALIASES.get(value, value)


def identify_replay(title):
    """Return (series, (home, away)) only for explicit full-match replays."""
    if not isinstance(title, str):
        return None
    parts = [item.strip() for item in title.split("|")]
    if len(parts) < 3 or not FULL_REPLAY.fullmatch(parts[-1]):
        return None
    matchup, *categories = parts[:-1]
    fields = SEPARATOR.split(matchup)
    if len(fields) != 2:
        return None
    home, away = (normalize_team(field) for field in fields)
    if not home or not away or home == away:
        return None
    label = " | ".join(categories)
    if NATIONS_LABEL.search(label):
        series = NATIONS
    elif WOMENS_LABEL.search(label):
        series = WOMENS
    elif MENS_LABEL.search(label):
        series = MENS
    else:
        # No competition/gender evidence, never infer men from silence.
        return None
    return series, (home, away)


def published_at(release):
    value = release.get("published")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        # A date-only publication is insufficient to disambiguate nearby
        # repeat fixtures or replay vs live timing.
        return None
    return when.astimezone(dt.timezone.utc)


def scheduled_at(episode):
    value = episode.get("scheduled")
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)
        except ValueError:
            pass
    return None


def match_release(release, episodes, *, max_delay_days=3, early_hours=4):
    """Return a single unambiguous fixture candidate, or a refusal reason."""
    if not 0 <= max_delay_days <= 7 or not 0 <= early_hours <= 12:
        raise ValueError("Invalid replay matching window")
    if release.get("source") != "rugbypass":
        return {"status": "not_rugbypass"}
    if release.get("available") is False:
        return {"status": "unavailable"}
    meta = release.get("metadata") or {}
    if meta.get("accessLevel") not in (None, "GRANTED") or meta.get("onlinePlayback") not in (None, "AVAILABLE"):
        return {"status": "unavailable"}
    duration = meta.get("duration", release.get("duration"))
    if isinstance(duration, (int, float)) and duration < 3600:
        return {"status": "short_recording"}
    identity = identify_replay(release.get("title"))
    if identity is None:
        return {"status": "not_supported_replay"}
    series, teams = identity
    published = published_at(release)
    if published is None:
        return {"status": "undated", "series_id": series, "teams": list(teams)}
    candidates = []
    for ep in episodes:
        if ep.get("series_id") != series:
            continue
        if (normalize_team(ep.get("home")), normalize_team(ep.get("away"))) != teams:
            continue
        kickoff = scheduled_at(ep)
        if kickoff is None:
            continue
        delta = published - kickoff
        if -dt.timedelta(hours=early_hours) <= delta <= dt.timedelta(days=max_delay_days):
            candidates.append((ep, kickoff, delta))
    evidence = {"series_id": series, "teams": list(teams),
                "published_at": published.isoformat(),
                "competition": series}
    if not candidates:
        return dict(evidence, status="no_fixture")
    if len(candidates) > 1:
        return dict(evidence, status="ambiguous",
                    candidates=sorted(ep["_id"] for ep, _, _ in candidates))
    ep, kickoff, delta = candidates[0]
    if (type(ep.get("season")) is not int or
            type(ep.get("episode")) is not int or
            not isinstance(ep.get("nfo_path"), str) or
            not isinstance(ep.get("nfo"), str)):
        return dict(evidence, status="unnumbered_fixture", fixture_id=ep["_id"])
    return dict(evidence, status="candidate", episode_id=ep["_id"],
                season=ep["season"], episode=ep["episode"],
                scheduled_at=kickoff.isoformat(),
                delay_hours=round(delta.total_seconds() / 3600, 3))
