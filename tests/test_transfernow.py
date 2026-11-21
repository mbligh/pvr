"""Offline tests for the standalone TransferNow command-line downloader."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from downloaders import transfernow as cli


class FakeResponse:
    def __init__(self, data=b"{}", content_type="application/json", url=""):
        self.data = data
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(data))}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit=None):
        return self.data if limit is None else self.data[:limit]

    def geturl(self):
        return self.url


class TransferNowTests(unittest.TestCase):
    def test_share_urls(self):
        share = cli.parse_share("https://www.transfernow.net/en/cld?utm_source=20261111Jj147Vwz")
        self.assertEqual(share.transfer_id, "20261111Jj147Vwz")
        self.assertEqual(share.file_id, "")
        share = cli.parse_share("https://www.transfernow.net/en/d/start?utm_source=20261111Jj147Vwz&utm_term=YFpOgt")
        self.assertEqual(share.file_id, "YFpOgt")
        share = cli.parse_share("https://www.transfernow.net/dl/20261111Jj147Vwz/secretKey")
        self.assertEqual(share.user_secret, "secretKey")
        self.assertEqual(cli.parse_share("20261111Jj147Vwz").transfer_id, "20261111Jj147Vwz")
        with self.assertRaises(cli.TransferNowError):
            cli.parse_share("https://evil.invalid/en/cld?utm_source=20261111Jj147Vwz")
        with self.assertRaises(cli.TransferNowError):
            cli.parse_share("https://www.transfernow.net/dl/%2e%2e")

    def test_metadata_and_resolve_with_no_api_key(self):
        calls = []
        filename = "video.mp4"
        def get(req, timeout):
            calls.append(req.full_url)
            if "/metadata?" in req.full_url:
                return FakeResponse(json.dumps({"files": [{"name": filename, "size": 123, "id": "YFpOgt"}], "status": "ENABLED"}).encode())
            return FakeResponse(json.dumps({"url": "https://storage.transfernow.net/a?download-verify=token"}).encode())
        share = cli.Share("20261111Jj147Vwz", "secretKey")
        with patch.object(cli, "urlopen", side_effect=get):
            files = cli.metadata(share)
            result = cli.signed_url(share, files[0]["id"])
        self.assertEqual(result, "https://storage.transfernow.net/a?download-verify=token")
        self.assertIn("userSecret=secretKey", calls[0])
        self.assertIn("fileId=YFpOgt", calls[1])
        self.assertIn("preview=false", calls[1])

    def test_probe_rejects_html_and_can_use_ranged_get(self):
        url = "https://storage.transfernow.net/example"
        with patch.object(cli, "urlopen", return_value=FakeResponse(b"<html>", "text/html", url)):
            with self.assertRaisesRegex(cli.TransferNowError, "Refusing to save"):
                cli.probe(url)
        from urllib.error import HTTPError
        calls = []
        def get(req, timeout):
            calls.append(req.get_method())
            if req.get_method() == "HEAD":
                raise HTTPError(req.full_url, 405, "Not allowed", {}, io.BytesIO(b""))
            self.assertEqual(req.get_header("Range"), "bytes=0-0")
            return FakeResponse(b"\x00", "application/octet-stream", url)
        with patch.object(cli, "urlopen", side_effect=get):
            self.assertEqual(cli.probe(url)[1], "application/octet-stream")
        self.assertEqual(calls, ["HEAD", "GET"])

    def test_resuming_partial_file_and_finishing_atomically(self):
        with tempfile.TemporaryDirectory() as dirname:
            target = Path(dirname) / "video.mp4"
            partial = Path(str(target) + ".part")
            partial.write_bytes(b"DATA")
            def run(cmd, check):
                self.assertEqual(cmd[cmd.index("--continue-at")+1], "-")
                self.assertEqual(cmd[cmd.index("--output")+1], str(partial))
                with partial.open("ab") as out:
                    out.write(b"MORE")
                return type("Completed", (), {"returncode": 0})()
            with patch.object(cli.subprocess, "run", side_effect=run):
                cli.download("https://storage.transfernow.net/a", {"size": 8, "name": "video.mp4"}, target)
            self.assertEqual(target.read_bytes(), b"DATAMORE")
            self.assertFalse(partial.exists())

    def test_refuse_partial_html_and_wrong_length(self):
        with tempfile.TemporaryDirectory() as dirname:
            target = Path(dirname) / "video.mp4"
            partial = Path(str(target) + ".part")
            partial.write_bytes(b"<!doctype html><html></html>")
            with self.assertRaisesRegex(cli.TransferNowError, "HTML"):
                cli.download("https://storage.transfernow.net/a", {"size": 500, "name": "video.mp4"}, target)
            partial.unlink()
            def run(cmd, check):
                partial.write_bytes(b"DATA")
                return type("Completed", (), {"returncode": 0})()
            with patch.object(cli.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(cli.TransferNowError, "Size mismatch"):
                    cli.download("https://storage.transfernow.net/a", {"size": 500, "name": "video.mp4"}, target)
            self.assertFalse(target.exists())
            self.assertTrue(partial.exists())

    def test_filename_is_confined_to_download_directory(self):
        self.assertEqual(cli.safe_name("../../other-dir/file.mp4"), "file.mp4")
        self.assertEqual(cli.safe_name("other\\dir\\file.mp4"), "file.mp4")
        with self.assertRaises(cli.TransferNowError):
            cli.safe_name("..")
        with self.assertRaises(cli.TransferNowError):
            cli.ensure_storage_url("https://evil.example.com/whatever")


    def test_reject_symlink_and_hardlink_partial_before_curl(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            target = root / "media.mp4"
            part = Path(str(target) + ".part")
            victim = root / "other"
            victim.write_bytes(b"data")
            part.symlink_to(victim)
            with patch.object(cli.subprocess, "run") as curl, self.assertRaisesRegex(
                    cli.TransferNowError, "unsafe"):
                cli.download("https://storage.transfernow.net/a",
                             {"size": 4, "name": "media.mp4"}, target)
            curl.assert_not_called()
            part.unlink()
            part.hardlink_to(victim)
            with patch.object(cli.subprocess, "run") as curl, self.assertRaisesRegex(
                    cli.TransferNowError, "unsafe"):
                cli.download("https://storage.transfernow.net/a",
                             {"size": 4, "name": "media.mp4"}, target)
            curl.assert_not_called()
            self.assertEqual(victim.read_bytes(), b"data")


if __name__ == "__main__":
    unittest.main()
