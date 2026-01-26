"""Environment variable file loader with priority-based loading.

This module implements environment-specific .env file loading with
priority order as specified in ADR-0007.
"""

from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class Environment(str, Enum):
    """Application environment types."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


def get_environment() -> Environment:
    """Detect current environment from APP_ENV environment variable.

    Returns:
        Environment enum value.

    Environment variable mapping:
    - "production" or "prod" → Environment.PRODUCTION
    - "staging" or "stage" → Environment.STAGING
    - "test" → Environment.TEST
    - Default → Environment.DEVELOPMENT

    Note: This function uses os.getenv() directly because environment
    detection must happen before settings are loaded (chicken-and-egg problem).
    This is the only acceptable use of direct environment variable access.
    """
    import os  # noqa: PLC0415

    app_env = os.getenv("APP_ENV", "").lower()

    if app_env in ("production", "prod"):
        return Environment.PRODUCTION
    elif app_env in ("staging", "stage"):
        return Environment.STAGING
    elif app_env == "test":
        return Environment.TEST
    else:
        return Environment.DEVELOPMENT


def load_env_files(project_root: Path | None = None) -> None:
    """Load .env files in priority order.

    Priority order (highest to lowest):
    1. `.env.{environment}.local` (highest priority, gitignored)
    2. `.env.{environment}` (environment-specific)
    3. `.env.local` (local overrides, gitignored)
    4. `.env` (base configuration)

    Args:
        project_root: Path to project root. If None, detects from current file location.
    """
    if project_root is None:
        # Assume we're in src/personal_agent/config, go up to project root
        project_root = Path(__file__).parent.parent.parent.parent

    environment = get_environment()
    env_name = environment.value

    # Priority order: later files override earlier ones
    env_files = [
        project_root / ".env",  # Base config (lowest priority)
        project_root / ".env.local",  # Local overrides
        project_root / f".env.{env_name}",  # Environment-specific
        project_root / f".env.{env_name}.local",  # Environment-specific local (highest priority)
    ]

    loaded_files = []
    for env_file in env_files:
        if env_file.exists():
            # override=False ensures explicit environment variables win over .env files
            load_dotenv(env_file, override=False)
            loaded_files.append(str(env_file.relative_to(project_root)))

    if loaded_files:
        log.info(
            "env_files_loaded",
            environment=env_name,
            files=loaded_files,
            project_root=str(project_root),
        )
    else:
        log.debug(
            "no_env_files_found",
            environment=env_name,
            project_root=str(project_root),
        )
