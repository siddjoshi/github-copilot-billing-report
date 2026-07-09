"""Per-user AI-credit / premium-request consumption source adapter.

The GitHub billing-usage API aggregates AIC usage by org/SKU, so per-user
consumption is loaded from a UI-exported CSV or, when available, a per-user
usage-report API.
"""
from __future__ import annotations

import csv
import threading
from concurrent.futures import ThreadPoolExecutor
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

    # Unique holders keyed by login. Each holder may carry a numeric user id used as
    # a best-effort fallback query key for deprovisioned users whose login handle no
    # longer resolves against the usage API.
    unique: list[tuple] = []
    seen: set = set()
    for holder in holders:
        org = holder[0] if len(holder) > 0 else None
        login = holder[1] if len(holder) > 1 else None
        user_id = holder[2] if len(holder) > 2 else None
        if not login:
            continue
        key = str(login).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append((login, user_id))

    if not unique:
        return []

    rate = float(cfg.credit_to_usd)
    unavailable = threading.Event()
    auth_failure: list = []

    def _query(user_key: str, *, is_fallback: bool) -> Optional[list]:
        """Return usageItems for one user key, or None if nothing/unavailable.

        A primary (login) query treats a hard 403/404/410 as "endpoint
        unavailable" (so callers fall back to CSV). A fallback (user-id) query
        swallows those errors locally without aborting the whole run.
        """
        try:
            payload = client.get(endpoint, params={**params_base, "user": user_key})
        except AuthFailure as exc:
            if is_fallback:
                return None
            auth_failure.append(exc)
            return None
        except GitHubError as exc:
            if exc.status in (403, 404, 410):
                if not is_fallback:
                    unavailable.set()
                return None
            raise
        items = payload.get("usageItems") if isinstance(payload, dict) else None
        return items or None

    def _fetch_one(entry: tuple) -> Optional[AicConsumption]:
        login, user_id = entry
        if unavailable.is_set() or auth_failure:
            return None
        items = _query(login, is_fallback=False)
        # Deprovisioned users: the obfuscated login often yields nothing; retry by
        # the permanent numeric user id (best-effort).
        if not items and user_id is not None and not unavailable.is_set() and not auth_failure:
            items = _query(str(user_id), is_fallback=True)
        if not items:
            return None
        # "Consumed" = credits actually used (gross). netQuantity/netAmount is the
        # BILLED amount after the included allowance discount and is often 0 even
        # when the user consumed credits, so it must NOT be used for consumption.
        credits = sum(_to_float(item.get("grossQuantity")) for item in items)
        usd = sum(_to_float(item.get("grossAmount")) for item in items)
        net_usd = sum(_to_float(item.get("netAmount")) for item in items)
        if credits == 0 and usd == 0:
            return None
        return AicConsumption(
            user_login=str(login).strip(),
            org_login=None,  # enterprise-wide per-user consumption
            credits_consumed=credits,
            usd_consumed=usd if usd else credits * rate,
            source="api",
            raw={"user": login, "usage_items": len(items), "net_usd": net_usd},
        )

    workers = max(1, int(getattr(cfg, "aic_concurrency", 1) or 1))
    rows: list[AicConsumption] = []
    if workers == 1:
        results = [_fetch_one(entry) for entry in unique]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_fetch_one, unique))
    rows = [r for r in results if r is not None]

    if auth_failure:
        raise AicSourceUnavailable("AIC consumption API is not accessible") from auth_failure[0]
    if not rows and unavailable.is_set():
        raise AicSourceUnavailable("enterprise AIC usage endpoint unavailable")
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
