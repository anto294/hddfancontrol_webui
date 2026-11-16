"""Cancelling a task and waiting for it."""

from __future__ import annotations

import asyncio

import pytest

from hddfancontrol_webui.aio import cancel_and_wait


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
