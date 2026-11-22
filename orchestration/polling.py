"""Shared, Atlas-persisted, schedule-aware recording discovery throttling.

No provider HTTP requests are performed here. Each source has one global
poll-state document; failed checks have their own bounded retry backoff.
Cron may invoke runners every five minutes without reindexing needlessly.
"""
import datetime as dt
import os
from pathlib import Path

from filing.publish import canonical_paths, validate_nfo
from orchestration.acquire import aware, release_is_ready
from orchestration.source_registry import SOURCE_SERIES

UTC = dt.timezone.utc
DAILY = dt.timedelta(days=1)
AGE_INTERVALS = (
    (dt.timedelta(hours=2), dt.timedelta(minutes=5)),
    (dt.timedelta(hours=12), dt.timedelta(minutes=15)),
    (dt.timedelta(days=2), dt.timedelta(hours=1)),
    (dt.timedelta(days=7), dt.timedelta(hours=6)),
    (dt.timedelta(days=30), dt.timedelta(days=1)),
)
WEEKLY = dt.timedelta(days=7)
BLOCKED = frozenset(("cancelled", "canceled", "superseded"))


def age_interval(age):
    """Required polling interval for an unfinished recording."""
    if age < dt.timedelta(0):
        raise ValueError("Fixture has not finished")
    for upper, interval in AGE_INTERVALS:
        if age < upper:
            return interval
    return WEEKLY


def expected_finish(episode):
    """F1 has explicit session end; rugby defaults to two hours after kickoff."""
    if episode.get("series_id") == "Formula_1":
        return aware(episode.get("scheduled_end"))
    explicit = aware(episode.get("scheduled_end"))
    if explicit:
        return explicit
    start = aware(episode.get("scheduled"))
    if start is None:
        return None
    duration = episode.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 < duration <= 86400:
        return start + dt.timedelta(seconds=duration)
    return start + dt.timedelta(hours=2)


def eligible_missing(database, *, sources, now=None, sports_root="/data/sports",
                     host=None, season=None, series_ids=None,
                     round_no=None, episode_number=None):
    """Read-only list of finished, numbered, unpublished, usable fixtures.

    A canonical file, another registered published file or moved/inconsistent
    publication history blocks re-polling. Unnumbered/cancelled/unfilable
    fixtures cannot be acquired and must never cause repeated indexer checks.
    """
    now = aware(now) or dt.datetime.now(UTC)
    supported = {name for source in sources for name in SOURCE_SERIES[source]}
    if series_ids is not None:
        supported.intersection_update(series_ids)
    if not supported:
        return []
    query = {"series_id": {"$in": sorted(supported)}}
    if season is not None:
        query["season"] = season
    if round_no is not None:
        query["round"] = round_no
    if episode_number is not None:
        query["episode"] = episode_number
    episodes = list(database.episodes.find(query))
    candidates = []
    root = Path(sports_root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Polling sports root must be an existing absolute directory")
    for episode in episodes:
        if (type(episode.get("episode")) is not int
                or episode.get("status") in BLOCKED
                or not isinstance(episode.get("_id"), str)):
            continue
        end = expected_finish(episode)
        if end is None or end > now:
            continue
        try:
            validate_nfo(episode)
            destination, nfo_path = canonical_paths(episode, root)
        except (KeyError, TypeError, ValueError):
            continue
        # Existing or partially moved canonical outputs need reconciliation,
        # not another remote-indexer scan.
        if os.path.lexists(destination) or os.path.lexists(nfo_path):
            continue
        candidates.append((episode, end))

    if not candidates:
        return []
    ids = [episode["_id"] for episode, _ in candidates]
    file_query = {"episode_id": {"$in": ids}}
    if host:
        file_query["host"] = host
    records = list(database.files.find(file_query))
    by_episode = {}
    for row in records:
        by_episode.setdefault(row.get("episode_id"), []).append(row)
    missing = []
    for episode, end in candidates:
        recorded = by_episode.get(episode["_id"], [])
        if any(record.get("file_state") == "moved" for record in recorded):
            continue
        published = False
        for record in recorded:
            path = record.get("path")
            if not isinstance(path, str):
                continue
            file = Path(path)
            if not file.is_absolute() or not file.is_relative_to(root.absolute()):
                continue
            try:
                if file.is_file() and file.stat().st_size > 0:
                    published = True
                    break
            except OSError:
                continue
        if not published:
            missing.append({"episode_id": episode["_id"],
                            "series_id": episode["series_id"], "finished": end})
    return missing


def plan(database, *, sources, now=None, sports_root="/data/sports",
         host=None, season=None, series_ids=None,
         round_no=None, episode_number=None):
    """Return per-source poll decisions, without modifying Atlas."""
    now = aware(now) or dt.datetime.now(UTC)
    missing = eligible_missing(database, sources=sources, now=now,
                               sports_root=sports_root, host=host, season=season,
                               series_ids=series_ids, round_no=round_no,
                               episode_number=episode_number)
    # A previously indexed, linked and still-usable release is acquisition
    # work, not discovery demand. Other providers may still be worth
    # checking to compare candidates for the same episode.
    linked = set()
    if missing:
        source_query = {"episode_id": {"$in": [e["episode_id"] for e in missing]},
                        "source": {"$in": list(sources)}}
        for release in database.releases.find(source_query):
            status = (release.get("availability_check") or {}).get("status")
            duration = release.get("duration")
            if (release_is_ready(release)
                    and status not in ("unavailable", "unsupported")
                    and (not isinstance(duration, (int, float)) or duration >= 3600)):
                linked.add((release.get("episode_id"), release.get("source")))
    result = {}
    for source in sources:
        state = database.indexer_poll_state.find_one({"_id": source}) or {}
        last = aware(state.get("last_success"))
        due_time = last + DAILY if last is not None else now
        urgent = None
        count = 0
        for event in missing:
            if (event["series_id"] not in SOURCE_SERIES[source]
                    or (event["episode_id"], source) in linked):
                continue
            count += 1
            age = now - event["finished"]
            interval = age_interval(age)
            # If a new fixture finished since the previous scan, search now
            # rather than waiting for the old daily/weekly polling cadence.
            when = (event["finished"] if last is None or last < event["finished"]
                    else last + interval)
            due_time = min(due_time, when)
            if urgent is None or interval < urgent:
                urgent = interval
        retry = aware(state.get("retry_not_before"))
        gated = max(due_time, retry) if retry is not None else due_time
        result[source] = {
            "source": source, "missing": count, "due": gated <= now,
            "reason": ("failure_backoff" if retry and retry > now and due_time <= now
                       else "due" if gated <= now else "not_due"),
            "next_at": gated, "interval_minutes": (
                int(urgent.total_seconds() // 60) if urgent is not None else 1440),
            "last_success": last,
        }
    return result, missing


def record_success(database, source, *, now=None):
    now = aware(now) or dt.datetime.now(UTC)
    database.indexer_poll_state.update_one(
        {"_id": source},
        {"$set": {"last_success": now, "last_attempt": now, "failures": 0},
         "$unset": {"retry_not_before": ""}},
        upsert=True)


def record_failure(database, source, *, now=None):
    now = aware(now) or dt.datetime.now(UTC)
    prior = database.indexer_poll_state.find_one({"_id": source}) or {}
    failures = max(0, int(prior.get("failures", 0))) + 1
    delay = dt.timedelta(minutes=min(60, 5 * 2 ** min(failures - 1, 5)))
    database.indexer_poll_state.update_one(
        {"_id": source},
        {"$set": {"last_attempt": now, "failures": failures,
                  "retry_not_before": now + delay}},
        upsert=True)
    return delay


def database_connection(database):
    """Open Atlas for runner preflight; credentials never enter logs."""
    import contextlib
    import os
    from pathlib import Path
    from pymongo import MongoClient

    uri = os.getenv("MONGODB_URI")
    if not uri:
        uri = (Path.home() / ".mongodb" / "pvr").read_text(encoding="utf-8").strip()
    if not uri:
        raise ValueError("MongoDB URI is empty")
    return MongoClient(uri, serverSelectionTimeoutMS=15000)


def status_summary(decisions, *, now=None):
    """JSON-serializable status for cron logs."""
    return [
        {"source": source, "due": data["due"], "reason": data["reason"],
         "missing": data["missing"], "interval_minutes": data["interval_minutes"],
         "next_at": data["next_at"].isoformat()}
        for source, data in decisions.items()
    ]
