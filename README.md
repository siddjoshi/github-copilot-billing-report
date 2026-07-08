# copilot-aic-report

Export a **GitHub Enterprise Copilot license + AI Credits (AIC)** report to CSV.

For every user who holds (or has held) a Copilot license across **all**
organizations in an enterprise, this **read-only** tool produces one CSV row per
`(user_login, org, billing_period)` with the license assignment date, seat cost,
AI-Credit allocation/consumption, real GitHub login, status, and revoke date.

The top priority is **correctness for revoked / deprovisioned users**: `user_login`
is ALWAYS the canonical GitHub handle — never a SAML NameID, SCIM userName, or email.

> The tool never mutates anything in GitHub. No seat changes, ever.

---

## Install

```bash
python -m pip install -e .            # or: pip install -r requirements.txt
```

Requires Python 3.9+.

## Authenticate

The GitHub token is read **only** from the environment (never from config, never
logged):

```bash
export GITHUB_TOKEN=ghp_xxx            # or GH_TOKEN / COPILOT_AIC_TOKEN
```

A **classic PAT** is supported. Required/optional scopes are validated up front and
recorded in the run log (see [Scopes](#required-scopes)).

## Run

```bash
copilot-aic-report --enterprise my-enterprise --output report.csv
# or with a config file:
copilot-aic-report --config config.yaml
```

Common flags (all also settable in the config file):

| Flag | Meaning |
|------|---------|
| `--enterprise` | Enterprise slug (required). |
| `--orgs` | `all` (auto-discover) or comma-separated org logins. |
| `--billing-period` | `YYYY-MM` (default: current UTC month). |
| `--report-months` | Historical range: `2026-01..2026-07`, `last_6_months`, or a list. |
| `--snapshot-store` | Directory of durable monthly snapshots (written & read). |
| `--audit-archive` | Streamed audit-log export (JSON/JSONL/dir) for history >180 days. |
| `--identity-map` | Append-only identity fallback JSON. |
| `--aic-csv` | Per-user AIC consumption CSV export (from the billing UI) — fastest bulk source. |
| `--fetch-membership` | Also fetch each org's member list (one call/org). Default: enterprise SCIM only. |
| `--fetch-org-billing` | Also fetch per-org Copilot billing summary (one call/org) for reconciliation. Default off. |
| `--fetch-org-identities` | Also fetch per-org SAML identities (one call/org). Default: enterprise externalIdentities. |
| `--rollup` | Also write a per-user aggregated rollup CSV. |
| `--allow-partial-scopes` | Warn (don't fail) when optional scopes are missing. |

Copy `config.example.yaml` to get started.

### Enterprise-first API usage

By default the tool uses **enterprise-level** endpoints and avoids per-org calls:
seats (`/enterprises/{ent}/copilot/billing/seats`, one paginated call with per-seat
`organization` attribution), externalIdentities, SCIM, audit log, billing usage, and
per-user AI-credit usage. Per-org membership/billing/identity calls are opt-in via the
flags above. If the enterprise seats endpoint is unavailable, the tool falls back to
discovering orgs and iterating them (skipping orgs that reject the token).

### Per-user AI-credit consumption performance

Per-user AIC consumption comes from
`GET /enterprises/{ent}/settings/billing/ai_credit/usage?user={login}` — one call per
user. For large enterprises this is fetched concurrently (`aic_concurrency`, default 8).
For the fastest bulk load, export the premium-request/AI-credit report from the billing
UI and pass it with `--aic-csv` (also required for accurate historical months).

---

## Required scopes

Preflight fails fast (exit code 3) if a **required** capability's scope is missing,
and warns (degrading that source) for **optional** ones.

| Capability | Data source | Accepted scopes (any) |
|-----------|-------------|-----------------------|
| copilot_seats (required) | Copilot seats + org billing | `manage_billing:copilot` / `read:org` / `admin:org` |
| membership (required) | org discovery + members | `read:org` / `admin:org` / `read:enterprise` |
| billing_usage (optional) | enhanced billing usage ($) | `manage_billing:enterprise` / `read:enterprise` / `repo` |
| audit_log (optional) | assign/revoke history | `read:audit_log` / `admin:enterprise` / `admin:org` |
| identity (optional) | SAML/SCIM resolution | `admin:enterprise` / `read:enterprise` / `scim:enterprise` |

---

## Data dictionary

Required columns (fixed order), then recommended/provenance columns.

| Column | Source / derivation |
|--------|---------------------|
| `user_login` | REAL GitHub login. Precedence: seat.assignee.login → audit-log user_login → externalIdentities user.login → identity_map. Never a SAML NameID/SCIM userName/email. Empty if UNRECOVERABLE (raw id goes to `external_identity`). |
| `license_assigned_date` | `seat.created_at` (UTC `YYYY-MM-DD`); reconstructed interval start for historical months. |
| `gh_copilot_license_cost` | `license_cost_table[plan_type]` (negotiated/config). Org-level actuals from billing-usage are logged. |
| `default_aic_user_level` | Date-aware `default_aic_table[plan_type]` (credits). `default_aic_usd` = credits × `credit_to_usd`. |
| `aic_billing_dollar_assigned` | Per-user budget if configured, else `default_aic_user_level × 0.01`. Rule recorded in `aic_assigned_rule_used`. |
| `aic_consumed` | Per-user consumed AI-credits from the usage report / CSV (`aic_consumed_usd` = ×0.01). `0` if none; empty for historical months with no per-user data. |
| `user_status` | `active` = holds a valid, non-cancelled license; else `inactive` (removed / pending_cancellation / suspended / deprovisioned). |
| `user_revoked_date` | `pending_cancellation_date` if set, else latest `copilot.seat_cancelled` from audit; empty if active/never-revoked. |
| `org_login` | Instance (org) login. |
| `plan_type` | `business` / `enterprise` / `unknown` (normalized). |
| `seat_status` | `active` / `pending_cancellation` / `removed`. |
| `assigned_via` | `direct` or `team:<slug>`. |
| `last_activity_at` | From seat telemetry (informational; not the status driver). |
| `external_identity` | SAML NameID / SCIM userName — captured here ONLY, never in `user_login`. |
| `identity_resolution_source` | `seat` / `audit` / `externalIdentities` / `identity_map` / `snapshot` / `unresolved`. |
| `account_state` | `member` / `suspended` / `deprovisioned`. |
| `aic_assigned_rule_used` | `per_user_budget` / `plan_default`. |
| `currency`, `billing_period` | Currency and `YYYY-MM` period (part of the row key). |
| `row_source` | `live_seats` / `audit_reconstructed` / `snapshot`. |
| `login_recovery_source` | `seat` / `audit_log` / `external_identity` / `identity_map` / `snapshot` / `UNRECOVERABLE`. |
| `history_confidence` | `exact` / `reconstructed` / `aggregate_only` / `unknown`. |
| `as_of_utc` | Timestamp the state represents. |
| `data_quality_notes` | Unresolved/estimated flags per row. |
| `data_generated_at_utc` | Run timestamp. |

---

## Historical / past-month reporting

`GET /orgs/{org}/copilot/billing/seats` is **point-in-time only** and omits removed
users. To report past months the tool builds a longitudinal **seat ledger**:

1. **Ingest** (widest history first): audit archive (unbounded) → audit-log API
   (~180 days) → stored snapshots (exact) → current live seats (exact).
2. **Build intervals** `[assigned_at → revoked_at)` per `(user, org)`; the real login
   is captured at ingest time so it survives later removal.
3. **Materialize** each requested month: a user is licensed if any interval overlaps
   the month's cycle. Snapshots are authoritative (`exact`); otherwise
   `audit_reconstructed`.
4. **Dollars** come from the enhanced billing-usage API for that exact month
   (available ~24 months). Per-user `aic_consumed` for a past month is emitted only if
   a per-user export exists for it — otherwise the cell is empty and
   `history_confidence=aggregate_only` (never fabricated).

**Forward-proofing:** every run WRITES a monthly snapshot to `snapshot_store`, making
any month it has ever processed reproducible forever — independent of the 180-day
window. For >180-day accuracy, enable audit-log streaming to a SIEM/object store and
schedule this tool monthly. Keep `identity_map` append-only so a login seen live stays
resolvable after removal.

The run prints the **earliest reliably-recoverable month** and flags any requested
month that predates it.

### Removed / deprovisioned → real login (hard guarantee)

Resolution order: snapshot → audit event → externalIdentities → identity_map. If all
fail, `user_login` is left empty, `login_recovery_source=UNRECOVERABLE`, the surviving
external id goes to `external_identity`, and a data-quality note is added. The run
summary counts/lists UNRECOVERABLE rows, and reconciliation asserts **zero** rows where
a login contains an external identity.

---

## Outputs

- **CSV** at `output_path` (+ optional per-user rollup).
- **Run log** (`log_path` + `.json`): scopes, orgs scanned, seats, identity resolution
  by source, unresolved list, reconciliation results, API/rate-limit stats.
- **Snapshots** under `snapshot_store/YYYY-MM/snapshot.json`.

## Validation & reconciliation (automatic)

- Active-seat row count per org ≈ org billing `seat_breakdown.total` (± pending).
- Σ per-user `aic_consumed` USD ≈ Copilot net from billing-usage (tolerance).
- 100% of rows have a real `user_login` (count/% + list of exceptions).
- Zero rows where a removed user's `user_login` holds an external identity.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest --cov=copilot_aic_report
```

The pipeline is idempotent, checkpointable, and re-runnable; secrets come from env
vars and are never logged or committed.
