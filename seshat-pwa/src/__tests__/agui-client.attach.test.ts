/**
 * FRE-1543 — the attach handshake of a page that holds nothing in memory.
 *
 * A reloaded page must replay the turn in flight from its start, so it ignores the dead
 * page's watermark, then adopts the server's watermark once the replay is complete.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { connectWebSocket } from '@/lib/agui-client';

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
  close = vi.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
  });

  constructor(_url: string) {
    wsInstances.push(this);
  }

  triggerOpen(): void {
    this.onopen?.(new Event('open'));
  }

  triggerMessage(payload: unknown): void {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(payload) }));
  }
}

const SESSION = 'test-session-attach';
const SEQ_KEY = `seshat_last_seq_${SESSION}`;

/** Must exceed the client's stall timeout. */
const PAST_STALL = 3500;

beforeEach(() => {
  wsInstances = [];
  localStorage.clear();
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
});

/** Resolve connect()'s pending microtasks so ws handlers are assigned. */
async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

function latest(): MockWebSocket {
  return wsInstances[wsInstances.length - 1];
}

function makeEvent(seq: number, text = `text-${seq}`) {
  return { type: 'TEXT_DELTA', seq, data: { text } };
}

describe('FRE-1543 attach', () => {
  it('the attach CONNECT ignores a stale watermark, and REPLAY_COMPLETE delivers the replay then sets the new watermark', async () => {
    // The dead page acknowledged seq 40; the in-flight turn started at 35. A stale
    // watermark would drop 36..40, which this fresh page never rendered.
    localStorage.setItem(SEQ_KEY, '40');
    const received: Array<{ type: string; seq?: number | null }> = [];
    connectWebSocket(SESSION, (ev) => received.push(ev as { type: string; seq?: number | null }), undefined, {
      attach: true,
    });
    await settle();
    latest().triggerOpen();

    expect(JSON.parse(latest().send.mock.calls[0][0] as string)).toEqual({
      type: 'CONNECT',
      last_seq: 0,
      attach: true,
    });

    latest().triggerMessage({ type: 'STATE_DELTA', seq: null, data: { key: 'turn_status', value: {} } });
    latest().triggerMessage(makeEvent(36));
    latest().triggerMessage(makeEvent(37));
    latest().triggerMessage({ type: 'REPLAY_COMPLETE', seq: null, last_seq: 52, turn_in_flight: true });

    expect(received.map((e) => [e.type, e.seq])).toEqual([
      ['STATE_DELTA', null],
      ['TEXT_DELTA', 36],
      ['TEXT_DELTA', 37],
      ['REPLAY_COMPLETE', null],
    ]);
    expect(localStorage.getItem(SEQ_KEY)).toBe('52');

    // The next live event follows the server's watermark contiguously.
    latest().triggerMessage(makeEvent(53));
    expect(received[received.length - 1]).toMatchObject({ seq: 53 });
  });

  it('a socket lost before REPLAY_COMPLETE attaches again; after it, reconnects are normal', async () => {
    const drop = async () => {
      const ws = latest();
      ws.readyState = MockWebSocket.CLOSED;
      ws.onclose?.(new CloseEvent('close', { code: 1006 }));
      await vi.advanceTimersByTimeAsync(2000);
      latest().triggerOpen();
      return JSON.parse(latest().send.mock.calls[0][0] as string) as Record<string, unknown>;
    };
    connectWebSocket(SESSION, () => {}, undefined, { attach: true });
    await settle();
    latest().triggerOpen();

    expect(await drop()).toMatchObject({ attach: true });
    expect(wsInstances).toHaveLength(2);

    latest().triggerMessage({ type: 'REPLAY_COMPLETE', seq: null, last_seq: 9, turn_in_flight: false });
    expect(await drop()).toEqual({ type: 'CONNECT', last_seq: 9 });
    expect(wsInstances).toHaveLength(3);
  });

  it('a closed connection delivers no further frame (an A→B→A switch cannot leak A\'s old socket)', async () => {
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));
    await settle();
    latest().triggerOpen();
    const old = latest();

    conn.close();
    old.triggerMessage({ type: 'STATE_DELTA', seq: null, data: { key: 'turn_status', value: {} } });

    expect(received).toEqual([]);
  });
});
