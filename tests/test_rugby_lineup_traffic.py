"""Offline regression tests for per-provider rugby lineup traffic limits."""
import datetime as dt
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from schedulers.rugby import lineup_cli, lineup_traffic as traffic

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


class FakeState:
    def __init__(self):
        self.saved = {}
        self.updates = []

    def find_one(self, query):
        return self.saved.get(query["_id"])

    def update_one(self, query, changes, upsert=False):
        self.updates.append((query, changes))
        record = self.saved.setdefault(query["_id"], {})
        for key, value in changes["$max"].items():
            record[key] = max(record.get(key, value), value)
        record.update(changes["$set"])


class FakeDatabase:
    def __init__(self):
        self.metadata_state = FakeState()


class FakeAdapter:
    def __init__(self, name, responses):
        self.name = name
        self.responses = iter(responses)
        self.calls = []
        self.saved = []

    def fetch(self, fixture):
        self.calls.append(fixture)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def normalize(self, raw, episode, now=None):
        return {"_id": episode["_id"], "status": "complete",
                "home": [{"name": "A"}], "away": [{"name": "B"}]}

    def upsert(self, database, record):
        self.saved.append(record)
        return True


def row(provider, adapter, fixture):
    return {"provider": provider, "module": adapter, "fixture_id": fixture,
            "episode": {"_id": f"{provider}-{fixture}",
                        "series_id": f"Rugby_{provider}"}}


class TrafficTests(unittest.TestCase):
    def test_retry_after_formats(self):
        self.assertEqual(traffic.retry_after_seconds("7200", NOW), 7200)
        self.assertEqual(traffic.retry_after_seconds(
            "Sun, 22 Nov 2026 20:00:00 GMT", NOW), 7200)
        self.assertEqual(traffic.retry_after_seconds("nonsense", NOW), 0)
        self.assertEqual(traffic.retry_after_seconds(None, NOW), 0)

    def test_429_immediately_stops_one_provider_and_cools_down_next_run(self):
        world = FakeAdapter("world", [
            HTTPError("url", 429, "Too Many Requests", {"Retry-After": "7200"}, None),
            {"unused": True},
        ])
        pwr = FakeAdapter("pwr", [{"works": True}])
        rows = [row("world", world, 1), row("world", world, 2),
                row("pwr", pwr, 3)]
        db = FakeDatabase()
        err = io.StringIO()
        rc = traffic.process(
            db, rows, apply=True, output=io.StringIO(), error=err,
            clock=lambda: NOW, monotonic=lambda: 0, sleep=lambda _: None,
            delays={"world": 0, "prem": 0, "pwr": 0})
        self.assertEqual(rc, 1)
        self.assertEqual(world.calls, [1])
        self.assertEqual(pwr.calls, [3])
        self.assertEqual(pwr.saved[0]["status"], "complete")
        saved = db.metadata_state.saved["rugby_lineups_world"]
        self.assertEqual(saved["last_http_status"], 429)
        self.assertGreaterEqual(saved["retry_not_before"] - NOW,
                                dt.timedelta(hours=6))
        self.assertIn('"http_status": 429', err.getvalue())

        # Another cron invocation must not request the blocked provider.
        rc = traffic.process(
            db, [row("world", world, 4)], apply=True,
            output=io.StringIO(), error=io.StringIO(), clock=lambda: NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(world.calls, [1])

    def test_prem_wrapped_http_error_is_detected(self):
        original = HTTPError("url", 429, "rate limit", {"Retry-After": "86400"}, None)
        try:
            raise RuntimeError("PREM lineup API HTTP 429 for 123") from original
        except RuntimeError as wrapped:
            self.assertIs(traffic.http_error(wrapped), original)
            wrapped_error = wrapped
        prem = FakeAdapter("prem", [wrapped_error, {"unused": True}])
        db = FakeDatabase()
        traffic.process(
            db, [row("prem", prem, 1), row("prem", prem, 2)],
            apply=True, output=io.StringIO(), error=io.StringIO(),
            clock=lambda: NOW)
        self.assertEqual(prem.calls, [1])
        self.assertEqual(db.metadata_state.saved["rugby_lineups_prem"]["retry_not_before"],
                         NOW + dt.timedelta(days=1))

    def test_other_throttles_are_provider_specific(self):
        pwr = FakeAdapter("pwr", [
            HTTPError("url", 503, "service unavailable", {}, None)])
        db = FakeDatabase()
        traffic.process(
            db, [row("pwr", pwr, 1)], apply=True,
            output=io.StringIO(), error=io.StringIO(), clock=lambda: NOW)
        self.assertEqual(db.metadata_state.saved["rugby_lineups_pwr"]["last_http_status"],
                         503)
        self.assertEqual(db.metadata_state.saved["rugby_lineups_pwr"]["retry_not_before"],
                         NOW + dt.timedelta(hours=1))
        world = FakeAdapter("world", [{"ok": True}])
        traffic.process(
            db, [row("world", world, 1)], apply=True,
            output=io.StringIO(), error=io.StringIO(), clock=lambda: NOW)
        self.assertEqual(world.calls, [1])

    def test_per_provider_request_spacing_and_no_preview_requests(self):
        a = FakeAdapter("world", [{}, {}, {}])
        db = FakeDatabase()
        selected = [row("world", a, i) for i in (1, 2, 3)]
        slept = []
        traffic.process(
            db, selected, apply=False, output=io.StringIO(),
            error=io.StringIO(), clock=lambda: NOW)
        self.assertEqual(a.calls, [])
        self.assertEqual(db.metadata_state.updates, [])
        traffic.process(
            db, selected, apply=True, output=io.StringIO(),
            error=io.StringIO(), clock=lambda: NOW,
            monotonic=lambda: 0, sleep=slept.append,
            delays={"world": 10, "prem": 3, "pwr": 3})
        self.assertEqual(a.calls, [1, 2, 3])
        self.assertEqual(slept, [10, 10])

    def test_source_limits_applied_before_global_limit(self):
        w = [{"_id": f"world-{i}", "series_id": "Rugby_Mens_Nations",
              "scheduled": NOW - dt.timedelta(minutes=i)} for i in range(5)]
        with (patch.object(lineup_cli.world_rugby_lineups, "candidates",
                           return_value=w),
              patch.object(lineup_cli.world_rugby_lineups, "fixture_id",
                           side_effect=lambda ep: ep["_id"]),
              patch.object(lineup_cli.pwr_lineups, "candidates",
                           return_value=[{"_id": "pwr-1", "series_id": "Rugby_Womens_PWR",
                                          "scheduled": NOW - dt.timedelta(days=1)}]),
              patch.object(lineup_cli.pwr_lineups, "fixture_id", return_value=1),
              patch.object(lineup_cli.prem_lineups, "candidates", return_value=[])):
            rows = lineup_cli.select_candidates(
                MagicMock(), now=NOW, limit=3,
                provider_limits={"world": 1, "pwr": 1, "prem": 1})
        self.assertEqual([row["provider"] for row in rows], ["world", "pwr"])

    def test_other_http_errors_do_not_block_provider(self):
        world = FakeAdapter("world", [
            HTTPError("url", 404, "missing", {}, None), {}])
        db = FakeDatabase()
        traffic.process(
            db, [row("world", world, 1), row("world", world, 2)],
            apply=True, output=io.StringIO(), error=io.StringIO(),
            clock=lambda: NOW, monotonic=lambda: 0,
            delays={"world": 0, "prem": 0, "pwr": 0})
        self.assertEqual(world.calls, [1, 2])
        self.assertEqual(db.metadata_state.updates, [])


if __name__ == "__main__":
    unittest.main()
