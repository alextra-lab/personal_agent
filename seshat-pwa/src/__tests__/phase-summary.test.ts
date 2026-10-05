/**
 * ADR-0123 T4 (FRE-937) — pure derivation for the collapsed per-turn summary.
 *
 * Covers: buildTurnSummary's duration math and tool dedupe, groupByParent's
 * generic grouping (shared with PhaseIndicator's live view so the two can
 * never drift), and isTurnCollapsed's message-derived gate.
 */

import { describe, it, expect } from 'vitest';

import { buildTurnSummary, groupByParent, isTurnCollapsed, parseTurnSummary } from '@/lib/phase-summary';
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

  it('FRE-1551: keeps one entry per call, in call order, with its status', () => {
    const tools: ToolCall[] = [
      { name: 'perplexity_query', status: 'completed', result: '' },
      { name: 'run_python', status: 'completed', result: 'failed' },
      { name: 'perplexity_query', status: 'running' },
    ];
    const summary = buildTurnSummary([], tools, 'completed');
    expect(summary.tools).toEqual([
      { name: 'perplexity_query', status: 'completed' },
      { name: 'run_python', status: 'failed' },
      { name: 'perplexity_query', status: 'unfinished' },
    ]);
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

describe('parseTurnSummary (FRE-1543)', () => {
  it('maps the stored snake_case record to the panel shape', () => {
    expect(
      parseTurnSummary({
        phases: [
          { phase_id: 'p1', phase: 'planning', detail: null, duration_ms: 2500, state: 'completed', parent_id: null },
          { phase_id: 'c1', phase: 'sub_agent', detail: 'w1', duration_ms: 10, state: 'error', parent_id: 'p1' },
        ],
        tools: [{ name: 'web_search', status: 'failed' }],
        terminal_state: 'cancelled',
      }),
    ).toEqual({
      phases: [
        { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 2500, state: 'completed', parentId: null },
        { phaseId: 'c1', phase: 'sub_agent', detail: 'w1', durationMs: 10, state: 'error', parentId: 'p1' },
      ],
      tools: [{ name: 'web_search', status: 'failed' }],
      terminalState: 'cancelled',
    });
  });

  it('drops malformed and unknown-phase rows, never throws', () => {
    const parsed = parseTurnSummary({
      phases: [
        null,
        { phase_id: 'x', phase: 'teleport', detail: null, duration_ms: 1, state: 'completed', parent_id: null },
        { phase_id: 'y', phase: 'planning', detail: null, duration_ms: 'slow', state: 'completed', parent_id: null },
        { phase_id: 'p1', phase: 'planning', detail: null, duration_ms: 5, state: 'completed', parent_id: null },
      ],
      tools: [{ name: 'ok', status: 'completed' }, 3, { name: 'x', status: 'exploded' }, { status: 'completed' }],
      terminal_state: 'completed',
    });
    expect(parsed?.phases.map((p) => p.phaseId)).toEqual(['p1']);
    expect(parsed?.tools).toEqual([{ name: 'ok', status: 'completed' }]);
  });

  it('FRE-1551: every stored call is a row, repeats included', () => {
    const parsed = parseTurnSummary({
      phases: [],
      tools: [
        { name: 'web_search', status: 'completed' },
        { name: 'web_search', status: 'failed' },
        { name: 'fetch_url', status: 'unfinished' },
      ],
      terminal_state: 'completed',
    });
    expect(parsed?.tools).toEqual([
      { name: 'web_search', status: 'completed' },
      { name: 'web_search', status: 'failed' },
      { name: 'fetch_url', status: 'unfinished' },
    ]);
  });

  it('FRE-1551: a history stored before per-call rows (plain names) still renders, with unknown status', () => {
    const parsed = parseTurnSummary({ phases: [], tools: ['web_search', 'fetch_url'], terminal_state: 'completed' });
    expect(parsed?.tools).toEqual([
      { name: 'web_search', status: 'unknown' },
      { name: 'fetch_url', status: 'unknown' },
    ]);
  });

  it('returns undefined for an absent, malformed or empty record', () => {
    expect(parseTurnSummary(undefined)).toBeUndefined();
    expect(parseTurnSummary('nope')).toBeUndefined();
    expect(parseTurnSummary({ phases: [], tools: [], terminal_state: 'completed' })).toBeUndefined();
    expect(parseTurnSummary({ phases: [], tools: [{ name: 't', status: 'completed' }], terminal_state: 'exploded' })).toBeUndefined();
  });
});
