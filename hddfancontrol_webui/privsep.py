"""Privilege separation, the root side: the user, folder, socket and environment of the gateway.

The core runs as root and never listens on the network. It starts the
gateway (gateway.py) as WEBUI_USER, which only gets:

- /config/web (mode 0700, its own): the credentials and the TLS files. The
  rest of /config is root's, mode 0600 (the core runs with umask 077).
- a Unix socket to the core, in a folder only root and that user's group
  can enter.
- a short list of environment variables: not NOTIFY_URL or anything else
  it does not need.

Run as another user than root (tests, development), the gateway runs as
that same user and nothing changes owner.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .auth import AUTH_FILE
from .const import CORE_SOCKET_ENV, DEFAULT_WEB_USER, WEB_DIR, WEB_DIR_ENV
from .tls import TLS_DIR

_LOGGER = logging.getLogger(__name__)

FALLBACK_USER = "nobody"
SOCKET_FILE = "core.sock"
GIVEN_CERT = "given-cert.pem"
GIVEN_KEY = "given-key.pem"
# The environment of the gateway: these names, and nothing else.
GATEWAY_ENV = (
    "PATH",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
    "TZ",
    "LANG",
    "LC_ALL",
    "APP_VERSION",
    "WEBUI_PORT",
    "WEBUI_HOST",
    "WEBUI_ALLOW",
    "WEBUI_USERNAME",
    "WEBUI_PASSWORD",
    "ENABLE_TLS",
    "FORCE_HTTPS",
    "OPENSSL_BIN",
)


# ---------------------------
#   WebUser
# ---------------------------
@dataclass(frozen=True)
class WebUser:
    """The user the gateway runs as: None for uid/gid means the current user (not root)."""

    name: str
    uid: int | None
    gid: int | None

    @property
    def peers(self) -> frozenset[int]:
        """The user ids that may connect to the core: the gateway's, and root's (or the current user)."""
        return frozenset({0, self.uid}) if self.uid is not None else frozenset({os.getuid()})


# ---------------------------
#   web_user
# ---------------------------
def web_user(name: str | None, log: logging.Logger | None = None) -> WebUser:
    """The user of the gateway: WEBUI_USER (default DEFAULT_WEB_USER), or nobody when it does not exist.

    Not running as root, the gateway runs as the current user. Root itself
    is refused: the point is to have the network in another user.
    """
    log = log or _LOGGER
    if os.geteuid() != 0:
        return WebUser(pwd.getpwuid(os.getuid()).pw_name, None, None)
    wanted = (name or "").strip() or DEFAULT_WEB_USER
    try:
        entry = pwd.getpwnam(wanted)
    except KeyError:
        log.warning("User %s does not exist: the gateway runs as %s", wanted, FALLBACK_USER)
        entry = pwd.getpwnam(FALLBACK_USER)
    if entry.pw_uid == 0:
        log.warning("WEBUI_USER %s is root: the gateway runs as %s", wanted, FALLBACK_USER)
        entry = pwd.getpwnam(FALLBACK_USER)
    return WebUser(entry.pw_name, entry.pw_uid, entry.pw_gid)


# ---------------------------
#   _give
# ---------------------------
def _give(path: Path, user: WebUser) -> None:
    """Give a file or a folder (and what is in it) to the user of the gateway, without following links."""
    if user.uid is None or user.gid is None:
        return
    paths = [path]
    if path.is_dir() and not path.is_symlink():
        paths += [p for p in path.rglob("*")]
    for item in paths:
        os.chown(item, user.uid, user.gid, follow_symlinks=False)


# ---------------------------
#   prepare_web_dir
# ---------------------------
def prepare_web_dir(config_dir: Path, user: WebUser, cert: str = "", key: str = "") -> tuple[Path, str, str]:
    """Make /config/web for the gateway and return it, with the certificate and key it should use.

    Moves auth.json and tls/ of an older version (kept in /config) there.
    A TLS_CERT/TLS_KEY pair is copied in, so the gateway can read it
    whatever the owner and mode of the originals. /config itself gets the
    search bit for others, so the gateway can reach its folder and nothing
    else.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    web_dir = config_dir / WEB_DIR
    web_dir.mkdir(mode=0o700, exist_ok=True)
    for name in (AUTH_FILE, TLS_DIR):
        old, new = config_dir / name, web_dir / name
        if old.exists() and not new.exists():
            old.rename(new)
            _LOGGER.info("Moved %s to %s", old, new)
    given_cert = given_key = ""
    if cert and key:
        tls = web_dir / TLS_DIR
        tls.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(cert, tls / GIVEN_CERT)
        shutil.copyfile(key, tls / GIVEN_KEY)
        (tls / GIVEN_KEY).chmod(0o600)
        given_cert, given_key = str(tls / GIVEN_CERT), str(tls / GIVEN_KEY)
    web_dir.chmod(0o700)
    _give(web_dir, user)
    if user.uid is not None:
        mode = stat.S_IMODE(config_dir.stat().st_mode)
        config_dir.chmod(mode | stat.S_IXOTH)
    return web_dir, given_cert, given_key


# ---------------------------
#   socket_dir
# ---------------------------
def socket_dir(user: WebUser) -> Path:
    """A new folder for the core's socket, which only root and the gateway's group can enter."""
    folder = Path(tempfile.mkdtemp(prefix="hddfancontrol-webui-"))
    if user.gid is not None:
        os.chown(folder, 0, user.gid)
        folder.chmod(0o710)
    return folder


# ---------------------------
#   secure_socket
# ---------------------------
def secure_socket(path: Path, user: WebUser) -> None:
    """Let the gateway's group, and only it, connect to the core's socket."""
    if user.gid is not None:
        os.chown(path, 0, user.gid)
        path.chmod(0o660)
    else:
        path.chmod(0o600)


# ---------------------------
#   gateway_env
# ---------------------------
def gateway_env(env: dict[str, str], socket_path: Path, web_dir: Path, cert: str, key: str) -> dict[str, str]:
    """The environment of the gateway: GATEWAY_ENV, where to find the core and its folder, and the copied TLS pair."""
    result = {name: env[name] for name in GATEWAY_ENV if name in env}
    result[CORE_SOCKET_ENV] = str(socket_path)
    result[WEB_DIR_ENV] = str(web_dir)
    result["HOME"] = str(web_dir)
    if cert and key:
        result["TLS_CERT"], result["TLS_KEY"] = cert, key
    return result
