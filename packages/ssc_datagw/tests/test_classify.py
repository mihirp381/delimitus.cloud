"""The classifier the Postgres connector runs first (SSC-051): one plain read passes, anything
else is refused, including the forms where sqlglot and Postgres would read the text apart."""

import pytest

from ssc_datagw.classify import refusal

READS = [
    "SELECT 1",
    "select id, amount from reporting.orders where id = $1",
    "SELECT * FROM reporting.orders ORDER BY id LIMIT 10 OFFSET 5",
    "SELECT o.id, c.name FROM reporting.orders o JOIN reporting.customers c ON c.id = o.customer",
    "WITH recent AS (SELECT * FROM reporting.orders WHERE placed > now() - interval '7 days') "
    "SELECT count(*) FROM recent",
    "SELECT id FROM reporting.a UNION ALL SELECT id FROM reporting.b",
    "SELECT id FROM reporting.a EXCEPT SELECT id FROM reporting.b",
    "(SELECT 1)",
    "SELECT date_trunc('month', placed), sum(amount) FROM reporting.orders GROUP BY 1",
    "SELECT meta->>'kind', tags[1], amount::text FROM reporting.orders",
    "SELECT 'it''s', 'a;b', 'E''x'",
    "SELECT current_setting('application_name')",
    "SELECT pg_sleep(1)",
    "SELECT row_number() OVER (PARTITION BY a ORDER BY b) FROM reporting.t",
    "SELECT * FROM reporting.orders WHERE note ILIKE $1",
    "SELECT 1;",
]

REFUSED = {
    "set transaction read write": "SET TRANSACTION READ WRITE",
    "temp table": "CREATE TEMP TABLE t AS SELECT 1",
    "temp table by select into": "SELECT 1 AS x INTO TEMP t",
    "select into": "SELECT * INTO reporting.copy FROM reporting.orders",
    "multi-statement": "SELECT 1; DELETE FROM reporting.orders",
    "two reads": "SELECT 1; SELECT 2",
    "query_to_xml": "SELECT query_to_xml('DELETE FROM reporting.t RETURNING 1', true, false, '')",
    "query_to_xml with schema": "SELECT pg_catalog.query_to_xml('SELECT 1', true, false, '')",
    "table_to_xml": "SELECT table_to_xml('reporting.orders', true, false, '')",
    "ts_stat": "SELECT * FROM ts_stat('DELETE FROM reporting.orders RETURNING 1')",
    "notify": "NOTIFY ssc",
    "notify with payload": "NOTIFY ssc, 'hello'",
    "pg_notify": "SELECT pg_notify('ssc', 'hello')",
    "listen": "LISTEN ssc",
    "pg_terminate_backend": "SELECT pg_terminate_backend(pg_backend_pid())",
    "pg_terminate_backend upper case": "SELECT PG_TERMINATE_BACKEND(1)",
    "pg_terminate_backend quoted": 'SELECT "pg_terminate_backend"(1)',
    "pg_terminate_backend as U& name": 'SELECT U&"\\0070g_terminate_backend"(1)',
    "pg_cancel_backend": "SELECT pg_cancel_backend(1)",
    "set_config": "SELECT set_config('default_transaction_read_only', 'off', false)",
    "nextval": "SELECT nextval('reporting.orders_id_seq')",
    "advisory lock": "SELECT pg_advisory_lock(1)",
    "read a server file": "SELECT pg_read_file('/etc/passwd')",
    "list a server dir": "SELECT * FROM pg_ls_dir('.')",
    "dblink": "SELECT * FROM dblink('host=evil', 'DELETE FROM t') AS x(a int)",
    "large object": "SELECT lo_import('/etc/passwd')",
    "data-modifying cte": "WITH gone AS (DELETE FROM reporting.t RETURNING *) SELECT * FROM gone",
    "inserting cte": "WITH n AS (INSERT INTO reporting.t VALUES (1) RETURNING 1) SELECT 1",
    "for update": "SELECT * FROM reporting.orders FOR UPDATE",
    "for share": "SELECT * FROM reporting.orders FOR SHARE",
    "insert": "INSERT INTO reporting.orders VALUES (1)",
    "update": "UPDATE reporting.orders SET amount = 0",
    "delete": "DELETE FROM reporting.orders",
    "merge": "MERGE INTO reporting.t USING reporting.s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "truncate": "TRUNCATE reporting.orders",
    "drop": "DROP TABLE reporting.orders",
    "alter": "ALTER TABLE reporting.orders ADD COLUMN x int",
    "copy out": "COPY reporting.orders TO STDOUT",
    "copy to program": "COPY (SELECT 1) TO PROGRAM 'id'",
    "do block": "DO $$ BEGIN DELETE FROM reporting.orders; END $$",
    "dollar quote hiding a call": "SELECT $a$ x $a$, pg_terminate_backend(1)",
    "dollar quote after a parameter": "SELECT $1$a$, pg_terminate_backend(1) --$a$",
    "e-string hiding a call": "SELECT E'\\'', pg_terminate_backend(1) --'",
    "e-string alone": "SELECT E'tab\\t'",
    "set": "SET statement_timeout = 0",
    "reset": "RESET ALL",
    "begin": "BEGIN",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
    "prepare": "PREPARE p AS DELETE FROM reporting.orders",
    "execute": "EXECUTE p",
    "call": "CALL reporting.cleanup()",
    "vacuum": "VACUUM reporting.orders",
    "explain analyze": "EXPLAIN ANALYZE DELETE FROM reporting.orders",
    "lock": "LOCK TABLE reporting.orders",
    "empty": "",
    "comment only": "-- nothing",
    "garbage": "SELEC 1 FROM",
}


@pytest.mark.parametrize("sql", READS)
def test_one_plain_read_passes(sql: str) -> None:
    assert refusal(sql) is None


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_anything_else_is_refused(case: str) -> None:
    assert refusal(REFUSED[case]) is not None


def test_the_reason_names_the_construct_and_never_quotes_the_statement() -> None:
    sql = "SELECT 1 AS x INTO TEMP sekrit_name"
    assert refusal(sql) == "the statement contains Into"
    assert refusal("SELECT 1; SELECT 2") == "the text is not exactly one statement"
    assert refusal("SELECT E'x'") == "the statement uses an E'...' string"
    assert refusal("SELECT $$x$$") == "the statement uses a dollar-quoted string"
    assert (refusal("NOTIFY ssc") or "").startswith("the statement is not a SELECT")
    assert refusal("SELECT ts_stat('x')") == "the statement calls a function that is refused"
