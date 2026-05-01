'use client';

/**
 * BudgetDeniedCard — renders the structured 503 from the backend's
 * Cost Check Gate (ADR-0065 / FRE-306).
 *
 * Replaces the empty-assistant-turn rendering that was the user-visible
 * symptom of the 2026-04-30 cap-overshoot incident: now the user sees
 * exactly which cap was hit, current spend vs the cap, and when the
 * window resets.
 */

import type { BudgetDeniedError } from '@/lib/agui-client';

export interface BudgetDeniedCardProps {
  error: BudgetDeniedError;
}

function formatResetTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      timeZoneName: 'short',
    });
  } catch {
    return iso;
  }
}

export function BudgetDeniedCard({ error }: BudgetDeniedCardProps) {
  const reset = formatResetTime(error.resetTime);
  const window = error.timeWindow.charAt(0).toUpperCase() + error.timeWindow.slice(1);
  return (
    <div
      role="alert"
      className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100"
    >
      <div className="font-semibold">Budget cap reached</div>
      <div className="mt-1 text-sm">
        The agent stopped before sending this request because the{' '}
        <span className="font-mono">{error.role}</span> {window.toLowerCase()} budget
        cap of <span className="font-mono">${error.cap}</span> would be exceeded.
      </div>
      <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-amber-700 dark:text-amber-300">Current spend</dt>
        <dd className="font-mono">${error.spend}</dd>
        <dt className="text-amber-700 dark:text-amber-300">Cap</dt>
        <dd className="font-mono">${error.cap}</dd>
        <dt className="text-amber-700 dark:text-amber-300">Window resets</dt>
        <dd>{reset}</dd>
        <dt className="text-amber-700 dark:text-amber-300">Reason</dt>
        <dd className="font-mono">{error.denialReason}</dd>
      </dl>
      <div className="mt-3 text-xs text-amber-700 dark:text-amber-300">
        To raise this cap, edit <code>config/governance/budget.yaml</code> on the
        backend and restart the service.
      </div>
    </div>
  );
}
