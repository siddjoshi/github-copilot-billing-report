import datetime as dt

from copilot_aic_report.ledger import SeatLedger
from copilot_aic_report.models import AuditEvent, Seat
from copilot_aic_report.resolve import IdentityResolver


def _ms(y, m, d):
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _seat(login, org, created, pending=None, plan="business", team=None, uid=1):
    return Seat(
        org_login=org,
        assignee_login=login,
        assignee_id=uid,
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
    # A scheduled cancellation is "cancelled" -> inactive, with the revoke date shown.
    assert r.user_status == "inactive"
    assert r.user_revoked_date == "2026-07-31"


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


def test_pending_cancellation_future_cycle_still_sets_revoked_date():
    # A pending cancellation scheduled beyond the current cycle end must still show
    # the user as inactive WITH the scheduled revoke date (previously left empty).
    led = SeatLedger()
    led.add_live_seat(_seat("mona", "acme", "2026-03-01T00:00:00Z", pending="2026-09-30T00:00:00Z"))
    rows = led.materialize_month("2026-07", "now")
    r = rows[0]
    assert r.seat_status == "pending_cancellation"
    assert r.user_status == "inactive"
    assert r.user_revoked_date == "2026-09-30"


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


_GUID = "2f1c8e4a-1234-4abc-9def-0123456789ab"


def test_suspended_guid_seat_resolved_and_inactive():
    # Suspended EMU account surfaces as a GUID login; resolver maps it to the real
    # login. Must be inactive, real login output, GUID kept in external_identity.
    resolver = IdentityResolver(identity_index={_GUID: "mona_acme"})
    led = SeatLedger(resolver=resolver)
    led.add_live_seat(_seat(_GUID + "_acme", "acme", "2026-03-01T00:00:00Z"))
    rows = led.materialize_month("2026-07", "now")
    r = rows[0]
    assert r.user_login == "mona_acme"          # real login, not the GUID
    assert r.suspended is True
    assert r.user_status == "inactive"
    assert r.external_identity == _GUID + "_acme"
    assert r.login_recovery_source == "externalIdentities"


def test_suspended_guid_unresolved_does_not_leak_guid():
    led = SeatLedger()  # no identity mapping
    led.add_live_seat(_seat(_GUID, "acme", "2026-03-01T00:00:00Z"))
    rows = led.materialize_month("2026-07", "now")
    r = rows[0]
    assert r.user_login is None                  # GUID never leaks into user_login
    assert r.external_identity == _GUID
    assert r.user_status == "inactive"
    assert r.login_recovery_source == "UNRECOVERABLE"


def test_suspended_guid_merges_audit_revoke():
    # After resolving the GUID to the real login, an audit seat_cancelled for that
    # real login merges into the same interval, producing a revoke date.
    resolver = IdentityResolver(identity_index={_GUID: "mona_acme"})
    led = SeatLedger(resolver=resolver)
    led.add_live_seat(_seat(_GUID, "acme", "2026-01-01T00:00:00Z"))
    led.add_audit_event(AuditEvent("copilot.seat_cancelled", "mona_acme", "acme", _ms(2026, 6, 15)))
    rows = led.materialize_month("2026-06", "now")
    r = rows[0]
    assert r.user_login == "mona_acme"
    assert r.user_revoked_date == "2026-06-15"


def test_live_seat_holders_uses_real_login():
    resolver = IdentityResolver(identity_index={_GUID: "mona_acme"})
    led = SeatLedger(resolver=resolver)
    led.add_live_seat(_seat(_GUID, "acme", "2026-03-01T00:00:00Z"))
    led.add_live_seat(_seat("octo", "globex", "2026-03-01T00:00:00Z"))
    holders = {(org, login) for org, login, _uid in led.live_seat_holders()}
    assert ("acme", "mona_acme") in holders
    assert ("globex", "octo") in holders


def test_enterprise_direct_seat_without_org_is_not_dropped():
    # Enterprise-direct seats may have an empty org_login; they must still produce a
    # holder/row rather than being silently discarded.
    led = SeatLedger()
    led.add_live_seat(_seat("Hemant_HondaCN", "", "2026-01-20T00:00:00Z"))
    rows = led.materialize_month("2026-06", "now")
    assert len(rows) == 1
    assert rows[0].user_login == "Hemant_HondaCN"
    assert rows[0].user_status == "active"


def test_seat_github_user_id_threaded_to_row():
    led = SeatLedger()
    led.add_live_seat(_seat("mona_acme", "acme", "2026-03-01T00:00:00Z", uid=98765))
    rows = led.materialize_month("2026-07", "now")
    assert rows[0].github_user_id == 98765


def test_obfuscated_login_kept_as_is_with_user_id():
    # A deprovisioned EMU seat carries an obfuscated hex login; it is preserved as-is
    # in user_login (not blanked) and the real numeric id is captured.
    led = SeatLedger()
    led.add_live_seat(_seat("4eb6538565c3d97ad2917d606ccdc4_LTIMPG", "acme", "2026-03-01T00:00:00Z", uid=555))
    rows = led.materialize_month("2026-07", "now")
    r = rows[0]
    assert r.user_login == "4eb6538565c3d97ad2917d606ccdc4_LTIMPG"
    assert r.github_user_id == 555
    # Still queryable for AIC (login is truthy) with the id available.
    assert ("acme", "4eb6538565c3d97ad2917d606ccdc4_LTIMPG", 555) in led.live_seat_holders()


def test_user_id_login_index_recovers_real_login_by_id():
    # One org has the user's real login (active); another surfaces the obfuscated
    # handle (deprovisioned) with the SAME numeric id. The index maps id -> real login.
    led = SeatLedger()
    led.add_live_seat(_seat("mona_acme", "acme", "2026-03-01T00:00:00Z", uid=777))
    led.add_live_seat(_seat("cafebabecafebabecafebabecafeba_acme", "globex", "2026-03-01T00:00:00Z", uid=777))
    index = led.user_id_login_index()
    assert index == {777: "mona_acme"}


def test_user_id_login_index_from_audit_history():
    led = SeatLedger()
    led.add_audit_event(AuditEvent("copilot.seat_assigned", "octocat", "acme", _ms(2026, 1, 1), user_id=321))
    index = led.user_id_login_index()
    assert index[321] == "octocat"
