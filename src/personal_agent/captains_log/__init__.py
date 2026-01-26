"""Captain's Log module for agent self-reflection and improvement proposals.

This module provides the CaptainLogManager for creating and managing
agent-generated reflection entries and improvement proposals.
"""

from personal_agent.captains_log.background import (
    get_background_task_count,
    run_in_background,
    wait_for_background_tasks,
)
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.captains_log.reflection import generate_reflection_entry

__all__ = [
    "CaptainLogManager",
    "CaptainLogEntry",
    "CaptainLogEntryType",
    "CaptainLogStatus",
    "ProposedChange",
    "TelemetryRef",
    "generate_reflection_entry",
    "run_in_background",
    "wait_for_background_tasks",
    "get_background_task_count",
]
