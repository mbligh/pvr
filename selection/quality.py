"""Policy-based selection of already inventoried local media files.

The selector only reads filesystem and Atlas inventory data. It deliberately
does not download, move, delete, or update preferred-file state.
"""
import os
import pathlib
import stat

MIN_HEIGHT = 720
MIN_DURATION_SECONDS = 3600
PREFERRED_FPS = 50.0


def evaluate_file(record, *, min_duration_seconds=MIN_DURATION_SECONDS):
    """Classify one Atlas file against local presence and quality thresholds.

    An unchanged, regular local file and usable ffprobe metadata are required.
    Unknown FPS does not count as either known-high or known-low frame rate.
    """
    result = {
        "file_id": record.get("_id"),
        "path": record.get("path"),
        "release_uri": record.get("release_uri"),
        "eligible": False,
    }
    if record.get("file_state") == "moved":
        result["reason"] = "moved"
        return result
    path = record.get("path")
    if not isinstance(path, str) or not pathlib.Path(path).is_absolute():
        result["reason"] = "invalid_path"
        return result
    try:
        info = os.lstat(path)
    except OSError:
        result["reason"] = "not_found"
        return result
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        result["reason"] = "not_regular_video"
        return result
    if (record.get("size_bytes") != info.st_size or
            (record.get("mtime_ns") is not None and
             record["mtime_ns"] != info.st_mtime_ns)):
        result["reason"] = "inventory_changed"
        return result
    media = record.get("media") or {}
    video = media.get("video") or {}
    height = video.get("height")
    duration = media.get("duration_seconds")
    fps = video.get("fps")
    if (not isinstance(height, (float, int)) or isinstance(height, bool) or
            height < MIN_HEIGHT):
        result["reason"] = "below_720p" if isinstance(height, (float, int)) else "unknown_height"
        return result
    if (not isinstance(duration, (float, int)) or isinstance(duration, bool) or
            duration < min_duration_seconds):
        result["reason"] = (
            "under_one_hour" if min_duration_seconds == MIN_DURATION_SECONDS
            else "under_minimum_duration"
        ) if isinstance(duration, (float, int)) else "unknown_duration"
        return result
    if not isinstance(fps, (float, int)) or isinstance(fps, bool) or not 0 < fps < 1000:
        result["reason"] = "unknown_fps"
        return result
    result.update({
        "eligible": True,
        "size_bytes": info.st_size,
        "height": int(height),
        "duration_seconds": float(duration),
        "fps": float(fps),
        "frame_rate_tier": "50plus" if fps >= PREFERRED_FPS else "under50",
    })
    return result


def select(verified):
    """Select a deterministic winner from evaluated file records."""
    eligible = [item for item in verified if item["eligible"]]
    order = lambda x: (
        0 if x["frame_rate_tier"] == "50plus" else 1,
        -x["size_bytes"],
        str(x["file_id"]),  # stable tie-break
    )
    ranked = sorted(eligible, key=order)
    winner = ranked[0] if ranked else None
    return {
        "status": "selected" if winner else "no_eligible_file",
        "preferred_file_id": winner["file_id"] if winner else None,
        "preferred_path": winner["path"] if winner else None,
        "candidates": sorted(verified, key=lambda x: str(x["file_id"])),
        "eligible_count": len(eligible),
        "excluded_count": len(verified) - len(eligible),
        "policy": {
            "min_height": MIN_HEIGHT,
            "min_duration_seconds": MIN_DURATION_SECONDS,
            "preferred_fps": PREFERRED_FPS,
            "ranking": "largest_50plus_else_largest_other",
        },
    }


def select_episode(database, episode_id, hostname):
    records = list(database.files.find({
        "episode_id": episode_id,
        "host": hostname,
    }))
    return select([evaluate_file(item) for item in records])
