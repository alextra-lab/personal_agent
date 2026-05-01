'use client';

import { useState, useCallback, useRef } from 'react';

import {
  BudgetDeniedError,
  connectToStream,
  postApprovalDecision,
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
 * React hook that manages the full AG-UI streaming lifecycle.
 *
 * Handles:
 * - Sending user messages to the Seshat backend.
 * - Connecting to the AG-UI SSE stream.
 * - Assembling streaming text deltas into assistant messages.
 * - Tracking tool call lifecycle (TOOL_CALL_START → TOOL_CALL_END).
 * - Surfacing context budget from STATE_DELTA events.
 * - Capturing HITL INTERRUPT events and providing a resolve callback.
 * - Cleaning up the EventSource on DONE or error.
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

  // --------------------------------------------------------------------------
  // Event dispatch
  // --------------------------------------------------------------------------

  const handleEvent = useCallback((event: AGUIEvent) => {
    switch (event.type) {
      case 'TEXT_DELTA': {
        const { text } = event.data as { text: string };
        currentContentRef.current += text;
        const snapshot = currentContentRef.current;

        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === 'assistant') {
            // Update the in-progress assistant message.
            return [
              ...prev.slice(0, -1),
              { ...last, content: snapshot },
            ];
          }
          // First delta for this turn — create the assistant message.
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
        // Pause streaming indicator — we're waiting for user input.
        setIsStreaming(false);
        break;
      }

      case 'tool_approval_request': {
        // Agent is blocked waiting for a tool-approval decision.
        // The stream remains open — do NOT set isStreaming=false here.
        // The ApprovalModal will call handleApprovalDecision when resolved.
        setPendingApproval(event.data as unknown as ToolApprovalRequestData);
        break;
      }

      case 'DONE': {
        streamRef.current?.close();
        streamRef.current = null;
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

      // 1. Send the message (triggers backend processing).
      try {
        await sendChatMessage({ message: text, sessionId, profile });
      } catch (err) {
        setIsStreaming(false);
        if (err instanceof BudgetDeniedError) {
          // ADR-0065 / FRE-306: render an explicit error card, not an empty
          // assistant turn. The card consumes `budgetDenied` from the hook.
          setBudgetDenied(err);
          return;
        }
        // Append an error pseudo-message so the user can see what happened.
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

      // 2. Connect to the SSE stream.
      streamRef.current = connectToStream(
        sessionId,
        handleEvent,
        () => {
          // SSE error — stream may have ended or backend restarted.
          streamRef.current = null;
          setIsStreaming(false);
        },
      );
    },
    [handleEvent],
  );

  const resolveInterrupt = useCallback((choice: string) => {
    // The caller is responsible for calling resumeInterrupt() from agui-client
    // with the session ID from pendingInterrupt before calling this.
    setPendingInterrupt(null);
    setIsStreaming(true);
  }, []);

  /**
   * Post an approve/deny decision for the current pendingApproval.
   *
   * Clears pendingApproval immediately (optimistic) and sends the decision
   * to the backend. Errors are logged to console but do not crash — the
   * backend will auto-deny when the request expires.
   */
  const handleApprovalDecision = useCallback(
    (decision: 'approve' | 'deny'): void => {
      if (pendingApproval === null) return;

      const { request_id } = pendingApproval;
      const sessionId = currentSessionRef.current;

      // Optimistically clear the modal so the user sees a response immediately.
      setPendingApproval(null);

      postApprovalDecision(sessionId, request_id, decision).catch((err: unknown) => {
        console.error(
          '[useSSEStream] postApprovalDecision failed',
          { request_id, decision, error: err instanceof Error ? err.message : String(err) },
        );
      });
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
  }, []);

  const seedMessages = useCallback((msgs: ChatMessage[]) => {
    setMessages(msgs);
    currentContentRef.current = '';
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
