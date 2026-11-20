"""Settings: checks, environment variables of the upstream image, command lines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hddfancontrol_webui.settings import (
    NOTIFY_MASK,
    SOURCE_ENV,
    SOURCE_FILE,
    Hwmon,
    Pwm,
    Settings,
    SettingsError,
    SettingsStore,
    env_document,
    from_dict,
    hddfancontrol_args,
    hdidle_args,
    mask_notify_url,
    restore_notify_url,
)

DRIVE = "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
DRIVE2 = "/dev/disk/by-id/ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1"
PWM = "/sys/class/hwmon/hwmon3/pwm1"
TEMP_A = "/sys/class/hwmon/hwmon1/temp1_input"
TEMP_B = "/sys/devices/platform/coretemp.0/hwmon/hwmon2/temp2_input"

# A docker-compose.yml environment that sets every variable.
UPSTREAM_ENV = {
    "DRIVES": f"{DRIVE} {DRIVE2}",
    "PWM": f"{PWM}:200:75 /sys/class/hwmon/hwmon3/pwm4:200:75",
    "DRIVE_TEMP_RANGE": "40 60",
    "MIN_FAN_SPEED_PRCT": "0",
    "INTERVAL": "10s",
    "HWMONS": "/sys/devices/platform/coretemp.0/hwmon/hwmon2/temp1_input:60:85",
    "RESTORE_FAN_SETTINGS": "true",
    "VERBOSITY": "TRACE",
    "SPIN_DOWN_TIME_S": "600",
}


def problems(data: object) -> dict[str, str]:
    """Return {field: code} of the problems of a document."""
    with pytest.raises(SettingsError) as info:
        from_dict(data)
    return {p["field"]: p["code"] for p in info.value.problems}


def test_defaults_are_those_of_hddfancontrol() -> None:
    """An empty document gives the defaults, which cannot run yet."""
    settings = from_dict({})
    assert settings == Settings()
    assert settings.drive_temp_min == 30 and settings.drive_temp_max == 50
    assert settings.min_fan_speed_prct == 20
    assert settings.restore_fan_settings is True
    assert "--restore-fan-settings" in hddfancontrol_args(settings, None)
    assert "--restore-fan-settings" not in hddfancontrol_args(from_dict({"restore_fan_settings": False}), None)
    assert not settings.complete


def test_upstream_environment() -> None:
    """The variables of the upstream compose file give the same daemon command line."""
    settings = from_dict(env_document(UPSTREAM_ENV))
    assert settings.drives == (DRIVE, DRIVE2)
    assert settings.pwms[0] == Pwm(PWM, 200, 75)
    assert settings.hwmons == (Hwmon("/sys/devices/platform/coretemp.0/hwmon/hwmon2/temp1_input", 60, 85),)
    assert settings.interval_s == 10
    assert settings.restore_fan_settings is True
    assert settings.complete
    args = hddfancontrol_args(settings, Path("/config/templog/temps.jsonl"))
    assert args[:4] == ["-v", "TRACE", "daemon", "--drives"]
    assert args[args.index("--pwm") + 1] == f"{PWM}:200:75"
    assert args[args.index("--drive-temp-range") + 1 : args.index("--drive-temp-range") + 3] == ["40", "60"]
    assert args[args.index("--interval") + 1] == "10s"
    assert args[args.index("--hwmons") + 1].endswith("temp1_input:60:85")
    assert "--restore-fan-settings" in args
    assert args[-2:] == ["--temp-log", "/config/templog/temps.jsonl"]


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("30", 30), ("30s", 30), ("3min", 180), ("2m", 120), ("1h", 3600), (" 5 sec ", 5)],
)
def test_interval_forms(value: str, seconds: int) -> None:
    """INTERVAL accepts the forms of humantime that people use."""
    assert from_dict(env_document({"INTERVAL": value})).interval_s == seconds


def test_invalid_environment_values() -> None:
    """Bad values name the field and the problem."""
    found = problems(
        env_document(
            {
                "INTERVAL": "soon",
                "PWM": f"{PWM} /sys/block/sda/device/delete:1:1",
                "DRIVE_TEMP_RANGE": "50",
                "VERBOSITY": "loud",
            }
        )
    )
    assert found["interval_s"] == "invalid_number"
    assert found["pwms.0.start"] == "required" and found["pwms.1.path"] == "invalid_pwm_path"
    assert found["drive_temp_max"] == "required"
    assert found["verbosity"] == "invalid_choice"


def test_restore_false_is_false() -> None:
    """Unlike the upstream script, RESTORE_FAN_SETTINGS=false does not turn it on."""
    assert from_dict(env_document({"RESTORE_FAN_SETTINGS": "false"})).restore_fan_settings is False
    assert problems(env_document({"RESTORE_FAN_SETTINGS": "maybe"})) == {"restore_fan_settings": "invalid_bool"}


@pytest.mark.parametrize(
    "drive",
    ["/etc/passwd", "/dev/../etc/shadow", "/dev/sda; rm -rf /", "/dev/sd a", "-rf", "", 5],
)
def test_drive_paths_are_checked(drive: object) -> None:
    """Only /dev paths and interface names reach hddfancontrol."""
    assert problems({"drives": [drive]}) == {"drives.0": "invalid_drive"}


def test_interface_selector_and_duplicates() -> None:
    """An interface name is accepted, a repeated drive is not."""
    assert from_dict({"drives": ["ata"]}).drives == ("ata",)
    assert problems({"drives": [DRIVE, DRIVE]}) == {"drives.1": "duplicate"}


def test_monitored_drives() -> None:
    """Monitored drives default to the /dev paths that drive the fans, and are checked."""
    assert from_dict({"drives": [DRIVE, "nvme"]}).monitor_drives == (DRIVE,)
    assert from_dict({"drives": [DRIVE], "monitor_drives": []}).monitor_drives == ()
    assert from_dict({"drives": [], "monitor_drives": [DRIVE2]}).monitor_drives == (DRIVE2,)
    assert problems({"monitor_drives": ["nvme", DRIVE, DRIVE]}) == {
        "monitor_drives.0": "invalid_dev_path",
        "monitor_drives.2": "duplicate",
    }
    assert from_dict(env_document({"DRIVES": DRIVE, "MONITOR_DRIVES": f"{DRIVE} {DRIVE2}"})).monitor_drives == (DRIVE, DRIVE2)
    assert from_dict(env_document({"DRIVES": DRIVE, "MONITOR_DRIVES": ""})).monitor_drives == ()


def test_pwm_checks() -> None:
    """PWM paths are pwmN files of /sys/class/hwmon (never another /sys file) and values in 0..255."""
    found = problems(
        {
            "pwms": [
                {"path": "/proc/self/mem", "start": 1, "stop": 1},
                {"path": PWM, "start": 300, "stop": -1},
                {"path": PWM, "start": 1, "stop": 1},
                {"path": "/sys/class/hwmon/hwmon3/pwm2", "start": True, "stop": "x"},
                "nope",
                {"path": "/sys/block/sda/device/delete", "start": 1, "stop": 1},
                {"path": "/sys/class/hwmon/hwmon3/pwm1_enable", "start": 1, "stop": 1},
                {"path": "/sys/class/hwmon/hwmon٣/pwm1", "start": 1, "stop": 1},
            ]
        }
    )
    assert found == {
        "pwms.0.path": "invalid_pwm_path",
        "pwms.5.path": "invalid_pwm_path",
        "pwms.6.path": "invalid_pwm_path",
        "pwms.7.path": "invalid_pwm_path",
        "pwms.1.start": "out_of_range",
        "pwms.1.stop": "out_of_range",
        "pwms.3.start": "invalid_number",
        "pwms.3.stop": "invalid_number",
        "pwms.4": "invalid_document",
    }


def test_duplicate_pwm() -> None:
    """The same PWM file cannot be listed twice."""
    pwm = {"path": PWM, "start": 200, "stop": 75}
    assert problems({"pwms": [pwm, pwm]}) == {"pwms.1.path": "duplicate"}


def test_temperature_order() -> None:
    """The maximum is above the minimum, and the target below the maximum."""
    assert problems({"drive_temp_min": 50, "drive_temp_max": 40, "target_temp": 35})["drive_temp_max"] == "range_order"
    assert problems({"target_temp": 55}) == {"target_temp": "target_order"}
    assert problems({"drive_temp_max": 500}) == {"drive_temp_max": "out_of_range"}
    assert problems({"drive_temp_max": [1]}) == {"drive_temp_max": "invalid_number"}


def test_hwmon_ranges() -> None:
    """A hwmon range needs both ends, in order."""
    found = problems(
        {
            "hwmons": [
                {"path": TEMP_A, "temp_min": 40},
                {"path": TEMP_B, "temp_min": 60, "temp_max": 50},
                {"path": "/tmp/c"},
                7,
                {"path": "/sys/block/sda/device/delete"},
            ]
        }
    )
    assert found == {
        "hwmons.0.temp_max": "range_both",
        "hwmons.1.temp_max": "range_order",
        "hwmons.2.path": "invalid_temp_path",
        "hwmons.3": "invalid_document",
        "hwmons.4.path": "invalid_temp_path",
    }
    assert from_dict({"hwmons": [{"path": TEMP_A}]}).hwmons == (Hwmon(TEMP_A),)


def test_lists_and_documents() -> None:
    """Wrong shapes are refused."""
    assert problems([]) == {"": "invalid_document"}
    assert problems({"drives": "ata"}) == {"drives": "invalid_list"}
    assert problems({"spin_down_drives": []}) == {"spin_down_drives": "invalid_document"}
    assert problems({"control_mode": "turbo"}) == {"control_mode": "invalid_choice"}


def test_spin_down_per_drive() -> None:
    """A drive's own spin down time wins; 0 keeps it spinning; unknown drives are refused."""
    settings = from_dict({"drives": [DRIVE, DRIVE2, "nvme"], "spin_down_drives": {DRIVE2: 0}, "spin_down_time_s": 900})
    assert settings.spin_down_of(DRIVE) == 900
    assert settings.spin_down_of(DRIVE2) == 0
    assert hdidle_args(settings) == ["-i", "0", "-a", DRIVE, "-i", "900", "-c", "ata"]
    assert problems({"drives": [DRIVE], "spin_down_drives": {DRIVE2: 10}}) == {f"spin_down_drives.{DRIVE2}": "unknown_drive"}
    assert problems({"drives": [DRIVE], "spin_down_drives": {DRIVE: -1}}) == {f"spin_down_drives.{DRIVE}": "out_of_range"}


def test_hdidle_off() -> None:
    """hd-idle does not run without a drive path or with every time at 0."""
    assert hdidle_args(from_dict({"drives": ["ata"]})) is None
    assert hdidle_args(from_dict({"drives": [DRIVE], "spin_down_time_s": 0})) is None
    debug = hdidle_args(from_dict({"drives": [DRIVE], "verbosity": "DEBUG", "spin_down_command": "scsi"}))
    assert debug == ["-i", "0", "-d", "-a", DRIVE, "-i", "600", "-c", "scsi"]


def test_measure_only_args() -> None:
    """In auto mode hddfancontrol gets a command that does nothing instead of the PWM files."""
    settings = from_dict({"drives": [DRIVE], "pwms": [{"path": PWM, "start": 200, "stop": 75}], "hwmons": [{"path": TEMP_A}]})
    args = hddfancontrol_args(settings, None, measure_only=True)
    assert "--pwm" not in args
    assert args[args.index("--fan-cmd") + 1] == "/bin/true"
    assert args[args.index("--hwmons") + 1] == TEMP_A
    assert "--temp-log" not in args


def test_new_environment_variables() -> None:
    """The variables added by this image."""
    settings = from_dict(
        env_document(
            {"CONTROL_MODE": "Auto", "TARGET_TEMP": "38", "HISTORY_DAYS": "90", "SMART_INTERVAL_H": "0", "AVERAGE": "3"}
        )
    )
    assert (
        settings.control_mode,
        settings.target_temp,
        settings.history_days,
        settings.smart_schedule,
        settings.average,
    ) == ("auto", 38, 90, "", 3)


def test_hwmon_env_without_range() -> None:
    """HWMONS entries may omit the range, or give only half of it."""
    doc = env_document({"HWMONS": f"{TEMP_A} {TEMP_B}:40"})
    assert doc["hwmons"] == [{"path": TEMP_A}, {"path": TEMP_B, "temp_min": "40", "temp_max": None}]


def test_round_trip() -> None:
    """to_dict gives a document that from_dict reads back to the same settings."""
    settings = from_dict(env_document(UPSTREAM_ENV))
    assert from_dict(json.loads(json.dumps(settings.to_dict()))) == settings


def test_store(tmp_path: Path) -> None:
    """The file wins over the environment; reset goes back; a bad file falls back."""
    store = SettingsStore(tmp_path, UPSTREAM_ENV)
    settings, source = store.load()
    assert source == SOURCE_ENV and settings.interval_s == 10

    store.save(from_dict({"interval_s": 42}))
    settings, source = store.load()
    assert source == SOURCE_FILE and settings.interval_s == 42
    assert not (tmp_path / "settings.tmp").exists()

    store.path.write_text("{broken")
    settings, source = store.load()
    assert source == SOURCE_ENV and store.error is not None
    assert store.error.problems[0]["code"] == "unreadable_file"

    store.path.write_text(json.dumps({"interval_s": 0}))
    settings, source = store.load()
    assert source == SOURCE_ENV and store.error is not None

    store.reset()
    assert not store.path.exists() and store.error is None


def test_store_with_invalid_environment(tmp_path: Path) -> None:
    """Invalid variables give the defaults and an error, never a crash."""
    store = SettingsStore(tmp_path, {"INTERVAL": "never"})
    settings, source = store.load()
    assert settings == Settings() and source == SOURCE_ENV
    assert store.error is not None


def test_pwm_chip() -> None:
    """The chip of a fan is optional, must be under /sys/devices, and survives a round trip."""
    chip = "/sys/devices/platform/nct6775.656"
    settings = from_dict({"pwms": [{"path": PWM, "start": 200, "stop": 75, "chip": chip}]})
    assert settings.pwms[0].chip == chip
    assert from_dict(json.loads(json.dumps(settings.to_dict()))) == settings
    assert from_dict({"pwms": [{"path": PWM, "start": 200, "stop": 75}]}).pwms[0].chip is None
    for chip in ("/etc/x", "/sys/block/sda"):
        assert problems({"pwms": [{"path": PWM, "start": 200, "stop": 75, "chip": chip}]}) == {"pwms.0.chip": "invalid_chip"}


def test_notifications() -> None:
    """The notify address is empty or http(s); format and language are from a fixed set; env names."""
    settings = from_dict({"notify_url": " https://ntfy.sh/x ", "notify_format": "gotify", "notify_language": "it"})
    assert (settings.notify_url, settings.notify_format, settings.notify_language) == ("https://ntfy.sh/x", "gotify", "it")
    assert from_dict({"notify_url": ""}).notify_url == ""
    for bad in ("ftp://x", "https://", "javascript:alert(1)", 5, "https://x/" + "a" * 600):
        with pytest.raises(SettingsError) as err:
            from_dict({"notify_url": bad})
        assert err.value.problems[0]["code"] == "invalid_url"
    # The NAS itself, link-local (the cloud's metadata service), unspecified and multicast addresses: never a target.
    for bad in (
        "http://127.0.0.1:8080/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "http://224.0.0.1/",
        "http://localhost:2586/x",
        "http://ntfy.localhost/x",
    ):
        with pytest.raises(SettingsError) as err:
            from_dict({"notify_url": bad})
        assert err.value.problems[0] == {"field": "notify_url", "code": "notify_address_forbidden", "params": {}}, bad
    # The LAN stays allowed: ntfy and Gotify often run there.
    for good in ("http://192.168.1.10:2586/alerts", "http://[fd00::5]/x", "http://10.0.0.2/", "https://ntfy.sh/x"):
        assert from_dict({"notify_url": good}).notify_url == good
    doc = env_document({"NOTIFY_URL": "http://gotify.lan/message?token=a", "NOTIFY_FORMAT": "Gotify", "NOTIFY_LANGUAGE": "IT"})
    assert doc == {"notify_url": "http://gotify.lan/message?token=a", "notify_format": "gotify", "notify_language": "it"}


def test_notify_url_is_masked() -> None:
    """The secrets of a notification address are hidden: user and password, query values, the fragment, an ntfy topic."""
    mask = NOTIFY_MASK
    assert mask_notify_url("https://ntfy.sh/my-secret-topic", "ntfy") == f"https://ntfy.sh/{mask}"
    assert (
        mask_notify_url("https://ntfy.example.com/ntfy/alerts?auth=tk_123&x", "ntfy")
        == f"https://ntfy.example.com/ntfy/{mask}?auth={mask}&{mask}"
    )
    assert (
        mask_notify_url("https://gotify.lan:8443/message?token=AbC", "gotify")
        == f"https://gotify.lan:8443/message?token={mask}"
    )
    assert mask_notify_url("http://anto:pw@hooks.lan/x#frag", "json") == f"http://{mask}@hooks.lan/x#{mask}"
    assert mask_notify_url("http://192.168.1.10:8080/hook", "json") == "http://192.168.1.10:8080/hook"
    assert mask_notify_url("https://ntfy.sh/", "ntfy") == "https://ntfy.sh/" and mask_notify_url("", "ntfy") == ""
    shown = Settings(notify_url="https://gotify.lan/message?token=AbC", notify_format="gotify").masked_dict()
    assert shown["notify_url"] == f"https://gotify.lan/message?token={mask}" and shown["notify_format"] == "gotify"


def test_a_masked_notify_url_sent_back_keeps_the_saved_one() -> None:
    """The masked value sent back keeps the address in force; another one replaces it, an empty one clears it.

    A value that still holds the mask but is not the mask of the address in
    force (edited by hand, or from another installation's backup) is
    refused: saving it would store a broken address.
    """
    current = Settings(notify_url="https://gotify.lan/message?token=AbC", notify_format="gotify")
    masked = mask_notify_url(current.notify_url, current.notify_format)
    # Also when the format changes in the same save: the mask the page got counts.
    kept = restore_notify_url({"notify_url": masked, "notify_format": "json"}, current)
    assert kept == {"notify_url": current.notify_url, "notify_format": "json"}
    assert restore_notify_url({"notify_url": " " + masked + " "}, current)["notify_url"] == current.notify_url
    assert restore_notify_url({"notify_url": "https://other.lan/x"}, current)["notify_url"] == "https://other.lan/x"
    assert restore_notify_url({"notify_url": ""}, current)["notify_url"] == ""
    assert restore_notify_url({"drives": []}, current) == {"drives": []}
    assert restore_notify_url([], current) == []
    with pytest.raises(SettingsError) as refused:
        restore_notify_url({"notify_url": f"https://evil.lan/message?token={NOTIFY_MASK}"}, current)
    assert refused.value.problems == [{"field": "notify_url", "code": "notify_url_masked", "params": {}}]


def test_fan_groups_and_drive_limits() -> None:
    """A fan follows some drives; a drive has its own limits, which fall back to the common ones and stay in order."""
    a, b = "/dev/disk/by-id/ata-A", "/dev/disk/by-id/ata-B"
    doc = {
        "drives": [a, b, "ata"],
        "pwms": [{"path": PWM, "start": 200, "stop": 75, "drives": [b, b]}],
        "drive_limits": {b: {"max": 45, "target": 38}, a: {}},
        "drive_temp_min": 30,
        "drive_temp_max": 50,
        "target_temp": 40,
    }
    settings = from_dict(doc)
    assert settings.pwms[0].drives == (b,)
    assert settings.drive_limits == {b: {"max": 45.0, "target": 38.0}}
    assert settings.limits_of(b) == (30.0, 38.0, 45.0)
    assert settings.limits_of(a) == (30.0, 40.0, 50.0)
    assert from_dict(settings.to_dict()) == settings
    bad = {
        **doc,
        "pwms": [{"path": PWM, "start": 200, "stop": 75, "drives": ["ata", "/dev/other", 3]}],
        "drive_limits": {b: {"max": 29}, a: {"target": 55}, "/dev/x": {"max": 40}, "ata": {}},
    }
    with pytest.raises(SettingsError) as err:
        from_dict(bad)
    fields = {(p["field"], p["code"]) for p in err.value.problems}
    assert ("pwms.0.drives.0", "unknown_drive") in fields
    assert ("pwms.0.drives.1", "unknown_drive") in fields
    assert ("pwms.0.drives.2", "unknown_drive") in fields
    assert (f"drive_limits.{b}.max", "range_order") in fields
    assert (f"drive_limits.{a}.target", "target_order") in fields
    assert ("drive_limits./dev/x", "unknown_drive") in fields
    assert ("drive_limits.ata", "unknown_drive") in fields
    for broken in ({"drive_limits": []}, {"drives": [a], "drive_limits": {a: 5}}):
        with pytest.raises(SettingsError):
            from_dict(broken)


def test_reminders_quiet_hours_summary_and_selftests() -> None:
    """The schedules are cron expressions, from the environment and checked."""
    env = {
        "NOTIFY_REPEAT_H": "6",
        "NOTIFY_QUIET": "*  22-23,0-6 * * *",
        "NOTIFY_SUMMARY": "@weekly",
        "SELFTEST_SHORT": "0 3 * * sun",
        "SELFTEST_LONG": "0 3 1 * *",
        "SMART_SCHEDULE": "0 */2 * * *",
        "SMART_INTERVAL_H": "12",
    }
    settings = from_dict(env_document(env))
    assert (settings.notify_repeat_h, settings.notify_quiet) == (6, "* 22-23,0-6 * * *")
    assert settings.notify_summary == "@weekly"
    assert (settings.selftest_short, settings.selftest_long) == ("0 3 * * sun", "0 3 1 * *")
    assert settings.smart_schedule == "0 */2 * * *"  # wins over the old SMART_INTERVAL_H
    assert from_dict({"notify_summary": None, "selftest_long": "  "}).notify_summary == ""
    with pytest.raises(SettingsError) as err:
        from_dict(env_document({"NOTIFY_QUIET": "22-7", "NOTIFY_REPEAT_H": "999"}))
    assert {p["field"]: p["code"] for p in err.value.problems} == {
        "notify_quiet": "cron_fields",
        "notify_repeat_h": "out_of_range",
    }
    with pytest.raises(SettingsError) as err:
        from_dict({"notify_summary": "0 25 * * *", "selftest_short": 3})
    assert [(p["field"], p["code"]) for p in err.value.problems] == [
        ("notify_summary", "cron_range"),
        ("selftest_short", "cron_fields"),
    ]


@pytest.mark.parametrize(
    ("hours", "schedule"),
    [
        (0, ""),
        (1, "0 * * * *"),
        (6, "0 */6 * * *"),
        ("12", "0 */12 * * *"),
        (24, "0 0 * * *"),
        (72, "0 0 */3 * *"),
        (168, "0 0 * * 0"),
    ],
)
def test_smart_interval_becomes_a_schedule(hours: int | str, schedule: str) -> None:
    """smart_interval_h of a settings file of 0.6, or SMART_INTERVAL_H, turns into smart_schedule."""
    assert from_dict({"smart_interval_h": hours}).smart_schedule == schedule
    assert from_dict(env_document({"SMART_INTERVAL_H": str(hours)})).smart_schedule == schedule


def test_smart_interval_not_a_number() -> None:
    """A wrong old interval is reported on smart_schedule."""
    with pytest.raises(SettingsError) as err:
        from_dict({"smart_interval_h": "often"})
    assert err.value.problems[0]["field"] == "smart_schedule"


@pytest.mark.parametrize(
    "doc",
    [
        {"drive_temp_max": 10**400},
        {"target_temp": 10**400},
        {"smart_interval_h": float("inf")},
        {"smart_interval_h": float("-inf")},
        {"smart_schedule": "² * * * *"},
        {"notify_quiet": "*/² * * * *"},
        {"env": {"INTERVAL": "9" * 5000}},
    ],
)
def test_odd_values_are_settings_errors(doc: dict[str, Any]) -> None:
    """Values that Python cannot turn into numbers are reported as problems, never raised as something else."""
    with pytest.raises(SettingsError):
        from_dict(env_document(doc["env"]) if "env" in doc else doc)


@pytest.mark.parametrize("content", [b'{"drives": ["\xe9"]}', b"[" * 100000, b"\xff\xfe"])
def test_a_broken_file_never_stops_the_start(tmp_path: Path, content: bytes) -> None:
    """A settings.json that is not UTF-8, or nested too deep, falls back to the environment with an error."""
    store = SettingsStore(tmp_path, {"INTERVAL": "7"})
    store.path.write_bytes(content)
    settings, source = store.load()
    assert source == SOURCE_ENV and settings.interval_s == 7 and store.error is not None


def test_an_unexpected_error_falls_back_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever goes wrong reading the file or the environment, the start goes on: environment, else defaults."""
    from hddfancontrol_webui import settings as settings_module

    store = SettingsStore(tmp_path, {})
    store.save(Settings())

    def broken(data: Any) -> Settings:
        raise RuntimeError("a bug")

    monkeypatch.setattr(settings_module, "from_dict", broken)
    settings, source = store.load()
    assert settings == Settings() and source == SOURCE_ENV and store.error is not None


def test_an_unexpected_error_in_the_environment_gives_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug reading the environment leaves the defaults, which control nothing, and says so."""
    from hddfancontrol_webui import settings as settings_module

    def broken(env: Any) -> dict[str, Any]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(settings_module, "env_document", broken)
    store = SettingsStore(Path("/nonexistent"), {})
    assert store.from_env() == Settings() and store.error is not None
    assert store.error.problems[0]["code"] == "unreadable_env"


@pytest.mark.parametrize(
    "path",
    [
        "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW",
        "/dev/disk/by-path/pci-0000:00:17.0-ata-1",
        "/dev/sda",
        "/dev/sdab",
        "/dev/nvme0n1",
    ],
)
def test_drive_paths_that_name_a_drive(path: str) -> None:
    """Stable names and kernel names of whole drives are drives, for control and for SMART."""
    settings = from_dict({"drives": [path], "monitor_drives": [path]})
    assert settings.drives == (path,) and settings.monitor_drives == (path,)


@pytest.mark.parametrize(
    "path",
    ["/dev/mem", "/dev/sda1", "/dev/nvme0n1p1", "/dev/disk/by-uuid/1234", "/dev/mapper/x", "/dev/sg0", "/dev/disk/by-id"],
)
def test_other_dev_nodes_are_not_drives(path: str) -> None:
    """Any other /dev node is refused: these paths go to smartctl, hdparm and hd-idle as root."""
    assert problems({"drives": [path], "monitor_drives": [path]}) == {
        "drives.0": "invalid_drive",
        "monitor_drives.0": "invalid_dev_path",
    }


def test_drive_limits_stay_within_what_drives_bear() -> None:
    """Drive temperatures stop at 80 °C, and the own limits of a SATA/SAS drive at 70; sensors keep their range."""
    nvme, sata = "/dev/disk/by-id/nvme-Samsung_980_S64", "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
    assert problems({"drive_temp_min": 30, "drive_temp_max": 81}) == {"drive_temp_max": "out_of_range"}
    assert from_dict({"drive_temp_max": 80}).drive_temp_max == 80
    doc = {"drives": [nvme, sata], "drive_limits": {nvme: {"max": 80}, sata: {"max": 71}}}
    assert problems(doc) == {f"drive_limits.{sata}.max": "out_of_range"}
    assert from_dict({**doc, "drive_limits": {nvme: {"max": 80}, sata: {"max": 70}}}).limits_of(sata)[2] == 70
    hwmon = {"hwmons": [{"path": TEMP_A, "temp_min": 60, "temp_max": 95}]}
    assert from_dict(hwmon).hwmons[0].temp_max == 95  # a CPU sensor, not a drive


PCI_TEMP = "/sys/devices/pci0000:00/0000:00:18.3/hwmon/hwmon2/temp1_input"


def test_hwmon_env_with_a_colon_in_the_path() -> None:
    """HWMONS takes the range from the end, only when it is numbers: the path may hold ':'."""
    doc = env_document({"HWMONS": f"{PCI_TEMP}:40:80 {PCI_TEMP} {TEMP_A}:45:75"})
    assert doc["hwmons"] == [
        {"path": PCI_TEMP, "temp_min": "40", "temp_max": "80"},
        {"path": PCI_TEMP},
        {"path": TEMP_A, "temp_min": "45", "temp_max": "75"},
    ]


def test_hwmon_path_with_a_colon_is_refused() -> None:
    """hddfancontrol splits --hwmons at the first ':' (splitn(3, ':') in its cl.rs): such a path cannot reach it."""
    with pytest.raises(SettingsError) as raised:
        from_dict({"hwmons": [{"path": PCI_TEMP, "temp_min": 40, "temp_max": 80}]})
    assert raised.value.problems == [{"field": "hwmons.0.path", "code": "temp_path_colon", "params": {}}]
