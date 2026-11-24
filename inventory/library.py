"""Incremental, non-destructive inventory of canonical sports series."""
import collections
import datetime as dt
import json
import os
import pathlib
import socket
import sys

from inventory.scan import identify, make_indexes, probe_video, walk_videos
from inventory.scan import directory_series, parse_nfo, EXTENSIONS, series_identity
from filing.publish import (SERIES_PATHS, validate_nfo, canonical_paths,
                            _write_exclusive, stat_identity)
from orchestration.locks import episode_lock, LockBusy

DEFAULT_ROOT = "/data/sports"


def scan(database, *, roots=None, apply=False, probe=True, ffprobe="ffprobe",
         hostname=None, verbose=False, show_unseen=False, stream=sys.stdout,
         summary_stream=sys.stderr, sports_root=DEFAULT_ROOT, series_ids=None,
         prober=probe_video, quiet=False, lock_root=None):
    selected = set(series_ids if series_ids is not None else SERIES_PATHS)
    if not selected or not selected <= SERIES_PATHS.keys():
        raise ValueError("Unsupported inventory series")
    if roots is None:
        library = pathlib.Path(sports_root).expanduser()
        if not library.is_absolute() or not library.is_dir():
            raise ValueError("Sports root must be an existing absolute directory")
        roots = [library / name for name in sorted(selected)
                 if (library / name).is_dir() and not (library / name).is_symlink()]
    else:
        roots = list(roots)
        if not roots:
            raise ValueError("At least one scan root is required")
        hints = set().union(*(directory_series(pathlib.Path(root)) for root in roots))
        # Mixed or incoming roots may contain recordings from any series.
        if hints and all(directory_series(pathlib.Path(root)) for root in roots):
            selected.intersection_update(hints)
    if apply and not probe:
        raise ValueError("--apply requires ffprobe verification")
    hostname = hostname or socket.gethostname()
    if not hostname or ":" in hostname or "/" in hostname:
        raise ValueError("Invalid hostname for file identity")

    episodes = list(database.episodes.find(
        {"series_id": {"$in": sorted(selected)}},
        {"_id": 1, "series_id": 1, "season": 1, "episode": 1,
         "scheduled": 1, "status": 1, "nfo_path": 1, "nfo": 1}
    )) if roots else []
    episodes = [ep for ep in episodes
                if ep.get("series_id") in selected
                and type(ep.get("season")) is int
                and ep["season"] >= SERIES_PATHS[ep["series_id"]][1]
                and ep.get("status") not in ("cancelled", "canceled", "superseded")]
    ids = [ep["_id"] for ep in episodes]
    releases = list(database.releases.find(
        {"episode_id": {"$in": ids}},
        {"_id": 1, "episode_id": 1}
    )) if ids else []
    previous = {row["_id"]: row for row in database.files.find(
        {"host": hostname, "episode_id": {"$in": ids}})} if ids else {}
    by_number, linked = make_indexes(episodes, releases)
    counts = collections.Counter()
    rows = []
    now = dt.datetime.now(dt.timezone.utc)
    files_per_episode = collections.Counter()
    observed_episodes = set()
    sidecars = []
    renames = []

    for path in walk_videos(roots):
        counts["scanned"] += 1
        episode, release_uri, evidence, reason = identify(path, by_number, linked)
        item = {"path": str(path.absolute())}
        if reason:
            item.update(status="conflict", reason=reason)
        elif episode is None:
            item["status"] = "unmatched"
        else:
            item.update(status="present" if probe else "unprobed",
                        episode_id=episode["_id"], series_id=episode["series_id"], season=episode["season"],
                        episode=episode["episode"], evidence=evidence)
            if release_uri:
                item["release_uri"] = release_uri
            try:
                before = path.stat()
                snapshot = stat_identity(path)
                file_id = f"{hostname}:{path.absolute()}"
                old = previous.get(file_id) or {}
                if old and (old.get("episode_id") != episode["_id"]
                            or old.get("file_state") == "moved"):
                    raise ValueError("Existing inventory association conflicts")
                cached = (old.get("size_bytes") == before.st_size
                          and old.get("mtime_ns") == before.st_mtime_ns
                          and isinstance(old.get("media"), dict)
                          and bool(old["media"].get("video")))
                if probe:
                    media = old["media"] if cached else prober(path, binary=ffprobe)
                    if stat_identity(path) != snapshot:
                        raise ValueError("Media file changed while probing")
                    item["media"] = media
                nfo_path = path.with_suffix(".nfo")
                nfo_bytes = None
                rename_target = None
                # Only installed recordings in a recognized series directory
                # get a sidecar. Incoming files are still inventoried normally.
                if episode["series_id"] in directory_series(path.parent):
                    if not os.path.lexists(nfo_path):
                        try:
                            nfo_bytes = validate_nfo(episode)
                        except ValueError as exc:
                            item["nfo_error"] = str(exc)
                        else:
                            item["nfo"] = "missing"
                    else:
                        item["nfo"] = "present"
                    try:
                        # Use the Atlas filename but preserve the actual container.
                        # Only rename within the established season directory.
                        canonical, canonical_nfo = canonical_paths(episode, sports_root)
                        target = canonical.with_suffix(path.suffix)
                        canonical_xml = validate_nfo(episode)
                        if path.absolute().parent == target.parent and path.absolute() != target:
                            occupied = any(os.path.lexists(canonical.with_suffix(ext))
                                           for ext in EXTENSIONS | {path.suffix})
                            if occupied or any(p.stem == canonical.stem and p.suffix.lower() in EXTENSIONS
                                               for p in target.parent.iterdir()) or os.path.lexists(canonical_nfo):
                                item["rename"] = "destination_exists"
                            else:
                                rename_target = target
                                item["rename"] = "planned"
                                item["destination"] = str(target)
                    except (ValueError, KeyError):
                        # Legacy episodes may lack valid canonical metadata.
                        pass
                item["bytes"] = before.st_size
                item["mtime_ns"] = before.st_mtime_ns
                if not probe:
                    observed_episodes.add(episode["_id"])
                if probe:
                    data = {
                        "_id": file_id,
                        "host": hostname,
                        "path": str(path.absolute()),
                        "episode_id": episode["_id"],
                        "series_id": episode["series_id"],
                        "season": episode["season"],
                        "episode": episode["episode"],
                        "match_evidence": list(dict.fromkeys((old.get("match_evidence") or []) + evidence)),
                        "release_uri": release_uri or old.get("release_uri"),
                        "size_bytes": before.st_size,
                        "mtime_ns": before.st_mtime_ns,
                        "media": media,
                        "last_seen": now,
                    }
                    if item.get("nfo") == "present" or nfo_bytes is not None:
                        data["nfo_path"] = str(nfo_path.absolute())
                    if not cached or any(old.get(k) != v for k, v in data.items()
                                         if k != "last_seen"):
                        rows.append(data)
                    else:
                        counts["cached"] += 1
                    if apply and rename_target is not None:
                        renames.append((path.absolute(), snapshot, rename_target, data,
                                        canonical_xml, episode, nfo_bytes is not None))
                    elif apply and nfo_bytes is not None:
                        sidecars.append((path, snapshot, nfo_path, nfo_bytes, episode))
                    files_per_episode[episode["_id"]] += 1
                    observed_episodes.add(episode["_id"])
            except (OSError, ValueError) as exc:
                item["status"] = "probe_error"
                item["reason"] = str(exc)
                item.pop("media", None)
        counts[item["status"]] += 1
        if verbose or (item["status"] != "unmatched" and
                       (not quiet or item["status"] != "present" or not cached
                        or item.get("nfo") == "missing" or item.get("nfo_error")
                        or item.get("rename"))):
            print(json.dumps(item, ensure_ascii=False), file=stream)

    # Absence is scoped to the supplied roots and scan time; it is not a
    # claim that the fixture's recording does not exist somewhere else.
    unseen = []
    for episode in episodes:
        scheduled = episode.get("scheduled")
        if not isinstance(scheduled, dt.datetime):
            continue
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=dt.timezone.utc)
        if scheduled + dt.timedelta(hours=3) > now:
            continue
        if episode["_id"] not in observed_episodes:
            unseen.append(episode)
    if show_unseen:
        for episode in sorted(unseen, key=lambda e: (
                e["season"], e.get("episode") or -1, e["_id"])):
            print(json.dumps({
                "status": "not_found_in_roots",
                "episode_id": episode["_id"],
                "season": episode["season"],
                "episode": episode.get("episode"),
                "scheduled": episode["scheduled"].isoformat(),
            }), file=stream)

    # All filesystem observations are complete before any Atlas updates.
    if apply and rows:
        persist(database, rows, now=now)
    for path, snapshot, target, data, content, episode, missing_nfo in renames:
        lock_dir = pathlib.Path(lock_root) if lock_root else (
            pathlib.Path("/data/incoming") if pathlib.Path("/data/incoming").is_dir()
            else pathlib.Path(sports_root))
        try:
            with episode_lock(lock_dir, episode["_id"]):
                result = rename_recording(database, path, snapshot, target, data,
                                          content, episode)
        except LockBusy:
            result = "episode_busy"
        counts["renamed" if result == "renamed" else "rename_skipped"] += 1
        if result == "renamed":
            if missing_nfo:
                counts["nfo_created"] += 1
        elif result == "destination_exists" and not os.path.lexists(path.with_suffix(".nfo")):
            sidecars.append((path, snapshot, path.with_suffix(".nfo"), content, episode))
        print(json.dumps({"type": "inventory_rename", "status": result,
                          "source": str(path), "destination": str(target)}, sort_keys=True),
              file=stream)
    for path, snapshot, nfo_path, content, episode in sidecars:
        if stat_identity(path) != snapshot:
            raise ValueError(f"Recording changed before NFO generation: {path}")
        try:
            _write_exclusive(nfo_path, content)
            counts["nfo_created"] += 1
        except FileExistsError:
            # A concurrent run may have installed the same sidecar.
            actual = parse_nfo(path)
            from inventory.scan import series_identity
            if (not actual or actual["key"] != (episode["season"], episode["episode"])
                    or series_identity(actual["showtitle"]) != episode["series_id"]):
                raise ValueError(f"Conflicting NFO appeared during inventory: {nfo_path}")
    report = {
        "mode": "apply" if apply else "preview",
        "series": next(iter(selected)) if len(selected) == 1 else sorted(selected),
        "roots": [str(r) for r in roots],
        "fixtures": len(episodes), "scanned": counts["scanned"],
        "present": counts["present"], "unprobed": counts["unprobed"],
        "unmatched": counts["unmatched"], "conflict": counts["conflict"],
        "probe_error": counts["probe_error"], "cached": counts["cached"],
        "nfo_created": counts["nfo_created"], "renamed": counts["renamed"],
        "rename_skipped": counts["rename_skipped"],
        "episodes_with_files": len(files_per_episode),
        "past_episodes_not_found_in_roots": len(unseen),
        "written": len(rows) if apply else 0,
    }
    print(json.dumps(report, sort_keys=True), file=summary_stream)
    return report


def rename_recording(database, source, snapshot, target, data, nfo_xml, episode):
    """Publish new names without clobbering; retire old paths only after Atlas.

    A database failure leaves both copies for operator reconciliation rather
    than losing the original. The same-directory hardlink avoids copying video.
    """
    source_nfo, target_nfo = source.with_suffix(".nfo"), target.with_suffix(".nfo")
    if source.parent != target.parent or source.parent.is_symlink():
        raise ValueError("Unsafe inventory rename directory")
    if stat_identity(source) != snapshot:
        raise ValueError("Recording changed before canonical rename")
    if (os.path.lexists(target_nfo) or any(
            os.path.lexists(target.with_suffix(ext)) for ext in EXTENSIONS | {target.suffix})
            or any(p.stem == target.stem and p.suffix.lower() in EXTENSIONS
                   for p in target.parent.iterdir())):
        return "destination_exists"
    dest_id = f"{data['host']}:{target}"
    existing = database.files.find_one({"_id": dest_id})
    if existing is not None:
        return "destination_exists"
    source_nfo_snapshot = None
    if os.path.lexists(source_nfo):
        identity = parse_nfo(source)
        if not identity:
            raise ValueError("Existing NFO cannot be read")
        if (identity["key"] is not None and
                identity["key"] != (episode["season"], episode["episode"])):
            raise ValueError("Existing NFO episode disagrees")
        if identity["showtitle"] and series_identity(identity["showtitle"]) != episode["series_id"]:
            raise ValueError("Existing NFO series disagrees")
        source_nfo_snapshot = stat_identity(source_nfo)

    created_video = created_nfo = False
    try:
        os.link(source, target, follow_symlinks=False)
        created_video = True
        if source_nfo_snapshot is not None:
            os.link(source_nfo, target_nfo, follow_symlinks=False)
        else:
            _write_exclusive(target_nfo, nfo_xml)
        created_nfo = True
        if stat_identity(source) != snapshot or stat_identity(target) != snapshot:
            raise ValueError("Recording changed during canonical rename")
        if source_nfo_snapshot is not None and (
                stat_identity(source_nfo) != source_nfo_snapshot or
                stat_identity(target_nfo) != source_nfo_snapshot):
            raise ValueError("NFO changed during canonical rename")
    except BaseException:
        if created_nfo:
            target_nfo.unlink(missing_ok=True)
        if created_video:
            target.unlink(missing_ok=True)
        raise

    # Once Atlas writes start, retain both originals on any uncertain failure.
    now = dt.datetime.now(dt.timezone.utc)
    # Retain provenance and any other metadata from the current source row.
    old = database.files.find_one({"_id": data["_id"]}) or data
    destination = dict(old, **data)
    for field in ("file_state", "moved_to", "moved_at"):
        destination.pop(field, None)
    destination.update(_id=dest_id, path=str(target), nfo_path=str(target_nfo),
                       previous_file_id=data["_id"], last_seen=now)
    database.files.insert_one(destination)
    changed = database.files.update_one(
        {"_id": data["_id"], "episode_id": episode["_id"],
         "path": str(source), "file_state": {"$ne": "moved"}},
        {"$set": {"file_state": "moved", "moved_to": dest_id, "moved_at": now}})
    if changed.matched_count != 1:
        raise ValueError("Inventory changed during rename; retained both copies")
    if stat_identity(source) != snapshot:
        raise ValueError("Original recording changed; retained both copies")
    if source_nfo_snapshot is not None:
        if stat_identity(source_nfo) != source_nfo_snapshot:
            raise ValueError("Original NFO changed; retained both copies")
        source_nfo.unlink()
    source.unlink()
    return "renamed"


def persist(database, rows, *, now=None):
    """Idempotent, non-destructive upsert; refuse to reassign existing paths."""
    from pymongo import UpdateOne

    now = now or dt.datetime.now(dt.timezone.utc)
    ids = [r["_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate local file identities")
    previous = list(database.files.find({"_id": {"$in": ids}},
                                        {"_id": 1, "episode_id": 1, "file_state": 1}))
    expected = {r["_id"]: r["episode_id"] for r in rows}
    conflicts = [r["_id"] for r in previous if r.get("episode_id") not in
                 (None, expected[r["_id"]]) or r.get("file_state") == "moved"]
    if conflicts:
        raise ValueError("Existing file associations conflict: " +
                         ", ".join(conflicts[:5]))

    database.files.create_index([("host", 1), ("path", 1)], unique=True)
    database.files.create_index([("episode_id", 1)])
    # A changed association between preflight and write triggers a duplicate
    # key error instead of silently replacing somebody else's episode link.
    operations = [
        UpdateOne(
            {"_id": row["_id"], "episode_id": {"$in": [None, row["episode_id"]]},
             "file_state": {"$ne": "moved"}},
            {"$set": {k: v for k, v in row.items() if k != "_id"},
             "$setOnInsert": {"first_seen": now}},
            upsert=True,
        )
        for row in rows
    ]
    if operations:
        database.files.bulk_write(operations, ordered=True)
    return len(operations)

