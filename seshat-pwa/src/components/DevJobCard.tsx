'use client';

/**
 * DevJobCard — the card of a development-agent job (ADR-0159 D10.6, FRE-1688).
 *
 * Shows the state, the latest step of the OpenCode event stream and the spend against the cap.
 * At the end it shows the artifact card (success) or the failure with the repo link and the
 * check result. When main did not move to the result (D8.9), it says so.
 */

import type { DevJobCardData, DevJobState } from '@/lib/types';
import { formatUsd } from '@/lib/dev-job';
import { ArtifactCard } from './ArtifactCard';

export interface DevJobCardProps {
  card: DevJobCardData;
}

const STATE_LABELS: Record<DevJobState, string> = {
  queued: 'Waiting to start',
  running: 'Building',
  checking: 'Checking the page',
  delivering: 'Publishing',
  succeeded: 'Done',
  failed: 'Failed',
  failed_check: 'Check failed',
  cap_reached: 'Stopped: spend cap reached',
  cost_unknown: 'Stopped: cost unknown',
  timed_out: 'Stopped: time limit reached',
  cancelled: 'Cancelled',
};

const REASON_TEXT: Record<string, string> = {
  supervisor_restart: 'The gateway restarted during the job.',
  check_failed: 'The page did not pass the check.',
  time_limit: 'The job reached its time limit.',
  no_result: 'The agent ended without a result.',
};

export function DevJobCard({ card }: DevJobCardProps) {
  const failed = card.ended && card.state !== 'succeeded';
  const spend =
    card.cap_usd === null
      ? formatUsd(card.spend_usd)
      : `${formatUsd(card.spend_usd)} of ${formatUsd(card.cap_usd)}`;
  const failedSteps = (card.check_result?.steps ?? []).filter((s) => !s.passed);

  return (
    <section
      role="status"
      aria-live={card.ended ? 'off' : 'polite'}
      aria-label="Build job"
      className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-ink"
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-medium">{STATE_LABELS[card.state]}</span>
        <span data-testid="dev-job-spend" className="text-xs text-ink-muted tabular-nums">
          {spend}
        </span>
      </div>

      {card.state === 'running' && card.step !== null && (
        <p className="mt-1 truncate text-xs text-ink-muted">{card.step}</p>
      )}

      {failed && (
        <div className="mt-2 space-y-1 text-xs">
          {card.failure_reason !== null && (
            <p>{REASON_TEXT[card.failure_reason] ?? card.failure_reason}</p>
          )}
          {failedSteps.map((step) => (
            <p key={step.name} className="text-ink-muted">
              {step.name}: {(step.messages ?? []).join(' ') || 'failed'}
            </p>
          ))}
        </div>
      )}

      {card.ended && card.repo_url !== null && (
        <a
          href={card.repo_url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 inline-block text-xs text-accent underline"
        >
          Repo
        </a>
      )}

      {card.ended && card.main_moved === false && (
        <p className="mt-2 text-xs text-ink-muted">main did not move to this result.</p>
      )}

      {card.state === 'succeeded' && card.result_artifact_id !== null && (
        <div className="mt-3">
          <ArtifactCard artifactId={card.result_artifact_id} fallbackHref="/artifacts" />
        </div>
      )}
    </section>
  );
}
