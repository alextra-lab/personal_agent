/**
 * ADR-0123 T4 (FRE-937) — pure derivation for the collapsed per-turn summary.
 *
 * On turn completion/cancellation/error, useSSEStream.ts calls buildTurnSummary
 * to derive a compact, persistent record from the live phase/tool state it
 * already holds — no new server-side storage, per ADR §7.
 */

import type { ChatMessage, PhaseNode, PhaseSummaryEntry, ToolCall, TurnSummary } from './types';

/**
 * Derive a TurnSummary from a turn's resolved phase nodes and the tools that ran.
 *
 * `durationMs` is `(endedAt ?? now) - startedAt` — the same server-start /
 * client-observed-end pairing PhaseIndicator's live counter already uses
 * (PhaseNode.endedAt is explicitly client-observed; see types.ts).
 *
 * A node still `running` at call time (should not happen — callers resolve
 * every node to a terminal state first) is defensively treated as `terminalState`.
 */
export function buildTurnSummary(
  phases: readonly PhaseNode[],
  tools: readonly ToolCall[],
  terminalState: TurnSummary['terminalState'],
  now: number = Date.now(),
): TurnSummary {
  const summaryPhases: PhaseSummaryEntry[] = phases.map((p) => ({
    phaseId: p.phaseId,
    phase: p.phase,
    detail: p.detail,
    durationMs: (p.endedAt ?? now) - Date.parse(p.startedAt),
    state: p.state === 'running' ? terminalState : p.state,
    parentId: p.parentId,
  }));

  const seen = new Set<string>();
  const toolNames: string[] = [];
  for (const t of tools) {
    if (!seen.has(t.name)) {
      seen.add(t.name);
      toolNames.push(t.name);
    }
  }

  return { phases: summaryPhases, tools: toolNames, terminalState };
}

/**
 * Split a flat phase-like list into top-level entries and a parentId → children
 * map. Shared by PhaseIndicator (live) and TurnSummaryPanel (collapsed) so both
 * views group concurrent children identically.
 *
 * A child whose parentId isn't present in the list (parent's own PHASE_START
 * dropped by best-effort emission) falls back to top-level rather than
 * disappearing.
 */
export function groupByParent<T extends { phaseId: string; parentId: string | null }>(
  items: readonly T[],
): { topLevel: T[]; childrenByParent: Map<string, T[]> } {
  const knownIds = new Set(items.map((i) => i.phaseId));
  const childrenByParent = new Map<string, T[]>();
  const topLevel: T[] = [];
  for (const item of items) {
    if (item.parentId && knownIds.has(item.parentId)) {
      const siblings = childrenByParent.get(item.parentId) ?? [];
      siblings.push(item);
      childrenByParent.set(item.parentId, siblings);
    } else {
      topLevel.push(item);
    }
  }
  return { topLevel, childrenByParent };
}

/**
 * True once the current turn has collapsed into its transcript summary — the
 * last message is an assistant message carrying a `phaseSummary`. Drives
 * whether StreamingChat still shows the live PhaseIndicator/ToolIndicator
 * footer (ADR-0123 §7).
 */
export function isTurnCollapsed(messages: readonly ChatMessage[]): boolean {
  const last = messages[messages.length - 1];
  return last?.role === 'assistant' && last.phaseSummary != null;
}
