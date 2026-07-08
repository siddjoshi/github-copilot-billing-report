"""Structured run log for the Copilot AIC report.

Captures: scopes used, orgs scanned, seats found, identity resolution by source,
the unresolved-identity list, reconciliation results, API-call/rate-limit stats,
and any warnings. Writes both a human-readable text log and a machine-readable
JSON sidecar. Secrets are never included.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .util import now_utc_iso


@dataclass
class RunLog:
    started_at: str = field(default_factory=now_utc_iso)
    finished_at: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    scopes: Dict[str, Any] = field(default_factory=dict)
    orgs_scanned: List[str] = field(default_factory=list)
    seats_found: int = 0
    rows_written: int = 0
    rollup_rows_written: int = 0
    resolution_by_source: Dict[str, int] = field(default_factory=dict)
    unresolved_identities: List[Dict[str, Any]] = field(default_factory=list)
    reconciliation: List[Dict[str, Any]] = field(default_factory=list)
    api_stats: Dict[str, Any] = field(default_factory=dict)
    aic_consumption_source: str = "none"
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def bump_resolution(self, source: str) -> None:
        self.resolution_by_source[source] = self.resolution_by_source.get(source, 0) + 1

    def finish(self) -> None:
        self.finished_at = now_utc_iso()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "config": self.config,
            "scopes": self.scopes,
            "orgs_scanned": self.orgs_scanned,
            "seats_found": self.seats_found,
            "rows_written": self.rows_written,
            "rollup_rows_written": self.rollup_rows_written,
            "resolution_by_source": self.resolution_by_source,
            "unresolved_identities": self.unresolved_identities,
            "reconciliation": self.reconciliation,
            "api_stats": self.api_stats,
            "aic_consumption_source": self.aic_consumption_source,
            "warnings": self.warnings,
            "errors": self.errors,
        }

    def render_text(self) -> str:
        d = self.as_dict()
        lines: List[str] = []
        lines.append("=== Copilot AIC Report — Run Log ===")
        lines.append(f"started_at:  {d['started_at']}")
        lines.append(f"finished_at: {d['finished_at']}")
        lines.append(f"billing_period: {d['config'].get('billing_period', '')}")
        lines.append(f"enterprise: {d['config'].get('enterprise_slug', '')}")
        lines.append("")
        lines.append("-- Scopes --")
        lines.append(json.dumps(d["scopes"], indent=2, sort_keys=True))
        lines.append("")
        lines.append(f"orgs_scanned ({len(d['orgs_scanned'])}): {', '.join(d['orgs_scanned'])}")
        lines.append(f"seats_found: {d['seats_found']}")
        lines.append(f"rows_written: {d['rows_written']}")
        lines.append(f"rollup_rows_written: {d['rollup_rows_written']}")
        lines.append(f"aic_consumption_source: {d['aic_consumption_source']}")
        lines.append("")
        lines.append("-- Identity resolution by source --")
        for src, n in sorted(d["resolution_by_source"].items()):
            lines.append(f"  {src}: {n}")
        lines.append(f"unresolved_identities: {len(d['unresolved_identities'])}")
        for item in d["unresolved_identities"]:
            lines.append(f"    - {json.dumps(item, sort_keys=True)}")
        lines.append("")
        lines.append("-- Reconciliation --")
        for check in d["reconciliation"]:
            status = "OK" if check.get("ok") else "MISMATCH"
            lines.append(f"  [{status}] {check.get('name')}: {check.get('detail', '')}")
        lines.append("")
        lines.append("-- API stats --")
        lines.append(json.dumps(d["api_stats"], indent=2, sort_keys=True))
        if d["warnings"]:
            lines.append("")
            lines.append("-- Warnings --")
            for w in d["warnings"]:
                lines.append(f"  ! {w}")
        if d["errors"]:
            lines.append("")
            lines.append("-- Errors --")
            for e in d["errors"]:
                lines.append(f"  X {e}")
        return "\n".join(lines) + "\n"

    def write(self, log_path: str) -> None:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(self.render_text())
        json_path = log_path + ".json" if not log_path.endswith(".json") else log_path
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, indent=2, sort_keys=True)
