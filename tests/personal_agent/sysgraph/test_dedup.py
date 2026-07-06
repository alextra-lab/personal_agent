"""check_before_emit tests (ADR-0105 D9/D10, FRE-721/T7)."""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio

from personal_agent.sysgraph import SysgraphRepository
from personal_agent.sysgraph.dedup import ReadBeforeEmitDecision, check_before_emit
from personal_agent.sysgraph.repository import ProposalRecord

_REPO_SRC = Path(__file__).resolve().parents[3] / "src" / "personal_agent"
_PROBE_RESULT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "eval"
    / "fre720_insights_separation"
    / "probe_result.json"
)


def _proposal(
    fingerprint: str, scope: str | None = None, category: str = "dedup-test-cat"
) -> ProposalRecord:
    return ProposalRecord(
        source="reflection",
        category=category,
        fingerprint=fingerprint,
        what="a proposal",
        why=None,
        how=None,
        seen_count=1,
        scope=scope,
    )


@pytest.mark.asyncio
async def test_check_before_emit_repo_none_generates_new() -> None:
    """Unwired call sites (repo=None) behave exactly as before this ticket."""
    result = await check_before_emit(
        None,
        source="reflection",
        category="dedup-test-cat",
        scope=None,
        proposal=_proposal("fp-dedup-unwired"),
    )
    assert result.decision == ReadBeforeEmitDecision.GENERATE_NEW
    assert result.proposal_id is None


@pytest.mark.asyncio
async def test_check_before_emit_disconnected_repo_generates_new() -> None:
    """A repo that exists but was never connected (pool=None) also degrades to unchanged behavior."""
    repo = SysgraphRepository(dsn="postgresql://unused/unused")
    result = await check_before_emit(
        repo,
        source="reflection",
        category="dedup-test-cat",
        scope=None,
        proposal=_proposal("fp-dedup-disconnected"),
    )
    assert result.decision == ReadBeforeEmitDecision.GENERATE_NEW


class _RaisingRepo:
    """Fake repo whose read_before_emit raises a given exception, for fail-open tests."""

    def __init__(self, exc: BaseException) -> None:
        self.pool = object()  # truthy: "connected"
        self._exc = exc

    async def read_before_emit(self, *_args: object, **_kwargs: object) -> None:
        raise self._exc


@pytest.mark.asyncio
async def test_check_before_emit_connection_error_degrades() -> None:
    """A connectivity failure degrades to GENERATE_NEW and logs a warning (AC-9 fail-open)."""
    repo = _RaisingRepo(OSError("connection refused"))
    result = await check_before_emit(
        repo,  # type: ignore[arg-type]
        source="reflection",
        category="dedup-test-cat",
        scope=None,
        proposal=_proposal("fp-dedup-oserror"),
    )
    assert result.decision == ReadBeforeEmitDecision.DEGRADED_GENERATE_NEW


@pytest.mark.asyncio
async def test_check_before_emit_postgres_error_degrades() -> None:
    """A Postgres-level failure (e.g. query error under real load) also degrades, never blocks."""
    repo = _RaisingRepo(asyncpg.exceptions.ConnectionDoesNotExistError("closed"))
    result = await check_before_emit(
        repo,  # type: ignore[arg-type]
        source="reflection",
        category="dedup-test-cat",
        scope=None,
        proposal=_proposal("fp-dedup-pgerror"),
    )
    assert result.decision == ReadBeforeEmitDecision.DEGRADED_GENERATE_NEW


@pytest.mark.asyncio
async def test_check_before_emit_programming_error_propagates() -> None:
    """A caller/programming bug (TypeError) must NOT be folded into a sysgraph degrade.

    Silently swallowing every exception would hide a real defect (e.g. a malformed
    ProposalRecord) behind an innocuous-looking "sysgraph unreachable" log line.
    """
    repo = _RaisingRepo(TypeError("bad argument shape"))
    with pytest.raises(TypeError):
        await check_before_emit(
            repo,  # type: ignore[arg-type]
            source="reflection",
            category="dedup-test-cat",
            scope=None,
            proposal=_proposal("fp-dedup-typeerror"),
        )


@pytest_asyncio.fixture
async def _cleanup_dedup_rows(sysgraph_pool: asyncpg.Pool) -> AsyncIterator[None]:
    try:
        yield
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.proposal WHERE fingerprint LIKE 'fp-dedup-live-%'"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_before_emit_generates_new_against_real_sysgraph(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    _cleanup_dedup_rows: None,
) -> None:
    """End-to-end: a connected repo, nothing equivalent -> GENERATE_NEW, a real row written.

    This is the AC-9 control comparison: compared against
    ``test_check_before_emit_repo_none_generates_new`` above (read disabled), this proves
    the read is not a no-op when actually wired — a duplicate would be suppressed here if
    an equivalent already existed, whereas the read-disabled path can never suppress
    anything.
    """
    result = await check_before_emit(
        sysgraph_repo,
        source="reflection",
        category="dedup-live-cat",
        scope="orchestrator",
        proposal=_proposal("fp-dedup-live-fresh", scope="orchestrator", category="dedup-live-cat"),
    )
    assert result.decision == ReadBeforeEmitDecision.GENERATE_NEW
    assert isinstance(result.proposal_id, UUID)

    # Replaying the identical (source, category, scope) now reinforces instead of duplicating.
    second = await check_before_emit(
        sysgraph_repo,
        source="reflection",
        category="dedup-live-cat",
        scope="orchestrator",
        proposal=_proposal("fp-dedup-live-second", scope="orchestrator", category="dedup-live-cat"),
    )
    assert second.decision == ReadBeforeEmitDecision.REINFORCED
    assert second.proposal_id == result.proposal_id
    async with sysgraph_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM sysgraph.proposal WHERE category = 'dedup-live-cat'"
        )
    assert count == 1


def _source_without_docstrings(path: Path) -> str:
    """Strip module/function/class docstrings so prose mentions can't false-positive a scan."""
    tree = ast.parse(path.read_text(), filename=str(path))
    docstring_nodes: set[ast.expr] = set()
    candidates = [
        tree,
        *(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ),
    ]
    for node in candidates:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                docstring_nodes.add(body[0].value)
    lines = path.read_text().splitlines()
    for docnode in docstring_nodes:
        start = docnode.lineno - 1
        end = (docnode.end_lineno or docnode.lineno) - 1
        for i in range(start, end + 1):
            lines[i] = ""
    return "\n".join(lines)


def test_ac8_shipped_branch_matches_probe_decision() -> None:
    """AC-8: the shipped dedup path is mechanically checked against FRE-720's probe artifact.

    FRE-720's decide_branch() is keyed purely on the measured separation; this asserts the
    recorded decision is still "fallback" and that this module's CODE (docstrings excluded,
    since they legitimately discuss the rejected semantic-dedup alternative in prose) never
    adopts vector clustering without the artifact having said "separated".
    """
    probe_result = json.loads(_PROBE_RESULT_PATH.read_text())
    assert probe_result["decision"] == "fallback"

    dedup_code = _source_without_docstrings(_REPO_SRC / "sysgraph" / "dedup.py")
    repository_code = _source_without_docstrings(_REPO_SRC / "sysgraph" / "repository.py")
    for banned_token in ("cosine", "embed", "vector(", "reranker"):
        assert banned_token not in dedup_code.lower(), f"{banned_token!r} found in dedup.py code"
        assert banned_token not in repository_code.lower(), (
            f"{banned_token!r} found in repository.py code"
        )


def test_ac10_no_user_kg_recall_stack_dependency() -> None:
    """AC-10: no System-KG module on this path imports the User-KG reranker/recall stack.

    AST-based import scan (not text grep) so a docstring mention can't false-positive —
    mirrors test_isolation.py's existing AC-2(c) scan style.
    """
    offenders: list[str] = []
    for filename in ("dedup.py", "repository.py"):
        path = _REPO_SRC / "sysgraph" / filename
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
                name == "personal_agent.memory" or name.startswith("personal_agent.memory.")
                for name in names
            ):
                offenders.append(f"{filename}: {names}")
    assert not offenders, (
        f"sysgraph dedup path imports the User-KG memory/recall stack: {offenders}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac10_runtime_path_makes_no_http_calls(
    sysgraph_repo: SysgraphRepository,
    sysgraph_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-10 runtime proof: the fallback path succeeds with all outbound HTTP calls forced to fail.

    Simulates the laptop/Mac-GPU tunnel (or any embedder/reranker endpoint) being completely
    unreachable by making every ``httpx`` request raise. If the read-before-emit path secretly
    depended on an embedder call, this would fail; it doesn't, because the category+scope
    fallback path never makes one.
    """
    import httpx

    async def _forbidden_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read-before-emit must not make any outbound HTTP call")

    monkeypatch.setattr(httpx.AsyncClient, "request", _forbidden_request)
    try:
        result = await check_before_emit(
            sysgraph_repo,
            source="statistical_detector",
            category="dedup-ac10-cat",
            scope=None,
            proposal=_proposal("fp-dedup-live-ac10", scope=None, category="dedup-ac10-cat"),
        )
        assert result.decision == ReadBeforeEmitDecision.GENERATE_NEW
    finally:
        async with sysgraph_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sysgraph.proposal WHERE fingerprint = 'fp-dedup-live-ac10'"
            )
