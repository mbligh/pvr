"""Offline tests for read-only multi-provider catalogue refresh and matching."""
import importlib.machinery
import importlib.util
import io
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pvr_refresh_releases_tests", str(ROOT / "refresh_releases"))
spec = importlib.util.spec_from_loader("pvr_refresh_releases_tests", loader)
CLI = importlib.util.module_from_spec(spec)
loader.exec_module(CLI)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.releases = {
            "bbc": [{"source": "bbc", "id": "m0034hxp",
                     "uri": "bbc:m0034hxp", "title": "Loughborough v Trailfinders"}],
            "youtube": [{"source": "youtube", "id": "A1BcD2EfG3H",
                         "uri": "youtube:A1BcD2EfG3H",
                         "title": "Sale Sharks vs Leicester Tigers"}],
            "rugbypass": [{"source": "rugbypass", "id": "1048390",
                           "uri": "rugbypass:1048390",
                           "title": "Scotland v New Zealand Full Match Replay"}],
            "rugby24": [{"source": "rugby24", "id": "northampton-v-gloucester",
                         "uri": "rugby24:northampton-v-gloucester",
                         "title": "Northampton v Gloucester - Full Match Replay"}],
        }

    def run_with_mock_matchers(self, **kwargs):
        """Test refresh state transitions without requiring fixture imports."""
        indexer = CLI.load_file("pvr_state_indexer", "index_releases")
        indexer.upsert_releases = MagicMock()
        def load(name, path):
            if path == "index_releases":
                return indexer
            matcher = MagicMock()
            matcher.run.return_value = {"linked": 0}
            return matcher
        with patch.object(CLI, "load_file", side_effect=load):
            return CLI.run(self.db, discover=self.discover,
                           output=io.StringIO(), **kwargs)

    def discover(self, sources, **kwargs):
        return {key: self.releases[key] for key in sources}

    def test_preview_validates_sources_but_makes_no_atlas_writes(self):
        result = CLI.run(self.db, rugby24_category="all", discover=self.discover, output=io.StringIO())
        self.assertEqual(result["total_releases"], 4)
        self.assertEqual(result["releases"], {"bbc": 1, "youtube": 1, "rugbypass": 1, "rugby24": 1})
        self.assertEqual(result["mode"], "preview")
        self.db.releases.bulk_write.assert_not_called()
        self.db.releases.update_one.assert_not_called()
        self.db.indexer_poll_state.update_one.assert_not_called()

    def test_source_mismatch_aborts_before_any_writes(self):
        self.releases["bbc"][0]["source"] = "youtube"
        with self.assertRaisesRegex(ValueError, "Release URI does not match source"):
            CLI.run(self.db, apply=True, rugby24_category="all", discover=self.discover,
                    output=io.StringIO())
        self.db.releases.bulk_write.assert_not_called()

    def test_duplicate_uris_are_rejected(self):
        self.releases["youtube"].append(dict(self.releases["youtube"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate release URI"):
            CLI.run(self.db, rugby24_category="all", discover=self.discover, output=io.StringIO())

    def test_apply_upserts_then_matches_fixtures(self):
        indexer = CLI.load_file("pvr_test_indexer", "index_releases")
        indexer.upsert_releases = MagicMock(return_value={"upserted": 1})
        matcher = MagicMock()
        matcher.run.return_value = {"linked": 2, "fixtures": 150}
        national = MagicMock()
        national.run.return_value = {"linked": 12, "fixtures": 166}
        prem = MagicMock()
        prem.run.return_value = {"linked": 4, "fixtures": 79}
        championship = MagicMock()
        championship.run.return_value = {"linked": 7, "fixtures": 48}
        sixnations = MagicMock()
        sixnations.run.return_value = {"linked": 3}
        with patch.object(CLI, "load_file", side_effect=[
                indexer, matcher, national, prem, championship, sixnations]):
            report = CLI.run(self.db, apply=True, rugby24_category="all", discover=self.discover,
                             output=io.StringIO())
        self.assertEqual(indexer.upsert_releases.call_count, 4)
        matcher.run.assert_called_once()
        national.run.assert_called_once()
        prem.run.assert_called_once()
        championship.run.assert_called_once()
        sixnations.run.assert_called_once()
        self.assertEqual(report["sixnations_matching"]["linked"], 3)
        self.assertTrue(matcher.run.call_args.kwargs["apply"])
        self.assertTrue(national.run.call_args.kwargs["apply"])
        self.assertEqual(report["matching"]["linked"], 2)
        self.assertEqual(report["international_matching"]["linked"], 12)
        self.assertEqual(report["prem_matching"]["linked"], 4)
        self.assertEqual(report["championship_matching"]["linked"], 7)

    def test_rugbypass_jsonl_parsing_and_failure_is_safe(self):
        class Result:
            def __init__(self, code=0, stdout=""):
                self.returncode = code
                self.stdout = stdout
                self.stderr = "private auth header"
        with self.assertRaisesRegex(RuntimeError, "exit 2"):
            CLI.scan_rugbypass(execute=lambda *args, **kw: Result(2))
        row = '{"source":"rugbypass","id":"1048390","uri":"rugbypass:1048390","title":"Example"}\n'
        result = CLI.scan_rugbypass(execute=lambda *args, **kw: Result(stdout=row))
        self.assertEqual(result[0]["uri"], "rugbypass:1048390")
        with self.assertRaisesRegex(ValueError, "non-JSON"):
            CLI.scan_rugbypass(execute=lambda *args, **kw: Result(stdout="not json"))

    def test_rugbypass_scans_dedicated_wxv_playlist(self):
        calls = []
        class Result:
            returncode = 0
            stdout = ('{"source":"rugbypass","id":"1043880","uri":"rugbypass:1043880",'
                      '"title":"South Africa v Ireland | WXV Global Series 2026 | Full Match Replay",'
                      '"published":"2026-10-31T18:22:00Z"}\n')
            stderr = ""
        def execute(cmd, **kw):
            calls.append(cmd)
            return Result()
        releases = CLI.scan_rugbypass(execute=execute, pages=5)
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["id"], "1043880")
        self.assertEqual(calls[0][-2:], ["--playlist", "37547"])
        self.assertEqual(calls[0][1:5], ["--list-games", "--jsonl", "--pages", "5"])

    def test_youtube_refresh_uses_combined_pwr_indexer(self):
        calls = []
        def fake_youtube(**kwargs):
            calls.append(kwargs)
            return self.releases["youtube"]
        result = CLI.discover_sources(("youtube",), yt_discover=fake_youtube)
        self.assertEqual(result["youtube"], self.releases["youtube"])
        self.assertEqual(calls, [{"limit": 100, "binary": "yt-dlp"}])

    def test_rugby24_category_is_forwarded_to_indexer(self):
        fetch = MagicMock(return_value=self.releases["rugby24"])
        out = CLI.discover_sources(("rugby24",), rugby24_pages=1,
                                   rugby24_category="championship",
                                   rugby24_discover=fetch)
        self.assertEqual(out["rugby24"], self.releases["rugby24"])
        fetch.assert_called_once_with(pages=1, category="championship",
                                      cached=None)

    def test_refresh_reads_cached_rugby24_releases_from_atlas(self):
        existing = {
            "_id": "rugby24:northampton-v-gloucester", "source": "rugby24",
            "id": "northampton-v-gloucester",
            "metadata": {"page_url": "https://rugby24.net/northampton-v-gloucester",
                         "embed_url": "https://ok.ru/videoembed/12345678901",
                         "competition": "league"},
        }
        self.db.releases.find.return_value = [existing]
        seen = []
        def scan(sources, **kwargs):
            seen.append(kwargs["rugby24_cached"])
            return {source: self.releases[source] for source in sources}
        CLI.run(self.db, sources=("rugby24",), rugby24_category="prem", discover=scan,
                output=io.StringIO())
        self.assertEqual(seen[0], {existing["_id"]: existing})
        self.db.releases.find.assert_called_once()
        self.db.releases.bulk_write.assert_not_called()


    def test_successful_rugby24_apply_clears_only_scanned_category(self):
        for category, target in (
            ("prem", "rugby24:prem"),
            ("championship", "rugby24:championship"),
            ("sixnations", "rugby24:sixnations"),
        ):
            with self.subTest(category=category):
                self.db.reset_mock()
                report = self.run_with_mock_matchers(
                    sources=("rugby24",), apply=True,
                    rugby24_category=category)
                self.assertEqual(report["mode"], "apply")
                self.assertEqual(report["total_releases"], 1)
                self.db.indexer_poll_state.update_one.assert_called_once()
                args, kwargs = self.db.indexer_poll_state.update_one.call_args
                self.assertEqual(args[0], {"_id": target})
                self.assertEqual(args[1]["$set"]["failures"], 0)
                self.assertEqual(args[1]["$set"]["last_success"],
                                 args[1]["$set"]["last_attempt"])
                self.assertEqual(args[1]["$unset"],
                                 {"retry_not_before": "", "failure_reason": ""})
                self.assertTrue(kwargs["upsert"])

    def test_successful_all_categories_clears_all_backoffs(self):
        self.assertEqual(self.run_with_mock_matchers(
            sources=("rugby24",), apply=True,
            rugby24_category="all")["mode"], "apply")
        self.assertEqual([
            call.args[0]["_id"] for call in
            self.db.indexer_poll_state.update_one.call_args_list
        ], ["rugby24:prem", "rugby24:championship", "rugby24:sixnations"])

    def test_rugby24_preview_and_other_source_apply_do_not_clear_backoff(self):
        CLI.run(self.db, sources=("rugby24",), apply=False,
                rugby24_category="championship", discover=self.discover,
                output=io.StringIO())
        self.db.indexer_poll_state.update_one.assert_not_called()
        self.run_with_mock_matchers(sources=("bbc",), apply=True)
        self.db.indexer_poll_state.update_one.assert_not_called()

    def test_failed_rugby24_refresh_does_not_clear_any_backoff(self):
        def failing_discovery(sources, **kwargs):
            raise RuntimeError("Cloudflare challenge")
        with self.assertRaisesRegex(RuntimeError, "Cloudflare challenge"):
            CLI.run(self.db, sources=("rugby24",), apply=True,
                    rugby24_category="all", discover=failing_discovery,
                    output=io.StringIO())
        self.db.indexer_poll_state.update_one.assert_not_called()

        indexer = CLI.load_file("pvr_test_indexer", "index_releases")
        indexer.upsert_releases = MagicMock()
        matcher = MagicMock()
        prem = MagicMock()
        championship = MagicMock()
        championship.run.side_effect = RuntimeError("Matching failed")
        with patch.object(CLI, "load_file", side_effect=[
                indexer, matcher, prem, championship]):
            with self.assertRaisesRegex(RuntimeError, "Matching failed"):
                CLI.run(self.db, sources=("rugby24",), apply=True,
                        rugby24_category="all", discover=self.discover,
                        output=io.StringIO())
        self.db.indexer_poll_state.update_one.assert_not_called()

    def test_rugby24_requires_explicit_category_before_discovery(self):
        discover = MagicMock()
        for sources in (("rugby24",), CLI.SOURCES):
            with self.subTest(sources=sources):
                with self.assertRaisesRegex(ValueError, "explicit Rugby24 category"):
                    CLI.run(self.db, sources=sources, discover=discover)
                with self.assertRaisesRegex(ValueError, "explicit Rugby24 category"):
                    CLI.discover_sources(sources)
        discover.assert_not_called()
        self.db.releases.find.assert_not_called()
        self.db.releases.bulk_write.assert_not_called()
        with self.assertRaises(SystemExit) as exc:
            CLI.main(["--source", "rugby24"])
        self.assertEqual(exc.exception.code, 2)

    def test_provider_argument_validation(self):
        with self.assertRaises(ValueError):
            CLI.run(self.db, sources=("unknown",), discover=self.discover)
        with self.assertRaises(ValueError):
            CLI.discover_sources(("youtube",), youtube_limit=0)


if __name__ == "__main__":
    unittest.main()
