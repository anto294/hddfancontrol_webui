"""Whether a newer release of the web UI is out, for the footer of the page.

Once a day (CHECK_PERIOD, the first time FIRST_CHECK_DELAY after the
start) the core asks GitHub's API for the latest release of the
repository (pre-releases are never "latest"). Nothing about the host is
sent: a plain GET with a generic User-Agent. A tag newer than the running
version is kept as {version, url} for the status, and the page shows it
next to its own version. The link is built here from the tag, never taken
from the answer. A build that is not a release ("dev", "edge-<commit>",
"experimental-<commit>") never asks. CHECK_UPDATES=false turns it off.

A failed check is logged once until a check succeeds again, and keeps
what the last one found: the fans never depend on it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

import aiohttp

REPOSITORY = "anto294/hddfancontrol_webui"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/tag/"
CHECK_PERIOD = 24 * 3600.0
FIRST_CHECK_DELAY = 60.0
CHECK_TIMEOUT = 10.0
# The answer of GitHub is a few kilobytes; anything longer is not read.
MAX_ANSWER = 1024 * 1024
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:b(\d+))?")


# ---------------------------
#   version_key
# ---------------------------
def version_key(text: str) -> tuple[int, int, int, float] | None:
    """A key that orders versions (0.12.0, v0.13.0, 0.13.0b1), or None for anything else; a beta comes before its release."""
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    major, minor, patch, beta = match.groups()
    return int(major), int(minor), int(patch), float(beta) if beta is not None else float("inf")


# ---------------------------
#   UpdateCheck
# ---------------------------
class UpdateCheck:
    """Asks GitHub for the latest release once a day; available is the newer one, or None."""

    def __init__(self, current: str, log: Callable[[str], None], url: str = LATEST_URL) -> None:
        """The running version, where to log a failure, and the API address (a test server in the tests)."""
        self._current = version_key(current)
        self._log = log
        self._url = url
        self._failing = False
        self.available: dict[str, str] | None = None

    @property
    def enabled(self) -> bool:
        """Whether the running version is a release, the only kind that is checked."""
        return self._current is not None

    async def check(self) -> None:
        """Ask once; on success, set available to the newer release or None."""
        if self._current is None:
            return
        try:
            tag = await self._latest_tag()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            if not self._failing:
                self._log(f"WARNING: cannot check for a newer release: {type(err).__name__}")
            self._failing = True
            return
        self._failing = False
        key = version_key(tag)
        if key is not None and key > self._current:
            self.available = {"version": tag.removeprefix("v"), "url": RELEASE_PAGE + tag}
        else:
            self.available = None

    async def _latest_tag(self) -> str:
        """The tag of the latest release; ValueError for an answer that names none."""
        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "hddfancontrol-webui"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._url, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                body = b""
                async for chunk in response.content.iter_any():
                    body += chunk
                    if len(body) > MAX_ANSWER:
                        raise ValueError("answer too long")
        data: Any = json.loads(body)
        tag = data.get("tag_name") if isinstance(data, dict) else None
        if not isinstance(tag, str) or version_key(tag) is None or not tag.startswith("v"):
            raise ValueError("no release tag")
        return tag

    async def run(self) -> None:
        """Check after FIRST_CHECK_DELAY, then every CHECK_PERIOD, until cancelled."""
        if self._current is None:
            return
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            await self.check()
            await asyncio.sleep(CHECK_PERIOD)
