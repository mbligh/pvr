"""F1 canonical library filing tests using Atlas-shaped download records."""
import datetime as dt
import importlib.machinery
import io
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from filing import publish as publisher
from selection.f1_files import minimum_duration, select_f1_episode

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = importlib.machinery.SourceFileLoader(
    "pvr_file_recording_f1", str(ROOT / "file_recording")).load_module()

EP_ID = "f1:11416"
RELEASE_URI = "reddit-teletz:20261121rg0NQzAH"
RELATIVE = ("Formula_1/Season_2026/"
            "Formula_1-s2026e216-Las_Vegas_Grand_Prix_Qualifying_20261120")
NFO = """<?xml version='1.0' encoding='utf-8'?>
<episodedetails>
  <title>Las Vegas Grand Prix - Qualifying</title>
  <showtitle>Formula 1</showtitle>
  <season>2026</season>
  <episode>216</episode>
  <aired>2026-11-20</aired>
  <plot>Formula 1 2026, Round 21: Las Vegas Grand Prix, Qualifying.</plot>
</episodedetails>
"""
EPISODE = {
    "_id": EP_ID, "series_id": "formula-1",
    "season": 2026, "episode": 216, "duration": 3600,
    "title": "Las Vegas Grand Prix - Qualifying",
    "nfo_path": RELATIVE + ".nfo", "nfo": NFO,
}
MEDIA = {"duration_seconds": 4852.723, "video": {
    "height": 1080, "width": 1920, "codec": "h264", "fps": 50.0},
    "audio_tracks": 1, "audio_codecs": ["aac"]}
RELEASE = {"_id": RELEASE_URI, "episode_id": EP_ID,
           "source": "reddit-teletz",
           "metadata": {"commentary": "sky", "height": 1080}}


class Formula1PathTests(unittest.TestCase):
    def test_canonical_paths_are_taken_from_atlas(self):
        video, sidecar = publisher.canonical_paths(EPISODE, "/data/sports")
        self.assertEqual(str(video), "/data/sports/" + RELATIVE + ".mkv")
        self.assertEqual(str(sidecar), "/data/sports/" + RELATIVE + ".nfo")
        self.assertEqual(publisher.validate_nfo(EPISODE), NFO.encode())

    def test_invalid_f1_paths_and_nfo_fail_closed(self):
        variants = [
            "../" + RELATIVE + ".nfo",
            RELATIVE.replace("Season_2026", "Season_2025") + ".nfo",
            RELATIVE.replace("Formula_1/", "Rugby_Womens_PWR/", 1) + ".nfo",
            "Formula_1/Season_2026/random-name.nfo",
            "/tmp/" + pathlib.Path(RELATIVE).name + ".nfo",
        ]
        for value in variants:
            with self.subTest(path=value), self.assertRaises(ValueError):
                publisher.canonical_paths(dict(EPISODE, nfo_path=value), "/data/sports")
        with self.assertRaisesRegex(ValueError, "NFO episode identity"):
            publisher.validate_nfo(dict(EPISODE, nfo=NFO.replace(
                "<showtitle>Formula 1</showtitle>",
                "<showtitle>Rugby Womens PWR</showtitle>")))
        with self.assertRaisesRegex(ValueError, "NFO episode identity"):
            publisher.validate_nfo(dict(EPISODE, nfo=NFO.replace(
                "<episode>216</episode>", "<episode>215</episode>")))
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            publisher.canonical_paths(dict(EPISODE, series_id="unknown"), "/data/sports")

    def test_short_sprint_qualifying_is_allowed_but_tiny_file_is_not(self):
        episode = dict(EPISODE, duration=2700)
        self.assertEqual(minimum_duration(episode), 1350)
        self.assertEqual(minimum_duration(dict(EPISODE, duration=0)), 900)


class Formula1FilingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.sports = root / "sports"
        self.sports.mkdir()
        self.source = root / "incoming" / "pvr-20261121rg0NQzAH.mkv"
        self.source.parent.mkdir()
        self.source.write_bytes(b"video" * 2048)
        self.target, self.sidecar = publisher.canonical_paths(EPISODE, self.sports)
        self.source_id = "slagg:" + str(self.source)
        self.original = {
            "_id": self.source_id, "host": "slagg", "path": str(self.source),
            "series_id": "formula-1", "episode_id": EP_ID,
            "season": 2026, "episode": 216,
            "release_uri": RELEASE_URI,
            "size_bytes": self.source.stat().st_size,
            "mtime_ns": self.source.stat().st_mtime_ns,
            "media": dict(MEDIA),
            "first_seen": dt.datetime.now(dt.timezone.utc),
        }
        self.db = MagicMock()
        self.db.files.find_one.side_effect = lambda query, *a, **k: (
            dict(self.original) if query["_id"] == self.source_id else None)
        self.db.episodes.find_one.return_value = dict(EPISODE)
        self.db.files.find.return_value = [self.original]
        self.db.releases.find.return_value = [dict(RELEASE)]
        self.db.files.update_one.return_value.matched_count = 1

    def pub(self, source, destination, nfo_path, content, *, sports_root, ffprobe):
        return publisher.publish(source, destination, nfo_path, content,
                                 sports_root=sports_root,
                                 probe=lambda path, binary: MEDIA)

    def run_cli(self, apply=False):
        return CLI.run(self.db, self.source, hostname="slagg",
                       sports_root=str(self.sports), apply=apply,
                       publisher=self.pub, stream=io.StringIO())

    def test_preview_is_read_only_and_uses_f1_naming(self):
        plan = self.run_cli()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["series_id"], "formula-1")
        self.assertEqual(plan["destination"], str(self.target))
        self.assertEqual(plan["nfo_path"], str(self.sidecar))
        self.assertEqual(plan["preferred_file_id"], self.source_id)
        self.assertFalse(self.target.parent.exists())
        self.assertTrue(self.source.exists())
        self.db.files.insert_one.assert_not_called()
        self.db.jobs.insert_one.assert_not_called()

    def test_apply_preserves_provenance_and_marks_moved(self):
        result = self.run_cli(apply=True)
        self.assertEqual(result["status"], "filed")
        self.assertEqual(result["transfer"], "hardlink")
        self.assertEqual(self.target.read_bytes(), b"video" * 2048)
        self.assertEqual(self.sidecar.read_bytes(), NFO.encode())
        self.assertFalse(self.source.exists())
        doc = self.db.files.insert_one.call_args.args[0]
        self.assertEqual(doc["_id"], "slagg:" + str(self.target))
        self.assertEqual(doc["series_id"], "formula-1")
        self.assertEqual(doc["episode_id"], EP_ID)
        self.assertEqual(doc["release_uri"], RELEASE_URI)
        self.assertEqual(doc["previous_file_id"], self.source_id)
        self.assertEqual(doc["nfo_path"], str(self.sidecar))
        self.assertEqual(self.db.files.update_one.call_args.args[1]["$set"]
                         ["file_state"], "moved")
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]
                         ["status"], "succeeded")

    def test_retry_after_success_is_idempotent(self):
        self.run_cli(apply=True)
        original = dict(self.original, file_state="moved",
                        moved_to="slagg:" + str(self.target))
        target_doc = {"_id": "slagg:" + str(self.target), "episode_id": EP_ID,
                      "path": str(self.target), "size_bytes": self.target.stat().st_size}
        self.db.files.find_one.side_effect = lambda query, *a, **k: (
            original if query["_id"] == self.source_id else
            target_doc if query["_id"] == target_doc["_id"] else None)
        self.db.jobs.insert_one.reset_mock()
        result = self.run_cli(apply=True)
        self.assertEqual(result["status"], "already_filed")
        self.db.jobs.insert_one.assert_not_called()

    def test_destination_collision_does_not_change_original(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"existing")
        self.assertEqual(self.run_cli(apply=True)["status"], "destination_exists")
        self.assertTrue(self.source.exists())
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.db.jobs.insert_one.assert_not_called()

    def test_failure_retains_original_media(self):
        self.db.files.insert_one.side_effect = RuntimeError("Atlas unavailable")
        with self.assertRaisesRegex(RuntimeError, "Atlas unavailable"):
            self.run_cli(apply=True)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.target.exists())
        self.assertTrue(self.sidecar.exists())

    def test_incomplete_f1_session_is_not_filed(self):
        self.original["media"] = dict(MEDIA, duration_seconds=900)
        self.assertEqual(self.run_cli()["status"], "ineligible")
        self.assertTrue(self.source.exists())
        self.db.jobs.insert_one.assert_not_called()

    def test_wrong_series_or_stale_file_is_rejected(self):
        self.db.episodes.find_one.return_value = dict(EPISODE, series_id="rugby-womens-pwr")
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.run_cli()
        self.db.episodes.find_one.return_value = dict(EPISODE)
        self.original["size_bytes"] -= 1
        with self.assertRaisesRegex(ValueError, "differs from Atlas"):
            self.run_cli()

    def test_sky_first_over_larger_multi_when_filing(self):
        second = self.source.parent / "alternate.mkv"
        second.write_bytes(b"multi" * 5000)
        larger = dict(self.original, _id="slagg:" + str(second),
                      path=str(second), size_bytes=second.stat().st_size,
                      mtime_ns=second.stat().st_mtime_ns,
                      release_uri="reddit-teletz:multi123456")
        self.db.files.find.return_value = [self.original, larger]
        self.db.releases.find.return_value = [dict(RELEASE), {
            "_id": "reddit-teletz:multi123456", "episode_id": EP_ID,
            "source": "reddit-teletz", "metadata": {
                "commentary": "multi", "contains_sky_audio": True}}]
        report = select_f1_episode(self.db, EPISODE, "slagg")
        self.assertEqual(report["preferred_file_id"], self.source_id)
        self.assertEqual(self.run_cli()["status"], "ready")
        self.assertTrue(self.source.exists())

    def test_existing_published_video_blocks_filing_another_one(self):
        already = self.sports / "Formula_1/Season_2026/another.mkv"
        already.parent.mkdir(parents=True)
        already.write_bytes(b"published")
        self.db.files.find.return_value = [self.original, {
            "_id": "slagg:" + str(already), "host": "slagg", "path": str(already),
            "series_id": "formula-1", "season": 2026, "episode": 216,
            "episode_id": EP_ID, "size_bytes": already.stat().st_size,
            "mtime_ns": already.stat().st_mtime_ns,
            "release_uri": RELEASE_URI, "media": MEDIA,
        }]
        self.assertEqual(self.run_cli()["status"], "already_published")
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
