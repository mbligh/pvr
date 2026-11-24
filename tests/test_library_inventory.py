"""Offline regression tests for manual recordings across sports series."""
import io
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from inventory.library import scan
from inventory.scan import identify, make_indexes


def episode(series, number=304):
    show = {"Rugby_Womens_WXV": "Rugby Womens WXV",
            "Formula_1": "Formula 1"}[series]
    return {"_id": f"{series}:fixture", "series_id": series,
            "season": 2026, "episode": number,
            "nfo_path": f"{series}/Season_2026/{series}-s2026e{number:03d}-canonical.nfo",
            "nfo": f"<episodedetails><showtitle>{show}</showtitle><season>2026</season>"
                   f"<episode>{number}</episode><title>Canonical title</title></episodedetails>"}


class LibraryInventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.w = episode("Rugby_Womens_WXV")
        self.f = episode("Formula_1")
        self.db = MagicMock()
        self.db.episodes.find.return_value = [self.w, self.f]
        self.db.releases.find.return_value = []
        self.db.files.find.return_value = []
        self.probe = MagicMock(return_value={"video": {"height": 1080},
                                             "duration_seconds": 7200})

    def video(self, series, name):
        path = self.root / series / "Season_2026" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test video")
        return path

    def run_scan(self, **kwargs):
        return scan(self.db, sports_root=self.root, hostname="slagg",
                    prober=self.probe, stream=io.StringIO(),
                    summary_stream=io.StringIO(), **kwargs)

    def test_title_suffix_is_optional_and_series_disambiguates(self):
        w = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        f = self.video("Formula_1", "Formula_1-s2026e304-different title.mp4")
        report = self.run_scan(apply=True)
        self.assertEqual(report["written"], 2)
        self.assertEqual(report["nfo_created"], 2)
        self.assertEqual(w.with_suffix(".nfo").read_text(), self.w["nfo"])
        self.assertEqual(f.with_suffix(".nfo").read_text(), self.f["nfo"])
        self.assertTrue(w.exists())
        self.assertTrue(f.exists())
        updates = self.db.files.bulk_write.call_args.args[0]
        self.assertEqual({x._doc["$set"]["episode_id"] for x in updates},
                         {self.w["_id"], self.f["_id"]})

    def test_preview_creates_neither_nfo_nor_inventory(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304-spelling.mkv")
        result = self.run_scan()
        self.assertEqual(result["present"], 1)
        self.assertFalse(path.with_suffix(".nfo").exists())
        self.db.files.bulk_write.assert_not_called()

    def test_conflicting_sidecar_and_duplicate_episode_rejected(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        sidecar = path.with_suffix(".nfo")
        sidecar.write_text(self.f["nfo"])
        result = self.run_scan(apply=True)
        self.assertEqual(result["conflict"], 1)
        self.assertEqual(sidecar.read_text(), self.f["nfo"])
        self.db.files.bulk_write.assert_not_called()
        sidecar.unlink()
        index, links = make_indexes([self.w, dict(self.w, _id="duplicate")], [])
        self.assertIn("ambiguous", identify(path, index, links)[3])

    def test_unchanged_recording_reuses_probe_and_preserves_provenance(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        sidecar = path.with_suffix(".nfo")
        sidecar.write_text(self.w["nfo"])
        st = path.stat()
        self.db.files.find.return_value = [{
            "_id": f"slagg:{path}", "host": "slagg", "path": str(path),
            "episode_id": self.w["_id"], "series_id": self.w["series_id"],
            "season": 2026, "episode": 304, "size_bytes": st.st_size,
            "mtime_ns": st.st_mtime_ns, "media": self.probe.return_value,
            "match_evidence": ["filename", "nfo"], "release_uri": "rugbypass:12345",
            "nfo_path": str(sidecar)}]
        report = self.run_scan(apply=True)
        self.assertEqual(report["cached"], 1)
        self.assertEqual(report["written"], 0)
        self.probe.assert_not_called()
        self.db.files.bulk_write.assert_not_called()

    def test_only_selected_series_is_scanned(self):
        self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        self.video("Formula_1", "s2026e304.mkv")
        report = self.run_scan(apply=True, series_ids=("Formula_1",))
        self.assertEqual(report["written"], 1)
        self.assertEqual(report["nfo_created"], 1)


if __name__ == "__main__":
    unittest.main()
