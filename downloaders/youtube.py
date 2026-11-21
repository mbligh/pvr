"""YouTube downloader adapter: source URI and exact final MKV destination.

Download into a staging directory on the destination filesystem; verify with
ffprobe; publish using hard-link no-clobber semantics. Never overwrite a file.
"""
import os
import pathlib
import re
import subprocess
import tempfile

from inventory.scan import probe_video

YOUTUBE_ID = re.compile(r"^youtube:([A-Za-z0-9_-]{11})$")


def youtube_id(uri):
    match = YOUTUBE_ID.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise ValueError("Expected an exact youtube:<11-character-video-id> URI")
    return match.group(1)


def download(uri, destination, *, yt_dlp="yt-dlp", ffprobe="ffprobe",
             execute=subprocess.run, probe=probe_video):
    """Return verified media data, or fail without publishing incomplete media.

    The output template is deliberately under a temporary directory on the
    *same filesystem* as the final destination, so os.link() publishes the
    verified file atomically and fails with EEXIST on a collision.
    """
    video_id = youtube_id(uri)
    destination = pathlib.Path(destination)
    if not destination.is_absolute() or destination.suffix.lower() != ".mkv":
        raise ValueError("Destination must be an absolute .mkv filename")
    if destination.is_symlink() or os.path.lexists(destination):
        raise FileExistsError(f"Destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise ValueError(f"Destination directory does not exist: {destination.parent}")
    with tempfile.TemporaryDirectory(prefix=".pvr-download-", dir=destination.parent) as staging:
        stage = pathlib.Path(staging)
        target = stage / "recording.mkv"
        command = [
            yt_dlp, "--no-playlist", "--no-overwrites",
            "--no-mtime", "--no-progress",
            "--format", "bv*+ba/b",
            "--merge-output-format", "mkv",
            "--remux-video", "mkv",
            "--output", str(stage / "recording.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        result = execute(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("yt-dlp failed (exit " + str(result.returncode) +
                               "): " + result.stderr.strip()[-600:])
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"yt-dlp did not produce the expected MKV: {target}")
        if target.stat().st_size <= 0:
            raise ValueError("yt-dlp produced an empty recording")
        media = probe(target, binary=ffprobe)
        before = target.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                target.stat().st_size, target.stat().st_mtime_ns, target.stat().st_ino):
            raise ValueError("Staged file changed while verifying")
        # os.link is an atomic, no-overwrite publication on the same
        # filesystem. It cannot overwrite an existing destination, even
        # if another job creates the path after the preflight check.
        os.link(target, destination, follow_symlinks=False)
        return media
