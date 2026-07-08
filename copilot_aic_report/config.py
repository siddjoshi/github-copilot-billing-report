"""Configuration model for the Copilot AIC report.

Configuration is layered (later overrides earlier):
  1. Built-in defaults (this module).
  2. A config file (YAML or JSON) passed via --config / CONFIG_PATH.
  3. Environment variables (for secrets and simple overrides).
  4. Command-line flags.

Secrets (the GitHub token) are ONLY read from the environment, never from the
config file, and are never serialized or logged.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

CREDIT_TO_USD = 0.01

# Env var names that may hold the GitHub token, in order of precedence.
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_AIC_TOKEN")

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_GRAPHQL_URL = "https://api.github.com/graphql"

# Plan-type normalization: raw plan_type values -> canonical keys.
PLAN_ALIASES = {
    "business": "business",
    "copilot_business": "business",
    "copilot business": "business",
    "enterprise": "enterprise",
    "copilot_enterprise": "enterprise",
    "copilot enterprise": "enterprise",
    "unknown": "unknown",
    "": "unknown",
}


def normalize_plan(plan_type: Optional[str]) -> str:
    """Normalize a raw plan_type string to a canonical key."""
    if not plan_type:
        return "unknown"
    return PLAN_ALIASES.get(str(plan_type).strip().lower(), str(plan_type).strip().lower())


@dataclass(frozen=True)
class DatedAllowance:
    """A plan default AIC allowance that is valid within an (inclusive) date window.

    ``start``/``end`` are ISO ``YYYY-MM-DD`` strings or ``None`` for open-ended.
    ``credits`` is a mapping of canonical plan key -> monthly credit allowance.
    """

    credits: Dict[str, float]
    start: Optional[str] = None
    end: Optional[str] = None

    def covers(self, day: _dt.date) -> bool:
        if self.start and day < _dt.date.fromisoformat(self.start):
            return False
        if self.end and day > _dt.date.fromisoformat(self.end):
            return False
        return True


# GitHub "flex allotment" defaults. These change (incl. promo windows) so they are
# date-aware and configurable. CONFIRM at run time.
DEFAULT_AIC_TABLE: List[DatedAllowance] = [
    DatedAllowance(credits={"business": 1900.0, "enterprise": 3900.0}),
]

# Negotiated seat pricing varies -> config-driven. USD monthly list prices as a
# conservative default; override via config or actual billed amount from billing-usage.
DEFAULT_LICENSE_COST_TABLE: Dict[str, float] = {
    "business": 19.0,
    "enterprise": 39.0,
    "unknown": 0.0,
}


@dataclass
class Config:
    # Enterprise / scope
    enterprise_slug: str = ""
    orgs: Any = "all"  # "all" or list[str]

    # Endpoints
    api_base: str = DEFAULT_API_BASE
    graphql_url: str = DEFAULT_GRAPHQL_URL

    # Reporting window
    billing_period: str = ""  # "YYYY-MM"; empty => current UTC cycle
    activity_window_days: Optional[int] = None  # None => use billing cycle

    # Historical reporting (addendum). ``report_months`` may be:
    #   - "" / None  => single current (or billing_period) month  [back-compat]
    #   - a list of "YYYY-MM"
    #   - a string range "YYYY-MM..YYYY-MM"
    #   - "last_N_months" (e.g. "last_6_months")
    report_months: Any = None
    snapshot_store: Optional[str] = None   # dir for durable monthly snapshots
    audit_archive: Optional[str] = None    # path to streamed audit logs (JSON/JSONL/dir)
    audit_api_retention_days: int = 180    # GitHub audit-log API retention window
    billing_usage_max_months: int = 24     # enhanced billing usage history depth

    # Pricing / AIC tables
    license_cost_table: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_LICENSE_COST_TABLE)
    )
    default_aic_table: List[DatedAllowance] = field(
        default_factory=lambda: list(DEFAULT_AIC_TABLE)
    )
    credit_to_usd: float = CREDIT_TO_USD
    currency: str = "USD"

    # Per-user AIC "assigned" budgets: login -> USD (optional override).
    per_user_aic_budget_usd: Dict[str, float] = field(default_factory=dict)

    # Per-user AIC consumption source config (adapter auto-detects; both supported).
    aic_consumption_api_enabled: bool = True
    aic_consumption_csv_path: Optional[str] = None

    # Per-org API calls are opt-in to minimize requests; enterprise-level endpoints
    # are preferred. When these are False (default): plan_type comes from the
    # enterprise seats payload, identities from enterprise externalIdentities, and
    # account state from enterprise SCIM.
    fetch_membership: bool = False
    fetch_org_billing: bool = False
    fetch_org_identities: bool = False

    # Output / logging
    output_path: str = "copilot_aic_report.csv"
    log_path: str = "copilot_aic_report.log"
    emit_rollup: bool = False
    rollup_path: Optional[str] = None

    # Identity fallback map file: JSON list of
    #   {github_login, scim_userName, saml_nameId, email}
    identity_map_path: Optional[str] = None

    # Resilience
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    per_page: int = 100
    checkpoint_path: Optional[str] = None

    # Secret (never serialized). Populated from env only.
    token: Optional[str] = field(default=None, repr=False)

    # ---- Derived helpers -------------------------------------------------

    def resolve_billing_period(self, now: Optional[_dt.datetime] = None) -> str:
        """Return the reporting period as ``YYYY-MM`` (current UTC month if unset)."""
        if self.billing_period:
            return self.billing_period
        now = now or _dt.datetime.now(_dt.timezone.utc)
        return f"{now.year:04d}-{now.month:02d}"

    def period_date(self) -> _dt.date:
        """First day of the reporting period (used for date-aware AIC lookups)."""
        year, month = (int(x) for x in self.resolve_billing_period().split("-"))
        return _dt.date(year, month, 1)

    def default_aic_credits(self, plan_type: str, day: Optional[_dt.date] = None) -> float:
        """Date-aware plan default AIC allowance (credits) for a plan/period."""
        plan = normalize_plan(plan_type)
        day = day or self.period_date()
        for allowance in self.default_aic_table:
            if allowance.covers(day) and plan in allowance.credits:
                return float(allowance.credits[plan])
        return 0.0

    def license_cost(self, plan_type: str) -> float:
        plan = normalize_plan(plan_type)
        return float(self.license_cost_table.get(plan, self.license_cost_table.get("unknown", 0.0)))

    def orgs_list(self) -> Optional[List[str]]:
        """Return an explicit org list, or ``None`` to signal auto-discover 'all'."""
        if isinstance(self.orgs, str):
            return None if self.orgs.strip().lower() == "all" else [self.orgs.strip()]
        return list(self.orgs)

    def to_safe_dict(self) -> Dict[str, Any]:
        """Serializable view with secrets redacted (for run-log)."""
        return {
            "enterprise_slug": self.enterprise_slug,
            "orgs": self.orgs,
            "api_base": self.api_base,
            "billing_period": self.resolve_billing_period(),
            "activity_window_days": self.activity_window_days,
            "license_cost_table": self.license_cost_table,
            "credit_to_usd": self.credit_to_usd,
            "currency": self.currency,
            "aic_consumption_api_enabled": self.aic_consumption_api_enabled,
            "aic_consumption_csv_path": self.aic_consumption_csv_path,
            "output_path": self.output_path,
            "emit_rollup": self.emit_rollup,
            "identity_map_path": self.identity_map_path,
            "token": "***REDACTED***" if self.token else None,
        }


def _load_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.lower().endswith((".yaml", ".yml")):
        import yaml  # local import so JSON-only users need not install PyYAML

        return yaml.safe_load(text) or {}
    return json.loads(text) if text.strip() else {}


def _parse_aic_table(raw: Any) -> List[DatedAllowance]:
    """Parse a config ``default_aic_table`` into DatedAllowance entries.

    Accepts either a plain ``{plan: credits}`` mapping (open-ended) or a list of
    ``{start, end, credits: {plan: n}}`` windows.
    """
    if raw is None:
        return list(DEFAULT_AIC_TABLE)
    if isinstance(raw, dict) and "credits" not in raw:
        return [DatedAllowance(credits={normalize_plan(k): float(v) for k, v in raw.items()})]
    entries = raw if isinstance(raw, list) else [raw]
    out: List[DatedAllowance] = []
    for item in entries:
        credits = {normalize_plan(k): float(v) for k, v in (item.get("credits") or {}).items()}
        out.append(DatedAllowance(credits=credits, start=item.get("start"), end=item.get("end")))
    return out


def load_config(
    config_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> Config:
    """Build a :class:`Config` from file + env + explicit overrides.

    The token is read exclusively from the environment.
    """
    env = env if env is not None else dict(os.environ)
    cfg = Config()

    if config_path:
        data = _load_file(config_path)
        if "default_aic_table" in data:
            cfg.default_aic_table = _parse_aic_table(data.pop("default_aic_table"))
        if "license_cost_table" in data:
            cfg.license_cost_table = {
                normalize_plan(k): float(v) for k, v in data.pop("license_cost_table").items()
            }
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    # Environment overrides (non-secret conveniences).
    if env.get("ENTERPRISE_SLUG"):
        cfg.enterprise_slug = env["ENTERPRISE_SLUG"]
    if env.get("BILLING_PERIOD"):
        cfg.billing_period = env["BILLING_PERIOD"]
    if env.get("ACTIVITY_WINDOW_DAYS"):
        cfg.activity_window_days = int(env["ACTIVITY_WINDOW_DAYS"])
    if env.get("OUTPUT_PATH"):
        cfg.output_path = env["OUTPUT_PATH"]
    if env.get("LOG_PATH"):
        cfg.log_path = env["LOG_PATH"]
    if env.get("IDENTITY_MAP"):
        cfg.identity_map_path = env["IDENTITY_MAP"]
    if env.get("REPORT_MONTHS"):
        cfg.report_months = env["REPORT_MONTHS"]
    if env.get("SNAPSHOT_STORE"):
        cfg.snapshot_store = env["SNAPSHOT_STORE"]
    if env.get("AUDIT_ARCHIVE"):
        cfg.audit_archive = env["AUDIT_ARCHIVE"]

    # Explicit overrides (e.g. from CLI flags).
    if overrides:
        for key, value in overrides.items():
            if value is None:
                continue
            if key == "default_aic_table":
                cfg.default_aic_table = _parse_aic_table(value)
            elif key == "license_cost_table":
                cfg.license_cost_table = {normalize_plan(k): float(v) for k, v in value.items()}
            elif hasattr(cfg, key):
                setattr(cfg, key, value)

    # Secret: env only.
    for name in TOKEN_ENV_VARS:
        if env.get(name):
            cfg.token = env[name]
            break

    return cfg


def with_overrides(cfg: Config, **changes: Any) -> Config:
    """Return a copy of ``cfg`` with dataclass fields replaced."""
    return replace(cfg, **changes)
