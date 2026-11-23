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

    def discover(self, sources, **kwargs):
        return {key: self.releases[key] for key in sources}

    def test_preview_validates_sources_but_makes_no_atlas_writes(self):
        result = CLI.run(self.db, discover=self.discover, output=io.StringIO())
        self.assertEqual(result["total_releases"], 4)
        self.assertEqual(result["releases"], {"bbc": 1, "youtube": 1, "rugbypass": 1, "rugby24": 1})
        self.assertEqual(result["mode"], "preview")
        self.db.releases.bulk_write.assert_not_called()
        self.db.releases.update_one.assert_not_called()

    def test_source_mismatch_aborts_before_any_writes(self):
        self.releases["bbc"][0]["source"] = "youtube"
        with self.assertRaisesRegex(ValueError, "Release URI does not match source"):
            CLI.run(self.db, apply=True, discover=self.discover,
                    output=io.StringIO())
        self.db.releases.bulk_write.assert_not_called()

    def test_duplicate_uris_are_rejected(self):
        self.releases["youtube"].append(dict(self.releases["youtube"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate release URI"):
            CLI.run(self.db, discover=self.discover, output=io.StringIO())

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
        with patch.object(CLI, "load_file", side_effect=[
                indexer, matcher, national, prem, championship]):
            report = CLI.run(self.db, apply=True, discover=self.discover,
                             output=io.StringIO())
        self.assertEqual(indexer.upsert_releases.call_count, 4)
        matcher.run.assert_called_once()
        national.run.assert_called_once()
        prem.run.assert_called_once()
        championship.run.assert_called_once()
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

    def test_provider_argument_validation(self):
        with self.assertRaises(ValueError):
            CLI.run(self.db, sources=("unknown",), discover=self.discover)
        with self.assertRaises(ValueError):
            CLI.discover_sources(("youtube",), youtube_limit=0)


if __name__ == "__main__":
    unittest.main()
