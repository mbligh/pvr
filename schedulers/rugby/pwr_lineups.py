"""Official PWR team-sheet ingestion, separate from spoiler-free fixture metadata."""
import datetime as dt
import json
import re
import urllib.request

SERIES = "Rugby_Womens_PWR"
FIXTURE_ID = re.compile(r"^Rugby_Womens_PWR:pwr-(\d+)@mbligh\.github\.io$")
UTC = dt.timezone.utc
# Historic PWR lineups before this date are incomplete/inconsistent and not
# useful for routine retries. Explicit fixture and --force checks bypass it.
MIN_AUTO_LINEUP_DATE = dt.date(2023, 7, 1)


def utc(value):
    """Normalize MongoDB naive UTC datetimes and aware datetime values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)



def fixture_id(episode):
    match = FIXTURE_ID.fullmatch(episode["_id"])
    if episode.get("series_id") != SERIES or not match:
        raise ValueError("Invalid PWR episode identity")
    return int(match.group(1))


def fetch(fixture):
    req = urllib.request.Request(
        f"https://www.thepwr.com/api/oval/lineup/match/{fixture}",
        headers={"User-Agent": "pvr-lineups/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as reply:
        payload = json.load(reply)
    # The provider sometimes knows the fixture from its season listing but
    # has no corresponding match-detail record (historic gaps and playoffs).
    # This is missing lineup data, not a failed import; keep the retry policy.
    if payload.get("status") == 404 and payload.get("message") == "Could not fetch match data":
        return {"forwards": [], "backs": [], "subs": []}
    if payload.get("status") != 200 or not isinstance(payload.get("data"), dict):
        raise ValueError(f"Bad lineup response for PWR fixture {fixture}")
    return payload["data"]


def normalize(data, episode, now=None):
    now = utc(now or dt.datetime.now(UTC))
    fixture = fixture_id(episode)
    teams = {"home": [], "away": []}
    # Some historical match feeds carry numbered players 24/25 ("sub 9/10"),
    # beyond the standard matchday 23. Retain them without treating them as
    # a selected shirt 1-23 or counting them toward lineup completeness.
    additional_players = {"home": [], "away": []}
    shirts = set()
    for group in ("forwards", "backs", "subs"):
        rows = data.get(group)
        if not isinstance(rows, list):
            raise ValueError(f"Missing/invalid {group} group for fixture {fixture}")
        for row in rows:
            number = row.get("shirtNumber")
            # The API occasionally prepends an *unnumbered* extra player
            # (shirtNumber=0) to the forwards array, even when their actual
            # position is a winger or substitute. They are not among the
            # selected numbered 1-23 and must not invalidate the team sheet.
            if number == 0:
                continue
            if type(number) is not int or not 1 <= number <= 25 or number in shirts:
                raise ValueError(f"Duplicate/invalid shirt number {number!r} in fixture {fixture}")
            shirts.add(number)
            for side in ("home", "away"):
                player = row.get(side + "Player")
                if not player:
                    continue
                if type(player.get("id")) is not int or not player.get("name"):
                    raise ValueError(f"Invalid player in fixture {fixture}")
                item = {"id": player["id"], "name": player["name"],
                        "shirt_number": number, "position": row["position"], "group": group}
                for source, target in (("knownName", "known_name"),
                                       ("firstName", "first_name"), ("lastName", "last_name")):
                    if player.get(source):
                        item[target] = player[source]
                if player.get("captain") in (True, "true"):
                    item["captain"] = True
                target = teams if number <= 23 else additional_players
                target[side].append(item)
    complete = all(len(players) >= 22 and set(range(1, 16)).issubset(
        {p["shirt_number"] for p in players}) for players in teams.values())
    status = "complete" if complete else "partial" if any(teams.values()) else "unavailable"
    record = {"_id": episode["_id"], "fixture_id": fixture,
              "series_id": SERIES, "season": episode["season"],
              "home_team": episode.get("home"), "away_team": episode.get("away"),
              "home": teams["home"], "away": teams["away"],
              "additional_players": additional_players,
              "status": status, "source": "thepwr.com", "checked_at": now}
    if status != "complete":
        kickoff = utc(episode.get("scheduled"))
        if kickoff is None and episode.get("scheduled_date"):
            kickoff = dt.datetime.combine(
                dt.date.fromisoformat(episode["scheduled_date"]),
                dt.time(hour=12), tzinfo=UTC)
        if kickoff and now < kickoff + dt.timedelta(days=2):
            interval = dt.timedelta(hours=3)
        elif kickoff and now < kickoff + dt.timedelta(days=14):
            interval = dt.timedelta(days=1)
        else:
            interval = dt.timedelta(days=30)
        record["next_check_at"] = now + interval
    return record


def candidates(db, season=None, fixture=None, force=False, limit=50, now=None):
    now = utc(now or dt.datetime.now(UTC))
    query = {"series_id": SERIES}
    if season is not None:
        query["season"] = season
    if fixture is not None:
        query["_id"] = f"{SERIES}:pwr-{fixture}@mbligh.github.io"
    episodes = list(db.episodes.find(query, {"_id": 1, "series_id": 1,
        "season": 1, "home": 1, "away": 1,
        "scheduled": 1, "scheduled_date": 1}))
    existing = {doc["_id"]: doc for doc in db.pwr_lineups.find(
        {"_id": {"$in": [ep["_id"] for ep in episodes]}},
        {"_id": 1, "status": 1, "next_check_at": 1})}
    chosen = []
    for episode in episodes:
        fixture_id(episode)
        old = existing.get(episode["_id"], {})
        if not force:
            scheduled = utc(episode.get("scheduled"))
            day = episode.get("scheduled_date")
            if fixture is None:
                # Do not keep retrying incomplete 2017–early 2023 data.
                # The cutoff applies to automatic scans only: --fixture
                # permits targeted investigation of an older match.
                match_date = (dt.date.fromisoformat(day) if day
                              else scheduled.date() if scheduled else None)
                if match_date is None or match_date < MIN_AUTO_LINEUP_DATE:
                    continue
            if scheduled and scheduled > now + dt.timedelta(days=3):
                continue
            # TBC fixtures still have an authoritative scheduled_date. Avoid
            # polling future seasons months before team selection is possible.
            if not scheduled and day and dt.date.fromisoformat(day) > (
                    now + dt.timedelta(days=3)).date():
                continue
            if old.get("status") == "complete":
                continue
            due = utc(old.get("next_check_at"))
            if due and due > now:
                continue
        chosen.append(episode)
    chosen.sort(key=lambda ep: utc(ep.get("scheduled")) or dt.datetime.min.replace(tzinfo=UTC),
                reverse=True)
    return chosen[:limit] if limit else chosen


def upsert(db, record):
    old = db.pwr_lineups.find_one({"_id": record["_id"]})
    if old:
        old_total = len(old.get("home", [])) + len(old.get("away", []))
        new_total = len(record["home"]) + len(record["away"])
        if old.get("status") == "complete" and record["status"] != "complete":
            return False
        if old_total > new_total:
            record["home"], record["away"] = old["home"], old["away"]
            record["status"] = old["status"]
    changes = {k: v for k, v in record.items() if k != "_id"}
    update = {"$set": changes, "$setOnInsert": {"created_at": record["checked_at"]}}
    if record["status"] == "complete":
        update["$unset"] = {"next_check_at": ""}
    db.pwr_lineups.update_one({"_id": record["_id"]}, update, upsert=True)
    return True
