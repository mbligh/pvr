"""Offline tests for the F1 calendar source and its key discovery."""
import unittest
from pathlib import Path
from unittest.mock import patch

from schedulers import f1_data as f1


class F1DataTests(unittest.TestCase):
    def setUp(self):
        self.event = {"meetingKey": "1279", "meetingName": "Australian Grand Prix",
                      "meetingTimezone": "Australia/Melbourne", "isTestEvent": False}
        self.listing = {"seasonFromEventListing": "2026", "events": [self.event],
                        "title": "2026 F1 Calendar"}
        self.session = {"meetingSessionKey": 11234, "session": "r",
                        "startTime": "2026-03-08T15:00:00", "gmtOffset": "+11:00"}
        self.detail = {"fomRaceId": "1279", "meetingContext": {
            "meetingKey": "1279", "timetables": [self.session]},
            "seasonContext": {"timetables": [{"session": "unrelated"}]}}

    def test_extracts_public_key_from_escaped_html(self):
        self.assertEqual(
            f1.extract_api_key(r'\"NEXT_PUBLIC_GLOBAL_BROADCAST_APIKEY\":\"EXAMPLE_PUBLIC_KEY_12345\"'),
            "EXAMPLE_PUBLIC_KEY_12345")
        self.assertEqual(
            f1.extract_api_key('"NEXT_PUBLIC_GLOBAL_BROADCAST_APIKEY":"EXAMPLE_PUBLIC_KEY_12345"'),
            "EXAMPLE_PUBLIC_KEY_12345")
        with self.assertRaisesRegex(ValueError, "not found"):
            f1.extract_api_key("<html>no key here</html>")

    def test_joins_meeting_specific_sessions_and_preserves_full_data(self):
        def fake_api(path, api_key, timeout):
            self.assertEqual(api_key, "test-key")
            if path == f1.LISTING_PATH:
                return self.listing
            self.assertEqual(path, f1.MEETING_PATH + "1279")
            return self.detail

        with patch.object(f1, "api_json", side_effect=fake_api):
            result = f1.collect(2026, "test-key", 10)
        self.assertEqual(result["season"], "2026")
        self.assertEqual(result["listing"]["title"], "2026 F1 Calendar")
        self.assertEqual(len(result["meetings"]), 1)
        self.assertEqual(result["meetings"][0]["event"], self.event)
        self.assertEqual(result["meetings"][0]["sessions"], [self.session])
        self.assertEqual(result["meetings"][0]["detail"], self.detail)
        self.assertNotEqual(result["meetings"][0]["sessions"],
                            self.detail["seasonContext"]["timetables"])

    def test_rejects_other_season_and_wrong_meeting(self):
        with patch.object(f1, "api_json", return_value=self.listing):
            with self.assertRaisesRegex(ValueError, "not 2025"):
                f1.collect(2025, "test-key", 10)
        with patch.object(f1, "api_json", side_effect=[self.listing, {
            "meetingContext": {"meetingKey": "9999", "timetables": []}}]):
            with self.assertRaisesRegex(ValueError, "returned meetingKey 9999"):
                f1.collect(2026, "test-key", 10)

    def test_single_meeting_filter_validates_keys(self):
        with patch.object(f1, "api_json", return_value=self.listing):
            with self.assertRaisesRegex(ValueError, "not found"):
                f1.collect(2026, "test-key", 10, meeting_keys=["9999"])


if __name__ == "__main__":
    unittest.main()
