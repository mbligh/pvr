#!/usr/bin/env python3
"""Backfill 2026 July Nations Championship fixtures without rewriting ICS history.

Read only the allowlisted match ID, teams, dates and venues from World Rugby.
Even though the API contains completed match results, no score/status/outcome
data enters the generated episode or NFO. Preview by default; --apply adds
missing canonical episodes to Atlas without changing previously stored ones.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
from collections import Counter

from icalendar import Calendar

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEGACY = pathlib.Path(__file__).resolve().parent / "legacy"
sys.path.insert(0, str(LEGACY))
import plex_nfo  # noqa: E402
from world_rugby import (  # noqa: E402
    request_page, convert_match, competition_base, COMPETITIONS, is_cancelled,
)
from schedulers.rugby.nations import verified_rounds  # noqa: E402

SERIES = "Rugby_Mens_Nations"
UTC = dt.timezone.utc


def fetch_historical_calendar(year):
    """Only the verified July 2026 round windows are currently supported."""
    if year != 2026:
        raise ValueError("Nations history is only validated for July 2026")
    start, end = dt.date(year, 7, 1), dt.date(year, 7, 31)
    found = {}
    page = 0
    while True:
        payload = request_page(start, end, page)
        for match in payload["content"]:
            if (match.get("sport") != "MRU" or
                    competition_base(match.get("competition")) not in COMPETITIONS["nations"][1]):
                continue
            if is_cancelled(match):
                raise ValueError("Cancelled historical Nations fixture; review manually")
            match_id = str(match["matchId"])
            if match_id in found:
                raise ValueError(f"Duplicate official historical fixture: {match_id}")
            found[match_id] = match
        page += 1
        if page >= payload["pageInfo"]["numPages"]:
            break
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", "-//mbligh/pvr//Historical Nations fixtures//EN")
    for match in sorted(found.values(), key=lambda m: (m["time"]["millis"], str(m["matchId"]))):
        event = convert_match(match)
        if event is not None:
            calendar.add_component(event)
    return calendar


def prepare(year, calendar, prior=None, now=None):
    """Produce exactly 18 unique, verified episodes with NFOs, or abort."""
    if year != 2026:
        raise ValueError("Only the verified 2026 July source can be backfilled")
    events = calendar.walk("VEVENT")
    mapping = verified_rounds(calendar)
    if len(events) != 18 or len(mapping) != 18 or Counter(mapping.values()) != {1: 6, 2: 6, 3: 6}:
        raise ValueError("Historical Nations July rounds are incomplete or ambiguous")
    prior = prior or []
    config = {"calendars": [{"slug": SERIES, "name": "Rugby Nations",
                             "nfo_showtitle": "Rugby Nations Championships",
                             "type": "world-rugby", "group": "nations"}]}
    with tempfile.TemporaryDirectory(prefix="pvr-nations-history-") as tmp:
        docs = pathlib.Path(tmp) / "docs"
        docs.mkdir()
        (docs / (SERIES + ".ics")).write_bytes(calendar.to_ical())
        (docs / "rugby-metadata-manifest.json").write_text(
            json.dumps({"schema_version": 2, "fixtures": prior}))
        files, manifest = plex_nfo.build(config, docs, now or dt.datetime.now(UTC))
    if len(manifest["fixtures"]) != 18:
        raise ValueError("Nations history lost fixtures in NFO conversion")
    result = []
    for fixture in manifest["fixtures"]:
        if fixture["season"] != year or fixture["episode"] is None or fixture["nfo"] not in files:
            raise ValueError(f"Invalid historical Nations fixture {fixture['id']}")
        result.append({
            "_id": fixture["id"], "series_id": SERIES, "season": year,
            "episode": fixture["episode"], "round": fixture["round"],
            "title": fixture["home"] + " vs " + fixture["away"],
            "scheduled": dt.datetime.fromisoformat(fixture["kickoff_utc"]).astimezone(UTC),
            "home": fixture["home"], "away": fixture["away"],
            "venue": fixture["venue"], "showtitle": fixture["showtitle"],
            "nfo_path": fixture["nfo"],
            "nfo": files[fixture["nfo"]].decode("utf-8"),
            "external_ids": {"rugbypass": fixture.get("rugbypass_id")},
            "metadata_provider": "world-rugby/history",
            "status": "scheduled",
        })
    if len({x["_id"] for x in result}) != 18 or len({x["episode"] for x in result}) != 18:
        raise ValueError("Duplicate Nations historical IDs or episode numbers")
    return result


def prior_assignments(db, year):
    if db is None:
        return []
    return [{"id": doc["_id"], "competition": SERIES, "season": year,
             "round": doc["round"], "episode": doc["episode"]}
            for doc in db.episodes.find({
                "series_id": SERIES, "season": year,
                "round": {"$in": [1, 2, 3]}, "episode": {"$type": "int"}
            }, {"_id": 1, "round": 1, "episode": 1})]


def persist(db, records, now=None):
    from pymongo import UpdateOne
    from schedulers.series_ids import assert_migrated

    assert_migrated(db)
    if len(records) != 18:
        raise ValueError("Refusing incomplete historical Nations import")
    ids = [r["_id"] for r in records]
    targets = {r["_id"]: r for r in records}
    existing = list(db.episodes.find({"$or": [
        {"_id": {"$in": ids}},
        {"series_id": SERIES, "season": 2026,
         "episode": {"$in": [r["episode"] for r in records]}},
    ]}, {"_id": 1, "series_id": 1, "season": 1, "episode": 1}))
    for doc in existing:
        expected = targets.get(doc["_id"])
        if expected is None or (doc.get("series_id"), doc.get("season"), doc.get("episode")) != (
                SERIES, 2026, expected["episode"]):
            raise ValueError(f"Conflicting Nations historical identity: {doc['_id']}")
    now = now or dt.datetime.now(UTC)
    operations = [
        UpdateOne({"_id": rec["_id"]}, {"$setOnInsert": rec | {
            "created_at": now, "metadata_updated_at": now}}, upsert=True)
        for rec in records
    ]
    reply = db.episodes.bulk_write(operations, ordered=False)
    return {"upserted": reply.upserted_count, "matched": reply.matched_count}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--apply", action="store_true",
                        help="Insert missing fixtures into Atlas (preview by default)")
    parser.add_argument("--database", default=os.environ.get("MONGODB_DATABASE", "pvr"))
    args = parser.parse_args(argv)
    client = None
    try:
        if args.apply:
            uri = os.environ.get("MONGODB_URI") or (
                pathlib.Path.home() / ".mongodb" / "pvr").read_text(encoding="utf-8").strip()
            from pymongo import MongoClient
            client = MongoClient(uri, serverSelectionTimeoutMS=15000)
            client.admin.command("ping")
        db = client[args.database] if client is not None else None
        calendar = fetch_historical_calendar(args.year)
        docs = prepare(args.year, calendar, prior_assignments(db, args.year))
        print(json.dumps({"year": args.year, "preview": not args.apply,
                          "episodes": len(docs),
                          "rounds": dict(sorted(Counter(x["round"] for x in docs).items()))}))
        if db is not None:
            print(json.dumps(persist(db, docs)))
    except (OSError, ValueError, KeyError) as exc:
        print(f"Nations history: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
