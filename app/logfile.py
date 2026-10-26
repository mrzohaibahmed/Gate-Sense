"""A camera worker's log file, rotated by size while the worker runs.

The worker (recognize.py --log-file) replaces sys.stdout and sys.stderr with a RotatingLog, and points the process's
file descriptors 1 and 2 at the same file, so what native libraries (FFmpeg, onnxruntime) print lands there too.
Once the file has reached WORKER_LOG_MAX_BYTES, the next write first makes it <name>.1 (the older ones shift up to
WORKER_LOG_BACKUPS; the oldest is deleted) and starts a new file. Windows can't rename a file that is open, so the
descriptors are pointed at the null device for the moment of the rename.

A rotation that fails (another process has the file open this instant: the dashboard reading the log tail) is
tried again after another ROTATE_RETRY_BYTES; writing never fails because of it.
"""

import io
import os
import sys
import threading
from pathlib import Path

ROTATE_RETRY_BYTES = 64 * 1024


def rotate_files(path: Path, backups: int) -> None:
    """<name> -> <name>.1, <name>.1 -> <name>.2, ... keeping `backups` old files (the oldest is deleted)."""
    path = Path(path)
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for i in range(backups - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
    if path.exists():
        os.replace(path, path.with_name(f"{path.name}.1"))


class RotatingLog(io.TextIOBase):
    """A text stream for sys.stdout/sys.stderr that writes to `path` and rotates it at `max_bytes`."""

    def __init__(self, path: Path, max_bytes: int, backups: int, redirect_fds: bool = True):
        self.path, self.max_bytes, self.backups = Path(path), max_bytes, backups
        self.redirect_fds = redirect_fds
        self.rotations = 0
        self._lock = threading.RLock()
        self._next_check = max_bytes
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115  closed on rotation
        if self.redirect_fds:
            for fd in (1, 2):
                os.dup2(self._file.fileno(), fd)

    def _size(self) -> int:
        return os.fstat(self._file.fileno()).st_size  # also counts what native code wrote through fds 1 and 2

    def write(self, text: str) -> int:
        with self._lock:
            size = self._size()
            if size >= self.max_bytes and size >= self._next_check:
                self._rotate(size)  # before writing, so the current file always holds the newest lines
            self._file.write(text)
            self._file.flush()
        return len(text)

    def _rotate(self, size: int) -> None:
        if self.redirect_fds:  # release the file's other handles, so Windows lets it be renamed
            null = os.open(os.devnull, os.O_WRONLY)
            for fd in (1, 2):
                os.dup2(null, fd)
            os.close(null)
        self._file.close()
        try:
            rotate_files(self.path, self.backups)
            self.rotations += 1
            self._next_check = self.max_bytes
        except OSError:
            self._next_check = size + ROTATE_RETRY_BYTES  # held open elsewhere right now: try again a bit later
        self._open()

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def close(self) -> None:
        with self._lock:
            self._file.close()


def redirect_output(path: Path, max_bytes: int, backups: int) -> RotatingLog:
    """Send this process's output (print, tracebacks, native libraries) to a rotating log at `path`."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
    log = RotatingLog(path, max_bytes, backups)
    sys.stdout = sys.stderr = log
    return log
