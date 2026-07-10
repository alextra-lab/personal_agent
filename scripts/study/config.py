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
