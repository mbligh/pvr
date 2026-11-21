"""TransferNow availability checks for the F1 acquisition worker.

Failures are categorized conservatively:
- unavailable: positive evidence that a transfer/file is gone
- unsupported: a transfer exists but cannot be handled automatically
- transient: ambiguous network/server failure, NOT grounds for fallback

The caller owns MongoDB writes. These functions do not download media.
"""
import datetime as dt
import re

from downloaders.transfernow import TransferNowError
from downloaders.transfernow_pvr import inspect

UNAVAILABLE = (
    re.compile(r"^TransferNow API returned HTTP (?:404|410)(?:\b|:)", re.I),
    re.compile(r"^Storage server returned HTTP (?:404|410)(?:\b|:)", re.I),
    re.compile(r"^Transfer status: (?:EXPIRED|DISABLED|DELETED|CANCELLED|REMOVED)\b", re.I),
    re.compile(r"^No downloadable files in this transfer", re.I),
)
UNSUPPORTED = (
    re.compile(r"^Transfer contains \d+ files; requires manual selection"),
    re.compile(r"^Unsupported video extension for automated import:"),
    re.compile(r"^Transfer requires a password"),
)


def classify_exception(exc):
    message = str(exc)
    if isinstance(exc, (TransferNowError, ValueError)):
        if any(p.search(message) for p in UNAVAILABLE):
            return "unavailable"
        if any(p.search(message) for p in UNSUPPORTED):
            return "unsupported"
    return "transient"


def classify_check(uri, url, *, inspector=inspect):
    """Return (status, detail); never claim availability from valid URL alone."""
    try:
        info = inspector(uri, url)
        if not isinstance(info, dict) or not isinstance(info.get("entry"), dict):
            raise ValueError("TransferNow inspection returned invalid metadata")
        return "available", {
            "file_id": info["entry"].get("id"),
            "filename": info["entry"].get("name"),
            "size_bytes": info["entry"].get("size"),
        }
    except (TransferNowError, ValueError, OSError, RuntimeError) as exc:
        return classify_exception(exc), {"error": str(exc)[:500]}


def cached_status(release, now, *, unavailable_recheck_hours=24,
                  retry_after_hours=6):
    """Only confirmed unavailable/unsupported and ambiguous errors are cached.

    Recent ambiguous failures stop processing the preferred release rather
    than causing an automatic fallback to a lower-quality recording.
    """
    value = release.get("availability_check") or {}
    checked = value.get("checked_at")
    if isinstance(checked, dt.datetime):
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=dt.timezone.utc)
        elapsed = (now - checked).total_seconds()
    else:
        return None
    status = value.get("status")
    if status in ("unavailable", "unsupported"):
        if 0 <= elapsed < unavailable_recheck_hours * 3600:
            return status
    if status == "transient":
        if 0 <= elapsed < retry_after_hours * 3600:
            return status
    return None


def check_release(database, episode_id, candidate, *, inspector=inspect,
                  now=None):
    """Inspect and update a dedicated operational field, not indexer metadata.

    The indexer's next upsert preserves availability_check. The check result
    does not set available=false permanently; transfers can be rechecked.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    status, details = classify_check(
        candidate["source_uri"], candidate["url"], inspector=inspector)
    change = {"status": status, "checked_at": now, **details}
    result = database.releases.update_one(
        {"_id": candidate["source_uri"], "episode_id": episode_id},
        {"$set": {"availability_check": change}})
    if result.matched_count != 1:
        raise RuntimeError("Release changed during TransferNow check")
    return status, change
