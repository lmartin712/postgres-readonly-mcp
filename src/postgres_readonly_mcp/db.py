"""Layers 3 and 4: read-only transaction, statement timeout, row cap."""

from __future__ import annotations

import psycopg

_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
"""


def _connect(dsn: str, timeout_ms: int) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=False, connect_timeout=10)
    conn.read_only = True  # the transaction starts with BEGIN READ ONLY
    with conn.cursor() as cur:
        # is_local=true -> like SET LOCAL, valid for this transaction only.
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
    return conn


def fetch_limited(dsn: str, sql: str, *, row_limit: int, timeout_ms: int):
    """Run sql in a READ ONLY transaction. Fetch at most row_limit + 1 rows.

    Uses a server-side cursor, so a huge result set is never pulled into memory.
    Returns (columns, rows, truncated).
    """
    conn = _connect(dsn, timeout_ms)
    try:
        with conn.cursor(name="mcp_query") as cur:
            cur.execute(sql)
            rows = cur.fetchmany(row_limit + 1)
            columns = [d.name for d in (cur.description or [])]
    finally:
        conn.rollback()
        conn.close()
    truncated = len(rows) > row_limit
    return columns, rows[:row_limit], truncated


def fetch_columns(dsn: str, schema: str, table: str, *, timeout_ms: int):
    conn = _connect(dsn, timeout_ms)
    try:
        with conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (schema, table))
            return cur.fetchall()
    finally:
        conn.rollback()
        conn.close()


def fetch_existing_tables(dsn: str, allowed: frozenset[tuple[str, str]], *, timeout_ms: int):
    conn = _connect(dsn, timeout_ms)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_type IN ('BASE TABLE', 'VIEW')")
            return sorted(t for t in cur.fetchall() if tuple(t) in allowed)
    finally:
        conn.rollback()
        conn.close()
