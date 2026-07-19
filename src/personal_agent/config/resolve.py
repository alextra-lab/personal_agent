#!/usr/bin/env python3
"""config-resolve CLI (ADR-0099 D2.2, stage 3, FRE-651).

Answers "which model does role Y use?" from committed files alone — no running
container. Resolves the role through the same matrix-backed loader every runtime
consumer uses (``config/model_roles.yaml``, ADR-0099 D1 stage 2) against the
single deployment catalog.

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

from personal_agent.config.config_guard import repo_root
from personal_agent.config.model_loader import ModelRoleError, resolve_role_model_key

_stdout = Console()
_stderr = Console(stderr=True)


def resolve(role: str) -> str:
    """Resolve *role*'s model key from committed files only.

    Args:
        role: A matrix role name declared in ``config/model_roles.yaml`` (e.g. ``"entity_extraction"``).

    Returns:
        The resolved model key.

    Raises:
        ModelRoleError: If *role* cannot be resolved against the deployment catalog.
    """
    return resolve_role_model_key(role, root=repo_root())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the resolved model key; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, help="Matrix role name (e.g. entity_extraction)")
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
