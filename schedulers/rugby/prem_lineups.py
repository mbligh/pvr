"""PREM Rugby selected-player team sheets, separate from spoiler-free fixtures.

Match responses carry results/statistics. Only explicitly allowlisted player
fields are copied into the independent prem_lineups collection.
"""
import datetime as dt
import json
import os
import urllib.error
import urllib.request

from schedulers.rugby.prem import PUBLIC_HEADERS
from schedulers.rugby.prem_identity import normalize_team

SERIES = "Rugby_Mens_Prem"
SOURCE = "premrugby.com"
UTC = dt.timezone.utc


def utc(value):
    """PyMongo returns UTC datetimes without tzinfo unless tz_aware is enabled."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def fixture_id(episode):
    """Use the official PREM ID; legacy episode _id hashes are NOT match IDs."""
    if episode.get("series_id") != SERIES:
        raise ValueError("Not a PREM episode")
    value = (episode.get("external_ids") or {}).get("prem")
    if isinstance(value, bool) or not str(value or "").isdecimal() or int(value) <= 0:
        raise ValueError(f"Missing official PREM ID on episode {episode.get('_id')}")
    return int(value)


def fetch(fixture):
    """Retrieve the official RugbyViz match JSON with public website headers."""
    if type(fixture) is not int or fixture <= 0:
        raise ValueError("Invalid PREM fixture ID")
    url = (f"https://rugby-union-feeds.incrowdsports.com/v1/matches/"
           f"{fixture}?provider=rugbyviz")
    request = urllib.request.Request(url, headers={
        "User-Agent": "pvr-prem-lineups/1.0",
        "Accept": "application/json",
    })
    for key, env in (("X-APP-ID", "PREM_APP_ID"),
                     ("X-API-KEY", "PREM_API_KEY"),
                     ("X-REALM", "PREM_REALM")):
        request.add_header(key, os.environ.get(env) or PUBLIC_HEADERS[key])
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"PREM lineup API HTTP {exc.code} for {fixture}") from exc
    data = payload.get("data")
    if (not isinstance(data, dict) or
            str(data.get("id")) != str(fixture) or
            data.get("provider") != "rugbyviz"):
        raise ValueError(f"Unexpected PREM match response for {fixture}")
    return data


def normalize(data, episode, now=None):
    now = utc(now or dt.datetime.now(UTC))
    fixture = fixture_id(episode)
    if str(data.get("id")) != str(fixture):
        raise ValueError(f"PREM fixture ID mismatch for {fixture}")
    teams = {"home": [], "away": []}
    for side in ("home", "away"):
        team = data.get(side + "Team")
        if not isinstance(team, dict):
            raise ValueError(f"Missing {side} team for PREM fixture {fixture}")
        expected = episode.get(side)
        actual = team.get("name")
        if expected and actual and normalize_team(expected) != normalize_team(actual):
            raise ValueError(f"PREM fixture {fixture} {side} team mismatch: {expected!r} != {actual!r}")
        players = team.get("players") or []
        if not isinstance(players, list):
            raise ValueError(f"Invalid {side} lineup for PREM fixture {fixture}")
        shirt_numbers = set()
        for player in players:
            number = player.get("positionId")
            if (type(number) is not int or number not in range(1, 24)
                    or number in shirt_numbers or
                    type(player.get("id")) is not int or
                    not isinstance(player.get("name"), str) or
                    not player["name"].strip()):
                raise ValueError(f"Invalid or repeated {side} player/number in PREM {fixture}")
            shirt_numbers.add(number)
            group = "forwards" if number <= 8 else "backs" if number <= 15 else "subs"
            item = {
                "id": player["id"],
                "name": player["name"].strip(),
                "shirt_number": number,
                "position": player.get("position") or "",
                "group": group,
            }
            for key, output in (("known", "known_name"),
                                ("firstName", "first_name"),
                                ("lastName", "last_name")):
                if isinstance(player.get(key), str) and player[key]:
                    item[output] = player[key]
            if player.get("captain") in (True, "true", "True", "Y"):
                item["captain"] = True
            teams[side].append(item)
        teams[side].sort(key=lambda player: player["shirt_number"])

    complete = all(
        len(players) >= 22 and set(range(1, 16)).issubset(
            {player["shirt_number"] for player in players})
        for players in teams.values()
    )
    status = "complete" if complete else "partial" if any(teams.values()) else "unavailable"
    record = {
        "_id": episode["_id"],
        "fixture_id": fixture,
        "series_id": SERIES,
        "season": episode["season"],
        "home_team": episode.get("home"),
        "away_team": episode.get("away"),
        "home": teams["home"],
        "away": teams["away"],
        "status": status,
        "source": SOURCE,
        "checked_at": now,
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


def candidates(db, *, season=None, fixture=None, force=False, limit=50, now=None):
    now = utc(now or dt.datetime.now(UTC))
    query = {"series_id": SERIES}
    if season is not None:
        query["season"] = season
    if fixture is not None:
        query["external_ids.prem"] = str(fixture)
    episodes = list(db.episodes.find(query, {
        "_id": 1, "series_id": 1, "external_ids.prem": 1,
        "season": 1, "home": 1, "away": 1,
        "scheduled": 1, "scheduled_date": 1,
    }))
    existing = {row["_id"]: row for row in db.prem_lineups.find(
        {"_id": {"$in": [ep["_id"] for ep in episodes]}},
        {"_id": 1, "status": 1, "next_check_at": 1},
    )}
    selected = []
    for ep in episodes:
        fixture_id(ep)
        prior = existing.get(ep["_id"], {})
        if not force:
            scheduled = utc(ep.get("scheduled"))
            if scheduled and scheduled > now + dt.timedelta(days=3):
                continue
            day = ep.get("scheduled_date")
            if not scheduled and day and dt.date.fromisoformat(day) > (
                    now + dt.timedelta(days=3)).date():
                continue
            if prior.get("status") == "complete":
                continue
            next_check = utc(prior.get("next_check_at"))
            if next_check and next_check > now:
                continue
        selected.append(ep)
    selected.sort(key=lambda ep: (
        utc(ep.get("scheduled")) or dt.datetime.min.replace(tzinfo=UTC),
        ep["_id"],
    ), reverse=True)
    return selected[:limit] if limit else selected


def upsert(db, record):
    """Never replace a complete sheet with less data on a later request."""
    prior = db.prem_lineups.find_one({"_id": record["_id"]})
    if prior:
        old_count = len(prior.get("home", [])) + len(prior.get("away", []))
        new_count = len(record["home"]) + len(record["away"])
        if prior.get("status") == "complete" and record["status"] != "complete":
            return False
        if old_count > new_count:
            record["home"], record["away"] = prior["home"], prior["away"]
            record["status"] = prior["status"]
    changes = {key: value for key, value in record.items() if key != "_id"}
    update = {
        "$set": changes,
        "$setOnInsert": {"created_at": record["checked_at"]},
    }
    if record["status"] == "complete":
        update["$unset"] = {"next_check_at": ""}
    db.prem_lineups.update_one({"_id": record["_id"]}, update, upsert=True)
    return True
