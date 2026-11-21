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

    def test_symlinked_sports_root_is_followed(self):
        # /data/sports is legitimately a symlink on slagg. Its visible
        # path should be preserved while the publisher writes to its target.
        root_link = pathlib.Path(self.tmp.name) / "sports-link"
        root_link.symlink_to(self.sports, target_is_directory=True)
        video, nfo = publisher.canonical_paths(EPISODE, root_link)
        media, snapshot, method = publisher.publish(
            self.source, video, nfo, NFO.encode(),
            sports_root=root_link, probe=self.probe)
        self.assertEqual(media, MEDIA)
        self.assertEqual(method, "hardlink")
        self.assertEqual(video.read_bytes(), self.source.read_bytes())
        self.assertEqual(nfo.read_bytes(), NFO.encode())
        self.assertTrue(self.source.exists())
        publisher.ensure_unchanged(self.source, snapshot)
        self.assertEqual(video.resolve(), self.video.resolve())

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
        self.db.files.find.return_value = [self.original]
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

    def test_international_and_nations_series_use_canonical_quality_filing(self):
        cases = [
            ("rugby-mens-intl", "Rugby_Mens_Intl", "Rugby Mens Intl"),
            ("rugby-womens-intl", "Rugby_Womens_Intl", "Rugby Womens Intl"),
            ("rugby-nations", "Rugby_Nations", "Rugby Nations Championships"),
        ]
        for series_id, dirname, showtitle in cases:
            with self.subTest(series=series_id):
                ep_id = f"{series_id}:fixture-1"
                stem = f"{dirname}-s2026e501-France_v_South_Africa_20261113"
                self.db.episodes.find_one.return_value = {
                    "_id": ep_id, "series_id": series_id,
                    "season": 2026, "episode": 501,
                    "nfo_path": f"{dirname}/Season_2026/{stem}.nfo",
                    "nfo": (f"<episodedetails><showtitle>{showtitle}</showtitle>"
                            "<season>2026</season><episode>501</episode>"
                            "</episodedetails>"),
                }
                self.original.update(series_id=series_id, episode_id=ep_id,
                                     season=2026, episode=501,
                                     release_uri="rugbypass:1051364")
                self.db.files.find.return_value = [dict(self.original)]
                preview = self.run_cli()
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["series_id"], series_id)
                self.assertTrue(preview["destination"].endswith(stem + ".mkv"))
        # Actually publish one of the new series to exercise Atlas mutation.
        completed = self.run_cli(apply=True)
        self.assertEqual(completed["status"], "filed")
        self.assertEqual(completed["series_id"], "rugby-nations")
        self.assertEqual(self.db.files.insert_one.call_args.args[0]["series_id"],
                         "rugby-nations")
        self.assertFalse(self.source.exists())

    def test_new_existing_rugby_directories_can_be_planned_without_creation(self):
        # A directory whitelist must not silently exclude the user's
        # established Six Nations, Lions, World Cup or WXV series when
        # numbered/NFO-backed episodes become available.
        for series_id, dirname, title in (
                ("rugby-mens-6n", "Rugby_Mens_6N", "Rugby Mens 6N"),
                ("rugby-mens-lions", "Rugby_Mens_Lions", "Rugby Mens Lions"),
                ("rugby-mens-world-cup", "Rugby_Mens_World_Cup", "Rugby Mens World Cup"),
                ("rugby-womens-6n", "Rugby_Womens_6N", "Rugby Womens 6N"),
                ("rugby-womens-lions", "Rugby_Womens_Lions", "Rugby Womens Lions"),
                ("rugby-womens-world-cup", "Rugby_Womens_World_Cup", "Rugby Womens World Cup"),
                ("rugby-womens-wxv", "Rugby_Womens_WXV", "Rugby Womens WXV"),
        ):
            with self.subTest(series_id=series_id):
                episode_id = f"{series_id}:test"
                stem = f"{dirname}-s2026e101-Canada_v_England_20261017"
                self.db.episodes.find_one.return_value = {
                    "_id": episode_id, "series_id": series_id,
                    "season": 2026, "episode": 101,
                    "nfo_path": f"{dirname}/Season_2026/{stem}.nfo",
                    "nfo": (f"<episodedetails><showtitle>{title}</showtitle>"
                            "<season>2026</season><episode>101</episode>"
                            "</episodedetails>"),
                }
                self.original.update(series_id=series_id, episode_id=episode_id,
                                     season=2026, episode=101)
                self.db.files.find.return_value = [dict(self.original)]
                preview = self.run_cli()
                self.assertEqual(preview["status"], "ready")
                self.assertEqual(preview["series_id"], series_id)
                self.assertEqual(
                    preview["destination"],
                    str(self.sports / dirname / "Season_2026" / (stem + ".mkv")))
                self.assertFalse((self.sports / dirname).exists())
        self.db.jobs.insert_one.assert_not_called()

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

    def test_repeated_successful_filing_reports_already_filed(self):
        result = self.run_cli(apply=True)
        self.assertEqual(result["status"], "filed")
        original = dict(self.original, file_state="moved",
                        moved_to="slagg:" + str(self.destination))
        destination_record = {
            "_id": "slagg:" + str(self.destination),
            "episode_id": EP_ID, "path": str(self.destination),
            "size_bytes": self.destination.stat().st_size,
        }
        def lookup(query, *args, **kwargs):
            if query["_id"] == self.file_id:
                return original
            if query["_id"] == destination_record["_id"]:
                return destination_record
            return None
        self.db.files.find_one.side_effect = lookup
        self.db.jobs.insert_one.reset_mock()
        repeat = self.run_cli(apply=True)
        self.assertEqual(repeat["status"], "already_filed")
        self.assertFalse(self.source.exists())
        self.assertTrue(self.destination.exists())
        self.db.jobs.insert_one.assert_not_called()

    def test_already_filed_needs_exact_nfo_and_file_verification(self):
        destination_record = {
            "_id": "slagg:" + str(self.destination),
            "episode_id": EP_ID, "path": str(self.destination),
            "size_bytes": 3,
        }
        original = dict(self.original, file_state="moved",
                        moved_to=destination_record["_id"])
        self.source.unlink()
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"bad")
        self.nfo.write_bytes(b"wrong")
        def lookup(query, *args, **kwargs):
            if query["_id"] == self.file_id:
                return original
            return destination_record
        self.db.files.find_one.side_effect = lookup
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.run_cli()

    def test_existing_library_file_for_same_episode_blocks_promotion(self):
        # A broadcaster file may have a different filename than the
        # canonical target; it still prevents an unselected YouTube
        # recording from becoming the preferred media-library file.
        preferred = self.sports / "Rugby_Womens_PWR" / "Season_2025" / "bbc-alternative.mkv"
        preferred.parent.mkdir(parents=True)
        preferred.write_bytes(b"x" * (self.source.stat().st_size + 1000))
        self.db.files.find.return_value = [self.original, {
            "_id": "slagg:" + str(preferred),
            "episode_id": EP_ID, "host": "slagg", "path": str(preferred),
            "size_bytes": preferred.stat().st_size,
            "mtime_ns": preferred.stat().st_mtime_ns,
            "media": MEDIA,
        }]
        plan = self.run_cli()
        self.assertEqual(plan["status"], "already_published")
        self.assertEqual(plan["existing_published_files"], [str(preferred)])
        result = self.run_cli(apply=True)
        self.assertEqual(result["status"], "already_published")
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())
        self.db.jobs.insert_one.assert_not_called()

    def test_other_episode_or_stale_library_record_does_not_block(self):
        other = self.sports / "Rugby_Womens_PWR" / "Season_2026" / "other.mkv"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"BBC recording of a DIFFERENT fixture")
        self.db.files.find.return_value = [self.original]
        self.assertEqual(self.run_cli()["status"], "ready")
        self.db.files.find.return_value = [self.original, {
            "_id": "slagg:missing",
            "episode_id": EP_ID, "host": "slagg",
            "path": str(self.sports / "Rugby_Womens_PWR/Season_2025/gone.mkv"),
            "size_bytes": 100000,
        }]
        self.assertEqual(self.run_cli()["status"], "ready")

    def test_ineligible_short_or_low_resolution_file_is_not_filed(self):
        for changes in ({"duration_seconds": 3599}, {"video": {
                "codec": "h264", "fps": 60, "width": 1280, "height": 719}}):
            with self.subTest(changes=changes):
                self.original["media"] = dict(MEDIA, **changes)
                self.assertEqual(self.run_cli()["status"], "ineligible")
                self.assertEqual(self.run_cli(apply=True)["status"], "ineligible")
                self.assertTrue(self.source.exists())
                self.db.jobs.insert_one.assert_not_called()
        self.original["media"] = MEDIA

    def test_nonpreferred_candidate_is_not_promoted(self):
        larger = self.source.parent / "alternate.mkv"
        larger.write_bytes(b"a" * (self.source.stat().st_size + 100))
        preferred_record = dict(self.original, _id="slagg:" + str(larger),
                                path=str(larger), size_bytes=larger.stat().st_size,
                                mtime_ns=larger.stat().st_mtime_ns)
        self.db.files.find.return_value = [self.original, preferred_record]
        plan = self.run_cli()
        self.assertEqual(plan["status"], "not_preferred")
        self.assertEqual(plan["preferred_file_id"], preferred_record["_id"])
        self.assertEqual(self.run_cli(apply=True)["status"], "not_preferred")
        self.db.jobs.insert_one.assert_not_called()
        self.assertTrue(self.source.exists())

    def test_better_candidate_cannot_replace_a_published_recording(self):
        published = self.sports / "Rugby_Womens_PWR" / "Season_2025" / "old.mkv"
        published.parent.mkdir(parents=True)
        published.write_bytes(b"a" * 500)
        lower = dict(self.original, _id="slagg:" + str(published),
                     path=str(published), size_bytes=published.stat().st_size,
                     mtime_ns=published.stat().st_mtime_ns,
                     media=dict(MEDIA, video={"width": 1920, "height": 1080,
                                               "fps": 25}))
        self.db.files.find.return_value = [self.original, lower]
        result = self.run_cli()
        self.assertEqual(result["status"], "upgrade_available")
        self.assertEqual(result["preferred_file_id"], self.file_id)
        self.assertEqual(self.run_cli(apply=True)["status"], "upgrade_available")
        self.assertFalse(self.destination.exists())
        self.db.jobs.insert_one.assert_not_called()

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
