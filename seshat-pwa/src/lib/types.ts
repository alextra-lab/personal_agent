/**
 * AG-UI event types mirroring the Seshat backend wire format.
 *
 * Backend source: src/personal_agent/transport/agui/adapter.py
 * Internal events: src/personal_agent/transport/events.py
 *
 * See: docs/architecture_decisions/ADR-0075-websocket-transport.md
 */

// --------------------------------------------------------------------------
// AG-UI event envelope (server → client)
// --------------------------------------------------------------------------

export type AGUIEventType =
  | 'TEXT_DELTA'
  | 'TOOL_CALL_START'
  | 'TOOL_CALL_END'
  | 'PHASE_START'
  | 'PHASE_END'
  | 'STATE_DELTA'
  | 'INTERRUPT'
  | 'tool_approval_request'
  | 'CONSTRAINT_PAUSE'
  | 'CONSTRAINT_RESOLVED'
  | 'CANCELLED'
  | 'RUN_ERROR'
  | 'DONE'
  | 'PONG'
  | 'REPLAY_GAP'
  /** Terminates a reconnect's replay — "that is everything above your watermark" (FRE-1040). */
  | 'REPLAY_COMPLETE';

export interface AGUIEvent {
  type: AGUIEventType;
  data: Record<string, unknown>;
  session_id: string;
  /** Per-session sequence number for reconnect replay. Null for PONG/REPLAY_GAP/REPLAY_COMPLETE. */
  seq: number | null;
  /** Present on REPLAY_GAP events — the oldest seq still available. */
  oldest_available_seq?: number;
  /** Present on tool_approval_request events. */
  request_id?: string;
  trace_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  risk_level?: 'low' | 'medium' | 'high';
  reason?: string;
  expires_at?: string;
}

// --------------------------------------------------------------------------
// Client → server messages
// --------------------------------------------------------------------------

export type ClientMessageType =
  | 'CONNECT'
  | 'PING'
  | 'APPROVAL_DECISION'
  | 'CONSTRAINT_DECISION'
  | 'USER_CANCEL'
  | 'INTERRUPT_RESPONSE';

export interface ClientMessage {
  type: ClientMessageType;
  /** Last received seq (CONNECT only). */
  last_seq?: number;
  /** Target request (APPROVAL_DECISION, CONSTRAINT_DECISION, INTERRUPT_RESPONSE). */
  request_id?: string;
  /** Decision value (APPROVAL_DECISION, or action_id for CONSTRAINT_DECISION). */
  decision?: string;
  /** Choice value (INTERRUPT_RESPONSE). */
  choice?: string;
  /** Optional reason (APPROVAL_DECISION). */
  reason?: string;
  /** "Remember this choice" flag (CONSTRAINT_DECISION). */
  remember?: boolean;
}

// --------------------------------------------------------------------------
// Typed data payloads for each AG-UI event type
// --------------------------------------------------------------------------

/** TEXT_DELTA payload — streaming text chunk from LLM. */
export interface TextDeltaData {
  text: string;
}

/** TOOL_CALL_START payload — tool invocation has begun. */
export interface ToolCallStartData {
  tool_name: string;
  args: Record<string, unknown>;
}

/** TOOL_CALL_END payload — tool invocation completed. */
export interface ToolCallEndData {
  tool_name: string;
  result: string;
}

/**
 * The closed set of inference / human-wait phases the transport announces
 * (ADR-0123 §1-§2). Mirrors `personal_agent.transport.events.Phase`.
 */
export type PhaseName =
  | 'planning'
  | 'synthesis'
  | 'artifact_build'
  | 'expansion'
  | 'sub_agent'
  | 'waiting_for_choice';

/** PHASE_START payload — an inference / human-wait phase began (ADR-0123 §2). */
export interface PhaseStartData {
  phase: PhaseName;
  phase_id: string;
  /**
   * ISO-8601 UTC server timestamp of the phase start. Held verbatim by the
   * client — never reparsed/reserialized — so a reconnect can assert
   * byte-equality against the persisted event (ADR-0123 AC-3(b)).
   */
  started_at: string;
  detail: string | null;
  /** The parent's phase_id when this is a concurrent child (AC-8). */
  parent_id: string | null;
}

/**
 * One entry in a `phase_state` full-state snapshot — a currently-active phase
 * (ADR-0123 §6, FRE-986). Same fields as PHASE_START's data minus the routing
 * `session_id`; `started_at` is held verbatim (AC-3(b)).
 */
export interface PhaseSnapshotEntry {
  phase: PhaseName;
  phase_id: string;
  started_at: string;
  detail: string | null;
  parent_id: string | null;
}

/**
 * `phase_state` STATE_DELTA payload — the complete set of currently-active phases
 * for the session, a full-state replacement (ADR-0123 §6). The newest one wins, so
 * a reconnecting client converges from it alone and self-corrects a dropped PHASE_END.
 */
export interface PhaseStateData {
  active: PhaseSnapshotEntry[];
}

/** PHASE_END payload — an inference / human-wait phase ended (ADR-0123 §2). */
export interface PhaseEndData {
  phase: PhaseName;
  phase_id: string;
  parent_id: string | null;
  /**
   * `false` when the phase ended because the wrapped work raised (FRE-936 /
   * AC-9(b)) — `phase_span`'s pairing guarantee means PHASE_END fires on
   * every exit, success or exception, so this is what distinguishes them.
   * Absent on events persisted before this field shipped; treated as `true`.
   */
  ok?: boolean;
}

/**
 * STATE_DELTA payload — agent state change.
 *
 * The key ``context_window`` carries a float in [0, 1] representing
 * context budget consumed (used by ContextBudgetMeter).
 */
export interface StateDeltaData {
  key: string;
  value: unknown;
}

/** INTERRUPT payload — HITL approval request. */
export interface InterruptData {
  context: string;
  options: string[];
}

/**
 * tool_approval_request payload — primitive tool awaiting human approval.
 *
 * The agent has paused execution and will not proceed until the user
 * sends an APPROVAL_DECISION message over the WebSocket connection.
 * The request expires at ``expires_at``; the UI should auto-deny on timeout.
 */
export interface ToolApprovalRequestData {
  request_id: string;
  trace_id: string;
  tool: string;
  args: Record<string, unknown>;
  risk_level: 'low' | 'medium' | 'high';
  reason: string;
  /** ISO-8601 UTC timestamp after which the backend auto-denies. */
  expires_at: string;
}

// --------------------------------------------------------------------------
// UI-layer domain types
// --------------------------------------------------------------------------

export interface ToolCall {
  /** Tool name as reported by the backend. */
  name: string;
  /** ``running`` while executing; ``completed`` once TOOL_CALL_END received. */
  status: 'running' | 'completed';
  /** Human-readable result summary (populated on completion). */
  result?: string;
}

/**
 * A phase instance in the live turn-progress surface (ADR-0123 T3, FRE-936).
 *
 * `running` resolves to `completed`/`error` on its own PHASE_END (keyed by
 * `ok`). As a backstop — when no matching PHASE_END arrives, e.g. a dropped
 * best-effort emission — a terminal transport event sweeps any still-running
 * phase directly: CANCELLED → `cancelled`, RUN_ERROR → `error`, DONE →
 * `completed` (a normal turn end must never leave a phase spinning).
 */
export interface PhaseNode {
  phaseId: string;
  phase: PhaseName;
  detail: string | null;
  /** Raw server ISO-8601 timestamp, held verbatim (ADR-0123 AC-3(b)). */
  startedAt: string;
  state: 'running' | 'completed' | 'cancelled' | 'error';
  /** The parent's phaseId when this is a concurrent child (AC-8); null for a top-level phase. */
  parentId: string | null;
  /**
   * Client-observed `Date.now()` at the moment this phase left `running`.
   * PHASE_END carries no server end timestamp, so this freezes the
   * displayed duration on resolution — without it a completed phase's
   * elapsed time would keep growing from `startedAt` forever, contradicting
   * its own checkmark. `null` while still running.
   */
  endedAt: number | null;
  /**
   * `true` when this node was resolved to `completed` by the `phase_state`
   * snapshot safety net (its own PHASE_END was dropped), rather than by an
   * authoritative PHASE_END (ADR-0123 §6, FRE-986). Such a provisional
   * completion remains *upgradable* — a later RUN_ERROR / CANCELLED still
   * refines it to `error` / `cancelled`; a genuinely PHASE_END-completed node
   * (unmarked) is never touched by those sweeps.
   */
  snapshotResolved?: boolean;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  /** Tool calls associated with this assistant turn. */
  toolCalls?: ToolCall[];
  /** Trace ID from the backend, populated on DONE event or when hydrating from history. */
  traceId?: string;
  /**
   * True once the DONE event has been received for this assistant turn.
   * Gates rendering of post-completion controls (e.g. TurnRating).
   * Never set mid-stream; always false/absent for user messages.
   */
  complete?: boolean;
  /**
   * Previously-submitted 0–3 rating for this turn, hydrated from history
   * (FRE-426). Undefined when the turn has not been rated. Seeds TurnRating
   * so a rated turn renders solid (vs the faint default) across reloads.
   */
  rating?: number;
}

// --------------------------------------------------------------------------
// Model catalog + selection (ADR-0121 — Path removed, model picker)
// --------------------------------------------------------------------------

/** A single catalog deployment, as offered to the model picker (§2 of the ADR). */
export interface DeploymentView {
  key: string;
  id: string;
  provider: string;
  placement: 'local' | 'cloud';
  kind: string;
  status: string;
  summary: string | null;
  context_length: number;
  /** Maximum output tokens, or `null` for the provider default (backend: `ModelDefinition.max_tokens: int | None`). */
  max_tokens: number | null;
  supports_vision: boolean;
  supports_pdf_document: boolean;
  input_cost_per_token: number | null;
  output_cost_per_token: number | null;
}

/** A single provider row, for the observe view's provider table. */
export interface ProviderView {
  key: string;
  placement: 'local' | 'cloud';
  available: boolean;
  summary: string | null;
  max_concurrency: number;
}

/** Per-role entry in the config-read payload. */
export interface SessionConfigRole {
  open: boolean;
  /** Resolved deployment key — the session's live selection, or the catalog
   *  default when there is no session (sessionless read) or no stored
   *  selection (FRE-938). Optional only for defensive typing. */
  resolved?: string;
  /** How `resolved` was determined — `"server-hydrated"` or `"default"`. */
  provenance?: string;
  /** Present only for `open` roles. */
  candidates?: DeploymentView[];
}

/** Response shape shared by `GET /{id}/config` and the sessionless `GET /config`. */
export interface SessionConfig {
  session_id?: string;
  roles: Record<string, SessionConfigRole>;
  providers: ProviderView[];
}

/** Pending HITL interrupt requiring user decision. */
export interface PendingInterrupt {
  context: string;
  options: string[];
  sessionId: string;
}

// --------------------------------------------------------------------------
// Constraint governance (ADR-0076)
// --------------------------------------------------------------------------

/** CONSTRAINT_PAUSE payload — harness constraint about to fire. */
export interface ConstraintPauseData {
  constraint: string;
  context: string;
  /** Valid action_id values, mapped to labels via CONSTRAINT_ACTION_LABELS. */
  options: string[];
  default_option: string;
  /** ISO-8601 UTC timestamp after which the default fires. */
  expires_at: string;
}

/** CONSTRAINT_RESOLVED payload — a pause was resolved. */
export interface ConstraintResolvedData {
  constraint: string;
  action_id: string;
  resolution: 'user_choice' | 'timeout_default' | 'connection_lost' | 'user_cancel';
}

/** Pending constraint pause requiring a DecisionCard. */
export interface PendingConstraint extends ConstraintPauseData {
  request_id: string;
}

/** A constraint pause that has been resolved — rendered as a collapsed pill. */
export interface ResolvedConstraint {
  request_id: string;
  constraint: string;
  action_id: string;
  resolution: ConstraintResolvedData['resolution'];
}

/**
 * Live metrics for the two-lane status bar (STATE_DELTA key=turn_status).
 *
 * Session lane (ADR-0092 §D9): persists across turns — cumulative cost,
 * context occupancy, and the three per-mechanism compaction signals.
 * Engagement lane: per-harness-run tool count (FRE-553).
 */
export interface TurnStatus {
  // Engagement lane — per-harness-run, resets on next user input
  context_tokens: number;
  /**
   * Server-resolved context-window ceiling; `null` until it resolves (FRE-961 /
   * ADR-0123 §5). Same absent-vs-zero contract as `tool_iteration_max` below: the
   * server sends `null` (not a fabricated 0) before the turn's real ceiling lands, so
   * the client renders "—" rather than a misleading 0% bar. Typed nullable so a 0-seed
   * cannot be reintroduced without changing the type.
   */
  context_max: number | null;
  /**
   * Live tool iteration and its server-resolved ceiling.
   *
   * `null` means **not yet received** and is deliberately distinct from `0`
   * (FRE-928 AC-4 / FRE-935): the client must never invent a ceiling. A seeded
   * constant of 6 once rendered an amber "4 of 6" near-limit warning during a turn
   * whose real ceiling was 25 — a warning computed from a placeholder spends the
   * user's trust on a fiction. The server sends both fields on every turn_status,
   * so the client has no need to guess. Typed as nullable so a seed cannot be
   * reintroduced without changing the type.
   */
  tool_iteration: number | null;
  tool_iteration_max: number | null;
  turn_cost_usd: number;
  // Session lane — persists across turns (ADR-0092 §D9)
  session_cost_usd: number;
  session_context_tokens: number;
  compaction_count: number;
  cache_reset_count: number;
  quality_alert_count: number;
  /** Transient this-turn A alert; null when no gateway budget compaction fired. */
  quality_alert: { severity: string; phases_fired: string[] } | null;
}

/**
 * RUN_ERROR payload — classified turn failure (FRE-398).
 *
 * Backend source: ClassifiedErrorEvent (transport/events.py).
 * Rendered by ClassifiedErrorCard; action ids wired in FRE-399.
 */
export interface ClassifiedErrorData {
  category:
    | 'model_server'
    | 'timeout'
    | 'connection'
    | 'rate_limit'
    | 'budget_denied'
    | 'tool_failure'
    | 'generic';
  reason: string;
  next_step: string;
  /** Stable action ids: "retry", "switch_to_cloud", "stop". */
  actions: string[];
  /** True when partial tool-result synthesis was salvaged into the reply. */
  partial: boolean;
}
