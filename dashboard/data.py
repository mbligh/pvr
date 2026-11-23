"""Read-only Atlas episode listing with verified local installed-media state.

Never infer a match from similar titles or episode numbering. Prefer the
canonical nfo_path and otherwise require an explicit Atlas inventory link.
Missing/unnumbered fixtures remain visible, including those without NFOs.
"""
from collections import defaultdict
import datetime as dt
from pathlib import Path
import math
import socket

from filing.publish import canonical_paths, SERIES_PATHS

UTC = dt.timezone.utc
SERIES = (
    "Formula_1",
    "Rugby_Mens_6N", "Rugby_Mens_Championship", "Rugby_Mens_Intl", "Rugby_Mens_Lions",
    "Rugby_Mens_Nations", "Rugby_Mens_Prem", "Rugby_Mens_World_Cup",
    "Rugby_Womens_6N", "Rugby_Womens_Intl", "Rugby_Womens_Lions",
    "Rugby_Womens_PWR", "Rugby_Womens_World_Cup", "Rugby_Womens_WXV",
)
assert set(SERIES) == set(SERIES_PATHS)
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".ts", ".mov", ".webm")
CANCELLED = frozenset(("cancelled", "canceled", "superseded"))
FIELDS = {"_id": 1, "series_id": 1, "season": 1, "episode": 1,
          "title": 1, "home": 1, "away": 1, "round": 1,
          "scheduled": 1, "scheduled_date": 1, "status": 1,
          "nfo_path": 1, "venue": 1}
FILE_FIELDS = {"episode_id": 1, "path": 1, "host": 1, "file_state": 1,
               "media": 1, "size_bytes": 1, "mtime_ns": 1, "release_uri": 1}


def aware(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return None


def event_time(episode, timezone):
    """Return (UTC datetime for chronological sorting, is_date_only)."""
    scheduled = aware(episode.get("scheduled"))
    if scheduled is not None:
        return scheduled, False
    date_text = episode.get("scheduled_date")
    if isinstance(date_text, str):
        try:
            day = dt.date.fromisoformat(date_text)
            date_only = dt.datetime.combine(day, dt.time.min, tzinfo=timezone)
            return date_only.astimezone(UTC), True
        except ValueError:
            pass
    return None, False


def filesize(size):
    if not isinstance(size, (int, float)) or size < 0:
        return "—"
    if size < 1024:
        return f"{int(size)} B"
    n = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
    return "—"


CODECS = {"h264": "H.264", "hevc": "H.265", "h265": "H.265",
          "av1": "AV1", "vp9": "VP9", "mpeg2video": "MPEG-2", "h263": "H.263"}


def quality(media):
    """Best available frame dimensions/frame-rate/codec from ffprobe metadata."""
    if not isinstance(media, dict):
        return None
    video = media.get("video")
    if not isinstance(video, dict):
        return None
    details = []
    height = video.get("height")
    try:
        height = int(height) if height is not None else None
    except (ValueError, TypeError, OverflowError):
        height = None
    if height and 100 <= height <= 8640:
        details.append(f"{height}p")
    fps = video.get("fps")
    try:
        fps = float(fps) if fps is not None else None
    except (ValueError, TypeError, OverflowError):
        fps = None
    if fps is not None and math.isfinite(fps) and 0 < fps <= 1000:
        details.append(f"{fps:g} fps")
    codec = video.get("codec")
    if isinstance(codec, str) and codec:
        details.append(CODECS.get(codec.lower(), codec.upper()))
    return " · ".join(details) if details else None


class DashboardData:
    def __init__(self, database, sports_root, timezone, *, hostname=None):
        self.database = database
        self.root = Path(sports_root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Sports root must be a readable directory")
        self.timezone = timezone
        self.hostname = hostname or socket.gethostname()

    def episodes(self, series, *, now=None):
        """Newest-first known Atlas events, never later than one week ahead."""
        if series not in SERIES:
            raise ValueError("Unsupported series")
        now = aware(now) or dt.datetime.now(UTC)
        cutoff = now + dt.timedelta(days=7)
        episodes = []
        for item in self.database.episodes.find({"series_id": series}, FIELDS):
            event_at, day_only = event_time(item, self.timezone)
            if event_at is not None and event_at > cutoff:
                continue
            episodes.append((item, event_at, day_only))
        episodes.sort(key=lambda x: (
            x[1] is not None,
            x[1] if x[1] is not None else dt.datetime.min.replace(tzinfo=UTC),
            str(x[0].get("_id", ""))), reverse=True)
        return episodes

    def inventory(self, episodes):
        ids = [ep["_id"] for ep, _, _ in episodes if isinstance(ep.get("_id"), str)]
        if not ids:
            return {}
        by_id = defaultdict(list)
        # Chunk queries to avoid excessive BSON size for large historical archives.
        for start in range(0, len(ids), 400):
            for item in self.database.files.find(
                    {"episode_id": {"$in": ids[start:start + 400]}}, FILE_FIELDS):
                by_id[item.get("episode_id")].append(item)
        return by_id

    def _existing_video(self, path, series):
        """Return (real path, stat) only for a nonempty file in that series."""
        path = Path(path)
        if not path.is_absolute() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            return None
        try:
            if path.is_symlink():
                return None
            resolved = path.resolve(strict=True)
            series_dir = self.root / series
            if not resolved.is_relative_to(series_dir) or not resolved.is_file():
                return None
            stat = resolved.stat()
            if stat.st_size > 0:
                return resolved, stat
        except (OSError, ValueError):
            pass
        return None

    def installed(self, episode, records):
        """Find a verified installed recording, and its matching Atlas metadata."""
        series = episode.get("series_id")
        canonical = None
        try:
            canonical, _ = canonical_paths(episode, self.root)
        except (KeyError, ValueError, TypeError):
            pass
        candidates = []
        if canonical is not None:
            candidates.append(canonical)
            # Existing media may have a different container extension.
            candidates.extend(canonical.with_suffix(ext) for ext in VIDEO_EXTENSIONS
                              if ext != ".mkv")
        # Only explicit episode-linked inventory permits a noncanonical path.
        for record in records:
            if record.get("file_state") == "moved":
                continue
            path = record.get("path")
            if isinstance(path, str):
                candidates.append(Path(path))

        checked = set()
        for candidate in candidates:
            key = str(candidate)
            if key in checked:
                continue
            checked.add(key)
            found = self._existing_video(candidate, series)
            if found is None:
                continue
            path, stat = found
            matches = []
            for record in records:
                if record.get("file_state") == "moved":
                    continue
                recpath = record.get("path")
                if not isinstance(recpath, str):
                    continue
                try:
                    if Path(recpath).resolve(strict=False) == path:
                        matches.append(record)
                except (OSError, ValueError):
                    continue
            matches.sort(key=lambda record: record.get("host") == self.hostname,
                         reverse=True)
            matched = matches[0] if matches else None
            media = matched.get("media") if matched is not None else None
            # Do not describe a changed file using stale Atlas probe metadata.
            if matched is not None and (
                    (matched.get("size_bytes") is not None
                     and matched["size_bytes"] != stat.st_size)
                    or (matched.get("mtime_ns") is not None
                        and matched["mtime_ns"] != stat.st_mtime_ns)):
                media = None
            return {"path": path, "stat": stat, "quality": quality(media),
                    "source": (matched.get("release_uri") or "").split(":")[0]
                    if matched else ""}
        return None

    def rows(self, series, *, now=None):
        now = aware(now) or dt.datetime.now(UTC)
        episodes = self.episodes(series, now=now)
        files = self.inventory(episodes)
        result = []
        for episode, moment, day_only in episodes:
            installed = self.installed(episode, files.get(episode.get("_id"), []))
            status = str(episode.get("status") or "").lower()
            if installed:
                state = "Installed"
            elif status in CANCELLED:
                state = "Cancelled" if status != "superseded" else "Superseded"
            elif moment is not None and moment > now:
                state = "Upcoming"
            else:
                state = "Missing"
            if moment is None:
                date_label = "Date unknown"
            elif day_only:
                date_label = moment.astimezone(self.timezone).strftime("%a %d %b %Y")
            else:
                date_label = moment.astimezone(self.timezone).strftime("%a %d %b %Y · %I:%M %p")
            title = episode.get("title") or (
                f"{episode.get('home', '')} vs {episode.get('away', '')}"
                if episode.get("home") and episode.get("away") else "Untitled event"
            )
            season, number = episode.get("season"), episode.get("episode")
            identifier = (f"s{season}e{number:03d}"
                          if type(season) is int and type(number) is int else "Unnumbered")
            result.append({
                "id": str(episode.get("_id", "")), "title": str(title),
                "date": date_label, "at": moment,
                "number": identifier, "status": state, "installed": bool(installed),
                "quality": (installed["quality"] if installed else None),
                "size": filesize(installed["stat"].st_size) if installed else "—",
                "source": installed["source"] if installed else "",
                "venue": str(episode.get("venue") or ""),
            })
        return result

    def details(self, series, episode_id):
        """Lookup for lazy quality probing; names must match an Atlas episode."""
        if series not in SERIES or not episode_id or len(episode_id) > 512:
            raise ValueError("Invalid event")
        episode = self.database.episodes.find_one(
            {"_id": episode_id, "series_id": series}, FIELDS)
        if episode is None:
            return None
        files = list(self.database.files.find({"episode_id": episode_id}, FILE_FIELDS))
        return self.installed(episode, files)
