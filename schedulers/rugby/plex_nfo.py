"""Assign rugby season/episode numbers and build spoiler-free NFO metadata.

Input consists of accepted calendar snapshots. The calendar filter controls
source updates and protects pre-kickoff fixture details from later spoilers.
"""
import datetime as dt
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from zoneinfo import ZoneInfo

from icalendar import Calendar
from .filter import start_time
from .six_nations import fixture_rounds
from .nations import verified_rounds as nations_verified_rounds

UK = ZoneInfo("Europe/London")
ROUND = re.compile(r"\bRound\s+(\d+)\b", re.I)
VERSUS = re.compile(r"\s+(?:vs?\.?|versus)\s+", re.I)


def slug(value):
    value = re.sub(r"[^A-Za-z0-9]+", "_", str(value)).strip("_")
    return value or "Unknown"


def season_year(kickoff):
    local = kickoff.astimezone(UK)
    return local.year if local.month >= 7 else local.year - 1


def xml_bytes(tag, fields):
    root = ET.Element(tag)
    for key, value in fields:
        ET.SubElement(root, key).text = str(value)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"




def build(config, source_dir, now, *, prem_metadata=None):
    prem_metadata = prem_metadata or {}
    output = {}
    manifest = {"schema_version": 2, "generated_at": now.isoformat(), "fixtures": []}
    previous = source_dir / "rugby-metadata-manifest.json"
    old_assignments = {}
    if previous.exists():
        old_data = json.loads(previous.read_text(encoding="utf-8"))
        if old_data.get("schema_version") == 2:
            old_assignments = {f["id"]: f for f in old_data.get("fixtures", [])}
    for entry in config["calendars"] + config.get("plex_sources", []):
        showtitle = entry.get("nfo_showtitle", entry["name"])
        path = source_dir / (entry["slug"] + ".ics")
        if not path.exists():
            raise ValueError(f"Missing source snapshot: {path}")
        calendar = Calendar.from_ical(path.read_bytes())
        grouped = defaultdict(list)
        six_nations_rounds = (fixture_rounds(calendar)
                              if entry.get("rounds") == "six-nations" else {})
        nations_rounds = (nations_verified_rounds(calendar)
                          if entry["slug"] == "Rugby_Mens_Nations" else {})
        wxv_week_start = {}
        if entry["slug"] == "Rugby_Womens_WXV":
            # Each WXV tournament year has independent week-based rounds.
            # Monday anchors keep weekend fixtures together; prior
            # assignments in the manifest override the recomputed round.
            for event in calendar.walk("VEVENT"):
                start = start_time(event, UK)
                if start is None or not isinstance(event.decoded("DTSTART"), dt.datetime):
                    continue
                local = start.astimezone(UK)
                monday = local.date() - dt.timedelta(days=local.weekday())
                year = local.year
                wxv_week_start[year] = min(wxv_week_start.get(year, monday), monday)
        for event in calendar.walk("VEVENT"):
            kickoff = start_time(event, UK)
            if kickoff is None:
                continue
            # Never publish scores, and never treat an all-day/TBC event as a timed fixture.
            if not isinstance(event.decoded("DTSTART"), dt.datetime):
                continue
            title = str(event.get("SUMMARY", ""))
            teams = VERSUS.split(title, maxsplit=1)
            if len(teams) != 2:
                continue
            description = str(event.get("DESCRIPTION", ""))
            round_match = ROUND.search(description)
            if entry.get("rounds") == "prem":
                year = season_year(kickoff)
                uid = str(event.get("UID", "")).strip()
                previous = old_assignments.get(f"{entry['slug']}:{uid}")
                round_no = prem_metadata.get(uid, {}).get("round")
                if round_no is None:
                    round_no = (previous.get("round") if previous and
                                isinstance(previous.get("episode"), int) else
                                int(round_match.group(1)) if round_match else None)
                if round_no is None:
                    manifest["fixtures"].append({
                        "id": f"{entry['slug']}:{uid}",
                        "competition": entry["slug"],
                        "showtitle": showtitle, "season": year,
                        "round": None, "episode": None,
                        "kickoff_utc": kickoff.astimezone(dt.timezone.utc).isoformat(),
                        "home": teams[0].strip(), "away": teams[1].strip(),
                        "venue": str(event.get("LOCATION", "")).strip(),
                        "nfo": None,
                    })
                    continue
            elif entry.get("rounds") == "six-nations":
                # Both championships use calendar-year seasons, unlike PWR
                # and Gallagher PREM, which use the July-to-June convention.
                year = kickoff.astimezone(UK).year
                fixture_id = f"{entry['slug']}:{event.get('UID', '')}"
                prior = old_assignments.get(fixture_id)
                if (prior and prior.get("season") == year and
                        isinstance(prior.get("episode"), int) and
                        isinstance(prior.get("round"), int)):
                    # Do not renumber previously accepted fixtures if the
                    # current feed later becomes incomplete or moves dates.
                    round_no = prior["round"]
                else:
                    round_no = six_nations_rounds.get(str(event.get("UID", "")).strip())
                if round_no is None:
                    # An incomplete schedule may not be safely ranked;
                    # retain fixture metadata, but no fabricated episode.
                    manifest["fixtures"].append({
                        "id": fixture_id, "competition": entry["slug"],
                        "showtitle": showtitle, "season": year,
                        "round": None, "episode": None,
                        "kickoff_utc": kickoff.astimezone(dt.timezone.utc).isoformat(),
                        "home": teams[0].strip(), "away": teams[1].strip(),
                        "venue": str(event.get("LOCATION", "")).strip(),
                        "nfo": None,
                    })
                    continue
            elif entry.get("type") == "world-rugby":
                year = kickoff.astimezone(UK).year
                if entry["slug"] == "Rugby_Womens_WXV":
                    local = kickoff.astimezone(UK)
                    monday = local.date() - dt.timedelta(days=local.weekday())
                    anchor = wxv_week_start[year]
                    round_no = 1 + (monday - anchor).days // 7
                    assigned = old_assignments.get(f"{entry['slug']}:{event.get('UID', '')}")
                    if assigned and assigned.get("round") is not None and assigned.get("episode") is not None:
                        round_no = assigned["round"]
                    if not 1 <= round_no <= 99:
                        raise ValueError("WXV round out of supported range")
                elif entry["slug"] == "Rugby_Mens_Nations":
                    assigned = old_assignments.get(f"{entry['slug']}:{event.get('UID', '')}")
                    round_no = (assigned["round"] if assigned and
                                assigned.get("episode") is not None and
                                assigned.get("round") is not None else
                                nations_rounds.get(str(event.get("UID", "")).strip()))
                    if round_no is None:
                        # Publish the fixture without fabricating a round/episode.
                        manifest["fixtures"].append({
                            "id": f"{entry['slug']}:{event.get('UID', '')}",
                            "competition": entry["slug"], "showtitle": showtitle, "season": year,
                            "round": None, "episode": None,
                            "kickoff_utc": kickoff.astimezone(dt.timezone.utc).isoformat(),
                            "home": teams[0].strip(), "away": teams[1].strip(),
                            "venue": str(event.get("LOCATION", "")).strip(),
                            "nfo": None,
                        })
                        continue
                else:
                    round_no = 1
            else:
                if not round_match:
                    continue
                round_no = int(round_match.group(1))
                if not 1 <= round_no <= 99:
                    continue
                year = season_year(kickoff)
            grouped[(year, round_no)].append((kickoff.astimezone(dt.timezone.utc), str(event.get("UID", "")), teams, event))
        for (year, round_no), matches in sorted(grouped.items()):
            # Series identifier and directory are exactly the same string.
            show = entry["slug"]
            # Keep show identifiers stable and filesystem-safe.
            show_dir = f"{show}/"
            output[show_dir + "tvshow.nfo"] = xml_bytes("tvshow", [
                ("title", (f"{entry['name']} {year}"
                           if entry.get("type") == "world-rugby" or
                           entry.get("season_mode") == "calendar"
                           else f"{entry['name']} {year}-{str((year + 1) % 100).zfill(2)}")),
                ("plot", "Spoiler-free rugby fixture metadata"),
            ])
            # Deterministic order for simultaneous kickoffs.
            matches.sort(key=lambda x: (x[0], slug(x[2][0]), slug(x[2][1]), x[1]))
            if len(matches) > 99:
                raise ValueError(f"More than 99 matches in {show} round {round_no}")
            used = {f["episode"] % 100 for f in old_assignments.values()
                    if f.get("competition") == entry["slug"] and f.get("season") == year
                    and f.get("round") == round_no and isinstance(f.get("episode"), int)}
            next_number = 1
            for kickoff, uid, teams, event in matches:
                home, away = (s.strip() for s in teams)
                fixture_id = f"{entry['slug']}:{uid}"
                prior = old_assignments.get(fixture_id)
                if prior and prior.get("round") == round_no and prior.get("episode") is not None:
                    number = prior["episode"] % 100
                else:
                    while next_number in used:
                        next_number += 1
                    number = next_number
                    used.add(number)
                episode = round_no * 100 + number
                prefix = f"{show}-s{year}e{episode:03d}"
                basename = f"{prefix}-{slug(home)}_v_{slug(away)}_{kickoff.astimezone(UK):%Y%m%d}"
                relative = f"{show_dir}Season_{year}/{basename}.nfo"
                venue = str(event.get("LOCATION", "")).strip()
                fields = [
                    ("title", f"{home} vs {away} - {kickoff.astimezone(UK):%Y/%m/%d}"),
                    ("showtitle", showtitle),
                    ("season", year),
                    ("episode", episode),
                    ("aired", kickoff.astimezone(UK).date().isoformat()),
                    ("plot", (str(event.get("DESCRIPTION", entry["name"])) if entry.get("type") == "world-rugby" else f"{entry['name']}, Round {round_no}") + (f". Venue: {venue}" if venue else "")),
                ]
                output[relative] = xml_bytes("episodedetails", fields)
                manifest["fixtures"].append({
                    "id": f"{entry['slug']}:{uid}",
                    "competition": entry["slug"],
                    "showtitle": showtitle,
                    "season": year,
                    "round": round_no,
                    "episode": episode,
                    "kickoff_utc": kickoff.isoformat(),
                    "home": home, "away": away,
                    "venue": venue,
                    "nfo": relative,
                })
    if not manifest["fixtures"]:
        raise ValueError("No round-numbered fixtures found; refusing to publish empty metadata")
    manifest["fixtures"].sort(key=lambda x: (x["competition"], x["season"],
                                               x["kickoff_utc"], x["id"]))
    manifest["files"] = {p: hashlib.sha256(data).hexdigest() for p, data in sorted(output.items())}
    return output, manifest
