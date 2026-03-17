"""Request Gateway -- deterministic pre-LLM pipeline.

Implements the seven-stage gateway from the Cognitive Architecture
Redesign v2 spec (docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md).
"""

from personal_agent.request_gateway.pipeline import run_gateway_pipeline
from personal_agent.request_gateway.types import (
    AssembledContext,
    Complexity,
    DecompositionResult,
    DecompositionStrategy,
    GatewayOutput,
    GovernanceContext,
    IntentResult,
    TaskType,
)

__all__ = [
    "AssembledContext",
    "Complexity",
    "DecompositionResult",
    "DecompositionStrategy",
    "GatewayOutput",
    "GovernanceContext",
    "IntentResult",
    "TaskType",
    "run_gateway_pipeline",
]
