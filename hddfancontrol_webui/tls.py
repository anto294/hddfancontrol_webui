"""HTTPS for the web UI: the certificate, and one port that answers both HTTPS and plain HTTP.

With ENABLE_TLS the web UI speaks TLS with TLS_CERT and TLS_KEY, or with
a self-signed certificate made at the first start in /config/tls (by the
openssl program, so no Python dependency). The port stays the same: a
TrueNAS app publishes one port, and a browser that still asks for
http:// must get an answer, not a broken connection. So DualListener
looks at the first byte of each connection: 0x16 opens a TLS handshake,
anything else is plain HTTP. Both go to the same aiohttp server; with
FORCE_HTTPS the web app redirects plain HTTP to HTTPS (web.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess  # nosec B404  # openssl, without a shell, with fixed arguments
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .aio import cancel_and_wait

_LOGGER = logging.getLogger(__name__)

TLS_DIR = "tls"
CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
CERT_DAYS = 3650
OPENSSL_TIMEOUT_S = 60
# The first byte of a TLS record that carries a handshake (the ClientHello).
TLS_HANDSHAKE = 0x16
# A connection that sends nothing for this long is closed before it is told apart.
SNIFF_TIMEOUT_S = 10.0
HANDSHAKE_TIMEOUT_S = 10.0
# The first request of a connection (its headers, and its body by Content-Length) must
# be complete this long after the first byte: aiohttp waits for headers for ever.
REQUEST_TIMEOUT_S = 30.0
# Open connections at most, of one address and in all; more are closed at once.
MAX_CONNECTIONS_PER_ADDRESS = 64
MAX_CONNECTIONS = 512


# ---------------------------
#   TlsError
# ---------------------------
class TlsError(RuntimeError):
    """The certificate cannot be read or made."""


# ---------------------------
#   make_certificate
# ---------------------------
def make_certificate(cert: Path, key: Path, openssl: str = "openssl") -> None:
    """Make a self-signed certificate for this host, valid CERT_DAYS. Blocking.

    The names are the host name, localhost and 127.0.0.1; a browser warns
    once for a self-signed certificate whatever its names, and the user
    can replace it with TLS_CERT and TLS_KEY.
    """
    program = shutil.which(openssl)
    if program is None:
        raise TlsError(f"cannot make a certificate: {openssl} is not installed; set TLS_CERT and TLS_KEY")
    cert.parent.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname() or "localhost"
    names = ",".join(dict.fromkeys([f"DNS:{host}", "DNS:localhost", "IP:127.0.0.1"]))
    args = [
        program,
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-days",
        str(CERT_DAYS),
        "-subj",
        f"/CN={host}",
        "-addext",
        f"subjectAltName={names}",
        "-keyout",
        str(key),
        "-out",
        str(cert),
    ]
    old_mask = os.umask(0o077)
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603  # fixed arguments, no shell
            args, capture_output=True, text=True, timeout=OPENSSL_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise TlsError(f"cannot make a certificate: {err}") from err
    finally:
        os.umask(old_mask)
    if result.returncode != 0:
        raise TlsError(f"cannot make a certificate: {result.stderr.strip() or result.returncode}")
    key.chmod(0o600)


# ---------------------------
#   ssl_context
# ---------------------------
def ssl_context(config_dir: Path, cert: str = "", key: str = "", openssl: str = "openssl") -> ssl.SSLContext:
    """The server context: the given certificate and key, or the self-signed pair of config_dir/tls. Blocking."""
    if bool(cert) != bool(key):
        raise TlsError("set both TLS_CERT and TLS_KEY, or neither")
    if cert:
        cert_path, key_path = Path(cert), Path(key)
    else:
        cert_path, key_path = config_dir / TLS_DIR / CERT_FILE, config_dir / TLS_DIR / KEY_FILE
        if not cert_path.exists() or not key_path.exists():
            _LOGGER.info("Making a self-signed certificate in %s", cert_path.parent)
            make_certificate(cert_path, key_path, openssl)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert_path, key_path)
    except (OSError, ssl.SSLError) as err:
        raise TlsError(f"cannot load {cert_path} and {key_path}: {err}") from err
    return context


# ---------------------------
#   _Counted
# ---------------------------
class _Counted(asyncio.Protocol):
    """The server's protocol for one connection, with its place in the count and a deadline for its first request.

    Every call is passed on to the server's own protocol. The connection is
    closed when its first request (the headers, then a body as long as its
    Content-Length says) is not complete within REQUEST_TIMEOUT_S of the
    hand-over: aiohttp itself waits for headers for ever. When the
    connection ends, its place in the count is given back.
    """

    def __init__(self, inner: asyncio.BaseProtocol, release: Callable[[], None]) -> None:
        """Pass everything on to inner; call release() once, when the connection ends."""
        self._inner = inner
        self._release: Callable[[], None] | None = release
        self._received = bytearray()
        self._needed: int | None = None
        self._deadline: asyncio.TimerHandle | None = None
        self._transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Start the deadline of the first request, and hand the transport to the server."""
        self._transport = transport
        self._deadline = asyncio.get_running_loop().call_later(REQUEST_TIMEOUT_S, self._too_slow)
        self._inner.connection_made(transport)

    def _too_slow(self) -> None:
        """The first request did not come in time: drop the connection."""
        _LOGGER.debug("Connection dropped: no complete request within %s s", REQUEST_TIMEOUT_S)
        if self._transport is not None:
            self._transport.abort()  # type: ignore[attr-defined]  # a socket transport, given by connect_accepted_socket

    def _watch(self, data: bytes) -> None:
        """Follow the first request until it is complete, then stop the deadline."""
        self._received += data
        if self._needed is None:
            end = self._received.find(b"\r\n\r\n")
            if end < 0:
                return
            length = re.search(rb"\r\ncontent-length:[ \t]*([0-9]{1,12})[ \t]*\r\n", self._received[: end + 2].lower())
            self._needed = end + 4 + (int(length.group(1)) if length else 0)
        if len(self._received) >= self._needed:
            self._done_waiting()

    def _done_waiting(self) -> None:
        """Stop the deadline and the copy of the first request."""
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None
        self._received = bytearray()

    def data_received(self, data: bytes) -> None:
        """Pass the data on, after watching the first request."""
        if self._deadline is not None:
            self._watch(data)
        self._inner.data_received(data)  # type: ignore[attr-defined]  # aiohttp's handler is a Protocol

    def eof_received(self) -> bool | None:
        """Pass it on."""
        return self._inner.eof_received()  # type: ignore[attr-defined, no-any-return]  # a Protocol

    def pause_writing(self) -> None:
        """Pass it on."""
        self._inner.pause_writing()

    def resume_writing(self) -> None:
        """Pass it on."""
        self._inner.resume_writing()

    def connection_lost(self, exc: Exception | None) -> None:
        """Give the place back, then pass it on."""
        self._done_waiting()
        if self._release is not None:
            self._release()
            self._release = None
        self._inner.connection_lost(exc)


# ---------------------------
#   DualListener
# ---------------------------
class DualListener:
    """Accept connections on one port and hand each to the server, over TLS or not, by its first byte.

    Without a context, every connection is plain HTTP. Before anything is
    read, admit(address) decides whether the address may connect at all
    (WEBUI_ALLOW), and at most MAX_CONNECTIONS_PER_ADDRESS connections of
    one address, MAX_CONNECTIONS in all, are open: a client that opens
    thousands of silent connections is cut off before it runs the gateway
    out of file descriptors. A first request must be complete within
    REQUEST_TIMEOUT_S (_Counted).
    """

    def __init__(
        self,
        factory: Callable[[], asyncio.BaseProtocol],
        host: str,
        port: int,
        context: ssl.SSLContext | None,
        admit: Callable[[str], bool] | None = None,
    ) -> None:
        """Serve factory()'s protocol (aiohttp's web.Server) on host:port, to the addresses admit() lets in."""
        self._factory = factory
        self._host = host
        self._port = port
        self._context = context
        self._admit = admit
        self._sock: socket.socket | None = None
        self._accepting: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[None]] = set()
        # Open connections per address, from accept to close.
        self._open: dict[str, int] = {}

    @property
    def port(self) -> int:
        """The port listened on (the one chosen by the system when 0 was asked)."""
        if self._sock is None:
            raise RuntimeError("not listening")
        return int(self._sock.getsockname()[1])

    def open_connections(self) -> int:
        """How many connections are open now, from accept to close."""
        return sum(self._open.values())

    async def start(self) -> None:
        """Listen, and accept in the background."""
        family = socket.AF_INET6 if ":" in self._host else socket.AF_INET
        self._sock = socket.create_server((self._host, self._port), family=family, backlog=128)
        self._sock.setblocking(False)
        self._accepting = asyncio.create_task(self._accept(self._sock))

    async def close(self) -> None:
        """Stop listening and drop the connections not yet handed over."""
        if self._accepting is not None:
            await cancel_and_wait(self._accepting)
        for task in list(self._pending):
            await cancel_and_wait(task)
        if self._sock is not None:
            self._sock.close()

    def _take_place(self, address: str) -> bool:
        """Count a new connection of address; False (not counted) when it or all of them are at their limit."""
        if self._open.get(address, 0) >= MAX_CONNECTIONS_PER_ADDRESS or self.open_connections() >= MAX_CONNECTIONS:
            return False
        self._open[address] = self._open.get(address, 0) + 1
        return True

    def _give_place(self, address: str) -> None:
        """A connection of address ended."""
        left = self._open.get(address, 0) - 1
        if left > 0:
            self._open[address] = left
        else:
            self._open.pop(address, None)

    async def _accept(self, sock: socket.socket) -> None:
        """Accept for ever; refuse at once what admit() or the limits refuse, and tell each other connection apart in its own task."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, peer = await loop.sock_accept(sock)
            except OSError as err:  # out of file descriptors, for example: try again
                _LOGGER.warning("Cannot accept a connection: %s", err)
                await asyncio.sleep(1)
                continue
            address = str(peer[0])
            if self._admit is not None and not self._admit(address):
                conn.close()
                continue
            if not self._take_place(address):
                _LOGGER.debug("Connection of %s refused: too many open", address)
                conn.close()
                continue
            task = asyncio.create_task(self._hand_over(conn, address))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _hand_over(self, conn: socket.socket, address: str) -> None:
        """Wait for the first byte, then give the connection to the server with or without TLS."""
        loop = asyncio.get_running_loop()
        conn.setblocking(False)
        handed = False
        try:
            first = await asyncio.wait_for(self._peek(conn), SNIFF_TIMEOUT_S)
            if not first:
                conn.close()
                return
            context = self._context if first[0] == TLS_HANDSHAKE else None
            await loop.connect_accepted_socket(
                lambda: _Counted(self._factory(), lambda: self._give_place(address)),
                conn,
                ssl=context,
                ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S if context else None,
            )
            handed = True
        except asyncio.CancelledError:
            conn.close()
            raise
        except (OSError, TimeoutError) as err:  # ssl.SSLError is an OSError: a browser that refused the certificate
            _LOGGER.debug("Connection dropped before the server: %s", err)
            conn.close()
        finally:
            if not handed:
                self._give_place(address)

    @staticmethod
    async def _peek(conn: socket.socket) -> bytes:
        """The first byte of the connection, left in it; empty when it closed."""
        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        loop.add_reader(conn.fileno(), readable.set)
        try:
            await readable.wait()
        finally:
            with suppress(ValueError, OSError):  # a socket closed meanwhile
                loop.remove_reader(conn.fileno())
        return conn.recv(1, socket.MSG_PEEK)
