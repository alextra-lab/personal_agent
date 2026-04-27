/**
 * ApprovalModal — full-screen overlay for primitive tool-approval decisions.
 *
 * Rendered when the backend emits a ``tool_approval_request`` SSE event.
 * The agent is paused; this modal collects an approve/deny decision and
 * POSTs it to the backend via ``handleApprovalDecision`` from useSSEStream.
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
 * 3. Expand the "Arguments" collapsible — verify pretty-printed JSON.
 *
 * 4. Watch the countdown timer tick down each second toward 0.
 *
 * 5. Click Approve — modal dismisses, agent continues execution.
 *    Expected: SSE stream resumes, response appears in chat.
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
      return 'bg-green-900/40 text-green-400 border border-green-700/50';
    case 'medium':
      return 'bg-yellow-900/40 text-yellow-400 border border-yellow-700/50';
    case 'high':
      return 'bg-red-900/40 text-red-400 border border-red-700/50';
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
 *   data:      The ``tool_approval_request`` SSE payload.
 *   onApprove: Called when the user clicks Approve.
 *   onDeny:    Called when the user clicks Deny or the countdown expires.
 */
export function ApprovalModal({ data, onApprove, onDeny }: ApprovalModalProps): React.JSX.Element {
  const denyRef = useRef<HTMLButtonElement>(null);

  // Countdown seconds — ticks once per second, auto-denies at 0.
  const [countdown, setCountdown] = useState<number>(() => secondsRemaining(data.expires_at));

  // Auto-focus the Deny button on mount (safer default: deny is low-risk to mistake).
  useEffect(() => {
    denyRef.current?.focus();
  }, []);

  // Countdown timer — tick every second; auto-deny at 0.
  useEffect(() => {
    if (countdown <= 0) {
      onDeny();
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
      <div className="w-full max-w-lg rounded-2xl bg-slate-800 border border-slate-700 shadow-2xl p-6 flex flex-col gap-4">

        {/* Header row: tool name + risk chip */}
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2
            id="approval-modal-title"
            className="text-base font-semibold text-slate-100 truncate"
          >
            Tool approval required:{' '}
            <span className="font-mono text-orange-400">{data.tool}</span>
          </h2>
          <span className={`px-2 py-0.5 rounded-full text-xs font-semibold uppercase tracking-wide flex-shrink-0 ${riskChipClasses(data.risk_level)}`}>
            {data.risk_level}
          </span>
        </div>

        {/* Reason */}
        <p className="text-sm text-slate-300 leading-relaxed">
          {data.reason}
        </p>

        {/* Collapsible args */}
        <details className="group">
          <summary className="cursor-pointer text-xs font-medium text-slate-400 hover:text-slate-200 transition-colors select-none list-none flex items-center gap-1.5">
            <svg
              className="w-3 h-3 transition-transform group-open:rotate-90"
              viewBox="0 0 12 12"
              fill="currentColor"
            >
              <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Arguments
          </summary>
          <pre className="mt-2 p-3 rounded-lg bg-slate-900 text-xs text-slate-300 overflow-x-auto font-mono leading-relaxed">
            {prettyArgs}
          </pre>
        </details>

        {/* Countdown */}
        <p className="text-xs text-slate-500 text-right">
          Auto-denies in{' '}
          <span className={countdown <= 10 ? 'text-red-400 font-semibold' : 'text-slate-400'}>
            {countdown}s
          </span>
        </p>

        {/* Action buttons */}
        <div className="flex gap-3 justify-end">
          <button
            ref={denyRef}
            onClick={onDeny}
            className="px-4 py-2 rounded-lg text-sm font-semibold border border-red-600 text-red-400 hover:bg-red-900/30 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 focus:ring-offset-slate-800 transition-colors"
          >
            Deny
          </button>
          <button
            onClick={onApprove}
            className="px-4 py-2 rounded-lg text-sm font-semibold bg-green-700 text-white hover:bg-green-600 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-offset-2 focus:ring-offset-slate-800 transition-colors"
          >
            Approve
          </button>
        </div>
      </div>
    </div>
  );
}
