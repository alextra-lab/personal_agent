'use client';

import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

import Link from 'next/link';

import { getSession, getSessionMessages, setSessionSelection, type UploadedAttachment } from '@/lib/agui-client';
import { generateUUID } from '@/lib/uuid';
import { LAST_SESSION_KEY } from '@/lib/session';
import { useAgentStream } from '@/hooks/useAgentStream';
import { useSessionConfig } from '@/hooks/useSessionConfig';
import type { TurnStatus } from '@/lib/types';

import { resolutionLabel } from '@/lib/constraint-options';
import { isTurnCollapsed } from '@/lib/phase-summary';
import { toggleSafeAreaDebugOverlay } from '@/lib/safeAreaDebug';

import { ApprovalModal } from './ApprovalModal';
import { BudgetDeniedCard } from './BudgetDeniedCard';
import { ChatInput } from './ChatInput';
import { ChatMessage } from './ChatMessage';
import { ClassifiedErrorCard } from './ClassifiedErrorCard';
import { DecisionCard } from './DecisionCard';
import { LocationConsent } from './LocationConsent';
import { PhaseIndicator } from './PhaseIndicator';
import { SessionList } from './SessionList';
import { ToolIndicator } from './ToolIndicator';
import { TurnStatusBar } from './TurnStatusBar';

// FRE-575 (fold-in to FRE-573): per-session key for last completed engagement tool state.
const toolStateKey = (sid: string) => `seshat-tool-state-${sid}`;

// FRE-1401: the cold-lane reading. No context ceiling has resolved for this session
// in this process, so both meter halves render "—" (owner decision: dash, not
// rehydration — a stale numerator beside a real ceiling reads as "plenty of room").
const COLD_SESSION_TURN_STATUS: TurnStatus = {
  context_tokens: 0,
  context_max: null,
  tool_iteration: null,
  tool_iteration_max: null,
  turn_cost_usd: 0,
  session_cost_usd: 0,
  session_context_tokens: 0,
  compaction_count: 0,
  cache_reset_count: 0,
  quality_alert_count: 0,
  quality_alert: null,
};

// FRE-1269 follow-up: gesture trigger for the safe-area debug overlay, for
// launch modes (standalone home-screen PWA) with no URL bar to carry
// ?debug=safearea. 5 taps within 3s — deliberately unlikely to fire from
// normal title taps, which don't otherwise do anything.
const DEBUG_GESTURE_TAP_COUNT = 5;
const DEBUG_GESTURE_WINDOW_MS = 3000;

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

  // ADR-0121 §4: no client-side default — a brand-new conversation's picker
  // choice lives here only until the first message persists it server-side.
  const [pendingSelection, setPendingSelection] = useState<string | null>(null);

  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [lastUserMessage, setLastUserMessage] = useState<string>('');
  const [lastAttachments, setLastAttachments] = useState<UploadedAttachment[]>([]);
  const [sessionTurnCount, setSessionTurnCount] = useState<number | null>(null);
  // Header line 1 (FRE-1264) — no "project" concept exists in this app's data
  // model, so the header's two lines are session title over turn count
  // rather than the ticket's literal "session name over project name".
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);

  const {
    roles: configRoles,
    hydrated: configHydrated,
    refetch: refetchConfig,
  } = useSessionConfig(sessionId);
  const modelCandidates = configRoles.primary?.candidates ?? [];
  const selectedModelKey = pendingSelection ?? configRoles.primary?.resolved ?? null;

  useEffect(() => {
    if (!isDrawerOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsDrawerOpen(false);
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isDrawerOpen]);

  const debugGestureTaps = useRef<number[]>([]);
  const handleTitleTap = useCallback(() => {
    const now = Date.now();
    const recent = debugGestureTaps.current.filter((t) => now - t < DEBUG_GESTURE_WINDOW_MS);
    recent.push(now);
    if (recent.length >= DEBUG_GESTURE_TAP_COUNT) {
      debugGestureTaps.current = [];
      toggleSafeAreaDebugOverlay();
    } else {
      debugGestureTaps.current = recent;
    }
  }, []);

  const handleModelChange = useCallback(
    (key: string) => {
      setPendingSelection(key);
      if (!sessionId) return; // No DB row yet — sent with the first message instead.
      void setSessionSelection(sessionId, 'primary', key)
        .then(() => {
          setPendingSelection(null);
          refetchConfig();
        })
        .catch((err: unknown) => {
          // 404 = session not in DB yet (new session, no messages sent). The
          // choice is held in pendingSelection and sent with the first message.
          if (err instanceof Error && err.message.includes('404')) return;
          console.error('Failed to set model selection', err);
          setPendingSelection(null);
        });
    },
    [sessionId, refetchConfig],
  );

  const {
    messages,
    isStreaming,
    isReconnecting,
    activeTools,
    phases,
    turnStatus,
    serverSelection,
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
  } = useAgentStream();

  // Reconcile the picker when the server broadcasts a selection change to the
  // active socket (ADR-0121 §4 — e.g. a change made elsewhere, or the live
  // confirmation of a selection this tab just sent with a new session's first
  // message).
  useEffect(() => {
    if (serverSelection && serverSelection.role === 'primary') {
      setPendingSelection(null);
      refetchConfig();
    }
  }, [serverSelection, refetchConfig]);

  // FRE-1401: reset the status bar the instant the session changes, before the
  // browser paints — a layout effect (not a passive one) so a session switch
  // never carries the previous session's ctx/cost/tools reading, even for one
  // rendered frame.
  useLayoutEffect(() => {
    if (!sessionId) return;
    seedTurnStatus(COLD_SESSION_TURN_STATUS);
  }, [sessionId, seedTurnStatus]);

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

    // FRE-426: also hydrate turn/cost status. Model-selection hydration is
    // handled separately by useSessionConfig (ADR-0121 §4).
    getSession(sessionId)
      .then((s) => {
        if (cancelled || s === null) return;
        if (s.turn_count !== undefined) setSessionTurnCount(s.turn_count);
        setSessionTitle(s.session_label ?? s.title ?? null);
        // FRE-575 (fold-in to FRE-573): restore last completed engagement tool
        // state from localStorage so the engagement lane doesn't reset on
        // remount (e.g. after navigating to an artifact and back).
        // FRE-928 AC-4 / FRE-935: with nothing stored, the lane stays UNKNOWN.
        // It must never be seeded with an invented ceiling — a fabricated 6 once
        // rendered an amber near-limit warning on a turn whose real ceiling was 25.
        let restoredTool: { tool_iteration: number | null; tool_iteration_max: number | null } = {
          tool_iteration: null,
          tool_iteration_max: null,
        };
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
        // FRE-1401: restore cost + tools only. The context ceiling is resolved
        // per-turn by the live projector (ADR-0092 §D3, process-local) and must
        // never be rehydrated from a durable source (owner decision, FRE-1401) —
        // it stays the cold-lane "—" until a live turn_status resolves it. Merge
        // onto whatever is current (an updater, not a plain value) rather than
        // overwriting outright: a live turn_status can resolve a real ceiling
        // before this REST call returns, and this must not null it back out.
        seedTurnStatus((prev) => ({
          ...(prev ?? COLD_SESSION_TURN_STATUS),
          tool_iteration: restoredTool.tool_iteration,
          tool_iteration_max: restoredTool.tool_iteration_max,
          turn_cost_usd: s.cost_usd ?? 0,
          session_cost_usd: s.cost_usd ?? 0,
        }));
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
    // block: 'nearest' — the default 'start' computes a nonzero scroll delta
    // for every scrollable ancestor in the chain, including the document
    // itself. 'nearest' still walks the same chain, but for an ancestor
    // where the target is already (or becomes, after the nested message
    // list scrolls) within view, it computes zero movement — so in practice
    // the document itself never moves (FRE-1266 AC-1).
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }, [messages, activeTools, phases, pendingConstraint, classifiedError]);

  const handleSend = (text: string, attachments: UploadedAttachment[]) => {
    if (!sessionId) return;
    localStorage.setItem(LAST_SESSION_KEY, sessionId);
    setLastUserMessage(text);
    setLastAttachments(attachments);
    // ADR-0121 §4: send the picker's choice so a NEW session adopts it; the
    // server ignores it for an existing session (stored selection wins).
    sendMessage(text, sessionId, pendingSelection ?? undefined, attachments);
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
    <div className="relative flex flex-col h-full bg-bg text-ink">
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
          <div className="absolute inset-y-0 left-0 z-30 w-full md:w-80 bg-bg border-r border-line flex flex-col">
            {/* Drawer header */}
            <div
              className="flex items-center justify-between px-4 border-b border-line flex-shrink-0"
              style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
            >
              <h2 className="text-sm font-semibold text-ink">Conversations</h2>
              <button
                onClick={() => setIsDrawerOpen(false)}
                aria-label="Close session list"
                className="p-1 rounded text-ink-muted hover:text-ink transition-colors"
              >
                ✕
              </button>
            </div>
            {/* Artifacts nav link — FRE-368 */}
            <Link
              href="/artifacts"
              onClick={() => setIsDrawerOpen(false)}
              className="flex items-center gap-2 px-4 py-2.5 text-sm text-ink-muted hover:text-ink hover:bg-surface border-b border-line/50 transition-colors"
            >
              <span aria-hidden="true">📎</span>
              Artifacts
            </Link>

            {/* Observe nav link — ADR-0121 §5 (FRE-920): resolved bindings + provider table */}
            <Link
              href={sessionId ? `/observe?session=${sessionId}` : '/observe'}
              onClick={() => setIsDrawerOpen(false)}
              className="flex items-center gap-2 px-4 py-2.5 text-sm text-ink-muted hover:text-ink hover:bg-surface border-b border-line/50 transition-colors"
            >
              <span aria-hidden="true">🔍</span>
              Observe
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

      {/* Header — safe-area top padding. Centred two-line title (FRE-1264):
          session title over turn count. This app's data model has no
          "project" concept (see the sessionTitle state declaration above),
          so turn count — real per-session metadata already fetched here —
          stands in for the ticket's literal "project name" line rather than
          a fabricated field or static branding text. */}
      <header
        className="flex items-center justify-between px-4 border-b border-line bg-bg/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <button
          onClick={() => setIsDrawerOpen(true)}
          aria-label="Open session list"
          className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-ink-muted hover:text-ink hover:bg-surface transition-colors"
        >
          {/* Hamburger icon — three horizontal lines */}
          <svg width="16" height="16" viewBox="0 0 18 18" fill="currentColor">
            <rect x="0" y="3" width="18" height="2" rx="1" />
            <rect x="0" y="8" width="18" height="2" rx="1" />
            <rect x="0" y="13" width="18" height="2" rx="1" />
          </svg>
        </button>

        {/* onClick: FRE-1269 follow-up debug-overlay gesture (5 taps/3s) —
            see handleTitleTap. No visual affordance by design; a normal tap
            or two does nothing. */}
        <div
          data-testid="header-title"
          className="flex flex-col items-center min-w-0 px-2"
          onClick={handleTitleTap}
        >
          <h1 className="text-sm font-semibold text-ink truncate max-w-[55vw]">
            {sessionTitle ?? 'New conversation'}
          </h1>
          <span className="text-xs text-ink-muted">
            {sessionTurnCount !== null && sessionTurnCount > 0
              ? `${sessionTurnCount} ${sessionTurnCount === 1 ? 'turn' : 'turns'}`
              : 'Seshat'}
          </span>
        </div>

        {messages.length > 0 ? (
          <button
            onClick={handleNewConversation}
            aria-label="New conversation"
            className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-ink-muted hover:text-ink hover:bg-surface transition-colors"
          >
            {/* Plus icon */}
            <svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor">
              <path d="M10 4a.75.75 0 01.75.75v4.5h4.5a.75.75 0 010 1.5h-4.5v4.5a.75.75 0 01-1.5 0v-4.5h-4.5a.75.75 0 010-1.5h4.5v-4.5A.75.75 0 0110 4z" />
            </svg>
          </button>
        ) : (
          // Symmetric spacer so the title stays centred when there's no New button.
          <div className="w-8 h-8 flex-shrink-0" aria-hidden="true" />
        )}
      </header>

      {/* Message list */}
      {/* relative: without it, this scroll container is `position: static` while
          its direct parent (this component's root div) is `position: relative` —
          so any `position: absolute` descendant (e.g. the sr-only labels in
          ChatMessage) is contained by that ancestor instead of by this clipped
          scroller, and its static-position offset (based on the message's
          unclipped flow position, which can be thousands of px into a long
          conversation) escapes the clip and pushes the *document* itself into
          overflow even though nothing is visibly out of place (FRE-1266 AC-1). */}
      <main className="relative flex-1 min-h-0 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
        {/* FRE-236: reconnecting banner — shown when WS was lost mid-turn */}
        {isReconnecting && (
          <div className="sticky top-0 z-10 px-4 py-2 bg-amber-900/80 backdrop-blur-sm text-amber-200 text-xs text-center border-b border-amber-800/50">
            Reconnecting…
          </div>
        )}
        {isLoadingHistory ? (
          <div className="flex flex-col items-center justify-center h-full text-ink-muted gap-2">
            <p className="text-sm">Loading conversation…</p>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-ink-muted gap-2">
            <p className="text-sm">Ask Seshat anything...</p>
          </div>
        ) : (
          <>
            {messages.map((msg) => (
              <ChatMessage key={msg.id} message={msg} sessionId={sessionId} />
            ))}
            {isStreaming && (
              // No avatar/role label (FRE-1264 AC-5) — matches ChatMessage's
              // now-chromeless assistant layout.
              <div className="px-4 py-4 flex gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-ink-muted animate-bounce [animation-delay:0ms]" />
                <span className="w-1.5 h-1.5 rounded-full bg-ink-muted animate-bounce [animation-delay:150ms]" />
                <span className="w-1.5 h-1.5 rounded-full bg-ink-muted animate-bounce [animation-delay:300ms]" />
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
                          handleSend(lastUserMessage, lastAttachments);
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
                <span className="inline-block rounded-full bg-surface border border-line px-3 py-1 text-xs text-ink-muted">
                  ▶ {resolutionLabel(r.constraint, r.action_id, r.resolution)}
                </span>
              </div>
            ))}

            {/* Active constraint decision card (ADR-0076) */}
            {pendingConstraint && (
              <div className="px-4 py-3">
                {/* FRE-928: key by request_id. With a queue, answering card A advances
                    pendingConstraint straight to card B without passing through null, so
                    React would otherwise reuse the same DecisionCard instance — and its
                    internal one-shot `decidedRef` latch would leave B's buttons dead. */}
                <DecisionCard
                  key={pendingConstraint.request_id}
                  pending={pendingConstraint}
                  onDecide={(actionId, remember) =>
                    sendConstraintDecision(pendingConstraint.request_id, actionId, remember)
                  }
                  builderCandidates={configRoles.artifact_builder?.candidates}
                />
              </div>
            )}

            {/* Stopped-by-user pill (ADR-0076) */}
            {cancelled && (
              <div className="px-4 py-1">
                <span className="inline-block rounded-full bg-surface border border-line px-3 py-1 text-xs text-ink-muted">
                  ■ Stopped by user
                </span>
              </div>
            )}
          </>
        )}

        {/* HITL interrupt card — light-default + dark: override (same pattern
            as BudgetDeniedCard.tsx) so the translucent amber wash doesn't
            wash out to near-white under a light page background. */}
        {pendingInterrupt && (
          <div className="mx-4 my-4 p-4 rounded-xl border border-amber-300 bg-amber-50 dark:border-amber-700/50 dark:bg-amber-900/20">
            <p className="text-sm font-medium text-amber-900 dark:text-amber-300 mb-1">Approval needed</p>
            <p className="text-sm text-ink mb-3">{pendingInterrupt.context}</p>
            <div className="flex gap-2 flex-wrap">
              {pendingInterrupt.options.map((option) => (
                <button
                  key={option}
                  onClick={() => handleInterruptChoice(option)}
                  className="px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors border-amber-500 text-amber-900 hover:bg-amber-100 dark:border-amber-600 dark:text-amber-300 dark:hover:bg-amber-800/40"
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
        {/* ADR-0123 §7 (FRE-937): once the current turn has collapsed into its
            transcript summary, the live footer surfaces stop rendering — not
            gated on isStreaming, since INTERRUPT also clears it mid-turn while
            a human-wait phase must keep showing. */}
        {!isTurnCollapsed(messages) && (
          <>
            <PhaseIndicator phases={phases} />
            <ToolIndicator tools={activeTools} />
          </>
        )}
        <ChatInput
          onSend={handleSend}
          // Block Send only while a decision is pending; streaming shows Stop,
          // and the textarea stays writable so the user can compose ahead (FRE-421).
          disabled={pendingInterrupt !== null || pendingApproval !== null}
          isStreaming={isStreaming}
          onStop={sendUserCancel}
          candidates={modelCandidates}
          selectedModelKey={selectedModelKey}
          modelHydrated={configHydrated || pendingSelection !== null}
          onModelChange={handleModelChange}
        />
      </footer>
    </div>
  );
}
