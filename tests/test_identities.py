from dataclasses import replace

import pytest

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import AuthFailure, GraphQLError
from copilot_aic_report.models import ExternalIdentity
from copilot_aic_report.sources.identities import (
    build_identity_index,
    fetch_enterprise_identities,
    fetch_org_identities,
)


class FakeClient:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.calls = []

    def graphql_paginate(self, query, variables, page_path):
        self.calls.append((query, variables, page_path))
        if self.error:
            raise self.error
        yield from self.pages


def test_fetch_enterprise_identities_maps_nodes_and_uses_expected_pagination():
    client = FakeClient(
        [
            {
                "samlIdentity": {"nameId": "Alice@Example.COM "},
                "scimIdentity": {"username": "alice@example.com"},
                "user": {"login": "alice"},
            },
            {
                "samlIdentity": {"nameId": "revoked@example.com"},
                "scimIdentity": {"username": "revoked_scim"},
                "user": None,
            },
        ]
    )

    identities = fetch_enterprise_identities(client, Config(enterprise_slug="ent"))

    assert identities == [
        ExternalIdentity(
            user_login="alice",
            saml_name_id="Alice@Example.COM ",
            scim_username="alice@example.com",
            email=None,
            scope="enterprise",
        ),
        ExternalIdentity(
            user_login=None,
            saml_name_id="revoked@example.com",
            scim_username="revoked_scim",
            email=None,
            scope="enterprise",
        ),
    ]
    query, variables, page_path = client.calls[0]
    assert variables == {"slug": "ent", "after": None}
    assert page_path == ["enterprise", "ownerInfo", "samlIdentityProvider", "externalIdentities"]
    assert "enterprise(slug:$slug)" in query


def test_fetch_org_identities_maps_nodes_and_handles_none_sub_objects():
    client = FakeClient(
        [
            {
                "samlIdentity": None,
                "scimIdentity": {"username": "bob@example.com"},
                "user": {"login": "bob"},
            },
            {
                "samlIdentity": {"nameId": "charlie@example.com"},
                "scimIdentity": None,
                "user": {},
            },
        ]
    )

    identities = fetch_org_identities(client, Config(), "octo-org")

    assert identities == [
        ExternalIdentity(
            user_login="bob",
            saml_name_id=None,
            scim_username="bob@example.com",
            email=None,
            scope="org:octo-org",
        ),
        ExternalIdentity(
            user_login=None,
            saml_name_id="charlie@example.com",
            scim_username=None,
            email=None,
            scope="org:octo-org",
        ),
    ]
    query, variables, page_path = client.calls[0]
    assert variables == {"login": "octo-org", "after": None}
    assert page_path == ["organization", "samlIdentityProvider", "externalIdentities"]
    assert "organization(login:$login)" in query


@pytest.mark.parametrize(
    "fetcher,args",
    [
        (fetch_enterprise_identities, (Config(enterprise_slug="ent"),)),
        (fetch_org_identities, (Config(), "octo-org")),
    ],
)
def test_graphql_error_returns_empty_list_with_stderr_note(fetcher, args, capsys):
    client = FakeClient(error=GraphQLError("SSO unavailable"))

    assert fetcher(client, *args) == []

    assert "SSO/permission may be unavailable" in capsys.readouterr().err


def test_auth_failure_propagates():
    client = FakeClient(error=AuthFailure("bad token"))

    with pytest.raises(AuthFailure):
        fetch_enterprise_identities(client, Config(enterprise_slug="ent"))


def test_build_identity_index_normalizes_keys_skips_none_login_and_keeps_first_seen():
    identities = [
        ExternalIdentity(
            user_login="alice",
            saml_name_id=" Alice@Example.COM ",
            scim_username="ALICE_SCIM",
            email="Alice@Example.com",
        ),
        ExternalIdentity(
            user_login=None,
            saml_name_id="revoked@example.com",
            scim_username="alice_scim",
            email="revoked@example.com",
        ),
        ExternalIdentity(
            user_login="bob",
            saml_name_id="alice@example.com",
            scim_username=" Bob_Scim ",
            email=None,
        ),
        replace(ExternalIdentity(user_login="carol", saml_name_id="", scim_username="   ", email=None)),
    ]

    assert build_identity_index(identities) == {
        "alice@example.com": "alice",
        "alice_scim": "alice",
        "bob_scim": "bob",
    }
