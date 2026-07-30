/**
 * User-facing copy for phases — shared between the live surface (PhaseIndicator,
 * ADR-0123 T3/FRE-936) and the collapsed per-turn summary (TurnSummaryPanel,
 * ADR-0123 T4/FRE-937), so the two views can never drift into different labels
 * for the same phase (ADR consequence: "a stale phase name is a small lie").
 */

import type { PhaseName } from './types';

/** User-facing copy per phase (ADR-0123 §1 table). */
export const PHASE_LABELS: Record<PhaseName, string> = {
  planning: 'Thinking',
  synthesis: 'Writing the response',
  artifact_build: 'Building the artifact',
  expansion: 'Working on multiple tasks',
  sub_agent: 'Sub-agent',
  waiting_for_choice: 'Waiting for your choice',
};

/** A sub-agent's task name is more meaningful than the generic label. */
export function labelFor(node: { phase: PhaseName; detail: string | null }): string {
  if (node.phase === 'sub_agent' && node.detail) return node.detail;
  // FRE-937 (master PR #758 bounce): a multi-round tool loop re-enters
  // `synthesis` once per pass — without a distinguisher every pass renders
  // the identical generic label, which reads as a stutter rather than
  // progress. The backend threads a round detail (executor.py's
  // step_llm_call); append it when present so repeated passes are legible.
  if (node.phase === 'synthesis' && node.detail) return `${PHASE_LABELS.synthesis} — ${node.detail}`;
  return PHASE_LABELS[node.phase];
}
