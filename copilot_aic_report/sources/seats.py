"""Copilot billing seat source."""
from __future__ import annotations

from copilot_aic_report.models import Seat


def fetch_seats(client, cfg, org_login) -> list[Seat]:
    """Fetch Copilot billing seats for one organization."""
    seats: list[Seat] = []
    path = f"/orgs/{org_login}/copilot/billing/seats"

    for raw_seat in client.paginate(path, items_key="seats"):
        assignee = raw_seat.get("assignee") or {}
        assigning_team = raw_seat.get("assigning_team") or {}

        seats.append(
            Seat(
                org_login=org_login,
                assignee_login=assignee.get("login"),
                assignee_id=assignee.get("id"),
                assignee_type=assignee.get("type"),
                created_at=raw_seat.get("created_at"),
                pending_cancellation_date=raw_seat.get("pending_cancellation_date"),
                last_activity_at=raw_seat.get("last_activity_at"),
                last_authenticated_at=raw_seat.get("last_authenticated_at"),
                last_activity_editor=raw_seat.get("last_activity_editor"),
                assigning_team_slug=assigning_team.get("slug"),
                plan_type=raw_seat.get("plan_type"),
                raw=raw_seat,
            )
        )

    return seats


def fetch_all_seats(client, cfg, org_logins) -> list[Seat]:
    """Fetch and concatenate Copilot billing seats for multiple organizations."""
    seats: list[Seat] = []
    for org_login in org_logins:
        seats.extend(fetch_seats(client, cfg, org_login))
    return seats
