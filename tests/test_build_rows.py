from copilot_aic_report.build_rows import build_rollup, build_rows, index_consumption
from copilot_aic_report.config import Config
from copilot_aic_report.ledger import MaterializedSeat
from copilot_aic_report.models import AccountState, AicConsumption


def _mat(login, org, period="2026-07", **kw):
    base = dict(
        user_login=login,
        org_login=org,
        billing_period=period,
        license_assigned_date="2026-03-01",
        user_revoked_date="",
        user_status="active",
        seat_status="active",
        row_source="live_seats",
        login_recovery_source="seat",
        history_confidence="exact",
        as_of_utc="2026-07-08T00:00:00+00:00",
        plan_type="business",
    )
    base.update(kw)
    return MaterializedSeat(**base)


def _cfg(**kw):
    c = Config(enterprise_slug="acme", billing_period="2026-07")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_basic_row_columns():
    cfg = _cfg()
    rows = build_rows([_mat("mona_acme", "acme")], cfg)
    assert len(rows) == 1
    r = rows[0]
    assert r["user_login"] == "mona_acme"
    assert r["gh_copilot_license_cost"] == "19.00"
    assert r["default_aic_user_level"] == "1900"
    assert r["default_aic_usd"] == "19.00"
    # No per-user budget, no consumption data -> assigned = default usd, consumed 0
    assert r["aic_billing_dollar_assigned"] == "19.00"
    assert r["aic_assigned_rule_used"] == "plan_default"
    assert r["aic_consumed"] == "0"
    assert r["billing_period"] == "2026-07"
    assert r["row_source"] == "live_seats"


def test_per_user_budget_rule():
    cfg = _cfg(per_user_aic_budget_usd={"mona_acme": 50.0})
    rows = build_rows([_mat("mona_acme", "acme")], cfg)
    assert rows[0]["aic_billing_dollar_assigned"] == "50.00"
    assert rows[0]["aic_assigned_rule_used"] == "per_user_budget"


def test_consumption_lookup():
    cfg = _cfg()
    cons = [AicConsumption(user_login="mona_acme", org_login="acme", credits_consumed=500.0)]
    idx = index_consumption(cons)
    rows = build_rows([_mat("mona_acme", "acme")], cfg, consumption_index=idx)
    assert rows[0]["aic_consumed"] == "500"
    assert rows[0]["aic_consumed_usd"] == "5.00"


def test_consumption_org_agnostic_fallback():
    cfg = _cfg()
    cons = [AicConsumption(user_login="mona", org_login=None, credits_consumed=300.0)]
    idx = index_consumption(cons)
    rows = build_rows([_mat("mona", "acme")], cfg, consumption_index=idx)
    assert rows[0]["aic_consumed"] == "300"


def test_no_per_user_consumption_history():
    cfg = _cfg()
    rows = build_rows([_mat("u", "acme")], cfg, per_user_has_consumption=False)
    assert rows[0]["aic_consumed"] == ""
    assert rows[0]["history_confidence"] == "aggregate_only"
    assert "unavailable" in rows[0]["data_quality_notes"]


def test_deprovisioned_forces_inactive():
    cfg = _cfg()
    acct = {("acme", "mona"): AccountState("mona", "acme", is_member=False, scim_active=False)}
    rows = build_rows([_mat("mona", "acme")], cfg, account_states=acct)
    assert rows[0]["user_status"] == "inactive"
    assert rows[0]["account_state"] == "deprovisioned"


def test_deprovisioned_revoked_date_from_scim():
    # A SCIM-deprovisioned user (no seat cancellation) gets a revoke date derived
    # from the SCIM deprovisioning timestamp.
    cfg = _cfg()
    acct = {("acme", "mona"): AccountState(
        "mona", "acme", is_member=False, scim_active=False,
        deprovisioned_at="2026-05-14T09:30:00Z",
    )}
    rows = build_rows(
        [_mat("mona", "acme")], cfg,
        account_states=acct,
        deprovisioned_at_by_login={"mona": "2026-05-14T09:30:00Z"},
    )
    assert rows[0]["user_status"] == "inactive"
    assert rows[0]["user_revoked_date"] == "2026-05-14"
    assert "SCIM deprovisioning" in rows[0]["data_quality_notes"]


def test_revoked_date_recovered_for_obfuscated_via_resolved_login():
    # Deprovisioned EMU user: seat login is obfuscated, but the SCIM date is keyed by
    # the real login recovered from the numeric id.
    cfg = _cfg()
    seat = _mat("cafebabecafebabecafebabecafeba_LTIMPG", "acme",
                github_user_id=777, user_status="inactive")
    rows = build_rows(
        [seat], cfg,
        user_id_to_login={777: "mona_acme"},
        deprovisioned_at_by_login={"mona_acme": "2026-04-01T00:00:00Z"},
    )
    assert rows[0]["user_revoked_date"] == "2026-04-01"


def test_seat_revoked_date_not_overwritten_by_scim():
    cfg = _cfg()
    seat = _mat("mona", "acme", user_status="inactive", user_revoked_date="2026-06-15")
    rows = build_rows(
        [seat], cfg,
        deprovisioned_at_by_login={"mona": "2026-01-01T00:00:00Z"},
    )
    assert rows[0]["user_revoked_date"] == "2026-06-15"


def test_unrecoverable_login_row():
    cfg = _cfg()
    seat = _mat(None, "acme", login_recovery_source="UNRECOVERABLE", external_identity="ghost@acme.com")
    rows = build_rows([seat], cfg)
    assert rows[0]["user_login"] == ""
    assert rows[0]["external_identity"] == "ghost@acme.com"
    assert rows[0]["identity_resolution_source"] == "unresolved"
    assert "UNRECOVERABLE" in rows[0]["data_quality_notes"]


def test_dedup_same_user_org_period():
    cfg = _cfg()
    rows = build_rows([_mat("mona", "acme"), _mat("mona", "acme")], cfg)
    assert len(rows) == 1


def test_distinct_across_orgs():
    cfg = _cfg()
    rows = build_rows([_mat("mona", "acme"), _mat("mona", "globex")], cfg)
    assert len(rows) == 2


def test_never_emits_null_string():
    cfg = _cfg()
    rows = build_rows([_mat("u", "acme", last_activity_at=None)], cfg)
    assert rows[0]["last_activity_at"] == ""


def test_rollup_aggregation():
    cfg = _cfg()
    rows = build_rows(
        [
            _mat("mona", "acme"),
            _mat("mona", "globex", user_status="inactive", user_revoked_date="2026-06-01"),
        ],
        cfg,
    )
    roll = build_rollup(rows, cfg)
    assert len(roll) == 1
    r = roll[0]
    assert r["user_login"] == "mona"
    assert r["any_active"] == "yes"
    assert r["orgs"] == "acme,globex"
    assert r["latest_user_revoked_date"] == "2026-06-01"
    assert r["total_gh_copilot_license_cost"] == "38.00"


def test_github_user_id_and_resolved_login_columns():
    cfg = _cfg()
    # A deprovisioned user: obfuscated login preserved as-is, real id present, and the
    # real login recovered via the id->login index.
    seat = _mat("4eb6538565c3d97ad2917d606ccdc4_LTIMPG", "acme", github_user_id=555)
    rows = build_rows([seat], cfg, user_id_to_login={555: "mona_acme"})
    r = rows[0]
    assert r["user_login"] == "4eb6538565c3d97ad2917d606ccdc4_LTIMPG"  # kept as-is
    assert r["github_user_id"] == 555
    assert r["resolved_user_login"] == "mona_acme"
    assert "obfuscated login" in r["data_quality_notes"]


def test_obfuscated_login_note_when_unrecoverable():
    cfg = _cfg()
    seat = _mat("4eb6538565c3d97ad2917d606ccdc4_LTIMPG", "acme", github_user_id=999)
    rows = build_rows([seat], cfg)  # no id->login mapping
    r = rows[0]
    assert r["github_user_id"] == 999
    assert r["resolved_user_login"] == ""
    assert "real login unrecoverable" in r["data_quality_notes"]


def test_real_login_is_its_own_resolved_login():
    cfg = _cfg()
    rows = build_rows([_mat("mona_acme", "acme", github_user_id=1)], cfg)
    assert rows[0]["resolved_user_login"] == "mona_acme"
    assert rows[0]["github_user_id"] == 1
    assert "obfuscated" not in rows[0]["data_quality_notes"]


def test_rollup_carries_id_and_resolved_login():
    cfg = _cfg()
    rows = build_rows(
        [_mat("4eb6538565c3d97ad2917d606ccdc4_LTIMPG", "acme", github_user_id=555)],
        cfg,
        user_id_to_login={555: "mona_acme"},
    )
    roll = build_rollup(rows, cfg)
    assert roll[0]["github_user_id"] == 555
    assert roll[0]["resolved_user_login"] == "mona_acme"


def test_enterprise_plan_pricing():
    cfg = _cfg()
    rows = build_rows([_mat("u", "acme", plan_type="enterprise")], cfg)
    assert rows[0]["gh_copilot_license_cost"] == "39.00"
    assert rows[0]["default_aic_user_level"] == "3900"
