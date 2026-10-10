/**
 * ApprovalModal — full-screen overlay for primitive tool-approval decisions.
 *
 * Rendered when the backend emits a ``tool_approval_request`` event: a call that
 * the permission rules ask about (ADR-0156 D8). The agent is paused; this modal
 * shows the tool, the exact arguments and the worker that asks, and sends the
 * decision over the WebSocket via ``handleApprovalDecision`` from useAgentStream.
 * "Allow for this session" appears only when the backend offers it: a call that
 * matched no rule. An explicit ask rule is never overridden (D8.3).
 *
 * MANUAL TEST PLAN
 * ================
 * Prerequisites: backend running with a primitive tool that requires approval
 * (e.g. filesystem write, shell exec).
 *
 * 1. Send a chat message that triggers the primitive tool.
 *    Expected: modal appears over the chat, Deny button is auto-focused.
 *
 * 2. Check the risk level chip colour:
 *    - low  → green chip
 *    - medium → yellow chip
 *    - high   → red chip
 *
 * 3. The exact arguments are shown as pretty-printed JSON; for a worker the
 *    "Asked by worker" line names it.
 *
 * 4. Watch the countdown timer tick down each second toward 0.
 *
 * 5. Click Allow once — modal dismisses, agent continues execution. For an
 *    unmatched call, "Allow for this session" lets the same call run again
 *    without a card.
 *    Expected: the stream resumes, response appears in chat.
 *
 * 6. Repeat step 1, click Deny — modal dismisses, agent aborts the tool.
 *    Expected: agent reports the denial in chat.
 *
 * 7. Repeat step 1, wait for the countdown to reach 0 without clicking.
 *    Expected: modal auto-calls onDeny, agent aborts the tool.
 *
 * 8. Verify keyboard: Tab cycles between Approve/Deny; Enter activates focused button.
 *
 * 9. Verify no layout shift when the modal appears over long chat history.
 */

'use client';

import React, { useEffect, useRef, useState } from 'react';

import type { ToolApprovalRequestData } from '@/lib/types';

// --------------------------------------------------------------------------
// Props
// --------------------------------------------------------------------------

interface ApprovalModalProps {
  data: ToolApprovalRequestData;
  onApprove: () => void;
  onApproveSession: () => void;
  onDeny: () => void;
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

/** Returns the number of whole seconds remaining until the expiry timestamp. */
function secondsRemaining(expiresAt: string): number {
  const delta = new Date(expiresAt).getTime() - Date.now();
  return Math.max(0, Math.floor(delta / 1000));
}

/** Tailwind classes for the risk-level chip. */
function riskChipClasses(risk: ToolApprovalRequestData['risk_level']): string {
  switch (risk) {
    case 'low':
      return 'bg-green-100 text-green-800 border border-green-300 dark:bg-green-900/40 dark:text-green-400 dark:border-green-700/50';
    case 'medium':
      return 'bg-yellow-100 text-yellow-800 border border-yellow-300 dark:bg-yellow-900/40 dark:text-yellow-400 dark:border-yellow-700/50';
    case 'high':
      // dark:text-red-300 (not -400) — composited against bg-red-900/40 over
      // the modal's bg-surface, -400 measured 4.26:1 (just under WCAG AA);
      // -300 clears it (FRE-1265 e2e contrast check).
      return 'bg-red-100 text-red-800 border border-red-300 dark:bg-red-900/40 dark:text-red-300 dark:border-red-700/50';
  }
}

// --------------------------------------------------------------------------
// Component
// --------------------------------------------------------------------------

/**
 * Full-screen modal overlay requesting human approval before a primitive
 * tool executes. Auto-denies when the countdown reaches zero.
 *
 * Args:
 *   data:      The ``tool_approval_request`` event payload.
 *   onApprove:        Called when the user clicks Allow once.
 *   onApproveSession: Called when the user clicks Allow for this session.
 *   onDeny:           Called when the user clicks Deny or the countdown expires.
 */
export function ApprovalModal({
  data,
  onApprove,
  onApproveSession,
  onDeny,
}: ApprovalModalProps): React.JSX.Element {
  const denyRef = useRef<HTMLButtonElement>(null);

  // Countdown seconds — ticks once per second, auto-denies at 0.
  const [countdown, setCountdown] = useState<number>(() => secondsRemaining(data.expires_at));

  // Auto-focus the Deny button on mount (safer default: deny is low-risk to mistake).
  useEffect(() => {
    denyRef.current?.focus();
  }, []);

  // Guard ref — ensures onDeny() fires at most once on timeout.
  const deniedRef = useRef(false);

  // Countdown timer — tick every second; auto-deny at 0.
  useEffect(() => {
    if (countdown <= 0) {
      if (!deniedRef.current) {
        deniedRef.current = true;
        onDeny();
      }
      return;
    }

    const timerId = setInterval(() => {
      setCountdown(secondsRemaining(data.expires_at));
    }, 1000);

    return () => clearInterval(timerId);
  }, [countdown, data.expires_at, onDeny]);

  const prettyArgs = JSON.stringify(data.args, null, 2);

  return (
    /* Overlay */
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="approval-modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
    >
      {/* Modal card */}
      <div className="w-full max-w-lg rounded-2xl bg-surface border border-line shadow-2xl p-6 flex flex-col gap-4">

        {/* Header row: tool name + risk chip */}
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2
            id="approval-modal-title"
            className="text-base font-semibold text-ink truncate"
          >
            Tool approval required:{' '}
            <span className="font-mono text-violet-700 dark:text-violet-300">{data.tool}</span>
          </h2>
          <span className={`px-2 py-0.5 rounded-full text-xs font-semibold uppercase tracking-wide flex-shrink-0 ${riskChipClasses(data.risk_level)}`}>
            {data.risk_level}
          </span>
        </div>

        {/* Reason */}
        <p className="text-sm text-ink-muted leading-relaxed">
          {data.reason}
        </p>

        {/* Worker that asks (ADR-0156 D8.3) */}
        {data.worker !== null && (
          <p className="text-sm text-ink-muted">
            Asked by worker:{' '}
            <span className="font-mono text-ink">{data.worker}</span>
          </p>
        )}

        {/* Exact arguments — always shown, so the user decides on what will run */}
        <div>
          <p className="text-xs font-medium text-ink-muted">Arguments</p>
          <pre
            aria-label="Tool arguments"
            className="mt-2 p-3 rounded-lg bg-bg text-xs text-ink overflow-auto max-h-64 font-mono leading-relaxed whitespace-pre-wrap break-all"
          >
            {prettyArgs}
          </pre>
        </div>

        {/* Countdown */}
        <p className="text-xs text-ink-muted text-right">
          Auto-denies in{' '}
          <span className={countdown <= 10 ? 'text-red-700 dark:text-red-300 font-semibold' : 'text-ink-muted'}>
            {countdown}s
          </span>
        </p>

        {/* Action buttons */}
        <div className="flex gap-3 justify-end flex-wrap">
          <button
            ref={denyRef}
            onClick={onDeny}
            className="px-4 py-2 rounded-lg text-sm font-semibold border border-red-600 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 focus:ring-offset-surface transition-colors"
          >
            Deny
          </button>
          {data.session_option && (
            <button
              onClick={onApproveSession}
              className="px-4 py-2 rounded-lg text-sm font-semibold border border-green-700 text-green-800 dark:text-green-300 hover:bg-green-50 dark:hover:bg-green-900/30 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-offset-2 focus:ring-offset-surface transition-colors"
            >
              Allow for this session
            </button>
          )}
          <button
            onClick={onApprove}
            className="px-4 py-2 rounded-lg text-sm font-semibold bg-green-700 text-white hover:bg-green-600 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-offset-2 focus:ring-offset-surface transition-colors"
          >
            Allow once
          </button>
        </div>
      </div>
    </div>
  );
}
