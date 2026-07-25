/**
 * reconcilePhaseSnapshot — the client half of the ADR-0123 §6 / FRE-986 phase-state
 * projection. A `phase_state` full-state snapshot is authoritative for the *currently-active*
 * phase set, so the client converges from the newest one alone and self-corrects when a
 * PhaseEnd delta is dropped (AC-3), without ever mislabelling a genuinely-completed phase.
 */

import { describe, it, expect } from 'vitest';

import { reconcilePhaseSnapshot } from '@/lib/phase-state';
import type { PhaseNode, PhaseSnapshotEntry } from '@/lib/types';

function running(phaseId: string, startedAt = '2026-07-25T10:00:00+00:00'): PhaseNode {
  return {
    phaseId,
    phase: 'planning',
    detail: null,
    startedAt,
    state: 'running',
    parentId: null,
    endedAt: null,
  };
}

function entry(phaseId: string, over: Partial<PhaseSnapshotEntry> = {}): PhaseSnapshotEntry {
  return {
    phase: 'planning',
    phase_id: phaseId,
    started_at: '2026-07-25T10:00:00+00:00',
    detail: null,
    parent_id: null,
    ...over,
  };
}

describe('reconcilePhaseSnapshot', () => {
  it('creates a running node from the snapshot alone (no prior deltas)', () => {
    const next = reconcilePhaseSnapshot([], [entry('p1', { started_at: '2026-07-25T10:00:05+00:00' })]);
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ phaseId: 'p1', state: 'running' });
    // AC-3(b): server timestamp held verbatim so elapsed is server-anchored.
    expect(next[0].startedAt).toBe('2026-07-25T10:00:05+00:00');
  });

  it('resolves a running node absent from the snapshot to completed + snapshotResolved (self-correction)', () => {
    const next = reconcilePhaseSnapshot([running('p1')], [{ ...entry('p2') }]);
    const p1 = next.find((p) => p.phaseId === 'p1')!;
    expect(p1.state).toBe('completed');
    expect(p1.snapshotResolved).toBe(true);
    expect(p1.endedAt).not.toBeNull();
    // and the new active phase is present as running
    expect(next.find((p) => p.phaseId === 'p2')!.state).toBe('running');
  });

  it('leaves a running node still present in the snapshot untouched', () => {
    const p1 = running('p1');
    const next = reconcilePhaseSnapshot([p1], [entry('p1')]);
    expect(next).toHaveLength(1);
    expect(next[0]).toEqual(p1);
  });

  it('carries multiple active phases (parent + children)', () => {
    const next = reconcilePhaseSnapshot(
      [],
      [
        entry('parent', { phase: 'expansion' }),
        entry('c1', { phase: 'sub_agent', parent_id: 'parent' }),
        entry('c2', { phase: 'sub_agent', parent_id: 'parent' }),
      ],
    );
    expect(next.map((p) => p.phaseId).sort()).toEqual(['c1', 'c2', 'parent']);
    expect(next.every((p) => p.state === 'running')).toBe(true);
    expect(next.find((p) => p.phaseId === 'c1')!.parentId).toBe('parent');
  });

  it('is idempotent — the same snapshot twice is stable', () => {
    const once = reconcilePhaseSnapshot([], [entry('p1')]);
    const twice = reconcilePhaseSnapshot(once, [entry('p1')]);
    expect(twice).toEqual(once);
  });

  it('does not re-narrate a genuinely completed (non-snapshot) node', () => {
    const done: PhaseNode = { ...running('p1'), state: 'completed', endedAt: 123 };
    const next = reconcilePhaseSnapshot([done], []);
    expect(next[0].state).toBe('completed');
    expect(next[0].snapshotResolved).toBeUndefined();
  });
});
