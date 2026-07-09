"""Copilot seat assignment/cancellation audit-log source."""
from __future__ import annotations

import sys
from typing import Any, Optional

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import AuthFailure, GitHubError
from copilot_aic_report.models import AuditEvent

# GitHub emits Copilot seat lifecycle events under several action names depending on
# the assignment surface and era. Both the modern ``cfb_`` (Copilot for Business)
# names and the legacy short names must be handled.
SEAT_ASSIGNED_ACTIONS = (
    "copilot.cfb_seat_added",
    "copilot.cfb_seat_assignment_created",
    "copilot.seat_assigned",  # legacy
    "copilot.seat_refresh",   # legacy renewal
)
SEAT_CANCELLED_ACTIONS = (
    "copilot.cfb_seat_cancelled",
    "copilot.cfb_seat_assignment_unassigned",
    "copilot.access_revoked",
    "copilot.seat_cancelled",  # legacy
)
SEAT_ACTIONS = SEAT_ASSIGNED_ACTIONS + SEAT_CANCELLED_ACTIONS

# Backward-compatible single-name aliases (kept for legacy callers/tests).
SEAT_ASSIGNED = "copilot.seat_assigned"
SEAT_CANCELLED = "copilot.seat_cancelled"

# All Copilot seat events share this action prefix; a single umbrella query fetches
# every seat event in one paginated pass (far fewer calls than per-action queries)
# and is resilient to new ``copilot.*`` seat action names.
_COPILOT_PREFIX = "copilot"


def is_seat_assigned(action: Optional[str]) -> bool:
    return bool(action) and action in SEAT_ASSIGNED_ACTIONS


def is_seat_cancelled(action: Optional[str]) -> bool:
    return bool(action) and action in SEAT_CANCELLED_ACTIONS


def is_seat_action(action: Optional[str]) -> bool:
    return is_seat_assigned(action) or is_seat_cancelled(action)


def fetch_events(client, cfg: Config, action: str, org_login: Optional[str] = None) -> list[AuditEvent]:
    """Fetch audit-log events for one Copilot audit ``action`` phrase.

    Enterprise-level seats are attributed to ``enterprise:{slug}`` (matching the seat
    source's fallback) so audit-reconstructed rows key-merge with live seats; org-level
    events are attributed to the org.
    """
    path = (
        f"/orgs/{org_login}/audit-log"
        if org_login
        else f"/enterprises/{cfg.enterprise_slug}/audit-log"
    )
    fallback_org = org_login or f"enterprise:{cfg.enterprise_slug}"
    try:
        raw_events = client.paginate(path, params={"phrase": f"action:{action}"})
        return [_to_audit_event(raw, fallback_org) for raw in raw_events]
    except AuthFailure:
        raise
    except GitHubError as exc:
        if exc.status in (403, 404):
            print(
                f"audit-log unavailable for {path} action:{action} ({exc.status}); skipping",
                file=sys.stderr,
            )
            return []
        raise


def fetch_enterprise_events(client, cfg: Config) -> list[AuditEvent]:
    """Fetch enterprise-level Copilot seat assignment and cancellation events.

    Uses a single ``action:copilot`` umbrella query and filters to seat lifecycle
    actions client-side, covering both ``cfb_`` and legacy action names.
    """
    events = fetch_events(client, cfg, _COPILOT_PREFIX)
    return [e for e in events if is_seat_action(e.action)]


def fetch_org_events(client, cfg: Config, org_login: str) -> list[AuditEvent]:
    """Fetch org-level Copilot seat assignment and cancellation events."""
    events = fetch_events(client, cfg, _COPILOT_PREFIX, org_login=org_login)
    return [e for e in events if is_seat_action(e.action)]


def earliest_assigned(
    events: list[AuditEvent], login: str, org_login: Optional[str] = None
) -> Optional[int]:
    """Return the earliest matching seat assignment timestamp in epoch millis."""
    timestamps = [
        event.timestamp_ms
        for event in events
        if _matches(event, login, org_login, "assigned") and event.timestamp_ms is not None
    ]
    return min(timestamps) if timestamps else None


def latest_cancelled(
    events: list[AuditEvent], login: str, org_login: Optional[str] = None
) -> Optional[int]:
    """Return the latest matching seat cancellation timestamp in epoch millis."""
    timestamps = [
        event.timestamp_ms
        for event in events
        if _matches(event, login, org_login, "cancelled") and event.timestamp_ms is not None
    ]
    return max(timestamps) if timestamps else None


def _to_audit_event(raw: Any, org_login: Optional[str]) -> AuditEvent:
    event = raw if isinstance(raw, dict) else {}
    return AuditEvent(
        action=event.get("action"),
        user_login=_extract_login(event),
        org_login=_extract_org_login(event, org_login),
        timestamp_ms=event.get("@timestamp"),
        user_id=_extract_user_id(event),
        raw=event,
    )


def _extract_user_id(event: dict[str, Any]) -> Optional[int]:
    for key in ("user_id", "userId", "actor_id", "actorId"):
        if key in event:
            value = event.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _extract_login(event: dict[str, Any]) -> Optional[str]:
    for key in ("user", "user_login", "userLogin"):
        if key in event:
            return _login_value(event.get(key))
    return None


def _extract_org_login(event: dict[str, Any], fallback: Optional[str]) -> Optional[str]:
    # Only a real *organization* attributes the seat to an org. ``business`` denotes
    # the enterprise (an enterprise-direct seat), which must map to the same
    # ``enterprise:{slug}`` fallback the seat source uses so rows key-merge.
    for key in ("org", "organization"):
        if key in event:
            login = _login_value(event.get(key))
            if login is not None:
                return login
    return fallback


def _login_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("login")
    if value is None:
        return None
    return str(value)


def _matches(
    event: AuditEvent, login: str, org_login: Optional[str], kind: str
) -> bool:
    action = event.action
    if kind == "assigned" and not is_seat_assigned(action):
        return False
    if kind == "cancelled" and not is_seat_cancelled(action):
        return False
    if (event.user_login or "").casefold() != login.casefold():
        return False
    if org_login is None:
        return True
    return (event.org_login or "").casefold() == org_login.casefold()

