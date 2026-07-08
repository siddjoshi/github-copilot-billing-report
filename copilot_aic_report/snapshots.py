"""Durable monthly snapshot store (SNAPSHOT_STORE).

On every run the tool WRITES a snapshot of the fully-resolved seat state for each
reported month, so any month it has ever processed is reproducible forever —
independent of the ~180-day audit-log API window. Snapshots are also READ back as
the authoritative "exact" state for historical materialization.

Layout: ``{store}/{YYYY-MM}/snapshot.json`` containing::

    {
      "billing_period": "2026-03",
      "generated_at_utc": "...",
      "records": [ {resolved seat record}, ... ]
    }
"""
from __future__ import annotations

import glob
import json
import os
import re
from typing import Any, Dict, List, Optional

from .util import now_utc_iso

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _month_path(store: str, period: str) -> str:
    return os.path.join(store, period, "snapshot.json")


def list_snapshot_months(store: Optional[str]) -> List[str]:
    """Return sorted ``YYYY-MM`` months that have a stored snapshot."""
    if not store or not os.path.isdir(store):
        return []
    months: List[str] = []
    for entry in os.listdir(store):
        if _MONTH_RE.match(entry) and os.path.exists(_month_path(store, entry)):
            months.append(entry)
    return sorted(months)


def earliest_snapshot_month(store: Optional[str]) -> Optional[str]:
    months = list_snapshot_months(store)
    return months[0] if months else None


def read_snapshot(store: Optional[str], period: str) -> Optional[Dict[str, Any]]:
    """Read a month's snapshot payload, or None if absent/unreadable."""
    if not store:
        return None
    path = _month_path(store, period)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def read_snapshot_records(store: Optional[str], period: str) -> Optional[List[Dict[str, Any]]]:
    payload = read_snapshot(store, period)
    if payload is None:
        return None
    records = payload.get("records")
    return records if isinstance(records, list) else []


def write_snapshot(
    store: Optional[str],
    period: str,
    records: List[Dict[str, Any]],
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Write (atomically) a month's snapshot. Returns the path, or None if disabled."""
    if not store:
        return None
    month_dir = os.path.join(store, period)
    os.makedirs(month_dir, exist_ok=True)
    payload: Dict[str, Any] = {
        "billing_period": period,
        "generated_at_utc": now_utc_iso(),
        "records": records,
    }
    if meta:
        payload["meta"] = meta
    path = _month_path(store, period)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path
