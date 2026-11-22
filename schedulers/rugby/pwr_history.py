#!/usr/bin/env python3
"""Import a historical PWR season without changing current rugby calendars.

Uses the same score-free calendar adapter and numbering/NFO generator as the
current PWR importer. Preview by default; --apply writes only episode records.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
from collections import Counter
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[2]

from . import plex_nfo
from .pwr import fetch_pwr

SERIES = "Rugby_Womens_PWR"
SHOW = "Rugby Womens PWR"
UTC = dt.timezone.utc
UK = ZoneInfo("Europe/London")
COMP_ID = 1095


def api_url(year):
    # Provider season 2025/26 is identified as 202501.
    if not 2015 <= year <= 2025:
        raise ValueError("Historical season year must be between 2015 and 2025")
    return (f"https://www.thepwr.com/api/oval/fixtures"
            f"?compId={COMP_ID}&seasonId={year}01")


def previous_fixtures(db, year):
    """Generate old-manifest assignments from Atlas, not the current-season manifest."""
    if db is None:
        return []
    prior = db.episodes.find(
        {"series_id": SERIES, "season": year},
        {"_id": 1, "season": 1, "round": 1, "episode": 1})
    return [
        {"id": doc["_id"], "competition": SERIES, "season": year,
         "round": doc["round"], "episode": doc["episode"]}
        for doc in prior
        if doc.get("round") is not None and isinstance(doc.get("episode"), int)
    ]


def prepare(year, calendar, prior=None, now=None):
    """Build historical episode docs using the established legacy NFO algorithm.

    The input is already a score-free ICS calendar from fetch_pwr(). There is
    deliberately no route for raw scores/results into the Atlas documents.
    """
    from icalendar import Calendar

    if not isinstance(calendar, Calendar):
        raise ValueError("PWR history must be an iCalendar object")
    events = calendar.walk("VEVENT")
    if not events:
        raise ValueError("Historical PWR feed is empty")
    now = now or dt.datetime.now(UTC)
    prior = prior or []
    config = {"calendars": [{
        "slug": SERIES, "name": SHOW, "nfo_showtitle": SHOW, "type": "pwr",
        "library_dir": "Rugby_Womens_PWR",
    }]}
    with tempfile.TemporaryDirectory(prefix="pvr-pwr-history-") as work:
        root = pathlib.Path(work)
        docs = root / "docs"
        docs.mkdir()
        (docs / (SERIES + ".ics")).write_bytes(calendar.to_ical())
        (docs / "rugby-metadata-manifest.json").write_text(
            json.dumps({"schema_version": 2, "fixtures": prior}),
            encoding="utf-8")
        nfo_files, manifest = plex_nfo.build(config, docs, now)

    fixtures = manifest["fixtures"]
    # Some older official PWR fixtures (notably 2020/21) have no round in
    # the API. Preserve them as unnumbered, rather than inventing episode
    # numbers or silently excluding them from the historical catalogue.
    if len({f["id"] for f in fixtures}) != len(fixtures):
        raise ValueError("Duplicate historical PWR fixture identities")
    known_ids = {f["id"] for f in fixtures}
    source_ids = [SERIES + ":" + str(e.get("UID", "")).strip() for e in events]
    if len(set(source_ids)) != len(source_ids) or not set(known_ids).issubset(source_ids):
        raise ValueError("Invalid or duplicate historical PWR fixture UIDs")
    old_assignments = {f["id"]: f for f in prior}
    records = []
    for fixture in fixtures:
        if fixture["season"] != year:
            raise ValueError(f"Unexpected season {fixture['season']} for {fixture['id']}")
        if fixture.get("episode") is None or fixture.get("nfo") not in nfo_files:
            raise ValueError(f"Unnumbered or NFO-less historical fixture {fixture['id']}")
        if not fixture["id"].startswith(SERIES + ":pwr-"):
            raise ValueError(f"Unexpected fixture identity {fixture['id']}")
        prev = old_assignments.get(fixture["id"])
        if prev and (prev["round"], prev["episode"]) != (
                fixture["round"], fixture["episode"]):
            raise ValueError(f"Existing numbering changed for {fixture['id']}")
        records.append({
            "_id": fixture["id"],
            "series_id": SERIES,
            "season": year,
            "episode": fixture["episode"],
            "title": fixture["home"] + " vs " + fixture["away"],
            "scheduled": dt.datetime.fromisoformat(
                fixture["kickoff_utc"]).astimezone(UTC),
            "home": fixture["home"],
            "away": fixture["away"],
            "round": fixture["round"],
            "venue": fixture["venue"],
            "showtitle": fixture["showtitle"],
            "nfo_path": fixture["nfo"],
            "nfo": nfo_files[fixture["nfo"]].decode("utf-8"),
            "external_ids": {},
            "metadata_provider": "calendar/rugby",
        })
    for event in events:
        eid = SERIES + ":" + str(event.get("UID", "")).strip()
        if eid in known_ids:
            continue
        # Only events without a source round may legitimately be unnumbered.
        summary = str(event.get("SUMMARY", ""))
        teams = plex_nfo.VERSUS.split(summary, maxsplit=1)
        description = str(event.get("DESCRIPTION", ""))
        if (not eid.startswith(SERIES + ":pwr-") or len(teams) != 2 or
                plex_nfo.ROUND.search(description)):
            raise ValueError(f"Unexplained loss of numbered PWR fixture: {eid}")
        kickoff = event.decoded("DTSTART")
        if isinstance(kickoff, dt.datetime):
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=UK)
            timed = kickoff.astimezone(UTC)
            season = plex_nfo.season_year(timed)
            date_only = None
        else:
            timed = None
            date_only = kickoff.isoformat()
            season = kickoff.year if kickoff.month >= 7 else kickoff.year - 1
        if season != year:
            raise ValueError(f"Unexpected unnumbered PWR season for {eid}")
        record = {
            "_id": eid, "series_id": SERIES, "season": year,
            "episode": None, "round": None, "title": summary,
            "scheduled": timed, "home": teams[0].strip(), "away": teams[1].strip(),
            "venue": str(event.get("LOCATION", "")).strip(),
            "showtitle": SHOW, "nfo_path": None, "nfo": None,
            "external_ids": {}, "metadata_provider": "calendar/rugby",
        }
        if date_only is not None:
            record["scheduled_date"] = date_only
        records.append(record)
    if len(records) != len(events):
        raise ValueError("Historical PWR fixture loss during reconstruction")
    identifiers = [(e["season"], e["episode"]) for e in records
                   if isinstance(e.get("episode"), int)]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate historical PWR season/episode numbering")
    return records


def persist(db, records, now=None):
    """Upsert historical fixtures only, preserving existing operational state."""
    from pymongo import UpdateOne
    from schedulers.series_ids import assert_migrated
    assert_migrated(db)

    if not records:
        raise ValueError("Refusing empty historical import")
    ids = [r["_id"] for r in records]
    expected_year = records[0]["season"]
    if any(r["season"] != expected_year or r["series_id"] != SERIES for r in records):
        raise ValueError("Historical import contains mixed series or years")
    targets = {item["_id"]: item for item in records}
    numbers = [r["episode"] for r in records if isinstance(r.get("episode"), int)]
    existing = list(db.episodes.find({"$or": [
        {"_id": {"$in": ids}},
        {"series_id": SERIES, "season": expected_year,
         "episode": {"$in": numbers}},
    ]}, {"_id": 1, "series_id": 1, "season": 1, "episode": 1}))
    conflicts = [
        doc["_id"] for doc in existing if (
            doc["_id"] not in targets or
            doc.get("series_id") != SERIES or
            doc.get("season") != expected_year or
            doc.get("episode") != targets[doc["_id"]]["episode"])
    ]
    if conflicts:
        raise ValueError(f"Historical fixture identities already used: {conflicts[:5]}")
    now = now or dt.datetime.now(UTC)
    # Historical records are immutable after first import. A later fetch
    # from a result-bearing API must not rewrite their accepted metadata.
    operations = [
        UpdateOne({"_id": item["_id"]}, {
            "$setOnInsert": item | {"created_at": now, "metadata_updated_at": now}
        }, upsert=True)
        for item in records
    ]
    result = db.episodes.bulk_write(operations, ordered=False)
    db.episodes.create_index([("series_id", 1), ("season", 1), ("episode", 1)])
    return {"matched": result.matched_count,
            "modified": result.modified_count,
            "upserted": result.upserted_count}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2025,
                        help="Season starting year, default 2025 (2025/26)")
    parser.add_argument("--apply", action="store_true",
                        help="Write historical episode records into MongoDB")
    parser.add_argument("--all-available", action="store_true",
                        help="Backfill supported unimported PWR seasons 2017/18 through 2024/25")
    parser.add_argument("--database", default=os.environ.get("MONGODB_DATABASE", "pvr"))
    args = parser.parse_args(argv)
    try:
        url = api_url(args.year)
    except ValueError as exc:
        parser.error(str(exc))
    client = None
    try:
        if args.apply:
            mongo_uri = os.environ.get("MONGODB_URI")
            if not mongo_uri:
                mongo_uri = (pathlib.Path.home() / ".mongodb" / "pvr").read_text(
                    encoding="utf-8").strip()
            if not mongo_uri:
                raise ValueError("MongoDB URI is empty")
            from pymongo import MongoClient
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=15000)
            client.admin.command("ping")
        db = client[args.database] if client is not None else None
        years = list(range(2017, 2025)) if args.all_available else [args.year]
        batches = []
        # Preflight every requested season before writing any of them.
        for year in years:
            calendar = fetch_pwr(api_url(year))
            records = prepare(year, calendar, previous_fixtures(db, year))
            rounds = Counter(str(r["round"]) if r["round"] is not None else "unknown"
                             for r in records)
            times = [r["scheduled"] for r in records if r.get("scheduled")]
            print(json.dumps({
                "season": year, "season_id": int(f"{year}01"),
                "fixtures": len(records), "numbered": sum(
                    isinstance(r["episode"], int) for r in records),
                "unnumbered": sum(r["episode"] is None for r in records),
                "rounds": dict(sorted(rounds.items())),
                "preview": not args.apply,
                "first": min(times).date().isoformat() if times else None,
                "last": max(times).date().isoformat() if times else None,
            }, sort_keys=True))
            batches.append((year, records))
        if args.apply:
            for year, records in batches:
                print(json.dumps({"season": year, "write": persist(db, records)},
                                 sort_keys=True))
    except (OSError, ValueError, KeyError) as exc:
        print(f"PWR historical importer: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
