/**
 * ADR-0123 T4 (FRE-937) — pure derivation for the collapsed per-turn summary.
 *
 * On turn completion/cancellation/error, useAgentStream.ts calls buildTurnSummary
 * to derive a compact, persistent record from the live phase/tool state it
 * already holds — no new server-side storage, per ADR §7.
 */

import { PHASE_LABELS } from './phase-labels';
import type { ChatMessage, PhaseName, PhaseNode, PhaseSummaryEntry, ToolCall, TurnSummary } from './types';

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

const SUMMARY_STATES: ReadonlySet<string> = new Set(['completed', 'cancelled', 'error']);

const isString = (v: unknown): v is string => typeof v === 'string';
const isNullableString = (v: unknown): v is string | null => v === null || typeof v === 'string';

/**
 * FRE-1543 — map the call history the server stored on an assistant message
 * (`turn_summary`, snake_case) to the `TurnSummary` the panel renders.
 *
 * Defensive: the value comes from the network. A malformed record, or an
 * individual malformed or unknown-phase row, is dropped — the panel then
 * renders less, never throws. A record left with no phases and no tools is
 * `undefined`, the same "no panel" outcome as a live trivial turn.
 *
 * @param raw - The message's `turn_summary` field, as received.
 * @returns The summary to render, or `undefined`.
 */
export function parseTurnSummary(raw: unknown): TurnSummary | undefined {
  if (raw === null || typeof raw !== 'object') return undefined;
  const { phases, tools, terminal_state: terminal } = raw as Record<string, unknown>;
  if (!isString(terminal) || !SUMMARY_STATES.has(terminal)) return undefined;

  const entries: PhaseSummaryEntry[] = [];
  for (const row of Array.isArray(phases) ? phases : []) {
    if (row === null || typeof row !== 'object') continue;
    const r = row as Record<string, unknown>;
    if (
      !isString(r.phase_id) ||
      !isString(r.phase) ||
      !(r.phase in PHASE_LABELS) ||
      !isNullableString(r.detail) ||
      typeof r.duration_ms !== 'number' ||
      !Number.isFinite(r.duration_ms) ||
      !isString(r.state) ||
      !SUMMARY_STATES.has(r.state) ||
      !isNullableString(r.parent_id)
    ) {
      continue;
    }
    entries.push({
      phaseId: r.phase_id,
      phase: r.phase as PhaseName,
      detail: r.detail,
      durationMs: r.duration_ms,
      state: r.state as PhaseSummaryEntry['state'],
      parentId: r.parent_id,
    });
  }
  const toolNames = Array.isArray(tools) ? tools.filter(isString) : [];
  if (entries.length === 0 && toolNames.length === 0) return undefined;
  return { phases: entries, tools: toolNames, terminalState: terminal as TurnSummary['terminalState'] };
}
