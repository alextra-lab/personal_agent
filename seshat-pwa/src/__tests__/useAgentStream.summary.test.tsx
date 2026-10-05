/**
 * ADR-0123 T4 (FRE-937) — collapsed per-turn summary attach on DONE/CANCELLED/
 * RUN_ERROR.
 *
 * Design note (codex plan review, 2026-07-30): `phases`/`activeTools` are NOT
 * cleared by these handlers — that would have broken 8 pre-existing tests in
 * useAgentStream.phases.test.tsx for no functional gain, since StreamingChat
 * gates the live footer's rendering on `isTurnCollapsed(messages)` instead
 * (see lib/phase-summary.ts). This file asserts the summary-attach behavior
 * only; useAgentStream.phases.test.tsx's terminal-state resolution tests are
 * untouched.
 */

import { renderHook, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';

let capturedOnEvent: ((event: unknown) => void) | null = null;

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  BudgetDeniedError: class BudgetDeniedError extends Error {},
  connectWebSocket: vi.fn((_sessionId: string, onEvent: (e: unknown) => void) => {
    capturedOnEvent = onEvent;
    return { send: vi.fn(), close: vi.fn() };
  }),
  sendChatMessage: vi.fn().mockResolvedValue(undefined),
  getSessionMessages: vi.fn().mockResolvedValue([]),
}));

vi.mock('@/lib/submitTurnRating', () => ({
  submitTurnRating: vi.fn().mockResolvedValue(true),
}));

vi.mock('@/lib/uuid', () => ({
  generateUUID: vi.fn(() => 'test-uuid'),
}));

import { useAgentStream } from '@/hooks/useAgentStream';
import { connectWebSocket } from '@/lib/agui-client';

const mockConnect = connectWebSocket as Mock;

function pushEvent(event: object): void {
  if (!capturedOnEvent) throw new Error('No onEvent captured — call sendMessage first');
  act(() => {
    capturedOnEvent!(event);
  });
}

async function startTurn(hook: ReturnType<typeof renderHook<ReturnType<typeof useAgentStream>, unknown>>): Promise<void> {
  await act(async () => {
    await hook.result.current.sendMessage('hello', 'session-1', 'local');
  });
}

function phaseStart(seq: number, opts: { phase_id: string; phase?: string; started_at: string }): object {
  return {
    type: 'PHASE_START',
    seq,
    session_id: 'session-1',
    data: { phase: opts.phase ?? 'planning', phase_id: opts.phase_id, started_at: opts.started_at, detail: null, parent_id: null },
  };
}

function phaseEnd(seq: number, opts: { phase_id: string; phase?: string; ok?: boolean }): object {
  return {
    type: 'PHASE_END',
    seq,
    session_id: 'session-1',
    data: { phase: opts.phase ?? 'planning', phase_id: opts.phase_id, parent_id: null, ok: opts.ok ?? true },
  };
}

function toolStart(tool_name: string, tool_call_id?: string): object {
  return { type: 'TOOL_CALL_START', data: { tool_name, ...(tool_call_id ? { tool_call_id } : {}) } };
}

function toolEnd(tool_name: string, result = 'ok', tool_call_id?: string): object {
  return { type: 'TOOL_CALL_END', data: { tool_name, result, ...(tool_call_id ? { tool_call_id } : {}) } };
}

beforeEach(() => {
  capturedOnEvent = null;
  mockConnect.mockClear();
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-07-30T10:00:00.000Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useAgentStream — collapsed turn summary (ADR-0123 T4, FRE-937)', () => {
  it('DONE attaches a completed summary with phase durations and deduped tools to the last assistant message', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent({ type: 'TEXT_DELTA', data: { text: 'hi' }, seq: 1 });
    pushEvent(toolStart('perplexity_query'));
    pushEvent(toolEnd('perplexity_query'));
    pushEvent(phaseStart(2, { phase_id: 'p1', started_at: '2026-07-30T10:00:00.000Z' }));

    vi.setSystemTime(new Date('2026-07-30T10:00:05.000Z'));
    pushEvent(phaseEnd(3, { phase_id: 'p1' }));

    pushEvent({ type: 'DONE', seq: null });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.phaseSummary?.terminalState).toBe('completed');
    expect(assistant?.phaseSummary?.phases).toEqual([
      { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 5000, state: 'completed', parentId: null },
    ]);
    expect(assistant?.phaseSummary?.tools).toEqual([{ name: 'perplexity_query', status: 'completed' }]);

    // Design change (codex review): live state is NOT cleared — it resolves
    // in place exactly as useAgentStream.phases.test.tsx already asserts.
    expect(hook.result.current.phases).toHaveLength(1);
    expect(hook.result.current.phases[0].state).toBe('completed');
  });

  it('FRE-1551: parallel calls of one tool are separate rows, and an end closes its own call by id', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent({ type: 'TEXT_DELTA', data: { text: 'hi' }, seq: 1 });
    pushEvent(toolStart('web_search', 'a'));
    pushEvent(toolStart('web_search', 'b'));
    pushEvent(toolEnd('web_search', 'failed', 'b'));
    pushEvent(toolEnd('web_search', '', 'a'));
    pushEvent(toolStart('fetch_url', 'c'));
    pushEvent(toolEnd('fetch_url', '', 'c'));
    pushEvent({ type: 'DONE', seq: null });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.phaseSummary?.tools).toEqual([
      { name: 'web_search', status: 'completed' },
      { name: 'web_search', status: 'failed' },
      { name: 'fetch_url', status: 'completed' },
    ]);
  });

  it('DONE with phase events but zero TEXT_DELTA (artifact-only turn) appends a placeholder assistant message, marked complete', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent(phaseStart(1, { phase_id: 'p1', phase: 'artifact_build', started_at: '2026-07-30T10:00:00.000Z' }));
    vi.setSystemTime(new Date('2026-07-30T10:00:20.000Z'));
    pushEvent(phaseEnd(2, { phase_id: 'p1', phase: 'artifact_build' }));
    pushEvent({ type: 'DONE', seq: null, trace_id: 'trace-artifact-only' });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant).toBeDefined();
    expect(assistant?.content).toBe('');
    expect(assistant?.complete).toBe(true);
    expect(assistant?.traceId).toBe('trace-artifact-only');
    expect(assistant?.phaseSummary?.terminalState).toBe('completed');
    expect(assistant?.phaseSummary?.phases[0]).toMatchObject({ phaseId: 'p1', state: 'completed', durationMs: 20_000 });
  });

  it('CANCELLED before any TEXT_DELTA appends a placeholder assistant message carrying the summary (AC-9a)', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-30T10:00:00.000Z' }));
    vi.setSystemTime(new Date('2026-07-30T10:00:12.000Z'));
    pushEvent({ type: 'CANCELLED', seq: null });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant).toBeDefined();
    expect(assistant?.content).toBe('');
    expect(assistant?.phaseSummary?.terminalState).toBe('cancelled');
    expect(assistant?.phaseSummary?.phases[0]).toMatchObject({ phaseId: 'p1', state: 'cancelled', durationMs: 12_000 });
  });

  it('FRE-1543: the DONE that follows CANCELLED keeps the cancelled summary (it used to relabel it Completed)', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent({ type: 'TEXT_DELTA', data: { text: 'partial' }, seq: 1 });
    pushEvent(phaseStart(2, { phase_id: 'p1', started_at: '2026-07-30T10:00:00.000Z' }));
    pushEvent({ type: 'CANCELLED', seq: null });
    pushEvent({ type: 'DONE', seq: null });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.phaseSummary?.terminalState).toBe('cancelled');
    expect(assistant?.complete).toBe(true);
  });

  it('FRE-1543: PHASE_END carries the server end stamp, so a replayed phase keeps its real duration', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-30T10:00:00.000Z' }));
    // The page sees the end an hour later (a reload replay); the phase took 3 s.
    vi.setSystemTime(new Date('2026-07-30T11:00:00.000Z'));
    pushEvent({
      type: 'PHASE_END',
      seq: 2,
      data: { phase: 'planning', phase_id: 'p1', parent_id: null, ok: true, ended_at: '2026-07-30T10:00:03.000Z' },
    });
    pushEvent({ type: 'DONE', seq: null });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.phaseSummary?.phases[0]).toMatchObject({ phaseId: 'p1', durationMs: 3_000 });
  });

  it('RUN_ERROR after a realistic PHASE_END(ok:false) then RUN_ERROR ordering attaches an error summary (AC-9b)', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent({ type: 'TEXT_DELTA', data: { text: 'partial' }, seq: 1 });
    pushEvent(phaseStart(2, { phase_id: 'p1', started_at: '2026-07-30T10:00:00.000Z' }));
    vi.setSystemTime(new Date('2026-07-30T10:00:08.000Z'));
    pushEvent(phaseEnd(3, { phase_id: 'p1', ok: false }));
    pushEvent({
      type: 'RUN_ERROR',
      seq: null,
      data: { category: 'tool_failure', reason: 'boom', next_step: '', actions: [], partial: true },
    });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.phaseSummary?.terminalState).toBe('error');
    expect(assistant?.phaseSummary?.phases[0]).toMatchObject({ phaseId: 'p1', state: 'error', durationMs: 8000 });
  });

  it('DONE with zero phases and zero tools attaches no summary and creates no placeholder', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);
    pushEvent({ type: 'DONE', seq: null });
    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant).toBeUndefined();
  });

  it('CANCELLED with zero phases and zero tools attaches no summary and creates no placeholder', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);
    pushEvent({ type: 'CANCELLED', seq: null });
    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant).toBeUndefined();
  });

  it('RUN_ERROR with zero phases and zero tools attaches no summary and creates no placeholder', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);
    pushEvent({
      type: 'RUN_ERROR',
      seq: null,
      data: { category: 'generic', reason: 'boom', next_step: '', actions: [], partial: false },
    });
    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant).toBeUndefined();
  });
});

describe('useAgentStream — live tool rows (FRE-1547)', () => {
  it('a TOOL_CALL_END completes only the first running row of that name', async () => {
    const hook = renderHook(() => useAgentStream());
    await startTurn(hook);

    pushEvent(toolStart('web_search'));
    pushEvent(toolStart('web_search'));
    pushEvent(toolEnd('web_search', ''));

    expect(hook.result.current.activeTools.map((t) => t.status)).toEqual(['completed', 'running']);

    pushEvent(toolEnd('web_search', 'failed'));

    expect(hook.result.current.activeTools).toEqual([
      { name: 'web_search', status: 'completed', result: '' },
      { name: 'web_search', status: 'completed', result: 'failed' },
    ]);
  });
});
