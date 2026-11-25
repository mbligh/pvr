"""Offline Rugby24 indexer, PREM league matcher, and OK.ru download tests."""
import datetime as dt
import importlib.machinery
import importlib.util
import pathlib
import subprocess
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from downloaders import rugby24
from matchers.sixnations import match_release as match_sixnations
from matchers.prem import match_release
from matchers.championship import match_release as match_championship

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
CHAMP_SLUG = "gloucester-v-bordeaux-begles-full-match-replay-16-october-2026-european-champions-cup-rugby"
CHAMP_TITLE = "Gloucester v Bordeaux Begles - Full Match Replay - 16 October 2026 - European Champions Cup Rugby"
SIX_TITLE = "France vs England Six Nations Rugby Full Match Replay 14 March 2026"
SIX_SLUG = "france-vs-england-six-nations-rugby-full-match-replay-14-march-2026"
SIX_FIXTURE = {"_id": "Rugby_Mens_6N:abc", "series_id": "Rugby_Mens_6N",
               "home": "France", "away": "England", "season": 2026, "episode": 501,
               "scheduled": dt.datetime(2026, 3, 14, 20, tzinfo=dt.timezone.utc)}
CHAMP_FIXTURE = {
    "_id": "Rugby_Mens_Championship:epcr-293316@mbligh.github.io",
    "series_id": "Rugby_Mens_Championship",
    "home": "Gloucester Rugby", "away": "Bordeaux-Begles",
    "season": 2026, "episode": 101,
    "scheduled": dt.datetime(2026, 10, 16, 19, tzinfo=dt.timezone.utc),
}
FIXTURE = {"_id": "Rugby_Mens_Prem:abc", "series_id": "Rugby_Mens_Prem",
           "home": "Northampton Saints", "away": "Gloucester Rugby",
           "season": 2026, "episode": 505,
           "scheduled": dt.datetime(2026, 10, 31, 17, 30, tzinfo=dt.timezone.utc)}


class IndexerTests(unittest.TestCase):
    def test_list_and_embeds_include_cup_but_do_not_misclassify(self):
        pages = {
            indexer.PREM_URL:
                '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>',
            indexer.PREM_URL + "?page2":
                '<h3><a href="/' + CUP_SLUG + '">' + CUP_TITLE + '</a></h3>',
            BASE + LEAGUE_SLUG: '<iframe src="//ok.ru/videoembed/12345678901"></iframe>',
            BASE + CUP_SLUG: '<iframe src="https://ok.ru/videoembed/16552117471956"></iframe>',
        }
        seen = []
        def fetch(url):
            seen.append(url)
            return pages[url]
        rows = indexer.discover(indexer.PREM_URL, pages=2, fetch=fetch)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["published"], "2026-10-31")
        self.assertEqual(rows[0]["metadata"]["competition"], "league")
        self.assertEqual(rows[0]["metadata"]["embed_url"],
                         "https://ok.ru/videoembed/12345678901")
        self.assertEqual(rows[1]["metadata"]["competition"], "cup")
        self.assertEqual(rows[1]["published"], "2026-11-20")
        self.assertIn(indexer.PREM_URL + "?page2", seen)
        self.assertEqual(indexer.article_slug("https://not-rugby24.net/foo"), None)

    def test_all_categories_and_no_cup_cross_contamination(self):
        prem = indexer.PREM_URL
        champions = indexer.CHAMPIONS_URL
        pages = {
            prem: '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>'
                  '<h3><a href="/' + CUP_SLUG + '">' + CUP_TITLE + '</a></h3>',
            champions: '<h3><a href="/' + CHAMP_SLUG + '">' + CHAMP_TITLE + '</a></h3>'
                       '<h3><a href="/highlights">European Champions Cup Highlights - Round 1 - 21 October 2026</a></h3>'
                       '<h3><a href="/chall">Example v Rival - Full Match Replay - Challenge Cup</a></h3>',
            BASE + LEAGUE_SLUG: '<iframe src="https://ok.ru/videoembed/12345678901"></iframe>',
            BASE + CUP_SLUG: '<iframe src="https://ok.ru/videoembed/16552117471956"></iframe>',
            BASE + CHAMP_SLUG: '<iframe src="//ok.ru/videoembed/12345678902"></iframe>',
            indexer.SIXNATIONS_URL: '<h3><a href="/' + SIX_SLUG + '">' + SIX_TITLE + '</a></h3>',
            BASE + SIX_SLUG: '<iframe src="//ok.ru/videoembed/12345678903"></iframe>',
        }
        seen = []
        def fetch(url):
            seen.append(url)
            return pages[url]
        rows = indexer.discover_all(category="all", pages=1, fetch=fetch)
        self.assertEqual(len(rows), 4)
        self.assertEqual({x["metadata"]["competition"] for x in rows},
                         {"league", "cup", "champions-cup", "sixnations"})
        self.assertEqual(rows[2]["published"], "2026-10-16")
        self.assertEqual(rows[2]["metadata"]["embed_url"],
                         "https://ok.ru/videoembed/12345678902")
        self.assertEqual(seen[0], prem)
        self.assertEqual(seen[3], champions)
        self.assertEqual(seen[5], indexer.SIXNATIONS_URL)
        self.assertEqual(indexer.competition("Challenge Cup Full Match Replay"), "unknown")
        self.assertEqual(indexer.competition(CHAMP_TITLE), "champions-cup")
        self.assertEqual(indexer.competition(CUP_TITLE), "cup")

    def test_sixnations_only_and_matcher(self):
        pages = {
            indexer.SIXNATIONS_URL:
                '<h3><a href="/' + SIX_SLUG + '">' + SIX_TITLE + '</a></h3>'
                '<h3><a href="/women">France vs England Women’s Six Nations Full Match Replay 14 March 2026</a></h3>'
                '<h3><a href="/u20">France vs England U20 Six Nations Full Match Replay 14 March 2026</a></h3>',
            BASE + SIX_SLUG: '<iframe src="//ok.ru/videoembed/12345678903"></iframe>',
        }
        rows = indexer.discover_all(category="sixnations", pages=1, fetch=pages.__getitem__)
        self.assertEqual(len(rows), 1)
        release = rows[0]
        self.assertEqual(release["metadata"]["competition"], "sixnations")
        self.assertEqual(match_sixnations(release, [SIX_FIXTURE])["status"], "candidate")
        self.assertEqual(match_sixnations(release, [dict(SIX_FIXTURE, series_id="Rugby_Womens_6N")])["status"], "no_fixture")
        self.assertEqual(match_sixnations(release, [SIX_FIXTURE, dict(SIX_FIXTURE, _id="other")])["status"], "ambiguous")
        self.assertEqual(match_sixnations(dict(release, title=SIX_TITLE.replace("Six Nations", "U20 Six Nations")), [SIX_FIXTURE])["status"], "not_sixnations")
        self.assertEqual(match_sixnations(dict(release, title="France v England - Full Match Replay - Six Nations - 14 March 2026"), [SIX_FIXTURE])["status"], "candidate")

    def test_competition_selection_is_required(self):
        with self.assertRaises(TypeError):
            indexer.discover()
        with self.assertRaises(TypeError):
            indexer.discover_all()
        with self.assertRaises(SystemExit) as exc:
            indexer.main([])
        self.assertEqual(exc.exception.code, 2)
        with self.assertRaises(SystemExit) as exc:
            indexer.main(["--category", "all", "--url", indexer.PREM_URL])
        self.assertEqual(exc.exception.code, 2)

    def test_championship_only_never_fetches_premiership(self):
        url = indexer.CHAMPIONS_URL
        urls = []
        pages = {
            url: '<h3><a href="/' + CHAMP_SLUG + '">' + CHAMP_TITLE + '</a></h3>',
            BASE + CHAMP_SLUG: '<iframe src="//ok.ru/videoembed/12345678902"></iframe>',
        }
        def fetch(page):
            urls.append(page)
            return pages[page]
        releases = indexer.discover_all(category="championship", pages=1, fetch=fetch)
        self.assertEqual([x["metadata"]["competition"] for x in releases],
                         ["champions-cup"])
        self.assertEqual(urls, [url, BASE + CHAMP_SLUG])
        with self.assertRaisesRegex(ValueError, "Unsupported Rugby24 category"):
            indexer.discover_all(category="unknown", pages=1, fetch=fetch)

    def test_private_browser_cookie_file_is_copied_only_to_temp_jar(self):
        with tempfile.TemporaryDirectory() as root:
            browser = pathlib.Path(root) / "browser.txt"
            jar = pathlib.Path(root) / "temporary.txt"
            content = "# Netscape HTTP Cookie File\n.rugby24.net\tTRUE\t/\tTRUE\t0\tcf_clearance\ttest-token\n"
            browser.write_text(content)
            with unittest.mock.patch.dict(os.environ, {
                    "RUGBY24_COOKIE_JAR": str(browser)}):
                indexer.init_cookie_jar(jar)
            self.assertEqual(jar.read_text(), content)
            self.assertEqual(browser.read_text(), content)
            self.assertEqual(jar.stat().st_mode & 0o777, 0o600)
            with unittest.mock.patch.dict(os.environ, {
                    "RUGBY24_COOKIE_JAR": str(browser) + ".missing"}):
                with self.assertRaisesRegex(ValueError, "RUGBY24_COOKIE_JAR"):
                    indexer.init_cookie_jar(jar)

    def test_known_article_embed_is_reused_without_refetching(self):
        now = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)
        prem = indexer.PREM_URL
        article = BASE + LEAGUE_SLUG
        page = '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>'
        cache = {"rugby24:" + LEAGUE_SLUG: {
            "source": "rugby24", "id": LEAGUE_SLUG,
            "metadata": {"page_url": article,
                         "embed_url": "https://ok.ru/videoembed/12345678901",
                         "competition": "league"}}}
        calls = []
        def fetch(url):
            calls.append(url)
            if url == prem:
                return page
            raise AssertionError("known article should not be fetched")
        rows = indexer.discover(indexer.PREM_URL, pages=1, fetch=fetch, cached=cache, now=now)
        self.assertEqual(calls, [prem])
        self.assertEqual(rows[0]["metadata"]["embed_url"],
                         "https://ok.ru/videoembed/12345678901")
        self.assertEqual(rows[0]["metadata"]["embed_checked_at"],
                         now.isoformat())

    def test_recent_cached_embed_revalidates_weekly_not_hourly(self):
        article = BASE + LEAGUE_SLUG
        now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        cached = {"rugby24:" + LEAGUE_SLUG: {
            "source": "rugby24", "id": LEAGUE_SLUG,
            "metadata": {
                "page_url": article, "competition": "league",
                "embed_url": "https://ok.ru/videoembed/12345678901",
                "embed_checked_at": (now - dt.timedelta(days=8)).isoformat(),
            }}}
        fetched = []
        def fetch(url):
            fetched.append(url)
            if url == indexer.PREM_URL:
                return '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>'
            if url == article:
                return '<iframe src="//ok.ru/videoembed/12345678902"></iframe>'
            raise AssertionError(url)
        rows = indexer.discover(indexer.PREM_URL, pages=1, fetch=fetch, cached=cached, now=now)
        self.assertEqual(fetched, [indexer.PREM_URL, article])
        self.assertEqual(rows[0]["metadata"]["embed_url"],
                         "https://ok.ru/videoembed/12345678902")

    def test_invalid_cached_embed_never_skips_article_fetch(self):
        cached = {"rugby24:" + LEAGUE_SLUG: {
            "source": "rugby24", "id": LEAGUE_SLUG,
            "metadata": {"page_url": BASE + LEAGUE_SLUG,
                         "embed_url": "https://evil.example/videoembed/123",
                         "competition": "league"}}}
        pages = {
            indexer.PREM_URL: '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>',
            BASE + LEAGUE_SLUG: '<iframe src="//ok.ru/videoembed/12345678902"></iframe>',
        }
        fetched = []
        def fetch(url):
            fetched.append(url)
            return pages[url]
        rows = indexer.discover(indexer.PREM_URL, pages=1, fetch=fetch, cached=cached)
        self.assertEqual(fetched, [indexer.PREM_URL, BASE + LEAGUE_SLUG])
        self.assertEqual(rows[0]["metadata"]["embed_url"],
                         "https://ok.ru/videoembed/12345678902")

    def test_indexer_fails_on_empty_or_unplayable_pages(self):
        with self.assertRaisesRegex(ValueError, "no full-match"):
            indexer.discover(indexer.PREM_URL, pages=1, fetch=lambda url: "<html></html>")
        with self.assertRaisesRegex(ValueError, "OK.ru embeds"):
            indexer.discover(indexer.PREM_URL, pages=1, fetch=lambda url:
                '<h3><a href="/' + LEAGUE_SLUG + '">' + TITLE + '</a></h3>')
        self.assertIsNone(indexer.embed_url('<iframe src="https://evil.tld/videoembed/123456"></iframe>'))



    def test_cf_curl_is_preferred_for_rugby24_when_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            wrapper = root / "cf-curl"
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o755)
            cookie_jar = root / "unused-cookies.txt"
            seen = []
            def execute(command, **kwargs):
                seen.append(command)
                self.assertEqual(kwargs, {"capture_output": True,
                                          "text": True, "check": False})
                self.assertEqual(command[0], str(wrapper))
                self.assertEqual(command[command.index("--allowed-host") + 1],
                                 "rugby24.net")
                self.assertEqual(command[command.index("--max-filesize") + 1],
                                 str(indexer.MAX_HTML_BYTES))
                self.assertIn("--fail", command)
                self.assertEqual(command[-1], indexer.CHAMPIONS_URL)
                pathlib.Path(command[command.index("-o") + 1]).write_text(
                    "<html>champions cup page</html>")
                return subprocess.CompletedProcess(command, 0, "", "")
            with unittest.mock.patch.dict(os.environ, {
                    "RUGBY24_CF_CURL": str(wrapper)}):
                page = indexer.fetch_page(indexer.CHAMPIONS_URL,
                                          cookie_jar=cookie_jar, execute=execute)
            self.assertEqual(page, "<html>champions cup page</html>")
            self.assertEqual(len(seen), 1)


    def test_cf_curl_uses_debian_package_without_path_lookup(self):
        # CI need not have cfsolve installed; simulate only the existence of
        # the packaged wrapper, not its execution or a live HTTP request.
        packaged = pathlib.Path("/usr/bin/cf-curl")
        original_is_file = pathlib.Path.is_file
        original_access = os.access
        seen = []

        def is_file(path):
            return path == packaged or original_is_file(path)

        def access(path, mode):
            return True if pathlib.Path(path) == packaged else original_access(path, mode)

        def execute(command, **kwargs):
            seen.append((command, kwargs))
            self.assertEqual(command[0], str(packaged))
            self.assertEqual(kwargs["env"]["CFSOLVE_BIN"], "/usr/bin/cfsolve")
            self.assertEqual(kwargs["env"]["CFSOLVE_PROFILE"],
                             "/usr/share/cfsolve/profiles/safari-26-ios.json")
            pathlib.Path(command[command.index("-o") + 1]).write_text(
                "<html>Debian packaged solver</html>")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            with unittest.mock.patch.dict(os.environ, {}, clear=True), \
                    unittest.mock.patch.object(pathlib.Path, "is_file", autospec=True, side_effect=is_file), \
                    unittest.mock.patch.object(os, "access", side_effect=access), \
                    unittest.mock.patch.object(indexer.shutil, "which", side_effect=AssertionError("PATH lookup should not occur")):
                result = indexer.fetch_page(indexer.PREM_URL,
                    cookie_jar=pathlib.Path(temporary) / "unused.txt",
                    execute=execute)
        self.assertEqual(result, "<html>Debian packaged solver</html>")
        self.assertEqual(len(seen), 1)

    def test_missing_debian_package_does_not_use_tools_checkout(self):
        packaged = pathlib.Path("/usr/bin/cf-curl")
        original_is_file = pathlib.Path.is_file

        def is_file(path):
            return False if path == packaged else original_is_file(path)

        with tempfile.TemporaryDirectory() as temporary:
            def execute(command, **kwargs):
                self.assertEqual(command[0], "curl")
                pathlib.Path(command[command.index("--output") + 1]).write_text(
                    "<html>legacy fallback</html>")
                return subprocess.CompletedProcess(command, 0, "200|" + command[-1], "")

            with unittest.mock.patch.dict(os.environ, {}, clear=True), \
                    unittest.mock.patch.object(pathlib.Path, "is_file", autospec=True, side_effect=is_file), \
                    unittest.mock.patch.object(indexer.shutil, "which", side_effect=AssertionError("PATH lookup should not occur")):
                result = indexer.fetch_page(indexer.PREM_URL,
                    cookie_jar=pathlib.Path(temporary) / "unused.txt",
                    execute=execute)
        self.assertEqual(result, "<html>legacy fallback</html>")

    def test_cf_curl_fails_closed_on_transport_error_and_oversized_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            wrapper = root / "cf-curl"
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o755)
            def failed(command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 1, "", "cf-curl: Cloudflare challenge detected; cfsolve failed")
            with unittest.mock.patch.dict(os.environ, {"RUGBY24_CF_CURL": str(wrapper)}):
                with self.assertRaisesRegex(RuntimeError, "Cloudflare challenge"):
                    indexer.fetch_page(indexer.PREM_URL,
                                      cookie_jar=root / "unused.txt", execute=failed)
                def oversized(command, **kwargs):
                    pathlib.Path(command[command.index("-o") + 1]).write_bytes(
                        b"x" * (indexer.MAX_HTML_BYTES + 1))
                    return subprocess.CompletedProcess(command, 0, "", "")
                with self.assertRaisesRegex(ValueError, "page too large"):
                    indexer.fetch_page(indexer.PREM_URL,
                                      cookie_jar=root / "unused.txt", execute=oversized)

    def test_cf_curl_missing_explicit_path_and_legacy_opt_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            jar = root / "legacy.txt"
            with unittest.mock.patch.dict(os.environ, {
                    "RUGBY24_CF_CURL": str(root / "missing")}):
                with self.assertRaisesRegex(RuntimeError, "is missing"):
                    indexer.fetch_page(indexer.PREM_URL, cookie_jar=jar)
            def old_curl(command, **kwargs):
                self.assertEqual(command[0], "curl")
                pathlib.Path(command[command.index("--output") + 1]).write_text(
                    "<html>legacy</html>")
                return subprocess.CompletedProcess(command, 0,
                                                   "200|" + command[-1], "")
            with unittest.mock.patch.dict(os.environ, {"RUGBY24_CF_CURL": "off"}):
                page = indexer.fetch_page(indexer.PREM_URL,
                                          cookie_jar=jar, execute=old_curl)
            self.assertEqual(page, "<html>legacy</html>")

    def test_curl_transport_cookie_jar_and_http_403_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            cookie_jar = pathlib.Path(temporary) / "cookies.txt"
            seen = []

            def execute(command, **kwargs):
                seen.append((command, kwargs))
                self.assertEqual(kwargs, {"capture_output": True,
                                          "text": True, "check": False})
                self.assertIn("--compressed", command)
                self.assertEqual(command[command.index("--cookie") + 1],
                                 str(cookie_jar))
                self.assertEqual(command[command.index("--cookie-jar") + 1],
                                 str(cookie_jar))
                self.assertIn("Chrome/143", command[command.index("--user-agent") + 1])
                pathlib.Path(command[command.index("--output") + 1]).write_text(
                    "<html>article</html>"
                )
                pathlib.Path(command[command.index("--dump-header") + 1]).write_text(
                    chr(10).join(["HTTP/2 403", "server: cloudflare",
                                  "cf-mitigated: challenge"])
                )
                return subprocess.CompletedProcess(
                    command, 0, "403|" + command[-1], ""
                )

            with self.assertRaisesRegex(ValueError, "non-Rugby24"):
                indexer.fetch_html("https://evil.example/", cookie_jar=cookie_jar,
                                   execute=execute)
            with self.assertRaisesRegex(RuntimeError,
                "Cloudflare challenge \\(HTTP 403\\) at /premiership-rugby"):
                indexer.fetch_html(indexer.PREM_URL, cookie_jar=cookie_jar,
                                   execute=execute)
            self.assertEqual(len(seen), 1)

            def ok(command, **kwargs):
                pathlib.Path(command[command.index("--output") + 1]).write_text(
                    "<html>article</html>"
                )
                return subprocess.CompletedProcess(
                    command, 0, "200|" + command[-1], ""
                )
            self.assertIn(
                "<html>", indexer.fetch_html(indexer.PREM_URL,
                                              cookie_jar=cookie_jar, execute=ok)
            )

    def test_curl_redirect_to_foreign_host_and_curl_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            cookie_jar = pathlib.Path(temporary) / "cookies.txt"

            def redirect(command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 0, "200|https://other.example/", ""
                )
            with self.assertRaisesRegex(RuntimeError, "redirected"):
                indexer.fetch_html(indexer.PREM_URL, cookie_jar=cookie_jar,
                                   execute=redirect)

            def failed(command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 60, "", "SSL certificate problem"
                )
            with self.assertRaisesRegex(RuntimeError, "SSL certificate"):
                indexer.fetch_html(indexer.PREM_URL, cookie_jar=cookie_jar,
                                   execute=failed)


    def test_http_location_from_https_follows_same_host_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            cookie_jar = pathlib.Path(temporary) / "cookies.txt"
            calls = []
            def execute(command, **kwargs):
                current = command[-1]
                calls.append(current)
                self.assertEqual(command[command.index("--proto") + 1],
                                 "=" + current.split(":", 1)[0])
                self.assertNotIn("--location", command)
                self.assertEqual(command[command.index("--cookie") + 1],
                                 str(cookie_jar))
                header_path = pathlib.Path(command[command.index("--dump-header") + 1])
                body_path = pathlib.Path(command[command.index("--output") + 1])
                if len(calls) == 1:
                    header_path.write_text(
                        "HTTP/2 301\r\nLocation: http://rugby24.net/premiership-rugby/\r\n")
                    return subprocess.CompletedProcess(command, 0, "301|" + current, "")
                header_path.write_text("HTTP/2 200\r\n")
                body_path.write_text("<html>rugby replays</html>")
                return subprocess.CompletedProcess(command, 0, "200|" + current, "")
            body = indexer.fetch_html(indexer.PREM_URL,
                                      cookie_jar=cookie_jar, execute=execute)
            self.assertEqual(body, "<html>rugby replays</html>")
            self.assertEqual(calls, [
                indexer.PREM_URL,
                "http://rugby24.net/premiership-rugby/",
            ])

    def test_redirect_rejects_other_hosts_and_loops(self):
        with tempfile.TemporaryDirectory() as temporary:
            jar = pathlib.Path(temporary) / "cookies.txt"
            for target in (
                "http://evil.example/path",
                "https://evil.example/path",
                "http://rugby24.net:8080/premiership-rugby",
                "http://user:secret@rugby24.net/premiership-rugby",
                "file:///etc/passwd",
            ):
                calls = []
                def execute(command, **kwargs):
                    calls.append(command[-1])
                    pathlib.Path(command[command.index("--dump-header") + 1]).write_text(
                        "HTTP/2 301\r\nLocation: " + target + "\r\n")
                    return subprocess.CompletedProcess(
                        command, 0, "301|" + command[-1], "")
                with self.subTest(target=target):
                    with self.assertRaisesRegex(RuntimeError, "different host"):
                        indexer.fetch_html(indexer.PREM_URL,
                                           cookie_jar=jar, execute=execute)
                    self.assertEqual(calls, [indexer.PREM_URL])

            # A genuinely cyclic HTTPS -> HTTP -> HTTPS redirect still fails,
            # but a single HTTPS -> HTTP hop is permitted.
            calls = []
            def cycle(command, **kwargs):
                current = command[-1]
                calls.append(current)
                target = ("http://rugby24.net/premiership-rugby"
                          if current.startswith("https:") else indexer.PREM_URL)
                pathlib.Path(command[command.index("--dump-header") + 1]).write_text(
                    "HTTP/1.1 302\r\nLocation: " + target + "\r\n")
                return subprocess.CompletedProcess(command, 0,
                                                   "302|" + current, "")
            with self.assertRaisesRegex(RuntimeError, "redirect loop"):
                indexer.fetch_html(indexer.PREM_URL,
                                   cookie_jar=jar, execute=cycle)
            self.assertEqual(calls, [indexer.PREM_URL,
                                     "http://rugby24.net/premiership-rugby"])

    def test_championship_matching_strict_teams_date_and_competition(self):
        release = {"source": "rugby24", "title": CHAMP_TITLE,
                   "published": "2026-10-16",
                   "metadata": {"competition": "champions-cup"}}
        self.assertEqual(match_championship(release, [CHAMP_FIXTURE])["episode_id"],
                         CHAMP_FIXTURE["_id"])
        self.assertEqual(match_championship(
            dict(release, published="2026-11-06"), [CHAMP_FIXTURE])["status"],
            "no_fixture")
        self.assertEqual(match_championship(
            dict(release, metadata={"competition": "league"}),
            [CHAMP_FIXTURE])["status"], "not_championship")
        self.assertEqual(match_championship(
            dict(release, title=CUP_TITLE), [CHAMP_FIXTURE])["status"],
            "not_championship")
        self.assertEqual(match_release(release, [FIXTURE])["status"], "not_league")
        with_extra = dict(CHAMP_FIXTURE, _id="Rugby_Mens_Championship:duplicate")
        self.assertEqual(match_championship(release, [CHAMP_FIXTURE, with_extra])["status"],
                         "ambiguous")
        self.assertEqual(match_championship(
            dict(release, title="Unknown v Bordeaux Begles - Full Match Replay - "
                                "16 October 2026 - European Champions Cup Rugby"),
            [CHAMP_FIXTURE])["status"], "not_championship_match")

    def test_championship_team_aliases_cover_official_first_round(self):
        from matchers.championship import team
        for official, site in (
            ("Gloucester Rugby", "Gloucester"),
            ("Bordeaux-Begles", "Bordeaux Begles"),
            ("Sale Sharks", "Sale Sharks"),
            ("Glasgow Warriors", "Glasgow Warriors"),
            ("Vodacom Bulls", "Bulls"),
            ("Stade Francais Paris", "Stade Francais"),
            ("Clermont Auvergne", "Clermont"),
            ("Leinster Rugby", "Leinster"),
            ("DHL Stormers", "Stormers"),
            ("Connacht Rugby", "Connacht"),
            ("La Rochelle", "La Rochelle"),
            ("10bet Lions", "Lions"),
            ("Munster Rugby", "Munster"),
            ("Bath Rugby", "Bath"),
            ("Northampton Saints", "Northampton Saints"),
            ("Cardiff Rugby", "Cardiff"),
        ):
            with self.subTest(official=official):
                self.assertEqual(team(official), team(site))
        self.assertEqual(match_championship(
            {"title": "Harlequins vs Sale Sharks 4 April 2026 Full Match Replay "
                      "European Champions Cup Rugby", "published": "2026-04-04",
             "metadata": {"competition": "champions-cup"}},
            [])["status"], "not_championship_match")

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
            "metadata": {"page_url": self.page, "embed_url": self.embed,
                         "competition": "league"}}
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

    def test_wrong_competition_cannot_download_even_if_manually_linked(self):
        from importlib.machinery import SourceFileLoader
        cli = SourceFileLoader("pvr_rugby24_dl_wrong_kind",
                               str(ROOT / "download_release")).load_module()
        db = MagicMock()
        db.files.find.return_value = []
        for fixture, kind in ((FIXTURE, "champions-cup"),
                              (CHAMP_FIXTURE, "cup"),
                              (CHAMP_FIXTURE, "league")):
            db.episodes.find_one.return_value = fixture
            uri = "rugby24:" + CHAMP_SLUG
            db.releases.find_one.return_value = {
                "_id": uri, "source": "rugby24",
                "episode_id": fixture["_id"],
                "metadata": {"page_url": BASE + CHAMP_SLUG,
                             "embed_url": self.embed, "competition": kind},
            }
            with self.subTest(fixture=fixture["_id"], kind=kind):
                with self.assertRaisesRegex(ValueError,
                                            "competition does not match"):
                    cli.plan(db, uri, self.target, hostname="slagg")

    def test_championship_download_plan_reuses_existing_embed_adapter(self):
        from importlib.machinery import SourceFileLoader
        cli = SourceFileLoader("pvr_rugby24_dl_champ", str(ROOT / "download_release")).load_module()
        uri = "rugby24:" + CHAMP_SLUG
        db = MagicMock()
        db.releases.find_one.return_value = {
            "_id": uri, "source": "rugby24", "episode_id": CHAMP_FIXTURE["_id"],
            "metadata": {"page_url": BASE + CHAMP_SLUG,
                         "embed_url": self.embed, "competition": "champions-cup"}}
        db.episodes.find_one.return_value = CHAMP_FIXTURE
        db.files.find.return_value = []
        plan = cli.plan(db, uri, pathlib.Path(self.temp.name) / "championship.mkv",
                        hostname="slagg")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["adapter"], "rugby24")
        self.assertEqual(plan["series_id"], "Rugby_Mens_Championship")
        self.assertEqual(plan["source_url"], self.embed)

    def test_reject_bad_uris(self):
        for uri in ("rugby24:../abc", "rugby24:http://ok.ru", "rugby24:a?b"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                rugby24.rugby24_id(uri)
        with self.assertRaises(ValueError):
            rugby24.download(self.uri, "https://bad.site/videoembed/123456", self.target)


if __name__ == "__main__":
    unittest.main()
