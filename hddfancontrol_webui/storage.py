"""Write the small files of /config so that a crash or a power cut never leaves half of one.

The text goes to a temporary file next to the target, which is flushed to
the disk, then renamed over the target; the folder is flushed too, so the
rename itself survives. A reader sees the old file or the new one, never an
empty one. Blocking: call it with asyncio.to_thread from the event loop.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


# ---------------------------
#   write_atomic
# ---------------------------
def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace path with text in one step, with these permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    # Some file systems cannot open or flush a folder; the rename is done anyway.
    with contextlib.suppress(OSError):
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
