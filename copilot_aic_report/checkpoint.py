"""Simple JSON-file checkpointing for resumable, idempotent runs.

The orchestrator records which orgs have been fully fetched and caches their
fetched payloads so a re-run can skip completed work. Checkpointing is optional
(enabled only when ``cfg.checkpoint_path`` is set).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Checkpoint:
    path: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=lambda: {"completed_orgs": [], "cache": {}})

    @classmethod
    def load(cls, path: Optional[str]) -> "Checkpoint":
        cp = cls(path=path)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    cp.data.setdefault("completed_orgs", [])
                    cp.data.setdefault("cache", {})
                    cp.data.update(loaded)
                    cp.data.setdefault("completed_orgs", [])
                    cp.data.setdefault("cache", {})
            except (json.JSONDecodeError, OSError):
                pass
        return cp

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def is_org_complete(self, org: str) -> bool:
        return org in self.data.get("completed_orgs", [])

    def get_org_cache(self, org: str) -> Optional[Dict[str, Any]]:
        return self.data.get("cache", {}).get(org)

    def mark_org_complete(self, org: str, payload: Dict[str, Any]) -> None:
        completed: List[str] = self.data.setdefault("completed_orgs", [])
        if org not in completed:
            completed.append(org)
        self.data.setdefault("cache", {})[org] = payload
        self.save()

    def clear(self) -> None:
        self.data = {"completed_orgs": [], "cache": {}}
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)
