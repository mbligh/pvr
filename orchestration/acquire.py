"""Preview-first missing-PWR-episode orchestration.

Only YouTube downloads are supported at this stage, and only when an
episode has exactly one matched, finished YouTube release. Decisions rely
on the local Atlas inventory and the existing quality selection policy.
"""
import datetime as dt
import os
import pathlib
import socket
import stat

from filing.publish import canonical_paths, validate_nfo
from matchers.pwr import SERIES_ID
from selection.quality import evaluate_file, select

INCOMING_NAME = "pvr-{id}.mkv"
BLOCKED_LIVE_STATES = {"is_live", "is_upcoming", "post_live"}


def aware(value):
    if not isinstance(value, dt.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def host_name(hostname=None):
    hostname = hostname or socket.gethostname()
    if not hostname or ":" in hostname or "/" in hostname:
        raise ValueError("Invalid host identifier")
    return hostname


def release_is_ready(release):
    if release.get("available") is False:
        return False
    metadata = release.get("metadata") or {}
    if metadata.get("live_status") in BLOCKED_LIVE_STATES:
        return False
    return True


def local_published(records, sports_root):
    """Any existing inventoried recording in the library blocks auto-filing.

    A file can be published but below preferred quality; upgrades are
    separate from this missing-episode acquisition workflow.
    """
    root = pathlib.Path(sports_root).absolute()
    for item in records:
        if item.get("file_state") == "moved":
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        candidate = pathlib.Path(path)
        if not candidate.is_absolute() or not candidate.is_relative_to(root):
            continue
        try:
            info = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
            # A published file with stale inventory metadata still counts.
            # Re-inventory/repair must be explicit, never silently publish
            # a second file because the size or mtime has changed.
            return path
    return None


def planning_rows(database, *, sports_root="/data/sports",
                  incoming_root="/data/incoming", season=None,
                  min_age_hours=3, retry_after_hours=6, hostname=None,
                  now=None):
    """Return (rows, counts) for all potentially due PWR fixtures.

    No filesystem or Atlas writes. Each row has one of:
    file, download_then_file, or skip. Remote release formats are
    intentionally *not* guessed from release metadata.
    """
    hostname = host_name(hostname)
    sports_root = pathlib.Path(sports_root).expanduser()
    incoming_root = pathlib.Path(incoming_root).expanduser()
    if not sports_root.is_absolute() or not incoming_root.is_absolute():
        raise ValueError("Library and incoming roots must be absolute")
    if not sports_root.is_dir() or not incoming_root.is_dir():
        raise ValueError("Library and incoming roots must already exist")
    if min_age_hours < 0 or retry_after_hours < 0:
        raise ValueError("Age and retry delay cannot be negative")
    now = aware(now) or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=min_age_hours)
    query = {"series_id": SERIES_ID, "season": {"$gte": 2025}}
    if season is not None:
        if season < 2025:
            raise ValueError("PWR season must be 2025 or later")
        query["season"] = season
    episodes = list(database.episodes.find(query))
    # Recent fixtures first by default, older-first for an explicit season
    # backfill. The date ordering does not alter the canonical episode ID.
    episodes.sort(key=lambda ep: (
        aware(ep.get("scheduled")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        ep.get("_id", "")), reverse=season is None)
    episodes_due = [
        ep for ep in episodes
        if isinstance(ep.get("episode"), int)
        and (scheduled := aware(ep.get("scheduled"))) is not None
        and scheduled <= cutoff
        and ep.get("status") not in ("cancelled", "canceled")
    ]
    ids = [ep["_id"] for ep in episodes_due]
    if not ids:
        return [], {"fixtures": len(episodes), "due": 0}
    releases = list(database.releases.find({
        "episode_id": {"$in": ids}, "source": "youtube",
    }))
    releases_by_ep = {}
    for release in releases:
        if release_is_ready(release):
            releases_by_ep.setdefault(release["episode_id"], []).append(release)
    records = list(database.files.find({
        "episode_id": {"$in": ids}, "host": hostname,
    }))
    records_by_ep = {}
    for record in records:
        records_by_ep.setdefault(record["episode_id"], []).append(record)
    # Historical failures may indicate a temporary source block. Avoid
    # hammering YouTube while leaving an explicit zero-delay override.
    recent_jobs_by_source = set()
    if retry_after_hours:
        recent_jobs = database.jobs.find({
            "type": "download", "host": hostname,
            "status": {"$in": ["failed", "running"]},
            "started_at": {"$gte": now - dt.timedelta(hours=retry_after_hours)},
        })
        recent_jobs_by_source = {
            j.get("source_uri") for j in recent_jobs if j.get("source_uri")
        }
    rows = []
    for ep in episodes_due:
        episode_id = ep["_id"]
        base = {
            "episode_id": episode_id, "season": ep["season"],
            "episode": ep["episode"], "scheduled": aware(ep["scheduled"]).isoformat(),
        }

        def skip(reason, **more):
            rows.append(dict(base, action="skip", reason=reason, **more))

        try:
            validate_nfo(ep)
            target, nfo = canonical_paths(ep, sports_root)
        except (ValueError, KeyError) as exc:
            skip("invalid_canonical_metadata", detail=str(exc))
            continue
        if os.path.lexists(target) or os.path.lexists(nfo):
            skip("canonical_destination_exists", destination=str(target))
            continue
        existing = records_by_ep.get(episode_id, [])
        published = local_published(existing, sports_root)
        if published:
            skip("already_published", path=published)
            continue
        # If a previous publication has been recorded as moved but the
        # destination is gone, do not silently redownload and hide data loss.
        if any(r.get("file_state") == "moved" for r in existing):
            skip("previous_publication_needs_reconciliation")
            continue
        choice = select([evaluate_file(r) for r in existing])
        if choice["preferred_path"]:
            path = pathlib.Path(choice["preferred_path"])
            if path.suffix.lower() != ".mkv":
                skip("preferred_file_not_mkv", path=str(path))
                continue
            rows.append(dict(base, action="file", source=str(path),
                             preferred_file_id=choice["preferred_file_id"],
                             destination=str(target)))
            continue
        # Exclude already downloaded source IDs regardless of ineligible or
        # stale metadata: never automatically repeat the same download.
        seen_releases = {r.get("release_uri") for r in existing if r.get("release_uri")}
        candidate_releases = [r for r in releases_by_ep.get(episode_id, [])
                              if r.get("_id") not in seen_releases]
        if not candidate_releases:
            skip("no_new_youtube_release")
            continue
        if len(candidate_releases) != 1:
            skip("multiple_unranked_youtube_releases",
                 release_uris=sorted(r["_id"] for r in candidate_releases))
            continue
        release = candidate_releases[0]
        uri = release["_id"]
        if not isinstance(uri, str) or not uri.startswith("youtube:"):
            skip("invalid_release_uri")
            continue
        video_id = uri.removeprefix("youtube:")
        if (len(video_id) != 11 or
                not all(c.isascii() and (c.isalnum() or c in "-_") for c in video_id)):
            skip("invalid_release_uri")
            continue
        duration = release.get("duration")
        if isinstance(duration, (float, int)) and duration < 3600:
            skip("known_short_release", source_uri=uri, duration_seconds=duration)
            continue
        if uri in recent_jobs_by_source:
            skip("recent_failed_or_running_download", source_uri=uri)
            continue
        incoming = incoming_root / INCOMING_NAME.format(id=video_id)
        if os.path.lexists(incoming):
            skip("incoming_destination_exists", source_uri=uri,
                 destination=str(incoming))
            continue
        if any(r.get("path") == str(incoming) for r in existing):
            skip("incoming_inventory_conflict", source_uri=uri)
            continue
        rows.append(dict(base, action="download_then_file",
                         source_uri=uri, incoming=str(incoming),
                         destination=str(target)))
    return rows, {"fixtures": len(episodes), "due": len(episodes_due)}
