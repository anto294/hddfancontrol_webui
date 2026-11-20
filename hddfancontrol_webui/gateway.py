"""The gateway: the only part of the web UI that the network reaches, run without privileges.

The core (__main__.py, web.py) runs as root, since it writes the PWM files
and sends commands to the drives, often in a privileged container. It only
listens on a Unix socket that this gateway can open. The gateway, started by
the core as WEBUI_USER, holds everything a client reaches before logging in:

- the port, plain HTTP and HTTPS (tls.py), and WEBUI_ALLOW, the networks
  allowed to connect (private ones by default);
- the login: sessions, HTTP Basic credentials for scripts, the limit on
  wrong passwords, and the credentials themselves (auth.py, in /config/web,
  which only this user can read);
- the first setup: without credentials nothing but the setup page answers,
  and creating them needs a one-time code that the gateway writes in the log
  of the container, so that only someone who can read that log can claim a
  new installation;
- FORCE_HTTPS, the security headers, the CSRF check and the static files.

What it lets through goes to the core with the user and the client address
(HEADER_USER, HEADER_CLIENT); forward() builds the headers for the core from
scratch (FORWARD_HEADERS only), so a client cannot send its own. A bug before the login can therefore reach this process only,
which cannot write the fans, read the drives or read the rest of /config.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import mimetypes
import os
import re
import secrets
import signal
import ssl
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from .auth import SESSION_COOKIE, SESSION_LIFETIME, AuthError, AuthStore
from .const import (
    CORE_SOCKET_ENV,
    DEFAULT_PORT,
    HEADER_CLIENT,
    HEADER_USER,
    VERSION,
    WEB_DIR_ENV,
    env_flag,
)
from .httputil import json_error, read_json
from .tls import DualListener, TlsError, ssl_context

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

STATIC_DIR = Path(__file__).parent / "static"
# The web app manifest (install on a phone's home screen); Python does not know its type.
mimetypes.add_type("application/manifest+json", ".webmanifest")
CSRF_HEADER = "X-Requested-With"
AUTH_FAILURE_DELAY = 1.0
# Reachable without a login: the login page and what it loads, and /healthz.
PUBLIC_PATHS = (
    "/healthz",
    "/login",
    "/api/login",
    "/api/setup",
    "/api/auth",
    "/static/style.css",
    "/static/login.js",
    "/static/i18n.js",
    "/static/icons.js",
    "/static/https.js",
    "/static/logo.png",
    "/static/favicon.png",
    "/static/apple-touch-icon.png",
    "/static/icon-512.png",
    "/static/manifest.webmanifest",
)
PUBLIC_PREFIXES = ("/static/i18n/",)
# After this many wrong passwords (or setup codes) from one address within
# LOGIN_WINDOW seconds, that address is refused without checking until the
# window ends. An IPv6 /64 that sent as many is slowed down instead (see
# LoginLimiter.delay): a host holds as many addresses of its /64 as it
# likes, but a whole LAN often shares one, and nobody may lock the owner out.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW = 900.0
LOGIN_MAX_ADDRESSES = 10000
# After this many wrong passwords from all clients together within
# LOGIN_WINDOW, the password checks of clients that sent a wrong one
# lately are slowed down, one at a time, by a delay that grows with the
# failures past the limit: LOGIN_SLOW_BASE seconds, doubled every
# LOGIN_SLOW_STEP failures, at most LOGIN_SLOW_MAX. A host with many IPv4
# addresses (or /64s) cannot guess at full speed, and nobody is refused:
# a client without a recent failure (the owner typing the right password)
# is not slowed down. Open sessions are not affected.
LOGIN_MAX_GLOBAL = 100
LOGIN_SLOW_BASE = 2.0
LOGIN_SLOW_STEP = 25
LOGIN_SLOW_MAX = 30.0
# How many global failures are remembered: enough for the delay to reach its maximum.
LOGIN_GLOBAL_KEEP = 1000
# What plain HTTP still serves with FORCE_HTTPS: the health check, and the page that moves the browser to HTTPS.
HTTP_PATHS = ("/healthz", "/static/https.js", "/static/style.css")
# The networks allowed by default: loopback, private, link-local, carrier-grade NAT
# (Tailscale and other VPNs) and unique local IPv6. WEBUI_ALLOW replaces them.
DEFAULT_ALLOW = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)
# The host names a browser may use without WEBUI_HOSTS: names of the LAN and
# of the usual routers and VPNs. An Internet name can be pointed at the NAS
# by anyone (DNS rebinding): a page of theirs would then talk to the web UI
# from the browser of someone on the LAN.
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".fritz.box", ".home", ".ts.net")
# A Host header: a name (letters, digits, dots, hyphens) or an IPv6 address in brackets, then maybe a port.
_HOST = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9.-]{1,253}))(?::[0-9]{1,5})?$")
REFUSED_LOG_PERIOD = 3600.0
# The endpoints the gateway answers itself.
GATEWAY_API = ("/api/login", "/api/logout", "/api/setup", "/api/auth")
# Requests to the core: the slowest are a SMART read of every drive and a notification test.
CORE_TIMEOUT_S = 120.0
CLIENT_MAX_SIZE = 64 * 1024
# Headers passed from the client to the core, and back.
FORWARD_HEADERS = ("Content-Type", "Accept")
RETURN_HEADERS = ("Content-Type", "Content-Disposition", "Cache-Control")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "payment=(), usb=(), serial=(), bluetooth=(), hid=(), midi=(), interest-cohort=()"
)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


# ---------------------------
#   TooManyAttempts
# ---------------------------
class TooManyAttempts(Exception):
    """The client is over the limit of wrong passwords, or already has a check under way: 429."""


# ---------------------------
#   LoginLimiter
# ---------------------------
class LoginLimiter:
    """Count the wrong passwords of each address, IPv6 /64 and of all of them.

    An address past its limit is refused; a /64 past it, or all clients
    past the global limit, are slowed down only. The scrypt checks already
    run one at a time; this stops one client from guessing for hours, and
    from keeping the checks busy for everyone else, without letting a
    neighbour of the same /64 lock the owner out. Behind a reverse proxy
    every client has the address of the proxy.
    """

    def __init__(
        self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW, max_global: int = LOGIN_MAX_GLOBAL
    ) -> None:
        """Allow max_failures wrong passwords per client, and max_global in all, within window seconds."""
        self._max = max_failures
        self._max_global = max_global
        self._window = window
        self._failures: dict[str, list[float]] = {}
        # The failures of each IPv6 /64, which only slow down its addresses.
        self._networks: dict[str, list[float]] = {}
        self._global: list[float] = []
        # The clients with a password check waiting or running.
        self._busy: set[str] = set()

    def begin(self, address: str) -> bool:
        """Start a password check of the client of address; False when it is blocked or has one under way."""
        key = self.client(address)
        if key in self._busy or self.blocked(address):
            return False
        self._busy.add(key)
        return True

    def end(self, address: str) -> None:
        """The password check of the client of address is over."""
        self._busy.discard(self.client(address))

    @staticmethod
    def client(address: str) -> str:
        """The client an address belongs to: the address itself, written one way (or the name given).

        An IPv4-mapped address (::ffff:a.b.c.d, from a dual-stack socket) is
        the IPv4 client, as in allowed().
        """
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return address
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ip)

    @staticmethod
    def network(address: str) -> str | None:
        """The IPv6 /64 of an address, or None (IPv4, IPv4-mapped, a name)."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        if not isinstance(ip, ipaddress.IPv6Address) or ip.ipv4_mapped is not None:
            return None
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))

    def _recent(self, key: str, now: float, table: dict[str, list[float]] | None = None) -> list[float]:
        """The failures of a client (or, with table, of a /64) within the window."""
        table = self._failures if table is None else table
        recent = [t for t in table.get(key, []) if now - t < self._window]
        if recent:
            table[key] = recent
        else:
            table.pop(key, None)
        return recent

    def _recent_global(self, now: float) -> int:
        """How many failures of all clients are within the window (at most LOGIN_GLOBAL_KEEP)."""
        self._global = [t for t in self._global if now - t < self._window][-LOGIN_GLOBAL_KEEP:]
        return len(self._global)

    def blocked(self, address: str) -> bool:
        """Whether the client of address sent too many wrong passwords lately."""
        return len(self._recent(self.client(address), time.monotonic())) >= self._max

    def delay(self, address: str) -> float:
        """How long the next password check of address waits; the longer of the global and the /64 delay.

        Global: 0 below the global limit or for a client without failures;
        past it, the delay doubles every LOGIN_SLOW_STEP failures, from
        LOGIN_SLOW_BASE to LOGIN_SLOW_MAX seconds. When the table of
        clients is full, a client it does not hold may have failed too: it
        waits like one that did. /64: once its addresses sent max_failures
        wrong passwords, every address of it waits LOGIN_SLOW_BASE, doubled
        for every max_failures more, at most LOGIN_SLOW_MAX: a guesser that
        takes a new address for each try is slowed, its neighbours never
        refused.
        """
        now = time.monotonic()
        waits = [0.0]
        if (network := self.network(address)) is not None:
            excess = len(self._recent(network, now, self._networks)) - self._max
            if excess >= 0:
                waits.append(min(LOGIN_SLOW_MAX, LOGIN_SLOW_BASE * 2 ** (excess // self._max)))
        excess = self._recent_global(now) - self._max_global
        if excess >= 0 and (self._recent(self.client(address), now) or len(self._failures) >= LOGIN_MAX_ADDRESSES):
            waits.append(min(LOGIN_SLOW_MAX, LOGIN_SLOW_BASE * 2 ** (excess // LOGIN_SLOW_STEP)))
        return float(max(waits))

    def failed(self, address: str) -> bool:
        """Count a wrong password of address; True when it is the one that starts slowing down the checks."""
        now = time.monotonic()
        self._global.append(now)
        stopped = self._recent_global(now) == self._max_global
        network = self.network(address)
        if network is not None:
            self._count(self._networks, network, now)
        self._count(self._failures, self.client(address), now)
        return stopped

    def _count(self, table: dict[str, list[float]], key: str, now: float) -> None:
        """Add a failure of key to table, unless the table is full of recent failures (the global count still holds it)."""
        if key not in table and len(table) >= LOGIN_MAX_ADDRESSES:
            for other in list(table):
                self._recent(other, now, table)
            if len(table) >= LOGIN_MAX_ADDRESSES:
                return
        table.setdefault(key, []).append(now)

    def succeeded(self, address: str) -> None:
        """Forget the failures of address after a right password; not those of its /64, which may hold a guesser."""
        self._failures.pop(self.client(address), None)


# ---------------------------
#   SetupCode
# ---------------------------
class SetupCode:
    """The one-time code that creating the first credentials needs, while there are none.

    It is made at the start of the gateway and written to its log only: the
    port may be reached by anyone on the network, the log only by whoever
    runs the container. It ends when the credentials are set.
    """

    def __init__(self) -> None:
        """Make a code of 16 hex digits (64 bits), in groups of four."""
        raw = secrets.token_hex(8)
        self.code: str | None = "-".join(raw[i : i + 4] for i in range(0, 16, 4))

    def check(self, given: str) -> bool:
        """Whether the code given is this one (dashes, spaces and case do not matter). Constant time."""
        if self.code is None:
            return False
        clean = "".join(c for c in given.lower() if c in "0123456789abcdef")
        return hmac.compare_digest(clean.encode(), self.code.replace("-", "").encode())

    def used(self) -> None:
        """The credentials are set: the code is over."""
        self.code = None


AUTH: web.AppKey[AuthStore] = web.AppKey("auth", AuthStore)
LOGINS: web.AppKey[LoginLimiter] = web.AppKey("logins", LoginLimiter)
SETUP: web.AppKey[SetupCode] = web.AppKey("setup", SetupCode)
PASSWORD_CHECKS: web.AppKey[asyncio.Semaphore] = web.AppKey("password_checks", asyncio.Semaphore)
SLOW_CHECKS: web.AppKey[asyncio.Lock] = web.AppKey("slow_checks", asyncio.Lock)
FORCE_HTTPS: web.AppKey[bool] = web.AppKey("force_https", bool)
ALLOW: web.AppKey[tuple[Network, ...]] = web.AppKey("allow", tuple)
CORE: web.AppKey[aiohttp.ClientSession] = web.AppKey("core", aiohttp.ClientSession)
LOG: web.AppKey[Callable[[str], None]] = web.AppKey("log")
REFUSED: web.AppKey[dict[str, float]] = web.AppKey("refused", dict)
HOSTS: web.AppKey[tuple[str, ...] | None] = web.AppKey("hosts")
REFUSED_HOSTS: web.AppKey[dict[str, float]] = web.AppKey("refused_hosts", dict)


# ---------------------------
#   parse_allow
# ---------------------------
def parse_allow(text: str | None) -> tuple[Network, ...]:
    """The networks of WEBUI_ALLOW (CIDRs or addresses, by commas or spaces); empty means DEFAULT_ALLOW.

    "private" stands for DEFAULT_ALLOW, so "private 2a02:…::/64" adds a
    network to them; "all" is every address. Raises ValueError on a value
    that is not a network.
    """
    items = (text or "").replace(",", " ").split() or ["private"]
    networks: list[Network] = []
    for item in items:
        word = item.lower()
        if word == "all":
            networks += [ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")]
        elif word == "private":
            networks += [ipaddress.ip_network(net) for net in DEFAULT_ALLOW]
        else:
            networks.append(ipaddress.ip_network(item, strict=False))
    return tuple(networks)


# ---------------------------
#   allowed
# ---------------------------
def allowed(address: str | None, networks: tuple[Network, ...]) -> bool:
    """Whether a client address is loopback or in one of the networks; an IPv4 address mapped in IPv6 counts as IPv4."""
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # The machine itself always: the health check of the container comes from 127.0.0.1.
    return ip.is_loopback or any(ip.version == net.version and ip in net for net in networks)


# ---------------------------
#   refused_hint
# ---------------------------
def refused_hint(address: str) -> str:
    """What to add to WEBUI_ALLOW for a refused address that may well be on the LAN: a global IPv6 one.

    A LAN with IPv6 gives its hosts global addresses too, and a browser
    prefers them when the NAS's name has an AAAA record. They cannot be
    allowed by default (they are Internet addresses), so the log names
    the /64 to allow.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return ""
    if ip.version != 6 or not ip.is_global:
        return ""
    network = ipaddress.ip_network(f"{ip}/64", strict=False)
    return f" (a global IPv6 address: if it is your LAN's, set WEBUI_ALLOW=private {network})"


# ---------------------------
#   parse_hosts
# ---------------------------
def parse_hosts(text: str | None) -> tuple[str, ...] | None:
    """The host names of WEBUI_HOSTS (by commas or spaces), lower case; "*.example.com" stands for its subdomains.

    None ("any") turns the check off. Raises ValueError on a value that is
    not a host name.
    """
    names: list[str] = []
    for item in (text or "").replace(",", " ").split():
        word = item.lower().rstrip(".")
        if word == "any":
            return None
        bare = word[2:] if word.startswith("*.") else word
        match = _HOST.match(bare)
        if match is None or match.group("name") is None or ":" in bare:
            raise ValueError(f"{item!r} is not a host name")
        names.append(word)
    return tuple(names)


# ---------------------------
#   host_of
# ---------------------------
def host_of(header: str | None) -> str | None:
    """The host name or address of a Host header, lower case and without the port; None when it is not one."""
    match = _HOST.match((header or "").strip())
    if match is None:
        return None
    if match.group("v6") is not None:
        try:
            return str(ipaddress.IPv6Address(match.group("v6")))
        except ValueError:
            return None
    return match.group("name").lower().rstrip(".") or None


# ---------------------------
#   host_allowed
# ---------------------------
def host_allowed(host: str, names: tuple[str, ...] | None) -> bool:
    """Whether a browser may use this host name: an IP address, a name of the LAN, or one of WEBUI_HOSTS."""
    if names is None:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES):
        return True
    return any(host == name or (name.startswith("*.") and host.endswith(name[1:])) for name in names)


# ---------------------------
#   _is_https
# ---------------------------
def _is_https(request: web.Request) -> bool:
    """Whether the browser reached the web UI over HTTPS, directly or through a reverse proxy.

    The proxy says so in X-Forwarded-Proto. Trusting it only decides
    whether the session cookie is marked Secure and whether FORCE_HTTPS
    lets the request in: a client that fakes it gets a cookie its own
    browser does not send back over HTTP.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    return request.secure or forwarded == "https"


# ---------------------------
#   _password_work
# ---------------------------
async def _password_work(request: web.Request, address: str, func: Callable[..., Any], *args: Any) -> Any:
    """Run a password check or change for the client of address in a thread, one at a time.

    Each scrypt hash takes 16 MiB. Without a limit, a burst of wrong logins
    (no session needed) runs one per worker thread and can push the
    container past its memory limit. One at a time also slows down guessing.

    A client has at most one check waiting or running: a burst of parallel
    guesses would otherwise all pass the limit before the first failure is
    counted. The limit is checked again once it is this check's turn.
    Raises TooManyAttempts for both.
    """
    logins = request.app[LOGINS]
    if not logins.begin(address):
        raise TooManyAttempts()
    try:
        if wait := logins.delay(address):
            # Past the global limit: the slowed checks wait one after the other,
            # outside the queue of the checks, so a client without failures goes first.
            async with request.app[SLOW_CHECKS]:
                await asyncio.sleep(wait)
        async with request.app[PASSWORD_CHECKS]:
            if logins.blocked(address):
                raise TooManyAttempts()
            return await asyncio.to_thread(func, *args)
    finally:
        logins.end(address)


# ---------------------------
#   _log
# ---------------------------
def _log(request: web.Request, text: str) -> None:
    """Log a security event, with the client address."""
    request.app[LOG](f"{text} (from {request.remote or '?'})")


# ---------------------------
#   _failed_login
# ---------------------------
def _failed_login(request: web.Request, address: str) -> None:
    """Count a wrong password (or setup code) of address, and log when it starts slowing down the checks."""
    if request.app[LOGINS].failed(address):
        request.app[LOG](
            f"{LOGIN_MAX_GLOBAL} wrong passwords in {int(LOGIN_WINDOW // 60)} minutes from all clients: "
            "the logins of clients that sent a wrong one are slowed down (open sessions go on)"
        )


# ---------------------------
#   network_filter
# ---------------------------
@web.middleware
async def network_filter(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Answer only the addresses of WEBUI_ALLOW; the others get a bare 403, logged once an hour.

    _serve already closes their connections at accept (admit_address):
    this is the same rule again, for any other way a request comes in.
    """
    if admit_address(request.app, request.remote or ""):
        return await handler(request)
    return web.Response(status=403, text="Forbidden")


# ---------------------------
#   admit_address
# ---------------------------
def admit_address(app: web.Application, address: str) -> bool:
    """Whether a client address is in WEBUI_ALLOW (or loopback); a refused one is logged once an hour."""
    if allowed(address, app[ALLOW]):
        return True
    address = address or "?"
    refused = app[REFUSED]
    now = time.monotonic()
    if now - refused.get(address, -REFUSED_LOG_PERIOD) >= REFUSED_LOG_PERIOD:
        if len(refused) > LOGIN_MAX_ADDRESSES:
            refused.clear()
        refused[address] = now
        app[LOG](f"Refused {address}: not in WEBUI_ALLOW{refused_hint(address)}")
    return False


# ---------------------------
#   host_check
# ---------------------------
@web.middleware
async def host_check(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Answer only the host names of host_allowed: a bare 403 for others (logged once an hour per name), 400 without one.

    Against DNS rebinding: a page of a site whose name was pointed at the
    NAS reaches it from the browser with that name in the Host header.
    /healthz answers whatever the name (the health check of Docker).
    """
    if request.path == "/healthz":
        return await handler(request)
    host = host_of(request.headers.get("Host"))
    if host is None:
        return web.Response(status=400, text="Bad Request")
    if host_allowed(host, request.app[HOSTS]):
        return await handler(request)
    refused = request.app[REFUSED_HOSTS]
    now = time.monotonic()
    if now - refused.get(host, -REFUSED_LOG_PERIOD) >= REFUSED_LOG_PERIOD:
        if len(refused) > LOGIN_MAX_ADDRESSES:
            refused.clear()
        refused[host] = now
        request.app[LOG](
            f"Refused the host name {host[:100]!r}: not a name of the LAN (if it is yours, set WEBUI_HOSTS={host[:100]})"
        )
    return web.Response(status=403, text="Forbidden")


# ---------------------------
#   security_headers
# ---------------------------
@web.middleware
async def security_headers(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Forbid framing, inline scripts, content sniffing and cross-origin use on every answer."""
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
    if request.path.startswith("/api/") or request.path == "/metrics":
        headers.setdefault("Cache-Control", "no-store")
    else:
        # Keep the pages and static files, but ask every time (a cheap 304):
        # after an update the browser must not run the old app.js.
        headers.setdefault("Cache-Control", "no-cache")
    if isinstance(response, web.HTTPException):
        raise response
    return response


# ---------------------------
#   https_only
# ---------------------------
@web.middleware
async def https_only(request: web.Request, handler: Handler) -> web.StreamResponse:
    """With FORCE_HTTPS, keep everything but HTTP_PATHS off plain HTTP.

    A page gets https.html, whose script sends the browser to the same
    address over HTTPS, built from the browser's own location: the server
    never builds a redirect from the Host header, which the client sets.
    The API and /metrics answer 403 https_required. No
    Strict-Transport-Security: with a self-signed certificate it would
    turn the warning a browser lets you pass into an error it does not.
    """
    if not request.app[FORCE_HTTPS] or _is_https(request) or request.path in HTTP_PATHS:
        return await handler(request)
    if request.path.startswith("/api/") or request.path == "/metrics" or request.method != "GET":
        return json_error(403, "https_required")
    return web.FileResponse(STATIC_DIR / "https.html", headers={"Cache-Control": "no-store"})


# ---------------------------
#   authentication
# ---------------------------
@web.middleware
async def authentication(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Let a request through with a session or Basic credentials; without credentials, only the setup.

    A path with a "." or ".." part (also written %2e or after %2f) gets a
    404: the public paths are matched as written, so /static/i18n/../app.js
    would pass as a translation and then be served as app.js.
    """
    auth = request.app[AUTH]
    path = request.path
    parts = path.split("/")
    if "." in parts or ".." in parts:
        raise web.HTTPNotFound()
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await handler(request)
    if not auth.enabled:
        # First start: nothing but the setup page until the credentials are made.
        if path.startswith("/api/") or path == "/metrics":
            return json_error(401, "setup_required")
        raise web.HTTPFound("/login")
    if auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        return await handler(request)
    header = request.headers.get("Authorization", "")
    if header:
        logins = request.app[LOGINS]
        address = request.remote or ""
        if logins.blocked(address):
            return json_error(429, "too_many_attempts")
        try:
            right = await _password_work(request, address, auth.check_basic, header)
        except TooManyAttempts:
            return json_error(429, "too_many_attempts")
        if right:
            logins.succeeded(address)
            return await handler(request)
        _failed_login(request, address)
        _log(request, "Wrong HTTP Basic credentials")
        await asyncio.sleep(AUTH_FAILURE_DELAY)
    # Programs (the API, a Prometheus scraper) get 401; a browser goes to the login page.
    if path.startswith("/api/") or path == "/metrics":
        return json_error(401, "unauthorized")
    raise web.HTTPFound("/login")


# ---------------------------
#   csrf
# ---------------------------
@web.middleware
async def csrf(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Refuse a change that a page of another site could have sent.

    A change needs the X-Requested-With header, which a page of another
    site cannot send without a CORS preflight (never accepted here), and,
    when the browser sends one, an Origin that matches the host.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await handler(request)
    if request.headers.get(CSRF_HEADER) != "fetch":
        return json_error(403, "csrf")
    origin = request.headers.get("Origin")
    if origin is not None and urlsplit(origin).netloc != request.host:
        return json_error(403, "csrf")
    return await handler(request)


# ---------------------------
#   _open_session
# ---------------------------
def _open_session(request: web.Request, response: web.Response) -> None:
    """Give the client a new session cookie."""
    response.set_cookie(
        SESSION_COOKIE,
        request.app[AUTH].new_session(),
        max_age=SESSION_LIFETIME,
        httponly=True,
        samesite="Strict",
        secure=_is_https(request),
        path="/",
    )


# ---------------------------
#   index
# ---------------------------
async def index(request: web.Request) -> web.FileResponse:
    """Serve the page."""
    return web.FileResponse(STATIC_DIR / "index.html")


# ---------------------------
#   login_page
# ---------------------------
async def login_page(request: web.Request) -> web.StreamResponse:
    """Serve the login page (also the first setup), or the main page with a session."""
    auth = request.app[AUTH]
    if auth.enabled and auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        raise web.HTTPFound("/")
    return web.FileResponse(STATIC_DIR / "login.html")


# ---------------------------
#   healthz
# ---------------------------
async def healthz(request: web.Request) -> web.Response:
    """Answer the health check of Docker: ok when the core answers too. No login."""
    try:
        async with request.app[CORE].get("http://core/healthz", timeout=aiohttp.ClientTimeout(total=10)) as response:
            healthy = response.status == 200
    except (aiohttp.ClientError, TimeoutError):
        healthy = False
    return web.Response(text="ok") if healthy else web.Response(status=503, text="core not answering")


# ---------------------------
#   post_login
# ---------------------------
async def post_login(request: web.Request) -> web.Response:
    """Check the credentials and open a session."""
    auth = request.app[AUTH]
    data = await read_json(request)
    username = data.get("username") if isinstance(data, dict) else None
    password = data.get("password") if isinstance(data, dict) else None
    if not isinstance(username, str) or not isinstance(password, str):
        return json_error(400, "invalid_document")
    logins = request.app[LOGINS]
    address = request.remote or ""
    if logins.blocked(address):
        return json_error(429, "too_many_attempts")
    try:
        right = await _password_work(request, address, auth.check, username, password)
    except TooManyAttempts:
        return json_error(429, "too_many_attempts")
    if not right:
        _failed_login(request, address)
        _log(request, f"Wrong login for {username[:64]!r}")
        await asyncio.sleep(AUTH_FAILURE_DELAY)
        return json_error(401, "wrong_credentials")
    logins.succeeded(address)
    _log(request, f"Login of {auth.username}")
    response = web.json_response({"ok": True})
    _open_session(request, response)
    return response


# ---------------------------
#   post_logout
# ---------------------------
async def post_logout(request: web.Request) -> web.Response:
    """Close the session."""
    request.app[AUTH].end_session(request.cookies.get(SESSION_COOKIE))
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------
#   get_auth
# ---------------------------
async def get_auth(request: web.Request) -> web.Response:
    """Whether credentials exist (else the setup is needed), where they come from, and the username for a session."""
    auth = request.app[AUTH]
    status = auth.status()
    if not auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        # Before the login, nothing about the account.
        status["username"] = ""
    return web.json_response(status)


# ---------------------------
#   post_setup
# ---------------------------
async def post_setup(request: web.Request) -> web.Response:
    """Create the first credentials with the setup code from the log, and log in."""
    auth, setup = request.app[AUTH], request.app[SETUP]
    if auth.enabled:
        return json_error(409, "already_set_up")
    data = await read_json(request)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("code", "username", "password")):
        return json_error(400, "invalid_document")
    logins = request.app[LOGINS]
    address = request.remote or ""
    if logins.blocked(address):
        return json_error(429, "too_many_attempts")
    if not setup.check(data["code"]):
        _failed_login(request, address)
        _log(request, "Wrong setup code")
        await asyncio.sleep(AUTH_FAILURE_DELAY)
        return json_error(401, "wrong_setup_code")
    try:
        await _password_work(request, address, auth.set_credentials, data["username"], data["password"], "")
    except TooManyAttempts:
        return json_error(429, "too_many_attempts")
    except AuthError as err:
        return json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the credentials: %s", err)
        return json_error(500, "save_failed")
    auth.end_all_sessions()  # here, on the event loop: never in the thread of set_credentials
    logins.succeeded(address)
    setup.used()
    _log(request, f"First setup done: credentials of {auth.username} created")
    response = web.json_response(auth.status())
    _open_session(request, response)
    return response


# ---------------------------
#   put_auth
# ---------------------------
async def put_auth(request: web.Request) -> web.Response:
    """Change the username and password (with a session). Every session ends; the caller gets a new one."""
    auth = request.app[AUTH]
    if not auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        return json_error(401, "unauthorized")
    data = await read_json(request)
    if not isinstance(data, dict) or not all(isinstance(data.get(k, ""), str) for k in ("username", "password", "current")):
        return json_error(400, "invalid_document")
    # The current password is a password check like a login: a stolen session must not guess it freely.
    address = request.remote or ""
    if request.app[LOGINS].blocked(address):
        return json_error(429, "too_many_attempts")
    try:
        await _password_work(
            request, address, auth.set_credentials, data.get("username", ""), data.get("password", ""), data.get("current", "")
        )
    except TooManyAttempts:
        return json_error(429, "too_many_attempts")
    except AuthError as err:
        if err.code == "wrong_password":
            _failed_login(request, address)
            _log(request, "Wrong current password when changing the credentials")
            await asyncio.sleep(AUTH_FAILURE_DELAY)
        return json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the credentials: %s", err)
        return json_error(500, "save_failed")
    auth.end_all_sessions()  # here, on the event loop: never in the thread of set_credentials
    _log(request, f"Credentials changed, now for {auth.username}; every other session ended")
    response = web.json_response(auth.status())
    _open_session(request, response)
    return response


# ---------------------------
#   forward
# ---------------------------
async def forward(request: web.Request) -> web.StreamResponse:
    """Pass a request that got through the login to the core, with who sent it, and its answer back."""
    path = request.path
    if not (path.startswith("/api/") or path == "/metrics"):
        raise web.HTTPNotFound()
    if path in GATEWAY_API:
        # The gateway's own endpoints, with a method they do not take: never the core's.
        raise web.HTTPMethodNotAllowed(request.method, [])
    auth = request.app[AUTH]
    headers = {name: request.headers[name] for name in FORWARD_HEADERS if name in request.headers}
    headers[HEADER_USER] = auth.username
    headers[HEADER_CLIENT] = request.remote or ""
    body = await request.read()
    url = "http://core" + request.rel_url.raw_path_qs
    try:
        async with request.app[CORE].request(
            request.method,
            url,
            data=body or None,
            headers=headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=CORE_TIMEOUT_S),
        ) as answer:
            content = await answer.read()
            back = {name: answer.headers[name] for name in RETURN_HEADERS if name in answer.headers}
            return web.Response(status=answer.status, body=content, headers=back)
    except (aiohttp.ClientError, TimeoutError) as err:
        # Not the path: it comes from the client, and must not write lines of its own in the log.
        _LOGGER.warning("The core did not answer a request: %s", err)
        return json_error(502, "core_unavailable")


# ---------------------------
#   create_gateway
# ---------------------------
def create_gateway(
    core: aiohttp.ClientSession | Callable[[], aiohttp.ClientSession],
    auth: AuthStore,
    log: Callable[[str], None],
    force_https: bool = False,
    allow: tuple[Network, ...] | None = None,
    hosts: tuple[str, ...] | None = (),
) -> web.Application:
    """Build the gateway around a session to the core (or a function that makes one at startup).

    hosts are the names of WEBUI_HOSTS besides those of the LAN; None lets every name in.
    """
    app = web.Application(
        middlewares=[network_filter, host_check, security_headers, https_only, authentication, csrf],
        client_max_size=CLIENT_MAX_SIZE,
    )
    app[AUTH] = auth
    app[LOGINS] = LoginLimiter()
    app[SETUP] = SetupCode()
    app[PASSWORD_CHECKS] = asyncio.Semaphore(1)
    app[SLOW_CHECKS] = asyncio.Lock()
    app[FORCE_HTTPS] = force_https
    app[ALLOW] = parse_allow(None) if allow is None else allow
    app[LOG] = log
    app[REFUSED] = {}
    app[HOSTS] = hosts
    app[REFUSED_HOSTS] = {}

    async def core_session(app: web.Application) -> Any:
        app[CORE] = core() if callable(core) else core
        yield
        await app[CORE].close()

    app.cleanup_ctx.append(core_session)

    async def hide_server(request: web.Request, response: web.StreamResponse) -> None:
        # No name or version of the server software for a scanner.
        response.headers["Server"] = "hddfancontrol-webui"

    app.on_response_prepare.append(hide_server)
    router = app.router
    router.add_get("/", index)
    router.add_get("/login", login_page)
    router.add_get("/healthz", healthz)
    router.add_post("/api/login", post_login)
    router.add_post("/api/logout", post_logout)
    router.add_post("/api/setup", post_setup)
    router.add_get("/api/auth", get_auth)
    router.add_put("/api/auth", put_auth)
    router.add_static("/static/", STATIC_DIR, show_index=False, follow_symlinks=False)
    router.add_route("*", "/{tail:.*}", forward)
    return app


# ---------------------------
#   build
# ---------------------------
def build(env: dict[str, str], log: Callable[[str], None]) -> tuple[web.Application, str, int, ssl.SSLContext | None]:
    """Build the gateway from its environment (as the core gives it): the app, host, port and TLS context.

    A WEBUI_ALLOW that is not valid falls back to the private networks, and
    a certificate that cannot be read or made leaves HTTPS (and the move to
    it) off: either way with an error in the log, never a port open wider.
    """
    socket_path = env[CORE_SOCKET_ENV]
    web_dir = Path(env[WEB_DIR_ENV])
    port = int(env.get("WEBUI_PORT", str(DEFAULT_PORT)))
    host = env.get("WEBUI_HOST", "0.0.0.0")  # noqa: S104  # nosec B104  # Docker decides what is published
    try:
        allow = parse_allow(env.get("WEBUI_ALLOW"))
    except ValueError as err:
        log(f"ERROR: WEBUI_ALLOW is not valid ({err}); only the private networks are allowed")
        allow = parse_allow(None)
    try:
        hosts = parse_hosts(env.get("WEBUI_HOSTS"))
    except ValueError as err:
        log(f"ERROR: WEBUI_HOSTS is not valid ({err}); only the names of the LAN are allowed")
        hosts = ()
    context: ssl.SSLContext | None = None
    tls_error = ""
    if env_flag(env, "ENABLE_TLS", True):
        try:
            context = ssl_context(
                web_dir, env.get("TLS_CERT", "").strip(), env.get("TLS_KEY", "").strip(), env.get("OPENSSL_BIN", "openssl")
            )
        except TlsError as err:
            tls_error = str(err)
    # Moving browsers to an HTTPS that failed would lock everyone out.
    force_https = env_flag(env, "FORCE_HTTPS") and not tls_error
    auth = AuthStore(web_dir, env)
    app = create_gateway(
        lambda: aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket_path)),
        auth,
        log,
        force_https=force_https,
        allow=allow,
        hosts=hosts,
    )
    scheme = "HTTPS and HTTP" if context else "HTTP"
    moved = ", HTTP moved to HTTPS" if force_https else ""
    log(f"hddfancontrol-webui {VERSION}: listening on port {port} ({scheme}{moved}) as uid {os.getuid()}")
    log("Allowed networks: " + ", ".join(str(net) for net in allow))
    if tls_error:
        log(f"ERROR: HTTPS is off: {tls_error}")
    if not auth.enabled:
        log(
            "FIRST SETUP: no username and password are set. Open the web UI and enter this setup code: "
            f"{app[SETUP].code} (a new one at every start)"
        )
    return app, host, port, context


# ---------------------------
#   main
# ---------------------------
def main() -> None:  # pragma: no cover  # the process entry point; build and _serve are tested
    """Run the gateway as started by the core: the socket of the core and the web folder come in the environment."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    def log(text: str) -> None:
        print(text, flush=True)

    app, host, port, context = build(dict(os.environ), log)
    asyncio.run(_serve(app, host, port, context, env_flag(os.environ, "WEBUI_ACCESS_LOG")))


# ---------------------------
#   _serve
# ---------------------------
async def _serve(app: web.Application, host: str, port: int, context: ssl.SSLContext | None, access_log: bool = False) -> None:
    """Serve until SIGTERM or SIGINT; with TLS, one port answers both HTTPS and HTTP (DualListener).

    Without TLS the same listener serves plain HTTP: either way an address
    outside WEBUI_ALLOW is closed at accept, before a byte is read or a
    handshake made, and the connections are limited per address and in all.

    With access_log (WEBUI_ACCESS_LOG) every request of a browser or a
    script is logged; off by default, since the page polls every few
    seconds and would bury everything else.
    """
    runner = web.AppRunner(app, access_log=logging.getLogger("aiohttp.access") if access_log else None, handle_signals=False)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    listener: DualListener | None = None
    try:
        server = runner.server
        if server is None:  # setup() made it
            raise RuntimeError("the server was not made")
        listener = DualListener(server, host, port, context, admit=lambda address: admit_address(app, address))
        await listener.start()
        await stop.wait()
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        if listener is not None:
            await listener.close()
        await runner.cleanup()


if __name__ == "__main__":  # pragma: no cover
    main()
