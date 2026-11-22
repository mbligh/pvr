"""Fetch and merge score-free rugby calendar events.

Source fixtures may originate from public ICS feeds or official rugby APIs.

Existing events are frozen at their last accepted DTSTART. A source update
whose LAST-MODIFIED/DTSTAMP is later than kickoff is never accepted, even if
the updater first encounters it after the match. Missing timestamps cannot
prove safety; an event is only updated while its saved kickoff is future.
"""
import datetime as dt
import urllib.request

from icalendar import Calendar
UTC = dt.timezone.utc


def start_time(event, default_tz):
    if "DTSTART" not in event:
        return None
    value = event.decoded("DTSTART")
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=default_tz) if value.tzinfo is None else value
    return dt.datetime.combine(value, dt.time.min, tzinfo=default_tz)


def event_key(event):
    uid = str(event.get("UID", ""))
    recurrence = event.get("RECURRENCE-ID")
    return (uid, str(recurrence.to_ical()) if recurrence else "")


def modified_time(event):
    values = []
    for prop in ("LAST-MODIFIED", "DTSTAMP"):
        if prop in event:
            value = event.decoded(prop)
            if isinstance(value, dt.datetime):
                values.append(value if value.tzinfo else value.replace(tzinfo=UTC))
    return max(values) if values else None


def events(calendar):
    return {event_key(c): c for c in calendar.walk("VEVENT") if event_key(c)[0]}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "calendar-spoiler-filter/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        data = response.read()
    cal = Calendar.from_ical(data)
    if not any(c.name == "VEVENT" for c in cal.walk()):
        raise ValueError("No VEVENT entries found; refusing to replace existing output")
    return cal


def merge(source, previous, now, tz, name):
    old = events(previous) if previous else {}
    current = events(source)
    accepted = dict(old)
    # An explicit World Rugby cancellation is authoritative even after kickoff;
    # merely absent source fixtures are deliberately retained.
    cancelled = source.get("x-wr-cancelled-uid", [])
    if not isinstance(cancelled, list):
        cancelled = [cancelled]
    for uid in cancelled:
        accepted.pop((str(uid), ""), None)
    for key, incoming in current.items():
        start = start_time(incoming, tz)
        if start is None:
            continue
        saved = old.get(key)
        cutoff = start_time(saved, tz) if saved is not None else start
        # Never import past events for the first time, or modify an event
        # after its previously accepted kickoff, even if source reschedules it.
        if cutoff <= now:
            continue
        modified = modified_time(incoming)
        # Reject a source revision timestamped after the saved kickoff.
        # For new events, use their source kickoff as the cutoff.
        if modified is not None and modified > cutoff:
            continue
        accepted[key] = incoming

    result = Calendar()
    result.add("prodid", "-//mbligh/calendar//Spoiler-free calendar//EN")
    result.add("version", "2.0")
    result.add("x-wr-calname", name + " (No Spoilers)")
    result.add("x-wr-timezone", str(tz))
    # Carry explicit cancellation evidence into the scheduler snapshot.
    # Without this, removed VEVENTs cannot be distinguished from merely
    # missing source fixtures when updating Atlas episode status.
    for uid in cancelled:
        result.add("x-wr-cancelled-uid", str(uid))
    # Retain timezone definitions for event DTSTART with TZID parameters.
    # The previous output may reference older timezone definitions.
    seen = set()
    for cal in (source, previous):
        if cal is None:
            continue
        for component in cal.subcomponents:
            if component.name == "VTIMEZONE":
                tzid = str(component.get("TZID", ""))
                if tzid not in seen:
                    result.add_component(component)
                    seen.add(tzid)
    for key in sorted(accepted):
        result.add_component(accepted[key])
    return result
