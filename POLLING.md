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

Only finished, numbered, non-cancelled, unfiled fixtures with valid filing
metadata cause event-specific discovery. Already linked, usable releases
from a source remove discovery demand for that source (but not other
sources that might offer competing recordings). Existing published
files or moved/inconsistent publication history prevent futile polling.
F1 uses explicit scheduled_end; rugby uses scheduled_end or kickoff plus
two hours (or known duration). This is an estimate, not confirmation
that a video is ready.

| Age after expected finish | Polling interval if still missing |
| --- | --- |
| Less than 2 hours | 5 minutes |
| 2–12 hours | 15 minutes |
| 12–48 hours | 1 hour |
| 2–7 days | 6 hours |
| 7–30 days | 24 hours |
| 30+ days | 7 days |

Every source has a **24-hour successful-check fallback**, even when
nothing is missing. Whichever is sooner (event demand or daily fallback)
governs the next check. An event missing for two weeks therefore requires
only daily polling, not five-minute polling. A newly finished event can
immediately shorten the interval even when older events were on the daily
fallback cadence.

After failed discovery/indexing, retries back off 5, 10, 20, 40,
then at most 60 minutes until success. A successful scan with zero new
recordings is still success. Failure does not change last-success time.
State is stored in MongoDB Atlas pvr.indexer_poll_state, one document per
source with last_success, last_attempt, failures and (for failures)
retry_not_before. The first successful check creates its state; no
schema migration is necessary.

Both runners use their existing local flock locks; acquisition workers
additionally use the existing .pvr-acquire.lock for downloaded media.
Scheduling the same runner every five minutes cannot overlap its
own discovery/acquisition pipeline on the same host.

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
type=polling output shows sources, next check times and missing-event
counts. Atlas access is required in both preview and apply mode.

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
