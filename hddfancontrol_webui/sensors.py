"""Find the kernel modules the sensor chips need, and load the missing ones.

sensors-detect --auto ends with a "Chip drivers" section of modprobe lines.
The upstream script ran each line with sh. Here only the module names are
taken from it, a module that is already loaded is left alone, and modprobe
runs without a shell for the others.

On TrueNAS SCALE, the main target, modprobe from inside the container often
fails: the module must then be loaded on the host, with an Init/Shutdown
Script. The result of every module is kept, and the web UI shows a warning
with those steps for each module that is still missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .const import MODPROBE_BIN, SENSORS_DETECT_BIN

_MODULE = re.compile(r"modprobe\s+([A-Za-z0-9_-]+)\s*")  # with fullmatch: the whole line

STATUS_ALREADY_LOADED = "already_loaded"
STATUS_LOADED = "loaded"
STATUS_FAILED = "failed"
# sensors-detect --auto takes about a minute; modprobe a moment.
RUN_TIMEOUT = 300.0

Log = Callable[[str], None]


# ---------------------------
#   ModuleResult
# ---------------------------
@dataclass
class ModuleResult:
    """What happened to one module."""

    module: str
    status: str
    error: str = ""


# ---------------------------
#   SensorReport
# ---------------------------
@dataclass
class SensorReport:
    """The result of the scan, for the web UI."""

    state: str = "not_run"
    detect_error: str = ""
    modules: list[ModuleResult] = field(default_factory=list)
    finished_at: str | None = None

    @property
    def failed(self) -> list[str]:
        """The modules that are needed and not loaded."""
        return [m.module for m in self.modules if m.status == STATUS_FAILED]

    def to_dict(self) -> dict[str, Any]:
        """Return the report as JSON-ready data."""
        data = asdict(self)
        data["failed"] = self.failed
        return data


# ---------------------------
#   chip_modules
# ---------------------------
def chip_modules(output: str) -> list[str]:
    """Return the modules listed between '# Chip drivers' and '#----cut here----'."""
    modules: list[str] = []
    inside = False
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("# Chip drivers"):
            inside = True
        elif line.startswith("#----cut here----"):
            if inside:
                break
        elif inside and (match := _MODULE.fullmatch(line)) and match.group(1) not in modules:
            modules.append(match.group(1))
    return modules


# ---------------------------
#   is_loaded
# ---------------------------
def is_loaded(module: str, sys_root: Path = Path("/sys")) -> bool:
    """Whether the kernel has the module, loaded or built in.

    /sys/module names use '_' where modprobe accepts '-' too.
    """
    return (sys_root / "module" / module.replace("-", "_")).is_dir()


# ---------------------------
#   _run
# ---------------------------
async def _run(*argv: str, limit_s: float = RUN_TIMEOUT) -> tuple[int, str]:
    """Run a program and return its exit code and output.

    A program that does not finish in time is killed and raises TimeoutError,
    an OSError like a program that cannot start.
    """
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), limit_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)
        raise TimeoutError(f"{argv[0]} did not finish within {limit_s:g} s") from None
    return proc.returncode or 0, output.decode("utf-8", errors="replace").strip()


# ---------------------------
#   _ensure
# ---------------------------
async def _ensure(module: str, log: Log, modprobe: str, sys_root: Path) -> ModuleResult:
    """Load a module unless the kernel has it already."""
    if is_loaded(module, sys_root):
        log(f"Module {module} is already loaded")
        return ModuleResult(module, STATUS_ALREADY_LOADED)
    try:
        code, output = await _run(modprobe, module)
    except OSError as err:
        code, output = -1, str(err)
    if code == 0:
        log(f"Loaded module {module}")
        return ModuleResult(module, STATUS_LOADED)
    error = output or f"exit code {code}"
    log(f"WARNING: cannot load module {module}: {error}. Load it on the host (see the web UI or the README).")
    return ModuleResult(module, STATUS_FAILED, error)


# ---------------------------
#   scan_sensor_modules
# ---------------------------
async def scan_sensor_modules(
    log: Log,
    sensors_detect: str = SENSORS_DETECT_BIN,
    modprobe: str = MODPROBE_BIN,
    sys_root: Path = Path("/sys"),
) -> SensorReport:
    """Find the chip drivers with sensors-detect and load those that are missing.

    i2c-dev is needed by sensors-detect to look at the SMBus. Nothing here
    stops the container: a module that cannot be loaded is only reported.
    """
    report = SensorReport(state="running")
    report.modules.append(await _ensure("i2c-dev", log, modprobe, sys_root))
    log("Running sensors-detect --auto, this can take a minute")
    try:
        code, output = await _run(sensors_detect, "--auto")
    except OSError as err:
        log(f"Cannot run sensors-detect: {err}")
        report.detect_error = str(err)
        report.state = "done"
        report.finished_at = datetime.now(UTC).isoformat()
        return report
    modules = chip_modules(output)
    if not modules:
        log(f"sensors-detect (exit code {code}) found no chip driver to load")
    for module in modules:
        report.modules.append(await _ensure(module, log, modprobe, sys_root))
    report.state = "done"
    report.finished_at = datetime.now(UTC).isoformat()
    return report
