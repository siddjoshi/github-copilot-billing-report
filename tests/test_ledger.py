import datetime as dt

from copilot_aic_report.ledger import SeatLedger
from copilot_aic_report.models import AuditEvent, Seat
from copilot_aic_report.resolve import IdentityResolver


def _ms(y, m, d):
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _seat(login, org, created, pending=None, plan="business", team=None):
    return Seat(
        org_login=org,
        assignee_login=login,
        assignee_id=1,
        assignee_type="User",
        created_at=created,
        pending_cancellation_date=pending,
        last_activity_at="2026-07-01T00:00:00Z",
        last_authenticated_at=None,
        last_activity_editor="vscode",
        assigning_team_slug=team,
        plan_type=plan,
    )


def test_live_seat_current_month_active():
    led = SeatLedger()
    led.add_live_seat(_seat("mona_acme", "acme", "2026-03-01T00:00:00Z"))
    rows = led.materialize_month("2026-07", "now")
    assert len(rows) == 1
    r = rows[0]
    assert r.user_login == "mona_acme"
    assert r.user_status == "active"
    assert r.seat_status == "active"
    assert r.row_source == "live_seats"
    assert r.history_confidence == "exact"
    assert r.login_recovery_source == "seat"
    assert r.assigned_via == "direct"


def test_live_seat_with_team():
    led = SeatLedger()
    led.add_live_seat(_seat("mona", "acme", "2026-03-01T00:00:00Z", team="dev"))
    rows = led.materialize_month("2026-07", "now")
    assert rows[0].assigned_via == "team:dev"


def test_pending_cancellation():
    led = SeatLedger()
    led.add_live_seat(_seat("mona", "acme", "2026-03-01T00:00:00Z", pending="2026-07-31T00:00:00Z"))
    rows = led.materialize_month("2026-07", "now")
    r = rows[0]
    assert r.seat_status == "pending_cancellation"
    assert r.user_status == "active"


def test_audit_reconstructed_revoked():
    led = SeatLedger()
    led.add_audit_event(AuditEvent("copilot.seat_assigned", "octocat", "acme", _ms(2026, 1, 15)))
    led.add_audit_event(AuditEvent("copilot.seat_cancelled", "octocat", "acme", _ms(2026, 2, 10)))
    # Active in Jan
    jan = led.materialize_month("2026-01", "now")
    assert jan[0].user_status == "active"
    assert jan[0].row_source == "audit_reconstructed"
    # Revoked in Feb
    feb = led.materialize_month("2026-02", "now")
    assert feb[0].user_status == "inactive"
    assert feb[0].seat_status == "removed"
    assert feb[0].user_revoked_date == "2026-02-10"
    # Gone in March
    assert led.materialize_month("2026-03", "now") == []


def test_reassignment_two_intervals():
    led = SeatLedger()
    led.add_audit_event(AuditEvent("copilot.seat_assigned", "u", "acme", _ms(2026, 1, 1)))
    led.add_audit_event(AuditEvent("copilot.seat_cancelled", "u", "acme", _ms(2026, 1, 20)))
    led.add_audit_event(AuditEvent("copilot.seat_assigned", "u", "acme", _ms(2026, 3, 1)))
    feb = led.materialize_month("2026-02", "now")
    assert feb == []  # gap month
    mar = led.materialize_month("2026-03", "now")
    assert mar[0].user_status == "active"
    assert mar[0].user_revoked_date == ""


def test_cancel_without_assign_predates_history():
    led = SeatLedger()
    led.add_audit_event(AuditEvent("copilot.seat_cancelled", "u", "acme", _ms(2026, 2, 10)))
    feb = led.materialize_month("2026-02", "now")
    assert feb[0].license_assigned_date == ""
    assert any("predates" in n for n in feb[0].notes)


def test_snapshot_month_is_authoritative():
    led = SeatLedger()
    led.add_snapshot("2026-02", [{"user_login": "snapuser", "org_login": "acme", "user_status": "active", "seat_status": "active"}])
    rows = led.materialize_month("2026-02", "now")
    assert rows[0].user_login == "snapuser"
    assert rows[0].row_source == "snapshot"
    assert rows[0].history_confidence == "exact"


def test_unrecoverable_via_external_only():
    # Audit event with no login is skipped; simulate holder with only external id
    led = SeatLedger()
    # Directly craft: no resolver mapping, add via a seat with no assignee login
    led.add_live_seat(_seat(None, "acme", "2026-03-01T00:00:00Z"))
    rows = led.materialize_month("2026-07", "now")
    assert rows[0].user_login is None
    assert rows[0].login_recovery_source == "UNRECOVERABLE"
