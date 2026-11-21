"""Scheduler ownership and backward-compatible command entry points."""
import importlib.machinery
import pathlib
import unittest

from schedulers import f1, f1_data
from schedulers.rugby import importer, pwr_history

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY = ROOT / "schedulers" / "rugby" / "legacy"


class SchedulerLayoutTests(unittest.TestCase):
    def test_metadata_producers_are_under_schedulers(self):
        for mod in (f1, f1_data, importer, pwr_history):
            with self.subTest(module=mod.__name__):
                self.assertTrue(
                    pathlib.Path(mod.__file__).is_relative_to(ROOT / "schedulers"))
        self.assertEqual(importer.LEGACY, LEGACY)
        self.assertEqual(pwr_history.LEGACY, LEGACY)
        self.assertFalse((ROOT / "calendar_legacy").exists())
        for filename in ("calendars.json", "plex_nfo.py", "filter.py",
                         "pwr.py", "prem.py", "world_rugby.py"):
            self.assertTrue((LEGACY / filename).is_file())

    def test_existing_cli_entry_points_reexport_active_implementations(self):
        for script, module, method in (
                ("import_rugby", importer, "build_catalogue"),
                ("import_pwr_history", pwr_history, "prepare"),
                ("import_f1", f1, "build_episodes"),
                ("f1_data", f1_data, "collect")):
            with self.subTest(script=script):
                wrapper = importlib.machinery.SourceFileLoader(
                    "test_entry_" + script, str(ROOT / script)).load_module()
                self.assertIs(getattr(wrapper, method), getattr(module, method))
                self.assertIs(wrapper.main, module.main)

    def test_imports_do_not_require_calendar_repository_checkout(self):
        # Only local copies of the old calendar algorithms are in the
        # scheduling import path. First bootstrap may fetch *data* from the
        # published calendar repo, but never source code.
        self.assertTrue(pathlib.Path(importer.plex_nfo.__file__).is_relative_to(LEGACY))
        self.assertTrue(pathlib.Path(importer.calendar_filter.__file__).is_relative_to(LEGACY))
        self.assertTrue(pathlib.Path(importer.fetch_world_rugby.__code__.co_filename).is_relative_to(LEGACY))


if __name__ == "__main__":
    unittest.main()
