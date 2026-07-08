from copilot_aic_report import reconcile
from copilot_aic_report.models import BillingUsageLine, OrgBillingSummary


def _usage(net, product="copilot", sku="copilot_premium_requests", date="2026-07-01"):
    return BillingUsageLine(
        date=date,
        product=product,
        sku=sku,
        quantity=1,
        unit_type="request",
        gross_amount=net,
        discount_amount=0,
        net_amount=net,
        organization_name="acme",
    )


def _row(**kw):
    base = {
        "user_login": "mona",
        "org_login": "acme",
        "user_status": "active",
        "row_source": "live_seats",
        "aic_consumed_usd": "0",
        "billing_period": "2026-07",
        "login_recovery_source": "seat",
        "external_identity": "",
    }
    base.update(kw)
    return base


def test_check_real_logins_ok():
    res = reconcile.check_real_logins([_row(), _row(user_login="octo")])
    assert res["ok"]
    assert "100.0%" in res["detail"]


def test_check_real_logins_missing():
    res = reconcile.check_real_logins([_row(user_login="")])
    assert not res["ok"]


def test_check_no_external_in_login():
    ok = reconcile.check_no_external_in_login([_row(user_login="mona_acme")])
    assert ok["ok"]
    bad = reconcile.check_no_external_in_login([_row(user_login="mona@acme.com")])
    assert not bad["ok"]
    bad2 = reconcile.check_no_external_in_login([_row(user_login="First Last")])
    assert not bad2["ok"]


def test_check_seat_counts():
    rows = [_row(), _row(user_login="b")]
    billing = {"acme": OrgBillingSummary("acme", "business", total=2, active_this_cycle=2, inactive_this_cycle=0, pending_cancellation=0, pending_invitation=0)}
    checks = reconcile.check_seat_counts(rows, billing)
    assert checks[0]["ok"]


def test_check_seat_counts_mismatch():
    rows = [_row()]
    billing = {"acme": OrgBillingSummary("acme", "business", total=5, active_this_cycle=5, inactive_this_cycle=0, pending_cancellation=0, pending_invitation=0)}
    checks = reconcile.check_seat_counts(rows, billing)
    assert not checks[0]["ok"]


def test_aic_reconciliation_within_tolerance():
    rows = [_row(aic_consumed_usd="100.00")]
    usage = [_usage(102.0)]
    res = reconcile.check_aic_reconciliation(rows, usage, tolerance_frac=0.05)
    assert res["ok"]


def test_aic_reconciliation_out_of_tolerance():
    rows = [_row(aic_consumed_usd="100.00")]
    usage = [_usage(200.0)]
    res = reconcile.check_aic_reconciliation(rows, usage, tolerance_frac=0.05)
    assert not res["ok"]


def test_aic_reconciliation_zero_net():
    rows = [_row(aic_consumed_usd="0")]
    res = reconcile.check_aic_reconciliation(rows, [])
    assert res["ok"]


def test_aic_reconciliation_scopes_usage_by_period():
    # Per-user rows for June; billing lines for both June and July. The July net
    # must NOT be counted when reconciling June.
    rows = [_row(aic_consumed_usd="10.00", billing_period="2026-06")]
    usage = [_usage(5.0, date="2026-06-01"), _usage(500.0, date="2026-07-01")]
    res = reconcile.check_aic_reconciliation(rows, usage, period="2026-06")
    assert res["ok"]  # only June net (5) compared against June gross (10)
    assert "billing_net_usd=5.00" in res["detail"]


def test_aic_reconciliation_period_filters_out_other_months():
    # No June billing lines -> net 0 for June even though July has a large net.
    rows = [_row(aic_consumed_usd="0", billing_period="2026-06")]
    usage = [_usage(500.0, date="2026-07-01")]
    res = reconcile.check_aic_reconciliation(rows, usage, period="2026-06")
    assert res["ok"]
    assert "billing_net_usd=0.00" in res["detail"]


def test_summarize_history():
    rows = [
        _row(row_source="live_seats"),
        _row(row_source="audit_reconstructed", login_recovery_source="UNRECOVERABLE", user_login="", external_identity="x@e.com"),
    ]
    summary = reconcile.summarize_history(rows)
    assert summary["by_row_source"]["live_seats"] == 1
    assert summary["by_row_source"]["audit_reconstructed"] == 1
    assert len(summary["unrecoverable"]) == 1


def test_run_all_returns_checks():
    rows = [_row()]
    billing = {"acme": OrgBillingSummary("acme", "business", total=1, active_this_cycle=1, inactive_this_cycle=0, pending_cancellation=0, pending_invitation=0)}
    checks = reconcile.run_all(rows, billing, [], periods=["2026-07"])
    names = {c["name"] for c in checks}
    assert "real_login_coverage" in names
    assert "no_external_identity_in_login" in names
