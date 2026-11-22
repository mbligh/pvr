# Scheduler boundary

The `schedulers/` package owns **what should exist** in the PVR, not
**where to find recordings** or **how to download them**.

```text
schedulers/
  rugby/
    importer.py           current rugby calendars -> pvr.episodes / NFOs
    pwr_history.py        historical PWR fixture import
    legacy/               vendored source adapters and spoiler-free numbering
      calendars.json
      filter.py
      plex_nfo.py
      pwr.py
      world_rugby.py
      prem.py
  f1.py                   Formula 1 sessions -> pvr.episodes / NFOs
  f1_data.py              fetch official F1 event/session schedules
indexers/                 discover obtainable *recordings*
downloaders/              download and verify recordings
matchers/                 link discovered releases to scheduled episodes
orchestration/            bounded acquisition workflows
```

The root executable names are compatibility wrappers:

- `python3 import_rugby` delegates to `schedulers.rugby.importer`.
- `python3 import_pwr_history` delegates to `schedulers.rugby.pwr_history`.
- `python3 import_f1` delegates to `schedulers.f1`.
- `python3 f1_data` delegates to `schedulers.f1_data`.

Existing cron commands and CLI arguments remain unchanged. The former
`calendar_legacy/` files have been moved (not duplicated) into
`schedulers/rugby/legacy/` so there is only one active copy of each module.

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

**WXV:** the scheduler now partitions previously accepted, spoiler-free
World Rugby women's fixtures into a distinct `rugby-womens-wxv` feed,
with independent WXV week-based episode numbering. On its first live
`import_rugby` run, Atlas receives canonical WXV episodes and NFOs;
legacy WXV records in `rugby-womens-intl` become `status=superseded`,
while any associated release/job IDs migrate. Media-inventory conflicts
abort the migration rather than silently moving recordings. The calendar
publisher `mbligh/calendar/build_rugby_calendars` moves frozen WXV
calendar events to the new feed with their original UIDs.

The named `Rugby_Nations` directory and stable `rugby-nations`
series identifier remain unchanged pending a separately coordinated
rename to `Rugby_Mens_Nations`.
