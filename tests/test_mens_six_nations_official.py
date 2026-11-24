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


def page(year=2027, *, missing=False, rsc=True, include_scores=False):
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
                "children": [
                    {"format": "HH:mm", "date": f"{day}T20:10:00.000Z"},
                    {"data-testid": "fixture-card__match-centre-link",
                     "href": href, "title": f"{home} v {away} - Six Nations Rugby - {day_text}"},
                    {"className": "fixturesResultsCard_stadium__eRJIL",
                     "children": "Aviva Stadium"},
                ],
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
        html = page(2026, rsc=False)
        # Paris' 21:10 is 20:10 UTC in February.
        html = html.replace("france-v-wales-05022026-2010",
                            "france-v-wales-05022026-2110")
        source = official.convert_mens(html, year=2026)
        france = next(e for e in matches(source)
                      if str(e.get("SUMMARY")) == "France vs Wales")
        self.assertEqual(france.decoded("DTSTART"),
                         dt.datetime(2026, 2, 5, 20, 10, tzinfo=UTC))

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

    def test_importer_uses_official_adapter_for_men_only(self):
        now = dt.datetime(2026, 11, 24, tzinfo=UTC)
        with patch.object(importer, "fetch_mens", return_value="official") as call:
            self.assertEqual(importer.fetch_calendar(
                {"type": "six-nations-mens"}, now), "official")
            call.assert_called_once_with(now=now)


if __name__ == "__main__":
    unittest.main()
