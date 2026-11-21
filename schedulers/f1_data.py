#!/usr/bin/env python3
"""Fetch the official F1 season meeting list and each meeting's session timetable.

Requires only Python 3's standard library. Output is one JSON object; progress
and errors go to stderr. The Formula 1 public website exposes the API key used
by its browser client, so there is no embedded key or account login.

Examples:
    ./f1_data > f1-2026.json
    ./f1_data --year 2026 --output /tmp/f1-2026.json
    ./f1_data --meeting 1279 | jq '.meetings[0].sessions'
    F1_API_KEY=... ./f1_data --limit 2
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SITE = "https://www.formula1.com"
API = "https://api.formula1.com"
LISTING_PATH = "/v1/editorial-eventlisting/events"
MEETING_PATH = "/v1/event-tracker/meeting/"
KEY_PATTERN = re.compile(
    r"NEXT_PUBLIC_GLOBAL_BROADCAST_APIKEY\\?[\"']\s*:\s*\\?[\"']([A-Za-z0-9_-]{16,})"
)
USER_AGENT = "Mozilla/5.0 (compatible; f1_data/1.0)"


def fetch(url, headers, timeout):
    """Request bytes, retrying temporary HTTP failures."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {exc.code} fetching {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot fetch {url}: {exc.reason}") from exc


def extract_api_key(html):
    match = KEY_PATTERN.search(html)
    if not match:
        raise ValueError("NEXT_PUBLIC_GLOBAL_BROADCAST_APIKEY not found in page HTML")
    return match.group(1)


def discover_api_key(year, timeout):
    page = f"{SITE}/en/racing/{year}"
    html = fetch(page, {"Accept": "text/html"}, timeout).decode("utf-8", errors="replace")
    return extract_api_key(html)


def api_json(path, api_key, timeout):
    url = API + path
    data = fetch(url, {
        "Accept": "application/json",
        "apikey": api_key,
        "locale": "en",
        "Origin": SITE,
        "Referer": SITE + "/",
    }, timeout)
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON from {url}") from exc


def collect(year, api_key, timeout, meeting_keys=None, limit=None):
    """Join the full listing with every requested meeting's complete tracker JSON."""
    listing = api_json(LISTING_PATH, api_key, timeout)
    if not isinstance(listing, dict) or not isinstance(listing.get("events"), list):
        raise ValueError("Event listing response does not contain an events array")
    actual_year = str(listing.get("seasonFromEventListing") or listing.get("year") or "")
    if actual_year != str(year):
        raise ValueError(
            f"Event listing returned season {actual_year or 'unknown'}, not {year}; "
            "the API may only return the current season"
        )

    events = listing["events"]
    if meeting_keys:
        found = {str(event.get("meetingKey")) for event in events}
        missing = set(meeting_keys) - found
        if missing:
            raise ValueError(f"Meeting key(s) not found in listing: {', '.join(sorted(missing))}")
        events = [e for e in events if str(e.get("meetingKey")) in meeting_keys]
    if limit is not None:
        events = events[:limit]

    meetings = []
    for index, event in enumerate(events, 1):
        key = str(event.get("meetingKey") or "")
        if not re.fullmatch(r"\d+", key):
            raise ValueError(f"Missing or invalid meetingKey for event {event!r}")
        print(f"[{index}/{len(events)}] {key}: {event.get('meetingName', '?')}", file=sys.stderr)
        detail = api_json(MEETING_PATH + key, api_key, timeout)
        if not isinstance(detail, dict):
            raise ValueError(f"Meeting {key}: detail response is not an object")
        context = detail.get("meetingContext")
        if not isinstance(context, dict) or not isinstance(context.get("timetables"), list):
            raise ValueError(f"Meeting {key}: missing meetingContext.timetables")
        if str(context.get("meetingKey")) != key:
            raise ValueError(f"Meeting {key}: detail returned meetingKey {context.get('meetingKey')}")
        meetings.append({
            "event": event,                 # Unmodified entry from event listing
            "sessions": context["timetables"],
            "detail": detail,               # Complete, unmodified meeting API response
        })

    return {
        "season": actual_year,
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources": {
            "eventListing": API + LISTING_PATH,
            "meetingDetail": API + MEETING_PATH + "{meetingKey}",
        },
        "listing": {k: v for k, v in listing.items() if k != "events"},
        "meetings": meetings,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=dt.datetime.now(dt.timezone.utc).year,
                        help="Expected season and key-discovery page (default: current year)")
    parser.add_argument("--meeting", action="append", dest="meeting_keys", metavar="KEY",
                        help="Fetch just this meetingKey (repeatable)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Fetch at most N meetings, for quick testing")
    parser.add_argument("--timeout", type=float, default=25, metavar="SECONDS")
    parser.add_argument("--api-key", default=os.environ.get("F1_API_KEY"),
                        help="Override HTML key discovery (also F1_API_KEY)")
    parser.add_argument("--output", "-o", type=Path,
                        help="Write JSON to file (default: stdout)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    try:
        api_key = args.api_key or discover_api_key(args.year, args.timeout)
        data = collect(args.year, api_key, args.timeout,
                       meeting_keys=args.meeting_keys, limit=args.limit)
        output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
        print(f"Fetched {len(data['meetings'])} meeting(s), "
              f"{sum(len(m['sessions']) for m in data['meetings'])} session(s)",
              file=sys.stderr)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"f1_data: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
