# Rugby fixture importer

The rugby scraping, spoiler filtering, numbering and NFO helpers originally copied from `mbligh/calendar` now live in `schedulers/rugby/`. The canonical fixture importer implementation is `schedulers/rugby/importer.py` and its stable entry point is `import_rugby`. All of them are maintained in PVR rather than imported live from `mbligh/calendar`. PVR continues to upsert canonical fixture records into MongoDB Atlas.

## Sources

- World Rugby (men's and women's internationals, WXV, Nations Championship): world_rugby.py
  For WXV, accept the historic `WXV` and current `WXV Global Series` API
  competition labels, but **exclude the separate Challenger competition**.
  Normalize all accepted fixture descriptions to `WXV` (not the API branding).
  The series slug `Rugby_Womens_WXV`, folder `Rugby_Womens_WXV`, canonical
  showtitle and original match/calendar UIDs remain unchanged.
- PWR: pwr.py
- Gallagher PREM: the official RugbyViz/InCrowd match API is the sole live source for Rugby_Mens_Prem. The public website X-APP-ID, X-API-KEY, and X-REALM client headers are included in the adapter; PREM_APP_ID, PREM_API_KEY and PREM_REALM are optional overrides if the public key rotates.
- Existing Six Nations ICS feeds: filter.py
  Men's and women's Six Nations both use calendar-year seasons and
  conservative round-number validation in schedulers/rugby/six_nations.py.
  Complete 15-match six-team fixture sets receive episodes 101..503;
  incomplete sets remain unnumbered unless a prior episode was assigned.
- Existing episode numbering and NFO generation: plex_nfo.py

All supported fixture calendars are imported, including fixtures without a trustworthy season episode number (episode = null).

## Case-sensitive media-library paths

Episode NFO paths use the case of the series display name, e.g.
`Rugby_Womens_PWR/Season_2026/Rugby_Womens_PWR-s2026e101-....nfo`.
The calendar slug is the exact media directory name (e.g. Rugby_Womens_PWR).
The slug in schedulers/rugby/calendars.json determines both the
series ID and directory. Display names do not affect canonical identity.

The PVR scheduler now uses directory names as canonical series slugs
and has no redundant library_dir overrides.
The canonical filer registers all twelve rugby directories in
[FILING.md](FILING.md). A directory mapping does not create fixtures.

**WXV migration is implemented and takes effect on the next live import.**
The scheduler partitions accepted historical WXV fixtures out of the
women's-international snapshot, creates `Rugby_Womens_WXV` episodes in
`Rugby_Womens_WXV`, and numbers WXV tournament weeks independently.
Old `Rugby_Womens_Intl` WXV records are preserved as `superseded` history
and release/job associations are remapped to the new episode IDs. Existing
media inventory under the old series is a preflight blocker, not moved
or overwritten automatically. The legacy UID suffix remains unchanged.
Source filtering now keeps World Rugby WXV events separate. Explicit
World Rugby cancellations are retained in merged scheduler snapshots
and marked in Atlas.

## Data and historical safety

On the first run, import_rugby downloads the published spoiler-free ICS snapshots and rugby metadata manifest from mbligh/calendar. Future runs use the saved Atlas state, not fresh historical data. The merger preserves fixtures after their accepted kickoff and only accepts explicit cancellation signals. Previously assigned episode numbers are retained.

Atlas database pvr uses:
- episodes: stable fixture _id, series, season, optional episode number, date, team names, venue and NFO XML where available
- calendar_snapshots: the last accepted spoiler-free ICS for each competition (persistent state)
- metadata_state: historical episode assignments and manifest

The importer validates all calendars and builds the complete batch before it starts writing. It upserts rather than deleting missing fixtures; it does not delete existing downloaded media or manage releases. Explicit cancellations are reflected in the saved calendar snapshots; episode deletion is deferred until the cancellation-handling design is completed.

## Run locally

On Debian, install dependencies via the system package manager (no venv):

    sudo apt install python3-pymongo python3-icalendar

The importer reads the Atlas URI from ~/.mongodb/pvr by default. This is a
plain-text file containing only the MongoDB connection URI, without an export
statement. Protect it with chmod 600 ~/.mongodb/pvr. MONGODB_URI, if set, takes
precedence (for GitHub Actions and other automation).

Parse the existing snapshots without connecting to Atlas:

    python3 import_rugby --dry-run --no-refresh

Perform the live import:

    python3 import_rugby

To schedule daily on slagg, add this entry with crontab -e:

    17 3 * * * cd "$HOME/pvr" && /usr/bin/python3 import_rugby >> "$HOME/pvr-import.log" 2>&1

Use a dedicated database user with readWrite access scoped to the pvr database.
MONGODB_DATABASE overrides the default database name.

## GitHub Actions credentials and networking

Set a repository secret named MONGODB_URI on mbligh/pvr. The connection string must contain the username/password for a restricted MongoDB database user. No credentials are committed. Optional PREMIERSHIP feed secrets: PREM_APP_ID, PREM_API_KEY and PREM_REALM.

Important: the Atlas PVR project currently allows only one specific IP address. GitHub-hosted runner egress IPs are variable; a secret by itself will not grant network access. Prefer a self-hosted GitHub runner with an allowlisted outbound IP, or a fixed-egress proxy. Opening Atlas to 0.0.0.0/0 is not required and is not done automatically.

The workflow runs tests on push, and the importer only on manual workflow_dispatch until access is configured and verified. After that, enable a suitable daily schedule. Existing mbligh/calendar workflows and publications remain unchanged.

## Scope and next steps

The legacy modules are copied as-is to preserve fixture parsing, spoiler avoidance, season assignment, and NFO format. The initial importer is a migration bridge, not a new generalized provider SDK. Move the common logic into cleaner modules after the first Atlas import has been validated. The existing RugbyPass catalogue is not copied; prior episode assignments and Nations Championship replay matches are preserved from the published manifest. New RugbyPass round mappings will come from the future release indexer.

After updating both repositories on slagg, preview the Atlas ID conversion with
./migrate_series_ids and apply it with ./migrate_series_ids --apply before
running import_rugby. See SERIES_ID_MIGRATION.md.

## Six Nations 2027 correction

The first import assigned 2027 Six Nations fixtures to season 2026 by
mistakenly applying the July-to-June rule used for Premiership/PWR. Both
Six Nations competitions now specify calendar-year seasons. Once a complete
15-match six-team, five-round feed is verified, round/episode numbers and
matching NFO records are generated. The underlying source ICS does not give
round labels, so an incomplete fixture set is not assigned invented rounds.
Previously accepted assignments are retained. The 2027 source snapshots were
validated against this code. Rerun ./import_rugby on slagg after pulling to
correct the stored season and NFO metadata without changing fixture UIDs.
Rugby_Mens_Prem is also registered for filing; confirmed kickoffs now receive verified API round and episode numbering.

## Gallagher PREM official API switch

Rugby_Mens_Prem obtains fixtures solely from the official InCrowd
RugbyViz API via schedulers/rugby/prem.py; the public rugbyfixture.io
Premiership ICS feed and separate Gallagher_PREM NFO pseudo-series have been
retired. Official public browser headers are coded as defaults with optional
environment overrides. The API season code rolls over in July (e.g. 202601).

This does not rewrite or duplicate the 79 existing Atlas fixture IDs:
schedulers/rugby/prem_identity.py reconciles official matches to the
previous stored spoiler-free calendar by the teams, season and kickoff date.
Current fixtures receive external_ids.prem and authoritative round numbers.
Existing historical UTC kickoffs remain frozen; no score, result or winner
fields are accepted. Provisional dates become date-only, without fabricated
kickoff times or numbered NFOs. Playoff fixtures with unknown TBC teams are
withheld until those teams are known. The first import is expected to
number 39 confirmed fixtures and retain 40 provisional date-only fixtures,
based on the live 2026-27 feed checked 2026-11-21.

On season rollover, previously stored official IDs/rounds are retained
even when those events are absent from the new current-season feed.
The calendar publisher uses the same Atlas records to export canonical
Rugby_Mens_Prem.ics and metadata. Never reintroduce the retired second
Gallagher_PREM series.

## Nations Championship 2026 round numbering and July backfill

The 2026 Nations Championship has seven official match weeks: July rounds
1-3, November rounds 4-6, and the 27-29 November Finals Weekend (Round 7).
The official fixture listing is at
https://nationschampionshiprugby.com/en/match-centre/fixtures
and the finals format is described at
https://nationschampionshiprugby.com/en/finals-weekend .

schedulers/rugby/nations.py assigns an episode number only after validating
that all six matches in a scheduled week are present, involve each of the
12 northern/southern teams exactly once, and match the published 2026
weekend dates. It preserves existing World Rugby UIDs and all previously
accepted round/episode assignments; unsupported years and incomplete
weeks are left unnumbered. Daily ./import_rugby applies this policy.

World Rugby still has 18 July 2026 fixtures in its historical match API,
but the daily importer intentionally does not fetch completed historical
months. To backfill without changing saved historical calendar snapshots
or already numbered November fixtures:

    ./import_nations_history                 # Preview only
    ./import_nations_history --apply         # Insert missing July episodes

This preview-first, opt-in command retrieves only the official July 2026
World Rugby fixtures; validates exactly six for each of rounds 1, 2 and 3,
and writes score-free canonical episodes/NFOs with insert-only upserts.
It never imports historical source scores/results, changes published ICS
snapshots, or edits existing Atlas records. Future years require separate
official round-window verification.

## PWR historical backfill coverage

./import_pwr_history --all-available previews the eight supported, currently
unimported PWR starting years 2017-2024 (2017/18 through 2024/25).
./import_pwr_history --all-available --apply imports them into Atlas,
without updating calendar snapshots or the currently published calendars.
The official PWR API has 602 fixtures across these years: 576 with confirmed
rounds and 26 in 2020/21 with no source round. The latter remain
unnumbered with no NFO path; do not invent episode numbers.

The website returned no usable 2015/16 or 2016/17 fixture lists when
checked on 2026-11-21. These two seasons are not in the --all-available
set. The already imported 2025/26 season is likewise excluded.
--year YYYY remains available for a specific historical season.

The historical importer validates all requested years before any writes,
upserts only missing records, and refuses to overwrite an existing episode
or conflicting identity. External fixture data is allowlisted and does not
copy score/result/winner information.

## Native rugby scheduler package

All actively used rugby metadata adapters and helpers live directly in
schedulers/rugby: calendar configuration, the three official fixture
adapters, spoiler-safe ICS merges, NFO generation and the separate
backfill importers. The transitional legacy subdirectory is removed.
Imports use the normal Python package, not sys.path manipulation.

The unused standalone command-line entry points previously embedded in
filter.py and plex_nfo.py are removed. Use ./import_rugby,
./import_pwr_history and ./import_nations_history for active workflows.
The two original adapter tests are now in tests/ and run with CI.
Canonical Atlas IDs, calendar freezes, NFO paths and CLI arguments
are unchanged.
