"""Strict, preview-first PREM 2026/27 historical fixture repair."""
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from icalendar import Calendar, Event

from schedulers.rugby import importer, prem, prem_history
from schedulers.rugby.prem_identity import reconcile_uids


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, tzinfo=UTC)
# Official match IDs and score-free fixture identity/date allowlist.
MATCHES = [
    ("Harlequins", "Bath Rugby", "2026-09-25T18:45:00Z"),
    ("Northampton Saints", "Newcastle Red Bulls", "2026-09-25T18:45:00Z"),
    ("Exeter Chiefs", "Gloucester Rugby", "2026-09-26T14:05:00Z"),
    ("Sale Sharks", "Bristol Bears", "2026-09-26T16:30:00Z"),
    ("Leicester Tigers", "Saracens", "2026-09-27T14:00:00Z"),
    ("Bath Rugby", "Exeter Chiefs", "2026-10-02T18:45:00Z"),
    ("Bristol Bears", "Northampton Saints", "2026-10-03T14:05:00Z"),
    ("Gloucester Rugby", "Harlequins", "2026-10-03T16:30:00Z"),
    ("Newcastle Red Bulls", "Leicester Tigers", "2026-10-03T18:45:00Z"),
    ("Saracens", "Sale Sharks", "2026-10-04T14:00:00Z"),
    ("Leicester Tigers", "Gloucester Rugby", "2026-10-09T18:45:00Z"),
    ("Northampton Saints", "Bath Rugby", "2026-10-10T14:05:00Z"),
    ("Saracens", "Bristol Bears", "2026-10-10T16:30:00Z"),
    ("Sale Sharks", "Harlequins", "2026-10-11T14:00:00Z"),
    ("Exeter Chiefs", "Newcastle Red Bulls", "2026-10-11T14:00:00Z"),
]


def official_source():
    matches = []
    for i, (home, away, when) in enumerate(MATCHES):
        matches.append({
            "id": 294908 + i, "compId": 1011, "season": 202601,
            "round": 1 + i // 5, "date": when, "tbc": 0,
            "homeTeam": {"name": home, "score": {"finalScore": 100}},
            "awayTeam": {"name": away, "score": {"finalScore": 0}},
            "venue": {"name": "Ground"}, "matchWinner": "home",
        })
    return prem.convert_prem({"data": matches})


def existing_round_three():
    prior = Calendar()
    prior.add("version", "2.0")
    # Simulate the four accepted fixtures (294919..294922); their UID is
    # older than the official PREM switch, and must remain unchanged.
    for i in range(11, 15):
        home, away, when = MATCHES[i]
        start = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        uid = "prior-" + str(294908 + i)
        event = Event()
        event.add("uid", uid)
        event.add("summary", home + " vs " + away)
        event.add("dtstart", start)
        event.add("dtend", start + dt.timedelta(hours=2))
        event.add("description", "Spoiler-free fixture")
        prior.add_component(event)
    return prior


def previous_manifest():
    return {
        "schema_version": 2,
        "fixtures": [
            {"id": "Rugby_Mens_Prem:prior-" + str(294919 + i),
             "competition": "Rugby_Mens_Prem",
             "season": 2026, "round": 3, "episode": 301 + i}
            for i in range(4)
        ],
        "files": {},
    }


class PremHistoryTests(unittest.TestCase):
    def test_validated_rounds_and_missing_past_only(self):
        source = official_source()
        prem_history.checked_rounds(source, NOW)
        prior = existing_round_three()
        source, metadata = reconcile_uids(source, prior)
        merged, added = prem_history.add_history(source, prior, NOW)
        self.assertEqual(added, 11)
        merged, again = prem_history.add_history(source, merged, NOW)
        self.assertEqual(again, 0)
        self.assertEqual(len(merged.walk("VEVENT")), 15)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            damaged = prem.convert_prem({"data": [
                {"id": 294908, "compId": 1011, "season": 202601,
                 "round": 1, "date": MATCHES[0][2],
                 "homeTeam": {"name": MATCHES[0][0]},
                 "awayTeam": {"name": MATCHES[0][1]}}]})
            prem_history.checked_rounds(damaged, NOW)

    def test_integration_retains_fixture_ids_and_renumbers_round_three(self):
        prior = existing_round_three()
        source = official_source()
        db = MagicMock()
        with (patch.object(importer, "read_snapshot", return_value=prior),
              patch.object(importer, "old_manifest", return_value=previous_manifest()),
              patch.object(importer, "fetch_calendar", return_value=source)):
            snapshots, manifest, episodes = importer.build_catalogue(
                db, {"calendars": [{"slug": "Rugby_Mens_Prem",
                    "name": "Rugby Mens Prem", "type": "prem", "rounds": "prem"}],
                    "timezone": "America/New_York", "plex_sources": []},
                NOW, refresh=True, backfill_prem_2026=True)
        self.assertEqual(len(episodes), 15)
        numbers = {ep["external_ids"]["prem"]: ep["episode"] for ep in episodes}
        self.assertEqual(sorted(numbers.values()),
                         [101, 102, 103, 104, 105, 201, 202, 203, 204, 205,
                          301, 302, 303, 304, 305])
        self.assertEqual(numbers["294918"], 301)
        self.assertEqual(numbers["294919"], 302)
        self.assertEqual(numbers["294922"], 304)
        self.assertEqual(numbers["294921"], 305)
        self.assertTrue(any(ep["_id"] == "Rugby_Mens_Prem:prior-294919"
                            and ep["episode"] == 302 for ep in episodes))
        self.assertEqual(len(manifest["fixtures"]), 15)
        self.assertNotIn("finalScore", str(episodes))
        self.assertNotIn("matchWinner", str(snapshots))
        self.assertIn("prior-294919", snapshots["Rugby_Mens_Prem"])

    def test_preflight_refuses_conflicting_media_and_protects_ids(self):
        proposed = [
            {"_id": "Rugby_Mens_Prem:" + str(i), "series_id": "Rugby_Mens_Prem",
             "season": 2026, "round": i // 100, "episode": i,
             "external_ids": {"prem": str(294908 + (i // 100 - 1) * 5 + i % 100 - 1)}}
            for i in [*range(101, 106), *range(201, 206), *range(301, 306)]
        ]
        db = MagicMock()
        db.episodes.find.return_value = []
        db.files.find.return_value = []
        report = prem_history.validate_plan(db, proposed)
        self.assertEqual(report["missing"], 15)
        self.assertEqual(report["renumbered"], 0)
        old = dict(proposed[-1], episode=301)
        db.episodes.find.return_value = [old]
        db.files.find.return_value = [{"_id": "recorded"}]
        with self.assertRaisesRegex(ValueError, "existing recordings"):
            prem_history.validate_plan(db, proposed)
        db.files.find.return_value = []
        self.assertEqual(prem_history.validate_plan(db, proposed)["renumbered"], 1)
        db.episodes.find.return_value = [dict(old, external_ids={"prem": "wrong"})]
        with self.assertRaisesRegex(ValueError, "official fixture identity"):
            prem_history.validate_plan(db, proposed)

    def test_round_three_prior_manifest_is_only_one_cleared(self):
        base = previous_manifest()
        base["fixtures"].append({
            "id": "Rugby_Mens_Nations:keep",
            "competition": "Rugby_Mens_Nations", "round": 3,
            "season": 2026, "episode": 301})
        out = prem_history.strip_round_three_assignments(base)
        self.assertEqual(len(out["fixtures"]), 1)
        self.assertEqual(len(base["fixtures"]), 5)
        with self.assertRaisesRegex(ValueError, "requires live"):
            importer.build_catalogue(None, {}, NOW, refresh=False,
                                     backfill_prem_2026=True)


if __name__ == "__main__":
    unittest.main()
