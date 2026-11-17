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

What it lets through goes to the core with the user, the client address and
the login state in the INTERNAL_HEADERS; forward() builds the headers for the
core from scratch (FORWARD_HEADERS only), so a client cannot send its own. A bug before the login can therefore reach this process only,
which cannot write the fans, read the drives or read the rest of /config.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
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
    HEADER_AUTH,
    HEADER_CLIENT,
    HEADER_USER,
    VERSION,
    WEB_DIR_ENV,
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
# window ends.
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW = 900.0
LOGIN_MAX_ADDRESSES = 10000
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
# A refused address is logged at most once in this many seconds.
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
#   LoginLimiter
# ---------------------------
class LoginLimiter:
    """Count the wrong passwords of each client address, and refuse one that sends too many.

    The scrypt checks already run one at a time; this stops one address
    from guessing for hours, and from keeping the checks busy for everyone
    else. Behind a reverse proxy every client has the address of the proxy.
    """

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW) -> None:
        """Allow max_failures wrong passwords per address within window seconds."""
        self._max = max_failures
        self._window = window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, address: str, now: float) -> list[float]:
        """The failures of address within the window."""
        recent = [t for t in self._failures.get(address, []) if now - t < self._window]
        if recent:
            self._failures[address] = recent
        else:
            self._failures.pop(address, None)
        return recent

    def blocked(self, address: str) -> bool:
        """Whether address sent too many wrong passwords lately."""
        return len(self._recent(address, time.monotonic())) >= self._max

    def failed(self, address: str) -> None:
        """Count a wrong password of address."""
        now = time.monotonic()
        if address not in self._failures and len(self._failures) >= LOGIN_MAX_ADDRESSES:
            for other in list(self._failures):
                self._recent(other, now)
            if len(self._failures) >= LOGIN_MAX_ADDRESSES:
                return
        self._failures.setdefault(address, []).append(now)

    def succeeded(self, address: str) -> None:
        """Forget the failures of address after a right password."""
        self._failures.pop(address, None)


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
FORCE_HTTPS: web.AppKey[bool] = web.AppKey("force_https", bool)
ALLOW: web.AppKey[tuple[Network, ...]] = web.AppKey("allow", tuple)
CORE: web.AppKey[aiohttp.ClientSession] = web.AppKey("core", aiohttp.ClientSession)
LOG: web.AppKey[Callable[[str], None]] = web.AppKey("log")
REFUSED: web.AppKey[dict[str, float]] = web.AppKey("refused", dict)


# ---------------------------
#   parse_allow
# ---------------------------
def parse_allow(text: str | None) -> tuple[Network, ...]:
    """The networks of WEBUI_ALLOW (CIDRs or addresses, by commas or spaces); empty means DEFAULT_ALLOW; "all" is every address.

    Raises ValueError on a value that is not a network.
    """
    items = (text or "").replace(",", " ").split()
    if not items:
        items = list(DEFAULT_ALLOW)
    if [item.lower() for item in items] == ["all"]:
        items = ["0.0.0.0/0", "::/0"]
    return tuple(ipaddress.ip_network(item, strict=False) for item in items)


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
    return f" (a global IPv6 address: if it is your LAN's, add {network} to WEBUI_ALLOW)"


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
async def _password_work(request: web.Request, func: Callable[..., Any], *args: Any) -> Any:
    """Run a password check or change in a thread, one at a time.

    Each scrypt hash takes 16 MiB. Without a limit, a burst of wrong logins
    (no session needed) runs one per worker thread and can push the
    container past its memory limit. One at a time also slows down guessing.
    """
    async with request.app[PASSWORD_CHECKS]:
        return await asyncio.to_thread(func, *args)


# ---------------------------
#   _log
# ---------------------------
def _log(request: web.Request, text: str) -> None:
    """Log a security event, with the client address."""
    request.app[LOG](f"{text} (from {request.remote or '?'})")


# ---------------------------
#   network_filter
# ---------------------------
@web.middleware
async def network_filter(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Answer only the addresses of WEBUI_ALLOW; the others get a bare 403, logged once an hour."""
    if allowed(request.remote, request.app[ALLOW]):
        return await handler(request)
    address = request.remote or "?"
    refused = request.app[REFUSED]
    now = time.monotonic()
    if now - refused.get(address, -REFUSED_LOG_PERIOD) >= REFUSED_LOG_PERIOD:
        if len(refused) > LOGIN_MAX_ADDRESSES:
            refused.clear()
        refused[address] = now
        request.app[LOG](f"Refused {address}: not in WEBUI_ALLOW{refused_hint(address)}")
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
    """Let a request through with a session or Basic credentials; without credentials, only the setup."""
    auth = request.app[AUTH]
    path = request.path
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
        if await _password_work(request, auth.check_basic, header):
            logins.succeeded(address)
            return await handler(request)
        logins.failed(address)
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
    if not await _password_work(request, auth.check, username, password):
        logins.failed(address)
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
        logins.failed(address)
        _log(request, "Wrong setup code")
        await asyncio.sleep(AUTH_FAILURE_DELAY)
        return json_error(401, "wrong_setup_code")
    try:
        await _password_work(request, auth.set_credentials, data["username"], data["password"], "")
    except AuthError as err:
        return json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the credentials: %s", err)
        return json_error(500, "save_failed")
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
    try:
        await _password_work(
            request, auth.set_credentials, data.get("username", ""), data.get("password", ""), data.get("current", "")
        )
    except AuthError as err:
        if err.code == "wrong_password":
            _log(request, "Wrong current password when changing the credentials")
            await asyncio.sleep(AUTH_FAILURE_DELAY)
        return json_error(400, err.code)
    except OSError as err:
        _LOGGER.error("Cannot save the credentials: %s", err)
        return json_error(500, "save_failed")
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
    headers[HEADER_AUTH] = json.dumps(auth.status())
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
) -> web.Application:
    """Build the gateway around a session to the core (or a function that makes one at startup)."""
    app = web.Application(
        middlewares=[network_filter, security_headers, https_only, authentication, csrf],
        client_max_size=CLIENT_MAX_SIZE,
    )
    app[AUTH] = auth
    app[LOGINS] = LoginLimiter()
    app[SETUP] = SetupCode()
    app[PASSWORD_CHECKS] = asyncio.Semaphore(1)
    app[FORCE_HTTPS] = force_https
    app[ALLOW] = parse_allow(None) if allow is None else allow
    app[LOG] = log
    app[REFUSED] = {}

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
#   _flag
# ---------------------------
def _flag(env: dict[str, str], name: str) -> bool:
    """Read a true/false variable of env; missing is false."""
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


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
    context: ssl.SSLContext | None = None
    tls_error = ""
    if _flag(env, "ENABLE_TLS"):
        try:
            context = ssl_context(
                web_dir, env.get("TLS_CERT", "").strip(), env.get("TLS_KEY", "").strip(), env.get("OPENSSL_BIN", "openssl")
            )
        except TlsError as err:
            tls_error = str(err)
    # Moving browsers to an HTTPS that failed would lock everyone out.
    force_https = _flag(env, "FORCE_HTTPS") and not tls_error
    auth = AuthStore(web_dir, env)
    app = create_gateway(
        lambda: aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket_path)),
        auth,
        log,
        force_https=force_https,
        allow=allow,
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
    asyncio.run(_serve(app, host, port, context))


# ---------------------------
#   _serve
# ---------------------------
async def _serve(app: web.Application, host: str, port: int, context: ssl.SSLContext | None) -> None:
    """Serve until SIGTERM or SIGINT; with TLS, one port answers both HTTPS and HTTP (DualListener)."""
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    listener: DualListener | None = None
    try:
        if context is None:
            await web.TCPSite(runner, host, port).start()
        else:
            server = runner.server
            if server is None:  # setup() made it
                raise RuntimeError("the server was not made")
            listener = DualListener(server, host, port, context)
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
