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
#   DualListener
# ---------------------------
class DualListener:
    """Accept connections on one port and hand each to the server, over TLS or not, by its first byte."""

    def __init__(self, factory: Callable[[], asyncio.BaseProtocol], host: str, port: int, context: ssl.SSLContext) -> None:
        """Serve factory()'s protocol (aiohttp's web.Server) on host:port."""
        self._factory = factory
        self._host = host
        self._port = port
        self._context = context
        self._sock: socket.socket | None = None
        self._accepting: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        """The port listened on (the one chosen by the system when 0 was asked)."""
        if self._sock is None:
            raise RuntimeError("not listening")
        return int(self._sock.getsockname()[1])

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

    async def _accept(self, sock: socket.socket) -> None:
        """Accept for ever; each connection is told apart in its own task."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                conn, _ = await loop.sock_accept(sock)
            except OSError as err:  # out of file descriptors, for example: try again
                _LOGGER.warning("Cannot accept a connection: %s", err)
                await asyncio.sleep(1)
                continue
            task = asyncio.create_task(self._hand_over(conn))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _hand_over(self, conn: socket.socket) -> None:
        """Wait for the first byte, then give the connection to the server with or without TLS."""
        loop = asyncio.get_running_loop()
        conn.setblocking(False)
        try:
            first = await asyncio.wait_for(self._peek(conn), SNIFF_TIMEOUT_S)
            if not first:
                conn.close()
                return
            context = self._context if first[0] == TLS_HANDSHAKE else None
            await loop.connect_accepted_socket(
                self._factory, conn, ssl=context, ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S if context else None
            )
        except asyncio.CancelledError:
            conn.close()
            raise
        except (OSError, TimeoutError) as err:  # ssl.SSLError is an OSError: a browser that refused the certificate
            _LOGGER.debug("Connection dropped before the server: %s", err)
            conn.close()

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
