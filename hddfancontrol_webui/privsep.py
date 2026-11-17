"""Privilege separation, the root side: the user, folder, socket and environment of the gateway.

The core runs as root and never listens on the network. It starts the
gateway (gateway.py) as WEBUI_USER, which only gets:

- /config/web (mode 0700, its own): the credentials and the self-signed
  TLS pair. Root never writes in it. The rest of /config is root's, mode
  0600 (the core runs with umask 077).
- /config/web-tls (root's, readable by its group): a copy of TLS_CERT and
  TLS_KEY, when they are set.
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
from .storage import write_atomic
from .tls import TLS_DIR

_LOGGER = logging.getLogger(__name__)

FALLBACK_USER = "nobody"
# The ids of nobody on most systems, for one without that user.
FALLBACK_ID = 65534
SOCKET_FILE = "core.sock"
TEMP_WEB_PREFIX = "hddfancontrol-webui-web-"
GIVEN_TLS_DIR = "web-tls"
GIVEN_CERT = "cert.pem"
GIVEN_KEY = "key.pem"
# The copies of TLS_CERT/TLS_KEY that edge builds kept in /config/web/tls.
OLD_COPIES = ("given-cert.pem", "given-key.pem")
# A certificate or key larger than this is not one.
MAX_TLS_FILE = 1024 * 1024
# What every PEM certificate or key holds.
PEM_MARK = "-----BEGIN "
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
    "WEBUI_HOSTS",
    "WEBUI_ACCESS_LOG",
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

    Not running as root, the gateway runs as the current user. Root, and a
    user whose group is root's, are refused: the point is to have the
    network in another user, without root's group files.
    """
    log = log or _LOGGER
    if os.geteuid() != 0:
        return WebUser(pwd.getpwuid(os.getuid()).pw_name, None, None)
    wanted = (name or "").strip() or DEFAULT_WEB_USER
    try:
        entry = pwd.getpwnam(wanted)
    except KeyError:
        log.warning("User %s does not exist: the gateway runs as %s", wanted, FALLBACK_USER)
        return _fallback_user()
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        log.warning("WEBUI_USER %s is root or in root's group: the gateway runs as %s", wanted, FALLBACK_USER)
        return _fallback_user()
    return WebUser(entry.pw_name, entry.pw_uid, entry.pw_gid)


# ---------------------------
#   _fallback_user
# ---------------------------
def _fallback_user() -> WebUser:
    """nobody, or its usual ids when the system has no such user: never root."""
    try:
        entry = pwd.getpwnam(FALLBACK_USER)
    except KeyError:
        return WebUser(FALLBACK_USER, FALLBACK_ID, FALLBACK_ID)
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        return WebUser(FALLBACK_USER, FALLBACK_ID, FALLBACK_ID)
    return WebUser(entry.pw_name, entry.pw_uid, entry.pw_gid)


# ---------------------------
#   _give
# ---------------------------
def _give(path: Path, user: WebUser) -> None:
    """Give a file or a folder (and what is in it) to the user of the gateway.

    Never follows a link, and skips a file with more than one name: a hard
    link could be another name of a file of root's.
    """
    if user.uid is None or user.gid is None:
        return
    paths = [path]
    if path.is_dir() and not path.is_symlink():
        paths += list(path.rglob("*"))
    for item in paths:
        info = item.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            _LOGGER.warning("Not giving %s to %s: it has more than one name", item, user.name)
            continue
        os.chown(item, user.uid, user.gid, follow_symlinks=False)


# ---------------------------
#   prepare_web_dir
# ---------------------------
def prepare_web_dir(config_dir: Path, user: WebUser, log: logging.Logger | None = None) -> Path:
    """Make /config/web for the gateway and return it; a temporary folder when that fails.

    The fans come first: a /config that cannot hold the folder (read only,
    a link in its place) costs the stored credentials while it lasts, never
    the core. The gateway then asks for a new setup, with the code of the
    log like a first start, and the error in the log says why.
    """
    log = log or _LOGGER
    try:
        return _prepare_web_dir(config_dir, user, log)
    except OSError as err:
        # The folders of earlier failed starts: only one is ever in use.
        for stale in Path(tempfile.gettempdir()).glob(TEMP_WEB_PREFIX + "*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale, ignore_errors=True)  # never follows links (fd based on Linux)
        folder = Path(tempfile.mkdtemp(prefix=TEMP_WEB_PREFIX))
        _give(folder, user)
        log.error("Cannot prepare the gateway's folder in %s (%s): it uses %s until the next start", config_dir, err, folder)
        return folder


# ---------------------------
#   _prepare_web_dir
# ---------------------------
def _prepare_web_dir(config_dir: Path, user: WebUser, log: logging.Logger) -> Path:
    """Make /config/web for the gateway and return it. Raises OSError.

    What is in the folder is the gateway's: root never writes, changes or
    follows anything in it, so a gateway taken over cannot turn a link
    there into a write as root. Root only makes the folder, moves in
    auth.json and tls/ of an older version (kept in /config, root's), and
    gives them to the user; the whole folder only when it belonged to
    another user. /config itself gets the search bit for others, so the
    gateway can reach its folder and nothing else.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    web_dir = config_dir / WEB_DIR
    if web_dir.is_symlink() or (web_dir.exists() and not web_dir.is_dir()):
        raise OSError(f"{web_dir} is not a folder")
    web_dir.mkdir(mode=0o700, exist_ok=True)
    web_dir.chmod(0o700)  # not a link (checked above), in root's /config
    if user.uid is not None and web_dir.lstat().st_uid != user.uid:
        _give(web_dir, user)
    for name in (AUTH_FILE, TLS_DIR):
        old, new = config_dir / name, web_dir / name
        if old.exists() and not os.path.lexists(new):
            old.rename(new)
            _give(new, user)
            log.info("Moved %s to %s", old, new)
    _drop_old_copies(web_dir / TLS_DIR, log)
    if user.uid is not None:
        mode = stat.S_IMODE(config_dir.stat().st_mode)
        try:
            config_dir.chmod(mode | stat.S_IXOTH)
        except OSError as err:  # an ACL of the host may forbid it: the gateway then says what it cannot open
            log.warning("Cannot let the gateway into %s: %s", config_dir, err)
    return web_dir


# ---------------------------
#   _drop_old_copies
# ---------------------------
def _drop_old_copies(tls_dir: Path, log: logging.Logger) -> None:
    """Remove the TLS_CERT/TLS_KEY copies that edge builds made in /config/web/tls (now in /config/web-tls).

    The folder is the gateway's: it is opened without following a link, and
    the names are removed inside it, so a link there removes only itself.
    """
    try:
        fd = os.open(tls_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        for name in OLD_COPIES:
            try:
                os.unlink(name, dir_fd=fd)
                log.info("Removed the old copy %s", tls_dir / name)
            except FileNotFoundError:
                continue
    except OSError as err:
        log.warning("Cannot remove the old TLS copies in %s: %s", tls_dir, err)
    finally:
        os.close(fd)


# ---------------------------
#   _gateway_can_change
# ---------------------------
def _gateway_can_change(source: str, user: WebUser) -> bool:
    """Whether the gateway's user could change the file or put another in its place.

    Every part of the path, as written and as its links lead, is checked:
    one that the user owns, or a folder that it may write (its group's, or
    everyone's without the sticky bit), lets it swap the file for a link to
    a file of root's, which root would then copy for it.
    """
    if user.uid is None:
        return False
    for text in (os.path.abspath(source), os.path.realpath(source)):
        path = Path(text)
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if info.st_uid == user.uid:
                return True
            if stat.S_ISDIR(info.st_mode) and not info.st_mode & stat.S_ISVTX:
                if info.st_mode & stat.S_IWOTH or (info.st_mode & stat.S_IWGRP and info.st_gid == user.gid):
                    return True
    return False


# ---------------------------
#   _read_pem
# ---------------------------
def _read_pem(source: str) -> str:
    """Read a PEM file as root: a regular file, not through a last link, at most MAX_TLS_FILE bytes. Raises OSError, ValueError.

    A file without a PEM block is refused, so a wrong path never hands the
    gateway the text of another file of root's.
    """
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{source} is not a regular file")
        data = handle.read(MAX_TLS_FILE + 1)
    if len(data) > MAX_TLS_FILE:
        raise ValueError(f"{source} is larger than {MAX_TLS_FILE} bytes")
    text = data.decode("utf-8")
    if PEM_MARK not in text:
        raise ValueError(f"{source} is not a PEM file")
    return text


# ---------------------------
#   given_tls
# ---------------------------
def given_tls(config_dir: Path, user: WebUser, cert: str, key: str, log: logging.Logger | None = None) -> tuple[str, str]:
    """Copy TLS_CERT and TLS_KEY where the gateway can read them, and return the copies.

    The copies go to /config/web-tls, root's folder that only the gateway's
    group may read, so the gateway cannot change them or what root writes.
    Root copies only files the gateway cannot change or replace by a link
    (one in /config/web is the gateway's own: it reads it itself). A half
    pair, or a pair that cannot be read, is returned as it was given: the
    gateway then reports it and leaves HTTPS off.
    """
    log = log or _LOGGER
    folder = config_dir / GIVEN_TLS_DIR
    if not (cert and key):
        shutil.rmtree(folder, ignore_errors=True)
        return cert, key
    if any(_gateway_can_change(source, user) for source in (cert, key)):
        shutil.rmtree(folder, ignore_errors=True)
        log.info("TLS_CERT and TLS_KEY are in a place of the gateway's user: it reads them itself")
        return cert, key
    try:
        if folder.is_symlink():
            folder.unlink()
        folder.mkdir(mode=0o700, exist_ok=True)
        targets = []
        for source, name in ((cert, GIVEN_CERT), (key, GIVEN_KEY)):
            target = folder / name
            write_atomic(target, _read_pem(source))
            if user.gid is not None:
                os.chown(target, 0, user.gid, follow_symlinks=False)
                target.chmod(0o640)
            targets.append(str(target))
        if user.gid is not None:
            os.chown(folder, 0, user.gid, follow_symlinks=False)
            folder.chmod(0o750)
    except (OSError, ValueError) as err:
        shutil.rmtree(folder, ignore_errors=True)  # never half a pair
        log.error("Cannot copy TLS_CERT and TLS_KEY for the gateway: %s", err)
        return cert, key
    return targets[0], targets[1]


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
    """The environment of the gateway: GATEWAY_ENV, where to find the core and its folder, and the TLS pair to use."""
    result = {name: env[name] for name in GATEWAY_ENV if name in env}
    result[CORE_SOCKET_ENV] = str(socket_path)
    result[WEB_DIR_ENV] = str(web_dir)
    result["HOME"] = str(web_dir)
    for name, value in (("TLS_CERT", cert), ("TLS_KEY", key)):
        if value:
            result[name] = value
    return result
