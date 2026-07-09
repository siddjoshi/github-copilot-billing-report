from copilot_aic_report.config import Config
from copilot_aic_report.models import Seat
from copilot_aic_report.sources.seats import fetch_all_seats, fetch_enterprise_seats, fetch_seats


class FakeClient:
    def __init__(self, pages_by_path=None):
        self.pages_by_path = pages_by_path or {}
        self.calls = []

    def paginate(self, path, params=None, *, items_key=None):
        self.calls.append((path, params, items_key))
        yield from self.pages_by_path.get(path, [])


def test_fetch_seats_maps_all_fields():
    raw_seat = {
        "assignee": {"login": "octocat", "id": 42, "type": "User"},
        "created_at": "2026-01-02T03:04:05Z",
        "pending_cancellation_date": "2026-02-01",
        "last_activity_at": "2026-01-10T12:00:00Z",
        "last_authenticated_at": "2026-01-09T12:00:00Z",
        "last_activity_editor": "vscode",
        "assigning_team": {"slug": "copilot-users"},
        "plan_type": "business",
    }
    client = FakeClient(
        {"/orgs/acme/copilot/billing/seats": [raw_seat]}
    )

    seats = fetch_seats(client, Config(per_page=50), "acme")

    assert seats == [
        Seat(
            org_login="acme",
            assignee_login="octocat",
            assignee_id=42,
            assignee_type="User",
            created_at="2026-01-02T03:04:05Z",
            pending_cancellation_date="2026-02-01",
            last_activity_at="2026-01-10T12:00:00Z",
            last_authenticated_at="2026-01-09T12:00:00Z",
            last_activity_editor="vscode",
            assigning_team_slug="copilot-users",
            plan_type="business",
            raw=raw_seat,
        )
    ]
    assert client.calls == [
        ("/orgs/acme/copilot/billing/seats", None, "seats")
    ]


def test_fetch_seats_missing_nested_and_optional_fields_are_none():
    raw_seat = {
        "assignee": None,
        "assigning_team": None,
    }
    client = FakeClient(
        {"/orgs/acme/copilot/billing/seats": [raw_seat]}
    )

    seats = fetch_seats(client, Config(), "acme")

    assert seats == [
        Seat(
            org_login="acme",
            assignee_login=None,
            assignee_id=None,
            assignee_type=None,
            created_at=None,
            pending_cancellation_date=None,
            last_activity_at=None,
            last_authenticated_at=None,
            last_activity_editor=None,
            assigning_team_slug=None,
            plan_type=None,
            raw=raw_seat,
        )
    ]


def test_fetch_all_seats_aggregates_multiple_orgs_in_order():
    acme_seat = {"assignee": {"login": "alice"}, "plan_type": "business"}
    octo_seat = {"assignee": {"login": "bob"}, "plan_type": "enterprise"}
    client = FakeClient(
        {
            "/orgs/acme/copilot/billing/seats": [acme_seat],
            "/orgs/octo/copilot/billing/seats": [octo_seat],
        }
    )

    seats = fetch_all_seats(client, Config(), ["acme", "octo"])

    assert [seat.org_login for seat in seats] == ["acme", "octo"]
    assert [seat.assignee_login for seat in seats] == ["alice", "bob"]
    assert [seat.raw for seat in seats] == [acme_seat, octo_seat]
    assert client.calls == [
        ("/orgs/acme/copilot/billing/seats", None, "seats"),
        ("/orgs/octo/copilot/billing/seats", None, "seats"),
    ]


def test_fetch_seats_empty_result_returns_empty_list():
    client = FakeClient({"/orgs/empty/copilot/billing/seats": []})

    assert fetch_seats(client, Config(), "empty") == []


def test_fetch_all_seats_empty_org_list_returns_empty_list():
    assert fetch_all_seats(FakeClient(), Config(), []) == []


def test_fetch_enterprise_seats_uses_org_attribution():
    seat = {"assignee": {"login": "alice"}, "organization": {"login": "acme"}, "plan_type": "enterprise"}
    client = FakeClient({"/enterprises/ent1/copilot/billing/seats": [seat]})
    seats = fetch_enterprise_seats(client, Config(enterprise_slug="ent1"))
    assert seats[0].org_login == "acme"
    assert seats[0].assignee_login == "alice"


def test_fetch_enterprise_seats_direct_assignment_falls_back_to_enterprise():
    # Enterprise-direct assignment: organization is None -> attribute to enterprise slug,
    # not "" (which previously caused the seat to be dropped downstream).
    seat = {"assignee": {"login": "Hemant_HondaCN"}, "organization": None, "plan_type": "business"}
    client = FakeClient({"/enterprises/hondacn/copilot/billing/seats": [seat]})
    seats = fetch_enterprise_seats(client, Config(enterprise_slug="hondacn"))
    assert seats[0].org_login == "enterprise:hondacn"
    assert seats[0].assignee_login == "Hemant_HondaCN"
