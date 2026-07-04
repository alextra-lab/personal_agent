#!/usr/bin/env python3
"""CLI for the cross-config guard (ADR-0099 D1/D4, FRE-649 — stage 1).

Thin wrapper over ``personal_agent.config.config_guard`` — see that module's
docstring for what each check does. This script's own exit code is non-zero
on ANY finding, safety or policy (CI/pre-commit is the universal gate per
ADR-0099 D4); the tiered *startup* hook in ``settings.py`` is softer, hard-
failing only on safety findings and warning-loud on policy ones.

Any suppression must be a considered exception, not a rubber stamp — append::

    # fre-649-allow: <reason>

Run from the repo root::

    uv run python scripts/check_config.py
    uv run python scripts/check_config.py --root tests/personal_agent/config/fixtures/divergent_forbidden_role
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from personal_agent.config.config_guard import repo_root, run_all_checks


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code (0 = clean)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=repo_root(),
        help="Repo (or fixture) root to check. Defaults to the real repo root.",
    )
    args = parser.parse_args(argv)

    findings = run_all_checks(args.root.resolve())
    if not findings:
        print("check_config: clean")
        return 0

    print("check_config: violations found:\n", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nTo suppress a deliberate, reviewed exception, append  # fre-649-allow: <reason>  "
        "to the line (committed-secret check only).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
