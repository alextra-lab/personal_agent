"""Session digest producer (ADR-0124 Phase 0, FRE-947 — supersedes FRE-347's summariser).

Emits the two artifacts described in :mod:`personal_agent.memory.session_digest`
— a ``session_label`` and a structured ``SessionDigest`` — from one model call over
a session's canonical captures.

Three axes were corrected against the FRE-347 producer this replaces:

**When it runs.** Not on every consolidation pass. The summary is a *derived read
model*, so the trigger is the debounced idle sweep in
``brainstem/scheduler.py`` — this module is a pure function of the captures it is
handed and owns no scheduling.

**What it reads.** Everything, unconditionally. The old 200-character user/assistant
excerpts and 20-turn cap are gone, and full tool payloads are now included. The clip
was symmetric and wrong in an asymmetric way: measured user messages sit at p50 58
chars — already below the cut — while assistant responses sit at p50 1,847, so it
barely touched the user text while discarding roughly 89% of the assistant text,
which is where a session's outcome lives. Tool results were absent entirely, so the
producer could only ever restate narration.

Egress follows from that: whatever this reads goes to whichever model backs the
``session_summary`` role, so "do these bytes leave the machine?" is answered once in
configuration by that role's binding, not re-decided per session.

**What it emits.** Four optional slots with per-item provenance, validated. Never
silently truncated input: oversized sessions are rejected **before** any model call,
so a doomed session costs a token estimate and a log line rather than a model call.
Unmarked truncation is the one thing this producer must never do — a summariser told
to check narration against evidence, handed a silently shortened payload, reads
absence of evidence as evidence of absence and writes a false accusation into the
graph that nothing downstream can distinguish from a real catch.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import orjson

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.config import load_model_config, resolve_role_model_key
from personal_agent.config.settings import get_settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client import InferenceSlotTimeout, LLMTimeout, LocalLLMClient, ModelRole
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.memory.session_digest import (
    MAX_LABEL_CHARS,
    Correction,
    DigestItem,
    Locator,
    SessionDigest,
    SessionSummaryOutcome,
    SessionSummaryStatus,
    SummaryFailureReason,
    UnresolvedItem,
    digest_token_count,
    validate_digest_provenance,
)
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import SystemTraceContext

log = get_logger(__name__)

#: Minimum turns before a session earns a digest (ADR-0124 D2).
#: Every ``Turn`` already carries its own summary and key entities, so a one-turn
#: session digest is not merely redundant — it is a **diverging artifact describing
#: the same event**, free to contradict the record it duplicates. Genuine
#: session-level relation ("A was rejected after X was discovered; B was chosen")
#: first appears at two turns. Removes ~51% of generations.
MIN_TURNS_FOR_DIGEST = 2

#: Output + system-prompt headroom held back from the model's context window when
#: deciding whether input is oversized.
_OUTPUT_RESERVE_TOKENS = 2_048

#: Used only when the resolved deployment declares no ``context_length``. Chosen
#: small deliberately: an unknown limit must not fail *open* into a model call that
#: the provider then rejects, because that is the silent-failure mode this check exists
#: to remove.
_FALLBACK_CONTEXT_LENGTH = 32_000

#: One retry on a validation failure (ADR-0124 Risks — "validator with one retry").
_MAX_GENERATION_ATTEMPTS = 2

_SYSTEM_PROMPT = """\
You write structured session digests for an agent's long-term memory.

A digest encodes the EPISTEMIC STATE LEFT BEHIND by a session. It does not retell \
the session. Never narrate what happened turn by turn.

Emit JSON only, matching this shape exactly:

{
  "label": "<= 90 characters, a distinguishing noun phrase",
  "digest": {
    "established": [item, ...],
    "decisions":   [item, ...],
    "unresolved":  [item, ...],
    "corrections": [correction, ...]
  }
}

item = {
  "text":  "<the item>",
  "basis": "tool_evidence" | "user_statement" | "assistant_reasoning" | "mixed",
  "span":    "<verbatim text copied from the session>",   // REQUIRED if basis is tool_evidence
  "locator": {"capture_id": "<capture id>", "field": "<field>"}  // REQUIRED with span
}

correction = item + {
  "tier": "A" | "B",
  "evidence_span":    "<verbatim supporting evidence>",
  "evidence_locator": {"capture_id": "...", "field": "..."}
}

`field` must be exactly one of: user_text | assistant_text | tool_result[N].output \
| tool_result[N].error, using the capture id and tool index shown in the transcript.

SLOTS — all optional, omit any that has nothing to say. Empty is a valid digest.
- established: facts and observations that survived the interaction. Filter this \
hardest; it is the slot most at risk of re-deriving facts that are already stored \
elsewhere.
- decisions: conclusions that materially constrain future reasoning, INCLUDING \
rejected alternatives and the reasons they were rejected.
- unresolved: unfinished state a future reader could wrongly treat as settled.
- corrections: see below. Usually empty, and that scarcity is correct.

SPANS — a span must be copied VERBATIM from the exact field its locator names. Do \
not paraphrase it, and do not cite a span that lives somewhere else in the session. \
An item you cannot cite this way must be tagged with a non-evidence basis or omitted.

CORRECTIONS — precision above all. A missed error is recoverable from the raw \
evidence; a false error writes self-confirming state into memory. Assert only:
- Tier A, direct contradiction: authoritative evidence contradicts the SAME \
proposition the assistant asserted. Cite the contradicted assistant claim in \
span/locator AND the contradicting evidence in evidence_span/evidence_locator.
- Tier B, explicit evidenced self-correction: the assistant corrected the record \
within the session and evidence supports the correction. Cite the self-correction \
in span/locator and its supporting evidence in evidence_span/evidence_locator.

NEVER assert a correction for: weak or partial conflict, a failed or incomplete \
tool call, text with several defensible readings, state that legitimately changed \
over time, or disagreement with a subjective judgment or recommendation. Those \
belong in unresolved, or are omitted. NEVER infer an error from absent evidence.

Before asserting Tier A, apply the SAME-PROPOSITION test explicitly. A correction \
requires the evidence to contradict the very thing the assistant asserted, not a \
neighbouring one. In particular:
- A JUDGMENT is not a factual claim. "I would treat this as low priority", "I \
recommend X", "that seems fine", a severity or priority assessment, a suggested \
course of action — none of these are contradicted by data carrying a different \
label or rating. The assistant asserted what it would DO; the data reports what \
something IS. Those are different propositions, so this is Tier C, never Tier A.
- An APPROXIMATION is not a wrong number. "about two thousand" against 2,276, or \
"around 300ms" against 310ms, agree.
- A SCOPED claim is not a universal one. "the ones I checked are healthy" is not \
contradicted by an unchecked index being unhealthy.
If you cannot name the single proposition that the assistant asserted and the \
evidence denies, in those words, there is no Tier-A correction to make.

LENGTH — include an item only if its future value exceeds the cost of displacing \
retrieved evidence. Aim for about __TARGET_TOKENS__ tokens across the whole digest \
and never exceed __MAX_TOKENS__. Digest length is NOT proportional to turn count.

Do not restate turn counts, durations or tool-call tallies: those are computed \
separately and must not be regenerated in prose.
"""

_TRANSCRIPT_HEADER = """\
Session {session_id}: {turn_count} turns, {started} to {ended}.
{evidence_notes}
Full transcript follows. Nothing has been truncated.
"""


def _tool_block(index: int, result: dict[str, Any]) -> tuple[str, list[str]]:
    """Render one tool invocation, and report any evidence it is missing.

    Args:
        index: Position of this result within its turn, used by locators.
        result: The captured tool result.

    Returns:
        The rendered block and a list of missing-evidence notes.
    """
    notes: list[str] = []
    name = result.get("tool_name", "unknown_tool")
    status = "ok" if result.get("success") else "failed"

    if "arguments" in result:
        raw = result["arguments"]
        arguments = raw if isinstance(raw, str) else orjson.dumps(raw).decode()
    else:
        # Captures written before FRE-947 carry no arguments. Say so rather than
        # printing nothing: an unexplained gap is what a summariser turns into a
        # fabricated contradiction.
        arguments = "<not recorded for this session>"
        notes.append(f"tool arguments were not recorded for {name}")

    output = result.get("output")
    if output is None:
        payload = "<no payload recorded>"
        if status == "ok":
            notes.append(f"the payload of a successful {name} call was not recorded")
    elif isinstance(output, str):
        payload = output
    else:
        payload = orjson.dumps(output, default=str).decode()

    error = result.get("error") or "(none)"

    block = (
        f"  [{index}] {name}  status={status}\n"
        f"      arguments: {arguments}\n"
        f"      error: {error}\n"
        f"      output: {payload}"
    )
    return block, notes


def _format_turn(index: int, capture: TaskCapture) -> tuple[str, list[str]]:
    """Render one turn in full, and report any evidence it is missing."""
    notes: list[str] = []
    parts = [
        f"--- Turn {index} (capture_id: {capture.trace_id}) ---",
        "User:",
        capture.user_message or "",
        "",
        "Assistant:",
        capture.assistant_response or "",
    ]
    if capture.assistant_response is None:
        notes.append(f"turn {index} has no recorded assistant response")

    if capture.tool_results:
        parts.append("")
        parts.append("Tool invocations:")
        for i, result in enumerate(capture.tool_results):
            block, block_notes = _tool_block(i, result)
            parts.append(block)
            notes.extend(block_notes)
    elif capture.tools_used:
        # The turn used tools but their results are absent from the capture.
        parts.append("")
        parts.append(
            f"Tool invocations: <not recorded; tools used: {', '.join(capture.tools_used)}>"
        )
        notes.append(f"turn {index} used tools whose results were not recorded")

    return "\n".join(parts), notes


def build_prompt(captures: Sequence[TaskCapture]) -> str:
    """Assemble the full-fidelity transcript prompt (ADR-0124 D2).

    Every turn, the complete user and assistant text of each, and every tool
    invocation with its name, arguments, status, error and payload. There is no
    conditional path that legitimately omits a payload, so any omission is a defect
    rather than a policy — which is why anything genuinely missing from the capture
    is *declared* at the top instead of silently skipped.

    Args:
        captures: The session's captures, ordered oldest first.

    Returns:
        The assembled prompt.
    """
    rendered: list[str] = []
    all_notes: list[str] = []
    for i, capture in enumerate(captures, start=1):
        block, notes = _format_turn(i, capture)
        rendered.append(block)
        all_notes.extend(notes)

    if all_notes:
        # Absence of evidence is not evidence of absence — say so explicitly, or the
        # summariser reads a gap in its own input as the assistant having made
        # something up.
        unique = list(dict.fromkeys(all_notes))
        evidence_notes = (
            "\nSOME EVIDENCE IS UNAVAILABLE for this session:\n"
            + "\n".join(f"  - {n}" for n in unique)
            + "\nDo not infer a contradiction, an error, or an omission from the absence of\n"
            "this evidence. Corrections that rest on tool status, tool errors, or the\n"
            "session's own text remain legitimate.\n"
        )
    else:
        evidence_notes = ""

    header = _TRANSCRIPT_HEADER.format(
        session_id=captures[0].session_id,
        turn_count=len(captures),
        started=captures[0].timestamp.isoformat(),
        ended=captures[-1].timestamp.isoformat(),
        evidence_notes=evidence_notes,
    )
    return header + "\n" + "\n\n".join(rendered)


def _system_prompt() -> str:
    """Render the system prompt with the configured length bounds.

    Substituted rather than ``format``-ed: the prompt embeds a literal JSON schema,
    and every brace in it would have to be doubled to survive ``str.format``.
    """
    settings = get_settings()
    return _SYSTEM_PROMPT.replace(
        "__TARGET_TOKENS__", str(settings.session_digest_target_tokens)
    ).replace("__MAX_TOKENS__", str(settings.session_digest_max_tokens))


def _strip_fences(content: str) -> str:
    """Remove a ```json fence if the model wrapped its output in one."""
    text = content.strip()
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        return text[start:end].strip() if end != -1 else text[start:].strip()
    if text.startswith("```"):
        end = text.find("```", 3)
        return text[3:end].strip() if end != -1 else text[3:].strip()
    return text


def _parse_locator(raw: object) -> Locator | None:
    if not isinstance(raw, dict):
        return None
    capture_id = raw.get("capture_id")
    field = raw.get("field")
    if not isinstance(capture_id, str) or not isinstance(field, str):
        return None
    return Locator(capture_id=capture_id, field=field)


def _parse_item(raw: object) -> DigestItem:
    """Parse one slot item. Raises ValueError on anything unusable."""
    if not isinstance(raw, dict):
        raise ValueError(f"item is not an object: {raw!r}")
    text = raw.get("text")
    basis = raw.get("basis")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("item has no text")
    if basis not in ("tool_evidence", "user_statement", "assistant_reasoning", "mixed"):
        raise ValueError(f"item has invalid basis: {basis!r}")
    span = raw.get("span")
    return DigestItem(
        text=text.strip(),
        basis=basis,
        span=span if isinstance(span, str) else None,
        locator=_parse_locator(raw.get("locator")),
    )


def _parse_correction(raw: object) -> Correction:
    """Parse one correction. Raises ValueError on anything unusable."""
    item = _parse_item(raw)
    assert isinstance(raw, dict)  # _parse_item already rejected non-dicts
    tier = raw.get("tier")
    if tier not in ("A", "B"):
        raise ValueError(f"correction has invalid tier: {tier!r}")
    evidence_span = raw.get("evidence_span")
    evidence_locator = _parse_locator(raw.get("evidence_locator"))
    if not isinstance(evidence_span, str) or evidence_locator is None:
        raise ValueError("correction is missing its evidence span or locator")
    return Correction(
        text=item.text,
        basis=item.basis,
        span=item.span,
        locator=item.locator,
        tier=tier,
        evidence_span=evidence_span,
        evidence_locator=evidence_locator,
    )


def parse_model_output(content: str, *, ended_at: datetime) -> tuple[str, SessionDigest]:
    """Parse and shape-check the model's JSON.

    ``unresolved`` items are stamped with the session's ``ended_at`` here rather
    than being asked of the model: it is computable state, and computed state is
    never regenerated in prose (ADR-0124 D3), so it cannot be hallucinated.

    Args:
        content: Raw model output, possibly fenced.
        ended_at: The session's last-turn timestamp, stamped onto unresolved items.

    Returns:
        The label and the parsed digest.

    Raises:
        ValueError: If the output is not usable JSON of the required shape.
    """
    try:
        parsed = orjson.loads(_strip_fences(content))
    except orjson.JSONDecodeError as e:
        raise ValueError(f"output is not valid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError("output is not a JSON object")

    label = parsed.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("output has no label")
    label = label.strip()
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"label is {len(label)} chars, limit is {MAX_LABEL_CHARS}")

    raw_digest = parsed.get("digest")
    if not isinstance(raw_digest, dict):
        raise ValueError("output has no digest object")

    def _slot(name: str) -> list[object]:
        value = raw_digest.get(name, [])
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"digest slot {name!r} is not a list")
        return value

    digest = SessionDigest(
        established=[_parse_item(r) for r in _slot("established")],
        decisions=[_parse_item(r) for r in _slot("decisions")],
        unresolved=[
            UnresolvedItem(**_parse_item(r).model_dump(), as_of=ended_at)
            for r in _slot("unresolved")
        ],
        corrections=[_parse_correction(r) for r in _slot("corrections")],
    )
    return label, digest


def _estimate_input_tokens(prompt: str) -> int:
    return estimate_tokens(prompt) + estimate_tokens(_system_prompt())


def _input_token_limit(context_length: int | None) -> int:
    return (context_length or _FALLBACK_CONTEXT_LENGTH) - _OUTPUT_RESERVE_TOKENS


async def _call_model(
    prompt: str,
    *,
    role_name: str,
    provider: str | None,
    session_id: str,
) -> str:
    """Dispatch one generation call. Raises on any client-level failure."""
    if provider is not None:
        from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

        # budget_role stays captains_log: ADR-0124 D2 defers splitting cost
        # attribution as a separate, smaller decision.
        cloud_client = get_llm_client_for_key(role_name, budget_role="captains_log")
        response: dict[str, Any] = await cloud_client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": prompt}],
            system_prompt=_system_prompt(),
            trace_ctx=SystemTraceContext.new("session_summary", session_id=session_id),
        )
        return response.get("content", "") or ""

    from personal_agent.llm_client.concurrency import InferencePriority  # noqa: PLC0415

    local_client = LocalLLMClient()
    llm_response = await local_client.respond(
        role=ModelRole.from_str(role_name) or ModelRole.SUB_AGENT,
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ],
        system_prompt=None,
        tools=None,
        max_tokens=2048,
        max_retries=0,
        timeout_s=120.0,
        priority=InferencePriority.BACKGROUND,
        priority_timeout=120.0,
        trace_ctx=SystemTraceContext.new("session_summary", session_id=session_id),
    )
    return llm_response.get("content", "") or ""


def _failed(
    reason: SummaryFailureReason,
    *,
    session_id: str,
    trace_id: str,
    detail: str = "",
) -> SessionSummaryOutcome:
    """Emit the failure event and build the failure outcome.

    Loud by construction: every failure path goes through here, so a session can
    never fail silently — which matters because a failure leaves the session dirty
    and eligible for retry rather than marking it clean.
    """
    log.warning(
        "session_summary_failed",
        session_id=session_id,
        trace_id=trace_id,
        failure_reason=reason.value,
        detail=detail,
    )
    return SessionSummaryOutcome(status=SessionSummaryStatus.FAILED, failure_reason=reason)


async def generate_session_digest(
    captures: Sequence[TaskCapture],
    *,
    session_id: str,
    ended_at: datetime,
    trace_id: str = "session_summary_sweep",
) -> SessionSummaryOutcome:
    """Generate a session's label and structured digest from its captures.

    Regenerates **wholesale** — never by patching a previous digest. Wholesale
    regeneration is ``f(canonical captures)``, which is self-correcting when prompts
    or models improve; incremental patching would summarise a summary, so early
    detail decays and an early error becomes a permanent input to every later pass.

    Args:
        captures: The session's captures, ordered oldest first.
        session_id: Session identifier, for logging and locator context.
        ended_at: The session's last-turn timestamp. Stamped onto unresolved items
            so a consumer can say "as of that session, X was open" rather than
            asserting the present tense.
        trace_id: Trace identifier for log correlation.

    Returns:
        A :class:`SessionSummaryOutcome`. ``SKIPPED_BELOW_FLOOR`` and ``FAILED`` are
        distinct states, deliberately: only the former is a completed projection, and
        conflating them is what let a failure be written as a result.
    """
    if len(captures) < MIN_TURNS_FOR_DIGEST:
        log.info(
            "session_summary_skipped_below_floor",
            session_id=session_id,
            trace_id=trace_id,
            turn_count=len(captures),
        )
        return SessionSummaryOutcome(status=SessionSummaryStatus.SKIPPED_BELOW_FLOOR)

    settings = get_settings()
    model_config = load_model_config()
    role_name = resolve_role_model_key("session_summary")
    model_def = model_config.models.get(role_name)
    provider = model_def.provider if model_def else None

    prompt = build_prompt(captures)

    # Pre-dispatch, so a doomed session costs an estimate and a log line rather
    # than a model call (ADR-0124 AC-5). Never silently truncate.
    estimated_tokens = _estimate_input_tokens(prompt)
    limit = _input_token_limit(model_def.context_length if model_def else None)
    if estimated_tokens > limit:
        return _failed(
            SummaryFailureReason.OVERSIZED_INPUT,
            session_id=session_id,
            trace_id=trace_id,
            detail=f"estimated {estimated_tokens} input tokens exceeds limit {limit}",
        )

    started_at = time.perf_counter()
    log.info(
        "session_summary_started",
        session_id=session_id,
        trace_id=trace_id,
        turn_count=len(captures),
        role="session_summary",
        model_key=role_name,
        provider=provider,
        estimated_input_tokens=estimated_tokens,
    )

    last_validation_failure: tuple[SummaryFailureReason, str] | None = None

    for attempt in range(1, _MAX_GENERATION_ATTEMPTS + 1):
        try:
            content = await _call_model(
                prompt, role_name=role_name, provider=provider, session_id=session_id
            )
        except BudgetDenied as e:
            # Never terminal: transient by nature, so the session stays retryable.
            return _failed(
                SummaryFailureReason.BUDGET_DENIED,
                session_id=session_id,
                trace_id=trace_id,
                detail=f"{e.denial_reason} role={e.role} cap={e.cap} spend={e.current_spend}",
            )
        except (LLMTimeout, InferenceSlotTimeout) as e:
            return _failed(
                SummaryFailureReason.TIMEOUT,
                session_id=session_id,
                trace_id=trace_id,
                detail=str(e),
            )
        except Exception as e:  # noqa: BLE001 — a sweep must never crash the scheduler
            return _failed(
                SummaryFailureReason.MODEL_ERROR,
                session_id=session_id,
                trace_id=trace_id,
                detail=f"{type(e).__name__}: {e}",
            )

        if not content.strip():
            last_validation_failure = (SummaryFailureReason.EMPTY_OUTPUT, "model returned nothing")
            continue

        try:
            label, digest = parse_model_output(content, ended_at=ended_at)
        except ValueError as e:
            last_validation_failure = (SummaryFailureReason.SCHEMA_INVALID, str(e))
            continue

        violations = validate_digest_provenance(digest, captures)
        if violations:
            last_validation_failure = (
                SummaryFailureReason.SPAN_VALIDATION_FAILED,
                "; ".join(violations[:5]),
            )
            continue

        tokens = digest_token_count(digest)
        if tokens > settings.session_digest_max_tokens:
            last_validation_failure = (
                SummaryFailureReason.DIGEST_OVER_BUDGET,
                f"{tokens} tokens exceeds {settings.session_digest_max_tokens}",
            )
            continue

        log.info(
            "session_summary_generated",
            session_id=session_id,
            trace_id=trace_id,
            turn_count=len(captures),
            attempt=attempt,
            label_chars=len(label),
            digest_tokens=tokens,
            established=len(digest.established),
            decisions=len(digest.decisions),
            unresolved=len(digest.unresolved),
            # Monitored as a drift signal: corrections are expected to be scarce,
            # so a rising rate is the alarm, not the achievement.
            corrections=len(digest.corrections),
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
            model_key=role_name,
        )
        return SessionSummaryOutcome(
            status=SessionSummaryStatus.GENERATED, label=label, digest=digest
        )

    reason, detail = last_validation_failure or (
        SummaryFailureReason.EMPTY_OUTPUT,
        "no attempt produced usable output",
    )
    return _failed(reason, session_id=session_id, trace_id=trace_id, detail=detail)
