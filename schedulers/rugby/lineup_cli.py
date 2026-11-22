"""Shared CLI and cross-provider selection for official rugby team sheets."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import uuid

from schedulers.rugby import prem_lineups, pwr_lineups, world_rugby_lineups, lineup_traffic

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
                      now=None, provider_limits=None):
    """Plan across providers; apply provider budgets before the global cap."""
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
    if provider_limits is None:
        return chosen[:limit] if limit else chosen
    # When all the newest fixtures belong to World Rugby, its cap must not
    # prevent otherwise eligible PWR or PREM matches from being selected.
    filtered = []
    counts = {provider: 0 for provider in PROVIDERS}
    for row in chosen:
        name = row["provider"]
        if counts[name] >= provider_limits[name]:
            continue
        filtered.append(row)
        counts[name] += 1
        if limit and len(filtered) >= limit:
            break
    return filtered


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
                        help="Global limit (0 = no global cap; provider caps still apply)")
    parser.add_argument("--max-world", type=int,
                        default=lineup_traffic.DEFAULT_WORLD_LIMIT,
                        help="Max World Rugby requests per run (default 8)")
    parser.add_argument("--max-provider", type=int,
                        default=lineup_traffic.DEFAULT_OTHER_LIMIT,
                        help="Max PWR and PREM requests per provider/run (default 12)")
    parser.add_argument("--world-delay", type=float,
                        default=lineup_traffic.DEFAULT_WORLD_DELAY,
                        help="Minimum interval between World Rugby requests (default 10s)")
    parser.add_argument("--provider-delay", type=float,
                        default=lineup_traffic.DEFAULT_OTHER_DELAY,
                        help="Minimum interval between PWR/PREM requests (default 3s)")
    parser.add_argument("--database", default=os.getenv("MONGODB_DATABASE", "pvr"))
    args = parser.parse_args(argv)
    if (args.limit < 0 or (args.season is not None and args.season < 1900)
            or args.max_world < 1 or args.max_provider < 1
            or not 0 <= args.world_delay <= 600
            or not 0 <= args.provider_delay <= 600):
        parser.error("Invalid limits, delays or season")
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
    with MongoClient(uri, serverSelectionTimeoutMS=15000) as client:
        database = client[args.database]
        limits = {"world": args.max_world, "pwr": args.max_provider,
                  "prem": args.max_provider}
        selected = select_candidates(
            database, provider=args.provider, series=args.series,
            fixture=args.fixture, season=args.season, force=args.force,
            limit=args.limit, now=now, provider_limits=limits)
        return lineup_traffic.process(
            database, selected, apply=args.apply,
            delays={"world": args.world_delay, "pwr": args.provider_delay,
                    "prem": args.provider_delay})
