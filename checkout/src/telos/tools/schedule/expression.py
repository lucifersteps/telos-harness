"""Five-field schedule expression parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


@dataclass(frozen=True)
class ScheduleExpression:
    minutes: set[int]
    hours: set[int]
    days_of_month: set[int]
    months: set[int]
    days_of_week: set[int]
    day_of_month_wildcard: bool
    day_of_week_wildcard: bool

    def matches(self, value: datetime) -> bool:
        schedule_dow = (value.weekday() + 1) % 7
        if value.minute not in self.minutes:
            return False
        if value.hour not in self.hours:
            return False
        if value.month not in self.months:
            return False

        dom_matches = value.day in self.days_of_month
        dow_matches = schedule_dow in self.days_of_week
        if self.day_of_month_wildcard and self.day_of_week_wildcard:
            return True
        if self.day_of_month_wildcard:
            return dow_matches
        if self.day_of_week_wildcard:
            return dom_matches
        return dom_matches or dow_matches


def parse_schedule_expression(expression: str) -> ScheduleExpression:
    fields = str(expression or "").strip().split()
    if len(fields) != 5:
        raise ValueError("schedule expression must use five fields: minute hour day month weekday")
    minute, hour, day_of_month, month, day_of_week = fields
    return ScheduleExpression(
        minutes=_parse_field(minute, minimum=0, maximum=59),
        hours=_parse_field(hour, minimum=0, maximum=23),
        days_of_month=_parse_field(day_of_month, minimum=1, maximum=31),
        months=_parse_field(month, minimum=1, maximum=12, names=MONTH_NAMES),
        days_of_week=_parse_field(
            day_of_week,
            minimum=0,
            maximum=7,
            names=DOW_NAMES,
            normalize=lambda value: 0 if value == 7 else value,
        ),
        day_of_month_wildcard=day_of_month in {"*", "?"},
        day_of_week_wildcard=day_of_week in {"*", "?"},
    )


def _parse_field(
    value: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None = None,
    normalize=None,
) -> set[int]:
    text = str(value or "").strip().lower()
    if text == "?":
        text = "*"
    if not text:
        raise ValueError("schedule field is empty")
    items: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError("schedule field contains an empty list item")
        base, step = _split_step(part)
        start, end = _parse_range(base, minimum=minimum, maximum=maximum, names=names)
        for raw in range(start, end + 1, step):
            normalized = normalize(raw) if normalize is not None else raw
            if normalized < minimum or normalized > maximum:
                raise ValueError(f"schedule field value out of range: {raw}")
            items.add(normalized)
    if not items:
        raise ValueError("schedule field produced no values")
    return items


def _split_step(part: str) -> tuple[str, int]:
    if "/" not in part:
        return part, 1
    base, raw_step = part.split("/", 1)
    try:
        step = int(raw_step)
    except ValueError as error:
        raise ValueError("schedule step must be an integer") from error
    if step <= 0:
        raise ValueError("schedule step must be positive")
    return base or "*", step


def _parse_range(
    value: str,
    *,
    minimum: int,
    maximum: int,
    names: dict[str, int] | None,
) -> tuple[int, int]:
    if value == "*":
        return minimum, maximum
    if "-" not in value:
        number = _parse_value(value, names=names)
        _validate_range_value(number, minimum=minimum, maximum=maximum)
        return number, number
    raw_start, raw_end = value.split("-", 1)
    start = _parse_value(raw_start, names=names)
    end = _parse_value(raw_end, names=names)
    _validate_range_value(start, minimum=minimum, maximum=maximum)
    _validate_range_value(end, minimum=minimum, maximum=maximum)
    if start > end:
        raise ValueError("schedule ranges must not wrap")
    return start, end


def _parse_value(value: str, *, names: dict[str, int] | None) -> int:
    if names is not None and value in names:
        return names[value]
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"invalid schedule field value: {value}") from error


def _validate_range_value(value: int, *, minimum: int, maximum: int) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"schedule field value out of range: {value}")
