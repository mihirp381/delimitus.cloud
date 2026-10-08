"""Whether a statement is one plain read, before it reaches the database (SSC-051, GA-5).

sqlglot parses the text as the connector's :class:`Dialect` reads it (:data:`POSTGRES`,
:data:`MYSQL`). The statement is refused unless it is exactly one
``SELECT`` (or ``UNION``, ``INTERSECT``, ``EXCEPT`` of them), with no data-changing ``WITH``, no
``SELECT INTO``, no row locks, and no function on :data:`DENIED` or matching a denied prefix or
suffix: those write, signal other sessions, read server files, or run a query given as text,
which this check would never see.

The check only counts if sqlglot reads the text as Postgres does. Two string forms are where they
differ, so both are refused: ``E'...'`` strings, whose backslash escapes sqlglot does not apply,
and dollar-quoted strings, whose extent sqlglot can read differently after a number or
parameter. A function name that is not a plain identifier (a ``U&"..."`` name Postgres decodes)
is refused for the same reason. Anything sqlglot cannot parse is refused. The session behind the
check forces ``standard_conforming_strings`` on, so plain strings lex alike in both.

What the check misses, the database stops: every read runs in a read-only transaction as a role
with ``SELECT`` only (``postgres_setup.sql``, ``mysql_setup.sql``) and ends in ``ROLLBACK``.

MySQL's denied names are the functions that read server files (``LOAD_FILE``), hold locks other
sessions wait on (``GET_LOCK`` and kin), wait on replication, burn time on purpose
(``BENCHMARK``, ``SLEEP``) or reach outside the server through the ``sys_*`` UDFs. ``SLEEP`` is
refused because it returns 1 when ``max_execution_time`` or ``KILL QUERY`` interrupts it: the
read would end quietly with a row instead of as ``QUERY_TIMEOUT``. sqlglot and MySQL read
backslash escapes alike, so no string form is refused.
"""

import re
from dataclasses import dataclass
from typing import Final

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

DENIED: Final = frozenset(
    {
        "currval",
        "nextval",
        "setval",
        "set_config",
        "pg_cancel_backend",
        "pg_terminate_backend",
        "pg_notify",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_promote",
        "pg_switch_wal",
        "pg_create_restore_point",
        "pg_log_backend_memory_contexts",
        "pg_export_snapshot",
        "pg_import_system_collations",
        "pg_current_xact_id",
        "txid_current",
        "ts_stat",
        "ts_rewrite",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "pg_stat_file",
        "pg_prewarm",
    }
)
"""Functions refused by name, compared in lower case and without their schema."""
DENIED_PREFIXES: Final = (
    "dblink",
    "lo_",
    "pg_advisory",
    "pg_try_advisory",
    "pg_read_",
    "pg_ls_",
    "pg_file_",
    "pg_stat_reset",
    "pg_replication_",
    "pg_logical_",
    "pg_wal_",
    "pg_backup_",
    "pg_create_",
    "pg_drop_",
    "pg_copy_",
)
DENIED_SUFFIXES: Final = ("_to_xml", "_to_xmlschema", "_to_xml_and_xmlschema")
MYSQL_DENIED: Final = frozenset(
    {
        "load_file",
        "benchmark",
        "sleep",
        "get_lock",
        "release_lock",
        "release_all_locks",
        "is_free_lock",
        "is_used_lock",
        "master_pos_wait",
        "source_pos_wait",
        "wait_for_executed_gtid_set",
        "wait_until_sql_thread_after_gtids",
        "statement_digest",
        "statement_digest_text",
    }
)
MYSQL_DENIED_PREFIXES: Final = (
    "sys_",
    "group_replication_",
    "asynchronous_connection_failover_",
    "gtid_",
    "ps_",
    "mysql_firewall_",
    "audit_",
    "keyring_",
    "version_tokens_",
    "service_",
)
_PLAIN_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_READS: Final = (exp.Select, exp.SetOperation, exp.Subquery)
_WRITES: Final = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Set,
    exp.Into,
    exp.Lock,
    exp.Copy,
)
_UNSAFE_STRINGS: Final = {
    TokenType.BYTE_STRING: "an E'...' string",
    TokenType.HEREDOC_STRING: "a dollar-quoted string",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Dialect:
    """How sqlglot reads one engine's SQL and which function names that engine refuses."""

    read: str
    denied: frozenset[str]
    denied_prefixes: tuple[str, ...]
    denied_suffixes: tuple[str, ...] = ()
    unsafe_strings: dict[TokenType, str] | None = None
    denied_qualifiers: frozenset[str] = frozenset()
    """Qualifiers whose functions are refused (BigQuery's ``ML.``), in lower case."""
    denied_paths: re.Pattern[str] | None = None
    """Tables refused by their dotted path, matched in lower case."""

    def refuses(self, name: str) -> bool:
        low = name.lower()
        return (
            low in self.denied
            or low.startswith(self.denied_prefixes)
            or (bool(self.denied_suffixes) and low.endswith(self.denied_suffixes))
        )


POSTGRES: Final = Dialect(
    read="postgres",
    denied=DENIED,
    denied_prefixes=DENIED_PREFIXES,
    denied_suffixes=DENIED_SUFFIXES,
    unsafe_strings=_UNSAFE_STRINGS,
)
MYSQL: Final = Dialect(read="mysql", denied=MYSQL_DENIED, denied_prefixes=MYSQL_DENIED_PREFIXES)


def _names(node: exp.Func) -> set[str]:
    if isinstance(node, exp.Anonymous):
        return {node.name}
    return {node.sql_name(), *type(node).sql_names()}


def refusal(  # noqa: PLR0911  (one return per refusal)
    sql: str, dialect: Dialect = POSTGRES, *, project: str | None = None
) -> str | None:
    """Why ``sql`` is not one plain read, or ``None`` when it is. The reason is for the log; it
    names the construct and never quotes the statement. With ``project`` (BigQuery) a table or
    function another project qualifies is refused."""
    try:
        tokens = sqlglot.tokenize(sql, read=dialect.read)
        statements = sqlglot.parse(sql, read=dialect.read)  # pyright: ignore[reportUnknownMemberType]
    except SqlglotError:
        return "the statement does not parse"
    unsafe = dialect.unsafe_strings or {}
    for token in tokens:
        if token.token_type in unsafe:
            return f"the statement uses {unsafe[token.token_type]}"
    found = [s for s in statements if s is not None]
    if len(found) != 1 or len(statements) > 1:
        return "the text is not exactly one statement"
    root = found[0]
    if not isinstance(root, _READS):
        return f"the statement is not a SELECT ({type(root).__name__})"
    for node in root.walk():
        if isinstance(node, _WRITES):
            return f"the statement contains {type(node).__name__}"
        scoped = _scope_refusal(node, dialect, project)
        if scoped is not None:
            return scoped
        if isinstance(node, exp.Func):
            names = _names(node)
            if isinstance(node, exp.Anonymous) and not _PLAIN_NAME.fullmatch(node.name):
                return "a function name is not a plain identifier"
            if any(dialect.refuses(n) for n in names):
                return "the statement calls a function that is refused"
    return None


def mysql_refusal(sql: str) -> str | None:
    """:func:`refusal` as MySQL reads the text."""
    return refusal(sql, MYSQL)


BIGQUERY_DENIED: Final = frozenset({"external_query", "session_user"})
"""``EXTERNAL_QUERY`` runs a query given as text on another database; ``SESSION_USER`` answers
the service account's email, which is the credential's."""
BIGQUERY_DENIED_QUALIFIERS: Final = frozenset({"ml", "ai"})
"""``ML.`` and ``AI.`` functions train, call or bill models outside the read."""
BIGQUERY_DENIED_PATHS: Final = re.compile(r"(^|\.)region-|information_schema\.jobs")
"""Region-qualified ``INFORMATION_SCHEMA`` views are project-wide, and the ``JOBS`` views show
the service account's earlier queries: another app's text and parameters on the same
connection."""
BIGQUERY: Final = Dialect(
    read="bigquery",
    denied=BIGQUERY_DENIED,
    denied_prefixes=(),
    denied_qualifiers=BIGQUERY_DENIED_QUALIFIERS,
    denied_paths=BIGQUERY_DENIED_PATHS,
)
OTHER_PROJECT: Final = "the statement names another project"


def _dotted(node: exp.Expression) -> list[str]:
    if isinstance(node, exp.Dot):
        return _dotted(node.this) + _dotted(node.expression)
    if isinstance(node, exp.Column):
        return [part.name for part in node.parts]
    return [node.name]


def _qualifiers(node: exp.Func) -> list[str]:
    """The names before a function's own (``ML`` in ``ML.PREDICT``), in lower case, without
    BigQuery's ``SAFE.`` prefix."""
    parent = node.parent
    names: list[str] = []
    if isinstance(parent, exp.Dot) and parent.expression is node:
        names = _dotted(parent.this)
    elif isinstance(parent, exp.Table) and parent.this is node:
        names = [n for n in (parent.catalog, parent.db) if n]
    names = [n.lower() for n in names]
    return names[1:] if names[:1] == ["safe"] else names


def _scope_refusal(node: exp.Expression, dialect: Dialect, project: str | None) -> str | None:
    """Why a table or function reaches outside what the connection reads (BigQuery)."""
    if isinstance(node, exp.Table):
        if project is not None and node.catalog and node.catalog.lower() != project.lower():
            return OTHER_PROJECT
        path = ".".join(part.name for part in node.parts).lower()
        if dialect.denied_paths is not None and dialect.denied_paths.search(path):
            return "the statement reads a view that is refused"
    if isinstance(node, exp.Func) and (dialect.denied_qualifiers or project is not None):
        qualifiers = _qualifiers(node)
        if any(q in dialect.denied_qualifiers for q in qualifiers):
            return "the statement calls a function that is refused"
        if project is not None and len(qualifiers) >= 2 and qualifiers[0] != project.lower():  # noqa: PLR2004  (project.dataset.function)
            return OTHER_PROJECT
    return None


def bigquery_refusal(sql: str, project: str) -> str | None:
    """:func:`refusal` as BigQuery reads the text, for a connection to ``project``."""
    return refusal(sql, BIGQUERY, project=project)
