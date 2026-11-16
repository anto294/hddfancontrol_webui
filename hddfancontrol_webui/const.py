"""Constants shared by the modules of the web UI."""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path


# ---------------------------
#   _version
# ---------------------------
def _version() -> str:
    """The version: APP_VERSION in the image, the package version on bare metal, else "dev"."""
    if os.environ.get("APP_VERSION"):
        return os.environ["APP_VERSION"]
    try:
        version = metadata.version("hddfancontrol-webui")
    except metadata.PackageNotFoundError:
        return "dev"
    return "dev" if version == "0.0.0" else version


VERSION = _version()

DEFAULT_CONFIG_DIR = Path("/config")
SETTINGS_FILE = "settings.json"
TEMP_LOG_DIR = "templog"
TEMP_LOG_FILE = "temps.jsonl"
TEMP_LOG_MAX_FILES = 7

DEFAULT_PORT = 8080

HDDFANCONTROL_BIN = "hddfancontrol"
HDIDLE_BIN = "hd-idle"
SENSORS_DETECT_BIN = "sensors-detect"
MODPROBE_BIN = "modprobe"
SMARTCTL_BIN = "smartctl"
# hddfancontrol needs a fan to drive. When the web UI drives the fans, it
# gets this command, which does nothing, and only measures.
FAN_CMD_NOOP = "/bin/true"
HISTORY_DB = "history.db"
# How often the recorder stores fan speeds and the newest temperatures.
RECORD_PERIOD = 30.0

# Names of the processes, as they appear in the logs and in the API.
SOURCE_HDDFANCONTROL = "hddfancontrol"
SOURCE_HDIDLE = "hd-idle"
SOURCE_PWM_TEST = "pwm-test"
SOURCE_SENSORS = "sensors"
SOURCE_FAN_CONTROL = "fan-control"
SOURCE_WEBUI = "webui"
SOURCE_TEMPS = "temps"

LOG_BUFFER_LINES = 2000

# A daemon that exits is started again after this many seconds, doubled after
# every quick exit up to the maximum. One that ran for RESTART_RESET_AFTER
# seconds starts again from the first delay.
RESTART_DELAY_MIN = 5.0
RESTART_DELAY_MAX = 60.0
RESTART_RESET_AFTER = 60.0

# hddfancontrol puts the fans back as they were (or at full speed) when it
# gets SIGTERM, so it is given time before SIGKILL.
STOP_TIMEOUT = 15.0

# The history endpoint never returns more points per series than this,
# whatever the window: older points are averaged together.
HISTORY_MAX_POINTS = 600
HISTORY_MAX_HOURS = 24 * 365
