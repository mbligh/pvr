"""Offline tests for file quality policy and read-only selector CLI."""
import importlib.machinery
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from selection.quality import evaluate_file, select, select_episode

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = importlib.machinery.SourceFileLoader(
    "pvr_select_recording", str(ROOT / "select_recording")
).load_module()
EP_ID = "Rugby_Womens_PWR:pwr-295034@mbligh.github.io"


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = pathlib.Path(self.tmp.name)

    def candidate(self, name, size=1000, height=1080, seconds=7200,
                  fps=50, state=None):
        path = self.folder / (name + ".mkv")
        path.write_bytes(b"v" * size)
        result = {
            "_id": "slagg:" + str(path),
            "host": "slagg", "path": str(path),
            "episode_id": EP_ID,
            "size_bytes": size,
            "mtime_ns": path.stat().st_mtime_ns,
            "media": {"duration_seconds": seconds,
                      "video": {"height": height, "fps": fps}},
        }
        if state:
            result["file_state"] = state
        return result

    def test_resolution_and_duration_thresholds_are_inclusive(self):
        ok = self.candidate("boundary", height=720, seconds=3600)
        self.assertTrue(evaluate_file(ok)["eligible"])
        too_short = self.candidate("short", height=1080, seconds=3599.9)
        self.assertEqual(evaluate_file(too_short)["reason"], "under_one_hour")
        too_small = self.candidate("low", height=719, seconds=3600)
        self.assertEqual(evaluate_file(too_small)["reason"], "below_720p")
        tiny = self.candidate("tiny", height=480, seconds=900)
        self.assertEqual(evaluate_file(tiny)["reason"], "below_720p")

    def test_50fps_classification_is_inclusive(self):
        self.assertEqual(evaluate_file(self.candidate("fifty", fps=50))[
            "frame_rate_tier"], "50plus")
        self.assertEqual(evaluate_file(self.candidate("low", fps=49.999))[
            "frame_rate_tier"], "under50")
        self.assertEqual(evaluate_file(self.candidate("sixty", fps=60))[
            "frame_rate_tier"], "50plus")

    def test_prefer_high_fps_over_larger_low_fps(self):
        larger_low = self.candidate("larger-low", size=9000, fps=25)
        smaller_high = self.candidate("smaller-high", size=1000, fps=50)
        outcome = select([evaluate_file(larger_low), evaluate_file(smaller_high)])
        self.assertEqual(outcome["preferred_file_id"], smaller_high["_id"])

    def test_choose_largest_within_preferred_high_fps(self):
        small = self.candidate("small", size=1000, fps=60)
        large = self.candidate("large", size=2000, fps=50)
        outcome = select([evaluate_file(small), evaluate_file(large)])
        self.assertEqual(outcome["preferred_file_id"], large["_id"])

    def test_largest_other_when_no_high_fps(self):
        small = self.candidate("small", size=1000, fps=25)
        large = self.candidate("large", size=2000, fps=30)
        outcome = select([evaluate_file(small), evaluate_file(large)])
        self.assertEqual(outcome["preferred_file_id"], large["_id"])

    def test_excluded_files_never_compete(self):
        large_low_res = self.candidate("lowres", size=20000, height=480)
        large_short = self.candidate("short", size=30000, seconds=1000)
        valid = self.candidate("valid", size=500, fps=24)
        outcome = select([evaluate_file(x) for x in
                          (large_low_res, large_short, valid)])
        self.assertEqual(outcome["preferred_file_id"], valid["_id"])
        self.assertEqual(outcome["excluded_count"], 2)

    def test_missing_or_stale_files_are_not_selected(self):
        valid = self.candidate("ok")
        changed_size = dict(valid, size_bytes=valid["size_bytes"] + 1)
        changed_mtime = dict(valid, mtime_ns=valid["mtime_ns"] - 1)
        gone = self.candidate("gone")
        pathlib.Path(gone["path"]).unlink()
        moved = self.candidate("moved", state="moved")
        link = self.folder / "alias.mkv"
        link.symlink_to(valid["path"])
        symlink = dict(valid, _id="slagg:" + str(link), path=str(link))
        for candidate, reason in [
            (changed_size, "inventory_changed"),
            (changed_mtime, "inventory_changed"),
            (gone, "not_found"),
            (moved, "moved"),
            (symlink, "not_regular_video"),
        ]:
            with self.subTest(reason=reason):
                self.assertEqual(evaluate_file(candidate)["reason"], reason)

    def test_unknown_frame_rate_is_excluded_not_assumed_low(self):
        unknown = self.candidate("unknown", size=3000, fps=None)
        valid = self.candidate("valid", size=100, fps=25)
        outcome = select([evaluate_file(unknown), evaluate_file(valid)])
        self.assertEqual(outcome["preferred_file_id"], valid["_id"])
        self.assertEqual(outcome["excluded_count"], 1)
        self.assertEqual(evaluate_file(unknown)["reason"], "unknown_fps")

    def test_no_valid_recordings(self):
        short = self.candidate("short", seconds=3599)
        outcome = select([evaluate_file(short)])
        self.assertEqual(outcome["status"], "no_eligible_file")
        self.assertIsNone(outcome["preferred_file_id"])

    def test_tie_breaks_stably(self):
        a = self.candidate("a", size=1000, fps=50)
        b = self.candidate("b", size=1000, fps=50)
        one = select([evaluate_file(a), evaluate_file(b)])
        two = select([evaluate_file(b), evaluate_file(a)])
        self.assertEqual(one["preferred_file_id"], a["_id"])
        self.assertEqual(two["preferred_file_id"], a["_id"])

    def test_actual_s2026e101_comparison_policy(self):
        # Based on known Atlas media metadata; use tiny local stand-ins for
        # actual size to avoid writing multi-gigabyte test fixtures.
        bbc = self.candidate("bbc", size=5000, fps=50, seconds=7609.322)
        yt = self.candidate("youtube", size=2500, fps=60, seconds=7930.028)
        outcome = select([evaluate_file(yt), evaluate_file(bbc)])
        self.assertEqual(outcome["preferred_file_id"], bbc["_id"])

    def test_selector_reads_atlas_without_mutations(self):
        item = self.candidate("bbc", size=1000)
        database = MagicMock()
        database.files.find.return_value = [item]
        database.episodes.find_one.return_value = {
            "_id": EP_ID, "series_id": "Rugby_Womens_PWR",
            "season": 2026, "episode": 101,
        }
        summary = CLI.run(database, EP_ID, hostname="slagg", stream=io.StringIO())
        self.assertEqual(summary["preferred_file_id"], item["_id"])
        database.files.find.assert_called_with({
            "episode_id": EP_ID, "host": "slagg"
        })
        database.files.update_one.assert_not_called()
        database.episodes.update_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
