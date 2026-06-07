r"""FRE-453 — canonical eval set runner: 18 turn types through the route-trace instrument.

Drives the canonical eval set (``dataset.yaml`` — 7 pedagogical turn types per ADR-0084
§Open decisions §3 + 11 toolbox-coverage queries) through the live service ``/chat`` and
reads back each scored turn's **route-trace ledger row** (FRE-452 / ADR-0088 D6), producing
a structured JSON + markdown report against the result-type taxonomy
(``docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md``, FRE-451).

Measurement posture (spec §5 "[proposed — M2 validates]"): **every expectation is a
hypothesis.** Comparisons are reported as MATCH/MISMATCH findings and never gate; the
exit code reflects instrument health only (non-zero iff a case produced no route-trace
row or ``/chat`` errored). The first run is the behavioural baseline, not a pass/fail.

Three observation layers per case:

1. **Programmatic** — orchestration event, model path (two independent field
   comparisons), expected skills/tools, structural route-mismatch candidate
   (same semantics as the ledger's ``_LABEL_LIE_SQL``; the taxonomy §7.3 *label-lie*
   verdict itself is rubric-derived, not computed here).
2. **Measurement** (no verdicts) — thinking/tokens/cost/latency per case (spec §7.4).
3. **Backend static-prompt surfaces** (observational only — v0.1 prompts, never tuned):
   which surfaces fired (captain's-log capture, reflection, entity extraction,
   within-session compression, tool-result digest) within a bounded ES wait, plus a
   post-run sweep for scheduled surfaces (consolidation, insights). No expectations.

The pedagogical-outcome layer is **rubric capture**: the report renders each case's
expected outcome set + rubric criteria as a fillable checklist next to the captured
response text; the human pass over a live run is the M2 gate's second half.

Running this against the live stack is a **master post-deploy action** (fre481
precedent); the harness itself plus its unit tests are the build deliverable.

Usage::

    uv run python scripts/eval/fre453_canonical_evalset/harness.py \\
        --run-id fre453-baseline-01 --profile local \\
        --auth-email <loopback-eval-email>

Validity note: the ledger key is ``(trace_id, task_id)`` with per-topology rows planned
(ADR-0088 seam); ``get_by_trace_id`` selects by ``trace_id`` alone. This harness is built
against the current single-row-per-trace turn-level write — when per-topology rows land,
the read must filter ``task_id IS NULL``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
import structlog
import yaml  # type: ignore[import-untyped]

from personal_agent.config import get_settings
from personal_agent.observability.route_trace.classifier import (
    delegate_disposition_candidate,
)
from personal_agent.observability.route_trace.ledger import RouteTraceLedger
from personal_agent.observability.route_trace.types import RouteTraceRow

log = structlog.get_logger(__name__)

DEFAULT_CHAT_URL = "http://localhost:9001/chat"

# Orchestration events the programmatic classifier can actually emit (classifier.py —
# used/discarded are hybrid, refined by rubric per FRE-515). Dataset expectations are
# restricted to this subset.
CLASSIFIER_EMITTABLE_EVENTS: frozenset[str] = frozenset(
    {"primary_handled", "delegate_called", "fallback_triggered"}
)

# The 10 pedagogical outcomes (RESULT_TYPE_TAXONOMY_SPEC §4 / ADR-0084 §D4 — frozen;
# membership changes require an ADR-0084 revision, never an edit here).
PEDAGOGICAL_OUTCOMES: frozenset[str] = frozenset(
    {
        "recall_practiced",
        "concept_extracted",
        "principle_identified",
        "counterintuitive_finding_marked",
        "open_thread_preserved",
        "cross_connection_made",
        "field_note_emitted",
        "learner_state_updated",
        "synthesis_performed",
        "misalignment_detected",
    }
)

# The ticket's expected-model-path vocabulary (FRE-453). Evaluated as two independent
# field comparisons on the row (decomposition_strategy, model_role) — never composite.
MODEL_PATHS: frozenset[str] = frozenset(
    {"single_primary", "single_sub_agent", "hybrid", "delegate"}
)

# Backend static-prompt surfaces observed per case (v0.1 — observational only, no
# expectations). Each maps surface name → candidate event names in the logs indices.
# ``captains_log.capture`` is special-cased: it lives in the captures index as a
# document keyed by trace_id, not as a log event.
BACKGROUND_EVENT_SURFACES: Mapping[str, Sequence[str]] = {
    "captains_log.reflection": (
        "reflection_llm_response_received",
        "reflection_llm_raw_response",
    ),
    "memory.entity_extraction": ("entities_updated", "entity_extraction_started"),
    "orchestrator.compression": (
        "context_compression_triggered",
        "context_compression_completed",
    ),
    "orchestrator.tool_result_digest": ("tool_result_digest_created", "tool_result_digest"),
}

# Scheduled surfaces that cannot be tied to a single turn — swept once post-run over
# the run's wall-clock window.
SWEEP_EVENT_SURFACES: Mapping[str, Sequence[str]] = {
    "brainstem.consolidation": ("consolidation_triggered", "consolidation_completed"),
    "insights.engine": ("pattern_detected", "cost_anomaly_detected"),
}

Verdict = Literal["match", "mismatch"]


# ---------------------------------------------------------------------------
# Dataset model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedBlock:
    """Per-case hypothesis block (everything here is non-gating).

    Attributes:
        model_path: Ticket vocabulary path (``MODEL_PATHS``).
        orchestration_event: Expected taxonomy §3 event (classifier-emittable subset).
        pedagogical_outcomes: Expected §4 outcome set — rubric layer, never computed.
        task_type: Optional gateway-label hint (reported, not compared).
        skills: Skill names expected in ``skills_loaded`` (each compared separately).
        tools_any_of: Tool or family names — MATCH if any appears in ``tools_used``.
        tools_used_nonempty: Optional structural check that the turn used tools at all.
    """

    model_path: str
    orchestration_event: str
    pedagogical_outcomes: tuple[str, ...] = ()
    task_type: str | None = None
    skills: tuple[str, ...] = ()
    tools_any_of: tuple[str, ...] = ()
    tools_used_nonempty: bool = False


@dataclass(frozen=True)
class EvalCase:
    """One canonical eval case.

    Attributes:
        id: Unique snake_case identifier.
        tier: ``"canonical"`` (the pedagogical 7) or ``"coverage"`` (toolbox union).
        title: Human-readable title.
        note: Why the case exists / what it probes.
        setup_messages: Unscored context turns sent into the same session first.
        stimulus: The scored turn.
        expected: The hypothesis block.
        rubric: Human-judged criteria rendered as a fillable checklist.
        regression: What a regression looks like on this case.
    """

    id: str
    tier: str
    title: str
    note: str
    setup_messages: tuple[str, ...]
    stimulus: str
    expected: ExpectedBlock
    rubric: tuple[str, ...]
    regression: str


@dataclass(frozen=True)
class CoverageSpec:
    """Toolbox-coverage configuration (union enforcement lives in the unit tests).

    Attributes:
        allowlist_skills: Skills exempt from union coverage (explicit, not silent).
        allowlist_tools: Native tools exempt from union coverage.
        tool_families: Family name → tool-name prefix (MCP enforcement granularity).
    """

    allowlist_skills: tuple[str, ...] = ()
    allowlist_tools: tuple[str, ...] = ()
    tool_families: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalSet:
    """The loaded dataset: cases + coverage configuration."""

    cases: tuple[EvalCase, ...]
    coverage: CoverageSpec


def load_dataset(path: Path) -> EvalSet:
    """Load and validate the canonical eval set from YAML.

    Args:
        path: Path to ``dataset.yaml``.

    Returns:
        The parsed :class:`EvalSet`.

    Raises:
        ValueError: If the file has no ``cases`` list or a case is malformed.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or not raw.get("cases"):
        raise ValueError(f"No 'cases' found in {path}")
    cov_raw = raw.get("coverage") or {}
    coverage = CoverageSpec(
        allowlist_skills=tuple(cov_raw.get("allowlist_skills") or ()),
        allowlist_tools=tuple(cov_raw.get("allowlist_tools") or ()),
        tool_families=dict(cov_raw.get("tool_families") or {}),
    )
    cases: list[EvalCase] = []
    for c in raw["cases"]:
        exp = c.get("expected") or {}
        cases.append(
            EvalCase(
                id=str(c["id"]),
                tier=str(c["tier"]),
                title=str(c["title"]),
                note=str(c.get("note", "")),
                setup_messages=tuple(str(m) for m in (c.get("setup_messages") or ())),
                stimulus=str(c["stimulus"]),
                expected=ExpectedBlock(
                    model_path=str(exp["model_path"]),
                    orchestration_event=str(exp["orchestration_event"]),
                    pedagogical_outcomes=tuple(exp.get("pedagogical_outcomes") or ()),
                    task_type=exp.get("task_type"),
                    skills=tuple(exp.get("skills") or ()),
                    tools_any_of=tuple(exp.get("tools_any_of") or ()),
                    tools_used_nonempty=bool(exp.get("tools_used_nonempty", False)),
                ),
                rubric=tuple(str(r) for r in (c.get("rubric") or ())),
                regression=str(c.get("regression", "")),
            )
        )
    return EvalSet(cases=tuple(cases), coverage=coverage)


# ---------------------------------------------------------------------------
# Evaluator (pure — unit-tested)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One MATCH/MISMATCH comparison between an expectation and the row.

    Attributes:
        name: Comparison identifier (e.g. ``"orchestration_event"``, ``"skill:bash"``).
        expected: The hypothesised value, rendered as a string.
        actual: The observed value, rendered as a string.
        verdict: ``"match"`` or ``"mismatch"`` — a finding, never a gate.
    """

    name: str
    expected: str
    actual: str
    verdict: Verdict


@dataclass(frozen=True)
class CaseEvaluation:
    """All programmatic findings for one case.

    Attributes:
        case_id: The evaluated case id.
        findings: Ordered MATCH/MISMATCH findings.
        route_mismatch_candidate: Structural disagreement between the gateway-declared
            strategy and the actual orchestration event (ledger ``_LABEL_LIE_SQL``
            semantics). The taxonomy §7.3 label-lie verdict is rubric-derived, not this.
    """

    case_id: str
    findings: tuple[Finding, ...]
    route_mismatch_candidate: bool


def _normalize(name: str) -> str:
    """Normalize skill/tool naming (kebab-case vs snake_case) for comparison."""
    return name.replace("-", "_").lower()


def _model_path_findings(expected_path: str, row: RouteTraceRow) -> tuple[Finding, ...]:
    """Derive the two independent field comparisons for the expected model path."""
    strategy = row.decomposition_strategy or "none"
    role = row.model_role or "none"
    expectations: dict[str, tuple[str, str | None]] = {
        "single_primary": ("single", "primary"),
        "single_sub_agent": ("single", "sub_agent"),
        "hybrid": ("hybrid", None),
        "delegate": ("delegate", None),
    }
    want_strategy, want_role = expectations[expected_path]
    ok = strategy == want_strategy and (want_role is None or role == want_role)
    actual = f"strategy={strategy}, role={role}"
    return (
        Finding(
            name="model_path",
            expected=expected_path,
            actual=actual,
            verdict="match" if ok else "mismatch",
        ),
    )


def _route_mismatch_candidate(row: RouteTraceRow) -> bool:
    """Mirror the ledger's ``_LABEL_LIE_SQL`` structural heuristic in Python."""
    strategy = row.decomposition_strategy
    event = row.orchestration_event
    declared_expansion_but_primary = (
        strategy is not None and strategy != "single" and event == "primary_handled"
    )
    declared_single_but_delegated = strategy == "single" and event in (
        "delegate_called",
        "delegate_result_used",
        "delegate_result_discarded",
    )
    return declared_expansion_but_primary or declared_single_but_delegated


def evaluate_case(
    case: EvalCase,
    row: RouteTraceRow,
    tool_families: Mapping[str, str] | None = None,
) -> CaseEvaluation:
    """Compare one case's hypotheses against its route-trace row.

    Args:
        case: The eval case (expectations are hypotheses).
        row: The turn-level route-trace ledger row.
        tool_families: Family name → tool prefix map for ``tools_any_of`` entries.

    Returns:
        The case's MATCH/MISMATCH findings plus the structural route-mismatch flag.
    """
    families = dict(tool_families or {})
    findings: list[Finding] = [
        Finding(
            name="orchestration_event",
            expected=case.expected.orchestration_event,
            actual=row.orchestration_event,
            verdict=(
                "match"
                if row.orchestration_event == case.expected.orchestration_event
                else "mismatch"
            ),
        ),
        *_model_path_findings(case.expected.model_path, row),
    ]

    loaded = {_normalize(s) for s in row.skills_loaded}
    for skill in case.expected.skills:
        findings.append(
            Finding(
                name=f"skill:{skill}",
                expected="loaded",
                actual="loaded" if _normalize(skill) in loaded else "not loaded",
                verdict="match" if _normalize(skill) in loaded else "mismatch",
            )
        )

    if case.expected.tools_any_of:
        used = tuple(row.tools_used)
        hit = _any_tool_match(case.expected.tools_any_of, used, families)
        findings.append(
            Finding(
                name="tools_any_of",
                expected=f"any of {list(case.expected.tools_any_of)}",
                actual=f"used {list(used)}",
                verdict="match" if hit else "mismatch",
            )
        )

    if case.expected.tools_used_nonempty:
        findings.append(
            Finding(
                name="tools_used_nonempty",
                expected="tools_used non-empty",
                actual=f"{len(tuple(row.tools_used))} tools",
                verdict="match" if tuple(row.tools_used) else "mismatch",
            )
        )

    return CaseEvaluation(
        case_id=case.id,
        findings=tuple(findings),
        route_mismatch_candidate=_route_mismatch_candidate(row),
    )


def _any_tool_match(
    wanted: Sequence[str],
    used: Sequence[str],
    families: Mapping[str, str],
) -> bool:
    """True if any wanted tool (or family member) appears in the used tools."""
    used_norm = [_normalize(u) for u in used]
    for w in wanted:
        if w in families:
            prefix = _normalize(families[w])
            if any(u.startswith(prefix) for u in used_norm):
                return True
        elif _normalize(w) in used_norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Live drivers (/chat, ledger poll, background observer)
# ---------------------------------------------------------------------------


async def call_chat(
    client: httpx.AsyncClient,
    chat_url: str,
    message: str,
    session_id: str | None,
    auth_email: str | None,
    profile: str,
) -> tuple[str, str, str]:
    """POST one turn to ``/chat`` and return (response_text, session_id, trace_id).

    Args:
        client: Shared async HTTP client.
        chat_url: Service ``/chat`` URL.
        message: The user message.
        session_id: Existing session to continue, or ``None`` to create one.
        auth_email: CF-Access email to impersonate for loopback calls, or ``None``.
        profile: Model profile (``local`` or ``cloud``).

    Returns:
        Tuple of response text, session id, and trace id.
    """
    params: dict[str, str] = {"message": message, "profile": profile, "channel": "EVAL"}
    if session_id:
        params["session_id"] = session_id
    headers: dict[str, str] = {}
    if auth_email:
        headers["Cf-Access-Authenticated-User-Email"] = auth_email
    resp = await client.post(chat_url, params=params, headers=headers, timeout=1200.0)
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("response", "")), str(data["session_id"]), str(data["trace_id"])


async def wait_for_route_trace(
    ledger: RouteTraceLedger,
    trace_id: str,
    timeout_s: float,
    interval_s: float = 2.0,
) -> RouteTraceRow | None:
    """Poll the route-trace ledger until the turn's row lands or the timeout expires.

    Args:
        ledger: Connected ledger instance.
        trace_id: The scored turn's trace id.
        timeout_s: Hard timeout in seconds.
        interval_s: Poll interval in seconds.

    Returns:
        The row, or ``None`` on timeout (an instrument-health failure).
    """
    from uuid import UUID

    deadline = asyncio.get_event_loop().time() + timeout_s
    tid = UUID(trace_id)
    while asyncio.get_event_loop().time() < deadline:
        row = await ledger.get_by_trace_id(tid)
        if row is not None:
            return row
        await asyncio.sleep(interval_s)
    return None


@dataclass(frozen=True)
class SurfaceObservation:
    """One backend static-prompt surface observation (no verdicts — v0.1 baseline).

    Attributes:
        surface: Surface name (e.g. ``"captains_log.capture"``).
        status: ``"fired"`` / ``"not_fired_within_window"``.
        detail: Short excerpt or count for human review.
    """

    surface: str
    status: str
    detail: str


async def _es_count_events(
    es: httpx.AsyncClient,
    es_url: str,
    index: str,
    trace_id: str,
    event_names: Sequence[str],
) -> tuple[int, str]:
    """Count events matching any candidate name for the trace; return (count, sample).

    Uses the dual-key ``event``/``event_type`` match (local log files key the name under
    ``event``; ES-shaped events use ``event_type``).
    """
    should = [{"term": {"event_type": n}} for n in event_names]
    should += [{"term": {"event": n}} for n in event_names]
    body = {
        "size": 1,
        "query": {
            "bool": {
                "must": [{"term": {"trace_id": trace_id}}],
                "should": should,
                "minimum_should_match": 1,
            }
        },
    }
    try:
        resp = await es.post(f"{es_url}/{index}/_search", json=body, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        return 0, f"es_error: {exc}"
    total = int(data["hits"]["total"]["value"])
    sample = ""
    if data["hits"]["hits"]:
        src = data["hits"]["hits"][0]["_source"]
        sample = str({k: src.get(k) for k in ("event", "event_type", "@timestamp") if src.get(k)})
    return total, sample


async def observe_background_surfaces(
    es: httpx.AsyncClient,
    es_url: str,
    logs_index: str,
    captures_index: str,
    trace_id: str,
    wait_s: float,
) -> list[SurfaceObservation]:
    """Observe which backend static-prompt surfaces fired for a turn (best-effort).

    v0.1 posture: these prompts have never been tuned or explored — the observation is
    the deliverable; "not fired within window" is a recorded finding, not an error.

    Args:
        es: Async HTTP client for Elasticsearch.
        es_url: Elasticsearch base URL.
        logs_index: Logs index pattern (events).
        captures_index: Captain's-log captures index pattern (per-turn docs).
        trace_id: The scored turn's trace id.
        wait_s: Settle time before reading (async pipelines run post-turn).

    Returns:
        One observation per registered surface.
    """
    await asyncio.sleep(wait_s)
    observations: list[SurfaceObservation] = []

    # Captain's-log capture: a document in the captures index, not a log event.
    body = {"size": 1, "query": {"term": {"trace_id": trace_id}}}
    try:
        resp = await es.post(f"{es_url}/{captures_index}/_search", json=body, timeout=30.0)
        resp.raise_for_status()
        total = int(resp.json()["hits"]["total"]["value"])
        observations.append(
            SurfaceObservation(
                surface="captains_log.capture",
                status="fired" if total else "not_fired_within_window",
                detail=f"{total} capture doc(s)",
            )
        )
    except httpx.HTTPError as exc:
        observations.append(
            SurfaceObservation(
                "captains_log.capture", "not_fired_within_window", f"es_error: {exc}"
            )
        )

    for surface, event_names in BACKGROUND_EVENT_SURFACES.items():
        count, sample = await _es_count_events(es, es_url, logs_index, trace_id, event_names)
        observations.append(
            SurfaceObservation(
                surface=surface,
                status="fired" if count else "not_fired_within_window",
                detail=f"{count} event(s)" + (f" — {sample}" if sample else ""),
            )
        )
    return observations


async def sweep_scheduled_surfaces(
    es: httpx.AsyncClient,
    es_url: str,
    logs_index: str,
    window_start: str,
) -> list[SurfaceObservation]:
    """Post-run sweep: did the scheduled surfaces fire at all during the run window?

    Args:
        es: Async HTTP client for Elasticsearch.
        es_url: Elasticsearch base URL.
        logs_index: Logs index pattern.
        window_start: ISO timestamp of the run start.

    Returns:
        One observation per scheduled surface.
    """
    observations: list[SurfaceObservation] = []
    for surface, event_names in SWEEP_EVENT_SURFACES.items():
        should = [{"term": {"event_type": n}} for n in event_names]
        should += [{"term": {"event": n}} for n in event_names]
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [{"range": {"@timestamp": {"gte": window_start}}}],
                    "should": should,
                    "minimum_should_match": 1,
                }
            },
        }
        try:
            resp = await es.post(f"{es_url}/{logs_index}/_search", json=body, timeout=30.0)
            resp.raise_for_status()
            total = int(resp.json()["hits"]["total"]["value"])
        except httpx.HTTPError as exc:
            observations.append(
                SurfaceObservation(surface, "not_fired_within_window", f"es_error: {exc}")
            )
            continue
        observations.append(
            SurfaceObservation(
                surface=surface,
                status="fired" if total else "not_fired_within_window",
                detail=f"{total} event(s) in run window",
            )
        )
    return observations


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _measurement_cells(row: RouteTraceRow) -> str:
    """Render the §7.4 measurement columns for one case row."""
    return (
        f"| {row.gateway_label} | {row.task_type} | {row.thinking_enabled} | "
        f"{row.input_tokens} | {row.output_tokens} | {row.cost_authoritative_usd:.4f} | "
        f"{row.latency_total_ms} | {row.tool_iteration_count} | {row.sub_agent_count} |"
    )


def _disposition_block(row: RouteTraceRow) -> list[str]:
    """Render the FRE-515 delegate-disposition rubric block for a ``delegate_called`` row.

    Returns an empty list for every other orchestration event (``fallback_triggered``
    rows also carry subs but are their own terminal event — taxonomy §3.5). The candidate
    is a triage lean, never a verdict: the fillable checkboxes are the hybrid half
    (taxonomy §3.3/§3.4), completed during the human pass.
    """
    candidate = delegate_disposition_candidate(row)
    if candidate is None:
        return []
    lines = [
        "**Delegate disposition (FRE-515 — hybrid used/discarded rubric):**",
        "",
        f"- structural signals: passed_to_synthesis={row.delegate_result_passed_to_synthesis}, "
        f"error_type={row.error_type}, final_reply_chars={row.final_reply_chars}",
        f"- candidate (triage lean, never a verdict): `{candidate}`",
        "",
        "| sub task_id | success | summary_chars | output_chars | reply_overlap | error |",
        "|---|---|---|---|---|---|",
    ]
    for sub in row.sub_agents:
        lines.append(
            f"| {sub.get('task_id')} | {sub.get('success')} | {sub.get('summary_chars')} "
            f"| {sub.get('output_chars')} | {sub.get('reply_overlap', 'n/a')} "
            f"| {sub.get('error') or '—'} |"
        )
    lines += [
        "",
        "Rubric — read the response, then check exactly one:",
        "- Q1 (dependence): the reply contains content traceable to a sub-agent summary "
        "that is not in the primary's own tool results or prior context → used.",
        "- Q2 (explicit rejection): the reply explicitly rejects/contradicts the "
        "sub-agent output on review → discarded (explicit).",
        "- Q3 (implicit non-use): the reply is an error/apology or shows no dependence "
        "on the summaries → discarded (implicit).",
        "- Tie-break: partial incorporation counts as used.",
        "",
        "- [ ] `delegate_result_used` confirmed",
        "- [ ] `delegate_result_discarded` confirmed",
        "",
    ]
    return lines


def render_markdown(
    run_meta: Mapping[str, object],
    results: Sequence[Mapping[str, object]],
    evalset: EvalSet,
    sweep: Sequence[SurfaceObservation] = (),
) -> str:
    """Render the structured run report as markdown.

    Args:
        run_meta: Run metadata (run_id, profile, timestamp, …).
        results: Per-case dicts with keys ``case``, ``row``, ``evaluation``,
            ``response_text``, ``background`` (row may be ``None`` on timeout).
        evalset: The loaded eval set (for the coverage matrix).
        sweep: Post-run scheduled-surface observations.

    Returns:
        The markdown report.
    """
    lines: list[str] = [
        f"# FRE-453 canonical eval set — run `{run_meta['run_id']}`",
        "",
        f"- **profile**: `{run_meta['profile']}`  ·  **timestamp**: {run_meta['timestamp']}",
        f"- **cases**: {len(results)}  ·  posture: every expectation is a hypothesis —",
        "  MATCH/MISMATCH are findings, never gates (spec §5). First run = baseline.",
        "",
        "## Per-case findings",
        "",
    ]
    for r in results:
        case = r["case"]
        row = r["row"]
        evaluation = r["evaluation"]
        assert isinstance(case, EvalCase)
        lines.append(f"### `{case.id}` ({case.tier}) — {case.title}")
        lines.append("")
        if row is None:
            lines.append("**NO ROUTE-TRACE ROW (instrument-health failure)**")
            lines.append("")
            continue
        assert isinstance(row, RouteTraceRow)
        assert isinstance(evaluation, CaseEvaluation)
        lines.append(f"- trace_id: `{row.trace_id}`  ·  session_id: `{row.session_id}`")
        if evaluation.route_mismatch_candidate:
            lines.append(
                "- ⚠ **structural route-mismatch candidate** (declared strategy vs actual "
                "event — ledger heuristic; §7.3 label-lie verdict is rubric-derived)"
            )
        lines.append("")
        lines.append("| comparison | expected | actual | verdict |")
        lines.append("|---|---|---|---|")
        for f in evaluation.findings:
            mark = "MATCH" if f.verdict == "match" else "**MISMATCH**"
            lines.append(f"| {f.name} | {f.expected} | {f.actual} | {mark} |")
        lines.append("")
        lines.append(
            "| gateway_label | task_type | thinking | in_tok | out_tok | cost_usd "
            "| lat_ms | tool_iters | sub_agents |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        lines.append(_measurement_cells(row))
        lines.append("")
        lines.extend(_disposition_block(row))
        lines.append("**Rubric (human pass — fill after reading the response):**")
        for outcome in case.expected.pedagogical_outcomes:
            lines.append(f"- [ ] outcome `{outcome}` confirmed")
        if not case.expected.pedagogical_outcomes:
            lines.append(
                "- [ ] zero-outcome hypothesis holds (no defensible pedagogical outcome; spec §5.3)"
            )
        for criterion in case.rubric:
            lines.append(f"- [ ] {criterion}")
        lines.append(f"- Regression shape to watch: {case.regression}")
        background = r.get("background") or []
        lines.append("")
        lines.append("**Backend surfaces (v0.1 — observational):**")
        if background:
            for obs in background:
                assert isinstance(obs, SurfaceObservation)
                lines.append(f"- `{obs.surface}`: {obs.status} ({obs.detail})")
        else:
            lines.append("- (not observed this run)")
        lines.append("")

    lines += ["## Coverage matrix (claimed by the dataset)", ""]
    claimed_skills = sorted({s for c in evalset.cases for s in c.expected.skills})
    claimed_tools = sorted({t for c in evalset.cases for t in c.expected.tools_any_of})
    lines.append(f"- skills claimed: {', '.join(claimed_skills)}")
    lines.append(f"- tools/families claimed: {', '.join(claimed_tools)}")
    lines.append(
        f"- allowlisted (v1 exemptions): skills {list(evalset.coverage.allowlist_skills)}, "
        f"tools {list(evalset.coverage.allowlist_tools)}"
    )
    lines += ["", "## Backend surfaces — post-run sweep (scheduled)", ""]
    if sweep:
        for obs in sweep:
            lines.append(f"- `{obs.surface}`: {obs.status} ({obs.detail})")
    else:
        lines.append("- (sweep not run)")
    lines.append("")
    return "\n".join(lines)


def _result_to_json(r: Mapping[str, object]) -> dict[str, object]:
    """Serialize one per-case result for the JSON report."""
    case = r["case"]
    row = r["row"]
    evaluation = r["evaluation"]
    assert isinstance(case, EvalCase)
    out: dict[str, object] = {
        "case": asdict(case),
        "row": None if row is None else _jsonable(asdict(row)),
        "evaluation": None if evaluation is None else asdict(evaluation),
        "response_text": r.get("response_text", ""),
        "setup_trace_ids": r.get("setup_trace_ids", []),
        "background": [asdict(o) for o in (r.get("background") or [])],  # type: ignore[union-attr]
    }
    return out


def _jsonable(obj: object) -> object:
    """Best-effort conversion of UUID/datetime-bearing structures to JSON-safe values."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_case(
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    ledger: RouteTraceLedger,
    args: argparse.Namespace,
    es_url: str,
    logs_index: str,
    case: EvalCase,
    tool_families: Mapping[str, str],
) -> dict[str, object]:
    """Drive one case (setup turns + scored stimulus) and collect all observations."""
    session_id: str | None = None
    setup_trace_ids: list[str] = []
    for msg in case.setup_messages:
        _, session_id, setup_tid = await call_chat(
            http, args.chat_url, msg, session_id, args.auth_email, args.profile
        )
        setup_trace_ids.append(setup_tid)
        log.info("setup_turn_sent", case=case.id, trace_id=setup_tid)

    response_text, session_id, trace_id = await call_chat(
        http, args.chat_url, case.stimulus, session_id, args.auth_email, args.profile
    )
    log.info("scored_turn_sent", case=case.id, session_id=session_id, trace_id=trace_id)

    row = await wait_for_route_trace(ledger, trace_id, args.row_timeout_s)
    evaluation = None if row is None else evaluate_case(case, row, tool_families)
    if row is None:
        log.warning("route_trace_row_missing", case=case.id, trace_id=trace_id)
    else:
        mismatches = [f.name for f in evaluation.findings if f.verdict == "mismatch"]  # type: ignore[union-attr]
        log.info(
            "case_evaluated",
            case=case.id,
            trace_id=trace_id,
            mismatches=mismatches,
            route_mismatch_candidate=evaluation.route_mismatch_candidate,  # type: ignore[union-attr]
        )

    background = await observe_background_surfaces(
        es, es_url, logs_index, args.captures_index, trace_id, args.background_wait_s
    )
    return {
        "case": case,
        "row": row,
        "evaluation": evaluation,
        "response_text": response_text,
        "setup_trace_ids": setup_trace_ids,
        "background": background,
    }


async def amain(args: argparse.Namespace) -> int:
    """Run the eval set and write the report. Non-zero exit = instrument-health failure."""
    settings = get_settings()
    es_url = settings.elasticsearch_url.rstrip("/")
    prefix = args.logs_prefix or settings.elasticsearch_index_prefix
    logs_index = f"{prefix}-*"
    evalset = load_dataset(Path(args.dataset))
    cases = [c for c in evalset.cases if not args.case or c.id in args.case]
    if not cases:
        log.error("no_cases_selected", requested=args.case)
        return 2

    window_start = datetime.now(timezone.utc).isoformat()
    run_meta: dict[str, object] = {
        "run_id": args.run_id,
        "profile": args.profile,
        "timestamp": window_start,
        "chat_url": args.chat_url,
        "logs_index": logs_index,
        "case_count": len(cases),
    }

    ledger = RouteTraceLedger()
    await ledger.connect()
    if ledger.pool is None:
        log.error("route_trace_ledger_unavailable")
        return 2

    results: list[dict[str, object]] = []
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
            try:
                health = await http.get(args.chat_url.replace("/chat", "/health"), timeout=10.0)
                log.info("gateway_health", status=health.status_code)
            except httpx.HTTPError as exc:
                log.error("gateway_unreachable", url=args.chat_url, error=str(exc))
                return 2
            for case in cases:
                results.append(
                    await run_case(
                        http,
                        es,
                        ledger,
                        args,
                        es_url,
                        logs_index,
                        case,
                        evalset.coverage.tool_families,
                    )
                )
            sweep = await sweep_scheduled_surfaces(es, es_url, logs_index, window_start)
    finally:
        await ledger.disconnect()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.run_id}_{args.profile}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(
            {"meta": run_meta, "results": [_result_to_json(r) for r in results]},
            indent=2,
        )
    )
    (out_dir / f"{stem}.md").write_text(render_markdown(run_meta, results, evalset, sweep))
    log.info("report_written", out=str(out_dir / f"{stem}.md"), cases=len(results))

    missing = [r["case"].id for r in results if r["row"] is None]  # type: ignore[union-attr]
    if missing:
        log.error("instrument_health_failure", cases_without_rows=missing)
        return 1
    return 0


def main() -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(description="FRE-453 canonical eval set runner")
    p.add_argument("--run-id", required=True, help="Run identifier (tag in output).")
    p.add_argument(
        "--profile", default="local", choices=["local", "cloud"], help="Model profile/backend."
    )
    p.add_argument(
        "--dataset",
        default="scripts/eval/fre453_canonical_evalset/dataset.yaml",
        help="Path to dataset.yaml.",
    )
    p.add_argument("--chat-url", default=DEFAULT_CHAT_URL, help="Service /chat URL.")
    p.add_argument(
        "--auth-email", default=None, help="CF-Access email to impersonate for loopback calls."
    )
    p.add_argument(
        "--case",
        action="append",
        default=None,
        help="Run only this case id (repeatable). Default: all cases.",
    )
    p.add_argument(
        "--out",
        default="telemetry/evaluation/fre453-canonical-evalset",
        help="Output directory (gitignored; raw runs are never committed).",
    )
    p.add_argument(
        "--logs-prefix",
        default=None,
        help="ES logs index prefix (default: settings.elasticsearch_index_prefix).",
    )
    p.add_argument(
        "--captures-index",
        default="agent-captains-captures-*",
        help="Captain's-log captures index pattern.",
    )
    p.add_argument(
        "--row-timeout-s",
        type=float,
        default=120.0,
        help="Per-case route-trace row poll timeout.",
    )
    p.add_argument(
        "--background-wait-s",
        type=float,
        default=20.0,
        help="Settle time before observing async backend surfaces.",
    )
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
