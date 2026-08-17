'use client';

import { useEffect, useState, type ReactNode } from 'react';

import { elapsedMs, formatElapsed, ESCALATION_THRESHOLD_MS } from '@/lib/phase-elapsed';
import { labelFor } from '@/lib/phase-labels';
import { groupByParent } from '@/lib/phase-summary';
import type { PhaseNode } from '@/lib/types';

interface PhaseIndicatorProps {
  phases: PhaseNode[];
}

/**
 * A static, honest statement — never an estimate, never a bar or percentage
 * (ADR §3/§4: the turn's shape isn't known in advance, so any denominator
 * would be invented).
 */
const ESCALATION_TEXT = 'Large artifacts can take several minutes.';

function PhaseRow({ node, now, children }: { node: PhaseNode; now: number; children?: ReactNode }) {
  // Running: the counter ticks live. Resolved: frozen at the client-observed
  // resolution moment (PHASE_END carries no server end timestamp) so a
  // completed phase's duration doesn't keep growing after its checkmark.
  const displayNow = node.state === 'running' ? now : (node.endedAt ?? now);
  const elapsed = elapsedMs(node.startedAt, displayNow);
  const showCandour = node.state === 'running' && elapsed > ESCALATION_THRESHOLD_MS;

  return (
    <div data-testid={`phase-${node.phaseId}`} data-state={node.state} className="flex flex-col gap-1">
      <div className="flex items-center gap-2 text-xs text-ink-muted">
        {node.state === 'running' && (
          <svg
            className="animate-spin h-3.5 w-3.5 text-amber-700 dark:text-amber-400 flex-shrink-0"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
            />
          </svg>
        )}
        {node.state === 'completed' && (
          <svg
            className="h-3.5 w-3.5 text-emerald-700 dark:text-emerald-400 flex-shrink-0"
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
          >
            <path
              fillRule="evenodd"
              d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
              clipRule="evenodd"
            />
          </svg>
        )}
        {node.state === 'cancelled' && <span className="text-ink-muted flex-shrink-0">■</span>}
        {node.state === 'error' && <span className="text-red-700 dark:text-red-400 flex-shrink-0">✕</span>}

        <span
          className={
            node.state === 'running'
              ? 'font-mono text-amber-700 dark:text-amber-400'
              : node.state === 'completed'
                ? 'font-mono text-emerald-700 dark:text-emerald-400'
                : node.state === 'error'
                  ? 'font-mono text-red-700 dark:text-red-400'
                  : 'font-mono text-ink-muted'
          }
        >
          {labelFor(node)}
        </span>
        <span>
          —{' '}
          {node.state === 'cancelled' ? 'cancelled' : node.state === 'error' ? 'failed' : formatElapsed(elapsed)}
        </span>
      </div>
      {showCandour && <div className="pl-5 text-[11px] text-ink-muted">{ESCALATION_TEXT}</div>}
      {children}
    </div>
  );
}

/**
 * The live turn-progress phase surface (ADR-0123 T3, FRE-936).
 *
 * Generalizes ToolIndicator's running/completed visual treatment: a live
 * elapsed counter per phase, concurrent children indented under their
 * parent (AC-8 shape), and honest terminal-state icons (AC-9) instead of a
 * spinner that could persist forever. Renders nothing when idle.
 */
export function PhaseIndicator({ phases }: PhaseIndicatorProps) {
  const [now, setNow] = useState(() => Date.now());
  const anyRunning = phases.some((p) => p.state === 'running');

  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [anyRunning]);

  if (phases.length === 0) return null;

  // groupByParent falls back an orphan child (parent's own PHASE_START never
  // arrived — best-effort emission can drop it) to top-level rather than
  // silently dropping it.
  const { topLevel, childrenByParent } = groupByParent(phases);

  return (
    <div className="px-4 py-2 flex flex-col gap-2">
      {topLevel.map((node) => {
        const kids = childrenByParent.get(node.phaseId) ?? [];
        return (
          <PhaseRow key={node.phaseId} node={node} now={now}>
            {kids.length > 0 && (
              <div className="pl-5 flex flex-col gap-1 border-l border-line">
                {kids.map((child) => (
                  <PhaseRow key={child.phaseId} node={child} now={now} />
                ))}
              </div>
            )}
          </PhaseRow>
        );
      })}
    </div>
  );
}
