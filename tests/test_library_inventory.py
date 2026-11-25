"""Offline regression tests for manual recordings across sports series."""
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from inventory.library import scan, persist
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
        self.db.files.find_one.return_value = None
        self.db.files.update_one.return_value.matched_count = 1
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
        self.assertEqual(report["renamed"], 2)
        w_target = self.root / self.w["nfo_path"]
        f_target = self.root / self.f["nfo_path"]
        self.assertEqual(w_target.read_text(), self.w["nfo"])
        self.assertEqual(f_target.read_text(), self.f["nfo"])
        self.assertTrue(w_target.with_suffix(".mkv").exists())
        self.assertTrue(f_target.with_suffix(".mp4").exists())
        self.assertFalse(w.exists())
        self.assertFalse(f.exists())
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
        path = self.video("Rugby_Womens_WXV", pathlib.Path(self.w["nfo_path"]).with_suffix(".mkv").name)
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

    def test_existing_nfo_is_moved_without_rewriting(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        sidecar = path.with_suffix(".nfo")
        original = self.w["nfo"].replace("Canonical title", "My metadata")
        sidecar.write_text(original)
        report = self.run_scan(apply=True)
        target = self.root / self.w["nfo_path"]
        self.assertEqual(report["renamed"], 1)
        self.assertEqual(report["nfo_created"], 0)
        self.assertEqual(target.read_text(), original)
        self.assertFalse(sidecar.exists())
        self.assertFalse(path.exists())
        row = self.db.files.insert_one.call_args.args[0]
        self.assertEqual(row["path"], str(target.with_suffix(".mkv")))
        self.assertEqual(row["previous_file_id"], f"slagg:{path}")

    def test_existing_canonical_video_is_not_overwritten(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        target = self.video("Rugby_Womens_WXV",
                            pathlib.Path(self.w["nfo_path"]).with_suffix(".mp4").name)
        target.write_bytes(b"existing canonical video")
        report = self.run_scan(apply=True)
        self.assertEqual(report["renamed"], 0)
        self.assertTrue(path.exists())
        self.assertEqual(target.read_bytes(), b"existing canonical video")

    def test_atlas_failure_preserves_original(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        self.db.files.insert_one.side_effect = RuntimeError("Atlas unavailable")
        with self.assertRaisesRegex(RuntimeError, "Atlas unavailable"):
            self.run_scan(apply=True)
        self.assertTrue(path.exists())
        self.assertTrue((self.root / self.w["nfo_path"]).with_suffix(".mkv").exists())

    def test_unmanaged_historical_season_is_quiet_and_not_probed(self):
        path = self.root / "Rugby_Womens_WXV/Season_2021/s2021e01.mkv"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old recording")
        output = io.StringIO()
        report = scan(self.db, sports_root=self.root, hostname="slagg",
                      prober=self.probe, apply=True, stream=output,
                      summary_stream=io.StringIO())
        self.assertEqual(report["unmanaged"], 1)
        self.assertEqual(report["conflict"], 0)
        self.assertEqual(output.getvalue(), "")
        self.probe.assert_not_called()
        self.db.files.bulk_write.assert_not_called()
        self.assertTrue(path.exists())
        self.assertFalse(path.with_suffix(".nfo").exists())
        output = io.StringIO()
        scan(self.db, sports_root=self.root, hostname="slagg", verbose=True,
             prober=self.probe, stream=output, summary_stream=io.StringIO())
        self.assertEqual(json.loads(output.getvalue())["status"], "unmanaged")

    def test_missing_number_in_managed_historical_season_is_conflict(self):
        path = self.root / "Rugby_Womens_WXV/Season_2021/s2021e01.mkv"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old recording")
        self.db.episodes.find.return_value = [dict(self.w, season=2021)]
        report = self.run_scan(apply=True)
        self.assertEqual(report["conflict"], 1)
        self.assertEqual(report["unmanaged"], 0)
        self.probe.assert_not_called()

    def test_reappearing_moved_path_is_reprobed_and_revived(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        target = self.video("Rugby_Womens_WXV",
                            pathlib.Path(self.w["nfo_path"]).with_suffix(".mkv").name)
        target.write_bytes(b"canonical recording")
        target.with_suffix(".nfo").write_text(self.w["nfo"])
        old = {"_id": f"slagg:{path}", "episode_id": self.w["_id"],
               "file_state": "moved", "moved_to": f"slagg:{target}",
               "size_bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
               "media": self.probe.return_value, "release_uri": "rugbypass:123"}
        self.db.files.find.return_value = [old]
        report = self.run_scan(apply=True)
        self.assertEqual(report["reappeared"], 1)
        self.assertEqual(report["conflict"], 0)
        self.assertEqual(report["probe_error"], 0)
        self.assertEqual(report["renamed"], 0)
        self.assertEqual(self.probe.call_count, 2)
        updates = self.db.files.bulk_write.call_args.args[0]
        revived = next(op for op in updates if op._filter["_id"] == old["_id"])
        self.assertEqual(revived._filter["file_state"], "moved")
        self.assertEqual(revived._filter["moved_to"], old["moved_to"])
        self.assertEqual(revived._doc["$unset"]["file_state"], "")
        self.assertIsNone(revived._doc["$set"]["release_uri"])
        self.assertEqual(target.read_bytes(), b"canonical recording")
        self.assertTrue(path.exists())

    def test_moved_path_preview_does_not_write(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        self.db.files.find.return_value = [{"_id": f"slagg:{path}",
                                           "episode_id": self.w["_id"],
                                           "file_state": "moved"}]
        report = self.run_scan()
        self.assertEqual(report["reappeared"], 1)
        self.probe.assert_called_once()
        self.db.files.bulk_write.assert_not_called()

    def test_wrong_association_is_conflict_not_probe_error(self):
        path = self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        for state in (None, "moved"):
            with self.subTest(state=state):
                self.db.files.find.return_value = [{"_id": f"slagg:{path}",
                                                    "episode_id": "other-series:fixture",
                                                    "file_state": state}]
                report = self.run_scan(apply=True, series_ids=("Rugby_Womens_WXV",))
                self.assertEqual(report["conflict"], 1)
                self.assertEqual(report["probe_error"], 0)
                self.assertEqual(report["written"], 0)
        self.probe.assert_not_called()
        self.db.files.bulk_write.assert_not_called()

    def test_changed_move_is_not_revived(self):
        old = {"_id": "slagg:/example.mkv", "episode_id": self.w["_id"],
               "file_state": "moved", "moved_to": "slagg:/original-target.mkv"}
        self.db.files.find.return_value = [dict(old, moved_to="slagg:/new-target.mkv")]
        with self.assertRaisesRegex(ValueError, "associations conflict"):
            persist(self.db, [{"_id": old["_id"], "episode_id": self.w["_id"]}],
                    reappeared={old["_id"]: old})
        self.db.files.bulk_write.assert_not_called()

    def test_only_selected_series_is_scanned(self):
        self.video("Rugby_Womens_WXV", "s2026e304.mkv")
        self.video("Formula_1", "s2026e304.mkv")
        report = self.run_scan(apply=True, series_ids=("Formula_1",))
        self.assertEqual(report["written"], 1)
        self.assertEqual(report["nfo_created"], 1)


if __name__ == "__main__":
    unittest.main()
