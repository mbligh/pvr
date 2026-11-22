"""Men's International legacy NC duplication: import filter and safe cleanup."""
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from icalendar import Calendar, Event

from schedulers.rugby import importer

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, 14, tzinfo=UTC)
MEN = importer.MENS_INTL
CONFIG = {
    "timezone": "America/New_York",
    "calendars": [
        {"slug": MEN, "name": "Rugby Mens Intl",
         "type": "world-rugby", "group": "mens-intl"},
    ],
}


def calendar(*matches):
    cal = Calendar()
    cal.add("version", "2.0")
    cal.add("prodid", "-//unit-test//EN")
    for uid, title, day in matches:
        event = Event()
        event.add("uid", uid)
        event.add("summary", title)
        when = dt.datetime(2026, 11, day, 14, tzinfo=UTC)
        event.add("dtstart", when)
        event.add("dtend", when + dt.timedelta(hours=2))
        event.add("location", "Test Ground")
        cal.add_component(event)
    return cal


class NationsIntlCleanupTests(unittest.TestCase):
    def setUp(self):
        self.previous = calendar(
            ("one", "England A vs Argentina XV", 6),
            ("old-nc", "NC: England vs New Zealand", 21),
            ("two", "Tunisia vs Switzerland", 21))
        self.prior = {"schema_version": 2, "fixtures": [
            {"id": MEN + ":one", "competition": MEN,
             "season": 2026, "round": 1, "episode": 105},
            {"id": MEN + ":old-nc", "competition": MEN,
             "season": 2026, "round": 1, "episode": 106},
            {"id": MEN + ":two", "competition": MEN,
             "season": 2026, "round": 1, "episode": 107},
        ]}

    def test_clean_only_nc_and_keep_calendar_metadata(self):
        result = importer.strip_nations_from_intl(self.previous)
        self.assertEqual([str(e.get("UID")) for e in result.walk("VEVENT")],
                         ["one", "two"])
        self.assertEqual(len(self.previous.walk("VEVENT")), 3)
        self.assertEqual(str(result.get("PRODID")), "-//unit-test//EN")

    def test_no_refresh_rebuild_removes_nc_snapshot_manifest_and_episodes(self):
        with patch.object(importer, "read_snapshot", return_value=self.previous),              patch.object(importer, "old_manifest", return_value=self.prior):
            snaps, manifest, eps = importer.build_catalogue(
                None, CONFIG, NOW, refresh=False)
        cal = Calendar.from_ical(snaps[MEN].encode())
        self.assertEqual([str(e.get("UID")) for e in cal.walk("VEVENT")],
                         ["one", "two"])
        self.assertEqual({x["_id"] for x in eps}, {MEN + ":one", MEN + ":two"})
        self.assertEqual({f["id"]: f["episode"] for f in manifest["fixtures"]},
                         {MEN + ":one": 105, MEN + ":two": 107})
        self.assertEqual({e["_id"]: e["episode"] for e in eps},
                         {MEN + ":one": 105, MEN + ":two": 107})

    def test_refresh_filters_new_nc_events_in_feed_too(self):
        feed = calendar(("new-nc", " NC: France vs Argentina", 28),
                        ("new-other", "Ireland vs Georgia", 29))
        with patch.object(importer, "read_snapshot", return_value=self.previous),              patch.object(importer, "old_manifest", return_value=self.prior),              patch.object(importer, "fetch_calendar", return_value=feed):
            snaps, manifest, eps = importer.build_catalogue(
                None, CONFIG, NOW, refresh=True)
        ids = {e["_id"] for e in eps}
        self.assertNotIn(MEN + ":old-nc", ids)
        self.assertNotIn(MEN + ":new-nc", ids)
        self.assertIn(MEN + ":new-other", ids)
        self.assertNotIn("NC:", snaps[MEN])
        self.assertEqual({x["id"] for x in manifest["fixtures"]}, ids)

    def test_persist_deletes_only_unreferenced_nc_duplicates(self):
        duplicate = {"_id": MEN + ":old-nc", "series_id": MEN,
                     "title": "NC: England vs New Zealand"}
        db = MagicMock()
        db.episodes.find.return_value = [duplicate]
        db.files.find.return_value = []
        db.releases.find.return_value = []
        db.jobs.find.return_value = []
        db.episodes.delete_many.return_value.deleted_count = 1
        e = {"_id": MEN + ":one", "series_id": MEN,
             "title": "England A vs Argentina XV"}
        importer.persist(
            db, {MEN: self.previous.to_ical().decode()},
            {"schema_version": 2, "fixtures": []}, [e], NOW)
        db.episodes.bulk_write.assert_called_once()
        db.episodes.delete_many.assert_called_once()
        filt = db.episodes.delete_many.call_args.args[0]
        self.assertEqual(filt["_id"]["$in"], [duplicate["_id"]])
        self.assertEqual(filt["series_id"], MEN)

    def test_persist_refuses_to_delete_record_with_media_history_before_writes(self):
        duplicate = {"_id": MEN + ":old-nc", "series_id": MEN,
                     "title": "NC: England vs New Zealand"}
        for linked in ("files", "releases", "jobs"):
            with self.subTest(collection=linked):
                db = MagicMock()
                db.episodes.find.return_value = [duplicate]
                db.files.find.return_value = []
                db.releases.find.return_value = []
                db.jobs.find.return_value = []
                getattr(db, linked).find.return_value = [{"_id": "keep"}]
                with self.assertRaisesRegex(ValueError, linked):
                    importer.persist(
                        db, {MEN: self.previous.to_ical().decode()},
                        {"schema_version": 2, "fixtures": []},
                        [{"_id": MEN + ":one", "series_id": MEN,
                          "title": "England A vs Argentina XV"}], NOW)
                db.episodes.bulk_write.assert_not_called()
                db.episodes.delete_many.assert_not_called()

    def test_stale_duplicate_cleanup_not_applied_to_unrelated_catalogue(self):
        db = MagicMock()
        db.episodes.find.return_value = []
        importer.persist(
            db, {"Rugby_Womens_WXV": "BEGIN:VCALENDAR\nEND:VCALENDAR\n"},
            {"schema_version": 2, "fixtures": []}, [], NOW)
        db.episodes.delete_many.assert_not_called()


if __name__ == "__main__":
    unittest.main()
