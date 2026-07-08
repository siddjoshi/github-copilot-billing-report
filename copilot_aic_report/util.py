"""Shared utilities: UTC time/date normalization, money formatting, CSV-safe values."""
from __future__ import annotations

import datetime as _dt
from typing import Optional


def to_utc_date(value: Optional[str]) -> str:
    """Normalize an ISO-8601 timestamp/date string to ``YYYY-MM-DD`` (UTC).

    Returns empty string for falsy input. Tolerates trailing ``Z`` and offsets.
    """
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(_dt.timezone.utc).date().isoformat()
    except ValueError:
        # Already a date-only or unparseable — take the first 10 chars if date-like.
        return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def epoch_ms_to_utc_date(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    try:
        dt = _dt.datetime.fromtimestamp(int(ms) / 1000.0, tz=_dt.timezone.utc)
        return dt.date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def to_utc_datetime(value: Optional[str]) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime, or None."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = _dt.datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def epoch_ms_to_utc_datetime(ms: Optional[int]) -> Optional[_dt.datetime]:
    if ms is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000.0, tz=_dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def now_utc_iso() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def fmt_money(amount: Optional[float], precision: int = 2) -> str:
    """Format a monetary/decimal value; empty string for None (never 'null')."""
    if amount is None:
        return ""
    return f"{float(amount):.{precision}f}"


def fmt_num(value: Optional[float]) -> str:
    """Format a numeric value with trailing-zero trimming; empty for None."""
    if value is None:
        return ""
    fval = float(value)
    if fval.is_integer():
        return str(int(fval))
    return f"{fval:g}"


def credits_to_usd(credits: Optional[float], rate: float) -> Optional[float]:
    if credits is None:
        return None
    return float(credits) * rate


def cell(value) -> str:
    """Coerce a value to a CSV cell string. None/'' -> '' (never the string 'null')."""
    if value is None:
        return ""
    if isinstance(value, str) and value.strip().lower() == "null":
        return ""
    return str(value)
