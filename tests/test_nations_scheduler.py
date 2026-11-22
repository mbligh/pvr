"""Nations Championship round mapping and spoiler-free 2026 July history."""
import datetime as dt
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

from icalendar import Calendar, Event

from schedulers.rugby.nations import NORTH, SOUTH, verified_rounds
from schedulers.rugby import nations_history
from schedulers.rugby.legacy import plex_nfo

UTC = dt.timezone.utc
WEEKENDS = {
    1: dt.date(2026, 7, 4), 2: dt.date(2026, 7, 11),
    3: dt.date(2026, 7, 18), 4: dt.date(2026, 11, 7),
    5: dt.date(2026, 11, 14), 6: dt.date(2026, 11, 21),
    7: dt.date(2026, 11, 28),
}


def sample_calendar(rounds, skip=None):
    cal = Calendar()
    cal.add("version", "2.0")
    north, south = sorted(NORTH), sorted(SOUTH)
    for n in rounds:
        for i, (a, b) in enumerate(zip(north, south)):
            if (n, i) == skip:
                continue
            day = WEEKENDS[n]
            kickoff = dt.datetime.combine(day, dt.time(10 + i), tzinfo=UTC)
            event = Event()
            event.add("uid", f"world-rugby-fixture-{n}-{i}@mbligh.github.io")
            event.add("summary", f"{a} vs {b}")
            event.add("dtstart", kickoff)
            event.add("dtend", kickoff + dt.timedelta(hours=2))
            event.add("description", "Nations Championship 2026")
            cal.add_component(event)
    return cal


class NationsMetadataTests(unittest.TestCase):
    def test_all_seven_rounds_need_complete_six_match_windows(self):
        cal = sample_calendar(range(1, 8))
        result = verified_rounds(cal)
        self.assertEqual(len(result), 42)
        self.assertEqual(Counter(result.values()), {x: 6 for x in range(1, 8)})

    def test_one_missing_match_blocks_only_its_own_week(self):
        cal = sample_calendar(range(1, 8), skip=(6, 3))
        result = verified_rounds(cal)
        self.assertEqual(len(result), 36)
        self.assertNotIn(6, result.values())
        self.assertEqual(set(result.values()), {1, 2, 3, 4, 5, 7})

    def test_wrong_hemisphere_or_unknown_year_never_assigned(self):
        cal = sample_calendar((6,))
        ev = cal.walk("VEVENT")[0]
        ev["SUMMARY"] = "england vs ireland"
        self.assertEqual(verified_rounds(cal), {})
        future = sample_calendar((7,))
        for ev in future.walk("VEVENT"):
            date = ev.decoded("DTSTART")
            ev.pop("DTSTART")
            ev.add("dtstart", date.replace(year=2028))
        self.assertEqual(verified_rounds(future), {})

    def test_nfo_builder_assigns_rounds_6_and_7_and_preserves_old_rounds(self):
        cal = sample_calendar((4, 5, 6, 7))
        conf = {"calendars": [{
            "slug": "Rugby_Mens_Nations", "name": "Rugby Nations",
            "nfo_showtitle": "Rugby Nations Championships",
            "type": "world-rugby", "group": "nations",
        }]}
        with tempfile.TemporaryDirectory() as temp:
            docs = Path(temp) / "docs"
            docs.mkdir()
            (docs / "Rugby_Mens_Nations.ics").write_bytes(cal.to_ical())
            (docs / "rugby-metadata-manifest.json").write_text(json.dumps({
                "schema_version": 2, "fixtures": [{
                    "id": "Rugby_Mens_Nations:world-rugby-fixture-4-0@mbligh.github.io",
                    "competition": "Rugby_Mens_Nations", "round": 4,
                    "episode": 406, "season": 2026,
                }]
            }))
            files, manifest = plex_nfo.build(
                conf, docs, dt.datetime(2026, 11, 21, tzinfo=UTC))
        self.assertEqual(len(manifest["fixtures"]), 24)
        episodes = [f for f in manifest["fixtures"] if f["round"] == 6]
        self.assertEqual({f["episode"] for f in episodes}, set(range(601, 607)))
        self.assertEqual({f["episode"] for f in manifest["fixtures"] if f["round"] == 7},
                         set(range(701, 707)))
        prior = next(x for x in manifest["fixtures"] if x["id"].endswith("4-0@mbligh.github.io"))
        self.assertEqual(prior["episode"], 406)
        self.assertTrue(all("Rugby_Mens_Nations/Season_2026/" in k
                            for k in files if "/Season_" in k and k.endswith(".nfo")))

    def test_july_history_preparation_never_contains_result_data(self):
        cal = sample_calendar((1, 2, 3))
        documents = nations_history.prepare(2026, cal)
        self.assertEqual(len(documents), 18)
        self.assertEqual({x["episode"] for x in documents},
                         {r*100+n for r in range(1, 4) for n in range(1, 7)})
        self.assertTrue(all(x["metadata_provider"] == "world-rugby/history"
                            for x in documents))
        self.assertTrue(all("<season>2026</season>" in x["nfo"] for x in documents))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            nations_history.prepare(2026, sample_calendar((1, 2, 3), skip=(2, 0)))

    def test_july_persist_adds_only_missing_records(self):
        rows = nations_history.prepare(2026, sample_calendar((1, 2, 3)))
        db = MagicMock()
        db.episodes.find.return_value = []
        db.episodes.bulk_write.return_value.upserted_count = 18
        db.episodes.bulk_write.return_value.matched_count = 0
        with patch("schedulers.series_ids.assert_migrated"):
            result = nations_history.persist(db, rows)
        self.assertEqual(result["upserted"], 18)
        operations = db.episodes.bulk_write.call_args.args[0]
        self.assertTrue(all("$setOnInsert" in x._doc and "$set" not in x._doc
                            for x in operations))
        db.episodes.find.return_value = [{
            "_id": "different-id", "series_id": nations_history.SERIES,
            "season": 2026, "episode": 101,
        }]
        with patch("schedulers.series_ids.assert_migrated"):
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                nations_history.persist(db, rows)


if __name__ == "__main__":
    unittest.main()
