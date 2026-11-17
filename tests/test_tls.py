"""Tests of HTTPS: the certificate and the port that answers both HTTPS and HTTP."""

from __future__ import annotations

import asyncio
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
