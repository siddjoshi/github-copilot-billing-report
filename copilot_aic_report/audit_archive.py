"""Audit-archive ingestion (AUDIT_ARCHIVE).

The audit-log API only retains ~180 days. To reconstruct per-user seat history
beyond that, streamed audit logs (SIEM / object-store exports) are ingested here.
Supports a single JSON array file, a JSONL (newline-delimited) file, or a directory
containing such files (recursively). Events are normalized to :class:`AuditEvent`
and filtered to the Copilot seat lifecycle actions.
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, Iterator, List, Optional

from .models import AuditEvent

SEAT_ACTIONS = {
    "copilot.seat_assigned",
    "copilot.seat_cancelled",
    "copilot.seat_refresh",
}


def _iter_records_from_text(text: str) -> Iterator[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return
    # Try a single JSON document first (array or object).
    try:
        doc = json.loads(text)
        if isinstance(doc, list):
            for item in doc:
                if isinstance(item, dict):
                    yield item
            return
        if isinstance(doc, dict):
            # Some exports wrap events under a key.
            for key in ("events", "records", "data"):
                if isinstance(doc.get(key), list):
                    for item in doc[key]:
                        if isinstance(item, dict):
                            yield item
                    return
            yield doc
            return
    except json.JSONDecodeError:
        pass
    # Fall back to JSONL.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                yield item
        except json.JSONDecodeError:
            continue


def _iter_files(path: str) -> Iterator[str]:
    if os.path.isdir(path):
        for pattern in ("*.json", "*.jsonl", "*.ndjson", "*.log"):
            for f in sorted(glob.glob(os.path.join(path, "**", pattern), recursive=True)):
                yield f
    elif os.path.exists(path):
        yield path


def _extract_login(event: Dict[str, Any]) -> Optional[str]:
    # Prefer the seat holder's identity; never fall back to ``actor`` (the admin
    # who performed the action) for seat_assigned/cancelled events.
    for key in ("user", "user_login", "userLogin", "assignee"):
        val = event.get(key)
        if isinstance(val, dict):
            login = val.get("login")
            if login:
                return login
        elif isinstance(val, str) and val:
            return val
    return None


def _extract_org(event: Dict[str, Any]) -> Optional[str]:
    for key in ("org", "organization", "business", "org_login"):
        val = event.get(key)
        if isinstance(val, dict):
            login = val.get("login") or val.get("name")
            if login:
                return login
        elif isinstance(val, str) and val:
            return val
    return None


def _to_event(raw: Dict[str, Any]) -> Optional[AuditEvent]:
    action = raw.get("action")
    if action not in SEAT_ACTIONS:
        return None
    ts = raw.get("@timestamp", raw.get("timestamp"))
    try:
        ts_ms = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts_ms = None
    return AuditEvent(
        action=action,
        user_login=_extract_login(raw),
        org_login=_extract_org(raw),
        timestamp_ms=ts_ms,
        raw=raw,
    )


def load_archive_events(path: Optional[str]) -> List[AuditEvent]:
    """Load and normalize Copilot seat events from the archive path (file or dir)."""
    if not path:
        return []
    events: List[AuditEvent] = []
    for file_path in _iter_files(path):
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for raw in _iter_records_from_text(text):
            event = _to_event(raw)
            if event is not None:
                events.append(event)
    return events


def archive_start_month(events: List[AuditEvent]) -> Optional[str]:
    """Earliest ``YYYY-MM`` present in the archive events (by @timestamp)."""
    import datetime as _dt

    stamps = [e.timestamp_ms for e in events if e.timestamp_ms is not None]
    if not stamps:
        return None
    dt = _dt.datetime.fromtimestamp(min(stamps) / 1000.0, tz=_dt.timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"
