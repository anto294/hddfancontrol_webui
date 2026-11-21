"""Risk and remaining life of a drive from its SMART data and trend."""

from __future__ import annotations

import pytest

from hddfancontrol_webui.health import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    assess,
    attribute_status,
    failure_rate,
    growing,
)

NOW = 1_800_000_000
DAY = 86400
HDD = {
    "status": "ok",
    "rpm": 7200,
    "power_on_hours": 8766 * 2,
    "reallocated_sectors": 0,
    "pending_sectors": 0,
    "crc_errors": 0,
}


@pytest.mark.parametrize(
    ("key", "count", "afr"),
    [
        ("reallocated_sectors", 0, 0.0252),
        ("reallocated_sectors", 4, 0.0274),  # the first bucket that holds it, as Scrutiny
        ("pending_sectors", 2000, 3.3203),  # past the last bucket: the highest rate passed
        ("crc_errors", 1, 0.0407),
        ("power_on_hours", 5, None),
        ("pending_sectors", -1, None),
    ],
)
def test_failure_rate(key: str, count: int, afr: float | None) -> None:
    """Counts are looked up in Scrutiny's buckets of Backblaze's failure rates."""
    assert failure_rate(key, count) == afr


def test_attribute_status() -> None:
    """A critical counter fails at 10 %; another warns at 10 % and fails at 20 %."""
    assert attribute_status("pending_sectors", 0.0255) == "ok"
    assert attribute_status("pending_sectors", 0.10) == "failed"
    assert attribute_status("crc_errors", 0.15) == "warning"
    assert attribute_status("crc_errors", 0.2088) == "failed"


def test_growing() -> None:
    """A critical counter that rose in the last 30 days grows; an old rise, or a flat one, does not."""
    trend = {
        "pending_sectors": [[NOW - 40 * DAY, 0], [NOW - 5 * DAY, 2]],
        "reallocated_sectors": [[NOW - 90 * DAY, 0], [NOW - 60 * DAY, 8], [NOW - DAY, 8]],
        "reported_uncorrectable": [[NOW - 10 * DAY, 1], [NOW - DAY, 3]],
        "crc_errors": [[NOW - 40 * DAY, 0], [NOW - DAY, 9]],
    }
    assert growing(trend, NOW) == ["pending_sectors", "reported_uncorrectable"]


def test_healthy_hard_drive() -> None:
    """Low risk, and five to seven years of power-on time less its age."""
    health = assess(HDD, {}, NOW)
    assert health is not None
    assert health["risk"] == RISK_LOW and health["afr"] == 0.0407
    assert health["life"] == {"basis": "age", "years": [3.0, 5.0]}
    assert {a["id"] for a in health["attributes"]} == {5, 197, 199}
    old = assess({**HDD, "power_on_hours": 8766 * 9}, {}, NOW)
    assert old is not None and old["life"]["years"] == [0.0, 0.0]


def test_risk_levels() -> None:
    """A failed critical counter is high; a warning, a growing counter or a high rate is medium."""
    high = assess({**HDD, "pending_sectors": 1}, {}, NOW)
    assert high is not None and high["risk"] == RISK_HIGH and high["life"] == {"basis": "risk", "years": [0.0, 0.0]}
    assert (assess({**HDD, "status": "failing"}, {}, NOW) or {})["risk"] == RISK_HIGH
    warned = assess({**HDD, "crc_errors": 3}, {}, NOW)
    assert warned is not None and warned["risk"] == RISK_MEDIUM and warned["life"]["years"] == [1.5, 2.5]
    trend = {"reallocated_sectors": [[NOW - 40 * DAY, 0], [NOW - DAY, 1]]}
    rising = assess({**HDD, "reallocated_sectors": 1}, trend, NOW)
    assert rising is not None and rising["risk"] == RISK_MEDIUM and rising["growing"] == ["reallocated_sectors"]
    assert (assess({**HDD, "reallocated_sectors": 10}, {}, NOW) or {})["risk"] == RISK_MEDIUM  # 7.5 %


def test_nvme_life_from_wear() -> None:
    """The wear left at the pace of the drive's hours, and of the last months when the trend moves."""
    nvme = {"status": "ok", "percentage_used": 10, "power_on_hours": 8766, "warnings": []}
    health = assess(nvme, {}, NOW)
    assert health is not None and health["risk"] == RISK_LOW
    assert health["life"] == {"basis": "wear", "years": [9.0, 9.0]}
    trend = {"percentage_used": [[NOW - 182.5 * DAY, 5], [NOW, 10]]}  # 10 % a year lately
    assert (assess(nvme, trend, NOW) or {})["life"]["years"] == [9.0, 9.0]
    trend = {"percentage_used": [[NOW - 73 * DAY, 6], [NOW, 10]]}  # 20 % a year lately
    assert (assess(nvme, trend, NOW) or {})["life"]["years"] == [4.5, 9.0]
    assert (assess({**nvme, "percentage_used": 0}, {}, NOW) or {})["life"] is None
    assert (assess({**nvme, "percentage_used": 100}, {}, NOW) or {})["life"]["years"] == [0.0, 0.0]
    assert (assess({**nvme, "warnings": ["media_errors"]}, {}, NOW) or {})["risk"] == RISK_MEDIUM


def test_nothing_to_judge() -> None:
    """No reading, a sleeping or unreadable drive: no assessment; a SATA SSD has a risk but no life."""
    assert assess({"status": "standby"}, {}, NOW) is None
    assert assess({"status": "error"}, {}, NOW) is None
    assert assess({}, {}, NOW) is None
    unknown = assess({"status": "ok"}, {}, NOW)
    assert unknown is not None and unknown["risk"] == RISK_UNKNOWN and unknown["life"] is None
    ssd = assess({"status": "ok", "reallocated_sectors": 0, "power_on_hours": 100}, {}, NOW)
    assert ssd is not None and ssd["risk"] == RISK_LOW and ssd["life"] is None
    assert (assess({**HDD, "power_on_hours": None}, {}, NOW) or {})["life"] is None
