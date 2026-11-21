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
SEASON_EPISODE = re.compile(r"(?i)(?<![a-z0-9])s(?P<season>20\d{2})e(?P<episode>\d{1,4})(?!\d)")
FOLDER_SEASON = re.compile(r"(?i)^Season_(20\d{2})$")
SERIES_DIR = "rugby_womens_pwr"
SERIES_ALIASES = {"rugbywomenspwr", "premiershipwomensrugby"}


def series_hint(text):
    return re.sub(r"[^a-z0-9]", "", text.lower()) in SERIES_ALIASES


def is_pwr_path(path):
    return any(series_hint(p) for p in path.parts) or (
        series_hint(path.stem.split("-s20", 1)[0])
    )


def parse_nfo(path):
    """Read only episode identity fields; reject malformed/contradictory NFOs."""
    nfo = path.with_suffix(".nfo")
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
        source_id = uri.split(":", 1)[-1]
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(source_id) +
                     r"(?![A-Za-z0-9])", name):
            found.append((uri, episode_id))
    return found


def identify(path, by_number, linked_releases):
    """Return (episode, source_uri, evidence, error).

    Only matching authoritative identities are accepted. A contradictory
    filename, NFO, folder, or release identity is an explicit conflict.
    """
    try:
        nfo = parse_nfo(path)
    except ValueError as exc:
        return None, None, [], str(exc)
    filename = [(int(m.group("season")), int(m.group("episode")))
                for m in SEASON_EPISODE.finditer(path.stem)]
    if len(set(filename)) > 1:
        return None, None, [], "Multiple different season/episode identities in filename"
    filename_key = filename[0] if filename else None
    nfo_key = nfo["key"] if nfo else None
    folder_seasons = {int(m.group(1)) for part in path.parts[:-1]
                      if (m := FOLDER_SEASON.fullmatch(part))}
    if len(folder_seasons) > 1:
        return None, None, [], "Conflicting Season_XXXX directories"
    folder_season = next(iter(folder_seasons)) if folder_seasons else None
    evidence = []
    if filename_key and nfo_key and filename_key != nfo_key:
        return None, None, [], "Filename and NFO season/episode disagree"
    key = filename_key or nfo_key
    if key and folder_season is not None and key[0] != folder_season:
        return None, None, [], "Season directory disagrees with filename/NFO"
    nfo_title = nfo["showtitle"] if nfo else ""
    if nfo_title and not series_hint(nfo_title):
        if key or is_pwr_path(path):
            return None, None, [], "NFO showtitle is not Rugby Womens PWR"
        return None, None, [], None  # Out-of-scope file.
    hinted = is_pwr_path(path) or (bool(nfo_title) and series_hint(nfo_title))
    # A bare S2025E101 in a mixed incoming directory is not a PWR identity.
    by_key = by_number.get(key) if key and hinted else None
    if key and hinted:
        if not by_key:
            return None, None, [], "PWR season/episode not found in Atlas"
        if filename_key:
            evidence.append("filename")
        if nfo_key:
            evidence.append("nfo")
    elif key and not hinted:
        # Still permit a unique linked source ID below, but not a bare number.
        pass

    source_hits = ids_in_filename(path.stem, linked_releases)
    episode_ids = {episode_id for _, episode_id in source_hits}
    if len(episode_ids) > 1:
        return None, None, [], "Filename identifies multiple different release episodes"
    linked_id = next(iter(episode_ids)) if episode_ids else None
    if by_key and linked_id and by_key["_id"] != linked_id:
        return None, None, [], "Release ID and season/episode point to different fixtures"
    if linked_id and key and not hinted:
        # If a title contains an episode number without identifying its series,
        # accept only when its number agrees with the linked release.
        # This guards against unrelated similarly-numbered programmes.
        return None, None, [], "Season/episode without series identity"
    if linked_id:
        evidence.append("release_id")
    found = by_key or (linked_releases["__episodes__"].get(linked_id)
                       if linked_id else None)
    if not found:
        return None, None, [], None
    if found.get("series_id") != SERIES_ID or found.get("season", 0) < 2025:
        return None, None, [], "Episode is outside supported PWR seasons"
    uri = None
    if source_hits:
        # More than one release of the same match might be named in the file;
        # keep an episode match but no unambiguous release provenance.
        uris = {u for u, ep_id in source_hits if ep_id == found["_id"]}
        uri = next(iter(uris)) if len(uris) == 1 else None
    return found, uri, evidence, None


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
    for ep in episodes:
        if ep.get("series_id") != SERIES_ID or ep.get("season", 0) < 2025:
            continue
        by_id[ep["_id"]] = ep
        if isinstance(ep.get("episode"), int):
            by_number[(ep["season"], ep["episode"])].append(ep)
    # A duplicated season/episode number is unsafe to identify by filename.
    unique = {key: items[0] for key, items in by_number.items()
              if len(items) == 1}
    links = {r["_id"]: r["episode_id"] for r in releases
             if r.get("episode_id") in by_id}
    links["__episodes__"] = by_id
    return unique, links
