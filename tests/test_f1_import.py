"""Offline tests for F1 episode numbering, timetables and NFO generation."""
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from schedulers import f1


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

    def test_live_source_calls_scheduler_module_directly(self):
        expected = self.payload()
        with patch.object(f1.f1_data, "fetch_season", return_value=expected) as fetch:
            self.assertIs(f1.read_source(None, 2026), expected)
        fetch.assert_called_once_with(2026)

    def test_saved_source_does_not_fetch_live(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            payload = self.payload()
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(f1.f1_data, "fetch_season") as fetch:
                self.assertEqual(f1.read_source(path, 2026), payload)
            fetch.assert_not_called()

    def test_import_cli_reports_live_source_failures_without_traceback(self):
        import contextlib
        import io
        err = io.StringIO()
        with patch.object(f1.f1_data, "fetch_season",
                          side_effect=RuntimeError("F1 API unreachable")), \
             contextlib.redirect_stderr(err):
            self.assertEqual(f1.main(["--year", "2026"]), 1)
        self.assertIn("F1 API unreachable", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_offsets_testing_stable_identity(self):
        result = f1.build_episodes(self.payload())
        self.assertEqual(len(result), 7)
        self.assertEqual([e["episode"] for e in result], list(range(121, 128)))
        self.assertEqual(result[4]["_id"], "Formula_1:1004")
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

    def test_us_central_alias_without_legacy_tzdb_link(self):
        # Some Debian installations have America/Chicago but not US/Central.
        data = self.payload()
        meeting = data["meetings"][1]
        meeting["event"]["meetingTimezone"] = "US/Central"
        for session in meeting["sessions"]:
            session["timezone"] = "US/Central"
            session["startTime"] = session["startTime"].replace("2026-08-28", "2026-10-23")
            session["endTime"] = session["endTime"].replace("2026-08-28", "2026-10-23")
            session["gmtOffset"] = "-05:00"

        def no_legacy_alias(name):
            if name == "US/Central":
                raise ZoneInfoNotFoundError(name)
            return ZoneInfo(name)

        with patch.object(f1, "ZoneInfo", side_effect=no_legacy_alias):
            episodes = f1.build_episodes(data)
        self.assertEqual(len(episodes), 7)
        self.assertEqual(episodes[0]["timezone"], "America/Chicago")
        self.assertEqual(episodes[0]["scheduled"].isoformat(), "2026-10-23T14:00:00+00:00")

    def test_xml_escaping(self):
        xml = f1.xml_nfo("Grand Prix & Qualifying", 2026, 127, "2026-08-28", "Circuit & cars")
        self.assertIn("Grand Prix &amp; Qualifying", xml)
        self.assertIn("Circuit &amp; cars", xml)


if __name__ == "__main__":
    unittest.main()
