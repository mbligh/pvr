"""Offline F1 auto-acquisition tests; no external HTTP or Atlas access."""
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from downloaders.transfernow import TransferNowError
from orchestration.f1_acquire import rows
from orchestration.f1_checks import cached_status, classify_exception

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


worker = load("pvr_acquire_f1_tests", "acquire_f1")
runner = load("pvr_run_f1_tests", "run_f1")
NOW = dt.datetime(2026, 11, 21, 16, 0, tzinfo=dt.timezone.utc)
EPISODE = {
    "_id": "Formula_1:11416", "series_id": "Formula_1",
    "season": 2026, "episode": 216, "round": 21,
    "title": "Las Vegas Grand Prix - Qualifying",
    "scheduled": NOW - dt.timedelta(hours=7),
    "scheduled_end": NOW - dt.timedelta(hours=4),
    "duration": 3600,
    "nfo_path": "Formula_1/Season_2026/Formula_1-s2026e216-Las_Vegas_Grand_Prix_Qualifying_20261120.nfo",
    "nfo": "<episodedetails><showtitle>Formula 1</showtitle><season>2026</season><episode>216</episode></episodedetails>",
}
RELEASES = [
    {"_id": "reddit-teletz:20261121sky123AA", "uri": "reddit-teletz:20261121sky123AA",
     "source": "reddit-teletz", "episode_id": EPISODE["_id"],
     "published": "2026-11-21",
     "metadata": {"url": "https://www.transfernow.net/dl/20261121sky123AA",
                  "commentary": "sky", "height": 1080}},
    {"_id": "reddit-teletz:20261121multi22BB", "uri": "reddit-teletz:20261121multi22BB",
     "source": "reddit-teletz", "episode_id": EPISODE["_id"],
     "published": "2026-11-21",
     "metadata": {"url": "https://www.transfernow.net/dl/20261121multi22BB",
                  "commentary": "multi", "contains_sky_audio": True, "height": 2160}},
]
MEDIA = {
    "duration_seconds": 4500,
    "video": {"height": 1080, "fps": 50.0},
    "audio_tracks": 1,
}


def matches(row, condition):
    for key, value in condition.items():
        val = row.get(key)
        if isinstance(value, dict):
            if "$in" in value and val not in value["$in"]:
                return False
            if "$gte" in value and not (val >= value["$gte"]):
                return False
        elif val != value:
            return False
    return True


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.updates = []

    def find(self, condition=None, projection=None):
        self.calls.append(("find", condition))
        return [dict(r) for r in self.rows if matches(r, condition or {})]

    def find_one(self, condition, projection=None):
        result = self.find(condition, projection)
        return result[0] if result else None

    def update_one(self, condition, modification, **kwargs):
        self.updates.append((condition, modification))
        for record in self.rows:
            if matches(record, condition):
                record.update(modification["$set"])
                return type("Result", (), {"matched_count": 1})()
        return type("Result", (), {"matched_count": 0})()


class FakeDB:
    def __init__(self):
        self.episodes = FakeCollection([dict(EPISODE)])
        self.releases = FakeCollection([dict(r) for r in RELEASES])
        self.files = FakeCollection([])
        self.jobs = FakeCollection([])


class F1AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.sports = base / "sports"
        self.sports.mkdir()
        self.incoming = base / "incoming"
        self.incoming.mkdir()
        self.db = FakeDB()
        self.calls = []
        self.output = io.StringIO()
        self.summary = io.StringIO()

    def path_for(self, release):
        return str(self.incoming / ("pvr-" + release["_id"].split(":", 1)[1] + ".mkv"))

    def plans(self, **overrides):
        settings = dict(sports_root=self.sports, incoming_root=self.incoming,
                        hostname="slagg", now=NOW)
        settings.update(overrides)
        return rows(self.db, **settings)

    def check(self, uri, url):
        self.calls.append(("check", uri))
        return {"entry": {"id": "onefile", "name": "video.mp4", "size": 300}}

    def download(self, database, uri, path, **kwargs):
        self.calls.append(("download", uri))
        self.assertEqual(path, str(self.incoming / (
            "pvr-" + uri.split(":", 1)[1] + ".mkv")))
        self.assertTrue(kwargs["apply"])
        return {"status": "downloaded"}

    def file(self, database, path, **kwargs):
        self.calls.append(("file", path))
        self.assertTrue(kwargs["apply"])
        return {"status": "filed"}

    def run_worker(self, *, apply=False, **overrides):
        params = dict(apply=apply, year=2026, limit=1, sports_root=str(self.sports),
                      incoming_root=str(self.incoming), hostname="slagg", now=NOW,
                      inspector=self.check, downloader=self.download, filer=self.file,
                      stream=self.output, summary_stream=self.summary)
        params.update(overrides)
        return worker.run(self.db, **params)

    def test_preview_only_reads_and_orders_sky_before_4k_multi(self):
        planned, count = self.plans()
        row, = planned
        self.assertEqual(count["due"], 1)
        self.assertEqual(row["action"], "download_then_file")
        self.assertEqual(row["candidates"][0]["source_uri"], RELEASES[0]["_id"])
        self.assertEqual(row["candidates"][1]["source_uri"], RELEASES[1]["_id"])
        result = self.run_worker()
        self.assertEqual(result["planned"], 1)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.db.releases.updates, [])
        self.assertFalse((self.incoming / ".pvr-acquire.lock").exists())

    def test_apply_checks_then_downloads_and_files_preferred(self):
        report = self.run_worker(apply=True)
        self.assertEqual((report["checked"], report["filed"], report["downloaded"]),
                         (1, 1, 1))
        self.assertEqual(self.calls, [
            ("check", RELEASES[0]["_id"]),
            ("download", RELEASES[0]["_id"]),
            ("file", self.path_for(RELEASES[0])),
        ])
        self.assertEqual(self.db.releases.rows[0]["availability_check"]["status"],
                         "available")

    def test_confirmed_http_404_triggers_sky_to_multi_fallback(self):
        def check(uri, url):
            self.calls.append(("check", uri))
            if uri == RELEASES[0]["_id"]:
                raise TransferNowError("TransferNow API returned HTTP 404: not found")
            return {"entry": {"id": "two", "name": "video.mkv", "size": 300}}
        summary = self.run_worker(apply=True, inspector=check)
        self.assertEqual(summary["fallbacks"], 1)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["filed"], 1)
        self.assertEqual([v for v in self.calls if v[0] == "download"],
                         [("download", RELEASES[1]["_id"])])
        self.assertEqual(self.db.releases.rows[0]["availability_check"]["status"],
                         "unavailable")
        self.assertEqual(self.db.releases.rows[1]["availability_check"]["status"],
                         "available")

    def test_transient_http_503_does_not_trigger_fallback(self):
        def check(uri, url):
            self.calls.append(("check", uri))
            raise TransferNowError("TransferNow API returned HTTP 503: unavailable")
        summary = self.run_worker(apply=True, inspector=check)
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(summary["fallbacks"], 0)
        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(self.calls, [("check", RELEASES[0]["_id"])])

    def test_cached_404_skips_to_second_candidate_without_check(self):
        self.db.releases.rows[0]["availability_check"] = {
            "status": "unavailable", "checked_at": NOW - dt.timedelta(hours=1)}
        report = self.run_worker(apply=True)
        self.assertEqual(report["fallbacks"], 1)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(self.calls[0], ("check", RELEASES[1]["_id"]))

    def test_cached_404_rechecks_after_expiration(self):
        self.db.releases.rows[0]["availability_check"] = {
            "status": "unavailable", "checked_at": NOW - dt.timedelta(hours=25)}
        self.run_worker(apply=True)
        self.assertEqual(self.calls[0], ("check", RELEASES[0]["_id"]))

    def test_prior_download_failure_waits_before_retrying_same_sky_link(self):
        self.db.jobs.rows.append({
            "type": "download", "host": "slagg", "status": "failed",
            "source_uri": RELEASES[0]["_id"],
            "started_at": NOW - dt.timedelta(hours=1)})
        summary = self.run_worker(apply=True)
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(summary["downloaded"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(summary["fallbacks"], 0)

    def test_download_failure_never_switches_to_another_link(self):
        def failed(*args, **kwargs):
            self.calls.append(("download_failure", args[1]))
            raise RuntimeError("partial transfer retained")
        summary = self.run_worker(apply=True, downloader=failed)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["fallbacks"], 0)
        self.assertEqual(summary["filed"], 0)
        self.assertEqual(self.calls, [
            ("check", RELEASES[0]["_id"]),
            ("download_failure", RELEASES[0]["_id"]),
        ])

    def test_all_confirmed_expired_is_not_a_fatal_job_error(self):
        def gone(uri, url):
            raise TransferNowError("Transfer status: EXPIRED")
        result = self.run_worker(apply=True, inspector=gone)
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["failed"], 0)

    def test_busy_download_provider_is_deferred_not_failed(self):
        from orchestration.locks import LockBusy
        def busy(*args, **kwargs):
            raise LockBusy("TransferNow download busy")
        report = self.run_worker(apply=True, downloader=busy)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["deferred"], 1)
        self.assertEqual(report["filed"], 0)

    def test_already_filed_is_not_downloaded_again(self):
        video = self.sports / EPISODE["nfo_path"].replace(".nfo", ".mkv")
        video.parent.mkdir(parents=True)
        video.write_bytes(b"already there")
        self.assertEqual(self.plans()[0][0]["reason"], "canonical_destination_exists")
        self.assertEqual(self.run_worker(apply=True)["planned"], 0)
        self.assertEqual(self.calls, [])

    def test_existing_registered_incoming_video_is_filed_without_download(self):
        source = self.incoming / "preexisting.mkv"
        source.write_bytes(b"known")
        self.db.files.rows.append({
            "_id": "slagg:" + str(source), "host": "slagg",
            "path": str(source), "episode_id": EPISODE["_id"],
            "season": 2026, "episode": 216,
            "series_id": "Formula_1", "release_uri": RELEASES[0]["_id"],
            "size_bytes": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "media": MEDIA,
        })
        self.assertEqual(self.plans()[0][0]["action"], "file")
        result = self.run_worker(apply=True)
        self.assertEqual((result["filed"], result["downloaded"]), (1, 0))
        self.assertEqual(self.calls, [("file", str(source))])

    def test_stale_inventory_blocks_reacquiring_same_episode(self):
        source = self.incoming / "missing.mkv"
        self.db.files.rows.append({
            "_id": "slagg:" + str(source), "host": "slagg",
            "path": str(source), "episode_id": EPISODE["_id"],
            "release_uri": RELEASES[0]["_id"], "size_bytes": 1234,
            "media": MEDIA,
        })
        self.assertEqual(self.plans()[0][0]["reason"],
                         "local_inventory_needs_reconciliation")
        self.assertEqual(self.run_worker(apply=True)["planned"], 0)

    def test_recent_window_limits_historical_backfill(self):
        self.db.episodes.rows[0]["scheduled_end"] = NOW - dt.timedelta(days=8)
        self.assertEqual(self.plans()[1]["due"], 0)
        self.assertEqual(self.plans(backfill=True)[1]["due"], 1)

    def test_expired_newest_does_not_starve_older_due_episode(self):
        older = dict(EPISODE, _id="Formula_1:11417", episode=217,
                     scheduled_end=NOW - dt.timedelta(hours=8),
                     nfo_path=EPISODE["nfo_path"].replace(
                         "s2026e216", "s2026e217"),
                     nfo=EPISODE["nfo"].replace(
                         "<episode>216</episode>", "<episode>217</episode>"))
        self.db.episodes.rows.append(older)
        alternate = dict(RELEASES[0],
                         _id="reddit-teletz:20261121race123CC",
                         uri="reddit-teletz:20261121race123CC",
                         episode_id=older["_id"],
                         metadata=dict(RELEASES[0]["metadata"],
                                       url="https://www.transfernow.net/dl/20261121race123CC"))
        self.db.releases.rows.append(alternate)

        def inspect(uri, url):
            self.calls.append(("check", uri))
            if uri in {RELEASES[0]["_id"], RELEASES[1]["_id"]}:
                raise TransferNowError("TransferNow API returned HTTP 410: gone")
            return {"entry": {"id": "file", "size": 40}}
        summary = self.run_worker(apply=True, inspector=inspect)
        self.assertEqual(summary["planned"], 2)
        self.assertEqual(summary["checked"], 3)
        self.assertEqual(summary["fallbacks"], 2)
        self.assertEqual(summary["filed"], 1)
        self.assertIn(("download", alternate["_id"]), self.calls)

    def test_availability_budget_stops_before_checking_next_episode(self):
        older = dict(EPISODE, _id="Formula_1:11417", episode=217,
                     scheduled_end=NOW - dt.timedelta(hours=8),
                     nfo_path=EPISODE["nfo_path"].replace(
                         "s2026e216", "s2026e217"),
                     nfo=EPISODE["nfo"].replace(
                         "<episode>216</episode>", "<episode>217</episode>"))
        self.db.episodes.rows.append(older)
        second = dict(RELEASES[0], _id="reddit-teletz:20261121race123CC",
                      uri="reddit-teletz:20261121race123CC",
                      episode_id=older["_id"],
                      metadata=dict(RELEASES[0]["metadata"],
                                    url="https://www.transfernow.net/dl/20261121race123CC"))
        self.db.releases.rows.append(second)
        def gone(uri, url):
            self.calls.append(("check", uri))
            raise TransferNowError("Transfer status: EXPIRED")
        summary = self.run_worker(apply=True, inspector=gone, max_checks=2)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["filed"], 0)
        self.assertNotIn(("check", second["_id"]), self.calls)

    def test_official_end_and_minimum_age_are_respected(self):
        self.db.episodes.rows[0]["scheduled_end"] = NOW - dt.timedelta(hours=1)
        self.assertEqual(self.plans()[1]["due"], 0)
        self.assertEqual(self.plans(min_age_hours=0)[1]["due"], 1)

    def test_episode_lock_refuses_same_episode_only(self):
        from orchestration.locks import episode_lock, LockBusy
        with episode_lock(self.incoming, "Formula_1:one"):
            with self.assertRaises(LockBusy):
                with episode_lock(self.incoming, "Formula_1:one"):
                    pass
            with episode_lock(self.incoming, "Formula_1:two"):
                pass

    def test_transient_cache_has_bounded_cooldown(self):
        row = {"availability_check": {
            "status": "transient", "checked_at": NOW}}
        self.assertEqual(cached_status(row, NOW), "transient")
        self.assertIsNone(cached_status(row, NOW + dt.timedelta(hours=8)))
        self.assertEqual(classify_exception(TransferNowError(
            "Storage server returned HTTP 403")), "transient")


class F1RunWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.calls = []
        self.db = MagicMock()
        self.inventory_patch = patch.object(runner.inventory, "scan")
        self.inventory = self.inventory_patch.start()
        self.addCleanup(self.inventory_patch.stop)
        self.plan_patch = patch.object(runner.polling, "plan", side_effect=self.poll_plan)
        self.plan_patch.start()
        self.addCleanup(self.plan_patch.stop)

    def poll_plan(self, db, **kwargs):
        now = dt.datetime.now(dt.timezone.utc)
        return ({"reddit-teletz": {
                    "source": "reddit-teletz", "due": True, "missing": 1,
                    "reason": "due", "next_at": now,
                    "interval_minutes": 5}},
                [{"episode_id": "Formula_1:11416",
                  "series_id": "Formula_1",
                  "finished": now - dt.timedelta(hours=4)}])

    def fake(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if "reddit_teletz" in str(cmd[1]):
            return subprocess.CompletedProcess(cmd, 0, '{"observation":{}}\n', "Teletz: 1\n")
        if "import_teletz" in str(cmd[1]):
            return subprocess.CompletedProcess(cmd, 0, '{"upserted":1}\n', "")
        return subprocess.CompletedProcess(cmd, 0, '{"filed":1}\n', "")

    def test_preview_does_not_discover_or_write(self):
        result = runner.run(incoming_root=self.path, execute=self.fake,
                            poll_db=self.db,
                            output=io.StringIO())
        self.assertEqual(result, 0)
        self.inventory.assert_not_called()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("acquire_f1", str(self.calls[0][0][1]))
        self.assertNotIn("--apply", self.calls[0][0])

    def test_apply_indexes_then_downloads_only_after_both_succeed(self):
        out = io.StringIO()
        result = runner.run(apply=True, incoming_root=self.path,
                            poll_db=self.db, execute=self.fake, output=out)
        self.assertEqual(result, 0)
        self.assertEqual([Path(call[0][1]).name for call in self.calls],
                         ["reddit_teletz", "import_teletz", "acquire_f1"])
        self.assertIn("--apply", self.calls[-1][0])
        self.assertEqual(self.calls[1][1]["input"], '{"observation":{}}\n')
        self.assertIn('"filed":1', out.getvalue())

    def test_discovery_error_aborts_without_import_or_download(self):
        def failed(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 1, "", "Reddit blocked\n")
        with self.assertRaisesRegex(RuntimeError, "discovery failed"):
            runner.run(apply=True, incoming_root=self.path, poll_db=self.db, execute=failed)
        self.assertEqual(len(self.calls), 1)

    def test_empty_discovery_is_a_successful_check(self):
        def no_records(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.assertEqual(
            runner.run(apply=True, incoming_root=self.path,
                       poll_db=self.db, execute=no_records), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([Path(c[0][1]).name for c in self.calls],
                         ["reddit_teletz", "acquire_f1"])
        self.db.indexer_poll_state.update_one.assert_called_once()

    def test_import_failure_aborts_acquisition(self):
        def fail_import(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if len(self.calls) == 1:
                return subprocess.CompletedProcess(cmd, 0, '{"observation":{}}', "")
            return subprocess.CompletedProcess(cmd, 1, "", "Atlas outage")
        with self.assertRaisesRegex(RuntimeError, "importer failed"):
            runner.run(apply=True, incoming_root=self.path, poll_db=self.db, execute=fail_import)
        self.assertEqual(len(self.calls), 2)

    def test_busy_reddit_indexer_still_allows_acquisition(self):
        from orchestration.locks import site_lock
        with site_lock(self.path, "indexer", "reddit-teletz"):
            self.assertEqual(runner.run(
                apply=True, incoming_root=self.path, poll_db=self.db,
                execute=self.fake, output=io.StringIO()), 0)
        self.assertEqual([Path(c[0][1]).name for c in self.calls],
                         ["acquire_f1"])
        self.db.indexer_poll_state.update_one.assert_not_called()

    def test_f1_not_due_still_acquires_previously_indexed_releases(self):
        self.plan_patch.stop()
        def ready_plan(db, **kwargs):
            status, episodes = self.poll_plan(db, **kwargs)
            status["reddit-teletz"]["due"] = False
            return status, episodes
        with patch.object(runner.polling, "plan", side_effect=ready_plan):
            self.assertEqual(runner.run(apply=True, incoming_root=self.path,
                                        poll_db=self.db, execute=self.fake), 0)
        self.assertEqual([Path(c[0][1]).name for c in self.calls], ["acquire_f1"])
        self.db.indexer_poll_state.update_one.assert_not_called()

    def test_f1_no_missing_and_daily_fallback(self):
        self.plan_patch.stop()
        def plan(db, **kwargs):
            status, _ = self.poll_plan(db, **kwargs)
            return status, []
        with patch.object(runner.polling, "plan", side_effect=plan):
            self.assertEqual(runner.run(apply=True, incoming_root=self.path,
                                        poll_db=self.db, execute=self.fake), 0)
        self.assertEqual([Path(c[0][1]).name for c in self.calls],
                         ["reddit_teletz", "import_teletz"])

    def test_run_f1_cli_has_matching_preview_and_year_aliases(self):
        result = subprocess.run([sys.executable, str(ROOT / "run_f1"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--year", "--season", "--apply", "--dry-run",
                     "--force-discovery", "--skip-discovery"):
            self.assertIn(flag, result.stdout)
        with patch.object(runner, "run", return_value=0) as mocked:
            self.assertEqual(runner.main(["--season", "2026", "--dry-run",
                                         "--incoming-root", str(self.path)]), 0)
        self.assertEqual(mocked.call_args.kwargs["year"], 2026)
        self.assertFalse(mocked.call_args.kwargs["apply"])

    def test_indexer_lock_is_independent_of_download_lock(self):
        from orchestration.locks import site_lock, LockBusy
        with site_lock(self.path, "indexer", "reddit-teletz"):
            with self.assertRaises(LockBusy):
                with site_lock(self.path, "indexer", "reddit-teletz"):
                    pass
            with site_lock(self.path, "download", "transfernow"):
                pass


if __name__ == "__main__":
    unittest.main()
