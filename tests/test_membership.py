import pytest

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import AuthFailure, GitHubError
from copilot_aic_report.sources.membership import (
    build_account_states,
    fetch_org_members,
    fetch_scim_active,
    fetch_scim_state,
)


class FakeClient:
    def __init__(self, pages=None, error=None):
        self.pages = pages or {}
        self.error = error
        self.calls = []

    def paginate(self, path, params=None, *, items_key=None):
        self.calls.append((path, params, items_key))
        if self.error:
            raise self.error
        return iter(self.pages.get((path, items_key), []))


def test_fetch_org_members_returns_lowercase_login_set():
    client = FakeClient(
        {
            ("/orgs/Octo-Org/members", None): [
                {"login": "Mona"},
                {"login": "HUBOT"},
                {"id": 123},
            ]
        }
    )

    members = fetch_org_members(client, Config(), "Octo-Org")

    assert members == {"mona", "hubot"}
    assert client.calls == [("/orgs/Octo-Org/members", None, None)]


def test_fetch_org_members_returns_empty_set_with_note_on_404(capsys):
    client = FakeClient(error=GitHubError("not found", status=404))

    members = fetch_org_members(client, Config(), "missing-org")

    assert members == set()
    assert "missing-org" in capsys.readouterr().err


def test_fetch_org_members_propagates_auth_failure():
    client = FakeClient(error=AuthFailure("bad token", status=403))

    with pytest.raises(AuthFailure):
        fetch_org_members(client, Config(), "Octo-Org")


def test_fetch_scim_active_maps_lowercase_username_to_active_flag():
    client = FakeClient(
        {
            ("/scim/v2/enterprises/acme/Users", "Resources"): [
                {"userName": "Mona", "active": True},
                {"userName": "HUBOT", "active": False},
                {"active": True},
            ]
        }
    )

    active_by_username = fetch_scim_active(client, Config(enterprise_slug="acme"))

    assert active_by_username == {"mona": True, "hubot": False}
    assert client.calls == [("/scim/v2/enterprises/acme/Users", None, "Resources")]


def test_fetch_scim_active_returns_empty_dict_with_note_on_404(capsys):
    client = FakeClient(error=GitHubError("not found", status=404))

    active_by_username = fetch_scim_active(client, Config(enterprise_slug="acme"))

    assert active_by_username == {}
    assert "acme" in capsys.readouterr().err


def test_fetch_scim_active_propagates_auth_failure():
    client = FakeClient(error=AuthFailure("missing scope", status=403))

    with pytest.raises(AuthFailure):
        fetch_scim_active(client, Config(enterprise_slug="acme"))


def test_fetch_scim_state_returns_active_and_deprovisioned_dates():
    client = FakeClient(
        {
            ("/scim/v2/enterprises/acme/Users", "Resources"): [
                {"userName": "Mona", "active": True, "meta": {"lastModified": "2026-01-01T00:00:00Z"}},
                {"userName": "HUBOT", "active": False, "meta": {"lastModified": "2026-05-14T09:30:00Z"}},
                {"userName": "NoDate", "active": False},
                {"active": False, "meta": {"lastModified": "2026-01-01T00:00:00Z"}},
            ]
        }
    )

    active, deprovisioned_at = fetch_scim_state(client, Config(enterprise_slug="acme"))

    assert active == {"mona": True, "hubot": False, "nodate": False}
    # Only inactive users with a lastModified are recorded.
    assert deprovisioned_at == {"hubot": "2026-05-14T09:30:00Z"}


def test_build_account_states_carries_deprovisioned_at():
    states = build_account_states(
        org_members_by_org={"octo": set()},
        scim_active={"mona": False},
        seat_logins_by_org={"octo": {"Mona"}},
        scim_deprovisioned_at={"mona": "2026-05-14T09:30:00Z"},
    )
    assert states[0].deprovisioned_at == "2026-05-14T09:30:00Z"


def test_build_account_states_uses_membership_and_scim_state_outcomes():
    states = build_account_states(
        org_members_by_org={
            "octo": {"mona", "activebot"},
            "space": {"other"},
        },
        scim_active={
            "mona": True,
            "activebot": False,
            "ghost": True,
        },
        seat_logins_by_org={
            "octo": {"Mona", "ActiveBot"},
            "space": {"Ghost"},
        },
    )

    by_org_user = {(state.org_login, state.user_login): state for state in states}

    mona = by_org_user[("octo", "Mona")]
    assert mona.is_member is True
    assert mona.scim_active is True
    assert mona.suspended is False
    assert mona.state() == "member"

    activebot = by_org_user[("octo", "ActiveBot")]
    assert activebot.is_member is True
    assert activebot.scim_active is False
    assert activebot.suspended is False
    assert activebot.state() == "deprovisioned"

    ghost = by_org_user[("space", "Ghost")]
    assert ghost.is_member is False
    assert ghost.scim_active is True
    assert ghost.suspended is False
    assert ghost.state() == "deprovisioned"
