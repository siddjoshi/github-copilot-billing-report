"""SAML/SCIM external identity source fetchers."""
from __future__ import annotations

import sys

from copilot_aic_report.github_client import GraphQLError
from copilot_aic_report.models import ExternalIdentity


ENTERPRISE_IDENTITIES_QUERY = """
query($slug:String!,$after:String){
  enterprise(slug:$slug){
    ownerInfo{
      samlIdentityProvider{
        externalIdentities(first:100, after:$after){
          pageInfo{hasNextPage endCursor}
          nodes{
            samlIdentity{ nameId }
            scimIdentity{ username }
            user{ login }
          }
        }
      }
    }
  }
}
"""


ORG_IDENTITIES_QUERY = """
query($login:String!,$after:String){
  organization(login:$login){
    samlIdentityProvider{
      externalIdentities(first:100, after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{
          samlIdentity{ nameId }
          scimIdentity{ username }
          user{ login }
        }
      }
    }
  }
}
"""


def fetch_enterprise_identities(client, cfg) -> list[ExternalIdentity]:
    """Fetch enterprise SAML/SCIM external identities."""
    try:
        nodes = client.graphql_paginate(
            ENTERPRISE_IDENTITIES_QUERY,
            {"slug": cfg.enterprise_slug, "after": None},
            ["enterprise", "ownerInfo", "samlIdentityProvider", "externalIdentities"],
        )
        return [_identity_from_node(node, "enterprise") for node in nodes]
    except GraphQLError as exc:
        print(
            f"Enterprise external identities unavailable; SSO/permission may be unavailable: {exc}",
            file=sys.stderr,
        )
        return []


def fetch_org_identities(client, cfg, org_login) -> list[ExternalIdentity]:
    """Fetch organization SAML/SCIM external identities."""
    del cfg
    try:
        nodes = client.graphql_paginate(
            ORG_IDENTITIES_QUERY,
            {"login": org_login, "after": None},
            ["organization", "samlIdentityProvider", "externalIdentities"],
        )
        return [_identity_from_node(node, f"org:{org_login}") for node in nodes]
    except GraphQLError as exc:
        print(
            f"Organization external identities unavailable for {org_login}; "
            f"SSO/permission may be unavailable: {exc}",
            file=sys.stderr,
        )
        return []


def build_identity_index(identities) -> dict:
    """Map normalized external identifiers to GitHub logins."""
    index = {}
    for identity in identities:
        if not identity.user_login:
            continue
        for key in (
            _norm(identity.saml_name_id),
            _norm(identity.scim_username),
            _norm(identity.email),
        ):
            if key and key not in index:
                index[key] = identity.user_login
    return index


def _norm(s):
    return s.strip().lower() if s else None


def _identity_from_node(node, scope: str) -> ExternalIdentity:
    node = node or {}
    saml_identity = node.get("samlIdentity") or {}
    scim_identity = node.get("scimIdentity") or {}
    user = node.get("user") or {}
    return ExternalIdentity(
        user_login=user.get("login"),
        saml_name_id=saml_identity.get("nameId"),
        scim_username=scim_identity.get("username"),
        email=None,
        scope=scope,
    )
