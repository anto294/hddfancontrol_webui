"""Username and password of the web UI, and the login sessions.

The credentials come from WEBUI_USERNAME and WEBUI_PASSWORD when the password
variable is set; the web UI cannot change those. Otherwise they can be set in
the web UI, and are kept in /config/auth.json as a scrypt hash, never as the
password itself. Without either, the web UI is open.

A login gives a random session token in an HttpOnly, SameSite=Strict
cookie. Sessions live in memory: a restart of the container asks everyone to
log in again. Scripts can send HTTP Basic credentials instead.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

AUTH_FILE = "auth.json"
SESSION_COOKIE = "hfc_session"
SESSION_LIFETIME = 7 * 86400
MIN_PASSWORD_LENGTH = 8
MAX_FIELD_LENGTH = 256

# scrypt cost: about 50 ms and 16 MiB per check on a small NAS CPU.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

SOURCE_NONE = "none"
SOURCE_ENV = "environment"
SOURCE_FILE = "file"


# ---------------------------
#   AuthError
# ---------------------------
class AuthError(ValueError):
    """A change of the credentials that cannot be made; the code is translated by the web UI."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        self.code = code
        super().__init__(code)


# ---------------------------
#   _hash
# ---------------------------
def _hash(password: str, salt: bytes) -> bytes:
    """Hash a password with scrypt."""
    return hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)


# ---------------------------
#   AuthStore
# ---------------------------
class AuthStore:
    """Check credentials, keep sessions, and change the stored credentials."""

    def __init__(self, config_dir: Path, env: Mapping[str, str] | None = None) -> None:
        """Read the credentials from the environment or from auth.json."""
        env = os.environ if env is None else env
        self.path = config_dir / AUTH_FILE
        self._env_user = env.get("WEBUI_USERNAME", "admin") or "admin"
        self._env_password = env.get("WEBUI_PASSWORD", "")
        self._sessions: dict[str, float] = {}
        self._file: dict[str, str] | None = self._load()

    def _load(self) -> dict[str, str] | None:
        """Read auth.json, or None when it is missing or broken."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("username", "salt", "hash")):
            return None
        return data

    @property
    def source(self) -> str:
        """Where the credentials come from."""
        if self._env_password:
            return SOURCE_ENV
        if self._file is not None:
            return SOURCE_FILE
        return SOURCE_NONE

    @property
    def enabled(self) -> bool:
        """Whether a login is needed."""
        return self.source != SOURCE_NONE

    @property
    def username(self) -> str:
        """The username, or an empty string when the web UI is open."""
        if self.source == SOURCE_ENV:
            return self._env_user
        if self._file is not None:
            return self._file["username"]
        return ""

    def check(self, username: str, password: str) -> bool:
        """Whether the credentials are right. Takes the same time whichever part is wrong."""
        if self.source == SOURCE_ENV:
            user_ok = hmac.compare_digest(username.encode(), self._env_user.encode())
            password_ok = hmac.compare_digest(password.encode(), self._env_password.encode())
            return user_ok and password_ok
        if self._file is not None:
            try:
                salt = bytes.fromhex(self._file["salt"])
                expected = bytes.fromhex(self._file["hash"])
            except ValueError:
                return False
            user_ok = hmac.compare_digest(username.encode(), self._file["username"].encode())
            password_ok = hmac.compare_digest(_hash(password, salt), expected)
            return user_ok and password_ok
        return False

    def check_basic(self, header: str) -> bool:
        """Check an 'Authorization: Basic ...' header."""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
        except (binascii.Error, ValueError):
            return False
        username, _, password = decoded.partition(":")
        return self.check(username, password)

    def new_session(self) -> str:
        """Open a session and return its token."""
        now = time.time()
        self._sessions = {t: exp for t, exp in self._sessions.items() if exp > now}
        token = secrets.token_urlsafe(32)
        self._sessions[token] = now + SESSION_LIFETIME
        return token

    def session_valid(self, token: str | None) -> bool:
        """Whether a session token is known and not expired."""
        if not token:
            return False
        expires = self._sessions.get(token)
        return expires is not None and expires > time.time()

    def end_session(self, token: str | None) -> None:
        """Close a session."""
        if token:
            self._sessions.pop(token, None)

    def set_credentials(self, username: str, password: str, current: str) -> None:
        """Set or change the stored credentials. Every open session ends."""
        if self.source == SOURCE_ENV:
            raise AuthError("auth_from_env")
        if self.enabled and not self.check(self.username, current):
            raise AuthError("wrong_password")
        username = username.strip()
        if not username or len(username) > MAX_FIELD_LENGTH or ":" in username:
            raise AuthError("invalid_username")
        if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_FIELD_LENGTH:
            raise AuthError("password_too_short")
        salt = secrets.token_bytes(16)
        data = {"username": username, "salt": salt.hex(), "hash": _hash(password, salt).hex()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # Readable only by root: the hash is not the password, but still.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)
        self._file = data
        self._sessions.clear()

    def clear_credentials(self, current: str) -> None:
        """Remove the stored credentials, which opens the web UI to everyone."""
        if self.source == SOURCE_ENV:
            raise AuthError("auth_from_env")
        if not self.enabled:
            return
        if not self.check(self.username, current):
            raise AuthError("wrong_password")
        self.path.unlink(missing_ok=True)
        self._file = None
        self._sessions.clear()

    def status(self) -> dict[str, Any]:
        """Return the state as JSON-ready data, without any secret."""
        return {"enabled": self.enabled, "source": self.source, "username": self.username}
