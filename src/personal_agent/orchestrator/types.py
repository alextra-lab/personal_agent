"""Core types for the orchestrator.

This module defines the data structures used throughout the orchestrator:
- TaskState: State machine states
- ExecutionContext: Mutable state container passed through execution steps
- OrchestratorStep: Individual step metadata
- OrchestratorResult: Final result returned to UI
- RoutingDecision: Router decision types (HANDLE, DELEGATE)
- RoutingResult: Router decision output with format detection and parameters
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from personal_agent.captains_log.turn_evidence import (
    GroundingRecord,
    RecallCandidateRecord,
    TurnEvidence,
)
from personal_agent.governance.models import Mode
from personal_agent.grounding.enforcement_selection import EnforcementSelection
from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.llm_client import ModelRole
from personal_agent.orchestrator.loop_gate import ToolLoopGate
from personal_agent.request_gateway.types import GatewayOutput

if TYPE_CHECKING:
    from personal_agent.error_classification import ClassifiedError
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.constraint_options import ConstraintDecision
    from personal_agent.orchestrator.expansion_types import ExpansionPlan, PhaseResult
    from personal_agent.orchestrator.sub_agent_types import SubAgentResult


class TaskState(str, Enum):
    """State machine states for task execution."""

    INIT = "init"
    PLANNING = "planning"
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"
    SYNTHESIS = "synthesis"
    COMPLETED = "completed"
    FAILED = "failed"


class RoutingDecision(str, Enum):
    """Router decision types.

    Router is delegate-only; DELEGATE sends to STANDARD/REASONING/CODING.
    HANDLE retained for backward compatibility in RoutingResult.
    """

    HANDLE = "HANDLE"  # Legacy; router no longer answers directly
    DELEGATE = "DELEGATE"  # Delegate to specialized model


class HeuristicRoutingPlan(TypedDict):
    """Result of deterministic pre-router heuristic gate.

    Used to skip LLM router when confidence is high.
    """

    target_model: ModelRole
    confidence: float
    reason: str
    used_heuristics: bool


class RecommendedParams(TypedDict, total=False):
    """Recommended parameters from router for downstream model calls.

    Phase 2 enhancement: Router can recommend parameters based on
    detected output format and query complexity.

    Fields:
        max_tokens: Recommended maximum tokens for response.
        temperature: Recommended sampling temperature.
        timeout_multiplier: Multiplier for base timeout (1.0 = default).
    """

    max_tokens: int
    temperature: float
    timeout_multiplier: float


class RoutingResult(TypedDict, total=False):
    """Router decision output with format detection and parameters.

    This TypedDict contains the router model's decision about how to
    handle a query, including model selection and parameter recommendations.

    Fields:
        decision: HANDLE (router answers) or DELEGATE (use specialized model).
        target_model: If DELEGATE, which model to use (REASONING, CODING).
        confidence: Router's confidence in decision (0.0-1.0).
        reasoning_depth: Estimated complexity on 1-10 scale.

        # Output format detection (Phase 2)
        detected_format: Detected output format (summary, detailed, etc.).
        format_confidence: Confidence in format detection (0.0-1.0).
        format_keywords_matched: Keywords that triggered format detection.

        # Parameter recommendations (Phase 2)
        recommended_params: Recommended parameters for downstream call.

        # Direct response (if HANDLE)
        response: Router's direct response text (if decision=HANDLE).

        # Explanation
        reason: Brief explanation of routing decision.
    """

    # Required fields
    decision: RoutingDecision
    confidence: float
    reasoning_depth: int
    reason: str

    # Delegation fields (if decision=DELEGATE)
    target_model: ModelRole | None

    # Format detection (Phase 2)
    detected_format: str | None
    format_confidence: float | None
    format_keywords_matched: list[str] | None

    # Parameter recommendations (Phase 2)
    recommended_params: RecommendedParams | None

    # Direct response (if decision=HANDLE)
    response: str | None


@dataclass(frozen=True)
class ToolResultPin:
    """A verbatim ``read`` result held back from digestion (ADR-0085 §D4).

    The most-recent ``read`` of a file path is kept verbatim while a dependent
    ``write`` against that path may still be issued (the read→write hazard). The
    pin is released on a successful ``write`` to the path or after
    ``tool_result_digest_pin_ttl_turns`` rounds (abandonment).

    Attributes:
        path: The file path the pinned read targeted.
        round_pinned: ``tool_iteration_count`` when the pin was recorded.
    """

    path: str
    round_pinned: int


@dataclass(frozen=True)
class ConstraintResolutionRecord:
    """One resolved constraint pause within a turn (ADR-0142 D3/D4a, FRE-1391).

    ``ExecutionContext.constraint_resolutions`` holds one of these per pause, in the
    order they resolved — a list, because D4a permits several pauses in one turn and a
    scalar field would silently record only the last one.

    Attributes:
        constraint: Which constraint raised the pause.
        action_id: The resolved action identifier applied — a user choice, a timeout
            default, or the no-client safe default. A preference-bypassed decision never
            reaches this list: no pause occurred, so there is nothing to record here.
    """

    constraint: str
    action_id: str


@dataclass(frozen=True)
class AttachmentRef:
    """Structured reference to a completed upload (FRE-661 / ADR-0101 §2, §8a).

    Carried on ``ExecutionContext.attachments``, separate from ``ctx.user_message``,
    so attachment metadata never pollutes the clean task text that Captain's Log
    and entity extraction read (AC-5).

    Attributes:
        artifact_id: Postgres ``artifacts.id`` of the completed upload.
        content_type: MIME type of the upload.
        title: Display filename.
        r2_key: Object key for the credentialed ``store.get(r2_key)`` byte fetch (§3).
        requested_pages: Optional 1-indexed page numbers to force-select for a
            Tier-2 PDF, bypassing salience auto-selection (ADR-0102 §4 /
            FRE-685). A resolution directive, not upload metadata — set only
            when re-resolving an already-stored document for a continuation
            request (the offered-and-honored follow-up for pages dropped by
            the per-turn page budget). ``None`` for a normal upload.
    """

    artifact_id: str
    content_type: str
    title: str
    r2_key: str
    requested_pages: tuple[int, ...] | None = None


@dataclass(frozen=True)
class PendingCloudAttachmentConfirmation:
    """Short-lived pending confirmation state for cloud-vision cost gate re-injection (FRE-749 / ADR-0101 §8b).

    When the pre-flight cost gate pauses due to image cost exceeding the threshold,
    this record carries the already-resolved attachment refs and cloud-vision model
    key across turns. On the next turn, if the user's message is an affirmative
    confirmation, the attachments are re-injected into the new ExecutionContext and
    proceed to cloud-vision call; otherwise, the pending state is dropped.

    Attributes:
        attachments: Tuple of AttachmentRef from the paused turn (immutable).
        cloud_vision_model_key: Model key to route cloud vision to (from _resolve_vision_routing_key).
        estimate_usd: Pre-flight cost estimate disclosed to user.
        created_at: Unix timestamp when the gate paused.
        ttl_seconds: Seconds before this pending state expires.
        original_trace_id: Trace ID of the paused turn (for telemetry correlation).
    """

    attachments: tuple[AttachmentRef, ...]
    cloud_vision_model_key: str
    estimate_usd: float
    created_at: float  # Unix timestamp
    ttl_seconds: int
    original_trace_id: str


@dataclass(frozen=True)
class DocumentContinuationOffer:
    """Facts needed to offer/serve a continuation for one budget-dropped PDF (ADR-0102 §4, FRE-685).

    Emitted by ``document_resolution.resolve_documents`` whenever a Tier-2
    document has pages the per-turn page budget did not include — either the
    initial auto-selection or a further-truncated continuation request.
    Carries no session/turn identity; the caller (``executor.py``) wraps this
    into the durable ``PendingDocumentContinuation`` record.

    Attributes:
        artifact_id: Postgres ``artifacts.id`` of the document.
        content_type: MIME type (``application/pdf`` for this record).
        title: Display filename, echoed in disclosures.
        r2_key: Object key for the credentialed ``store.get(r2_key)`` re-fetch.
        dropped_pages: 1-indexed page numbers not included this turn.
    """

    artifact_id: str
    content_type: str
    title: str
    r2_key: str
    dropped_pages: tuple[int, ...]


@dataclass(frozen=True)
class PendingDocumentContinuation:
    """Durable session state offering to deliver previously budget-dropped PDF pages (ADR-0102 §4, FRE-685).

    Saved when ``resolve_documents`` drops Tier-2 pages under the per-turn
    page budget (the disclose-and-offer-to-continue behaviour). Holds every
    offer from the offering turn, not just one — a turn can have more than
    one over-budget document sharing the same page budget. On a later turn,
    if the user's message names a page range (or a broad affirmative), the
    matching artifact(s) are re-resolved with ``AttachmentRef.requested_pages``
    set to exactly the requested/dropped intersection — no re-upload needed,
    since the artifact is already in R2 under ``r2_key``.

    Attributes:
        offers: One offer per over-budget document from the turn that made it.
        created_at: Unix timestamp when the offer(s) were saved.
        ttl_seconds: Seconds before this pending state expires.
        original_trace_id: Trace ID of the turn that made the offer.
    """

    offers: tuple[DocumentContinuationOffer, ...]
    created_at: float  # Unix timestamp
    ttl_seconds: int
    original_trace_id: str


@dataclass
class ExecutionContext:
    """Mutable state container passed through execution steps.

    This dataclass holds all state that flows through the orchestrator's
    state machine. It is intentionally mutable to allow step functions to
    update state as execution progresses.

    Attributes:
        session_id: Unique identifier for the session.
        trace_id: Unique identifier for this task's trace (for telemetry).
        user_message: The user's input message.
        mode: Current operational mode from governance.
        channel: Communication channel (CHAT, CODE_TASK, SYSTEM_HEALTH).
        messages: OpenAI-style chat history (system, user, assistant, tool).
        current_plan: Optional execution plan (for future planning features).
        tool_results: List of tool execution results.
        final_reply: The final response text to return to user.
        error: Exception if task failed, None otherwise.
        steps: List of OrchestratorStep records for observability.
        state: Current state in the state machine.

    # Routing state (Day 11.5)
    selected_model_role: Model role selected by router (if delegated).
    routing_history: History of routing decisions for this task.

    # Request monitoring (ADR-0012)
    metrics_summary: Aggregated system metrics summary from RequestMonitor.
    """

    session_id: str
    trace_id: str
    user_message: str
    mode: Mode
    channel: "Channel"  # Forward reference, defined in channels.py
    messages: list[dict[str, Any]] = field(default_factory=list)
    current_plan: dict[str, Any] | None = None
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_reply: str | None = None
    error: Exception | None = None
    classified_error: ClassifiedError | None = None
    steps: list["OrchestratorStep"] = field(default_factory=list)
    state: TaskState = TaskState.INIT
    metrics_summary: dict[str, Any] | None = None  # ADR-0012: Request-scoped metrics

    # Routing state (Day 11.5)
    selected_model_role: ModelRole | None = None
    routing_history: list[RoutingResult] = field(default_factory=list)

    # LLM response tracking (for stateful /v1/responses API)
    last_response_id: str | None = None

    # Tool loop governance (per-request)
    tool_iteration_count: int = 0
    loop_gate: ToolLoopGate = field(default_factory=ToolLoopGate)
    # FRE-973: monotonic timestamp marking turn start, for the wall-clock deadline
    # enforced in step_llm_call (a runaway turn is bounded by elapsed time, not just
    # tool-call count). Stamped at construction — in production this happens
    # immediately before execute_task_safe is invoked (orchestrator.py).
    turn_started_monotonic: float = field(default_factory=time.monotonic)
    # FRE-1298: request-ingress instant for the current-date/time prompt block.
    # Stamped once here — like turn_started_monotonic above — and reused verbatim
    # for every model call in the turn (a tool loop calls step_llm_call repeatedly
    # against this same ctx); rendering from this field rather than re-reading the
    # wall clock per call is what keeps the value identical across those calls.
    turn_started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Set True when the iteration limit fires so step_llm_call performs a no-tool synthesis pass
    force_synthesis_from_limit: bool = False
    # FRE-973/FRE-1375: set by _stop_turn_for_deadline / _stop_turn_for_cancel. Tells
    # step_synthesis to skip grounding verification/retry — an early-stopped reply is
    # a salvage of gathered results, not a generated claim, and enforcement's retry
    # path (back to TaskState.LLM_CALL) would otherwise issue exactly the extra model
    # call a deadline stop or a user Stop must never produce.
    turn_stopped_early: bool = False
    # ADR-0076: extra iterations granted when the user picks "Continue" at a
    # tool_iteration_limit constraint pause. Added on top of the resolved max.
    tool_iteration_bonus: int = 0
    # ADR-0142 (FRE-1391): the post-grant tool-iteration ceiling this turn's loop actually
    # used, stamped by _resolve_max_iterations on every call so the last stamp before turn
    # end reflects any bonus granted mid-turn. None until the loop resolves it once.
    effective_tool_iteration_ceiling: int | None = None
    # ADR-0142 (FRE-1391): one entry per constraint pause this turn raised, in the order
    # they resolved. See ConstraintResolutionRecord for why this is a list, not a scalar.
    constraint_resolutions: list[ConstraintResolutionRecord] = field(default_factory=list)
    # ADR-0142 (FRE-1391): pause accounting the future D4a credited-pause bound will read.
    # Both start at an explicit zero rather than None, so a quiet turn's row can be told
    # apart from one where the accounting never ran.
    credited_pause_seconds: float = 0.0
    pause_count: int = 0
    # ADR-0076: accumulated LLM spend for this turn (USD), surfaced live via the
    # turn_status STATE_DELTA so the user sees cost as it accrues.
    turn_cost_usd: float = 0.0
    # FRE-1326: the real, provider-reported input-token count from the most recently
    # completed primary model call this turn (step_llm_call sets this from
    # response["usage"]["prompt_tokens"] when positive). Sticky across tool-loop
    # iterations within a turn; _report_turn_progress prefers this over the pre-call
    # estimate once a call has resolved with real usage.
    last_prompt_tokens: int = 0

    # Memory enrichment (Phase 2.2)
    memory_context: list[dict[str, Any]] | None = None  # Retrieved conversations for context
    # ADR-0125 D3 item 5 (FRE-1004): everything recall offered this turn, captured
    # before budget trimming so a dropped item stays nameable; and the turn's evidence
    # record, built once at the admission point and read by TaskCapture.
    recall_candidates: tuple["RecallCandidateRecord", ...] = ()
    turn_evidence: "TurnEvidence | None" = None
    # ADR-0138 D2/D3(a) (FRE-1280): the sources this turn retrieved, each with the stable
    # identifier a citation resolves against. Turn-scoped by construction — one registry
    # per context — so "present in *this* turn's source set" is a property of the object.
    # None on paths that never enter execute_task (sub-agents); every registration helper
    # no-ops rather than raising. FRE-1282 consumes it in step_synthesis: the inline D3
    # checks resolve against it, and D4 blocks, retries or refuses on the result.
    source_registry: "SourceRegistry | None" = None

    # ADR-0138 D4 (FRE-1282). Generation attempts this turn has made under the grounding
    # contract, counting from 1 at the first verification. Its own counter, deliberately
    # separate from tool_iteration_count: a forced-retrieval retry is a *generation*
    # attempt, and folding it into the tool-loop bound would let a turn that used its tool
    # budget legitimately lose its one chance to fix an unsourced claim.
    grounding_attempts: int = 0

    # Set when D4 orders a retry; step_llm_call reads it to guarantee the retry can
    # actually retrieve — tools offered, retrieval demanded — rather than merely being
    # asked to. Cleared as it is consumed.
    grounding_retry_pending: bool = False

    # Extra tool iterations reserved for D4's forced-retrieval retries, added to the
    # ceiling in _resolve_max_iterations on the same footing as ADR-0076's user-granted
    # bonus. Without it the retry is forced in name only: a turn that spent its tool
    # budget legitimately would be told to retrieve with nothing left to retrieve with.
    grounding_retrieval_grant: int = 0

    # What this turn retrieved or tried to, in order, as human-readable descriptors.
    # D4's terminal statement names what was searched, and that is a claim about the turn
    # record (a system-record span under D1), not about the world.
    retrieval_attempts: list[str] = field(default_factory=list)

    # ADR-0138 D3(d) (FRE-1286). Inline entailment judge calls this turn has spent,
    # accumulated ACROSS D4 attempts. The cap is cumulative for the same reason
    # grounding_attempts is its own counter: a per-pass bound bounds nothing when the pass
    # may run again, so a turn that retries would pay the cap once per generation.
    grounding_entailment_checks: int = 0

    # ADR-0138's output side of the ADR-0125 evidence contract, attached for the capture.
    grounding_record: "GroundingRecord | None" = None

    # ADR-0138 D5 (FRE-1285). The enforcement level this turn runs under, chosen ONCE
    # before the first generation and then held. Not re-decided per tool-loop pass: the
    # level is a statement about how this turn was generated, and a turn that started
    # heavy did have its retrieval forced whatever a later pass would have chosen. Its
    # presence is also the once-per-turn guard.
    grounding_enforcement: "EnforcementSelection | None" = None

    # ADR-0138 D5 (FRE-1284). The catalog deployment key that actually served this turn's
    # generation, stamped by step_llm_call with the key it really used. Recorded rather
    # than re-derived at finalize for two reasons: the role name is not the model (an
    # attachment-routed turn resolves to a different deployment, and crediting one model's
    # compliance to another is how a promotion gets bought with someone else's turns), and
    # re-running the attachment routing can raise, which a metric write must never do.
    answering_model_key: str | None = None

    # ADR-0129 D3 (FRE-1067): set by step_tool_execution just before it returns,
    # reset to 0 at its own entry — the driver loop's step-span closure reads
    # this back as the step span's "tool_count" attribute once the step
    # function returns something other than TOOL_EXECUTION. Not a durable
    # count; a single-use handoff channel for the step currently in flight.
    last_tool_execution_count: int = 0

    # Gateway output (Cognitive Architecture Redesign v2)
    gateway_output: GatewayOutput | None = None  # From request_gateway pipeline

    eval_mode: bool = False  # True when request came from an eval/benchmark harness channel

    # FRE-229: owning user UUID — passed from the authenticated request for TaskCapture
    user_id: UUID | None = None
    # FRE-673: whether the request carries a verified identity (CF Access). Threaded into
    # memory-recall visibility scoping so 'group'-visibility memory is revealed (FRE-229).
    authenticated: bool = False
    # FRE-213: user email + display name for the operator stanza (ADR-0052)
    user_email: str | None = None
    user_display_name: str | None = None
    # Rendered operator stanza populated in step_init; injected into system prompt in step_llm_call.
    operator_stanza: str = ""
    # FRE-1150: the name that stanza asserts, carried alongside it so the turn's capture
    # records the asserted identity from the same resolution the prompt used — never a
    # second source that could disagree with what the model was actually told.
    operator_name: str = ""
    # FRE-1150: the stanza's identity claim + authority rule without the profile detail
    # block. Recorded on the capture so the mechanism is auditable without persisting the
    # user's location, pronouns, role or languages into a text-indexed telemetry store.
    operator_assertion: str = ""

    # --- Expansion controller state (Slice 3, ADR-0036) ---
    expansion_strategy: str | None = None
    expansion_constraints: dict[str, Any] | None = None
    sub_agent_results: list["SubAgentResult"] | None = None
    expansion_plan: "ExpansionPlan | None" = None
    expansion_phase_results: list["PhaseResult"] = field(default_factory=list)

    # --- Phase B skill routing (FRE-skill-routing) ---
    # Tracks which skill bodies have been read_skill'd this conversation for dedup.
    loaded_skills: set[str] = field(default_factory=set)

    # --- Phase C skill routing (FRE-skill-routing) ---
    # Set True after the routing model has been queried for this request.
    # Prevents the routing call from re-firing on every step_llm_call iteration.
    skill_routing_done: bool = False
    # Model ID returned by the routing call (for telemetry breakdown across cells).
    skill_routing_model_id: str = ""

    # --- ADR-0081 §D3 cache-aware compaction (FRE-434) ---
    # Bounded salient highlights produced by the most recent frozen reset; ride
    # the current turn's volatile block (regenerated on reset, never frozen).
    salient_highlights: str = ""

    # --- ADR-0085 §D4 intra-turn tool-result digest pinning (FRE-475) ---
    # Reads held verbatim pending a dependent write, keyed by tool_call_id.
    tool_result_pins: dict[str, ToolResultPin] = field(default_factory=dict)

    # --- ADR-0088 execution-topology observability seam (FRE-513) ---
    # Resolved execution-topology label for this turn (primary / hybrid_fanout /
    # decompose / delegate). Set once by observe_topology on seam enter; read by the
    # route-trace assembler and carried on stream:turn.observed events.
    topology: str | None = None

    # --- FRE-661 / ADR-0101 §2 structured attachment carrier ---
    # Kept separate from user_message so Captain's Log + entity extraction never
    # see attachment metadata (AC-5). Immutable tuple to prevent caller-side
    # mutation of the list passed at the handle_user_request seam.
    attachments: tuple[AttachmentRef, ...] = ()

    # --- FRE-666 / ADR-0101 §6 guardrail disclosure ---
    # User-facing strings describing any downscale/drop a guardrail applied while
    # resolving this turn's raster attachments. Appended to ctx.final_reply by
    # step_synthesis so the disclosure reaches the user (never silent).
    attachment_disclosures: list[str] = field(default_factory=list)

    # --- FRE-691 / ADR-0101 §8b cloud-attachment cost confirmation ---
    # Set True once the user has confirmed (or the turn is under the cost
    # threshold) so a turn that re-enters LLM_CALL (tool iterations, hybrid
    # synthesis) with the images still in context is not re-prompted. The gate
    # is per-turn: one confirmation authorises the turn's cloud-vision usage,
    # while the per-call ADR-0065 reservation still caps every call.
    attachment_cost_confirmed: bool = False

    # --- FRE-684 / ADR-0102 T4 document-driven routing ---
    # Set during turn-assembly iff a PDF attachment actually classified Tier 2
    # (vision) and a capability/routing decision was made for it. ``None``
    # means no document forced a routing decision this turn (either no PDF
    # attachment, or every PDF resolved via Tier 1 text — ADR-0102 §1: Tier 1
    # must work on any model, so it never sets this). When set, both
    # ``_maybe_confirm_attachment_cost`` and ``step_llm_call`` use this key
    # instead of independently recomputing image-only vision routing, so a
    # document-driven escalation doesn't leave the image cost-gate checking a
    # stale (pre-escalation) model key.
    document_effective_model_key: str | None = None

    # --- ADR-0122 §2/§4 (T5/FRE-930) per-build artifact-builder selection ---
    # Turn-scoped resolution of the artifact-builder DecisionCard, raised at turn
    # start in ``step_init`` off the ``artifact_build_intent`` signal (FRE-929) —
    # before the first LLM call, never at the build boundary (which raised it ~117 s
    # late, the AC-7 failure). ``None`` until the turn-start ask runs; a card pick, a
    # silent stored preference, or a safe default on timeout/no-socket all populate
    # it. Authoritative turn-scoped state (AC-10a); the value is mirrored onto an
    # async ``ContextVar`` (``constraint_options.set_artifact_builder_resolution``) to
    # reach ``artifact_draft``, which receives only a ``TraceContext``.
    artifact_builder_resolution: "ConstraintDecision | None" = None

    # --- ADR-0122 §5 (T6/FRE-931) — the planning step is told the budget ---
    # Turn-scoped guidance naming the resolved builder's effective output-token
    # budget and context window, computed alongside ``artifact_builder_resolution``
    # in ``_maybe_resolve_artifact_builder``. ``step_llm_call`` appends it to the
    # VOLATILE tail of the system prompt (never the STATIC/SEMI-STATIC prefix, so it
    # never enters ``static_prefix_hash`` — ADR-0081 §D1) so the primary scopes the
    # ``artifact_draft`` plan to what the builder can actually emit, rather than
    # discovering the ceiling by overrunning it mid-generation (the FRE-478 class).
    # ``None`` when no turn-start ask ran (no ``artifact_build_intent`` signal).
    artifact_builder_planning_note: str | None = None


class OrchestratorStep(TypedDict):
    """Step metadata for observability.

    This TypedDict records information about each step in the orchestrator's
    execution. Used for trace reconstruction and debugging.

    Fields:
        type: Step type ("llm_call", "tool_call", "plan", "summary", "warning").
        description: Human-readable description of what this step did.
        metadata: Additional structured data (model_role, tool_name, span_ids, etc.).
    """

    type: str
    description: str
    metadata: dict[str, Any]


class OrchestratorResult(TypedDict, total=False):
    """Final result returned to UI from orchestrator.

    This TypedDict contains the orchestrator's response to a user request.

    Fields:
        reply: Final user-facing text response.
        steps: List of OrchestratorStep records for transparency.
        trace_id: Trace ID for telemetry correlation.
    """

    reply: str
    steps: list[OrchestratorStep]
    trace_id: str | None
