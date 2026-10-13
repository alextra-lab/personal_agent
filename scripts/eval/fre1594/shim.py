"""FRE-1594 / ADR-0156 A5 (T-B): the authorization shim of the reveal spike.

The two experimental tools are not in the registry. The shim stands in for the A5.5 check order:
a tool that is not revealed is refused and not run, and a revealed one runs the production
executor of the same name. (FRE-1596 split the one telemetry tool into these two, so the spike's
tool names are now the production names.) Every result is in the D6 contract shape.

The shim reads the eval Elasticsearch only. Tempo is the one Tempo, read only, as the eval gateways
read it (``docker-compose.eval.yml``).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from scripts.eval.fre1594.reveal import LATENCY_ARGS, SEARCH_ARGS

from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import dispatch_error, failure_payload, render_content
from personal_agent.tools.telemetry_query import (
    TelemetryQueryError,
    measure_model_latency_executor,
    measure_model_latency_tool,
    search_logs_executor,
    search_logs_tool,
)
from personal_agent.tools.types import ToolErrorDetail

EVAL_ES_PORT = 9202  # docker-compose.eval.yml, elasticsearch-eval
_EVAL_ES_HOSTS = ("127.0.0.1", "localhost", "elasticsearch-eval", "cloud-sim-elasticsearch-eval")
_SEARCH_ACTIONS = ("search", "count")

TelemetryExecutor = Callable[..., Awaitable[object]]

_PRODUCTION: Mapping[str, TelemetryExecutor] = {
    "search_logs": search_logs_executor,
    "measure_model_latency": measure_model_latency_executor,
}
_DEFINITIONS = {"search_logs": search_logs_tool, "measure_model_latency": measure_model_latency_tool}


@dataclass(frozen=True)
class ToolRun:
    """The outcome of one call through the shim.

    Attributes:
        content: The tool message content (D6 contract shape).
        valid: AC-1 step 2: the arguments parse, fit the tool and are not refused.
        ran: AC-1 step 3: the tool ran and returned a result.
        code: The failure code, or None.
    """

    content: str
    valid: bool
    ran: bool
    code: str | None


def _failure(detail: ToolErrorDetail) -> str:
    return json.dumps(failure_payload(detail))


def not_run(name: str) -> str:
    """Return the result of a call that the spike does not run.

    Args:
        name: The tool name.

    Returns:
        A D6 failure that says the call was not run.
    """
    return _failure(
        dispatch_error(
            "permission_denied",
            f"{name} is not run in this session.",
            retryable=False,
            next_action="Go on without this call.",
        )
    )


def assert_eval_elasticsearch(url: str) -> None:
    """Refuse an Elasticsearch that is not the eval one.

    Args:
        url: ``settings.elasticsearch_url``.

    Raises:
        SystemExit: If the URL does not name the eval Elasticsearch port on a local or eval host.
    """
    parts = urlsplit(url)
    if parts.port != EVAL_ES_PORT or parts.hostname not in _EVAL_ES_HOSTS:
        raise SystemExit(
            f"refusing to start: the shim reads the eval Elasticsearch only (port {EVAL_ES_PORT}), "
            f"and AGENT_ELASTICSEARCH_URL is {url!r}."
        )


def _arguments(name: str, raw: Mapping[str, object]) -> dict[str, object] | str:
    """Map a call's arguments to the executor's keyword arguments, or say why not."""
    allowed: Sequence[str] = SEARCH_ARGS if name == "search_logs" else LATENCY_ARGS
    foreign = sorted(set(raw) - set(allowed))
    if foreign:
        return f"{name} does not take {', '.join(foreign)}."
    if name == "search_logs":
        if raw.get("action") not in _SEARCH_ACTIONS:
            return "search_logs needs action 'search' or 'count'."
    return dict(raw)


async def run_tool(
    name: str,
    arguments: str,
    *,
    revealed: Collection[str],
    providers: Mapping[str, Sequence[str]],
    ctx: TraceContext,
    executor: TelemetryExecutor | None = None,
) -> ToolRun:
    """Authorize and run one call to an experimental tool.

    Args:
        name: ``search_logs`` or ``measure_model_latency``.
        arguments: The call's argument JSON.
        revealed: The tools revealed before this model response.
        providers: The skills that provide each tool.
        ctx: Trace context for the tool's logs.
        executor: One executor for both tools. Default: the production executor of ``name``. Tests
            pass a fake.

    Returns:
        The run.
    """
    if name not in revealed:
        skills = ", ".join(providers.get(name, ()))
        # A5.5: the D6 refusal lists every skill that provides the tool. T-C1 fixes the real code.
        detail = ToolErrorDetail(
            code="not_revealed",
            message=f"{name} is not available yet. Load a skill that provides it first: {skills}.",
            retryable=True,
            next_action="Call read_skill with one of those skills, then call the tool.",
        )
        return ToolRun(_failure(detail), False, False, detail.code)
    try:
        raw = json.loads(arguments)
    except json.JSONDecodeError:
        raw = None
    if not isinstance(raw, dict):
        detail = dispatch_error(
            "invalid_json_arguments",
            f"Arguments for {name} were not a JSON object.",
            retryable=True,
            next_action=f"Call {name} again with a JSON object.",
        )
        return ToolRun(_failure(detail), False, False, detail.code)
    mapped = _arguments(name, raw)
    if isinstance(mapped, str):
        detail = dispatch_error(
            "invalid_arguments", mapped, retryable=True, next_action=f"Call {name} again."
        )
        return ToolRun(_failure(detail), False, False, detail.code)
    try:
        run = executor or _PRODUCTION[name]
        output = await run(**mapped, ctx=ctx)
    except TelemetryQueryError as exc:
        refused = exc.code == "refused"
        return ToolRun(_failure(exc.detail), not refused, False, exc.code)
    except Exception as exc:  # noqa: BLE001 - any other failure means the call did not run
        detail = dispatch_error(
            "tool_exception", f"{type(exc).__name__}: {exc}", retryable=False, next_action="Stop."
        )
        return ToolRun(_failure(detail), True, False, detail.code)
    definition = _DEFINITIONS[name]
    content = render_content(
        output=output if isinstance(output, Mapping) else str(output),
        error_detail=None,
        max_result_tokens=definition.max_result_tokens or 10_000,
        truncation_next_action=definition.truncation_next_action or "Narrow the request.",
    )
    return ToolRun(content, True, True, None)
