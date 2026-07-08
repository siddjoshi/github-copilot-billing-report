"""Report-period handling for historical (multi-month) reporting.

Parses ``REPORT_MONTHS`` into an ordered list of ``YYYY-MM`` periods and provides
billing-cycle overlap helpers used by the seat ledger. Also computes the earliest
recoverable month from snapshot / audit-archive / API-retention constraints.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, List, Optional, Tuple


def _parse_ym(value: str) -> Tuple[int, int]:
    year, month = value.strip().split("-")
    return int(year), int(month)


def month_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def add_months(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, (index % 12) + 1


def month_range(start: str, end: str) -> List[str]:
    """Inclusive list of ``YYYY-MM`` from ``start`` to ``end`` (order-normalized)."""
    sy, sm = _parse_ym(start)
    ey, em = _parse_ym(end)
    if (sy, sm) > (ey, em):
        sy, sm, ey, em = ey, em, sy, sm
    out: List[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(month_str(y, m))
        y, m = add_months(y, m, 1)
    return out


def last_n_months(n: int, now: Optional[_dt.datetime] = None) -> List[str]:
    """The most recent ``n`` months, ending with the current UTC month, ascending."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    end_y, end_m = now.year, now.month
    months = [add_months(end_y, end_m, -(n - 1 - i)) for i in range(n)]
    return [month_str(y, m) for y, m in months]


def parse_report_months(report_months, default_period: str, now: Optional[_dt.datetime] = None) -> List[str]:
    """Normalize the ``report_months`` config into an ordered ``YYYY-MM`` list.

    Accepts: None/"" (=> [default_period]); a list of periods; a range string
    "YYYY-MM..YYYY-MM"; or "last_N_months".
    """
    if report_months is None or report_months == "":
        return [default_period]
    if isinstance(report_months, (list, tuple)):
        return _dedupe_sorted([str(x).strip() for x in report_months if str(x).strip()])
    text = str(report_months).strip()
    lowered = text.lower()
    if lowered.startswith("last_") and lowered.endswith("_months"):
        n = int(lowered[len("last_"):-len("_months")])
        return last_n_months(n, now=now)
    if ".." in text:
        start, end = text.split("..", 1)
        return month_range(start, end)
    return [text]


def _dedupe_sorted(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for v in sorted(values):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def cycle_bounds_utc(period: str) -> Tuple[_dt.datetime, _dt.datetime]:
    """Return [start, end) UTC datetimes for a monthly billing cycle ``YYYY-MM``."""
    y, m = _parse_ym(period)
    start = _dt.datetime(y, m, 1, tzinfo=_dt.timezone.utc)
    ny, nm = add_months(y, m, 1)
    end = _dt.datetime(ny, nm, 1, tzinfo=_dt.timezone.utc)
    return start, end


def interval_overlaps_period(
    assigned_at: Optional[_dt.datetime],
    revoked_at: Optional[_dt.datetime],
    period: str,
) -> bool:
    """Does the interval [assigned_at, revoked_at) overlap the month's cycle?

    ``assigned_at`` None => treated as open-ended in the past (-inf).
    ``revoked_at`` None => still open (interval extends to +inf).
    """
    start, end = cycle_bounds_utc(period)
    a = assigned_at or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
    b = revoked_at or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc)
    return a < end and b > start


def earliest_recoverable_month(
    snapshot_months: Iterable[str],
    audit_archive_start: Optional[str],
    api_retention_days: int,
    now: Optional[_dt.datetime] = None,
) -> str:
    """Earliest ``YYYY-MM`` for which per-user data is reliably recoverable.

    = min(earliest snapshot, audit archive start, today - retention window month).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    api_cutoff = now - _dt.timedelta(days=api_retention_days)
    candidates: List[str] = [month_str(api_cutoff.year, api_cutoff.month)]
    snaps = [s for s in snapshot_months if s]
    if snaps:
        candidates.append(min(snaps))
    if audit_archive_start:
        candidates.append(audit_archive_start)
    return min(candidates)
