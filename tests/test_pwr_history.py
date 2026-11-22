"""Offline tests for historical PWR fixtures, postseason numbering and safety."""
import datetime as dt
import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

from schedulers.rugby import pwr_history as MODULE
UK = ZoneInfo("Europe/London")
UTC = dt.timezone.utc


def calendar_from_fixtures():
    calendar = Calendar()
    calendar.add("version", "2.0")
    matches = [
        (289126, "2025-10-24T18:30:00+00:00", 1,
         "Harlequins", "Loughborough Lightning"),
        (289127, "2025-10-25T14:00:00+00:00", 1,
         "Trailfinders Women", "Exeter Chiefs"),
        (289198, "2026-06-14T13:00:00+00:00", 19,
         "Gloucester Hartpury", "Trailfinders Women"),
        (289199, "2026-06-14T15:30:00+00:00", 19,
         "Saracens", "Exeter Chiefs"),
        (289200, "2026-06-28T14:00:00+00:00", 20,
         "Saracens", "Trailfinders Women"),
    ]
    for match_id, instant, round_no, home, away in matches:
        kickoff = dt.datetime.fromisoformat(instant)
        ev = Event()
        ev.add("uid", f"pwr-{match_id}@mbligh.github.io")
        ev.add("summary", f"{home} vs {away}")
        ev.add("dtstart", kickoff)
        ev.add("dtend", kickoff + dt.timedelta(hours=2))
        ev.add("description", f"Premiership Women's Rugby — Round {round_no}")
        ev.add("location", "Example Ground")
        calendar.add_component(ev)
    return calendar


class PWRHistoryTests(unittest.TestCase):
    def test_source_url_and_year_guard(self):
        self.assertEqual(MODULE.api_url(2025),
            "https://www.thepwr.com/api/oval/fixtures?compId=1095&seasonId=202501")
        with self.assertRaises(ValueError):
            MODULE.api_url(2026)

    def test_existing_numbering_and_postseason(self):
        records = MODULE.prepare(2025, calendar_from_fixtures())
        self.assertEqual(len(records), 5)
        by_id = {r["_id"].split(":")[1].split("@")[0]: r for r in records}
        self.assertEqual(by_id["pwr-289126"]["episode"], 101)
        self.assertEqual(by_id["pwr-289127"]["episode"], 102)
        self.assertEqual(by_id["pwr-289198"]["episode"], 1901)
        self.assertEqual(by_id["pwr-289199"]["episode"], 1902)
        self.assertEqual(by_id["pwr-289200"]["episode"], 2001)
        self.assertTrue(all(r["season"] == 2025 for r in records))
        self.assertTrue(all("Season_2025/" in r["nfo_path"] for r in records))
        self.assertTrue(all("Rugby_Womens_PWR-s2025" in r["nfo_path"] for r in records))
        self.assertIn("<episode>2001</episode>", by_id["pwr-289200"]["nfo"])
        self.assertTrue(all("score" not in r["nfo"].lower() for r in records))

    def test_reimport_keeps_historical_episode_numbers(self):
        previous = [{"id": "Rugby_Womens_PWR:pwr-289126@mbligh.github.io",
                     "round": 1, "episode": 104, "season": 2025,
                     "competition": MODULE.SERIES}]
        records = MODULE.prepare(2025, calendar_from_fixtures(), prior=previous)
        by_id = {r["_id"]: r for r in records}
        self.assertEqual(by_id[previous[0]["id"]]["episode"], 104)
        # Other events receive unused IDs from the same round.
        self.assertEqual(len({r["episode"] for r in records}), len(records))

    def test_missing_round_is_preserved_unnumbered(self):
        cal = calendar_from_fixtures()
        missing = Event()
        missing.add("uid", "pwr-999999@mbligh.github.io")
        missing.add("summary", "Harlequins vs Sale Sharks")
        missing.add("dtstart", dt.datetime(2026, 3, 1, 14, tzinfo=UTC))
        missing.add("description", "Premiership Women's Rugby")
        cal.add_component(missing)
        records = MODULE.prepare(2025, cal)
        self.assertEqual(len(records), 6)
        no_round = next(x for x in records if "999999" in x["_id"])
        self.assertEqual(no_round["season"], 2025)
        self.assertIsNone(no_round["round"])
        self.assertIsNone(no_round["episode"])
        self.assertIsNone(no_round["nfo_path"])
        self.assertIsNone(no_round["nfo"])
        self.assertEqual(no_round["scheduled"].year, 2026)
        self.assertEqual(sum(x["episode"] is not None for x in records), 5)

    def test_mismatched_historical_season_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unexpected season"):
            MODULE.prepare(2024, calendar_from_fixtures())

    def test_safe_api_adapter_discards_result_fields(self):
        from schedulers.rugby import pwr
        payload = {"data": [{
            "id": 289126, "dateTime": "2025-10-24T18:30:00.000Z",
            "round": 1, "tbc": 0,
            "homeTeam": {"name": "Harlequins",
                         "score": {"finalScore": 12345}},
            "awayTeam": {"name": "Loughborough Lightning",
                         "score": {"finalScore": 67890}},
            "venue": {"name": "Example Ground"},
            "matchStatus": "result", "matchWinner": 4521
        }]}
        with patch.object(pwr.urllib.request, "urlopen",
                          return_value=io.BytesIO(json.dumps(payload).encode())):
            cal = pwr.fetch_pwr(MODULE.api_url(2025))
        records = MODULE.prepare(2025, cal)
        serialized = json.dumps(records, default=str)
        for secret in ("12345", "67890", "matchWinner", "matchStatus", "finalScore"):
            self.assertNotIn(secret, serialized)

    def test_apply_checks_conflicts_and_keeps_operational_fields_untouched(self):
        records = MODULE.prepare(2025, calendar_from_fixtures())
        db = MagicMock()
        db.episodes.find.return_value = []
        db.episodes.bulk_write.return_value.matched_count = 0
        db.episodes.bulk_write.return_value.modified_count = 0
        db.episodes.bulk_write.return_value.upserted_count = len(records)
        with patch("schedulers.series_ids.assert_migrated", return_value=None):
            report = MODULE.persist(db, records)
        self.assertEqual(report["upserted"], 5)
        operations = db.episodes.bulk_write.call_args.args[0]
        self.assertEqual(len(operations), 5)
        # The importer updates metadata only; it never sets download state.
        self.assertTrue(all("$set" not in op._doc and
                            "download_status" not in op._doc["$setOnInsert"]
                            for op in operations))
        db.episodes.find.return_value = [{"_id": records[0]["_id"]}]
        with patch("schedulers.series_ids.assert_migrated", return_value=None):
            with self.assertRaisesRegex(ValueError, "already used"):
                MODULE.persist(db, records)
        self.assertEqual(db.episodes.bulk_write.call_count, 1)


if __name__ == "__main__":
    unittest.main()
