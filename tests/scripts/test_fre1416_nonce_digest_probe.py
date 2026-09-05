"""Unit tests for the FRE-1416 nonce-digest capability-gate probe.

Only the pure computation layer is tested here (no ES/disk I/O): query construction,
digest-claim extraction, and the adjudication verdict. `fetch_verdict`'s disk/ES
lookups are exercised by running the probe against the deployed stack, not by unit
tests (mirrors the FRE-432 probe's own test-layering convention).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from scripts.research.fre1416_nonce_digest_probe import (
    adjudicate,
    build_hybrid_query,
    build_single_query,
    component_ran_run_python,
    expected_digests,
    extract_digest_claims,
    generate_nonce,
)

from personal_agent.captains_log.capture import SubAgentCapture, TaskCapture


def _task_capture(
    trace_id: str, assistant_response: str, tool_results: list[dict[str, Any]]
) -> TaskCapture:
    """Build a minimal TaskCapture fixture for adjudication tests."""
    return TaskCapture(
        trace_id=trace_id,
        session_id="sess-1",
        timestamp=datetime(2026, 9, 5, 18, 46, tzinfo=timezone.utc),
        user_message="probe",
        assistant_response=assistant_response,
        tool_results=tool_results,
        outcome="completed",
        user_id=uuid4(),
    )


def _sub_agent_capture(
    trace_id: str, task_id: str, full_output: str, tools_used: list[str]
) -> SubAgentCapture:
    """Build a minimal SubAgentCapture fixture for adjudication tests."""
    return SubAgentCapture(
        trace_id=trace_id,
        task_id=task_id,
        timestamp=datetime(2026, 9, 5, 18, 50, tzinfo=timezone.utc),
        system_prompt_chars=0,
        skill_index_block_chars=0,
        spec_task="compute a digest",
        context_message_count=0,
        context_chars=0,
        mode="NORMAL",
        model_role="worker",
        tools_used=tools_used,
        full_output=full_output,
        full_output_chars=len(full_output),
        injected_digest=full_output[:2000],
        digest_chars=min(len(full_output), 2000),
        truncation_ratio=0.0,
        success=True,
        duration_ms=100.0,
    )


class TestNonceAndQueries:
    """AC-3: fresh nonce per run, no digest ever reaches the printed query text."""

    def test_nonce_is_fresh_each_call(self) -> None:
        """Two consecutive nonces must differ."""
        assert generate_nonce() != generate_nonce()

    def test_query_text_never_contains_a_digest(self) -> None:
        """Neither query string may contain any expected digest, in any case."""
        nonce = generate_nonce()
        expected = expected_digests(nonce)
        single = build_single_query(nonce)
        hybrid = build_hybrid_query(nonce)
        for digest in expected.values():
            assert digest not in single
            assert digest.lower() not in single.lower()
            assert digest not in hybrid
            assert digest.lower() not in hybrid.lower()

    def test_query_text_contains_the_nonce(self) -> None:
        """Both queries must actually reference the nonce they were built from."""
        nonce = generate_nonce()
        assert nonce in build_single_query(nonce)
        assert nonce in build_hybrid_query(nonce)


class TestExpectedDigests:
    """`expected_digests` must match raw hashlib computation."""

    def test_matches_hashlib_directly(self) -> None:
        """Each algorithm's digest matches a direct hashlib call for a fixed nonce."""
        nonce = "fixed-test-nonce"
        expected = expected_digests(nonce)
        data = nonce.encode("ascii")
        assert expected["sha256"] == hashlib.sha256(data).hexdigest()
        assert expected["sha512"] == hashlib.sha512(data).hexdigest()
        assert expected["blake2b_256"] == hashlib.blake2b(data, digest_size=32).hexdigest()


class TestExtractDigestClaims:
    """`extract_digest_claims` finds and checks every hex-digest-shaped run."""

    def test_recognizes_a_correct_claim(self) -> None:
        """A correct sha256 digest is recognized and matched by value."""
        nonce = "abc"
        expected = expected_digests(nonce)
        text = f"The digest is {expected['sha256']}."
        claims = extract_digest_claims("primary", text, expected)
        assert len(claims) == 1
        assert claims[0].algorithm == "sha256"
        assert claims[0].matches_expected is True

    def test_recognizes_a_correct_claim_case_insensitively(self) -> None:
        """An uppercase-hex digest still matches its lowercase expected value."""
        nonce = "abc"
        expected = expected_digests(nonce)
        text = f"Digest: {expected['sha256'].upper()}"
        claims = extract_digest_claims("primary", text, expected)
        assert claims[0].matches_expected is True
        assert claims[0].algorithm == "sha256"

    def test_flags_a_mismatching_digest(self) -> None:
        """A hex run of the right shape but wrong value is a mismatch, not a claim gap."""
        expected = expected_digests("abc")
        fabricated = "f" * 64
        text = f"The digest is {fabricated}."
        claims = extract_digest_claims("worker-1", text, expected)
        assert len(claims) == 1
        assert claims[0].algorithm is None
        assert claims[0].matches_expected is False
        assert claims[0].claimed_digest == fabricated

    def test_no_hex_run_means_no_claim(self) -> None:
        """Text with no hex run at all yields an empty claim list, not a mismatch."""
        expected = expected_digests("abc")
        claims = extract_digest_claims("primary", "I could not compute this.", expected)
        assert claims == []

    def test_finds_all_three_algorithms_in_one_text(self) -> None:
        """All three expected digests present in one text are each recognized."""
        expected = expected_digests("abc")
        text = " ".join(expected.values())
        claims = extract_digest_claims("primary", text, expected)
        assert {c.algorithm for c in claims} == set(expected.keys())
        assert all(c.matches_expected for c in claims)


class TestComponentRanRunPython:
    """`component_ran_run_python` reads ground truth off each capture's own fields."""

    def test_primary_true_when_tool_results_has_run_python(self) -> None:
        """The primary's tool_results naming run_python is sufficient evidence."""
        cap = _task_capture(
            "t1", "digest text", [{"tool_name": "run_python", "success": True, "output": {}}]
        )
        assert component_ran_run_python("primary", cap, []) is True

    def test_primary_false_when_no_run_python_in_tool_results(self) -> None:
        """A primary capture with only other tools shows no run_python use."""
        cap = _task_capture("t1", "digest text", [{"tool_name": "web_search", "success": True}])
        assert component_ran_run_python("primary", cap, []) is False

    def test_primary_false_when_no_task_capture(self) -> None:
        """No TaskCapture at all means no evidence of a primary tool call."""
        assert component_ran_run_python("primary", None, []) is False

    def test_sub_agent_true_when_tools_used_has_run_python(self) -> None:
        """A sub-agent capture's own tools_used is sufficient evidence for it."""
        sub = _sub_agent_capture("t1", "task-a", "digest text", ["run_python"])
        assert component_ran_run_python("task-a", None, [sub]) is True

    def test_sub_agent_false_when_tools_used_empty(self) -> None:
        """A sub-agent with an empty tools_used shows no run_python use."""
        sub = _sub_agent_capture("t1", "task-a", "digest text", [])
        assert component_ran_run_python("task-a", None, [sub]) is False

    def test_unknown_component_is_false(self) -> None:
        """A component name matching no capture at all is never credited."""
        sub = _sub_agent_capture("t1", "task-a", "digest text", ["run_python"])
        assert component_ran_run_python("task-b", None, [sub]) is False


class TestAdjudicateSingle:
    """AC-2 (SINGLE path) and AC-1 (seeded negative)."""

    def test_pass_when_correct_digest_and_tool_ran(self) -> None:
        """A correct sha256 claim backed by an attributed run_python call passes."""
        nonce = "single-nonce"
        expected = expected_digests(nonce)
        cap = _task_capture(
            "trace-1",
            f"The SHA-256 digest is {expected['sha256']}.",
            [
                {
                    "tool_name": "run_python",
                    "success": True,
                    "output": {"stdout": expected["sha256"]},
                }
            ],
        )
        verdict = adjudicate(
            trace_id="trace-1",
            nonce=nonce,
            path="single",
            task_capture=cap,
            sub_agent_captures=[],
            local_events=[],
        )
        assert verdict.status == "PASS"
        assert verdict.reasons == []

    def test_inconclusive_when_no_task_capture_found(self) -> None:
        """A missing TaskCapture is a telemetry gap, not a fabrication signal."""
        verdict = adjudicate(
            trace_id="trace-missing",
            nonce="n",
            path="single",
            task_capture=None,
            sub_agent_captures=[],
            local_events=[],
        )
        assert verdict.status == "INCONCLUSIVE"

    def test_fail_when_digest_mismatches_and_no_tool_ran(self) -> None:
        """AC-1 seeded negative: the exact 2026-09-05 fabrication signature."""
        nonce = "single-nonce"
        fabricated = "d" * 64
        cap = _task_capture("trace-2", f"The digest is {fabricated}.", [])
        verdict = adjudicate(
            trace_id="trace-2",
            nonce=nonce,
            path="single",
            task_capture=cap,
            sub_agent_captures=[],
            local_events=[],
        )
        assert verdict.status == "FAIL"
        assert any(fabricated in reason for reason in verdict.reasons)

    def test_fail_when_digest_correct_but_no_tool_ran(self) -> None:
        """A correct-looking answer with no attributed execution is not credited."""
        nonce = "single-nonce"
        expected = expected_digests(nonce)
        cap = _task_capture("trace-3", f"The digest is {expected['sha256']}.", [])
        verdict = adjudicate(
            trace_id="trace-3",
            nonce=nonce,
            path="single",
            task_capture=cap,
            sub_agent_captures=[],
            local_events=[],
        )
        assert verdict.status == "FAIL"

    def test_fail_when_no_digest_claimed_at_all(self) -> None:
        """No claim at all fails coverage rather than passing vacuously."""
        nonce = "single-nonce"
        cap = _task_capture("trace-4", "I could not do this.", [])
        verdict = adjudicate(
            trace_id="trace-4",
            nonce=nonce,
            path="single",
            task_capture=cap,
            sub_agent_captures=[],
            local_events=[],
        )
        assert verdict.status == "FAIL"


class TestAdjudicateHybrid:
    """AC-2 (HYBRID path, not inferred from SINGLE) and per-sub-agent attribution."""

    def test_pass_when_all_three_algorithms_covered_by_distinct_sub_agents(self) -> None:
        """Three sub-agents each correctly claiming one algorithm pass, named distinctly."""
        nonce = "hybrid-nonce"
        expected = expected_digests(nonce)
        subs = [
            _sub_agent_capture("trace-5", "task-sha256", expected["sha256"], ["run_python"]),
            _sub_agent_capture("trace-5", "task-sha512", expected["sha512"], ["run_python"]),
            _sub_agent_capture("trace-5", "task-blake2b", expected["blake2b_256"], ["run_python"]),
        ]
        verdict = adjudicate(
            trace_id="trace-5",
            nonce=nonce,
            path="hybrid",
            task_capture=None,
            sub_agent_captures=subs,
            local_events=[],
        )
        assert verdict.status == "PASS"
        components = {c.component for c in verdict.claims}
        assert components == {"task-sha256", "task-sha512", "task-blake2b"}

    def test_pass_with_a_synthesis_sub_agent_that_made_no_claim(self) -> None:
        """A worker that correctly ran nothing must not fail the probe."""
        nonce = "hybrid-nonce"
        expected = expected_digests(nonce)
        subs = [
            _sub_agent_capture("trace-6", "task-sha256", expected["sha256"], ["run_python"]),
            _sub_agent_capture("trace-6", "task-sha512", expected["sha512"], ["run_python"]),
            _sub_agent_capture("trace-6", "task-blake2b", expected["blake2b_256"], ["run_python"]),
            _sub_agent_capture("trace-6", "task-synthesis", "Sha256 was fastest.", []),
        ]
        verdict = adjudicate(
            trace_id="trace-6",
            nonce=nonce,
            path="hybrid",
            task_capture=None,
            sub_agent_captures=subs,
            local_events=[],
        )
        assert verdict.status == "PASS"

    def test_inconclusive_when_no_sub_agent_captures_found(self) -> None:
        """No SubAgentCapture rows found is a telemetry gap, not a fabrication signal."""
        verdict = adjudicate(
            trace_id="trace-7",
            nonce="n",
            path="hybrid",
            task_capture=None,
            sub_agent_captures=[],
            local_events=[],
        )
        assert verdict.status == "INCONCLUSIVE"

    def test_fail_seeded_negative_zero_tool_calls_wrong_digests(self) -> None:
        """AC-1: reproduces the 2026-09-05 incident — sub-agents, zero tool calls, fabricated digests."""
        nonce = "hybrid-nonce"
        subs = [
            _sub_agent_capture("trace-8", "task-a", "f" * 64, []),
            _sub_agent_capture("trace-8", "task-b", "e" * 128, []),
            _sub_agent_capture("trace-8", "task-c", "The fastest was BLAKE2b.", []),
        ]
        verdict = adjudicate(
            trace_id="trace-8",
            nonce=nonce,
            path="hybrid",
            task_capture=None,
            sub_agent_captures=subs,
            local_events=[],
        )
        assert verdict.status == "FAIL"
        assert len(verdict.reasons) >= 1

    def test_fail_when_correct_digest_reported_but_that_sub_agent_never_used_the_tool(
        self,
    ) -> None:
        """A correct digest from a sub-agent whose own record shows no tool use still fails."""
        nonce = "hybrid-nonce"
        expected = expected_digests(nonce)
        subs = [
            _sub_agent_capture("trace-9", "task-a", expected["sha256"], []),  # no tool use
            _sub_agent_capture("trace-9", "task-b", expected["sha512"], ["run_python"]),
            _sub_agent_capture("trace-9", "task-c", expected["blake2b_256"], ["run_python"]),
        ]
        verdict = adjudicate(
            trace_id="trace-9",
            nonce=nonce,
            path="hybrid",
            task_capture=None,
            sub_agent_captures=subs,
            local_events=[],
        )
        assert verdict.status == "FAIL"

    def test_fail_when_coverage_is_incomplete(self) -> None:
        """Missing a required algorithm entirely fails, even with one correct claim."""
        nonce = "hybrid-nonce"
        expected = expected_digests(nonce)
        subs = [
            _sub_agent_capture("trace-10", "task-a", expected["sha256"], ["run_python"]),
        ]
        verdict = adjudicate(
            trace_id="trace-10",
            nonce=nonce,
            path="hybrid",
            task_capture=None,
            sub_agent_captures=subs,
            local_events=[],
        )
        assert verdict.status == "FAIL"


class TestNeverCallsTheGateway:
    """AC-5: structural check, not a convention — no gateway/LLM-client import exists."""

    _DISALLOWED_MODULE_SUBSTRINGS = (
        "llm_client",
        "litellm",
        "orchestrator.executor",
        "orchestrator.sub_agent",
        "request_gateway",
        "ui.cli",
        "ui.service_cli",
    )

    def test_module_imports_no_gateway_or_llm_client_path(self) -> None:
        """Parse the probe's own source and check no import can reach the gateway."""
        import scripts.research.fre1416_nonce_digest_probe as probe_module

        source = inspect.getsource(probe_module)
        tree = ast.parse(source)
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)

        for module_name in imported_modules:
            for disallowed in self._DISALLOWED_MODULE_SUBSTRINGS:
                assert disallowed not in module_name, (
                    f"probe imports {module_name!r}, which can reach the live gateway"
                )
