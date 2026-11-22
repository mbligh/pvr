"""Allowlisted, spoiler-free adapter for the official Gallagher PREM match feed."""
import datetime as dt
import json
import os
import urllib.error
import urllib.request

from icalendar import Calendar, Event

# Public website client headers (not account credentials).
# The public X-API-KEY is observable in browser traffic and can rotate.
# Keep optional environment overrides for operational recovery.
PUBLIC_HEADERS = {
    "X-APP-ID": "web",
    "X-API-KEY": "zDiLQ9o18oVrwn30etwT",
    "X-REALM": "prl",
}


def convert_prem(payload, *, comp_id=1011, season=202601):
    matches = payload["data"]
    if not isinstance(matches, list) or not matches:
        raise ValueError("Empty Gallagher PREM match feed")
    calendar = Calendar()
    calendar.add("prodid", "-//mbligh/calendar//Gallagher PREM metadata//EN")
    calendar.add("version", "2.0")
    seen = set()
    for match in matches:
        if match["compId"] != comp_id or match["season"] != season:
            raise ValueError("Unexpected competition or season in Gallagher PREM feed")
        match_id = int(match["id"])
        if match_id in seen:
            raise ValueError(f"Duplicate Gallagher PREM fixture {match_id}")
        seen.add(match_id)
        round_no = int(match["round"])
        if not 1 <= round_no <= 99:
            raise ValueError(f"Invalid Gallagher PREM round {round_no}")
        kickoff = dt.datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
        if kickoff.tzinfo is None:
            raise ValueError(f"Naive kickoff for {match_id}")
        home = match["homeTeam"]["name"]
        away = match["awayTeam"]["name"]
        # Play-off placeholders have no actual teams yet. Do not create
        # bogus "TBC vs TBC" episodes or NFO files.
        if not home or not away or home.strip().upper() == "TBC" or away.strip().upper() == "TBC":
            continue
        event = Event()
        event.add("uid", f"prem-{match_id}@mbligh.github.io")
        event.add("summary", f"{home} vs {away}")
        if match.get("tbc"):
            # Do not represent a provisional kickoff as an exact datetime.
            # Preserve the source's provisional date as an all-day fixture.
            event.add("dtstart", kickoff.date())
            event.add("dtend", kickoff.date() + dt.timedelta(days=1))
        else:
            event.add("dtstart", kickoff)
            event.add("dtend", kickoff + dt.timedelta(hours=2))
        event.add("description", f"Gallagher PREM — Round {round_no}")
        event.add("x-prem-id", str(match_id))
        event.add("x-prem-round", round_no)
        venue = (match.get("venue") or {}).get("name")
        if venue:
            event.add("location", venue)
        # Never read or copy score, form, status, winner, events or images.
        calendar.add_component(event)
    if not calendar.walk("VEVENT"):
        raise ValueError("No confirmed Gallagher PREM fixtures")
    return calendar


def fetch_prem(url, *, comp_id=1011, season=202601):
    request = urllib.request.Request(url, headers={
        "User-Agent": "calendar-spoiler-filter/1.0",
        "Accept": "application/json",
    })
    for header, variable in (
        ("X-APP-ID", "PREM_APP_ID"),
        ("X-API-KEY", "PREM_API_KEY"),
        ("X-REALM", "PREM_REALM"),
    ):
        request.add_header(header, os.environ.get(variable) or PUBLIC_HEADERS[header])
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return convert_prem(json.load(response), comp_id=comp_id, season=season)
    except urllib.error.HTTPError as exc:
        # The upstream API includes validation details in the error response.
        # Include a bounded excerpt so scheduled failures are actionable.
        detail = exc.read(1024).decode("utf-8", errors="replace").strip()
        detail = " ".join(detail.split())[:500]
        raise RuntimeError(
            f"Gallagher PREM API returned HTTP {exc.code}"
            + (f": {detail}" if detail else " (no response body)")
        ) from exc
