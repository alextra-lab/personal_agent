/**
 * Pure elapsed-time math for the live phase surface (ADR-0123 T3, FRE-936).
 *
 * Kept as injectable-`now` pure functions specifically so AC-3/AC-5's timing
 * assertions are deterministic — no fake-timer flakiness.
 */

import { describe, it, expect } from 'vitest';

import { elapsedMs, formatElapsed, ESCALATION_THRESHOLD_MS } from '@/lib/phase-elapsed';

describe('elapsedMs', () => {
  it('computes elapsed from an ISO server timestamp to `now`', () => {
    const startedAt = '2026-07-25T10:00:00.000Z';
    const now = new Date('2026-07-25T10:00:05.000Z').getTime();
    expect(elapsedMs(startedAt, now)).toBe(5000);
  });

  it('defaults `now` to the current wall clock when omitted', () => {
    const startedAt = new Date(Date.now() - 1000).toISOString();
    expect(elapsedMs(startedAt)).toBeGreaterThanOrEqual(1000);
  });

  it('never returns negative elapsed (clock skew safety)', () => {
    const startedAt = '2026-07-25T10:00:05.000Z';
    const now = new Date('2026-07-25T10:00:00.000Z').getTime();
    expect(elapsedMs(startedAt, now)).toBe(0);
  });

  // ADR-0123 AC-3(b): phase runs 60s, socket drops 30s, reattach — elapsed
  // must read ≈90s from the server's phase-start timestamp, not ≈0/≈30/≈60
  // (the three wrong-hypothesis traps a reconnect-plus-offset implementation
  // would produce).
  it('AC-3(b): 60s-run + 30s-drop scenario reads ≈90s, not the wrong-hypothesis values', () => {
    const startedAt = '2026-07-25T10:00:00.000Z';
    const reattachAt = new Date('2026-07-25T10:01:30.000Z').getTime(); // +90s

    const elapsed = elapsedMs(startedAt, reattachAt);

    expect(elapsed).toBeGreaterThanOrEqual(90_000 - 2000);
    expect(elapsed).toBeLessThanOrEqual(90_000 + 2000);
    // Explicitly rule out the three wrong hypotheses (ADR AC-3 note).
    expect(elapsed).not.toBeCloseTo(0, -3);
    expect(elapsed).not.toBeCloseTo(30_000, -3);
    expect(elapsed).not.toBeCloseTo(60_000, -3);
  });

  // ADR-0123 AC-5: displayed elapsed increases monotonically and tracks true
  // wall-clock at two sampled instants ≥30s apart.
  it('AC-5: increases monotonically at two sampled instants ≥30s apart', () => {
    const startedAt = '2026-07-25T10:00:00.000Z';
    const t1 = new Date('2026-07-25T10:01:00.000Z').getTime(); // +60s
    const t2 = new Date('2026-07-25T10:01:35.000Z').getTime(); // +95s (35s later)

    const e1 = elapsedMs(startedAt, t1);
    const e2 = elapsedMs(startedAt, t2);

    expect(e2).toBeGreaterThan(e1);
    expect(e2 - e1).toBeGreaterThanOrEqual(35_000 - 2000);
    expect(e2 - e1).toBeLessThanOrEqual(35_000 + 2000);
  });
});

describe('formatElapsed', () => {
  it('formats sub-minute durations as seconds', () => {
    expect(formatElapsed(0)).toBe('0s');
    expect(formatElapsed(5000)).toBe('5s');
    expect(formatElapsed(59_000)).toBe('59s');
  });

  it('formats minute-plus durations as m/s', () => {
    expect(formatElapsed(60_000)).toBe('1m 00s');
    expect(formatElapsed(130_000)).toBe('2m 10s');
  });
});

describe('ESCALATION_THRESHOLD_MS', () => {
  it('is a positive duration used to gate escalating-candour copy (ADR §3)', () => {
    expect(ESCALATION_THRESHOLD_MS).toBeGreaterThan(0);
  });
});
