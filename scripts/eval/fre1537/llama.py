"""FRE-1511 / ADR-0154 D7: requests to the llama.cpp server, and the planner and primary bodies.

Every body is built from the primary request that the capture step saved, so the sampling is the
production sampling. Nothing here executes a tool.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from scripts.eval.fre1537 import render
from scripts.eval.fre1537.common import RunPaths

DEFAULT_URL = "http://127.0.0.1:8600/v1/chat/completions"
DEFAULT_MODEL = "unsloth/qwen3.8-flash-next"
SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
    "cache_prompt",
)
REFERENCE_LABEL = "boiler_decline"


@dataclass(frozen=True)
class PlannerMode:
    """A planner mode: the extra request parameters that set how the planner call thinks.

    Attributes:
        name: The mode name, used as the default tag of a run.
        params: Request-body parameters added to the planner call.
    """

    name: str
    params: Mapping[str, object]


MODES: dict[str, PlannerMode] = {
    # ADR-0154 D4: thinking off, the default sampling of the primary.
    "thinking_off": PlannerMode(
        "thinking_off", {"chat_template_kwargs": {"enable_thinking": False}}
    ),
    # The server default: thinking on, which is the production planner setting today.
    "server_default": PlannerMode("server_default", {}),
}
_THINKING_OFF = MODES["thinking_off"].params

# FRE-1541: the `planner` mode is read from the catalog that ships, so the probe qualifies the
# parameters the gateway will send and not a copy of them.
CATALOG = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
CATALOG_DEPLOYMENT = "qwen3.8-flash-next"
MODE_NAMES = (*MODES, "planner")
_SAMPLER_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repeat_penalty",
)


def catalog_planner_mode(
    path: Path | None = None, deployment: str = CATALOG_DEPLOYMENT
) -> PlannerMode:
    """Read the ``planner`` mode of a deployment from ``config/models.yaml``.

    The probe replays the captured primary's sampling in every arm, so it can measure a thinking
    switch and nothing else. A mode that changes a sampler therefore cannot be qualified by it.

    Args:
        path: The catalog file. Default is the repo's ``config/models.yaml``.
        deployment: The catalog key of the deployment.

    Returns:
        A mode named ``planner``. Its parameters carry ``enable_thinking: false`` only when the
        catalog declares it. A mode that leaves thinking on carries none, so the scorer's
        0-reasoning threshold fails it.

    Raises:
        ValueError: The deployment declares no ``planner`` mode, or its sampling differs from
            its default mode's.
    """
    entry = yaml.safe_load((path or CATALOG).read_text())["models"][deployment]
    modes = entry["modes"]
    declared = modes.get("planner")
    if declared is None:
        raise ValueError(
            f"{deployment!r} declares no `planner` mode in the catalog. Add it, then run the probe "
            "on it; ADR-0154 D7 admits a mode only with a passing result."
        )
    default = modes[entry["default_mode"]]
    changed = [k for k in _SAMPLER_KEYS if declared.get(k) != default.get(k)]
    if changed:
        raise ValueError(
            f"the `planner` mode of {deployment!r} changes {', '.join(changed)} against its default "
            "mode. The probe replays the captured primary's sampling, so it cannot qualify that."
        )
    thinking_off = declared.get("enable_thinking") is False
    return PlannerMode(
        "planner", {"chat_template_kwargs": {"enable_thinking": False}} if thinking_off else {}
    )


def resolve_mode(name: str) -> PlannerMode:
    """Return the planner mode a command-line name selects.

    Args:
        name: One of :data:`MODE_NAMES`.

    Returns:
        The fixed mode of that name, or the catalog's ``planner`` mode.
    """
    return catalog_planner_mode() if name == "planner" else MODES[name]


@dataclass(frozen=True)
class Inputs:
    """What a replay needs: the rendered prompts and the captured primary requests.

    Attributes:
        prompts: The content of ``prompts.json``.
        captured: Captured primary request records, by fixture label.
    """

    prompts: Mapping[str, object]
    captured: Mapping[str, Mapping[str, object]]

    @property
    def system(self) -> str:
        """Return the design A planner system prompt."""
        return str(self.prompts["system"])

    def fixture(self, label: str) -> Mapping[str, object]:
        """Return the rendered prompt entry of one fixture."""
        fixtures = self.prompts["fixtures"]
        assert isinstance(fixtures, Mapping)
        entry = fixtures[label]
        assert isinstance(entry, Mapping)
        return entry

    def body(self, label: str) -> Mapping[str, object]:
        """Return the captured primary request body of one fixture."""
        body = self.captured[label]["body"]
        assert isinstance(body, Mapping)
        return body


def load_inputs(paths: RunPaths) -> Inputs:
    """Load ``prompts.json`` and every captured request of a run directory.

    Args:
        paths: The run paths.

    Returns:
        The inputs.

    Raises:
        SystemExit: If the prompts file or the captured requests are missing.
    """
    if not paths.prompts.exists():
        raise SystemExit(f"{paths.prompts} is missing: run the capture and render steps first")
    captured = {p.stem: json.loads(p.read_text()) for p in sorted(paths.captured.glob("*.json"))}
    if not captured:
        raise SystemExit(f"no captured requests in {paths.captured}: run the capture step first")
    return Inputs(prompts=json.loads(paths.prompts.read_text()), captured=captured)


def reference_label(inputs: Inputs) -> str:
    """Return the fixture whose sampling and tools the long-history arm borrows."""
    return REFERENCE_LABEL if REFERENCE_LABEL in inputs.captured else next(iter(inputs.captured))


def sampling(body: Mapping[str, object]) -> dict[str, object]:
    """Return the sampling parameters of a captured primary request."""
    return {k: body[k] for k in SAMPLING_KEYS if k in body}


def planner_request(
    system: str,
    user: str,
    sampling_source: Mapping[str, object],
    mode: PlannerMode,
    max_tokens: int,
    digest: str | None = None,
) -> dict[str, object]:
    """Build a design A planner request body.

    Args:
        system: The planner system prompt.
        user: The planner user message.
        sampling_source: A captured primary request body, for the sampling.
        mode: The planner mode.
        max_tokens: The completion budget.
        digest: Optional memory digest lines. Since FRE-1360 they follow the user message as
            the production ``memory_recall`` tool result.

    Returns:
        The request body.
    """
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            *render.digest_exchange(digest),
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        **sampling(sampling_source),
        **mode.params,
    }


def planner_user(inputs: Inputs, label: str) -> str:
    """Return the planner user message of one fixture, rendered inside the gateway.

    A digest run reuses it: since FRE-1360 the digest is not in the user message.

    Args:
        inputs: The run inputs.
        label: Fixture label.

    Returns:
        The user message.
    """
    return str(inputs.fixture(label)["user"])


def planner_body(
    inputs: Inputs, label: str, mode: PlannerMode, digest: str | None = None
) -> dict[str, object]:
    """Build the design A planner request of one fixture.

    Args:
        inputs: The run inputs.
        label: Fixture label.
        mode: The planner mode.
        digest: Optional memory digest lines, carried as a tool result (FRE-1360).

    Returns:
        The request body.
    """
    return planner_request(
        inputs.system, planner_user(inputs, label), inputs.body(label), mode, 16384, digest
    )


def primary_body(inputs: Inputs, label: str, max_tokens: int | None = None) -> dict[str, object]:
    """Rebuild the primary request of a fixture from its captured body."""
    src = inputs.body(label)
    body: dict[str, object] = {
        "messages": src["messages"],
        "tools": src["tools"],
        "tool_choice": src.get("tool_choice", "auto"),
        "parallel_tool_calls": src.get("parallel_tool_calls", True),
        **sampling(src),
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    return body


def prime_body(inputs: Inputs, label: str) -> dict[str, object]:
    """Build the request that primes the previous turn's prefix: the request without its last user message."""
    src = inputs.body(label)
    messages = list(src["messages"])  # type: ignore[call-overload]
    body: dict[str, object] = {
        "messages": messages[:-1],
        "tools": src["tools"],
        "max_tokens": 1,
        **sampling(src),
        **_THINKING_OFF,
    }
    prefix = body["messages"]
    assert isinstance(prefix, list)
    if prefix[-1]["role"] == "system":  # a single-turn fixture: prime the system and tools prefix
        body["messages"] = [*prefix, {"role": "user", "content": "."}]
    return body


def parse_plan(text: str | None) -> dict[str, object]:
    """Parse a planner reply into the fields the scorer reads.

    Args:
        text: The reply text. A ``</think>`` prefix and a code fence are stripped.

    Returns:
        ``parse_error`` and ``raw`` for an unreadable reply. Otherwise ``strategy``, ``declined`` (a
        ``SINGLE`` plan with no task), ``task_count``, ``goals`` and ``constraints``.
    """
    t = re.sub(r"^.*</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    failure: dict[str, object] = {"parse_error": True, "raw": (text or "")[:300]}
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", t, flags=re.S)
        if not match:
            return failure
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return failure
    if not isinstance(data, dict):
        return failure
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    assert isinstance(tasks, list)
    return {
        "parse_error": False,
        "strategy": data.get("strategy"),
        "declined": data.get("strategy") == "SINGLE" and not tasks,
        "task_count": len(tasks),
        "goals": [str((x or {}).get("goal", ""))[:200] for x in tasks if isinstance(x, dict)],
        "constraints": [
            len((x or {}).get("constraints") or []) for x in tasks if isinstance(x, dict)
        ],
    }


def stream(
    client: httpx.Client, url: str, model: str, body: Mapping[str, object]
) -> dict[str, object]:
    """Send one streamed chat completion and record what the gateway would see.

    Args:
        client: The HTTP client.
        url: The chat-completions URL.
        model: The model name for the request.
        body: The request body.

    Returns:
        ``secs``, ``ttft_any`` (first reasoning, content or tool token), ``ttft_content``, ``content``,
        ``reasoning_chars``, ``tool_names``, ``finish_reason``, ``usage`` and the engine's ``timings``.

    Raises:
        httpx.HTTPError: On a transport error or a non-2xx reply.
    """
    request = {**body, "model": model, "stream": True, "stream_options": {"include_usage": True}}
    t0 = time.monotonic()
    first_any: float | None = None
    first_content: float | None = None
    content: list[str] = []
    reasoning_chars = 0
    tool_names: list[str] = []
    finish: object = None
    engine_build: object = None
    usage: Mapping[str, object] = {}
    timings: Mapping[str, object] = {}
    with client.stream("POST", url, json=request) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            usage = chunk.get("usage") or usage
            engine_build = chunk.get("system_fingerprint") or engine_build
            timings = chunk.get("timings") or timings
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                text = delta.get("content") or ""
                calls = delta.get("tool_calls") or []
                now = time.monotonic() - t0
                if (reasoning or text or calls) and first_any is None:
                    first_any = now
                if (text or calls) and first_content is None:
                    first_content = now
                reasoning_chars += len(reasoning)
                content.append(text)
                tool_names += [
                    c["function"]["name"] for c in calls if (c.get("function") or {}).get("name")
                ]
                finish = choice.get("finish_reason") or finish
    text = "".join(content)
    # A server that writes its reasoning inline, before `</think>`, still thought: count it.
    inline = re.match(r"^(?:<think>)?(.*?)</think>", text, flags=re.S)
    reasoning_chars += len(inline.group(1)) if inline else 0
    return {
        "secs": round(time.monotonic() - t0, 2),
        "ttft_any": None if first_any is None else round(first_any, 2),
        "ttft_content": None if first_content is None else round(first_content, 2),
        "content": text,
        "reasoning_chars": reasoning_chars,
        "tool_names": tool_names,
        "finish_reason": finish,
        "system_fingerprint": engine_build,
        "usage": dict(usage),
        "timings": {
            k: timings.get(k)
            for k in ("cache_n", "prompt_n", "prompt_ms", "predicted_n", "predicted_ms")
        },
    }
