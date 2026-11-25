"""Schedule-aware source registry and Atlas polling backoff (offline tests)."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from orchestration import polling
from orchestration.source_registry import SOURCE_SERIES, sources_for, series_for

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, 14, tzinfo=UTC)


def episode(series, finished, number=101):
    if series == "Formula_1":
        season = 2026
        name = "Formula_1"
    else:
        season = 2026
        name = series
    e = {
        "_id": f"{series}:{number}", "series_id": series, "season": season,
        "episode": number,
        "scheduled": finished - dt.timedelta(hours=2),
        "nfo_path": f"{name}/Season_2026/{name}-s2026e{number:03d}-Test.nfo",
        "nfo": (f"<episodedetails><showtitle>{'Formula 1' if series == 'Formula_1' else series.replace('_', ' ')}</showtitle>"
                f"<season>2026</season><episode>{number}</episode></episodedetails>"),
    }
    if series == "Formula_1":
        e["scheduled_end"] = finished
    return e


class Database:
    def __init__(self, episodes=(), files=()):
        self.episode_rows = list(episodes)
        self.file_rows = list(files)
        self.states = {}
        self.episodes = MagicMock()
        self.files = MagicMock()
        self.indexer_poll_state = MagicMock()
        self.releases = MagicMock()
        self.releases.find.return_value = []
        self.episodes.find.side_effect = self._episodes
        self.files.find.side_effect = self._files
        self.indexer_poll_state.find_one.side_effect = lambda q: self.states.get(q["_id"])
        self.indexer_poll_state.update_one.side_effect = self._update

    def _episodes(self, query):
        series = set(query["series_id"]["$in"])
        return [ep for ep in self.episode_rows
                if ep["series_id"] in series
                and ("season" not in query or ep["season"] == query["season"])
                and ("round" not in query or ep.get("round") == query["round"])
                and ("episode" not in query or ep.get("episode") == query["episode"])]

    def _files(self, query):
        ids = set(query["episode_id"]["$in"])
        return [row for row in self.file_rows if row["episode_id"] in ids]

    def _update(self, query, update, **kwargs):
        state = self.states.setdefault(query["_id"], {})
        state.update(update["$set"])
        for key in update.get("$unset", {}):
            state.pop(key, None)


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sports = Path(self.tmp.name) / "sports"
        self.sports.mkdir()

    def check(self, db, sources=("bbc", "youtube", "rugbypass"), now=NOW):
        return polling.plan(db, sources=sources, now=now, sports_root=self.sports)

    def test_registry_routes_mens_prem_to_rugby24_only(self):
        self.assertEqual(sources_for("Rugby_Mens_Prem"), ("rugby24",))
        self.assertEqual(sources_for("Rugby_Mens_6N"), ("rugbypass", "rugby24"))
        self.assertEqual(sources_for("Rugby_Womens_6N"), ("rugbypass",))
        self.assertEqual(sources_for("Rugby_Womens_PWR"),
                         ("bbc", "youtube", "rugbypass"))
        self.assertEqual(sources_for("Rugby_Mens_Intl"), ("rugbypass",))
        self.assertEqual(sources_for("Formula_1"), ("reddit-teletz",))
        self.assertNotIn("Rugby_Mens_Prem", series_for("bbc"))
        self.assertEqual(sources_for("Rugby_Mens_Championship"), ("rugby24",))
        self.assertEqual(series_for("rugby24"), (
            "Rugby_Mens_Prem", "Rugby_Mens_Championship", "Rugby_Mens_6N"))

    def test_rugby24_polling_uses_prem_fixtures(self):
        prem = episode("Rugby_Mens_Prem", NOW - dt.timedelta(minutes=30))
        db = Database([prem])
        decisions, missing = self.check(db, sources=("rugby24",))
        self.assertEqual(len(missing), 1)
        self.assertEqual(decisions["rugby24"]["interval_minutes"], 5)
        self.assertTrue(decisions["rugby24"]["due"])

    def test_rugby24_category_states_are_independent(self):
        prem = episode("Rugby_Mens_Prem", NOW - dt.timedelta(hours=1))
        champ = episode("Rugby_Mens_Championship",
                        NOW - dt.timedelta(hours=1), 102)
        db = Database([prem, champ])
        keys = {"prem": "rugby24:prem",
                "championship": "rugby24:championship"}
        polling.record_success(db, keys["prem"], now=NOW)
        for category, series_id in (
                ("prem", "Rugby_Mens_Prem"),
                ("championship", "Rugby_Mens_Championship")):
            decisions, missing = polling.plan(
                db, sources=("rugby24",), now=NOW,
                sports_root=self.sports, series_ids=(series_id,),
                state_keys={"rugby24": keys[category]})
            self.assertEqual(len(missing), 1)
            self.assertEqual(decisions["rugby24"]["due"], category == "championship")
        self.assertEqual(set(db.states), {keys["prem"]})
        # Planning is read-only: Championship gets its own state document
        # on its first actual success or failure.

    def test_cloudflare_backoff_is_long_and_survives_force_checks(self):
        db = Database([episode("Rugby_Mens_Championship",
                               NOW - dt.timedelta(hours=1))])
        key = "rugby24:championship"
        first = polling.record_failure(db, key, now=NOW,
                                       reason="cloudflare_challenge")
        self.assertEqual(first, dt.timedelta(hours=12))
        decisions, _ = polling.plan(
            db, sources=("rugby24",), now=NOW,
            sports_root=self.sports,
            series_ids=("Rugby_Mens_Championship",),
            state_keys={"rugby24": key})
        self.assertFalse(decisions["rugby24"]["due"])
        self.assertTrue(decisions["rugby24"]["cooldown_active"])
        again = polling.record_failure(db, key, now=NOW + first,
                                       reason="cloudflare_challenge")
        self.assertEqual(again, dt.timedelta(hours=24))
        polling.record_success(db, key, now=NOW + dt.timedelta(hours=36))
        self.assertNotIn("failure_reason", db.states[key])
        self.assertNotIn("retry_not_before", db.states[key])

    def test_age_backoff_boundaries(self):
        table = [(0, 5), (119, 5), (120, 15), (719, 15),
                 (720, 60), (2879, 60), (2880, 360),
                 (7 * 1440, 1440), (30 * 1440, 10080)]
        for minutes, interval in table:
            with self.subTest(minutes=minutes):
                age = dt.timedelta(minutes=minutes)
                self.assertEqual(int(polling.age_interval(age).total_seconds() / 60),
                                 interval)

    def test_no_missing_checks_only_at_daily_fallback(self):
        db = Database()
        source = "bbc"
        db.states[source] = {"last_success": NOW - dt.timedelta(hours=2)}
        decisions, missing = self.check(db, sources=(source,))
        self.assertEqual(missing, [])
        self.assertFalse(decisions[source]["due"])
        self.assertEqual(decisions[source]["interval_minutes"], 1440)
        decisions, missing = self.check(db, sources=(source,), now=NOW + dt.timedelta(hours=23))
        self.assertTrue(decisions[source]["due"])

    def test_missing_old_event_never_triggers_five_minute_polling(self):
        old = episode("Rugby_Womens_PWR", NOW - dt.timedelta(days=14))
        db = Database([old])
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(hours=2)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(len(missing), 1)
        self.assertFalse(decisions["bbc"]["due"])
        self.assertEqual(decisions["bbc"]["interval_minutes"], 1440)
        self.assertEqual(decisions["bbc"]["next_at"],
                         NOW + dt.timedelta(hours=22))

    def test_fresh_event_triggers_five_minute_interval(self):
        db = Database([episode("Rugby_Womens_PWR", NOW - dt.timedelta(minutes=30))])
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(minutes=10)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(len(missing), 1)
        self.assertTrue(decisions["bbc"]["due"])
        self.assertEqual(decisions["bbc"]["interval_minutes"], 5)
        db.states["bbc"]["last_success"] = NOW - dt.timedelta(minutes=2)
        decisions, _ = self.check(db, sources=("bbc",))
        self.assertFalse(decisions["bbc"]["due"])
        self.assertEqual(decisions["bbc"]["next_at"], NOW + dt.timedelta(minutes=3))

    def test_new_fixture_overrides_old_cooldown(self):
        db = Database([episode("Rugby_Womens_PWR", NOW - dt.timedelta(minutes=1))])
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(minutes=2)}
        decisions, _ = self.check(db, sources=("bbc",))
        self.assertTrue(decisions["bbc"]["due"])

    def test_all_supported_series_combined_per_indexer(self):
        recent = episode("Rugby_Womens_PWR", NOW - dt.timedelta(minutes=30))
        old = episode("Rugby_Mens_Intl", NOW - dt.timedelta(days=3), 102)
        prem = episode("Rugby_Mens_Prem", NOW - dt.timedelta(minutes=5), 103)
        db = Database([recent, old, prem])
        for source in ("bbc", "youtube", "rugbypass"):
            db.states[source] = {"last_success": NOW - dt.timedelta(minutes=8)}
        decisions, missing = self.check(db)
        self.assertEqual(len(missing), 2)  # PREM ignored
        self.assertEqual([decisions[s]["missing"] for s in ("bbc", "youtube", "rugbypass")],
                         [1, 1, 2])
        self.assertTrue(all(x["due"] for x in decisions.values()))
        target, _ = polling.plan(db, sources=("rugbypass",), now=NOW,
                                 sports_root=self.sports)
        self.assertEqual(target["rugbypass"]["interval_minutes"], 5)
        # Once PWR is published, only the three-day-old international remains.
        dest = self.sports / recent["nfo_path"].replace(".nfo", ".mkv")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"video")
        db.states["rugbypass"]["last_success"] = NOW - dt.timedelta(minutes=30)
        decisions, missing = self.check(db, sources=("rugbypass",))
        self.assertEqual(len(missing), 1)
        self.assertFalse(decisions["rugbypass"]["due"])
        self.assertEqual(decisions["rugbypass"]["missing"], 1)
        self.assertEqual(decisions["rugbypass"]["improving"], 1)
        self.assertEqual(decisions["rugbypass"]["interval_minutes"], 60)
        # After the 24h upgrade window, the published PWR episode stops
        # adding any demand; only the three-day-old international remains.
        db.states["rugbypass"]["last_success"] = NOW + dt.timedelta(hours=24)
        decisions, missing = self.check(
            db, sources=("rugbypass",), now=NOW + dt.timedelta(hours=25))
        self.assertEqual(len(missing), 1)
        self.assertFalse(decisions["rugbypass"]["due"])
        self.assertEqual(decisions["rugbypass"]["improving"], 0)
        self.assertEqual(decisions["rugbypass"]["interval_minutes"], 360)

    def test_first_usable_release_moves_every_compatible_source_to_hourly(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(minutes=20))
        db = Database([ep])
        db.releases.find.return_value = [{
            "_id": "bbc:m0034hxp", "source": "bbc", "episode_id": ep["_id"],
            "available": True, "duration": 7200,
        }]
        for source in ("bbc", "youtube", "rugbypass"):
            db.states[source] = {"last_success": NOW - dt.timedelta(minutes=6)}
        decisions, missing = self.check(db)
        self.assertEqual(len(missing), 1)  # Acquisition is still needed.
        for source in ("bbc", "youtube", "rugbypass"):
            with self.subTest(source=source):
                self.assertEqual(decisions[source]["missing"], 0)
                self.assertEqual(decisions[source]["improving"], 1)
                self.assertEqual(decisions[source]["interval_minutes"], 60)
                self.assertFalse(decisions[source]["due"])
                self.assertEqual(decisions[source]["next_at"],
                                 NOW + dt.timedelta(minutes=54))
        # A single source filter must still recognize the BBC release.
        only_youtube, _ = self.check(db, sources=("youtube",))
        self.assertEqual(only_youtube["youtube"]["improving"], 1)
        self.assertFalse(only_youtube["youtube"]["due"])

    def test_phase_b_hourly_for_published_canonical_recording(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=3))
        dest = self.sports / ep["nfo_path"].replace(".nfo", ".mkv")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"low quality complete video")
        db = Database([ep])
        for source in ("bbc", "youtube", "rugbypass"):
            db.states[source] = {"last_success": NOW - dt.timedelta(minutes=59)}
        decisions, missing = self.check(db)
        self.assertEqual(missing, [])  # Published files remain non-upgradeable.
        for source in ("bbc", "youtube", "rugbypass"):
            self.assertEqual(decisions[source]["improving"], 1)
            self.assertEqual(decisions[source]["interval_minutes"], 60)
            self.assertFalse(decisions[source]["due"])
        decisions, _ = self.check(db, now=NOW + dt.timedelta(minutes=2))
        self.assertTrue(all(value["due"] for value in decisions.values()))

    def test_phase_b_hourly_for_registered_published_recording(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=4))
        path = self.sports / "archive.mkv"
        path.write_bytes(b"registered canonical library video")
        db = Database([ep], files=[{"episode_id": ep["_id"],
                                   "path": str(path), "file_state": "published"}])
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(hours=2)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(missing, [])
        self.assertTrue(decisions["bbc"]["due"])
        self.assertEqual(decisions["bbc"]["improving"], 1)

    def test_phase_b_keeps_hourly_checks_until_window_end(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=23, minutes=59))
        db = Database([ep])
        db.releases.find.return_value = [
            {"_id": "bbc:m0034hxp", "source": "bbc",
             "episode_id": ep["_id"], "duration": 7200}]
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(hours=2)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(len(missing), 1)
        self.assertEqual(decisions["bbc"]["improving"], 1)
        self.assertEqual(decisions["bbc"]["interval_minutes"], 60)
        self.assertTrue(decisions["bbc"]["due"])

    def test_phase_b_expires_at_24_hours_then_daily_fallback(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=24))
        dest = self.sports / ep["nfo_path"].replace(".nfo", ".mkv")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"complete")
        db = Database([ep])
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(hours=3)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(missing, [])
        self.assertFalse(decisions["bbc"]["due"])
        self.assertEqual(decisions["bbc"]["improving"], 0)
        self.assertEqual(decisions["bbc"]["interval_minutes"], 1440)
        self.assertEqual(decisions["bbc"]["next_at"], NOW + dt.timedelta(hours=21))
        decisions, _ = self.check(db, sources=("bbc",),
                                  now=NOW + dt.timedelta(hours=22))
        self.assertTrue(decisions["bbc"]["due"])

    def test_usable_release_after_24_hours_stays_on_daily_fallback(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(days=3))
        db = Database([ep])
        db.releases.find.return_value = [
            {"_id": "youtube:validvideo", "source": "youtube",
             "episode_id": ep["_id"], "available": True, "duration": 7200}]
        for source in ("bbc", "youtube", "rugbypass"):
            db.states[source] = {"last_success": NOW - dt.timedelta(hours=2)}
        decisions, missing = self.check(db)
        self.assertEqual(len(missing), 1)
        self.assertTrue(all(d["improving"] == 0 for d in decisions.values()))
        self.assertTrue(all(d["missing"] == 0 for d in decisions.values()))
        self.assertTrue(all(not d["due"] for d in decisions.values()))
        self.assertTrue(all(d["interval_minutes"] == 1440
                            for d in decisions.values()))

    def test_partial_publication_does_not_trigger_hourly_phase_b(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(minutes=20))
        nfo = self.sports / ep["nfo_path"]
        nfo.parent.mkdir(parents=True)
        nfo.write_text("partial")
        db = Database([ep])
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(minutes=15)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(missing, [])
        self.assertEqual(decisions["bbc"]["improving"], 0)
        self.assertFalse(decisions["bbc"]["due"])

    def test_phase_b_f1_uses_explicit_session_end_and_reddit_only(self):
        ep = episode("Formula_1", NOW - dt.timedelta(hours=2))
        db = Database([ep])
        db.releases.find.return_value = [
            {"_id": "reddit-teletz:verified", "source": "reddit-teletz",
             "episode_id": ep["_id"], "available": True}]
        db.states["reddit-teletz"] = {"last_success": NOW - dt.timedelta(minutes=15)}
        decisions, missing = self.check(db, sources=("reddit-teletz",))
        self.assertEqual(len(missing), 1)
        self.assertEqual(decisions["reddit-teletz"]["improving"], 1)
        self.assertFalse(decisions["reddit-teletz"]["due"])
        self.assertEqual(decisions["reddit-teletz"]["interval_minutes"], 60)

    def test_phase_a_new_event_can_poll_while_another_event_is_in_phase_b(self):
        improving = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=4), 103)
        no_release = episode("Rugby_Womens_PWR", NOW - dt.timedelta(minutes=10), 104)
        db = Database([improving, no_release])
        db.releases.find.return_value = [
            {"_id": "bbc:m0034hxp", "source": "bbc",
             "episode_id": improving["_id"], "duration": 7200}]
        db.states["bbc"] = {"last_success": NOW - dt.timedelta(minutes=10)}
        decisions, missing = self.check(db, sources=("bbc",))
        self.assertEqual(len(missing), 2)
        self.assertEqual(decisions["bbc"]["missing"], 1)
        self.assertEqual(decisions["bbc"]["improving"], 1)
        self.assertEqual(decisions["bbc"]["interval_minutes"], 5)
        self.assertTrue(decisions["bbc"]["due"])

    def test_known_short_or_expired_release_does_not_block_discovery(self):
        ep = episode("Formula_1", NOW - dt.timedelta(minutes=30))
        db = Database([ep])
        db.states["reddit-teletz"] = {
            "last_success": NOW - dt.timedelta(minutes=10)}
        for release in (
            {"source": "reddit-teletz", "episode_id": ep["_id"],
             "duration": 30},
            {"source": "reddit-teletz", "episode_id": ep["_id"],
             "duration": 5400, "availability_check": {"status": "unavailable"}},
            {"source": "reddit-teletz", "episode_id": ep["_id"],
             "duration": 5400, "availability_check": {"status": "transient"}},
            {"source": "reddit-teletz", "episode_id": ep["_id"],
             "metadata": {"duration": 12}},
        ):
            with self.subTest(release=release):
                db.releases.find.return_value = [release]
                decisions, _ = self.check(db, sources=("reddit-teletz",))
                self.assertTrue(decisions["reddit-teletz"]["due"])

    def test_explicit_series_and_round_filters_constrain_poll_demand(self):
        prem = episode("Rugby_Mens_Intl", NOW - dt.timedelta(hours=2), 201)
        prem["round"] = 2
        pwr = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=3), 202)
        pwr["round"] = 3
        db = Database([prem, pwr])
        state, missing = polling.plan(
            db, sources=("bbc", "rugbypass"), now=NOW,
            sports_root=self.sports, series_ids=("Rugby_Mens_Intl",),
            round_no=2, episode_number=201)
        self.assertEqual([e["series_id"] for e in missing], ["Rugby_Mens_Intl"])
        self.assertEqual(state["bbc"]["missing"], 0)
        self.assertEqual(state["rugbypass"]["missing"], 1)

    def test_invalid_sports_root_fails_closed_without_indexing(self):
        db = Database([episode("Rugby_Womens_PWR",
                               NOW - dt.timedelta(hours=1))])
        with self.assertRaisesRegex(ValueError, "sports root"):
            polling.plan(db, sources=("bbc",), now=NOW,
                         sports_root="/missing/sports/root")

    def test_f1_requires_explicit_session_end(self):
        recent = episode("Formula_1", NOW - dt.timedelta(hours=1))
        missing_end = episode("Formula_1", NOW - dt.timedelta(hours=4), 102)
        del missing_end["scheduled_end"]
        future = episode("Formula_1", NOW + dt.timedelta(hours=1), 103)
        db = Database([recent, missing_end, future])
        _, missing = self.check(db, sources=("reddit-teletz",))
        self.assertEqual([row["episode_id"] for row in missing], [recent["_id"]])

    def test_cancelled_unnumbered_or_unfileable_episodes_do_not_poll(self):
        old = NOW - dt.timedelta(hours=2)
        cancel = episode("Rugby_Womens_PWR", old, 101)
        cancel["status"] = "cancelled"
        unnumbered = episode("Rugby_Womens_PWR", old, 102)
        unnumbered["episode"] = None
        unsupported = episode("Rugby_Womens_PWR", old, 103)
        unsupported.pop("nfo")
        db = Database([cancel, unnumbered, unsupported])
        _, missing = self.check(db, sources=("bbc",))
        self.assertEqual(missing, [])

    def test_registered_published_file_and_moved_history_block_polling(self):
        ep = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=2))
        other = episode("Rugby_Womens_PWR", NOW - dt.timedelta(hours=3), 102)
        path = self.sports / "historical.mkv"
        path.write_bytes(b"video")
        db = Database([ep, other], files=[
            {"episode_id": ep["_id"], "path": str(path)},
            {"episode_id": other["_id"], "file_state": "moved"},
        ])
        _, missing = self.check(db, sources=("bbc",))
        self.assertEqual(missing, [])

    def test_failure_retry_and_record_success(self):
        db = Database()
        source = "bbc"
        db.states[source] = {"last_success": NOW - dt.timedelta(days=2)}
        for n, minutes in [(1, 5), (2, 10), (3, 20), (4, 40), (5, 60), (6, 60)]:
            with self.subTest(n=n):
                delay = polling.record_failure(db, source, now=NOW)
                self.assertEqual(delay, dt.timedelta(minutes=minutes))
                decisions, _ = self.check(db, sources=(source,))
                self.assertFalse(decisions[source]["due"])
                self.assertEqual(decisions[source]["reason"], "failure_backoff")
        polling.record_success(db, source, now=NOW)
        self.assertEqual(db.states[source]["failures"], 0)
        self.assertNotIn("retry_not_before", db.states[source])
        decisions, _ = self.check(db, sources=(source,))
        self.assertFalse(decisions[source]["due"])


if __name__ == "__main__":
    unittest.main()
