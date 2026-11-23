"""Offline dashboard tests: series coverage, date filtering, local files and HTTP."""
import datetime as dt
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dashboard.data import DashboardData, SERIES, event_time, quality
from dashboard.web import QualityLookup, make_handler, render_page
from filing.publish import SERIES_PATHS

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, 14, 0, tzinfo=UTC)
TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def episode(series="Rugby_Womens_PWR", *, number=101, when=NOW, title="Home vs Away"):
    item = {
        "_id": f"{series}:{number}", "series_id": series,
        "season": 2026, "episode": number, "scheduled": when,
        "title": title, "status": "scheduled",
    }
    if number is not None:
        stem = f"{series}-s2026e{number:03d}-Fixture"
        item["nfo_path"] = f"{series}/Season_2026/{stem}.nfo"
    return item


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def _matched(self, item, query):
        for key, value in query.items():
            field = item.get(key)
            if isinstance(value, dict) and "$in" in value:
                if field not in value["$in"]:
                    return False
            elif field != value:
                return False
        return True

    def find(self, query, fields=None):
        self.calls.append(("find", query, fields))
        return [dict(d) for d in self.docs if self._matched(d, query)]

    def find_one(self, query, fields=None):
        self.calls.append(("find_one", query, fields))
        return next((dict(d) for d in self.docs if self._matched(d, query)), None)


class FakeDB:
    def __init__(self, episodes, files=()):
        self.episodes = FakeCollection(episodes)
        self.files = FakeCollection(files)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sports"
        self.root.mkdir()
        self.events = [
            episode(number=101, when=NOW - dt.timedelta(hours=3)),
            episode(number=102, when=NOW + dt.timedelta(days=3),
                    title="A <script>alert(1)</script> & B"),
            episode(number=103, when=NOW - dt.timedelta(days=5)),
            episode(number=104, when=NOW + dt.timedelta(days=8)),
        ]
        self.files = []
        self.db = FakeDB(self.events, self.files)
        self.data = DashboardData(self.db, self.root, TZ, hostname="slagg")

    def make_media(self, ep, *, size=b"recorded", media=None, extension=".mkv",
                   inventory=True):
        relative = Path(ep["nfo_path"]).with_suffix(extension)
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(size)
        if inventory:
            doc = {"_id": "slagg:" + str(path), "episode_id": ep["_id"],
                   "path": str(path), "host": "slagg",
                   "size_bytes": path.stat().st_size,
                   "mtime_ns": path.stat().st_mtime_ns,
                   "media": media, "release_uri": "youtube:ABC",
                   "file_state": "published"}
            self.files.append(doc)
        return path

    def test_series_selector_exactly_matches_canonical_filing_series(self):
        self.assertEqual(len(SERIES), len(SERIES_PATHS))
        self.assertEqual(SERIES[0], "Formula_1")
        self.assertEqual(SERIES[-1], "Rugby_Womens_WXV")
        self.assertEqual(set(SERIES), set(SERIES_PATHS))

    def test_newest_first_and_exact_one_week_future_cutoff(self):
        self.events.append(episode(number=105, when=NOW + dt.timedelta(days=7)))
        rows = self.data.rows("Rugby_Womens_PWR", now=NOW)
        self.assertEqual([r["id"] for r in rows],
                         ["Rugby_Womens_PWR:105", "Rugby_Womens_PWR:102",
                          "Rugby_Womens_PWR:101", "Rugby_Womens_PWR:103"])
        self.assertNotIn("Rugby_Womens_PWR:104", [r["id"] for r in rows])
        self.assertEqual(rows[1]["status"], "Upcoming")
        self.assertEqual(rows[2]["status"], "Missing")
        self.assertEqual(rows[2]["number"], "s2026e101")

    def test_unnumbered_and_all_day_event_are_included(self):
        untimed = episode(number=None, when=None, title="Unnumbered fixture")
        untimed["scheduled_date"] = "2026-11-21"
        self.events.append(untimed)
        self.events.append(episode(number=102, when=None, title="No date"))
        self.events.append(episode(number=103, when=None, title="Too far"))
        self.events[-1]["scheduled_date"] = "2026-12-12"
        rows = self.data.rows("Rugby_Womens_PWR", now=NOW)
        by_title = {row["title"]: row for row in rows}
        self.assertIn("Unnumbered fixture", by_title)
        self.assertEqual(by_title["Unnumbered fixture"]["number"], "Unnumbered")
        self.assertIn("21 Nov 2026", by_title["Unnumbered fixture"]["date"])
        self.assertEqual(by_title["No date"]["date"], "Date unknown")
        self.assertNotIn("Too far", by_title)

    def test_canonical_installed_even_without_atlas_file_record(self):
        self.make_media(self.events[0], inventory=False)
        rows = self.data.rows("Rugby_Womens_PWR", now=NOW)
        installed = next(row for row in rows if row["id"] == self.events[0]["_id"])
        self.assertEqual(installed["status"], "Installed")
        self.assertIsNone(installed["quality"])
        self.assertNotEqual(installed["size"], "—")

    def test_matching_metadata_contains_res_fps_codec_and_source(self):
        media = {"video": {"height": 1080, "width": 1920,
                           "fps": 50.0, "codec": "h264"}}
        self.make_media(self.events[0], media=media)
        row = next(row for row in self.data.rows("Rugby_Womens_PWR", now=NOW)
                   if row["id"] == self.events[0]["_id"])
        self.assertEqual(row["quality"], "1080p · 50 fps · H.264")
        self.assertEqual(row["source"], "youtube")

    def test_matching_stale_metadata_is_not_reused(self):
        path = self.make_media(self.events[0], media={"video": {"height": 1080}})
        self.files[0]["size_bytes"] = path.stat().st_size + 999
        item = self.data.installed(self.events[0], self.files)
        self.assertIsNone(item["quality"])

    def test_only_linked_inventory_can_match_noncanonical_file(self):
        raw = episode(number=None, when=NOW - dt.timedelta(hours=3))
        self.events.append(raw)
        target = self.root / "Rugby_Womens_PWR" / "legacy.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"historical")
        self.files.append({"episode_id": raw["_id"], "path": str(target),
                           "media": {"video": {"height": 720, "fps": 25}}})
        rows = self.data.rows("Rugby_Womens_PWR", now=NOW)
        match = next(r for r in rows if r["id"] == raw["_id"])
        self.assertEqual(match["status"], "Installed")
        self.assertEqual(match["quality"], "720p · 25 fps")
        self.files.clear()
        rows = self.data.rows("Rugby_Womens_PWR", now=NOW)
        match = next(r for r in rows if r["id"] == raw["_id"])
        self.assertEqual(match["status"], "Missing")

    def test_refuses_file_outside_selected_series_or_symlink(self):
        wrong_series = self.root / "Rugby_Mens_Intl" / "wrong.mkv"
        wrong_series.parent.mkdir(parents=True)
        wrong_series.write_bytes(b"other")
        self.files.append({"episode_id": self.events[0]["_id"],
                           "path": str(wrong_series)})
        outside = Path(self.temp.name) / "outside.mkv"
        outside.write_bytes(b"outside")
        self.files.append({"episode_id": self.events[0]["_id"],
                           "path": str(outside)})
        link = self.root / self.events[0]["nfo_path"].replace(".nfo", ".mkv")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        row = next(r for r in self.data.rows("Rugby_Womens_PWR", now=NOW)
                   if r["id"] == self.events[0]["_id"])
        self.assertFalse(row["installed"])

    def test_noncanonical_registered_file_preferred_when_canonical_absent(self):
        self.make_media(self.events[0], extension=".mp4", inventory=True)
        row = next(r for r in self.data.rows("Rugby_Womens_PWR", now=NOW)
                   if r["id"] == self.events[0]["_id"])
        self.assertTrue(row["installed"])

    def test_cancelled_event_remains_in_table(self):
        self.events[0]["status"] = "cancelled"
        row = next(r for r in self.data.rows("Rugby_Womens_PWR", now=NOW)
                   if r["id"] == self.events[0]["_id"])
        self.assertEqual(row["status"], "Cancelled")

    def test_lazy_probe_called_only_for_installed_unindexed_file_and_cached(self):
        path = self.make_media(self.events[0], inventory=False)
        fake = MagicMock(return_value={
            "video": {"height": 2160, "fps": 50, "codec": "hevc"}})
        lookup = QualityLookup(self.data, probe=fake)
        first = lookup.lookup("Rugby_Womens_PWR", self.events[0]["_id"])
        second = lookup.lookup("Rugby_Womens_PWR", self.events[0]["_id"])
        self.assertEqual(first, second)
        self.assertEqual(first["quality"], "2160p · 50 fps · H.265")
        fake.assert_called_once_with(path.resolve(), binary="ffprobe", timeout=12)
        self.assertEqual(lookup.lookup("Rugby_Womens_PWR",
                                        self.events[2]["_id"])["installed"], False)
        fake.assert_called_once()

    def test_html_escapes_untrusted_titles_and_has_all_tabs(self):
        rows = self.data.rows("Rugby_Womens_PWR", now=NOW)
        page = render_page("Rugby_Womens_PWR", rows, timezone=TZ, updated_at=NOW)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertEqual(page.count('<a class="tab'), len(SERIES))
        self.assertIn('aria-current="page"', page)
        self.assertIn("No recordings are modified", page)
        self.assertIn("7 days", page)

    def test_quality_formatter_rejects_invalid_or_nonfinite_values(self):
        self.assertIsNone(quality({"video": {"fps": float("nan")}}))
        self.assertIsNone(quality(None))
        self.assertEqual(quality({"video": {"height": "720", "fps": "59.94",
                                            "codec": "vp9"}}),
                         "720p · 59.94 fps · VP9")

    def test_api_and_static_assets_over_real_local_http(self):
        self.make_media(self.events[0], inventory=False)
        fake = MagicMock(return_value={
            "video": {"height": 1080, "fps": 50, "codec": "h264"}})
        handler = make_handler(self.data, quality_lookup=QualityLookup(self.data,
                                probe=fake), now_fn=lambda: NOW)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, *, binary=False):
                    con = HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                    con.request("GET", path)
                    response = con.getresponse()
                    body = response.read()
                    if not binary:
                        body = body.decode("utf-8")
                    headers = dict(response.getheaders())
                    result = response.status, headers, body
                    con.close()
                    return result

                status, headers, page = request("/?series=Rugby_Womens_PWR")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["Content-Type"])
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn("Rugby_Womens_PWR", page)
                self.assertIn("Installed", page)
                self.assertIn('rel="icon" href="/favicon.ico" type="image/x-icon"', page)
                self.assertIn("img-src 'self'", headers["Content-Security-Policy"])

                status, headers, icon = request("/favicon.ico", binary=True)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "image/x-icon")
                self.assertEqual(icon, (ROOT / "dashboard" / "favicon.ico").read_bytes())
                self.assertEqual(icon[:4], b"\x00\x00\x01\x00")

                status, headers, css = request("/style.css")
                self.assertEqual(status, 200)
                self.assertIn("color-scheme: dark", css)
                status, headers, js = request("/dashboard.js")
                self.assertEqual(status, 200)
                self.assertIn("IntersectionObserver", js)

                endpoint = "/api/quality?" + urlencode({
                    "series": "Rugby_Womens_PWR", "event": self.events[0]["_id"]})
                status, headers, response = request(endpoint)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(response)["quality"],
                                 "1080p · 50 fps · H.264")
                self.assertEqual(request("/api/quality?series=not-real&event=x")[0], 400)
                self.assertEqual(request("/?series=not-real")[0], 400)
                self.assertEqual(request("/bad-path")[0], 404)
                self.assertEqual(json.loads(request("/healthz")[2]), {"ok": True})
            finally:
                server.shutdown()
                thread.join(timeout=4)

    def test_cli_help_is_executable_extensionless(self):
        proc = subprocess.run([sys.executable, str(ROOT / "pvr_dashboard"), "--help"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--bind", proc.stdout)
        self.assertIn("--port", proc.stdout)
        self.assertIn("--sports-root", proc.stdout)


if __name__ == "__main__":
    unittest.main()
