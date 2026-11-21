# PVR missing-recording acquisition (first batch worker)

`acquire_missing` is a bounded, preview-first, **one-shot** worker for PWR
recordings already catalogued in Atlas. It connects the existing episode
import, release matcher, local file selector, YouTube downloader and
canonical filer. The worker does not re-index any provider, guess missing
fixtures, download BBC/RugbyPass recordings, replace a published file or
clean up unselected media.

## Usage on slagg

```sh
cd ~/pvr
git pull

# Read-only plan: recent missing fixtures first, one action by default.
python3 acquire_missing

# Preview the old 2025/26 season, oldest first. No changes.
python3 acquire_missing --season 2025 --limit 5

# Explicit single-episode test: downloads, verifies, chooses and files.
python3 acquire_missing --season 2025 --limit 1 --apply

# Only after verifying the first complete run, process up to five episodes.
python3 acquire_missing --season 2025 --limit 5 --apply
```

Each actionable preview emits a JSONL row with `action=file` for an eligible
existing incoming MKV, or `action=download_then_file` for a known linked
YouTube release. The summary goes to stderr. Use `--show-skipped` to see
why a due episode was excluded, including existing publications, unavailable
recordings, recent failures, multiple unknown-quality remote releases,
missing valid NFO metadata or destination collisions.

Default `--limit 1` means **at most one episode**, not one source request
or one hour. The season filter is optional: without it, the worker considers
the newest past matches first. With `--season 2025`, it walks that season
in chronological order. No cron configuration is installed by this change.

## Eligibility and selection

For each due, numbered PWR fixture with valid canonical NFO, the worker:

1. Waits at least `--min-age-hours 3` after the fixture's scheduled
   kickoff and excludes cancelled fixtures.
2. Skips episodes with any existing canonical video/NFO destination or
   an already-published inventory file under the sports library. Even a
   library file with stale inventory metadata blocks unattended replacement.
3. Checks all locally inventoried files on the current host. Files need
   unchanged size/mtime and ffprobe metadata; reject below **720p** or
   under **one hour**. Choose the largest eligible file at **>=50fps**,
   otherwise the largest eligible file below 50fps.
4. If an eligible local recording is found, files that preferred MKV
   through `file_recording`; it does not download another release.
5. Otherwise, considers **only already-matched and finished YouTube
   releases** in `pvr.releases`. It declines ambiguous multiple
   unmatched-for-quality releases rather than guessing the best remote
   video; currently, Atlas has one matched YouTube release for each of
   65 PWR episodes.
6. Excludes a release with known duration below 3,600 seconds, already
   inventoried source URI, a claimed destination, or a recent failure.
   Remote resolution/FPS/file size are not reliably known before download.
7. Downloads a unique candidate into `/data/incoming/pvr-<id>.mkv`
   with `download_release`, verifies the result, and invokes
   `file_recording`, which rechecks Atlas quality ranking before
   publishing. A downloaded but ineligible or non-preferred file is
   **left intact in incoming**, with a visible skipped filing status.

New media and sidecars are published with the existing no-clobber behavior;
source deletion only occurs after successful filing. There is no
automatic media replacement or cleanup. The existing `pvr.jobs` and
`pvr.files` records retain detailed download/filing provenance.

## Retries and safety

- An exclusive lock file `/data/incoming/.pvr-acquire.lock` prevents
  overlapping `--apply` runs on the same incoming filesystem.
- Recent failed/running download jobs suppress another attempt for
  `--retry-after-hours 6` by default; use `--retry-after-hours 0`
  for an explicit immediate retry after diagnosing the problem.
- Any exception during downloading or filing halts the current batch.
  The summary indicates `failed > 0` and the CLI exits nonzero.
  The potentially completed incoming file or publication is preserved
  for reconciliation; no automated rollback or redownload is attempted.
- `--limit` can be 1..100; use a small limit initially. Preview is
  safe to run without any lock because it never changes filesystem/Atlas.
- Source URI/episode identity checks, ffprobe verification, exact NFO
  publication, and no-overwrite behavior are inherited from the existing
  standalone tools. A failed download can be examined in `pvr.jobs`.

The worker needs a local `~/.mongodb/pvr` or `MONGODB_URI`, local
`yt-dlp`, `ffmpeg` and `ffprobe`, and pre-existing
`/data/incoming` and `/data/sports` directories. Symlinked
`/data/sports` is supported.

## Scheduling

The worker is a **one-shot command** suitable for cron on slagg after a
successful manual trial. For example, to process at most two missing
fixtures daily:

```cron
30 5 * * * cd /home/mbligh/pvr && /usr/bin/python3 acquire_missing --limit 2 --apply >> /home/mbligh/pvr-acquire.log 2>&1
```

This is an example only; the repository does not install or change crontabs.
It uses the local configured timezone of the host. Run more conservative
manually-scoped season backfills before enabling unattended scheduling.

## Deliberately not implemented yet

- Fetching/refreshing external provider listings or automatic matcher runs.
- BBC iPlayer and RugbyPass download adapters.
- Quality prediction/selection across multiple unknown remote releases.
- Explicit upgrade/replacement of an existing published recording.
- Removal of unused alternatives.
- Cross-machine distributed acquisition locking.
- Automatic recovery of partial Atlas writes or stale source inventory.

Review these restrictions before raising batch limits or putting the worker
into cron.
