"""Privilege separation: the user, folder, socket and environment of the gateway."""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui import privsep
from hddfancontrol_webui.privsep import WebUser, gateway_env, prepare_web_dir, secure_socket, socket_dir, web_user


def test_not_root_runs_as_the_same_user() -> None:
    """Without root the gateway runs as the current user, which alone may reach the core."""
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    user = web_user("anyone")
    assert user.uid is None and user.peers == frozenset({os.getuid()})


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Pretend to be root: chown is recorded instead of done."""
    chowns: list[tuple[str, int, int]] = []
    monkeypatch.setattr(privsep.os, "geteuid", lambda: 0)
    monkeypatch.setattr(privsep.os, "chown", lambda path, uid, gid, **_: chowns.append((str(path), uid, gid)))
    return chowns


def test_web_user_as_root(as_root: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The named user; nobody when it is missing or is root; root and it may reach the core."""
    users = {
        "hddfancontrol-webui": pwd.struct_passwd(("hddfancontrol-webui", "x", 10001, 10001, "", "/", "/sbin/nologin")),
        "nobody": pwd.struct_passwd(("nobody", "x", 65534, 65534, "", "/", "/sbin/nologin")),
        "root": pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/sh")),
    }

    def getpwnam(name: str) -> pwd.struct_passwd:
        return users[name]

    monkeypatch.setattr(privsep.pwd, "getpwnam", getpwnam)
    user = web_user(None)
    assert user == WebUser("hddfancontrol-webui", 10001, 10001) and user.peers == frozenset({0, 10001})
    assert web_user("missing").name == "nobody"
    assert web_user("root").name == "nobody"


def test_prepare_web_dir(tmp_path: Path, as_root: list[tuple[str, int, int]]) -> None:
    """The gateway's folder: older auth.json and tls/ moved in, a given TLS pair copied, all given to its user."""
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "auth.json").write_text("{}")
    (config / "tls").mkdir()
    (config / "tls" / "cert.pem").write_text("old")
    (config / "history.db").write_text("")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("CERT")
    key.write_text("KEY")
    user = WebUser("web", 10001, 10001)
    web_dir, given_cert, given_key = prepare_web_dir(config, user, str(cert), str(key))
    assert web_dir == config / "web" and stat.S_IMODE(web_dir.stat().st_mode) == 0o700
    assert (web_dir / "auth.json").exists() and not (config / "auth.json").exists()
    assert (web_dir / "tls" / "cert.pem").read_text() == "old"
    assert Path(given_cert).read_text() == "CERT" and Path(given_key).read_text() == "KEY"
    assert stat.S_IMODE(Path(given_key).stat().st_mode) == 0o600
    owned = {path for path, uid, gid in as_root if (uid, gid) == (10001, 10001)}
    assert str(web_dir) in owned and given_key in owned and str(config / "history.db") not in owned
    assert stat.S_IMODE(config.stat().st_mode) == 0o701  # only the search bit for others
    # Again: nothing to move, nothing lost.
    assert prepare_web_dir(config, user)[1:] == ("", "")
    assert (web_dir / "auth.json").exists()


def test_prepare_web_dir_not_root(tmp_path: Path) -> None:
    """Without root nothing changes owner and the config folder keeps its mode."""
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    user = WebUser("me", None, None)
    web_dir, _, _ = prepare_web_dir(config, user)
    assert web_dir.is_dir() and stat.S_IMODE(config.stat().st_mode) == 0o700


def test_socket_folder_and_mode(tmp_path: Path, as_root: list[tuple[str, int, int]]) -> None:
    """Root's folder and socket, open to the gateway's group only; without root, to the user only."""
    user = WebUser("web", 10001, 10002)
    folder = socket_dir(user)
    try:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o710 and (str(folder), 0, 10002) in as_root
        sock = folder / "core.sock"
        sock.write_text("")
        secure_socket(sock, user)
        assert stat.S_IMODE(sock.stat().st_mode) == 0o660 and (str(sock), 0, 10002) in as_root
        secure_socket(sock, WebUser("me", None, None))
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    finally:
        for item in folder.iterdir():
            item.unlink()
        folder.rmdir()
    plain = socket_dir(WebUser("me", None, None))
    assert stat.S_IMODE(plain.stat().st_mode) == 0o700
    plain.rmdir()


def test_gateway_env(tmp_path: Path) -> None:
    """Only the variables the gateway needs, where the core is, its folder, and the copied TLS pair."""
    env = {
        "PATH": "/bin",
        "WEBUI_PASSWORD": "p",
        "WEBUI_ALLOW": "all",
        "NOTIFY_URL": "https://ntfy.sh/secret",
        "DRIVES": "/dev/sda",
        "TLS_CERT": "/given/cert.pem",
    }
    result = gateway_env(env, tmp_path / "core.sock", tmp_path / "web", "/web/tls/c.pem", "/web/tls/k.pem")
    assert result["PATH"] == "/bin" and result["WEBUI_PASSWORD"] == "p" and result["WEBUI_ALLOW"] == "all"
    assert "NOTIFY_URL" not in result and "DRIVES" not in result
    assert result["HFC_CORE_SOCKET"] == str(tmp_path / "core.sock") and result["HOME"] == str(tmp_path / "web")
    assert (result["TLS_CERT"], result["TLS_KEY"]) == ("/web/tls/c.pem", "/web/tls/k.pem")
    assert "TLS_CERT" not in gateway_env(env, tmp_path / "s", tmp_path, "", "")
