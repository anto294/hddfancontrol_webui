"""Privilege separation: the user, folder, socket and environment of the gateway."""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui import privsep
from hddfancontrol_webui.privsep import WebUser, gateway_env, given_tls, prepare_web_dir, secure_socket, socket_dir, web_user


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
        "wheel": pwd.struct_passwd(("wheel", "x", 1000, 0, "", "/", "/bin/sh")),
    }

    def getpwnam(name: str) -> pwd.struct_passwd:
        return users[name]

    monkeypatch.setattr(privsep.pwd, "getpwnam", getpwnam)
    user = web_user(None)
    assert user == WebUser("hddfancontrol-webui", 10001, 10001) and user.peers == frozenset({0, 10001})
    assert web_user("missing").name == "nobody"
    assert web_user("root").name == "nobody"
    assert web_user("wheel").name == "nobody"  # root's group
    # No nobody either: its usual ids, never root.
    del users["nobody"]
    assert web_user("missing") == WebUser("nobody", 65534, 65534)
    users["nobody"] = users["root"]
    assert web_user("root") == WebUser("nobody", 65534, 65534)


def test_prepare_web_dir(tmp_path: Path, as_root: list[tuple[str, int, int]]) -> None:
    """The gateway's folder: older auth.json and tls/ moved in and given to its user, nothing else of /config."""
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "auth.json").write_text("{}")
    (config / "tls").mkdir()
    (config / "tls" / "cert.pem").write_text("old")
    (config / "history.db").write_text("")
    user = WebUser("web", 10001, 10001)
    web_dir = prepare_web_dir(config, user)
    assert web_dir == config / "web" and stat.S_IMODE(web_dir.stat().st_mode) == 0o700
    assert (web_dir / "auth.json").exists() and not (config / "auth.json").exists()
    assert (web_dir / "tls" / "cert.pem").read_text() == "old"
    owned = {path for path, uid, gid in as_root if (uid, gid) == (10001, 10001)}
    assert {str(web_dir), str(web_dir / "auth.json"), str(web_dir / "tls" / "cert.pem")} <= owned
    assert str(config / "history.db") not in owned
    assert stat.S_IMODE(config.stat().st_mode) == 0o701  # only the search bit for others
    # The copies of edge builds go, through no link.
    secret = tmp_path / "given-key.pem"
    secret.write_text("elsewhere")
    (web_dir / "tls" / "given-cert.pem").symlink_to(secret)
    (web_dir / "tls" / "given-key.pem").write_text("KEY")
    prepare_web_dir(config, user)
    assert not os.path.lexists(web_dir / "tls" / "given-cert.pem") and not (web_dir / "tls" / "given-key.pem").exists()
    assert secret.read_text() == "elsewhere" and (web_dir / "tls" / "cert.pem").exists()
    # Again: nothing to move, nothing lost.
    assert prepare_web_dir(config, user) == web_dir and (web_dir / "auth.json").exists()


def test_prepare_web_dir_touches_nothing_the_gateway_put_there(
    tmp_path: Path, as_root: list[tuple[str, int, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link or a hard link the gateway left in its folder is never followed, written or given by root."""
    config = tmp_path / "config"
    web_dir = config / "web"
    web_dir.mkdir(parents=True)
    secret = config / "settings.json"
    secret.write_text("root's")
    (web_dir / "tls").symlink_to(config)
    (web_dir / "hard").hardlink_to(secret)
    (config / "tls").mkdir()  # an older tls/ is not moved over the link
    user = WebUser("web", os.getuid(), os.getgid())
    prepare_web_dir(config, user)
    assert as_root == [] and (web_dir / "tls").is_symlink()  # already the gateway's: nothing given again
    # Another owner: everything given, but not the hard link nor through the link.
    other = WebUser("web", os.getuid() + 1, os.getgid())
    prepare_web_dir(config, other)
    given = {Path(path).name for path, _, _ in as_root}
    assert "web" in given and "tls" in given and "hard" not in given and "settings.json" not in given
    # A link in place of the folder itself is refused: the gateway gets a temporary folder.
    web_dir.rename(config / "moved")
    web_dir.symlink_to(config / "moved")
    monkeypatch.setattr(privsep.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    temporary = prepare_web_dir(config, user)
    assert temporary.is_dir() and temporary.parent == tmp_path / "tmp"
    # The next failed start replaces it: never one more folder per start.
    (temporary / "auth.json").write_text("{}")
    again = prepare_web_dir(config, user)
    assert again != temporary and list((tmp_path / "tmp").iterdir()) == [again]


def test_prepare_web_dir_without_the_search_bit(tmp_path: Path, as_root: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A /config whose mode cannot change (an ACL of the host) is logged, not fatal."""
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    real_chmod = Path.chmod

    def chmod(self: Path, mode: int) -> None:
        if self == config:
            raise PermissionError("EPERM")
        real_chmod(self, mode)

    monkeypatch.setattr(Path, "chmod", chmod)
    assert prepare_web_dir(config, WebUser("web", 10001, 10001)).is_dir()


def test_prepare_web_dir_not_root(tmp_path: Path) -> None:
    """Without root nothing changes owner and the config folder keeps its mode."""
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    user = WebUser("me", None, None)
    web_dir = prepare_web_dir(config, user)
    assert web_dir.is_dir() and stat.S_IMODE(config.stat().st_mode) == 0o700


def test_given_tls(tmp_path: Path, as_root: list[tuple[str, int, int]]) -> None:
    """TLS_CERT and TLS_KEY are copied to root's folder for the gateway's group; a half or unreadable pair is passed as given."""
    config = tmp_path / "config"
    config.mkdir()
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("CERT")
    key.write_text("KEY")
    user = WebUser("web", 10001, 10002)
    copied_cert, copied_key = given_tls(config, user, str(cert), str(key))
    folder = config / "web-tls"
    assert (copied_cert, copied_key) == (str(folder / "cert.pem"), str(folder / "key.pem"))
    assert Path(copied_cert).read_text() == "CERT" and Path(copied_key).read_text() == "KEY"
    assert stat.S_IMODE(Path(copied_key).stat().st_mode) == 0o640 and stat.S_IMODE(folder.stat().st_mode) == 0o750
    assert {(copied_key, 0, 10002), (str(folder), 0, 10002)} <= set(as_root)
    assert given_tls(config, user, str(cert), "") == (str(cert), "")
    assert not folder.exists()  # no pair, no copy left
    assert given_tls(config, user, str(cert), str(tmp_path / "missing")) == (str(cert), str(tmp_path / "missing"))
    assert not folder.exists()  # a failed copy leaves nothing
    folder.symlink_to(tmp_path)  # a link in its place is dropped, not followed
    assert given_tls(config, WebUser("me", None, None), str(cert), str(key))[0] == copied_cert
    assert not (tmp_path / "cert.pem").exists() and not folder.is_symlink()


def test_given_tls_never_reads_what_the_gateway_can_change(
    tmp_path: Path, as_root: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair in a place of the gateway's user, or a link, a FIFO, a huge or binary file, is not copied by root."""
    config = tmp_path / "config"
    config.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("root's secret")
    web = config / "web"
    web.mkdir()
    (web / "cert.pem").symlink_to(secret)
    (web / "key.pem").write_text("KEY")
    # The gateway's user owns /config/web (here: the current user plays it).
    gateway = WebUser("web", os.getuid(), os.getgid())
    root_owned = WebUser("web", os.getuid() + 1, os.getgid())
    pair = (str(web / "cert.pem"), str(web / "key.pem"))
    assert given_tls(config, gateway, *pair) == pair
    assert not (config / "web-tls").exists()
    # Not the gateway's: still no last link, no FIFO, nothing huge, nothing binary; never a half copy.
    key = tmp_path / "k.pem"
    key.write_text("KEY")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    huge = tmp_path / "huge"
    huge.write_bytes(b"x" * (privsep.MAX_TLS_FILE + 1))
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe")
    for bad in (web / "cert.pem", fifo, huge, binary):
        assert given_tls(config, root_owned, str(key), str(bad)) == (str(key), str(bad))
        assert not (config / "web-tls").exists()


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
    """Only the variables the gateway needs, where the core is, its folder, and the TLS pair."""
    env = {
        "PATH": "/bin",
        "WEBUI_PASSWORD": "p",
        "WEBUI_ALLOW": "all",
        "WEBUI_ACCESS_LOG": "true",
        "NOTIFY_URL": "https://ntfy.sh/secret",
        "DRIVES": "/dev/sda",
        "TLS_CERT": "/given/cert.pem",
    }
    result = gateway_env(env, tmp_path / "core.sock", tmp_path / "web", "/web/tls/c.pem", "/web/tls/k.pem")
    assert result["PATH"] == "/bin" and result["WEBUI_PASSWORD"] == "p" and result["WEBUI_ALLOW"] == "all"
    assert result["WEBUI_ACCESS_LOG"] == "true"
    assert "NOTIFY_URL" not in result and "DRIVES" not in result
    assert result["HFC_CORE_SOCKET"] == str(tmp_path / "core.sock") and result["HOME"] == str(tmp_path / "web")
    assert (result["TLS_CERT"], result["TLS_KEY"]) == ("/web/tls/c.pem", "/web/tls/k.pem")
    assert "TLS_CERT" not in gateway_env(env, tmp_path / "s", tmp_path, "", "")
    # A half pair reaches the gateway, which reports it.
    half = gateway_env(env, tmp_path / "s", tmp_path, "/c.pem", "")
    assert half["TLS_CERT"] == "/c.pem" and "TLS_KEY" not in half
