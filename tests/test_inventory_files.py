"""Offline tests for local PWR media inventory, identification and Atlas safety."""
import datetime as dt
import importlib.machinery
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

from inventory import scan as identify

ROOT = pathlib.Path(__file__).resolve().parents[1]
cli = importlib.machinery.SourceFileLoader(
    "pvr_inventory_files", str(ROOT / "inventory_files")
).load_module()
SERIES = "rugby-womens-pwr"
EP = {"_id": SERIES + ":pwr-289126@mbligh.github.io",
      "series_id": SERIES, "season": 2025, "episode": 101,
      "scheduled": dt.datetime(2025, 10, 24, 18, 30, tzinfo=dt.timezone.utc),
      "nfo_path": "Rugby_Womens_PWR/Season_2025/Rugby_Womens_PWR-s2025e101-Harlequins_v_Loughborough_Lightning_20251024.nfo"}
EP2 = {"_id": SERIES + ":pwr-289200@mbligh.github.io",
       "series_id": SERIES, "season": 2025, "episode": 2001,
       "scheduled": dt.datetime(2026, 6, 28, 14, tzinfo=dt.timezone.utc),
       "nfo_path": "Rugby_Womens_PWR/Season_2025/Rugby_Womens_PWR-s2025e2001-Saracens_v_Trailfinders_Women_20260628.nfo"}
RELEASE = {"_id": "bbc:m0034hxp", "episode_id": EP["_id"]}
FRAME = {
    "format": {"duration": "7200.25", "size": "1000000000", "bit_rate": "2000000",
               "format_name": "matroska,webm"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "width": 1920, "height": 1080, "avg_frame_rate": "60000/1001",
         "bit_rate": "1800000"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
    ],
}


def sidecar(path, season="2025", episode="101", show="Rugby Womens PWR"):
    root = ET.Element("episodedetails")
    for tag, val in (("showtitle", show), ("season", season), ("episode", episode)):
        ET.SubElement(root, tag).text = val
    ET.ElementTree(root).write(path.with_suffix(".nfo"), encoding="unicode")


def media(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake media, ffprobe injected")
    return path


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.by_number, self.links = identify.make_indexes([EP, EP2], [RELEASE])

    def check(self, p):
        return identify.identify(p, self.by_number, self.links)

    def test_canonical_filename_without_sidecar(self):
        p = media(self.root / "Rugby_Womens_PWR/Season_2025" /
                  "Rugby_Womens_PWR-s2025e101-Harlequins_v_Loughborough_Lightning.mkv")
        episode, uri, evidence, error = self.check(p)
        self.assertEqual(episode["_id"], EP["_id"])
        self.assertIsNone(uri)
        self.assertEqual(evidence, ["filename"])
        self.assertIsNone(error)

    def test_nfo_only_match_and_video_extension(self):
        p = media(self.root / "Rugby_Womens_PWR" / "final.mp4")
        sidecar(p, episode="2001")
        result = self.check(p)
        self.assertEqual(result[0]["_id"], EP2["_id"])
        self.assertEqual(result[2], ["nfo"])

    def test_incoming_bbc_pid_with_no_series_name(self):
        p = media(self.root / "incoming" / "PremiershipWomensRugby-m0034hxp.mkv")
        ep, uri, evidence, error = self.check(p)
        self.assertEqual(ep["_id"], EP["_id"])
        self.assertEqual(uri, "bbc:m0034hxp")
        self.assertEqual(evidence, ["release_id"])
        self.assertIsNone(error)

    def test_unrelated_program_not_matched_by_season_number(self):
        p = media(self.root / "incoming" / "Completely_Another_Show-s2025e101.mkv")
        self.assertEqual(self.check(p), (None, None, [], None))
        p2 = media(self.root / "incoming" / "Show-s2025e101-m0034hxp.mkv")
        self.assertIn("without series identity", self.check(p2)[3])

    def test_mismatched_sidecar_and_folder_are_conflicts(self):
        p = media(self.root / "Rugby_Womens_PWR/Season_2025" /
                  "Rugby_Womens_PWR-s2025e101-bad.mkv")
        sidecar(p, episode="2001")
        self.assertIn("disagree", self.check(p)[3])
        sidecar(p, season="2026", episode="101")
        self.assertIn("disagree", self.check(p)[3])
        sidecar(p, show="Different TV Show")
        self.assertIn("showtitle", self.check(p)[3])

    def test_malformed_nfo_fails_closed(self):
        p = media(self.root / "Rugby_Womens_PWR/Season_2025" /
                  "Rugby_Womens_PWR-s2025e101-bad.mkv")
        p.with_suffix(".nfo").write_text("<broken>")
        self.assertIn("Invalid sidecar", self.check(p)[3])

    def test_ignore_2024_and_duplicate_canonical_number(self):
        p = media(self.root / "Rugby_Womens_PWR/Season_2024" /
                  "Rugby_Womens_PWR-s2024e101.mkv")
        self.assertIn("not found", self.check(p)[3])
        collision = dict(EP, _id="another")
        unique, linked = identify.make_indexes([EP, collision], [])
        p2 = media(self.root / "Rugby_Womens_PWR/Season_2025" /
                   "Rugby_Womens_PWR-s2025e101.mkv")
        self.assertIn("not found", identify.identify(p2, unique, linked)[3])

    def test_does_not_follow_symlink_directories_or_files(self):
        video = media(self.root / "Rugby_Womens_PWR/Season_2025" /
                      "Rugby_Womens_PWR-s2025e101.mkv")
        (video.parent / "link.mkv").symlink_to(video)
        (self.root / "alias").symlink_to(video.parent, target_is_directory=True)
        self.assertEqual(list(identify.walk_videos([self.root])), [video])
        with self.assertRaisesRegex(ValueError, "not a readable directory"):
            list(identify.walk_videos([self.root / "nonexistent"]))


class ProbeTests(unittest.TestCase):
    def test_media_fields_and_framerate(self):
        def execute(cmd, **kwargs):
            self.assertIn("-show_entries", cmd)
            self.assertEqual(kwargs["timeout"], 30)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(FRAME), "")
        x = identify.probe_video("/some/path.mkv", execute=execute)
        self.assertEqual(x["duration_seconds"], 7200.25)
        self.assertEqual(x["size_bytes"], 1000000000)
        self.assertEqual(x["video"]["height"], 1080)
        self.assertAlmostEqual(x["video"]["fps"], 59.94, places=2)
        self.assertEqual(x["audio_tracks"], 1)
        self.assertEqual(x["audio_codecs"], ["aac"])

    def test_rejects_failed_or_audio_only_media(self):
        for out, code, error in [
            ("", 1, "bad input"),
            (json.dumps({"streams": [{"codec_type": "audio"}],
                         "format": {"duration": "10"}}), 0, ""),
            (json.dumps({"streams": [{"codec_type": "video"}],
                         "format": {"duration": "N/A"}}), 0, ""),
        ]:
            with self.subTest(out=out):
                def execute(cmd, **kwargs):
                    return subprocess.CompletedProcess(cmd, code, out, error)
                with self.assertRaises(ValueError):
                    identify.probe_video("video.mkv", execute=execute)


class FakeDB:
    def __init__(self, episodes, releases):
        self.episodes = MagicMock()
        self.episodes.find.return_value = episodes
        self.releases = MagicMock()
        self.releases.find.return_value = releases
        self.files = MagicMock()
        self.files.find.return_value = []
        self.files.bulk_write.return_value = None


class InventoryCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "Rugby_Womens_PWR"
        self.valid = media(self.root / "Season_2025" /
                           "Rugby_Womens_PWR-s2025e101-example.mkv")
        self.bad = media(self.root / "Season_2025" /
                         "Rugby_Womens_PWR-s2025e2001-example.mkv")
        sidecar(self.bad, episode="101")
        self.db = FakeDB([EP, EP2], [RELEASE])

    def test_preview_no_writes_and_no_probe_option(self):
        output, summary = io.StringIO(), io.StringIO()
        result = cli.scan(self.db, roots=[self.root], probe=False,
                          hostname="slagg", stream=output, summary_stream=summary)
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["unprobed"], 1)
        self.assertEqual(result["conflict"], 1)
        self.assertEqual(result["written"], 0)
        self.db.files.bulk_write.assert_not_called()
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_apply_records_only_verified_video_and_is_non_destructive(self):
        import unittest.mock
        with unittest.mock.patch.object(cli, "probe_video",
                                        return_value={"video": {"codec": "h264"},
                                                      "duration_seconds": 7200.0}):
            result = cli.scan(self.db, roots=[self.root], apply=True,
                              hostname="slagg", stream=io.StringIO(),
                              summary_stream=io.StringIO())
        self.assertEqual(result["present"], 1)
        self.assertEqual(result["conflict"], 1)
        self.assertEqual(result["written"], 1)
        ops = self.db.files.bulk_write.call_args.args[0]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]._doc["$set"]["episode_id"], EP["_id"])
        self.assertTrue(ops[0]._filter["_id"].startswith("slagg:"))
        self.assertEqual(ops[0]._doc["$setOnInsert"].keys(), {"first_seen"})
        self.assertFalse(any("delete" in str(op).lower() for op in ops))

    def test_unseen_report_is_scope_limited_and_does_not_write(self):
        from unittest.mock import patch
        with patch.object(cli, "probe_video", return_value={
                "video": {"codec": "h264"}, "duration_seconds": 7200.0}):
            output, summary = io.StringIO(), io.StringIO()
            report = cli.scan(self.db, roots=[self.root], hostname="slagg",
                              show_unseen=True, stream=output,
                              summary_stream=summary)
        self.assertEqual(report["episodes_with_files"], 1)
        self.assertEqual(report["past_episodes_not_found_in_roots"], 1)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        unseen = [x for x in messages if x["status"] == "not_found_in_roots"]
        self.assertEqual(len(unseen), 1)
        self.assertEqual(unseen[0]["episode_id"], EP2["_id"])
        self.db.files.bulk_write.assert_not_called()

    def test_existing_different_episode_is_rejected(self):
        doc = {"_id": "slagg:" + str(self.valid), "episode_id": EP2["_id"]}
        self.db.files.find.return_value = [doc]
        with self.assertRaisesRegex(ValueError, "associations conflict"):
            cli.persist(self.db, [{
                "_id": doc["_id"], "episode_id": EP["_id"], "host": "slagg",
                "path": str(self.valid)
            }])
        self.db.files.bulk_write.assert_not_called()

    def test_refuses_apply_without_probe(self):
        with self.assertRaisesRegex(ValueError, "requires ffprobe"):
            cli.scan(self.db, apply=True, probe=False,
                     stream=io.StringIO(), summary_stream=io.StringIO())


if __name__ == "__main__":
    unittest.main()
