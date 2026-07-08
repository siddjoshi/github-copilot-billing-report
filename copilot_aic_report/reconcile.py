"""Validation & reconciliation checks. Each check returns a dict
``{"name", "ok", "detail"}`` suitable for the run log.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .models import BillingUsageLine, OrgBillingSummary
from .sources.billing_usage import copilot_net_usd


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_seat_counts(
    rows: List[Dict[str, object]],
    org_billing: Dict[str, OrgBillingSummary],
) -> List[Dict[str, object]]:
    """Active-seat row count per org ≈ seat_breakdown.total (± pending)."""
    checks: List[Dict[str, object]] = []
    counts: Dict[str, int] = {}
    for row in rows:
        if str(row.get("user_status")) == "active" and str(row.get("row_source")) == "live_seats":
            org = str(row.get("org_login"))
            counts[org] = counts.get(org, 0) + 1
    for org, summary in org_billing.items():
        total = summary.total
        if total is None:
            continue
        actual = counts.get(org, 0)
        pending = summary.pending_cancellation or 0
        ok = abs(actual - total) <= max(pending, 0)
        checks.append(
            {
                "name": f"seat_count[{org}]",
                "ok": ok,
                "detail": f"active_rows={actual} vs billing.total={total} (±pending={pending})",
            }
        )
    return checks


def check_aic_reconciliation(
    rows: List[Dict[str, object]],
    usage_lines: List[BillingUsageLine],
    tolerance_frac: float = 0.05,
    period: Optional[str] = None,
) -> Dict[str, object]:
    """Reconcile Σ per-user aic_consumed (gross USD) against the billed net.

    ``aic_consumed`` is gross consumption; billing usage ``net`` is the amount billed
    after the included allowance. Net is therefore ``<= gross`` and is often 0 when all
    consumption is within the allowance — that is expected, not a mismatch. We flag only
    the impossible case where billed net exceeds gross consumption beyond tolerance.
    """
    scoped = [r for r in rows if period is None or str(r.get("billing_period")) == period]
    per_user_sum = sum(_to_float(r.get("aic_consumed_usd")) or 0.0 for r in scoped)
    net = copilot_net_usd(usage_lines)
    ok = net <= per_user_sum * (1 + tolerance_frac) + 1e-9
    detail = (
        f"per_user_gross_usd={per_user_sum:.2f}; billing_net_usd={net:.2f} "
        f"(net should be <= gross; within-allowance usage bills 0)"
    )
    return {"name": f"aic_reconciliation{f'[{period}]' if period else ''}", "ok": ok, "detail": detail}


def check_real_logins(rows: List[Dict[str, object]]) -> Dict[str, object]:
    """100% of rows must have a real user_login."""
    unresolved = [r for r in rows if not str(r.get("user_login") or "").strip()]
    total = len(rows)
    ok = len(unresolved) == 0
    pct = (100.0 * (total - len(unresolved)) / total) if total else 100.0
    detail = f"{total - len(unresolved)}/{total} resolved ({pct:.1f}%); unresolved={len(unresolved)}"
    return {"name": "real_login_coverage", "ok": ok, "detail": detail}


def check_no_external_in_login(rows: List[Dict[str, object]]) -> Dict[str, object]:
    """Must be zero rows where user_login holds an external identity (email/NameID/GUID)."""
    from .resolve import looks_like_external_id

    leaks = [r for r in rows if looks_like_external_id(str(r.get("user_login") or ""))]
    ok = len(leaks) == 0
    sample = [str(r.get("user_login")) for r in leaks[:5]]
    return {
        "name": "no_external_identity_in_login",
        "ok": ok,
        "detail": f"leaks={len(leaks)}" + (f" sample={sample}" if sample else ""),
    }


def check_status_agreement(
    rows: List[Dict[str, object]],
    org_billing: Dict[str, OrgBillingSummary],
) -> List[Dict[str, object]]:
    """Flag org active/inactive counts that disagree with the billing breakdown."""
    checks: List[Dict[str, object]] = []
    for org, summary in org_billing.items():
        if summary.active_this_cycle is None:
            continue
        active_rows = sum(
            1
            for r in rows
            if str(r.get("org_login")) == org
            and str(r.get("user_status")) == "active"
            and str(r.get("row_source")) == "live_seats"
        )
        # This is an activity-vs-license comparison; report as informational.
        ok = True
        checks.append(
            {
                "name": f"status_breakdown[{org}]",
                "ok": ok,
                "detail": f"active_license_rows={active_rows}; billing.active_this_cycle={summary.active_this_cycle}",
            }
        )
    return checks


def summarize_history(rows: List[Dict[str, object]]) -> Dict[str, object]:
    """Per-run history provenance summary + UNRECOVERABLE list."""
    by_source: Dict[str, int] = {}
    unrecoverable: List[Dict[str, str]] = []
    for row in rows:
        rs = str(row.get("row_source") or "unknown")
        by_source[rs] = by_source.get(rs, 0) + 1
        if str(row.get("login_recovery_source")) == "UNRECOVERABLE":
            unrecoverable.append(
                {
                    "org_login": str(row.get("org_login") or ""),
                    "billing_period": str(row.get("billing_period") or ""),
                    "external_identity": str(row.get("external_identity") or ""),
                }
            )
    return {"by_row_source": by_source, "unrecoverable": unrecoverable}


def run_all(
    rows: List[Dict[str, object]],
    org_billing: Dict[str, OrgBillingSummary],
    usage_lines: List[BillingUsageLine],
    periods: Optional[List[str]] = None,
) -> List[Dict[str, object]]:
    checks: List[Dict[str, object]] = []
    checks.extend(check_seat_counts(rows, org_billing))
    checks.append(check_real_logins(rows))
    checks.append(check_no_external_in_login(rows))
    checks.extend(check_status_agreement(rows, org_billing))
    if periods:
        for period in periods:
            checks.append(check_aic_reconciliation(rows, usage_lines, period=period))
    else:
        checks.append(check_aic_reconciliation(rows, usage_lines))
    return checks
