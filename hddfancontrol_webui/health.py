"""How likely a drive is to fail, and how long it may last: an estimate from its SMART data and their trend.

Risk, from Backblaze's data: each critical ATA counter is looked up in the
annual failure rates (AFR) that Backblaze observed on its fleet for drives
with that count. The table is Scrutiny's
(github.com/AnalogJ/scrutiny, pkg/thresholds/ata_attribute_metadata.go,
MIT License, Copyright (c) 2020 Jason Kulatunga), reduced to the counters
smart.py reads, with the rates rounded to four digits. A count past the
last bucket takes the highest rate of the buckets it passed. A critical
counter at an AFR of AFR_FAILED (10 %) or more fails, a non-critical one
warns at AFR_FAILED and fails at AFR_FAILED_OTHER.

The drive's risk is its worst counter, raised to at least "medium" when a
critical counter grew in the last GROWTH_DAYS (from the trend kept by
history.py): a growing count is the clearest sign of a dying drive.

Remaining life is a rough estimate, never a promise:

- SSD and NVMe report their wear (percentage_used): the life left is the
  wear left at the pace of the drive's own hours, or of its last
  GROWTH_DAYS when the trend shows the wear moving.
- A hard drive has no wear counter. Its range starts from HDD_LIFE_YEARS
  of power-on time (5 years is the service life and warranty of NAS and
  enterprise lines; most drives of Backblaze's fleet last longer), less
  its age; a medium risk halves it, a high risk makes it "replace soon".
"""

from __future__ import annotations

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
AFR_FAILED = 0.10
AFR_FAILED_OTHER = 0.20
# Under this AFR a drive is as likely to fail as a healthy one of the fleet (about 2.5 % a year).
AFR_MEDIUM = 0.05

# The trend counts a counter as growing when it rose over this many days.
GROWTH_DAYS = 30
# NVMe/SSD: the trend's pace of wear is used once it covers this many days and moved.
WEAR_PACE_DAYS = 30
# Hard drives: the years of power-on time the range of life starts from.
HDD_LIFE_YEARS = (5.0, 7.0)
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
#   _age_life
# ---------------------------
def _age_life(summary: dict[str, Any], risk: str) -> dict[str, Any] | None:
    """Years left of a hard drive from its age and risk, or None without its hours."""
    hours = summary.get("power_on_hours")
    if not isinstance(hours, int | float) or hours < 0:
        return None
    age = hours / HOURS_PER_YEAR
    factor = 0.5 if risk == RISK_MEDIUM else 1.0
    low, high = (max(0.0, (years - age) * factor) for years in HDD_LIFE_YEARS)
    return {"basis": "age", "years": [round(low, 1), round(high, 1)]}


# ---------------------------
#   assess
# ---------------------------
def assess(summary: dict[str, Any], trend: dict[str, list[list[float]]], now: float) -> dict[str, Any] | None:
    """The risk and remaining life of a drive from its SMART summary and trend; None for a reading with no data.

    {risk, afr, attributes: [{key, id, value, afr, status}], growing: [key], life: {basis, years: [low, high]} | None}
    """
    if summary.get("status") in (None, "error", "standby"):
        return None
    attributes: list[dict[str, Any]] = []
    rates: list[float] = []
    for key, attr_id in ATTRIBUTE_IDS.items():
        value = summary.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        afr = failure_rate(key, value)
        if afr is not None:
            rates.append(afr)
            attributes.append({"key": key, "id": attr_id, "value": value, "afr": afr, "status": attribute_status(key, afr)})
    rising = growing(trend, now)
    nvme = isinstance(summary.get("percentage_used"), int | float)
    worst = max(rates, default=None)
    if summary.get("status") == "failing" or any(a["status"] == "failed" for a in attributes):
        risk = RISK_HIGH
    elif rising or any(a["status"] == "warning" for a in attributes) or (worst is not None and worst >= AFR_MEDIUM):
        risk = RISK_MEDIUM
    elif attributes or nvme:
        risk = RISK_LOW
    else:
        risk = RISK_UNKNOWN
    if nvme and risk == RISK_LOW and summary.get("warnings"):
        risk = RISK_MEDIUM  # media errors, a critical warning, worn out
    # A SATA SSD names its wear in vendor attributes only: no life for it, its risk all the same.
    if risk == RISK_HIGH:
        life: dict[str, Any] | None = {"basis": "risk", "years": [0.0, 0.0]}
    elif nvme:
        life = _wear_life(summary, trend, now)
    else:
        life = _age_life(summary, risk) if summary.get("rpm") is not None else None
    return {"risk": risk, "afr": worst, "attributes": attributes, "growing": rising, "life": life}
