"""Enhanced billing usage source for dollar amounts and SKUs."""
from __future__ import annotations

import sys
from typing import Any, Iterable, Optional

from copilot_aic_report.github_client import GitHubError
from copilot_aic_report.models import BillingUsageLine


def fetch_enterprise_usage(client, cfg) -> list[BillingUsageLine]:
    """Fetch enhanced billing usage for the configured enterprise."""
    year, month = _billing_year_month(cfg)
    path = f"/enterprises/{cfg.enterprise_slug}/settings/billing/usage"
    return _fetch_usage(client, path, {"year": year, "month": month}, cfg.enterprise_slug)


def fetch_org_usage(client, cfg, org_login) -> list[BillingUsageLine]:
    """Fetch enhanced billing usage for one organization."""
    year, month = _billing_year_month(cfg)
    path = f"/organizations/{org_login}/settings/billing/usage"
    return _fetch_usage(client, path, {"year": year, "month": month}, org_login)


def filter_copilot(lines) -> list[BillingUsageLine]:
    """Return only billing usage lines whose product is Copilot."""
    return [
        line
        for line in lines
        if (line.product or "").lower() == "copilot"
    ]


def copilot_net_usd(lines) -> float:
    """Sum net USD over Copilot billing usage lines."""
    return sum(line.net_amount or 0.0 for line in filter_copilot(lines))


def _billing_year_month(cfg) -> tuple[int, int]:
    period = cfg.resolve_billing_period()
    year, month = period.split("-", 1)
    return int(year), int(month)


def _fetch_usage(client, path: str, params: dict[str, int], entity: str) -> list[BillingUsageLine]:
    try:
        payload = client.get(path, params=params) or {}
    except GitHubError as exc:
        if exc.status in (403, 404):
            print(
                "Note: enhanced billing usage endpoint is unavailable or not enabled "
                f"for {entity} (HTTP {exc.status}); continuing without billing usage.",
                file=sys.stderr,
            )
            return []
        raise
    return [_usage_item_to_line(item) for item in _usage_items(payload)]


def _usage_items(payload: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("usageItems") or []
    return [item for item in items if isinstance(item, dict)]


def _usage_item_to_line(item: dict[str, Any]) -> BillingUsageLine:
    return BillingUsageLine(
        date=item.get("date"),
        product=item.get("product"),
        sku=item.get("sku"),
        quantity=_to_float(item.get("quantity")),
        unit_type=item.get("unitType"),
        gross_amount=_to_float(item.get("grossAmount")),
        discount_amount=_to_float(item.get("discountAmount")),
        net_amount=_to_float(item.get("netAmount")),
        organization_name=item.get("organizationName"),
        repository_name=item.get("repositoryName"),
        raw=item,
    )


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)
