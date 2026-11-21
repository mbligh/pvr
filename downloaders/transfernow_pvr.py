"""PVR adapter for TransferNow F1 releases.

Selects exactly one media file, verifies the storage URL and downloaded
media, remuxes when necessary and publishes with atomic no-clobber linking.
All network calls happen only after an explicit PVR --apply (or standalone
TransferNow --list/--check command). No unrelated collection writes.
"""
import contextlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from downloaders import transfernow
from inventory.scan import probe_video

SOURCE_URI = re.compile(r"^reddit-teletz:([A-Za-z0-9]{8,64})$")
EXTENSIONS = {".mkv", ".mp4", ".mov", ".m4v", ".ts", ".webm"}


def source_id(uri):
    match = SOURCE_URI.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise ValueError("Expected reddit-teletz:<TransferNow-ID> URI")
    return match.group(1)


def share_from_release(uri, url):
    """Check that the stored URL identifies exactly this indexed transfer."""
    wanted = source_id(uri)
    if not isinstance(url, str):
        raise ValueError("Release is missing its TransferNow URL")
    try:
        share = transfernow.parse_share(url)
    except transfernow.TransferNowError as exc:
        raise ValueError(f"Invalid stored TransferNow URL: {exc}") from exc
    if share.transfer_id != wanted:
        raise ValueError("Release source ID and TransferNow URL disagree")
    return share


def inspect(uri, url, *, metadata=transfernow.metadata,
            signed_url=transfernow.signed_url, probe=transfernow.probe):
    """Network availability check; does not download or modify Atlas.

    Only single-file public video links are accepted for automated filing.
    A multi-file transfer is ambiguous and must be handled manually.
    """
    share = share_from_release(uri, url)
    entries = transfernow.select_files(metadata(share), share.file_id)
    if len(entries) != 1:
        raise ValueError(f"Transfer contains {len(entries)} files; requires manual selection")
    entry = entries[0]
    filename = transfernow.safe_name(entry["name"])
    if Path(filename).suffix.lower() not in EXTENSIONS:
        raise ValueError(f"Unsupported video extension for automated import: {filename}")
    signed = signed_url(share, entry["id"])
    length, content_type = probe(signed)
    return {"entry": entry, "name": filename, "download_url": signed,
            "size_bytes": entry["size"], "content_type": content_type,
            "content_length": length}


def download(uri, url, destination, *, ffprobe="ffprobe", ffmpeg="ffmpeg",
             inspect_link=inspect, download_file=transfernow.download,
             execute=subprocess.run, probe=probe_video):
    """Download and publish a single MKV without overwriting an existing file."""
    share_from_release(uri, url)
    final = Path(destination)
    if not final.is_absolute() or final.suffix.lower() != ".mkv":
        raise ValueError("Destination must be an absolute .mkv path")
    if not final.parent.is_dir():
        raise ValueError(f"Destination directory does not exist: {final.parent}")
    if os.path.lexists(final):
        raise FileExistsError(f"Destination already exists: {final}")
    # Do not expose partial or unprobed media under the final path.
    with tempfile.TemporaryDirectory(prefix=".pvr-transfernow-", dir=final.parent) as tmp:
        staging = Path(tmp)
        info = inspect_link(uri, url)
        source = staging / ("incoming" + Path(info["name"]).suffix.lower())
        with contextlib.redirect_stdout(sys.stderr):
            download_file(info["download_url"], info["entry"], source)
        if not source.is_file() or source.is_symlink() or source.stat().st_size <= 0:
            raise ValueError("TransferNow did not produce a regular nonempty file")
        if source.suffix.lower() != ".mkv":
            candidate = staging / "recording.mkv"
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-nostdin", "-n", "-i", str(source), "-map", "0",
                "-c", "copy", str(candidate),
            ]
            result = execute(command, capture_output=True, text=True, check=False)
            if result.returncode:
                raise ValueError("ffmpeg remux failed: " + result.stderr[-400:])
        else:
            candidate = source
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError("Remux did not produce a regular media file")
        before = candidate.stat()
        media = probe(candidate, binary=ffprobe)
        after = candidate.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("Media changed during verification")
        # Atomic, no-clobber publish on the destination filesystem.
        os.link(candidate, final, follow_symlinks=False)
        return media
