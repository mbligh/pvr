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

Initial rugby ingestion is in place: `calendar_legacy/` contains independently copied rugby parsers, fixture/ICS spoiler filtering, numbering and NFO generation from `mbligh/calendar`. `import_rugby` seeds historical accepted calendars and prior episode assignments from published snapshots, then upserts all known fixtures (including unnumbered ones) and NFO XML into the Atlas `pvr` database. See [RUGBY_IMPORT.md](RUGBY_IMPORT.md).

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

No automatic source choice, downloader batch scheduling, canonical filing/NFO
publication, or quality upgrade policy is implemented yet. The initial
single-match trial uses an existing 2025/26 YouTube replay linked to S2025E102;
no download has been performed on slagg by these GitHub changes.

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

`acquire_missing` (see [ACQUISITION.md](ACQUISITION.md)) is a one-shot,
preview-first PWR batch worker. It uses canonical Atlas episode metadata,
already-linked YouTube releases, measured local file quality and the existing
download/file tools. Due numbered fixtures are considered only after a
configurable post-kickoff delay (default 3 hours); by default it shows just
one actionable episode and makes no changes. `--apply --limit N` downloads
and files at most N missing episodes, reusing an eligible already-downloaded
recording before contacting the provider. It refuses unattended upgrades of
published recordings, ambiguous multiple remote sources, output collisions,
stale/moved inventory requiring reconciliation and rapid retries of failed
YouTube downloads (default 6-hour cooldown). A nonblocking host-local
`flock` prevents overlapping runs and errors halt the batch. Each download
and filing step retains its independent MongoDB job history.

This is not a complete automatic PVR scheduler: indexers and matchers are
still manual, only the YouTube downloader exists, and no remote quality
prediction, replacement of published recordings, or cleanup is performed.
No cron job was installed as part of the change; users can run it manually
and then schedule it on slagg once its behavior is verified.

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
snapshots into spoiler-free PVR episodes for series formula-1. Pre-season
testing is deliberately excluded. Numbering is round*10 + session offset:
FP1=1, FP2=2, FP3=3, Sprint Qualifying=4, Sprint=5, Qualifying=6,
Race=7. The official session code, not the inconsistent sessionType label,
is authoritative. Only actual sessions are created, leaving sparse numbers.
Stable IDs use f1:<meetingSessionKey>, independent of round and rescheduling.

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
Formula 1 recordings (formula-1 series) using the Atlas episode nfo_path
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
