"""Offline tests for official PREM match-day lineup ingestion."""
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from schedulers.rugby import prem_lineups as mod

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, 12, tzinfo=UTC)
EP = {
    "_id": "Rugby_Mens_Prem:05d2fed576db879bc07227c8075010f1",
    "series_id": mod.SERIES,
    "external_ids": {"prem": "294928"},
    "season": 2026,
    "home": "Bristol Bears",
    "away": "Leicester Tigers",
    "scheduled": dt.datetime(2026, 10, 30, 19, 45),
}


def match_data(subs=8):
    def players(offset):
        return [
            {"id": offset + n, "name": f"Player {n}",
             "positionId": n, "position": f"position {n}",
             "firstName": "Player", "lastName": str(n), "known": f"Player {n}"}
            for n in range(1, 16 + subs)
        ]
    return {"id": 294928, "provider": "rugbyviz",
            "status": "result", "matchWinner": 100,
            "homeTeam": {"name": "Bristol Bears", "score": 42,
                         "players": list(reversed(players(1000)))},
            "awayTeam": {"name": "Leicester Tigers", "score": 0,
                         "players": players(2000)}}


class PremLineupsTests(unittest.TestCase):
    def test_uses_external_id_not_legacy_episode_hash(self):
        self.assertEqual(mod.fixture_id(EP), 294928)
        with self.assertRaises(ValueError):
            mod.fixture_id({**EP, "external_ids": {}})

    def test_extracts_and_sorts_without_spoilers(self):
        doc = mod.normalize(match_data(), EP, NOW)
        self.assertEqual(doc["_id"], EP["_id"])
        self.assertEqual(doc["status"], "complete")
        self.assertEqual(len(doc["home"]), 23)
        self.assertEqual(len(doc["away"]), 23)
        self.assertEqual([p["shirt_number"] for p in doc["home"]], list(range(1, 24)))
        self.assertEqual(doc["home"][0]["id"], 1001)
        self.assertEqual(doc["home"][0]["known_name"], "Player 1")
        self.assertNotIn("score", str(doc).lower())
        self.assertNotIn("matchWinner", str(doc))
        self.assertNotIn("next_check_at", doc)

    def test_complete_old_seven_substitute_match(self):
        self.assertEqual(mod.normalize(match_data(subs=7), EP, NOW)["status"], "complete")

    def test_empty_lineup_gets_backoff(self):
        data = match_data()
        data["homeTeam"].pop("players")
        data["awayTeam"]["players"] = None
        doc = mod.normalize(data, EP, NOW)
        self.assertEqual(doc["status"], "unavailable")
        self.assertGreater(doc["next_check_at"], NOW)

    def test_wrong_fixture_or_opposition_is_rejected(self):
        data = match_data()
        data["id"] = 294929
        with self.assertRaises(ValueError):
            mod.normalize(data, EP, NOW)
        data = match_data()
        data["awayTeam"]["name"] = "Saracens"
        with self.assertRaises(ValueError):
            mod.normalize(data, EP, NOW)

    def test_duplicate_shirt_number_rejected(self):
        data = match_data()
        data["homeTeam"]["players"][0]["positionId"] = 1
        with self.assertRaises(ValueError):
            mod.normalize(data, EP, NOW)

    def test_candidates_datetime_and_external_id(self):
        db = MagicMock()
        older = {**EP, "_id": "Rugby_Mens_Prem:older",
                 "external_ids": {"prem": "294921"},
                 "scheduled": dt.datetime(2026, 10, 24)}
        date_only_future = {**EP, "_id": "Rugby_Mens_Prem:future",
                            "external_ids": {"prem": "294969"},
                            "scheduled": None, "scheduled_date": "2026-12-30"}
        newer = {**EP, "_id": "Rugby_Mens_Prem:newer",
                 "external_ids": {"prem": "294932"},
                 "scheduled": dt.datetime(2026, 11, 21, 12, tzinfo=UTC)}
        db.episodes.find.return_value = [older, date_only_future, newer]
        db.prem_lineups.find.return_value = []
        got = mod.candidates(db, now=NOW)
        self.assertEqual([mod.fixture_id(e) for e in got], [294932, 294921])
        self.assertEqual(len(mod.candidates(db, now=NOW, force=True)), 3)
        mod.candidates(db, fixture=294928, now=NOW)
        self.assertEqual(db.episodes.find.call_args.args[0]["external_ids.prem"],
                         "294928")

    def test_skips_completed_and_future_retry(self):
        db = MagicMock()
        db.episodes.find.return_value = [EP]
        db.prem_lineups.find.return_value = [{
            "_id": EP["_id"], "status": "unavailable",
            "next_check_at": dt.datetime(2026, 11, 23),
        }]
        self.assertEqual(mod.candidates(db, now=NOW), [])
        self.assertEqual(len(mod.candidates(db, now=NOW, force=True)), 1)
        db.prem_lineups.find.return_value = [{"_id": EP["_id"], "status": "complete"}]
        self.assertEqual(mod.candidates(db, now=NOW), [])

    def test_upsert_does_not_downgrade_complete(self):
        db = MagicMock()
        db.prem_lineups.find_one.return_value = {
            "_id": EP["_id"], "status": "complete",
            "home": [{"id": i} for i in range(23)],
            "away": [{"id": i} for i in range(23)],
        }
        empty = mod.normalize({**match_data(),
            "homeTeam": {"name": "Bristol Bears", "players": []},
            "awayTeam": {"name": "Leicester Tigers", "players": []}}, EP, NOW)
        self.assertFalse(mod.upsert(db, empty))
        db.prem_lineups.update_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
