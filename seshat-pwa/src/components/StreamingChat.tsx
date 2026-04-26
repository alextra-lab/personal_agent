'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

import { resumeInterrupt, getSessionMessages } from '@/lib/agui-client';
import { generateUUID } from '@/lib/uuid';
import type { ExecutionProfile } from '@/lib/types';
import { useSSEStream } from '@/hooks/useSSEStream';

import { ChatInput } from './ChatInput';
import { ChatMessage } from './ChatMessage';
import { ContextBudgetMeter } from './ContextBudgetMeter';
import { ToolIndicator } from './ToolIndicator';

const PROFILE_STORAGE_KEY = 'seshat_profile';
const LAST_SESSION_KEY = 'seshat_last_session_id';

interface StreamingChatProps {
  /** Session ID sourced from the /c/[sessionId] URL param. */
  sessionId?: string;
}

/**
 * Primary chat interface composing all sub-components.
 *
 * Session identity is driven by the URL param — this component never
 * mints or stores session IDs itself. Navigating to a new /c/{id} URL
 * causes a natural remount and state reset.
 *
 * Layout:
 * - Header: Seshat title + New button (safe-area aware)
 * - Body:   Scrollable message list (with loading skeleton while hydrating)
 * - Footer: Tool indicators + chat input with inline model selector (safe-area aware)
 */
export function StreamingChat({ sessionId }: StreamingChatProps) {
  const router = useRouter();

  const [profile, setProfile] = useState<ExecutionProfile>(() => {
    if (typeof window !== 'undefined') {
      const stored = localStorage.getItem(PROFILE_STORAGE_KEY);
      if (stored === 'local' || stored === 'cloud') return stored as ExecutionProfile;
    }
    return 'local';
  });

  const [isLoadingHistory, setIsLoadingHistory] = useState(false);

  const handleProfileChange = useCallback((p: ExecutionProfile) => {
    setProfile(p);
    if (typeof window !== 'undefined') {
      localStorage.setItem(PROFILE_STORAGE_KEY, p);
    }
  }, []);

  const {
    messages,
    isStreaming,
    activeTools,
    contextBudget,
    pendingInterrupt,
    sendMessage,
    resolveInterrupt,
    seedMessages,
  } = useSSEStream();

  // Hydrate message history from the backend when the session changes.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    setIsLoadingHistory(true);

    getSessionMessages(sessionId)
      .then((serverMsgs) => {
        if (cancelled || serverMsgs.length === 0) return;
        seedMessages(
          serverMsgs.map((m) => ({
            id: generateUUID(),
            role: m.role as 'user' | 'assistant',
            content: m.content,
            timestamp: m.timestamp ? new Date(m.timestamp) : new Date(),
            traceId: m.trace_id,
          })),
        );
      })
      .catch(() => {
        // Treat fetch errors as empty history — present new-session UX.
      })
      .finally(() => {
        if (!cancelled) setIsLoadingHistory(false);
      });

    return () => {
      cancelled = true;
    };
  }, [sessionId, seedMessages]);

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, activeTools]);

  const handleSend = (text: string) => {
    if (!sessionId) return;
    // Persist last-known session ID so root / can redirect here on next visit.
    localStorage.setItem(LAST_SESSION_KEY, sessionId);
    sendMessage(text, sessionId, profile);
  };

  const handleInterruptChoice = async (choice: string) => {
    if (!sessionId) return;
    try {
      await resumeInterrupt({ sessionId, choice });
      resolveInterrupt(choice);
    } catch {
      // Stale stream — user can retry.
    }
  };

  const handleNewConversation = () => {
    const newId = generateUUID();
    localStorage.setItem(LAST_SESSION_KEY, newId);
    router.push(`/c/${newId}`);
    // URL change triggers a remount which resets all hook state naturally.
  };

  return (
    <div className="flex flex-col h-full bg-slate-900 text-slate-100">
      {/* Header — safe-area top padding */}
      <header
        className="flex items-center justify-between px-4 border-b border-slate-700 bg-slate-900/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <h1 className="text-base font-semibold text-slate-100">Seshat</h1>
        {messages.length > 0 && (
          <button
            onClick={handleNewConversation}
            className="text-xs text-slate-500 hover:text-slate-300 transition-colors"
          >
            New
          </button>
        )}
      </header>

      {contextBudget !== null && <ContextBudgetMeter budget={contextBudget} />}

      {/* Message list */}
      <main className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
        {isLoadingHistory ? (
          <div className="flex flex-col items-center justify-center h-full text-slate-500 gap-2">
            <p className="text-sm">Loading conversation…</p>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-slate-500 gap-2">
            <p className="text-sm">Ask Seshat anything...</p>
          </div>
        ) : (
          <>
            {messages.map((msg) => (
              <ChatMessage key={msg.id} message={msg} />
            ))}
            {isStreaming && (
              <div className="px-4 py-5 border-b border-slate-800/60">
                <div className="flex items-center gap-2.5 mb-2">
                  <div className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-semibold flex-shrink-0 bg-orange-500/20 text-orange-400 border border-orange-500/30">
                    S
                  </div>
                  <span className="text-xs font-semibold text-slate-400">Seshat</span>
                </div>
                <div className="pl-[34px] flex gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-bounce [animation-delay:0ms]" />
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-bounce [animation-delay:150ms]" />
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-400 animate-bounce [animation-delay:300ms]" />
                </div>
              </div>
            )}
          </>
        )}

        {/* HITL interrupt card */}
        {pendingInterrupt && (
          <div className="mx-4 my-4 p-4 rounded-xl border border-amber-700/50 bg-amber-900/20">
            <p className="text-sm font-medium text-amber-300 mb-1">Approval needed</p>
            <p className="text-sm text-slate-300 mb-3">{pendingInterrupt.context}</p>
            <div className="flex gap-2 flex-wrap">
              {pendingInterrupt.options.map((option) => (
                <button
                  key={option}
                  onClick={() => handleInterruptChoice(option)}
                  className="px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors border-amber-600 text-amber-300 hover:bg-amber-800/40"
                >
                  {option}
                </button>
              ))}
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </main>

      {/* Footer — safe-area bottom padding handled inside ChatInput */}
      <footer className="flex-shrink-0">
        <ToolIndicator tools={activeTools} />
        <ChatInput
          onSend={handleSend}
          disabled={isStreaming || pendingInterrupt !== null}
          profile={profile}
          onProfileChange={handleProfileChange}
        />
      </footer>
    </div>
  );
}
