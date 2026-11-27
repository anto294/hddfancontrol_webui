"""Failure rate, colour band and life of a drive from its SMART data and trend."""

from __future__ import annotations

import pytest

from hddfancontrol_webui.health import (
    AGE_AFR_CAP,
    FLEET_AFR,
    GROWING_RATIO,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    SURVIVAL_YEARS,
    age_afr,
    assess,
    attribute_status,
    band,
    failure_rate,
    growing,
    relative_risk,
    survival,
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


def test_age_rate() -> None:
    """Flat at the fleet's rate until 8 years, up to 4.25 % at 10 years 3 months, then the same slope, capped."""
    assert age_afr(0) == FLEET_AFR and age_afr(8) == FLEET_AFR
    assert age_afr(10.25) == pytest.approx(0.0425)
    assert FLEET_AFR < age_afr(9) < 0.0425
    assert age_afr(12) > 0.0425 and age_afr(40) == AGE_AFR_CAP


def test_relative_risk_and_band() -> None:
    """A count is weighed against a clean drive of Scrutiny's table, never below 1; bands follow BAND_LIMITS."""
    assert relative_risk("reallocated_sectors", 0) == 1.0
    assert relative_risk("pending_sectors", 1) == pytest.approx(0.3420 / 0.0255)
    assert relative_risk("crc_errors", 1) == 1.0
    assert relative_risk("power_on_hours", 5) is None
    assert band(FLEET_AFR) == 0 and band(FLEET_AFR * 2) == 1 and band(FLEET_AFR * 9) == 4 and band(0.9) == 6


def test_survival() -> None:
    """The chance falls with the years, faster with a higher ratio and with age."""
    young = survival(1, 1.0, 1)
    assert young == pytest.approx(1 - FLEET_AFR, abs=1e-4)
    assert survival(1, 1.0, 5) < young
    assert survival(1, 10.0, 1) < young
    assert survival(9, 1.0, 1) < young
    assert survival(1, 1.0, 0) == 1.0


def test_healthy_hard_drive() -> None:
    """Clean counters: the fleet's rate, the first band and a high chance; a zero count never raises the risk."""
    clean = {**HDD, "power_on_hours": 8766 * 6, "spin_retries": 0, "reported_uncorrectable": 0, "offline_uncorrectable": 0}
    health = assess(clean, {}, NOW)
    assert health is not None
    assert health["risk"] == RISK_LOW and health["band"] == 0
    assert health["afr"] == FLEET_AFR and health["ratio"] == 1.0 and health["times"] == 1.0
    assert health["life"]["basis"] == "age"
    chances = dict(health["life"]["survival"])
    assert set(chances) == set(SURVIVAL_YEARS) and 0.98 < chances[1] and chances[1] > chances[3] > chances[5] > 0.8
    assert {a["id"] for a in health["attributes"]} == {5, 10, 187, 197, 198, 199}
    assert all(a["band"] == 0 for a in health["attributes"])
    old = assess({**clean, "power_on_hours": 8766 * 11}, {}, NOW)
    assert old is not None and 0 < old["band"] <= 2 and old["afr"] > FLEET_AFR


def test_counters_raise_the_risk() -> None:
    """The worst counter sets the rate; a few reallocated sectors stay green, pending sectors do not."""
    few = assess({**HDD, "reallocated_sectors": 3}, {}, NOW)
    assert few is not None and few["band"] == 0 and few["risk"] == RISK_LOW
    many = assess({**HDD, "reallocated_sectors": 40}, {}, NOW)
    assert many is not None and many["band"] == 4 and many["risk"] == RISK_MEDIUM and many["ratio"] == 9.4
    pending = assess({**HDD, "pending_sectors": 20, "reallocated_sectors": 40}, {}, NOW)
    assert pending is not None and pending["band"] == 6 and pending["risk"] == RISK_HIGH
    assert pending["ratio"] == round(0.9565 / 0.0255, 1)
    assert dict(pending["life"]["survival"])[1] < 0.6
    attrs = {a["key"]: a for a in pending["attributes"]}
    assert attrs["pending_sectors"]["band"] == 6 and attrs["crc_errors"]["band"] == 0


def test_growing_counter_counts_more() -> None:
    """A critical counter that grew counts at least GROWING_RATIO times a clean drive."""
    trend = {"reallocated_sectors": [[NOW - 40 * DAY, 0], [NOW - DAY, 1]]}
    rising = assess({**HDD, "reallocated_sectors": 1}, trend, NOW)
    assert rising is not None and rising["growing"] == ["reallocated_sectors"] and rising["ratio"] == GROWING_RATIO
    assert rising["band"] == band(FLEET_AFR * GROWING_RATIO)


def test_failing_drive() -> None:
    """SMART's own verdict: the last band and "replace soon"."""
    failing = assess({**HDD, "status": "failing"}, {}, NOW)
    assert (
        failing is not None and failing["band"] == 6 and failing["risk"] == RISK_HIGH and failing["life"] == {"basis": "risk"}
    )


def test_nvme_life_from_wear() -> None:
    """The wear left at the pace of the drive's hours, and of the last months when the trend moves."""
    nvme = {"status": "ok", "percentage_used": 10, "power_on_hours": 8766, "warnings": []}
    health = assess(nvme, {}, NOW)
    assert health is not None and health["risk"] == RISK_LOW and health["band"] == 0 and health["afr"] is None
    assert health["life"] == {"basis": "wear", "years": [9.0, 9.0]}
    trend = {"percentage_used": [[NOW - 182.5 * DAY, 5], [NOW, 10]]}  # 10 % a year lately
    assert (assess(nvme, trend, NOW) or {})["life"]["years"] == [9.0, 9.0]
    trend = {"percentage_used": [[NOW - 73 * DAY, 6], [NOW, 10]]}  # 20 % a year lately
    assert (assess(nvme, trend, NOW) or {})["life"]["years"] == [4.5, 9.0]
    assert (assess({**nvme, "percentage_used": 0}, {}, NOW) or {})["life"] is None
    assert (assess({**nvme, "percentage_used": 100}, {}, NOW) or {})["life"]["years"] == [0.0, 0.0]
    warned = assess({**nvme, "warnings": ["media_errors"]}, {}, NOW)
    assert warned is not None and warned["risk"] == RISK_MEDIUM and warned["band"] == 4


def test_nothing_to_judge() -> None:
    """No reading, a sleeping or unreadable drive: no assessment; a SATA SSD has a rate but no life."""
    assert assess({"status": "standby"}, {}, NOW) is None
    assert assess({"status": "error"}, {}, NOW) is None
    assert assess({}, {}, NOW) is None
    unknown = assess({"status": "ok"}, {}, NOW)
    assert unknown is not None and unknown["risk"] == RISK_UNKNOWN and unknown["band"] is None and unknown["life"] is None
    ssd = assess({"status": "ok", "reallocated_sectors": 0, "power_on_hours": 8766 * 12}, {}, NOW)
    assert ssd is not None and ssd["afr"] == FLEET_AFR and ssd["life"] is None  # no age curve for an SSD
    assert (assess({**HDD, "power_on_hours": None}, {}, NOW) or {})["life"] is None
