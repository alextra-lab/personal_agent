"""Isolation proofs for the sysgraph schema (ADR-0105 AC-2).

AC-2 — physical isolation, provable at the DB permission layer:
(a) a connection using the recall/user-facing role gets a permission error
    reading sysgraph;
(b) a Cypher traversal from the user KG can never reach a sysgraph node —
    structurally true because sysgraph is Postgres and the user KG is Neo4j,
    documented rather than independently tested here;
(c) no recall/tutor code path constructs or opens a sysgraph connection.
"""

from __future__ import annotations

import ast
from pathlib import Path

import asyncpg
import pytest

_RECALL_PATH_ROOTS = ("memory", "orchestrator", "tools")
_REPO_SRC = Path(__file__).resolve().parents[3] / "src" / "personal_agent"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_role_denied_on_sysgraph(recall_role_pool: asyncpg.Pool) -> None:
    """AC-2(a): recall_role reading a sysgraph table gets a real permission error."""
    async with recall_role_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM sysgraph.proposal")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sysgraph_role_has_no_public_table_privilege(agent_pool: asyncpg.Pool) -> None:
    """Symmetric to AC-2(a): sysgraph_role has no grant on a user-facing table.

    Asserts table-level privilege specifically (not schema-level USAGE, which
    Postgres grants to every role by default via the implicit PUBLIC role and
    is unrelated to this migration's isolation guarantee).
    """
    async with agent_pool.acquire() as conn:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            has_privilege = await conn.fetchval(
                "SELECT has_table_privilege('sysgraph_role', 'sessions', $1)",
                privilege,
            )
            assert has_privilege is False, f"sysgraph_role must not have {privilege} on sessions"


def test_no_recall_path_imports_sysgraph() -> None:
    """AC-2(c): no memory/orchestrator/tools module imports personal_agent.sysgraph.

    Uses an AST import scan (not a plain text grep) so a match in a comment or
    docstring can't produce a false positive.
    """
    offenders: list[str] = []
    for root_name in _RECALL_PATH_ROOTS:
        root = _REPO_SRC / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = [module] + [f"{module}.{alias.name}" for alias in node.names]
                else:
                    continue
                if any(
                    name == "personal_agent.sysgraph" or name.startswith("personal_agent.sysgraph.")
                    for name in names
                ):
                    offenders.append(str(path.relative_to(_REPO_SRC)))

    assert not offenders, f"recall/tutor path(s) import personal_agent.sysgraph: {offenders}"
