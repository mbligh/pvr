"""Regression tests for conservative RugbyPass national/international replay matching."""
import datetime as dt
import importlib.machinery
import io
import pathlib
import unittest

from filing.publish import canonical_paths, validate_nfo
from matchers import rugbypass as rb
from test_match_releases import FakeDB

UTC = dt.timezone.utc
ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = importlib.machinery.SourceFileLoader(
    "pvr_match_rugbypass", str(ROOT / "match_rugbypass")).load_module()


def fixture(series, home, away, scheduled, *, number=501, season=2026,
            id_=None):
    show = {
        rb.MENS: ("Rugby_Mens_Intl", "Rugby Mens Intl"),
        rb.WOMENS: ("Rugby_Womens_Intl", "Rugby Womens Intl"),
        rb.WXV: ("Rugby_Womens_WXV", "Rugby Womens WXV"),
        rb.NATIONS: ("Rugby_Mens_Nations", "Rugby Nations Championships"),
    }[series]
    root, title = show
    filename = f"{root}-s{season}e{number:03d}-{home.replace(' ', '_')}_v_{away.replace(' ', '_')}_20261113.nfo"
    nfo = f"<episodedetails><showtitle>{title}</showtitle><season>{season}</season><episode>{number}</episode></episodedetails>"
    return {"_id": id_ or f"{series}:fixture-{home}-{away}-{number}",
            "series_id": series, "home": home, "away": away,
            "scheduled": scheduled, "season": season, "episode": number,
            "nfo": nfo, "nfo_path": f"{root}/Season_{season}/{filename}"}


def release(title, when, *, id_="1051364", **fields):
    return {"_id": "rugbypass:" + id_, "source": "rugbypass",
            "title": title, "published": when, "available": True,
            "metadata": {"duration": 9000, "accessLevel": "GRANTED",
                         "onlinePlayback": "AVAILABLE"}, **fields}


class MatchNationalReplays(unittest.TestCase):
    def setUp(self):
        t = dt.datetime(2026, 11, 13, 20, 10, tzinfo=UTC)
        self.nc = fixture(rb.NATIONS, "France", "South Africa", t, number=501)
        self.duplicate = fixture(rb.MENS, "NC: France 🇫🇷",
                                 "🇿🇦 South Africa", t, number=117)
        self.rp = release(
            "France v South Africa | Nations Championship 2026 | Round 5 | Full Match Replay",
            "2026-11-13T22:48:03Z")

    def test_nations_routing_prevents_duplicate_mens_international(self):
        result = rb.match_release(self.rp, [self.nc, self.duplicate])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["episode_id"], self.nc["_id"])
        self.assertEqual(result["series_id"], rb.NATIONS)
        self.assertEqual(result["delay_hours"], 2.634)

    def test_womens_international_and_wxv_replays(self):
        women = fixture(rb.WOMENS, "Hong Kong China", "Tonga",
                        dt.datetime(2026, 11, 21, 6, tzinfo=UTC), number=120)
        rp = release(
            "Hong Kong China v Tonga | Women’s International | Full Match Replay",
            "2026-11-21T08:13:11Z", id_="1054041")
        self.assertEqual(rb.match_release(rp, [women])["episode_id"], women["_id"])
        wxv = fixture(rb.WXV, "South Africa", "Ireland",
                      dt.datetime(2026, 10, 31, 13, tzinfo=UTC))
        legacy = fixture(rb.WOMENS, "South Africa", "Ireland",
                         dt.datetime(2026, 10, 31, 13, tzinfo=UTC))
        wxv_release = release(
            "South Africa v Ireland | WXV Global Series 2026 | Full Match Replay",
            "2026-10-31T16:00:00Z", id_="1122334")
        self.assertEqual(
            rb.match_release(wxv_release, [legacy])["status"], "no_fixture")
        self.assertEqual(
            rb.match_release(wxv_release, [wxv, legacy])["episode_id"], wxv["_id"])

    def test_mens_explicit_label_and_country_nicknames(self):
        game = fixture(rb.MENS, "New Zealand 🇳🇿", "🇦🇺 Australia",
                       dt.datetime(2026, 10, 10, 6, 10, tzinfo=UTC))
        release_ = release("All Blacks v Wallabies | Men's International | Full Match Replay",
                           "2026-10-10T08:20:00Z")
        self.assertEqual(rb.match_release(release_, [game])["episode_id"], game["_id"])
        cup = fixture(rb.MENS, "Georgia", "Tonga",
                      dt.datetime(2026, 11, 14, 13, tzinfo=UTC))
        title = "Georgia v Tonga | Nations Cup 2026 | Round 5 | Full Match Replay"
        self.assertEqual(rb.match_release(release(title, "2026-11-14T16:00:00Z"), [cup])["episode_id"], cup["_id"])

    def test_country_suffix_a_and_xv_are_not_dropped(self):
        self.assertNotEqual(rb.normalize_team("England A"), rb.normalize_team("England"))
        self.assertNotEqual(rb.normalize_team("Argentina XV"), rb.normalize_team("Argentina"))
        a = fixture(rb.MENS, "USA", "Australia A", dt.datetime(2026, 10, 31, tzinfo=UTC))
        r = release("USA v Australia | Men's International | Full Match Replay",
                    "2026-10-31T02:00:00Z")
        self.assertEqual(rb.match_release(r, [a])["status"], "no_fixture")

    def test_no_gender_guess_from_unlabelled_match(self):
        self.assertIsNone(rb.identify_replay("France v South Africa | Rugby Match | Full Match Replay"))
        self.assertIsNone(rb.identify_replay("France v South Africa | Nations Championship 2026 | Highlights"))
        self.assertIsNone(rb.identify_replay("France v South Africa | Nations Championship 2026 | Match Highlights"))
        self.assertIsNone(rb.identify_replay("Whistle Watch | Nations Championship 2026 | Full Match Replay"))
        self.assertIsNone(rb.identify_replay("France v South Africa | Nations Championship 2026 | Round 5 | Preview"))

    def test_ambiguous_womens_duplicate_feed_is_not_matched(self):
        when = dt.datetime(2026, 10, 17, 1, tzinfo=UTC)
        a = fixture(rb.WXV, "Australia", "Scotland", when, number=102, id_="w1")
        b = fixture(rb.WXV, "♀️Australia 🇦🇺", "🏴󠁧󠁢󠁳󠁣󠁴󠁿 Scotland",
                    when + dt.timedelta(hours=1), number=103, id_="w2")
        vod = release("Australia v Scotland | WXV Global Series 2026 | Full Match Replay",
                      "2026-10-17T04:30:00Z")
        result = rb.match_release(vod, [a, b])
        self.assertEqual(result["status"], "ambiguous")
        self.assertCountEqual(result["candidates"], ["w1", "w2"])

    def test_date_window_blocks_old_duplicate_replays(self):
        for published in ("2026-11-13T14:00:00Z",
                          "2026-11-17T23:00:00Z", "2025-11-13T22:48:03Z"):
            with self.subTest(published=published):
                result = rb.match_release(
                    dict(self.rp, published=published), [self.nc])
                self.assertEqual(result["status"], "no_fixture")
        self.assertEqual(rb.match_release(
            dict(self.rp, published="2026-11-13"), [self.nc])["status"], "undated")

    def test_unnumbered_nations_fixture_never_links(self):
        unnumbered = dict(self.nc, episode=None, nfo=None, nfo_path=None)
        result = rb.match_release(self.rp, [unnumbered])
        self.assertEqual(result["status"], "unnumbered_fixture")
        self.assertNotIn("episode_id", result)

    def test_unavailable_short_non_rugbypass_are_excluded(self):
        cases = [
            (dict(self.rp, available=False), "unavailable"),
            (dict(self.rp, source="youtube"), "not_rugbypass"),
            (dict(self.rp, metadata={"duration": 1200}), "short_recording"),
            (dict(self.rp, metadata={"accessLevel": "NOT_GRANTED"}), "unavailable"),
        ]
        for vod, status in cases:
            with self.subTest(status=status):
                self.assertEqual(rb.match_release(vod, [self.nc])["status"], status)

    def test_proper_canonical_paths_and_nfo_validation(self):
        for series, title, root in [
            (rb.MENS, "Rugby Mens Intl", "Rugby_Mens_Intl"),
            (rb.WOMENS, "Rugby Womens Intl", "Rugby_Womens_Intl"),
            (rb.WXV, "Rugby Womens WXV", "Rugby_Womens_WXV"),
            (rb.NATIONS, "Rugby Nations Championships", "Rugby_Mens_Nations")]:
            item = fixture(series, "Canada", "England",
                           dt.datetime(2026, 11, 1, tzinfo=UTC))
            self.assertTrue(validate_nfo(item))
            target, nfo = canonical_paths(item, "/data/sports")
            self.assertEqual(target.suffix, ".mkv")
            self.assertEqual(nfo.parts[3], root)
            wrong = dict(item, nfo=item["nfo"].replace(title, "Other Rugby"))
            with self.assertRaises(ValueError):
                validate_nfo(wrong)

    def test_cli_preview_apply_and_idempotence(self):
        db = FakeDB([self.nc, self.duplicate], [self.rp])
        preview = CLI.run(db, stream=io.StringIO(), summary_stream=io.StringIO())
        self.assertEqual(preview.get("candidate"), 1)
        self.assertEqual(db.releases.writes, [])
        apply = CLI.run(db, apply=True, stream=io.StringIO(), summary_stream=io.StringIO())
        self.assertEqual(apply["linked"], 1)
        self.assertEqual(db.releases.documents[0]["episode_id"], self.nc["_id"])
        self.assertEqual(db.releases.documents[0]["match_details"]["competition"], rb.NATIONS)
        repeated = CLI.run(db, apply=True, stream=io.StringIO(), summary_stream=io.StringIO())
        self.assertEqual(repeated["already_linked"], 1)
        self.assertEqual(len(db.releases.writes), 1)
        db2 = FakeDB([self.duplicate], [dict(self.rp, episode_id="manual:record")])
        summary = CLI.run(db2, apply=True, stream=io.StringIO(), summary_stream=io.StringIO())
        self.assertEqual(summary["existing_link"], 1)
        self.assertEqual(db2.releases.writes, [])


if __name__ == "__main__":
    unittest.main()
