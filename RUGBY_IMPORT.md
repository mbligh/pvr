# Rugby fixture importer

The rugby scraping and spoiler-free processing modules from mbligh/calendar have been copied into calendar_legacy/ without changing that repository. The independent PVR importer calls their established functions and upserts canonical fixture records into MongoDB Atlas.

## Sources

- World Rugby (men's and women's internationals, WXV, Nations Championship): world_rugby.py
- PWR: pwr.py
- Gallagher PREM direct feed: prem.py (requires existing PREM_APP_ID and PREM_API_KEY credentials)
- Existing Premiership and Six Nations ICS feeds: filter.py
- Existing episode numbering and NFO generation: plex_nfo.py

All supported fixture calendars are imported, including fixtures without a trustworthy season episode number (episode = null).

## Case-sensitive media-library paths

Episode NFO paths use the case of the series display name, e.g.
`Rugby_Womens_PWR/Season_2026/Rugby_Womens_PWR-s2026e101-....nfo`.
The calendar slug (`rugby-womens-pwr`) remains lowercase for stable IDs.
If a series directory differs from the display name, set `library_dir` in its
`calendar_legacy/calendars.json` entry. This only changes PVR metadata paths;
no existing video files or the original calendar repository are renamed.

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
