"""Captain's Log module for agent self-reflection and improvement proposals.

This module provides the CaptainLogManager for creating and managing
agent-generated reflection entries and improvement proposals.

ADR-0030 additions: ChangeCategory, ChangeScope, dedup, and PromotionPipeline.
"""

from personal_agent.captains_log.background import (
    get_background_task_count,
    run_in_background,
    wait_for_background_tasks,
)
from personal_agent.captains_log.dedup import compute_proposal_fingerprint
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposedChange,
    TelemetryRef,
)
from personal_agent.captains_log.promotion import PromotionCriteria, PromotionPipeline
from personal_agent.captains_log.reflection import generate_reflection_entry

__all__ = [
    "CaptainLogManager",
    "CaptainLogEntry",
    "CaptainLogEntryType",
    "CaptainLogStatus",
    "ChangeCategory",
    "ChangeScope",
    "ProposedChange",
    "TelemetryRef",
    "compute_proposal_fingerprint",
    "PromotionCriteria",
    "PromotionPipeline",
    "generate_reflection_entry",
    "run_in_background",
    "wait_for_background_tasks",
    "get_background_task_count",
]
