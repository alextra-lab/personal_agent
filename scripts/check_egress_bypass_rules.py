"""Static bypass rule set for the outbound transport factory (ADR-0132 D2 / FRE-1147).

Runs the ast-grep rules in ``.ast-grep/rules/`` against ``src/personal_agent/``,
forbidding httpx client construction, module-level ``httpx.get/post/...`` calls,
and direct Anthropic/OpenAI SDK client construction outside
``personal_agent.security.create_guarded_http_client`` — every enumerated egress
seam must consult ``DomainGuard.check_url`` before a connection is formed.

Two exemptions, both load-bearing:

* ``security.py`` itself — the factory's own implementation, and
  ``DomainGuard``'s URLhaus feed fetch (the guard cannot gate its own bootstrap
  without recursion).
* ``ui/`` — the CLI's client to its own local backend (``:9000``), a sync
  ``httpx.Client`` in one file and an async one in the other, kept out as one
  CLI-to-own-backend concern rather than an ADR-enumerated egress seam
  (``DomainGuard.ensure_loaded()`` is async; a sync transport would need a
  separate bootstrap story this ticket does not add).

Usage:
    uv run python scripts/check_egress_bypass_rules.py

Exit code is non-zero if any rule fires.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "src" / "personal_agent"
EXEMPT_GLOBS = [
    "!src/personal_agent/security.py",
    "!src/personal_agent/ui/**",
]


def main() -> int:
    """Run the ast-grep egress bypass rules and forward their exit code."""
    cmd = [
        "ast-grep",
        "scan",
        *[g for glob in EXEMPT_GLOBS for g in ("--globs", glob)],
        str(TARGET),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
