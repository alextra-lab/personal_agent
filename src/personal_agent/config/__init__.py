"""Unified configuration management for the Personal Agent.

This module provides a single source of truth for all configuration,
integrating environment variables, YAML files, and defaults.

All configuration loaders live in this module per ADR-0007.
"""

from personal_agent.config.env_loader import Environment, get_environment
from personal_agent.config.governance_loader import (
    GovernanceConfigError,
    load_governance_config,
)
from personal_agent.config.model_loader import ModelConfigError, load_model_config
from personal_agent.config.settings import AppConfig, get_settings, load_app_config

# Singleton instance
settings = get_settings()

__all__ = [
    # App-level settings
    "settings",
    "AppConfig",
    "get_settings",
    "load_app_config",
    "Environment",
    "get_environment",
    # Configuration loaders
    "load_governance_config",
    "load_model_config",
    # Exception classes
    "GovernanceConfigError",
    "ModelConfigError",
]
