"""Execution profile configuration (ADR-0044).

A profile defines a complete execution harness: models, cost constraints, and
delegation rules. Conversations are bound to a profile at creation time.

Profiles reference models by name; the model registry remains in config/models.yaml
(ADR-0031). Profiles are the execution configuration layer above model definitions.
"""

from __future__ import annotations

import contextvars
import re
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Current-profile context variable
# ---------------------------------------------------------------------------

#: Async-safe context variable that carries the active ExecutionProfile through
#: an orchestrator call chain without threading the profile through every
#: function signature.  Set once in the service endpoint; read by the LLM
#: client factory to select the correct model.
_current_profile: contextvars.ContextVar[ExecutionProfile | None] = contextvars.ContextVar(
    "current_profile", default=None
)


def set_current_profile(profile: ExecutionProfile) -> contextvars.Token[ExecutionProfile | None]:
    """Set the active profile for the current async context.

    Args:
        profile: The ExecutionProfile to activate.

    Returns:
        A token that can be passed to :func:`reset_current_profile` to restore
        the previous value (useful in tests).
    """
    return _current_profile.set(profile)


def get_current_profile() -> ExecutionProfile | None:
    """Return the active ExecutionProfile, or ``None`` if none is set."""
    return _current_profile.get()


def resolve_model_key(role_name: str) -> str:
    """Resolve a model role name to its config key, accounting for the active ExecutionProfile.

    Without an active profile, returns ``role_name`` unchanged (local/default execution).
    With a profile, ``primary`` and ``sub_agent`` roles are redirected to the profile's
    model identifiers (e.g. ``"claude_sonnet"``). ``compressor`` and unrecognised roles
    are never redirected.

    This mirrors the resolution logic in :func:`~personal_agent.llm_client.factory.get_llm_client`
    and must stay in sync with it. Having a single canonical helper prevents the two sites
    from drifting (ADR-0063 §D6).

    Args:
        role_name: The model role string (e.g. ``"primary"``, ``"sub_agent"``).

    Returns:
        The resolved config key to use for ``model_configs.get()``.
    """
    profile = get_current_profile()
    if profile is None:
        return role_name
    if role_name == "primary" and profile.primary_model:
        return profile.primary_model
    if role_name == "sub_agent" and profile.sub_agent_model:
        return profile.sub_agent_model
    return role_name


class DelegationConfig(BaseModel):
    """Delegation rules for cross-provider escalation.

    Controls whether a conversation may escalate to cloud models mid-task,
    and which provider/model to use for that escalation.

    Attributes:
        allow_cloud_escalation: Whether cloud escalation is permitted for this profile.
        escalation_provider: Cloud provider name (e.g. "anthropic") for escalation.
            Only relevant when allow_cloud_escalation is True.
        escalation_model: Model identifier to use for escalated sub-tasks.
            Only relevant when allow_cloud_escalation is True.
    """

    model_config = ConfigDict(frozen=True)

    allow_cloud_escalation: bool = False
    escalation_provider: str | None = None
    escalation_model: str | None = None


class ExecutionProfile(BaseModel):
    """Complete execution harness configuration.

    Bound to a conversation at creation time. Determines which models,
    providers, and cost constraints apply for the lifetime of that conversation.

    Profile selection is per-conversation, not per-request — switching profile
    mid-conversation is not supported (ADR-0044 D2).

    Attributes:
        name: Profile identifier (e.g. "local", "cloud").
        description: Human-readable description of this profile.
        primary_model: Model identifier for the primary agent.
        sub_agent_model: Model identifier for spawned sub-agents.
        provider_type: Whether this profile uses local or cloud inference.
        cost_limit_per_session: Maximum spend in USD per conversation session.
            None means no limit (appropriate for local profiles with no API cost).
        delegation: Cross-provider escalation rules.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    primary_model: str
    sub_agent_model: str
    provider_type: Literal["local", "cloud"]
    cost_limit_per_session: float | None = None
    delegation: DelegationConfig = Field(default_factory=DelegationConfig)


def load_profile(name: str, profiles_dir: str | Path = "config/profiles") -> ExecutionProfile:
    """Load an execution profile by name from the profiles directory.

    Args:
        name: Profile name without extension (e.g. "local", "cloud").
        profiles_dir: Directory containing profile YAML files. Defaults to
            "config/profiles" relative to the working directory.

    Returns:
        Loaded and validated ExecutionProfile.

    Raises:
        FileNotFoundError: If no YAML file for the given profile name exists.
        ValueError: If the profile YAML is structurally invalid.
    """
    # Validate name before any filesystem access.
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise ValueError(
            f"Profile name '{name}' contains invalid characters; "
            "only alphanumeric characters, underscores, and dashes are allowed."
        )

    # Enumerate the profiles directory and select the matching file by stem.
    # The path passed to open() is derived from the filesystem glob result —
    # not from user input — which breaks the taint chain for path injection.
    profiles_path = Path(profiles_dir)
    matched: Path | None = next(
        (p for p in profiles_path.glob("*.yaml") if p.stem == name),
        None,
    )
    if matched is None:
        raise FileNotFoundError(f"Profile '{name}' not found in {profiles_dir}")

    with open(matched) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Profile file at {matched} must contain a YAML mapping, got {type(data)}")

    return ExecutionProfile(**data)


def list_profiles(profiles_dir: str | Path = "config/profiles") -> list[str]:
    """List available profile names in the profiles directory.

    Args:
        profiles_dir: Directory to scan for YAML profile files.

    Returns:
        Sorted list of profile names (filenames without the .yaml extension).
        Returns an empty list when the directory does not exist.
    """
    path = Path(profiles_dir)
    if not path.exists():
        return []
    return sorted(p.stem for p in path.glob("*.yaml"))
