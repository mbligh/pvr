"""Spoiler-free official EPCR Investec Champions Cup fixture scheduler.

EPCR's public website uses RugbyViz via the InCrowd match API. Only
pre-match fixture identifiers, teams, kickoff, round and venue are retained;
never persist provider scores, results, form or match events.
"""
import datetime as dt
import json
import os
import urllib.error
import urllib.request

from icalendar import Calendar, Event

COMP_ID = 1008
BASE_URL = "https://rugby-union-feeds.incrowdsports.com/v1/matches"
PUBLIC_HEADERS = {
    "X-APP-ID": "web",
    "X-API-KEY": "2At3OgFAzAWfB0Pv8hn9mU4x",
    "X-REALM": "epcr",
}


def convert_epcr(payload, *, season, comp_id=COMP_ID):
    """Project the public match feed onto score-free iCalendar events."""
    matches = payload.get("data")
    if not isinstance(matches, list) or not matches:
        raise ValueError("Empty EPCR Champions Cup fixture feed")
    metadata = payload.get("metadata") or {}
    if (metadata.get("totalPages", 1) != 1 or
            metadata.get("totalItems", len(matches)) != len(matches)):
        raise ValueError("Incomplete EPCR match feed; pagination is required")
    calendar = Calendar()
    calendar.add("prodid", "-//mbligh/pvr//Investec Champions Cup fixtures//EN")
    calendar.add("version", "2.0")
    seen = set()
    for match in matches:
        if match.get("compId") != comp_id or match.get("season") != season:
            raise ValueError("Unexpected EPCR competition or season")
        match_id = str(match["id"])
        if not match_id.isdecimal() or match_id in seen:
            raise ValueError(f"Invalid or duplicate EPCR match ID: {match_id}")
        seen.add(match_id)
        round_no = int(match["round"])
        if not 1 <= round_no <= 99:
            raise ValueError(f"Invalid EPCR round: {round_no}")
        kickoff = dt.datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
        if kickoff.tzinfo is None:
            raise ValueError(f"Naive EPCR kickoff: {match_id}")
        home = (match.get("homeTeam") or {}).get("name")
        away = (match.get("awayTeam") or {}).get("name")
        # Knockout bracket placeholders must not become fictional episodes.
        if not home or not away or any(
                team.strip().upper() in ("TBC", "TBD", "TO BE CONFIRMED")
                for team in (home, away)):
            continue
        event = Event()
        event.add("uid", f"epcr-{match_id}@mbligh.github.io")
        event.add("summary", f"{home} vs {away}")
        if match.get("tbc"):
            # An unspecified kickoff is not a precise time.
            event.add("dtstart", kickoff.date())
            event.add("dtend", kickoff.date() + dt.timedelta(days=1))
        else:
            event.add("dtstart", kickoff)
            event.add("dtend", kickoff + dt.timedelta(hours=2))
        event.add("description", f"Investec Champions Cup — Round {round_no}")
        event.add("x-epcr-id", match_id)
        event.add("x-epcr-round", round_no)
        venue = (match.get("venue") or {}).get("name")
        if venue:
            event.add("location", venue)
        calendar.add_component(event)
    if not calendar.walk("VEVENT"):
        raise ValueError("No confirmed EPCR Champions Cup fixtures")
    return calendar


def fetch_epcr(url, *, season, comp_id=COMP_ID):
    """Fetch exactly one season; reject truncated/error responses without retry loops."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "mbligh-pvr/1.0",
        "Accept": "application/json",
    })
    for header, variable in (
        ("X-APP-ID", "EPCR_APP_ID"),
        ("X-API-KEY", "EPCR_API_KEY"),
        ("X-REALM", "EPCR_REALM"),
    ):
        request.add_header(header, os.environ.get(variable) or PUBLIC_HEADERS[header])
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return convert_epcr(json.load(response), season=season, comp_id=comp_id)
    except urllib.error.HTTPError as exc:
        # In particular, a 429 stops the import; don't hammer EPCR on throttling.
        detail = " ".join(exc.read(1024).decode("utf-8", "replace").split())[:500]
        raise RuntimeError(
            f"EPCR Champions Cup API returned HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from exc
