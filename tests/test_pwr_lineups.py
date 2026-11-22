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

    def test_candidates_do_not_poll_complete(self):
        db = MagicMock()
        db.episodes.find.return_value = [EP]
        db.pwr_lineups.find.return_value = [{"_id": EP["_id"], "status": "complete"}]
        self.assertEqual(mod.candidates(db, now=NOW), [])
        self.assertEqual(len(mod.candidates(db, now=NOW, force=True)), 1)


if __name__ == "__main__":
    unittest.main()
