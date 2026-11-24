"""Official, score-free men's and women's Six Nations fixtures (2026 onward).

Read the public server-rendered fixture cards at sixnationsrugby.com. Their
Next.js payload also provides UTC kickoffs for upcoming matches. Completed
cards omit kickoff timestamps; their report link slugs contain kickoff times,
venue-local for men and UTC for women. Previously accepted past events are
frozen by the calendar merge.

No results, scores, standings, or match-report data are copied into events.
"""
import datetime as dt
import html.parser
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

BASE = "https://www.sixnationsrugby.com/en/m6n/fixtures/"
WOMENS_BASE = "https://www.sixnationsrugby.com/en/w6n/fixtures/"
COMPETITIONS = {"m6n": (BASE, "Men's"), "w6n": (WOMENS_BASE, "Women's")}
MIN_YEAR = 2026
UTC = dt.timezone.utc
TEAMS = {"england": "England", "france": "France", "ireland": "Ireland",
         "italy": "Italy", "scotland": "Scotland", "wales": "Wales"}
HOME_ZONES = {"france": "Europe/Paris", "italy": "Europe/Rome",
              "ireland": "Europe/Dublin"}
LINK = re.compile(
    r"^/en/(?P<competition>m6n|w6n)/fixtures/(?P<season>\d{4})00/"
    r"(?P<home>[a-z]+)(?P<home_suffix>-women)?-v-"
    r"(?P<away>[a-z]+)(?P<away_suffix>-women)?-"
    r"(?P<date>\d{8})-(?P<time>\d{4})/(?P<stage>build-up|report)/?$"
)
TITLE = re.compile(
    r"^([A-Za-z]+) v ([A-Za-z]+) - (Women's )?Six Nations Rugby - "
    r"(\d{2} [A-Za-z]+ \d{4})$"
)
VERSUS = re.compile(r"\s+(?:vs?\.?|versus)\s+", re.I)


class _FixturePage(html.parser.HTMLParser):
    """Read only fixture cards, not their score/half-time/status children."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.round = None
        self.card = None
        self.stadium = False
        self.cards = []
        self.scripts = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        props = dict(attrs)
        if tag == "script":
            self.script = []
        if tag == "div" and (props.get("id") or "").startswith("roundNumber-"):
            number = props["id"].removeprefix("roundNumber-")
            self.round = int(number) if number.isdecimal() else None
        if tag == "article" and props.get("data-testid") == "fixture-card":
            if self.card is not None:
                raise ValueError("Nested Six Nations fixture cards")
            self.card = {"round": self.round, "venue": ""}
        if self.card is not None:
            if tag == "div" and "fixturesResultsCard_stadium" in props.get("class", ""):
                self.stadium = True
            if tag == "a" and props.get("data-testid") == "fixture-card__match-centre-link":
                if self.card.get("href"):
                    raise ValueError("Duplicate Six Nations match-centre link")
                self.card["href"] = props.get("href")
                self.card["title"] = props.get("title")

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.stadium and self.card is not None:
            self.card["venue"] += data

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if tag == "div":
            self.stadium = False
        if tag == "article" and self.card is not None:
            self.cards.append(self.card)
            self.card = None


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _rsc_cards(scripts):
    """Extract complete fixture cards directly from public Next.js RSC data.

    Direct HTTP GETs currently render the fixture list *only* in the
    embedded server-component scripts, whereas a hydrated browser also
    produces HTML articles. Both paths yield the same score-free fields.
    """
    cards = []

    def visit(node, round_no=None):
        if isinstance(node, list):
            for item in node:
                visit(item, round_no)
            return
        if not isinstance(node, dict):
            return
        identifier = node.get("id")
        if isinstance(identifier, str) and identifier.startswith("roundNumber-"):
            label = identifier.removeprefix("roundNumber-")
            round_no = int(label) if label.isdecimal() else None
        if node.get("data-testid") == "fixture-card":
            links = []
            dates = set()
            venue = ""
            for child in _walk(node):
                if not isinstance(child, dict):
                    continue
                if child.get("data-testid") == "fixture-card__match-centre-link":
                    links.append(child)
                if ("fixturesResultsCard_stadium" in child.get("className", "")
                        and isinstance(child.get("children"), str)):
                    venue = child["children"]
                if child.get("format") == "HH:mm" and child.get("date"):
                    dates.add(child["date"])
            if len(links) != 1 or len(dates) > 1:
                raise ValueError("Ambiguous Six Nations server fixture card")
            kickoff = None
            if dates:
                kickoff = dt.datetime.fromisoformat(dates.pop().replace("Z", "+00:00"))
                if kickoff.tzinfo is None:
                    raise ValueError("Naive official Six Nations kickoff")
            cards.append({
                "round": round_no, "venue": venue,
                "href": links[0].get("href"), "title": links[0].get("title"),
                "kickoff": kickoff.astimezone(UTC) if kickoff else None,
            })
            return
        for child in node.values():
            visit(child, round_no)

    for script in scripts:
        for item in script.split("self.__next_f.push(")[1:]:
            # Next.js uses push([...]) with or without a semicolon.
            encoded = item.rsplit(")", 1)[0].rstrip(";")
            try:
                frame = json.loads(encoded)
                if frame[0] != 1 or not isinstance(frame[1], str):
                    continue
                payload = json.loads(frame[1].split(":", 1)[1])
            except (ValueError, IndexError, TypeError):
                continue
            visit(payload)
    return cards


def _validate(matches):
    """Require the official complete five-round, six-team round robin."""
    if len(matches) != 15:
        raise ValueError(f"Incomplete Six Nations season: {len(matches)}/15 fixtures")
    if Counter(m["round"] for m in matches) != {r: 3 for r in range(1, 6)}:
        raise ValueError("Invalid Six Nations round counts")
    pairs = set()
    for round_no in range(1, 6):
        games = [m for m in matches if m["round"] == round_no]
        participants = [team for m in games for team in (m["home"], m["away"])]
        if set(participants) != set(TEAMS.values()) or len(set(participants)) != 6:
            raise ValueError(f"Invalid Six Nations round {round_no} participants")
        pairs.update(frozenset((m["home"], m["away"])) for m in games)
    if len(pairs) != 15:
        raise ValueError("Repeated or missing Six Nations team pairing")


def convert_fixtures(html, *, year, competition):
    """Convert one official page into 15 safe events; reject incomplete pages."""
    if competition not in COMPETITIONS:
        raise ValueError("Unknown Six Nations competition")
    _, label = COMPETITIONS[competition]
    womens = competition == "w6n"
    if year < MIN_YEAR:
        raise ValueError("Six Nations history before 2026 is not supported")
    page = _FixturePage()
    page.feed(html)
    cards = _rsc_cards(page.scripts) or page.cards
    matches = []
    ids = set()
    for card in cards:
        href = urllib.parse.urlsplit(card.get("href") or "").path
        match = LINK.fullmatch(href)
        raw_title = card.get("title")
        title = TITLE.fullmatch(raw_title or "")
        if (not match or int(match["season"]) != year or
                match["competition"] != competition or
                bool(match["home_suffix"]) != womens or
                bool(match["away_suffix"]) != womens):
            raise ValueError("Unexpected official Six Nations fixture link")
        home, away = match["home"], match["away"]
        if home not in TEAMS or away not in TEAMS or home == away:
            raise ValueError("Unexpected Six Nations teams")
        day = dt.datetime.strptime(match["date"], "%d%m%Y").date()
        # The 2027 women's cards have no title while venues are TBD.
        # The competition-specific URL and full UTC timestamp still agree.
        if title:
            title_day = dt.datetime.strptime(title[4], "%d %B %Y").date()
            if (bool(title[3]) != womens or
                    (title[1].casefold(), title[2].casefold()) != (home, away) or
                    day != title_day):
                raise ValueError("Six Nations fixture title disagrees with link")
        elif not (womens and raw_title in (None, "", "$undefined") and card.get("kickoff")):
            raise ValueError("Missing or invalid official Six Nations fixture title")
        if day.year != year:
            raise ValueError("Six Nations fixture date disagrees with page")
        if href in ids:
            raise ValueError("Duplicate Six Nations fixture link")
        ids.add(href)
        kickoff = card.get("kickoff")
        if kickoff is None:
            if match["stage"] != "report":
                raise ValueError("Missing official UTC kickoff: " + href)
            # Completed fixtures may drop the explicit time from the card.
            # Report URLs retain the kickoff: UTC for women, venue-local for
            # men. Never infer an upcoming fixture time from its URL.
            clock = dt.datetime.strptime(match["time"], "%H%M").time()
            zone = UTC if womens else ZoneInfo(HOME_ZONES.get(home, "Europe/London"))
            kickoff = dt.datetime.combine(day, clock, tzinfo=zone).astimezone(UTC)
        elif abs((kickoff.date() - day).days) > 1:
            raise ValueError("Official fixture timestamp/date mismatch")
        round_no = card["round"]
        if not isinstance(round_no, int) or not 1 <= round_no <= 5:
            raise ValueError("Missing official Six Nations round")
        matches.append({
            "home": TEAMS[home], "away": TEAMS[away], "round": round_no,
            "kickoff": kickoff, "venue": card["venue"].strip(),
            "uid": f"sixnations-{competition}-{year}-{home}-{away}@mbligh.github.io",
        })
    _validate(matches)
    calendar = Calendar()
    calendar.add("prodid", f"-//mbligh/pvr//Official {label} Six Nations//EN")
    calendar.add("version", "2.0")
    for match in sorted(matches, key=lambda m: (m["kickoff"], m["uid"])):
        event = Event()
        event.add("uid", match["uid"])
        suffix = " Women" if womens else ""
        event.add("summary", f"{match['home']}{suffix} vs {match['away']}{suffix}")
        event.add("dtstart", match["kickoff"])
        event.add("dtend", match["kickoff"] + dt.timedelta(hours=2))
        event.add("description", f"{label} Six Nations - Round {match['round']}")
        event.add("x-six-nations-round", match["round"])
        if match["venue"] and match["venue"].casefold() not in ("tbd", "tbc", "to be confirmed"):
            event.add("location", match["venue"])
        calendar.add_component(event)
    return calendar


def _team(text):
    matches = [name for name in TEAMS.values()
               if re.search(r"\b" + name + r"\b", text, re.I)]
    return matches[0] if len(matches) == 1 else None


def _key(event):
    from .filter import start_time
    start = start_time(event, UTC)
    parts = VERSUS.split(str(event.get("SUMMARY", "")), maxsplit=1)
    if start is None or len(parts) != 2:
        return None
    home, away = (_team(text) for text in parts)
    if not home or not away or home == away:
        return None
    return (start.astimezone(ZoneInfo("Europe/London")).year, home, away)


def preserve_uids(source, previous):
    """Keep third-party ICS UIDs already stored in Atlas across source switch.

    Each team pairing is unique in a Six Nations tournament year; don't
    re-identify by kickoff time, which may have been rescheduled.
    """
    known = {}
    for event in previous.walk("VEVENT"):
        key = _key(event)
        if key is None:
            continue
        uid = str(event.get("UID", "")).strip()
        if key in known and known[key] != uid:
            raise ValueError("Ambiguous existing Six Nations UID: " + str(key))
        known[key] = uid
    for event in source.walk("VEVENT"):
        uid = known.get(_key(event))
        if uid:
            event["UID"] = uid
    return source


def fetch_fixtures(*, now, competition):
    """Fetch current and following calendar years, never before 2026.

    An as-yet-unpublished next season may be absent. A partially published
    season fails validation rather than silently deleting/renumbering matches.
    Each run performs at most two requests.
    """
    base, label = COMPETITIONS[competition]
    current = max(MIN_YEAR, now.astimezone(UTC).year)
    combined = Calendar()
    combined.add("prodid", f"-//mbligh/pvr//Official {label} Six Nations//EN")
    combined.add("version", "2.0")
    for year in (current, current + 1):
        url = base + f"{year}00"
        request = urllib.request.Request(url, headers={
            "User-Agent": "mbligh-pvr/1.0",
            "Accept": "text/html",
        })
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                page = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if year > current and exc.code == 404:
                continue
            raise RuntimeError(f"Six Nations fixtures HTTP {exc.code} ({year})") from exc
        try:
            season = convert_fixtures(page, year=year, competition=competition)
        except ValueError as exc:
            # A future year may have a valid page shell before its fixtures
            # are published; do not treat a partly published season as safe.
            if year > current and str(exc) == "Incomplete Six Nations season: 0/15 fixtures":
                continue
            raise
        for event in season.walk("VEVENT"):
            combined.add_component(event)
    if not combined.walk("VEVENT"):
        raise ValueError("No official Six Nations fixtures")
    return combined


def convert_mens(html, *, year):
    return convert_fixtures(html, year=year, competition="m6n")


def convert_womens(html, *, year):
    return convert_fixtures(html, year=year, competition="w6n")


def fetch_mens(*, now):
    return fetch_fixtures(now=now, competition="m6n")


def fetch_womens(*, now):
    return fetch_fixtures(now=now, competition="w6n")
