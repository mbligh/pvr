"""Exact, case-sensitive canonical rugby library directory compatibility tests."""
import json
import pathlib
import unittest

from filing.publish import (
    RUGBY_SERIES_PATHS, SERIES_PATHS, canonical_paths, validate_nfo,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Names of existing directories under /data/sports on slagg. These are not
# inferred from the external calendar or RugbyPass competition labels.
DIRECTORIES = {
    "rugby-mens-6n": ("Rugby_Mens_6N", "Rugby Mens 6N"),
    "rugby-mens-intl": ("Rugby_Mens_Intl", "Rugby Mens Intl"),
    "rugby-mens-lions": ("Rugby_Mens_Lions", "Rugby Mens Lions"),
    "rugby-mens-world-cup": ("Rugby_Mens_World_Cup", "Rugby Mens World Cup"),
    "rugby-nations": ("Rugby_Nations", "Rugby Nations Championships"),
    "rugby-womens-6n": ("Rugby_Womens_6N", "Rugby Womens 6N"),
    "rugby-womens-intl": ("Rugby_Womens_Intl", "Rugby Womens Intl"),
    "rugby-womens-lions": ("Rugby_Womens_Lions", "Rugby Womens Lions"),
    "rugby-womens-pwr": ("Rugby_Womens_PWR", "Rugby Womens PWR"),
    "rugby-womens-world-cup": ("Rugby_Womens_World_Cup", "Rugby Womens World Cup"),
    "rugby-womens-wxv": ("Rugby_Womens_WXV", "Rugby Womens WXV"),
}


def episode(series, name, showtitle):
    relative = f"{name}/Season_2026/{name}-s2026e101-Canada_v_England_20261017.nfo"
    return {
        "_id": f"{series}:unit-test",
        "series_id": series,
        "season": 2026,
        "episode": 101,
        "nfo_path": relative,
        "nfo": (f"<episodedetails><showtitle>{showtitle}</showtitle>"
                "<season>2026</season><episode>101</episode></episodedetails>"),
    }


class CanonicalRugbyDirectories(unittest.TestCase):
    def test_all_exact_existing_directory_names_registered(self):
        self.assertEqual(set(RUGBY_SERIES_PATHS), set(DIRECTORIES))
        self.assertEqual(
            {row[0] for row in RUGBY_SERIES_PATHS.values()},
            {name for name, _ in DIRECTORIES.values()})
        self.assertEqual(
            set(SERIES_PATHS) - set(RUGBY_SERIES_PATHS), {"formula-1"})

    def test_filenames_nfo_and_paths_use_exact_names_for_each_series(self):
        for series, (name, title) in DIRECTORIES.items():
            with self.subTest(series=series):
                item = episode(series, name, title)
                self.assertEqual(validate_nfo(item), item["nfo"].encode())
                mkv, nfo = canonical_paths(item, "/data/sports")
                self.assertEqual(
                    str(mkv), f"/data/sports/{name}/Season_2026/"
                    f"{name}-s2026e101-Canada_v_England_20261017.mkv")
                self.assertEqual(
                    str(nfo), "/data/sports/" + item["nfo_path"])

    def test_cannot_file_into_different_existing_rugby_directory(self):
        names = [name for name, _ in DIRECTORIES.values()]
        for series, (expected, title) in DIRECTORIES.items():
            other = next(name for name in names if name != expected)
            ep = episode(series, other, title)
            with self.subTest(series=series, wrong_dir=other):
                with self.assertRaisesRegex(ValueError, "Unsafe or inconsistent"):
                    canonical_paths(ep, "/data/sports")

    def test_wxv_is_not_silently_redirected_from_womens_intl(self):
        ep = episode("rugby-womens-intl", "Rugby_Womens_WXV", "Rugby Womens Intl")
        with self.assertRaises(ValueError):
            canonical_paths(ep, "/data/sports")
        # A separate fixture import/migration is required to create
        # rugby-womens-wxv episodes. The legacy women's-intl NFO identity
        # is deliberately not silently rewritten.
        ep["series_id"] = "rugby-womens-wxv"
        with self.assertRaisesRegex(ValueError, "identity"):
            validate_nfo(ep)

    def test_calendar_import_configuration_pins_case_sensitive_paths(self):
        config = json.loads((ROOT / "schedulers" / "rugby" / "legacy" / "calendars.json").read_text())
        for entry in config["calendars"]:
            series = entry["slug"]
            if series in DIRECTORIES:
                self.assertEqual(entry.get("library_dir"), DIRECTORIES[series][0])

    def test_unlisted_and_bogus_series_cannot_be_filed(self):
        for series in ("rugby-mens-prem", "rugby-women-wxv", "not-real"):
            with self.subTest(series=series):
                ep = episode(series, "Rugby_Womens_WXV", "Rugby Womens WXV")
                with self.assertRaisesRegex(ValueError, "Unsupported"):
                    canonical_paths(ep, "/data/sports")


if __name__ == "__main__":
    unittest.main()
