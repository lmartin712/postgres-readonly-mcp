"""Layer 5: append-only JSONL audit log. Allowed and rejected calls are both recorded."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, *, tool: str, sql: str, verdict: str, reason: str | None = None,
              row_count: int | None = None, duration_ms: float | None = None) -> dict:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "sql": sql,
            "verdict": verdict,  # "allowed" | "rejected" | "error"
            "reason": reason,
            "row_count": row_count,
            "duration_ms": None if duration_ms is None else round(duration_ms, 2),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return entry
