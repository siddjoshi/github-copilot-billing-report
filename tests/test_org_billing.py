import pytest

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import GitHubError
from copilot_aic_report.sources.org_billing import fetch_org_billing


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.response


class RaisingClient:
    def __init__(self, status):
        self.status = status

    def get(self, path, params=None):
        raise GitHubError("boom", status=self.status, body={"message": "boom"})


def test_fetch_org_billing_maps_full_response():
    response = {
        "seat_breakdown": {
            "total": 42,
            "active_this_cycle": 30,
            "inactive_this_cycle": 7,
            "added_this_cycle": 5,
            "pending_cancellation": 3,
            "pending_invitation": 2,
        },
        "plan_type": "enterprise",
        "seat_management_setting": "assign_selected",
        "public_code_suggestions": "enabled",
    }
    client = FakeClient(response)

    summary = fetch_org_billing(client, Config(), "octo-org")

    assert client.calls == [("/orgs/octo-org/copilot/billing", None)]
    assert summary.org_login == "octo-org"
    assert summary.plan_type == "enterprise"
    assert summary.total == 42
    assert summary.active_this_cycle == 30
    assert summary.inactive_this_cycle == 7
    assert summary.pending_cancellation == 3
    assert summary.pending_invitation == 2
    assert summary.raw is response


def test_fetch_org_billing_tolerates_missing_fields():
    response = {
        "seat_breakdown": {
            "total": 4,
            "pending_invitation": 1,
        }
    }
    client = FakeClient(response)

    summary = fetch_org_billing(client, Config(), "sparse-org")

    assert client.calls == [("/orgs/sparse-org/copilot/billing", None)]
    assert summary.org_login == "sparse-org"
    assert summary.plan_type is None
    assert summary.total == 4
    assert summary.active_this_cycle is None
    assert summary.inactive_this_cycle is None
    assert summary.pending_cancellation is None
    assert summary.pending_invitation == 1
    assert summary.raw is response


def test_fetch_org_billing_returns_empty_summary_on_404():
    summary = fetch_org_billing(RaisingClient(404), Config(), "missing-org")

    assert summary.org_login == "missing-org"
    assert summary.plan_type is None
    assert summary.total is None
    assert summary.active_this_cycle is None
    assert summary.inactive_this_cycle is None
    assert summary.pending_cancellation is None
    assert summary.pending_invitation is None
    assert summary.raw == {}


def test_fetch_org_billing_propagates_non_404_github_errors():
    with pytest.raises(GitHubError) as exc_info:
        fetch_org_billing(RaisingClient(500), Config(), "broken-org")

    assert exc_info.value.status == 500
