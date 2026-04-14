/**
 * AG-UI event types mirroring the Seshat backend wire format.
 *
 * Backend source: src/personal_agent/transport/agui/adapter.py
 * Internal events: src/personal_agent/transport/events.py
 */

// --------------------------------------------------------------------------
// AG-UI event envelope
// --------------------------------------------------------------------------

export type AGUIEventType =
  | 'TEXT_DELTA'
  | 'TOOL_CALL_START'
  | 'TOOL_CALL_END'
  | 'STATE_DELTA'
  | 'INTERRUPT'
  | 'DONE';

export interface AGUIEvent {
  type: AGUIEventType;
  data: Record<string, unknown>;
  session_id: string;
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
}

/** Execution profile — determines which model the backend uses. */
export type ExecutionProfile = 'local' | 'cloud';

/** Pending HITL interrupt requiring user decision. */
export interface PendingInterrupt {
  context: string;
  options: string[];
  sessionId: string;
}
