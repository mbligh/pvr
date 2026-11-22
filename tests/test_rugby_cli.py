"""Shared preview-first CLI semantics for rugby and F1 scheduler imports."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from schedulers.rugby import importer


class RugbyImportCLITests(unittest.TestCase):
    def run_preview(self, args):
        buffer = io.StringIO()
        snapshots = {"Rugby_Mens_Nations": "fixture snapshot"}
        episodes = [{"episode": 601}, {"episode": None}]
        with patch.object(importer, "build_catalogue",
                          return_value=(snapshots, {"fixtures": []}, episodes)) as build, \
             patch.object(importer, "persist") as write, \
             patch.dict(os.environ, {"MONGODB_URI": ""}), \
             contextlib.redirect_stdout(buffer):
            importer.main(args)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["preview"])
        self.assertIsNone(report["database"])
        self.assertEqual(report["episodes"], 2)
        self.assertEqual(report["numbered"], 1)
        self.assertIsNone(build.call_args.args[0])
        write.assert_not_called()

    def test_default_preview_needs_no_atlas_connection(self):
        self.run_preview(["--no-refresh"])

    def test_legacy_dry_run_still_previews(self):
        self.run_preview(["--dry-run", "--no-refresh"])

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

    def test_apply_and_dry_run_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                importer.main(["--apply", "--dry-run"])
        self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
