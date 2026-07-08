"""End-to-end test of the orchestrator using a fully faked HTTP session.

Exercises: scope preflight, org discovery (GraphQL), seats/org-billing/members/
identities/audit, row building, CSV + rollup + run-log + snapshot writing.
"""
import csv
import json

import pytest

from copilot_aic_report import __main__ as cli
from copilot_aic_report.config import Config


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class RoutingSession:
    """Routes requests by URL/graphql-body to canned responses."""

    SCOPES = "read:org, manage_billing:copilot, read:audit_log, admin:enterprise, manage_billing:enterprise"

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        # GraphQL
        if url.endswith("/graphql"):
            q = (json or {}).get("query", "")
            if "organizations" in q:
                return FakeResponse(200, {"data": {"enterprise": {"organizations": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"login": "acme"}],
                }}}})
            if "ownerInfo" in q:
                return FakeResponse(200, {"data": {"enterprise": {"ownerInfo": {"samlIdentityProvider": {
                    "externalIdentities": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"samlIdentity": {"nameId": "mona@acme.com"}, "scimIdentity": {"username": "mona.svc"}, "user": {"login": "mona_acme"}}],
                    }
                }}}}})
            if "organization(" in q:
                return FakeResponse(200, {"data": {"organization": {"samlIdentityProvider": None}}})
            return FakeResponse(200, {"data": {}})

        # REST — dispatch by path fragments
        if "/rate_limit" in url:
            return FakeResponse(200, {"resources": {}}, headers={"X-OAuth-Scopes": self.SCOPES})
        if "/ai_credit/usage" in url or "/premium_request/usage" in url:
            # Enterprise per-user AIC endpoint — no consumption by default.
            return FakeResponse(200, {"timePeriod": {"year": 2026, "month": 7}, "user": (params or {}).get("user"), "usageItems": []})
        if "/enterprises/" in url and "/copilot/billing/seats" in url:
            # Enterprise-wide seats endpoint (preferred path) with org attribution.
            return FakeResponse(200, {"total_seats": 1, "seats": [{
                "assignee": {"login": "mona_acme", "id": 5, "type": "User"},
                "organization": {"login": "acme"},
                "created_at": "2026-03-01T00:00:00Z",
                "pending_cancellation_date": None,
                "last_activity_at": "2026-07-01T00:00:00Z",
                "last_authenticated_at": "2026-07-01T00:00:00Z",
                "last_activity_editor": "vscode",
                "assigning_team": None,
                "plan_type": "business",
            }]}, headers={})
        if "/copilot/billing/seats" in url:
            return FakeResponse(200, {"total_seats": 1, "seats": [{
                "assignee": {"login": "mona_acme", "id": 5, "type": "User"},
                "created_at": "2026-03-01T00:00:00Z",
                "pending_cancellation_date": None,
                "last_activity_at": "2026-07-01T00:00:00Z",
                "last_authenticated_at": "2026-07-01T00:00:00Z",
                "last_activity_editor": "vscode",
                "assigning_team": None,
                "plan_type": "business",
            }]}, headers={})
        if url.endswith("/copilot/billing") or url.endswith("/copilot/billing/"):
            return FakeResponse(200, {"seat_breakdown": {"total": 1, "active_this_cycle": 1, "inactive_this_cycle": 0, "pending_cancellation": 0, "pending_invitation": 0}, "plan_type": "business"})
        if "/members" in url:
            return FakeResponse(200, [{"login": "mona_acme"}], headers={})
        if "/scim/v2/enterprises/" in url:
            return FakeResponse(200, {"Resources": [{"userName": "mona.svc", "active": True}], "totalResults": 1}, headers={})
        if "/settings/billing/usage" in url:
            return FakeResponse(200, {"usageItems": [
                {"date": "2026-07-01", "product": "copilot", "sku": "copilot_premium_requests", "quantity": 1900, "unitType": "credit", "grossAmount": 19.0, "discountAmount": 0.0, "netAmount": 19.0, "organizationName": "acme"},
            ]})
        if "/audit-log" in url:
            return FakeResponse(200, [], headers={})
        return FakeResponse(404, text=f"unrouted {url}")


def _make_cfg(tmp_path):
    return Config(
        enterprise_slug="acme",
        orgs="all",
        billing_period="2026-07",
        token="fake-token",
        output_path=str(tmp_path / "out.csv"),
        log_path=str(tmp_path / "run.log"),
        rollup_path=str(tmp_path / "rollup.csv"),
        emit_rollup=True,
        snapshot_store=str(tmp_path / "snaps"),
    )


def _patch_session(monkeypatch):
    import copilot_aic_report.github_client as gc
    orig_post_init = gc.GitHubClient.__post_init__

    def patched(self):
        if self.session is None:
            self.session = RoutingSession()
        self.sleep = lambda s: None

    monkeypatch.setattr(gc.GitHubClient, "__post_init__", patched)


def test_end_to_end(tmp_path, monkeypatch):
    _patch_session(monkeypatch)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)

    assert log.rows_written == 1
    assert log.seats_found == 1

    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    r = rows[0]
    assert r["user_login"] == "mona_acme"
    assert r["org_login"] == "acme"
    assert r["billing_period"] == "2026-07"
    assert r["user_status"] == "active"
    assert r["gh_copilot_license_cost"] == "19.00"
    assert r["default_aic_user_level"] == "1900"
    assert r["row_source"] == "live_seats"
    assert r["identity_resolution_source"] == "seat"

    # Rollup written
    with open(cfg.rollup_path, "r", encoding="utf-8", newline="") as fh:
        roll = list(csv.DictReader(fh))
    assert roll[0]["user_login"] == "mona_acme"

    # Run log + snapshot written
    assert (tmp_path / "run.log").exists()
    assert (tmp_path / "run.log.json").exists()
    snap = json.loads((tmp_path / "snaps" / "2026-07" / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["billing_period"] == "2026-07"
    assert snap["records"][0]["user_login"] == "mona_acme"

    # Reconciliation includes real-login coverage OK
    names = {c["name"]: c["ok"] for c in log.reconciliation}
    assert names["real_login_coverage"] is True
    assert names["no_external_identity_in_login"] is True


def test_missing_token_fails(tmp_path, monkeypatch):
    _patch_session(monkeypatch)
    cfg = _make_cfg(tmp_path)
    cfg.token = None
    from copilot_aic_report.auth import AuthError

    with pytest.raises(AuthError):
        cli.run(cfg)


def test_main_requires_enterprise(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda **kw: Config(enterprise_slug=""))
    rc = cli.main(["--output", "x.csv"])
    assert rc == 2


def test_historical_month_snapshot_reuse(tmp_path, monkeypatch):
    _patch_session(monkeypatch)
    from copilot_aic_report import snapshots
    snapshots.write_snapshot(
        str(tmp_path / "snaps"),
        "2026-05",
        [{"user_login": "ghost_acme", "org_login": "acme", "user_status": "inactive", "seat_status": "removed",
          "billing_period": "2026-05", "row_source": "snapshot", "login_recovery_source": "snapshot",
          "identity_resolution_source": "snapshot", "gh_copilot_license_cost": "19.00", "default_aic_user_level": "1900",
          "aic_billing_dollar_assigned": "19.00", "aic_consumed": "0", "user_revoked_date": "2026-05-10",
          "license_assigned_date": "2026-01-01", "plan_type": "business", "history_confidence": "exact",
          "as_of_utc": "2026-05-08T00:00:00+00:00", "currency": "USD", "external_identity": "",
          "data_quality_notes": "", "data_generated_at_utc": "2026-05-08T00:00:00+00:00"}],
    )
    cfg = _make_cfg(tmp_path)
    cfg.report_months = ["2026-05", "2026-07"]
    log = cli.run(cfg)
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    logins = {(r["user_login"], r["billing_period"]) for r in rows}
    assert ("ghost_acme", "2026-05") in logins  # from stored snapshot
    assert ("mona_acme", "2026-07") in logins  # live


def test_main_success_returns_zero(tmp_path, monkeypatch):
    _patch_session(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda **kw: cfg)
    rc = cli.main(["--enterprise", "acme"])
    assert rc == 0
    assert (tmp_path / "out.csv").exists()


def test_main_auth_error_returns_three(tmp_path, monkeypatch):
    _patch_session(monkeypatch)
    cfg = _make_cfg(tmp_path)
    cfg.token = None
    monkeypatch.setattr(cli, "load_config", lambda **kw: cfg)
    rc = cli.main(["--enterprise", "acme"])
    assert rc == 3


def test_run_degraded_optional_scopes(tmp_path, monkeypatch):
    _patch_session(monkeypatch)

    # Return a token lacking optional scopes (only required ones).
    def limited_scopes(self):
        return "read:org, manage_billing:copilot"

    import copilot_aic_report.github_client as gc
    monkeypatch.setattr(gc.GitHubClient, "get_oauth_scopes", limited_scopes)

    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)
    # Still produces rows from live seats even with optional sources skipped.
    assert log.rows_written == 1
    assert any("optional scope" in w for w in log.warnings)


class StandaloneEnterpriseSession(RoutingSession):
    """Simulates a token that cannot see the enterprise itself.

    The enterprise-wide Copilot seats endpoint 404s and GraphQL enterprise org
    discovery returns a null enterprise (partial "Not Found"), but the token can
    read some organizations' Copilot billing directly via ``/user/orgs``. This is
    the standalone-Copilot-enterprise / limited-PAT case: discovery must fall back
    so seats are still reported instead of writing an empty CSV.
    """

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/graphql"):
            q = (json or {}).get("query", "")
            if "organizations" in q or "ownerInfo" in q:
                return FakeResponse(200, {
                    "data": {"enterprise": None},
                    "errors": [{"type": "NOT_FOUND", "path": ["enterprise"],
                                "message": "Could not resolve to an Enterprise"}],
                })
            return FakeResponse(200, {"data": {}})
        if "/enterprises/" in url and "/copilot/billing/seats" in url:
            return FakeResponse(404, {"message": "Not Found"}, text="Not Found")
        if url.endswith("/user/orgs"):
            return FakeResponse(200, [{"login": "acme"}], headers={})
        return super().request(method, url, params, json, timeout=timeout, headers=headers)


def test_falls_back_to_user_orgs_when_enterprise_inaccessible(tmp_path, monkeypatch):
    import copilot_aic_report.github_client as gc

    def patched(self):
        if self.session is None:
            self.session = StandaloneEnterpriseSession()
        self.sleep = lambda s: None

    monkeypatch.setattr(gc.GitHubClient, "__post_init__", patched)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)

    # The enterprise endpoints were inaccessible, but per-org discovery over the
    # token's own orgs still produced seats -> a non-empty report.
    assert log.rows_written == 1
    assert log.seats_found == 1
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["user_login"] == "mona_acme"
    assert rows[0]["org_login"] == "acme"
    assert any("per-org" in w or "org discovery" in w for w in log.warnings)


def test_load_identity_map(tmp_path):
    p = tmp_path / "idmap.json"
    p.write_text(
        '[{"github_login":"mona_acme","scim_userName":"mona.svc","saml_nameId":"mona@acme.com","email":"m@acme.com"}]',
        encoding="utf-8",
    )
    entries = cli.load_identity_map(str(p))
    assert entries[0].github_login == "mona_acme"
    assert entries[0].scim_username == "mona.svc"
    assert cli.load_identity_map(None) == []
    assert cli.load_identity_map(str(tmp_path / "missing.json")) == []


def test_remap_scim_active_to_login():
    # SCIM userName differs from GitHub login (EMU); remap via identity index.
    scim_active = {"mona.svc": False, "octo": True}
    identity_index = {"mona.svc": "mona_acme"}
    remapped = cli._remap_scim_active_to_login(scim_active, identity_index)
    assert remapped["mona_acme"] is False   # resolved via index
    assert remapped["octo"] is True         # no index entry -> kept as-is (userName==login)


def test_remap_deprovisioned_forces_inactive_end_to_end(tmp_path, monkeypatch):
    # A SCIM-deprovisioned EMU user (userName != login) must be reported inactive.
    class DeprovSession(RoutingSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/scim/v2/enterprises/" in url:
                return FakeResponse(200, {"Resources": [{"userName": "mona.svc", "active": False}], "totalResults": 1}, headers={})
            return super().request(method, url, params, json, headers, timeout)

    import copilot_aic_report.github_client as gc

    def patched(self):
        if self.session is None:
            self.session = DeprovSession()
        self.sleep = lambda s: None

    monkeypatch.setattr(gc.GitHubClient, "__post_init__", patched)
    cfg = _make_cfg(tmp_path)
    cli.run(cfg)
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    r = rows[0]
    assert r["user_login"] == "mona_acme"
    assert r["user_status"] == "inactive"
    assert r["account_state"] == "deprovisioned"


class CountingSession(RoutingSession):
    def __init__(self):
        self.member_calls = 0

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        if "/members" in url:
            self.member_calls += 1
        return super().request(method, url, params, json, headers, timeout)


def _patch_counting(monkeypatch, holder):
    import copilot_aic_report.github_client as gc

    def patched(self):
        if self.session is None:
            self.session = holder["session"]
        self.sleep = lambda s: None

    monkeypatch.setattr(gc.GitHubClient, "__post_init__", patched)


def test_membership_not_fetched_by_default(tmp_path, monkeypatch):
    holder = {"session": CountingSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    assert cfg.fetch_membership is False
    cli.run(cfg)
    assert holder["session"].member_calls == 0  # no per-org member calls
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # Seat holders are assumed members -> account_state member (SCIM active).
    assert rows[0]["account_state"] == "member"


def test_membership_fetched_when_enabled(tmp_path, monkeypatch):
    holder = {"session": CountingSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    cfg.fetch_membership = True
    cli.run(cfg)
    assert holder["session"].member_calls == 1  # one call for the single org


def test_partial_graphql_errors_logged(tmp_path, monkeypatch):
    class PartialSession(RoutingSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/enterprises/" in url and "/copilot/billing/seats" in url:
                return FakeResponse(404, text='{"message":"Not Found"}')  # force per-org fallback
            if url.endswith("/graphql"):
                q = (json or {}).get("query", "")
                if "organizations" in q:
                    return FakeResponse(200, {
                        "data": {"enterprise": {"organizations": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"login": "acme"}, None],
                        }}},
                        "errors": [{"type": "FORBIDDEN", "path": ["enterprise", "organizations", "nodes", 1], "message": "forbids classic PAT"}],
                    })
            return super().request(method, url, params, json, headers, timeout)

    holder = {"session": PartialSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)
    assert log.rows_written == 1  # still produced from accessible org
    assert log.api_stats.get("partial_graphql_errors") == 1
    assert any("partial GraphQL" in w for w in log.warnings)


def test_org_seats_404_is_skipped(tmp_path, monkeypatch):
    # Force per-org fallback (enterprise seats 404), with one org lacking Copilot.
    class MixedSession(RoutingSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/enterprises/" in url and "/copilot/billing/seats" in url:
                return FakeResponse(404, text='{"message":"Not Found"}')
            if url.endswith("/graphql"):
                q = (json or {}).get("query", "")
                if "organizations" in q:
                    return FakeResponse(200, {"data": {"enterprise": {"organizations": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"login": "acme"}, {"login": "no-copilot"}],
                    }}}})
            if "/orgs/no-copilot/copilot/billing/seats" in url:
                return FakeResponse(404, text='{"message":"Not Found"}')
            return super().request(method, url, params, json, headers, timeout)

    holder = {"session": MixedSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)
    # acme still produces its row; no-copilot is skipped, not fatal.
    assert log.rows_written == 1
    assert any("skipped 1 org" in w for w in log.warnings)


def test_org_seats_403_is_skipped(tmp_path, monkeypatch):
    class ForbidSession(RoutingSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/enterprises/" in url and "/copilot/billing/seats" in url:
                return FakeResponse(404, text='{"message":"Not Found"}')
            if url.endswith("/graphql"):
                q = (json or {}).get("query", "")
                if "organizations" in q:
                    return FakeResponse(200, {"data": {"enterprise": {"organizations": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"login": "acme"}, {"login": "restricted"}],
                    }}}})
            if "/orgs/restricted/copilot/billing/seats" in url:
                return FakeResponse(403, text='{"message":"forbids classic PAT"}')
            return super().request(method, url, params, json, headers, timeout)

    holder = {"session": ForbidSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)
    assert log.rows_written == 1
    assert any("skipped 1 org" in w for w in log.warnings)


def test_enterprise_seats_preferred_no_per_org_seat_calls(tmp_path, monkeypatch):
    # Default path: enterprise seats endpoint used; no per-org seat calls made.
    class TrackingSession(RoutingSession):
        def __init__(self):
            self.org_seat_calls = 0
            self.enterprise_seat_calls = 0

        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/enterprises/" in url and "/copilot/billing/seats" in url:
                self.enterprise_seat_calls += 1
            elif "/orgs/" in url and "/copilot/billing/seats" in url:
                self.org_seat_calls += 1
            return super().request(method, url, params, json, headers, timeout)

    holder = {"session": TrackingSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)
    assert holder["session"].enterprise_seat_calls >= 1
    assert holder["session"].org_seat_calls == 0
    assert log.rows_written == 1
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["org_login"] == "acme"  # org attribution from enterprise seat


def test_suspended_guid_user_live_end_to_end(tmp_path, monkeypatch):
    # A suspended EMU user surfaces with a GUID login on the enterprise seats endpoint.
    guid = "2f1c8e4a-1234-4abc-9def-0123456789ab"

    class SuspendedSession(RoutingSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if url.endswith("/graphql"):
                q = (json or {}).get("query", "")
                if "ownerInfo" in q:
                    return FakeResponse(200, {"data": {"enterprise": {"ownerInfo": {"samlIdentityProvider": {
                        "externalIdentities": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [{"samlIdentity": {"nameId": guid}, "scimIdentity": {"username": guid}, "user": {"login": "real_dev"}}],
                        }
                    }}}}})
            if "/enterprises/" in url and "/copilot/billing/seats" in url:
                return FakeResponse(200, {"total_seats": 1, "seats": [{
                    "assignee": {"login": guid, "id": 9, "type": "User"},
                    "organization": {"login": "acme"},
                    "created_at": "2026-03-01T00:00:00Z",
                    "pending_cancellation_date": None,
                    "plan_type": "enterprise",
                }]}, headers={})
            return super().request(method, url, params, json, headers, timeout)

    holder = {"session": SuspendedSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    cli.run(cfg)
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    r = rows[0]
    assert r["user_login"] == "real_dev"           # GUID resolved to real login
    assert r["external_identity"] == guid          # GUID surfaced separately
    assert r["user_status"] == "inactive"          # suspended -> inactive
    assert r["account_state"] == "suspended"


def test_aic_consumption_populated_end_to_end(tmp_path, monkeypatch):
    class AicSession(RoutingSession):
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            if "/settings/billing/ai_credit/usage" in url:
                return FakeResponse(200, {"timePeriod": {"year": 2026, "month": 7},
                                          "user": (params or {}).get("user"),
                                          "usageItems": [{"netQuantity": 250, "netAmount": 2.5}]})
            return super().request(method, url, params, json, headers, timeout)

    holder = {"session": AicSession()}
    _patch_counting(monkeypatch, holder)
    cfg = _make_cfg(tmp_path)
    log = cli.run(cfg)
    assert log.aic_consumption_source == "api"
    with open(cfg.output_path, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    r = rows[0]
    assert r["aic_consumed"] == "250"
    assert r["aic_consumed_usd"] == "2.50"
