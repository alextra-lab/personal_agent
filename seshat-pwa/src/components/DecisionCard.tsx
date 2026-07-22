'use client';

import { useEffect, useRef, useState } from 'react';

import { actionLabel, CONSTRAINT_TITLES } from '@/lib/constraint-options';
import type { DeploymentView, PendingConstraint } from '@/lib/types';

export interface DecisionCardProps {
  pending: PendingConstraint;
  /** Send the chosen action_id + remember flag over the WebSocket. */
  onDecide: (actionId: string, remember: boolean) => void;
  /**
   * Catalog deployments for the `artifact_builder` constraint (ADR-0122 T3),
   * keyed by `DeploymentView.key` against `pending.options`. Sourced from
   * `useSessionConfig`'s `roles.artifact_builder.candidates` — descriptive
   * only: `pending.options` stays authoritative for which buttons render and
   * which `action_id` gets sent, so an option missing here (a stale config
   * poll, a different availability check) just falls back to a plain label
   * rather than dropping the option. Ignored for every other constraint.
   */
  builderCandidates?: DeploymentView[];
}

function formatBuilderDetail(candidate: DeploymentView): string {
  const parts = [
    `${candidate.provider} · ${candidate.placement}`,
    `${Math.round(candidate.context_length / 1000)}K context`,
    candidate.max_tokens != null
      ? `${Math.round(candidate.max_tokens / 1000)}K max output`
      : 'provider default max output',
  ];
  if (candidate.input_cost_per_token != null) {
    parts.push(`$${(candidate.input_cost_per_token * 1_000_000).toFixed(2)}/M in`);
  }
  if (candidate.output_cost_per_token != null) {
    parts.push(`$${(candidate.output_cost_per_token * 1_000_000).toFixed(2)}/M out`);
  }
  return parts.join(' · ');
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
export function DecisionCard({ pending, onDecide, builderCandidates }: DecisionCardProps) {
  const { constraint, context, options, expires_at } = pending;
  const [remember, setRemember] = useState(false);
  const [countdown, setCountdown] = useState(() => secondsRemaining(expires_at));
  // FRE-928: the one-shot latch is scoped to the CARD, not the component instance.
  // With a pending-constraint queue, answering one card can advance this slot straight
  // to the next without an unmount, and an instance-scoped latch would leave the new
  // card's buttons permanently dead. Callers should still key by request_id; this makes
  // the component correct either way rather than leaving a trap for the caller.
  const decidedForRef = useRef<string | null>(null);

  useEffect(() => {
    const id = setInterval(() => setCountdown(secondsRemaining(expires_at)), 1000);
    return () => clearInterval(id);
  }, [expires_at]);

  const title = CONSTRAINT_TITLES[constraint] ?? 'Decision needed';
  const totalWindow = Math.max(secondsRemaining(expires_at), 1);
  const pct = Math.min(100, Math.max(0, (countdown / totalWindow) * 100));

  const decide = (actionId: string): void => {
    // Read the ref at CALL time, not render time: a click does not re-render, so a
    // render-scoped copy would stay stale and let a double-click through.
    if (decidedForRef.current === pending.request_id) return;
    decidedForRef.current = pending.request_id;
    onDecide(actionId, remember);
  };

  return (
    <div
      role="group"
      aria-label={title}
      className="rounded-lg border border-sky-300 bg-sky-50 p-4 text-sky-900 dark:border-sky-400/45 dark:bg-[#0a1b29] dark:text-slate-100"
    >
      <div className="font-semibold dark:text-sky-50">{title}</div>
      <div className="mt-1 text-sm dark:text-slate-300">{context}</div>

      <div className="mt-3 flex flex-wrap gap-2">
        {options.map((actionId, i) => {
          const candidate =
            constraint === 'artifact_builder'
              ? builderCandidates?.find((c) => c.key === actionId)
              : undefined;

          if (candidate) {
            return (
              <button
                key={actionId}
                type="button"
                onClick={() => decide(actionId)}
                className={`flex flex-col items-start gap-0.5 w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                  i === 0
                    ? 'border-sky-500 bg-sky-100 dark:border-sky-500 dark:bg-sky-900/30'
                    : 'border-sky-300 hover:bg-sky-100 dark:border-sky-700 dark:hover:bg-sky-900/20'
                }`}
              >
                <span className="flex items-center gap-1.5 w-full">
                  <span
                    className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                      candidate.placement === 'local' ? 'bg-emerald-400' : 'bg-amber-400'
                    }`}
                  />
                  <span className="font-semibold text-sky-900 dark:text-sky-50">{candidate.key}</span>
                </span>
                <span className="text-xs text-sky-700 dark:text-sky-400 pl-3">
                  {formatBuilderDetail(candidate)}
                </span>
                {candidate.summary && (
                  <span className="text-xs text-sky-600 dark:text-sky-500 pl-3 line-clamp-1">
                    {candidate.summary}
                  </span>
                )}
              </button>
            );
          }

          return (
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
          );
        })}
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
        <div className="h-1 w-full overflow-hidden rounded-full bg-sky-200 dark:bg-sky-400/15">
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
