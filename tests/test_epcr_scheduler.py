"""EPCR Champions Cup official fixture scheduling and Atlas import."""
import datetime as dt
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from icalendar import Calendar
from zoneinfo import ZoneInfo

from dashboard.data import DashboardData
from schedulers.rugby import epcr, importer, plex_nfo

UTC = dt.timezone.utc
SERIES = "Rugby_Mens_Championship"
NOW = dt.datetime(2026, 11, 23, tzinfo=UTC)


def match(mid, home="Leinster Rugby", away="Bath Rugby", *,
          day="2026-12-12T20:00:00.000Z", round_no=2, tbc=0):
    return {
        "id": mid, "compId": 1008, "season": 202601, "round": round_no,
        "date": day, "tbc": tbc,
        "homeTeam": {"name": home, "score": 48, "form": ["W"]},
        "awayTeam": {"name": away, "score": 5, "form": ["L"]},
        "matchWinner": 1, "status": "result", "events": [{"try": "SPOILER"}],
        "venue": {"name": "Aviva Stadium"},
    }


def payload(*matches):
    return {"status": "success", "data": list(matches),
            "metadata": {"totalItems": len(matches), "totalPages": 1}}


def blank():
    cal = Calendar()
    cal.add("version", "2.0")
    return cal


class EPCRSchedulerTests(unittest.TestCase):
    def entry(self):
        cfg = json.loads((pathlib.Path(__file__).resolve().parents[1] /
                          "schedulers/rugby/calendars.json").read_text())
        return next(e for e in cfg["calendars"] if e["slug"] == SERIES)

    def test_configuration_points_to_official_competition(self):
        entry = self.entry()
        self.assertEqual(entry["type"], "epcr")
        self.assertEqual(entry["compId"], 1008)
        self.assertIn("rugby-union-feeds.incrowdsports.com", entry["url"])

    def test_conversion_uses_only_schedule_fields_and_skips_placeholders(self):
        cal = epcr.convert_epcr(payload(
            match(293330),
            match(293331, "Glasgow Warriors", "Toulouse",
                  day="2027-01-09T17:00:00Z", round_no=3, tbc=1),
            match(293370, "TBC", "TBC", round_no=5)), season=202601)
        events = cal.walk("VEVENT")
        self.assertEqual(len(events), 2)
        self.assertEqual(str(events[0]["UID"]), "epcr-293330@mbligh.github.io")
        self.assertEqual(str(events[0]["X-EPCR-ID"]), "293330")
        self.assertEqual(str(events[0]["X-EPCR-ROUND"]), "2")
        self.assertIsInstance(events[0].decoded("DTSTART"), dt.datetime)
        self.assertIsInstance(events[1].decoded("DTSTART"), dt.date)
        self.assertNotIsInstance(events[1].decoded("DTSTART"), dt.datetime)
        wire = cal.to_ical().decode()
        for forbidden in ("SPOILER", "matchWinner", "score", "form",
                          "status", "result"):
            self.assertNotIn(forbidden, wire)

    def test_rejects_duplicate_foreign_or_truncated_feeds(self):
        for bad in (
            payload(match(1), match(1)),
            payload({**match(1), "compId": 1011}),
            payload({**match(1), "season": 202501}),
            {**payload(match(1)), "metadata": {"totalPages": 2, "totalItems": 2}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    epcr.convert_epcr(bad, season=202601)

    def test_request_uses_browser_public_headers_and_can_override(self):
        fixture = json.dumps(payload(match(293330))).encode()
        with patch.object(epcr.urllib.request, "urlopen",
                          return_value=io.BytesIO(fixture)) as fetch:
            with patch.dict("os.environ", {}, clear=True):
                epcr.fetch_epcr(self.entry()["url"] + "&season=202601",
                                season=202601)
        headers = dict(fetch.call_args.args[0].header_items())
        self.assertEqual(headers["X-app-id"], "web")
        self.assertEqual(headers["X-api-key"], epcr.PUBLIC_HEADERS["X-API-KEY"])
        self.assertEqual(headers["X-realm"], "epcr")

    def test_new_series_bootstraps_without_nonexistent_legacy_ics(self):
        database = MagicMock()
        database.calendar_snapshots.find_one.return_value = None
        with patch.object(importer, "legacy_content") as legacy:
            source = importer.read_snapshot(database, SERIES)
        self.assertEqual(source.walk("VEVENT"), [])
        legacy.assert_not_called()

    def test_season_selection_and_import_routing(self):
        entry = self.entry()
        for now, code in (
            (dt.datetime(2026, 11, 23, tzinfo=UTC), 202601),
            (dt.datetime(2027, 5, 1, tzinfo=UTC), 202601),
            (dt.datetime(2027, 8, 1, tzinfo=UTC), 202701),
        ):
            with patch.object(importer, "fetch_epcr", return_value=blank()) as fetch:
                importer.fetch_calendar(entry, now)
            self.assertEqual(fetch.call_args.kwargs["season"], code)
            self.assertIn("season=" + str(code), fetch.call_args.args[0])

    def test_episode_numbering_paths_and_external_ids(self):
        cal = epcr.convert_epcr(payload(
            match(293330),
            match(293331, "Glasgow Warriors", "Toulouse",
                  day="2026-12-12T21:00:00Z"),
            match(293370, "TBC", "TBC", round_no=5)), season=202601)
        entry = self.entry()
        with tempfile.TemporaryDirectory() as temp:
            docs = pathlib.Path(temp)
            (docs / (SERIES + ".ics")).write_bytes(cal.to_ical())
            (docs / "rugby-metadata-manifest.json").write_text(
                json.dumps({"schema_version": 2, "fixtures": []}))
            files, manifest = plex_nfo.build(
                {"calendars": [entry]}, docs, NOW)
        rows = manifest["fixtures"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["episode"] for r in rows], [201, 202])
        self.assertTrue(all(r["season"] == 2026 and r["round"] == 2 for r in rows))
        self.assertTrue(all(r["nfo"].startswith(
            SERIES + "/Season_2026/" + SERIES + "-s2026e") for r in rows))
        self.assertIn(b"<showtitle>Rugby Mens Championship</showtitle>",
                      files[rows[0]["nfo"]])
        self.assertIn(SERIES + "/tvshow.nfo", files)

        db = MagicMock()
        # The new series must not read a missing public calendar, and the
        # merger must reject newly discovered fixtures already in the past.
        with (patch.object(importer, "read_snapshot", return_value=blank()),
              patch.object(importer, "old_manifest", return_value={
                  "schema_version": 2, "fixtures": []}),
              patch.object(importer, "fetch_epcr", return_value=cal)):
            snapshots, _, episodes = importer.build_catalogue(
                db, {"timezone": "America/New_York", "calendars": [entry]},
                NOW, refresh=True)
        self.assertIn(SERIES, snapshots)
        self.assertEqual(len(episodes), 2)
        self.assertEqual({e["external_ids"]["epcr"] for e in episodes},
                         {"293330", "293331"})
        self.assertTrue(all(e["metadata_provider"] == "epcr/official"
                            for e in episodes))

    def test_generic_merge_still_rejects_new_past_fixtures(self):
        cal = epcr.convert_epcr(payload(
            match(293317, day="2026-10-16T19:00:00Z", round_no=1),
            match(293330)), season=202601)
        result = importer.calendar_filter.merge(
            cal, blank(), NOW, dt.timezone.utc, "Championship")
        self.assertEqual([str(e["UID"]) for e in result.walk("VEVENT")],
                         ["epcr-293330@mbligh.github.io"])

    def test_imports_completed_round_as_missing_without_scores(self):
        # The actual November 2026 EPCR feed has 12 already-completed round
        # one fixtures. They must appear as Missing even though round two
        # is outside the dashboard's seven-day upcoming cutoff.
        previous = blank()
        completed = [
            match(293317 + n, f"Home {n}", f"Away {n}",
                  day=f"2026-10-17T{12 + n:02d}:00:00Z", round_no=1)
            for n in range(12)
        ]
        source = epcr.convert_epcr(payload(*completed, match(293330)),
                                   season=202601)
        db = MagicMock()
        with (patch.object(importer, "read_snapshot", return_value=previous),
              patch.object(importer, "old_manifest", return_value={
                  "schema_version": 2, "fixtures": []}),
              patch.object(importer, "fetch_epcr", return_value=source)):
            snapshots, _, episodes = importer.build_catalogue(
                db, {"timezone": "America/New_York", "calendars": [self.entry()]},
                NOW, refresh=True)

        self.assertEqual(len(episodes), 13)
        self.assertEqual(len([e for e in episodes if e["round"] == 1]), 12)
        self.assertEqual({e["episode"] for e in episodes if e["round"] == 1},
                         set(range(101, 113)))
        self.assertEqual({e["external_ids"]["epcr"] for e in episodes
                          if e["round"] == 1},
                         {str(293317 + n) for n in range(12)})
        self.assertNotIn("score", snapshots[SERIES].lower())
        self.assertNotIn("result", snapshots[SERIES].lower())
        self.assertTrue(all(e["nfo_path"] and e.get("nfo") for e in episodes))

        with tempfile.TemporaryDirectory() as temp:
            db.episodes.find.return_value = episodes
            db.files.find.return_value = []
            data = DashboardData(db, pathlib.Path(temp),
                                 ZoneInfo("America/New_York"))
            visible = data.rows(SERIES, now=NOW)
        self.assertEqual(len(visible), 12)
        self.assertEqual({row["status"] for row in visible}, {"Missing"})

    def test_historical_import_preserves_existing_past_fixtures(self):
        old = epcr.convert_epcr(payload(
            match(293317, "Original Home", "Original Away",
                  day="2026-10-16T19:00:00Z", round_no=1)), season=202601)
        revised = epcr.convert_epcr(payload(
            match(293317, "Changed Home", "Changed Away",
                  day="2026-10-18T19:00:00Z", round_no=1)), season=202601)
        merged = importer.calendar_filter.merge(
            revised, old, NOW, dt.timezone.utc, SERIES,
            allow_new_past=True)
        saved = merged.walk("VEVENT")
        self.assertEqual(len(saved), 1)
        self.assertEqual(str(saved[0]["SUMMARY"]),
                         "Original Home vs Original Away")
        self.assertEqual(saved[0].decoded("DTSTART"),
                         dt.datetime(2026, 10, 16, 19, tzinfo=UTC))

    def test_historical_import_rejects_explicit_post_kickoff_revision(self):
        source = epcr.convert_epcr(payload(
            match(293317, day="2026-10-16T19:00:00Z", round_no=1)),
            season=202601)
        source.walk("VEVENT")[0].add("last-modified", NOW)
        merged = importer.calendar_filter.merge(
            source, blank(), NOW, dt.timezone.utc, SERIES,
            allow_new_past=True)
        self.assertEqual(merged.walk("VEVENT"), [])


if __name__ == "__main__":
    unittest.main()
