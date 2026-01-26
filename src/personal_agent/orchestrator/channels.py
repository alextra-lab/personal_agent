"""Channel definitions for orchestrator.

Channels represent different interaction modes and influence:
- Default model role selection
- Which tools are considered
- Default execution paths
"""

from enum import Enum


class Channel(str, Enum):
    """Communication channels for user interactions.

    Channels influence orchestrator behavior:
    - CHAT: General conversation, Q&A, research
    - CODE_TASK: Coding questions and tasks
    - SYSTEM_HEALTH: System health checks and diagnostics
    """

    CHAT = "CHAT"
    CODE_TASK = "CODE_TASK"
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
