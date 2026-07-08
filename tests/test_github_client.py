import pytest

from copilot_aic_report import github_client as gc
from copilot_aic_report.github_client import (
    AuthFailure,
    GitHubClient,
    GitHubError,
    GraphQLError,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, responses):
        # responses: list of FakeResponse returned in order
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return self._responses.pop(0)


def make_client(session, **kw):
    return GitHubClient(
        token="t",
        session=session,
        sleep=lambda s: None,
        rng=lambda: 0.5,
        now=lambda: 1000.0,
        **kw,
    )


def test_get_returns_json():
    sess = FakeSession([FakeResponse(200, {"ok": True})])
    client = make_client(sess)
    assert client.get("/thing") == {"ok": True}
    assert client.stats.rest_calls == 1


def test_auth_failure_raises_no_retry():
    sess = FakeSession([FakeResponse(401, text="bad creds")])
    client = make_client(sess)
    with pytest.raises(AuthFailure) as exc:
        client.get("/thing")
    assert exc.value.status == 401


def test_403_scope_raises_authfailure():
    sess = FakeSession([FakeResponse(403, text="forbidden", headers={})])
    client = make_client(sess)
    with pytest.raises(AuthFailure):
        client.get("/x")


def test_rate_limit_retry_after_then_success():
    sess = FakeSession(
        [
            FakeResponse(403, headers={"Retry-After": "0", "X-RateLimit-Remaining": "0"}),
            FakeResponse(200, {"done": 1}),
        ]
    )
    client = make_client(sess)
    assert client.get("/x") == {"done": 1}
    assert client.stats.rate_limit_waits == 1


def test_rate_limit_reset_header():
    sess = FakeSession(
        [
            FakeResponse(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1005"}),
            FakeResponse(200, {"done": 1}),
        ]
    )
    client = make_client(sess)
    assert client.get("/x") == {"done": 1}
    assert client.stats.total_wait_seconds == pytest.approx(5.0)


def test_5xx_retry_then_success():
    sess = FakeSession([FakeResponse(500, text="oops"), FakeResponse(200, {"ok": 1})])
    client = make_client(sess)
    assert client.get("/x") == {"ok": 1}
    assert client.stats.retries == 1


def test_5xx_exhausts_retries():
    responses = [FakeResponse(500, text="oops") for _ in range(10)]
    sess = FakeSession(responses)
    client = make_client(sess, max_retries=2)
    with pytest.raises(GitHubError):
        client.get("/x")


def test_4xx_client_error_raises():
    sess = FakeSession([FakeResponse(422, text="unprocessable")])
    client = make_client(sess)
    with pytest.raises(GitHubError) as exc:
        client.get("/x")
    assert exc.value.status == 422


def test_paginate_follows_link():
    page1 = FakeResponse(
        200,
        [{"id": 1}],
        headers={"Link": '<https://api.github.com/next?page=2>; rel="next"'},
    )
    page2 = FakeResponse(200, [{"id": 2}], headers={})
    sess = FakeSession([page1, page2])
    client = make_client(sess)
    items = list(client.paginate("/things"))
    assert items == [{"id": 1}, {"id": 2}]


def test_paginate_items_key():
    resp = FakeResponse(200, {"total_seats": 1, "seats": [{"a": 1}]}, headers={})
    sess = FakeSession([resp])
    client = make_client(sess)
    items = list(client.paginate("/orgs/x/copilot/billing/seats", items_key="seats"))
    assert items == [{"a": 1}]


def test_graphql_success():
    sess = FakeSession([FakeResponse(200, {"data": {"x": 1}})])
    client = make_client(sess)
    assert client.graphql("query") == {"x": 1}


def test_graphql_errors_raise():
    sess = FakeSession([FakeResponse(200, {"errors": [{"message": "bad"}]})])
    client = make_client(sess)
    with pytest.raises(GraphQLError):
        client.graphql("query")


def test_graphql_partial_data_returned_with_errors():
    # GitHub returns partial results: data present + per-field errors. Must not raise.
    sess = FakeSession([FakeResponse(200, {
        "data": {"enterprise": {"organizations": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"login": "a"}, None]}}},
        "errors": [{"type": "FORBIDDEN", "path": ["enterprise", "organizations", "nodes", 1], "message": "forbids classic PAT"}],
    })])
    client = make_client(sess)
    data = client.graphql("q")
    assert data["enterprise"]["organizations"]["nodes"][0]["login"] == "a"
    assert len(client.partial_graphql_errors) == 1


def test_graphql_paginate_skips_none_nodes():
    resp = FakeResponse(200, {
        "data": {"enterprise": {"organizations": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"login": "a"}, None, {"login": "b"}]}}},
        "errors": [{"type": "FORBIDDEN", "path": ["enterprise", "organizations", "nodes", 1]}],
    })
    sess = FakeSession([resp])
    client = make_client(sess)
    nodes = list(client.graphql_paginate("q", {"after": None}, ["enterprise", "organizations"]))
    assert [n["login"] for n in nodes] == ["a", "b"]


def test_graphql_paginate():
    p1 = FakeResponse(
        200,
        {
            "data": {
                "enterprise": {
                    "organizations": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        "nodes": [{"login": "a"}],
                    }
                }
            }
        },
    )
    p2 = FakeResponse(
        200,
        {
            "data": {
                "enterprise": {
                    "organizations": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"login": "b"}],
                    }
                }
            }
        },
    )
    sess = FakeSession([p1, p2])
    client = make_client(sess)
    nodes = list(
        client.graphql_paginate(
            "query", {"slug": "x", "after": None}, ["enterprise", "organizations"]
        )
    )
    assert [n["login"] for n in nodes] == ["a", "b"]


def test_graphql_paginate_missing_connection():
    sess = FakeSession([FakeResponse(200, {"data": {"enterprise": None}})])
    client = make_client(sess)
    nodes = list(client.graphql_paginate("q", {"after": None}, ["enterprise", "organizations"]))
    assert nodes == []


def test_get_oauth_scopes():
    sess = FakeSession([FakeResponse(200, {}, headers={"X-OAuth-Scopes": "read:org, repo"})])
    client = make_client(sess)
    assert client.get_oauth_scopes() == "read:org, repo"


def test_mutation_method_rejected():
    sess = FakeSession([FakeResponse(200, {})])
    client = make_client(sess)
    with pytest.raises(ValueError):
        client._request("DELETE", "http://x")


def test_stats_as_dict():
    sess = FakeSession([FakeResponse(200, {})])
    client = make_client(sess)
    client.get("/x")
    d = client.stats.as_dict()
    assert d["rest_calls"] == 1


def test_authorization_header_sent():
    sess = FakeSession([FakeResponse(200, {})])
    client = make_client(sess)
    client.get("/x")
    sent = sess.calls[0]["headers"]
    assert sent["Authorization"] == "Bearer t"
    assert sent["Accept"] == gc.DEFAULT_ACCEPT


def test_no_authorization_when_no_token():
    sess = FakeSession([FakeResponse(200, {})])
    client = GitHubClient(token="", session=sess, sleep=lambda s: None, rng=lambda: 0.5, now=lambda: 1.0)
    client.get("/x")
    assert "Authorization" not in sess.calls[0]["headers"]
