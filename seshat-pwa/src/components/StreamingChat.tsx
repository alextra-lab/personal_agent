'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

import Link from 'next/link';

import { getSession, getSessionMessages, setSessionProfile } from '@/lib/agui-client';
import { generateUUID } from '@/lib/uuid';
import type { ExecutionProfile } from '@/lib/types';
import { useSSEStream } from '@/hooks/useSSEStream';

import { resolutionLabel } from '@/lib/constraint-options';

import { ApprovalModal } from './ApprovalModal';
import { BudgetDeniedCard } from './BudgetDeniedCard';
import { ChatInput } from './ChatInput';
import { ChatMessage } from './ChatMessage';
import { ClassifiedErrorCard } from './ClassifiedErrorCard';
import { DecisionCard } from './DecisionCard';
import { LocationConsent } from './LocationConsent';
import { SessionList } from './SessionList';
import { ToolIndicator } from './ToolIndicator';
import { TurnStatusBar } from './TurnStatusBar';

const PROFILE_STORAGE_KEY = 'seshat_profile';
const LAST_SESSION_KEY = 'seshat_last_session_id';
// FRE-575 (fold-in to FRE-573): per-session key for last completed engagement tool state.
const toolStateKey = (sid: string) => `seshat-tool-state-${sid}`;

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
  const [lastUserMessage, setLastUserMessage] = useState<string>('');

  useEffect(() => {
    if (!isDrawerOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsDrawerOpen(false);
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isDrawerOpen]);

  const handleProfileChange = useCallback(
    (p: ExecutionProfile) => {
      const previous = profile;
      // Optimistic update + fast-paint cache.
      setProfile(p);
      if (typeof window !== 'undefined') {
        localStorage.setItem(PROFILE_STORAGE_KEY, p);
      }
      if (!sessionId) return;
      // ADR-0079 / FRE-419: the toggle is a server-side write. Revert the pill
      // if the write fails so the UI never diverges from the server.
      void setSessionProfile(sessionId, p).catch((err: unknown) => {
        // 404 = session not in DB yet (new session, no messages sent). The profile
        // is already in localStorage and will be sent with the first message —
        // no revert needed.
        if (err instanceof Error && err.message.includes('404')) return;
        console.error('Failed to set session profile; reverting', err);
        setProfile(previous);
        if (typeof window !== 'undefined') {
          localStorage.setItem(PROFILE_STORAGE_KEY, previous);
        }
      });
    },
    [sessionId, profile],
  );

  const {
    messages,
    isStreaming,
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
    seedMessages,
    seedTurnStatus,
  } = useSSEStream();

  // Reconcile the pill when the server broadcasts a profile change to the
  // active socket (ADR-0079 / FRE-419 — e.g. a change made elsewhere).
  useEffect(() => {
    if (serverProfile && serverProfile !== profile) {
      setProfile(serverProfile);
      if (typeof window !== 'undefined') {
        localStorage.setItem(PROFILE_STORAGE_KEY, serverProfile);
      }
    }
  }, [serverProfile, profile]);

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
            // FRE-407: hydrated history is already complete — mark it so the
            // rating control renders on every past assistant turn, not just the
            // live one (the DONE handler only stamps the most recent message).
            complete: true,
            // FRE-426: seed the previously-submitted rating so a rated turn
            // renders solid (vs faint default) across reloads.
            rating: m.rating,
          })),
        );
      })
      .catch(() => {
        // Treat fetch errors as empty history — present new-session UX.
      })
      .finally(() => {
        if (!cancelled) setIsLoadingHistory(false);
      });

    // ADR-0079 / FRE-419: hydrate the profile pill from the server (source of
    // truth) so an iOS reload / second device can't leave it on the cached
    // `local` default. 404 (new session) keeps the cached value.
    getSession(sessionId)
      .then((s) => {
        if (cancelled || s === null) return;
        setProfile(s.execution_profile);
        if (typeof window !== 'undefined') {
          localStorage.setItem(PROFILE_STORAGE_KEY, s.execution_profile);
        }
        // FRE-426: seed the status bar so context + cost show on mount/switch,
        // before the first live turn_status. Corrected by the next turn.
        if (s.context_tokens !== undefined && s.context_max !== undefined) {
          // FRE-575 (fold-in to FRE-573): restore last completed engagement tool
          // state from localStorage so the engagement lane doesn't show 0/6 on
          // remount (e.g. after navigating to an artifact and back).
          let restoredTool = { tool_iteration: 0, tool_iteration_max: 6 };
          if (typeof window !== 'undefined' && sessionId) {
            try {
              const raw = localStorage.getItem(toolStateKey(sessionId));
              if (raw) {
                const parsed = JSON.parse(raw) as { tool_iteration: number; tool_iteration_max: number };
                if (typeof parsed.tool_iteration === 'number' && typeof parsed.tool_iteration_max === 'number') {
                  restoredTool = parsed;
                }
              }
            } catch {
              // Corrupt localStorage entry — keep defaults.
            }
          }
          seedTurnStatus({
            context_tokens: s.context_tokens,
            context_max: s.context_max,
            tool_iteration: restoredTool.tool_iteration,
            tool_iteration_max: restoredTool.tool_iteration_max,
            turn_cost_usd: s.cost_usd ?? 0,
            // ADR-0092 session lane — corrected by first live turn_status.
            session_cost_usd: s.cost_usd ?? 0,
            session_context_tokens: s.context_tokens,
            compaction_count: 0,
            cache_reset_count: 0,
            quality_alert_count: 0,
            quality_alert: null,
          });
        }
      })
      .catch(() => {
        // Keep the cached pill on a transient fetch error.
      });

    return () => {
      cancelled = true;
    };
  }, [sessionId, seedMessages, seedTurnStatus]);

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, activeTools, pendingConstraint, classifiedError]);

  const handleSend = (text: string) => {
    if (!sessionId) return;
    localStorage.setItem(LAST_SESSION_KEY, sessionId);
    setLastUserMessage(text);
    // ADR-0079: send the pill so a NEW session is established with the user's
    // selection; the server ignores it for an existing session (stored wins).
    sendMessage(text, sessionId, profile);
  };

  const handleInterruptChoice = (choice: string) => {
    if (!sessionId) return;
    resolveInterrupt(choice);
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
            {/* Artifacts nav link — FRE-368 */}
            <Link
              href="/artifacts"
              onClick={() => setIsDrawerOpen(false)}
              className="flex items-center gap-2 px-4 py-2.5 text-sm text-slate-400 hover:text-slate-100 hover:bg-slate-800 border-b border-slate-700/50 transition-colors"
            >
              <span aria-hidden="true">📎</span>
              Artifacts
            </Link>

            {/* Location consent toggle — FRE-230 (hidden when operator gate off) */}
            <LocationConsent />

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
              <ChatMessage key={msg.id} message={msg} sessionId={sessionId} />
            ))}
            {isStreaming && (
              <div className="px-4 py-5 border-b border-slate-800/60">
                <div className="flex items-center gap-2.5 mb-2">
                  <div className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-semibold flex-shrink-0 bg-gradient-to-br from-violet-500 to-violet-700 text-white border border-violet-300/60">
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

            {classifiedError !== null && (
              <div className="px-4 py-3">
                <ClassifiedErrorCard
                  error={classifiedError}
                  onRetry={
                    lastUserMessage
                      ? () => {
                          dismissClassifiedError();
                          handleSend(lastUserMessage);
                        }
                      : undefined
                  }
                  onSwitchToCloud={
                    lastUserMessage
                      ? () => {
                          dismissClassifiedError();
                          handleProfileChange('cloud');
                          handleSend(lastUserMessage);
                        }
                      : undefined
                  }
                  onDismiss={dismissClassifiedError}
                />
              </div>
            )}

            {/* Resolved constraint pills (ADR-0076) */}
            {resolvedConstraints.map((r) => (
              <div key={r.request_id} className="px-4 py-1">
                <span className="inline-block rounded-full bg-slate-800 border border-slate-700 px-3 py-1 text-xs text-slate-400">
                  ▶ {resolutionLabel(r.constraint, r.action_id, r.resolution)}
                </span>
              </div>
            ))}

            {/* Active constraint decision card (ADR-0076) */}
            {pendingConstraint && (
              <div className="px-4 py-3">
                <DecisionCard
                  pending={pendingConstraint}
                  onDecide={(actionId, remember) =>
                    sendConstraintDecision(pendingConstraint.request_id, actionId, remember)
                  }
                />
              </div>
            )}

            {/* Stopped-by-user pill (ADR-0076) */}
            {cancelled && (
              <div className="px-4 py-1">
                <span className="inline-block rounded-full bg-slate-800 border border-slate-700 px-3 py-1 text-xs text-slate-400">
                  ■ Stopped by user
                </span>
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
        {/* Persistent turn-status bar, co-located with the input controls.
            Stays visible after streaming ends (shows the last turn's metrics);
            self-hides only until the first turn produces a turn_status. */}
        <TurnStatusBar status={turnStatus} />
        <ToolIndicator tools={activeTools} />
        <ChatInput
          onSend={handleSend}
          // Block Send only while a decision is pending; streaming shows Stop,
          // and the textarea stays writable so the user can compose ahead (FRE-421).
          disabled={pendingInterrupt !== null || pendingApproval !== null}
          isStreaming={isStreaming}
          onStop={sendUserCancel}
          profile={profile}
          onProfileChange={handleProfileChange}
        />
      </footer>
    </div>
  );
}
