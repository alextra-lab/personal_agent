/**
 * FRE-236 — iOS visibility lifecycle callbacks for connectWebSocket.
 *
 * Verifies:
 * - onWsConnected fires when the WebSocket opens.
 * - onWsDisconnected fires on unexpected close (not intentional, not superseded).
 * - onWsDisconnected does NOT fire on intentional close() call.
 * - onWsDisconnected does NOT fire on code-4001 (superseded) close.
 * - visibilitychange→visible triggers reconnect when WS is closed.
 * - hidden_duration_ms included in CONNECT payload after visibility hide→visible.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { connectWebSocket } from '@/lib/agui-client';

// ── WebSocket mock ─────────────────────────────────────────────────────────

let wsInstances: MockWebSocket[] = [];

class MockWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.OPEN;

  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  send = vi.fn();
  close = vi.fn();

  constructor(_url: string) {
    wsInstances.push(this);
  }

  triggerOpen(): void {
    this.onopen?.(new Event('open'));
  }

  triggerClose(code = 1006): void {
    this.readyState = MockWebSocket.CLOSED;
    const ev = new CloseEvent('close', { code, wasClean: false });
    this.onclose?.(ev);
  }
}

const SESSION = 'test-session-visibility';

// ── Helpers ────────────────────────────────────────────────────────────────

function setVisibilityState(state: 'hidden' | 'visible'): void {
  Object.defineProperty(document, 'visibilityState', {
    value: state,
    configurable: true,
  });
}

async function flushAsync(): Promise<void> {
  // Flush one round of microtasks (covers the single await in getWSTicket)
  await new Promise<void>((r) => setTimeout(r, 0));
}

// ── Setup ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  wsInstances = [];
  localStorage.clear();
  vi.stubGlobal('WebSocket', MockWebSocket);
  setVisibilityState('visible');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
  setVisibilityState('visible');
});

// ── Tests ──────────────────────────────────────────────────────────────────

describe('connectWebSocket — onWsConnected callback', () => {
  it('fires onWsConnected when WS opens', async () => {
    const onConnected = vi.fn();
    const conn = connectWebSocket(SESSION, () => {}, undefined, {
      onWsConnected: onConnected,
    });
    await flushAsync();
    wsInstances[0].triggerOpen();

    expect(onConnected).toHaveBeenCalledTimes(1);
    conn.close();
  });
});

describe('connectWebSocket — onWsDisconnected callback', () => {
  it('fires onWsDisconnected on unexpected close', async () => {
    const onDisconnected = vi.fn();
    const conn = connectWebSocket(SESSION, () => {}, undefined, {
      onWsDisconnected: onDisconnected,
    });
    await flushAsync();
    wsInstances[0].triggerOpen();
    wsInstances[0].triggerClose(1006);

    expect(onDisconnected).toHaveBeenCalledTimes(1);
    conn.close();
  });

  it('does NOT fire onWsDisconnected when close() is called intentionally', async () => {
    const onDisconnected = vi.fn();
    const conn = connectWebSocket(SESSION, () => {}, undefined, {
      onWsDisconnected: onDisconnected,
    });
    await flushAsync();
    wsInstances[0].triggerOpen();
    conn.close();

    expect(onDisconnected).not.toHaveBeenCalled();
  });

  it('does NOT fire onWsDisconnected on code-4001 (superseded) close', async () => {
    const onDisconnected = vi.fn();
    const conn = connectWebSocket(SESSION, () => {}, undefined, {
      onWsDisconnected: onDisconnected,
    });
    await flushAsync();
    wsInstances[0].triggerOpen();
    wsInstances[0].triggerClose(4001);

    expect(onDisconnected).not.toHaveBeenCalled();
    conn.close();
  });
});

describe('connectWebSocket — visibility-triggered reconnect', () => {
  it('reconnects when visibilitychange→visible fires with WS closed', async () => {
    const conn = connectWebSocket(SESSION, () => {});
    await flushAsync();
    wsInstances[0].triggerOpen();

    // Go hidden, then WS dies
    setVisibilityState('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    wsInstances[0].triggerClose(1006);

    // Return to foreground
    setVisibilityState('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await flushAsync();

    // A second WS instance should have been created
    expect(wsInstances.length).toBeGreaterThanOrEqual(2);
    conn.close();
  });

  it('includes hidden_duration_ms in CONNECT payload after visibility hide→visible cycle', async () => {
    const conn = connectWebSocket(SESSION, () => {});
    await flushAsync();
    wsInstances[0].triggerOpen();

    // Go hidden while WS is OPEN — agui-client tracks hiddenAt
    setVisibilityState('hidden');
    document.dispatchEvent(new Event('visibilitychange'));

    // Simulate WS dying (iOS kill while backgrounded)
    wsInstances[0].triggerClose(1006);

    // Return to foreground → triggers reconnect
    setVisibilityState('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await flushAsync();

    // Trigger the new WS open
    const secondWS = wsInstances[wsInstances.length - 1];
    secondWS.triggerOpen();

    // The CONNECT payload should contain hidden_duration_ms
    const connectCall = secondWS.send.mock.calls.find((args: unknown[]) => {
      try {
        const payload = JSON.parse(args[0] as string) as Record<string, unknown>;
        return payload['type'] === 'CONNECT';
      } catch {
        return false;
      }
    });
    expect(connectCall).toBeDefined();
    const connectPayload = JSON.parse(connectCall![0] as string) as Record<string, unknown>;
    expect(typeof connectPayload['hidden_duration_ms']).toBe('number');
    expect(connectPayload['hidden_duration_ms'] as number).toBeGreaterThanOrEqual(0);

    conn.close();
  });
});
