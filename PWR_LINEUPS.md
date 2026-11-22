# PWR selected-player team sheets

The public fixtures API used by `import_rugby` does **not** contain player
selections. The separate endpoint
`https://www.thepwr.com/api/oval/lineup/match/<fixture_id>`
returns `data.forwards`, `data.backs` and `data.subs`, with numbered
home/away player IDs, names, positions and captain flags.

`./import_rugby_lineups --provider pwr` previews matches due for checking;
`./import_rugby_lineups --provider pwr --apply` fetches and upserts them.
The old `./import_pwr_lineups` remains a compatibility wrapper. See
[RUGBY_LINEUPS.md](RUGBY_LINEUPS.md) for the unified command.
`--season 2025`, `--fixture 295034`, `--limit 50`, and `--force`
allow controlled backfill, direct checks and manual refresh.

Routine runs ignore fixtures dated before **July 1, 2023**, because historical
PWR team sheets are frequently missing or incomplete. This prevents repeated
errors and retries for those older matches. The cutoff also applies to
unavailable/partial matches already saved in Atlas; no deletion is required.
An explicit `--fixture 272435` or `--force` overrides the cutoff for
manual historical investigations. Fixtures with no usable date are skipped in
routine scans. Data already imported remains untouched.

Records are stored in the separate Atlas `pvr.pwr_lineups` collection with
`_id` equal to the canonical `episodes._id`. Fields include
`fixture_id`, `season`, `home_team`, `away_team`, `home`, `away`,
`status`, `source`, and `checked_at`.
`home` and `away` are arrays of player objects: `id`, `name`,
`shirt_number`, `position`, `group`, optional alternate names and
`captain`. Complete means at least 22 players per side with starters 1–15;
historic seven-substitute squads are valid. Empty or partial data remains
eligible for retry (every three hours near match, daily soon after, and
monthly for older gaps); complete sheets stop polling unless forced.
Reruns do not discard previously collected complete sheets.

The provider sometimes supplies extra rows with `shirtNumber: 0` alongside
the valid numbered starters and substitutes. Those unselected/unnumbered rows
are ignored, not treated as match failures. Historical data also includes
numbered extras `24` and `25`, typically labelled `sub 9` or `sub 10`.
Those player records are preserved in `additional_players.home` or
`additional_players.away` (with source shirt numbers and positions); they do
not count toward matchday 1–23 completeness. Truly duplicated or otherwise
invalid shirt numbers still produce errors. The provider also returns
`{"message":"Could not fetch match data","status":404}` for some historical
matches and unconfirmed playoffs; this known missing-data response is saved as
`unavailable` and rechecked under the normal backoff policy. Unexpected
errors still fail the run. Provisional fixtures with `scheduled_date` are not
queried more than three days ahead unless `--force` is supplied.

No scores, result fields or match statuses are copied. This importer never
changes canonical `episodes`, calendar snapshots, or NFO sidecars.
It reads `MONGODB_URI` or `~/.mongodb/pvr`, as the other PVR CLIs do.
Use a one-shot cron entry as desired:

```cron
23 */3 * * * cd "$HOME/pvr" && ./import_rugby_lineups --apply --limit 50 >> "$HOME/pvr-lineups.log" 2>&1
```
