"""World Rugby match-day team selections from the official RIMS summary API.

Only player identifiers, names, shirt numbers and positions are retained.
The summary response also contains scores and match events; these are never
copied into lineup documents or the spoiler-free episode catalogue.
"""
import datetime as dt
import json
import re
import urllib.error
import urllib.request
import uuid

UTC = dt.timezone.utc
SERIES = (
    "Rugby_Mens_Intl",
    "Rugby_Mens_Nations",
    "Rugby_Womens_Intl",
    "Rugby_Womens_WXV",
)
ID_PATTERN = re.compile(
    r"^(Rugby_(?:Mens_Intl|Mens_Nations|Womens_Intl|Womens_WXV)):"
    r"world-rugby-([0-9a-fA-F-]{36})@mbligh\.github\.io$"
)
SUMMARY_API = "https://api.wr-rims-prod.pulselive.com/rugby/v3/match/{}/summary"

# Rugby XV team-sheet display order is not the shirt number. The RIMS
# lineups use consistent pitch-slot orders for starters (e.g. prop #3 uses
# order=5, winger #14 uses order=25). We only consult this mapping when the
# API repeats a jersey number AND the implied starter slot is vacant.
STARTER_SHIRT_BY_ORDER = {
    1: 1, 4: 2, 5: 3, 6: 4, 8: 5,
    9: 6, 11: 7, 12: 8, 13: 9, 17: 10,
    18: 11, 19: 12, 21: 13, 25: 14, 26: 15,
}



def utc(value):
    """Make PyMongo's naive UTC datetime values comparable with aware ones."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fixture_id(episode):
    match = ID_PATTERN.fullmatch(episode.get("_id") or "")
    if not match or match.group(1) != episode.get("series_id"):
        raise ValueError("Episode does not have a World Rugby match UUID")
    return str(uuid.UUID(match.group(2)))


def fetch(fixture):
    """Read a single known World Rugby match; allowlist fields in normalize()."""
    match_id = str(uuid.UUID(str(fixture)))
    request = urllib.request.Request(SUMMARY_API.format(match_id), headers={
        "User-Agent": "pvr-world-rugby-lineups/1.0", "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=45) as reply:
            payload = json.load(reply)
    except urllib.error.HTTPError as exc:
        # Unplayed matches may have no published summary yet. Only 404 is
        # interpreted as missing team sheets; other failures are real errors.
        if exc.code == 404:
            return {"match": {"matchId": match_id},
                    "teams": [{"teamList": {"list": []}},
                              {"teamList": {"list": []}}]}
        raise
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected World Rugby response for {match_id}")
    return payload


def inferred_shirt_overrides(roster, match_id, side):
    """Correct only source jersey conflicts with an unambiguous starter slot.

    Do not infer numbers for arbitrary missing/duplicate rows. Caller must
    retain the original number for auditability.
    """
    by_number = {}
    for index, row in enumerate(roster):
        if not isinstance(row, dict):
            continue
        raw = row.get("number")
        if (raw is None or raw == "" or type(raw) not in (str, int)
                or not str(raw).isdecimal()):
            continue
        by_number.setdefault(int(raw), []).append((index, row))

    corrections = {}
    used = set(by_number)
    for shirt, entries in list(by_number.items()):
        if len(entries) <= 1:
            continue
        candidates = []
        for index, row in entries:
            expected = STARTER_SHIRT_BY_ORDER.get(row.get("order"))
            player = row.get("player") or {}
            if (expected is not None and expected != shirt
                    and expected not in used
                    and row.get("position") != "Rep"
                    and player.get("isReplacement") is False):
                candidates.append((index, expected))
        if len(candidates) != 1:
            raise ValueError(
                f"World Rugby {match_id}: ambiguous duplicate {side} shirt {shirt!r}")
        index, correct = candidates[0]
        corrections[index] = correct
        used.add(correct)
    return corrections


def normalize(payload, episode, now=None):
    now = utc(now or dt.datetime.now(UTC))
    match_id = fixture_id(episode)
    match = payload.get("match")
    summary_teams = payload.get("teams")
    if match is None and summary_teams == []:
        # RIMS responds HTTP 200 with {"match": null, "teams": []}
        # for certain known fixtures lacking published match detail.
        # This is not a mismatched ID, and should follow normal retries.
        match = {"matchId": match_id}
        summary_teams = [{"teamList": {"list": []}},
                         {"teamList": {"list": []}}]
    if not isinstance(match, dict) or match.get("matchId") != match_id:
        raise ValueError(f"World Rugby match ID mismatch: {match_id}")
    if not isinstance(summary_teams, list) or len(summary_teams) != 2:
        raise ValueError(f"Missing World Rugby team lists: {match_id}")
    match_teams = match.get("teams") or []
    if match_teams and len(match_teams) != 2:
        raise ValueError(f"Invalid World Rugby team identity: {match_id}")

    selections = {"home": [], "away": []}
    extras = {"home": [], "away": []}
    for index, side in enumerate(("home", "away")):
        if match_teams:
            reported = (match_teams[index] or {}).get("name")
            expected = episode.get(side)
            if reported and expected:
                def comparable(value):
                    return re.sub(r"[^a-z0-9]", "", value.casefold())
                if comparable(reported) != comparable(expected):
                    raise ValueError(
                        f"World Rugby {match_id} {side} team mismatch: "
                        f"{expected!r} != {reported!r}")
        entry = summary_teams[index]
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid World Rugby {side} summary: {match_id}")
        team_list = entry.get("teamList") or {}
        roster = team_list.get("list") or []
        if not isinstance(roster, list):
            raise ValueError(f"Invalid World Rugby {side} lineup: {match_id}")
        # Validate a duplicate against the official pitch slot before
        # deciding the selected shirt number. The original is retained.
        overrides = inferred_shirt_overrides(roster, match_id, side)
        numbers = set()
        for row_index, row in enumerate(roster):
            if not isinstance(row, dict):
                raise ValueError(f"Malformed World Rugby player record: {match_id}")
            number = row.get("number")
            if number is None or number == "":
                # The same teamList includes non-players such as the coach,
                # typically without a shirt number. Never store them.
                continue
            if (type(number) not in (str, int) or
                    not str(number).isdecimal() or
                    int(number) not in range(1, 26)):
                raise ValueError(
                    f"World Rugby {match_id}: duplicate/invalid {side} shirt {number!r}")
            reported_number = int(number)
            number = overrides.get(row_index, reported_number)
            if number in numbers:
                raise ValueError(
                    f"World Rugby {match_id}: duplicate {side} shirt {number!r}")
            numbers.add(number)
            player = row.get("player") or {}
            name = player.get("name") or {}
            display = name.get("display")
            identifier = player.get("id")
            if (not isinstance(identifier, (str, int)) or
                    isinstance(identifier, bool) or
                    not str(identifier).strip() or
                    not isinstance(display, str) or not display.strip()):
                raise ValueError(f"World Rugby {match_id}: missing {side} player identity")
            result = {
                "id": str(identifier),
                "name": display.strip(),
                "shirt_number": number,
                "position": row.get("positionLabel") or row.get("position") or "",
                "group": "forwards" if number <= 8 else "backs" if number <= 15 else "subs",
            }
            if number != reported_number:
                result["reported_shirt_number"] = reported_number
                result["shirt_number_inferred"] = True
            if isinstance(player.get("altId"), str) and player["altId"]:
                result["alt_id"] = player["altId"]
            for source, key in (("first", "first_name"), ("last", "last_name")):
                section = name.get(source) or {}
                variant = section.get("known") or section.get("official")
                if variant:
                    result[key] = variant
            if row.get("isCaptain") is True:
                result["captain"] = True
            target = selections if number <= 23 else extras
            target[side].append(result)
        selections[side].sort(key=lambda value: value["shirt_number"])
        extras[side].sort(key=lambda value: value["shirt_number"])

    complete = all(
        len(players) >= 22 and set(range(1, 16)).issubset(
            {player["shirt_number"] for player in players})
        for players in selections.values()
    )
    status = ("complete" if complete else
              "partial" if any(selections.values()) else "unavailable")
    record = {
        "_id": episode["_id"], "fixture_id": match_id,
        "series_id": episode["series_id"], "season": episode["season"],
        "home_team": episode.get("home"), "away_team": episode.get("away"),
        "home": selections["home"], "away": selections["away"],
        "additional_players": extras,
        "status": status, "source": "world.rugby", "checked_at": now,
    }
    if status != "complete":
        kickoff = utc(episode.get("scheduled"))
        if kickoff and now < kickoff + dt.timedelta(days=2):
            interval = dt.timedelta(hours=3)
        elif kickoff and now < kickoff + dt.timedelta(days=14):
            interval = dt.timedelta(days=1)
        else:
            interval = dt.timedelta(days=30)
        record["next_check_at"] = now + interval
    return record


def candidates(db, *, series=None, season=None, fixture=None, force=False,
               limit=50, now=None):
    now = utc(now or dt.datetime.now(UTC))
    if series is not None and series not in SERIES:
        raise ValueError(f"Not a World Rugby series: {series}")
    query = {"series_id": series if series else {"$in": list(SERIES)}}
    if season is not None:
        query["season"] = season
    if fixture is not None:
        match_id = str(uuid.UUID(str(fixture)))
        query["_id"] = {"$regex":
            r"world-rugby-" + re.escape(match_id) + r"@mbligh\.github\.io$"}
    episodes = list(db.episodes.find(query, {
        "_id": 1, "series_id": 1, "season": 1, "home": 1, "away": 1,
        "scheduled": 1, "status": 1,
    }))
    existing = {item["_id"]: item for item in db.world_rugby_lineups.find(
        {"_id": {"$in": [ep["_id"] for ep in episodes]}},
        {"_id": 1, "status": 1, "next_check_at": 1},
    )}
    chosen = []
    for episode in episodes:
        try:
            fixture_id(episode)
        except ValueError:
            # Preserve Google ICS legacy episode IDs. Only the official
            # world-rugby-UUID entries can be queried with this API.
            continue
        if episode.get("status") in ("cancelled", "canceled", "superseded"):
            continue
        current = existing.get(episode["_id"], {})
        if not force:
            scheduled = utc(episode.get("scheduled"))
            if scheduled is None or scheduled > now + dt.timedelta(days=3):
                continue
            if current.get("status") == "complete":
                continue
            due = utc(current.get("next_check_at"))
            if due and due > now:
                continue
        chosen.append(episode)
    chosen.sort(
        key=lambda ep: (utc(ep.get("scheduled")) or
                        dt.datetime.min.replace(tzinfo=UTC), ep["_id"]),
        reverse=True,
    )
    return chosen[:limit] if limit else chosen


def upsert(db, record):
    old = db.world_rugby_lineups.find_one({"_id": record["_id"]})
    if old:
        old_total = len(old.get("home", [])) + len(old.get("away", []))
        new_total = len(record["home"]) + len(record["away"])
        if old.get("status") == "complete" and record["status"] != "complete":
            return False
        if old_total > new_total:
            record["home"], record["away"] = old["home"], old["away"]
            record["status"] = old["status"]
    update = {
        "$set": {key: value for key, value in record.items() if key != "_id"},
        "$setOnInsert": {"created_at": record["checked_at"]},
    }
    if record["status"] == "complete":
        update["$unset"] = {"next_check_at": ""}
    db.world_rugby_lineups.update_one(
        {"_id": record["_id"]}, update, upsert=True)
    return True
