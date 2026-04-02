"""Core evaluation runner.

Sends conversation turns to the agent API
and checks telemetry assertions via Elasticsearch.

Usage:
    runner = EvaluationRunner(agent_url="http://localhost:9000")
    result = await runner.run_path(path)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import datetime, timezone

import httpx
import structlog

from tests.evaluation.harness.models import (
    AssertionResult,
    ConversationPath,
    PathResult,
    TelemetryAssertion,
    TurnResult,
)
from tests.evaluation.harness.neo4j_checker import Neo4jChecker
from tests.evaluation.harness.telemetry import TelemetryChecker

log = structlog.get_logger(__name__)

DEFAULT_AGENT_URL = "http://localhost:9000"
DEFAULT_CHAT_TIMEOUT_S = 300.0
DEFAULT_INTER_TURN_DELAY_S = 2.0
# Time to wait between paths — lets the inference server flush KV cache and
# recover thermals before the next path starts. 0 = disabled (legacy behaviour).
DEFAULT_INTER_PATH_DELAY_S = 8.0
# Timeout for the responsiveness probe sent before each run. Shorter than
# the full chat timeout so we fail fast on an overloaded server.
_RESPONSIVENESS_PROBE_TIMEOUT_S = 20.0
_RESPONSIVENESS_PROBE_MSG = "ping"


class EvaluationRunner:
    """Executes conversation paths against the live agent API.

    Args:
        agent_url: Base URL of the agent service.
        telemetry: TelemetryChecker instance for assertion verification.
        neo4j_checker: Optional Neo4jChecker for post-path graph assertions.
        chat_timeout_s: Timeout for POST /chat requests.
        inter_turn_delay_s: Delay between turns to allow ES indexing.
        inter_path_delay_s: Cooldown between paths to let the inference server
            recover. Set to 0 to disable. Default 8 s.
    """

    def __init__(  # noqa: D107
        self,
        agent_url: str = DEFAULT_AGENT_URL,
        telemetry: TelemetryChecker | None = None,
        neo4j_checker: Neo4jChecker | None = None,
        chat_timeout_s: float = DEFAULT_CHAT_TIMEOUT_S,
        inter_turn_delay_s: float = DEFAULT_INTER_TURN_DELAY_S,
        inter_path_delay_s: float = DEFAULT_INTER_PATH_DELAY_S,
    ) -> None:
        self._agent_url = agent_url
        self._telemetry = telemetry or TelemetryChecker()
        self._neo4j_checker = neo4j_checker
        self._chat_timeout_s = chat_timeout_s
        self._inter_turn_delay_s = inter_turn_delay_s
        self._inter_path_delay_s = inter_path_delay_s

    async def check_agent_health(self) -> bool:
        """Verify the agent service is running and healthy.

        Returns:
            True if agent is reachable and healthy.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(f"{self._agent_url}/health")
                resp.raise_for_status()
                data: dict[str, object] = resp.json()
                return data.get("status") == "healthy"
            except (httpx.HTTPStatusError, httpx.TransportError):
                return False

    async def check_inference_responsive(self) -> bool:
        """Send a lightweight chat probe to verify the inference server can accept work.

        Unlike ``check_agent_health`` (which only hits ``/health``), this sends a
        real ``/chat`` request with a short timeout. A slow or overloaded inference
        server will fail this probe, preventing the harness from queuing 100+
        turns against a server that is already struggling.

        Returns:
            True if the agent responds within ``_RESPONSIVENESS_PROBE_TIMEOUT_S``.
        """
        async with httpx.AsyncClient(timeout=_RESPONSIVENESS_PROBE_TIMEOUT_S) as client:
            try:
                resp = await client.post(
                    f"{self._agent_url}/chat",
                    params={"message": _RESPONSIVENESS_PROBE_MSG, "session_id": "probe-session"},
                )
                resp.raise_for_status()
                log.info("inference_responsiveness_probe_ok", timeout_s=_RESPONSIVENESS_PROBE_TIMEOUT_S)
                return True
            except (httpx.ReadTimeout, httpx.ConnectTimeout):
                log.error(
                    "inference_responsiveness_probe_timeout",
                    timeout_s=_RESPONSIVENESS_PROBE_TIMEOUT_S,
                    detail="Inference server did not respond within the probe window. "
                    "The server may be overloaded. Aborting eval run.",
                )
                return False
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                log.error("inference_responsiveness_probe_failed", error=str(exc))
                return False

    async def create_session(self) -> str:
        """Create a new session for a conversation path.

        Returns:
            The session_id string.
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._agent_url}/sessions",
                json={"channel": "CHAT", "mode": "NORMAL", "metadata": {}},
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["session_id"])

    async def run_path(self, path: ConversationPath) -> PathResult:
        """Execute a complete conversation path.

        For single-session paths (``turns`` set), creates one session and runs
        all turns. For multi-session paths (``sessions`` set), creates a new
        session per ``SessionSpec``, with consolidation delays between sessions.

        Args:
            path: The conversation path to execute.

        Returns:
            PathResult with all turn results and assertion outcomes.
        """
        if path.sessions:
            return await self._run_multi_session_path(path)
        return await self._run_single_session_path(path)

    async def _run_single_session_path(self, path: ConversationPath) -> PathResult:
        """Execute a single-session conversation path."""
        session_id = await self.create_session()
        result = PathResult(
            path_id=path.path_id,
            path_name=path.name,
            category=path.category,
            session_id=session_id,
            quality_criteria=path.quality_criteria,
            started_at=datetime.now(tz=timezone.utc),
        )

        log.info(
            "path_execution_started",
            path_id=path.path_id,
            path_name=path.name,
            session_id=session_id,
            turn_count=len(path.turns),
        )

        async with httpx.AsyncClient(
            timeout=self._chat_timeout_s,
        ) as client:
            for i, turn in enumerate(path.turns):
                turn_result = await self._execute_turn(
                    client=client,
                    session_id=session_id,
                    turn_index=i,
                    user_message=turn.user_message,
                    assertions=turn.assertions,
                )
                result.turns.append(turn_result)

                if i < len(path.turns) - 1:
                    await asyncio.sleep(self._inter_turn_delay_s)

                log.info(
                    "turn_executed",
                    path_id=path.path_id,
                    turn=i + 1,
                    trace_id=turn_result.trace_id,
                    assertions_passed=sum(1 for a in turn_result.assertion_results if a.passed),
                    assertions_total=len(turn_result.assertion_results),
                    response_time_ms=turn_result.response_time_ms,
                )

        self._run_post_path_assertions(path, result)
        return result

    async def _run_multi_session_path(self, path: ConversationPath) -> PathResult:
        """Execute a multi-session conversation path (cross-session recall)."""
        first_session_id = await self.create_session()
        result = PathResult(
            path_id=path.path_id,
            path_name=path.name,
            category=path.category,
            session_id=first_session_id,
            quality_criteria=path.quality_criteria,
            started_at=datetime.now(tz=timezone.utc),
        )

        total_turns = sum(len(s.turns) for s in path.sessions)
        log.info(
            "multi_session_path_started",
            path_id=path.path_id,
            path_name=path.name,
            session_count=len(path.sessions),
            total_turns=total_turns,
        )

        global_turn_index = 0
        for sess_idx, session_spec in enumerate(path.sessions):
            session_id = first_session_id if sess_idx == 0 else await self.create_session()

            log.info(
                "session_started",
                path_id=path.path_id,
                session_index=sess_idx,
                session_id=session_id,
                turn_count=len(session_spec.turns),
            )

            async with httpx.AsyncClient(
                timeout=self._chat_timeout_s,
            ) as client:
                for i, turn in enumerate(session_spec.turns):
                    turn_result = await self._execute_turn(
                        client=client,
                        session_id=session_id,
                        turn_index=global_turn_index,
                        user_message=turn.user_message,
                        assertions=turn.assertions,
                    )
                    result.turns.append(turn_result)
                    global_turn_index += 1

                    if i < len(session_spec.turns) - 1:
                        await asyncio.sleep(self._inter_turn_delay_s)

                    log.info(
                        "turn_executed",
                        path_id=path.path_id,
                        session_index=sess_idx,
                        turn=global_turn_index,
                        trace_id=turn_result.trace_id,
                        assertions_passed=sum(
                            1 for a in turn_result.assertion_results if a.passed
                        ),
                        assertions_total=len(turn_result.assertion_results),
                        response_time_ms=turn_result.response_time_ms,
                    )

            # Wait for consolidation between sessions (not after last session)
            if sess_idx < len(path.sessions) - 1 and session_spec.post_session_delay_s > 0:
                log.info(
                    "inter_session_consolidation_wait",
                    path_id=path.path_id,
                    session_index=sess_idx,
                    delay_s=session_spec.post_session_delay_s,
                )
                await asyncio.sleep(session_spec.post_session_delay_s)

        self._run_post_path_assertions(path, result)
        return result

    async def _run_post_path_assertions(
        self, path: ConversationPath, result: PathResult
    ) -> None:
        """Run post-path Neo4j assertions and finalize result."""
        if path.post_path_assertions and self._neo4j_checker:
            if path.post_path_delay_s > 0:
                log.info(
                    "post_path_delay",
                    path_id=path.path_id,
                    delay_s=path.post_path_delay_s,
                )
                await asyncio.sleep(path.post_path_delay_s)

            post_results = await self._neo4j_checker.check_assertions(
                path.post_path_assertions,
            )
            result.post_path_assertion_results = post_results

            log.info(
                "post_path_assertions_checked",
                path_id=path.path_id,
                passed=sum(1 for r in post_results if r.passed),
                total=len(post_results),
            )
        elif path.post_path_assertions and not self._neo4j_checker:
            log.warning(
                "post_path_assertions_skipped_no_checker",
                path_id=path.path_id,
                assertion_count=len(path.post_path_assertions),
            )

        result.completed_at = datetime.now(tz=timezone.utc)
        result.all_assertions_passed = all(
            a.passed for t in result.turns for a in t.assertion_results
        ) and all(a.passed for a in result.post_path_assertion_results)

        log.info(
            "path_execution_completed",
            path_id=path.path_id,
            all_passed=result.all_assertions_passed,
            passed=result.passed_assertions,
            failed=result.failed_assertions,
            total_time_ms=result.total_time_ms,
        )

    async def run_paths(
        self,
        paths: Sequence[ConversationPath],
    ) -> list[PathResult]:
        """Execute multiple conversation paths sequentially with inter-path cooldown.

        A configurable delay (``inter_path_delay_s``) is inserted between paths to
        let the inference server flush its KV cache and recover before the next path
        starts. This prevents queue build-up on single-GPU servers.

        Args:
            paths: Conversation paths to execute.

        Returns:
            List of PathResult for each path.
        """
        results: list[PathResult] = []
        for idx, path in enumerate(paths):
            result = await self.run_path(path)
            results.append(result)
            if idx < len(paths) - 1 and self._inter_path_delay_s > 0:
                log.info(
                    "inter_path_cooldown",
                    path_id=path.path_id,
                    next_path_id=paths[idx + 1].path_id,
                    delay_s=self._inter_path_delay_s,
                )
                await asyncio.sleep(self._inter_path_delay_s)
        return results

    async def _execute_turn(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        turn_index: int,
        user_message: str,
        assertions: tuple[TelemetryAssertion, ...],
    ) -> TurnResult:
        """Execute a single conversation turn.

        Sends the message, waits for indexing, then checks assertions.

        Args:
            client: httpx client.
            session_id: Current session.
            turn_index: 0-based turn index.
            user_message: Message to send.
            assertions: Telemetry assertions to check.

        Returns:
            TurnResult with response and assertion outcomes.
        """
        start = time.monotonic()
        try:
            resp = await client.post(
                f"{self._agent_url}/chat",
                params={"message": user_message, "session_id": session_id},
            )
            resp.raise_for_status()
        except httpx.ReadTimeout:
            elapsed_ms = (time.monotonic() - start) * 1000
            log.warning(
                "turn_timeout",
                turn_index=turn_index,
                elapsed_ms=elapsed_ms,
                timeout_s=self._chat_timeout_s,
            )
            # Mark all assertions as failed due to timeout
            timeout_results = tuple(
                AssertionResult(
                    assertion=a,
                    passed=False,
                    actual_value=None,
                    message=f"Turn timed out after {elapsed_ms:.0f}ms",
                )
                for a in assertions
            )
            return TurnResult(
                turn_index=turn_index,
                user_message=user_message,
                response_text=f"[TIMEOUT after {elapsed_ms:.0f}ms]",
                trace_id="",
                assertion_results=timeout_results,
                response_time_ms=elapsed_ms,
            )

        elapsed_ms = (time.monotonic() - start) * 1000

        data = resp.json()
        response_text = data.get("response", "")
        trace_id = data.get("trace_id", "")

        # Check telemetry assertions
        assertion_results: tuple[AssertionResult, ...] = ()
        if assertions:
            events = await self._telemetry.fetch_events(trace_id)
            assertion_results = tuple(self._telemetry.check_assertions(events, assertions))

        return TurnResult(
            turn_index=turn_index,
            user_message=user_message,
            response_text=response_text,
            trace_id=trace_id,
            assertion_results=assertion_results,
            response_time_ms=elapsed_ms,
        )
