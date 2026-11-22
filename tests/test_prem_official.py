"""Official Gallagher PREM as the single spoiler-free fixture source."""
import datetime as dt
import io
import json
import pathlib
import unittest
from unittest.mock import MagicMock, patch

from icalendar import Calendar, Event

from schedulers.rugby import importer
from schedulers.rugby.prem_identity import reconcile_uids, season_for
from schedulers.rugby import prem


UTC = dt.timezone.utc


def fixture(mid, home, away, date, round_no=3, tbc=0):
    return {
        "id": mid, "compId": 1011, "season": 202601,
        "date": date, "round": round_no, "tbc": tbc,
        "homeTeam": {"name": home, "score": {"finalScore": 19}},
        "awayTeam": {"name": away, "score": {"finalScore": 10}},
        "matchWinner": "A", "status": "result",
        "venue": {"name": "Ground"},
    }


def old_event(uid, title, when):
    e = Event()
    e.add("uid", uid)
    e.add("summary", title)
    e.add("dtstart", when)
    e.add("dtend", when + dt.timedelta(hours=2))
    e.add("description", "Spoiler-free rugby fixture")
    return e


def calendar(*events):
    c = Calendar()
    c.add("version", "2.0")
    for e in events:
        c.add_component(e)
    return c


class OfficialPremTests(unittest.TestCase):
    def test_public_headers_are_default_and_can_be_overridden(self):
        match = fixture(294919, "Northampton Saints", "Bath Rugby",
                        "2026-10-10T14:05:00Z")
        payload = json.dumps({"data": [match]}).encode()
        with patch.object(prem.urllib.request, "urlopen",
                          return_value=io.BytesIO(payload)) as fetch:
            with patch.dict("os.environ", {}, clear=True):
                prem.fetch_prem("https://example.test/fixtures")
        headers = dict(fetch.call_args.args[0].header_items())
        self.assertEqual(headers["X-app-id"], "web")
        self.assertEqual(headers["X-api-key"], prem.PUBLIC_HEADERS["X-API-KEY"])
        self.assertEqual(headers["X-realm"], "prl")

    def test_adapter_allowlists_fields_and_handles_tbc(self):
        matches = [
            fixture(294919, "Northampton Saints", "Bath Rugby",
                    "2026-10-10T14:05:00Z", round_no=3),
            fixture(294950, "Saracens", "Gloucester Rugby",
                    "2027-04-17T14:00:00Z", round_no=16, tbc=1),
            fixture(295000, "TBC", "TBC", "2027-06-19T14:00:00Z",
                    round_no=20),
        ]
        cal = prem.convert_prem({"data": matches})
        events = cal.walk("VEVENT")
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0].decoded("DTSTART"), dt.datetime)
        self.assertIsInstance(events[1].decoded("DTSTART"), dt.date)
        self.assertNotIsInstance(events[1].decoded("DTSTART"), dt.datetime)
        self.assertEqual(str(events[0]["UID"]),
                         "prem-294919@mbligh.github.io")
        self.assertEqual(str(events[0]["X-PREM-ID"]), "294919")
        self.assertEqual(str(events[0]["X-PREM-ROUND"]), "3")
        result = cal.to_ical().decode()
        for forbidden in ("matchWinner", "finalScore", "attendance",
                          "score", "result", "status"):
            self.assertNotIn(forbidden, result)

    def test_active_season_code_rolls_over_in_july(self):
        for when, expected in [
            (dt.datetime(2026, 11, 21, tzinfo=UTC), 202601),
            (dt.datetime(2027, 5, 1, tzinfo=UTC), 202601),
            (dt.datetime(2027, 9, 1, tzinfo=UTC), 202701),
        ]:
            self.assertEqual(season_for(when), expected)
        entry = next(e for e in json.loads(
            (importer.RUGBY_DIR / "calendars.json").read_text())["calendars"]
            if e["slug"] == "Rugby_Mens_Prem")
        self.assertEqual(entry["type"], "prem")
        self.assertNotIn("rugbyfixture.io", entry["url"])
        with patch.object(importer, "fetch_prem", return_value=calendar()) as call:
            importer.fetch_calendar(entry, dt.datetime(2026, 11, 21, tzinfo=UTC))
        self.assertIn("season=202601", call.call_args.args[0])

    def test_previous_ids_reused_with_matching_teams_and_dates(self):
        prev = calendar(old_event(
            "ics-old-match-uid",
            "Northampton Saints vs Bath",
            dt.datetime(2026, 10, 10, 14, 5, tzinfo=UTC)))
        fresh = prem.convert_prem({"data": [fixture(
            294919, "Northampton Saints", "Bath Rugby",
            "2026-10-10T14:05:00Z", round_no=3)]})
        after, meta = reconcile_uids(fresh, prev)
        self.assertEqual(str(after.walk("VEVENT")[0]["UID"]), "ics-old-match-uid")
        self.assertEqual(meta["ics-old-match-uid"],
                         {"prem": "294919", "round": 3})
        self.assertNotIn("prem-294919@mbligh.github.io", meta)
        self.assertEqual(str(prev.walk("VEVENT")[0]["DESCRIPTION"]),
                         "Spoiler-free rugby fixture")

    def test_official_id_retains_uid_after_major_rescheduling(self):
        past = old_event(
            "ics-original", "Northampton Saints vs Bath",
            dt.datetime(2027, 4, 17, 14, tzinfo=UTC))
        past.add("x-prem-id", "294919")
        prev = calendar(past)
        source = prem.convert_prem({"data": [fixture(
            294919, "Northampton Saints", "Bath Rugby",
            "2027-05-30T14:05:00Z", round_no=18)]})
        out, meta = reconcile_uids(source, prev)
        self.assertEqual(str(out.walk("VEVENT")[0]["UID"]), "ics-original")
        self.assertEqual(meta["ics-original"]["prem"], "294919")

    def test_full_catalogue_uses_frozen_ics_identity_and_api_round(self):
        old = calendar(old_event("abc-original", "Northampton Saints vs Bath",
                                 dt.datetime(2026, 10, 10, 14, 5, tzinfo=UTC)))
        api = prem.convert_prem({"data": [
            fixture(294919, "Northampton Saints", "Bath Rugby",
                    "2026-10-10T14:05:00Z", round_no=3),
            fixture(294950, "Saracens", "Gloucester Rugby",
                    "2027-04-17T14:00:00Z", round_no=16, tbc=1),
        ]})
        e = next(e for e in json.loads(
            (importer.RUGBY_DIR / "calendars.json").read_text())["calendars"]
            if e["slug"] == "Rugby_Mens_Prem")
        db = MagicMock()
        db.calendar_snapshots.find_one.return_value = None
        def prior(_db, name):
            return old if name == "Rugby_Mens_Prem" else calendar()
        with (patch.object(importer, "read_snapshot", side_effect=prior),
              patch.object(importer, "old_manifest", return_value={
                  "schema_version": 2, "fixtures": [], "files": {}}),
              patch.object(importer, "fetch_calendar", return_value=api)):
            snapshots, manifest, eps = importer.build_catalogue(
                db, {"calendars": [e], "timezone": "America/New_York",
                     "plex_sources": []},
                dt.datetime(2026, 11, 21, tzinfo=UTC), refresh=True)
        self.assertEqual(len(eps), 2)
        past = next(x for x in eps if x["scheduled"] is not None)
        self.assertEqual(past["_id"], "Rugby_Mens_Prem:abc-original")
        self.assertEqual((past["season"], past["round"], past["episode"]),
                         (2026, 3, 301))
        self.assertEqual(past["external_ids"]["prem"], "294919")
        self.assertIn("Rugby_Mens_Prem/Season_2026/", past["nfo_path"])
        self.assertIn("<episode>301</episode>", past["nfo"])
        future = next(x for x in eps if x["scheduled"] is None)
        self.assertEqual(future["round"], 16)
        self.assertIsNone(future["episode"])
        self.assertEqual(future["scheduled_date"], "2027-04-17")
        self.assertEqual(future["external_ids"]["prem"], "294950")
        self.assertIn("abc-original", snapshots["Rugby_Mens_Prem"])
        self.assertNotIn("finalScore", str(eps))
        self.assertNotIn("matchWinner", str(eps))

    def test_historical_external_id_survives_next_season_rollover(self):
        ep = {
            "_id": "Rugby_Mens_Prem:abc", "series_id": "Rugby_Mens_Prem",
            "round": None, "external_ids": {"rugbypass": None},
            "status": "scheduled", "season": 2026,
        }
        db = MagicMock()
        db.episodes.find.side_effect = [[], []]
        db.files.find.return_value = []
        importer.persist(db, {}, {"schema_version": 2, "fixtures": []},
                         [ep], dt.datetime(2027, 9, 1, tzinfo=UTC))
        patch_doc = db.episodes.bulk_write.call_args.args[0][0]._doc
        self.assertNotIn("external_ids", patch_doc["$set"])
        self.assertNotIn("round", patch_doc["$set"])


if __name__ == "__main__":
    unittest.main()
