"""Reconcile official Gallagher PREM match IDs to existing public-ICS fixture UIDs.

The previous Atlas calendar snapshot is the source of *historical identity*
and frozen kickoff data; the official API is the sole live fixture source.
Never import results, scores, or other post-kickoff observations.
"""
import datetime as dt
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
VERSUS = re.compile(r"\s+(?:vs?\.?|versus)\s+", re.I)


def season_for(now):
    """Public match API season code for the UK July-June rugby season."""
    if isinstance(now, dt.date) and not isinstance(now, dt.datetime):
        now = dt.datetime.combine(now, dt.time.min, tzinfo=UK)
    if now.tzinfo is None:
        raise ValueError("PREM season needs an aware date")
    uk = now.astimezone(UK)
    return (uk.year if uk.month >= 7 else uk.year - 1) * 100 + 1


def normalize_team(text):
    key = re.sub(r"[^a-z0-9]", "", text.casefold())
    return {
        "bathrugby": "bath", "gloucesterrugby": "gloucester",
        "newcastleredbulls": "newcastle", "newcastlefalcons": "newcastle",
    }.get(key, key)


def match_key(event):
    sides = VERSUS.split(str(event.get("SUMMARY", "")), maxsplit=1)
    if len(sides) != 2:
        return None
    return tuple(map(normalize_team, sides))


def kickoff_day(event):
    value = event.decoded("DTSTART")
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UK)
        return value.astimezone(UK).date()
    return value


def season_of_event(event):
    day = kickoff_day(event)
    return day.year if day.month >= 7 else day.year - 1


def reconcile_uids(source, previous):
    """Return rewritten source and approved metadata keyed by stable UID.

    Reuse a public-ICS UID when an unambiguous official fixture matches its
    teams, season and kickoff date. New fixtures retain their official UID.
    Never rewrite previous ICS events or timestamps (the merger freezes them).
    """
    candidates = defaultdict(list)
    previous_by_official_id = {}
    for event in previous.walk("VEVENT"):
        key = match_key(event)
        if key is not None:
            candidates[(season_of_event(event), key)].append(event)
        known_id = str(event.get("X-PREM-ID", "")).strip()
        if known_id:
            if known_id in previous_by_official_id:
                raise ValueError(f"Duplicate stored official PREM ID: {known_id}")
            previous_by_official_id[known_id] = event

    output_metadata = {}
    mapped_old = set()
    for event in source.walk("VEVENT"):
        uid = str(event.get("UID", "")).strip()
        official_id = str(event.get("X-PREM-ID", "")).strip()
        round_no = int(str(event.get("X-PREM-ROUND", "0")))
        if not uid or not official_id or not 1 <= round_no <= 99:
            raise ValueError("Malformed official PREM fixture identity or round")
        key = (season_of_event(event), match_key(event))
        possibles = candidates.get(key, [])
        if official_id in previous_by_official_id:
            # Subsequent schedule changes can exceed 14 days, and even
            # change teams. The official ID is then authoritative.
            matched = [previous_by_official_id[official_id]]
        else:
            matched = [old for old in possibles
                       if abs((kickoff_day(old) - kickoff_day(event)).days) <= 14]
        if len(matched) > 1:
            raise ValueError(f"Ambiguous PREM fixture match for {uid}")
        if matched:
            old_uid = str(matched[0].get("UID"))
            if old_uid in mapped_old:
                raise ValueError(f"Multiple official PREM fixtures match {old_uid}")
            mapped_old.add(old_uid)
            event["UID"] = old_uid
            uid = old_uid
        if uid in output_metadata:
            raise ValueError(f"Duplicate official PREM UID after reconciliation: {uid}")
        output_metadata[uid] = {"prem": official_id, "round": round_no}
    return source, output_metadata
