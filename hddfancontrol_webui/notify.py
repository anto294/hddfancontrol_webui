"""Send a notification to ntfy, Gotify or any service that takes a JSON POST.

The address and the format come from the settings (notify_url,
notify_format). One POST per message, with a short timeout: alerts are rare,
and a dead server must not hold anything up.

The POST comes from the NAS's network, as root's process: it is never sent
to the NAS itself (loopback), to link-local addresses (169.254.0.0/16, the
metadata service of a cloud; fe80::/10), to unspecified or multicast ones,
nor to IPv4 addresses mapped on these (forbidden_ip). The check is made on
the address the connection itself uses (_GuardedResolver, for a name; the
literal itself, for an IP), so a DNS server that answers differently the
second time cannot slip past it. Private networks stay allowed: ntfy and
Gotify often run on the LAN.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

_LOGGER = logging.getLogger(__name__)

NOTIFY_TIMEOUT = 10.0
TITLE = "hddfancontrol"
# The error codes of NotifyError, translated as error.<code> by the page.
NOTIFY_FAILED = "notify_failed"
NOTIFY_FORBIDDEN = "notify_address_forbidden"
# "This network": Linux connects 0.0.0.x to the host itself.
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
# Makes the resolver behind _GuardedResolver (a name for the tests to replace).
_new_resolver = aiohttp.DefaultResolver


# ---------------------------
#   NotifyError
# ---------------------------
class NotifyError(RuntimeError):
    """The notification could not be delivered; code says why, for the page (error.<code>)."""

    def __init__(self, text: str, code: str = NOTIFY_FAILED) -> None:
        """A short reason for the log, and the code of the page."""
        super().__init__(text)
        self.code = code


# ---------------------------
#   ForbiddenAddress
# ---------------------------
class ForbiddenAddress(OSError):
    """A notification address that is (or resolves only to) a forbidden_ip."""


# ---------------------------
#   forbidden_ip
# ---------------------------
def forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether a notification may never go to ip: loopback, link-local, unspecified (0.0.0.0/8) or multicast; IPv4 mapped too."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK)
    )


# ---------------------------
#   literal_ip
# ---------------------------
def literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address a host name of a URL is written as (without an IPv6 zone), or None for a name.

    The old ways to write IPv4 that the C library still takes (127.1,
    2130706433, 0x7f.1) count too: older aiohttp passes them on to it.
    """
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fA-Fx.]+", host) and host[0].isdigit():
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


# ---------------------------
#   forbidden_host
# ---------------------------
def forbidden_host(host: str) -> bool:
    """Whether a host of a URL is known to be forbidden without a lookup: a forbidden_ip literal, or localhost."""
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    ip = literal_ip(host)
    return ip is not None and forbidden_ip(ip)


# ---------------------------
#   _GuardedResolver
# ---------------------------
class _GuardedResolver(AbstractResolver):
    """Resolve names with aiohttp's resolver, keeping only the addresses forbidden_ip allows.

    The connector connects to what this returns: the check and the
    connection use the same answer of the DNS.
    """

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        """Ask inner (aiohttp's default resolver) for the addresses."""
        self._inner = inner if inner is not None else _new_resolver()

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        """The allowed addresses of host; ForbiddenAddress when there is none."""
        results = await self._inner.resolve(host, port, family)
        kept = [r for r in results if (ip := literal_ip(r["host"])) is not None and not forbidden_ip(ip)]
        if not kept:
            raise ForbiddenAddress(f"{host} is the NAS itself or a link-local, unspecified or multicast address")
        return kept

    async def close(self) -> None:
        """Close the inner resolver."""
        await self._inner.close()


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
    where = _where(url)
    if forbidden_host(urlsplit(url).hostname or ""):
        _LOGGER.warning("Not sending the notification to %s: a forbidden address (the NAS itself, or link-local)", where)
        raise NotifyError("forbidden address", NOTIFY_FORBIDDEN)
    try:
        connector = aiohttp.TCPConnector(resolver=_GuardedResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
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
        if _forbidden_cause(err):
            _LOGGER.warning("Not sending the notification to %s: it resolves to a forbidden address", where)
            raise NotifyError("forbidden address", NOTIFY_FORBIDDEN) from err
        # The detail (TLS, DNS, refused) only here; the web UI's log and the page get a short reason.
        _LOGGER.warning("Cannot send the notification to %s: %s", where, type(err).__name__ + ": " + _without_query(str(err)))
        raise NotifyError("cannot connect") from err


# ---------------------------
#   _forbidden_cause
# ---------------------------
def _forbidden_cause(err: BaseException) -> bool:
    """Whether an error of aiohttp comes from _GuardedResolver refusing every address."""
    seen: BaseException | None = err
    while seen is not None:
        if isinstance(seen, ForbiddenAddress) or isinstance(getattr(seen, "os_error", None), ForbiddenAddress):
            return True
        seen = seen.__cause__
    return False


# ---------------------------
#   _where
# ---------------------------
def _where(url: str) -> str:
    """The host and port of an address, for the log: never its user, password, path or query (tokens live there)."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "?"
    if not host:
        return "?"
    host = f"[{host}]" if ":" in host else host
    return f"{host}:{port}" if port is not None else host


# ---------------------------
#   _without_query
# ---------------------------
def _without_query(text: str) -> str:
    """The text of an error with the query string and the user and password of any address cut off, where tokens may be."""
    return " ".join(re.sub(r"://[^/@\s]*@", "://", word.split("?", 1)[0]) if "://" in word else word for word in text.split())
