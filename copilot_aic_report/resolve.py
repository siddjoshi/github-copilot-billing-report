"""Identity resolution: map every seat/holder to its REAL GitHub login.

This is the top-priority correctness requirement. ``user_login`` must ALWAYS be the
canonical GitHub handle, NEVER an external SSO/SCIM identity (SAML NameID, SCIM
userName, or email). Resolution precedence:

  1. seat.assignee.login          (source="seat")
  2. audit-log user_login         (source="audit")
  3. externalIdentities lookup    (source="externalIdentities")
  4. IDENTITY_MAP fallback        (source="identity_map")
  5. otherwise                    (source="unresolved"): best-known handle or empty;
     the raw external id is surfaced ONLY in the ``external_identity`` field.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import ExternalIdentity, IdentityMapEntry

# A suspended/deprovisioned EMU account often surfaces with a GUID-form login
# (a UUID, optionally followed by an ``_shortcode`` suffix) instead of a real handle.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _norm(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


@dataclass
class Resolution:
    user_login: Optional[str]
    source: str  # seat | audit | externalIdentities | identity_map | unresolved
    external_identity: Optional[str] = None  # raw external id, ONLY when relevant

    @property
    def resolved(self) -> bool:
        return bool(self.user_login) and self.source != "unresolved"


class IdentityResolver:
    """Resolves real GitHub logins using precedence rules.

    ``identity_index`` maps normalized external ids (SAML nameId / SCIM userName /
    email, lowercased) -> real login. ``identity_map`` is the optional user-supplied
    fallback list. Both are merged; the externalIdentities index takes precedence
    over the identity_map for the same key.
    """

    def __init__(
        self,
        identity_index: Optional[Dict[str, str]] = None,
        identity_map: Optional[List[IdentityMapEntry]] = None,
    ):
        self._external: Dict[str, str] = dict(identity_index or {})
        self._map: Dict[str, str] = {}
        for entry in identity_map or []:
            login = entry.github_login
            if not login:
                continue
            for key in (entry.scim_username, entry.saml_name_id, entry.email):
                nk = _norm(key)
                if nk and nk not in self._map:
                    self._map[nk] = login

    # -- lookups ----------------------------------------------------------

    def lookup_external(self, external_id: Optional[str]) -> Optional[str]:
        nk = _norm(external_id)
        if not nk:
            return None
        return self._external.get(nk)

    def lookup_map(self, external_id: Optional[str]) -> Optional[str]:
        nk = _norm(external_id)
        if not nk:
            return None
        return self._map.get(nk)

    # -- resolution -------------------------------------------------------

    def resolve(
        self,
        seat_login: Optional[str] = None,
        audit_login: Optional[str] = None,
        external_id: Optional[str] = None,
    ) -> Resolution:
        """Resolve a holder to a real login following the precedence rules."""
        if seat_login:
            return Resolution(user_login=canonicalize_login(seat_login), source="seat")
        if audit_login:
            return Resolution(user_login=canonicalize_login(audit_login), source="audit")

        ext = self.lookup_external(external_id)
        if ext:
            return Resolution(
                user_login=canonicalize_login(ext),
                source="externalIdentities",
                external_identity=external_id,
            )
        mapped = self.lookup_map(external_id)
        if mapped:
            return Resolution(
                user_login=canonicalize_login(mapped),
                source="identity_map",
                external_identity=external_id,
            )
        # Unresolved: never leak the external id into user_login.
        return Resolution(user_login=None, source="unresolved", external_identity=external_id)


def canonicalize_login(login: Optional[str]) -> Optional[str]:
    """Return the canonical GitHub login.

    EMU logins carry a ``_shortcode`` suffix (e.g. ``mona_acme``); per configuration
    we keep the canonical login WITH the suffix. This function trims surrounding
    whitespace and strips a leading ``@`` if present, but otherwise preserves the
    handle exactly (including the EMU suffix).
    """
    if not login:
        return None
    text = str(login).strip()
    if text.startswith("@"):
        text = text[1:]
    return text or None


def emu_shortname(login: Optional[str]) -> Optional[str]:
    """Return the EMU short name (portion before the last ``_``), if it looks like
    an EMU login; otherwise the login unchanged. Provided for optional emission."""
    login = canonicalize_login(login)
    if not login or "_" not in login:
        return login
    return login.rsplit("_", 1)[0]


def looks_like_external_id(value: Optional[str]) -> bool:
    """Heuristic: does ``value`` look like an email / NameID rather than a login?"""
    if not value:
        return False
    return "@" in value or " " in value or is_placeholder_login(value)


def is_placeholder_login(login: Optional[str]) -> bool:
    """True if ``login`` is a GUID-form placeholder (suspended/deprovisioned EMU
    account) rather than a real GitHub handle. Matches a UUID anywhere in the
    string, so ``<uuid>`` and ``<uuid>_shortcode`` are both detected."""
    if not login:
        return False
    return bool(_UUID_RE.search(str(login)))


def extract_guid(login: Optional[str]) -> Optional[str]:
    """Return the bare UUID from a GUID-form login (dropping any ``_shortcode``)."""
    if not login:
        return None
    match = _UUID_RE.search(str(login))
    return match.group(0) if match else None
