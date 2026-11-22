"""Canonical series IDs are the exact case-sensitive media directory names.

Historical slugs are accepted only for one-time database and public-feed
migration. Never create new episodes under an old slug.
"""
import re

LEGACY_TO_CANONICAL = {'rugby-mens-6n': 'Rugby_Mens_6N', 'rugby-mens-intl': 'Rugby_Mens_Intl', 'rugby-mens-lions': 'Rugby_Mens_Lions', 'rugby-mens-world-cup': 'Rugby_Mens_World_Cup', 'rugby-nations': 'Rugby_Mens_Nations', 'rugby-mens-prem': 'Rugby_Mens_Prem', 'rugby-womens-6n': 'Rugby_Womens_6N', 'rugby-womens-intl': 'Rugby_Womens_Intl', 'rugby-womens-lions': 'Rugby_Womens_Lions', 'rugby-womens-pwr': 'Rugby_Womens_PWR', 'rugby-womens-world-cup': 'Rugby_Womens_World_Cup', 'rugby-womens-wxv': 'Rugby_Womens_WXV', 'gallagher-prem': 'Gallagher_PREM', 'formula-1': 'Formula_1'}

# The official F1 meetingSessionKey is retained after the canonical prefix.
LEGACY_PREFIXES = {**LEGACY_TO_CANONICAL, "f1": "Formula_1"}
CANONICAL_TO_LEGACY = {v: k for k, v in LEGACY_TO_CANONICAL.items()}
ROOT_RENAMES = {"Rugby_Nations": "Rugby_Mens_Nations"}


def canonical_text(value):
    if not isinstance(value, str):
        return value
    if value in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[value]
    for old, new in LEGACY_PREFIXES.items():
        if value.startswith(old + ":"):
            return new + value[len(old):]
    # Only rewrite filesystem paths, never an arbitrary human-language title.
    for old, new in ROOT_RENAMES.items():
        value = value.replace("/" + old + "/", "/" + new + "/")
        value = value.replace("/" + old + "-", "/" + new + "-")
        if value.startswith(old + "/") or value.startswith(old + "-"):
            value = new + value[len(old):]
    return value


def canonicalize(value):
    if isinstance(value, dict):
        return {canonical_text(k): canonicalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonicalize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(canonicalize(v) for v in value)
    return canonical_text(value)


def assert_migrated(db):
    """Block post-refactor imports until old primary keys are migrated."""
    old_series = list(LEGACY_TO_CANONICAL)
    if db.episodes.find_one({"series_id": {"$in": old_series}},
                            {"_id": 1}):
        raise ValueError("Old series IDs remain in Atlas: run ./migrate_series_ids "
                         "--apply on slagg before running updated PVR jobs")
    if db.episodes.find_one({"_id": {"$regex": "^(?:f1|formula-1):"}},
                            {"_id": 1}):
        raise ValueError("Legacy F1 episode IDs remain: run ./migrate_series_ids --apply")
    if db.calendar_snapshots.find_one({"_id": {"$in": old_series}},
                                      {"_id": 1}):
        raise ValueError("Legacy calendar snapshot IDs remain: run ./migrate_series_ids --apply")
