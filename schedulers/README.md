# Scheduler boundary

The `schedulers/` package owns **what should exist** in the PVR, not
**where to find recordings** or **how to download them**.

```text
schedulers/
  rugby/
    importer.py           current rugby calendars -> pvr.episodes / NFOs
    pwr_history.py        historical PWR fixture import (preview-first)
    nations_history.py    July 2026 Nations historical import (preview-first)
    nations.py            verified Nations Championship rounds 1-7
    six_nations.py        validate complete tournament round numbering
    calendars.json         competition/source configuration
    filter.py              spoiler-free calendar merge
    plex_nfo.py            round numbering and NFO generation
    pwr.py                 official women's PWR fixture API
    world_rugby.py         official World Rugby fixture API
    prem.py                official Gallagher PREM fixture API
    prem_identity.py       preserve existing PREM match identifiers
    wxv.py                 womens international/WXV partitioning
  f1.py                   Formula 1 sessions -> pvr.episodes / NFOs
  f1_data.py              fetch official F1 event/session schedules
indexers/                 discover obtainable *recordings*
downloaders/              download and verify recordings
matchers/                 link discovered releases to scheduled episodes
orchestration/            bounded acquisition workflows
```

The root executable names are compatibility wrappers:

- `./import_rugby` delegates to `schedulers.rugby.importer`.
- `./import_pwr_history` delegates to `schedulers.rugby.pwr_history`.
- `./import_f1` delegates to `schedulers.f1`.
- `./f1_data` delegates to `schedulers.f1_data`.

All fixture import entry points (./import_rugby, ./import_f1,
./import_pwr_history, and ./import_nations_history) default to
preview mode. Explicit --apply is required for Atlas writes. Rugby and
F1 also accept the redundant --dry-run compatibility flag, but not
together with --apply. Rugby preview reads current Atlas snapshots and
assignment history, and performs read-only conflict checks. Explicit
--offline instead uses the published bootstrap snapshots without connecting
to Atlas (e.g. for GitHub Actions smoke tests). It cannot be combined
with --apply and may not reflect current Atlas state. Older rugby cron
commands that omitted --apply must be updated to include it.

The former
`calendar_legacy/` files and adapters now live directly in
`schedulers/rugby/` as normal package modules. The legacy subdirectory
is removed. The original adapter tests now run with the shared suite in
tests/; no separate PYTHONPATH is required.

**Atlas ownership:** schedulers write canonical `pvr.episodes`, NFO XML,
stable event identities and saved calendar-source snapshots/assignment
state. They do not write `pvr.releases`, `pvr.files`, or acquisition jobs.
Indexers discover releases; `index_releases` and matchers handle their
database persistence and association separately. The teletz importer
belongs with release ingestion, not with fixture scheduling.

**Calendar publisher target:** Eventually `mbligh/calendar` should consume
`pvr.episodes` as the authoritative rugby fixture metadata and publish
score-free ICS files. The existing published calendar UIDs must survive the
migration; historical events must not be rewritten with post-kickoff
source data. For now the calendar GitHub Actions publication workflow remains
unchanged. The current PVR importer can still read published initial
historical snapshots only when no Atlas state exists.

**WXV:** the World Rugby API's `WXV Global Series` label maps to the existing
`WXV` competition (including in new fixture descriptions and NFO plots).
The separate Challenger competition is excluded, while historic WXV fixtures
are still recognized; no series, folder or calendar renaming is required.
The scheduler now partitions previously accepted, spoiler-free
World Rugby women's fixtures into a distinct `Rugby_Womens_WXV` feed,
with independent WXV week-based episode numbering. On its first live
`import_rugby` run, Atlas receives canonical WXV episodes and NFOs;
legacy WXV records in `Rugby_Womens_Intl` become `status=superseded`,
while any associated release/job IDs migrate. Media-inventory conflicts
abort the migration rather than silently moving recordings. The calendar
publisher `mbligh/calendar/build_rugby_calendars` moves frozen WXV
calendar events to the new feed with their original UIDs.

The Nations directory has already been renamed to Rugby_Mens_Nations.
That exact spelling is the series ID. All rugby slugs equal media folder names;
old lowercase spellings survive only as migration inputs and URL aliases.

Six Nations seasons follow calendar years for both men and women. Complete
15-match fixtures are validated as five rounds of three matches, one game
per team each round, before assigning 101..503. Missing or ambiguous
fixtures remain unnumbered. This fixes 2027 fixtures previously imported
as season 2026. The Rugby_Mens_Prem directory is also registered in
filing; confirmed kickoff fixtures now have numbered NFO metadata.

PREM (men's): the official public RugbyViz API is the only active source
for Rugby_Mens_Prem, using public website client headers in prem.py.
The PVR importer reconciles official match IDs against historical stored
ICS identities, preserves pre-kickoff freezes, and adds per-round numbering.
Time-TBC fixtures remain date-only. No separate Gallagher_PREM series is
created, and the public Premiership ICS service is no longer queried.

The daily import_rugby now verifies 2026 Nations Championship round 6 and
Finals Weekend round 7 using official competition match windows and full
12-team, 6-match cross-hemisphere coverage checks. It preserves accepted
round 4/5 assignments. July rounds 1-3 must be imported separately with
./import_nations_history --apply; this is intentionally NOT a recurring
post-kickoff scrape. The first eight PWR seasons with public historical
fixtures (2017/18 through 2024/25) can be imported once with
./import_pwr_history --all-available --apply. PWR 2020/21 has 26
fixtures without source rounds and retains them with null episode numbers,
rather than silently omitting or guessing.
