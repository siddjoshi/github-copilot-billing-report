"""RFC 4180-compliant CSV writer for the Copilot AIC report.

Column order is fixed: the required columns first (exact order from the spec),
then recommended columns. Values are coerced via :func:`util.cell` so empty cells
are emitted rather than the literal string ``null``.
"""
from __future__ import annotations

import csv
from typing import Dict, Iterable, List

from .util import cell

REQUIRED_COLUMNS: List[str] = [
    "user_login",
    "license_assigned_date",
    "gh_copilot_license_cost",
    "default_aic_user_level",
    "aic_billing_dollar_assigned",
    "aic_consumed",
    "user_status",
    "user_revoked_date",
]

RECOMMENDED_COLUMNS: List[str] = [
    "org_login",
    "plan_type",
    "seat_status",
    "assigned_via",
    "last_activity_at",
    "external_identity",
    "identity_resolution_source",
    "account_state",
    "aic_assigned_rule_used",
    "default_aic_usd",
    "aic_consumed_usd",
    "currency",
    "billing_period",
    "row_source",
    "login_recovery_source",
    "history_confidence",
    "as_of_utc",
    "data_quality_notes",
    "data_generated_at_utc",
]

ALL_COLUMNS: List[str] = REQUIRED_COLUMNS + RECOMMENDED_COLUMNS

# Rollup (per-user aggregation) columns.
ROLLUP_COLUMNS: List[str] = [
    "user_login",
    "earliest_license_assigned_date",
    "any_active",
    "user_status",
    "orgs",
    "total_gh_copilot_license_cost",
    "total_aic_billing_dollar_assigned",
    "total_aic_consumed",
    "total_aic_consumed_usd",
    "latest_user_revoked_date",
    "identity_resolution_source",
    "currency",
    "billing_period",
    "data_quality_notes",
    "data_generated_at_utc",
]


def write_rows(path: str, rows: Iterable[Dict[str, object]], columns: List[str] = ALL_COLUMNS) -> int:
    """Write ``rows`` (list of dicts) to ``path`` as UTF-8 CSV. Returns row count."""
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell(row.get(col)) for col in columns])
            count += 1
    return count


def write_report(path: str, rows: Iterable[Dict[str, object]]) -> int:
    return write_rows(path, rows, ALL_COLUMNS)


def write_rollup(path: str, rows: Iterable[Dict[str, object]]) -> int:
    return write_rows(path, rows, ROLLUP_COLUMNS)
