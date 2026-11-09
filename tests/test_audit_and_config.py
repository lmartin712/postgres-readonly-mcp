"""Audit log, config and service rejection path. No database needed."""

import json

import pytest

from postgres_readonly_mcp.audit import AuditLog
from postgres_readonly_mcp.config import Config, parse_allowed_tables
from postgres_readonly_mcp.service import ReadOnlyService


def test_audit_appends_jsonl_with_required_fields(tmp_path):
    log = AuditLog(tmp_path / "sub" / "audit.jsonl")
    log.write(tool="query", sql="SELECT 1", verdict="allowed", row_count=1, duration_ms=1.234)
    log.write(tool="query", sql="DROP TABLE x", verdict="rejected", reason="DROP is not allowed")
    lines = (tmp_path / "sub" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(x) for x in lines)
    assert set(first) == {"timestamp", "tool", "sql", "verdict", "reason", "row_count", "duration_ms"}
    assert first["verdict"] == "allowed" and first["row_count"] == 1
    assert second["verdict"] == "rejected" and second["reason"] == "DROP is not allowed"


def test_rejected_queries_are_audited_without_a_database(tmp_path):
    cfg = Config(dsn="postgresql://unused", allowed_tables=parse_allowed_tables("customers"),
                 audit_log=tmp_path / "a.jsonl")
    svc = ReadOnlyService(cfg, AuditLog(cfg.audit_log))
    with pytest.raises(ValueError, match="rejected"):
        svc.query("DELETE FROM customers")
    with pytest.raises(ValueError):
        svc.query("SELECT * FROM staff_salaries")
    with pytest.raises(ValueError):
        svc.describe_table("staff_salaries")
    entries = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert [e["verdict"] for e in entries] == ["rejected"] * 3
    assert [e["tool"] for e in entries] == ["query", "query", "describe_table"]
    assert "staff_salaries" in entries[1]["reason"]


def test_config_defaults_and_parsing():
    cfg = Config.from_env({"PG_DSN": "postgresql://x", "MCP_ALLOWED_TABLES": "a, Sales.B"})
    assert cfg.row_limit == 200 and cfg.statement_timeout_ms == 5000
    assert cfg.allowed_tables == {("public", "a"), ("sales", "b")}


def test_config_overrides(tmp_path):
    cfg = Config.from_env({"PG_DSN": "postgresql://x", "MCP_ALLOWED_TABLES": "a",
                           "MCP_ROW_LIMIT": "5", "MCP_STATEMENT_TIMEOUT_MS": "100",
                           "MCP_AUDIT_LOG": str(tmp_path / "l.jsonl")})
    assert (cfg.row_limit, cfg.statement_timeout_ms) == (5, 100)
    assert cfg.audit_log == tmp_path / "l.jsonl"


@pytest.mark.parametrize("env", [
    {},
    {"PG_DSN": "postgresql://x"},
    {"PG_DSN": "postgresql://x", "MCP_ALLOWED_TABLES": " , "},
    {"PG_DSN": "postgresql://x", "MCP_ALLOWED_TABLES": "a.b.c"},
    {"PG_DSN": "postgresql://x", "MCP_ALLOWED_TABLES": "a", "MCP_ROW_LIMIT": "0"},
])
def test_config_rejects_bad_input(env):
    with pytest.raises(ValueError):
        Config.from_env(env)
