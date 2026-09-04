"""LLM dispatch confinement — ADR-0141 AC-6 / FRE-1367.

Two rules: LocalLLMClient cannot be reintroduced anywhere, and litellm's
acompletion()/completion() are confined to llm_client/. Scoped across
src/, scripts/, experiments/, tests/ — the ticket's full "executable code"
surface (grepped: zero real call sites exist outside llm_client/ once the
two eval-script bypasses are migrated, so this scope has no false positives).

The two rule files live in .ast-grep/llm-dispatch-rules/, NOT .ast-grep/rules/
— that directory is in sgconfig.yml's ruleDirs and is auto-loaded by every
bare `ast-grep scan` elsewhere (notably check_egress_bypass_rules.py), which
would otherwise flag litellm_client.py's own two legitimate dispatch calls
(reproduced and confirmed during planning: dropping a matching rule into
.ast-grep/rules/ made the egress-bypass scan fail on litellm_client.py:1017
and :1520). `ast-grep scan --rule <path>` scopes exclusively to the one named
rule regardless of sgconfig.yml (also verified), so this script's own
invocations are unaffected by which directory the rule files live in.

Usage:
    uv run python scripts/check_llm_dispatch_confinement.py

Exit code is non-zero if either rule fires.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["src/personal_agent", "scripts", "experiments", "tests"]
RULES_DIR = REPO_ROOT / ".ast-grep" / "llm-dispatch-rules"
TOMBSTONE_RULE = RULES_DIR / "no-local-llm-client.yml"
DISPATCH_RULE = RULES_DIR / "no-raw-litellm-dispatch.yml"


def _scan(rule: Path, globs: list[str] | None = None) -> int:
    cmd = ["ast-grep", "scan", "--rule", str(rule)]
    for g in globs or []:
        cmd += ["--globs", g]
    cmd += TARGETS
    return subprocess.run(cmd, cwd=REPO_ROOT, check=False).returncode


def main() -> int:
    """Run both confinement rules and forward the combined exit code."""
    rc1 = _scan(TOMBSTONE_RULE)
    rc2 = _scan(DISPATCH_RULE, globs=["!**/llm_client/**"])
    return rc1 or rc2


if __name__ == "__main__":
    sys.exit(main())
