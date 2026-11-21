"""PVR adapter for TransferNow F1 releases.

Selects exactly one media file, verifies the storage URL and downloaded
media, remuxes when necessary and publishes with atomic no-clobber linking.
All network calls happen only after an explicit PVR --apply (or standalone
TransferNow --list/--check command). No unrelated collection writes.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
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



def staging_path(uri, destination):
    """Stable private staging path on the destination filesystem."""
    transfer_id = source_id(uri)
    final = Path(destination)
    if not final.is_absolute() or final.suffix.lower() != ".mkv":
        raise ValueError("Destination must be an absolute .mkv path")
    digest = hashlib.sha256(
        (uri + "\0" + str(final)).encode("utf-8")).hexdigest()[:24]
    return final.parent / (".pvr-transfernow-" + transfer_id + "-" + digest)


def _regular_private_file(path):
    """Never read/resume an attacker-controlled symlink or hardlink."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Unsafe staging file (symlink or hardlink): {path}")
    return info


@contextlib.contextmanager
def _locked_stage(stage):
    """Refuse simultaneous writers for the same destination and release."""
    if os.path.lexists(stage):
        if stage.is_symlink() or not stage.is_dir():
            raise ValueError(f"Unsafe TransferNow staging directory: {stage}")
    else:
        stage.mkdir(mode=0o700)
    lock = stage / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Unsafe TransferNow staging lock: {lock}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"TransferNow download already running: {stage}") from exc
        yield stage
    finally:
        os.close(fd)


def _check_manifest(stage, identity):
    """Bind retained bytes to the transfer file ID, size and output path.

    Do not silently resume a partial from a different upload. In particular,
    a different file with the same TransferNow ID must be reviewed manually.
    """
    manifest = stage / "manifest.json"
    if os.path.lexists(manifest):
        info = _regular_private_file(manifest)
        if info.st_size > 8192:
            raise ValueError(f"Oversized TransferNow manifest: {manifest}")
        try:
            original = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid TransferNow resume manifest: {manifest}") from exc
        if original != identity:
            raise ValueError(
                f"TransferNow metadata changed; refusing to reuse cached bytes: {stage}")
    else:
        # A killed process during initial manifest creation must not make its
        # unverified bytes eligible for resuming.
        if any(item.name != ".lock" for item in stage.iterdir()):
            raise ValueError(f"Staging directory has no manifest: {stage}")
        with manifest.open("x", encoding="utf-8") as stream:
            json.dump(identity, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())


def download(uri, url, destination, *, ffprobe="ffprobe", ffmpeg="ffmpeg",
             inspect_link=inspect, download_file=transfernow.download,
             execute=subprocess.run, probe=probe_video):
    """Resume an interrupted TransferNow download, verify, publish no-clobber.

    The source and source.part survive failed transfers, remuxes and probes.
    The stage is removed only after a complete, verified MKV is published.
    """
    share_from_release(uri, url)
    final = Path(destination)
    stage = staging_path(uri, final)
    if not final.parent.is_dir():
        raise ValueError(f"Destination directory does not exist: {final.parent}")
    if os.path.lexists(final):
        raise FileExistsError(f"Destination already exists: {final}")
    with _locked_stage(stage):
        # Recheck after taking the lock. Avoid a network operation on a
        # destination another job may have completed in the meantime.
        if os.path.lexists(final):
            raise FileExistsError(f"Destination already exists: {final}")
        info = inspect_link(uri, url)
        entry = info["entry"]
        original_name = transfernow.safe_name(entry["name"])
        extension = Path(original_name).suffix.lower()
        if extension not in EXTENSIONS:
            raise ValueError(f"Unsupported video extension: {original_name}")
        if entry["size"] <= 0:
            raise ValueError("TransferNow reported an empty source")
        identity = {
            "version": 1,
            "uri": uri, "source_url": url, "destination": str(final),
            "file_id": entry["id"], "filename": original_name,
            "size": entry["size"],
        }
        _check_manifest(stage, identity)
        source = stage / ("incoming" + extension)
        part = source.with_name(source.name + ".part")
        for path in (source, part):
            if os.path.lexists(path):
                _regular_private_file(path)
        with contextlib.redirect_stdout(sys.stderr):
            # The core TransferNow downloader knows how to resume .part and
            # reject an HTML response, oversized partial or wrong byte count.
            download_file(info["download_url"], entry, source)
        if (source.is_symlink() or not source.is_file() or
                source.stat().st_size <= 0):
            raise ValueError("TransferNow did not produce a regular nonempty file")
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=stage) as tmp:
            if extension != ".mkv":
                candidate = Path(tmp) / "recording.mkv"
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
            # Atomic, no-clobber publish on the same filesystem.
            os.link(candidate, final, follow_symlinks=False)
        # Once the final file has been safely published, the cache is no
        # longer needed. A cleanup failure must not mark a good recording
        # failed in Atlas; the stale directory can be removed manually.
        try:
            shutil.rmtree(stage)
        except OSError as exc:
            print(f"Warning: could not remove TransferNow cache {stage}: {exc}",
                  file=sys.stderr)
        return media
