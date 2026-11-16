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
