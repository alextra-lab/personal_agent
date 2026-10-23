"""Run the chat turn body the way production does: through the request-task registry (FRE-1408).

``_process_chat_stream_background`` raises unless the registry created its task (ADR-0143 D1),
so a test that awaited it directly would fail at entry. ``run_turn`` spawns it through the
registry singleton and awaits the task.
"""

from __future__ import annotations

from typing import Any

from personal_agent.service.request_tasks import RequestTask, get_request_task_registry


def spawn_turn(**kwargs: Any) -> RequestTask:
    """Spawn the chat turn body through the registry without awaiting it.

    Args:
        **kwargs: The keyword arguments of ``_process_chat_stream_background``.
            ``trace_id`` and ``session_id`` are required.

    Returns:
        The registry record; its ``task`` is the running turn.
    """
    from personal_agent.service.app import _process_chat_stream_background

    return get_request_task_registry().spawn(
        _process_chat_stream_background(**kwargs),
        trace_id=kwargs["trace_id"],
        session_id=kwargs["session_id"],
    )


async def run_turn(**kwargs: Any) -> RequestTask:
    """Spawn the chat turn body through the registry and await it.

    Args:
        **kwargs: The keyword arguments of ``_process_chat_stream_background``.

    Returns:
        The registry record of the finished turn. ``record.task.get_context()`` is the
        context the body ran in.

    Raises:
        BaseException: Whatever the task raised, as when the body is awaited directly.
    """
    record = spawn_turn(**kwargs)
    assert record.task is not None
    await record.task
    return record
