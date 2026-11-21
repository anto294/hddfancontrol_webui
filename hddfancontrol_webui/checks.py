"""What the host or the container keeps the web UI from doing, looked for once at start.

A container without the right devices, capabilities or volumes starts
anyway and fails later, one feature at a time: SMART says "permission
denied", a fan is never written, the settings cannot be saved. Here the
causes are looked for up front and each one is reported with what to do,
in the log and in a banner of the page. Nothing is changed here: the
fixes (privileged, a device, a volume) are the user's.

/sys mounted read only is not repeated here: it has its own mode and
banner (read_only). A drive that is gone is the drive_missing alert's.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .hardware import Hardware
from .settings import Settings

# Capabilities (linux/capability.h) and what needs them.
CAP_SYS_MODULE = 16
CAP_SYS_RAWIO = 17
CAP_SYS_ADMIN = 21
CAPABILITY_NAMES = {CAP_SYS_MODULE: "CAP_SYS_MODULE", CAP_SYS_RAWIO: "CAP_SYS_RAWIO", CAP_SYS_ADMIN: "CAP_SYS_ADMIN"}

# The English line of each problem in the log; the page translates the code (check.<code>).
MESSAGES = {
    "not_root": "the core runs as uid {uid}, not root: it cannot write the fans nor read SMART. Run it as root.",
    "config_not_writable": "{path} cannot be written ({error}): settings, names and history are not saved. "
    "Mount a writable volume there.",
    "no_drives": "/dev/disk/by-id lists no drive: the drives cannot be found. Give the container /dev "
    "(privileged: true, or a /dev:/dev volume).",
    "drive_not_readable": "{drive} cannot be opened ({error}): its temperature and SMART data cannot be read. "
    "Give the container the device (privileged: true).",
    "pwm_not_writable": "{path} cannot be written: this fan cannot be controlled. Run the container with privileged: true.",
    "missing_capabilities": "the core lacks {caps}: SMART (CAP_SYS_RAWIO, CAP_SYS_ADMIN for NVMe) or loading "
    "the sensor modules (CAP_SYS_MODULE) will fail. Run the container with privileged: true.",
    "program_missing": "{program} is not installed or not in PATH: {what} cannot run.",
}
# What each program does, for program_missing.
PROGRAM_USES = {
    "hddfancontrol": "the drive temperatures",
    "hd-idle": "the spin down of the drives",
    "smartctl": "SMART",
    "sensors-detect": "the sensor scan",
    "modprobe": "loading the sensor modules",
}

Problem = dict[str, Any]


# ---------------------------
#   message
# ---------------------------
def message(problem: Problem) -> str:
    """The English log line of a problem."""
    return MESSAGES[problem["code"]].format(**problem["params"])


# ---------------------------
#   _problem
# ---------------------------
def _problem(code: str, **params: Any) -> Problem:
    """A problem as the page reads it: a code and the parameters of its text."""
    return {"code": code, "params": {key: str(value) for key, value in params.items()}}


# ---------------------------
#   _config_writable
# ---------------------------
def _config_writable(config_dir: Path) -> Problem | None:
    """Write and remove a temporary file in /config, as the settings are written."""
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=config_dir, prefix=".check-")
        os.close(handle)
        os.unlink(name)
    except OSError as err:
        return _problem("config_not_writable", path=config_dir, error=err.strerror or err)
    return None


# ---------------------------
#   _drives
# ---------------------------
def _drives(hardware: Hardware, settings: Settings) -> list[Problem]:
    """No drive in /dev at all, or a drive of the settings that is here but cannot be opened."""
    if not hardware.drives():
        return [_problem("no_drives")]
    problems = []
    for drive in dict.fromkeys((*settings.drives, *settings.monitor_drives)):
        if not drive.startswith("/"):
            continue  # an interface name such as "ata"
        error = hardware.open_error(drive)
        if error is not None:
            problems.append(_problem("drive_not_readable", drive=drive, error=error))
    return problems


# ---------------------------
#   _capabilities
# ---------------------------
def _capabilities(hardware: Hardware, load_modules: bool) -> Problem | None:
    """The capabilities root needs here and lacks (a container that is not privileged drops them)."""
    effective = hardware.capabilities()
    if effective is None:
        return None
    needed = [CAP_SYS_RAWIO, CAP_SYS_ADMIN] + ([CAP_SYS_MODULE] if load_modules else [])
    lacking = [CAPABILITY_NAMES[cap] for cap in needed if not effective >> cap & 1]
    return _problem("missing_capabilities", caps=", ".join(lacking)) if lacking else None


# ---------------------------
#   startup_checks
# ---------------------------
def startup_checks(
    config_dir: Path,
    hardware: Hardware,
    settings: Settings,
    programs: Mapping[str, str],
    load_modules: bool,
    euid: int | None = None,
) -> list[Problem]:
    """Every problem found, as {code, params}. Blocking: files of /dev, /sys, /proc and /config.

    programs maps each program (hddfancontrol, hd-idle, smartctl, and
    sensors-detect and modprobe with load_modules) to its path or name in
    PATH. Without root, the capabilities and the fans are not looked at:
    not_root says it all.
    """
    euid = os.geteuid() if euid is None else euid
    problems: list[Problem] = []
    if euid != 0:
        problems.append(_problem("not_root", uid=euid))
    if (found := _config_writable(config_dir)) is not None:
        problems.append(found)
    problems += _drives(hardware, settings)
    if euid == 0:
        if (found := _capabilities(hardware, load_modules)) is not None:
            problems.append(found)
        if not hardware.read_only():
            problems += [
                _problem("pwm_not_writable", path=pwm.path) for pwm in settings.pwms if hardware.writable(pwm.path) is False
            ]
    for name, program in programs.items():
        if name in ("sensors-detect", "modprobe") and not load_modules:
            continue
        if shutil.which(program) is None:
            problems.append(_problem("program_missing", program=name, what=PROGRAM_USES.get(name, name)))
    return problems


# ---------------------------
#   run_checks
# ---------------------------
def run_checks(
    log: Callable[[str], None],
    config_dir: Path,
    hardware: Hardware,
    settings: Settings,
    programs: Mapping[str, str],
    load_modules: bool,
) -> list[Problem]:
    """startup_checks, each problem logged as a WARNING with log(text); a check that fails is logged, never raised. Blocking."""
    try:
        problems = startup_checks(config_dir, hardware, settings, programs, load_modules)
    except Exception as err:  # the checks only report: they must never stop the start
        log(f"WARNING: the startup checks failed: {err!r}")
        return []
    for problem in problems:
        log(f"WARNING: startup check: {message(problem)}")
    if not problems:
        log("Startup checks: permissions, devices and programs are fine")
    return problems
