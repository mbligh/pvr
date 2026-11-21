"""Offline tests for canonical NFO filing and non-destructive Atlas updates."""
import datetime as dt
import errno
import importlib.machinery
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from filing import publish as publisher

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = importlib.machinery.SourceFileLoader(
    "pvr_file_recording", str(ROOT / "file_recording")
).load_module()

SOURCE_URI = "youtube:8CFBWPSG3vc"
EP_ID = "rugby-womens-pwr:pwr-289127@mbligh.github.io"
PREFIX = "Rugby_Womens_PWR/Season_2025/"
STEM = "Rugby_Womens_PWR-s2025e102-Trailfinders_Women_v_Exeter_Chiefs_20251025"
NFO = """<?xml version='1.0' encoding='utf-8'?>
<episodedetails>
  <title>Trailfinders Women vs Exeter Chiefs - 2025/10/25</title>
  <showtitle>Rugby Womens PWR</showtitle>
  <season>2025</season>
  <episode>102</episode>
  <aired>2025-10-25</aired>
  <plot>Rugby Womens PWR, Round 1. Venue: Trailfinders Sports Club</plot>
</episodedetails>
"""
EPISODE = {
    "_id": EP_ID, "season": 2025, "episode": 102,
    "series_id": "rugby-womens-pwr",
    "nfo_path": PREFIX + STEM + ".nfo", "nfo": NFO,
}
MEDIA = {
    "duration_seconds": 8745.048, "video": {"codec": "h264", "fps": 60,
                                             "width": 1920, "height": 1080},
    "audio_tracks": 1,
}


def fixture(tmp):
    root = pathlib.Path(tmp)
    sports = root / "sports"
    sports.mkdir()
    source = root / "incoming" / "pvr-8CFBWPSG3vc.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video data" * 1024)
    return sports, source


class PathTests(unittest.TestCase):
    def test_canonical_media_path_and_exact_nfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            sports, source = fixture(tmp)
            video, nfo = publisher.canonical_paths(EPISODE, sports)
            self.assertEqual(video, sports / PREFIX / (STEM + ".mkv"))
            self.assertEqual(nfo, sports / PREFIX / (STEM + ".nfo"))
            self.assertEqual(publisher.validate_nfo(EPISODE), NFO.encode())
            self.assertEqual(ET.fromstring(NFO).findtext("episode"), "102")

    def test_reject_wrong_series_number_paths_and_xml(self):
        for bad in (
            "../Rugby_Womens_PWR/Season_2025/" + STEM + ".nfo",
            "Rugby_Womens_PWR/Season_2024/" + STEM + ".nfo",
            "Rugby_Womens_PWR/Season_2025/untrusted.nfo",
            "/tmp/" + STEM + ".nfo",
            "Rugby_Womens_PWR/Season_2025/" + STEM + ".xml",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                publisher.canonical_paths(dict(EPISODE, nfo_path=bad),
                                          "/data/sports")
        for bad_xml in (
            NFO.replace("<episode>102</episode>", "<episode>103</episode>"),
            NFO.replace("Rugby Womens PWR", "Unrelated Show"),
            "<broken>",
        ):
            with self.subTest(xml=bad_xml), self.assertRaises(ValueError):
                publisher.validate_nfo(dict(EPISODE, nfo=bad_xml))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sports, self.source = fixture(self.tmp.name)
        self.video, self.nfo = publisher.canonical_paths(EPISODE, self.sports)

    def probe(self, path, *, binary):
        self.assertEqual(binary, "ffprobe")
        self.assertEqual(path.read_bytes(), self.source.read_bytes())
        return MEDIA

    def test_hardlink_publication_and_original_retained(self):
        media, snapshot, method = publisher.publish(
            self.source, self.video, self.nfo, NFO.encode(),
            sports_root=self.sports, probe=self.probe)
        self.assertEqual(media, MEDIA)
        self.assertEqual(method, "hardlink")
        self.assertEqual(self.video.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.nfo.read_bytes(), NFO.encode())
        self.assertTrue(self.source.exists())
        publisher.ensure_unchanged(self.source, snapshot)
        self.assertEqual(os.stat(self.source).st_ino, os.stat(self.video).st_ino)

    def test_existing_destination_or_sidecar_never_overwritten(self):
        self.video.parent.mkdir(parents=True)
        for occupied in (self.video, self.nfo):
            occupied.write_bytes(b"not ours")
            with self.assertRaises(FileExistsError):
                publisher.publish(self.source, self.video, self.nfo, NFO.encode(),
                                  sports_root=self.sports, probe=self.probe)
            self.assertEqual(occupied.read_bytes(), b"not ours")
            self.assertTrue(self.source.exists())
            occupied.unlink()

    def test_probe_error_leaves_source_and_no_published_artifacts(self):
        def fail_probe(path, *, binary):
            raise ValueError("invalid media")
        with self.assertRaisesRegex(ValueError, "invalid media"):
            publisher.publish(self.source, self.video, self.nfo, NFO.encode(),
                              sports_root=self.sports, probe=fail_probe)
        self.assertFalse(self.video.exists())
        self.assertFalse(self.nfo.exists())
        self.assertTrue(self.source.exists())

    def test_nfo_write_failure_rolls_back_only_new_video(self):
        with patch.object(publisher, "_write_exclusive", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                publisher.publish(self.source, self.video, self.nfo, NFO.encode(),
                                  sports_root=self.sports, probe=self.probe)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.video.exists())

    def test_cross_filesystem_copy_is_staged_and_verified(self):
        real_link = os.link
        def link(src, dst, **kw):
            if pathlib.Path(src) == self.source:
                raise OSError(errno.EXDEV, "Cross-device link")
            return real_link(src, dst, **kw)
        with patch.object(publisher.os, "link", side_effect=link):
            media, snapshot, method = publisher.publish(
                self.source, self.video, self.nfo, NFO.encode(),
                sports_root=self.sports, probe=self.probe)
        self.assertEqual(method, "copy")
        self.assertNotEqual(os.stat(self.source).st_ino, os.stat(self.video).st_ino)
        self.assertEqual(media, MEDIA)
        self.assertEqual(list(self.video.parent.glob(".pvr-*")), [])
        publisher.ensure_unchanged(self.source, snapshot)

    def test_moved_source_changed_snapshot_is_not_deleted(self):
        saved = publisher.stat_identity(self.source)
        self.source.write_bytes(b"different contents")
        with self.assertRaisesRegex(ValueError, "changed during filing"):
            publisher.ensure_unchanged(self.source, saved)
        self.assertTrue(self.source.exists())

    def test_destination_directory_symlink_rejected(self):
        self.video.parent.parent.mkdir()
        real = self.sports / "outside"
        real.mkdir()
        self.video.parent.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            publisher.publish(self.source, self.video, self.nfo, NFO.encode(),
                              sports_root=self.sports, probe=self.probe)


class FilingCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sports, self.source = fixture(self.tmp.name)
        self.destination, self.nfo = publisher.canonical_paths(EPISODE, self.sports)
        self.file_id = "slagg:" + str(self.source)
        self.original = {
            "_id": self.file_id, "host": "slagg", "path": str(self.source),
            "series_id": "rugby-womens-pwr", "season": 2025, "episode": 102,
            "episode_id": EP_ID, "release_uri": SOURCE_URI,
            "size_bytes": self.source.stat().st_size,
            "mtime_ns": self.source.stat().st_mtime_ns,
            "media": MEDIA, "first_seen": dt.datetime.now(dt.timezone.utc),
        }
        self.db = MagicMock()
        def lookup(query, *args, **kwargs):
            if query["_id"] == self.file_id:
                return dict(self.original)
            return None
        self.db.files.find_one.side_effect = lookup
        self.db.episodes.find_one.return_value = dict(EPISODE)
        self.db.files.update_one.return_value.matched_count = 1
        self.db.jobs.update_one.return_value.modified_count = 1

    def run_cli(self, *, apply=False, publisher_override=None):
        if publisher_override is None:
            publisher_override = self.publisher
        return CLI.run(self.db, self.source, sports_root=str(self.sports),
                       hostname="slagg", apply=apply,
                       publisher=publisher_override, stream=io.StringIO())

    def publisher(self, source, destination, nfo_path, content, *, sports_root, ffprobe):
        return publisher.publish(source, destination, nfo_path, content,
                                 sports_root=sports_root,
                                 probe=lambda path, binary: MEDIA)

    def test_preview_does_not_create_directories_or_write_atlas(self):
        result = self.run_cli()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["episode"], 102)
        self.assertEqual(result["destination"], str(self.destination))
        self.assertEqual(result["nfo_path"], str(self.nfo))
        self.assertFalse(self.destination.parent.exists())
        self.db.files.insert_one.assert_not_called()
        self.db.jobs.insert_one.assert_not_called()

    def test_apply_filing_creates_canonical_video_nfo_provenance_and_job(self):
        result = self.run_cli(apply=True)
        self.assertEqual(result["status"], "filed")
        self.assertTrue(self.destination.is_file())
        self.assertEqual(self.nfo.read_text(), NFO)
        self.assertFalse(self.source.exists())
        target_row = self.db.files.insert_one.call_args.args[0]
        self.assertEqual(target_row["_id"], "slagg:" + str(self.destination))
        self.assertEqual(target_row["release_uri"], SOURCE_URI)
        self.assertEqual(target_row["previous_file_id"], self.file_id)
        self.assertEqual(target_row["media"], MEDIA)
        old_update = self.db.files.update_one.call_args.args[1]["$set"]
        self.assertEqual(old_update["file_state"], "moved")
        self.assertEqual(old_update["moved_to"], target_row["_id"])
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
                         "succeeded")

    def test_destination_collision_does_not_create_job(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"already present")
        result = self.run_cli(apply=True)
        self.assertEqual(result["status"], "destination_exists")
        self.assertEqual(self.destination.read_bytes(), b"already present")
        self.assertTrue(self.source.exists())
        self.db.jobs.insert_one.assert_not_called()

    def test_reject_stale_inventory_size_or_time(self):
        self.original["size_bytes"] -= 1
        with self.assertRaisesRegex(ValueError, "differs from Atlas"):
            self.run_cli()
        self.original["size_bytes"] += 1
        self.original["mtime_ns"] -= 1
        with self.assertRaisesRegex(ValueError, "differs from Atlas"):
            self.run_cli()

    def test_reject_nfo_metadata_conflict(self):
        self.db.episodes.find_one.return_value = dict(
            EPISODE, nfo=NFO.replace("<episode>102</episode>", "<episode>101</episode>"))
        with self.assertRaisesRegex(ValueError, "NFO episode identity"):
            self.run_cli()

    def test_failed_atlas_destination_insert_retains_source_and_publication(self):
        self.db.files.insert_one.side_effect = RuntimeError("Atlas insert failed")
        with self.assertRaisesRegex(RuntimeError, "Atlas insert failed"):
            self.run_cli(apply=True)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.destination.exists())
        self.assertTrue(self.nfo.exists())
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
                         "failed")
        self.assertTrue(self.db.jobs.update_one.call_args.args[1]["$set"]
                        ["requires_reconciliation"])

    def test_failed_source_state_change_retains_both(self):
        self.db.files.update_one.return_value.matched_count = 0
        with self.assertRaisesRegex(ValueError, "Source inventory changed"):
            self.run_cli(apply=True)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.destination.exists())
        self.assertTrue(self.nfo.exists())
        self.assertTrue(self.db.files.insert_one.called)

    def test_publisher_failure_retains_original_without_db_move(self):
        def fail(*args, **kwargs):
            raise RuntimeError("media damaged")
        with self.assertRaisesRegex(RuntimeError, "media damaged"):
            self.run_cli(apply=True, publisher_override=fail)
        self.db.files.insert_one.assert_not_called()
        self.db.files.update_one.assert_not_called()
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
