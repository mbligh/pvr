# Canonical filing of existing PWR recordings

`file_recording` publishes one **already-inventoried** PWR recording into
the series directory with the canonical season/episode filename and the exact
score-free NFO XML already stored in `pvr.episodes`. It is **preview-only**
unless `--apply` is supplied.

Initially supported: Rugby Womens PWR, season 2025 onward. This command is
separate from `download_release` and is not yet part of automatic scheduling
or quality selection.

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
`destination_exists` is a hard stop. The command never selects which
recording is best or replaces a previous version.

## Data and publication model

- The source must already have a `pvr.files` entry for the current host
  (default is `hostname`) with an episode association.
- Canonical episode identity is checked against both the inventory and the
  NFO XML, including series, season and episode number.
- Source file size and modification time must still match Atlas; the filer
  refuses changed or missing recordings.
- The destination is derived from the stored NFO relative path. Directory
  traversal, unexpected series names or seasons, symlink directories and
  NFO/episode mismatches are rejected.
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
