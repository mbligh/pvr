"""Shared lineup import plans across World Rugby, PREM and PWR."""
import datetime as dt
import unittest
from unittest.mock import MagicMock, patch

from schedulers.rugby import lineup_cli as mod

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 22, 18, tzinfo=UTC)
WORLD_ID = "1e74b48e-25fe-4ebe-beea-abb6cea010a0"

PWR = {"_id": "Rugby_Womens_PWR:pwr-295034@mbligh.github.io",
       "series_id": "Rugby_Womens_PWR", "scheduled": dt.datetime(2026, 11, 20),
       "season": 2026}
PREM = {"_id": "Rugby_Mens_Prem:legacy",
        "series_id": "Rugby_Mens_Prem",
        "external_ids": {"prem": "294928"},
        "scheduled": dt.datetime(2026, 10, 30, tzinfo=UTC),
        "season": 2026}
WORLD = {"_id": f"Rugby_Mens_Nations:world-rugby-{WORLD_ID}@mbligh.github.io",
         "series_id": "Rugby_Mens_Nations",
         "scheduled": dt.datetime(2026, 11, 21, tzinfo=UTC),
         "season": 2026}


class RugbyLineupCommandTests(unittest.TestCase):
    def test_provider_and_series_routing(self):
        self.assertEqual(mod.active_providers(), ["pwr", "prem", "world"])
        self.assertEqual(mod.active_providers("all", "Rugby_Mens_Nations"), ["world"])
        self.assertEqual(mod.active_providers("pwr"), ["pwr"])
        with self.assertRaises(ValueError):
            mod.active_providers("prem", "Rugby_Womens_PWR")
        with self.assertRaises(ValueError):
            mod.active_providers(fixture="294928")

    def test_provider_specific_match_ids(self):
        self.assertEqual(mod.parsed_fixture("pwr", "295034"), 295034)
        self.assertEqual(mod.parsed_fixture("prem", "294928"), 294928)
        self.assertEqual(mod.parsed_fixture("world", WORLD_ID), WORLD_ID)
        with self.assertRaises(ValueError):
            mod.parsed_fixture("world", "294928")
        with self.assertRaises(ValueError):
            mod.parsed_fixture("pwr", WORLD_ID)

    def test_global_limit_order_with_naive_and_aware_dates(self):
        with (patch.object(mod.pwr_lineups, "candidates", return_value=[PWR]) as pwr,
              patch.object(mod.prem_lineups, "candidates", return_value=[PREM]) as prem,
              patch.object(mod.world_rugby_lineups, "candidates", return_value=[WORLD]) as world):
            items = mod.select_candidates(MagicMock(), now=NOW, limit=2)
        self.assertEqual([item["provider"] for item in items], ["world", "pwr"])
        for mocked in (pwr, prem, world):
            self.assertEqual(mocked.call_args.kwargs["limit"], 0)

    def test_world_series_and_uuid_forwarded(self):
        with patch.object(mod.world_rugby_lineups, "candidates",
                          return_value=[WORLD]) as world:
            items = mod.select_candidates(
                MagicMock(), series="Rugby_Mens_Nations",
                fixture=WORLD_ID, now=NOW, limit=0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fixture_id"], WORLD_ID)
        self.assertEqual(world.call_args.kwargs["fixture"], WORLD_ID)
        self.assertEqual(world.call_args.kwargs["series"], "Rugby_Mens_Nations")

    def test_date_only_order(self):
        old = {**PWR, "scheduled": None, "scheduled_date": "2026-11-20"}
        self.assertGreater(mod.date_sort(WORLD), mod.date_sort(old))


if __name__ == "__main__":
    unittest.main()
