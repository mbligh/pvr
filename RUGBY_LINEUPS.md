# Unified rugby team-sheet import

One command handles the official PWR, Gallagher PREM and World Rugby match
lineups: `./import_rugby_lineups`. It previews work by default, emits JSONL
records to stdout, and writes only with `--apply`. It is separate from
`import_rugby` (fixture calendar metadata) and `run_rugby` (recording
discovery/acquisition).

Examples:

```bash
./import_rugby_lineups                       # preview, newest 50 eligible matches across all APIs
./import_rugby_lineups --apply --limit 0      # bounded backfill with per-provider limits
./import_rugby_lineups --provider pwr --apply
./import_rugby_lineups --provider prem --fixture 294928 --apply
./import_rugby_lineups --provider world --fixture 1e74b48e-25fe-4ebe-beea-abb6cea010a0 --apply
./import_rugby_lineups --series Rugby_Womens_WXV --apply
./import_rugby_lineups --provider world --season 2026 --apply --limit 0
```

Options: `--provider {all,pwr,prem,world}` (default all), `--series`,
`--fixture`, `--season`, `--limit` (global; 0 disables only the global cap), `--max-world`,
`--max-provider`, `--world-delay`, `--provider-delay`, `--force`
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

The RIMS summary API occasionally returns a valid match sheet with one
duplicated shirt number and one vacant starter shirt. In this case the
starter's stable pitch-layout `order` can resolve the missing shirt (for
example order 5 corresponds to jersey 3; order 25 to jersey 14). The
importer **only** corrects unambiguous conflicts where an inferred starter
slot is vacant, potentially after a chain of other incorrectly numbered
starters (for example South Africa vs England in July 2026: reported
20 -> starter 7, whose existing occupant should instead be starter 4).
Each inferred player must have a recognized starter slot, and a chain must
terminate at an empty shirt number; genuine ambiguity still fails.
Each correction retains
`reported_shirt_number` (the original API number) and sets
`shirt_number_inferred: true`. Ambiguous duplicate numbers still fail
rather than silently losing a player. Some otherwise valid matches return
HTTP 200 with `{"match":null,"teams":[]}` from `/summary`. Those are
recorded as `unavailable` and retried on the normal schedule.

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

## Request budgets and HTTP throttling

Unlike fixture calendars, the team-sheet importer fetches **one HTTP response
per selected match**. This command is safe to run from cron every few hours,
but large historical backfills should not burst requests.

- Normal selection skips completed team sheets and fixtures more than three
  days in the future. Partial/unavailable sheets use age-based retries:
  every 3 hours near kickoff, daily for the following two weeks, then every
  30 days. The automatic PWR history cutoff is July 2023.
- By default, each invocation fetches **at most 8 World Rugby match summaries**,
  **12 PWR lineups**, and **12 PREM lineups**, in addition to the global
  \`--limit 50\`. Limits are applied per provider **before** selecting the
  global limit, so one provider cannot consume all the slots.
- Requests are sequential and paced **at least 10 seconds apart for World
  Rugby** and **3 seconds for PWR/PREM**, measured between the starts of
  successive requests to the *same* provider. A single run may take several
  minutes. Adjust with \`--world-delay\` / \`--provider-delay\`.
- A provider's HTTP **429**, **403**, or **503** immediately stops further
  requests to **that provider** for the remainder of the run. Other providers
  continue. The importer honors \`Retry-After\` (seconds or an HTTP date),
  subject to a minimum cooldown of **6 hours on 429**, **24 hours on 403**,
  and **1 hour on 503**.
- Cooldown deadlines persist in MongoDB Atlas \`metadata_state\` as
  \`rugby_lineups_world\`, \`rugby_lineups_pwr\`, or \`rugby_lineups_prem\`.
  Subsequent cron runs avoid the blocked provider. \`--force\` does not
  bypass these server-requested cooldowns.
- PREM wraps its underlying HTTP error in \`RuntimeError\`; the common
  importer inspects the cause to apply the same throttling policy.
- **Preview does not make HTTP requests or modify Atlas**. Other HTTP
  errors are reported without inventing lineups and without creating a
  provider-wide cooldown. Existing completed team sheets remain untouched.

\`--limit 0\` removes only the global cap; it **no longer means unbounded
requests**. Raise \`--max-world\` or \`--max-provider\` explicitly when
conducting a controlled backfill.

For a more cautious backfill:

    ./import_rugby_lineups --apply --limit 0 --max-world 4 --world-delay 20

## Optional cron

One scheduled command can update all three providers (run as the same
user that owns `~/.mongodb/pvr`):

```cron
23 */3 * * * cd /home/mbligh/pvr && ./import_rugby_lineups --apply --limit 50 >> /home/mbligh/.log/pvr-lineups 2>&1
```

This is documentation only; no remote crontab is edited automatically.
