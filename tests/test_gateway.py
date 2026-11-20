"""The gateway: network filter, first setup, login, CSRF, headers, HTTPS and the way to the core."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yarl
from aiohttp import web
from aiohttp.test_utils import TestClient

from hddfancontrol_webui import gateway as gateway_module
from hddfancontrol_webui.auth import AuthStore
from hddfancontrol_webui.const import HEADER_CLIENT, HEADER_USER
from hddfancontrol_webui.gateway import (
    AUTH,
    LOGINS,
    PASSWORD_CHECKS,
    SETUP,
    LoginLimiter,
    allowed,
    create_gateway,
    parse_allow,
    parse_hosts,
)

FETCH = {"X-Requested-With": "fetch"}
CREDENTIALS = {"WEBUI_USERNAME": "anto", "WEBUI_PASSWORD": "a-long-password"}
GOOD = {"username": "anto", "password": "a-long-password"}

Client = TestClient[Any, Any]


class Gateway:
    """A gateway in front of a fake core on a Unix socket, with what the core received and the log."""

    def __init__(self, client: Client, seen: list[dict[str, Any]], lines: list[str]) -> None:
        """Keep the client and the records."""
        self.client = client
        self.seen = seen
        self.lines = lines


GatewayMaker = Callable[..., Awaitable[Gateway]]


@pytest.fixture
async def make_gateway(aiohttp_client: Any, tmp_path: Path) -> AsyncIterator[GatewayMaker]:
    """Build a gateway; the fake core answers every path with what it received."""
    runners: list[web.AppRunner] = []
    seen: list[dict[str, Any]] = []

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        record = {
            "method": request.method,
            "path": request.path_qs,
            "headers": dict(request.headers),
            "body": body.decode(),
        }
        seen.append(record)
        if request.path == "/api/history.csv":
            return web.Response(
                text="a,b\n", content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="h.csv"'}
            )
        return web.json_response({"core": True, "path": request.path_qs}, status=418 if request.path == "/api/teapot" else 200)

    async def make(
        env: dict[str, str] | None = None,
        force_https: bool = False,
        allow: str | None = None,
        core: bool = True,
        hosts: tuple[str, ...] | None = (),
    ) -> Gateway:
        socket_path = tmp_path / f"core{len(runners)}.sock"
        if core:
            core_app = web.Application()
            core_app.router.add_route("*", "/{tail:.*}", echo)
            runner = web.AppRunner(core_app)
            await runner.setup()
            await web.UnixSite(runner, str(socket_path)).start()
            runners.append(runner)
        lines: list[str] = []
        web_dir = tmp_path / f"web{len(runners)}"
        web_dir.mkdir(exist_ok=True)
        app = create_gateway(
            lambda: aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket_path))),
            AuthStore(web_dir, env or {}),
            lines.append,
            force_https=force_https,
            allow=parse_allow(allow),
            hosts=hosts,
        )
        client: Client = await aiohttp_client(app)
        return Gateway(client, seen, lines)

    yield make
    for runner in runners:
        await runner.cleanup()


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pause after a wrong password in the tests."""
    monkeypatch.setattr(gateway_module, "AUTH_FAILURE_DELAY", 0)


async def logged_in(make_gateway: GatewayMaker, **kwargs: Any) -> Gateway:
    """A gateway with credentials, and a client with a session."""
    gw = await make_gateway(CREDENTIALS, **kwargs)
    assert (await gw.client.post("/api/login", json=GOOD, headers=FETCH)).status == 200
    return gw


def test_parse_allow_and_allowed() -> None:
    """Private networks by default, "all" for every address, mapped IPv4 counted as IPv4; a typo raises."""
    default = parse_allow(None)
    assert allowed("192.168.1.10", default) and allowed("100.101.1.1", default) and allowed("fd00::1", default)
    assert allowed("::ffff:10.0.0.5", default) and allowed("fe80::1%eth0", default)
    assert not allowed("8.8.8.8", default) and not allowed("2001:db8::1", default)
    assert not allowed(None, default) and not allowed("not-an-address", default)
    assert allowed("8.8.8.8", parse_allow("all")) and allowed("2001:db8::1", parse_allow("ALL"))
    only = parse_allow("192.168.1.0/24, 10.1.2.3")
    assert allowed("192.168.1.99", only) and allowed("10.1.2.3", only) and not allowed("10.1.2.4", only)
    assert allowed("127.0.0.1", only) and allowed("::1", only)  # loopback always (the health check)
    more = parse_allow("private, 2a02:1210::/64")
    assert allowed("192.168.1.10", more) and allowed("2a02:1210::5", more) and not allowed("8.8.8.8", more)
    assert set(default) <= set(more)
    with pytest.raises(ValueError):
        parse_allow("192.168.1.0/33")


async def test_network_filter(make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """An address outside WEBUI_ALLOW gets a bare 403, logged once."""
    gw = await make_gateway(CREDENTIALS, allow="10.0.0.0/8")
    real = gateway_module.allowed
    # The test client is on loopback, which is always allowed: count it as a stranger here.
    monkeypatch.setattr(
        gateway_module, "allowed", lambda address, networks: address != "127.0.0.1" and real(address, networks)
    )
    for _ in range(2):
        response = await gw.client.get("/login")
        assert response.status == 403 and await response.text() == "Forbidden"
    assert sum("not in WEBUI_ALLOW" in line for line in gw.lines) == 1
    monkeypatch.setattr(gateway_module, "allowed", real)
    assert (await gw.client.get("/login")).status == 200


def test_refused_hint() -> None:
    """A refused global IPv6 address gets the /64 to allow; others nothing."""
    hint = gateway_module.refused_hint("2a02:1210:2c00:aa00:1:2:3:4")
    assert "WEBUI_ALLOW=private 2a02:1210:2c00:aa00::/64" in hint
    assert gateway_module.refused_hint("8.8.8.8") == ""
    assert gateway_module.refused_hint("fd00::1") == ""
    assert gateway_module.refused_hint("?") == ""


async def test_first_setup(make_gateway: GatewayMaker) -> None:
    """Without credentials only the setup answers; the code of the log creates them and logs in, once."""
    gw = await make_gateway()
    client = gw.client
    code = client.app[SETUP].code
    assert code is not None and len(code) == 19
    assert (await (await client.get("/api/auth")).json())["enabled"] is False
    page = await client.get("/", allow_redirects=False)
    assert page.status == 302 and page.headers["Location"] == "/login"
    api = await client.get("/api/status")
    assert api.status == 401 and (await api.json())["error"] == "setup_required"
    assert (await client.get("/login")).status == 200
    assert gw.seen == []  # nothing reached the core
    body = {"code": "0000-0000-0000-0000", "username": "anto", "password": "a-long-password"}
    wrong = await client.post("/api/setup", json=body, headers=FETCH)
    assert wrong.status == 401 and (await wrong.json())["error"] == "wrong_setup_code"
    assert (await client.post("/api/setup", json={"code": code}, headers=FETCH)).status == 400
    short = await client.post("/api/setup", json={**body, "code": code, "password": "short"}, headers=FETCH)
    assert (await short.json())["error"] == "password_too_short"
    # Dashes, spaces and case do not matter.
    typed = code.upper().replace("-", " ")
    ok = await client.post("/api/setup", json={**body, "code": typed}, headers=FETCH)
    assert ok.status == 200 and (await ok.json())["source"] == "file"
    assert (await client.get("/api/status")).status == 200  # logged in
    again = await client.post("/api/setup", json={**body, "code": typed}, headers=FETCH)
    assert again.status == 409
    assert client.app[SETUP].code is None and not client.app[SETUP].check(typed)
    assert any("First setup done" in line for line in gw.lines) and any("Wrong setup code" in line for line in gw.lines)


async def test_setup_code_attempts_are_limited(make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Too many wrong setup codes from one address are refused without a check."""
    gw = await make_gateway()
    monkeypatch.setattr(gw.client.app[LOGINS], "_max", 2)
    body = {"code": "x", "username": "anto", "password": "a-long-password"}
    for _ in range(2):
        assert (await gw.client.post("/api/setup", json=body, headers=FETCH)).status == 401
    body["code"] = gw.client.app[SETUP].code
    assert (await gw.client.post("/api/setup", json=body, headers=FETCH)).status == 429


async def test_login_flow_and_forwarding(make_gateway: GatewayMaker) -> None:
    """Pages and the API need a login; then requests reach the core with who sent them, and nothing a client forged."""
    gw = await make_gateway(CREDENTIALS)
    client = gw.client
    page = await client.get("/", allow_redirects=False)
    assert page.status == 302 and page.headers["Location"] == "/login"
    assert (await client.get("/login")).status == 200
    style = await client.get("/static/style.css")
    assert style.status == 200 and style.headers["Cache-Control"] == "no-cache"
    assert (await client.get("/static/i18n/it.json")).status == 200
    assert (await client.get("/static/app.js", allow_redirects=False)).status == 302
    unauthorized = await client.get("/api/status")
    assert unauthorized.status == 401 and unauthorized.headers["Cache-Control"] == "no-store"
    assert (await (await client.get("/api/auth")).json())["username"] == ""  # nothing before the login
    assert (await client.post("/api/login", json={"username": "anto", "password": "no"}, headers=FETCH)).status == 401
    assert (await client.post("/api/login", json=[], headers=FETCH)).status == 400
    # An integer past Python's limit of digits is a ValueError of the parser: a 400, not a 500 with a traceback.
    huge = await client.post("/api/login", data="9" * 60000, headers={**FETCH, "Content-Type": "application/json"})
    assert huge.status == 400 and (await huge.json())["error"] == "invalid_json"
    ok = await client.post("/api/login", json=GOOD, headers=FETCH)
    cookie = ok.headers["Set-Cookie"]
    assert ok.status == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert (await client.get("/login", allow_redirects=False)).status == 302
    assert (await client.get("/")).status == 200
    forged = {HEADER_USER: "root", HEADER_CLIENT: "1.2.3.4", "X-Webui-Auth": "{}", "X-Other": "x"}
    response = await client.post("/api/restart?a=1", json={"x": 1}, headers={**FETCH, **forged})
    assert (await response.json()) == {"core": True, "path": "/api/restart?a=1"}
    got = gw.seen[-1]
    assert got["method"] == "POST" and json.loads(got["body"]) == {"x": 1}
    assert got["headers"][HEADER_USER] == "anto" and got["headers"][HEADER_CLIENT] == "127.0.0.1"
    assert all(name not in got["headers"] for name in ("X-Other", "X-Webui-Auth", "Cookie"))
    assert "auth" not in await (await client.get("/api/status")).json()  # the core's answer, as it is
    teapot = await client.get("/api/teapot")
    assert teapot.status == 418
    csv = await client.get("/api/history.csv")
    assert csv.content_type == "text/csv" and "h.csv" in csv.headers["Content-Disposition"]
    assert (await client.get("/elsewhere")).status == 404
    assert any("Login of anto" in line for line in gw.lines) and any("Wrong login" in line for line in gw.lines)
    await client.post("/api/logout", headers=FETCH)
    assert (await client.get("/api/status")).status == 401


async def test_headers(make_gateway: GatewayMaker) -> None:
    """Every answer has the security headers and no server version."""
    gw = await make_gateway(CREDENTIALS)
    for path in ("/login", "/nope", "/api/status"):
        response = await gw.client.get(path, allow_redirects=False)
        headers = response.headers
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"], path
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert "camera=()" in headers["Permissions-Policy"] and headers["Server"] == "hddfancontrol-webui"


async def test_csrf(make_gateway: GatewayMaker) -> None:
    """A change without the header, or from another origin, is refused before the core."""
    gw = await logged_in(make_gateway)
    assert (await gw.client.post("/api/restart")).status == 403
    assert (await gw.client.post("/api/restart", headers={**FETCH, "Origin": "http://evil.example"})).status == 403
    assert gw.seen == []
    assert (await gw.client.post("/api/restart", headers=FETCH)).status == 200


async def test_requests_of_other_sites_never_reach_the_api(make_gateway: GatewayMaker) -> None:
    """A browser marks a request sent by a page of another site (another port of the NAS too): no API, no metrics, even a GET.

    An <img> of another app on the same host could otherwise keep manual
    control alive by asking for the status. The pages of the web UI, a
    link typed or followed, and scripts (no header) are not concerned.
    """
    gw = await logged_in(make_gateway)
    for site in ("same-site", "cross-site"):
        for method, path in (("GET", "/api/status"), ("GET", "/metrics"), ("POST", "/api/restart")):
            response = await gw.client.request(method, path, headers={**FETCH, "Sec-Fetch-Site": site})
            assert response.status == 403 and (await response.json())["error"] == "csrf", (site, path)
    assert gw.seen == []
    for site in ("same-origin", "none"):
        assert (await gw.client.get("/api/status", headers={"Sec-Fetch-Site": site})).status == 200
    assert (await gw.client.get("/api/status")).status == 200
    assert (await gw.client.get("/login", headers={"Sec-Fetch-Site": "cross-site"})).status == 200  # a page may be linked


async def test_basic_auth_and_metrics(make_gateway: GatewayMaker) -> None:
    """Scripts and Prometheus send Basic credentials."""
    gw = await make_gateway(CREDENTIALS)
    good = "Basic " + base64.b64encode(b"anto:a-long-password").decode()
    bad = "Basic " + base64.b64encode(b"anto:x").decode()
    assert (await gw.client.get("/metrics")).status == 401
    assert (await gw.client.get("/metrics", headers={"Authorization": good})).status == 200
    assert (await gw.client.get("/api/status", headers={"Authorization": bad})).status == 401


async def test_change_credentials(make_gateway: GatewayMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials made at the setup can be changed with the current password, with a session only.

    Every other session ends, on the event loop after the change: a request
    of a stolen session running meanwhile cannot put its session back.
    """
    import threading

    gw = await make_gateway()
    client = gw.client
    ended: list[threading.Thread] = []
    real_end = AuthStore.end_all_sessions

    def end(self: AuthStore) -> None:
        ended.append(threading.current_thread())
        real_end(self)

    monkeypatch.setattr(AuthStore, "end_all_sessions", end)
    stolen = client.app[AUTH].new_session()
    code = client.app[SETUP].code
    await client.post("/api/setup", json={"code": code, **GOOD}, headers=FETCH)
    assert (await client.put("/api/auth", json={"username": 1}, headers=FETCH)).status == 400
    wrong = await client.put(
        "/api/auth", json={"username": "x", "password": "another-password", "current": "no"}, headers=FETCH
    )
    assert (await wrong.json())["error"] == "wrong_password"
    body = {"username": "new", "password": "another-password", "current": "a-long-password"}
    ok = await client.put("/api/auth", json=body, headers=FETCH)
    assert ok.status == 200 and (await ok.json())["username"] == "new"
    assert ended and all(thread is threading.main_thread() for thread in ended)
    assert not client.app[AUTH].session_valid(stolen)
    assert (await (await client.get("/api/auth")).json())["username"] == "new"  # still logged in
    removed = await client.delete("/api/auth", json={"current": "another-password"}, headers=FETCH)
    assert removed.status == 405 and gw.seen == []  # credentials cannot be removed, and the core never sees it
    client.session.cookie_jar.clear()
    assert (await client.put("/api/auth", json=body, headers=FETCH)).status == 401
    assert any("Credentials changed" in line for line in gw.lines)


async def test_wrong_current_password_counts_as_a_wrong_login(
    make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a session, guessing the current password still meets the login limit."""
    gw = await make_gateway()
    client = gw.client
    await client.post("/api/setup", json={"code": client.app[SETUP].code, **GOOD}, headers=FETCH)
    monkeypatch.setattr(client.app[LOGINS], "_max", 1)
    guess = {"username": "x", "password": "another-password", "current": "guess"}
    assert (await client.put("/api/auth", json=guess, headers=FETCH)).status == 400
    blocked = await client.put("/api/auth", json={**guess, "current": "a-long-password"}, headers=FETCH)
    assert blocked.status == 429 and (await blocked.json())["error"] == "too_many_attempts"


async def test_credentials_from_the_environment_cannot_change(make_gateway: GatewayMaker) -> None:
    """WEBUI_USERNAME and WEBUI_PASSWORD win, and are read only."""
    gw = await logged_in(make_gateway)
    body = {"username": "x", "password": "another-password", "current": "a-long-password"}
    assert (await (await gw.client.put("/api/auth", json=body, headers=FETCH)).json())["error"] == "auth_from_env"


async def test_password_checks_run_one_at_a_time(make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of logins never runs two scrypt checks at once."""
    gw = await make_gateway(CREDENTIALS)
    lock = threading.Lock()
    running = {"now": 0, "most": 0}

    def slow_check(username: str, password: str) -> bool:
        with lock:
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
        time.sleep(0.02)
        with lock:
            running["now"] -= 1
        return True

    monkeypatch.setattr(gw.client.app[AUTH], "check", slow_check)
    # Five clients at once (the test client has one address: each request plays another).
    clients = iter(f"10.0.0.{n}" for n in range(1, 100))
    monkeypatch.setattr(LoginLimiter, "client", staticmethod(lambda address: next(clients)))
    answers = await asyncio.gather(*(gw.client.post("/api/login", json=GOOD, headers=FETCH) for _ in range(5)))
    assert all(answer.status == 200 for answer in answers) and running["most"] == 1


async def test_parallel_guesses_of_one_client_get_one_check(
    make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30 wrong logins sent at once by one client: one password check, the others 429 at once."""
    gw = await make_gateway(CREDENTIALS)
    checks: list[str] = []
    real = gw.client.app[AUTH].check

    def counted(username: str, password: str) -> bool:
        checks.append(password)
        time.sleep(0.05)
        return bool(real(username, password))

    monkeypatch.setattr(gw.client.app[AUTH], "check", counted)
    bad = {"username": "anto", "password": "nope"}
    answers = await asyncio.gather(*(gw.client.post("/api/login", json=bad, headers=FETCH) for _ in range(30)))
    statuses = sorted(answer.status for answer in answers)
    assert len(checks) == 1 and statuses == [401] + [429] * 29


async def test_the_limit_is_checked_again_in_the_queue(make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """A check that waited for its turn is refused if the client went over the limit meanwhile."""
    gw = await make_gateway(CREDENTIALS)
    limiter = gw.client.app[LOGINS]
    monkeypatch.setattr(limiter, "_max", 1)
    await gw.client.app[PASSWORD_CHECKS].acquire()  # another check is running
    waiting = asyncio.ensure_future(gw.client.post("/api/login", json=GOOD, headers=FETCH))
    await asyncio.sleep(0.05)
    limiter.failed("127.0.0.1")  # meanwhile, a wrong password of this client was counted
    gw.client.app[PASSWORD_CHECKS].release()
    assert (await waiting).status == 429


async def test_too_many_wrong_passwords(make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch) -> None:
    """An address that sends too many wrong passwords is refused without a check; a right one resets the count."""
    gw = await make_gateway(CREDENTIALS)
    client = gw.client
    limiter = client.app[LOGINS]
    monkeypatch.setattr(limiter, "_max", 2)
    bad = {"username": "anto", "password": "nope"}
    assert (await client.post("/api/login", json=bad, headers=FETCH)).status == 401
    assert (await client.post("/api/login", json=GOOD, headers=FETCH)).status == 200
    for _ in range(2):
        assert (await client.post("/api/login", json=bad, headers=FETCH)).status == 401
    blocked = await client.post("/api/login", json=GOOD, headers=FETCH)
    assert blocked.status == 429 and (await blocked.json())["error"] == "too_many_attempts"
    client.session.cookie_jar.clear()  # no session: only the Basic credentials count
    basic = "Basic " + base64.b64encode(b"anto:a-long-password").decode()
    assert (await client.get("/api/status", headers={"Authorization": basic})).status == 429
    limiter.succeeded("127.0.0.1")
    assert (await client.get("/api/status", headers={"Authorization": basic})).status == 200


def test_login_limiter_window_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures expire after the window; the table of addresses stays bounded."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(gateway_module, "LOGIN_MAX_ADDRESSES", 2)
    limiter = LoginLimiter(max_failures=1, window=10)
    limiter.failed("a")
    assert limiter.blocked("a")
    clock["now"] += 11
    assert not limiter.blocked("a")
    limiter.failed("b")
    limiter.failed("c")
    limiter.failed("d")  # the table is full of recent failures: not counted
    assert limiter.blocked("b") and limiter.blocked("c") and not limiter.blocked("d")
    clock["now"] += 11
    limiter.failed("d")  # expired entries make room
    assert limiter.blocked("d")


async def test_dot_segments_never_pass_as_public(make_gateway: GatewayMaker) -> None:
    """A path with . or .. parts is refused before the login check, however it is written."""
    gw = await make_gateway(CREDENTIALS)
    # The test client resolves a literal ".." itself (curl --path-as-is does not): the encoded forms reach the server.
    for path in ("/static/i18n/%2e%2e/app.js", "/static/i18n/..%2fapp.js", "/static/i18n/%2e/../app.js", "/static/%2E/app.js"):
        response = await gw.client.get(yarl.URL(path, encoded=True))
        assert response.status == 404, path
    assert (await gw.client.get("/static/i18n/en.json")).status == 200  # the public files still are


def test_login_limiter_refuses_an_address_and_only_slows_its_64() -> None:
    """An IPv6 address is refused alone; its /64 (often a whole LAN) is counted too, but only slowed down, never refused.

    A host of the LAN that guesses must not lock its neighbours out, nor
    can it guess at full speed by taking a new address of the /64 for
    every ten tries. A right password of one address resets that address
    alone.
    """
    limiter = LoginLimiter(max_failures=2, window=10)
    attacker, owner = "fd00:1:2:3::1", "fd00:1:2:3::2"
    assert limiter.delay(owner) == 0
    limiter.failed(attacker)
    limiter.failed(attacker)
    assert limiter.blocked(attacker) and limiter.blocked("fd00:1:2:3:0:0:0:1")  # the same address, written otherwise
    assert not limiter.blocked(owner) and limiter.delay(owner) > 0
    assert limiter.delay("fd00:1:2:4::1") == 0 and not limiter.blocked("fd00:1:2:4::1")
    limiter.succeeded(owner)
    assert limiter.blocked(attacker) and limiter.delay(owner) > 0
    limiter.succeeded(attacker)
    assert not limiter.blocked(attacker)
    # The slowdown grows with the failures of the /64, up to LOGIN_SLOW_MAX.
    first = limiter.delay(owner)
    for n in range(40):
        limiter.failed(f"fd00:1:2:3::{n + 10:x}")
    assert first < limiter.delay(owner) <= gateway_module.LOGIN_SLOW_MAX
    limiter.failed("192.168.1.10")
    limiter.failed("192.168.1.10")
    assert limiter.blocked("192.168.1.10") and not limiter.blocked("192.168.1.11")
    assert limiter.blocked("::ffff:192.168.1.10") and limiter.delay("192.168.1.11") == 0
    assert LoginLimiter.client("not an address") == "not an address"


def test_login_limiter_counts_an_ipv4_mapped_address_as_ipv4() -> None:
    """::ffff:a.b.c.d (a dual-stack socket) is the IPv4 client a.b.c.d, not a /64 shared by every IPv4 host."""
    assert LoginLimiter.client("::ffff:192.168.1.10") == "192.168.1.10"
    limiter = LoginLimiter(max_failures=1, window=10)
    limiter.failed("::ffff:192.168.1.10")
    assert limiter.blocked("192.168.1.10") and not limiter.blocked("::ffff:192.168.1.11")


def test_login_limiter_slows_down_past_the_global_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the global limit nobody is refused: clients that failed wait longer and longer, the others not at all."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(gateway_module, "LOGIN_SLOW_STEP", 2)
    limiter = LoginLimiter(max_failures=10, window=10, max_global=3)
    assert [limiter.failed(f"10.0.0.{i}") for i in range(1, 4)] == [False, False, True]
    assert not limiter.blocked("10.0.0.1") and not limiter.blocked("10.0.0.99")
    assert limiter.delay("10.0.0.99") == 0  # no failure of its own: the owner logging in
    assert limiter.delay("10.0.0.1") == gateway_module.LOGIN_SLOW_BASE
    for i in range(4, 8):
        limiter.failed(f"10.0.0.{i}")
    assert limiter.delay("10.0.0.1") == 4 * gateway_module.LOGIN_SLOW_BASE  # doubled every LOGIN_SLOW_STEP failures
    for i in range(8, 60):
        limiter.failed(f"10.0.0.{i}")
    assert limiter.delay("10.0.0.1") == gateway_module.LOGIN_SLOW_MAX
    clock["now"] += 11
    assert limiter.delay("10.0.0.1") == 0


def test_login_limiter_slows_down_unknown_clients_when_its_table_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client the full table could not hold may have failed: past the global limit it waits too."""
    monkeypatch.setattr(gateway_module, "LOGIN_MAX_ADDRESSES", 2)
    limiter = LoginLimiter(max_failures=10, window=100, max_global=2)
    for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        limiter.failed(address)
    assert limiter.delay("10.0.0.3") > 0 and limiter.delay("10.0.0.4") > 0


async def test_past_the_global_limit_the_right_password_still_logs_in(
    make_gateway: GatewayMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrong passwords from many clients slow down those clients, never refuse the owner; one line in the log."""
    gw = await make_gateway(CREDENTIALS)
    limiter = gw.client.app[LOGINS]
    monkeypatch.setattr(limiter, "_max_global", 2)
    monkeypatch.setattr(gateway_module, "LOGIN_SLOW_BASE", 0.3)
    bad = {"username": "anto", "password": "nope"}
    limiter.failed("10.9.9.9")  # another client
    assert (await gw.client.post("/api/login", json=bad, headers=FETCH)).status == 401
    assert any("slowed down" in line for line in gw.lines)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert (await gw.client.post("/api/login", json=GOOD, headers=FETCH)).status == 200  # slowed, not refused
    assert loop.time() - started >= 0.3
    monkeypatch.setattr(LoginLimiter, "client", staticmethod(lambda address: "10.1.1.1"))  # a client without failures
    started = loop.time()
    assert (await gw.client.post("/api/login", json=GOOD, headers=FETCH)).status == 200
    assert loop.time() - started < 0.3


async def test_session_cookie_is_secure_behind_an_https_proxy(make_gateway: GatewayMaker) -> None:
    """The session cookie is marked Secure when a reverse proxy says the browser used HTTPS."""
    gw = await make_gateway(CREDENTIALS)
    plain = await gw.client.post("/api/login", json=GOOD, headers=FETCH)
    assert "Secure" not in plain.headers["Set-Cookie"]
    proxied = await gw.client.post("/api/login", json=GOOD, headers={**FETCH, "X-Forwarded-Proto": "https, http"})
    assert "Secure" in proxied.headers["Set-Cookie"]


async def test_web_app_manifest(make_gateway: GatewayMaker) -> None:
    """The manifest and its icons are public (the browser fetches them without the session) and typed."""
    gw = await make_gateway(CREDENTIALS)
    response = await gw.client.get("/static/manifest.webmanifest")
    assert response.status == 200 and response.content_type == "application/manifest+json"
    manifest = json.loads(await response.text())
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for item in manifest["icons"]:
        assert (await gw.client.get(item["src"], allow_redirects=False)).status == 200


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.10",
        "192.168.1.10:8080",
        "[fd00::1]:8080",
        "[::1]",
        "localhost:8080",
        "truenas",
        "truenas.local",
        "nas.lan",
        "nas.home.arpa",
        "nas.internal",
        "nas.localdomain",
        "truenas.fritz.box",
        "nas.home",
        "truenas.tail1234.ts.net",
        "NAS.LAN.",
        "nas.example.com",
        "a.nas.example.org",
    ],
)
async def test_host_names_of_the_lan_and_of_webui_hosts(make_gateway: GatewayMaker, host: str) -> None:
    """IP addresses, names of the LAN, routers and VPNs, and the names of WEBUI_HOSTS reach the web UI."""
    gw = await make_gateway(CREDENTIALS, hosts=parse_hosts("nas.example.com, *.nas.example.org"))
    assert (await gw.client.get("/login", headers={"Host": host})).status == 200


async def test_other_host_names_are_refused(make_gateway: GatewayMaker) -> None:
    """An Internet name gets a bare 403, logged once with the WEBUI_HOSTS to set; /healthz still answers; no Host is 400."""
    gw = await make_gateway(CREDENTIALS)
    for _ in range(3):
        refused = await gw.client.get("/login", headers={"Host": "rebind.attacker.example:8080"})
        assert refused.status == 403 and await refused.text() == "Forbidden"
    assert [line for line in gw.lines if "rebind.attacker.example" in line] == [
        "Refused the host name 'rebind.attacker.example': not a name of the LAN (if it is yours, set WEBUI_HOSTS=rebind.attacker.example)"
    ]
    assert (await gw.client.get("/api/login", headers={"Host": "nas.example.org"})).status == 403
    assert (await gw.client.get("/healthz", headers={"Host": "rebind.attacker.example"})).status != 403
    for bad in ("", "a b", "[zz::1]", "x" * 300, "user@host"):
        assert (await gw.client.get("/login", headers={"Host": bad})).status == 400, bad


async def test_webui_hosts_any_turns_the_check_off(make_gateway: GatewayMaker) -> None:
    """WEBUI_HOSTS=any lets every name in (a reverse proxy that checks names itself)."""
    gw = await make_gateway(CREDENTIALS, hosts=parse_hosts("any"))
    assert (await gw.client.get("/login", headers={"Host": "nas.example.com"})).status == 200


def test_parse_hosts() -> None:
    """Names by commas or spaces, lower case, *. for subdomains; any is None; a bad one is a ValueError."""
    assert parse_hosts(None) == () and parse_hosts("A.example.com, *.b.example.com.") == ("a.example.com", "*.b.example.com")
    assert parse_hosts("x.example any") is None
    for bad in ("a b@c", "http://x", "x.example:8080", "*."):
        with pytest.raises(ValueError):
            parse_hosts(bad)


def test_build_reads_webui_hosts(tmp_path: Path) -> None:
    """build takes WEBUI_HOSTS; a bad value is logged and only the names of the LAN are allowed."""
    lines: list[str] = []
    env = {"HFC_CORE_SOCKET": str(tmp_path / "s"), "HFC_WEB_DIR": str(tmp_path), "ENABLE_TLS": "false", **CREDENTIALS}
    app, _, _, _ = gateway_module.build({**env, "WEBUI_HOSTS": "nas.example.com"}, lines.append)
    assert app[gateway_module.HOSTS] == ("nas.example.com",)
    app, _, _, _ = gateway_module.build({**env, "WEBUI_HOSTS": "bad@name"}, lines.append)
    assert app[gateway_module.HOSTS] == () and any("WEBUI_HOSTS is not valid" in line for line in lines)


async def test_force_https(make_gateway: GatewayMaker) -> None:
    """With force_https, plain HTTP gets the page that moves the browser to HTTPS, or 403 for the API; not /healthz."""
    gw = await make_gateway(CREDENTIALS, force_https=True)
    client = gw.client
    # A forged Host never reaches the page (400), and the page builds no redirect from it either.
    assert (await client.get("/settings", headers={"Host": "evil.example@good"})).status == 400
    page = await client.get("/settings?x=1", headers={"Host": "nas.lan:8080"}, allow_redirects=False)
    assert page.status == 200 and "/static/https.js" in await page.text()
    assert "Location" not in page.headers and "Content-Security-Policy" in page.headers
    api = await client.get("/api/status")
    assert api.status == 403 and (await api.json())["error"] == "https_required"
    assert (await client.post("/api/login", json={})).status == 403
    assert (await client.get("/metrics")).status == 403
    for path in ("/healthz", "/static/https.js", "/static/style.css"):
        assert (await client.get(path)).status == 200, path
    proxied = await client.post("/api/login", json=GOOD, headers={**FETCH, "X-Forwarded-Proto": "https"})
    assert proxied.status == 200 and "Strict-Transport-Security" not in proxied.headers


async def test_healthz_and_core_down(make_gateway: GatewayMaker) -> None:
    """/healthz says ok only when the core answers; a request the core cannot take is a 502."""
    up = await make_gateway(CREDENTIALS)
    assert (await up.client.get("/healthz")).status == 200
    down = await make_gateway(CREDENTIALS, core=False)
    assert (await down.client.get("/healthz")).status == 503
    await down.client.post("/api/login", json=GOOD, headers=FETCH)
    response = await down.client.get("/api/status")
    assert response.status == 502 and (await response.json())["error"] == "core_unavailable"


def test_build_from_the_environment(tmp_path: Path) -> None:
    """build reads the core's socket, the folder, the port, the networks and TLS; problems fall back safely."""
    lines: list[str] = []
    env = {"HFC_CORE_SOCKET": str(tmp_path / "s"), "HFC_WEB_DIR": str(tmp_path), "WEBUI_PORT": "9999", "ENABLE_TLS": "false"}
    app, host, port, context = gateway_module.build(env, lines.append)
    assert (host, port, context) == ("0.0.0.0", 9999, None)
    assert any("FIRST SETUP" in line and str(app[SETUP].code) in line for line in lines)
    lines.clear()
    bad = {**env, "WEBUI_ALLOW": "10.0.0.0/33", "ENABLE_TLS": "true", "FORCE_HTTPS": "true", "TLS_CERT": "/nope"}
    bad.update(CREDENTIALS)
    app, _, _, context = gateway_module.build(bad, lines.append)
    assert context is None and app[gateway_module.FORCE_HTTPS] is False
    assert app[gateway_module.ALLOW] == parse_allow(None)
    assert any("WEBUI_ALLOW is not valid" in line for line in lines) and any("HTTPS is off" in line for line in lines)
    assert not any("FIRST SETUP" in line for line in lines)


def test_https_is_on_by_default(tmp_path: Path) -> None:
    """Without ENABLE_TLS the gateway serves HTTPS too, with a self-signed pair; without openssl, HTTP only and an error."""
    lines: list[str] = []
    env = {"HFC_CORE_SOCKET": str(tmp_path / "s"), "HFC_WEB_DIR": str(tmp_path), **CREDENTIALS}
    _, _, _, context = gateway_module.build(env, lines.append)
    assert context is not None and (tmp_path / "tls" / "cert.pem").exists()
    assert any("HTTPS and HTTP" in line for line in lines)
    lines.clear()
    other = tmp_path / "other"
    other.mkdir()
    _, _, _, context = gateway_module.build(
        {**env, "HFC_WEB_DIR": str(other), "OPENSSL_BIN": "/nonexistent/openssl"}, lines.append
    )
    assert context is None and any("HTTPS is off" in line for line in lines)  # the fans and the login come first


async def test_serve_until_sigterm(tmp_path: Path, unused_tcp_port: int) -> None:
    """_serve answers on its port, over HTTP and (with a context) HTTPS, until SIGTERM."""
    import os
    import signal
    import ssl

    from hddfancontrol_webui.tls import ssl_context

    for context in (None, ssl_context(tmp_path)):
        lines: list[str] = []
        env = {"HFC_CORE_SOCKET": str(tmp_path / "s"), "HFC_WEB_DIR": str(tmp_path), "WEBUI_PASSWORD": "a-long-password"}
        app, _, _, _ = gateway_module.build(env, lines.append)
        task = asyncio.create_task(gateway_module._serve(app, "127.0.0.1", unused_tcp_port, context, context is None))
        scheme = "https" if context else "http"
        trusted = ssl.create_default_context(cafile=str(tmp_path / "tls" / "cert.pem")) if context else None
        async with aiohttp.ClientSession() as session:
            for _ in range(100):
                try:
                    async with session.get(f"{scheme}://127.0.0.1:{unused_tcp_port}/login", ssl=trusted) as response:
                        assert response.status == 200
                        break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.02)
            else:
                raise AssertionError("the gateway did not answer")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 10)


async def test_serve_refuses_strangers_before_reading(
    tmp_path: Path, unused_tcp_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address outside WEBUI_ALLOW is closed at accept, before a byte or a handshake, and logged once; also without TLS."""
    import os
    import signal

    real = gateway_module.allowed
    monkeypatch.setattr(
        gateway_module, "allowed", lambda address, networks: address != "127.0.0.1" and real(address, networks)
    )
    lines: list[str] = []
    env = {
        "HFC_CORE_SOCKET": str(tmp_path / "s"),
        "HFC_WEB_DIR": str(tmp_path),
        "WEBUI_PASSWORD": "a-long-password",
        "ENABLE_TLS": "false",
    }
    app, _, _, context = gateway_module.build(env, lines.append)
    assert context is None
    task = asyncio.create_task(gateway_module._serve(app, "127.0.0.1", unused_tcp_port, context))
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", unused_tcp_port)
            break
        except OSError:
            await asyncio.sleep(0.02)
    else:
        raise AssertionError("the gateway did not listen")
    writer.write(b"GET /login HTTP/1.1\r\nHost: localhost\r\n\r\n")
    try:
        assert await asyncio.wait_for(reader.read(), 5) == b""  # not even a 403: nothing was read
    except ConnectionResetError:
        pass
    writer.close()
    assert sum("Refused 127.0.0.1" in line for line in lines) == 1
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)
