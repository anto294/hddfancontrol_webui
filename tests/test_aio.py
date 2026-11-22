"""Cancelling a task and waiting for it; waiting for a function in a daemon thread."""

from __future__ import annotations

import asyncio
import threading

import pytest

from hddfancontrol_webui.aio import cancel_and_wait, in_daemon_thread


async def test_cancel_and_wait() -> None:
    """The task's own cancellation is not raised, its error is, and the caller's cancellation goes through."""
    sleeper = asyncio.create_task(asyncio.sleep(60))
    await cancel_and_wait(sleeper)
    assert sleeper.cancelled()

    async def fail() -> None:
        raise ValueError("boom")

    failed = asyncio.create_task(fail())
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="boom"):
        await cancel_and_wait(failed)

    async def stubborn() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(60)

    slow = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(cancel_and_wait(slow))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(waiter)
    slow.cancel()


async def test_in_daemon_thread() -> None:
    """The result or the error of the function comes back; a thread that hangs is a daemon and the wait can be cancelled."""
    assert await in_daemon_thread(lambda: 7) == 7

    def fail() -> int:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        await in_daemon_thread(fail)

    release, running = threading.Event(), threading.Event()
    seen: list[threading.Thread] = []

    def hang() -> int:
        seen.append(threading.current_thread())
        running.set()
        release.wait(5)
        return 1

    task = asyncio.create_task(in_daemon_thread(hang, name="hang"))
    assert await asyncio.to_thread(running.wait, 5)
    assert seen[0].daemon and seen[0].name == "hang"
    await cancel_and_wait(task)
    assert task.cancelled()
    release.set()  # the thread ends by itself; its result is dropped
    seen[0].join(5)
    await asyncio.sleep(0.01)
