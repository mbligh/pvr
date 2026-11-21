"""Offline tests for conservative PWR release-to-episode matching."""
import datetime as dt
import importlib.machinery
import io
import unittest
from pathlib import Path

from matchers import pwr

ROOT = Path(__file__).resolve().parents[1]
matcher_cli = importlib.machinery.SourceFileLoader(
    "pvr_match_releases", str(ROOT / "match_releases")
).load_module()

UTC = dt.timezone.utc
PWR = pwr.SERIES_ID


def fixture(id_, date, home="Loughborough Lightning", away="Trailfinders Women",
            season=2026, number=101):
    return {"_id": id_, "series_id": PWR, "season": season, "episode": number,
            "home": home, "away": away, "scheduled": date}


def release(uri, title, published, **kwargs):
    source, id_ = uri.split(":", 1)
    return {"_id": uri, "source": source, "id": id_, "title": title,
            "published": published, **kwargs}


GAME = fixture("rugby-womens-pwr:pwr-295034@mbligh.github.io",
               dt.datetime(2026, 11, 20, 19, 30, tzinfo=UTC))
YOUTUBE = release(
    "youtube:bdxiq73pR-I",
    "LIVE JAECOO PWR | Loughborough Lightning vs Trailfinders Women",
    "2026-11-20T19:14:59+00:00",
    metadata={"date_basis": "release_timestamp"},
)
BBC = release("bbc:m0034hxp",
              "2026/27: Loughborough Lightning v Trailfinders",
              "2026-11-20")


class PWRMatcherTests(unittest.TestCase):
    def test_all_nine_canonical_teams_and_variants(self):
        self.assertEqual(len(pwr.TEAMS), 9)
        for team in pwr.TEAMS:
            self.assertEqual(pwr.known_team(team), team)
        self.assertEqual(pwr.known_team("Gloucester Hartpury"), "Gloucester-Hartpury")
        self.assertEqual(pwr.known_team("Trailfinders"), "Trailfinders Women")
        self.assertEqual(pwr.known_team("Saracens Women"), "Saracens")
        self.assertEqual(pwr.known_team("Ealing Trailfinders"), "Trailfinders Women")
        self.assertIsNone(pwr.known_team("England"))

    def test_teams_from_full_title(self):
        cases = [
            ("LIVE JAECOO PWR | Loughborough Lightning vs Trailfinders Women",
             ("Loughborough Lightning", "Trailfinders Women")),
            ("2026/27: Loughborough Lightning v Trailfinders",
             ("Loughborough Lightning", "Trailfinders Women")),
            ("Gloucester-Hartpury vs Saracens Women | Full Match Replay",
             ("Gloucester-Hartpury", "Saracens")),
            ("LIVE | Round 4 | Bristol Bears v Exeter Chiefs",
             ("Bristol Bears", "Exeter Chiefs")),
            ("PWR | Sale Sharks versus Leicester Tigers",
             ("Sale Sharks", "Leicester Tigers")),
            ("Harlequins vs Loughborough Lightning",
             ("Harlequins", "Loughborough Lightning")),
        ]
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(pwr.title_teams(title), expected)
        for title in (
            "Loughborough Lightning vs Trailfinders Women | Highlights",
            "JAECOO PWR NEXT GEN CUP FINAL | Saracens vs Trailfinders Women",
            "Preview | Saracens vs Trailfinders",
            "Bristol Bears v England",
            "Leicester Tigers Highlights",
            "Gloucester-Hartpury v Saracens v Bristol Bears",
        ):
            with self.subTest(title=title):
                self.assertIsNone(pwr.title_teams(title))

    def test_bbc_and_youtube_match_same_canonical_fixture(self):
        for rec in (BBC, YOUTUBE):
            result = pwr.match_release(rec, [GAME])
            self.assertEqual(result["status"], "candidate")
            self.assertEqual(result["episode_id"], GAME["_id"])
            self.assertEqual(result["season"], 2026)
            self.assertEqual(result["episode"], 101)
            self.assertEqual(result["delta_days"], 0)

    def test_pwr_2025_recording_cannot_match_2026_season(self):
        old = release("youtube:old", YOUTUBE["title"],
                      "2025-11-20T19:15:00Z")
        result = pwr.match_release(old, [GAME])
        self.assertEqual(result["status"], "no_fixture")
        self.assertNotIn("episode_id", result)

    def test_matching_respects_home_away_and_date(self):
        reversed_fixture = fixture("reversed", GAME["scheduled"],
                                   "Trailfinders Women", "Loughborough Lightning")
        self.assertEqual(pwr.match_release(YOUTUBE, [reversed_fixture])["status"],
                         "no_fixture")
        outside = fixture("other", dt.datetime(2026, 11, 24, 19, tzinfo=UTC))
        self.assertEqual(pwr.match_release(YOUTUBE, [outside])["status"], "no_fixture")
        self.assertEqual(pwr.match_release(YOUTUBE, [outside], 5)["status"],
                         "candidate")

    def test_ambiguous_fixtures_are_not_automatically_selected(self):
        second = fixture("another", dt.datetime(2026, 11, 21, 19, tzinfo=UTC))
        result = pwr.match_release(YOUTUBE, [GAME, second])
        self.assertEqual(result["status"], "ambiguous")
        self.assertCountEqual(result["candidates"], [GAME["_id"], "another"])

    def test_restricted_undated_date_only_and_london_midnight(self):
        self.assertEqual(pwr.match_release(dict(YOUTUBE, available=False), [GAME])["status"],
                         "unavailable")
        self.assertEqual(pwr.match_release(dict(YOUTUBE, published=None), [GAME])["status"],
                         "undated")
        self.assertEqual(pwr.recording_date(
            dict(YOUTUBE, published="2026-07-01T23:30:00+00:00")),
            dt.date(2026, 7, 2))
        self.assertEqual(pwr.fixture_date({
            "scheduled_date": "2026-11-20"}), dt.date(2026, 11, 20))


class MemoryCollection:
    def __init__(self, documents):
        self.documents = [dict(d) for d in documents]
        self.writes = []

    def find(self, query, projection=None):
        source = query.get("source")
        if isinstance(source, dict):
            sources = source["$in"]
            return [d for d in self.documents if d.get("source") in sources]
        if isinstance(source, str):
            return [d for d in self.documents if d.get("source") == source]
        return list(self.documents)

    def update_one(self, query, update):
        self.writes.append((query, update))
        matches = [d for d in self.documents if d["_id"] == query["_id"]
                   and d.get("episode_id") is None]
        if matches:
            matches[0].update(update["$set"])
        return type("Result", (), {"modified_count": len(matches)})()


class FakeDB:
    def __init__(self, episodes, releases):
        self.episodes = MemoryCollection(episodes)
        self.releases = MemoryCollection(releases)


class ReleaseLinkTests(unittest.TestCase):
    def test_preview_then_apply_idempotently_and_preserve_existing_data(self):
        rec = dict(YOUTUBE, first_seen="preserved", download_status="done")
        db = FakeDB([GAME], [rec, BBC])
        out = io.StringIO()
        info = matcher_cli.run(db, stream=out, summary_stream=io.StringIO())
        self.assertEqual(info["candidate"], 2)
        self.assertEqual(len(db.releases.writes), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        applied = matcher_cli.run(db, apply=True, stream=io.StringIO(),
                                  summary_stream=io.StringIO())
        self.assertEqual(applied["linked"], 2)
        self.assertEqual(len(db.releases.writes), 2)
        self.assertEqual(db.releases.documents[0]["episode_id"], GAME["_id"])
        self.assertEqual(db.releases.documents[1]["episode_id"], GAME["_id"])
        self.assertEqual(db.releases.documents[0]["first_seen"], "preserved")
        self.assertEqual(db.releases.documents[0]["download_status"], "done")
        self.assertEqual(db.releases.documents[0]["match_details"]["date_basis"],
                         "release_timestamp")
        repeated = matcher_cli.run(db, apply=True, stream=io.StringIO(),
                                   summary_stream=io.StringIO())
        self.assertEqual(repeated["already_linked"], 2)
        self.assertEqual(len(db.releases.writes), 2)

    def test_manual_or_conflicting_link_is_not_overwritten(self):
        db = FakeDB([GAME], [dict(YOUTUBE, episode_id="manual:fixture")])
        results = matcher_cli.run(db, apply=True, stream=io.StringIO(),
                                  summary_stream=io.StringIO())
        self.assertEqual(results["existing_link"], 1)
        self.assertEqual(db.releases.documents[0]["episode_id"], "manual:fixture")
        self.assertEqual(len(db.releases.writes), 0)

    def test_unmatched_historical_video_remains_unlinked(self):
        older = dict(YOUTUBE, _id="youtube:old", published="2025-11-20T19:15:00Z")
        db = FakeDB([GAME], [older])
        report = matcher_cli.run(db, apply=True, stream=io.StringIO(),
                                 summary_stream=io.StringIO())
        self.assertEqual(report["no_fixture"], 1)
        self.assertEqual(len(db.releases.writes), 0)


if __name__ == "__main__":
    unittest.main()
