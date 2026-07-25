/**
 * ADR-0123 T3 (FRE-936) — the live phase surface's hook-level state machine.
 *
 * Covers AC-3 (reconnect resumes, doesn't restart/re-narrate) and AC-9
 * (cancel/error resolve the active phase honestly), plus concurrent
 * children, the sendMessage reset, and the REPLAY_GAP non-reset decision.
 */

import { renderHook, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';

import { elapsedMs } from '@/lib/phase-elapsed';

// ── Module-level captured values ───────────────────────────────────────────

let capturedOnEvent: ((event: unknown) => void) | null = null;
let capturedOpts: {
  onWsConnected?: () => void;
  onWsDisconnected?: () => void;
} | null = null;

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

function phaseStart(
  seq: number,
  opts: { phase_id: string; phase?: string; started_at: string; detail?: string | null; parent_id?: string | null },
): object {
  return {
    type: 'PHASE_START',
    seq,
    session_id: 'session-1',
    data: {
      phase: opts.phase ?? 'planning',
      phase_id: opts.phase_id,
      started_at: opts.started_at,
      detail: opts.detail ?? null,
      parent_id: opts.parent_id ?? null,
    },
  };
}

function phaseEnd(
  seq: number,
  opts: { phase_id: string; phase?: string; parent_id?: string | null; ok?: boolean },
): object {
  return {
    type: 'PHASE_END',
    seq,
    session_id: 'session-1',
    data: {
      phase: opts.phase ?? 'planning',
      phase_id: opts.phase_id,
      parent_id: opts.parent_id ?? null,
      ok: opts.ok ?? true,
    },
  };
}

beforeEach(() => {
  capturedOnEvent = null;
  capturedOpts = null;
  mockConnect.mockClear();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

// ── Lifecycle ────────────────────────────────────────────────────────────

describe('useSSEStream — phases lifecycle', () => {
  it('is empty before any PHASE_START', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    expect(hook.result.current.phases).toEqual([]);
  });

  it('PHASE_START appends a running node holding the server fields verbatim', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent(
      phaseStart(1, {
        phase_id: 'p1',
        phase: 'artifact_build',
        started_at: '2026-07-25T10:00:00.000Z',
        detail: 'My Report',
      }),
    );

    expect(hook.result.current.phases).toEqual([
      {
        phaseId: 'p1',
        phase: 'artifact_build',
        detail: 'My Report',
        startedAt: '2026-07-25T10:00:00.000Z',
        state: 'running',
        parentId: null,
        endedAt: null,
      },
    ]);
  });

  it('PHASE_END with ok:true (default) resolves the matching node to completed', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseEnd(2, { phase_id: 'p1' }));
    expect(hook.result.current.phases[0].state).toBe('completed');
  });

  it('PHASE_END with ok:false resolves the matching node to error, independent of any later RUN_ERROR', async () => {
    // FRE-936: this is the realistic backend ordering — phase_span's `finally`
    // always emits PHASE_END before an outer error handler gets to RUN_ERROR.
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseEnd(2, { phase_id: 'p1', ok: false }));
    expect(hook.result.current.phases[0].state).toBe('error');
  });
});

// ── phase_state full-state snapshot (ADR-0123 §6, FRE-986) ──────────────────

function phaseState(seq: number, active: object[]): object {
  return {
    type: 'STATE_DELTA',
    seq,
    session_id: 'session-1',
    data: { key: 'phase_state', value: { active } },
  };
}

describe('useSSEStream — phase_state snapshot (FRE-986, AC-3)', () => {
  it('converges the active phase from a snapshot alone (no prior deltas)', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(
      phaseState(5, [
        {
          phase: 'planning',
          phase_id: 'p1',
          started_at: '2026-07-25T10:00:03.000Z',
          detail: null,
          parent_id: null,
        },
      ]),
    );
    expect(hook.result.current.phases).toHaveLength(1);
    expect(hook.result.current.phases[0]).toMatchObject({ phaseId: 'p1', state: 'running' });
    expect(hook.result.current.phases[0].startedAt).toBe('2026-07-25T10:00:03.000Z');
  });

  it('self-corrects a stuck-running phase whose PHASE_END was dropped', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    // PHASE_END for p1 is dropped; a later snapshot (for a new phase p2) omits p1.
    pushEvent(
      phaseState(3, [
        {
          phase: 'synthesis',
          phase_id: 'p2',
          started_at: '2026-07-25T10:01:00.000Z',
          detail: null,
          parent_id: null,
        },
      ]),
    );
    const byId = Object.fromEntries(hook.result.current.phases.map((p) => [p.phaseId, p]));
    expect(byId.p1.state).toBe('completed');
    expect(byId.p2.state).toBe('running');
  });

  it('lets a later RUN_ERROR upgrade a snapshot-resolved node to error', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseState(3, [])); // p1 dropped its PHASE_END(ok:false) → snapshot-completes it
    expect(hook.result.current.phases[0].state).toBe('completed');
    pushEvent({ type: 'RUN_ERROR', session_id: 'session-1', data: { category: 'x', reason: 'y' } });
    expect(hook.result.current.phases[0].state).toBe('error');
  });

  it('lets a later CANCELLED upgrade a snapshot-resolved node to cancelled', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseState(3, []));
    pushEvent({ type: 'CANCELLED', session_id: 'session-1' });
    expect(hook.result.current.phases[0].state).toBe('cancelled');
  });

  it('does NOT let RUN_ERROR mislabel a genuinely completed earlier phase', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseEnd(2, { phase_id: 'p1' })); // genuine ok:true completion
    pushEvent(phaseStart(3, { phase_id: 'p2', started_at: '2026-07-25T10:01:00.000Z' }));
    pushEvent({ type: 'RUN_ERROR', session_id: 'session-1', data: { category: 'x', reason: 'y' } });
    const byId = Object.fromEntries(hook.result.current.phases.map((p) => [p.phaseId, p]));
    expect(byId.p1.state).toBe('completed'); // untouched
    expect(byId.p2.state).toBe('error'); // the running one is swept
  });

  it('ignores a malformed phase_state payload without throwing', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    // null value, non-array active — must be ignored, leaving p1 running.
    pushEvent({ type: 'STATE_DELTA', session_id: 'session-1', data: { key: 'phase_state', value: null } });
    pushEvent({
      type: 'STATE_DELTA',
      session_id: 'session-1',
      data: { key: 'phase_state', value: { active: 'nope' } },
    });
    expect(hook.result.current.phases[0].state).toBe('running');
  });
});

// ── AC-3: reconnect resumes rather than restarts or re-narrates ────────────

describe('useSSEStream — AC-3 reconnect', () => {
  it('60s-run + 30s-drop + reattach: shows the currently active phase (not the first), elapsed ≈90s, byte-equal startedAt, and a phase completed before the drop is not re-narrated as active', async () => {
    const t0 = new Date('2026-07-25T10:00:00.000Z');
    vi.setSystemTime(t0);

    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    // p0: the turn's FIRST phase — starts and fully ends before the drop.
    pushEvent(phaseStart(1, { phase_id: 'p0', started_at: t0.toISOString() }));
    pushEvent(phaseEnd(2, { phase_id: 'p0' }));

    // p1: starts next, runs 60s through the drop.
    const p1Start = new Date('2026-07-25T10:00:05.000Z');
    vi.setSystemTime(p1Start);
    pushEvent(phaseStart(3, { phase_id: 'p1', phase: 'synthesis', started_at: p1Start.toISOString() }));

    // Advance 60s (still running), then simulate the socket dropping.
    vi.setSystemTime(new Date(p1Start.getTime() + 60_000));
    act(() => {
      capturedOpts?.onWsDisconnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(true);

    // 30s disconnect window, then reattach. No new events for p1 arrive
    // (nothing changed server-side) — the existing seq-replay mechanism
    // would redeliver anything that did.
    vi.setSystemTime(new Date(p1Start.getTime() + 90_000));
    act(() => {
      capturedOpts?.onWsConnected?.();
    });
    expect(hook.result.current.isReconnecting).toBe(false);

    const phases = hook.result.current.phases;
    const p0 = phases.find((p) => p.phaseId === 'p0')!;
    const p1 = phases.find((p) => p.phaseId === 'p1')!;

    // (c) completed-before-drop is not re-narrated as active.
    expect(p0.state).toBe('completed');

    // (a) the surface reflects the currently active phase (p1), not the
    // turn's first phase (p0, already resolved above).
    expect(p1.state).toBe('running');

    // (b) byte-equal timestamp + elapsed ≈90s, not ≈0/≈30/≈60.
    expect(p1.startedAt).toBe(p1Start.toISOString());
    const elapsed = elapsedMs(p1.startedAt, Date.now());
    expect(elapsed).toBeGreaterThanOrEqual(90_000 - 2000);
    expect(elapsed).toBeLessThanOrEqual(90_000 + 2000);
  });
});

// ── AC-9: cancel/error terminate the surface honestly ──────────────────────

describe('useSSEStream — AC-9 terminal states', () => {
  it('(a) CANCELLED resolves a still-running phase to cancelled (backstop — no PHASE_END arrived)', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent({ type: 'CANCELLED', seq: null });
    expect(hook.result.current.phases[0].state).toBe('cancelled');
  });

  it('(a) CANCELLED does not touch a phase already resolved by its own PHASE_END', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseEnd(2, { phase_id: 'p1' }));
    pushEvent({ type: 'CANCELLED', seq: null });
    expect(hook.result.current.phases[0].state).toBe('completed');
  });

  it('(b) RUN_ERROR resolves a still-running phase to error (backstop — no PHASE_END arrived)', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent({
      type: 'RUN_ERROR',
      seq: null,
      data: { category: 'generic', reason: 'boom', next_step: '', actions: [], partial: false },
    });
    expect(hook.result.current.phases[0].state).toBe('error');
  });

  it('(b) a phase already resolved to error via ok:false stays error across a later RUN_ERROR', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent(phaseEnd(2, { phase_id: 'p1', ok: false }));
    pushEvent({
      type: 'RUN_ERROR',
      seq: null,
      data: { category: 'generic', reason: 'boom', next_step: '', actions: [], partial: false },
    });
    expect(hook.result.current.phases[0].state).toBe('error');
  });

  it('DONE resolves any still-running phase to completed (safety net — nothing spins forever)', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    pushEvent({ type: 'DONE', seq: null });
    expect(hook.result.current.phases[0].state).toBe('completed');
  });
});

// ── Concurrent children ─────────────────────────────────────────────────────

describe('useSSEStream — concurrent children (AC-8 shape)', () => {
  it('a parent EXPANSION phase and its SUB_AGENT children each resolve independently', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);

    pushEvent(
      phaseStart(1, { phase_id: 'e1', phase: 'expansion', started_at: '2026-07-25T10:00:00.000Z', detail: '2 sub-agents' }),
    );
    pushEvent(
      phaseStart(2, { phase_id: 'c1', phase: 'sub_agent', started_at: '2026-07-25T10:00:01.000Z', parent_id: 'e1' }),
    );
    pushEvent(
      phaseStart(3, { phase_id: 'c2', phase: 'sub_agent', started_at: '2026-07-25T10:00:01.000Z', parent_id: 'e1' }),
    );

    pushEvent(phaseEnd(4, { phase_id: 'c1', phase: 'sub_agent', parent_id: 'e1' }));

    let byId = Object.fromEntries(hook.result.current.phases.map((p) => [p.phaseId, p]));
    expect(byId.e1.state).toBe('running');
    expect(byId.c1.state).toBe('completed');
    expect(byId.c2.state).toBe('running');

    pushEvent(phaseEnd(5, { phase_id: 'c2', phase: 'sub_agent', parent_id: 'e1' }));
    pushEvent(phaseEnd(6, { phase_id: 'e1', phase: 'expansion' }));

    byId = Object.fromEntries(hook.result.current.phases.map((p) => [p.phaseId, p]));
    expect(byId.e1.state).toBe('completed');
    expect(byId.c1.state).toBe('completed');
    expect(byId.c2.state).toBe('completed');
  });
});

// ── Reset semantics ──────────────────────────────────────────────────────

describe('useSSEStream — phases reset semantics', () => {
  it('resets to empty on a new sendMessage (new turn)', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    expect(hook.result.current.phases).toHaveLength(1);

    await act(async () => {
      await hook.result.current.sendMessage('new message', 'session-1', 'local');
    });
    expect(hook.result.current.phases).toEqual([]);
  });

  it('is left untouched by REPLAY_GAP — clearing would destroy live state with nothing to replace it', async () => {
    const hook = renderHook(() => useSSEStream());
    await startTurn(hook);
    pushEvent(phaseStart(1, { phase_id: 'p1', started_at: '2026-07-25T10:00:00.000Z' }));
    const before = hook.result.current.phases;

    pushEvent({ type: 'REPLAY_GAP', seq: null, oldest_available_seq: 999 });

    expect(hook.result.current.phases).toBe(before);
  });
});
