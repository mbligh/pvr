"""Plan missing numbered rugby recordings across supported providers.

All remote candidates for a due, unpublished episode are evaluated before
publication: without media download we cannot reliably compare FPS or file
size. The planner is read-only; the worker enforces explicit limits.
"""
import datetime as dt
import os
import pathlib
import re
import socket
import stat

from filing.publish import canonical_paths, validate_nfo
from orchestration.source_registry import RUGBY_SOURCES, RUGBY_SERIES, sources_for
from matchers.pwr import SERIES_ID
from matchers.rugbypass import SERIES as INTERNATIONAL_SERIES
from selection.quality import evaluate_file, select

SOURCES = RUGBY_SOURCES
SOURCE_ORDER = {name: i for i, name in enumerate(SOURCES)}
BLOCKED_LIVE_STATES = {"is_live", "is_upcoming", "post_live"}
ID_PATTERNS = {
    "bbc": re.compile(r"^[a-z][a-z0-9]{7}$"),
    "youtube": re.compile(r"^[A-Za-z0-9_-]{11}$"),
    "rugbypass": re.compile(r"^[0-9]{1,16}$"),
    "rugby24": re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
}


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


def validate_release(release):
    """Return a safe source ID, or None for a malformed source document."""
    uri = release.get("_id")
    source = release.get("source")
    if not isinstance(uri, str) or source not in ID_PATTERNS:
        return None
    prefix, colon, source_id = uri.partition(":")
    if (colon != ":" or prefix != source or len(source_id) > 128
            or not ID_PATTERNS[source].fullmatch(source_id)):
        return None
    return source_id


def incoming_path(root, release):
    source_id = validate_release(release)
    if source_id is None:
        raise ValueError("Invalid source URI")
    source = release["source"]
    # Retain the existing YouTube paths so previous operator downloads and
    # Atlas history are not silently abandoned.
    name = f"pvr-{source_id}.mkv" if source == "youtube" else f"pvr-{source}-{source_id}.mkv"
    return pathlib.Path(root) / name


def local_published(records, sports_root):
    """A physically present registered library file blocks unattended upgrades."""
    root = pathlib.Path(sports_root).absolute()
    for item in records:
        if item.get("file_state") == "moved":
            continue
        value = item.get("path")
        if not isinstance(value, str):
            continue
        path = pathlib.Path(value)
        if not path.is_absolute() or not path.is_relative_to(root):
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size:
            return str(path)
    return None


def planning_rows(database, *, sports_root="/data/sports",
                  incoming_root="/data/incoming", season=None,
                  round_no=None, episode_number=None,
                  min_age_hours=3, retry_after_hours=6, hostname=None,
                  max_candidates=3, sources=None, series_id=SERIES_ID,
                  now=None):
    """Return (rows, counts) for due rugby episodes; no writes/downloads.

    Rows may request 'file', 'download_then_file',
    'download_candidates_then_file', or 'skip'.
    """
    hostname = host_name(hostname)
    if series_id not in RUGBY_SERIES:
        raise ValueError("Unsupported rugby fixture series")
    sources = tuple(dict.fromkeys(sources if sources is not None else sources_for(series_id)))
    if not sources or any(source not in sources_for(series_id) for source in sources):
        raise ValueError("Source not supported for selected rugby series")
    sports_root = pathlib.Path(sports_root).expanduser()
    incoming_root = pathlib.Path(incoming_root).expanduser()
    if not sports_root.is_absolute() or not incoming_root.is_absolute():
        raise ValueError("Library and incoming roots must be absolute")
    if not sports_root.is_dir() or not incoming_root.is_dir():
        raise ValueError("Library and incoming roots must already exist")
    if min_age_hours < 0 or retry_after_hours < 0 or not 1 <= max_candidates <= 5:
        raise ValueError("Invalid age, retry delay or candidate limit")
    now = aware(now) or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=min_age_hours)
    query = {"series_id": series_id, "season": {"$gte": 2025}}
    if season is not None:
        if season < 2025:
            raise ValueError("Rugby season must be 2025 or later")
        query["season"] = season
    if round_no is not None:
        if not 1 <= round_no <= 99:
            raise ValueError("Rugby round must be between 1 and 99")
        query["round"] = round_no
    if episode_number is not None:
        if episode_number < 1:
            raise ValueError("Rugby episode must be positive")
        query["episode"] = episode_number
    episodes = list(database.episodes.find(query))
    episodes.sort(key=lambda ep: (
        aware(ep.get("scheduled")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        ep.get("_id", "")), reverse=season is None)
    due = [
        ep for ep in episodes
        if isinstance(ep.get("episode"), int)
        and (scheduled := aware(ep.get("scheduled"))) is not None
        and scheduled <= cutoff
        and ep.get("status") not in ("cancelled", "canceled", "superseded")
    ]
    ids = [ep["_id"] for ep in due]
    if not ids:
        return [], {"fixtures": len(episodes), "due": 0}
    releases = list(database.releases.find({
        "episode_id": {"$in": ids}, "source": {"$in": list(sources)},
    }))
    by_episode = {}
    for release in releases:
        if release_is_ready(release):
            by_episode.setdefault(release["episode_id"], []).append(release)
    records = list(database.files.find({"episode_id": {"$in": ids}, "host": hostname}))
    files_by_episode = {}
    for record in records:
        files_by_episode.setdefault(record["episode_id"], []).append(record)
    recent_jobs = set()
    if retry_after_hours:
        jobs = database.jobs.find({
            "type": "download", "host": hostname,
            "status": {"$in": ["failed", "running"]},
            "started_at": {"$gte": now - dt.timedelta(hours=retry_after_hours)},
        })
        recent_jobs = {j.get("source_uri") for j in jobs if j.get("source_uri")}

    rows = []
    for episode in due:
        eid = episode["_id"]
        base = {
            "episode_id": eid, "season": episode["season"],
            "episode": episode["episode"],
            "scheduled": aware(episode["scheduled"]).isoformat(),
        }

        def skip(reason, **extra):
            rows.append(dict(base, action="skip", reason=reason, **extra))

        try:
            validate_nfo(episode)
            destination, nfo = canonical_paths(episode, sports_root)
        except (ValueError, KeyError) as exc:
            skip("invalid_canonical_metadata", detail=str(exc))
            continue
        if os.path.lexists(destination) or os.path.lexists(nfo):
            skip("canonical_destination_exists", destination=str(destination))
            continue
        existing = files_by_episode.get(eid, [])
        published = local_published(existing, sports_root)
        if published:
            skip("already_published", path=published)
            continue
        if any(record.get("file_state") == "moved" for record in existing):
            skip("previous_publication_needs_reconciliation")
            continue
        choice = select([evaluate_file(record) for record in existing])
        preferred = choice["preferred_path"]
        if preferred and pathlib.Path(preferred).suffix.lower() != ".mkv":
            skip("preferred_file_not_mkv", path=preferred)
            continue

        seen = {r.get("release_uri") for r in existing if r.get("release_uri")}
        remote = []
        short_releases = []
        invalid = False
        for release in by_episode.get(eid, []):
            uri = release.get("_id")
            if validate_release(release) is None:
                invalid = True
                continue
            if (isinstance(release.get("duration"), (float, int)) and
                    release["duration"] < 3600):
                short_releases.append(uri)
                continue
            if (isinstance((release.get("metadata") or {}).get("duration"), (float, int)) and
                    release["metadata"]["duration"] < 3600):
                short_releases.append(uri)
                continue
            if uri not in seen:
                remote.append(release)
        # Invalid metadata must not lead to an unverified partial comparison.
        if invalid:
            skip("invalid_release_uri")
            continue
        remote.sort(key=lambda r: (SOURCE_ORDER[r["source"]], r["_id"]))
        if remote and len(remote) + (1 if preferred else 0) > max_candidates:
            skip("too_many_source_candidates", sources=[r["_id"] for r in remote],
                 existing_preferred=preferred)
            continue
        if any(r["_id"] in recent_jobs for r in remote):
            skip("recent_failed_or_running_download",
                 sources=[r["_id"] for r in remote if r["_id"] in recent_jobs])
            continue
        occupied = []
        inventoried = []
        for release in remote:
            incoming = incoming_path(incoming_root, release)
            if os.path.lexists(incoming):
                occupied.append(str(incoming))
            elif any(r.get("path") == str(incoming) for r in existing):
                inventoried.append(str(incoming))
        if occupied:
            skip("incoming_destination_exists", paths=occupied)
            continue
        if inventoried:
            skip("incoming_inventory_conflict", paths=inventoried)
            continue

        if not remote:
            if preferred:
                rows.append(dict(base, action="file", source=preferred,
                                 preferred_file_id=choice["preferred_file_id"],
                                 destination=str(destination)))
            elif short_releases:
                skip("known_short_release", source_uris=sorted(short_releases))
            else:
                skip("no_new_supported_release")
            continue
        downloads = [{
            "source_uri": release["_id"],
            "incoming": str(incoming_path(incoming_root, release)),
        } for release in remote]
        if len(downloads) == 1 and not preferred:
            rows.append(dict(base, action="download_then_file",
                             **downloads[0], destination=str(destination)))
        else:
            rows.append(dict(base, action="download_candidates_then_file",
                             downloads=downloads,
                             existing_preferred=preferred,
                             destination=str(destination)))
    return rows, {"fixtures": len(episodes), "due": len(due)}
