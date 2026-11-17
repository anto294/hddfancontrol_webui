"""Cron expressions for the schedules of the web UI (SMART reads, self-tests, summary, quiet hours).

The five fields of crontab(5): minute, hour, day of month, month, day of
week. Each takes `*`, a number, a range `a-b`, a step `*/n`, `a-b/n` or
`a/n`, and lists of those joined by commas; months and days of the week
also take their English names (`jan`, `mon`), and Sunday is 0 or 7. The
macros `@hourly`, `@daily` (`@midnight`), `@weekly`, `@monthly` and
`@yearly` (`@annually`) work too. As in Vixie cron, when both the day of
the month and the day of the week are restricted (neither starts with
`*`), a day that matches either one matches.

Times are local (the TZ of the container). A minute that does not exist
on that day (the hour skipped when daylight saving time starts) never
matches.
"""

from __future__ import annotations

import datetime
import functools
import re
import time
from dataclasses import dataclass

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
# (low, high, names, names offset) of each field.
# A number of a field: ASCII digits only (str.isdigit also takes "²", which int() refuses), and few of them.
_NUMBER = re.compile(r"[0-9]{1,4}")
FIELDS = (
    (0, 59, (), 0),
    (0, 23, (), 0),
    (1, 31, (), 0),
    (1, 12, MONTHS, 1),
    (0, 7, DAYS, 0),
)
FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")
MAX_LENGTH = 200
# How far to look for the next or previous run: a 29 February that must
# also be a given weekday comes back within 28 years, but such an
# expression is useless here; 5 years covers every leap day.
SEARCH_DAYS = 5 * 366


# ---------------------------
#   CronError
# ---------------------------
class CronError(ValueError):
    """An expression that is not valid: code is error.<code> of the web UI, params its placeholders."""

    def __init__(self, code: str, **params: object) -> None:
        """Keep the code and its parameters."""
        super().__init__(code)
        self.code = code
        self.params = params


# ---------------------------
#   _value
# ---------------------------
def _value(text: str, index: int) -> int:
    """One number or name of a field."""
    low, high, names, offset = FIELDS[index]
    lowered = text.lower()
    if lowered in names:
        return names.index(lowered) + offset
    if not _NUMBER.fullmatch(text):
        raise CronError("cron_value", field=FIELD_NAMES[index], value=text)
    number = int(text)
    if not low <= number <= high:
        raise CronError("cron_range", field=FIELD_NAMES[index], value=text, min=low, max=high)
    return number


# ---------------------------
#   _field
# ---------------------------
def _field(text: str, index: int) -> frozenset[int]:
    """The values one field matches."""
    low, high, _names, _offset = FIELDS[index]
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not _NUMBER.fullmatch(step_text) or int(step_text) == 0:
                raise CronError("cron_value", field=FIELD_NAMES[index], value=part)
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _value(first, index), _value(last, index)
            if start > end:
                raise CronError("cron_value", field=FIELD_NAMES[index], value=part)
        else:
            start = _value(base, index)
            # "5/15" means from 5 to the end, every 15.
            end = high if slash else start
        values.update(range(start, end + 1, step))
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


# ---------------------------
#   Cron
# ---------------------------
@dataclass(frozen=True)
class Cron:
    """A parsed expression: the values each field matches."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    # Both the day of the month and the day of the week restricted: either one is enough.
    either_day: bool

    def matches_day(self, day: datetime.date) -> bool:
        """Whether some minute of this day may match."""
        if day.month not in self.months:
            return False
        by_month_day = day.day in self.days
        # date.weekday(): Monday is 0; cron: Sunday is 0.
        by_weekday = (day.weekday() + 1) % 7 in self.weekdays
        return by_month_day or by_weekday if self.either_day else by_month_day and by_weekday

    def matches(self, now: float) -> bool:
        """Whether the minute of this timestamp matches, in local time."""
        local = time.localtime(now)
        return (
            local.tm_min in self.minutes
            and local.tm_hour in self.hours
            and self.matches_day(datetime.date(local.tm_year, local.tm_mon, local.tm_mday))
        )

    def _runs_of(self, day: datetime.date, reverse: bool) -> list[float]:
        """The timestamps of the matching minutes of a day that matches, in order."""
        runs = []
        for hour in sorted(self.hours, reverse=reverse):
            for minute in sorted(self.minutes, reverse=reverse):
                stamp = time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))
                local = time.localtime(stamp)
                # A minute skipped by daylight saving time comes back as another one.
                if (local.tm_hour, local.tm_min, local.tm_mday) == (hour, minute, day.day):
                    runs.append(stamp)
        return runs

    def next(self, after: float) -> float | None:
        """The first matching minute strictly after this timestamp, or None within SEARCH_DAYS."""
        local = time.localtime(after)
        day = datetime.date(local.tm_year, local.tm_mon, local.tm_mday)
        for _ in range(SEARCH_DAYS):
            if self.matches_day(day):
                for stamp in self._runs_of(day, reverse=False):
                    if stamp > after:
                        return stamp
            day += datetime.timedelta(days=1)
        return None

    def previous(self, at: float) -> float | None:
        """The last matching minute at or before this timestamp, or None within SEARCH_DAYS."""
        local = time.localtime(at)
        day = datetime.date(local.tm_year, local.tm_mon, local.tm_mday)
        for _ in range(SEARCH_DAYS):
            if self.matches_day(day):
                for stamp in self._runs_of(day, reverse=True):
                    if stamp <= at:
                        return stamp
            day -= datetime.timedelta(days=1)
        return None


# ---------------------------
#   parse
# ---------------------------
@functools.lru_cache(maxsize=64)
def parse(expression: str) -> Cron:
    """Parse an expression; raises CronError. Cached: the loops parse the settings every minute."""
    text = " ".join(expression.split())
    if len(text) > MAX_LENGTH:
        raise CronError("cron_fields")
    text = MACROS.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise CronError("cron_fields")
    minutes, hours, days, months, weekdays = (_field(part, index) for index, part in enumerate(parts))
    either_day = not parts[2].startswith("*") and not parts[4].startswith("*")
    cron = Cron(minutes, hours, days, months, weekdays, either_day)
    if not any(cron.matches_day(datetime.date(2028, 1, 1) + datetime.timedelta(days=n)) for n in range(SEARCH_DAYS)):
        raise CronError("cron_never")
    return cron


# ---------------------------
#   windows
# ---------------------------
def windows(cron: Cron, after: float, count: int, longest: float = 8 * 86400) -> list[tuple[float, float]]:
    """The next count stretches of matching minutes, as (start, end) with end excluded.

    For the quiet hours, where the expression marks every minute of a
    stretch. A stretch going on at `after` starts at `after`'s minute; one
    longer than `longest` is cut there.
    """
    found: list[tuple[float, float]] = []
    minute = after - after % 60
    start: float | None = minute if cron.matches(minute) else cron.next(minute)
    while start is not None and len(found) < count:
        end = start + 60
        while end - start < longest and cron.matches(end):
            end += 60
        found.append((start, end))
        # A stretch cut at longest goes on at end: next() is strictly after end - 1.
        start = cron.next(end - 1)
    return found
