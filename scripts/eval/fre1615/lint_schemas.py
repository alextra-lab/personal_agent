"""FRE-1615 Phase 0: lint the tool schemas of a captured primary request. It lists findings and fixes nothing.

Rules:

- ``integer-as-number``: a ``number`` parameter that counts or indexes something (its name or its description
  says limit, count, days, lines, characters, tokens, seconds, priority, or a whole-number range).
- ``array-without-items``: an ``array`` parameter with no ``items`` schema.
- ``prose-enum``: a ``string`` parameter whose description lists its allowed values, with no ``enum``.
- ``prose-range``: a ``number`` parameter whose description gives a range, with no ``minimum`` or ``maximum``.

    uv run python -m scripts.eval.fre1615.lint_schemas --body <captured/<id>.json> [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

_INTEGER_NAME = re.compile(
    r"(^|_)(limit|k|days|days_ago|offset|lines|tail_lines|max_\w+|tokens|priority|\w*seconds|count|n)$"
)
_INTEGER_TEXT = re.compile(
    r"\b(number of|how many|max(imum)? (results|turns|characters|documentation tokens|lines)|line number|"
    r"days|seconds|priority)\b|\d\s*(\.\.|-|–)\s*\d",
    re.I,
)
_RANGE = re.compile(r"\d\s*(\.\.|-|–)\s*\d|\bmax(imum)?\s+[\d,]+|\bmin\s+\d", re.I)
# A closed list: "Options:", "Must be one of", "Valid values", or quoted values joined by "or".
_ENUM_TEXT = re.compile(
    r"must be one of|options?:|valid values:?|'[^']+'(\s*\([^)]*\))?[^']{0,40}\bor\s+'",
    re.I,
)
# An open list of examples is not an enum.
_EXAMPLES = re.compile(r"\be\.g\.|\bexamples?:", re.I)


@dataclass(frozen=True)
class Finding:
    """One lint finding.

    Attributes:
        tool: The tool name.
        parameter: The parameter name.
        rule: The rule that fired.
        detail: The description text that made it fire, shortened.
    """

    tool: str
    parameter: str
    rule: str
    detail: str


def lint_tool(tool: Mapping[str, object]) -> list[Finding]:
    """Return the findings of one OpenAI-format tool definition."""
    function = tool["function"]
    assert isinstance(function, Mapping)
    name = str(function["name"])
    properties = (function.get("parameters") or {}).get("properties") or {}  # type: ignore[union-attr]
    findings: list[Finding] = []
    for param, schema in properties.items():
        kind = schema.get("type")
        text = str(schema.get("description", ""))
        short = text[:140]
        if kind == "number" and (_INTEGER_NAME.search(param) or _INTEGER_TEXT.search(text)):
            findings.append(Finding(name, param, "integer-as-number", short))
        if (
            kind in ("number", "integer")
            and _RANGE.search(text)
            and not ("minimum" in schema or "maximum" in schema)
        ):
            findings.append(Finding(name, param, "prose-range", short))
        if kind == "array" and "items" not in schema:
            findings.append(Finding(name, param, "array-without-items", short))
        closed = _ENUM_TEXT.search(text) and not _EXAMPLES.search(text)
        if kind in ("string", "array") and "enum" not in schema and closed:
            findings.append(Finding(name, param, "prose-enum", short))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    record = json.loads(args.body.read_text())
    body = record.get("body", record)
    findings = [f for tool in body["tools"] for f in lint_tool(tool)]
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=1))
    else:
        for f in findings:
            print(f"{f.rule:20s} {f.tool}.{f.parameter}: {f.detail}")
        counts: dict[str, int] = {}
        for f in findings:
            counts[f.rule] = counts.get(f.rule, 0) + 1
        print("totals:", counts, "tools:", len(body["tools"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
