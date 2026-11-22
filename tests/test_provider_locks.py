"""Provider-specific indexer and physical-download locks remain independent."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from orchestration.locks import LockBusy, episode_lock, site_lock

ROOT = Path(__file__).resolve().parents[1]


def load(name, script):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / script))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


refresh = load("pvr_refresh_lock_tests", "refresh_releases")
downloader = load("pvr_download_lock_tests", "download_release")


class LockIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_same_site_indexer_conflicts_but_other_operations_do_not(self):
        with site_lock(self.root, "indexer", "bbc"):
            with self.assertRaises(LockBusy):
                with site_lock(self.root, "indexer", "bbc"):
                    pass
            with site_lock(self.root, "indexer", "youtube"):
                with site_lock(self.root, "download", "bbc"):
                    with site_lock(self.root, "download", "transfernow"):
                        pass
        with site_lock(self.root, "indexer", "bbc"):
            pass

    def test_download_site_serialization_without_global_lock(self):
        with site_lock(self.root, "download", "youtube"):
            with self.assertRaises(LockBusy):
                with site_lock(self.root, "download", "youtube"):
                    pass
            with site_lock(self.root, "download", "rugbypass"):
                with site_lock(self.root, "indexer", "youtube"):
                    pass

    def test_episode_reservations_prevent_same_episode_across_sites(self):
        with episode_lock(self.root, "Rugby_Womens_PWR:1"):
            with self.assertRaises(LockBusy):
                with episode_lock(self.root, "Rugby_Womens_PWR:1"):
                    pass
            with episode_lock(self.root, "Rugby_Womens_PWR:2"):
                pass
        with episode_lock(self.root, "Rugby_Womens_PWR:1"):
            pass

    def test_invalid_untrusted_lock_names_are_rejected(self):
        for kind, site in (("indexer", "../bbc"), ("download", "reddit-teletz"),
                           ("unknown", "bbc"), ("download", "unknown")):
            with self.subTest(kind=kind, site=site), self.assertRaises(ValueError):
                with site_lock(self.root, kind, site):
                    pass
        self.assertFalse(list(self.root.iterdir()))

    def test_indexer_source_lock_held_through_discovery(self):
        db = MagicMock()
        seen = []

        def discover(sources, **kwargs):
            self.assertEqual(sources, ("bbc",))
            with self.assertRaises(LockBusy):
                with site_lock(self.root, "indexer", "bbc"):
                    pass
            with site_lock(self.root, "download", "bbc"):
                seen.append("download independent")
            return {"bbc": []}

        report = refresh.run(db, sources=("bbc",), discover=discover,
                             lock_root=self.root, output=io.StringIO())
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(seen, ["download independent"])

    def test_indexer_skip_if_same_source_already_running(self):
        db = MagicMock()
        with site_lock(self.root, "indexer", "rugbypass"):
            with self.assertRaises(LockBusy):
                refresh.run(
                    db, sources=("rugbypass",), discover=lambda *a, **k: None,
                    lock_root=self.root, output=io.StringIO())
        db.releases.bulk_write.assert_not_called()

    def test_teletz_download_is_locked_as_transfernow_not_reddit(self):
        with site_lock(self.root, "download", "transfernow"):
            with self.assertRaises(LockBusy):
                downloader.run(MagicMock(), "reddit-teletz:anything",
                               self.root / "recording.mkv", apply=True)
            with site_lock(self.root, "indexer", "reddit-teletz"):
                pass

    def test_real_download_wrapper_locks_site_before_db_job(self):
        db = MagicMock()
        uri = "youtube:8CFBWPSG3vc"
        eid = "Rugby_Womens_PWR:fixture-10"
        db.releases.find_one.return_value = {
            "_id": uri, "source": "youtube", "episode_id": eid,
            "available": True, "metadata": {"live_status": "was_live"},
        }
        db.episodes.find_one.return_value = {
            "_id": eid, "series_id": "Rugby_Womens_PWR",
            "season": 2026, "episode": 101,
        }
        db.files.find.return_value = []
        db.files.find_one.return_value = None
        path = self.root / "match.mkv"
        media = {"duration_seconds": 7200, "video": {"height": 1080, "fps": 50}}
        called = []

        def fetch(uri, destination, **kwargs):
            with self.assertRaises(LockBusy):
                with site_lock(self.root, "download", "youtube"):
                    pass
            with site_lock(self.root, "indexer", "youtube"):
                with site_lock(self.root, "download", "bbc"):
                    pass
            called.append(uri)
            Path(destination).write_bytes(b"test media")
            return media

        with site_lock(self.root, "download", "youtube"):
            with self.assertRaises(LockBusy):
                downloader.run(db, uri, path, apply=True, downloader=fetch)
        db.jobs.insert_one.assert_not_called()
        result = downloader.run(db, uri, path, apply=True, downloader=fetch)
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(called, [uri])
        self.assertEqual(db.jobs.insert_one.call_count, 1)


if __name__ == "__main__":
    unittest.main()
