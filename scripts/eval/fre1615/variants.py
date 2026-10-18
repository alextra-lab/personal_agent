"""FRE-1615 Phase 0: build the variant bodies of each captured task and count their true tokens.

- ``A``: the captured body (current prompt, all tools).
- ``B``: ``variants/B_system.md`` as the system message, all tools.
- ``C``: ``B``, with the tools of the profile that ``profiles.yaml`` gives the task's Stage 4 TaskType.
- ``Cp`` (C-prime): ``B``, with the static ``used_30d`` tools of ``profiles.yaml``.

The report gives each variant's token parts, and for ``C`` whether the profile holds every tool group the
task ``expects`` (``suite.yaml``). A task whose profile misses a group must expand on that turn.

    uv run python -m scripts.eval.fre1615.variants --run-dir <run> --props <props.json> --tokenizer <tokenizer.json>
"""

from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from scripts.eval.fre1615.tokens import Counter, JsonDict, load_counter

HERE = Path(__file__).parent
B_SYSTEM = HERE / "variants" / "B_system.md"
PROFILES = HERE / "variants" / "profiles.yaml"


def with_system(body: Mapping[str, object], system: str) -> JsonDict:
    """Return a copy of ``body`` whose first (system) message holds ``system``."""
    out = copy.deepcopy(dict(body))
    messages = out["messages"]
    assert isinstance(messages, list) and messages[0]["role"] == "system"
    messages[0] = {**messages[0], "content": system}
    return out


def with_tools(body: Mapping[str, object], names: set[str]) -> JsonDict:
    """Return a copy of ``body`` that keeps only the tools in ``names``, in their order."""
    out = copy.deepcopy(dict(body))
    tools = out["tools"]
    assert isinstance(tools, list)
    out["tools"] = [t for t in tools if t["function"]["name"] in names]
    return out


def profile_tools(task_type: str, config: Mapping[str, object]) -> tuple[list[str], set[str]]:
    """Return the groups and the tool names of a TaskType's profile."""
    groups = config["groups"]
    profiles = config["profiles"]
    assert isinstance(groups, dict) and isinstance(profiles, dict)
    chosen = list(profiles[task_type])
    return chosen, {name for g in chosen for name in groups[g]}


def report(run_dir: Path, counter: Counter) -> dict[str, object]:
    """Build the variant report over every captured task of ``run_dir``."""
    config = yaml.safe_load(PROFILES.read_text())
    b_system = B_SYSTEM.read_text().strip()
    rows = []
    for path in sorted((run_dir / "captured").glob("*.json")):
        record = json.loads(path.read_text())
        task, body = record["task"], record["body"]
        task_type = record["intent"]["task_type"]
        groups, names = profile_tools(task_type, config)
        b = with_system(body, b_system)
        c = with_tools(b, names)
        cp = with_tools(b, set(config["used_30d"]))
        missing = [g for g in task.get("expects", []) if g not in groups]
        rows.append(
            {
                "id": task["id"],
                "task_type": task_type,
                "A": counter.breakdown(body),
                "B": counter.breakdown(b),
                "C": counter.breakdown(c),
                "Cp": counter.breakdown(cp),
                "C_tools": len(c["tools"]),
                "C_missing_groups": missing,
            }
        )
    summary: dict[str, object] = {}
    for v in ("A", "B", "C", "Cp"):
        totals = [r[v]["total"] for r in rows]  # type: ignore[index]
        summary[v] = {
            "median_total": statistics.median(totals),
            "min": min(totals),
            "max": max(totals),
        }
    summary["C_tasks_missing_a_group"] = [r["id"] for r in rows if r["C_missing_groups"]]
    return {"rows": rows, "summary": summary}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--props", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    args = parser.parse_args(argv)
    out = report(args.run_dir, load_counter(args.props, args.tokenizer))
    (args.run_dir / "variants.json").write_text(json.dumps(out, indent=1))
    for r in out["rows"]:  # type: ignore[attr-defined]
        print(
            f"{r['id']:4s} {r['task_type']:15s} A {r['A']['total']:6d}  B {r['B']['total']:6d}  "
            f"C {r['C']['total']:6d} ({r['C_tools']:2d} tools)  Cp {r['Cp']['total']:6d}  "
            f"missing {r['C_missing_groups']}"
        )
    print(json.dumps(out["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
