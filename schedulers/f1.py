#!/usr/bin/env python3
"""Convert official F1 data to spoiler-free PVR episodes; preview unless --apply.

Examples:
    ./import_f1 --input f1-2026.json
    ./import_f1 --input f1-2026.json --jsonl > f1-episodes.jsonl
    ./import_f1 --input f1-2026.json --apply
    ./import_f1 --year 2026 --apply
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import f1_data

UTC = dt.timezone.utc
SERIES_ID = "Formula_1"
SHOWTITLE = "Formula 1"
LIBRARY_DIR = "Formula_1"
# Offsets are fixed, independent of chronological order or sprint-weekend format.
SESSION_OFFSETS = {"p1": 1, "p2": 2, "p3": 3, "ss": 4, "s": 5, "q": 6, "r": 7}
SESSION_NAMES = {"p1": "Practice 1", "p2": "Practice 2", "p3": "Practice 3",
                 "ss": "Sprint Qualifying", "s": "Sprint",
                 "q": "Qualifying", "r": "Race"}
ROUND_PATTERN = re.compile(r"^ROUND\s+(\d+)$", re.IGNORECASE)
# F1 may return legacy tzdb links absent from minimal tzdata packages.
TIMEZONE_ALIASES = {"US/Central": "America/Chicago"}


def slug(text):
    value = unicodedata.normalize("NFKD", text)
    value = value.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")


def xml_nfo(title, season, episode, aired, plot):
    root = ET.Element("episodedetails")
    for tag, value in [("title", title), ("showtitle", SHOWTITLE),
                       ("season", season), ("episode", episode),
                       ("aired", aired), ("plot", plot)]:
        ET.SubElement(root, tag).text = str(value)
    ET.indent(root, space="  ")
    return (ET.tostring(root, encoding="utf-8", xml_declaration=True)
            + b"\n").decode("utf-8")


def session_times(session, meeting):
    """Parse local start/end as UTC and reject inconsistent supplied GMT offsets."""
    tzname = session.get("timezone") or meeting.get("meetingTimezone")
    if not tzname:
        raise ValueError("Missing F1 session timezone")
    tzname = TIMEZONE_ALIASES.get(tzname, tzname)
    try:
        zone = ZoneInfo(tzname)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Timezone {tzname!r} unavailable; install the system tzdata package") from exc
    local_times = []
    for field in ("startTime", "endTime"):
        raw = session.get(field)
        if not isinstance(raw, str):
            raise ValueError(f"Missing {field}")
        naive = dt.datetime.fromisoformat(raw)
        if naive.tzinfo is not None:
            raise ValueError(f"Expected timezone-local {field}, got {raw}")
        local = naive.replace(tzinfo=zone)
        offset = session.get("gmtOffset")
        if offset and local.strftime("%z") != offset.replace(":", ""):
            raise ValueError(f"{field}: GMT offset {offset} disagrees with {tzname}")
        local_times.append(local)
    start, end = local_times
    if end <= start:
        raise ValueError("Session ends before it starts")
    return start.astimezone(UTC), end.astimezone(UTC), start.date().isoformat()


def build_episodes(payload):
    """Validate the complete season before returning records suitable for upsert."""
    if not isinstance(payload, dict) or not isinstance(payload.get("meetings"), list):
        raise ValueError("Expected f1_data JSON with meetings array")
    try:
        year = int(payload["season"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Missing or invalid season") from exc
    if not 1950 <= year <= 2100 or not payload["meetings"]:
        raise ValueError("Invalid season or empty meetings")

    episodes, seen_ids, seen_episodes, meetings = [], set(), set(), set()
    for item in payload["meetings"]:
        if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
            raise ValueError("Missing meeting event data")
        meeting = item["event"]
        if meeting.get("isTestEvent") is True or meeting.get("type") == "fom-testing":
            continue
        match = ROUND_PATTERN.fullmatch(str(meeting.get("roundText", "")))
        if not match:
            raise ValueError(f"Missing championship round: {meeting.get('meetingName')}")
        round_no = int(match.group(1))
        if not 1 <= round_no <= 99:
            raise ValueError(f"Invalid championship round: {round_no}")
        name = meeting.get("meetingName")
        key = str(meeting.get("meetingKey", ""))
        if not isinstance(name, str) or not name.strip() or not key.isdecimal():
            raise ValueError("Invalid meeting name or key")
        if key in meetings:
            raise ValueError(f"Duplicate meeting key {key}")
        meetings.add(key)
        sessions = item.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            raise ValueError(f"Meeting {key} has no sessions")
        detail = item.get("detail")
        if isinstance(detail, dict):
            context = detail.get("meetingContext")
            if not isinstance(context, dict) or str(context.get("meetingKey")) != key:
                raise ValueError(f"Meeting {key} has mismatched detail context")
        for session in sessions:
            if not isinstance(session, dict):
                raise ValueError(f"Meeting {key} has malformed session")
            code = session.get("session")
            if code not in SESSION_OFFSETS:
                raise ValueError(f"Meeting {key}: unknown session code {code!r}")
            session_key = session.get("meetingSessionKey")
            if type(session_key) is not int or session_key <= 0:
                raise ValueError(f"Meeting {key}: invalid meetingSessionKey {session_key!r}")
            stable_id = f"Formula_1:{session_key}"
            number = round_no * 10 + SESSION_OFFSETS[code]
            if stable_id in seen_ids or (year, number) in seen_episodes:
                raise ValueError(f"Duplicate session ID or episode: {stable_id}, {year}E{number}")
            seen_ids.add(stable_id)
            seen_episodes.add((year, number))
            start, end, aired = session_times(session, meeting)
            if start.year not in (year - 1, year, year + 1):
                raise ValueError(f"Meeting {key}: session time outside season")
            session_name = SESSION_NAMES[code]
            title = f"{name} - {session_name}"
            circuit = meeting.get("circuitOfficialName") or meeting.get("circuitShortName") or ""
            location = meeting.get("meetingLocation") or ""
            plot = f"Formula 1 {year}, Round {round_no}: {name}, {session_name}."
            if circuit:
                plot += f" Circuit: {circuit}."
            if location:
                plot += f" Location: {location}."
            base = (f"{LIBRARY_DIR}-s{year}e{number:03d}-"
                    f"{slug(name)}_{slug(session_name)}_{aired.replace('-', '')}")
            episodes.append({
                "_id": stable_id, "series_id": SERIES_ID, "showtitle": SHOWTITLE,
                "season": year, "episode": number, "round": round_no, "title": title,
                "scheduled": start, "scheduled_end": end,
                "duration": int((end - start).total_seconds()),
                "scheduled_date": aired,
                "meeting_name": name, "meeting_key": key,
                "session_code": code, "session_name": session_name,
                "session_key": session_key, "venue": circuit, "location": location,
                "timezone": TIMEZONE_ALIASES.get(
                    session.get("timezone") or meeting.get("meetingTimezone"),
                    session.get("timezone") or meeting.get("meetingTimezone")),
                "nfo_path": f"{LIBRARY_DIR}/Season_{year}/{base}.nfo",
                "nfo": xml_nfo(title, year, number, aired, plot),
                "external_ids": {"formula1_meeting": key,
                                 "formula1_session": str(session_key)},
                "metadata_provider": "formula1/official",
                "source_url": ("https://www.formula1.com" + meeting["url"])
                              if str(meeting.get("url", "")).startswith("/en/") else None,
            })
    if not episodes:
        raise ValueError("No championship sessions found (testing is excluded)")
    episodes.sort(key=lambda e: (e["season"], e["episode"]))
    return episodes


def persist(db, episodes, now):
    """Retain other PVR state; refuse accidental F1 episode renumbering."""
    from schedulers.series_ids import assert_migrated
    assert_migrated(db)
    from pymongo import UpdateOne

    year = episodes[0]["season"]
    existing = list(db.episodes.find(
        {"series_id": SERIES_ID, "season": year}, {"_id": 1, "episode": 1}))
    by_id = {ep["_id"]: ep for ep in episodes}
    by_number = {(ep["season"], ep["episode"]): ep["_id"] for ep in episodes}
    for old in existing:
        other_id = by_number.get((year, old.get("episode")))
        if other_id and other_id != old["_id"]:
            raise ValueError(f"Existing F1 E{old['episode']} belongs to {old['_id']}, "
                             f"not {other_id}; refusing to renumber")
        current = by_id.get(old["_id"])
        if current and old.get("episode") != current["episode"]:
            raise ValueError(f"Existing {old['_id']} numbered E{old.get('episode')}; "
                             f"new source says E{current['episode']}; refusing to renumber")
    ops = [
        UpdateOne({"_id": ep["_id"]},
                  {"$set": {k: v for k, v in ep.items() if k != "_id"} |
                           {"metadata_updated_at": now},
                   "$setOnInsert": {"created_at": now}},
                  upsert=True) for ep in episodes
    ]
    result = db.episodes.bulk_write(ops, ordered=False)
    db.episodes.create_index([("series_id", 1), ("season", 1), ("episode", 1)])
    db.episodes.create_index([("scheduled", 1)])
    return {"matched": result.matched_count, "modified": result.modified_count,
            "upserted": result.upserted_count}


def read_source(path, year):
    if path:
        return json.load(sys.stdin) if str(path) == "-" else json.loads(
            path.read_text(encoding="utf-8"))
    return f1_data.fetch_season(year)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=pathlib.Path, metavar="FILE",
                        help="Saved f1_data JSON; - for stdin (default: fetch live)")
    parser.add_argument("--year", type=int, help="Expected season (default: current year)")
    parser.add_argument("--database", default=os.getenv("MONGODB_DATABASE", "pvr"))
    parser.add_argument("--apply", action="store_true",
                        help="Upsert to MongoDB Atlas; default is preview only")
    parser.add_argument("--jsonl", action="store_true",
                        help="Emit complete episode records as JSON Lines")
    args = parser.parse_args(argv)
    if args.jsonl and args.apply:
        parser.error("--jsonl and --apply are mutually exclusive")
    year = args.year or dt.datetime.now(UTC).year
    try:
        source = read_source(args.input, year)
        if args.year is not None and str(source.get("season")) != str(args.year):
            raise ValueError(f"Expected season {args.year}, received {source.get('season')}")
        episodes = build_episodes(source)
        if args.jsonl:
            for ep in episodes:
                print(json.dumps(ep, ensure_ascii=False,
                                 default=lambda value: value.isoformat()))
            return 0
        report = {"season": episodes[0]["season"], "series_id": SERIES_ID,
                  "meetings": len({ep["meeting_key"] for ep in episodes}),
                  "sessions": len(episodes), "preview": not args.apply,
                  "first_episode": episodes[0]["episode"],
                  "last_episode": episodes[-1]["episode"],
                  "database": args.database if args.apply else None}
        if args.apply:
            uri = os.getenv("MONGODB_URI")
            if not uri:
                uri = (pathlib.Path.home() / ".mongodb" / "pvr").read_text(
                    encoding="utf-8").strip()
            if not uri:
                raise ValueError("Empty MongoDB URI")
            from pymongo import MongoClient
            with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
                client.admin.command("ping")
                report["mongo"] = persist(client[args.database], episodes,
                                          dt.datetime.now(UTC))
        print(json.dumps(report, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        print(f"import_Formula_1: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
