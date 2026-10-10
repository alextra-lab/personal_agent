"""Native read-only telemetry query tool — FRE-1564 (ADR-0150 D2 amendment, ADR-0028 Tier 1).

A sub-agent worker of the ``general`` type holds private reads, so it must be able to read
the system's own logs and metrics without ``bash``. This tool is the only Elasticsearch
route a worker has. It is a security boundary, so it is built on two rules:

* **The model never writes the request.** It names an action, one of three index families
  and a validated query and aggregation. The tool builds the URL, the body, the time bound
  and the source filter. There is no path, method, header or raw body to supply.
* **Allow, never deny.** A query node, an aggregation type and a field name is accepted only
  if this module lists it. A denylist over Elasticsearch's query language cannot be complete
  (scripts in ``terms_set``, ``global`` aggregations that ignore the time bound, ``inner_hits``,
  ``top_metrics``, field aliases). With an allowlist, a new operator or a new logged field is
  unreachable until a person adds it here.

``agent-logs-*`` holds conversation-derived fields (``user_message``, ``query_text``,
``arguments``, ...). None of them is in the allowlist, so none can be queried, aggregated or
returned. ``message`` and ``error`` are free text written by the agent's own logging and can
carry a fragment of user input inside an exception string. That is the residual risk.

**Tempo (FRE-1567).** ADR-0129 moved model-call timing onto OTel spans, so Elasticsearch has
none. The ``latency`` action reads Tempo's search API under the same two rules: the model picks
a span kind, group keys and two exact-match filters, and the tool writes the TraceQL, the
window and the limits. The span attributes it selects and reads are listed in
``TEMPO_ATTRIBUTES``. Attributes that carry text or a conversation id (``statusMessage``,
``url.full``, ``http.target``, ``slm.session_id``, span names of HTTP spans) are not in it,
and the result is built from the listed attributes only. The model gets per-group statistics,
never spans.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeGuard

import httpx

from personal_agent.config import settings
from personal_agent.security import EgressBlockedError, create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# family -> the date field the time bound applies to. Indices are addressed as ``<family>-*``.
FAMILY_TIME_FIELD: Mapping[str, str] = MappingProxyType(
    {
        "agent-logs": "@timestamp",
        "agent-topology": "@timestamp",
        "agent-monitors-slm-health": "probed_at",
    }
)

# family -> the fields a query, aggregation or ``fields`` list may name. Positive list,
# checked against the live mappings on 2026-10-10. A name plus ``.keyword`` is also allowed.
# FRE-1567 removed duration_ms, latency_ms and response_time_ms: no event wrote them in the
# 30-day window (ADR-0129, FRE-1219), and the window cap makes older data unreachable.
ALLOWED_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "agent-logs": frozenset(
            {
                "@timestamp",
                "level",
                "event_type",
                "message",
                "error",
                "trace_id",
                "session_id",
                "action",
                "task_type",
                "mode",
                "tool_name",
                "model",
                "model_id",
                "role",
                "provider",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cost_usd",
                "elapsed_ms",
                "actual_wall_ms",
                "max_latency_ms",
                "probe_latency_ms",
                "planner_duration_ms",
                "elapsed_generation_ms",
                "rerank_ms",
                "query_embedding_ms",
                "success",
                "turn_count",
                "decision",
                "status",
                "component",
                "backend",
                "reachable",
                "count",
                "http_status",
                "status_code",
            }
        ),
        "agent-topology": frozenset(
            {
                "@timestamp",
                "session_id",
                "trace_id",
                "task_id",
                "task_type",
                "topology",
                "role",
                "model_role",
                "complexity",
                "decomposition_strategy",
                "decomposition_reason",
                "planner_decision",
                "planner_mode",
                "planner_failure_reason",
                "planner_gate_reason",
                "planner_duration_ms",
                "planner_prompt_tokens",
                "planner_completion_tokens",
                "input_tokens",
                "output_tokens",
                "first_token_ms",
                "latency_total_ms",
                "authoritative_cost_usd",
                "result_type",
                "gateway_label",
                "intent_confidence",
            }
        ),
        "agent-monitors-slm-health": frozenset(
            {
                "error",
                "generation_ok",
                "generation_probe_latency_ms",
                "generation_skip_reason",
                "gpu_util_pct",
                "kind",
                "latency_ema_ms",
                "model_id",
                "model_loaded",
                "probe_latency_ms",
                "probed_at",
                "queue_depth",
                "reachable",
                "status",
                "trace_id",
                "vram_total_mb",
                "vram_used_mb",
            }
        ),
    }
)

_ACTIONS = ("search", "count")
_DEFAULT_SIZE = 20
_MAX_SIZE = 50
_DEFAULT_SINCE = "24h"
_MAX_SINCE_MINUTES = 30 * 24 * 60
_SINCE_UNIT_MINUTES = {"m": 1, "h": 60, "d": 24 * 60}
_SINCE_RE = re.compile(r"(\d{1,3})([mhd])")
_MAX_FIELDS = 20
_MAX_QUERY_DEPTH = 8
_MAX_QUERY_NODES = 50
_MAX_AGG_DEPTH = 4
_MAX_AGGS = 10
_MAX_AGG_BUCKETS = 100
_MAX_TERMS = 100
_MAX_STRING = 200
_MAX_RESULT_CHARS = 20_000
_CLIENT_TIMEOUT_S = 15.0
_ES_TIMEOUT = "10s"
_ERROR_CHARS = 300

_AGG_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_FIXED_INTERVAL_RE = re.compile(r"\d{1,4}(s|m|h|d)")
_CALENDAR_INTERVALS = frozenset(
    {"minute", "hour", "day", "week", "month", "quarter", "year", "1m", "1h", "1d", "1w", "1M"}
)

_BOOL_CLAUSES = ("must", "filter", "should", "must_not")
_RANGE_KEYS = frozenset({"gte", "gt", "lte", "lt", "format", "time_zone"})
# field-keyed operator -> the keys its object form may carry
_VALUE_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "term": frozenset({"value"}),
        "prefix": frozenset({"value"}),
        "match": frozenset({"query", "operator"}),
        "match_phrase": frozenset({"query"}),
    }
)
_METRIC_AGGS = ("avg", "sum", "min", "max", "value_count", "cardinality", "stats")
_AGG_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "terms": frozenset({"field", "size", "order", "min_doc_count"}),
        "date_histogram": frozenset(
            {"field", "calendar_interval", "fixed_interval", "min_doc_count"}
        ),
        "percentiles": frozenset({"field", "percents"}),
        **{name: frozenset({"field"}) for name in _METRIC_AGGS},
    }
)
_BUCKET_AGGS = frozenset({"terms", "date_histogram"})


@dataclass(frozen=True)
class _TempoSpan:
    """One span kind the ``latency`` action can read.

    Attributes:
        selector: The fixed TraceQL condition that picks the spans.
        timings: Output prefix -> an integer millisecond attribute summarised per group.
    """

    selector: str
    timings: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


# span kind -> how to find it. Live on 2026-10-10: ``model_call <model>`` spans (service
# seshat-vps) carry the role; slm-server ``chat <model>`` spans carry prefill and decode time.
TEMPO_SPANS: Mapping[str, _TempoSpan] = MappingProxyType(
    {
        "model_call": _TempoSpan('span:name =~ "model_call .*"'),
        "slm_chat": _TempoSpan(
            'resource.service.name = "slm-server" && span:name =~ "chat .*"',
            MappingProxyType({"prefill": "slm.prefill_ms", "decode": "slm.decode_ms"}),
        ),
    }
)
# group key (and filter name) -> the span attribute it reads
TEMPO_GROUP_KEYS: Mapping[str, str] = MappingProxyType(
    {"role": "gen_ai.operation.name", "model": "gen_ai.request.model"}
)
_TEMPO_TOKENS: Mapping[str, str] = MappingProxyType(
    {"input_tokens": "gen_ai.usage.input_tokens", "output_tokens": "gen_ai.usage.output_tokens"}
)
# Every span attribute the tool selects or reads: the positive list. The conversation-text
# check of 2026-10-10 (plan section 1.1) found that no attribute here carries text.
TEMPO_ATTRIBUTES: frozenset[str] = frozenset(
    {
        *TEMPO_GROUP_KEYS.values(),
        *_TEMPO_TOKENS.values(),
        *(name for kind in TEMPO_SPANS.values() for name in kind.timings.values()),
        "span:duration",
        "span:status",
    }
)
# Tempo names a selected ``span:status`` "status" in its reply.
_TEMPO_STATUS_KEY = "status"
_TEMPO_READ_KEYS = frozenset(
    {*(a for a in TEMPO_ATTRIBUTES if not a.startswith("span:")), _TEMPO_STATUS_KEY}
)

_LATENCY = "latency"
_TEMPO_DEFAULT_LIMIT = 200
_TEMPO_MAX_LIMIT = 500
_TEMPO_SPANS_PER_SET = 100
# Tempo's search refuses a window over 168 h (live, 2026-10-10).
_TEMPO_MAX_SINCE_MINUTES = 7 * 24 * 60
_TEMPO_MAX_BYTES = 16 * 1024 * 1024
_TEMPO_MAX_SPANS = _TEMPO_MAX_LIMIT * _TEMPO_SPANS_PER_SET
_TEMPO_MAX_GROUPS = 50
_TEMPO_QUERY_ERROR_STATUSES = frozenset({400, 422})
# A filter value, and a group label read back from Tempo, must look like a role or model id.
# No quote or backslash can enter the TraceQL, and no free text can come back.
_LABEL_RE = re.compile(r"[A-Za-z0-9._/:-]{1,100}")
_ES_ONLY_ARGS = ("index", "query", "aggs", "fields", "size")
_TEMPO_ONLY_ARGS = ("span", "group_by", "role", "model", "limit")


def _render_description() -> str:
    """Build the tool description from the allowlists, so the two cannot drift (AC-1).

    Returns:
        The description, with one line per index family and per Tempo list.
    """
    family_lines = [
        f"- {family} fields: {', '.join(sorted(fields))}."
        for family, fields in ALLOWED_FIELDS.items()
    ]
    return "\n".join(
        [
            "Read the system's own logs, metrics, health and model-call timing. Read-only. "
            "Never use it for the user's conversation text: that is not available.",
            "Actions: 'search' and 'count' read Elasticsearch and need 'index'. 'latency' "
            "reads model-call timing from Tempo trace spans and needs 'since'.",
            "Elasticsearch index families and the only fields each accepts:",
            *family_lines,
            "Elasticsearch holds no model-call timing: duration_ms, latency_ms and "
            "response_time_ms are not in agent-logs (retired in August 2026, ADR-0129). "
            "agent-topology latency_total_ms is empty since 2026-08-08 (FRE-1568). "
            "Use 'latency' for model-call timing. Other timing fields depend on event_type; "
            "to find which event carries a field, use an exists query with a terms "
            "aggregation on event_type.",
            "search and count: 'since' default 24h, at most 30d. Query operators: match_all, "
            "bool, term, terms, match, match_phrase, prefix, range, exists. Aggregations: "
            "terms, date_histogram, avg, sum, min, max, value_count, cardinality, stats, "
            "percentiles.",
            f"- latency spans: {', '.join(TEMPO_SPANS)}.",
            f"- latency group_by: {', '.join(TEMPO_GROUP_KEYS)}.",
            f"- latency span attributes: {', '.join(sorted(TEMPO_ATTRIBUTES))}.",
            "latency: model_call is one span per model call; slm_chat is the local model "
            "server's side and adds prefill and decode time. Filter with 'role' and 'model' "
            f"(exact values). 'since' 1m to 7d. 'limit' caps traces (default "
            f"{_TEMPO_DEFAULT_LIMIT}, at most {_TEMPO_MAX_LIMIT}). It returns, per group, "
            "count, errors, p50_ms, p90_ms, max_ms and token sums over the spans it read. "
            "complete=false means a cap was hit and the numbers cover a sample.",
            "A name outside these lists is refused.",
        ]
    )


query_telemetry_tool = ToolDefinition(
    name="query_telemetry",
    description=_render_description(),
    category="read_only",
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description=(
                "'search' (hits and aggregations), 'count' (a document count) or 'latency' "
                "(model-call timing from Tempo)."
            ),
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="index",
            type="string",
            description="The index family to read. Required for search and count.",
            required=False,
            default=None,
            json_schema={
                "type": "string",
                "enum": list(FAMILY_TIME_FIELD),
                "description": "The index family to read. Required for search and count.",
            },
        ),
        ToolParameter(
            name="query",
            type="object",
            description='Optional filter, e.g. {"term": {"level": "ERROR"}}. Default: all.',
            required=False,
            default=None,
            json_schema={
                "type": "object",
                "description": 'Optional filter, e.g. {"term": {"level": "ERROR"}}. Default: all.',
            },
        ),
        ToolParameter(
            name="aggs",
            type="object",
            description=(
                "Optional aggregations for 'search', e.g. "
                '{"by_event": {"terms": {"field": "event_type", "size": 10}}}.'
            ),
            required=False,
            default=None,
            json_schema={
                "type": "object",
                "description": (
                    "Optional aggregations for 'search', e.g. "
                    '{"by_event": {"terms": {"field": "event_type", "size": 10}}}.'
                ),
            },
        ),
        ToolParameter(
            name="fields",
            type="array",
            description="Optional field names to return for each hit. Default: all allowed fields.",
            required=False,
            default=None,
            json_schema={
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional field names to return for each hit.",
            },
        ),
        ToolParameter(
            name="size",
            type="number",
            description=f"Hits to return (default {_DEFAULT_SIZE}, at most {_MAX_SIZE}).",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="since",
            type="string",
            description=(
                f"How far back to look: a number and m, h or d, e.g. '90m', '24h', '7d'. "
                f"search and count: default {_DEFAULT_SINCE}, at most 30d. latency: required, "
                "at most 7d."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="span",
            type="string",
            description="latency only: the span kind. Default model_call.",
            required=False,
            default=None,
            json_schema={
                "type": "string",
                "enum": list(TEMPO_SPANS),
                "description": "latency only: the span kind. Default model_call.",
            },
        ),
        ToolParameter(
            name="group_by",
            type="array",
            description="latency only: group keys. Default ['role', 'model'].",
            required=False,
            default=None,
            json_schema={
                "type": "array",
                "items": {"type": "string", "enum": list(TEMPO_GROUP_KEYS)},
                "description": "latency only: group keys. Default ['role', 'model'].",
            },
        ),
        ToolParameter(
            name="role",
            type="string",
            description="latency only: keep spans of this role, e.g. 'primary'.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="model",
            type="string",
            description="latency only: keep spans of this model id.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description=(
                f"latency only: traces to read (default {_TEMPO_DEFAULT_LIMIT}, at most "
                f"{_TEMPO_MAX_LIMIT})."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=20,
    rate_limit_per_hour=120,
    # The primary reads telemetry with bash and curl (docs/skills/query-elasticsearch.md).
    # Offering it a second route would add prompt tokens to every turn and change its
    # tool surface, which the ticket did not ask for.
    worker_only=True,
)


def _refuse(reason: str) -> ToolExecutionError:
    """Build the error for a request the tool will not send.

    Args:
        reason: Why the request was refused.

    Returns:
        An error whose message starts with ``refused:``.
    """
    return ToolExecutionError(f"refused: {reason}")


def _is_number(value: object) -> TypeGuard[int | float]:
    """Report whether ``value`` is a finite int or float and not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: object) -> TypeGuard[int]:
    """Report whether ``value`` is an int and not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


class _Validator:
    """Checks a model-written query or aggregation against this module's allowlists.

    One instance per call. It counts query nodes and aggregations, so the bounds hold across
    the whole request and not per branch.
    """

    def __init__(self, family: str) -> None:
        self._family = family
        self._allowed = ALLOWED_FIELDS[family]
        self._query_nodes = 0
        self._aggs = 0

    def field(self, name: object) -> str:
        """Check one field name.

        Args:
            name: The field name the model wrote.

        Returns:
            The name unchanged.

        Raises:
            ToolExecutionError: The name is not a string or not in this family's list.
        """
        if not isinstance(name, str) or name.removesuffix(".keyword") not in self._allowed:
            raise _refuse(
                f"field {name!r} is not available in {self._family}. "
                f"Allowed fields: {', '.join(sorted(self._allowed))}"
            )
        return name

    def scalar(self, value: object) -> None:
        """Check a literal value: a short string, a number or a bool.

        Args:
            value: The value the model wrote.

        Raises:
            ToolExecutionError: The value is another type, or a string over the length bound.
        """
        if isinstance(value, str):
            if len(value) > _MAX_STRING:
                raise _refuse(f"a string value is longer than {_MAX_STRING} characters")
        elif not (_is_number(value) or isinstance(value, bool)):
            raise _refuse("a value must be a string, a number or a boolean")

    def query(self, node: object, depth: int = 1) -> None:
        """Check a query node and its children.

        Args:
            node: An object with exactly one operator key.
            depth: The nesting depth of ``node``.

        Raises:
            ToolExecutionError: The node uses an operator, field or shape that is not listed,
                or the query is nested or wide beyond the bounds.
        """
        self._query_nodes += 1
        if depth > _MAX_QUERY_DEPTH or self._query_nodes > _MAX_QUERY_NODES:
            raise _refuse("the query is nested or wide beyond the allowed bounds")
        if not isinstance(node, dict) or len(node) != 1:
            raise _refuse("a query node must be an object with exactly one operator")
        ((op, body),) = node.items()
        if op == "match_all":
            if body != {}:
                raise _refuse("match_all takes an empty object")
        elif op == "bool":
            self._bool(body, depth)
        elif op in _VALUE_KEYS:
            self._field_clause(op, body)
        elif op == "terms":
            self._terms(body)
        elif op == "range":
            self._range(body)
        elif op == "exists":
            if not isinstance(body, dict) or set(body) != {"field"}:
                raise _refuse('exists takes {"field": <name>}')
            self.field(body["field"])
        else:
            raise _refuse(
                f"query operator {op!r} is not supported. Supported: match_all, bool, term, "
                "terms, match, match_phrase, prefix, range, exists"
            )

    def _bool(self, body: object, depth: int) -> None:
        if not isinstance(body, dict) or not body:
            raise _refuse("bool takes a non-empty object")
        for key, clause in body.items():
            if key == "minimum_should_match":
                if not _is_int(clause) or not 0 <= clause <= 10:
                    raise _refuse("minimum_should_match must be an integer from 0 to 10")
            elif key in _BOOL_CLAUSES:
                for child in clause if isinstance(clause, list) else [clause]:
                    self.query(child, depth + 1)
            else:
                raise _refuse(f"bool key {key!r} is not supported")

    def _single_field(self, body: object) -> tuple[str, object]:
        if not isinstance(body, dict) or len(body) != 1:
            raise _refuse("a field clause names exactly one field")
        ((name, value),) = body.items()
        return self.field(name), value

    def _field_clause(self, op: str, body: object) -> None:
        _, value = self._single_field(body)
        if isinstance(value, dict):
            if not value or not set(value) <= _VALUE_KEYS[op]:
                raise _refuse(f"{op} accepts only the keys {sorted(_VALUE_KEYS[op])}")
            for key, item in value.items():
                if key == "operator":
                    if item not in ("and", "or"):
                        raise _refuse("operator must be 'and' or 'or'")
                else:
                    self.scalar(item)
        else:
            self.scalar(value)

    def _terms(self, body: object) -> None:
        _, values = self._single_field(body)
        if not isinstance(values, list) or not 1 <= len(values) <= _MAX_TERMS:
            raise _refuse(f"terms takes a list of 1 to {_MAX_TERMS} values")
        for item in values:
            self.scalar(item)

    def _range(self, body: object) -> None:
        _, bounds = self._single_field(body)
        if not isinstance(bounds, dict) or not bounds or not set(bounds) <= _RANGE_KEYS:
            raise _refuse(f"range accepts only the keys {sorted(_RANGE_KEYS)}")
        for item in bounds.values():
            self.scalar(item)

    def aggs(self, node: object, depth: int = 1) -> None:
        """Check a set of named aggregations and their children.

        Args:
            node: A mapping of aggregation name to definition.
            depth: The nesting depth of ``node``.

        Raises:
            ToolExecutionError: An aggregation type, key or field is not listed, or the
                aggregations are nested or numerous beyond the bounds.
        """
        if depth > _MAX_AGG_DEPTH or not isinstance(node, dict) or not node:
            raise _refuse(
                f"aggregations must be a non-empty object nested at most {_MAX_AGG_DEPTH} deep"
            )
        for name, definition in node.items():
            self._aggs += 1
            if self._aggs > _MAX_AGGS:
                raise _refuse(f"at most {_MAX_AGGS} aggregations are allowed")
            if not isinstance(name, str) or not _AGG_NAME_RE.fullmatch(name):
                raise _refuse("an aggregation name is 1 to 40 letters, digits, '_' or '-'")
            if not isinstance(definition, dict):
                raise _refuse("an aggregation is an object")
            kinds = [key for key in definition if key != "aggs"]
            if len(kinds) != 1 or kinds[0] not in _AGG_KEYS:
                raise _refuse(
                    f"an aggregation has exactly one type. Supported: {', '.join(_AGG_KEYS)}"
                )
            self._agg_spec(kinds[0], definition[kinds[0]])
            if "aggs" in definition:
                if kinds[0] not in _BUCKET_AGGS:
                    raise _refuse(f"{kinds[0]} cannot hold sub-aggregations")
                self.aggs(definition["aggs"], depth + 1)

    def _agg_spec(self, kind: str, spec: object) -> None:
        if not isinstance(spec, dict) or not set(spec) <= _AGG_KEYS[kind]:
            raise _refuse(f"{kind} accepts only the keys {sorted(_AGG_KEYS[kind])}")
        if "field" not in spec:
            raise _refuse(f"{kind} needs a field")
        self.field(spec["field"])
        size = spec.get("size")
        if "size" in spec and (not _is_int(size) or not 1 <= size <= _MAX_AGG_BUCKETS):
            raise _refuse(f"size must be an integer from 1 to {_MAX_AGG_BUCKETS}")
        min_count = spec.get("min_doc_count")
        if "min_doc_count" in spec and (not _is_int(min_count) or not 0 <= min_count <= 1_000_000):
            raise _refuse("min_doc_count must be an integer from 0 to 1000000")
        if "order" in spec:
            order = spec["order"]
            if not isinstance(order, dict) or len(order) != 1:
                raise _refuse("order names one key and a direction")
            ((key, direction),) = order.items()
            if not (isinstance(key, str) and _AGG_NAME_RE.fullmatch(key.lstrip("_") or "-")):
                raise _refuse("order key must be _count, _key or a sub-aggregation name")
            if direction not in ("asc", "desc"):
                raise _refuse("order direction must be 'asc' or 'desc'")
        if kind == "date_histogram":
            self._interval(spec)
        if "percents" in spec:
            percents = spec["percents"]
            if (
                not isinstance(percents, list)
                or not 1 <= len(percents) <= 10
                or not all(_is_number(p) and 0 <= p <= 100 for p in percents)
            ):
                raise _refuse("percents is a list of 1 to 10 numbers from 0 to 100")

    @staticmethod
    def _interval(spec: dict[object, object]) -> None:
        calendar, fixed = spec.get("calendar_interval"), spec.get("fixed_interval")
        if (calendar is None) == (fixed is None):
            raise _refuse("date_histogram needs exactly one of calendar_interval, fixed_interval")
        if calendar is not None and (
            not isinstance(calendar, str) or calendar not in _CALENDAR_INTERVALS
        ):
            raise _refuse(f"calendar_interval must be one of {sorted(_CALENDAR_INTERVALS)}")
        if fixed is not None and not (
            isinstance(fixed, str) and _FIXED_INTERVAL_RE.fullmatch(fixed)
        ):
            raise _refuse("fixed_interval is a number and s, m, h or d, e.g. '15m'")

    def include_fields(self, fields: object) -> list[str]:
        """Resolve the ``fields`` argument to the list returned for each hit.

        Args:
            fields: The model's list, or ``None`` or an empty list for the family default.

        Returns:
            The field names, without duplicates, in the order given. The family's whole
            allowlist when none were given. Never a wildcard.

        Raises:
            ToolExecutionError: ``fields`` is not a list of allowed names, or is too long.
        """
        if fields is None or fields == []:
            return sorted(self._allowed)
        if not isinstance(fields, list) or len(fields) > _MAX_FIELDS:
            raise _refuse(f"fields is a list of at most {_MAX_FIELDS} field names")
        return list(dict.fromkeys(self.field(name) for name in fields))


def _parse_since(since: object) -> str:
    """Check the look-back window.

    Args:
        since: A number and ``m``, ``h`` or ``d``, or ``None`` for the default.

    Returns:
        The window as written.

    Raises:
        ToolExecutionError: The value is malformed, zero, or longer than 30 days.
    """
    if since is None:
        return _DEFAULT_SINCE
    match = _SINCE_RE.fullmatch(since) if isinstance(since, str) else None
    if match is None:
        raise _refuse("since is a number and m, h or d, e.g. '90m', '24h', '7d'")
    minutes = int(match.group(1)) * _SINCE_UNIT_MINUTES[match.group(2)]
    if not 1 <= minutes <= _MAX_SINCE_MINUTES:
        raise _refuse("since must be between 1 minute and 30 days")
    return str(since)


def _parse_size(size: object) -> int:
    """Check the hit count and bound it.

    Args:
        size: The requested number of hits, or ``None`` for the default.

    Returns:
        The hit count, from 0 to the cap.

    Raises:
        ToolExecutionError: The value is not a number.
    """
    if size is None:
        return _DEFAULT_SIZE
    if not _is_number(size):
        raise _refuse("size is a number")
    return max(0, min(int(size), _MAX_SIZE))


def _build_request(
    action: str,
    index: str,
    query: object,
    aggs: object,
    fields: object,
    size: object,
    since: object,
) -> tuple[str, dict[str, Any]]:
    """Validate every argument and build the one request the tool will send.

    Args:
        action: ``search`` or ``count``.
        index: An index family.
        query: The model's query, or ``None`` or ``{}`` for all documents.
        aggs: The model's aggregations, or ``None`` or ``{}`` for none.
        fields: The model's field list, or ``None`` or ``[]`` for the family default.
        size: The requested number of hits.
        since: The look-back window.

    Returns:
        The request path (after the host) and the JSON body.

    Raises:
        ToolExecutionError: Any argument is refused. Nothing has been sent at that point.
    """
    if action not in _ACTIONS:
        raise _refuse(f"action must be one of {', '.join((*_ACTIONS, _LATENCY))}")
    if not isinstance(index, str) or index not in FAMILY_TIME_FIELD:
        raise _refuse(f"index must be one of {', '.join(FAMILY_TIME_FIELD)}")
    window = _parse_since(since)
    check = _Validator(index)
    user_query: object = {"match_all": {}} if query in (None, {}) else query
    check.query(user_query)
    has_aggs = aggs not in (None, {})
    if has_aggs:
        if action == "count":
            raise _refuse("aggregations need action 'search'")
        check.aggs(aggs)
    include = check.include_fields(fields)
    hits = _parse_size(size)

    time_field = FAMILY_TIME_FIELD[index]
    bounded = {
        "bool": {
            "filter": [{"range": {time_field: {"gte": f"now-{window}"}}}],
            "must": [user_query],
        }
    }
    if action == "count":
        return f"/{index}-*/_count", {"query": bounded}
    body: dict[str, Any] = {
        "query": bounded,
        "size": hits,
        "timeout": _ES_TIMEOUT,
        "track_total_hits": True,
        "_source": {"includes": include},
        "sort": [{time_field: {"order": "desc", "unmapped_type": "date"}}],
    }
    if has_aggs:
        body["aggs"] = aggs
    return f"/{index}-*/_search", body


def _short_reason(response: httpx.Response) -> str:
    """Pull a short reason out of an Elasticsearch error reply.

    Args:
        response: A reply with a 4xx or 5xx status.

    Returns:
        The first root-cause reason, or the start of the body, cut to a short length.
    """
    try:
        error = response.json().get("error", {})
        causes = error.get("root_cause") or [error]
        reason = str(causes[0].get("reason") or error.get("reason") or response.text)
    except (ValueError, AttributeError, IndexError, TypeError):
        reason = response.text
    return reason[:_ERROR_CHARS]


def _shard_failures(data: Mapping[str, Any]) -> dict[str, Any]:
    """Report shards that failed, because Elasticsearch answers 200 with their data missing.

    ``agent-logs-*`` spans monthly indices, and a field mapped differently in one month can
    fail on that month's shards only. The rest of the reply still looks complete.

    Args:
        data: The decoded reply.

    Returns:
        ``{}`` when no shard failed. Otherwise ``shards_failed`` and, when the reply gives
        one, a short ``shard_failure_reason``.
    """
    shards = data.get("_shards")
    failed = shards.get("failed", 0) if isinstance(shards, dict) else 0
    if not _is_int(failed) or failed <= 0:
        return {}
    out: dict[str, Any] = {"shards_failed": failed}
    failures = shards.get("failures") if isinstance(shards, dict) else None
    if isinstance(failures, list) and failures and isinstance(failures[0], dict):
        reason = failures[0].get("reason")
        text = reason.get("reason") if isinstance(reason, dict) else reason
        if isinstance(text, str):
            out["shard_failure_reason"] = text[:_ERROR_CHARS]
    return out


def _shape_result(action: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce an Elasticsearch reply to what the model needs, within the size cap.

    Args:
        action: ``search`` or ``count``.
        data: The decoded reply.

    Returns:
        ``{"count": n}`` for a count. For a search: ``total``, ``aggregations`` when asked,
        and ``hits`` (the source documents). Either carries ``shards_failed`` when a shard
        failed. Over the cap, hits are dropped from the end until the reply fits and
        ``truncated`` says so, so aggregations survive. If the aggregations alone are over the
        cap, the reply is ``total``, ``truncated`` and the first characters of the result.
    """
    if action == "count":
        return {"count": data.get("count", 0), **_shard_failures(data)}
    hits = data.get("hits", {})
    total = hits.get("total", {})
    out: dict[str, Any] = {"total": total.get("value", 0) if isinstance(total, dict) else total}
    if "aggregations" in data:
        out["aggregations"] = data["aggregations"]
    if data.get("timed_out"):
        out["timed_out"] = True
    out.update(_shard_failures(data))
    documents = [h.get("_source", {}) for h in hits.get("hits", [])]
    out["hits"] = documents
    if len(json.dumps(out)) <= _MAX_RESULT_CHARS:
        return out
    while documents and len(json.dumps(out)) > _MAX_RESULT_CHARS:
        documents.pop()
    text = json.dumps(out)
    if len(text) <= _MAX_RESULT_CHARS:
        return {**out, "truncated": True, "hits_returned": len(documents)}
    return {
        "total": out["total"],
        "truncated": True,
        "chars_total": len(text),
        "partial_json": text[:_MAX_RESULT_CHARS],
    }


def _is_absent(value: object) -> bool:
    """Report whether an optional argument was left out (``None``, ``""``, ``{}`` or ``[]``)."""
    return value is None or value in ("", {}, [])


def _refuse_foreign_args(action: str, args: Mapping[str, object], names: Sequence[str]) -> None:
    """Refuse arguments that belong to the other data source.

    Args:
        action: The action the model asked for.
        args: Every argument the model passed, by name.
        names: The names that ``action`` does not take.

    Raises:
        ToolExecutionError: One of ``names`` was given a value.
    """
    given = [name for name in names if not _is_absent(args.get(name))]
    if given:
        raise _refuse(f"action {action!r} does not take {', '.join(given)}")


@dataclass(frozen=True)
class _TempoRequest:
    """A validated ``latency`` request: the query parameters and how to read the reply.

    Attributes:
        params: The search API query parameters, built by the tool.
        span: The span kind.
        group_by: The group keys, in order.
        window: The ``since`` value as written.
        limit: The trace limit.
    """

    params: Mapping[str, str]
    span: str
    group_by: tuple[str, ...]
    window: str
    limit: int


def _build_tempo_request(
    since: object,
    span: object,
    group_by: object,
    role: object,
    model: object,
    limit: object,
    *,
    now: float,
) -> _TempoRequest:
    """Validate the ``latency`` arguments and build the one search the tool will send.

    Args:
        since: The look-back window. Required, from ``1m`` to ``7d``.
        span: A key of ``TEMPO_SPANS``, or ``None`` for ``model_call``.
        group_by: A list of distinct keys of ``TEMPO_GROUP_KEYS``, or ``None`` or ``[]`` for
            role and model.
        role: An exact role to keep, or ``None``.
        model: An exact model id to keep, or ``None``.
        limit: The trace limit, an integer from 1 to 500, or ``None`` for 200.
        now: The current Unix time in seconds.

    Returns:
        The request.

    Raises:
        ToolExecutionError: Any argument is refused. Nothing has been sent at that point.
    """
    if since is None:
        raise _refuse("latency needs 'since', e.g. '24h' (at most 7d)")
    match = _SINCE_RE.fullmatch(since) if isinstance(since, str) else None
    if match is None:
        raise _refuse("since is a number and m, h or d, e.g. '90m', '24h', '7d'")
    minutes = int(match.group(1)) * _SINCE_UNIT_MINUTES[match.group(2)]
    if not 1 <= minutes <= _TEMPO_MAX_SINCE_MINUTES:
        raise _refuse("for latency, since must be between 1 minute and 7 days")

    kind = "model_call" if span is None else span
    if not isinstance(kind, str) or kind not in TEMPO_SPANS:
        raise _refuse(f"span must be one of {', '.join(TEMPO_SPANS)}")

    keys: object = list(TEMPO_GROUP_KEYS) if _is_absent(group_by) else group_by
    if (
        not isinstance(keys, list)
        or not all(isinstance(k, str) and k in TEMPO_GROUP_KEYS for k in keys)
        or len(set(keys)) != len(keys)
    ):
        raise _refuse(f"group_by is a list of distinct keys from {', '.join(TEMPO_GROUP_KEYS)}")

    if limit is None:
        traces = _TEMPO_DEFAULT_LIMIT
    elif _is_number(limit) and float(limit).is_integer() and 1 <= limit <= _TEMPO_MAX_LIMIT:
        traces = int(limit)
    else:
        raise _refuse(f"limit must be an integer from 1 to {_TEMPO_MAX_LIMIT}")

    conditions = [TEMPO_SPANS[kind].selector]
    for name, value in (("role", role), ("model", model)):
        if value is None:
            continue
        if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
            raise _refuse(f"{name} is 1 to 100 letters, digits or . _ / : -")
        conditions.append(f'span.{TEMPO_GROUP_KEYS[name]} = "{value}"')

    selected = [
        *(f"span.{a}" for a in (*TEMPO_GROUP_KEYS.values(), *_TEMPO_TOKENS.values())),
        "span:status",
        *(f"span.{a}" for a in TEMPO_SPANS[kind].timings.values()),
    ]
    traceql = f"{{ {' && '.join(conditions)} }} | select({', '.join(selected)})"
    end = int(now)
    return _TempoRequest(
        params=MappingProxyType(
            {
                "q": traceql,
                "start": str(end - minutes * 60),
                "end": str(end),
                "limit": str(traces),
                "spss": str(_TEMPO_SPANS_PER_SET),
            }
        ),
        span=kind,
        group_by=tuple(keys),
        window=match.group(0),
        limit=traces,
    )


def _as_int(value: object) -> int | None:
    """Read an integer that Tempo's JSON may write as a number or a decimal string.

    Args:
        value: The raw value.

    Returns:
        The integer, or ``None`` when ``value`` is not a whole number.
    """
    if _is_int(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,20}", value):
        return int(value)
    return None


def _span_attributes(span: Mapping[str, object]) -> dict[str, str | int]:
    """Read the listed attributes of one span and drop every other one.

    Args:
        span: One span of a Tempo search reply.

    Returns:
        Attribute name -> a string or integer value, for names in ``_TEMPO_READ_KEYS`` only.
    """
    out: dict[str, str | int] = {}
    attributes = span.get("attributes")
    for item in attributes if isinstance(attributes, list) else []:
        if not isinstance(item, dict) or item.get("key") not in _TEMPO_READ_KEYS:
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("stringValue"), str):
            out[item["key"]] = value["stringValue"]
        elif (number := _as_int(value.get("intValue"))) is not None:
            out[item["key"]] = number
    return out


def _label(value: str | int | None) -> str:
    """Turn a group value from Tempo into a safe label.

    Args:
        value: The attribute value, or ``None`` when the span lacks it.

    Returns:
        The value when it looks like a role or model id, ``<none>`` when absent, else
        ``<unrecognised>``. No free text from Tempo can pass.
    """
    if value is None:
        return "<none>"
    if isinstance(value, str) and _LABEL_RE.fullmatch(value):
        return value
    return "<unrecognised>"


def _percentile(ordered: Sequence[float], percent: int) -> float:
    """Nearest-rank percentile of a sorted, non-empty sequence, rounded to 0.1."""
    return round(ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)], 1)


@dataclass
class _Group:
    """Running totals for one group while the reply is read."""

    durations_ms: list[float] = field(default_factory=list)
    errors: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_TEMPO_TOKENS, 0))
    timings: dict[str, list[float]] = field(default_factory=dict)

    def summary(self, labels: Mapping[str, str]) -> dict[str, object]:
        """Return the group's statistics with its labels first."""
        ordered = sorted(self.durations_ms)
        out: dict[str, object] = {
            **labels,
            "count": len(ordered),
            "errors": self.errors,
            "p50_ms": _percentile(ordered, 50),
            "p90_ms": _percentile(ordered, 90),
            "max_ms": round(ordered[-1], 1),
            **self.tokens,
        }
        for prefix, values in self.timings.items():
            values.sort()
            out[f"{prefix}_p50_ms"] = _percentile(values, 50)
            out[f"{prefix}_p90_ms"] = _percentile(values, 90)
        return out


def _trace_span_sets(trace: Mapping[str, object]) -> list[Mapping[str, object]] | None:
    """Return a trace's span sets, from either reply shape, or ``None`` when it has none."""
    span_sets = trace.get("spanSets")
    if isinstance(span_sets, list):
        return [s for s in span_sets if isinstance(s, dict)]
    single = trace.get("spanSet")
    return [single] if isinstance(single, dict) else None


def _summarise_tempo(data: Mapping[str, object], request: _TempoRequest) -> dict[str, object]:
    """Reduce a Tempo search reply to per-group statistics, and say if anything was cut.

    Args:
        data: The decoded reply.
        request: The request that produced it.

    Returns:
        The result for the model: the bounds, ``complete`` with its reasons, and the groups,
        largest first. Percentiles are over ``spans_used`` only.

    Raises:
        ToolExecutionError: ``traces`` is present and is not a list.
    """
    traces = data.get("traces", [])
    if not isinstance(traces, list):
        raise ToolExecutionError("Tempo returned a reply of an unexpected shape.")
    reasons: dict[str, None] = {}
    if len(traces) >= request.limit:
        reasons["trace limit reached: more traces can match; narrow the window or raise limit"] = (
            None
        )
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        done, total = _as_int(metrics.get("completedJobs")), _as_int(metrics.get("totalJobs"))
        if done is not None and total is not None and done < total:
            reasons["the search stopped before all blocks were read"] = None

    timings = TEMPO_SPANS[request.span].timings
    groups: dict[tuple[str, ...], _Group] = {}
    seen: set[tuple[str, str]] = set()
    used = 0
    for index, trace in enumerate(traces[: request.limit]):
        span_sets = _trace_span_sets(trace) if isinstance(trace, dict) else None
        if span_sets is None:
            reasons["a trace had no span set"] = None
            continue
        for span_set in span_sets:
            spans = span_set.get("spans", [])
            spans = spans if isinstance(spans, list) else []
            matched = _as_int(span_set.get("matched"))
            if matched is None or matched < 0:
                reasons["a span set had no valid match count"] = None
            elif matched > len(spans):
                reasons["a span set held more matching spans than it returned"] = None
            for span in spans:
                if not isinstance(span, dict):
                    reasons["a span was not an object"] = None
                    continue
                span_id = str(span.get("spanID", ""))
                if span_id and (str(index), span_id) in seen:
                    continue
                seen.add((str(index), span_id))
                if used >= _TEMPO_MAX_SPANS:
                    reasons[f"span cap of {_TEMPO_MAX_SPANS} reached"] = None
                    break
                nanos = _as_int(span.get("durationNanos"))
                if nanos is None or nanos < 0:
                    reasons["a span had no valid duration"] = None
                    continue
                attributes = _span_attributes(span)
                key = tuple(_label(attributes.get(TEMPO_GROUP_KEYS[k])) for k in request.group_by)
                group = groups.get(key)
                if group is None:
                    if len(groups) >= _TEMPO_MAX_GROUPS:
                        reasons[f"more than {_TEMPO_MAX_GROUPS} groups; the rest were dropped"] = (
                            None
                        )
                        continue
                    group = groups[key] = _Group()
                used += 1
                group.durations_ms.append(nanos / 1_000_000)
                group.errors += attributes.get(_TEMPO_STATUS_KEY) == "error"
                for name, attribute in _TEMPO_TOKENS.items():
                    value = attributes.get(attribute)
                    group.tokens[name] += value if isinstance(value, int) else 0
                for prefix, attribute in timings.items():
                    value = attributes.get(attribute)
                    if isinstance(value, int):
                        group.timings.setdefault(prefix, []).append(float(value))

    ranked = sorted(groups.items(), key=lambda item: (-len(item[1].durations_ms), item[0]))
    out: dict[str, object] = {
        "source": "tempo",
        "span": request.span,
        "since": request.window,
        "group_by": list(request.group_by),
        "trace_limit": request.limit,
        "traces_returned": len(traces),
        "spans_used": used,
        "complete": not reasons,
        "percentiles_over": "spans_used",
        "groups": [
            group.summary(dict(zip(request.group_by, key, strict=True))) for key, group in ranked
        ],
    }
    if reasons:
        out["sample"] = True
        out["incomplete_reasons"] = list(reasons)
    return out


async def _read_tempo(
    request: _TempoRequest, *, trace_id: str, session_id: str | None
) -> Mapping[str, object]:
    """Send the one search request and read the reply within the size and time bounds.

    Args:
        request: The validated request.
        trace_id: For logging.
        session_id: For logging.

    Returns:
        The decoded reply object.

    Raises:
        ToolExecutionError: Tempo cannot be reached, times out, rejects the query, or replies
            with more than 16 MB or with something other than a JSON object.
    """
    url = f"{settings.tempo_url.rstrip('/')}/api/search"
    body = bytearray()
    try:
        async with asyncio.timeout(_CLIENT_TIMEOUT_S):
            async with create_guarded_http_client(timeout=_CLIENT_TIMEOUT_S) as client:
                async with client.stream("GET", url, params=dict(request.params)) as response:
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _TEMPO_MAX_BYTES:
                            raise ToolExecutionError(
                                "Tempo's reply is too large. Narrow the window, add a role or "
                                "model filter, or lower limit."
                            )
    except EgressBlockedError as exc:
        raise ToolExecutionError("Tempo is blocked by the egress guard.") from exc
    except httpx.ConnectError as exc:
        log.error("query_telemetry_tempo_connect_failed", trace_id=trace_id, session_id=session_id)
        raise ToolExecutionError("Cannot connect to Tempo. Is the tempo service running?") from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        log.error("query_telemetry_tempo_timeout", trace_id=trace_id, session_id=session_id)
        raise ToolExecutionError("Tempo request timed out.") from exc
    except httpx.HTTPError as exc:
        log.error(
            "query_telemetry_tempo_failed",
            trace_id=trace_id,
            session_id=session_id,
            error=type(exc).__name__,
        )
        raise ToolExecutionError("Tempo request failed.") from exc

    if response.is_error:
        # A 400 or 422 describes the query, which the tool built. Any other body (a proxy
        # page, a server error) is not checked text, so the model gets the status only.
        if response.status_code in _TEMPO_QUERY_ERROR_STATUSES:
            reason = bytes(body[:_ERROR_CHARS]).decode("utf-8", errors="replace")
        else:
            reason = "no detail passed on for this status"
        log.warning(
            "query_telemetry_tempo_rejected",
            trace_id=trace_id,
            session_id=session_id,
            status=response.status_code,
            reason=reason,
        )
        raise ToolExecutionError(
            f"Tempo rejected the query (HTTP {response.status_code}): {reason}"
        )
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ToolExecutionError("Tempo returned a reply that is not JSON.") from exc
    if not isinstance(data, dict):
        raise ToolExecutionError("Tempo returned a reply that is not a JSON object.")
    return data


async def _latency(args: Mapping[str, object], *, ctx: TraceContext) -> dict[str, object]:
    """Run the ``latency`` action: one bounded Tempo search, summarised per group.

    Args:
        args: Every argument the model passed, by name.
        ctx: Trace context for logging.

    Returns:
        The per-group statistics (see ``_summarise_tempo``).

    Raises:
        ToolExecutionError: The request is refused (nothing was sent), or Tempo fails.
    """
    try:
        _refuse_foreign_args(_LATENCY, args, _ES_ONLY_ARGS)
        request = _build_tempo_request(
            args.get("since"),
            args.get("span"),
            args.get("group_by"),
            args.get("role"),
            args.get("model"),
            args.get("limit"),
            now=time.time(),
        )
    except ToolExecutionError as exc:
        log.warning(
            "query_telemetry_refused",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            reason=str(exc),
        )
        raise
    log.info(
        "query_telemetry_tempo_started",
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        span=request.span,
        since=request.window,
        limit=request.limit,
    )
    data = await _read_tempo(request, trace_id=ctx.trace_id, session_id=ctx.session_id)
    return _summarise_tempo(data, request)


async def query_telemetry_executor(
    action: str = "",
    index: str = "",
    query: object = None,
    aggs: object = None,
    fields: object = None,
    size: object = None,
    since: object = None,
    span: object = None,
    group_by: object = None,
    role: object = None,
    model: object = None,
    limit: object = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Run one bounded, read-only search, count or latency summary over the own telemetry.

    Args:
        action: ``search`` or ``count`` (Elasticsearch), or ``latency`` (Tempo).
        index: An index family: ``agent-logs``, ``agent-topology`` or
            ``agent-monitors-slm-health``. Search and count only.
        query: Optional filter from the supported query operators. Default: all documents.
        aggs: Optional aggregations for ``search``.
        fields: Optional field names to return for each hit.
        size: Hits to return. Default 20, at most 50.
        since: Look-back window, a number and ``m``, ``h`` or ``d``. Search and count:
            default ``24h``, at most ``30d``. Latency: required, at most ``7d``.
        span: Latency only: a key of ``TEMPO_SPANS``. Default ``model_call``.
        group_by: Latency only: keys of ``TEMPO_GROUP_KEYS``. Default role and model.
        role: Latency only: an exact role to keep.
        model: Latency only: an exact model id to keep.
        limit: Latency only: traces to read. Default 200, at most 500.
        ctx: Trace context for logging.

    Returns:
        For ``count``, ``{"count": n}``. For ``search``, ``total``, ``hits`` and, when
        asked, ``aggregations``. A reply over 20,000 characters is cut and marked
        ``truncated``. For ``latency``, per-group statistics and ``complete``.

    Raises:
        ToolExecutionError: The request is refused (the message starts with ``refused:`` and
            nothing was sent), the store rejects the query, or it cannot be reached.
    """
    trace_id = ctx.trace_id
    session_id = ctx.session_id
    args: dict[str, object] = {
        "index": index,
        "query": query,
        "aggs": aggs,
        "fields": fields,
        "size": size,
        "since": since,
        "span": span,
        "group_by": group_by,
        "role": role,
        "model": model,
        "limit": limit,
    }
    if action == _LATENCY:
        return await _latency(args, ctx=ctx)
    try:
        _refuse_foreign_args(action, args, _TEMPO_ONLY_ARGS)
        path, body = _build_request(action, index, query, aggs, fields, size, since)
    except ToolExecutionError as exc:
        log.warning(
            "query_telemetry_refused", trace_id=trace_id, session_id=session_id, reason=str(exc)
        )
        raise

    log.info(
        "query_telemetry_started",
        trace_id=trace_id,
        session_id=session_id,
        action=action,
        index=index,
        since=_parse_since(since),
    )
    url = f"{settings.elasticsearch_url.rstrip('/')}{path}"
    try:
        async with create_guarded_http_client(timeout=_CLIENT_TIMEOUT_S) as client:
            response = await client.post(
                url,
                json=body,
                params={"ignore_unavailable": "true", "allow_no_indices": "true"},
            )
    except EgressBlockedError as exc:
        raise ToolExecutionError("Elasticsearch is blocked by the egress guard.") from exc
    except httpx.ConnectError as exc:
        log.error("query_telemetry_connect_failed", trace_id=trace_id, session_id=session_id)
        raise ToolExecutionError(
            "Cannot connect to Elasticsearch. Is the elasticsearch service running?"
        ) from exc
    except httpx.TimeoutException as exc:
        log.error("query_telemetry_timeout", trace_id=trace_id, session_id=session_id)
        raise ToolExecutionError("Elasticsearch request timed out.") from exc
    except httpx.HTTPError as exc:
        log.error(
            "query_telemetry_failed",
            trace_id=trace_id,
            session_id=session_id,
            error=type(exc).__name__,
        )
        raise ToolExecutionError("Elasticsearch request failed.") from exc

    if response.is_error:
        reason = _short_reason(response)
        log.warning(
            "query_telemetry_rejected",
            trace_id=trace_id,
            session_id=session_id,
            status=response.status_code,
            reason=reason,
        )
        raise ToolExecutionError(
            f"Elasticsearch rejected the query (HTTP {response.status_code}): {reason}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolExecutionError("Elasticsearch returned a reply that is not JSON.") from exc
    if not isinstance(data, dict):
        raise ToolExecutionError("Elasticsearch returned a reply that is not a JSON object.")
    return _shape_result(action, data)
