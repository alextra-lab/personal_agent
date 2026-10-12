"""FRE-1592 M1 — token count of each component of the production primary request.

Counts with the served model's own chat template and tokenizer: llama.cpp ``/apply-template``
renders the request (messages and tools), and ``/tokenize`` counts the rendered prompt. No
generation. Each component's count is an ablation: the tokens of the full request minus the
tokens of the same request without that component, so template wrappers are included.

Input is a primary request captured by ``scripts/eval/fre1537/capture.py`` (FRE-1590 captures
are bodies of that shape). The capture gateway has no memory graph and does not run as
production, so the script adds the two sections that production adds: the deployment context
(``orchestrator/executor.py``, production only) and the operator stanza (built by
``prompts.get_owner_identity`` with the placeholder name ``Owner``).

Two checks fail the run instead of printing a wrong table:

* the system prompt rebuilt from the code constants must equal the captured one;
* with ``--expect-prompt-tokens``, the count of the captured body as it is must equal the
  ``prompt_tokens`` that llama.cpp reported when the same body was replayed.

Run from the repo root:

    uv run python -m scripts.eval.fre1592.inventory --capture <body.json> --expect-prompt-tokens 12377
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from personal_agent.orchestrator.prompts import (
    GROUNDING_CONTRACT_PROMPT,
    REASONING_DISCIPLINE_PROMPT,
    TOOL_USE_NATIVE_PROMPT,
    get_owner_identity,
)
from personal_agent.orchestrator.skills import SKILLS_DIR, assemble_skill_catalog

_EXECUTOR = Path(__file__).resolve().parents[3] / "src/personal_agent/orchestrator/executor.py"

# orchestrator/executor.py, the production-only deployment context. Checked against the source
# file at run time, so a change there stops the script.
DEPLOYMENT_CONTEXT = (
    "## Deployment Context\n"
    "You are running inside a Docker container on a cloud VPS.\n"
    "- App code is at `/app` — the host's repo mount point is NOT accessible from here\n"
    "- Configuration is injected as environment variables at startup; there is no `.env` file "
    "inside the container\n"
    "- Do NOT search for files at host filesystem paths (the host's repo checkout or home "
    "directory) — they do not exist inside the container\n"
    "- All backend services are reachable via Docker internal DNS:\n"
    "    postgres:5432  |  neo4j:7687 (bolt) / neo4j:7474 (HTTP)  |  elasticsearch:9200\n"
    "    redis:6379"
)

Body = dict[str, object]
Message = dict[str, object]


class _StubMemory:
    """Stands in for MemoryService: the stanza needs only a node with a name."""

    async def get_or_provision_user_person(self, **_: object) -> dict[str, str]:
        return {"name": "Owner"}


def _check_deployment_context() -> None:
    """Fail if a line of DEPLOYMENT_CONTEXT is missing from the executor source."""
    source = _EXECUTOR.read_text()
    for line in DEPLOYMENT_CONTEXT.split("\n"):
        fragment = line.split("—")[0].strip()[:40]
        if fragment and fragment not in source:
            raise SystemExit(f"deployment context drifted from executor.py: {fragment!r}")


def _operator_stanza() -> str:
    """Build the operator stanza with the production code and a placeholder name."""
    from uuid import uuid4

    identity = asyncio.run(
        get_owner_identity(_StubMemory(), uuid4(), "owner@example.invalid", "Owner")  # type: ignore[arg-type]
    )
    return identity.stanza


def _components(captured_system: str) -> dict[str, str]:
    """Split the captured system prompt into its components and check the split."""
    marker = "\n\n## Grounding"
    head, _, _ = captured_system.partition(marker)
    tool_prompt = TOOL_USE_NATIVE_PROMPT
    if not head.startswith(tool_prompt):
        raise SystemExit("captured system prompt does not start with TOOL_USE_NATIVE_PROMPT")
    awareness = head[len(tool_prompt) :].strip("\n")
    catalog = assemble_skill_catalog()
    rebuilt = (
        f"{tool_prompt}\n\n{awareness}\n\n"
        f"{GROUNDING_CONTRACT_PROMPT}\n{REASONING_DISCIPLINE_PROMPT}\n\n{catalog}"
    )
    if rebuilt != captured_system:
        raise SystemExit("rebuilt system prompt differs from the capture: the code changed")
    return {
        "tool_use_rules": tool_prompt,
        "tool_awareness": awareness,
        "grounding_contract": GROUNDING_CONTRACT_PROMPT,
        "reasoning_discipline": REASONING_DISCIPLINE_PROMPT,
        "deployment_context": DEPLOYMENT_CONTEXT,
        "operator_stanza": _operator_stanza(),
        "skill_catalog": catalog,
    }


def _system(parts: Mapping[str, str], drop: str | None = None) -> str:
    """Assemble the production system prompt in executor order, without ``drop``."""

    def keep(name: str) -> str:
        return "" if name == drop else parts[name]

    head = "\n\n".join(p for p in (keep("tool_use_rules"), keep("tool_awareness")) if p)
    rules = "\n".join(p for p in (keep("grounding_contract"), keep("reasoning_discipline")) if p)
    base = "\n\n".join(
        p
        for p in (rules, keep("deployment_context"), keep("operator_stanza"), keep("skill_catalog"))
        if p
    )
    return f"{head}\n\n{base}" if head else base


class Counter:
    """Counts a request's prompt tokens with the server's template and tokenizer."""

    def __init__(self, url: str) -> None:
        """Open a client for the llama.cpp server at ``url``."""
        self._client = httpx.Client(base_url=url.rstrip("/"), timeout=60.0)

    def text(self, content: str) -> int:
        """Return the token count of a plain text."""
        r = self._client.post("/tokenize", json={"content": content})
        r.raise_for_status()
        return len(r.json()["tokens"])

    def request(self, messages: Sequence[Message], tools: Sequence[object]) -> int:
        """Return the token count of the prompt that the chat template renders."""
        payload: Body = {"messages": list(messages)}
        if tools:
            payload["tools"] = list(tools)
        r = self._client.post("/apply-template", json=payload)
        r.raise_for_status()
        return self.text(r.json()["prompt"])


def _with_system(messages: Sequence[Message], system: str) -> list[Message]:
    return [{**messages[0], "content": system}, *messages[1:]]


def main(argv: Sequence[str] | None = None) -> int:
    """Print the inventory as Markdown tables."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8600")
    parser.add_argument("--expect-prompt-tokens", type=int, default=None)
    args = parser.parse_args(argv)

    body = json.loads(args.capture.read_text())
    body = body.get("body", body)
    messages: list[Message] = body["messages"]
    tools: list[dict[str, object]] = body["tools"]
    _check_deployment_context()
    parts = _components(str(messages[0]["content"]))
    count = Counter(args.url)

    as_captured = count.request(messages, tools)
    if args.expect_prompt_tokens is not None and as_captured != args.expect_prompt_tokens:
        print(f"instrument check FAILED: {as_captured} != {args.expect_prompt_tokens}")
        return 1

    prod = _with_system(messages, _system(parts))
    full = count.request(prod, tools)
    no_tools = count.request(prod, [])
    rows: list[tuple[str, int]] = [("tool definitions (all)", full - no_tools)]
    for name in parts:
        rows.append(
            (name, full - count.request(_with_system(messages, _system(parts, name)), tools))
        )
    system_only = count.request([prod[0]], [])
    rows.append(("conversation after the system message", no_tools - system_only))

    print(f"Capture: {args.capture.name}. Counted with {args.url} /apply-template + /tokenize.")
    print(f"As captured: {as_captured} tokens. Production head added: {full} tokens.\n")
    print("| Component | Tokens | Share |\n|---|---:|---:|")
    for name, n in rows:
        print(f"| {name} | {n:,} | {n / full:.1%} |")
    print(f"| **whole request** | **{full:,}** | 100% |\n")

    print("| Tool | Tokens |\n|---|---:|")
    per_tool = []
    for i, tool in enumerate(tools):
        fn = tool["function"]
        name = fn["name"] if isinstance(fn, dict) else str(fn)
        per_tool.append((name, full - count.request(prod, tools[:i] + tools[i + 1 :])))
    for name, n in sorted(per_tool, key=lambda r: -r[1]):
        print(f"| {name} | {n:,} |")

    print("\n| Skill | Catalog line tokens | SKILL.md body tokens |\n|---|---:|---:|")
    for line in parts["skill_catalog"].split("\n"):
        m = re.match(r"- ([a-z0-9-]+): ", line)
        if m:
            skill_md = SKILLS_DIR / m.group(1) / "SKILL.md"
            print(f"| {m.group(1)} | {count.text(line)} | {count.text(skill_md.read_text()):,} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
