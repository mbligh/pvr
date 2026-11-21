"""Offline checks for the read-only F1 Reddit release selector."""
import datetime as dt
import importlib.machinery
import io
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selection.f1_releases import make_candidate, rank_releases, summarize

cli = importlib.machinery.SourceFileLoader(
    "pvr_select_f1_releases_test", str(ROOT / "select_f1_releases")).load_module()

TODAY = dt.date(2026, 11, 21)


def release(identifier, commentary, height, *, sky=False, available=None,
            published="2026-11-21", audio=None, url=True, episode_id="f1:race"):
    record = {
        "_id": "reddit-teletz:" + identifier,
        "uri": "reddit-teletz:" + identifier, "source": "reddit-teletz",
        "episode_id": episode_id,
        "published": published,
        "metadata": {
            "url": ("https://www.transfernow.net/dl/" + identifier) if url else "",
            "commentary": commentary, "contains_sky_audio": sky,
            "height": height, "audio_variant": audio, "uploader": "MWR",
            "preference_rank": 99,  # Selector must NOT blindly trust this.
        },
    }
    if available is not None:
        record["available"] = available
    return record


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def find(self, query, projection=None):
        self.calls.append((query, projection))
        out = []
        for row in self.rows:
            ok = True
            for key, value in query.items():
                actual = row.get(key)
                if isinstance(value, dict) and "$in" in value:
                    if actual not in value["$in"]:
                        ok = False
                elif actual != value:
                    ok = False
            if ok:
                out.append(dict(row))
        return out


class FakeDB:
    def __init__(self, episodes, releases):
        self.episodes = FakeCollection(episodes)
        self.releases = FakeCollection(releases)


def data():
    return FakeDB([
        {"_id": "f1:race", "series_id": "formula-1", "season": 2026,
         "episode": 217, "round": 21, "title": "Las Vegas Grand Prix - Race"},
        {"_id": "f1:qualifying", "series_id": "formula-1", "season": 2026,
         "episode": 216, "round": 21, "title": "Las Vegas Grand Prix - Qualifying"},
        {"_id": "f1:practice", "series_id": "formula-1", "season": 2026,
         "episode": 211, "round": 21, "title": "Las Vegas Grand Prix - Practice 1"},
        {"_id": "rugby:fixture", "series_id": "rugby-womens-pwr", "season": 2026,
         "episode": 217, "round": 21, "title": "Rugby"}], [
        release("sky1080aa", "sky", 1080),
        release("multi4kaaa", "multi", 2160, sky=True),
        release("f1tv1080a", "f1tv", 1080),
        release("sky720aaaa", "sky", 720),
        release("dutch1080", "dutch", 1080),
        release("nourlaaaaa", "sky", 2160, url=False),
        release("blockedaaa", "sky", 2160, available=False),
        release("quali1080a", "f1tv", 1080, episode_id="f1:qualifying"),
        release("rugby1080a", "sky", 1080, episode_id="rugby:fixture"),
    ])


class F1ReleaseSelectorTests(unittest.TestCase):
    def test_sky_beats_multi_4k_and_ignores_indexer_rank(self):
        ranked = rank_releases(data().releases.rows[:5], today=TODAY)
        self.assertEqual(ranked[0]["uri"], "reddit-teletz:sky1080aa")
        self.assertTrue(ranked[0]["selected"])
        self.assertEqual([r["preference"] for r in ranked], [0, 0, 1, 2, 3])
        self.assertFalse(any(r["selected"] for r in ranked[1:]))

    def test_unavailable_and_invalid_url_excluded_not_hidden(self):
        rows = [release("badsource1", "sky", 2160, available=False),
                release("badsource2", "sky", 2160, url=False),
                release("goodmulti1", "multi", 1080, sky=True)]
        ranked = rank_releases(rows, today=TODAY)
        self.assertEqual(ranked[0]["uri"], "reddit-teletz:goodmulti1")
        self.assertEqual(sum(not r["selectable"] for r in ranked), 2)
        self.assertTrue(ranked[0]["selected"])

    def test_preference_order_fallback(self):
        ranked = rank_releases([release("lowdutch1", "dutch", 1080),
                                release("f1stereo1", "f1tv", 1080),
                                release("multino11", "multi", 2160, sky=False)],
                               today=TODAY)
        self.assertEqual(ranked[0]["uri"], "reddit-teletz:f1stereo1")

    def test_no_verified_link_is_not_selected(self):
        rows = [release("badurl123", "sky", 1080, url=False),
                release("unavail12", "sky", 1080, available=False)]
        ranked = rank_releases(rows, today=TODAY)
        self.assertIsNone(next((r for r in ranked if r["selected"]), None))

    def test_freshness_is_warning_not_claim_of_expiration(self):
        entry = make_candidate(release("olderlink1", "sky", 1080,
                                       published="2026-10-11"), TODAY)
        self.assertTrue(entry["selectable"])
        self.assertIn("not checked", entry["warning"])
        self.assertNotIn("expired", entry["warning"].lower())

    def test_deterministic_tiebreak_freshness_before_uri(self):
        rows = [release("aoldlink10", "sky", 1080, published="2026-11-10"),
                release("znewlink10", "sky", 1080, published="2026-11-20")]
        self.assertEqual(rank_releases(rows, today=TODAY)[0]["uri"],
                         "reddit-teletz:znewlink10")

    def test_groups_and_summary_without_writes(self):
        db = data()
        report = summarize(db, year=2026, today=TODAY)
        stats = report["summary"]
        self.assertEqual(stats["episodes"], 3)
        self.assertEqual(stats["episodes_with_releases"], 2)
        self.assertEqual(stats["episodes_without_releases"], 1)
        self.assertEqual(stats["release_options"], 8)
        self.assertEqual(stats["excluded_options"], 2)
        self.assertEqual(stats["episodes_with_selection"], 2)
        self.assertEqual(len(report["episodes"]), 2)
        self.assertEqual(report["episodes"][0]["episode"], 216)
        self.assertEqual(report["episodes"][1]["episode"], 217)
        self.assertNotIn("write", str(db.episodes.calls))
        self.assertEqual(db.releases.calls[0][0]["source"], "reddit-teletz")
        self.assertEqual(db.releases.calls[0][0]["episode_id"]["$in"],
                         ["f1:practice", "f1:qualifying", "f1:race"])

    def test_filter_and_include_missing(self):
        db = data()
        report = summarize(db, year=2026, episode_number=211,
                           include_missing=True, today=TODAY)
        self.assertEqual(len(report["episodes"]), 1)
        self.assertIsNone(report["episodes"][0]["selected_uri"])
        self.assertEqual(report["summary"]["episodes_without_releases"], 1)
        self.assertEqual(report["summary"]["release_options"], 0)

    def test_unknown_filter_rejected(self):
        with self.assertRaisesRegex(ValueError, "No F1 episodes"):
            summarize(data(), year=2026, round_no=99, today=TODAY)

    def test_text_preview_has_all_options_and_summary(self):
        report = summarize(data(), year=2026, today=TODAY)
        output = io.StringIO()
        cli.render(report, stream=output)
        rendered = output.getvalue()
        self.assertIn("S2026E217", rendered)
        self.assertIn("[SELECT] Dedicated Sky commentary", rendered)
        self.assertIn("multi4kaaa", rendered)
        self.assertIn("have NOT been verified", rendered)


if __name__ == "__main__":
    unittest.main()
