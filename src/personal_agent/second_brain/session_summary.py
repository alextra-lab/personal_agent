"""Session digest producer (ADR-0124 Phase 0, FRE-947 — supersedes FRE-347's summariser).

Emits the two artifacts described in :mod:`personal_agent.memory.session_digest`
— a ``session_label`` and a structured ``SessionDigest`` — from one model call over
a session's canonical captures.

Three axes were corrected against the FRE-347 producer this replaces:

**When it runs.** Not on every consolidation pass. The summary is a *derived read
model*, so the trigger is the debounced idle sweep in
``brainstem/scheduler.py`` — this module is a pure function of the captures it is
handed and owns no scheduling.

**What it reads (Amendment B — conversation-only).** The whole conversation — full
user and full assistant text, every turn — and **nothing else**. The old 200-character
excerpts and 20-turn cap are gone: measured user messages sit at p50 58 chars — already
below the cut — while assistant responses sit at p50 1,847, so the clip barely touched
user text while discarding roughly 89% of the assistant text where a session's outcome
lives. Amendment A first removed tool *payloads*, keeping tool name/status/error as
metadata; Amendment B removes that metadata too — no tool name, status, error, argument
or payload reaches the prompt. The digest is the user's memory of the *conversation*;
tool output reached the user through the assistant's narration, and that narration is
what belongs in memory. (Tool results continue to be captured and stored — only their
delivery here stops. Invocation and success/failure counts are computable from those
stored captures if a future consumer needs them; no such computed property exists yet,
and none is fed to the generator.)

**What it emits.** Four optional slots with per-item provenance, validated. Never
silently truncated input: oversized sessions are rejected **before** any model call,
so a doomed session costs a token estimate and a log line rather than a model call.
Unmarked truncation is the one thing this producer must never do — a summariser handed
silently shortened input reads absence of evidence as evidence of absence and writes a
false accusation into the graph that nothing downstream can distinguish from a real catch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import orjson

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.config import load_model_config, resolve_role_model_key
from personal_agent.config.settings import get_settings
from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client import InferenceSlotTimeout, LLMTimeout, ModelRole
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
    ground_correction,
    trim_digest_to_budget,
)
from personal_agent.memory.session_digest_wire import (
    DIGEST_TOOL_NAME,
    digest_tool,
    digest_tool_choice,
)
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.spans import close_root_span, open_root_span
from personal_agent.telemetry.trace import SystemTraceContext, read_or_mint_trace_id

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

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

#: Output ceiling for the call. Unchanged by ADR-0124 Amendment C1 and never binding:
#: zero truncations across FRE-994's 100 calls, largest billed output 1,568. The
#: rendered digest is bounded at 400 tokens and the label at 90 characters, which
#: implies ~1,220 once the measured structural overhead is added — so this is real
#: headroom for the JSON envelope, and it keeps the cost gate's pre-call reservation
#: proportionate to the real spend.
_MAX_OUTPUT_TOKENS = 2_048

#: Safety factor on the pre-dispatch token estimate. ``estimate_tokens`` uses
#: cl100k_base, which systematically undercounts Anthropic tokenisation. Conversation
#: input is a few KB at p90 (Amendment A removed the ~67k-token payload worst case), so
#: the oversize check almost never fires now — but an estimate that lands just under the
#: true limit still turns into a provider 400 on every attempt, which the failure
#: taxonomy classifies as a transient model error and therefore retries forever.
_TOKEN_ESTIMATE_SAFETY_FACTOR = 1.2

#: Cap on the ``detail`` field of a failure event (see :func:`_failed`).
_MAX_FAILURE_DETAIL_CHARS = 500

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
  "basis": "user_statement" | "assistant_reasoning" | "mixed"
}

correction = {
  "text":    "<the self-correction>",
  "basis":   "user_statement" | "assistant_reasoning" | "mixed",
  "tier":    "self_correction",
  "locator": {"capture_id": "<capture id>", "field": "assistant_text"},
  "evidence_locator": {"capture_id": "<capture id>", "field": "assistant_text"}
}

`field` must be exactly assistant_text, using the capture id shown in the transcript. \
You are given only the conversation — never cite a field that isn't a turn's assistant \
response.

SLOTS — all optional, omit any that has nothing to say. Empty is a valid digest. \
Within each slot, put the MOST CONSEQUENTIAL item first: if the digest has to be \
shortened to fit, items are dropped from the end of a slot.
- established: facts and observations that survived the interaction. Filter this \
hardest; it is the slot most at risk of re-deriving facts that are already stored \
elsewhere.
- decisions: conclusions that materially constrain future reasoning, INCLUDING \
rejected alternatives and the reasons they were rejected.
- unresolved: unfinished state a future reader could wrongly treat as settled.
- corrections: see below. Usually empty, and that scarcity is correct.

LOCATORS — point, do not quote. Name the turn each citation comes from and nothing \
more; the quoted text is read from the turn you name, so never copy, paraphrase or \
summarise it into your answer. Cite a turn only if the text you have in mind is \
really in ITS assistant response — naming the wrong turn discards the correction.

CORRECTIONS — precision above all. A missed error is recoverable from the raw \
evidence; a false error writes self-confirming state into memory. You are given only \
the conversation — no tool status, errors, or payloads. The only kind you may assert:
- self_correction: the assistant corrected the record within the session. Point at the \
self-correction with locator and, with evidence_locator, at the assistant's own \
supporting text — both must be a turn's assistant response, never the user's message. \
If the correcting fact came from the user, the assistant must have restated it in its \
own reply for it to be citable here.

NEVER assert a correction for: weak or partial conflict, text with several \
defensible readings, state that legitimately changed over time, or disagreement \
with a subjective judgment or recommendation. Those belong in unresolved, or are \
omitted. NEVER infer an error from absent evidence, and NEVER assert a correction \
whose claim or evidence would need to be cited from the user's own message — only \
the assistant's text is citable.

Before asserting a correction, apply the SAME-PROPOSITION test explicitly. A \
correction requires the assistant's own later text to contradict the very thing it \
asserted earlier, not a neighbouring claim. In particular:
- A JUDGMENT is not a factual claim. "I would treat this as low priority", "I \
recommend X", "that seems fine", a severity or priority assessment, a suggested \
course of action — none of these are contradicted by stating a different fact \
later. The assistant asserted what it would DO or thought was true; a later fact is \
a different proposition, so this is not a correction.
- An APPROXIMATION is not a wrong number. "about two thousand" against 2,276, or \
"around 300ms" against 310ms, agree.
- A SCOPED claim is not a universal one. "the ones I checked are healthy" is not \
contradicted by a later claim about something unchecked.
If you cannot name the single proposition the assistant asserted and its own later \
text denies, in those words, there is no correction to make.

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


def _neutralise_delimiters(text: str) -> str:
    """Defuse forged transcript structure in attacker-influenceable content.

    Turn headers and the missing-evidence banner are plain text. A user message or
    an assistant response can itself echo attacker-influenced content — pasted web
    content, a forwarded document, a file path the assistant read back. Without
    this, crafted conversation text could forge a turn boundary or fake the
    evidence-unavailable declaration and thereby restructure the transcript the
    summariser reasons over. (Amendment B removed tool metadata from the prompt
    entirely, which closes that surface for this concern too — the remaining risk
    is conversation text, which this function still covers.)

    This does not make the prompt injection-proof — nothing at this layer does. It
    removes the cheap structural forgery, which is worth doing now because digests
    written today are durable and later phases inherit whatever this stores.
    """
    return text.replace("--- Turn ", "--- turn ").replace(
        "SOME EVIDENCE IS UNAVAILABLE", "some evidence is unavailable"
    )


def _format_turn(index: int, capture: TaskCapture) -> tuple[str, list[str]]:
    """Render one turn in full, and report any evidence it is missing.

    Conversation-only (Amendment B): no tool metadata of any kind — name, status,
    error, argument or payload — is rendered here, even when the capture carries a
    full tool result. Tool activity remains captured and stored; it simply never
    reaches this prompt.
    """
    notes: list[str] = []
    parts = [
        f"--- Turn {index} (capture_id: {capture.trace_id}) ---",
        "User:",
        _neutralise_delimiters(capture.user_message or ""),
        "",
        "Assistant:",
        _neutralise_delimiters(capture.assistant_response or ""),
    ]
    if capture.assistant_response is None:
        notes.append(f"turn {index} has no recorded assistant response")

    return "\n".join(parts), notes


def build_prompt(captures: Sequence[TaskCapture]) -> str:
    """Assemble the conversation-only transcript prompt (ADR-0124 D2, Amendment B).

    Every turn, the complete user and assistant text of each, and **nothing else** —
    no tool name, status, error, argument or payload reaches this prompt. Anything
    genuinely missing from what the producer *does* read (an absent assistant
    response) is *declared* at the top instead of silently skipped, so the
    summariser never reads a gap in its own input as the assistant having invented
    something.

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
            "this evidence. Corrections that rest on the session's own conversation text\n"
            "remain legitimate.\n"
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


#: Opening words of the LENGTH paragraph. Located rather than duplicated: a second
#: copy of the text would drift from the prompt on the next edit, and the whole point
#: of parameterising here instead of in the eval harness is that there is exactly one
#: prompt.
_LENGTH_RULE_MARKER = "LENGTH — include an item only"


def system_prompt(
    *,
    target_tokens: int | None = None,
    max_tokens: int | None = None,
    include_length_rule: bool = True,
) -> str:
    """Render the system prompt, optionally overriding its length policy.

    Substituted rather than ``format``-ed: the prompt embeds a literal JSON schema,
    and every brace in it would have to be doubled to survive ``str.format``.

    Called with no arguments — which is every production caller — this returns
    exactly the settings-driven prompt it always did. The overrides exist for the
    FRE-994 compression curve, which varies the length policy per call and must run
    against the *deployed* prompt: a copy in the harness would calibrate a prompt
    that is not in production and would drift silently on the next edit.

    Args:
        target_tokens: Overrides ``session_digest_target_tokens``.
        max_tokens: Overrides ``session_digest_max_tokens``.
        include_length_rule: When False, the LENGTH paragraph is removed rather than
            given a large number — a large number is still an instruction, and the
            unbounded arm measures what the generator writes when nothing constrains
            it.

    Returns:
        The rendered system prompt.
    """
    settings = get_settings()
    prompt = _SYSTEM_PROMPT.replace(
        "__TARGET_TOKENS__",
        str(settings.session_digest_target_tokens if target_tokens is None else target_tokens),
    ).replace(
        "__MAX_TOKENS__",
        str(settings.session_digest_max_tokens if max_tokens is None else max_tokens),
    )

    if not include_length_rule:
        start = prompt.find(_LENGTH_RULE_MARKER)
        if start != -1:
            end = prompt.find("\n\n", start)
            prompt = prompt[:start] + (prompt[end + 2 :] if end != -1 else "")

    return prompt


def _system_prompt() -> str:
    """Render the system prompt with the configured length bounds.

    Retained as the module-internal call site; the policy lives in
    :func:`system_prompt`.
    """
    return system_prompt()


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
    if basis not in ("user_statement", "assistant_reasoning", "mixed"):
        raise ValueError(f"item has invalid basis: {basis!r}")
    # `span`/`locator` are deliberately NOT read off the model's output (FRE-1024). No
    # basis obliges a citation outside `corrections` since Amendment B retired
    # `tool_evidence`, and reading them here would leave model-authored span text on the
    # path — which is the thing this change removes, not merely relocates.
    return DigestItem(text=text.strip(), basis=basis)


def _parse_correction(raw: object, captures: Sequence[TaskCapture]) -> Correction | None:
    """Parse one correction and ground it against the session's own text.

    Returns ``None`` — a drop, never a raise — when the **citation** cannot be grounded:
    corrections are rare and optional, so an unresolvable one may not cost the whole
    digest (FRE-1024, following FRE-993's trim-not-discard precedent).

    A malformed **shape** is a different matter and still raises. An off-vocabulary
    ``tier`` is a contract violation the schema owns and FRE-956 deliberately enforces at
    parse time, so it stays ``SCHEMA_INVALID``. Keeping the two apart is also what lets
    ``corrections_dropped`` mean exactly one thing to a reader — "the citation did not
    resolve" — which is what the rendered declaration asserts.

    Args:
        raw: One entry from the model's ``corrections`` slot.
        captures: The session's captures, which the locators are quoted from.

    Returns:
        The grounded correction, or ``None`` if its citation did not resolve.

    Raises:
        ValueError: If the item has no text or carries an invalid ``tier``.
    """
    item = _parse_item(raw)
    assert isinstance(raw, dict)  # _parse_item already rejected non-dicts
    tier = raw.get("tier")
    if tier not in ("self_correction",):
        raise ValueError(f"correction has invalid tier: {tier!r}")

    return ground_correction(
        text=item.text,
        basis=item.basis,
        tier=tier,
        locator=_parse_locator(raw.get("locator")),
        evidence_locator=_parse_locator(raw.get("evidence_locator")),
        captures=captures,
    )


def parse_model_output(
    content: str, *, ended_at: datetime, captures: Sequence[TaskCapture]
) -> tuple[str, SessionDigest]:
    """Parse the model's JSON and apply everything the producer owns.

    Two fields the model never authors, for one reason: state the code can compute is
    never regenerated in prose (ADR-0124 D3), because generating it can only introduce
    drift. ``unresolved`` items are stamped with the session's ``ended_at``, and a
    correction's spans are quoted from the locators it cited (Amendment E, FRE-1024).

    A correction whose citation does not resolve is dropped and counted into
    ``corrections_dropped``; the rest of the digest survives.

    Args:
        content: Raw model output, possibly fenced.
        ended_at: The session's last-turn timestamp, stamped onto unresolved items.
        captures: The session's captures, which correction locators are quoted from.

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

    raw_corrections = _slot("corrections")
    grounded = [_parse_correction(r, captures) for r in raw_corrections]
    corrections = [c for c in grounded if c is not None]

    digest = SessionDigest(
        established=[_parse_item(r) for r in _slot("established")],
        decisions=[_parse_item(r) for r in _slot("decisions")],
        unresolved=[
            UnresolvedItem(**_parse_item(r).model_dump(), as_of=ended_at)
            for r in _slot("unresolved")
        ],
        corrections=corrections,
        corrections_dropped=len(raw_corrections) - len(corrections),
    )
    return label, digest


def _estimate_input_tokens(prompt: str) -> int:
    return estimate_tokens(prompt) + estimate_tokens(_system_prompt())


def _input_token_limit(context_length: int | None) -> int:
    return (context_length or _FALLBACK_CONTEXT_LENGTH) - _OUTPUT_RESERVE_TOKENS


class OutputTruncated(Exception):
    """The provider stopped at the output ceiling, so the reply is a fragment.

    Raised instead of letting the fragment reach the parser, because a truncated JSON
    fragment fails parsing with ``unexpected end of data`` — indistinguishable, after the
    fact, from a model that got the format wrong. FRE-995 measured exactly that
    conflation: every sampled ``schema_invalid`` detail was a truncation. Distinguishing
    them at the point the stop reason is still visible is the only place it can be done.
    """


#: Stop reasons that mean "the ceiling cut this off", across provider vocabularies.
_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})


def _reply_payload(response: Mapping[str, Any]) -> str:
    """Read the digest JSON from a reply, whether it came back as a tool call or text.

    Under the contract the payload arrives in the tool call's arguments — a structured
    field, which is precisely why fence wrapping and trailing prose have nowhere to
    occur. The text fallback covers the contract being switched off, and a model that
    answers in prose anyway; treating that as an empty reply would turn a recoverable
    answer into a failure.
    """
    for call in response.get("tool_calls") or []:
        if call.get("name") == DIGEST_TOOL_NAME and call.get("arguments"):
            return str(call["arguments"])
    return response.get("content", "") or ""


def _reject_if_truncated(response: Mapping[str, Any]) -> None:
    """Raise :class:`OutputTruncated` when the reply was cut off at the ceiling.

    Two independent signals, deliberately. ``finish_reason`` is the direct one, but it is
    not always trustworthy: litellm's ``response_format`` path overwrites the provider's
    stop reason with ``"stop"`` before the caller ever sees it. This producer avoids that
    path, and the token check means a future library change that reintroduces it degrades
    into a false *positive* — reporting truncation we could still see in the token count —
    rather than silently scoring a truncated digest as clean.
    """
    finish_reason = response.get("finish_reason")
    if finish_reason in _TRUNCATION_FINISH_REASONS:
        raise OutputTruncated(f"provider stopped with finish_reason={finish_reason!r}")

    completion_tokens = (response.get("usage") or {}).get("completion_tokens")
    if isinstance(completion_tokens, int) and completion_tokens >= _MAX_OUTPUT_TOKENS:
        raise OutputTruncated(
            f"output reached the {_MAX_OUTPUT_TOKENS}-token ceiling "
            f"(finish_reason={finish_reason!r})"
        )


async def _call_model(
    prompt: str,
    *,
    role_name: str,
    provider: str | None,
    session_id: str,
    tracer: "Tracer | None" = None,
) -> str:
    """Dispatch one generation call. Raises on any client-level failure.

    Args:
        prompt: The fully-rendered prompt to send.
        role_name: Resolved model config key for the ``session_summary`` role.
        provider: Cloud provider, or ``None`` to dispatch to the local SLM.
        session_id: The session this digest is being generated for.
        tracer: Tracer to open this call's per-session root span with
            (FRE-1295). Defaults to the process-wide tracer; the scheduler's
            sweep passes its own tracer through so tests can inject an
            in-memory exporter.

    Raises:
        OutputTruncated: If the reply was cut off at the output ceiling.
    """
    # FRE-1295: the sweep tick keeps one root span open for its whole run
    # (FRE-1069/ADR-0129 D3); SystemTraceContext.new below reads whatever span
    # is CURRENT, so without a per-session span here every session swept in one
    # tick would mint the SAME trace id on its cost reservation — collapsing N
    # sessions onto one trace and breaking ADR-0074 §8c joinability.
    # open_root_span forces a genuine fresh root (context=Context()) regardless
    # of the tick's span already being current, so this call's model call, its
    # trace_ctx, and its budget_reservations row all agree on one new id.
    parent_trace_id = read_or_mint_trace_id()
    child_span, child_token, child_cv_tokens = open_root_span("session_summary", tracer=tracer)
    try:
        log.info(
            "batch_child_trace_opened",
            trace_id=read_or_mint_trace_id(),
            parent_trace_id=parent_trace_id,
            session_id=session_id,
        )
        if provider is not None:
            from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

            # The contract is applied on the cloud path only. The local path forwards tools
            # to llama-server, but that behaviour is outside FRE-996's evidence and the
            # deployed session_summary role is cloud — so no unverified claim ships here.
            use_contract = get_settings().session_digest_structured_output

            # budget_role stays captains_log: ADR-0124 D2 defers splitting cost
            # attribution as a separate, smaller decision.
            cloud_client = get_llm_client_for_key(role_name, budget_role="captains_log")
            response: dict[str, Any] = await cloud_client.respond(
                role=ModelRole.SESSION_SUMMARY,
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_system_prompt(),
                # Held to the schema as a forced tool rather than a `response_format`:
                # for the deployed claude-sonnet-5, litellm turns `response_format` into a
                # synthetic forced tool AND overwrites the provider's stop_reason with
                # "stop", which would hide truncation entirely. See session_digest_wire.
                tools=[digest_tool()] if use_contract else None,
                tool_choice=digest_tool_choice() if use_contract else None,
                # Without this the client falls back to the deployment's max_tokens
                # (128k) for an artifact bounded at 400 rendered tokens, and the cost gate
                # reserves against that ceiling on every call — exhausting a shared
                # budget lane far faster than the actual spend warrants.
                max_tokens=_MAX_OUTPUT_TOKENS,
                trace_ctx=SystemTraceContext.new("session_summary", session_id=session_id),
            )
            _reject_if_truncated(response)
            return _reply_payload(response)

        from personal_agent.llm_client.concurrency import InferencePriority  # noqa: PLC0415
        from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415

        # ADR-0141 D1: the same factory door as the cloud branch above, on the
        # same budget lane — placement now decides how the one client
        # dispatches, not which class is built.
        local_client = get_llm_client_for_key(role_name, budget_role="captains_log")
        llm_response = await local_client.respond(
            role=ModelRole.SESSION_SUMMARY,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": prompt},
            ],
            system_prompt=None,
            tools=None,
            max_tokens=_MAX_OUTPUT_TOKENS,
            max_retries=0,
            timeout_s=120.0,
            priority=InferencePriority.BACKGROUND,
            priority_timeout=120.0,
            trace_ctx=SystemTraceContext.new("session_summary", session_id=session_id),
        )
        _reject_if_truncated(llm_response)
        return llm_response.get("content", "") or ""
    finally:
        close_root_span(child_span, child_token, child_cv_tokens)


def _failed(
    reason: SummaryFailureReason,
    *,
    session_id: str,
    trace_id: str,
    detail: str = "",
    retry_after: datetime | None = None,
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
        # Truncated: `detail` can carry a repr of model output or a provider error
        # body, both derived from session content. A failure reason is diagnostic;
        # it is not a channel for shipping session text into the log index.
        detail=detail[:_MAX_FAILURE_DETAIL_CHARS],
    )
    return SessionSummaryOutcome(
        status=SessionSummaryStatus.FAILED, failure_reason=reason, retry_after=retry_after
    )


async def generate_session_digest(
    captures: Sequence[TaskCapture],
    *,
    session_id: str,
    ended_at: datetime,
    trace_id: str = "session_summary_sweep",
    tracer: "Tracer | None" = None,
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
        tracer: Tracer to open this call's per-session root span with
            (FRE-1295). Defaults to the process-wide tracer; the scheduler's
            sweep passes its own tracer through so tests can inject an
            in-memory exporter.
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
    if not settings.session_summary_enabled:
        # Checked here as well as in the sweep: this is half the governance point
        # ADR-0124 D2 names, and it must hold for any caller — an operator-run
        # eval or backfill included — not only for the scheduled path.
        log.info("session_summary_disabled_by_settings", session_id=session_id, trace_id=trace_id)
        return SessionSummaryOutcome(status=SessionSummaryStatus.SKIPPED_BELOW_FLOOR)

    model_config = load_model_config()
    role_name = resolve_role_model_key("session_summary")
    model_def = model_config.models.get(role_name)
    provider = model_def.provider if model_def else None

    prompt = build_prompt(captures)

    # Pre-dispatch, so a doomed session costs an estimate and a log line rather
    # than a model call (ADR-0124 AC-5). Never silently truncate.
    estimated_tokens = int(_estimate_input_tokens(prompt) * _TOKEN_ESTIMATE_SAFETY_FACTOR)
    limit = _input_token_limit(model_def.context_length if model_def else None)
    if estimated_tokens > limit:
        return _failed(
            SummaryFailureReason.OVERSIZED_INPUT,
            session_id=session_id,
            trace_id=trace_id,
            detail=f"estimated {estimated_tokens} input tokens exceeds limit {limit}",
        )

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
                prompt,
                role_name=role_name,
                provider=provider,
                session_id=session_id,
                tracer=tracer,
            )
        except BudgetDenied as e:
            # Never terminal: transient by nature, so the session stays retryable. Paced
            # rather than unbounded, though (FRE-987): the gate knows the instant this
            # cap's window rolls over, which is the only moment a retry could succeed, so
            # it is carried out to the sweep instead of leaving it to guess on a
            # 300-second clock. Transient is a statement about recovery, not a licence to
            # re-attempt 288 times a day.
            return _failed(
                SummaryFailureReason.BUDGET_DENIED,
                session_id=session_id,
                trace_id=trace_id,
                detail=f"{e.denial_reason} role={e.role} cap={e.cap} spend={e.current_spend}",
                retry_after=e.window_resets_at,
            )
        except OutputTruncated as e:
            # The one failure a retry cannot address, so it is the one that does not get
            # one (FRE-993, resolving the bound FRE-996 deferred here). The retry
            # re-issues a byte-identical request — same transcript, same system prompt,
            # same ceiling — and truncation is a property of that ceiling rather than of
            # what was sampled beneath it.
            #
            # Deliberately NOT extended to SCHEMA_INVALID or UNGROUNDED_DIGEST. Those are
            # stochastic: the same request can be resampled into a valid reply, and
            # Amendment C5 measured 2% contract drift that a retry plausibly recovers.
            # Retiring their retry would spend delivery to save a call. (SPAN_VALIDATION_
            # FAILED used to be named here too; FRE-1024 removed the reason outright.)
            #
            # This is a guard, not a saving: FRE-994 recorded zero truncations in 100
            # calls, so it protects a path that does not currently fire.
            last_validation_failure = (SummaryFailureReason.OUTPUT_TRUNCATED, str(e))
            break
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
            label, digest = parse_model_output(content, ended_at=ended_at, captures=captures)
        except ValueError as e:
            last_validation_failure = (SummaryFailureReason.SCHEMA_INVALID, str(e))
            continue

        # An ungroundable correction costs its own item, never the digest (FRE-1024) —
        # unless it was the ONLY thing the model produced, in which case grounding has
        # left nothing to deliver. Storing that empty record would clear the session's
        # failure state and advance its freshness stamp, dropping it out of the dirty
        # population for good: the delivery failure ADR-0124 Amendment C5 names, and the
        # one the trim path's own last-item guard already refuses to cause. Narrow by
        # construction — a digest the model simply left empty is untouched here.
        if digest.is_empty() and digest.corrections_dropped:
            last_validation_failure = (
                SummaryFailureReason.UNGROUNDED_DIGEST,
                f"all {digest.corrections_dropped} correction(s) failed to ground and "
                "no other slot had content",
            )
            continue

        # Trim, do not discard (ADR-0124 Amendment C2, FRE-993). The ceiling is a
        # rejection threshold of last resort, not the sizing mechanism: a digest over it
        # is already parsed, already provenance-checked and already paid for, and
        # regenerating buys nothing at a measured prompt elasticity of 0.16 — the second
        # attempt lands at the same length and fails the same check. Discarding here
        # rejected 47% of content-bearing digests at the deployed bound.
        pre_trim_tokens = digest_token_count(digest)
        digest, items_dropped = trim_digest_to_budget(digest, settings.session_digest_max_tokens)
        tokens = digest_token_count(digest)
        if tokens > settings.session_digest_max_tokens:
            # Reachable only when ONE item exceeds the whole ceiling — 3.4× the largest
            # item observed across FRE-994's 549. Failing is deliberate: the
            # alternatives are storing over the ceiling, or storing the empty digest
            # Amendment C5 names as the remaining delivery failure.
            last_validation_failure = (
                SummaryFailureReason.DIGEST_OVER_BUDGET,
                f"{tokens} tokens exceeds {settings.session_digest_max_tokens} "
                "and no further item may be dropped",
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
            # The live before/after signal for FRE-993. Without these two the trim rate
            # is measurable only on FRE-994's frozen corpus, never in production.
            pre_trim_digest_tokens=pre_trim_tokens,
            digest_items_dropped=items_dropped,
            established=len(digest.established),
            decisions=len(digest.decisions),
            unresolved=len(digest.unresolved),
            # Monitored as a drift signal: corrections are expected to be scarce,
            # so a rising rate is the alarm, not the achievement.
            corrections=len(digest.corrections),
            # How often the model cites a turn that is not there (FRE-1024). Previously
            # this discarded the whole digest and surfaced as a failure event; now it is
            # survivable, so it needs its own counter or it becomes invisible.
            corrections_dropped=digest.corrections_dropped,
            # A derived span is the whole cited turn, so the stored record can grow far
            # beyond the rendered ceiling the budget measures. Emitted so the real size
            # distribution is observable before anyone decides whether it needs a bound.
            correction_span_chars=sum(
                len(c.span) + len(c.evidence_span) for c in digest.corrections
            ),
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
