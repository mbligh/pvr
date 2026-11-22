"""Conservative, one-time official Gallagher PREM 2026/27 history recovery.

The normal rugby calendar merge must continue to reject newly discovered
past fixtures. This helper is ONLY used by the explicit backfill command,
after validating the full official first three rounds.
"""
from collections import defaultdict
from .prem_identity import kickoff_day, match_key

SERIES = "Rugby_Mens_Prem"
SEASON = 2026
# The verified 2026/27 official round IDs. These are fixture IDs, not scores.
ROUND_IDS = {
    1: set(map(str, range(294908, 294913))),
    2: set(map(str, range(294913, 294918))),
    3: set(map(str, range(294918, 294923))),
}
ROUND_DATES = {
    1: ("2026-09-25", "2026-09-27"),
    2: ("2026-10-02", "2026-10-04"),
    3: ("2026-10-09", "2026-10-11"),
}


def checked_rounds(source, now):
    """Require 15 verified, timed fixtures; never accept a partial API response."""
    rounds = defaultdict(list)
    for event in source.walk("VEVENT"):
        raw = str(event.get("X-PREM-ROUND", "")).strip()
        if raw not in ("1", "2", "3"):
            continue
        round_no = int(raw)
        official = str(event.get("X-PREM-ID", "")).strip()
        start = event.decoded("DTSTART")
        if not hasattr(start, "tzinfo") or start.tzinfo is None:
            raise ValueError("PREM history requires confirmed timezone-aware kickoffs")
        day = kickoff_day(event)
        lo, hi = ROUND_DATES[round_no]
        if not (lo <= day.isoformat() <= hi) or start > now:
            raise ValueError("PREM historical fixture outside verified round dates")
        teams = match_key(event)
        if not teams or any(not name or name == "tbc" for name in teams):
            raise ValueError("PREM historical fixture missing real teams")
        rounds[round_no].append((official, teams, event))

    for round_no in (1, 2, 3):
        found = rounds[round_no]
        ids = [official for official, _, _ in found]
        teams = [team for _, pair, _ in found for team in pair]
        if len(found) != 5 or set(ids) != ROUND_IDS[round_no]:
            raise ValueError(
                f"PREM 2026 round {round_no} is incomplete or has unexpected official match IDs"
            )
        if len(set(teams)) != 10:
            raise ValueError(f"PREM 2026 round {round_no} repeats or omits a team")
    return rounds


def add_history(source, merged, now):
    """Add only missing verified past events, preserving accepted old events."""
    rounds = checked_rounds(source, now)
    accepted = {str(e.get("UID", "")).strip() for e in merged.walk("VEVENT")}
    count = 0
    for round_no in (1, 2, 3):
        for official, teams, event in rounds[round_no]:
            uid = str(event.get("UID", "")).strip()
            if not uid:
                raise ValueError("PREM historical fixture lacks stable UID")
            if uid not in accepted:
                merged.add_component(event)
                accepted.add(uid)
                count += 1
    return merged, count


def strip_round_three_assignments(prior_manifest):
    """Renumber 2026 Round 3 chronologically, without changing fixture IDs."""
    manifest = dict(prior_manifest)
    manifest["fixtures"] = [
        fixture for fixture in prior_manifest.get("fixtures", [])
        if not (
            fixture.get("competition") == SERIES
            and fixture.get("season") == SEASON
            and fixture.get("round") == 3
        )
    ]
    return manifest


def validate_plan(db, episodes):
    """Preflight before writes: exact 15 episode slots, stable identities."""
    planned = [ep for ep in episodes
               if ep.get("series_id") == SERIES
               and ep.get("season") == SEASON
               and ep.get("round") in (1, 2, 3)]
    if len(planned) != 15:
        raise ValueError(f"PREM backfill expected 15 numbered fixtures, got {len(planned)}")
    slots = {(ep["round"], ep["episode"]) for ep in planned}
    wanted = {(round_no, round_no * 100 + number)
              for round_no in (1, 2, 3) for number in range(1, 6)}
    if slots != wanted or len({ep["_id"] for ep in planned}) != 15:
        raise ValueError("PREM history contains missing/duplicate season episode numbers")
    official_ids = {str((ep.get("external_ids") or {}).get("prem", ""))
                    for ep in planned}
    if official_ids != set().union(*ROUND_IDS.values()):
        raise ValueError("PREM backfill lost a verified official fixture ID")

    old = list(db.episodes.find({
        "series_id": SERIES, "season": SEASON, "round": {"$in": [1, 2, 3]}
    }, {"_id": 1, "episode": 1, "external_ids": 1}))
    by_id = {ep["_id"]: ep for ep in planned}
    if any(existing["_id"] not in by_id for existing in old):
        raise ValueError("Existing historical PREM fixture is missing from the proposed import")
    for existing in old:
        source_id = (existing.get("external_ids") or {}).get("prem")
        new_id = by_id[existing["_id"]]["external_ids"]["prem"]
        if source_id is not None and str(source_id) != str(new_id):
            raise ValueError("PREM backfill would change an existing official fixture identity")
    changes = [existing["_id"] for existing in old
               if existing.get("episode") != by_id[existing["_id"]]["episode"]]
    if changes:
        files = list(db.files.find({"episode_id": {"$in": changes}}, {"_id": 1}))
        if files:
            raise ValueError("Refusing PREM renumber: existing recordings use old episode numbers")
    return {
        "rounds": [1, 2, 3],
        "fixtures": len(planned),
        "missing": 15 - len(old),
        "renumbered": len(changes),
    }
