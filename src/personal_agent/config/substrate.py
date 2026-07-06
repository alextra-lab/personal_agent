#!/usr/bin/env python3
"""Substrate backend-selection seam (ADR-0112 D3 / AC-2, FRE-816).

Answers "which backend does each substrate component use under profile X?" from
committed files + AppConfig alone — no running container. Reads the substrate
manifest (``config/substrate.yaml``) and resolves each component's ``source``
against the same config machinery every runtime consumer uses:

* ``setting:<field>``       → ``getattr(settings, field)`` (an AppConfig field)
* ``model_endpoint:<role>`` → the ``endpoint`` of the active model file's
  model-def for ``<role>`` (config/models*.yaml, ADR-0031/0099)
* ``backed_by:<component>`` → this component rides another's backend
  (``vector_index → neo4j``)

This is the seam ADR-0112 D3 requires: every D3 substrate component (stores,
embedder, reranker, SLM, search/vector index) is pointable, per config profile,
at a local/self-hosted OR managed backend with **no code change**. It resolves
through AppConfig so it never reads ``os.environ`` directly.

**Scope (FRE-816 = AC-2 branch b).** This delivers the resolver + profile
mechanism; only local backends are wired into serving paths today, so it
satisfies AC-2's config-test escape-hatch ("a second profile resolves through
the same interface with no code edit"), not the "boots and serves under both
profiles" branch. Rewiring serving paths to CONSUME this resolver is runtime
adoption (the AC-5/AC-9 tickets).

Run from the repo root::

    uv run python -m personal_agent.config.substrate --profile managed
    uv run python -m personal_agent.config.substrate --profile private --component postgres
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict
from rich.console import Console

from personal_agent.config.config_guard import (
    load_substrate_manifest,
    repo_root,
)

if TYPE_CHECKING:
    from personal_agent.config.settings import AppConfig

log = structlog.get_logger(__name__)

_stdout = Console()
_stderr = Console(stderr=True)

BackendKind = Literal["local", "managed"]


class SubstrateProfileError(Exception):
    """Raised when a substrate profile is not declared in ``config/substrate.yaml``."""


class SubstrateSourceError(Exception):
    """Raised when a component's ``source`` reference is malformed or unresolvable."""


class ResolvedBackend(BaseModel):
    """A single substrate component's resolved backend under a profile.

    Attributes:
        component: The D3 component key (e.g. ``"postgres"``).
        kind: The declared custody stance for the row — ``"local"`` or
            ``"managed"``. Recorded here; the AC-1/AC-3 guards enforce it later.
        source: The manifest ``source`` reference this was resolved from.
        target: The resolved backend target (URL/URI/endpoint), or ``None`` when
            the source is unconfigured (e.g. a ``managed_*`` field left unset) —
            a first-class "unconfigured managed backend" state, not an error.
    """

    model_config = ConfigDict(frozen=True)

    component: str
    kind: BackendKind
    source: str
    target: str | None


class SubstrateResolution(BaseModel):
    """The full per-profile resolution of every substrate component."""

    model_config = ConfigDict(frozen=True)

    profile: str
    backends: dict[str, ResolvedBackend]


def _resolve_setting(field: str, settings: AppConfig) -> str | None:
    if not hasattr(settings, field):
        raise SubstrateSourceError(
            f"source 'setting:{field}' names AppConfig field {field!r} which does not exist"
        )
    value = getattr(settings, field)
    return str(value) if value is not None else None


def _resolve_model_endpoint(role: str, settings: AppConfig, root: Path) -> str | None:
    """Resolve ``model_endpoint:<role>`` to the active model file's model-def endpoint.

    Reuses the ADR-0099 role matrix + model loader so there is one canonical
    role→endpoint path, not a second copy. Returns ``None`` when the model-def
    declares no ``endpoint`` override (the runtime consumer then falls back to
    ``settings.llm_base_url`` — a runtime-adoption detail, not this seam's).
    """
    from personal_agent.config.model_loader import (  # noqa: PLC0415 — avoid import cycle
        load_model_config,
        resolve_role_model_key,
    )

    try:
        key = resolve_role_model_key(role, config_path=settings.model_config_path, root=root)
        model_def = load_model_config(settings.model_config_path).models.get(key)
    except Exception as exc:  # noqa: BLE001 — a config error surfaces as an unresolved source
        raise SubstrateSourceError(
            f"source 'model_endpoint:{role}' could not be resolved: {exc}"
        ) from exc
    return model_def.endpoint if model_def is not None else None


def resolve_substrate(
    profile: str,
    *,
    settings: AppConfig | None = None,
    root: Path | None = None,
) -> SubstrateResolution:
    """Resolve every substrate component's backend for *profile* (ADR-0112 D3 / AC-2).

    Args:
        profile: A profile declared under ``config/substrate.yaml`` ``profiles:``
            (e.g. ``"private"``, ``"managed"``).
        settings: The ``AppConfig`` to resolve ``setting:`` / ``model_endpoint:``
            sources against. ``None`` uses the live ``settings`` singleton.
        root: Repo (or fixture) root containing ``config/``. Defaults to the real
            repo root; tests point this at a fixture.

    Returns:
        A ``SubstrateResolution`` mapping each declared component to its
        ``ResolvedBackend``.

    Raises:
        SubstrateProfileError: If *profile* is not declared in the manifest.
        SubstrateSourceError: If a component's ``source`` is malformed or its
            reference cannot be resolved.
    """
    resolved_root = root if root is not None else repo_root()
    manifest = load_substrate_manifest(resolved_root)
    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        declared = sorted(profiles) if isinstance(profiles, dict) else []
        raise SubstrateProfileError(
            f"substrate profile {profile!r} is not declared in config/substrate.yaml "
            f"(declared: {declared})"
        )

    rows = profiles[profile]
    if not isinstance(rows, dict):
        raise SubstrateSourceError(f"substrate profile {profile!r} is not a mapping of components")

    if settings is None:
        from personal_agent.config import settings as live_settings  # noqa: PLC0415

        settings = live_settings

    # First pass resolves everything except backed_by (which references a
    # sibling component's already-resolved target); second pass fills those.
    targets: dict[str, str | None] = {}
    rows_by_component: dict[str, dict[str, object]] = {}
    deferred: list[str] = []

    for component, row in rows.items():
        if not isinstance(row, dict):
            raise SubstrateSourceError(
                f"substrate profile {profile!r} component {component!r} is not a mapping"
            )
        rows_by_component[component] = row
        source = row.get("source")
        if not isinstance(source, str) or ":" not in source:
            raise SubstrateSourceError(
                f"substrate profile {profile!r} component {component!r}: source {source!r} "
                "must be '<kind>:<ref>'"
            )
        source_kind, ref = source.split(":", 1)
        if source_kind == "setting":
            targets[component] = _resolve_setting(ref, settings)
        elif source_kind == "model_endpoint":
            targets[component] = _resolve_model_endpoint(ref, settings, resolved_root)
        elif source_kind == "backed_by":
            deferred.append(component)
        else:
            raise SubstrateSourceError(
                f"substrate profile {profile!r} component {component!r}: unknown source kind "
                f"{source_kind!r} in {source!r}"
            )

    for component in deferred:
        source = str(rows_by_component[component]["source"])
        ref = source.split(":", 1)[1]
        if ref not in targets:
            raise SubstrateSourceError(
                f"substrate profile {profile!r} component {component!r}: source {source!r} "
                f"references component {ref!r} which this profile does not declare"
            )
        targets[component] = targets[ref]

    backends = {
        component: ResolvedBackend(
            component=component,
            kind=_coerce_kind(profile, component, row.get("kind")),
            source=str(row["source"]),
            target=targets[component],
        )
        for component, row in rows_by_component.items()
    }
    return SubstrateResolution(profile=profile, backends=backends)


def _coerce_kind(profile: str, component: str, kind: object) -> BackendKind:
    if kind == "local":
        return "local"
    if kind == "managed":
        return "managed"
    raise SubstrateSourceError(
        f"substrate profile {profile!r} component {component!r}: invalid kind {kind!r} "
        "(expected 'local' or 'managed')"
    )


def _format_resolution(resolution: SubstrateResolution) -> str:
    """Render a resolution as an aligned ``component  kind  target  (source)`` table."""
    lines = [f"profile: {resolution.profile}"]
    for component in sorted(resolution.backends):
        backend = resolution.backends[component]
        target = backend.target if backend.target is not None else "<unset>"
        lines.append(f"  {component:<14} {backend.kind:<8} {target}  ({backend.source})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the resolved substrate table; returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, help="Substrate profile (e.g. private, managed, dev, test)"
    )
    parser.add_argument(
        "--component",
        help="Print only this component's resolved target (one of the D3 components)",
    )
    args = parser.parse_args(argv)

    try:
        resolution = resolve_substrate(args.profile)
    except (SubstrateProfileError, SubstrateSourceError) as exc:
        _stderr.print(f"substrate-resolve: {exc}")
        return 1

    if args.component is not None:
        backend = resolution.backends.get(args.component)
        if backend is None:
            _stderr.print(
                f"substrate-resolve: profile {args.profile!r} declares no component "
                f"{args.component!r} (declared: {sorted(resolution.backends)})"
            )
            return 1
        _stdout.print(backend.target if backend.target is not None else "<unset>")
        return 0

    _stdout.print(_format_resolution(resolution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
