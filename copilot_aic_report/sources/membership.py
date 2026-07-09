"""Membership and account-state source helpers."""
from __future__ import annotations

import sys

from copilot_aic_report.github_client import AuthFailure, GitHubError
from copilot_aic_report.models import AccountState


def fetch_org_members(client, cfg, org_login) -> set[str]:
    """Return lowercased organization member logins."""
    try:
        members = client.paginate(f"/orgs/{org_login}/members")
    except AuthFailure:
        raise
    except GitHubError as exc:
        if exc.status in (403, 404):
            print(
                f"Warning: unable to fetch members for org {org_login}: {exc}",
                file=sys.stderr,
            )
            return set()
        raise

    return {
        str(member["login"]).lower()
        for member in members
        if isinstance(member, dict) and member.get("login")
    }


def _iter_scim_resources(client, cfg):
    """Yield enterprise SCIM user resources (one paginated call), or nothing on
    403/404. Auth failures propagate."""
    enterprise_slug = cfg.enterprise_slug
    try:
        resources = client.paginate(
            f"/scim/v2/enterprises/{enterprise_slug}/Users",
            items_key="Resources",
        )
    except AuthFailure:
        raise
    except GitHubError as exc:
        if exc.status in (403, 404):
            print(
                f"Warning: unable to fetch enterprise SCIM users for {enterprise_slug}: {exc}",
                file=sys.stderr,
            )
            return
        raise
    for resource in resources:
        if isinstance(resource, dict) and resource.get("userName"):
            yield resource


def fetch_scim_active(client, cfg) -> dict:
    """Return lowercased enterprise SCIM userName values mapped to active flags."""
    return {
        str(resource["userName"]).lower(): bool(resource.get("active"))
        for resource in _iter_scim_resources(client, cfg)
    }


def fetch_scim_state(client, cfg) -> tuple[dict, dict]:
    """Return two maps from a single SCIM pass, keyed by lowercased userName:

    * ``active`` — userName -> bool active flag.
    * ``deprovisioned_at`` — userName -> ``meta.lastModified`` timestamp, for
      inactive users only (the effective deprovisioning/revocation date).
    """
    active: dict = {}
    deprovisioned_at: dict = {}
    for resource in _iter_scim_resources(client, cfg):
        key = str(resource["userName"]).lower()
        is_active = bool(resource.get("active"))
        active[key] = is_active
        if not is_active:
            last_modified = (resource.get("meta") or {}).get("lastModified")
            if last_modified:
                deprovisioned_at[key] = last_modified
    return active, deprovisioned_at


def build_account_states(
    org_members_by_org: dict[str, set[str]],
    scim_active: dict[str, bool],
    seat_logins_by_org: dict[str, set[str]],
    scim_deprovisioned_at: dict[str, str] | None = None,
) -> list[AccountState]:
    """Build account states for all Copilot seat holders."""
    scim_deprovisioned_at = scim_deprovisioned_at or {}
    states: list[AccountState] = []
    for org_login, seat_logins in seat_logins_by_org.items():
        org_members = org_members_by_org.get(org_login, set())
        for login in seat_logins:
            normalized_login = login.lower()
            states.append(
                AccountState(
                    user_login=login,
                    org_login=org_login,
                    is_member=normalized_login in org_members,
                    suspended=False,
                    scim_active=scim_active.get(normalized_login),
                    deprovisioned_at=scim_deprovisioned_at.get(normalized_login),
                )
            )
    return states
