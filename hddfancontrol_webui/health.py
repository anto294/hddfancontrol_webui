"""How likely a drive is to fail, and how likely it is to still work in a few years: statistics, not a forecast.

The failure rate of a drive is the rate of a drive of its age, times how
much its SMART counters raise it:

- By age, from Backblaze's Drive Stats of today's drives (the bathtub curve
  revisited with the 2025 fleet): about FLEET_AFR (1.36 %) a year through
  most of their life, then a rise to 4.25 % at 10 years and 3 months
  (AGE_AFR). The rise is taken to start at 8 years; past the last point the
  same slope goes on, never above AGE_AFR_CAP: Backblaze has few drives
  that old.
- By counter, from the annual failure rates that Backblaze observed for
  drives with each count, as tabulated by Scrutiny
  (github.com/AnalogJ/scrutiny, pkg/thresholds/ata_attribute_metadata.go,
  MIT License, Copyright (c) 2020 Jason Kulatunga), reduced to the counters
  smart.py reads, with the rates rounded to four digits. That table comes
  from an older fleet whose clean drives failed more often than today's, so
  it is used only as a ratio: the rate at the drive's count over the rate
  at a count of 0 (relative_risk). The drive takes its worst counter (the
  counters move together, so they are not multiplied), and at least
  GROWING_RATIO when a critical counter grew in the last GROWTH_DAYS (from
  the trend kept by history.py): a growing count is the clearest sign of a
  dying drive.

The rate over FLEET_AFR gives the colour band (BAND_LIMITS, 0 to 6). For a
hard drive, the chance it still works in SURVIVAL_YEARS comes from the same
rates, rising with its age year by year. SSD and NVMe report their wear
(percentage_used): their life left is the wear left at the pace of the
drive's own hours, or of its last GROWTH_DAYS when the trend shows the wear
moving. A drive that SMART itself says is failing is "replace soon".
"""

from __future__ import annotations

import math
from typing import Any

# (low, high, annual failure rate) per raw count, from Scrutiny's ObservedThresholds.
_AFR_TABLE: dict[str, tuple[tuple[int, int, float], ...]] = {
    "reallocated_sectors": (  # 5
        (0, 0, 0.0252),
        (1, 4, 0.0274),
        (4, 16, 0.0750),
        (16, 70, 0.2359),
        (70, 260, 0.3619),
        (260, 1100, 0.5677),
        (1100, 4500, 1.5028),
        (4500, 17000, 2.0660),
        (17000, 70000, 1.7755),
    ),
    "spin_retries": ((0, 0, 0.0546), (1, 80, 0.5556)),  # 10
    "reported_uncorrectable": (  # 187
        (0, 0, 0.0281),
        (1, 1, 0.3388),
        (1, 3, 0.2406),
        (3, 6, 0.5014),
        (6, 11, 0.3801),
        (11, 20, 0.5346),
        (20, 35, 0.8428),
        (35, 65, 1.4429),
        (65, 120, 1.6191),
    ),
    "reallocation_events": (  # 196
        (0, 0, 0.0074),
        (1, 1, 0.0266),
        (1, 2, 0.0247),
        (2, 4, 0.0320),
        (4, 7, 0.0431),
        (7, 11, 0.0338),
        (11, 17, 0.1698),
        (17, 27, 0.0590),
        (27, 45, 0.2470),
    ),
    "pending_sectors": (  # 197
        (0, 0, 0.0255),
        (1, 2, 0.3420),
        (2, 6, 0.6824),
        (6, 16, 0.6108),
        (16, 40, 0.9565),
        (40, 100, 1.6520),
        (100, 250, 2.5138),
        (250, 650, 3.3203),
        (650, 1600, 3.1330),
    ),
    "offline_uncorrectable": (  # 198
        (0, 0, 0.0287),
        (1, 2, 0.8136),
        (2, 4, 1.1173),
        (4, 6, 1.3559),
        (6, 8, 0.7324),
        (8, 10, 0.5777),
        (10, 12, 1.7464),
        (12, 14, 2.6449),
        (14, 16, 0.7970),
    ),
    "crc_errors": (  # 199, not critical: a cable more often than the drive
        (0, 1, 0.0407),
        (1, 2, 0.1513),
        (2, 4, 0.1685),
        (4, 8, 0.1539),
        (8, 16, 0.1488),
        (16, 35, 0.2088),
        (35, 70, 0.1374),
        (70, 130, 0.2234),
        (130, 260, 0.1828),
    ),
}
ATTRIBUTE_IDS = {
    "reallocated_sectors": 5,
    "spin_retries": 10,
    "reported_uncorrectable": 187,
    "reallocation_events": 196,
    "pending_sectors": 197,
    "offline_uncorrectable": 198,
    "crc_errors": 199,
}
CRITICAL = frozenset(ATTRIBUTE_IDS) - {"crc_errors"}
# Scrutiny's limits: a critical counter fails at AFR_FAILED; another warns there and fails at AFR_FAILED_OTHER.
# Kept in the attributes of the answer; the drive's own rate and band come from the ratios.
AFR_FAILED = 0.10
AFR_FAILED_OTHER = 0.20

# Backblaze Drive Stats, the bathtub curve revisited (2025): today's drives fail at about this rate a year...
FLEET_AFR = 0.0136
# ...until a rise to 4.25 % at 10 years 3 months: (age in years, annual failure rate), linear in between.
AGE_AFR = ((0.0, FLEET_AFR), (8.0, FLEET_AFR), (10.25, 0.0425))
AGE_AFR_CAP = 0.10
# No rate above this: even a drive in a bad way has some chance of a year.
AFR_MAX = 0.95
# A critical counter still growing counts at least as this many times a clean drive.
GROWING_RATIO = 4.0
# The colour bands: the drive's rate over FLEET_AFR at or above each limit moves it up one band (0 to 6).
BAND_LIMITS = (1.5, 2.5, 4.0, 8.0, 16.0, 32.0)
# The band of a drive whose own warnings (NVMe) or SMART verdict say something is wrong.
WARNING_BAND = 4
FAILING_BAND = 6
# Hard drives: the chance they still work after this many years.
SURVIVAL_YEARS = (1, 3, 5)

# The trend counts a counter as growing when it rose over this many days.
GROWTH_DAYS = 30
# NVMe/SSD: the trend's pace of wear is used once it covers this many days and moved.
WEAR_PACE_DAYS = 30
HOURS_PER_YEAR = 8766.0

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_UNKNOWN = "unknown"


# ---------------------------
#   failure_rate
# ---------------------------
def failure_rate(key: str, count: int) -> float | None:
    """The annual failure rate Backblaze saw for drives with this count of a counter, or None (no table).

    The first bucket that holds the count, as Scrutiny reads it; past the
    last bucket, the highest rate of every bucket the count passed.
    """
    table = _AFR_TABLE.get(key)
    if table is None or count < 0:
        return None
    for low, high, afr in table:
        if low <= count <= high:
            return afr
    return max(afr for low, _high, afr in table if low <= count)


# ---------------------------
#   attribute_status
# ---------------------------
def attribute_status(key: str, afr: float) -> str:
    """ok, warning or failed for a counter at this failure rate, by Scrutiny's limits."""
    if key in CRITICAL:
        return "failed" if afr >= AFR_FAILED else "ok"
    return "failed" if afr >= AFR_FAILED_OTHER else "warning" if afr >= AFR_FAILED else "ok"


# ---------------------------
#   growing
# ---------------------------
def growing(trend: dict[str, list[list[float]]], now: float) -> list[str]:
    """The critical counters that rose over the last GROWTH_DAYS of the trend."""
    since = now - GROWTH_DAYS * 86400
    found = []
    for key in sorted(CRITICAL):
        points = trend.get(key, [])
        before = [value for ts, value in points if ts <= since]
        recent = [value for ts, value in points if ts > since]
        if recent and max(recent) > (before[-1] if before else recent[0]):
            found.append(key)
    return found


# ---------------------------
#   _wear_life
# ---------------------------
def _wear_life(summary: dict[str, Any], trend: dict[str, list[list[float]]], now: float) -> dict[str, Any] | None:
    """Years left of an SSD or NVMe drive from its wear, or None when it says nothing yet."""
    used = summary.get("percentage_used")
    hours = summary.get("power_on_hours")
    if not isinstance(used, int | float) or used >= 100:
        return {"basis": "wear", "years": [0.0, 0.0]} if isinstance(used, int | float) else None
    paces = []
    if isinstance(hours, int | float) and hours > 0 and used > 0:
        paces.append(used / (hours / HOURS_PER_YEAR))  # % a year over its whole life
    points = trend.get("percentage_used", [])
    recent = [(ts, value) for ts, value in points if ts >= now - 365 * 86400]
    if len(recent) >= 2 and recent[-1][0] - recent[0][0] >= WEAR_PACE_DAYS * 86400 and recent[-1][1] > recent[0][1]:
        paces.append((recent[-1][1] - recent[0][1]) / ((recent[-1][0] - recent[0][0]) / (365 * 86400)))
    if not paces:
        return None
    years = sorted((100 - used) / pace for pace in paces)
    return {"basis": "wear", "years": [round(years[0], 1), round(years[-1], 1)]}


# ---------------------------
#   age_afr
# ---------------------------
def age_afr(years: float) -> float:
    """The annual failure rate of a drive of this age with clean counters (AGE_AFR, linear in between)."""
    points = AGE_AFR
    if years <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if years <= x1:
            return y0 + (y1 - y0) * (years - x0) / (x1 - x0)
    (x0, y0), (x1, y1) = points[-2], points[-1]
    return min(AGE_AFR_CAP, y1 + (y1 - y0) * (years - x1) / (x1 - x0))


# ---------------------------
#   relative_risk
# ---------------------------
def relative_risk(key: str, count: int) -> float | None:
    """How many times a clean drive's rate Backblaze saw for this count of a counter (at least 1), or None."""
    afr, clean = failure_rate(key, count), failure_rate(key, 0)
    if afr is None or not clean:
        return None
    return max(1.0, afr / clean)


# ---------------------------
#   band
# ---------------------------
def band(afr: float) -> int:
    """The colour band (0 to 6) of an annual failure rate: how many BAND_LIMITS its ratio to FLEET_AFR reaches."""
    ratio = afr / FLEET_AFR
    return sum(1 for limit in BAND_LIMITS if ratio >= limit)


# ---------------------------
#   survival
# ---------------------------
def survival(age: float, ratio: float, years: float, step: float = 0.05) -> float:
    """The chance a drive of this age and relative risk still works after this many years.

    The rate of each step is the rate of the drive's age then, times its
    ratio, as a constant hazard over the step.
    """
    hazard = 0.0
    elapsed = 0.0
    while elapsed < years - 1e-9:
        span = min(step, years - elapsed)
        afr = min(AFR_MAX, age_afr(age + elapsed + span / 2) * ratio)
        hazard += -math.log(1.0 - afr) * span
        elapsed += span
    return math.exp(-hazard)


# ---------------------------
#   assess
# ---------------------------
def assess(summary: dict[str, Any], trend: dict[str, list[list[float]]], now: float) -> dict[str, Any] | None:
    """The failure rate, colour band and life of a drive from its SMART summary and trend; None for a reading with no data.

    {risk, band, afr, ratio, times, attributes: [{key, id, value, afr, ratio, band, status}], growing: [key],
    life: {basis: "age", survival: [[years, chance]]} | {basis: "wear", years: [low, high]} | {basis: "risk"} | None}
    """
    if summary.get("status") in (None, "error", "standby"):
        return None
    attributes: list[dict[str, Any]] = []
    for key, attr_id in ATTRIBUTE_IDS.items():
        value = summary.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        afr, ratio = failure_rate(key, value), relative_risk(key, value)
        if afr is not None and ratio is not None:
            attributes.append(
                {
                    "key": key,
                    "id": attr_id,
                    "value": value,
                    "afr": afr,
                    "ratio": round(ratio, 1),
                    "band": band(FLEET_AFR * ratio),
                    "status": attribute_status(key, afr),
                }
            )
    rising = growing(trend, now)
    nvme = isinstance(summary.get("percentage_used"), int | float)
    hdd = summary.get("rpm") is not None
    hours = summary.get("power_on_hours")
    age = hours / HOURS_PER_YEAR if isinstance(hours, int | float) and hours >= 0 else None
    ratio = max((a["ratio"] for a in attributes), default=1.0)
    if rising:
        ratio = max(ratio, GROWING_RATIO)
    rate: float | None = None
    if summary.get("status") == "failing":
        level = FAILING_BAND
    elif attributes:
        # The age curve is the one of hard drives; an SSD with ATA counters takes the rate of a young drive.
        rate = min(AFR_MAX, age_afr(age if hdd and age is not None else 0.0) * ratio)
        level = band(rate)
    elif nvme:
        level = WARNING_BAND if summary.get("warnings") else 0
    else:
        return {
            "risk": RISK_UNKNOWN,
            "band": None,
            "afr": None,
            "ratio": None,
            "times": None,
            "attributes": [],
            "growing": rising,
            "life": None,
        }
    risk = RISK_LOW if level <= 2 else RISK_MEDIUM if level <= 4 else RISK_HIGH
    life: dict[str, Any] | None
    if level == FAILING_BAND and summary.get("status") == "failing":
        life = {"basis": "risk"}
    elif nvme:
        life = _wear_life(summary, trend, now)
    elif hdd and age is not None and rate is not None:
        life = {"basis": "age", "survival": [[years, round(survival(age, ratio, years), 3)] for years in SURVIVAL_YEARS]}
    else:
        life = None  # a SATA SSD names its wear in vendor attributes only
    return {
        "risk": risk,
        "band": level,
        "afr": None if rate is None else round(rate, 4),
        "ratio": None if rate is None else round(ratio, 1),
        "times": None if rate is None else round(rate / FLEET_AFR, 1),
        "attributes": attributes,
        "growing": rising,
        "life": life,
    }
