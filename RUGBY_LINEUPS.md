# Unified rugby team-sheet import

One command handles the official PWR, Gallagher PREM and World Rugby match
lineups: `./import_rugby_lineups`. It previews work by default, emits JSONL
records to stdout, and writes only with `--apply`. It is separate from
`import_rugby` (fixture calendar metadata) and `run_rugby` (recording
discovery/acquisition).

Examples:

```bash
./import_rugby_lineups                       # preview, newest 50 eligible matches across all APIs
./import_rugby_lineups --apply --limit 0      # backfill and refresh all eligible matches
./import_rugby_lineups --provider pwr --apply
./import_rugby_lineups --provider prem --fixture 294928 --apply
./import_rugby_lineups --provider world --fixture 1e74b48e-25fe-4ebe-beea-abb6cea010a0 --apply
./import_rugby_lineups --series Rugby_Womens_WXV --apply
./import_rugby_lineups --provider world --season 2026 --apply --limit 0
```

Options: `--provider {all,pwr,prem,world}` (default all), `--series`,
`--fixture`, `--season`, `--limit` (global, 0 means all), `--force`
(revisit completed or deliberately skipped matches), `--database`, and
`--apply`. A fixture ID requires a provider or a series to disambiguate
PWR and PREM numeric IDs. `--series` automatically chooses its provider.

The former executable entry points `import_pwr_lineups` and
`import_prem_lineups` remain as very small compatibility shims. Both call
the same shared command implementation with the appropriate provider
selected, so existing user cron entries continue to work. Use the single
`import_rugby_lineups` for all new jobs.

## Data sources and identities

| Provider | Match JSON | Series | Atlas collection | Match ID |
| --- | --- | --- | --- | --- |
| PWR | `https://www.thepwr.com/api/oval/lineup/match/<id>` | `Rugby_Womens_PWR` | `pwr_lineups` | Numeric ID in episode UID |
| PREM | `https://rugby-union-feeds.incrowdsports.com/v1/matches/<id>?provider=rugbyviz` | `Rugby_Mens_Prem` | `prem_lineups` | `episodes.external_ids.prem` |
| World Rugby | `https://api.wr-rims-prod.pulselive.com/rugby/v3/match/<uuid>/summary` | `Rugby_Mens_Intl`, `Rugby_Mens_Nations`, `Rugby_Womens_Intl`, `Rugby_Womens_WXV` | `world_rugby_lineups` | UUID in `world-rugby-<uuid>` episode UID |

World Rugby lineups are stored for the four listed official series only.
Unrelated legacy Google Calendar event IDs (without a World Rugby UUID)
cannot be looked up in RIMS and are skipped, rather than guessed.

World Rugby `summary.teams[0/1].teamList.list` holds home/away players;
`number` is the string shirt number, `player.id` is a string identifier,
`player.name.display` contains the public display name, and
`positionLabel` contains the role. The response also includes unnumbered
coaches and match results: those records/fields are discarded. Upcoming
matches without a published team list are `unavailable` and retried.

All collections use the canonical `episodes._id` as their document `_id`.
Documents include `fixture_id`, `series_id`, `season`, `home_team`,
`away_team`, `home`, `away`, `status`, `source`, and `checked_at`.
Player selections record IDs, names, shirt numbers, positions and optional
public name variants/captain flags. Numbers 1–23 count as matchday
selections; any 24/25 extras are saved separately under
`additional_players` where provided, and never count toward completeness.
For World Rugby, `player.id` remains a string as supplied by RIMS.

Status `complete` requires both sides to have at least 22 named numbered
players and positions 1–15. `partial` and `unavailable` data is retried
with age-based backoff; complete team sheets stop polling unless forced.
A run does not replace a previously complete team sheet with an incomplete
one. Future fixtures more than three days away are skipped by default.
PWR additionally skips fixtures dated before **July 1, 2023**, except for
explicit `--fixture` or `--force` requests.

All source APIs can contain scores, individual player statistics or events.
The lineup adapters **only allowlist selected-player fields**. Neither
fixture `episodes` nor score-free calendars/NFOs are modified. The
existing PREM public client headers can be overridden by `PREM_APP_ID`,
`PREM_API_KEY`, `PREM_REALM`. The database URI comes from
`MONGODB_URI` or `~/.mongodb/pvr`.

## Optional cron

One scheduled command can update all three providers (run as the same
user that owns `~/.mongodb/pvr`):

```cron
23 */3 * * * cd /home/mbligh/pvr && ./import_rugby_lineups --apply --limit 50 >> /home/mbligh/.log/pvr-lineups 2>&1
```

This is documentation only; no remote crontab is edited automatically.
