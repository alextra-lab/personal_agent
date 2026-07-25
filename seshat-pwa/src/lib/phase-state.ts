/**
 * Client-side reconciliation of the ADR-0123 §6 / FRE-986 `phase_state` full-state snapshot.
 *
 * The snapshot is authoritative for the set of *currently-active* phases, keyed by session and
 * carried as a replaceable STATE_DELTA (mirroring `turn_status`). Reconciling against it gives
 * the two properties the ADR requires that an event-log replay cannot:
 *   - **convergence** — a reconnecting client rebuilds the active phase from one message, and
 *   - **self-correction** — a phase whose PHASE_END was dropped is resolved by the next snapshot
 *     rather than left spinning.
 */

import type { PhaseNode, PhaseSnapshotEntry } from './types';

/**
 * Fold a `phase_state` snapshot into the live phase list.
 *
 * @param prev - the current phase nodes.
 * @param active - the snapshot's currently-active phase entries.
 * @returns the reconciled phase list (a new array); the input is not mutated.
 *
 * Rules:
 *  1. A `running` node absent from the snapshot is resolved to `completed` and marked
 *     `snapshotResolved` — the dropped-PHASE_END safety net, kept upgradable by a later
 *     terminal delta.
 *  2. A snapshot entry with no matching node is appended as `running`, its server `started_at`
 *     held verbatim so elapsed stays server-anchored (AC-3(b)).
 *  3. A `running` node still in the snapshot is left untouched (elapsed keeps advancing).
 * Idempotent: applying the same snapshot twice is a no-op.
 */
export function reconcilePhaseSnapshot(
  prev: PhaseNode[],
  active: readonly PhaseSnapshotEntry[],
): PhaseNode[] {
  const activeIds = new Set(active.map((e) => e.phase_id));
  const next: PhaseNode[] = prev.map((p) =>
    p.state === 'running' && !activeIds.has(p.phaseId)
      ? { ...p, state: 'completed', snapshotResolved: true, endedAt: p.endedAt ?? Date.now() }
      : p,
  );
  for (const e of active) {
    if (!next.some((p) => p.phaseId === e.phase_id)) {
      next.push({
        phaseId: e.phase_id,
        phase: e.phase,
        detail: e.detail,
        startedAt: e.started_at,
        state: 'running',
        parentId: e.parent_id,
        endedAt: null,
      });
    }
  }
  return next;
}
