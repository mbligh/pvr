# Downloading a matched PWR release

`download_release` downloads **one existing Atlas-matched YouTube,
BBC iPlayer, or RugbyPass release** to an **explicit absolute .mkv
destination**, verifies it with `ffprobe`, and stores its file inventory
and job in Atlas. It also retains the existing Formula 1 TransferNow
adapter. Preview is the default; **only `--apply` makes changes**.

This is the **single-release adapter**, separate from `acquire_rugby`
which performs multi-provider selection and canonical filing. This command
does not generate NFOs, overwrite published media, or clean up alternatives.

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

**System downloader configuration:** This adapter deliberately does **not**
override the format selector (`-f`) or format sorter (`-S`). The
`mbligh/apt` `yt-dlp-sabr` package installs `/etc/yt-dlp.conf` with
`--remote-components ejs:npm`, `-f bv+ba/b` and
`-S res:1080,+vcodec:avc`. Those options must remain in effect because
YouTube's format and PO-token requirements can change; the adapter should
not force a different format. The initial adapter version mistakenly forced
`bv*+ba/b`, which overrode the package's selector and may trigger 403
media-download failures.

If a YouTube download fails with HTTP 403, check which executable/package
and JS challenge support are in effect, without downloading anything:

```sh
command -v yt-dlp
yt-dlp --version
dpkg-query -W yt-dlp-sabr 2>/dev/null || true
yt-dlp --verbose --list-formats \
  'https://www.youtube.com/watch?v=8CFBWPSG3vc'
```

Look for missing Deno/EJS challenge support, skipped formats, or PO-token
warnings. This **may not** fix every 403: some formats/clients require
video-specific PO tokens even with an up-to-date yt-dlp. The adapter
preserves and reports the yt-dlp error rather than trying insecure or
unverified client workarounds automatically.

## BBC and RugbyPass adapters

The adapters use an exact source URI and destination just like YouTube.
BBC accepts `bbc:m0034hxp` (8-character iPlayer PID), and RugbyPass
accepts `rugbypass:1048390` (numeric VOD ID). `--get-iplayer`,
`--rugbypass`, and `--ffmpeg` override executable paths. BBC invokes
`get_iplayer --pid` with an isolated temporary profile to avoid the
operator's existing postprocessing hooks, history and preferences; quality
is requested as `fhd,hd`. RugbyPass invokes the existing authenticated
`~/tools/video/rugbypass` with a temporary `--output` path and
`--no-fixture-nfo` to avoid legacy fixture filing.

Both adapters stage an MP4 in a temporary directory, remux into MKV using
`ffmpeg -c copy` (no transcoding), verify with `ffprobe`, then publish
using an atomic no-clobber hard link. Temporary output is removed whether
the download succeeds or fails. Errors from these third-party tools are
summarized without exposing private token-bearing diagnostic URLs.

BBC's iPlayer catalogue exposes only currently available episodes;
RugbyPass relies on an existing private auth profile. The source-specific
downloader can still fail for rights restrictions, expired auth, or media
format issues; the existing failed job semantics apply.

Example previews (no data transfer):

```sh
python3 download_release bbc:m0034hxp \
  --destination /data/incoming/pvr-bbc-m0034hxp.mkv
python3 download_release rugbypass:1048390 \
  --destination /data/incoming/pvr-rugbypass-1048390.mkv
```

These particular example IDs are not necessarily *new missing PWR*
releases: the BBC example is already linked to a published episode,
and the RugbyPass example is from another rugby competition, so the PWR
download command will deliberately reject it until it is linked to a
supported episode. Multi-source PWR automation is described in
[ACQUISITION.md](ACQUISITION.md).

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

## Subsequent workflow

Quality selection, canonical filing/NFO publication, and bounded PWR
acquisition from BBC, YouTube and RugbyPass have now been implemented.
Use [ACQUISITION.md](ACQUISITION.md) for the combined workflow, including
remote source refresh and matching. Published-file upgrades and removal
of lower-ranked incoming alternatives remain explicitly manual.
