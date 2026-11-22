"""Verify PVR rugby metadata paths match the case-sensitive media library."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from icalendar import Calendar, Event
from plex_nfo import build


class LibraryPathTests(unittest.TestCase):
    def test_pwr_library_case_and_stable_episode(self):
        with tempfile.TemporaryDirectory() as temp:
            docs = Path(temp) / "docs"
            docs.mkdir()
            cal = Calendar()
            cal.add("version", "2.0")
            event = Event()
            event.add("uid", "pwr-295034@mbligh.github.io")
            event.add("summary", "Loughborough Lightning vs Trailfinders Women")
            event.add("dtstart", dt.datetime(2026, 11, 20, 19, 30, tzinfo=dt.timezone.utc))
            event.add("description", "Premiership Women's Rugby — Round 1")
            cal.add_component(event)
            (docs / "Rugby_Womens_PWR.ics").write_bytes(cal.to_ical())
            fixture_id = "Rugby_Womens_PWR:pwr-295034@mbligh.github.io"
            # Existing lowercase manifest paths must not affect the stable episode.
            (docs / "rugby-metadata-manifest.json").write_text(json.dumps({
                "schema_version": 2,
                "fixtures": [{
                    "id": fixture_id, "competition": "Rugby_Womens_PWR",
                    "season": 2026, "episode": 101, "round": 1,
                    "nfo": "rugby_womens_pwr/Season_2026/old.nfo"
                }]
            }))
            config = {"calendars": [{
                "slug": "Rugby_Womens_PWR",
                "name": "Rugby Womens PWR",
                "type": "pwr",
            }]}
            outputs, manifest = build(config, docs, dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc))
            fixture = next(item for item in manifest["fixtures"] if item["id"] == fixture_id)
            expected = ("Rugby_Womens_PWR/Season_2026/"
                        "Rugby_Womens_PWR-s2026e101-Loughborough_Lightning_v_"
                        "Trailfinders_Women_20261120.nfo")
            self.assertEqual(fixture["episode"], 101)
            self.assertEqual(fixture["nfo"], expected)
            self.assertIn(expected, outputs)
            self.assertIn("Rugby_Womens_PWR/tvshow.nfo", outputs)

    def test_slug_is_only_library_directory_even_if_legacy_override_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            docs = Path(temp) / "docs"
            docs.mkdir()
            cal = Calendar()
            cal.add("version", "2.0")
            event = Event()
            event.add("uid", "sample")
            event.add("summary", "Home vs Away")
            event.add("dtstart", dt.datetime(2026, 11, 20, 19, tzinfo=dt.timezone.utc))
            event.add("description", "Round 1")
            cal.add_component(event)
            (docs / "test.ics").write_bytes(cal.to_ical())
            outputs, manifest = build({"calendars": [{
                "slug": "test", "name": "Some Name", "library_dir": "Custom_Directory"
            }]}, docs, dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc))
            self.assertTrue(manifest["fixtures"][0]["nfo"].startswith("test/"))
            self.assertIn("test/tvshow.nfo", outputs)
            self.assertNotIn("Custom_Directory/tvshow.nfo", outputs)


if __name__ == "__main__":
    unittest.main()
