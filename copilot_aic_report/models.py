"""Shared intermediate data models produced by source fetchers and consumed by
the row builder / resolver. Kept deliberately simple (plain dataclasses) so that
each source module can be developed and tested independently.

All timestamps are stored as raw ISO-8601 strings (UTC) as returned by GitHub;
normalization to date-only ``YYYY-MM-DD`` happens in the row builder / util layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Seat:
    """A Copilot seat as returned by GET /orgs/{org}/copilot/billing/seats."""

    org_login: str
    assignee_login: Optional[str]
    assignee_id: Optional[int]
    assignee_type: Optional[str]
    created_at: Optional[str]
    pending_cancellation_date: Optional[str]
    last_activity_at: Optional[str]
    last_authenticated_at: Optional[str]
    last_activity_editor: Optional[str]
    assigning_team_slug: Optional[str]
    plan_type: Optional[str]
    raw: Dict = field(default_factory=dict)


@dataclass
class OrgBillingSummary:
    """GET /orgs/{org}/copilot/billing seat_breakdown + plan_type."""

    org_login: str
    plan_type: Optional[str]
    total: Optional[int]
    active_this_cycle: Optional[int]
    inactive_this_cycle: Optional[int]
    pending_cancellation: Optional[int]
    pending_invitation: Optional[int]
    raw: Dict = field(default_factory=dict)


@dataclass
class BillingUsageLine:
    """A single line from the enhanced billing-usage API (product == copilot)."""

    date: Optional[str]
    product: Optional[str]
    sku: Optional[str]
    quantity: Optional[float]
    unit_type: Optional[str]
    gross_amount: Optional[float]
    discount_amount: Optional[float]
    net_amount: Optional[float]
    organization_name: Optional[str]
    repository_name: Optional[str] = None
    raw: Dict = field(default_factory=dict)


@dataclass
class AicConsumption:
    """Per-user AI-credit consumption for the reporting period."""

    user_login: str
    org_login: Optional[str]
    credits_consumed: float
    usd_consumed: Optional[float] = None
    source: str = "unknown"  # "api" | "csv"
    raw: Dict = field(default_factory=dict)


@dataclass
class ExternalIdentity:
    """A SAML/SCIM external identity mapped to a real GitHub login (may be None)."""

    user_login: Optional[str]
    saml_name_id: Optional[str]
    scim_username: Optional[str]
    email: Optional[str]
    scope: str = "enterprise"  # "enterprise" | f"org:{login}"
    scim_active: Optional[bool] = None


@dataclass
class AuditEvent:
    """A copilot.seat_assigned / copilot.seat_cancelled audit-log event."""

    action: str  # "copilot.seat_assigned" | "copilot.seat_cancelled"
    user_login: Optional[str]  # REAL handle of the seat holder
    org_login: Optional[str]
    timestamp_ms: Optional[int]  # @timestamp in epoch millis
    user_id: Optional[int] = None  # numeric GitHub user id of the seat holder
    raw: Dict = field(default_factory=dict)


@dataclass
class AccountState:
    """Membership / account state for a user in an org (or enterprise-wide)."""

    user_login: str
    org_login: Optional[str]
    is_member: bool = False
    suspended: bool = False
    scim_active: Optional[bool] = None
    deprovisioned_at: Optional[str] = None  # SCIM meta.lastModified when inactive

    def state(self) -> str:
        if self.suspended:
            return "suspended"
        if self.scim_active is False:
            return "deprovisioned"
        if self.is_member:
            return "member"
        return "deprovisioned"


@dataclass
class IdentityMapEntry:
    github_login: Optional[str]
    scim_username: Optional[str] = None
    saml_name_id: Optional[str] = None
    email: Optional[str] = None
