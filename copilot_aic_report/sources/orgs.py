"""Enterprise organization discovery source."""
from __future__ import annotations

from typing import List, Optional, Tuple

from copilot_aic_report.github_client import GraphQLError


ORG_DISCOVERY_QUERY = """
query($slug: String!, $after: String) {
  enterprise(slug:$slug) {
    organizations(first:100, after:$after) {
      pageInfo{hasNextPage endCursor}
      nodes{ login }
    }
  }
}
"""


def discover_orgs(client, cfg) -> list[str]:
    """Return configured or discovered organization logins.

    Discovery precedence:

    1. An explicit ``orgs`` list from config (no API calls).
    2. Enterprise-wide GraphQL discovery (``enterprise(slug){organizations}``).
    3. REST fallback over the token's own organizations (``GET /user/orgs``) when the
       enterprise is not visible via GraphQL — e.g. standalone Copilot enterprises, or
       a token without ``read:enterprise``. Organizations the token cannot read Copilot
       billing for are skipped later by the caller.
    """
    return discover_orgs_with_source(client, cfg)[0]


def discover_orgs_with_source(client, cfg) -> tuple[list[str], str]:
    """Return configured/discovered organization logins plus the discovery source."""
    explicit_orgs = cfg.orgs_list()
    if explicit_orgs is not None:
        return _dedupe(explicit_orgs), "config"

    enterprise_slug = cfg.enterprise_slug
    if not enterprise_slug or not str(enterprise_slug).strip():
        raise ValueError("enterprise_slug is required when auto-discovering organizations")

    logins, enterprise_visible = _discover_enterprise_orgs(client, enterprise_slug)
    if enterprise_visible:
        return _dedupe(logins), "enterprise"

    # Enterprise not queryable via GraphQL (standalone Copilot enterprise or a token
    # lacking read:enterprise). Fall back to the token's own accessible organizations
    # so per-org Copilot billing can still be reported.
    return discover_accessible_orgs(client), "accessible"


def _discover_enterprise_orgs(client, enterprise_slug: str) -> Tuple[List[str], bool]:
    """Page enterprise organizations via GraphQL.

    Returns ``(logins, enterprise_visible)``. ``enterprise_visible`` is ``False`` when
    the ``enterprise`` node is null/inaccessible (partial GraphQL "Not Found" errors or
    a hard :class:`GraphQLError`), signalling the caller to fall back to REST discovery.
    """
    logins: List[str] = []
    after: Optional[str] = None
    enterprise_visible = False
    while True:
        try:
            data = client.graphql(ORG_DISCOVERY_QUERY, {"slug": enterprise_slug, "after": after})
        except GraphQLError:
            return [], False
        enterprise = (data or {}).get("enterprise")
        if enterprise is None:
            return logins, enterprise_visible
        enterprise_visible = True
        connection = enterprise.get("organizations") or {}
        for node in connection.get("nodes") or []:
            if node and node.get("login"):
                logins.append(node["login"])
        page_info = connection.get("pageInfo") or {}
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            after = page_info["endCursor"]
        else:
            break
    return logins, enterprise_visible


def discover_accessible_orgs(client) -> list[str]:
    """Discover organizations the authenticated token can access (REST ``/user/orgs``).

    Used when enterprise-level GraphQL discovery is unavailable. The returned set is
    scoped to what the token can see, not verified enterprise membership; callers should
    treat it as best-effort and skip orgs whose Copilot billing is inaccessible.
    """
    return _dedupe(
        org.get("login")
        for org in client.paginate("/user/orgs")
        if org and org.get("login")
    )


def _dedupe(values) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
