"""Validator tests. No database needed."""

import pytest

from postgres_readonly_mcp.config import parse_allowed_tables
from postgres_readonly_mcp.validator import (
    Rejected, check_functions, check_forbidden_constructs, check_tables,
    parse_single_select, referenced_tables, validate_query, validate_table_name,
)

ALLOWED = parse_allowed_tables("customers, invoices, invoice_lines, sales.orders")


# Layer 1: statement shape --------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from customers",
    "SELECT * FROM customers;",
    "WITH c AS (SELECT * FROM customers) SELECT * FROM c",
    "(SELECT id FROM customers)",
    "SELECT id FROM customers UNION ALL SELECT id FROM invoices",
    "-- comment\nSELECT 1",
])
def test_shape_accepts(sql):
    parse_single_select(sql)


@pytest.mark.parametrize("sql, fragment", [
    ("INSERT INTO customers (name) VALUES ('x')", "INSERT"),
    ("UPDATE customers SET name = 'x'", "UPDATE"),
    ("DELETE FROM customers", "DELETE"),
    ("DROP TABLE customers", "DROP"),
    ("CREATE TABLE t (id int)", "CREATE"),
    ("ALTER TABLE customers ADD COLUMN x int", "ALTER"),
    ("TRUNCATE customers", "TRUNCATE"),
    ("VACUUM", "ONLY SELECT"),
    ("SET statement_timeout = 0", ""),
    ("COPY customers TO '/tmp/x'", ""),
])
def test_shape_rejects_non_select(sql, fragment):
    with pytest.raises(Rejected) as e:
        validate_query(sql, ALLOWED)
    assert fragment in str(e.value).upper()


def test_rejects_multiple_statements():
    with pytest.raises(Rejected, match="Exactly one statement"):
        validate_query("SELECT 1; SELECT 2", ALLOWED)


def test_rejects_select_followed_by_drop():
    with pytest.raises(Rejected, match="Exactly one statement"):
        validate_query("SELECT 1; DROP TABLE customers", ALLOWED)


@pytest.mark.parametrize("sql", ["", "   ", ";", "-- only a comment"])
def test_rejects_empty(sql):
    with pytest.raises(Rejected):
        validate_query(sql, ALLOWED)


def test_rejects_unparseable():
    with pytest.raises(Rejected):
        validate_query("SELECT FROM WHERE ((", ALLOWED)


def test_rejects_select_into():
    with pytest.raises(Rejected, match="INTO"):
        validate_query("SELECT * INTO backup FROM customers", ALLOWED)


@pytest.mark.parametrize("clause", ["FOR UPDATE", "FOR SHARE", "FOR NO KEY UPDATE"])
def test_rejects_locking_clauses(clause):
    with pytest.raises(Rejected, match="Locking"):
        validate_query(f"SELECT * FROM customers {clause}", ALLOWED)


def test_rejects_data_modifying_cte():
    sql = "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d"
    with pytest.raises(Rejected, match="DELETE"):
        validate_query(sql, ALLOWED)


@pytest.mark.parametrize("call", [
    "pg_sleep(10)",
    "PG_SLEEP(10)",
    "pg_catalog.pg_sleep(10)",
    "dblink('host=x', 'select 1')",
    "dblink_exec('host=x', 'drop table t')",
    "lo_import('/etc/passwd')",
    "lo_export(1, '/tmp/x')",
    "pg_read_file('/etc/passwd')",
    "pg_read_binary_file('/etc/passwd')",
    "set_config('statement_timeout', '0', false)",
    "nextval('seq')",
    "query_to_xml('delete from customers', true, true, '')",
    "pg_advisory_lock(1)",
    "pg_terminate_backend(1)",
])
def test_rejects_denied_functions(call):
    with pytest.raises(Rejected, match="not allowed"):
        validate_query(f"SELECT {call}", ALLOWED)


def test_rejects_denied_function_inside_where():
    with pytest.raises(Rejected, match="pg_sleep"):
        validate_query("SELECT * FROM customers WHERE id = 1 AND pg_sleep(1) IS NOT NULL", ALLOWED)


def test_rejects_table_function_in_from():
    with pytest.raises(Rejected, match="Table-valued"):
        validate_query("SELECT * FROM generate_series(1, 10)", ALLOWED)


def test_layer1_functions_checked_independently():
    tree = parse_single_select("SELECT pg_sleep(1)")
    with pytest.raises(Rejected):
        check_functions(tree)
    check_forbidden_constructs(tree)  # a different layer: no complaint here


def test_allows_ordinary_functions():
    validate_query("SELECT count(*), max(id), coalesce(name, 'x'), now() FROM customers", ALLOWED)


# Layer 2: table allow-list -------------------------------------------------

def test_allows_listed_table():
    v = validate_query("SELECT * FROM customers", ALLOWED)
    assert v.tables == {("public", "customers")}


def test_rejects_unlisted_table():
    with pytest.raises(Rejected, match="staff_salaries"):
        validate_query("SELECT * FROM staff_salaries", ALLOWED)


def test_allows_join_of_listed_tables():
    sql = """SELECT c.name, sum(l.quantity * l.unit_price)
             FROM customers c JOIN invoices i ON i.customer_id = c.id
             JOIN invoice_lines l ON l.invoice_id = i.id GROUP BY c.name"""
    v = validate_query(sql, ALLOWED)
    assert v.tables == {("public", "customers"), ("public", "invoices"), ("public", "invoice_lines")}


def test_rejects_join_with_one_unlisted_table():
    with pytest.raises(Rejected, match="staff_salaries"):
        validate_query("SELECT * FROM customers c JOIN staff_salaries s ON s.id = c.id", ALLOWED)


def test_rejects_unlisted_table_in_subquery():
    with pytest.raises(Rejected, match="staff_salaries"):
        validate_query("SELECT * FROM customers WHERE id IN (SELECT id FROM staff_salaries)", ALLOWED)


def test_rejects_unlisted_table_in_cte_body():
    with pytest.raises(Rejected, match="staff_salaries"):
        validate_query("WITH s AS (SELECT * FROM staff_salaries) SELECT * FROM s", ALLOWED)


def test_rejects_unlisted_table_in_union():
    with pytest.raises(Rejected, match="staff_salaries"):
        validate_query("SELECT id FROM customers UNION SELECT id FROM staff_salaries", ALLOWED)


def test_cte_name_is_not_a_table():
    v = validate_query("WITH recent AS (SELECT * FROM invoices) SELECT * FROM recent", ALLOWED)
    assert v.tables == {("public", "invoices")}


def test_cte_name_may_shadow_unlisted_table_name():
    sql = "WITH staff_salaries AS (SELECT 1 AS x) SELECT * FROM staff_salaries"
    assert validate_query(sql, ALLOWED).tables == frozenset()


def test_schema_qualified_listed_table():
    assert validate_query("SELECT * FROM sales.orders", ALLOWED).tables == {("sales", "orders")}


def test_schema_mismatch_is_rejected():
    with pytest.raises(Rejected, match="other.customers"):
        validate_query("SELECT * FROM other.customers", ALLOWED)


def test_unqualified_name_is_public_only():
    # "orders" is only allowed as sales.orders, not as public.orders
    with pytest.raises(Rejected, match="public.orders"):
        validate_query("SELECT * FROM orders", ALLOWED)


def test_public_prefix_matches_unqualified_entry():
    validate_query("SELECT * FROM public.customers", ALLOWED)


def test_catalog_views_are_not_allowed():
    for t in ("pg_catalog.pg_shadow", "information_schema.tables", "pg_user"):
        with pytest.raises(Rejected):
            validate_query(f"SELECT * FROM {t}", ALLOWED)


def test_database_qualified_name_rejected():
    with pytest.raises(Rejected, match="Database-qualified"):
        validate_query("SELECT * FROM otherdb.public.customers", ALLOWED)


def test_quoted_identifier_is_case_exact():
    validate_query('SELECT * FROM "customers"', ALLOWED)
    with pytest.raises(Rejected):
        validate_query('SELECT * FROM "Customers"', ALLOWED)


def test_unquoted_uppercase_folds_to_lowercase():
    validate_query("SELECT * FROM CUSTOMERS", ALLOWED)


def test_layer2_checked_independently():
    tree = parse_single_select("SELECT * FROM staff_salaries")
    check_forbidden_constructs(tree)  # layer 1 is satisfied
    check_functions(tree)
    with pytest.raises(Rejected):
        check_tables(referenced_tables(tree), ALLOWED)


# describe_table name -------------------------------------------------------

def test_describe_name_accepts():
    assert validate_table_name("customers", ALLOWED) == ("public", "customers")
    assert validate_table_name("sales.orders", ALLOWED) == ("sales", "orders")


@pytest.mark.parametrize("name", [
    "staff_salaries", "pg_catalog.pg_shadow", "customers; DROP TABLE customers",
    "customers c", "a.b.c", "", "customers JOIN invoices", "generate_series(1,2)",
])
def test_describe_name_rejects(name):
    with pytest.raises(Rejected):
        validate_table_name(name, ALLOWED)
