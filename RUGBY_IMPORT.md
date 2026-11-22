# Rugby fixture importer

The rugby scraping, spoiler filtering, numbering and NFO helpers copied from `mbligh/calendar` now live in `schedulers/rugby/legacy/`. The canonical fixture importer implementation is `schedulers/rugby/importer.py` and its stable entry point is `import_rugby`. All of them are maintained in PVR rather than imported live from `mbligh/calendar`. PVR continues to upsert canonical fixture records into MongoDB Atlas.

## Sources

- World Rugby (men's and women's internationals, WXV, Nations Championship): world_rugby.py
  For WXV, accept the historic `WXV` and current `WXV Global Series` API
  competition labels, but **exclude the separate Challenger competition**.
  Normalize all accepted fixture descriptions to `WXV` (not the API branding).
  The series slug `Rugby_Womens_WXV`, folder `Rugby_Womens_WXV`, canonical
  showtitle and original match/calendar UIDs remain unchanged.
- PWR: pwr.py
- Gallagher PREM direct feed: prem.py (requires existing PREM_APP_ID and PREM_API_KEY credentials)
- Existing Premiership and Six Nations ICS feeds: filter.py
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
The slug in schedulers/rugby/legacy/calendars.json determines both the
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
The newly created Rugby_Mens_Prem directory is also registered for filing,
but its existing fixtures still await reliable episode numbering.
