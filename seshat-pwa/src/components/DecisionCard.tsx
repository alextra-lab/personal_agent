'use client';

import { useEffect, useRef, useState } from 'react';

import { actionLabel, CONSTRAINT_TITLES } from '@/lib/constraint-options';
import type { PendingConstraint } from '@/lib/types';

export interface DecisionCardProps {
  pending: PendingConstraint;
  /** Send the chosen action_id + remember flag over the WebSocket. */
  onDecide: (actionId: string, remember: boolean) => void;
}

function secondsRemaining(expiresAt: string): number {
  const delta = new Date(expiresAt).getTime() - Date.now();
  return Number.isNaN(delta) ? 0 : Math.max(0, Math.floor(delta / 1000));
}

/**
 * Inline constraint-decision card (ADR-0076).
 *
 * Unlike {@link ApprovalModal} (full-screen, blocking), this renders inline in
 * the message stream and is non-blocking — the user may keep typing or hit
 * Stop. The backend's `expires_at` is authoritative for the timeout default;
 * the countdown here is informational. The card is removed by the parent when
 * the matching CONSTRAINT_RESOLVED arrives (collapsing into a pill).
 */
export function DecisionCard({ pending, onDecide }: DecisionCardProps) {
  const { constraint, context, options, expires_at } = pending;
  const [remember, setRemember] = useState(false);
  const [countdown, setCountdown] = useState(() => secondsRemaining(expires_at));
  const decidedRef = useRef(false);

  useEffect(() => {
    const id = setInterval(() => setCountdown(secondsRemaining(expires_at)), 1000);
    return () => clearInterval(id);
  }, [expires_at]);

  const title = CONSTRAINT_TITLES[constraint] ?? 'Decision needed';
  const totalWindow = Math.max(secondsRemaining(expires_at), 1);
  const pct = Math.min(100, Math.max(0, (countdown / totalWindow) * 100));

  const decide = (actionId: string): void => {
    if (decidedRef.current) return;
    decidedRef.current = true;
    onDecide(actionId, remember);
  };

  return (
    <div
      role="group"
      aria-label={title}
      className="rounded-lg border border-sky-300 bg-sky-50 p-4 text-sky-900 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-100"
    >
      <div className="font-semibold">{title}</div>
      <div className="mt-1 text-sm">{context}</div>

      <div className="mt-3 flex flex-wrap gap-2">
        {options.map((actionId, i) => (
          <button
            key={actionId}
            type="button"
            onClick={() => decide(actionId)}
            className={
              i === 0
                ? 'px-3 py-1.5 rounded-lg text-sm font-semibold bg-sky-600 text-white hover:bg-sky-500'
                : 'px-3 py-1.5 rounded-lg text-sm font-semibold border border-sky-400 text-sky-800 hover:bg-sky-100 dark:border-sky-600 dark:text-sky-200 dark:hover:bg-sky-900'
            }
          >
            {actionLabel(constraint, actionId)}
          </button>
        ))}
      </div>

      <label className="mt-3 flex items-center gap-2 text-xs text-sky-700 dark:text-sky-300 cursor-pointer">
        <input
          type="checkbox"
          checked={remember}
          onChange={(e) => setRemember(e.target.checked)}
          className="h-3.5 w-3.5"
        />
        Remember this choice
      </label>

      <div className="mt-3" title={`Default applies in ${countdown}s`}>
        <div className="h-1 w-full overflow-hidden rounded-full bg-sky-200 dark:bg-sky-900">
          <div
            className="h-full rounded-full bg-sky-500 transition-all duration-1000 ease-linear"
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="mt-1 text-[11px] font-mono text-sky-600 dark:text-sky-400">
          {countdown > 0 ? `default in ${countdown}s` : 'applying default…'}
        </div>
      </div>
    </div>
  );
}
