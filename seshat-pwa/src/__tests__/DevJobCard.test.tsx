/**
 * Tests for DevJobCard (FRE-1688, ADR-0159 D10.6): state, latest step, spend, and the result.
 */

import { render, screen } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

vi.mock('@/components/ArtifactCard', () => ({
  ArtifactCard: ({ artifactId }: { artifactId: string }) => (
    <div data-testid="artifact-card">{artifactId}</div>
  ),
}));

import { DevJobCard } from '@/components/DevJobCard';
import { formatUsd } from '@/lib/dev-job';
import type { DevJobCardData } from '@/lib/types';

function card(overrides: Partial<DevJobCardData> = {}): DevJobCardData {
  return {
    event: 'state',
    job_id: '11111111-1111-1111-1111-111111111111',
    mode: 'build',
    state: 'running',
    ended: false,
    step: null,
    spend_usd: '0.250000',
    cap_usd: '2.360000',
    failure_reason: null,
    check_result: null,
    repo_url: null,
    result_artifact_id: null,
    main_moved: null,
    ...overrides,
  };
}

describe('DevJobCard — a running job', () => {
  it('shows the state and the latest step', () => {
    render(<DevJobCard card={card({ state: 'running', step: 'tool shell' })} />);
    expect(screen.getByText('Building')).toBeDefined();
    expect(screen.getByText('tool shell')).toBeDefined();
  });

  it('shows no agent step once the job is checking or publishing', () => {
    render(<DevJobCard card={card({ state: 'checking', step: 'tool shell' })} />);
    expect(screen.getByText('Checking the page')).toBeDefined();
    expect(screen.queryByText('tool shell')).toBeNull();
  });

  it('is a status region that does not interrupt the reader', () => {
    render(<DevJobCard card={card()} />);
    expect(screen.getByRole('status').getAttribute('aria-live')).toBe('polite');
  });

  it('shows the spend of the payload against the cap', () => {
    const data = card({ spend_usd: '0.250000', cap_usd: '2.360000' });
    render(<DevJobCard card={data} />);
    expect(screen.getByTestId('dev-job-spend').textContent).toBe(
      `${formatUsd(data.spend_usd)} of ${formatUsd(data.cap_usd)}`,
    );
    expect(screen.getByTestId('dev-job-spend').textContent).toBe('$0.25 of $2.36');
  });

  it('shows a spend below one cent with four decimals, so it is not shown as zero', () => {
    expect(formatUsd('0.004200')).toBe('$0.0042');
    expect(formatUsd('0.000000')).toBe('$0.00');
    expect(formatUsd(null)).toBe('—');
  });

  it('shows no cap when none is known', () => {
    render(<DevJobCard card={card({ cap_usd: null })} />);
    expect(screen.getByTestId('dev-job-spend').textContent).toBe('$0.25');
  });
});

describe('DevJobCard — a job that ended', () => {
  it('shows the artifact card for a job that succeeded', () => {
    render(
      <DevJobCard
        card={card({
          event: 'end',
          state: 'succeeded',
          ended: true,
          result_artifact_id: '22222222-2222-2222-2222-222222222222',
        })}
      />,
    );
    expect(screen.getByText('Done')).toBeDefined();
    expect(screen.getByTestId('artifact-card').textContent).toBe(
      '22222222-2222-2222-2222-222222222222',
    );
  });

  it('says that main did not move when it did not', () => {
    render(
      <DevJobCard
        card={card({
          event: 'end',
          state: 'succeeded',
          ended: true,
          result_artifact_id: '22222222-2222-2222-2222-222222222222',
          main_moved: false,
        })}
      />,
    );
    expect(screen.getByText(/main did not move to this result/i)).toBeDefined();
  });

  it('says nothing about main when it moved', () => {
    render(
      <DevJobCard
        card={card({ event: 'end', state: 'succeeded', ended: true, main_moved: true })}
      />,
    );
    expect(screen.queryByText(/main did not move/i)).toBeNull();
  });

  it('shows the failure with the repo link and the failed check step', () => {
    render(
      <DevJobCard
        card={card({
          event: 'end',
          state: 'failed_check',
          ended: true,
          failure_reason: 'check_failed',
          repo_url: 'https://github.com/example-org/page-abc',
          check_result: {
            passed: false,
            steps: [
              { name: 'load', passed: true, messages: [] },
              { name: 'interaction', passed: false, messages: ['clicking Save threw'] },
            ],
          },
        })}
      />,
    );
    expect(screen.getByText('Check failed')).toBeDefined();
    expect(screen.getByText(/interaction/)).toBeDefined();
    expect(screen.getByText(/clicking Save threw/)).toBeDefined();
    const link = screen.getByRole('link', { name: /repo/i });
    expect(link.getAttribute('href')).toBe('https://github.com/example-org/page-abc');
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(screen.queryByTestId('artifact-card')).toBeNull();
  });

  it('explains a restart in plain words', () => {
    render(
      <DevJobCard
        card={card({
          event: 'end',
          state: 'failed',
          ended: true,
          failure_reason: 'supervisor_restart',
        })}
      />,
    );
    expect(screen.getByText(/gateway restarted/i)).toBeDefined();
  });

  it('shows an unknown failure reason as it is', () => {
    render(
      <DevJobCard
        card={card({ event: 'end', state: 'failed', ended: true, failure_reason: 'agent: no_result' })}
      />,
    );
    expect(screen.getByText(/agent: no_result/)).toBeDefined();
  });

  it('is not a live region once the job ended', () => {
    render(<DevJobCard card={card({ event: 'end', state: 'failed', ended: true })} />);
    expect(screen.getByRole('status').getAttribute('aria-live')).toBe('off');
  });
});
