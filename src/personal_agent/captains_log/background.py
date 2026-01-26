"""Background task management for Captain's Log reflections.

This module provides a simple background task manager to prevent task
garbage collection while keeping the main execution responsive.
"""

import asyncio
from typing import Set

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Global set to hold references to running background tasks
_background_tasks: Set[asyncio.Task] = set()


def run_in_background(coro) -> None:
    """Run a coroutine in the background without blocking.

    The task is tracked to prevent garbage collection but won't
    block the calling code.

    Args:
        coro: Coroutine to run in background.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # Log any errors but don't propagate them
    task.add_done_callback(_log_task_error)


def _log_task_error(task: asyncio.Task) -> None:
    """Log errors from background tasks.

    Args:
        task: Completed task to check for errors.
    """
    try:
        task.result()
    except Exception as e:
        log.warning(
            "background_task_error",
            error=str(e),
            task_name=task.get_name(),
        )


async def wait_for_background_tasks() -> None:
    """Wait for all background tasks to complete.

    This is useful for testing or graceful shutdown.
    """
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)


def get_background_task_count() -> int:
    """Get the number of running background tasks.

    Returns:
        Number of currently running background tasks.
    """
    return len(_background_tasks)
