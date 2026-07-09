"""GitHub REST + GraphQL client with pagination, rate-limit handling and retries.

Read-only. All mutation verbs are intentionally unsupported. The client is designed
for testability: the underlying HTTP transport (a ``requests.Session``-like object)
and the ``sleep``/``rng`` functions can be injected.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

DEFAULT_ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """Base error for GitHub API interactions."""

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthFailure(GitHubError):
    """401/403 — authentication or scope failure. Fail loudly, never retry blindly."""


class RateLimited(GitHubError):
    """Signalled internally when a 403/429 is a rate-limit condition."""


class GraphQLError(GitHubError):
    """GraphQL responded with an ``errors`` array."""


@dataclass
class ClientStats:
    rest_calls: int = 0
    graphql_calls: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    total_wait_seconds: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rest_calls": self.rest_calls,
            "graphql_calls": self.graphql_calls,
            "retries": self.retries,
            "rate_limit_waits": self.rate_limit_waits,
            "total_wait_seconds": round(self.total_wait_seconds, 3),
        }


@dataclass
class GitHubClient:
    token: str
    api_base: str = "https://api.github.com"
    graphql_url: str = "https://api.github.com/graphql"
    session: Any = None  # requests.Session-like; created lazily if None
    per_page: int = 100
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random
    now: Callable[[], float] = time.time
    stats: ClientStats = field(default_factory=ClientStats)
    partial_graphql_errors: List[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.session is None:
            import requests  # lazy import

            self.session = requests.Session()

    # ---- headers ---------------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Accept": DEFAULT_ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if extra:
            headers.update(extra)
        return headers

    # ---- backoff ---------------------------------------------------------

    def _backoff_seconds(self, attempt: int) -> float:
        raw = self.backoff_base_seconds * (2 ** attempt)
        capped = min(raw, self.backoff_max_seconds)
        return capped * (0.5 + 0.5 * self.rng())  # full-ish jitter

    def _wait_for_rate_limit(self, response) -> Optional[float]:
        """Return seconds to wait if the response is a rate-limit condition, else None."""
        headers = getattr(response, "headers", {}) or {}
        status = response.status_code
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        remaining = headers.get("X-RateLimit-Remaining")
        if status in (403, 429) and remaining is not None and str(remaining) == "0":
            reset = headers.get("X-RateLimit-Reset")
            if reset is not None:
                try:
                    return max(0.0, float(reset) - self.now())
                except (TypeError, ValueError):
                    return self.backoff_base_seconds
            return self.backoff_base_seconds
        return None

    # ---- core request ----------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        if method.upper() not in ("GET", "POST"):
            raise ValueError("Only read-only GET/POST are permitted.")
        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self.max_retries:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(extra_headers),
                timeout=60,
            )
            status = response.status_code

            # Rate limit handling (may present as 403 or 429).
            wait = self._wait_for_rate_limit(response)
            if wait is not None and attempt < self.max_retries:
                self.stats.rate_limit_waits += 1
                self.stats.total_wait_seconds += wait
                self.sleep(wait)
                attempt += 1
                self.stats.retries += 1
                continue

            if status in (401, 403):
                # Distinguish exhausted-retry rate limit from real auth failure.
                raise AuthFailure(
                    f"{status} for {method} {url}: {_safe_text(response)}",
                    status=status,
                    body=_safe_json(response),
                )
            if status >= 500 and attempt < self.max_retries:
                self.stats.retries += 1
                self.sleep(self._backoff_seconds(attempt))
                attempt += 1
                last_exc = GitHubError(f"{status} server error for {url}", status=status)
                continue
            if status >= 400:
                raise GitHubError(
                    f"{status} for {method} {url}: {_safe_text(response)}",
                    status=status,
                    body=_safe_json(response),
                )
            return response

        raise last_exc or GitHubError(f"Exhausted retries for {method} {url}")

    # ---- REST ------------------------------------------------------------

    def _abs_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.api_base.rstrip('/')}/{path.lstrip('/')}"

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.stats.rest_calls += 1
        response = self._request("GET", self._abs_url(path), params=params)
        return _safe_json(response)

    def get_oauth_scopes(self) -> Optional[str]:
        """Return the raw ``X-OAuth-Scopes`` header from a lightweight call."""
        self.stats.rest_calls += 1
        response = self._request("GET", self._abs_url("/rate_limit"))
        return (getattr(response, "headers", {}) or {}).get("X-OAuth-Scopes")

    def paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        items_key: Optional[str] = None,
    ) -> Iterator[Any]:
        """Yield items across all pages, following ``Link`` rel="next" headers.

        ``items_key`` extracts a list from an object response (e.g. seats endpoints
        return ``{"total_seats": N, "seats": [...]}``).
        """
        query = dict(params or {})
        query.setdefault("per_page", self.per_page)
        url = self._abs_url(path)
        while url:
            self.stats.rest_calls += 1
            response = self._request("GET", url, params=query)
            payload = _safe_json(response)
            items = list(_extract_items(payload, items_key))
            for item in items:
                yield item
            next_link = _next_link(getattr(response, "headers", {}) or {})
            if next_link:
                url = next_link
                query = None  # subsequent URLs already carry the cursor
                continue
            # SCIM v2 pagination: no Link header, uses startIndex/count/totalResults.
            if isinstance(payload, dict) and "totalResults" in payload:
                try:
                    total = int(payload.get("totalResults") or 0)
                    start = int(payload.get("startIndex") or 1)
                except (TypeError, ValueError):
                    break
                per = payload.get("itemsPerPage") or len(items) or self.per_page
                try:
                    per = int(per)
                except (TypeError, ValueError):
                    per = self.per_page
                fetched = start - 1 + len(items)
                if items and fetched < total:
                    query = {"startIndex": fetched + 1, "count": per or self.per_page}
                    continue  # re-request the same base URL with the next window
            break

    # ---- GraphQL ---------------------------------------------------------

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.stats.graphql_calls += 1
        response = self._request(
            "POST",
            self.graphql_url,
            json_body={"query": query, "variables": variables or {}},
        )
        payload = _safe_json(response) or {}
        errors = payload.get("errors")
        data = payload.get("data")
        if errors:
            # GitHub returns partial results as ``data`` + per-field ``errors`` (e.g.
            # some enterprise orgs forbid classic-PAT access). Preserve usable data
            # and record the errors; only hard-fail when there is no data at all.
            if data:
                self.partial_graphql_errors.extend(errors)
                return data
            raise GraphQLError(f"GraphQL errors: {errors}", body=errors)
        return data or {}

    def graphql_paginate(
        self,
        query: str,
        variables: Dict[str, Any],
        page_path: List[str],
    ) -> Iterator[Any]:
        """Paginate a GraphQL connection.

        ``page_path`` is the list of keys from the response ``data`` down to the
        connection object that exposes ``pageInfo`` and ``nodes``. ``variables``
        must accept an ``after`` cursor variable named ``after``. ``None`` nodes
        (forbidden/partial entries) are skipped.
        """
        variables = dict(variables)
        variables.setdefault("after", None)
        while True:
            data = self.graphql(query, variables)
            connection = _dig(data, page_path)
            if connection is None:
                return
            for node in connection.get("nodes", []) or []:
                if node is not None:
                    yield node
            page_info = connection.get("pageInfo") or {}
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                variables["after"] = page_info["endCursor"]
            else:
                return


# ---- helpers -------------------------------------------------------------


def _safe_json(response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _safe_text(response) -> str:
    try:
        return (response.text or "")[:500]
    except Exception:
        return ""


def _extract_items(payload: Any, items_key: Optional[str]) -> Iterable[Any]:
    if items_key is not None:
        if isinstance(payload, dict):
            return payload.get(items_key) or []
        return []
    if isinstance(payload, list):
        return payload
    return [payload] if payload is not None else []


def _next_link(headers: Dict[str, str]) -> Optional[str]:
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        segs = part.split(";")
        if len(segs) < 2:
            continue
        url = segs[0].strip().lstrip("<").rstrip(">")
        for attr in segs[1:]:
            if attr.strip() == 'rel="next"':
                return url
    return None


def _dig(data: Dict[str, Any], path: List[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur
