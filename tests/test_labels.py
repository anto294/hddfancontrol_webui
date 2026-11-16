"""Names of drives and fans."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hddfancontrol_webui import labels as labels_module
from hddfancontrol_webui.labels import LabelError, Labels

DRIVE = "/dev/disk/by-id/ata-ST4000VN006-3CW104_ZW627CWW"
PWM = "/sys/class/hwmon/hwmon2/pwm1"


def test_set_and_remove(tmp_path: Path) -> None:
    """Labels are trimmed, saved, read back and removed by an empty name."""
    labels = Labels(tmp_path)
    assert labels.all() == {}
    labels.set(DRIVE, "  Disco   parità ")
    labels.set(PWM, "Ventola frontale")
    assert Labels(tmp_path).all() == {DRIVE: "Disco parità", PWM: "Ventola frontale"}
    assert labels.set(PWM, "") == {DRIVE: "Disco parità"}
    assert json.loads((tmp_path / "labels.json").read_text()) == {DRIVE: "Disco parità"}


@pytest.mark.parametrize(
    ("key", "label", "code"),
    [
        ("/etc/passwd", "x", "invalid_label_key"),
        ("sda", "x", "invalid_label_key"),
        (DRIVE, "x" * 41, "invalid_label"),
        (DRIVE, "bad\x00name", "invalid_label"),
    ],
)
def test_refused(tmp_path: Path, key: str, label: str, code: str) -> None:
    """Only /dev and /sys keys, short printable names."""
    with pytest.raises(LabelError, match=code):
        Labels(tmp_path).set(key, label)


def test_too_many(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The number of labels is bounded."""
    monkeypatch.setattr(labels_module, "MAX_LABELS", 1)
    labels = Labels(tmp_path)
    labels.set(DRIVE, "a")
    with pytest.raises(LabelError, match="too_many_labels"):
        labels.set(PWM, "b")


def test_broken_file(tmp_path: Path) -> None:
    """A broken or odd file gives no labels instead of an error."""
    (tmp_path / "labels.json").write_text("{nope")
    assert Labels(tmp_path).all() == {}
    (tmp_path / "labels.json").write_text(json.dumps({DRIVE: "ok", PWM: 5}))
    assert Labels(tmp_path).all() == {DRIVE: "ok"}
    (tmp_path / "labels.json").write_text("[]")
    assert Labels(tmp_path).all() == {}
