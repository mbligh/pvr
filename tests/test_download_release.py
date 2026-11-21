"""Tests for the single-release downloader, safety and Atlas job bookkeeping."""
import datetime as dt
import importlib.machinery
import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock

from downloaders import youtube

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = importlib.machinery.SourceFileLoader(
    "pvr_download_release", str(ROOT / "download_release")
).load_module()

URI = "youtube:8CFBWPSG3vc"
EP_ID = "rugby-womens-pwr:pwr-289127@mbligh.github.io"
RELEASE = {
    "_id": URI, "source": "youtube", "id": "8CFBWPSG3vc",
    "title": "Trailfinders Women vs Exeter Chiefs",
    "available": True, "episode_id": EP_ID,
    "metadata": {"live_status": "was_live"},
}
EPISODE = {
    "_id": EP_ID, "series_id": "rugby-womens-pwr",
    "season": 2025, "episode": 102,
}
MEDIA = {
    "duration_seconds": 7300.25,
    "video": {"height": 1080, "fps": 50.0, "codec": "h264"},
    "audio_tracks": 1,
}


class YoutubeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = pathlib.Path(self.tmp.name)
        self.destination = self.directory / "match.mkv"

    def execute(self, command, **kwargs):
        self.assertIn("--no-playlist", command)
        self.assertIn("--remux-video", command)
        self.assertIn("--merge-output-format", command)
        self.assertEqual(command[-1],
                         "https://www.youtube.com/watch?v=8CFBWPSG3vc")
        out = command[command.index("--output") + 1]
        staged = pathlib.Path(out.replace("%(ext)s", "mkv"))
        staged.write_bytes(b"valid fake media from a known video")
        return subprocess.CompletedProcess(command, 0, "", "")

    def probe(self, path, *, binary):
        self.assertEqual(binary, "ffprobe")
        self.assertEqual(path.read_bytes(),
                         b"valid fake media from a known video")
        return MEDIA

    def test_valid_download_is_atomic_and_staging_is_removed(self):
        result = youtube.download(
            URI, self.destination, execute=self.execute, probe=self.probe
        )
        self.assertEqual(result, MEDIA)
        self.assertEqual(self.destination.read_bytes(),
                         b"valid fake media from a known video")
        self.assertEqual(list(self.directory.iterdir()), [self.destination])

    def test_refuses_existing_destination(self):
        self.destination.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            youtube.download(URI, self.destination, execute=self.execute,
                             probe=self.probe)
        self.assertEqual(self.destination.read_bytes(), b"original")

    def test_collision_during_download_cannot_overwrite(self):
        def racing_download(command, **kwargs):
            result = self.execute(command, **kwargs)
            self.destination.write_bytes(b"competitor")
            return result
        with self.assertRaises(FileExistsError):
            youtube.download(URI, self.destination, execute=racing_download,
                             probe=self.probe)
        self.assertEqual(self.destination.read_bytes(), b"competitor")
        self.assertEqual(list(self.directory.iterdir()), [self.destination])

    def test_unverified_media_cannot_be_published(self):
        def reject(path, *, binary):
            raise ValueError("No video stream")
        with self.assertRaisesRegex(ValueError, "No video stream"):
            youtube.download(URI, self.destination, execute=self.execute,
                             probe=reject)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_downloader_failure_cleans_staging(self):
        def fail(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "downloader error")
        with self.assertRaisesRegex(RuntimeError, "yt-dlp failed"):
            youtube.download(URI, self.destination, execute=fail)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_id_and_destination_are_rejected(self):
        self.assertEqual(youtube.youtube_id(URI), "8CFBWPSG3vc")
        for value in ("youtube:../../blah", "bbc:m0034hxp",
                      "youtube:8CFBWPSG3vc?foobar", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                youtube.youtube_id(value)
        with self.assertRaisesRegex(ValueError, "absolute"):
            youtube.download(URI, "relative.mkv")
        with self.assertRaisesRegex(ValueError, "absolute"):
            youtube.download(URI, self.directory / "match.mp4")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            youtube.download(URI, self.directory / "missing" / "match.mkv")


class AtlasDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = pathlib.Path(self.tmp.name) / "test.mkv"
        self.db = MagicMock()
        self.db.releases.find_one.return_value = dict(RELEASE)
        self.db.episodes.find_one.return_value = dict(EPISODE)
        self.db.files.find.return_value = []
        self.db.files.find_one.return_value = None
        self.db.files.update_one.return_value.upserted_id = "id"
        self.db.jobs.update_one.return_value.modified_count = 1

    def run_cli(self, **kwargs):
        return CLI.run(
            self.db, URI, self.destination, hostname="slagg",
            output=io.StringIO(), **kwargs
        )

    def test_preview_is_read_only(self):
        plan = self.run_cli()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["episode"], 102)
        self.db.jobs.insert_one.assert_not_called()
        self.db.files.update_one.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_already_owned_episode_is_skipped(self):
        other = self.destination.parent / "other.mkv"
        other.write_bytes(b"already acquired")
        self.db.files.find.return_value = [{
            "path": str(other), "episode_id": EP_ID,
            "size_bytes": other.stat().st_size
        }]
        self.assertEqual(self.run_cli()["status"], "already_owned")
        self.assertEqual(self.run_cli(apply=True)["status"], "already_owned")
        self.db.jobs.insert_one.assert_not_called()
        # Explicit alternate recording should not be blocked.
        self.assertEqual(
            self.run_cli(allow_existing_episode=True)["status"], "ready"
        )

    def test_stale_inventory_entry_does_not_block(self):
        self.db.files.find.return_value = [{
            "path": str(self.destination.parent / "missing.mkv"),
            "size_bytes": 12345
        }]
        self.assertEqual(self.run_cli()["status"], "ready")

    def test_existing_destination_is_never_overwritten(self):
        self.destination.write_bytes(b"existing, unknown")
        plan = self.run_cli()
        self.assertEqual(plan["status"], "destination_exists")
        self.assertEqual(self.run_cli(apply=True)["status"], "destination_exists")
        self.assertEqual(self.destination.read_bytes(), b"existing, unknown")
        self.db.jobs.insert_one.assert_not_called()

    def test_reject_unauthorized_and_unlinked_releases(self):
        for altered in (
            dict(RELEASE, episode_id=None),
            dict(RELEASE, available=False),
            dict(RELEASE, metadata={"live_status": "is_upcoming"}),
        ):
            self.db.releases.find_one.return_value = altered
            with self.assertRaises(ValueError):
                self.run_cli()
        self.db.releases.find_one.return_value = dict(RELEASE)
        self.db.episodes.find_one.return_value = dict(EPISODE, season=2024)
        with self.assertRaisesRegex(ValueError, "2025"):
            self.run_cli()

    def test_apply_records_file_and_successful_job(self):
        def save(uri, destination, **kwargs):
            self.assertEqual(uri, URI)
            pathlib.Path(destination).write_bytes(b"downloaded video")
            return MEDIA
        summary = self.run_cli(apply=True, downloader=save)
        self.assertEqual(summary["status"], "downloaded")
        self.assertEqual(summary["file_id"], "slagg:" + str(self.destination))
        self.assertEqual(self.db.jobs.insert_one.call_count, 1)
        self.assertEqual(self.db.jobs.update_one.call_count, 1)
        self.assertEqual(
            self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
            "succeeded")
        file_upsert = self.db.files.update_one.call_args
        self.assertEqual(file_upsert.args[1]["$set"]["episode_id"], EP_ID)
        self.assertEqual(file_upsert.args[1]["$set"]["release_uri"], URI)
        self.assertEqual(file_upsert.args[1]["$set"]["media"], MEDIA)
        self.assertEqual(file_upsert.kwargs["upsert"], True)
        self.assertTrue(self.destination.exists())

    def test_failed_download_creates_failed_job_but_no_file(self):
        def fail(uri, destination, **kwargs):
            raise RuntimeError("source unavailable")
        with self.assertRaisesRegex(RuntimeError, "source unavailable"):
            self.run_cli(apply=True, downloader=fail)
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
                         "failed")
        self.db.files.update_one.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_failed_db_registration_preserves_published_file(self):
        self.db.files.update_one.side_effect = RuntimeError("Atlas unavailable")
        def save(uri, destination, **kwargs):
            pathlib.Path(destination).write_bytes(b"successfully downloaded")
            return MEDIA
        with self.assertRaisesRegex(RuntimeError, "Atlas unavailable"):
            self.run_cli(apply=True, downloader=save)
        self.assertTrue(self.destination.exists())
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
                         "failed")


if __name__ == "__main__":
    unittest.main()
