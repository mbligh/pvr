"""Official, score-free Guinness Men's Six Nations fixtures (2026 onward).

Read the public server-rendered fixture cards at sixnationsrugby.com. Their
Next.js payload also provides UTC kickoffs for upcoming matches. Completed
2026 cards omit kickoff timestamps; their link slugs contain venue-local
times, used only for that explicitly requested historical season.

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
MIN_YEAR = 2026
UTC = dt.timezone.utc
TEAMS = {"england": "England", "france": "France", "ireland": "Ireland",
         "italy": "Italy", "scotland": "Scotland", "wales": "Wales"}
HOME_ZONES = {"france": "Europe/Paris", "italy": "Europe/Rome",
              "ireland": "Europe/Dublin"}
LINK = re.compile(
    r"^/en/m6n/fixtures/(?P<season>\d{4})00/"
    r"(?P<home>[a-z]+)-v-(?P<away>[a-z]+)-"
    r"(?P<date>\d{8})-(?P<time>\d{4})/(?:build-up|report)/?$"
)
TITLE = re.compile(
    r"^([A-Za-z]+) v ([A-Za-z]+) - Six Nations Rugby - "
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


def _rsc_kickoffs(scripts):
    """Extract timestamp/link pairs from React Server Component fixture cards.

    We deliberately parse the structured JSON tree rather than matching a
    date near unrelated score or article text. RSC format is optional for
    completed 2026 games, but mandatory for future kickoffs.
    """
    kickoffs = {}
    for script in scripts:
        for item in script.split("self.__next_f.push(")[1:]:
            encoded = item.split(");", 1)[0]
            try:
                frame = json.loads(encoded)
                if frame[0] != 1 or not isinstance(frame[1], str):
                    continue
                payload = json.loads(frame[1].split(":", 1)[1])
            except (ValueError, IndexError, TypeError):
                continue
            for node in _walk(payload):
                if not (isinstance(node, dict) and
                        node.get("data-testid") == "fixture-card"):
                    continue
                hrefs = set()
                dates = set()
                for nested in _walk(node):
                    if not isinstance(nested, dict):
                        continue
                    if (nested.get("data-testid") ==
                            "fixture-card__match-centre-link"):
                        hrefs.add(urllib.parse.urlsplit(nested.get("href", "")).path)
                    if nested.get("format") == "HH:mm" and nested.get("date"):
                        dates.add(nested["date"])
                if len(hrefs) == 1 and len(dates) == 1:
                    href = hrefs.pop()
                    value = dt.datetime.fromisoformat(dates.pop().replace("Z", "+00:00"))
                    if value.tzinfo is None:
                        raise ValueError("Naive Six Nations kickoff in RSC")
                    if href in kickoffs and kickoffs[href] != value:
                        raise ValueError("Conflicting Six Nations kickoffs")
                    kickoffs[href] = value.astimezone(UTC)
    return kickoffs


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


def convert_mens(html, *, year):
    """Convert one official page into 15 safe events; reject incomplete pages."""
    if year < MIN_YEAR:
        raise ValueError("Six Nations history before 2026 is not supported")
    page = _FixturePage()
    page.feed(html)
    kickoffs = _rsc_kickoffs(page.scripts)
    matches = []
    ids = set()
    for card in page.cards:
        href = urllib.parse.urlsplit(card.get("href") or "").path
        match = LINK.fullmatch(href)
        title = TITLE.fullmatch(card.get("title") or "")
        if not match or not title or int(match["season"]) != year:
            raise ValueError("Unexpected official Six Nations fixture link")
        home, away = match["home"], match["away"]
        if (home not in TEAMS or away not in TEAMS or home == away or
                (title[1].casefold(), title[2].casefold()) != (home, away)):
            raise ValueError("Unexpected Six Nations teams")
        day = dt.datetime.strptime(match["date"], "%d%m%Y").date()
        title_day = dt.datetime.strptime(title[3], "%d %B %Y").date()
        if day != title_day or day.year != year:
            raise ValueError("Six Nations fixture date disagrees with page")
        if href in ids:
            raise ValueError("Duplicate Six Nations fixture link")
        ids.add(href)
        kickoff = kickoffs.get(href)
        if kickoff is None:
            if year != 2026:
                raise ValueError("Missing official UTC kickoff: " + href)
            # Only for completed 2026 fixtures: these pages now display
            # scores instead of the pre-match UTC time. The URL encodes
            # the originally published venue-local kick-off.
            clock = dt.datetime.strptime(match["time"], "%H%M").time()
            zone = ZoneInfo(HOME_ZONES.get(home, "Europe/London"))
            kickoff = dt.datetime.combine(day, clock, tzinfo=zone).astimezone(UTC)
        elif abs((kickoff.date() - day).days) > 1:
            raise ValueError("Official fixture timestamp/date mismatch")
        round_no = card["round"]
        if not isinstance(round_no, int) or not 1 <= round_no <= 5:
            raise ValueError("Missing official Six Nations round")
        matches.append({
            "home": TEAMS[home], "away": TEAMS[away], "round": round_no,
            "kickoff": kickoff, "venue": card["venue"].strip(),
            "uid": f"sixnations-m6n-{year}-{home}-{away}@mbligh.github.io",
        })
    _validate(matches)
    calendar = Calendar()
    calendar.add("prodid", "-//mbligh/pvr//Official Men's Six Nations//EN")
    calendar.add("version", "2.0")
    for match in sorted(matches, key=lambda m: (m["kickoff"], m["uid"])):
        event = Event()
        event.add("uid", match["uid"])
        event.add("summary", f"{match['home']} vs {match['away']}")
        event.add("dtstart", match["kickoff"])
        event.add("dtend", match["kickoff"] + dt.timedelta(hours=2))
        event.add("description", f"Men's Six Nations - Round {match['round']}")
        event.add("x-six-nations-round", match["round"])
        if match["venue"]:
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


def fetch_mens(*, now):
    """Fetch current and following calendar years, never before 2026.

    An as-yet-unpublished next season may be absent. A partially published
    season fails validation rather than silently deleting/renumbering matches.
    Each run performs at most two requests.
    """
    current = max(MIN_YEAR, now.astimezone(UTC).year)
    combined = Calendar()
    combined.add("prodid", "-//mbligh/pvr//Official Men's Six Nations//EN")
    combined.add("version", "2.0")
    for year in (current, current + 1):
        url = BASE + f"{year}00"
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
        # Next year's site may resolve successfully to an unpopulated page.
        if year > current and 'data-testid="fixture-card"' not in page:
            continue
        season = convert_mens(page, year=year)
        for event in season.walk("VEVENT"):
            combined.add_component(event)
    if not combined.walk("VEVENT"):
        raise ValueError("No official Six Nations fixtures")
    return combined
