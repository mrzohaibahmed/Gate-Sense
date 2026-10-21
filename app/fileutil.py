"""Atomic file writes: readers (the dashboard, a running worker) never see a half-written file."""

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


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    replace_retry(tmp, path)
