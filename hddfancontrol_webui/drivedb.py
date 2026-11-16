"""Brand and product line of a drive from its model name, and what the line is made for.

smartctl already names the family of many drives (model_family, from the
drive database of smartmontools). This small catalog adds what the web UI
shows next to it: the brand, the product line, its use (NAS, enterprise,
desktop, surveillance) and, where it is known for the whole pattern, the
recording method (CMR or SMR; SMR drives are a poor fit for ZFS and RAID
rebuilds). Temperature limits are not written here: the drive reports its
own (SCT), which smart.py reads.

Only lines whose model numbers are unambiguous are listed; an unknown model
gets only its brand, or nothing.
"""

from __future__ import annotations

import re
from typing import Any

USE_NAS = "nas"
USE_ENTERPRISE = "enterprise"
USE_DESKTOP = "desktop"
USE_SURVEILLANCE = "surveillance"
USE_SSD = "ssd"
USES = (USE_NAS, USE_ENTERPRISE, USE_DESKTOP, USE_SURVEILLANCE, USE_SSD)

# Brand from the start of the model name, first match wins.
_BRANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(WDC |WD\d|WUH|WUS|WD_)", re.I), "Western Digital"),
    (re.compile(r"^(HGST|HUH|HUS|HMS|Hitachi)", re.I), "HGST"),
    (re.compile(r"^ST\d", re.I), "Seagate"),
    (re.compile(r"^(TOSHIBA|HDW|MG\d\d|MD\d\d)", re.I), "Toshiba"),
    (re.compile(r"^SAMSUNG", re.I), "Samsung"),
    (re.compile(r"^CT\d", re.I), "Crucial"),
    (re.compile(r"^(KINGSTON|SA400|SKC|SNV)", re.I), "Kingston"),
    (re.compile(r"^INTEL", re.I), "Intel"),
    (re.compile(r"^Micron", re.I), "Micron"),
    (re.compile(r"^(SanDisk|SDSSD)", re.I), "SanDisk"),
]

# Product lines: (pattern on the model name without the brand prefix, brand, line, use, recording or None).
_LINES: list[tuple[re.Pattern[str], str, str, str, str | None]] = [
    # Western Digital: the letters after the capacity name the line.
    (re.compile(r"^WD(20|30|40|60)EFAX"), "Western Digital", "WD Red", USE_NAS, "SMR"),
    (re.compile(r"^WD\d+EFRX"), "Western Digital", "WD Red / Red Plus", USE_NAS, "CMR"),
    (re.compile(r"^WD\d+EF(ZX|ZZ|PX|AX)"), "Western Digital", "WD Red / Red Plus", USE_NAS, "CMR"),
    (re.compile(r"^WD\d+(FFBX|KFBX|KFGX|FFWX)"), "Western Digital", "WD Red Pro", USE_NAS, "CMR"),
    (re.compile(r"^WD\d+PUR"), "Western Digital", "WD Purple", USE_SURVEILLANCE, None),
    (re.compile(r"^WD\d+(FRYZ|FYYZ|KRYZ|VRYZ)"), "Western Digital", "WD Gold", USE_ENTERPRISE, "CMR"),
    (re.compile(r"^WD\d+(FZBX|FZWX|FAEX)"), "Western Digital", "WD Black", USE_DESKTOP, "CMR"),
    (re.compile(r"^WD\d+(EZAZ|EZEX|EZRZ|EZRX|EARX|EARZ)"), "Western Digital", "WD Blue", USE_DESKTOP, None),
    (re.compile(r"^WU[HS]\d"), "Western Digital", "Ultrastar", USE_ENTERPRISE, "CMR"),
    (re.compile(r"^(HUH|HUS)\d"), "HGST", "Ultrastar", USE_ENTERPRISE, "CMR"),
    # Seagate: two letters after the capacity name the line.
    (re.compile(r"^ST\d+VN"), "Seagate", "IronWolf", USE_NAS, "CMR"),
    (re.compile(r"^ST\d+NE"), "Seagate", "IronWolf Pro", USE_NAS, "CMR"),
    (re.compile(r"^ST\d+NM"), "Seagate", "Exos / Constellation", USE_ENTERPRISE, "CMR"),
    (re.compile(r"^ST\d+(VX|SKV)"), "Seagate", "SkyHawk", USE_SURVEILLANCE, None),
    (re.compile(r"^ST\d+DM00[48]"), "Seagate", "BarraCuda", USE_DESKTOP, "SMR"),
    (re.compile(r"^ST\d+DM"), "Seagate", "BarraCuda", USE_DESKTOP, None),
    (re.compile(r"^ST\d+LM0(15|24|48|49)"), "Seagate", 'BarraCuda 2.5"', USE_DESKTOP, "SMR"),
    # Toshiba.
    (re.compile(r"^HDWG\d"), "Toshiba", "N300", USE_NAS, "CMR"),
    (re.compile(r"^HDWE\d"), "Toshiba", "X300", USE_DESKTOP, "CMR"),
    (re.compile(r"^HDWD\d"), "Toshiba", "P300", USE_DESKTOP, None),
    (re.compile(r"^MG\d\d"), "Toshiba", "MG (Enterprise)", USE_ENTERPRISE, "CMR"),
]

_PREFIX = re.compile(r"^(WDC|TOSHIBA|HGST|Hitachi|SAMSUNG)[ _]+", re.I)


# ---------------------------
#   lookup
# ---------------------------
def lookup(model: str, rotational: bool | None = None) -> dict[str, Any]:
    """Return what is known of a model: brand, line, use and recording (each may be missing)."""
    model = model.strip()
    info: dict[str, Any] = {}
    for pattern, brand in _BRANDS:
        if pattern.search(model):
            info["brand"] = brand
            break
    bare = _PREFIX.sub("", model)
    for pattern, brand, line, use, recording in _LINES:
        if pattern.search(bare):
            info.update(brand=brand, line=line, use=use)
            if recording:
                info["recording"] = recording
            break
    if rotational is False:
        info["use"] = USE_SSD
        info.pop("recording", None)
    return info
