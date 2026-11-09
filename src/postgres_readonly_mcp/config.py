"""Configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SCHEMA = "public"


def parse_allowed_tables(raw: str) -> frozenset[tuple[str, str]]:
    """Turn 'customers, sales.invoices' into {('public','customers'), ('sales','invoices')}."""
    result: set[tuple[str, str]] = set()
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        parts = item.split(".")
        if len(parts) == 1:
            result.add((DEFAULT_SCHEMA, parts[0]))
        elif len(parts) == 2 and all(parts):
            result.add((parts[0], parts[1]))
        else:
            raise ValueError(f"Invalid table in MCP_ALLOWED_TABLES: {item!r}")
    return frozenset(result)


@dataclass(frozen=True)
class Config:
    dsn: str
    allowed_tables: frozenset[tuple[str, str]]
    row_limit: int = 200
    statement_timeout_ms: int = 5000
    audit_log: Path = Path.home() / ".postgres-readonly-mcp" / "audit.jsonl"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ) if env is None else env
        dsn = env.get("PG_DSN", "").strip()
        if not dsn:
            raise ValueError("PG_DSN is not set")
        allowed = parse_allowed_tables(env.get("MCP_ALLOWED_TABLES", ""))
        if not allowed:
            raise ValueError("MCP_ALLOWED_TABLES is empty: refusing to start with no allowed tables")
        row_limit = int(env.get("MCP_ROW_LIMIT", "200"))
        timeout = int(env.get("MCP_STATEMENT_TIMEOUT_MS", "5000"))
        if row_limit < 1 or timeout < 1:
            raise ValueError("MCP_ROW_LIMIT and MCP_STATEMENT_TIMEOUT_MS must be positive")
        kwargs = {}
        if env.get("MCP_AUDIT_LOG"):
            kwargs["audit_log"] = Path(env["MCP_AUDIT_LOG"]).expanduser()
        return cls(dsn=dsn, allowed_tables=allowed, row_limit=row_limit,
                   statement_timeout_ms=timeout, **kwargs)
