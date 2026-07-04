#!/usr/bin/env python3
"""config-resolve CLI (ADR-0099 D2.2, stage 3, FRE-651).

Answers "which model does profile X use for role Y?" from committed files
alone — no running container. Reads the deployment-provenance manifest
(``config/deployment.yaml``) to find the profile's active model-definition
file, then resolves the role through the same matrix-backed loader every
runtime consumer uses (``config/model_roles.yaml``, ADR-0099 D1 stage 2).

Run from the repo root::

    uv run python -m personal_agent.config.resolve --profile cloud --role entity_extraction
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console

from personal_agent.config.config_guard import (
    DeploymentProfileError,
    load_deployment_manifest,
    model_config_path_for_profile,
    repo_root,
)
from personal_agent.config.model_loader import ModelRoleError, resolve_role_model_key

_stdout = Console()
_stderr = Console(stderr=True)


def resolve(profile: str, role: str) -> str:
    """Resolve *role*'s model key for *profile* from committed files only.

    Args:
        profile: A deployment profile declared in ``config/deployment.yaml`` (e.g. ``"cloud"``).
        role: A matrix role name declared in ``config/model_roles.yaml`` (e.g. ``"entity_extraction"``).

    Returns:
        The resolved model key.

    Raises:
        DeploymentProfileError: If *profile* is undeclared in ``config/deployment.yaml``.
        ModelRoleError: If *role* cannot be resolved for that profile's model-config path.
    """
    root = repo_root()
    manifest = load_deployment_manifest(root)
    config_path = model_config_path_for_profile(profile, manifest, root)
    return resolve_role_model_key(role, config_path=config_path, root=root)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the resolved model key; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, help="Deployment profile (e.g. cloud, local, eval)"
    )
    parser.add_argument("--role", required=True, help="Matrix role name (e.g. entity_extraction)")
    args = parser.parse_args(argv)

    try:
        model_key = resolve(args.profile, args.role)
    except (DeploymentProfileError, ModelRoleError) as exc:
        _stderr.print(f"config-resolve: {exc}")
        return 1

    _stdout.print(model_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
