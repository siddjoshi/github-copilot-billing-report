"""Seat ledger: longitudinal reconstruction of Copilot seat state per user/org.

Merges (widest history first) audit-archive events, audit-log API events, stored
snapshots and the current live seats into ordered assign→revoke intervals per
``(user_login, org_login)``. Each requested month is then materialized:

  * A user is "licensed in month M" if any interval overlaps M's billing cycle.
  * ``license_assigned_date`` = interval start; ``user_revoked_date`` = interval end
    within/at M (empty if still open).
  * Real login is captured from the event/seat/snapshot at ingest time, so it
    survives later removal. Unresolvable holders are flagged UNRECOVERABLE and never
    leak an external identity into ``user_login``.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import AuditEvent, Seat
from .periods import cycle_bounds_utc, interval_overlaps_period
from .resolve import IdentityResolver, canonicalize_login, extract_guid, is_placeholder_login
from .util import epoch_ms_to_utc_datetime, to_utc_datetime

ASSIGN_SUFFIXES = ("seat_assigned", "seat_refresh")
CANCEL_SUFFIX = "seat_cancelled"


@dataclass
class Interval:
    assigned_at: Optional[_dt.datetime]
    revoked_at: Optional[_dt.datetime]
    origin: str  # audit | live_seat | snapshot
    pending_cancellation_at: Optional[_dt.datetime] = None
    seat: Optional[Seat] = None
    snapshot_record: Optional[dict] = None


@dataclass
class MaterializedSeat:
    user_login: Optional[str]
    org_login: str
    billing_period: str
    license_assigned_date: str
    user_revoked_date: str
    user_status: str  # active | inactive
    seat_status: str  # active | pending_cancellation | removed
    row_source: str  # live_seats | audit_reconstructed | snapshot
    login_recovery_source: str  # seat | audit_log | snapshot | external_identity | identity_map | UNRECOVERABLE
    history_confidence: str  # exact | reconstructed | aggregate_only | unknown
    as_of_utc: str
    external_identity: Optional[str] = None
    plan_type: Optional[str] = None
    last_activity_at: Optional[str] = None
    assigned_via: Optional[str] = None
    seat: Optional[Seat] = None
    snapshot_record: Optional[dict] = None
    suspended: bool = False
    notes: List[str] = field(default_factory=list)


@dataclass
class _Holder:
    """Accumulated events for one (login, org) key."""

    login: Optional[str]
    org: str
    external_identity: Optional[str] = None
    login_source: str = "UNRECOVERABLE"
    assigns: List[_dt.datetime] = field(default_factory=list)
    cancels: List[_dt.datetime] = field(default_factory=list)
    live_seat: Optional[Seat] = None
    suspended: bool = False


class SeatLedger:
    def __init__(self, resolver: Optional[IdentityResolver] = None):
        self.resolver = resolver or IdentityResolver()
        self._holders: Dict[Tuple[str, str], _Holder] = {}
        # snapshot records keyed by period -> list of records
        self.snapshots: Dict[str, List[dict]] = {}

    # -- key helpers ------------------------------------------------------

    def _key(self, login: Optional[str], org: str, external_id: Optional[str]) -> Tuple[str, str]:
        ident = (canonicalize_login(login) or ("ext:" + (external_id or "unknown"))).lower()
        return (ident, org)

    def _holder(self, login: Optional[str], org: str, external_id: Optional[str], source: str) -> _Holder:
        key = self._key(login, org, external_id)
        holder = self._holders.get(key)
        if holder is None:
            resolved_login = canonicalize_login(login)
            holder = _Holder(
                login=resolved_login,
                org=org,
                external_identity=external_id,
                login_source=source if resolved_login else "UNRECOVERABLE",
            )
            self._holders[key] = holder
        else:
            # Upgrade login if we now have a real one and previously did not.
            if not holder.login and login:
                holder.login = canonicalize_login(login)
                holder.login_source = source
            if not holder.external_identity and external_id:
                holder.external_identity = external_id
        return holder

    # -- ingestion --------------------------------------------------------

    def add_audit_event(self, event: AuditEvent) -> None:
        if not event.action or not event.org_login:
            return
        ts = epoch_ms_to_utc_datetime(event.timestamp_ms)
        if ts is None:
            return
        login = event.user_login
        # Resolve external id -> real login if the event lacks a login.
        external_id = None
        if not login:
            return  # audit events without any subject are unusable
        holder = self._holder(login, event.org_login, external_id, source="audit_log")
        if event.action.endswith(ASSIGN_SUFFIXES):
            holder.assigns.append(ts)
        elif event.action.endswith(CANCEL_SUFFIX):
            holder.cancels.append(ts)

    def add_live_seat(self, seat: Seat) -> None:
        if not seat.org_login:
            return
        login = seat.assignee_login
        if login and is_placeholder_login(login):
            # Suspended/deprovisioned EMU account: the seat carries a GUID placeholder
            # instead of a real handle. Resolve the real login via externalIdentities /
            # identity_map so audit and AIC data (keyed by real login) can merge; never
            # treat the GUID as a real login.
            resolution = self.resolver.resolve(external_id=login)
            if not resolution.user_login:
                core = extract_guid(login)
                if core and core != login:
                    resolution = self.resolver.resolve(external_id=core)
            holder = self._holder(
                resolution.user_login,
                seat.org_login,
                external_id=login,
                source=resolution.source if resolution.user_login else "seat",
            )
            holder.suspended = True
            if not holder.external_identity:
                holder.external_identity = login
        else:
            holder = self._holder(login, seat.org_login, None, source="seat")
        holder.live_seat = seat
        assigned = to_utc_datetime(seat.created_at)
        if assigned is not None:
            holder.assigns.append(assigned)

    def add_snapshot(self, period: str, records: List[dict]) -> None:
        self.snapshots[period] = list(records or [])

    def live_seat_holders(self) -> List[tuple]:
        """Return ``(org, login)`` pairs for current live seats with a real login.

        Used to query per-user AIC consumption by the resolved GitHub login (never
        a GUID placeholder).
        """
        out: List[tuple] = []
        for holder in self._holders.values():
            if holder.live_seat is not None and holder.login:
                out.append((holder.org, holder.login))
        return out

    # -- interval construction -------------------------------------------

    def _intervals_for(self, holder: _Holder) -> List[Interval]:
        """Pair assigns/cancels chronologically into intervals."""
        assigns = sorted(holder.assigns)
        cancels = sorted(holder.cancels)
        intervals: List[Interval] = []
        ci = 0
        for idx, start in enumerate(assigns):
            # Determine the next assign to bound this interval.
            next_start = assigns[idx + 1] if idx + 1 < len(assigns) else None
            # Find the first cancel at/after start and before next assign.
            end: Optional[_dt.datetime] = None
            while ci < len(cancels) and cancels[ci] < start:
                ci += 1
            if ci < len(cancels) and (next_start is None or cancels[ci] < next_start):
                end = cancels[ci]
                ci += 1
            origin = "live_seat" if (holder.live_seat is not None and next_start is None) else "audit"
            pending = None
            seat = holder.live_seat if origin == "live_seat" else None
            if seat is not None and seat.pending_cancellation_date:
                pending = to_utc_datetime(seat.pending_cancellation_date)
                if end is None:
                    end = pending
            intervals.append(
                Interval(
                    assigned_at=start,
                    revoked_at=end,
                    origin=origin,
                    pending_cancellation_at=pending,
                    seat=seat,
                )
            )
        # Cancel with no preceding assign (assignment predates history window).
        if not assigns and cancels:
            intervals.append(
                Interval(assigned_at=None, revoked_at=max(cancels), origin="audit")
            )
        return intervals

    # -- materialization --------------------------------------------------

    def materialize_month(self, period: str, now_iso: str) -> List[MaterializedSeat]:
        """Materialize all seats licensed in ``period``.

        Snapshot for the month (if present) is authoritative ("exact"); otherwise
        reconstruct from intervals.
        """
        if period in self.snapshots:
            return [
                _from_snapshot_record(rec, period, now_iso) for rec in self.snapshots[period]
            ]

        cycle_start, cycle_end = cycle_bounds_utc(period)
        out: List[MaterializedSeat] = []
        for holder in self._holders.values():
            intervals = self._intervals_for(holder)
            for interval in intervals:
                if not interval_overlaps_period(interval.assigned_at, interval.revoked_at, period):
                    continue
                out.append(self._materialize(holder, interval, period, cycle_start, cycle_end, now_iso))
        return out

    def _materialize(
        self,
        holder: _Holder,
        interval: Interval,
        period: str,
        cycle_start: _dt.datetime,
        cycle_end: _dt.datetime,
        now_iso: str,
    ) -> MaterializedSeat:
        seat = interval.seat
        # Revoked within/at this month?
        revoked_date = ""
        seat_status = "active"
        user_status = "active"
        notes: List[str] = []
        if interval.revoked_at is not None and interval.revoked_at < cycle_end:
            revoked_date = interval.revoked_at.date().isoformat()
            if interval.pending_cancellation_at is not None and interval.revoked_at >= cycle_start:
                seat_status = "pending_cancellation"
                user_status = "active"  # still holds seat until end-of-cycle
            else:
                seat_status = "removed"
                user_status = "inactive"
        elif interval.pending_cancellation_at is not None:
            seat_status = "pending_cancellation"

        if interval.origin == "live_seat":
            row_source = "live_seats"
            history_confidence = "exact"
        else:
            row_source = "audit_reconstructed"
            history_confidence = "reconstructed"

        login = holder.login
        login_source = holder.login_source
        # Preserve the external identity (e.g. suspended EMU GUID) even when a real
        # login was recovered, so it is visible in the external_identity column.
        external_identity = holder.external_identity
        if not login:
            login_source = "UNRECOVERABLE"
            notes.append("login unrecoverable; external identity retained separately")

        # Suspended/deprovisioned account -> inactive regardless of seat state.
        if holder.suspended:
            user_status = "inactive"
            notes.append("account suspended/deprovisioned (GUID placeholder login)")

        assigned_date = interval.assigned_at.date().isoformat() if interval.assigned_at else ""
        if interval.assigned_at is None:
            notes.append("assignment predates available history window")
            history_confidence = "reconstructed"

        return MaterializedSeat(
            user_login=login,
            org_login=holder.org,
            billing_period=period,
            license_assigned_date=assigned_date,
            user_revoked_date=revoked_date,
            user_status=user_status,
            seat_status=seat_status,
            row_source=row_source,
            login_recovery_source=login_source,
            history_confidence=history_confidence,
            as_of_utc=now_iso,
            external_identity=external_identity,
            plan_type=seat.plan_type if seat else None,
            last_activity_at=seat.last_activity_at if seat else None,
            assigned_via=(f"team:{seat.assigning_team_slug}" if seat and seat.assigning_team_slug else ("direct" if seat else None)),
            seat=seat,
            suspended=holder.suspended,
            notes=notes,
        )


def _from_snapshot_record(rec: dict, period: str, now_iso: str) -> MaterializedSeat:
    return MaterializedSeat(
        user_login=rec.get("user_login"),
        org_login=rec.get("org_login", ""),
        billing_period=period,
        license_assigned_date=rec.get("license_assigned_date", ""),
        user_revoked_date=rec.get("user_revoked_date", ""),
        user_status=rec.get("user_status", ""),
        seat_status=rec.get("seat_status", ""),
        row_source="snapshot",
        login_recovery_source=rec.get("login_recovery_source", "snapshot"),
        history_confidence="exact",
        as_of_utc=rec.get("as_of_utc", now_iso),
        external_identity=rec.get("external_identity"),
        plan_type=rec.get("plan_type"),
        last_activity_at=rec.get("last_activity_at"),
        assigned_via=rec.get("assigned_via"),
        snapshot_record=rec,
        notes=list(rec.get("notes", []) or []),
    )
