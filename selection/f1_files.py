"""Read-only F1 preference among already-downloaded Atlas media records.

Uses the same verified file checks as PWR, but allows genuine short F1
sessions and puts known Sky commentary ahead of other releases. It does
not check source URLs, download, mutate the database, or alter files.
"""
from selection.f1_releases import classification
from selection.quality import evaluate_file, PREFERRED_FPS

MIN_DURATION_SECONDS = 900


def minimum_duration(episode):
    """Require at least half of the officially scheduled session duration.

    F1 sessions can be shorter than the PWR one-hour threshold. This is a
    loose sanity check, not a guarantee the actual replay is complete.
    """
    expected = episode.get("duration")
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and expected > 0:
        return max(MIN_DURATION_SECONDS, int(expected * 0.5))
    return MIN_DURATION_SECONDS


def select_f1_episode(database, episode, hostname):
    if episode.get("series_id") != "Formula_1":
        raise ValueError("F1 file selection requires a Formula 1 episode")
    episode_id = episode["_id"]
    records = list(database.files.find({"episode_id": episode_id, "host": hostname}))
    uris = {r.get("release_uri") for r in records if isinstance(r.get("release_uri"), str)}
    releases = list(database.releases.find(
        {"_id": {"$in": sorted(uris)}, "episode_id": episode_id},
        {"_id": 1, "source": 1, "metadata": 1})) if uris else []
    by_uri = {r["_id"]: r for r in releases}
    threshold = minimum_duration(episode)
    checked = []
    for record in records:
        choice = evaluate_file(record, min_duration_seconds=threshold)
        release = by_uri.get(record.get("release_uri"))
        if (release and release.get("source") == "reddit-teletz" and
                isinstance(release.get("metadata"), dict)):
            rank = classification(release["metadata"])
            kind = release["metadata"].get("commentary") or "unknown"
        else:
            rank, kind = 4, "unknown"
        choice.update(preference=rank, commentary=kind)
        checked.append(choice)
    eligible = [x for x in checked if x["eligible"]]
    ranked = sorted(eligible, key=lambda c: (
        c["preference"], 0 if c["fps"] >= PREFERRED_FPS else 1,
        -c["height"], -c["size_bytes"], str(c["file_id"])))
    winner = ranked[0] if ranked else None
    return {
        "status": "selected" if winner else "no_eligible_file",
        "preferred_file_id": winner["file_id"] if winner else None,
        "preferred_path": winner["path"] if winner else None,
        "candidates": sorted(checked, key=lambda c: str(c["file_id"])),
        "eligible_count": len(eligible),
        "excluded_count": len(checked) - len(eligible),
        "policy": {
            "min_height": 720,
            "min_duration_seconds": threshold,
            "preferred_fps": PREFERRED_FPS,
            "ranking": "Sky_first_then_fps_resolution_size",
        },
    }
