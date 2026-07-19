"""Settings for the ADR-0114 study substrate (FRE-838).

Deliberately NOT part of ``personal_agent.config.settings`` (the main
``AppConfig`` singleton). The study sandbox is a decoupled research
substrate — keeping its credential surface in a separate, ``STUDY_``-
prefixed settings class means the study environment can never carry a
prod credential by construction, satisfying ADR-0114 D1's isolation
requirement independently of any prod code path.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

STUDY_NEO4J_BOLT_PORT: int = 7691


class StudySettings(BaseSettings):
    """Connection settings for the isolated study Neo4j sandbox.

    Loads ``.env`` directly (``env_file`` below) rather than relying on
    ``personal_agent.config.get_settings()`` having already been called
    first to load it as a side effect (code-review finding, FRE-838) — a
    caller that imports this class without going through the main app's
    settings singleton must still see a ``STUDY_NEO4J_PASSWORD`` set only
    in ``.env``, not just the shell environment.
    """

    model_config = SettingsConfigDict(
        env_prefix="STUDY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    neo4j_uri: str = f"bolt://localhost:{STUDY_NEO4J_BOLT_PORT}"
    neo4j_user: str = "neo4j"
    neo4j_password: str


def study_substrate_env(study_settings: StudySettings) -> dict[str, str]:
    """Build the ``AGENT_``-prefixed env overrides that point ``AppConfig`` at the study sandbox.

    Bridges ``StudySettings``' ``STUDY_``-prefixed credentials into the env
    vars ``personal_agent.config.settings`` (the main ``AppConfig`` singleton)
    actually reads. Deliberately has NO ``personal_agent`` import and does
    NOT mutate ``os.environ`` itself — it is a pure dict-builder so it can be
    imported and unit-tested freely without side effects. The caller (a CLI
    entrypoint, e.g. ``scripts/study/run_baseline.py``) is responsible for
    applying the result to ``os.environ`` BEFORE importing anything that
    pulls in ``personal_agent`` (``settings`` is a cached import-time
    singleton — env set after the first import is a no-op, per FRE-778's
    ``ab_multipath.py``). Postgres/ES are pinned to harmless local
    test-stack values purely to satisfy ``AppConfig``'s FRE-375
    all-five-URIs guard under ``APP_ENV=test`` — no recall path this study
    drives touches either.

    Args:
        study_settings: The study sandbox's connection settings.

    Returns:
        Env var overrides to apply before importing ``personal_agent``.
    """
    return {
        "APP_ENV": "test",
        "AGENT_NEO4J_URI": study_settings.neo4j_uri,
        "AGENT_NEO4J_USER": study_settings.neo4j_user,
        "AGENT_NEO4J_PASSWORD": study_settings.neo4j_password,
        "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
        "AGENT_DATABASE_URL": (
            "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
        ),
        "AGENT_DATABASE_ADMIN_URL": (
            "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
        ),
        "AGENT_SYSGRAPH_DATABASE_URL": (
            "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
        ),
        "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
        "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
    }
