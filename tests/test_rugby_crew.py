"""Offline tests for officials/commentary enrichment; never contact live sites."""
import datetime as dt
import io
import unittest
from contextlib import redirect_stdout

from schedulers.rugby import crew

UTC = dt.timezone.utc


class Collection:
    def __init__(self, records):
        self.records = records
        self.writes = []

    def find(self, query):
        def allowed(ep):
            start, end = query["scheduled"]["$gte"], query["scheduled"]["$lt"]
            return (start <= ep["scheduled"].replace(tzinfo=UTC) < end
                    and ep.get("status") not in query["status"]["$nin"]
                    and ("series_id" not in query or
                         ep["series_id"] in query["series_id"]["$in"]))
        return [ep.copy() for ep in self.records if allowed(ep)]

    def update_one(self, query, changes):
        self.writes.append((query, changes))
        row = next((ep for ep in self.records if ep["_id"] == query["_id"]), None)
        if row is not None:
            row.update(changes["$set"])
        return type("Result", (), {"matched_count": int(row is not None)})()


class Database:
    def __init__(self, records):
        self.episodes = Collection(records)


class CrewTests(unittest.TestCase):
    def test_world_rugby_labelled_table(self):
        fragment = """<div>Match Officials</div>
        <div class="mc-info-table__list-title">Referee</div>
        <div class="mc-info-table__sub-list-item">Jess Ling</div>
        <div class="mc-info-table__list-title">Assistant Referee 1</div>
        <div class="mc-info-table__sub-list-item">Precious Pazani</div>
        <div class="mc-info-table__list-title">TV Match Official</div>
        <div class="mc-info-table__sub-list-item">Matteo Liperini</div>"""
        self.assertEqual(crew.parse_world_officials(fragment), {
            "referee": "Jess Ling", "assistant_referee_1": "Precious Pazani",
            "tmo": "Matteo Liperini"})
        self.assertEqual(crew.parse_world_officials("<div>Referee X</div>"), {})

    def test_pwr_officials(self):
        match = {"officials": [{"name": "Nikki O'Donnell", "role": "referee"},
                               {"name": "Jenny Burrow", "role": "touch judge/assistant 1"},
                               {"name": "Other", "role": "unrecognised"}]}
        self.assertEqual(crew.parse_pwr_officials(match), {
            "referee": "Nikki O'Donnell", "assistant_referee_1": "Jenny Burrow"})

    def test_icdb_match_and_sitemap(self):
        fixture = """<title>Loughborough v Ealing Trailfinders Commentators -
        English Women's Premiership - 20/11/2026 - ICDb</title>
        <div id="fixture-correction-container" data-fixture-ts="1795203000"></div>
        <div class="broadcast-card"><div class="broadcast-title">
        <h3>BBC iPlayer <img src="flag.png"></h3></div>
        <div class="role-row"><div class="role-col-role">Main Commentator</div>
        <div class="role-col-name"><a href="/person/1">Nick Heath</a></div>
        <div class="role-col-lang">English</div></div></div>
        <a href="https://rugby.icdb.tv/add?fixsubmit=17950">Submit</a>"""
        parsed = crew.parse_icdb_match(fixture,
                     "https://rugby.icdb.tv/match/17950-Loughborough-v-Ealing-Trailfinders")
        self.assertEqual(parsed["broadcasts"][0]["people"][0]["name"], "Nick Heath")
        self.assertEqual(parsed["broadcasts"][0]["channel"], "BBC iPlayer")
        self.assertTrue(crew.same_game({
            "series_id": "Rugby_Womens_PWR", "home": "Loughborough Lightning",
            "away": "Trailfinders Women",
            "scheduled": dt.datetime(2026, 11, 20, 19, 30, tzinfo=UTC),
        }, parsed))
        self.assertEqual(crew.normalize_team("GloucesterHartpury"),
                         crew.normalize_team("Gloucester Hartpury"))
        self.assertFalse(crew.same_game({
            "series_id": "Rugby_Mens_Prem", "home": "Loughborough Lightning",
            "away": "Trailfinders Women",
            "scheduled": dt.datetime(2026, 11, 20, 19, 30, tzinfo=UTC),
        }, parsed))
        self.assertEqual(crew.parse_icdb_sitemap(
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b'<url><loc>https://rugby.icdb.tv/match/17950-Loughborough-v-Ealing-Trailfinders</loc>'
            b'<lastmod>2026-11-20</lastmod></url></urlset>')[0][1],
                         dt.date(2026, 11, 20))

    def test_dry_run_apply_and_idempotency(self):
        ep = {"_id": "Rugby_Womens_PWR:pwr-295034@mbligh.github.io",
              "series_id": "Rugby_Womens_PWR", "season": 2026,
              "home": "Loughborough Lightning", "away": "Trailfinders Women",
              "scheduled": dt.datetime(2026, 11, 20, 19, 30)}
        db = Database([ep])
        data = json_bytes({"data": [{"id": 295034, "officials": [
            {"role": "referee", "name": "Nikki O'Donnell"}]}]})
        fetch = lambda url: data
        with redirect_stdout(io.StringIO()):
            crew.run(db, start=dt.date(2026, 11, 20), end=dt.date(2026, 11, 20),
                     sources=("pwr",), fetch=fetch, delay=0)
        self.assertEqual(db.episodes.writes, [])
        with redirect_stdout(io.StringIO()):
            crew.run(db, start=dt.date(2026, 11, 20), end=dt.date(2026, 11, 20),
                     sources=("pwr",), fetch=fetch, apply=True, delay=0)
        self.assertEqual(len(db.episodes.writes), 1)
        self.assertEqual(ep["match_officials"]["referee"], "Nikki O'Donnell")
        with redirect_stdout(io.StringIO()):
            crew.run(db, start=dt.date(2026, 11, 20), end=dt.date(2026, 11, 20),
                     sources=("pwr",), fetch=fetch, apply=True, delay=0)
        self.assertEqual(len(db.episodes.writes), 1)

    def test_do_not_create_for_superseded_record(self):
        db = Database([{"_id": "Rugby_Womens_PWR:pwr-1@mbligh.github.io",
                        "series_id": "Rugby_Womens_PWR", "season": 2026,
                        "status": "superseded",
                        "scheduled": dt.datetime(2026, 11, 20, 19, 30)}])
        with redirect_stdout(io.StringIO()):
            crew.run(db, start=dt.date(2026, 11, 20), end=dt.date(2026, 11, 20),
                     sources=("pwr",), fetch=lambda _: json_bytes({"data": []}), delay=0)
        self.assertEqual(db.episodes.writes, [])


def json_bytes(value):
    import json
    return json.dumps(value).encode()


if __name__ == "__main__":
    unittest.main()
