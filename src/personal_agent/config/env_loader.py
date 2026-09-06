"""Environment variable file loader with priority-based loading.

This module implements environment-specific .env file loading with
priority order as specified in ADR-0007.
"""

from enum import Enum
from pathlib import Path

import structlog
from dotenv import load_dotenv

log = structlog.get_logger(__name__)


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
    ``_integration_run`` and ``_running_under_pytest`` below read the same way, for
    the same reason -- these three are the only acceptable direct
    environment-variable reads in this module.
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


_env_loaded_for_root: Path | None = None


def _integration_run() -> bool:
    """Whether this run opted into real config via PERSONAL_AGENT_INTEGRATION=1.

    Note: uses os.getenv() directly for the same bootstrap reason as
    get_environment() -- this is read before settings exist.
    """
    import os  # noqa: PLC0415

    return os.getenv("PERSONAL_AGENT_INTEGRATION") == "1"


def _running_under_pytest() -> bool:
    """Whether this process is a pytest run, as opposed to a standalone script.

    ``APP_ENV=test`` alone is not a safe signal for "hermetic test run": several
    scripts/eval/*/harness.py and scripts/study/config.py scripts set it too, purely
    to satisfy FRE-375's substrate-redirection guard, while still making real LLM
    calls that need ``.env``-sourced API keys. ``PYTEST_VERSION`` is set by pytest
    itself, before any of this repo's code runs, so it distinguishes "under pytest"
    from "APP_ENV=test set by a standalone script" (FRE-1318).

    Note: uses os.getenv() directly for the same bootstrap reason as
    get_environment() -- this is read before settings exist.
    """
    import os  # noqa: PLC0415

    return os.getenv("PYTEST_VERSION") is not None


def load_env_files(project_root: Path | None = None) -> None:
    """Load .env files in priority order (idempotent: only loads and logs once per process).

    Priority order (highest to lowest):
    1. `.env.{environment}.local` (highest priority, gitignored)
    2. `.env.{environment}` (environment-specific)
    3. `.env.local` (local overrides, gitignored)
    4. `.env` (base configuration)

    Skipped entirely when running under pytest with `environment ==
    Environment.TEST`, unless `PERSONAL_AGENT_INTEGRATION=1` is set (FRE-1318): CI
    never has a .env file, so a unit test run that loads the developer's real one
    diverges from CI by construction, and closing that gap must not require a
    marker on the individual test. Integration tests, which already set this flag,
    opt back in to real config. Gating on pytest as well as the environment value
    (rather than the environment alone) keeps this narrow to test runs -- standalone
    scripts that set `APP_ENV=test` only for FRE-375 substrate redirection still get
    their `.env`-sourced secrets.

    Args:
        project_root: Path to project root. If None, detects from current file location.
    """
    global _env_loaded_for_root
    if project_root is None:
        # Assume we're in src/personal_agent/config, go up to project root
        project_root = Path(__file__).parent.parent.parent.parent
    project_root = project_root.resolve()

    if _env_loaded_for_root is not None and _env_loaded_for_root == project_root:
        return

    environment = get_environment()
    env_name = environment.value

    if environment == Environment.TEST and _running_under_pytest() and not _integration_run():
        _env_loaded_for_root = project_root
        log.debug(
            "env_files_skipped_hermetic_test",
            environment=env_name,
            project_root=str(project_root),
        )
        return

    # With override=False, first-loaded value wins. Load highest priority first.
    env_files = [
        project_root / f".env.{env_name}.local",  # Environment-specific local (highest priority)
        project_root / f".env.{env_name}",  # Environment-specific
        project_root / ".env.local",  # Local overrides
        project_root / ".env",  # Base config (lowest priority)
    ]

    loaded_files = []
    for env_file in env_files:
        if env_file.exists():
            # override=False ensures explicit environment variables win over .env files
            load_dotenv(env_file, override=False)
            loaded_files.append(str(env_file.relative_to(project_root)))

    _env_loaded_for_root = project_root

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
