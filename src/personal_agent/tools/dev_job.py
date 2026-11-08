"""The tool that starts a dev-agent job (ADR-0159 D10.1–D10.5; FRE-1687).

``start_dev_job`` hands a build or a fix of an interactive page to the development agent. Every
call asks: ``config/governance/permissions.yaml`` holds an explicit ``ask`` rule for it in every
mode, so the card offers "allow once" only (D10.2).

The executor creates the ``dev_jobs`` row and returns at once. The job supervisor takes the job
from the row, so the turn does not wait for the job (D10.5). The row is created only when
``tool_approval_decisions`` holds the ``approve`` decision of this call's card, under the unique
approval request id (D10.3). A denied card, a card that timed out and a reused approval create no
row, and so no container.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

from personal_agent.config import settings
from personal_agent.dev_agent.jobs import ApprovalEvidence, JobCreated, JobStore, RefusalReason
from personal_agent.dev_broker.config import load_broker_config
from personal_agent.llm_client.cost_tracker import _normalize_asyncpg_dsn
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.executor import ApprovalGrant
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

TOOL_NAME = "start_dev_job"

#: The longest task text that a job takes.
MAX_TASK_CHARS = 8000

DevJobCode = Literal[
    "approval_required",
    "approval_mismatch",
    "not_available",
    "invalid_arguments",
    "job_active",
    "month_cap_reached",
    "approval_missing",
    "approval_used",
]


class DevJobError(ToolError[DevJobCode]):
    """A ``start_dev_job`` failure (ADR-0156 D6.1)."""


start_dev_job_tool = ToolDefinition(
    name=TOOL_NAME,
    description=(
        "Start a development agent job that builds or fixes one interactive HTML page in its own "
        "git repo, tests it in a browser, and publishes it as an artifact. The user approves "
        "every job on a card. The call returns at once with the job id; the job runs for "
        "minutes, and its result arrives on the job card. mode 'build' makes a new page from "
        "the task. mode 'fix' changes an existing artifact and needs its artifact_id. Only one "
        "job runs at a time."
    ),
    category="dev_agent",
    parameters=[
        ToolParameter(
            name="task",
            type="string",
            description=(
                "What the page must do or what to fix, in full: the agent sees only this text "
                f"and, for a fix, the page. At most {MAX_TASK_CHARS} characters."
            ),
            required=True,
        ),
        ToolParameter(
            name="mode",
            type="string",
            description="'build' for a new page, or 'fix' for an existing artifact.",
            required=True,
            json_schema={"type": "string", "enum": ["build", "fix"]},
        ),
        ToolParameter(
            name="artifact_id",
            type="string",
            description="The artifact to fix. Required for mode 'fix'. Omit it for 'build'.",
            required=False,
        ),
    ],
    risk_level="high",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    timeout_seconds=30,
    error_codes=error_codes(DevJobError),
)

#: What the model gets for each refusal of the job store: (message, next action).
_REFUSALS: dict[RefusalReason, tuple[str, str]] = {
    "job_active": (
        "A development job is running already. Only one job runs at a time.",
        "Tell the user that a job is running, and that they can ask again when it ends.",
    ),
    "month_cap_reached": (
        "The development agent reached its spend cap for this calendar month (UTC).",
        "Do not retry this month. Tell the user that the monthly cap is reached.",
    ),
    "approval_missing": (
        "No stored approval matches this call, so no job was created.",
        "Do not retry the same call. Tell the user that the approval was not recorded.",
    ),
    "approval_used": (
        "This approval created a job already. One approval starts one job.",
        "Do not retry. Call start_dev_job again only if the user asks for another job.",
    ),
}


def _refuse(code: DevJobCode, message: str, next_action: str) -> DevJobError:
    return DevJobError(code, f"{TOOL_NAME}: {message}", retryable=False, next_action=next_action)


def _artifact_uuid(mode: str, artifact_id: str | None) -> UUID | None:
    """Validate the mode and the artifact id.

    Args:
        mode: ``build`` or ``fix``.
        artifact_id: The artifact to fix, or ``None``.

    Returns:
        The artifact id for ``fix``, ``None`` for ``build``.

    Raises:
        DevJobError: ``invalid_arguments`` for any other combination.
    """
    if mode == "build":
        if artifact_id:
            raise _refuse(
                "invalid_arguments",
                "mode 'build' takes no artifact_id.",
                "Call again with mode 'fix' to change that artifact, or without artifact_id.",
            )
        return None
    if mode != "fix":
        raise _refuse("invalid_arguments", "mode must be 'build' or 'fix'.", "Call again.")
    if not artifact_id:
        raise _refuse(
            "invalid_arguments",
            "mode 'fix' needs the artifact_id of the page to fix.",
            "Find the id with artifact_list, then call again.",
        )
    try:
        return UUID(artifact_id)
    except ValueError:
        raise _refuse(
            "invalid_arguments",
            f"artifact_id {artifact_id[:80]!r} is not an artifact id.",
            "Find the id with artifact_list, then call again.",
        ) from None


def _grant_matches(grant: ApprovalGrant, task: str, mode: str, artifact_id: str | None) -> bool:
    """Return whether the card showed exactly the arguments that this call runs with."""
    shown = grant.args
    return (
        shown.get("task") == task
        and shown.get("mode") == mode
        and (shown.get("artifact_id") or None) == (artifact_id or None)
    )


async def start_dev_job_executor(
    task: str,
    mode: str,
    artifact_id: str | None = None,
    ctx: TraceContext | None = None,
    approval: ApprovalGrant | None = None,
) -> dict[str, str]:
    """Create a ``queued`` dev-agent job for an approved card, and return at once.

    Args:
        task: The task for the agent.
        mode: ``build`` or ``fix``.
        artifact_id: The artifact to fix, for ``fix``.
        ctx: The turn's trace context, with the user and the session.
        approval: The approved card, set by the execution layer. ``None`` when no card was
            approved.

    Returns:
        The job id, its state and a "build started" message.

    Raises:
        DevJobError: When the call has no approved card, the agent is not available, the
            arguments are invalid, a job is active, the month cap is reached, or the approval is
            missing or used.
    """
    trace_id = ctx.trace_id if ctx is not None else None
    if approval is None:
        raise _refuse(
            "approval_required",
            "a job starts only after the user approves its card, and this call had no approval.",
            "Do not retry. Tell the user that the job needs their approval on the card.",
        )
    if not _grant_matches(approval, task, mode, artifact_id):
        log.warning(
            "dev_job_approval_mismatch",
            request_id=approval.request_id,
            trace_id=trace_id,
        )
        raise _refuse(
            "approval_mismatch",
            "the approved card showed other arguments than this call.",
            "Do not retry. Tell the user that the approval did not match the request.",
        )
    if not settings.dev_agent_supervisor_enabled or not _supervisor_running():
        raise _refuse(
            "not_available",
            "the development agent is not running on this deployment.",
            "Tell the user that the development agent is not available.",
        )
    user_id = ctx.user_id if ctx is not None else None
    session_id = ctx.session_id if ctx is not None else None
    if user_id is None or not session_id:
        raise _refuse(
            "not_available",
            "this call has no user or no session, and a job belongs to both.",
            "Tell the user that a job can start only from a signed-in chat session.",
        )
    task = task.strip()
    if not task or len(task) > MAX_TASK_CHARS:
        raise _refuse(
            "invalid_arguments",
            f"task must hold 1 to {MAX_TASK_CHARS} characters.",
            "Call again with the task in full, inside the limit.",
        )
    source = _artifact_uuid(mode, artifact_id)

    broker = load_broker_config()
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=1, command_timeout=15
    )
    try:
        result = await JobStore(pool).create_approved_job(
            job_id=uuid4(),
            user_id=user_id if isinstance(user_id, UUID) else UUID(str(user_id)),
            session_id=UUID(str(session_id)),
            trace_id=uuid4(),
            approval=ApprovalEvidence(
                request_id=approval.request_id,
                tool_call_id=approval.tool_call_id,
                tool=TOOL_NAME,
                args_hash=approval.args_hash,
            ),
            mode="fix" if source is not None else "build",
            task_text=task,
            model_id=broker.default_model,
            month_cap_usd=broker.month_cap_usd,
            source_artifact_id=source,
        )
    finally:
        await pool.close()

    if not isinstance(result, JobCreated):
        log.warning(
            "dev_job_refused",
            reason=result.reason,
            request_id=approval.request_id,
            trace_id=trace_id,
            session_id=str(session_id),
        )
        message, next_action = _REFUSALS[result.reason]
        raise _refuse(result.reason, message, next_action)
    job = result.job
    return {
        "job_id": str(job.id),
        "state": job.state.value,
        "message": (
            "Build started. The job runs for some minutes, and its card shows the progress and "
            "the result. Do not wait for it in this turn."
        ),
    }


def _supervisor_running() -> bool:
    # Imported here: the gateway wiring imports the launcher and the GitHub client, which a
    # process that registers this tool does not need at import time.
    from personal_agent.dev_agent.gateway import supervisor_running  # noqa: PLC0415

    return supervisor_running()
