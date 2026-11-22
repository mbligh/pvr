# Historical PWR season import (2025/26)

The PWR fixture/results site has a public JSON fixture API behind its season selector:

- User-facing page: https://www.thepwr.com/fixtures-results?compId=1095&seasonId=202501
- Fixture API: https://www.thepwr.com/api/oval/fixtures?compId=1095&seasonId=202501

On 21 November 2026 the API returned **75 fixtures** for season ID `202501`,
explicitly named `2025/26`: 18 regular-season rounds (72 fixtures),
round 19 (two semifinals) and round 20 (one final).

The `import_pwr_history` CLI intentionally reuses
`schedulers/rugby/pwr.fetch_pwr`, which allowlists fixture IDs, kickoff times,
team names, round numbers and venues and **never copies scores or match
outcomes**. It then calls `schedulers/rugby/plex_nfo.build` for the same
existing season, round-based episode numbering and XML NFO conventions as
`import_rugby`.

The first season's regular games have numbers `s2025e101` onward, the
semifinals are `s2025e1901`/`s2025e1902`, and the final is
`s2025e2001`. Identifiers retain the source fixture ID, e.g.
`Rugby_Womens_PWR:pwr-289126@mbligh.github.io`. NFOs are stored in MongoDB,
with intended filesystem paths under
`Rugby_Womens_PWR/Season_2025/`.

## Usage on slagg

```sh
cd ~/pvr
git pull

# Fetch and validate all 2025/26 fixtures, print a summary; NO Atlas writes.
./import_pwr_history --year 2025

# Write season 2025 episodes and NFO metadata to pvr.episodes.
./import_pwr_history --year 2025 --apply

# Show newly available matches, without modifying release links.
python3 match_releases

# Write unambiguous associations for BBC / YouTube / RugbyPass releases.
python3 match_releases --apply
```

The historical importer uses `~/.mongodb/pvr` and supports `MONGODB_URI`
and `MONGODB_DATABASE` overrides, like other PVR commands. `--apply`
is required for database writes. It only writes canonical PWR episode
metadata; it neither changes the live 2026/27 calendar nor schedules
downloads. It preserves any existing operational fields on episode records.

It validates that all ICS fixtures received canonical season/episode/NFO
assignments, detects duplicate identities/numbering, fails when historical
source dates belong to a different season, and preserves any previous
numbering from Atlas when rerun. A source/API failure prevents writes.

The existing daily `import_rugby` cron job never deletes episodes that do not
appear in the current calendar snapshot, so importing historical fixtures
does not require modifying that job. Older 2024/25 seasons can be investigated
separately; do not assume `seasonId` mappings exist without verification.
