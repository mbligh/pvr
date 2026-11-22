# Gallagher PREM selected-player lineup import

The PREM match-centre site uses the existing official RugbyViz/InCrowd JSON feed:

```text
https://rugby-union-feeds.incrowdsports.com/v1/matches/<match_id>?provider=rugbyviz
```

Provide the public website `X-APP-ID`, `X-API-KEY` and `X-REALM` headers from
`schedulers/rugby/prem.py` (same environment overrides). Each team's `players`
array exposes IDs, names, `positionId` (jersey number 1–23), positional labels,
and optional captain information. No page HTML parsing is needed.

`./import_prem_lineups` is preview-only by default. Run
`./import_prem_lineups --apply --limit 0` to backfill available entries, or
`./import_prem_lineups --fixture 294928 --apply` for an individual match.
`--season 2026`, `--limit 50`, and `--force` are also supported. The
`--fixture` argument uses the **official** PREM numeric match ID.

The importer matches `episodes.external_ids.prem` to the source match ID.
**Do not assume PREM episode `_id` contains the official match ID**: current
records use stable legacy hashes from the original rugby calendars.

Documents in `pvr.prem_lineups` use the canonical episode `_id` and include
`fixture_id`, `series_id`, `season`, `home_team`, `away_team`, `home`,
`away`, `status`, `source`, and `checked_at`. `home` and `away` are
position-sorted arrays of player documents (`id`, `name`, `shirt_number`,
`position`, `group`, optional name variants, and `captain`).
`complete` means at least 22 per side with 1–15 starters; 23 is standard.
Empty and partial team sheets are retried with a staged schedule. Previously
complete results are never overwritten with incomplete data.

This importer reads only allowlisted player fields. It never stores score,
match outcome, match events, or player statistics; it never modifies the
spoiler-free episode calendar, `episodes` collection, or NFO.

The default MongoDB connection comes from `~/.mongodb/pvr`, or from
`MONGODB_URI` if defined. The optional one-shot cron entry is:

```cron
31 */3 * * * cd "$HOME/pvr" && ./import_prem_lineups --apply --limit 50 >> "$HOME/pvr-prem-lineups.log" 2>&1
```
