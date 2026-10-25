"""A general worker reads the artifact its task names (FRE-1678).

An artifact has no file path: its bytes live in R2 and its row in Postgres. A worker that held
only ``read`` asked for ``/workspace/artifacts/<name>.html``, was refused, and searched the file
system. The worker now holds ``artifact_read`` (by id), and the refusal for a path outside the
rules names it.

Real: the worker loop, dispatch, governance, the tool layer and ``artifact_read_executor``.
Faked: the model (scripted), the artifact store and the database session.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from personal_agent.config import settings
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.governance.sub_agent_tools import evaluate_sub_agent_tool_grant
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from personal_agent.tools import artifact_tools
from tests._helpers.worker_run import run_worker, shared_layer, worker_layer

_HTML = "<h1>Entropy, one equation</h1>"


class _Store:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        return self._payloads[r2_key]


class _Session:
    def __init__(self, row: SimpleNamespace) -> None:
        self._row = row

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        assert params is not None
        if params["user_id"] != self._row.user_id or params["artifact_id"] != self._row.id:
            return SimpleNamespace(first=lambda: None)
        return SimpleNamespace(first=lambda: self._row)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def artifact(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """One stored HTML artifact, and the R2 settings that make the artifact tools register."""
    user_id = uuid4()
    art_id = uuid4()
    r2_key = f"artifact/{user_id}/GLOBAL/{art_id}.html"
    row = SimpleNamespace(
        id=art_id,
        user_id=user_id,
        slug="entropy-one-equation",
        title="Entropy, one equation",
        summary="s",
        content_type="text/html; charset=utf-8",
        size_bytes=len(_HTML),
        r2_key=r2_key,
        tags=[],
        created_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        artifact_tools, "get_artifact_store", lambda: _Store({r2_key: _HTML.encode()})
    )
    monkeypatch.setattr(artifact_tools, "AsyncSessionLocal", lambda: _Session(row))
    with (
        patch.object(settings, "r2_endpoint_url", "https://r2.test"),
        patch.object(settings, "r2_access_key_id", "k"),
        patch.object(settings, "r2_secret_access_key", "s"),
    ):
        yield SimpleNamespace(id=art_id, user_id=user_id)


def _general_spec(task: str) -> SubAgentSpec:
    """A ``general`` task with the tools that governance grants its type today."""
    grant = evaluate_sub_agent_tool_grant(
        WORKER_TYPES[WorkerType.GENERAL].tools, Mode.NORMAL, load_governance_config()
    )
    return SubAgentSpec(
        task=task,
        context=[],
        timeout_seconds=30.0,
        tools=list(grant.granted),
        worker_type=WorkerType.GENERAL,
    )


class TestTheWorkerReadsTheArtifactItsTaskNames:
    """AC-1 (the decision) and AC-2: the content arrives on the first call."""

    def test_the_general_type_holds_artifact_read_and_governance_grants_it(self) -> None:
        spec = WORKER_TYPES[WorkerType.GENERAL]
        grant = evaluate_sub_agent_tool_grant(spec.tools, Mode.NORMAL, load_governance_config())
        assert "artifact_read" in grant.granted
        assert grant.denied == ()

    @pytest.mark.asyncio
    async def test_the_first_call_returns_the_artifact_content(
        self, artifact: SimpleNamespace
    ) -> None:
        spec = _general_spec(f"Summarise the artifact with artifact_id {artifact.id}.")
        with shared_layer(worker_layer()):
            run = await run_worker(
                spec,
                [("artifact_read", {"artifact_id": str(artifact.id)})],
                user_id=artifact.user_id,
            )
        payload = run.payload("c0")
        assert payload["ok"] is True, payload
        assert payload["content"] == _HTML

    @pytest.mark.asyncio
    async def test_another_users_artifact_is_not_found(self, artifact: SimpleNamespace) -> None:
        spec = _general_spec(f"Summarise the artifact with artifact_id {artifact.id}.")
        with shared_layer(worker_layer()):
            run = await run_worker(
                spec,
                [("artifact_read", {"artifact_id": str(artifact.id)})],
                user_id=UUID(int=1),
            )
        payload = run.payload("c0")
        assert payload["ok"] is False
        assert "content" not in payload


class TestTheRefusalNamesArtifactRead:
    """AC-3: a path outside the rules points at the tool that reads an artifact."""

    @pytest.mark.asyncio
    async def test_a_workspace_artifact_path_names_artifact_read(
        self, artifact: SimpleNamespace
    ) -> None:
        spec = _general_spec("Summarise the artifact.")
        with shared_layer(worker_layer()):
            run = await run_worker(
                spec,
                [("read", {"path": "/workspace/artifacts/entropy_one_equation.html"})],
                user_id=artifact.user_id,
            )
        payload = run.payload("c0")
        assert payload["ok"] is False
        assert payload["error"]["code"] == "path_refused"
        assert "artifact_read" in payload["error"]["next_action"]

    @pytest.mark.asyncio
    async def test_a_file_the_task_does_not_name_names_artifact_read(
        self, artifact: SimpleNamespace, tmp_path: Path
    ) -> None:
        other = tmp_path / "other.html"
        other.write_text(_HTML, encoding="utf-8")
        spec = _general_spec("Summarise the artifact.")
        with shared_layer(worker_layer()):
            run = await run_worker(spec, [("read", {"path": str(other)})], user_id=artifact.user_id)
        payload = run.payload("c0")
        assert payload["ok"] is False
        assert payload["error"]["code"] == "not_granted"
        assert "artifact_read" in payload["error"]["next_action"]
