# Downloading a matched PWR release

`download_release` is the first PVR acquisition CLI. It downloads **one
existing Atlas-matched YouTube release** to an **explicit absolute .mkv
destination**, verifies the result with `ffprobe`, and stores the file
inventory entry and download job in Atlas. Preview is the default; **only
`--apply` makes changes**.

It does not automatically select sources, file an episode in its final library
location, generate new NFO sidecars, schedule batches, or delete recordings.
The first version is deliberately single-release so quality and publishing
behavior can be validated before automating downloads.

## Prerequisites

On `slagg`, use the existing `~/.mongodb/pvr` MongoDB URI (or the
`MONGODB_URI` environment variable). The `pvr.releases` document must
already have an `episode_id` populated by `match_releases`, referring to
a canonical PWR episode in Season_2025 onward.

Install/verify the local tools:

```sh
command -v yt-dlp
command -v ffmpeg
command -v ffprobe
```

`yt-dlp` performs the media fetch. FFmpeg is needed for merging/remuxing;
`ffprobe` verifies the completed recording.

## Single-match trial

The indexed YouTube match **Trailfinders Women vs Exeter Chiefs** (25 October
2025) is `youtube:8CFBWPSG3vc`, matched to canonical episode
`S2025E102`. Neither of the two previously inventoried S2026E101 files
is touched.

```sh
cd ~/pvr
git pull

# Preview only; no directory/file creation and no Atlas writes.
python3 download_release youtube:8CFBWPSG3vc \
  --destination /data/incoming/pvr-8CFBWPSG3vc.mkv

# Run exactly one download and record the result.
python3 download_release youtube:8CFBWPSG3vc \
  --destination /data/incoming/pvr-8CFBWPSG3vc.mkv --apply
```

The destination parent directory must already exist. The suffix must be
`.mkv`. A successful run will produce the exact requested path, not a
file under `/data/sports`; file publication is a separate component.

## Safety and bookkeeping

Before running `yt-dlp`, the CLI validates the source URI, the Atlas release,
its matched episode, the season cutoff (2025 onward), availability, the local
destination, and existing files recorded on the same host for that episode.
It checks whether those inventoried paths still contain a nonempty file of
the recorded size, rather than blindly treating old Atlas entries as present.

A present file for the episode makes the default plan `already_owned`,
**even if it came from a different source**. Use
`--allow-existing-episode` only when intentionally acquiring an alternative
recording. An existing destination is always `destination_exists` and never
overwritten. Host identity defaults to the machine hostname; `--host`
allows a stable explicit value if needed.

On `--apply`, a `pvr.jobs` document begins in status `running`. The
YouTube adapter downloads into a temporary directory immediately adjacent to
the target path, asks yt-dlp to output MKV, and probes the finished file.
Only when the probe verifies video content and positive duration does it
publish the file with an **atomic hard link that fails if the destination
already exists**. The temporary directory is removed, including on failure.
No incomplete file is exposed at the final path.

After publication, the CLI writes `pvr.files` with episode association,
source URI, host/path, size, timestamps, and ffprobe media attributes. The
job is then marked `succeeded` with the created file ID and metadata.
Download or probe failures mark the job `failed` without publishing a
partial file.

If the actual video download succeeded but **MongoDB registration fails**,
the completed local file is deliberately left intact. Such a failure is
reported; do not retry to overwrite the path. Investigate the job record
and recover the inventory with:

```sh
python3 inventory_files --root /data/incoming --apply
```

Running the trial command again with the same path will not redownload.
This command does not manage parallel duplicate downloads by source yet;
no-clobber publication still prevents one from overwriting another.

## What comes next

After checking a successful single release and inspecting the resulting
`pvr.files` and `pvr.jobs` documents, we can add the other source
adapters (BBC iPlayer and RugbyPass), canonical filing and NFO publishing,
and then selection/scheduling. Automatic quality preferences and upgrades
should be implemented separately from this first download adapter.
