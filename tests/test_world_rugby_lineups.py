"""World Rugby match-summary selections and spoiler-safe Atlas import."""
import datetime as dt
import io
import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from schedulers.rugby import world_rugby_lineups as mod

MATCH_ID = "1e74b48e-25fe-4ebe-beea-abb6cea010a0"
NOW = dt.datetime(2026, 11, 22, 18, tzinfo=dt.timezone.utc)
EP = {
    "_id": f"Rugby_Mens_Nations:world-rugby-{MATCH_ID}@mbligh.github.io",
    "series_id": "Rugby_Mens_Nations",
    "season": 2026,
    "home": "Ireland",
    "away": "Argentina",
    "scheduled": dt.datetime(2026, 11, 6, 20, 10),
}


def summary(subs=8):
    def roster(base):
        players = [{
            "player": {
                "id": str(base + n),
                "altId": f"uuid-{base+n}",
                "name": {
                    "display": f"Player {n}",
                    "first": {"known": "Player"},
                    "last": {"known": str(n)},
                },
            },
            "number": str(n),
            "position": "PR" if n <= 3 else "Rep",
            "positionLabel": "Prop" if n <= 3 else "Replacement",
            "order": n,
        } for n in reversed(range(1, 16 + subs))]
        players.insert(4, {
            "player": {"id": str(base + 100), "name": {"display": "Coach"}},
            "number": None, "positionLabel": "Coach",
        })
        return {"teamList": {"list": players}, "scoring": {"points": 42}}
    return {
        "match": {
            "matchId": MATCH_ID, "status": "C", "scores": [42, 12],
            "teams": [{"name": "Ireland"}, {"name": "Argentina"}],
        },
        "teams": [roster(1000), roster(2000)],
        "officials": [{"name": "Referee"}],
    }


class WorldRugbyLineupTests(unittest.TestCase):
    def test_fixture_id_uuid_and_legacy_ids(self):
        self.assertEqual(mod.fixture_id(EP), MATCH_ID)
        with self.assertRaises(ValueError):
            mod.fixture_id({**EP, "_id": "Rugby_Mens_Nations:old@google.com"})

    def test_23_players_coach_excluded_spoilers_stripped(self):
        record = mod.normalize(summary(), EP, NOW)
        self.assertEqual(record["_id"], EP["_id"])
        self.assertEqual(record["status"], "complete")
        self.assertEqual(len(record["home"]), 23)
        self.assertEqual(len(record["away"]), 23)
        self.assertEqual([p["shirt_number"] for p in record["home"]], list(range(1, 24)))
        self.assertEqual(record["home"][0]["name"], "Player 1")
        self.assertEqual(record["home"][0]["id"], "1001")
        self.assertEqual(record["home"][0]["first_name"], "Player")
        self.assertNotIn("Coach", [p["name"] for p in record["home"]])
        self.assertNotIn("scores", str(record))
        self.assertNotIn("scoring", str(record))
        self.assertNotIn("next_check_at", record)

    def test_extra_24_and_older_22(self):
        data = summary(subs=7)
        self.assertEqual(mod.normalize(data, EP, NOW)["status"], "complete")
        data = summary()
        data["teams"][1]["teamList"]["list"].append({
            "player": {"id": "999", "name": {"display": "Extra"}},
            "number": "24", "positionLabel": "Replacement",
        })
        record = mod.normalize(data, EP, NOW)
        self.assertEqual(len(record["away"]), 23)
        self.assertEqual(record["additional_players"]["away"][0]["id"], "999")

    def test_empty_future_match_is_unavailable(self):
        data = summary()
        for team in data["teams"]:
            team["teamList"]["list"] = []
        record = mod.normalize(data, EP, NOW)
        self.assertEqual(record["status"], "unavailable")
        self.assertGreater(record["next_check_at"], NOW)

    def test_mismatching_match_and_teams_rejected(self):
        data = summary()
        data["match"]["matchId"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        with self.assertRaises(ValueError):
            mod.normalize(data, EP, NOW)
        data = summary()
        data["match"]["teams"][1]["name"] = "South Africa"
        with self.assertRaises(ValueError):
            mod.normalize(data, EP, NOW)

    def test_known_source_duplicate_shirts_resolve_from_starter_slots(self):
        # Observed in six Nations Championship 2026 summaries.
        # Cases: Italy/Fiji, England/NZ, Scotland/Australia,
        # France/SA, Italy/SA, and South Africa/England (two teams).
        cases = ((3, 18, 5), (14, 11, 25), (12, 23, 19),
                 (2, 16, 4), (4, 19, 6), (7, 20, 11),
                 (15, 23, 26))
        for correct, erroneous, slot in cases:
            with self.subTest(correct=correct, reported=erroneous):
                data = summary()
                rows = data["teams"][0]["teamList"]["list"]
                starter = next(r for r in rows if r["number"] == str(correct))
                starter["number"] = str(erroneous)
                starter["order"] = slot
                starter["position"] = "PR" if correct < 4 else "CE"
                starter["player"]["isReplacement"] = False
                result = mod.normalize(data, EP, NOW)
                self.assertEqual(result["status"], "complete")
                fixed = next(p for p in result["home"]
                             if p["id"] == str(1000 + correct))
                self.assertEqual(fixed["shirt_number"], correct)
                self.assertEqual(fixed["reported_shirt_number"], erroneous)
                self.assertTrue(fixed["shirt_number_inferred"])
                self.assertEqual(len(result["home"]), 23)

    def test_two_independent_duplicate_corrections(self):
        data = summary()
        for side, true_number, repeated, slot, player_base in (
                (0, 7, 20, 11, 1000), (1, 15, 23, 26, 2000)):
            rows = data["teams"][side]["teamList"]["list"]
            player = next(row for row in rows if row["number"] == str(true_number))
            player["number"] = str(repeated)
            player["order"] = slot
            player["position"] = "FL" if true_number == 7 else "FB"
            player["player"]["isReplacement"] = False
        result = mod.normalize(data, EP, NOW)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["home"][6]["shirt_number"], 7)
        self.assertEqual(result["away"][14]["shirt_number"], 15)

    def test_chained_duplicate_conflict_south_africa_england(self):
        # RIMS summary 572577b5-5e95-4ad5-8fce-25da14ed11e4:
        # du Toit: reported 7, order 6 -> actual 4;
        # Hanekom: reported 20, order 11 -> actual 7;
        # Dixon: correctly assigned 20, replacement.
        data = summary()
        rows = data["teams"][0]["teamList"]["list"]
        lock = next(r for r in rows if r["number"] == "4")
        lock["number"] = "7"
        lock["order"] = 6
        lock["position"] = "SR"
        lock["player"]["isReplacement"] = False
        flanker = next(r for r in rows if r["number"] == "7" and r is not lock)
        flanker["number"] = "20"
        flanker["order"] = 11
        flanker["position"] = "FL"
        flanker["player"]["isReplacement"] = False
        substitute = next(r for r in rows if r["number"] == "20" and r is not flanker)
        substitute["position"] = "Rep"
        substitute["player"]["isReplacement"] = True

        record = mod.normalize(data, EP, NOW)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(len(record["home"]), 23)
        self.assertEqual({p["shirt_number"] for p in record["home"]},
                         set(range(1, 24)))
        by_id = {p["id"]: p for p in record["home"]}
        self.assertEqual(by_id["1004"]["shirt_number"], 4)
        self.assertEqual(by_id["1004"]["reported_shirt_number"], 7)
        self.assertTrue(by_id["1004"]["shirt_number_inferred"])
        self.assertEqual(by_id["1007"]["shirt_number"], 7)
        self.assertEqual(by_id["1007"]["reported_shirt_number"], 20)
        self.assertEqual(by_id["1020"]["shirt_number"], 20)
        self.assertNotIn("reported_shirt_number", by_id["1020"])

    def test_chained_duplicate_rejects_occupied_final_shirt(self):
        data = summary()
        rows = data["teams"][0]["teamList"]["list"]
        lock = next(r for r in rows if r["number"] == "4")
        lock["number"] = "7"
        lock["order"] = 6
        lock["position"] = "SR"
        lock["player"]["isReplacement"] = False
        flanker = next(r for r in rows if r["number"] == "7" and r is not lock)
        flanker["number"] = "20"
        flanker["order"] = 11
        flanker["position"] = "FL"
        flanker["player"]["isReplacement"] = False
        rows.append({
            "number": "4", "order": 99, "position": "Rep",
            "player": {
                "id": "9999", "name": {"display": "Unrelated extra"},
                "isReplacement": True,
            },
        })
        with self.assertRaisesRegex(ValueError, "ambiguous duplicate"):
            mod.normalize(data, EP, NOW)

    def test_cannot_guess_duplicate_if_starter_slot_is_already_occupied(self):
        data = summary()
        rows = data["teams"][0]["teamList"]["list"]
        row = next(x for x in rows if x["number"] == "3")
        row["number"] = "18"
        row["order"] = 5
        row["player"]["isReplacement"] = False
        # A second player already occupies inferred jersey #3, so this
        # is not a provably resolvable single typo.
        rows.append({
            "number": "3", "order": 99, "position": "PR",
            "player": {"id": "999", "name": {"display": "Extra"}},
        })
        with self.assertRaisesRegex(ValueError, "ambiguous duplicate"):
            mod.normalize(data, EP, NOW)

    def test_missing_match_null_response_is_unavailable(self):
        missing = {"match": None, "teams": [], "officials": [],
                   "motm": None, "mascots": None}
        result = mod.normalize(missing, EP, NOW)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(result["home"]), 0)
        self.assertEqual(len(result["away"]), 0)
        self.assertGreater(result["next_check_at"], NOW)
        # Only the exact documented empty summary shape is accepted.
        with self.assertRaises(ValueError):
            mod.normalize({"match": None, "teams": [{"teamList": {}}]}, EP, NOW)

    def test_duplicate_number_rejected(self):
        data = summary()
        data["teams"][0]["teamList"]["list"][0]["number"] = "1"
        with self.assertRaisesRegex(ValueError, "shirt"):
            mod.normalize(data, EP, NOW)

    def test_fetch_uses_summary_and_known_missing_returns_empty(self):
        with patch.object(mod.urllib.request, "urlopen",
                          return_value=io.BytesIO(json.dumps(summary()).encode())) as opened:
            data = mod.fetch(MATCH_ID)
        self.assertEqual(data["match"]["matchId"], MATCH_ID)
        self.assertIn("/summary", opened.call_args.args[0].full_url)
        missing = urllib.error.HTTPError("url", 404, "missing", None, None)
        with patch.object(mod.urllib.request, "urlopen", side_effect=missing):
            missing_data = mod.fetch(MATCH_ID)
        self.assertEqual(mod.normalize(missing_data, EP, NOW)["status"], "unavailable")
        with patch.object(mod.urllib.request, "urlopen",
                          side_effect=urllib.error.HTTPError("url", 503, "error", None, None)):
            with self.assertRaises(urllib.error.HTTPError):
                mod.fetch(MATCH_ID)

    def test_candidates_skip_legacy_completed_future_cancelled(self):
        db = MagicMock()
        legacy = {**EP, "_id": "Rugby_Mens_Nations:123@google.com"}
        future = {**EP, "_id":
            "Rugby_Mens_Nations:world-rugby-11111111-1111-4111-8111-111111111111@mbligh.github.io",
            "scheduled": dt.datetime(2027, 5, 1)}
        cancelled = {**EP, "_id":
            "Rugby_Mens_Nations:world-rugby-22222222-2222-4222-8222-222222222222@mbligh.github.io",
            "status": "cancelled"}
        db.episodes.find.return_value = [EP, legacy, future, cancelled]
        db.world_rugby_lineups.find.return_value = []
        self.assertEqual([mod.fixture_id(x) for x in mod.candidates(db, now=NOW)],
                         [MATCH_ID])
        self.assertEqual(len(mod.candidates(db, now=NOW, force=True)), 2)
        db.world_rugby_lineups.find.return_value = [{
            "_id": EP["_id"], "status": "complete",
        }]
        self.assertEqual(mod.candidates(db, now=NOW), [])

    def test_upsert_preserves_complete(self):
        db = MagicMock()
        db.world_rugby_lineups.find_one.return_value = {
            "_id": EP["_id"], "status": "complete",
            "home": [{}] * 23, "away": [{}] * 23,
        }
        data = summary()
        for team in data["teams"]:
            team["teamList"]["list"] = []
        self.assertFalse(mod.upsert(db, mod.normalize(data, EP, NOW)))
        db.world_rugby_lineups.update_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
