#!/usr/bin/env python3
"""Run one read-only Cypher query on the knowledge graph and print the rows (FRE-1578).

Usage:
    cypher.py [--param name=value ...] [--limit N] "<cypher>"

Output: one JSON document on stdout, ``{"rows": [...], "row_count": n, "truncated": bool}``.
Exit codes: 0 ok, 1 the query or the connection failed, 2 bad usage. Every failure is one
``error:`` line on stderr, with no traceback and no password.

Read-only. The session opens in read access mode and runs the query as a read transaction, so
the server refuses a write. A check for write clauses runs first, only to give a plain error
before a connection is made. The server is the guard, not the check.

A read transaction does not stop a read-mode procedure that reaches out (``apoc.load.json``) or
shows the configuration (``dbms.listConfig``). So a ``CALL`` is allowed only for the procedures
in ``_ALLOWED_PROCEDURES``, which list the schema and nothing else.

Environment (the same names as ``config/``; the script is standalone, because importing
``personal_agent.config`` starts the app configuration and logs to stdout):

    AGENT_NEO4J_URI       default bolt://neo4j:7687
    AGENT_NEO4J_USER      default neo4j
    AGENT_NEO4J_PASSWORD  required
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import neo4j

_USAGE = 'usage: cypher.py [--param name=value ...] [--limit N] "<cypher>"'
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000
_TIMEOUT_S = 15
# The query is read as tokens, not searched as text. A search over text has two failure modes: a
# quote inside a quoted name starts a false string, and a quoted name can hide a procedure name
# (the form with backticks around each part is valid Cypher). A token is a string, a comment, a
# quoted name, a word, white space, or one other character.
_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
    |(?P<comment>//[^\n]*|/\*.*?\*/)
    |(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    |(?P<quoted>`(?:``|[^`])*`)
    |(?P<word>[A-Za-z_]\w*)
    |(?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
_WRITE_WORDS = frozenset(
    {"CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH"}
)
# A procedure call is allowed only if it is on this list (allow, never deny): a procedure can
# write (apoc.cypher.doIt), read a file or a URL (apoc.load.json), or show the server
# configuration (dbms.listConfig), and a read transaction stops only the first.
_ALLOWED_PROCEDURES = frozenset(
    {
        "db.labels",
        "db.relationshipTypes",
        "db.propertyKeys",
        "db.schema.visualization",
        "db.schema.nodeTypeProperties",
        "db.schema.relTypeProperties",
    }
)


class _Record(Protocol):
    """One result row of the driver."""

    def data(self) -> dict[str, object]:
        """Return the row as a plain dict."""
        ...


class _Transaction(Protocol):
    """The part of a driver transaction that the script uses."""

    def run(self, query: str, parameters: dict[str, object], /) -> Iterable[_Record]:
        """Run one statement."""
        ...


class CypherError(Exception):
    """A failure with a message that is safe to print. The exit code is 1."""


class UsageError(CypherError):
    """The command line is wrong. The exit code is 2."""


def _safe_uri(uri: str) -> str:
    """Return ``uri`` without user-info, so that a password in it is never printed."""
    try:
        parts = urlsplit(uri)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "the URI"
    if not host:
        return "the URI"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _tokens(query: str) -> list[tuple[str, str]]:
    """Split ``query`` into (kind, text) tokens, with white space and comments left out."""
    return [
        (match.lastgroup or "other", match.group())
        for match in _TOKEN.finditer(query)
        if match.lastgroup not in ("ws", "comment")
    ]


def _name(token: tuple[str, str]) -> str:
    """Return the name that a word or a quoted-name token stands for, without backticks."""
    kind, text = token
    return text[1:-1].replace("``", "`") if kind == "quoted" else text


def find_write_clause(query: str) -> str | None:
    """Return the first write clause in ``query``, or ``None``.

    Args:
        query: The Cypher text.

    Returns:
        The clause in upper case, or ``None``. Text in a string, a comment or a quoted name
        is not read, and a word after a dot is a property name, not a clause.
    """
    tokens = _tokens(query)
    for index, (kind, text) in enumerate(tokens):
        if kind != "word" or (index and tokens[index - 1] == ("other", ".")):
            continue
        word = text.upper()
        if word in _WRITE_WORDS:
            return word
        if word == "LOAD" and index + 1 < len(tokens) and tokens[index + 1][1].upper() == "CSV":
            return "LOAD CSV"
    return None


def find_blocked_procedure(query: str) -> str | None:
    """Return the first ``CALL`` in ``query`` that is not on the allowed list, or ``None``.

    Fails closed: a ``CALL`` that is not followed by a name that can be read, or by the opening
    of a subquery, counts as blocked.

    Args:
        query: The Cypher text.

    Returns:
        The procedure name (quoted parts joined and unquoted), ``<unreadable>``, or ``None``.
        A ``CALL { ... }`` or ``CALL (...) { ... }`` subquery is not a procedure.
    """
    tokens = _tokens(query)
    for index, (kind, text) in enumerate(tokens):
        if kind != "word" or text.upper() != "CALL":
            continue
        if index and tokens[index - 1] == ("other", "."):
            continue
        cursor = index + 1
        if cursor < len(tokens) and tokens[cursor][1] in ("{", "("):
            continue
        parts: list[str] = []
        while cursor < len(tokens) and tokens[cursor][0] in ("word", "quoted"):
            parts.append(_name(tokens[cursor]))
            if cursor + 2 < len(tokens) and tokens[cursor + 1] == ("other", "."):
                cursor += 2
            else:
                break
        name = ".".join(parts)
        if not parts or name not in _ALLOWED_PROCEDURES:
            return name or "<unreadable>"
    return None


def _parse_args(argv: Sequence[str]) -> tuple[str, dict[str, object], int]:
    """Read the command line.

    Raises:
        UsageError: The usage is wrong.
    """
    params: dict[str, object] = {}
    limit = _DEFAULT_LIMIT
    query: str | None = None
    items = list(argv)
    while items:
        item = items.pop(0)
        if item == "--param" and items:
            name, sep, raw = items.pop(0).partition("=")
            if not sep or not name.isidentifier():
                raise UsageError("--param needs name=value, with a plain name")
            try:
                params[name] = json.loads(raw)
            except ValueError:
                params[name] = raw
        elif item == "--limit" and items:
            try:
                limit = int(items.pop(0))
            except ValueError:
                raise UsageError("--limit needs a whole number") from None
            if not 1 <= limit <= _MAX_LIMIT:
                raise UsageError(f"--limit is from 1 to {_MAX_LIMIT}")
        elif item.startswith("--") or query is not None:
            raise UsageError(_USAGE)
        else:
            query = item
    if not query or not query.strip():
        raise UsageError(_USAGE)
    return query, params, limit


def run_query(
    query: str,
    params: Mapping[str, object],
    limit: int,
    env: Mapping[str, str],
    driver: neo4j.Driver | None = None,
) -> dict[str, object]:
    """Run ``query`` in a read transaction.

    Args:
        query: One Cypher statement.
        params: Query parameters.
        limit: The most rows to return.
        env: The environment.
        driver: A driver, for a test. ``None`` opens one from ``env``.

    Returns:
        ``{"rows": [...], "row_count": n, "truncated": bool}``.

    Raises:
        CypherError: The check, the connection or the query failed.
    """
    # The procedure check is first: the name apoc.nodes.delete holds the word DELETE.
    procedure = find_blocked_procedure(query)
    if procedure:
        raise CypherError(
            f"the procedure {procedure} is not allowed. "
            f"Allowed procedures: {', '.join(sorted(_ALLOWED_PROCEDURES))}. "
            "Use MATCH and RETURN for other data."
        )
    clause = find_write_clause(query)
    if clause:
        raise CypherError(
            f"this script is read-only and the query holds {clause}. "
            "Use MATCH and RETURN. If it is a property or alias name, quote it with backticks."
        )
    uri = env.get("AGENT_NEO4J_URI", "bolt://neo4j:7687")
    password = env.get("AGENT_NEO4J_PASSWORD", "")
    if driver is None and not password:
        raise CypherError("AGENT_NEO4J_PASSWORD is not set in this environment")
    try:
        import neo4j
        from neo4j import exceptions as neo4j_exceptions
    except ImportError:
        raise CypherError(
            "the neo4j driver is not importable by this python3. "
            "Run the script with /app/.venv/bin/python."
        ) from None

    def read(tx: _Transaction) -> list[dict[str, object]]:
        result = tx.run(query, dict(params))
        return [record.data() for _, record in zip(range(limit + 1), result, strict=False)]

    owned = driver is None
    client = driver
    try:
        if client is None:
            client = neo4j.GraphDatabase.driver(
                uri,
                auth=(env.get("AGENT_NEO4J_USER", "neo4j"), password),
                connection_timeout=_TIMEOUT_S,
            )
        with client.session(default_access_mode=neo4j.READ_ACCESS) as session:
            rows = session.execute_read(neo4j.unit_of_work(timeout=_TIMEOUT_S)(read))
    except neo4j_exceptions.AuthError:
        raise CypherError(
            "authentication failed. Check AGENT_NEO4J_USER and AGENT_NEO4J_PASSWORD."
        ) from None
    except neo4j_exceptions.ServiceUnavailable:
        raise CypherError(
            f"cannot connect to {_safe_uri(uri)}. Is the neo4j service running?"
        ) from None
    except neo4j_exceptions.Neo4jError as exc:
        # The driver puts the server text in .message. str(exc) adds {neo4j_code: ...} noise.
        text = getattr(exc, "message", None) or str(exc)
        first = (text.strip().splitlines() or [""])[0]
        if password:
            first = first.replace(password, "***")
        raise CypherError(f"the server refused the query: {first[:200]}") from None
    except (neo4j_exceptions.DriverError, ValueError) as exc:
        raise CypherError(
            f"the driver failed ({type(exc).__name__}). Check AGENT_NEO4J_URI."
        ) from None
    finally:
        if owned and client is not None:
            client.close()
    return {
        "rows": rows[:limit],
        "row_count": min(len(rows), limit),
        "truncated": len(rows) > limit,
    }


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run the command line and print the result.

    Args:
        argv: The arguments after the program name.
        env: The environment.

    Returns:
        The exit code.
    """
    if list(argv) in (["-h"], ["--help"]):
        print(_USAGE)
        print("Runs one read-only Cypher statement and prints the rows as JSON.")
        return 0
    try:
        query, params, limit = _parse_args(argv)
        result = run_query(query, params, limit, env)
        print(json.dumps(result, default=str))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except CypherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
