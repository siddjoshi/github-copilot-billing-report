import pytest

from copilot_aic_report.sources.orgs import discover_orgs


class FakeConfig:
    def __init__(self, orgs, enterprise_slug="ent"):
        self._orgs = orgs
        self.enterprise_slug = enterprise_slug

    def orgs_list(self):
        return self._orgs


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def graphql_paginate(self, query, variables, page_path):
        self.calls.append((query, variables, page_path))
        for page in self.pages:
            yield from page


def test_explicit_org_list_is_returned_deduped_in_order_without_graphql():
    client = FakeClient([[{"login": "unused"}]])
    cfg = FakeConfig(["alpha", "beta", "alpha", "gamma", "beta"])

    assert discover_orgs(client, cfg) == ["alpha", "beta", "gamma"]
    assert client.calls == []


def test_auto_discovers_orgs_with_graphql_pagination_and_dedupes_in_order():
    client = FakeClient(
        [
            [{"login": "first"}, {"login": "second"}],
            [{"login": "first"}, {"login": "third"}],
        ]
    )
    cfg = FakeConfig(None, enterprise_slug="my-enterprise")

    assert discover_orgs(client, cfg) == ["first", "second", "third"]

    assert len(client.calls) == 1
    query, variables, page_path = client.calls[0]
    assert "enterprise(slug:$slug)" in query
    assert "organizations(first:100, after:$after)" in query
    assert "nodes{ login }" in query
    assert variables == {"slug": "my-enterprise", "after": None}
    assert page_path == ["enterprise", "organizations"]


@pytest.mark.parametrize("slug", ["", "   ", None])
def test_auto_discovery_requires_enterprise_slug(slug):
    client = FakeClient([])
    cfg = FakeConfig(None, enterprise_slug=slug)

    with pytest.raises(ValueError, match="enterprise_slug"):
        discover_orgs(client, cfg)


def test_auto_discovery_skips_loginless_and_none_nodes():
    # Partial GraphQL results can include None nodes (forbidden orgs) or nodes
    # without a login; these must be skipped, not crash.
    client = FakeClient([[{"login": "ok"}, None, {}, {"login": None}, {"login": "ok2"}]])
    cfg = FakeConfig(None, enterprise_slug="ent")
    assert discover_orgs(client, cfg) == ["ok", "ok2"]
