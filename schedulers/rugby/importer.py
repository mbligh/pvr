#!/usr/bin/env python3
"""Import spoiler-free rugby fixture metadata into MongoDB Atlas.

Fixture-source adapters, spoiler-safe calendar merging, and NFO generation
live alongside this importer in schedulers/rugby/.
Current mbligh/calendar publication is not modified.

Bootstrap from the published ICS snapshots, NOT newly fetched historical scores.
Subsequent imports use snapshots and numbering assignments retained in Atlas.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUGBY_DIR = pathlib.Path(__file__).resolve().parent

from icalendar import Calendar
from . import filter as calendar_filter
from . import plex_nfo
from .pwr import fetch_pwr
from .prem import fetch_prem
from .epcr import fetch_epcr
from .mens_six_nations import fetch_mens, fetch_womens, preserve_uids
from .world_rugby import fetch_world_rugby
from schedulers.series_ids import assert_migrated, canonicalize, CANONICAL_TO_LEGACY
from schedulers.rugby.prem_identity import reconcile_uids, season_for
from schedulers.rugby import prem_history
from schedulers.rugby.wxv import (
    WOMENS, WXV, split_snapshot, legacy_id, validate_migration,
)

LEGACY_BASE = "https://raw.githubusercontent.com/mbligh/calendar/main/docs/"
UTC = dt.timezone.utc
MENS_INTL = "Rugby_Mens_Intl"
EPCR_SERIES = "Rugby_Mens_Championship"
# These competitions have independent authoritative series.
MENS_INTL_DUPLICATE_PREFIXES = ("nc:", "6n:")
MENS_INTL_DUPLICATE_REGEX = r"^\s*(?:NC|6N):"


def strip_nations_from_intl(calendar):
    """Exclude legacy NC: and 6N: fixtures from the Men's Intl calendar."""
    cleaned = Calendar.from_ical(calendar.to_ical())
    cleaned.subcomponents = [
        component for component in cleaned.subcomponents
        if not (
            component.name == "VEVENT"
            and str(component.get("SUMMARY", "")).lstrip().casefold().startswith(MENS_INTL_DUPLICATE_PREFIXES)
        )
    ]
    return cleaned


def stale_nations_in_intl(db, snapshots, episodes):
    """Find stale Atlas duplicates, refusing any with related download history."""
    if MENS_INTL not in snapshots:
        return []
    if any(
        row.get("series_id") == MENS_INTL
        and str(row.get("title", "")).lstrip().casefold().startswith(MENS_INTL_DUPLICATE_PREFIXES)
        for row in episodes
    ):
        raise ValueError("Dedicated competition fixtures remain in Men's Intl import")
    duplicates = list(db.episodes.find({
        "series_id": MENS_INTL, "title": {"$regex": MENS_INTL_DUPLICATE_REGEX, "$options": "i"}
    }))
    ids = [row["_id"] for row in duplicates]
    if ids:
        for name in ("files", "releases", "jobs"):
            if list(getattr(db, name).find({"episode_id": {"$in": ids}}, {"_id": 1})):
                raise ValueError(
                    f"Refusing to remove Men's Intl NC/6N duplicates referenced by {name}"
                )
    return ids


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
    # A newly added official competition has no historical public ICS snapshot.
    if slug == EPCR_SERIES:
        empty = Calendar()
        empty.add("version", "2.0")
        return empty
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
    if kind == "six-nations-mens":
        return fetch_mens(now=now)
    if kind == "six-nations-womens":
        return fetch_womens(now=now)
    if kind == "epcr":
        season = season_for(now)
        separator = "&" if "?" in entry["url"] else "?"
        return fetch_epcr(entry["url"] + f"{separator}season={season}",
                          comp_id=entry["compId"], season=season)
    if kind == "world-rugby":
        return fetch_world_rugby(entry["group"], now)
    url = entry.get("url")
    if not url:
        import urllib.parse
        url = ("https://calendar.google.com/calendar/ical/"
               + urllib.parse.quote(entry["id"], safe="") + "/public/basic.ics")
    return calendar_filter.fetch(url)


def build_catalogue(db, config, now, refresh, *, backfill_prem_2026=False):
    """Build in memory first. No Atlas writes until ALL source parsing/numbering succeeds."""
    if backfill_prem_2026 and not refresh:
        raise ValueError("Historical PREM backfill requires live official fixture refresh")
    config = json.loads(json.dumps(config))
    prior_manifest = old_manifest(db)
    if backfill_prem_2026:
        prior_manifest = prem_history.strip_round_three_assignments(prior_manifest)
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
                if current:
                    previous = Calendar.from_ical(current["ics"].encode("utf-8"))
                elif db is None:
                    # Offline smoke tests have no Atlas snapshots. Read the
                    # already published, score-free canonical WXV snapshot,
                    # rather than seeding from a now-separated Intl feed.
                    previous = read_snapshot(None, WXV)
                else:
                    # Initial Atlas migration must use accepted Intl history.
                    previous = wxv_seed
            else:
                previous = read_snapshot(db, slug)
            if refresh:
                source = fetch_calendar(entry, now)
                if slug == "Rugby_Mens_Prem":
                    source, prem_metadata = reconcile_uids(source, previous)
                if slug in ("Rugby_Mens_6N", "Rugby_Womens_6N"):
                    source = preserve_uids(source, previous)
                merged = calendar_filter.merge(
                    source, previous, now, zone, entry["name"],
                    # EPCR's adapter deliberately strips results before the
                    # merger sees historical fixtures. Other feeds retain the
                    # normal first-seen-after-kickoff prohibition.
                    allow_new_past=(slug in (EPCR_SERIES, "Rugby_Mens_6N", "Rugby_Womens_6N")))
                if slug == prem_history.SERIES and backfill_prem_2026:
                    merged, _ = prem_history.add_history(source, merged, now)
            else:
                merged = previous
            if slug == MENS_INTL:
                # Prior international snapshots retain NC/6N fixtures that
                # already have their own dedicated competition series.
                merged = strip_nations_from_intl(merged)
            data = merged.to_ical()
            if not merged.walk("VEVENT") and not (
                    slug == EPCR_SERIES and not refresh):
                raise ValueError("Empty rugby calendar: " + slug)
            snapshots[slug] = data.decode("utf-8")
            (docs / (slug + ".ics")).write_bytes(data)
        (docs / "rugby-metadata-manifest.json").write_text(
            json.dumps(prior_manifest), encoding="utf-8"
        )
        # Shared numbering and NFO generation preserve previously accepted
        # episode assignments across imports.
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
            "metadata_provider": (
                "prem/official" if fixture["competition"] == "Rugby_Mens_Prem"
                else "epcr/official" if fixture["competition"] == EPCR_SERIES
                else "calendar/rugby"),
            "status": "scheduled",
        }
        # Official fixture IDs are scheduler metadata. Recording IDs belong
        # to releases and matchers, not to episode source ingestion.
        uid = fixture["id"].split(":", 1)[1]
        if fixture["competition"] == "Rugby_Mens_Prem" and uid in prem_metadata:
            record["external_ids"] = {"prem": prem_metadata[uid]["prem"]}
        if fixture["competition"] == EPCR_SERIES and uid.startswith("epcr-"):
            official_id = uid.split("@", 1)[0].removeprefix("epcr-")
            if official_id.isdecimal():
                record["external_ids"] = {"epcr": official_id}
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
                    if slug == "Rugby_Mens_Prem" and uid in prem_metadata
                    else {"epcr": str(event.get("X-EPCR-ID"))}
                    if slug == EPCR_SERIES and event.get("X-EPCR-ID")
                    else {}
                ),
                "metadata_provider": (
                    "prem/official" if slug == "Rugby_Mens_Prem"
                    else "epcr/official" if slug == EPCR_SERIES
                    else "calendar/rugby"),
                "status": "scheduled",
            }
            episodes.append(episode)
            known_ids.add(fixture_id)
    if not episodes:
        raise ValueError("No rugby episode metadata built")
    return snapshots, manifest, episodes


def validate_existing(db, episodes):
    """Read-only migration/conflict checks shared by preview and apply.

    Existing recordings in the old women's-international library cannot
    silently move to WXV. Fail before touching Atlas in either mode.
    """
    targets = [ep for ep in episodes if ep["series_id"] == WXV]
    old_ids = [legacy_id(ep["_id"]) for ep in targets]
    previous = list(db.episodes.find({"_id": {"$in": old_ids}})) if old_ids else []
    media = list(db.files.find({"episode_id": {"$in": old_ids}})) if previous else []
    validate_migration(episodes, previous, files=media)
    new_ids = [ep["_id"] for ep in targets]
    already = list(db.episodes.find({"_id": {"$in": new_ids}})) if new_ids else []
    target_paths = {ep["_id"]: ep["nfo_path"] for ep in targets}
    for doc in already:
        if (doc.get("series_id") != WXV or
                doc.get("nfo_path") not in (None, target_paths[doc["_id"]])):
            raise ValueError(f"Conflicting existing WXV episode {doc['_id']}")
    return previous


def persist(db, snapshots, manifest, episodes, now):
    from pymongo import UpdateOne

    # Validate before writes, as in preview. Retain the previous WXV
    # records for subsequent association migrations.
    previous = validate_existing(db, episodes)
    duplicates = stale_nations_in_intl(db, snapshots, episodes)

    # Never delete merely missing fixtures or existing acquisition state.
    # In particular, previous imports stored some source recording IDs on
    # episodes. Avoid replacing the whole external_ids document: the scheduler
    # only owns official fixture IDs, while the release matcher owns recordings.
    operations = []
    for episode in episodes:
        fields = {
            k: v for k, v in episode.items()
            if k not in ("_id", "external_ids") and
            not (episode["series_id"] == "Rugby_Mens_Prem" and
                 k == "round" and v is None)
        }
        prem_id = (episode.get("external_ids") or {}).get("prem")
        if prem_id is not None:
            fields["external_ids.prem"] = prem_id
        epcr_id = (episode.get("external_ids") or {}).get("epcr")
        if epcr_id is not None:
            fields["external_ids.epcr"] = epcr_id
        fields["metadata_updated_at"] = now
        operations.append(UpdateOne({"_id": episode["_id"]}, {
            "$set": fields,
            "$setOnInsert": {"created_at": now},
        }, upsert=True))
    db.episodes.bulk_write(operations, ordered=False)
    if duplicates:
        db.episodes.delete_many({
            "_id": {"$in": duplicates}, "series_id": MENS_INTL,
            "title": {"$regex": MENS_INTL_DUPLICATE_REGEX, "$options": "i"}
        })
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path,
                        default=RUGBY_DIR / "calendars.json")
    parser.add_argument("--database", default=os.environ.get("MONGODB_DATABASE", "pvr"))
    parser.add_argument("--no-refresh", action="store_true",
                        help="Import existing spoiler-free calendar snapshots without fetching live APIs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="Write validated fixtures to Atlas (default: preview only)")
    mode.add_argument("--dry-run", action="store_true",
                      help="Preview only (compatibility alias; now the default)")
    parser.add_argument("--offline", action="store_true",
                        help="Preview using published bootstrap snapshots; no Atlas access")
    parser.add_argument("--backfill-prem-2026", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.offline and args.apply:
        parser.error("--offline cannot be combined with --apply")
    if args.backfill_prem_2026 and (args.offline or args.no_refresh):
        parser.error("PREM 2026 backfill needs Atlas and live official fixture refresh")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    now = dt.datetime.now(UTC)

    client = None
    db = None
    if not args.offline:
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
        kwargs = {"backfill_prem_2026": True} if args.backfill_prem_2026 else {}
        snapshots, manifest, episodes = build_catalogue(
            db, config, now, refresh=not args.no_refresh, **kwargs
        )
        prem_plan = prem_history.validate_plan(db, episodes) if args.backfill_prem_2026 else None
        if db is not None and not args.apply:
            validate_existing(db, episodes)
        print(json.dumps({
            "calendars": len(snapshots),
            "episodes": len(episodes),
            "numbered": sum(e["episode"] is not None for e in episodes),
            "preview": not args.apply,
            "database": args.database if db is not None else None,
            **({"prem_history_2026": prem_plan} if prem_plan is not None else {}),
        }, indent=2))
        if args.apply:
            persist(db, snapshots, manifest, episodes, now)
            print("MongoDB upsert complete")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
