"""Send a notification to ntfy, Gotify or any service that takes a JSON POST.

The address and the format come from the settings (notify_url,
notify_format). One POST per message, with a short timeout: alerts are rare,
and a dead server must not hold anything up.
"""

from __future__ import annotations

import aiohttp

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
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if fmt == "ntfy":
                headers = {
                    "Title": TITLE,
                    "Priority": "high" if urgent else "default",
                    "Tags": "warning" if urgent else "white_check_mark",
                }
                request = session.post(url, data=message.encode("utf-8"), headers=headers)
            elif fmt == "gotify":
                request = session.post(url, json={"title": TITLE, "message": message, "priority": 8 if urgent else 4})
            else:
                request = session.post(url, json={"title": TITLE, "message": message, "level": "alert" if urgent else "ok"})
            async with request as response:
                if response.status >= 300:
                    raise NotifyError(f"HTTP {response.status}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise NotifyError(str(err) or type(err).__name__) from err
