# PVR rugby source refresh and missing-recording acquisition

Three one-shot commands separate rugby discovery and acquisition:

- **refresh_releases** discovers BBC iPlayer, YouTube PWR and RugbyPass
  releases, validates their IDs, indexes them in Atlas, and links recordings
  to already scheduled fixtures. Preview makes no writes; --apply imports
  and matches. It never downloads media.
- **acquire_rugby** uses only releases *already linked in Atlas*, evaluates
  available recordings, downloads at most --max-candidates per due episode,
  and publishes the quality winner with the canonical NFO. It never calls
  provider discovery or release indexers. Preview is the default.
- **run_rugby** orchestrates both. On --apply it calls refresh_releases
  --apply, then acquire_rugby --apply. A failed or empty provider catalogue
  aborts before downloads. Without --apply it only previews existing
  indexed acquisitions, matching run_f1's behavior. --skip-discovery
  skips refresh, even on --apply.

Command pairing: run_rugby/run_f1 for discovery and acquisition,
acquire_rugby/acquire_f1 for previously indexed releases, and
import_rugby/import_f1 for official fixture schedules. The runners
do not update fixture schedules.

Both acquire workers accept --season (alias --year), --round, --episode,
--limit, --min-age-hours, --retry-after-hours, --sports-root, --incoming-root,
--host, --ffmpeg, --ffprobe, --show-skipped, --database, and mutually
exclusive --apply/--dry-run. Rugby-specific acquisition options include
--series, --source and --max-candidates. Discovery pagination options
(--youtube-limit, --bbc-pages, --rugbypass-pages) now belong to
run_rugby or refresh_releases, not acquire_rugby.

**Cron migration:** Existing commands of the form
`./acquire_rugby --refresh --apply` must become
`./run_rugby --apply`. The --refresh flag is no longer
accepted by acquire_rugby. No crontab changes are performed automatically.

## Getting started on slagg

```sh
cd ~/pvr
git pull

# Inspect provider catalogue sizes without modifying Atlas:
python3 refresh_releases

# Refresh the Atlas source catalogue and link unambiguous matches:
python3 refresh_releases --apply

# Preview acquisition (may show several source downloads for one episode):
./acquire_rugby --limit 3

# Run a bounded single-episode trial across available sources:
./acquire_rugby --limit 1 --apply
```

To combine discovery, matching and acquisition in one invocation:

```sh
./run_rugby --limit 1 --apply
```

To preview from currently indexed releases:

```sh
./run_rugby --limit 3
```

Like run_f1, this preview **does not discover new recordings**. To
inspect a plan using newly discovered sources, first run
`./refresh_releases --apply`, then preview
`./acquire_rugby`.
For apply against previously indexed releases only, use
`./run_rugby --skip-discovery --apply` or
`./acquire_rugby --apply`.

Continue the historical **YouTube-only** backfill without involving new
providers:

```sh
./acquire_rugby --season 2025 --source youtube --limit 10 --apply
```

`--source` is repeatable, e.g. `--source bbc --source youtube`. The
acquisition worker filters already indexed sources; run_rugby applies the
same selected sources during its refresh.

## RugbyPass: internationals and Nations Championship

The same safe PWR worker now supports `--series` for the three other
**already-numbered** Atlas rugby fixture series, plus the separately
registered `Rugby_Womens_WXV` series. For these non-PWR series,
the supported source is **RugbyPass only**; BBC and YouTube remain
PWR-specific until their international catalogue and matching rules
are established.

```sh
cd ~/pvr
git pull

# Preview matches already found in RugbyPass's Atlas catalogue:
python3 match_rugbypass --verbose

# Link only unambiguous full-match replays to existing numbered fixtures:
python3 match_rugbypass --apply

# Inspect missing men's internationals and Nations Championship recordings:
./acquire_rugby --series Rugby_Mens_Intl --limit 5
./acquire_rugby --series Rugby_Mens_Nations --limit 5

# Inspect women's internationals:
./acquire_rugby --series Rugby_Womens_Intl --limit 5

# WXV has its own scheduler fixture identities after import_rugby is applied:
./acquire_rugby --series Rugby_Womens_WXV --limit 5

# Safely trial exactly one Nations Championship RugbyPass recording:
./acquire_rugby --series Rugby_Mens_Nations --limit 1 --apply

# Later: refresh RugbyPass listings, match and acquire in one bounded run:
./run_rugby --series Rugby_Womens_Intl --limit 1 --apply
```

These commands use the existing `pvr.episodes` IDs, numbered
`Season_YYYY` directories and stored NFO XML, and apply the same
>=720p, >=1h, prefer >=50fps, otherwise largest-file rule.
A canonical recording is never replaced and lower-ranked incoming
recordings are retained. The RugbyPass adapter reuses
`~/tools/video/rugbypass` and its existing local login profile.

**Nations Championship routing:** These matches also appear in the men's
international calendar, but an explicitly labelled `Nations Championship`
RugbyPass recording links only to `Rugby_Mens_Nations`; it is not acquired
a second time into `Rugby_Mens_Intl`. This prevents duplicate videos
between those two libraries.

**WXV directory separation:** The updated scheduler partitions
already-accepted WXV fixtures from the women's-international series,
creates distinct `Rugby_Womens_WXV` IDs and week-based round numbering,
and keeps the original WXV records as superseded history. This takes
effect after `import_rugby --apply` is run on slagg with the new code. The
RugbyPass matcher then links replays to WXV's dedicated series. No
existing media recordings are automatically moved or overwritten.

**Unnumbered fixtures and ambiguity:** At the time of this change, Atlas
has 110 numbered men's internationals, 44 numbered women's internationals
and 12 numbered Nations Championship fixtures. Other Nations fixtures
lack canonical episode numbers/NFOs and are excluded until metadata is
updated. Duplicate national fixtures whose teams/dates overlap within
the replay matching window are marked ambiguous, not guessed. See
[MATCHING.md](MATCHING.md).

For PWR, the default command without `--series` continues to use BBC,
YouTube and RugbyPass together. Source selection for the international
series is restricted to RugbyPass: using `--source youtube` or
`--source bbc` with one of these series is deliberately an error.

## Complete selection policy

For each scheduled, numbered supported rugby fixture older than three hours (configurable),
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
30 5 * * * cd /home/mbligh/pvr && ./run_rugby --limit 2 --apply >> /home/mbligh/pvr-acquire.log 2>&1
```

The sample is not installed or enabled. run_rugby --apply refreshes the BBC brand,
up to 100 YouTube stream entries and up to five RugbyPass catalogue pages
per section by default. Override with `--youtube-limit`,
`--bbc-pages`, `--rugbypass-pages` and `--rugbypass`. Avoid very
frequent full-catalogue refreshes or unbounded historical backfills.

## Not implemented yet

- Acquisition from unrelated TV series, other rugby competitions or Formula 1
  through this rugby worker (Formula 1 has its separate acquisition path).
- Predictive source quality ranking without downloading the media.
- Replacing a published recording with a newly discovered better version.
- Cleaning up lower-ranked incoming recordings.
- Distributed host locking or automatic repair of partial Atlas writes.
- Full historical RugbyPass discovery beyond its featured catalogue.
