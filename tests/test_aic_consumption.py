import pytest

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import AuthFailure, GitHubError
from copilot_aic_report.sources.aic_consumption import (
    AicSourceUnavailable,
    fetch_from_api,
    get_consumption,
    load_from_csv,
)


class FakeClient:
    def __init__(self, payload=None, exc=None, payload_by_user=None):
        self.payload = payload
        self.payload_by_user = payload_by_user
        self.exc = exc
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.exc:
            raise self.exc
        if self.payload_by_user is not None:
            user = (params or {}).get("user")
            return self.payload_by_user.get(user, {"usageItems": []})
        return self.payload


def test_load_from_csv_parses_header_variants_derives_usd_and_skips_rows_without_user(tmp_path):
    csv_path = tmp_path / "usage.csv"
    csv_path.write_text(
        " Username ,AI_Credits,organizationName\n"
        "alice,12.5,octo-org\n"
        ",99,ignored-org\n"
        "bob,0,second-org\n",
        encoding="utf-8",
    )
    cfg = Config(credit_to_usd=0.02)

    rows = load_from_csv(csv_path, cfg)

    assert [row.user_login for row in rows] == ["alice", "bob"]
    assert rows[0].org_login == "octo-org"
    assert rows[0].credits_consumed == 12.5
    assert rows[0].usd_consumed == 0.25
    assert rows[0].source == "csv"
    assert rows[0].raw[" Username "] == "alice"
    assert rows[1].org_login == "second-org"
    assert rows[1].usd_consumed == 0.0


def test_load_from_csv_uses_explicit_usd_column_and_accepts_alternate_names(tmp_path):
    csv_path = tmp_path / "usage.csv"
    csv_path.write_text(
        "handle,premium_requests,org,net_amount\n"
        "carol,50,eng,4.75\n",
        encoding="utf-8",
    )

    rows = load_from_csv(str(csv_path), Config(credit_to_usd=0.99))

    assert len(rows) == 1
    assert rows[0].user_login == "carol"
    assert rows[0].org_login == "eng"
    assert rows[0].credits_consumed == 50.0
    assert rows[0].usd_consumed == 4.75


def test_load_from_csv_missing_file_raises_source_unavailable(tmp_path):
    missing = tmp_path / "missing.csv"

    with pytest.raises(AicSourceUnavailable):
        load_from_csv(missing, Config())


def test_fetch_from_api_queries_enterprise_endpoint_and_sums_items():
    client = FakeClient({"usageItems": [
        {"product": "copilot", "grossQuantity": 5, "grossAmount": 0.05},
        {"product": "copilot", "grossQuantity": 3, "grossAmount": 0.03},
    ]})
    cfg = Config(enterprise_slug="my-ent", billing_period="2026-07", credit_to_usd=0.01)

    rows = fetch_from_api(client, cfg, [("platform", "dana")])

    assert len(rows) == 1
    assert rows[0].user_login == "dana"
    assert rows[0].org_login is None  # enterprise-wide per-user consumption
    assert rows[0].credits_consumed == 8.0
    assert rows[0].usd_consumed == pytest.approx(0.08)
    assert rows[0].source == "api"
    assert client.calls == [(
        "/enterprises/my-ent/settings/billing/ai_credit/usage",
        {"year": 2026, "month": 7, "user": "dana"},
    )]


def test_fetch_from_api_dedupes_logins_across_orgs():
    client = FakeClient({"usageItems": [{"grossQuantity": 10, "grossAmount": 0.1}]})
    cfg = Config(enterprise_slug="ent", billing_period="2026-07")
    # Same login in two orgs -> queried once (enterprise-wide).
    rows = fetch_from_api(client, cfg, [("org-a", "dana"), ("org-b", "dana")])
    assert len(rows) == 1
    assert len(client.calls) == 1


def test_fetch_from_api_derives_usd_when_amount_absent():
    client = FakeClient({"usageItems": [{"grossQuantity": 100}]})
    cfg = Config(billing_period="2026-07", credit_to_usd=0.01)
    rows = fetch_from_api(client, cfg, [("org", "u")])
    assert rows[0].credits_consumed == 100.0
    assert rows[0].usd_consumed == pytest.approx(1.0)


def test_fetch_from_api_skips_users_with_zero_consumption():
    client = FakeClient({"usageItems": [{"grossQuantity": 0, "grossAmount": 0}]})
    rows = fetch_from_api(client, Config(billing_period="2026-07"), [("org", "u")])
    assert rows == []


def test_fetch_from_api_all_orgs_unavailable_raises():
    # Every org 404s -> source unavailable so callers can fall back to CSV.
    with pytest.raises(AicSourceUnavailable):
        fetch_from_api(FakeClient(exc=GitHubError("missing", status=404)),
                       Config(billing_period="2026-07"), [("org", "u")])


def test_fetch_from_api_auth_failure_raises_unavailable():
    with pytest.raises(AicSourceUnavailable):
        fetch_from_api(FakeClient(exc=AuthFailure("forbidden", status=403)),
                       Config(billing_period="2026-07"), [("org", "u")])


def test_fetch_from_api_partial_auth_failure_keeps_other_users():
    # A single user's exhausted-retry 403 (e.g. secondary rate limit) must NOT
    # discard every other user's consumption — the report showed AIC=0 for
    # everyone because one auth failure aborted the whole batch.
    class PartialClient:
        def __init__(self):
            self.calls = []

        def get(self, path, params=None):
            self.calls.append((path, params))
            user = (params or {}).get("user")
            if user == "boom":
                raise AuthFailure("forbidden", status=403)
            if user == "alice":
                return {"usageItems": [{"grossQuantity": 10, "grossAmount": 0.1}]}
            return {"usageItems": []}

    cfg = Config(enterprise_slug="ent", billing_period="2026-07", credit_to_usd=0.01, aic_concurrency=1)
    rows = fetch_from_api(PartialClient(), cfg, [("org", "boom"), ("org", "alice")])

    assert [r.user_login for r in rows] == ["alice"]
    assert rows[0].credits_consumed == 10.0


def test_fetch_from_api_all_auth_failures_still_raises_unavailable():
    # When EVERY user fails with auth/rate-limit and nothing is collected, the
    # source is genuinely unavailable so callers can fall back to CSV.
    class AllFailClient:
        def get(self, path, params=None):
            raise AuthFailure("forbidden", status=403)

    cfg = Config(enterprise_slug="ent", billing_period="2026-07", aic_concurrency=1)
    with pytest.raises(AicSourceUnavailable):
        fetch_from_api(AllFailClient(), cfg, [("org", "a"), ("org", "b")])


def test_fetch_from_api_enterprise_endpoint_404_raises_unavailable():
    # The enterprise endpoint is a single endpoint; a 404 means unavailable.
    with pytest.raises(AicSourceUnavailable):
        fetch_from_api(FakeClient(exc=GitHubError("missing", status=404)),
                       Config(enterprise_slug="ent", billing_period="2026-07"), [("org", "x")])


def test_fetch_from_api_no_holders_returns_empty():
    assert fetch_from_api(FakeClient({"usageItems": []}), Config(billing_period="2026-07"), []) == []


def test_fetch_from_api_falls_back_to_user_id_for_deprovisioned():
    # The obfuscated login returns nothing; the numeric user id fallback finds usage.
    client = FakeClient(payload_by_user={
        "hash_LTIMPG": {"usageItems": []},
        "555": {"usageItems": [{"grossQuantity": 42, "grossAmount": 0.42}]},
    })
    cfg = Config(enterprise_slug="ent", billing_period="2026-07", credit_to_usd=0.01)
    rows = fetch_from_api(client, cfg, [("acme", "hash_LTIMPG", 555)])
    assert len(rows) == 1
    # Attributed to the (obfuscated) login so it matches the report row's user_login.
    assert rows[0].user_login == "hash_LTIMPG"
    assert rows[0].credits_consumed == 42.0
    # Both the login and the id-fallback queries were issued.
    issued = [params.get("user") for _p, params in client.calls]
    assert issued == ["hash_LTIMPG", "555"]


def test_fetch_from_api_id_fallback_404_does_not_abort_run():
    # A 404 on the id fallback must NOT mark the whole endpoint unavailable; other
    # users still resolve normally.
    class SelectiveClient:
        def __init__(self):
            self.calls = []

        def get(self, path, params=None):
            self.calls.append((path, params))
            user = (params or {}).get("user")
            if user == "999":  # id fallback 404s
                raise GitHubError("missing", status=404)
            if user == "ghost_LTIMPG":
                return {"usageItems": []}
            if user == "alice":
                return {"usageItems": [{"grossQuantity": 10, "grossAmount": 0.1}]}
            return {"usageItems": []}

    cfg = Config(enterprise_slug="ent", billing_period="2026-07", credit_to_usd=0.01)
    rows = fetch_from_api(
        SelectiveClient(), cfg,
        [("acme", "ghost_LTIMPG", 999), ("acme", "alice", 1)],
    )
    assert [r.user_login for r in rows] == ["alice"]


def test_fetch_from_api_still_accepts_two_tuple_holders():
    client = FakeClient({"usageItems": [{"grossQuantity": 5, "grossAmount": 0.05}]})
    cfg = Config(enterprise_slug="ent", billing_period="2026-07", credit_to_usd=0.01)
    rows = fetch_from_api(client, cfg, [("acme", "dana")])
    assert rows[0].user_login == "dana"


def test_get_consumption_prefers_csv_when_path_is_set_even_if_api_enabled(tmp_path):
    csv_path = tmp_path / "usage.csv"
    csv_path.write_text("user,credits\nerin,3\n", encoding="utf-8")

    rows, source = get_consumption(
        FakeClient(payload={"usageItems": [{"grossQuantity": 10}]}),
        Config(aic_consumption_csv_path=str(csv_path), aic_consumption_api_enabled=True),
        [("org", "api-user")],
    )

    assert source == "csv"
    assert [row.user_login for row in rows] == ["erin"]


def test_get_consumption_uses_api_when_enabled_without_csv_path():
    rows, source = get_consumption(
        FakeClient(payload={"usageItems": [{"grossQuantity": 2, "grossAmount": 0.02}]}),
        Config(billing_period="2026-07", aic_consumption_api_enabled=True, aic_consumption_csv_path=None),
        [("eng", "frank")],
    )

    assert source == "api"
    assert [row.user_login for row in rows] == ["frank"]


def test_get_consumption_none_without_holders():
    rows, source = get_consumption(
        FakeClient(payload={"usageItems": [{"grossQuantity": 2}]}),
        Config(aic_consumption_api_enabled=True, aic_consumption_csv_path=None),
        [],
    )
    assert rows == []
    assert source == "none"


def test_get_consumption_returns_none_when_api_unavailable_and_csv_missing(tmp_path):
    rows, source = get_consumption(
        FakeClient(exc=GitHubError("gone", status=410)),
        Config(
            billing_period="2026-07",
            aic_consumption_api_enabled=True,
            aic_consumption_csv_path=str(tmp_path / "missing.csv"),
        ),
        [("org", "u")],
    )

    assert rows == []
    assert source == "none"


def test_get_consumption_returns_none_when_no_sources_configured():
    rows, source = get_consumption(
        FakeClient(payload={"usageItems": [{"grossQuantity": 1}]}),
        Config(aic_consumption_api_enabled=False, aic_consumption_csv_path=None),
        [("org", "ignored")],
    )

    assert rows == []
    assert source == "none"
