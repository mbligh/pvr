"""Shared CLI and cross-provider selection for official rugby team sheets."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import uuid

from schedulers.rugby import prem_lineups, pwr_lineups, world_rugby_lineups

UTC = dt.timezone.utc
PROVIDERS = {
    "pwr": pwr_lineups,
    "prem": prem_lineups,
    "world": world_rugby_lineups,
}
SERIES_PROVIDER = {
    pwr_lineups.SERIES: "pwr",
    prem_lineups.SERIES: "prem",
    **{value: "world" for value in world_rugby_lineups.SERIES},
}


def active_providers(provider="all", series=None, fixture=None):
    """Validate provider selection, including ambiguous numeric match IDs."""
    if provider not in ("all", *PROVIDERS):
        raise ValueError(f"Unknown lineup provider: {provider}")
    if series is not None:
        selected = SERIES_PROVIDER.get(series)
        if selected is None:
            raise ValueError(f"Unsupported series: {series}")
        if provider not in ("all", selected):
            raise ValueError(f"Series {series} is not provided by {provider}")
        provider = selected
    if fixture is not None and provider == "all":
        raise ValueError("--fixture requires --provider or --series")
    return list(PROVIDERS) if provider == "all" else [provider]


def parsed_fixture(provider, fixture):
    if fixture is None:
        return None
    if provider == "world":
        try:
            return str(uuid.UUID(str(fixture)))
        except (ValueError, AttributeError) as exc:
            raise ValueError("World Rugby fixture must be a UUID") from exc
    if not str(fixture).isdecimal() or int(fixture) <= 0:
        raise ValueError("PREM/PWR fixture must be a positive numeric match ID")
    return int(fixture)


def date_sort(episode):
    scheduled = episode.get("scheduled")
    if scheduled is not None:
        return (scheduled.replace(tzinfo=UTC) if scheduled.tzinfo is None
                else scheduled.astimezone(UTC))
    date_only = episode.get("scheduled_date")
    if date_only:
        return dt.datetime.combine(dt.date.fromisoformat(date_only),
                                   dt.time.min, tzinfo=UTC)
    return dt.datetime.min.replace(tzinfo=UTC)


def select_candidates(database, *, provider="all", series=None,
                      fixture=None, season=None, force=False, limit=50,
                      now=None):
    """Plan across providers; limit is global, not one quota per provider."""
    now = now or dt.datetime.now(UTC)
    chosen = []
    for name in active_providers(provider, series, fixture):
        adapter = PROVIDERS[name]
        requested = parsed_fixture(name, fixture)
        kwargs = dict(season=season, fixture=requested, force=force,
                      limit=0, now=now)
        if name == "world":
            kwargs["series"] = series
        for episode in adapter.candidates(database, **kwargs):
            if series is not None and episode.get("series_id") != series:
                continue
            chosen.append({
                "provider": name,
                "module": adapter,
                "episode": episode,
                "fixture_id": adapter.fixture_id(episode),
            })
    chosen.sort(
        key=lambda row: (date_sort(row["episode"]), row["episode"]["_id"]),
        reverse=True)
    return chosen[:limit] if limit else chosen


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read or import PREM, PWR and World Rugby team selections")
    parser.add_argument("--apply", action="store_true",
                        help="Save into MongoDB (default: preview only)")
    parser.add_argument("--force", action="store_true",
                        help="Recheck complete/cooling-down and historical fixtures")
    parser.add_argument("--provider", choices=("all", *PROVIDERS), default="all",
                        help="Source API, default all three")
    parser.add_argument("--series", choices=sorted(SERIES_PROVIDER),
                        help="Restrict to one canonical PVR series")
    parser.add_argument("--fixture", help="Provider match ID (numeric or UUID)")
    parser.add_argument("--season", type=int, help="PVR episode season")
    parser.add_argument("--limit", type=int, default=50,
                        help="Maximum total checks (0 = all)")
    parser.add_argument("--database", default=os.getenv("MONGODB_DATABASE", "pvr"))
    args = parser.parse_args(argv)
    if args.limit < 0 or args.season is not None and args.season < 1900:
        parser.error("Invalid limit or season")
    try:
        for provider in active_providers(args.provider, args.series, args.fixture):
            parsed_fixture(provider, args.fixture)
    except ValueError as exc:
        parser.error(str(exc))

    uri = os.getenv("MONGODB_URI")
    if not uri:
        try:
            uri = (Path.home() / ".mongodb" / "pvr").read_text(
                encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"Cannot read MongoDB URI file: {exc}")
    if not uri:
        parser.error("MongoDB URI is empty")
    from pymongo import MongoClient
    now = dt.datetime.now(UTC)
    errors = 0
    with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
        selected = select_candidates(
            client[args.database], provider=args.provider, series=args.series,
            fixture=args.fixture, season=args.season, force=args.force,
            limit=args.limit, now=now)
        for row in selected:
            provider = row["provider"]
            adapter = row["module"]
            episode = row["episode"]
            fixture = row["fixture_id"]
            if not args.apply:
                print(json.dumps({
                    "provider": provider, "series_id": episode["series_id"],
                    "fixture_id": fixture, "episode_id": episode["_id"],
                    "preview": True,
                }))
                continue
            try:
                record = adapter.normalize(adapter.fetch(fixture), episode, now=now)
                updated = adapter.upsert(client[args.database], record)
                print(json.dumps({
                    "provider": provider, "series_id": episode["series_id"],
                    "fixture_id": fixture, "episode_id": episode["_id"],
                    "status": record["status"],
                    "home": len(record["home"]), "away": len(record["away"]),
                    "updated": updated,
                }))
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                errors += 1
                print(f"{provider} lineup {fixture}: {exc}", file=sys.stderr)
    print(json.dumps({
        "candidates": len(selected), "preview": not args.apply,
        "errors": errors,
    }), file=sys.stderr)
    return 1 if errors else 0
