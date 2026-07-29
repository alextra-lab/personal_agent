/**
 * FRE-1040 — a hole in the seq series must never stall the client forever.
 *
 * Background. The client dispatches only a contiguous run from its stored
 * `ackSeq`. Before FRE-1040 the server drew `seq` from one global Postgres
 * sequence, so a second live conversation consumed numbers inside this
 * session's series and the client waited forever for a number that would never
 * arrive on this socket — the assistant response sat in `pendingBuf` and only a
 * full session reload recovered it.
 *
 * The server-side fix (per-session sequence) removes that cause. These tests
 * cover what remains: a *genuine* hole (a `queue_full` drop, a `max_sent_seq`
 * skip, or the migration boundary) must still resolve.
 *
 * The ordering rule under test, and why it is not simply "flush after N ms":
 * advancing `ackSeq` past a hole is exactly what FRE-590 removed, because it
 * makes reconnect replay unable to recover the missing event. So the stall
 * timer triggers *recovery* — one forced reconnect, `ackSeq` untouched — and
 * the buffer is flushed only once the server has proven, via REPLAY_COMPLETE,
 * that it cannot fill the hole.
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

const SESSION = 'test-session-hole-recovery';
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

describe('FRE-1040 hole recovery', () => {
  it('a stalled buffer forces a reconnect WITHOUT advancing ackSeq (FRE-590 preserved)', async () => {
    localStorage.setItem(SEQ_KEY, '1');
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

    await settle();
    latest().triggerOpen();

    // seq=3 arrives; seq=2 is the hole. Nothing dispatches.
    latest().triggerMessage(makeEvent(3));
    expect(received).toHaveLength(0);
    expect(wsInstances).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(PAST_STALL);

    // Recovery, not data loss: a new socket, and the watermark is untouched so
    // the server replays from seq=2 and can still fill the hole.
    expect(wsInstances.length).toBeGreaterThan(1);
    expect(localStorage.getItem(SEQ_KEY)).toBe('1');
    expect(received).toHaveLength(0);

    conn.close();
  });

  it('the replayed event fills the hole and no flush happens (AC-3, self-correcting)', async () => {
    localStorage.setItem(SEQ_KEY, '1');
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

    await settle();
    latest().triggerOpen();
    latest().triggerMessage(makeEvent(3));
    await vi.advanceTimersByTimeAsync(PAST_STALL);

    // Reconnected socket: the server replays the missing seq=2, then seq=3.
    await settle();
    const reconnected = latest();
    reconnected.triggerOpen();
    reconnected.triggerMessage(makeEvent(2));
    reconnected.triggerMessage(makeEvent(3));

    const seqs = received.map((e) => (e as { seq?: number }).seq);
    expect(seqs).toEqual([2, 3]);
    expect(localStorage.getItem(SEQ_KEY)).toBe('3');

    conn.close();
  });

  it('REPLAY_COMPLETE over an unfillable hole flushes the buffer (AC-2)', async () => {
    localStorage.setItem(SEQ_KEY, '1');
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

    await settle();
    latest().triggerOpen();
    latest().triggerMessage(makeEvent(3));
    await vi.advanceTimersByTimeAsync(PAST_STALL);

    await settle();
    const reconnected = latest();
    reconnected.triggerOpen();
    // Replay redelivers seq=3 but has nothing at seq=2 — the hole is genuine.
    reconnected.triggerMessage(makeEvent(3));
    expect(received).toHaveLength(0);

    reconnected.triggerMessage({ type: 'REPLAY_COMPLETE', seq: null });

    const seqs = received
      .map((e) => (e as { seq?: number }).seq)
      .filter((s): s is number => s != null);
    expect(seqs).toContain(3);
    expect(localStorage.getItem(SEQ_KEY)).toBe('3');

    conn.close();
  });

  it('a second stall on the same hole flushes — no reconnect loop against an old server', async () => {
    // Rollout window: new PWA, gateway not yet rebuilt, so REPLAY_COMPLETE never
    // arrives. The client must still resolve rather than reconnect every 3s.
    localStorage.setItem(SEQ_KEY, '1');
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

    await settle();
    latest().triggerOpen();
    latest().triggerMessage(makeEvent(3));

    await vi.advanceTimersByTimeAsync(PAST_STALL);
    const socketsAfterFirstStall = wsInstances.length;
    expect(socketsAfterFirstStall).toBeGreaterThan(1);

    await settle();
    latest().triggerOpen();
    latest().triggerMessage(makeEvent(3));
    await vi.advanceTimersByTimeAsync(PAST_STALL);

    // Flushed, not reconnected again.
    expect(wsInstances).toHaveLength(socketsAfterFirstStall);
    const seqs = received
      .map((e) => (e as { seq?: number }).seq)
      .filter((s): s is number => s != null);
    expect(seqs).toContain(3);
    expect(localStorage.getItem(SEQ_KEY)).toBe('3');

    conn.close();
  });

  it('a cold-start client flushes instead of reconnecting — a reconnect would destroy the turn', async () => {
    // ackSeq===0 is not a hole, it is the absence of a watermark: an existing
    // session's numbering continues from wherever it left off, so seq 1 never
    // arrives and the contiguous drain can never start.
    //
    // A forced reconnect here is strictly destructive. The server gates replay
    // on `last_seq > 0` (ws_endpoint.py), so a CONNECT carrying last_seq=0
    // replays nothing and sends no REPLAY_COMPLETE — while connect() has already
    // cleared the buffer holding the response. Flushing is both safe and correct:
    // with no watermark, "everything above 0" is simply everything received.
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

    await settle();
    latest().triggerOpen();
    latest().triggerMessage(makeEvent(9001));
    latest().triggerMessage(makeEvent(9002));
    expect(received).toHaveLength(0);

    await vi.advanceTimersByTimeAsync(PAST_STALL);

    expect(wsInstances).toHaveLength(1); // no reconnect
    expect(received.map((e) => (e as { seq?: number }).seq)).toEqual([9001, 9002]);
    expect(localStorage.getItem(SEQ_KEY)).toBe('9002');

    conn.close();
  });

  it('a drained buffer disarms the timer — no stale reconnect', async () => {
    localStorage.setItem(SEQ_KEY, '1');
    const received: unknown[] = [];
    const conn = connectWebSocket(SESSION, (ev) => received.push(ev));

    await settle();
    latest().triggerOpen();

    latest().triggerMessage(makeEvent(3)); // buffered — arms the timer
    latest().triggerMessage(makeEvent(2)); // fills the hole — drains both

    expect(received).toHaveLength(2);
    expect(localStorage.getItem(SEQ_KEY)).toBe('3');

    await vi.advanceTimersByTimeAsync(PAST_STALL);
    expect(wsInstances).toHaveLength(1);

    conn.close();
  });

  it('closing the connection cancels a pending stall timer', async () => {
    localStorage.setItem(SEQ_KEY, '1');
    const conn = connectWebSocket(SESSION, () => {});

    await settle();
    latest().triggerOpen();
    latest().triggerMessage(makeEvent(3));

    conn.close();
    await vi.advanceTimersByTimeAsync(PAST_STALL);

    expect(wsInstances).toHaveLength(1);
  });
});
