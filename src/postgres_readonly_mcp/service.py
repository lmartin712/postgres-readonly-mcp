"""The three tool implementations: validation, execution and audit in one place."""

from __future__ import annotations

import time
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any

import psycopg

from . import db
from .audit import AuditLog
from .config import Config
from .validator import Rejected, validate_query, validate_table_name


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)  # keep exact precision
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _short(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    msg = diag.message_primary if diag is not None and diag.message_primary else str(e)
    return msg.splitlines()[0]


class ReadOnlyService:
    def __init__(self, config: Config, audit: AuditLog):
        self.config = config
        self.audit = audit

    def list_tables(self) -> list[str]:
        start = time.perf_counter()
        try:
            rows = db.fetch_existing_tables(self.config.dsn, self.config.allowed_tables,
                                            timeout_ms=self.config.statement_timeout_ms)
        except psycopg.Error as e:
            self._log("list_tables", "", "error", _short(e), None, start)
            raise RuntimeError(f"Database error: {_short(e)}") from None
        names = [f"{s}.{t}" for s, t in rows]
        self._log("list_tables", "", "allowed", None, len(names), start)
        return names

    def describe_table(self, name: str) -> list[dict]:
        start = time.perf_counter()
        try:
            schema, table = validate_table_name(name, self.config.allowed_tables)
        except Rejected as e:
            self._log("describe_table", name, "rejected", str(e), None, start)
            raise ValueError(f"Rejected: {e}") from None
        try:
            rows = db.fetch_columns(self.config.dsn, schema, table,
                                    timeout_ms=self.config.statement_timeout_ms)
        except psycopg.Error as e:
            self._log("describe_table", name, "error", _short(e), None, start)
            raise RuntimeError(f"Database error: {_short(e)}") from None
        if not rows:
            self._log("describe_table", name, "error", "table not found", 0, start)
            raise ValueError(f"Table {schema}.{table} not found")
        self._log("describe_table", name, "allowed", None, len(rows), start)
        return [{"column": c, "type": t, "nullable": n == "YES"} for c, t, n in rows]

    def query(self, sql: str) -> dict:
        start = time.perf_counter()
        try:
            validate_query(sql, self.config.allowed_tables)
        except Rejected as e:
            self._log("query", sql, "rejected", str(e), None, start)
            raise ValueError(f"Query rejected: {e}") from None
        try:
            columns, rows, truncated = db.fetch_limited(
                self.config.dsn, sql, row_limit=self.config.row_limit,
                timeout_ms=self.config.statement_timeout_ms)
        except psycopg.Error as e:
            self._log("query", sql, "error", _short(e), None, start)
            raise RuntimeError(f"Database error: {_short(e)}") from None
        self._log("query", sql, "allowed", "truncated at row limit" if truncated else None,
                  len(rows), start)
        result = {
            "columns": columns,
            "rows": [[_jsonable(v) for v in r] for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }
        if truncated:
            result["note"] = (f"Result cut off at {self.config.row_limit} rows. "
                              "Add filters or aggregation to see the rest.")
        return result

    def _log(self, tool, sql, verdict, reason, row_count, start):
        self.audit.write(tool=tool, sql=sql, verdict=verdict, reason=reason,
                         row_count=row_count, duration_ms=(time.perf_counter() - start) * 1000)
