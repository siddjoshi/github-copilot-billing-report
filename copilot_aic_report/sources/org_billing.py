"""Fetch organization-level Copilot billing summaries."""
from __future__ import annotations

from copilot_aic_report.github_client import GitHubError
from copilot_aic_report.models import OrgBillingSummary


def fetch_org_billing(client, cfg, org_login) -> OrgBillingSummary:
    """Fetch GET /orgs/{org}/copilot/billing for one organization."""
    try:
        payload = client.get(f"/orgs/{org_login}/copilot/billing")
    except GitHubError as exc:
        if exc.status == 404:
            return OrgBillingSummary(
                org_login=org_login,
                plan_type=None,
                total=None,
                active_this_cycle=None,
                inactive_this_cycle=None,
                pending_cancellation=None,
                pending_invitation=None,
                raw={},
            )
        raise

    seat_breakdown = (payload or {}).get("seat_breakdown") or {}
    return OrgBillingSummary(
        org_login=org_login,
        plan_type=(payload or {}).get("plan_type"),
        total=seat_breakdown.get("total"),
        active_this_cycle=seat_breakdown.get("active_this_cycle"),
        inactive_this_cycle=seat_breakdown.get("inactive_this_cycle"),
        pending_cancellation=seat_breakdown.get("pending_cancellation"),
        pending_invitation=seat_breakdown.get("pending_invitation"),
        raw=payload or {},
    )
