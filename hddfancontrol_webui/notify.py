"""Send a notification to ntfy, Gotify or any service that takes a JSON POST.

The address and the format come from the settings (notify_url,
notify_format). One POST per message, with a short timeout: alerts are rare,
and a dead server must not hold anything up.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

NOTIFY_TIMEOUT = 10.0
TITLE = "hddfancontrol"


# ---------------------------
#   NotifyError
# ---------------------------
class NotifyError(RuntimeError):
    """The notification could not be delivered."""


# ---------------------------
#   send
# ---------------------------
async def send(url: str, fmt: str, message: str, urgent: bool) -> None:
    """Send message to url in the format fmt ("ntfy", "gotify" or "json"). Raise NotifyError.

    ntfy: the text as the body, with Title, Priority and Tags headers.
    gotify: JSON with title, message and priority (the token goes in the URL).
    json: JSON with title, message and level ("alert" or "ok"), for webhooks.
    """
    timeout = aiohttp.ClientTimeout(total=NOTIFY_TIMEOUT)
    # Only the host goes to the log: a Gotify token is in the address.
    where = urlsplit(url).netloc or "?"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Never followed: the message (and its headers) would go wherever the server says.
            if fmt == "ntfy":
                headers = {
                    "Title": TITLE,
                    "Priority": "high" if urgent else "default",
                    "Tags": "warning" if urgent else "white_check_mark",
                }
                request = session.post(url, data=message.encode("utf-8"), headers=headers, allow_redirects=False)
            elif fmt == "gotify":
                body = {"title": TITLE, "message": message, "priority": 8 if urgent else 4}
                request = session.post(url, json=body, allow_redirects=False)
            else:
                body = {"title": TITLE, "message": message, "level": "alert" if urgent else "ok"}
                request = session.post(url, json=body, allow_redirects=False)
            async with request as response:
                if response.status >= 300:
                    _LOGGER.warning("The notification server %s answered HTTP %d", where, response.status)
                    raise NotifyError(f"HTTP {response.status}")
    except TimeoutError as err:
        _LOGGER.warning("The notification server %s did not answer in %g s", where, NOTIFY_TIMEOUT)
        raise NotifyError("timeout") from err
    except aiohttp.ClientError as err:
        # The detail (TLS, DNS, refused) only here; the web UI's log and the page get a short reason.
        _LOGGER.warning("Cannot send the notification to %s: %s", where, type(err).__name__ + ": " + _without_query(str(err)))
        raise NotifyError("cannot connect") from err


# ---------------------------
#   _without_query
# ---------------------------
def _without_query(text: str) -> str:
    """The text of an error with any query string of an address cut off, where a token may be."""
    return " ".join(word.split("?", 1)[0] if "://" in word else word for word in text.split())
