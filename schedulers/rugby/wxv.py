"""Safe WXV separation of already-accepted women's international ICS fixtures.

Use the *stored spoiler-free* calendar to seed historical WXV, never refetch
historical match results. UID suffixes remain unchanged across series. The
old women's-international Atlas episodes are superseded, not deleted.
"""
import copy
import re

from icalendar import Calendar

WXV = "rugby-womens-wxv"
WOMENS = "rugby-womens-intl"
WXV_DESC = re.compile(r"^\s*WXV(?:\s+20\d{2})?(?:\s|$)", re.IGNORECASE)
WORLD_RUGBY_UID = re.compile(r"^world-rugby-[A-Za-z0-9-]+@mbligh\.github\.io$")


def is_wxv_event(event):
    if event.name != "VEVENT":
        return False
    return bool(WXV_DESC.match(str(event.get("DESCRIPTION", ""))))


def empty_like(calendar):
    """Copy calendar headers/VTIMEZONEs, without VEVENTs or cancellation IDs."""
    result = Calendar()
    for key, value in calendar.property_items():
        if key.upper() in ("BEGIN", "END", "X-WR-CANCELLED-UID"):
            continue
        result.add(key, value)
    for component in calendar.subcomponents:
        if component.name != "VEVENT":
            result.add_component(copy.deepcopy(component))
    return result


def split_snapshot(calendar):
    """Partition a trusted stored calendar by WXV description, keeping each UID."""
    intl, wxv = empty_like(calendar), empty_like(calendar)
    seen = set()
    for event in calendar.walk("VEVENT"):
        uid = str(event.get("UID", "")).strip()
        if not WORLD_RUGBY_UID.fullmatch(uid):
            # Unknown calendar events (including old Google IDs) remain
            # exclusively in the original international calendar.
            intl.add_component(copy.deepcopy(event))
            continue
        if uid in seen:
            raise ValueError(f"Duplicate World Rugby UID in stored women's ICS: {uid}")
        seen.add(uid)
        target = wxv if is_wxv_event(event) else intl
        target.add_component(copy.deepcopy(event))
    # Old cancellations lack competition labels: preserve them in Intl
    # until a fresh source query determines their series.
    cancelled = calendar.get("x-wr-cancelled-uid", [])
    if not isinstance(cancelled, list):
        cancelled = [cancelled]
    for value in cancelled:
        intl.add("x-wr-cancelled-uid", str(value))
    return intl, wxv


def original_uid(series_id):
    if not isinstance(series_id, str) or not series_id.startswith(WXV + ":"):
        raise ValueError("Not a WXV canonical episode ID")
    uid = series_id[len(WXV) + 1:]
    if not WORLD_RUGBY_UID.fullmatch(uid):
        raise ValueError("Unrecognized WXV fixture UID")
    return uid


def legacy_id(wxv_id):
    return WOMENS + ":" + original_uid(wxv_id)


def validate_migration(episodes, legacy_records, files=(), releases=()):
    """Fail closed before performing any Atlas writes.

    Existing published media cannot safely be assigned a new series without
    moving its files, so require manual reconciliation. Release references
    can be updated after the new episodes are committed.
    """
    targets = {ep["_id"]: ep for ep in episodes if ep["series_id"] == WXV}
    for old in legacy_records:
        eid = old["_id"]
        if old.get("series_id") != WOMENS or not eid.startswith(WOMENS + ":"):
            raise ValueError(f"Unexpected legacy WXV fixture identity {eid}")
        replacement = WXV + ":" + eid[len(WOMENS)+1:]
        if replacement not in targets:
            raise ValueError(f"Legacy WXV fixture not found in new source: {eid}")
        if old.get("superseded_by") not in (None, replacement):
            raise ValueError(f"Conflicting prior WXV migration for {eid}")
        if "WXV" not in str(old.get("nfo", "")) and "WXV" not in str(old.get("title", "")):
            # The description in legacy NFO is the original competition
            # evidence; team/date alone could match a different test.
            raise ValueError(f"Cannot prove legacy fixture belongs to WXV: {eid}")
    if any(True for _ in files):
        raise ValueError("WXV recordings already inventoried under Intl; reconcile file paths first")
    return targets
