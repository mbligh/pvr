"""Shared rugby/F1 acquisition CLI interface and legacy compatibility."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, script):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / script))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


rugby = load("pvr_acquire_rugby_cli", "acquire_rugby")
f1 = load("pvr_acquire_f1_cli", "acquire_f1")
old = load("pvr_acquire_missing_cli", "acquire_missing")


class AcquisitionCLITests(unittest.TestCase):
    def cli(self, worker, flags):
        from pymongo import MongoClient
        with patch.dict(os.environ, {"MONGODB_URI": "mongodb://example.invalid/pvr"}), \
             patch("pymongo.MongoClient") as client, \
             patch.object(worker, "run", return_value={"failed": 0}) as run:
            code = worker.main(flags)
        self.assertEqual(code, 0)
        client.assert_called_once_with(
            "mongodb://example.invalid/pvr", serverSelectionTimeoutMS=15000)
        args, kwargs = run.call_args
        self.assertFalse(kwargs["apply"])
        self.assertEqual(kwargs["season"] if worker is rugby else kwargs["year"], 2026)
        self.assertEqual(kwargs["round_no"], 4)
        self.assertEqual(kwargs["episode_number"], 401)
        self.assertEqual(kwargs["limit"], 2)

    def test_both_workers_accept_same_season_round_episode_preview_options(self):
        self.cli(rugby, ["--season", "2026", "--round", "4",
                         "--episode", "401", "--limit", "2", "--dry-run"])
        self.cli(f1, ["--season", "2026", "--round", "4",
                      "--episode", "401", "--limit", "2", "--dry-run"])

    def test_year_alias_works_in_both_workers(self):
        for worker in (rugby, f1):
            self.cli(worker, ["--year", "2026", "--round", "4",
                              "--episode", "401", "--limit", "2"])

    def test_apply_and_dry_run_are_mutually_exclusive(self):
        for worker in (rugby, f1):
            with self.subTest(worker=worker.__name__), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    worker.main(["--apply", "--dry-run"])
                self.assertEqual(caught.exception.code, 2)

    def test_old_name_exposes_same_worker_and_help(self):
        self.assertEqual(old.run.__code__.co_code, rugby.run.__code__.co_code)
        self.assertTrue(callable(old.main))
        result = subprocess.run([sys.executable, str(ROOT / "acquire_missing"), "--help"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--season", result.stdout)
        self.assertIn("--round", result.stdout)


if __name__ == "__main__":
    unittest.main()
