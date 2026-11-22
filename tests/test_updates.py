"""Tests for updates.py: the check for a newer release."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web

from hddfancontrol_webui import updates
from hddfancontrol_webui.updates import RELEASE_PAGE, UpdateCheck, version_key


def test_version_key() -> None:
    """Releases and betas are ordered; anything else is no version."""
    assert version_key("0.12.0") < version_key("v0.13.0b1") < version_key("0.13.0") < version_key("0.13.1")  # type: ignore[operator]
    assert version_key("1.0.0") > version_key("0.99.99")  # type: ignore[operator]
    for other in ("dev", "edge-abc123", "experimental-abc123", "0.12", "0.12.0-rc1", " 0.12.0"):
        assert version_key(other) is None


async def serve(aiohttp_server: Any, answers: list[tuple[int, Any]]) -> tuple[str, list[dict[str, str]]]:
    """A fake GitHub API that gives the answers in turn; returns its address and the headers it got."""
    seen: list[dict[str, str]] = []

    async def latest(request: web.Request) -> web.StreamResponse:
        seen.append(dict(request.headers))
        status, body = answers.pop(0)
        if isinstance(body, bytes):
            return web.Response(status=status, body=body)
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/latest", latest)
    server = await aiohttp_server(app)
    return str(server.make_url("/latest")), seen


async def test_newer_release_is_found(aiohttp_server: Any) -> None:
    """A newer tag is offered with a link built from it; the same or an older one is not."""
    url, seen = await serve(
        aiohttp_server,
        [
            (200, {"tag_name": "v0.13.0", "html_url": "https://evil.example/"}),
            (200, {"tag_name": "v0.12.0"}),
            (200, {"tag_name": "v0.11.0"}),
        ],
    )
    log: list[str] = []
    check = UpdateCheck("0.12.0", log.append, url=url)
    assert check.enabled
    await check.check()
    assert check.available == {"version": "0.13.0", "url": RELEASE_PAGE + "v0.13.0"}
    await check.check()
    assert check.available is None
    await check.check()
    assert check.available is None
    assert log == []
    assert seen[0]["User-Agent"] == "hddfancontrol-webui"


async def test_beta_sees_its_release(aiohttp_server: Any) -> None:
    """A beta is offered the release it led to."""
    url, _ = await serve(aiohttp_server, [(200, {"tag_name": "v0.13.0"})])
    check = UpdateCheck("0.13.0b2", lambda text: None, url=url)
    await check.check()
    assert check.available is not None and check.available["version"] == "0.13.0"


@pytest.mark.parametrize(
    "answer",
    [
        (500, {"tag_name": "v9.0.0"}),
        (200, {"tag_name": "9.0.0"}),  # tags start with v
        (200, {"tag_name": "v9.0.0/../x"}),
        (200, {"name": "v9.0.0"}),
        (200, ["v9.0.0"]),
        (200, b"not json"),
        (200, b"x" * (updates.MAX_ANSWER + 1)),
    ],
)
async def test_bad_answers_are_failures(aiohttp_server: Any, answer: tuple[int, Any]) -> None:
    """An answer without a well-formed tag is a failed check: logged once, what was found is kept."""
    url, _ = await serve(aiohttp_server, [(200, {"tag_name": "v0.13.0"}), answer, answer, (200, {"tag_name": "v0.14.0"})])
    log: list[str] = []
    check = UpdateCheck("0.12.0", log.append, url=url)
    await check.check()
    await check.check()
    await check.check()
    assert check.available == {"version": "0.13.0", "url": RELEASE_PAGE + "v0.13.0"}
    assert len(log) == 1 and log[0].startswith("WARNING: cannot check for a newer release")
    await check.check()
    assert check.available is not None and check.available["version"] == "0.14.0"


async def test_unreachable_server_is_a_failure() -> None:
    """A refused connection is logged, never raised."""
    log: list[str] = []
    check = UpdateCheck("0.12.0", log.append, url="http://127.0.0.1:9/latest")
    await check.check()
    assert check.available is None and len(log) == 1


async def test_builds_that_are_no_release_never_ask(aiohttp_server: Any) -> None:
    """dev, edge and experimental builds do not check."""
    url, seen = await serve(aiohttp_server, [(200, {"tag_name": "v9.0.0"})])
    for version in ("dev", "edge-abc", "experimental-abc"):
        check = UpdateCheck(version, lambda text: None, url=url)
        assert not check.enabled
        await check.check()
        await check.run()  # returns at once
        assert check.available is None
    assert seen == []


async def test_run_checks_after_a_delay_then_daily(aiohttp_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """run waits FIRST_CHECK_DELAY, then checks every CHECK_PERIOD until cancelled."""
    url, seen = await serve(aiohttp_server, [(200, {"tag_name": "v0.13.0"})] * 3)
    monkeypatch.setattr(updates, "FIRST_CHECK_DELAY", 0.0)
    monkeypatch.setattr(updates, "CHECK_PERIOD", 0.05)
    check = UpdateCheck("0.12.0", lambda text: None, url=url)
    task = asyncio.create_task(check.run())
    for _ in range(200):
        if len(seen) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) >= 2 and check.available is not None
