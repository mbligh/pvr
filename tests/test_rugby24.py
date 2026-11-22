"""Offline Rugby24 indexer, PREM league matcher, and OK.ru download tests."""
import datetime as dt
import importlib.machinery
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock
from types import SimpleNamespace

from downloaders import rugby24
from matchers.prem import match_release

ROOT = pathlib.Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("test_pvr_rugby24_indexer", str(ROOT / "indexers/rugby24"))
spec = importlib.util.spec_from_loader(loader.name, loader)
indexer = importlib.util.module_from_spec(spec)
loader.exec_module(indexer)

LEAGUE_SLUG = "northampton-saints-v-gloucester-full-match-replay-prem-rugby-31-october-2026"
CUP_SLUG = "bristol-bears-v-bath-full-match-replay-premiership-rugby-cup-20-november-2026"
BASE = "https://rugby24.net/"
TITLE = "Northampton Saints v Gloucester - Full Match Replay - PREM Rugby - 31 October 2026"
CUP_TITLE = "Bristol Bears v Bath - Full Match Replay - Premiership Rugby Cup - 20 November 2026"
FIXTURE = {"_id": "Rugby_Mens_Prem:abc", "series_id": "Rugby_Mens_Prem",
           "home": "Northampton Saints", "away": "Gloucester Rugby",
           "season": 2026, "episode": 505,
           "scheduled": dt.datetime(2026, 10, 31, 17, 30, tzinfo=dt.timezone.utc)}


class IndexerTests(unittest.TestCase):
    def test_list_and_embeds_include_cup_but_do_not_misclassify(self):
        pages = {
            indexer.DEFAULT_URL:
                '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>',
            indexer.DEFAULT_URL + "?page2":
                '<h3><a href="/' + CUP_SLUG + '">' + CUP_TITLE + '</a></h3>',
            BASE + LEAGUE_SLUG: '<iframe src="//ok.ru/videoembed/12345678901"></iframe>',
            BASE + CUP_SLUG: '<iframe src="https://ok.ru/videoembed/16552117471956"></iframe>',
        }
        seen = []
        def fetch(url):
            seen.append(url)
            return pages[url]
        rows = indexer.discover(pages=2, fetch=fetch)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["published"], "2026-10-31")
        self.assertEqual(rows[0]["metadata"]["competition"], "league")
        self.assertEqual(rows[0]["metadata"]["embed_url"],
                         "https://ok.ru/videoembed/12345678901")
        self.assertEqual(rows[1]["metadata"]["competition"], "cup")
        self.assertEqual(rows[1]["published"], "2026-11-20")
        self.assertIn(indexer.DEFAULT_URL + "?page2", seen)
        self.assertEqual(indexer.article_slug("https://not-rugby24.net/foo"), None)

    def test_indexer_fails_on_empty_or_unplayable_pages(self):
        with self.assertRaisesRegex(ValueError, "no full-match"):
            indexer.discover(pages=1, fetch=lambda url: "<html></html>")
        with self.assertRaisesRegex(ValueError, "OK.ru embeds"):
            indexer.discover(pages=1, fetch=lambda url:
                '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>')
        self.assertIsNone(indexer.embed_url('<iframe src="https://evil.tld/videoembed/123456"></iframe>'))


    def test_browser_fetch_uses_session_and_reports_url_on_403(self):
        class Session:
            def __init__(self, status=200):
                self.status = status
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return SimpleNamespace(
                    status_code=self.status, url=url,
                    content=b"<html>article</html>",
                    headers={"server": "cloudflare", "cf-mitigated": "challenge"}
                )

        session = Session()
        page = indexer.fetch_html(indexer.DEFAULT_URL, session=session)
        self.assertIn("<html>", page)
        self.assertEqual(session.calls[0][1]["timeout"], 30)
        with self.assertRaisesRegex(ValueError, "non-Rugby24"):
            indexer.fetch_html("https://evil.example/", session=session)
        session.status = 403
        with self.assertRaisesRegex(RuntimeError,
                                   "HTTP 403 at /premiership-rugby"):
            indexer.fetch_html(indexer.DEFAULT_URL, session=session)
        self.assertEqual(len(session.calls), 2)

    def test_browser_redirect_to_foreign_host_is_rejected(self):
        class Session:
            def get(self, url, **kwargs):
                return SimpleNamespace(
                    status_code=200, url="https://other.example/",
                    content=b"fake", headers={}
                )
        with self.assertRaisesRegex(RuntimeError, "redirected"):
            indexer.fetch_html(indexer.DEFAULT_URL, session=Session())

    def test_fixture_matching_requires_league_teams_and_date(self):
        release = {"source": "rugby24", "title": TITLE, "published": "2026-10-31",
                   "metadata": {"competition": "league"}}
        result = match_release(release, [FIXTURE])
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["episode_id"], FIXTURE["_id"])
        cup = dict(release, title=CUP_TITLE, published="2026-11-20",
                   metadata={"competition": "cup"})
        self.assertEqual(match_release(cup, [FIXTURE])["status"], "not_league")
        self.assertEqual(match_release(dict(release, published="2026-11-10"),
                                       [FIXTURE])["status"], "no_fixture")
        other = dict(FIXTURE, _id="Rugby_Mens_Prem:def")
        self.assertEqual(match_release(release, [FIXTURE, other])["status"], "ambiguous")


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = pathlib.Path(self.temp.name) / "prem.mkv"
        self.uri = "rugby24:" + LEAGUE_SLUG
        self.embed = "https://ok.ru/videoembed/12345678901"
        self.page = BASE + LEAGUE_SLUG

    def test_checked_embed_and_atomic_staging(self):
        from importlib.machinery import SourceFileLoader
        cli = SourceFileLoader("pvr_rugby24_dl", str(ROOT / "download_release")).load_module()
        db = MagicMock()
        db.releases.find_one.return_value = {
            "_id": self.uri, "source": "rugby24", "episode_id": FIXTURE["_id"],
            "metadata": {"page_url": self.page, "embed_url": self.embed}}
        db.episodes.find_one.return_value = FIXTURE
        db.files.find.return_value = []
        plan = cli.plan(db, self.uri, self.target, hostname="slagg")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["adapter"], "rugby24")
        self.assertEqual(plan["source_url"], self.embed)
        def execute(cmd, **kwargs):
            self.assertEqual(cmd[-1], self.embed)
            self.assertEqual(cmd[cmd.index("--referer") + 1], self.page)
            target = pathlib.Path(cmd[cmd.index("--output") + 1].replace("%(ext)s", "mkv"))
            target.write_bytes(b"mkv bytes")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        media = {"duration_seconds": 7000, "video": {"height": 720, "fps": 25}}
        result = rugby24.download(
            self.uri, self.embed, self.target, referer=self.page,
            execute=execute, probe=lambda path, *, binary: media)
        self.assertEqual(result, media)
        self.assertEqual(self.target.read_bytes(), b"mkv bytes")
        with self.assertRaises(FileExistsError):
            rugby24.download(self.uri, self.embed, self.target)
        db.releases.find_one.return_value["metadata"]["embed_url"] = "https://evil.example/video"
        with self.assertRaises(ValueError):
            cli.plan(db, self.uri, pathlib.Path(self.temp.name) / "second.mkv", hostname="slagg")

    def test_reject_bad_uris(self):
        for uri in ("rugby24:../abc", "rugby24:http://ok.ru", "rugby24:a?b"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                rugby24.rugby24_id(uri)
        with self.assertRaises(ValueError):
            rugby24.download(self.uri, "https://bad.site/videoembed/123456", self.target)


if __name__ == "__main__":
    unittest.main()
