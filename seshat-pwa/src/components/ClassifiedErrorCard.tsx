'use client';

/**
 * ClassifiedErrorCard — renders a RUN_ERROR transport event (FRE-398).
 *
 * Surfaces the classified reason and next-step guidance inline in the
 * message stream. Action buttons (retry, switch_to_cloud, stop) are
 * labelled per the backend action ids; retry and switch-to-cloud are
 * wired in FRE-399.
 */

import type { ClassifiedErrorData } from '@/lib/types';

export interface ClassifiedErrorCardProps {
  error: ClassifiedErrorData;
  /** Re-send the last message with the current profile (FRE-399). */
  onRetry?: () => void;
  /** Switch to cloud profile and re-send the last message (FRE-399). */
  onSwitchToCloud?: () => void;
  /** Dismiss the card. */
  onDismiss: () => void;
}

const ACTION_LABELS: Record<string, string> = {
  retry: 'Retry',
  switch_to_cloud: 'Switch to Cloud',
  stop: 'Dismiss',
};

const CATEGORY_TITLES: Record<ClassifiedErrorData['category'], string> = {
  model_server: 'Model server error',
  timeout: 'Request timed out',
  connection: 'Connection error',
  rate_limit: 'Rate limit reached',
  budget_denied: 'Budget cap reached',
  generic: 'Turn failed',
};

export function ClassifiedErrorCard({
  error,
  onRetry,
  onSwitchToCloud,
  onDismiss,
}: ClassifiedErrorCardProps) {
  const title = CATEGORY_TITLES[error.category] ?? 'Turn failed';

  const handleAction = (actionId: string): void => {
    switch (actionId) {
      case 'retry':
        if (onRetry) onRetry();
        else onDismiss();
        break;
      case 'switch_to_cloud':
        if (onSwitchToCloud) onSwitchToCloud();
        else onDismiss();
        break;
      case 'stop':
      default:
        onDismiss();
    }
  };

  return (
    <div
      role="alert"
      className="rounded-lg border border-rose-400 bg-rose-950/60 p-4 text-rose-100 dark:border-rose-600"
    >
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="mt-0.5 text-rose-400">⚠</span>
        <div className="flex-1 min-w-0">
          <div className="font-semibold text-rose-200">{title}</div>
          <div className="mt-1 text-sm text-rose-100">{error.reason}</div>
          <div className="mt-1 text-sm text-rose-300">{error.next_step}</div>
          {error.partial && (
            <div className="mt-2 text-xs text-rose-400 italic">
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
                  ? 'px-3 py-1.5 rounded-lg text-sm font-semibold bg-rose-600 text-white hover:bg-rose-500 transition-colors'
                  : 'px-3 py-1.5 rounded-lg text-sm font-semibold border border-rose-500 text-rose-200 hover:bg-rose-900/50 transition-colors'
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
