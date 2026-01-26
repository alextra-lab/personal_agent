"""Orchestrator module for task execution and state machine.

This module provides the core orchestrator that coordinates end-to-end flows
between the UI, LLM client, tools, and governance components.
"""

from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.orchestrator import Orchestrator
from personal_agent.orchestrator.session import Session, SessionManager
from personal_agent.orchestrator.types import (
    ExecutionContext,
    OrchestratorResult,
    OrchestratorStep,
    RoutingDecision,
    RoutingResult,
    TaskState,
)

__all__ = [
    # Public API
    "Orchestrator",
    "execute_task_safe",
    # Types
    "Channel",
    "TaskState",
    "ExecutionContext",
    "OrchestratorStep",
    "OrchestratorResult",
    # Routing types (Day 11.5)
    "RoutingDecision",
    "RoutingResult",
    # Session management
    "Session",
    "SessionManager",
]
