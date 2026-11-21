# PVR PWR source refresh and missing-recording acquisition

Two independent preview-first tools work together:

- `refresh_releases` discovers current **BBC iPlayer**, **YouTube PWR**
  and **RugbyPass** catalogue entries, validates their stable IDs and
  matches them to known PWR fixtures in Atlas. Preview performs no writes;
  `--apply` imports releases and links unambiguous matches.
- `acquire_missing` finds due PWR episodes that lack a published recording,
  downloads the needed source recordings, evaluates their **actual** video
  properties, and canonically files the winner with the NFO stored in Atlas.
  It is limited to one episode per run unless `--limit` is increased.

## Getting started on slagg

```sh
cd ~/pvr
git pull

# Inspect provider catalogue sizes without modifying Atlas:
python3 refresh_releases

# Refresh the Atlas source catalogue and link unambiguous matches:
python3 refresh_releases --apply

# Preview acquisition (may show several source downloads for one episode):
python3 acquire_missing --limit 3

# Run a bounded single-episode trial across available sources:
python3 acquire_missing --limit 1 --apply
```

To combine discovery, matching and acquisition in one invocation:

```sh
python3 acquire_missing --refresh --limit 1 --apply
```

Without `--apply`, `--refresh` queries providers but **does not import newly
discovered releases into Atlas**. The subsequent acquisition preview can
therefore include only releases already linked in the database. For a fully
accurate plan including newly discovered sources, run
`refresh_releases --apply` first, then preview `acquire_missing`.

Continue the historical **YouTube-only** backfill without involving new
providers:

```sh
python3 acquire_missing --season 2025 --source youtube --limit 10 --apply
```

`--source` is repeatable, e.g. `--source bbc --source youtube`. The
acquisition worker uses the same provider filter for optional refresh and
for choosing source candidates.

## Complete selection policy

For each scheduled, numbered PWR fixture older than three hours (configurable),
the worker:

1. Checks the canonical episode NFO/filename from Atlas; skips cancelled
   matches or inconsistent metadata.
2. If there is an existing canonical MKV/NFO, an actual published file
   registered under `/data/sports`, or a previous move that needs
   reconciliation, **does not acquire another version**.
3. Evaluates existing local incoming MKVs using `pvr.files` and actual
   filesystem information. Rejects files smaller than 720p, shorter than
   one hour, with unknown FPS, or whose inventory is stale.
4. Finds the **remaining** finished, matched BBC/YouTube/RugbyPass releases.
   A source URI already inventoried is not downloaded again, even if the
   recording is below minimum quality; untracked source files are not
   overwritten. Known short (<3600s) remote releases are excluded.
5. If more than one provider is available, **downloads all missing
   candidates within `--max-candidates` (default 3)**. Only after those
   downloads does it measure actual duration, FPS, resolution and bytes.
   It cannot reliably rank catalogue-only video quality before download.
6. Applies the established quality rule: >=720p, duration >=3600s;
   largest file among recordings at >=50fps, otherwise largest lower-FPS
   recording. 60fps does not automatically beat 50fps; there is no BBC
   preference except when BBC wins on measured quality.
7. Uses `file_recording` to publish only that selected source, with
   atomic no-clobber MKV and exact canonical NFO.
8. Leaves all unselected recordings in `/data/incoming`; there is no
   automatic deletion or upgrade of already-published files.

For a single missing release, the familiar preview action is
`download_then_file`. For multiple releases it is
`download_candidates_then_file` with an ordered `downloads` array.
`action=file` means an eligible local recording can be published without
downloading anything. The ordering BBC, YouTube, RugbyPass is only a stable
transfer order, **not** a quality preference.

`--max-candidates` counts the best existing local recording plus all new
remote candidates and is limited to 1..5. If the limit is exceeded the
episode is skipped as `too_many_source_candidates` instead of choosing
arbitrarily or silently ignoring a provider. `--limit` counts episodes,
not individual video downloads: one episode may require up to three
large transfers at the default candidate limit.

## Source adapters

- **BBC**: `downloaders/bbc.py` calls `get_iplayer --pid` with an isolated
  per-download profile and an explicit temporary output directory. It asks
  for `fhd,hd` quality and avoids the host's existing `get_iplayer`
  command hooks, PVR history and media filing. The resulting MP4 is remuxed
  to MKV by FFmpeg, then checked by ffprobe. BBC availability can change,
  and downloads may be subject to region and programme restrictions.
- **YouTube**: existing `yt-dlp-sabr` configuration is respected; the
  adapter stages and probes a completed MKV.
- **RugbyPass**: the existing `~/tools/video/rugbypass` tool performs
  authenticated single-VOD downloads to an explicit temporary MP4 with
  `--no-fixture-nfo`; the adapter remuxes and probes it. The existing
  private RugbyPass auth profile must be functional. The source indexer
  reads accessible full-match replays from featured sections; it is not
  a complete historical catalogue.

Both new adapters use one temporary folder next to the requested incoming
MKV. Verified publication uses an atomic no-overwrite hard link. The canonical
library move may involve a verified cross-filesystem copy. They never write
an NFO themselves: the authoritative NFO comes from MongoDB at filing time.

Direct single-source trials (always preview first):

```sh
python3 download_release bbc:m0034hxp --destination /data/incoming/pvr-bbc-m0034hxp.mkv
python3 download_release bbc:m0034hxp --destination /data/incoming/pvr-bbc-m0034hxp.mkv --apply
```

Note: the BBC example is a **previously matched episode that is already
published** on slagg. The default download command will report
`already_owned` and skip it; this is expected. The explicit
`--allow-existing-episode` option would permit an alternative recording
without replacing the published copy.

## Safety and recovery

- **Preview first**; `--apply` is required for Atlas updates and media.
- The `/data/incoming/.pvr-acquire.lock` host-local flock prevents
  overlapping worker invocations.
- A recent failed or running download prevents immediate provider retries
  for six hours by default (`--retry-after-hours`). Explicitly set the
  value to 0 only after diagnosing a failure.
- If any candidate fails or is unexpectedly skipped during an episode's
  multi-source acquisition, the batch **stops without filing a winner**,
  leaving complete downloaded candidates intact for reconciliation and
  later retry. The CLI reports a failure and returns nonzero.
- Media failing quality checks is retained for investigation, not
  published or deleted. Missing FFprobe information is not guessed.
- Stale/duplicate records, untracked destination files, and partial
  previous moves are hard stops; existing published media is never
  overwritten.
- `pvr.jobs` tracks each provider download and canonical filing, and
  `pvr.files` tracks all local candidates/provenance.

## Scheduling

The command is a **one-shot** cron-compatible worker; no cron job is
installed by the repository. Once manual trials are successful and all
three providers work on slagg, a conservative unattended invocation is:

```cron
30 5 * * * cd /home/mbligh/pvr && /usr/bin/python3 acquire_missing --refresh --limit 2 --apply >> /home/mbligh/pvr-acquire.log 2>&1
```

The sample is not installed or enabled. A refresh fetches the BBC brand,
up to 100 YouTube stream entries and up to five RugbyPass catalogue pages
per section by default. Override with `--youtube-limit`,
`--bbc-pages`, `--rugbypass-pages` and `--rugbypass`. Avoid very
frequent full-catalogue refreshes or unbounded historical backfills.

## Not implemented yet

- Acquisition from unrelated TV series, men's rugby or Formula 1 through
  this *PWR-specific* worker (other separate tools may handle those).
- Predictive source quality ranking without downloading the media.
- Replacing a published recording with a newly discovered better version.
- Cleaning up lower-ranked incoming recordings.
- Distributed host locking or automatic repair of partial Atlas writes.
- Full historical RugbyPass discovery beyond its featured catalogue.
