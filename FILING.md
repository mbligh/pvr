# Canonical filing of inventoried rugby and Formula 1 recordings

`file_recording` publishes one **already-inventoried** supported recording into
the series directory with the canonical season/episode filename and the exact
score-free NFO XML already stored in `pvr.episodes`. It is **preview-only**
unless `--apply` is supplied.

Supported series: Rugby Womens PWR (season 2025 onward), Rugby Mens Intl,
Rugby Womens Intl, Rugby Nations Championships (2026 onward), and Formula 1.
This command is separate from `download_release`; the quality-selector
and preview-first `acquire_missing` worker reuse it. Existing canonical
NFO validation and no-overwrite checks apply to all supported series.

## Trial on slagg

First preview the 25 October 2025 Trailfinders Women vs Exeter Chiefs
recording downloaded from YouTube, currently registered in `pvr.files` as
`youtube:8CFBWPSG3vc`:

```sh
cd ~/pvr
git pull
python3 file_recording /data/incoming/pvr-8CFBWPSG3vc.mkv
```

Its canonical destinations, based **only** on the Atlas episode's
`nfo_path` and `nfo` fields, should be:

```text
/data/sports/Rugby_Womens_PWR/Season_2025/
  Rugby_Womens_PWR-s2025e102-Trailfinders_Women_v_Exeter_Chiefs_20251025.mkv
  Rugby_Womens_PWR-s2025e102-Trailfinders_Women_v_Exeter_Chiefs_20251025.nfo
```

Confirm that the preview's `status` is `ready`, then perform the filing:

```sh
python3 file_recording /data/incoming/pvr-8CFBWPSG3vc.mkv --apply
```

The source in `/data/incoming` is removed **only after** both canonical
files have been published and the destination's `pvr.files` record and
original inventory state have been updated in Atlas.

No unrelated files are touched, including the two recordings of
`S2026E101`. No existing canonical destination file or NFO is overwritten;
`destination_exists` is a hard stop.

**Preferred recording protection:** If Atlas already lists a physically
present, verified file for the **same episode** under the sports library,
the filer reports `status=already_published` and makes no changes, even
when that recording has a noncanonical filename. This prevents a downloaded
YouTube alternative from being promoted over an existing BBC recording.
The check is episode-specific: a BBC recording for S2026E101 does **not**
block filing YouTube S2025E102. The filer does not yet rank recording
quality or select replacements; intentional upgrades will be a separate
operation. Files merely left in `/data/incoming` do not count as already
published.

## Quality selection (local files)

Before a new file is published, the filer uses the same policy as the
read-only `select_recording` command:

1. Exclude any recording with height below 720 pixels.
2. Exclude any recording shorter than 3,600 seconds (one hour).
3. Among qualifying recordings with frame rate **>=50 fps**, choose the
   **largest physical file in bytes**.
4. If there are no qualifying >=50 fps recordings, choose the largest
   qualifying recording under 50 fps.

Exactly 720 pixels, 3,600 seconds and 50 fps **pass** the thresholds.
Files with missing duration/resolution/frame-rate information are not
eligible until re-probed. Missing, moved, symlinked, or changed files are
not eligible either. Equal-size candidates are resolved by stable file ID.
File size is deliberately used as the ranking metric: there is no additional
BBC preference, 60fps preference, video-codec preference or bitrate weighting.

```sh
# Explain which S2025E102 file is preferred, without modifying anything:
python3 select_recording 'rugby-womens-pwr:pwr-289127@mbligh.github.io'

# Compare BBC and YouTube copies of S2026E101:
python3 select_recording 'rugby-womens-pwr:pwr-295034@mbligh.github.io'
```

If an incoming source loses the quality comparison, `file_recording`
reports `not_preferred`. If its height, duration or FPS fails validation,
it reports `ineligible`. In both cases, `--apply` does nothing.

If a recording for that episode is **already in the media library**, it is
never automatically replaced. An incoming file that wins the comparison
against an already-published recording is reported as
`upgrade_available` for future explicit replacement; a losing incoming
file reports `already_published`. Existing destination collisions still
stop publication. Selection never deletes or moves files, never writes
Atlas, and never triggers downloads. `file_recording` acts only when the
selected eligible candidate is not blocked by an existing publication.

## Data and publication model

- The source must already have a `pvr.files` entry for the current host
  (default is `hostname`) with an episode association.
- Canonical episode identity is checked against both the inventory and the
  NFO XML, including series, season and episode number.
- Source file size and modification time must still match Atlas; the filer
  refuses changed or missing recordings.
- The destination is derived from the stored NFO relative path. Directory
  traversal, unexpected series names or seasons, and NFO/episode mismatches
  are rejected. The library root itself may be a symlink (for example,
  `/data/sports` on slagg) and will be followed, while symlinked series
  and season subdirectories are still rejected. Atlas file paths retain
  the configured `/data/sports/...` spelling.
- The first `--apply` action creates an Atlas `pvr.jobs` record with
  `type=filing` and `status=running`.
- If the source and destination share a filesystem, the publisher creates a
  no-clobber hard link and probes it with `ffprobe`. For different
  filesystems it copies to a temporary file in the target directory,
  verifies the source has not changed, publishes via no-clobber hard link
  and probes the published copy. In both cases, the original still exists
  during verification.
- The NFO sidecar is written in the destination directory and installed
  using an atomic no-clobber link. The exact Atlas XML bytes are used.
- On success, the new `pvr.files` record inherits source URI and technical
  metadata, and stores `previous_file_id` and `nfo_path`.
- The historical source file record is **retained** in MongoDB with
  `file_state=moved` and `moved_to` pointing to the destination; its
  physical path is removed after the database commit.
- The job is marked `succeeded` with the transfer method and destination
  ID. Preview makes no filesystem or database modifications.

The command accepts `--sports-root /path` (default `/data/sports`),
`--ffprobe /path/to/ffprobe`, `--host slagg` and the same MongoDB URI
settings as the other PVR commands.

## Errors and recovery

**No overwrite:** if the canonical MKV, the NFO, or an Atlas destination
record already exists, the filer stops without changing them.

**Before the Atlas commit:** if media probing or NFO publication fails, the
publisher removes only new destination files created during that failed
attempt; the original stays intact. Temporary staging files are removed.

**Partial/ambiguous Atlas failure:** after publication starts, database
operations may be partially successful if the connection fails. The filer
conservatively retains **both the original and the canonical destination**
rather than trying an unsafe rollback. The job is marked `failed` and
`requires_reconciliation=true` when possible. Inspect the filesystem and
`pvr.files` before retrying. Do not delete either copy blindly.

**Source deletion failure:** if the destination was committed but unlinking
the original failed, the original remains for manual reconciliation. The
history record identifies its destination. The filer does not automatically
overwrite/recover conflicted files on retry.

**Media integrity:** `ffprobe` checks container/stream metadata and positive
duration. It is not a full playback/hash verification.

## Further work

Support identical-file recovery from interrupted jobs, multiple source
adapters, source quality selection/replacement, and automatic dispatch by the
scheduler. These should be independent operations from the safe, explicit
single-file publisher.
