/**
 * Tests for useSSEStream hook event dispatch (FRE-400 WS2).
 *
 * Strategy: mock connectWebSocket to capture the onEvent callback, then
 * drive it directly with crafted AGUIEvent objects. This exercises all the
 * hook's state transitions without a real WebSocket.
 */

import { renderHook, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import type { Mock } from 'vitest';

// ── Mocks ────────────────────────────────────────────────────────────────────

let capturedOnEvent: ((event: unknown) => void) | null = null;

// These are declared at module level so tests can inspect them, but they
// must NOT be referenced inside the vi.mock() factory (which is hoisted
// before the let declarations are initialised — causes ReferenceError).
// Instead the factory creates fresh vi.fn() stubs on each mock call and
// assigns them back via the module-level vars in connectWebSocket.
let mockSend = vi.fn();
let mockClose = vi.fn();

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  BudgetDeniedError: class BudgetDeniedError extends Error {},
  connectWebSocket: vi.fn((
    _sessionId: string,
    onEvent: (e: unknown) => void,
  ) => {
    capturedOnEvent = onEvent;
    // Re-use the module-level stubs so tests can assert on them.
    mockSend = vi.fn();
    mockClose = vi.fn();
    return { send: mockSend, close: mockClose };
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

// ── Imports (after mocks) ─────────────────────────────────────────────────────

import { useSSEStream } from '@/hooks/useSSEStream';
import { connectWebSocket, getSessionMessages } from '@/lib/agui-client';
import { submitTurnRating } from '@/lib/submitTurnRating';

const mockConnect = connectWebSocket as Mock;
const mockGetSessionMessages = getSessionMessages as Mock;
const mockSubmitRating = submitTurnRating as Mock;

// ── Helpers ───────────────────────────────────────────────────────────────────

function pushEvent(event: object): void {
  if (!capturedOnEvent) throw new Error('No onEvent captured — call sendMessage first');
  act(() => { capturedOnEvent!(event); });
}

async function startTurn(hook: ReturnType<typeof renderHook<ReturnType<typeof useSSEStream>, unknown>>) {
  await act(async () => {
    await hook.result.current.sendMessage('hello', 'session-1', 'local');
  });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  capturedOnEvent = null;
  mockSend.mockClear();
  mockClose.mockClear();
  mockConnect.mockClear();
  mockSubmitRating.mockClear();
  mockSubmitRating.mockResolvedValue(true);
  mockGetSessionMessages.mockReset();
  mockGetSessionMessages.mockResolvedValue([]);
});

describe('useSSEStream — TEXT_DELTA accumulation', () => {
  it('accumulates TEXT_DELTA events into a single assistant message', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({ type: 'TEXT_DELTA', data: { text: 'Hello' }, seq: 1 });
    pushEvent({ type: 'TEXT_DELTA', data: { text: ' world' }, seq: 2 });

    const msgs = hook.result.current.messages;
    const assistant = msgs.find((m) => m.role === 'assistant');
    expect(assistant?.content).toBe('Hello world');
  });

  it('sets isStreaming=false on DONE', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    expect(hook.result.current.isStreaming).toBe(true);

    pushEvent({ type: 'DONE', seq: null });
    expect(hook.result.current.isStreaming).toBe(false);
  });
});

describe('useSSEStream — FRE-757 persist-on-send', () => {
  function pushTraceStatus(traceId: string): void {
    pushEvent({
      type: 'STATE_DELTA',
      data: {
        key: 'turn_status',
        value: {
          context_tokens: 1,
          context_max: 100,
          tool_iteration: 0,
          tool_iteration_max: 10,
          turn_cost_usd: 0,
          trace_id: traceId,
        },
      },
      seq: 1,
    });
  }

  it('persists the ok default (rating=2, default=true) once on DONE', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushTraceStatus('trace-done');
    pushEvent({ type: 'DONE', seq: null });

    expect(mockSubmitRating).toHaveBeenCalledTimes(1);
    expect(mockSubmitRating).toHaveBeenCalledWith('trace-done', 'session-1', 2, true);
  });

  it('does NOT persist a default when the completed turn has no trace_id', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent({ type: 'DONE', seq: null });
    expect(mockSubmitRating).not.toHaveBeenCalled();
  });

  it('preserves a stored rating across a replay-gap rehydrate', async () => {
    mockGetSessionMessages.mockResolvedValueOnce([
      { role: 'assistant', content: 'hi', trace_id: 't1', rating: 3 },
    ]);
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    await act(async () => {
      capturedOnEvent!({ type: 'REPLAY_GAP', seq: null });
      // Flush the getSessionMessages().then rehydrate.
      await Promise.resolve();
      await Promise.resolve();
    });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.rating).toBe(3);
    // Replay rehydrate must not auto-post a default for historical turns.
    expect(mockSubmitRating).not.toHaveBeenCalled();
  });
});

describe('useSSEStream — STATE_DELTA (turn_status)', () => {
  it('sets turnStatus from STATE_DELTA key=turn_status', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'STATE_DELTA',
      data: {
        key: 'turn_status',
        value: {
          context_tokens: 5000,
          context_max: 96000,
          tool_iteration: 2,
          tool_iteration_max: 10,
          turn_cost_usd: 0.005,
          trace_id: 'trace-abc',
        },
      },
      seq: 1,
    });

    const status = hook.result.current.turnStatus;
    expect(status?.context_tokens).toBe(5000);
    expect(status?.tool_iteration).toBe(2);
  });
});

describe('useSSEStream — CONSTRAINT_PAUSE / CONSTRAINT_RESOLVED', () => {
  it('sets pendingConstraint on CONSTRAINT_PAUSE', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'CONSTRAINT_PAUSE',
      request_id: 'req-1',
      session_id: 'session-1',
      data: {
        constraint: 'tool_iteration_limit',
        context: 'Too many tool calls.',
        options: ['continue_10', 'finish_now'],
        default_option: 'finish_now',
        expires_at: new Date(Date.now() + 30_000).toISOString(),
      },
      seq: 1,
    });

    expect(hook.result.current.pendingConstraint).not.toBeNull();
    expect(hook.result.current.pendingConstraint?.request_id).toBe('req-1');
    expect(hook.result.current.pendingConstraint?.constraint).toBe('tool_iteration_limit');
  });

  it('clears pendingConstraint and adds to resolvedConstraints on CONSTRAINT_RESOLVED', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'CONSTRAINT_PAUSE',
      request_id: 'req-1',
      session_id: 'session-1',
      data: {
        constraint: 'tool_iteration_limit',
        context: 'Too many tool calls.',
        options: ['continue_10', 'finish_now'],
        default_option: 'finish_now',
        expires_at: new Date(Date.now() + 30_000).toISOString(),
      },
      seq: 1,
    });

    pushEvent({
      type: 'CONSTRAINT_RESOLVED',
      request_id: 'req-1',
      session_id: 'session-1',
      data: {
        constraint: 'tool_iteration_limit',
        action_id: 'continue_10',
        resolution: 'user_choice',
      },
      seq: 2,
    });

    expect(hook.result.current.pendingConstraint).toBeNull();
    expect(hook.result.current.resolvedConstraints).toHaveLength(1);
    expect(hook.result.current.resolvedConstraints[0].action_id).toBe('continue_10');
  });
});

describe('useSSEStream — CANCELLED', () => {
  it('sets cancelled=true and isStreaming=false on CANCELLED', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    expect(hook.result.current.isStreaming).toBe(true);

    pushEvent({ type: 'CANCELLED', session_id: 'session-1', data: { reason: 'user_cancel' }, seq: 1 });

    expect(hook.result.current.cancelled).toBe(true);
    expect(hook.result.current.isStreaming).toBe(false);
  });
});

describe('useSSEStream — RUN_ERROR', () => {
  it('sets classifiedError and clears isStreaming on RUN_ERROR', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'RUN_ERROR',
      session_id: 'session-1',
      trace_id: 'trace-err',
      data: {
        category: 'model_server',
        reason: 'Server returned 500.',
        next_step: 'Check the server.',
        actions: ['retry', 'stop'],
        partial: false,
      },
      seq: 1,
    });

    const err = hook.result.current.classifiedError;
    expect(err).not.toBeNull();
    expect(err?.category).toBe('model_server');
    expect(hook.result.current.isStreaming).toBe(false);
  });

  it('dismissClassifiedError clears classifiedError', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'RUN_ERROR',
      session_id: 'session-1',
      trace_id: 'trace-err',
      data: {
        category: 'generic',
        reason: 'Something went wrong.',
        next_step: 'Try again.',
        actions: ['stop'],
        partial: false,
      },
      seq: 1,
    });

    act(() => { hook.result.current.dismissClassifiedError(); });
    expect(hook.result.current.classifiedError).toBeNull();
  });
});

describe('useSSEStream — outbound sends', () => {
  it('sendConstraintDecision sends CONSTRAINT_DECISION over WS', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    act(() => { hook.result.current.sendConstraintDecision('req-1', 'continue_10', false); });

    expect(mockSend).toHaveBeenCalledWith({
      type: 'CONSTRAINT_DECISION',
      request_id: 'req-1',
      decision: 'continue_10',
      remember: false,
    });
  });

  it('sendUserCancel sends USER_CANCEL over WS', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    act(() => { hook.result.current.sendUserCancel(); });

    expect(mockSend).toHaveBeenCalledWith({ type: 'USER_CANCEL' });
  });
});

describe('useSSEStream — seq dedup', () => {
  it('ignores events with seq <= last handled seq', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({ type: 'TEXT_DELTA', data: { text: 'first' }, seq: 5 });
    // Duplicate / out-of-order seq should be dropped.
    pushEvent({ type: 'TEXT_DELTA', data: { text: 'DUPLICATE' }, seq: 3 });
    pushEvent({ type: 'TEXT_DELTA', data: { text: 'DUPLICATE' }, seq: 5 });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.content).toBe('first');
  });
});
