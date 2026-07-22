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

describe('useSSEStream — STATE_DELTA (session_selection, ADR-0121 §4)', () => {
  it('sets serverSelection from a well-formed session_selection STATE_DELTA', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'STATE_DELTA',
      data: {
        key: 'session_selection',
        value: { role: 'primary', deployment_key: 'claude_sonnet' },
      },
      seq: 1,
    });

    expect(hook.result.current.serverSelection).toEqual({
      role: 'primary',
      deploymentKey: 'claude_sonnet',
    });
  });

  it('ignores a malformed session_selection payload', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent({
      type: 'STATE_DELTA',
      data: { key: 'session_selection', value: { role: 'primary' } }, // missing deployment_key
      seq: 1,
    });

    expect(hook.result.current.serverSelection).toBeNull();
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

describe('useSSEStream — concurrent constraint pauses queue (FRE-928)', () => {
  function pushPause(requestId: string, seq: number): void {
    pushEvent({
      type: 'CONSTRAINT_PAUSE',
      request_id: requestId,
      session_id: 'session-1',
      data: {
        constraint: 'artifact_builder',
        context: 'Choose the model to build this artifact.',
        options: ['fast', 'thorough'],
        default_option: 'fast',
        expires_at: new Date(Date.now() + 180_000).toISOString(),
      },
      seq,
    });
  }

  it('a second pause does not overwrite the first — it queues behind it', async () => {
    // Two turns can run concurrently on one session (/chat/stream is fire-and-forget).
    // Overwriting left the first card unanswerable while its server-side waiter rode
    // a full timeout, silently applying a default the user never chose.
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushPause('req-a', 1);
    pushPause('req-b', 2);

    expect(hook.result.current.pendingConstraint?.request_id).toBe('req-a');
  });

  it('answering the first card advances to the second', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushPause('req-a', 1);
    pushPause('req-b', 2);

    act(() => {
      hook.result.current.sendConstraintDecision('req-a', 'thorough', false);
    });

    expect(hook.result.current.pendingConstraint?.request_id).toBe('req-b');
  });

  it('a replayed duplicate pause does not queue twice', async () => {
    // Reconnect replays persisted events, so the same pause can arrive again.
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushPause('req-a', 1);
    pushPause('req-a', 1);

    act(() => {
      hook.result.current.sendConstraintDecision('req-a', 'fast', false);
    });

    expect(hook.result.current.pendingConstraint).toBeNull();
  });

  it('sending a new message does not discard a still-pending card', async () => {
    // Self-review finding: the turn reset used to wipe the queue. A card maps to a
    // live server waiter that now survives a disconnect, and nothing blocks sending
    // while one is open — so clearing hid an answerable card whose waiter then timed
    // out into a default, i.e. exactly the defect this ticket fixes.
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushPause('req-a', 1);
    expect(hook.result.current.pendingConstraint?.request_id).toBe('req-a');

    await startTurn(hook);

    expect(hook.result.current.pendingConstraint?.request_id).toBe('req-a');
  });

  it('resolving a queued card removes it from anywhere in the queue', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushPause('req-a', 1);
    pushPause('req-b', 2);

    // The SECOND card times out server-side and resolves while the first is showing.
    pushEvent({
      type: 'CONSTRAINT_RESOLVED',
      request_id: 'req-b',
      session_id: 'session-1',
      data: {
        constraint: 'artifact_builder',
        action_id: 'fast',
        resolution: 'timeout_default',
      },
      seq: 3,
    });

    expect(hook.result.current.pendingConstraint?.request_id).toBe('req-a');

    act(() => {
      hook.result.current.sendConstraintDecision('req-a', 'thorough', false);
    });

    expect(hook.result.current.pendingConstraint).toBeNull();
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
