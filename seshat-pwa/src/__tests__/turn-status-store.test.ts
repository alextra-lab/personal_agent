/**
 * FRE-1538: the per-session status-bar store and the ctx-carry merge rule.
 *
 * The invariant under test is absent ≠ zero (FRE-928 / FRE-935): a reading that never
 * arrived is never stored and never shown as 0, and no ceiling is invented.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest';

import {
  COLD_TURN_STATUS,
  clearStoredTools,
  hasCtxReading,
  loadTurnStatus,
  mergeTurnStatus,
  persistTurnStatus,
} from '@/lib/turn-status-store';
import type { TurnStatus } from '@/lib/types';

const KEY = 'seshat-turn-status-s1';

function status(over: Partial<TurnStatus>): TurnStatus {
  return { ...COLD_TURN_STATUS, ...over };
}

beforeEach(() => {
  localStorage.clear();
});

describe('hasCtxReading', () => {
  it('is true only for a resolved positive ceiling with a numeric usage', () => {
    expect(hasCtxReading(status({ context_max: 128000, session_context_tokens: 0 }))).toBe(true);
    expect(hasCtxReading(status({ context_max: null, session_context_tokens: 500 }))).toBe(false);
    expect(hasCtxReading(status({ context_max: 0, session_context_tokens: 500 }))).toBe(false);
    expect(hasCtxReading(status({ context_max: Number.NaN, session_context_tokens: 500 }))).toBe(false);
  });
});

describe('mergeTurnStatus', () => {
  const prev = status({
    context_tokens: 11000,
    session_context_tokens: 12000,
    context_max: 128000,
    tool_iteration: 3,
    tool_iteration_max: 6,
  });

  it('carries the previous ctx reading over a next status that has no ceiling', () => {
    const next = status({ context_max: null, session_context_tokens: 0, tool_iteration: 1, tool_iteration_max: 6 });
    const merged = mergeTurnStatus(prev, next);
    expect(merged.session_context_tokens).toBe(12000);
    expect(merged.context_tokens).toBe(11000);
    expect(merged.context_max).toBe(128000);
    // Tools always come from the next status.
    expect(merged.tool_iteration).toBe(1);
  });

  it('lets a new ctx reading replace the old one (a compaction reading is a new reading)', () => {
    const next = status({ context_max: 128000, session_context_tokens: 4000, context_tokens: 4000 });
    expect(mergeTurnStatus(prev, next).session_context_tokens).toBe(4000);
  });

  it('keeps a next status with no ceiling unchanged when there is nothing to carry', () => {
    const next = status({ context_max: null });
    expect(mergeTurnStatus(null, next)).toEqual(next);
    expect(mergeTurnStatus(COLD_TURN_STATUS, next).context_max).toBeNull();
  });
});

describe('persistTurnStatus / loadTurnStatus', () => {
  it('round-trips a ctx reading and a tools reading', () => {
    persistTurnStatus(
      's1',
      status({
        context_tokens: 11000,
        session_context_tokens: 12000,
        context_max: 128000,
        tool_iteration: 3,
        tool_iteration_max: 6,
      }),
    );
    const loaded = loadTurnStatus('s1');
    expect(loaded.session_context_tokens).toBe(12000);
    expect(loaded.context_tokens).toBe(11000);
    expect(loaded.context_max).toBe(128000);
    expect(loaded.tool_iteration).toBe(3);
    expect(loaded.tool_iteration_max).toBe(6);
  });

  it('mirrors the displayed status: a lane with no reading is removed from the record, never stored as null', () => {
    persistTurnStatus('s1', status({ session_context_tokens: 12000, context_max: 128000, tool_iteration: 3, tool_iteration_max: 6 }));
    // Finite → null tools inside one turn: the display shows no reading, so the record must too.
    // (ctx is carried by mergeTurnStatus before persist, so it is present here.)
    persistTurnStatus('s1', status({ session_context_tokens: 12000, context_max: 128000, tool_iteration: null, tool_iteration_max: null }));
    const loaded = loadTurnStatus('s1');
    expect(loaded.tool_iteration).toBeNull();
    expect(loaded.tool_iteration_max).toBeNull();
    expect(loaded.session_context_tokens).toBe(12000);
    expect(JSON.stringify(JSON.parse(localStorage.getItem(KEY) as string))).not.toContain('null');
  });

  it('writes nothing for a status that holds no reading at all', () => {
    persistTurnStatus('s1', COLD_TURN_STATUS);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it('stores a real zero tool count (0 is a reading) but not a missing one', () => {
    persistTurnStatus('s1', status({ tool_iteration: 0, tool_iteration_max: 6 }));
    const loaded = loadTurnStatus('s1');
    expect(loaded.tool_iteration).toBe(0);
    expect(loaded.tool_iteration_max).toBe(6);
  });

  it('keeps sessions apart', () => {
    persistTurnStatus('s1', status({ session_context_tokens: 12000, context_max: 128000 }));
    expect(loadTurnStatus('s2')).toEqual(COLD_TURN_STATUS);
  });

  it('returns the cold status for a session with no record', () => {
    expect(loadTurnStatus('nobody')).toEqual(COLD_TURN_STATUS);
  });

  it('ignores corrupt or wrong-typed stored data field by field', () => {
    localStorage.setItem(KEY, '{not json');
    expect(loadTurnStatus('s1')).toEqual(COLD_TURN_STATUS);

    localStorage.setItem(
      KEY,
      JSON.stringify({ ctx: { session_context_tokens: '12000', context_tokens: 1, context_max: 128000 }, tools: { tool_iteration: 2, tool_iteration_max: 6 } }),
    );
    const loaded = loadTurnStatus('s1');
    expect(loaded.context_max).toBeNull();
    expect(loaded.tool_iteration).toBe(2);
  });

  it('drops a half-valid ctx group whole: a valid ceiling never pairs with the cold numerator (no 0/max)', () => {
    localStorage.setItem(KEY, JSON.stringify({ ctx: { context_max: 128000, context_tokens: 1 } }));
    const loaded = loadTurnStatus('s1');
    expect(loaded.context_max).toBeNull();
    expect(loaded.session_context_tokens).toBe(0);
    expect(hasCtxReading(loaded)).toBe(false);

    localStorage.setItem(KEY, JSON.stringify({ ctx: { session_context_tokens: 5, context_tokens: 5, context_max: 0 } }));
    expect(loadTurnStatus('s1').context_max).toBeNull();

    localStorage.setItem(KEY, JSON.stringify({ tools: { tool_iteration: 2 } }));
    expect(loadTurnStatus('s1').tool_iteration).toBeNull();
  });

  it('survives a storage that throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    expect(() => persistTurnStatus('s1', status({ session_context_tokens: 1, context_max: 10 }))).not.toThrow();
    spy.mockRestore();
  });
});

describe('clearStoredTools', () => {
  it('removes the tools reading and keeps the ctx reading', () => {
    persistTurnStatus('s1', status({ session_context_tokens: 12000, context_max: 128000, tool_iteration: 3, tool_iteration_max: 6 }));
    clearStoredTools('s1');
    const loaded = loadTurnStatus('s1');
    expect(loaded.tool_iteration).toBeNull();
    expect(loaded.tool_iteration_max).toBeNull();
    expect(loaded.session_context_tokens).toBe(12000);
  });

  it('is a no-op for a session with no record', () => {
    expect(() => clearStoredTools('nobody')).not.toThrow();
    expect(localStorage.getItem('seshat-turn-status-nobody')).toBeNull();
  });
});
