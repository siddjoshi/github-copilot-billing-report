import pytest

from copilot_aic_report.github_client import GraphQLError
from copilot_aic_report.sources.orgs import discover_accessible_orgs, discover_orgs


class FakeConfig:
    def __init__(self, orgs, enterprise_slug="ent"):
        self._orgs = orgs
        self.enterprise_slug = enterprise_slug

    def orgs_list(self):
        return self._orgs


class FakeClient:
    """Fakes the pieces of GitHubClient that org discovery uses.

    ``graphql_pages`` is a list of ``data`` payloads returned by successive
    ``graphql`` calls (or an exception instance to raise). ``rest_orgs`` is the
    list of org dicts yielded by ``paginate('/user/orgs')``.
    """

    def __init__(self, graphql_pages=None, rest_orgs=None):
        self.graphql_pages = list(graphql_pages or [])
        self.rest_orgs = list(rest_orgs or [])
        self.graphql_calls = []
        self.rest_calls = []

    def graphql(self, query, variables):
        self.graphql_calls.append((query, variables))
        if not self.graphql_pages:
            return {}
        page = self.graphql_pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    def paginate(self, path, params=None, *, items_key=None):
        self.rest_calls.append(path)
        yield from self.rest_orgs


def _ent_page(nodes, has_next=False, cursor=None):
    return {
        "enterprise": {
            "organizations": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


def test_explicit_org_list_is_returned_deduped_in_order_without_api_calls():
    client = FakeClient(graphql_pages=[_ent_page([{"login": "unused"}])])
    cfg = FakeConfig(["alpha", "beta", "alpha", "gamma", "beta"])

    assert discover_orgs(client, cfg) == ["alpha", "beta", "gamma"]
    assert client.graphql_calls == []
    assert client.rest_calls == []


def test_auto_discovers_orgs_with_graphql_pagination_and_dedupes_in_order():
    client = FakeClient(
        graphql_pages=[
            _ent_page([{"login": "first"}, {"login": "second"}], has_next=True, cursor="c1"),
            _ent_page([{"login": "first"}, {"login": "third"}]),
        ]
    )
    cfg = FakeConfig(None, enterprise_slug="my-enterprise")

    assert discover_orgs(client, cfg) == ["first", "second", "third"]

    assert len(client.graphql_calls) == 2
    query, variables = client.graphql_calls[0]
    assert "enterprise(slug:$slug)" in query
    assert "organizations(first:100, after:$after)" in query
    assert "nodes{ login }" in query
    assert variables == {"slug": "my-enterprise", "after": None}
    assert client.graphql_calls[1][1] == {"slug": "my-enterprise", "after": "c1"}
    # Enterprise was visible, so the REST fallback is not used.
    assert client.rest_calls == []


@pytest.mark.parametrize("slug", ["", "   ", None])
def test_auto_discovery_requires_enterprise_slug(slug):
    client = FakeClient()
    cfg = FakeConfig(None, enterprise_slug=slug)

    with pytest.raises(ValueError, match="enterprise_slug"):
        discover_orgs(client, cfg)


def test_auto_discovery_skips_loginless_and_none_nodes():
    # Partial GraphQL results can include None nodes (forbidden orgs) or nodes
    # without a login; these must be skipped, not crash.
    client = FakeClient(
        graphql_pages=[_ent_page([{"login": "ok"}, None, {}, {"login": None}, {"login": "ok2"}])]
    )
    cfg = FakeConfig(None, enterprise_slug="ent")
    assert discover_orgs(client, cfg) == ["ok", "ok2"]
    assert client.rest_calls == []


def test_falls_back_to_user_orgs_when_enterprise_not_visible():
    # Enterprise node null (e.g. standalone Copilot enterprise, or a token without
    # read:enterprise): discovery falls back to the token's accessible orgs.
    client = FakeClient(
        graphql_pages=[{"enterprise": None}],
        rest_orgs=[{"login": "acme"}, {"login": "beta"}, {"login": "acme"}],
    )
    cfg = FakeConfig(None, enterprise_slug="ltimghce")

    assert discover_orgs(client, cfg) == ["acme", "beta"]
    assert client.rest_calls == ["/user/orgs"]


def test_falls_back_to_user_orgs_on_graphql_error():
    client = FakeClient(
        graphql_pages=[GraphQLError("enterprise: Not Found")],
        rest_orgs=[{"login": "one"}, {"login": None}, {"login": "two"}],
    )
    cfg = FakeConfig(None, enterprise_slug="ltimghce")

    assert discover_orgs(client, cfg) == ["one", "two"]
    assert client.rest_calls == ["/user/orgs"]


def test_discover_accessible_orgs_dedupes_and_skips_blanks():
    client = FakeClient(rest_orgs=[{"login": "a"}, None, {}, {"login": "a"}, {"login": "b"}])
    assert discover_accessible_orgs(client) == ["a", "b"]
