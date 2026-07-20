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
from personal_agent.llm_client.models import (
    ModelConfig,
    ModelDefinition,
    required_kind_for_role,
)

#: A parsed ``config/model_roles.yaml`` mapping.
_RoleMatrix = dict[str, object]

log = structlog.get_logger(__name__)

_REPO_CONFIG: Path = Path(__file__).resolve().parents[3] / "config"

#: The one deployment catalog (ADR-0121). ``config/models.cloud.yaml`` was
#: deleted in FRE-916 phase 2 after being proven comment-only-different from this
#: file, together with the ``AGENT_MODEL_CONFIG_PATH`` setting that selected it:
#: there is no longer a per-environment catalog choice, so the path is a constant
#: rather than configuration. The one legitimate environment difference the second
#: file used to carry is expressed as provider ``base_url`` instead.
CATALOG_PATH: Path = _REPO_CONFIG / "models.yaml"

#: Repo-relative form of :data:`CATALOG_PATH`, for attribution columns that
#: record which catalog a row was written under (ADR-0074). Deliberately relative
#: and stable: an absolute path would embed the deployment's filesystem layout in
#: every session row and differ between host and container.
CATALOG_RELPATH: str = "config/models.yaml"

#: Layer-3 bindings are merged into the real catalog only — a fixture or benchmark
#: file defines its own deployments, and injecting the repo's bindings would dangle
#: every reference. Matched on the full resolved path, not the basename: the
#: fixtures are called `models.yaml` too.
_REAL_CATALOGS: frozenset[Path] = frozenset({CATALOG_PATH})

#: Layer 3 role bindings (ADR-0121), merged into the same ModelConfig so all
#: three layers validate in one pass.
ROLE_BINDINGS_PATH: Path = Path(__file__).resolve().parents[3] / "config" / "model_roles.yaml"

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

    def _rewrite(url: str | None) -> str | None:
        if url is None:
            return None
        parsed = urlparse(url)
        if parsed.hostname != _SLM_TUNNEL_PLACEHOLDER_HOST:
            return None
        return parsed._replace(scheme=real.scheme, netloc=real.netloc).geturl()

    updated_models = dict(config.models)
    changed = False
    for role, definition in config.models.items():
        new_endpoint = _rewrite(definition.endpoint)
        if new_endpoint is not None:
            updated_models[role] = definition.model_copy(update={"endpoint": new_endpoint})
            changed = True

    # Rewrite the provider base_url too. Nothing dispatches off it yet — the SLM
    # models still carry their own endpoint — but ADR-0121 makes the provider the
    # authoritative locus, and FRE-917 unifies resolution onto it. Leaving the
    # placeholder host in that authoritative field is a trap for the first
    # consumer that reads it; rewrite it here so the two loci never disagree.
    updated_providers = dict(config.providers)
    for name, provider in config.providers.items():
        new_base = _rewrite(provider.base_url)
        if new_base is not None:
            updated_providers[name] = provider.model_copy(update={"base_url": new_base})
            changed = True

    if not changed:
        return config
    return config.model_copy(update={"models": updated_models, "providers": updated_providers})


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

    # Merge Layer 3 bindings so all three layers validate together (ADR-0121).
    if "roles" not in content and config_path.resolve() in _REAL_CATALOGS:
        raw = load_yaml_file(ROLE_BINDINGS_PATH, error_class=ModelConfigError) or {}
        content = {**content, "roles": raw.get("bindings", {})}

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
        config_path: Path (or path string) to a models.yaml file. If None, uses
            :data:`CATALOG_PATH` — the single deployment catalog. Callers pass an
            explicit path only for fixtures and tests; there is no longer a
            per-environment catalog choice (FRE-916 phase 2).
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
        >>> print(config.models["qwen3.6-35b-thinking"].id)
        qwen/qwen3-35b-a22b
    """
    if settings is None:
        from personal_agent.config import settings as _live_settings  # noqa: PLC0415

        settings = _live_settings

    if config_path is None:
        config_path = CATALOG_PATH
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

    The matrix is the one hand-edited home for role assignment. Every role
    resolves to its single ``all:`` value. FRE-916 phase 2 retired the
    ``divergence: allowed`` branch along with the second catalog: with one
    catalog there is no per-profile value for an assignment to diverge into.
    There is no fallback — a missing matrix, an undeclared role, a role with no
    ``all`` key, or a resolved key absent from the catalog's ``models:`` mapping
    all raise.

    Args:
        role: The matrix role name (e.g. ``"entity_extraction"``).
        config_path: Catalog path the resolved key is validated against.
            ``None`` uses :data:`CATALOG_PATH`, matching
            :func:`load_model_config`'s own convention. Tests point it at a
            fixture.
        root: Repo (or fixture) root containing ``config/model_roles.yaml``.
            Defaults to the real repo root; tests point this at a fixture.

    Returns:
        The resolved model key — a key in the active config's ``models:`` mapping.

    Raises:
        ModelRoleError: If the matrix is missing/empty, the role is undeclared,
            the role declares no ``all`` key, or the resolved key is absent from
            the catalog's ``models:`` mapping.
    """
    from personal_agent.config.config_guard import repo_root  # noqa: PLC0415

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
        config_path = CATALOG_PATH
    resolved_config_path = Path(config_path)
    if not resolved_config_path.is_absolute():
        resolved_config_path = (resolved_root / resolved_config_path).resolve()
    else:
        resolved_config_path = resolved_config_path.resolve()

    raw_model_key: object = role_cfg.get("all") if isinstance(role_cfg, dict) else None
    if not raw_model_key:
        raise ModelRoleError(
            f"role {role!r} declares no 'all' model key in config/model_roles.yaml"
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


def resolve_role_target(
    role: str,
    *,
    model_key: str | None = None,
    config: ModelConfig | None = None,
) -> tuple[str, ModelDefinition | None]:
    """Resolve a role to its deployment key and EFFECTIVE definition (ADR-0121).

    The deployment carries what the model *is*; the Layer-3 binding carries how
    this role *uses* it. Reading ``config.models[key]`` directly returns only the
    former, silently dropping per-use parameters.

    Overrides apply **only when the resolved key is the binding's own
    deployment.** When an active ExecutionProfile redirects the role elsewhere
    (cloud ``sub_agent`` -> ``claude_haiku``), that deployment's own values stand
    — matching pre-ADR-0121 behaviour, where the profile's model was looked up
    whole rather than merged. Overrides are model-specific in practice, so
    carrying them onto a different model would change the call.

    Args:
        role: Role name (e.g. ``"sub_agent"``).
        model_key: Already-resolved key, when the caller resolved it through an
            active profile. ``None`` uses the role's binding.
        config: Catalog to resolve against. ``None`` loads the live one.

    Returns:
        ``(deployment_key, effective_definition)``. Both, because every caller
        needs both — the definition to make the call, the key to acquire the
        right concurrency slot — and deriving the key separately would duplicate
        this resolution order at the call site. Definition is ``None`` when the
        key names no deployment.
    """
    resolved_config = config if config is not None else load_model_config()
    binding = resolved_config.roles.get(role)
    key = model_key if model_key is not None else (binding.deployment if binding else role)

    definition = resolved_config.models.get(key)
    if definition is None or binding is None or key != binding.deployment:
        return key, definition

    overrides = {
        field: value
        for field, value in binding.model_dump(exclude={"deployment", "open"}).items()
        if value is not None
    }
    return key, (definition.model_copy(update=overrides) if overrides else definition)


def resolve_role_definition(
    role: str,
    *,
    model_key: str | None = None,
    config: ModelConfig | None = None,
) -> ModelDefinition | None:
    """Return a role's effective definition — see :func:`resolve_role_target`.

    Convenience wrapper for callers that need the definition but not the
    deployment key. Prefer :func:`resolve_role_target` when you also need the
    key (to acquire a concurrency slot, or to compare against a routing key).
    """
    return resolve_role_target(role, model_key=model_key, config=config)[1]


def is_selectable_binding(role: str, key: str, config: ModelConfig) -> bool:
    """Whether ``key`` is a legal user selection for ``role`` (ADR-0121 §6).

    Authorization is the **intersection** of a role-side and a model-side fact:
    the role is ``open`` (a blast-radius policy on the role) AND the key names a
    valid, ``kind``-compatible catalog entry (intrinsic to the model). Both
    halves are required and both fail closed — a pinned role, an unknown key, or
    a wrong-kind key all return ``False``.

    This is the single predicate behind both guardrail actions: the resolver
    (:func:`resolve_selected_deployment`) falls back to the default when it is
    ``False``, and the selection write API rejects the write when it is
    ``False``. (Provider *availability* — health — is a read-time concern layered
    on top by FRE-918/AC-5; existence + kind + open are the T2 checks.)

    Args:
        role: The role a selection is proposed for.
        key: The proposed catalog deployment key.
        config: The catalog to validate against.

    Returns:
        ``True`` iff ``role`` is open and ``key`` is a valid, kind-compatible
        deployment; ``False`` otherwise.
    """
    binding = config.roles.get(role)
    if binding is None or not binding.open:
        return False
    definition = config.models.get(key)
    if definition is None:
        return False
    return definition.kind is required_kind_for_role(role)


def resolve_selected_deployment(role: str, selection: str | None, config: ModelConfig) -> str:
    """Resolve the effective deployment key for a role given an advisory selection.

    Fail-closed (ADR-0121 §6): the ``selection`` is honoured ONLY when
    :func:`is_selectable_binding` accepts it (open role + valid, kind-compatible
    key). Otherwise the role's configured binding default wins — never an
    arbitrary or empty model. Pinned roles are never consulted, so a selection
    row that exists for a pinned role (injected directly, bypassing the write
    API) is structurally ignored (AC-4a).

    Args:
        role: The role being resolved.
        selection: The advisory selected key (from the selection store or a
            client), or ``None`` when the session has no selection for this role.
        config: The catalog to resolve against.

    Returns:
        The deployment key to run this role on — the honoured selection, or the
        role's configured binding default (its own name when the role is
        unbound, matching :func:`resolve_role_target`'s fallback).
    """
    binding = config.roles.get(role)
    default_key = binding.deployment if binding else role
    if selection is None:
        return default_key
    return selection if is_selectable_binding(role, selection, config) else default_key


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
        :data:`CATALOG_RELPATH`, retained so existing session/message rows keep a
        populated attribution column (ADR-0121 step 4 migrates this dimension to
        provider + model properly).
    """
    config_path_str = CATALOG_RELPATH
    try:
        # Resolve through the binding: "primary" is a ROLE, and since ADR-0121
        # the catalog is keyed by model, so models.get("primary") returns None
        # and every session/message would lose its model attribution.
        _, primary = resolve_role_target("primary")
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
#: Names expected to be vision-capable on the deployed config. A mix of ROLES
#: (primary, sub_agent) and DEPLOYMENT keys (claude_sonnet, claude_haiku) —
#: resolve_role_target handles both, since a name with no binding falls back to
#: a direct key lookup. A raw models[...] lookup would warn about the roles on
#: every boot now that ADR-0121 keys the catalog by model.
_EXPECTED_VISION_ROLES: tuple[str, ...] = (
    "primary",
    "sub_agent",
    "claude_sonnet",
    "claude_haiku",
)


def check_vision_capabilities(*, trace_id: str | None = None) -> tuple[list[str], list[str]]:
    """Log the active config's vision-capable roles and warn on drift (ADR-0101 §5).

    Startup drift guard (FRE-734). Vision broke in production once because the
    then-deployed second config file was never given the ``supports_vision`` flag
    that ``config/models.yaml`` carried, so every image turn failed routing while
    CI stayed green. FRE-916 phase 2 removed that whole class of drift by
    collapsing to a single catalog — this guard is retained because a role can
    still be re-bound to a deployment that lacks the flag. It emits a startup
    signal — an info line listing the
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
    config_path_str = CATALOG_RELPATH
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
        name
        for name in _EXPECTED_VISION_ROLES
        if (definition := resolve_role_target(name, config=cfg)[1]) is None
        or not definition.supports_vision
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
