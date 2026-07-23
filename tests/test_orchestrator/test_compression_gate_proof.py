"""FRE-908 — prove the within-session compression gate can trigger (ADR-0061).

Measurement-only per the ticket's scope guard: no threshold change, no flag flip,
no live gateway turns. All fixtures are synthetic and offline.

Findings are written up in ``docs/research/2026-07-17-fre-908-compression-gate-proof.md``;
these tests are the evidence backing that report's per-AC claims.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

import pytest
import tiktoken

from personal_agent.config import settings
from personal_agent.llm_client.message_content import count_content_tokens
from personal_agent.orchestrator import within_session_compression as wsc
from personal_agent.orchestrator.context_compressor import FALLBACK_MARKER
from personal_agent.orchestrator.context_window import (
    estimate_message_tokens,
    estimate_messages_tokens,
)
from tests._helpers.telemetry_mounts import gateway_mounts, is_mount_covered, load_compose


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


def _big_tool_body(target_tokens: int) -> str:
    """A JSON tool-result body whose cl100k_base token count is ~target_tokens.

    Calibrated against a real encode rather than an assumed chars-per-token
    ratio: repeated short strings compress under BPE in a way that a flat
    "N chars per token" estimate gets wrong by ~1.7x (measured while building
    this fixture — the naive divide-by-3 undershot the intended size and
    produced a body 1.7x larger than requested).
    """
    encoding = tiktoken.get_encoding("cl100k_base")
    sample_n = 200
    sample_body = json.dumps({"results": ["x" * 20 for _ in range(sample_n)]})
    tokens_per_element = len(encoding.encode(sample_body)) / sample_n
    n = max(1, int(target_tokens / tokens_per_element))
    return json.dumps({"results": ["x" * 20 for _ in range(n)]})


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

        needed_tokens = hard_threshold - pre_tokens + 2000
        big_body = _big_tool_body(needed_tokens)
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
    async def test_compress_in_place_now_shrinks_a_tail_resident_spike(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRE-942 — the spike the gate fires for is now actually reduced.

        **This assertion is inverted from the one FRE-908 checked in**, and the
        inversion is the point. FRE-908 measured that ``compress_in_place``
        achieved *zero* reduction here (``tokens_saved == 0``,
        ``middle_tokens_out == middle_tokens_in``, output the same size as
        input): the tail band was preserved verbatim with a token floor
        (0.25 × 96,000 = 24,000) but **no ceiling**, so any tool response big
        enough to single-handedly cross the hard threshold (~81,600) was, by
        construction, also big enough to satisfy the tail's floor on its own —
        swept wholesale into the protected tail, never reaching the pre-pass or
        summariser, both of which only operate on the middle band. ADR-0061 §D1
        cites "large tool responses spiking mid-turn" as the hard trigger's own
        rationale, so the gate fired and did nothing in its own design scenario.

        FRE-942 gave the tail a ceiling and made it a user-anchored contiguous
        suffix. The oversized tool message now exceeds the ceiling and carries
        no user turn, so it falls to the middle band, where
        ``_pre_pass_tool_outputs`` replaces it with a one-line descriptor. Same
        fixture, same gate, real reduction.

        (Same pattern as this file's AC-4 test, which FRE-910 renamed and flipped
        once the durability gap it documented was closed.)
        """
        monkeypatch.setattr(
            "personal_agent.telemetry.within_session_compression._default_output_dir",
            lambda: tmp_path,
        )

        max_tokens = settings.context_window_max_tokens
        hard_threshold = int(settings.within_session_hard_threshold_ratio * max_tokens)
        tail_floor = int(settings.within_session_min_tail_ratio * max_tokens)
        tail_ceiling = int(settings.within_session_max_tail_ratio * max_tokens)
        pre_messages = self._pre_turn_messages()
        pre_tokens = estimate_messages_tokens(pre_messages)
        needed_tokens = hard_threshold - pre_tokens + 2000
        big_body = _big_tool_body(needed_tokens)
        messages = pre_messages + _tool_pair("call-big", big_body)
        input_tokens = estimate_messages_tokens(messages)
        assert wsc.needs_hard_compression(messages, max_tokens) is True

        # Deterministic, offline: no live LLM dispatch (matches the
        # test_within_session_compression.py convention). The middle band
        # here has no tool messages to pre-pass, so the summariser would be
        # invoked on plain conversational text regardless of whether the
        # spike itself gets compressed — mock it out rather than relying on
        # the test environment's CostGate-unregistered fast-fail as an
        # incidental substitute for isolation.
        async def fake_compress_turns(
            msgs: list, trace_id: str = "", session_id: str | None = None
        ) -> str:
            return FALLBACK_MARKER

        with patch(
            "personal_agent.orchestrator.context_compressor.compress_turns",
            side_effect=fake_compress_turns,
        ):
            compressed, record = await wsc.compress_in_place(
                messages,
                trace_id="t1",
                session_id="s1",
                trigger="hard",
                bus=None,
            )

        assert record.trigger == "hard"
        # The spike no longer hides in the protected tail: the ceiling bounds the
        # band, and the spike carries no user turn to anchor one.
        assert record.tail_tokens <= tail_ceiling
        # It therefore reaches the pre-pass, which replaces it with a descriptor.
        assert record.pre_pass_replacements >= 1
        assert record.middle_tokens_out < record.middle_tokens_in
        assert record.tokens_saved > 0
        # And the working set genuinely shrinks — below the gate that fired.
        assert estimate_messages_tokens(compressed) < input_tokens
        assert estimate_messages_tokens(compressed) < hard_threshold
        # Guard against the regression this ticket fixed: the old code returned a
        # tail larger than its own floor because the floor had no ceiling.
        assert record.tail_tokens < tail_floor


class TestAccumulatedTailIsBounded:
    """FRE-942 — the shape the *production* records actually show.

    FRE-908's Finding 3 reproduced one oversized trailing message. The 289 real
    compaction records in ``agent-logs-*`` (reproducible via
    ``scripts/audit/fre942_compaction_census.py``) show the general form: 44% of
    passes achieved zero-or-negative net reduction, and the worst left a
    post-compaction working set of 254,484 tokens — 2.65x the 96,000-token window —
    with ``tail_tokens = 254,071``.

    The cause is that ``_extract_tail`` enforced two floors (``min_tokens``,
    ``min_turns``) and **no ceiling**: ``min_turns`` kept pulling messages in after
    the token floor was already satisfied, with no bound on any of them. Several
    merely-large trailing results therefore accumulated into a verbatim-preserved
    band many times the window.
    """

    @pytest.mark.asyncio
    async def test_several_large_trailing_results_do_not_escape_into_the_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "personal_agent.telemetry.within_session_compression._default_output_dir",
            lambda: tmp_path,
        )

        max_tokens = settings.context_window_max_tokens
        tail_ceiling = int(settings.within_session_max_tail_ratio * max_tokens)

        # Four trailing tool rounds, each individually well under the old 24,000
        # tail floor — so none of them is the "oversized single message" FRE-908
        # found. Together they are what the production records show.
        messages: list[dict[str, Any]] = [
            _msg("system", "system prompt"),
            _msg("user", "start the task"),
        ]
        for i in range(5):
            messages.append(_msg("user", f"step {i}"))
            messages.extend(_tool_pair(f"call-{i}", _big_tool_body(20_000)))

        input_tokens = estimate_messages_tokens(messages)
        assert wsc.needs_hard_compression(messages, max_tokens) is True

        async def fake_compress_turns(
            msgs: list, trace_id: str = "", session_id: str | None = None
        ) -> str:
            return FALLBACK_MARKER

        with patch(
            "personal_agent.orchestrator.context_compressor.compress_turns",
            side_effect=fake_compress_turns,
        ):
            compressed, record = await wsc.compress_in_place(
                messages,
                trace_id="t-accum",
                session_id="s-accum",
                trigger="hard",
                bus=None,
            )

        # The band that grew without bound is now bounded.
        assert record.tail_tokens <= tail_ceiling
        # The results that no longer fit the tail reached the pre-pass instead.
        assert record.pre_pass_replacements >= 1
        assert record.tokens_saved > 0
        # And the pass actually did its job: the working set drops back under the
        # gate that fired it, which is what the production records never achieved.
        assert estimate_messages_tokens(compressed) < input_tokens
        assert estimate_messages_tokens(compressed) < int(
            settings.within_session_hard_threshold_ratio * max_tokens
        )


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

    # test_soft_trigger_call_site_guard_is_closed_under_production_default was removed
    # with the cache_frozen_layout_enabled flag (FRE-941): the reactive soft trigger
    # and compression_manager are deleted outright, so there is no call-site guard to pin.


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

    def test_gateway_volumes_mount_within_session_compression_durably(
        self,
    ) -> None:
        """FRE-908 found ADR-0061's own stream ephemeral (unlike ADR-0059's
        sibling, mounted individually). FRE-910 fixed this by mounting the
        /app/telemetry parent once, covering every writer — this assertion
        flips from FRE-908's original negative (proving the gap) to confirm
        the fix landed; see TestTelemetryMountCoverage for the general guard.
        """
        repo_root = Path(__file__).resolve().parents[2]
        mounts = gateway_mounts(load_compose(repo_root))

        assert is_mount_covered(PurePosixPath("/app/telemetry/context_quality"), mounts)
        assert is_mount_covered(PurePosixPath("/app/telemetry/within_session_compression"), mounts)
