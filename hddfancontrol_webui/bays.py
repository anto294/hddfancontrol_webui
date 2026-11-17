"""Where each drive sits in the case: a grid of bays, so a drive to pull is found at once.

Kept in /config/bays.json, apart from the settings: like the names, it only
changes what the web UI shows. A bay holds a drive by its stable
/dev/disk/by-id path, or nothing.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .settings import is_dev_path
from .storage import write_atomic

_LOGGER = logging.getLogger(__name__)

BAYS_FILE = "bays.json"
MAX_SIDE = 12


# ---------------------------
#   BayError
# ---------------------------
class BayError(ValueError):
    """A bay map that cannot be stored; the code is translated by the web UI."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        self.code = code
        super().__init__(code)


# ---------------------------
#   _side
# ---------------------------
def _side(value: Any) -> int:
    """A number of rows or columns, from 0 to MAX_SIDE."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SIDE:
        raise BayError("out_of_range")
    return value


# ---------------------------
#   check_bays
# ---------------------------
def check_bays(data: Any) -> dict[str, Any]:
    """Check a bay map {rows, cols, slots}: slots has rows × cols entries, each a drive path or ""."""
    if not isinstance(data, dict):
        raise BayError("invalid_document")
    rows, cols, slots = _side(data.get("rows")), _side(data.get("cols")), data.get("slots", [])
    if not isinstance(slots, list) or len(slots) != rows * cols:
        raise BayError("invalid_document")
    if not all(isinstance(slot, str) and (not slot or is_dev_path(slot)) for slot in slots):
        raise BayError("invalid_dev_path")
    filled = [slot for slot in slots if slot]
    if len(set(filled)) != len(filled):
        raise BayError("duplicate")
    return {"rows": rows, "cols": cols, "slots": list(slots)}


# ---------------------------
#   Bays
# ---------------------------
class Bays:
    """Read and change the bay map."""

    def __init__(self, config_dir: Path) -> None:
        """Read bays.json, or start without a map."""
        self.path = config_dir / BAYS_FILE
        self._lock = threading.Lock()
        self._map: dict[str, Any] = {"rows": 0, "cols": 0, "slots": []}
        try:
            self._map = check_bays(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.error("Cannot read %s, starting without a bay map: %s", self.path, err)

    def get(self) -> dict[str, Any]:
        """The bay map."""
        with self._lock:
            return dict(self._map, slots=list(self._map["slots"]))

    def set(self, data: Any) -> dict[str, Any]:
        """Check and store a new bay map. Blocking."""
        checked = check_bays(data)
        with self._lock:
            write_atomic(self.path, json.dumps(checked, indent=2) + "\n")
            self._map = checked
            return dict(checked, slots=list(checked["slots"]))
