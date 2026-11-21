"""Offline tests of bounded acquisition decisions and safe execution."""
import datetime as dt
import importlib.machinery
import importlib.util
import io
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from orchestration.acquire import planning_rows

ROOT = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pvr_acquire_missing", str(ROOT / "acquire_missing"))
spec = importlib.util.spec_from_loader("pvr_acquire_missing", loader)
CLI = importlib.util.module_from_spec(spec)
loader.exec_module(CLI)
EPID = "rugby-womens-pwr:pwr-289126@mbligh.github.io"
URI = "youtube:A1BcD2EfG3H"
NOW = dt.datetime(2026, 11, 21, 14, tzinfo=dt.timezone.utc)
STEM = "Rugby_Womens_PWR-s2025e101-Harlequins_v_Loughborough_Lightning_20251024"
EP = {
    "_id": EPID, "season": 2025, "episode": 101, "series_id": "rugby-womens-pwr",
    "scheduled": dt.datetime(2025, 10, 24, 18, 30, tzinfo=dt.timezone.utc),
    "nfo": "<episodedetails><showtitle>Rugby Womens PWR</showtitle><season>2025</season><episode>101</episode></episodedetails>",
    "nfo_path": "Rugby_Womens_PWR/Season_2025/" + STEM + ".nfo",
}
RELEASE = {
    "_id": URI, "source": "youtube", "episode_id": EPID,
    "available": True, "duration": 8200,
    "metadata": {"live_status": "was_live"},
}


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()
        real_sports = self.root / "real-sports"
        real_sports.mkdir()
        self.sports = self.root / "sports"
        self.sports.symlink_to(real_sports, target_is_directory=True)
        self.db = MagicMock()
        self.episodes = [dict(EP)]
        self.releases = [dict(RELEASE)]
        self.files = []
        self.jobs = []
        self.db.episodes.find.side_effect = lambda *a, **kw: list(self.episodes)
        self.db.releases.find.side_effect = lambda *a, **kw: list(self.releases)
        self.db.files.find.side_effect = lambda *a, **kw: list(self.files)
        self.db.jobs.find.side_effect = lambda *a, **kw: list(self.jobs)

    def record(self, path, *, fps=60, height=1080, seconds=7200,
               release_uri=URI, moved=False):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video data")
        row = {"_id": "slagg:" + str(path), "host": "slagg", "path": str(path),
               "episode_id": EPID, "size_bytes": path.stat().st_size,
               "mtime_ns": path.stat().st_mtime_ns, "release_uri": release_uri,
               "media": {"duration_seconds": seconds,
                         "video": {"height": height, "fps": fps}}}
        if moved:
            row["file_state"] = "moved"
        self.files.append(row)
        return row

    def rows(self, **kwargs):
        return planning_rows(
            self.db, sports_root=str(self.sports), incoming_root=str(self.incoming),
            hostname="slagg", now=NOW, **kwargs)[0]

    def run_cli(self, *, apply=False, downloader=None, filer=None, **kwargs):
        def unexpected(*args, **kwargs):
            raise AssertionError("Downloader/filer should not be called")
        return CLI.run(
            self.db, apply=apply, hostname="slagg",
            sports_root=str(self.sports), incoming_root=str(self.incoming),
            now=NOW, stream=io.StringIO(), summary_stream=io.StringIO(),
            downloader=downloader or unexpected, filer=filer or unexpected,
            **kwargs)

    def test_preview_is_read_only(self):
        row, = self.rows()
        self.assertEqual(row["action"], "download_then_file")
        self.assertEqual(row["source_uri"], URI)
        self.assertEqual(row["incoming"], str(self.incoming / "pvr-A1BcD2EfG3H.mkv"))
        summary = self.run_cli()
        self.assertEqual(summary["planned"], 1)
        self.assertEqual(summary["actionable"], 1)
        self.assertEqual(summary["downloaded"], 0)
        self.db.jobs.insert_one.assert_not_called()
        self.assertFalse((self.incoming / "pvr-A1BcD2EfG3H.mkv").exists())

    def test_apply_downloads_then_files(self):
        calls = []
        def download(db, uri, path, **kw):
            calls.append(("download", uri, path))
            self.assertTrue(kw["apply"])
            return {"status": "downloaded"}
        def filer(db, path, **kw):
            calls.append(("file", path))
            self.assertTrue(kw["apply"])
            return {"status": "filed"}
        summary = self.run_cli(apply=True, downloader=download, filer=filer)
        self.assertEqual((summary["downloaded"], summary["filed"]), (1, 1))
        self.assertEqual(calls, [
            ("download", URI, str(self.incoming / "pvr-A1BcD2EfG3H.mkv")),
            ("file", str(self.incoming / "pvr-A1BcD2EfG3H.mkv")),
        ])

    def test_existing_eligible_incoming_is_filed_without_download(self):
        local = self.incoming / "old.mkv"
        self.record(local)
        row, = self.rows()
        self.assertEqual((row["action"], row["source"]), ("file", str(local)))
        paths = []
        def filer(db, path, **kw):
            paths.append(path)
            return {"status": "filed"}
        summary = self.run_cli(apply=True, filer=filer)
        self.assertEqual((summary["filed"], summary["downloaded"]), (1, 0))
        self.assertEqual(paths, [str(local)])

    def test_published_low_fps_media_blocks_download(self):
        stored = self.sports / "Rugby_Womens_PWR/Season_2025/alternative.mkv"
        self.record(stored, fps=25, height=720, seconds=3600)
        self.assertEqual(self.rows()[0]["reason"], "already_published")
        self.assertEqual(self.run_cli(apply=True)["planned"], 0)

    def test_unindexed_canonical_file_is_not_overwritten(self):
        target = self.sports / "Rugby_Womens_PWR/Season_2025" / (STEM + ".mkv")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"untracked")
        self.assertEqual(self.rows()[0]["reason"], "canonical_destination_exists")

    def test_local_ineligible_same_release_is_not_redownloaded(self):
        self.record(self.incoming / "short.mkv", seconds=1000)
        self.assertEqual(self.rows()[0]["reason"], "no_new_youtube_release")

    def test_existing_incoming_destination_is_skipped(self):
        (self.incoming / "pvr-A1BcD2EfG3H.mkv").write_bytes(b"untracked")
        self.assertEqual(self.rows()[0]["reason"], "incoming_destination_exists")

    def test_multiple_remote_releases_not_ranked_without_quality(self):
        self.releases.append(dict(RELEASE, _id="youtube:AbCdEfGhIjK"))
        row, = self.rows()
        self.assertEqual(row["reason"], "multiple_unranked_youtube_releases")
        self.assertEqual(len(row["release_uris"]), 2)

    def test_short_release_and_cooldown_are_skipped(self):
        self.releases[0]["duration"] = 1200
        self.assertEqual(self.rows()[0]["reason"], "known_short_release")
        self.releases[0]["duration"] = 8200
        self.jobs = [{"source_uri": URI}]
        self.assertEqual(self.rows()[0]["reason"], "recent_failed_or_running_download")
        self.assertEqual(self.rows(retry_after_hours=0)[0]["action"], "download_then_file")

    def test_recent_cancelled_and_invalid_metadata(self):
        self.episodes[0]["scheduled"] = NOW - dt.timedelta(hours=2)
        self.assertEqual(self.rows(), [])
        self.episodes[0]["scheduled"] = NOW - dt.timedelta(hours=3)
        self.assertEqual(self.rows()[0]["action"], "download_then_file")
        self.episodes[0]["status"] = "cancelled"
        self.assertEqual(self.rows(), [])
        self.episodes[0]["status"] = "active"
        self.episodes[0]["nfo"] = "<broken"
        self.assertEqual(self.rows()[0]["reason"], "invalid_canonical_metadata")

    def test_moved_source_without_destination_requires_reconciliation(self):
        r = self.record(self.incoming / "old.mkv", moved=True)
        pathlib.Path(r["path"]).unlink()
        self.assertEqual(self.rows()[0]["reason"],
                         "previous_publication_needs_reconciliation")

    def test_apply_failure_stops(self):
        def download(*a, **kw):
            raise RuntimeError("yt-dlp 403")
        summary = self.run_cli(apply=True, downloader=download)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["filed"], 0)

    def test_worker_lock_refuses_overlap(self):
        with CLI.acquire_lock(self.incoming):
            with self.assertRaisesRegex(RuntimeError, "Another PVR acquisition"):
                with CLI.acquire_lock(self.incoming):
                    pass
        with CLI.acquire_lock(self.incoming):
            pass


if __name__ == "__main__":
    unittest.main()
