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

function renderStream(initialSessionId: string) {
  return renderHook(
    ({ activeSessionId }: { activeSessionId: string }) => useAgentStream(activeSessionId),
    { initialProps: { activeSessionId: initialSessionId } },
  );
}

beforeEach(() => {
  connections = {};
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
});
