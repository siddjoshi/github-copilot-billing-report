import pytest

from copilot_aic_report import auth
from copilot_aic_report.auth import (
    AuthError,
    ScopeReport,
    evaluate_scopes,
    format_scope_error,
    parse_oauth_scopes,
    preflight,
    require_token,
)


def test_parse_oauth_scopes():
    assert parse_oauth_scopes("read:org, repo,  manage_billing:copilot") == [
        "read:org",
        "repo",
        "manage_billing:copilot",
    ]
    assert parse_oauth_scopes("") == []
    assert parse_oauth_scopes(None) == []


def test_evaluate_scopes_all_satisfied():
    report = evaluate_scopes(
        ["read:org", "read:audit_log", "admin:enterprise", "manage_billing:enterprise"],
        ["copilot_seats", "audit_log", "membership", "identity", "billing_usage"],
    )
    assert report.ok
    assert all(report.satisfied.values())


def test_evaluate_scopes_missing():
    report = evaluate_scopes(["read:org"], ["copilot_seats", "audit_log"])
    assert report.satisfied["copilot_seats"] is True
    assert report.satisfied["audit_log"] is False
    assert not report.ok
    assert "audit_log" in report.missing


def test_alternatives_any_one_suffices():
    # copilot_seats satisfied by manage_billing:copilot alone
    r = evaluate_scopes(["manage_billing:copilot"], ["copilot_seats"])
    assert r.ok


def test_format_scope_error_lists_missing():
    report = evaluate_scopes([], ["audit_log"])
    msg = format_scope_error(report)
    assert "audit_log" in msg
    assert "read:audit_log" in msg


def test_require_token():
    assert require_token("tok") == "tok"
    with pytest.raises(AuthError):
        require_token(None)
    with pytest.raises(AuthError):
        require_token("")


def test_preflight_success():
    report = preflight(
        fetch_scopes=lambda: "read:org, read:audit_log, admin:enterprise, manage_billing:enterprise",
        token="tok",
        required_capabilities=["copilot_seats", "audit_log", "identity", "billing_usage"],
    )
    assert report.ok


def test_preflight_missing_raises():
    with pytest.raises(AuthError):
        preflight(
            fetch_scopes=lambda: "read:org",
            token="tok",
            required_capabilities=["audit_log"],
        )


def test_preflight_allow_partial():
    report = preflight(
        fetch_scopes=lambda: "read:org",
        token="tok",
        required_capabilities=["audit_log"],
        allow_partial=True,
    )
    assert not report.ok


def test_preflight_no_token():
    with pytest.raises(AuthError):
        preflight(fetch_scopes=lambda: "", token=None, required_capabilities=[])


def test_scope_report_as_dict():
    report = evaluate_scopes(["read:org"], ["audit_log"])
    d = report.as_dict()
    assert "token_scopes" in d
    assert "missing" in d
