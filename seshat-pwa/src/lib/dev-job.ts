/**
 * Helpers for the `dev_job` STATE_DELTA (ADR-0159 D10.6, FRE-1688): the shape guard, the merge
 * of a new card onto the shown one, and the money format.
 */

import type {
  DevJobCardData,
  DevJobCheckResult,
  DevJobCheckStep,
  DevJobState,
} from '@/lib/types';

const STATES: readonly DevJobState[] = [
  'queued',
  'running',
  'checking',
  'delivering',
  'succeeded',
  'failed',
  'failed_check',
  'cap_reached',
  'cost_unknown',
  'timed_out',
  'cancelled',
];

function isStringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

/**
 * Keep the check result only when every field that the card reads has its shape. A bad result
 * is dropped and the card still shows, so a malformed field cannot throw in the render.
 */
function parseCheckResult(value: unknown): DevJobCheckResult | null {
  if (value === null || typeof value !== 'object') return null;
  const v = value as Record<string, unknown>;
  const rawSteps = v.steps ?? [];
  if (!Array.isArray(rawSteps)) return null;
  const steps: DevJobCheckStep[] = [];
  for (const raw of rawSteps) {
    if (raw === null || typeof raw !== 'object') return null;
    const r = raw as Record<string, unknown>;
    if (typeof r.name !== 'string' || typeof r.passed !== 'boolean') return null;
    const messages = r.messages ?? [];
    if (!Array.isArray(messages) || !messages.every((m) => typeof m === 'string')) return null;
    steps.push({ name: r.name, passed: r.passed, messages: messages as string[] });
  }
  return { passed: v.passed === true, steps };
}

/**
 * Check the shape of a `dev_job` payload. A malformed payload is ignored, never thrown on.
 *
 * @param value - The STATE_DELTA value.
 * @returns The card, or null when the value is not a card.
 */
export function parseDevJobCard(value: unknown): DevJobCardData | null {
  if (value === null || typeof value !== 'object') return null;
  const v = value as Record<string, unknown>;
  if (typeof v.job_id !== 'string' || v.job_id.length === 0) return null;
  if (!STATES.includes(v.state as DevJobState)) return null;
  if (v.event !== 'state' && v.event !== 'step' && v.event !== 'end') return null;
  if (typeof v.ended !== 'boolean') return null;
  for (const key of ['step', 'spend_usd', 'cap_usd', 'failure_reason', 'repo_url', 'result_artifact_id']) {
    if (!isStringOrNull(v[key])) return null;
  }
  if (v.main_moved !== null && typeof v.main_moved !== 'boolean') return null;
  return {
    event: v.event,
    job_id: v.job_id,
    mode: v.mode === 'fix' ? 'fix' : 'build',
    state: v.state as DevJobState,
    ended: v.ended,
    step: v.step as string | null,
    spend_usd: v.spend_usd as string | null,
    cap_usd: v.cap_usd as string | null,
    failure_reason: v.failure_reason as string | null,
    check_result: parseCheckResult(v.check_result),
    repo_url: v.repo_url as string | null,
    result_artifact_id: v.result_artifact_id as string | null,
    main_moved: v.main_moved as boolean | null,
  };
}

/**
 * Fold a new card onto the shown one.
 *
 * An ended card of a job is never replaced by an older card of the same job. A card of the same
 * running job without a step keeps the latest step: the attach snapshot has none.
 *
 * @param prev - The shown card, or null.
 * @param next - The card that arrived.
 * @returns The card to show.
 */
export function mergeDevJob(prev: DevJobCardData | null, next: DevJobCardData): DevJobCardData {
  if (prev === null || prev.job_id !== next.job_id) return next;
  if (prev.ended && !next.ended) return prev;
  if (next.step === null && !next.ended && prev.step !== null) {
    return { ...next, step: prev.step };
  }
  return next;
}

/**
 * Format a decimal USD string from the job row.
 *
 * @param value - The row's decimal string, or null.
 * @returns Two decimals; four for an amount below one cent; a dash for null.
 */
export function formatUsd(value: string | null): string {
  if (value === null) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `$${n > 0 && n < 0.01 ? n.toFixed(4) : n.toFixed(2)}`;
}
