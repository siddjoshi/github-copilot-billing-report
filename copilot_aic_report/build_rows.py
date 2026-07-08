"""Merge materialized seats + billing/usage/AIC/account data into CSV row dicts.

Produces one row per (user_login, org, billing_period) following the required
column derivations, plus the recommended and provenance columns. Also computes the
optional per-user rollup.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .config import Config, normalize_plan
from .ledger import MaterializedSeat
from .models import AccountState, AicConsumption
from .util import fmt_money, fmt_num, now_utc_iso

# login_recovery_source -> identity_resolution_source (base column) mapping.
_RESOLUTION_MAP = {
    "seat": "seat",
    "audit_log": "audit",
    "external_identity": "externalIdentities",
    "identity_map": "identity_map",
    "snapshot": "snapshot",
    "UNRECOVERABLE": "unresolved",
}


def _aic_key(login: Optional[str], org: Optional[str]) -> Tuple[str, Optional[str]]:
    return ((login or "").lower(), org)


def index_consumption(rows: List[AicConsumption]) -> Dict[Tuple[str, Optional[str]], AicConsumption]:
    """Index consumption by (login.lower(), org) and (login.lower(), None)."""
    index: Dict[Tuple[str, Optional[str]], AicConsumption] = {}
    for row in rows:
        index[_aic_key(row.user_login, row.org_login)] = row
        # Also index org-agnostic so a seat can match when the export lacks org.
        key = _aic_key(row.user_login, None)
        if key not in index:
            index[key] = row
    return index


def _lookup_consumption(
    index: Dict[Tuple[str, Optional[str]], AicConsumption],
    login: Optional[str],
    org: Optional[str],
) -> Optional[AicConsumption]:
    if not login:
        return None
    return index.get(_aic_key(login, org)) or index.get(_aic_key(login, None))


def build_rows(
    materialized: List[MaterializedSeat],
    cfg: Config,
    *,
    consumption_index: Optional[Dict[Tuple[str, Optional[str]], AicConsumption]] = None,
    account_states: Optional[Dict[Tuple[str, str], AccountState]] = None,
    org_plan_by_org: Optional[Dict[str, str]] = None,
    per_user_has_consumption: bool = True,
    generated_at: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Build final CSV row dicts from materialized seats and side data."""
    consumption_index = consumption_index or {}
    account_states = account_states or {}
    org_plan_by_org = org_plan_by_org or {}
    generated_at = generated_at or now_utc_iso()
    rate = cfg.credit_to_usd

    rows: List[Dict[str, object]] = []
    seen: set = set()
    # AIC consumption is enterprise-wide per user; assign it to only the first
    # (login, period) row so multi-org users are not double-counted.
    consumed_assigned: set = set()
    for seat in materialized:
        login = seat.user_login
        org = seat.org_login
        period = seat.billing_period

        # De-duplicate the same (login, org, period) — keep distinct across orgs.
        dedup_key = ((login or seat.external_identity or "").lower(), org, period)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        plan_raw = seat.plan_type or org_plan_by_org.get(org)
        plan = normalize_plan(plan_raw)

        notes: List[str] = list(seat.notes)
        history_confidence = seat.history_confidence

        # License cost (negotiated/config per-seat; actual org totals live in run log).
        license_cost = cfg.license_cost(plan)

        # Default AIC allowance (date-aware).
        default_credits = cfg.default_aic_credits(plan, cfg.period_date() if period == cfg.resolve_billing_period() else _period_first_day(period))
        default_usd = default_credits * rate

        # AIC assigned (USD): per-user budget if configured, else plan default * rate.
        budget = cfg.per_user_aic_budget_usd.get(login) if login else None
        if budget is not None:
            aic_assigned_usd = float(budget)
            rule = "per_user_budget"
        else:
            aic_assigned_usd = default_usd
            rule = "plan_default"

        # AIC consumed (per-user, enterprise-wide). Attribute to the first org row
        # for this (login, period) to avoid double-counting across a user's orgs.
        consumed = _lookup_consumption(consumption_index, login, org)
        consume_key = ((login or "").lower(), period)
        already_assigned = consume_key in consumed_assigned
        if consumed is not None and not already_assigned:
            consumed_assigned.add(consume_key)
            aic_consumed_credits: Optional[float] = consumed.credits_consumed
            aic_consumed_usd: Optional[float] = (
                consumed.usd_consumed
                if consumed.usd_consumed is not None
                else consumed.credits_consumed * rate
            )
        elif consumed is not None and already_assigned:
            aic_consumed_credits = 0.0
            aic_consumed_usd = 0.0
            notes.append("AIC consumption attributed to another org row for this user")
        elif per_user_has_consumption:
            aic_consumed_credits = 0.0
            aic_consumed_usd = 0.0
        else:
            aic_consumed_credits = None
            aic_consumed_usd = None
            if history_confidence == "exact":
                history_confidence = "aggregate_only"
            notes.append("per-user AIC consumption unavailable for this month")

        # Account state.
        acct = account_states.get((org, (login or "").lower())) if login else None
        account_state = acct.state() if acct else ("member" if seat.seat_status == "active" else "")
        user_status = seat.user_status
        if acct and (acct.suspended or acct.scim_active is False):
            user_status = "inactive"
            if "deprovisioned/suspended account -> inactive" not in notes:
                notes.append("deprovisioned/suspended account -> inactive")
        # A GUID-placeholder seat is an authoritative suspension signal even when
        # SCIM/membership data is unavailable.
        if getattr(seat, "suspended", False):
            user_status = "inactive"
            account_state = "suspended"

        identity_resolution_source = _RESOLUTION_MAP.get(
            seat.login_recovery_source, seat.login_recovery_source
        )
        if seat.login_recovery_source == "UNRECOVERABLE":
            notes.append("UNRECOVERABLE login; external identity in external_identity only")

        row = {
            # Required (order enforced by csv_writer)
            "user_login": login or "",
            "license_assigned_date": seat.license_assigned_date,
            "gh_copilot_license_cost": fmt_money(license_cost),
            "default_aic_user_level": fmt_num(default_credits),
            "aic_billing_dollar_assigned": fmt_money(aic_assigned_usd),
            "aic_consumed": fmt_num(aic_consumed_credits),
            "user_status": user_status,
            "user_revoked_date": seat.user_revoked_date,
            # Recommended / provenance
            "org_login": org,
            "plan_type": plan,
            "seat_status": seat.seat_status,
            "assigned_via": seat.assigned_via or "",
            "last_activity_at": seat.last_activity_at or "",
            "external_identity": seat.external_identity or "",
            "identity_resolution_source": identity_resolution_source,
            "account_state": account_state,
            "aic_assigned_rule_used": rule,
            "default_aic_usd": fmt_money(default_usd),
            "aic_consumed_usd": fmt_money(aic_consumed_usd),
            "currency": cfg.currency,
            "billing_period": period,
            "row_source": seat.row_source,
            "login_recovery_source": seat.login_recovery_source,
            "history_confidence": history_confidence,
            "as_of_utc": seat.as_of_utc,
            "data_quality_notes": "; ".join(notes),
            "data_generated_at_utc": generated_at,
        }
        rows.append(row)
    return rows


def _period_first_day(period: str):
    import datetime as _dt

    y, m = (int(x) for x in period.split("-"))
    return _dt.date(y, m, 1)


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_rollup(rows: List[Dict[str, object]], cfg: Config, generated_at: Optional[str] = None) -> List[Dict[str, object]]:
    """Aggregate per-user rollup across orgs/periods."""
    generated_at = generated_at or now_utc_iso()
    by_user: Dict[str, Dict[str, object]] = {}
    for row in rows:
        login = str(row.get("user_login") or "")
        if not login:
            login = f"[unresolved]{row.get('external_identity','')}"
        agg = by_user.setdefault(
            login,
            {
                "user_login": row.get("user_login") or "",
                "earliest_license_assigned_date": "",
                "any_active": "no",
                "orgs": set(),
                "total_gh_copilot_license_cost": 0.0,
                "total_aic_billing_dollar_assigned": 0.0,
                "total_aic_consumed": 0.0,
                "total_aic_consumed_usd": 0.0,
                "latest_user_revoked_date": "",
                "identity_resolution_source": row.get("identity_resolution_source", ""),
                "currency": cfg.currency,
                "billing_period": "",
                "notes": set(),
                "data_generated_at_utc": generated_at,
            },
        )
        agg["orgs"].add(str(row.get("org_login") or ""))
        assigned = str(row.get("license_assigned_date") or "")
        if assigned and (not agg["earliest_license_assigned_date"] or assigned < agg["earliest_license_assigned_date"]):
            agg["earliest_license_assigned_date"] = assigned
        revoked = str(row.get("user_revoked_date") or "")
        if revoked and revoked > str(agg["latest_user_revoked_date"]):
            agg["latest_user_revoked_date"] = revoked
        if str(row.get("user_status")) == "active":
            agg["any_active"] = "yes"
        for src, dst in (
            ("gh_copilot_license_cost", "total_gh_copilot_license_cost"),
            ("aic_billing_dollar_assigned", "total_aic_billing_dollar_assigned"),
            ("aic_consumed", "total_aic_consumed"),
            ("aic_consumed_usd", "total_aic_consumed_usd"),
        ):
            val = _to_float(row.get(src))
            if val is not None:
                agg[dst] = float(agg[dst]) + val
        if row.get("data_quality_notes"):
            agg["notes"].add(str(row.get("data_quality_notes")))

    out: List[Dict[str, object]] = []
    for agg in by_user.values():
        out.append(
            {
                "user_login": agg["user_login"],
                "earliest_license_assigned_date": agg["earliest_license_assigned_date"],
                "any_active": agg["any_active"],
                "user_status": "active" if agg["any_active"] == "yes" else "inactive",
                "orgs": ",".join(sorted(o for o in agg["orgs"] if o)),
                "total_gh_copilot_license_cost": fmt_money(agg["total_gh_copilot_license_cost"]),
                "total_aic_billing_dollar_assigned": fmt_money(agg["total_aic_billing_dollar_assigned"]),
                "total_aic_consumed": fmt_num(agg["total_aic_consumed"]),
                "total_aic_consumed_usd": fmt_money(agg["total_aic_consumed_usd"]),
                "latest_user_revoked_date": agg["latest_user_revoked_date"],
                "identity_resolution_source": agg["identity_resolution_source"],
                "currency": agg["currency"],
                "billing_period": agg["billing_period"],
                "data_quality_notes": "; ".join(sorted(agg["notes"])),
                "data_generated_at_utc": agg["data_generated_at_utc"],
            }
        )
    return out
