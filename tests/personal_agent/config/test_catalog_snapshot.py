"""Behaviour-preserving snapshot for the ADR-0121 T1 catalog refactor (FRE-916).

ADR-0121 §7 requires this refactor ship with a snapshot assertion: every role's
fully-resolved model definition is captured *before* the catalog is restructured
and asserted identical *after*. The failure mode being guarded is a role silently
resolving to a different model — the FRE-879 regression class.

A definition-only snapshot is **not sufficient**: model definitions can stay
byte-identical while live behaviour changes, because several consumers key off
the *role name* rather than the resolved definition. This module therefore
captures four dimensions:

1. **Resolution** — ``(catalog, profile, role) -> resolved key + full definition``.
2. **Concurrency** — which semaphore each role registers, and at what limit.
   (``LocalLLMClient`` registers by catalog key and acquires by ``ModelRole``
   value; re-keying the catalog can silently disconnect the two.)
3. **Timeouts** — the effective per-``ModelRole`` timeout. ``_role_timeouts`` is
   built from ``models.get(role.value)``; a miss falls back to a hardcoded
   default, which would silently drop ``primary`` from 600s to 60s.
4. **Pricing** — the ``litellm.model_cost`` entries the config registers, keyed
   by ``provider/id`` rather than by role.

Dimension 5 from the plan (substrate ``model_endpoint:<role>``) is covered by
dimension 1: ``substrate.py`` resolves it as
``load_model_config(...).models[resolve_role_model_key(role)].endpoint``, and
``endpoint`` is part of the captured definition.

The golden file is committed alongside this module. Regenerate deliberately —
never to make a red test green:

    python -m tests.personal_agent.config.test_catalog_snapshot --write
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from personal_agent.config.model_loader import (
    _load_model_config_at_path,
    _load_role_matrix,
    load_model_config,
    resolve_role_model_key,
)
from personal_agent.config.profile import (
    load_profile,
    resolve_model_key,
    set_current_profile,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN = Path(__file__).with_name("catalog_snapshot_golden.json")

#: The two model-definition files deployed today (config/model_roles.yaml
#: active_profiles). Collapsed to one catalog by FRE-916.
_CATALOGS: dict[str, Path] = {
    "local": _REPO_ROOT / "config" / "models.yaml",
    "cloud": _REPO_ROOT / "config" / "models.cloud.yaml",
}

#: Roles resolved through the ADR-0099 matrix (config/model_roles.yaml), whose
#: callers then enter via ``get_llm_client_for_key``.
_MATRIX_ROLES: tuple[str, ...] = (
    "entity_extraction",
    "captains_log",
    "insights",
    "compressor",
    "embedding",
    "reranker",
    "reranker_fallback",
)

#: Roles resolved through the ExecutionProfile (config/profile.py), whose
#: callers enter via ``get_llm_client``. These are the roles FRE-916 re-homes
#: onto Layer-3 bindings, so they are the highest-risk rows in the snapshot.
_PROFILE_ROLES: tuple[str, ...] = ("primary", "sub_agent", "artifact_builder")

#: ExecutionProfile states to capture. ``None`` is the no-profile path — the one
#: that returns the bare role name today and must keep resolving to the same
#: model once the catalog is keyed by deployment.
_PROFILES: tuple[str | None, ...] = (None, "local", "cloud")


def _clear_caches() -> None:
    """Drop the loader's lru_caches so each catalog is read fresh."""
    _load_model_config_at_path.cache_clear()
    _load_role_matrix.cache_clear()


def _definition_of(key: str, catalog_path: Path) -> dict[str, Any] | None:
    """Return the full definition for ``key``, or None when absent."""
    definition = load_model_config(catalog_path).models.get(key)
    return None if definition is None else definition.model_dump(mode="json")


def _capture_resolution() -> dict[str, Any]:
    """Capture ``(catalog, profile, role) -> resolved key + definition``."""
    out: dict[str, Any] = {}
    for catalog_name, catalog_path in _CATALOGS.items():
        for profile_name in _PROFILES:
            token = None
            if profile_name is not None:
                token = set_current_profile(
                    load_profile(profile_name, _REPO_ROOT / "config" / "profiles")
                )
            try:
                for role in _PROFILE_ROLES:
                    cell = f"{catalog_name}|{profile_name or 'none'}|{role}"
                    try:
                        key = resolve_model_key(role)
                        out[cell] = {"key": key, "definition": _definition_of(key, catalog_path)}
                    except Exception as exc:  # noqa: BLE001 — a raise IS the behaviour
                        out[cell] = {"raises": type(exc).__name__}

                for role in _MATRIX_ROLES:
                    cell = f"{catalog_name}|{profile_name or 'none'}|{role}"
                    try:
                        key = resolve_role_model_key(
                            role, config_path=catalog_path, root=_REPO_ROOT
                        )
                        out[cell] = {"key": key, "definition": _definition_of(key, catalog_path)}
                    except Exception as exc:  # noqa: BLE001 — a raise IS the behaviour
                        out[cell] = {"raises": type(exc).__name__}
            finally:
                if token is not None:
                    from personal_agent.config.profile import _current_profile

                    _current_profile.reset(token)
    return out


def _capture_concurrency_and_timeouts() -> dict[str, Any]:
    """Capture semaphore registration and effective per-ModelRole timeouts."""
    from personal_agent.llm_client.client import LocalLLMClient
    from personal_agent.llm_client.types import ModelRole

    out: dict[str, Any] = {}
    for catalog_name, catalog_path in _CATALOGS.items():
        _clear_caches()
        client = LocalLLMClient(model_config_path=catalog_path)
        out[f"{catalog_name}|concurrency"] = client._concurrency.get_status()
        out[f"{catalog_name}|timeouts"] = {
            role.value: client._role_timeouts[role] for role in ModelRole
        }
    return out


def _capture_pricing() -> dict[str, Any]:
    """Capture the litellm.model_cost entries each catalog registers."""
    from personal_agent.llm_client.pricing import register_model_pricing

    out: dict[str, Any] = {}
    for catalog_name, catalog_path in _CATALOGS.items():
        _clear_caches()
        captured: dict[str, Any] = {}

        def _capture(entries: dict[str, Any], _sink: dict[str, Any] = captured) -> None:
            _sink.update(entries)

        with patch("litellm.register_model", side_effect=_capture):
            register_model_pricing(load_model_config(catalog_path))
        out[catalog_name] = captured
    return out


def build_snapshot() -> dict[str, Any]:
    """Build the full four-dimension behaviour snapshot."""
    _clear_caches()
    snapshot = {
        "resolution": _capture_resolution(),
        "runtime": _capture_concurrency_and_timeouts(),
        "pricing": _capture_pricing(),
    }
    _clear_caches()
    return snapshot


@pytest.mark.skipif(
    not (_REPO_ROOT / "config" / "models.cloud.yaml").exists(),
    reason=(
        "Pre-FRE-916 snapshot: the two-catalog golden is only meaningful while "
        "config/models.cloud.yaml exists. Superseded by the post-refactor golden."
    ),
)
def test_catalog_behaviour_matches_golden() -> None:
    """Every role resolves, throttles, times out, and prices as it did on main."""
    assert _GOLDEN.exists(), (
        f"Golden snapshot missing at {_GOLDEN}. Generate it on an unmodified "
        "tree with: python -m tests.personal_agent.config.test_catalog_snapshot --write"
    )
    expected = json.loads(_GOLDEN.read_text())
    actual = build_snapshot()

    drift = sorted(
        cell
        for cell in set(expected["resolution"]) | set(actual["resolution"])
        if expected["resolution"].get(cell) != actual["resolution"].get(cell)
    )
    assert not drift, (
        "Model resolution changed for: "
        + ", ".join(drift)
        + ". Every difference must be an explicitly declared, reviewed delta "
        "(ADR-0121 §7) — never a silent side effect of the refactor."
    )
    assert actual["runtime"] == expected["runtime"], (
        "Concurrency registration or per-role timeouts changed. A role whose "
        "catalog key no longer matches its ModelRole value loses its semaphore "
        "and falls back to a hardcoded timeout."
    )
    assert actual["pricing"] == expected["pricing"], "Registered model pricing changed."


if __name__ == "__main__":
    _GOLDEN.write_text(json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n")
