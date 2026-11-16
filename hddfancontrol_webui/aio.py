"""Small asyncio helpers shared by the supervisor, the fan controller and the monitor."""

from __future__ import annotations

import asyncio
from typing import Any


# ---------------------------
#   cancel_and_wait
# ---------------------------
async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """Cancel a task and wait until it has ended.

    The cancellation of the task itself is expected and not raised; an
    error it ended with is. Unlike awaiting the task inside
    contextlib.suppress(CancelledError), a cancellation of the caller while
    it waits is not swallowed.
    """
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled() and (error := task.exception()) is not None:
        raise error
