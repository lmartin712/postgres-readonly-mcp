"""MCP server entry point (stdio transport)."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .audit import AuditLog
from .config import Config
from .service import ReadOnlyService


def build_server(config: Config) -> FastMCP:
    service = ReadOnlyService(config, AuditLog(config.audit_log))
    mcp = FastMCP("postgres-readonly")

    @mcp.tool()
    def list_tables() -> list[str]:
        """List the tables this server is allowed to query (schema-qualified)."""
        return service.list_tables()

    @mcp.tool()
    def describe_table(name: str) -> list[dict]:
        """Describe the columns of an allow-listed table. Accepts 'table' or 'schema.table'."""
        return service.describe_table(name)

    @mcp.tool()
    def query(sql: str) -> dict:
        """Run one read-only SELECT (WITH ... SELECT is fine) against allow-listed tables.

        Results are capped at the configured row limit; the response says if it was cut off.
        """
        return service.query(sql)

    return mcp


def main() -> None:
    build_server(Config.from_env()).run()  # stdio


if __name__ == "__main__":
    main()
