"""Copilot billing seat source.

Two data paths:
* :func:`fetch_enterprise_seats` — one paginated enterprise-wide call
  (``GET /enterprises/{ent}/copilot/billing/seats``). Preferred: it returns every
  seat across all orgs (with per-seat ``organization`` attribution) and is not
  subject to the per-org classic-PAT access restrictions.
* :func:`fetch_seats` — per-org fallback (``GET /orgs/{org}/copilot/billing/seats``)
  for when the enterprise endpoint is unavailable.
"""
from __future__ import annotations

from copilot_aic_report.models import Seat


def _seat_from_raw(raw_seat: dict, org_login: str) -> Seat:
    assignee = raw_seat.get("assignee") or {}
    assigning_team = raw_seat.get("assigning_team") or {}
    return Seat(
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


def _org_login_of(raw_seat: dict, fallback: str = "") -> str:
    org = raw_seat.get("organization")
    if isinstance(org, dict):
        return org.get("login") or org.get("name") or fallback
    if isinstance(org, str) and org:
        return org
    return fallback


def fetch_enterprise_seats(client, cfg) -> list[Seat]:
    """Fetch every Copilot seat across the enterprise in one paginated call.

    Each seat carries an ``organization`` field used for per-instance attribution.
    """
    path = f"/enterprises/{cfg.enterprise_slug}/copilot/billing/seats"
    seats: list[Seat] = []
    for raw_seat in client.paginate(path, items_key="seats"):
        seats.append(_seat_from_raw(raw_seat, _org_login_of(raw_seat)))
    return seats


def fetch_seats(client, cfg, org_login) -> list[Seat]:
    """Fetch Copilot billing seats for one organization."""
    path = f"/orgs/{org_login}/copilot/billing/seats"
    return [_seat_from_raw(raw_seat, org_login) for raw_seat in client.paginate(path, items_key="seats")]


def fetch_all_seats(client, cfg, org_logins) -> list[Seat]:
    """Fetch and concatenate Copilot billing seats for multiple organizations."""
    seats: list[Seat] = []
    for org_login in org_logins:
        seats.extend(fetch_seats(client, cfg, org_login))
    return seats
