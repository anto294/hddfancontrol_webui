"""Names that people give to fans and drives ("Front fan", "Parity drive").

They live in /config/labels.json, apart from the settings, because they only
change what the web UI shows: renaming a fan never restarts fan control.
A drive is named by its stable /dev/disk/by-id path, never by sdX, which can
change at the next boot; a fan by its PWM file.
"""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from pathlib import Path

from .settings import is_dev_path, is_sys_path
from .storage import write_atomic

_LOGGER = logging.getLogger(__name__)

LABELS_FILE = "labels.json"
MAX_LABEL_LENGTH = 40
MAX_LABELS = 500


# ---------------------------
#   LabelError
# ---------------------------
class LabelError(ValueError):
    """A label that cannot be stored; the code is translated by the web UI."""

    def __init__(self, code: str) -> None:
        """Keep the code."""
        self.code = code
        super().__init__(code)


# ---------------------------
#   Labels
# ---------------------------
class Labels:
    """Read and change the labels."""

    def __init__(self, config_dir: Path) -> None:
        """Read labels.json, or start empty."""
        self.path = config_dir / LABELS_FILE
        self._lock = threading.Lock()
        self._labels: dict[str, str] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.error("Cannot read %s, starting without labels: %s", self.path, err)
            return
        if isinstance(data, dict):
            self._labels = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def all(self) -> dict[str, str]:
        """Return every label."""
        with self._lock:
            return dict(self._labels)

    def move(self, old: str, new: str) -> None:
        """Give the label of old to new, when a fan's PWM file moved to another hwmon number."""
        with self._lock:
            if old not in self._labels or old == new:
                return
            labels = dict(self._labels)
            labels[new] = labels.pop(old)
            write_atomic(self.path, json.dumps(labels, indent=2, ensure_ascii=False) + "\n")
            self._labels = labels

    def set(self, key: str, label: str) -> dict[str, str]:
        """Set the label of a drive or a fan; an empty label removes it. Return every label."""
        if not (is_dev_path(key) or is_sys_path(key)):
            raise LabelError("invalid_label_key")
        label = " ".join(label.split())
        if len(label) > MAX_LABEL_LENGTH or any(unicodedata.category(c).startswith("C") for c in label):
            raise LabelError("invalid_label")
        with self._lock:
            labels = dict(self._labels)
            if label:
                labels[key] = label
            else:
                labels.pop(key, None)
            if len(labels) > MAX_LABELS:
                raise LabelError("too_many_labels")
            write_atomic(self.path, json.dumps(labels, indent=2, ensure_ascii=False) + "\n")
            self._labels = labels
            return dict(labels)
