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
    """Load per-user AIC consumption from GitHub's per-user AI-credit report.

    Two per-user AIC report endpoints are used, in preference order:

    1. **Enterprise (preferred, backward-compatible):**
       ``GET /enterprises/{ent}/settings/billing/ai_credit/usage?user={login}`` — one
       org-agnostic call per unique user. Preserved as the primary source so
       environments where it is available keep working unchanged.
    2. **Organization (fallback, only when needed):**
       ``GET /organizations/{org}/settings/billing/ai_credit/usage?user={login}`` —
       used only when the enterprise endpoint is genuinely unusable (every call fails,
       e.g. the enterprise-level report does not exist or is IP-blocked). The org
       report's ``usageItems`` carry no per-user field, so ``?user=`` is required.

    Important: the ``?user=`` filter returns ``404 "User '…' not found"`` for logins the
    billing system does not know (e.g. audit-reconstructed or deprovisioned handles) and
    ``200`` with an empty ``usageItems`` for known users with no consumption. **A per-user
    404 means "no data for that user", not "endpoint gone"** — it must never abort the
    batch, or a single unknown login zeroes out everyone (the bug this fixes). Only when
    *every* call fails (zero 200 responses) is the source treated as unavailable.

    The result is reported org-agnostic (``org_login=None``) and each unique login is
    queried once. Sums ``grossQuantity`` (credits) and ``grossAmount`` (USD) — gross is
    what the user actually consumed; net is billed after the included allowance (often 0).

    Scales to large enterprises (10k+ users): one call per user issued concurrently
    (``cfg.aic_concurrency``) with the shared :class:`GitHubClient` backing off on
    primary/secondary rate limits.

    Raises :class:`AicSourceUnavailable` only when nothing at all could be collected and
    the source genuinely failed (so callers can fall back to a CSV export).
    """
    period = cfg.resolve_billing_period()
    year_text, month_text = period.split("-", 1)
    params_base = {"year": int(year_text), "month": int(month_text)}
    enterprise_slug = getattr(cfg, "enterprise_slug", "") or ""

    def _enterprise_endpoint() -> str:
        return f"/enterprises/{enterprise_slug}/settings/billing/ai_credit/usage"

    def _org_endpoint(org: str) -> str:
        return f"/organizations/{org}/settings/billing/ai_credit/usage"

    # Unique holders keyed by login (AIC is per-user, enterprise-wide). Keep the org
    # from the first seat holder for the login (used only for the org-level fallback),
    # a numeric user id used as a best-effort fallback query key, and an ordered list of
    # query keys. Billing keys AIC by the ORIGINAL seat login (obfuscated for EMU
    # enterprises), which differs from a login that was resolved to a real handle via
    # audit — querying the resolved handle returns "User not found". So each holder may
    # carry ``query_login`` (holder[3]); we query that first, then the (resolved) login,
    # and attribute the result to ``login`` so it matches the report row.
    unique: list[tuple] = []
    seen: set = set()
    for holder in holders:
        org = holder[0] if len(holder) > 0 else None
        login = holder[1] if len(holder) > 1 else None
        user_id = holder[2] if len(holder) > 2 else None
        query_login = holder[3] if len(holder) > 3 else None
        if not login:
            continue
        key = str(login).lower()
        if key in seen:
            continue
        seen.add(key)
        query_keys: list = []
        for candidate in (query_login, login):
            if candidate and str(candidate) not in query_keys:
                query_keys.append(str(candidate))
        unique.append((login, user_id, org, query_keys))

    if not unique:
        return []

    rate = float(cfg.credit_to_usd)

    def _items(payload: Any) -> Optional[list]:
        items = payload.get("usageItems") if isinstance(payload, dict) else None
        return items or None

    def _consume(login: Any, org: Any, items: list) -> Optional[AicConsumption]:
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
            raw={"user": login, "org": org, "usage_items": len(items), "net_usd": net_usd},
        )

    def _is_user_not_found(exc: GitHubError) -> bool:
        """True if a 404 is a per-user "User '…' not found" (no data for that user),
        as opposed to a generic path-level 404 (endpoint absent)."""
        body = getattr(exc, "body", None)
        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("message", ""))
        if not msg:
            msg = str(exc)
        msg = msg.lower()
        return "not found" in msg and "user" in msg

    def _run(mode: str) -> list[AicConsumption]:
        """Query every unique user via ``mode`` (``enterprise`` or ``org``).

        Returns the collected consumption rows and, via the enclosing ``stats`` dict,
        records how many calls returned 200 (``ok``) vs failed at the endpoint/access
        level (``endpoint_fail``) so the caller can distinguish "no consumption" from
        "endpoint unusable". Per-user "User not found" 404s are treated as no data and
        never counted as failures.
        """
        lock = threading.Lock()
        stats = {"ok": 0, "endpoint_fail": 0}
        auth_failures: list = []

        def _query(endpoint: str, user_key: str, *, is_fallback: bool) -> Optional[list]:
            try:
                payload = client.get(endpoint, params={**params_base, "user": user_key})
            except AuthFailure as exc:
                if not is_fallback:
                    with lock:
                        auth_failures.append(exc)
                        stats["endpoint_fail"] += 1
                return None
            except GitHubError as exc:
                if getattr(exc, "status", None) in (403, 404, 410):
                    if not is_fallback and not _is_user_not_found(exc):
                        with lock:
                            stats["endpoint_fail"] += 1
                    return None
                raise
            with lock:
                stats["ok"] += 1
            return _items(payload)

        def _fetch_one(entry: tuple) -> Optional[AicConsumption]:
            login, user_id, org, query_keys = entry
            if mode == "org":
                if not org or str(org).startswith("enterprise:"):
                    return None
                endpoint = _org_endpoint(org)
            else:
                endpoint = _enterprise_endpoint()
            items = None
            # Try each identity form (original seat login first, then resolved login)
            # until one returns data; billing may only recognise one of them.
            for qk in query_keys:
                items = _query(endpoint, qk, is_fallback=False)
                if items:
                    break
            # Deprovisioned users: the login often yields nothing; retry by id.
            if not items and user_id is not None:
                items = _query(endpoint, str(user_id), is_fallback=True)
            if not items:
                return None
            # Attribute to the (display) login so it matches the materialized row.
            return _consume(login, org, items)

        workers = max(1, int(getattr(cfg, "aic_concurrency", 1) or 1))
        if workers == 1:
            results = [_fetch_one(entry) for entry in unique]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_fetch_one, unique))
        rows = [r for r in results if r is not None]
        _run.last_stats = stats  # type: ignore[attr-defined]
        _run.last_auth_failures = auth_failures  # type: ignore[attr-defined]
        return rows

    def _terminal(rows: list, allow_org_fallback: bool) -> list:
        """Return rows or raise; optionally attempt the org fallback when the primary
        mode collected nothing AND no call succeeded (endpoint genuinely unusable)."""
        stats = _run.last_stats  # type: ignore[attr-defined]
        auth_failures = _run.last_auth_failures  # type: ignore[attr-defined]
        if rows:
            return rows
        if stats["ok"] > 0:
            # The endpoint works; everyone was simply within their allowance / no usage.
            return rows
        # Nothing succeeded — the endpoint/access is unusable.
        if allow_org_fallback and any(
            entry[2] and not str(entry[2]).startswith("enterprise:") for entry in unique
        ):
            org_rows = _run("org")
            return _terminal(org_rows, allow_org_fallback=False)
        if auth_failures:
            raise AicSourceUnavailable("AIC consumption API is not accessible") from auth_failures[0]
        raise AicSourceUnavailable("per-user AIC usage endpoint unavailable")

    primary = "enterprise" if enterprise_slug else "org"
    rows = _run(primary)
    return _terminal(rows, allow_org_fallback=(primary == "enterprise"))


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
