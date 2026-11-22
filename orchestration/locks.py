"""Host-local, nonblocking advisory locks for PVR provider operations.

The same site has separate indexer and downloader locks. A slow or stalled
download therefore never holds an indexer lock. Distinct sites can progress
independently. Episode locks prevent concurrent workers publishing the same
recording through different sources.

flock files are intentionally retained: unlinking a lock file risks
different processes locking different inodes for the same logical resource.
"""
import contextlib
import fcntl
import hashlib
import os
from pathlib import Path

from orchestration.source_registry import RUGBY_SOURCES, F1_SOURCES

INDEXER_SITES = frozenset((*RUGBY_SOURCES, *F1_SOURCES))
DOWNLOAD_SITES = frozenset((*RUGBY_SOURCES, "transfernow"))


class LockBusy(RuntimeError):
    """A separate process currently owns the requested resource."""


@contextlib.contextmanager
def _lock(root, filename, description):
    path = Path(root) / filename
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(f"Another {description} is active") from exc
        yield


def site_lock(root, kind, site):
    """Lock one remote site and operation; fail quickly if occupied."""
    supported = {"indexer": INDEXER_SITES, "download": DOWNLOAD_SITES}
    if kind not in supported or site not in supported[kind]:
        raise ValueError(f"Invalid site lock: {kind}/{site}")
    # Source identifiers are fixed local literals, not external inputs.
    return _lock(root, f".pvr-{kind}-{site}.lock", f"{kind} for {site}")


def episode_lock(root, episode_id):
    """Prevent concurrent publication/download comparisons for one episode."""
    if not isinstance(episode_id, str) or not episode_id:
        raise ValueError("Invalid episode lock identity")
    digest = hashlib.sha256(episode_id.encode("utf-8")).hexdigest()[:24]
    return _lock(root, f".pvr-episode-{digest}.lock",
                 f"acquisition for episode {episode_id}")
