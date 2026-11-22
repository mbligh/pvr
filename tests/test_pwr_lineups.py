"""Offline tests for selected PWR players."""
import datetime as dt
import unittest
from unittest.mock import MagicMock
from schedulers.rugby import pwr_lineups as mod

NOW = dt.datetime(2026, 11, 22, tzinfo=dt.timezone.utc)
EP = {"_id": "Rugby_Womens_PWR:pwr-295034@mbligh.github.io",
      "series_id": mod.SERIES, "season": 2026, "scheduled": NOW,
      "home": "Loughborough Lightning", "away": "Trailfinders Women"}


def sheet(subs=8):
    rows = [{"shirtNumber": n, "position": "player",
             "homePlayer": {"id": 1000+n, "name": f"Home {n}"},
             "awayPlayer": {"id": 2000+n, "name": f"Away {n}"}}
            for n in range(1, 16+subs)]
    return {"forwards": rows[:8], "backs": rows[8:15], "subs": rows[15:]}


class Tests(unittest.TestCase):
    def test_complete_23(self):
        doc = mod.normalize(sheet(), EP, NOW)
        self.assertEqual(doc["status"], "complete")
        self.assertEqual((len(doc["home"]), len(doc["away"])), (23, 23))
        self.assertEqual(doc["home"][0]["id"], 1001)
        self.assertNotIn("next_check_at", doc)

    def test_old_22(self):
        self.assertEqual(mod.normalize(sheet(7), EP, NOW)["status"], "complete")

    def test_empty_backoff(self):
        doc = mod.normalize({"forwards": [], "backs": [], "subs": []}, EP, NOW)
        self.assertEqual(doc["status"], "unavailable")
        self.assertGreater(doc["next_check_at"], NOW)

    def test_bad_shirt(self):
        data = sheet()
        data["subs"][0]["shirtNumber"] = 1
        with self.assertRaises(ValueError):
            mod.normalize(data, EP, NOW)

    def test_extra_shirt_24_or_25_is_preserved_not_counted(self):
        data = sheet()
        data["subs"].extend([
            {"shirtNumber": 24, "position": "sub 9",
             "homePlayer": {"id": 3024, "name": "Home extra"},
             "awayPlayer": None},
            {"shirtNumber": 25, "position": "sub 10",
             "homePlayer": None,
             "awayPlayer": {"id": 4025, "name": "Away extra"}},
        ])
        document = mod.normalize(data, EP, NOW)
        self.assertEqual(document["status"], "complete")
        self.assertEqual(len(document["home"]), 23)
        self.assertEqual(len(document["away"]), 23)
        self.assertEqual(document["additional_players"]["home"][0]["shirt_number"], 24)
        self.assertEqual(document["additional_players"]["home"][0]["id"], 3024)
        self.assertEqual(document["additional_players"]["away"][0]["shirt_number"], 25)
        self.assertEqual(document["additional_players"]["away"][0]["id"], 4025)

    def test_extra_player_does_not_make_incomplete_lineup_complete(self):
        data = sheet()
        data["subs"] = data["subs"][:6]
        data["subs"].append({
            "shirtNumber": 24, "position": "second row",
            "homePlayer": {"id": 3024, "name": "Extra"},
            "awayPlayer": None,
        })
        document = mod.normalize(data, EP, NOW)
        self.assertEqual(document["status"], "partial")
        self.assertEqual(len(document["home"]), 21)
        self.assertEqual(len(document["additional_players"]["home"]), 1)

    def test_duplicate_shirt_24_is_rejected(self):
        data = sheet()
        data["subs"].extend([
            {"shirtNumber": 24, "position": "sub 9",
             "homePlayer": {"id": 3024, "name": "Extra"},
             "awayPlayer": None},
            {"shirtNumber": 24, "position": "sub 9",
             "homePlayer": None,
             "awayPlayer": {"id": 4024, "name": "Duplicate"}},
        ])
        with self.assertRaisesRegex(ValueError, "number 24"):
            mod.normalize(data, EP, NOW)

    def test_candidates_sort_mongodb_naive_and_aware_utc(self):
        db = MagicMock()
        older_naive = {**EP, "_id": "Rugby_Womens_PWR:pwr-200001@mbligh.github.io",
                       "scheduled": dt.datetime(2026, 11, 21, 12)}
        newer_aware = {**EP, "_id": "Rugby_Womens_PWR:pwr-200002@mbligh.github.io",
                       "scheduled": dt.datetime(2026, 11, 22, 13,
                                                tzinfo=dt.timezone(dt.timedelta(hours=1)))}
        no_kickoff = {**EP, "_id": "Rugby_Womens_PWR:pwr-200003@mbligh.github.io",
                      "scheduled": None}
        db.episodes.find.return_value = [older_naive, no_kickoff, newer_aware]
        db.pwr_lineups.find.return_value = []
        result = mod.candidates(db, now=NOW)
        self.assertEqual([mod.fixture_id(row) for row in result],
                         [200002, 200001, 200003])

    def test_candidates_naive_mongodb_next_check(self):
        db = MagicMock()
        db.episodes.find.return_value = [EP]
        db.pwr_lineups.find.return_value = [
            {"_id": EP["_id"], "status": "unavailable",
             "next_check_at": dt.datetime(2026, 11, 23)}
        ]
        self.assertEqual(mod.candidates(db, now=NOW), [])
        later = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)
        self.assertEqual(len(mod.candidates(db, now=later)), 1)

    def test_normalize_naive_schedule(self):
        episode = {**EP, "scheduled": dt.datetime(2026, 11, 22)}
        document = mod.normalize({"forwards": [], "backs": [], "subs": []},
                                 episode, now=NOW)
        self.assertEqual(document["status"], "unavailable")
        self.assertGreater(document["next_check_at"], NOW)


    def test_unnumbered_extra_player_does_not_block_valid_selection(self):
        data = sheet()
        data["forwards"].insert(0, {
            "shirtNumber": 0,
            "position": "sub 6",
            "homePlayer": None,
            "awayPlayer": {
                "id": 999, "name": "Unnumbered Extra",
                "position": {"id": 21, "name": "sub 6", "shirtNumber": 0},
            },
        })
        document = mod.normalize(data, EP, NOW)
        self.assertEqual(document["status"], "complete")
        self.assertEqual(len(document["home"]), 23)
        self.assertEqual(len(document["away"]), 23)
        self.assertNotIn(999, [p["id"] for p in document["away"]])

    def test_provider_known_missing_match_becomes_unavailable(self):
        import io
        from unittest.mock import patch
        import json
        response = io.BytesIO(json.dumps({
            "message": "Could not fetch match data", "status": 404,
        }).encode("utf-8"))
        with patch.object(mod.urllib.request, "urlopen", return_value=response):
            data = mod.fetch(295106)
        self.assertEqual(data, {"forwards": [], "backs": [], "subs": []})
        result = mod.normalize(data, {**EP, "_id":
            "Rugby_Womens_PWR:pwr-295106@mbligh.github.io"}, NOW)
        self.assertEqual(result["status"], "unavailable")

    def test_unexpected_failed_response_still_raises(self):
        import io
        from unittest.mock import patch
        import json
        response = io.BytesIO(json.dumps({
            "message": "Unexpected API failure", "status": 503,
        }).encode("utf-8"))
        with patch.object(mod.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                mod.fetch(295106)

    def test_future_date_only_fixtures_not_polled(self):
        db = MagicMock()
        episode = {**EP, "scheduled": None, "scheduled_date": "2027-06-05"}
        db.episodes.find.return_value = [episode]
        db.pwr_lineups.find.return_value = []
        self.assertEqual(mod.candidates(db, now=NOW), [])
        self.assertEqual(len(mod.candidates(db, now=NOW, force=True)), 1)
        close = dt.datetime(2027, 6, 3, 12, tzinfo=dt.timezone.utc)
        self.assertEqual(len(mod.candidates(db, now=close)), 1)
        lineups = mod.normalize({"forwards": [], "backs": [], "subs": []},
                                episode, now=close)
        self.assertLessEqual(
            lineups["next_check_at"] - close, dt.timedelta(hours=3))

    def test_candidates_do_not_poll_complete(self):
        db = MagicMock()
        db.episodes.find.return_value = [EP]
        db.pwr_lineups.find.return_value = [{"_id": EP["_id"], "status": "complete"}]
        self.assertEqual(mod.candidates(db, now=NOW), [])
        self.assertEqual(len(mod.candidates(db, now=NOW, force=True)), 1)


if __name__ == "__main__":
    unittest.main()
