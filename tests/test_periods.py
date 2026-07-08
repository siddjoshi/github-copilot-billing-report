import datetime as dt

from copilot_aic_report import periods


def test_month_range_ascending():
    assert periods.month_range("2026-01", "2026-03") == ["2026-01", "2026-02", "2026-03"]


def test_month_range_reversed_input_normalized():
    assert periods.month_range("2026-03", "2026-01") == ["2026-01", "2026-02", "2026-03"]


def test_month_range_year_boundary():
    assert periods.month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_add_months():
    assert periods.add_months(2026, 12, 1) == (2027, 1)
    assert periods.add_months(2026, 1, -1) == (2025, 12)


def test_last_n_months():
    now = dt.datetime(2026, 3, 15, tzinfo=dt.timezone.utc)
    assert periods.last_n_months(3, now=now) == ["2026-01", "2026-02", "2026-03"]


def test_parse_report_months_default():
    assert periods.parse_report_months(None, "2026-07") == ["2026-07"]
    assert periods.parse_report_months("", "2026-07") == ["2026-07"]


def test_parse_report_months_list():
    assert periods.parse_report_months(["2026-02", "2026-01"], "x") == ["2026-01", "2026-02"]


def test_parse_report_months_range():
    assert periods.parse_report_months("2026-01..2026-03", "x") == ["2026-01", "2026-02", "2026-03"]


def test_parse_report_months_last_n():
    now = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    assert periods.parse_report_months("last_2_months", "x", now=now) == ["2026-02", "2026-03"]


def test_cycle_bounds():
    start, end = periods.cycle_bounds_utc("2026-02")
    assert start == dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)


def test_interval_overlaps_period():
    a = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
    b = dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc)
    assert periods.interval_overlaps_period(a, b, "2026-01")
    assert periods.interval_overlaps_period(a, b, "2026-02")
    assert not periods.interval_overlaps_period(a, b, "2026-03")


def test_interval_open_ended():
    a = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
    assert periods.interval_overlaps_period(a, None, "2026-12")
    # Open start (None) overlaps any period at/after the interval end
    b = dt.datetime(2026, 2, 10, tzinfo=dt.timezone.utc)
    assert periods.interval_overlaps_period(None, b, "2026-01")


def test_earliest_recoverable_month():
    now = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    # API cutoff ~ 2026-01 (180 days). Snapshot older wins.
    assert periods.earliest_recoverable_month(["2025-03"], None, 180, now=now) == "2025-03"
    assert periods.earliest_recoverable_month([], "2024-01", 180, now=now) == "2024-01"
    # No snapshot/archive -> api cutoff month
    got = periods.earliest_recoverable_month([], None, 180, now=now)
    assert got == "2026-01"
