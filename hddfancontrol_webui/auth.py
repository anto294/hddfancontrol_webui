"""Username and password of the web UI, and the login sessions.

The credentials come from WEBUI_USERNAME and WEBUI_PASSWORD when the password
variable is set; the web UI cannot change those. Otherwise they can be set in
the web UI, and are kept in /config/auth.json as a scrypt hash, never as the
password itself. Without either, the gateway asks for the setup code of its
log before anything else (gateway.py), and the credentials, once made, can
be changed but not removed.

The gateway keeps this store, as an unprivileged user, in /config/web. A
login gives a random session token in an HttpOnly, SameSite=Strict cookie.
Sessions live in memory: a restart of the container asks everyone to log in
again. A session ends after SESSION_IDLE without a request, or SESSION_LIFETIME
after the login. Scripts can send HTTP Basic credentials instead.
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

from .storage import write_atomic

AUTH_FILE = "auth.json"
SESSION_COOKIE = "hfc_session"
SESSION_LIFETIME = 7 * 86400
# An open page asks for the status every few seconds, so only a closed one goes idle.
SESSION_IDLE = 12 * 3600
MAX_SESSIONS = 1000
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
        password = env.get("WEBUI_PASSWORD", "")
        self._env_password = bool(password)
        # Checked through scrypt like a stored password, so a guess costs the same.
        self._env_salt = secrets.token_bytes(16)
        self._env_hash = _hash(password, self._env_salt) if password else b""
        # token -> (end of its lifetime, last request).
        self._sessions: dict[str, tuple[float, float]] = {}
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
            password_ok = hmac.compare_digest(_hash(password, self._env_salt), self._env_hash)
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
        """Open a session and return its token; past MAX_SESSIONS the oldest ones end."""
        now = time.time()
        live = {t: s for t, s in self._sessions.items() if s[0] > now and now - s[1] < SESSION_IDLE}
        while len(live) >= MAX_SESSIONS:
            del live[min(live, key=lambda t: live[t][1])]
        token = secrets.token_urlsafe(32)
        live[token] = (now + SESSION_LIFETIME, now)
        self._sessions = live
        return token

    def session_valid(self, token: str | None) -> bool:
        """Whether a session token is known and neither expired nor idle; a valid one counts as used now."""
        if not token:
            return False
        session = self._sessions.get(token)
        now = time.time()
        if session is None or session[0] <= now or now - session[1] >= SESSION_IDLE:
            self._sessions.pop(token, None)
            return False
        self._sessions[token] = (session[0], now)
        return True

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
        if not username or len(username) > MAX_FIELD_LENGTH or ":" in username or not username.isprintable():
            raise AuthError("invalid_username")
        if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_FIELD_LENGTH:
            raise AuthError("password_too_short")
        salt = secrets.token_bytes(16)
        data = {"username": username, "salt": salt.hex(), "hash": _hash(password, salt).hex()}
        # Readable only by the gateway's user: the hash is not the password, but still.
        write_atomic(self.path, json.dumps(data))
        self._file = data
        self._sessions.clear()

    def status(self) -> dict[str, Any]:
        """Return the state as JSON-ready data, without any secret."""
        return {"enabled": self.enabled, "source": self.source, "username": self.username}
