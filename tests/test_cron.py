"""Tests of the cron expressions."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from hddfancontrol_webui import cron
from hddfancontrol_webui.cron import CronError, parse, windows


@pytest.fixture(autouse=True)
def rome(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run in a time zone with daylight saving time."""
    monkeypatch.setenv("TZ", "Europe/Rome")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(day: int, hour: int, minute: int = 0, month: int = 11, year: int = 2026) -> float:
    """A local timestamp."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def test_fields_lists_ranges_steps_and_names() -> None:
    """Every form of a field gives the values it should."""
    cron = parse("*/15 1-3,22 1,15 jan-mar,DEC mon-fri")
    assert cron.minutes == {0, 15, 30, 45}
    assert cron.hours == {1, 2, 3, 22}
    assert cron.days == {1, 15}
    assert cron.months == {1, 2, 3, 12}
    assert cron.weekdays == {1, 2, 3, 4, 5}
    assert parse("5/20 0-10/5 * * 7").minutes == {5, 25, 45}
    assert parse("5/20 0-10/5 * * 7").hours == {0, 5, 10}
    assert parse("0 0 * * 7").weekdays == {0}
    assert parse("  0   9 * *   1 ") == parse("0 9 * * mon")


def test_macros() -> None:
    """The @ macros are their five fields."""
    assert parse("@daily") == parse("0 0 * * *") == parse("@MIDNIGHT")
    assert parse("@weekly") == parse("0 0 * * 0")
    assert parse("@hourly").hours == set(range(24))
    assert parse("@monthly") == parse("0 0 1 * *")
    assert parse("@yearly") == parse("@annually") == parse("0 0 1 1 *")


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ("", "cron_fields"),
        ("* * * *", "cron_fields"),
        ("* * * * * *", "cron_fields"),
        ("0 " * 150, "cron_fields"),
        ("60 * * * *", "cron_range"),
        ("* 24 * * *", "cron_range"),
        ("* * 0 * *", "cron_range"),
        ("* * * 13 *", "cron_range"),
        ("* * * * 8", "cron_range"),
        ("x * * * *", "cron_value"),
        ("*/0 * * * *", "cron_value"),
        ("*/x * * * *", "cron_value"),
        ("5-1 * * * *", "cron_value"),
        ("1,,2 * * * *", "cron_value"),
        ("* * * * mon-xyz", "cron_value"),
        ("0 0 30 2 *", "cron_never"),
        ("0 0 31 4,6,9,11 *", "cron_never"),
    ],
)
def test_invalid(expression: str, code: str) -> None:
    """A wrong expression raises CronError with a code for the web UI."""
    with pytest.raises(CronError) as err:
        parse(expression)
    assert err.value.code == code


def test_range_error_names_the_field() -> None:
    """The parameters say which field and which limits."""
    with pytest.raises(CronError) as err:
        parse("0 25 * * *")
    assert err.value.params == {"field": "hour", "value": "25", "min": 0, "max": 23}


def test_next_and_previous() -> None:
    """The next run is strictly after, the previous one at or before."""
    cron = parse("0 3 * * 0")  # Sunday at 3
    now = at(17, 12)  # Tuesday
    assert cron.next(now) == at(22, 3)
    assert cron.previous(now) == at(15, 3)
    assert cron.previous(at(15, 3)) == at(15, 3)
    assert cron.next(at(15, 3)) == at(22, 3)
    assert parse("30 9 * * *").next(at(17, 9, 30)) == at(18, 9, 30)
    assert parse("0 0 29 2 *").next(now) == at(29, 0, month=2, year=2028)


def test_day_of_month_or_weekday() -> None:
    """Both restricted: either day matches; one restricted: only that one counts."""
    either = parse("0 0 1 * mon")
    assert either.next(at(17, 12)) == at(23, 0)  # the Monday before the 1st
    assert either.next(at(30, 12)) == at(1, 0, month=12)
    only_weekday = parse("0 0 * * mon")
    assert only_weekday.next(at(17, 12)) == at(23, 0)
    only_day = parse("0 0 1 * *")
    assert only_day.next(at(17, 12)) == at(1, 0, month=12)
    assert not parse("0 0 */2 * mon").either_day


def test_daylight_saving_time() -> None:
    """A minute skipped when the clocks go forward never matches; one repeated matches."""
    skipped = parse("30 2 * * *")
    # 29 March 2026: 02:00 → 03:00 in Rome.
    assert skipped.next(at(28, 12, month=3)) == at(30, 2, 30, month=3)
    assert not skipped.matches(at(29, 3, 30, month=3))
    assert parse("0 9 * * *").next(at(24, 12, month=10)) == at(25, 9, month=10)


def test_matches() -> None:
    """matches looks at the minute of a timestamp, in local time."""
    quiet = parse("* 22-23,0-6 * * *")
    assert quiet.matches(at(17, 23, 59))
    assert quiet.matches(at(17, 0))
    assert not quiet.matches(at(17, 7))
    assert parse("0 9 * * mon").matches(at(16, 9) + 30)
    assert not parse("0 9 * * mon").matches(at(17, 9))


def test_windows() -> None:
    """The stretches of the quiet hours, cut when they never end."""
    quiet = parse("* 22-23,0-6 * * *")
    assert windows(quiet, at(17, 12), 2) == [(at(17, 22), at(18, 7)), (at(18, 22), at(19, 7))]
    # Inside a stretch: it starts at the current minute.
    assert windows(quiet, at(17, 23, 10) + 20, 1) == [(at(17, 23, 10), at(18, 7))]
    always = windows(parse("* * * * *"), at(17, 12), 2, longest=3600)
    assert always == [(at(17, 12), at(17, 13)), (at(17, 13), at(17, 14))]


def test_nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the search limit, next and previous give None."""
    monkeypatch.setattr(cron, "SEARCH_DAYS", 10)
    leap = parse("0 0 29 2 *")
    assert leap.next(at(17, 12)) is None
    assert leap.previous(at(17, 12)) is None
    assert windows(leap, at(17, 12), 3) == []


@pytest.mark.parametrize(
    "expr", ["² * * * *", "*/² * * * *", "1-² * * * *", "٣ * * * *", "9" * 5000 + " * * * *", "*/" + "9" * 5000 + " * * * *"]
)
def test_only_ascii_digits(expr: str) -> None:
    """A number of a field is ASCII digits (a few): other digits that str.isdigit takes are a CronError, not a crash."""
    with pytest.raises(CronError):
        parse(expr)
