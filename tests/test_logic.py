from datetime import date

import pytest


def validate_date_range(from_date: date, to_date: date, max_days: int = 92):
    if to_date < from_date:
        raise ValueError("to_date must be on or after from_date")
    if (to_date - from_date).days + 1 > max_days:
        raise ValueError(f"Date range cannot exceed {max_days} days")


def test_valid_single_day_range():
    validate_date_range(date(2026, 10, 12), date(2026, 10, 12))


def test_valid_92_day_range():
    validate_date_range(date(2026, 1, 1), date(2026, 4, 2))


def test_rejects_reversed_dates():
    with pytest.raises(ValueError):
        validate_date_range(date(2026, 10, 13), date(2026, 10, 12))


def test_rejects_range_over_92_days():
    with pytest.raises(ValueError):
        validate_date_range(date(2026, 1, 1), date(2026, 4, 3))
