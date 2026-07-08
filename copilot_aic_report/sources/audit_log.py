"""Copilot seat assignment/cancellation audit-log source."""
from __future__ import annotations

import sys
from typing import Any, Optional

from copilot_aic_report.config import Config
from copilot_aic_report.github_client import AuthFailure, GitHubError
from copilot_aic_report.models import AuditEvent

SEAT_ASSIGNED = "copilot.seat_assigned"
SEAT_CANCELLED = "copilot.seat_cancelled"
SEAT_ACTIONS = (SEAT_ASSIGNED, SEAT_CANCELLED)


def fetch_events(client, cfg: Config, action: str, org_login: Optional[str] = None) -> list[AuditEvent]:
    """Fetch audit-log events for one Copilot seat action."""
    path = (
        f"/orgs/{org_login}/audit-log"
        if org_login
        else f"/enterprises/{cfg.enterprise_slug}/audit-log"
    )
    try:
        raw_events = client.paginate(path, params={"phrase": f"action:{action}"})
        return [_to_audit_event(raw, org_login) for raw in raw_events]
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
    """Fetch enterprise-level Copilot seat assignment and cancellation events."""
    events: list[AuditEvent] = []
    for action in SEAT_ACTIONS:
        events.extend(fetch_events(client, cfg, action))
    return events


def fetch_org_events(client, cfg: Config, org_login: str) -> list[AuditEvent]:
    """Fetch org-level Copilot seat assignment and cancellation events."""
    events: list[AuditEvent] = []
    for action in SEAT_ACTIONS:
        events.extend(fetch_events(client, cfg, action, org_login=org_login))
    return events


def earliest_assigned(
    events: list[AuditEvent], login: str, org_login: Optional[str] = None
) -> Optional[int]:
    """Return the earliest matching seat assignment timestamp in epoch millis."""
    timestamps = [
        event.timestamp_ms
        for event in events
        if _matches(event, login, org_login, "seat_assigned") and event.timestamp_ms is not None
    ]
    return min(timestamps) if timestamps else None


def latest_cancelled(
    events: list[AuditEvent], login: str, org_login: Optional[str] = None
) -> Optional[int]:
    """Return the latest matching seat cancellation timestamp in epoch millis."""
    timestamps = [
        event.timestamp_ms
        for event in events
        if _matches(event, login, org_login, "seat_cancelled") and event.timestamp_ms is not None
    ]
    return max(timestamps) if timestamps else None


def _to_audit_event(raw: Any, org_login: Optional[str]) -> AuditEvent:
    event = raw if isinstance(raw, dict) else {}
    return AuditEvent(
        action=event.get("action"),
        user_login=_extract_login(event),
        org_login=_extract_org_login(event, org_login),
        timestamp_ms=event.get("@timestamp"),
        raw=event,
    )


def _extract_login(event: dict[str, Any]) -> Optional[str]:
    for key in ("user", "user_login", "userLogin"):
        if key in event:
            return _login_value(event.get(key))
    return None


def _extract_org_login(event: dict[str, Any], fallback: Optional[str]) -> Optional[str]:
    for key in ("org", "business", "organization"):
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
    event: AuditEvent, login: str, org_login: Optional[str], action_suffix: str
) -> bool:
    if not (event.action or "").endswith(action_suffix):
        return False
    if (event.user_login or "").casefold() != login.casefold():
        return False
    if org_login is None:
        return True
    return (event.org_login or "").casefold() == org_login.casefold()

