"""Public dashboard snapshots, throttling and failed-publication retries."""
import datetime as dt
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pvr_status", str(ROOT / "publish_status"))
spec = importlib.util.spec_from_loader(loader.name, loader)
status = importlib.util.module_from_spec(spec)
loader.exec_module(status)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.rows = [{"_id": "bbc", "failures": 0,
                      "last_success": dt.datetime(2026, 11, 27)},
                     {"_id": "rugby24:prem", "failures": 3,
                      "failure_reason": "cloudflare_challenge"},
                     {"_id": "rugbypass", "failures": 1,
                      "failure_reason": "secret at /private/path"},
                     {"_id": "rugby24", "failures": 99}]
        self.db.indexer_poll_state.find.side_effect = lambda *a, **k: self.rows
        self.db.jobs.find_one.return_value = None
        self.db.episodes.find_one.return_value = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state"
        self.send = Mock()

    def test_snapshot_allowlists_providers_and_safe_fields(self):
        data = status.snapshot(self.db)
        items = {i["id"]: i for i in data["items"]}
        self.assertEqual(len(items), 7)
        self.assertEqual(items["bbc"]["status"], "success")
        self.assertEqual(items["bbc"]["last_success"], "2026-11-27T00:00:00.000Z")
        self.assertEqual(items["youtube"]["status"], "unknown")
        self.assertEqual(items["rugby24:prem"]["reason"], "cloudflare_challenge")
        self.assertEqual(items["rugbypass"]["reason"], "discovery_failed")
        self.assertNotIn("private", str(data))
        self.assertNotIn("rugby24", items)

    def test_downloaders_keep_independent_results_and_safe_titles(self):
        now = dt.datetime(2026, 11, 28, 19, 0)
        jobs = {
            "running": dict(status="running", adapter="bbc", episode_id="current",
                            started_at=now, destination="/private/token"),
            "succeeded": dict(status="succeeded", episode_id="ok", finished_at=now),
            "failed": dict(status="failed", episode_id="bad", finished_at=now,
                           error="HTTP Error 403: https://private/token"),
        }
        def find(query, *args, **kwargs):
            if query["adapter"] == "bbc":
                return jobs[query.get("status", "running")]
        self.db.jobs.find_one.side_effect = find
        self.db.episodes.find_one.side_effect = lambda query, *a: {
            "title": {"current": "Team A vs Team B", "ok": "Previous match",
                      "bad": "Another match"}[query["_id"]]}
        data = status.downloader_snapshot(self.db, activity=lambda job: "running")
        self.assertEqual(len(data), 5)
        bbc = data[0]
        self.assertEqual(bbc["current"]["started_at"], "2026-11-28T19:00:00.000Z")
        self.assertEqual(bbc["current"]["title"], "Team A vs Team B")
        self.assertEqual(bbc["last_success"]["title"], "Previous match")
        self.assertEqual(bbc["last_failure"]["reason"], "http_403")
        self.assertNotIn("private", str(data))
        self.assertIsNone(data[1]["current"])
        self.assertEqual(status.downloader_snapshot(
            self.db, activity=lambda job: "interrupted")[0]["current"]["status"], "interrupted")

    def test_download_lock_is_observed_without_creating_files(self):
        import socket
        import fcntl
        job = dict(adapter="bbc", host=socket.gethostname(),
                   destination=str(self.path.parent / "test.mkv"))
        lock = self.path.parent / ".pvr-download-bbc.lock"
        self.assertEqual(status.local_download_activity(job), "interrupted")
        self.assertFalse(lock.exists())
        with lock.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(status.local_download_activity(job), "running")
        self.assertEqual(status.local_download_activity(job), "interrupted")
        job["host"] = "remote.invalid"
        self.assertEqual(status.local_download_activity(job), "unknown")

    def publish(self, now):
        return status.publish(self.db, self.path, "secret",
                              clock=lambda: now, transmit=self.send)

    def test_change_and_heartbeat(self):
        self.assertEqual(self.publish(10000), "published")
        self.assertEqual(self.publish(10300), "unchanged")
        self.assertEqual(self.send.call_count, 1)
        self.rows[0]["failures"] = 1
        self.assertEqual(self.publish(10600), "published")
        self.assertEqual(self.publish(14199), "unchanged")
        self.assertEqual(self.publish(14200), "published")
        self.assertEqual(self.send.call_count, 3)

    def test_failed_send_does_not_advance_state(self):
        self.send.side_effect = OSError("network")
        with self.assertRaises(OSError):
            self.publish(10000)
        self.assertFalse(self.path.exists())
        self.send.side_effect = None
        self.assertEqual(self.publish(10300), "published")

    def test_clock_rollback_forces_heartbeat(self):
        self.assertEqual(self.publish(10000), "published")
        self.assertEqual(self.publish(9900), "published")


if __name__ == "__main__":
    unittest.main()
