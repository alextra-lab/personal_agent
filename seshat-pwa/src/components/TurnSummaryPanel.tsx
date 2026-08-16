'use client';

import type { ReactNode } from 'react';

import { formatElapsed } from '@/lib/phase-elapsed';
import { labelFor } from '@/lib/phase-labels';
import { groupByParent } from '@/lib/phase-summary';
import type { PhaseSummaryEntry, TurnSummary } from '@/lib/types';

interface TurnSummaryPanelProps {
  summary: TurnSummary | undefined;
}

const TERMINAL_HEADER: Record<TurnSummary['terminalState'], string> = {
  completed: 'Completed',
  cancelled: 'Cancelled',
  error: 'Failed',
};

function headerLine(summary: TurnSummary): string {
  const phaseCount = summary.phases.length;
  const totalMs = summary.phases
    .filter((p) => p.parentId === null)
    .reduce((sum, p) => sum + p.durationMs, 0);
  const parts = [
    TERMINAL_HEADER[summary.terminalState],
    `${phaseCount} ${phaseCount === 1 ? 'phase' : 'phases'}`,
    formatElapsed(totalMs),
  ];
  if (summary.tools.length > 0) {
    parts.push(`${summary.tools.length} ${summary.tools.length === 1 ? 'tool' : 'tools'}`);
  }
  return parts.join(' · ');
}

function PhaseSummaryRow({ entry, children }: { entry: PhaseSummaryEntry; children?: ReactNode }) {
  return (
    <div
      data-testid={`turn-summary-phase-${entry.phaseId}`}
      data-state={entry.state}
      className="flex flex-col gap-1"
    >
      <div className="flex items-center gap-2 text-xs text-ink-muted">
        {entry.state === 'completed' && <span className="text-emerald-400 flex-shrink-0">&#10003;</span>}
        {entry.state === 'cancelled' && <span className="text-ink-muted flex-shrink-0">&#9632;</span>}
        {entry.state === 'error' && <span className="text-red-400 flex-shrink-0">&#10005;</span>}
        <span
          className={
            entry.state === 'completed'
              ? 'font-mono text-emerald-400'
              : entry.state === 'error'
                ? 'font-mono text-red-400'
                : 'font-mono text-ink-muted'
          }
        >
          {labelFor(entry)}
        </span>
        <span>—</span>
        <span>{formatElapsed(entry.durationMs)}</span>
      </div>
      {children}
    </div>
  );
}

/**
 * The collapsed, persistent per-turn summary in the transcript (ADR-0123 T4,
 * FRE-937/AC-7). Renders nothing when there is no summary to show — a plain
 * short Q&A turn with zero phases and zero tools never grows this control.
 *
 * Collapsed by default via native <details>/<summary> — no extra state,
 * keyboard-accessible for free.
 */
export function TurnSummaryPanel({ summary }: TurnSummaryPanelProps) {
  if (!summary || (summary.phases.length === 0 && summary.tools.length === 0)) return null;

  const { topLevel, childrenByParent } = groupByParent(summary.phases);

  return (
    <details data-testid="turn-summary" className="mt-2 text-xs text-ink-muted">
      <summary
        data-testid="turn-summary-header"
        className="cursor-pointer select-none text-ink-muted hover:text-ink"
      >
        {headerLine(summary)}
      </summary>
      <div className="mt-2 pl-2 flex flex-col gap-2 border-l border-line">
        {topLevel.map((entry) => {
          const kids = childrenByParent.get(entry.phaseId) ?? [];
          return (
            <PhaseSummaryRow key={entry.phaseId} entry={entry}>
              {kids.length > 0 && (
                <div className="pl-5 flex flex-col gap-1 border-l border-line">
                  {kids.map((child) => (
                    <PhaseSummaryRow key={child.phaseId} entry={child} />
                  ))}
                </div>
              )}
            </PhaseSummaryRow>
          );
        })}
        {summary.tools.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {summary.tools.map((name) => (
              <span
                key={name}
                className="inline-flex items-center text-xs px-2 py-0.5 rounded-full font-mono bg-surface text-ink-muted border border-line"
              >
                {name}
              </span>
            ))}
          </div>
        )}
      </div>
    </details>
  );
}
