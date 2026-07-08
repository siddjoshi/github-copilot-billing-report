import datetime as dt

import pytest

from copilot_aic_report import util


def test_to_utc_date_with_z():
    assert util.to_utc_date("2026-03-15T12:34:56Z") == "2026-03-15"


def test_to_utc_date_with_offset_converts_to_utc():
    # 2026-03-15T23:30:00-05:00 == 2026-03-16 04:30 UTC
    assert util.to_utc_date("2026-03-15T23:30:00-05:00") == "2026-03-16"


def test_to_utc_date_naive_assumed_utc():
    assert util.to_utc_date("2026-03-15T00:00:00") == "2026-03-15"


def test_to_utc_date_date_only_passthrough():
    assert util.to_utc_date("2026-03-15") == "2026-03-15"


def test_to_utc_date_empty_and_none():
    assert util.to_utc_date("") == ""
    assert util.to_utc_date(None) == ""
    assert util.to_utc_date("   ") == ""


def test_to_utc_date_unparseable():
    assert util.to_utc_date("not-a-date") == ""


def test_epoch_ms_to_utc_date():
    ms = int(dt.datetime(2026, 3, 15, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert util.epoch_ms_to_utc_date(ms) == "2026-03-15"


def test_epoch_ms_none_and_bad():
    assert util.epoch_ms_to_utc_date(None) == ""
    assert util.epoch_ms_to_utc_date("bad") == ""


def test_now_utc_iso_has_offset():
    val = util.now_utc_iso()
    assert val.endswith("+00:00")


def test_fmt_money():
    assert util.fmt_money(19) == "19.00"
    assert util.fmt_money(None) == ""
    assert util.fmt_money(1.005, 2) in ("1.00", "1.01")  # rounding tolerated


def test_fmt_num_integer_and_float():
    assert util.fmt_num(1900.0) == "1900"
    assert util.fmt_num(12.5) == "12.5"
    assert util.fmt_num(None) == ""


def test_credits_to_usd():
    assert util.credits_to_usd(1900, 0.01) == pytest.approx(19.0)
    assert util.credits_to_usd(None, 0.01) is None


def test_cell_never_emits_null():
    assert util.cell(None) == ""
    assert util.cell("null") == ""
    assert util.cell("NULL") == ""
    assert util.cell("ok") == "ok"
    assert util.cell(5) == "5"
