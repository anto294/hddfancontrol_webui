"""Every language has every key and placeholder, and every key used exists."""

from __future__ import annotations

import json
import re
from pathlib import Path

from hddfancontrol_webui.settings import NOTIFY_LANGUAGES

STATIC = Path(__file__).parent.parent / "hddfancontrol_webui" / "static"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load(lang: str) -> dict[str, str]:
    """Read a translation file."""
    data: dict[str, str] = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    return data


def test_languages_match() -> None:
    """Every language has the same keys and placeholders as English, and is offered everywhere."""
    en = load("en")
    languages = sorted(path.stem for path in (STATIC / "i18n").glob("*.json"))
    assert tuple(sorted(NOTIFY_LANGUAGES)) == tuple(languages)
    picker = (STATIC / "i18n.js").read_text()
    page = (STATIC / "index.html").read_text()
    for lang in languages:
        other = load(lang)
        assert set(other) == set(en), lang
        for key, text in en.items():
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(other[key])), (lang, key)
        assert f'"{lang}"' in picker.split("const LANGUAGES = ")[1].split("\n")[0], lang
        assert f'<option value="{lang}">' in page, lang


def test_keys_used_exist() -> None:
    """Every data-i18n attribute and every literal t("...") key is translated."""
    en = load("en")
    used: set[str] = set()
    for page in STATIC.glob("*.html"):
        used |= set(re.findall(r'data-i18n="([^"]+)"', page.read_text()))
    for script in STATIC.glob("*.js"):
        used |= set(re.findall(r'\bt\("([a-z_]+\.[a-z_.]+)"', script.read_text()))
    assert used - set(en) == set()


def test_codes_of_the_server_are_translated() -> None:
    """Error codes, modes, states and statuses that the server sends have a text."""
    en = load("en")
    source = "\n".join(p.read_text() for p in (STATIC.parent).glob("*.py"))
    codes = set(re.findall(r'(?:add\([^,]+, |_json_error\(\d+, |AuthError\(|CronError\()"([a-z_]+)"', source))
    assert codes, "no code found"
    assert {c for c in codes if f"error.{c}" not in en} == set()
    for prefix, values in {
        "mode": ["running", "waiting", "pwm_test", "stopped", "manual", "read_only", "curve", "auto"],
        "daemon": ["running", "stopped", "restarting", "failed"],
        "smart.status": ["ok", "warning", "failing", "standby", "error"],
        "pwmtest": ["pending", "running", "done", "failed", "cancelled"],
        "security": ["state_none", "state_environment", "state_file"],
        "settings": ["source_file", "source_environment", "mode_help_curve", "mode_help_auto"],
    }.items():
        assert {v for v in values if f"{prefix}.{v}" not in en} == set(), prefix
    warnings = re.findall(
        r'"([a-z_]+)"', (STATIC.parent / "smart.py").read_text().split("WATCHED_ATTRIBUTES = {")[1].split("}")[0]
    )
    warnings += ["attribute_failed", "worn_out", "media_errors", "critical_warning"]
    assert {w for w in warnings if f"smart.warn.{w}" not in en} == set()
