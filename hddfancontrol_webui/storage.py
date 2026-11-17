"""Write the small files of /config so that a crash or a power cut never leaves half of one.

The text goes to a new temporary file with a random name next to the target, which is flushed to
the disk, then renamed over the target; the folder is flushed too, so the
rename itself survives. A reader sees the old file or the new one, never an
empty one. Blocking: call it with asyncio.to_thread from the event loop.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


# ---------------------------
#   write_atomic
# ---------------------------
def write_atomic(path: Path, text: str) -> None:
    """Replace path with text in one step, readable and writable by the owner only.

    Only the web UI itself (root) reads these files; settings and names
    hold no secret, but nothing else on the host needs to read them either.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # A new file with a random name, made with O_EXCL (mkstemp): never a link
    # or a file planted at a known name, which root would write through.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Some file systems cannot open or flush a folder; the rename is done anyway.
    with contextlib.suppress(OSError):
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
