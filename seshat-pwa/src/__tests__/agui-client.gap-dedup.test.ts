/**
 * FRE-542 — Gap-aware client dedup (defense-in-depth for live-render ordering).
 *
 * Verifies that the WebSocket client buffers out-of-order events and flushes
 * them in seq order, so a seq inversion (the FRE-518 failure mode) cannot
 * permanently drop events or cause ordering issues.
 *
 * Test strategy: mock global.WebSocket to get a handle on the WS instance,
 * inject messages directly via onmessage, and assert onEvent call order.
 *
 * Note on localStorage seq key: tests use `seshat_last_seq_<sessionId>` which
 * maps to the stored ackSeq — jsdom provides a working localStorage.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { connectWebSocket } from '@/lib/agui-client';

// ── WebSocket mock ─────────────────────────────────────────────────────────
// Must be a class (not vi.fn() + arrow implementation) so that `new WebSocket()`
// works correctly. Vitest warns and fails when the implementation is an arrow fn.

let lastFakeWS: MockWebSocket | null = null;

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
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    lastFakeWS = this;
  }

  /** Called by tests to simulate the server opening the connection. */
  triggerOpen(): void {
    this.onopen?.(new Event('open'));
  }

  /** Called by tests to inject a server → client message. */
  triggerMessage(payload: unknown): void {
    this.onmessage?.(
      new MessageEvent('message', { data: JSON.stringify(payload) }),
    );
  }
}

const SESSION = 'test-session-gap-dedup';
const SEQ_KEY = `seshat_last_seq_${SESSION}`;

// ── Setup ──────────────────────────────────────────────────────────────────

beforeEach(() => {
  lastFakeWS = null;
  localStorage.clear();
  // Stub WebSocket with a class so `new WebSocket(url)` works correctly.
  // Static constants (CONNECTING=0 etc.) must be present so connectWebSocket's
  // guard check (`ws?.readyState === WebSocket.CONNECTING`) doesn't compare
  // undefined===undefined and falsely skip connection setup.
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
});

/** Flush microtasks so async connect() resolves and assigns ws.onopen etc. */
async function flushAsync(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
}

// ── Helpers ────────────────────────────────────────────────────────────────

function makeEvent(seq: number, text = `text-${seq}`) {
  return { type: 'TEXT_DELTA', seq, data: { text } };
}

function makeDone() {
  return { type: 'DONE', seq: null };
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe('FRE-542 gap-aware client dedup', () => {
  describe('in-order delivery (no regression)', () => {
    it('dispatches seq=1 then seq=2 in arrival order', async () => {
      const received: unknown[] = [];
      const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

      await flushAsync();
      lastFakeWS!.triggerOpen();
      lastFakeWS!.triggerMessage(makeEvent(1));
      lastFakeWS!.triggerMessage(makeEvent(2));

      expect(received).toHaveLength(2);
      expect((received[0] as { seq: number }).seq).toBe(1);
      expect((received[1] as { seq: number }).seq).toBe(2);

      conn.close();
    });
  });

  describe('out-of-order delivery', () => {
    it('seq=2 arrives before seq=1 — neither event is dropped', async () => {
      const received: unknown[] = [];
      const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

      await flushAsync();
      lastFakeWS!.triggerOpen();

      // Deliver seq=2 FIRST (the FRE-518 race failure mode)
      lastFakeWS!.triggerMessage(makeEvent(2));
      // seq=2 should be buffered, not dispatched yet
      expect(received).toHaveLength(0);

      // Now deliver seq=1 — this fills the gap; both should flush in order
      lastFakeWS!.triggerMessage(makeEvent(1));
      expect(received).toHaveLength(2);
      expect((received[0] as { seq: number }).seq).toBe(1);
      expect((received[1] as { seq: number }).seq).toBe(2);

      conn.close();
    });

    it('seq=3 arrives before seq=1 and seq=2 — all three eventually dispatched in order', async () => {
      const received: unknown[] = [];
      const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

      await flushAsync();
      lastFakeWS!.triggerOpen();

      lastFakeWS!.triggerMessage(makeEvent(3));
      expect(received).toHaveLength(0);
      lastFakeWS!.triggerMessage(makeEvent(1));
      expect(received).toHaveLength(1);
      expect((received[0] as { seq: number }).seq).toBe(1);
      lastFakeWS!.triggerMessage(makeEvent(2));
      expect(received).toHaveLength(3);
      expect((received[1] as { seq: number }).seq).toBe(2);
      expect((received[2] as { seq: number }).seq).toBe(3);

      conn.close();
    });
  });

  describe('reconnect watermark', () => {
    it('CONNECT message uses stored ackSeq, not max-seen', async () => {
      // Simulate a prior connection where ackSeq was persisted at seq=1
      // but seq=3 was received (out-of-order, buffered, connection dropped
      // before seq=2 arrived).
      // The correct reconnect watermark is ackSeq=1 (last contiguously dispatched).
      localStorage.setItem(SEQ_KEY, '1');

      const conn = connectWebSocket(SESSION, () => {});
      await flushAsync();
      lastFakeWS!.triggerOpen();

      // The CONNECT message sent to the server should carry last_seq=1
      const connectCall = lastFakeWS!.send.mock.calls.find(([msg]) => {
        const parsed = JSON.parse(msg as string);
        return parsed.type === 'CONNECT';
      });
      expect(connectCall).toBeDefined();
      const connectMsg = JSON.parse(connectCall![0] as string);
      expect(connectMsg.last_seq).toBe(1);

      conn.close();
    });

    it('ackSeq advances only when contiguous seqs are dispatched', async () => {
      const conn = connectWebSocket(SESSION, () => {});
      await flushAsync();
      lastFakeWS!.triggerOpen();

      // seq=1 dispatched → ackSeq=1 persisted
      lastFakeWS!.triggerMessage(makeEvent(1));
      expect(localStorage.getItem(SEQ_KEY)).toBe('1');

      // seq=3 buffered (gap at seq=2) — ackSeq must NOT advance to 3
      lastFakeWS!.triggerMessage(makeEvent(3));
      expect(localStorage.getItem(SEQ_KEY)).toBe('1');

      // seq=2 fills the gap — ackSeq should now be 3 (drained both)
      lastFakeWS!.triggerMessage(makeEvent(2));
      expect(localStorage.getItem(SEQ_KEY)).toBe('3');

      conn.close();
    });
  });

  describe('cold-start fallback (DONE flush)', () => {
    it('events stuck in buffer are flushed in seq order on DONE', async () => {
      // Simulate cold start: ackSeq=0 from storage but server-assigned seqs
      // start at a high global value (not contiguous from 1).
      // The ackSeq+1 drain will stall; DONE must flush them.
      const received: unknown[] = [];
      const conn = connectWebSocket(SESSION, (ev) => received.push(ev));
      await flushAsync();
      lastFakeWS!.triggerOpen();

      lastFakeWS!.triggerMessage(makeEvent(100));
      lastFakeWS!.triggerMessage(makeEvent(101));
      expect(received).toHaveLength(0); // stuck in buffer

      lastFakeWS!.triggerMessage(makeDone());
      // DONE flush: 100, 101 dispatched in order, then DONE
      expect(received).toHaveLength(3);
      expect((received[0] as { seq: number }).seq).toBe(100);
      expect((received[1] as { seq: number }).seq).toBe(101);
      expect((received[2] as { type: string }).type).toBe('DONE');

      conn.close();
    });
  });

  describe('true duplicates are dropped', () => {
    it('same seq arriving twice on one connection is delivered only once', async () => {
      const received: unknown[] = [];
      const conn = connectWebSocket(SESSION, (ev) => received.push(ev));
      await flushAsync();
      lastFakeWS!.triggerOpen();

      lastFakeWS!.triggerMessage(makeEvent(1));
      lastFakeWS!.triggerMessage(makeEvent(1)); // duplicate
      expect(received).toHaveLength(1);

      conn.close();
    });

    it('server replay duplicate (seq <= ackSeq) is dropped', async () => {
      // Pre-seed ackSeq=5 — simulates a reconnect where server mistakenly
      // replays an already-acked event.
      localStorage.setItem(SEQ_KEY, '5');
      const received: unknown[] = [];
      const conn = connectWebSocket(SESSION, (ev) => received.push(ev));
      await flushAsync();
      lastFakeWS!.triggerOpen();

      lastFakeWS!.triggerMessage(makeEvent(3)); // seq <= ackSeq — drop
      lastFakeWS!.triggerMessage(makeEvent(5)); // seq <= ackSeq — drop
      expect(received).toHaveLength(0);

      lastFakeWS!.triggerMessage(makeEvent(6)); // seq > ackSeq — dispatch
      expect(received).toHaveLength(1);

      conn.close();
    });
  });
});
