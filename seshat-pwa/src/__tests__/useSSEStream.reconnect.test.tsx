/**
 * FRE-236 — isReconnecting state and draft persistence in useSSEStream.
 *
 * Tests the new lifecycle behavior:
 * - isReconnecting is false initially.
 * - isReconnecting becomes true when onWsDisconnected fires while streaming.
 * - isReconnecting stays false when onWsDisconnected fires after streaming ended.
 * - isReconnecting is cleared by onWsConnected, DONE event, or sendMessage.
 * - Draft is written to localStorage on visibilitychange→hidden while streaming.
 * - Draft is not written when not streaming.
 * - Draft is cleared from localStorage on DONE event.
 * - Draft is cleared from localStorage on sendMessage.
 */

import { renderHook, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import type { Mock } from 'vitest';

// ── Module-level captured values ───────────────────────────────────────────

let capturedOnEvent: ((event: unknown) => void) | null = null;
let capturedOpts: {
  onWsConnected?: () => void;
  onWsDisconnected?: () => void;
} | null = null;
let mockSend = vi.fn();
let mockClose = vi.fn();

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  BudgetDeniedError: class BudgetDeniedError extends Error {},
  connectWebSocket: vi.fn((
    _sessionId: string,
    onEvent: (e: unknown) => void,
    _onError: unknown,
    opts: { onWsConnected?: () => void; onWsDisconnected?: () => void } | undefined,
  ) => {
    capturedOnEvent = onEvent;
    capturedOpts = opts ?? null;
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
import { connectWebSocket } from '@/lib/agui-client';

const mockConnect = connectWebSocket as Mock;

// ── Helpers ────────────────────────────────────────────────────────────────

function pushEvent(event: object): void {
  if (!capturedOnEvent) throw new Error('No onEvent captured — call sendMessage first');
  act(() => {
    capturedOnEvent!(event);
  });
}

async function startTurn(
  hook: ReturnType<typeof renderHook<ReturnType<typeof useSSEStream>, unknown>>,
): Promise<void> {
  await act(async () => {
    await hook.result.current.sendMessage('hello', 'session-1', 'local');
  });
}

function setVisibilityState(state: 'hidden' | 'visible'): void {
  Object.defineProperty(document, 'visibilityState', {
    value: state,
    configurable: true,
  });
}

// ── Setup ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  capturedOnEvent = null;
  capturedOpts = null;
  mockSend.mockClear();
  mockClose.mockClear();
  mockConnect.mockClear();
  localStorage.clear();
  setVisibilityState('visible');
});

// ── isReconnecting tests ───────────────────────────────────────────────────

describe('useSSEStream — isReconnecting', () => {
  it('is false initially', () => {
    const hook = renderHook(() => useSSEStream());
    expect(hook.result.current.isReconnecting).toBe(false);
  });

  it('is set true when onWsDisconnected fires while isStreaming', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    expect(hook.result.current.isStreaming).toBe(true);

    act(() => {
      capturedOpts?.onWsDisconnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(true);
  });

  it('stays false when onWsDisconnected fires after streaming has ended (DONE received)', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    // Complete the turn first
    pushEvent({ type: 'DONE', seq: null });
    expect(hook.result.current.isStreaming).toBe(false);

    // WS disconnect AFTER turn ended — should not set isReconnecting
    act(() => {
      capturedOpts?.onWsDisconnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(false);
  });

  it('is cleared to false by onWsConnected', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    act(() => {
      capturedOpts?.onWsDisconnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(true);

    act(() => {
      capturedOpts?.onWsConnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(false);
  });

  it('is cleared to false by DONE event', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    act(() => {
      capturedOpts?.onWsDisconnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(true);

    pushEvent({ type: 'DONE', seq: null });
    expect(hook.result.current.isReconnecting).toBe(false);
  });

  it('is cleared to false by sendMessage', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    act(() => {
      capturedOpts?.onWsDisconnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(true);

    await act(async () => {
      await hook.result.current.sendMessage('new message', 'session-1', 'local');
    });
    expect(hook.result.current.isReconnecting).toBe(false);
  });
});

// ── Draft persistence tests ────────────────────────────────────────────────

describe('useSSEStream — draft persistence on visibility hide', () => {
  const DRAFT_KEY = (sid: string) => `seshat_bg_draft_${sid}`;

  it('writes draft to localStorage on visibilitychange→hidden while streaming', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    // Simulate some partial text arriving
    pushEvent({ type: 'TEXT_DELTA', data: { text: 'partial response' }, seq: 1 });

    // App goes to background
    setVisibilityState('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    const draftRaw = localStorage.getItem(DRAFT_KEY('session-1'));
    expect(draftRaw).not.toBeNull();
    const draft = JSON.parse(draftRaw!) as { content: string; at: string };
    expect(draft.content).toBe('partial response');
    expect(typeof draft.at).toBe('string');
  });

  it('does not write draft when not streaming', () => {
    renderHook(() => useSSEStream());
    // No startTurn — hook is idle

    setVisibilityState('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });

    const keys = Object.keys(localStorage).filter((k) => k.startsWith('seshat_bg_draft_'));
    expect(keys).toHaveLength(0);
  });

  it('clears draft from localStorage on DONE event', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent({ type: 'TEXT_DELTA', data: { text: 'content' }, seq: 1 });

    // Persist draft via visibility hide
    setVisibilityState('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(localStorage.getItem(DRAFT_KEY('session-1'))).not.toBeNull();

    // DONE should clear the draft
    pushEvent({ type: 'DONE', seq: null });
    expect(localStorage.getItem(DRAFT_KEY('session-1'))).toBeNull();
  });

  it('clears draft from localStorage on sendMessage', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent({ type: 'TEXT_DELTA', data: { text: 'content' }, seq: 1 });

    // Persist draft
    setVisibilityState('hidden');
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(localStorage.getItem(DRAFT_KEY('session-1'))).not.toBeNull();

    // New message should clear the draft
    await act(async () => {
      await hook.result.current.sendMessage('new', 'session-1', 'local');
    });
    expect(localStorage.getItem(DRAFT_KEY('session-1'))).toBeNull();
  });
});
