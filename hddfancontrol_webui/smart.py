"""Read the SMART data of the drives with smartctl, without waking them.

smartctl runs with ``-n standby``: a drive that sleeps is not read, and its
last data stays as it was. The JSON output (``-j``) is reduced to what the
web UI shows, plus the attribute table for the details.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from .const import SMARTCTL_BIN
from .drivedb import lookup

# ATA attributes that should stay at 0 on a healthy drive.
WATCHED_ATTRIBUTES = {
    5: "reallocated_sectors",
    10: "spin_retries",
    187: "reported_uncorrectable",
    196: "reallocation_events",
    197: "pending_sectors",
    198: "offline_uncorrectable",
    199: "crc_errors",
}
COUNTERS = {4: "start_stop_count", 193: "load_cycle_count"}
NVME_WEAR_WARNING = 90
# A failing drive can keep smartctl waiting for minutes; the SMART refresh
# reads the drives one after the other and must not hang on one.
SMARTCTL_TIMEOUT = 120.0

# Bits of the smartctl exit status.
EXIT_OPEN_FAILED = 0x02
EXIT_SMART_FAILING = 0x08

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_FAILING = "failing"
STATUS_STANDBY = "standby"
STATUS_ERROR = "error"


# ---------------------------
#   failed_now
# ---------------------------
def failed_now(when_failed: str | None) -> bool:
    """Whether the when_failed of an attribute means it is under its threshold now.

    smartctl says "now", or "past" for an attribute that went under its
    threshold once and came back (often the airflow temperature of a drive
    that ran hot): that is history, not a failure. Anything else that is
    not empty counts as now.
    """
    when = (when_failed or "").strip().lower()
    return bool(when) and when not in ("past", "in_the_past")


# ---------------------------
#   _messages
# ---------------------------
def _messages(data: dict[str, Any]) -> list[str]:
    """Return the messages smartctl printed."""
    return [str(m.get("string", "")) for m in data.get("smartctl", {}).get("messages", []) if isinstance(m, dict)]


# ---------------------------
#   summarize
# ---------------------------
def summarize(data: dict[str, Any], exit_code: int) -> dict[str, Any]:
    """Reduce the JSON of 'smartctl -j -a -l scttemp' to what the web UI shows."""
    messages = _messages(data)
    if exit_code & EXIT_OPEN_FAILED and "smart_status" not in data:
        if any("STANDBY" in m.upper() or "SLEEP" in m.upper() for m in messages):
            return {"status": STATUS_STANDBY}
        return {"status": STATUS_ERROR, "messages": messages}

    summary: dict[str, Any] = {
        "model": data.get("model_name") or data.get("scsi_model_name") or "",
        "serial": data.get("serial_number", ""),
        "firmware": data.get("firmware_version", ""),
        "capacity_bytes": data.get("user_capacity", {}).get("bytes"),
        "passed": data.get("smart_status", {}).get("passed"),
        "temperature": data.get("temperature", {}).get("current"),
        "power_on_hours": data.get("power_on_time", {}).get("hours"),
        "power_cycles": data.get("power_cycle_count"),
        "warnings": [],
        "attributes": [],
        "failed_past": [],
    }
    # What the drive is: from the drive database of smartctl, the drive itself and drivedb.py.
    rpm = data.get("rotation_rate")
    sct = data.get("ata_sct_status", {}).get("temperature", {})
    # The limits the drive is made for are in the SCT temperature history;
    # some drives also give them in the status.
    limits = data.get("ata_sct_temperature_history", {}).get("temperature", {})
    speed = data.get("interface_speed", {})
    summary.update(
        family=data.get("model_family", ""),
        rpm=rpm if isinstance(rpm, int) and rpm > 0 else None,
        form_factor=data.get("form_factor", {}).get("name", ""),
        protocol=data.get("device", {}).get("protocol", ""),
        link_max=speed.get("max", {}).get("string", ""),
        link_now=speed.get("current", {}).get("string", ""),
        temp_limit=limits.get("op_limit_max", sct.get("op_limit_max", data.get("temperature", {}).get("op_limit_max"))),
        temp_lifetime_max=sct.get("lifetime_max"),
        catalog=lookup(summary["model"], None if rpm is None else rpm > 0),
    )
    for attribute in data.get("ata_smart_attributes", {}).get("table", []):
        attr_id = attribute.get("id")
        raw = attribute.get("raw", {}).get("value")
        summary["attributes"].append(
            {
                "id": attr_id,
                "name": attribute.get("name", ""),
                "value": attribute.get("value"),
                "worst": attribute.get("worst"),
                "thresh": attribute.get("thresh"),
                "raw": attribute.get("raw", {}).get("string", raw),
                "failed": attribute.get("when_failed", ""),
                "failed_now": failed_now(attribute.get("when_failed")),
            }
        )
        if attr_id in WATCHED_ATTRIBUTES:
            summary[WATCHED_ATTRIBUTES[attr_id]] = raw
            if isinstance(raw, int) and raw > 0:
                summary["warnings"].append(WATCHED_ATTRIBUTES[attr_id])
        elif attr_id in COUNTERS:
            summary[COUNTERS[attr_id]] = raw
        if failed_now(attribute.get("when_failed")):
            summary["warnings"].append("attribute_failed")
        elif attribute.get("when_failed"):
            summary["failed_past"].append(attribute.get("name") or str(attr_id))

    nvme = data.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        for key in ("percentage_used", "available_spare", "media_errors", "critical_warning", "unsafe_shutdowns"):
            summary[key] = nvme.get(key)
        if (nvme.get("percentage_used") or 0) >= NVME_WEAR_WARNING:
            summary["warnings"].append("worn_out")
        if (nvme.get("media_errors") or 0) > 0:
            summary["warnings"].append("media_errors")
        if (nvme.get("critical_warning") or 0) != 0:
            summary["warnings"].append("critical_warning")

    summary["warnings"] = list(dict.fromkeys(summary["warnings"]))
    if summary["passed"] is False or exit_code & EXIT_SMART_FAILING:
        summary["status"] = STATUS_FAILING
    elif summary["warnings"]:
        summary["status"] = STATUS_WARNING
    else:
        summary["status"] = STATUS_OK
    return summary


# ---------------------------
#   read_smart
# ---------------------------
async def read_smart(device: str, smartctl: str = SMARTCTL_BIN, limit_s: float = SMARTCTL_TIMEOUT) -> dict[str, Any]:
    """Run smartctl on a drive and summarize what it says. It gets limit_s seconds."""
    try:
        proc = await asyncio.create_subprocess_exec(
            smartctl,
            "-j",
            "-a",
            # SCT status (the highest temperature it has seen) and history (the limits it is made for).
            "-l",
            "scttemp",
            "-n",
            "standby",
            device,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as err:
        return {"status": STATUS_ERROR, "messages": [str(err)]}
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), limit_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # A process stuck in the kernel dies only when its I/O returns: do not wait for it.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)
        return {"status": STATUS_ERROR, "messages": [f"smartctl did not answer within {limit_s:g} s"]}
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": STATUS_ERROR, "messages": ["smartctl gave no JSON"]}
    if not isinstance(data, dict):
        return {"status": STATUS_ERROR, "messages": ["smartctl gave no JSON"]}
    return summarize(data, proc.returncode or 0)
