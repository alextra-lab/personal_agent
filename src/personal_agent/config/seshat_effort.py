"""Seshat effort: one level per session sets the harness and the primary's reasoning (ADR-0157).

The three presets live in ``config/seshat_effort.yaml``. The gateway loads them once at start
into frozen models and refuses to start when the file is invalid (ADR-0157 D3). A value that the
owner stored in the settings menu wins over the file from the next turn (FRE-1723).

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
4. the primary's catalog mode on both client paths (:func:`primary_mode_for`).
"""

from __future__ import annotations

import contextvars
import functools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from personal_agent.config.setting_overrides import (
    EFFORT_DEFAULT_LEVEL_PARAMETER,
    EFFORT_LEVEL_PARAMETER_PREFIX,
    effort_overrides,
)
from personal_agent.exceptions import SeshatEffortConfigError

if TYPE_CHECKING:
    from personal_agent.config.settings import AppConfig, Thoroughness

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
    _check_round_ceilings(config, settings)
    return config


def _check_round_ceilings(config: SeshatEffortConfig, settings: AppConfig) -> None:
    """Refuse presets that the global settings do not allow (ADR-0157 D3)."""
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


@functools.lru_cache(maxsize=1)
def file_seshat_effort() -> SeshatEffortConfig:
    """Return the presets as the file states them, loaded once per process.

    Returns:
        The frozen presets.

    Raises:
        SeshatEffortConfigError: See :func:`load_seshat_effort`.
    """
    return load_seshat_effort()


def _with_override(
    config: SeshatEffortConfig, parameter: str, value: JsonValue
) -> SeshatEffortConfig:
    """Return presets with one stored value applied, checked as the file is checked (FRE-1723).

    Strict, so a value that the file's YAML types could not hold (``"yes"``, ``true`` as a round
    budget) is refused and not coerced.
    """
    from personal_agent.config import get_settings  # noqa: PLC0415

    raw = config.model_dump(mode="json")
    if parameter == EFFORT_DEFAULT_LEVEL_PARAMETER:
        raw["default_level"] = value
    else:
        level = parameter.removeprefix(EFFORT_LEVEL_PARAMETER_PREFIX)
        if level not in EFFORT_LEVELS:
            raise SeshatEffortConfigError(f"{level} is not an effort level")
        if not isinstance(value, dict) or "level" in value:
            raise SeshatEffortConfigError(f"{parameter} takes the values of a preset")
        raw["levels"][level] = {**value, "level": level}
    try:
        candidate = SeshatEffortConfig.model_validate(raw, strict=True)
    except ValidationError as exc:
        reasons = (
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise SeshatEffortConfigError("; ".join(reasons)) from exc
    _check_round_ceilings(candidate, get_settings())
    return candidate


def validate_effort_override(parameter: str, value: JsonValue) -> JsonValue:
    """Check one proposed preset edit against the file values (FRE-1723).

    Each parameter stands alone: a level's checks read only that level, so another stored override
    cannot change the result.

    Args:
        parameter: ``effort.default_level`` or ``effort.level.<level>``.
        value: The proposed value.

    Returns:
        The value to store. For a preset, every field, with no ``level`` key.

    Raises:
        SeshatEffortConfigError: With the reason, when the value is refused.
    """
    config = _with_override(file_seshat_effort(), parameter, value)
    if parameter == EFFORT_DEFAULT_LEVEL_PARAMETER:
        return config.default_level
    return preset_values(config, parameter.removeprefix(EFFORT_LEVEL_PARAMETER_PREFIX))


def preset_values(config: SeshatEffortConfig, level: str) -> JsonValue:
    """Return one level's values in the form that the override table stores (FRE-1723).

    Args:
        config: The presets to read.
        level: A level name that ``config`` holds.

    Returns:
        Every field of the level, with no ``level`` key.
    """
    values: JsonValue = config.levels[level].model_dump(mode="json", exclude={"level"})  # type: ignore[index]
    return values


@functools.lru_cache(maxsize=8)
def _effort_with_overrides(overrides: tuple[tuple[str, str], ...]) -> SeshatEffortConfig:
    """Return the presets with the stored overrides applied.

    A stored override that fails, for example after ``.env`` lowered the ceiling, is skipped. Every
    turn reads this path, so a stored row must never make it raise. The settings menu shows a skipped
    override as not usable.

    Args:
        overrides: ``(parameter, JSON value)`` pairs from :func:`effort_overrides`.

    Returns:
        The presets with each usable override applied.
    """
    config = file_seshat_effort()
    for parameter, encoded in overrides:
        try:
            config = _with_override(config, parameter, json.loads(encoded))
        except SeshatEffortConfigError as exc:
            log.warning("effort_override_skipped", parameter=parameter, error=str(exc))
    return config


def get_seshat_effort() -> SeshatEffortConfig:
    """Return the presets that apply now: the file, then the stored overrides (FRE-1723).

    Returns:
        The frozen presets.

    Raises:
        SeshatEffortConfigError: See :func:`load_seshat_effort`.
    """
    overrides = effort_overrides()
    return _effort_with_overrides(overrides) if overrides else file_seshat_effort()


def effort_spec(level: EffortLevel | None) -> EffortSpec:
    """Return the spec of a level.

    Args:
        level: The level name. ``None`` returns the spec of ``default_level``.

    Returns:
        The frozen spec.
    """
    config = get_seshat_effort()
    return config.levels[level if level is not None else config.default_level]


EffortProvenance = Literal["server-hydrated", "default"]


def hydrate_effort(stored: object) -> tuple[EffortLevel, EffortProvenance]:
    """Return a stored level and its provenance, for the session GETs and the attach replay.

    Args:
        stored: The value read from ``sessions.seshat_effort``.

    Returns:
        ``(level, "server-hydrated")`` for a stored level, else ``(default_level, "default")``.
        The database refuses any other value, so the second case means the value could not be
        read. Provenance then never claims a stored choice (ADR-0121 invariant 2).
    """
    for level in EFFORT_LEVELS:
        if stored == level:
            return level, "server-hydrated"
    return effort_spec(None).level, "default"


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


def primary_mode_for(deployment_key: str) -> str | None:
    """Return the mode to request for the deployment that answers this turn (ADR-0157 D5).

    Args:
        deployment_key: The catalog key of the answering deployment: the session's primary, or
            the vision deployment on an escalated turn.

    Returns:
        The level's mode when the deployment declares it. ``None`` outside a chat turn, for a
        level without a mode, or for a mode the deployment does not declare: the client then
        keeps its own resolution, so a binding's mode is not overridden.
    """
    spec = get_current_effort()
    if spec is None or spec.primary_mode is None:
        return None
    from personal_agent.config.model_loader import load_model_config  # noqa: PLC0415

    model_def = load_model_config().models.get(deployment_key)
    if model_def is None or spec.primary_mode not in model_def.modes:
        return None
    return spec.primary_mode
