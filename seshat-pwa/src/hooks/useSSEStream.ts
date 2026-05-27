'use client';

import { useState, useCallback, useRef } from 'react';

import {
  BudgetDeniedError,
  connectWebSocket,
  getSessionMessages,
  sendChatMessage,
  type StreamConnection,
} from '@/lib/agui-client';
import { generateUUID } from '@/lib/uuid';
import type {
  AGUIEvent,
  ChatMessage,
  PendingInterrupt,
  ToolApprovalRequestData,
  ToolCall,
} from '@/lib/types';

// --------------------------------------------------------------------------
// Hook return type
// --------------------------------------------------------------------------

export interface UseSSEStreamReturn {
  messages: ChatMessage[];
  isStreaming: boolean;
  activeTools: ToolCall[];
  /** Context window utilisation in [0, 1]; null until first STATE_DELTA. */
  contextBudget: number | null;
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
  sendMessage: (text: string, sessionId: string, profile?: string) => Promise<void>;
  resolveInterrupt: (choice: string) => void;
  /** Post an approve/deny decision for the current pendingApproval. */
  handleApprovalDecision: (decision: 'approve' | 'deny') => void;
  disconnect: () => void;
  clearMessages: () => void;
  /** Replace the message list with a server-hydrated history. */
  seedMessages: (msgs: ChatMessage[]) => void;
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
  const [contextBudget, setContextBudget] = useState<number | null>(null);
  const [pendingInterrupt, setPendingInterrupt] = useState<PendingInterrupt | null>(null);
  const [pendingApproval, setPendingApproval] = useState<ToolApprovalRequestData | null>(null);
  const [budgetDenied, setBudgetDenied] = useState<BudgetDeniedError | null>(null);

  // Refs that survive re-renders without causing them.
  const streamRef = useRef<StreamConnection | null>(null);
  const currentContentRef = useRef<string>('');
  const currentSessionRef = useRef<string>('');
  const maxHandledSeqRef = useRef<number>(0);

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
        if (key === 'context_window' && typeof value === 'number') {
          setContextBudget(value);
        }
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
        setIsStreaming(false);
        setActiveTools([]);
        break;
      }
    }
  }, []);

  // --------------------------------------------------------------------------
  // Public API
  // --------------------------------------------------------------------------

  const sendMessage = useCallback(
    async (text: string, sessionId: string, profile = 'local') => {
      // Close any existing stream.
      streamRef.current?.close();
      streamRef.current = null;

      if (currentSessionRef.current !== sessionId) {
        maxHandledSeqRef.current = 0;
      }
      currentContentRef.current = '';
      currentSessionRef.current = sessionId;

      // Optimistically add the user message.
      const userMessage: ChatMessage = {
        id: generateUUID(),
        role: 'user',
        content: text,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, userMessage]);
      setIsStreaming(true);
      setPendingInterrupt(null);
      setPendingApproval(null);
      setBudgetDenied(null);
      setActiveTools([]);

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
      );

      // 2. Send the message (triggers backend processing).
      try {
        await sendChatMessage({ message: text, sessionId, profile });
      } catch (err) {
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

  const disconnect = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    setIsStreaming(false);
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

  return {
    messages,
    isStreaming,
    activeTools,
    contextBudget,
    pendingInterrupt,
    pendingApproval,
    budgetDenied,
    sendMessage,
    resolveInterrupt,
    handleApprovalDecision,
    disconnect,
    clearMessages,
    seedMessages,
  };
}
