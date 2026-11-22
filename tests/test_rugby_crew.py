"""Offline tests for officials/commentary enrichment; never contact live sites."""
import datetime as dt
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError

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


class State:
    def __init__(self):
        self.record = None
        self.writes = []

    def find_one(self, filter):
        return self.record

    def update_one(self, filter, update, upsert=False):
        self.writes.append((filter, update))
        if self.record is None:
            self.record = {"_id": filter["_id"]}
        for key, value in update.get("$set", {}).items():
            self.record[key] = value
        for key, value in update.get("$max", {}).items():
            if key not in self.record or self.record[key] < value:
                self.record[key] = value


class Database:
    def __init__(self, records):
        self.episodes = Collection(records)
        self.metadata_state = State()


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

    def test_retry_after_parsing(self):
        now = dt.datetime(2026, 11, 22, 21, 0, tzinfo=UTC)
        self.assertEqual(crew.retry_after_seconds("7200", now), 7200)
        self.assertEqual(crew.retry_after_seconds(
            "Sun, 22 Nov 2026 23:00:00 GMT", now), 7200)
        self.assertEqual(crew.retry_after_seconds("not a time", now), 0)
        self.assertEqual(crew.retry_after_seconds(None, now), 0)

    def test_world_rugby_throttle_stops_and_carries_over_to_next_run(self):
        now = dt.datetime.now(UTC)
        ep1 = self.world_episode("2120baa6-8f19-40bc-98a3-db97e3ef9b45", now)
        ep2 = self.world_episode("3deba7fe-2095-4691-9555-fb22da23306b", now)
        pwr = {"_id": "Rugby_Womens_PWR:pwr-295034@mbligh.github.io",
               "series_id": "Rugby_Womens_PWR", "season": 2026,
               "scheduled": now, "title": "A vs B"}
        db = Database([ep1, ep2, pwr])
        seen = []

        def fetch(url):
            seen.append(url)
            if "world.rugby" in url:
                raise HTTPError(url, 429, "Too Many Requests",
                                {"Retry-After": "7200"}, None)
            return json_bytes({"data": [{"id": 295034, "officials": [
                {"role": "referee", "name": "Nikki O'Donnell"}]}]})

        day = now.date()
        with redirect_stdout(io.StringIO()):
            stats = crew.run(db, start=day, end=day,
                             sources=("world-rugby", "pwr"), apply=True,
                             fetch=fetch, delay=0, world_delay=0)
        self.assertEqual(stats["world_rugby_requests"], 1)
        self.assertEqual(stats["world_rugby_cooldown"], 1)
        self.assertEqual(len([u for u in seen if "world.rugby" in u]), 1)
        self.assertEqual(pwr["match_officials"]["referee"], "Nikki O'Donnell")
        self.assertGreaterEqual(
            (db.metadata_state.record["retry_not_before"] - now).total_seconds(),
            7100)
        with redirect_stdout(io.StringIO()):
            stats = crew.run(db, start=day, end=day, sources=("world-rugby",),
                             apply=True, fetch=fetch, delay=0, world_delay=0)
        self.assertEqual(stats["world_rugby_cooldown"], 1)
        self.assertEqual(len([u for u in seen if "world.rugby" in u]), 1)

    def test_world_rugby_preview_does_not_persist_throttle(self):
        now = dt.datetime.now(UTC)
        db = Database([self.world_episode(
            "2120baa6-8f19-40bc-98a3-db97e3ef9b45", now)])
        seen = []

        def fetch(url):
            seen.append(url)
            raise HTTPError(url, 429, "Too Many Requests", {}, None)

        with redirect_stdout(io.StringIO()):
            crew.run(db, start=now.date(), end=now.date(),
                     sources=("world-rugby",), apply=False,
                     fetch=fetch, delay=0, world_delay=0)
        self.assertEqual(len(seen), 1)
        self.assertIsNone(db.metadata_state.record)
        self.assertFalse(db.episodes.writes)

    def test_world_rugby_pacing_budget_and_skip_existing_or_empty(self):
        now = dt.datetime.now(UTC)
        ids = ("2120baa6-8f19-40bc-98a3-db97e3ef9b45",
               "3deba7fe-2095-4691-9555-fb22da23306b",
               "1e74b48e-25fe-4ebe-beea-abb6cea010a0")
        midday = now.replace(hour=12, minute=0, second=0, microsecond=0)
        episodes = [self.world_episode(uid, midday + dt.timedelta(seconds=i))
                    for i, uid in enumerate(ids)]
        db = Database(episodes)
        pages = []

        def fetch(url):
            pages.append(url)
            if url.endswith(ids[1]):
                return b"<html>Nothing published yet</html>"
            return (b"<div>Match Officials</div>"
                    b'<div class="mc-info-table__list-title">Referee</div>'
                    b'<div class="mc-info-table__sub-list-item">Jess Ling</div>')

        with patch.object(crew.time, "monotonic", return_value=0), \
             patch.object(crew.time, "sleep") as sleeping:
            with redirect_stdout(io.StringIO()):
                counters = crew.run(db, start=now.date(), end=now.date(),
                                    sources=("world-rugby",), apply=True,
                                    fetch=fetch, delay=0, max_world=2,
                                    world_delay=10)
        self.assertEqual(len(pages), 2)
        sleeping.assert_called_once_with(10)
        self.assertEqual(counters["world_rugby_limit_reached"], 1)
        self.assertEqual(episodes[0]["match_officials"]["referee"], "Jess Ling")
        self.assertIn("crew_world_rugby_checked_at", episodes[1])
        with redirect_stdout(io.StringIO()):
            counters = crew.run(db, start=now.date(), end=now.date(),
                                sources=("world-rugby",), apply=True,
                                fetch=fetch, delay=0, max_world=2,
                                world_delay=0)
        self.assertEqual(len(pages), 3)
        self.assertEqual(counters["world_rugby_existing_skipped"], 1)
        self.assertEqual(counters["world_rugby_recently_checked"], 1)
        self.assertEqual(episodes[2]["match_officials"]["referee"], "Jess Ling")

    @staticmethod
    def world_episode(uid, when):
        return {"_id": f"Rugby_Womens_WXV:world-rugby-{uid}@mbligh.github.io",
                "series_id": "Rugby_Womens_WXV", "season": 2026,
                "scheduled": when, "title": "South Africa vs Ireland"}

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
