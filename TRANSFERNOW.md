# TransferNow CLI downloader

Browser-free downloads from ordinary public TransferNow sharing links. Uses the
same unauthenticated website endpoints as the TransferNow download page. This
is **not** TransferNow's separately keyed developer API. Python 3.9+ and `curl`
are required; no Python packages or browser are needed.

The extensionless `transfernow` executable is at the root of `mbligh/pvr`; its implementation is `downloaders/transfernow.py`.

## Usage

From the root of `mbligh/pvr`:

```sh
# Inspect files without downloading
./transfernow --list \
  'https://www.transfernow.net/en/cld?utm_source=20261111Jj147Vwz'

# Resolve the signed file URL and check it, without transferring the video
./transfernow --check \
  'https://www.transfernow.net/en/cld?utm_source=20261111Jj147Vwz'

# Download all files in the transfer to the current directory
./transfernow \
  'https://www.transfernow.net/en/cld?utm_source=20261111Jj147Vwz'

# Or download a selected file to a specified directory/name
./transfernow -d ~/tmp --file-id YFpOgt \
  'https://www.transfernow.net/dl/20261111Jj147Vwz'
./transfernow -o ~/tmp/race.mp4 \
  'https://www.transfernow.net/en/d/start?utm_source=20261111Jj147Vwz&utm_term=YFpOgt'
```

The script accepts `/dl/TRANSFER_ID[/SECRET]`, `/en/cld?utm_source=ID`, and
`/en/d/start?utm_source=ID&utm_term=FILE_ID` URLs, or a bare transfer ID.
The `utm_term` identifier, if supplied, selects that file only. Otherwise all
files are downloaded individually with their original names.

Files are written first to `NAME.part`, which `curl --continue-at -` resumes on
rerun. Only a completed, size-verified download is renamed to `NAME`. A
pre-existing completed file is skipped if its size matches. The script refuses
to replace an existing file unless `--overwrite` is supplied. It also checks
the storage response type before writing, to avoid the HTML-as-MP4 problem.

Options:

- `--list` — list file IDs/names/sizes (no signed URL requested)
- `--check` — resolve signed URLs and probe storage HTTP headers only
- `--check --show-url` — also print the temporary signed storage URLs
- `--file-id ID` — select one file
- `-d DIR` / `-o PATH` — output directory or explicit single-file destination
- `--overwrite` — replace an existing complete file and discard a `.part` file

For password-protected transfers use `TRANSFERNOW_PASSWORD=...` in the
environment. A transfer that requires browser-only email verification or other
purchase/access checks may not work with this simple client.

TransferNow may change the **undocumented** website API at any time. As of
November 14, 2026, the public example was verified to respond with file
metadata and a signed CDN URL using:

```
GET https://www.transfernow.net/api/transfer/downloads/metadata?transferId=ID
GET https://www.transfernow.net/api/transfer/downloads/link?transferId=ID&preview=false&fileId=FILE_ID
```

Full 2.4 GB file transfer has **not** been tested by the author. `--check`
should be used first to verify storage reachability on the target system.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_transfernow.py' -v
```

## Using a selected PVR release

The standalone core accepts a TransferNow sharing URL. PVR's download_release
command instead accepts a matched reddit-teletz: URI already stored in Atlas.

Preview without accessing TransferNow or writing to MongoDB:

    ./download_release reddit-teletz:20261121rg0NQzAH \
      --destination /data/incoming/pvr-20261121rg0NQzAH.mkv

Check the source without downloading:

    ./transfernow --check https://www.transfernow.net/dl/20261121rg0NQzAH

Explicitly acquire that release:

    ./download_release reddit-teletz:20261121rg0NQzAH \
      --destination /data/incoming/pvr-20261121rg0NQzAH.mkv --apply

The adapter validates that URI and stored URL agree, accepts exactly one
supported video file in the transfer, checks the signed storage URL,
downloads to a temporary staging directory on the destination filesystem,
remuxes non-MKV video losslessly with ffmpeg, and validates with ffprobe.
The final .mkv is atomically hard-linked into place without replacing an
existing file. Success and failure are recorded in pvr.jobs; successful
recordings are registered in pvr.files.

Requirements: system Python, curl, ffmpeg, ffprobe and pymongo (already
required by the PVR Atlas tools). Downloads require explicit --apply.

The standalone ./transfernow preserves .part files for resumable transfers.
The PVR adapter uses temporary staging and therefore currently discards
partials between PVR job attempts. That needs improving before scheduling
unattended multi-gigabyte downloads.

Automatic release fallback, availability recording in MongoDB, bulk selection
and scheduled downloads are not yet implemented. This command downloads
exactly the source URI specified and never silently chooses another one.
