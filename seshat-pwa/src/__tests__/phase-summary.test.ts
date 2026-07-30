/**
 * ADR-0123 T4 (FRE-937) — pure derivation for the collapsed per-turn summary.
 *
 * Covers: buildTurnSummary's duration math and tool dedupe, groupByParent's
 * generic grouping (shared with PhaseIndicator's live view so the two can
 * never drift), and isTurnCollapsed's message-derived gate.
 */

import { describe, it, expect } from 'vitest';

import { buildTurnSummary, groupByParent, isTurnCollapsed } from '@/lib/phase-summary';
import type { ChatMessage, PhaseNode, ToolCall } from '@/lib/types';

function phaseNode(overrides: Partial<PhaseNode>): PhaseNode {
  return {
    phaseId: 'p1',
    phase: 'planning',
    detail: null,
    startedAt: '2026-07-30T10:00:00.000Z',
    state: 'completed',
    parentId: null,
    endedAt: new Date('2026-07-30T10:00:05.000Z').getTime(),
    ...overrides,
  };
}

describe('buildTurnSummary', () => {
  it('computes durationMs from server startedAt to client endedAt', () => {
    const summary = buildTurnSummary(
      [phaseNode({ startedAt: '2026-07-30T10:00:00.000Z', endedAt: new Date('2026-07-30T10:00:05.000Z').getTime() })],
      [],
      'completed',
    );
    expect(summary.phases[0].durationMs).toBe(5000);
  });

  it('maps each phase node state directly into the summary entry', () => {
    const summary = buildTurnSummary(
      [phaseNode({ phaseId: 'a', state: 'completed' }), phaseNode({ phaseId: 'b', state: 'error' })],
      [],
      'error',
    );
    const byId = Object.fromEntries(summary.phases.map((p) => [p.phaseId, p]));
    expect(byId.a.state).toBe('completed');
    expect(byId.b.state).toBe('error');
  });

  it('defensively resolves a stray still-running node to terminalState (belt-and-braces)', () => {
    const summary = buildTurnSummary(
      [phaseNode({ state: 'running', endedAt: null })],
      [],
      'cancelled',
      new Date('2026-07-30T10:00:09.000Z').getTime(),
    );
    expect(summary.phases[0].state).toBe('cancelled');
    expect(summary.phases[0].durationMs).toBe(9000);
  });

  it('dedupes tool names, preserving first-seen order', () => {
    const tools: ToolCall[] = [
      { name: 'perplexity_query', status: 'completed', result: 'a' },
      { name: 'run_python', status: 'completed', result: 'b' },
      { name: 'perplexity_query', status: 'completed', result: 'c' },
    ];
    const summary = buildTurnSummary([], tools, 'completed');
    expect(summary.tools).toEqual(['perplexity_query', 'run_python']);
  });

  it('sets terminalState on the summary', () => {
    const summary = buildTurnSummary([], [], 'error');
    expect(summary.terminalState).toBe('error');
  });

  it('preserves detail and parentId verbatim', () => {
    const summary = buildTurnSummary(
      [phaseNode({ phase: 'sub_agent', detail: 'pricing history', parentId: 'e1' })],
      [],
      'completed',
    );
    expect(summary.phases[0].detail).toBe('pricing history');
    expect(summary.phases[0].parentId).toBe('e1');
  });
});

describe('groupByParent', () => {
  it('splits top-level nodes from children keyed by parentId', () => {
    const items = [
      { phaseId: 'e1', parentId: null },
      { phaseId: 'c1', parentId: 'e1' },
      { phaseId: 'c2', parentId: 'e1' },
    ];
    const { topLevel, childrenByParent } = groupByParent(items);
    expect(topLevel.map((i) => i.phaseId)).toEqual(['e1']);
    expect(childrenByParent.get('e1')?.map((i) => i.phaseId)).toEqual(['c1', 'c2']);
  });

  it('treats a child whose parent is not present in the list as top-level (orphan)', () => {
    const items = [{ phaseId: 'c1', parentId: 'missing-parent' }];
    const { topLevel, childrenByParent } = groupByParent(items);
    expect(topLevel.map((i) => i.phaseId)).toEqual(['c1']);
    expect(childrenByParent.size).toBe(0);
  });

  it('returns an empty grouping for an empty list', () => {
    const { topLevel, childrenByParent } = groupByParent([]);
    expect(topLevel).toEqual([]);
    expect(childrenByParent.size).toBe(0);
  });
});

describe('isTurnCollapsed', () => {
  function chatMessage(overrides: Partial<ChatMessage>): ChatMessage {
    return {
      id: 'm1',
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      ...overrides,
    };
  }

  it('is false for an empty message list', () => {
    expect(isTurnCollapsed([])).toBe(false);
  });

  it('is false when the last message is from the user', () => {
    expect(isTurnCollapsed([chatMessage({ role: 'user' })])).toBe(false);
  });

  it('is false when the last message is assistant with no phaseSummary', () => {
    expect(isTurnCollapsed([chatMessage({ role: 'assistant' })])).toBe(false);
  });

  it('is true when the last message is assistant with a phaseSummary', () => {
    expect(
      isTurnCollapsed([
        chatMessage({ role: 'assistant', phaseSummary: { phases: [], tools: [], terminalState: 'completed' } }),
      ]),
    ).toBe(true);
  });
});
