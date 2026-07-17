# tests/personal_agent/memory/test_reranker.py
"""Tests for the cross-attention reranker module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from personal_agent.config import settings
from personal_agent.memory.reranker import RerankResult, _passthrough, rerank


def _mock_reranker_config() -> MagicMock:
    """Create a mock ModelConfig with a reranker entry."""
    model_def = MagicMock()
    model_def.id = "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"
    model_def.endpoint = "http://localhost:8504/v1"
    config = MagicMock()
    config.models = {"reranker": model_def}
    return config


class TestRerank:
    @pytest.mark.asyncio
    async def test_successful_rerank(self) -> None:
        """Should call /v1/rerank and return sorted results."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.9},
                {"index": 2, "relevance_score": 0.5},
            ]
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=mock_client),
        ):
            results = await rerank("what database?", ["doc0", "doc1", "doc2"])

        assert len(results) == 3
        # Should be sorted by score descending
        assert results[0].score == 0.9
        assert results[0].index == 1
        assert results[0].document == "doc1"
        assert results[1].score == 0.5
        assert results[2].score == 0.3

    @pytest.mark.asyncio
    async def test_disabled_returns_passthrough(self) -> None:
        """When reranker_enabled=False, return documents in original order."""
        with patch(
            "personal_agent.memory.reranker.get_settings",
        ) as mock_settings:
            mock_settings.return_value.reranker_enabled = False
            results = await rerank("query", ["a", "b", "c"])

        assert len(results) == 3
        assert results[0].index == 0
        assert results[0].document == "a"

    @pytest.mark.asyncio
    async def test_server_down_returns_passthrough(self) -> None:
        """When reranker server is unreachable, return documents in original order."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=mock_client),
        ):
            results = await rerank("query", ["a", "b"])

        assert len(results) == 2
        assert results[0].document == "a"

    @pytest.mark.asyncio
    async def test_empty_documents(self) -> None:
        """Empty document list should return empty list."""
        results = await rerank("query", [])
        assert results == []

    @pytest.mark.asyncio
    async def test_missing_config_returns_passthrough(self) -> None:
        """Missing reranker config in models.yaml should degrade gracefully."""
        with patch(
            "personal_agent.memory.reranker._get_reranker_config",
            side_effect=KeyError("reranker"),
        ):
            results = await rerank("query", ["a", "b"])

        assert len(results) == 2
        assert results[0].document == "a"


_CF_HEADERS = {"CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "sec"}


class TestCfAccessInjection:
    """CF-Access injection for the reranker path (FRE-656).

    The reranker must send CF-Access headers to the Access-gated Mac SLM gateway,
    and only to it (gated by hostname).
    """

    def _mock_client(self) -> AsyncMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=resp)
        return client

    @pytest.mark.asyncio
    async def test_slm_endpoint_gets_cf_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.example.com")
        client = self._mock_client()
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("Voodisss/Qwen3-Reranker-4B", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch(
                "personal_agent.memory.reranker.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
        ):
            await rerank("q", ["a"])

        assert client.post.call_args.kwargs["headers"] == _CF_HEADERS

    @pytest.mark.asyncio
    async def test_non_slm_endpoint_no_cf_headers(self) -> None:
        client = self._mock_client()
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch(
                "personal_agent.memory.reranker.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
        ):
            await rerank("q", ["a"])

        assert client.post.call_args.kwargs["headers"] == {}


class TestRerankTelemetryJoinability:
    """FRE-698 (ADR-0074): rerank events carry identity + enrichment; per-call span;
    trace headers forwarded to the SLM server so its rerank log can join.
    """

    def _mock_client(self, results: list[dict[str, object]]) -> AsyncMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": results}
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=resp)
        return client

    @pytest.mark.asyncio
    async def test_applied_event_carries_identity_and_enrichment(self) -> None:
        from personal_agent.config import get_settings

        client = self._mock_client(
            [
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.9},
                {"index": 2, "relevance_score": 0.5},
            ]
        )
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("model-x", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            await rerank(
                "q",
                ["d0", "d1", "d2"],
                top_k=10,
                trace_id="tr-1",
                session_id="se-1",
                task_id="ta-1",
            )

        applied = [
            c for c in mock_log.info.call_args_list if c.args and c.args[0] == "reranker_applied"
        ]
        assert applied, "reranker_applied not emitted"
        kw = applied[0].kwargs
        assert kw["trace_id"] == "tr-1"
        assert kw["session_id"] == "se-1"
        assert kw["task_id"] == "ta-1"
        assert kw["span_id"]
        assert kw["model_id"] == "model-x"
        assert kw["candidate_count"] == 3
        assert kw["input_cap"] == get_settings().reranker_input_cap
        assert kw["result_count"] == 3
        assert kw["top_score"] == 0.9

    @pytest.mark.asyncio
    async def test_two_reranks_have_distinct_span_ids(self) -> None:
        client = self._mock_client([{"index": 0, "relevance_score": 0.5}])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("model-x", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            await rerank("q1", ["d"], trace_id="tr", session_id="se")
            await rerank("q2", ["d"], trace_id="tr", session_id="se")

        spans = [
            c.kwargs["span_id"]
            for c in mock_log.info.call_args_list
            if c.args and c.args[0] == "reranker_applied"
        ]
        assert len(spans) == 2
        assert spans[0] != spans[1]

    @pytest.mark.asyncio
    async def test_failed_event_carries_identity(self) -> None:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("model-x", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            results = await rerank("q", ["a", "b"], trace_id="tr", session_id="se", task_id="ta")

        failed = [
            c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "reranker_failed"
        ]
        assert failed, "reranker_failed not emitted"
        kw = failed[0].kwargs
        assert kw["trace_id"] == "tr"
        assert kw["session_id"] == "se"
        assert kw["task_id"] == "ta"
        assert kw["span_id"]
        assert kw["model_id"] == "model-x"
        assert kw["candidate_count"] == 2
        # graceful degradation still returns passthrough
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_task_id_none_is_explicit_contract(self) -> None:
        """task_id is genuinely absent on the recall path; the field is present and None."""
        client = self._mock_client([{"index": 0, "relevance_score": 0.5}])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("model-x", "http://localhost:8504/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            await rerank("q", ["d"], trace_id="tr", session_id="se")

        kw = [
            c.kwargs
            for c in mock_log.info.call_args_list
            if c.args and c.args[0] == "reranker_applied"
        ][0]
        assert "task_id" in kw
        assert kw["task_id"] is None

    @pytest.mark.asyncio
    async def test_trace_headers_sent_to_slm_when_trace_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.example.com")
        client = self._mock_client([])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("Voodisss/Qwen3-Reranker-4B", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch(
                "personal_agent.memory.reranker.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
        ):
            await rerank("q", ["a"], trace_id="tr-1", session_id="se-1")

        headers = client.post.call_args.kwargs["headers"]
        assert headers["X-Trace-Id"] == "tr-1"
        assert headers["X-Session-Id"] == "se-1"
        assert headers["X-Span-Id"]
        # CF-Access headers must be preserved alongside the trace headers.
        assert headers["CF-Access-Client-Id"] == "id"
        assert headers["CF-Access-Client-Secret"] == "sec"

    @pytest.mark.asyncio
    async def test_no_trace_headers_without_trace_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Context-less calls send only CF headers — no X-* injection (gating)."""
        monkeypatch.setattr(settings, "slm_tunnel_base_url", "https://slm.example.com")
        client = self._mock_client([])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("Voodisss/Qwen3-Reranker-4B", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch(
                "personal_agent.memory.reranker.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
        ):
            await rerank("q", ["a"])

        headers = client.post.call_args.kwargs["headers"]
        assert headers == _CF_HEADERS
        assert "X-Trace-Id" not in headers


class TestVoyagePrimary:
    """FRE-851 — Voyage rerank-2.5 as the PRIMARY reranker target."""

    def _mock_client(self, items: list[dict[str, object]]) -> AsyncMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": items}
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=resp)
        return client

    @pytest.mark.asyncio
    async def test_voyage_success_uses_top_k_request_and_data_response(self) -> None:
        client = self._mock_client(
            [
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.9},
            ]
        )
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            results = await rerank("q", ["d0", "d1"], top_k=10)

        assert len(results) == 2
        assert results[0].score == 0.9
        assert results[0].document == "d1"

        body = client.post.call_args.kwargs["json"]
        assert body["top_k"] == 10
        assert "top_n" not in body

    @pytest.mark.asyncio
    async def test_voyage_uses_bearer_auth_from_settings(self) -> None:
        client = self._mock_client([])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            await rerank("q", ["a"], top_k=5)

        headers = client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer test-voyage-key"

    @pytest.mark.asyncio
    async def test_voyage_does_not_receive_join_key_headers(self) -> None:
        """Internal correlation headers stay on our infra — Voyage never gets them."""
        client = self._mock_client([])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            await rerank("q", ["a"], top_k=5, trace_id="tr-1", session_id="se-1")

        headers = client.post.call_args.kwargs["headers"]
        assert "X-Trace-Id" not in headers
        assert "X-Session-Id" not in headers
        assert "X-Span-Id" not in headers

    @pytest.mark.asyncio
    async def test_voyage_success_logs_applied_with_fallback_false(self) -> None:
        client = self._mock_client([{"index": 0, "relevance_score": 0.5}])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            await rerank("q", ["a"], top_k=5)

        applied = [
            c for c in mock_log.info.call_args_list if c.args and c.args[0] == "reranker_applied"
        ]
        assert applied
        assert applied[0].kwargs["model_id"] == "rerank-2.5"
        assert applied[0].kwargs["fallback"] is False

    @pytest.mark.asyncio
    async def test_voyage_api_key_never_logged(self) -> None:
        client = self._mock_client([{"index": 0, "relevance_score": 0.5}])
        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "super-secret-voyage-key"
            await rerank("q", ["a"], top_k=5)

        for call in [*mock_log.info.call_args_list, *mock_log.warning.call_args_list]:
            assert "super-secret-voyage-key" not in str(call)


class TestFallbackToMacTunnel:
    """FRE-851 — Voyage failure/timeout falls back to the Mac-tunnel 4B, then passthrough."""

    @pytest.mark.asyncio
    async def test_voyage_failure_falls_back_and_succeeds(self) -> None:
        fallback_resp = MagicMock()
        fallback_resp.raise_for_status = MagicMock()
        fallback_resp.json.return_value = {"results": [{"index": 0, "relevance_score": 0.6}]}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=[httpx.ConnectError("voyage down"), fallback_resp])

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch(
                "personal_agent.memory.reranker._get_reranker_fallback_config",
                return_value=("Qwen/Qwen3-Reranker-4B-mxfp8", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch(
                "personal_agent.memory.reranker.cf_access_service_token_headers",
                return_value=dict(_CF_HEADERS),
            ),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            mock_settings.return_value.slm_tunnel_base_url = "https://slm.example.com"
            results = await rerank("q", ["a"], top_k=5, trace_id="tr-1", session_id="se-1")

        assert len(results) == 1
        assert results[0].score == 0.6

        # Second call went to the Mac tunnel with the legacy top_n + CF headers,
        # and (unlike the Voyage attempt) carries the trace join-key headers.
        second_call = client.post.call_args_list[1]
        assert second_call.kwargs["json"]["top_n"] == 5
        assert second_call.kwargs["headers"]["CF-Access-Client-Id"] == "id"
        assert second_call.kwargs["headers"]["X-Trace-Id"] == "tr-1"

        failed = [
            c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "reranker_failed"
        ]
        applied = [
            c for c in mock_log.info.call_args_list if c.args and c.args[0] == "reranker_applied"
        ]
        assert failed, "reranker_failed not logged for the Voyage attempt"
        assert applied, "reranker_applied not logged for the fallback success"
        assert applied[0].kwargs["model_id"] == "Qwen/Qwen3-Reranker-4B-mxfp8"
        assert applied[0].kwargs["fallback"] is True

    @pytest.mark.asyncio
    async def test_voyage_and_fallback_both_fail_returns_passthrough(self) -> None:
        """No regression to the existing passthrough backstop when both targets fail."""
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=httpx.ConnectError("down"))

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch(
                "personal_agent.memory.reranker._get_reranker_fallback_config",
                return_value=("Qwen/Qwen3-Reranker-4B-mxfp8", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            mock_settings.return_value.slm_tunnel_base_url = "https://slm.example.com"
            results = await rerank("q", ["a", "b"], top_k=5)

        assert len(results) == 2
        assert results[0].document == "a"  # passthrough preserves order

        fb_failed = [
            c
            for c in mock_log.warning.call_args_list
            if c.args and c.args[0] == "reranker_fallback_failed"
        ]
        assert fb_failed, "reranker_fallback_failed not logged"

    @pytest.mark.asyncio
    async def test_fallback_config_missing_returns_passthrough(self) -> None:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=httpx.ConnectError("voyage down"))

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch(
                "personal_agent.memory.reranker._get_reranker_fallback_config",
                side_effect=KeyError("reranker_fallback"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            results = await rerank("q", ["a"], top_k=5)

        assert len(results) == 1
        assert results[0].document == "a"

    @pytest.mark.asyncio
    async def test_missing_voyage_key_skips_directly_to_fallback_without_network_call(
        self,
    ) -> None:
        """No voyage_api_key -> fail fast (no "Bearer None" request, no wasted round-trip)."""
        fallback_resp = MagicMock()
        fallback_resp.raise_for_status = MagicMock()
        fallback_resp.json.return_value = {"results": [{"index": 0, "relevance_score": 0.7}]}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=fallback_resp)

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch(
                "personal_agent.memory.reranker._get_reranker_fallback_config",
                return_value=("Qwen/Qwen3-Reranker-4B-mxfp8", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
            patch("personal_agent.memory.reranker.log") as mock_log,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = None
            mock_settings.return_value.slm_tunnel_base_url = "https://slm.example.com"
            results = await rerank("q", ["a"], top_k=5)

        assert len(results) == 1
        assert results[0].score == 0.7

        # Exactly one HTTP call — the Voyage attempt never touched the network.
        assert client.post.call_count == 1

        failed = [
            c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "reranker_failed"
        ]
        assert failed
        assert "voyage_api_key" in failed[0].kwargs["error"]

    @pytest.mark.asyncio
    async def test_fallback_success_does_not_restart_the_clock(self) -> None:
        """FRE-851 telemetry fix: the fallback path measures total elapsed time since the
        ORIGINAL (primary) attempt began, not a fresh timer started inside _rerank_fallback —
        otherwise duration_ms on a degraded turn silently hides the failed primary's latency.
        """
        import time as time_module

        fallback_resp = MagicMock()
        fallback_resp.raise_for_status = MagicMock()
        fallback_resp.json.return_value = {"results": [{"index": 0, "relevance_score": 0.5}]}

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=[httpx.ConnectError("voyage down"), fallback_resp])

        real_monotonic = time_module.monotonic

        with (
            patch(
                "personal_agent.memory.reranker._get_reranker_config",
                return_value=("rerank-2.5", "https://api.voyageai.com/v1"),
            ),
            patch(
                "personal_agent.memory.reranker._get_reranker_fallback_config",
                return_value=("Qwen/Qwen3-Reranker-4B-mxfp8", "https://slm.example.com/v1"),
            ),
            patch("personal_agent.memory.reranker.httpx.AsyncClient", return_value=client),
            patch("personal_agent.memory.reranker.get_settings") as mock_settings,
            patch(
                "personal_agent.memory.reranker.time.monotonic", wraps=real_monotonic
            ) as mock_monotonic,
        ):
            mock_settings.return_value.reranker_enabled = True
            mock_settings.return_value.reranker_input_cap = 25
            mock_settings.return_value.voyage_api_key = "test-voyage-key"
            mock_settings.return_value.slm_tunnel_base_url = "https://slm.example.com"
            await rerank("q", ["a"], top_k=5)

        # Exactly 3 reads: rerank()'s start, the reranker_failed duration, and the
        # fallback's final duration computed FROM that same start — not 4, which
        # would mean _rerank_fallback started its own independent clock.
        assert mock_monotonic.call_count == 3


class TestPassthrough:
    def test_preserves_order(self) -> None:
        results = _passthrough(["first", "second", "third"])
        assert len(results) == 3
        assert results[0].document == "first"
        assert results[0].index == 0
        assert results[1].index == 1

    def test_decreasing_scores(self) -> None:
        results = _passthrough(["a", "b", "c"])
        assert results[0].score > results[1].score > results[2].score

    def test_frozen_dataclass(self) -> None:
        result = RerankResult(index=0, score=0.5, document="test")
        with pytest.raises(AttributeError):
            result.score = 0.9  # type: ignore[misc]
