"""RugbyPass adapter: rugbypass:VOD_ID -> verified exact-destination MKV.

Reuses the existing mbligh/tools/video/rugbypass client (including its
private auth profile), but requests an explicit staged MP4 and disables
legacy fixture NFO generation. No script modifications or extra login.
"""
import pathlib
import re
import subprocess

from downloaders.staged import publish_remuxed, run_checked

RUGBYPASS_URI = re.compile(r"^rugbypass:([0-9]{1,16})$")


def rugbypass_id(uri):
    match = RUGBYPASS_URI.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise ValueError("Expected rugbypass:<numeric-video-id> URI")
    return match.group(1)


def download(uri, destination, *, rugbypass="~/tools/video/rugbypass",
             ffmpeg="ffmpeg", ffprobe="ffprobe", execute=subprocess.run,
             **kwargs):
    video_id = rugbypass_id(uri)
    script = str(pathlib.Path(rugbypass).expanduser())

    def produce(stage):
        target = stage / "recording.mp4"
        run_checked([script, video_id, "--output", str(target),
                     "--no-fixture-nfo", "--ffmpeg", ffmpeg], execute=execute)
        return target

    return publish_remuxed(destination, produce=produce, ffmpeg=ffmpeg,
                           ffprobe=ffprobe, execute=execute, **kwargs)
