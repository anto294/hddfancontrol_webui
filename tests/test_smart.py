"""Reducing the smartctl JSON output."""

from __future__ import annotations

import json

import pytest
from conftest import ScriptMaker

from hddfancontrol_webui.smart import read_smart, start_selftest, summarize, trend_values

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
        "assert sys.argv[1:7] == ['-j', '-a', '-l', 'scttemp', '-n', 'standby'], sys.argv\n" f"print({json.dumps(ATA)!r})",
    )
    good = await read_smart("/dev/sda", smartctl)
    assert good["model"] == "ST4000VN006-3CW104"
    for program in (make_script("bad", "print('nope')"), make_script("list", "print('[]')"), "/nonexistent/smartctl"):
        result = await read_smart("/dev/sda", program)
        assert result["status"] == "error"
    hung = await read_smart("/dev/sda", make_script("hung", "time.sleep(30)"), limit_s=0.2)
    assert hung["status"] == "error" and "did not answer" in hung["messages"][0]


def test_drive_identity() -> None:
    """Family, speed, form factor, link and the temperature limits the drive reports."""
    data = {
        **ATA,
        "model_family": "Seagate IronWolf",
        "rotation_rate": 5400,
        "form_factor": {"name": "3.5 inches"},
        "device": {"protocol": "ATA"},
        "interface_speed": {"max": {"string": "6.0 Gb/s"}, "current": {"string": "3.0 Gb/s"}},
        "ata_sct_status": {"temperature": {"op_limit_max": 70, "lifetime_max": 52}},
    }
    summary = summarize(data, 0)
    assert summary["family"] == "Seagate IronWolf" and summary["rpm"] == 5400
    assert (summary["link_max"], summary["link_now"], summary["protocol"]) == ("6.0 Gb/s", "3.0 Gb/s", "ATA")
    assert (summary["temp_limit"], summary["temp_lifetime_max"]) == (70, 52)
    history = {**data, "ata_sct_temperature_history": {"temperature": {"op_limit_max": 60}}}
    assert summarize(history, 0)["temp_limit"] == 60
    plain = {**ATA, "temperature": {"current": 30, "op_limit_max": 55}}
    assert summarize(plain, 0)["temp_limit"] == 55
    assert summary["catalog"] == {"brand": "Seagate", "line": "IronWolf", "use": "nas", "recording": "CMR"}
    ssd = summarize({**ATA, "rotation_rate": 0}, 0)
    assert ssd["rpm"] is None and ssd["catalog"]["use"] == "ssd"


def test_attribute_failed_in_the_past_is_not_a_warning() -> None:
    """An attribute under its threshold in the past only is listed, without a warning; now is a warning."""
    from hddfancontrol_webui.smart import failed_now

    data = json.loads(json.dumps(ATA))
    data["ata_smart_attributes"]["table"][1]["when_failed"] = "past"
    summary = summarize(data, 0)
    name = data["ata_smart_attributes"]["table"][1]["name"]
    assert "attribute_failed" not in summary["warnings"]
    assert summary["failed_past"] == [name]
    assert summary["attributes"][1]["failed_now"] is False
    data["ata_smart_attributes"]["table"][1]["when_failed"] = "now"
    summary = summarize(data, 0)
    assert "attribute_failed" in summary["warnings"] and summary["failed_past"] == []
    assert [failed_now(w) for w in ("", None, "past", "In_the_past", "now", "FAILING_NOW")] == [
        False,
        False,
        False,
        False,
        True,
        True,
    ]


def test_selftest_log() -> None:
    """The newest self-test of an ATA or NVMe drive; a failed one is a warning."""
    data = json.loads(json.dumps(ATA))
    data["ata_smart_self_test_log"] = {
        "standard": {
            "table": [
                {
                    "type": {"string": "Extended offline"},
                    "status": {"value": 7, "string": "Completed: read failure"},
                    "lifetime_hours": 21000,
                },
                {
                    "type": {"string": "Short offline"},
                    "status": {"value": 0, "string": "Completed without error", "passed": True},
                },
            ]
        }
    }
    data["ata_smart_data"] = {"self_test": {"status": {"value": 249, "remaining_percent": 90}}}
    summary = summarize(data, 0)
    assert summary["selftest"] == {
        "type": "Extended offline",
        "result": "Completed: read failure",
        "passed": False,
        "hours": 21000,
        "running": True,
    }
    assert "selftest_failed" in summary["warnings"]
    nvme = {
        "smart_status": {"passed": True},
        "nvme_self_test_log": {
            "current_self_test_operation": {"value": 0},
            "table": [
                {
                    "self_test_code": {"string": "Short"},
                    "self_test_result": {"value": 0, "string": "Completed without error"},
                    "power_on_hours": 5,
                }
            ],
        },
    }
    assert summarize(nvme, 0)["selftest"] == {
        "type": "Short",
        "result": "Completed without error",
        "passed": True,
        "hours": 5,
        "running": False,
    }
    nvme["nvme_self_test_log"]["table"][0]["self_test_result"]["value"] = 1
    assert summarize(nvme, 0)["selftest"]["passed"] is None  # aborted: says nothing
    nvme["nvme_self_test_log"]["table"][0]["self_test_result"]["value"] = 7
    assert summarize(nvme, 0)["selftest"]["passed"] is False
    assert "selftest" not in summarize({"smart_status": {"passed": True}}, 0)


async def test_start_selftest(make_script: ScriptMaker) -> None:
    """A self-test starts, a sleeping drive is left alone, a failure is reported."""
    ok = make_script("st-ok", "print('{}')")
    assert await start_selftest("/dev/sda", "short", ok) == {"status": "started"}
    asleep = make_script(
        "st-sleep",
        "import json; print(json.dumps({'smartctl': {'messages': [{'string': 'Device is in STANDBY mode'}]}})); sys.exit(2)",
    )
    assert await start_selftest("/dev/sda", "long", asleep) == {"status": "standby"}
    broken = make_script("st-bad", "print('nope'); sys.exit(4)")
    assert (await start_selftest("/dev/sda", "short", broken))["status"] == "error"
    assert (await start_selftest("/dev/sda", "short", "/nonexistent/smartctl"))["status"] == "error"
    hung = make_script("st-hung", "time.sleep(30)")
    assert (await start_selftest("/dev/sda", "short", hung, limit_s=0.2))["status"] == "error"
    with pytest.raises(ValueError):
        await start_selftest("/dev/sda", "conveyance", ok)


@pytest.mark.parametrize(
    ("value", "passed"), [(7, False), (0x79, False), (0x30, False), (0x10, None), (0xF9, None), (0, None)]
)
def test_ata_selftest_status_byte(value: int, passed: bool | None) -> None:
    """Without "passed", the result is read from the status byte (high nibble) or a bare result."""
    data = json.loads(json.dumps(ATA))
    data["ata_smart_self_test_log"] = {
        "standard": {"table": [{"type": {"string": "Short offline"}, "status": {"value": value}}]}
    }
    assert summarize(data, 0)["selftest"]["passed"] is passed


def test_trend_values() -> None:
    """Only the numbers of TREND_KEYS go to the trend: never a missing value, a flag or text."""
    summary = {
        "pending_sectors": 2,
        "power_on_hours": 12.5,
        "media_errors": None,
        "crc_errors": True,
        "model": "X",
        "available_spare": "9",
    }
    assert trend_values(summary) == {"pending_sectors": 2.0, "power_on_hours": 12.5}
