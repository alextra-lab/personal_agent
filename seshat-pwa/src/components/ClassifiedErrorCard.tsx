'use client';

/**
 * ClassifiedErrorCard — renders a RUN_ERROR transport event (FRE-398).
 *
 * Surfaces the classified reason and next-step guidance inline in the
 * message stream. Action buttons (retry, stop) are labelled per the backend
 * action ids (FRE-399). ADR-0121 T5 (FRE-920): Path is removed, so there is
 * no "switch to cloud" escalation action — retrying means retrying.
 */

import type { ClassifiedErrorData } from '@/lib/types';

export interface ClassifiedErrorCardProps {
  error: ClassifiedErrorData;
  /** Re-send the last message on the current selection (FRE-399). */
  onRetry?: () => void;
  /** Dismiss the card. */
  onDismiss: () => void;
}

const ACTION_LABELS: Record<string, string> = {
  retry: 'Retry',
  stop: 'Dismiss',
};

const CATEGORY_TITLES: Record<ClassifiedErrorData['category'], string> = {
  model_server: 'Model server error',
  timeout: 'Request timed out',
  connection: 'Connection error',
  rate_limit: 'Rate limit reached',
  budget_denied: 'Budget cap reached',
  tool_failure: 'Tool failed',
  generic: 'Turn failed',
};

export function ClassifiedErrorCard({
  error,
  onRetry,
  onDismiss,
}: ClassifiedErrorCardProps) {
  const title = CATEGORY_TITLES[error.category] ?? 'Turn failed';

  const handleAction = (actionId: string): void => {
    switch (actionId) {
      case 'retry':
        if (onRetry) onRetry();
        else onDismiss();
        break;
      case 'stop':
      default:
        onDismiss();
    }
  };

  return (
    // Fixed dark-red alert card in dark mode (#1b1416/#9f2d22 — not a token
    // value, a deliberate saturated-error look) paired with an explicit
    // light-mode red-tinted card, same two-palette pattern as DecisionCard's
    // sky treatment (FRE-1265) — the old plain `text-slate-*` on this fixed
    // dark background would render as near-black-on-dark-red in light mode.
    <div
      role="alert"
      className="rounded-lg border border-red-300 bg-red-50 p-4 text-red-950 dark:border-red-500/30 dark:bg-[#1b1416] dark:text-ink-muted"
    >
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="mt-0.5 text-red-600 dark:text-red-400">⚠</span>
        <div className="flex-1 min-w-0">
          <div className="font-semibold text-red-950 dark:text-ink">{title}</div>
          <div className="mt-1 text-sm text-red-900 dark:text-ink-muted">{error.reason}</div>
          <div className="mt-1 text-sm text-red-800 dark:text-ink-muted">{error.next_step}</div>
          {error.partial && (
            <div className="mt-2 text-xs text-red-700 dark:text-ink-muted italic">
              Partial results from this turn were salvaged above.
            </div>
          )}
        </div>
      </div>

      {error.actions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {error.actions.map((actionId, i) => (
            <button
              key={actionId}
              type="button"
              onClick={() => handleAction(actionId)}
              className={
                i === 0
                  ? 'px-3 py-1.5 rounded-lg text-sm font-semibold bg-[#9f2d22] text-red-100 hover:bg-[#b3362a] transition-colors'
                  : 'px-3 py-1.5 rounded-lg text-sm font-semibold border border-line text-ink-muted hover:bg-line/40 transition-colors'
              }
            >
              {ACTION_LABELS[actionId] ?? actionId}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
