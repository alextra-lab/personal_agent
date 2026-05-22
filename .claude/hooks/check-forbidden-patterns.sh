#!/bin/bash
# PreToolUse hook: block edits introducing forbidden patterns into src/personal_agent/.
# CLAUDE.md forbids print(), os.getenv(), bare except:, alembic.
# Exit 2 = block the tool call and surface the message to the model.

input=$(cat)

printf '%s' "$input" | python3 -c '
import json, re, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = d.get("tool_name", "")
if tool not in ("Edit", "Write"):
    sys.exit(0)

tin = d.get("tool_input", {}) or {}
path = tin.get("file_path", "") or ""

if "src/personal_agent/" not in path:
    sys.exit(0)

if "/tests/" in path or path.endswith("_test.py"):
    sys.exit(0)

if tool == "Write":
    payload = tin.get("content", "") or ""
else:
    payload = tin.get("new_string", "") or ""

PATTERNS = [
    (r"^\s*print\s*\(", "print() — use structlog with trace_id instead"),
    (r"\bos\.getenv\s*\(", "os.getenv() — use settings from personal_agent.config instead"),
    (r"^\s*except\s*:\s*$", "bare except: — use personal_agent.exceptions classes instead"),
    (r"^\s*(import|from)\s+alembic\b", "alembic — project does not use Alembic; schema goes in docker/postgres/init.sql + migrations/"),
]

violations = []
for line_no, line in enumerate(payload.splitlines(), start=1):
    if line.lstrip().startswith("#"):
        continue
    for pat, msg in PATTERNS:
        if re.search(pat, line):
            violations.append("  line %d: %s\n    >> %s" % (line_no, msg, line.rstrip()))
            break

if violations:
    sys.stderr.write(
        "Blocked: edit introduces forbidden pattern(s) in src/personal_agent/.\n"
        "File: " + path + "\n"
        + "\n".join(violations)
        + "\n\nSee CLAUDE.md Coding Standards. If intentional, edit the hook to exempt the path.\n"
    )
    sys.exit(2)

sys.exit(0)
'

exit $?
