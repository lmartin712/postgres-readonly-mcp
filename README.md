# postgres-readonly-mcp

A small [MCP](https://modelcontextprotocol.io) server that lets Claude (Claude Desktop or Claude Code) query a PostgreSQL database without being able to change it or read tables you did not approve. It runs locally over stdio.

It exists because "give the assistant database access" usually comes with three conditions: only some tables, a cap on result size, and a record of everything it asked. This project implements exactly those, and keeps the safety logic small enough to read in one sitting (about 150 lines in `validator.py`).

Tools exposed to Claude:

| Tool | What it does |
|---|---|
| `list_tables` | Lists the allow-listed tables that exist in the database. |
| `describe_table(name)` | Column names, types and nullability for one allow-listed table. |
| `query(sql)` | Runs one `SELECT` (or `WITH ... SELECT`) and returns columns, rows, `row_count` and `truncated`. |

## The five safety layers

Each layer works on its own and has its own tests.

1. **Statement check** (`validator.py`, sqlglot). The SQL is parsed with the PostgreSQL dialect. It must be exactly one statement, and a `SELECT`, `WITH ... SELECT` or a set operation (`UNION` and friends). Rejected: INSERT/UPDATE/DELETE/MERGE/DDL/COPY and anything the parser reports as a raw command (VACUUM, SET, ...), multiple statements, `SELECT ... INTO`, locking clauses (`FOR UPDATE`, `FOR SHARE`), data-modifying CTEs, table-valued functions in `FROM`, and a short deny list of functions with side effects or file/network/session access: `pg_sleep*`, `set_config`, `dblink*`, `lo_*`, `pg_read_file`, `pg_read_binary_file`, `pg_stat_file`, `pg_ls_*`, `nextval`, `setval`, `pg_advisory_*`, `pg_terminate_backend`, `query_to_xml` and relatives.
2. **Table allow-list** (`validator.py`). Every table the query references, including those inside subqueries, joins and CTE bodies, must be in `MCP_ALLOWED_TABLES`. CTE names are not treated as tables. Unqualified names mean schema `public`. `pg_catalog`, `information_schema` and database-qualified names are rejected unless you list them.
3. **Read-only transaction with a timeout** (`db.py`). Every query runs in `BEGIN READ ONLY` with `statement_timeout` set for that transaction, and is always rolled back.
4. **Row limit** (`db.py`). The query uses a server-side cursor and fetches at most `MCP_ROW_LIMIT + 1` rows. If the extra row arrives, the response says `"truncated": true`. The user's SQL is not rewritten, so there is no `LIMIT` injection to get wrong.
5. **Audit log** (`audit.py`). Every call, allowed or rejected or failed, is appended as one JSON line: `timestamp`, `tool`, `sql`, `verdict`, `reason`, `row_count`, `duration_ms`.

The outermost layer is not in this code: connect with a dedicated database role that has `SELECT` on the allow-listed tables and nothing else. The demo `seed/init.sql` creates such a role (`mcp_reader`).

## Quick start

Requires Python 3.11+ and Docker (Docker only for the demo database).

```bash
docker compose up -d          # PostgreSQL 16 on localhost:55432, seeded with fictional invoicing data
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"

export PG_DSN="postgresql://mcp_reader:demo_reader_pw@localhost:55432/invoicing_demo"
export MCP_ALLOWED_TABLES="customers,invoices,invoice_lines"
export MCP_AUDIT_LOG="$PWD/logs/audit.jsonl"
postgres-readonly-mcp         # speaks MCP over stdio; normally launched by the client
```

The demo database also contains `staff_salaries`, which is deliberately not allow-listed (and not granted to `mcp_reader`) so you can watch it get rejected.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PG_DSN` | required | libpq connection string or URL. |
| `MCP_ALLOWED_TABLES` | required | Comma-separated. `customers` means `public.customers`; use `sales.invoices` for other schemas. The server refuses to start if empty. |
| `MCP_ROW_LIMIT` | `200` | Maximum rows returned per query. |
| `MCP_STATEMENT_TIMEOUT_MS` | `5000` | Per-statement timeout. |
| `MCP_AUDIT_LOG` | `~/.postgres-readonly-mcp/audit.jsonl` | JSONL audit file. Use an absolute path under Claude Desktop. |

The server reads real environment variables only. `.env.example` lists the values for reference.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings, Developer, Edit Config), using the absolute path of the Python in your virtual environment:

```json
{
  "mcpServers": {
    "postgres-readonly": {
      "command": "/absolute/path/to/postgres-readonly-mcp/.venv/bin/python",
      "args": ["-m", "postgres_readonly_mcp.server"],
      "env": {
        "PG_DSN": "postgresql://mcp_reader:demo_reader_pw@localhost:55432/invoicing_demo",
        "MCP_ALLOWED_TABLES": "customers,invoices,invoice_lines",
        "MCP_ROW_LIMIT": "200",
        "MCP_STATEMENT_TIMEOUT_MS": "5000",
        "MCP_AUDIT_LOG": "/absolute/path/to/logs/audit.jsonl"
      }
    }
  }
}
```

On Windows the command is `C:\\path\\to\\postgres-readonly-mcp\\.venv\\Scripts\\python.exe`. Restart Claude Desktop afterwards. For Claude Code, the equivalent is `claude mcp add postgres-readonly -e PG_DSN=... -e MCP_ALLOWED_TABLES=... -- /path/to/.venv/bin/python -m postgres_readonly_mcp.server`.

### Example prompts

- "Which tables can you see, and what columns does `invoices` have?"
- "Total invoiced amount per customer, highest first."
- "Show open and overdue invoices with the customer name."
- "What is in `staff_salaries`?" (expected: refused, and the refusal is logged)

### Example audit log lines

```json
{"timestamp": "2026-11-09T23:51:20.896+00:00", "tool": "query", "sql": "SELECT name, city FROM customers ORDER BY id", "verdict": "allowed", "reason": null, "row_count": 4, "duration_ms": 40.5}
{"timestamp": "2026-11-09T23:51:20.899+00:00", "tool": "query", "sql": "SELECT * FROM staff_salaries", "verdict": "rejected", "reason": "Table not on the allow-list: public.staff_salaries", "row_count": null, "duration_ms": 0.27}
```

`verdict` is `allowed`, `rejected` (validator said no, database never contacted) or `error` (database error such as a timeout). When a result hits the row limit the entry is `allowed` with reason `truncated at row limit`.

## Tests

```bash
pytest                                   # 83 tests, no database needed; integration tests are skipped
PG_DSN="postgresql://mcp_reader:demo_reader_pw@localhost:55432/invoicing_demo" pytest   # all 94
```

The validator tests cover every rejection class, CTEs, joins, subqueries, schema qualification and quoting. The integration tests also check the two outer layers without going through the validator: a write attempted inside the read-only transaction fails, and the `mcp_reader` role cannot read `staff_salaries` even if the parser were bypassed.

## Limitations

- **The parser checks are a defense layer, not a guarantee.** SQL is a large language and PostgreSQL has many functions; a deny list can miss one, and sqlglot can in principle parse something differently from PostgreSQL. The real boundary is the database role: give it `SELECT` on the allow-listed tables only, no access to `pg_read_server_files`, and no `EXECUTE` on dangerous functions. Treat layers 1 and 2 as protection against mistakes and prompt injection, and layers 3 and 4 as protection against runaway queries.
- Views and functions owned by someone else can read tables the allow-list does not mention. The allow-list governs names in the SQL text, not what a view expands to. Only allow-list views you trust.
- Unqualified names resolve through the role's `search_path`. Set it explicitly for the role if you use more than one schema.
- Table-valued functions in `FROM` (`generate_series`, `unnest`, ...) are rejected outright. This is conservative and costs some convenience.
- The row limit caps what is returned, not what the database reads; the timeout covers that. Very wide rows can still produce large responses.
- Query results are shown to the model. Anything in an allow-listed table can end up in the conversation, so allow-list with that in mind.
- The audit log is a plain local file with no rotation or tamper protection.
- One new connection per call. Fine for interactive use, not for high throughput.
- Tested against PostgreSQL 16 and `mcp` 1.x (`mcp>=1.9,<2` is pinned because 2.x renamed the server API).

## License

MIT, see `LICENSE`.
