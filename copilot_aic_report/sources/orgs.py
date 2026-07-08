"""Enterprise organization discovery source."""
from __future__ import annotations


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
    """Return configured or enterprise-discovered organization logins."""
    explicit_orgs = cfg.orgs_list()
    if explicit_orgs is not None:
        return _dedupe(explicit_orgs)

    enterprise_slug = cfg.enterprise_slug
    if not enterprise_slug or not str(enterprise_slug).strip():
        raise ValueError("enterprise_slug is required when auto-discovering organizations")

    nodes = client.graphql_paginate(
        ORG_DISCOVERY_QUERY,
        {"slug": enterprise_slug, "after": None},
        ["enterprise", "organizations"],
    )
    return _dedupe(
        node["login"]
        for node in nodes
        if node and node.get("login")
    )


def _dedupe(values) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
