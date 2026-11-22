#!/usr/bin/env python3
"""Enrich existing rugby episodes with officials and broadcast commentary.

Sources: World Rugby official match pages, PWR fixtures API, ICDb sitemap and
match listings. Read-only by default; --apply updates existing Atlas episodes.
No scores are collected or written. No episode upserts are performed.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from orchestration.polling import database_connection

UTC = dt.timezone.utc
PWR_SERIES = "Rugby_Womens_PWR"
WORLD_SERIES = frozenset(("Rugby_Mens_Intl", "Rugby_Womens_Intl",
                          "Rugby_Mens_Nations", "Rugby_Womens_WXV"))
WORLD_URL = "https://www.world.rugby/beta/en/match/"
ICDB_SITEMAP = "https://rugby.icdb.tv/sitemap.xml"
ICDB_MATCH_PREFIX = "https://rugby.icdb.tv/match/"
PWR_API = "https://www.thepwr.com/api/oval/fixtures?compId=1095&seasonId="
UUID = re.compile(r"world-rugby-([0-9a-f]{8}-[0-9a-f-]{27})@", re.I)
PWR_ID = re.compile(r"pwr-(\d+)@", re.I)
TAG = re.compile(r"<[^>]+>")
SPACES = re.compile(r"\s+")

ROLES = {
    "referee": "referee",
    "assistant referee 1": "assistant_referee_1",
    "assistant referee 2": "assistant_referee_2",
    "touch judge/assistant 1": "assistant_referee_1",
    "touch judge/assistant 2": "assistant_referee_2",
    "touch judge 1": "assistant_referee_1",
    "touch judge 2": "assistant_referee_2",
    "tmo": "tmo",
    "tv match official": "tmo",
    "television match official": "tmo",
    "fpro": "fpro",
    "foul play review officer (fpro)": "fpro",
    "foul play review officer": "fpro",
}
COMPETITIONS = {
    "Rugby_Womens_PWR": ("english women's premiership",),
    "Rugby_Mens_Nations": ("nations championship",),
    "Rugby_Womens_WXV": ("wxv womens series", "wxv women's series", "wxv"),
    "Rugby_Mens_6N": ("six nations", "men's six nations"),
    "Rugby_Womens_6N": ("women's six nations", "womens six nations"),
    "Rugby_Mens_Prem": ("english premiership", "english premiership rugby"),
    "Rugby_Mens_Intl": ("rugby union friendly", "international friendlies"),
    "Rugby_Womens_Intl": ("women's rugby union friendly", "womens internationals"),
    "Rugby_Mens_World_Cup": ("rugby world cup",),
    "Rugby_Womens_World_Cup": ("women's rugby world cup",),
}
TEAM_ALIASES = {
    "united states": "usa",
    "united states of america": "usa",
    "ealing trailfinders": "trailfinders",
    "trailfinders women": "trailfinders",
    "loughborough lightning": "loughborough",
    "gloucester hartpury": "gloucester hartpury",
    "bristol bears women": "bristol bears",
    "sale sharks women": "sale sharks",
    "saracens women": "saracens",
    "harlequins women": "harlequins",
    "leicester tigers women": "leicester tigers",
    "exeter chiefs women": "exeter chiefs",
}


def clean(value):
    return SPACES.sub(" ", html.unescape(TAG.sub(" ", value or ""))).strip()


def normalize_team(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9 ]", " ", value.casefold())
    value = SPACES.sub(" ", value).strip()
    return TEAM_ALIASES.get(value, value)


def get_bytes(url, *, timeout=25, max_bytes=2_000_000):
    if not url.startswith(("https://www.world.rugby/", "https://www.thepwr.com/",
                           "https://rugby.icdb.tv/")):
        raise ValueError("Unexpected source URL")
    request = Request(url, headers={
        "User-Agent": "mbligh-pvr-crew/1.0 (+https://github.com/mbligh/pvr)",
        "Accept": "text/html, application/xml, application/json;q=0.9",
    })
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Source response exceeds size limit")
    return raw


def parse_world_officials(page):
    """Read the labelled Match Officials table, never the header referee name."""
    if "Match Officials" not in page:
        return {}
    block = page.split("Match Officials", 1)[1][:18000]
    result = {}
    rows = re.findall(
        r'<div class="mc-info-table__list-title">\s*(.*?)\s*</div>'
        r'.{0,1000}?<div class="mc-info-table__sub-list-item">\s*(.*?)\s*</div>',
        block, re.S,
    )
    for role, name in rows:
        key = ROLES.get(clean(role).casefold())
        name = clean(name)
        if key and name and len(name) <= 120:
            result[key] = name
    # Some appointments list a single official as both TMO and FPRO.
    return result


def parse_pwr_officials(fixture):
    roles = {}
    for official in fixture.get("officials") or []:
        role = ROLES.get(str(official.get("role") or "").casefold().strip())
        name = str(official.get("name") or "").strip()
        if role and 1 <= len(name) <= 120:
            roles[role] = name
    return roles


def pwr_season_id(episode):
    # PWR fixtures API and the existing calendar source use YYYY01.
    season = episode.get("season")
    return int(season) * 100 + 1 if isinstance(season, int) else None


def parse_icdb_sitemap(xml):
    root = ElementTree.fromstring(xml)
    entries = []
    for entry in root.findall("{*}url"):
        loc = entry.findtext("{*}loc") or ""
        lastmod = entry.findtext("{*}lastmod") or ""
        if not re.fullmatch(r"https://rugby\.icdb\.tv/match/\d+-[A-Za-z0-9%-]+", loc):
            continue
        try:
            day = dt.date.fromisoformat(lastmod[:10])
        except ValueError:
            continue
        entries.append((loc, day))
    return entries


def parse_icdb_match(page, url):
    """Match title and kickoff are required before accepting a broadcast crew."""
    title = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    if not title:
        return None
    m = re.fullmatch(
        r"(.*?) v (.*?) Commentators - (.*?) - (\d{2}/\d{2}/\d{4}) - ICDb",
        clean(title.group(1)),
    )
    stamp = re.search(r'id="fixture-correction-container"[^>]*data-fixture-ts="(\d+)"', page)
    if not (m and stamp):
        return None
    moment = dt.datetime.fromtimestamp(int(stamp.group(1)), tz=UTC)
    if moment.date() != dt.datetime.strptime(m.group(4), "%d/%m/%Y").date() and abs(
            (moment.date() - dt.datetime.strptime(m.group(4), "%d/%m/%Y").date()).days) > 1:
        return None
    broadcasts = []
    for match in re.finditer(
            r'<div class="broadcast-card">(.+?)(?=<div class="broadcast-card">|'
            r'<div id="fixture-correction-container"|'
            r'<a href="https://rugby.icdb.tv/add\?)', page, re.S):
        section = match.group(1)
        channel_match = re.search(r'<div class="broadcast-title">.*?<h3>(.*?)</h3>',
                                  section, re.S)
        if not channel_match:
            continue
        channel = clean(channel_match.group(1))
        people = []
        for role, name, lang in re.findall(
                r'<div class="role-row">\s*'
                r'<div class="role-col-role">\s*(.*?)\s*</div>\s*'
                r'<div class="role-col-name">\s*<a[^>]*>(.*?)</a>.*?'
                r'<div class="role-col-lang">\s*(.*?)\s*</div>',
                section, re.S):
            person = clean(name)
            role = clean(role)
            if not (person and role):
                continue
            people.append({"role": role, "name": person, "language": clean(lang)})
        if people and channel:
            broadcasts.append({"channel": channel, "people": people,
                               "source": "icdb", "url": url})
    if not broadcasts:
        return None
    return {"home": m.group(1), "away": m.group(2),
            "competition": m.group(3), "scheduled": moment,
            "broadcasts": broadcasts}


def same_game(episode, match):
    expected = COMPETITIONS.get(episode.get("series_id"), ())
    if match["competition"].casefold() not in expected:
        return False
    if (normalize_team(episode.get("home")) != normalize_team(match["home"])
            or normalize_team(episode.get("away")) != normalize_team(match["away"])):
        return False
    scheduled = episode.get("scheduled")
    if not isinstance(scheduled, dt.datetime):
        return False
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=UTC)
    return abs((scheduled - match["scheduled"]).total_seconds()) <= 4 * 3600


def merge_broadcasts(previous, new):
    """Preserve feeds not seen in this scrape; replace only exact channel entries."""
    found = {x["channel"].casefold(): x for x in previous if isinstance(x, dict)
             and isinstance(x.get("channel"), str)}
    for item in new:
        found[item["channel"].casefold()] = item
    return sorted(found.values(), key=lambda x: x["channel"].casefold())


def emit(data):
    print(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str), flush=True)


def run(db, *, start, end, series=None, sources=None, apply=False,
        limit=0, max_icdb=60, fetch=get_bytes, delay=0.1):
    sources = set(sources or ("world-rugby", "pwr", "icdb"))
    query = {"scheduled": {"$gte": dt.datetime.combine(start, dt.time.min, tzinfo=UTC),
                           "$lt": dt.datetime.combine(end + dt.timedelta(days=1),
                                                        dt.time.min, tzinfo=UTC)},
             "status": {"$nin": ["cancelled", "canceled", "superseded"]}}
    if series:
        query["series_id"] = {"$in": series}
    episodes = list(db.episodes.find(query))
    episodes.sort(key=lambda ep: (ep.get("scheduled") or dt.datetime.min, ep["_id"]))
    if limit:
        episodes = episodes[:limit]
    by_id = {ep["_id"]: ep for ep in episodes}
    changes = defaultdict(dict)
    counters = defaultdict(int)

    def retrieve(url):
        counters["requests"] += 1
        try:
            data = fetch(url)
            if delay:
                time.sleep(delay)
            return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            emit({"type": "source_error", "url": url,
                  "error": type(exc).__name__, "message": str(exc)[:160]})
            counters["failed_requests"] += 1
            return None

    if "world-rugby" in sources:
        for ep in episodes:
            if ep.get("series_id") not in WORLD_SERIES:
                continue
            matched = UUID.search(ep["_id"])
            if not matched:
                continue
            url = WORLD_URL + quote(matched.group(1))
            content = retrieve(url)
            if content is None:
                continue
            officials = parse_world_officials(content.decode("utf-8", errors="replace"))
            if officials:
                officials.update({"source": "world_rugby", "url": url})
                if ep.get("match_officials") != officials:
                    changes[ep["_id"]]["match_officials"] = officials
                counters["world_rugby_matches"] += 1

    if "pwr" in sources:
        years = sorted({pwr_season_id(ep) for ep in episodes
                        if ep.get("series_id") == PWR_SERIES and pwr_season_id(ep)})
        for sid in years:
            url = PWR_API + str(sid)
            raw = retrieve(url)
            if raw is None:
                continue
            try:
                fixtures = json.loads(raw).get("data", [])
                if not isinstance(fixtures, list):
                    raise ValueError("Invalid PWR fixtures payload")
            except (ValueError, TypeError, AttributeError):
                emit({"type": "source_error", "url": url, "error": "Invalid JSON"})
                continue
            for fixture in fixtures:
                eid = f"{PWR_SERIES}:pwr-{fixture.get('id')}@mbligh.github.io"
                ep = by_id.get(eid)
                if ep is None:
                    continue
                officials = parse_pwr_officials(fixture)
                if officials:
                    officials.update({"source": "pwr", "url": url, "fixture_id": fixture["id"]})
                    if ep.get("match_officials") != officials:
                        changes[eid]["match_officials"] = officials
                    counters["pwr_matches"] += 1

    if "icdb" in sources:
        raw = retrieve(ICDB_SITEMAP)
        if raw is not None:
            try:
                links = parse_icdb_sitemap(raw)
            except ElementTree.ParseError:
                links = []
                emit({"type": "source_error", "url": ICDB_SITEMAP,
                      "error": "Invalid sitemap"})
            # Sitemap lastmod is a discovery hint, not proof of the fixture's date.
            # Fetch only pages likely to match one of the requested events.
            candidates = []
            for url, day in links:
                if not (start - dt.timedelta(days=7) <= day <=
                        end + dt.timedelta(days=7)):
                    continue
                slug = url.split("/", 4)[-1]
                pieces = re.split(r"-v-", slug, maxsplit=1)
                if len(pieces) != 2:
                    continue
                slug_teams = [normalize_team(re.sub(r"^\d+-", "", pieces[0]).replace("-", " ")),
                              normalize_team(pieces[1].replace("-", " "))]
                matches = [ep for ep in episodes if
                           normalize_team(ep.get("home")) == slug_teams[0] and
                           normalize_team(ep.get("away")) == slug_teams[1]]
                if matches:
                    candidates.append((url, matches))
            for url, possible in candidates[:max_icdb]:
                data = retrieve(url)
                if data is None:
                    continue
                match = parse_icdb_match(data.decode("utf-8", errors="replace"), url)
                if not match:
                    continue
                accepted = [ep for ep in possible if same_game(ep, match)]
                if len(accepted) != 1:
                    counters["icdb_ambiguous"] += 1
                    continue
                ep = accepted[0]
                existing = changes[ep["_id"]].get("broadcast_commentary",
                           ep.get("broadcast_commentary") or [])
                merged = merge_broadcasts(existing, match["broadcasts"])
                if merged != (ep.get("broadcast_commentary") or []):
                    changes[ep["_id"]]["broadcast_commentary"] = merged
                counters["icdb_matches"] += 1
            counters["icdb_candidate_pages"] = len(candidates)

    for eid, fields in changes.items():
        if not fields:
            continue
        if apply:
            result = db.episodes.update_one(
                {"_id": eid, "series_id": by_id[eid]["series_id"]}, {"$set": fields})
            if result.matched_count != 1:
                raise RuntimeError("Episode disappeared while enriching: " + eid)
        emit({"type": "enrichment", "episode_id": eid,
              "mode": "apply" if apply else "preview",
              "fields": sorted(fields),
              "referee": fields.get("match_officials", {}).get("referee"),
              "broadcasts": len(fields.get("broadcast_commentary", []))})
    emit({"type": "summary", "mode": "apply" if apply else "preview",
          "episodes_checked": len(episodes),
          "episodes_changed": sum(bool(fields) for fields in changes.values()),
          **dict(counters)})
    return counters


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Update existing episodes")
    parser.add_argument("--start", type=dt.date.fromisoformat, help="UTC start YYYY-MM-DD")
    parser.add_argument("--end", type=dt.date.fromisoformat, help="UTC end YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=30, help="Default lookback days")
    parser.add_argument("--ahead", type=int, default=7, help="Default future days")
    parser.add_argument("--series", action="append", help="Only this canonical series (repeat)")
    parser.add_argument("--source", action="append",
                        choices=("world-rugby", "pwr", "icdb"),
                        help="Only this source (repeat)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max episodes (0 = unlimited)")
    parser.add_argument("--max-icdb", type=int, default=60,
                        help="Max ICDb match pages per run")
    parser.add_argument("--database", default="pvr", help="MongoDB database")
    args = parser.parse_args(argv)
    today = dt.datetime.now(UTC).date()
    start = args.start or today - dt.timedelta(days=args.days)
    end = args.end or today + dt.timedelta(days=args.ahead)
    if start > end or args.days < 0 or args.ahead < 0 or args.limit < 0 or args.max_icdb < 0:
        parser.error("Invalid date range or limits")
    with database_connection(args.database) as client:
        client.admin.command("ping")
        run(client[args.database], start=start, end=end,
            series=args.series, sources=args.source, apply=args.apply,
            limit=args.limit, max_icdb=args.max_icdb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
