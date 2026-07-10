"""Tests for the study-substrate settings (FRE-838, ADR-0114 D1).

StudySettings must be fully decoupled from the main app's AGENT_-prefixed
settings surface, so the study sandbox's runtime configuration can never
carry a prod credential, even by accident.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from scripts.study.config import StudySettings, study_substrate_env


def test_defaults_point_at_the_study_bolt_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDY_NEO4J_URI", raising=False)
    monkeypatch.delenv("STUDY_NEO4J_USER", raising=False)
    monkeypatch.setenv("STUDY_NEO4J_PASSWORD", "study_dev_password")

    # _env_file=None: isolate from this developer's local .env so the
    # "defaults" assertion doesn't depend on it being free of overrides.
    settings = StudySettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.neo4j_uri == "bolt://localhost:7691"
    assert settings.neo4j_user == "neo4j"
    assert settings.neo4j_password == "study_dev_password"


def test_neo4j_password_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDY_NEO4J_PASSWORD", raising=False)

    # _env_file=None: isolate from whatever this developer's local .env
    # happens to contain, so the test proves the field is genuinely
    # required rather than depending on the repo's real .env being empty.
    with pytest.raises(ValidationError):
        StudySettings(_env_file=None)  # type: ignore[call-arg]


def test_ignores_agent_prefixed_prod_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process env carrying prod AGENT_* vars must not leak into StudySettings.

    This is the mechanism behind AC-5(1) ("prod credentials are absent from
    the study environment") — StudySettings only ever reads STUDY_-prefixed
    vars, so setting AGENT_NEO4J_URI/AGENT_NEO4J_PASSWORD (as a real prod
    process would) has zero effect on what the study sandbox connects to.
    """
    monkeypatch.setenv("AGENT_NEO4J_URI", "bolt://localhost:7687")  # fre-375-allow: ignored
    monkeypatch.setenv("AGENT_NEO4J_USER", "neo4j")
    monkeypatch.setenv("AGENT_NEO4J_PASSWORD", "prod_password_should_never_be_read")
    monkeypatch.setenv("STUDY_NEO4J_PASSWORD", "study_dev_password")
    monkeypatch.delenv("STUDY_NEO4J_URI", raising=False)
    monkeypatch.delenv("STUDY_NEO4J_USER", raising=False)

    settings = StudySettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.neo4j_uri == "bolt://localhost:7691"
    assert settings.neo4j_password == "study_dev_password"
    assert settings.neo4j_password != "prod_password_should_never_be_read"


# ---------------------------------------------------------------------------
# study_substrate_env (FRE-840) — bridges STUDY_ creds into the AGENT_ env
# vars personal_agent.config.settings reads. Pure dict-builder: no
# os.environ mutation, no personal_agent import.
# ---------------------------------------------------------------------------


def test_study_substrate_env_bridges_study_creds_to_agent_vars() -> None:
    settings = StudySettings(_env_file=None, neo4j_password="study_dev_password")  # type: ignore[call-arg]

    env = study_substrate_env(settings)

    assert env["AGENT_NEO4J_URI"] == "bolt://localhost:7691"
    assert env["AGENT_NEO4J_USER"] == "neo4j"
    assert env["AGENT_NEO4J_PASSWORD"] == "study_dev_password"
    assert env["APP_ENV"] == "test"


def test_study_substrate_env_never_carries_a_prod_looking_uri() -> None:
    settings = StudySettings(_env_file=None, neo4j_password="study_dev_password")  # type: ignore[call-arg]

    env = study_substrate_env(settings)

    assert "7687" not in env["AGENT_NEO4J_URI"]  # prod's bolt port
    assert env["AGENT_NEO4J_URI"] == settings.neo4j_uri
