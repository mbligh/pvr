"""Offline tests for F1 episode numbering, timetables and NFO generation."""
import importlib.machinery
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
f1 = importlib.machinery.SourceFileLoader("pvr_import_f1", str(ROOT / "import_f1")).load_module()


class F1ImportTests(unittest.TestCase):
    def payload(self):
        sessions = [{"session": code, "sessionType": "Sprint Qualifying" if code == "s" else "Other",
                     "meetingSessionKey": 1000 + i,
                     "startTime": f"2026-08-28T{9+i:02d}:00:00",
                     "endTime": f"2026-08-28T{10+i:02d}:00:00",
                     "gmtOffset": "+02:00", "timezone": "Europe/Amsterdam"}
                    for i, code in enumerate(["p1", "p2", "p3", "ss", "s", "q", "r"])]
        return {"season": "2026", "meetings": [
            {"event": {"meetingName": "Testing", "meetingKey": "900",
                       "isTestEvent": True, "roundText": "TESTING"}},
            {"event": {"meetingName": "Dutch Grand Prix", "meetingKey": "1292",
                       "roundText": "ROUND 12", "meetingTimezone": "Europe/Amsterdam",
                       "circuitOfficialName": "Circuit Zandvoort",
                       "url": "/en/racing/2026/netherlands", "isTestEvent": False},
             "sessions": sessions, "detail": {"meetingContext": {"meetingKey": "1292"}}}]}

    def test_offsets_testing_stable_identity(self):
        result = f1.build_episodes(self.payload())
        self.assertEqual(len(result), 7)
        self.assertEqual([e["episode"] for e in result], list(range(121, 128)))
        self.assertEqual(result[4]["_id"], "f1:1004")
        self.assertEqual(result[4]["session_name"], "Sprint")
        self.assertEqual(result[4]["metadata_provider"], "formula1/official")
        self.assertEqual(result[-1]["title"], "Dutch Grand Prix - Race")
        self.assertEqual(result[-1]["scheduled"].isoformat(), "2026-08-28T13:00:00+00:00")
        self.assertIn("<episode>127</episode>", result[-1]["nfo"])
        self.assertIn("Formula_1-s2026e127", result[-1]["nfo_path"])

    def test_sparse_episode_numbers(self):
        data = self.payload()
        data["meetings"][1]["sessions"] = [e for e in data["meetings"][1]["sessions"]
                                            if e["session"] in ("p1", "ss", "s", "q", "r")]
        self.assertEqual([e["episode"] for e in f1.build_episodes(data)],
                         [121, 124, 125, 126, 127])

    def test_duplicate_episode_rejected(self):
        data = self.payload()
        data["meetings"][1]["sessions"].append(dict(data["meetings"][1]["sessions"][0],
                                                   meetingSessionKey=9999))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            f1.build_episodes(data)

    def test_bad_offset_or_session_rejected(self):
        data = self.payload()
        data["meetings"][1]["sessions"][0]["gmtOffset"] = "+01:00"
        with self.assertRaisesRegex(ValueError, "offset"):
            f1.build_episodes(data)
        data = self.payload()
        data["meetings"][1]["sessions"][0]["session"] = "bad"
        with self.assertRaisesRegex(ValueError, "unknown"):
            f1.build_episodes(data)

    def test_xml_escaping(self):
        xml = f1.xml_nfo("Grand Prix & Qualifying", 2026, 127, "2026-08-28", "Circuit & cars")
        self.assertIn("Grand Prix &amp; Qualifying", xml)
        self.assertIn("Circuit &amp; cars", xml)


if __name__ == "__main__":
    unittest.main()
