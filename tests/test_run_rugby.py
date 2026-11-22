"""run_rugby orchestration: read-only preview, discovery, fail-closed acquisition."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        self.discovery = json.dumps({
            "mode": "apply",
            "sources": ["bbc", "youtube", "rugbypass"],
            "total_releases": 5
        })

    def execute(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertFalse(kwargs["check"])
        if "refresh_releases" in cmd[1]:
            return Result(stdout=self.discovery + "\n")
        return Result(stdout='{"mode": "apply"}\n')

    def invoke(self, **kwargs):
        return runner.run(incoming_root=self.incoming, execute=self.execute,
                          output=self.stdout, error=self.stderr, **kwargs)

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
                         ["refresh_releases", "acquire_rugby"])
        # Refresh consumes the same provider filter as the acquisition stage.
        refresh = self.calls[0][0]
        self.assertIn("--apply", refresh)
        self.assertEqual(refresh.count("--source"), 2)
        acquisition = self.calls[1][0]
        self.assertIn("--apply", acquisition)
        self.assertEqual(acquisition[acquisition.index("--limit") + 1], "2")
        self.assertEqual(acquisition.count("--source"), 2)
        self.assertTrue((self.incoming / ".pvr-rugby-run.lock").is_file())

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

    def test_empty_discovery_fails_closed(self):
        for body in ('{"mode": "apply", "sources": ["bbc", "youtube", "rugbypass"], "total_releases": 0}',
                     "invalid JSON",
                     '{"mode": "preview", "sources": ["bbc", "youtube", "rugbypass"], "total_releases": 5}'):
            with self.subTest(body=body):
                self.calls.clear()
                self.discovery = body
                with self.assertRaisesRegex(RuntimeError, "no verified releases"):
                    self.invoke(apply=True)
                self.assertEqual(len(self.calls), 1)

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

    def test_run_lock_refuses_overlap(self):
        with runner.lock_run(self.incoming):
            with self.assertRaisesRegex(RuntimeError, "Another rugby"):
                with runner.lock_run(self.incoming):
                    pass
        with runner.lock_run(self.incoming):
            pass

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
