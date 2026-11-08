/**
 * Tests for the dev_job payload guard and merge (FRE-1688).
 */

import { describe, it, expect } from 'vitest';

import { mergeDevJob, parseDevJobCard } from '@/lib/dev-job';
import type { DevJobCardData } from '@/lib/types';

function payload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    event: 'state',
    job_id: 'job-1',
    mode: 'build',
    state: 'running',
    ended: false,
    step: null,
    spend_usd: '0.1',
    cap_usd: '2.36',
    failure_reason: null,
    check_result: null,
    repo_url: null,
    result_artifact_id: null,
    main_moved: null,
    ...overrides,
  };
}

describe('parseDevJobCard', () => {
  it('accepts a well-formed payload', () => {
    expect(parseDevJobCard(payload())?.job_id).toBe('job-1');
  });

  it.each([
    ['null', null],
    ['a string', 'running'],
    ['an unknown state', payload({ state: 'exploding' })],
    ['a missing job id', payload({ job_id: undefined })],
    ['a numeric spend', payload({ spend_usd: 0.1 })],
  ])('ignores %s', (_name, value) => {
    expect(parseDevJobCard(value)).toBeNull();
  });
});

describe('parseDevJobCard — check_result', () => {
  it('keeps a well-formed result', () => {
    const result = { passed: false, steps: [{ name: 'load', passed: false, messages: ['x'] }] };
    expect(parseDevJobCard(payload({ check_result: result }))?.check_result).toEqual(result);
  });

  it('reads steps that is null as no steps', () => {
    const card = parseDevJobCard(payload({ check_result: { passed: false, steps: null } }));
    expect(card?.check_result).toEqual({ passed: false, steps: [] });
  });

  it.each([
    ['steps that is a string', { passed: false, steps: 'x' }],
    ['a step that is a string', { passed: false, steps: ['x'] }],
    ['a step with no name', { passed: false, steps: [{ passed: false }] }],
    ['messages that is not a list', { passed: false, steps: [{ name: 'a', passed: false, messages: 'x' }] }],
    ['messages with a number', { passed: false, steps: [{ name: 'a', passed: false, messages: [1] }] }],
  ])('drops a result with %s and keeps the card', (_name, result) => {
    const card = parseDevJobCard(payload({ check_result: result }));
    expect(card).not.toBeNull();
    expect(card?.check_result).toBeNull();
  });
});

describe('mergeDevJob', () => {
  const running = parseDevJobCard(payload({ step: 'tool shell' })) as DevJobCardData;
  const ended = parseDevJobCard(
    payload({ event: 'end', state: 'failed', ended: true }),
  ) as DevJobCardData;

  it('takes the first card', () => {
    expect(mergeDevJob(null, running)).toBe(running);
  });

  it('keeps an ended card when an older card of the same job arrives', () => {
    expect(mergeDevJob(ended, running)).toBe(ended);
  });

  it('replaces a card with a newer one', () => {
    expect(mergeDevJob(running, ended)).toBe(ended);
  });

  it('shows the card of another job', () => {
    const other = parseDevJobCard(payload({ job_id: 'job-2' })) as DevJobCardData;
    expect(mergeDevJob(ended, other)).toBe(other);
  });

  it('keeps the latest step when a snapshot of the same running job has none', () => {
    const snapshot = parseDevJobCard(payload({ state: 'checking', step: null })) as DevJobCardData;
    const merged = mergeDevJob(running, snapshot);
    expect(merged.state).toBe('checking');
    expect(merged.step).toBe('tool shell');
  });
});
