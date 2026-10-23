"""Atomic file writes: readers (the dashboard, a running worker) never see a half-written file.

Durable writes (the default) also flush the new file to disk before it replaces the old one, so a power cut
leaves either the old file or the new one, never an empty or partly written file under the real name. Files
that are rewritten many times a second and mean nothing after a restart (live frames, status files) pass
durable=False: they skip the flush, which would slow the camera loop.
"""

import os
import time
from pathlib import Path


def replace_retry(tmp: Path, path: Path, attempts: int = 20, delay: float = 0.05) -> None:
    """os.replace, retried: Windows refuses the replace while another process has the target open."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                Path(tmp).unlink(missing_ok=True)
                raise
            time.sleep(delay)


def sync_dir(folder: Path) -> None:
    """Flush a folder's entries (the rename) to disk. POSIX only: Windows can't open a folder for this, and NTFS
    journals the rename itself."""
    if os.name == "nt":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, durable: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        if durable:
            f.flush()
            os.fsync(f.fileno())
    replace_retry(tmp, path)
    if durable:
        sync_dir(path.parent)


class ErrorLog:
    """Prints a recurring error (a failed live-frame or status write) the first time and then at most once every
    `every` seconds with how many happened, so a full disk or a locked file never floods the worker log."""

    def __init__(self, what: str, every: float = 60.0, clock=time.monotonic, log=print):
        self.what, self.every, self.clock, self.log = what, every, clock, log
        self.count = 0  # failures since the last message
        self.total = 0
        self._last: float | None = None

    def __call__(self, error: BaseException) -> None:
        self.count += 1
        self.total += 1
        now = self.clock()
        if self._last is None or now - self._last >= self.every:
            more = f" ({self.count} since the last message)" if self.count > 1 else ""
            self.log(f"WARNING: {self.what} failed: {type(error).__name__}: {error}{more}; continuing")
            self._last, self.count = now, 0
