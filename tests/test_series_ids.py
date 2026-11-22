"""Directory-name identity migration: preflight, references and backups."""
import gzip
import importlib.machinery
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock

from bson.json_util import loads
from schedulers.series_ids import (LEGACY_TO_CANONICAL, CANONICAL_TO_LEGACY,
                                   canonicalize, canonical_text, assert_migrated)

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATE = importlib.machinery.SourceFileLoader(
    "migrate_series_ids_test", str(ROOT / "migrate_series_ids")
).load_module()


class Collection:
    def __init__(self, docs):
        self.docs = {doc["_id"]: dict(doc) for doc in docs}

    def find(self, filter):
        assert filter == {}
        return list(self.docs.values())

    def find_one(self, filter):
        return self.docs.get(filter["_id"])

    def insert_one(self, record):
        assert record["_id"] not in self.docs
        self.docs[record["_id"]] = record

    def delete_one(self, filter):
        del self.docs[filter["_id"]]

    def replace_one(self, filter, replacement):
        assert filter["_id"] in self.docs
        self.docs[filter["_id"]] = replacement


class DB:
    def __init__(self, **collections):
        self.collections = {k: Collection(v) for k, v in collections.items()}

    def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, key):
        return self.collections[key]


class SeriesIdentityTests(unittest.TestCase):
    def test_exact_directory_id_mapping(self):
        expected = {
            "rugby-womens-wxv": "Rugby_Womens_WXV",
            "rugby-nations": "Rugby_Mens_Nations",
            "rugby-womens-pwr": "Rugby_Womens_PWR",
            "rugby-mens-intl": "Rugby_Mens_Intl",
            "formula-1": "Formula_1",
        }
        for old, new in expected.items():
            self.assertEqual(LEGACY_TO_CANONICAL[old], new)
            self.assertEqual(CANONICAL_TO_LEGACY[new], old)
            self.assertEqual(canonical_text(old + ":world-rugby-id@mbligh.github.io"),
                             new + ":world-rugby-id@mbligh.github.io")
        self.assertEqual(canonical_text("f1:11416"), "Formula_1:11416")
        self.assertEqual(canonical_text("world-rugby-foo@mbligh.github.io"),
                         "world-rugby-foo@mbligh.github.io")
        self.assertEqual(canonical_text("rugbypass:1051364"), "rugbypass:1051364")
        self.assertEqual(canonical_text("WXV Global Series Challenger"), "WXV Global Series Challenger")

    def test_directory_path_and_manifest_migration(self):
        before = {
            "_id": "rugby-nations:world-rugby-123@mbligh.github.io",
            "series_id": "rugby-nations",
            "nfo_path": ("Rugby_Nations/Season_2026/"
                         "Rugby_Nations-s2026e101-Test.nfo"),
            "nfo": "<showtitle>Rugby Nations Championships</showtitle>",
            "external_ids": {"world_rugby": "123"},
        }
        after = canonicalize(before)
        self.assertEqual(after["_id"],
                         "Rugby_Mens_Nations:world-rugby-123@mbligh.github.io")
        self.assertEqual(after["series_id"], "Rugby_Mens_Nations")
        self.assertEqual(after["nfo_path"],
                         "Rugby_Mens_Nations/Season_2026/"
                         "Rugby_Mens_Nations-s2026e101-Test.nfo")
        self.assertEqual(after["nfo"], before["nfo"])
        self.assertEqual(canonicalize({"files": {before["nfo_path"]: "abc"}}),
                         {"files": {after["nfo_path"]: "abc"}})

    def test_cross_collection_migration_and_preserved_uid(self):
        old = "rugby-womens-pwr:pwr-295034@mbligh.github.io"
        new = "Rugby_Womens_PWR:pwr-295034@mbligh.github.io"
        source = "bbc:m0034hxp"
        db = DB(
            episodes=[{"_id": old, "series_id": "rugby-womens-pwr",
                       "title": "A vs B"}],
            files=[{"_id": "slagg:/data/sports/Rugby_Womens_PWR/video.mkv",
                    "episode_id": old, "series_id": "rugby-womens-pwr"}],
            releases=[{"_id": source, "episode_id": old}],
            jobs=[{"_id": "job-1", "episode_id": old}],
            calendar_snapshots=[{"_id": "rugby-womens-pwr",
                                 "ics": "UID:pwr-295034@mbligh.github.io"}],
            metadata_state=[{"_id": "rugby_manifest",
                             "value": {"fixtures": [{"id": old,
                                                     "competition": "rugby-womens-pwr"}]}}],
        )
        changes = MIGRATE.plan(db)
        self.assertEqual(len(changes), 6)
        with tempfile.TemporaryDirectory() as tmp:
            backup = MIGRATE.backup_changes(changes, tmp)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            with gzip.open(backup, "rt") as f:
                self.assertEqual(len([loads(line) for line in f]), 6)
        self.assertEqual(sum(MIGRATE.migrate(db, changes).values()), 6)
        self.assertEqual(MIGRATE.plan(db), [])
        self.assertIn(new, db["episodes"].docs)
        self.assertNotIn(old, db["episodes"].docs)
        self.assertEqual(db["releases"].docs[source]["episode_id"], new)
        self.assertEqual(next(iter(db["files"].docs.values()))["episode_id"], new)
        self.assertEqual(db["jobs"].docs["job-1"]["episode_id"], new)
        self.assertEqual(db["metadata_state"].docs["rugby_manifest"]["value"]["fixtures"][0]["id"], new)
        self.assertEqual(db["calendar_snapshots"].docs["Rugby_Womens_PWR"]["ics"],
                         "UID:pwr-295034@mbligh.github.io")

    def test_preflight_rejects_nonmatching_target_and_does_not_write(self):
        old = {"_id": "f1:11416", "series_id": "formula-1"}
        conflict = {"_id": "Formula_1:11416", "series_id": "Formula_1",
                    "season": 1900}
        db = DB(episodes=[old, conflict])
        with self.assertRaisesRegex(ValueError, "conflicting target"):
            MIGRATE.plan(db)
        self.assertEqual(db["episodes"].docs["f1:11416"], old)

    def test_guard_fails_closed_when_old_records_remain(self):
        db = MagicMock()
        db.episodes.find_one.side_effect = [None, None]
        db.calendar_snapshots.find_one.return_value = None
        assert_migrated(db)
        db.episodes.find_one.side_effect = [{"_id": "f1:1"}]
        with self.assertRaisesRegex(ValueError, "Old series IDs"):
            assert_migrated(db)


if __name__ == "__main__":
    unittest.main()
