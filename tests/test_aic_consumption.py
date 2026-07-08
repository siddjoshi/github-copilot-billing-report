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
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.exc:
            raise self.exc
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


def test_fetch_from_api_maps_canned_rows_and_sends_period_params():
    client = FakeClient({"rows": [{"login": "dana", "quantity": "7", "org": "platform"}]})
    cfg = Config(enterprise_slug="my-ent", billing_period="2026-07", credit_to_usd=0.5)

    rows = fetch_from_api(client, cfg)

    assert len(rows) == 1
    assert rows[0].user_login == "dana"
    assert rows[0].org_login == "platform"
    assert rows[0].credits_consumed == 7.0
    assert rows[0].usd_consumed == 3.5
    assert rows[0].source == "api"
    assert client.calls == [(
        "/enterprises/my-ent/settings/billing/premium_request/usage",
        {"year": 2026, "month": 7},
    )]


@pytest.mark.parametrize("exc", [GitHubError("missing", status=404), AuthFailure("forbidden", status=403)])
def test_fetch_from_api_unavailable_errors_raise_source_unavailable(exc):
    with pytest.raises(AicSourceUnavailable):
        fetch_from_api(FakeClient(exc=exc), Config(enterprise_slug="ent", billing_period="2026-07"))


def test_fetch_from_api_returns_empty_for_unrecognized_shape():
    rows = fetch_from_api(FakeClient({"not_rows": [{"credits": 1}]}), Config(enterprise_slug="ent"))

    assert rows == []


def test_get_consumption_prefers_csv_when_path_is_set_even_if_api_enabled(tmp_path):
    csv_path = tmp_path / "usage.csv"
    csv_path.write_text("user,credits\nerin,3\n", encoding="utf-8")

    rows, source = get_consumption(
        FakeClient(payload={"rows": [{"login": "api-user", "quantity": 10}]}),
        Config(aic_consumption_csv_path=str(csv_path), aic_consumption_api_enabled=True),
    )

    assert source == "csv"
    assert [row.user_login for row in rows] == ["erin"]


def test_get_consumption_uses_api_when_enabled_without_csv_path():
    rows, source = get_consumption(
        FakeClient(payload={"rows": [{"user": "frank", "credits": 2}]}),
        Config(enterprise_slug="ent", aic_consumption_api_enabled=True, aic_consumption_csv_path=None),
    )

    assert source == "api"
    assert [row.user_login for row in rows] == ["frank"]


def test_get_consumption_returns_none_when_api_unavailable_and_csv_missing(tmp_path):
    rows, source = get_consumption(
        FakeClient(exc=GitHubError("gone", status=410)),
        Config(
            enterprise_slug="ent",
            aic_consumption_api_enabled=True,
            aic_consumption_csv_path=str(tmp_path / "missing.csv"),
        ),
    )

    assert rows == []
    assert source == "none"


def test_get_consumption_returns_none_when_no_sources_configured():
    rows, source = get_consumption(
        FakeClient(payload={"rows": [{"user": "ignored", "credits": 1}]}),
        Config(aic_consumption_api_enabled=False, aic_consumption_csv_path=None),
    )

    assert rows == []
    assert source == "none"
