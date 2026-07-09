from copilot_aic_report.config import Config
from copilot_aic_report.github_client import GitHubError
from copilot_aic_report.sources import audit_log


class FakeClient:
    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error
        self.calls = []

    def paginate(self, path, params=None, *, items_key=None):
        self.calls.append((path, params, items_key))
        if self.error:
            raise self.error
        yield from self.events


def test_fetch_events_extracts_login_variants_and_org_values():
    raw_events = [
        {"action": "copilot.seat_assigned", "@timestamp": 10, "user": "alice", "org": "octo"},
        {
            "action": "copilot.seat_assigned",
            "@timestamp": 20,
            "user_login": {"login": "bob"},
            "business": {"login": "biz"},
        },
        {
            "action": "copilot.seat_assigned",
            "@timestamp": 30,
            "userLogin": "carol",
            "organization": {"login": "org-from-raw"},
        },
    ]
    client = FakeClient(raw_events)

    events = audit_log.fetch_events(
        client, Config(enterprise_slug="ent"), "copilot.seat_assigned", org_login="fallback-org"
    )

    assert client.calls == [
        (
            "/orgs/fallback-org/audit-log",
            {"phrase": "action:copilot.seat_assigned"},
            None,
        )
    ]
    assert [event.user_login for event in events] == ["alice", "bob", "carol"]
    assert [event.org_login for event in events] == ["octo", "biz", "org-from-raw"]
    assert [event.timestamp_ms for event in events] == [10, 20, 30]
    assert all(event.raw is raw for event, raw in zip(events, raw_events))


def test_fetch_events_extracts_user_id():
    raw_events = [
        {"action": "copilot.seat_assigned", "@timestamp": 10, "user": "alice", "user_id": 42},
        {"action": "copilot.seat_assigned", "@timestamp": 20, "user": "bob", "actor_id": "77"},
        {"action": "copilot.seat_assigned", "@timestamp": 30, "user": "carol"},
    ]
    events = audit_log.fetch_events(
        FakeClient(raw_events), Config(enterprise_slug="ent"), "copilot.seat_assigned"
    )
    assert [event.user_id for event in events] == [42, 77, None]


def test_fetch_enterprise_and_org_events_request_assigned_and_cancelled():
    client = FakeClient(
        [
            {"action": "copilot.seat_assigned", "@timestamp": 1, "user": "alice"},
            {"action": "copilot.seat_cancelled", "@timestamp": 2, "user": "bob"},
        ]
    )
    cfg = Config(enterprise_slug="my-ent")

    enterprise_events = audit_log.fetch_enterprise_events(client, cfg)
    org_events = audit_log.fetch_org_events(client, cfg, "my-org")

    assert [event.action for event in enterprise_events] == [
        "copilot.seat_assigned",
        "copilot.seat_cancelled",
        "copilot.seat_assigned",
        "copilot.seat_cancelled",
    ]
    assert [event.org_login for event in org_events] == ["my-org"] * 4
    assert client.calls == [
        ("/enterprises/my-ent/audit-log", {"phrase": "action:copilot.seat_assigned"}, None),
        ("/enterprises/my-ent/audit-log", {"phrase": "action:copilot.seat_cancelled"}, None),
        ("/orgs/my-org/audit-log", {"phrase": "action:copilot.seat_assigned"}, None),
        ("/orgs/my-org/audit-log", {"phrase": "action:copilot.seat_cancelled"}, None),
    ]


def test_earliest_assigned_and_latest_cancelled_filter_case_insensitive_and_org():
    events = [
        audit_log.AuditEvent("copilot.seat_assigned", "Alice", "org-a", 300, {}),
        audit_log.AuditEvent("copilot.seat_assigned", "alice", "org-a", 100, {}),
        audit_log.AuditEvent("copilot.seat_assigned", "ALICE", "org-b", 50, {}),
        audit_log.AuditEvent("copilot.seat_cancelled", "alice", "org-a", 200, {}),
        audit_log.AuditEvent("copilot.seat_cancelled", "ALICE", "org-a", 400, {}),
        audit_log.AuditEvent("copilot.seat_cancelled", "alice", "org-b", 500, {}),
        audit_log.AuditEvent("copilot.seat_assigned", "alice", "org-a", None, {}),
        audit_log.AuditEvent("copilot.seat_cancelled", "alice", "org-a", None, {}),
    ]

    assert audit_log.earliest_assigned(events, "alice") == 50
    assert audit_log.earliest_assigned(events, "ALICE", "ORG-A") == 100
    assert audit_log.latest_cancelled(events, "Alice") == 500
    assert audit_log.latest_cancelled(events, "alice", "org-a") == 400
    assert audit_log.earliest_assigned(events, "nobody") is None
    assert audit_log.latest_cancelled(events, "alice", "missing-org") is None


def test_fetch_events_returns_empty_for_404_and_writes_stderr(capsys):
    client = FakeClient(error=GitHubError("not found", status=404))

    events = audit_log.fetch_events(client, Config(enterprise_slug="ent"), "copilot.seat_assigned")

    assert events == []
    assert "audit-log unavailable" in capsys.readouterr().err
