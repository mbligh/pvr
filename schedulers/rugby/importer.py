#!/usr/bin/env python3
"""Import spoiler-free rugby fixture metadata into MongoDB Atlas.

Historical calendar logic is vendored under schedulers/rugby/legacy/.
Current mbligh/calendar publication is not modified.

Bootstrap from the published ICS snapshots, NOT newly fetched historical scores.
Subsequent imports use snapshots and numbering assignments retained in Atlas.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEGACY = pathlib.Path(__file__).resolve().parent / "legacy"
sys.path.insert(0, str(LEGACY))

from icalendar import Calendar  # noqa: E402
import filter as calendar_filter  # noqa: E402
import plex_nfo  # noqa: E402
from pwr import fetch_pwr  # noqa: E402
from prem import fetch_prem  # noqa: E402
from world_rugby import fetch_world_rugby  # noqa: E402
from schedulers.series_ids import assert_migrated, canonicalize, CANONICAL_TO_LEGACY
from schedulers.rugby.prem_identity import reconcile_uids, season_for
from schedulers.rugby.wxv import (
    WOMENS, WXV, split_snapshot, legacy_id, validate_migration,
)

LEGACY_BASE = "https://raw.githubusercontent.com/mbligh/calendar/main/docs/"
UTC = dt.timezone.utc


def legacy_content(filename):
    """Initial snapshots preserve existing spoiler-free historical data and numbering."""
    req = urllib.request.Request(LEGACY_BASE + filename, headers={
        "User-Agent": "mbligh-pvr/0.1", "Accept": "application/octet-stream"
    })
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read()


def read_snapshot(db, slug):
    record = db.calendar_snapshots.find_one({"_id": slug}) if db is not None else None
    if record:
        return Calendar.from_ical(record["ics"].encode("utf-8"))
    # Published historical snapshots predate the canonical directory slugs.
    # Fetch the old public URL for first bootstrap, retaining original UIDs.
    legacy = CANONICAL_TO_LEGACY.get(slug, slug)
    return Calendar.from_ical(legacy_content(legacy + ".ics"))


def old_manifest(db):
    record = db.metadata_state.find_one({"_id": "rugby_manifest"}) if db is not None else None
    if record:
        return canonicalize(record["value"])
    data = json.loads(legacy_content("rugby-metadata-manifest.json"))
    if data.get("schema_version") != 2:
        raise ValueError("Legacy manifest schema is not version 2")
    return canonicalize(data)


def fetch_calendar(entry, now):
    kind = entry.get("type")
    if kind == "pwr":
        return fetch_pwr(entry["url"])
    if kind == "prem":
        # The provider uses YYYY01 for the July-June campaign.
        season = season_for(now)
        separator = "&" if "?" in entry["url"] else "?"
        return fetch_prem(entry["url"] + f"{separator}season={season}",
                          comp_id=entry["compId"], season=season)
    if kind == "world-rugby":
        return fetch_world_rugby(entry["group"], now)
    url = entry.get("url")
    if not url:
        import urllib.parse
        url = ("https://calendar.google.com/calendar/ical/"
               + urllib.parse.quote(entry["id"], safe="") + "/public/basic.ics")
    return calendar_filter.fetch(url)


def build_catalogue(db, config, now, refresh):
    """Build in memory first. No Atlas writes until ALL source parsing/numbering succeeds."""
    config = json.loads(json.dumps(config))
    prior_manifest = old_manifest(db)
    snapshots = {}
    prem_metadata = {}
    # First WXV migration must use already accepted score-free events.
    # The old women's calendar includes WXV; on subsequent runs the two
    # Atlas snapshots are separate. Never ingest old results from an API.
    original_womens = read_snapshot(db, WOMENS)
    intl_previous, wxv_seed = split_snapshot(original_womens)
    zone = ZoneInfo(config.get("timezone", "UTC"))
    with tempfile.TemporaryDirectory(prefix="pvr-rugby-") as temp:
        output = pathlib.Path(temp)
        docs = output / "docs"
        docs.mkdir()
        for entry in config["calendars"]:
            slug = entry["slug"]
            if slug == WOMENS:
                previous = intl_previous
            elif slug == WXV:
                current = (db.calendar_snapshots.find_one({"_id": WXV})
                           if db is not None else None)
                previous = (Calendar.from_ical(current["ics"].encode("utf-8"))
                            if current else wxv_seed)
            else:
                previous = read_snapshot(db, slug)
            if refresh:
                source = fetch_calendar(entry, now)
                if slug == "Rugby_Mens_Prem":
                    source, prem_metadata = reconcile_uids(source, previous)
                merged = calendar_filter.merge(source, previous, now, zone, entry["name"])
            else:
                merged = previous
            data = merged.to_ical()
            if not merged.walk("VEVENT"):
                raise ValueError("Empty rugby calendar: " + slug)
            snapshots[slug] = data.decode("utf-8")
            (docs / (slug + ".ics")).write_bytes(data)
        (docs / "rugby-metadata-manifest.json").write_text(
            json.dumps(prior_manifest), encoding="utf-8"
        )
        # Reuse the legacy numbering, NFO generation, and old assignment
        # preservation rather than reinventing the episode algorithm.
        output_files, manifest = plex_nfo.build(
            config, docs, now, prem_metadata=prem_metadata)

    episodes = []
    for fixture in manifest["fixtures"]:
        record = {
            "_id": fixture["id"],
            "series_id": fixture["competition"],
            "season": fixture["season"],
            "episode": fixture["episode"],
            "title": fixture["home"] + " vs " + fixture["away"],
            "scheduled": dt.datetime.fromisoformat(fixture["kickoff_utc"]).astimezone(UTC),
            "home": fixture["home"],
            "away": fixture["away"],
            "round": fixture["round"],
            "venue": fixture["venue"],
            "showtitle": fixture["showtitle"],
            "nfo_path": fixture["nfo"],
            "external_ids": (
                {"rugbypass": fixture.get("rugbypass_id")}
                | ({"prem": prem_metadata[fixture["id"].split(":", 1)[1]]["prem"]}
                   if fixture["competition"] == "Rugby_Mens_Prem" and
                   fixture["id"].split(":", 1)[1] in prem_metadata else {})
            ),
            "metadata_provider": ("prem/official" if fixture["competition"] ==
                                  "Rugby_Mens_Prem" else "calendar/rugby"),
            "status": "scheduled",
        }
        if fixture["nfo"]:
            record["nfo"] = output_files[fixture["nfo"]].decode("utf-8")
        episodes.append(record)
    # ICS feeds for Six Nations and Premiership can lack reliable round/episode
    # numbers. They still represent real scheduled fixtures and belong in Atlas.
    # The published manifest above remains authoritative where numbering exists.
    known_ids = {ep["_id"] for ep in episodes}
    for entry in config["calendars"]:
        slug = entry["slug"]
        calendar = Calendar.from_ical(snapshots[slug].encode("utf-8"))
        for event in calendar.walk("VEVENT"):
            uid = str(event.get("UID", "")).strip()
            if not uid:
                continue
            fixture_id = slug + ":" + uid
            if fixture_id in known_ids:
                continue
            start = calendar_filter.start_time(event, zone)
            if start is None:
                continue
            title = str(event.get("SUMMARY", "")).strip()
            # These snapshots are already spoiler-filtered, but avoid merging
            # other non-fixture events into the episode catalogue.
            teams = plex_nfo.VERSUS.split(title, maxsplit=1)
            if len(teams) != 2:
                continue
            timed = isinstance(event.decoded("DTSTART"), dt.datetime)
            season = (start.astimezone(plex_nfo.UK).year
                      if (entry.get("type") == "world-rugby" or
                          entry.get("season_mode") == "calendar")
                      else plex_nfo.season_year(start))
            episode = {
                "_id": fixture_id,
                "series_id": slug,
                "season": season,
                "episode": None,
                "title": title,
                "scheduled": start.astimezone(UTC) if timed else None,
                "scheduled_date": start.date().isoformat() if not timed else None,
                "home": teams[0].strip(),
                "away": teams[1].strip(),
                "round": (prem_metadata.get(uid, {}).get("round")
                          if slug == "Rugby_Mens_Prem" else None),
                "venue": str(event.get("LOCATION", "")).strip(),
                "showtitle": entry.get("nfo_showtitle", entry["name"]),
                "nfo_path": None,
                "external_ids": (
                    {"prem": prem_metadata[uid]["prem"]}
                    if slug == "Rugby_Mens_Prem" and uid in prem_metadata else {}
                ),
                "metadata_provider": ("prem/official" if slug ==
                                      "Rugby_Mens_Prem" else "calendar/rugby"),
                "status": "scheduled",
            }
            episodes.append(episode)
            known_ids.add(fixture_id)
    if not episodes:
        raise ValueError("No rugby episode metadata built")
    return snapshots, manifest, episodes


def persist(db, snapshots, manifest, episodes, now):
    from pymongo import UpdateOne

    # Preflight *before any writes*: if recordings exist in the old Intl
    # directory, we cannot safely reassign them to WXV without moving
    # their physical files. Keep all old WXV records as disabled history.
    targets = [ep for ep in episodes if ep["series_id"] == WXV]
    old_ids = [legacy_id(ep["_id"]) for ep in targets]
    previous = list(db.episodes.find({"_id": {"$in": old_ids}})) if old_ids else []
    media = list(db.files.find({"episode_id": {"$in": old_ids}})) if previous else []
    validate_migration(episodes, previous, files=media)
    new_ids = [ep["_id"] for ep in targets]
    already = list(db.episodes.find({"_id": {"$in": new_ids}})) if new_ids else []
    for doc in already:
        if (doc.get("series_id") != WXV or
                doc.get("nfo_path") not in (None,
                    next(ep["nfo_path"] for ep in targets if ep["_id"] == doc["_id"]))):
            raise ValueError(f"Conflicting existing WXV episode {doc['_id']}")

    # Never delete merely missing fixtures or existing acquisition state.
    operations = [
        UpdateOne({"_id": episode["_id"]}, {
            "$set": {k: v for k, v in episode.items()
                     if k != "_id" and
                     not (episode["series_id"] == "Rugby_Mens_Prem" and
                          ((k == "external_ids" and
                            "prem" not in episode["external_ids"]) or
                           (k == "round" and v is None)))}
                    | {"metadata_updated_at": now},
            "$setOnInsert": {"created_at": now}
        }, upsert=True)
        for episode in episodes
    ]
    db.episodes.bulk_write(operations, ordered=False)
    for old in previous:
        replaced = WXV + ":" + old["_id"][len(WOMENS) + 1:]
        # Use a tombstone rather than deleting an episode that has appeared
        # in previous published manifests or third-party calendars.
        db.episodes.update_one(
            {"_id": old["_id"], "series_id": WOMENS},
            {"$set": {"status": "superseded", "superseded_by": replaced,
                      "metadata_updated_at": now}})
        # Preserve release association while changing the canonical episode.
        db.releases.update_many(
            {"episode_id": old["_id"]},
            {"$set": {"episode_id": replaced}})
        db.jobs.update_many(
            {"episode_id": old["_id"]},
            {"$set": {"episode_id": replaced}})
    # Explicitly cancelled events are retained as historical records but
    # marked cancelled for the calendar publisher and acquisition worker.
    for slug, ics in snapshots.items():
        calendar = Calendar.from_ical(ics.encode("utf-8"))
        cancelled = calendar.get("x-wr-cancelled-uid", [])
        if not isinstance(cancelled, list):
            cancelled = [cancelled]
        for uid in cancelled:
            db.episodes.update_many(
                {"_id": slug + ":" + str(uid), "series_id": slug},
                {"$set": {"status": "cancelled", "metadata_updated_at": now}})
        db.calendar_snapshots.update_one({"_id": slug}, {
            "$set": {"ics": ics, "updated_at": now}
        }, upsert=True)
    db.metadata_state.update_one({"_id": "rugby_manifest"}, {
        "$set": {"value": manifest, "updated_at": now}
    }, upsert=True)
    db.episodes.create_index([("series_id", 1), ("season", 1), ("episode", 1)])
    db.episodes.create_index([("scheduled", 1)])
    return len(episodes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path,
                        default=LEGACY / "calendars.json")
    parser.add_argument("--database", default=os.environ.get("MONGODB_DATABASE", "pvr"))
    parser.add_argument("--no-refresh", action="store_true",
                        help="Import existing spoiler-free calendar snapshots without fetching live APIs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report only; does not require Atlas credentials")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    now = dt.datetime.now(UTC)

    client = None
    db = None
    if not args.dry_run:
        uri = os.environ.get("MONGODB_URI")
        if not uri:
            uri_file = pathlib.Path.home() / ".mongodb" / "pvr"
            try:
                uri = uri_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                parser.error(f"Cannot read MongoDB URI from {uri_file}: {exc.strerror}")
            if not uri:
                parser.error(f"MongoDB URI file is empty: {uri_file}")
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=15000)
        client.admin.command("ping")
        db = client[args.database]
    try:
        if db is not None:
            assert_migrated(db)
        snapshots, manifest, episodes = build_catalogue(
            db, config, now, refresh=not args.no_refresh
        )
        print(json.dumps({
            "calendars": len(snapshots),
            "episodes": len(episodes),
            "numbered": sum(e["episode"] is not None for e in episodes),
            "preview": args.dry_run,
            "database": args.database if not args.dry_run else None,
        }, indent=2))
        if db is not None:
            persist(db, snapshots, manifest, episodes, now)
            print("MongoDB upsert complete")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
