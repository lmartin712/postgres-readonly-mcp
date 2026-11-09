"""Integration tests. Skipped unless PG_DSN is set (see README: docker compose up -d)."""

import json
import os

import psycopg
import pytest

from postgres_readonly_mcp import db
from postgres_readonly_mcp.audit import AuditLog
from postgres_readonly_mcp.config import Config, parse_allowed_tables
from postgres_readonly_mcp.service import ReadOnlyService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("PG_DSN"), reason="PG_DSN not set"),
]


@pytest.fixture
def svc(tmp_path):
    cfg = Config(dsn=os.environ["PG_DSN"],
                 allowed_tables=parse_allowed_tables("customers,invoices,invoice_lines"),
                 row_limit=3, statement_timeout_ms=500, audit_log=tmp_path / "audit.jsonl")
    return ReadOnlyService(cfg, AuditLog(cfg.audit_log))


def _log(svc):
    return [json.loads(x) for x in svc.audit.path.read_text(encoding="utf-8").splitlines()]


def test_list_tables_only_allow_listed(svc):
    assert svc.list_tables() == ["public.customers", "public.invoice_lines", "public.invoices"]


def test_describe_table(svc):
    cols = svc.describe_table("customers")
    assert [c["column"] for c in cols] == ["id", "name", "city", "created_at"]


def test_describe_unlisted_table_rejected(svc):
    with pytest.raises(ValueError):
        svc.describe_table("staff_salaries")


def test_query_returns_rows(svc):
    r = svc.query("SELECT name FROM customers ORDER BY id LIMIT 2")
    assert r["rows"] == [["Acme Kft."], ["Example Ltd."]]
    assert r["truncated"] is False


def test_row_limit_truncates_and_reports(svc):
    r = svc.query("SELECT id FROM invoice_lines ORDER BY id")
    assert r["row_count"] == 3 and r["truncated"] is True
    assert _log(svc)[-1]["row_count"] == 3


def test_row_count_exactly_at_limit_is_not_truncated(svc):
    r = svc.query("SELECT id FROM customers ORDER BY id LIMIT 3")
    assert r["row_count"] == 3 and r["truncated"] is False


def test_join_and_cte(svc):
    r = svc.query("""WITH t AS (SELECT invoice_id, sum(quantity * unit_price) AS total
                              FROM invoice_lines GROUP BY invoice_id)
                     SELECT c.name, t.total FROM t JOIN invoices i ON i.id = t.invoice_id
                     JOIN customers c ON c.id = i.customer_id ORDER BY t.total DESC LIMIT 1""")
    assert r["rows"][0][0] == "Placeholder Zrt."  # invoice 7: 40 x 110.00


def test_unlisted_table_rejected_and_logged(svc):
    with pytest.raises(ValueError):
        svc.query("SELECT * FROM staff_salaries")
    assert _log(svc)[-1]["verdict"] == "rejected"


def test_statement_timeout_applies(svc):
    # A self cross join of an allow-listed table is slow enough to hit the 500 ms timeout.
    sql = ("SELECT count(*) FROM invoice_lines a, invoice_lines b, invoice_lines c, "
           "invoice_lines d, invoice_lines e, invoice_lines f, invoice_lines g, invoice_lines h")
    with pytest.raises(RuntimeError, match="timeout"):
        svc.query(sql)
    assert _log(svc)[-1]["verdict"] == "error"


# Layer 3 on its own: bypass the validator and hit the database directly.

def test_read_only_transaction_blocks_writes_without_the_validator(svc):
    conn = db._connect(svc.config.dsn, 1000)
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cur.execute("CREATE TEMP TABLE x (id int)")
    finally:
        conn.rollback()
        conn.close()


def test_database_role_blocks_unlisted_table_without_the_validator(svc):
    # With the demo role (mcp_reader) the real boundary is the GRANT, not the parser.
    if "mcp_reader" not in svc.config.dsn:
        pytest.skip("PG_DSN does not use the mcp_reader role")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.fetch_limited(svc.config.dsn, "SELECT * FROM staff_salaries", row_limit=1, timeout_ms=1000)
