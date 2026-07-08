"""CLI orchestrator for the Copilot license + AIC CSV report (read-only).

Pipeline: config -> auth preflight -> discover orgs -> fetch sources ->
build seat ledger -> materialize each report month -> merge into rows ->
reconcile -> write CSV (+ optional rollup) + run log + monthly snapshots.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Optional, Tuple

from . import __version__
from .auth import AuthError, preflight
from .build_rows import build_rollup, build_rows, index_consumption
from .config import Config, load_config
from .github_client import AuthFailure, GitHubClient, GitHubError
from .ledger import SeatLedger
from .models import AccountState, AicConsumption, IdentityMapEntry
from .periods import earliest_recoverable_month, parse_report_months
from .resolve import IdentityResolver
from .run_log import RunLog
from . import csv_writer, snapshots
from .reconcile import run_all, summarize_history
from .sources import (
    aic_consumption,
    audit_log,
    billing_usage,
    identities,
    membership,
    org_billing as org_billing_src,
    orgs as orgs_src,
    seats as seats_src,
)
from .audit_archive import archive_start_month, load_archive_events

REQUIRED_CAPABILITIES = ["copilot_seats", "membership"]
OPTIONAL_CAPABILITIES = ["billing_usage", "audit_log", "identity"]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="copilot-aic-report",
        description="Export GitHub Enterprise Copilot license + AI Credits report to CSV (read-only).",
    )
    p.add_argument("--config", help="Path to YAML/JSON config file.")
    p.add_argument("--enterprise", dest="enterprise_slug", help="Enterprise slug.")
    p.add_argument("--orgs", help='"all" or comma-separated org logins.')
    p.add_argument("--billing-period", dest="billing_period", help="YYYY-MM (default: current UTC month).")
    p.add_argument("--report-months", dest="report_months", help='List/range/"last_N_months".')
    p.add_argument("--output", dest="output_path", help="CSV output path.")
    p.add_argument("--log", dest="log_path", help="Run-log path.")
    p.add_argument("--rollup", dest="rollup_path", help="Optional per-user rollup CSV path.")
    p.add_argument("--snapshot-store", dest="snapshot_store", help="Snapshot store directory.")
    p.add_argument("--audit-archive", dest="audit_archive", help="Streamed audit-log archive path.")
    p.add_argument("--identity-map", dest="identity_map_path", help="Identity map JSON path.")
    p.add_argument("--aic-csv", dest="aic_consumption_csv_path", help="Per-user AIC consumption CSV export.")
    p.add_argument("--fetch-membership", dest="fetch_membership", action="store_true", default=None,
                   help="Fetch each org's member list (one call/org) for stricter account-state detection. "
                        "Default: enterprise-SCIM-only, seat holders assumed members.")
    p.add_argument("--allow-partial-scopes", action="store_true", help="Warn (not fail) on missing optional scopes.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _overrides_from_args(args: argparse.Namespace) -> Dict[str, object]:
    ov: Dict[str, object] = {}
    for key in (
        "enterprise_slug",
        "billing_period",
        "report_months",
        "output_path",
        "log_path",
        "rollup_path",
        "snapshot_store",
        "audit_archive",
        "identity_map_path",
        "aic_consumption_csv_path",
        "fetch_membership",
    ):
        val = getattr(args, key, None)
        if val is not None:
            ov[key] = val
    if getattr(args, "orgs", None):
        ov["orgs"] = "all" if args.orgs.strip().lower() == "all" else [o.strip() for o in args.orgs.split(",")]
    if getattr(args, "rollup_path", None):
        ov["emit_rollup"] = True
    return ov


def _remap_scim_active_to_login(
    scim_active: Dict[str, bool],
    identity_index: Dict[str, str],
) -> Dict[str, bool]:
    """Remap SCIM ``userName``-keyed active flags to GitHub-login keys.

    ``identity_index`` maps normalized external ids (incl. SCIM userName) -> real
    login. Any SCIM userName that already equals a login is retained as-is so that
    non-EMU setups (userName == login) still work.
    """
    out: Dict[str, bool] = {}
    for user_name, active in scim_active.items():
        login = identity_index.get(user_name)
        if login:
            out[login.lower()] = active
        else:
            out[user_name] = active  # userName may already be the login
    return out


def load_identity_map(path: Optional[str]) -> List[IdentityMapEntry]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    entries: List[IdentityMapEntry] = []
    for item in data if isinstance(data, list) else []:
        entries.append(
            IdentityMapEntry(
                github_login=item.get("github_login") or item.get("login"),
                scim_username=item.get("scim_userName") or item.get("scim_username"),
                saml_name_id=item.get("saml_nameId") or item.get("saml_name_id"),
                email=item.get("email"),
            )
        )
    return entries


def run(cfg: Config, allow_partial: bool = False) -> RunLog:
    log = RunLog()
    log.config = cfg.to_safe_dict()

    client = GitHubClient(
        token=cfg.token or "",
        api_base=cfg.api_base,
        graphql_url=cfg.graphql_url,
        per_page=cfg.per_page,
        max_retries=cfg.max_retries,
        backoff_base_seconds=cfg.backoff_base_seconds,
        backoff_max_seconds=cfg.backoff_max_seconds,
    )

    # -- auth preflight --
    report = preflight(
        fetch_scopes=client.get_oauth_scopes,
        token=cfg.token,
        required_capabilities=REQUIRED_CAPABILITIES,
    )
    optional_report = preflight(
        fetch_scopes=client.get_oauth_scopes,
        token=cfg.token,
        required_capabilities=OPTIONAL_CAPABILITIES,
        allow_partial=True,
    )
    log.scopes = {"required": report.as_dict(), "optional": optional_report.as_dict()}
    for cap, ok in optional_report.satisfied.items():
        if not ok:
            log.warn(f"optional scope for '{cap}' missing; that data source will be skipped/degraded")

    # -- periods --
    default_period = cfg.resolve_billing_period()
    report_months = parse_report_months(cfg.report_months, default_period)
    snap_months = snapshots.list_snapshot_months(cfg.snapshot_store)
    archive_events = load_archive_events(cfg.audit_archive)
    arch_start = archive_start_month(archive_events)
    earliest = earliest_recoverable_month(snap_months, arch_start, cfg.audit_api_retention_days)
    print(f"[copilot-aic-report] earliest reliably-recoverable month: {earliest}", file=sys.stderr)
    for period in report_months:
        if period < earliest and period not in snap_months:
            log.warn(f"period {period} predates recoverable window ({earliest}); best-effort / aggregate-only")

    # -- discover orgs --
    org_logins = orgs_src.discover_orgs(client, cfg)
    log.orgs_scanned = list(org_logins)

    # -- identities (optional) --
    identity_index: Dict[str, str] = {}
    if optional_report.satisfied.get("identity"):
        try:
            idents = identities.fetch_enterprise_identities(client, cfg)
        except AuthFailure:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            idents = []
            log.warn(f"enterprise identity fetch failed: {exc}")
        for org in org_logins:
            # Per-org identity providers may be inaccessible; skip individually.
            try:
                idents.extend(identities.fetch_org_identities(client, cfg, org))
            except (GitHubError, AuthFailure) as exc:
                if getattr(exc, "status", None) in (403, 404):
                    continue
                log.warn(f"org '{org}' identity fetch failed: {exc}")
            except Exception as exc:  # pragma: no cover - defensive
                log.warn(f"org '{org}' identity fetch failed: {exc}")
        identity_index = identities.build_identity_index(idents)
    resolver = IdentityResolver(identity_index=identity_index, identity_map=load_identity_map(cfg.identity_map_path))

    # -- ledger ingestion --
    ledger = SeatLedger(resolver=resolver)
    for event in archive_events:
        ledger.add_audit_event(event)

    all_seats = []
    org_plan_by_org: Dict[str, str] = {}
    org_billing_map = {}
    seat_logins_by_org: Dict[str, set] = {}
    skipped_orgs: List[str] = []
    for org in org_logins:
        # The token passed global preflight, so a per-org 404/403 means Copilot is
        # not enabled for that org, or the org restricts access — skip it, don't abort.
        try:
            seats = seats_src.fetch_seats(client, cfg, org)
        except (GitHubError, AuthFailure) as exc:
            status = getattr(exc, "status", None)
            if status in (403, 404):
                skipped_orgs.append(org)
                log.warn(f"skipped org '{org}' (seats {status}: Copilot not enabled or access restricted)")
                continue
            raise
        all_seats.extend(seats)
        seat_logins_by_org[org] = {s.assignee_login for s in seats if s.assignee_login}
        for seat in seats:
            ledger.add_live_seat(seat)
        try:
            summary = org_billing_src.fetch_org_billing(client, cfg, org)
            org_billing_map[org] = summary
            if summary.plan_type:
                org_plan_by_org[org] = summary.plan_type
        except (GitHubError, AuthFailure) as exc:
            if getattr(exc, "status", None) in (403, 404):
                log.warn(f"org '{org}' billing summary unavailable ({getattr(exc, 'status', None)})")
            else:
                raise
    log.seats_found = len(all_seats)
    if skipped_orgs:
        log.warn(f"skipped {len(skipped_orgs)} org(s) without accessible Copilot billing")

    # -- audit API events (optional) --
    if optional_report.satisfied.get("audit_log"):
        try:
            for event in audit_log.fetch_enterprise_events(client, cfg):
                ledger.add_audit_event(event)
        except AuthFailure:
            raise
        except Exception as exc:  # pragma: no cover
            log.warn(f"audit-log fetch failed: {exc}")

    # -- membership / account state --
    # Enterprise-level SCIM is always used (single paginated call). Per-org member
    # lists are fetched only when explicitly enabled (cfg.fetch_membership); otherwise
    # seat holders are assumed to be org members and deprovisioning is detected via
    # SCIM ``active`` / suspended state.
    org_members_by_org: Dict[str, set] = {}
    scim_active: Dict[str, bool] = {}
    try:
        if cfg.fetch_membership:
            for org in org_logins:
                org_members_by_org[org] = membership.fetch_org_members(client, cfg, org)
        else:
            for org, logins in seat_logins_by_org.items():
                org_members_by_org[org] = {login.lower() for login in logins}
        scim_active = membership.fetch_scim_active(client, cfg)
    except AuthFailure:
        raise
    except Exception as exc:  # pragma: no cover
        log.warn(f"membership fetch failed: {exc}")
    # SCIM ``active`` is keyed by SCIM userName (an IdP identity that differs from the
    # GitHub login under EMU/SSO). Remap it to GitHub logins via the identity index so
    # the deprovisioned -> inactive downgrade in build_rows actually fires.
    scim_active_by_login = _remap_scim_active_to_login(scim_active, identity_index)
    account_state_list = membership.build_account_states(org_members_by_org, scim_active_by_login, seat_logins_by_org)
    account_states = {(a.org_login, (a.user_login or "").lower()): a for a in account_state_list}

    # -- billing usage (optional, per requested period aggregate) --
    usage_lines = []
    if optional_report.satisfied.get("billing_usage"):
        try:
            usage_lines = billing_usage.filter_copilot(billing_usage.fetch_enterprise_usage(client, cfg))
        except AuthFailure:
            raise
        except Exception as exc:  # pragma: no cover
            log.warn(f"billing-usage fetch failed: {exc}")

    # -- per-user AIC consumption --
    consumption_rows, consumption_source = aic_consumption.get_consumption(client, cfg)
    log.aic_consumption_source = consumption_source
    consumption_index = index_consumption(consumption_rows)
    per_user_has_consumption = consumption_source != "none"

    # -- materialize + build rows per period --
    all_rows: List[Dict[str, object]] = []
    for period in report_months:
        stored = snapshots.read_snapshot_records(cfg.snapshot_store, period)
        is_current = period == default_period
        if stored is not None and not is_current:
            # Authoritative exact history — emit stored rows directly.
            all_rows.extend(stored)
            continue
        cfg_period = cfg
        materialized = ledger.materialize_month(period, log.started_at)
        rows = build_rows(
            materialized,
            cfg_period,
            consumption_index=consumption_index if is_current else {},
            account_states=account_states,
            org_plan_by_org=org_plan_by_org,
            per_user_has_consumption=per_user_has_consumption if is_current else False,
            generated_at=log.started_at,
        )
        # Override each row's billing_period to the materialized period.
        for row in rows:
            row["billing_period"] = period
        all_rows.extend(rows)
        # Write snapshot for reproducibility (current/live months especially).
        if cfg.snapshot_store:
            snapshots.write_snapshot(cfg.snapshot_store, period, rows, meta={"tool_version": __version__})

    for row in all_rows:
        log.bump_resolution(str(row.get("identity_resolution_source") or "unknown"))
        if not str(row.get("user_login") or "").strip():
            log.unresolved_identities.append(
                {
                    "org_login": row.get("org_login"),
                    "billing_period": row.get("billing_period"),
                    "external_identity": row.get("external_identity"),
                }
            )

    # -- reconcile --
    log.reconciliation = run_all(all_rows, org_billing_map, usage_lines, periods=report_months)
    hist = summarize_history(all_rows)
    log.reconciliation.append(
        {"name": "history_provenance", "ok": True, "detail": json.dumps(hist["by_row_source"], sort_keys=True)}
    )
    if hist["unrecoverable"]:
        log.warn(f"UNRECOVERABLE logins: {len(hist['unrecoverable'])}")

    # -- write outputs --
    log.rows_written = csv_writer.write_report(cfg.output_path, all_rows)
    if cfg.emit_rollup and cfg.rollup_path:
        rollup_rows = build_rollup(all_rows, cfg, generated_at=log.started_at)
        log.rollup_rows_written = csv_writer.write_rollup(cfg.rollup_path, rollup_rows)

    log.api_stats = client.stats.as_dict()
    if client.partial_graphql_errors:
        log.api_stats["partial_graphql_errors"] = len(client.partial_graphql_errors)
        forbidden = sorted(
            {
                str(e.get("path", ["", "", "", ""])[0]) + ":" + (e.get("message", "")[:120])
                for e in client.partial_graphql_errors
                if isinstance(e, dict)
            }
        )
        log.warn(
            f"{len(client.partial_graphql_errors)} partial GraphQL error(s) — some orgs/identities "
            f"were inaccessible (e.g. orgs forbidding classic PAT). Accessible data was still used."
        )
        for item in forbidden[:20]:
            log.warn(f"graphql-forbidden: {item}")
    log.finish()
    log.write(cfg.log_path)
    return log


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(config_path=args.config, overrides=_overrides_from_args(args))
    if not cfg.enterprise_slug:
        print("ERROR: enterprise slug is required (--enterprise or config).", file=sys.stderr)
        return 2
    try:
        log = run(cfg, allow_partial=args.allow_partial_scopes)
    except AuthError as exc:
        print(f"AUTH ERROR:\n{exc}", file=sys.stderr)
        return 3
    except AuthFailure as exc:
        print(f"AUTH FAILURE (401/403): {exc}", file=sys.stderr)
        return 3
    print(
        f"[copilot-aic-report] wrote {log.rows_written} rows to {cfg.output_path}; "
        f"log at {cfg.log_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
