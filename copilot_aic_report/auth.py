"""Authentication and scope preflight for the Copilot AIC report.

The tool is read-only. This module validates that the supplied classic PAT carries
the scopes required by the data sources it will touch, and fails fast with a clear,
actionable message otherwise. Required scopes are surfaced in the run log.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

# Logical capability -> acceptable scope alternatives (any one satisfies it).
# Each capability maps to a list of scope sets; a capability is satisfied if the
# token holds *all* scopes in any one of the alternative sets.
CAPABILITY_SCOPES: Dict[str, List[Set[str]]] = {
    # Copilot seats + org billing summary
    "copilot_seats": [{"manage_billing:copilot"}, {"read:org"}, {"admin:org"}],
    # Enhanced billing usage (dollar amounts)
    "billing_usage": [{"manage_billing:enterprise"}, {"read:enterprise"}, {"repo"}],
    # Audit log (authoritative assign/revoke + real login)
    "audit_log": [{"read:audit_log"}, {"admin:enterprise"}, {"admin:org"}],
    # Enterprise/org membership + org list
    "membership": [{"read:org"}, {"admin:org"}, {"read:enterprise"}],
    # Identity (SAML/SCIM via GraphQL)
    "identity": [{"admin:enterprise"}, {"read:enterprise"}, {"scim:enterprise"}],
}

# Human-readable hint per capability for error messages.
CAPABILITY_HINTS: Dict[str, str] = {
    "copilot_seats": "Copilot seat listing (GET /orgs/{org}/copilot/billing/seats)",
    "billing_usage": "Enhanced billing usage dollar amounts",
    "audit_log": "Audit log (authoritative assign/revoke dates + real login)",
    "membership": "Org discovery and membership/account state",
    "identity": "SAML/SCIM external identity resolution (GraphQL)",
}


class AuthError(Exception):
    """Raised when authentication or required scopes are missing/insufficient."""


@dataclass
class ScopeReport:
    token_scopes: List[str]
    required_capabilities: List[str]
    satisfied: Dict[str, bool]
    missing: Dict[str, List[Set[str]]]

    @property
    def ok(self) -> bool:
        return all(self.satisfied.values())

    def as_dict(self) -> Dict[str, object]:
        return {
            "token_scopes": self.token_scopes,
            "required_capabilities": self.required_capabilities,
            "satisfied": self.satisfied,
            "missing": {k: [sorted(s) for s in v] for k, v in self.missing.items()},
        }


def parse_oauth_scopes(header_value: Optional[str]) -> List[str]:
    """Parse the ``X-OAuth-Scopes`` response header into a list of scopes."""
    if not header_value:
        return []
    return [s.strip() for s in header_value.split(",") if s.strip()]


def _capability_satisfied(alternatives: List[Set[str]], have: Set[str]) -> bool:
    return any(req.issubset(have) for req in alternatives)


def evaluate_scopes(
    token_scopes: Sequence[str],
    required_capabilities: Sequence[str],
) -> ScopeReport:
    """Evaluate whether ``token_scopes`` satisfy each required capability."""
    have = set(token_scopes)
    satisfied: Dict[str, bool] = {}
    missing: Dict[str, List[Set[str]]] = {}
    for cap in required_capabilities:
        alternatives = CAPABILITY_SCOPES.get(cap, [])
        ok = _capability_satisfied(alternatives, have)
        satisfied[cap] = ok
        if not ok:
            missing[cap] = alternatives
    return ScopeReport(
        token_scopes=list(token_scopes),
        required_capabilities=list(required_capabilities),
        satisfied=satisfied,
        missing=missing,
    )


def format_scope_error(report: ScopeReport) -> str:
    lines = ["Missing required token scopes for read-only Copilot AIC report:"]
    for cap, alternatives in report.missing.items():
        hint = CAPABILITY_HINTS.get(cap, cap)
        options = " OR ".join("+".join(sorted(s)) for s in alternatives)
        lines.append(f"  - {cap} ({hint}): need one of [{options}]")
    lines.append(f"Token currently has: {', '.join(report.token_scopes) or '(none)'}")
    return "\n".join(lines)


def require_token(token: Optional[str]) -> str:
    if not token:
        raise AuthError(
            "No GitHub token found. Set one of GITHUB_TOKEN / GH_TOKEN / "
            "COPILOT_AIC_TOKEN in the environment. The token is never read from "
            "config files and never logged."
        )
    return token


def preflight(
    fetch_scopes,
    token: Optional[str],
    required_capabilities: Sequence[str],
    allow_partial: bool = False,
) -> ScopeReport:
    """Run a scope preflight.

    ``fetch_scopes`` is a callable that returns the ``X-OAuth-Scopes`` header value
    (typically wired to the GitHub client). Raises :class:`AuthError` on missing
    token or (unless ``allow_partial``) insufficient scopes.
    """
    require_token(token)
    header = fetch_scopes()
    scopes = parse_oauth_scopes(header)
    report = evaluate_scopes(scopes, required_capabilities)
    if not report.ok and not allow_partial:
        raise AuthError(format_scope_error(report))
    return report
