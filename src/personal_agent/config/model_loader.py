"""Load and validate model configuration from YAML file.

This module provides the main entry point for loading model configuration:
- Loads config/models.yaml
- Validates against Pydantic schema
- Returns typed ModelConfig object

All configuration loaders live in the config/ module per ADR-0007.
"""

import functools
from pathlib import Path
from urllib.parse import urlparse

import structlog
from pydantic import ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.config.settings import AppConfig
from personal_agent.llm_client.models import ModelConfig

#: A parsed ``config/model_roles.yaml`` mapping.
_RoleMatrix = dict[str, object]

log = structlog.get_logger(__name__)

#: Neutral placeholder host baked into config/models*.yaml (FRE-895) — the real Mac
#: SLM Cloudflare-tunnel host never lands in tracked source. See settings.slm_tunnel_base_url.
_SLM_TUNNEL_PLACEHOLDER_HOST = "slm.example.com"


def _apply_slm_tunnel_override(config: ModelConfig, settings: AppConfig) -> ModelConfig:
    """Rewrite placeholder SLM tunnel endpoints to the real tunnel base (FRE-895).

    Any model endpoint pointed at ``_SLM_TUNNEL_PLACEHOLDER_HOST`` is rewritten to
    ``settings.slm_tunnel_base_url`` (path preserved) when that setting is configured;
    otherwise the placeholder passes through untouched.

    Args:
        config: The loaded model config to rewrite.
        settings: The specific ``AppConfig`` to resolve the override against —
            never the live global singleton implicitly, so a caller resolving
            against an explicit (e.g. test/eval) ``AppConfig`` (ADR-0112 D3/AC-2's
            "same interface, no code edit" seam) gets a deterministic result from
            *that* config, not whatever the current process happens to have set.
    """
    real_base = settings.slm_tunnel_base_url
    if not real_base:
        return config

    real = urlparse(real_base.rstrip("/"))
    updated_models = dict(config.models)
    changed = False
    for role, definition in config.models.items():
        if definition.endpoint is None:
            continue
        parsed = urlparse(definition.endpoint)
        if parsed.hostname != _SLM_TUNNEL_PLACEHOLDER_HOST:
            continue
        new_endpoint = parsed._replace(scheme=real.scheme, netloc=real.netloc).geturl()
        updated_models[role] = definition.model_copy(update={"endpoint": new_endpoint})
        changed = True

    if not changed:
        return config
    return config.model_copy(update={"models": updated_models})


class ModelConfigError(ConfigLoadError):
    """Raised when model configuration cannot be loaded or is invalid."""

    pass


class ModelRoleError(ModelConfigError):
    """Raised when a role cannot be resolved from config/model_roles.yaml.

    ADR-0099 D1 stage 2 (FRE-650): there is exactly one hand-edited home for
    role assignment (the matrix); a missing matrix, an undeclared role, or a
    resolved model key absent from the active profile's ``models:`` mapping
    is a deterministic failure — never a silent fallback.
    """

    pass


@functools.lru_cache(maxsize=8)
def _load_model_config_at_path(config_path_str: str) -> ModelConfig:
    """Load and validate model configuration for a resolved path."""
    config_path = Path(config_path_str)

    log.info("loading_model_config", config_path=str(config_path))

    # Load YAML file using shared loader
    try:
        content = load_yaml_file(config_path, error_class=ModelConfigError)
        if not content:
            log.warning("model_config_empty", config_path=str(config_path))
            # Return empty config with empty models dict
            return ModelConfig(models={})
    except ConfigLoadError as e:
        raise ModelConfigError(f"Failed to load model config file: {e}") from None

    # Validate against Pydantic schema
    try:
        config = ModelConfig.model_validate(content)
        log.info(
            "model_config_loaded",
            models_count=len(config.models),
            model_ids=[model.id for model in config.models.values()],
        )
        return config
    except ValidationError as e:
        # Format validation errors for better debugging
        error_messages = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            error_msg = f"{field_path}: {error['msg']}"
            error_messages.append(error_msg)

        error_summary = "\n".join(error_messages)
        raise ModelConfigError(f"Model configuration validation failed:\n{error_summary}") from None
    except Exception as e:
        raise ModelConfigError(f"Unexpected error validating model config: {e}") from None


def load_model_config(
    config_path: Path | str | None = None,
    *,
    settings: AppConfig | None = None,
) -> ModelConfig:
    """Load and validate model configuration from YAML file.

    Args:
        config_path: Path (or path string) to models.yaml file. If None, uses
            ``settings.model_config_path``.
        settings: The ``AppConfig`` to resolve the FRE-895 SLM-tunnel override
            against. ``None`` uses the live global singleton — pass an explicit
            ``AppConfig`` (e.g. from :func:`personal_agent.config.substrate.resolve_substrate`)
            to resolve deterministically against *that* config instead.

    Returns:
        Validated ModelConfig object (not a raw dict).

    Raises:
        ModelConfigError: If configuration cannot be loaded, parsed, or validated.

    Example:
        >>> from personal_agent.config import load_model_config
        >>> config = load_model_config()
        >>> print(config.models["primary"].id)
        qwen/qwen3-35b-a22b
    """
    if settings is None:
        from personal_agent.config import settings as _live_settings  # noqa: PLC0415

        settings = _live_settings

    if config_path is None:
        config_path = settings.model_config_path
        # Resolve relative paths to absolute (relative to project root)
        if not config_path.is_absolute():
            project_root = Path(__file__).parent.parent.parent.parent
            config_path = (project_root / config_path).resolve()
        else:
            config_path = config_path.resolve()
        log.debug("using_model_config_path_from_settings", path=str(config_path))
    elif isinstance(config_path, str):
        config_path = Path(config_path)

    # Ensure config_path is a Path at this point and use a canonical key for caching.
    config_path = Path(config_path).resolve()

    if not config_path.exists():
        raise ModelConfigError(f"Model config file not found: {config_path}")

    if not config_path.is_file():
        raise ModelConfigError(f"Model config path is not a file: {config_path}")

    return _apply_slm_tunnel_override(_load_model_config_at_path(str(config_path)), settings)


@functools.lru_cache(maxsize=8)
def _load_role_matrix(root_str: str) -> _RoleMatrix:
    """Load and cache ``config/model_roles.yaml`` for a resolved repo root.

    Cached by resolved root path (mirrors :func:`_load_model_config_at_path`'s
    pattern) — tests pointing ``root`` at a fixture directory must call
    ``_load_role_matrix.cache_clear()`` afterwards to avoid cache bleed
    between fixture roots.
    """
    from personal_agent.config.config_guard import load_matrix  # noqa: PLC0415

    return load_matrix(Path(root_str))


def resolve_role_model_key(
    role: str,
    *,
    config_path: Path | str | None = None,
    root: Path | None = None,
) -> str:
    """Resolve a role to its model key via ``config/model_roles.yaml`` (ADR-0099 D1 stage 2).

    The matrix is the one hand-edited home for role assignment. A
    ``divergence: forbidden`` role always resolves to its single ``all:``
    value, used under every active profile. A ``divergence: allowed`` role
    resolves to the ``local:``/``cloud:`` value matching whichever active
    profile ``config_path`` maps to. There is no fallback: a missing matrix,
    an undeclared role, an unresolvable divergence value, or a resolved key
    absent from the active profile's ``models:`` mapping all raise.

    Args:
        role: The matrix role name (e.g. ``"entity_extraction"``).
        config_path: Model-definition file path used to detect the active
            profile bucket (for ``allowed`` roles) and to validate the
            resolved key exists. ``None`` uses ``settings.model_config_path``
            (the live deployment's active config), matching
            :func:`load_model_config`'s own convention.
        root: Repo (or fixture) root containing ``config/model_roles.yaml``.
            Defaults to the real repo root; tests point this at a fixture.

    Returns:
        The resolved model key — a key in the active config's ``models:`` mapping.

    Raises:
        ModelRoleError: If the matrix is missing/empty, the role is
            undeclared, the role's divergence value has no matching entry,
            the active profile cannot be determined, or the resolved key is
            absent from the active profile's ``models:`` mapping.
    """
    from personal_agent.config.config_guard import (  # noqa: PLC0415
        repo_root,
        resolve_active_profile,
    )

    resolved_root = root if root is not None else repo_root()
    matrix = _load_role_matrix(str(resolved_root))
    if not matrix:
        raise ModelRoleError(
            f"config/model_roles.yaml is missing or empty under {resolved_root}; "
            f"cannot resolve role {role!r} (ADR-0099 D1 — no fallback)"
        )

    roles = matrix.get("roles", {})
    role_cfg = roles.get(role) if isinstance(roles, dict) else None
    if role_cfg is None:
        raise ModelRoleError(f"role {role!r} is not declared in config/model_roles.yaml roles:")

    if config_path is None:
        from personal_agent.config import settings  # noqa: PLC0415

        config_path = settings.model_config_path
    resolved_config_path = Path(config_path)
    if not resolved_config_path.is_absolute():
        resolved_config_path = (resolved_root / resolved_config_path).resolve()
    else:
        resolved_config_path = resolved_config_path.resolve()

    divergence = role_cfg.get("divergence") if isinstance(role_cfg, dict) else None
    raw_model_key: object
    if divergence == "forbidden":
        raw_model_key = role_cfg.get("all")
        if not raw_model_key:
            raise ModelRoleError(
                f"role {role!r} is divergence:forbidden but has no 'all' value "
                "in config/model_roles.yaml"
            )
    elif divergence == "allowed":
        profile = resolve_active_profile(resolved_config_path, matrix, resolved_root)
        if profile is None:
            raise ModelRoleError(
                "cannot determine the active profile for model_config_path="
                f"{resolved_config_path} against config/model_roles.yaml active_profiles"
            )
        raw_model_key = role_cfg.get(profile)
        if not raw_model_key:
            raise ModelRoleError(
                f"role {role!r} has divergence:allowed but no {profile!r} value "
                "in config/model_roles.yaml"
            )
    else:
        raise ModelRoleError(
            f"role {role!r} has invalid divergence {divergence!r} in "
            "config/model_roles.yaml (expected 'forbidden' or 'allowed')"
        )

    if not isinstance(raw_model_key, str):
        raise ModelRoleError(
            f"role {role!r} resolves to a non-string matrix value {raw_model_key!r}"
        )
    model_key = raw_model_key

    resolved_config = load_model_config(resolved_config_path)
    if model_key not in resolved_config.models:
        raise ModelRoleError(
            f"role {role!r} resolves to model key {model_key!r} which is not "
            f"defined under models: in {resolved_config_path}"
        )
    return model_key


def resolve_active_attribution(
    *,
    trace_id: str | None = None,
) -> tuple[str | None, str]:
    """Resolve the active primary model id and its config path string.

    ADR-0074 (FRE-376) requires every new session and every assistant
    message to carry the model attribution that was in effect when the row
    was written. This helper centralises the lookup so the service layer
    and the Redis event consumer both produce identical attribution.

    Args:
        trace_id: Originating chat trace, threaded through for log
            correlation on the warning path (ADR-0074 §I3). Optional because
            this helper is also called from startup smoke checks where no
            trace exists.

    Returns:
        ``(primary_model_id, model_config_path_str)`` — ``primary_model_id``
        is ``None`` only if the config has no ``primary`` role assignment
        (degenerate startup config); ``model_config_path_str`` is always
        the resolved path string from settings.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    config_path_str = str(settings.model_config_path)
    try:
        cfg = load_model_config()
        primary = cfg.models.get("primary")
        primary_id = primary.id if primary is not None else None
    except Exception as exc:  # noqa: BLE001 — keep the chat-turn path live
        log.warning(
            "model_attribution_resolve_failed",
            error=str(exc),
            config_path=config_path_str,
            trace_id=trace_id,
        )
        primary_id = None
    return primary_id, config_path_str


# Expected vision-capable roles on the deployed profiles (ADR-0101 §5).
# FRE-734: the drift that broke production was these roles unflagged in the
# deployed config file — so the startup guard checks these specific roles,
# not merely "some model is vision-capable".
_EXPECTED_VISION_ROLES: tuple[str, ...] = (
    "primary",
    "sub_agent",
    "claude_sonnet",
    "claude_haiku",
)


def check_vision_capabilities(*, trace_id: str | None = None) -> tuple[list[str], list[str]]:
    """Log the active config's vision-capable roles and warn on drift (ADR-0101 §5).

    Startup drift guard (FRE-734). Vision broke in production once because the
    deployed config file (``config/models.cloud.yaml``, selected via
    ``AGENT_MODEL_CONFIG_PATH``) was never given the ``supports_vision`` flag that
    ``config/models.yaml`` carried, so every image turn failed routing while CI
    stayed green. This emits a startup signal — an info line listing the
    vision-capable roles, plus a role-aware warning when an expected production
    role is not flagged — so the drift is visible in logs at boot, not discovered
    by a user.

    Non-fatal by design: vision is not load-bearing, so a config gap must not down
    the gateway; the CI parity test
    (``test_deployed_vision_capable_models_flagged``) is the real gate. Any failure
    loading the config is swallowed and logged, never raised.

    Args:
        trace_id: Optional trace correlation id. Startup has no request context,
            so this is normally None.

    Returns:
        ``(capable_roles, missing_roles)`` — ``capable_roles`` is every role key in
        the active config with ``supports_vision=True`` (sorted); ``missing_roles``
        is the subset of ``_EXPECTED_VISION_ROLES`` absent or not vision-flagged.
        Both empty lists on a load failure.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    config_path_str = str(settings.model_config_path)
    try:
        cfg = load_model_config()
    except Exception as exc:  # noqa: BLE001 — startup diagnostic must never down the service
        log.warning(
            "vision_capabilities_check_failed",
            error=str(exc),
            config_path=config_path_str,
            trace_id=trace_id,
        )
        return [], []

    capable = sorted(key for key, model in cfg.models.items() if model.supports_vision)
    missing = [
        role
        for role in _EXPECTED_VISION_ROLES
        if role not in cfg.models or not cfg.models[role].supports_vision
    ]

    log.info(
        "vision_capabilities_at_startup",
        vision_capable_roles=capable,
        config_path=config_path_str,
        trace_id=trace_id,
    )
    if missing:
        log.warning(
            "vision_capable_roles_missing",
            missing_roles=missing,
            config_path=config_path_str,
            remedy=(
                "Set supports_vision: true on these roles in the active model config "
                "(ADR-0101 §5; FRE-734 config-parity drift)."
            ),
            trace_id=trace_id,
        )
    return capable, missing
