"""SQL validation. Layers 1 and 2: statement shape and table allow-list.

No database access happens here. Every check raises Rejected with a
human-readable reason; the caller logs the reason and returns it to the client.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .config import DEFAULT_SCHEMA

# Functions with side effects or file/network/session access. Matched by exact
# lower-case name, or by the prefixes below.
DENY_FUNCTIONS = frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "set_config",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_read_file", "pg_read_binary_file", "pg_stat_file", "pg_ls_dir",
    "nextval", "setval",
    "query_to_xml", "query_to_xml_and_xmlschema", "cursor_to_xml",
    "table_to_xml", "schema_to_xml", "database_to_xml",
})
DENY_PREFIXES = ("lo_", "dblink", "pg_advisory_", "pg_ls_", "pg_logical_", "pg_replication_")

_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.Command, exp.Copy,
)


class Rejected(Exception):
    """The query was refused. The message is safe to show to the client."""


@dataclass(frozen=True)
class Validated:
    sql: str
    tables: frozenset[tuple[str, str]]


# Layer 1 ------------------------------------------------------------------

def parse_single_select(sql: str) -> exp.Expression:
    """Exactly one statement, and it must be a SELECT (WITH ... SELECT and set operations included)."""
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except SqlglotError as e:
        raise Rejected(f"SQL could not be parsed: {str(e).splitlines()[0]}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise Rejected(f"Exactly one statement is allowed, got {len(statements)}")
    root = statements[0]
    while isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, *_SET_OPERATIONS)):
        raise Rejected(f"Only SELECT statements are allowed, got {type(root).__name__.upper()}")
    return statements[0]


def check_forbidden_constructs(tree: exp.Expression) -> None:
    """Reject DML/DDL anywhere in the tree (e.g. inside a CTE), SELECT INTO and locking clauses."""
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise Rejected(f"{type(node).__name__.upper()} is not allowed")
        if isinstance(node, exp.Into):
            raise Rejected("SELECT ... INTO is not allowed")
        if isinstance(node, exp.Lock):
            raise Rejected("Locking clauses (FOR UPDATE / FOR SHARE) are not allowed")


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def check_functions(tree: exp.Expression) -> None:
    """Reject functions on the deny list, and all table-valued functions."""
    for node in tree.walk():
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise Rejected("Table-valued functions in FROM are not allowed")
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in DENY_FUNCTIONS or name.startswith(DENY_PREFIXES):
                raise Rejected(f"Function {name}() is not allowed")


# Layer 2 ------------------------------------------------------------------

def _ident(identifier: exp.Identifier) -> str:
    # Unquoted identifiers fold to lower case in PostgreSQL; quoted ones are exact.
    return identifier.name if identifier.quoted else identifier.name.lower()


def referenced_tables(tree: exp.Expression) -> frozenset[tuple[str, str]]:
    """All real tables referenced, as (schema, name). CTE names are not tables."""
    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    found: set[tuple[str, str]] = set()
    for table in tree.find_all(exp.Table):
        if table.args.get("catalog"):
            raise Rejected("Database-qualified table names are not allowed")
        name_id = table.this
        if not isinstance(name_id, exp.Identifier):
            continue  # table function; handled by check_functions
        schema_id = table.args.get("db")
        if schema_id is None and name_id.name.lower() in cte_names and not name_id.quoted:
            continue
        schema = _ident(schema_id) if schema_id is not None else DEFAULT_SCHEMA
        found.add((schema, _ident(name_id)))
    return frozenset(found)


def check_tables(tables: frozenset[tuple[str, str]], allowed: frozenset[tuple[str, str]]) -> None:
    denied = sorted(t for t in tables if t not in allowed)
    if denied:
        shown = ", ".join(f"{s}.{n}" for s, n in denied)
        raise Rejected(f"Table not on the allow-list: {shown}")


# All layers ---------------------------------------------------------------

def validate_query(sql: str, allowed: frozenset[tuple[str, str]]) -> Validated:
    if not sql or not sql.strip():
        raise Rejected("Empty query")
    tree = parse_single_select(sql)
    check_forbidden_constructs(tree)
    check_functions(tree)
    tables = referenced_tables(tree)
    check_tables(tables, allowed)
    return Validated(sql=sql, tables=tables)


def validate_table_name(name: str, allowed: frozenset[tuple[str, str]]) -> tuple[str, str]:
    """Parse 'table' or 'schema.table' for describe_table and check the allow-list."""
    try:
        ident = sqlglot.parse_one(name, into=exp.Table, read="postgres")
    except SqlglotError as e:
        raise Rejected("Invalid table name") from e
    if (not isinstance(ident, exp.Table) or not isinstance(ident.this, exp.Identifier)
            or ident.args.get("catalog") or ident.args.get("alias") or ident.args.get("joins")):
        raise Rejected("Invalid table name")
    schema = _ident(ident.args["db"]) if ident.args.get("db") is not None else DEFAULT_SCHEMA
    key = (schema, _ident(ident.this))
    check_tables(frozenset({key}), allowed)
    return key
