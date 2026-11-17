"""Username, password and sessions."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from hddfancontrol_webui import auth as auth_module
from hddfancontrol_webui.auth import AuthError, AuthStore


def basic(user: str, password: str) -> str:
    """Build a Basic header."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_open_by_default(tmp_path: Path) -> None:
    """Without credentials nothing is checked and nothing passes check()."""
    auth = AuthStore(tmp_path, {})
    assert not auth.enabled and auth.status() == {"enabled": False, "source": "none", "username": ""}
    assert not auth.check("admin", "")


def test_environment_credentials(tmp_path: Path) -> None:
    """WEBUI_USERNAME and WEBUI_PASSWORD win and cannot be changed in the web UI."""
    auth = AuthStore(tmp_path, {"WEBUI_USERNAME": "nas", "WEBUI_PASSWORD": "s3cret-pass"})
    assert auth.source == "environment" and auth.username == "nas"
    assert auth.check("nas", "s3cret-pass")
    assert not auth.check("nas", "wrong") and not auth.check("admin", "s3cret-pass")
    assert auth.check_basic(basic("nas", "s3cret-pass"))
    assert not auth.check_basic("Basic !!!") and not auth.check_basic("Bearer x")
    with pytest.raises(AuthError, match="auth_from_env"):
        auth.set_credentials("a", "longenough", "s3cret-pass")
    assert AuthStore(tmp_path, {"WEBUI_PASSWORD": "x"}).username == "admin"


def test_stored_credentials(tmp_path: Path) -> None:
    """Set and change the stored credentials; the file holds no password."""
    auth = AuthStore(tmp_path, {})
    with pytest.raises(AuthError, match="password_too_short"):
        auth.set_credentials("admin", "short", "")
    with pytest.raises(AuthError, match="invalid_username"):
        auth.set_credentials("a:b", "longenough", "")
    auth.set_credentials(" anto ", "first-password", "")
    stored = (tmp_path / "auth.json").read_text()
    assert "first-password" not in stored and json.loads(stored)["username"] == "anto"
    assert (tmp_path / "auth.json").stat().st_mode & 0o077 == 0
    assert auth.check("anto", "first-password")

    reloaded = AuthStore(tmp_path, {})
    assert reloaded.source == "file" and reloaded.check("anto", "first-password")
    with pytest.raises(AuthError, match="wrong_password"):
        reloaded.set_credentials("anto", "second-password", "nope")
    token = reloaded.new_session()
    reloaded.set_credentials("anto", "second-password", "first-password")
    assert not reloaded.session_valid(token)
    assert not hasattr(reloaded, "clear_credentials")  # credentials can be changed, never removed


def test_broken_file(tmp_path: Path) -> None:
    """A broken auth.json counts as none (the setup code is asked again), and a bad hash never matches."""
    (tmp_path / "auth.json").write_text("{nope")
    assert not AuthStore(tmp_path, {}).enabled
    (tmp_path / "auth.json").write_text(json.dumps({"username": "a", "salt": "zz", "hash": "zz"}))
    auth = AuthStore(tmp_path, {})
    assert auth.enabled and not auth.check("a", "anything")


def test_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions expire and can be ended."""
    auth = AuthStore(tmp_path, {})
    token = auth.new_session()
    assert auth.session_valid(token) and not auth.session_valid("other") and not auth.session_valid(None)
    auth.end_session(token)
    assert not auth.session_valid(token)
    token = auth.new_session()
    now = auth_module.time.time()
    monkeypatch.setattr(auth_module.time, "time", lambda: now + auth_module.SESSION_LIFETIME + 1)
    assert not auth.session_valid(token)
    auth.new_session()
    assert token not in auth._sessions


def test_sessions_end_when_idle_or_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session ends after SESSION_IDLE without use, or SESSION_LIFETIME after the login; the table is bounded."""
    from hddfancontrol_webui import auth as auth_module

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(auth_module.time, "time", lambda: clock["now"])
    auth = AuthStore(tmp_path, {"WEBUI_PASSWORD": "s3cret-pass"})
    token = auth.new_session()
    for _ in range(20):  # used every 10 hours: never idle, until its lifetime ends
        clock["now"] += 10 * 3600
        if not auth.session_valid(token):
            break
    assert clock["now"] - 1_000_000.0 > auth_module.SESSION_LIFETIME - 10 * 3600
    idle = auth.new_session()
    clock["now"] += auth_module.SESSION_IDLE
    assert not auth.session_valid(idle) and not auth.session_valid(None)
    monkeypatch.setattr(auth_module, "MAX_SESSIONS", 3)
    tokens = []
    for _ in range(4):
        clock["now"] += 1
        tokens.append(auth.new_session())
    assert not auth.session_valid(tokens[0]) and all(auth.session_valid(t) for t in tokens[1:])
