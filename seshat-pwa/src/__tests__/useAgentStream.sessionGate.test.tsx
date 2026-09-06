/**
 * FRE-1414: a stale session's WebSocket must not leak a live event into
 * whatever session is currently displayed after a switch that sends no
 * new message on the newly displayed session.
 *
 * Strategy: mock connectWebSocket to track one connection PER sessionId
 * (keyed by the sessionId argument), so a test can keep driving session A's
 * captured onEvent handler after the hook's displayed session has moved on
 * to B — exactly the race this ticket closes.
 */

import { renderHook, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import type { Mock } from 'vitest';

interface MockConnection {
  onEvent: (event: unknown) => void;
  send: Mock;
  close: Mock;
}

let connections: Record<string, MockConnection> = {};

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  BudgetDeniedError: class BudgetDeniedError extends Error {},
  connectWebSocket: vi.fn((sessionId: string, onEvent: (e: unknown) => void) => {
    const send = vi.fn();
    const close = vi.fn();
    connections[sessionId] = { onEvent, send, close };
    return { send, close };
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
import { getSessionMessages, sendChatMessage } from '@/lib/agui-client';

const mockGetSessionMessages = getSessionMessages as Mock;
const mockSendChatMessage = sendChatMessage as Mock;

function renderStream(initialSessionId: string) {
  return renderHook(
    ({ activeSessionId }: { activeSessionId: string }) => useAgentStream(activeSessionId),
    { initialProps: { activeSessionId: initialSessionId } },
  );
}

beforeEach(() => {
  connections = {};
  mockGetSessionMessages.mockReset();
  mockGetSessionMessages.mockResolvedValue([]);
  mockSendChatMessage.mockReset();
  mockSendChatMessage.mockResolvedValue(undefined);
});

describe('useAgentStream — WS session gate (FRE-1414)', () => {
  it('closes session A\'s connection when the displayed session switches to B without sending', async () => {
    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });
    expect(connections['session-a'].close).not.toHaveBeenCalled();

    hook.rerender({ activeSessionId: 'session-b' });

    expect(connections['session-a'].close).toHaveBeenCalledTimes(1);
  });

  it('does not close the connection when the displayed session is unchanged', async () => {
    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });

    hook.rerender({ activeSessionId: 'session-a' });

    expect(connections['session-a'].close).not.toHaveBeenCalled();
  });

  it("a straggling turn_status event from A's still-open (mocked) socket does not update B's displayed turnStatus", async () => {
    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });

    hook.rerender({ activeSessionId: 'session-b' });

    // Simulate the backend socket for A delivering one more event before the
    // close actually takes effect on the wire.
    act(() => {
      connections['session-a'].onEvent({
        type: 'STATE_DELTA',
        session_id: 'session-a',
        data: {
          key: 'turn_status',
          value: {
            context_tokens: 999,
            context_max: 1000,
            tool_iteration: 0,
            tool_iteration_max: 10,
            turn_cost_usd: 0,
            trace_id: 'trace-a',
          },
        },
        seq: 1,
      });
    });

    expect(hook.result.current.turnStatus?.context_tokens).not.toBe(999);
  });

  it("switching away from A clears A's live-turn UI (isStreaming, tools, phases)", async () => {
    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });
    expect(hook.result.current.isStreaming).toBe(true);

    act(() => {
      connections['session-a'].onEvent({
        type: 'TOOL_CALL_START',
        session_id: 'session-a',
        data: { tool_name: 'run_python' },
        seq: 1,
      });
    });
    expect(hook.result.current.activeTools).toHaveLength(1);

    hook.rerender({ activeSessionId: 'session-b' });

    expect(hook.result.current.isStreaming).toBe(false);
    expect(hook.result.current.activeTools).toHaveLength(0);
  });

  it("a DONE event for A (which omits session_id on the wire) does not resolve B's isStreaming", async () => {
    // FRE-1414: DONE/PONG/REPLAY_GAP/REPLAY_COMPLETE carry no session_id in
    // the real backend payload — the gate must key off the owning
    // connection, not event.session_id, or these types bypass it entirely.
    const hookA = renderStream('session-a');
    await act(async () => {
      await hookA.result.current.sendMessage('hi', 'session-a', 'local');
    });
    hookA.rerender({ activeSessionId: 'session-b' });

    await act(async () => {
      await hookA.result.current.sendMessage('hi', 'session-b', 'local');
    });
    expect(hookA.result.current.isStreaming).toBe(true);

    // A's stale DONE (no session_id field at all) arrives late.
    act(() => {
      connections['session-a'].onEvent({ type: 'DONE', seq: null });
    });

    // B is mid-turn — a stray DONE meant for A must not end it.
    expect(hookA.result.current.isStreaming).toBe(true);
  });

  it("switching away from A clears its transcript and terminal-turn cards (messages, cancelled, resolvedConstraints, budgetDenied, classifiedError)", async () => {
    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });

    act(() => {
      connections['session-a'].onEvent({ type: 'TEXT_DELTA', session_id: 'session-a', data: { text: 'partial reply' }, seq: 1 });
      connections['session-a'].onEvent({
        type: 'CONSTRAINT_RESOLVED',
        request_id: 'req-a',
        session_id: 'session-a',
        data: { constraint: 'artifact_builder', action_id: 'fast', resolution: 'user_choice' },
        seq: 2,
      });
      connections['session-a'].onEvent({ type: 'CANCELLED', session_id: 'session-a', data: {}, seq: 3 });
    });
    expect(hook.result.current.messages.length).toBeGreaterThan(0);
    expect(hook.result.current.cancelled).toBe(true);
    expect(hook.result.current.resolvedConstraints).toHaveLength(1);

    hook.rerender({ activeSessionId: 'session-b' });

    expect(hook.result.current.messages).toHaveLength(0);
    expect(hook.result.current.cancelled).toBe(false);
    expect(hook.result.current.resolvedConstraints).toHaveLength(0);
    expect(hook.result.current.budgetDenied).toBeNull();
    expect(hook.result.current.classifiedError).toBeNull();
  });

  it("a REPLAY_GAP rehydrate for A does not overwrite B's messages if it resolves after the switch", async () => {
    let resolveHistory!: (msgs: unknown[]) => void;
    mockGetSessionMessages.mockImplementationOnce(
      () => new Promise((resolve) => { resolveHistory = resolve; }),
    );

    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });

    // Triggers the in-flight getSessionMessages('session-a') fetch.
    act(() => {
      connections['session-a'].onEvent({ type: 'REPLAY_GAP', seq: null });
    });

    hook.rerender({ activeSessionId: 'session-b' });

    // A's rehydrate finally resolves after the switch.
    await act(async () => {
      resolveHistory([{ role: 'assistant', content: 'A history', trace_id: 't1' }]);
      await Promise.resolve();
    });

    expect(hook.result.current.messages).toHaveLength(0);
  });

  it("a delayed sendChatMessage rejection for A does not tear down B's newer connection or turn", async () => {
    let rejectSend!: (err: unknown) => void;
    mockSendChatMessage.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectSend = reject; }),
    );

    const hook = renderStream('session-a');
    let sendAPromise!: Promise<void>;
    act(() => {
      sendAPromise = hook.result.current.sendMessage('hi', 'session-a', 'local');
    });

    hook.rerender({ activeSessionId: 'session-b' });
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-b', 'local');
    });
    expect(hook.result.current.isStreaming).toBe(true);
    expect(connections['session-b'].close).not.toHaveBeenCalled();

    // A's original POST finally rejects, long after B's turn started.
    await act(async () => {
      rejectSend(new Error('network error'));
      await sendAPromise;
    });

    expect(hook.result.current.isStreaming).toBe(true);
    expect(connections['session-b'].close).not.toHaveBeenCalled();
    expect(hook.result.current.messages.some((m) => m.content.includes('Error contacting Seshat'))).toBe(false);
  });

  it("does not stamp B's completed turn with A's trace_id after an abandoned mid-turn switch", async () => {
    const hook = renderStream('session-a');
    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-a', 'local');
    });

    // A's turn_status resolves a trace_id, but DONE never arrives before the switch.
    act(() => {
      connections['session-a'].onEvent({
        type: 'STATE_DELTA',
        session_id: 'session-a',
        data: {
          key: 'turn_status',
          value: {
            context_tokens: 1,
            context_max: 100,
            tool_iteration: 0,
            tool_iteration_max: 10,
            turn_cost_usd: 0,
            trace_id: 'trace-a',
          },
        },
        seq: 1,
      });
    });

    hook.rerender({ activeSessionId: 'session-b' });

    await act(async () => {
      await hook.result.current.sendMessage('hi', 'session-b', 'local');
    });

    // B produces an assistant reply, then a DONE with no trace_id of its own
    // (no turn_status for B yet either) — must not fall back to A's stashed
    // trace_id when stamping it.
    act(() => {
      connections['session-b'].onEvent({ type: 'TEXT_DELTA', session_id: 'session-b', data: { text: 'B reply' }, seq: 1 });
      connections['session-b'].onEvent({ type: 'DONE', seq: null });
    });

    const assistant = hook.result.current.messages.find((m) => m.role === 'assistant');
    expect(assistant?.traceId).not.toBe('trace-a');
  });
});
