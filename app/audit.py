"""Append-only audit trail for admin actions (who, what, when)."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("audit")


class AuditLog:
    def __init__(self, path: str | None, keep: int = 1000) -> None:
        self.path = path
        self._recent: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def record(self, actor: str, action: str, target: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "details": details or {},
        }
        with self._lock:
            self._recent.append(entry)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
        log.info("admin action", extra={"audit": entry})
        return entry

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent)[-limit:][::-1]
