"""Offline tests of the source-independent rugby team/date matcher."""
import datetime as dt
import unittest
from unittest.mock import Mock

from matchers.teams_date import match_listing, match_teams_date, load_episodes

NATIONS = "Rugby_Mens_Nations"
INTL = "Rugby_Mens_Intl"
PREM = "Rugby_Mens_Prem"
CUP = "Rugby_Mens_Championship"
WOMEN = "Rugby_Womens_Intl"
PWR = "Rugby_Womens_PWR"
ALLOWED = [NATIONS, INTL, PREM, CUP, "Rugby_Mens_6N"]


def fixture(id_, series=NATIONS, home="Ireland", away="South Africa",
            date="2026-11-21", **kwargs):
    return dict(_id=id_, series_id=series, home=home, away=away,
                scheduled_date=date, season=2026, episode=603, **kwargs)


GAME = fixture("nations")
TITLE = "Ireland v South Africa 21.11.2026"


class TeamsDateTests(unittest.TestCase):
    def match(self, text=TITLE, episodes=(GAME,), series=ALLOWED, **kwargs):
        return match_teams_date(text, series, episodes, **kwargs)

    def test_title_formats(self):
        for text in (TITLE, "Ireland vs. South Africa 21/11/26 FINAL",
                     "21-11-2026 | Ireland versus South Africa",
                     "FULL MATCH | Ireland v South Africa | 2026-11-21",
                     "Ireland v South Africa - Full Match Replay - 21 November 2026",
                     "Ireland v South Africa 21.11.2026 | 21/11/2026"):
            with self.subTest(text=text):
                result = self.match(text)
                self.assertEqual(result["status"], "candidate")
                self.assertEqual(result["episode_id"], "nations")
                self.assertEqual(result["series_id"], NATIONS)
                self.assertEqual(result["episode"], 603)
                self.assertEqual(result["recording_date"], "2026-11-21")

    def test_existing_aliases_across_competitions(self):
        cases = [
            ("Northampton v Gloucester 31.10.2026", PREM,
             "Northampton Saints", "Gloucester Rugby", "2026-10-31"),
            ("Bath Rugby v Sale Sharks 31.10.2026", PREM,
             "Bath", "Sale Sharks", "2026-10-31"),
            ("Bordeaux Begles v Stade Francais 18.10.2026", CUP,
             "Union Bordeaux Bègles", "Stade Français Paris", "2026-10-18"),
            ("Northampton v Cardiff Rugby 18.10.2026", CUP,
             "Northampton Saints", "Cardiff", "2026-10-18"),
            ("Ireland v Springboks 21.11.2026", NATIONS,
             "🇮🇪 Ireland", "South Africa 🇿🇦", "2026-11-21"),
            ("USA v England 21.11.2026", INTL,
             "[RWC] United States", "England", "2026-11-21"),
        ]
        for text, series, home, away, date in cases:
            with self.subTest(text=text):
                result = self.match(text, [fixture("match", series, home, away, date)])
                self.assertEqual(result["status"], "candidate")
                self.assertEqual(result["series_id"], series)

    def test_scope_gender_and_order(self):
        female = fixture("women", WOMEN)
        self.assertEqual(self.match(episodes=[female])["status"], "no_fixture")
        self.assertEqual(self.match(episodes=[GAME, female], series=[NATIONS, WOMEN])[
            "status"], "ambiguous")
        self.assertEqual(self.match("Ireland Women v South Africa Women 21.11.2026",
                                    [GAME, female], [NATIONS, WOMEN])[
            "episode_id"], "women")
        self.assertEqual(self.match("South Africa v Ireland 21.11.2026")[
            "status"], "no_fixture")
        self.assertEqual(self.match("Ireland A v South Africa 21.11.2026")[
            "status"], "no_fixture")

    def test_womens_club_scope(self):
        game = fixture("pwr", PWR, "Gloucester Hartpury", "Saracens")
        self.assertEqual(self.match("Gloucester-Hartpury v Sarries 21.11.2026",
                                    [game], [PWR])["episode_id"], "pwr")

    def test_window_and_ambiguity(self):
        nearby = fixture("nearby", INTL, date="2026-11-22")
        far = fixture("far", INTL, date="2026-11-23")
        self.assertEqual(self.match(episodes=[nearby])["delta_days"], -1)
        self.assertEqual(self.match(episodes=[nearby], tolerance_days=0)["status"],
                         "no_fixture")
        result = self.match(episodes=[GAME, nearby])
        self.assertEqual(result["status"], "ambiguous")
        self.assertCountEqual(result["candidates"], ["nations", "nearby"])
        self.assertNotIn("episode_id", result)
        self.assertEqual(self.match(episodes=[GAME, far])["episode_id"], "nations")

    def test_unusable_fixtures(self):
        for status in ("cancelled", "superseded", "postponed", "abandoned"):
            self.assertEqual(self.match(episodes=[dict(GAME, status=status)])[
                "status"], "no_fixture")
        unnumbered = dict(GAME, episode=None)
        self.assertEqual(self.match(episodes=[unnumbered])["status"],
                         "unnumbered_fixture")
        self.assertEqual(self.match(episodes=[GAME, dict(unnumbered, _id="other")])[
            "status"], "ambiguous")

    def test_london_date_for_naive_bson_datetime(self):
        game = dict(GAME, scheduled=dt.datetime(2026, 7, 1, 23, 30),
                    scheduled_date="2026-07-01")
        result = self.match("Ireland v South Africa 02.07.2026", [game],
                            tolerance_days=0)
        self.assertEqual(result["scheduled_date"], "2026-07-02")

    def test_reject_invalid_or_incomplete_evidence(self):
        cases = {
            "Ireland v South Africa": "undated",
            "Ireland v South Africa 31.02.2026": "invalid_date",
            TITLE + " | 22.11.2026": "ambiguous_date",
            TITLE + " | 31.02.2026": "invalid_date",
            "Champions Cup Highlights 21.10.2026": "not_full_match",
            TITLE + " highlights": "not_full_match",
            "Rugby League | " + TITLE: "not_full_match",
            "Ireland U20 v South Africa U20 21.11.2026": "not_full_match",
            "Ireland v France v South Africa 21.11.2026": "invalid_teams",
            "Ireland v South Africa A 21.11.2026": "no_fixture",
        }
        for text, status in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.match(text)["status"], status)
        with self.assertRaises(ValueError):
            self.match(series=[])
        with self.assertRaises(ValueError):
            self.match(series=NATIONS)
        with self.assertRaises(ValueError):
            self.match(tolerance_days=-1)

    def test_atlas_wrapper_is_read_only(self):
        db = Mock()
        db.episodes.find.return_value = [GAME]
        result = match_listing(db, TITLE, [NATIONS])
        self.assertEqual(result["episode_id"], "nations")
        self.assertEqual(db.episodes.find.call_args.args[0],
                         {"series_id": {"$in": [NATIONS]}})
        self.assertEqual(len(db.episodes.method_calls), 1)
        self.assertEqual(load_episodes(db, [NATIONS]), [GAME])


if __name__ == "__main__":
    unittest.main()
