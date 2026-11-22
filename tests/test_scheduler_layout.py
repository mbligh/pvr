"""Scheduler ownership and backward-compatible command entry points."""
import importlib.machinery
import pathlib
import unittest

from schedulers import f1, f1_data
from schedulers.rugby import importer, pwr_history

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUGBY = ROOT / "schedulers" / "rugby"


class SchedulerLayoutTests(unittest.TestCase):
    def test_metadata_producers_are_under_schedulers(self):
        for mod in (f1, f1_data, importer, pwr_history):
            with self.subTest(module=mod.__name__):
                self.assertTrue(
                    pathlib.Path(mod.__file__).is_relative_to(ROOT / "schedulers"))
        self.assertEqual(importer.RUGBY_DIR, RUGBY)
        self.assertFalse((ROOT / "calendar_legacy").exists())
        self.assertFalse((RUGBY / "legacy").exists())
        for filename in ("calendars.json", "plex_nfo.py", "filter.py",
                         "pwr.py", "prem.py", "world_rugby.py"):
            self.assertTrue((RUGBY / filename).is_file())
        self.assertTrue((ROOT / "tests" / "test_rugby_world_rugby.py").is_file())
        self.assertTrue((ROOT / "tests" / "test_rugby_nfo_paths.py").is_file())

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
        # Package-local imports do not depend on a legacy sys.path override
        # or a calendar repository checkout. First bootstrap may fetch *data*.
        self.assertTrue(pathlib.Path(importer.plex_nfo.__file__).is_relative_to(RUGBY))
        self.assertTrue(pathlib.Path(importer.calendar_filter.__file__).is_relative_to(RUGBY))
        self.assertTrue(pathlib.Path(importer.fetch_world_rugby.__code__.co_filename).is_relative_to(RUGBY))
        self.assertIs(importer.plex_nfo.start_time, importer.calendar_filter.start_time)
        from schedulers.rugby import pwr_history, nations_history
        self.assertIs(pwr_history.plex_nfo, importer.plex_nfo)
        self.assertIs(nations_history.plex_nfo, importer.plex_nfo)


if __name__ == "__main__":
    unittest.main()
