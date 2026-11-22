# PVR — Project Design

## Purpose

A general-purpose scheduled-media catalogue and acquisition system for sport, television, and other episodic content. It discovers what *should exist*, finds available recordings, chooses a suitable source, downloads them, and publishes a portable media library. Rugby and Formula 1 are initial use cases, but the core must not contain sport-specific logic.

Implementation: Python for the backend, workers, and CLI tools; MongoDB Atlas (project **PVR**) for persistent state; a lightweight, responsive web UI may be added. Prefer independently runnable components over a monolith.

## Core data model

- **Series**: logical show/competition, display name, media-library location, metadata configuration, quality policy, and calendar memberships.
- **Episode**: stable internal ID; series; numeric season and episode; title; optional scheduled time, duration, attributes, and external IDs. The metadata provider supplies an *explicit* list of episodes and their numbering; the core does not infer missing episodes from gaps. Numbers may be sparse, particularly for F1 sessions.
- **Release**: a recording discovered at a source, identified uniquely by `(source, source_id)`, with a stable source URI, title, publication/availability metadata, and any known quality information. Multiple releases can correspond to one episode.
- **File**: actual local media path, associated episode/release, probed technical details, and provenance. Track multiple candidates and replacement history, but publish one preferred recording by default.
- **Job/history**: discovery, acquisition, filing, retries, decisions, errors, and timestamps.

Store episode NFO XML directly in MongoDB alongside searchable structured fields. NFO is episode metadata, shared across BBC/YouTube/etc. The metadata provider may supply complete NFO XML initially; central generation can be a fallback. Track metadata revisions so existing sidecars can be refreshed without downloading again.

Use immutable event identities where available; `(series, season, episode)` is a library identity rather than the primary database key. Rescheduled events retain identity. Cancellation is explicit: remove/cancel items marked cancelled by the provider, but do not delete events merely because they disappear from a source.

## Component interfaces

All tools are callable independently from the command line and by the orchestrator. Keep stdout machine-readable (JSON Lines where returning many records), diagnostics on stderr, and support dry runs where meaningful. Common logic can live in shared Python modules.

1. **Metadata providers** answer *what episodes should exist?* They ingest official fixture/schedule sites, television metadata, etc., and emit canonical series/episode records with stable IDs, numbering, dates, and optionally NFO. Reuse and ultimately consolidate the existing rugby/F1 scrapers and metadata generation in `mbligh/calendar`; avoid duplicating those algorithms.
2. **Indexers** answer *what recordings can be found?* They enumerate releases, optionally filtering by search substring, date, or other provider-supported criteria. Return source ID, stable URI, title, and optional metadata/quality. **Indexers do not write MongoDB themselves**: a shared importer validates and upserts on `(source, source_id)`, preserving first/last-seen times. A vanished listing is not immediately treated as a deletion.
3. **Downloaders** accept **source URI + exact destination file path**. Use existing tools/adapters (e.g. `get_iplayer`, `yt-dlp`/`ytdl-sub`, and the RugbyPass script). Exit status reports success/failure, with distinguishable invalid, unavailable, and temporary errors; write complete files atomically, using staging and remuxing as needed. Source URIs (e.g. `bbc:m0034hxp`, `youtube:bdxiq73pR-I`) identify recordings, not expiring media URLs.
4. **Matcher/selector** associates releases with episodes, compares quality and source preferences, explains decisions, and avoids duplicate downloads. A recording can satisfy an episode without being its ideal quality; continue limited searches for upgrades. For sports, frame rate matters as well as resolution, bitrate, codec, completeness, and source reliability.
5. **Filer/publisher** probes downloaded files, assigns the canonical episode filename, writes the stored NFO, and safely publishes the completed recording. Never overwrite an existing recording accidentally; support explicit replacement/upgrade policies.

Discovery must not implicitly download; selection must not change files; filing must not discover sources. Manual CLI overrides and repeatable/idempotent operations are important for debugging.

## Scheduling

The orchestrator uses known episode times to decide *when* to search, rather than continuously polling all sources. Query after an event is likely finished; back off retries for missing recordings and perform infrequent reconciliation. Batch discovery by provider when several events need checking.

Distinguish **missing** (no usable file) from **upgradeable** (usable file exists, preferred release may appear). Stop regular polling once quality is satisfactory; use a limited upgrade window. Workers and the scheduler can run as one-shot CLI commands from cron; no permanent daemon is required.

## MongoDB and UI

MongoDB Atlas is the authoritative operational store for series, episodes, releases, files, jobs, decisions, calendars, and current status. Index common lookups and keep lightweight denormalized episode summaries for fast UI listing. The UI should read entirely from MongoDB through a small API (FastAPI is a likely choice), never synchronously run scrapers or inspect media. User actions enqueue jobs; workers update status; simple UI polling is sufficient initially.

Do not store media files in MongoDB. Keep large artwork and verbose logs on disk or at external URLs, with references and concise status in the database. Secrets remain outside the database records exposed to the UI.

## Outputs and compatibility

- **Media library is a filesystem API**, independent of Plex, Jellyfin, or Kodi. Maintain a stable series/season/episode filename convention and NFO sidecars, e.g. `/data/sports/Rugby_Womens_PWR/Season_2026/Rugby_Womens_PWR-s2026e101-....mkv` with matching `.nfo`.
- **Google Calendars and ICS are additional outputs** generated from the same canonical episode database, including calendars for manual inspection and spoiler-free views. Preserve existing Google Calendar event identity across reschedules, and protect no-spoilers titles/descriptions from results.
- **The UI is another consumer**, not the source of truth.

## Implementation status (2026-11-20)

Initial rugby ingestion is in place: `schedulers/rugby/` contains independently copied rugby parsers, fixture/ICS spoiler filtering, numbering and NFO generation from `mbligh/calendar`. Canonical scheduling logic is in `schedulers/rugby/importer.py`. `import_rugby` seeds historical accepted calendars and prior episode assignments from published snapshots, then upserts all known fixtures (including unnumbered ones) and NFO XML into the Atlas `pvr` database. See [RUGBY_IMPORT.md](RUGBY_IMPORT.md).

The GitHub Actions workflow runs offline regression tests and a snapshot-bootstrap smoke test; Atlas ingestion remains manual-only in GitHub Actions. For local runs on `slagg`, the importer reads `~/.mongodb/pvr` (plain MongoDB URI) using system Python; `MONGODB_URI` remains an optional override. GitHub-hosted Atlas imports still require a secret and allowlisted network egress. The original `mbligh/calendar` and `mbligh/tools` workflows remain unchanged. Google Calendar publishing from Atlas, F1, and release/download handling are not yet migrated.

## Release indexers (initial implementation)

Independently executable `indexers/bbc` (BBC iPlayer iBL brand, default PWR)
and `indexers/youtube` (yt-dlp /streams listing, default PWR) emit a common
JSONL release format. YouTube uses per-video, metadata-only extraction by
default to recover archived livestream dates, preferring original release/broadcast
timestamps to upload dates and recording the date's source in metadata; a
`--flat` option provides fast listing without date enrichment. The indexer
ignores only yt-dlp's explicit scheduled-upcoming-livestream failures while
retaining successfully extracted records; unexpected errors abort ingestion. The existing `mbligh/tools/video/rugbypass` now also
provides `--list-videos --jsonl` / `--list-games --jsonl`, retaining its
existing human-readable and download functions. `index_releases` validates
and upserts those release records into Atlas `pvr.releases` by stable source
URI, without modifying episode matching or download state. See
[INDEXERS.md](INDEXERS.md) for examples.

These indexers currently run independently for manual inspection. The
`match_releases` CLI now supports **preview-first**, manual PWR episode
association by canonical home/away teams and a ±2-day recording date window.
`matchers/pwr.py` contains the nine-team canonical vocabulary and known
aliases. It preserves existing links, rejects ambiguous/unavailable/non-match
content, and writes `episode_id` to release documents only with `--apply`.
See [MATCHING.md](MATCHING.md). A separate preview-first
`import_pwr_history` command now supports the verified `202501` PWR fixture
feed (75 matches, including semifinals and final), reusing the score-free
calendar adapter and legacy season/round numbering plus NFO generation.
It preserves all existing Season_2026 fixture records. The 2025 records
are added to Atlas only when the user runs it with `--apply`; see
[PWR_HISTORY.md](PWR_HISTORY.md). Future orchestration will schedule source
checks for recent missing/upgradeable content. No existing
`mbligh/calendar` or daily PVR metadata cron jobs are changed.

## Local recording inventory (first adapter)

The `inventory_files` CLI now scans existing PWR recordings from the local
filesystem, resolving canonical identities through filename season/episode,
NFO sidecars, and exact already-linked source IDs. It uses `ffprobe` to
capture duration, codec, resolution, frame rate, audio tracks and sizes.
Preview is the default; `--apply` upserts verified observations to the
separate Atlas `files` collection. Records have stable IDs based on host and
absolute path, preserve existing associations and do not move or overwrite
media. Multiple files may satisfy one episode. The first default scan root is
`/data/sports/Rugby_Womens_PWR`; operators can add or substitute arbitrary
`--root` paths including `/data/incoming`. See [INVENTORY.md](INVENTORY.md).

This only inventories **existing** files, it does not download new recordings,
select between quality options, or mark vanished files missing. Its episode
matching is currently scoped to PWR Season_2025 onward; other competitions
will get their own identity adapters. No user filesystem scan has yet been run
and no Atlas file records have been written by this change.

## Initial verified downloader adapter

`download_release` is a preview-first, single-release YouTube downloader
accepting an existing Atlas source URI and explicit absolute output path.
It validates the release-to-episode link, supported PWR season, and existing
`pvr.files` records before transfer; already-owned episodes are skipped by
default. The `downloaders/youtube.py` adapter downloads into a same-directory
temporary staging area through yt-dlp/ffmpeg, verifies the recording with
ffprobe, and publishes with atomic no-clobber hard-link semantics. On
`--apply`, `pvr.jobs` tracks execution and `pvr.files` stores the newly
verified recording (host/path, episode, source URI, size and technical
metadata). A database error after publication deliberately leaves the video
on disk for inventory recovery. See [DOWNLOADING.md](DOWNLOADING.md).

This paragraph describes the initial YouTube trial. The subsequent
quality selector, canonical filer, multi-provider download adapters,
refresh_releases importer/matcher, and bounded acquire_rugby worker are
documented below. Source selection uses measured local media properties;
explicit published-file upgrades and remote quality prediction remain future
work.

## Canonical file publisher (PWR)

`file_recording` is a preview-first filer for verified PWR recordings already
registered in `pvr.files`. It uses the episode's persisted `nfo_path` and
`nfo` XML to derive exact series/season media and NFO paths without
recomputing fixture numbering. It verifies source identity, avoids any
existing canonical path or Atlas inventory collision, checks MKV media with
ffprobe, publishes media/NFO with atomic no-clobber hard links (or a staged
copy across filesystems), and only removes the source **after** recording
the new destination and marking the source record moved in Atlas. The
`files` collection retains original provenance through `previous_file_id`
and `moved_to`. `jobs` records success/failure and possible reconciliation
needs. The configured sports-library root may be a symlink (as
`/data/sports` is on slagg); the filer follows that root while rejecting
symlinked series/season subdirectories and retaining the configured path
in Atlas. See [FILING.md](FILING.md).

The standalone read-only `select_recording` CLI and
`selection/quality.py` now rank local, physically verified `pvr.files`
records for a single episode: (1) reject files below 720p, (2) reject files
shorter than one hour, (3) among >=50fps recordings choose the largest file
in bytes, otherwise (4) choose the largest qualifying <50fps file. Exactly
720p, 3600 seconds and 50fps pass. Missing/unknown media attributes, stale
filesystem entries and moved records are not eligible. File-ID tie-breaks
are deterministic. This preference is based on measured *local* files, not
unknown quality of catalogued remote releases. See [FILING.md](FILING.md).

`file_recording` now checks the selection: it refuses an `ineligible`
or `not_preferred` source. It never automatically replaces media already
published under the sports directory, reporting `upgrade_available` if
the newly acquired recording is better, or `already_published` otherwise.
An explicit replacement/upgrade mechanism remains to be implemented.
There is still no automatic download selection or batch scheduling.

## First bounded missing-recording acquisition worker

`refresh_releases` now refreshes BBC iPlayer PWR, YouTube PWR and RugbyPass
full-match catalogues, validates source IDs, upserts discovered releases
into Atlas, and calls the existing matcher's unambiguous fixture linker.
Preview queries providers without Atlas writes; `--apply` refreshes and
links. The existing RugbyPass script's private auth is reused for catalogue
retrieval; YouTube metadata comes from yt-dlp, BBC from the iBL programme
API. This is provider discovery, not a guaranteed complete historical archive.

The `download_release` adapter additionally handles `bbc:PID` through
get_iplayer and `rugbypass:ID` through the existing authenticated
`~/tools/video/rugbypass` script, without changing the existing YouTube
or Formula 1 TransferNow paths. Both new adapters stage source MP4s,
remux with ffmpeg stream copy, verify via ffprobe and publish an exact MKV
using no-clobber semantics. BBC uses an isolated get_iplayer profile
so personal postprocessing hooks and download history are untouched.
The central filer remains the only producer of canonical NFO sidecars.

`acquire_rugby` remains a one-shot, preview-first PWR batch worker:
`--refresh` optionally invokes the source refresh first; `--apply`
makes Atlas/media changes; `--limit N` caps episodes (default 1).
A source filter and `--max-candidates` (default 3, maximum 5) bound the
provider comparison. For multiple finished matched releases of one
unpublished episode, the worker downloads all missing candidates within
that cap, measures real media FPS/resolution/duration/size, then selects
the preferred recording using the existing quality policy and files it.
This is necessary because format and size metadata may be unavailable
until media is downloaded. Previously downloaded eligible media is
reused and compared with newly available alternatives. Unselected
recordings are retained in incoming, published recordings are never
automatically replaced, and a failed candidate halts the batch without
publishing a partial comparison. Local flock and retry cooldowns remain.

See [ACQUISITION.md](ACQUISITION.md). No crontab was changed; manual
multi-source trials are required before enabling unattended operation.
Further work includes better remote metadata estimation, quality upgrades
of already-published files, broader RugbyPass discovery and cleanup.

## Existing rugby library directory registry (2026-11-21)

The PVR canonical filer now registers the full eleven existing rugby
library names verbatim: `Rugby_Mens_6N`, `Rugby_Mens_Intl`,
`Rugby_Mens_Lions`, `Rugby_Mens_World_Cup`, `Rugby_Mens_Nations`,
`Rugby_Womens_6N`, `Rugby_Womens_Intl`, `Rugby_Womens_Lions`,
`Rugby_Womens_PWR`, `Rugby_Womens_World_Cup`, and
`Rugby_Womens_WXV`. Series IDs now equal the exact, case-sensitive media directory names. Atlas,
calendar and NFO paths share a single namespace. Importer calendar slug values now equal the matching directory names, so
there is no separate library_dir namespace. This does not by itself create
fixtures or move existing recordings.
See [FILING.md](FILING.md).

WXV has a distinct `Rugby_Womens_WXV` directory and a dedicated
World Rugby fixture source. The scheduler accepts both historic `WXV`
and current `WXV Global Series` provider labels, while excluding
`WXV Global Series Challenger`. New accepted fixture descriptions are
normalized to the canonical `WXV` name. Calendar event UIDs and episode numbering remain unchanged; existing database series IDs need migration.
The migration is implemented in the
scheduler: `import_rugby` creates new WXV episode IDs, independent
tournament-week numbering and canonical NFOs, preserving original
calendar UIDs. Old WXV entries in `Rugby_Womens_Intl` become
superseded history; association migration is preflight-validated.
The live Atlas migration requires running the updated importer on slagg.

## RugbyPass international and Nations Championship expansion (2026-11-21)

The new `matchers/rugbypass.py` and preview-first `match_rugbypass`
link RugbyPass **full-match replays** to the existing numbered
`Rugby_Mens_Intl`, `Rugby_Womens_Intl`, `Rugby_Mens_Nations`, and
eventually the dedicated `Rugby_Womens_WXV` fixture series in Atlas. Titles must explicitly identify
competition/gender, home and away countries in order, and fall within a
bounded publication time after scheduled kickoff. Explicit Nations
Championship replay titles route to `Rugby_Mens_Nations`, not the duplicated
`NC:` men's-international calendar records. Ties and unnumbered fixtures
remain unmatched, as do unlabelled or unavailable replays. Existing
manual and PWR links are never rewritten.

`refresh_releases --apply` runs this matcher following RugbyPass
catalogue import, leaving the original PWR matcher in place.
`download_release`, `filing/publish.py`, and `file_recording`
allow all three series with the same staged downloads, exact canonical
stored NFO XML, quality ranking, no-clobber publication and Atlas history.
`acquire_rugby --series <id>` selects a single series; international
series are deliberately RugbyPass-only while the original PWR multi-source
default is unchanged. Worker default remains preview/one episode and
host-local locking prevents duplicate workers. No cron job or live media
download was performed as part of the code commits.

See [MATCHING.md](MATCHING.md) and [ACQUISITION.md](ACQUISITION.md).
The primary remaining gaps are missing Nations episode numbering, duplicate
World Rugby/Google fixture reconciliation, non-RugbyPass international
providers, upgrading published recordings and cleanup.

## Initial implementation and migration

1. Define/version the canonical episode, release, and adapter JSON schemas; implement MongoDB collections/indexes and a small CLI.
2. Import current `mbligh/calendar` fixture metadata, NFOs, and calendar mappings into Atlas; retain existing calendar publication/manifest during transition.
3. Wrap existing RugbyPass, BBC iPlayer, and YouTube discovery/download tools behind indexer/downloader CLIs; consolidate the current PWR filing logic into the generic filer.
4. Add event-driven polling, release matching, quality selection, and repeatable acquisition jobs.
5. Move Google Calendar generation/sync to the shared database and retire redundant calendar jobs only after output and event IDs are verified.
6. Add the web UI once the stored state and CLIs are reliable.

Keep this document updated as the design and implementation evolve.


## Formula 1 calendar data prototype (2026-11-21)

The standalone executable f1_data fetches the official season event listing from
/v1/editorial-eventlisting/events, then fetches each event's complete details
from /v1/event-tracker/meeting/{meetingKey}. The script discovers the API key
from the public Formula 1 season HTML (NEXT_PUBLIC_GLOBAL_BROADCAST_APIKEY),
with optional F1_API_KEY / --api-key override. It requires only Python 3.

Usage: ./f1_data --year 2026 > f1-2026.json, or
./f1_data --year 2026 --meeting 1279 | jq '.meetings[0].sessions'.
--limit 2 makes short exploratory runs. Diagnostics are on stderr, JSON on
stdout or at --output PATH. The output preserves listing-wide metadata and
for each meeting the original listing event, the session schedule from
meetingContext.timetables, and the entire returned event-tracker detail.

This is a data discovery/test tool, not yet an Atlas metadata importer or
calendar publisher. The event-listing endpoint currently returned the 2026
season with 25 meetings (including testing); it is not verified to support
arbitrary historical seasons, so the script rejects a mismatched season.
The source uses a website-exposed key and may change. Unit tests run offline
using synthetic responses; a live full-season run was not available in the
development environment.


## Formula 1 episode import (2026-11-21)

The preview-first executable import_f1 converts official f1_data season
snapshots into spoiler-free PVR episodes for series Formula_1. Pre-season
testing is deliberately excluded. Numbering is round*10 + session offset:
FP1=1, FP2=2, FP3=3, Sprint Qualifying=4, Sprint=5, Qualifying=6,
Race=7. The official session code, not the inconsistent sessionType label,
is authoritative. Only actual sessions are created, leaving sparse numbers.
Stable IDs use Formula_1:<meetingSessionKey>, independent of round and rescheduling.

The importer stores UTC times, circuit, official meeting/session IDs, NFO
XML and suggested paths under Formula_1/Season_YYYY. It previews by default;
--jsonl exports full records; --apply is required for Atlas upserts and
refuses conflicts with existing season/episode numbering. It never deletes
absent sessions. F1 normalizes the legacy US/Central alias to America/Chicago before timezone
conversion, since not all tzdata installations contain backward links.
See F1_IMPORT.md. No Atlas changes or calendar publications
were performed as part of this addition.


## Reddit/teletz F1 release discovery (2026-11-21)

The indexers/reddit_teletz adapter reuses the existing mbligh/tools Reddit
client to collect verified top-level AutoModerator-thread comments and
extract independent TransferNow releases. Its default output is JSONL
compatible with index_releases; --bundle carries raw source observations too.
The preview-first import_teletz consumer writes raw comment content,
provenance and content fingerprints to pvr.source_observations while writing
the extracted releases through the shared index_releases importer to
pvr.releases. Exact F1 title matching links to pvr.episodes when safe; source
links are rechecked on each scan, so edits can add new releases. A dedicated
Sky source is preferred over MULTi Sky, then F1TV, but actual download
selection is not yet implemented. No live Atlas writes or cron changes
are part of this code change. See REDDIT_TELETZ.md.


## Formula 1 Reddit release selection preview (2026-11-21)

The standalone read-only select_f1_releases CLI queries Atlas pvr.episodes and
pvr.releases, groups matched reddit-teletz TransferNow options by Formula 1
episode, and prints all ranked alternatives with the selected URI and reasons.
The JSON mode provides the same structured preview for tooling. Selection
prefers dedicated Sky over MULTi with English Sky (even at 4K), then F1TV,
then other/unknown. Within each commentary tier it prefers declared resolution
and newer publication date, with stable deterministic tie-breaking.
Unavailable/malformed links remain visible but cannot be selected, and older
links are warned about rather than asserted to be expired. This changes no
Atlas records and does not download, check links or write media files. The
existing tools/transfernow/transfernow downloader is a future adapter
candidate; it remains in mbligh/tools for now. See REDDIT_TELETZ.md.


## TransferNow downloader relocation (2026-11-21)

The standalone TransferNow CLI formerly maintained in mbligh/tools/transfernow
is now an executable `./transfernow` in the PVR root with its importable
implementation in `downloaders/transfernow.py`. Its original website API,
metadata listing, HEAD checks, curl resume, size verification and standalone
CLI are preserved. The original offline tests were moved to
`tests/test_transfernow.py` and run in the PVR CI suite. TransferNow remains
separate from discovery and episode selection; the PVR-managed job/recording
adapter must explicitly call the importable module. See TRANSFERNOW.md.

## PVR-managed TransferNow downloads (2026-11-21)

The preview-first download_release command now accepts a matched
reddit-teletz: URI for a canonical Formula 1 episode. The adapter validates
the Atlas release and URL, downloads exactly one video with the migrated
TransferNow implementation, remuxes if necessary, probes with ffprobe,
publishes with atomic no-clobber linking and registers Atlas file/job state.
No download occurs without --apply. Source fallback, availability recording,
and scheduling remain separate future work. The PVR adapter now retains .part files and completed source media
in per-release, per-destination private staging directories, bound to
the original TransferNow file identity via a manifest. Interrupted
downloads retry with refreshed signed storage URLs and curl resume.
Changed metadata, symlinked cache files and concurrent writers fail
closed. The cache is removed only after verified no-clobber publication;
orphaned caches require explicit manual cleanup.
See TRANSFERNOW.md.


## Formula 1 canonical filing (2026-11-21)

The existing preview-first file_recording command now also files verified
Formula 1 recordings (Formula_1 series) using the Atlas episode nfo_path
and stored NFO XML. The canonical root is Formula_1/Season_YYYY.
filing/publish.py selects the permitted directory and NFO showtitle from
the verified episode series; path traversal, symlinked subdirectories, and
collisions are rejected for either PWR or Formula 1. All existing safe
publication, cross-filesystem copy, verification, job history, destination
inventory, source provenance and idempotent retry checks are reused. An
F1-specific read-only selection policy requires >=720p, valid frame rate,
and at least half the official session duration (minimum 15 minutes),
rather than PWR's fixed one-hour floor. When multiple local recordings
exist, known dedicated Sky commentary is preferred over MULTi Sky, then
F1TV, then other/unknown; within a tier prefer >=50 fps, resolution,
and size. No media is moved on GitHub; ./file_recording --apply must run
on the host with the real file and Atlas connection. See F1_IMPORT.md.

## Persistent TransferNow retry staging (2026-11-21)

The TransferNow PVR adapter now keeps its staging directory across
interrupted downloads, keyed by both release URI and destination. A
manifest binds partial bytes to the original transfer URL, file ID,
filename and expected byte count, preventing silent reuse after metadata
changes. The shared TransferNow core handles curl --continue-at -
and complete-byte verification. Remux and ffprobe failures retain the
fully downloaded original; staging is removed only after verified
atomic no-clobber publication. A per-stage file lock rejects overlapping
downloads. Cache entries are not automatically expired or garbage
collected. This change does not enable unattended schedules, fallback
selection, or downloads without --apply. See TRANSFERNOW.md.


## Formula 1 acquisition and scheduling (2026-11-21)

The preview-first acquire_f1 worker handles recent missing F1 episodes
using Atlas releases, Sky-first ranking, verified TransferNow availability,
download_release and file_recording. Confirmed gone/unsupported sources
allow fallback; transient checks and interrupted downloads do not. A
separate availability_check field persists status in MongoDB independently
from imported indexer metadata. The runner bounds downloads and checks,
uses a recent-session window, respects local inventory/recovery states,
and shares the lock with PWR. The run_f1 one-shot wrapper refreshes Reddit
and imports verified observations before attempting acquisition, aborting
if discovery/import fails. No crontab is installed automatically. See
F1_AUTOMATION.md.


## Scheduler ownership and Atlas-backed calendar consumer (2026-11-21)

Canonical fixture metadata creators now live under `schedulers/`:
`schedulers/rugby/importer.py`, `schedulers/rugby/pwr_history.py`,
`schedulers/f1.py`, and `schedulers/f1_data.py`. Rugby
source/calendar/numbering code, formerly in `calendar_legacy/`, is now
maintained in `schedulers/rugby/`; the old directory was removed.
`import_rugby`, `import_pwr_history`, `import_f1`, and `f1_data`
remain top-level compatibility CLIs so existing slagg cron and callers
do not change. `indexers/` discover releases; `downloaders/` handle
recording transfers. Release import/matching remains separate from
`pvr.episodes` scheduling. See [schedulers/README.md](schedulers/README.md).

The `mbligh/calendar` repository now has a preview-first
`build_rugby_calendars` CLI that builds rugby ICS from `pvr.episodes`,
preserves original UID suffixes and frozen history, and migrates existing
WXV calendar events into a separate feed after verifying matching Atlas
fixtures. It never reads media releases. The hourly GitHub Action keeps
the legacy publisher active only until the explicitly generated
`.github/atlas-rugby-active` marker is committed from the Atlas-allowlisted
host. The builder's `--apply --publish --activate` is the one-time
handoff; recurring builds then use `--apply --publish`. The
exporter must be exercised on slagg and its published fixture/UID diff
reviewed before cutover; Atlas IP allowlisting prevents relying on a
GitHub-hosted Actions runner. The scheduler still bootstraps old history
from previously published calendar *data* only when Atlas lacks snapshots,
but no longer imports code from the calendar checkout.

Cancellation status propagation and the WXV fixture/episode/NFO split
are implemented and tested, but must be applied to the production Atlas
database by running the updated rugby scheduler on slagg. The publishing
handoff is deliberately not activated without the allowlisted host's
first fully verified Atlas-derived ICS commit. No remote slagg cron
changes were made by these Git commits. The user has already renamed the Nations directory to Rugby_Mens_Nations, which is now its canonical series ID.

## Single directory-based series identifier (2026-11-21)

All PVR series IDs and calendar slugs now equal the exact case-sensitive media
folder name. Former hyphenated series IDs and F1 episode prefix f1: are
legacy migration inputs only. See SERIES_ID_MIGRATION.md. New code must not
write old-schema data before the migration; no live Atlas changes were made
by these Git commits.

## Six Nations 2027 metadata correction

Both men's and women's Six Nations use calendar-year seasons, unlike
Premiership/PWR. Fixtures from the public ICS sources lack explicit rounds.
The scheduler validates six teams, 15 unique fixtures, and five complete
weeks of three games before assigning round/episode 101..503. Existing
assigned episode numbers are preserved; partial feeds stay unnumbered.
A subsequent import_rugby upsert corrects the 2027 fixtures previously
stored under season 2026 without changing their stable fixture IDs.
The newly created Rugby_Mens_Prem directory is now in the canonical
filing series registry, pending proper Premiership episode numbering.

## Official Gallagher PREM feed migration (2026-11-21)

Rugby_Mens_Prem is now sourced directly from the official InCrowd RugbyViz
match API with three browser-public HTTP headers embedded as defaults and
optional environment overrides. The importer dynamically selects the active
July-to-June season, reconciles official match IDs to the 79 original
spoiler-free ICS calendar UIDs already stored in Atlas, and assigns real
round/episode/NFO metadata for fixtures with confirmed times. API TBC flags
produce date-only fixtures, not speculative kickoff times; unknown playoff
participants are excluded. Previously recorded fixture data remains frozen
at kickoff and historical primary keys are never replaced by prem- keys.
Never ingest scores or results from this API. On annual rollover, preserve
official external IDs and previous round assignments while the active API
changes seasons. Legacy Gallagher_PREM NFO pseudo-series and the public
rugbyfixture.io Premiership ICS input are retired; the calendar repo uses
canonical Rugby_Mens_Prem fixtures from Atlas.

## Rugby historical backfill and Nations round completion (2026-11-21)

The official Nations Championship labels the 27-29 November 2026 Finals
Weekend as round 7. The daily importer now validates every 2026 fixture
week as six uniquely matched northern/southern teams before assigning the
published seven-round layout (July rounds 1-3, November 4-6, Finals 7).
Pre-existing 2026 round 4/5 numbering remains frozen and new round 6/7
episodes use the canonical Rugby_Mens_Nations directory and stable UIDs.

Historical imports are deliberately separate from the daily scheduler:
import_nations_history preview/apply retrieves 18 source fixtures for July
2026 (six in each of rounds 1-3), filters results/score data and inserts
missing episodes/NFO XML without editing prior Atlas records or calendars.
Historical PWR imports support --all-available for 2017-2024 (602 records),
with the 26 source-roundless 2020/21 matches retained as unnumbered
episodes. Historic writes are insert-only after conflict preflight, and the
source provides no usable 2015/16 or 2016/17 fixtures. Both backfill
commands remain opt-in; no unattended cron jobs scrape past outcomes.

## Rugby scheduler package cleanup (2026-11-21)

All active rugby adapters, calendar configuration, spoiler-safe merging
and NFO numbering are directly under schedulers/rugby, alongside
importer.py, pwr_history.py and nations_history.py. The old
schedulers/rugby/legacy directory has been removed. Package-relative
imports replace sys.path manipulation. Original legacy adapter tests
moved to tests/ and participate in the standard CI suite. The
obsolete standalone filter.py and plex_nfo.py entry points were retired.
Root importer CLIs and the data-only bootstrap from published calendar
snapshots remain unchanged. No database schema or fixture IDs changed.


## Formula 1 in-process scheduler source cleanup (2026-11-21)

Both import_f1 and the standalone f1_data CLI now use a common
schedulers.f1_data.fetch_season() function. The F1 episode importer
calls it in-process rather than invoking the root f1_data executable
as a subprocess. It handles public API-key discovery, F1_API_KEY
overrides and validated event/session data retrieval.
Existing --input JSON/standard-input handling, preview/JSONL modes,
Atlas persistence, stable session IDs and both root CLIs remain
unchanged. Errors are caught directly, and tests cover source
dispatch and key selection.


## RugbyPass release-only boundary (2026-11-21)

RugbyPass's catalogue and downloadable video IDs belong solely to release
indexing, replay matching and media downloading. The rugby scheduler no
longer parses RugbyPass replay observations or infers Nations Championship
rounds from recording titles/dates. It uses official competition weeks with
complete team/fixture validation and preserves prior episode assignments.
It does not emit new RugbyPass IDs in fixture manifests or episode metadata.
Existing external_ids.rugbypass values in Atlas remain undisturbed by
scheduler upserts; official Premiership external_ids.prem is independently
updated with dotted field writes. This separates fixture scheduling from
recording discovery without losing historical IDs.


## Consistent fixture-import CLI and safety default (2026-11-21)

The rugby and Formula 1 scheduler entry points now share preview-first
semantics: an invocation without flags validates and previews; --apply
is required to write to Atlas. Both accept --dry-run as an optional
mutually exclusive preview alias (retained for existing rugby callers).
The historical importers already followed this convention.
The rugby GitHub Actions manual import uses --apply and its smoke test
uses the explicit --offline mode. The documented daily slagg cron
command uses --apply; any previously installed bare import_rugby
cron command must be updated separately on the host to avoid silently
becoming a no-write preview. Ordinary rugby previews now require Atlas
read access and use its current snapshots and assignment history. The
explicit --offline flag reads published bootstrap snapshots instead,
without accessing Atlas; it cannot be combined with --apply. Offline
preview is for CI diagnostics, not an accurate simulation of an
Atlas update.
Both default Atlas preview and --apply perform the same read-only
conflict checks; only --apply performs writes.


## Acquisition CLI naming and shared argument conventions (2026-11-21)

acquire_rugby is the rugby acquisition worker; the previous
compatibility executable was retired after the operator updated the
host cron jobs. Both acquire_rugby and acquire_f1 expose --season / --year
interchangeably; rugby additionally supports F1-style --round and
--episode filters in its read-only Atlas planner. Both expose
--dry-run for preview (already the default) and --apply for writes,
and share the common output roots, host, retry, minimum-age,
per-run limit and ffmpeg/ffprobe options. Sport-specific options remain
different where the underlying behavior differs: Rugby supports
multi-provider refresh and a maximum number of video candidates;
F1 supports a recent-session window, backfill and an availability
check budget. No Atlas schemas, downloader behavior, acquired
filenames or cron schedules were changed.


## Separate rugby run orchestrator (2026-11-21)

run_rugby is the one-shot orchestration counterpart of run_f1:
preview-only by default (uses already indexed Atlas releases), and on
--apply first invokes refresh_releases --apply (BBC/YouTube/RugbyPass
discovery, validation, indexing and fixture matching), then invokes
acquire_rugby --apply to download/compare/file. A failed, malformed or
empty discovery response aborts before acquisition; a dedicated host-local
flock serializes the entire discovery/acquisition pipeline, while the
acquisition worker retains its existing shared media lock. --skip-discovery
permits a bounded apply pass against previously indexed releases.
run_rugby exposes acquisition filters/budgets and provider discovery
limits and forwards selected sources consistently to both phases.

acquire_rugby no longer invokes refresh_releases; its --refresh
and discovery pagination CLI options were removed. Existing cron calls using
acquire_rugby --refresh --apply must explicitly switch to
run_rugby --apply. Source filters and downloader settings remain
available on acquire_rugby; no fixture import or schema modification
is part of this change. See ACQUISITION.md.


## Atlas-based recording source polling/backoff (2026-11-22)

The source registry in orchestration/source_registry.py records which
canonical series each indexer can serve. BBC and PWR YouTube serve only
Rugby_Womens_PWR; RugbyPass supports PWR plus Intl/Nations/WXV; teletz
serves Formula_1. Men's Premiership and Six Nations have no recording
indexer, so their fixtures never trigger PWR source requests.

orchestration/polling.py reads finished, numbered, unpublished fixtures,
existing canonical/inventory files and usable linked releases to determine
outstanding discovery demand. F1 uses explicit scheduled_end and rugby
uses explicit end or conservative two-hour kickoff estimate. Demand
intervals depend on fixture age: 5 minutes (<2h), 15 minutes (2-12h),
1h (12-48h), 6h (2-7d), daily (7-30d), weekly (30+d).
Each source also gets a successful check at least once per 24h while
the runner executes. Failures retry after 5,10,20,40, then at most 60m.
Successful empty scans count as success. Atlas indexer_poll_state stores
per-source last_success, last_attempt, failures, retry_not_before.

run_rugby --apply now checks only due sources and considers all supported
rugby series by default; the per-run episode budget spans series.
run_f1 --apply uses the same policy. Both provide --force-discovery
for manual checks and --skip-discovery for acquisition without indexing.
Preview never fetches or writes. Existing acquisition minimum-age,
source-quality, no-overwrite and F1 historical backfill limits remain.
No cron jobs installed; see POLLING.md for suggested five-minute entries.


## Per-site indexer and download locks (2026-11-22)

orchestration/locks.py provides host-local, nonblocking flock locks.
The per-site indexer lock serializes each provider's discovery and
Atlas import; the per-site download lock serializes physical transfers,
source revalidation and file inventory registration. For reddit-teletz
the actual download site is TransferNow. Indexer and downloader locks
do not conflict even for the same source. Separate providers can index
or download independently.

Both run_rugby and run_f1 no longer hold a runner-wide lock across
the download stage. refresh_releases locks each selected source through
discovery and matching (the CLI returns temporary-contention status 75).
run_rugby treats that status as a skip, not a failed indexer check.
run_f1 locks reddit-teletz across discovery and import, rechecking its
polling decision under the lock. Each worker instead holds a hashed
per-episode lock while revalidating plans, acquiring recordings, comparing
candidates and filing. Contention is deferred rather than counted as
a failed download. Lock files are retained and released on process exit.
Jobs must share the same incoming root/host for this coordination.
There is no download watchdog yet, so a genuinely stuck download
continues to hold only its provider and episode locks while all
indexers remain eligible.

## PWR team selections

Official match-day player selections are ingested separately with `import_pwr_lineups` into the `pwr_lineups` collection, keyed by the canonical PWR episode ID. The fixture calendar and NFO remain score-free. See [PWR_LINEUPS.md](PWR_LINEUPS.md) for schema and polling guidance.



## Hourly 24-hour improvement discovery (2026-11-22)

Recording indexers now have explicit Phase A (find first usable release)
and Phase B (look for later higher-quality releases). Phase A retains
age-based adaptive polling for genuinely missing recordings. The first
usable release from any compatible source, or a valid already published
video, moves the event to Phase B. For 24 hours after the event's
expected finish, all compatible sources can be queried at most hourly
due to that event. This includes events whose recordings have already
been filed; a missing fixture with a usable release still remains an
acquisition candidate. Phase B searches never themselves download or
replace canonical recordings. Once 24 hours have elapsed, the event
no longer causes discovery demand, while the unconditional per-source
daily fallback remains. Per-source error backoff and locking are unchanged.
orchestration/polling.py implements this without new Atlas fields.
See POLLING.md.
