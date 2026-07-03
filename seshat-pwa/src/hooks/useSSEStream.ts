'use client';

import { useState, useCallback, useRef, useEffect } from 'react';

import {
  BudgetDeniedError,
  connectWebSocket,
  getSessionMessages,
  sendChatMessage,
  type StreamConnection,
  type UploadedAttachment,
} from '@/lib/agui-client';
import { submitTurnRating } from '@/lib/submitTurnRating';
import { generateUUID } from '@/lib/uuid';
import type {
  AGUIEvent,
  ChatMessage,
  ClassifiedErrorData,
  ConstraintPauseData,
  ConstraintResolvedData,
  ExecutionProfile,
  PendingConstraint,
  PendingInterrupt,
  ResolvedConstraint,
  ToolApprovalRequestData,
  ToolCall,
  TurnStatus,
} from '@/lib/types';

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------

/** localStorage key for persisting the in-progress assistant draft on visibility hide. */
const DRAFT_KEY = (sid: string) => `seshat_bg_draft_${sid}`;

/** Resting "ok" rating persisted on turn completion (FRE-757; == imputation default). */
const RATING_OK_DEFAULT = 2;

// --------------------------------------------------------------------------
// Hook return type
// --------------------------------------------------------------------------

export interface UseSSEStreamReturn {
  messages: ChatMessage[];
  isStreaming: boolean;
  activeTools: ToolCall[];
  /** Live per-turn metrics (ADR-0076); null until first turn_status STATE_DELTA. */
  turnStatus: TurnStatus | null;
  /**
   * Server-authoritative execution profile from a `session_profile` STATE_DELTA
   * (ADR-0079 / FRE-419); null until one arrives. The UI reconciles its pill
   * to this when it changes.
   */
  serverProfile: ExecutionProfile | null;
  /** Active constraint pause awaiting a decision (ADR-0076); null when none. */
  pendingConstraint: PendingConstraint | null;
  /** Constraints resolved this turn, rendered as collapsed pills. */
  resolvedConstraints: ResolvedConstraint[];
  /** True after a CANCELLED event — renders the "Stopped by user" pill. */
  cancelled: boolean;
  pendingInterrupt: PendingInterrupt | null;
  /** Pending tool-approval request; non-null while agent is waiting for a decision. */
  pendingApproval: ToolApprovalRequestData | null;
  /**
   * Structured Cost-Gate denial from the most recent send (FRE-306). Non-null
   * means the backend returned a 503 with `error="budget_denied"`; the chat
   * UI renders <BudgetDeniedCard> instead of an empty assistant turn.
   * Cleared on the next sendMessage().
   */
  budgetDenied: BudgetDeniedError | null;
  /**
   * Classified turn failure from RUN_ERROR transport event (FRE-398). Non-null
   * when the backend emitted a ClassifiedErrorEvent; renders ClassifiedErrorCard.
   * Cleared on the next sendMessage() or when the user dismisses it.
   */
  classifiedError: ClassifiedErrorData | null;
  /** Dismiss the ClassifiedErrorCard (user action or next send). */
  dismissClassifiedError: () => void;
  sendMessage: (text: string, sessionId: string, profile: ExecutionProfile, attachments?: UploadedAttachment[]) => Promise<void>;
  resolveInterrupt: (choice: string) => void;
  /** Post an approve/deny decision for the current pendingApproval. */
  handleApprovalDecision: (decision: 'approve' | 'deny') => void;
  /** Send a constraint decision (ADR-0076) and optimistically clear the card. */
  sendConstraintDecision: (requestId: string, actionId: string, remember: boolean) => void;
  /** Send a USER_CANCEL (Stop button) to halt the current turn. */
  sendUserCancel: () => void;
  disconnect: () => void;
  clearMessages: () => void;
  /** Replace the message list with a server-hydrated history. */
  seedMessages: (msgs: ChatMessage[]) => void;
  seedTurnStatus: (status: TurnStatus) => void;
  /**
   * True while the WebSocket was lost mid-turn and we are waiting to reconnect.
   * The UI shows a "Reconnecting…" banner while this is set (FRE-236).
   */
  isReconnecting: boolean;
}

// --------------------------------------------------------------------------
// Hook
// --------------------------------------------------------------------------

/**
 * React hook that manages the full AG-UI streaming lifecycle over WebSocket.
 *
 * Handles:
 * - Sending user messages to the Seshat backend.
 * - Connecting to the AG-UI WebSocket (ADR-0075).
 * - Assembling streaming text deltas into assistant messages.
 * - Tracking tool call lifecycle (TOOL_CALL_START → TOOL_CALL_END).
 * - Surfacing context budget from STATE_DELTA events.
 * - Capturing HITL INTERRUPT events and providing a resolve callback.
 * - Tool approval round-trips via WebSocket (replaces POST /approval).
 * - Reconnect replay via seq numbers and localStorage persistence.
 * - REPLAY_GAP fallback to full session history API.
 */
export function useSSEStream(): UseSSEStreamReturn {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [activeTools, setActiveTools] = useState<ToolCall[]>([]);
  const [turnStatus, setTurnStatus] = useState<TurnStatus | null>(null);
  const [serverProfile, setServerProfile] = useState<ExecutionProfile | null>(null);
  const [pendingConstraint, setPendingConstraint] = useState<PendingConstraint | null>(null);
  const [resolvedConstraints, setResolvedConstraints] = useState<ResolvedConstraint[]>([]);
  const [cancelled, setCancelled] = useState<boolean>(false);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingInterrupt | null>(null);
  const [pendingApproval, setPendingApproval] = useState<ToolApprovalRequestData | null>(null);
  const [budgetDenied, setBudgetDenied] = useState<BudgetDeniedError | null>(null);
  const [classifiedError, setClassifiedError] = useState<ClassifiedErrorData | null>(null);
  // FRE-236: true while WS was lost mid-turn and we are waiting to reconnect.
  const [isReconnecting, setIsReconnecting] = useState(false);

  // Refs that survive re-renders without causing them.
  const streamRef = useRef<StreamConnection | null>(null);
  const currentContentRef = useRef<string>('');
  const currentSessionRef = useRef<string>('');
  const maxHandledSeqRef = useRef<number>(0);
  // FRE-407: the turn's trace_id arrives on the turn_status STATE_DELTA; we stash
  // it here so the DONE handler can stamp it onto the completed assistant message
  // (the rating control joins on trace_id).
  const currentTurnTraceIdRef = useRef<string>('');
  // FRE-575 (fold-in to FRE-573): track latest turn_status so DONE can persist
  // tool state to localStorage for engagement-lane remount restore.
  const lastTurnStatusRef = useRef<TurnStatus | null>(null);
  // FRE-236: mirrors isStreaming state for use in non-React closures (event handlers).
  // Must be updated alongside every setIsStreaming call.
  const isStreamingRef = useRef(false);
  // FRE-757: trace_ids whose default "ok" was already persisted on completion,
  // so a repeated DONE never double-posts the resting default.
  const defaultRatedTracesRef = useRef<Set<string>>(new Set());

  // FRE-236: persist the in-progress draft to localStorage on visibility hide so
  // a kill+relaunch can detect that a turn was in-flight and the relaunch hydration
  // should treat the last message as authoritative (not phantom-draft).
  useEffect(() => {
    const persistDraft = () => {
      const sid = currentSessionRef.current;
      const content = currentContentRef.current;
      if (isStreamingRef.current && sid && content) {
        try {
          localStorage.setItem(DRAFT_KEY(sid), JSON.stringify({ content, at: new Date().toISOString() }));
        } catch {
          // Quota exceeded — skip draft persistence.
        }
      }
    };
    const onVisChange = () => {
      if (document.visibilityState === 'hidden') persistDraft();
    };
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisChange);
      window.addEventListener('pagehide', persistDraft);
    }
    return () => {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisChange);
        window.removeEventListener('pagehide', persistDraft);
      }
    };
  }, []); // reads only stable refs — no deps needed

  // --------------------------------------------------------------------------
  // Event dispatch
  // --------------------------------------------------------------------------

  const handleEvent = useCallback((event: AGUIEvent) => {
    if (event.seq != null) {
      if (event.seq <= maxHandledSeqRef.current) return;
      maxHandledSeqRef.current = event.seq;
    }

    switch (event.type) {
      case 'TEXT_DELTA': {
        const { text } = event.data as { text: string };
        currentContentRef.current += text;
        const snapshot = currentContentRef.current;

        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === 'assistant') {
            return [
              ...prev.slice(0, -1),
              { ...last, content: snapshot },
            ];
          }
          return [
            ...prev,
            {
              id: generateUUID(),
              role: 'assistant' as const,
              content: snapshot,
              timestamp: new Date(),
              toolCalls: [],
            },
          ];
        });
        break;
      }

      case 'TOOL_CALL_START': {
        const { tool_name } = event.data as { tool_name: string };
        setActiveTools((prev) => [
          ...prev,
          { name: tool_name, status: 'running' },
        ]);
        break;
      }

      case 'TOOL_CALL_END': {
        const { tool_name, result } = event.data as {
          tool_name: string;
          result: string;
        };
        setActiveTools((prev) =>
          prev.map((t) =>
            t.name === tool_name
              ? { ...t, status: 'completed', result }
              : t,
          ),
        );
        break;
      }

      case 'STATE_DELTA': {
        const { key, value } = event.data as { key: string; value: unknown };
        if (key === 'turn_status' && value !== null && typeof value === 'object') {
          const next = value as TurnStatus;
          lastTurnStatusRef.current = next;
          setTurnStatus(next);
          // FRE-407: capture the turn's trace_id for the DONE handler to stamp.
          const tid = (value as { trace_id?: unknown }).trace_id;
          if (typeof tid === 'string' && tid.length > 0) {
            currentTurnTraceIdRef.current = tid;
          }
        }
        // ADR-0079 / FRE-419: server-authoritative profile change → reconcile pill.
        if (key === 'session_profile' && (value === 'local' || value === 'cloud')) {
          setServerProfile(value);
        }
        break;
      }

      case 'CONSTRAINT_PAUSE': {
        const data = event.data as unknown as ConstraintPauseData;
        setPendingConstraint({
          request_id: String(event.request_id ?? ''),
          constraint: data.constraint,
          context: data.context,
          options: data.options,
          default_option: data.default_option,
          expires_at: data.expires_at,
        });
        break;
      }

      case 'CONSTRAINT_RESOLVED': {
        const data = event.data as unknown as ConstraintResolvedData;
        const requestId = String(event.request_id ?? '');
        // Collapse the active card (if it matches) into a resolved pill.
        setPendingConstraint((prev) =>
          prev && prev.request_id === requestId ? null : prev,
        );
        setResolvedConstraints((prev) => {
          if (prev.some((r) => r.request_id === requestId)) return prev;
          return [
            ...prev,
            {
              request_id: requestId,
              constraint: data.constraint,
              action_id: data.action_id,
              resolution: data.resolution,
            },
          ];
        });
        break;
      }

      case 'CANCELLED': {
        setCancelled(true);
        setPendingConstraint(null);
        isStreamingRef.current = false; // FRE-236: keep ref in sync
        setIsStreaming(false);
        break;
      }

      case 'RUN_ERROR': {
        const data = event.data as unknown as ClassifiedErrorData;
        setClassifiedError({
          category: data.category,
          reason: data.reason,
          next_step: data.next_step,
          actions: Array.isArray(data.actions) ? data.actions : [],
          partial: Boolean(data.partial),
        });
        isStreamingRef.current = false; // FRE-236: keep ref in sync
        setIsStreaming(false);
        setActiveTools([]);
        break;
      }

      case 'INTERRUPT': {
        const { context, options } = event.data as {
          context: string;
          options: string[];
        };
        setPendingInterrupt({
          context,
          options,
          sessionId: event.session_id,
        });
        isStreamingRef.current = false; // FRE-236: keep ref in sync
        setIsStreaming(false);
        break;
      }

      case 'tool_approval_request': {
        // tool_approval_request fields are top-level in the envelope (not under data).
        const e = event as unknown as Record<string, unknown>;
        const approvalData: ToolApprovalRequestData = {
          request_id: String(e.request_id ?? ''),
          trace_id: String(e.trace_id ?? ''),
          tool: String(e.tool ?? ''),
          args: (e.args ?? {}) as Record<string, unknown>,
          risk_level: (String(e.risk_level ?? 'medium')) as 'low' | 'medium' | 'high',
          reason: String(e.reason ?? ''),
          expires_at: String(e.expires_at ?? ''),
        };
        setPendingApproval(approvalData);
        break;
      }

      case 'REPLAY_GAP': {
        // Server indicates our last_seq is older than retained events.
        // Fall back to fetching full conversation history via REST API.
        const sessionId = currentSessionRef.current;
        if (sessionId) {
          void getSessionMessages(sessionId).then((serverMsgs) => {
            const hydrated: ChatMessage[] = serverMsgs.map((m) => ({
              id: generateUUID(),
              role: m.role as 'user' | 'assistant',
              content: m.content,
              timestamp: m.timestamp ? new Date(m.timestamp) : new Date(),
              traceId: m.trace_id,
              // FRE-407: hydrated history is complete → rating control renders.
              complete: true,
              // FRE-757: preserve any stored rating across a replay-gap rehydrate
              // (mirrors normal history hydration) so the control shows the real
              // rating instead of reverting to the resting default.
              rating: m.rating,
            }));
            setMessages(hydrated);
          });
        }
        break;
      }

      case 'PONG':
        // No-op — confirms server liveness.
        break;

      case 'DONE': {
        isStreamingRef.current = false; // FRE-236: keep ref in sync
        setIsStreaming(false);
        setIsReconnecting(false); // FRE-236: clear reconnect banner on turn completion
        // FRE-236: clear any draft persisted during a background-hide event.
        if (currentSessionRef.current) {
          localStorage.removeItem(DRAFT_KEY(currentSessionRef.current));
        }
        setActiveTools([]);
        // FRE-407: the turn is complete — mark the assistant message complete
        // (unconditionally) and stamp its trace_id so TurnRating can render.
        // trace_id arrives on the turn_status STATE_DELTA (stashed in the ref);
        // fall back to event.trace_id if a future DONE payload carries it.
        const doneTraceId = event.trace_id || currentTurnTraceIdRef.current || '';
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === 'assistant') {
            return [
              ...prev.slice(0, -1),
              { ...last, traceId: doneTraceId || last.traceId, complete: true },
            ];
          }
          return prev;
        });

        // FRE-757: persist the resting "ok" default on send. Fire ONLY here, on
        // a live turn's completion — never from the rating control's mount — so
        // historical / replayed turns never auto-post. The write is
        // create-if-absent server-side, so it can never clobber a rating the
        // user gives after DONE; the per-trace guard avoids a duplicate on a
        // repeated DONE.
        const persistSession = currentSessionRef.current;
        if (
          doneTraceId &&
          persistSession &&
          !defaultRatedTracesRef.current.has(doneTraceId)
        ) {
          defaultRatedTracesRef.current.add(doneTraceId);
          void submitTurnRating(doneTraceId, persistSession, RATING_OK_DEFAULT, true);
        }

        currentTurnTraceIdRef.current = '';
        // FRE-575 (fold-in to FRE-573): persist the completed engagement's tool
        // state so the engagement lane restores on remount (e.g. artifact → back).
        // Write on every DONE; remove the key for zero-tool turns to avoid stale data.
        if (typeof window !== 'undefined' && currentSessionRef.current) {
          const ts = lastTurnStatusRef.current;
          const storageKey = `seshat-tool-state-${currentSessionRef.current}`;
          if (ts && ts.tool_iteration > 0) {
            localStorage.setItem(
              storageKey,
              JSON.stringify({
                tool_iteration: ts.tool_iteration,
                tool_iteration_max: ts.tool_iteration_max,
              }),
            );
          } else {
            localStorage.removeItem(storageKey);
          }
        }
        break;
      }
    }
  }, []);

  // --------------------------------------------------------------------------
  // Public API
  // --------------------------------------------------------------------------

  const sendMessage = useCallback(
    async (text: string, sessionId: string, profile: ExecutionProfile, attachments?: UploadedAttachment[]) => {
      // Close any existing stream.
      streamRef.current?.close();
      streamRef.current = null;

      if (currentSessionRef.current !== sessionId) {
        maxHandledSeqRef.current = 0;
      }
      currentContentRef.current = '';
      currentSessionRef.current = sessionId;
      lastTurnStatusRef.current = null;

      // FRE-236: clear any stale draft and reconnect state from a previous turn.
      localStorage.removeItem(DRAFT_KEY(sessionId));
      setIsReconnecting(false);

      // Optimistically add the user message.
      const userMessage: ChatMessage = {
        id: generateUUID(),
        role: 'user',
        content: text,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, userMessage]);
      isStreamingRef.current = true; // FRE-236: keep ref in sync
      setIsStreaming(true);
      setPendingInterrupt(null);
      setPendingApproval(null);
      setBudgetDenied(null);
      setClassifiedError(null);
      setActiveTools([]);
      setPendingConstraint(null);
      setResolvedConstraints([]);
      setCancelled(false);
      // Note: turnStatus is intentionally NOT reset here — the status bar stays
      // visible between turns (showing the last turn's metrics) and is
      // overwritten by the first turn_status of the new turn (ADR-0076).

      // 1. Connect WebSocket BEFORE sending the message so we don't miss
      //    events from the background task. The old SSE flow had the same
      //    ordering requirement.
      streamRef.current = connectWebSocket(
        sessionId,
        handleEvent,
        () => {
          // WS error — connection may have dropped.
          // Reconnect logic is handled inside connectWebSocket.
        },
        {
          // FRE-236: set isReconnecting when WS drops unexpectedly mid-turn.
          onWsDisconnected: () => {
            if (isStreamingRef.current) {
              setIsReconnecting(true);
            }
          },
          // FRE-236: clear isReconnecting when WS (re)connects.
          onWsConnected: () => {
            setIsReconnecting(false);
          },
        },
      );

      // 2. Send the message (triggers backend processing).
      // Generate a per-send idempotency key so the server can deduplicate
      // a second POST that might arrive if the WS reconnects mid-request (FRE-392).
      const clientMsgId = generateUUID();
      try {
        await sendChatMessage({ message: text, sessionId, profile, clientMsgId, attachments });
      } catch (err) {
        isStreamingRef.current = false; // FRE-236: keep ref in sync
        setIsStreaming(false);
        streamRef.current?.close();
        streamRef.current = null;
        if (err instanceof BudgetDeniedError) {
          setBudgetDenied(err);
          return;
        }
        setMessages((prev) => [
          ...prev,
          {
            id: generateUUID(),
            role: 'assistant',
            content: `Error contacting Seshat: ${err instanceof Error ? err.message : String(err)}`,
            timestamp: new Date(),
          },
        ]);
        return;
      }
    },
    [handleEvent],
  );

  const resolveInterrupt = useCallback((choice: string) => {
    // Send INTERRUPT_RESPONSE over WebSocket.
    if (streamRef.current && pendingInterrupt) {
      streamRef.current.send({
        type: 'INTERRUPT_RESPONSE',
        request_id: '', // Interrupts don't use request_id yet
        choice,
      });
    }
    setPendingInterrupt(null);
    isStreamingRef.current = true; // FRE-236: keep ref in sync
    setIsStreaming(true);
  }, [pendingInterrupt]);

  /**
   * Post an approve/deny decision for the current pendingApproval via WebSocket.
   *
   * Clears pendingApproval immediately (optimistic). Errors are logged to
   * console but do not crash — the backend will auto-deny when the request expires.
   */
  const handleApprovalDecision = useCallback(
    (decision: 'approve' | 'deny'): void => {
      if (pendingApproval === null) return;

      const { request_id } = pendingApproval;

      // Optimistically clear the modal.
      setPendingApproval(null);

      // Send decision over WebSocket instead of POST.
      if (streamRef.current) {
        streamRef.current.send({
          type: 'APPROVAL_DECISION',
          request_id,
          decision,
        });
      }
    },
    [pendingApproval],
  );

  /**
   * Send a constraint decision (ADR-0076) over WebSocket and optimistically
   * collapse the DecisionCard. The matching CONSTRAINT_RESOLVED event from the
   * backend records the final pill; this just removes the interactive card.
   */
  const sendConstraintDecision = useCallback(
    (requestId: string, actionId: string, remember: boolean): void => {
      setPendingConstraint((prev) =>
        prev && prev.request_id === requestId ? null : prev,
      );
      streamRef.current?.send({
        type: 'CONSTRAINT_DECISION',
        request_id: requestId,
        decision: actionId,
        remember,
      });
    },
    [],
  );

  /** Send a USER_CANCEL (Stop button) to halt the current turn (ADR-0076). */
  const sendUserCancel = useCallback((): void => {
    streamRef.current?.send({ type: 'USER_CANCEL' });
  }, []);

  const dismissClassifiedError = useCallback((): void => {
    setClassifiedError(null);
  }, []);

  const disconnect = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    isStreamingRef.current = false; // FRE-236: keep ref in sync
    setIsStreaming(false);
    setIsReconnecting(false);
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
    currentContentRef.current = '';
    maxHandledSeqRef.current = 0;
  }, []);

  const seedMessages = useCallback((msgs: ChatMessage[]) => {
    setMessages(msgs);
    currentContentRef.current = '';
    maxHandledSeqRef.current = 0;
  }, []);

  // FRE-426: seed the status bar from server state on mount/switch so the
  // context + cost meters are populated before the first live turn_status.
  const seedTurnStatus = useCallback((status: TurnStatus) => {
    setTurnStatus(status);
  }, []);

  return {
    messages,
    isStreaming,
    isReconnecting,
    activeTools,
    turnStatus,
    serverProfile,
    pendingConstraint,
    resolvedConstraints,
    cancelled,
    pendingInterrupt,
    pendingApproval,
    budgetDenied,
    classifiedError,
    dismissClassifiedError,
    sendMessage,
    resolveInterrupt,
    handleApprovalDecision,
    sendConstraintDecision,
    sendUserCancel,
    disconnect,
    clearMessages,
    seedMessages,
    seedTurnStatus,
  };
}
