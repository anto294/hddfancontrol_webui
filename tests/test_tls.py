"""Tests of HTTPS: the certificate and the port that answers both HTTPS and HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import ssl
import stat
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from hddfancontrol_webui import tls
from hddfancontrol_webui.tls import DualListener, TlsError, make_certificate, ssl_context

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")


@needs_openssl
def test_self_signed_certificate(tmp_path: Path) -> None:
    """Without TLS_CERT and TLS_KEY a certificate is made once in config/tls, its key readable by the owner only."""
    context = ssl_context(tmp_path)
    cert, key = tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"
    assert isinstance(context, ssl.SSLContext) and cert.exists()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    made = cert.read_bytes()
    ssl_context(tmp_path)
    assert cert.read_bytes() == made  # kept
    # The same pair given by name.
    assert isinstance(ssl_context(tmp_path / "other", str(cert), str(key)), ssl.SSLContext)


def test_certificate_problems(tmp_path: Path) -> None:
    """Half a pair, files that are not a certificate, or no openssl: TlsError."""
    with pytest.raises(TlsError, match="both"):
        ssl_context(tmp_path, cert="/x.pem")
    bad = tmp_path / "bad.pem"
    bad.write_text("nothing")
    with pytest.raises(TlsError, match="cannot load"):
        ssl_context(tmp_path, str(bad), str(bad))
    with pytest.raises(TlsError, match="not installed"):
        ssl_context(tmp_path, openssl="no-such-openssl-program")


def test_openssl_fails(tmp_path: Path) -> None:
    """An openssl that fails, or cannot run, is reported."""
    failing = tmp_path / "openssl"
    failing.write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
    failing.chmod(0o755)
    with pytest.raises(TlsError, match="broken"):
        make_certificate(tmp_path / "c.pem", tmp_path / "k.pem", str(failing))
    failing.write_text("not a script")
    with pytest.raises(TlsError, match="cannot make"):
        make_certificate(tmp_path / "c.pem", tmp_path / "k.pem", str(failing))


@needs_openssl
async def test_one_port_for_https_and_http(tmp_path: Path) -> None:
    """The same port answers HTTPS and HTTP; silent, closed and broken connections are dropped."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=f"secure={request.secure}")

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, ssl_context(tmp_path))
    with pytest.raises(RuntimeError):
        _ = listener.port
    await listener.start()
    port = listener.port
    # The client trusts the certificate just made (its names include 127.0.0.1): verification stays on.
    trusted = ssl.create_default_context(cafile=str(tmp_path / "tls" / "cert.pem"))
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://127.0.0.1:{port}/", ssl=trusted) as response:
            assert await response.text() == "secure=True"
        async with session.get(f"http://127.0.0.1:{port}/") as response:
            assert await response.text() == "secure=False"
    # A connection closed at once, and a TLS handshake that is garbage.
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x16\x03\x01garbage-not-a-client-hello" * 4)
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 5) is not None
    writer.close()
    # A connection that sends nothing is still waiting when the listener closes.
    _, silent = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.05)
    await listener.close()
    silent.close()
    await server.shutdown()


async def test_silent_connection_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that sends nothing is closed after SNIFF_TIMEOUT_S."""
    monkeypatch.setattr(tls, "SNIFF_TIMEOUT_S", 0.05)

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, ssl.create_default_context(ssl.Purpose.CLIENT_AUTH))
    await listener.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
    assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    await listener.close()
    await server.shutdown()


async def test_refused_addresses_are_closed_before_anything_is_read() -> None:
    """An address admit() refuses is closed at once: no byte is read, no TLS handshake, the server never sees it."""
    seen: list[str] = []
    handled: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        handled.append(request.path)
        return web.Response(text="ok")

    def admit(address: str) -> bool:
        seen.append(address)
        return False

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, None, admit=admit)
    await listener.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    with contextlib.suppress(ConnectionResetError):
        assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    assert seen == ["127.0.0.1"] and handled == []
    await listener.close()
    await server.shutdown()


async def test_plain_http_without_tls() -> None:
    """Without a context the listener serves plain HTTP only, with the same limits."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=f"secure={request.secure}")

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, None)
    await listener.start()
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{listener.port}/") as response:
            assert await response.text() == "secure=False"
    await listener.close()
    await server.shutdown()


async def test_connections_are_limited_per_address_and_in_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past MAX_CONNECTIONS_PER_ADDRESS (or MAX_CONNECTIONS) a new connection is closed at once; a closed one frees its place."""
    monkeypatch.setattr(tls, "MAX_CONNECTIONS_PER_ADDRESS", 2)

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, None)
    await listener.start()
    port = listener.port
    held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
    await asyncio.sleep(0.1)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    with contextlib.suppress(ConnectionResetError):
        assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    held[0][1].close()
    for _ in range(100):
        if listener.open_connections() < 2:
            break
        await asyncio.sleep(0.02)
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/") as response:
            assert await response.text() == "ok"
    monkeypatch.setattr(tls, "MAX_CONNECTIONS_PER_ADDRESS", 100)
    monkeypatch.setattr(tls, "MAX_CONNECTIONS", 1)  # the second held connection fills it
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    with contextlib.suppress(ConnectionResetError):
        assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    held[1][1].close()
    for _ in range(100):
        if listener.open_connections() == 0:
            break
        await asyncio.sleep(0.02)
    assert listener.open_connections() == 0
    await listener.close()
    await server.shutdown()


async def test_the_first_request_must_come_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first request still incomplete after REQUEST_TIMEOUT_S is dropped; a slow answer to a complete one is not."""
    monkeypatch.setattr(tls, "REQUEST_TIMEOUT_S", 0.2)

    async def handler(request: web.Request) -> web.Response:
        body = await request.read()
        await asyncio.sleep(0.4)  # longer than the timeout: the request itself was complete
        return web.Response(text=f"got {len(body)}")

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, None)
    await listener.start()
    port = listener.port
    # Headers that never end.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n")
    with contextlib.suppress(ConnectionResetError):
        assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    # A body shorter than its Content-Length.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nabc")
    with contextlib.suppress(ConnectionResetError):
        assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    # Complete, in two pieces: answered after the timeout all the same.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\nConnection: close\r\n\r\n")
    await asyncio.sleep(0.05)
    writer.write(b"abc")
    answer = await asyncio.wait_for(reader.read(), 5)
    assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"got 3")
    writer.close()
    await listener.close()
    await server.shutdown()


async def test_every_request_must_come_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadline holds for each request of a connection, chunked bodies too; the wait between requests is not one."""
    monkeypatch.setattr(tls, "REQUEST_TIMEOUT_S", 0.3)

    async def handler(request: web.Request) -> web.Response:
        body = await request.read()
        return web.Response(text=f"got {len(body)}")

    server = web.Server(handler)
    listener = DualListener(server, "127.0.0.1", 0, None)
    await listener.start()
    port = listener.port

    async def answer(reader: asyncio.StreamReader) -> bytes:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        length = int(next(line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")))
        return await asyncio.wait_for(reader.readexactly(length), 5)

    async def dropped(reader: asyncio.StreamReader) -> bool:
        with contextlib.suppress(ConnectionResetError):
            return await asyncio.wait_for(reader.read(), 5) == b""
        return True

    # A chunked body, complete in pieces (with a trailer): answered.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n")
    await asyncio.sleep(0.05)
    writer.write(b"2;ext=1\r\nde\r\n0\r\nX-Trailer: 1\r\n\r\n")
    assert await answer(reader) == b"got 5"
    # The connection then waits longer than the deadline between two requests: that is not one.
    await asyncio.sleep(0.5)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert await answer(reader) == b"got 0"
    # A second request that never ends is dropped like a first one.
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n")
    assert await dropped(reader)
    writer.close()
    # A chunked body that never ends: dropped.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab")
    assert await dropped(reader)
    writer.close()
    # Two requests sent at once (pipelined): both answered.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\nhiGET / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert await answer(reader) == b"got 2" and await answer(reader) == b"got 0"
    writer.close()
    await listener.close()
    await server.shutdown()


def test_request_watch_framing() -> None:
    """Requests are told complete by their framing; one it cannot follow stops the watch (aiohttp refuses it)."""
    watch = tls._RequestWatch()
    assert watch.feed(b"GET / HTTP/1.1\r\nHost: x\r") == [True] and watch.busy
    assert watch.feed(b"\n\r\n") == [False] and not watch.busy
    # Byte by byte, a chunked body with a trailer.
    events: list[bool] = []
    for byte in b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nwiki\r\n0\r\nA: b\r\n\r\n":
        events += watch.feed(bytes([byte]))
    assert events == [True, False]
    bad = tls._RequestWatch()
    bad.feed(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n")
    assert bad.off
    long_head = tls._RequestWatch()
    long_head.feed(b"GET / HTTP/1.1\r\nX: " + b"a" * (tls.MAX_HEAD_BYTES + 1))
    assert long_head.off
    long_line = tls._RequestWatch()
    long_line.feed(b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n" + b"1" * (tls.MAX_LINE_BYTES + 1))
    assert long_line.off
