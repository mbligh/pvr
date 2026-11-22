"""WXV separation: frozen history, independent numbering and Atlas migration."""
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

from schedulers.rugby import importer, wxv

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 21, 12, tzinfo=UTC)
CONFIG = {
    "timezone": "America/New_York",
    "calendars": [
        {"slug": "rugby-womens-intl", "name": "Rugby Womens Intl",
         "nfo_showtitle": "Rugby Womens Intl", "type": "world-rugby",
         "group": "womens-intl", "library_dir": "Rugby_Womens_Intl"},
        {"slug": "rugby-womens-wxv", "name": "Rugby Womens WXV",
         "nfo_showtitle": "Rugby Womens WXV", "type": "world-rugby",
         "group": "wxv", "library_dir": "Rugby_Womens_WXV"}
    ]
}


def mixed_snapshot():
    cal = Calendar()
    cal.add("version", "2.0")
    cal.add("prodid", "-//unit-test//EN")
    games = [
        ("ordinary", "Ireland", "Wales", dt.datetime(2026, 10, 20, tzinfo=UTC),
         "Women's Internationals 2026"),
        ("first", "Canada", "England", dt.datetime(2026, 10, 16, 23, tzinfo=UTC),
         "WXV 2026"),
        ("second", "Australia", "Scotland", dt.datetime(2026, 10, 23, 8, tzinfo=UTC),
         "WXV 2026"),
        ("third", "New Zealand", "France", dt.datetime(2026, 10, 31, 6, tzinfo=UTC),
         "WXV 2026"),
    ]
    for mid, home, away, kickoff, comp in games:
        e = Event()
        e.add("uid", f"world-rugby-{mid}@mbligh.github.io")
        e.add("summary", f"{home} vs {away}")
        e.add("dtstart", kickoff)
        e.add("dtend", kickoff + dt.timedelta(hours=2))
        e.add("description", comp)
        cal.add_component(e)
    return cal


class WXVSplitTests(unittest.TestCase):
    def test_snapshot_split_preserves_original_uids_and_events(self):
        intl, wxvcal = wxv.split_snapshot(mixed_snapshot())
        self.assertEqual(len(intl.walk("VEVENT")), 1)
        self.assertEqual(len(wxvcal.walk("VEVENT")), 3)
        self.assertEqual([str(e.get("UID")) for e in wxvcal.walk("VEVENT")],
                         [f"world-rugby-{x}@mbligh.github.io"
                          for x in ("first", "second", "third")])
        self.assertTrue(all("WXV" in str(e.get("DESCRIPTION"))
                            for e in wxvcal.walk("VEVENT")))

    def test_global_series_snapshot_is_wxv_but_challenger_is_not(self):
        stored = mixed_snapshot()
        for match_id, label in (
            ("global", "WXV Global Series 2026"),
            ("challenger", "WXV Global Series Challenger 2026"),
        ):
            event = Event()
            event.add("uid", f"world-rugby-{match_id}@mbligh.github.io")
            event.add("description", label)
            stored.add_component(event)
        intl, wxvcal = wxv.split_snapshot(stored)
        self.assertEqual(
            {str(e.get("UID")) for e in wxvcal.walk("VEVENT")},
            {f"world-rugby-{mid}@mbligh.github.io"
             for mid in ("first", "second", "third", "global")},
        )
        # Already-published history is retained in its original snapshot,
        # never silently deleted or incorrectly migrated into WXV.
        self.assertIn("world-rugby-challenger@mbligh.github.io",
                      {str(e.get("UID")) for e in intl.walk("VEVENT")})

    def test_historical_wxv_uses_independent_round_and_nfo_paths(self):
        split_intl, split_wxv = wxv.split_snapshot(mixed_snapshot())
        with tempfile.TemporaryDirectory() as d:
            docs = pathlib.Path(d) / "docs"
            docs.mkdir()
            (docs / "rugby-womens-intl.ics").write_bytes(split_intl.to_ical())
            (docs / "rugby-womens-wxv.ics").write_bytes(split_wxv.to_ical())
            (docs / "rugby-metadata-manifest.json").write_text(json.dumps({
                "schema_version": 2, "fixtures": [
                    {"id": "rugby-womens-intl:world-rugby-first@mbligh.github.io",
                     "competition": "rugby-womens-intl", "season": 2026,
                     "round": 1, "episode": 117}
                ]}))
            output, manifest = importer.plex_nfo.build(CONFIG, docs, NOW)
        ww = {f["id"].split("world-rugby-")[-1].split("@")[0]: f
              for f in manifest["fixtures"] if f["competition"] == wxv.WXV}
        self.assertEqual({k: v["episode"] for k, v in ww.items()},
                         {"first": 101, "second": 201, "third": 301})
        self.assertTrue(all(f["nfo"].startswith("Rugby_Womens_WXV/Season_2026/")
                            for f in ww.values()))
        self.assertTrue(all(f["showtitle"] == "Rugby Womens WXV"
                            for f in ww.values()))

    def test_import_build_is_repeatable_from_stored_score_free_snapshot(self):
        snapshot = mixed_snapshot()
        db = MagicMock()
        db.calendar_snapshots.find_one.return_value = None
        old = {"schema_version": 2, "fixtures": []}
        with patch.object(importer, "read_snapshot", return_value=snapshot), patch.object(
                importer, "old_manifest", return_value=old):
            stored, manifest, episodes = importer.build_catalogue(
                db, CONFIG, NOW, refresh=False)
        self.assertEqual(set(stored), {wxv.WOMENS, wxv.WXV})
        self.assertEqual(len(episodes), 4)
        self.assertEqual(len([e for e in episodes if e["series_id"] == wxv.WXV]), 3)
        ids = [e["_id"] for e in episodes]
        self.assertIn("rugby-womens-wxv:world-rugby-first@mbligh.github.io", ids)
        self.assertNotIn("rugby-womens-intl:world-rugby-first@mbligh.github.io", ids)
        self.assertTrue(all("score" not in str(e).lower() for e in episodes))

    def test_migration_marks_old_wxv_as_superseded_and_updates_links(self):
        old = {
            "_id": "rugby-womens-intl:world-rugby-first@mbligh.github.io",
            "series_id": wxv.WOMENS, "nfo": "<plot>WXV 2026</plot>"
        }
        newer = {
            "_id": "rugby-womens-wxv:world-rugby-first@mbligh.github.io",
            "series_id": wxv.WXV, "season": 2026, "episode": 101,
            "nfo_path": "Rugby_Womens_WXV/Season_2026/Rugby_Womens_WXV-s2026e101-Canada_v_England_20261016.nfo",
            "nfo": "<episodedetails><showtitle>Rugby Womens WXV</showtitle>"
                   "<season>2026</season><episode>101</episode></episodedetails>",
        }
        db = MagicMock()
        db.episodes.find.side_effect = [[old], []]
        db.files.find.return_value = []
        db.episodes.bulk_write.return_value = MagicMock()
        importer.persist(db, {wxv.WXV: mixed_snapshot().to_ical().decode()},
                         {"schema_version": 2, "fixtures": []},
                         [newer], NOW)
        db.episodes.bulk_write.assert_called_once()
        update = db.episodes.update_one.call_args
        self.assertEqual(update.args[0]["_id"], old["_id"])
        self.assertEqual(update.args[1]["$set"]["superseded_by"], newer["_id"])
        self.assertEqual(update.args[1]["$set"]["status"], "superseded")
        self.assertEqual(db.releases.update_many.call_args.args[1]["$set"]["episode_id"],
                         newer["_id"])
        self.assertEqual(db.jobs.update_many.call_args.args[1]["$set"]["episode_id"],
                         newer["_id"])

    def test_inventory_conflict_fails_before_writes(self):
        old = {
            "_id": "rugby-womens-intl:world-rugby-first@mbligh.github.io",
            "series_id": wxv.WOMENS, "nfo": "<plot>WXV 2026</plot>"
        }
        new = {"_id": wxv.WXV + ":" + old["_id"].split(":", 1)[1],
               "series_id": wxv.WXV}
        db = MagicMock()
        db.episodes.find.side_effect = [[old], []]
        db.files.find.return_value = [{"episode_id": old["_id"],
                                       "path": "/data/sports/Rugby_Womens_Intl/x.mkv"}]
        with self.assertRaisesRegex(ValueError, "inventoried"):
            importer.persist(db, {}, {"schema_version": 2}, [new], NOW)
        db.episodes.bulk_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
