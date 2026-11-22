"""Offline tests for PVR's TransferNow adapter and existing Atlas bookkeeping."""
import importlib.machinery
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock

from downloaders import transfernow_pvr as adapter

ROOT = Path(__file__).resolve().parents[1]
CLI = importlib.machinery.SourceFileLoader(
    "pvr_download_release_transfernow_test", str(ROOT / "download_release")
).load_module()

URI = "reddit-teletz:20261121rg0NQzAH"
URL = "https://www.transfernow.net/dl/20261121rg0NQzAH"
EP_ID = "Formula_1:11416"
EPISODE = {"_id": EP_ID, "series_id": "Formula_1", "season": 2026, "episode": 216}
RELEASE = {"_id": URI, "uri": URI, "source": "reddit-teletz",
           "id": "20261121rg0NQzAH", "episode_id": EP_ID,
           "title": "Las Vegas Grand Prix - Qualifying - Sky 1080p",
           "metadata": {"url": URL, "commentary": "sky", "height": 1080}}
MEDIA = {"duration_seconds": 4200, "video": {"height": 1080}, "audio_tracks": 1}


def verified_info(name="video.mkv"):
    return {"entry": {"id": "abc123", "size": 11, "name": name},
            "name": name, "download_url": "https://storage.transfernow.net/test",
            "size_bytes": 11, "content_type": "video/x-matroska"}


class TransferNowPvrAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.dest = self.folder / "recording.mkv"

    def check(self, uri, url):
        self.assertEqual((uri, url), (URI, URL))
        return verified_info()

    def grab(self, url, entry, path):
        self.assertEqual(entry["id"], "abc123")
        Path(path).write_bytes(b"media")

    def video(self, path, *, binary):
        self.assertEqual(binary, "ffprobe")
        self.assertEqual(Path(path).read_bytes(), b"media")
        return MEDIA

    def test_uri_matches_stored_url(self):
        self.assertEqual(adapter.source_id(URI), "20261121rg0NQzAH")
        self.assertEqual(adapter.share_from_release(URI, URL).transfer_id,
                         "20261121rg0NQzAH")
        for bad in ("youtube:8CFBWPSG3vc", "reddit-teletz:../../abc", "reddit-teletz:"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adapter.source_id(bad)
        with self.assertRaises(ValueError):
            adapter.share_from_release(URI, "https://www.transfernow.net/dl/20261121WRONG123")

    def test_inspect_rejects_multifile_and_archive(self):
        metadata = lambda share: [{"id": "abc123", "name": "a.mkv", "size": 15},
                                  {"id": "abc124", "name": "b.mkv", "size": 16}]
        with self.assertRaisesRegex(ValueError, "2 files"):
            adapter.inspect(URI, URL, metadata=metadata)
        with self.assertRaisesRegex(ValueError, "Unsupported video extension"):
            adapter.inspect(
                URI, URL,
                metadata=lambda share: [{"id": "abc123", "name": "video.zip", "size": 15}])

    def test_inspect_checks_source_and_storage_headers(self):
        result = adapter.inspect(
            URI, URL,
            metadata=lambda share: [{"id": "abc123", "name": "video.mkv", "size": 11}],
            signed_url=lambda share, key: "https://storage.transfernow.net/test",
            probe=lambda url: (11, "video/x-matroska"))
        self.assertEqual(result["size_bytes"], 11)
        self.assertEqual(result["download_url"], "https://storage.transfernow.net/test")

    def test_download_no_clobber_and_verified_publish(self):
        media = adapter.download(
            URI, URL, self.dest, inspect_link=self.check,
            download_file=self.grab, probe=self.video)
        self.assertEqual(media, MEDIA)
        self.assertEqual(self.dest.read_bytes(), b"media")
        self.assertEqual(list(self.folder.iterdir()), [self.dest])
        with self.assertRaises(FileExistsError):
            adapter.download(
                URI, URL, self.dest, inspect_link=self.check,
                download_file=self.grab, probe=self.video)
        self.assertEqual(self.dest.read_bytes(), b"media")

    def test_mp4_remux_before_probe(self):
        def inspect(uri, url):
            return verified_info("video.mp4")
        def execute(cmd, **kwargs):
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("-c", cmd)
            self.assertIn("copy", cmd)
            self.assertEqual(Path(cmd[-1]).suffix, ".mkv")
            Path(cmd[-1]).write_bytes(b"media")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        result = adapter.download(
            URI, URL, self.dest, inspect_link=inspect,
            download_file=self.grab, execute=execute, probe=self.video)
        self.assertEqual(result, MEDIA)
        self.assertEqual(self.dest.read_bytes(), b"media")

    def test_failed_probe_and_collision_do_not_publish(self):
        def reject(path, *, binary):
            raise ValueError("corrupt video")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            adapter.download(
                URI, URL, self.dest, inspect_link=self.check,
                download_file=self.grab, probe=reject)
        stage = adapter.staging_path(URI, self.dest)
        self.assertTrue(stage.is_dir())
        self.assertTrue((stage / "incoming.mkv").is_file())
        self.assertFalse(self.dest.exists())
        def collide(url, entry, path):
            self.grab(url, entry, path)
            self.dest.write_bytes(b"some other download")
        with self.assertRaises(FileExistsError):
            adapter.download(
                URI, URL, self.dest, inspect_link=self.check,
                download_file=collide, probe=self.video)
        self.assertEqual(self.dest.read_bytes(), b"some other download")
        self.assertTrue(stage.is_dir())  # no cleanup on failed publication


    def test_real_core_resume_after_interruption_and_clean_on_success(self):
        """Simulate two curl invocations; second retains first run's bytes."""
        from unittest.mock import patch
        from downloaders import transfernow

        info = verified_info()
        info["entry"]["size"] = 5
        stages = []

        def curl(command, **kwargs):
            self.assertEqual(command[command.index("--continue-at") + 1], "-")
            partial = Path(command[command.index("--output") + 1])
            stages.append(partial)
            if len(stages) == 1:
                partial.write_bytes(b"me")
                return subprocess.CompletedProcess(command, 28)
            self.assertEqual(partial.read_bytes(), b"me")
            with partial.open("ab") as output:
                output.write(b"dia")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(transfernow.subprocess, "run", side_effect=curl):
            with self.assertRaisesRegex(transfernow.TransferNowError, "curl failed"):
                adapter.download(
                    URI, URL, self.dest, inspect_link=lambda uri, url: info,
                    probe=self.video)
            stage = adapter.staging_path(URI, self.dest)
            self.assertTrue(stage.is_dir())
            self.assertEqual((stage / "incoming.mkv.part").read_bytes(), b"me")
            self.assertFalse(self.dest.exists())
            result = adapter.download(
                URI, URL, self.dest, inspect_link=lambda uri, url: info,
                probe=self.video)
        self.assertEqual(result, MEDIA)
        self.assertEqual(self.dest.read_bytes(), b"media")
        self.assertEqual(stages[0], stages[1])
        self.assertFalse(stage.exists())
        self.assertEqual(list(self.folder.iterdir()), [self.dest])

    def test_completed_source_survives_failed_remux_and_is_reused(self):
        source_info = verified_info("video.mp4")
        attempts = []
        def use_cache(url, entry, output):
            output = Path(output)
            attempts.append(output)
            if output.exists():
                self.assertEqual(output.read_bytes(), b"media")
            else:
                output.write_bytes(b"media")
        def break_remux(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "unavailable")
        with self.assertRaisesRegex(ValueError, "remux failed"):
            adapter.download(
                URI, URL, self.dest, inspect_link=lambda uri, url: source_info,
                download_file=use_cache, execute=break_remux, probe=self.video)
        stage = adapter.staging_path(URI, self.dest)
        self.assertEqual((stage / "incoming.mp4").read_bytes(), b"media")
        def remux(command, **kwargs):
            Path(command[-1]).write_bytes(b"media")
            return subprocess.CompletedProcess(command, 0, "", "")
        result = adapter.download(
            URI, URL, self.dest, inspect_link=lambda uri, url: source_info,
            download_file=use_cache, execute=remux, probe=self.video)
        self.assertEqual(result, MEDIA)
        self.assertEqual(attempts[0], attempts[1])
        self.assertFalse(stage.exists())
        self.assertEqual(self.dest.read_bytes(), b"media")

    def test_changed_metadata_refuses_to_reuse_previous_partial(self):
        def interrupted(url, entry, output):
            part = Path(str(output) + ".part")
            part.write_bytes(b"part")
            raise RuntimeError("network interruption")
        with self.assertRaisesRegex(RuntimeError, "network interruption"):
            adapter.download(URI, URL, self.dest, inspect_link=self.check,
                             download_file=interrupted, probe=self.video)
        stage = adapter.staging_path(URI, self.dest)
        part = stage / "incoming.mkv.part"
        self.assertEqual(part.read_bytes(), b"part")
        altered = verified_info()
        altered["entry"]["id"] = "another-file-id"
        with self.assertRaisesRegex(ValueError, "metadata changed"):
            adapter.download(
                URI, URL, self.dest, inspect_link=lambda uri, url: altered,
                download_file=lambda *a: self.fail("unsafe resumed transfer"),
                probe=self.video)
        self.assertEqual(part.read_bytes(), b"part")
        self.assertFalse(self.dest.exists())

    def test_staging_does_not_mix_destinations(self):
        alternate = self.folder / "other.mkv"
        self.assertNotEqual(adapter.staging_path(URI, self.dest),
                            adapter.staging_path(URI, alternate))
        self.assertEqual(adapter.staging_path(URI, self.dest),
                         adapter.staging_path(URI, self.dest))

    def test_rejects_symlinked_cached_part_without_touching_target(self):
        def interrupted(url, entry, output):
            raise RuntimeError("interrupted")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            adapter.download(URI, URL, self.dest, inspect_link=self.check,
                             download_file=interrupted, probe=self.video)
        stage = adapter.staging_path(URI, self.dest)
        victim = self.folder / "untouched"
        victim.write_bytes(b"never change")
        (stage / "incoming.mkv.part").symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "Unsafe staging file"):
            adapter.download(URI, URL, self.dest, inspect_link=self.check,
                             download_file=self.grab, probe=self.video)
        self.assertEqual(victim.read_bytes(), b"never change")
        self.assertFalse(self.dest.exists())

    def test_refuses_parallel_write_to_same_cache(self):
        def nested(url, entry, path):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                adapter.download(URI, URL, self.dest, inspect_link=self.check,
                                 download_file=self.grab, probe=self.video)
            Path(path).write_bytes(b"media")
        adapter.download(URI, URL, self.dest, inspect_link=self.check,
                         download_file=nested, probe=self.video)
        self.assertEqual(self.dest.read_bytes(), b"media")



class TransferNowAtlasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "pvr.mkv"
        self.db = MagicMock()
        self.db.releases.find_one.return_value = RELEASE.copy()
        self.db.episodes.find_one.return_value = EPISODE.copy()
        self.db.files.find.return_value = []
        self.db.files.find_one.return_value = None

    def run_cli(self, **kwargs):
        return CLI.run(
            self.db, URI, self.dest, hostname="slagg", output=io.StringIO(),
            **kwargs)

    def test_preview_is_readonly_and_source_bound(self):
        plan = self.run_cli()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["adapter"], "transfernow")
        self.assertEqual(plan["source_url"], URL)
        self.assertEqual(plan["series_id"], "Formula_1")
        self.db.jobs.insert_one.assert_not_called()
        self.db.files.update_one.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_rejects_wrong_series_wrong_url_and_existing_destination(self):
        self.db.episodes.find_one.return_value = dict(EPISODE, series_id="Rugby_Womens_PWR")
        with self.assertRaises(ValueError):
            self.run_cli()
        self.db.episodes.find_one.return_value = EPISODE
        self.db.releases.find_one.return_value = dict(RELEASE, metadata={"url": "https://evil.invalid/dl/abc"})
        with self.assertRaises(ValueError):
            self.run_cli()
        self.db.releases.find_one.return_value = RELEASE
        self.dest.write_bytes(b"already here")
        self.assertEqual(self.run_cli(apply=True)["status"], "destination_exists")
        self.db.jobs.insert_one.assert_not_called()

    def test_apply_uses_moved_downloader_and_registers(self):
        def downloaded(uri, url, dest, **kwargs):
            self.assertEqual(uri, URI)
            self.assertEqual(url, URL)
            self.assertEqual(kwargs["ffmpeg"], "ffmpeg")
            Path(dest).write_bytes(b"media")
            return MEDIA
        result = self.run_cli(apply=True, transfernow_downloader=downloaded)
        self.assertEqual(result["status"], "downloaded")
        job = self.db.jobs.insert_one.call_args.args[0]
        self.assertEqual(job["adapter"], "transfernow")
        file_record = self.db.files.update_one.call_args.args[1]["$set"]
        self.assertEqual(file_record["series_id"], "Formula_1")
        self.assertEqual(file_record["release_uri"], URI)
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
                         "succeeded")

    def test_failed_transfernow_records_failed_job(self):
        def failed(uri, url, dest, **kwargs):
            raise RuntimeError("link expired")
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.run_cli(apply=True, transfernow_downloader=failed)
        self.assertEqual(self.db.jobs.update_one.call_args.args[1]["$set"]["status"],
                         "failed")
        self.db.files.update_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
