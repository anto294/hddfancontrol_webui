"""The version shown by the web UI."""

from __future__ import annotations

import pytest

from hddfancontrol_webui import const


@pytest.mark.parametrize(
    ("value", "expected"),
    [("v0.3.0", "0.3.0"), ("0.3.0", "0.3.0"), ("v1.0.0b1", "1.0.0b1"), ("edge-abc123", "edge-abc123"), ("vintage", "vintage")],
)
def test_version_drops_the_v_of_the_tag(monkeypatch: pytest.MonkeyPatch, value: str, expected: str) -> None:
    """The image gets the release tag; the web UI adds the "v" itself."""
    monkeypatch.setenv("APP_VERSION", value)
    assert const._version() == expected


def test_env_flag() -> None:
    """The true words, anything else false, and the default for missing or empty."""
    env = {"A": " Yes ", "B": "0", "C": "maybe", "D": ""}
    assert const.env_flag(env, "A") is True
    assert const.env_flag(env, "B", True) is False and const.env_flag(env, "C") is False
    assert const.env_flag(env, "D", True) is True and const.env_flag(env, "missing") is False
