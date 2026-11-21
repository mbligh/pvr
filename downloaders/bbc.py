"""BBC iPlayer adapter: bbc:PID -> one verified exact-destination MKV.

Uses get_iplayer with an isolated temporary profile to avoid invoking the
operator's existing PWR post-processing commands or mutating their download
history. The canonical NFO is added later by file_recording, not here.
"""
import pathlib
import re
import subprocess

from downloaders.staged import publish_remuxed, run_checked

BBC_URI = re.compile(r"^bbc:([a-z][a-z0-9]{7})$")


def bbc_id(uri):
    match = BBC_URI.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise ValueError("Expected bbc:<8-character-programme-PID> URI")
    return match.group(1)


def download(uri, destination, *, get_iplayer="get_iplayer", ffmpeg="ffmpeg",
             ffprobe="ffprobe", execute=subprocess.run, **kwargs):
    pid = bbc_id(uri)

    def produce(stage):
        profile = stage / "profile"
        profile.mkdir()
        # Dedicated profile avoids user preferences such as commandtv hooks
        # which would move the file out of the staging directory.
        command = [
            get_iplayer, "--get", "--pid", pid,
            "--profile-dir", str(profile),
            "--output-tv", str(stage),
            "--file-prefix", "recording",
            "--tv-quality", "fhd,hd",
            "--force", "--no-metadata",
        ]
        run_checked(command, execute=execute)
        files = [x for x in stage.glob("*.mp4") if x.is_file() and
                 not x.is_symlink() and x.stat().st_size > 0]
        if len(files) != 1:
            raise ValueError("get_iplayer produced no unique staged MP4")
        return files[0]

    return publish_remuxed(destination, produce=produce, ffmpeg=ffmpeg,
                           ffprobe=ffprobe, execute=execute, **kwargs)
