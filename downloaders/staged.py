"""Safe staged download publication shared by the BBC and RugbyPass adapters.

External tools write only inside a temporary directory beside the requested
output. Remuxing uses stream copy (no transcoding). Only a probed MKV is
published using atomic no-overwrite hard-link semantics.
"""
import os
import pathlib
import subprocess
import tempfile

from inventory.scan import probe_video


def destination_path(destination):
    destination = pathlib.Path(destination)
    if not destination.is_absolute() or destination.suffix.lower() != ".mkv":
        raise ValueError("Destination must be an absolute .mkv filename")
    if os.path.lexists(destination):
        raise FileExistsError(f"Destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise ValueError(f"Destination directory does not exist: {destination.parent}")
    return destination


def run_checked(command, *, execute=subprocess.run):
    """No command shell. Avoid leaking sensitive signed URLs in exceptions."""
    result = execute(command, capture_output=True, text=True, check=False)
    if result.returncode:
        # Tool diagnostics may contain signed CDN URLs or account data.
        # Do not log arbitrary stdout/stderr to Atlas or console.
        raise RuntimeError(
            f"{pathlib.Path(command[0]).name} failed (exit {result.returncode}); "
            "inspect the original tool directly for details")
    return result


def publish_remuxed(destination, *, produce, ffmpeg="ffmpeg", ffprobe="ffprobe",
                    execute=subprocess.run, probe=probe_video):
    """Produce an MP4 in staging, remux to MKV, verify, then no-clobber publish."""
    destination = destination_path(destination)
    with tempfile.TemporaryDirectory(prefix=".pvr-download-", dir=destination.parent) as directory:
        staging = pathlib.Path(directory)
        recording = produce(staging)
        recording = pathlib.Path(recording)
        if (recording.parent != staging or recording.suffix.lower() != ".mp4" or
                recording.is_symlink() or not recording.is_file() or
                recording.stat().st_size == 0):
            raise ValueError("Downloader did not create exactly one staged MP4")
        mkv = staging / "verified.mkv"
        run_checked([ffmpeg, "-hide_banner", "-loglevel", "error",
                     "-nostdin", "-n", "-i", str(recording),
                     "-map", "0:v:0", "-map", "0:a?",
                     "-c", "copy", "-f", "matroska", str(mkv)], execute=execute)
        if not mkv.is_file() or mkv.is_symlink() or mkv.stat().st_size <= 0:
            raise ValueError("Remux did not produce a nonempty MKV")
        original = mkv.stat()
        media = probe(mkv, binary=ffprobe)
        checked = mkv.stat()
        if (original.st_ino, original.st_size, original.st_mtime_ns) != (
                checked.st_ino, checked.st_size, checked.st_mtime_ns):
            raise ValueError("Staged recording changed while probing")
        os.link(mkv, destination, follow_symlinks=False)
        return media
