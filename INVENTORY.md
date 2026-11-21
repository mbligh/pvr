# Inventory existing PWR recordings

`inventory_files` scans locally stored video files, identifies which canonical
PWR fixture each recording belongs to, and probes technical quality using
`ffprobe`. It is **read-only by default**. With `--apply`, verified files
are upserted into MongoDB Atlas `pvr.files`; neither mode moves, renames,
modifies, deletes or downloads media.

The first inventory adapter is scoped to **Rugby Womens PWR**, 2025/26
(`Season_2025`) onward. It uses the existing Atlas `pvr.episodes` and
already-linked `pvr.releases` collections, not calendar scraping or a second
episode numbering system.

## On slagg

Install `ffprobe` if it is not already available (usually provided by ffmpeg
on Debian):

```sh
sudo apt install ffmpeg
```

Run the inventory:

```sh
cd ~/pvr
git pull

# Scan the canonical PWR library; probe metadata but do not write Atlas.
python3 inventory_files

# Include a second location containing unfinished or older downloads.
python3 inventory_files --root /data/sports/Rugby_Womens_PWR --root /data/incoming

# Review identity matches without running ffprobe (preview only).
python3 inventory_files --no-probe

# Also list past fixtures with no identified file under these scan roots.
python3 inventory_files --show-unseen

# Once the preview is acceptable, store verified file records in Atlas.
python3 inventory_files --apply
```

Use `--root /data/sports` to search other directories under that tree too.
Only files positively identified as PWR will be associated with its episodes.
`--verbose` includes unrelated video files in the output; otherwise only
identified and problematic files are printed.

The summary includes `episodes_with_files` and
`past_episodes_not_found_in_roots`. The latter only counts fixtures at least
three hours past scheduled kickoff, and **only means not found among the
directories scanned**. Other disks, directories and unprocessed files may
contain recordings. `--show-unseen` prints those episodes individually.
An NFO/filename conflict or failed probe is not counted as a verified file.

There are no added credentials: this uses the existing `~/.mongodb/pvr`,
`MONGODB_URI`, and `MONGODB_DATABASE` configuration. `--host` can override
the machine name used in stable file IDs. The default is the local hostname,
which on slagg should normally be `slagg`. Ensure that every scan of the
same host uses the same hostname so records are idempotent.

## Identity resolution

The scanner only accepts an unambiguous association backed by one or more of:

- A PWR filename such as
  `Rugby_Womens_PWR-s2025e101-Harlequins_v_Loughborough_Lightning.mkv`,
  with the canonical series identified by the filename or directory.
- A sidecar `.nfo` with an `episodedetails` root, PWR showtitle and
  unambiguous season/episode fields.
- A source identifier (BBC PID, YouTube ID or RugbyPass video ID) embedded in
  the filename **and already linked to an Atlas PWR episode**.

A bare `s2025e101` in a mixed directory is not sufficient to identify PWR.
Contradictory filename, NFO, folder season, or release IDs are reported as
`conflict` and do not generate database writes. Unknown or unrelated video
files are `unmatched`, not assigned by fuzzy team-name guesses.

Symlink files and directories are skipped to avoid duplicate scans through
aliases. Missing scan roots and permission errors fail rather than silently
claiming the library is empty. Video extensions scanned:
`.mkv`, `.mp4`, `.m4v`, `.ts`, `.mov`, `.webm`.

## Technical inventory and Atlas format

Each identified file is probed for:

- Video codec, resolution, approximate average frame rate and bitrate
- Duration, container format, file size, total bitrate
- Audio track count and codec names

An `ffprobe` failure, missing video stream or missing positive duration
reports `probe_error`; it is **not** marked as a usable file.
`--no-probe` prints `unprobed` and is incompatible with `--apply`.

An example `pvr.files` document:

```json
{
  "_id": "slagg:/data/sports/Rugby_Womens_PWR/Season_2025/Rugby_Womens_PWR-s2025e101-example.mkv",
  "host": "slagg",
  "path": "/data/sports/Rugby_Womens_PWR/Season_2025/Rugby_Womens_PWR-s2025e101-example.mkv",
  "episode_id": "rugby-womens-pwr:pwr-289126@mbligh.github.io",
  "series_id": "rugby-womens-pwr",
  "season": 2025,
  "episode": 101,
  "match_evidence": ["filename"],
  "release_uri": null,
  "size_bytes": 1000000000,
  "mtime_ns": 1780000000000000000,
  "media": {
    "duration_seconds": 7200.25,
    "video": {
      "codec": "h264",
      "width": 1920,
      "height": 1080,
      "fps": 59.94
    },
    "audio_tracks": 1,
    "audio_codecs": ["aac"]
  }
}
```

The `_id` combines host and absolute path: files with the same path on
different machines are distinct. Observations update `last_seen` and preserve
`first_seen` and unrelated operational fields on repeat scans. An existing
file association to another episode is rejected, never silently replaced.
If the filename does not reveal a unique source, `release_uri` remains null;
do not assume a particular YouTube or BBC recording based only on episode ID.

**Not yet implemented:** tracking vanished/moved files as missing, validating
full-file playback beyond `ffprobe`, calculating cryptographic content hashes,
ranking quality, or selecting files for replacement. The scanner never deletes
file records from Atlas. For now, a record means the file was present and
probed at `last_seen`, not guaranteed still present later.

The next step after reviewing the report is to integrate the downloader and
filer with `pvr.files` so acquisition can avoid already-owned recordings.
