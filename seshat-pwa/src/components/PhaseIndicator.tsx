'use client';

import { useEffect, useState, type ReactNode } from 'react';

import { elapsedMs, formatElapsed, ESCALATION_THRESHOLD_MS } from '@/lib/phase-elapsed';
import type { PhaseNode } from '@/lib/types';

interface PhaseIndicatorProps {
  phases: PhaseNode[];
}

/** User-facing copy per phase (ADR-0123 §1 table). */
const PHASE_LABELS: Record<PhaseNode['phase'], string> = {
  planning: 'Thinking',
  synthesis: 'Writing the response',
  artifact_build: 'Building the artifact',
  expansion: 'Working on multiple tasks',
  sub_agent: 'Sub-agent',
  waiting_for_choice: 'Waiting for your choice',
};

/**
 * A static, honest statement — never an estimate, never a bar or percentage
 * (ADR §3/§4: the turn's shape isn't known in advance, so any denominator
 * would be invented).
 */
const ESCALATION_TEXT = 'Large artifacts can take several minutes.';

function labelFor(node: PhaseNode): string {
  // A sub-agent's task name is more meaningful than the generic label.
  if (node.phase === 'sub_agent' && node.detail) return node.detail;
  return PHASE_LABELS[node.phase];
}

function PhaseRow({ node, now, children }: { node: PhaseNode; now: number; children?: ReactNode }) {
  // Running: the counter ticks live. Resolved: frozen at the client-observed
  // resolution moment (PHASE_END carries no server end timestamp) so a
  // completed phase's duration doesn't keep growing after its checkmark.
  const displayNow = node.state === 'running' ? now : (node.endedAt ?? now);
  const elapsed = elapsedMs(node.startedAt, displayNow);
  const showCandour = node.state === 'running' && elapsed > ESCALATION_THRESHOLD_MS;

  return (
    <div data-testid={`phase-${node.phaseId}`} data-state={node.state} className="flex flex-col gap-1">
      <div className="flex items-center gap-2 text-xs text-slate-400">
        {node.state === 'running' && (
          <svg
            className="animate-spin h-3.5 w-3.5 text-amber-400 flex-shrink-0"
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
            className="h-3.5 w-3.5 text-emerald-400 flex-shrink-0"
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
        {node.state === 'cancelled' && <span className="text-slate-500 flex-shrink-0">■</span>}
        {node.state === 'error' && <span className="text-red-400 flex-shrink-0">✕</span>}

        <span
          className={
            node.state === 'running'
              ? 'font-mono text-amber-400'
              : node.state === 'completed'
                ? 'font-mono text-emerald-400'
                : node.state === 'error'
                  ? 'font-mono text-red-400'
                  : 'font-mono text-slate-500'
          }
        >
          {labelFor(node)}
        </span>
        <span>
          —{' '}
          {node.state === 'cancelled' ? 'cancelled' : node.state === 'error' ? 'failed' : formatElapsed(elapsed)}
        </span>
      </div>
      {showCandour && <div className="pl-5 text-[11px] text-slate-500">{ESCALATION_TEXT}</div>}
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

  // Group children by parent — falling back to top-level when the parent's
  // own PHASE_START never arrived (best-effort emission can drop it): an
  // orphan child must still render rather than silently disappear.
  const knownIds = new Set(phases.map((p) => p.phaseId));
  const childrenByParent = new Map<string, PhaseNode[]>();
  const topLevel: PhaseNode[] = [];
  for (const p of phases) {
    if (p.parentId && knownIds.has(p.parentId)) {
      const siblings = childrenByParent.get(p.parentId) ?? [];
      siblings.push(p);
      childrenByParent.set(p.parentId, siblings);
    } else {
      topLevel.push(p);
    }
  }

  return (
    <div className="px-4 py-2 flex flex-col gap-2">
      {topLevel.map((node) => {
        const kids = childrenByParent.get(node.phaseId) ?? [];
        return (
          <PhaseRow key={node.phaseId} node={node} now={now}>
            {kids.length > 0 && (
              <div className="pl-5 flex flex-col gap-1 border-l border-slate-800">
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
