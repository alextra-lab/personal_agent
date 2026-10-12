"""FRE-1592 M1 — token count of each component of the production primary request.

Counts with the served model's own chat template and tokenizer: each request goes to llama.cpp
``/v1/chat/completions`` with ``max_tokens`` 1, and the count is ``usage.prompt_tokens``. (The
server's ``/tokenize`` and ``/apply-template`` answer 403 behind the slm_server router; master
approved this method on 2026-10-12.) Two properties of the count:

* it includes the generation prompt that the chat template adds after the last message, as every
  production call does;
* a cached prefix does not change it: ``prompt_tokens`` counts cached and evaluated tokens alike.

Each component's count is an ablation: the tokens of the full request minus the tokens of the same
request without that component, so template wrappers are included. A plain text (a catalog line,
a skill body) is counted as a user message, minus the count of an empty user message.

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

    uv run python -m scripts.eval.fre1592.inventory --capture <body.json> --store <counts.json>
        --expect-prompt-tokens 12377 [--dry-run]   (one line)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from personal_agent.orchestrator.prompts import (
    GROUNDING_CONTRACT_PROMPT,
    REASONING_DISCIPLINE_PROMPT,
    TOOL_USE_NATIVE_PROMPT,
    get_owner_identity,
)
from personal_agent.orchestrator.skills import assemble_skill_catalog, get_all_skills

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
    """Counts a request's prompt tokens with the server's template and tokenizer.

    Each count is saved in ``store`` under a hash of its request, so a stopped run resumes
    without sending a request again. Each call prints one progress line.
    """

    def __init__(self, url: str, model: str, store: Path, *, dry_run: bool = False) -> None:
        """Open a client for the chat-completions URL ``url`` and load the saved counts."""
        self._client = httpx.Client(timeout=300.0)
        self._url = url
        self._model = model
        self._store = store
        self._saved: dict[str, int] = json.loads(store.read_text()) if store.exists() else {}
        self._dry_run = dry_run
        self.calls = 0
        self._t0 = time.monotonic()

    def request(self, messages: Sequence[Message], tools: Sequence[object]) -> int:
        """Return ``usage.prompt_tokens`` of the request, generating one token.

        Args:
            messages: The chat messages.
            tools: The tool definitions; empty for none.

        Returns:
            The prompt tokens, from the store when the same request was counted before.

        Raises:
            httpx.HTTPError: On a transport error or a non-2xx reply.
        """
        payload: Body = {
            "model": self._model,
            "messages": list(messages),
            "max_tokens": 1,
            "stream": False,
            "cache_prompt": True,
        }
        if tools:
            payload["tools"] = list(tools)
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if key in self._saved:
            return self._saved[key]
        self.calls += 1
        size = len(json.dumps(payload))
        if self._dry_run:
            print(f"call {self.calls}: {size:,} chars", flush=True)
            return 0
        t = time.monotonic()
        r = self._client.post(self._url, json=payload)
        r.raise_for_status()
        n = int(r.json()["usage"]["prompt_tokens"])
        self._saved[key] = n
        self._store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._saved))
        os.replace(tmp, self._store)
        print(
            f"call {self.calls}: {n:,} tokens, {time.monotonic() - t:.1f} s "
            f"(elapsed {time.monotonic() - self._t0:.0f} s)",
            flush=True,
        )
        return n

    def text(self, content: str) -> int:
        """Return the tokens of a plain text: a user message minus an empty user message.

        Args:
            content: The text.

        Returns:
            Its token count, chat-template wrapper excluded.
        """
        empty = self.request([{"role": "user", "content": ""}], [])
        return self.request([{"role": "user", "content": content}], []) - empty


def _tool_name(tool: Mapping[str, object]) -> str:
    """Return the function name of a tool definition."""
    fn = tool.get("function")
    return str(fn.get("name")) if isinstance(fn, Mapping) else "?"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the inventory as Markdown tables.

    Args:
        argv: The command line; ``sys.argv`` when None.

    Returns:
        The exit code: 1 when the instrument check fails.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True, help="JSON file of saved counts")
    parser.add_argument("--url", default="http://127.0.0.1:8600/v1/chat/completions")
    parser.add_argument("--llama-model", default="unsloth/qwen3.8-flash-next")
    parser.add_argument("--expect-prompt-tokens", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="list the calls, send nothing")
    args = parser.parse_args(argv)

    body = json.loads(args.capture.read_text())
    body = body.get("body", body)
    messages: list[Message] = body["messages"]
    tools: list[dict[str, object]] = body["tools"]
    _check_deployment_context()
    parts = _components(str(messages[0]["content"]))
    count = Counter(args.url, args.llama_model, args.store, dry_run=args.dry_run)

    # The instrument check, then the whole production request: the only two large requests.
    as_captured = count.request(messages, tools)
    if (
        not args.dry_run
        and args.expect_prompt_tokens is not None
        and as_captured != args.expect_prompt_tokens
    ):
        print(f"instrument check FAILED: {as_captured} != {args.expect_prompt_tokens}")
        return 1
    prod_system: Message = {"role": "system", "content": _system(parts)}
    full = count.request([prod_system, *messages[1:]], tools)

    # Small requests from here on. Each holds one fixed user message, because a chat
    # template can refuse a request without one; it cancels in every difference. The tools
    # render as one block, so their total is (system + tools) - (system); each tool alone is
    # (stub + tool) - (stub).
    user: Message = {"role": "user", "content": "."}
    system_only = count.request([prod_system, user], [])
    tools_total = count.request([prod_system, user], tools) - system_only
    rows: list[tuple[str, int]] = [("tool definitions (all)", tools_total)]
    for name in parts:
        without = count.request([{"role": "system", "content": _system(parts, name)}, user], [])
        rows.append((name, system_only - without))
    stub: Message = {"role": "system", "content": "."}
    stub_n = count.request([stub, user], [])
    per_tool = [(_tool_name(t), count.request([stub, user], [t]) - stub_n) for t in tools]
    skills = get_all_skills()
    catalog = [
        (m.group(1), line)
        for line in parts["skill_catalog"].split("\n")
        if (m := re.match(r"- ([a-z0-9-]+): ", line))
    ]
    per_skill = [(name, count.text(line), count.text(skills[name].body)) for name, line in catalog]
    if args.dry_run:
        print(f"{count.calls} calls to send ({len(count._saved)} already saved)")
        return 0
    # system_only holds the "." user message; the difference below charges it to the system.
    rows.append(("conversation after the system message", full - system_only - tools_total))

    print(f"Capture: {args.capture.name}. Counted with {args.url}, max_tokens 1, prompt_tokens.")
    print(f"As captured: {as_captured:,} tokens. With the production head: {full:,} tokens.\n")
    print("| Component | Tokens | Share |\n|---|---:|---:|")
    for name, n in rows:
        print(f"| {name} | {n:,} | {n / full:.1%} |")
    print(f"| **whole request** | **{full:,}** | 100% |\n")
    alone = sum(n for _, n in per_tool)
    print(f"Tools one at a time sum to {alone:,}; together {tools_total:,}.")
    print("| Tool | Tokens alone |\n|---|---:|")
    for name, n in sorted(per_tool, key=lambda r: -r[1]):
        print(f"| {name} | {n:,} |")
    print("\n| Skill | Catalog line tokens | SKILL.md body tokens |\n|---|---:|---:|")
    for name, line_n, body_n in per_skill:
        print(f"| {name} | {line_n} | {body_n:,} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
