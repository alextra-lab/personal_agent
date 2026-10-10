"""FRE-1564 AC-7 — the prompt tokens the added worker tools and descriptions cost.

Measures, from the code in this tree, three things and prints a Markdown table:

* per worker type: the tool declarations the type is offered on every worker call, before
  and after this change;
* the planner system prompt (one call per fan-out), before and after;
* the primary: ``query_telemetry`` is worker-only, so the primary's declarations do not change.

"Before" is the state of origin/main at 85ea515a, written down below. Tokens are estimated
with tiktoken ``cl100k_base`` (``llm_client.token_counter``), which is not the served model's
tokenizer. With ``--tokenize-url`` the script also counts with a llama.cpp server's
``/tokenize`` (tokenize only, no generation).

Run from the repo root:

    uv run python -m scripts.eval.fre1564.prompt_cost [--tokenize-url http://127.0.0.1:8600]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

import httpx

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.orchestrator import expansion_controller
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from personal_agent.tools import register_mvp_tools
from personal_agent.tools.notes_tools import notes_search_executor, notes_search_tool
from personal_agent.tools.registry import ToolRegistry

# origin/main at 85ea515a.
_BEFORE_TOOLS: Mapping[WorkerType, tuple[str, ...]] = {
    WorkerType.RESEARCHER: ("web_search",),
    WorkerType.GENERAL: ("run_python", "search_memory", "recall_personal_history"),
}
_BEFORE_DESCRIPTION: Mapping[WorkerType, str] = {
    WorkerType.RESEARCHER: (
        "Finds facts on the open web for one bounded question and reports "
        "them as data with sources and gaps"
    ),
    WorkerType.GENERAL: (
        "Answers a bounded question from its own knowledge, a computation, "
        "or the user's own memory, and reports in text"
    ),
}
_BEFORE_SURFACE = ("run_python", "web_search", "search_memory", "recall_personal_history")


def _registry() -> ToolRegistry:
    """Build the registry as production does: primitives on, R2 tools (notes_search) present."""
    settings.primitive_tools_enabled = True
    registry = ToolRegistry()
    register_mvp_tools(registry)
    if registry.get_tool("notes_search") is None:
        registry.register(notes_search_tool, notes_search_executor)
    return registry


def _declarations(registry: ToolRegistry, names: Sequence[str]) -> str:
    """Serialize the OpenAI-format declarations for ``names`` as a worker request carries them."""
    wanted = set(names)
    defs = [
        d
        for d in registry.get_tool_definitions_for_llm(mode=None, include_worker_only=True)
        if d["function"]["name"] in wanted
    ]
    return json.dumps(defs, separators=(",", ":"))


def _counter(tokenize_url: str | None) -> Callable[[str], int]:
    """Return a token counter: the llama.cpp server's when a URL is given, else tiktoken."""
    if tokenize_url is None:
        return estimate_tokens

    def count(text: str) -> int:
        response = httpx.post(
            f"{tokenize_url.rstrip('/')}/tokenize", json={"content": text}, timeout=30.0
        )
        response.raise_for_status()
        return len(response.json()["tokens"])

    return count


def _planner_prompt(surface: Sequence[str], *, before: bool) -> str:
    """Render the planner system prompt with the old or the new registry."""
    if not before:
        return expansion_controller._build_planner_system_prompt(list(surface))
    old = MappingProxyType(
        {
            t: replace(spec, description=_BEFORE_DESCRIPTION[t], tools=_BEFORE_TOOLS[t])
            for t, spec in WORKER_TYPES.items()
        }
    )
    original = expansion_controller.WORKER_TYPES
    expansion_controller.WORKER_TYPES = old  # type: ignore[misc]
    try:
        return expansion_controller._build_planner_system_prompt(list(surface))
    finally:
        expansion_controller.WORKER_TYPES = original  # type: ignore[misc]


def main() -> None:
    """Print the cost table."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--tokenize-url", default=None, help="llama.cpp server base URL")
    args = parser.parse_args()
    count = _counter(args.tokenize_url)
    method = "llama.cpp /tokenize" if args.tokenize_url else "tiktoken cl100k_base (estimate)"
    registry = _registry()

    print(f"Token counts by {method}.\n")
    print("| Worker call | Tool declarations before | after | Added |")
    print("|---|---:|---:|---:|")
    for worker_type, spec in WORKER_TYPES.items():
        before = count(_declarations(registry, _BEFORE_TOOLS[worker_type]))
        after = count(_declarations(registry, spec.tools))
        print(f"| `{worker_type.value}` | {before} | {after} | +{after - before} |")

    surface_after = sorted({t for spec in WORKER_TYPES.values() for t in spec.tools})
    p_before = count(_planner_prompt(_BEFORE_SURFACE, before=True))
    p_after = count(_planner_prompt(surface_after, before=False))
    print("\n| Planner call (once per fan-out) | before | after | Added |")
    print("|---|---:|---:|---:|")
    print(f"| system prompt | {p_before} | {p_after} | +{p_after - p_before} |")

    primary_defs = registry.get_tool_definitions_for_llm(Mode.NORMAL)
    primary_text = json.dumps(primary_defs, separators=(",", ":"))
    worker_only = _declarations(registry, ["query_telemetry"])
    in_primary = "query_telemetry" in {d["function"]["name"] for d in primary_defs}
    print("\n| Primary call (every turn) | tool declarations | `query_telemetry` offered | Added |")
    print("|---|---:|---|---:|")
    added = count(worker_only) if in_primary else 0
    print(f"| NORMAL mode | {count(primary_text)} | {'yes' if in_primary else 'no'} | +{added} |")


if __name__ == "__main__":
    main()
