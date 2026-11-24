"""Read-only filesystem video inventory and conservative identity resolution.

This module does no Atlas writes. The CLI selects the scope and persists
verified observations when explicitly requested.
"""
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict

from matchers.pwr import SERIES_ID

EXTENSIONS = {".mkv", ".mp4", ".m4v", ".ts", ".mov", ".webm"}
SEASON_EPISODE = re.compile(r"(?i)(?<![a-z0-9])s(?P<season>(?:19|20)\d{2})e(?P<episode>\d{1,4})(?!\d)")
FOLDER_SEASON = re.compile(r"(?i)^Season_((?:19|20)\d{2})$")
SERIES_DIR = "rugby_womens_pwr"
SERIES_ALIASES = {"rugbywomenspwr", "premiershipwomensrugby"}


def series_hint(text):
    return re.sub(r"[^a-z0-9]", "", text.lower()) in SERIES_ALIASES


def is_pwr_path(path):
    return any(series_hint(p) for p in path.parts) or (
        series_hint(path.stem.split("-s20", 1)[0])
    )


def normalize_series(text):
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def series_identity(text):
    # Lazy import avoids the publish -> scan dependency cycle.
    from filing.publish import SERIES_PATHS
    normalized = normalize_series(text)
    if normalized in SERIES_ALIASES:
        return SERIES_ID
    for series, (showtitle, _) in SERIES_PATHS.items():
        if normalized in (normalize_series(series), showtitle):
            return series
    return None


def directory_series(path):
    return {series for part in pathlib.Path(path).parts
            if (series := series_identity(part))}


def parse_nfo(path):
    """Read only episode identity fields; reject malformed/contradictory NFOs."""
    nfo = path.with_suffix(".nfo")
    if nfo.is_symlink() or (os.path.lexists(nfo) and not nfo.is_file()):
        raise ValueError(f"Unsafe sidecar {nfo}")
    if not nfo.is_file():
        return None
    try:
        root = ET.parse(nfo).getroot()
        if root.tag != "episodedetails":
            raise ValueError("Not an episodedetails NFO")
        title = (root.findtext("showtitle") or "").strip()
        season = (root.findtext("season") or "").strip()
        episode = (root.findtext("episode") or "").strip()
        if season or episode:
            if not (season.isdecimal() and episode.isdecimal()):
                raise ValueError("Incomplete or invalid NFO season/episode")
            key = int(season), int(episode)
        else:
            key = None
        return {"key": key, "showtitle": title, "path": str(nfo)}
    except (ET.ParseError, OSError, ValueError) as exc:
        raise ValueError(f"Invalid sidecar {nfo}: {exc}") from exc


def ids_in_filename(name, linked_releases):
    """Match exact known source IDs, never a numeric guess or title search."""
    found = []
    for uri, episode_id in linked_releases.items():
        if uri == "__episodes__":
            continue
        source_id = uri.split(":", 1)[-1]
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(source_id) +
                     r"(?![A-Za-z0-9])", name):
            found.append((uri, episode_id))
    return found


def identify(path, by_number, linked_releases):
    """Identify a recording by series/season/episode, never its title."""
    try:
        nfo = parse_nfo(path)
    except ValueError as exc:
        return None, None, [], str(exc)
    matches = list(SEASON_EPISODE.finditer(path.stem))
    filename = [(int(m.group("season")), int(m.group("episode"))) for m in matches]
    if len(set(filename)) > 1:
        return None, None, [], "Multiple different season/episode identities in filename"
    filename_key = filename[0] if filename else None
    nfo_key = nfo["key"] if nfo else None
    folder_seasons = {int(m.group(1)) for part in path.parts[:-1]
                      if (m := FOLDER_SEASON.fullmatch(part))}
    if len(folder_seasons) > 1:
        return None, None, [], "Conflicting Season_XXXX directories"
    folder_season = next(iter(folder_seasons)) if folder_seasons else None
    if filename_key and nfo_key and filename_key != nfo_key:
        return None, None, [], "Filename and NFO season/episode disagree"
    key = filename_key or nfo_key
    if key and folder_season is not None and key[0] != folder_season:
        return None, None, [], "Season directory disagrees with filename/NFO"
    hints = directory_series(path.parent)
    # Only the prefix before the episode token can identify a series.
    if matches:
        prefix = path.stem[:matches[0].start()].rstrip("-_ .")
        if series := series_identity(prefix):
            hints.add(series)
    nfo_title = nfo["showtitle"] if nfo else ""
    if nfo_title:
        series = series_identity(nfo_title)
        if not series:
            if key or hints:
                return None, None, [], "Unrecognized NFO showtitle"
            return None, None, [], None
        hints.add(series)
    if len(hints) > 1:
        return None, None, [], "Series directory, filename and NFO disagree"
    series = next(iter(hints)) if hints else None
    evidence = []
    if key and series:
        found = by_number.get((series, *key))
        if not found:
            return None, None, [], "Series season/episode not found in Atlas or ambiguous"
        if filename_key:
            evidence.append("filename")
        if nfo_key:
            evidence.append("nfo")
        # The title suffix is arbitrary; do not mistake digits in a title
        # or date for an unrelated provider release ID.
        return found, None, evidence, None

    # Existing incoming workflow: a unique linked release can identify an
    # unnumbered filename, but never override a contradictory identity.
    source_hits = ids_in_filename(path.stem, linked_releases)
    episode_ids = {episode_id for _, episode_id in source_hits}
    if len(episode_ids) > 1:
        return None, None, [], "Filename identifies multiple different release episodes"
    linked_id = next(iter(episode_ids)) if episode_ids else None
    if linked_id and key and not series:
        return None, None, [], "Season/episode without series identity"
    found = linked_releases["__episodes__"].get(linked_id) if linked_id else None
    if not found:
        return None, None, [], None
    if series and found["series_id"] != series:
        return None, None, [], "Series directory disagrees with linked episode"
    if folder_season is not None and found["season"] != folder_season:
        return None, None, [], "Season directory disagrees with linked episode"
    uris = {u for u, ep_id in source_hits if ep_id == found["_id"]}
    return found, next(iter(uris)) if len(uris) == 1 else None, ["release_id"], None


def probe_video(path, *, binary="ffprobe", execute=subprocess.run, timeout=30):
    cmd = [binary, "-v", "error", "-show_entries",
           "format=duration,size,bit_rate,format_name:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate,duration,channels",
           "-of", "json", str(path)]
    try:
        result = execute(cmd, capture_output=True, text=True,
                         timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"ffprobe failed: {exc}") from exc
    if result.returncode:
        raise ValueError("ffprobe failed: " + result.stderr.strip()[:250])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe produced invalid JSON") from exc
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise ValueError("ffprobe streams is not an array")
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        raise ValueError("No video stream")
    primary = video[0]
    fmt = payload.get("format") or {}

    def positive_float(value):
        try:
            num = float(value)
            return num if num > 0 and num != float("inf") else None
        except (TypeError, ValueError):
            return None

    def frame_rate(value):
        try:
            a, b = str(value).split("/", 1)
            return round(float(a) / float(b), 3) if float(b) else None
        except (TypeError, ValueError):
            return positive_float(value)

    duration = positive_float(fmt.get("duration")) or positive_float(primary.get("duration"))
    if duration is None:
        raise ValueError("No positive video duration")
    return {
        "duration_seconds": round(duration, 3),
        "size_bytes": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
        "bitrate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "container": fmt.get("format_name"),
        "video": {
            "codec": primary.get("codec_name"),
            "width": primary.get("width"),
            "height": primary.get("height"),
            "fps": frame_rate(primary.get("avg_frame_rate")) or
                   frame_rate(primary.get("r_frame_rate")),
            "bitrate": int(primary["bit_rate"]) if
                       str(primary.get("bit_rate", "")).isdigit() else None,
        },
        "audio_tracks": len(audio),
        "audio_codecs": sorted({s.get("codec_name") for s in audio
                                if isinstance(s.get("codec_name"), str)}),
    }


def walk_videos(roots):
    """Walk once; do not follow symlinks or silently accept absent roots."""
    seen = set()
    for root in roots:
        root = pathlib.Path(root).expanduser()
        if not root.is_dir():
            raise ValueError(f"Scan root is not a readable directory: {root}")
        def onerror(error):
            raise ValueError(f"Cannot scan {error.filename}: {error}") from error
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            dirs[:] = sorted(d for d in dirs if not (pathlib.Path(folder) / d).is_symlink())
            for name in sorted(files):
                path = pathlib.Path(folder) / name
                if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                    continue
                absolute = str(path.absolute())
                if absolute not in seen:
                    seen.add(absolute)
                    yield path


def make_indexes(episodes, releases):
    by_number = defaultdict(list)
    by_id = {}
    from filing.publish import SERIES_PATHS
    for ep in episodes:
        series = ep.get("series_id")
        config = SERIES_PATHS.get(series)
        if (not config or type(ep.get("season")) is not int
                or ep["season"] < config[1]
                or ep.get("status") in ("cancelled", "canceled", "superseded")):
            continue
        by_id[ep["_id"]] = ep
        if type(ep.get("episode")) is int:
            by_number[(series, ep["season"], ep["episode"])].append(ep)
    # A duplicated season/episode number is unsafe to identify by filename.
    unique = {key: items[0] for key, items in by_number.items()
              if len(items) == 1}
    links = {r["_id"]: r["episode_id"] for r in releases
             if r.get("episode_id") in by_id}
    links["__episodes__"] = by_id
    return unique, links
