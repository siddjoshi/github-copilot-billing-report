import pytest

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import GitHubError
from copilot_aic_report.sources.billing_usage import (
    copilot_net_usd,
    fetch_enterprise_usage,
    fetch_org_usage,
    filter_copilot,
)


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else {"usageItems": []}
        self.error = error
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.error is not None:
            raise self.error
        return self.payload


def test_fetch_enterprise_usage_maps_fields_and_derives_year_month_params():
    client = FakeClient(
        {
            "usageItems": [
                {
                    "date": "2026-07-01",
                    "product": "copilot",
                    "sku": "copilot_enterprise",
                    "quantity": "3",
                    "unitType": "Seats",
                    "grossAmount": "117.00",
                    "discountAmount": None,
                    "netAmount": 100,
                    "organizationName": "octo-org",
                    "repositoryName": "octo-repo",
                    "extra": "preserved",
                }
            ]
        }
    )
    cfg = Config(enterprise_slug="octo-enterprise", billing_period="2026-07")

    lines = fetch_enterprise_usage(client, cfg)

    assert client.calls == [
        (
            "/enterprises/octo-enterprise/settings/billing/usage",
            {"year": 2026, "month": 7},
        )
    ]
    assert len(lines) == 1
    line = lines[0]
    assert line.date == "2026-07-01"
    assert line.product == "copilot"
    assert line.sku == "copilot_enterprise"
    assert line.quantity == 3.0
    assert line.unit_type == "Seats"
    assert line.gross_amount == 117.0
    assert line.discount_amount is None
    assert line.net_amount == 100.0
    assert line.organization_name == "octo-org"
    assert line.repository_name == "octo-repo"
    assert line.raw["extra"] == "preserved"


def test_fetch_org_usage_uses_org_endpoint_and_period_params():
    client = FakeClient({"usageItems": []})
    cfg = Config(billing_period="2025-12")

    assert fetch_org_usage(client, cfg, "octo-org") == []

    assert client.calls == [
        ("/organizations/octo-org/settings/billing/usage", {"year": 2025, "month": 12})
    ]


def test_filter_copilot_keeps_only_case_insensitive_copilot_products():
    lines = fetch_enterprise_usage(
        FakeClient(
            {
                "usageItems": [
                    {"product": "Copilot", "netAmount": 10},
                    {"product": "actions", "netAmount": 20},
                    {"product": None, "netAmount": 30},
                    {"product": "copilot", "netAmount": 40},
                ]
            }
        ),
        Config(enterprise_slug="ent", billing_period="2026-07"),
    )

    filtered = filter_copilot(lines)

    assert [line.product for line in filtered] == ["Copilot", "copilot"]


def test_copilot_net_usd_sums_only_copilot_net_amounts_and_ignores_none():
    lines = fetch_enterprise_usage(
        FakeClient(
            {
                "usageItems": [
                    {"product": "copilot", "netAmount": "10.25"},
                    {"product": "actions", "netAmount": "999.00"},
                    {"product": "COPILOT", "netAmount": None},
                    {"product": "copilot", "netAmount": 2},
                ]
            }
        ),
        Config(enterprise_slug="ent", billing_period="2026-07"),
    )

    assert copilot_net_usd(lines) == pytest.approx(12.25)


@pytest.mark.parametrize("status", [403, 404])
def test_fetch_returns_empty_and_logs_note_for_disabled_or_unavailable_endpoint(status, capsys):
    client = FakeClient(error=GitHubError("not available", status=status))
    cfg = Config(enterprise_slug="ent", billing_period="2026-07")

    assert fetch_enterprise_usage(client, cfg) == []

    captured = capsys.readouterr()
    assert "billing usage endpoint" in captured.err.lower()
    assert str(status) in captured.err
    assert "ent" in captured.err


def test_fetch_reraises_non_endpoint_availability_errors():
    client = FakeClient(error=GitHubError("server error", status=500))
    cfg = Config(enterprise_slug="ent", billing_period="2026-07")

    with pytest.raises(GitHubError) as exc:
        fetch_enterprise_usage(client, cfg)

    assert exc.value.status == 500
