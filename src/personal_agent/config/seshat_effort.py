"""Seshat effort: one level per session sets the harness and the primary's reasoning (ADR-0157).

The three presets live in ``config/seshat_effort.yaml``. The gateway loads them once at start
into frozen models and refuses to start when the file is invalid (ADR-0157 D3).

At turn launch the chat path resolves the session's level into one frozen :class:`EffortSpec`
and carries it through the turn in an async-safe context variable, the same mechanism that
``config.selection`` uses for the model selection. Each ``asyncio.Task`` copies the context when
it is created, so a level change during a turn never reaches that turn (ADR-0079 invariant 7),
and the helpers, which run in tasks that the turn creates, read the turn's spec.

Outside a chat turn no spec is set (a scheduled job, a script, a test that drives one function).
Every reader then keeps the behavior from before ADR-0157: the global round budget, the
``baseline`` planner prompt, expansion as the global settings allow, and the deployment's
``default_mode``. Both chat entry paths always set a spec (ADR-0157 D2 rule 7).

These readers take the spec, not the global settings (ADR-0157 D3):

1. the decomposition step (``expansion``);
2. the planner prompt render (``sub_agent_rounds`` and ``planner_policy``);
3. the helper's round budget (:func:`rounds_for_turn`);
4. the primary's catalog mode on both client paths (:func:`effective_primary_mode`).
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from personal_agent.exceptions import SeshatEffortConfigError

if TYPE_CHECKING:
    from personal_agent.config.settings import AppConfig, Thoroughness
    from personal_agent.llm_client.models import ModelDefinition

log = structlog.get_logger(__name__)

#: The committed presets file (ADR-0157 D1).
SESHAT_EFFORT_PATH: Path = Path(__file__).resolve().parents[3] / "config" / "seshat_effort.yaml"

EffortLevel = Literal["fast", "standard", "deep"]
EFFORT_LEVELS: tuple[EffortLevel, ...] = ("fast", "standard", "deep")
PlannerPolicy = Literal["baseline", "p2"]
#: The catalog mode names a level can request (ADR-0157 D5). ``None`` means the deployment's
#: ``default_mode``.
PrimaryModeName = Literal["fast", "deep"]


class EffortSpec(BaseModel):
    """The values of one level, resolved once per turn (ADR-0157 D1, D3).

    Attributes:
        level: The level name.
        expansion: ``False`` routes every turn to ``SINGLE`` with reason ``effort_fast`` (D4).
        sub_agent_rounds: The helper round budget, for every thoroughness level. ``None`` only
            when ``expansion`` is ``False``.
        planner_policy: ``p2`` adds the D6 block to the D2 planner prompt. ``None`` only when
            ``expansion`` is ``False``.
        primary_mode: The catalog mode the primary requests when its deployment declares it.
            ``None`` requests the deployment's ``default_mode``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: EffortLevel
    expansion: bool
    sub_agent_rounds: int | None = Field(default=None, ge=1)
    planner_policy: PlannerPolicy | None = None
    primary_mode: PrimaryModeName | None = None

    @model_validator(mode="after")
    def _expansion_needs_its_values(self) -> EffortSpec:
        if self.expansion and (self.sub_agent_rounds is None or self.planner_policy is None):
            raise ValueError(
                f"level {self.level!r} expands, so it needs sub_agent_rounds and planner_policy"
            )
        return self


class SeshatEffortConfig(BaseModel):
    """The validated presets file (ADR-0157 D1).

    Attributes:
        default_level: The level a new session stores when its request names none (D2).
        levels: Exactly the three levels, by name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_level: EffortLevel
    levels: Mapping[EffortLevel, EffortSpec]

    @model_validator(mode="after")
    def _every_level_present(self) -> SeshatEffortConfig:
        missing = [name for name in EFFORT_LEVELS if name not in self.levels]
        if missing:
            raise ValueError(f"missing level(s): {', '.join(missing)}")
        return self


def load_seshat_effort(
    path: Path | None = None, *, settings: AppConfig | None = None
) -> SeshatEffortConfig:
    """Load and validate the presets against the global ceilings (ADR-0157 D3).

    Args:
        path: The presets file. ``None`` uses :data:`SESHAT_EFFORT_PATH`.
        settings: The settings that hold the ceilings. ``None`` uses the live settings.

    Returns:
        The frozen presets.

    Raises:
        SeshatEffortConfigError: If the file is missing or invalid, a level name or a mode name
            is unknown, a level's ``sub_agent_rounds`` is above ``sub_agent_max_tool_iterations``,
            or ``sub_agent_rounds_by_thoroughness`` is set.
    """
    if settings is None:
        from personal_agent.config import get_settings  # noqa: PLC0415

        settings = get_settings()
    source = path if path is not None else SESHAT_EFFORT_PATH
    try:
        raw = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SeshatEffortConfigError(f"cannot read {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SeshatEffortConfigError(f"{source} must hold a mapping")
    levels = raw.get("levels")
    if isinstance(levels, dict):
        # The name lives once, as the key. Each spec carries it, so a reader holds one value.
        raw = {
            **raw,
            "levels": {
                name: {**values, "level": name} if isinstance(values, dict) else values
                for name, values in levels.items()
            },
        }
    try:
        config = SeshatEffortConfig.model_validate(raw)
    except ValidationError as exc:
        raise SeshatEffortConfigError(f"{source} is invalid: {exc}") from exc

    if settings.sub_agent_rounds_by_thoroughness:
        raise SeshatEffortConfigError(
            "sub_agent_rounds_by_thoroughness must be empty: a Seshat effort level sets one "
            "round budget for every thoroughness level (ADR-0157 D3)"
        )
    ceiling = settings.sub_agent_max_tool_iterations
    above = {
        name: spec.sub_agent_rounds
        for name, spec in config.levels.items()
        if spec.sub_agent_rounds is not None and spec.sub_agent_rounds > ceiling
    }
    if above:
        raise SeshatEffortConfigError(
            f"level round budgets {above} exceed sub_agent_max_tool_iterations ({ceiling}), "
            "the ceiling of every level (ADR-0157 D3)"
        )
    return config


@functools.lru_cache(maxsize=1)
def get_seshat_effort() -> SeshatEffortConfig:
    """Return the presets, loaded once per process.

    Returns:
        The frozen presets.

    Raises:
        SeshatEffortConfigError: See :func:`load_seshat_effort`.
    """
    return load_seshat_effort()


def effort_spec(level: EffortLevel | None) -> EffortSpec:
    """Return the spec of a level.

    Args:
        level: The level name. ``None`` returns the spec of ``default_level``.

    Returns:
        The frozen spec.
    """
    config = get_seshat_effort()
    return config.levels[level if level is not None else config.default_level]


#: The spec of the current turn. ``None`` outside a chat turn.
_current_effort: contextvars.ContextVar[EffortSpec | None] = contextvars.ContextVar(
    "current_effort", default=None
)


def set_current_effort(spec: EffortSpec) -> contextvars.Token[EffortSpec | None]:
    """Set the resolved spec for the current async context, once per turn.

    Args:
        spec: The spec resolved at turn launch.

    Returns:
        A token for :func:`reset_current_effort`.
    """
    return _current_effort.set(spec)


def get_current_effort() -> EffortSpec | None:
    """Return the spec of the current turn.

    Returns:
        The spec set at turn launch, or ``None`` outside a chat turn. A reader then keeps the
        behavior from before ADR-0157 (see the module docstring).
    """
    return _current_effort.get()


def reset_current_effort(token: contextvars.Token[EffortSpec | None]) -> None:
    """Restore the spec to a prior value.

    Args:
        token: The token that :func:`set_current_effort` returned.
    """
    _current_effort.reset(token)


def spec_rounds(spec: EffortSpec | None, level: Thoroughness) -> int:
    """Return the helper round budget of a spec for one thoroughness level (ADR-0157 D3).

    Args:
        spec: The level's spec, or ``None`` outside a chat turn.
        level: The task's thoroughness level.

    Returns:
        The spec's ``sub_agent_rounds``, else the global ``sub_agent_rounds_for(level)``. Only a
        level without expansion has no rounds, and such a level starts no helper.
    """
    if spec is not None and spec.sub_agent_rounds is not None:
        return spec.sub_agent_rounds
    from personal_agent.config import get_settings  # noqa: PLC0415

    return get_settings().sub_agent_rounds_for(level)


def rounds_for_turn(level: Thoroughness) -> int:
    """Return the helper round budget that binds on this turn (ADR-0157 D3 item 3).

    Args:
        level: The task's thoroughness level.

    Returns:
        :func:`spec_rounds` of the turn's spec.
    """
    return spec_rounds(get_current_effort(), level)


def effective_primary_mode(spec: EffortSpec | None, model_def: ModelDefinition) -> str:
    """Return the catalog mode that the answering deployment runs on this turn (ADR-0157 D5).

    Args:
        spec: The turn's spec, or ``None`` outside a chat turn.
        model_def: The answering deployment's catalog definition.

    Returns:
        The level's mode when the deployment declares it, else its ``default_mode``. No warning
        is logged on the fallback: it is the designed case (Deep declares on no model at this
        date).
    """
    if spec is not None and spec.primary_mode is not None and spec.primary_mode in model_def.modes:
        return spec.primary_mode
    return model_def.default_mode


def primary_mode_for(deployment_key: str) -> str | None:
    """Return the mode to request for the deployment that answers this turn (ADR-0157 D5).

    Args:
        deployment_key: The catalog key of the answering deployment: the session's primary, or
            the vision deployment on an escalated turn.

    Returns:
        :func:`effective_primary_mode` for the turn's spec, or ``None`` outside a chat turn or for
        a key without catalog modes (the client then keeps its own resolution).
    """
    spec = get_current_effort()
    if spec is None:
        return None
    from personal_agent.config.model_loader import load_model_config  # noqa: PLC0415

    model_def = load_model_config().models.get(deployment_key)
    if model_def is None or not model_def.modes:
        return None
    return effective_primary_mode(spec, model_def)
