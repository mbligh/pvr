# PVR project

## Requirements

- Discover scheduled rugby (including EPCR Investec Champions Cup and official 2026-onward men's and women's Six Nations) and Formula 1 events, and index recordings from supported providers.
- Match releases to canonical events without mixing competitions, and preserve source metadata. For PWR, use an explicit UK fixture date from the video title ahead of later YouTube upload timestamps when unambiguous.
- Acquire selected media, inspect recordings, and file episodes under the established sports directory structure.
- Discover official PWR full-match recordings from both YouTube channel streams and the curated full-match playlist, deduplicating by video ID and avoiding repeated metadata fetches.
- Store event, release, and file metadata in the MongoDB Atlas `pvr` database.
- Run natively on slagg from scheduler/CLI entry points without Docker or a virtual environment.
- Provide a read-only local dashboard for events, installed recordings, and quality.

## High-level implementation

- Command-line entry points (including `import_rugby`, `run_rugby`, `run_f1` and `pvr_dashboard`) drive workflows.
- Rugby acquisition uses one globally newest-first queue across selected series (even when `--season` is set), with one episode budget and per-episode locking/revalidation; `acquire_rugby --all-series` supports direct acquisition without discovery.
- A successful applied Rugby24 refresh resets only the independently scanned category polling state(s), clearing stale Cloudflare failure cooldowns; previews and failed refreshes never reset them.
- `indexers/`, `matchers/`, `orchestration/`, `schedulers/`, `downloaders/`, `inventory/`, and `filing/` separate discovery, matching, acquisition, inspection, and publishing.
- `schedulers/rugby/mens_six_nations.py` parses the official Six Nations 2026+ men's and women's fixture cards and server-component UTC kickoff metadata, requiring 15 fixtures in five complete rounds. It fetches the current and next season, never before 2026, uses competition-specific URL kickoff fallback for completed report cards in every supported year, preserves previously accepted past kickoffs and legacy ICS fixture UIDs, and excludes results for both separate competition series.
- `dashboard/` implements a standard-library HTTP server at port 8787; it reads Atlas and filesystem state, serves its own static CSS, JavaScript and `favicon.ico`, and does not modify recordings. Series navigation has three separate horizontal rows: Formula 1, men's rugby and women's rugby, with horizontal scrolling within rows on narrow screens.
- Deployment is through pulling the Git repository on slagg and restarting the existing dashboard process; no automatic restart is configured.

- RugbyPass WXV Global Series 2026 full-match replay discovery reads dedicated playlist 37547 in addition to featured homepage carousels, using the shared `mbligh/tools/video/rugbypass` CLI. Individual authenticated VOD-detail requests provide actual publication times for unambiguous matching across repeat fixtures; playlist listing alone does not provide dates. The playlist discovery currently covers its first 20 entries. Both repos must be pulled on slagg before applied WXV acquisition.
Detailed behavior and commands are documented in `DASHBOARD.md`, `RUGBY_IMPORT.md`, `INDEXERS.md`, `ACQUISITION.md`, `MATCHING.md`, and `F1_AUTOMATION.md`.

- `import_rugby_crew` enriches existing rugby episodes from World Rugby, PWR and ICDb, with provenance and broadcast-specific commentary. See `RUGBY_CREW.md`.

World Rugby enrichment is rate-limited (sequential, 10 seconds, max 24 pages/run) and persists a 429/403/503 cooldown in Atlas when applying; see `RUGBY_CREW.md`.

- `import_rugby_lineups` applies provider-specific per-run budgets, request spacing, and MongoDB-backed 429/403/503 cooldowns, documented in `RUGBY_LINEUPS.md`.

- The Champions Cup scheduler in `schedulers/rugby/epcr.py` reads EPCR's public RugbyViz fixtures (compId 1008), projects schedule-only fields, numbers rounds 1–8 under `Rugby_Mens_Championship`, and saves official match IDs in Atlas. Current-season completed fixtures are backfilled as Missing; previously accepted kickoffs are frozen. Rugby24 discovery, matching and downloading are separate: the shared Rugby24 indexer covers Premiership and Champions Cup, requires an explicit competition selection (including an explicit `both` for combined scans) from every caller, with strict competition-aware team/date matching, validated same-host HTTP(S) redirects when Rugby24 requires HTTP, a per-category scan and per-category Atlas poll schedule, cached embedded video URLs from existing `pvr.releases` documents, long Cloudflare challenge cooldowns, Debian-packaged `/usr/bin/cf-curl` and `/usr/bin/cfsolve` with packaged browser profiles and cached clearance (Rust solver invoked only on Cloudflare challenges), legacy curl-only fallback and optional local browser cookies, and the existing verified OK.ru downloader. Challenges fail closed when unsolved.

- Before applied polling/acquisition, `run_rugby` and `run_f1` incrementally inventory installed recordings in their supported sports directories. `inventory_files` defaults to all supported rugby/F1 series; identity is series, season and episode number, independent of filename title suffix. It probes new/changed media, reuses unchanged verified metadata, records actual paths in Atlas and creates missing NFOs alongside videos using canonical episode XML, and canonicalizes noncanonical installed filenames within the same season directory using Atlas nfo_path (preserving the real video extension). Existing NFOs move unchanged; missing ones use Atlas XML. No destination is overwritten; same-directory hardlinks and episode locks protect publication, and old paths are retired only after successful Atlas updates. Preview is read-only; explicit `--root` and repeatable `--series` constrain inventory.

- Both senior Six Nations series participate in RugbyPass polling, replay matching and automatic acquisition via `run_rugby`; matching requires explicit competition/gender evidence, exact national teams and a nearby publication timestamp. Age-grade matches are excluded. The existing shared downloader and filing path are reused.
