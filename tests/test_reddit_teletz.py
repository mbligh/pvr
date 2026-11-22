"""Offline regression tests for Reddit teletz discovery and PVR ingest."""
import importlib.machinery
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import reddit_teletz as parser

importer = importlib.machinery.SourceFileLoader(
    "pvr_teletz_import_test", str(ROOT / "import_teletz")).load_module()

LAS_VEGAS = """SKY Commentary / world feed* 1080p - F1Series
https://www.transfernow.net/dl/20261121rg0NQzAH [Stream/DL]
*Sky commentary over the World Feed keeping FOM's original race coverage uninterrupted.
"""

MULTIPLE = """**F1TV** Commentary 1080p - BILLIE
5.1: https://www.transfernow.net/dl/20261011n311fggU [DL]
Stereo: https://www.transfernow.net/dl/20261011mJrVVQxc [Stream/DL]
Choose Stereo for browser viewing.

**SKY** Commentary 1080p - MWR
https://www.transfernow.net/dl/20261011ou17WFlZ [Stream/DL]

**DUTCH** (GPR) 1080p
https://www.transfernow.net/dl/202610115J6dEhxX [Stream/DL]

**MULTi** Commentary 1080p - MWR (Modified by teletz)
https://www.transfernow.net/dl/202610110ICkmf6l [DL]
**English Sky, English F1TV, Deutsch, Francais, Espanol, NL GPR, track**

**MULTi** Commentary 4k - MWR (Modified by teletz)
https://www.transfernow.net/dl/202610111kXpUHGB [DL]
**English Sky, English F1TV, Deutsch, Francais, Espanol, NL GPR, track**

**F1TV** **! Pre-Race Show !** 1080p - MWR
https://www.transfernow.net/dl/20261011Yn2KON1v [Stream/DL]
Fixed the previous link.

**F1TV** **! Post-Race Show !** 1080p - MWR
https://www.transfernow.net/dl/202610110L1qlhk5 [Stream/DL]
"""


def comment(body=LAS_VEGAS, title="[04:00 UTC] 2026 F1 - Las Vegas Grand Prix - Qualifying"):
    return {
        "id": "t1_19syofxxo8qat",
        "author": "teletz", "subreddit": "MotorsportsReplays",
        "depth": 0, "parent_author": "AutoModerator",
        "post_id": "t3_1y1nilb", "post_title": title,
        "url": "https://www.reddit.com/r/MotorsportsReplays/comments/1y1nilb/comment/19syofxxo8qat/?context=3",
        "created": "2026-11-21T08:55:08.716000+0000",
        "body": body, "links": [],
    }


class RedditTeletzTests(unittest.TestCase):
    def test_simple_sky_release_and_provenance(self):
        bundle = parser.process_comments([comment()])[0]
        self.assertEqual(bundle["observation"]["_id"], "reddit:comment:19syofxxo8qat")
        self.assertEqual(bundle["observation"]["body"], LAS_VEGAS)
        self.assertEqual(len(bundle["releases"]), 1)
        release = bundle["releases"][0]
        self.assertEqual(release["uri"], "reddit-teletz:20261121rg0NQzAH")
        self.assertEqual(release["metadata"]["commentary"], "sky")
        self.assertEqual(release["metadata"]["height"], 1080)
        self.assertEqual(release["metadata"]["preference_rank"], 0)
        self.assertEqual(release["metadata"]["delivery"], "stream/download")

    def test_multi_option_classification_excludes_extra_shows(self):
        releases = parser.release_records(comment(
            MULTIPLE, "2026 F1 - United States Grand Prix - Race"))
        self.assertEqual(len(releases), 6)
        by_id = {r["id"]: r for r in releases}
        self.assertEqual(by_id["20261011n311fggU"]["metadata"]["audio_variant"], "5.1")
        self.assertEqual(by_id["20261011mJrVVQxc"]["metadata"]["audio_variant"], "stereo")
        self.assertEqual(by_id["20261011ou17WFlZ"]["metadata"]["preference_rank"], 0)
        self.assertEqual(by_id["202610110ICkmf6l"]["metadata"]["preference_rank"], 1)
        self.assertEqual(by_id["202610111kXpUHGB"]["metadata"]["height"], 2160)
        self.assertTrue(by_id["202610111kXpUHGB"]["metadata"]["contains_sky_audio"])
        self.assertEqual(by_id["20261011n311fggU"]["metadata"]["preference_rank"], 2)
        self.assertEqual(by_id["202610115J6dEhxX"]["metadata"]["preference_rank"], 3)
        self.assertNotIn("20261011Yn2KON1v", by_id)
        self.assertNotIn("202610110L1qlhk5", by_id)

    def test_href_only_label_is_recovered(self):
        raw = comment("**SKY** Commentary 1080p - MWR\n[Stream/DL]")
        raw["links"] = [{"label": "[Stream/DL]",
                         "url": "https://www.transfernow.net/dl/20261011ou17WFlZ"}]
        releases = parser.release_records(raw)
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["metadata"]["commentary"], "sky")

    def test_only_verified_automoderator_top_level(self):
        invalid = comment()
        invalid["parent_author"] = "someone-else"
        self.assertEqual(parser.process_comments([invalid]), [])
        invalid = comment()
        invalid["depth"] = 1
        self.assertEqual(parser.process_comments([invalid]), [])

    def test_thread_matches_only_unambiguous_episode(self):
        episodes = [
            {"_id": "Formula_1:11416", "season": 2026, "session_code": "q",
             "meeting_name": "Las Vegas Grand Prix"},
            {"_id": "Formula_1:999", "season": 2026, "session_code": "ss",
             "meeting_name": "British Grand Prix"},
        ]
        self.assertEqual(parser.match_episode(comment()["post_title"], episodes),
                         "Formula_1:11416")
        self.assertEqual(parser.match_episode(
            "2026 F1 - Great Britain Grand Prix - Sprint Qualifying", episodes), "Formula_1:999")
        self.assertIsNone(parser.match_episode(
            "2026 F1 - Las Vegas Grand Prix - Pre-Race Show", episodes))
        self.assertIsNone(parser.match_episode(
            "2026 F1 - United States Grand Prix - Qualifying", episodes))

    def test_edited_comment_adds_new_link_without_new_observation(self):
        original = comment()
        changed = comment(LAS_VEGAS + "\nhttps://www.transfernow.net/dl/20261011ou17WFlZ")
        self.assertNotEqual(parser.observation(original)["content_sha256"],
                            parser.observation(changed)["content_sha256"])
        self.assertEqual(parser.observation(original)["_id"],
                         parser.observation(changed)["_id"])
        self.assertEqual(len(parser.release_records(changed)), 2)

    def test_duplicate_transfer_url_keeps_observation_provenance(self):
        first = parser.process_comments([comment()])[0]
        second = comment()
        second["id"] = "t1_anothercomment"
        second["body"] = LAS_VEGAS
        bundles = [first, parser.process_comments([second])[0]]
        obs, releases, provenance = importer.read_bundles(
            io.StringIO("\n".join(json.dumps(b) for b in bundles)))
        self.assertEqual(len(obs), 2)
        self.assertEqual(len(releases), 1)
        uri = "reddit-teletz:20261121rg0NQzAH"
        self.assertEqual(len(provenance[uri]), 2)
        self.assertEqual(len(releases[uri]["metadata"]["observation_ids"]), 2)

    def test_malformed_bundle_is_rejected(self):
        bundle = parser.process_comments([comment()])[0]
        bundle["releases"][0]["metadata"]["observation_id"] = "reddit:comment:wrong"
        with self.assertRaisesRegex(ValueError, "Invalid TransferNow"):
            importer.read_bundles(io.StringIO(json.dumps(bundle) + "\n"))

    def test_release_only_deduplicates_reposted_link(self):
        other = comment()
        other["id"] = "t1_anothercomment"
        with tempfile.TemporaryDirectory() as path:
            fixture = Path(path) / "reddit.json"
            fixture.write_text(json.dumps({"comments": [comment(), other]}))
            run = subprocess.run(
                [sys.executable, str(ROOT / "indexers/reddit_teletz"),
                 "--input", str(fixture)],
                capture_output=True, text=True, check=True)
            records = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(len(records[0]["metadata"]["observation_ids"]), 2)
            validated = subprocess.run(
                [sys.executable, str(ROOT / "index_releases"), "--dry-run"],
                input=run.stdout, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(validated.stdout)["valid"], 1)

    def test_cli_bundle_is_compatible_with_index_releases(self):
        with tempfile.TemporaryDirectory() as path:
            fixture = Path(path) / "reddit.json"
            fixture.write_text(json.dumps({"comments": [comment(MULTIPLE)]}))
            run = subprocess.run(
                [sys.executable, str(ROOT / "indexers/reddit_teletz"),
                 "--input", str(fixture), "--bundle"],
                capture_output=True, text=True, check=True)
            bundles = [json.loads(line) for line in run.stdout.splitlines()]
            self.assertEqual(len(bundles), 1)
            self.assertEqual(len(bundles[0]["releases"]), 6)
            preview = subprocess.run(
                [sys.executable, str(ROOT / "import_teletz")],
                input=run.stdout, capture_output=True, text=True, check=True)
            result = json.loads(preview.stdout)
            self.assertEqual(result["observations"], 1)
            self.assertEqual(result["releases"], 6)
            self.assertTrue(result["preview"])

            raw = subprocess.run(
                [sys.executable, str(ROOT / "indexers/reddit_teletz"),
                 "--input", str(fixture)],
                capture_output=True, text=True, check=True)
            validation = subprocess.run(
                [sys.executable, str(ROOT / "index_releases"), "--dry-run"],
                input=raw.stdout, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(validation.stdout)["valid"], 6)


if __name__ == "__main__":
    unittest.main()
