"""Read-only Formula 1 acquisition planning from Atlas and local inventory."""
import datetime as dt
import os
from pathlib import Path

from filing.publish import canonical_paths, validate_nfo
from orchestration.acquire import aware, host_name, local_published
from selection.f1_files import select_f1_episode
from selection.f1_releases import rank_releases

SERIES = "Formula_1"
SOURCE = "reddit-teletz"


def rows(database, *, year=2026, round_no=None, episode_number=None,
         sports_root="/data/sports", incoming_root="/data/incoming",
         hostname=None, min_age_hours=2, recent_hours=120, backfill=False,
         now=None):
    """Plan due sessions; no network checks, writes or directory creation."""
    now = aware(now) or dt.datetime.now(dt.timezone.utc)
    hostname = host_name(hostname)
    if min_age_hours < 0 or recent_hours <= 0:
        raise ValueError("Invalid age/window")
    sports, incoming = Path(sports_root), Path(incoming_root)
    if not sports.is_absolute() or not incoming.is_absolute():
        raise ValueError("Library and incoming roots must be absolute")
    if not sports.is_dir() or not incoming.is_dir():
        raise ValueError("Library and incoming roots must already exist")
    filters = {"series_id": SERIES, "season": year}
    if round_no is not None:
        filters["round"] = round_no
    if episode_number is not None:
        filters["episode"] = episode_number
    episodes = list(database.episodes.find(filters))
    episodes.sort(key=lambda ep: (
        aware(ep.get("scheduled_end")) or aware(ep.get("scheduled")) or
        dt.datetime.min.replace(tzinfo=dt.timezone.utc), str(ep.get("_id"))),
        reverse=True)
    cutoff = now - dt.timedelta(hours=min_age_hours)
    oldest = now - dt.timedelta(hours=recent_hours)
    due = []
    for episode in episodes:
        finished = aware(episode.get("scheduled_end"))
        if finished is None:
            continue  # Do not guess when an F1 session is finished.
        if (type(episode.get("episode")) is not int or
                episode.get("status") in ("cancelled", "canceled") or
                finished > cutoff or (not backfill and finished < oldest)):
            continue
        due.append(episode)
    ids = [ep["_id"] for ep in due]
    if not ids:
        return [], {"episodes": len(episodes), "due": 0}
    releases = list(database.releases.find({
        "episode_id": {"$in": ids}, "source": SOURCE,
    }))
    files = list(database.files.find({
        "episode_id": {"$in": ids}, "host": hostname,
    }))
    by_episode = {ep_id: [] for ep_id in ids}
    local = {ep_id: [] for ep_id in ids}
    for release in releases:
        if release.get("episode_id") in by_episode:
            by_episode[release["episode_id"]].append(release)
    for record in files:
        if record.get("episode_id") in local:
            local[record["episode_id"]].append(record)
    planned = []
    for ep in due:
        eid = ep["_id"]
        base = {"episode_id": eid, "season": ep["season"],
                "episode": ep["episode"], "round": ep.get("round"),
                "title": ep.get("title"),
                "scheduled_end": aware(ep["scheduled_end"]).isoformat()}

        def skip(reason, **kwargs):
            planned.append(dict(base, action="skip", reason=reason, **kwargs))

        try:
            validate_nfo(ep)
            target, nfo = canonical_paths(ep, sports)
        except (ValueError, KeyError) as exc:
            skip("invalid_canonical_metadata", detail=str(exc))
            continue
        if os.path.lexists(target) or os.path.lexists(nfo):
            skip("canonical_destination_exists", destination=str(target))
            continue
        existing = local[eid]
        published = local_published(existing, sports)
        if published:
            skip("already_published", path=published)
            continue
        if any(record.get("file_state") == "moved" for record in existing):
            skip("previous_publication_needs_reconciliation")
            continue
        if existing:
            choice = select_f1_episode(database, ep, hostname)
            if choice["preferred_path"]:
                path = Path(choice["preferred_path"])
                if path.suffix.lower() != ".mkv":
                    skip("preferred_file_not_mkv", path=str(path))
                else:
                    planned.append(dict(base, action="file", source=str(path),
                                        preferred_file_id=choice["preferred_file_id"],
                                        destination=str(target)))
                continue
            # Existing but unrecognized, stale, low-quality, or mismatched
            # inventory must be reconciled, not silently downloaded again.
            skip("local_inventory_needs_reconciliation",
                 file_ids=[r.get("_id") for r in existing])
            continue
        options = rank_releases(by_episode[eid], today=now.date())
        candidates = [r for r in options if r["selectable"]]
        if not candidates:
            skip("no_selectable_release", release_count=len(options))
            continue
        # Existing files at the destination of any candidate are not
        # overwritten, even if they were never indexed in Atlas.
        if any(os.path.lexists(incoming / ("pvr-" + c["uri"].split(":", 1)[1] + ".mkv"))
               for c in candidates):
            skip("incoming_destination_exists")
            continue
        planned.append(dict(base, action="download_then_file",
                            destination=str(target),
                            candidates=[{"source_uri": c["uri"], "url": c["url"],
                                         "commentary": c["commentary"],
                                         "height": c["height"],
                                         "preference": c["preference"],
                                         "incoming": str(incoming / (
                                             "pvr-" + c["uri"].split(":", 1)[1] + ".mkv"))}
                                        for c in candidates]))
    return planned, {"episodes": len(episodes), "due": len(due)}
