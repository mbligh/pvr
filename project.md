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

The GitHub Actions workflow runs offline regression tests and a snapshot-bootstrap smoke test; Atlas ingestion is manual-only until a restricted `MONGODB_URI` secret and allowlisted network egress are configured. The original `mbligh/calendar` and `mbligh/tools` workflows remain unchanged. Google Calendar publishing from Atlas, F1, and release/download handling are not yet migrated.

## Initial implementation and migration

1. Define/version the canonical episode, release, and adapter JSON schemas; implement MongoDB collections/indexes and a small CLI.
2. Import current `mbligh/calendar` fixture metadata, NFOs, and calendar mappings into Atlas; retain existing calendar publication/manifest during transition.
3. Wrap existing RugbyPass, BBC iPlayer, and YouTube discovery/download tools behind indexer/downloader CLIs; consolidate the current PWR filing logic into the generic filer.
4. Add event-driven polling, release matching, quality selection, and repeatable acquisition jobs.
5. Move Google Calendar generation/sync to the shared database and retire redundant calendar jobs only after output and event IDs are verified.
6. Add the web UI once the stored state and CLIs are reliable.

Keep this document updated as the design and implementation evolve.
