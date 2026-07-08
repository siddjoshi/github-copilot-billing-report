import datetime as dt

import pytest

from copilot_aic_report import config as cfgmod
from copilot_aic_report.config import Config, DatedAllowance, load_config, normalize_plan


def test_normalize_plan_aliases():
    assert normalize_plan("Copilot Business") == "business"
    assert normalize_plan("copilot_enterprise") == "enterprise"
    assert normalize_plan(None) == "unknown"
    assert normalize_plan("") == "unknown"


def test_default_aic_credits_defaults():
    cfg = Config()
    assert cfg.default_aic_credits("business") == 1900.0
    assert cfg.default_aic_credits("enterprise") == 3900.0
    assert cfg.default_aic_credits("unknown") == 0.0


def test_date_aware_promo_window():
    cfg = Config(
        default_aic_table=[
            DatedAllowance(credits={"business": 3000.0}, start="2026-01-01", end="2026-06-30"),
            DatedAllowance(credits={"business": 1900.0}),
        ],
        billing_period="2026-03",
    )
    assert cfg.default_aic_credits("business") == 3000.0
    # Outside promo window -> falls through to open-ended entry.
    assert cfg.default_aic_credits("business", dt.date(2026, 12, 1)) == 1900.0


def test_license_cost_fallback():
    cfg = Config()
    assert cfg.license_cost("business") == 19.0
    assert cfg.license_cost("enterprise") == 39.0
    assert cfg.license_cost("weird-plan") == 0.0


def test_orgs_list_all_vs_explicit():
    assert Config(orgs="all").orgs_list() is None
    assert Config(orgs=["a", "b"]).orgs_list() == ["a", "b"]
    assert Config(orgs="solo").orgs_list() == ["solo"]


def test_resolve_billing_period_default(monkeypatch):
    cfg = Config()
    now = dt.datetime(2026, 7, 8, tzinfo=dt.timezone.utc)
    assert cfg.resolve_billing_period(now) == "2026-07"
    cfg2 = Config(billing_period="2025-12")
    assert cfg2.resolve_billing_period() == "2025-12"


def test_period_date():
    assert Config(billing_period="2026-03").period_date() == dt.date(2026, 3, 1)


def test_to_safe_dict_redacts_token():
    cfg = Config(token="secret-token", enterprise_slug="acme")
    safe = cfg.to_safe_dict()
    assert safe["token"] == "***REDACTED***"
    assert safe["enterprise_slug"] == "acme"


def test_load_config_token_from_env():
    cfg = load_config(env={"GITHUB_TOKEN": "abc", "ENTERPRISE_SLUG": "acme"})
    assert cfg.token == "abc"
    assert cfg.enterprise_slug == "acme"


def test_load_config_token_precedence():
    cfg = load_config(env={"GH_TOKEN": "second", "COPILOT_AIC_TOKEN": "third"})
    assert cfg.token == "second"


def test_load_config_no_token():
    cfg = load_config(env={})
    assert cfg.token is None


def test_load_config_from_json_file(tmp_path):
    p = tmp_path / "conf.json"
    p.write_text(
        '{"enterprise_slug":"acme","license_cost_table":{"business":25},'
        '"default_aic_table":{"business":2000}}',
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(p), env={})
    assert cfg.enterprise_slug == "acme"
    assert cfg.license_cost("business") == 25.0
    assert cfg.default_aic_credits("business") == 2000.0


def test_load_config_yaml_windowed_aic(tmp_path):
    p = tmp_path / "conf.yaml"
    p.write_text(
        "enterprise_slug: acme\n"
        "default_aic_table:\n"
        "  - start: '2026-01-01'\n"
        "    end: '2026-06-30'\n"
        "    credits: {business: 3000, enterprise: 7000}\n"
        "  - credits: {business: 1900, enterprise: 3900}\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(p), env={"BILLING_PERIOD": "2026-02"})
    assert cfg.default_aic_credits("enterprise") == 7000.0


def test_overrides_applied():
    cfg = load_config(overrides={"output_path": "custom.csv", "orgs": ["x"]}, env={})
    assert cfg.output_path == "custom.csv"
    assert cfg.orgs_list() == ["x"]


def test_activity_window_env():
    cfg = load_config(env={"ACTIVITY_WINDOW_DAYS": "30"})
    assert cfg.activity_window_days == 30
