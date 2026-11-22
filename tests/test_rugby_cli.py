"""Shared preview-first CLI semantics for rugby and F1 scheduler imports."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from schedulers.rugby import importer


class RugbyImportCLITests(unittest.TestCase):
    def run_preview(self, args, *, offline=False):
        buffer = io.StringIO()
        snapshots = {"Rugby_Mens_Nations": "fixture snapshot"}
        episodes = [{"episode": 601}, {"episode": None}]
        db = MagicMock()
        with patch("pymongo.MongoClient") as client_class, \
             patch.object(importer, "build_catalogue",
                          return_value=(snapshots, {"fixtures": []}, episodes)) as build, \
             patch.object(importer, "validate_existing") as validate, \
             patch.object(importer, "assert_migrated") as migrated, \
             patch.object(importer, "persist") as write, \
             patch.dict(os.environ, {"MONGODB_URI": "mongodb://example.invalid/pvr"}), \
             contextlib.redirect_stdout(buffer):
            client_class.return_value.__getitem__.return_value = db
            importer.main(args)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["preview"])
        self.assertEqual(report["database"], None if offline else "pvr")
        self.assertEqual(report["episodes"], 2)
        self.assertEqual(report["numbered"], 1)
        self.assertIs(build.call_args.args[0], None if offline else db)
        write.assert_not_called()
        if offline:
            client_class.assert_not_called()
            validate.assert_not_called()
            migrated.assert_not_called()
        else:
            client_class.assert_called_once_with(
                "mongodb://example.invalid/pvr", serverSelectionTimeoutMS=15000)
            migrated.assert_called_once_with(db)
            validate.assert_called_once_with(db, episodes)
            self.assertTrue(client_class.return_value.close.called)

    def test_default_preview_reads_atlas_but_never_writes(self):
        self.run_preview(["--no-refresh"])

    def test_dry_run_alias_also_reads_atlas(self):
        self.run_preview(["--dry-run", "--no-refresh"])

    def test_offline_preview_needs_no_atlas_connection(self):
        self.run_preview(["--offline", "--no-refresh"], offline=True)
        self.run_preview(["--offline", "--dry-run", "--no-refresh"], offline=True)

    def test_explicit_apply_writes_to_atlas(self):
        stdout = io.StringIO()
        db = MagicMock()
        snapshots = {"Rugby_Mens_Nations": "fixture snapshot"}
        manifest = {"fixtures": []}
        episodes = [{"episode": 601}]
        with patch("pymongo.MongoClient") as client_class, \
             patch.object(importer, "build_catalogue",
                          return_value=(snapshots, manifest, episodes)) as build, \
             patch.object(importer, "assert_migrated") as migrated, \
             patch.object(importer, "persist") as persist, \
             patch.dict(os.environ, {"MONGODB_URI": "mongodb://example.invalid/pvr"}), \
             contextlib.redirect_stdout(stdout):
            client_class.return_value.__getitem__.return_value = db
            importer.main(["--apply", "--no-refresh"])
        client_class.assert_called_once_with(
            "mongodb://example.invalid/pvr", serverSelectionTimeoutMS=15000)
        migrated.assert_called_once_with(db)
        self.assertIs(build.call_args.args[0], db)
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[:4],
                         (db, snapshots, manifest, episodes))
        self.assertTrue(client_class.return_value.close.called)
        self.assertIn("MongoDB upsert complete", stdout.getvalue())
        report = json.JSONDecoder().raw_decode(stdout.getvalue())[0]
        self.assertFalse(report["preview"])
        self.assertEqual(report["database"], "pvr")

    def test_live_preview_cannot_silently_fallback_to_offline(self):
        # Without a usable Atlas URI, ordinary preview must fail closed.
        with patch.dict(os.environ, {"MONGODB_URI": ""}), \
             patch.object(importer.pathlib.Path, "home",
                          return_value=Path("/nonexistent/fixture-test-home")), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                importer.main(["--no-refresh"])
        self.assertEqual(exc.exception.code, 2)

    def test_offline_and_apply_are_incompatible(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                importer.main(["--apply", "--offline"])
        self.assertEqual(exc.exception.code, 2)

    def test_apply_and_dry_run_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                importer.main(["--apply", "--dry-run"])
        self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
