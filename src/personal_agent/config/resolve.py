#!/usr/bin/env python3
"""config-resolve CLI (ADR-0099 D2.2, stage 3, FRE-651; ADR-0121, FRE-926).

Answers "which model does role Y use?" from committed files alone — no running
container. Resolves the role through :func:`~personal_agent.config.model_loader.resolve_role_target`,
the Layer-3-bindings-backed resolver (``config/model_roles.yaml``'s ``bindings:``
block merged into ``config/models.yaml``, ADR-0121) that the client factory uses
at runtime. This answers for every bound role, whether or not it is also
declared in the legacy ``roles:`` matrix — ``sub_agent``, ``artifact_builder``,
and ``vision`` live only in ``bindings:`` (FRE-926, following FRE-920's ADR-0121
T5 matrix cleanup).

``--profile`` was removed in FRE-916 phase 2 (ADR-0121): it selected which
model-definition file to read, and there is now exactly one. Role assignment no
longer varies by deployment environment, so the question the flag answered can no
longer have two answers.

Run from the repo root::

    uv run python -m personal_agent.config.resolve --role entity_extraction
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console

from personal_agent.config.model_loader import (
    ModelRoleError,
    load_model_config,
    resolve_role_target,
)

_stdout = Console()
_stderr = Console(stderr=True)


def resolve(role: str) -> str:
    """Resolve *role*'s deployment key from committed files only.

    Args:
        role: A role name bound in ``config/model_roles.yaml``'s ``bindings:``
            block (e.g. ``"entity_extraction"``, ``"sub_agent"``,
            ``"artifact_builder"``, ``"vision"``).

    Returns:
        The resolved deployment key — a key in ``config/models.yaml``'s
        ``models:`` mapping.

    Raises:
        ModelRoleError: If *role* has no binding. Checked directly against
            ``config.roles`` rather than trusting :func:`resolve_role_target`
            alone — that resolver falls back to treating an unbound name as a
            literal deployment key, which would let a bare deployment key
            (e.g. ``"claude_haiku"``) silently answer as though it were a role.
    """
    config = load_model_config()
    if role not in config.roles:
        raise ModelRoleError(f"role {role!r} is not bound in config/model_roles.yaml bindings:")
    deployment_key, _ = resolve_role_target(role, config=config)
    return deployment_key


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the resolved deployment key; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, help="Role name (e.g. entity_extraction)")
    args = parser.parse_args(argv)

    try:
        model_key = resolve(args.role)
    except ModelRoleError as exc:
        _stderr.print(f"config-resolve: {exc}")
        return 1

    _stdout.print(model_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
