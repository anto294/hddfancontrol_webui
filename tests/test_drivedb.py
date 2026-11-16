"""Brand and line of a drive from its model name."""

from __future__ import annotations

import pytest

from hddfancontrol_webui.drivedb import lookup


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("WDC WD40EFRX-68N32N0", {"brand": "Western Digital", "line": "WD Red / Red Plus", "use": "nas", "recording": "CMR"}),
        ("WDC WD40EFAX-68JH4N1", {"brand": "Western Digital", "line": "WD Red", "use": "nas", "recording": "SMR"}),
        ("WDC WD101EFAX-68LDBN0", {"brand": "Western Digital", "line": "WD Red / Red Plus", "use": "nas", "recording": "CMR"}),
        ("WDC WD20PURZ-85GU6Y0", {"brand": "Western Digital", "line": "WD Purple", "use": "surveillance"}),
        (
            "ST16000NM001G-2KK103",
            {"brand": "Seagate", "line": "Exos / Constellation", "use": "enterprise", "recording": "CMR"},
        ),
        ("ST4000DM004-2CV104", {"brand": "Seagate", "line": "BarraCuda", "use": "desktop", "recording": "SMR"}),
        ("ST2000DM001-1ER164", {"brand": "Seagate", "line": "BarraCuda", "use": "desktop"}),
        ("TOSHIBA HDWG480", {"brand": "Toshiba", "line": "N300", "use": "nas", "recording": "CMR"}),
        ("TOSHIBA MG08ACA16TE", {"brand": "Toshiba", "line": "MG (Enterprise)", "use": "enterprise", "recording": "CMR"}),
        ("HGST HUH721010ALE604", {"brand": "HGST", "line": "Ultrastar", "use": "enterprise", "recording": "CMR"}),
        ("Samsung SSD 980 1TB", {"brand": "Samsung"}),
        ("CT500P2SSD8", {"brand": "Crucial"}),
        ("Mystery Drive 9000", {}),
    ],
)
def test_lookup(model: str, expected: dict[str, str]) -> None:
    """Known lines get brand, line, use and recording; others what is known."""
    assert lookup(model) == expected


def test_ssd_has_no_recording() -> None:
    """A drive that does not rotate is an SSD, whatever its name says."""
    assert lookup("Samsung SSD 870", rotational=False) == {"brand": "Samsung", "use": "ssd"}
