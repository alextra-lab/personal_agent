"""AC-a integration test — ADR-0132 D2 / FRE-1147.

Drives each of the 7 enumerated egress seams' real production wiring with
`DomainGuard` in ALLOWLIST mode: a disallowed domain must be refused before
any connection is attempted, and an allowlisted domain must proceed.

No `httpx.AsyncClient`/SDK-client mocking is used for the "refused" half of
each pair — the DomainGuard request hook fires before transport dispatch
(verified against httpx 0.28.1 source in the implementation plan), so a real
`create_guarded_http_client()` denies without ever reaching a transport.
`_unreachable_transport()` patches `httpx.AsyncHTTPTransport.handle_async_request`
process-wide for the duration of the refused-call assertion, turning "before
any connection is attempted" into something the test proves rather than
assumes: if the guard's hook did NOT fire first, the patched transport would
be reached and raise `AssertionError`, failing the test.

The "proceeds" half of each pair reuses each seam's own existing mock pattern
(see `tests/personal_agent/llm_client/test_local_via_litellm.py`,
`tests/personal_agent/memory/test_embeddings.py`, etc.) so a real seam wiring
change is what's under test, not a reimplementation of those fixtures.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.security import DomainGuard, EgressBlockedError, GuardMode
from personal_agent.telemetry.trace import TraceContext

_CTX = TraceContext.new_trace()


def _patch_guard(
    monkeypatch: pytest.MonkeyPatch, *, mode: GuardMode, allowlist: frozenset[str] = frozenset()
) -> DomainGuard:
    """Install a pre-loaded DomainGuard as the process singleton for one test."""
    guard = DomainGuard(cache_path=Path("unused-in-test.json"), mode=mode, allowlist=allowlist)
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    monkeypatch.setattr("personal_agent.security.get_domain_guard", lambda: guard)
    return guard


@contextlib.contextmanager
def _unreachable_transport() -> Any:
    """Fail the test if any request reaches the real transport layer.

    Proves "refused before any connection is attempted" rather than assuming
    it: patches the actual default httpx transport class used by every
    `create_guarded_http_client()` call (none of the 7 seams pass a custom
    `transport=`), process-wide for the duration of the `with` block.
    """
    with patch.object(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(
            side_effect=AssertionError("transport reached — guard did not refuse pre-connection")
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# 1. LLM client (LiteLLMClient.respond, local placement) — ADR-0141 D1/FRE-1367
# ---------------------------------------------------------------------------
#
# Rewritten against the unified client: local placement now dispatches through
# litellm's OpenAI-compatible route (AsyncOpenAI(http_client=guarded)), not raw
# httpx.AsyncClient. This test exercises the PRODUCTION DEFAULT fallback path
# (`self._egress_guard or get_domain_guard()`, leaving `_egress_guard` unset) —
# distinct from test_local_via_litellm.py's TestEgressGuardOnTheLocalRoute,
# which always injects `client._egress_guard` directly (a test seam bypassing
# the process-wide singleton this test proves picks up the monkeypatched guard).


def _sse_response(content: str) -> bytes:
    chunk = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()


class TestLlmClientSeam:
    def _client(self, *, endpoint: str) -> Any:
        from personal_agent.llm_client.litellm_client import LiteLLMClient
        from personal_agent.llm_client.models import ModelDefinition, Placement

        return LiteLLMClient(
            model_id="test-primary",
            provider="slm_local",
            max_tokens=None,
            budget_role="main_inference",
            placement=Placement.LOCAL,
            model_def=ModelDefinition(
                id="test-primary",
                endpoint=endpoint,
                context_length=32768,
                max_concurrency=2,
                default_timeout=5,
            ),
        )

    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.llm_client.types import ModelRole
        from personal_agent.security import EgressBlockedError

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        client = self._client(endpoint="https://not-allowed.example")
        with _unreachable_transport(), pytest.raises(EgressBlockedError):
            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=TraceContext.new_trace(),
                max_retries=0,
            )

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.llm_client.types import ModelRole

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        client = self._client(endpoint="https://allowed.example")

        async def _handle(_self: Any, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_sse_response("hi back"),
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _handle):
            response = await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=TraceContext.new_trace(),
                max_retries=0,
            )
        assert response["content"] == "hi back"


# ---------------------------------------------------------------------------
# 2. SLM health probe (probe_slm_health)
# ---------------------------------------------------------------------------


class TestSlmHealthProbeSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.observability.slm_health.probe import probe_slm_health

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        with _unreachable_transport():
            snapshot = await probe_slm_health(
                url="https://not-allowed.example/health", trace_id="t1", timeout_s=1.0
            )
        # probe_slm_health never raises — refusal surfaces as a down snapshot.
        assert snapshot.status == "down"
        assert snapshot.reachable is False

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.observability.slm_health.probe import probe_slm_health

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "personal_agent.observability.slm_health.probe.httpx.AsyncClient",
            return_value=mock_client,
        ):
            snapshot = await probe_slm_health(
                url="https://allowed.example/health", trace_id="t1", timeout_s=1.0
            )
        assert snapshot.status != "down"


# ---------------------------------------------------------------------------
# 3. Embeddings (_call_embeddings_api → openai.AsyncOpenAI)
# ---------------------------------------------------------------------------


class TestEmbeddingsSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.memory.embeddings import _call_embeddings_api

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        with (
            patch.dict("personal_agent.memory.embeddings._openai_clients", {}, clear=True),
            _unreachable_transport(),
            pytest.raises(Exception) as exc_info,
        ):
            await _call_embeddings_api(["x"], "m", "https://not-allowed.example/v1")
        assert isinstance(exc_info.value.__cause__, EgressBlockedError) or isinstance(
            exc_info.value, EgressBlockedError
        )

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.memory.embeddings import _call_embeddings_api

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )

        def _mock_response(vectors: list[list[float]]) -> MagicMock:
            resp = MagicMock()
            resp.data = [MagicMock(embedding=v) for v in vectors]
            return resp

        def _fake_ctor(**kwargs: object) -> MagicMock:
            client = MagicMock()
            client.embeddings.create = AsyncMock(return_value=_mock_response([[0.1] * 4]))
            return client

        with (
            patch.dict("personal_agent.memory.embeddings._openai_clients", {}, clear=True),
            patch("openai.AsyncOpenAI", side_effect=_fake_ctor),
        ):
            result = await _call_embeddings_api(["x"], "m", "https://allowed.example/v1")
        assert result.data[0].embedding == [0.1] * 4


# ---------------------------------------------------------------------------
# 4. Reranker (rerank, SLM-local branch)
# ---------------------------------------------------------------------------


class TestRerankerSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.memory.reranker import rerank

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        mock_settings = MagicMock(reranker_enabled=True, reranker_input_cap=100, reranker_top_k=5)
        with (
            patch("personal_agent.memory.reranker.get_settings", return_value=mock_settings),
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("reranker-model", "https://not-allowed.example/v1"),
            ),
            _unreachable_transport(),
        ):
            results = await rerank("query", ["doc0", "doc1"])
        # rerank() degrades to passthrough on total failure (FRE-851) rather
        # than raising — refusal is observed as a non-reranked passthrough.
        assert [r.index for r in results] == [0, 1]

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.memory.reranker import rerank

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        mock_settings = MagicMock(reranker_enabled=True, reranker_input_cap=100, reranker_top_k=5)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=resp)
        with (
            patch("personal_agent.memory.reranker.get_settings", return_value=mock_settings),
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("reranker-model", "https://allowed.example/v1"),
            ),
            patch(
                "personal_agent.memory.reranker.create_guarded_http_client",
                return_value=mock_client,
            ),
        ):
            results = await rerank("query", ["doc0", "doc1"])
        assert results[0].index == 0
        assert results[0].score == 0.9


# ---------------------------------------------------------------------------
# 5. Artifact export (_HttpAssetFetcher.fetch)
# ---------------------------------------------------------------------------


class TestArtifactExportSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.service.artifacts_router import _HttpAssetFetcher
        from personal_agent.storage.artifact_export import ArtifactExportError

        # The guard's allowlist deliberately excludes artifacts.example.com —
        # the fetcher's own SSRF allowlist (below) is what lets the URL past
        # its own pre-check so it reaches DomainGuard, which then refuses it.
        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"other-allowed.example"})
        )
        fetcher = _HttpAssetFetcher(
            origin_host="artifacts.example.com",
            allowed_hosts=frozenset({"artifacts.example.com"}),
        )
        with _unreachable_transport(), pytest.raises(ArtifactExportError):
            await fetcher.fetch("https://artifacts.example.com/x.css")

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.service.artifacts_router import _HttpAssetFetcher

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"artifacts.example.com"})
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"body"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=resp)
        with patch(
            "personal_agent.service.artifacts_router.create_guarded_http_client",
            return_value=mock_client,
        ):
            fetcher = _HttpAssetFetcher(
                origin_host="artifacts.example.com",
                allowed_hosts=frozenset({"artifacts.example.com"}),
            )
            body = await fetcher.fetch("https://artifacts.example.com/x.css")
        assert body == b"body"


# ---------------------------------------------------------------------------
# 6. Envelope probe (probe_served_envelope)
# ---------------------------------------------------------------------------


class TestEnvelopeProbeSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.observability.artifact_envelope.probe import probe_served_envelope

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        with (
            patch(
                "personal_agent.observability.artifact_envelope.probe.to_artifacts_egress_url",
                side_effect=lambda u: u,
            ),
            _unreachable_transport(),
        ):
            # probe_served_envelope never raises — logs a warning and returns.
            await probe_served_envelope(
                public_url="https://not-allowed.example/a",
                artifact_id="art1",
                slug="s",
                content_type="text/html",
                trace_id="t1",
                session_id=None,
                user_id=None,
            )

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.observability.artifact_envelope.probe import probe_served_envelope

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = httpx.Headers({"content-type": "text/html"})
        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=resp)
        stream_cm.__aexit__ = AsyncMock(return_value=None)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.stream = MagicMock(return_value=stream_cm)
        with (
            patch(
                "personal_agent.observability.artifact_envelope.probe.to_artifacts_egress_url",
                side_effect=lambda u: u,
            ),
            patch(
                "personal_agent.observability.artifact_envelope.probe.create_guarded_http_client",
                return_value=mock_client,
            ),
        ):
            await probe_served_envelope(
                public_url="https://allowed.example/a",
                artifact_id="art1",
                slug="s",
                content_type="text/html",
                trace_id="t1",
                session_id=None,
                user_id=None,
            )


# ---------------------------------------------------------------------------
# 7. Web/search tools
# ---------------------------------------------------------------------------


def _mock_http_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


class TestWebSearchToolSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.tools.executor import ToolExecutionError
        from personal_agent.tools.web import web_search_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(
            "personal_agent.tools.web.settings.searxng_base_url", "https://not-allowed.example"
        )
        with _unreachable_transport(), pytest.raises(ToolExecutionError):
            await web_search_executor(query="python docs", ctx=_CTX)

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.tools.web import web_search_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(
            "personal_agent.tools.web.settings.searxng_base_url", "https://allowed.example"
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}
        with patch(
            "personal_agent.tools.web.create_guarded_http_client",
            return_value=_mock_http_client(resp),
        ):
            result = await web_search_executor(query="python docs", ctx=_CTX)
        assert result is not None


class TestFetchUrlToolSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.tools.executor import ToolExecutionError
        from personal_agent.tools.fetch import fetch_url_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        with _unreachable_transport(), pytest.raises(ToolExecutionError):
            await fetch_url_executor(url="https://not-allowed.example/page", ctx=_CTX)

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.tools.fetch import fetch_url_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        resp = MagicMock()
        resp.is_error = False
        resp.text = "<p>Allowed page content.</p>"
        resp.headers = {"content-type": "text/html"}
        with patch(
            "personal_agent.tools.fetch.create_guarded_http_client",
            return_value=_mock_http_client(resp),
        ):
            result = await fetch_url_executor(url="https://allowed.example/page", ctx=_CTX)
        assert "Allowed page content." in result["text"]


class TestPerplexityToolSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.tools.executor import ToolExecutionError
        from personal_agent.tools.perplexity import perplexity_query_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(
            "personal_agent.tools.perplexity.settings.perplexity_api_key", "test-key"
        )
        monkeypatch.setattr(
            "personal_agent.tools.perplexity.settings.perplexity_base_url",
            "https://not-allowed.example",
        )
        with _unreachable_transport(), pytest.raises(ToolExecutionError):
            await perplexity_query_executor(query="what is python?", mode="ask", ctx=_CTX)

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.tools.perplexity import perplexity_query_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(
            "personal_agent.tools.perplexity.settings.perplexity_api_key", "test-key"
        )
        monkeypatch.setattr(
            "personal_agent.tools.perplexity.settings.perplexity_base_url",
            "https://allowed.example",
        )
        resp = MagicMock()
        resp.is_error = False
        resp.json.return_value = {
            "choices": [{"message": {"content": "Python is a language."}}],
            "citations": [],
        }
        with patch(
            "personal_agent.tools.perplexity.create_guarded_http_client",
            return_value=_mock_http_client(resp),
        ):
            result = await perplexity_query_executor(query="what is python?", mode="ask", ctx=_CTX)
        assert result is not None


class TestContext7ToolSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.tools.context7 import get_library_docs_executor
        from personal_agent.tools.executor import ToolExecutionError

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(
            "personal_agent.tools.context7._CONTEXT7_BASE", "https://not-allowed.example"
        )
        with _unreachable_transport(), pytest.raises(ToolExecutionError):
            await get_library_docs_executor(library="react", ctx=_CTX)

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.tools.context7 import get_library_docs_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(
            "personal_agent.tools.context7._CONTEXT7_BASE", "https://allowed.example"
        )
        resolve_resp = MagicMock()
        resolve_resp.raise_for_status = MagicMock()
        resolve_resp.is_error = False
        resolve_resp.json.return_value = {"results": [{"id": "/react/react", "title": "React"}]}
        docs_resp = MagicMock()
        docs_resp.raise_for_status = MagicMock()
        docs_resp.is_error = False
        docs_resp.text = "# React docs"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[resolve_resp, docs_resp])
        with patch(
            "personal_agent.tools.context7.create_guarded_http_client", return_value=mock_client
        ):
            result = await get_library_docs_executor(library="react", ctx=_CTX)
        assert result is not None


class TestLinearToolSeam:
    @pytest.mark.asyncio
    async def test_disallowed_domain_refused_before_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.tools.executor import ToolExecutionError
        from personal_agent.tools.linear import _LINEAR_URL, create_linear_issue_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr("personal_agent.tools.linear.settings.linear_api_key", "test-key")
        monkeypatch.setattr(
            "personal_agent.tools.linear._LINEAR_URL", "https://not-allowed.example/graphql"
        )
        with _unreachable_transport(), pytest.raises(ToolExecutionError):
            await create_linear_issue_executor(title="T", description="D", ctx=_CTX)

    @pytest.mark.asyncio
    async def test_allowed_domain_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import personal_agent.tools.linear as linear_mod
        from personal_agent.tools.linear import create_linear_issue_executor

        _patch_guard(
            monkeypatch, mode=GuardMode.ALLOWLIST, allowlist=frozenset({"allowed.example"})
        )
        monkeypatch.setattr(linear_mod.settings, "linear_api_key", "test-key")
        monkeypatch.setattr(
            linear_mod.settings, "linear_personal_agent_label_id", "personal-agent-label-id"
        )
        monkeypatch.setattr(linear_mod, "_LINEAR_URL", "https://allowed.example/graphql")
        monkeypatch.setattr(linear_mod, "_team_id_cache", "team-id-test")
        monkeypatch.setattr(linear_mod, "_state_id_cache", {"Needs Approval": "state-id-test"})
        monkeypatch.setattr(linear_mod, "_label_id_cache", {"agent-filed": "agent-filed-label-id"})
        monkeypatch.setattr(linear_mod, "_labels_fetched_for_teams", {"team-id-test"})

        responses = [
            {"issues": {"nodes": []}},  # dedup check: no existing issue
            {
                "issueCreate": {
                    "issue": {"id": "1", "identifier": "FRE-1", "title": "T", "url": "https://x"}
                }
            },
        ]
        call_count = 0

        async def fake_post(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            resp = MagicMock()
            resp.is_error = False
            resp.json.return_value = {"data": responses[min(call_count, len(responses) - 1)]}
            call_count += 1
            return resp

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        client.post = fake_post
        with patch("personal_agent.tools.linear.create_guarded_http_client", return_value=client):
            result = await create_linear_issue_executor(title="T", description="D", ctx=_CTX)
        assert result["identifier"] == "FRE-1"
