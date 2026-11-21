"""Safe publication of a verified recording and its canonical Atlas NFO.

All new destination files are created without overwrite. The caller owns
database bookkeeping and removes the source only after successful commits.
"""
import errno
import os
import pathlib
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from inventory.scan import probe_video

SHOW = "Rugby_Womens_PWR"


def canonical_paths(episode, sports_root):
    """Use the authoritative NFO path, never source title or guessed names."""
    season = episode.get("season")
    number = episode.get("episode")
    if not isinstance(season, int) or not isinstance(number, int) or season < 2025 or number < 1:
        raise ValueError("Episode must have a PWR season >=2025 and positive episode number")
    relative = episode.get("nfo_path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Episode is missing its canonical nfo_path")
    path = pathlib.PurePosixPath(relative)
    expected_prefix = f"{SHOW}-s{season}e{number:03d}-"
    if (path.is_absolute() or ".." in path.parts or len(path.parts) != 3 or
            path.parts[0] != SHOW or path.parts[1] != f"Season_{season}" or
            not path.name.startswith(expected_prefix) or path.suffix != ".nfo"):
        raise ValueError(f"Unsafe or inconsistent canonical nfo_path: {relative}")
    root = pathlib.Path(sports_root).expanduser()
    if not root.is_absolute():
        raise ValueError("--sports-root must be an absolute directory")
    nfo = root.joinpath(*path.parts)
    return nfo.with_suffix(".mkv"), nfo


def validate_nfo(episode):
    value = episode.get("nfo")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Canonical episode is missing NFO XML")
    try:
        root = ET.fromstring(value)
        if root.tag != "episodedetails":
            raise ValueError("Unexpected NFO XML root")
        season = int(root.findtext("season") or "")
        number = int(root.findtext("episode") or "")
        show = root.findtext("showtitle") or ""
    except (ET.ParseError, ValueError) as exc:
        raise ValueError(f"Invalid canonical episode NFO: {exc}") from exc
    if (season, number) != (episode["season"], episode["episode"]) or (
            re.sub(r"[^a-z0-9]", "", show.casefold()) != "rugbywomenspwr"):
        raise ValueError("Canonical NFO episode identity does not match Atlas")
    return value.encode("utf-8")


def stat_identity(path):
    """Snapshot used to refuse deleting/modifying a changed original."""
    path = pathlib.Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Not a regular non-symlink recording: {path}")
    st = path.stat()
    if not st.st_size:
        raise ValueError(f"Empty recording: {path}")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def ensure_unchanged(path, identity):
    if stat_identity(path) != identity:
        raise ValueError(f"Source recording changed during filing: {path}")


def prepare_directory(target, root):
    """Follow a symlinked library root; reject symlinks within the library.

    /data/sports may be a symlink to another mounted filesystem. Keep its
    original path for file identity while allowing normal filesystem access.
    """
    root = pathlib.Path(root)
    if not root.is_dir():
        raise ValueError(f"Sports root does not exist: {root}")
    for directory in [target.parent.parent, target.parent]:
        if directory.is_symlink():
            raise ValueError(f"Destination directory is a symlink: {directory}")
        directory.mkdir(exist_ok=True)
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Unsafe destination directory: {directory}")


def _copy_and_publish(source, destination, snapshot):
    """Use same-device hardlink, or verified staged copy across filesystems.

    No-clobber hardlink publication prevents rename() overwriting a concurrent
    writer. Staged cross-device copies live beside the destination.
    """
    try:
        os.link(source, destination, follow_symlinks=False)
        return "hardlink"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pvr-filing-", suffix=".mkv",
                                         dir=destination.parent, delete=False) as output:
            temp = pathlib.Path(output.name)
            with open(source, "rb") as input_file:
                shutil.copyfileobj(input_file, output, length=4 * 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        ensure_unchanged(source, snapshot)
        if temp.stat().st_size != snapshot[2]:
            raise ValueError("Staged copy size does not match source")
        shutil.copystat(source, temp, follow_symlinks=False)
        # The copy is probed by publish() after this function returns.
        os.link(temp, destination, follow_symlinks=False)
        return "copy"
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _write_exclusive(path, content):
    """Write and fsync a temp sidecar, then atomically install no-clobber."""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pvr-nfo-", suffix=".tmp",
                                         dir=path.parent, delete=False) as output:
            temp = pathlib.Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temp, path, follow_symlinks=False)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def publish(source, destination, nfo_path, nfo_xml, *, sports_root,
            ffprobe="ffprobe", probe=probe_video):
    """Publish a verified copy and exact canonical NFO, retaining the source.

    On a filesystem/probe error before publishing both outputs, remove only
    paths created by this invocation. Atlas commits and source deletion are
    deliberately handled separately by the caller.
    """
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    nfo_path = pathlib.Path(nfo_path)
    snapshot = stat_identity(source)
    if destination == source or destination.with_suffix(".nfo") != nfo_path:
        raise ValueError("Invalid canonical destination")
    if os.path.lexists(destination) or os.path.lexists(nfo_path):
        raise FileExistsError("Canonical video or NFO destination already exists")
    prepare_directory(destination, sports_root)
    if os.path.lexists(destination) or os.path.lexists(nfo_path):
        raise FileExistsError("Canonical video or NFO destination already exists")
    created_video = False
    created_nfo = False
    try:
        method = _copy_and_publish(source, destination, snapshot)
        created_video = True
        media = probe(destination, binary=ffprobe)
        ensure_unchanged(source, snapshot)
        if stat_identity(destination)[2] != snapshot[2]:
            raise ValueError("Published video size mismatch")
        _write_exclusive(nfo_path, nfo_xml)
        created_nfo = True
        return media, snapshot, method
    except BaseException:
        # No database writes yet; source is still present. Do not delete a
        # destination we did not create (e.g. a concurrent writer's file).
        if created_nfo:
            nfo_path.unlink(missing_ok=True)
        if created_video:
            destination.unlink(missing_ok=True)
        raise
