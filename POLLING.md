# Adaptive recording-source polling

run_rugby and run_f1 are one-shot cron-compatible orchestrators. Running them
every five minutes is safe: Atlas determines whether a source indexer should
run. They do not install cron jobs or update official fixture schedules
(import_rugby and import_f1 do that separately).

## Source coverage

The source registry is orchestration/source_registry.py:

| Indexer | Eligible episode series |
| --- | --- |
| BBC iPlayer PWR | Rugby_Womens_PWR |
| YouTube PWR | Rugby_Womens_PWR |
| RugbyPass | Rugby_Womens_PWR, Rugby_Mens_Intl, Rugby_Womens_Intl, Rugby_Mens_Nations, Rugby_Womens_WXV |
| Reddit/teletz | Formula_1 |

Men's Premiership, men's Six Nations and women's Six Nations have
**no supported recording indexer**. These fixtures never trigger
the BBC/YouTube PWR indexers. Multiple missing fixtures on one source
result in one provider check, not one check per fixture.

## Eligibility and polling

Only finished, numbered, non-cancelled fixtures with valid canonical filing
metadata can cause event-specific discovery. F1 uses the explicit
scheduled_end; rugby uses scheduled_end or kickoff plus two hours (or
known duration). These are expected end times, not proof the recording
has been published.

There are two distinct phases:

**Phase A — Find the first usable release.** While no compatible source has
a usable release and no recording is published, missing fixtures use age-based
backoff:

| Age after expected finish | Polling interval if still missing |
| --- | --- |
| Less than 2 hours | 5 minutes |
| 2–12 hours | 15 minutes |
| 12–48 hours | 1 hour |
| 2–7 days | 6 hours |
| 7–30 days | 24 hours |
| 30+ days | 7 days |

**Phase B — Look for improvements.** As soon as **any** compatible indexer
has a usable matched release, or a valid recorded video has already been
published to the library, all relevant indexers switch to **hourly checks**.
Phase B is limited to **24 hours after the expected event finish**, not
24 hours after finding the first release. This is a maximum event-driven
polling rate per indexer: another episode still in Phase A may cause
a shared indexer to run sooner. An indexer run for Phase B is never
triggered less than an hour after that source's last successful check
by Phase B alone.

Published videos still get Phase B searches, but are never acquisition
candidates. Existing canonical media cannot be overwritten by these runs;
an improved release may be discovered and indexed but **will not be
downloaded or replace a published recording automatically**. Abnormal
partial publications (e.g. an NFO without its video) require reconciliation
instead of causing improvement checks.

After Phase B expires, the event no longer causes event-driven polling.
Each indexer still has a **24-hour successful-check fallback**, even if
there are no missing fixtures at all. The earlier of event demand and this
fallback determines the next check. A two-week-old missing event never
causes five-minute polling by itself.

After failed discovery/indexing, retries back off 5, 10, 20, 40,
then at most 60 minutes until success. A successful scan with zero new
recordings is still success. Failure does not change last-success time.
State is stored in MongoDB Atlas pvr.indexer_poll_state, one document per
source with last_success, last_attempt, failures and (for failures)
retry_not_before. The first successful check creates its state; no
schema migration is necessary.

Provider operations use distinct host-local advisory locks under
/data/incoming (or the configured incoming root):

- .pvr-indexer-<source>.lock serializes an individual provider's discovery
  and matching. BBC, YouTube, RugbyPass and reddit-teletz have independent
  locks; F1's Reddit lock spans both discovery and Atlas import.
- .pvr-download-<site>.lock serializes physical transfers and inventory
  registration by website. BBC/YouTube/RugbyPass use their source names;
  reddit-teletz recordings download from TransferNow and use its lock.
- .pvr-episode-<hash>.lock reserves a canonical episode across download,
  quality comparison and filing. Different providers cannot independently
  publish the same episode; once locked, the worker refreshes its plan.

There is **no global runner/acquisition lock**: a stalled download never
blocks indexers, and unrelated providers or episodes can continue.
Contention is nonblocking: a busy indexer is skipped without recording
a provider failure, and busy acquisition is deferred rather than marked
failed. Indexer and downloader locks for the *same* provider do not block
one another.

These are host-local flock locks: they coordinate jobs using the same local
incoming filesystem, not processes on separate hosts. Lock files remain
on disk after unlocking to avoid inode-replacement races. A stuck process
retains its individual lock until it exits, so a separate download watchdog
may still be desirable.

## Commands

```sh
cd ~/pvr
git pull --ff-only

# Read-only polling status and existing indexed releases
./run_rugby
./run_f1

# Check only due indexers, then attempt bounded acquisition
./run_rugby --apply --limit 2
./run_f1 --apply --limit 1

# Manual override to force a check regardless of successful cooldown
./run_rugby --apply --force-discovery
./run_f1 --apply --force-discovery

# Use already indexed recordings without source discovery
./run_rugby --apply --skip-discovery
./run_f1 --apply --skip-discovery
```

--force-discovery and --skip-discovery are incompatible. Without --apply,
runners never index, update poll state, or download media. Their JSON
type=polling output shows sources, next check times, and separate
missing (Phase A) and improving (Phase B) event counts. The runner's
acquisition candidate list still includes missing files with usable
linked releases, even when the indexer is in Phase B. Atlas access is required in both preview and apply mode.

run_rugby checks all five currently supported rugby series by default,
rather than PWR only. --series limits manual work to one series.
--limit is shared across all selected rugby series. Recent missing
events are considered first, irrespective of series.

## Five-minute cron example

```cron
*/5 * * * * cd /home/mbligh/pvr && ./run_rugby --apply --limit 2 >> /home/mbligh/.log/pvr-rugby 2>&1
*/5 * * * * cd /home/mbligh/pvr && ./run_f1 --apply --limit 1 >> /home/mbligh/.log/pvr-f1 2>&1
```

Ensure log directories exist. Replace old cron entries rather than
adding a duplicate schedule. GitHub does not install cron jobs.
Provider checks follow the adaptive policy, not the cron frequency.

Acquisition eligibility remains independent: acquire_rugby defaults to
at least three hours after kickoff, and acquire_f1 defaults to two hours
after a session ends. Early indexing therefore does not remove these
age gates. Older F1 recordings beyond the five-day acquisition window
still need manual ./acquire_f1 --backfill operation. Every source continues
daily fallback polling, even if no sessions are in the acquisition window.
