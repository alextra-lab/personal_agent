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
  | 'STATE_DELTA'
  | 'INTERRUPT'
  | 'tool_approval_request'
  | 'CONSTRAINT_PAUSE'
  | 'CONSTRAINT_RESOLVED'
  | 'CANCELLED'
  | 'RUN_ERROR'
  | 'DONE'
  | 'PONG'
  | 'REPLAY_GAP';

export interface AGUIEvent {
  type: AGUIEventType;
  data: Record<string, unknown>;
  session_id: string;
  /** Postgres-assigned sequence number for reconnect replay. Null for PONG/REPLAY_GAP. */
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
}

/** Execution profile — determines which model the backend uses. */
export type ExecutionProfile = 'local' | 'cloud';

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

/** Live per-turn metrics for the status bar (STATE_DELTA key=turn_status). */
export interface TurnStatus {
  context_tokens: number;
  context_max: number;
  tool_iteration: number;
  tool_iteration_max: number;
  turn_cost_usd: number;
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
