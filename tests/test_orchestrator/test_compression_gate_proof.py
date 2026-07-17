"""FRE-908 — prove the within-session compression gate can trigger (ADR-0061).

Measurement-only per the ticket's scope guard: no threshold change, no flag flip,
no live gateway turns. All fixtures are synthetic and offline.

Findings are written up in ``docs/research/2026-07-17-fre-908-compression-gate-proof.md``;
these tests are the evidence backing that report's per-AC claims.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import tiktoken
import yaml

from personal_agent.config import settings
from personal_agent.llm_client.message_content import count_content_tokens
from personal_agent.orchestrator import within_session_compression as wsc
from personal_agent.orchestrator.context_window import (
    estimate_message_tokens,
    estimate_messages_tokens,
)


def _msg(role: str, content: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": role, "content": content}
    out.update(extra)
    return out


def _tool_pair(tool_call_id: str, body: str) -> list[dict[str, Any]]:
    """One assistant tool_calls message + its matching tool-result message."""
    return [
        _msg(
            "assistant",
            "",
            tool_calls=[{"id": tool_call_id, "function": {"name": "search", "arguments": "{}"}}],
        ),
        _msg("tool", body, tool_call_id=tool_call_id),
    ]


# ---------------------------------------------------------------------------
# AC-1 — the hard gate fires on a genuinely oversized history
# ---------------------------------------------------------------------------


class TestHardGateFiresAtProductionScale:
    """Models the scenario ADR-0061 §D1 was built for: a single large tool
    response spiking mid-turn, between two frozen-reset (ADR-0081) evaluations.

    Not "build until estimate_messages_tokens exceeds the threshold, then assert
    needs_hard_compression" — that would be circular, since needs_hard_compression
    *is* estimate_messages_tokens(messages) >= threshold. Instead this proves the
    *transition* a real mid-turn tool response causes.
    """

    def _pre_turn_messages(self) -> list[dict[str, Any]]:
        messages = [
            _msg("system", "system prompt"),
            _msg("user", "start the task"),
        ]
        # A handful of small prior turns — keeps head/tail extraction realistic
        # without approaching the frozen-reset ceiling.
        for i in range(6):
            messages.append(_msg("user", f"follow-up {i}"))
            messages.append(_msg("assistant", f"reply {i}"))
        return messages

    def test_pre_and_post_span_the_hard_threshold(self) -> None:
        max_tokens = settings.context_window_max_tokens
        frozen_ceiling = int(settings.cache_frozen_accum_max_ratio * max_tokens)
        hard_threshold = int(settings.within_session_hard_threshold_ratio * max_tokens)
        reserved_floor = max_tokens - 4500  # apply_context_window default reserve

        # Guard the fixture's premise: if intra-turn digest were ever enabled it
        # would shrink the tool body before the hard gate sees it, silently
        # invalidating the sizing below.
        assert settings.tool_result_compression_enabled is False

        pre_messages = self._pre_turn_messages()
        pre_tokens = estimate_messages_tokens(pre_messages)
        assert pre_tokens < frozen_ceiling, (
            "fixture premise violated: pre-turn history already at/above the "
            "frozen-reset ceiling — widen the gap before adding the tool result"
        )

        # tiktoken cl100k_base ≈ 4 chars/token; oversize generously so the
        # single tool result alone crosses the hard threshold from here.
        needed_tokens = hard_threshold - pre_tokens + 2000
        big_body = json.dumps({"results": ["x" * 20 for _ in range(needed_tokens // 3)]})
        messages = pre_messages + _tool_pair("call-big", big_body)

        post_tokens = estimate_messages_tokens(messages)
        assert post_tokens >= hard_threshold
        assert post_tokens < reserved_floor, (
            "fixture premise violated: post-turn total collides with "
            "apply_context_window's own trim floor — the hard gate wouldn't be "
            "the mechanism actually observed at this size"
        )

        assert wsc.needs_hard_compression(pre_messages, max_tokens) is False
        assert wsc.needs_hard_compression(messages, max_tokens) is True

    @pytest.mark.asyncio
    async def test_compress_in_place_shrinks_oversized_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "personal_agent.telemetry.within_session_compression._default_output_dir",
            lambda: tmp_path,
        )

        max_tokens = settings.context_window_max_tokens
        hard_threshold = int(settings.within_session_hard_threshold_ratio * max_tokens)
        pre_messages = self._pre_turn_messages()
        pre_tokens = estimate_messages_tokens(pre_messages)
        needed_tokens = hard_threshold - pre_tokens + 2000
        big_body = json.dumps({"results": ["x" * 20 for _ in range(needed_tokens // 3)]})
        messages = pre_messages + _tool_pair("call-big", big_body)
        input_tokens = estimate_messages_tokens(messages)

        compressed, record = await wsc.compress_in_place(
            messages,
            trace_id="t1",
            session_id="s1",
            trigger="hard",
            bus=None,
        )

        assert record.trigger == "hard"
        assert record.middle_tokens_out < record.middle_tokens_in
        assert record.tokens_saved > 0
        assert estimate_messages_tokens(compressed) < input_tokens


# ---------------------------------------------------------------------------
# AC-2 — estimator reconciliation
# ---------------------------------------------------------------------------


class TestEstimatorReconciliation:
    def test_estimator_matches_cl100k_encoding(self) -> None:
        """The estimator is already tiktoken-backed — no heuristic-vs-encoding
        gap for what it counts. Documents that the ticket's implicit "crude
        estimator" premise doesn't hold; the real gap is the blind spot below.
        """
        text = "The quick brown fox jumps over the lazy dog. " * 50
        encoding = tiktoken.get_encoding("cl100k_base")
        assert count_content_tokens(text) == len(encoding.encode(text))

    def test_reasoning_content_is_invisible_to_the_estimator(self) -> None:
        """Quantifies the thinking blind spot: assistant messages carry a
        ``reasoning_content`` field (executor.py, Qwen3.6 unsloth convention)
        that estimate_message_tokens never reads.
        """
        encoding = tiktoken.get_encoding("cl100k_base")
        content = "Here is the answer."
        reasoning = "Let me think through this step by step. " * 400  # ~3.4k tokens

        msg = _msg("assistant", content, reasoning_content=reasoning)

        estimated = estimate_message_tokens(msg)
        real_total = len(encoding.encode(content)) + len(encoding.encode(reasoning))

        assert estimated < real_total
        gap_ratio = real_total / estimated
        assert gap_ratio > 1.5, (
            f"expected the reasoning trace to dominate the real total; "
            f"got estimated={estimated}, real_total={real_total}, ratio={gap_ratio:.2f}"
        )


# ---------------------------------------------------------------------------
# AC-3 — precedence between the three trim mechanisms
# ---------------------------------------------------------------------------


class TestPrecedenceOrdering:
    def test_frozen_reset_ceiling_is_tighter_than_both_adr_0061_thresholds(self) -> None:
        """Quantifies Finding 0: the ADR-0081 frozen-reset ceiling sits below
        both ADR-0061 thresholds under production defaults, so steady growth
        is compacted by the scheduler before either ADR-0061 gate would ever
        see it. Computed from live settings so it self-invalidates on drift.
        """
        max_tokens = settings.context_window_max_tokens
        frozen_ceiling = int(settings.cache_frozen_accum_max_ratio * max_tokens)
        soft_threshold = int(settings.context_compression_threshold_ratio * max_tokens)
        hard_threshold = int(settings.within_session_hard_threshold_ratio * max_tokens)

        assert frozen_ceiling < soft_threshold < hard_threshold

    def test_soft_trigger_call_site_guard_is_closed_under_production_default(self) -> None:
        """Documents the precondition for Finding 0's "soft trigger is dead by
        design" claim: cache_frozen_layout_enabled defaults True in production
        (settings.py), which makes the executor.py:4842 call-site guard
        ``if ctx.session_id and not settings.cache_frozen_layout_enabled:``
        evaluate False — compression_manager.maybe_trigger_compression is never
        reached. See docs/research/2026-07-17-fre-908-compression-gate-proof.md
        for the full call-order trace (file:line citations); this test pins
        the settings precondition that trace depends on.
        """
        assert settings.cache_frozen_layout_enabled is True


# ---------------------------------------------------------------------------
# AC-4 — telemetry durability
# ---------------------------------------------------------------------------


class TestTelemetryDurability:
    def test_output_dir_is_cwd_relative(self) -> None:
        from personal_agent.telemetry.within_session_compression import (
            _default_output_dir,
        )

        output_dir = _default_output_dir()
        assert not output_dir.is_absolute()
        assert output_dir.parts == ("telemetry", "within_session_compression")

    def test_gateway_volumes_mount_context_quality_but_not_within_session_compression(
        self,
    ) -> None:
        """ADR-0059's sibling stream is durable in the deployed gateway;
        ADR-0061's own stream is not — the container's ephemeral layer under
        /app loses every WSC-*.jsonl record on rebuild.
        """
        repo_root = Path(__file__).resolve().parents[2]
        compose_path = repo_root / "docker-compose.cloud.yml"
        doc = yaml.safe_load(compose_path.read_text())
        volumes = doc["services"]["seshat-gateway"]["volumes"]
        targets = [
            v.split(":", 1)[1].split(":", 1)[0] for v in volumes if isinstance(v, str) and ":" in v
        ]

        assert "/app/telemetry/context_quality" in targets
        assert "/app/telemetry/within_session_compression" not in targets
