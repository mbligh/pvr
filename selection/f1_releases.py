"""Read-only F1 release preference ranking.

Rank declared commentary before claimed resolution: dedicated Sky, MULTi
containing English Sky, F1TV, then other. A link is only a candidate; this
module NEVER verifies TransferNow availability, downloads, or alters state.
"""
import datetime as dt
import re

SERIES_ID = "formula-1"
SOURCE = "reddit-teletz"
URL = re.compile(r"^https://www\.transfernow\.net/dl/[A-Za-z0-9]{8,64}$")
TIERS = (
    "Dedicated Sky commentary",
    "MULTi with English Sky audio",
    "F1TV commentary",
    "Other or unclassified commentary",
)


def classification(metadata):
    commentary = str(metadata.get("commentary") or "unknown").lower()
    if commentary == "sky":
        return 0
    if commentary == "multi" and metadata.get("contains_sky_audio") is True:
        return 1
    if commentary == "f1tv":
        return 2
    return 3


def resolution(metadata):
    height = metadata.get("height")
    return height if type(height) is int and height > 0 else None


def publication_date(record):
    value = record.get("published")
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            pass
    return None


def make_candidate(release, today):
    meta = release.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    uri = str(release.get("uri") or release.get("_id") or "")
    height = resolution(meta)
    tier = classification(meta)
    url = meta.get("url")
    date = publication_date(release)
    days_old = (today - date).days if date else None
    if release.get("available") is False:
        blocked = "Marked unavailable in MongoDB"
    elif not isinstance(url, str) or not URL.fullmatch(url):
        blocked = "Missing or invalid TransferNow link"
    else:
        blocked = None
    # Selection is deliberately independent of the indexer's advisory
    # preference_rank; recalculating permits reviewing erroneous tags.
    return {
        "uri": uri,
        "url": url if isinstance(url, str) else None,
        "commentary": meta.get("commentary") or "unknown",
        "sky_audio": meta.get("contains_sky_audio") is True,
        "height": height,
        "audio_variant": meta.get("audio_variant"),
        "uploader": meta.get("uploader"),
        "delivery": meta.get("delivery"),
        "published": date.isoformat() if date else None,
        "age_days": days_old,
        "source_comment": meta.get("reddit_url"),
        "preference": tier,
        "preference_reason": TIERS[tier],
        "selectable": blocked is None,
        "warning": blocked or (
            "Link not checked; posted more than five days ago"
            if days_old is not None and days_old > 5 else
            "Link not checked"),
        "selected": False,
    }


def rank_releases(releases, *, today=None):
    today = today or dt.datetime.now(dt.timezone.utc).date()
    candidates = [make_candidate(r, today) for r in releases]
    if len({c["uri"] for c in candidates}) != len(candidates):
        raise ValueError("Duplicate release URI found in Atlas")
    # Viable first; commentary class before resolution; latest publication
    # date as tie-breaker; stable URI for deterministic ordering.
    candidates.sort(key=lambda c: (
        not c["selectable"],
        c["preference"],
        -(c["height"] or 0),
        -dt.date.fromisoformat(c["published"]).toordinal() if c["published"] else 0,
        c["uri"],
    ))
    selected = next((c for c in candidates if c["selectable"]), None)
    if selected:
        selected["selected"] = True
    return candidates


def summarize(database, *, year, round_no=None, episode_number=None,
              include_missing=False, today=None):
    """Group Reddit releases under real F1 episodes; never write to Atlas."""
    query = {"series_id": SERIES_ID, "season": year}
    if round_no is not None:
        query["round"] = round_no
    if episode_number is not None:
        query["episode"] = episode_number
    episodes = list(database.episodes.find(
        query, {"_id": 1, "series_id": 1, "season": 1, "episode": 1,
                "round": 1, "title": 1, "scheduled": 1, "session_code": 1}
    ))
    episodes.sort(key=lambda e: (e.get("episode") if isinstance(e.get("episode"), int)
                                 else 10000, str(e.get("_id"))))
    if (round_no is not None or episode_number is not None) and not episodes:
        raise ValueError("No F1 episodes match the requested filters")
    ids = [ep["_id"] for ep in episodes]
    found = list(database.releases.find({
        "episode_id": {"$in": ids}, "source": SOURCE,
    })) if ids else []
    releases = {eid: [] for eid in ids}
    for release in found:
        episode_id = release.get("episode_id")
        if episode_id in releases:
            releases[episode_id].append(release)
    groups = []
    total_with = total_options = total_selectable = blocked = 0
    for episode in episodes:
        candidates = rank_releases(releases[episode["_id"]], today=today)
        if candidates:
            total_with += 1
            total_options += len(candidates)
            total_selectable += int(any(c["selected"] for c in candidates))
            blocked += sum(not c["selectable"] for c in candidates)
        if candidates or include_missing:
            groups.append({
                "episode_id": episode["_id"],
                "season": episode.get("season"),
                "episode": episode.get("episode"),
                "round": episode.get("round"),
                "title": episode.get("title"),
                "selected_uri": next((c["uri"] for c in candidates
                                      if c["selected"]), None),
                "candidates": candidates,
            })
    return {
        "mode": "preview",
        "series_id": SERIES_ID,
        "season": year,
        "filters": {"round": round_no, "episode": episode_number},
        "summary": {
            "episodes": len(episodes),
            "episodes_with_releases": total_with,
            "episodes_without_releases": len(episodes) - total_with,
            "episodes_with_selection": total_selectable,
            "release_options": total_options,
            "excluded_options": blocked,
        },
        "episodes": groups,
    }
