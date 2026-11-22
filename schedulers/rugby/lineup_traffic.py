"""Bounded, source-specific HTTP traffic for rugby team-sheet imports.

This module does not issue HTTP requests itself. It spaces adapter.fetch calls,
stops on throttling responses, and stores provider cooldowns in Atlas so cron
invocations share the same backoff window.
"""
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import sys
import time
from urllib.error import HTTPError

UTC = dt.timezone.utc
DEFAULT_WORLD_LIMIT = 8
DEFAULT_OTHER_LIMIT = 12
DEFAULT_WORLD_DELAY = 10.0
DEFAULT_OTHER_DELAY = 3.0
# Server-provided Retry-After can extend, but never shorten, these intervals.
THROTTLE_SECONDS = {403: 24 * 3600, 429: 6 * 3600, 503: 3600}
STATE_PREFIX = "rugby_lineups_"


def utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def retry_after_seconds(value, now):
    """Support both Retry-After: 3600 and RFC-style HTTP dates."""
    if not value:
        return 0
    value = str(value).strip()
    if value.isdecimal():
        return int(value)
    try:
        return max(0, int((utc(parsedate_to_datetime(value)) - now).total_seconds()))
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0


def http_error(exc):
    """Walk exception causes; PREM wraps HTTPError in RuntimeError."""
    for _ in range(6):
        if isinstance(exc, HTTPError):
            return exc
        exc = getattr(exc, "__cause__", None)
        if exc is None:
            break
    return None


def cooldown(database, provider, now):
    doc = database.metadata_state.find_one({"_id": STATE_PREFIX + provider}) or {}
    until = utc(doc.get("retry_not_before"))
    return until if until and until > now else None


def save_cooldown(database, provider, status, now, retry_at):
    database.metadata_state.update_one(
        {"_id": STATE_PREFIX + provider},
        {"$max": {"retry_not_before": retry_at},
         "$set": {"last_http_status": status, "last_rate_limited_at": now}},
        upsert=True,
    )


def process(database, selected, *, apply=False, delays=None,
            output=None, error=None, clock=None, monotonic=None, sleep=None):
    """Run an already per-provider-capped selection, without new episode records.

    Preview is entirely read-only (including no HTTP and no cooldown writes).
    A 429/403/503 disables only the affected provider for the current run
    and persists a provider-specific cooldown across future runs.
    """
    delays = delays if delays is not None else {
        "world": DEFAULT_WORLD_DELAY,
        "prem": DEFAULT_OTHER_DELAY,
        "pwr": DEFAULT_OTHER_DELAY,
    }
    output = sys.stdout if output is None else output
    error = sys.stderr if error is None else error
    clock = clock or (lambda: dt.datetime.now(UTC))
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep

    blocked = {}
    last_request = {}
    calls = {"world": 0, "prem": 0, "pwr": 0}
    errors = 0
    updated_count = 0
    for row in selected:
        provider = row["provider"]
        adapter = row["module"]
        episode = row["episode"]
        fixture = row["fixture_id"]
        if not apply:
            print(json.dumps({
                "provider": provider, "series_id": episode["series_id"],
                "fixture_id": fixture, "episode_id": episode["_id"],
                "preview": True,
            }), file=output)
            continue

        if provider not in blocked:
            blocked[provider] = cooldown(database, provider, utc(clock()))
            if blocked[provider]:
                print(json.dumps({
                    "type": "provider_cooldown", "provider": provider,
                    "reason": "previous_throttle",
                    "retry_not_before": blocked[provider].isoformat(),
                }), file=error)
        if blocked[provider]:
            continue

        last = last_request.get(provider)
        if last is not None:
            delay = delays[provider] - (monotonic() - last)
            if delay > 0:
                sleep(delay)
        last_request[provider] = monotonic()
        calls[provider] += 1
        try:
            record = adapter.normalize(adapter.fetch(fixture), episode, now=utc(clock()))
            updated = adapter.upsert(database, record)
            updated_count += bool(updated)
            print(json.dumps({
                "provider": provider, "series_id": episode["series_id"],
                "fixture_id": fixture, "episode_id": episode["_id"],
                "status": record["status"],
                "home": len(record["home"]), "away": len(record["away"]),
                "updated": updated,
            }), file=output)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            errors += 1
            http = http_error(exc)
            if http is not None and http.code in THROTTLE_SECONDS:
                now = utc(clock())
                seconds = max(THROTTLE_SECONDS[http.code], retry_after_seconds(
                    http.headers.get("Retry-After") if http.headers else None, now))
                retry_at = now + dt.timedelta(seconds=min(seconds, 365 * 86400))
                save_cooldown(database, provider, http.code, now, retry_at)
                blocked[provider] = retry_at
                print(json.dumps({
                    "type": "provider_cooldown", "provider": provider,
                    "http_status": http.code,
                    "retry_not_before": retry_at.isoformat(),
                }), file=error)
            print(f"{provider} lineup {fixture}: {exc}", file=error)
    print(json.dumps({
        "candidates": len(selected), "preview": not apply,
        "requests": calls, "updated": updated_count, "errors": errors,
        "blocked_providers": [name for name, until in blocked.items() if until],
    }), file=error)
    return 1 if errors else 0
