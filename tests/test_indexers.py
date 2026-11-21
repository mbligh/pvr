"""Offline tests for BBC/YouTube discovery and the common JSONL ingest contract."""
import importlib.machinery
import io
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    return importlib.machinery.SourceFileLoader(name, str(ROOT / path)).load_module()


bbc = load("indexers/bbc", "pvr_index_bbc")
youtube = load("indexers/youtube", "pvr_index_youtube")
ingest = load("index_releases", "pvr_release_ingest")


class BBCIndexTests(unittest.TestCase):
    def example(self):
        return {"type": "episode", "id": "m0034hxp",
                "title": "Premiership Women's Rugby",
                "subtitle": "2026/27: Loughborough Lightning v Trailfinders",
                "release_date": "20 Nov 2026",
                "tleo_id": "m001mnb5"}

    def test_release_fields_and_identity(self):
        item = bbc.convert_episode(self.example(), "m001mnb5")
        self.assertEqual(item["source"], "bbc")
        self.assertEqual(item["id"], "m0034hxp")
        self.assertEqual(item["uri"], "bbc:m0034hxp")
        self.assertEqual(item["published"], "2026-11-20")
        self.assertIn("Loughborough", item["title"])

    def test_discover_deduplicates_and_filters(self):
        urls = []
        def fake(url):
            urls.append(url)
            return {"programme_episodes": {"count": 1, "elements": [
                self.example(), dict(self.example())]}}
        result = bbc.discover("m001mnb5", search="lightning", fetch=fake)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(urls), 1)
        self.assertIn("page=1", urls[0])
        self.assertEqual(bbc.discover("m001mnb5", search="no match", fetch=fake), [])

    def test_invalid_response_rejected(self):
        with self.assertRaises(ValueError):
            bbc.discover("m001mnb5", fetch=lambda _: {"something_else": []})
        self.assertIsNone(bbc.convert_episode({"id": "bad", "title": "Invalid"}, "m001mnb5"))


class YouTubeIndexTests(unittest.TestCase):
    def test_archived_stream_and_upcoming_excluded(self):
        replay = {"id": "bdxiq73pR-I",
                  "title": "LIVE JAECOO PWR Loughborough Lightning vs Trailfinders Women",
                  "live_status": "was_live", "duration": 7920}
        pending = {"id": "aaaaaaaaaaa", "title": "Upcoming match",
                   "live_status": "is_upcoming"}
        def fake(command, **kwargs):
            self.assertIn("--flat-playlist", command)
            self.assertIn("--playlist-end", command)
            return subprocess.CompletedProcess(command, 0,
                stdout=json.dumps(replay) + "\n" + json.dumps(pending) + "\n",
                stderr="")
        releases = youtube.discover(search="Lightning", execute=fake)
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["uri"], "youtube:bdxiq73pR-I")
        self.assertEqual(releases[0]["duration"], 7920)
        self.assertTrue(releases[0]["available"])
        self.assertNotIn("available", youtube.convert_video({
            "id": "bdxiq73pR-I", "title": "Unclassified"}))

    def test_failures_do_not_emit_partial_data(self):
        def fake(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")
        with self.assertRaises(RuntimeError):
            youtube.discover(execute=fake)
        self.assertIsNone(youtube.convert_video({
            "id": "aaaaaaaaaaa", "title": "Live", "live_status": "is_live"}))


class ReleaseImporterTests(unittest.TestCase):
    def test_validate_pvr_releases_and_source_id(self):
        item = {"source": "bbc", "id": "m0034hxp",
                "uri": "bbc:m0034hxp", "title": "A match"}
        self.assertEqual(ingest.validate(item), item)
        for data in [dict(item, uri="youtube:m0034hxp"),
                     dict(item, id="bad:identifier"),
                     dict(item, title=""),
                     dict(item, available="yes")]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                ingest.validate(data)

    def test_record_input_atomic_and_operational_fields_excluded(self):
        first = {"source": "youtube", "id": "bdxiq73pR-I",
                 "uri": "youtube:bdxiq73pR-I", "title": "PWR match",
                 "episode_id": "untrusted", "download_status": "success"}
        records = ingest.read_records(io.StringIO(json.dumps(first) + "\n"))
        self.assertEqual(set(records), {"youtube:bdxiq73pR-I"})
        self.assertNotIn("episode_id", records[first["uri"]])
        self.assertNotIn("download_status", records[first["uri"]])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ingest.read_records(io.StringIO(
                json.dumps(first) + "\n" + json.dumps(first) + "\n"))


if __name__ == "__main__":
    unittest.main()
