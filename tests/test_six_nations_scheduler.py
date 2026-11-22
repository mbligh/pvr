"""Calendar-year Six Nations numbering without guessing incomplete rounds."""
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from collections import Counter
from unittest.mock import MagicMock, patch

from icalendar import Calendar, Event
from schedulers.rugby.six_nations import fixture_rounds
from schedulers.rugby import plex_nfo
from schedulers.rugby import importer

UTC = dt.timezone.utc
ROOT = pathlib.Path(__file__).resolve().parents[1]
TEAMS = ("Ireland", "England", "Scotland", "Italy", "France", "Wales")
# Complete pairwise round-robin, all six teams play once per round.
PAIRINGS = (
    (("Ireland", "England"), ("Scotland", "Italy"), ("France", "Wales")),
    (("Ireland", "Italy"), ("England", "France"), ("Scotland", "Wales")),
    (("Ireland", "France"), ("Italy", "Wales"), ("England", "Scotland")),
    (("Ireland", "Wales"), ("France", "Scotland"), ("Italy", "England")),
    (("Ireland", "Scotland"), ("Wales", "England"), ("France", "Italy")),
)


def sample_calendar(womens=False, *, year=2027, skip=None):
    dates = (("2027-04-10", "2027-04-17", "2027-04-24", "2027-05-08", "2027-05-15")
             if womens else
             ("2027-02-05", "2027-02-13", "2027-02-20", "2027-03-05", "2027-03-13"))
    cal = Calendar()
    cal.add("version", "2.0")
    for r, (date, games) in enumerate(zip(dates, PAIRINGS), 1):
        for g, (home, away) in enumerate(games, 1):
            if skip == (r, g):
                continue
            ev = Event()
            ev.add("uid", f"6n-{year}-{r}-{g}@example.com")
            # The published feed includes flags but not round labels.
            suffix = " Women" if womens else ""
            ev.add("summary", f"{home}{suffix} 🇮🇪 vs {away}{suffix} 🇫🇷")
            ev.add("dtstart", dt.datetime.fromisoformat(date).replace(tzinfo=UTC)
                   + dt.timedelta(hours=12+g))
            ev.add("description", "Spoiler-free rugby fixture")
            cal.add_component(ev)
    return cal


def build_calendars(entries):
    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)
        docs = root / "docs"
        docs.mkdir()
        for e, cal in entries:
            (docs / (e["slug"] + ".ics")).write_bytes(cal.to_ical())
        (docs / "rugby-metadata-manifest.json").write_text(
            json.dumps({"schema_version": 2, "fixtures": []}))
        return plex_nfo.build({"calendars": [e for e, _ in entries]}, docs,
                              dt.datetime(2026, 11, 21, tzinfo=UTC))


class SixNationsTests(unittest.TestCase):
    def entries(self):
        cfg = json.loads((ROOT / "schedulers/rugby/calendars.json").read_text())
        return [e for e in cfg["calendars"]
                if e["slug"] in ("Rugby_Mens_6N", "Rugby_Womens_6N")]

    def test_configuration_uses_calendar_year_and_rounds(self):
        self.assertEqual(len(self.entries()), 2)
        for entry in self.entries():
            self.assertEqual(entry["season_mode"], "calendar")
            self.assertEqual(entry["rounds"], "six-nations")

    def test_both_championships_have_fifteen_numbered_2027_fixtures(self):
        entries = self.entries()
        output, manifest = build_calendars([
            (e, sample_calendar(womens=("Womens" in e["slug"])))
            for e in entries])
        for e in entries:
            rows = [f for f in manifest["fixtures"] if f["competition"] == e["slug"]]
            self.assertEqual(len(rows), 15)
            self.assertEqual({f["season"] for f in rows}, {2027})
            self.assertEqual(Counter(f["round"] for f in rows),
                             {r: 3 for r in range(1, 6)})
            self.assertEqual({f["episode"] for f in rows},
                             {r*100 + n for r in range(1, 6) for n in range(1, 4)})
            self.assertTrue(all(
                f["nfo"].startswith(e["slug"] + "/Season_2027/" + e["slug"] + "-s2027e")
                for f in rows))
            self.assertIn(b"<season>2027</season>", output[rows[0]["nfo"]])
            self.assertIn(b"<episode>101</episode>",
                          output[next(f["nfo"] for f in rows if f["episode"] == 101)])
            self.assertIn(e["slug"] + "/tvshow.nfo", output)
            self.assertIn(b"2027</title>", output[e["slug"] + "/tvshow.nfo"])
            self.assertNotIn(b"2027-28", output[e["slug"] + "/tvshow.nfo"])

    def test_incomplete_round_robin_is_left_unnumbered(self):
        e = self.entries()[0]
        partial = sample_calendar(skip=(2, 2))
        self.assertEqual(fixture_rounds(partial), {})
        files, manifest = build_calendars([(e, partial)])
        self.assertEqual(len(manifest["fixtures"]), 14)
        self.assertTrue(all(f["season"] == 2027 and f["episode"] is None
                            and f["nfo"] is None for f in manifest["fixtures"]))
        self.assertFalse(any(name.endswith(".nfo") for name in files))

    def test_catalogue_uses_correct_year_when_only_partial_fixtures_exist(self):
        # Regression for the previously imported 2027 6N fixtures being
        # incorrectly written into Season_2026 without round numbers.
        e = self.entries()[0]
        cal = sample_calendar(skip=(2, 2))
        empty = Calendar()
        empty.add("version", "2.0")
        db = MagicMock()
        db.calendar_snapshots.find_one.return_value = None

        def read_snapshot(_db, series):
            return cal if series == e["slug"] else empty

        with (patch.object(importer, "read_snapshot", side_effect=read_snapshot),
              patch.object(importer, "old_manifest", return_value={
                  "schema_version": 2, "fixtures": []})):
            _, _, records = importer.build_catalogue(
                db, {"timezone": "America/New_York", "calendars": [e]},
                dt.datetime(2026, 11, 21, tzinfo=UTC), refresh=False)
        self.assertEqual(len(records), 14)
        self.assertTrue(all(doc["season"] == 2027 and doc["episode"] is None
                            for doc in records))

    def test_disrupted_round_cannot_be_fabricated(self):
        cal = sample_calendar()
        ev = next(x for x in cal.walk("VEVENT")
                  if str(x.get("UID")) == "6n-2027-1-1@example.com")
        # Duplicate one country in a round, without changing fixture count.
        ev["SUMMARY"] = "Ireland 🇮🇪 vs Scotland 🇫🇷"
        self.assertEqual(fixture_rounds(cal), {})

    def test_prior_assignment_survives_partial_feed(self):
        e = self.entries()[0]
        cal = sample_calendar(skip=(2, 2))
        with tempfile.TemporaryDirectory() as temp:
            docs = pathlib.Path(temp) / "docs"
            docs.mkdir()
            (docs / (e["slug"] + ".ics")).write_bytes(cal.to_ical())
            (docs / "rugby-metadata-manifest.json").write_text(
                json.dumps({"schema_version": 2, "fixtures": [{
                    "id": e["slug"] + ":6n-2027-1-1@example.com",
                    "competition": e["slug"], "season": 2027,
                    "episode": 101, "round": 1,
                }]}))
            _, manifest = plex_nfo.build({"calendars": [e]}, docs,
                                         dt.datetime(2026, 11, 21, tzinfo=UTC))
        fixed = next(f for f in manifest["fixtures"]
                     if f["id"].endswith("1-1@example.com"))
        self.assertEqual((fixed["season"], fixed["episode"]), (2027, 101))
        self.assertEqual(sum(f["episode"] is not None for f in manifest["fixtures"]), 1)


if __name__ == "__main__":
    unittest.main()
