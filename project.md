# PVR project

## Requirements

- Discover scheduled rugby (including EPCR Investec Champions Cup) and Formula 1 events, and index recordings from supported providers.
- Match releases to canonical events without mixing competitions, and preserve source metadata.
- Acquire selected media, inspect recordings, and file episodes under the established sports directory structure.
- Discover official PWR full-match recordings from both YouTube channel streams and the curated full-match playlist, deduplicating by video ID and avoiding repeated metadata fetches.
- Store event, release, and file metadata in the MongoDB Atlas `pvr` database.
- Run natively on slagg from scheduler/CLI entry points without Docker or a virtual environment.
- Provide a read-only local dashboard for events, installed recordings, and quality.

## High-level implementation

- Command-line entry points (including `import_rugby`, `run_rugby`, `run_f1` and `pvr_dashboard`) drive workflows.
- `indexers/`, `matchers/`, `orchestration/`, `schedulers/`, `downloaders/`, `inventory/`, and `filing/` separate discovery, matching, acquisition, inspection, and publishing.
- `dashboard/` implements a standard-library HTTP server at port 8787; it reads Atlas and filesystem state, serves its own static CSS, JavaScript and `favicon.ico`, and does not modify recordings. Series navigation has three separate horizontal rows: Formula 1, men's rugby and women's rugby, with horizontal scrolling within rows on narrow screens.
- Deployment is through pulling the Git repository on slagg and restarting the existing dashboard process; no automatic restart is configured.

Detailed behavior and commands are documented in `DASHBOARD.md`, `RUGBY_IMPORT.md`, `INDEXERS.md`, `ACQUISITION.md`, `MATCHING.md`, and `F1_AUTOMATION.md`.

- `import_rugby_crew` enriches existing rugby episodes from World Rugby, PWR and ICDb, with provenance and broadcast-specific commentary. See `RUGBY_CREW.md`.

World Rugby enrichment is rate-limited (sequential, 10 seconds, max 24 pages/run) and persists a 429/403/503 cooldown in Atlas when applying; see `RUGBY_CREW.md`.

- `import_rugby_lineups` applies provider-specific per-run budgets, request spacing, and MongoDB-backed 429/403/503 cooldowns, documented in `RUGBY_LINEUPS.md`.

- The Champions Cup scheduler in `schedulers/rugby/epcr.py` reads EPCR's public RugbyViz fixtures (compId 1008), projects schedule-only fields, numbers rounds 1–8 under `Rugby_Mens_Championship`, and saves official match IDs in Atlas. Current-season completed fixtures are backfilled as Missing; previously accepted kickoffs are frozen. Rugby24 discovery, matching and downloading are separate: the Rugby24 indexer covers both Premiership and Champions Cup, with strict competition-aware team/date matching and the existing verified OK.ru downloader.
