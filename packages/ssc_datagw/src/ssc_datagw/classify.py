"""Whether a statement is one plain read, before it reaches the database (SSC-051).

sqlglot parses the text as Postgres. The statement is refused unless it is exactly one
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

What the check misses, the database stops: every read runs in ``BEGIN READ ONLY`` as a role with
``SELECT`` only (``postgres_setup.sql``) and ends in ``ROLLBACK``.
"""

import re
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


def _names(node: exp.Func) -> set[str]:
    if isinstance(node, exp.Anonymous):
        return {node.name}
    return {node.sql_name(), *type(node).sql_names()}


def _denied(name: str) -> bool:
    low = name.lower()
    return low in DENIED or low.startswith(DENIED_PREFIXES) or low.endswith(DENIED_SUFFIXES)


def refusal(sql: str) -> str | None:  # noqa: PLR0911  (one return per refusal)
    """Why ``sql`` is not one plain read, or ``None`` when it is. The reason is for the log; it
    names the construct and never quotes the statement."""
    try:
        tokens = sqlglot.tokenize(sql, read="postgres")
        statements = sqlglot.parse(sql, read="postgres")  # pyright: ignore[reportUnknownMemberType]
    except SqlglotError:
        return "the statement does not parse"
    for token in tokens:
        if token.token_type in _UNSAFE_STRINGS:
            return f"the statement uses {_UNSAFE_STRINGS[token.token_type]}"
    found = [s for s in statements if s is not None]
    if len(found) != 1 or len(statements) > 1:
        return "the text is not exactly one statement"
    root = found[0]
    if not isinstance(root, _READS):
        return f"the statement is not a SELECT ({type(root).__name__})"
    for node in root.walk():
        if isinstance(node, _WRITES):
            return f"the statement contains {type(node).__name__}"
        if isinstance(node, exp.Func):
            names = _names(node)
            if isinstance(node, exp.Anonymous) and not _PLAIN_NAME.fullmatch(node.name):
                return "a function name is not a plain identifier"
            if any(_denied(n) for n in names):
                return "the statement calls a function that is refused"
    return None
