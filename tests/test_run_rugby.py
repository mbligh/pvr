"""run_rugby orchestration: read-only preview, discovery, fail-closed acquisition."""
import contextlib
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
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("pvr_run_rugby_tests", str(ROOT / "run_rugby"))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)


class Result:
    def __init__(self, code=0, stdout="", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


class RugbyRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.incoming = Path(self.tmp.name)
        self.calls = []
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.db = MagicMock()
        self.discovery = json.dumps({
            "mode": "apply",
            "sources": ["bbc"], "total_releases": 5
        })
        self.finished = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        self.planned_by_series = {}
        self.due_by_source = {}
        self.missing = None

    def execute(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertFalse(kwargs["check"])
        if "refresh_releases" in cmd[1]:
            source = cmd[cmd.index("--source") + 1]
            report = json.loads(self.discovery)
            report["sources"] = [source]
            return Result(stdout=json.dumps(report) + "\n")
        series = cmd[cmd.index("--series") + 1]
        count = self.planned_by_series.get(series, 1)
        return Result(stdout='{"mode": "apply"}\n',
                      stderr=json.dumps({"planned": count}) + "\n")

    def invoke(self, **kwargs):
        # Pin legacy test cases to their original three providers; the real
        # default runner includes Rugby24 for the separate men\u0027s PREM series.
        if "sources" not in kwargs and "series" not in kwargs:
            kwargs["sources"] = ["bbc", "youtube", "rugbypass"]
        selected = kwargs.get("series") or "Rugby_Womens_PWR"
        source_values = kwargs.get("sources") or (
            ["bbc", "youtube", "rugbypass"] if selected == "Rugby_Womens_PWR"
            else ["rugbypass"]
        )
        def decision(db, *, sources, now=None, **other):
            pending = (self.missing if self.missing is not None else
                       [{"series_id": selected, "episode_id": selected + ":fixture",
                         "finished": self.finished}])
            statuses = {
                src: {"due": self.due_by_source.get(src, True),
                      "reason": "due", "missing": sum(
                          ev["series_id"] in runner.polling.SOURCE_SERIES[src]
                          for ev in pending),
                      "interval_minutes": 5, "next_at": self.finished}
                for src in sources
            }
            return statuses, pending
        with patch.object(runner.polling, "plan", side_effect=decision):
            return runner.run(incoming_root=self.incoming, execute=self.execute,
                              poll_db=self.db, output=self.stdout,
                              error=self.stderr, **kwargs)

    def test_preview_calls_only_acquisition_with_no_apply(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(len(self.calls), 1)
        cmd = self.calls[0][0]
        self.assertEqual(Path(cmd[1]).name, "acquire_rugby")
        self.assertNotIn("--apply", cmd)
        self.assertNotIn("--refresh", cmd)
        self.assertFalse((self.incoming / ".pvr-rugby-run.lock").exists())

    def test_apply_refreshes_then_acquires(self):
        self.discovery = json.dumps({
            "mode": "apply", "sources": ["bbc", "youtube"], "total_releases": 5
        })
        self.assertEqual(self.invoke(apply=True, limit=2, series="Rugby_Womens_PWR",
                                  sources=["bbc", "youtube"]), 0)
        self.assertEqual([Path(x[0][1]).name for x in self.calls],
                         ["refresh_releases", "refresh_releases", "acquire_rugby"])
        # Each source is now independently throttled and recorded.
        self.assertTrue(all("--apply" in call[0] for call in self.calls))
        self.assertEqual(
            [call[0][call[0].index("--source") + 1] for call in self.calls[:2]],
            ["bbc", "youtube"])
        acquisition = self.calls[-1][0]
        self.assertIn("--apply", acquisition)
        self.assertEqual(acquisition[acquisition.index("--limit") + 1], "2")
        self.assertEqual(acquisition.count("--source"), 2)
        self.assertFalse((self.incoming / ".pvr-rugby-run.lock").exists())

    def test_skip_discovery_uses_existing_release_links(self):
        self.assertEqual(self.invoke(apply=True, skip_discovery=True), 0)
        self.assertEqual([Path(x[0][1]).name for x in self.calls], ["acquire_rugby"])
        self.assertIn("--apply", self.calls[0][0])

    def test_discovery_failure_never_calls_acquisition(self):
        def fail_discovery(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return Result(code=7, stderr="catalogue unavailable\n")
        self.execute = fail_discovery
        with self.assertRaisesRegex(RuntimeError, "refusing acquisition"):
            self.invoke(apply=True)
        self.assertEqual(len(self.calls), 1)

    def test_malformed_discovery_fails_closed_but_valid_empty_is_success(self):
        for body in ("invalid JSON", '{"mode": "preview", "sources": ["bbc"], "total_releases": 5}'):
            with self.subTest(body=body):
                self.calls.clear()
                self.discovery = body
                with self.assertRaises(Exception):
                    self.invoke(apply=True)
                self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.discovery = '{"mode": "apply", "sources": ["bbc"], "total_releases": 0}'
        self.assertEqual(self.invoke(apply=True, sources=["bbc"]), 0)
        self.assertEqual(len(self.calls), 2)

    def test_prem_series_refresh_is_only_rugby24(self):
        self.assertIn("rugby24", runner.source_set(None, None))
        self.assertEqual(runner.source_set("Rugby_Mens_Prem", None), ("rugby24",))
        self.assertEqual(self.invoke(apply=True, series="Rugby_Mens_Prem"), 0)
        refresh, acquire = (x[0] for x in self.calls)
        self.assertEqual(refresh[refresh.index("--source") + 1], "rugby24")
        self.assertEqual(acquire[acquire.index("--source") + 1], "rugby24")
        self.assertEqual(refresh[refresh.index("--rugby24-category") + 1], "prem")
        self.assertEqual(
            self.db.indexer_poll_state.update_one.call_args.args[0]["_id"],
            "rugby24:prem")
        with self.assertRaises(ValueError):
            runner.source_set("Rugby_Mens_Prem", ["youtube"])

    def test_championship_series_refresh_and_acquire_is_rugby24(self):
        self.assertEqual(runner.source_set("Rugby_Mens_Championship", None),
                         ("rugby24",))
        self.assertEqual(self.invoke(apply=True, series="Rugby_Mens_Championship"), 0)
        refresh, acquire = (x[0] for x in self.calls)
        self.assertEqual(refresh[refresh.index("--source") + 1], "rugby24")
        self.assertEqual(acquire[acquire.index("--source") + 1], "rugby24")
        self.assertEqual(refresh[refresh.index("--rugby24-category") + 1],
                         "championship")
        self.assertEqual(
            self.db.indexer_poll_state.update_one.call_args.args[0]["_id"],
            "rugby24:championship")
        with self.assertRaisesRegex(ValueError, "Rugby24 only"):
            runner.source_set("Rugby_Mens_Championship", ["youtube"])

    def test_all_series_scans_rugby24_categories_independently(self):
        self.assertEqual(self.invoke(apply=True, sources=["rugby24"]), 0)
        refreshes = [cmd for cmd, _ in self.calls
                     if Path(cmd[1]).name == "refresh_releases"]
        self.assertEqual(len(refreshes), 2)
        self.assertEqual([cmd[cmd.index("--rugby24-category") + 1]
                          for cmd in refreshes], ["prem", "championship"])
        self.assertEqual(
            [call.args[0]["_id"] for call in
             self.db.indexer_poll_state.update_one.call_args_list],
            ["rugby24:prem", "rugby24:championship"])

    def test_failed_prem_check_does_not_block_championship_refresh(self):
        def partial_failure(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if Path(cmd[1]).name == "refresh_releases":
                category = cmd[cmd.index("--rugby24-category") + 1]
                if category == "prem":
                    return Result(code=1, stderr=(
                        "Rugby24 Cloudflare challenge (HTTP 403)\n"))
                return Result(stdout=json.dumps({
                    "mode": "apply", "sources": ["rugby24"],
                    "total_releases": 1}))
            return Result(stderr='{"planned": 0}\n')
        self.execute = partial_failure
        with self.assertRaisesRegex(RuntimeError, "refusing acquisition"):
            self.invoke(apply=True, sources=["rugby24"])
        self.assertEqual(
            [cmd[cmd.index("--rugby24-category") + 1] for cmd, _ in
             self.calls if Path(cmd[1]).name == "refresh_releases"],
            ["prem", "championship"])
        self.assertEqual(
            [call.args[0]["_id"] for call in
             self.db.indexer_poll_state.update_one.call_args_list],
            ["rugby24:prem", "rugby24:championship"])

    def test_championship_challenge_cooldown_and_force_discovery(self):
        def blocked(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if Path(cmd[1]).name == "acquire_rugby":
                return Result(stderr='{"planned": 1}\n')
            return Result(code=1, stderr=(
                "Rugby24 indexer: Rugby24 Cloudflare challenge (HTTP 403) "
                "at /champions-cup\n"))
        self.execute = blocked
        with self.assertRaisesRegex(RuntimeError, "refusing acquisition"):
            self.invoke(apply=True, series="Rugby_Mens_Championship")
        update = self.db.indexer_poll_state.update_one.call_args
        self.assertEqual(update.args[0]["_id"], "rugby24:championship")
        self.assertEqual(update.args[1]["$set"]["failure_reason"],
                         "cloudflare_challenge")
        self.calls.clear()

        # Force should not override a known Cloudflare challenge cooldown.
        def planning(db, *, sources, **kwargs):
            return ({"rugby24": {
                "due": False, "cooldown_active": True,
                "reason": "failure_backoff", "missing": 1,
                "improving": 0, "next_at": self.finished,
                "interval_minutes": 60}},
                [{"series_id": "Rugby_Mens_Championship",
                  "episode_id": "Rugby_Mens_Championship:101",
                  "finished": self.finished}])
        with patch.object(runner.polling, "plan", side_effect=planning):
            self.assertEqual(runner.run(
                apply=True, force_discovery=True,
                series="Rugby_Mens_Championship", incoming_root=self.incoming,
                execute=self.execute, poll_db=self.db, output=io.StringIO(),
                error=io.StringIO()), 0)
        self.assertEqual(len(self.calls), 1)  # acquisition only

    def test_national_series_refresh_is_only_rugbypass(self):
        self.discovery = json.dumps({
            "mode": "apply", "sources": ["rugbypass"], "total_releases": 8
        })
        self.assertEqual(self.invoke(apply=True, series="Rugby_Mens_Nations"), 0)
        refresh, acquire = (x[0] for x in self.calls)
        self.assertEqual([refresh[refresh.index("--source") + 1]], ["rugbypass"])
        self.assertEqual([acquire[acquire.index("--source") + 1]], ["rugbypass"])
        with self.assertRaisesRegex(ValueError, "RugbyPass only"):
            self.invoke(apply=True, series="Rugby_Mens_Nations", sources=["youtube"])

    def test_forward_explicit_common_and_source_options(self):
        self.assertEqual(self.invoke(apply=True, skip_discovery=True, season=2026,
                                  round_no=4, episode_number=401, limit=3,
                                  max_candidates=2, sports_root="/srv/sports",
                                  hostname="slagg", min_age_hours=4,
                                  show_skipped=True), 0)
        cmd = self.calls[0][0]
        for key, val in [("--season", "2026"), ("--round", "4"),
                         ("--episode", "401"), ("--limit", "3"),
                         ("--max-candidates", "2"), ("--host", "slagg"),
                         ("--sports-root", "/srv/sports")]:
            self.assertEqual(cmd[cmd.index(key) + 1], val)
        self.assertIn("--show-skipped", cmd)

    def test_acquisition_failure_propagates(self):
        def fail_acquisition(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            return Result(code=2)
        self.execute = fail_acquisition
        self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.invoke(apply=True, skip_discovery=True), 2)

    def test_indexer_and_downloader_site_locks_are_independent(self):
        from orchestration.locks import LockBusy, site_lock
        with site_lock(self.incoming, "indexer", "bbc"):
            with self.assertRaises(LockBusy):
                with site_lock(self.incoming, "indexer", "bbc"):
                    pass
            with site_lock(self.incoming, "indexer", "youtube"):
                with site_lock(self.incoming, "download", "bbc"):
                    pass

    def test_per_indexer_cooldown_skips_only_unneeded_providers(self):
        self.due_by_source = {"bbc": False, "youtube": False, "rugbypass": True}
        self.assertEqual(self.invoke(apply=True), 0)
        self.assertEqual([Path(x[0][1]).name for x in self.calls],
                         ["refresh_releases", "acquire_rugby"])
        self.assertEqual(self.calls[0][0][self.calls[0][0].index("--source") + 1],
                         "rugbypass")
        self.assertEqual(self.db.indexer_poll_state.update_one.call_count, 1)

    def test_busy_indexer_does_not_delay_other_sources_or_acquisition(self):
        def occupied(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if Path(cmd[1]).name == "refresh_releases" and (
                    cmd[cmd.index("--source") + 1] == "bbc"):
                return Result(code=75, stderr="PVR source refresh busy\n")
            if Path(cmd[1]).name == "refresh_releases":
                src = cmd[cmd.index("--source") + 1]
                return Result(stdout=json.dumps({"mode": "apply",
                                                   "sources": [src],
                                                   "total_releases": 0}))
            return Result(stderr='{"planned":1}\n')
        self.execute = occupied
        self.assertEqual(self.invoke(apply=True), 0)
        self.assertEqual(
            [Path(call[0][1]).name for call in self.calls],
            ["refresh_releases", "refresh_releases", "refresh_releases",
             "acquire_rugby"])
        self.assertEqual(
            [call.args[0]["_id"] for call in
             self.db.indexer_poll_state.update_one.call_args_list],
            ["youtube", "rugbypass"])

    def test_no_missing_and_all_sources_fresh_exits_without_work(self):
        self.missing = []
        self.due_by_source = dict.fromkeys(("bbc", "youtube", "rugbypass"), False)
        self.assertEqual(self.invoke(apply=True), 0)
        self.assertEqual(self.calls, [])
        self.db.indexer_poll_state.update_one.assert_not_called()

    def test_daily_fallback_runs_indexers_without_missing_fixtures(self):
        self.missing = []
        self.assertEqual(self.invoke(apply=True), 0)
        self.assertEqual(
            [Path(x[0][1]).name for x in self.calls],
            ["refresh_releases", "refresh_releases", "refresh_releases"])
        self.assertEqual(self.db.indexer_poll_state.update_one.call_count, 3)

    def test_auto_targets_all_rugby_series_with_global_episode_budget(self):
        recent = self.finished + dt.timedelta(minutes=10)
        self.missing = [
            {"series_id": "Rugby_Womens_PWR", "episode_id": "pwr:1",
             "finished": self.finished},
            {"series_id": "Rugby_Mens_Intl", "episode_id": "intl:1",
             "finished": recent},
        ]
        self.planned_by_series["Rugby_Mens_Intl"] = 0
        self.planned_by_series["Rugby_Womens_PWR"] = 1
        self.assertEqual(self.invoke(apply=True, skip_discovery=True, limit=1), 0)
        self.assertEqual(
            [c[0][c[0].index("--series") + 1] for c in self.calls],
            ["Rugby_Mens_Intl", "Rugby_Womens_PWR"])
        self.assertTrue(all(c[0][c[0].index("--limit") + 1] == "1"
                            for c in self.calls))
        # When first series consumed the budget, the second is never invoked.
        self.calls.clear()
        self.planned_by_series["Rugby_Mens_Intl"] = 1
        self.assertEqual(self.invoke(apply=True, skip_discovery=True, limit=1), 0)
        self.assertEqual(len(self.calls), 1)

    def test_cli_rejects_contradictory_flags_and_has_help(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                runner.main(["--apply", "--dry-run"])
        self.assertEqual(exc.exception.code, 2)
        for script in ("run_rugby", "acquire_rugby"):
            res = subprocess.run([sys.executable, str(ROOT / script), "--help"],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
        acquire = subprocess.run([sys.executable, str(ROOT / "acquire_rugby"), "--help"],
                                 capture_output=True, text=True)
        self.assertNotIn("--refresh", acquire.stdout)
        self.assertNotIn("--youtube-limit", acquire.stdout)


if __name__ == "__main__":
    unittest.main()
