# PVR project

## Requirements

- Discover scheduled rugby and Formula 1 events, and index recordings from supported providers.
- Match releases to canonical events without mixing competitions, and preserve source metadata.
- Acquire selected media, inspect recordings, and file episodes under the established sports directory structure.
- Store event, release, and file metadata in the MongoDB Atlas `pvr` database.
- Run natively on slagg from scheduler/CLI entry points without Docker or a virtual environment.
- Provide a read-only local dashboard for events, installed recordings, and quality.

## High-level implementation

- Command-line entry points (including `import_rugby`, `run_rugby`, `run_f1` and `pvr_dashboard`) drive workflows.
- `indexers/`, `matchers/`, `orchestration/`, `schedulers/`, `downloaders/`, `inventory/`, and `filing/` separate discovery, matching, acquisition, inspection, and publishing.
- `dashboard/` implements a standard-library HTTP server at port 8787; it reads Atlas and filesystem state, serves its own static CSS, JavaScript and `favicon.ico`, and does not modify recordings.
- Deployment is through pulling the Git repository on slagg and restarting the existing dashboard process; no automatic restart is configured.

Detailed behavior and commands are documented in `DASHBOARD.md`, `RUGBY_IMPORT.md`, `INDEXERS.md`, `ACQUISITION.md`, `MATCHING.md`, and `F1_AUTOMATION.md`.

- `import_rugby_crew` enriches existing rugby episodes from World Rugby, PWR and ICDb, with provenance and broadcast-specific commentary. See `RUGBY_CREW.md`.
