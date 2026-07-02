/**
 * Constraint action_id → display label mapping (ADR-0076).
 *
 * Mirrors the backend registry in
 * src/personal_agent/orchestrator/constraint_options.py. The wire protocol
 * carries stable `action_id`s; the PWA maps them to human labels here so
 * button text can change without invalidating stored preferences.
 */

export const CONSTRAINT_ACTION_LABELS: Record<string, Record<string, string>> = {
  tool_iteration_limit: {
    continue_10: 'Continue (10 more)',
    finish_now: 'Finish now',
  },
  context_compression: {
    compress_continue: 'Compress and continue',
    stop_here: 'Stop here instead',
  },
  // ADR-0101 §8b / FRE-691: pre-flight cloud-attachment cost confirmation.
  attachment_cost: {
    proceed_cloud: 'Proceed on cloud',
    keep_local: 'Keep local / free',
  },
};

/** Human title for a constraint type, shown in the DecisionCard header. */
export const CONSTRAINT_TITLES: Record<string, string> = {
  tool_iteration_limit: 'Tool call limit reached',
  context_compression: 'Context window nearly full',
  attachment_cost: 'Confirm cloud attachment cost',
};

/** Map an action_id to its display label, falling back to the raw id. */
export function actionLabel(constraint: string, actionId: string): string {
  return CONSTRAINT_ACTION_LABELS[constraint]?.[actionId] ?? actionId;
}

/** Short pill text describing a resolved constraint. */
export function resolutionLabel(
  constraint: string,
  actionId: string,
  resolution: string,
): string {
  if (resolution === 'user_cancel') return 'Stopped by user';
  if (resolution === 'connection_lost') return 'Disconnected — default applied';
  const label = actionLabel(constraint, actionId);
  if (resolution === 'timeout_default') return `Timed out — ${label}`;
  return label;
}
