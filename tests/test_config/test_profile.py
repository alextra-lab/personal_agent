"""Tests for execution profile configuration (ADR-0044, FRE-207)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from personal_agent.config.profile import (
    DelegationConfig,
    ExecutionProfile,
    list_profiles,
    load_profile,
    resolve_model_key,
    set_current_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def profiles_dir(tmp_path: Path) -> Path:
    """Return a temporary profiles directory pre-populated with local and cloud YAMLs."""
    local_data = {
        "name": "local",
        "description": "Local inference via SLM Server (Qwen3.5-35B)",
        "primary_model": "qwen3.5-35b-a3b",
        "sub_agent_model": "qwen3-8b",
        "provider_type": "local",
        "cost_limit_per_session": None,
        "delegation": {
            "allow_cloud_escalation": False,
            "escalation_provider": None,
            "escalation_model": None,
        },
    }
    cloud_data = {
        "name": "cloud",
        "description": "Cloud inference via LiteLLM (Claude Sonnet)",
        "primary_model": "claude-sonnet-4-20250514",
        "sub_agent_model": "claude-haiku-4-5-20251001",
        "provider_type": "cloud",
        "cost_limit_per_session": 2.00,
        "delegation": {
            "allow_cloud_escalation": True,
            "escalation_provider": "anthropic",
            "escalation_model": "claude-sonnet-4-20250514",
        },
    }
    (tmp_path / "local.yaml").write_text(yaml.dump(local_data), encoding="utf-8")
    (tmp_path / "cloud.yaml").write_text(yaml.dump(cloud_data), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# ExecutionProfile model tests
# ---------------------------------------------------------------------------


class TestExecutionProfile:
    """Tests for ExecutionProfile Pydantic model."""

    def test_local_profile_is_frozen(self) -> None:
        """ExecutionProfile must be immutable (frozen=True).

        Pydantic frozen models raise ValidationError on mutation.
        """
        profile = ExecutionProfile(
            name="local",
            primary_model="qwen3.5-35b-a3b",
            sub_agent_model="qwen3-8b",
            provider_type="local",
        )
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            profile.name = "mutated"  # type: ignore[misc]

    def test_cloud_profile_is_frozen(self) -> None:
        """Cloud ExecutionProfile must also be immutable.

        Pydantic frozen models raise ValidationError on mutation.
        """
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude-sonnet-4-20250514",
            sub_agent_model="claude-haiku-4-5-20251001",
            provider_type="cloud",
            cost_limit_per_session=2.00,
        )
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            profile.cost_limit_per_session = 99.0  # type: ignore[misc]

    def test_cost_limit_can_be_none(self) -> None:
        """cost_limit_per_session accepts None (local profile, no API cost)."""
        profile = ExecutionProfile(
            name="local",
            primary_model="qwen3.5-35b-a3b",
            sub_agent_model="qwen3-8b",
            provider_type="local",
            cost_limit_per_session=None,
        )
        assert profile.cost_limit_per_session is None

    def test_cost_limit_can_be_float(self) -> None:
        """cost_limit_per_session accepts a positive float."""
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude-sonnet-4-20250514",
            sub_agent_model="claude-haiku-4-5-20251001",
            provider_type="cloud",
            cost_limit_per_session=2.00,
        )
        assert profile.cost_limit_per_session == pytest.approx(2.00)

    def test_provider_type_rejects_invalid(self) -> None:
        """provider_type must be 'local' or 'cloud' — anything else raises."""
        with pytest.raises(Exception):
            ExecutionProfile(
                name="bad",
                primary_model="x",
                sub_agent_model="y",
                provider_type="gpu",  # type: ignore[arg-type]
            )

    def test_description_defaults_to_empty_string(self) -> None:
        """Description is optional and defaults to empty string."""
        profile = ExecutionProfile(
            name="local",
            primary_model="model-a",
            sub_agent_model="model-b",
            provider_type="local",
        )
        assert profile.description == ""


# ---------------------------------------------------------------------------
# DelegationConfig tests
# ---------------------------------------------------------------------------


class TestDelegationConfig:
    """Tests for DelegationConfig defaults and immutability."""

    def test_defaults(self) -> None:
        """Default DelegationConfig disallows escalation."""
        cfg = DelegationConfig()
        assert cfg.allow_cloud_escalation is False
        assert cfg.escalation_provider is None
        assert cfg.escalation_model is None

    def test_is_frozen(self) -> None:
        """DelegationConfig must be immutable.

        Pydantic frozen models raise ValidationError on mutation.
        """
        cfg = DelegationConfig()
        with pytest.raises((TypeError, AttributeError, ValidationError)):
            cfg.allow_cloud_escalation = True  # type: ignore[misc]

    def test_cloud_escalation_config(self) -> None:
        """DelegationConfig correctly stores escalation provider and model."""
        cfg = DelegationConfig(
            allow_cloud_escalation=True,
            escalation_provider="anthropic",
            escalation_model="claude-sonnet-4-20250514",
        )
        assert cfg.allow_cloud_escalation is True
        assert cfg.escalation_provider == "anthropic"
        assert cfg.escalation_model == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# load_profile tests
# ---------------------------------------------------------------------------


class TestLoadProfile:
    """Tests for load_profile()."""

    def test_load_local_profile(self, profiles_dir: Path) -> None:
        """load_profile('local') returns a valid local ExecutionProfile."""
        profile = load_profile("local", profiles_dir=profiles_dir)

        assert isinstance(profile, ExecutionProfile)
        assert profile.name == "local"
        assert profile.provider_type == "local"
        assert profile.primary_model == "qwen3.5-35b-a3b"
        assert profile.sub_agent_model == "qwen3-8b"
        assert profile.cost_limit_per_session is None
        assert profile.delegation.allow_cloud_escalation is False

    def test_load_cloud_profile(self, profiles_dir: Path) -> None:
        """load_profile('cloud') returns a valid cloud ExecutionProfile."""
        profile = load_profile("cloud", profiles_dir=profiles_dir)

        assert isinstance(profile, ExecutionProfile)
        assert profile.name == "cloud"
        assert profile.provider_type == "cloud"
        assert profile.primary_model == "claude-sonnet-4-20250514"
        assert profile.sub_agent_model == "claude-haiku-4-5-20251001"
        assert profile.cost_limit_per_session == pytest.approx(2.00)
        assert profile.delegation.allow_cloud_escalation is True
        assert profile.delegation.escalation_provider == "anthropic"

    def test_missing_profile_raises_file_not_found(self, tmp_path: Path) -> None:
        """load_profile raises FileNotFoundError for a non-existent profile."""
        with pytest.raises(FileNotFoundError, match="nonexistent"):
            load_profile("nonexistent", profiles_dir=tmp_path)

    def test_load_profile_from_real_config_dir(self) -> None:
        """load_profile works against the real config/profiles directory."""
        real_profiles = Path("config/profiles")
        if not real_profiles.exists():
            pytest.skip("config/profiles directory not present in working directory")

        local_profile = load_profile("local", profiles_dir=real_profiles)
        assert local_profile.provider_type == "local"

        cloud_profile = load_profile("cloud", profiles_dir=real_profiles)
        assert cloud_profile.provider_type == "cloud"

    def test_load_profile_accepts_path_object(self, profiles_dir: Path) -> None:
        """profiles_dir argument accepts a Path object as well as a string."""
        profile = load_profile("local", profiles_dir=profiles_dir)
        assert profile.name == "local"


# ---------------------------------------------------------------------------
# list_profiles tests
# ---------------------------------------------------------------------------


class TestListProfiles:
    """Tests for list_profiles()."""

    def test_finds_local_and_cloud(self, profiles_dir: Path) -> None:
        """list_profiles returns both local and cloud profile names."""
        names = list_profiles(profiles_dir=profiles_dir)
        assert "local" in names
        assert "cloud" in names

    def test_returns_sorted_list(self, profiles_dir: Path) -> None:
        """list_profiles returns names in alphabetical order."""
        names = list_profiles(profiles_dir=profiles_dir)
        assert names == sorted(names)

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        """list_profiles returns [] when the directory contains no YAML files."""
        assert list_profiles(profiles_dir=tmp_path) == []

    def test_nonexistent_directory_returns_empty_list(self, tmp_path: Path) -> None:
        """list_profiles returns [] when the directory does not exist."""
        nonexistent = tmp_path / "does_not_exist"
        assert list_profiles(profiles_dir=nonexistent) == []

    def test_ignores_non_yaml_files(self, tmp_path: Path) -> None:
        """list_profiles only returns .yaml files, ignoring others."""
        (tmp_path / "notes.txt").write_text("not a profile")
        (tmp_path / "local.yaml").write_text(
            yaml.dump(
                {
                    "name": "local",
                    "primary_model": "model-a",
                    "sub_agent_model": "model-b",
                    "provider_type": "local",
                }
            )
        )
        names = list_profiles(profiles_dir=tmp_path)
        assert names == ["local"]
        assert "notes" not in names


# ---------------------------------------------------------------------------
# resolve_model_key tests (ADR-0063 §D6)
# ---------------------------------------------------------------------------


class TestResolveModelKey:
    """Tests for resolve_model_key() — profile-aware model config key resolution."""

    def test_returns_role_name_without_active_profile(self) -> None:
        """resolve_model_key returns the role name unchanged when no profile is set."""
        assert resolve_model_key("primary") == "primary"
        assert resolve_model_key("sub_agent") == "sub_agent"
        assert resolve_model_key("compressor") == "compressor"

    def test_redirects_primary_via_active_profile(self) -> None:
        """resolve_model_key returns profile.primary_model when a profile is active."""
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            provider_type="cloud",
        )
        token = set_current_profile(profile)
        try:
            assert resolve_model_key("primary") == "claude_sonnet"
        finally:
            from personal_agent.config.profile import _current_profile
            _current_profile.reset(token)

    def test_redirects_sub_agent_via_active_profile(self) -> None:
        """resolve_model_key returns profile.sub_agent_model for sub_agent role."""
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            provider_type="cloud",
        )
        token = set_current_profile(profile)
        try:
            assert resolve_model_key("sub_agent") == "claude_haiku"
        finally:
            from personal_agent.config.profile import _current_profile
            _current_profile.reset(token)

    def test_does_not_redirect_compressor_role(self) -> None:
        """resolve_model_key never redirects compressor — only primary and sub_agent."""
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            provider_type="cloud",
        )
        token = set_current_profile(profile)
        try:
            assert resolve_model_key("compressor") == "compressor"
        finally:
            from personal_agent.config.profile import _current_profile
            _current_profile.reset(token)

    def test_unknown_role_passes_through_unchanged(self) -> None:
        """resolve_model_key returns unrecognised role strings unchanged."""
        profile = ExecutionProfile(
            name="cloud",
            primary_model="claude_sonnet",
            sub_agent_model="claude_haiku",
            provider_type="cloud",
        )
        token = set_current_profile(profile)
        try:
            assert resolve_model_key("unknown_role") == "unknown_role"
        finally:
            from personal_agent.config.profile import _current_profile
            _current_profile.reset(token)
