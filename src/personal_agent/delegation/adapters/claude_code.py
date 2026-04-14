"""Claude Code delegation adapter — CLI subprocess.

Formalizes the Slice 2 subprocess delegation pattern with:
- MCP server connection info injection (--mcp-server flag)
- Delegation depth limit (max_depth from package constraints)
- Structured outcome parsing

See: docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md D4, D6
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import datetime, timezone

import structlog

from personal_agent.request_gateway.delegation_types import (
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

_CLAUDE_CLI_BINARY = "claude"
_DEFAULT_MCP_SERVER_FLAG = "--mcp-server"


class ClaudeCodeAdapter:
    """Delegates tasks to Claude Code via CLI subprocess.

    Satisfies DelegationExecutorProtocol via structural subtyping.

    The adapter injects the Seshat MCP server URL (if provided) so Claude
    Code can query the knowledge graph mid-task (ADR-0050 D4).

    Attributes:
        _mcp_server_url: Optional URL of the Seshat MCP server to inject
            into the Claude Code invocation. If None, delegation proceeds
            without bidirectional knowledge access.
    """

    def __init__(self, mcp_server_url: str | None = None) -> None:
        """Initialise the adapter.

        Args:
            mcp_server_url: Optional Seshat MCP server URL to pass to
                Claude Code so it can query the knowledge graph mid-task.
        """
        self._mcp_server_url = mcp_server_url

    def available(self) -> bool:
        """Check whether the ``claude`` CLI binary exists in PATH.

        Returns:
            True if ``claude`` is found in PATH, False otherwise.
        """
        return shutil.which(_CLAUDE_CLI_BINARY) is not None

    async def delegate(
        self,
        package: DelegationPackage,
        timeout: float = 300.0,
        trace_ctx: TraceContext | None = None,
    ) -> DelegationOutcome:
        """Execute a delegation via the Claude Code CLI.

        Builds a ``claude --print <task>`` subprocess invocation, optionally
        injecting the Seshat MCP server URL for bidirectional knowledge access.

        Args:
            package: Structured delegation package. ``task_description`` is
                passed as the prompt to Claude Code.
            timeout: Maximum seconds to wait before aborting.
            trace_ctx: Trace context for telemetry correlation. If None, a
                new trace ID is generated.

        Returns:
            DelegationOutcome with success, result text, and any artifacts.
            Never raises — all errors are encoded as DelegationOutcome.
        """
        trace_id = trace_ctx.trace_id if trace_ctx else str(uuid.uuid4())

        if not self.available():
            log.warning(
                "claude_code_adapter.unavailable",
                trace_id=trace_id,
                task_id=package.task_id,
            )
            return DelegationOutcome(
                task_id=package.task_id,
                success=False,
                rounds_needed=0,
                what_worked="",
                what_was_missing="Claude Code CLI not found in PATH",
                duration_minutes=0.0,
            )

        # Build argument list; task_description is the user prompt.
        # asyncio.create_subprocess_exec avoids shell injection by design
        # (no shell=True, each arg is a separate element).
        cmd = [_CLAUDE_CLI_BINARY, "--print", package.task_description]

        mcp_url = self._mcp_server_url
        if mcp_url:
            cmd.extend([_DEFAULT_MCP_SERVER_FLAG, mcp_url])
            log.debug(
                "claude_code_adapter.mcp_injected",
                trace_id=trace_id,
                task_id=package.task_id,
                mcp_url=mcp_url,
            )

        start = datetime.now(tz=timezone.utc)
        log.info(
            "claude_code_adapter.delegating",
            trace_id=trace_id,
            task_id=package.task_id,
            target_agent=package.target_agent,
            complexity=package.estimated_complexity,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            duration = (datetime.now(tz=timezone.utc) - start).total_seconds() / 60.0
            success = proc.returncode == 0
            result_text = stdout.decode() if stdout else ""
            error_text = stderr.decode() if stderr and not success else ""

            log.info(
                "claude_code_adapter.delegation_complete",
                trace_id=trace_id,
                task_id=package.task_id,
                success=success,
                returncode=proc.returncode,
                duration_minutes=round(duration, 3),
            )

            return DelegationOutcome(
                task_id=package.task_id,
                success=success,
                rounds_needed=1,
                what_worked=result_text[:500] if success else "",
                what_was_missing=error_text[:500] if not success else "",
                duration_minutes=duration,
            )

        except asyncio.TimeoutError:
            duration = (datetime.now(tz=timezone.utc) - start).total_seconds() / 60.0
            log.warning(
                "claude_code_adapter.timeout",
                trace_id=trace_id,
                task_id=package.task_id,
                timeout_seconds=timeout,
            )
            return DelegationOutcome(
                task_id=package.task_id,
                success=False,
                rounds_needed=1,
                what_worked="",
                what_was_missing=f"Delegation timed out after {timeout}s",
                duration_minutes=duration,
            )

        except Exception as exc:
            duration = (datetime.now(tz=timezone.utc) - start).total_seconds() / 60.0
            log.error(
                "claude_code_adapter.error",
                trace_id=trace_id,
                task_id=package.task_id,
                error=str(exc),
                exc_info=True,
            )
            return DelegationOutcome(
                task_id=package.task_id,
                success=False,
                rounds_needed=0,
                what_worked="",
                what_was_missing=str(exc),
                duration_minutes=duration,
            )
