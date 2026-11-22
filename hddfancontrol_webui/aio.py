"""Small asyncio helpers shared by the supervisor, the fan controller and the monitor."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


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


# ---------------------------
#   in_daemon_thread
# ---------------------------
async def in_daemon_thread(func: Callable[[], T], name: str = "worker") -> T:
    """Run func in a daemon thread of its own and wait for its result or error.

    Unlike asyncio.to_thread, a call that never returns (an open() on a
    dying drive) holds no thread of the shared pool, and does not keep the
    process from exiting: asyncio.run waits for the pool's threads, never
    for a daemon thread. Cancelling the wait leaves the thread to end by
    itself; its result is dropped.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def settle(result: T | None, error: BaseException | None) -> None:
        if future.done():
            return  # the wait was cancelled
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)  # type: ignore[arg-type]  # None only when error is set

    def run() -> None:
        try:
            result: T | None = func()
            error: BaseException | None = None
        except BaseException as err:  # handed to the waiting task, like asyncio.to_thread does
            result, error = None, err
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:
            pass  # the loop is closed: nobody waits any more

    threading.Thread(target=run, name=name, daemon=True).start()
    return await future
