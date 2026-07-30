/**
 * ADR-0123 T4 (FRE-937) — master PR #758 bounce: labelFor must distinguish
 * repeated synthesis phases within one turn, not render the same generic
 * string once per tool-loop round. The owner observed seven identical
 * "Writing the response" rows on 2026-07-28 with no way to tell them apart.
 * The backend now threads `detail: "round N"` (executor.py's step_llm_call);
 * labelFor must use it the same way it already does for sub_agent.
 */

import { describe, it, expect } from 'vitest';

import { labelFor } from '@/lib/phase-labels';

describe('labelFor — synthesis round disambiguation', () => {
  it('appends the round detail to the base synthesis label when present', () => {
    expect(labelFor({ phase: 'synthesis', detail: 'round 2' })).toBe('Writing the response — round 2');
  });

  it('falls back to the generic label when synthesis has no detail (e.g. historical events)', () => {
    expect(labelFor({ phase: 'synthesis', detail: null })).toBe('Writing the response');
  });

  it('produces distinguishable labels for two different rounds', () => {
    const first = labelFor({ phase: 'synthesis', detail: 'round 1' });
    const second = labelFor({ phase: 'synthesis', detail: 'round 2' });
    expect(first).not.toBe(second);
  });
});

describe('labelFor — unaffected phases', () => {
  it('still shows a sub-agent detail as the whole label, not appended (unchanged behavior)', () => {
    expect(labelFor({ phase: 'sub_agent', detail: 'pricing history' })).toBe('pricing history');
  });

  it('planning ignores detail (it is only ever entered once per turn, never re-entered)', () => {
    expect(labelFor({ phase: 'planning', detail: 'round 1' })).toBe('Thinking');
  });

  it('phases with no detail concept render their plain label', () => {
    expect(labelFor({ phase: 'artifact_build', detail: null })).toBe('Building the artifact');
    expect(labelFor({ phase: 'waiting_for_choice', detail: null })).toBe('Waiting for your choice');
  });
});
