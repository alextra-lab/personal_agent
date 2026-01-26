"""Local LLM Client module.

This module provides the LocalLLMClient for interacting with local LLM servers
(LM Studio, Ollama, etc.) with proper error handling, retries, and telemetry.
"""

import warnings
from typing import TYPE_CHECKING

from personal_agent.llm_client.claude import ClaudeClient
from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.llm_client.tool_call_parser import parse_text_tool_calls
from personal_agent.llm_client.types import (
    LLMClientError,
    LLMConnectionError,
    LLMInvalidResponse,
    LLMRateLimit,
    LLMResponse,
    LLMServerError,
    LLMTimeout,
    ModelRole,
    ToolCall,
)

if TYPE_CHECKING:
    # Type checking only - avoid circular import
    from personal_agent.config import ModelConfigError, load_model_config
    from personal_agent.llm_client.client import LocalLLMClient
else:
    # Lazy import to avoid circular dependency at runtime
    # Re-export from config module with deprecation warning
    # TODO: Remove in v0.2.0 - use `from personal_agent.config import load_model_config` instead
    def __getattr__(name: str):
        if name == "LocalLLMClient":
            from personal_agent.llm_client.client import LocalLLMClient

            return LocalLLMClient
        if name in ("ModelConfigError", "load_model_config"):
            import personal_agent.config

            warnings.warn(
                "Importing load_model_config from personal_agent.llm_client is deprecated. "
                "Use 'from personal_agent.config import load_model_config' instead. "
                "This will be removed in v0.2.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            return getattr(personal_agent.config, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LocalLLMClient",
    "load_model_config",
    "ModelConfigError",
    "LLMClientError",
    "LLMConnectionError",
    "LLMInvalidResponse",
    "LLMResponse",
    "LLMRateLimit",
    "LLMServerError",
    "LLMTimeout",
    "ModelRole",
    "ToolCall",
    "parse_text_tool_calls",
    "ClaudeClient",
    "CostTrackerService",
]
