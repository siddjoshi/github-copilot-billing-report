"""Per-user AI-credit / premium-request consumption source adapter.

The GitHub billing-usage API aggregates AIC usage by org/SKU, so per-user
consumption is loaded from a UI-exported CSV or, when available, a per-user
usage-report API.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from copilot_aic_report.github_client import AuthFailure, GitHubError
from copilot_aic_report.models import AicConsumption


class AicSourceUnavailable(Exception):
    """Raised when a configured AIC consumption source cannot be used."""


_USER_COLUMNS = {"user", "user_login", "login", "handle", "username"}
_CREDIT_COLUMNS = {"credits", "credits_consumed", "ai_credits", "premium_requests", "quantity"}
_ORG_COLUMNS = {"org", "org_login", "organization", "organizationname"}
_USD_COLUMNS = {"usd", "amount", "net_amount", "usd_consumed"}


def load_from_csv(path: Any, cfg: Any) -> list[AicConsumption]:
    """Load per-user AIC consumption from a billing UI CSV export."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise AicSourceUnavailable(f"AIC consumption CSV not found: {csv_path}")

    rows: list[AicConsumption] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            item = dict(raw)
            row = _map_row(item, cfg, source="csv")
            if row is not None:
                rows.append(row)
    return rows


def fetch_from_api(client: Any, cfg: Any, holders: Iterable[tuple] = ()) -> list[AicConsumption]:
    """Load per-user AIC consumption from GitHub's enterprise per-user AI-credit report.

    Uses ``GET /enterprises/{ent}/settings/billing/ai_credit/usage?user={login}`` — an
    enterprise-level endpoint that works regardless of per-org classic-PAT restrictions
    and needs only one call per unique user (not per org+user). AIC is pooled/consumed
    per user at the billing-entity level, so the result is org-agnostic
    (``org_login=None``). Sums ``netQuantity`` (credits) and ``netAmount`` (USD).

    Raises :class:`AicSourceUnavailable` if the endpoint is not available at all
    (so callers can fall back to a CSV export).
    """
    period = cfg.resolve_billing_period()
    year_text, month_text = period.split("-", 1)
    params_base = {"year": int(year_text), "month": int(month_text)}
    endpoint = f"/enterprises/{cfg.enterprise_slug}/settings/billing/ai_credit/usage"

    # Unique logins across the (org, login) holders.
    logins: list[str] = []
    seen: set = set()
    for _org, login in holders:
        if login and str(login).lower() not in seen:
            seen.add(str(login).lower())
            logins.append(login)

    rows: list[AicConsumption] = []
    any_success = False
    for login in logins:
        try:
            payload = client.get(endpoint, params={**params_base, "user": login})
        except AuthFailure as exc:
            raise AicSourceUnavailable("AIC consumption API is not accessible") from exc
        except GitHubError as exc:
            if exc.status in (403, 404, 410):
                # Enterprise endpoint unavailable entirely -> stop and signal fallback.
                raise AicSourceUnavailable("enterprise AIC usage endpoint unavailable") from exc
            raise
        any_success = True
        items = payload.get("usageItems") if isinstance(payload, dict) else None
        if not items:
            continue
        credits = sum(_to_float(item.get("netQuantity")) for item in items)
        usd = sum(_to_float(item.get("netAmount")) for item in items)
        if credits == 0 and usd == 0:
            continue
        rows.append(
            AicConsumption(
                user_login=str(login).strip(),
                org_login=None,  # enterprise-wide per-user consumption
                credits_consumed=credits,
                usd_consumed=usd if usd else credits * float(cfg.credit_to_usd),
                source="api",
                raw={"user": login, "usage_items": len(items)},
            )
        )

    if not any_success and logins:
        raise AicSourceUnavailable("no AIC usage retrieved")
    return rows


def get_consumption(client: Any, cfg: Any, holders: Iterable[tuple] = ()) -> tuple[list[AicConsumption], str]:
    """Return AIC consumption rows and the source used: ``csv``, ``api``, or ``none``.

    Precedence: a configured CSV export first (authoritative, works for historical
    months), else the per-user API for the given ``holders``.
    """
    csv_path = cfg.aic_consumption_csv_path
    holders = list(holders)

    if csv_path:
        try:
            return load_from_csv(csv_path, cfg), "csv"
        except AicSourceUnavailable:
            pass

    if cfg.aic_consumption_api_enabled and holders:
        try:
            return fetch_from_api(client, cfg, holders), "api"
        except AicSourceUnavailable:
            if csv_path:
                try:
                    return load_from_csv(csv_path, cfg), "csv"
                except AicSourceUnavailable:
                    pass

    return [], "none"


def _map_row(raw: Mapping[str, Any], cfg: Any, *, source: str) -> Optional[AicConsumption]:
    user = _value_for(raw, _USER_COLUMNS)
    if not user:
        return None

    credits = _to_float(_value_for(raw, _CREDIT_COLUMNS))
    usd_value = _value_for(raw, _USD_COLUMNS)
    usd = _to_float(usd_value) if _has_value(usd_value) else credits * float(cfg.credit_to_usd)

    return AicConsumption(
        user_login=str(user).strip(),
        org_login=_optional_text(_value_for(raw, _ORG_COLUMNS)),
        credits_consumed=credits,
        usd_consumed=usd,
        source=source,
        raw=dict(raw),
    )


def _iter_api_rows(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = []
        for key in ("rows", "usage", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
    else:
        candidates = []

    for item in candidates:
        if isinstance(item, Mapping):
            yield item


def _value_for(row: Mapping[str, Any], names: set[str]) -> Any:
    for key, value in row.items():
        if _normalize_key(key) in names:
            return value
    return None


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower()


def _optional_text(value: Any) -> Optional[str]:
    if not _has_value(value):
        return None
    return str(value).strip()


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _to_float(value: Any) -> float:
    if not _has_value(value):
        return 0.0
    return float(str(value).strip().replace(",", ""))
