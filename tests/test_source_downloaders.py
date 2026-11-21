"""Offline regression tests for BBC and RugbyPass staged download adapters."""
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

from downloaders import bbc, rugbypass


MEDIA = {"duration_seconds": 7200, "video": {"height": 1080, "fps": 50}}


class SourceDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.target = self.root / "selected.mkv"
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if "--get" in command:  # get_iplayer
            self.assertIn("--profile-dir", command)
            self.assertIn("--tv-quality", command)
            self.assertNotIn("--command-tv", command)
            stage = pathlib.Path(command[command.index("--output-tv") + 1])
            (stage / "recording.mp4").write_bytes(b"iplayer downloaded media")
        elif "--no-fixture-nfo" in command:  # existing RugbyPass tool
            path = pathlib.Path(command[command.index("--output") + 1])
            self.assertEqual(path.name, "recording.mp4")
            path.write_bytes(b"rugbypass downloaded media")
        elif "-f" in command and "matroska" in command:  # ffmpeg stream-copy remux
            self.assertIn("-c", command)
            self.assertIn("copy", command)
            pathlib.Path(command[-1]).write_bytes(b"verified mkv bytes")
        else:
            self.fail(f"Unexpected subprocess command: {command}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def probe(self, path, *, binary):
        self.assertEqual(binary, "ffprobe")
        self.assertEqual(path.read_bytes(), b"verified mkv bytes")
        return MEDIA

    def test_bbc_stages_isolated_profile_and_publishes_mkv(self):
        media = bbc.download("bbc:m0034hxp", self.target,
                             execute=self.execute, probe=self.probe)
        self.assertEqual(media, MEDIA)
        self.assertEqual(self.target.read_bytes(), b"verified mkv bytes")
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.commands[0][self.commands[0].index("--pid") + 1],
                         "m0034hxp")
        self.assertFalse(list(self.root.glob(".pvr-download-*")))

    def test_rugbypass_stages_without_legacy_nfo(self):
        media = rugbypass.download("rugbypass:1048390", self.target,
                                   rugbypass="~/tools/video/rugbypass",
                                   execute=self.execute, probe=self.probe)
        self.assertEqual(media, MEDIA)
        self.assertTrue(self.target.is_file())
        self.assertIn("--no-fixture-nfo", self.commands[0])
        self.assertEqual(len(self.commands), 2)

    def test_no_overwrite_or_posthoc_media_loss(self):
        self.target.write_bytes(b"existing preferred")
        with self.assertRaises(FileExistsError):
            bbc.download("bbc:m0034hxp", self.target,
                         execute=self.execute, probe=self.probe)
        self.assertEqual(self.target.read_bytes(), b"existing preferred")
        self.assertEqual(self.commands, [])

    def test_adapter_command_failure_leaves_no_published_file(self):
        def fail(command, **kwargs):
            return SimpleNamespace(returncode=1, stderr="private token data", stdout="")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            rugbypass.download("rugbypass:1048390", self.target, execute=fail)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".pvr-download-*")))

    def test_remux_failure_leaves_original_media_unpublished(self):
        def fail_ffmpeg(command, **kwargs):
            if "--get" in command:
                stage = pathlib.Path(command[command.index("--output-tv") + 1])
                (stage / "recording.mp4").write_bytes(b"mp4")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        with self.assertRaises(RuntimeError):
            bbc.download("bbc:m0034hxp", self.target, execute=fail_ffmpeg)
        self.assertFalse(self.target.exists())

    def test_reject_bad_source_uris(self):
        for uri in ("bbc:m001", "bbc:../secret", "bbc:M0034HXP"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                bbc.bbc_id(uri)
        for uri in ("rugbypass:10483;rm", "rugbypass:https://invalid",
                    "rugbypass:../../"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                rugbypass.rugbypass_id(uri)


if __name__ == "__main__":
    unittest.main()
