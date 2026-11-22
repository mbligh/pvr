# Formula 1 automatic acquisition

Two commands:

- ./acquire_f1: preview or explicitly acquire and file F1 sessions from
  already indexed teletz releases. No discovery or source URLs accessed in
  preview mode.
- ./run_f1: one-shot cron wrapper. With --apply it uses Atlas
  polling state to decide when to check Reddit/teletz (age-based
  intervals, at least daily), imports new/edited releases if present,
  and acquires eligible recent missing sessions. Discovery/importer
  failure aborts acquisition. See POLLING.md.

## Try on slagg

    cd ~/pvr
    git pull
    ./acquire_f1 --year 2026 --limit 3 --show-skipped
    ./run_f1

After checking the plan, run one recent acquisition:

    ./run_f1 --apply --pages 3 --limit 1 --max-checks 8

The acquisition worker accepts both --season and --year for
the season filter, matching acquire_rugby. Both workers share
--round, --episode, --limit, --dry-run and --apply; F1 uses
an explicit recent-session window and availability-check budget
instead of rugby's optional refresh and candidate-comparison budget.

For historical recordings, explicitly opt into backfill:

    ./acquire_f1 --year 2026 --backfill --limit 1
    ./acquire_f1 --year 2026 --backfill --limit 1 --apply

Defaults: --limit 1 (at most one successfully filed session),
--max-checks 8 (at most eight live TransferNow checks per run),
--recent-hours 120 (sessions ending within five days),
--min-age-hours 2 (after the scheduled session end),
--retry-after-hours 6 and --unavailable-recheck-hours 24.
Unknown session-end times, future and cancelled events are excluded.
Missing releases and sessions with existing recordings are skipped.

## Selection and fallback

The ranking is dedicated Sky first, MULTi with English Sky second, then
F1TV and other commentary. The worker checks links in preference order.

- Positive unavailable evidence (HTTP 404/410, EXPIRED/DISABLED/DELETED
  TransferNow status, or no downloadable files) permits trying the next
  candidate. Multi-file transfers, non-video formats and password-only
  transfers are unsupported and also allow fallback.
- Ambiguous HTTP errors such as 403, 429, 500, timeouts and server problems
  are NOT evidence of expiry. They stop fallback for this episode.
- After a successful link check, the existing download_release command
  downloads one selected release to /data/incoming, with persistent
  resumable partials. The existing file_recording command then files it
  into Formula_1/Season_YYYY with the canonical Atlas NFO.
- Download/remux/file failures never fall back to a different source. The
  interrupted transfer and Atlas job history remain available for retry or
  reconciliation. Registered incoming recordings are filed before new
  downloads. A published file prevents reacquisition or overwrites.

Check results live in pvr.releases.availability_check (status, checked_at,
and limited diagnostics). This is an operational field, separate from the
indexer's metadata and preserved across subsequent Reddit imports.
Definitively unavailable/unsupported statuses are rechecked after 24h,
and transient check failures after 6h. Recent failed/running download jobs
also impose a six-hour retry cooldown. The worker does not permanently
mark releases available=false based on these checks.

An availability-only attempt does not consume the one-success download
limit, so an expired newest session cannot starve older missing sessions.
The separate eight-check network budget bounds work. Indexer and downloader locks are separate and scoped by source/site.
The reddit-teletz indexer lock spans discovery and Atlas import;
the TransferNow download lock never prevents another Reddit indexer
check. An additional per-episode reservation prevents duplicate
filing across overlapping runs. See POLLING.md.

## Optional cron on slagg

No crontab is installed or activated by GitHub. Once the manual --apply
test passes, call the runner every five minutes; source checks only run
when due under the Atlas-backed polling policy:

    */5 * * * * cd /home/mbligh/pvr && ./run_f1 --apply --pages 3 --limit 1 --max-checks 8 >> /home/mbligh/.log/pvr-f1 2>&1

A recent missing session permits five-minute polling; an old missing
session does not. --force-discovery manually bypasses the successful
poll cooldown. See POLLING.md.

See the per-run summaries with:

    tail -100 ~/.log/pvr-f1

Import the official Formula 1 episode schedule separately each year;
this runner indexes releases, not new episodes. It never upgrades an
existing published recording automatically. Use the --backfill option
manually for older events beyond the five-day cron window.
