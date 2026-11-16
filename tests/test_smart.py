"""Reducing the smartctl JSON output."""

from __future__ import annotations

import json

from conftest import ScriptMaker

from hddfancontrol_webui.smart import read_smart, summarize

ATA = {
    "model_name": "ST4000VN006-3CW104",
    "serial_number": "ZW627CWW",
    "firmware_version": "SC60",
    "user_capacity": {"bytes": 4000787030016},
    "smart_status": {"passed": True},
    "temperature": {"current": 38},
    "power_on_time": {"hours": 21034},
    "power_cycle_count": 61,
    "ata_smart_attributes": {
        "table": [
            {
                "id": 4,
                "name": "Start_Stop_Count",
                "value": 100,
                "worst": 100,
                "thresh": 20,
                "raw": {"value": 1234, "string": "1234"},
            },
            {
                "id": 5,
                "name": "Reallocated_Sector_Ct",
                "value": 100,
                "worst": 100,
                "thresh": 10,
                "raw": {"value": 0, "string": "0"},
            },
            {
                "id": 199,
                "name": "UDMA_CRC_Error_Count",
                "value": 200,
                "worst": 200,
                "thresh": 0,
                "raw": {"value": 3, "string": "3"},
            },
            {
                "id": 193,
                "name": "Load_Cycle_Count",
                "value": 99,
                "worst": 99,
                "thresh": 0,
                "raw": {"value": 4321, "string": "4321"},
            },
        ]
    },
}


def test_healthy_ata_with_crc_errors() -> None:
    """Counters are read, and CRC errors raise a warning."""
    summary = summarize(ATA, 0)
    assert summary["status"] == "warning"
    assert summary["warnings"] == ["crc_errors"]
    assert summary["reallocated_sectors"] == 0
    assert summary["start_stop_count"] == 1234
    assert summary["load_cycle_count"] == 4321
    assert summary["power_on_hours"] == 21034
    assert len(summary["attributes"]) == 4


def test_failing_drive() -> None:
    """A failed SMART status or a failed attribute means failing."""
    data = json.loads(json.dumps(ATA))
    data["smart_status"]["passed"] = False
    data["ata_smart_attributes"]["table"][1]["when_failed"] = "FAILING_NOW"
    summary = summarize(data, 8)
    assert summary["status"] == "failing"
    assert "attribute_failed" in summary["warnings"]


def test_nvme() -> None:
    """NVMe wear and media errors."""
    data = {
        "model_name": "Samsung 980",
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "percentage_used": 93,
            "media_errors": 2,
            "critical_warning": 0,
            "available_spare": 100,
        },
    }
    summary = summarize(data, 0)
    assert summary["warnings"] == ["worn_out", "media_errors"]
    assert summary["percentage_used"] == 93


def test_standby_and_errors() -> None:
    """A sleeping drive is reported as such; other failures keep their messages."""
    asleep = {"smartctl": {"messages": [{"string": "Device is in STANDBY mode, exit(2)", "severity": "information"}]}}
    assert summarize(asleep, 2) == {"status": "standby"}
    missing = {"smartctl": {"messages": [{"string": "Smartctl open device: /dev/x failed: No such device"}]}}
    assert summarize(missing, 2)["status"] == "error"


async def test_read_smart(make_script: ScriptMaker) -> None:
    """smartctl runs with -n standby and its JSON is summarized."""
    smartctl = make_script(
        "smartctl",
        "assert sys.argv[1:5] == ['-j', '-a', '-n', 'standby'], sys.argv\n" f"print({json.dumps(ATA)!r})",
    )
    assert (await read_smart("/dev/sda", smartctl))["model"] == "ST4000VN006-3CW104"
    assert (await read_smart("/dev/sda", make_script("bad", "print('nope')")))["status"] == "error"
    assert (await read_smart("/dev/sda", make_script("list", "print('[]')")))["status"] == "error"
    assert (await read_smart("/dev/sda", "/nonexistent/smartctl"))["status"] == "error"
