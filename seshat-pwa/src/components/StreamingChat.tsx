'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

import { resumeInterrupt, getSessionMessages } from '@/lib/agui-client';
import { generateUUID } from '@/lib/uuid';
import type { ExecutionProfile } from '@/lib/types';
import { useSSEStream } from '@/hooks/useSSEStream';

import { ApprovalModal } from './ApprovalModal';
import { BudgetDeniedCard } from './BudgetDeniedCard';
import { ChatInput } from './ChatInput';
import { ChatMessage } from './ChatMessage';
import { ContextBudgetMeter } from './ContextBudgetMeter';
import { SessionList } from './SessionList';
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
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);

  useEffect(() => {
    if (!isDrawerOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsDrawerOpen(false);
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isDrawerOpen]);

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
    pendingApproval,
    budgetDenied,
    sendMessage,
    resolveInterrupt,
    handleApprovalDecision,
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
            role: (m.role === 'user' || m.role === 'assistant') ? m.role : 'assistant',
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
    <div className="relative flex flex-col h-full bg-slate-900 text-slate-100">
      {/* Tool-approval modal — rendered above everything else (z-50) */}
      {pendingApproval !== null && (
        <ApprovalModal
          data={pendingApproval}
          onApprove={() => handleApprovalDecision('approve')}
          onDeny={() => handleApprovalDecision('deny')}
        />
      )}

      {/* Session list drawer */}
      {isDrawerOpen && (
        <>
          {/* Backdrop — tap to close */}
          <div
            className="absolute inset-0 z-20 bg-black/50"
            onClick={() => setIsDrawerOpen(false)}
          />
          {/* Panel */}
          <div className="absolute inset-y-0 left-0 z-30 w-full md:w-80 bg-slate-900 border-r border-slate-700 flex flex-col">
            {/* Drawer header */}
            <div
              className="flex items-center justify-between px-4 border-b border-slate-700 flex-shrink-0"
              style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
            >
              <h2 className="text-sm font-semibold text-slate-100">Conversations</h2>
              <button
                onClick={() => setIsDrawerOpen(false)}
                aria-label="Close session list"
                className="p-1 rounded text-slate-400 hover:text-slate-100 transition-colors"
              >
                ✕
              </button>
            </div>
            {/* Session list — remounts on each open (fresh fetch) */}
            <SessionList
              currentSessionId={sessionId}
              onSelect={() => setIsDrawerOpen(false)}
            />
          </div>
        </>
      )}

      {/* Header — safe-area top padding */}
      <header
        className="flex items-center justify-between px-4 border-b border-slate-700 bg-slate-900/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <div className="flex items-center gap-2">
          <button
            onClick={() => setIsDrawerOpen(true)}
            aria-label="Open session list"
            className="p-1 rounded text-slate-400 hover:text-slate-100 transition-colors"
          >
            {/* Hamburger icon — three horizontal lines */}
            <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor">
              <rect x="0" y="3" width="18" height="2" rx="1" />
              <rect x="0" y="8" width="18" height="2" rx="1" />
              <rect x="0" y="13" width="18" height="2" rx="1" />
            </svg>
          </button>
          <h1 className="text-base font-semibold text-slate-100">Seshat</h1>
        </div>
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
            {budgetDenied !== null && (
              <div className="px-4 py-3">
                <BudgetDeniedCard error={budgetDenied} />
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
          disabled={isStreaming || pendingInterrupt !== null || pendingApproval !== null}
          profile={profile}
          onProfileChange={handleProfileChange}
        />
      </footer>
    </div>
  );
}
