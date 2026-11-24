"""Official men's Six Nations scheduler regression tests (no live HTTP in CI)."""
import datetime as dt
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from icalendar import Calendar, Event
from schedulers.rugby import importer
from schedulers.rugby import mens_six_nations as official
from schedulers.rugby.filter import merge

UTC = dt.timezone.utc
ROUNDS = (
    (("Ireland", "England"), ("Scotland", "Italy"), ("France", "Wales")),
    (("Italy", "Ireland"), ("Scotland", "Wales"), ("England", "France")),
    (("Wales", "Ireland"), ("England", "Italy"), ("France", "Scotland")),
    (("Scotland", "Ireland"), ("Italy", "France"), ("Wales", "England")),
    (("Italy", "Wales"), ("England", "Scotland"), ("Ireland", "France")),
)
DATES = ("02-05", "02-13", "02-20", "03-05", "03-13")


def page(year=2027, *, missing=False, rsc=True, rsc_times=True, include_scores=False):
    markup = ["<html><body>"]
    rounds = []
    for round_no, games in enumerate(ROUNDS, 1):
        cards = []
        markup.append(f'<div id="roundNumber-{round_no}">')
        for n, (home, away) in enumerate(games):
            if missing and round_no == 5 and n == 2:
                continue
            day = dt.date.fromisoformat(f"{year}-{DATES[round_no - 1]}")
            day_text = day.strftime("%d %B %Y")
            slug = (f"{home.lower()}-v-{away.lower()}-"
                    f"{day:%d%m%Y}-2010")
            href = f"/en/m6n/fixtures/{year}00/{slug}/build-up"
            card = {
                "data-testid": "fixture-card",
                "children": (
                    ([{"format": "HH:mm", "date": f"{day}T20:10:00.000Z"}]
                     if rsc_times else []) + [
                    {"data-testid": "fixture-card__match-centre-link",
                     "href": href, "title": f"{home} v {away} - Six Nations Rugby - {day_text}"},
                    {"className": "fixturesResultsCard_stadium__eRJIL",
                     "children": "Aviva Stadium"},
                ]),
            }
            cards.append(card)
            markup.append(
                '<article data-testid="fixture-card">'
                '<div class="fixturesResultsCard_stadium__eRJIL">Aviva Stadium</div>'
                + ('<span class="score">32-18 WIN</span>' if include_scores else '')
                + f'<a title="{home} v {away} - Six Nations Rugby - {day_text}" '
                f'data-testid="fixture-card__match-centre-link" '
                f'href="{href}">Match centre</a></article>'
            )
        markup.append("</div>")
        rounds.append({"id": f"roundNumber-{round_no}", "children": cards})
    if rsc:
        frame = [1, "26:" + json.dumps(["$", "section", None, {"children": rounds}])]
        markup.append("<script>self.__next_f.push(" + json.dumps(frame) + ")</script>")
    markup.append("</body></html>")
    return "".join(markup)


def matches(cal):
    return list(cal.walk("VEVENT"))


class OfficialMensSixNationsTests(unittest.TestCase):
    def test_2027_complete_rounds_and_utc_kickoffs(self):
        source = official.convert_mens(page(), year=2027)
        rows = matches(source)
        self.assertEqual(len(rows), 15)
        self.assertEqual({str(e.get("X-SIX-NATIONS-ROUND"))
                          for e in rows}, {"1", "2", "3", "4", "5"})
        self.assertEqual(
            rows[0].decoded("DTSTART"),
            dt.datetime(2027, 2, 5, 20, 10, tzinfo=UTC))
        self.assertEqual(len({str(e.get("UID")) for e in rows}), 15)
        self.assertTrue(all(e.decoded("DTSTART").year == 2027 for e in rows))

    def test_plain_http_body_without_rendered_cards(self):
        html = page()
        # Non-browser clients receive fixture markup only in RSC scripts.
        html = __import__("re").sub(
            r'<article data-testid="fixture-card">.*?</article>',
            "", html, flags=__import__("re").S)
        self.assertEqual(len(matches(official.convert_mens(html, year=2027))), 15)

    def test_no_results_are_copied_into_episode_metadata(self):
        source = official.convert_mens(
            page(include_scores=True), year=2027)
        self.assertNotIn("32-18", source.to_ical().decode())
        self.assertNotIn("WIN", source.to_ical().decode())

    def test_2026_history_fallback_uses_venue_timezone(self):
        html = page(2026, rsc=False).replace("/build-up", "/report")
        # Paris' 21:10 is 20:10 UTC in February.
        html = html.replace("france-v-wales-05022026-2010",
                            "france-v-wales-05022026-2110")
        source = official.convert_mens(html, year=2026)
        france = next(e for e in matches(source)
                      if str(e.get("SUMMARY")) == "France vs Wales")
        self.assertEqual(france.decoded("DTSTART"),
                         dt.datetime(2026, 2, 5, 20, 10, tzinfo=UTC))

    def test_2026_rsc_only_completed_fixtures_have_no_score_dependency(self):
        html = page(2026, rsc_times=False, include_scores=True).replace("/build-up", "/report")
        html = __import__("re").sub(
            r'<article data-testid="fixture-card">.*?</article>',
            "", html, flags=__import__("re").S)
        source = official.convert_mens(html, year=2026)
        self.assertEqual(len(matches(source)), 15)
        self.assertNotIn("32-18", source.to_ical().decode())

    def test_completed_future_seasons_use_competition_specific_clocks(self):
        for year in (2027, 2028, 2035):
            with self.subTest(year=year):
                men = page(year, rsc_times=False).replace("/build-up", "/report")
                men = men.replace(f"france-v-wales-0502{year}-2010",
                                  f"france-v-wales-0502{year}-2110")
                women = self.womens_page(year, rsc_times=False).replace("/build-up", "/report")
                for convert, html, title in (
                    (official.convert_mens, men, "France vs Wales"),
                    (official.convert_womens, women, "France Women vs Wales Women"),
                ):
                    rows = matches(convert(html, year=year))
                    self.assertEqual(len(rows), 15)
                    event = next(e for e in rows if str(e["SUMMARY"]) == title)
                    self.assertEqual(event.decoded("DTSTART"),
                                     dt.datetime(year, 2, 5, 20, 10, tzinfo=UTC))

    def test_mixed_completed_and_upcoming_cards(self):
        # Completed report card no longer has a time; other cards still do.
        html = page(2027)
        old = 'ireland-v-england-05022027-2010/build-up'
        html = html.replace(old, old.replace("build-up", "report"))
        marker = json.dumps({"format": "HH:mm", "date": "2027-02-05T20:10:00.000Z"}) + ", "
        escaped = json.dumps(marker)[1:-1]
        self.assertIn(escaped, html)
        html = html.replace(escaped, "", 1)
        rows = matches(official.convert_mens(html, year=2027))
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[0].decoded("DTSTART"), dt.datetime(2027, 2, 5, 20, 10, tzinfo=UTC))

    def test_no_guessing_incomplete_rounds_or_future_kickoffs(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            official.convert_mens(page(missing=True), year=2027)
        with self.assertRaisesRegex(ValueError, "Missing official UTC"):
            official.convert_mens(page(rsc=False), year=2027)
        with self.assertRaisesRegex(ValueError, "before 2026"):
            official.convert_mens(page(2025), year=2025)

    def test_existing_legacy_uids_are_retained(self):
        previous = Calendar()
        previous.add("version", "2.0")
        old = Event()
        old.add("uid", "rugbyfixture-original-id")
        old.add("dtstart", dt.datetime(2027, 2, 5, 19, tzinfo=UTC))
        old.add("summary", "Ireland 🇮🇪 vs England 🏉")
        previous.add_component(old)
        source = official.convert_mens(page(), year=2027)
        official.preserve_uids(source, previous)
        ireland = next(e for e in matches(source)
                       if str(e.get("SUMMARY")) == "Ireland vs England")
        self.assertEqual(str(ireland.get("UID")), "rugbyfixture-original-id")
        frozen = merge(source, previous, dt.datetime(2027, 2, 6, tzinfo=UTC),
                       UTC, "Rugby Mens 6N", allow_new_past=True)
        preserved = next(e for e in matches(frozen)
                         if str(e.get("UID")) == "rugbyfixture-original-id")
        self.assertEqual(preserved.decoded("DTSTART"), old.decoded("DTSTART"))

    def test_fetches_only_2026_and_later_and_handles_unpublished_next(self):
        requested = []

        def get(request, timeout=None):
            url = request.full_url
            requested.append(url)
            if url.endswith("202700"):
                raise urllib.error.HTTPError(url, 404, "Not published", {}, None)
            return io.BytesIO(page(2026).encode("utf-8"))

        with patch.object(official.urllib.request, "urlopen", side_effect=get):
            source = official.fetch_mens(now=dt.datetime(2026, 11, 24, tzinfo=UTC))
        self.assertEqual(len(matches(source)), 15)
        self.assertEqual(requested, [
            official.BASE + "202600", official.BASE + "202700"])

    def womens_page(self, year=2027, *, titles=True, rsc_times=True):
        import re
        html = page(year, rsc_times=rsc_times)
        html = html.replace("/en/m6n/", "/en/w6n/")
        html = re.sub(r"([a-z]+)-v-([a-z]+)-(\d{8})",
                      r"\1-women-v-\2-women-\3", html)
        html = html.replace(" - Six Nations Rugby - ", " - Women's Six Nations Rugby - ")
        html = html.replace("Aviva Stadium", "TBD")
        if not titles:
            html = re.sub(r"[A-Z][a-z]+ v [A-Z][a-z]+ - Women's Six Nations Rugby - \d{2} [A-Za-z]+ \d{4}",
                          "$undefined", html)
        return html

    def test_womens_2027_source_and_provisional_venues(self):
        rows = matches(official.convert_womens(self.womens_page(), year=2027))
        self.assertEqual(len(rows), 15)
        ireland = next(e for e in rows if str(e["SUMMARY"]) == "Ireland Women vs England Women")
        self.assertTrue(all("sixnations-w6n-" in str(e["UID"]) for e in rows))
        self.assertTrue(all("LOCATION" not in e for e in rows))
        self.assertEqual(ireland.decoded("DTSTART"),
                         dt.datetime(2027, 2, 5, 20, 10, tzinfo=UTC))
        with self.assertRaises(ValueError):
            official.convert_mens(self.womens_page(), year=2027)

    def test_womens_missing_future_titles(self):
        html = self.womens_page(titles=False)
        self.assertIn("$undefined", html)
        self.assertEqual(len(matches(official.convert_womens(html, year=2027))), 15)

    def test_womens_historical_url_clock_is_utc(self):
        source = official.convert_womens(
            self.womens_page(2026, rsc_times=False).replace("/build-up", "/report"), year=2026)
        france = next(e for e in matches(source)
                      if str(e["SUMMARY"]) == "France Women vs Wales Women")
        self.assertEqual(france.decoded("DTSTART"),
                         dt.datetime(2026, 2, 5, 20, 10, tzinfo=UTC))

    def test_womens_fetch_and_uid_reconciliation(self):
        now = dt.datetime(2026, 11, 24, tzinfo=UTC)
        with patch.object(official.urllib.request, "urlopen", side_effect=[
                io.BytesIO(self.womens_page(2026, rsc_times=False).replace("/build-up", "/report").encode()),
                io.BytesIO(self.womens_page().encode())]) as get:
            source = official.fetch_womens(now=now)
        self.assertEqual(len(matches(source)), 30)
        self.assertEqual([c.args[0].full_url for c in get.call_args_list],
                         [official.WOMENS_BASE + "202600", official.WOMENS_BASE + "202700"])
        old = Calendar()
        event = Event()
        event.add("uid", "previous-womens-uid")
        event.add("summary", "Ireland Women 🇮🇪 vs England Women")
        event.add("dtstart", dt.datetime(2027, 2, 5, 20, 10, tzinfo=UTC))
        old.add_component(event)
        official.preserve_uids(source, old)
        self.assertIn("previous-womens-uid", [str(e["UID"]) for e in matches(source)])
        with patch.object(importer, "fetch_womens", return_value="official") as call:
            self.assertEqual(importer.fetch_calendar(
                {"type": "six-nations-womens"}, now), "official")
            call.assert_called_once_with(now=now)

    def test_importer_uses_official_adapter_for_men(self):
        now = dt.datetime(2026, 11, 24, tzinfo=UTC)
        with patch.object(importer, "fetch_mens", return_value="official") as call:
            self.assertEqual(importer.fetch_calendar(
                {"type": "six-nations-mens"}, now), "official")
            call.assert_called_once_with(now=now)


if __name__ == "__main__":
    unittest.main()
